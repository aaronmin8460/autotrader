"""Display bars for the dashboard's charts, at display timeframes.

The trading system fetches 15-minute bars through two boundaries -
`autotrader.data.historical` for crypto and `autotrader.equity.data` for
equities - and those boundaries own the trading timeframe, the trading
universe, and the metered API budget. A chart wants something else: a
5-minute line for one session, hourly bars for a month, daily bars for a
quarter. This module builds those requests against the same provider SDK, and
it is the only place outside the two boundaries that does.

**It is not in the trading path.** Nothing under `autotrader.runtime`,
`autotrader.equity`, `autotrader.execution` or `autotrader.reconciliation`
imports this module; the dashboard's chart process is the sole caller, runs as
its own systemd unit, and a failure here affects a chart panel and nothing
else. It writes no store and consumes no API budget row - the chart process
keeps its own provider ceiling.

**Market data only.** The two client factories it uses build historical-data
clients, which have no order surface of any kind; the trading client factory
lives in `autotrader.execution.paper` and nothing here reaches it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from alpaca.data.enums import Adjustment
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from autotrader.data.historical import FEED as CRYPTO_FEED
from autotrader.equity.data import FEED as STOCK_FEED


@dataclass(frozen=True)
class ChartBar:
    """One bar, as plain values. No SDK type leaves this module."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class ChartRange:
    """One display range: the provider timeframe and how far back to ask."""

    key: str
    label: str
    timeframe_amount: int
    timeframe_unit: str
    lookback: timedelta
    #: Keep only bars inside the regular US session (equities only).
    session_only: bool
    #: Keep only the newest N distinct exchange sessions, or None for all.
    sessions: int | None
    #: Downsample to at most this many points per series.
    max_points: int
    #: Seconds a fetched series stays fresh in the chart cache.
    ttl_seconds: int

    def timeframe(self) -> TimeFrame:
        unit = {
            "minute": TimeFrameUnit.Minute,
            "hour": TimeFrameUnit.Hour,
            "day": TimeFrameUnit.Day,
        }[self.timeframe_unit]
        return TimeFrame(self.timeframe_amount, unit)


#: The supported ranges. Intraday ranges ask for a few extra calendar days so
#: a weekend or a holiday still yields the requested number of sessions.
CHART_RANGES: dict[str, ChartRange] = {
    "1D": ChartRange("1D", "1 day", 5, "minute", timedelta(days=6), True, 1, 120, 60),
    "5D": ChartRange("5D", "5 days", 15, "minute", timedelta(days=10), True, 5, 160, 300),
    "1M": ChartRange("1M", "1 month", 1, "hour", timedelta(days=45), True, None, 200, 1800),
    "3M": ChartRange("3M", "3 months", 1, "day", timedelta(days=100), False, None, 120, 3600),
    "6M": ChartRange("6M", "6 months", 1, "day", timedelta(days=200), False, None, 160, 3600),
}

RANGE_KEYS: tuple[str, ...] = tuple(CHART_RANGES)


def range_for(key: str) -> ChartRange:
    try:
        return CHART_RANGES[key.upper()]
    except KeyError:
        raise ValueError(f"Unknown chart range {key!r}; expected one of {RANGE_KEYS}.") from None


def _window(spec: ChartRange, now: datetime) -> tuple[datetime, datetime]:
    if now.tzinfo is None:
        raise ValueError("`now` must be timezone-aware.")
    end = now.astimezone(UTC)
    return end - spec.lookback, end


def _to_bars(raw: Iterable[object]) -> list[ChartBar]:
    bars: list[ChartBar] = []
    for bar in raw:
        timestamp = getattr(bar, "timestamp", None)
        if timestamp is None:
            continue
        moment = timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=UTC)
        bars.append(
            ChartBar(
                timestamp=moment.astimezone(UTC),
                open=float(getattr(bar, "open", 0.0)),
                high=float(getattr(bar, "high", 0.0)),
                low=float(getattr(bar, "low", 0.0)),
                close=float(getattr(bar, "close", 0.0)),
                volume=float(getattr(bar, "volume", 0.0) or 0.0),
            )
        )
    bars.sort(key=lambda bar: bar.timestamp)
    return bars


def _by_symbol(barset: object) -> dict[str, list[object]]:
    data = getattr(barset, "data", None)
    if not isinstance(data, dict):
        return {}
    return {str(key): list(value) for key, value in data.items()}


def build_stock_request(
    symbols: Sequence[str], spec: ChartRange, now: datetime
) -> StockBarsRequest:
    """One batched, split-adjusted stock-bars request for every symbol at once.

    Split-adjusted rather than raw: a chart of a symbol that split would
    otherwise draw a phantom crash, and the broker's average entry price -
    which the detail chart overlays - is already in post-split shares.
    """
    start, end = _window(spec, now)
    return StockBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=spec.timeframe(),
        start=start,
        end=end,
        feed=STOCK_FEED,
        adjustment=Adjustment.SPLIT,
    )


def build_crypto_request(
    symbols: Sequence[str], spec: ChartRange, now: datetime
) -> CryptoBarsRequest:
    start, end = _window(spec, now)
    return CryptoBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=spec.timeframe(),
        start=start,
        end=end,
    )


def fetch_stock_chart_bars(
    client: object, symbols: Sequence[str], spec: ChartRange, *, now: datetime
) -> dict[str, list[ChartBar]]:
    """One provider call for the whole list; every requested symbol has a key."""
    if not symbols:
        return {}
    barset = client.get_stock_bars(build_stock_request(symbols, spec, now))  # type: ignore[attr-defined]
    returned = _by_symbol(barset)
    return {symbol: _to_bars(returned.get(symbol, [])) for symbol in symbols}


def fetch_crypto_chart_bars(
    client: object, symbols: Sequence[str], spec: ChartRange, *, now: datetime
) -> dict[str, list[ChartBar]]:
    """One provider call for the whole list; every requested symbol has a key."""
    if not symbols:
        return {}
    barset = client.get_crypto_bars(  # type: ignore[attr-defined]
        build_crypto_request(symbols, spec, now), feed=CRYPTO_FEED
    )
    returned = _by_symbol(barset)
    return {symbol: _to_bars(returned.get(symbol, [])) for symbol in symbols}


__all__ = [
    "CHART_RANGES",
    "RANGE_KEYS",
    "ChartBar",
    "ChartRange",
    "build_crypto_request",
    "build_stock_request",
    "fetch_crypto_chart_bars",
    "fetch_stock_chart_bars",
    "range_for",
]
