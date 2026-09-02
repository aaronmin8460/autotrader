"""Phase-6 runner: contingent U45 completion (ledger §L11).

Runs ONLY if the §L11 evaluation gate passed (recorded in the ledger flow
before invoking this module). Content is fixed: the U45 all-eligible
equal-weight control, the IDENTICAL frozen A1-B rule generalized to U45
(base becomes min(1/45, 0.10); the frozen fits and fingerprint panel already
cover all 45 names), and — only if the sleeve beats its control per §L11.3 —
ONE U45 blend at the best U30 ratio.

Usage:
    python -m studies.equity_two_sleeve.run_phase6 --stage base-u45
    python -m studies.equity_two_sleeve.run_phase6 --stage a1b-u45
    python -m studies.equity_two_sleeve.run_phase6 --stage blend-u45 --ratio B30
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from studies.equity_deep_arch.evaluate import write_json
from studies.equity_two_sleeve import REPORT_ROOT, TWO_SLEEVE_DATASETS
from studies.equity_two_sleeve.blend import (
    RATIOS,
    a_sleeve_targets,
    combine_targets,
    e_sleeve_targets,
    replay_blend,
)

OUT = Path(REPORT_ROOT) / "phase6"
CURVES = Path(TWO_SLEEVE_DATASETS) / "curves"


def _log(message: str) -> None:
    print(message, flush=True)


def _install_four_directory_stances() -> None:
    """Extend the stance search with THIS program's drive artifacts.

    Importing `run_phase5` installs the asset-character 3-directory loader;
    this adds the two-sleeve drive directory as the fourth and final stop.
    All prior directories stay read-only.
    """
    import pandas as pd

    import studies.equity_asset_character.run_phase5  # noqa: F401  (installs 3-dir loader)
    import studies.equity_eda1_nextgen.run_phase234 as rp
    from studies.equity_10_full.windows import FULL_WINDOWS
    from studies.equity_asset_character import CHARACTER_DATASETS
    from studies.equity_deep_arch.overlay import source_stance
    from studies.equity_v1_v5.scoring import frame_to_decisions

    prior_drive = Path(CHARACTER_DATASETS) / "v3-decisions"
    own_drive = Path(TWO_SLEEVE_DATASETS) / "v3-decisions"

    def load_stance4(symbol: str, frame) -> dict:
        records = []
        for window in FULL_WINDOWS:
            for directory in (rp.FROZEN_DECISIONS, rp.DRIVE_DECISIONS, prior_drive, own_drive):
                path = directory / f"{symbol}_{window.name}_V3.parquet"
                if path.exists():
                    records.extend(frame_to_decisions(pd.read_parquet(path)))
                    break
            else:
                raise SystemExit(f"No V3 series for {symbol}/{window.name}.")
        ordered = sorted(records, key=lambda record: record.timestamp)
        stances = source_stance(ordered)
        return {pd.Timestamp(r.timestamp): s for r, s in zip(ordered, stances, strict=True)}

    rp.load_stance = load_stance4


_install_four_directory_stances()


def run_base_u45() -> None:
    """U45 all-eligible EW control + BH_EW through the weighted machinery."""
    from studies.equity_eda1_nextgen.run_phase234 import (
        UniverseContext,
        build_targets,
        equal_weights,
        load_universe,
        replay_weighted,
        weighted_report,
    )
    from studies.equity_two_sleeve.blend import save_curve
    from studies.equity_v1_v5.scoring import COST_MODELS

    context = UniverseContext(load_universe("u50"))
    m = len(context.universe)
    all_members = {session: tuple(context.universe) for session in context.sessions}
    payload: dict[str, object] = {"universe": context.universe, "size": m}

    for row_label, participate in (
        ("BH_EW", dict.fromkeys(context.sessions, True)),
        ("ALL_ELIGIBLE", context.participate),
    ):
        weights = {
            session: equal_weights(tuple(context.universe), m) for session in context.sessions
        }
        targets = build_targets(
            context.frames,
            context.sessions,
            participate,
            all_members,
            context.stance,
            active_weight_of=weights,
            reserved_weight=context.reserved,
        )
        blocks = {}
        for cost_model in COST_MODELS:
            result = replay_weighted(context.frames, targets, cost_model, label=row_label)
            blocks[cost_model.label] = weighted_report(result, context.states)
            if cost_model.label == "equity-marketable" and row_label == "ALL_ELIGIBLE":
                save_curve(result, CURVES / "U45_ALL_ELIGIBLE_equity-marketable.parquet")
        payload[row_label] = blocks
        _log(f"{row_label}: done")
    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "base_u45.json", payload)


def run_a1b_u45() -> None:
    """The frozen A1-B rule on U45 — zero modifications beyond the base
    weight's own min(1/M, cap) arithmetic."""
    from studies.equity_asset_character.run_phase5 import TiltContext

    context = TiltContext("u50")
    payload = {
        "universe": context.context.universe,
        "A1_B": context.evaluate("A1_B_U45", "A1_B"),
    }
    targets = a_sleeve_targets(context)
    mine = replay_blend(
        context.context.frames, targets, "A1_B_U45", context.context.states, curve_dir=CURVES
    )
    if (
        mine["equity-marketable"]["net_return"]
        != payload["A1_B"]["equity-marketable"]["net_return"]
    ):
        raise SystemExit("a1b-u45: sleeve-builder path diverged from the inherited path.")
    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "a1b_u45.json", payload)
    _log("a1b-u45: done (builder fidelity verified)")


def run_blend_u45(ratio: str) -> None:
    """ONE U45 blend at the best U30 ratio (§L11.4). No ratio grid."""
    from studies.equity_asset_character.run_phase5 import TiltContext

    s_e, s_a = RATIOS[ratio]
    context = TiltContext("u50")
    frames = context.context.frames
    targets_e = e_sleeve_targets(
        frames, context.context.sessions, context.context.participate, context.context.stance
    )
    targets_a = a_sleeve_targets(context)
    combined = combine_targets([(s_e, targets_e), (s_a, targets_a)])
    label = f"{ratio}_U45"
    block = replay_blend(frames, combined, label, context.context.states, curve_dir=CURVES)
    block["sleeve_budgets"] = {"E": s_e, "A_u45": s_a, "cash_floor": 1.0 - s_e - s_a}
    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / f"blend_u45_{ratio}.json", {"ratio": ratio, label: block})
    _log(f"blend-u45 {ratio}: done")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("base-u45", "a1b-u45", "blend-u45"))
    parser.add_argument("--ratio", default="B30")
    arguments = parser.parse_args()
    started = time.perf_counter()
    if arguments.stage == "base-u45":
        run_base_u45()
    elif arguments.stage == "a1b-u45":
        run_a1b_u45()
    else:
        run_blend_u45(arguments.ratio)
    _log(f"stage {arguments.stage} complete in {time.perf_counter() - started:.0f}s")


if __name__ == "__main__":
    main()
