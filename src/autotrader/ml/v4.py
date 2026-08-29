"""V4 training: fitting the probability model the decision layer serves.

This is the half of V4 that reads a dataset, holds numpy, and writes an
artifact. The half that *scores* is `autotrader.decision.probability`, and the
split is not stylistic: the decision package is fenced off from the filesystem
and from numpy by tests older than this module, so the trained parameters have
to travel to it as a record. `train_model` produces exactly the record
`autotrader.decision.probability.artifact_from_record` reads back.

**Every number this module reports is scored through the decision package.**
numpy appears inside the fitting loops - a Newton step, a split search - and
nowhere after them. The moment an estimator exists it is converted into its
decision-layer value, and every probability quoted downstream of that point
(the walk-forward comparison, the calibration curve, the test metrics that go
into the artifact) is produced by `ProbabilityArtifact.probability_up`. There is
therefore one scoring implementation rather than a training one and a serving
one that agree until they do not, and `tests/test_decision_v4.py` pins the two
together on the same rows anyway.

**Features come from the decision layer, not from `autotrader.ml.features`.**
The ML foundation's own thirteen features build datasets; V4 has to be fitted on
exactly what a live V4 will see, and a live V4 sees
`autotrader.decision.features.compute_features` because that is what the
decision package computes. Training on one feature layer and serving from
another is train/serve skew with a version number on it. The consequence is that
V4's feature version *is* the decision layer's `FEATURE_SCHEMA_VERSION`, and an
artifact is refused against a different one.

**Walk-forward, never k-fold.** Model selection runs on
`autotrader.ml.splits.walk_forward_folds`, which was written for this: anchored
folds that train on the past and are graded on their own future, each with the
purge and embargo already applied. There is no shuffle anywhere in this module
and no parameter that would enable one.

**Fitting sees the training fold and nothing else.** The standardizer's means
and scales are computed on training rows, the estimator is fitted on training
rows, and the calibration is fitted on the *validation* split - never training,
where the model has already seen the outcomes and would learn to correct a bias
that will not be there live, and never test, which is straightforward leakage.
`assert_no_leakage` is called on every split this module produces.

**Complexity has to earn its place.** `select_candidate` prefers the simplest
family whose walk-forward log loss is not materially worse than the best, and
refuses any candidate that cannot beat the class-frequency baseline by
`MATERIAL_LOG_LOSS_IMPROVEMENT`. A boosted ensemble that ties a linear model
loses, because it costs more to reason about and reproduces less readily for the
same evidence. The comparison is recorded in full either way, so the decision
can be re-examined rather than taken on trust.

**Determinism, and its one honest limit.** Nothing here reads a clock, a process
id, or an unseeded generator: the Newton solver runs a fixed number of iterations
to a fixed tolerance, and the split search breaks ties towards the lowest feature
index and the lowest threshold. Given the same frame, configuration and seed,
this module produces byte-identical artifact records, and a test asserts it. What
cannot be promised is bit-identical output across *different numpy builds* -
`numpy.linalg.solve` dispatches to whichever BLAS the platform provides, and
summation order in a reduction is not part of numpy's API. The experiment record
stores the library versions for exactly that reason, which turns a surprising
mismatch into a diagnosable one rather than a mystery.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from autotrader.decision.config import IndicatorPeriods, policy_for_symbol
from autotrader.decision.features import FEATURE_SCHEMA_VERSION as DECISION_FEATURE_VERSION
from autotrader.decision.features import compute_features as compute_decision_features
from autotrader.decision.probability import (
    FAMILY_CLASS_FREQUENCY,
    FAMILY_GRADIENT_BOOSTED,
    FAMILY_LOGISTIC,
    LEAF_FEATURE,
    V4_FEATURE_COLUMNS,
    Calibration,
    ClassFrequencyEstimator,
    DecisionTree,
    Estimator,
    FeatureStandardizer,
    GradientBoostedEstimator,
    IdentityCalibration,
    IsotonicCalibration,
    LogisticEstimator,
    ProbabilityArtifact,
    TrainingWindow,
)
from autotrader.decision.timeframes import BASE_TIMEFRAME
from autotrader.ml import AssetClass, MLError, asset_class_for_symbol, normalize_symbol
from autotrader.ml.calibration import brier_score, expected_calibration_error
from autotrader.ml.dataset import build_observations
from autotrader.ml.experiment import ExperimentMetadata, GitProvenance, new_experiment
from autotrader.ml.features import bars_present_in_window
from autotrader.ml.grid import BarGrid, now_utc
from autotrader.ml.labels import (
    DIRECTION_UP,
    LabelKind,
    LabelSpec,
    SessionPolicy,
    ThresholdMode,
    compute_labels,
    label_columns,
)
from autotrader.ml.registry import (
    ArtifactMetadata,
    ArtifactStage,
    ModelRegistry,
    RegisteredArtifact,
    artifact_filename,
)
from autotrader.ml.schema import (
    FEATURE_WINDOW_BARS,
    ColumnRole,
    ColumnSpec,
    FeatureSchema,
    build_schema,
)
from autotrader.ml.splits import (
    SplitSpec,
    TemporalSplit,
    assert_no_leakage,
    temporal_split,
    walk_forward_folds,
)
from autotrader.ml.storage import ensure_directory, report_root, sha256_of_file, write_json
from autotrader.runtime.schedule import BAR_INTERVAL

#: The default forward horizon, in 15-minute bars. Four bars is one hour: long
#: enough that a move is not one bar's microstructure noise, short enough that
#: the position is flat well inside an equity session. Configurable, and
#: recorded in the label specification's fingerprint either way.
DEFAULT_HORIZON_BARS = 4

#: How much better than the class-frequency baseline a candidate's mean
#: walk-forward log loss has to be before it is admissible at all.
#:
#: Log loss on a near-balanced binary target sits around 0.693 for a model that
#: knows nothing. Two thousandths of a nat is small in absolute terms and is
#: deliberately not smaller: fold-to-fold variation on a few thousand rows is
#: larger than that, so a margin below it would promote noise.
MATERIAL_LOG_LOSS_IMPROVEMENT = 0.002

#: Families in increasing order of what they cost to reason about. Used to break
#: a tie towards the simpler model, which is the whole of this project's stance
#: on complexity: it is a cost, and it has to be paid for with evidence.
SIMPLICITY_ORDER: tuple[str, ...] = (
    FAMILY_CLASS_FREQUENCY,
    FAMILY_LOGISTIC,
    FAMILY_GRADIENT_BOOSTED,
)

#: Where V4's comparison reports are written under the reports root.
COMPARISONS_DIRECTORY = "v4-comparisons"

#: Probabilities are clamped this far from the ends before a log is taken. A
#: confident, wrong prediction should be punished heavily and not infinitely:
#: one such row would otherwise make a whole fold's mean log loss infinite and
#: destroy the comparison the metric exists to support.
LOG_LOSS_EPSILON = 1e-12


class V4TrainingError(MLError):
    """A V4 training run that cannot be performed on what it was given."""


# --------------------------------------------------------------------------
# The target
# --------------------------------------------------------------------------


def default_label_spec(
    *,
    horizon_bars: int = DEFAULT_HORIZON_BARS,
    session_policy: SessionPolicy = SessionPolicy.SPAN_SESSIONS,
) -> LabelSpec:
    """The target V4 is fitted against unless a caller names another.

    A binary direction label over a tradable interval: enter at the *next*
    bar's open, exit at the open `horizon_bars` later, and ask whether that
    return was positive. Entry one bar after the feature bar is not a parameter
    - `autotrader.ml.labels` refuses zero - because a decision taken when bar
    *t* closed cannot be filled inside bar *t*.

    The threshold is zero, which makes the label "did it go up" rather than
    "did it go up enough to pay for the spread". That is deliberate: costs
    belong to the backtester and to the risk engine, which already model them,
    and folding a cost assumption into the target would bake one particular
    fee schedule into every model fitted afterwards.
    """
    return LabelSpec(
        name="v4-direction",
        kind=LabelKind.DIRECTION,
        horizon_bars=horizon_bars,
        entry_price_column="open",
        exit_price_column="open",
        threshold_mode=ThresholdMode.ABSOLUTE,
        upper_threshold=0.0,
        session_policy=session_policy,
    )


# --------------------------------------------------------------------------
# The column contract of a V4 training frame
# --------------------------------------------------------------------------


def _feature_lookbacks(periods: IndicatorPeriods) -> Mapping[str, int]:
    """How many completed bars each V4 feature depends on, including its own.

    Derived from the decision layer's own warm-up properties rather than
    written down, so a change to an indicator period moves these with it. Each
    standardized feature is its raw feature's warm-up plus the standardization
    window that is then measured over it, less the bar the two share - which is
    exactly how `IndicatorPeriods.required_bars` is defined for the slowest of
    them.
    """
    window = periods.standardization_bars
    raw_warmups = {
        "ema_spread_z": max(periods.ema_slow, periods.atr_warmup),
        "ema_slope_z": periods.trend_warmup,
        "rsi_centered": periods.rsi_warmup,
        "macd_hist_z": periods.macd_warmup,
        "return_z": max(periods.return_warmup, periods.atr_warmup),
    }
    lookbacks = {name: warmup + window - 1 for name, warmup in raw_warmups.items()}
    lookbacks["volatility_ratio"] = periods.atr_warmup + periods.baseline_bars - 1
    lookbacks["volume_ratio"] = periods.baseline_bars
    return lookbacks


def v4_feature_columns(periods: IndicatorPeriods | None = None) -> tuple[ColumnSpec, ...]:
    """The `ColumnSpec` for every feature V4 reads, in contract order.

    Declared here rather than in the decision package because a `ColumnSpec` is
    an ML-foundation type and the decision package may not import one. Every
    entry declares `forward_bars=0`, which `ColumnSpec` enforces for the feature
    role - so "no V4 feature sees the future" is a property the schema refuses
    to express otherwise, rather than a claim in a docstring.
    """
    settings = periods if periods is not None else IndicatorPeriods()
    lookbacks = _feature_lookbacks(settings)
    return tuple(
        ColumnSpec(
            name=name,
            dtype="float64",
            role=ColumnRole.FEATURE,
            description=(
                f"autotrader.decision.features.{name}, computed by the same function the "
                "live V4 engine calls. Unit-free by construction and therefore comparable "
                "across symbols, asset classes and volatility regimes."
            ),
            lookback_bars=lookbacks[name],
            forward_bars=0,
        )
        for name in V4_FEATURE_COLUMNS
    )


def v4_schema(label: LabelSpec, *, periods: IndicatorPeriods | None = None) -> FeatureSchema:
    """The full column contract of a V4 training frame.

    Versioned by the decision layer's feature schema, because that is what
    decides what the feature columns mean. The fingerprint additionally covers
    the lookbacks, so a quietly widened indicator period changes the fingerprint
    even when the version does not move.
    """
    return build_schema(
        v4_feature_columns(periods),
        label_columns(label),
        version=DECISION_FEATURE_VERSION,
    )


# --------------------------------------------------------------------------
# Building a training frame
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TrainingFrame:
    """Rows a V4 model may be fitted on, plus how they were produced."""

    frame: pd.DataFrame
    schema: FeatureSchema
    label: LabelSpec
    symbol: str
    asset_class: AssetClass
    periods: IndicatorPeriods
    grid_row_count: int
    dropped_warmup_rows: int
    dropped_incomplete_window_rows: int

    @property
    def row_count(self) -> int:
        return len(self.frame)

    @property
    def labelled_row_count(self) -> int:
        return int(self.frame["label_valid"].fillna(False).sum())

    def matrix(self) -> np.ndarray:
        """The feature block as a dense float matrix, in contract order."""
        return self.frame.loc[:, list(V4_FEATURE_COLUMNS)].to_numpy(dtype="float64")


def build_training_frame(
    bars: pd.DataFrame,
    *,
    grid: BarGrid,
    label: LabelSpec | None = None,
    periods: IndicatorPeriods | None = None,
    minimum_bars_present_in_window: int = FEATURE_WINDOW_BARS,
) -> TrainingFrame:
    """Turn stored bars into rows a V4 model can be fitted on.

    Three things are joined: the decision layer's features, computed over the
    published bars exactly as the live engine computes them; the label
    framework's forward interval, computed on the *grid* so that a missing bar
    invalidates an interval rather than being stepped over; and the provenance
    columns `autotrader.ml.splits` needs to purge and embargo.

    The two are computed on different indexes on purpose. Features are
    positional over the bars a provider actually published, which is what a live
    engine is handed. Labels are positional over the grid, because whether a
    position could have been entered four bars later is a question about the
    market's clock and not about which bars happened to arrive. They are joined
    on the timestamp, so a row survives only where both are defined.

    A row is kept when its bar was published, its trailing window was
    sufficiently complete, and every V4 feature is defined on it. Warm-up rows
    are dropped rather than imputed: a model fitted on a filled-in standardized
    feature has been taught that a measurement nobody took was zero.
    """
    specification = label if label is not None else default_label_spec()
    symbol = normalize_symbol(str(pd.unique(bars["symbol"])[0]))
    asset_class = asset_class_for_symbol(symbol)
    if grid.asset_class.value != asset_class.value:
        raise V4TrainingError(
            f"{symbol} is a {asset_class.value} symbol but the grid is a "
            f"{grid.asset_class.value} grid."
        )
    settings = (
        periods
        if periods is not None
        else policy_for_symbol(symbol).timeframe(BASE_TIMEFRAME.label).periods
    )

    observations = build_observations(bars, grid, symbol)
    present_counts = bars_present_in_window(observations)

    features = compute_decision_features(bars, periods=settings)
    aligned = (
        features.set_index("timestamp")
        .loc[:, list(V4_FEATURE_COLUMNS) + ["realized_volatility"]]
        .reindex(pd.DatetimeIndex(observations["timestamp"]))
        .reset_index(drop=True)
    )

    volatility = (
        aligned["realized_volatility"]
        if specification.threshold_mode is ThresholdMode.VOLATILITY
        else None
    )
    labels = compute_labels(observations, grid, specification, volatility=volatility)

    schema = v4_schema(specification, periods=settings)
    assembled = pd.DataFrame(
        {
            "symbol": observations["symbol"],
            "feature_timestamp": observations["timestamp"],
            "knowable_at": observations["timestamp"] + BAR_INTERVAL,
            "asset_class": pd.array([asset_class.value] * len(grid), dtype="string"),
            "grid_index": np.arange(len(grid), dtype="int64"),
            "session_id": observations["session_id"],
            "session_bar_count": observations["session_bar_count"],
            "bars_present_in_window": present_counts,
        }
    )
    for name in V4_FEATURE_COLUMNS:
        assembled[name] = aligned[name]
    for column in label_columns(specification):
        assembled[column.name] = labels[column.name]

    published = observations["is_present"].to_numpy(dtype=bool)
    complete_window = present_counts.to_numpy(dtype="int64") >= minimum_bars_present_in_window
    defined = assembled.loc[:, list(V4_FEATURE_COLUMNS)].notna().all(axis=1).to_numpy(dtype=bool)

    keep = published & complete_window & defined
    frame = assembled.loc[keep].reset_index(drop=True)
    frame = frame[list(schema.names)]
    for name, dtype in schema.dtypes.items():
        frame[name] = frame[name].astype(dtype)
    schema.validate_frame(frame)

    return TrainingFrame(
        frame=frame,
        schema=schema,
        label=specification,
        symbol=symbol,
        asset_class=asset_class,
        periods=settings,
        grid_row_count=len(grid),
        dropped_warmup_rows=int((published & complete_window & ~defined).sum()),
        dropped_incomplete_window_rows=int((published & ~complete_window).sum()),
    )


def _labels_of(frame: pd.DataFrame) -> np.ndarray:
    """The binary target of a labelled frame, as 0.0/1.0."""
    values = frame["label"].to_numpy(dtype="float64")
    return (values == float(DIRECTION_UP)).astype("float64")


# --------------------------------------------------------------------------
# Fitting: the only place numpy is used
# --------------------------------------------------------------------------


def fit_standardizer(matrix: np.ndarray) -> FeatureStandardizer:
    """Per-feature mean and spread, computed on training rows alone.

    A feature whose training spread is zero gets a scale of 1.0 rather than 0.0.
    A constant column carries no information for any estimator to use, and
    dividing by its spread would turn a useless feature into an infinite one.
    """
    if matrix.ndim != 2 or matrix.shape[0] < 1:
        raise V4TrainingError("A standardizer needs at least one row of features.")
    means = matrix.mean(axis=0)
    scales = matrix.std(axis=0, ddof=0)
    scales = np.where(scales > 0.0, scales, 1.0)
    return FeatureStandardizer(
        means=tuple(float(value) for value in means),
        scales=tuple(float(value) for value in scales),
    )


def fit_logistic(
    standardized: np.ndarray,
    labels: np.ndarray,
    *,
    l2: float = 1.0,
    max_iterations: int = 50,
    tolerance: float = 1e-8,
) -> LogisticEstimator:
    """L2-regularised logistic regression, fitted by penalised Newton steps.

    Newton rather than gradient descent because with seven features the Hessian
    is an 8x8 matrix: one solve per iteration, convergence in under ten, and no
    learning rate to choose - which is one fewer arbitrary constant in a project
    that has to justify every one of them.

    The penalty is applied to the coefficients and not to the intercept.
    Shrinking the intercept would pull the model's base rate towards even odds
    regardless of what the data said, which is a bias nobody asked for; the
    penalty exists to control the *slopes*, and on a separable fold it is the
    only thing keeping them finite.

    Deterministic: a fixed starting point of zero, a fixed iteration cap, and a
    fixed tolerance. No randomness of any kind is involved, so the `seed` a
    caller records for this family is honestly recorded as unused.
    """
    rows, width = standardized.shape
    if rows != labels.shape[0]:
        raise V4TrainingError(f"{rows} feature row(s) but {labels.shape[0]} label(s).")
    if float(l2) <= 0.0:
        raise V4TrainingError(
            f"l2 must be positive, got {l2}. An unpenalised fit on a separable fold "
            "sends its coefficients to infinity and reports perfect confidence."
        )

    design = np.column_stack([np.ones(rows, dtype="float64"), standardized])
    weights = np.zeros(width + 1, dtype="float64")
    penalty = np.full(width + 1, float(l2), dtype="float64")
    penalty[0] = 0.0

    for _ in range(int(max_iterations)):
        odds = design @ weights
        probabilities = 1.0 / (1.0 + np.exp(-np.clip(odds, -500.0, 500.0)))
        variance = np.clip(probabilities * (1.0 - probabilities), 1e-10, None)
        gradient = design.T @ (probabilities - labels) + penalty * weights
        hessian = design.T @ (design * variance[:, None]) + np.diag(penalty)
        # A ridge on the diagonal keeps the solve well posed even when a fold
        # holds a feature that never varies; without it a singular Hessian is a
        # LinAlgError in the middle of a walk-forward run.
        hessian = hessian + np.eye(width + 1) * 1e-10
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError as error:
            raise V4TrainingError(f"The logistic fit could not be solved: {error}") from error
        weights = weights - step
        if float(np.max(np.abs(step))) < float(tolerance):
            break

    return LogisticEstimator(
        intercept=float(weights[0]),
        coefficients=tuple(float(value) for value in weights[1:]),
    )


def _leaf(value: float) -> dict[str, object]:
    """One leaf, in the flat-array node form `DecisionTree` is built from."""
    return {"feature": LEAF_FEATURE, "threshold": 0.0, "left": 0, "right": 0, "value": value}


def _best_split(
    standardized: np.ndarray,
    gradient: np.ndarray,
    hessian: np.ndarray,
    rows: np.ndarray,
    *,
    l2: float,
    min_samples_leaf: int,
    min_gain: float,
) -> tuple[int, float] | None:
    """The split that most reduces the boosting objective, or None if none does.

    The standard second-order criterion: a split is worth making when the sum
    of the two children's ``G^2 / (H + l2)`` exceeds the parent's by `min_gain`.

    Ties break towards the lowest feature index and then the lowest threshold,
    which is what makes the fitted ensemble a function of the data alone. A
    search that broke ties by whichever candidate numpy happened to visit first
    would still be deterministic on one machine and would not be reproducible
    from the experiment record, which is the property that matters.
    """
    total_gradient = float(gradient[rows].sum())
    total_hessian = float(hessian[rows].sum())
    parent = total_gradient * total_gradient / (total_hessian + l2)

    best_gain = float(min_gain)
    best: tuple[int, float] | None = None
    count = rows.shape[0]
    if count < 2 * min_samples_leaf:
        return None

    for feature in range(standardized.shape[1]):
        values = standardized[rows, feature]
        order = np.argsort(values, kind="stable")
        ordered = values[order]
        left_gradient = np.cumsum(gradient[rows][order])[:-1]
        left_hessian = np.cumsum(hessian[rows][order])[:-1]
        right_gradient = total_gradient - left_gradient
        right_hessian = total_hessian - left_hessian

        left_count = np.arange(1, count)
        usable = (
            (ordered[:-1] < ordered[1:])
            & (left_count >= min_samples_leaf)
            & ((count - left_count) >= min_samples_leaf)
        )
        if not bool(usable.any()):
            continue
        gains = (
            left_gradient * left_gradient / (left_hessian + l2)
            + right_gradient * right_gradient / (right_hessian + l2)
            - parent
        )
        gains = np.where(usable, gains, -np.inf)
        position = int(np.argmax(gains))
        gain = float(gains[position])
        if gain > best_gain:
            best_gain = gain
            best = (feature, float((ordered[position] + ordered[position + 1]) / 2.0))
    return best


def _grow_tree(
    standardized: np.ndarray,
    gradient: np.ndarray,
    hessian: np.ndarray,
    *,
    max_depth: int,
    l2: float,
    min_samples_leaf: int,
    min_gain: float,
    learning_rate: float,
) -> DecisionTree:
    """Grow one regression tree on the current gradients, depth-first.

    Nodes are appended in the order they are created and children always land at
    a higher index than their parent, which is the acyclicity `DecisionTree`
    checks on construction.
    """
    nodes: list[dict[str, object]] = []

    def leaf_value(rows: np.ndarray) -> float:
        total_gradient = float(gradient[rows].sum())
        total_hessian = float(hessian[rows].sum())
        return -learning_rate * total_gradient / (total_hessian + l2)

    def grow(rows: np.ndarray, depth: int) -> int:
        index = len(nodes)
        nodes.append(_leaf(leaf_value(rows)))
        if depth >= max_depth:
            return index
        split = _best_split(
            standardized,
            gradient,
            hessian,
            rows,
            l2=l2,
            min_samples_leaf=min_samples_leaf,
            min_gain=min_gain,
        )
        if split is None:
            return index
        feature, threshold = split
        going_left = standardized[rows, feature] <= threshold
        left = grow(rows[going_left], depth + 1)
        right = grow(rows[~going_left], depth + 1)
        nodes[index] = {
            "feature": feature,
            "threshold": threshold,
            "left": left,
            "right": right,
            "value": 0.0,
        }
        return index

    grow(np.arange(standardized.shape[0], dtype="int64"), 0)
    return DecisionTree(
        feature=tuple(int(node["feature"]) for node in nodes),
        threshold=tuple(float(node["threshold"]) for node in nodes),
        left=tuple(int(node["left"]) for node in nodes),
        right=tuple(int(node["right"]) for node in nodes),
        value=tuple(float(node["value"]) for node in nodes),
    )


def fit_gradient_boosted(
    standardized: np.ndarray,
    labels: np.ndarray,
    *,
    trees: int = 60,
    max_depth: int = 3,
    learning_rate: float = 0.05,
    l2: float = 1.0,
    min_samples_leaf: int = 40,
    min_gain: float = 1e-6,
) -> GradientBoostedEstimator:
    """Gradient-boosted regression trees on the logistic loss.

    Second-order boosting: each tree is fitted to the current gradient and
    Hessian of the log loss, and its leaves take the Newton step for the rows
    that land in them. The learning rate is folded into the leaf values here, so
    the serving side is a plain sum and shrinkage cannot be applied twice.

    Every source of randomness a boosted implementation usually offers - row
    subsampling, column subsampling, a shuffled split order - is absent rather
    than seeded. They buy variance reduction on large datasets and cost exact
    reproducibility, and on a few thousand rows the trade is not worth making.
    Depth is capped low and leaves are kept large for the same reason: this is
    a low signal-to-noise tabular problem, and an ensemble that can isolate
    forty bars has memorised a fortnight.
    """
    rows, width = standardized.shape
    if rows != labels.shape[0]:
        raise V4TrainingError(f"{rows} feature row(s) but {labels.shape[0]} label(s).")
    if int(trees) < 1:
        raise V4TrainingError(f"trees must be at least 1, got {trees}.")

    base_rate = float(np.clip(labels.mean(), LOG_LOSS_EPSILON, 1.0 - LOG_LOSS_EPSILON))
    base_score = float(np.log(base_rate / (1.0 - base_rate)))
    scores = np.full(rows, base_score, dtype="float64")

    grown: list[DecisionTree] = []
    for _ in range(int(trees)):
        probabilities = 1.0 / (1.0 + np.exp(-np.clip(scores, -500.0, 500.0)))
        gradient = probabilities - labels
        hessian = np.clip(probabilities * (1.0 - probabilities), 1e-10, None)
        tree = _grow_tree(
            standardized,
            gradient,
            hessian,
            max_depth=int(max_depth),
            l2=float(l2),
            min_samples_leaf=int(min_samples_leaf),
            min_gain=float(min_gain),
            learning_rate=float(learning_rate),
        )
        grown.append(tree)
        scores = scores + np.asarray(
            [tree.leaf_value(row) for row in standardized], dtype="float64"
        )

    return GradientBoostedEstimator(base_score=base_score, width=width, trees=tuple(grown))


def fit_class_frequency(labels: np.ndarray, *, width: int) -> ClassFrequencyEstimator:
    """The null baseline: the training set's own base rate."""
    if labels.shape[0] < 1:
        raise V4TrainingError("Cannot fit a base rate on an empty label array.")
    return ClassFrequencyEstimator(probability_up=float(labels.mean()), width=int(width))


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------


