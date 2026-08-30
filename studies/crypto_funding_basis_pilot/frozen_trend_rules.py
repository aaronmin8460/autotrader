# FROZEN COPY - do not edit.
# Copied verbatim from studies/crypto_deep_architecture/trend_rules.py on branch
# research/crypto-deep-architecture, source SHA-256
# e67a74707047d7bd7494be82152e07f9fa42ea867657159782777f1d209d6d9d
# Vendored rather than imported so this pilot's baseline architecture cannot
# drift when the other research branch moves. Any edit here would break the
# 'identical harness' claim the incremental comparison rests on.
"""Iteration 2: slow trend rules and the ledger-exact replay that scores them.

Every rule here is causal by construction: a signal at bar t reads closes and
highs/lows up to and including bar t, and a state change fills at bar t+1's
open under the cost model's adverse slippage and fee - the same execution
contract the prior studies' simulator enforced. All-in sizing, one long
position, no shorting.

The replay reports realized and unrealized PnL separately and can liquidate
any open position at a boundary bar's open, because the V5 lesson is pinned
in this repository's research record: a headline that depends on one
favourable open position is not a result.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from autotrader.research.costs import CostModel

#: Signal states.
FLAT = 0
LONG = 1


@dataclass(frozen=True)
class TrendRule:
    """One predeclared rule cell."""

    name: str
    family: str
    lookback_bars: int
    deadband: float = 0.0


def predeclared_rules() -> tuple[TrendRule, ...]:
    """The nine journal-declared cells, and nothing else."""
    cells: list[TrendRule] = []
    for lookback in (672, 1344, 2688):
        for deadband in (0.0, 0.0025):
            tag = f"tsmom_{lookback}_{int(deadband * 1e4)}bp"
            cells.append(
                TrendRule(name=tag, family="tsmom", lookback_bars=lookback, deadband=deadband)
            )
    for lookback in (672, 1344, 2688):
        cells.append(
            TrendRule(name=f"donchian_{lookback}", family="donchian", lookback_bars=lookback)
        )
    return tuple(cells)


def tsmom_states(close: pd.Series, lookback: int, deadband: float) -> np.ndarray:
    """LONG above +deadband, FLAT below -deadband, previous state between."""
    past = close.shift(lookback)
    trailing = (close / past.where(past > 0.0) - 1.0).to_numpy(dtype="float64")
    states = np.zeros(len(close), dtype="int8")
    state = FLAT
    for index in range(len(close)):
        value = trailing[index]
        if np.isfinite(value):
            if value > deadband:
                state = LONG
            elif value < -deadband:
                state = FLAT
        states[index] = state
    return states


def donchian_states(close: pd.Series, high: pd.Series, low: pd.Series, lookback: int) -> np.ndarray:
    """Enter above the prior N-bar high, exit below the prior N/2-bar low."""
    exit_bars = lookback // 2
    min_full = int(np.ceil(lookback * 0.98))
    min_exit = int(np.ceil(exit_bars * 0.98))
    upper = high.rolling(lookback, min_periods=min_full).max().shift(1)
    lower = low.rolling(exit_bars, min_periods=min_exit).min().shift(1)
    close_values = close.to_numpy(dtype="float64")
    upper_values = upper.to_numpy(dtype="float64")
    lower_values = lower.to_numpy(dtype="float64")
    states = np.zeros(len(close), dtype="int8")
    state = FLAT
    for index in range(len(close)):
        if state == FLAT:
            if np.isfinite(upper_values[index]) and close_values[index] > upper_values[index]:
                state = LONG
        elif np.isfinite(lower_values[index]) and close_values[index] < lower_values[index]:
            state = FLAT
        states[index] = state
    return states


def rule_states(rule: TrendRule, observations: pd.DataFrame) -> np.ndarray:
    close = observations["close"].astype("float64")
    if rule.family == "tsmom":
        return tsmom_states(close, rule.lookback_bars, rule.deadband)
    return donchian_states(
        close,
        observations["high"].astype("float64"),
        observations["low"].astype("float64"),
        rule.lookback_bars,
    )


@dataclass
class ReplayResult:
    """One replay over one interval, ledgered."""

    net_return: float
    realized_pnl: float
    unrealized_pnl: float
    forced_liquidation_return: float
    trades: int
    fees_paid: float
    time_in_market: float
    open_position_at_end: bool
    max_drawdown: float


def replay(
    observations: pd.DataFrame,
    states: np.ndarray,
    cost: CostModel,
    *,
    start: int,
    end: int,
    starting_cash: float = 100_000.0,
) -> ReplayResult:
    """Replay desired `states` over grid positions [start, end], ledger-exact.

    A state change decided on bar t fills at the open of the next *present*
    bar after t (the provider occasionally publishes nothing; an order does
    not fill on a bar that never printed). Equity marks on each present bar's
    close. The forced-liquidation diagnostic additionally sells any open
    position at the last present bar's close under the same cost model.
    """
    opens = observations["open"].to_numpy(dtype="float64")
    closes = observations["close"].to_numpy(dtype="float64")
    present = observations["is_present"].to_numpy(dtype=bool)

    fee = float(cost.fee_rate)
    slip = float(cost.slippage_rate)

    cash = starting_cash
    quantity = 0.0
    entry_cost_basis = 0.0
    realized = 0.0
    fees = 0.0
    trades = 0
    bars_long = 0
    bars_counted = 0
    peak = starting_cash
    max_drawdown = 0.0
    last_mark = starting_cash

    pending: int | None = None
    for index in range(start, min(end, len(states) - 1) + 1):
        if pending is not None and present[index] and np.isfinite(opens[index]):
            price = opens[index]
            if pending == LONG and quantity == 0.0:
                fill = price * (1.0 + slip)
                spend = cash / (1.0 + fee)
                quantity = spend / fill
                fee_paid = spend * fee
                fees += fee_paid
                entry_cost_basis = cash
                cash = 0.0
                trades += 1
            elif pending == FLAT and quantity > 0.0:
                fill = price * (1.0 - slip)
                proceeds = quantity * fill
                fee_paid = proceeds * fee
                fees += fee_paid
                cash = proceeds - fee_paid
                realized += cash - entry_cost_basis
                quantity = 0.0
                trades += 1
            pending = None

        if present[index] and np.isfinite(closes[index]):
            last_mark = cash + quantity * closes[index]
            bars_counted += 1
            if quantity > 0.0:
                bars_long += 1
            peak = max(peak, last_mark)
            if peak > 0.0:
                max_drawdown = min(max_drawdown, last_mark / peak - 1.0)

        # The decision at this bar's close supersedes any unfilled pending
        # order: the latest desired state is the only one worth acting on.
        desired = int(states[index])
        holding = LONG if quantity > 0.0 else FLAT
        pending = desired if desired != holding else None

    unrealized = last_mark - cash - (entry_cost_basis if quantity > 0.0 else 0.0)
    forced = last_mark
    if quantity > 0.0:
        position_value = last_mark - cash
        proceeds = position_value * (1.0 - slip)
        forced = cash + proceeds * (1.0 - fee)
    return ReplayResult(
        net_return=last_mark / starting_cash - 1.0,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        forced_liquidation_return=forced / starting_cash - 1.0,
        trades=trades,
        fees_paid=fees,
        time_in_market=bars_long / bars_counted if bars_counted else 0.0,
        open_position_at_end=quantity > 0.0,
        max_drawdown=max_drawdown,
    )


__all__ = [
    "FLAT",
    "LONG",
    "ReplayResult",
    "TrendRule",
    "donchian_states",
    "predeclared_rules",
    "replay",
    "rule_states",
    "tsmom_states",
]
