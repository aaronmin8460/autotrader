"""M1: the configurable label framework, and the interval every label names.

A label is a claim about the future, and the only honest way to store one is
next to the exact interval it measured. Every row this module produces carries
`label_entry_timestamp`, `label_exit_timestamp` and `label_knowable_at`, so a
dataset never has to be trusted about what its target meant - it can be read.

**There is no single true label, and this module does not pretend otherwise.**
Horizon, entry and exit price columns, classification thresholds, whether a
threshold is absolute or scaled by trailing volatility, and whether a holding
period may cross an overnight gap are all specification, not code. What a
`LabelSpec` fixes is that whatever you chose is written down, fingerprinted,
and reproducible.

**One rule is not configurable: the entry is at least one bar after the feature
bar.** A decision made from bar *t* cannot be filled inside bar *t*, because
bar *t*'s open is already in the past by the time bar *t* closes. This is the
same rule the backtester enforces - signal on bar *t*, fill at bar *t+1* -
and it is docs/SPEC.md section 6F. `entry_offset_bars=0` is refused outright
rather than offered with a warning: a label that measures a return you could
not have captured produces a model that looks excellent and trades badly, and
that failure is completely silent.

**Forward reading happens here and nowhere else.** This is the one module in
the package that indexes bars later than `feature_timestamp`. It does so
explicitly, through position arithmetic on the grid, and every value it derives
is stamped with a `label_knowable_at` so the split module can keep the
resulting row out of any window it would contaminate.

**A horizon is counted in tradable bars.** Four bars from an equity 15:00 is
not 16:00 - that bar does not exist - it is the second bar of the next session,
and the row records that the holding period crossed a session gap.
`SessionPolicy.WITHIN_SESSION` refuses those rows entirely for a model that is
meant to be flat overnight. On a continuous crypto grid there is no gap to
cross, and asking to stay within a session is refused as meaningless rather
than silently satisfied.

**An incomplete horizon is never a label.** The last rows of any dataset have
no future to measure. They keep their features, get `label_valid=False`, and
carry no target. Filling them - with zero, with the last known return, with
anything - would be inventing outcomes for the most recent data, which is
exactly the data a fresh model is most likely to be judged on.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd

from autotrader.ml import MLError
from autotrader.ml.features import VOLATILITY_FEATURE
from autotrader.ml.grid import BarGrid
from autotrader.ml.schema import ColumnRole, ColumnSpec
from autotrader.ml.storage import sha256_of_record
from autotrader.runtime.schedule import BAR_INTERVAL

#: The earliest bar a position may be entered on, relative to the feature bar.
#:
#: One, always. Bar *t*'s open happened before bar *t* closed, so a decision
#: taken from bar *t*'s close can first be acted on at bar *t+1*. Configurable
#: upwards - a slower system might need two - and never downwards.
MINIMUM_ENTRY_OFFSET_BARS = 1

#: Price columns a label may enter or exit at.
#:
#: Open and close only. A label that exits at the bar's `high` measures a price
#: nobody could have chosen to take: knowing the high of a bar requires the bar
#: to be over. Path-dependent barrier labels are a legitimate future
#: `LabelKind` with their own intrabar rules, not a choice of price column.
TRADABLE_PRICE_COLUMNS: tuple[str, ...] = ("open", "close")

#: Ternary class values. Signed so that the sign of the class is the direction.
TERNARY_SELL = -1
TERNARY_HOLD = 0
TERNARY_BUY = 1

#: The stable, auditable meaning of each ternary class value.
TERNARY_CLASSES: dict[int, str] = {
    TERNARY_SELL: "SELL",
    TERNARY_HOLD: "HOLD",
    TERNARY_BUY: "BUY",
}

#: Binary class values for a direction label.
DIRECTION_DOWN = 0
DIRECTION_UP = 1

DIRECTION_CLASSES: dict[int, str] = {
    DIRECTION_DOWN: "DOWN_OR_FLAT",
    DIRECTION_UP: "UP",
}

#: The dtype of a classification label. Nullable, because a row whose horizon
#: runs off the end of the dataset has no class and must not be given one.
CLASS_DTYPE = "Int8"


class LabelError(MLError):
    """A label specification that cannot be satisfied, or an input it cannot label."""


class LabelKind(Enum):
    """What shape of target a specification produces.

    `FORWARD_RETURN` is the continuous quantity every other kind is derived
    from, and it is always stored alongside them, so a threshold can be
    re-examined without rebuilding the dataset.
    """

    FORWARD_RETURN = "forward_return"
    DIRECTION = "direction"
    TERNARY = "ternary"


class ThresholdMode(Enum):
    """How a classification threshold is measured.

    `ABSOLUTE` treats the threshold as a return fraction: 0.002 is twenty basis
    points, whatever the market is doing. `VOLATILITY` treats it as a multiple
    of the row's own trailing realized volatility, so the same specification
    means "a move worth noticing" in a calm week and in a violent one. The
    volatility it scales by is a *feature* - backward-looking by construction -
    which is what keeps a volatility-scaled threshold free of look-ahead.
    """

    ABSOLUTE = "absolute"
    VOLATILITY = "volatility"


class SessionPolicy(Enum):
    """Whether a holding period may cross a session gap.

    `SPAN_SESSIONS` allows it and records `label_spans_session_gap` on every
    row so the overnight cases stay identifiable. `WITHIN_SESSION` invalidates
    them, which is what a model intended to hold nothing overnight needs.
    """

    SPAN_SESSIONS = "span_sessions"
    WITHIN_SESSION = "within_session"


@dataclass(frozen=True)
class LabelSpec:
    """One fully specified target definition.

    Frozen and fingerprinted: two datasets built from equal specifications are
    labelled identically, and a changed field produces a different fingerprint
    rather than a quietly different dataset under the same name.
    """

    name: str
    kind: LabelKind
    horizon_bars: int
    entry_offset_bars: int = MINIMUM_ENTRY_OFFSET_BARS
    entry_price_column: str = "open"
    exit_price_column: str = "open"
    threshold_mode: ThresholdMode = ThresholdMode.ABSOLUTE
    upper_threshold: float = 0.0
    lower_threshold: float = 0.0
    volatility_column: str = VOLATILITY_FEATURE
    session_policy: SessionPolicy = SessionPolicy.SPAN_SESSIONS

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise LabelError("A label specification needs a non-empty name.")
        for field, value in (
            ("kind", self.kind),
            ("threshold_mode", self.threshold_mode),
            ("session_policy", self.session_policy),
        ):
            if not isinstance(value, LabelKind | ThresholdMode | SessionPolicy):
                raise LabelError(f"{field} must be an enum member, got {value!r}.")
        if isinstance(self.horizon_bars, bool) or not isinstance(self.horizon_bars, int):
            raise LabelError(
                f"horizon_bars must be an int, got {type(self.horizon_bars).__name__}."
            )
        if self.horizon_bars < 1:
            raise LabelError(
                f"horizon_bars must be at least 1, got {self.horizon_bars}. A "
                "zero-bar horizon measures the interval between a price and itself."
            )
        if isinstance(self.entry_offset_bars, bool) or not isinstance(self.entry_offset_bars, int):
            raise LabelError("entry_offset_bars must be an int.")
        if self.entry_offset_bars < MINIMUM_ENTRY_OFFSET_BARS:
            raise LabelError(
                f"entry_offset_bars must be at least {MINIMUM_ENTRY_OFFSET_BARS}, got "
                f"{self.entry_offset_bars}. A decision taken once bar t closed cannot "
                "be filled inside bar t, because bar t's prices are already in the "
                "past. See docs/SPEC.md section 6F."
            )
        for field, column in (
            ("entry_price_column", self.entry_price_column),
            ("exit_price_column", self.exit_price_column),
        ):
            if column not in TRADABLE_PRICE_COLUMNS:
                raise LabelError(
                    f"{field} must be one of {', '.join(TRADABLE_PRICE_COLUMNS)}, got "
                    f"{column!r}. A bar's high or low is only knowable once the bar "
                    "is over, so neither is a price a decision could have taken."
                )
        for field, value in (
            ("upper_threshold", self.upper_threshold),
            ("lower_threshold", self.lower_threshold),
        ):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise LabelError(f"{field} must be a number, got {type(value).__name__}.")
            if not np.isfinite(float(value)):
                raise LabelError(f"{field} must be finite, got {value!r}.")
        if self.kind is LabelKind.FORWARD_RETURN and (
            self.threshold_mode is not ThresholdMode.ABSOLUTE
            or float(self.upper_threshold) != 0.0
            or float(self.lower_threshold) != 0.0
        ):
            raise LabelError(
                "A forward-return label is a continuous target and applies no "
                "threshold. Leave the thresholds at zero, or choose the "
                "direction or ternary kind."
            )
        if self.kind is LabelKind.TERNARY and float(self.upper_threshold) <= float(
            self.lower_threshold
        ):
            raise LabelError(
                f"A ternary label needs upper_threshold ({self.upper_threshold}) strictly "
                f"above lower_threshold ({self.lower_threshold}); otherwise BUY and SELL "
                "overlap and HOLD is empty."
            )
        if self.kind is LabelKind.DIRECTION and float(self.lower_threshold) != 0.0:
            raise LabelError(
                "A direction label splits on one boundary, upper_threshold. Leave "
                "lower_threshold at zero, or choose the ternary kind."
            )
        if self.threshold_mode is ThresholdMode.VOLATILITY:
            if self.kind is LabelKind.FORWARD_RETURN:
                raise LabelError("Volatility scaling applies to a threshold; there is none here.")
            if not isinstance(self.volatility_column, str) or not self.volatility_column.strip():
                raise LabelError("Volatility scaling needs the name of a volatility feature.")

    @property
    def exit_offset_bars(self) -> int:
        """How many bars after the feature bar the position is closed."""
        return self.entry_offset_bars + self.horizon_bars

    @property
    def label_dtype(self) -> str:
        """The dtype of the `label` column this specification produces."""
        return "float64" if self.kind is LabelKind.FORWARD_RETURN else CLASS_DTYPE

    @property
    def classes(self) -> dict[int, str] | None:
        """The class-value meanings, or None for a continuous target."""
        if self.kind is LabelKind.TERNARY:
            return dict(TERNARY_CLASSES)
        if self.kind is LabelKind.DIRECTION:
            return dict(DIRECTION_CLASSES)
        return None

    def to_record(self) -> dict[str, object]:
        """The serializable, fingerprinted form. Every field participates."""
        return {
            "name": self.name,
            "kind": self.kind.value,
            "horizon_bars": self.horizon_bars,
            "entry_offset_bars": self.entry_offset_bars,
            "entry_price_column": self.entry_price_column,
            "exit_price_column": self.exit_price_column,
            "threshold_mode": self.threshold_mode.value,
            "upper_threshold": float(self.upper_threshold),
            "lower_threshold": float(self.lower_threshold),
            "volatility_column": self.volatility_column,
            "session_policy": self.session_policy.value,
        }

    @property
    def fingerprint(self) -> str:
        """SHA-256 over the specification. Equal fingerprints, equal labels."""
        return sha256_of_record(self.to_record())

    @property
    def identifier(self) -> str:
        """A short, stable identifier: the name plus the first of the fingerprint.

        The name alone is an operator's word and can be reused for two
        different definitions; the fingerprint alone is unreadable. Together
        they name a target in a filename and in a model artifact without either
        failure mode.
        """
        return f"{self.name}-{self.fingerprint[:12]}"

    def describe(self) -> str:
        """Exactly which future interval this label measures, in one sentence.

        Stored verbatim in every dataset's metadata sidecar, because the point
        of a configurable label framework is that a reader six months later can
        find out what the target was without reading this file.
        """
        entry = f"the {self.entry_price_column.upper()} of the bar {self.entry_offset_bars}"
        exit_bar = f"the {self.exit_price_column.upper()} of the bar {self.exit_offset_bars}"
        interval = (
            f"Forward return from {entry} grid bar(s) after the feature bar to "
            f"{exit_bar} grid bar(s) after it, a holding period of "
            f"{self.horizon_bars} bar(s)."
        )
        if self.kind is LabelKind.FORWARD_RETURN:
            target = "The label is that return, unthresholded."
        elif self.kind is LabelKind.DIRECTION:
            target = (
                f"The label is 1 (UP) when that return exceeds "
                f"{self._threshold_phrase(self.upper_threshold)}, else 0."
            )
        else:
            target = (
                f"The label is +1 (BUY) above {self._threshold_phrase(self.upper_threshold)}, "
                f"-1 (SELL) below {self._threshold_phrase(self.lower_threshold)}, "
                "and 0 (HOLD) between them."
            )
        if self.session_policy is SessionPolicy.WITHIN_SESSION:
            sessions = "A holding period that would cross a session gap is not labelled at all."
        else:
            sessions = (
                "A holding period may cross a session gap; label_spans_session_gap "
                "marks the rows where it does."
            )
        knowable = (
            "The label becomes knowable one bar interval after label_exit_timestamp, "
            "when the exit bar has closed; label_knowable_at records that instant."
        )
        return f"{interval} {target} {sessions} {knowable}"

    def _threshold_phrase(self, value: float) -> str:
        if self.threshold_mode is ThresholdMode.VOLATILITY:
            return f"{value:g} x {self.volatility_column}"
        return f"{value:g}"


def label_columns(spec: LabelSpec) -> tuple[ColumnSpec, ...]:
    """The label and label-metadata columns `spec` contributes to a schema.

    Each declares `forward_bars`, which is what makes the schema's own
    look-ahead check meaningful: these are the only columns in a dataset
    allowed to read a bar later than `feature_timestamp`, and they say how much
    later.
    """
    forward = spec.exit_offset_bars
    return (
        ColumnSpec(
            name="label_entry_timestamp",
            dtype="datetime64[ns, UTC]",
            role=ColumnRole.LABEL_META,
            description=(
                f"Start of the bar the position is entered on: {spec.entry_offset_bars} "
                f"grid bar(s) after the feature bar. Entry price is its "
                f"{spec.entry_price_column}."
            ),
            forward_bars=spec.entry_offset_bars,
        ),
        ColumnSpec(
            name="label_exit_timestamp",
            dtype="datetime64[ns, UTC]",
            role=ColumnRole.LABEL_META,
            description=(
                f"Start of the bar the position is closed on: {forward} grid bar(s) "
                f"after the feature bar. Exit price is its {spec.exit_price_column}."
            ),
            forward_bars=forward,
        ),
        ColumnSpec(
            name="label_knowable_at",
            dtype="datetime64[ns, UTC]",
            role=ColumnRole.LABEL_META,
            description=(
                "label_exit_timestamp + 15m: the first instant this label was a fact. "
                "The split module purges on this column, so no training row can "
                "resolve inside a later window."
            ),
            forward_bars=forward,
        ),
        ColumnSpec(
            name="label_spans_session_gap",
            dtype="boolean",
            role=ColumnRole.LABEL_META,
            description=(
                "Whether the feature bar and the exit bar belong to different "
                "sessions, so the position was held across a market closure. "
                "Always False on a continuous crypto grid."
            ),
            forward_bars=forward,
        ),
        ColumnSpec(
            name="label_forward_return",
            dtype="float64",
            role=ColumnRole.LABEL_META,
            description=(
                "The raw forward return of the interval, stored whatever the label "
                "kind, so a threshold can be re-examined without rebuilding."
            ),
            forward_bars=forward,
        ),
        ColumnSpec(
            name="label_valid",
            dtype="boolean",
            role=ColumnRole.LABEL_META,
            description=(
                "Whether this row has a usable target. False when the horizon runs "
                "past the end of the grid, when the entry or exit bar was never "
                "published, when a price is unusable, when a volatility-scaled "
                "threshold has no volatility yet, or when the session policy "
                "excludes the interval."
            ),
            forward_bars=forward,
        ),
        ColumnSpec(
            name="label",
            dtype=spec.label_dtype,
            role=ColumnRole.LABEL,
            description=f"{spec.name}: {spec.describe()}",
            forward_bars=forward,
        ),
    )


def _forward_positions(count: int, offset: int) -> tuple[np.ndarray, np.ndarray]:
    """Positions `offset` bars ahead, and a mask of which of them exist.

    Out-of-range positions are clamped to zero so the gather below is always
    in bounds; the mask is what decides whether the gathered value is used.
    Clamping rather than filtering keeps every array the same length as the
    grid, so a row's position never shifts.
    """
    positions = np.arange(count) + offset
    valid = positions < count
    return np.where(valid, positions, 0), valid


def compute_labels(
    observations: pd.DataFrame,
    grid: BarGrid,
    spec: LabelSpec,
    *,
    volatility: pd.Series | None = None,
) -> pd.DataFrame:
    """Label every row of `observations` according to `spec`.

    `observations` is the bar frame reindexed onto `grid`, so position *i* is
    grid bar *i* whether or not the provider published it. `volatility` is
    required when the specification scales its thresholds, and must be a
    backward-looking feature column - the caller passes one from
    `autotrader.ml.features`, which is the only kind it produces.

    Returns a frame with exactly the columns `label_columns(spec)` names, in
    that order. Rows without a complete, usable horizon carry `label_valid=False`
    and a null label; nothing is imputed.
    """
    if not isinstance(spec, LabelSpec):
        raise LabelError(f"spec must be a LabelSpec, got {type(spec).__name__}.")
    count = len(observations)
    if count != len(grid):
        raise LabelError(
            f"Observations hold {count} rows but the grid holds {len(grid)} bars. "
            "Labels are positional, so the two must describe the same bars."
        )
    if spec.session_policy is SessionPolicy.WITHIN_SESSION and not grid.has_session_gaps:
        raise LabelError(
            "SessionPolicy.WITHIN_SESSION was requested on a continuous "
            f"{grid.asset_class.value} grid, which has no session to stay within. "
            "Crypto trades 24/7; the policy would silently accept every interval "
            "rather than constrain any of them."
        )

    frame = observations.reset_index(drop=True)
    entry_positions, entry_exists = _forward_positions(count, spec.entry_offset_bars)
    exit_positions, exit_exists = _forward_positions(count, spec.exit_offset_bars)
    horizon_exists = entry_exists & exit_exists

    present = frame["is_present"].to_numpy(dtype=bool)
    entry_price = frame[spec.entry_price_column].to_numpy(dtype="float64")[entry_positions]
    exit_price = frame[spec.exit_price_column].to_numpy(dtype="float64")[exit_positions]

    usable_prices = (
        np.isfinite(entry_price)
        & np.isfinite(exit_price)
        & (entry_price > 0.0)
        & present[entry_positions]
        & present[exit_positions]
    )

    session_ids = np.asarray(grid.session_ids, dtype=object)
    spans_gap = (
        (session_ids != session_ids[exit_positions]) & horizon_exists
        if grid.has_session_gaps
        else np.zeros(count, dtype=bool)
    )

    # The interval is usable when it exists, was published at both ends, and
    # the session policy permits it. Whether the *threshold* is known is a
    # separate question, asked below: the return is a fact of the market and is
    # kept even on a row whose classification could not be decided.
    interval_valid = horizon_exists & usable_prices
    if spec.session_policy is SessionPolicy.WITHIN_SESSION:
        interval_valid = interval_valid & ~spans_gap

    with np.errstate(divide="ignore", invalid="ignore"):
        forward_return = np.where(
            interval_valid, exit_price / np.where(interval_valid, entry_price, 1.0) - 1.0, np.nan
        )

    timestamps = frame["timestamp"]
    entry_timestamp = timestamps.iloc[entry_positions].reset_index(drop=True).where(horizon_exists)
    exit_timestamp = timestamps.iloc[exit_positions].reset_index(drop=True).where(horizon_exists)

    upper, lower, threshold_known = _thresholds(spec, count, volatility)
    valid = interval_valid & threshold_known

    labelled = pd.DataFrame(
        {
            "label_entry_timestamp": entry_timestamp,
            "label_exit_timestamp": exit_timestamp,
            "label_knowable_at": exit_timestamp + BAR_INTERVAL,
            "label_spans_session_gap": pd.array(spans_gap, dtype="boolean"),
            "label_forward_return": forward_return,
            "label_valid": pd.array(valid, dtype="boolean"),
            "label": _target(spec, forward_return, upper, lower, valid),
        }
    )
    return labelled[[column.name for column in label_columns(spec)]]


def _thresholds(
    spec: LabelSpec, count: int, volatility: pd.Series | None
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The per-row upper and lower thresholds, and where they are known.

    An absolute threshold is the same number on every row and is always known.
    A volatility-scaled one is unknown wherever the volatility feature has not
    warmed up, and those rows are not labelled: a threshold of zero times a
    missing volatility would classify every tiny move as a signal.
    """
    if spec.threshold_mode is ThresholdMode.ABSOLUTE:
        upper = np.full(count, float(spec.upper_threshold))
        lower = np.full(count, float(spec.lower_threshold))
        return upper, lower, np.ones(count, dtype=bool)
    if volatility is None:
        raise LabelError(
            f"{spec.name!r} scales its thresholds by {spec.volatility_column!r}, but no "
            "volatility column was supplied."
        )
    if len(volatility) != count:
        raise LabelError(
            f"The volatility column holds {len(volatility)} rows but the grid holds {count} bars."
        )
    sigma = volatility.to_numpy(dtype="float64")
    known = np.isfinite(sigma)
    safe = np.where(known, sigma, 0.0)
    return float(spec.upper_threshold) * safe, float(spec.lower_threshold) * safe, known


