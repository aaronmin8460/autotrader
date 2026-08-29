"""M1 contract tests: storage, the prediction contract, calibration, the registry.

Where `test_ml_dataset.py` proves the data layer does not see the future, this
file proves the surrounding contracts hold: that heavy artifacts cannot land on
the internal disk, that a credential cannot reach a metadata file, that a
prediction that is not a distribution cannot exist, that an artifact is
identified by its bytes, and that an experiment record is enough to know
whether two runs should agree.

It also pins the architectural boundary in both directions. `autotrader.ml`
imports no execution, risk, state, reconciliation or runtime-loop module, and
nothing outside it except the CLI imports `autotrader.ml`. Training is not
coupled to the broker, and this is what makes that a checkable claim rather
than an intention.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from autotrader import __version__
from autotrader.cli import app as autotrader_app
from autotrader.ml import AssetClass
from autotrader.ml import registry as registry_module
from autotrader.ml.calibration import (
    BinnedCalibrator,
    CalibrationError,
    Calibrator,
    IdentityCalibrator,
    brier_score,
    expected_calibration_error,
    reliability_table,
)
from autotrader.ml.dataset import DatasetSpec, build_dataset, dataset_schema
from autotrader.ml.experiment import (
    ExperimentError,
    GitProvenance,
    experiment_path,
    new_experiment,
    write_experiment,
)
from autotrader.ml.grid import crypto_grid
from autotrader.ml.labels import LabelKind, LabelSpec
from autotrader.ml.model import (
    MODEL_CONTRACT_VERSION,
    PROBABILITY_DOWN,
    PROBABILITY_UP,
    ClassFrequencyModel,
    ModelError,
    Prediction,
    PredictionContext,
    ProbabilityModel,
    build_predictions,
)
from autotrader.ml.registry import (
    ArtifactMetadata,
    ArtifactStage,
    ModelRegistry,
    RegistryError,
    artifact_filename,
    artifact_version_of,
)
from autotrader.ml.schema import FEATURE_SCHEMA_VERSION
from autotrader.ml.splits import SplitSpec
from autotrader.ml.storage import (
    DATASETS_ENV,
    MODELS_ENV,
    REPORTS_ENV,
    SETUP_HINT,
    StorageError,
    dataset_root,
    find_secret_keys,
    model_root,
    read_json,
    report_root,
    sha256_of_file,
    sha256_of_record,
    write_json,
)
from autotrader.runtime.schedule import BAR_INTERVAL

T0 = datetime(2026, 1, 1, tzinfo=UTC)
SOURCE_ROOT = Path(registry_module.__file__).resolve().parents[1]
ML_ROOT = SOURCE_ROOT / "ml"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def sample_bars(count: int = 200) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    close = 50_000 * np.exp(np.cumsum(rng.normal(0.0, 0.002, count)))
    high = close * 1.001
    low = close * 0.999
    open_ = np.clip(np.concatenate([[close[0]], close[:-1]]), low, high)
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [T0 + timedelta(minutes=15 * index) for index in range(count)], utc=True
            ),
            "symbol": pd.array(["BTC/USD"] * count, dtype="string"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.uniform(1.0, 10.0, count),
            "trade_count": rng.integers(1, 100, count).astype("float64"),
            "vwap": close,
        }
    )


def direction_label() -> LabelSpec:
    return LabelSpec(name="dir", kind=LabelKind.DIRECTION, horizon_bars=4)


def sample_dataset(count: int = 300) -> pd.DataFrame:
    bars = sample_bars(count)
    return build_dataset(
        bars,
        spec=DatasetSpec(symbol="BTC/USD", label=direction_label()),
        grid=crypto_grid(
            bars["timestamp"].iloc[0].to_pydatetime(), bars["timestamp"].iloc[-1].to_pydatetime()
        ),
    ).frame


def code_without_prose(source: str) -> str:
    """`source` with every docstring removed.

    The guards below scan executable code. These modules document the words
    they refuse - "there is no PRODUCTION stage" - so a naive substring scan
    would trip over the sentence explaining the rule it is checking.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def imported_modules(path: Path) -> set[str]:
    """Every module named by an `import` or `from ... import` in one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def prediction(**overrides: object) -> Prediction:
    defaults: dict[str, object] = {
        "model_version": "1.0.0",
        "artifact_version": "a" * 64,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "label_spec_id": direction_label().identifier,
        "symbol": "BTC/USD",
        "asset_class": AssetClass.CRYPTO,
        "timestamp": T0,
        "knowable_at": T0 + BAR_INTERVAL,
        "probability_down": 0.4,
        "probability_up": 0.6,
        "calibrated_confidence": 0.6,
    }
    defaults.update(overrides)
    return Prediction(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Storage: heavy artifacts go external, and never carry a secret
# --------------------------------------------------------------------------


def test_an_unset_storage_root_is_refused_with_the_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable, resolver in (
        (DATASETS_ENV, dataset_root),
        (MODELS_ENV, model_root),
        (REPORTS_ENV, report_root),
    ):
        monkeypatch.delenv(variable, raising=False)
        with pytest.raises(StorageError) as error:
            resolver()
        assert variable in str(error.value)
        assert SETUP_HINT in str(error.value)


def test_a_missing_storage_root_is_not_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The unmounted-volume failure mode: mkdir would fill the internal disk."""
    absent = tmp_path / "not-mounted"
    monkeypatch.setenv(DATASETS_ENV, str(absent))
    with pytest.raises(StorageError, match="not mounted"):
        dataset_root()
    assert not absent.exists()


