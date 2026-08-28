"""C8: the bounded recent-bar fetch. One small request per symbol per cycle.

The runtime needs exactly enough completed history to evaluate EMA 20 / EMA 50
on the newest completed bar, and nothing more. It re-reads a fixed, bounded
window every cycle rather than maintaining an incremental local store: a
window of at most `MAX_LOOKBACK_BARS` bars is one cheap request, and a cache
that must stay correct across restarts is a data-warehouse problem this
milestone has no reason to take on.

**The request itself excludes the in-progress bar.** The window ends at the
last instant of the newest *completed* interval, mirroring C1's inclusive-`end`
handling, so the provider is not even asked for the candle that is still
forming. The runtime re-checks completeness on whatever comes back anyway
(`runner`), because a provider's answer is data, not a promise.

**No polling.** There is one call here per symbol per completed boundary - two
calls every fifteen minutes for the whole system. No account read, no position
read, and no price read happens in this module at all; those belong to the
execution boundary and only run when a signal actually needs one. The call
counter exists so a later shared crypto+equity API budget has a real number to
start from.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

import pandas as pd

from autotrader.data.historical import RESOLUTION, fetch_bars, normalize_symbol
from autotrader.runtime.schedule import (
    BAR_INTERVAL,
    DEFAULT_SAFETY_DELAY,
    latest_completed_bar_start,
    lookback_window_start,
    require_lookback_bars,
    require_safety_delay,
)


class MarketDataSource(Protocol):
    """Where the runtime gets recent completed bars from.

    A protocol so tests inject bars directly instead of reaching a provider,
    and so the runtime never holds a provider client of its own.
    """

    def recent_bars(self, symbol: str, *, now: datetime, lookback_bars: int) -> pd.DataFrame:
        """Canonical bars for the bounded completed window ending at `now`."""


def completed_window(
    now: datetime,
    *,
    lookback_bars: int,
    safety_delay: timedelta = DEFAULT_SAFETY_DELAY,
) -> tuple[datetime, datetime]:
    """The ``(start, end)`` request window of completed bars at `now`.

    `end` is the last instant of the newest completed interval, not the next
    boundary: Alpaca's crypto endpoint treats `end` as inclusive, so asking for
    the boundary itself would return the in-progress bar stamped there.
    """
    count = require_lookback_bars(lookback_bars)
    delay = require_safety_delay(safety_delay)
    latest = latest_completed_bar_start(now, safety_delay=delay)
    start = lookback_window_start(latest, lookback_bars=count)
    return start, latest + BAR_INTERVAL - RESOLUTION


class AlpacaCryptoBars:
    """The production `MarketDataSource`, over C1's provider boundary.

    Holds one client for the life of the process. Building a client per cycle
    would re-read credentials from the environment ninety-six times a day for
    no benefit; the client itself is stateless between calls.
    """

    def __init__(
        self,
        client: object | None = None,
        *,
        safety_delay: timedelta = DEFAULT_SAFETY_DELAY,
    ) -> None:
        self._client = client
        self._safety_delay = require_safety_delay(safety_delay)
        #: Provider calls made, for the API-budget work a later phase owns.
        self.api_calls = 0

    def _resolve_client(self) -> object:
        if self._client is None:
            from autotrader.data.historical import create_client

            self._client = create_client()
        return self._client

    def recent_bars(self, symbol: str, *, now: datetime, lookback_bars: int) -> pd.DataFrame:
        """Fetch the bounded completed window for one pair.

        Raises `HistoricalDataError` when the provider refuses, which the
        runtime treats as a controlled failure to retry on the next boundary.
        """
        ticker = normalize_symbol(symbol)
        start, end = completed_window(
            now, lookback_bars=lookback_bars, safety_delay=self._safety_delay
        )
        self.api_calls += 1
        return fetch_bars(self._resolve_client(), ticker, start, end)


__all__ = ["AlpacaCryptoBars", "MarketDataSource", "completed_window"]
