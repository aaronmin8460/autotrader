"""Where every calibrated probability comes from, counted rather than trusted.

Carried over from the horizon study, which generalized the pilot's single
finding into a measured property of the stack: 77% of its fitted models carried
an extreme calibrated step (>=0.99 or <=0.01) supported by fewer than 30
validation rows, and the worst step rested on one row in 3,103. This study
audits every model it trains the same way, because a near-certain probability
from a thin isotonic bin is exactly what a production risk layer would size
against.

The audit recomputes nothing it could misstate: validation rows come from the
``TemporalSplit`` the trained model itself carries, uncalibrated scores are
produced by the shipped artifact with its calibration replaced by the identity
through the same ``probability_up`` path the live engine uses, and the step
assignment reproduces ``IsotonicCalibration.apply``'s rule.
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

#: A calibrated probability at or beyond these bounds is "extreme": close
#: enough to certainty that its provenance must be shown.
EXTREME_HIGH = 0.99
EXTREME_LOW = 0.01

#: How many validation rows an extreme step must hold before its value is
#: considered supported rather than a thin-bin artifact. The horizon study's
#: predeclared figure, kept.
MIN_EXTREME_SUPPORT = 30


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
    """Everything the study records about one model's calibration provenance."""
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
    "MIN_EXTREME_SUPPORT",
    "audit_artifact_calibration",
    "audit_calibration",
    "step_index",
]
