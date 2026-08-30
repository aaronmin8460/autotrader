"""V4's training plan: one model per scoring window, fitted only on that window's past.

**The rule this module exists to enforce.** A model that scores a window must
have been fitted only on information that existed before that window opened.
Training once over the whole history and scoring the same history would produce a
V4 - and therefore a V5 - that knew its own answers, and every number downstream
would be fiction. So each scoring window gets its own model, fitted on the bars
that preceded it.

**Anchored, not rolling.** Every fold trains on everything from the start of the
dataset up to its own boundary, which is the scheme `autotrader.ml.splits`
implements and the one `autotrader.ml.v4.compare_candidates` grades candidates
under. A rolling window would discard history for no stated reason.

**The gap between training and scoring is market time, not row count.** A label
at bar *t* resolves `horizon_bars` later, so training rows near the boundary
carry outcomes that fall inside the window being scored. The horizon is removed,
and then a whole regular session on top of it - `EMBARGO_BARS` - so the last
training label resolves strictly before the first scored bar. `autotrader.ml.v4`
purges and embargoes *within* each fold as well; this is the outer gap between
the training history and the scored window.

**Equity labels may cross a session gap, and that is the shipped default rather
than an oversight.** `SessionPolicy.SPAN_SESSIONS` lets a four-bar holding
period that begins near the close resolve in the next session, and flags every
such row with `label_spans_session_gap`. It is the honest choice for a
regular-hours strategy that holds positions overnight - which this one does,
because it cannot trade them out after 16:00 - and the fraction of training rows
affected is measured and reported rather than left implicit.

**Model choice is re-made every window, by the repository's own rule.**
`compare_candidates` grades every candidate against a class-frequency baseline on
anchored sub-folds of that window's training data, and `select_candidate` refuses
anything that does not beat the baseline by a material margin. The comparison is
recorded whether or not it selects a model, because "no candidate beat its null"
is the finding on a market that offered no edge, and a study that only recorded
the winners could not report it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from autotrader.decision.probability import FAMILY_CLASS_FREQUENCY, ProbabilityArtifact
from autotrader.equity.session import market_date
from autotrader.ml.grid import equity_grid
from autotrader.ml.v4 import (
    DEFAULT_HORIZON_BARS,
    MATERIAL_LOG_LOSS_IMPROVEMENT,
    TrainingFrame,
    build_training_frame,
    compare_candidates,
    default_candidates,
    default_label_spec,
    select_candidate,
    train_model,
)
from studies.equity_v1_v5.calendar import SnapshotCalendar
from studies.equity_v1_v5.windows import EMBARGO_BARS, ScoringWindow

#: Bars removed between the last training bar and the first scored bar.
TRAIN_TEST_GAP_BARS = DEFAULT_HORIZON_BARS + EMBARGO_BARS

#: The smallest training frame worth fitting on. `train_model` splits three
#: ways and `compare_candidates` cuts four folds out of that, so a frame much
#: smaller than this leaves parts with no rows and fails with a less useful
#: message than this one.
MIN_TRAINING_ROWS = 1500


class WalkForwardError(Exception):
    """A window's model could not be trained on information available to it."""


@dataclass(frozen=True)
class WindowModel:
    """The model one scoring window is served by, and the evidence for choosing it."""

    window: str
    symbol: str
    training_rows: int
    training_first_bar: str
    training_last_bar: str
    scoring_first_bar: str
    gap_bars: int
    labels_spanning_session_gap: int
    selected_family: str
    selection_reason: str
    beat_baseline: bool
    baseline_log_loss: float | None
    selected_log_loss: float | None
    model_version: str
    calibrated: bool
    artifact: ProbabilityArtifact = field(repr=False)

    def to_json_dict(self) -> dict[str, object]:
        return {
            "window": self.window,
            "symbol": self.symbol,
            "training_rows": self.training_rows,
            "training_first_bar_utc": self.training_first_bar,
            "training_last_bar_utc": self.training_last_bar,
            "scoring_first_bar_utc": self.scoring_first_bar,
            "gap_bars": self.gap_bars,
            "labels_spanning_session_gap": self.labels_spanning_session_gap,
            "selected_family": self.selected_family,
            "selection_reason": self.selection_reason,
            "beat_baseline": self.beat_baseline,
            "baseline_log_loss": self.baseline_log_loss,
            "selected_log_loss": self.selected_log_loss,
            "model_version": self.model_version,
            "calibrated": self.calibrated,
            "material_improvement_threshold": MATERIAL_LOG_LOSS_IMPROVEMENT,
        }


def training_frame_for(
    frame: pd.DataFrame,
    calendar: SnapshotCalendar,
    *,
    symbol: str,
    last_row: int,
) -> TrainingFrame:
    """Build V4's training rows from `frame[:last_row]` and nothing later.

    The slice is taken *before* the grid is built, so the grid itself spans only
    the sessions the training data covers and cannot describe a bar the model is
    not allowed to know about.
    """
    history = frame.iloc[: last_row + 1].reset_index(drop=True)
    if history.empty:
        raise WalkForwardError(f"{symbol} has no history before row {last_row}.")
    first = market_date(history["timestamp"].iloc[0].to_pydatetime())
    last = market_date(history["timestamp"].iloc[-1].to_pydatetime())
    sessions = calendar.sessions_between(first, last)
    if not sessions:
        raise WalkForwardError(f"{symbol}: the calendar reports no session in {first}..{last}.")
    return build_training_frame(history, grid=equity_grid(sessions), label=default_label_spec())


