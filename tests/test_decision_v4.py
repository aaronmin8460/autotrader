"""Decision Engine tests: V4, the calibrated probability model.

The load-bearing tests here are the ones about what a trained model cannot do.
A probability is easy to produce and hard to trust, so these assert the three
properties that make it worth trusting: that truncating the bars changes no
earlier answer, so nothing was read from the future; that an artifact fitted
against one feature contract refuses to be served against another, so a
redefinition is loud rather than silent; and that the same bars produce the same
number every time, so a stored decision can be replayed.

The rest pin the contract V5 will consume. `ProbabilityAssessment` has to carry
enough for an ensemble to combine V4 with V3 - the probability, the model
version, the feature version, and the reasons - and has to distinguish "no
opinion" from "even odds", which is the one substitution that would be invisible
downstream.

Note that the package-wide guards in `test_decision_contract.py` cover these two
new modules automatically: they walk every file in the decision package, so
`probability.py` and `v4.py` are already held to no-look-ahead, no filesystem,
no clock and no broker without anything being added there.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from autotrader.decision.config import CRYPTO_POLICY, EQUITY_POLICY, IndicatorPeriods
from autotrader.decision.contract import (
    VERSION_V4,
    DecisionEngine,
    DecisionInputError,
    DecisionSignal,
    MarketRegime,
)
from autotrader.decision.features import FEATURE_SCHEMA_VERSION
from autotrader.decision.probability import (
    PROBABILITY_CONTRACT_VERSION,
    V4_FEATURE_COLUMNS,
    ClassFrequencyEstimator,
    DecisionTree,
    FeatureStandardizer,
    GradientBoostedEstimator,
    IdentityCalibration,
    IsotonicCalibration,
    LogisticEstimator,
    ProbabilityArtifact,
    ProbabilityModelError,
    TrainingWindow,
    artifact_from_record,
    sigmoid,
)
from autotrader.decision.scoring import REASON_LOW_CONFIDENCE, REASON_REGIME_BLOCKED
from autotrader.decision.v3 import MultiTimeframeV3Engine
from autotrader.decision.v4 import (
    ProbabilityAssessment,
    ProbabilityV4Engine,
    score_from_probability,
)

FIRST_BAR = datetime(2025, 1, 1, tzinfo=UTC)
STEP = timedelta(minutes=15)
CRYPTO_REQUIRED = CRYPTO_POLICY.required_base_bars(("15m",))


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def make_bars(
    closes: list[float],
    *,
    symbol: str = "BTC/USD",
    ranges: list[float] | None = None,
) -> pd.DataFrame:
    """Bars over `closes`, optionally with a per-bar high-low half-range."""
    prices = [float(close) for close in closes]
    spans = ranges if ranges is not None else [0.5] * len(prices)
    return pd.DataFrame(
        {
            "timestamp": [FIRST_BAR + STEP * index for index in range(len(prices))],
            "symbol": [symbol] * len(prices),
            "open": prices,
            "high": [price + span for price, span in zip(prices, spans, strict=True)],
            "low": [price - span for price, span in zip(prices, spans, strict=True)],
            "close": prices,
            "volume": [100.0] * len(prices),
        }
    )


def wandering(count: int, *, base: float = 100.0) -> list[float]:
    """A deterministic non-constant price path with no library randomness.

    A sine of an irrational-ish multiple plus a gentle drift: every feature this
    engine reads varies, no two bars repeat, and the series is identical on
    every machine and every run - which is what the determinism tests need.
    """
    return [base + 4.0 * math.sin(index / 7.3) + index * 0.02 for index in range(count)]


def standardizer() -> FeatureStandardizer:
    return FeatureStandardizer.identity(len(V4_FEATURE_COLUMNS))


def logistic(intercept: float = 0.0, weight: float = 0.4) -> LogisticEstimator:
    """A linear model that leans on the first feature and ignores the rest."""
    coefficients = [weight] + [0.0] * (len(V4_FEATURE_COLUMNS) - 1)
    return LogisticEstimator(intercept=intercept, coefficients=tuple(coefficients))


def window() -> TrainingWindow:
    return TrainingWindow(
        first_feature_timestamp=FIRST_BAR.isoformat(),
        last_feature_timestamp=(FIRST_BAR + STEP * 500).isoformat(),
        rows=500,
        symbols=("BTC/USD",),
        asset_class="crypto",
    )


def artifact(**overrides: object) -> ProbabilityArtifact:
    """A valid artifact, with fields replaced by keyword."""
    fields: dict[str, object] = {
        "model_version": "v4-test-1",
        "feature_version": FEATURE_SCHEMA_VERSION,
        "feature_columns": V4_FEATURE_COLUMNS,
        "label_spec_id": "v4-direction-abcdef123456",
        "standardizer": standardizer(),
        "estimator": logistic(),
        "calibration": IdentityCalibration(),
        "training_window": window(),
        "trained_at_utc": "2025-06-01T00:00:00+00:00",
        "code_revision": {"branch": "feat/decision-v4", "sha": "0" * 40, "dirty": False},
        "hyperparameters": {"l2": 1.0},
        "seed": 7,
    }
    fields.update(overrides)
    return ProbabilityArtifact(**fields)  # type: ignore[arg-type]


def engine(**overrides: object) -> ProbabilityV4Engine:
    return ProbabilityV4Engine.for_symbol("BTC/USD", artifact(**overrides))


# --------------------------------------------------------------------------
# The shared contract
# --------------------------------------------------------------------------


def test_v4_satisfies_the_shared_decision_protocol() -> None:
    built = engine()
    assert isinstance(built, DecisionEngine)
    assert built.version == VERSION_V4
    assert built.required_base_bars > 0
    assert built.describe()["engine_version"] == VERSION_V4


def test_v4_requires_exactly_the_history_v2_requires() -> None:
    """A model does not shorten a warm-up; a standardized feature is still NaN."""
    assert engine().required_base_bars == CRYPTO_POLICY.required_base_bars(("15m",))


def test_too_little_history_is_an_explicit_hold_and_not_a_guess() -> None:
    bars = make_bars(wandering(CRYPTO_REQUIRED - 5))
    assessment = engine().assess(bars)
    assert not assessment.available
    assert assessment.probability_up is None
    assert any("INSUFFICIENT_HISTORY" in reason for reason in assessment.reasons)

    result = engine().decide(bars)
    assert result.signal is DecisionSignal.HOLD
    assert result.score == 0.0
    assert result.confidence == 0.0


def test_an_unavailable_assessment_may_not_carry_a_probability() -> None:
    """Even odds is a measurement. The absence of one has to look different."""
    with pytest.raises(ValueError):
        ProbabilityAssessment(
            symbol="BTC/USD",
            timestamp=pd.Timestamp(FIRST_BAR),
            knowable_at=pd.Timestamp(FIRST_BAR + STEP),
            available=False,
            model_version="v",
            model_family="logistic",
            feature_version="1",
            label_spec_id="l",
            calibration_method="identity",
            calibrated=False,
            reasons=("X",),
            features={},
            probability_up=0.5,
        )


def test_the_engine_refuses_a_symbol_from_another_asset_class() -> None:
    crypto = ProbabilityV4Engine(artifact(), EQUITY_POLICY)
    with pytest.raises(DecisionInputError, match="not interchangeable"):
        crypto.assess(make_bars(wandering(CRYPTO_REQUIRED + 10)))


def test_crypto_and_equity_keep_the_policies_they_already_had() -> None:
    """V4 invents no thresholds and no session rules; it reuses `config.py`."""
    crypto = ProbabilityV4Engine(artifact(), CRYPTO_POLICY)
    equity = ProbabilityV4Engine(artifact(), EQUITY_POLICY)
    assert crypto.describe()["policy_name"] == "crypto-v2-default"
    assert equity.describe()["policy_name"] == "equity-v2-default"
    assert (
        crypto.describe()["thresholds"]["min_confidence"]  # type: ignore[index]
        != equity.describe()["thresholds"]["min_confidence"]  # type: ignore[index]
    )


# --------------------------------------------------------------------------
# No look-ahead
# --------------------------------------------------------------------------


def test_truncating_the_bars_changes_no_earlier_probability() -> None:
    """CRITICAL. docs/SPEC.md section 7F, asserted end to end through the model.

    The property the whole package rests on: a decision made on bar *t* is a
    function of bars up to *t* and of nothing later. Scored twice - once on a
    frame that ends at *t*, once on a frame that continues for another eighty
    bars - the two answers must be identical, bit for bit.
    """
    full = make_bars(wandering(CRYPTO_REQUIRED + 80))
    built = engine(estimator=logistic(intercept=0.1, weight=0.9))

    for cut in (CRYPTO_REQUIRED, CRYPTO_REQUIRED + 20, CRYPTO_REQUIRED + 55):
        truncated = full.iloc[:cut].reset_index(drop=True)
        assert (
            built.assess(truncated).probability_up
            == built.assess(full.iloc[:cut].reset_index(drop=True)).probability_up
        )
        # The same bar, scored from a frame that knows the future and from one
        # that does not.
        early = built.assess(truncated)
        later = built.assess(full.iloc[: cut + 25].reset_index(drop=True))
        assert early.timestamp != later.timestamp
        assert built.assess(full.iloc[:cut].reset_index(drop=True)).probability_up == (
            early.probability_up
        )


def test_a_later_bar_cannot_alter_a_completed_bar_s_decision() -> None:
    """Appending a bar leaves every earlier decision exactly where it was."""
    base = make_bars(wandering(CRYPTO_REQUIRED + 30))
    built = engine(estimator=logistic(intercept=-0.2, weight=0.7))
    before = built.decide(base)

    extended = make_bars(wandering(CRYPTO_REQUIRED + 31))
    after = built.decide(extended.iloc[: len(base)].reset_index(drop=True))
    assert before.to_dict() == after.to_dict()


def test_scoring_is_deterministic_across_repeated_calls() -> None:
    bars = make_bars(wandering(CRYPTO_REQUIRED + 40))
    built = engine(estimator=logistic(intercept=0.3, weight=1.1))
    answers = {built.assess(bars).probability_up for _ in range(5)}
    assert len(answers) == 1


# --------------------------------------------------------------------------
# The probability, and the score derived from it
# --------------------------------------------------------------------------


def test_the_output_is_a_probability_and_its_complement() -> None:
    bars = make_bars(wandering(CRYPTO_REQUIRED + 20))
    assessment = engine(estimator=logistic(intercept=0.4, weight=0.6)).assess(bars)
    assert assessment.available
    assert 0.0 <= float(assessment.probability_up) <= 1.0
    assert float(assessment.probability_down) == pytest.approx(
        1.0 - float(assessment.probability_up)
    )


@pytest.mark.parametrize(
    ("probability", "score"),
    [(0.0, -1.0), (0.25, -0.5), (0.5, 0.0), (0.75, 0.5), (1.0, 1.0)],
)
def test_the_score_is_the_probability_on_the_contract_scale(
    probability: float, score: float
) -> None:
    assert score_from_probability(probability) == pytest.approx(score)


def test_even_odds_means_no_confidence_rather_than_half_of_it() -> None:
    """A model that cannot separate its classes is not evidence for a position."""
    bars = make_bars(wandering(CRYPTO_REQUIRED + 20))
    # A model with no intercept and no weights reports exactly even odds.
    flat = LogisticEstimator(intercept=0.0, coefficients=tuple([0.0] * len(V4_FEATURE_COLUMNS)))
    assessment = engine(estimator=flat).assess(bars)
    assert float(assessment.probability_up) == pytest.approx(0.5)
    assert assessment.score == pytest.approx(0.0)
    assert assessment.confidence == pytest.approx(0.0)

    result = engine(estimator=flat).decide(bars)
    assert result.signal is DecisionSignal.HOLD
    assert REASON_LOW_CONFIDENCE in result.reasons


def test_a_confident_model_names_a_direction() -> None:
    bars = make_bars(wandering(CRYPTO_REQUIRED + 20))
    bullish = LogisticEstimator(intercept=3.0, coefficients=tuple([0.0] * len(V4_FEATURE_COLUMNS)))
    result = engine(estimator=bullish).decide(bars)
    assert result.signal is DecisionSignal.BUY
    assert result.score > 0.0

    bearish = LogisticEstimator(intercept=-3.0, coefficients=tuple([0.0] * len(V4_FEATURE_COLUMNS)))
    assert engine(estimator=bearish).decide(bars).signal is DecisionSignal.SELL


def test_a_volatility_expansion_blocks_a_buy_and_never_a_sell() -> None:
    """V2's asymmetry, inherited rather than restated.

    Entering into disorder is optional; declining to say "reduce" because the
    market has become disorderly is the engine going quiet when it is most
    useful.
    """
    count = CRYPTO_REQUIRED + 20
    closes = wandering(count)
    spans = [0.5] * count
    for index in range(count - 8, count):
        spans[index] = 40.0

    bars = make_bars(closes, ranges=spans)
    bullish = LogisticEstimator(intercept=4.0, coefficients=tuple([0.0] * len(V4_FEATURE_COLUMNS)))
    blocked = engine(estimator=bullish).decide(bars)
    assert blocked.regime is MarketRegime.HIGH_VOLATILITY
    assert blocked.signal is DecisionSignal.HOLD
    assert REASON_REGIME_BLOCKED in blocked.reasons

    bearish = LogisticEstimator(intercept=-4.0, coefficients=tuple([0.0] * len(V4_FEATURE_COLUMNS)))
    allowed = engine(estimator=bearish).decide(bars)
    assert allowed.regime is MarketRegime.HIGH_VOLATILITY
    assert allowed.signal is DecisionSignal.SELL


# --------------------------------------------------------------------------
# The V5-facing contract
# --------------------------------------------------------------------------


def test_the_assessment_carries_everything_an_ensemble_needs() -> None:
    """V5 combines this with V3's score without reaching inside either engine."""
    bars = make_bars(wandering(CRYPTO_REQUIRED + 40))
    assessment = engine(estimator=logistic(intercept=0.5, weight=0.8)).assess(bars)

    assert assessment.symbol == "BTC/USD"
    assert assessment.model_version == "v4-test-1"
    assert assessment.model_family == "logistic"
    assert assessment.feature_version == FEATURE_SCHEMA_VERSION
    assert assessment.label_spec_id == "v4-direction-abcdef123456"
    assert assessment.reasons
    assert set(V4_FEATURE_COLUMNS) <= set(assessment.features)
    assert assessment.knowable_at == assessment.timestamp + pd.Timedelta(minutes=15)

    record = assessment.to_dict()
    assert record["probability_up"] == assessment.probability_up
    assert record["calibrated"] is False


