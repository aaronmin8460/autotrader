"""C4: deterministic long-only crypto backtesting over canonical bars.

This module simulates what the EMA crossover strategy would have done on a
stored C1 dataset. It is **engineering validation** - proof that data,
validation, signals, execution timing, and portfolio accounting connect
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
filled at an invented price. Crypto trades continuously, so "the next bar" is
simply the next bar: there is no session boundary, no market open, and no
overnight gap to reason about.

**Fractional positions, in Decimal.** Crypto is fractionable, so the archived
equity milestone's ``floor(cash / price)`` whole-share rule is gone: a BUY
spends the available cash on whatever fraction of a coin it buys. Position
quantities and cash are `decimal.Decimal`, not binary floats, so an accounting
identity that should hold exactly does hold exactly. There is deliberately no
one-whole-coin minimum: $100 buys a fraction of a $100,000 coin.

**Fees are modelled; slippage is not.** A conservative flat taker fee of
``0.25%`` is charged on both the BUY and the SELL side (`TAKER_FEE_RATE`). See
that constant for exactly what this assumption is and is not.

**Reuse, not reimplementation.** Data-quality rules live in C2
(`autotrader.data.validation`) and the crossover lives in the strategy layer
(`autotrader.strategies.ema_cross`). This module calls both and duplicates
neither: it computes no EMA and repeats no validation rule. A dataset that
fails validation aborts the backtest before any signal is generated; it is
never silently repaired.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, localcontext
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

#: Starting simulated cash for V0.2, in USD.
DEFAULT_INITIAL_CASH = Decimal("100000")

#: The one strategy this engine runs. There is no strategy selection.
STRATEGY_NAME = f"EMA{FAST_PERIOD} / EMA{SLOW_PERIOD}"

#: Fills use this bar column, one bar after the signal.
EXECUTION_PRICE_COLUMN = "open"

#: Open positions are marked to market against this column at each bar close.
MARK_PRICE_COLUMN = "close"

#: Simulated taker fee, charged on **every** executed side.
#:
#: This is a deliberately simple, deliberately conservative V0.2 backtest
#: assumption, not a fee schedule. It models the cost of crossing the spread
#: with a market order at a flat 0.25% of notional. Alpaca's real crypto fees
#: depend on 30-day trailing volume tiers and on provider rules that change;
#: none of that is implemented here, and this number is **not** billing or
#: reconciliation logic. It exists so a backtest cannot report a return that
#: silently assumes trading is free.
TAKER_FEE_RATE = Decimal("0.0025")

#: Position quantities are quantized to this exponent, always **downwards**.
#:
#: Crypto positions are continuous for the purposes of this simulation, so the
#: exponent is far finer than any real broker increment. Deliberately *not* the
#: live BTC/ETH increment: provider minimums change, the historical simulation
#: must stay reproducible, and the real broker's asset metadata is the runtime
#: authority at the execution boundary instead (`autotrader.execution`).
QUANTITY_EXPONENT = Decimal("1E-18")

#: One unit in the last place of a quantized quantity.
_QUANTITY_STEP = QUANTITY_EXPONENT

#: Working precision for the simulation's exact decimal arithmetic. Chosen well
#: above the widest value the accounting can produce (a quantity of 18 decimal
#: places times a price of ~17 significant digits, times the fee multiplier),
#: so no intermediate result is ever silently rounded.
DECIMAL_PRECISION = 60

_ZERO = Decimal(0)
_ONE = Decimal(1)


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
    `price` is that bar's open. `quantity` is fractional. `fee` is the modelled
    taker cost of this side, always positive. `cash_after` is the cash balance
    once the fill and its fee settled, and is never negative.
    """

    signal_timestamp: pd.Timestamp
    execution_timestamp: pd.Timestamp
    symbol: str
    side: ExecutionSide
    quantity: Decimal
    price: Decimal
    fee: Decimal
    cash_after: Decimal


@dataclass(frozen=True)
class BacktestResult:
    """The outcome of one deterministic simulation.

    Money and quantities are `Decimal`; `total_return` and `max_drawdown` are
    **decimal fractions** as plain floats, because they are presentation
    ratios rather than balances: ``-0.25`` is a 25% drawdown. `max_drawdown` is
    never positive. `equity_curve` holds one end-of-bar equity value per input
    bar, so its last element is `final_equity`. `total_fees` is the sum of
    every modelled taker fee.
    """

    symbol: str
    bar_count: int
    initial_cash: Decimal
    final_cash: Decimal
    final_equity: Decimal
    total_return: float
    max_drawdown: float
    ending_position_quantity: Decimal
    ending_position_market_value: Decimal
    total_fees: Decimal
    completed_round_trips: int
    signal_count: int
    unexecuted_last_bar_signal_count: int
    executions: tuple[Execution, ...]
    equity_curve: tuple[Decimal, ...]

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