def fit_isotonic(scores: np.ndarray, outcomes: np.ndarray) -> IsotonicCalibration:
    """Isotonic regression by pool-adjacent-violators, as a step function.

    Scores are first aggregated by distinct value, so the fitted steps have
    strictly ascending thresholds by construction rather than by luck. PAV then
    merges adjacent groups until the observed frequencies are non-decreasing,
    which is the least-squares monotone fit and needs no solver - the whole of
    it is one pass with a stack.

    **The fitted values are held away from 0 and 1 by the sample's own
    resolution.** A block that happened to contain no positives has an observed
    frequency of exactly zero, and shipping that as a calibrated probability
    would be the model asserting impossibility on the evidence of a few dozen
    rows. The bound is ``1 / 2n``: with *n* validation rows a rate below one in
    *n* is indistinguishable from zero, so half of that is the smallest claim
    the sample can support. Clamping is applied pointwise and therefore cannot
    break the monotonicity PAV just established.

    No new dependency. Isotonic regression is usually reached for through a
    library, and `autotrader.ml.calibration` says as much; the algorithm itself
    is thirty lines, and thirty lines is cheaper than a dependency a trading
    process would then carry.
    """
    values = np.asarray(scores, dtype="float64").ravel()
    events = np.asarray(outcomes, dtype="float64").ravel()
    if values.size != events.size:
        raise V4TrainingError(f"{values.size} score(s) but {events.size} outcome(s).")
    if values.size == 0:
        raise V4TrainingError("Cannot fit a calibration on an empty validation split.")

    unique, inverse = np.unique(values, return_inverse=True)
    weights = np.bincount(inverse, minlength=unique.size).astype("float64")
    positives = np.bincount(inverse, weights=events, minlength=unique.size)

    # Each block is [weighted sum of outcomes, total weight]. Blocks are merged
    # from the right whenever the newest one would sit below its neighbour,
    # which is exactly the violation isotonic regression exists to remove.
    blocks: list[list[float]] = []
    for index in range(unique.size):
        blocks.append([float(positives[index]), float(weights[index])])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            merged = blocks.pop()
            blocks[-1][0] += merged[0]
            blocks[-1][1] += merged[1]

    bound = 1.0 / (2.0 * float(values.size))
    thresholds: list[float] = []
    fitted: list[float] = []
    position = 0
    for total_positives, total_weight in blocks:
        thresholds.append(float(unique[position]))
        observed = total_positives / total_weight
        fitted.append(float(min(max(observed, bound), 1.0 - bound)))
        consumed = 0.0
        while position < unique.size and consumed < total_weight - 1e-9:
            consumed += float(weights[position])
            position += 1

    return IsotonicCalibration(thresholds=tuple(thresholds), values=tuple(fitted))


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def log_loss(probabilities: np.ndarray, outcomes: np.ndarray) -> float:
    """Mean negative log likelihood: the metric model selection is decided on.

    Chosen over accuracy because it is a *proper* scoring rule - it is minimised
    only by reporting the true probability - so a model cannot improve it by
    being confidently wrong in the right proportion. Accuracy on a near-balanced
    target rewards a model for guessing the majority class and says nothing
    about whether its probabilities mean anything, which is the entire question
    V4 exists to answer.
    """
    scores = np.clip(
        np.asarray(probabilities, dtype="float64"), LOG_LOSS_EPSILON, 1.0 - LOG_LOSS_EPSILON
    )
    events = np.asarray(outcomes, dtype="float64")
    return float(-np.mean(events * np.log(scores) + (1.0 - events) * np.log(1.0 - scores)))


