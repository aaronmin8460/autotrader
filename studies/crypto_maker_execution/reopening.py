"""Counterfactual reopening accounting at lower round-trip costs.

Uses the exact identity from the cost-aware study (§2.2): under all-in
sizing on a cost-invariant trade schedule, one round trip multiplies
equity by (1 + r) / (1 + B). For a stored (frictionless, crypto-taker)
result pair of the same cell, the implied round-trip count is

    N = ln((1 + net_frictionless) / (1 + net_taker)) / ln(1 + B_taker)

and the counterfactual net at a different round-trip cost C is

    net_C = (1 + net_frictionless) * (1 + C)^(-N) - 1.

Deriving N from the pair (rather than trusting a trade-count column)
absorbs open-position and partial-side effects exactly. Schedule
invariance across cost models was verified by the cost-aware study and
re-verified by the funding/basis pilot's ledger checks.

Also recomputes the economic-horizon bands (share of bars whose |move|
clears a given round trip) from the digest-verified parquets.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pandas as pd

from studies.crypto_maker_execution.bars import bars_for
from studies.crypto_maker_execution.venue import taker_baseline_break_even

CANDIDATE_COSTS_BPS = (20.0, 30.0, 40.0, 50.0)
HORIZONS_BARS = (4, 16, 32, 96)


def reports_root() -> Path:
    qa = os.environ.get("AUTOTRADER_QA", "/Volumes/AUTOTRADER_QA")
    return Path(qa) / "reports"


def counterfactual_net(
    net_frictionless: float, net_taker: float, cost_rt: float
) -> tuple[float, float]:
    """(implied round trips, counterfactual net) for one stored cell pair."""
    b_taker = taker_baseline_break_even()
    gross = 1.0 + net_frictionless
    ratio = gross / (1.0 + net_taker)
    if gross <= 0 or ratio <= 0:
        return float("nan"), float("nan")
    implied_n = math.log(ratio) / math.log(1.0 + b_taker)
    return implied_n, gross * (1.0 + cost_rt) ** (-implied_n) - 1.0


def tsmom_counterfactuals() -> pd.DataFrame:
    """I2 trend rules — the one cost-caused prior rejection."""
    path = (
        reports_root() / "crypto-deep-architecture" / "iteration2" / "trend_results_development.csv"
    )
    raw = pd.read_csv(path)
    wide = raw.pivot_table(
        index=["symbol", "window", "rule"], columns="cost", values="net_return"
    ).reset_index()
    rows = []
    for _, record in wide.iterrows():
        frictionless = float(record["frictionless"])
        taker = float(record["crypto-taker"])
        implied_n, _ = counterfactual_net(frictionless, taker, 0.0)
        entry = {
            "symbol": record["symbol"],
            "window": record["window"],
            "rule": record["rule"],
            "net_frictionless": frictionless,
            "net_taker": taker,
            "implied_round_trips": implied_n,
        }
        for cost_bps in CANDIDATE_COSTS_BPS:
            _, net = counterfactual_net(frictionless, taker, cost_bps / 1e4)
            entry[f"net_at_{int(cost_bps)}bps"] = net
        rows.append(entry)
    return pd.DataFrame(rows)


def da_spread_counterfactuals() -> pd.DataFrame:
    """DA-SPREAD-96 cells (dev + extended eras), q80 gate, forced basis."""
    base = reports_root() / "crypto-deep-architecture" / "iteration4"
    rows = []
    for directory in (base / "cells", base / "cells_extended"):
        for path in sorted(directory.glob("*gradient-boosted*.json")):
            cell = json.loads(path.read_text())
            costs = cell["gates"]["q80"]["costs"]
            frictionless = costs["frictionless"]["forced_return"]
            taker = costs["crypto-taker"]["forced_return"]
            implied_n, _ = counterfactual_net(frictionless, taker, 0.0)
            entry = {
                "symbol": cell["symbol"],
                "window": cell["window"],
                "net_frictionless": frictionless,
                "net_taker": taker,
                "trades": costs["crypto-taker"]["trades"],
                "implied_round_trips": implied_n,
            }
            for cost_bps in CANDIDATE_COSTS_BPS:
                _, net = counterfactual_net(frictionless, taker, cost_bps / 1e4)
                entry[f"net_at_{int(cost_bps)}bps"] = net
            rows.append(entry)
    return pd.DataFrame(rows)


def horizon_bands() -> pd.DataFrame:
    """Share of bars whose |forward move| clears a round trip, per horizon."""
    rows = []
    for symbol in ("BTC/USD", "ETH/USD"):
        frame = bars_for(symbol)
        opens = frame["open"]
        scored = frame[frame["timestamp"] >= pd.Timestamp("2025-01-01", tz="UTC")]
        for horizon in HORIZONS_BARS:
            entry_price = opens.shift(-1)
            exit_price = opens.shift(-1 - horizon)
            move = (exit_price / entry_price - 1.0).loc[scored.index].dropna()
            entry = {
                "symbol": symbol,
                "horizon_bars": horizon,
                "median_abs_move_bps": float(move.abs().median() * 1e4),
                "bars": len(move),
            }
            for cost_bps in (*CANDIDATE_COSTS_BPS, 60.18):
                threshold = cost_bps / 1e4
                entry[f"abs_clears_{cost_bps:g}bps_pct"] = float(
                    (move.abs() > threshold).mean() * 100.0
                )
                entry[f"up_clears_{cost_bps:g}bps_pct"] = float((move > threshold).mean() * 100.0)
            rows.append(entry)
    return pd.DataFrame(rows)


def write_artifacts() -> None:
    root = reports_root() / "crypto-maker-execution"
    tsmom = tsmom_counterfactuals()
    tsmom.to_csv(root / "reopening_tsmom_counterfactuals.csv", index=False)
    spread = da_spread_counterfactuals()
    spread.to_csv(root / "reopening_da_spread_counterfactuals.csv", index=False)
    bands = horizon_bands()
    bands.to_csv(root / "reopening_horizon_bands.csv", index=False)

    summary: dict = {}
    taker_only = tsmom[~tsmom["rule"].isin(["buy_and_hold"])]
    for cost_bps in CANDIDATE_COSTS_BPS:
        column = f"net_at_{int(cost_bps)}bps"
        by_rule = taker_only.groupby("rule")[column].mean().sort_values(ascending=False).head(5)
        summary[f"tsmom_best_rules_mean_per_window_at_{int(cost_bps)}bps"] = {
            rule: round(float(value), 5) for rule, value in by_rule.items()
        }
        spread_portfolio = spread.groupby("window")[column].mean()
        summary[f"da_spread_mean_per_window_at_{int(cost_bps)}bps"] = round(
            float(spread_portfolio.mean()), 5
        )
    (root / "reopening_analysis.json").write_text(json.dumps(summary, indent=1))
    print("reopening artifacts written")


if __name__ == "__main__":
    write_artifacts()
