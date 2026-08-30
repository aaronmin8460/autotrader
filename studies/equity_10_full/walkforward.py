"""Training one study cell: symbol x window, at the frozen 4-bar horizon.

The horizon study's cell trainer with the horizon fixed where its final
classification froze it. Everything the selection depends on is the shipped
machinery, untouched: ``compare_candidates`` grades the shipped three families
on anchored sub-folds with the 26-bar embargo and label-aware purge;
``select_candidate`` applies the shipped 0.002 materiality gate. The gate is
not weakened, per the study's standing rule - the null is the finding unless a
candidate earns otherwise.

Every cell also trains and records, exactly as the horizon study did:

- a RAW null artifact - class frequency over all labelled training rows,
  identity calibration - the constant the out-of-sample comparison is measured
  against;
- SHADOW artifacts for the two non-selected families, so a "the gate is
  masking real signal" claim can be tested from the record. The winner rule
  never reads them.

Every trained model carries the calibration audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pandas as pd

from autotrader.decision.features import FEATURE_SCHEMA_VERSION
from autotrader.decision.probability import (
    FAMILY_CLASS_FREQUENCY,
    V4_FEATURE_COLUMNS,
    IdentityCalibration,
    ProbabilityArtifact,
    TrainingWindow,
)
from autotrader.equity.session import market_date
from autotrader.ml.grid import equity_grid
from autotrader.ml.labels import DIRECTION_UP
from autotrader.ml.v4 import (
    MATERIAL_LOG_LOSS_IMPROVEMENT,
    ModelComparison,
    TrainingFrame,
    build_training_frame,
    compare_candidates,
    default_candidates,
    default_label_spec,
    fit_class_frequency,
    fit_standardizer,
    select_candidate,
    train_model,
)
from studies.equity_10_full import STUDY_SEED
from studies.equity_10_full.calibration_audit import audit_calibration
from studies.equity_10_full.windows import EMBARGO_BARS, HORIZON_BARS
from studies.equity_v1_v5.calendar import SnapshotCalendar
from studies.equity_v1_v5.windows import ScoringWindow

#: Bars between the last training row and the first scored bar: the frozen
#: horizon plus one whole regular session, the pilot's own 30.
TRAIN_TEST_GAP_BARS = HORIZON_BARS + EMBARGO_BARS

#: The smallest training frame worth fitting on, unchanged from the pilot.
MIN_TRAINING_ROWS = 1500

#: Fixed so the artifact's own `trained_at` cannot make two identical runs
#: differ; the run's wall clock lives in the manifest instead.
TRAINED_AT = datetime(2026, 8, 30, tzinfo=UTC)


class CellError(Exception):
    """A study cell that cannot be trained on information available to it."""


@dataclass(frozen=True)
class CellModels:
    """Every model one cell trained, and the evidence behind the choice."""

    symbol: str
    window: str
    gap_bars: int
    training_rows: int
    labelled_rows: int
    training_first_bar: str
    training_last_bar: str
    scoring_first_bar: str
    label_base_rate: float
    labels_spanning_session_gap: int
    spanning_fraction: float
    selected_family: str
    selection_reason: str
    beat_baseline: bool
    baseline_log_loss: float
    selected_log_loss: float
    comparison: ModelComparison = field(repr=False)
    selected_artifact: ProbabilityArtifact = field(repr=False)
    null_artifact: ProbabilityArtifact = field(repr=False)
    shadow_artifacts: dict[str, ProbabilityArtifact] = field(repr=False)
    calibration_audits: dict[str, dict[str, object]] = field(repr=False)

    def to_json_dict(self) -> dict[str, object]:
        """The serializable cell record the checkpoint stores."""
        return {
            "symbol": self.symbol,
            "window": self.window,
            "horizon_bars": HORIZON_BARS,
            "gap_bars": self.gap_bars,
            "training_rows": self.training_rows,
            "labelled_rows": self.labelled_rows,
            "training_first_bar_utc": self.training_first_bar,
            "training_last_bar_utc": self.training_last_bar,
            "scoring_first_bar_utc": self.scoring_first_bar,
            "label_base_rate": self.label_base_rate,
            "labels_spanning_session_gap": self.labels_spanning_session_gap,
            "spanning_fraction": self.spanning_fraction,
            "selected_family": self.selected_family,
            "selection_reason": self.selection_reason,
            "beat_baseline": self.beat_baseline,
            "baseline_log_loss": self.baseline_log_loss,
            "selected_log_loss": self.selected_log_loss,
            "log_loss_improvement": self.baseline_log_loss - self.selected_log_loss,
            "material_improvement_threshold": MATERIAL_LOG_LOSS_IMPROVEMENT,
            "comparison": self.comparison.to_record(),
            "selected_artifact": self.selected_artifact.to_record(),
            "null_artifact": self.null_artifact.to_record(),
            "shadow_artifacts": {
                family: artifact.to_record()
                for family, artifact in sorted(self.shadow_artifacts.items())
            },
            "calibration_audits": self.calibration_audits,
        }


def training_frame_for(
    frame: pd.DataFrame,
    calendar: SnapshotCalendar,
    *,
    symbol: str,
    last_row: int,
) -> TrainingFrame:
    """V4 training rows from ``frame[:last_row]`` and nothing later.

    The slice is taken before the grid is built, so the grid spans only the
    sessions the training data covers and cannot describe a bar the model is
    not allowed to know about.
    """
    history = frame.iloc[: last_row + 1].reset_index(drop=True)
    if history.empty:
        raise CellError(f"{symbol} has no history before row {last_row}.")
    first = market_date(history["timestamp"].iloc[0].to_pydatetime())
    last = market_date(history["timestamp"].iloc[-1].to_pydatetime())
    sessions = calendar.sessions_between(first, last)
    if not sessions:
        raise CellError(f"{symbol}: the calendar reports no session in {first}..{last}.")
    return build_training_frame(history, grid=equity_grid(sessions), label=default_label_spec())


def _null_artifact(training: TrainingFrame, *, model_version: str) -> ProbabilityArtifact:
    """The raw class-frequency constant over every labelled training row."""
    labelled = training.frame.loc[
        training.frame["label_valid"].fillna(False).to_numpy(dtype=bool)
    ].reset_index(drop=True)
    labels = (labelled["label"].to_numpy(dtype="float64") == float(DIRECTION_UP)).astype("float64")
    matrix = labelled.loc[:, list(V4_FEATURE_COLUMNS)].to_numpy(dtype="float64")
    estimator = fit_class_frequency(labels, width=matrix.shape[1])
    standardizer = fit_standardizer(matrix)
    window = TrainingWindow(
        first_feature_timestamp=pd.Timestamp(labelled["feature_timestamp"].iloc[0]).isoformat(),
        last_feature_timestamp=pd.Timestamp(labelled["feature_timestamp"].iloc[-1]).isoformat(),
        rows=len(labelled),
        symbols=(training.symbol,),
        asset_class=training.asset_class.value,
    )
    return ProbabilityArtifact(
        model_version=model_version,
        feature_version=FEATURE_SCHEMA_VERSION,
        feature_columns=V4_FEATURE_COLUMNS,
        label_spec_id=training.label.identifier,
        standardizer=standardizer,
        estimator=estimator,
        calibration=IdentityCalibration(),
        training_window=window,
        trained_at_utc="",
        code_revision={},
        hyperparameters={},
        metrics={},
        seed=STUDY_SEED,
        notes="Raw class-frequency null benchmark for the ten-symbol study.",
    )


def train_cell(
    frame: pd.DataFrame,
    calendar: SnapshotCalendar,
    window: ScoringWindow,
    *,
    symbol: str,
    seed: int = STUDY_SEED,
) -> CellModels:
    """Fit every model one cell needs, on that window's past alone."""
    first_scored, _ = window.positions(frame)
    last_training_row = first_scored - TRAIN_TEST_GAP_BARS - 1
    if last_training_row < 0:
        raise CellError(
            f"{symbol}/{window.name}: the window opens at row {first_scored}, which leaves "
            f"no room for a {TRAIN_TEST_GAP_BARS}-bar gap."
        )

    training = training_frame_for(frame, calendar, symbol=symbol, last_row=last_training_row)
    if len(training.frame) < MIN_TRAINING_ROWS:
        raise CellError(
            f"{symbol}/{window.name}: only {len(training.frame)} training rows are "
            f"available; at least {MIN_TRAINING_ROWS} are required."
        )

    comparison = compare_candidates(
        training,
        candidates=default_candidates(),
        embargo_bars=EMBARGO_BARS,
        seed=seed,
    )
    selected, reason = select_candidate(comparison.results)

    version_stem = f"equity-10full-{symbol.lower()}-{window.name}"
    trained_by_family = {}
    audits: dict[str, dict[str, object]] = {}
    for candidate in default_candidates():
        trained = train_model(
            training,
            candidate,
            model_version=f"{version_stem}-{candidate.family}",
            seed=seed,
            trained_at=TRAINED_AT,
            notes=(
                f"Ten-symbol equity historical evaluation. "
                f"{'SELECTED' if candidate.family == selected.family else 'SHADOW'} for "
                f"{window.name}. Fitted on rows up to "
                f"{training.frame['feature_timestamp'].iloc[-1].isoformat()}, "
                f"{TRAIN_TEST_GAP_BARS} bars before the window opens."
            ),
        )
        trained_by_family[candidate.family] = trained
        audits[trained.artifact.model_version] = audit_calibration(trained)

    by_family = {result.candidate.family: result for result in comparison.results}
    baseline = by_family[FAMILY_CLASS_FREQUENCY]
    labelled = training.frame.loc[training.frame["label_valid"].fillna(False).to_numpy(dtype=bool)]
    spanning = int(labelled["label_spans_session_gap"].fillna(False).sum())
    base_rate = float((labelled["label"].to_numpy(dtype="float64") == float(DIRECTION_UP)).mean())

    return CellModels(
        symbol=symbol,
        window=window.name,
        gap_bars=TRAIN_TEST_GAP_BARS,
        training_rows=len(training.frame),
        labelled_rows=len(labelled),
        training_first_bar=training.frame["feature_timestamp"].iloc[0].isoformat(),
        training_last_bar=training.frame["feature_timestamp"].iloc[-1].isoformat(),
        scoring_first_bar=frame["timestamp"].iloc[first_scored].isoformat(),
        label_base_rate=base_rate,
        labels_spanning_session_gap=spanning,
        spanning_fraction=float(spanning / len(labelled)) if len(labelled) else 0.0,
        selected_family=selected.family,
        selection_reason=reason,
        beat_baseline=selected.family != FAMILY_CLASS_FREQUENCY,
        baseline_log_loss=float(baseline.mean_log_loss),
        selected_log_loss=float(by_family[selected.family].mean_log_loss),
        comparison=comparison,
        selected_artifact=trained_by_family[selected.family].artifact,
        null_artifact=_null_artifact(training, model_version=f"{version_stem}-raw-null"),
        shadow_artifacts={
            family: trained.artifact
            for family, trained in trained_by_family.items()
            if family != selected.family
        },
        calibration_audits=audits,
    )


