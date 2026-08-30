"""Deterministic reproduction: representative cells rerun and required to match.

Three checks, each against the stored study artifacts rather than against a
second run of the same code path in the same process:

- **dataset digests**: every symbol's frame re-hashed and required to equal its
  provenance sidecar;
- **training determinism**: representative cells retrained from scratch and
  required to serialize identically to the stored checkpoint (the fixed
  ``trained_at`` makes byte equality the honest bar, not an approximation);
- **scoring determinism**: the head of each representative window re-scored
  through the single-pass path and required to reproduce the stored decision
  series row for row.

    python -m studies.equity_10_full.run_repro --datasets <dir> --output <dir>
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import pandas as pd

from studies.equity_10_full import DATA_END, DATA_START, STUDY_SYMBOLS
from studies.equity_10_full.checkpoint import (
    cell_path,
    is_complete,
    read_json,
    series_path,
    write_json,
)
from studies.equity_10_full.run_study import load_calendar, load_frame, log
from studies.equity_10_full.triple import score_window_triple
from studies.equity_10_full.walkforward import train_cell
from studies.equity_10_full.windows import window_by_name
from studies.equity_v1_v5.dataset import evaluation_path, frame_digest

#: The representative cells rerun end to end. Chosen before results were
#: examined: the first symbol's mid-study window, the symbol with the worst
#: measured warm-up, and a split symbol's split window.
REPRESENTATIVE_CELLS: tuple[tuple[str, str], ...] = (
    ("SPY", "w05"),
    ("GOOGL", "w09"),
    ("NVDA", "w07"),
)

#: How many bars of each representative window the scoring check reproduces.
RESCORE_BARS = 120


def canonical(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the reproducibility checks.")
    parser.add_argument("--datasets", default=os.environ.get("EQUITY_DATASETS", "."))
    parser.add_argument("--output", default=os.environ.get("STUDY_REPORTS", "."))
    arguments = parser.parse_args()
    datasets, output = Path(arguments.datasets), Path(arguments.output)
    target = cell_path(output, kind="repro", symbol="study", unit="reproducibility")
    if is_complete(target):
        log("reproducibility checkpoint exists, skipping.")
        return

    checks: list[dict[str, object]] = []

    for symbol in STUDY_SYMBOLS:
        frame = load_frame(datasets, symbol)
        sidecar = evaluation_path(datasets, symbol, DATA_START, DATA_END).with_suffix(
            ".provenance.json"
        )
        recorded = json.loads(sidecar.read_text(encoding="utf-8"))["frame_sha256"]
        ok = frame_digest(frame) == recorded
        checks.append({"symbol": symbol, "check": "dataset_digest", "ok": ok})
        log(f"{symbol}: dataset_digest {'PASS' if ok else 'FAIL'}")

    calendar = load_calendar(datasets)
    for symbol, window_name in REPRESENTATIVE_CELLS:
        window = window_by_name(window_name)
        frame = load_frame(datasets, symbol)
        stored = read_json(cell_path(output, kind="cells", symbol=symbol, unit=window_name))

        started = time.perf_counter()
        retrained = train_cell(frame, calendar, window, symbol=symbol)
        ok = canonical(retrained.to_json_dict()) == canonical(stored["train"])
        checks.append(
            {
                "symbol": symbol,
                "check": f"training_determinism_{window_name}",
                "ok": ok,
                "seconds": round(time.perf_counter() - started, 1),
            }
        )
        log(f"{symbol}/{window_name}: training_determinism {'PASS' if ok else 'FAIL'}")

        started = time.perf_counter()
        rescored = score_window_triple(
            frame,
            window,
            symbol=symbol,
            artifact=retrained.selected_artifact,
            max_bars=RESCORE_BARS,
        )
        for engine in ("V3", "V4", "V5"):
            stored_series = pd.read_parquet(
                series_path(output, symbol=symbol, window=window_name, engine=engine)
            ).head(len(rescored[engine]))
            fresh = pd.DataFrame([record.to_row() for record in rescored[engine]])
            ok = stored_series.reset_index(drop=True).equals(fresh.reset_index(drop=True))
            checks.append(
                {
                    "symbol": symbol,
                    "check": f"scoring_determinism_{window_name}_{engine}",
                    "ok": ok,
                }
            )
            log(f"{symbol}/{window_name}/{engine}: scoring_determinism {'PASS' if ok else 'FAIL'}")

    failed = [check for check in checks if not check["ok"]]
    write_json(target, {"checks": checks, "failures": len(failed)})
    if failed:
        raise SystemExit(f"reproducibility: {len(failed)} check(s) FAILED; see {target}")
    log("reproducibility: all checks PASS.")


if __name__ == "__main__":
    main()
