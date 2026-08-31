"""5-minute historical bars for the execution pilot (ledger §L7, §L8).

Same provider path, feed, and split adjustment as the 15m pipeline; the same
shipped per-bar regular-session filter (it judges each bar's own timestamp
against the calendar, so the bar interval is immaterial). Stored per symbol
with a small provenance sidecar.

Usage:
    python -m studies.equity_eda1_nextgen.run_5m_data --symbols SPY QQQ …
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from studies.equity_10_full import CALENDAR_END, CALENDAR_START, DATA_END, DATA_START
from studies.equity_eda1_nextgen import NEXTGEN_DATASETS
from studies.equity_eda1_nextgen.universe import INCUMBENTS
from studies.equity_v1_v5.calendar import read_snapshot, snapshot_path
from studies.equity_v1_v5.dataset import (
    _chunks,
    drop_duplicate_bars,
    file_sha256,
    frame_digest,
)


def filter_regular_session_5m(frame: pd.DataFrame, calendar) -> tuple[pd.DataFrame, int]:
    """Regular-session filter on the 5-minute grid.

    The shipped `session_bar_mask` requires bars on the runtime's 15m
    boundary (`floor_to_boundary`), which silently discards two of every
    three 5m bars; this variant keeps a bar iff it starts inside its own
    session and completes by the close, on the 5m grid.
    """
    from datetime import timedelta

    from autotrader.equity.session import market_date

    if frame.empty:
        return frame, 0
    first = market_date(frame["timestamp"].iloc[0].to_pydatetime())
    last = market_date(frame["timestamp"].iloc[-1].to_pydatetime())
    sessions = {s.session_date: s for s in calendar.sessions_between(first, last)}
    five = timedelta(minutes=5)
    mask = []
    for ts in frame["timestamp"]:
        moment = ts.to_pydatetime()
        session = sessions.get(market_date(moment))
        keep = (
            session is not None
            and session.open_utc <= moment
            and moment + five <= session.close_utc
            and (moment.minute % 5 == 0 and moment.second == 0)
        )
        mask.append(keep)
    kept = frame.loc[mask].reset_index(drop=True)
    return kept, len(frame) - len(kept)


FIVE_DIR = Path(NEXTGEN_DATASETS) / "bars-5m"


def _log(message: str) -> None:
    print(f"[{datetime.now(UTC).isoformat(timespec='seconds')}] {message}", flush=True)


def download_5m(symbol: str, client) -> pd.DataFrame:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    from autotrader.data.historical import to_canonical_frame
    from autotrader.equity.data import FEED, _bars_by_symbol, to_request_window
    from studies.equity_v1_v5.dataset import RESEARCH_ADJUSTMENT

    parts: list[pd.DataFrame] = []
    for chunk_start, chunk_end in _chunks(DATA_START, DATA_END):
        window_start, window_end = to_request_window(chunk_start, chunk_end)
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=window_start,
            end=window_end,
            feed=FEED,
            adjustment=RESEARCH_ADJUSTMENT,
        )
        barset = client.get_stock_bars(request)
        frame = to_canonical_frame(_bars_by_symbol(barset).get(symbol, []), symbol)
        if not frame.empty:
            parts.append(frame)
    if not parts:
        raise ValueError(f"No 5m bars for {symbol}.")
    merged = pd.concat(parts, ignore_index=True)
    return merged.sort_values("timestamp", kind="stable", ignore_index=True)


def build_symbol(symbol: str, client, calendar) -> str:
    stem = f"{symbol}_5m_{DATA_START.isoformat()}_{DATA_END.isoformat()}"
    target = FIVE_DIR / f"{stem}.session.parquet"
    sidecar = target.with_suffix(".provenance.json")
    if target.exists() and sidecar.exists():
        return f"{symbol}: exists"
    started = time.perf_counter()
    raw = download_5m(symbol, client)
    deduped, duplicates = drop_duplicate_bars(raw)
    regular, dropped = filter_regular_session_5m(deduped, calendar)
    FIVE_DIR.mkdir(parents=True, exist_ok=True)
    regular.to_parquet(target, engine="pyarrow", index=False)
    sidecar.write_text(
        json.dumps(
            {
                "symbol": symbol,
                "timeframe": "5m",
                "adjustment": "split",
                "raw_rows": len(raw),
                "duplicates_dropped": duplicates,
                "extended_dropped": dropped,
                "session_rows": len(regular),
                "frame_sha256": frame_digest(regular),
                "file_sha256": file_sha256(target),
                "retrieved_at_utc": datetime.now(UTC).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return f"{symbol}: {len(regular)} session rows in {time.perf_counter() - started:.0f}s"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="*", default=list(INCUMBENTS))
    arguments = parser.parse_args()

    from autotrader.equity.data import create_client

    calendar, _meta = read_snapshot(
        snapshot_path(Path(NEXTGEN_DATASETS), CALENDAR_START, CALENDAR_END)
    )
    client = create_client()
    for symbol in arguments.symbols:
        try:
            _log(build_symbol(symbol, client, calendar))
        except Exception as error:
            _log(f"{symbol}: FAILED — {type(error).__name__}: {error}")


if __name__ == "__main__":
    main()
