"""The one-interval future-shift falsification (search-ledger.md §9).

Every OI and flow `knowable_at` is moved 15 minutes toward the past, letting
each decision read its derivative sources exactly one bar early - a deliberate
causality violation. The predeclared cells are then re-run against the same
labels, and the shifted metrics are compared with the stored honest cells.

Reading: if the shifted run is materially BETTER, near-boundary information
matters and any accidental one-bar leak would have manufactured performance -
investigate before believing any positive result. If it is essentially
unchanged, the honest join is not sitting on a knife's edge of timing.

Predeclared cells: (full, BTC/USD, W03, 96) and (full, ETH/USD, X05, 96).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from studies.crypto_new_alpha import frames, pilot

SHIFT = pd.Timedelta("15min")
SHIFT_DIR = Path("/Volumes/AUTOTRADER_QA/datasets/crypto-new-alpha/normalized-shifted")
OUTPUT = Path("/Volumes/AUTOTRADER_QA/reports/crypto-new-alpha-oi-liq-flow/leakage_shift.json")

CELLS = (("full", "BTC/USD", "W03", 96), ("full", "ETH/USD", "X05", 96))


def build_shifted_dir() -> None:
    SHIFT_DIR.mkdir(parents=True, exist_ok=True)
    for perp in frames.PERP_OF.values():
        for kind in ("oi", "flow"):
            source = frames.NORMALIZED_DIR / f"{perp}_{kind}.parquet"
            frame = pd.read_parquet(source)
            frame["knowable_at"] = frame["knowable_at"] - SHIFT
            target = SHIFT_DIR / f"{perp}_{kind}.parquet"
            tmp = target.with_suffix(".parquet.tmp")
            frame.to_parquet(tmp, index=False)
            os.replace(tmp, target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-shifted", action="store_true")
    args = parser.parse_args()

    build_shifted_dir()
    results = []
    original_dir = frames.NORMALIZED_DIR
    try:
        frames.NORMALIZED_DIR = SHIFT_DIR
        frames._CACHE.clear()
        for arm, symbol, window, horizon in CELLS:
            shifted = pilot.run_cell(arm, symbol, window, horizon)
            honest_path = pilot.cell_path(arm, symbol, horizon, window)
            honest = json.loads(honest_path.read_text()) if honest_path.exists() else None
            row = {"arm": arm, "symbol": symbol, "window": window, "horizon": horizon}
            for family in ("logistic", "gbt"):
                shifted_ll = shifted["families"][family]["predictive"]["log_loss"]
                honest_ll = honest["families"][family]["predictive"]["log_loss"] if honest else None
                row[family] = {
                    "honest_log_loss": honest_ll,
                    "shifted_log_loss": shifted_ll,
                    "shift_gain": (honest_ll - shifted_ll) if honest_ll is not None else None,
                }
            results.append(row)
            print(json.dumps(row, indent=1))
    finally:
        frames.NORMALIZED_DIR = original_dir
        frames._CACHE.clear()
        if not args.keep_shifted:
            shutil.rmtree(SHIFT_DIR, ignore_errors=True)

    payload = {"generated_at": datetime.now(tz=UTC).isoformat(), "cells": results}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, OUTPUT)
    print(f"-> {OUTPUT}")


if __name__ == "__main__":
    main()
