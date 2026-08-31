"""The locked w00 attack: score the frozen challenger on the never-inspected
2021-05..2021-09 window.

Governed by the ledger's w00 LOCK PROTOCOL. This runner REFUSES to run unless
`w00_unlock.json` exists beside the ledger and records the digest of the
ledger at freeze time — the freeze block, with its predeclared pass/fail
criteria, must be written before the first w00 bar is scored. One run,
consumed forever.

Stages (checkpointed, resume-safe):
1. stitch fragment + main frames; derive the w00 window empirically from the
   4,750-bar warm-up on the stitched frames;
2. score V3 live on every w00 bar per symbol (the stored series do not cover
   w00); assert zero INSUFFICIENT_* reasons;
3. build the EDA-1 overlay from the stitched-close participation series;
4. replay challenger / V3 / buy-and-hold under all three cost models, paired.

Usage:
    python -m studies.equity_deep_arch.run_w00
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd

from autotrader.data.validation import EQUITY_UNIVERSE_LABEL
from autotrader.equity import EQUITY_SYMBOLS
from autotrader.equity.session import market_date
from autotrader.research.metrics import EQUITY_15M, metrics_for_replay
from autotrader.research.replay import ReplayConfig, replay_portfolio
from studies.equity_10_full import STUDY_SYMBOLS
from studies.equity_10_full.benchmarks import BuyAndHoldEngine
from studies.equity_10_full.windows import LOOKBACK_BARS
from studies.equity_deep_arch.build_w00 import FRAGMENT_END, FRAGMENT_START
from studies.equity_deep_arch.evaluate import write_json
from studies.equity_deep_arch.overlay import participation_overlay
from studies.equity_deep_arch.run_eda1 import ARCHITECTURE, default_datasets
from studies.equity_deep_arch.state import (
    ParticipationSpec,
    participation_series,
    per_bar_participation,
    session_closes,
)
from studies.equity_v1_v5.adapters import DecisionSeriesEngine
from studies.equity_v1_v5.dataset import evaluation_path
from studies.equity_v1_v5.scoring import (
    COST_MODELS,
    INITIAL_CASH,
    build_engines,
    decisions_to_frame,
    frame_to_decisions,
    insufficient_history_count,
    score_window,
)
from studies.equity_v1_v5.windows import ScoringWindow

OUTPUT = Path("/Volumes/AUTOTRADER_QA/reports/equity-deep-architecture/w00")
UNLOCK = Path("/Volumes/AUTOTRADER_QA/reports/equity-deep-architecture/w00_unlock.json")

W00_END = date(2021, 9, 29)


def stitched_frame(datasets: Path, symbol: str) -> pd.DataFrame:
    fragment = pd.read_parquet(
        evaluation_path(datasets / "w00-fragment", symbol, FRAGMENT_START, FRAGMENT_END)
    )
    main = pd.read_parquet(sorted(datasets.glob(f"{symbol}_15m_*session.parquet"))[0])
    if fragment["timestamp"].iloc[-1] >= main["timestamp"].iloc[0]:
        raise RuntimeError(f"{symbol}: fragment overlaps the main frame.")
    return pd.concat([fragment, main], ignore_index=True)


def w00_window(frames: dict[str, pd.DataFrame]) -> ScoringWindow:
    """First scored session = the latest, across symbols, of the first session
    whose FIRST bar already has the full warm-up behind it. A session whose
    early bars sit inside the warm-up would be rejected by `score_window`."""
    starts = []
    for symbol, frame in frames.items():
        if len(frame) <= LOOKBACK_BARS:
            raise RuntimeError(f"{symbol}: stitched frame shorter than the warm-up.")
        first_position: dict[object, int] = {}
        for position, ts in enumerate(frame["timestamp"]):
            day = market_date(ts.to_pydatetime())
            if day not in first_position:
                first_position[day] = position
        eligible = [
            day for day, position in first_position.items() if position >= LOOKBACK_BARS - 1
        ]
        if not eligible:
            raise RuntimeError(f"{symbol}: no session clears the warm-up on the stitched frame.")
        starts.append(min(eligible))
    return ScoringWindow(
        name="w00",
        start=max(starts),
        end=W00_END,
        covers="locked semi-fresh attack window preceding w01",
    )


def _score_symbol(args: tuple[str, str]) -> str:
    symbol, window_start = args
    datasets = default_datasets()
    target = OUTPUT / f"{symbol}_w00_V3.parquet"
    if target.exists():
        return f"{symbol}: exists"
    frame = stitched_frame(datasets, symbol)
    window = ScoringWindow(
        name="w00", start=date.fromisoformat(window_start), end=W00_END, covers="w00"
    )
    spec = next(s for s in build_engines() if s.name == "V3")
    records = score_window(
        frame, window, spec, symbol=symbol, artifact=None, lookback_bars=LOOKBACK_BARS
    )
    insufficient = insufficient_history_count(records)
    if insufficient:
        raise RuntimeError(f"{symbol}: {insufficient} INSUFFICIENT_* decisions on w00.")
    decisions_to_frame(records).to_parquet(target, engine="pyarrow", index=False)
    return f"{symbol}: scored {len(records)} bars"


def main() -> None:
    if not UNLOCK.exists():
        raise SystemExit(
            "w00 is LOCKED. Freeze the final candidate in the search ledger and write "
            f"{UNLOCK} first."
        )
    unlock = json.loads(UNLOCK.read_text())
    print(f"w00 unlocked by freeze record: {unlock.get('frozen_at')}")
    started = time.perf_counter()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    datasets = default_datasets()

    frames = {symbol: stitched_frame(datasets, symbol) for symbol in STUDY_SYMBOLS}
    window = w00_window(frames)
    print(f"w00 window: {window.start}..{window.end}", flush=True)

    with ProcessPoolExecutor(max_workers=5) as pool:
        for line in pool.map(
            _score_symbol, [(symbol, window.start.isoformat()) for symbol in STUDY_SYMBOLS]
        ):
            print(line, flush=True)

    spec = ParticipationSpec()
    spy_stitched = frames["SPY"]
    participation = participation_series(session_closes(spy_stitched), spec)

    challenger_records = []
    v3_records = []
    window_frames: dict[str, pd.DataFrame] = {}
    for symbol in STUDY_SYMBOLS:
        stored = frame_to_decisions(pd.read_parquet(OUTPUT / f"{symbol}_w00_V3.parquet"))
        bars = window.bars(frames[symbol])
        window_frames[symbol] = bars
        by_bar = per_bar_participation(bars, participation)
        challenger_records.extend(participation_overlay(stored, by_bar, architecture=ARCHITECTURE))
        v3_records.extend(stored)

    engines = {
        ARCHITECTURE: DecisionSeriesEngine(
            challenger_records, name=ARCHITECTURE, version="eda", warmup_bars=0
        ),
        "V3": DecisionSeriesEngine(v3_records, name="V3", version="v3", warmup_bars=0),
        "BUY_AND_HOLD": BuyAndHoldEngine(),
    }
    result: dict[str, object] = {
        "window": {"start": str(window.start), "end": str(window.end)},
        "engines": {},
    }
    for name, engine in engines.items():
        blocks = {}
        for cost_model in COST_MODELS:
            config = ReplayConfig(
                initial_cash=INITIAL_CASH,
                cost_model=cost_model,
                supported_symbols=EQUITY_SYMBOLS,
                universe_label=EQUITY_UNIVERSE_LABEL,
            )
            replayed = replay_portfolio(window_frames, engine, config)
            metrics = metrics_for_replay(replayed, EQUITY_15M).to_json_dict()
            blocks[cost_model.label] = {
                "metrics": metrics,
                "per_symbol_net": {
                    s: float(sleeve.final_equity / sleeve.initial_cash - 1)
                    for s, sleeve in replayed.sleeves.items()
                },
            }
            print(f"{name}/{cost_model.label}: net {metrics['total_return']:+.4f}", flush=True)
        result["engines"][name] = blocks

    result["elapsed_seconds"] = time.perf_counter() - started
    write_json(OUTPUT / "w00_attack.json", result)
    print("w00 attack complete — the window is now consumed forever.", flush=True)


if __name__ == "__main__":
    main()
