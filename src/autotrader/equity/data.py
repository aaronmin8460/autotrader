"""Equity V0.2: Alpaca historical 15-minute **stock** bars -> canonical Parquet.

The equity counterpart of C1, and the only place this branch talks to a stock
market-data provider. Deliberately narrow in the same way: one provider
(Alpaca), one asset class (US equities), one feed, one timeframe (15m), and one
frozen ten-symbol universe.

**The feed is IEX, and that is a subscription fact rather than a preference.**
Alpaca's Basic plan serves `iex`; `sip` requires a paid data subscription and
returns a 403 without one. Asking for the feed this account actually has is
what makes a download work at all, and it is stated once here so nothing
downstream has to guess. IEX is a single-venue feed: it carries a real subset
of consolidated volume, and bars for a symbol with no IEX prints in an interval
are simply absent rather than zero-volume. Both are visible in the data and
neither is corrected for.

**Credentials are required.** Unlike crypto, Alpaca does not serve stock market
data unauthenticated, so `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` must both be
set. A half-configured environment is refused rather than sent as a broken
credential pair.

**Dates are US market calendar dates.** `--start` and `--end` name exchange
days in `MARKET_TIMEZONE`, which is what an equity operator means by a date,
and the request window is converted to UTC here so nothing downstream has to
know the difference. Stored timestamps are UTC, always.

**Batching is the point of `fetch_bars_for_symbols`.** Alpaca's stock bars
endpoint accepts a list of symbols and answers with one response keyed by
symbol, so the runtime's ten-symbol universe costs one request per cycle rather
than ten. The call counter exists so the later shared crypto+equity API budget
has a real number to start from.

**Extended-hours bars are returned and are not filtered here.** This module
reports what the provider published; deciding which of those candles belong to
the regular session is `autotrader.equity.session`'s job, and doing it in two
places would be doing it twice.

Structural validation (duplicates, OHLC relationships, symbol universe) is C2's
`validate_frame`, which takes the universe to check against; this module does
only the minimal normalization needed to produce a stable canonical dataset.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models.bars import Bar
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from autotrader.data.historical import (
    CANONICAL_COLUMNS,
    PROVIDER,
    RESOLUTION,
    atomic_write,
    to_canonical_frame,
)
from autotrader.equity import (
    EQUITY_SYMBOLS,
    EQUITY_TIMEFRAME,
    MARKET_TIMEZONE,
    MARKET_TIMEZONE_NAME,
    EquityError,
    normalize_symbol,
    normalize_timeframe,
)

#: Alpaca's IEX stock feed - the one an Alpaca Basic account is entitled to.
FEED = DataFeed.IEX

#: The bar timeframe, as the SDK's own type. Built once: it is a value, and
#: rebuilding it per request would only create ways for two of them to differ.
TIMEFRAME = TimeFrame(15, TimeFrameUnit.Minute)

_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")

_API_KEY_ENV = "ALPACA_API_KEY"
_SECRET_KEY_ENV = "ALPACA_SECRET_KEY"

MISSING_CREDENTIALS_MESSAGE = (
    "Alpaca credentials are not configured, and stock market data - unlike crypto - "
    f"cannot be requested without them.\nSet {_API_KEY_ENV} and {_SECRET_KEY_ENV}."
)


class EquityDataError(EquityError):
    """An expected, user-facing equity market-data failure."""


@dataclass(frozen=True)
class EquityDownloadResult:
    """What one completed equity download produced."""

    symbol: str
    timeframe: str
    start: date
    end: date
    feed: str
    row_count: int
    parquet_path: Path
    metadata_path: Path


# --------------------------------------------------------------------------
# Input normalization
# --------------------------------------------------------------------------


def parse_market_date(value: str, field: str) -> date:
    """Parse a strict ``YYYY-MM-DD`` US market calendar date.

    A market date, not a UTC date: equities trade a session, and an operator
    asking for ``2026-08-26`` means that exchange day in New York.
    """
    candidate = value.strip()
    if not _DATE_PATTERN.fullmatch(candidate):
        raise EquityDataError(f"Invalid --{field} date: {value!r}. Expected the format YYYY-MM-DD.")
    try:
        return datetime.strptime(candidate, "%Y-%m-%d").date()
    except ValueError as exc:
        raise EquityDataError(
            f"Invalid --{field} date: {value!r}. Expected the format YYYY-MM-DD."
        ) from exc


def resolve_date_range(start: str, end: str) -> tuple[date, date]:
    """Parse both boundaries and reject an inverted range."""
    start_date = parse_market_date(start, "start")
    end_date = parse_market_date(end, "end")
    if end_date < start_date:
        raise EquityDataError(
            f"Invalid date range: --end {end_date.isoformat()} is before "
            f"--start {start_date.isoformat()}."
        )
    return start_date, end_date


def to_request_window(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    """Convert inclusive market calendar dates to the closed UTC window to request.

    Both boundaries are midnight in `MARKET_TIMEZONE` converted to UTC, so a
    request for a single exchange day covers that day's whole session and its
    surrounding extended hours regardless of which side of a daylight-saving
    change it falls on. `end` is the last instant of `end_date` rather than the
    next midnight, matching C1's inclusive-`end` handling.
    """
    start_local = datetime.combine(start_date, time.min, tzinfo=MARKET_TIMEZONE)
    next_midnight_local = datetime.combine(
        end_date + timedelta(days=1), time.min, tzinfo=MARKET_TIMEZONE
    )
    return start_local.astimezone(UTC), next_midnight_local.astimezone(UTC) - RESOLUTION


# --------------------------------------------------------------------------
# Alpaca access
# --------------------------------------------------------------------------


def credentials_configured() -> bool:
    """Report whether both credential environment variables hold a value."""
    return bool(os.environ.get(_API_KEY_ENV, "").strip()) and bool(
        os.environ.get(_SECRET_KEY_ENV, "").strip()
    )


def create_client() -> StockHistoricalDataClient:
    """Build a market-data-only Alpaca stock client from the process environment.

    Market data only. This is not a trading client, cannot place an order, and
    is a different type from the one that can: the single paper trading client
    factory lives in `autotrader.execution.paper` and nothing here reaches it.
    """
    if not credentials_configured():
        raise EquityDataError(MISSING_CREDENTIALS_MESSAGE)
    return StockHistoricalDataClient(
        api_key=os.environ[_API_KEY_ENV].strip(),
        secret_key=os.environ[_SECRET_KEY_ENV].strip(),
    )


def build_bars_request(
    symbols: str | list[str], start: datetime, end: datetime
) -> StockBarsRequest:
    """Build the 15-minute IEX stock-bars request for one or many symbols.

    `start` and `end` must be timezone-aware. alpaca-py converts them to naive
    UTC on the request object, so passing aware datetimes is what keeps the
    window unambiguous. Unlike the crypto endpoint, the feed *is* a request
    field here.
    """
    return StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TIMEFRAME,
        start=start,
        end=end,
        feed=FEED,
    )


def _api_error_text(exc: APIError) -> str:
    try:
        return str(exc.message)
    except Exception:  # noqa: BLE001 - the provider payload is not always JSON
        return str(exc)


def _bars_by_symbol(barset: object) -> dict[str, list[Bar]]:
    """The response's per-symbol bar lists, whatever shape it arrived in."""
    data = getattr(barset, "data", None)
    if not isinstance(data, dict):
        return {}
    return {str(key): list(value) for key, value in data.items()}


