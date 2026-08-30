"""The full-study runner: staged, checkpointed, resumable, and honest about failure.

Stages, each idempotent and safe to re-run (finished units are skipped):

``prep``      per symbol: dataset digest verification, coverage report,
              aggregation audit, and the warm-up probe that proves sampled
              scored bars answer with zero INSUFFICIENT_* / FEATURE_UNAVAILABLE
              tokens before any heavy compute is spent.
``score``     per symbol x development window: train the V4 cell (selected +
              raw null + shadows + calibration audits), score V1/V2 live and
              V3/V4/V5 from one V5 pass, verify stored-vs-live and the
              single-pass recovery, replay under three cost models, benchmark
              buy-and-hold, and checkpoint everything.
``finalize``  per symbol: V4 out-of-sample evaluation on ground-truth labels,
              continuous development-region replays, equity-curve export.
``holdout-score`` / ``holdout-finalize``
              the same two stages for the w12 holdout. REFUSED until
              ``holdout_unlock.json`` exists in the output directory - the
              development conclusion must be recorded before the holdout is
              touched, and the unlock file's timestamp is the evidence.

Run as a module so the ``studies`` package resolves::

    python -m studies.equity_10_full.run_study --stage score --workers 6 \
        --datasets <dir> --output <dir>

Failures are classified (DATA / SESSION / AGGREGATION / WARMUP / MODEL /
CALIBRATION / LEAKAGE / CHECKPOINT / REPLAY / METRICS / INFRASTRUCTURE),
logged, and never restart the stage: a failed unit simply has no checkpoint
and is retried on the next invocation.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from studies.equity_10_full import (
    CALENDAR_END,
    CALENDAR_START,
    DATA_END,
    DATA_START,
    STUDY_SYMBOLS,
)
from studies.equity_10_full.benchmarks import BuyAndHoldEngine, forced_liquidation
from studies.equity_10_full.checkpoint import (
    cell_path,
    is_complete,
    read_json,
    series_path,
    write_json,
    write_series,
)
from studies.equity_10_full.evaluation import evaluate_models, full_frame_training, window_rows
from studies.equity_10_full.triple import score_window_triple, verify_recovered_records
from studies.equity_10_full.walkforward import assert_gap_respected, train_cell
from studies.equity_10_full.windows import (
    DEV_WINDOWS,
    FULL_WINDOWS,
    HOLDOUT_WINDOW,
    LOOKBACK_BARS,
)
from studies.equity_v1_v5.aggregation import audit as aggregation_audit
from studies.equity_v1_v5.calendar import read_snapshot, snapshot_path
from studies.equity_v1_v5.dataset import evaluation_path, frame_digest
from studies.equity_v1_v5.scoring import (
    COST_MODELS,
    INITIAL_CASH,
    build_engines,
    decisions_to_frame,
    frame_to_decisions,
    insufficient_history_count,
    metrics_for,
    overnight_fills,
    replay_series,
    score_window,
    verify_series_matches_live,
)
from studies.equity_v1_v5.windows import ScoringWindow, coverage_report

#: The default worker cap: six of this machine's eight cores, per the pilot's
#: recommendation for a dedicated run.
MAX_WORKERS = 6

#: Positions probed per symbol in the warm-up pre-flight, spread evenly over
#: the development scored region.
WARMUP_PROBE_POSITIONS = 12

#: Failure classes for the log. The class is chosen from the exception type
#: and the stage, not guessed from the message.
FAILURE_CLASSES = (
    "DATA",
    "SESSION",
    "AGGREGATION",
    "WARMUP",
    "MODEL",
    "CALIBRATION",
    "LEAKAGE",
    "CHECKPOINT",
    "REPLAY",
    "METRICS",
    "INFRASTRUCTURE",
)


def log(message: str) -> None:
    print(f"[{datetime.now(UTC).isoformat(timespec='seconds')}] {message}", flush=True)


def load_frame(datasets: Path, symbol: str) -> pd.DataFrame:
    return pd.read_parquet(evaluation_path(datasets, symbol, DATA_START, DATA_END))


def load_calendar(datasets: Path):
    calendar, _meta = read_snapshot(snapshot_path(datasets, CALENDAR_START, CALENDAR_END))
    return calendar


def feature_unavailable_count(records) -> int:
    """Scored bars declined because a feature could not be computed."""
    return sum(
        1
        for record in records
        if any(reason.startswith("FEATURE_UNAVAILABLE") for reason in record.reasons)
    )


# --------------------------------------------------------------------------
# prep
# --------------------------------------------------------------------------


def run_prep_symbol(args: tuple[str, str, str]) -> str:
    symbol, datasets_str, output_str = args
    datasets, output = Path(datasets_str), Path(output_str)
    target = cell_path(output, kind="prep", symbol=symbol, unit="prep")
    if is_complete(target):
        log(f"{symbol}: prep checkpoint exists, skipping.")
        return symbol

    frame = load_frame(datasets, symbol)
    calendar = load_calendar(datasets)
    sidecar = evaluation_path(datasets, symbol, DATA_START, DATA_END).with_suffix(
        ".provenance.json"
    )
    provenance = json.loads(sidecar.read_text(encoding="utf-8"))
    digest = frame_digest(frame)
    if digest != provenance["frame_sha256"]:
        raise RuntimeError(f"DATA {symbol}: frame digest mismatch against provenance.")

    log(f"{symbol}: coverage + aggregation audit over {len(frame)} bars…")
    coverage = coverage_report(calendar, frame, FULL_WINDOWS)
    aggregation = aggregation_audit(frame, calendar)

    # Warm-up probe: V1, V2 and V3 driven live on sampled scored bars. V4 reads
    # V2's features on V2's timeframe and V5 requires exactly V3-and-V4, so a
    # clean V2 + V3 probe bounds all five engines' availability.
    first_scored, _ = DEV_WINDOWS[0].positions(frame)
    _, last_scored = DEV_WINDOWS[-1].positions(frame)
    positions = [
        first_scored + int((last_scored - first_scored) * k / (WARMUP_PROBE_POSITIONS - 1))
        for k in range(WARMUP_PROBE_POSITIONS)
    ]
    probe_findings: list[str] = []
    probed = []
    engines = [spec for spec in build_engines() if spec.name in ("V1", "V2", "V3")]
    for position in positions:
        window_slice = frame.iloc[position - LOOKBACK_BARS + 1 : position + 1].reset_index(
            drop=True
        )
        for spec in engines:
            engine = spec.build(symbol, None)
            result = engine.decide(window_slice)
            bad = [
                reason
                for reason in result.reasons
                if reason.startswith("INSUFFICIENT") or reason.startswith("FEATURE_UNAVAILABLE")
            ]
            probed.append(
                {
                    "engine": spec.name,
                    "timestamp": str(frame["timestamp"].iloc[position]),
                    "blocked": bool(bad),
                }
            )
            if bad:
                probe_findings.append(
                    f"{symbol}/{spec.name} at {frame['timestamp'].iloc[position]}: {bad}"
                )
    if probe_findings:
        raise RuntimeError(f"WARMUP {symbol}: probe found blocked bars: {probe_findings}")

    write_json(
        target,
        {
            "symbol": symbol,
            "frame_sha256": digest,
            "rows": len(frame),
            "provenance": provenance,
            "coverage": coverage,
            "aggregation": aggregation,
            "warmup_probe": {
                "lookback_bars": LOOKBACK_BARS,
                "positions_probed": len(positions),
                "engines": ["V1", "V2", "V3"],
                "blocked": 0,
                "probes": probed,
            },
        },
    )
    log(f"{symbol}: prep complete, warm-up probe clean at lookback {LOOKBACK_BARS}.")
    return symbol


# --------------------------------------------------------------------------
# score
# --------------------------------------------------------------------------


def replay_block(window_bars: pd.DataFrame, records, name: str, version: str) -> dict[str, object]:
    """Every cost model's replay of one stored series, with terminal diagnostics."""
    block: dict[str, object] = {}
    for cost_model in COST_MODELS:
        replayed = replay_series(
            window_bars, records, name=name, version=version, cost_model=cost_model
        )
        block[cost_model.label] = {
            "metrics": metrics_for(replayed),
            "terminal": forced_liquidation(replayed, cost_model),
        }
    return block