def test_a_storage_root_that_is_a_file_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "a-file"
    target.write_text("", encoding="utf-8")
    monkeypatch.setenv(MODELS_ENV, str(target))
    with pytest.raises(StorageError, match="not a directory"):
        model_root()


def test_a_credential_shaped_key_is_refused_at_any_depth(tmp_path: Path) -> None:
    nested = {"outer": [{"inner": {"alpaca_api_key": "x"}}]}
    assert find_secret_keys(nested) == ("outer[0].inner.alpaca_api_key",)
    with pytest.raises(StorageError, match="credential-shaped"):
        write_json(tmp_path / "record.json", nested)
    assert not (tmp_path / "record.json").exists()


@pytest.mark.parametrize(
    "key", ["api_key", "SECRET_KEY", "password", "access_token", "private_key", "Authorization"]
)
def test_every_credential_marker_is_caught(tmp_path: Path, key: str) -> None:
    with pytest.raises(StorageError):
        write_json(tmp_path / "record.json", {key: "value"})


def test_an_ordinary_record_round_trips(tmp_path: Path) -> None:
    payload = {"symbol": "BTC/USD", "rows": 12}
    path = write_json(tmp_path / "record.json", payload)
    assert read_json(path) == payload


def test_a_record_fingerprint_ignores_key_order() -> None:
    assert sha256_of_record({"a": 1, "b": 2}) == sha256_of_record({"b": 2, "a": 1})


def test_a_file_fingerprint_follows_its_bytes(tmp_path: Path) -> None:
    path = tmp_path / "blob.bin"
    path.write_bytes(b"one")
    first = sha256_of_file(path)
    path.write_bytes(b"two")
    assert sha256_of_file(path) != first


# --------------------------------------------------------------------------
# The prediction contract
# --------------------------------------------------------------------------


def test_a_valid_prediction_reports_every_field_v4_needs() -> None:
    record = prediction().to_record()
    for field in (
        "model_version",
        "artifact_version",
        "feature_schema_version",
        "symbol",
        "timestamp_utc",
        "probability_up",
        "probability_down",
        "probability_neutral",
        "calibrated_confidence",
    ):
        assert field in record
    assert record["model_contract_version"] == MODEL_CONTRACT_VERSION


def test_probabilities_that_do_not_sum_to_one_are_refused() -> None:
    with pytest.raises(ModelError, match="sum to 1"):
        prediction(probability_down=0.4, probability_up=0.4)


def test_a_probability_outside_the_unit_interval_is_refused() -> None:
    with pytest.raises(ModelError, match=r"in \[0, 1\]"):
        prediction(probability_down=-0.1, probability_up=1.1)


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(Exception, match="timezone-aware"):
        prediction(timestamp=datetime(2026, 1, 1), knowable_at=datetime(2026, 1, 1))


