"""Iteration 5, Phase 1: breakout-setup population statistics. No ML.

At each UTC day's last completed bar, an event fires when the close exceeds
the maximum high of the prior N bars (N in {672, 2688}). Statistics are the
non-overlapping population's forward 96-bar net cost-adjusted returns. The
journal's gates decide whether any meta-labeling phase is justified at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from studies.crypto_deep_architecture.data import (
    exact_break_even,
    load_symbol_frame,
    shared_grid,
    window_mask,
)
from studies.crypto_deep_architecture.run_phase1 import cost_adjusted, forward_return_label

OUTPUT_DIR = Path("/Volumes/AUTOTRADER_QA/reports/crypto-deep-architecture/iteration5")

SYMBOLS = ("BTC/USD", "ETH/USD")
LOOKBACKS = (672, 2688)
HORIZON = 96
BARS_PER_DAY = 96
STAT_WINDOWS = ("P1", "P2", "P3", "W01", "W02", "W03", "W04", "W05")


def breakout_events(observations: pd.DataFrame, lookback: int) -> np.ndarray:
    """Breakout bars (amended per journal: any-bar trigger), non-overlapping."""
    high = observations["high"].astype("float64")
    close = observations["close"].astype("float64")
    min_full = int(np.ceil(lookback * 0.98))
    prior_high = high.rolling(lookback, min_periods=min_full).max().shift(1)
    raw = prior_high.notna().to_numpy() & (
        close.to_numpy(dtype="float64") > prior_high.to_numpy(dtype="float64")
    )
    events = np.zeros(len(observations), dtype=bool)
    blocked_until = -1
    for index in np.flatnonzero(raw):
        if index > blocked_until:
            events[index] = True
            blocked_until = index + HORIZON
    return events


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    break_even = exact_break_even()
    grid = shared_grid()
    stats: dict[str, dict] = {"break_even": break_even}
    for symbol in SYMBOLS:
        sf = load_symbol_frame(symbol, grid)
        labels = forward_return_label(sf.observations, grid, HORIZON)
        raw_return = labels["label_forward_return"]
        valid = labels["label_valid"].fillna(False).astype(bool)
        net = cost_adjusted(raw_return, break_even)
        timestamps = sf.timestamps
        for lookback in LOOKBACKS:
            events = pd.Series(breakout_events(sf.observations, lookback)) & valid
            per_window = {}
            dev_nets: list[float] = []
            for window in STAT_WINDOWS:
                mask = events & window_mask(timestamps, window)
                count = int(mask.sum())
                values = net[mask]
                per_window[window] = {
                    "events": count,
                    "mean_net_bps": float(values.mean() * 1e4) if count else None,
                    "median_net_bps": float(values.median() * 1e4) if count else None,
                    "hit_rate": float((raw_return[mask] > break_even).mean()) if count else None,
                }
                if window.startswith("W") and count:
                    dev_nets.extend(values.tolist())
            pooled = pd.Series(dev_nets)
            stats[f"{symbol}|N{lookback}"] = {
                "per_window": per_window,
                "dev_pooled_events": int(len(pooled)),
                "dev_pooled_mean_net_bps": float(pooled.mean() * 1e4) if len(pooled) else None,
                "dev_pooled_median_net_bps": (
                    float(pooled.median() * 1e4) if len(pooled) else None
                ),
                "dev_pooled_positive_share": (float((pooled > 0).mean()) if len(pooled) else None),
            }
    (OUTPUT_DIR / "breakout_population.json").write_text(json.dumps(stats, indent=2))
    print(json.dumps({k: v for k, v in stats.items() if k != "break_even"}, indent=2)[:2000])


if __name__ == "__main__":
    main()