def train_for_window(
    frame: pd.DataFrame,
    calendar: SnapshotCalendar,
    window: ScoringWindow,
    *,
    symbol: str,
    seed: int = 0,
    trained_at: datetime | None = None,
) -> WindowModel:
    """Fit the model that will score `window`, on that window's past alone."""
    first_scored, _ = window.positions(frame)
    last_training_row = first_scored - TRAIN_TEST_GAP_BARS - 1
    if last_training_row < 0:
        raise WalkForwardError(
            f"{symbol}/{window.name}: the window opens at row {first_scored}, which leaves "
            f"no room for a {TRAIN_TEST_GAP_BARS}-bar gap before any training data."
        )

    training = training_frame_for(frame, calendar, symbol=symbol, last_row=last_training_row)
    if len(training.frame) < MIN_TRAINING_ROWS:
        raise WalkForwardError(
            f"{symbol}/{window.name}: only {len(training.frame)} training rows are available "
            f"before the window, and a three-way split plus four folds needs at least "
            f"{MIN_TRAINING_ROWS}."
        )

    comparison = compare_candidates(
        training,
        candidates=default_candidates(),
        embargo_bars=EMBARGO_BARS,
        seed=seed,
    )
    candidate, reason = select_candidate(comparison.results)
    trained = train_model(
        training,
        candidate,
        model_version=f"equity-pilot-{symbol.lower()}-{window.name}",
        seed=seed,
        trained_at=trained_at,
        notes=(
            f"SPY/QQQ equity historical pilot. Fitted on rows up to "
            f"{training.frame['feature_timestamp'].iloc[-1].isoformat()}, "
            f"{TRAIN_TEST_GAP_BARS} bars before the {window.name} window opens."
        ),
    )

    by_family = {result.candidate.family: result for result in comparison.results}
    baseline = by_family.get(FAMILY_CLASS_FREQUENCY)
    selected = by_family.get(candidate.family)
    spanning = (
        int(training.frame["label_spans_session_gap"].sum())
        if "label_spans_session_gap" in training.frame.columns
        else 0
    )
    return WindowModel(
        window=window.name,
        symbol=symbol,
        training_rows=len(training.frame),
        training_first_bar=training.frame["feature_timestamp"].iloc[0].isoformat(),
        training_last_bar=training.frame["feature_timestamp"].iloc[-1].isoformat(),
        scoring_first_bar=frame["timestamp"].iloc[first_scored].isoformat(),
        gap_bars=TRAIN_TEST_GAP_BARS,
        labels_spanning_session_gap=spanning,
        selected_family=candidate.family,
        selection_reason=reason,
        beat_baseline=candidate.family != FAMILY_CLASS_FREQUENCY,
        baseline_log_loss=None if baseline is None else float(baseline.mean_log_loss),
        selected_log_loss=None if selected is None else float(selected.mean_log_loss),
        model_version=trained.artifact.model_version,
        calibrated=bool(trained.artifact.calibrated),
        artifact=trained.artifact,
    )


def assert_no_forward_information(
    models: Sequence[WindowModel],
    frame: pd.DataFrame,
) -> tuple[str, ...]:
    """Confirm every model's last training bar precedes its window by the full gap.

    The check that the plan above was actually carried out. Compares stored
    instants rather than the row arithmetic that produced them, so an off-by-one
    in `train_for_window` would surface here instead of being reproduced.
    """
    problems: list[str] = []
    timestamps = pd.DatetimeIndex(frame["timestamp"])
    for model in models:
        last_train = pd.Timestamp(model.training_last_bar)
        first_score = pd.Timestamp(model.scoring_first_bar)
        if last_train >= first_score:
            problems.append(
                f"{model.symbol}/{model.window}: last training bar {last_train.isoformat()} "
                f"is not before the first scored bar {first_score.isoformat()}."
            )
            continue
        gap = int(
            timestamps.searchsorted(first_score, side="left")
            - timestamps.searchsorted(last_train, side="left")
        )
        if gap < model.gap_bars:
            problems.append(
                f"{model.symbol}/{model.window}: only {gap} bars separate the last training "
                f"bar from the first scored bar; {model.gap_bars} were required."
            )
    return tuple(problems)


def describe_plan(models: Sequence[WindowModel]) -> Mapping[str, object]:
    """The whole training plan as serializable values, for the audit record."""
    return {
        "horizon_bars": DEFAULT_HORIZON_BARS,
        "embargo_bars": EMBARGO_BARS,
        "train_test_gap_bars": TRAIN_TEST_GAP_BARS,
        "scheme": "anchored walk-forward, one model per scoring window",
        "models": [model.to_json_dict() for model in models],
    }


__all__ = [
    "MIN_TRAINING_ROWS",
    "TRAIN_TEST_GAP_BARS",
    "WalkForwardError",
    "WindowModel",
    "assert_no_forward_information",
    "describe_plan",
    "train_for_window",
    "training_frame_for",
]