def roc_auc(probabilities: np.ndarray, outcomes: np.ndarray) -> float:
    """Rank-based area under the ROC curve, ties counted as half.

    The discrimination half of the picture, read next to calibration error
    rather than instead of it: a model can rank perfectly and be scaled
    hopelessly, and one that always predicts the base rate is perfectly
    calibrated and perfectly useless. Returns 0.5 - the uninformative value -
    when a fold happens to contain only one class, because AUC is undefined
    there and refusing the whole comparison over it would be worse.
    """
    scores = np.asarray(probabilities, dtype="float64")
    events = np.asarray(outcomes, dtype="float64")
    positives = float(events.sum())
    negatives = float(events.size - positives)
    if positives <= 0.0 or negatives <= 0.0:
        return 0.5
    order = np.argsort(scores, kind="stable")
    ranks = np.empty(scores.size, dtype="float64")
    ranks[order] = np.arange(1, scores.size + 1, dtype="float64")
    # Average the ranks within each group of tied scores, which is what makes a
    # tie count as half a correct ordering rather than as a whole one.
    unique, inverse, counts = np.unique(scores, return_inverse=True, return_counts=True)
    summed = np.bincount(inverse, weights=ranks, minlength=unique.size)
    ranks = (summed / counts)[inverse]
    positive_rank_sum = float(ranks[events > 0.0].sum())
    return float(
        (positive_rank_sum - positives * (positives + 1.0) / 2.0) / (positives * negatives)
    )


