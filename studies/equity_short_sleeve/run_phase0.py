"""Phase-0 runner: baseline reproduction gate (ledger §L3).

Reruns, from this worktree, the three authoritative baselines this program
leans on and compares them (sorted-JSON) against the stored artifacts at
ZERO tolerance. Also records the baseline's declared metric set — including
the regime-transition count, which no prior program reported and which the
short sleeve's activation clock depends on.

Usage:
    python -m studies.equity_short_sleeve.run_phase0 --stage eda1
    python -m studies.equity_short_sleeve.run_phase0 --stage bridge
    python -m studies.equity_short_sleeve.run_phase0 --stage base-u30
    python -m studies.equity_short_sleeve.run_phase0 --stage compare
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from studies.equity_deep_arch.evaluate import write_json
from studies.equity_short_sleeve import CHARACTER_REPORTS, NEXTGEN_REPORTS, REPORT_ROOT

BASELINE = Path(REPORT_ROOT) / "baseline"


def _log(message: str) -> None:
    print(message, flush=True)


def run_eda1() -> None:
    """EDA-1 sleeve full evaluation through the deep-arch runner."""
    from studies.equity_deep_arch.run_eda1 import (
        default_datasets,
        default_decisions,
        run_stage,
    )

    out = BASELINE / "eda1"
    out.mkdir(parents=True, exist_ok=True)
    run_stage("full", default_datasets(), default_decisions(), out)
    _log("eda1: done")


def run_bridge() -> None:
    """U10 weighted bridge — B0, the row every short candidate is judged
    against — plus the regime-transition census."""
    from studies.equity_eda1_nextgen.run_phase234 import UniverseContext, equal_weights
    from studies.equity_eda1_nextgen.universe import INCUMBENTS

    context = UniverseContext(list(INCUMBENTS))
    membership = {session: tuple(sorted(INCUMBENTS)) for session in context.sessions}
    weights = {
        session: equal_weights(tuple(sorted(INCUMBENTS)), 10) for session in context.sessions
    }
    payload: dict[str, object] = {
        "universe": context.universe,
        "EDA1_weighted_bridge": context.evaluate("EDA1_BRIDGE", weights, membership),
    }
    write_json(BASELINE / "bridge_u10.json", payload)

    # Regime census: the short sleeve's activation clock.
    ordered = sorted(context.sessions)
    states = [bool(context.participate[s]) for s in ordered]
    transitions = sum(1 for a, b in zip(states[:-1], states[1:], strict=True) if a != b)
    to_defensive = sum(1 for a, b in zip(states[:-1], states[1:], strict=True) if a and not b)
    runs: list[dict[str, object]] = []
    start = 0
    for index in range(1, len(states) + 1):
        if index == len(states) or states[index] != states[start]:
            runs.append(
                {
                    "state": "PARTICIPATE" if states[start] else "DEFENSIVE",
                    "start": str(ordered[start]),
                    "end": str(ordered[index - 1]),
                    "sessions": index - start,
                }
            )
            start = index
    defensive_runs = [r for r in runs if r["state"] == "DEFENSIVE"]
    write_json(
        BASELINE / "regime_census.json",
        {
            "sessions": len(ordered),
            "participate_sessions": sum(states),
            "defensive_sessions": len(states) - sum(states),
            "participate_share": sum(states) / len(states),
            "transitions": transitions,
            "participate_to_defensive": to_defensive,
            "defensive_to_participate": transitions - to_defensive,
            "defensive_runs": len(defensive_runs),
            "longest_defensive_run": max((int(r["sessions"]) for r in defensive_runs), default=0),
            "runs": runs,
        },
    )
    _log(f"bridge: done ({transitions} regime transitions, {len(defensive_runs)} defensive runs)")


def run_base_u30() -> None:
    """U30 all-eligible + BH_EW, inherited path (short-universe reference)."""
    from studies.equity_eda1_nextgen.run_phase234 import (
        UniverseContext,
        build_targets,
        equal_weights,
        load_universe,
        replay_weighted,
        weighted_report,
    )
    from studies.equity_v1_v5.scoring import COST_MODELS

    context = UniverseContext(load_universe("u30"))
    m = len(context.universe)
    all_members = {session: tuple(context.universe) for session in context.sessions}
    payload: dict[str, object] = {"universe": context.universe, "size": m}

    bh_weights = {
        session: equal_weights(tuple(context.universe), m) for session in context.sessions
    }
    always_on = dict.fromkeys(context.sessions, True)
    bh_targets = build_targets(
        context.frames,
        context.sessions,
        always_on,
        all_members,
        context.stance,
        active_weight_of=bh_weights,
        reserved_weight=context.reserved,
    )
    blocks: dict[str, object] = {}
    for cost_model in COST_MODELS:
        result = replay_weighted(context.frames, bh_targets, cost_model, label="BH_EW")
        blocks[cost_model.label] = weighted_report(result, context.states)
    payload["BH_EW"] = blocks

    weights = {session: equal_weights(tuple(context.universe), m) for session in context.sessions}
    all_targets = build_targets(
        context.frames,
        context.sessions,
        context.participate,
        all_members,
        context.stance,
        active_weight_of=weights,
        reserved_weight=context.reserved,
    )
    blocks = {}
    for cost_model in COST_MODELS:
        result = replay_weighted(context.frames, all_targets, cost_model, label="ALL_ELIGIBLE")
        blocks[cost_model.label] = weighted_report(result, context.states)
    payload["ALL_ELIGIBLE"] = blocks
    write_json(BASELINE / "base_u30.json", payload)
    _log("base-u30: done")


def run_compare() -> None:
    """Sorted-JSON comparison of every rerun against its stored artifact."""
    report: dict[str, object] = {}
    all_pass = True

    def compare(name: str, mine_path: Path, theirs_path: Path) -> None:
        nonlocal all_pass
        entry: dict[str, object] = {"rerun": str(mine_path), "authoritative": str(theirs_path)}
        if not mine_path.exists() or not theirs_path.exists():
            entry["status"] = "MISSING"
            all_pass = False
        else:
            a = json.loads(mine_path.read_text())
            b = json.loads(theirs_path.read_text())
            same = json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
            entry["status"] = "IDENTICAL" if same else "DIFFERENT"
            if not same:
                all_pass = False
        report[name] = entry

    compare(
        "eda1_full_evaluation",
        BASELINE / "eda1" / "full_evaluation.json",
        Path(CHARACTER_REPORTS) / "baseline" / "eda1" / "full_evaluation.json",
    )
    compare(
        "bridge_u10",
        BASELINE / "bridge_u10.json",
        Path(NEXTGEN_REPORTS) / "phase2" / "bridge_u10.json",
    )
    compare(
        "base_u30",
        BASELINE / "base_u30.json",
        Path(NEXTGEN_REPORTS) / "phase2" / "base_u30.json",
    )
    report["phase0_gate"] = "PASS" if all_pass else "FAIL"
    write_json(BASELINE / "comparison.json", report)
    _log(f"phase0 gate: {report['phase0_gate']}")
    for name, entry in report.items():
        if isinstance(entry, dict):
            _log(f"  {name}: {entry['status']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("eda1", "bridge", "base-u30", "compare"))
    arguments = parser.parse_args()
    started = time.perf_counter()
    {
        "eda1": run_eda1,
        "bridge": run_bridge,
        "base-u30": run_base_u30,
        "compare": run_compare,
    }[arguments.stage]()
    _log(f"stage {arguments.stage} complete in {time.perf_counter() - started:.0f}s")


if __name__ == "__main__":
    main()
