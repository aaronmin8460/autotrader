"""The pilot runner. Trains V4 per window, scores V1-V5, replays, writes artifacts.

Deliberately bounded: two worker processes at most, two symbols, six windows, and
no parameter sweep. Another study may be running on the same machine, and a
pilot that starves it of cores has answered a question nobody asked.

One process per symbol. That is the natural split - the two symbols share no
state, and each holds one frame and one engine at a time - and it caps the
memory as well as the CPU. Within a symbol everything runs sequentially, in the
window-then-engine order the report reads in.

Run it as a module so the `studies` package resolves:

    python -m studies.equity_v1_v5.run_pilot --output <dir>
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import time
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from studies.equity_v1_v5 import PILOT_SYMBOLS
from studies.equity_v1_v5.aggregation import audit as aggregation_audit
from studies.equity_v1_v5.calendar import read_snapshot, snapshot_path
from studies.equity_v1_v5.dataset import evaluation_path
from studies.equity_v1_v5.scoring import (
    COST_MODELS,
    build_engines,
    decisions_to_frame,
    insufficient_history_count,
    metrics_for,
    overnight_fills,
    replay_series,
    score_window,
    verify_series_matches_live,
)
from studies.equity_v1_v5.walkforward import (
    assert_no_forward_information,
    describe_plan,
    train_for_window,
)
from studies.equity_v1_v5.windows import SCORING_WINDOWS, coverage_report

#: The hard cap on worker processes. Two, because a concurrent study has as much
#: claim on this machine as this one does.
MAX_WORKERS = 2

#: The dataset window every artifact in this study is keyed by.
DATA_START = date(2021, 1, 4)
DATA_END = date(2026, 8, 28)
CALENDAR_START = date(2020, 1, 1)
CALENDAR_END = date(2026, 12, 31)

#: Fixed so a re-run reproduces the same models. Every fit in this study is
#: seeded from it.
SEED = 0

#: Fixed so the artifact's own `trained_at` cannot make two identical runs
#: differ. The wall-clock time of the run is recorded separately, in the
#: manifest, where it belongs.
TRAINED_AT = datetime(2026, 8, 29, tzinfo=UTC)


def run_symbol(symbol: str, datasets: Path, output: Path) -> dict[str, object]:
    """Train, score and replay one symbol. Everything this process needs is here."""
    started = time.perf_counter()
    calendar, calendar_meta = read_snapshot(snapshot_path(datasets, CALENDAR_START, CALENDAR_END))
    frame = pd.read_parquet(evaluation_path(datasets, symbol, DATA_START, DATA_END))
    provenance = json.loads(
        evaluation_path(datasets, symbol, DATA_START, DATA_END)
        .with_suffix(".provenance.json")
        .read_text(encoding="utf-8")
    )

    result: dict[str, object] = {
        "symbol": symbol,
        "dataset": provenance,
        "calendar_retrieved_at_utc": calendar_meta.get("retrieved_at_utc"),
        "coverage": coverage_report(calendar, frame),
        "aggregation": aggregation_audit(frame, calendar),
        "windows": [],
    }

    models = []
    for window in SCORING_WINDOWS:
        model = train_for_window(
            frame, calendar, window, symbol=symbol, seed=SEED, trained_at=TRAINED_AT
        )
        models.append(model)
    result["walk_forward"] = describe_plan(models)
    result["walk_forward_violations"] = list(assert_no_forward_information(models, frame))

    series_dir = output / "decisions"
    series_dir.mkdir(parents=True, exist_ok=True)

    for window, model in zip(SCORING_WINDOWS, models, strict=True):
        window_bars = window.bars(frame)
        entry: dict[str, object] = {
            "window": window.to_json_dict(),
            "scored_bars": len(window_bars),
            "engines": [],
        }
        for spec in build_engines():
            artifact = model.artifact if spec.needs_model else None
            elapsed = time.perf_counter()
            records = score_window(frame, window, spec, symbol=symbol, artifact=artifact)
            scoring_seconds = time.perf_counter() - elapsed

            stored = decisions_to_frame(records)
            stored.to_parquet(
                series_dir / f"{symbol}_{window.name}_{spec.name}.parquet",
                engine="pyarrow",
                index=False,
            )

            mismatches = verify_series_matches_live(
                frame, records, spec, symbol=symbol, artifact=artifact
            )
            replays: dict[str, object] = {}
            for cost_model in COST_MODELS:
                replayed = replay_series(
                    window_bars,
                    records,
                    name=spec.name,
                    version=spec.version,
                    cost_model=cost_model,
                )
                replays[cost_model.label] = metrics_for(replayed)

            signals = sum(1 for record in records if record.to_signal() is not None)
            entry["engines"].append(
                {
                    "engine": spec.name,
                    "decisions": len(records),
                    "signals": signals,
                    "insufficient_history": insufficient_history_count(records),
                    "overnight_fills": overnight_fills(window_bars, records),
                    "live_series_mismatches": list(mismatches),
                    "scoring_seconds": round(scoring_seconds, 2),
                    "metrics": replays,
                }
            )
            print(
                f"  [{symbol}] {window.name}/{spec.name}: {len(records)} bars, "
                f"{signals} signals, {scoring_seconds:.0f}s",
                flush=True,
            )
        result["windows"].append(entry)

    result["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    (output / f"{symbol}_pilot.json").write_text(
        json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return result


def _worker(args: tuple[str, str, str]) -> str:
    symbol, datasets, output = args
    run_symbol(symbol, Path(datasets), Path(output))
    return symbol


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the SPY/QQQ historical pilot.")
    parser.add_argument("--datasets", default=os.environ.get("EQUITY_DATASETS", "."))
    parser.add_argument("--output", default=os.environ.get("PILOT_REPORTS", "."))
    parser.add_argument("--workers", type=int, default=MAX_WORKERS)
    parser.add_argument("--symbols", nargs="*", default=list(PILOT_SYMBOLS))
    arguments = parser.parse_args()

    workers = max(1, min(arguments.workers, MAX_WORKERS, len(arguments.symbols)))
    output = Path(arguments.output)
    output.mkdir(parents=True, exist_ok=True)
    print(f"Running {arguments.symbols} on {workers} worker(s) -> {output}", flush=True)

    started = time.perf_counter()
    payload = [(symbol, arguments.datasets, str(output)) for symbol in arguments.symbols]
    if workers == 1:
        for item in payload:
            _worker(item)
    else:
        with mp.get_context("spawn").Pool(processes=workers) as pool:
            for symbol in pool.imap_unordered(_worker, payload):
                print(f"finished {symbol}", flush=True)
    print(f"Total wall clock: {time.perf_counter() - started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
