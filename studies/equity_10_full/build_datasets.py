"""Build the ten evaluation frames: reuse the pilot's two, download the other eight.

Every frame goes through the pilot's own pipeline - download split-adjusted,
de-duplicate, filter to regular session by the broker calendar, re-null the
undefined-VWAP sentinel, validate, fingerprint - because that pipeline is the
one the pilot proved. Nothing here reimplements a step of it.

**SPY and QQQ are reused, not re-downloaded.** The pilot proved their stored
frames byte-identical to split-adjusted re-downloads and published their
digests; this runner verifies those digests and refuses a frame that fails.

**The other eight are downloaded split-adjusted**, one symbol at a time so an
interrupted build resumes at the symbol boundary. A symbol whose session frame
and provenance sidecar already exist is skipped.

Run as a module so the ``studies`` package resolves::

    python -m studies.equity_10_full.build_datasets --datasets <dir>
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from studies.equity_10_full import (
    CALENDAR_END,
    CALENDAR_START,
    DATA_END,
    DATA_START,
    PILOT_BUILT_SYMBOLS,
    PILOT_FRAME_SHA256,
    STUDY_SYMBOLS,
)
from studies.equity_10_full.split_audit import audit_overnight_steps
from studies.equity_v1_v5.calendar import read_snapshot, snapshot_path
from studies.equity_v1_v5.dataset import (
    DatasetError,
    build_evaluation_frame,
    download_raw,
    evaluation_path,
    file_sha256,
    frame_digest,
    raw_path,
    write_provenance,
)


class BuildError(Exception):
    """A dataset that cannot be built or verified under the study's rules."""


def _log(message: str) -> None:
    print(f"[{datetime.now(UTC).isoformat(timespec='seconds')}] {message}", flush=True)


def verify_pilot_frame(datasets: Path, symbol: str) -> dict[str, object]:
    """Verify one reused pilot frame against its pinned digest."""
    path = evaluation_path(datasets, symbol, DATA_START, DATA_END)
    if not path.exists():
        raise BuildError(f"The pilot frame for {symbol} is missing at {path}.")
    frame = pd.read_parquet(path)
    digest = frame_digest(frame)
    expected = PILOT_FRAME_SHA256[symbol]
    if digest != expected:
        raise BuildError(
            f"{symbol}: stored frame digest {digest} does not match the pilot's "
            f"published {expected}. The frame cannot be reused."
        )
    _log(f"{symbol}: pilot frame verified, {len(frame)} rows, sha256 {digest[:16]}…")
    return {"symbol": symbol, "rows": len(frame), "frame_sha256": digest, "provenance": "pilot"}


def build_symbol(datasets: Path, symbol: str, calendar) -> dict[str, object]:
    """Download and reduce one symbol, or load its finished checkpoint."""
    session_file = evaluation_path(datasets, symbol, DATA_START, DATA_END)
    sidecar = session_file.with_suffix(".provenance.json")
    if session_file.exists() and sidecar.exists():
        provenance = json.loads(sidecar.read_text(encoding="utf-8"))
        frame = pd.read_parquet(session_file)
        digest = frame_digest(frame)
        if digest != provenance.get("frame_sha256"):
            raise BuildError(
                f"{symbol}: stored frame digest {digest} does not match its own "
                f"provenance sidecar. Delete both files deliberately to rebuild."
            )
        _log(f"{symbol}: session frame exists, {len(frame)} rows, verified — skipping download.")
        return {
            "symbol": symbol,
            "rows": len(frame),
            "frame_sha256": digest,
            "provenance": "cached",
        }

    started = time.perf_counter()
    _log(f"{symbol}: downloading {DATA_START}..{DATA_END} split-adjusted…")

    def progress(chunk_start, chunk_end, counts) -> None:
        _log(f"  {symbol} {chunk_start}..{chunk_end}: {counts.get(symbol, 0)} bars")

    raw = download_raw([symbol], DATA_START, DATA_END, progress=progress)[symbol]
    raw_file = raw_path(datasets, symbol, DATA_START, DATA_END)
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    raw.to_parquet(raw_file, engine="pyarrow", index=False)
    raw_digest = file_sha256(raw_file)

    frame, provenance = build_evaluation_frame(
        raw,
        calendar,
        symbol=symbol,
        start=DATA_START,
        end=DATA_END,
        raw_digest=raw_digest,
        retrieved_at=datetime.now(UTC),
    )
    if not provenance.validation_ok:
        raise BuildError(
            f"{symbol}: the evaluation frame failed validation: "
            f"{list(provenance.validation_issues)}"
        )
    frame.to_parquet(session_file, engine="pyarrow", index=False)
    write_provenance(provenance, sidecar)
    _log(
        f"{symbol}: built {len(frame)} session rows "
        f"({provenance.gaps.missing_bars} missing, "
        f"{provenance.extended_hours_rows_dropped} extended dropped) in "
        f"{time.perf_counter() - started:.0f}s."
    )
    return {
        "symbol": symbol,
        "rows": len(frame),
        "frame_sha256": provenance.frame_sha256,
        "provenance": "downloaded",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the ten-symbol evaluation datasets.")
    parser.add_argument("--datasets", default=os.environ.get("EQUITY_DATASETS", "."))
    parser.add_argument("--symbols", nargs="*", default=list(STUDY_SYMBOLS))
    parser.add_argument("--audit-output", default=None)
    arguments = parser.parse_args()

    datasets = Path(arguments.datasets)
    calendar, _meta = read_snapshot(snapshot_path(datasets, CALENDAR_START, CALENDAR_END))

    summaries: list[dict[str, object]] = []
    for symbol in arguments.symbols:
        if symbol in PILOT_BUILT_SYMBOLS:
            summaries.append(verify_pilot_frame(datasets, symbol))
        else:
            try:
                summaries.append(build_symbol(datasets, symbol, calendar))
            except DatasetError as error:
                raise BuildError(f"{symbol}: {error}") from error

    audit = audit_overnight_steps(datasets, [str(entry["symbol"]) for entry in summaries])
    payload = {
        "built_at_utc": datetime.now(UTC).isoformat(),
        "data_start": DATA_START.isoformat(),
        "data_end": DATA_END.isoformat(),
        "symbols": summaries,
        "overnight_step_audit": audit,
    }
    output = (
        Path(arguments.audit_output)
        if arguments.audit_output
        else datasets / "ten_symbol_build_summary.json"
    )
    output.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    _log(f"wrote {output}")


if __name__ == "__main__":
    main()
