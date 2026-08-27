"""Phase 1: Alpaca historical 15-minute bars -> canonical Parquet.

This module is the only place the project talks to a market-data provider. It
is deliberately narrow: one provider (Alpaca), one asset class (US equities),
one feed (IEX), one timeframe (15m), and one fixed universe. There is no
trading client here and no order path anywhere in this repository.

Data-quality validation (duplicates, OHLC relationships, session gaps) is
Phase 2 and is intentionally absent. This module performs only the minimal
normalization required to produce a stable canonical dataset.
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
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models.bars import Bar
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

PROVIDER = "alpaca"
FEED = DataFeed.IEX

#: The frozen V0.1 universe (docs/SPEC.md section 3.1).
SUPPORTED_SYMBOLS: tuple[str, ...] = ("SPY", "QQQ", "AAPL", "MSFT", "NVDA")

#: The only timeframe this phase supports (docs/SPEC.md section 3.2).
SUPPORTED_TIMEFRAME = "15m"

#: `--start` / `--end` are US market calendar dates in this timezone.
MARKET_TIMEZONE = ZoneInfo("America/New_York")

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

MISSING_CREDENTIALS_MESSAGE = (
    f"Alpaca credentials are not configured.\nSet {_API_KEY_ENV} and {_SECRET_KEY_ENV}."
)


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
    """Uppercase `symbol` and confirm it is in the V0.1 universe."""
    normalized = symbol.strip().upper()
    if normalized not in SUPPORTED_SYMBOLS:
        supported = ", ".join(SUPPORTED_SYMBOLS)
        raise HistoricalDataError(
            f"Unsupported symbol: {symbol!r}. Supported symbols are: {supported}."
        )
    return normalized


def normalize_timeframe(timeframe: str) -> str:
    """Confirm `timeframe` is the single timeframe this phase supports."""
    normalized = timeframe.strip().lower()
    if normalized != SUPPORTED_TIMEFRAME:
        raise HistoricalDataError(
            f"Unsupported timeframe: {timeframe!r}. Only {SUPPORTED_TIMEFRAME!r} is supported."
        )
    return normalized


def parse_market_date(value: str, field: str) -> date:
    """Parse a strict ``YYYY-MM-DD`` US market calendar date."""
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
    start_date = parse_market_date(start, "start")
    end_date = parse_market_date(end, "end")
    if end_date < start_date:
        raise HistoricalDataError(
            f"Invalid date range: --end {end_date.isoformat()} is before "
            f"--start {start_date.isoformat()}."
        )
    return start_date, end_date


def to_request_window(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    """Convert inclusive market calendar dates to a UTC ``[start, end)`` window.

    `end_date` is inclusive for the user, so the request boundary becomes
    midnight America/New_York on the following day, expressed in UTC.
    """
    start_local = datetime.combine(start_date, time.min, tzinfo=MARKET_TIMEZONE)
    end_local = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=MARKET_TIMEZONE)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


# --------------------------------------------------------------------------
# Alpaca access
# --------------------------------------------------------------------------


def credentials_configured() -> bool:
    """Report whether both credential environment variables hold a value."""
    return bool(os.environ.get(_API_KEY_ENV, "").strip()) and bool(
        os.environ.get(_SECRET_KEY_ENV, "").strip()
    )


def create_client() -> StockHistoricalDataClient:
    """Build a market-data-only Alpaca client from the process environment."""
    if not credentials_configured():
        raise HistoricalDataError(MISSING_CREDENTIALS_MESSAGE)
    return StockHistoricalDataClient(
        api_key=os.environ[_API_KEY_ENV].strip(),
        secret_key=os.environ[_SECRET_KEY_ENV].strip(),
    )


def build_bars_request(symbol: str, start: datetime, end: datetime) -> StockBarsRequest:
    """Build the 15-minute IEX stock-bars request for one symbol.

    `start` and `end` must be timezone-aware. alpaca-py converts them to naive
    UTC on the request object, so passing aware datetimes is what keeps the
    window unambiguous.
    """
    return StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(15, TimeFrameUnit.Minute),
        start=start,
        end=end,
        feed=FEED,
    )


def _api_error_text(exc: APIError) -> str:
    try:
        return str(exc.message)
    except Exception:  # noqa: BLE001 - the provider payload is not always JSON
        return str(exc)


def fetch_bars(
    client: StockHistoricalDataClient,
    symbol: str,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Request bars from Alpaca and return them in the canonical schema."""
    request = build_bars_request(symbol, start, end)
    try:
        barset = client.get_stock_bars(request)
    except APIError as exc:
        raise HistoricalDataError(
            f"Alpaca rejected the historical data request: {_api_error_text(exc)}"
        ) from exc
    bars = getattr(barset, "data", {}).get(symbol, [])
    return bars_to_dataframe(bars, symbol)


