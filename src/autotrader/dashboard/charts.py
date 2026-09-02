"""The dashboard's chart layer: batched, cached, capped, and out of the trading path.

Every chart on the dashboard is a price series and nothing more. This module
turns a list of symbols and a display range into one batched provider request
per miss, keeps the answer for a range-specific time, and refuses to make more
than a fixed number of provider calls a minute whatever the browser asks for.
It opens no store, reads no account, and shares no cache with anything that
trades: a chart that is stale says so on the wire and the account panels next
to it are unaffected, because they never go through here.

**Not the trading data path.** The runtimes fetch bars through their own
boundaries with their own metered budget. This process is a separate systemd
unit; its provider calls are counted here, against its own ceiling, and its
failure is a chart panel that reads "unavailable".

**Symbols are checked, not trusted.** A request may name at most
`MAX_SYMBOLS_PER_REQUEST` symbols, each either a tracked crypto pair or a
plain equity ticker. The page only ever asks for symbols an API payload named;
the ceiling is what bounds provider traffic if it ever asks for more.

**No provider text reaches the response.** A failed fetch becomes an
`unavailable_reason` code. Exception text is discarded here, because it is the
likeliest place for a key fragment to appear and this payload is bound for a
browser.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from autotrader.dashboard.models import ASSET_CLASS_CRYPTO, ASSET_CLASS_EQUITY
from autotrader.data import chart_bars
from autotrader.data.chart_bars import CHART_RANGES, RANGE_KEYS, ChartBar, ChartRange
from autotrader.execution.models import SUPPORTED_SYMBOLS

#: The most symbols one request may name. The current universes are ten
#: equities and two pairs; the observer universe is browsed one symbol at a
#: time. Anything wider is a mistake, and is refused rather than fetched.
MAX_SYMBOLS_PER_REQUEST = 12

#: Provider calls this process will make in any sixty-second window. Far
#: below the provider's own published ceiling and far above what a polling
#: page needs: the whole ten-symbol universe is one call per range.
MAX_PROVIDER_CALLS_PER_MINUTE = 30

_BUDGET_WINDOW = timedelta(seconds=60)

_EQUITY_TICKER = re.compile(r"^[A-Z]{1,5}$")

#: Regular US session, exchange-local.
MARKET_TIMEZONE = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)

UNAVAILABLE_BROKER_NOT_CONFIGURED = "BROKER_NOT_CONFIGURED"
UNAVAILABLE_PROVIDER_UNREADABLE = "PROVIDER_UNREADABLE"
UNAVAILABLE_PROVIDER_BUDGET = "PROVIDER_BUDGET_EXHAUSTED"
UNAVAILABLE_NO_BARS = "NO_BARS"
UNAVAILABLE_INVALID_SYMBOL = "INVALID_SYMBOL"

_CRYPTO_KEYS = frozenset(SUPPORTED_SYMBOLS)


class ChartRequestError(ValueError):
    """The request itself was malformed: a bad range, or symbols out of bounds."""


@dataclass(frozen=True)
class ChartSeries:
    """One symbol's display series, or the reason there is none."""

    symbol: str
    asset_class: str
    range: str
    timeframe: str
    available: bool
    points: tuple[tuple[str, float, float, float, float, float], ...]
    first_at: str | None
    last_at: str | None
    first_close: float | None
    last_close: float | None
    change_fraction: float | None
    fetched_at: str | None
    from_cache: bool
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class ChartBatch:
    """The answer to one request: every series asked for, in the order asked."""

    generated_at: str
    range: str
    range_label: str
    ttl_seconds: int
    series: tuple[ChartSeries, ...]
    provider_calls_made: int
    cache_hits: int
    budget_remaining: int
    note: str


def asset_class_for(symbol: str) -> str:
    return ASSET_CLASS_CRYPTO if symbol in _CRYPTO_KEYS else ASSET_CLASS_EQUITY


def normalize_symbols(raw: Sequence[str]) -> tuple[str, ...]:
    """Upper-cased, de-duplicated, order-preserving, and bounded.

    A crypto pair must be one of the tracked pairs; an equity must look like a
    ticker. Neither check widens what the page can ask for - it only refuses
    strings that could not be a symbol at all.
    """
    seen: list[str] = []
    for item in raw:
        symbol = item.strip().upper()
        if not symbol:
            continue
        if symbol not in _CRYPTO_KEYS and not _EQUITY_TICKER.fullmatch(symbol):
            raise ChartRequestError(f"{item!r} is not a tracked pair or an equity ticker.")
        if symbol not in seen:
            seen.append(symbol)
    if not seen:
        raise ChartRequestError("At least one symbol is required.")
    if len(seen) > MAX_SYMBOLS_PER_REQUEST:
        raise ChartRequestError(
            f"At most {MAX_SYMBOLS_PER_REQUEST} symbols per request; {len(seen)} were named."
        )
    return tuple(seen)


