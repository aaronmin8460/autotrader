"""Signal-generating strategies. Strategies never submit broker orders.

The single V0.2 strategy is the EMA 20 / EMA 50 crossover in `ema_cross`. It
emits signals only - no orders, no fills, no positions, no P&L - and there is
deliberately no plugin framework around it.

Its semantics did not change in the crypto pivot. The crossover reads a close
price and two EMAs; nothing in it was ever specific to an asset class, and
`BTC/USD` is simply a symbol string like any other.
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
