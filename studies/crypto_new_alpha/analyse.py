"""Aggregate pilot cells into the predeclared comparison tables.

For an (arm, horizon, family) triple this reads every completed cell, pairs
it with its baseline twin (same symbol/window/horizon), and reports:

* per-window portfolio Δ log loss (arm − baseline; negative = arm better)
* window win counts, mean Δ, row-weighted pooled log loss vs the null
* strongest-window removal (the predeclared fragility attack)
* per-symbol splits and per-era splits
* the daily-stride economic quintile spreads for both arms

The predeclared criteria (search-ledger.md §8) are evaluated mechanically -
this module contains no thresholds that were not in the ledger.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from studies.crypto_new_alpha.pilot import ALL_WINDOWS, MODERN_WINDOWS, cell_path

OUTPUT_DIR = Path("/Volumes/AUTOTRADER_QA/reports/crypto-new-alpha-oi-liq-flow/models")

SYMBOLS = ("BTC/USD", "ETH/USD")

MATERIALITY = 0.002
REQUIRED_WINDOW_WINS_FRACTION = 11 / 17
SPREAD_GATE_BPS = 40.0


def load_cell(arm: str, symbol: str, horizon: int, window: str) -> dict | None:
    path = cell_path(arm, symbol, horizon, window)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def compare(arm: str, horizon: int, family: str, windows: tuple[str, ...]) -> dict:
    per_window: dict[str, dict] = {}
    per_symbol: dict[str, list] = {s: [] for s in SYMBOLS}
    pooled = {"arm_ll_rows": 0.0, "base_ll_rows": 0.0, "null_ll_rows": 0.0, "rows": 0}
    economics: dict[str, list] = {"arm": [], "baseline": []}

    for window in windows:
        deltas = []
        for symbol in SYMBOLS:
            arm_cell = load_cell(arm, symbol, horizon, window)
            base_cell = load_cell("baseline", symbol, horizon, window)
            if not arm_cell or not base_cell:
                continue
            if arm_cell.get("status") != "ok" or base_cell.get("status") != "ok":
                continue
            if arm_cell["test_rows"] != base_cell["test_rows"]:
                raise ValueError(
                    f"population mismatch {arm}/{symbol}/{window}/h{horizon}: "
                    f"{arm_cell['test_rows']} vs {base_cell['test_rows']}"
                )
            arm_predictive = arm_cell["families"][family]["predictive"]
            base_predictive = base_cell["families"][family]["predictive"]
            delta = arm_predictive["log_loss"] - base_predictive["log_loss"]
            deltas.append(delta)
            per_symbol[symbol].append(delta)
            rows = arm_predictive["rows"]
            pooled["arm_ll_rows"] += arm_predictive["log_loss"] * rows
            pooled["base_ll_rows"] += base_predictive["log_loss"] * rows
            pooled["null_ll_rows"] += arm_cell["null"]["log_loss"] * rows
            pooled["rows"] += rows
            arm_spread = arm_cell["families"][family]["economic"]["spread_bps"]
            base_spread = base_cell["families"][family]["economic"]["spread_bps"]
            if arm_spread is not None:
                economics["arm"].append(arm_spread)
            if base_spread is not None:
                economics["baseline"].append(base_spread)
        if deltas:
            per_window[window] = {
                "delta_log_loss": float(np.mean(deltas)),
                "cells": len(deltas),
            }

    if not per_window:
        return {"arm": arm, "horizon": horizon, "family": family, "status": "no-cells"}

    window_deltas = {w: v["delta_log_loss"] for w, v in per_window.items()}
    values = np.asarray(list(window_deltas.values()))
    wins = int((values < 0).sum())
    mean_delta = float(values.mean())
    strongest = min(window_deltas, key=window_deltas.get)
    without_strongest = (
        float(np.mean([v for w, v in window_deltas.items() if w != strongest]))
        if len(window_deltas) > 1
        else None
    )
    rows = pooled["rows"]
    pooled_arm = pooled["arm_ll_rows"] / rows
    pooled_null = pooled["null_ll_rows"] / rows
    modern = [v for w, v in window_deltas.items() if w in MODERN_WINDOWS]
    extended = [v for w, v in window_deltas.items() if w not in MODERN_WINDOWS]

    result = {
        "arm": arm,
        "horizon": horizon,
        "family": family,
        "windows_scored": len(window_deltas),
        "mean_delta_log_loss": mean_delta,
        "window_wins": wins,
        "win_fraction": wins / len(window_deltas),
        "strongest_window": strongest,
        "mean_delta_without_strongest": without_strongest,
        "per_window": window_deltas,
        "per_symbol_mean_delta": {
            s: (float(np.mean(v)) if v else None) for s, v in per_symbol.items()
        },
        "per_era_mean_delta": {
            "modern": float(np.mean(modern)) if modern else None,
            "extended": float(np.mean(extended)) if extended else None,
        },
        "pooled_log_loss": pooled_arm,
        "pooled_baseline_log_loss": pooled["base_ll_rows"] / rows,
        "pooled_null_log_loss": pooled_null,
        "pooled_vs_null": pooled_arm - pooled_null,
        "economic_spread_bps": {
            "arm_mean": float(np.mean(economics["arm"])) if economics["arm"] else None,
            "baseline_mean": (
                float(np.mean(economics["baseline"])) if economics["baseline"] else None
            ),
        },
        "criteria": {},
    }
    result["criteria"] = {
        "c1_material_improvement": mean_delta <= -MATERIALITY,
        "c2_window_wins": (wins / len(window_deltas)) >= REQUIRED_WINDOW_WINS_FRACTION,
        "c4_not_worse_than_null": (pooled_arm - pooled_null) <= 0.0,
        "c5_survives_strongest_removal": (
            without_strongest is not None and without_strongest <= -MATERIALITY
        ),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arms", default="full")
    parser.add_argument("--horizons", default="96,32,16")
    parser.add_argument("--windows", default=",".join(w for w in ALL_WINDOWS if w != "W07"))
    parser.add_argument("--tag", default="main")
    args = parser.parse_args()

    arms = tuple(a.strip() for a in args.arms.split(",") if a.strip())
    horizons = tuple(int(h) for h in args.horizons.split(",") if h.strip())
    windows = tuple(w.strip() for w in args.windows.split(",") if w.strip())

    out: dict = {"generated_at": datetime.now(tz=UTC).isoformat(), "tag": args.tag, "results": []}
    for arm in arms:
        for horizon in horizons:
            for family in ("logistic", "gbt"):
                result = compare(arm, horizon, family, windows)
                out["results"].append(result)
                if result.get("status") == "no-cells":
                    continue
                print(
                    f"{arm} h{horizon} {family}: dLL={result['mean_delta_log_loss']:+.5f} "
                    f"wins={result['window_wins']}/{result['windows_scored']} "
                    f"vs_null={result['pooled_vs_null']:+.5f} "
                    f"drop1={result['mean_delta_without_strongest']} "
                    f"spread(arm/base)="
                    f"{result['economic_spread_bps']['arm_mean']}/"
                    f"{result['economic_spread_bps']['baseline_mean']} "
                    f"criteria={result['criteria']}"
                )
    path = OUTPUT_DIR / f"analysis_{args.tag}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2))
    os.replace(tmp, path)
    print(f"-> {path}")


if __name__ == "__main__":
    main()