def to_decimal_price(value: object) -> Decimal:
    """Convert a bar price to an exact Decimal, deterministically.

    A stored bar price is a binary float. It is converted through its shortest
    round-tripping decimal form rather than through `Decimal(float)`, so the
    same stored dataset always produces the same simulation - and so a price
    that reads as ``42.15`` is treated as ``42.15`` rather than as the binary
    value a hair away from it.
    """
    return Decimal(str(float(value)))


def _require_usable_initial_cash(initial_cash: object) -> Decimal:
    """Reject a starting balance that cannot fund a simulation.

    Accepts `Decimal`, `int`, a finite `float`, and a decimal string. A float
    is routed through its shortest round-tripping form, so ``100000.0`` is
    exactly ``100000``.
    """
    if isinstance(initial_cash, bool):
        raise BacktestInputError(f"initial_cash must be a number, got {initial_cash!r}.")
    if isinstance(initial_cash, Decimal):
        value = initial_cash
    elif isinstance(initial_cash, int):
        value = Decimal(initial_cash)
    elif isinstance(initial_cash, float):
        if not math.isfinite(initial_cash):
            raise BacktestInputError(
                f"initial_cash must be a positive, finite number, got {initial_cash!r}."
            )
        value = Decimal(str(initial_cash))
    elif isinstance(initial_cash, str):
        try:
            value = Decimal(initial_cash)
        except ArithmeticError:
            raise BacktestInputError(
                f"initial_cash must be a number, got {initial_cash!r}."
            ) from None
    else:
        raise BacktestInputError(f"initial_cash must be a number, got {initial_cash!r}.")

    if not value.is_finite() or value <= 0:
        raise BacktestInputError(f"initial_cash must be a positive, finite number, got {value!r}.")
    return value


def _require_valid_bars(bars: pd.DataFrame) -> ValidationResult:
    """Run the C2 validator and abort on any finding.

    Validation rules are not duplicated here, and a failing dataset is never
    repaired - no re-sorting, no column patching, no dropped rows.
    """
    result = validate_frame(bars)
    if result.valid:
        return result
    findings = "\n".join(f"- {issue}" for issue in result.errors)
    raise BacktestInputError(
        f"Bars failed validation with {result.error_count} error(s); "
        f"the backtest was not run.\n{findings}"
    )


# --------------------------------------------------------------------------
# Portfolio arithmetic
# --------------------------------------------------------------------------


def buy_fee(quantity: Decimal, price: Decimal, fee_rate: Decimal = TAKER_FEE_RATE) -> Decimal:
    """The modelled taker fee for buying `quantity` at `price`."""
    return quantity * price * fee_rate


def sell_fee(quantity: Decimal, price: Decimal, fee_rate: Decimal = TAKER_FEE_RATE) -> Decimal:
    """The modelled taker fee for selling `quantity` at `price`.

    The same rate as a BUY: a market order pays the taker side either way.
    """
    return quantity * price * fee_rate


def buy_cost(quantity: Decimal, price: Decimal, fee_rate: Decimal = TAKER_FEE_RATE) -> Decimal:
    """Total cash a BUY consumes: notional plus the fee on it."""
    return quantity * price + buy_fee(quantity, price, fee_rate)


def affordable_quantity(
    cash: Decimal, price: Decimal, fee_rate: Decimal = TAKER_FEE_RATE
) -> Decimal:
    """The largest quantity `cash` buys at `price` **after paying the fee**.

    Sizing reserves the fee up front - it solves ``q * price * (1 + fee) <=
    cash`` rather than spending every dollar on notional and discovering the
    fee afterwards. Spending 100% of cash on the asset and then charging a fee
    would drive the balance negative, which is an accounting bug rather than a
    modelling choice.

    The quotient is quantized **down**, and the result then steps back until
    the full cost genuinely fits. The step-back is defensive: quantizing down
    already guarantees it, and a limit must never be exceeded by a rounding
    artefact.
    """
    if cash <= 0 or price <= 0:
        return _ZERO
    quantity = (cash / (price * (_ONE + fee_rate))).quantize(QUANTITY_EXPONENT, rounding=ROUND_DOWN)
    while quantity > 0 and buy_cost(quantity, price, fee_rate) > cash:
        quantity -= _QUANTITY_STEP
    return quantity if quantity > 0 else _ZERO