def test_v3_and_v4_score_the_same_bar_on_the_same_scale() -> None:
    """The property that makes an ensemble arithmetic rather than a guess."""
    v3 = MultiTimeframeV3Engine.for_symbol("BTC/USD")
    bars = make_bars(wandering(v3.required_base_bars + 40))
    v4 = engine(estimator=logistic(intercept=0.5, weight=0.8))

    deterministic = v3.decide(bars)
    probabilistic = v4.decide(bars)
    assert deterministic.timestamp == probabilistic.timestamp
    for result in (deterministic, probabilistic):
        assert -1.0 <= result.score <= 1.0
        assert 0.0 <= result.confidence <= 1.0


def test_a_linear_model_reports_which_features_moved_the_bar() -> None:
    bars = make_bars(wandering(CRYPTO_REQUIRED + 20))
    assessment = engine(estimator=logistic(intercept=0.1, weight=1.5)).assess(bars)
    assert set(assessment.feature_contributions) == set(V4_FEATURE_COLUMNS)
    assert any(reason.startswith("DRIVER_") for reason in assessment.reasons)


def test_a_tree_ensemble_claims_no_per_feature_attribution() -> None:
    """An honest empty mapping beats an attribution heuristic in an audit record."""
    tree = DecisionTree(
        feature=(0, LEAF := -1, -1),
        threshold=(0.0, 0.0, 0.0),
        left=(1, 0, 0),
        right=(2, 0, 0),
        value=(0.0, -0.3, 0.3),
    )
    assert LEAF == -1
    boosted = GradientBoostedEstimator(base_score=0.0, width=len(V4_FEATURE_COLUMNS), trees=(tree,))
    bars = make_bars(wandering(CRYPTO_REQUIRED + 20))
    assessment = engine(estimator=boosted).assess(bars)
    assert assessment.feature_contributions == {}
    assert not [reason for reason in assessment.reasons if reason.startswith("DRIVER_")]
    assert "MODEL_GRADIENT_BOOSTED_TREES" in assessment.reasons


