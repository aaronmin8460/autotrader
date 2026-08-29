"""Timeframe specs, higher-timeframe aggregation, and the alignment rule.

V3 needs 1-hour and 4-hour bars. It **derives** them from the 15-minute bars
the system already fetches rather than asking the provider for them, and that
is a deliberate choice with three consequences worth stating.

*It costs no extra provider call.* One bounded 15-minute window per symbol per
cycle already exists and is already budgeted (C9, and the shared API budget in
CI1). Three requests per symbol instead of one would triple that cost to
re-obtain data the system is holding.

*It cannot disagree with itself.* A provider's own 1-hour candle and the four
15-minute candles inside it are computed by different code on different data
paths and do occasionally differ. Derived bars are the same bars, so a 1-hour
close is by construction the close of the last 15-minute bar inside the hour.

*Completeness becomes checkable rather than trusted.* A derived bucket is
complete when every one of its constituent bars is present. That is a count
this module performs, not a promise a provider makes.

**Buckets are anchored to the UTC epoch, never to the supplied window.** The
bucket a bar belongs to is a property of the bar's timestamp alone, so the same
bar lands in the same bucket whether the caller passed 200 bars or 2000. An
aggregator anchored to "the first bar I was given" produces different 4-hour
candles for different lookbacks, which makes a replay disagree with the live
decision it is replaying.

**A partly-observed bucket is dropped, never emitted short.** A bucket is kept
only when it holds exactly `interval / base_interval` constituent bars. This
single rule is what keeps equities honest without this module knowing anything
about market sessions: a 4-hour UTC bucket straddling the close holds a handful
of regular-session bars and nothing else, never reaches its full count, and is
discarded - so no candle is ever fabricated across an overnight gap, a weekend,
or a holiday. For a US regular session it leaves exactly six complete 1-hour
buckets and exactly one complete 4-hour bucket per session, and for continuous
crypto it leaves every bucket. Neither case required a calendar.

**The alignment rule is one inequality.** The base bar starting at ``T`` closes
at ``T + base_interval``. A higher-timeframe bar starting at ``B`` closes at
``B + interval``. The second is knowable at the first only when::

    B + interval <= T + base_interval

Anything else reads a candle that is still forming - the exact look-ahead
docs/SPEC.md section 7F forbids - and no amount of "it is nearly closed" makes
it available.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd
from pandas.api.typing import DataFrameGroupBy

from autotrader.decision.bars import (
    OPTIONAL_COLUMNS,
    REQUIRED_COLUMNS,
    floor_to_interval,
    normalize_bars,
)
from autotrader.decision.contract import DecisionConfigError, DecisionInputError
from autotrader.runtime.schedule import BAR_INTERVAL

#: The internal column the bucket assignment is carried on. Named so it cannot
#: collide with a canonical column, and dropped before anything is returned.
_BUCKET_COLUMN = "__bucket_start"


@dataclass(frozen=True)
class TimeframeSpec:
    """One timeframe: a stable label and the interval one of its bars covers.

    `label` is part of the audit record - it appears in reason tokens, in
    policy metadata, and as a key in V3's per-timeframe feature map - so it is
    a written constant rather than something derived from the interval.
    """

    label: str
    interval: timedelta

    def __post_init__(self) -> None:
        if not self.label:
            raise DecisionConfigError("A timeframe label must be a non-empty identifier.")
        if self.interval <= timedelta(0):
            raise DecisionConfigError(
                f"Timeframe {self.label!r} must cover a positive interval, got {self.interval}."
            )

    def constituents(self, base_interval: timedelta = BAR_INTERVAL) -> int:
        """How many base bars make up one bar of this timeframe.

        An interval that is not a whole multiple of the base is refused rather
        than rounded: a bucket that cannot be filled by a whole number of base
        bars can never be judged complete by counting, and counting is the only
        completeness evidence this module has.
        """
        if base_interval <= timedelta(0):
            raise DecisionConfigError(f"base_interval must be positive, got {base_interval}.")
        base_us = int(base_interval / timedelta(microseconds=1))
        interval_us = int(self.interval / timedelta(microseconds=1))
        if interval_us % base_us != 0:
            raise DecisionConfigError(
                f"Timeframe {self.label!r} covers {self.interval}, which is not a whole "
                f"multiple of the {base_interval} base interval. A bucket that cannot be "
                "filled by a whole number of base bars can never be shown to be complete."
            )
        return interval_us // base_us

    def required_base_bars(
        self,
        timeframe_bars: int,
        base_interval: timedelta = BAR_INTERVAL,
    ) -> int:
        """Base bars needed to build `timeframe_bars` complete bars of this timeframe."""
        if isinstance(timeframe_bars, bool) or not isinstance(timeframe_bars, int):
            raise DecisionConfigError(
                f"timeframe_bars must be an int, got {type(timeframe_bars).__name__}."
            )
        if timeframe_bars < 0:
            raise DecisionConfigError(f"timeframe_bars must not be negative, got {timeframe_bars}.")
        return timeframe_bars * self.constituents(base_interval)

    @property
    def reason_token(self) -> str:
        """The label as it appears inside a machine reason, e.g. ``15M``, ``4H``."""
        return self.label.upper()


#: The base timeframe. The only one the system actually fetches, and the one
#: every other timeframe in this module is derived from.
BASE_TIMEFRAME = TimeframeSpec(label="15m", interval=timedelta(minutes=15))

#: Trend confirmation. Four base bars.
HOUR_TIMEFRAME = TimeframeSpec(label="1h", interval=timedelta(hours=1))

#: Broader regime and context. Sixteen base bars.
FOUR_HOUR_TIMEFRAME = TimeframeSpec(label="4h", interval=timedelta(hours=4))

#: V3's three timeframes, tactical first. Order is part of the contract: it is
#: the order features, reasons, and per-timeframe metadata are reported in.
V3_TIMEFRAMES: tuple[TimeframeSpec, ...] = (
    BASE_TIMEFRAME,
    HOUR_TIMEFRAME,
    FOUR_HOUR_TIMEFRAME,
)

TIMEFRAMES_BY_LABEL: dict[str, TimeframeSpec] = {spec.label: spec for spec in V3_TIMEFRAMES}


def timeframe_for(label: str) -> TimeframeSpec:
    """Return the spec named `label`, refusing an unknown timeframe."""
    try:
        return TIMEFRAMES_BY_LABEL[label]
    except KeyError:
        raise DecisionConfigError(
            f"Unknown timeframe {label!r}. Known timeframes are: {', '.join(TIMEFRAMES_BY_LABEL)}."
        ) from None


def _aggregate_vwap(
    frame: pd.DataFrame,
    grouped: DataFrameGroupBy,
    volume_totals: pd.Series,
) -> pd.Series:
    """Volume-weighted mean of the constituent VWAPs, per bucket.

    A plain mean would weight a thin bar the same as a heavy one, which is the
    one thing a volume-weighted average price exists not to do. When the whole
    bucket traded no volume there is nothing to weight by, and the unweighted
    mean is the only answer that is not a division by zero.

    Vectorized rather than applied per group: V3 aggregates the same window
    three ways on every decision, and a Python-level pass over every bucket is
    the difference between a decision costing milliseconds and costing seconds.
    """
    notional = (frame["vwap"].astype("float64") * frame["volume"].astype("float64")).groupby(
        frame[_BUCKET_COLUMN], sort=True
    )
    weighted = notional.sum() / volume_totals
    return weighted.where(volume_totals > 0, grouped["vwap"].mean())


def aggregate_bars(
    bars: pd.DataFrame,
    spec: TimeframeSpec,
    *,
    base_interval: timedelta = BAR_INTERVAL,
) -> pd.DataFrame:
    """Aggregate base bars into complete `spec` bars. Incomplete buckets are dropped.

    Returns a canonical frame stamped with each bucket's **start**, matching the
    interval-start convention used everywhere else in this system. The supplied
    frame is not modified, and aggregating the base timeframe onto itself is a
    well-defined identity rather than a special case.
    """
    frame = normalize_bars(bars, base_interval=base_interval)
    constituents = spec.constituents(base_interval)
    interval = pd.Timedelta(spec.interval)

    frame[_BUCKET_COLUMN] = floor_to_interval(frame["timestamp"], interval)
    grouped = frame.groupby(_BUCKET_COLUMN, sort=True)

    volume_totals = grouped["volume"].sum()
    aggregated = pd.DataFrame(
        {
            "timestamp": volume_totals.index,
            "symbol": grouped["symbol"].first().to_numpy(),
            "open": grouped["open"].first().to_numpy(),
            "high": grouped["high"].max().to_numpy(),
            "low": grouped["low"].min().to_numpy(),
            "close": grouped["close"].last().to_numpy(),
            "volume": volume_totals.to_numpy(),
        }
    )
    if "trade_count" in frame.columns:
        aggregated["trade_count"] = grouped["trade_count"].sum().to_numpy()
    if "vwap" in frame.columns:
        aggregated["vwap"] = _aggregate_vwap(frame, grouped, volume_totals).to_numpy()

    complete = grouped.size().to_numpy() == constituents
    aggregated = aggregated.loc[complete].reset_index(drop=True)

    columns = [
        column for column in (*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS) if column in aggregated.columns
    ]
    return aggregated[columns]


def usable_history(
    aggregated: pd.DataFrame,
    spec: TimeframeSpec,
    *,
    base_bar_start: datetime | pd.Timestamp,
    base_interval: timedelta = BAR_INTERVAL,
) -> pd.DataFrame:
    """The `spec` bars fully closed by the time the base bar at `base_bar_start` closed.

    The one place V3's no-look-ahead rule is enforced. A 4-hour bar starting at
    12:00 is not usable on the 15-minute bar starting at 15:30 - it does not
    close until 16:00, and the base bar closes at 15:45 - however tempting the
    fifteen minutes of hindsight would be.
    """
    deadline = pd.Timestamp(base_bar_start)
    if deadline.tzinfo is None:
        raise DecisionInputError(
            "base_bar_start must be timezone-aware; a naive bar start would be read as UTC "
            "and could admit a higher-timeframe bar that has not closed."
        )
    deadline = deadline.tz_convert("UTC") + pd.Timedelta(base_interval)
    if aggregated.empty:
        return aggregated.reset_index(drop=True)
    closes = aggregated["timestamp"] + pd.Timedelta(spec.interval)
    return aggregated.loc[closes <= deadline].reset_index(drop=True)


def align_timeframes(
    bars: pd.DataFrame,
    specs: tuple[TimeframeSpec, ...] = V3_TIMEFRAMES,
    *,
    base_bar_start: datetime | pd.Timestamp | None = None,
    base_interval: timedelta = BAR_INTERVAL,
) -> dict[str, pd.DataFrame]:
    """Aggregate `bars` to every spec and trim each to what the base bar could see.

    `base_bar_start` defaults to the newest bar in `bars`, which is the newest
    *completed* bar by the time a decision engine is called: whether a bar is
    complete is C9's rule, judged before these bars were fetched, and this
    module re-deriving it would be a second copy of a rule that already exists.
    """
    frame = normalize_bars(bars, base_interval=base_interval)
    anchor = pd.Timestamp(frame["timestamp"].iloc[-1] if base_bar_start is None else base_bar_start)
    if anchor.tzinfo is None:
        raise DecisionInputError("base_bar_start must be timezone-aware.")
    anchor = anchor.tz_convert("UTC")

    aligned: dict[str, pd.DataFrame] = {}
    for spec in specs:
        aggregated = aggregate_bars(frame, spec, base_interval=base_interval)
        aligned[spec.label] = usable_history(
            aggregated,
            spec,
            base_bar_start=anchor,
            base_interval=base_interval,
        )
    return aligned


__all__ = [
    "BASE_TIMEFRAME",
    "FOUR_HOUR_TIMEFRAME",
    "HOUR_TIMEFRAME",
    "TIMEFRAMES_BY_LABEL",
    "V3_TIMEFRAMES",
    "TimeframeSpec",
    "aggregate_bars",
    "align_timeframes",
    "timeframe_for",
    "usable_history",
]