def _max_drawdown(equity_curve: Sequence[Decimal]) -> float:
    """The worst peak-to-trough decline in `equity_curve`, as a fraction.

    ``drawdown_t = equity_t / max(equity_0..equity_t) - 1``, and the result is
    the minimum of those - ``0.0`` for a curve that never declines. Only bars
    at or before *t* are consulted, so no future bar can influence a drawdown.
    """
    peak = equity_curve[0]
    worst = _ZERO
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, equity / peak - _ONE)
    return float(worst)


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def run_backtest(
    bars: pd.DataFrame, initial_cash: Decimal | float | int | str = DEFAULT_INITIAL_CASH
) -> BacktestResult:
    """Simulate the EMA crossover strategy over `bars`.

    Validates with C2, generates crossover signals, fills each signal at the
    **next** bar's open, and marks the portfolio at every bar's close. Long
    only, at most one position, no leverage, fractional quantities, and a flat
    `TAKER_FEE_RATE` on both sides.

    A `BUY` while already long, an `EXIT` while flat, and a `BUY` whose cash
    cannot cover even the smallest representable quantity plus its fee are all
    no-ops rather than executions. A signal on the final bar has no next bar
    and is left unexecuted. An open position at the end is *not* liquidated;
    it is marked to the final bar's close.

    The supplied frame is never modified. The same frame always produces the
    same result.

    Raises `BacktestInputError` when `initial_cash` is not positive and finite
    or when the dataset fails validation.
    """
    starting_cash = _require_usable_initial_cash(initial_cash)
    validation = _require_valid_bars(bars)
    # Validation passed, so the dataset resolves to exactly one symbol.
    symbol = str(validation.symbol)

    signals = generate_ema_cross_signals(bars)
    timestamps = list(bars["timestamp"])
    raw_opens = bars[EXECUTION_PRICE_COLUMN].tolist()
    raw_closes = bars[MARK_PRICE_COLUMN].tolist()

    position_of = {timestamp: index for index, timestamp in enumerate(timestamps)}
    signal_at: dict[int, Signal] = {position_of[signal.timestamp]: signal for signal in signals}

    executions: list[Execution] = []
    equity_curve: list[Decimal] = []

    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        cash = +starting_cash
        quantity = _ZERO
        total_fees = _ZERO
        completed_round_trips = 0
        # The signal awaiting the next bar's open. Carrying it forward one bar
        # is the whole no-look-ahead rule: it is never consulted on its own bar.
        pending: Signal | None = None

        for index in range(len(timestamps)):
            if pending is not None:
                price = to_decimal_price(raw_opens[index])
                if pending.type is SignalType.BUY and quantity == 0:
                    size = affordable_quantity(cash, price)
                    if size > 0:
                        fee = buy_fee(size, price)
                        cash -= size * price + fee
                        quantity = size
                        total_fees += fee
                        executions.append(
                            Execution(
                                signal_timestamp=pending.timestamp,
                                execution_timestamp=timestamps[index],
                                symbol=symbol,
                                side=ExecutionSide.BUY,
                                quantity=size,
                                price=price,
                                fee=fee,
                                cash_after=cash,
                            )
                        )
                elif pending.type is SignalType.EXIT and quantity > 0:
                    fee = sell_fee(quantity, price)
                    cash += quantity * price - fee
                    total_fees += fee
                    executions.append(
                        Execution(
                            signal_timestamp=pending.timestamp,
                            execution_timestamp=timestamps[index],
                            symbol=symbol,
                            side=ExecutionSide.SELL,
                            quantity=quantity,
                            price=price,
                            fee=fee,
                            cash_after=cash,
                        )
                    )
                    quantity = _ZERO
                    completed_round_trips += 1
                # Everything else is a no-op: EXIT while flat, BUY while
                # already long, and a BUY that cannot afford any quantity at
                # all. None of them short, pyramid, or duplicate a holding.
                pending = None

            # Mark to market only after this bar's open has been acted on.
            equity_curve.append(cash + quantity * to_decimal_price(raw_closes[index]))
            pending = signal_at.get(index)

        final_close = to_decimal_price(raw_closes[-1])
        ending_position_market_value = quantity * final_close
        final_equity = cash + ending_position_market_value
        total_return = float(final_equity / starting_cash - _ONE)
        max_drawdown = _max_drawdown(equity_curve)

    return BacktestResult(
        symbol=symbol,
        bar_count=len(timestamps),
        initial_cash=starting_cash,
        final_cash=cash,
        final_equity=final_equity,
        total_return=total_return,
        max_drawdown=max_drawdown,
        ending_position_quantity=quantity,
        ending_position_market_value=ending_position_market_value,
        total_fees=total_fees,
        completed_round_trips=completed_round_trips,
        signal_count=len(signals),
        # The last bar has no successor, so a signal there stays pending.
        unexecuted_last_bar_signal_count=1 if pending is not None else 0,
        executions=tuple(executions),
        equity_curve=tuple(equity_curve),
    )


__all__ = [
    "DECIMAL_PRECISION",
    "DEFAULT_INITIAL_CASH",
    "EXECUTION_PRICE_COLUMN",
    "MARK_PRICE_COLUMN",
    "QUANTITY_EXPONENT",
    "STRATEGY_NAME",
    "TAKER_FEE_RATE",
    "BacktestInputError",
    "BacktestResult",
    "Execution",
    "ExecutionSide",
    "affordable_quantity",
    "buy_cost",
    "buy_fee",
    "run_backtest",
    "sell_fee",
    "to_decimal_price",
]
