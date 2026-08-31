"""Phase-12 model robustness: year splits, strongest-removal, thin-train view.

Windows map to calendar years (the year containing the window's test span).
For each model family this reports, at the primary horizon, full-vs-baseline:

* per-year mean Δ log loss and Δ daily-stride quintile spread
* strongest-year removal and strongest-window removal
* the same aggregates restricted to cells with ≥ MIN_SOUND_TRAIN training
  rows - a labelled post-hoc sensitivity (the predeclared 500-row floor let
  through ETH extended cells that train on days of data and blow up in both
  arms; the criteria themselves are still evaluated on the predeclared set).
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from studies.crypto_new_alpha.analyse import load_cell
from studies.crypto_new_alpha.pilot import DEFAULT_WINDOWS

OUTPUT_DIR = Path("/Volumes/AUTOTRADER_QA/reports/crypto-new-alpha-oi-liq-flow/robustness")

SYMBOLS = ("BTC/USD", "ETH/USD")

WINDOW_YEAR = {
    "X01": 2021,
    "X02": 2022,
    "X03": 2022,
    "X04": 2022,
    "X05": 2022,
    "X06": 2023,
    "X07": 2023,
    "X08": 2023,
    "X09": 2023,
    "P3": 2024,
    "W01": 2025,
    "W02": 2025,
    "W03": 2025,
    "W04": 2025,
    "W05": 2026,
    "W06": 2026,
    "W07": 2026,
}

MIN_SOUND_TRAIN = 5000


def collect(horizon: int, family: str, windows) -> list[dict]:
    rows = []
    for window in windows:
        for symbol in SYMBOLS:
            full = load_cell("full", symbol, horizon, window)
            base = load_cell("baseline", symbol, horizon, window)
            if not full or not base:
                continue
            if full.get("status") != "ok" or base.get("status") != "ok":
                continue
            f, b = full["families"][family], base["families"][family]
            spread_full = f["economic"]["spread_bps"]
            spread_base = b["economic"]["spread_bps"]
            rows.append(
                {
                    "window": window,
                    "symbol": symbol,
                    "year": WINDOW_YEAR[window],
                    "train_rows": full["train_rows"],
                    "delta_ll": f["predictive"]["log_loss"] - b["predictive"]["log_loss"],
                    "delta_spread": (
                        (spread_full - spread_base)
                        if spread_full is not None and spread_base is not None
                        else None
                    ),
                    "full_spread": spread_full,
                    "full_ic": f["predictive"]["rank_ic"],
                    "base_ic": b["predictive"]["rank_ic"],
                }
            )
    return rows


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"cells": 0}
    deltas = [r["delta_ll"] for r in rows]
    spreads = [r["delta_spread"] for r in rows if r["delta_spread"] is not None]
    full_spreads = [r["full_spread"] for r in rows if r["full_spread"] is not None]
    ic_pairs = [
        (r["full_ic"], r["base_ic"])
        for r in rows
        if r["full_ic"] is not None and r["base_ic"] is not None
    ]
    per_year: dict = {}
    for year in sorted({r["year"] for r in rows}):
        year_deltas = [r["delta_ll"] for r in rows if r["year"] == year]
        per_year[str(year)] = {"cells": len(year_deltas), "mean_delta_ll": float(np.mean(year_deltas))}
    strongest_year = min(per_year, key=lambda y: per_year[y]["mean_delta_ll"])
    without_year = [r["delta_ll"] for r in rows if str(r["year"]) != strongest_year]
    return {
        "cells": len(rows),
        "mean_delta_ll": float(np.mean(deltas)),
        "median_delta_ll": float(np.median(deltas)),
        "mean_delta_spread_bps": float(np.mean(spreads)) if spreads else None,
        "median_delta_spread_bps": float(np.median(spreads)) if spreads else None,
        "mean_full_spread_bps": float(np.mean(full_spreads)) if full_spreads else None,
        "median_full_spread_bps": float(np.median(full_spreads)) if full_spreads else None,
        "mean_full_ic": float(np.mean([p[0] for p in ic_pairs])) if ic_pairs else None,
        "mean_base_ic": float(np.mean([p[1] for p in ic_pairs])) if ic_pairs else None,
        "per_year": per_year,
        "strongest_year": strongest_year,
        "mean_delta_ll_without_strongest_year": (
            float(np.mean(without_year)) if without_year else None
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--horizon", type=int, default=96)
    parser.add_argument("--windows", default=",".join(DEFAULT_WINDOWS))
    parser.add_argument("--tag", default="main")
    args = parser.parse_args()
    windows = tuple(w.strip() for w in args.windows.split(",") if w.strip())

    out: dict = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "horizon": args.horizon,
        "tag": args.tag,
        "families": {},
    }
    for family in ("logistic", "gbt"):
        rows = collect(args.horizon, family, windows)
        sound = [r for r in rows if r["train_rows"] >= MIN_SOUND_TRAIN]
        per_symbol = {
            s: summarize([r for r in rows if r["symbol"] == s]) for s in SYMBOLS
        }
        out["families"][family] = {
            "all_cells": summarize(rows),
            "sound_train_cells": summarize(sound),
            "per_symbol": per_symbol,
        }
        print(family, "all:", json.dumps(out["families"][family]["all_cells"])[:300])
        print(family, "sound:", json.dumps(out["families"][family]["sound_train_cells"])[:300])
    output = OUTPUT_DIR / f"model_robustness_{args.tag}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2))
    os.replace(tmp, output)
    print(f"-> {output}")


if __name__ == "__main__":
    main()
