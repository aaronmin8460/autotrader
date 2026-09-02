"""Phase-4 runner: real-data reduction identity, then the S1–S4 tournament
(ledger §L5.1, §L6, §L7, §L13).

Stages:
    identity  — replay_signed reproduces replay_weighted on the REAL EDA-1
                U10 bridge targets, field for field (§L5.1). Gate.
    plans     — build and diagnose every predeclared short plan, no replay.
    tournament— replay every predeclared row at the primary cost/borrow pair.

Usage:
    python -m studies.equity_short_sleeve.run_phase4 --stage identity
    python -m studies.equity_short_sleeve.run_phase4 --stage plans
    python -m studies.equity_short_sleeve.run_phase4 --stage tournament
"""

from __future__ import annotations

import argparse
import time
from decimal import Decimal
from pathlib import Path

from autotrader.research.costs import EQUITY_COST, STRESS_COST, ZERO_COST, CostModel
from studies.equity_deep_arch.evaluate import write_json
from studies.equity_eda1_nextgen.weighted_replay import replay_weighted
from studies.equity_short_sleeve import REPORT_ROOT
from studies.equity_short_sleeve.candidates import (
    GROSS_GRID,
    MAX_SHORT_GROSS,
    apply_short_plan,
    index_short_plan,
    plan_diagnostics,
    selected_short_plan,
)
from studies.equity_short_sleeve.context import ShortContext, targets_digest
from studies.equity_short_sleeve.report import signed_report
from studies.equity_short_sleeve.shorts import (
    BORROW_ZERO,
    PRIMARY_BORROW,
    replay_signed,
)

OUT = Path(REPORT_ROOT) / "phase4"

#: §L7 short-side execution friction. LONG stays the inherited 2 bp/side.
SHORT_COST_BASE = CostModel(
    label="short-marketable", fee_rate=Decimal("0"), slippage_rate=Decimal("0.0004")
)
SHORT_COST_STRESS = CostModel(
    label="short-stress", fee_rate=Decimal("0"), slippage_rate=Decimal("0.0008")
)
LONG_COST_STRESS = CostModel(
    label="equity-2x", fee_rate=Decimal("0"), slippage_rate=Decimal("0.0004")
)
SHORT_COST_SEVERE = CostModel(
    label="short-severe", fee_rate=Decimal("0.005"), slippage_rate=Decimal("0.004")
)

#: The declared cost ladder: (label, long model, short model).
COST_LADDER = (
    ("COST_BASE", EQUITY_COST, SHORT_COST_BASE),
    ("COST_STRESS", LONG_COST_STRESS, SHORT_COST_STRESS),
    ("COST_SEVERE", STRESS_COST, SHORT_COST_SEVERE),
)

SHARED_FIELDS = (
    "timestamps",
    "equity_curve",
    "initial_cash",
    "final_equity",
    "forced_liquidation_net",
    "fill_count",
    "traded_notional",
    "total_fees",
    "total_slippage",
    "exposure_mean",
    "exposure_bars",
    "max_active_names",
    "mean_active_names",
    "max_symbol_weight_assigned",
    "turnover",
)


def _log(message: str) -> None:
    print(message, flush=True)


def run_identity() -> None:
    """§L5.1 on real data: the signed engine IS the inherited engine when no
    weight is negative. This gate precedes every short number."""
    context = ShortContext()
    report: dict[str, object] = {"long_targets_digest": context.long_digest}
    all_pass = True
    for cost_label, cost in (("frictionless", ZERO_COST), ("equity-marketable", EQUITY_COST)):
        inherited = replay_weighted(
            context.long_frames, context.long_targets, cost, label="EDA1_BRIDGE"
        )
        signed = replay_signed(
            context.long_frames,
            context.long_targets,
            cost,
            label="EDA1_BRIDGE",
            short_cost_model=SHORT_COST_SEVERE,  # unreachable; must not matter
            borrow=PRIMARY_BORROW,  # unreachable; must charge nothing
        )
        mismatches = [
            name for name in SHARED_FIELDS if getattr(signed, name) != getattr(inherited, name)
        ]
        entry = {
            "mismatched_fields": mismatches,
            "status": "IDENTICAL" if not mismatches else "DIFFERENT",
            "inherited_net": inherited.net_return,
            "signed_net": signed.net_return,
            "borrow_cost": signed.borrow_cost,
            "short_fills": signed.short_fill_count,
            "short_pnl": signed.short_pnl,
            "reconciliation_error": signed.reconciliation_error,
        }
        if mismatches or signed.borrow_cost != 0.0 or signed.short_fill_count != 0:
            all_pass = False
            entry["status"] = "DIFFERENT"
        report[cost_label] = entry
        _log(f"  {cost_label}: {entry['status']} (net {signed.net_return:.10f})")
    report["identity_gate"] = "PASS" if all_pass else "FAIL"
    write_json(OUT / "identity.json", report)
    _log(f"§L5.1 identity gate: {report['identity_gate']}")