def test_a_prediction_may_not_claim_to_be_knowable_before_its_bar_closed() -> None:
    with pytest.raises(ModelError, match="one bar interval after"):
        prediction(knowable_at=T0)


def test_a_ternary_prediction_carries_a_neutral_class() -> None:
    ternary = prediction(probability_down=0.2, probability_neutral=0.5, probability_up=0.3)
    assert ternary.is_ternary is True
    assert ternary.predicted_class == "HOLD"
    assert prediction().is_ternary is False


def test_a_tie_breaks_away_from_taking_a_position() -> None:
    assert prediction(probability_down=0.5, probability_up=0.5).predicted_class == "DOWN"
    tied = prediction(probability_down=0.5, probability_neutral=0.5, probability_up=0.0)
    assert tied.predicted_class == "HOLD"


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


def test_the_reference_baseline_satisfies_the_model_protocol() -> None:
    assert isinstance(ClassFrequencyModel(label_kind=LabelKind.DIRECTION), ProbabilityModel)


def test_the_reference_baseline_is_deterministic() -> None:
    frame = sample_dataset()
    usable = frame.loc[frame["label_valid"].fillna(False)]
    features = usable[list(dataset_schema(direction_label()).feature_names)]

    first = ClassFrequencyModel(label_kind=LabelKind.DIRECTION)
    second = ClassFrequencyModel(label_kind=LabelKind.DIRECTION)
    first.fit(features, usable["label"], seed=0)
    second.fit(features, usable["label"], seed=0)
    pd.testing.assert_frame_equal(first.predict_proba(features), second.predict_proba(features))


def test_the_reference_baseline_predicts_the_training_base_rate() -> None:
    frame = sample_dataset()
    usable = frame.loc[frame["label_valid"].fillna(False)]
    features = usable[list(dataset_schema(direction_label()).feature_names)]
    model = ClassFrequencyModel(label_kind=LabelKind.DIRECTION)
    model.fit(features, usable["label"], seed=0)

    probabilities = model.predict_proba(features)
    observed = float((usable["label"].astype("int64") == 1).mean())
    assert probabilities[PROBABILITY_UP].iloc[0] == pytest.approx(observed)
    assert probabilities[PROBABILITY_UP].nunique() == 1


def test_a_continuous_target_has_no_classes_to_predict() -> None:
    baseline = ClassFrequencyModel(label_kind=LabelKind.FORWARD_RETURN)
    with pytest.raises(ModelError, match="no classes"):
        _ = baseline.class_values


def test_predictions_inherit_the_identity_of_the_rows_they_came_from() -> None:
    frame = sample_dataset()
    usable = frame.loc[frame["label_valid"].fillna(False)].reset_index(drop=True).head(20)
    features = usable[list(dataset_schema(direction_label()).feature_names)]
    model = ClassFrequencyModel(label_kind=LabelKind.DIRECTION)
    model.fit(features, usable["label"], seed=0)

    context = PredictionContext(
        model_version="1.0.0",
        artifact_version="b" * 64,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        label_spec_id=direction_label().identifier,
    )
    predictions = build_predictions(usable, model.predict_proba(features), context=context)

    assert len(predictions) == len(usable)
    assert predictions[0].symbol == "BTC/USD"
    assert predictions[0].asset_class is AssetClass.CRYPTO
    assert predictions[0].timestamp == usable["feature_timestamp"].iloc[0].to_pydatetime()
    assert predictions[0].knowable_at == predictions[0].timestamp + BAR_INTERVAL


def test_probability_columns_of_the_wrong_shape_are_refused() -> None:
    frame = sample_dataset().head(5)
    context = PredictionContext("1.0.0", "c" * 64, FEATURE_SCHEMA_VERSION, "dir")
    wrong = pd.DataFrame({"p_up": np.full(5, 1.0)})
    with pytest.raises(ModelError, match="Probabilities must hold"):
        build_predictions(frame, wrong, context=context)


