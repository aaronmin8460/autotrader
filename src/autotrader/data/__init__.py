"""Historical market data acquisition and Parquet storage.

Phase 1 provides Alpaca 15-minute stock bars on the IEX feed. Data-quality
validation is Phase 2 and is not implemented here.
"""

from autotrader.data.historical import (
    CANONICAL_COLUMNS,
    SUPPORTED_SYMBOLS,
    SUPPORTED_TIMEFRAME,
    DownloadResult,
    HistoricalDataError,
    download_bars,
)

__all__ = [
    "CANONICAL_COLUMNS",
    "SUPPORTED_SYMBOLS",
    "SUPPORTED_TIMEFRAME",
    "DownloadResult",
    "HistoricalDataError",
    "download_bars",
]