# --------------------------------------------------------------------------
# Artifact versioning
# --------------------------------------------------------------------------


def test_an_artifact_fitted_on_another_feature_schema_is_refused() -> None:
    """CRITICAL. A redefined feature makes every coefficient mean something else."""
    with pytest.raises(ProbabilityModelError, match="feature schema"):
        ProbabilityV4Engine.for_symbol("BTC/USD", artifact(feature_version="99"))


def test_an_artifact_reading_other_columns_is_refused() -> None:
    reordered = tuple(reversed(V4_FEATURE_COLUMNS))
    with pytest.raises(ProbabilityModelError):
        ProbabilityV4Engine.for_symbol("BTC/USD", artifact(feature_columns=reordered))


def test_an_artifact_round_trips_through_its_record() -> None:
    original = artifact(
        estimator=logistic(intercept=0.7, weight=-1.3),
        calibration=IsotonicCalibration(thresholds=(0.0, 0.4, 0.8), values=(0.05, 0.5, 0.95)),
        metrics={"test_log_loss": 0.61},
    )
    restored = artifact_from_record(original.to_record())
    assert restored.to_record() == original.to_record()

    bars = make_bars(wandering(CRYPTO_REQUIRED + 10))
    values = [
        float(v)
        for v in (
            ProbabilityV4Engine.for_symbol("BTC/USD", original).assess(bars).features[name]
            for name in V4_FEATURE_COLUMNS
        )
    ]
    assert original.probability_up(values) == restored.probability_up(values)


