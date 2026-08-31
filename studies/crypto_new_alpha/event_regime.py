"""Phase-11 regime robustness for the leading event conditions.

Splits the predeclared event conditions by the causal regime labels
(search-ledger.md §11): trend regime (bull / bear / sideways / crash /
recovery) and volatility regime (high-vol / low-vol), at the primary horizon.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from studies.crypto_new_alpha.events import _stats, build_event_masks, concatenated_frame
from studies.crypto_new_alpha.frames import PRIMARY_HORIZON, SYMBOLS

OUTPUT = Path(
    "/Volumes/AUTOTRADER_QA/reports/crypto-new-alpha-oi-liq-flow/robustness/event_regimes.json"
)

CONDITIONS = (
    "E7b_cvd_div_down",
    "E3b_oi_fall_p5",
    "E5d_price_down_oi_down",
    "E1_delev_long_p95",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    horizon = PRIMARY_HORIZON
    out: dict = {"generated_at": datetime.now(tz=UTC).isoformat(), "horizon": horizon, "symbols": {}}
    for symbol in SYMBOLS:
        frame = concatenated_frame(symbol)
        masks = build_event_masks(frame)
        usable = frame[f"usable_{horizon}"]
        symbol_out: dict = {}
        for condition in CONDITIONS:
            mask = masks[condition] & usable
            rows = frame.loc[mask]
            entry: dict = {}
            for column, label in (("regime", "trend"), ("vol_regime", "volatility")):
                split = {}
                for value, group in rows.groupby(column, dropna=True):
                    stats = _stats(group[f"fwd_{horizon}"].to_numpy(dtype="float64"))
                    stats["thin"] = stats["n"] < 30
                    split[str(value)] = stats
                entry[label] = split
            symbol_out[condition] = entry
        out["symbols"][symbol] = symbol_out
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2, default=str))
    os.replace(tmp, OUTPUT)
    print(f"-> {OUTPUT}")
    for symbol, conditions in out["symbols"].items():
        for condition, entry in conditions.items():
            trend = {
                k: f"{v['mean_bps']:+.0f}bps(n={v['n']})"
                for k, v in entry["trend"].items()
                if v["n"]
            }
            print(symbol, condition, trend)


if __name__ == "__main__":
    main()
