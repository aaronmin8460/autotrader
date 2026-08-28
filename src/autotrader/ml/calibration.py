"""M1: probability calibration interfaces, and the metrics that judge them.

A model that outputs 0.7 should be right about seventy per cent of the time it
says so. Most classifiers are not: they are ranked well and scaled badly, and a
raw score used as a probability makes every downstream decision - position
size, a confidence gate, an expected-value calculation - wrong in a direction
nobody notices, because the ranking still looks fine.

So the probability contract in `autotrader.ml.model` carries a *calibrated*
confidence, and this module is what produces one.

**Two implementations, one interface.** `IdentityCalibrator` passes scores
through unchanged and is the honest default: it says "these are raw scores" out
loud instead of implying a calibration that was never fitted.
`BinnedCalibrator` is real, dependency-free reliability binning - sort scores
into bins, replace each with the observed frequency in its bin. Isotonic and
Platt scaling are the natural upgrades and both want a dependency this project
does not yet carry; the `Calibrator` protocol is the seam they slot into
without anything else changing.

**A calibrator is fitted on validation data, never on test data and never on
training data.** Training scores are optimistic - the model has already seen
those outcomes - so a calibrator fitted on them learns to correct a bias that
will not be there in live use. Fitting on test data is straightforward leakage.
This module cannot enforce which split it is handed; `autotrader.ml.splits`
produces the right one and the experiment record says which was used.

**Binning is not monotone, on purpose.** A bin with more positives than the bin
above it stays that way, because that is what the data said. Isotonic
regression would smooth it, and smoothing is a modelling decision that belongs
to whoever chooses the calibrator rather than to the calibrator's storage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd

from autotrader.ml import MLError

#: How many equal-width bins a reliability diagram uses by default.
#:
#: Ten is the convention and it is a real trade-off: more bins resolve the
#: calibration curve more finely and hold fewer samples each, so the observed
#: frequency in every bin gets noisier. A calibrator fitted on a short
#: validation window should use fewer, not more.
DEFAULT_BIN_COUNT = 10


class CalibrationError(MLError):
    """A calibrator could not be fitted, or was asked to transform bad input."""


@runtime_checkable
class Calibrator(Protocol):
    """Maps raw model scores to probabilities that mean what they say.

    `name` identifies the method in an artifact record. `fit` learns the
    mapping from held-out scores and their outcomes; `transform` applies it.
    Both are deterministic: the same inputs must produce the same mapping, or
    a stored artifact cannot be reproduced from its experiment record.
    """

    name: str

    def fit(self, probabilities: np.ndarray, outcomes: np.ndarray) -> None:
        """Learn the mapping from held-out scores and the outcomes they predicted."""

    def transform(self, probabilities: np.ndarray) -> np.ndarray:
        """Map raw scores to calibrated probabilities."""

    def to_record(self) -> dict[str, object]:
        """The serializable description stored in an artifact's metadata."""


def _require_probabilities(values: np.ndarray, field_name: str = "probabilities") -> np.ndarray:
    """Refuse anything that is not a finite array of values in [0, 1]."""
    array = np.asarray(values, dtype="float64").ravel()
    if array.size == 0:
        raise CalibrationError(f"{field_name} is empty.")
    if not np.all(np.isfinite(array)):
        raise CalibrationError(f"{field_name} contains a non-finite value.")
    if np.any(array < 0.0) or np.any(array > 1.0):
        raise CalibrationError(
            f"{field_name} must lie in [0, 1]; got a value outside it "
            f"(min {array.min():.6g}, max {array.max():.6g})."
        )
    return array


def _require_outcomes(values: np.ndarray, expected: int) -> np.ndarray:
    """Refuse outcomes that are not a matching-length array of 0/1."""
    array = np.asarray(values).ravel()
    if array.size != expected:
        raise CalibrationError(
            f"outcomes holds {array.size} value(s) but probabilities holds {expected}."
        )
    numeric = array.astype("float64")
    if not np.all(np.isfinite(numeric)):
        raise CalibrationError("outcomes contains a non-finite value.")
    if not np.all(np.isin(numeric, (0.0, 1.0))):
        raise CalibrationError(
            "outcomes must be 0 or 1: a calibrator learns how often a score of p "
            "was followed by the event, and that question needs a binary answer."
        )
    return numeric


@dataclass
class IdentityCalibrator:
    """Passes scores through unchanged.

    The default, and not a placeholder to be embarrassed about: a model whose
    scores have not been calibrated should report them as they are. Recording
    `identity` in an artifact is a statement that no calibration was fitted,
    which is exactly the fact a reader of that artifact needs.
    """

    name: str = "identity"

    def fit(self, probabilities: np.ndarray, outcomes: np.ndarray) -> None:
        """Validate the inputs and learn nothing, which is the whole method."""
        _require_outcomes(outcomes, _require_probabilities(probabilities).size)

    def transform(self, probabilities: np.ndarray) -> np.ndarray:
        return _require_probabilities(probabilities)

    def to_record(self) -> dict[str, object]:
        return {"method": self.name}