def evaluate_probabilities(probabilities: np.ndarray, outcomes: np.ndarray) -> dict[str, float]:
    """Every metric one split's predictions are judged by, in one mapping.

    Discrimination and calibration together, because either alone is
    misleading: log loss and Brier reward both at once, AUC isolates ranking,
    and the expected calibration error isolates whether a stated probability
    means what it says.
    """
    scores = np.asarray(probabilities, dtype="float64")
    events = np.asarray(outcomes, dtype="float64")
    return {
        "rows": float(scores.size),
        "base_rate": float(events.mean()) if events.size else float("nan"),
        "log_loss": log_loss(scores, events),
        "brier_score": brier_score(scores, events),
        "expected_calibration_error": expected_calibration_error(scores, events),
        "roc_auc": roc_auc(scores, events),
        "accuracy": float(np.mean((scores >= 0.5) == (events > 0.0))),
        "mean_predicted": float(scores.mean()),
    }


# --------------------------------------------------------------------------
# Candidates
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One model family and the hyperparameters it is to be fitted under."""

    name: str
    family: str
    hyperparameters: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.family not in SIMPLICITY_ORDER:
            raise V4TrainingError(
                f"Unknown family {self.family!r}. Known families: {', '.join(SIMPLICITY_ORDER)}."
            )
        object.__setattr__(self, "hyperparameters", dict(self.hyperparameters))

    @property
    def complexity_rank(self) -> int:
        """Where this family sits on the simplicity ordering."""
        return SIMPLICITY_ORDER.index(self.family)

    def to_record(self) -> dict[str, object]:
        return {
            "name": self.name,
            "family": self.family,
            "hyperparameters": dict(self.hyperparameters),
        }


def default_candidates() -> tuple[Candidate, ...]:
    """The three models V4 compares, in increasing order of complexity.

    A null baseline that has to be beaten, a regularised linear model, and a
    small boosted ensemble. Nothing heavier is on this list, and adding one
    would need walk-forward evidence in this repository that these three cannot
    produce - which is the rule the selection function enforces rather than
    merely recommends.
    """
    return (
        Candidate(name="baseline-frequency", family=FAMILY_CLASS_FREQUENCY),
        Candidate(
            name="logistic-l2",
            family=FAMILY_LOGISTIC,
            hyperparameters={"l2": 1.0, "max_iterations": 50, "tolerance": 1e-8},
        ),
        Candidate(
            name="gradient-boosted",
            family=FAMILY_GRADIENT_BOOSTED,
            hyperparameters={
                "trees": 60,
                "max_depth": 3,
                "learning_rate": 0.05,
                "l2": 1.0,
                "min_samples_leaf": 40,
                "min_gain": 1e-6,
            },
        ),
    )


def fit_estimator(candidate: Candidate, standardized: np.ndarray, labels: np.ndarray) -> Estimator:
    """Fit whichever family `candidate` names, on already-standardized rows."""
    settings = dict(candidate.hyperparameters)
    if candidate.family == FAMILY_CLASS_FREQUENCY:
        return fit_class_frequency(labels, width=standardized.shape[1])
    if candidate.family == FAMILY_LOGISTIC:
        return fit_logistic(standardized, labels, **settings)  # type: ignore[arg-type]
    return fit_gradient_boosted(standardized, labels, **settings)  # type: ignore[arg-type]


def _artifact_for(
    estimator: Estimator,
    standardizer: FeatureStandardizer,
    *,
    calibration: Calibration,
    label: LabelSpec,
    window: TrainingWindow,
    model_version: str,
    hyperparameters: Mapping[str, object],
    seed: int,
    trained_at: str = "",
    metrics: Mapping[str, float] | None = None,
    code_revision: Mapping[str, object] | None = None,
    notes: str = "",
) -> ProbabilityArtifact:
    """Assemble a decision-layer artifact from fitted parts."""
    return ProbabilityArtifact(
        model_version=model_version,
        feature_version=DECISION_FEATURE_VERSION,
        feature_columns=V4_FEATURE_COLUMNS,
        label_spec_id=label.identifier,
        standardizer=standardizer,
        estimator=estimator,
        calibration=calibration,
        training_window=window,
        trained_at_utc=trained_at,
        code_revision=dict(code_revision or {}),
        hyperparameters=dict(hyperparameters),
        metrics=dict(metrics or {}),
        seed=int(seed),
        notes=notes,
    )


def _window_for(frame: pd.DataFrame, *, symbol: str, asset_class: AssetClass) -> TrainingWindow:
    """The training window a fitted model saw."""
    return TrainingWindow(
        first_feature_timestamp=pd.Timestamp(frame["feature_timestamp"].iloc[0]).isoformat(),
        last_feature_timestamp=pd.Timestamp(frame["feature_timestamp"].iloc[-1]).isoformat(),
        rows=len(frame),
        symbols=(symbol,),
        asset_class=asset_class.value,
    )


def _score_through_decision_layer(artifact: ProbabilityArtifact, matrix: np.ndarray) -> np.ndarray:
    """Probabilities for a whole matrix, produced one row at a time by V4's own code.

    Deliberately not vectorized. This is the single scoring path the live engine
    uses, and routing every evaluated row through it is what makes the reported
    metrics metrics *of the shipped model* rather than of a training-time
    reimplementation that agrees with it today.
    """
    return np.asarray(
        [artifact.probability_up([float(value) for value in row]) for row in matrix],
        dtype="float64",
    )


# --------------------------------------------------------------------------
# Walk-forward comparison
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FoldResult:
    """One candidate's performance on one walk-forward fold."""

    fold: int
    train_rows: int
    test_rows: int
    metrics: Mapping[str, float]

    def to_record(self) -> dict[str, object]:
        return {
            "fold": self.fold,
            "train_rows": self.train_rows,
            "test_rows": self.test_rows,
            "metrics": {name: float(value) for name, value in self.metrics.items()},
        }


