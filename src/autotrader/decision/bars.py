"""The bar input contract every decision engine and timeframe utility shares.

One place that answers "are these bars something a decision may be made from?"
so that V2, V3, and the timeframe aggregator cannot drift apart on the answer.

**Validation here rejects; it never repairs.** Unsorted input is refused rather
than sorted, a duplicate timestamp is refused rather than deduplicated, and a
multi-symbol frame is refused rather than split. Every one of those repairs
would hide an upstream data-contract violation behind a decision that looked
fine, and full data-quality validation is C2's job (`autotrader.data.validation`)
rather than something to reimplement per consumer.

**Two checks here are stricter than C3's.** The crossover strategy tolerated
duplicate timestamps and said so, because a duplicate could not make a
crossover look like something it was not. Both are refused here: this package
groups bars into higher-timeframe buckets by counting them, and a duplicated
bar makes a bucket look complete when it is not, which is precisely a
higher-timeframe bar fabricated from data that does not exist.

**Timestamps must be UTC-aware and on the base grid.** A naive timestamp has no
offset to convert from, and a bar that does not land on a base-interval
boundary cannot be assigned to a higher-timeframe bucket without guessing.
Both are refused rather than assumed.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd

from autotrader.decision.contract import DecisionInputError
from autotrader.runtime.schedule import BAR_INTERVAL

#: The columns a decision engine reads. A subset of C1's canonical schema: the
#: decision layer never needs the filesystem or provider metadata around it.
REQUIRED_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
)

#: Columns aggregated into higher timeframes when present. Optional because a
#: research fixture may legitimately carry only OHLCV, and refusing it would
#: make the aggregator unusable for the branch that needs it most.
OPTIONAL_COLUMNS: tuple[str, ...] = ("trade_count", "vwap")

_NUMERIC_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")


def require_columns(bars: pd.DataFrame) -> None:
    """Reject input missing a column the decision layer reads."""
    if not isinstance(bars, pd.DataFrame):
        raise DecisionInputError(f"bars must be a DataFrame, got {type(bars).__name__}.")
    missing = [column for column in REQUIRED_COLUMNS if column not in bars.columns]
    if missing:
        raise DecisionInputError(
            f"Bars are missing required column(s): {', '.join(missing)}. "
            f"A decision engine requires: {', '.join(REQUIRED_COLUMNS)}."
        )


def require_single_symbol(bars: pd.DataFrame) -> str:
    """Return the one symbol in `bars`, rejecting a mixed frame."""
    symbols = pd.unique(bars["symbol"])
    if len(symbols) != 1:
        found = ", ".join(sorted(str(symbol) for symbol in symbols))
        raise DecisionInputError(
            f"Bars must contain exactly one symbol, found {len(symbols)}: {found}. "
            "A decision engine evaluates one symbol at a time."
        )
    return str(symbols[0])


def require_utc_index(bars: pd.DataFrame) -> pd.Series:
    """Return the timestamp column as UTC, refusing naive or non-datetime input."""
    timestamps = bars["timestamp"]
    if not pd.api.types.is_datetime64_any_dtype(timestamps):
        raise DecisionInputError(
            "Bars must carry datetime timestamps; a string or integer timestamp column "
            "cannot be placed on a bar grid without guessing its unit."
        )
    if timestamps.dt.tz is None:
        raise DecisionInputError(
            "Bar timestamps must be timezone-aware; naive timestamps would be read as UTC "
            "and silently misdate every bar boundary."
        )
    return timestamps.dt.tz_convert("UTC")


def require_strictly_ascending(timestamps: pd.Series) -> None:
    """Reject unsorted or duplicated timestamps rather than fixing either.

    Strict, unlike C3. A duplicate bar is not merely untidy here: higher
    timeframe buckets are judged complete by counting their constituents, so a
    repeated 15-minute bar can make an hour look fully observed when a quarter
    of it is missing. That is a fabricated bar, and it is refused at the door.
    """
    if not timestamps.is_monotonic_increasing:
        raise DecisionInputError(
            "Bars must be ordered ascending by timestamp. A decision engine does not sort "
            "its input, because silently reordering would mask an upstream data-contract "
            "violation."
        )
    if timestamps.duplicated().any():
        duplicated = timestamps[timestamps.duplicated()].iloc[0]
        raise DecisionInputError(
            f"Bars must not repeat a timestamp; {duplicated} appears more than once. "
            "A repeated bar makes a higher-timeframe bucket look complete when it is not."
        )


def require_base_grid(timestamps: pd.Series, base_interval: timedelta = BAR_INTERVAL) -> None:
    """Reject a bar that does not start on a base-interval boundary.

    Bar timestamps are interval *starts* everywhere in this system, and the
    grid is anchored to the UTC epoch. A bar stamped 10:07 belongs to no
    15-minute bucket, and placing it in one would be a guess about data the
    provider did not send.
    """
    step = pd.Timedelta(base_interval)
    if step <= pd.Timedelta(0):
        raise DecisionInputError(f"base_interval must be positive, got {base_interval}.")
    off_grid = timestamps != floor_to_interval(timestamps, step)
    if bool(off_grid.any()):
        offender = timestamps[off_grid].iloc[0]
        raise DecisionInputError(
            f"Bar timestamps must start on a {base_interval} boundary anchored to the UTC "
            f"epoch; {offender} does not. Bar timestamps are interval starts."
        )


def floor_to_interval(timestamps: pd.Series, interval: pd.Timedelta) -> pd.Series:
    """Floor UTC `timestamps` down to the `interval` grid anchored at the epoch.

    Delegated to pandas rather than done in integer arithmetic on purpose: the
    resolution a datetime column is stored at is a pandas implementation detail
    that has changed across major versions, and a hand-rolled nanosecond modulo
    silently produces the wrong bucket the moment the column is microsecond
    backed. `dt.floor` is anchored to the epoch, which is what makes a bucket
    boundary the same instant regardless of where the supplied window starts.
    """
    return timestamps.dt.floor(interval)


def require_finite_prices(bars: pd.DataFrame) -> None:
    """Reject a NaN or infinite price or volume.

    A NaN close propagates through every moving average that touches it and
    turns into a HOLD many bars later, at which point the cause is a data
    problem several hundred bars back. It is cheaper to refuse it here.
    """
    for column in _NUMERIC_COLUMNS:
        values = pd.to_numeric(bars[column], errors="coerce")
        finite = values.notna() & (values.abs() != float("inf"))
        if not bool(finite.all()):
            offending = bars.loc[~finite, "timestamp"].iloc[0]
            raise DecisionInputError(
                f"Column {column!r} must be finite on every bar; the bar at {offending} "
                "is NaN or infinite. A non-finite price silently poisons every indicator "
                "that averages over it."
            )


def normalize_bars(
    bars: pd.DataFrame,
    *,
    base_interval: timedelta = BAR_INTERVAL,
) -> pd.DataFrame:
    """Validate `bars` and return a UTC-normalized copy with a fresh index.

    The supplied frame is never modified. The returned frame carries the same
    columns with `timestamp` converted to UTC and the index reset to
    ``0..n-1``, so that positional access in the feature layer means what it
    reads like.
    """
    require_columns(bars)
    if bars.empty:
        raise DecisionInputError(
            "Bars must not be empty: a decision is made on a specific completed bar, and "
            "an empty frame names none."
        )
    require_single_symbol(bars)
    timestamps = require_utc_index(bars)
    require_strictly_ascending(timestamps)
    require_base_grid(timestamps, base_interval)
    normalized = bars.copy().reset_index(drop=True)
    normalized["timestamp"] = timestamps.reset_index(drop=True)
    require_finite_prices(normalized)
    return normalized


__all__ = [
    "OPTIONAL_COLUMNS",
    "REQUIRED_COLUMNS",
    "floor_to_interval",
    "normalize_bars",
    "require_base_grid",
    "require_columns",
    "require_finite_prices",
    "require_single_symbol",
    "require_strictly_ascending",
    "require_utc_index",
]
