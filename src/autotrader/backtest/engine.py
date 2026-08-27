"""Phase 4: deterministic long-only backtesting over canonical bars.

This module simulates what the Phase 3 EMA crossover strategy would have done
on a stored Phase 1 dataset. It is **engineering validation** - proof that
data, validation, signals, execution timing, and portfolio accounting connect
correctly - and it is emphatically **not a claim that the strategy is
profitable** (docs/SPEC.md section 3.3).

Nothing here touches a broker. There is no order, no broker trading client,
and no network access: the whole simulation is local arithmetic over a
DataFrame.

**No look-ahead** (docs/SPEC.md section 6F). A crossover on bar *t* is knowable
only once bar *t* has closed, so the earliest moment it can be acted on is the
open of bar *t+1*::

    signal on bar t  ->  fill at bar t+1 open

A signal is therefore never filled on its own bar - not at that bar's open, not
at its close - and a signal on the final bar is left unexecuted rather than
filled at an invented price.

**Reuse, not reimplementation.** Data-quality rules live in Phase 2
(`autotrader.data.validation`) and the crossover lives in Phase 3
(`autotrader.strategies.ema_cross`). This module calls both and duplicates
neither: it computes no EMA and repeats no validation rule. A dataset that
fails Phase 2 validation aborts the backtest before any signal is generated;
it is never silently repaired.

**Simplifications, stated plainly.** Commission, fees, and slippage are all
zero, and a fill happens at exactly the next bar's open with no market impact.
Sizing spends all available cash on whole shares. These are a deliberate
engineering baseline for V0.1, not a realistic execution model, and results
must be read with that in mind.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

import pandas as pd

from autotrader.data.validation import ValidationResult, validate_frame
from autotrader.strategies.ema_cross import (
    FAST_PERIOD,
    SLOW_PERIOD,
    Signal,
    SignalType,
    generate_ema_cross_signals,
)

#: Starting simulated cash for V0.1.
DEFAULT_INITIAL_CASH = 100_000.0

#: The one strategy this engine runs. There is no strategy selection.
STRATEGY_NAME = f"EMA{FAST_PERIOD} / EMA{SLOW_PERIOD}"

#: Fills use this bar column, one bar after the signal.
EXECUTION_PRICE_COLUMN = "open"

#: Open positions are marked to market against this column at each bar close.
MARK_PRICE_COLUMN = "close"


class BacktestInputError(Exception):
    """The backtest cannot run on what it was given.

    Raised for an invalid dataset or an unusable starting balance. The CLI
    reports these without a traceback.
    """


class ExecutionSide(Enum):
    """The market side of a simulated fill.

    A strategy emits `BUY`/`EXIT`; a fill is `BUY`/`SELL`. An `EXIT` signal
    becomes a `SELL` execution - the two vocabularies are deliberately kept
    distinct so a signal is never mistaken for a trade.
    """

    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Execution:
    """One simulated fill.

    `signal_timestamp` is the bar whose close produced the signal and
    `execution_timestamp` is the strictly later bar at whose open it filled;
    `price` is that bar's open. `cash_after` is the cash balance once the fill
    settled, and is never negative.
    """

    signal_timestamp: pd.Timestamp
    execution_timestamp: pd.Timestamp
    symbol: str
    side: ExecutionSide
    quantity: int
    price: float
    cash_after: float


@dataclass(frozen=True)
class BacktestResult:
    """The outcome of one deterministic simulation.

    `max_drawdown` and `total_return` are **decimal fractions**, not
    percentages: ``-0.25`` is a 25% drawdown. `max_drawdown` is never
    positive. `equity_curve` holds one end-of-bar equity value per input bar,
    so its last element is `final_equity`.
    """

    symbol: str
    bar_count: int
    initial_cash: float
    final_cash: float
    final_equity: float
    total_return: float
    max_drawdown: float
    ending_position_quantity: int
    ending_position_market_value: float
    completed_round_trips: int
    signal_count: int
    unexecuted_last_bar_signal_count: int
    executions: tuple[Execution, ...]
    equity_curve: tuple[float, ...]

    @property
    def buy_execution_count(self) -> int:
        """How many simulated BUY fills occurred."""
        return sum(1 for execution in self.executions if execution.side is ExecutionSide.BUY)

    @property
    def sell_execution_count(self) -> int:
        """How many simulated SELL fills occurred."""
        return sum(1 for execution in self.executions if execution.side is ExecutionSide.SELL)


# --------------------------------------------------------------------------
# Input contract
# --------------------------------------------------------------------------


def _require_usable_initial_cash(initial_cash: float) -> float:
    """Reject a starting balance that cannot fund a simulation."""
    try:
        value = float(initial_cash)
    except (TypeError, ValueError):
        raise BacktestInputError(f"initial_cash must be a number, got {initial_cash!r}.") from None
    if not math.isfinite(value) or value <= 0:
        raise BacktestInputError(f"initial_cash must be a positive, finite number, got {value!r}.")
    return value


def _require_valid_bars(bars: pd.DataFrame) -> ValidationResult:
    """Run the Phase 2 validator and abort on any finding.

    Validation rules are not duplicated here, and a failing dataset is never
    repaired - no re-sorting, no column patching, no dropped rows.
    """
    result = validate_frame(bars)
    if result.valid:
        return result
    findings = "\n".join(f"- {issue}" for issue in result.errors)
    raise BacktestInputError(
        f"Bars failed Phase 2 validation with {result.error_count} error(s); "
        f"the backtest was not run.\n{findings}"
    )


# --------------------------------------------------------------------------
# Portfolio arithmetic
# --------------------------------------------------------------------------


def _affordable_whole_shares(cash: float, price: float) -> int:
    """The largest whole-share quantity `cash` buys at `price`.

    Fractional shares are out of scope, so the quantity is floored. Floating
    point can land the quotient a hair above what is actually affordable, so
    the result steps back until it is; cash must never go negative.
    """
    quantity = int(cash // price)
    while quantity > 0 and quantity * price > cash:
        quantity -= 1
    return quantity


def _max_drawdown(equity_curve: Sequence[float]) -> float:
    """The worst peak-to-trough decline in `equity_curve`, as a fraction.

    ``drawdown_t = equity_t / max(equity_0..equity_t) - 1``, and the result is
    the minimum of those - ``0.0`` for a curve that never declines. Only bars
    at or before *t* are consulted, so no future bar can influence a drawdown.
    """
    peak = equity_curve[0]
    worst = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    return worst


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def run_backtest(bars: pd.DataFrame, initial_cash: float = DEFAULT_INITIAL_CASH) -> BacktestResult:
    """Simulate the EMA crossover strategy over `bars`.

    Validates with Phase 2, generates Phase 3 signals, fills each signal at the
    **next** bar's open, and marks the portfolio at every bar's close. Long
    only, at most one position, no leverage, and zero commission, fees, and
    slippage.

    A `BUY` while already long, an `EXIT` while flat, and a `BUY` too small to
    afford a single share are all no-ops rather than executions. A signal on
    the final bar has no next bar and is left unexecuted. An open position at
    the end is *not* liquidated; it is marked to the final bar's close.

    The supplied frame is never modified. The same frame always produces the
    same result.

    Raises `BacktestInputError` when `initial_cash` is not positive and finite
    or when the dataset fails Phase 2 validation.
    """
    starting_cash = _require_usable_initial_cash(initial_cash)
    cash = starting_cash
    validation = _require_valid_bars(bars)
    # Validation passed, so the dataset resolves to exactly one symbol.
    symbol = str(validation.symbol)

    signals = generate_ema_cross_signals(bars)
    timestamps = list(bars["timestamp"])
    opens = bars[EXECUTION_PRICE_COLUMN].to_numpy(dtype="float64")
    closes = bars[MARK_PRICE_COLUMN].to_numpy(dtype="float64")

    position_of = {timestamp: index for index, timestamp in enumerate(timestamps)}
    signal_at: dict[int, Signal] = {position_of[signal.timestamp]: signal for signal in signals}

    executions: list[Execution] = []
    equity_curve: list[float] = []
    quantity = 0
    completed_round_trips = 0
    # The signal awaiting the next bar's open. Carrying it forward one bar is
    # the whole no-look-ahead rule: it is never consulted on its own bar.
    pending: Signal | None = None

    for index in range(len(timestamps)):
        if pending is not None:
            price = float(opens[index])
            if pending.type is SignalType.BUY and quantity == 0:
                size = _affordable_whole_shares(cash, price)
                if size > 0:
                    cash -= size * price
                    quantity = size
                    executions.append(
                        Execution(
                            signal_timestamp=pending.timestamp,
                            execution_timestamp=timestamps[index],
                            symbol=symbol,
                            side=ExecutionSide.BUY,
                            quantity=size,
                            price=price,
                            cash_after=cash,
                        )
                    )
            elif pending.type is SignalType.EXIT and quantity > 0:
                cash += quantity * price
                executions.append(
                    Execution(
                        signal_timestamp=pending.timestamp,
                        execution_timestamp=timestamps[index],
                        symbol=symbol,
                        side=ExecutionSide.SELL,
                        quantity=quantity,
                        price=price,
                        cash_after=cash,
                    )
                )
                quantity = 0
                completed_round_trips += 1
            # Everything else is a no-op: EXIT while flat, BUY while already
            # long, and a BUY that cannot afford one whole share. None of them
            # short, pyramid, or duplicate a holding.
            pending = None

        # Mark to market only after this bar's open has been acted on.
        equity_curve.append(cash + quantity * float(closes[index]))
        pending = signal_at.get(index)

    final_close = float(closes[-1])
    ending_position_market_value = quantity * final_close
    final_equity = cash + ending_position_market_value

    return BacktestResult(
        symbol=symbol,
        bar_count=len(timestamps),
        initial_cash=starting_cash,
        final_cash=cash,
        final_equity=final_equity,
        total_return=final_equity / starting_cash - 1.0,
        max_drawdown=_max_drawdown(equity_curve),
        ending_position_quantity=quantity,
        ending_position_market_value=ending_position_market_value,
        completed_round_trips=completed_round_trips,
        signal_count=len(signals),
        # The last bar has no successor, so a signal there stays pending.
        unexecuted_last_bar_signal_count=1 if pending is not None else 0,
        executions=tuple(executions),
        equity_curve=tuple(equity_curve),
    )


__all__ = [
    "DEFAULT_INITIAL_CASH",
    "EXECUTION_PRICE_COLUMN",
    "MARK_PRICE_COLUMN",
    "STRATEGY_NAME",
    "BacktestInputError",
    "BacktestResult",
    "Execution",
    "ExecutionSide",
    "run_backtest",
]
