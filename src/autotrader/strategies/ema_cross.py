"""C3: deterministic EMA 20 / EMA 50 crossover signals.

This is the project's only strategy (docs/SPEC.md section 3.3). It exists to
validate the engineering pipeline end to end - data -> signal - and it is a
test fixture, not an edge. No claim is made or implied that it is profitable.

**Unchanged by the crypto pivot.** Nothing here was ever asset-class specific:
the strategy reads `close` and a symbol string, so `BTC/USD` works exactly as
any other symbol did. No crypto-specific indicator, no RSI, no MACD, no
sentiment, no ML, and no parameter optimization was added.

**Scope.** The strategy reads canonical historical bars and emits signals.
Nothing else: no execution price, no order, no fill, no position, no cash, and
no P&L. Per docs/SPEC.md section 6A a strategy module must never import a
broker client, and this module imports nothing beyond the standard library and
pandas.

**No look-ahead.** A signal carries the timestamp of the bar whose close
produced the crossover, because that close is the first moment the crossover
is knowable. That timestamp is *not* an execution timestamp and the signal
carries no price: a signal at bar *t* asserts only that the crossover was
observable once bar *t* had closed. Deciding when, and at what price, such a
signal could be acted on belongs to C4 backtesting, which must fill at
*t+1* or later (docs/SPEC.md section 6F).

**Validation.** The checks here are deliberately minimal - only enough to keep
an input-contract violation from surfacing as an obscure pandas error. Full
data-quality validation (duplicate timestamps, OHLC relationships) is C2 and is
intentionally not duplicated here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd

#: EMA periods. Fixed for V0.2; deliberately not configurable.
FAST_PERIOD = 20
SLOW_PERIOD = 50

#: The strategy trades off the bar close only.
PRICE_COLUMN = "close"

#: The subset of the canonical bar schema this strategy reads.
REQUIRED_COLUMNS: tuple[str, ...] = ("timestamp", "symbol", PRICE_COLUMN)

#: Column names `add_ema_columns` appends.
FAST_EMA_COLUMN = "ema_20"
SLOW_EMA_COLUMN = "ema_50"

#: Stable, auditable machine reasons. Never natural-language explanations.
BUY_REASON = "EMA20_CROSS_ABOVE_EMA50"
EXIT_REASON = "EMA20_CROSS_BELOW_EMA50"


class StrategyInputError(Exception):
    """The supplied bars violate this strategy's input contract."""


class SignalType(Enum):
    """The only two signals a long-only crossover strategy can produce."""

    BUY = "BUY"
    EXIT = "EXIT"


@dataclass(frozen=True)
class Signal:
    """One crossover observation.

    `timestamp` is the bar on which the crossover became knowable, not an
    execution time, and there is deliberately no price field.
    """

    timestamp: pd.Timestamp
    symbol: str
    type: SignalType
    reason: str


# --------------------------------------------------------------------------
# Input contract
# --------------------------------------------------------------------------


def _require_columns(bars: pd.DataFrame) -> None:
    """Reject input missing a column this strategy reads."""
    missing = [column for column in REQUIRED_COLUMNS if column not in bars.columns]
    if missing:
        raise StrategyInputError(
            f"Bars are missing required column(s): {', '.join(missing)}. "
            f"This strategy requires: {', '.join(REQUIRED_COLUMNS)}."
        )


def _require_single_symbol(bars: pd.DataFrame) -> str:
    """Return the one symbol in `bars`, rejecting a multi-symbol frame.

    C1 produces one-symbol datasets. Grouped multi-symbol processing is
    not implemented, so a mixed frame is a contract violation rather than
    something to silently split.
    """
    symbols = pd.unique(bars["symbol"])
    if len(symbols) != 1:
        found = ", ".join(sorted(str(symbol) for symbol in symbols))
        raise StrategyInputError(
            f"Bars must contain exactly one symbol, found {len(symbols)}: {found}. "
            "This strategy processes a single symbol at a time."
        )
    return str(symbols[0])


