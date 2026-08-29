"""V4's trained model, as a value: the scoring half of the probability engine.

A trained model reaches this package as data, never as a file. Everything here
is a plain value built from a plain mapping - coefficients, split thresholds,
a calibration curve, and the provenance that says what produced them - and
every method on it is arithmetic. The fitting half lives in `autotrader.ml.v4`,
which is allowed to read a dataset, hold numpy, and write an artifact, and which
emits exactly the record `artifact_from_record` reads back.

**Why the split is that way round and not the other.** The decision package is
the leftmost box of the pipeline and is fenced off by tests that are older than
this module: it may not open a file, may not import numpy, and may not reach
anything that could reach a broker. The ML foundation may do the first two and
is fenced off from the third separately. So the trained parameters travel from
the training package to this one as a record, and the direction of the import
arrow - `ml` may see `decision`, `decision` may never see `ml` - is what keeps
both fences standing.

**One scoring implementation, not two.** The obvious failure of a split like
this is a model that trains under one arithmetic and serves under another,
disagreeing in the third decimal for a year before anyone notices. The training
package therefore fits with numpy and then evaluates *through this module* for
every number it reports - its walk-forward metrics, its calibration curve, its
recorded comparison. numpy appears in the fitting loop and nowhere downstream
of it, and a parity test pins the two together on the same rows.

**Pure Python is not a compromise here.** A decision is one bar. Scoring one bar
against a linear model is a dot product over seven terms, and against a boosted
ensemble is a few hundred walks down a depth-3 tree. Both are microseconds, and
buying them with a dependency the trading process would then carry for the rest
of its life is a bad trade.

**The artifact refuses to be used against features it was not trained on.** It
carries the feature schema version and the exact column list it saw, and
`ProbabilityArtifact.require_compatible_with` checks both before a probability
is produced. A model fitted on a redefined `ema_spread_z` and served against
the new one would be confidently, silently wrong - the columns still line up,
the numbers still look like probabilities - which is the failure that versioning
exists to make loud.

**Calibration travels with the model, because a probability that is not
calibrated is not a probability.** `IdentityCalibration` says out loud that none
was fitted; `IsotonicCalibration` carries the fitted step function itself, so
the mapping applied live is the mapping that was measured, rather than one
re-derived at load time from data that may no longer be around.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from autotrader.decision.contract import DecisionError
from autotrader.decision.features import FEATURE_SCHEMA_VERSION, SCORED_FEATURES

#: The version of the artifact *shape* below, distinct from any model's own
#: version. This changes when the record's structure changes, which invalidates
#: every stored artifact at once rather than one of them.
PROBABILITY_CONTRACT_VERSION = "1.0.0"

#: The columns V4 reads, and the reason it is this list and not a longer one.
#:
#: Exactly the decision layer's own `SCORED_FEATURES`: the seven measurements
#: that are unit-free by construction - each one either a ratio against the
#: market's own baseline or a standardization against its own trailing spread.
#: Three consequences follow, and all three are why the set is reused rather
#: than extended. They are comparable across BTC/USD and SPY, so one model can
#: be fitted per asset class instead of per symbol. They are the same numbers
#: V2 and V3 judge, so a V5 ensemble combines two readings of one set of
#: measurements rather than two different views of the market. And the raw
#: levels deliberately left out - `ema_fast`, `macd`, `atr`, `close` - carry the
#: price scale of whichever symbol and year they came from, which a linear model
#: will happily fit and then fail to generalize from.
V4_FEATURE_COLUMNS: tuple[str, ...] = SCORED_FEATURES

#: The estimator families this contract can express, as stored in a record.
FAMILY_LOGISTIC = "logistic"
FAMILY_GRADIENT_BOOSTED = "gradient_boosted_trees"
FAMILY_CLASS_FREQUENCY = "class_frequency"

#: The calibration methods this contract can express.
CALIBRATION_IDENTITY = "identity"
CALIBRATION_ISOTONIC = "isotonic"

#: A leaf's `feature` entry. Negative because a leaf splits on nothing, and a
#: sentinel outside the valid index range cannot collide with feature 0.
LEAF_FEATURE = -1


class ProbabilityModelError(DecisionError):
    """A stored model that cannot be read, or cannot be applied to these features."""


def sigmoid(value: float) -> float:
    """The logistic function, evaluated so that it cannot overflow.

    `exp` is only ever called on a non-positive argument, which underflows to
    zero at the extremes instead of raising. The naive form raises
    `OverflowError` somewhere past a score of 710, which is a value a boosted
    ensemble on a degenerate fold can genuinely produce.
    """
    numeric = float(value)
    if numeric != numeric:
        raise ProbabilityModelError(
            "A model score of NaN cannot become a probability. An unmeasurable "
            "bar is an explicit HOLD upstream, never a number that propagates."
        )
    if numeric >= 0.0:
        return 1.0 / (1.0 + math.exp(-numeric))
    exponential = math.exp(numeric)
    return exponential / (1.0 + exponential)


def _require_probability(value: float, field_name: str) -> float:
    """Refuse anything that is not a finite number in ``[0, 1]``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProbabilityModelError(f"{field_name} must be a real number, got {value!r}.")
    numeric = float(value)
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        raise ProbabilityModelError(f"{field_name} must be finite, got {numeric!r}.")
    if not 0.0 <= numeric <= 1.0:
        raise ProbabilityModelError(f"{field_name} must lie in [0, 1], got {numeric}.")
    return numeric