def benchmark_block(window_bars: pd.DataFrame) -> dict[str, object]:
    """Buy-and-hold through the same simulator, every cost model."""
    from autotrader.data.validation import EQUITY_UNIVERSE_LABEL
    from autotrader.equity import EQUITY_SYMBOLS
    from autotrader.research.replay import ReplayConfig, replay

    block: dict[str, object] = {}
    for cost_model in COST_MODELS:
        config = ReplayConfig(
            initial_cash=INITIAL_CASH,
            cost_model=cost_model,
            supported_symbols=EQUITY_SYMBOLS,
            universe_label=EQUITY_UNIVERSE_LABEL,
        )
        replayed = replay(window_bars, BuyAndHoldEngine(), config)
        block[cost_model.label] = {
            "metrics": metrics_for(replayed),
            "terminal": forced_liquidation(replayed, cost_model),
        }
    return block


def run_score_unit(args: tuple[str, str, str, str]) -> str:
    symbol, window_name, datasets_str, output_str = args
    datasets, output = Path(datasets_str), Path(output_str)
    window = next(w for w in FULL_WINDOWS if w.name == window_name)
    unit = f"{symbol}/{window.name}"
    target = cell_path(output, kind="cells", symbol=symbol, unit=window.name)
    if is_complete(target):
        log(f"{unit}: cell checkpoint exists, skipping.")
        return unit

    started = time.perf_counter()
    frame = load_frame(datasets, symbol)
    calendar = load_calendar(datasets)

    log(f"{unit}: training V4 cell…")
    cell = train_cell(frame, calendar, window, symbol=symbol)
    gap_problems = assert_gap_respected(
        symbol=symbol,
        window=window.name,
        training_last_bar=cell.training_last_bar,
        scoring_first_bar=cell.scoring_first_bar,
        gap_bars=cell.gap_bars,
        frame=frame,
    )
    if gap_problems:
        raise RuntimeError(f"LEAKAGE {unit}: {gap_problems}")

    window_bars = window.bars(frame)
    engines_by_name = {spec.name: spec for spec in build_engines()}

    log(f"{unit}: scoring V1/V2 live and V3/V4/V5 from one V5 pass ({len(window_bars)} bars)…")
    series: dict[str, tuple] = {}
    for name in ("V1", "V2"):
        spec = engines_by_name[name]
        series[name] = score_window(
            frame, window, spec, symbol=symbol, artifact=None, lookback_bars=LOOKBACK_BARS
        )
    triple = score_window_triple(
        frame, window, symbol=symbol, artifact=cell.selected_artifact, lookback_bars=LOOKBACK_BARS
    )
    series.update(triple)

    log(f"{unit}: verifying stored-vs-live and the single-pass recovery…")
    verification: dict[str, object] = {}
    for name in ("V1", "V2", "V5"):
        spec = engines_by_name[name]
        artifact = cell.selected_artifact if spec.needs_model else None
        mismatches = verify_series_matches_live(
            frame,
            series[name],
            spec,
            symbol=symbol,
            artifact=artifact,
            lookback_bars=LOOKBACK_BARS,
        )
        verification[name] = list(mismatches)
        if mismatches:
            raise RuntimeError(f"REPLAY {unit}: stored {name} series mismatches live: {mismatches}")
    recovery = verify_recovered_records(
        frame,
        symbol=symbol,
        artifact=cell.selected_artifact,
        recovered_v3=series["V3"],
        recovered_v4=series["V4"],
        lookback_bars=LOOKBACK_BARS,
    )
    verification["V3_V4_recovery"] = list(recovery)
    if recovery and recovery != ("no records to verify",):
        raise RuntimeError(f"REPLAY {unit}: single-pass recovery not bit-identical: {recovery}")

    engine_entries: dict[str, object] = {}
    for name in ("V1", "V2", "V3", "V4", "V5"):
        records = series[name]
        insufficient = insufficient_history_count(records)
        unavailable = feature_unavailable_count(records)
        if insufficient:
            raise RuntimeError(
                f"WARMUP {unit}: {name} declined {insufficient} scored bars for want of "
                f"history at lookback {LOOKBACK_BARS}."
            )
        write_series(
            series_path(output, symbol=symbol, window=window.name, engine=name),
            decisions_to_frame(records),
        )
        engine_entries[name] = {
            "decisions": len(records),
            "signals": sum(1 for record in records if record.to_signal() is not None),
            "insufficient_history": insufficient,
            "feature_unavailable": unavailable,
            "overnight_fills": overnight_fills(window_bars, records),
            "replays": replay_block(window_bars, records, name, engines_by_name[name].version),
        }

    payload = {
        "symbol": symbol,
        "window": window.to_json_dict(),
        "scored_bars": len(window_bars),
        "lookback_bars": LOOKBACK_BARS,
        "train": cell.to_json_dict(),
        "engines": engine_entries,
        "benchmark_buy_and_hold": benchmark_block(window_bars),
        "verification": verification,
        "elapsed_seconds": round(time.perf_counter() - started, 1),
    }
    write_json(target, payload)
    log(f"{unit}: complete in {payload['elapsed_seconds']}s.")
    return unit