def test_a_calibrator_shapes_the_reported_confidence() -> None:
    frame = sample_dataset()
    usable = frame.loc[frame["label_valid"].fillna(False)].reset_index(drop=True).head(30)
    probabilities = pd.DataFrame(
        {PROBABILITY_DOWN: np.full(30, 0.25), PROBABILITY_UP: np.full(30, 0.75)}
    )
    context = PredictionContext("1.0.0", "d" * 64, FEATURE_SCHEMA_VERSION, "dir")

    flattening = BinnedCalibrator(bin_count=2)
    flattening.fit(np.full(40, 0.75), np.concatenate([np.ones(20), np.zeros(20)]))
    calibrated = build_predictions(usable, probabilities, context=context, calibrator=flattening)
    raw = build_predictions(usable, probabilities, context=context)
    assert raw[0].calibrated_confidence == pytest.approx(0.75)
    assert calibrated[0].calibrated_confidence == pytest.approx(0.5)


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------


def test_the_identity_calibrator_changes_nothing() -> None:
    scores = np.array([0.1, 0.5, 0.9])
    calibrator = IdentityCalibrator()
    calibrator.fit(scores, np.array([0, 1, 1]))
    assert np.allclose(calibrator.transform(scores), scores)
    assert calibrator.to_record() == {"method": "identity"}


def test_both_calibrators_satisfy_the_protocol() -> None:
    assert isinstance(IdentityCalibrator(), Calibrator)
    assert isinstance(BinnedCalibrator(), Calibrator)


def test_binning_corrects_a_systematically_overconfident_model() -> None:
    rng = np.random.default_rng(5)
    outcomes = rng.binomial(1, 0.3, 4000).astype("float64")
    # The model says 0.9 whenever the event happens 30% of the time.
    scores = np.where(outcomes > 0, 0.92, 0.88)

    before = expected_calibration_error(scores, outcomes)
    calibrator = BinnedCalibrator(bin_count=5)
    calibrator.fit(scores, outcomes)
    after = expected_calibration_error(calibrator.transform(scores), outcomes)
    assert before > 0.4
    assert after < before


def test_an_unobserved_bin_keeps_the_models_own_answer() -> None:
    calibrator = BinnedCalibrator(bin_count=10)
    calibrator.fit(np.array([0.05, 0.05, 0.95, 0.95]), np.array([0.0, 0.0, 1.0, 1.0]))
    assert calibrator.transform(np.array([0.55]))[0] == pytest.approx(0.55)


def test_an_unfitted_calibrator_refuses_to_transform() -> None:
    with pytest.raises(CalibrationError, match="has not been fitted"):
        BinnedCalibrator().transform(np.array([0.5]))


def test_a_non_binary_outcome_is_refused() -> None:
    with pytest.raises(CalibrationError, match="must be 0 or 1"):
        BinnedCalibrator().fit(np.array([0.5, 0.5]), np.array([0.0, 0.4]))


def test_a_score_outside_the_unit_interval_is_refused() -> None:
    with pytest.raises(CalibrationError, match=r"\[0, 1\]"):
        IdentityCalibrator().transform(np.array([1.5]))


def test_the_reliability_table_reports_empty_bins_rather_than_hiding_them() -> None:
    table = reliability_table(np.array([0.05, 0.95]), np.array([0.0, 1.0]), bin_count=10)
    assert len(table) == 10
    assert int(table["samples"].sum()) == 2
    assert table["observed_frequency"].isna().sum() == 8


def test_a_perfect_forecast_scores_zero_on_both_metrics() -> None:
    outcomes = np.array([1.0, 0.0, 1.0, 0.0])
    assert brier_score(outcomes, outcomes) == pytest.approx(0.0)
    assert expected_calibration_error(outcomes, outcomes) == pytest.approx(0.0)


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------


def artifact_metadata(path: Path, **overrides: object) -> ArtifactMetadata:
    defaults: dict[str, object] = {
        "model_name": "baseline",
        "model_version": "1.0.0",
        "artifact_version": artifact_version_of(path),
        "artifact_filename": "model.json",
        "created_at_utc": T0,
        "asset_class": "crypto",
        "symbols": ("BTC/USD",),
        "timeframe": "15m",
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_schema_fingerprint": "f" * 64,
        "label_spec": direction_label().to_record(),
        "label_spec_id": direction_label().identifier,
        "dataset_fingerprint": "d" * 64,
        "experiment_id": "e" * 64,
        "split": SplitSpec().to_record(),
        "hyperparameters": {"label_kind": "direction"},
        "calibration": {"method": "identity"},
        "metrics": {"brier": 0.21},
    }
    defaults.update(overrides)
    return ArtifactMetadata(**defaults)  # type: ignore[arg-type]


