"""Deterministic long-only crypto backtesting over stored historical bars.

C4 connects the existing pipeline - canonical Parquet crypto bars, C2
validation, EMA crossover signals - to a simulated portfolio. Signals fill at
the **next** bar's open, never on their own bar (docs/SPEC.md section 6F).
Quantities are fractional `Decimal` values and a conservative flat taker fee
is charged on both sides. It is engineering validation only: no order is
created, no broker is contacted, and no claim of profitability is made.
"""

from autotrader.backtest.engine import (
    DEFAULT_INITIAL_CASH,
    QUANTITY_EXPONENT,
    STRATEGY_NAME,
    TAKER_FEE_RATE,
    BacktestInputError,
    BacktestResult,
    Execution,
    ExecutionSide,
    affordable_quantity,
    buy_cost,
    buy_fee,
    run_backtest,
    sell_fee,
)

__all__ = [
    "DEFAULT_INITIAL_CASH",
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
]
