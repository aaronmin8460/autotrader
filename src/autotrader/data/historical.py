"""C1: Alpaca historical 15-minute **crypto** bars -> canonical Parquet.

This module is the only place the project talks to a market-data provider. It
is deliberately narrow: one provider (Alpaca), one asset class (crypto spot),
one feed (Alpaca's US crypto feed), one timeframe (15m), and one fixed pair
universe. There is no trading client here.

**Crypto, not equities.** V0.2 replaced the equity path outright rather than
growing an asset-class switch; the completed equity milestone is archived at
the Git tag `equity-v0.1-phase7`. There is no `StockHistoricalDataClient`, no
IEX feed, and no stock symbol in the active system.

**Crypto trades continuously.** `--start` and `--end` are **UTC** calendar
dates, not exchange-session dates: there is no market open, no close, and no
holiday. Weekend and overnight bars are ordinary data.

**Credentials are optional here.** Alpaca serves crypto market data without
authentication, so a download works with no key configured. When
`ALPACA_API_KEY` / `ALPACA_SECRET_KEY` are present they are passed through,
because an authenticated client gets better provider rate limits. Paper order
submission still requires them (`autotrader.execution`).

Data-quality validation (duplicates, OHLC relationships, gaps) is C2 and is
intentionally absent. This module performs only the minimal normalization
required to produce a stable canonical dataset.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import CryptoFeed
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.models.bars import Bar
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

PROVIDER = "alpaca"

#: Alpaca's crypto feed. There is exactly one, and it is not an equity feed.
FEED = CryptoFeed.US

#: The frozen V0.2 universe (docs/SPEC.md section 3.1). Canonical pair form.
SUPPORTED_SYMBOLS: tuple[str, ...] = ("BTC/USD", "ETH/USD")

#: The only quote currency this milestone supports.
QUOTE_CURRENCY = "USD"

#: The only timeframe this milestone supports (docs/SPEC.md section 3.2).
SUPPORTED_TIMEFRAME = "15m"

#: The smallest instant the provider's timestamps distinguish. Used only to
#: turn "up to and including this day" into a boundary an inclusive-`end` API
#: understands.
RESOLUTION = timedelta(microseconds=1)

#: A slash cannot appear in a flat filename, so the canonical pair symbol is
#: slugged for the filesystem only. The domain symbol is never rewritten.
SYMBOL_SEPARATOR = "/"
SLUG_SEPARATOR = "_"

#: The canonical historical-bar contract. Order is part of the contract.
CANONICAL_COLUMNS: tuple[str, ...] = (
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

_NUMERIC_COLUMNS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "trade_count",
    "vwap",
)

_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")

_API_KEY_ENV = "ALPACA_API_KEY"
_SECRET_KEY_ENV = "ALPACA_SECRET_KEY"


class HistoricalDataError(Exception):
    """An expected, user-facing failure. The CLI reports these without a traceback."""


@dataclass(frozen=True)
class DownloadResult:
    """What a completed download produced."""

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


def normalize_symbol(symbol: str) -> str:
    """Uppercase `symbol` and confirm it is in the V0.2 pair universe.

    Only the canonical pair form is accepted. `BTCUSD` is **not** silently
    reinterpreted as `BTC/USD`: the slash is part of the provider's symbol,
    and quietly rewriting it would let two spellings of one market drift apart
    in stored data.
    """
    if not isinstance(symbol, str):
        raise HistoricalDataError(f"symbol must be a string, got {type(symbol).__name__}.")
    normalized = symbol.strip().upper()
    if normalized not in SUPPORTED_SYMBOLS:
        supported = ", ".join(SUPPORTED_SYMBOLS)
        raise HistoricalDataError(
            f"Unsupported symbol: {symbol!r}. Supported symbols are: {supported}."
        )
    return normalized


def filesystem_slug(symbol: str) -> str:
    """The filesystem-safe form of a canonical pair, e.g. ``BTC/USD`` -> ``BTC_USD``.

    Used for filenames only. The stored DataFrame, the metadata sidecar, and
    every domain model keep the canonical `BTC/USD` spelling.
    """
    return normalize_symbol(symbol).replace(SYMBOL_SEPARATOR, SLUG_SEPARATOR)


def normalize_timeframe(timeframe: str) -> str:
    """Confirm `timeframe` is the single timeframe this milestone supports."""
    normalized = timeframe.strip().lower()
    if normalized != SUPPORTED_TIMEFRAME:
        raise HistoricalDataError(
            f"Unsupported timeframe: {timeframe!r}. Only {SUPPORTED_TIMEFRAME!r} is supported."
        )
    return normalized


def parse_utc_date(value: str, field: str) -> date:
    """Parse a strict ``YYYY-MM-DD`` UTC calendar date.

    Crypto trades continuously, so a day is a UTC calendar day - there is no
    exchange session to anchor it to.
    """
    candidate = value.strip()
    if not _DATE_PATTERN.fullmatch(candidate):
        raise HistoricalDataError(
            f"Invalid --{field} date: {value!r}. Expected the format YYYY-MM-DD."
        )
    try:
        return datetime.strptime(candidate, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HistoricalDataError(
            f"Invalid --{field} date: {value!r}. Expected the format YYYY-MM-DD."
        ) from exc


def resolve_date_range(start: str, end: str) -> tuple[date, date]:
    """Parse both boundaries and reject an inverted range."""
    start_date = parse_utc_date(start, "start")
    end_date = parse_utc_date(end, "end")
    if end_date < start_date:
        raise HistoricalDataError(
            f"Invalid date range: --end {end_date.isoformat()} is before "
            f"--start {start_date.isoformat()}."
        )
    return start_date, end_date


def to_request_window(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    """Convert inclusive UTC calendar dates to the closed UTC window to request.

    Alpaca's crypto bars endpoint treats `end` as **inclusive**, and a 24/7
    market has a bar stamped exactly at midnight. Asking for the next day's
    midnight would therefore return one bar belonging to the day after the one
    the user asked for - a bar dated 2026-01-01 inside a file named
    ``..._2025-12-31``. The boundary is the last instant of `end_date` instead,
    so a one-day request returns exactly the 96 bars of that day.
    """
    start_utc = datetime.combine(start_date, time.min, tzinfo=UTC)
    next_midnight = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=UTC)
    return start_utc, next_midnight - RESOLUTION


# --------------------------------------------------------------------------
# Alpaca access
# --------------------------------------------------------------------------


def credentials_configured() -> bool:
    """Report whether both credential environment variables hold a value."""
    return bool(os.environ.get(_API_KEY_ENV, "").strip()) and bool(
        os.environ.get(_SECRET_KEY_ENV, "").strip()
    )


def create_client() -> CryptoHistoricalDataClient:
    """Build a market-data-only Alpaca crypto client.

    Alpaca serves crypto bars unauthenticated, so this never requires a
    credential to succeed. Configured credentials are passed through when both
    are present, which raises the provider's rate limit; a half-configured
    environment is treated as unconfigured rather than sent as a broken
    credential pair.
    """
    if credentials_configured():
        return CryptoHistoricalDataClient(
            api_key=os.environ[_API_KEY_ENV].strip(),
            secret_key=os.environ[_SECRET_KEY_ENV].strip(),
        )
    return CryptoHistoricalDataClient()


def build_bars_request(symbol: str, start: datetime, end: datetime) -> CryptoBarsRequest:
    """Build the 15-minute crypto-bars request for one pair.

    `start` and `end` must be timezone-aware. alpaca-py converts them to naive
    UTC on the request object, so passing aware datetimes is what keeps the
    window unambiguous. The feed is not a request field for crypto - it is an
    argument to the client call.
    """
    return CryptoBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(15, TimeFrameUnit.Minute),
        start=start,
        end=end,
    )


def _api_error_text(exc: APIError) -> str:
    try:
        return str(exc.message)
    except Exception:  # noqa: BLE001 - the provider payload is not always JSON
        return str(exc)


def fetch_bars(
    client: CryptoHistoricalDataClient,
    symbol: str,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Request bars from Alpaca and return them in the canonical schema."""
    request = build_bars_request(symbol, start, end)
    try:
        barset = client.get_crypto_bars(request, feed=FEED)
    except APIError as exc:
        raise HistoricalDataError(
            f"Alpaca rejected the historical data request: {_api_error_text(exc)}"
        ) from exc
    bars = getattr(barset, "data", {}).get(symbol, [])
    return bars_to_dataframe(bars, symbol)


