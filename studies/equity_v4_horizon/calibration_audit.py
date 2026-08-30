"""Where every calibrated probability comes from, counted rather than trusted.

The pilot's one non-null model scored two bars at 0.999678 - the extreme step
of an isotonic map with eleven distinct levels, fitted on a validation split
whose top step held very few rows. That is isotonic regression behaving as
isotonic regression does, and the study's design says how it must be handled:
measure the support behind every step, flag extreme values that rest on thin
bins, and refuse to let a horizon win on the strength of a handful of
near-certain predictions (design.md sections 7 and 10/P6).

The audit recomputes nothing it could misstate. The validation rows come from
the ``TemporalSplit`` the trained model itself carries; the uncalibrated scores
are produced by the shipped artifact with its calibration replaced by the
identity, through the same ``probability_up`` path the live engine uses; and
the step assignment reproduces ``IsotonicCalibration.apply``'s rule - a score
belongs to the last step whose edge it has reached, and a score below every
edge belongs to the first.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

import numpy as np
import pandas as pd

from autotrader.decision.probability import (
    V4_FEATURE_COLUMNS,
    IdentityCalibration,
    IsotonicCalibration,
    ProbabilityArtifact,
)
from autotrader.ml.labels import DIRECTION_UP
from autotrader.ml.v4 import TrainedModel, evaluate_probabilities
from studies.equity_v4_horizon.horizons import MIN_EXTREME_SUPPORT

#: A calibrated probability at or beyond these bounds is "extreme" for the
#: audit: close enough to certainty that its provenance must be shown.
EXTREME_HIGH = 0.99
EXTREME_LOW = 0.01


def step_index(thresholds: Sequence[float], score: float) -> int:
    """The isotonic step a score lands on, by the shipped ``apply`` rule."""
    position = int(np.searchsorted(np.asarray(thresholds, dtype="float64"), score, side="right"))
    return max(position - 1, 0)


def audit_calibration(trained: TrainedModel) -> dict[str, object]:
    """The calibration audit for one trained model, on its own validation rows."""
    return audit_artifact_calibration(trained.artifact, trained.split.validation.frame)


def audit_artifact_calibration(
    artifact: ProbabilityArtifact, validation: pd.DataFrame
) -> dict[str, object]:
    """Everything the design requires to be known about one model's calibration.

    Returns a JSON-ready record. For an identity calibration (nothing was
    fitted) the record says so and carries only the validation metrics; for an
    isotonic map it carries every step's fitted value and validation support,
    the distinct-level count, and the extreme-step provenance the winner rule's
    P6 criterion reads.
    """
    calibration = artifact.calibration
    matrix = validation.loc[:, list(V4_FEATURE_COLUMNS)].to_numpy(dtype="float64")
    outcomes = (validation["label"].to_numpy(dtype="float64") == float(DIRECTION_UP)).astype(
        "float64"
    )

    raw_artifact = dataclasses.replace(artifact, calibration=IdentityCalibration())
    raw_scores = np.asarray(
        [raw_artifact.probability_up([float(value) for value in row]) for row in matrix],
        dtype="float64",
    )
    calibrated_scores = np.asarray(
        [artifact.probability_up([float(value) for value in row]) for row in matrix],
        dtype="float64",
    )

    record: dict[str, object] = {
        "method": type(calibration).__name__,
        "validation_rows": int(len(validation)),
        "validation_metrics_calibrated": evaluate_probabilities(calibrated_scores, outcomes),
        "validation_metrics_uncalibrated": evaluate_probabilities(raw_scores, outcomes),
    }

    if not isinstance(calibration, IsotonicCalibration):
        record["distinct_levels"] = int(np.unique(calibrated_scores).size)
        record["extreme_from_thin_bins"] = False
        record["steps"] = []
        return record

    supports = np.zeros(len(calibration.thresholds), dtype="int64")
    for score in raw_scores:
        supports[step_index(calibration.thresholds, float(score))] += 1

    steps = [
        {
            "threshold": float(threshold),
            "value": float(value),
            "validation_support": int(support),
            "extreme": bool(value >= EXTREME_HIGH or value <= EXTREME_LOW),
        }
        for threshold, value, support in zip(
            calibration.thresholds, calibration.values, supports, strict=True
        )
    ]
    extreme_thin = [
        step
        for step in steps
        if step["extreme"] and int(step["validation_support"]) < MIN_EXTREME_SUPPORT
    ]
    record["distinct_levels"] = int(len(set(calibration.values)))
    record["steps"] = steps
    record["largest_step_support"] = int(supports.max()) if len(supports) else 0
    record["max_calibrated_value"] = float(max(calibration.values))
    record["min_calibrated_value"] = float(min(calibration.values))
    record["extreme_steps"] = [step for step in steps if step["extreme"]]
    record["extreme_from_thin_bins"] = bool(extreme_thin)
    record["min_extreme_support_required"] = MIN_EXTREME_SUPPORT
    return record


__all__ = [
    "EXTREME_HIGH",
    "EXTREME_LOW",
    "audit_artifact_calibration",
    "audit_calibration",
    "step_index",
]