@dataclass
class BinnedCalibrator:
    """Reliability binning: a score becomes the observed frequency of its bin.

    Fitting sorts held-out scores into `bin_count` equal-width bins over [0, 1]
    and records how often the event actually happened in each. Transforming
    looks a score up in its bin.

    An empty bin - no held-out score ever landed there - has no observed
    frequency, and this fills it with the bin's own midpoint rather than with
    the global rate. The midpoint is the identity mapping's answer, so an
    unobserved region of the score range is left as the model reported it
    instead of being pulled towards a base rate that says nothing about it.
    """

    bin_count: int = DEFAULT_BIN_COUNT
    name: str = "binned"
    frequencies: tuple[float, ...] = field(default_factory=tuple)
    sample_counts: tuple[int, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if isinstance(self.bin_count, bool) or not isinstance(self.bin_count, int):
            raise CalibrationError("bin_count must be an int.")
        if self.bin_count < 2:
            raise CalibrationError(f"bin_count must be at least 2, got {self.bin_count}.")

    @property
    def fitted(self) -> bool:
        return len(self.frequencies) == self.bin_count

    def _bin_of(self, values: np.ndarray) -> np.ndarray:
        """Which bin each score falls in. 1.0 belongs to the last bin, not past it."""
        indices = np.floor(values * self.bin_count).astype("int64")
        return np.clip(indices, 0, self.bin_count - 1)

    def fit(self, probabilities: np.ndarray, outcomes: np.ndarray) -> None:
        scores = _require_probabilities(probabilities)
        events = _require_outcomes(outcomes, scores.size)
        indices = self._bin_of(scores)
        counts = np.bincount(indices, minlength=self.bin_count).astype("int64")
        positives = np.bincount(indices, weights=events, minlength=self.bin_count)
        midpoints = (np.arange(self.bin_count) + 0.5) / self.bin_count
        with np.errstate(invalid="ignore", divide="ignore"):
            observed = np.where(counts > 0, positives / np.maximum(counts, 1), midpoints)
        self.frequencies = tuple(float(value) for value in observed)
        self.sample_counts = tuple(int(value) for value in counts)

    def transform(self, probabilities: np.ndarray) -> np.ndarray:
        if not self.fitted:
            raise CalibrationError(
                "This calibrator has not been fitted, so it has no mapping to apply. "
                "Fit it on a held-out split first."
            )
        scores = _require_probabilities(probabilities)
        return np.asarray(self.frequencies, dtype="float64")[self._bin_of(scores)]

    def to_record(self) -> dict[str, object]:
        return {
            "method": self.name,
            "bin_count": self.bin_count,
            "frequencies": list(self.frequencies),
            "sample_counts": list(self.sample_counts),
        }


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def reliability_table(
    probabilities: np.ndarray,
    outcomes: np.ndarray,
    *,
    bin_count: int = DEFAULT_BIN_COUNT,
) -> pd.DataFrame:
    """Predicted versus observed frequency, per bin.

    The table a reliability diagram is drawn from, and the thing to look at
    before trusting any confidence number: a well-calibrated model has
    `mean_predicted` close to `observed_frequency` in every populated bin.
    Empty bins are reported with a null observed frequency rather than dropped,
    because a score range the model never produced is itself worth seeing.
    """
    scores = _require_probabilities(probabilities)
    events = _require_outcomes(outcomes, scores.size)
    if isinstance(bin_count, bool) or not isinstance(bin_count, int) or bin_count < 2:
        raise CalibrationError(f"bin_count must be an int of at least 2, got {bin_count!r}.")

    indices = np.clip(np.floor(scores * bin_count).astype("int64"), 0, bin_count - 1)
    counts = np.bincount(indices, minlength=bin_count).astype("int64")
    positives = np.bincount(indices, weights=events, minlength=bin_count)
    predicted = np.bincount(indices, weights=scores, minlength=bin_count)
    populated = counts > 0
    return pd.DataFrame(
        {
            "bin_lower": np.arange(bin_count) / bin_count,
            "bin_upper": (np.arange(bin_count) + 1) / bin_count,
            "samples": counts,
            "mean_predicted": np.where(populated, predicted / np.maximum(counts, 1), np.nan),
            "observed_frequency": np.where(populated, positives / np.maximum(counts, 1), np.nan),
        }
    )


def expected_calibration_error(
    probabilities: np.ndarray,
    outcomes: np.ndarray,
    *,
    bin_count: int = DEFAULT_BIN_COUNT,
) -> float:
    """Sample-weighted mean gap between predicted and observed frequency.

    Zero means perfectly calibrated on this sample. It says nothing about
    whether the model is any good - a model that always predicts the base rate
    is perfectly calibrated and perfectly useless - so it is read next to a
    discrimination metric, never instead of one.
    """
    table = reliability_table(probabilities, outcomes, bin_count=bin_count)
    populated = table.loc[table["samples"] > 0]
    if populated.empty:
        return 0.0
    gaps = (populated["mean_predicted"] - populated["observed_frequency"]).abs()
    weights = populated["samples"].to_numpy(dtype="float64")
    return float((gaps.to_numpy(dtype="float64") * weights).sum() / weights.sum())


def brier_score(probabilities: np.ndarray, outcomes: np.ndarray) -> float:
    """Mean squared error of a probabilistic forecast.

    Lower is better, and unlike calibration error it punishes a model that is
    calibrated but uninformative, so the two are read together.
    """
    scores = _require_probabilities(probabilities)
    events = _require_outcomes(outcomes, scores.size)
    return float(np.mean((scores - events) ** 2))


__all__ = [
    "DEFAULT_BIN_COUNT",
    "BinnedCalibrator",
    "CalibrationError",
    "Calibrator",
    "IdentityCalibrator",
    "brier_score",
    "expected_calibration_error",
    "reliability_table",
]
