"""Aggregate the scored cells into the pilot's predeclared readings.

Everything here is arithmetic over checkpoint files. It computes the three
success-threshold components exactly as `pilot-designs.md` fixed them, plus
the robustness attacks the mandate requires, and writes the artifacts the
final report cites. No thresholds are invented here; the constants below are
transcribed from the predeclaration.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import numpy as np

OUTPUT_DIR = Path("/Volumes/AUTOTRADER_QA/reports/crypto-funding-basis-pilot")
CELLS_DIR = OUTPUT_DIR / "cells"

#: Predeclared success threshold, transcribed - not re-derived.
REQUIRED_WINDOW_WINS = 12
REQUIRED_WINDOW_TOTAL = 17
REQUIRED_NET_IMPROVEMENT_PER_WINDOW = 0.015
#: The prior program's log-loss materiality bar.
MATERIALITY = 0.002

PRIMARY_HORIZON = 96
PRIMARY_GATE = "q80"
PRIMARY_COST = "crypto-taker"

ERA_2021_23 = tuple(f"X{i:02d}" for i in range(1, 10))
ERA_2024_26 = ("P3", "W01", "W02", "W03", "W04", "W05", "W06", "W07")


def load_cells() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(CELLS_DIR.glob("*.json"))]


def index_cells(cells: list[dict]) -> dict:
    """(arm, symbol, horizon, window) -> cell, for ok cells only."""
    return {
        (c["arm"], c["symbol"], c["horizon"], c["window"]): c
        for c in cells
        if c.get("status") == "ok"
    }


def economic(cell: dict, gate: str = PRIMARY_GATE, cost: str = PRIMARY_COST) -> dict:
    return cell["gates"][gate]["costs"][cost]


def paired_records(
    index: dict, treatment: str, horizon: int, baseline: str = "baseline"
) -> list[dict]:
    """One record per (symbol, window) where both arms scored."""
    out = []
    for (arm, symbol, cell_horizon, window), cell in index.items():
        if arm != treatment or cell_horizon != horizon:
            continue
        base = index.get((baseline, symbol, horizon, window))
        if base is None:
            continue
        base_econ, treat_econ = economic(base), economic(cell)
        out.append(
            {
                "symbol": symbol,
                "window": window,
                "era": "2021-23" if window in ERA_2021_23 else "2024-26",
                "baseline_log_loss": base["predictive"]["log_loss"],
                "treatment_log_loss": cell["predictive"]["log_loss"],
                "delta_log_loss": cell["predictive"]["log_loss"] - base["predictive"]["log_loss"],
                "baseline_decision_log_loss": base["decision"]["log_loss"],
                "treatment_decision_log_loss": cell["decision"]["log_loss"],
                "baseline_auc_up": base["predictive"]["per_side"]["up"]["roc_auc"],
                "treatment_auc_up": cell["predictive"]["per_side"]["up"]["roc_auc"],
                "baseline_auc_down": base["predictive"]["per_side"]["down"]["roc_auc"],
                "treatment_auc_down": cell["predictive"]["per_side"]["down"]["roc_auc"],
                "baseline_brier": np.mean(
                    [base["predictive"]["per_side"][s]["brier"] for s in ("up", "down")]
                ),
                "treatment_brier": np.mean(
                    [cell["predictive"]["per_side"][s]["brier"] for s in ("up", "down")]
                ),
                "baseline_ece": np.mean(
                    [base["predictive"]["per_side"][s]["ece"] for s in ("up", "down")]
                ),
                "treatment_ece": np.mean(
                    [cell["predictive"]["per_side"][s]["ece"] for s in ("up", "down")]
                ),
                "baseline_null_log_loss": base["predictive"]["null_log_loss"],
                "treatment_null_log_loss": cell["predictive"]["null_log_loss"],
                "baseline_forced": base_econ["forced_return"],
                "treatment_forced": treat_econ["forced_return"],
                "delta_forced": treat_econ["forced_return"] - base_econ["forced_return"],
                "baseline_net": base_econ["net_return"],
                "treatment_net": treat_econ["net_return"],
                "baseline_trades": base_econ["trades"],
                "treatment_trades": treat_econ["trades"],
                "baseline_max_drawdown": base_econ["max_drawdown"],
                "treatment_max_drawdown": treat_econ["max_drawdown"],
                "baseline_hit_rate": base_econ["ledger"]["hit_rate"],
                "treatment_hit_rate": treat_econ["ledger"]["hit_rate"],
                "baseline_average_trade": base_econ["ledger"]["average_trade"],
                "treatment_average_trade": treat_econ["ledger"]["average_trade"],
                "baseline_time_in_market": base_econ["time_in_market"],
                "treatment_time_in_market": treat_econ["time_in_market"],
                "decision_days": cell["decision"]["decision_days"],
            }
        )
    return sorted(out, key=lambda r: (r["symbol"], r["window"]))


def window_level(records: list[dict]) -> list[dict]:
    """Collapse the two symbols into one portfolio figure per window."""
    grouped = defaultdict(list)
    for record in records:
        grouped[record["window"]].append(record)
    out = []
    for window, rows in grouped.items():
        out.append(
            {
                "window": window,
                "era": rows[0]["era"],
                "symbols": len(rows),
                "baseline_log_loss": float(np.mean([r["baseline_log_loss"] for r in rows])),
                "treatment_log_loss": float(np.mean([r["treatment_log_loss"] for r in rows])),
                "delta_log_loss": float(np.mean([r["delta_log_loss"] for r in rows])),
                "baseline_forced": float(np.mean([r["baseline_forced"] for r in rows])),
                "treatment_forced": float(np.mean([r["treatment_forced"] for r in rows])),
                "delta_forced": float(np.mean([r["delta_forced"] for r in rows])),
            }
        )
    return sorted(out, key=lambda r: r["window"])


def verdict(records: list[dict]) -> dict:
    """The three predeclared success components, evaluated literally."""
    windows = window_level(records)
    wins = [w for w in windows if w["delta_log_loss"] < 0]
    era_2021 = [w for w in windows if w["era"] == "2021-23"]
    era_2024 = [w for w in windows if w["era"] == "2024-26"]

    mean_delta_forced = float(np.mean([w["delta_forced"] for w in windows])) if windows else 0.0
    era_2021_delta = float(np.mean([w["delta_forced"] for w in era_2021])) if era_2021 else 0.0
    era_2024_delta = float(np.mean([w["delta_forced"] for w in era_2024])) if era_2024 else 0.0

    # Drop-one attacks: does the thesis survive losing its best contributor?
    # "Strongest" means the largest improvement over baseline - the window
    # doing the most to carry the result - so the attack removes the maximum,
    # not the minimum.
    by_delta = sorted(windows, key=lambda w: w["delta_forced"], reverse=True)
    strongest_window = by_delta[0]["window"] if by_delta else None
    without_strongest = [w for w in windows if w["window"] != strongest_window]
    drop_window_delta = (
        float(np.mean([w["delta_forced"] for w in without_strongest])) if without_strongest else 0.0
    )

    symbol_deltas = defaultdict(list)
    for record in records:
        symbol_deltas[record["symbol"]].append(record["delta_forced"])
    symbol_means = {s: float(np.mean(v)) for s, v in symbol_deltas.items()}
    strongest_symbol = max(symbol_means, key=symbol_means.get) if symbol_means else None
    without_symbol = [r for r in records if r["symbol"] != strongest_symbol]
    drop_symbol_delta = (
        float(np.mean([w["delta_forced"] for w in window_level(without_symbol)]))
        if without_symbol
        else 0.0
    )

    # Concentration: how many windows carry the improvement?
    improving = [w for w in windows if w["delta_forced"] > 0]

    return {
        "windows_scored": len(windows),
        "log_loss_wins": len(wins),
        "log_loss_wins_required": REQUIRED_WINDOW_WINS,
        "log_loss_win_windows": [w["window"] for w in wins],
        "mean_delta_log_loss": float(np.mean([w["delta_log_loss"] for w in windows]))
        if windows
        else 0.0,
        "mean_delta_log_loss_material": abs(
            float(np.mean([w["delta_log_loss"] for w in windows])) if windows else 0.0
        )
        >= MATERIALITY,
        "mean_delta_forced_per_window": mean_delta_forced,
        "net_improvement_required": REQUIRED_NET_IMPROVEMENT_PER_WINDOW,
        "era_2021_23_delta_forced": era_2021_delta,
        "era_2024_26_delta_forced": era_2024_delta,
        "strongest_window": strongest_window,
        "delta_forced_without_strongest_window": drop_window_delta,
        "strongest_symbol": strongest_symbol,
        "delta_forced_without_strongest_symbol": drop_symbol_delta,
        "windows_with_positive_delta_forced": len(improving),
        "criterion_1_log_loss_wins": len(wins) >= REQUIRED_WINDOW_WINS,
        "criterion_2_net_improvement": mean_delta_forced >= REQUIRED_NET_IMPROVEMENT_PER_WINDOW,
        "criterion_3_era_2021_23_not_negative": era_2021_delta >= 0.0,
        "all_criteria_met": (
            len(wins) >= REQUIRED_WINDOW_WINS
            and mean_delta_forced >= REQUIRED_NET_IMPROVEMENT_PER_WINDOW
            and era_2021_delta >= 0.0
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--horizon", type=int, default=PRIMARY_HORIZON)
    args = parser.parse_args()

    cells = load_cells()
    index = index_cells(cells)
    print(f"{len(cells)} cell files, {len(index)} scored ok")

    payload: dict = {"horizon": args.horizon, "arms": {}}
    for treatment in ("augmented", "funding_only", "basis_only"):
        records = paired_records(index, treatment, args.horizon)
        if not records:
            continue
        payload["arms"][treatment] = {
            "cells": records,
            "windows": window_level(records),
            "verdict": verdict(records),
        }
        v = payload["arms"][treatment]["verdict"]
        print(
            f"\n=== {treatment} vs baseline (h={args.horizon}) ===\n"
            f"  windows scored           : {v['windows_scored']}\n"
            f"  log-loss wins            : {v['log_loss_wins']}/{v['windows_scored']} "
            f"(need {REQUIRED_WINDOW_WINS}/{REQUIRED_WINDOW_TOTAL})\n"
            f"  mean delta log loss      : {v['mean_delta_log_loss']:+.6f} "
            f"(negative = augmented better; material if |x| >= {MATERIALITY})\n"
            f"  mean delta net /window   : {v['mean_delta_forced_per_window']:+.4%} "
            f"(need >= {REQUIRED_NET_IMPROVEMENT_PER_WINDOW:+.2%})\n"
            f"  2021-23 delta            : {v['era_2021_23_delta_forced']:+.4%}\n"
            f"  2024-26 delta            : {v['era_2024_26_delta_forced']:+.4%}\n"
            f"  drop strongest window    : {v['delta_forced_without_strongest_window']:+.4%}\n"
            f"  drop strongest symbol    : {v['delta_forced_without_strongest_symbol']:+.4%}\n"
            f"  criteria (1/2/3)         : {v['criterion_1_log_loss_wins']} / "
            f"{v['criterion_2_net_improvement']} / {v['criterion_3_era_2021_23_not_negative']}\n"
            f"  ALL CRITERIA MET         : {v['all_criteria_met']}"
        )

    path = OUTPUT_DIR / f"incremental_metrics_h{args.horizon}.json"
    path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
