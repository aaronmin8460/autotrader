"""Phase-3 runner: tradeoff metrics, Pareto table, §L7 gate evaluation.

Pure analysis of stored Phase-2 artifacts — no replay. Emits
`phase3/pareto_gates.json` and prints the verdicts.

Usage:
    python -m studies.equity_two_sleeve.run_phase3
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from studies.equity_deep_arch.evaluate import write_json
from studies.equity_two_sleeve import REPORT_ROOT
from studies.equity_two_sleeve.blend import RATIOS

OUT = Path(REPORT_ROOT) / "phase3"

#: §L6 Pareto axes: (key, better-direction). Pullback bleed and turnover are
#: "less negative / smaller is better".
AXES = ("net", "sharpe", "maxdd", "pullback", "turnover")


def _log(message: str) -> None:
    print(message, flush=True)


def row_metrics(block: dict) -> dict[str, float]:
    primary = block["equity-marketable"]
    metrics = primary["metrics"]
    stress = block["stress"]
    return {
        "net": float(primary["net_return"]),
        "cagr": float(metrics["annualized_return"]),
        "sharpe": float(metrics["sharpe_ratio"]),
        "sortino": float(metrics["sortino_ratio"]),
        "maxdd": float(metrics["max_drawdown"]),
        "calmar": float(metrics["annualized_return"] / abs(metrics["max_drawdown"])),
        "pullback": float(primary["regime_table"]["pullback"]["annualized_mean_return"]),
        "drawdown_state": float(primary["regime_table"]["drawdown"]["annualized_mean_return"]),
        "calm": float(primary["regime_table"]["calm"]["annualized_mean_return"]),
        "w09": float(primary["window_returns"]["w09"]),
        "turnover": float(primary["turnover"]),
        "exposure_mean": float(primary["exposure_mean"]),
        "cost_drag": float(metrics["cost_drag"]),
        "stress_net": float(stress["net_return"]),
        "neg_window_mean": float(primary["mean_negative_window_return"]),
        "pos_window_mean": float(primary["mean_positive_window_return"]),
        "max_symbol_weight": float(primary["max_symbol_weight_assigned"]),
        "forced_net": float(primary["forced_liquidation_net"]),
    }


def dominates(a: dict[str, float], b: dict[str, float]) -> bool:
    """a dominates b on the §L6 axes (≥ on all, > on ≥ 1); maxdd/pullback are
    'less negative is better', turnover 'smaller is better'."""
    ge = (
        a["net"] >= b["net"],
        a["sharpe"] >= b["sharpe"],
        a["maxdd"] >= b["maxdd"],
        a["pullback"] >= b["pullback"],
        a["turnover"] <= b["turnover"],
    )
    gt = (
        a["net"] > b["net"],
        a["sharpe"] > b["sharpe"],
        a["maxdd"] > b["maxdd"],
        a["pullback"] > b["pullback"],
        a["turnover"] < b["turnover"],
    )
    return all(ge) and any(gt)


def main() -> None:
    started = time.perf_counter()
    blends = json.loads((Path(REPORT_ROOT) / "phase2" / "blends.json").read_text())
    bridge = json.loads((Path(REPORT_ROOT) / "baseline" / "bridge_u10.json").read_text())[
        "EDA1_weighted_bridge"
    ]

    rows: dict[str, dict[str, float]] = {"T0": row_metrics(bridge)}
    for label in (*RATIOS, "CTRL_SE_90", *(f"CTRL_GEN_{k[1:]}" for k in RATIOS)):
        rows[label] = row_metrics(blends[label])

    t0 = rows["T0"]
    for label, row in rows.items():
        if label == "T0":
            continue
        row["return_retention"] = row["net"] / t0["net"]
        row["sharpe_improvement"] = row["sharpe"] - t0["sharpe"]
        row["maxdd_improvement_pts"] = (row["maxdd"] - t0["maxdd"]) * 100.0
        row["pullback_improvement"] = row["pullback"] - t0["pullback"]
        row["turnover_delta"] = row["turnover"] - t0["turnover"]

    pareto_entrants = ("T0", *RATIOS)
    dominated: dict[str, list[str]] = {}
    for label in pareto_entrants:
        by = [
            other
            for other in pareto_entrants
            if other != label and dominates(rows[other], rows[label])
        ]
        if by:
            dominated[label] = by

    gates: dict[str, dict[str, object]] = {}
    for label in RATIOS:
        row = rows[label]
        g1_retention = row["net"] >= 0.90 * t0["net"]
        g1_frontier = (
            row["sharpe"] >= t0["sharpe"] + 0.10
            and row["maxdd"] >= t0["maxdd"] + 0.03
            and row["net"] >= 0.75 * t0["net"]
        )
        gates[label] = {
            "G1": g1_retention or g1_frontier,
            "G1_via": "retention" if g1_retention else ("frontier" if g1_frontier else "fail"),
            "G2_sharpe": row["sharpe"] >= t0["sharpe"] + 0.05,
            "G3_maxdd": row["maxdd"] >= t0["maxdd"] + 0.02,
            "G4_pullback": row["pullback"] > t0["pullback"],
            "G4_crash_w09": row["w09"] >= t0["w09"] - 0.005,
            "G6_turnover": row["turnover"] <= 1.3 * t0["turnover"],
            "G6_max_weight": row["max_symbol_weight"] <= 0.10 + 1e-9,
            "G6_stress": row["stress_net"] > 0 and row["stress_net"] >= 0.5 * t0["stress_net"],
            "hard_floor_75pct": row["net"] >= 0.75 * t0["net"],
        }
        gates[label]["pre_attack_pass"] = all(
            v for k, v in gates[label].items() if k not in ("G1_via",)
        )

    def _v1_v2(label: str) -> dict[str, object]:
        row = rows[label]
        se = rows["CTRL_SE_90"]
        gen = rows[f"CTRL_GEN_{label[1:]}"]
        v1 = (
            (row["sharpe"] >= se["sharpe"] + 0.03 or row["net"] >= se["net"] + 0.05)
            and row["maxdd"] >= se["maxdd"] - 0.01
            and row["sharpe"] >= se["sharpe"] - 0.03
            and row["net"] >= se["net"] - 0.05
        )
        v2 = (
            row["net"] >= gen["net"] + 0.05
            and row["sharpe"] >= gen["sharpe"] - 0.02
            and row["maxdd"] >= gen["maxdd"] - 0.01
        )
        return {"V1_vs_same_exposure": v1, "V2_vs_generic_sleeve": v2}

    value_beyond_exposure = {label: _v1_v2(label) for label in RATIOS}

    payload = {
        "rows": rows,
        "dominated": dominated,
        "gates": gates,
        "value_beyond_exposure": value_beyond_exposure,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "pareto_gates.json", payload)

    for label in ("T0", *RATIOS, "CTRL_SE_90", *(f"CTRL_GEN_{k[1:]}" for k in RATIOS)):
        row = rows[label]
        _log(
            f"{label:12s} net {row['net']:+.4f}  sharpe {row['sharpe']:.3f}  "
            f"maxDD {row['maxdd']:+.4f}  pullback {row['pullback']:+.3f}  "
            f"w09 {row['w09']:+.4f}  turn {row['turnover']:.1f}  "
            f"stress {row['stress_net']:+.4f}"
        )
    _log(f"dominated: {dominated}")
    for label in RATIOS:
        _log(f"{label}: gates {gates[label]}  value {value_beyond_exposure[label]}")
    _log(f"phase3 complete in {time.perf_counter() - started:.0f}s")


if __name__ == "__main__":
    main()
