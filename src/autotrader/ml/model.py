"""M1: the probability-prediction contract a future V4 will expose.

This is the shape of the thing V4 hands to whatever decides. It is defined now,
before any model exists, because the contract is the part that has to be right:
a probability whose meaning is not pinned down is a number that will be
compared against a threshold by someone who did not write it.

One prediction carries, for one symbol at one bar:

    model_version            which trained model produced it
    artifact_version         which immutable artifact that model was stored as
    feature_schema_version   which column contract its inputs satisfied
    label_spec_id            which target definition it was trained against
    symbol / asset_class     what it is about
    timestamp                the feature bar, an interval start
    knowable_at              when this prediction could first have been made
    probability_down/up      the class probabilities
    probability_neutral      the third class, or None for a binary target
    calibrated_confidence    the calibrated probability of the predicted class

**Training is not coupled to execution.** Nothing in this module - or anywhere
in `autotrader.ml` - imports the execution boundary, the risk engine,
reconciliation, or a runtime. A `Prediction` is a value: it has no method that
sizes a position, no method that submits anything, and no reference to an
account. Turning one into an order is a decision the risk engine and the
execution boundary already own, and this package deliberately cannot reach
them. A test asserts the import graph in both directions.

**`knowable_at` is what makes a prediction auditable.** It is the feature bar's
close, so a backtest that consumes predictions can refuse any that would have
arrived from the future, and a live system can be checked against the same
rule it was evaluated under.

**Determinism is part of the interface.** `fit` takes an explicit `seed` and is
required to be a pure function of its features, its labels, that seed, and the
model's own hyperparameters. Two runs of the same experiment record must
produce the same artifact; a model that reads a clock, a process id, or an
unseeded global generator cannot be reproduced and does not satisfy this
protocol.

**The reference implementation is a null baseline, not a trading model.**
`ClassFrequencyModel` predicts the training set's class frequencies and ignores
its features entirely. It exists to exercise the contract end to end without
adding a dependency, and to be the floor any real candidate has to clear: a
model that cannot beat the base rate has found nothing. Choosing V4's actual
model is an evidence-driven decision that walk-forward results have not yet
been produced for.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd

from autotrader.ml import AssetClass, MLError
from autotrader.ml.calibration import Calibrator, IdentityCalibrator
from autotrader.ml.labels import (
    DIRECTION_DOWN,
    DIRECTION_UP,
    TERNARY_BUY,
    TERNARY_HOLD,
    TERNARY_SELL,
    LabelKind,
)
from autotrader.ml.storage import sha256_of_record
from autotrader.runtime.schedule import BAR_INTERVAL, require_utc

#: The version of the prediction contract itself. Distinct from any model's
#: version: this changes when the *shape* of a prediction changes, which
#: invalidates every consumer rather than one artifact.
MODEL_CONTRACT_VERSION = "1.0.0"

#: The probability columns a model's `predict_proba` returns, in class order.
#: A binary model returns the two ends and omits the middle.
PROBABILITY_DOWN = "probability_down"
PROBABILITY_NEUTRAL = "probability_neutral"
PROBABILITY_UP = "probability_up"
PROBABILITY_COLUMNS: tuple[str, ...] = (PROBABILITY_DOWN, PROBABILITY_NEUTRAL, PROBABILITY_UP)
BINARY_PROBABILITY_COLUMNS: tuple[str, ...] = (PROBABILITY_DOWN, PROBABILITY_UP)

#: How far the class probabilities may sum from one before a prediction is
#: refused. Floating-point addition of three numbers is not exact; a genuine
#: normalization bug is orders of magnitude larger than this.
PROBABILITY_SUM_TOLERANCE = 1e-6


class ModelError(MLError):
    """A prediction that violates the contract, or a model that cannot satisfy it."""


@dataclass(frozen=True)
class Prediction:
    """One model output, for one symbol at one bar.

    Validated on construction: the probabilities must be finite, in [0, 1], and
    sum to one; the timestamps must be UTC-aware and consistent; the calibrated
    confidence must itself be a probability. An invalid prediction cannot exist,
    so nothing downstream has to check one.
    """

    model_version: str
    artifact_version: str
    feature_schema_version: str
    label_spec_id: str
    symbol: str
    asset_class: AssetClass
    timestamp: datetime
    knowable_at: datetime
    probability_down: float
    probability_up: float
    calibrated_confidence: float
    probability_neutral: float | None = None

    def __post_init__(self) -> None:
        for field in (
            "model_version",
            "artifact_version",
            "feature_schema_version",
            "label_spec_id",
            "symbol",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ModelError(f"{field} must be a non-empty string, got {value!r}.")
        if not isinstance(self.asset_class, AssetClass):
            raise ModelError(f"asset_class must be an AssetClass, got {self.asset_class!r}.")
        object.__setattr__(self, "timestamp", require_utc(self.timestamp, "timestamp"))
        object.__setattr__(self, "knowable_at", require_utc(self.knowable_at, "knowable_at"))
        if self.knowable_at != self.timestamp + BAR_INTERVAL:
            raise ModelError(
                f"knowable_at must be one bar interval after timestamp: "
                f"{self.timestamp.isoformat()} + {BAR_INTERVAL} is "
                f"{(self.timestamp + BAR_INTERVAL).isoformat()}, not "
                f"{self.knowable_at.isoformat()}. A prediction is knowable when its "
                "feature bar closes, and not before."
            )
        probabilities = [self.probability_down, self.probability_up]
        if self.probability_neutral is not None:
            probabilities.append(self.probability_neutral)
        for value in (*probabilities, self.calibrated_confidence):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ModelError(f"A probability must be a number, got {type(value).__name__}.")
            if not np.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
                raise ModelError(f"A probability must be finite and in [0, 1], got {value!r}.")
        total = float(sum(probabilities))
        if abs(total - 1.0) > PROBABILITY_SUM_TOLERANCE:
            raise ModelError(
                f"Class probabilities must sum to 1 within {PROBABILITY_SUM_TOLERANCE}, "
                f"got {total!r}. A set that does not is not a distribution, and a "
                "confidence read off it means nothing."
            )

    @property
    def is_ternary(self) -> bool:
        """Whether this prediction carries a neutral class."""
        return self.probability_neutral is not None

    @property
    def probabilities(self) -> dict[str, float]:
        """The class probabilities, keyed by column name, neutral omitted if absent."""
        values = {
            PROBABILITY_DOWN: float(self.probability_down),
            PROBABILITY_UP: float(self.probability_up),
        }
        if self.probability_neutral is not None:
            values[PROBABILITY_NEUTRAL] = float(self.probability_neutral)
        return values

    @property
    def predicted_class(self) -> str:
        """The most likely class: DOWN, HOLD, or UP.

        Ties break towards HOLD when there is one and towards DOWN otherwise -
        deliberately away from acting. A prediction that cannot separate its
        classes is not evidence for taking a position.
        """
        neutral = 0.0 if self.probability_neutral is None else float(self.probability_neutral)
        best = max(float(self.probability_down), neutral, float(self.probability_up))
        if self.probability_neutral is not None and neutral == best:
            return "HOLD"
        if float(self.probability_down) == best:
            return "DOWN"
        return "UP"

    def to_record(self) -> dict[str, object]:
        """The serializable form. Contains no account, order, or credential field."""
        return {
            "model_contract_version": MODEL_CONTRACT_VERSION,
            "model_version": self.model_version,
            "artifact_version": self.artifact_version,
            "feature_schema_version": self.feature_schema_version,
            "label_spec_id": self.label_spec_id,
            "symbol": self.symbol,
            "asset_class": self.asset_class.value,
            "timestamp_utc": self.timestamp.isoformat(),
            "knowable_at_utc": self.knowable_at.isoformat(),
            "probability_down": float(self.probability_down),
            "probability_neutral": (
                None if self.probability_neutral is None else float(self.probability_neutral)
            ),
            "probability_up": float(self.probability_up),
            "calibrated_confidence": float(self.calibrated_confidence),
            "predicted_class": self.predicted_class,
        }


@runtime_checkable
class ProbabilityModel(Protocol):
    """What a V4 candidate model has to be.

    Deliberately small. A model fits on a feature frame and a label series, and
    produces class probabilities for a feature frame. It does not read a
    dataset from disk, does not know where its artifact lives, and cannot reach
    a broker.

    `fit` must be deterministic given `(features, labels, seed)` and the
    model's own hyperparameters, so that an experiment record is enough to
    reproduce an artifact byte for byte.
    """

    model_name: str
    model_version: str

    def fit(self, features: pd.DataFrame, labels: pd.Series, *, seed: int) -> None:
        """Fit on the training split. Deterministic given the seed."""

    def predict_proba(self, features: pd.DataFrame) -> pd.DataFrame:
        """Class probabilities per row, as `PROBABILITY_COLUMNS` (neutral optional)."""

    def hyperparameters(self) -> dict[str, object]:
        """The settings that, with the seed and the data, determine the fit."""


@dataclass
class ClassFrequencyModel:
    """The null baseline: predict the training class frequencies, ignore the features.

    Not a trading model and not a starting point for one. It is here so the
    contract above can be exercised end to end without a machine-learning
    dependency, and so every future candidate has an honest floor to be
    measured against - a model that does not beat the base rate has found
    nothing, however good its accuracy looks on an imbalanced target.

    Deterministic by construction: it contains no randomness at all, so its
    `seed` is accepted, recorded, and unused.
    """

    label_kind: LabelKind
    model_name: str = "class-frequency-baseline"
    model_version: str = "1.0.0"
    seed: int = 0
    _frequencies: dict[int, float] | None = None

    @property
    def fitted(self) -> bool:
        return self._frequencies is not None

    @property
    def class_values(self) -> tuple[int, ...]:
        """The class values this model's label kind uses, in probability order."""
        if self.label_kind is LabelKind.TERNARY:
            return (TERNARY_SELL, TERNARY_HOLD, TERNARY_BUY)
        if self.label_kind is LabelKind.DIRECTION:
            return (DIRECTION_DOWN, DIRECTION_UP)
        raise ModelError(
            "A continuous forward-return label has no classes. This baseline "
            "predicts class probabilities, so it needs a direction or ternary target."
        )

    def hyperparameters(self) -> dict[str, object]:
        return {"label_kind": self.label_kind.value}

    def fit(self, features: pd.DataFrame, labels: pd.Series, *, seed: int = 0) -> None:
        """Count each class's share of the training labels."""
        values = self.class_values
        observed = pd.Series(labels).dropna().astype("int64")
        if observed.empty:
            raise ModelError("Cannot fit on an empty label series.")
        unknown = sorted(set(observed.unique()) - set(values))
        if unknown:
            raise ModelError(
                f"Labels contain value(s) outside this label kind's classes: {unknown}."
            )
        total = float(len(observed))
        self.seed = int(seed)
        self._frequencies = {value: float((observed == value).sum()) / total for value in values}

    def predict_proba(self, features: pd.DataFrame) -> pd.DataFrame:
        """The same fitted frequencies on every row. The features are not read."""
        if self._frequencies is None:
            raise ModelError("This model has not been fitted.")
        rows = len(features)
        if self.label_kind is LabelKind.TERNARY:
            columns = {
                PROBABILITY_DOWN: self._frequencies[TERNARY_SELL],
                PROBABILITY_NEUTRAL: self._frequencies[TERNARY_HOLD],
                PROBABILITY_UP: self._frequencies[TERNARY_BUY],
            }
        else:
            columns = {
                PROBABILITY_DOWN: self._frequencies[DIRECTION_DOWN],
                PROBABILITY_UP: self._frequencies[DIRECTION_UP],
            }
        return pd.DataFrame(
            {name: np.full(rows, value, dtype="float64") for name, value in columns.items()},
            index=pd.RangeIndex(rows),
        )


