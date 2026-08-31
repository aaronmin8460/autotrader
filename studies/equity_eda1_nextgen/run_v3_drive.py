"""Drive the shipped V3 engine over expanded-universe symbols (ledger §L3).

The decision path validates symbols against the frozen production universe —
a runtime invariant this program must not weaken — so research frames are
scored under a whitelisted **alias**: the frame's symbol column is rewritten
to SPY, the engine (which is built from `EQUITY_POLICY`, identical for all
ten) scores the bars, and the resulting records are relabelled back. The
alias can influence nothing but the label, which `--stage wiring` proves by
scoring QQQ's frozen frame under the alias and demanding whole-record
identity with its stored series.

Scoring is per (symbol × window), resumable, embarrassingly parallel.

Usage:
    python -m studies.equity_eda1_nextgen.run_v3_drive --stage wiring
    python -m studies.equity_eda1_nextgen.run_v3_drive --stage drive --symbols XLK XLF …
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from studies.equity_10_full.windows import FULL_WINDOWS
from studies.equity_deep_arch.evaluate import write_json
from studies.equity_eda1_nextgen import NEXTGEN_DATASETS, REPORT_ROOT

ALIAS = "SPY"
LOOKBACK_BARS = 4750
DECISIONS_DIR = Path(NEXTGEN_DATASETS) / "v3-decisions"


def _load_frame(symbol: str) -> pd.DataFrame:
    files = sorted(Path(NEXTGEN_DATASETS).glob(f"{symbol}_15m_*session.parquet"))
    if not files:
        files = sorted(
            Path("/Volumes/AUTOTRADER_QA/datasets/equity-historical").glob(
                f"{symbol}_15m_*session.parquet"
            )
        )
    if len(files) != 1:
        raise SystemExit(f"Expected one session frame for {symbol}, found {files}.")
    return pd.read_parquet(files[0])


def _score_cell(task: tuple[str, str]) -> str:
    """Score one (symbol, window) cell under the alias; store relabelled."""
    symbol, window_name = task
    from studies.equity_v1_v5.scoring import build_engines, decisions_to_frame, score_window

    target = DECISIONS_DIR / f"{symbol}_{window_name}_V3.parquet"
    if target.exists():
        return f"{symbol}/{window_name}: exists"

    frame = _load_frame(symbol)
    aliased = frame.copy()
    aliased["symbol"] = ALIAS
    window = next(w for w in FULL_WINDOWS if w.name == window_name)

    first, _last = window.positions(aliased)
    if first < LOOKBACK_BARS - 1:
        return f"{symbol}/{window_name}: SKIPPED insufficient warm-up ({first + 1} bars precede)"

    spec = next(s for s in build_engines() if s.name == "V3")
    started = time.perf_counter()
    records = score_window(
        aliased, window, spec, symbol=ALIAS, artifact=None, lookback_bars=LOOKBACK_BARS
    )
    insufficient = sum(
        1
        for record in records
        if any("INSUFFICIENT" in reason or "FEATURE_UNAVAILABLE" in reason for reason in record.reasons)
    )
    stored = decisions_to_frame(records)
    stored["symbol"] = symbol
    DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp.parquet")
    stored.to_parquet(tmp, engine="pyarrow", index=False)
    tmp.rename(target)
    flag = f" INSUFFICIENT={insufficient}" if insufficient else ""
    return (
        f"{symbol}/{window_name}: {len(records)} bars in "
        f"{time.perf_counter() - started:.0f}s{flag}"
    )


def run_wiring(sample_windows: tuple[str, ...] = ("w05",)) -> None:
    """QQQ's frozen frame under the alias must reproduce its stored series."""
    from studies.equity_v1_v5.scoring import build_engines, frame_to_decisions, score_window

    frozen = Path("/Volumes/AUTOTRADER_QA/datasets/equity-historical")
    decisions = Path("/Volumes/AUTOTRADER_QA/reports/equity-10-symbol-full/decisions")
    frame = pd.read_parquet(sorted(frozen.glob("QQQ_15m_*session.parquet"))[0])
    aliased = frame.copy()
    aliased["symbol"] = ALIAS
    spec = next(s for s in build_engines() if s.name == "V3")

    report: dict[str, object] = {"alias": ALIAS, "windows": {}}
    for window_name in sample_windows:
        window = next(w for w in FULL_WINDOWS if w.name == window_name)
        records = score_window(
            aliased, window, spec, symbol=ALIAS, artifact=None, lookback_bars=LOOKBACK_BARS
        )
        stored = frame_to_decisions(pd.read_parquet(decisions / f"QQQ_{window_name}_V3.parquet"))
        if len(records) != len(stored):
            raise SystemExit(
                f"wiring FAIL: {window_name} scored {len(records)} bars vs stored {len(stored)}."
            )
        mismatches = 0
        for mine, theirs in zip(records, stored, strict=True):
            same = (
                mine.timestamp == theirs.timestamp
                and mine.signal == theirs.signal
                and round(mine.score, 9) == round(theirs.score, 9)
                and mine.confidence == theirs.confidence
                and mine.regime == theirs.regime
                and tuple(mine.reasons) == tuple(theirs.reasons)
            )
            if not same:
                mismatches += 1
        report["windows"][window_name] = {"bars": len(records), "mismatches": mismatches}
        if mismatches:
            raise SystemExit(f"wiring FAIL: {mismatches} mismatched records on {window_name}.")
    report["wiring_check"] = "PASS"
    write_json(Path(REPORT_ROOT) / "phase2" / "v3_alias_wiring.json", report)
    print("v3 alias wiring: PASS", flush=True)


def run_drive(symbols: list[str], workers: int) -> None:
    tasks = [(symbol, window.name) for symbol in symbols for window in FULL_WINDOWS]
    pending = [
        task
        for task in tasks
        if not (DECISIONS_DIR / f"{task[0]}_{task[1]}_V3.parquet").exists()
    ]
    print(f"{len(pending)} cells to score across {len(symbols)} symbols", flush=True)
    results: list[str] = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for line in pool.map(_score_cell, pending):
            print(line, flush=True)
            results.append(line)
    summary = {
        "symbols": symbols,
        "lookback_bars": LOOKBACK_BARS,
        "alias": ALIAS,
        "results": results,
    }
    write_json(Path(REPORT_ROOT) / "phase2" / "v3_drive_log.json", summary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("wiring", "drive"))
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--workers", type=int, default=5)
    arguments = parser.parse_args()

    if arguments.stage == "wiring":
        run_wiring()
    else:
        if not arguments.symbols:
            raise SystemExit("--symbols is required for drive.")
        run_drive(list(arguments.symbols), arguments.workers)


if __name__ == "__main__":
    main()
