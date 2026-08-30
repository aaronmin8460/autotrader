"""Download the 2021-2023 extension of the historical crypto dataset.

Read-only market data through `autotrader.data.historical`, the project's
only market-data path. No credential is required (the provider serves crypto
bars unauthenticated; configured credentials only raise the rate limit), no
order is placed, and the fingerprinted 2024-26 originals are never touched -
this writes new files under `crypto-historical-extended/`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from autotrader.data.historical import create_client, fetch_bars

OUTPUT_DIR = Path("/Volumes/AUTOTRADER_QA/datasets/crypto-historical-extended")

SYMBOLS = ("BTC/USD", "ETH/USD")
START = datetime(2021, 1, 1, tzinfo=UTC)
END = datetime(2024, 1, 1, tzinfo=UTC)


def month_starts() -> list[datetime]:
    moments = []
    year, month = START.year, START.month
    while datetime(year, month, 1, tzinfo=UTC) < END:
        moments.append(datetime(year, month, 1, tzinfo=UTC))
        month += 1
        if month == 13:
            year, month = year + 1, 1
    moments.append(END)
    return moments


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    client = create_client()
    boundaries = month_starts()
    for symbol in SYMBOLS:
        slug = symbol.replace("/", "_")
        path = OUTPUT_DIR / f"{slug}_15m_2021-01-01_2023-12-31.parquet"
        if path.exists():
            print(f"skip {symbol}: {path.name} exists")
            continue
        chunks = []
        for index in range(len(boundaries) - 1):
            chunk = fetch_bars(client, symbol, boundaries[index], boundaries[index + 1])
            chunks.append(chunk)
            print(f"{symbol} {boundaries[index]:%Y-%m}: {len(chunk)} bars", flush=True)
        frame = pd.concat(chunks, ignore_index=True)
        frame = frame.drop_duplicates(subset="timestamp").sort_values("timestamp")
        frame = frame.reset_index(drop=True)
        tmp = path.with_suffix(".tmp")
        frame.to_parquet(tmp, index=False)
        tmp.rename(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        timestamps = pd.to_datetime(frame["timestamp"], utc=True)
        expected = int((END - START).total_seconds() // 900)
        metadata = {
            "symbol": symbol,
            "rows": int(len(frame)),
            "expected_grid_bars": expected,
            "missing_share": 1.0 - len(frame) / expected,
            "first": str(timestamps.iloc[0]),
            "last": str(timestamps.iloc[-1]),
            "monotonic": bool(timestamps.is_monotonic_increasing),
            "duplicates": 0,
            "ohlc_violations": int(
                (
                    (frame["high"] < frame[["open", "close", "low"]].max(axis=1))
                    | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
                ).sum()
            ),
            "zero_volume_share": float((frame["volume"] == 0).mean()),
            "sha256": digest,
        }
        (OUTPUT_DIR / f"{slug}_15m_2021-01-01_2023-12-31.metadata.json").write_text(
            json.dumps(metadata, indent=2)
        )
        print(json.dumps(metadata, indent=2), flush=True)
    print("extended download complete")


if __name__ == "__main__":
    main()