def build_plans(context: ShortContext) -> dict[str, object]:
    """Every predeclared plan, keyed by row label."""
    plans: dict[str, object] = {}
    sessions, participate = context.sessions, context.participate

    for members in (("SPY",), ("QQQ",), ("QQQ", "SPY")):
        for gross in GROSS_GRID:
            plan = index_short_plan(sessions, participate, gross=gross, members=members)
            plans[plan.label] = plan

    for names in (2, 3):
        for gross in GROSS_GRID:
            plans[f"S2_N{names}_{int(gross * 100)}"] = selected_short_plan(
                sessions,
                participate,
                context.panel,
                context.mark_of,
                context.incumbents,
                label=f"S2_N{names}_{int(gross * 100)}",
                gross=gross,
                names=names,
            )

    for gross in GROSS_GRID:
        plans[f"S3_N5_{int(gross * 100)}"] = selected_short_plan(
            sessions,
            participate,
            context.panel,
            context.mark_of,
            context.universe,
            label=f"S3_N5_{int(gross * 100)}",
            gross=gross,
            names=5,
            characteristic="beta_252",
        )
    plans["S3_N3_10"] = selected_short_plan(
        sessions,
        participate,
        context.panel,
        context.mark_of,
        context.universe,
        label="S3_N3_10",
        gross=0.10,
        names=3,
        characteristic="beta_252",
    )
    # Amendment A2: secondary, non-promotable.
    plans["S3UW_N5_10"] = selected_short_plan(
        sessions,
        participate,
        context.panel,
        context.mark_of,
        context.universe,
        label="S3UW_N5_10",
        gross=0.10,
        names=5,
        characteristic="underwater_252",
    )
    # S4 shares S3's short book; the difference is which long book it rides,
    # which is handled at replay time (the incumbent's own DEFENSIVE V3 book
    # is already the long book in DEFENSIVE, so S4's short book is S3's and
    # its decomposition is reported separately).
    for gross in (0.10, 0.15):
        plans[f"S4_N5_{int(gross * 100)}"] = selected_short_plan(
            sessions,
            participate,
            context.panel,
            context.mark_of,
            context.universe,
            label=f"S4_N5_{int(gross * 100)}",
            gross=gross,
            names=5,
            characteristic="beta_252",
        )
    return plans


def run_plans() -> None:
    context = ShortContext()
    plans = build_plans(context)
    payload = {
        "universe": context.universe,
        "incumbents": list(context.incumbents),
        "defensive_sessions": len(context.defensive_sessions),
        "plans": {
            label: plan_diagnostics(plan, context.participate, context.sessions)
            for label, plan in plans.items()
        },
    }
    write_json(OUT / "plans.json", payload)
    for label, diagnostics in payload["plans"].items():
        _log(
            f"  {label}: on {diagnostics['sessions_with_shorts']}/"
            f"{diagnostics['defensive_sessions']} defensive sessions, "
            f"mean {diagnostics['mean_names_when_on']:.2f} names, "
            f"{diagnostics['distinct_symbols']} distinct symbols"
        )


def run_tournament() -> None:
    context = ShortContext()
    plans = build_plans(context)
    transitions = context.transitions_to_participate()
    payload: dict[str, object] = {
        "long_targets_digest": context.long_digest,
        "transitions_to_participate": [str(t) for t in transitions],
    }

    # B0 through the signed engine (proven identical in --stage identity).
    b0 = replay_signed(
        context.frames, context.long_targets, EQUITY_COST, label="B0", borrow=BORROW_ZERO
    )
    payload["B0"] = signed_report(b0, context.states, context.participate, transitions=transitions)
    _log(f"B0: net {b0.net_return:+.4f} sharpe {b0.metrics().to_json_dict()['sharpe_ratio']:.4f}")

    rows: dict[str, object] = {}
    for label, plan in plans.items():
        started = time.perf_counter()
        targets = apply_short_plan(context.long_targets, context.frames, plan, context.sessions)

        # §L13: the long book must be untouched. Any positive weight in the
        # combined series must equal B0's, exactly.
        def positive_only(source):
            """The strictly-positive subset, with symbols that hold nothing
            dropped — a symbol present only as an all-zero short-book carrier
            is not a change to the long book."""
            kept = {}
            for symbol, series in source.items():
                positives = {s: w for s, w in series.items() if w > 0.0}
                if positives:
                    kept[symbol] = positives
            return kept

        longs_only = positive_only(targets)
        b0_longs = positive_only(context.long_targets)
        non_regression = targets_digest(longs_only) == targets_digest(b0_longs)
        result = replay_signed(
            context.frames,
            targets,
            EQUITY_COST,
            label=label,
            short_cost_model=SHORT_COST_BASE,
            borrow=PRIMARY_BORROW,
            max_short_gross=MAX_SHORT_GROSS + 0.01,
        )
        block = signed_report(result, context.states, context.participate, transitions=transitions)
        block["long_book_non_regression"] = "PASS" if non_regression else "FAIL"
        rows[label] = block
        _log(
            f"  {label}: net {result.net_return:+.4f} "
            f"short_pnl {result.short_pnl / result.initial_cash:+.4f} "
            f"shortgross {result.short_gross_mean:.4f} "
            f"nonreg {'OK' if non_regression else 'FAIL'} "
            f"({time.perf_counter() - started:.0f}s)"
        )
    payload["rows"] = rows
    write_json(OUT / "tournament.json", payload)
    _log("tournament: done")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("identity", "plans", "tournament"))
    arguments = parser.parse_args()
    started = time.perf_counter()
    {"identity": run_identity, "plans": run_plans, "tournament": run_tournament}[arguments.stage]()
    _log(f"stage {arguments.stage} complete in {time.perf_counter() - started:.0f}s")


if __name__ == "__main__":
    main()