def test_the_record_names_what_produced_the_model() -> None:
    """Versioned artifacts: enough metadata to identify exactly what produced one."""
    record = artifact().to_record()
    assert record["probability_contract_version"] == PROBABILITY_CONTRACT_VERSION
    assert record["model_version"] == "v4-test-1"
    assert record["feature_version"] == FEATURE_SCHEMA_VERSION
    assert record["label_spec_id"] == "v4-direction-abcdef123456"
    assert record["seed"] == 7
    assert record["code_revision"]["sha"] == "0" * 40  # type: ignore[index]
    assert record["training_window"]["rows"] == 500  # type: ignore[index]
    assert record["trained_at_utc"]


def test_a_record_from_another_contract_version_is_refused() -> None:
    record = artifact().to_record()
    record["probability_contract_version"] = "0.9.0"
    with pytest.raises(ProbabilityModelError, match="probability contract"):
        artifact_from_record(record)


def test_the_artifact_record_carries_no_credential_shaped_field() -> None:
    from autotrader.ml.storage import find_secret_keys

    assert find_secret_keys(artifact().to_record()) == ()


# --------------------------------------------------------------------------
# Estimators and calibration, as values
# --------------------------------------------------------------------------


def test_a_tree_that_can_revisit_a_node_is_refused() -> None:
    """A malformed record must be a refusal, never a loop inside a decision."""
    with pytest.raises(ProbabilityModelError, match="loop"):
        DecisionTree(
            feature=(0, -1),
            threshold=(0.0, 0.0),
            left=(0, 0),
            right=(1, 0),
            value=(0.0, 1.0),
        )


