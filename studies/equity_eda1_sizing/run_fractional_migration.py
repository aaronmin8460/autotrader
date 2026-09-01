"""OLD versus NEW sizing policy over the frozen EDA-1 stance series. One run.

The migration study for `EDA1_FRACTIONAL_RESERVED_90`: the same stored EDA-1
decision series that froze `C_RESERVED_UNIVERSE`, replayed under both capital
policies so the only thing that differs is the policy. This is NOT an alpha
search - the strategy is untouched - and the acceptance gates were predeclared
in `policy.md` before this module first ran.

Usage:
    python -m studies.equity_eda1_sizing.run_fractional_migration
    python -m studies.equity_eda1_sizing.run_fractional_migration --deadband fallback
"""

from __future__ import annotations

import argparse
import time
from decimal import Decimal
from pathlib import Path

import pandas as pd
from studies.equity_eda1_sizing import STUDY_SYMBOLS
from studies.equity_eda1_sizing.evidence import (
    default_datasets,
    default_decisions,
    stance_frame,
    verify_wiring,
)
from studies.equity_eda1_sizing.run_sizing import (
    COST_MODELS,
    EXTERNAL_SCENARIOS,
    PRIMARY_COST,
    describe,
    price_frames,
    regime_table,
    spy_drawdown_states,
    window_bounds,
    window_returns,
    write_json,
)
from studies.equity_eda1_sizing.simulate import RebalanceRule, SimulationResult, simulate

from autotrader.equity.allocation import (
    POLICY_FRACTIONAL_RESERVED_90,
    POLICY_RESERVED_UNIVERSE,
    AllocationPolicy,
    allocation_policy_for,
)
from autotrader.risk.engine import MAX_DAILY_LOSS_FRACTION

DEFAULT_OUTPUT = Path("/Volumes/AUTOTRADER_QA/reports/equity-paper-fractional-90")

#: The predeclared fallback deadband width (policy.md §4): the relative floor
#: widens once to 5% of slot; nothing else may move.
FALLBACK_SLOT_FRACTION = Decimal("0.05")

SMALL_ACCOUNT_CASH = Decimal("150")


def fallback_policy() -> AllocationPolicy:
    base = allocation_policy_for(POLICY_FRACTIONAL_RESERVED_90)
    return AllocationPolicy(
        policy_id=base.policy_id,
        per_symbol_cap=base.per_symbol_cap,
        total_cap=base.total_cap,
        target_gross=base.target_gross,
        universe_size=base.universe_size,
        fractional=True,
        deadband_min_notional=base.deadband_min_notional,
        deadband_slot_fraction=FALLBACK_SLOT_FRACTION,
    )


def daily_halt_incidence(result: SimulationResult) -> dict[str, object]:
    """UTC days whose intraday trough breaches the 2% halt against the day's
    first mark. The simulator does not enforce the halt (matching the study
    that froze policy C); this reports how often it would have engaged.
    """
    curve = pd.Series(
        [float(value) for value in result.equity_curve],
        index=pd.DatetimeIndex(result.timestamps),
    )
    hits = 0
    worst = 0.0
    days = 0
    for _, values in curve.groupby(curve.index.date):
        days += 1
        first = float(values.iloc[0])
        if first <= 0:
            continue
        trough = float(values.min()) / first - 1.0
        worst = min(worst, trough)
        if trough <= -MAX_DAILY_LOSS_FRACTION:
            hits += 1
    return {
        "days": days,
        "halt_days": hits,
        "halt_day_fraction": hits / days if days else 0.0,
        "worst_intraday_drawdown_vs_day_open": worst,
    }


def run_matrix(
    *,
    label: str,
    policy: AllocationPolicy,
    rule: RebalanceRule,
    stances: pd.DataFrame,
    frames: dict[str, pd.DataFrame],
    bounds,
    states,
    initial_cash: Decimal | None = None,
    externals: tuple[Decimal, ...] = EXTERNAL_SCENARIOS,
    costs=COST_MODELS,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "config": policy.to_json_dict(),
        "config_hash": policy.config_hash(),
        "rule": rule.value,
        "scenarios": {},
    }
    for external in externals:
        per_cost: dict[str, object] = {}
        for cost_label, cost_model in costs:
            kwargs = {} if initial_cash is None else {"initial_cash": initial_cash}
            result = simulate(
                label=f"{label}/X={external}/{cost_label}",
                stances=stances,
                frames=frames,
                policy=policy,
                cost_model=cost_model,
                cost_label=cost_label,
                external_exposure_fraction=external,
                rule=rule,
                **kwargs,
            )
            row = describe(result, cost_model)
            row["daily_halt_incidence"] = daily_halt_incidence(result)
            if cost_label == PRIMARY_COST:
                row["window_returns"] = window_returns(result, bounds)
                row["regime_table"] = regime_table(result, states)
                row["final_positions_positive"] = sum(
                    1 for quantity in result.final_positions.values() if quantity > 0
                )
            per_cost[cost_label] = row
        entry["scenarios"][str(external)] = per_cost  # type: ignore[index]
    return entry