@dataclass(frozen=True)
class CandidateResult:
    """One candidate's walk-forward record, fold by fold and averaged."""

    candidate: Candidate
    folds: tuple[FoldResult, ...]

    @property
    def mean_metrics(self) -> Mapping[str, float]:
        """The unweighted mean of each metric across folds.

        Unweighted on purpose. Anchored walk-forward folds are of comparable
        length by construction, and weighting by row count would let the last
        fold - the one with the most training data behind it and the least to
        say about robustness - dominate the average.
        """
        if not self.folds:
            return {}
        names = sorted(self.folds[0].metrics)
        return {name: float(np.mean([fold.metrics[name] for fold in self.folds])) for name in names}

    @property
    def mean_log_loss(self) -> float:
        return float(self.mean_metrics.get("log_loss", float("inf")))

    def to_record(self) -> dict[str, object]:
        return {
            **self.candidate.to_record(),
            "folds": [fold.to_record() for fold in self.folds],
            "mean_metrics": dict(self.mean_metrics),
        }


@dataclass(frozen=True)
class ModelComparison:
    """The recorded evidence behind V4's model choice."""

    results: tuple[CandidateResult, ...]
    chosen: Candidate
    rationale: str
    fold_count: int
    embargo_bars: int
    initial_train_fraction: float
    seed: int
    label_spec_id: str
    feature_version: str
    symbol: str
    rows: int

    def result_for(self, name: str) -> CandidateResult:
        for result in self.results:
            if result.candidate.name == name:
                return result
        raise V4TrainingError(f"No candidate named {name!r} was compared.")

    def to_record(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "rows": self.rows,
            "feature_version": self.feature_version,
            "label_spec_id": self.label_spec_id,
            "walk_forward": {
                "folds": self.fold_count,
                "embargo_bars": self.embargo_bars,
                "initial_train_fraction": self.initial_train_fraction,
                "scheme": (
                    "anchored walk-forward: every fold trains on the past and is graded "
                    "on its own future, with purging on label_knowable_at and an embargo "
                    "in bars at each boundary. Rows stay in time order throughout."
                ),
            },
            "seed": self.seed,
            "material_log_loss_improvement": MATERIAL_LOG_LOSS_IMPROVEMENT,
            "candidates": [result.to_record() for result in self.results],
            "chosen": self.chosen.to_record(),
            "rationale": self.rationale,
        }