def model_file(tmp_path: Path, body: str = '{"weights": []}') -> Path:
    path = tmp_path / "model.json"
    path.write_text(body, encoding="utf-8")
    return path


def test_an_artifact_round_trips_through_the_registry(tmp_path: Path) -> None:
    blob = model_file(tmp_path)
    registry = ModelRegistry(tmp_path / "registry")
    registry.register(artifact_metadata(blob), blob)

    stored = registry.get("baseline", "1.0.0")
    assert stored.stage is ArtifactStage.EXPERIMENTAL
    assert stored.verify() is True
    assert stored.metadata.autotrader_version == __version__
    assert registry.list_models() == ("baseline",)


def test_a_stored_artifact_keeps_the_extension_it_arrived_with(tmp_path: Path) -> None:
    """So a reader can tell a pickle from a JSON without opening it."""
    assert artifact_filename("baseline", Path("trained.json")) == "model.json"
    assert artifact_filename("baseline", Path("booster.pkl")) == "model.pkl"


def test_an_artifact_is_immutable(tmp_path: Path) -> None:
    blob = model_file(tmp_path)
    registry = ModelRegistry(tmp_path / "registry")
    registry.register(artifact_metadata(blob), blob)
    with pytest.raises(RegistryError, match="already registered"):
        registry.register(artifact_metadata(blob), blob)


def test_metadata_that_disagrees_with_the_file_is_refused(tmp_path: Path) -> None:
    blob = model_file(tmp_path)
    registry = ModelRegistry(tmp_path / "registry")
    with pytest.raises(RegistryError, match="identified by its bytes"):
        registry.register(artifact_metadata(blob, artifact_version="0" * 64), blob)


def test_a_tampered_artifact_fails_verification(tmp_path: Path) -> None:
    blob = model_file(tmp_path)
    registry = ModelRegistry(tmp_path / "registry")
    stored = registry.register(artifact_metadata(blob), blob)
    stored.artifact_path.write_text("tampered", encoding="utf-8")
    assert registry.get("baseline", "1.0.0").verify() is False


def test_a_stage_change_records_why(tmp_path: Path) -> None:
    blob = model_file(tmp_path)
    registry = ModelRegistry(tmp_path / "registry")
    registry.register(artifact_metadata(blob), blob)
    registry.set_stage(
        "baseline", "1.0.0", ArtifactStage.CANDIDATE, reason="Beat the base rate on 5 folds.", at=T0
    )

    assert registry.get("baseline", "1.0.0").stage is ArtifactStage.CANDIDATE
    history = registry.stage_history("baseline", "1.0.0")
    assert [entry["stage"] for entry in history] == ["experimental", "candidate"]
    assert "5 folds" in str(history[-1]["reason"])


def test_a_stage_change_without_a_reason_is_refused(tmp_path: Path) -> None:
    blob = model_file(tmp_path)
    registry = ModelRegistry(tmp_path / "registry")
    registry.register(artifact_metadata(blob), blob)
    with pytest.raises(RegistryError, match="must record a reason"):
        registry.set_stage("baseline", "1.0.0", ArtifactStage.ARCHIVED, reason="  ")


def test_the_immutable_record_is_not_rewritten_by_a_stage_change(tmp_path: Path) -> None:
    blob = model_file(tmp_path)
    registry = ModelRegistry(tmp_path / "registry")
    stored = registry.register(artifact_metadata(blob), blob)
    record_path = stored.directory / "artifact.json"
    before = record_path.read_bytes()
    registry.set_stage("baseline", "1.0.0", ArtifactStage.ARCHIVED, reason="Superseded.", at=T0)
    assert record_path.read_bytes() == before