# --------------------------------------------------------------------------
# Canonical conversion
# --------------------------------------------------------------------------


def to_canonical_frame(bars: Iterable[Bar], symbol: str) -> pd.DataFrame:
    """Convert Alpaca bars to the canonical DataFrame under an **already
    validated** symbol.

    The canonical bar contract itself - column set, column order, UTC-aware
    timestamps, ascending rows, float64 numerics, nullable `trade_count` and
    `vwap` - is one thing, and which symbols a caller may ask for is another.
    This function owns the first and deliberately knows nothing about the
    second, so the equity boundary can produce the same canonical frame under
    its own universe without a second copy of the conversion drifting away from
    this one. Callers validate the symbol first; `bars_to_dataframe` is the
    crypto-universe form that does.
    """
    records = [
        {
            "timestamp": bar.timestamp,
            "symbol": symbol,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "trade_count": bar.trade_count,
            "vwap": bar.vwap,
        }
        for bar in bars
    ]
    frame = pd.DataFrame.from_records(records, columns=list(CANONICAL_COLUMNS))
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["symbol"] = frame["symbol"].astype("string")
    for column in _NUMERIC_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    return frame.sort_values("timestamp", kind="stable", ignore_index=True)


def bars_to_dataframe(bars: Iterable[Bar], symbol: str) -> pd.DataFrame:
    """Convert Alpaca bars to the canonical DataFrame.

    Timestamps become timezone-aware UTC, rows are ordered ascending, and the
    columns are exactly `CANONICAL_COLUMNS` in that order. The stored `symbol`
    is the canonical pair form, slash included. `trade_count` and `vwap` stay
    nullable because Alpaca does not always provide them.
    """
    return to_canonical_frame(bars, normalize_symbol(symbol))


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def output_stem(symbol: str, timeframe: str, start: date, end: date) -> str:
    """Deterministic, date-ranged basename, e.g. ``BTC_USD_15m_2025-01-01_2025-12-31``."""
    return f"{filesystem_slug(symbol)}_{timeframe}_{start.isoformat()}_{end.isoformat()}"


