"""Reference-side parity: the shipped A1-B policy math vs the research
pipeline on the same frozen data (ledger §L15's reference half).

Three comparisons, every mark of the research region:

1. fingerprints — `autotrader.equity.a1b_policy.structural_at` recomputed
   from the frozen frames vs the research fingerprint panel;
2. z + labels — the shipped single-mark z-scoring + nearest-centroid
   assignment vs the research `cross_sectional_z` + `retro_labels`;
3. weights — the shipped `mark_weights` vs the research
   `TiltContext.mark_weights` for scheme A1_B.

Usage:
    python -m studies.equity_two_sleeve.run_parity
"""

from __future__ import annotations

import math
import time
from pathlib import Path

from studies.equity_deep_arch.evaluate import write_json
from studies.equity_two_sleeve import REPORT_ROOT

OUT = Path(REPORT_ROOT) / "parity"

TOLERANCE = 1e-9


def _log(message: str) -> None:
    print(message, flush=True)


def main() -> None:
    started = time.perf_counter()

    from autotrader.equity.a1b_policy import (
        assign_labels,
        build_series,
        cross_sectional_z_at_mark,
        governing_fit,
        load_policy,
        mark_weights,
        structural_at,
        symbol_sessions,
    )
    from studies.equity_asset_character.allocation import retro_labels
    from studies.equity_asset_character.fingerprints import cross_sectional_z
    from studies.equity_asset_character.run_phase2 import load_panel
    from studies.equity_asset_character.run_phase5 import TiltContext, surviving_features
    from studies.equity_eda1_nextgen.run_phase234 import load_frame

    policy = load_policy()
    features = tuple(surviving_features())
    assert features == policy.surviving_features, "feature lists diverge"

    panel = load_panel()
    z_panel = cross_sectional_z(panel, features)
    context = TiltContext("u30", z_structural=z_panel)
    marks = list(context.marks)
    fit_records = context.fit_records

    report: dict[str, object] = {
        "policy_hash": policy.policy_hash,
        "marks_total": len(marks),
        "tolerance": TOLERANCE,
    }

    # ------------------------------------------------------------------
    # 1. Fingerprint parity on the three most recent marks (frame rebuild
    #    is the expensive step; the math is mark-invariant).
    # ------------------------------------------------------------------
    fp_marks = marks[-3:]
    tables = {s: symbol_sessions(load_frame(s)) for s in policy.u45_z_cross_section}
    reference_table = tables["SPY"]
    fp_mismatches = 0
    fp_cells = 0
    values_by_mark: dict = {}
    for mark in marks:
        values_by_mark[mark] = {}
    for symbol in policy.u45_z_cross_section:
        series = build_series(tables[symbol], reference_table)
        for mark in marks:
            values_by_mark[mark][symbol] = structural_at(series, mark)
    for mark in fp_marks:
        for symbol in policy.u45_z_cross_section:
            mine = values_by_mark[mark][symbol]
            theirs = panel.loc[(mark, symbol)]
            for feature in features:
                fp_cells += 1
                a, b = mine[feature], float(theirs[feature])
                if math.isnan(a) and math.isnan(b):
                    continue
                if math.isnan(a) != math.isnan(b) or abs(a - b) > TOLERANCE:
                    fp_mismatches += 1
                    _log(f"FP MISMATCH {mark} {symbol} {feature}: {a} vs {b}")
    report["fingerprints"] = {
        "marks": [m.isoformat() for m in fp_marks],
        "cells": fp_cells,
        "mismatches": fp_mismatches,
    }
    _log(f"fingerprints: {fp_cells} cells, {fp_mismatches} mismatches")

    # ------------------------------------------------------------------
    # 2 + 3. Z/label/weight parity at every mark with a governing fit.
    # ------------------------------------------------------------------
    label_mismatches = 0
    weight_mismatches = 0
    marks_with_fit = 0
    max_weight_delta = 0.0
    for mark in marks:
        fit = governing_fit(policy, mark)
        from studies.equity_asset_character.allocation import governing_fit as research_gf

        research_fit = research_gf(fit_records, mark)
        if (fit is None) != (research_fit is None):
            raise SystemExit(f"governing-fit disagreement at {mark}")
        if fit is None:
            continue
        if fit.fit_mark.isoformat() != research_fit["fit_mark"]:
            raise SystemExit(f"governing-fit date disagreement at {mark}")
        marks_with_fit += 1

        z_mine = cross_sectional_z_at_mark(
            values_by_mark[mark],
            policy.surviving_features,
            winsor=policy.z_winsor,
            min_symbols=policy.z_min_symbols,
        )
        labels_mine = assign_labels(fit, z_mine)
        labels_theirs = retro_labels(research_fit, z_panel, mark)
        if labels_mine != labels_theirs:
            for symbol in set(labels_mine) | set(labels_theirs):
                if labels_mine.get(symbol) != labels_theirs.get(symbol):
                    label_mismatches += 1
                    _log(
                        f"LABEL MISMATCH {mark} {symbol}: "
                        f"{labels_mine.get(symbol)} vs {labels_theirs.get(symbol)}"
                    )

        active_mine, reserved_mine, _ = mark_weights(policy, fit, z_mine)
        active_theirs, reserved_theirs = context.mark_weights("A1_B", mark)
        for symbol in sorted(set(active_mine) | set(active_theirs)):
            delta = abs(active_mine.get(symbol, 0.0) - active_theirs.get(symbol, 0.0))
            max_weight_delta = max(max_weight_delta, delta)
            if delta > TOLERANCE:
                weight_mismatches += 1
                _log(
                    f"WEIGHT MISMATCH {mark} {symbol}: "
                    f"{active_mine.get(symbol)} vs {active_theirs.get(symbol)}"
                )
        for symbol in sorted(set(reserved_mine) | set(reserved_theirs)):
            delta = abs(reserved_mine.get(symbol, 0.0) - reserved_theirs.get(symbol, 0.0))
            if delta > TOLERANCE:
                weight_mismatches += 1
                _log(f"RESERVED MISMATCH {mark} {symbol}")

    report["labels_and_weights"] = {
        "marks_with_fit": marks_with_fit,
        "label_mismatches": label_mismatches,
        "weight_mismatches": weight_mismatches,
        "max_weight_delta": max_weight_delta,
    }
    verdict = "PASS" if (fp_mismatches + label_mismatches + weight_mismatches) == 0 else "FAIL"
    report["verdict"] = verdict
    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "reference_parity.json", report)
    _log(
        f"parity {verdict}: {marks_with_fit} fitted marks, "
        f"{label_mismatches} label / {weight_mismatches} weight mismatches, "
        f"max weight delta {max_weight_delta:.2e} "
        f"({time.perf_counter() - started:.0f}s)"
    )


if __name__ == "__main__":
    main()
