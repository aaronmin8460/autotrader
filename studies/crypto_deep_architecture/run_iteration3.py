"""Iteration 3 runner: daily-stride, persistence-debounced trend cells.

The six journal-declared cells: trailing-return lookback R in {672, 1344,
2688} bars, persistence in {1, 2} daily samples, decision taken on the last
completed bar of each UTC day, fill at the next bar's open. Same replay,
same cost models, same windows and forced-liquidation accounting as I2.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from autotrader.research.costs import cost_model_for
from studies.crypto_deep_architecture.data import (
    DEVELOPMENT_WINDOWS,
    load_symbol_frame,
    shared_grid,
)
from studies.crypto_deep_architecture.run_iteration2 import window_bounds
from studies.crypto_deep_architecture.trend_rules import FLAT, LONG, replay

OUTPUT_DIR = Path("/Volumes/AUTOTRADER_QA/reports/crypto-deep-architecture/iteration3")

SYMBOLS = ("BTC/USD", "ETH/USD")
COST_MODELS = ("frictionless", "crypto-taker", "stress")

#: Grid position of the last bar of each UTC day: session_bar_index == 95.
BARS_PER_DAY = 96


def daily_persistent_states(
    close: pd.Series, session_bar_index: np.ndarray, lookback: int, persistence: int
) -> np.ndarray:
    """Desired state per bar: reconsidered once per UTC day, debounced.

    At each day's final completed bar the trailing R-bar return's sign is
    sampled. The desired state switches only after `persistence` consecutive
    daily samples disagree with the held state. Between decision instants the
    desired state is simply held, so the replay trades at most once per day.
    """
    past = close.shift(lookback)
    trailing = (close / past.where(past > 0.0) - 1.0).to_numpy(dtype="float64")
    states = np.zeros(len(close), dtype="int8")
    state = FLAT
    run_sign = 0
    run_length = 0
    for index in range(len(close)):
        if session_bar_index[index] == BARS_PER_DAY - 1 and np.isfinite(trailing[index]):
            sample = LONG if trailing[index] > 0.0 else FLAT
            if sample == run_sign:
                run_length += 1
            else:
                run_sign = sample
                run_length = 1
            if sample != state and run_length >= persistence:
                state = sample
        states[index] = state
    return states


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", default=None, help="score one extra window (e.g. W06)")
    parser.add_argument("--rule", default=None, help="restrict to one cell, e.g. daily_1344_p2")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    grid = shared_grid()
    frames = {symbol: load_symbol_frame(symbol, grid) for symbol in SYMBOLS}
    windows = (args.window,) if args.window else DEVELOPMENT_WINDOWS

    cells = [
        (f"daily_{lookback}_p{persistence}", lookback, persistence)
        for lookback in (672, 1344, 2688)
        for persistence in (1, 2)
    ]
    if args.rule:
        cells = [cell for cell in cells if cell[0] == args.rule]
        if not cells:
            raise SystemExit(f"unknown rule {args.rule!r}")

    rows: list[dict] = []
    for symbol in SYMBOLS:
        observations = frames[symbol].observations
        timestamps = frames[symbol].timestamps
        close = observations["close"].astype("float64")
        session_index = observations["session_bar_index"].to_numpy(dtype="int64")
        for name, lookback, persistence in cells:
            states = daily_persistent_states(close, session_index, lookback, persistence)
            for window in windows:
                start, end = window_bounds(timestamps, window)
                for cost_label in COST_MODELS:
                    result = replay(
                        observations, states, cost_model_for(cost_label), start=start, end=end
                    )
                    rows.append(
                        {
                            "symbol": symbol,
                            "window": window,
                            "rule": name,
                            "cost": cost_label,
                            "net_return": result.net_return,
                            "forced_return": result.forced_liquidation_return,
                            "realized_pnl": result.realized_pnl,
                            "unrealized_pnl": result.unrealized_pnl,
                            "trades": result.trades,
                            "time_in_market": result.time_in_market,
                            "max_drawdown": result.max_drawdown,
                            "open_at_end": result.open_position_at_end,
                        }
                    )
    frame = pd.DataFrame(rows)
    suffix = args.window if args.window else "development"
    path = OUTPUT_DIR / f"daily_trend_results_{suffix}.csv"
    frame.to_csv(path, index=False)
    print(f"iteration 3 results -> {path} ({len(frame)} rows)")


if __name__ == "__main__":
    main()
