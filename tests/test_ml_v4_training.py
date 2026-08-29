"""V4 training tests: the leakage guards, and the evidence behind the model choice.

Everything here is offline, needs no credentials, and touches no network. The
tests that matter are the ones that would still pass if the training code were
subtly wrong and only fail when it leaks:

* **The scaler sees the training fold only.** Perturbing the test rows must not
  move a single fitted parameter. A standardizer fitted before the split would
  fail this immediately, and nothing else would notice it ever again.
* **Every split moves forward in time.** Walk-forward folds train on the past
  and are graded on their own future, and a training row whose label resolves
  inside the test window is purged rather than counted.
* **What is fitted is what is served.** A probability produced by the training
  pipeline and one produced by the live V4 engine on the same bar have to be the
  same number, because they are meant to be the same model.
* **The same inputs produce the same artifact.** Twice, byte for byte.

`tests/test_decision_v4.py` holds the serving half of V4's contract; this file
holds the half that reads data.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from autotrader.decision.config import CRYPTO_POLICY
from autotrader.decision.features import FEATURE_SCHEMA_VERSION, compute_features
from autotrader.decision.probability import (
    FAMILY_CLASS_FREQUENCY,
    FAMILY_GRADIENT_BOOSTED,
    FAMILY_LOGISTIC,
    V4_FEATURE_COLUMNS,
    IsotonicCalibration,
    LogisticEstimator,
    artifact_from_record,
)
from autotrader.decision.v4 import ProbabilityV4Engine
from autotrader.equity.session import session_from_local
from autotrader.ml import AssetClass, MLError
from autotrader.ml import v4 as v4_module
from autotrader.ml.dataset import frame_fingerprint
from autotrader.ml.grid import BarGrid, crypto_grid, equity_grid
from autotrader.ml.labels import LabelKind, LabelSpec, SessionPolicy, ThresholdMode
from autotrader.ml.registry import ArtifactStage, ModelRegistry, RegistryError
from autotrader.ml.schema import ColumnRole
from autotrader.ml.splits import SplitSpec, walk_forward_folds
from autotrader.ml.v4 import (
    MATERIAL_LOG_LOSS_IMPROVEMENT,
    Candidate,
    CandidateResult,
    FoldResult,
    V4TrainingError,
    build_training_frame,
    compare_candidates,
    default_candidates,
    default_label_spec,
    experiment_for,
    fit_isotonic,
    fit_logistic,
    fit_standardizer,
    log_loss,
    register_model,
    roc_auc,
    select_candidate,
    train_model,
    v4_schema,
    write_comparison,
)
from test_runtime import code_without_prose

CRYPTO_SYMBOL = "BTC/USD"
EQUITY_SYMBOL = "SPY"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
FIRST_SESSION = date(2026, 3, 2)

#: Long enough that the default indicator warm-up leaves a frame worth splitting
#: three ways and running walk-forward folds over.
BAR_COUNT = 900


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def synthetic_bars(
    timestamps: list[datetime],
    *,
    symbol: str = CRYPTO_SYMBOL,
    seed: int = 11,
    base: float = 50_000.0,
) -> pd.DataFrame:
    """A deterministic geometric walk over exactly `timestamps`.

    A walk rather than a straight line, because several tests compare fitted
    parameters for equality and a constant series would make them pass whatever
    the fitting code did.
    """
    rng = np.random.default_rng(seed)
    count = len(timestamps)
    close = base * np.exp(np.cumsum(rng.normal(0.0, 0.002, count)))
    high = close * (1.0 + np.abs(rng.normal(0.0, 0.001, count)))
    low = close * (1.0 - np.abs(rng.normal(0.0, 0.001, count)))
    open_ = np.clip(np.concatenate([[close[0]], close[:-1]]), low, high)
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(timestamps, utc=True),
            "symbol": pd.array([symbol] * count, dtype="string"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.uniform(1.0, 10.0, count),
        }
    )


def crypto_bars(count: int = BAR_COUNT, *, seed: int = 11) -> pd.DataFrame:
    return synthetic_bars([T0 + timedelta(minutes=15 * index) for index in range(count)], seed=seed)


def grid_for(bars: pd.DataFrame) -> BarGrid:
    return crypto_grid(
        bars["timestamp"].iloc[0].to_pydatetime(), bars["timestamp"].iloc[-1].to_pydatetime()
    )


def market_sessions(count: int = 40, *, early_closes: tuple[int, ...] = (5,)) -> tuple:
    """`count` consecutive weekday sessions, some of them 13:00 early closes."""
    built = []
    day = FIRST_SESSION
    while len(built) < count:
        if day.weekday() < 5:
            close_hour = 13 if len(built) in early_closes else 16
            built.append(
                session_from_local(
                    day,
                    datetime.combine(day, time(9, 30)),
                    datetime.combine(day, time(close_hour, 0)),
                )
            )
        day += timedelta(days=1)
    return tuple(built)


def crypto_training(count: int = BAR_COUNT, *, seed: int = 11):
    bars = crypto_bars(count, seed=seed)
    return build_training_frame(bars, grid=grid_for(bars))


def equity_training(sessions: int = 40):
    grid = equity_grid(market_sessions(count=sessions))
    bars = synthetic_bars(list(grid.starts), symbol=EQUITY_SYMBOL, seed=5, base=400.0)
    return build_training_frame(bars, grid=grid)


def logistic_candidate() -> Candidate:
    return Candidate(
        name="logistic-l2",
        family=FAMILY_LOGISTIC,
        hyperparameters={"l2": 1.0, "max_iterations": 50, "tolerance": 1e-8},
    )


def graded(name: str, family: str, losses: list[float]) -> CandidateResult:
    """A synthetic comparison result with a chosen mean log loss."""
    return CandidateResult(
        candidate=Candidate(name=name, family=family),
        folds=tuple(
            FoldResult(fold=index, train_rows=100, test_rows=50, metrics={"log_loss": loss})
            for index, loss in enumerate(losses)
        ),
    )


# --------------------------------------------------------------------------
# The training frame
# --------------------------------------------------------------------------


def test_a_training_frame_matches_its_declared_column_contract() -> None:
    training = crypto_training()
    training.schema.validate_frame(training.frame)
    assert training.schema.version == FEATURE_SCHEMA_VERSION
    assert training.schema.feature_names == V4_FEATURE_COLUMNS
    assert training.row_count > 0


def test_v4_trains_on_the_features_the_live_engine_computes() -> None:
    """CRITICAL. Train/serve skew is silent, and this is where it would start."""
    bars = crypto_bars()
    training = build_training_frame(bars, grid=grid_for(bars))
    live = compute_features(bars, periods=CRYPTO_POLICY.timeframe("15m").periods)

    row = training.frame.iloc[-1]
    matching = live.loc[live["timestamp"] == row["feature_timestamp"]].iloc[0]
    for name in V4_FEATURE_COLUMNS:
        assert float(row[name]) == pytest.approx(float(matching[name]))


def test_no_feature_column_declares_a_forward_horizon() -> None:
    """CRITICAL. The schema refuses to express a feature that reads the future."""
    schema = v4_schema(default_label_spec())
    for column in schema.columns:
        if column.role is ColumnRole.FEATURE:
            assert column.forward_bars == 0, column.name
    assert schema.max_forward_bars == default_label_spec().exit_offset_bars


def test_a_warm_up_row_is_dropped_rather_than_filled() -> None:
    """A model taught that an unmeasured feature was zero is confidently wrong."""
    training = crypto_training()
    assert training.dropped_warmup_rows > 0
    assert training.frame.loc[:, list(V4_FEATURE_COLUMNS)].notna().all().all()


def test_the_unlabelled_tail_is_present_but_never_trained_on() -> None:
    """The last rows have no future to measure, and are exactly what a live V4 scores."""
    training = crypto_training()
    assert training.labelled_row_count < training.row_count
    tail = training.frame.tail(default_label_spec().exit_offset_bars)
    assert not bool(tail["label_valid"].fillna(False).any())
    assert tail["label"].isna().all()


def test_every_row_is_stamped_with_when_it_could_first_have_existed() -> None:
    """Completed bars only: a row exists one bar interval after its feature bar."""
    training = crypto_training()
    delta = training.frame["knowable_at"] - training.frame["feature_timestamp"]
    assert (delta == pd.Timedelta(minutes=15)).all()


def test_the_label_measures_an_interval_that_starts_after_the_feature_bar() -> None:
    """docs/SPEC.md section 6F: a decision on bar t cannot be filled inside bar t."""
    labelled = crypto_training().frame
    labelled = labelled.loc[labelled["label_valid"].fillna(False)]
    assert (labelled["label_entry_timestamp"] > labelled["feature_timestamp"]).all()
    assert (labelled["label_exit_timestamp"] > labelled["label_entry_timestamp"]).all()
    assert (labelled["label_knowable_at"] > labelled["label_exit_timestamp"]).all()


def test_an_equity_frame_is_built_on_sessions_and_a_crypto_frame_is_not() -> None:
    """The session semantics each asset class already uses, unchanged."""
    equity = equity_training()
    assert equity.asset_class is AssetClass.EQUITY
    assert equity.frame["session_id"].nunique() > 1
    # A holding period that crosses a session gap is marked, not hidden.
    assert bool(equity.frame["label_spans_session_gap"].fillna(False).any())

    crypto = crypto_training()
    assert crypto.asset_class is AssetClass.CRYPTO
    assert not bool(crypto.frame["label_spans_session_gap"].fillna(False).any())


def test_an_overnight_holding_period_can_be_excluded_on_equity_only() -> None:
    grid = equity_grid(market_sessions(count=40))
    bars = synthetic_bars(list(grid.starts), symbol=EQUITY_SYMBOL, seed=5, base=400.0)
    within = build_training_frame(
        bars, grid=grid, label=default_label_spec(session_policy=SessionPolicy.WITHIN_SESSION)
    )
    labelled = within.frame.loc[within.frame["label_valid"].fillna(False)]
    assert not bool(labelled["label_spans_session_gap"].fillna(False).any())

    crypto = crypto_bars()
    with pytest.raises(MLError, match="continuous"):
        build_training_frame(
            crypto,
            grid=grid_for(crypto),
            label=default_label_spec(session_policy=SessionPolicy.WITHIN_SESSION),
        )


def test_a_grid_from_the_other_asset_class_is_refused() -> None:
    bars = crypto_bars(200)
    with pytest.raises(V4TrainingError, match="crypto symbol but the grid"):
        build_training_frame(bars, grid=equity_grid(market_sessions(count=5)))


# --------------------------------------------------------------------------
# Temporal splitting, purging, and the absence of a shuffle
# --------------------------------------------------------------------------


def test_walk_forward_folds_only_ever_move_forward() -> None:
    """CRITICAL. No random k-fold over time-ordered rows, ever."""
    training = crypto_training()
    folds = walk_forward_folds(training.frame, folds=4, initial_train_fraction=0.5)
    assert len(folds) == 4
    for fold in folds:
        assert (
            fold.train.frame["feature_timestamp"].max() < fold.test.frame["feature_timestamp"].min()
        )
    # Each fold's test window begins after the previous one's did.
    starts = [fold.test.frame["feature_timestamp"].min() for fold in folds]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)


def test_a_training_label_never_resolves_inside_its_own_test_window() -> None:
    """CRITICAL. Purging, checked on the column that can detect the violation."""
    training = crypto_training()
    for fold in walk_forward_folds(training.frame, folds=4, embargo_bars=2):
        boundary = fold.test.frame["feature_timestamp"].min()
        assert (fold.train.frame["label_knowable_at"] <= boundary).all()


def test_an_embargo_removes_bars_the_purge_alone_would_keep() -> None:
    training = crypto_training()
    without = walk_forward_folds(training.frame, folds=3, embargo_bars=0)
    with_embargo = walk_forward_folds(training.frame, folds=3, embargo_bars=25)
    for plain, embargoed in zip(without, with_embargo, strict=True):
        assert embargoed.train.row_count < plain.train.row_count
        assert embargoed.train.embargoed_rows > 0


def test_the_training_module_names_no_shuffling_construct() -> None:
    """CRITICAL, asserted against the parse tree rather than against prose."""
    source = code_without_prose(Path(inspect.getfile(v4_module)).read_text(encoding="utf-8"))
    for token in (
        "shuffle",
        "KFold",
        "train_test_split",
        "permutation",
        "default_rng",
        "RandomState",
        "sample(",
    ):
        assert token not in source, token


def test_the_training_module_contains_no_look_ahead_construct() -> None:
    text = Path(inspect.getfile(v4_module)).read_text(encoding="utf-8")
    source = code_without_prose(text)
    for token in ("bfill", "backfill", "ffill", "[::-1]", "ascending=False"):
        assert token not in source, token
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "shift":
                for argument in node.args:
                    assert not (
                        isinstance(argument, ast.UnaryOp) and isinstance(argument.op, ast.USub)
                    )
            for keyword in node.keywords:
                assert keyword.arg != "center"


# --------------------------------------------------------------------------
# Leakage: what the fitting sees
# --------------------------------------------------------------------------


def test_perturbing_the_test_rows_moves_no_fitted_parameter() -> None:
    """CRITICAL. The single sharpest test in this file.

    A standardizer or an estimator fitted before the split would read the test
    period's values. Multiplying every test-split feature by a large constant
    would then move the fitted parameters, and nothing downstream would ever
    report it. Fitted on the training rows alone, the artifact is untouched.
    """
    training = crypto_training()
    split = SplitSpec()
    honest = train_model(
        training,
        logistic_candidate(),
        model_version="v4-leak-1",
        split=split,
        seed=3,
        trained_at=T0,
    )

    tampered = training.frame.copy()
    boundary = int(len(tampered) * 0.85)
    for name in V4_FEATURE_COLUMNS:
        tampered.loc[boundary:, name] = tampered.loc[boundary:, name] * 1000.0 + 7.0
    contaminated = train_model(
        v4_module.TrainingFrame(
            frame=tampered,
            schema=training.schema,
            label=training.label,
            symbol=training.symbol,
            asset_class=training.asset_class,
            periods=training.periods,
            grid_row_count=training.grid_row_count,
            dropped_warmup_rows=training.dropped_warmup_rows,
            dropped_incomplete_window_rows=training.dropped_incomplete_window_rows,
        ),
        logistic_candidate(),
        model_version="v4-leak-1",
        split=split,
        seed=3,
        trained_at=T0,
    )

    assert honest.artifact.standardizer == contaminated.artifact.standardizer
    assert honest.artifact.estimator == contaminated.artifact.estimator


def test_the_calibration_is_fitted_on_validation_and_not_on_training() -> None:
    """Training scores are optimistic; a calibration fitted on them corrects a
    bias that will not be there live."""
    training = crypto_training()
    trained = train_model(
        training,
        logistic_candidate(),
        model_version="v4-cal-1",
        seed=3,
        trained_at=T0,
    )
    assert isinstance(trained.artifact.calibration, IsotonicCalibration)
    assert trained.artifact.calibrated
    # The calibration curve has at most as many steps as there were validation
    # rows, which is the only sample it can have been fitted from.
    assert len(trained.artifact.calibration.thresholds) <= trained.split.validation.row_count


def test_calibration_can_be_declined_and_says_so() -> None:
    training = crypto_training()
    trained = train_model(
        training,
        logistic_candidate(),
        model_version="v4-raw-1",
        seed=3,
        calibrate=False,
        trained_at=T0,
    )
    assert not trained.artifact.calibrated
    assert trained.artifact.calibration_method == "identity"


def test_a_split_that_leaves_a_part_empty_is_refused() -> None:
    training = crypto_training(count=200)
    tiny = training.frame.head(6)
    with pytest.raises(MLError):
        train_model(
            v4_module.TrainingFrame(
                frame=tiny,
                schema=training.schema,
                label=training.label,
                symbol=training.symbol,
                asset_class=training.asset_class,
                periods=training.periods,
                grid_row_count=training.grid_row_count,
                dropped_warmup_rows=0,
                dropped_incomplete_window_rows=0,
            ),
            logistic_candidate(),
            model_version="v4-tiny",
            trained_at=T0,
        )


# --------------------------------------------------------------------------
# Train/serve parity
# --------------------------------------------------------------------------


def test_the_trained_model_and_the_live_engine_agree_on_the_same_bar() -> None:
    """CRITICAL. One scoring implementation, verified rather than assumed."""
    bars = crypto_bars()
    training = build_training_frame(bars, grid=grid_for(bars))
    trained = train_model(
        training, logistic_candidate(), model_version="v4-parity-1", seed=3, trained_at=T0
    )
    built = ProbabilityV4Engine.for_symbol(CRYPTO_SYMBOL, trained.artifact)

    for offset in (1, 40, 120):
        row = training.frame.iloc[-offset]
        moment = row["feature_timestamp"]
        window = bars.loc[bars["timestamp"] <= moment].reset_index(drop=True)
        assessment = built.assess(window)

        assert assessment.timestamp == moment
        from_frame = trained.artifact.probability_up(
            [float(row[name]) for name in V4_FEATURE_COLUMNS]
        )
        assert assessment.probability_up == pytest.approx(from_frame, abs=1e-12)


def test_a_boosted_model_serves_what_it_was_fitted_as() -> None:
    training = crypto_training()
    boosted = Candidate(
        name="gradient-boosted",
        family=FAMILY_GRADIENT_BOOSTED,
        hyperparameters={
            "trees": 8,
            "max_depth": 2,
            "learning_rate": 0.1,
            "l2": 1.0,
            "min_samples_leaf": 25,
        },
    )
    trained = train_model(training, boosted, model_version="v4-gbt-1", seed=3, trained_at=T0)
    restored = artifact_from_record(trained.artifact.to_record())
    row = training.frame.iloc[-5]
    values = [float(row[name]) for name in V4_FEATURE_COLUMNS]
    assert restored.probability_up(values) == trained.artifact.probability_up(values)


# --------------------------------------------------------------------------
# Reproducibility
# --------------------------------------------------------------------------


def test_the_same_frame_seed_and_configuration_produce_the_same_artifact() -> None:
    """CRITICAL. A model that cannot be reproduced cannot be audited."""
    training = crypto_training()
    candidate = logistic_candidate()
    first = train_model(training, candidate, model_version="v4-repro-1", seed=42, trained_at=T0)
    second = train_model(training, candidate, model_version="v4-repro-1", seed=42, trained_at=T0)
    assert first.artifact.to_record() == second.artifact.to_record()
    assert first.test_metrics == second.test_metrics


def test_a_boosted_fit_is_reproducible_too() -> None:
    """The families that usually subsample do not, which is why this can pass."""
    training = crypto_training()
    boosted = Candidate(
        name="gradient-boosted",
        family=FAMILY_GRADIENT_BOOSTED,
        hyperparameters={"trees": 6, "max_depth": 2, "learning_rate": 0.1, "l2": 1.0},
    )
    first = train_model(training, boosted, model_version="v4-gbt-r", seed=1, trained_at=T0)
    second = train_model(training, boosted, model_version="v4-gbt-r", seed=1, trained_at=T0)
    assert first.artifact.to_record() == second.artifact.to_record()


def test_two_identical_runs_share_an_experiment_id_and_a_changed_one_does_not() -> None:
    training = crypto_training()
    trained = train_model(
        training, logistic_candidate(), model_version="v4-exp-1", seed=5, trained_at=T0
    )
    fingerprint = frame_fingerprint(training.frame)
    first = experiment_for(training, trained, name="v4", dataset_fingerprint=fingerprint)
    second = experiment_for(training, trained, name="v4", dataset_fingerprint=fingerprint)
    assert first.experiment_id == second.experiment_id

    other = Candidate(name="logistic-l2", family=FAMILY_LOGISTIC, hyperparameters={"l2": 9.0})
    changed = train_model(training, other, model_version="v4-exp-1", seed=5, trained_at=T0)
    assert (
        experiment_for(training, changed, name="v4", dataset_fingerprint=fingerprint).experiment_id
        != first.experiment_id
    )


def test_a_seed_is_recorded_even_where_the_family_ignores_it() -> None:
    """ "This run used seed 5 and ignored it" is a fact; "there was no seed" is a gap."""
    training = crypto_training()
    trained = train_model(
        training, logistic_candidate(), model_version="v4-seed", seed=5, trained_at=T0
    )
    assert trained.artifact.seed == 5
    assert trained.artifact.to_record()["seed"] == 5


# --------------------------------------------------------------------------
# Fitting
# --------------------------------------------------------------------------


def test_the_logistic_fit_recovers_a_signal_that_is_in_the_data() -> None:
    """A sanity floor: a solver that returned zeros would pass everything else."""
    rng = np.random.default_rng(0)
    matrix = rng.normal(size=(600, 3))
    odds = 1.5 * matrix[:, 0] - 1.0 * matrix[:, 1]
    labels = (odds > 0.0).astype("float64")
    fitted = fit_logistic(matrix, labels, l2=0.01)
    assert fitted.coefficients[0] > 0.0
    assert fitted.coefficients[1] < 0.0
    assert abs(fitted.coefficients[2]) < abs(fitted.coefficients[0])


def test_an_unpenalised_logistic_fit_is_refused() -> None:
    with pytest.raises(V4TrainingError, match="separable"):
        fit_logistic(np.zeros((4, 2)), np.array([0.0, 1.0, 0.0, 1.0]), l2=0.0)


def test_a_constant_feature_is_scaled_by_one_rather_than_by_zero() -> None:
    scaler = fit_standardizer(np.array([[1.0, 5.0], [3.0, 5.0]]))
    assert scaler.scales[1] == 1.0
    assert scaler.means[1] == 5.0


def test_isotonic_calibration_is_monotone_and_never_claims_certainty() -> None:
    """A bin that saw no positives has not proved a rate of zero."""
    scores = np.linspace(0.0, 1.0, 200)
    outcomes = (scores > 0.6).astype("float64")
    curve = fit_isotonic(scores, outcomes)
    assert list(curve.values) == sorted(curve.values)
    assert list(curve.thresholds) == sorted(set(curve.thresholds))
    assert min(curve.values) > 0.0
    assert max(curve.values) < 1.0


def test_isotonic_calibration_moves_a_miscalibrated_score_towards_the_truth() -> None:
    scores = np.concatenate([np.full(100, 0.9), np.full(100, 0.1)])
    outcomes = np.concatenate([np.full(100, 1.0), np.full(100, 0.0)])
    # A model that is right but overconfident in the other direction.
    inverted = fit_isotonic(np.concatenate([np.full(100, 0.9), np.full(100, 0.1)]), outcomes)
    assert inverted.apply(0.9) > inverted.apply(0.1)
    assert log_loss(np.asarray([inverted.apply(s) for s in scores]), outcomes) < log_loss(
        scores, outcomes
    )


def test_the_metrics_behave_the_way_model_selection_needs_them_to() -> None:
    perfect = np.array([0.99, 0.01, 0.99, 0.01])
    outcomes = np.array([1.0, 0.0, 1.0, 0.0])
    assert log_loss(perfect, outcomes) < log_loss(np.full(4, 0.5), outcomes)
    assert roc_auc(perfect, outcomes) == pytest.approx(1.0)
    assert roc_auc(np.full(4, 0.5), outcomes) == pytest.approx(0.5)
    # AUC is undefined on a single-class fold and reports the uninformative value.
    assert roc_auc(perfect, np.ones(4)) == pytest.approx(0.5)


def test_log_loss_is_finite_even_for_a_confidently_wrong_prediction() -> None:
    assert np.isfinite(log_loss(np.array([0.0, 1.0]), np.array([1.0, 0.0])))


# --------------------------------------------------------------------------
# The recorded comparison, and the model choice
# --------------------------------------------------------------------------


def test_the_comparison_grades_every_candidate_on_the_same_folds() -> None:
    training = crypto_training()
    comparison = compare_candidates(training, folds=3, seed=13)
    assert {result.candidate.name for result in comparison.results} == {
        candidate.name for candidate in default_candidates()
    }
    for result in comparison.results:
        assert len(result.folds) == comparison.fold_count
        assert set(result.mean_metrics) >= {
            "log_loss",
            "brier_score",
            "expected_calibration_error",
            "roc_auc",
        }


def test_the_comparison_record_states_how_it_was_produced() -> None:
    training = crypto_training()
    record = compare_candidates(training, folds=3, seed=13).to_record()
    assert record["walk_forward"]["scheme"].startswith("anchored walk-forward")  # type: ignore[index]
    assert record["feature_version"] == FEATURE_SCHEMA_VERSION
    assert record["rationale"]
    assert len(record["candidates"]) == 3  # type: ignore[arg-type]


def test_a_tie_is_broken_towards_the_simpler_family() -> None:
    """Complexity is a cost and has to be paid for with evidence."""
    chosen, rationale = select_candidate(
        (
            graded("baseline", FAMILY_CLASS_FREQUENCY, [0.693, 0.693]),
            graded("logistic", FAMILY_LOGISTIC, [0.600, 0.600]),
            graded("boosted", FAMILY_GRADIENT_BOOSTED, [0.5995, 0.5995]),
        )
    )
    assert chosen.family == FAMILY_LOGISTIC
    assert "simplest family" in rationale


def test_a_materially_better_complex_model_still_wins() -> None:
    chosen, _ = select_candidate(
        (
            graded("baseline", FAMILY_CLASS_FREQUENCY, [0.693, 0.693]),
            graded("logistic", FAMILY_LOGISTIC, [0.650, 0.650]),
            graded("boosted", FAMILY_GRADIENT_BOOSTED, [0.500, 0.500]),
        )
    )
    assert chosen.family == FAMILY_GRADIENT_BOOSTED


def test_a_candidate_that_cannot_beat_the_base_rate_is_refused() -> None:
    """A fair walk-forward that found nothing must be allowed to say so."""
    chosen, rationale = select_candidate(
        (
            graded("baseline", FAMILY_CLASS_FREQUENCY, [0.693, 0.693]),
            graded("logistic", FAMILY_LOGISTIC, [0.6929, 0.6929]),
            graded("boosted", FAMILY_GRADIENT_BOOSTED, [0.700, 0.700]),
        )
    )
    assert chosen.family == FAMILY_CLASS_FREQUENCY
    assert "no candidate found a usable edge" in rationale
    assert str(MATERIAL_LOG_LOSS_IMPROVEMENT) in rationale


def test_a_comparison_without_a_baseline_is_refused() -> None:
    with pytest.raises(V4TrainingError, match="floor"):
        select_candidate((graded("logistic", FAMILY_LOGISTIC, [0.4]),))


def test_a_comparison_is_written_where_the_evidence_lives(tmp_path: Path) -> None:
    training = crypto_training()
    comparison = compare_candidates(training, folds=3, seed=13)
    path = write_comparison(comparison, root=tmp_path)
    assert path.is_file()
    assert "rationale" in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Artifact registration
# --------------------------------------------------------------------------


def test_an_artifact_is_registered_with_the_provenance_that_identifies_it(
    tmp_path: Path,
) -> None:
    training = crypto_training()
    trained = train_model(
        training, logistic_candidate(), model_version="v4-reg-1", seed=3, trained_at=T0
    )
    fingerprint = frame_fingerprint(training.frame)
    experiment = experiment_for(training, trained, name="v4", dataset_fingerprint=fingerprint)
    registry = ModelRegistry(root=tmp_path / "registry")
    registered = register_model(
        trained,
        training,
        experiment=experiment,
        dataset_fingerprint=fingerprint,
        registry=registry,
        directory=tmp_path / "built",
    )

    assert registered.verify()
    assert registered.stage is ArtifactStage.EXPERIMENTAL
    metadata = registered.metadata
    assert metadata.feature_schema_version == FEATURE_SCHEMA_VERSION
    assert metadata.feature_schema_fingerprint == training.schema.fingerprint
    assert metadata.label_spec_id == training.label.identifier
    assert metadata.dataset_fingerprint == fingerprint
    assert metadata.experiment_id == experiment.experiment_id
    assert metadata.symbols == (CRYPTO_SYMBOL,)
    assert metadata.split["parts"]  # type: ignore[index]

    stored = artifact_from_record(
        __import__("json").loads(registered.artifact_path.read_text(encoding="utf-8"))
    )
    assert stored.model_version == "v4-reg-1"


def test_registering_the_same_version_twice_is_refused(tmp_path: Path) -> None:
    """Artifacts are immutable: a new fit is a new version, never an overwrite."""
    training = crypto_training()
    trained = train_model(
        training, logistic_candidate(), model_version="v4-once", seed=3, trained_at=T0
    )
    fingerprint = frame_fingerprint(training.frame)
    experiment = experiment_for(training, trained, name="v4", dataset_fingerprint=fingerprint)
    registry = ModelRegistry(root=tmp_path / "registry")
    arguments = {
        "experiment": experiment,
        "dataset_fingerprint": fingerprint,
        "registry": registry,
        "directory": tmp_path / "built",
    }
    register_model(trained, training, **arguments)  # type: ignore[arg-type]
    with pytest.raises(RegistryError, match="immutable"):
        register_model(trained, training, **arguments)  # type: ignore[arg-type]


def test_the_registry_has_no_stage_that_makes_a_model_trade(tmp_path: Path) -> None:
    """Turning V4 on is a deliberate change to a runtime, made somewhere else."""
    assert not [stage for stage in ArtifactStage if stage.name == "PRODUCTION"]
    source = Path(inspect.getfile(v4_module)).read_text(encoding="utf-8")
    for token in ("activate", "PRODUCTION", "submit_order", "TradingClient"):
        assert token not in code_without_prose(source), token


# --------------------------------------------------------------------------
# Boundaries
# --------------------------------------------------------------------------


def test_the_training_module_reaches_no_broker_and_no_runtime_loop() -> None:
    tree = ast.parse(Path(inspect.getfile(v4_module)).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for name in (
        "alpaca",
        "autotrader.execution",
        "autotrader.risk",
        "autotrader.state",
        "autotrader.account",
        "autotrader.reconciliation",
        "autotrader.runtime.runner",
        "autotrader.runtime.execution",
        "autotrader.equity.runtime",
    ):
        assert not [module for module in imported if module.startswith(name)], name


def test_a_label_specification_still_refuses_a_same_bar_entry() -> None:
    with pytest.raises(MLError, match="entry_offset_bars"):
        LabelSpec(
            name="same-bar",
            kind=LabelKind.DIRECTION,
            horizon_bars=4,
            entry_offset_bars=0,
            threshold_mode=ThresholdMode.ABSOLUTE,
        )


def test_the_default_target_is_a_tradable_forward_interval() -> None:
    spec = default_label_spec()
    assert spec.kind is LabelKind.DIRECTION
    assert spec.entry_offset_bars >= 1
    assert spec.entry_price_column == "open"
    assert spec.exit_price_column == "open"
    assert "grid bar(s) after the feature bar" in spec.describe()


def test_a_fitted_logistic_model_is_the_kind_the_engine_can_attribute() -> None:
    training = crypto_training()
    trained = train_model(
        training, logistic_candidate(), model_version="v4-attr", seed=3, trained_at=T0
    )
    assert isinstance(trained.artifact.estimator, LogisticEstimator)
    row = training.frame.iloc[-1]
    contributions = trained.artifact.feature_contributions(
        [float(row[name]) for name in V4_FEATURE_COLUMNS]
    )
    assert set(contributions) == set(V4_FEATURE_COLUMNS)
