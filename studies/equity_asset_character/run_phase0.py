"""Phase-0 baseline reproduction (ledger §L1).

Reruns, from this worktree, the prior program's published baselines and
compares them against the stored authoritative artifacts:

- B1 sleeve variant (ledger §L1.2) vs `phase1/phase1_B1_enter2_exit1.json`;
- the U10 weighted bridge (§L1.3) vs `phase2/bridge_u10.json`;
- the U30 all-eligible base (§L1.4) vs `phase2/base_u30.json`.

(The EDA-1 sleeve reproduction, §L1.1, runs through the deep-arch runner
directly; its comparison also happens here.)

Everything is written under this program's own report root; prior artifacts
are read-only.

Usage:
    python -m studies.equity_asset_character.run_phase0 --stage b1
    python -m studies.equity_asset_character.run_phase0 --stage bridge
    python -m studies.equity_asset_character.run_phase0 --stage base-u30
    python -m studies.equity_asset_character.run_phase0 --stage compare
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from studies.equity_10_full import STUDY_SYMBOLS
from studies.equity_asset_character import REPORT_ROOT
from studies.equity_deep_arch.evaluate import evaluate_challenger, write_json
from studies.equity_deep_arch.run_eda1 import default_datasets, default_decisions
from studies.equity_eda1_nextgen import REPORT_ROOT as NEXTGEN_REPORTS
from studies.equity_eda1_nextgen.refined_states import RefinedSpec
from studies.equity_eda1_nextgen.run_phase1 import build_challenger
from studies.equity_eda1_nextgen.run_phase234 import UniverseContext, equal_weights, load_universe
from studies.equity_v1_v5.scoring import COST_MODELS

BASELINE = Path(REPORT_ROOT) / "baseline"

#: (this program's rerun, the prior program's authoritative artifact)
COMPARISONS: tuple[tuple[str, str], ...] = (
    ("eda1/full_evaluation.json", "baseline/full_evaluation.json"),
    ("phase1_B1_enter2_exit1.json", "phase1/phase1_B1_enter2_exit1.json"),
    ("bridge_u10.json", "phase2/bridge_u10.json"),
    ("base_u30.json", "phase2/base_u30.json"),
)


def _log(message: str) -> None:
    print(message, flush=True)


def run_b1() -> None:
    """The B1 sleeve variant, exactly as the prior program's Phase 1 ran it."""
    name, spec = "B1_enter2_exit1", RefinedSpec(k_enter=2, k_exit=1)
    datasets, decisions = default_datasets(), default_decisions()
    challenger, flips = build_challenger(datasets, decisions, STUDY_SYMBOLS, name, spec)
    result = evaluate_challenger(
        datasets, decisions, challenger, label=f"P1_{name}", symbols=STUDY_SYMBOLS
    )
    result["spec"] = spec.to_json_dict()
    result["session_state_flips"] = flips
    write_json(BASELINE / f"phase1_{name}.json", result)
    _log("b1: done")


def run_bridge() -> None:
    """T1 through the weighted machinery on U10, as the prior bridge."""
    from studies.equity_eda1_nextgen.universe import INCUMBENTS

    context = UniverseContext(list(INCUMBENTS))
    membership = {session: tuple(sorted(INCUMBENTS)) for session in context.sessions}
    weights = {
        session: equal_weights(tuple(sorted(INCUMBENTS)), 10) for session in context.sessions
    }
    payload = {
        "universe": context.universe,
        "EDA1_weighted_bridge": context.evaluate("EDA1_BRIDGE", weights, membership),
    }
    write_json(BASELINE / "bridge_u10.json", payload)
    _log("bridge: done")


def run_base_u30() -> None:
    """The U30 all-eligible base strategy and its equal-weight B&H control."""
    context = UniverseContext(load_universe("u30"))
    m = len(context.universe)
    all_members = {session: tuple(context.universe) for session in context.sessions}
    payload: dict[str, object] = {"universe": context.universe, "size": m}

    from studies.equity_eda1_nextgen.run_phase234 import (
        build_targets,
        replay_weighted,
        weighted_report,
    )

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
    blocks = {}
    for cost_model in COST_MODELS:
        result = replay_weighted(context.frames, bh_targets, cost_model, label="BH_EW")
        blocks[cost_model.label] = weighted_report(result, context.states)
    payload["BH_EW"] = blocks

    weights = {session: equal_weights(tuple(context.universe), m) for session in context.sessions}
    payload["ALL_ELIGIBLE"] = context.evaluate("ALL_ELIGIBLE", weights, all_members)
    write_json(BASELINE / "base_u30.json", payload)
    _log("base-u30: done")


def run_compare() -> None:
    """Sorted-JSON comparison of every rerun against its stored artifact."""
    report: dict[str, object] = {}
    all_pass = True
    for mine, theirs in COMPARISONS:
        mine_path = BASELINE / mine
        theirs_path = Path(NEXTGEN_REPORTS) / theirs
        entry: dict[str, object] = {"rerun": str(mine_path), "authoritative": str(theirs_path)}
        if not mine_path.exists() or not theirs_path.exists():
            entry["status"] = "MISSING"
            all_pass = False
        else:
            a = json.dumps(json.loads(mine_path.read_text()), sort_keys=True)
            b = json.dumps(json.loads(theirs_path.read_text()), sort_keys=True)
            entry["status"] = "IDENTICAL" if a == b else "DIFFERENT"
            if a != b:
                all_pass = False
        report[mine] = entry
    report["phase0_gate"] = "PASS" if all_pass else "FAIL"
    write_json(BASELINE / "comparison.json", report)
    _log(f"phase0 gate: {report['phase0_gate']}")
    for mine, entry in report.items():
        if isinstance(entry, dict):
            _log(f"  {mine}: {entry['status']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", required=True, choices=("b1", "bridge", "base-u30", "compare")
    )
    arguments = parser.parse_args()
    started = time.perf_counter()
    {"b1": run_b1, "bridge": run_bridge, "base-u30": run_base_u30, "compare": run_compare}[
        arguments.stage
    ]()
    _log(f"stage {arguments.stage} complete in {time.perf_counter() - started:.0f}s")


if __name__ == "__main__":
    main()
