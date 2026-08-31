"""Aggressor-semantics validation: kline taker-buy fields vs raw tick trades.

For each predeclared sample month (search-ledger.md §1) the monthly aggTrades
zip is downloaded to the QA volume, streamed in chunks, aggregated to the 15m
grid, and compared against the kline fields the study actually uses:

* sum(price x quantity)                      vs kline `quote_volume`
* sum over aggressor-buy trades of the same  vs kline `taker_buy_quote_volume`

Aggressor-buy means `is_buyer_maker == false` (the buyer took liquidity).
The raw zip is deleted after aggregation - its URL and SHA-256 stay in the
manifest, and the 15m aggregate (plus tick-only statistics: per-side trade
counts, mean aggressor trade size, large-trade fraction) is kept as a parquet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from studies.crypto_new_alpha.acquire import _get  # same transport, same retries

SAMPLE_MONTHS = ("2021-05", "2022-11", "2024-03", "2026-05")
SYMBOLS = ("BTCUSDT", "ETHUSDT")

RAW_DIR = Path("/Volumes/AUTOTRADER_QA/datasets/crypto-new-alpha/raw/aggtrades-samples")
NORMALIZED_DIR = Path("/Volumes/AUTOTRADER_QA/datasets/crypto-new-alpha/normalized")
REPORT = Path(
    "/Volumes/AUTOTRADER_QA/reports/crypto-new-alpha-oi-liq-flow/aggtrades_validation.json"
)

COLUMNS = (
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
)

#: A "large" trade is one at or above the month's own p99 quote notional -
#: computed per month from the tick data itself (documentation statistic only;
#: never a model feature in this pilot).
LARGE_QUANTILE = 0.99

CHUNK_ROWS = 2_000_000


def aggregate_month(symbol: str, month: str) -> tuple[pd.DataFrame, dict]:
    url = (
        "https://data.binance.vision/data/futures/um/monthly/aggTrades/"
        f"{symbol}/{symbol}-aggTrades-{month}.zip"
    )
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = RAW_DIR / f"{symbol}-aggTrades-{month}.zip"
    if not zip_path.exists():
        body = _get(url, timeout=1800.0)
        if body is None:
            raise RuntimeError(f"absent aggTrades month {symbol} {month}")
        part = zip_path.with_suffix(".zip.part")
        part.write_bytes(body)
        os.replace(part, zip_path)
    digest = hashlib.sha256()
    with zip_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)

    per_bucket: dict = {}
    notionals_sample: list[np.ndarray] = []
    rows_total = 0
    with zipfile.ZipFile(zip_path) as archive:
        inner = archive.namelist()[0]
        with archive.open(inner) as stream:
            reader = pd.read_csv(
                stream, header=None, names=COLUMNS, chunksize=CHUNK_ROWS, dtype=str
            )
            for chunk in reader:
                if chunk.iloc[0, 0] == "agg_trade_id":
                    chunk = chunk.iloc[1:]
                if chunk.empty:
                    continue
                rows_total += len(chunk)
                price = chunk["price"].astype("float64").to_numpy()
                quantity = chunk["quantity"].astype("float64").to_numpy()
                notional = price * quantity
                time_ms = chunk["transact_time"].astype("int64").to_numpy()
                buyer_is_taker = chunk["is_buyer_maker"].str.lower().eq("false").to_numpy()
                bucket = (time_ms // 900_000) * 900_000
                frame = pd.DataFrame(
                    {
                        "bucket": bucket,
                        "notional": notional,
                        "buy_notional": np.where(buyer_is_taker, notional, 0.0),
                        "buy_count": buyer_is_taker.astype("int64"),
                        "sell_count": (~buyer_is_taker).astype("int64"),
                    }
                )
                grouped = frame.groupby("bucket").agg(
                    notional=("notional", "sum"),
                    buy_notional=("buy_notional", "sum"),
                    buy_count=("buy_count", "sum"),
                    sell_count=("sell_count", "sum"),
                )
                for key, row in grouped.iterrows():
                    if key in per_bucket:
                        per_bucket[key] = per_bucket[key] + row.to_numpy()
                    else:
                        per_bucket[key] = row.to_numpy()
                if len(notionals_sample) < 200:
                    notionals_sample.append(notional[::101].copy())

    aggregate = pd.DataFrame.from_dict(
        per_bucket, orient="index", columns=["notional", "buy_notional", "buy_count", "sell_count"]
    ).sort_index()
    aggregate.index = pd.to_datetime(aggregate.index, unit="ms", utc=True)
    aggregate.index.name = "bar_open"

    pooled = np.concatenate(notionals_sample) if notionals_sample else np.array([])
    large_cut = float(np.quantile(pooled, LARGE_QUANTILE)) if len(pooled) else float("nan")
    stats = {
        "symbol": symbol,
        "month": month,
        "source_url": url,
        "zip_sha256": digest.hexdigest(),
        "tick_rows": rows_total,
        "mean_aggressor_buy_share": float(
            aggregate["buy_notional"].sum() / aggregate["notional"].sum()
        ),
        "large_trade_notional_p99_usd": large_cut,
        "buckets": int(len(aggregate)),
    }
    return aggregate.reset_index(), stats


def compare_with_klines(symbol: str, month: str, aggregate: pd.DataFrame) -> dict:
    flow = pd.read_parquet(NORMALIZED_DIR / f"{symbol}_flow.parquet")
    start = pd.Timestamp(f"{month}-01", tz="UTC")
    end = (start + pd.offsets.MonthEnd(1)) + pd.Timedelta("23h45min")
    month_flow = flow.loc[(flow["bar_open"] >= start) & (flow["bar_open"] <= end)]
    merged = month_flow.merge(aggregate, on="bar_open", how="inner")
    quote_err = (
        (merged["notional"] - merged["quote_volume"]).abs()
        / merged["quote_volume"].where(merged["quote_volume"] > 0)
    ).dropna()
    buy_err = (
        (merged["buy_notional"] - merged["taker_buy_quote_volume"]).abs()
        / merged["quote_volume"].where(merged["quote_volume"] > 0)
    ).dropna()
    return {
        "bars_compared": int(len(merged)),
        "quote_volume_rel_err_max": float(quote_err.max()) if len(quote_err) else None,
        "taker_buy_rel_err_max": float(buy_err.max()) if len(buy_err) else None,
        "quote_volume_rel_err_p50": float(quote_err.median()) if len(quote_err) else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-zip", action="store_true")
    args = parser.parse_args()
    results = []
    for symbol in SYMBOLS:
        for month in SAMPLE_MONTHS:
            aggregate, stats = aggregate_month(symbol, month)
            out = NORMALIZED_DIR / f"{symbol}_aggtrades_{month}.parquet"
            tmp = out.with_suffix(".parquet.tmp")
            aggregate.to_parquet(tmp, index=False)
            os.replace(tmp, out)
            comparison = compare_with_klines(symbol, month, aggregate)
            record = {**stats, **comparison}
            results.append(record)
            print(
                f"{symbol} {month}: bars={comparison['bars_compared']} "
                f"quote_err_max={comparison['quote_volume_rel_err_max']} "
                f"buy_err_max={comparison['taker_buy_rel_err_max']}",
                flush=True,
            )
            if not args.keep_zip:
                (RAW_DIR / f"{symbol}-aggTrades-{month}.zip").unlink(missing_ok=True)
    payload = {"generated_at": datetime.now(tz=UTC).isoformat(), "months": results}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    tmp = REPORT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, REPORT)
    print(f"-> {REPORT}")


if __name__ == "__main__":
    main()