def _require_ascending_timestamps(bars: pd.DataFrame) -> None:
    """Reject unsorted input instead of sorting it.

    Sorting here would hide an upstream data-contract violation, so the
    strategy fails loudly instead. Duplicate timestamps are accepted at this
    layer; detecting them is C2's job.
    """
    if not bars["timestamp"].is_monotonic_increasing:
        raise StrategyInputError(
            "Bars must be ordered ascending by timestamp. This strategy does not sort its "
            "input, because silently reordering would mask an upstream data-contract "
            "violation."
        )


# --------------------------------------------------------------------------
# Indicator
# --------------------------------------------------------------------------


def _ema(close: pd.Series, period: int) -> pd.Series:
    """The recursive exponential moving average of `close`.

    `adjust=False` selects pandas' recursive form, seeded with the first
    observation: ``ema[0] = close[0]`` and
    ``ema[i] = ema[i - 1] + alpha * (close[i] - ema[i - 1])`` with
    ``alpha = 2 / (period + 1)``. `min_periods` masks the output until
    `period` observations exist, so the warm-up is explicit rather than a
    ramp of under-informed values.
    """
    return close.ewm(span=period, adjust=False, min_periods=period).mean()


def add_ema_columns(bars: pd.DataFrame) -> pd.DataFrame:
    """Return a **copy** of `bars` with `ema_20` and `ema_50` appended.

    The supplied frame is never modified. Both columns are NaN during their
    warm-up, so `ema_50` carries no value until 50 bars have been observed.
    """
    _require_columns(bars)
    enriched = bars.copy()
    close = enriched[PRICE_COLUMN].astype("float64")
    enriched[FAST_EMA_COLUMN] = _ema(close, FAST_PERIOD)
    enriched[SLOW_EMA_COLUMN] = _ema(close, SLOW_PERIOD)
    return enriched


# --------------------------------------------------------------------------
# Signal generation
# --------------------------------------------------------------------------


def generate_ema_cross_signals(bars: pd.DataFrame) -> list[Signal]:
    """Generate EMA 20 / EMA 50 crossover signals from canonical bars.

    BUY is emitted on a bar where the fast EMA moves from at-or-below the slow
    EMA to strictly above it; EXIT where it moves from at-or-above to strictly
    below. Every other bar produces nothing, so a crossover yields at most one
    signal and no signal repeats while the relation merely persists.

    Returns signals ascending by timestamp. An empty frame yields no signals.
    The supplied frame is not modified.
    """
    _require_columns(bars)
    if bars.empty:
        return []
    symbol = _require_single_symbol(bars)
    _require_ascending_timestamps(bars)

    enriched = add_ema_columns(bars)
    fast = enriched[FAST_EMA_COLUMN]
    slow = enriched[SLOW_EMA_COLUMN]
    previous_fast = fast.shift(1)
    previous_slow = slow.shift(1)

    # Comparisons against a warm-up NaN are False, so no bar before the slow
    # EMA is defined on both this bar and the previous one can be actionable.
    crossed_above = (previous_fast <= previous_slow) & (fast > slow)
    crossed_below = (previous_fast >= previous_slow) & (fast < slow)

    signals: list[Signal] = []
    for position, timestamp in enumerate(enriched["timestamp"]):
        if crossed_above.iat[position]:
            signal_type, reason = SignalType.BUY, BUY_REASON
        elif crossed_below.iat[position]:
            signal_type, reason = SignalType.EXIT, EXIT_REASON
        else:
            continue
        signals.append(Signal(timestamp=timestamp, symbol=symbol, type=signal_type, reason=reason))
    return signals


__all__ = [
    "BUY_REASON",
    "EXIT_REASON",
    "FAST_EMA_COLUMN",
    "FAST_PERIOD",
    "PRICE_COLUMN",
    "REQUIRED_COLUMNS",
    "SLOW_EMA_COLUMN",
    "SLOW_PERIOD",
    "Signal",
    "SignalType",
    "StrategyInputError",
    "add_ema_columns",
    "generate_ema_cross_signals",
]