def _in_session(moment: datetime) -> bool:
    local = moment.astimezone(MARKET_TIMEZONE)
    return local.weekday() < 5 and SESSION_OPEN <= local.time() < SESSION_CLOSE  # noqa: PLR2004


def _session_date(moment: datetime) -> str:
    return moment.astimezone(MARKET_TIMEZONE).date().isoformat()


def shape_series(bars: Sequence[ChartBar], spec: ChartRange, *, equity: bool) -> list[ChartBar]:
    """Session filtering, newest-sessions cut, and downsampling for one series.

    Deterministic: the same bars and range produce the same points, so a
    cached series and a fresh one draw the same line.
    """
    kept = [bar for bar in bars if not (equity and spec.session_only) or _in_session(bar.timestamp)]
    if spec.sessions is not None and kept:
        sessions = sorted({_session_date(bar.timestamp) for bar in kept})[-spec.sessions :]
        wanted = set(sessions)
        kept = [bar for bar in kept if _session_date(bar.timestamp) in wanted]
    if len(kept) > spec.max_points:
        stride = -(-len(kept) // spec.max_points)
        sampled = kept[::stride]
        if sampled[-1] is not kept[-1]:
            sampled.append(kept[-1])
        kept = sampled
    return kept


def _series(
    symbol: str,
    spec: ChartRange,
    bars: Sequence[ChartBar],
    *,
    fetched_at: datetime,
    from_cache: bool,
) -> ChartSeries:
    shaped = shape_series(bars, spec, equity=asset_class_for(symbol) == ASSET_CLASS_EQUITY)
    timeframe = f"{spec.timeframe_amount}{spec.timeframe_unit[0]}"
    if not shaped:
        return ChartSeries(
            symbol=symbol,
            asset_class=asset_class_for(symbol),
            range=spec.key,
            timeframe=timeframe,
            available=False,
            points=(),
            first_at=None,
            last_at=None,
            first_close=None,
            last_close=None,
            change_fraction=None,
            fetched_at=fetched_at.isoformat(),
            from_cache=from_cache,
            unavailable_reason=UNAVAILABLE_NO_BARS,
        )
    first, last = shaped[0], shaped[-1]
    reference = first.open if first.open > 0 else first.close
    return ChartSeries(
        symbol=symbol,
        asset_class=asset_class_for(symbol),
        range=spec.key,
        timeframe=timeframe,
        available=True,
        points=tuple(
            (bar.timestamp.isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume)
            for bar in shaped
        ),
        first_at=first.timestamp.isoformat(),
        last_at=last.timestamp.isoformat(),
        first_close=reference,
        last_close=last.close,
        change_fraction=(last.close / reference - 1.0) if reference > 0 else None,
        fetched_at=fetched_at.isoformat(),
        from_cache=from_cache,
    )


def _unavailable(symbol: str, spec: ChartRange, reason: str, *, now: datetime) -> ChartSeries:
    return ChartSeries(
        symbol=symbol,
        asset_class=asset_class_for(symbol),
        range=spec.key,
        timeframe=f"{spec.timeframe_amount}{spec.timeframe_unit[0]}",
        available=False,
        points=(),
        first_at=None,
        last_at=None,
        first_close=None,
        last_close=None,
        change_fraction=None,
        fetched_at=now.isoformat(),
        from_cache=False,
        unavailable_reason=reason,
    )


@dataclass
class _Entry:
    expires_at: datetime
    fetched_at: datetime
    bars: tuple[ChartBar, ...]


class ChartCache:
    """Per-(range, symbol) bars with a deterministic TTL, plus the call ceiling.

    One instance per process. Reads that miss are grouped by asset class and
    fetched in one provider call per class; a miss that would exceed the
    per-minute ceiling is reported unavailable rather than fetched.
    """

    def __init__(
        self,
        *,
        stock_client_factory: object | None = None,
        crypto_client_factory: object | None = None,
        credentials_check: object | None = None,
        max_calls_per_minute: int = MAX_PROVIDER_CALLS_PER_MINUTE,
    ) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._calls: deque[datetime] = deque()
        self._max_calls = max_calls_per_minute
        self._stock_factory = stock_client_factory
        self._crypto_factory = crypto_client_factory
        self._credentials_check = credentials_check
        self._stock_client: object | None = None
        self._crypto_client: object | None = None

    # -- provider plumbing --------------------------------------------------

    def _credentials_configured(self) -> bool:
        if self._credentials_check is not None:
            return bool(self._credentials_check())  # type: ignore[operator]
        from autotrader.equity.data import credentials_configured

        return credentials_configured()

    def _stock(self) -> object:
        if self._stock_client is None:
            if self._stock_factory is not None:
                self._stock_client = self._stock_factory()  # type: ignore[operator]
            else:
                from autotrader.equity.data import create_client

                self._stock_client = create_client()
        return self._stock_client

    def _crypto(self) -> object:
        if self._crypto_client is None:
            if self._crypto_factory is not None:
                self._crypto_client = self._crypto_factory()  # type: ignore[operator]
            else:
                from autotrader.data.historical import create_client

                self._crypto_client = create_client()
        return self._crypto_client

    def _budget_remaining(self, now: datetime) -> int:
        while self._calls and now - self._calls[0] > _BUDGET_WINDOW:
            self._calls.popleft()
        return max(0, self._max_calls - len(self._calls))

    def _fetch(
        self, symbols: list[str], spec: ChartRange, *, crypto: bool, now: datetime
    ) -> dict[str, list[ChartBar]]:
        if crypto:
            return chart_bars.fetch_crypto_chart_bars(self._crypto(), symbols, spec, now=now)
        return chart_bars.fetch_stock_chart_bars(self._stock(), symbols, spec, now=now)

    # -- the read ------------------------------------------------------------

    def read(self, symbols: Sequence[str], range_key: str, *, now: datetime) -> ChartBatch:
        """Every series asked for, from cache where fresh, fetched where not."""
        wanted = normalize_symbols(symbols)
        try:
            spec = chart_bars.range_for(range_key)
        except ValueError as error:
            raise ChartRequestError(str(error)) from None
        now = now.astimezone(UTC)

        with self._lock:
            results: dict[str, ChartSeries] = {}
            misses: dict[bool, list[str]] = {True: [], False: []}
            hits = 0
            for symbol in wanted:
                entry = self._entries.get((spec.key, symbol))
                if entry is not None and entry.expires_at > now:
                    results[symbol] = _series(
                        symbol, spec, entry.bars, fetched_at=entry.fetched_at, from_cache=True
                    )
                    hits += 1
                else:
                    misses[symbol in _CRYPTO_KEYS].append(symbol)

            calls = 0
            for crypto, group in misses.items():
                if not group:
                    continue
                if not crypto and not self._credentials_configured():
                    for symbol in group:
                        results[symbol] = _unavailable(
                            symbol, spec, UNAVAILABLE_BROKER_NOT_CONFIGURED, now=now
                        )
                    continue
                if self._budget_remaining(now) <= 0:
                    for symbol in group:
                        results[symbol] = _unavailable(
                            symbol, spec, UNAVAILABLE_PROVIDER_BUDGET, now=now
                        )
                    continue
                self._calls.append(now)
                calls += 1
                try:
                    fetched = self._fetch(group, spec, crypto=crypto, now=now)
                except Exception:  # noqa: BLE001 - the text is discarded on purpose
                    if crypto:
                        self._crypto_client = None
                    else:
                        self._stock_client = None
                    for symbol in group:
                        results[symbol] = _unavailable(
                            symbol, spec, UNAVAILABLE_PROVIDER_UNREADABLE, now=now
                        )
                    continue
                expires = now + timedelta(seconds=spec.ttl_seconds)
                for symbol in group:
                    bars = tuple(fetched.get(symbol, []))
                    self._entries[(spec.key, symbol)] = _Entry(
                        expires_at=expires, fetched_at=now, bars=bars
                    )
                    results[symbol] = _series(symbol, spec, bars, fetched_at=now, from_cache=False)

            remaining = self._budget_remaining(now)

        return ChartBatch(
            generated_at=now.isoformat(),
            range=spec.key,
            range_label=spec.label,
            ttl_seconds=spec.ttl_seconds,
            series=tuple(results[symbol] for symbol in wanted),
            provider_calls_made=calls,
            cache_hits=hits,
            budget_remaining=remaining,
            note=(
                "Provider bars for display only. Not the trading data path, not account "
                "state, and cached per range; the account panels never read this."
            ),
        )

    def evict_expired(self, *, now: datetime) -> int:
        with self._lock:
            expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
            for key in expired:
                del self._entries[key]
        return len(expired)


def ranges_payload() -> list[dict[str, object]]:
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "timeframe": f"{spec.timeframe_amount}{spec.timeframe_unit[0]}",
            "ttl_seconds": spec.ttl_seconds,
            "max_points": spec.max_points,
        }
        for spec in CHART_RANGES.values()
    ]


__all__ = [
    "MAX_PROVIDER_CALLS_PER_MINUTE",
    "MAX_SYMBOLS_PER_REQUEST",
    "RANGE_KEYS",
    "UNAVAILABLE_BROKER_NOT_CONFIGURED",
    "UNAVAILABLE_INVALID_SYMBOL",
    "UNAVAILABLE_NO_BARS",
    "UNAVAILABLE_PROVIDER_BUDGET",
    "UNAVAILABLE_PROVIDER_UNREADABLE",
    "ChartBatch",
    "ChartCache",
    "ChartRequestError",
    "ChartSeries",
    "asset_class_for",
    "normalize_symbols",
    "ranges_payload",
    "shape_series",
]