def _require_finite(value: object, field_name: str) -> float:
    """Refuse a non-numeric, NaN or infinite model parameter."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProbabilityModelError(f"{field_name} must be a real number, got {value!r}.")
    numeric = float(value)
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        raise ProbabilityModelError(f"{field_name} must be finite, got {numeric!r}.")
    return numeric


def _floats(values: object, field_name: str) -> tuple[float, ...]:
    """A tuple of finite floats from a stored sequence."""
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ProbabilityModelError(f"{field_name} must be a sequence of numbers.")
    return tuple(
        _require_finite(value, f"{field_name}[{index}]") for index, value in enumerate(values)
    )


def _integers(values: object, field_name: str) -> tuple[int, ...]:
    """A tuple of ints from a stored sequence."""
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ProbabilityModelError(f"{field_name} must be a sequence of integers.")
    built: list[int] = []
    for index, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProbabilityModelError(f"{field_name}[{index}] must be an int, got {value!r}.")
        built.append(int(value))
    return tuple(built)


# --------------------------------------------------------------------------
# Standardization
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureStandardizer:
    """Per-feature centring and scaling, fitted on training rows alone.

    Stored with the model rather than recomputed at load time, and that is the
    whole point of it being here. A scaler fitted over a whole dataset before
    it was split has already read the test period's mean, which is look-ahead
    wearing a preprocessing hat: the model's inputs on a test row depend on
    values from that row's own future. Fitting on the training fold and
    *shipping the fitted constants* is the only arrangement where the numbers a
    live bar is scaled by are the same numbers the evaluation used.

    A feature whose training spread was zero is scaled by one rather than by
    zero. A constant input contributes nothing to a fitted model in any case,
    and dividing by its spread would turn an uninformative column into an
    infinite one.
    """

    means: tuple[float, ...]
    scales: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.means) != len(self.scales):
            raise ProbabilityModelError(
                f"A standardizer needs one mean and one scale per feature; got "
                f"{len(self.means)} mean(s) and {len(self.scales)} scale(s)."
            )
        if not self.means:
            raise ProbabilityModelError("A standardizer must cover at least one feature.")
        for index, scale in enumerate(self.scales):
            if scale <= 0.0:
                raise ProbabilityModelError(
                    f"scales[{index}] is {scale}, which is not a usable divisor. A "
                    "feature with no training spread is stored with a scale of 1.0."
                )

    def apply(self, values: Sequence[float]) -> tuple[float, ...]:
        """Centre and scale one row of raw feature values."""
        if len(values) != len(self.means):
            raise ProbabilityModelError(
                f"This standardizer covers {len(self.means)} feature(s) but was given "
                f"{len(values)}."
            )
        return tuple(
            (float(value) - mean) / scale
            for value, mean, scale in zip(values, self.means, self.scales, strict=True)
        )

    def to_record(self) -> dict[str, object]:
        return {"means": list(self.means), "scales": list(self.scales)}

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> FeatureStandardizer:
        return cls(
            means=_floats(record.get("means"), "standardizer.means"),
            scales=_floats(record.get("scales"), "standardizer.scales"),
        )

    @classmethod
    def identity(cls, width: int) -> FeatureStandardizer:
        """A standardizer that changes nothing, for a model that needs no scaling."""
        return cls(means=(0.0,) * width, scales=(1.0,) * width)


# --------------------------------------------------------------------------
# Estimators
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LogisticEstimator:
    """L2-regularised logistic regression: the baseline V4 is expected to ship.

    A linear model on standardized inputs, which is the most a tabular problem
    with seven features, a low signal-to-noise ratio, and a few thousand rows
    usually supports. Its coefficients are readable, which matters more here
    than it does elsewhere: `reasons` on a `DecisionResult` can name the two
    features that actually moved this bar's probability, and an operator can
    check that the model did not learn something absurd.
    """

    intercept: float
    coefficients: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.coefficients:
            raise ProbabilityModelError("A logistic model needs at least one coefficient.")

    @property
    def width(self) -> int:
        """How many features this estimator expects."""
        return len(self.coefficients)

    def raw_score(self, standardized: Sequence[float]) -> float:
        """The log-odds this model assigns to one standardized row."""
        if len(standardized) != self.width:
            raise ProbabilityModelError(
                f"This model expects {self.width} feature(s), got {len(standardized)}."
            )
        total = float(self.intercept)
        for value, coefficient in zip(standardized, self.coefficients, strict=True):
            total += float(value) * coefficient
        return total

    def contributions(self, standardized: Sequence[float]) -> tuple[float, ...]:
        """Each feature's signed contribution to the log-odds, before the intercept.

        Exact for a linear model rather than an attribution heuristic, which is
        the reason the audit record can carry it without a disclaimer.
        """
        return tuple(
            float(value) * coefficient
            for value, coefficient in zip(standardized, self.coefficients, strict=True)
        )

    def to_record(self) -> dict[str, object]:
        return {
            "family": FAMILY_LOGISTIC,
            "intercept": float(self.intercept),
            "coefficients": list(self.coefficients),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> LogisticEstimator:
        return cls(
            intercept=_require_finite(record.get("intercept"), "estimator.intercept"),
            coefficients=_floats(record.get("coefficients"), "estimator.coefficients"),
        )


@dataclass(frozen=True)
class DecisionTree:
    """One shallow regression tree, stored as parallel arrays.

    Flat rather than nested because a tree that is a set of integer arrays can
    be serialized, hashed, and validated for acyclicity without recursion, and
    because walking it is an index loop rather than a chain of dictionary
    lookups. A leaf carries `LEAF_FEATURE` and its contribution is `value`.

    Children are required to sit at a higher index than their parent, which is
    what makes the walk in `leaf_value` provably terminate: a malformed record
    that pointed a node back at itself would otherwise be an infinite loop
    inside a trading decision rather than a refusal at load time.
    """

    feature: tuple[int, ...]
    threshold: tuple[float, ...]
    left: tuple[int, ...]
    right: tuple[int, ...]
    value: tuple[float, ...]

    def __post_init__(self) -> None:
        size = len(self.feature)
        if size == 0:
            raise ProbabilityModelError("A tree needs at least one node.")
        for name in ("threshold", "left", "right", "value"):
            if len(getattr(self, name)) != size:
                raise ProbabilityModelError(
                    f"A tree's arrays must all hold {size} entries; {name} holds "
                    f"{len(getattr(self, name))}."
                )
        for node in range(size):
            if self.feature[node] == LEAF_FEATURE:
                continue
            if self.feature[node] < 0:
                raise ProbabilityModelError(
                    f"Node {node} splits on feature {self.feature[node]}, which is neither "
                    f"a valid index nor the leaf sentinel {LEAF_FEATURE}."
                )
            for side, children in (("left", self.left), ("right", self.right)):
                child = children[node]
                if not node < child < size:
                    raise ProbabilityModelError(
                        f"Node {node}'s {side} child is {child}, which is not a later node "
                        f"in a tree of {size}. A tree that can revisit a node is a loop."
                    )

    @property
    def max_feature_index(self) -> int:
        """The highest feature index this tree splits on, or -1 for a stump."""
        return max(self.feature)

    def leaf_value(self, standardized: Sequence[float]) -> float:
        """Walk one row down to its leaf and return that leaf's contribution.

        The comparison is ``<=`` on the left, matching the fitting side. A
        boundary rule that disagreed between fitting and serving would move
        exactly the rows that sit on a split point, which are the rows a tree is
        least confident about and most likely to be asked for.
        """
        node = 0
        while self.feature[node] != LEAF_FEATURE:
            index = self.feature[node]
            if index >= len(standardized):
                raise ProbabilityModelError(
                    f"This tree splits on feature {index} but was given "
                    f"{len(standardized)} feature(s)."
                )
            node = (
                self.left[node]
                if float(standardized[index]) <= self.threshold[node]
                else self.right[node]
            )
        return float(self.value[node])

    def to_record(self) -> dict[str, object]:
        return {
            "feature": list(self.feature),
            "threshold": list(self.threshold),
            "left": list(self.left),
            "right": list(self.right),
            "value": list(self.value),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> DecisionTree:
        return cls(
            feature=_integers(record.get("feature"), "tree.feature"),
            threshold=_floats(record.get("threshold"), "tree.threshold"),
            left=_integers(record.get("left"), "tree.left"),
            right=_integers(record.get("right"), "tree.right"),
            value=_floats(record.get("value"), "tree.value"),
        )


@dataclass(frozen=True)
class GradientBoostedEstimator:
    """An additive ensemble of shallow trees, summed in log-odds space.

    The learning rate is folded into every leaf value at fitting time rather
    than stored beside the trees, so serving is a plain sum and there is no
    second place a shrinkage factor could be applied twice or not at all.

    `width` is declared rather than inferred from the splits. A tree ensemble
    that happens never to split on the last feature would otherwise report a
    narrower width than it was trained at, and would then silently accept a
    short feature row.
    """

    base_score: float
    width: int
    trees: tuple[DecisionTree, ...]

    def __post_init__(self) -> None:
        if isinstance(self.width, bool) or not isinstance(self.width, int) or self.width < 1:
            raise ProbabilityModelError(f"width must be a positive int, got {self.width!r}.")
        if not self.trees:
            raise ProbabilityModelError(
                "A boosted model with no trees is its base rate wearing a "
                "tree ensemble's name. Store a class-frequency model instead."
            )
        for position, tree in enumerate(self.trees):
            if tree.max_feature_index >= self.width:
                raise ProbabilityModelError(
                    f"Tree {position} splits on feature {tree.max_feature_index}, outside "
                    f"the declared width of {self.width}."
                )

    def raw_score(self, standardized: Sequence[float]) -> float:
        """The log-odds this ensemble assigns to one standardized row."""
        if len(standardized) != self.width:
            raise ProbabilityModelError(
                f"This model expects {self.width} feature(s), got {len(standardized)}."
            )
        total = float(self.base_score)
        for tree in self.trees:
            total += tree.leaf_value(standardized)
        return total

    def to_record(self) -> dict[str, object]:
        return {
            "family": FAMILY_GRADIENT_BOOSTED,
            "base_score": float(self.base_score),
            "width": int(self.width),
            "trees": [tree.to_record() for tree in self.trees],
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> GradientBoostedEstimator:
        trees = record.get("trees")
        if isinstance(trees, str) or not isinstance(trees, Sequence):
            raise ProbabilityModelError("estimator.trees must be a sequence of trees.")
        width = record.get("width")
        if isinstance(width, bool) or not isinstance(width, int):
            raise ProbabilityModelError(f"estimator.width must be an int, got {width!r}.")
        return cls(
            base_score=_require_finite(record.get("base_score"), "estimator.base_score"),
            width=int(width),
            trees=tuple(DecisionTree.from_record(_mapping(tree, "tree")) for tree in trees),
        )


@dataclass(frozen=True)
class ClassFrequencyEstimator:
    """The null baseline: the training set's base rate, on every bar.

    Not a trading model, and stored in the same contract on purpose. It is the
    floor a candidate has to clear - a model that cannot beat the base rate has
    found nothing, however healthy its accuracy looks on an unbalanced target -
    and expressing it as an artifact means the comparison is run through the
    same scoring path as everything it is compared against.
    """

    probability_up: float
    width: int

    def __post_init__(self) -> None:
        _require_probability(self.probability_up, "probability_up")
        if isinstance(self.width, bool) or not isinstance(self.width, int) or self.width < 1:
            raise ProbabilityModelError(f"width must be a positive int, got {self.width!r}.")

    def raw_score(self, standardized: Sequence[float]) -> float:
        """The base rate's log-odds. The features are accepted and not read."""
        if len(standardized) != self.width:
            raise ProbabilityModelError(
                f"This model expects {self.width} feature(s), got {len(standardized)}."
            )
        probability = min(max(float(self.probability_up), 1e-12), 1.0 - 1e-12)
        return math.log(probability / (1.0 - probability))

    def to_record(self) -> dict[str, object]:
        return {
            "family": FAMILY_CLASS_FREQUENCY,
            "probability_up": float(self.probability_up),
            "width": int(self.width),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> ClassFrequencyEstimator:
        width = record.get("width")
        if isinstance(width, bool) or not isinstance(width, int):
            raise ProbabilityModelError(f"estimator.width must be an int, got {width!r}.")
        return cls(
            probability_up=_require_finite(
                record.get("probability_up"), "estimator.probability_up"
            ),
            width=int(width),
        )


#: Every estimator family, by the token stored in a record.
Estimator = LogisticEstimator | GradientBoostedEstimator | ClassFrequencyEstimator

_ESTIMATOR_FAMILIES = {
    FAMILY_LOGISTIC: LogisticEstimator,
    FAMILY_GRADIENT_BOOSTED: GradientBoostedEstimator,
    FAMILY_CLASS_FREQUENCY: ClassFrequencyEstimator,
}


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    """Refuse a record fragment that is not a mapping."""
    if not isinstance(value, Mapping):
        raise ProbabilityModelError(f"{field_name} must be a mapping, got {type(value).__name__}.")
    return value


def estimator_from_record(record: Mapping[str, object]) -> Estimator:
    """Rebuild whichever estimator family a stored record names."""
    family = str(record.get("family", ""))
    builder = _ESTIMATOR_FAMILIES.get(family)
    if builder is None:
        known = ", ".join(sorted(_ESTIMATOR_FAMILIES))
        raise ProbabilityModelError(
            f"Unknown estimator family {family!r}. Known families: {known}."
        )
    return builder.from_record(record)


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentityCalibration:
    """Passes a raw score through unchanged, and says so in the record.

    The honest default rather than a placeholder. An artifact recording
    `identity` is stating that no calibration was fitted, which is exactly what
    a reader needs to know before treating its output as a probability - and
    `ProbabilityAssessment.calibrated` reports the same fact upwards, so a V5
    ensemble can decline to size on an uncalibrated number.
    """

    method: str = CALIBRATION_IDENTITY

    def apply(self, probability: float) -> float:
        return _require_probability(probability, "probability")

    def to_record(self) -> dict[str, object]:
        return {"method": CALIBRATION_IDENTITY}


@dataclass(frozen=True)
class IsotonicCalibration:
    """A fitted monotone step function, stored as the steps themselves.

    Isotonic regression on held-out scores produces a non-decreasing piecewise
    constant map from raw score to observed frequency. `thresholds` are the
    left edges of those steps in ascending order and `values` are the
    frequencies; a score is mapped by the last step whose edge it has reached.

    Stored as the fitted steps rather than as the data they came from, because
    the mapping applied to a live bar has to be the mapping that was measured.
    Refitting at load time would silently depend on whichever validation rows
    happened to still be on disk.

    Monotonicity is checked here rather than assumed. It is the one property
    that makes the mapping order-preserving, and a record that lost it - by an
    edit, a truncation, a merge - would reorder two bars' probabilities relative
    to the scores the model actually produced.
    """

    thresholds: tuple[float, ...]
    values: tuple[float, ...]
    method: str = CALIBRATION_ISOTONIC

    def __post_init__(self) -> None:
        if len(self.thresholds) != len(self.values):
            raise ProbabilityModelError(
                f"An isotonic calibration needs one value per threshold; got "
                f"{len(self.thresholds)} threshold(s) and {len(self.values)} value(s)."
            )
        if not self.thresholds:
            raise ProbabilityModelError("An isotonic calibration needs at least one step.")
        for index in range(len(self.thresholds)):
            _require_probability(self.values[index], f"values[{index}]")
            if index and self.thresholds[index] <= self.thresholds[index - 1]:
                raise ProbabilityModelError(
                    f"thresholds must ascend strictly; thresholds[{index}] is "
                    f"{self.thresholds[index]} after {self.thresholds[index - 1]}."
                )
            if index and self.values[index] < self.values[index - 1]:
                raise ProbabilityModelError(
                    f"An isotonic map must not decrease; values[{index}] is "
                    f"{self.values[index]} after {self.values[index - 1]}."
                )

    def apply(self, probability: float) -> float:
        """Map a raw score onto its step.

        A linear scan rather than a bisection: the step count is bounded by the
        number of distinct fitted levels, which is tens, and a scan has no
        boundary condition to get wrong.
        """
        score = _require_probability(probability, "probability")
        calibrated = self.values[0]
        for threshold, value in zip(self.thresholds, self.values, strict=True):
            if score < threshold:
                break
            calibrated = value
        return float(calibrated)

    def to_record(self) -> dict[str, object]:
        return {
            "method": CALIBRATION_ISOTONIC,
            "thresholds": list(self.thresholds),
            "values": list(self.values),
        }


Calibration = IdentityCalibration | IsotonicCalibration


def calibration_from_record(record: Mapping[str, object]) -> Calibration:
    """Rebuild whichever calibration a stored record names."""
    method = str(record.get("method", ""))
    if method == CALIBRATION_IDENTITY:
        return IdentityCalibration()
    if method == CALIBRATION_ISOTONIC:
        return IsotonicCalibration(
            thresholds=_floats(record.get("thresholds"), "calibration.thresholds"),
            values=_floats(record.get("values"), "calibration.values"),
        )
    raise ProbabilityModelError(
        f"Unknown calibration method {method!r}. Known methods: "
        f"{CALIBRATION_IDENTITY}, {CALIBRATION_ISOTONIC}."
    )


# --------------------------------------------------------------------------
# The artifact
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TrainingWindow:
    """Which rows a model saw, as an interval and a count.

    Part of an artifact's identity rather than a note attached to it: two models
    with identical hyperparameters fitted over different windows are different
    models, and the window is the field that says so.
    """

    first_feature_timestamp: str
    last_feature_timestamp: str
    rows: int
    symbols: tuple[str, ...]
    asset_class: str

    def __post_init__(self) -> None:
        if not self.symbols:
            raise ProbabilityModelError("A training window must name the symbol(s) it covers.")
        if isinstance(self.rows, bool) or not isinstance(self.rows, int) or self.rows < 1:
            raise ProbabilityModelError(f"rows must be a positive int, got {self.rows!r}.")

    def to_record(self) -> dict[str, object]:
        return {
            "first_feature_timestamp": self.first_feature_timestamp,
            "last_feature_timestamp": self.last_feature_timestamp,
            "rows": int(self.rows),
            "symbols": list(self.symbols),
            "asset_class": self.asset_class,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> TrainingWindow:
        symbols = record.get("symbols")
        if isinstance(symbols, str) or not isinstance(symbols, Sequence):
            raise ProbabilityModelError("training_window.symbols must be a sequence.")
        rows = record.get("rows")
        if isinstance(rows, bool) or not isinstance(rows, int):
            raise ProbabilityModelError(f"training_window.rows must be an int, got {rows!r}.")
        return cls(
            first_feature_timestamp=str(record.get("first_feature_timestamp", "")),
            last_feature_timestamp=str(record.get("last_feature_timestamp", "")),
            rows=int(rows),
            symbols=tuple(str(symbol) for symbol in symbols),
            asset_class=str(record.get("asset_class", "")),
        )


@dataclass(frozen=True)
class ProbabilityArtifact:
    """One trained V4 model, complete: parameters, calibration, and provenance.

    Everything needed to reproduce a probability and to say what produced it.
    `model_version` is the operator's name for this model; `feature_version` and
    `feature_columns` are what it may be applied to; `training_window`,
    `label_spec_id`, `code_revision` and `hyperparameters` are how it came to
    exist. The artifact is frozen, so a probability and the record of what
    produced it cannot drift apart after the fact.
    """

    model_version: str
    feature_version: str
    feature_columns: tuple[str, ...]
    label_spec_id: str
    standardizer: FeatureStandardizer
    estimator: Estimator
    calibration: Calibration
    training_window: TrainingWindow
    trained_at_utc: str = ""
    code_revision: Mapping[str, object] = field(default_factory=dict)
    hyperparameters: Mapping[str, object] = field(default_factory=dict)
    metrics: Mapping[str, float] = field(default_factory=dict)
    seed: int = 0
    notes: str = ""

    def __post_init__(self) -> None:
        for name in ("model_version", "feature_version", "label_spec_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ProbabilityModelError(f"{name} must be a non-empty string, got {value!r}.")
        if not self.feature_columns:
            raise ProbabilityModelError("An artifact must record which features it reads.")
        width = len(self.feature_columns)
        if len(self.standardizer.means) != width:
            raise ProbabilityModelError(
                f"This artifact reads {width} feature(s) but its standardizer covers "
                f"{len(self.standardizer.means)}."
            )
        object.__setattr__(self, "feature_columns", tuple(self.feature_columns))
        object.__setattr__(self, "code_revision", MappingProxyType(dict(self.code_revision)))
        object.__setattr__(self, "hyperparameters", MappingProxyType(dict(self.hyperparameters)))
        object.__setattr__(
            self, "metrics", MappingProxyType({str(k): float(v) for k, v in self.metrics.items()})
        )

    @property
    def family(self) -> str:
        """Which estimator family this artifact holds."""
        return str(self.estimator.to_record()["family"])

    @property
    def calibrated(self) -> bool:
        """Whether a calibration was actually fitted, rather than passed through."""
        return not isinstance(self.calibration, IdentityCalibration)

    @property
    def calibration_method(self) -> str:
        """The calibration method's stable token, for the audit record."""
        return str(self.calibration.to_record()["method"])

    def require_compatible_with(
        self,
        *,
        feature_version: str = FEATURE_SCHEMA_VERSION,
        feature_columns: Sequence[str] = V4_FEATURE_COLUMNS,
    ) -> None:
        """Refuse to apply this model to a feature set it was not fitted on.

        Both halves are checked, because they fail differently. A changed
        *version* means a column was redefined - the names still line up and the
        numbers still look like features, so nothing downstream could notice. A
        changed *column list* means the model is being handed inputs in an order
        or a shape it never saw, which is a coefficient applied to the wrong
        measurement.
        """
        if self.feature_version != feature_version:
            raise ProbabilityModelError(
                f"Model {self.model_version!r} was fitted on feature schema "
                f"{self.feature_version!r}, but this engine computes "
                f"{feature_version!r}. A feature redefinition changes what every "
                "coefficient means; retrain rather than reuse."
            )
        expected = tuple(feature_columns)
        if self.feature_columns != expected:
            raise ProbabilityModelError(
                f"Model {self.model_version!r} reads {', '.join(self.feature_columns)} but "
                f"this engine supplies {', '.join(expected)}."
            )

    def probability_up(self, values: Sequence[float]) -> float:
        """The calibrated probability that this bar's forward interval is up.

        The whole scoring path in one method: standardize with the constants
        fitted on the training fold, evaluate the estimator, squash to a
        probability, then apply the fitted calibration. Every consumer - the
        engine, the training package's own evaluation, the parity test - goes
        through here, so there is one answer rather than several.
        """
        standardized = self.standardizer.apply(values)
        return self.calibration.apply(sigmoid(self.estimator.raw_score(standardized)))

    def uncalibrated_probability_up(self, values: Sequence[float]) -> float:
        """The estimator's own output, before calibration.

        Reported alongside the calibrated number rather than instead of it, so
        an audit can see how far the calibration moved a given bar - which is
        the quantity that says whether the calibration is doing real work or
        merely passing scores along.
        """
        standardized = self.standardizer.apply(values)
        return sigmoid(self.estimator.raw_score(standardized))

    def feature_contributions(self, values: Sequence[float]) -> Mapping[str, float]:
        """Each feature's signed contribution to the log-odds, where that is exact.

        Only a linear model has one. A boosted ensemble's output is not a sum of
        per-feature terms, and inventing an attribution for it - by permutation,
        by gain, by anything - would put a number in an audit record that reads
        like a measurement and is an estimate. An empty mapping is the honest
        answer, and the reason tokens still name the direction and the strength.
        """
        if not isinstance(self.estimator, LogisticEstimator):
            return MappingProxyType({})
        standardized = self.standardizer.apply(values)
        contributions = self.estimator.contributions(standardized)
        return MappingProxyType(dict(zip(self.feature_columns, contributions, strict=True)))

    def to_record(self) -> dict[str, object]:
        """The serializable form. Carries no credential and no account field."""
        return {
            "probability_contract_version": PROBABILITY_CONTRACT_VERSION,
            "model_version": self.model_version,
            "model_family": self.family,
            "feature_version": self.feature_version,
            "feature_columns": list(self.feature_columns),
            "label_spec_id": self.label_spec_id,
            "trained_at_utc": self.trained_at_utc,
            "seed": int(self.seed),
            "training_window": self.training_window.to_record(),
            "code_revision": dict(self.code_revision),
            "hyperparameters": dict(self.hyperparameters),
            "standardizer": self.standardizer.to_record(),
            "estimator": self.estimator.to_record(),
            "calibration": self.calibration.to_record(),
            "metrics": dict(self.metrics),
            "notes": self.notes,
        }


def artifact_from_record(record: Mapping[str, object]) -> ProbabilityArtifact:
    """Rebuild an artifact from the record `to_record` produced.

    The contract version is checked first and by exact equality. A record
    written under a different shape may still carry every key this function
    reads, and would rebuild into a model that is wrong in whichever field the
    shape change moved - so the version is a gate rather than a hint.
    """
    stored = _mapping(record, "record")
    version = str(stored.get("probability_contract_version", ""))
    if version != PROBABILITY_CONTRACT_VERSION:
        raise ProbabilityModelError(
            f"This artifact was written under probability contract {version!r}, but this "
            f"code reads {PROBABILITY_CONTRACT_VERSION!r}."
        )
    columns = stored.get("feature_columns")
    if isinstance(columns, str) or not isinstance(columns, Sequence):
        raise ProbabilityModelError("feature_columns must be a sequence of column names.")
    seed = stored.get("seed", 0)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ProbabilityModelError(f"seed must be an int, got {seed!r}.")
    metrics = _mapping(stored.get("metrics", {}), "metrics")
    return ProbabilityArtifact(
        model_version=str(stored.get("model_version", "")),
        feature_version=str(stored.get("feature_version", "")),
        feature_columns=tuple(str(name) for name in columns),
        label_spec_id=str(stored.get("label_spec_id", "")),
        standardizer=FeatureStandardizer.from_record(
            _mapping(stored.get("standardizer"), "standardizer")
        ),
        estimator=estimator_from_record(_mapping(stored.get("estimator"), "estimator")),
        calibration=calibration_from_record(_mapping(stored.get("calibration"), "calibration")),
        training_window=TrainingWindow.from_record(
            _mapping(stored.get("training_window"), "training_window")
        ),
        trained_at_utc=str(stored.get("trained_at_utc", "")),
        code_revision=dict(_mapping(stored.get("code_revision", {}), "code_revision")),
        hyperparameters=dict(_mapping(stored.get("hyperparameters", {}), "hyperparameters")),
        metrics={str(name): float(value) for name, value in metrics.items()},
        seed=int(seed),
        notes=str(stored.get("notes", "")),
    )


__all__ = [
    "CALIBRATION_IDENTITY",
    "CALIBRATION_ISOTONIC",
    "FAMILY_CLASS_FREQUENCY",
    "FAMILY_GRADIENT_BOOSTED",
    "FAMILY_LOGISTIC",
    "LEAF_FEATURE",
    "PROBABILITY_CONTRACT_VERSION",
    "V4_FEATURE_COLUMNS",
    "Calibration",
    "ClassFrequencyEstimator",
    "DecisionTree",
    "Estimator",
    "FeatureStandardizer",
    "GradientBoostedEstimator",
    "IdentityCalibration",
    "IsotonicCalibration",
    "LogisticEstimator",
    "ProbabilityArtifact",
    "ProbabilityModelError",
    "TrainingWindow",
    "artifact_from_record",
    "calibration_from_record",
    "estimator_from_record",
    "sigmoid",
]
