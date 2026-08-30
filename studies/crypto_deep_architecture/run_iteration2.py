"""Iteration 2 runner: score the nine predeclared trend cells on W01-W05.

Development windows only. W06 and W07 are not read here; the single selected
cell (if any survives the journal's selection rule) gets W06 exactly once in
a separate, later invocation with `--window W06 --rule <name>`.

Outputs one CSV of per-window results per cost model, plus buy-and-hold
benchmarks computed under identical execution semantics (enter at the first
present bar's open with costs, mark at the last present close; forced
liquidation sells there too).
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
    window_mask,
)
from studies.crypto_deep_architecture.trend_rules import (
    LONG,
    predeclared_rules,
    replay,
    rule_states,
)

OUTPUT_DIR = Path("/Volumes/AUTOTRADER_QA/reports/crypto-deep-architecture/iteration2")

SYMBOLS = ("BTC/USD", "ETH/USD")
COST_MODELS = ("frictionless", "crypto-taker", "stress")


def window_bounds(timestamps: pd.Series, window: str) -> tuple[int, int]:
    mask = window_mask(timestamps, window)
    positions = np.flatnonzero(mask.to_numpy())
    return int(positions[0]), int(positions[-1])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", default=None, help="score one extra window (e.g. W06)")
    parser.add_argument("--rule", default=None, help="restrict to one rule cell by name")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    grid = shared_grid()
    frames = {symbol: load_symbol_frame(symbol, grid) for symbol in SYMBOLS}
    windows = (args.window,) if args.window else DEVELOPMENT_WINDOWS
    rules = predeclared_rules()
    if args.rule:
        rules = tuple(rule for rule in rules if rule.name == args.rule)
        if not rules:
            raise SystemExit(f"unknown rule {args.rule!r}")

    rows: list[dict] = []
    for symbol in SYMBOLS:
        observations = frames[symbol].observations
        timestamps = frames[symbol].timestamps
        state_cache = {rule.name: rule_states(rule, observations) for rule in rules}
        buy_and_hold = np.full(len(observations), LONG, dtype="int8")

        for window in windows:
            start, end = window_bounds(timestamps, window)
            for cost_label in COST_MODELS:
                cost = cost_model_for(cost_label)
                bh = replay(observations, buy_and_hold, cost, start=start, end=end)
                rows.append(
                    {
                        "symbol": symbol,
                        "window": window,
                        "rule": "buy_and_hold",
                        "cost": cost_label,
                        "net_return": bh.net_return,
                        "forced_return": bh.forced_liquidation_return,
                        "realized_pnl": bh.realized_pnl,
                        "unrealized_pnl": bh.unrealized_pnl,
                        "trades": bh.trades,
                        "time_in_market": bh.time_in_market,
                        "max_drawdown": bh.max_drawdown,
                        "open_at_end": bh.open_position_at_end,
                    }
                )
                for rule in rules:
                    result = replay(
                        observations, state_cache[rule.name], cost, start=start, end=end
                    )
                    rows.append(
                        {
                            "symbol": symbol,
                            "window": window,
                            "rule": rule.name,
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
    path = OUTPUT_DIR / f"trend_results_{suffix}.csv"
    frame.to_csv(path, index=False)
    print(f"iteration 2 results -> {path} ({len(frame)} rows)")


if __name__ == "__main__":
    main()
