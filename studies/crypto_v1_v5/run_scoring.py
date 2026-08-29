"""Run the walk-forward training and the five-engine scoring pass. Writes external artifacts only.

This is the expensive half of the study and the only part that takes real time.
It trains one V4 per out-of-sample window per symbol, then asks all five engines
about every bar of every window under the model that window's past produced.

Nothing here reaches a broker, opens a socket, reads a credential, or writes
inside the repository. Its outputs are a decision series, a set of model
artifacts, and the records that identify both.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from studies.crypto_v1_v5.dataset import load_evaluation_frame
from studies.crypto_v1_v5.scoring import (
    SHARED_LOOKBACK_BARS,
    ScoringChunk,
    plan_chunks,
    score_chunk,
)
from studies.crypto_v1_v5.walkforward import fit_fold_model, grade_fold, plan_folds

#: Decision instants per parallel task. Small enough to spread unevenly-sized
#: folds across workers, large enough that process overhead stays negligible.
CHUNK_SIZE = 240

_BARS: dict[str, pd.DataFrame] = {}


def _init_worker(paths: dict[str, str]) -> None:
    """Load each symbol's bars once per process rather than once per task."""
    for symbol, path in paths.items():
        frame, _ = load_evaluation_frame(Path(path))
        _BARS[symbol] = frame


def _run_chunk(chunk: ScoringChunk) -> pd.DataFrame:
    return score_chunk(_BARS[chunk.symbol], chunk, lookback_bars=SHARED_LOOKBACK_BARS)


def _checkpoint_path(directory: Path, chunk: ScoringChunk) -> Path:
    """Where one chunk's result is stored, named by exactly what it covers.

    Symbol, fold and the decision range identify a chunk completely, so a
    resumed run recognises finished work by name rather than by trusting a
    separate index that could disagree with the files beside it.
    """
    slug = chunk.symbol.replace("/", "_")
    return directory / (
        f"{slug}_{chunk.fold_id}_{chunk.first_decision_index}-{chunk.last_decision_index}.parquet"
    )


def dataset_paths(root: Path) -> dict[str, str]:
    return {
        "BTC/USD": str(root / "BTC_USD_15m_2024-01-01_2026-08-28.parquet"),
        "ETH/USD": str(root / "ETH_USD_15m_2024-01-01_2026-08-28.parquet"),
    }


def train_all_folds(
    frames: dict[str, pd.DataFrame],
    *,
    oos_start: pd.Timestamp,
    holdout_windows: int,
    out_dir: Path,
    variants: Sequence[str],
) -> dict[tuple[str, str, str], dict]:
    """Train every fold for every symbol, for each requested variant."""
    trained: dict[tuple[str, str, str], dict] = {}
    records: list[dict] = []
    stamp = datetime(2026, 8, 29, tzinfo=UTC)
    for symbol, frame in frames.items():
        plans = plan_folds(
            oos_start=oos_start,
            oos_end=frame["timestamp"].iloc[-1],
            dataset_start=frame["timestamp"].iloc[0],
            holdout_windows=holdout_windows,
        )
        for plan in plans:
            started = time.time()
            evidence = grade_fold(frame, plan, symbol=symbol)
            for variant in variants:
                model = fit_fold_model(
                    evidence, force_family=(variant == "forced"), trained_at=stamp
                )
                trained[(symbol, plan.fold_id, variant)] = {
                    "artifact_record": model.artifact.to_record(),
                    "model": model,
                }
                records.append({"variant": variant, **model.to_record()})
            print(
                f"  trained {symbol} {plan.fold_id} "
                f"({', '.join(variants)}) in {time.time() - started:.0f}s",
                flush=True,
            )
    (out_dir / "v4_walkforward_folds.json").write_text(json.dumps(records, indent=2, default=str))
    return trained


def load_all_folds(
    frames: dict[str, pd.DataFrame],
    *,
    oos_start: pd.Timestamp,
    holdout_windows: int,
    out_dir: Path,
    variants: Sequence[str],
) -> dict[tuple[str, str, str], dict]:
    """Reuse the models a previous run already fitted, instead of fitting them again.

    Training is deterministic here - every seed is zero and `trained_at` is a
    fixed stamp - so refitting would reproduce these artifacts byte for byte and
    the only thing it would produce is delay. Reuse is therefore a shortcut past
    repetition rather than a shortcut past rigour.

    Every artifact named by the plan must be present. A missing one is an error
    rather than a silent refit, because a run that quietly retrained one fold and
    reused six would be scoring against a model set nobody can identify.
    """
    directory = out_dir / "artifacts"
    trained: dict[tuple[str, str, str], dict] = {}
    for symbol, frame in frames.items():
        plans = plan_folds(
            oos_start=oos_start,
            oos_end=frame["timestamp"].iloc[-1],
            dataset_start=frame["timestamp"].iloc[0],
            holdout_windows=holdout_windows,
        )
        for plan in plans:
            for variant in variants:
                path = directory / f"{symbol.replace('/', '_')}_{plan.fold_id}_{variant}.json"
                if not path.is_file():
                    raise FileNotFoundError(
                        f"--reuse-artifacts was given but {path} is missing. Re-run without "
                        "the flag to fit the models, rather than scoring some folds against "
                        "stored models and others against fresh ones."
                    )
                trained[(symbol, plan.fold_id, variant)] = {
                    "artifact_record": json.loads(path.read_text())
                }
    print(f"reused {len(trained)} stored model artifacts (no retraining)", flush=True)
    return trained