def test_versions_are_ordered_by_time_not_by_version_string(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path / "registry")
    for version, created in (("1.9.0", T0), ("1.10.0", T0 + timedelta(days=1))):
        blob = tmp_path / f"{version}.json"
        blob.write_text(f'{{"v": "{version}"}}', encoding="utf-8")
        registry.register(
            artifact_metadata(
                blob,
                model_version=version,
                artifact_version=artifact_version_of(blob),
                created_at_utc=created,
            ),
            blob,
        )
    assert [item.metadata.model_version for item in registry.list_versions("baseline")] == [
        "1.9.0",
        "1.10.0",
    ]
    assert registry.latest("baseline").metadata.model_version == "1.10.0"


def test_an_unregistered_model_is_a_controlled_failure(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path / "registry")
    with pytest.raises(RegistryError, match="not registered"):
        registry.get("absent", "1.0.0")
    with pytest.raises(RegistryError, match="No versions"):
        registry.latest("absent")


def test_the_registry_has_no_production_stage_and_cannot_activate_anything() -> None:
    """CRITICAL. Nothing in this package may turn a model into a trading decision."""
    assert {stage.value for stage in ArtifactStage} == {
        "experimental",
        "candidate",
        "archived",
    }
    source = code_without_prose(Path(registry_module.__file__).read_text(encoding="utf-8"))
    for forbidden in ("PRODUCTION", "activate", "promote", "deploy", "enable"):
        assert forbidden not in source, forbidden


# --------------------------------------------------------------------------
# Experiment records
# --------------------------------------------------------------------------


def sample_experiment(**overrides: object):
    defaults: dict[str, object] = {
        "name": "baseline-sweep",
        "seed": 17,
        "dataset_fingerprints": ("a" * 64,),
        "schema": dataset_schema(direction_label()),
        "label": direction_label(),
        "split": SplitSpec(),
        "model_name": "class-frequency-baseline",
        "model_version": "1.0.0",
        "hyperparameters": {"label_kind": "direction"},
        "created_at": T0,
    }
    defaults.update(overrides)
    return new_experiment(**defaults)  # type: ignore[arg-type]


def test_the_same_configuration_produces_the_same_experiment_id() -> None:
    assert sample_experiment().experiment_id == sample_experiment().experiment_id


def test_when_and_how_it_was_described_do_not_change_the_experiment_id() -> None:
    later = sample_experiment(created_at=T0 + timedelta(days=30), notes="Second attempt.")
    assert later.experiment_id == sample_experiment().experiment_id


def test_a_changed_determinant_changes_the_experiment_id() -> None:
    base = sample_experiment()
    assert sample_experiment(seed=18).experiment_id != base.experiment_id
    assert (
        sample_experiment(hyperparameters={"label_kind": "ternary"}).experiment_id
        != base.experiment_id
    )
    assert sample_experiment(split=SplitSpec(embargo_bars=5)).experiment_id != base.experiment_id
    assert (
        sample_experiment(
            label=LabelSpec(name="dir", kind=LabelKind.DIRECTION, horizon_bars=8)
        ).experiment_id
        != base.experiment_id
    )


def test_results_are_not_inputs_and_do_not_change_the_id() -> None:
    base = sample_experiment()
    assert base.with_metrics({"brier": 0.2}).experiment_id == base.experiment_id


def test_an_experiment_without_a_seed_is_refused() -> None:
    with pytest.raises(ExperimentError, match="cannot be reproduced"):
        sample_experiment(seed="none")


def test_an_experiment_must_name_the_data_it_used() -> None:
    with pytest.raises(ExperimentError, match="content"):
        sample_experiment(dataset_fingerprints=())


def test_an_experiment_record_is_written_with_its_provenance(tmp_path: Path) -> None:
    metadata = sample_experiment(
        git=GitProvenance(branch="feat/ml-foundation", sha="abc123", dirty=False)
    )
    path = write_experiment(metadata, root=tmp_path)
    assert path == experiment_path(metadata, root=tmp_path)

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["experiment_id"] == metadata.experiment_id
    assert record["identity"]["seed"] == 17
    assert record["git"]["sha"] == "abc123"
    assert record["libraries"]["autotrader"] == __version__


def test_an_unreadable_repository_is_unknown_rather_than_false() -> None:
    """`dirty=None` and `dirty=False` are different answers."""
    assert GitProvenance().to_record() == {"branch": None, "sha": None, "dirty": None}


# --------------------------------------------------------------------------
# The architectural boundary
# --------------------------------------------------------------------------