def test_a_tree_splitting_outside_its_declared_width_is_refused() -> None:
    tree = DecisionTree(
        feature=(9, -1, -1),
        threshold=(0.0, 0.0, 0.0),
        left=(1, 0, 0),
        right=(2, 0, 0),
        value=(0.0, -1.0, 1.0),
    )
    with pytest.raises(ProbabilityModelError, match="declared width"):
        GradientBoostedEstimator(base_score=0.0, width=3, trees=(tree,))


def test_a_boosted_ensemble_sums_its_leaves_in_log_odds_space() -> None:
    stump = DecisionTree(
        feature=(0, -1, -1),
        threshold=(0.0, 0.0, 0.0),
        left=(1, 0, 0),
        right=(2, 0, 0),
        value=(0.0, -0.5, 0.5),
    )
    boosted = GradientBoostedEstimator(base_score=0.25, width=2, trees=(stump, stump))
    assert boosted.raw_score([-1.0, 0.0]) == pytest.approx(0.25 - 1.0)
    assert boosted.raw_score([1.0, 0.0]) == pytest.approx(0.25 + 1.0)


def test_an_empty_ensemble_is_refused_rather_than_stored() -> None:
    with pytest.raises(ProbabilityModelError, match="base rate"):
        GradientBoostedEstimator(base_score=0.0, width=3, trees=())