def build_chunks(
    frames: dict[str, pd.DataFrame],
    trained: dict[tuple[str, str, str], dict],
    *,
    variant: str,
    oos_start: pd.Timestamp,
    holdout_windows: int,
) -> list[ScoringChunk]:
    chunks: list[ScoringChunk] = []
    for symbol, frame in frames.items():
        plans = plan_folds(
            oos_start=oos_start,
            oos_end=frame["timestamp"].iloc[-1],
            dataset_start=frame["timestamp"].iloc[0],
            holdout_windows=holdout_windows,
        )
        timestamps = frame["timestamp"]
        for plan in plans:
            inside = timestamps[(timestamps >= plan.test_start) & (timestamps <= plan.test_end)]
            if inside.empty:
                continue
            first = int(inside.index[0])
            last = int(inside.index[-1])
            if first < SHARED_LOOKBACK_BARS - 1:
                first = SHARED_LOOKBACK_BARS - 1
            chunks.extend(
                plan_chunks(
                    symbol,
                    first_decision_index=first,
                    last_decision_index=last,
                    artifact_record=trained[(symbol, plan.fold_id, variant)]["artifact_record"],
                    fold_id=plan.fold_id,
                    chunk_size=CHUNK_SIZE,
                )
            )
    return chunks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--oos-start", default="2025-01-01")
    parser.add_argument("--holdout-windows", type=int, default=1)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--variants", default="selected,forced")
    parser.add_argument("--score-variants", default="selected")
    parser.add_argument(
        "--reuse-artifacts",
        action="store_true",
        help=(
            "Score against the models a previous run already fitted instead of fitting "
            "them again. Training is deterministic, so this skips repetition rather than "
            "rigour; every artifact the plan names must already be present."
        ),
    )
    args = parser.parse_args()

    datasets = Path(args.datasets)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    oos_start = pd.Timestamp(args.oos_start, tz="UTC")
    variants = tuple(v.strip() for v in args.variants.split(",") if v.strip())
    score_variants = tuple(v.strip() for v in args.score_variants.split(",") if v.strip())

    paths = dataset_paths(datasets)
    frames: dict[str, pd.DataFrame] = {}
    provenance: list[dict] = []
    for symbol, path in paths.items():
        frame, record = load_evaluation_frame(Path(path))
        frames[symbol] = frame
        provenance.append(record.to_record())
    (out_dir / "dataset_provenance.json").write_text(json.dumps(provenance, indent=2))
    print(f"loaded {len(frames)} datasets", flush=True)

    if args.reuse_artifacts:
        trained = load_all_folds(
            frames,
            oos_start=oos_start,
            holdout_windows=args.holdout_windows,
            out_dir=out_dir,
            variants=variants,
        )
    else:
        print("training walk-forward folds...", flush=True)
        trained = train_all_folds(
            frames,
            oos_start=oos_start,
            holdout_windows=args.holdout_windows,
            out_dir=out_dir,
            variants=variants,
        )
        artifacts_dir = out_dir / "artifacts"
        artifacts_dir.mkdir(exist_ok=True)
        for (symbol, fold_id, variant), payload in trained.items():
            name = f"{symbol.replace('/', '_')}_{fold_id}_{variant}.json"
            (artifacts_dir / name).write_text(json.dumps(payload["artifact_record"], indent=2))

    for variant in score_variants:
        chunks = build_chunks(
            frames,
            trained,
            variant=variant,
            oos_start=oos_start,
            holdout_windows=args.holdout_windows,
        )
        total = sum(chunk.decision_count for chunk in chunks)
        print(
            f"scoring variant={variant}: {len(chunks)} chunks, {total} decision instants, "
            f"{args.workers} workers",
            flush=True,
        )
        # Each finished chunk is written before the next is awaited, so an
        # interruption costs the chunks in flight rather than the whole pass.
        # The previous run held every result in memory until the variant
        # finished and lost 100 completed chunks to a shutdown; that is the
        # entire reason this directory exists.
        checkpoints = out_dir / "chunks" / variant
        checkpoints.mkdir(parents=True, exist_ok=True)
        pending = [chunk for chunk in chunks if not _checkpoint_path(checkpoints, chunk).is_file()]
        resumed = len(chunks) - len(pending)
        if resumed:
            print(f"  resuming: {resumed} chunks already on disk, {len(pending)} to go", flush=True)

        started = time.time()
        if pending:
            with mp.Pool(
                processes=args.workers, initializer=_init_worker, initargs=(paths,)
            ) as pool:
                for done, (chunk, frame) in enumerate(
                    zip(pending, pool.imap(_run_chunk, pending), strict=False), start=1
                ):
                    frame.to_parquet(_checkpoint_path(checkpoints, chunk), index=False)
                    if done % 20 == 0 or done == len(pending):
                        elapsed = time.time() - started
                        rate = done / elapsed
                        print(
                            f"    {done}/{len(pending)} chunks  {elapsed / 60:.1f}m elapsed  "
                            f"eta {(len(pending) - done) / rate / 60:.1f}m",
                            flush=True,
                        )

        collected = [pd.read_parquet(_checkpoint_path(checkpoints, chunk)) for chunk in chunks]
        decisions = pd.concat(collected, ignore_index=True)
        decisions = decisions.sort_values(["symbol", "engine", "timestamp"]).reset_index(drop=True)
        target = out_dir / f"decisions_{variant}.parquet"
        decisions.to_parquet(target, index=False)
        print(
            f"  wrote {len(decisions)} decision rows to {target} "
            f"in {(time.time() - started) / 60:.1f}m",
            flush=True,
        )


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    main()
