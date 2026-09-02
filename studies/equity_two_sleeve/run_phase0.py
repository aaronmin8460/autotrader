"""Phase-0 runner: baseline reproduction gate (ledger §L1).

Reruns, from this worktree, the four authoritative baselines and compares
them (sorted-JSON) against the stored artifacts. Additionally persists the
primary-cost equity curves of the two sleeves (and the generic U30 control)
for Phase 1, and asserts that this program's sleeve target builders
reproduce the inherited evaluation paths bit-for-bit (final equity match).

Usage:
    python -m studies.equity_two_sleeve.run_phase0 --stage eda1
    python -m studies.equity_two_sleeve.run_phase0 --stage bridge
    python -m studies.equity_two_sleeve.run_phase0 --stage base-u30
    python -m studies.equity_two_sleeve.run_phase0 --stage a1b
    python -m studies.equity_two_sleeve.run_phase0 --stage compare
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from studies.equity_deep_arch.evaluate import write_json
from studies.equity_two_sleeve import REPORT_ROOT, TWO_SLEEVE_DATASETS
from studies.equity_two_sleeve.blend import (
    a_sleeve_targets,
    e_sleeve_targets,
    replay_blend,
    save_curve,
)

BASELINE = Path(REPORT_ROOT) / "baseline"
CURVES = Path(TWO_SLEEVE_DATASETS) / "curves"

CHARACTER_REPORTS = Path("/Volumes/AUTOTRADER_QA/reports/equity-eda1-asset-character")
NEXTGEN_REPORTS = Path("/Volumes/AUTOTRADER_QA/reports/equity-eda1-next-generation")


def _log(message: str) -> None:
    print(message, flush=True)


def _assert_final_equity(blocks: dict, mine: dict, label: str) -> None:
    theirs = blocks["equity-marketable"]["net_return"]
    ours = mine["equity-marketable"]["net_return"]
    if theirs != ours:
        raise SystemExit(
            f"{label}: sleeve-builder replay net {ours} != inherited evaluate net {theirs}."
        )
    _log(f"{label}: sleeve-builder path reproduces the inherited path exactly.")


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
    """U10 weighted bridge — inherited path for the byte comparison, and this
    program's sleeve-E builder for the fidelity assertion + curve."""
    from studies.equity_eda1_nextgen.run_phase234 import UniverseContext, equal_weights
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

    targets = e_sleeve_targets(
        context.frames, context.sessions, context.participate, context.stance
    )
    mine = replay_blend(context.frames, targets, "EDA1_BRIDGE", context.states, curve_dir=CURVES)
    _assert_final_equity(payload["EDA1_weighted_bridge"], mine, "bridge")
    _log("bridge: done")


def run_base_u30() -> None:
    """U30 all-eligible + BH_EW, inherited path; ALL_ELIGIBLE curve saved."""
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
    blocks = {}
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
        if cost_model.label == "equity-marketable":
            save_curve(result, CURVES / "ALL_ELIGIBLE_equity-marketable.parquet")
    payload["ALL_ELIGIBLE"] = blocks
    write_json(BASELINE / "base_u30.json", payload)
    _log("base-u30: done")


def run_a1b() -> None:
    """A1-B U30 — inherited TiltContext path for the byte comparison, and
    this program's sleeve-A builder for the fidelity assertion + curve."""
    from studies.equity_asset_character.run_phase5 import TiltContext

    context = TiltContext("u30")
    payload = {
        "universe": context.context.universe,
        "A1_B": context.evaluate("A1_B", "A1_B"),
    }
    write_json(BASELINE / "a1_u30_a1b.json", payload)

    targets = a_sleeve_targets(context)
    mine = replay_blend(
        context.context.frames, targets, "A1_B", context.context.states, curve_dir=CURVES
    )
    _assert_final_equity(payload["A1_B"], mine, "a1b")
    _log("a1b: done")


def run_compare() -> None:
    """Sorted-JSON comparison of every rerun against its stored artifact."""
    report: dict[str, object] = {}
    all_pass = True

    def compare(name: str, mine_path: Path, theirs_path: Path, *, keys=None) -> None:
        nonlocal all_pass
        entry: dict[str, object] = {"rerun": str(mine_path), "authoritative": str(theirs_path)}
        if not mine_path.exists() or not theirs_path.exists():
            entry["status"] = "MISSING"
            all_pass = False
        else:
            a = json.loads(mine_path.read_text())
            b = json.loads(theirs_path.read_text())
            if keys is not None:
                a = {k: a[k] for k in keys}
                b = {k: b[k] for k in keys}
            same = json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
            entry["status"] = "IDENTICAL" if same else "DIFFERENT"
            if keys is not None:
                entry["compared_keys"] = list(keys)
            if not same:
                all_pass = False
        report[name] = entry

    compare(
        "eda1_full_evaluation",
        BASELINE / "eda1" / "full_evaluation.json",
        CHARACTER_REPORTS / "baseline" / "eda1" / "full_evaluation.json",
    )
    compare(
        "bridge_u10",
        BASELINE / "bridge_u10.json",
        NEXTGEN_REPORTS / "phase2" / "bridge_u10.json",
    )
    compare(
        "base_u30",
        BASELINE / "base_u30.json",
        NEXTGEN_REPORTS / "phase2" / "base_u30.json",
    )
    compare(
        "a1_u30_A1_B",
        BASELINE / "a1_u30_a1b.json",
        CHARACTER_REPORTS / "phase5" / "a1_u30.json",
        keys=("universe", "A1_B"),
    )
    report["phase0_gate"] = "PASS" if all_pass else "FAIL"
    write_json(BASELINE / "comparison.json", report)
    _log(f"phase0 gate: {report['phase0_gate']}")
    for name, entry in report.items():
        if isinstance(entry, dict):
            _log(f"  {name}: {entry['status']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", required=True, choices=("eda1", "bridge", "base-u30", "a1b", "compare")
    )
    arguments = parser.parse_args()
    started = time.perf_counter()
    {
        "eda1": run_eda1,
        "bridge": run_bridge,
        "base-u30": run_base_u30,
        "a1b": run_a1b,
        "compare": run_compare,
    }[arguments.stage]()
    _log(f"stage {arguments.stage} complete in {time.perf_counter() - started:.0f}s")


if __name__ == "__main__":
    main()