def output_paths(
    output_dir: Path, symbol: str, timeframe: str, start: date, end: date
) -> tuple[Path, Path]:
    """Return the ``(parquet, metadata)`` paths for one download."""
    stem = output_stem(symbol, timeframe, start, end)
    directory = Path(output_dir)
    return directory / f"{stem}.parquet", directory / f"{stem}.metadata.json"


def atomic_write(path: Path, write: Callable[[Path], None]) -> None:
    """Write via a sibling temporary file and rename, so a crash cannot truncate `path`.

    Public because the equity market-data boundary writes its Parquet and its
    metadata sidecar the same way, and a second copy of a crash-safety helper
    is a second thing that can be subtly wrong.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        write(temp_path)
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


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

    `symbol` is the canonical pair; `symbol_slug` records the filesystem form
    so the two spellings are documented together rather than inferred.
    """
    return {
        "provider": PROVIDER,
        "feed": FEED.value,
        "symbol": normalize_symbol(symbol),
        "symbol_slug": filesystem_slug(symbol),
        "timeframe": timeframe,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
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
    client: CryptoHistoricalDataClient | None = None,
) -> DownloadResult:
    """Download one pair's bars and write the Parquet file plus its metadata sidecar."""
    resolved_symbol = normalize_symbol(symbol)
    resolved_timeframe = normalize_timeframe(timeframe)
    start_date, end_date = resolve_date_range(start, end)
    request_start, request_end = to_request_window(start_date, end_date)

    data_client = create_client() if client is None else client
    frame = fetch_bars(data_client, resolved_symbol, request_start, request_end)
    if frame.empty:
        raise HistoricalDataError(
            f"Alpaca returned no {resolved_timeframe} bars for {resolved_symbol} between "
            f"{start_date.isoformat()} and {end_date.isoformat()} on the {FEED.value} crypto "
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
    return DownloadResult(
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
    "FEED",
    "PROVIDER",
    "QUOTE_CURRENCY",
    "SLUG_SEPARATOR",
    "SUPPORTED_SYMBOLS",
    "SUPPORTED_TIMEFRAME",
    "SYMBOL_SEPARATOR",
    "DownloadResult",
    "HistoricalDataError",
    "atomic_write",
    "bars_to_dataframe",
    "build_bars_request",
    "build_metadata",
    "create_client",
    "credentials_configured",
    "download_bars",
    "fetch_bars",
    "filesystem_slug",
    "normalize_symbol",
    "normalize_timeframe",
    "output_paths",
    "output_stem",
    "parse_utc_date",
    "resolve_date_range",
    "to_canonical_frame",
    "to_request_window",
    "write_metadata",
    "write_parquet",
]
