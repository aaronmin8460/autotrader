"""Raw dump zips -> normalized, audited, fingerprinted parquets.

Three outputs per derivative symbol, all UTC, all sorted, all with explicit
`knowable_at` stamps (ledger §2):

* `<SYM>_flow.parquet` - one row per 15m perp kline: bar_open, bar_close,
  knowable_at (= bar_close + 1ms), quote_volume, taker_buy_quote_volume,
  count, close, volume. Missing intervals stay missing - no interpolation.
* `<SYM>_oi.parquet` - one row per open-interest snapshot: create_time,
  knowable_at (= create_time + 5min conservative publication charge),
  oi_contracts, oi_notional. Exact duplicates dropped (counted); conflicting
  duplicates keep the last row (counted).
* `<CM_SYM>_liq.parquet` (validation only) - one row per liquidation order
  event: event_ts, side, contracts, price, notional_usd.

Every output gets a manifest entry: row count, span, SHA-256 of the parquet,
and the audit counters. Zero is never used to mean missing anywhere.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

RAW_DIR = Path("/Volumes/AUTOTRADER_QA/datasets/crypto-new-alpha/raw")
OUT_DIR = Path("/Volumes/AUTOTRADER_QA/datasets/crypto-new-alpha/normalized")
MANIFEST = OUT_DIR / "normalization_manifest.json"

SYMBOLS = ("BTCUSDT", "ETHUSDT")
CM_SYMBOLS = ("BTCUSD_PERP", "ETHUSD_PERP")

#: Coin-margined contract sizes in USD (venue contract specification).
CM_CONTRACT_USD = {"BTCUSD_PERP": 100.0, "ETHUSD_PERP": 10.0}

#: Conservative open-interest publication charge (ledger §2).
OI_PUBLICATION_LAG = pd.Timedelta("5min")

KLINE_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
)

BAR = pd.Timedelta("15min")


def _read_zip_csv(path: Path, names: tuple[str, ...]) -> pd.DataFrame:
    """One dump zip as a frame, tolerating both headerless and headered files."""
    frame = pd.read_csv(path, header=None, dtype=str)
    if len(frame.columns) != len(names):
        raise ValueError(f"{path.name}: {len(frame.columns)} columns, expected {len(names)}")
    frame.columns = list(names)
    first = str(frame.iloc[0, 0])
    if not first.lstrip("-").replace(".", "", 1).isdigit():
        frame = frame.iloc[1:].reset_index(drop=True)
    return frame


def normalize_flow(symbol: str) -> tuple[pd.DataFrame, dict]:
    """All 15m perp klines for one symbol, audited."""
    files = sorted((RAW_DIR / "klines" / symbol).glob("*.zip"))
    if not files:
        raise FileNotFoundError(f"no kline zips for {symbol}")
    parts = []
    for path in files:
        frame = _read_zip_csv(path, KLINE_COLUMNS)
        parts.append(frame)
    raw = pd.concat(parts, ignore_index=True)
    out = pd.DataFrame(
        {
            "bar_open": pd.to_datetime(raw["open_time"].astype("int64"), unit="ms", utc=True),
            "bar_close": pd.to_datetime(raw["close_time"].astype("int64"), unit="ms", utc=True),
            "close": raw["close"].astype("float64"),
            "volume": raw["volume"].astype("float64"),
            "quote_volume": raw["quote_volume"].astype("float64"),
            "count": raw["count"].astype("int64"),
            "taker_buy_quote_volume": raw["taker_buy_quote_volume"].astype("float64"),
        }
    )
    duplicates = int(out.duplicated(subset="bar_open").sum())
    out = out.drop_duplicates(subset="bar_open", keep="first")
    out = out.sort_values("bar_open", kind="stable").reset_index(drop=True)

    misaligned = int((out["bar_close"] != out["bar_open"] + BAR - pd.Timedelta("1ms")).sum())
    if misaligned:
        raise ValueError(f"{symbol}: {misaligned} klines are not 15m-grid aligned")
    off_grid = int(
        ((out["bar_open"].dt.minute % 15 != 0) | (out["bar_open"].dt.second != 0)).sum()
    )
    if off_grid:
        raise ValueError(f"{symbol}: {off_grid} klines off the 15m UTC grid")

    span = pd.date_range(out["bar_open"].iloc[0], out["bar_open"].iloc[-1], freq="15min", tz="UTC")
    missing = int(len(span) - len(out))
    negative = int(
        (
            (out["quote_volume"] < 0)
            | (out["taker_buy_quote_volume"] < 0)
            | (out["taker_buy_quote_volume"] > out["quote_volume"] * (1 + 1e-9))
        ).sum()
    )
    if negative:
        raise ValueError(f"{symbol}: {negative} rows with impossible flow accounting")

    out["knowable_at"] = out["bar_close"] + pd.Timedelta("1ms")
    audit = {
        "files": len(files),
        "rows": int(len(out)),
        "first_bar": str(out["bar_open"].iloc[0]),
        "last_bar": str(out["bar_open"].iloc[-1]),
        "duplicate_bars_dropped": duplicates,
        "missing_intervals": missing,
        "missing_fraction": missing / len(span),
        "zero_volume_bars": int((out["quote_volume"] == 0).sum()),
    }
    return out, audit


def normalize_oi(symbol: str) -> tuple[pd.DataFrame, dict]:
    """All open-interest snapshots for one symbol, audited."""
    files = sorted((RAW_DIR / "metrics" / symbol).glob("*.zip"))
    if not files:
        raise FileNotFoundError(f"no metrics zips for {symbol}")
    parts = []
    for path in files:
        frame = pd.read_csv(path)
        parts.append(
            pd.DataFrame(
                {
                    "create_time": frame["create_time"],
                    "oi_contracts": frame["sum_open_interest"].astype("float64"),
                    "oi_notional": frame["sum_open_interest_value"].astype("float64"),
                }
            )
        )
    raw = pd.concat(parts, ignore_index=True)
    raw["create_time"] = pd.to_datetime(raw["create_time"], utc=True, format="%Y-%m-%d %H:%M:%S")

    exact_dupes = int(raw.duplicated().sum())
    raw = raw.drop_duplicates(keep="first")
    conflicts = int(raw.duplicated(subset="create_time").sum())
    raw = raw.drop_duplicates(subset="create_time", keep="last")
    # A zero or negative open-interest notional on a flagship perp is the
    # archive's spelling of a broken snapshot, not a market state. Dropped and
    # counted - a dropped snapshot becomes a (tolerated or NaN-ing) gap, which
    # is the declared behaviour for holes; it is never carried as a value.
    nonpositive = int((raw["oi_notional"] <= 0).sum())
    raw = raw.loc[raw["oi_notional"] > 0]
    out = raw.sort_values("create_time", kind="stable").reset_index(drop=True)
    gaps = out["create_time"].diff().dropna()
    out["knowable_at"] = out["create_time"] + OI_PUBLICATION_LAG
    audit = {
        "files": len(files),
        "rows": int(len(out)),
        "first_snapshot": str(out["create_time"].iloc[0]),
        "last_snapshot": str(out["create_time"].iloc[-1]),
        "exact_duplicates_dropped": exact_dupes,
        "conflicting_duplicates_kept_last": conflicts,
        "nonpositive_notional_rows_dropped": nonpositive,
        "gap_seconds_p50": float(gaps.dt.total_seconds().quantile(0.5)),
        "gap_seconds_p99": float(gaps.dt.total_seconds().quantile(0.99)),
        "gap_seconds_max": float(gaps.dt.total_seconds().max()),
        "gaps_over_30min": int((gaps > pd.Timedelta("30min")).sum()),
        "gaps_over_2h": int((gaps > pd.Timedelta("2h")).sum()),
    }
    return out, audit


LIQ_COLUMNS = (
    "time",
    "side",
    "order_type",
    "time_in_force",
    "original_quantity",
    "price",
    "average_price",
    "order_status",
    "last_fill_quantity",
    "accumulated_fill_quantity",
)


def normalize_cm_liquidations(symbol: str) -> tuple[pd.DataFrame, dict]:
    """The narrow coin-margined liquidation record (validation evidence only)."""
    files = sorted((RAW_DIR / "liquidations-cm" / symbol).glob("*.zip"))
    if not files:
        raise FileNotFoundError(f"no liquidation zips for {symbol}")
    parts = []
    for path in files:
        parts.append(_read_zip_csv(path, LIQ_COLUMNS))
    raw = pd.concat(parts, ignore_index=True)
    exact_dupes = int(raw.duplicated().sum())
    raw = raw.drop_duplicates(keep="first")
    contracts = raw["accumulated_fill_quantity"].astype("float64")
    out = pd.DataFrame(
        {
            "event_ts": pd.to_datetime(raw["time"].astype("int64"), unit="ms", utc=True),
            # side SELL = a long position force-closed; BUY = a short force-closed.
            "side": raw["side"].astype(str),
            "contracts": contracts,
            "price": raw["average_price"].astype("float64"),
            "notional_usd": contracts * CM_CONTRACT_USD[symbol],
            "order_status": raw["order_status"].astype(str),
        }
    )
    out = out.sort_values("event_ts", kind="stable").reset_index(drop=True)
    audit = {
        "files": len(files),
        "rows": int(len(out)),
        "first_event": str(out["event_ts"].iloc[0]),
        "last_event": str(out["event_ts"].iloc[-1]),
        "exact_duplicates_dropped": exact_dupes,
        "sides": out["side"].value_counts().to_dict(),
        "total_notional_usd": float(out["notional_usd"].sum()),
    }
    return out, audit


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write(frame: pd.DataFrame, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    frame.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    return _sha256(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets", default="flow,oi,cmliq")
    args = parser.parse_args()
    wanted = {token.strip() for token in args.datasets.split(",") if token.strip()}

    manifest: dict = {"generated_at": datetime.now(tz=UTC).isoformat(), "outputs": {}}
    if MANIFEST.exists():
        manifest["outputs"] = json.loads(MANIFEST.read_text()).get("outputs", {})

    if "flow" in wanted:
        for symbol in SYMBOLS:
            frame, audit = normalize_flow(symbol)
            path = OUT_DIR / f"{symbol}_flow.parquet"
            digest = _write(frame, path)
            manifest["outputs"][f"{symbol}_flow"] = {"path": str(path), "sha256": digest, **audit}
            print(f"{symbol} flow: {audit['rows']} rows, missing {audit['missing_intervals']}")
    if "oi" in wanted:
        for symbol in SYMBOLS:
            frame, audit = normalize_oi(symbol)
            path = OUT_DIR / f"{symbol}_oi.parquet"
            digest = _write(frame, path)
            manifest["outputs"][f"{symbol}_oi"] = {"path": str(path), "sha256": digest, **audit}
            print(f"{symbol} oi: {audit['rows']} rows, max gap {audit['gap_seconds_max']:.0f}s")
    if "cmliq" in wanted:
        for symbol in CM_SYMBOLS:
            frame, audit = normalize_cm_liquidations(symbol)
            path = OUT_DIR / f"{symbol}_liq.parquet"
            digest = _write(frame, path)
            manifest["outputs"][f"{symbol}_liq"] = {"path": str(path), "sha256": digest, **audit}
            print(f"{symbol} liq: {audit['rows']} events")

    tmp = MANIFEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    os.replace(tmp, MANIFEST)
    print(f"manifest -> {MANIFEST}")


if __name__ == "__main__":
    main()
