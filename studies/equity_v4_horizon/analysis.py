"""Aggregating the cells and applying the frozen winner rule, mechanically.

The winner rule was written down before any horizon was scored (design.md
section 10). This module turns the stage-1 checkpoints into per-horizon
summaries and evaluates criteria P1-P8 exactly as declared, so the verdict is
computed from the rule rather than argued from the tables. P9 reads the stage-2
diagnostics; P10 is evaluated only after the selection-set verdict is recorded.

Nothing here can change what counts as winning. The thresholds are imported
from the frozen declarations or restated with their design references, and the
one function that says "replace the shipped horizon" demands every criterion at
once.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from studies.equity_v4_horizon.checkpoint import cell_path, read_cell
from studies.equity_v4_horizon.horizons import (
    SELECTION_WINDOWS,
    STUDY_HORIZONS,
)

SYMBOLS = ("SPY", "QQQ")

#: P1: minimum non-null cells (of 10) for a challenger horizon.
P1_MIN_NON_NULL = 4
#: P2: minimum non-null cells per symbol.
P2_MIN_PER_SYMBOL = 2
#: P3: minimum distinct windows with a non-null cell.
P3_MIN_WINDOWS = 3
#: P5: minimum median distinct calibrated levels over non-null models, and the
#: ECE margin the selected model may carry over the null.
P5_MIN_LEVELS = 5
P5_ECE_MARGIN = 0.02


def load_cells(output_root: Path, *, windows=None, horizons=STUDY_HORIZONS) -> list[dict]:
    """Every completed selection-set cell, refusing to proceed on gaps."""
    chosen = windows if windows is not None else SELECTION_WINDOWS
    cells = []
    for symbol in SYMBOLS:
        for window in chosen:
            for horizon in horizons:
                path = cell_path(
                    output_root, symbol=symbol, window=window.name, horizon_bars=horizon
                )
                cells.append(read_cell(path))
    return cells


def _oos_gain(cell: dict) -> float | None:
    """The selected model's common-subset log-loss gain over the null."""
    models = cell.get("oos_common_subset", {}).get("models")
    if not models:
        return None
    return float(models["selected"]["log_loss_gain_vs_null"])


def _oos_ece(cell: dict, model: str) -> float | None:
    models = cell.get("oos_common_subset", {}).get("models")
    if not models or model not in models:
        return None
    return float(models[model]["metrics"]["expected_calibration_error"])


def _selected_audit(cell: dict) -> dict | None:
    """The calibration audit of the cell's selected model."""
    version = cell.get("selected_artifact", {}).get("model_version")
    return cell.get("calibration_audits", {}).get(version)


def _thin_extreme_emitted(cell: dict) -> bool:
    """P6: did the selected model emit thin-bin extreme confidence on scored bars?

    True only when both halves hold: the calibration carries an extreme step
    with fewer validation rows than the declared support floor, AND the scored
    window actually produced predictions at or beyond the extreme bounds.
    """
    audit = _selected_audit(cell)
    if not audit or not audit.get("extreme_from_thin_bins"):
        return False
    models = cell.get("oos_full_window", {}).get("models")
    if not models:
        return False
    distribution = models["selected"]["distribution"]
    return (distribution.get("n_extreme_high", 0) + distribution.get("n_extreme_low", 0)) > 0


def summarize_horizon(cells: list[dict], horizon: int) -> dict:
    """Everything the winner rule reads about one horizon, plus the raw lists."""
    mine = [cell for cell in cells if cell["horizon_bars"] == horizon]
    non_null = [cell for cell in mine if cell["beat_baseline"]]
    clean_non_null = [cell for cell in non_null if not _thin_extreme_emitted(cell)]
    gains = [g for g in (_oos_gain(cell) for cell in mine) if g is not None]
    non_null_gains = [(cell["window"], cell["symbol"], _oos_gain(cell)) for cell in clean_non_null]
    audits = [a for a in (_selected_audit(cell) for cell in non_null) if a]
    levels = [int(a.get("distinct_levels", 1)) for a in audits]
    ece_pairs = [
        (
            _oos_ece(cell, "selected"),
            _oos_ece(cell, "null"),
        )
        for cell in non_null
    ]
    ece_deltas = [s - n for s, n in ece_pairs if s is not None and n is not None]
    return {
        "horizon_bars": horizon,
        "cells": len(mine),
        "non_null_cells": len(non_null),
        "non_null_cells_clean": len(clean_non_null),
        "non_null_by_symbol": {
            symbol: sum(1 for cell in clean_non_null if cell["symbol"] == symbol)
            for symbol in SYMBOLS
        },
        "non_null_windows": sorted({cell["window"] for cell in clean_non_null}),
        "thin_extreme_cells": [
            f"{cell['symbol']}/{cell['window']}" for cell in non_null if _thin_extreme_emitted(cell)
        ],
        "inner_improvements": [
            {
                "symbol": cell["symbol"],
                "window": cell["window"],
                "selected": cell["selected_family"],
                "improvement": cell["log_loss_improvement"],
            }
            for cell in mine
        ],
        "mean_oos_gain_all_cells": statistics.fmean(gains) if gains else None,
        "non_null_oos_gains": non_null_gains,
        "median_distinct_levels_non_null": statistics.median(levels) if levels else None,
        "median_ece_delta_non_null": statistics.median(ece_deltas) if ece_deltas else None,
        "label_base_rates": {
            f"{cell['symbol']}/{cell['window']}": cell["label_base_rate"] for cell in mine
        },
        "spanning_fractions": {
            f"{cell['symbol']}/{cell['window']}": cell["oos_spanning_fraction"] for cell in mine
        },
    }


