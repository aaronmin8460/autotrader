"""The overlapping-label audit, measured on the real frames cell by cell.

The synthetic tests pin the purge and embargo arithmetic exactly - ``h + 1``
rows purged and ``max(0, 26 - (h + 1))`` embargoed at every gapless boundary.
This module measures the same quantities on the real SPY/QQQ training frames
each cell actually fitted on, so the run artifacts carry evidence rather than
an appeal to the tests: per cell, the purged and embargoed row counts at every
inner walk-forward boundary, and a re-run of ``assert_no_leakage`` over the
three-way split the final model was trained under.

    python -m studies.equity_v4_horizon.overlap_audit --symbol SPY
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from autotrader.ml.splits import SplitSpec, assert_no_leakage, temporal_split, walk_forward_folds
from studies.equity_v1_v5.calendar import read_snapshot
from studies.equity_v1_v5.windows import EMBARGO_BARS
from studies.equity_v4_horizon.horizons import (
    HOLDOUT_WINDOW,
    SELECTION_WINDOWS,
    STUDY_HORIZONS,
    outer_gap_bars,
    overlap_factor,
)
from studies.equity_v4_horizon.run_predictive import (
    CALENDAR_SNAPSHOT,
    DEFAULT_DATASET_ROOT,
    DEFAULT_OUTPUT_ROOT,
    load_frame,
    log,
)
from studies.equity_v4_horizon.walkforward import training_frame_for


def audit_symbol(symbol: str, *, dataset_root: Path, output_root: Path, stage: str) -> Path:
    frame = load_frame(dataset_root, symbol)
    calendar, _ = read_snapshot(dataset_root / CALENDAR_SNAPSHOT)
    windows = SELECTION_WINDOWS if stage == "selection" else (HOLDOUT_WINDOW,)

    cells = []
    for window in windows:
        first_scored, _ = window.positions(frame)
        for horizon in STUDY_HORIZONS:
            gap = outer_gap_bars(horizon)
            training = training_frame_for(
                frame,
                calendar,
                symbol=symbol,
                last_row=first_scored - gap - 1,
                horizon_bars=horizon,
            )
            folds = walk_forward_folds(
                training.frame, folds=4, initial_train_fraction=0.5, embargo_bars=EMBARGO_BARS
            )
            boundaries = [
                {
                    "fold": fold.index,
                    "train_rows": fold.train.row_count,
                    "test_rows": fold.test.row_count,
                    "purged_rows": fold.train.purged_rows,
                    "embargoed_rows": fold.train.embargoed_rows,
                    "leak_check": bool(
                        (
                            fold.train.frame["label_knowable_at"]
                            <= fold.test.frame["feature_timestamp"].iloc[0]
                        ).all()
                    ),
                }
                for fold in folds
            ]
            split = temporal_split(training.frame, SplitSpec(embargo_bars=EMBARGO_BARS))
            assert_no_leakage(split)
            cells.append(
                {
                    "symbol": symbol,
                    "window": window.name,
                    "horizon_bars": horizon,
                    "overlap_factor": overlap_factor(horizon),
                    "outer_gap_bars": gap,
                    "expected_purge_gapless": horizon + 1,
                    "expected_embargo_gapless": max(0, EMBARGO_BARS - (horizon + 1)),
                    "inner_folds": boundaries,
                    "three_way_split_leak_free": True,
                    "three_way_purged": [part.purged_rows for part in split.parts],
                    "three_way_embargoed": [part.embargoed_rows for part in split.parts],
                }
            )
            log(
                f"{symbol}/{window.name}/h{horizon}: purge per fold "
                f"{[b['purged_rows'] for b in boundaries]}, embargo "
                f"{[b['embargoed_rows'] for b in boundaries]}, leak checks "
                f"{[b['leak_check'] for b in boundaries]}"
            )

    payload = {
        "study": "equity-v4-label-horizon",
        "audit": "overlapping-label purge/embargo, measured on real frames",
        "stage": stage,
        "embargo_bars": EMBARGO_BARS,
        "cells": cells,
    }
    path = output_root / f"overlap_audit_{symbol}_{stage}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True, choices=("QQQ", "SPY"))
    parser.add_argument("--stage", default="selection", choices=("selection", "holdout"))
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    arguments = parser.parse_args()
    started = time.perf_counter()
    path = audit_symbol(
        arguments.symbol,
        dataset_root=arguments.dataset_root,
        output_root=arguments.output_root,
        stage=arguments.stage,
    )
    elapsed = time.perf_counter() - started
    log(f"{arguments.symbol}: overlap audit written to {path} in {elapsed:.0f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
