"""Historical crypto market data acquisition, Parquet storage, and validation.

C1 provides Alpaca 15-minute crypto bars for BTC/USD and ETH/USD on Alpaca's
US crypto feed. C2 adds read-only validation of a stored dataset against that
canonical contract. Neither knows anything about exchange sessions: crypto
trades 24/7.
"""

from autotrader.data.historical import (
    CANONICAL_COLUMNS,
    QUOTE_CURRENCY,
    SUPPORTED_SYMBOLS,
    SUPPORTED_TIMEFRAME,
    DownloadResult,
    HistoricalDataError,
    download_bars,
    filesystem_slug,
    normalize_symbol,
)
from autotrader.data.validation import (
    ValidationInputError,
    ValidationIssue,
    ValidationResult,
    validate_frame,
    validate_parquet_file,
)

__all__ = [
    "CANONICAL_COLUMNS",
    "QUOTE_CURRENCY",
    "SUPPORTED_SYMBOLS",
    "SUPPORTED_TIMEFRAME",
    "DownloadResult",
    "HistoricalDataError",
    "ValidationInputError",
    "ValidationIssue",
    "ValidationResult",
    "download_bars",
    "filesystem_slug",
    "normalize_symbol",
    "validate_frame",
    "validate_parquet_file",
]