def assert_gap_respected(
    *,
    symbol: str,
    window: str,
    training_last_bar: str,
    scoring_first_bar: str,
    gap_bars: int,
    frame: pd.DataFrame,
) -> tuple[str, ...]:
    """Confirm stored instants respect the train->score gap.

    Compares stored timestamps rather than the row arithmetic that produced
    them, so an off-by-one in ``train_cell`` surfaces here instead of being
    reproduced.
    """
    problems: list[str] = []
    timestamps = pd.DatetimeIndex(frame["timestamp"])
    last_train = pd.Timestamp(training_last_bar)
    first_score = pd.Timestamp(scoring_first_bar)
    if last_train >= first_score:
        problems.append(
            f"{symbol}/{window}: last training bar {last_train.isoformat()} is not before "
            f"the first scored bar."
        )
        return tuple(problems)
    gap = int(
        timestamps.searchsorted(first_score, side="left")
        - timestamps.searchsorted(last_train, side="left")
    )
    if gap < gap_bars:
        problems.append(
            f"{symbol}/{window}: only {gap} bars separate training from scoring; "
            f"{gap_bars} were required."
        )
    return tuple(problems)


__all__ = [
    "MIN_TRAINING_ROWS",
    "TRAIN_TEST_GAP_BARS",
    "TRAINED_AT",
    "CellError",
    "CellModels",
    "assert_gap_respected",
    "train_cell",
    "training_frame_for",
]