def fetch_bars_for_symbols(
    client: StockHistoricalDataClient,
    symbols: Iterable[str],
    start: datetime,
    end: datetime,
) -> dict[str, pd.DataFrame]:
    """Request bars for several symbols at once, canonical frame per symbol.

    One provider call for the whole list. Every requested symbol appears in the
    result, with an empty canonical frame when the provider returned nothing
    for it - a missing key would otherwise read as "no such symbol" when it
    actually means "no prints on this feed in this window", and the runtime has
    to be able to tell those apart from a symbol it forgot to ask for.
    """
    tickers = [normalize_symbol(symbol) for symbol in symbols]
    if not tickers:
        raise EquityDataError("At least one symbol is required.")
    request = build_bars_request(tickers if len(tickers) > 1 else tickers[0], start, end)
    try:
        barset = client.get_stock_bars(request)
    except APIError as exc:
        raise EquityDataError(
            f"Alpaca rejected the stock bars request: {_api_error_text(exc)}"
        ) from exc
    returned = _bars_by_symbol(barset)
    return {ticker: to_canonical_frame(returned.get(ticker, []), ticker) for ticker in tickers}


def fetch_bars(
    client: StockHistoricalDataClient,
    symbol: str,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Request one symbol's bars and return them in the canonical schema."""
    ticker = normalize_symbol(symbol)
    return fetch_bars_for_symbols(client, [ticker], start, end)[ticker]


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def output_stem(symbol: str, timeframe: str, start: date, end: date) -> str:
    """Deterministic, date-ranged basename, e.g. ``SPY_15m_2026-01-02_2026-08-27``.

    No slug step: an equity ticker contains no separator that a filename
    cannot, which is exactly why the crypto path needs one and this does not.
    """
    return f"{normalize_symbol(symbol)}_{timeframe}_{start.isoformat()}_{end.isoformat()}"


def output_paths(
    output_dir: Path, symbol: str, timeframe: str, start: date, end: date
) -> tuple[Path, Path]:
    """Return the ``(parquet, metadata)`` paths for one download."""
    stem = output_stem(symbol, timeframe, start, end)
    directory = Path(output_dir)
    return directory / f"{stem}.parquet", directory / f"{stem}.metadata.json"


def write_parquet(frame: pd.DataFrame, path: Path) -> None:
    """Persist the canonical frame to Parquet atomically."""
    atomic_write(path, lambda target: frame.to_parquet(target, engine="pyarrow", index=False))


def build_metadata(
    symbol: str,
    timeframe: str,
    start: date,
    end: date,
    row_count: int,
    parquet_filename: str,
    retrieved_at: datetime,
) -> dict[str, object]:
    """Build the reproducibility sidecar. Never include credentials or account data.

    `date_timezone` records that the requested dates are exchange days rather
    than UTC days, which is the one thing about an equity dataset that cannot
    be recovered from the rows themselves.
    """
    return {
        "provider": PROVIDER,
        "asset_class": "us_equity",
        "feed": FEED.value,
        "symbol": normalize_symbol(symbol),
        "timeframe": timeframe,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "date_timezone": MARKET_TIMEZONE_NAME,
        "timestamp_timezone": "UTC",
        "retrieved_at_utc": retrieved_at.astimezone(UTC).isoformat(),
        "row_count": row_count,
        "parquet_filename": parquet_filename,
    }


def write_metadata(metadata: dict[str, object], path: Path) -> None:
    """Persist the sidecar metadata atomically."""
    payload = json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    atomic_write(path, lambda target: target.write_text(payload, encoding="utf-8"))


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def download_bars(
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    output_dir: Path,
    client: StockHistoricalDataClient | None = None,
) -> EquityDownloadResult:
    """Download one symbol's bars and write the Parquet file plus its sidecar."""
    resolved_symbol = normalize_symbol(symbol)
    resolved_timeframe = normalize_timeframe(timeframe)
    start_date, end_date = resolve_date_range(start, end)
    request_start, request_end = to_request_window(start_date, end_date)

    data_client = create_client() if client is None else client
    frame = fetch_bars(data_client, resolved_symbol, request_start, request_end)
    if frame.empty:
        raise EquityDataError(
            f"Alpaca returned no {resolved_timeframe} bars for {resolved_symbol} between "
            f"{start_date.isoformat()} and {end_date.isoformat()} on the {FEED.value} stock "
            "feed. No files were written."
        )

    parquet_path, metadata_path = output_paths(
        output_dir, resolved_symbol, resolved_timeframe, start_date, end_date
    )
    write_parquet(frame, parquet_path)
    write_metadata(
        build_metadata(
            symbol=resolved_symbol,
            timeframe=resolved_timeframe,
            start=start_date,
            end=end_date,
            row_count=len(frame),
            parquet_filename=parquet_path.name,
            retrieved_at=datetime.now(UTC),
        ),
        metadata_path,
    )
    return EquityDownloadResult(
        symbol=resolved_symbol,
        timeframe=resolved_timeframe,
        start=start_date,
        end=end_date,
        feed=FEED.value,
        row_count=len(frame),
        parquet_path=parquet_path,
        metadata_path=metadata_path,
    )


__all__ = [
    "CANONICAL_COLUMNS",
    "EQUITY_SYMBOLS",
    "EQUITY_TIMEFRAME",
    "FEED",
    "MISSING_CREDENTIALS_MESSAGE",
    "TIMEFRAME",
    "EquityDataError",
    "EquityDownloadResult",
    "build_bars_request",
    "build_metadata",
    "create_client",
    "credentials_configured",
    "download_bars",
    "fetch_bars",
    "fetch_bars_for_symbols",
    "output_paths",
    "output_stem",
    "parse_market_date",
    "resolve_date_range",
    "to_request_window",
    "write_metadata",
    "write_parquet",
]