# --------------------------------------------------------------------------
# finalize
# --------------------------------------------------------------------------


def continuous_region(frame: pd.DataFrame, windows: tuple[ScoringWindow, ...]) -> pd.DataFrame:
    region = ScoringWindow(
        name="region", start=windows[0].start, end=windows[-1].end, covers="continuous region"
    )
    return region.bars(frame)


def run_finalize_symbol(args: tuple[str, str, str, str]) -> str:
    symbol, datasets_str, output_str, scope = args
    datasets, output = Path(datasets_str), Path(output_str)
    windows = DEV_WINDOWS if scope == "dev" else FULL_WINDOWS
    unit_name = "summary" if scope == "dev" else "summary_full"
    target = cell_path(output, kind="finalize", symbol=symbol, unit=unit_name)
    if is_complete(target):
        log(f"{symbol}: finalize({scope}) checkpoint exists, skipping.")
        return symbol

    frame = load_frame(datasets, symbol)
    calendar = load_calendar(datasets)

    cells = {
        window.name: read_json(cell_path(output, kind="cells", symbol=symbol, unit=window.name))
        for window in windows
    }

    log(f"{symbol}: V4 out-of-sample evaluation on ground-truth labels…")
    from autotrader.decision.probability import artifact_from_record

    ground_truth = full_frame_training(frame, calendar)
    oos: dict[str, object] = {}
    for window in windows:
        cell = cells[window.name]["train"]
        artifacts = {
            "selected": artifact_from_record(cell["selected_artifact"]),
            "null": artifact_from_record(cell["null_artifact"]),
        }
        for family, record in cell["shadow_artifacts"].items():
            artifacts[f"shadow_{family}"] = artifact_from_record(record)
        oos[window.name] = evaluate_models(window_rows(ground_truth, window), artifacts)

    log(f"{symbol}: continuous {scope}-region replays…")
    region_bars = continuous_region(frame, windows)
    continuous: dict[str, object] = {}
    curves: dict[str, object] = {"timestamp": [str(ts) for ts in region_bars["timestamp"]]}
    for name in ("V1", "V2", "V3", "V4", "V5"):
        records = []
        for window in windows:
            stored = pd.read_parquet(
                series_path(output, symbol=symbol, window=window.name, engine=name)
            )
            records.extend(frame_to_decisions(stored))
        records = tuple(records)
        block: dict[str, object] = {}
        for cost_model in COST_MODELS:
            replayed = replay_series(
                region_bars, records, name=name, version=name.lower(), cost_model=cost_model
            )
            block[cost_model.label] = {
                "metrics": metrics_for(replayed),
                "terminal": forced_liquidation(replayed, cost_model),
            }
            if cost_model.label == "equity-marketable":
                curves[name] = [str(value) for value in replayed.equity_curve]
        continuous[name] = block
    continuous["BUY_AND_HOLD"] = benchmark_block(region_bars)

    from autotrader.data.validation import EQUITY_UNIVERSE_LABEL
    from autotrader.equity import EQUITY_SYMBOLS
    from autotrader.research.replay import ReplayConfig, replay

    for cost_model in COST_MODELS:
        if cost_model.label == "equity-marketable":
            config = ReplayConfig(
                initial_cash=INITIAL_CASH,
                cost_model=cost_model,
                supported_symbols=EQUITY_SYMBOLS,
                universe_label=EQUITY_UNIVERSE_LABEL,
            )
            replayed = replay(region_bars, BuyAndHoldEngine(), config)
            curves["BUY_AND_HOLD"] = [str(value) for value in replayed.equity_curve]

    curve_path = Path(output) / "curves" / f"{symbol}_{scope}_equity_curves.parquet"
    curve_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(curves).to_parquet(curve_path, engine="pyarrow", index=False)

    write_json(
        target,
        {
            "symbol": symbol,
            "scope": scope,
            "windows": [window.name for window in windows],
            "region_bars": len(region_bars),
            "v4_out_of_sample": oos,
            "continuous_replays": continuous,
            "equity_curves_parquet": str(curve_path),
        },
    )
    log(f"{symbol}: finalize({scope}) complete.")
    return symbol