def grade(old: dict, new: dict, small: dict) -> dict[str, object]:
    """The predeclared gates from policy.md §6. Nothing here may be reweighed."""
    gates: dict[str, object] = {}
    primary = PRIMARY_COST

    nets = {
        external: new["scenarios"][external][primary]["net_return"] for external in new["scenarios"]
    }
    gates["1_new_net_positive_every_X"] = {
        "values": nets,
        "pass": all(value > 0 for value in nets.values()),
    }

    old_turn = old["scenarios"]["0.00"][primary]["turnover"]
    new_turn = new["scenarios"]["0.00"][primary]["turnover"]
    gates["2_turnover_within_3x_old"] = {
        "old": old_turn,
        "new": new_turn,
        "ratio": new_turn / old_turn if old_turn else float("inf"),
        "pass": new_turn <= 3 * old_turn,
    }

    old_drag = old["scenarios"]["0.00"][primary]["cost_drag"]
    new_drag = new["scenarios"]["0.00"][primary]["cost_drag"]
    gates["3_cost_drag_within_3x_old"] = {
        "old": old_drag,
        "new": new_drag,
        "pass": new_drag <= 3 * old_drag,
    }

    caps_ok = True
    caps: dict[str, object] = {}
    for external, per_cost in new["scenarios"].items():
        row = per_cost[primary]
        symbol_ok = Decimal(row["max_symbol_weight"]) <= Decimal("0.11")
        total_ok = Decimal(row["max_total_weight"]) <= Decimal("0.90")
        caps[external] = {
            "max_symbol_weight": row["max_symbol_weight"],
            "max_total_weight": row["max_total_weight"],
            "max_realized_symbol": row["max_realized_symbol_fraction"],
            "max_realized_total": row["max_realized_total_fraction"],
        }
        caps_ok = caps_ok and symbol_ok and total_ok
    gates["4_assigned_caps_hold"] = {"detail": caps, "pass": caps_ok}

    asymmetry = sum(
        per_cost[primary]["weight_asymmetry_bars"] for per_cost in new["scenarios"].values()
    )
    gates["5_zero_asymmetry_bars"] = {"total": asymmetry, "pass": asymmetry == 0}

    small_row = small["scenarios"]["0.00"][primary]
    gates["6_small_account_viable"] = {
        "final_positions_positive": small_row["final_positions_positive"],
        "fill_count": small_row["fill_count"],
        "max_total_weight": small_row["max_total_weight"],
        "pass": (
            small_row["final_positions_positive"] == len(STUDY_SYMBOLS)
            and Decimal(small_row["max_total_weight"]) <= Decimal("0.90")
        ),
    }

    gates["all_pass"] = all(gate["pass"] for name, gate in gates.items() if isinstance(gate, dict))
    return gates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets", type=Path, default=default_datasets())
    parser.add_argument("--decisions", type=Path, default=default_decisions())
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--deadband",
        choices=("primary", "fallback"),
        default="primary",
        help="fallback widens the relative floor once, to the predeclared 5%.",
    )
    args = parser.parse_args()

    started = time.perf_counter()
    eda1, _v3, summary = stance_frame(args.datasets, args.decisions)
    verify_wiring(summary)

    index = pd.DatetimeIndex(eda1.index)
    frames = price_frames(args.datasets, index)
    bounds = window_bounds(args.decisions)
    states = spy_drawdown_states(args.datasets, index)

    old_policy = allocation_policy_for(POLICY_RESERVED_UNIVERSE)
    new_policy = (
        allocation_policy_for(POLICY_FRACTIONAL_RESERVED_90)
        if args.deadband == "primary"
        else fallback_policy()
    )

    old = run_matrix(
        label="OLD/C_RESERVED_UNIVERSE",
        policy=old_policy,
        rule=RebalanceRule.WHOLE_SHARE,
        stances=eda1,
        frames=frames,
        bounds=bounds,
        states=states,
    )
    new = run_matrix(
        label="NEW/EDA1_FRACTIONAL_RESERVED_90",
        policy=new_policy,
        rule=RebalanceRule.FRACTIONAL_DEADBAND,
        stances=eda1,
        frames=frames,
        bounds=bounds,
        states=states,
    )
    small = run_matrix(
        label="NEW/$150",
        policy=new_policy,
        rule=RebalanceRule.FRACTIONAL_DEADBAND,
        stances=eda1,
        frames=frames,
        bounds=bounds,
        states=states,
        initial_cash=SMALL_ACCOUNT_CASH,
        externals=(Decimal("0.00"), Decimal("0.05")),
        costs=tuple(pair for pair in COST_MODELS if pair[0] == PRIMARY_COST),
    )
    # The contrast the migration exists to fix: whole shares on $150 buy nothing.
    small_old = run_matrix(
        label="OLD/$150",
        policy=old_policy,
        rule=RebalanceRule.WHOLE_SHARE,
        stances=eda1,
        frames=frames,
        bounds=bounds,
        states=states,
        initial_cash=SMALL_ACCOUNT_CASH,
        externals=(Decimal("0.00"),),
        costs=tuple(pair for pair in COST_MODELS if pair[0] == PRIMARY_COST),
    )

    payload = {
        "participation": summary,
        "wiring_check": "PASS",
        "deadband_variant": args.deadband,
        "old": old,
        "new": new,
        "new_small_account": small,
        "old_small_account": small_old,
        "gates": grade(old, new, small),
        "elapsed_seconds": round(time.perf_counter() - started, 1),
    }
    name = (
        "fractional-migration.json"
        if args.deadband == "primary"
        else "fractional-migration-fallback.json"
    )
    write_json(args.output / name, payload)
    print(f"{args.deadband}: {payload['elapsed_seconds']}s -> {args.output / name}")
    print(f"gates all_pass = {payload['gates']['all_pass']}")


if __name__ == "__main__":
    main()
