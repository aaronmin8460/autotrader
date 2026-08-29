"""Deterministic bar fixtures shared by the research tests.

Every series here is generated from a closed-form expression rather than from a
random source. Research tests assert on exact `Decimal` results, so a fixture
that varied between runs would make a failure impossible to reproduce - which
is the same property the infrastructure under test is supposed to guarantee.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pandas as pd

#: The canonical bar schema, in contract order.
COLUMNS: tuple[str, ...] = (
    "timestamp",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "trade_count",
    "vwap",
)

#: The 15-minute cadence the whole system uses.
BAR_INTERVAL = timedelta(minutes=15)

#: A fixed start instant. A Wednesday, so a crypto fixture crosses a weekend
#: without the test having to arrange it.
START = datetime(2025, 1, 1, tzinfo=UTC)


def bars_from_closes(
    closes: list[float],
    *,
    symbol: str = "BTC/USD",
    start: datetime = START,
    interval: timedelta = BAR_INTERVAL,
) -> pd.DataFrame:
    """Build a valid canonical frame whose closes are exactly `closes`.

    Each bar opens at the previous bar's close, so a next-bar fill price is
    predictable from the input, and the high/low bracket the open and close so
    the C2 OHLC relationships hold.
    """
    rows = []
    for index, close in enumerate(closes):
        open_price = closes[index - 1] if index else close
        rows.append(
            (
                start + interval * index,
                symbol,
                open_price,
                max(open_price, close) * 1.001,
                min(open_price, close) * 0.999,
                close,
                100.0 + index,
                10,
                (open_price + close) / 2,
            )
        )
    return pd.DataFrame(rows, columns=list(COLUMNS))


def wave(
    count: int = 600,
    *,
    phase: float = 0.0,
    drift: float = 0.02,
    symbol: str = "BTC/USD",
) -> pd.DataFrame:
    """A deterministic two-frequency wave with a drift term.

    Crosses often enough that a 20/50 EMA strategy produces a handful of round
    trips over a few hundred bars, which is what most of these tests need.
    """
    closes = [
        100.0
        + 20.0 * math.sin(index / 23.0 + phase)
        + 15.0 * math.sin(index / 7.0 + phase)
        + index * drift
        for index in range(count)
    ]
    return bars_from_closes(closes, symbol=symbol)


def _level(count: int, price: float) -> list[float]:
    """`count` bars at exactly `price`."""
    return [price] * count


def rally(*, symbol: str = "BTC/USD") -> pd.DataFrame:
    """Flat, then a step up: exactly one entry and no exit.

    A step rather than a ramp, for the reason the production fixtures use one:
    on a series that is already rising when the slow EMA finishes warming up,
    the fast EMA is *already* above the slow one and no crossing is ever
    observed. A flat warm-up followed by a step guarantees the crossover
    happens after both EMAs are defined, which is what the test intends to
    exercise.
    """
    return bars_from_closes(_level(60, 100.0) + _level(40, 120.0), symbol=symbol)


def rally_then_selloff(*, symbol: str = "BTC/USD") -> pd.DataFrame:
    """Flat, up, then down: one complete round trip."""
    return bars_from_closes(_level(60, 100.0) + _level(40, 120.0) + _level(60, 80.0), symbol=symbol)


def multi_cycle(*, symbol: str = "BTC/USD") -> pd.DataFrame:
    """Two full cycles, ending long: several round trips plus an open position."""
    return bars_from_closes(
        _level(60, 100.0)
        + _level(40, 120.0)
        + _level(60, 80.0)
        + _level(50, 130.0)
        + _level(50, 60.0)
        + _level(50, 140.0),
        symbol=symbol,
    )


def flat(count: int = 200, *, symbol: str = "BTC/USD") -> pd.DataFrame:
    """A constant series: the EMAs never cross, so nothing ever trades."""
    return bars_from_closes([100.0] * count, symbol=symbol)


def equity_bars(count: int = 400, *, symbol: str = "SPY") -> pd.DataFrame:
    """The same wave under an equity ticker, for the equity universe path."""
    return wave(count, symbol=symbol)


__all__ = [
    "BAR_INTERVAL",
    "COLUMNS",
    "START",
    "bars_from_closes",
    "equity_bars",
    "flat",
    "multi_cycle",
    "rally",
    "rally_then_selloff",
    "wave",
]