# --------------------------------------------------------------------------
# stage driver
# --------------------------------------------------------------------------


def require_holdout_unlocked(output: Path) -> None:
    unlock = output / "holdout_unlock.json"
    if not unlock.exists():
        raise SystemExit(
            "The holdout stage is locked. Record the development conclusion first and "
            f"write {unlock} (with the conclusion and a timestamp) to unlock it."
        )
    unlocked_at = datetime.fromtimestamp(unlock.stat().st_mtime, UTC)
    log(f"holdout unlocked by {unlock} (mtime {unlocked_at})")


def classified_failure(stage: str, unit: str, error: BaseException) -> str:
    text = str(error)
    for cls in FAILURE_CLASSES:
        if text.startswith(cls):
            return cls
    if isinstance(error, (OSError, MemoryError)):
        return "INFRASTRUCTURE"
    if stage in ("prep",):
        return "DATA"
    if stage in ("score", "holdout-score"):
        return "MODEL"
    return "METRICS"


def drive(pool_args: list, worker, workers: int, stage: str) -> list[str]:
    total = len(pool_args)
    failures: list[str] = []
    started = time.perf_counter()
    log(f"stage {stage}: {total} unit(s) on {workers} worker(s).")
    if workers == 1:
        iterator = map(_safe(worker, stage), pool_args)
    else:
        pool = mp.get_context("spawn").Pool(processes=workers)
        iterator = pool.imap_unordered(_SafeWorker(worker, stage), pool_args)
    for done, outcome in enumerate(iterator, start=1):
        elapsed = time.perf_counter() - started
        eta = elapsed / done * (total - done)
        if outcome.startswith("FAILED"):
            failures.append(outcome)
            log(f"stage {stage}: {outcome}")
        log(
            f"stage {stage}: {done}/{total} units done "
            f"({done / total:.0%}), elapsed {elapsed / 60:.1f}m, ETA {eta / 60:.1f}m."
        )
    if workers > 1:
        pool.close()
        pool.join()
    return failures


