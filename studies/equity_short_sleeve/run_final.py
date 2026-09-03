"""Final tournament assembler: one machine-readable table over every artifact.

Reads only what previous phases wrote; computes nothing new except the
predeclared §L11 gate evaluation, so the table cannot disagree with the
phase reports it summarizes.

Usage:
    python -m studies.equity_short_sleeve.run_final
"""

from __future__ import annotations

import json
from pathlib import Path

from studies.equity_deep_arch.evaluate import write_json
from studies.equity_short_sleeve import REPORT_ROOT

ROOT = Path(REPORT_ROOT)

#: §L11 thresholds, transcribed from the ledger. Not editable here.
RETENTION_BAR = 0.90
SHARPE_BAR = 0.10
MAXDD_RELATIVE_BAR = 0.20
BEAR_BAR = 0.015
CRASH_BAR = 0.015
WORST_SESSION_BAR = -0.010
WORST_5SESSION_BAR = -0.025


def gate(row: dict, b0: dict) -> dict[str, object]:
    """The predeclared §L11 gate, applied mechanically."""
    m, m0 = row["metrics"], b0["metrics"]
    retention = row["net_return"] / b0["net_return"]
    d_sharpe = m["sharpe_ratio"] - m0["sharpe_ratio"]
    # Positive = the candidate's drawdown is SHALLOWER than B0's, by that
    # fraction of B0's depth. Both drawdowns are stored negative, so the
    # comparison is on magnitudes.
    dd_relative = (abs(m0["max_drawdown"]) - abs(m["max_drawdown"])) / abs(m0["max_drawdown"])
    d_bear = row["mean_negative_window_return"] - b0["mean_negative_window_return"]
    d_crash = row["window_returns"]["w09"] - b0["window_returns"]["w09"]
    sleeve = row["sleeve"]
    routes = {
        "sharpe": d_sharpe >= SHARPE_BAR,
        "maxdd_relative": dd_relative >= MAXDD_RELATIVE_BAR,
        "bear_and_crash": d_bear >= BEAR_BAR and d_crash >= CRASH_BAR,
    }
    return {
        "retention": retention,
        "requirement_1_retention": retention >= RETENTION_BAR,
        "delta_sharpe": d_sharpe,
        "maxdd_relative_improvement": dd_relative,
        "delta_bear_pts": d_bear,
        "delta_crash_pts": d_crash,
        "requirement_2_routes": routes,
        "requirement_2_any": any(routes.values()),
        "requirement_3_no_catastrophe": (
            sleeve["worst_session_contribution_pct"] >= WORST_SESSION_BAR
            and sleeve["post_transition_5_worst_pct"] >= WORST_5SESSION_BAR
        ),
        "gate_passes": retention >= RETENTION_BAR and any(routes.values()),
    }


def main() -> None:
    tournament = json.loads((ROOT / "phase4" / "tournament.json").read_text())
    netted = json.loads((ROOT / "phase5" / "netted.json").read_text())["rows"]
    b0 = tournament["B0"]

    table: dict[str, object] = {
        "baseline": {
            "label": "B0 — EDA-1 U10 weighted bridge",
            "net_return": b0["net_return"],
            "cagr": b0["metrics"]["annualized_return"],
            "sharpe": b0["metrics"]["sharpe_ratio"],
            "sortino": b0["metrics"]["sortino_ratio"],
            "max_drawdown": b0["metrics"]["max_drawdown"],
            "turnover": b0["turnover"],
            "mean_negative_window_return": b0["mean_negative_window_return"],
            "w09": b0["window_returns"]["w09"],
            "up_capture": b0["up_capture"],
            "down_capture": b0["down_capture"],
            "exposure": b0["exposure"],
        },
        "gate_thresholds": {
            "retention": RETENTION_BAR,
            "sharpe": SHARPE_BAR,
            "maxdd_relative": MAXDD_RELATIVE_BAR,
            "bear_pts": BEAR_BAR,
            "crash_pts": CRASH_BAR,
        },
        "rows": {},
    }

    for label, row in list(tournament["rows"].items()) + list(netted.items()):
        m, sleeve, exposure = row["metrics"], row["sleeve"], row["exposure"]
        table["rows"][label] = {
            "net_return": row["net_return"],
            "cagr": m["annualized_return"],
            "sharpe": m["sharpe_ratio"],
            "sortino": m["sortino_ratio"],
            "max_drawdown": m["max_drawdown"],
            "turnover": row["turnover"],
            "short_turnover": row["short_turnover"],
            "borrow_cost_pct": row["borrow_cost"] / 1_000_000.0,
            "mean_negative_window_return": row["mean_negative_window_return"],
            "w09": row["window_returns"]["w09"],
            "w11": row["window_returns"]["w11"],
            "up_capture": row["up_capture"],
            "down_capture": row["down_capture"],
            "long_gross_mean": exposure["long_gross_mean"],
            "short_gross_mean": exposure["short_gross_mean"],
            "short_gross_max": exposure["short_gross_max"],
            "total_gross_mean": exposure["total_gross_mean"],
            "net_exposure_mean": exposure["net_exposure_mean"],
            "mean_short_names": exposure["mean_short_names"],
            "short_pnl_pct": sleeve["short_pnl_pct_of_initial"],
            "short_pnl_defensive": sleeve["short_pnl_defensive"] / 1_000_000.0,
            "short_pnl_participate": sleeve["short_pnl_participate"] / 1_000_000.0,
            "short_hit_rate": sleeve["short_hit_rate"],
            "short_profit_factor": sleeve["profit_factor"],
            "short_sharpe": sleeve["short_sharpe"],
            "worst_session_contribution_pct": sleeve["worst_session_contribution_pct"],
            "best_session_contribution_pct": sleeve["best_session_contribution_pct"],
            "post_transition_5_worst_pct": sleeve["post_transition_5_worst_pct"],
            "long_book_non_regression": row.get("long_book_non_regression", "CONTROL"),
            "reconciliation_error": row["reconciliation_error"],
            "gate": gate(row, b0),
        }

    passers = [k for k, v in table["rows"].items() if v["gate"]["gate_passes"]]
    table["gate_passers"] = passers
    table["verdict"] = "PASS" if passers else "NO CANDIDATE PASSES §L11"
    table["rows_evaluated"] = len(table["rows"])
    write_json(ROOT / "final-tournament-table.json", table)
    print(f"rows {len(table['rows'])}; gate passers: {passers or 'none'}", flush=True)
    best = max(table["rows"], key=lambda k: table["rows"][k]["net_return"])
    print(f"least-bad by net: {best} ({table['rows'][best]['net_return']:.4f})", flush=True)


if __name__ == "__main__":
    main()