# --------------------------------------------------------------------------
# Canonical conversion
# --------------------------------------------------------------------------


def bars_to_dataframe(bars: Iterable[Bar], symbol: str) -> pd.DataFrame:
    """Convert Alpaca bars to the canonical DataFrame.

    Timestamps become timezone-aware UTC, rows are ordered ascending, and the
    columns are exactly `CANONICAL_COLUMNS` in that order. `trade_count` and
    `vwap` stay nullable because Alpaca does not always provide them.
    """
    normalized_symbol = normalize_symbol(symbol)
    records = [
        {
            "timestamp": bar.timestamp,
            "symbol": normalized_symbol,
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


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def output_stem(symbol: str, timeframe: str, start: date, end: date) -> str:
    """Deterministic, date-ranged basename, e.g. ``SPY_15m_2025-01-01_2025-12-31``."""
    return f"{symbol}_{timeframe}_{start.isoformat()}_{end.isoformat()}"


def output_paths(
    output_dir: Path, symbol: str, timeframe: str, start: date, end: date
) -> tuple[Path, Path]:
    """Return the ``(parquet, metadata)`` paths for one download."""
    stem = output_stem(symbol, timeframe, start, end)
    directory = Path(output_dir)
    return directory / f"{stem}.parquet", directory / f"{stem}.metadata.json"


def _atomic_write(path: Path, write: Callable[[Path], None]) -> None:
    """Write via a sibling temporary file and rename, so a crash cannot truncate `path`."""
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
    _atomic_write(path, lambda target: frame.to_parquet(target, engine="pyarrow", index=False))


def build_metadata(
    symbol: str,
    timeframe: str,
    start: date,
    end: date,
    row_count: int,
    parquet_filename: str,
    retrieved_at: datetime,
) -> dict[str, object]:
    """Build the reproducibility sidecar. Never include credentials or account data."""
    return {
        "provider": PROVIDER,
        "feed": FEED.value,
        "symbol": symbol,
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
    _atomic_write(path, lambda target: target.write_text(payload, encoding="utf-8"))


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
) -> DownloadResult:
    """Download one symbol's bars and write the Parquet file plus its metadata sidecar."""
    resolved_symbol = normalize_symbol(symbol)
    resolved_timeframe = normalize_timeframe(timeframe)
    start_date, end_date = resolve_date_range(start, end)
    request_start, request_end = to_request_window(start_date, end_date)

    data_client = create_client() if client is None else client
    frame = fetch_bars(data_client, resolved_symbol, request_start, request_end)
    if frame.empty:
        raise HistoricalDataError(
            f"Alpaca returned no {resolved_timeframe} bars for {resolved_symbol} between "
            f"{start_date.isoformat()} and {end_date.isoformat()} on the {FEED.value} feed. "
            "No files were written."
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
    "MARKET_TIMEZONE",
    "MISSING_CREDENTIALS_MESSAGE",
    "PROVIDER",
    "SUPPORTED_SYMBOLS",
    "SUPPORTED_TIMEFRAME",
    "DownloadResult",
    "HistoricalDataError",
    "bars_to_dataframe",
    "build_bars_request",
    "build_metadata",
    "create_client",
    "credentials_configured",
    "download_bars",
    "fetch_bars",
    "normalize_symbol",
    "normalize_timeframe",
    "output_paths",
    "output_stem",
    "parse_market_date",
    "resolve_date_range",
    "to_request_window",
    "write_metadata",
    "write_parquet",
]
