"""Phase-2 data engineering: download the expanded pool, audit it, build the
U30/U50 manifests (ledger §L3).

Downloads go to the program's own dataset directory; the ten incumbent frames
stay in the frozen `equity-historical` directory and are referenced read-only
(digests verified against the published build summary). Every new frame goes
through the validated pilot pipeline — split-adjusted download, de-duplicate,
regular-session filter against the broker calendar snapshot, VWAP re-null,
validation, fingerprint, provenance sidecar.

Usage:
    python -m studies.equity_eda1_nextgen.run_phase2_data --stage download
    python -m studies.equity_eda1_nextgen.run_phase2_data --stage manifest
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from studies.equity_10_full import CALENDAR_END, CALENDAR_START, DATA_END, DATA_START
from studies.equity_10_full.split_audit import audit_overnight_steps
from studies.equity_deep_arch.evaluate import write_json
from studies.equity_eda1_nextgen import NEXTGEN_DATASETS, REPORT_ROOT
from studies.equity_eda1_nextgen.universe import (
    DOWNLOAD_SYMBOLS,
    MAX_MISSING_FRACTION,
    build_manifests,
    liquidity_metric,
)
from studies.equity_eda1_nextgen.fetch import download_raw_any
from studies.equity_v1_v5.calendar import read_snapshot, snapshot_path
from studies.equity_v1_v5.dataset import (
    DatasetError,
    DatasetProvenance,
    describe_gaps,
    drop_duplicate_bars,
    file_sha256,
    filter_regular_session,
    frame_digest,
    renull_undefined_vwap,
    write_provenance,
)


def research_paths(datasets: Path, symbol: str) -> tuple[Path, Path]:
    """Where one research symbol's raw and session frames live. Same naming
    convention as the shipped `output_stem`, without its runtime whitelist."""
    stem = f"{symbol}_15m_{DATA_START.isoformat()}_{DATA_END.isoformat()}"
    return datasets / f"{stem}.raw.parquet", datasets / f"{stem}.session.parquet"


def build_research_frame(raw: pd.DataFrame, calendar, *, symbol: str, raw_digest: str):
    """The pilot's `build_evaluation_frame`, verbatim step-for-step, with the
    validation universe widened to the requested symbol (research data only —
    the runtime's ten-symbol whitelist is untouched)."""
    from autotrader.data.validation import validate_frame
    from autotrader.equity import EQUITY_TIMEFRAME, MARKET_TIMEZONE_NAME
    from autotrader.equity.data import FEED

    deduped, duplicates = drop_duplicate_bars(raw)
    regular, dropped = filter_regular_session(deduped, calendar)
    if regular.empty:
        raise DatasetError(f"{symbol} has no regular-session bars.")
    frame, renulled = renull_undefined_vwap(regular)
    gaps = describe_gaps(frame, calendar)
    validation = validate_frame(
        frame,
        supported_symbols=(symbol,),
        universe_label=f"nextgen-research:{symbol}",
    )
    provenance = DatasetProvenance(
        symbol=symbol,
        provider="alpaca",
        feed=FEED.value,
        asset_class="us_equity",
        adjustment="split",
        timeframe=EQUITY_TIMEFRAME,
        session_policy="regular-session-only (09:30-16:00 America/New_York, broker calendar)",
        date_timezone=MARKET_TIMEZONE_NAME,
        timestamp_timezone="UTC",
        requested_start=DATA_START.isoformat(),
        requested_end=DATA_END.isoformat(),
        first_bar_utc=frame["timestamp"].iloc[0].isoformat(),
        last_bar_utc=frame["timestamp"].iloc[-1].isoformat(),
        raw_rows=len(raw),
        regular_session_rows=len(frame),
        extended_hours_rows_dropped=dropped,
        duplicate_rows_dropped=duplicates,
        renulled_vwap_rows=renulled,
        gaps=gaps,
        validation_ok=validation.valid,
        validation_issues=tuple(f"{issue.code}: {issue.message}" for issue in validation.errors),
        raw_sha256=raw_digest,
        frame_sha256=frame_digest(frame),
        retrieved_at_utc=datetime.now(UTC).isoformat(),
    )
    return frame, provenance

FROZEN_DATASETS = Path("/Volumes/AUTOTRADER_QA/datasets/equity-historical")


def _log(message: str) -> None:
    print(f"[{datetime.now(UTC).isoformat(timespec='seconds')}] {message}", flush=True)


def ensure_calendar(datasets: Path) -> None:
    """Copy the validated calendar snapshot into the program directory (a copy,
    never a mutation of the frozen one)."""
    target = snapshot_path(datasets, CALENDAR_START, CALENDAR_END)
    if target.exists():
        return
    source = snapshot_path(FROZEN_DATASETS, CALENDAR_START, CALENDAR_END)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    _log(f"calendar snapshot copied to {target.name}")


def build_symbol_any(datasets: Path, symbol: str, calendar) -> dict[str, object]:
    """The ten-symbol study's `build_symbol`, with the research fetch instead
    of the runtime-whitelisted one. Pipeline steps are the shipped ones."""
    raw_file, session_file = research_paths(datasets, symbol)
    sidecar = session_file.with_suffix(".provenance.json")
    if session_file.exists() and sidecar.exists():
        provenance = json.loads(sidecar.read_text(encoding="utf-8"))
        frame = pd.read_parquet(session_file)
        digest = frame_digest(frame)
        if digest != provenance.get("frame_sha256"):
            raise ValueError(
                f"{symbol}: stored frame digest does not match its provenance sidecar."
            )
        _log(f"{symbol}: session frame exists, {len(frame)} rows, verified — skipping.")
        return {"symbol": symbol, "rows": len(frame), "frame_sha256": digest, "provenance": "cached"}

    started = time.perf_counter()
    _log(f"{symbol}: downloading {DATA_START}..{DATA_END} split-adjusted…")
    raw = download_raw_any([symbol], DATA_START, DATA_END)[symbol]
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    raw.to_parquet(raw_file, engine="pyarrow", index=False)
    raw_digest = file_sha256(raw_file)

    frame, provenance = build_research_frame(raw, calendar, symbol=symbol, raw_digest=raw_digest)
    if not provenance.validation_ok:
        raise ValueError(
            f"{symbol}: evaluation frame failed validation: "
            f"{list(provenance.validation_issues)}"
        )
    frame.to_parquet(session_file, engine="pyarrow", index=False)
    write_provenance(provenance, sidecar)
    _log(
        f"{symbol}: built {len(frame)} session rows "
        f"({provenance.gaps.missing_bars} missing) in {time.perf_counter() - started:.0f}s."
    )
    return {
        "symbol": symbol,
        "rows": len(frame),
        "frame_sha256": provenance.frame_sha256,
        "provenance": "downloaded",
    }


def run_download(datasets: Path, symbols: list[str]) -> None:
    datasets.mkdir(parents=True, exist_ok=True)
    ensure_calendar(datasets)
    calendar, _meta = read_snapshot(snapshot_path(datasets, CALENDAR_START, CALENDAR_END))

    summaries: list[dict[str, object]] = []
    failures: dict[str, str] = {}
    for symbol in symbols:
        try:
            summaries.append(build_symbol_any(datasets, symbol, calendar))
        except Exception as error:  # recorded, not fatal: exclusion is a manifest fact
            failures[symbol] = f"{type(error).__name__}: {error}"
            _log(f"{symbol}: FAILED — {failures[symbol]}")
    payload = {
        "built_at_utc": datetime.now(UTC).isoformat(),
        "data_start": DATA_START.isoformat(),
        "data_end": DATA_END.isoformat(),
        "requested": symbols,
        "built": summaries,
        "failures": failures,
    }
    write_json(Path(REPORT_ROOT) / "phase2" / "download_summary.json", payload)
    _log(f"download stage complete: {len(summaries)} built, {len(failures)} failed")


def _session_frame(datasets: Path, symbol: str) -> pd.DataFrame | None:
    files = sorted(datasets.glob(f"{symbol}_15m_*session.parquet"))
    if not files:
        return None
    return pd.read_parquet(files[0])


def run_manifest(datasets: Path) -> None:
    """Eligibility audit + deterministic U30/U50 manifests."""
    spy = _session_frame(FROZEN_DATASETS, "SPY")
    if spy is None:
        raise SystemExit("SPY frame missing from the frozen directory.")
    from autotrader.equity.session import market_date

    expected_sessions = len({market_date(ts.to_pydatetime()) for ts in spy["timestamp"]})
    expected_bars = len(spy)

    from studies.equity_eda1_nextgen.universe import (
        CONTEXT_ONLY,
        DOWNLOAD_SYMBOLS,
        INCUMBENTS,
    )

    eligibility: dict[str, dict[str, object]] = {}
    for symbol in sorted(set(INCUMBENTS) | set(DOWNLOAD_SYMBOLS)):
        directory = FROZEN_DATASETS if symbol in INCUMBENTS else datasets
        frame = _session_frame(directory, symbol)
        reasons: list[str] = []
        if frame is None:
            eligibility[symbol] = {
                "eligible": False,
                "liquidity": 0.0,
                "reasons": ["no session frame (download failed or symbol unavailable)"],
            }
            continue
        sessions = len({market_date(ts.to_pydatetime()) for ts in frame["timestamp"]})
        first_bar = str(frame["timestamp"].iloc[0])
        missing_fraction = max(0.0, 1.0 - len(frame) / expected_bars)
        if sessions < expected_sessions:
            reasons.append(
                f"covers {sessions}/{expected_sessions} sessions (listing/coverage gap)"
            )
        if missing_fraction >= MAX_MISSING_FRACTION:
            reasons.append(f"missing-bar fraction {missing_fraction:.4f} >= 0.01")
        eligibility[symbol] = {
            "eligible": not reasons,
            "liquidity": liquidity_metric(frame),
            "rows": len(frame),
            "sessions": sessions,
            "first_bar": first_bar,
            "missing_fraction": missing_fraction,
            "reasons": reasons,
        }

    split_audit = audit_overnight_steps(
        datasets, [s for s in DOWNLOAD_SYMBOLS if _session_frame(datasets, s) is not None]
    )

    manifests = build_manifests(
        {
            symbol: row
            for symbol, row in eligibility.items()
            if symbol not in CONTEXT_ONLY
        }
    )
    payload = {
        "built_at_utc": datetime.now(UTC).isoformat(),
        "classification": "FIXED CURRENT LIQUID UNIVERSE RESEARCH",
        "expected_sessions": expected_sessions,
        "expected_bars": expected_bars,
        "eligibility": eligibility,
        "context_only": list(CONTEXT_ONLY),
        "manifests": manifests,
        "overnight_step_audit": split_audit,
    }
    write_json(Path(REPORT_ROOT) / "phase2" / "universe_manifest.json", payload)
    _log(
        f"manifest stage complete: U30={manifests['u30_size']} U50={manifests['u50_size']}, "
        f"excluded={sorted(manifests['excluded'])}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("download", "manifest"))
    parser.add_argument("--datasets", type=Path, default=Path(NEXTGEN_DATASETS))
    parser.add_argument("--symbols", nargs="*", default=list(DOWNLOAD_SYMBOLS))
    arguments = parser.parse_args()

    started = time.perf_counter()
    if arguments.stage == "download":
        run_download(arguments.datasets, list(arguments.symbols))
    else:
        run_manifest(arguments.datasets)
    _log(f"stage {arguments.stage} complete in {time.perf_counter() - started:.0f}s")


if __name__ == "__main__":
    main()
