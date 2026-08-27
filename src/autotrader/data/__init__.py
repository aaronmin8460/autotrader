"""Historical market data acquisition, Parquet storage, and validation.

Phase 1 provides Alpaca 15-minute stock bars on the IEX feed. Phase 2 adds
read-only validation of a stored dataset against that canonical contract.
"""

from autotrader.data.historical import (
    CANONICAL_COLUMNS,
    SUPPORTED_SYMBOLS,
    SUPPORTED_TIMEFRAME,
    DownloadResult,
    HistoricalDataError,
    download_bars,
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
    "SUPPORTED_SYMBOLS",
    "SUPPORTED_TIMEFRAME",
    "DownloadResult",
    "HistoricalDataError",
    "ValidationInputError",
    "ValidationIssue",
    "ValidationResult",
    "download_bars",
    "validate_frame",
    "validate_parquet_file",
]