def compare_candidates(
    training: TrainingFrame,
    *,
    candidates: Sequence[Candidate] | None = None,
    folds: int = 4,
    initial_train_fraction: float = 0.5,
    embargo_bars: int = 0,
    seed: int = 0,
) -> ModelComparison:
    """Fit and grade every candidate on the same anchored walk-forward folds.

    Each fold trains on everything up to a moving boundary and is graded on the
    stretch that follows, with training rows whose labels resolve inside the
    test window purged and a further `embargo_bars` removed at the boundary.
    Every candidate sees the identical folds, so the comparison measures the
    models rather than the luck of a split.

    The standardizer is refitted inside every fold, from that fold's training
    rows alone. Fitting it once over the whole frame would be faster and would
    leak the test period's mean into the training features of every fold.
    """
    entries = tuple(candidates) if candidates is not None else default_candidates()
    if not entries:
        raise V4TrainingError("A comparison needs at least one candidate.")

    fold_specs = walk_forward_folds(
        training.frame,
        folds=folds,
        initial_train_fraction=initial_train_fraction,
        embargo_bars=embargo_bars,
    )
    if not fold_specs:
        raise V4TrainingError("The frame yielded no walk-forward folds.")

    results: list[CandidateResult] = []
    for candidate in entries:
        graded: list[FoldResult] = []
        for fold in fold_specs:
            train_frame = fold.train.frame
            test_frame = fold.test.frame
            if train_frame.empty or test_frame.empty:
                continue
            train_matrix = train_frame.loc[:, list(V4_FEATURE_COLUMNS)].to_numpy(dtype="float64")
            test_matrix = test_frame.loc[:, list(V4_FEATURE_COLUMNS)].to_numpy(dtype="float64")
            train_labels = _labels_of(train_frame)
            test_labels = _labels_of(test_frame)

            standardizer = fit_standardizer(train_matrix)
            standardized = np.asarray(
                [standardizer.apply([float(v) for v in row]) for row in train_matrix],
                dtype="float64",
            )
            estimator = fit_estimator(candidate, standardized, train_labels)
            artifact = _artifact_for(
                estimator,
                standardizer,
                calibration=IdentityCalibration(),
                label=training.label,
                window=_window_for(
                    train_frame, symbol=training.symbol, asset_class=training.asset_class
                ),
                model_version=f"{candidate.name}-fold{fold.index}",
                hyperparameters=candidate.hyperparameters,
                seed=seed,
            )
            probabilities = _score_through_decision_layer(artifact, test_matrix)
            graded.append(
                FoldResult(
                    fold=fold.index,
                    train_rows=len(train_frame),
                    test_rows=len(test_frame),
                    metrics=evaluate_probabilities(probabilities, test_labels),
                )
            )
        results.append(CandidateResult(candidate=candidate, folds=tuple(graded)))

    chosen, rationale = select_candidate(tuple(results))
    return ModelComparison(
        results=tuple(results),
        chosen=chosen,
        rationale=rationale,
        fold_count=len(fold_specs),
        embargo_bars=embargo_bars,
        initial_train_fraction=initial_train_fraction,
        seed=seed,
        label_spec_id=training.label.identifier,
        feature_version=DECISION_FEATURE_VERSION,
        symbol=training.symbol,
        rows=training.row_count,
    )


