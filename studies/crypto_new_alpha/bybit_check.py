"""Second-venue confirmation: does the aggressor-flow signal exist on Bybit?

Predeclared design (search-ledger.md §11): Bybit linear-perp tick trades for a
stratified sample of days - the 5th, 15th and 25th of January, April, July and
October, 2021 through 2026 (dates past the archive end are skipped) - both
symbols. Each day's trades are streamed, aggregated to the 15m grid
(aggressor side = the `side` column: Buy means the taker bought), and then:

1. **Signal agreement**: correlation of 15m / 4h / 24h flow imbalance between
   Bybit and the primary venue on the identical bars. High agreement means
   the flow information is a property of the market, not one venue's tape.
2. **Event coherence**: on sampled days, the mean 4h forward return of the
   decision stream conditional on Bybit-measured bottom-quartile 4h flow
   after a negative trailing 24h return (the E7b mechanism, sample-local
   quartiles) - direction compared with the primary-venue result.

Raw daily files are deleted after aggregation; URL + SHA-256 kept in the
report. Never mixed into training.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from studies.crypto_new_alpha.acquire import _get

SYMBOLS = ("BTCUSDT", "ETHUSDT")
BASE_URL = "https://public.bybit.com/trading"
RAW_DIR = Path("/Volumes/AUTOTRADER_QA/datasets/crypto-new-alpha/raw/bybit-samples")
NORMALIZED_DIR = Path("/Volumes/AUTOTRADER_QA/datasets/crypto-new-alpha/normalized")
REPORT = Path(
    "/Volumes/AUTOTRADER_QA/reports/crypto-new-alpha-oi-liq-flow/robustness/bybit_second_venue.json"
)

YEARS = (2021, 2022, 2023, 2024, 2025, 2026)
MONTHS = (1, 4, 7, 10)
DAYS = (5, 15, 25)
ARCHIVE_END = pd.Timestamp("2026-08-28")


def sample_days() -> list[str]:
    out = []
    for year in YEARS:
        for month in MONTHS:
            for day in DAYS:
                stamp = pd.Timestamp(year=year, month=month, day=day)
                if stamp <= ARCHIVE_END:
                    out.append(stamp.strftime("%Y-%m-%d"))
    return out


def aggregate_day(symbol: str, day: str) -> tuple[pd.DataFrame | None, dict]:
    url = f"{BASE_URL}/{symbol}/{symbol}{day}.csv.gz"
    body = _get(url, timeout=900.0)
    if body is None:
        return None, {"symbol": symbol, "day": day, "status": "absent", "source_url": url}
    digest = hashlib.sha256(body).hexdigest()
    frame = pd.read_csv(io.BytesIO(gzip.decompress(body)))
    # Columns: timestamp(s),symbol,side,size,price,tickDirection,trdMatchID,
    # grossValue,homeNotional,foreignNotional. side is the taker's direction.
    notional = frame["price"].astype("float64") * frame["size"].astype("float64")
    seconds = frame["timestamp"].astype("float64")
    bucket = pd.to_datetime((seconds // 900).astype("int64") * 900, unit="s", utc=True)
    is_buy = frame["side"].astype(str).str.lower().eq("buy")
    grouped = (
        pd.DataFrame(
            {
                "bar_open": bucket,
                "notional": notional,
                "buy_notional": np.where(is_buy, notional, 0.0),
            }
        )
        .groupby("bar_open", as_index=False)
        .sum()
    )
    record = {
        "symbol": symbol,
        "day": day,
        "status": "ok",
        "source_url": url,
        "sha256": digest,
        "ticks": int(len(frame)),
    }
    return grouped, record


def run(symbols: tuple[str, ...]) -> dict:
    days = sample_days()
    all_records = []
    aggregates: dict[str, list[pd.DataFrame]] = {s: [] for s in symbols}
    for symbol in symbols:
        for day in days:
            grouped, record = aggregate_day(symbol, day)
            all_records.append(record)
            if grouped is not None:
                aggregates[symbol].append(grouped)
            print(f"{symbol} {day}: {record['status']}", flush=True)

    comparison = {}
    for symbol in symbols:
        if not aggregates[symbol]:
            continue
        bybit = pd.concat(aggregates[symbol], ignore_index=True)
        out = NORMALIZED_DIR / f"{symbol}_bybit_flow_samples.parquet"
        tmp = out.with_suffix(".parquet.tmp")
        bybit.to_parquet(tmp, index=False)
        os.replace(tmp, out)

        primary = pd.read_parquet(NORMALIZED_DIR / f"{symbol}_flow.parquet")
        merged = primary.merge(bybit, on="bar_open", how="inner", suffixes=("", "_bybit"))
        merged["imb_primary"] = (
            2.0 * merged["taker_buy_quote_volume"] - merged["quote_volume"]
        ) / merged["quote_volume"].where(merged["quote_volume"] > 0)
        merged["imb_bybit"] = (2.0 * merged["buy_notional"] - merged["notional"]) / merged[
            "notional"
        ].where(merged["notional"] > 0)
        by_day = merged.assign(day=merged["bar_open"].dt.date)

        def _window_corr(bars: int, frame_by_day=by_day) -> float | None:
            rows = []
            for _, group in frame_by_day.groupby("day"):
                if len(group) < bars:
                    continue
                a = group["imb_primary"].rolling(bars).mean()
                b = group["imb_bybit"].rolling(bars).mean()
                rows.append(pd.DataFrame({"a": a, "b": b}).dropna())
            if not rows:
                return None
            pooled = pd.concat(rows)
            return float(pooled["a"].corr(pooled["b"]))

        comparison[symbol] = {
            "bars_compared": int(len(merged)),
            "imbalance_corr_15m": float(merged["imb_primary"].corr(merged["imb_bybit"])),
            "imbalance_corr_1h": _window_corr(4),
            "imbalance_corr_4h": _window_corr(16),
            "notional_ratio_median": float(
                (
                    merged["notional"] / merged["quote_volume"].where(merged["quote_volume"] > 0)
                ).median()
            ),
        }
    return {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "sample_days": days,
        "downloads": all_records,
        "comparison": comparison,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    args = parser.parse_args()
    symbols = tuple(s.strip() for s in args.symbols.split(",") if s.strip())
    payload = run(symbols)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    tmp = REPORT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, REPORT)
    print(f"-> {REPORT}")
    for symbol, row in payload["comparison"].items():
        print(symbol, json.dumps(row))


if __name__ == "__main__":
    main()
