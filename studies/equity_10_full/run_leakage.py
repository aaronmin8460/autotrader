"""Causality audits: every engine, every symbol, probes inside the scored region.

The pilot found the shipped auditor silently vacuous at equity warm-up lengths
(every probe landed inside the 3,000-bar warm-up and compared ``() == ()``),
and its harness places probes strictly inside the scored region instead,
counts the decisions each probe actually covered, and fails a probe that
covered none. This runner drives that corrected auditor - unchanged - for all
five engines on all ten symbols, at this study's larger lookback.

The audit frame ends mid-way through the development region (the last bar of
w06), so the probed decisions are real scored-region decisions with the full
warm-up behind them. V4 and V5 are audited with the w07 cell's selected
artifact - the model that actually served the bars following that point.

    python -m studies.equity_10_full.run_leakage --workers 6 \
        --datasets <dir> --output <dir>
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import time
from pathlib import Path

from studies.equity_10_full import STUDY_SYMBOLS
from studies.equity_10_full.checkpoint import cell_path, is_complete, read_json, write_json
from studies.equity_10_full.run_study import load_frame, log
from studies.equity_10_full.windows import LOOKBACK_BARS, window_by_name
from studies.equity_v1_v5.adapters import LiveDecisionEngine
from studies.equity_v1_v5.leakage import audit_engine, audit_frame, summarize
from studies.equity_v1_v5.scoring import build_engines

#: The window whose LAST bar ends the audit frame, and the window whose model
#: serves the bars right after it. Mid-region on purpose: the probed decisions
#: are ordinary scored bars, not edge cases at either end of the study.
AUDIT_ANCHOR_WINDOW = "w06"
ARTIFACT_WINDOW = "w07"

#: Scored bars per audit frame and probes per engine, the harness defaults.
SCORED_BARS = 24
PROBES = 5


def run_leakage_symbol(args: tuple[str, str, str]) -> str:
    symbol, datasets_str, output_str = args
    datasets, output = Path(datasets_str), Path(output_str)
    target = cell_path(output, kind="leakage", symbol=symbol, unit="leakage")
    if is_complete(target):
        log(f"{symbol}: leakage checkpoint exists, skipping.")
        return symbol

    from autotrader.decision.probability import artifact_from_record

    frame = load_frame(datasets, symbol)
    anchor = window_by_name(AUDIT_ANCHOR_WINDOW)
    _, end_position = anchor.positions(frame)
    bars = audit_frame(
        frame, end_position=end_position, lookback_bars=LOOKBACK_BARS, scored_bars=SCORED_BARS
    )

    artifact_cell = read_json(cell_path(output, kind="cells", symbol=symbol, unit=ARTIFACT_WINDOW))
    artifact = artifact_from_record(artifact_cell["train"]["selected_artifact"])

    audits = []
    for spec in build_engines():
        started = time.perf_counter()
        engine = spec.build(symbol, artifact if spec.needs_model else None)
        adapter = LiveDecisionEngine(
            engine, name=spec.name, version=spec.version, lookback_bars=LOOKBACK_BARS
        )
        audit = audit_engine(adapter, bars, probes=PROBES, engine_name=spec.name, symbol=symbol)
        audits.append(audit)
        log(
            f"{symbol}/{spec.name}: causality {'PASS' if audit.ok else 'FAIL'} "
            f"({audit.changed} changed, {audit.vacuous_probes} vacuous, "
            f"{time.perf_counter() - started:.0f}s)"
        )

    payload = summarize(audits)
    payload["symbol"] = symbol
    payload["audit_anchor_window"] = AUDIT_ANCHOR_WINDOW
    payload["artifact_window"] = ARTIFACT_WINDOW
    payload["audit_frame_last_bar_utc"] = str(bars["timestamp"].iloc[-1])
    write_json(target, payload)
    if not payload["all_causal"]:
        raise RuntimeError(f"LEAKAGE {symbol}: causality audit failed; see {target}.")
    return symbol


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the causality audits.")
    parser.add_argument("--datasets", default=os.environ.get("EQUITY_DATASETS", "."))
    parser.add_argument("--output", default=os.environ.get("STUDY_REPORTS", "."))
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--symbols", nargs="*", default=list(STUDY_SYMBOLS))
    arguments = parser.parse_args()

    payload = [(symbol, arguments.datasets, arguments.output) for symbol in arguments.symbols]
    workers = max(1, min(arguments.workers, len(payload)))
    log(f"leakage: {len(payload)} symbol(s) on {workers} worker(s).")
    started = time.perf_counter()
    if workers == 1:
        for item in payload:
            run_leakage_symbol(item)
    else:
        with mp.get_context("spawn").Pool(processes=workers) as pool:
            for symbol in pool.imap_unordered(run_leakage_symbol, payload):
                log(f"leakage: finished {symbol}")
    log(f"leakage: wall clock {(time.perf_counter() - started) / 60:.1f} minutes.")


if __name__ == "__main__":
    main()