def select_candidate(results: Sequence[CandidateResult]) -> tuple[Candidate, str]:
    """Choose a model from the walk-forward evidence, and say why in one sentence.

    Three rules, applied in order, and all three are about refusing complexity
    that has not been paid for:

    1. A candidate must beat the class-frequency baseline's mean log loss by
       `MATERIAL_LOG_LOSS_IMPROVEMENT`. One that does not has found nothing, and
       shipping it would be shipping the base rate with extra steps.
    2. Among those that clear the bar, the best mean log loss sets the standard.
    3. Among those within `MATERIAL_LOG_LOSS_IMPROVEMENT` of that standard, the
       *simplest* family wins. A boosted ensemble that merely ties a linear
       model has offered nothing in exchange for being harder to reason about,
       slower to reproduce, and impossible to attribute per feature.

    When nothing clears the bar the baseline is returned, and the rationale says
    so. That is a real outcome and not a failure: a fair walk-forward on a
    market that offered no edge over this horizon should conclude exactly that,
    and a function that quietly promoted the least-bad candidate instead would
    be the mechanism by which noise reaches production.
    """
    if not results:
        raise V4TrainingError("Cannot select a candidate from an empty comparison.")
    ranked = {result.candidate.name: result for result in results}
    baseline = next(
        (r for r in results if r.candidate.family == FAMILY_CLASS_FREQUENCY),
        None,
    )
    if baseline is None:
        raise V4TrainingError(
            "A comparison must include a class-frequency baseline. Without a floor, "
            "'the best of these three' is not evidence that any of them found anything."
        )

    floor = baseline.mean_log_loss
    admissible = [
        result
        for result in results
        if result.candidate.family != FAMILY_CLASS_FREQUENCY
        and result.mean_log_loss <= floor - MATERIAL_LOG_LOSS_IMPROVEMENT
    ]
    if not admissible:
        return baseline.candidate, (
            f"No candidate improved on the class-frequency baseline's mean walk-forward "
            f"log loss of {floor:.6f} by the required {MATERIAL_LOG_LOSS_IMPROVEMENT}. "
            "The baseline is selected, which is the honest reading of this evidence: "
            "on these folds, this feature set and this horizon, no candidate found a "
            "usable edge."
        )

    best = min(result.mean_log_loss for result in admissible)
    tied = [
        result
        for result in admissible
        if result.mean_log_loss <= best + MATERIAL_LOG_LOSS_IMPROVEMENT
    ]
    winner = min(tied, key=lambda result: (result.candidate.complexity_rank, result.mean_log_loss))
    others = ", ".join(
        f"{name}={result.mean_log_loss:.6f}" for name, result in sorted(ranked.items())
    )
    return winner.candidate, (
        f"{winner.candidate.name} ({winner.candidate.family}) selected on mean walk-forward "
        f"log loss {winner.mean_log_loss:.6f} against a baseline of {floor:.6f}. It is the "
        f"simplest family within {MATERIAL_LOG_LOSS_IMPROVEMENT} of the best result "
        f"({best:.6f}). Full comparison: {others}."
    )


# --------------------------------------------------------------------------
# Training the shipped model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TrainedModel:
    """A fitted, calibrated V4 model and everything measured about it."""

    artifact: ProbabilityArtifact
    candidate: Candidate
    split: TemporalSplit
    validation_metrics: Mapping[str, float]
    test_metrics: Mapping[str, float]
    uncalibrated_validation_metrics: Mapping[str, float]

    def to_record(self) -> dict[str, object]:
        return {
            "artifact": self.artifact.to_record(),
            "candidate": self.candidate.to_record(),
            "split": self.split.to_record(),
            "validation_metrics": dict(self.validation_metrics),
            "uncalibrated_validation_metrics": dict(self.uncalibrated_validation_metrics),
            "test_metrics": dict(self.test_metrics),
        }