def test_the_ml_package_imports_no_trading_or_runtime_module() -> None:
    """CRITICAL. Training is not coupled to the broker or to execution.

    `autotrader.runtime.schedule` is the deliberate exception: it is pure
    15-minute boundary arithmetic with no client, no state and no loop, and
    reusing it is what keeps a bar interval meaning one thing project-wide.
    """
    forbidden = (
        "autotrader.execution",
        "autotrader.risk",
        "autotrader.state",
        "autotrader.reconciliation",
        "autotrader.account",
        "autotrader.backtest",
        "autotrader.dashboard",
        "autotrader.smoke",
        "autotrader.runtime.runner",
        "autotrader.runtime.execution",
        "autotrader.runtime.lock",
        "autotrader.equity.runtime",
        "alpaca",
    )
    for module in sorted(ML_ROOT.rglob("*.py")):
        for imported in imported_modules(module):
            for name in forbidden:
                assert not imported.startswith(name), f"{module.name} imports {imported}"


def test_only_the_cli_reaches_into_the_ml_package() -> None:
    """Nothing that trades may depend on the ML foundation."""
    for module in sorted(SOURCE_ROOT.rglob("*.py")):
        if ML_ROOT in module.parents or module.parent.name == "cli":
            continue
        for imported in imported_modules(module):
            assert not imported.startswith("autotrader.ml"), f"{module} imports {imported}"


def test_no_ml_module_names_an_order_or_a_broker_client() -> None:
    for module in sorted(ML_ROOT.rglob("*.py")):
        source = code_without_prose(module.read_text(encoding="utf-8"))
        for forbidden in (
            "TradingClient",
            "submit_order",
            "OrderRequest",
            "execute_paper_order",
            "reconcile_paper_state",
            "ALPACA_API_KEY",
            "ALPACA_SECRET_KEY",
        ):
            assert forbidden not in source, f"{module.name} names {forbidden}"


# --------------------------------------------------------------------------
# The CLI surface
# --------------------------------------------------------------------------


def test_the_ml_sub_application_is_attached() -> None:
    result = CliRunner(env={"COLUMNS": "200"}).invoke(autotrader_app, ["ml", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("build-dataset", "schema", "split", "registry-list", "registry-show"):
        assert command in result.output


def test_the_schema_command_reports_the_contract_without_touching_a_file() -> None:
    result = CliRunner(env={"COLUMNS": "200"}).invoke(
        autotrader_app, ["ml", "schema", "--label-kind", "direction", "--horizon-bars", "4"]
    )
    assert result.exit_code == 0, result.output
    assert FEATURE_SCHEMA_VERSION in result.output
    assert "grid bar(s) after the feature bar" in result.output


def test_the_cli_builds_and_splits_a_dataset_end_to_end(tmp_path: Path) -> None:
    bars_path = tmp_path / "bars.parquet"
    sample_bars(600).to_parquet(bars_path, engine="pyarrow", index=False)
    runner = CliRunner(env={"COLUMNS": "200"})

    built = runner.invoke(
        autotrader_app,
        [
            "ml",
            "build-dataset",
            str(bars_path),
            "--symbol",
            "BTC/USD",
            "--output-dir",
            str(tmp_path / "datasets"),
            "--label-kind",
            "direction",
            "--horizon-bars",
            "4",
        ],
    )
    assert built.exit_code == 0, built.output
    assert "Dataset built" in built.output

    produced = sorted((tmp_path / "datasets").glob("*.parquet"))
    assert len(produced) == 1
    split = runner.invoke(autotrader_app, ["ml", "split", str(produced[0])])
    assert split.exit_code == 0, split.output
    assert "Split is temporally clean" in split.output


def test_an_impossible_label_is_a_message_not_a_traceback(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        autotrader_app, ["ml", "schema", "--horizon-bars", "4", "--entry-offset-bars", "0"]
    )
    assert result.exit_code == 1
    assert "cannot be filled inside bar t" in result.output


def test_an_empty_registry_reports_itself(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        autotrader_app, ["ml", "registry-list", "--root", str(tmp_path / "registry")]
    )
    assert result.exit_code == 0, result.output
    assert "No artifacts registered" in result.output