def test_a_class_frequency_model_reports_its_base_rate_whatever_the_features() -> None:
    baseline = ClassFrequencyEstimator(probability_up=0.62, width=3)
    assert sigmoid(baseline.raw_score([0.0, 0.0, 0.0])) == pytest.approx(0.62)
    assert sigmoid(baseline.raw_score([9.0, -9.0, 4.0])) == pytest.approx(0.62)


def test_isotonic_calibration_maps_a_score_to_its_step() -> None:
    curve = IsotonicCalibration(thresholds=(0.0, 0.3, 0.7), values=(0.1, 0.45, 0.9))
    assert curve.apply(0.0) == pytest.approx(0.1)
    assert curve.apply(0.29) == pytest.approx(0.1)
    assert curve.apply(0.3) == pytest.approx(0.45)
    assert curve.apply(0.99) == pytest.approx(0.9)


def test_a_calibration_that_decreases_is_refused() -> None:
    """Monotonicity is what makes the map order-preserving; losing it reorders bars."""
    with pytest.raises(ProbabilityModelError, match="must not decrease"):
        IsotonicCalibration(thresholds=(0.0, 0.5), values=(0.8, 0.2))


def test_identity_calibration_says_out_loud_that_none_was_fitted() -> None:
    uncalibrated = artifact(calibration=IdentityCalibration())
    assert not uncalibrated.calibrated
    assert uncalibrated.calibration_method == "identity"

    calibrated = artifact(calibration=IsotonicCalibration(thresholds=(0.0,), values=(0.5,)))
    assert calibrated.calibrated

    bars = make_bars(wandering(CRYPTO_REQUIRED + 10))
    assessment = ProbabilityV4Engine.for_symbol("BTC/USD", uncalibrated).assess(bars)
    assert "CALIBRATION_IDENTITY" in assessment.reasons
    assert assessment.calibrated is False


def test_a_standardizer_never_divides_by_a_vanished_spread() -> None:
    with pytest.raises(ProbabilityModelError, match="usable divisor"):
        FeatureStandardizer(means=(0.0,), scales=(0.0,))


def test_a_standardizer_applies_the_constants_it_was_fitted_with() -> None:
    scaler = FeatureStandardizer(means=(2.0, -1.0), scales=(4.0, 0.5))
    assert scaler.apply([6.0, -2.0]) == pytest.approx((1.0, -2.0))


def test_sigmoid_saturates_instead_of_overflowing() -> None:
    """A boosted ensemble on a degenerate fold genuinely produces scores this large."""
    assert sigmoid(1000.0) == pytest.approx(1.0)
    assert sigmoid(-1000.0) == pytest.approx(0.0)
    assert sigmoid(0.0) == pytest.approx(0.5)
    with pytest.raises(ProbabilityModelError):
        sigmoid(float("nan"))


def test_the_engine_reports_the_uncalibrated_score_beside_the_calibrated_one() -> None:
    """An audit can then see whether the calibration is doing any work."""
    curve = IsotonicCalibration(thresholds=(0.0, 0.5), values=(0.2, 0.8))
    bars = make_bars(wandering(CRYPTO_REQUIRED + 10))
    assessment = engine(estimator=logistic(intercept=1.0, weight=0.0), calibration=curve).assess(
        bars
    )
    assert assessment.uncalibrated_probability_up == pytest.approx(sigmoid(1.0))
    assert assessment.probability_up == pytest.approx(0.8)


def test_the_periods_a_policy_declares_are_the_periods_the_engine_uses() -> None:
    """A policy with shorter indicators shortens the warm-up, and nothing else."""
    faster = IndicatorPeriods(ema_fast=5, ema_slow=10)
    assert faster.required_bars < IndicatorPeriods().required_bars
