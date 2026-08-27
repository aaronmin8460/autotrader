"""Deterministic long-only backtesting over stored historical bars.

Phase 4 connects the existing pipeline - canonical Parquet bars, Phase 2
validation, Phase 3 EMA crossover signals - to a simulated portfolio. Signals
fill at the **next** bar's open, never on their own bar (docs/SPEC.md section
6F). It is engineering validation only: no order is created, no broker is
contacted, and no claim of profitability is made.
"""

from autotrader.backtest.engine import (
    DEFAULT_INITIAL_CASH,
    STRATEGY_NAME,
    BacktestInputError,
    BacktestResult,
    Execution,
    ExecutionSide,
    run_backtest,
)

__all__ = [
    "DEFAULT_INITIAL_CASH",
    "STRATEGY_NAME",
    "BacktestInputError",
    "BacktestResult",
    "Execution",
    "ExecutionSide",
    "run_backtest",
]
