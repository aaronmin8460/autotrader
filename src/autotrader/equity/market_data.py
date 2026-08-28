"""Equity V0.2: the bounded recent-bar fetch for the runtime. One batch per cycle.

The runtime needs exactly enough completed regular-session history to evaluate
EMA 20 / EMA 50 on the newest completed bar of each of ten symbols, and nothing
more. It re-reads a bounded window every cycle rather than maintaining an
incremental local store, for the same reason the crypto path does: a bounded
window is one cheap request, and a cache that must stay correct across restarts
is a data-warehouse problem this milestone has no reason to take on.

**Ten symbols, one request.** Alpaca's stock bars endpoint takes a list, so the
whole universe costs a single call per cycle - not ten. That is the entire
API-budget story for equity market data, and it is why nothing here loops over
symbols issuing requests.

**The window is measured in sessions, not in days.** A 200-bar lookback spans
roughly eight trading days, but eight *calendar* days is a different and
sometimes wrong number: a holiday week, a long weekend, or a run of early
closes all shorten it. So the window is anchored on real sessions read from the
broker's calendar - which the runtime already holds - and the calendar lookup
is cached, so this costs no extra provider call in steady state.

**Extended-hours candles are filtered out here, before anything sees them.**
The IEX feed serves pre-market and post-market bars in the same response as
regular-session ones. They are real data and they are not what this strategy
trades, so `session_bar_mask` drops them against each day's own session - which
is what makes an early close come out right rather than merely close.

**The request never asks for the in-progress candle.** The window ends at the
last instant of the newest completed bar's interval. The runtime re-checks
completeness on whatever comes back anyway, because a provider's answer is
data, not a promise.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

import pandas as pd

from autotrader.data.historical import RESOLUTION
from autotrader.equity import EQUITY_SYMBOLS
from autotrader.equity.data import fetch_bars_for_symbols
from autotrader.equity.session import (
    MarketCalendar,
    MarketSession,
    lookback_window,
    market_date,
    recent_sessions,
    session_bar_mask,
    sessions_needed,
)
from autotrader.runtime.schedule import require_lookback_bars, require_utc


class EquityBarSource(Protocol):
    """Where the equity runtime gets recent completed bars from.

    A protocol so tests inject bars directly instead of reaching a provider,
    and so the runtime never holds a market-data client of its own.
    """

    def recent_bars(
        self,
        symbols: Sequence[str],
        *,
        now: datetime,
        latest_bar_start: datetime,
        lookback_bars: int,
    ) -> dict[str, pd.DataFrame]:
        """Canonical regular-session bars per symbol, newest bar last."""


def filter_to_sessions(
    frame: pd.DataFrame,
    sessions: Sequence[MarketSession],
    *,
    lookback_bars: int,
) -> pd.DataFrame:
    """Keep only regular-session bars, then keep only the newest `lookback_bars`.

    Two separate trims, in that order, because they answer different questions:
    the first is "is this candle tradable at all?" and the second is "how much
    history does the strategy need?". Doing the count first would let a
    pre-market bar consume one of the slots EMA 50 is relying on.
    """
    count = require_lookback_bars(lookback_bars)
    if frame.empty:
        return frame
    mask = session_bar_mask(sessions, list(frame["timestamp"]))
    regular = frame.loc[mask].reset_index(drop=True)
    if len(regular) <= count:
        return regular
    return regular.iloc[-count:].reset_index(drop=True)


class AlpacaEquityBars:
    """The production `EquityBarSource`, over the equity market-data boundary.

    Holds one client for the life of the process. Building one per cycle would
    re-read credentials from the environment twenty-six times a day for no
    benefit; the client itself is stateless between calls.
    """

    def __init__(
        self,
        calendar: MarketCalendar,
        client: object | None = None,
    ) -> None:
        self._calendar = calendar
        self._client = client
        #: Provider calls made, for the shared API budget a later phase owns.
        #: Calendar lookups are counted by the calendar itself.
        self.api_calls = 0

    def _resolve_client(self) -> object:
        if self._client is None:
            from autotrader.equity.data import create_client

            self._client = create_client()
        return self._client

    def sessions_for(self, *, latest_bar_start: datetime, lookback_bars: int) -> tuple:
        """The sessions the lookback window spans, oldest first."""
        count = require_lookback_bars(lookback_bars)
        latest = require_utc(latest_bar_start, "latest_bar_start")
        return recent_sessions(
            self._calendar,
            day=market_date(latest),
            count=sessions_needed(count),
        )

    def recent_bars(
        self,
        symbols: Sequence[str],
        *,
        now: datetime,
        latest_bar_start: datetime,
        lookback_bars: int,
    ) -> dict[str, pd.DataFrame]:
        """Fetch the bounded completed window for the whole universe at once.

        Raises `EquityDataError` when the provider refuses, which the runtime
        treats as a controlled failure to retry on the next boundary.
        """
        count = require_lookback_bars(lookback_bars)
        latest = require_utc(latest_bar_start, "latest_bar_start")
        require_utc(now, "now")
        sessions = self.sessions_for(latest_bar_start=latest, lookback_bars=count)
        start, end = lookback_window(sessions, latest_bar_start=latest)
        self.api_calls += 1
        frames = fetch_bars_for_symbols(self._resolve_client(), symbols, start, end - RESOLUTION)
        return {
            symbol: filter_to_sessions(frame, sessions, lookback_bars=count)
            for symbol, frame in frames.items()
        }


__all__ = [
    "EQUITY_SYMBOLS",
    "AlpacaEquityBars",
    "EquityBarSource",
    "filter_to_sessions",
]
