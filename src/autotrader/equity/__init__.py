"""Equity V0.2: the US-equity vocabulary, and nothing that can reach a network.

This module is the one place the equity universe, its timeframe, and its market
timezone are written down. It imports only the standard library on purpose, so
that every other equity module - the session arithmetic, the market-data
boundary, the execution boundary, the runtime - can depend on it without any of
them depending on each other.

**The universe is exactly ten symbols, and the tuple order is the processing
order.** Equity V0.2 is a fixed, frozen list (docs/SPEC.md section 3.1E).
Adding an eleventh symbol is a scope change requiring an edit to the spec, not
a configuration value: an unbounded universe would make the per-cycle API cost,
the risk arithmetic, and the "no concurrent broker submissions" rule all depend
on something nobody wrote down.

**Regular US market hours only.** Crypto is continuous and schedules on UTC
boundaries; equities are not, and do not. Session times are read from the
broker's own calendar rather than assumed, so a holiday and an early close are
facts this system is told rather than dates it hardcodes. `MARKET_TIMEZONE` is
the frame those wall-clock session times are expressed in - Alpaca's calendar
endpoint returns naive ``09:30``/``16:00`` strings, and reading them in any
other zone would misplace every session by hours.

**UTC is what gets stored.** `America/New_York` interprets a session; every
timestamp this system persists, checkpoints, or schedules against is UTC. The
two are kept explicitly distinct rather than left to a default.

Nothing here trades, fetches, or schedules. See `autotrader.equity.session` for
the session arithmetic, `autotrader.equity.data` for market data,
`autotrader.execution.equity` for the paper execution boundary, and
`autotrader.equity.runtime` for the loop.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

#: The frozen Equity V0.2 universe, in the order the runtime processes it.
#:
#: Three index ETFs and seven large-cap US equities. The order is part of the
#: contract: one symbol is finished - risk sized against the account as it
#: stands, order submitted or refused - before the next is looked at, so ten
#: signals landing on the same bar can never size themselves against the same
#: stale cash and exposure figures.
EQUITY_SYMBOLS: tuple[str, ...] = (
    "SPY",
    "QQQ",
    "IWM",
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
)

#: How many symbols the universe holds. Named so a test can assert the count
#: without restating the list, which is how a symbol creeps in unnoticed.
EQUITY_UNIVERSE_SIZE = 10

#: The only timeframe Equity V0.2 supports, matching the crypto product.
EQUITY_TIMEFRAME = "15m"

#: The zone US market sessions are expressed in.
#:
#: Not a display preference: Alpaca's `/calendar` endpoint returns each
#: session's open and close as naive wall-clock times on the session's own
#: date, and they are Eastern. Reading ``09:30`` as UTC would place the open
#: four or five hours early, every day, and silently.
MARKET_TIMEZONE = ZoneInfo("America/New_York")

#: The name of that zone, for metadata sidecars and status output.
MARKET_TIMEZONE_NAME = "America/New_York"


class EquityError(Exception):
    """An expected, user-facing equity failure. Reported without a traceback."""


def normalize_symbol(symbol: str) -> str:
    """Uppercase `symbol` and confirm it is one of the ten Equity V0.2 symbols.

    The universe is closed. A symbol outside it is refused here rather than
    passed on to a broker call that would succeed: this system's risk
    arithmetic, its per-cycle API budget, and its reconciliation scope are all
    written against a known list, and quietly trading an eleventh symbol would
    invalidate all three.
    """
    if not isinstance(symbol, str):
        raise EquityError(f"symbol must be a string, got {type(symbol).__name__}.")
    normalized = symbol.strip().upper()
    if normalized not in EQUITY_SYMBOLS:
        raise EquityError(
            f"Unsupported equity symbol: {symbol!r}. Supported symbols are: "
            f"{', '.join(EQUITY_SYMBOLS)}."
        )
    return normalized


def normalize_timeframe(timeframe: str) -> str:
    """Confirm `timeframe` is the single timeframe this milestone supports."""
    if not isinstance(timeframe, str):
        raise EquityError(f"timeframe must be a string, got {type(timeframe).__name__}.")
    normalized = timeframe.strip().lower()
    if normalized != EQUITY_TIMEFRAME:
        raise EquityError(
            f"Unsupported timeframe: {timeframe!r}. Only {EQUITY_TIMEFRAME!r} is supported."
        )
    return normalized


__all__ = [
    "EQUITY_SYMBOLS",
    "EQUITY_TIMEFRAME",
    "EQUITY_UNIVERSE_SIZE",
    "MARKET_TIMEZONE",
    "MARKET_TIMEZONE_NAME",
    "EquityError",
    "normalize_symbol",
    "normalize_timeframe",
]