@dataclass(frozen=True)
class PredictionContext:
    """The identity a batch of predictions is stamped with.

    Everything that is true of every row: which model, which artifact, which
    schema, which target. Carried as one value so a caller cannot build half a
    batch under one model version and half under another.
    """

    model_version: str
    artifact_version: str
    feature_schema_version: str
    label_spec_id: str

    def to_record(self) -> dict[str, object]:
        return {
            "model_version": self.model_version,
            "artifact_version": self.artifact_version,
            "feature_schema_version": self.feature_schema_version,
            "label_spec_id": self.label_spec_id,
        }

    @property
    def fingerprint(self) -> str:
        return sha256_of_record(self.to_record())


def _normalize(values: np.ndarray) -> np.ndarray:
    """Rescale each row to sum to one, refusing a row that cannot be rescaled."""
    totals = values.sum(axis=1)
    if np.any(~np.isfinite(totals)) or np.any(totals <= 0.0):
        raise ModelError(
            "A row of class probabilities does not sum to a positive, finite total "
            "and cannot be normalized into a distribution."
        )
    return values / totals[:, None]


def build_predictions(
    rows: pd.DataFrame,
    probabilities: pd.DataFrame,
    *,
    context: PredictionContext,
    calibrator: Calibrator | None = None,
) -> tuple[Prediction, ...]:
    """Turn model output into validated `Prediction` values.

    `rows` is a slice of a built dataset - it supplies `symbol`, `asset_class`,
    `feature_timestamp` and `knowable_at`, so a prediction inherits the exact
    identity of the feature row it came from rather than being stamped by the
    caller. `probabilities` is what `predict_proba` returned, aligned
    positionally.

    The calibrator, if supplied, is applied to the predicted class's
    probability to produce `calibrated_confidence`; without one, that
    confidence *is* the raw maximum and `IdentityCalibrator` records the fact.
    Probabilities are renormalized so a distribution that drifted by a
    floating-point epsilon is repaired, and one that is genuinely broken is
    refused.
    """
    if len(rows) != len(probabilities):
        raise ModelError(f"{len(rows)} feature row(s) but {len(probabilities)} probability row(s).")
    for column in ("symbol", "asset_class", "feature_timestamp", "knowable_at"):
        if column not in rows.columns:
            raise ModelError(f"Feature rows are missing the {column!r} column.")

    present = [name for name in PROBABILITY_COLUMNS if name in probabilities.columns]
    if present not in (list(PROBABILITY_COLUMNS), list(BINARY_PROBABILITY_COLUMNS)):
        raise ModelError(
            f"Probabilities must hold either {', '.join(BINARY_PROBABILITY_COLUMNS)} or "
            f"{', '.join(PROBABILITY_COLUMNS)}; got {', '.join(probabilities.columns)}."
        )
    ternary = PROBABILITY_NEUTRAL in present
    matrix = _normalize(probabilities.loc[:, present].to_numpy(dtype="float64"))
    confidences = matrix.max(axis=1)
    applied = (calibrator if calibrator is not None else IdentityCalibrator()).transform(
        confidences
    )

    features = rows.reset_index(drop=True)
    built: list[Prediction] = []
    for position in range(len(features)):
        values = matrix[position]
        built.append(
            Prediction(
                model_version=context.model_version,
                artifact_version=context.artifact_version,
                feature_schema_version=context.feature_schema_version,
                label_spec_id=context.label_spec_id,
                symbol=str(features["symbol"].iloc[position]),
                asset_class=AssetClass(str(features["asset_class"].iloc[position])),
                timestamp=features["feature_timestamp"].iloc[position].to_pydatetime(),
                knowable_at=features["knowable_at"].iloc[position].to_pydatetime(),
                probability_down=float(values[0]),
                probability_neutral=float(values[1]) if ternary else None,
                probability_up=float(values[-1]),
                calibrated_confidence=float(applied[position]),
            )
        )
    return tuple(built)


def predictions_to_records(predictions: Sequence[Prediction]) -> list[dict[str, object]]:
    """The serializable form of a batch, for a report or an evaluation artifact."""
    return [prediction.to_record() for prediction in predictions]


__all__ = [
    "BINARY_PROBABILITY_COLUMNS",
    "MODEL_CONTRACT_VERSION",
    "PROBABILITY_COLUMNS",
    "PROBABILITY_DOWN",
    "PROBABILITY_NEUTRAL",
    "PROBABILITY_SUM_TOLERANCE",
    "PROBABILITY_UP",
    "ClassFrequencyModel",
    "ModelError",
    "Prediction",
    "PredictionContext",
    "ProbabilityModel",
    "build_predictions",
    "predictions_to_records",
]