def train_model(
    training: TrainingFrame,
    candidate: Candidate,
    *,
    model_version: str,
    split: SplitSpec | None = None,
    seed: int = 0,
    calibrate: bool = True,
    code_revision: Mapping[str, object] | None = None,
    trained_at: datetime | None = None,
    notes: str = "",
) -> TrainedModel:
    """Fit, calibrate and evaluate one model over a three-way temporal split.

    The order is the whole point and is not negotiable: the estimator is fitted
    on the training rows, the calibration is fitted on the validation rows using
    the estimator's *uncalibrated* scores, and the test rows are touched exactly
    once, at the end, to produce the numbers the artifact records. Nothing is
    refitted afterwards - a model retrained on train-plus-validation after
    calibration would invalidate the calibration it just fitted, and one
    retrained on everything would have no honest metrics at all.
    """
    specification = split if split is not None else SplitSpec()
    parts = temporal_split(training.frame, specification)
    assert_no_leakage(parts)
    if parts.train.frame.empty or parts.validation.frame.empty or parts.test.frame.empty:
        raise V4TrainingError(
            "A three-way temporal split left an empty part. The frame is too short to "
            "train, calibrate and evaluate without reusing rows for two of the three."
        )

    train_matrix = parts.train.frame.loc[:, list(V4_FEATURE_COLUMNS)].to_numpy(dtype="float64")
    train_labels = _labels_of(parts.train.frame)
    standardizer = fit_standardizer(train_matrix)
    standardized = np.asarray(
        [standardizer.apply([float(value) for value in row]) for row in train_matrix],
        dtype="float64",
    )
    estimator = fit_estimator(candidate, standardized, train_labels)

    window = _window_for(
        parts.train.frame, symbol=training.symbol, asset_class=training.asset_class
    )
    stamp = (trained_at if trained_at is not None else now_utc()).isoformat()

    raw = _artifact_for(
        estimator,
        standardizer,
        calibration=IdentityCalibration(),
        label=training.label,
        window=window,
        model_version=model_version,
        hyperparameters=candidate.hyperparameters,
        seed=seed,
        trained_at=stamp,
        code_revision=code_revision,
        notes=notes,
    )

    validation_matrix = parts.validation.frame.loc[:, list(V4_FEATURE_COLUMNS)].to_numpy(
        dtype="float64"
    )
    validation_labels = _labels_of(parts.validation.frame)
    validation_scores = _score_through_decision_layer(raw, validation_matrix)
    uncalibrated_metrics = evaluate_probabilities(validation_scores, validation_labels)

    calibration: Calibration = (
        fit_isotonic(validation_scores, validation_labels) if calibrate else IdentityCalibration()
    )
    calibrated = _artifact_for(
        estimator,
        standardizer,
        calibration=calibration,
        label=training.label,
        window=window,
        model_version=model_version,
        hyperparameters=candidate.hyperparameters,
        seed=seed,
        trained_at=stamp,
        code_revision=code_revision,
        notes=notes,
    )

    validation_metrics = evaluate_probabilities(
        _score_through_decision_layer(calibrated, validation_matrix), validation_labels
    )
    test_matrix = parts.test.frame.loc[:, list(V4_FEATURE_COLUMNS)].to_numpy(dtype="float64")
    test_labels = _labels_of(parts.test.frame)
    test_metrics = evaluate_probabilities(
        _score_through_decision_layer(calibrated, test_matrix), test_labels
    )

    final = _artifact_for(
        estimator,
        standardizer,
        calibration=calibration,
        label=training.label,
        window=window,
        model_version=model_version,
        hyperparameters=candidate.hyperparameters,
        seed=seed,
        trained_at=stamp,
        code_revision=code_revision,
        notes=notes,
        metrics={f"test_{name}": value for name, value in test_metrics.items()},
    )
    return TrainedModel(
        artifact=final,
        candidate=candidate,
        split=parts,
        validation_metrics=validation_metrics,
        uncalibrated_validation_metrics=uncalibrated_metrics,
        test_metrics=test_metrics,
    )


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def write_comparison(
    comparison: ModelComparison, *, root: Path | None = None, stem: str | None = None
) -> Path:
    """Write a comparison report to the reports root.

    Named by the symbol, the label and the feature version rather than by a
    timestamp, so rerunning the same comparison overwrites its own record
    instead of accumulating near-duplicates that differ only in when they ran.
    """
    directory = ensure_directory(
        Path(root) if root is not None else report_root() / COMPARISONS_DIRECTORY
    )
    name = stem or (
        f"{comparison.symbol.replace('/', '_')}_{comparison.label_spec_id}"
        f"_fs{comparison.feature_version}"
    )
    return write_json(directory / f"{name}.json", comparison.to_record())


def experiment_for(
    training: TrainingFrame,
    trained: TrainedModel,
    *,
    name: str,
    dataset_fingerprint: str,
    split: SplitSpec | None = None,
    git: GitProvenance | None = None,
    notes: str = "",
) -> ExperimentMetadata:
    """The reproducibility record for one V4 training run."""
    return new_experiment(
        name=name,
        seed=trained.artifact.seed,
        dataset_fingerprints=(dataset_fingerprint,),
        schema=training.schema,
        label=training.label,
        split=split if split is not None else SplitSpec(),
        model_name=trained.candidate.name,
        model_version=trained.artifact.model_version,
        hyperparameters=dict(trained.candidate.hyperparameters),
        calibration=dict(trained.artifact.calibration.to_record()),
        git=git,
        notes=notes,
    ).with_metrics({f"test_{k}": v for k, v in trained.test_metrics.items()})


def register_model(
    trained: TrainedModel,
    training: TrainingFrame,
    *,
    experiment: ExperimentMetadata,
    dataset_fingerprint: str,
    registry: ModelRegistry,
    directory: Path,
    stage: ArtifactStage = ArtifactStage.EXPERIMENTAL,
    notes: str = "",
) -> RegisteredArtifact:
    """Write the artifact JSON and register it immutably.

    The artifact file is the model: a JSON record of coefficients or trees, the
    standardizer, the calibration and the provenance. `artifact_version` is the
    SHA-256 of those bytes, computed by the registry from the file rather than
    taken on trust, so an artifact's identity is a property of what it contains.

    The stage is `EXPERIMENTAL` unless a caller says otherwise, and there is no
    stage that makes a model trade. Turning V4 on is a deliberate change to a
    runtime, made somewhere else, by someone who has read the evidence.
    """
    ensure_directory(directory)
    path = write_json(
        directory / f"{trained.artifact.model_version}.json", trained.artifact.to_record()
    )
    metadata = ArtifactMetadata(
        model_name=trained.candidate.name,
        model_version=trained.artifact.model_version,
        artifact_version=sha256_of_file(path),
        artifact_filename=artifact_filename(trained.candidate.name, path),
        created_at_utc=now_utc(),
        asset_class=training.asset_class.value,
        symbols=(training.symbol,),
        timeframe="15m",
        feature_schema_version=DECISION_FEATURE_VERSION,
        feature_schema_fingerprint=training.schema.fingerprint,
        label_spec=training.label.to_record(),
        label_spec_id=training.label.identifier,
        dataset_fingerprint=dataset_fingerprint,
        experiment_id=experiment.experiment_id,
        split=trained.split.to_record(),
        hyperparameters=dict(trained.candidate.hyperparameters),
        calibration=dict(trained.artifact.calibration.to_record()),
        metrics={f"test_{name}": value for name, value in trained.test_metrics.items()},
        notes=notes,
    )
    return registry.register(metadata, path, stage=stage)


__all__ = [
    "COMPARISONS_DIRECTORY",
    "DEFAULT_HORIZON_BARS",
    "LOG_LOSS_EPSILON",
    "MATERIAL_LOG_LOSS_IMPROVEMENT",
    "SIMPLICITY_ORDER",
    "Candidate",
    "CandidateResult",
    "FoldResult",
    "ModelComparison",
    "TrainedModel",
    "TrainingFrame",
    "V4TrainingError",
    "build_training_frame",
    "compare_candidates",
    "default_candidates",
    "default_label_spec",
    "evaluate_probabilities",
    "experiment_for",
    "fit_class_frequency",
    "fit_estimator",
    "fit_gradient_boosted",
    "fit_isotonic",
    "fit_logistic",
    "fit_standardizer",
    "log_loss",
    "register_model",
    "roc_auc",
    "select_candidate",
    "train_model",
    "v4_feature_columns",
    "v4_schema",
    "write_comparison",
]
