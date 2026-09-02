"""Alias-scored V3 drive for the U45∖U30 symbols (ledger §L11 data prep).

The asset-character program's validated drive mechanism, with this program's
artifact isolation: new decisions land in THIS program's dataset root; the
21 cells the prior program already completed are honoured in place (its
directory stays read-only). Stance-data computation only — produces no
result and gates nothing.

Usage:
    python -m studies.equity_two_sleeve.run_v3_drive --stage wiring
    python -m studies.equity_two_sleeve.run_v3_drive --stage drive --workers 5
"""

from __future__ import annotations

import argparse
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from studies.equity_10_full.windows import FULL_WINDOWS
from studies.equity_asset_character.run_v3_drive import (
    ALIAS,
    DRIVE_SYMBOLS,
    LOOKBACK_BARS,
    _load_frame,
)
from studies.equity_asset_character.run_v3_drive import (
    DECISIONS_DIR as PRIOR_DECISIONS_DIR,
)
from studies.equity_deep_arch.evaluate import write_json
from studies.equity_two_sleeve import REPORT_ROOT, TWO_SLEEVE_DATASETS

DECISIONS_DIR = Path(TWO_SLEEVE_DATASETS) / "v3-decisions"


def _cell_exists(symbol: str, window_name: str) -> bool:
    name = f"{symbol}_{window_name}_V3.parquet"
    return (DECISIONS_DIR / name).exists() or (PRIOR_DECISIONS_DIR / name).exists()


def _score_cell(task: tuple[str, str]) -> str:
    symbol, window_name = task
    from studies.equity_v1_v5.scoring import build_engines, decisions_to_frame, score_window

    if _cell_exists(symbol, window_name):
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
    stored = decisions_to_frame(records)
    stored["symbol"] = symbol
    DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
    target = DECISIONS_DIR / f"{symbol}_{window_name}_V3.parquet"
    tmp = target.with_suffix(".tmp.parquet")
    stored.to_parquet(tmp, engine="pyarrow", index=False)
    tmp.rename(target)
    return f"{symbol}/{window_name}: {len(records)} bars in {time.perf_counter() - started:.0f}s"


def run_wiring() -> None:
    """QQQ's frozen frame under the alias must reproduce its stored series —
    the same proof the prior programs ran, re-proven from this worktree."""
    from studies.equity_v1_v5.scoring import build_engines, frame_to_decisions, score_window

    frozen = Path("/Volumes/AUTOTRADER_QA/datasets/equity-historical")
    decisions = Path("/Volumes/AUTOTRADER_QA/reports/equity-10-symbol-full/decisions")
    frame = pd.read_parquet(sorted(frozen.glob("QQQ_15m_*session.parquet"))[0])
    aliased = frame.copy()
    aliased["symbol"] = ALIAS
    spec = next(s for s in build_engines() if s.name == "V3")

    window = next(w for w in FULL_WINDOWS if w.name == "w05")
    records = score_window(
        aliased, window, spec, symbol=ALIAS, artifact=None, lookback_bars=LOOKBACK_BARS
    )
    stored = frame_to_decisions(pd.read_parquet(decisions / "QQQ_w05_V3.parquet"))
    if len(records) != len(stored):
        raise SystemExit(f"wiring FAIL: scored {len(records)} bars vs stored {len(stored)}.")
    mismatches = sum(
        1
        for mine, theirs in zip(records, stored, strict=True)
        if not (
            mine.timestamp == theirs.timestamp
            and mine.signal == theirs.signal
            and round(mine.score, 9) == round(theirs.score, 9)
            and mine.confidence == theirs.confidence
            and mine.regime == theirs.regime
            and tuple(mine.reasons) == tuple(theirs.reasons)
        )
    )
    if mismatches:
        raise SystemExit(f"wiring FAIL: {mismatches} mismatched records on w05.")
    write_json(
        Path(REPORT_ROOT) / "drive" / "v3_alias_wiring.json",
        {
            "alias": ALIAS,
            "windows": {"w05": {"bars": len(records), "mismatches": 0}},
            "wiring_check": "PASS",
        },
    )
    print("v3 alias wiring: PASS", flush=True)


def run_drive(symbols: list[str], workers: int) -> None:
    tasks = [(symbol, window.name) for symbol in symbols for window in FULL_WINDOWS]
    pending = [task for task in tasks if not _cell_exists(*task)]
    print(f"{len(pending)} cells to score across {len(symbols)} symbols", flush=True)
    results: list[str] = []
    started = time.perf_counter()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for index, line in enumerate(pool.map(_score_cell, pending), start=1):
            elapsed = time.perf_counter() - started
            pace = index / (elapsed / 3600.0) if elapsed else 0.0
            print(f"[{index}/{len(pending)} {pace:.1f} cells/h] {line}", flush=True)
            results.append(line)
    write_json(
        Path(REPORT_ROOT) / "drive" / "v3_drive_log.json",
        {
            "symbols": symbols,
            "lookback_bars": LOOKBACK_BARS,
            "alias": ALIAS,
            "results": results,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("wiring", "drive"))
    parser.add_argument("--symbols", nargs="*", default=list(DRIVE_SYMBOLS))
    parser.add_argument("--workers", type=int, default=5)
    arguments = parser.parse_args()

    if arguments.stage == "wiring":
        run_wiring()
    else:
        run_drive(list(arguments.symbols), arguments.workers)


if __name__ == "__main__":
    main()
