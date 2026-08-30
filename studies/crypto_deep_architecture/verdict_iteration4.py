"""Mechanical application of Iteration 4's predeclared falsification rule.

Reads the fold checkpoints and prints, per family: portfolio mean net
forced-liquidation return, per-symbol means, per-fold table, single-window
dependence (drop-one sweep), stress survival, and gate-neighbourhood signs.
No thresholds in here were chosen after seeing results; they are the
journal's."""

from __future__ import annotations

import json
from glob import glob
from pathlib import Path

import pandas as pd

CELLS = Path("/Volumes/AUTOTRADER_QA/reports/crypto-deep-architecture/iteration4/cells")


def load() -> pd.DataFrame:
    rows = []
    for path in sorted(glob(str(CELLS / "*.json"))):
        with open(path) as handle:
            rec = json.load(handle)
        for gate, payload in rec["gates"].items():
            for cost, result in payload["costs"].items():
                rows.append(
                    {
                        "symbol": rec["symbol"],
                        "family": rec["family"],
                        "window": rec["window"],
                        "gate": gate,
                        "cost": cost,
                        "forced": result["forced_return"],
                        "trades": result["trades"],
                    }
                )
    return pd.DataFrame(rows)


def main() -> None:
    df = load()
    for family in sorted(df["family"].unique()):
        sub = df[(df.family == family) & (df.gate == "q80") & (df.cost == "crypto-taker")]
        pivot = sub.pivot_table(index="window", columns="symbol", values="forced")
        pivot["portfolio"] = pivot.mean(axis=1)
        print(f"===== {family} (q80, crypto-taker, forced) =====")
        print(pivot.round(4).to_string())
        portfolio_mean = pivot["portfolio"].mean()
        print(f"portfolio mean/window: {portfolio_mean:+.4f}")
        print(f"per-symbol means: {pivot.drop(columns='portfolio').mean().round(4).to_dict()}")
        print(f"windows positive: {int((pivot['portfolio'] > 0).sum())}/{len(pivot)}")
        drop_one = {
            window: round(pivot.drop(index=window)["portfolio"].mean(), 4) for window in pivot.index
        }
        print(f"portfolio mean after dropping each window: {drop_one}")
        stress = df[(df.family == family) & (df.gate == "q80") & (df.cost == "stress")]
        stress_pivot = stress.pivot_table(index="window", columns="symbol", values="forced")
        print(f"stress portfolio mean/window: {stress_pivot.mean(axis=1).mean():+.4f}")
        for gate in ("q70", "q90"):
            g = df[(df.family == family) & (df.gate == gate) & (df.cost == "crypto-taker")]
            gp = g.pivot_table(index="window", columns="symbol", values="forced")
            print(f"{gate} portfolio mean/window: {gp.mean(axis=1).mean():+.4f}")
        trades = sub["trades"].sum()
        print(f"total trades (q80, all folds, both symbols): {int(trades)}")
        print()


if __name__ == "__main__":
    main()
