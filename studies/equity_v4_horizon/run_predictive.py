"""Stage 1 runner: train and out-of-sample-evaluate every predictive cell.

One process per symbol, sequential over horizons and windows inside it - the
same worker discipline as the pilot, capped at two processes for the whole
study because another research task shares this machine.

The runner enforces two design rules mechanically rather than by discipline:

- **The holdout stays untouched.** 2026-summer cells at the alternative
  horizons are refused unless ``--stage holdout`` is passed, which is only done
  after the selection-set verdict is recorded (design.md sections 5 and 10/P10).
- **Finished cells are skipped, never recomputed.** Every cell lands as an
  atomic checkpoint; a resume after a crash or restart repeats nothing and can
  produce no duplicate (see ``checkpoint``).

Usage::

    python -m studies.equity_v4_horizon.run_predictive --symbol SPY \
        --stage selection --output-root /Volumes/AUTOTRADER_QA/reports/equity-v4-label-horizon
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

from studies.equity_v1_v5.calendar import read_snapshot
from studies.equity_v4_horizon.checkpoint import cell_path, is_complete, write_cell
from studies.equity_v4_horizon.evaluation import (
    common_valid_timestamps,
    evaluate_models,
    evaluation_frames,
    spanning_fraction,
    window_rows,
)
from studies.equity_v4_horizon.horizons import (
    HOLDOUT_WINDOW,
    SELECTION_WINDOWS,
    STUDY_HORIZONS,
    STUDY_SEED,
    prediction_for,
)
from studies.equity_v4_horizon.walkforward import assert_gap_respected, train_cell

#: Where the validated pilot datasets and the calendar snapshot live.
DEFAULT_DATASET_ROOT = Path("/Volumes/AUTOTRADER_QA/datasets/equity-historical")
DEFAULT_OUTPUT_ROOT = Path("/Volumes/AUTOTRADER_QA/reports/equity-v4-label-horizon")
CALENDAR_SNAPSHOT = "market_calendar_2020-01-01_2026-12-31.json"

#: The exact frames this study is allowed to read, by content digest. A frame
#: that does not match is refused: the design pins the pilot's validated data.
EXPECTED_DIGESTS: dict[str, str] = {
    "SPY": "d409cd3b1bdf7847bcc879db68e8b7f8a4b8f310b6b032cef85a06a9017ccc5b",
    "QQQ": "c53d984e588955fa09e0db4aeec0a2d3911a436d380f640da9ab77d6c1cc5a9f",
}
DATASET_STEM = "{symbol}_15m_2021-01-04_2026-08-28"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def load_frame(dataset_root: Path, symbol: str) -> pd.DataFrame:
    """The pilot's session frame for `symbol`, verified against its digest."""
    path = dataset_root / f"{DATASET_STEM.format(symbol=symbol)}.session.parquet"
    frame = pd.read_parquet(path)
    digest = hashlib.sha256(frame.to_csv(index=False).encode("utf-8")).hexdigest()
    expected = EXPECTED_DIGESTS[symbol]
    if digest != expected:
        raise SystemExit(
            f"{path} has digest {digest[:16]}… but the study pins {expected[:16]}…. "
            "Refusing to score unverified data."
        )
    return frame


def windows_for(stage: str) -> tuple:
    if stage == "selection":
        return SELECTION_WINDOWS
    if stage == "holdout":
        return (HOLDOUT_WINDOW,)
    raise SystemExit(f"Unknown stage {stage!r}; use 'selection' or 'holdout'.")


def run_symbol(
    symbol: str,
    *,
    stage: str,
    horizons: tuple[int, ...],
    dataset_root: Path,
    output_root: Path,
) -> int:
    frame = load_frame(dataset_root, symbol)
    calendar, calendar_meta = read_snapshot(dataset_root / CALENDAR_SNAPSHOT)
    log(
        f"{symbol}: {len(frame)} bars verified; calendar snapshot holds "
        f"{calendar_meta.get('session_count')} sessions."
    )

    log(f"{symbol}: building evaluation frames for horizons {list(horizons)}…")
    frames = evaluation_frames(frame, calendar, symbol=symbol, horizons=STUDY_HORIZONS)
    common = common_valid_timestamps(frames)
    log(f"{symbol}: evaluation frames aligned; {len(common)} bars evaluable at every horizon.")

    failures = 0
    for window in windows_for(stage):
        for horizon in horizons:
            path = cell_path(output_root, symbol=symbol, window=window.name, horizon_bars=horizon)
            if is_complete(path):
                log(f"{symbol}/{window.name}/h{horizon}: checkpoint exists, skipping.")
                continue
            started = time.monotonic()
            log(f"{symbol}/{window.name}/h{horizon}: training…")
            cell = train_cell(
                frame, calendar, window, symbol=symbol, horizon_bars=horizon, seed=STUDY_SEED
            )
            gap_problems = assert_gap_respected(cell, frame)
            if gap_problems:
                failures += 1
                log(f"{symbol}/{window.name}/h{horizon}: GAP VIOLATION {gap_problems}")
                continue

            evaluation = frames[horizon]
            rows_common = window_rows(evaluation, window, restrict_to=common)
            rows_full = window_rows(evaluation, window)
            artifacts = {
                "selected": cell.selected_artifact,
                "null": cell.null_artifact,
                **{f"shadow_{k}": v for k, v in cell.shadow_artifacts.items()},
            }
            payload = cell.to_json_dict()
            payload["oos_common_subset"] = evaluate_models(rows_common, artifacts)
            payload["oos_full_window"] = evaluate_models(rows_full, artifacts)
            payload["oos_spanning_fraction"] = spanning_fraction(rows_full)
            payload["predicted_spanning_fraction_full_session"] = prediction_for(
                horizon
            ).session_gap_fraction_full_session
            payload["stage"] = stage
            payload["gap_check"] = "PASS"
            payload["elapsed_seconds"] = round(time.monotonic() - started, 1)
            write_cell(path, payload)
            log(
                f"{symbol}/{window.name}/h{horizon}: {cell.selected_family} "
                f"(beat_baseline={cell.beat_baseline}, "
                f"improvement={cell.baseline_log_loss - cell.selected_log_loss:+.6f}) "
                f"in {payload['elapsed_seconds']}s."
            )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True, choices=sorted(EXPECTED_DIGESTS))
    parser.add_argument("--stage", default="selection", choices=("selection", "holdout"))
    parser.add_argument(
        "--horizons",
        nargs="*",
        type=int,
        default=list(STUDY_HORIZONS),
        help="Subset of the frozen horizon set to run (default: all four).",
    )
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    arguments = parser.parse_args()

    horizons = tuple(int(h) for h in arguments.horizons)
    for horizon in horizons:
        if horizon not in STUDY_HORIZONS:
            raise SystemExit(f"Horizon {horizon} is not in the frozen set {STUDY_HORIZONS}.")

    manifest = {
        "study": "equity-v4-label-horizon",
        "stage": arguments.stage,
        "symbol": arguments.symbol,
        "horizons": list(horizons),
        "seed": STUDY_SEED,
        "started_at": date.today().isoformat(),
        "argv": sys.argv[1:],
    }
    manifest_path = arguments.output_root / f"manifest_{arguments.symbol}_{arguments.stage}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    failures = run_symbol(
        arguments.symbol,
        stage=arguments.stage,
        horizons=horizons,
        dataset_root=arguments.dataset_root,
        output_root=arguments.output_root,
    )
    log(f"{arguments.symbol}: stage {arguments.stage} finished with {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
