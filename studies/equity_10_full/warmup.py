"""Measuring the real Equity warm-up, per symbol, instead of trusting a constant.

The declared ``EQUITY_POLICY.required_base_bars(("15m","1h","4h"))`` is 2,834 -
109 required 4-hour bars at 26 base bars each. The pilot measured the true
worst case on real SPY/QQQ frames at 2,885, because an early close yields no
4-hour bar at all and a single missing 15-minute bar destroys the one 4-hour
bucket its session had. Single-name symbols have more missing bars, so this
module measures every symbol rather than assuming the pilot's two generalize.

**What is measured.** For every candidate decision bar *t*, the smallest
lookback window ending at *t* that contains, in full, the constituents of the
109 most recently *usable* complete 4-hour buckets. A bucket is complete when
all ``4h / 15m = 16`` of its base bars exist in the regular-session frame (the
same count-based rule ``aggregate_bars`` applies), and usable at *t* when its
last constituent bar is at or before *t* (the shipped ``usable_history``
admission rule: ``bucket_start + interval <= base_bar_start + base_interval``).

The same measurement at the 1-hour timeframe (109 buckets of 4) is reported
alongside; it is dominated by the 4-hour requirement everywhere the pilot
looked, and this module checks that rather than assuming it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from autotrader.decision.config import EQUITY_POLICY

#: Constituent base bars per complete derived bar, from the derivation itself:
#: a UTC-epoch bucket of the derived interval must hold exactly this many
#: 15-minute bars to be emitted.
BASE_BARS_PER_BUCKET = {"1h": 4, "4h": 16}

#: 15 minutes, the base interval every count here is against.
BASE_INTERVAL = pd.Timedelta("15min")


class WarmupError(Exception):
    """The warm-up could not be measured on what was supplied."""


@dataclass(frozen=True)
class WarmupMeasurement:
    """One symbol's measured worst-case lookback over a region of its frame."""

    symbol: str
    timeframe: str
    required_buckets: int
    first_position: int
    last_position: int
    worst_lookback_bars: int
    worst_at_utc: str
    declared_required_base_bars: int

    def to_json_dict(self) -> dict[str, object]:
        return dict(self.__dict__)


def _bucket_table(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Every complete derived bucket: its first and last constituent row position."""
    per_bucket = BASE_BARS_PER_BUCKET[timeframe]
    interval = BASE_INTERVAL * per_bucket
    timestamps = pd.DatetimeIndex(frame["timestamp"])
    bucket_start = timestamps.floor(interval)
    counts = pd.Series(range(len(frame))).groupby(bucket_start).agg(["min", "max", "count"])
    complete = counts.loc[counts["count"] == per_bucket]
    if complete.empty:
        raise WarmupError(f"No complete {timeframe} bucket exists in this frame.")
    return complete.reset_index(names="bucket_start")


def measure_worst_lookback(
    frame: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    required_buckets: int | None = None,
    first_position: int | None = None,
) -> WarmupMeasurement:
    """The worst-case sliding-window lookback over ``[first_position, end)``.

    For each candidate decision bar at or after `first_position`, the window
    handed to the engine must reach back to the first constituent of the
    `required_buckets`-th most recent usable bucket. The worst case over the
    region is what a fixed study lookback must clear.
    """
    if required_buckets is None:
        required_buckets = EQUITY_POLICY.timeframe(timeframe).periods.required_bars
    buckets = _bucket_table(frame, timeframe)
    last_positions = buckets["max"].to_numpy()
    first_positions = buckets["min"].to_numpy()
    if len(buckets) < required_buckets:
        raise WarmupError(
            f"{symbol}: only {len(buckets)} complete {timeframe} buckets exist; "
            f"{required_buckets} are required."
        )

    # The earliest decision bar with `required_buckets` usable buckets behind it.
    earliest = int(last_positions[required_buckets - 1])
    start = earliest if first_position is None else max(first_position, earliest)

    worst = 0
    worst_position = start
    cursor = required_buckets - 1  # index of the newest usable bucket at `start`
    for position in range(start, len(frame)):
        while cursor + 1 < len(buckets) and last_positions[cursor + 1] <= position:
            cursor += 1
        anchor = int(first_positions[cursor - required_buckets + 1])
        lookback = position - anchor + 1
        if lookback > worst:
            worst = lookback
            worst_position = position
    declared = EQUITY_POLICY.required_base_bars(("15m", "1h", "4h"))
    return WarmupMeasurement(
        symbol=symbol,
        timeframe=timeframe,
        required_buckets=int(required_buckets),
        first_position=start,
        last_position=len(frame) - 1,
        worst_lookback_bars=int(worst),
        worst_at_utc=pd.Timestamp(frame["timestamp"].iloc[worst_position]).isoformat(),
        declared_required_base_bars=int(declared),
    )


__all__ = [
    "BASE_BARS_PER_BUCKET",
    "BASE_INTERVAL",
    "WarmupError",
    "WarmupMeasurement",
    "measure_worst_lookback",
]
