"""Out-of-sample V4 evaluation: what each cell's models actually did on their window.

The inner walk-forward decides which model a window is served by; this module
asks whether that choice generalized. The ground-truth label for a scored bar
is a fact of the market and may be read from the future *of the data*; the
models being graded were trained strictly before the window with the 30-bar
gap, so nothing here feeds a model information it could not have had.

Carried over from the horizon study at the single frozen horizon: the selected
model, the raw null, and both shadows are all scored through the shipped
``probability_up`` path on the same rows, so the comparison differs by the
model alone.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from autotrader.decision.probability import V4_FEATURE_COLUMNS, ProbabilityArtifact
from autotrader.equity.session import market_date
from autotrader.ml.grid import equity_grid
from autotrader.ml.labels import DIRECTION_UP
from autotrader.ml.v4 import (
    TrainingFrame,
    build_training_frame,
    default_label_spec,
    evaluate_probabilities,
)
from studies.equity_v1_v5.calendar import SnapshotCalendar
from studies.equity_v1_v5.windows import ScoringWindow

#: Probability quantiles reported for every prediction distribution.
DISTRIBUTION_QUANTILES: tuple[float, ...] = (0.0, 0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99, 1.0)


class EvaluationError(Exception):
    """An out-of-sample evaluation that would compare unlike things."""


def full_frame_training(
    frame: pd.DataFrame,
    calendar: SnapshotCalendar,
) -> TrainingFrame:
    """Features and ground-truth labels for the whole history.

    Used only to read outcomes and features for scored bars; no model is ever
    fitted on this frame.
    """
    first = market_date(frame["timestamp"].iloc[0].to_pydatetime())
    last = market_date(frame["timestamp"].iloc[-1].to_pydatetime())
    sessions = calendar.sessions_between(first, last)
    return build_training_frame(frame, grid=equity_grid(sessions), label=default_label_spec())


def window_rows(training: TrainingFrame, window: ScoringWindow) -> pd.DataFrame:
    """The evaluable rows of one window: inside it, and carrying a valid label."""
    frame = training.frame
    days = pd.Index([market_date(ts.to_pydatetime()) for ts in frame["feature_timestamp"]])
    inside = np.asarray((days >= window.start) & (days <= window.end), dtype=bool)
    valid = frame["label_valid"].fillna(False).to_numpy(dtype=bool)
    return frame.loc[inside & valid].reset_index(drop=True)


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
    artifacts: dict[str, ProbabilityArtifact],
) -> dict[str, object]:
    """Every artifact's out-of-sample record on one set of evaluable rows."""
    if "null" not in artifacts:
        raise EvaluationError("evaluate_models needs the raw null under the key 'null'.")
    results: dict[str, object] = {"rows": int(len(rows))}
    if len(rows) == 0:
        return results
    outcomes = outcomes_of(rows)
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
    results["spanning_fraction"] = float(
        rows["label_spans_session_gap"].fillna(False).to_numpy(dtype=bool).mean()
    )
    return results


__all__ = [
    "DISTRIBUTION_QUANTILES",
    "EvaluationError",
    "distribution_of",
    "evaluate_models",
    "full_frame_training",
    "outcomes_of",
    "score_artifact",
    "window_rows",
]