def _target(
    spec: LabelSpec,
    forward_return: np.ndarray,
    upper: np.ndarray,
    lower: np.ndarray,
    valid: np.ndarray,
) -> pd.Series:
    """Turn the forward return into the specification's target column."""
    if spec.kind is LabelKind.FORWARD_RETURN:
        return pd.Series(np.where(valid, forward_return, np.nan), dtype="float64")
    if spec.kind is LabelKind.DIRECTION:
        classes = np.where(forward_return > upper, DIRECTION_UP, DIRECTION_DOWN)
    else:
        classes = np.full(len(forward_return), TERNARY_HOLD, dtype="int8")
        classes = np.where(forward_return > upper, TERNARY_BUY, classes)
        classes = np.where(forward_return < lower, TERNARY_SELL, classes)
    return pd.Series(pd.array(np.where(valid, classes, 0), dtype=CLASS_DTYPE)).mask(~valid)


__all__ = [
    "CLASS_DTYPE",
    "DIRECTION_CLASSES",
    "DIRECTION_DOWN",
    "DIRECTION_UP",
    "MINIMUM_ENTRY_OFFSET_BARS",
    "TERNARY_BUY",
    "TERNARY_CLASSES",
    "TERNARY_HOLD",
    "TERNARY_SELL",
    "TRADABLE_PRICE_COLUMNS",
    "LabelError",
    "LabelKind",
    "LabelSpec",
    "SessionPolicy",
    "ThresholdMode",
    "compute_labels",
    "label_columns",
]
