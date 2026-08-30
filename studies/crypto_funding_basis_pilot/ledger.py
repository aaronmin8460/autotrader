"""Per-trade reconstruction of the frozen replay, for trade-level statistics.

The frozen `replay` reports portfolio aggregates but not a trade list, and hit
rate and average trade need one. Rather than edit the frozen engine - which
would break the "identical harness" claim - this module re-walks the same
state array under the same fill rule and emits the individual round trips.

It is only trustworthy if it agrees with the engine it mirrors, so
`ledger_matches_replay` compares the compounded ledger against the frozen
`ReplayResult`, and the test suite asserts the agreement on real cells. A
divergence means this module is wrong, not the engine.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from autotrader.research.costs import CostModel
from studies.crypto_funding_basis_pilot.frozen_trend_rules import FLAT, LONG


@dataclass(frozen=True)
class Trade:
    entry_index: int
    exit_index: int | None
    entry_equity: float
    exit_equity: float | None
    return_fraction: float | None


def trade_ledger(
    observations: pd.DataFrame,
    states: np.ndarray,
    cost: CostModel,
    *,
    start: int,
    end: int,
    starting_cash: float = 100_000.0,
) -> list[Trade]:
    """Round trips implied by `states`, under the frozen fill and cost rule."""
    opens = observations["open"].to_numpy(dtype="float64")
    present = observations["is_present"].to_numpy(dtype=bool)
    fee = float(cost.fee_rate)
    slip = float(cost.slippage_rate)

    cash = starting_cash
    quantity = 0.0
    entry_equity = 0.0
    entry_index = -1
    trades: list[Trade] = []

    pending: int | None = None
    for index in range(start, min(end, len(states) - 1) + 1):
        if pending is not None and present[index] and np.isfinite(opens[index]):
            price = opens[index]
            if pending == LONG and quantity == 0.0:
                fill = price * (1.0 + slip)
                spend = cash / (1.0 + fee)
                quantity = spend / fill
                entry_equity = cash
                entry_index = index
                cash = 0.0
            elif pending == FLAT and quantity > 0.0:
                fill = price * (1.0 - slip)
                proceeds = quantity * fill
                cash = proceeds - proceeds * fee
                trades.append(
                    Trade(
                        entry_index=entry_index,
                        exit_index=index,
                        entry_equity=entry_equity,
                        exit_equity=cash,
                        return_fraction=cash / entry_equity - 1.0 if entry_equity else None,
                    )
                )
                quantity = 0.0
            pending = None

        # The frozen engine marks equity on every present bar; the ledger does
        # not need to, because a round trip's return is fixed by its two fills.
        desired = int(states[index])
        holding = LONG if quantity > 0.0 else FLAT
        pending = desired if desired != holding else None

    if quantity > 0.0:
        trades.append(
            Trade(
                entry_index=entry_index,
                exit_index=None,
                entry_equity=entry_equity,
                exit_equity=None,
                return_fraction=None,
            )
        )
    return trades


def ledger_statistics(trades: list[Trade]) -> dict:
    """Hit rate and average trade over *closed* round trips only."""
    closed = [t.return_fraction for t in trades if t.return_fraction is not None]
    open_count = sum(1 for t in trades if t.exit_index is None)
    if not closed:
        return {
            "closed_trades": 0,
            "open_at_end": open_count,
            "hit_rate": None,
            "average_trade": None,
            "best_trade": None,
            "worst_trade": None,
        }
    values = np.asarray(closed, dtype="float64")
    return {
        "closed_trades": int(values.size),
        "open_at_end": open_count,
        "hit_rate": float((values > 0.0).mean()),
        "average_trade": float(values.mean()),
        "best_trade": float(values.max()),
        "worst_trade": float(values.min()),
    }


def ledger_matches_replay(trades: list[Trade], result, tolerance: float = 1e-9) -> bool:
    """Does the compounded closed-trade ledger reproduce the engine's realised leg?

    Only comparable when no position is open at the end; with an open position
    the engine's mark carries unrealised value the ledger deliberately omits.
    """
    if result.open_position_at_end:
        return True
    compounded = 1.0
    for trade in trades:
        if trade.return_fraction is not None:
            compounded *= 1.0 + trade.return_fraction
    return abs(compounded - (1.0 + result.net_return)) <= tolerance * max(1.0, compounded)


__all__ = ["Trade", "ledger_matches_replay", "ledger_statistics", "trade_ledger"]
