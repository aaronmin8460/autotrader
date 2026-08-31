"""Acquire the pre-dataset fragment 2020-08-17..2021-01-03 for the w00 lock.

Read-only historical GETs through the repository's own market-data path,
split-adjusted, reduced through the identical session pipeline as the main
frames, fingerprinted with provenance sidecars. Governed by the search
ledger's w00 LOCK PROTOCOL: this data may be downloaded and validated at any
time, but no strategy result may be computed on bars before 2021-09-30 until
the program's final candidate is frozen.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from studies.equity_10_full import STUDY_SYMBOLS
from studies.equity_10_full.build_datasets import CALENDAR_END, CALENDAR_START
from studies.equity_v1_v5.calendar import read_snapshot, snapshot_path
from studies.equity_v1_v5.dataset import (
    build_evaluation_frame,
    download_raw,
    evaluation_path,
    file_sha256,
    raw_path,
    write_provenance,
)

FRAGMENT_START = date(2020, 8, 17)
FRAGMENT_END = date(2021, 1, 3)


def main() -> None:
    datasets = Path(
        os.environ.get("EQUITY_DATASETS", "/Volumes/AUTOTRADER_QA/datasets/equity-historical")
    )
    fragment_dir = datasets / "w00-fragment"
    fragment_dir.mkdir(parents=True, exist_ok=True)
    calendar, _meta = read_snapshot(snapshot_path(datasets, CALENDAR_START, CALENDAR_END))

    summaries = []
    for symbol in STUDY_SYMBOLS:
        session_file = evaluation_path(fragment_dir, symbol, FRAGMENT_START, FRAGMENT_END)
        sidecar = session_file.with_suffix(".provenance.json")
        if session_file.exists() and sidecar.exists():
            print(f"{symbol}: fragment exists, skipping", flush=True)
            continue
        started = time.perf_counter()
        raw = download_raw([symbol], FRAGMENT_START, FRAGMENT_END)[symbol]
        raw_file = raw_path(fragment_dir, symbol, FRAGMENT_START, FRAGMENT_END)
        raw.to_parquet(raw_file, engine="pyarrow", index=False)
        frame, provenance = build_evaluation_frame(
            raw,
            calendar,
            symbol=symbol,
            start=FRAGMENT_START,
            end=FRAGMENT_END,
            raw_digest=file_sha256(raw_file),
            retrieved_at=datetime.now(UTC),
        )
        frame.to_parquet(session_file, engine="pyarrow", index=False)
        write_provenance(provenance, sidecar)
        summaries.append(
            {
                "symbol": symbol,
                "rows": len(frame),
                "missing": provenance.gaps.missing_bars,
                "validation_ok": provenance.validation_ok,
                "frame_sha256": provenance.frame_sha256,
            }
        )
        print(
            f"{symbol}: {len(frame)} session rows, {provenance.gaps.missing_bars} missing, "
            f"valid={provenance.validation_ok}, {time.perf_counter() - started:.0f}s",
            flush=True,
        )

    manifest = fragment_dir / "fragment_manifest.json"
    manifest.write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    print("fragment acquisition complete", flush=True)
    first = pd.read_parquet(evaluation_path(fragment_dir, "SPY", FRAGMENT_START, FRAGMENT_END))[
        "timestamp"
    ]
    print(f"SPY fragment span: {first.iloc[0]} .. {first.iloc[-1]}")


if __name__ == "__main__":
    main()
