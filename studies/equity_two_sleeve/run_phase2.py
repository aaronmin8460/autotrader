"""Phase-2 runner: the predeclared blend candidates and controls (§L3–§L4).

Rows (all through the inherited weighted machinery, three costs, curves
saved at primary cost):

- B20 / B30 / B40 — sleeve E (EDA-1 U10) + sleeve A (A1-B U30) at the
  frozen ratios, 0.10 cash floor, combined cap 0.10;
- CTRL_SE_90 — sleeve E × 0.90 (same-total-budget / same-exposure control);
- CTRL_GEN_20/30/40 — sleeve A replaced by the generic U30 equal-weight
  all-eligible sleeve at the same ratios.

Usage:
    python -m studies.equity_two_sleeve.run_phase2
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from studies.equity_deep_arch.evaluate import write_json
from studies.equity_two_sleeve import REPORT_ROOT, TWO_SLEEVE_DATASETS
from studies.equity_two_sleeve.blend import (
    RATIOS,
    Targets,
    a_sleeve_targets,
    combine_targets,
    e_sleeve_targets,
    g_sleeve_targets,
    replay_blend,
    scale_targets,
)

OUT = Path(REPORT_ROOT) / "phase2"
CURVES = Path(TWO_SLEEVE_DATASETS) / "curves"


def _log(message: str) -> None:
    print(message, flush=True)


def mean_bar_total(targets: Targets) -> float:
    """Mean over bars of the summed target weight — gross sleeve allocation."""
    totals: dict[pd.Timestamp, float] = {}
    for series in targets.values():
        for stamp, weight in series.items():
            totals[stamp] = totals.get(stamp, 0.0) + weight
    return sum(totals.values()) / len(totals) if totals else 0.0


def main() -> None:
    from studies.equity_asset_character.run_phase5 import TiltContext

    started = time.perf_counter()
    context = TiltContext("u30")
    frames = context.context.frames
    sessions = context.context.sessions
    participate = context.context.participate
    stance = context.context.stance
    states = context.context.states

    targets_e = e_sleeve_targets(frames, sessions, participate, stance)
    targets_a = a_sleeve_targets(context)
    targets_g = g_sleeve_targets(frames, context.context.universe, sessions, participate, stance)
    _log("sleeve targets built")

    payload: dict[str, object] = {
        "universe": context.context.universe,
        "ratios": {k: list(v) for k, v in RATIOS.items()},
        "sleeve_gross_allocation_mean": {
            "E_full": mean_bar_total(targets_e),
            "A_full": mean_bar_total(targets_a),
            "G_full": mean_bar_total(targets_g),
        },
    }

    for label, (s_e, s_a) in RATIOS.items():
        combined = combine_targets([(s_e, targets_e), (s_a, targets_a)])
        block = replay_blend(frames, combined, label, states, curve_dir=CURVES)
        block["sleeve_budgets"] = {"E": s_e, "A": s_a, "cash_floor": 1.0 - s_e - s_a}
        block["sleeve_gross_allocation_mean"] = {
            "E": mean_bar_total(scale_targets(targets_e, s_e)),
            "A": mean_bar_total(scale_targets(targets_a, s_a)),
            "combined": mean_bar_total(combined),
        }
        payload[label] = block
        _log(f"{label}: done ({time.perf_counter() - started:.0f}s elapsed)")

    se = scale_targets(targets_e, 0.90)
    payload["CTRL_SE_90"] = replay_blend(frames, se, "CTRL_SE_90", states, curve_dir=CURVES)
    _log("CTRL_SE_90: done")

    for label, (s_e, s_a) in RATIOS.items():
        gen_label = f"CTRL_GEN_{label[1:]}"
        combined = combine_targets([(s_e, targets_e), (s_a, targets_g)])
        block = replay_blend(frames, combined, gen_label, states, curve_dir=CURVES)
        block["sleeve_budgets"] = {"E": s_e, "G": s_a, "cash_floor": 1.0 - s_e - s_a}
        payload[gen_label] = block
        _log(f"{gen_label}: done ({time.perf_counter() - started:.0f}s elapsed)")

    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "blends.json", payload)
    _log(f"phase2 complete in {time.perf_counter() - started:.0f}s")


if __name__ == "__main__":
    main()
