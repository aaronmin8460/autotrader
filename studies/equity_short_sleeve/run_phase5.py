"""Phases 5, 8 and 9: cost/borrow sensitivity, the netted index control, and
the mandatory same-exposure controls (ledger §L7, §L10, amendment A5).

Stages:
    netted   — S1N, the netted index hedge (amendment A5 control).
    costs    — the declared cost ladder x borrow grid on the representative rows.
    controls — net-exposure-matched and gross-matched scaled incumbents, plus
               the index-hedge control at matched realized short gross.

Usage:
    python -m studies.equity_short_sleeve.run_phase5 --stage netted
    python -m studies.equity_short_sleeve.run_phase5 --stage costs
    python -m studies.equity_short_sleeve.run_phase5 --stage controls
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from autotrader.research.costs import EQUITY_COST
from studies.equity_deep_arch.evaluate import write_json
from studies.equity_short_sleeve import REPORT_ROOT
from studies.equity_short_sleeve.candidates import (
    GROSS_GRID,
    MAX_SHORT_GROSS,
    apply_short_plan,
    index_short_plan,
    selected_short_plan,
)
from studies.equity_short_sleeve.context import ShortContext
from studies.equity_short_sleeve.report import signed_report
from studies.equity_short_sleeve.run_phase4 import COST_LADDER, SHORT_COST_BASE
from studies.equity_short_sleeve.shorts import (
    BORROW_MODELS,
    BORROW_ZERO,
    PRIMARY_BORROW,
    replay_signed,
)

OUT = Path(REPORT_ROOT) / "phase5"

#: The rows the sensitivity grid is run on: the index control, the primary
#: hypothesis, and the U10 bridge control — declared before the grid runs.
GRID_ROWS = ("S1_SPY_10", "S3_N5_10", "S2_N2_10")


def _log(message: str) -> None:
    print(message, flush=True)


def scale_targets(targets, factor: float):
    return {
        symbol: {stamp: weight * factor for stamp, weight in series.items()}
        for symbol, series in targets.items()
    }


def _plan_for(context: ShortContext, label: str):
    kind, rest = label.split("_", 1)
    if kind == "S1":
        *members, gross = rest.split("_")
        return index_short_plan(
            context.sessions,
            context.participate,
            gross=int(gross) / 100.0,
            members=tuple(members),
        )
    names, gross = rest.split("_")
    count = int(names[1:])
    if kind == "S2":
        return selected_short_plan(
            context.sessions,
            context.participate,
            context.panel,
            context.mark_of,
            context.incumbents,
            label=label,
            gross=int(gross) / 100.0,
            names=count,
        )
    return selected_short_plan(
        context.sessions,
        context.participate,
        context.panel,
        context.mark_of,
        context.universe,
        label=label,
        gross=int(gross) / 100.0,
        names=count,
        characteristic="beta_252",
    )


def run_netted() -> None:
    context = ShortContext()
    transitions = context.transitions_to_participate()
    rows: dict[str, object] = {}
    for members in (("SPY",), ("QQQ",), ("QQQ", "SPY")):
        for gross in GROSS_GRID:
            plan = index_short_plan(
                context.sessions, context.participate, gross=gross, members=members
            )
            label = f"S1N_{'_'.join(members)}_{int(gross * 100)}"
            targets = apply_short_plan(
                context.long_targets,
                context.frames,
                plan,
                context.sessions,
                net_against_long=True,
            )
            result = replay_signed(
                context.frames,
                targets,
                EQUITY_COST,
                label=label,
                short_cost_model=SHORT_COST_BASE,
                borrow=PRIMARY_BORROW,
                max_short_gross=MAX_SHORT_GROSS + 0.01,
            )
            rows[label] = signed_report(
                result, context.states, context.participate, transitions=transitions
            )
            _log(
                f"  {label}: net {result.net_return:+.4f} "
                f"shortgross {result.short_gross_mean:.4f} "
                f"netexp {result.net_exposure_mean:.4f}"
            )
    write_json(OUT / "netted.json", {"rows": rows})


def run_costs() -> None:
    context = ShortContext()
    payload: dict[str, object] = {}
    for label in GRID_ROWS:
        plan = _plan_for(context, label)
        targets = apply_short_plan(context.long_targets, context.frames, plan, context.sessions)
        block: dict[str, object] = {}
        for cost_label, long_cost, short_cost in COST_LADDER:
            for borrow in BORROW_MODELS:
                result = replay_signed(
                    context.frames,
                    targets,
                    long_cost,
                    label=f"{label}|{cost_label}|{borrow.label}",
                    short_cost_model=short_cost,
                    borrow=borrow,
                    max_short_gross=MAX_SHORT_GROSS + 0.01,
                )
                block[f"{cost_label}|{borrow.label}"] = {
                    "net_return": result.net_return,
                    "sharpe": result.metrics().to_json_dict()["sharpe_ratio"],
                    "max_drawdown": result.metrics().to_json_dict()["max_drawdown"],
                    "short_pnl_pct": result.short_pnl / result.initial_cash,
                    "borrow_cost_pct": result.borrow_cost / result.initial_cash,
                    "short_gross_mean": result.short_gross_mean,
                }
            _log(f"  {label} {cost_label}: done")
        payload[label] = block
    # B0 under the same long-cost ladder, for a matched comparison.
    b0: dict[str, object] = {}
    for cost_label, long_cost, _ in COST_LADDER:
        result = replay_signed(
            context.frames,
            context.long_targets,
            long_cost,
            label=f"B0|{cost_label}",
            borrow=BORROW_ZERO,
        )
        b0[cost_label] = {
            "net_return": result.net_return,
            "sharpe": result.metrics().to_json_dict()["sharpe_ratio"],
            "max_drawdown": result.metrics().to_json_dict()["max_drawdown"],
        }
    payload["B0"] = b0
    write_json(OUT / "costs.json", payload)


def run_controls() -> None:
    """§L10: net-exposure-matched and gross-matched scaled incumbents.

    The scale factors are solved by bisection on the REALIZED mean exposure of
    the scaled incumbent, so the match is on what actually happened rather
    than on a nominal weight.
    """
    context = ShortContext()
    transitions = context.transitions_to_participate()
    targets_of: dict[str, object] = {}
    for label in GRID_ROWS + ("S1N_SPY_10",):
        if label.startswith("S1N"):
            plan = index_short_plan(
                context.sessions, context.participate, gross=0.10, members=("SPY",)
            )
            targets_of[label] = apply_short_plan(
                context.long_targets,
                context.frames,
                plan,
                context.sessions,
                net_against_long=True,
            )
        else:
            targets_of[label] = apply_short_plan(
                context.long_targets,
                context.frames,
                _plan_for(context, label),
                context.sessions,
            )

    def replay(targets, label):
        return replay_signed(
            context.frames,
            targets,
            EQUITY_COST,
            label=label,
            short_cost_model=SHORT_COST_BASE,
            borrow=PRIMARY_BORROW,
            max_short_gross=MAX_SHORT_GROSS + 0.01,
        )

    def scaled(factor: float, label: str):
        # The scaled incumbent is long-only, so the ten U10 frames suffice —
        # proven bit-identical to the 26-frame replay (amendment A6) and ~2x
        # faster, which matters across a 28-step bisection.
        return replay_signed(
            context.long_frames,
            scale_targets(context.long_targets, factor),
            EQUITY_COST,
            label=label,
            borrow=BORROW_ZERO,
        )

    def solve(objective: float, extractor) -> tuple[float, object]:
        low, high = 0.50, 1.0
        best = None
        for _ in range(14):
            mid = (low + high) / 2.0
            result = scaled(mid, f"CTRL_{mid:.5f}")
            value = extractor(result)
            best = (mid, result)
            if value > objective:
                high = mid
            else:
                low = mid
        return best

    payload: dict[str, object] = {}
    for label, targets in targets_of.items():
        candidate = replay(targets, label)
        net_target = candidate.net_exposure_mean
        gross_target = candidate.total_gross_mean
        net_factor, net_control = solve(net_target, lambda r: r.net_exposure_mean)
        gross_factor, gross_control = solve(gross_target, lambda r: r.total_gross_mean)
        payload[label] = {
            "candidate": signed_report(
                candidate, context.states, context.participate, transitions=transitions
            ),
            "net_exposure_target": net_target,
            "gross_exposure_target": gross_target,
            "CTRL_NET": {
                "scale_factor": net_factor,
                "achieved_net_exposure": net_control.net_exposure_mean,
                **signed_report(
                    net_control, context.states, context.participate, transitions=transitions
                ),
            },
            "CTRL_GROSS": {
                "scale_factor": gross_factor,
                "achieved_total_gross": gross_control.total_gross_mean,
                **signed_report(
                    gross_control, context.states, context.participate, transitions=transitions
                ),
            },
        }
        _log(
            f"  {label}: net exp {net_target:.4f} -> scale {net_factor:.4f} "
            f"(got {net_control.net_exposure_mean:.4f}); "
            f"gross {gross_target:.4f} -> scale {gross_factor:.4f} "
            f"(got {gross_control.total_gross_mean:.4f})"
        )
    write_json(OUT / "controls.json", payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("netted", "costs", "controls"))
    arguments = parser.parse_args()
    started = time.perf_counter()
    {"netted": run_netted, "costs": run_costs, "controls": run_controls}[arguments.stage]()
    _log(f"stage {arguments.stage} complete in {time.perf_counter() - started:.0f}s")


if __name__ == "__main__":
    main()
