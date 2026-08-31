"""Research-only historical-bars fetch for symbols outside the production
universe.

The production `normalize_symbol` guard exists so the *runtime* can never
quietly trade an eleventh symbol; that property is untouched here. Historical
research legitimately needs bars for symbols the runtime will never trade, so
this module rebuilds the same read-only request through the same shipped
pieces — `build_bars_request`, the canonical schema, the chunking, the
research split adjustment — with the only difference being that the ticker
list is the study's, not the runtime's. Nothing here can place, cancel, or
size an order; the client type cannot trade.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import pandas as pd

from studies.equity_v1_v5.dataset import RESEARCH_ADJUSTMENT, DatasetError, _chunks


def _plain_ticker(symbol: str) -> str:
    if not isinstance(symbol, str) or not symbol.strip():
        raise DatasetError(f"A ticker must be a non-empty string, got {symbol!r}.")
    return symbol.strip().upper()


def download_raw_any(
    symbols: Sequence[str],
    start: date,
    end: date,
    *,
    client: object | None = None,
    progress: object | None = None,
    adjustment: object | None = RESEARCH_ADJUSTMENT,
) -> dict[str, pd.DataFrame]:
    """`download_raw`, minus the production ticker whitelist."""
    from alpaca.common.exceptions import APIError

    from autotrader.data.historical import to_canonical_frame
    from autotrader.equity.data import (
        EquityDataError,
        _bars_by_symbol,
        build_bars_request,
        create_client,
        to_request_window,
    )

    tickers = [_plain_ticker(symbol) for symbol in symbols]
    data_client = create_client() if client is None else client
    collected: dict[str, list[pd.DataFrame]] = {ticker: [] for ticker in tickers}
    for chunk_start, chunk_end in _chunks(start, end):
        window_start, window_end = to_request_window(chunk_start, chunk_end)
        request = build_bars_request(
            tickers if len(tickers) > 1 else tickers[0], window_start, window_end, adjustment
        )
        try:
            barset = data_client.get_stock_bars(request)
        except APIError as exc:
            raise EquityDataError(f"Provider rejected the bars request: {exc}") from exc
        by_symbol = _bars_by_symbol(barset)
        frames = {
            ticker: to_canonical_frame(by_symbol.get(ticker, []), ticker) for ticker in tickers
        }
        for ticker, frame in frames.items():
            if not frame.empty:
                collected[ticker].append(frame)
        if progress is not None:
            progress(chunk_start, chunk_end, {s: len(f) for s, f in frames.items()})

    result: dict[str, pd.DataFrame] = {}
    for ticker, parts in collected.items():
        if not parts:
            raise DatasetError(
                f"The provider returned no bars for {ticker} between "
                f"{start.isoformat()} and {end.isoformat()}."
            )
        merged = pd.concat(parts, ignore_index=True)
        result[ticker] = merged.sort_values("timestamp", kind="stable", ignore_index=True)
    return result


__all__ = ["download_raw_any"]
