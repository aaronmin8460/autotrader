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
        started = time.time()
        collected: list[pd.DataFrame] = []
        with mp.Pool(processes=args.workers, initializer=_init_worker, initargs=(paths,)) as pool:
            for done, frame in enumerate(pool.imap_unordered(_run_chunk, chunks), start=1):
                collected.append(frame)
                if done % 20 == 0 or done == len(chunks):
                    elapsed = time.time() - started
                    rate = done / elapsed
                    print(
                        f"    {done}/{len(chunks)} chunks  {elapsed / 60:.1f}m elapsed  "
                        f"eta {(len(chunks) - done) / rate / 60:.1f}m",
                        flush=True,
                    )
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