class _SafeWorker:
    """A picklable wrapper that turns one unit's crash into a classified log line."""

    def __init__(self, worker, stage: str) -> None:
        self.worker = worker
        self.stage = stage

    def __call__(self, args):
        try:
            return self.worker(args)
        except BaseException as error:  # noqa: BLE001 - classified and reported
            cls = classified_failure(self.stage, str(args), error)
            traceback.print_exc()
            return f"FAILED [{cls}] {args[:2]}: {error}"


def _safe(worker, stage: str):
    return _SafeWorker(worker, stage)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ten-symbol full evaluation.")
    parser.add_argument(
        "--stage",
        required=True,
        choices=["prep", "score", "finalize", "holdout-score", "holdout-finalize"],
    )
    parser.add_argument("--datasets", default=os.environ.get("EQUITY_DATASETS", "."))
    parser.add_argument("--output", default=os.environ.get("STUDY_REPORTS", "."))
    parser.add_argument("--workers", type=int, default=MAX_WORKERS)
    parser.add_argument("--symbols", nargs="*", default=list(STUDY_SYMBOLS))
    arguments = parser.parse_args()

    output = Path(arguments.output)
    output.mkdir(parents=True, exist_ok=True)
    workers = max(1, min(arguments.workers, MAX_WORKERS))
    started = time.perf_counter()

    if arguments.stage == "prep":
        args = [(symbol, arguments.datasets, str(output)) for symbol in arguments.symbols]
        failures = drive(args, run_prep_symbol, min(workers, len(args)), "prep")
    elif arguments.stage == "score":
        args = [
            (symbol, window.name, arguments.datasets, str(output))
            for symbol in arguments.symbols
            for window in DEV_WINDOWS
        ]
        failures = drive(args, run_score_unit, min(workers, len(args)), "score")
    elif arguments.stage == "finalize":
        args = [(symbol, arguments.datasets, str(output), "dev") for symbol in arguments.symbols]
        failures = drive(args, run_finalize_symbol, min(workers, len(args)), "finalize")
    elif arguments.stage == "holdout-score":
        require_holdout_unlocked(output)
        args = [
            (symbol, HOLDOUT_WINDOW.name, arguments.datasets, str(output))
            for symbol in arguments.symbols
        ]
        failures = drive(args, run_score_unit, min(workers, len(args)), "holdout-score")
    else:
        require_holdout_unlocked(output)
        args = [(symbol, arguments.datasets, str(output), "full") for symbol in arguments.symbols]
        failures = drive(args, run_finalize_symbol, min(workers, len(args)), "holdout-finalize")

    log(f"stage {arguments.stage}: wall clock {(time.perf_counter() - started) / 60:.1f} minutes.")
    if failures:
        log(f"stage {arguments.stage}: {len(failures)} unit(s) FAILED; re-run to retry them.")
        raise SystemExit(1)
    log(f"stage {arguments.stage}: all units complete.")


if __name__ == "__main__":
    main()
