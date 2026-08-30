"""Out-of-sample evaluation: what each cell's models actually did on their window.

The inner walk-forward decides which model a window is served by; this module
asks the separate question the design calls confirmation - did that choice
generalize to the window itself? The ground-truth label for a scored bar is a
fact of the market and may be read from the future *of the data*; the models
being graded were trained strictly before the window, with the horizon-scaled
gap between them, so nothing here feeds a model information it could not have
had.

**One frame per symbol per horizon, built over the full history.** Features do
not depend on the horizon, so the row set is identical across horizons and only
the label columns differ - asserted, not assumed, by `require_aligned`. Labels
are computed on the full-frame session grid, which is what lets a window's last
bars resolve into the sessions after the window instead of being dropped.

**Cross-horizon comparability requires one row set.** A 26-bar label runs off
the end of the data 22 bars before a 4-bar label does, so each horizon's
evaluable set differs at the frame's tail. The primary metrics are therefore
computed on the COMMON subset - bars whose labels are valid at every study
horizon - and the per-horizon full sets are reported alongside. Comparing
log losses over different rows would let a horizon win by dodging the bars the
others were graded on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from autotrader.decision.probability import V4_FEATURE_COLUMNS, ProbabilityArtifact
from autotrader.equity.session import market_date
from autotrader.ml.grid import equity_grid
from autotrader.ml.labels import DIRECTION_UP
from autotrader.ml.v4 import TrainingFrame, build_training_frame, evaluate_probabilities
from studies.equity_v1_v5.calendar import SnapshotCalendar
from studies.equity_v1_v5.windows import ScoringWindow
from studies.equity_v4_horizon.horizons import STUDY_HORIZONS, label_spec_for

#: Probability quantiles reported for every prediction distribution.
DISTRIBUTION_QUANTILES: tuple[float, ...] = (0.0, 0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99, 1.0)


class EvaluationError(Exception):
    """An out-of-sample evaluation that would compare unlike things."""


def full_frame_training(
    frame: pd.DataFrame,
    calendar: SnapshotCalendar,
    *,
    symbol: str,
    horizon_bars: int,
) -> TrainingFrame:
    """Features and labels for the whole history, labelled at `horizon_bars`.

    Used only to read ground-truth outcomes and features for scored bars; no
    model is ever fitted on this frame.
    """
    first = market_date(frame["timestamp"].iloc[0].to_pydatetime())
    last = market_date(frame["timestamp"].iloc[-1].to_pydatetime())
    sessions = calendar.sessions_between(first, last)
    return build_training_frame(
        frame, grid=equity_grid(sessions), label=label_spec_for(horizon_bars)
    )


def require_aligned(frames: Mapping[int, TrainingFrame]) -> None:
    """Refuse evaluation frames whose feature rows differ across horizons.

    The features are horizon-independent by construction; if the row sets or a
    feature column diverge, something upstream changed between builds and every
    cross-horizon comparison would be comparing different bars.
    """
    horizons = sorted(frames)
    if not horizons:
        raise EvaluationError("No evaluation frames were supplied.")
    reference = frames[horizons[0]].frame
    for horizon in horizons[1:]:
        candidate = frames[horizon].frame
        if (
            len(candidate) != len(reference)
            or not (
                candidate["feature_timestamp"].to_numpy()
                == reference["feature_timestamp"].to_numpy()
            ).all()
        ):
            raise EvaluationError(
                f"Horizon {horizon} evaluation frame holds different rows than "
                f"horizon {horizons[0]}'s; features must be horizon-independent."
            )
        for column in V4_FEATURE_COLUMNS[:1]:
            if not np.allclose(
                candidate[column].to_numpy(dtype="float64"),
                reference[column].to_numpy(dtype="float64"),
                equal_nan=True,
            ):
                raise EvaluationError(
                    f"Feature {column!r} differs between horizon {horizon} and "
                    f"horizon {horizons[0]} evaluation frames."
                )


def common_valid_timestamps(frames: Mapping[int, TrainingFrame]) -> pd.DatetimeIndex:
    """Bars whose labels are valid at every study horizon."""
    require_aligned(frames)
    mask: np.ndarray | None = None
    reference = frames[sorted(frames)[0]].frame
    for training in frames.values():
        valid = training.frame["label_valid"].fillna(False).to_numpy(dtype=bool)
        mask = valid if mask is None else (mask & valid)
    assert mask is not None
    return pd.DatetimeIndex(reference.loc[mask, "feature_timestamp"])


def window_rows(
    training: TrainingFrame,
    window: ScoringWindow,
    *,
    restrict_to: pd.DatetimeIndex | None = None,
) -> pd.DataFrame:
    """The evaluable rows of one window: labelled, and optionally intersected."""
    frame = training.frame
    days = pd.Index([market_date(ts.to_pydatetime()) for ts in frame["feature_timestamp"]])
    inside = np.asarray((days >= window.start) & (days <= window.end), dtype=bool)
    valid = frame["label_valid"].fillna(False).to_numpy(dtype=bool)
    keep = inside & valid
    if restrict_to is not None:
        keep &= frame["feature_timestamp"].isin(restrict_to).to_numpy(dtype=bool)
    return frame.loc[keep].reset_index(drop=True)


def score_artifact(artifact: ProbabilityArtifact, rows: pd.DataFrame) -> np.ndarray:
    """Calibrated probabilities for every row, through the shipped scoring path."""
    matrix = rows.loc[:, list(V4_FEATURE_COLUMNS)].to_numpy(dtype="float64")
    return np.asarray(
        [artifact.probability_up([float(value) for value in row]) for row in matrix],
        dtype="float64",
    )


def outcomes_of(rows: pd.DataFrame) -> np.ndarray:
    """The binary ground truth of evaluable rows."""
    return (rows["label"].to_numpy(dtype="float64") == float(DIRECTION_UP)).astype("float64")


def distribution_of(probabilities: np.ndarray) -> dict[str, object]:
    """The prediction-distribution summary the design asks for."""
    if probabilities.size == 0:
        return {"rows": 0}
    quantiles = np.quantile(probabilities, DISTRIBUTION_QUANTILES)
    return {
        "rows": int(probabilities.size),
        "quantiles": {
            f"q{int(level * 100):02d}": float(value)
            for level, value in zip(DISTRIBUTION_QUANTILES, quantiles, strict=True)
        },
        "distinct_values": int(np.unique(probabilities).size),
        "n_extreme_high": int((probabilities >= 0.99).sum()),
        "n_extreme_low": int((probabilities <= 0.01).sum()),
        "mean": float(probabilities.mean()),
    }


def evaluate_models(
    rows: pd.DataFrame,
    artifacts: Mapping[str, ProbabilityArtifact],
) -> dict[str, object]:
    """Every artifact's out-of-sample record on one set of evaluable rows.

    The null is scored through the same path as everything else, so the
    comparison differs by the model alone. Returns per-model metrics plus each
    model's log-loss gain over the null on these rows.
    """
    if "null" not in artifacts:
        raise EvaluationError("evaluate_models needs the raw null under the key 'null'.")
    outcomes = outcomes_of(rows)
    results: dict[str, object] = {"rows": int(len(rows))}
    if len(rows) == 0:
        return results
    scores = {name: score_artifact(artifact, rows) for name, artifact in artifacts.items()}
    null_metrics = evaluate_probabilities(scores["null"], outcomes)
    per_model: dict[str, object] = {}
    for name, probabilities in scores.items():
        metrics = evaluate_probabilities(probabilities, outcomes)
        per_model[name] = {
            "metrics": metrics,
            "log_loss_gain_vs_null": float(null_metrics["log_loss"] - metrics["log_loss"]),
            "distribution": distribution_of(probabilities),
        }
    results["models"] = per_model
    results["outcome_base_rate"] = float(outcomes.mean())
    return results


def spanning_fraction(rows: pd.DataFrame) -> float:
    """The measured fraction of evaluable rows whose label crosses a session gap."""
    if len(rows) == 0:
        return 0.0
    return float(rows["label_spans_session_gap"].fillna(False).to_numpy(dtype=bool).mean())


def evaluation_frames(
    frame: pd.DataFrame,
    calendar: SnapshotCalendar,
    *,
    symbol: str,
    horizons: Sequence[int] = STUDY_HORIZONS,
) -> dict[int, TrainingFrame]:
    """One full-history evaluation frame per horizon, alignment-checked."""
    frames = {
        horizon: full_frame_training(frame, calendar, symbol=symbol, horizon_bars=horizon)
        for horizon in horizons
    }
    require_aligned(frames)
    return frames


__all__ = [
    "DISTRIBUTION_QUANTILES",
    "EvaluationError",
    "common_valid_timestamps",
    "distribution_of",
    "evaluate_models",
    "evaluation_frames",
    "full_frame_training",
    "outcomes_of",
    "require_aligned",
    "score_artifact",
    "spanning_fraction",
    "window_rows",
]
