"""Signal-generating strategies. Strategies never submit broker orders.

Phase 3 provides the single V0.1 strategy: the EMA 20 / EMA 50 crossover in
`ema_cross`. It emits signals only - no orders, no fills, no positions, no
P&L - and there is deliberately no plugin framework around it.
"""

from autotrader.strategies.ema_cross import (
    BUY_REASON,
    EXIT_REASON,
    FAST_PERIOD,
    SLOW_PERIOD,
    Signal,
    SignalType,
    StrategyInputError,
    add_ema_columns,
    generate_ema_cross_signals,
)

__all__ = [
    "BUY_REASON",
    "EXIT_REASON",
    "FAST_PERIOD",
    "SLOW_PERIOD",
    "Signal",
    "SignalType",
    "StrategyInputError",
    "add_ema_columns",
    "generate_ema_cross_signals",
]