def evaluate_criteria(cells: list[dict], challenger: int) -> dict:
    """P1-P8 for one challenger horizon against the shipped 4-bar horizon.

    P8 (leakage) is reported from the per-cell gap checks; the overlap audit
    and the test suite carry the rest of that criterion's evidence.
    """
    summary = summarize_horizon(cells, challenger)
    baseline = summarize_horizon(cells, 4)
    non_null = summary["non_null_cells_clean"]

    p1 = non_null >= P1_MIN_NON_NULL and non_null > baseline["non_null_cells_clean"]
    p2 = all(count >= P2_MIN_PER_SYMBOL for count in summary["non_null_by_symbol"].values())
    p3 = len(summary["non_null_windows"]) >= P3_MIN_WINDOWS

    gains = [g for _, _, g in summary["non_null_oos_gains"] if g is not None]
    if gains and len(gains) > 1:
        by_window: dict[str, list[float]] = {}
        for window, _, gain in summary["non_null_oos_gains"]:
            by_window.setdefault(window, []).append(gain)
        best_window = max(by_window, key=lambda w: max(by_window[w]))
        rest = [
            gain
            for window, _, gain in summary["non_null_oos_gains"]
            if window != best_window and gain is not None
        ]
        p4 = bool(rest) and statistics.fmean(rest) > 0.0
    else:
        p4 = False

    levels = summary["median_distinct_levels_non_null"]
    ece_delta = summary["median_ece_delta_non_null"]
    p5 = (
        levels is not None
        and levels >= P5_MIN_LEVELS
        and ece_delta is not None
        and ece_delta <= P5_ECE_MARGIN
    )
    p6 = not summary["thin_extreme_cells"]

    neighbours = {
        4: (8,),
        8: (4, 16),
        16: (8, 26),
        26: (16,),
    }[challenger]
    p7 = False
    for neighbour in neighbours:
        n_summary = summarize_horizon(cells, neighbour)
        gain = n_summary["mean_oos_gain_all_cells"]
        if n_summary["non_null_cells_clean"] >= 2 or (gain is not None and gain > 0.0):
            p7 = True

    p8 = all(
        cell.get("gap_check") == "PASS" for cell in cells if cell["horizon_bars"] == challenger
    )

    return {
        "challenger": challenger,
        "P1_materiality": p1,
        "P2_both_symbols": p2,
        "P3_multiple_windows": p3,
        "P4_not_one_window": p4,
        "P5_calibration_credible": p5,
        "P6_no_thin_extremes": p6,
        "P7_neighbor_coherence": p7,
        "P8_leakage_clean": p8,
        "P1_to_P8_all": all((p1, p2, p3, p4, p5, p6, p7, p8)),
        "summary": summary,
    }


def selection_verdict(output_root: Path) -> dict:
    """The full selection-set analysis: summaries, criteria, and the verdict."""
    cells = load_cells(output_root)
    summaries = {h: summarize_horizon(cells, h) for h in STUDY_HORIZONS}
    criteria = {h: evaluate_criteria(cells, h) for h in STUDY_HORIZONS if h != 4}
    survivors = [h for h, entry in criteria.items() if entry["P1_to_P8_all"]]
    verdict = {
        "summaries": summaries,
        "criteria": criteria,
        "survivors_P1_to_P8": survivors,
        # Tie-break: the shorter horizon wins (design.md section 10).
        "provisional_winner": min(survivors) if survivors else None,
    }
    return verdict


def write_verdict(output_root: Path) -> Path:
    payload = selection_verdict(output_root)
    path = output_root / "selection_verdict.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


__all__ = [
    "P1_MIN_NON_NULL",
    "P2_MIN_PER_SYMBOL",
    "P3_MIN_WINDOWS",
    "P5_ECE_MARGIN",
    "P5_MIN_LEVELS",
    "SYMBOLS",
    "evaluate_criteria",
    "load_cells",
    "selection_verdict",
    "summarize_horizon",
    "write_verdict",
]
