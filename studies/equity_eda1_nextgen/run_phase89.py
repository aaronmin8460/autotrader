"""Phases 8–9 runner: the authorized combination and the tournament robustness
reruns (ledger §L9–L11 and dated amendments).

COMBO-A = Phase-1 winner regime rule (B1: k_enter 2, k_exit 1) + the Phase-3
winning selection + the Phase-4 winning allocator, on the winning universe.
COMBO-B exists only if a Phase-7 execution variant earned inclusion (none
did). `--stage combo` runs COMBO-A; `--stage attack` runs the predeclared
robustness reruns for a named weighted entrant (drop-strongest-symbol rerun,
regime perturbations, rebalance-cadence perturbations).

Usage:
    python -m studies.equity_eda1_nextgen.run_phase89 --stage combo \\
        --universe u30 --rule <rule> --allocator <AL_A|AL_B|AL_C>
    python -m studies.equity_eda1_nextgen.run_phase89 --stage attack \\
        --universe u30 --rule <rule> --allocator <A> --regime <incumbent|b1> \\
        --drop-symbol <SYM>
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from studies.equity_deep_arch.evaluate import write_json
from studies.equity_deep_arch.state import session_closes
from studies.equity_eda1_nextgen import REPORT_ROOT
from studies.equity_eda1_nextgen.refined_states import (
    RefinedSpec,
    refined_participation_series,
)
from studies.equity_eda1_nextgen.run_phase234 import (
    UniverseContext,
    equal_weights,
    inverse_vol_weights,
    load_frame,
    load_universe,
    selection_rules,
)

B1_SPEC = RefinedSpec(k_enter=2, k_exit=1)


def b1_participation_map() -> dict:
    spy = load_frame("SPY")
    series = refined_participation_series(session_closes(spy), B1_SPEC)
    return {row["session"]: bool(row["participate"]) for _, row in series.iterrows()}


def weights_for(context: UniverseContext, membership, allocator: str, top_n: int):
    if allocator == "AL_A":
        return {
            session: (
                dict.fromkeys(
                    membership[session],
                    min(1.0 / len(membership[session]), 0.10),
                )
                if membership[session]
                else {}
            )
            for session in context.sessions
        }
    if allocator == "AL_B":
        return {session: equal_weights(membership[session], top_n) for session in context.sessions}
    if allocator == "AL_C":
        return inverse_vol_weights(context, membership, top_n)
    raise SystemExit(f"Unknown allocator {allocator}.")


def build_context(
    universe_name: str,
    *,
    regime: str,
    drop_symbol: str | None = None,
    rebalance_every: int | None = None,
) -> UniverseContext:
    universe = load_universe(universe_name)
    if drop_symbol:
        if drop_symbol not in universe:
            raise SystemExit(f"{drop_symbol} not in {universe_name}.")
        universe = [s for s in universe if s != drop_symbol]
    if rebalance_every is not None:
        import studies.equity_eda1_nextgen.selection as selection_module

        selection_module.REBALANCE_EVERY_SESSIONS = rebalance_every
    context = UniverseContext(universe)
    if regime == "b1":
        context.participate = b1_participation_map()
    elif regime != "incumbent":
        raise SystemExit(f"Unknown regime {regime}.")
    return context


def run_combo(universe_name: str, rule: str, allocator: str) -> None:
    context = build_context(universe_name, regime="b1")
    membership = selection_rules(context)[rule]
    top_n = int(rule.rsplit("top", 1)[1])
    weights = weights_for(context, membership, allocator, top_n)
    payload = {
        "universe": context.universe,
        "regime": "B1 (k_enter=2, k_exit=1)",
        "rule": rule,
        "allocator": allocator,
        "COMBO_A": context.evaluate("COMBO_A", weights, membership),
    }
    write_json(
        Path(REPORT_ROOT) / "phase8" / f"combo_a_{universe_name}_{rule}_{allocator}.json", payload
    )
    print("combo done", flush=True)


def run_attack(
    universe_name: str,
    rule: str,
    allocator: str,
    regime: str,
    drop_symbol: str | None,
    rebalance_every: int | None,
    regime_variant: str | None,
) -> None:
    context = build_context(
        universe_name,
        regime=regime,
        drop_symbol=drop_symbol,
        rebalance_every=rebalance_every,
    )
    if regime_variant:
        spy = load_frame("SPY")
        base = B1_SPEC if regime == "b1" else RefinedSpec()
        variants = {
            "sma150": RefinedSpec(**{**base.to_json_dict(), "sma_sessions": 150}),
            "sma250": RefinedSpec(**{**base.to_json_dict(), "sma_sessions": 250}),
            "band_tighter": RefinedSpec(
                **{**base.to_json_dict(), "enter_dd": -0.04, "exit_dd": -0.04}
            ),
            "band_looser": RefinedSpec(
                **{**base.to_json_dict(), "enter_dd": -0.06, "exit_dd": -0.06}
            ),
            "lag2": RefinedSpec(**{**base.to_json_dict(), "lag_sessions": 2}),
        }
        series = refined_participation_series(session_closes(spy), variants[regime_variant])
        context.participate = {
            row["session"]: bool(row["participate"]) for _, row in series.iterrows()
        }
    membership = selection_rules(context)[rule]
    top_n = int(rule.rsplit("top", 1)[1])
    weights = weights_for(context, membership, allocator, top_n)
    tag = "_".join(
        filter(
            None,
            (
                universe_name,
                rule,
                allocator,
                regime,
                f"drop{drop_symbol}" if drop_symbol else None,
                f"reb{rebalance_every}" if rebalance_every else None,
                regime_variant,
            ),
        )
    )
    payload = {
        "universe": context.universe,
        "attack": tag,
        "result": context.evaluate(f"ATTACK_{tag}", weights, membership),
    }
    write_json(Path(REPORT_ROOT) / "phase9" / f"attack_{tag}.json", payload)
    print(f"attack {tag} done", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("combo", "attack"))
    parser.add_argument("--universe", default="u30")
    parser.add_argument("--rule", required=True)
    parser.add_argument("--allocator", required=True)
    parser.add_argument("--regime", default="b1")
    parser.add_argument("--drop-symbol", default=None)
    parser.add_argument("--rebalance-every", type=int, default=None)
    parser.add_argument("--regime-variant", default=None)
    arguments = parser.parse_args()

    started = time.perf_counter()
    if arguments.stage == "combo":
        run_combo(arguments.universe, arguments.rule, arguments.allocator)
    else:
        run_attack(
            arguments.universe,
            arguments.rule,
            arguments.allocator,
            arguments.regime,
            arguments.drop_symbol,
            arguments.rebalance_every,
            arguments.regime_variant,
        )
    print(f"stage complete in {time.perf_counter() - started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
