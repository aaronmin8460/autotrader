"""The integration contract: what a Decision Engine must look like to be evaluated.

This is the seam that keeps the evaluator from belonging to one strategy. The
replay simulator, the walk-forward runner, the leakage auditor and the sweep
all consume `DecisionEngine` and none of them names a strategy. A future
Decision Engine V2/V3/V4/V5 becomes evaluable by satisfying this protocol -
not by the backtester learning about it, and not by that engine's production
code being rewritten to suit research.

**The protocol is deliberately narrow.** An engine is asked for its identity,
its parameters, its warm-up length, and the signals it derives from a frame of
bars. It is never handed cash, a position, a broker, or an account: sizing and
execution belong to the replay simulator, exactly as in production they belong
to the risk engine and the execution boundary rather than to a strategy
(docs/SPEC.md section 6A). An engine that cannot see the portfolio cannot
accidentally be evaluated on one it would not have had.

**`warmup_bars` is a leakage control, not a hint.** It is how many bars an
engine must observe before its output means anything, and the walk-forward
runner refuses a window shorter than it. An indicator evaluated during its own
warm-up produces under-informed values that look like signal; declaring the
warm-up makes that checkable rather than assumed.

**Two adapters ship here.** `EmaCrossEngine` wraps the existing production
crossover (`autotrader.strategies.ema_cross`) without reimplementing it, which
is what proves the contract fits real strategy code. `ParametricEmaCross` is a
research-only generalization over the EMA periods, because a sweep needs
something with parameters to sweep and the production strategy's periods are
deliberately fixed. A test pins the two together: at the production periods
they must emit identical signals, so the parametric engine is a faithful
extension rather than a second, subtly different strategy.

`BuyAndHoldEngine` is the benchmark. A strategy result reported without one is
not a result: most of what a long-only crypto backtest earns in a bull sample
is the sample, and the only way to see that is to measure it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

import pandas as pd

from autotrader.strategies.ema_cross import (
    FAST_PERIOD,
    PRICE_COLUMN,
    SLOW_PERIOD,
    SignalType,
    StrategyInputError,
    generate_ema_cross_signals,
)


class EngineInputError(Exception):
    """An engine was configured or invoked with something it cannot use."""


class Action(Enum):
    """What an engine proposes. Long-only, so there are exactly two.

    Kept distinct from the production `SignalType` vocabulary on purpose: this
    is the research contract, and letting a research action be a strategy
    signal object would couple every future engine to the one strategy that
    happens to exist today.
    """

    ENTER_LONG = "ENTER_LONG"
    EXIT_LONG = "EXIT_LONG"


@dataclass(frozen=True)
class ResearchSignal:
    """One engine proposal, timestamped by the bar that made it knowable.

    `timestamp` is the close of the bar the proposal was derived from, never an
    execution time, and there is deliberately no price field: what a proposal
    could have been filled at is the simulator's question, not the engine's.

    `strength` is an optional conviction in ``[0, 1]``. The replay simulator
    ignores it under fixed sizing; it exists so a V3+ engine that ranks its
    own proposals has somewhere to put that, rather than encoding it in a
    reason string that nothing can read.
    """

    timestamp: pd.Timestamp
    symbol: str
    action: Action
    reason: str
    strength: float = 1.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.strength <= 1.0:
            raise EngineInputError(f"strength must be within [0, 1], got {self.strength}.")


@runtime_checkable
class DecisionEngine(Protocol):
    """What the research infrastructure requires of a strategy.

    Implementations must be **pure with respect to their input**: the same
    frame must produce the same signals, and no signal may depend on a bar
    later than the one it is timestamped with. Both are checkable rather than
    merely required - `autotrader.research.leakage` audits the second by
    perturbing future bars and re-asking.
    """

    @property
    def name(self) -> str:
        """A stable identifier, used in reports and directory names."""

    @property
    def version(self) -> str:
        """The engine's own version, so V2 and V3 results never merge."""

    @property
    def parameters(self) -> Mapping[str, object]:
        """The full parameter set, recorded with every result."""

    @property
    def warmup_bars(self) -> int:
        """Bars required before output is meaningful."""

    def generate(self, bars: pd.DataFrame) -> Sequence[ResearchSignal]:
        """Derive signals from `bars`, which must never be modified."""


def describe(engine: DecisionEngine) -> dict[str, object]:
    """The identity block recorded alongside every result from `engine`."""
    return {
        "name": engine.name,
        "version": engine.version,
        "parameters": dict(engine.parameters),
        "warmup_bars": engine.warmup_bars,
    }


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _require_frame(bars: pd.DataFrame) -> None:
    """Reject a frame missing what every engine here reads."""
    for column in ("timestamp", "symbol", PRICE_COLUMN):
        if column not in bars.columns:
            raise EngineInputError(f"Bars are missing required column {column!r}.")


def _single_symbol(bars: pd.DataFrame) -> str:
    """The one symbol in `bars`, refusing a mixed frame."""
    symbols = pd.unique(bars["symbol"])
    if len(symbols) != 1:
        raise EngineInputError(
            f"Bars must contain exactly one symbol, found {len(symbols)}. "
            "A multi-symbol frame is replayed per symbol, not as one series."
        )
    return str(symbols[0])


_ACTION_FOR_SIGNAL = {
    SignalType.BUY: Action.ENTER_LONG,
    SignalType.EXIT: Action.EXIT_LONG,
}


# --------------------------------------------------------------------------
# Adapter over the production strategy
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EmaCrossEngine:
    """The production EMA 20 / EMA 50 crossover, behind the research contract.

    An adapter and nothing more: it calls `generate_ema_cross_signals` and
    translates the result into research vocabulary. It computes no EMA, holds
    no threshold, and would break loudly rather than quietly diverge if the
    production strategy's semantics changed - which is the point of adapting
    rather than copying.

    This is the worked example for every future engine: a V2 engine's own
    module stays production code, and an adapter like this one makes it
    evaluable.
    """

    name: str = "ema-cross"
    version: str = "v1"

    @property
    def parameters(self) -> Mapping[str, object]:
        return {"fast_period": FAST_PERIOD, "slow_period": SLOW_PERIOD}

    @property
    def warmup_bars(self) -> int:
        """The slow EMA's warm-up: no crossover is knowable before it."""
        return SLOW_PERIOD

    def generate(self, bars: pd.DataFrame) -> Sequence[ResearchSignal]:
        try:
            signals = generate_ema_cross_signals(bars)
        except StrategyInputError as error:
            raise EngineInputError(str(error)) from None
        return tuple(
            ResearchSignal(
                timestamp=signal.timestamp,
                symbol=signal.symbol,
                action=_ACTION_FOR_SIGNAL[signal.type],
                reason=signal.reason,
            )
            for signal in signals
        )


# --------------------------------------------------------------------------
# Research-only engines
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParametricEmaCross:
    """A research-only EMA crossover with configurable periods.

    Exists so a bounded sweep has a real parameter to vary. The production
    strategy's periods are fixed on purpose and are not made configurable to
    suit research; this is a separate object that happens to reduce to it.

    The reduction is enforced by test rather than asserted here: at
    ``(20, 50)`` this must emit exactly the signals `EmaCrossEngine` emits. If
    that ever stops being true, the sweep is exploring a different strategy
    than the one production runs, and the result would be meaningless in the
    most dangerous way - quietly.
    """

    fast_period: int = FAST_PERIOD
    slow_period: int = SLOW_PERIOD
    name: str = "parametric-ema-cross"
    version: str = "v1"

    def __post_init__(self) -> None:
        for label, period in (
            ("fast_period", self.fast_period),
            ("slow_period", self.slow_period),
        ):
            if not isinstance(period, int) or isinstance(period, bool):
                raise EngineInputError(f"{label} must be an int, got {period!r}.")
            if period < 1:
                raise EngineInputError(f"{label} must be at least 1, got {period}.")
        if self.fast_period >= self.slow_period:
            raise EngineInputError(
                f"fast_period ({self.fast_period}) must be strictly shorter than slow_period "
                f"({self.slow_period}); equal or inverted periods cannot cross."
            )

    @property
    def parameters(self) -> Mapping[str, object]:
        return {"fast_period": self.fast_period, "slow_period": self.slow_period}

    @property
    def warmup_bars(self) -> int:
        return self.slow_period

    def generate(self, bars: pd.DataFrame) -> Sequence[ResearchSignal]:
        _require_frame(bars)
        if bars.empty:
            return ()
        symbol = _single_symbol(bars)
        if not bars["timestamp"].is_monotonic_increasing:
            raise EngineInputError(
                "Bars must be ordered ascending by timestamp. This engine does not sort its "
                "input, because reordering would mask an upstream data-contract violation."
            )

        close = bars[PRICE_COLUMN].astype("float64")
        # `adjust=False` and `min_periods=period` reproduce the production
        # strategy's recursive EMA and its explicit warm-up mask exactly.
        fast = close.ewm(span=self.fast_period, adjust=False, min_periods=self.fast_period).mean()
        slow = close.ewm(span=self.slow_period, adjust=False, min_periods=self.slow_period).mean()
        previous_fast = fast.shift(1)
        previous_slow = slow.shift(1)

        # Every comparison against a warm-up NaN is False, so no bar before
        # both EMAs are defined on this bar and the previous one is actionable.
        crossed_above = (previous_fast <= previous_slow) & (fast > slow)
        crossed_below = (previous_fast >= previous_slow) & (fast < slow)

        signals: list[ResearchSignal] = []
        for position, timestamp in enumerate(bars["timestamp"]):
            if crossed_above.iat[position]:
                action = Action.ENTER_LONG
                reason = f"EMA{self.fast_period}_CROSS_ABOVE_EMA{self.slow_period}"
            elif crossed_below.iat[position]:
                action = Action.EXIT_LONG
                reason = f"EMA{self.fast_period}_CROSS_BELOW_EMA{self.slow_period}"
            else:
                continue
            signals.append(
                ResearchSignal(timestamp=timestamp, symbol=symbol, action=action, reason=reason)
            )
        return tuple(signals)


@dataclass(frozen=True)
class BuyAndHoldEngine:
    """The benchmark: enter on the first actionable bar and never exit.

    Every strategy result is reported against this. A long-only strategy in a
    rising sample earns most of what it earns from the sample, and a comparison
    against buy-and-hold is the cheapest way to see whether anything else
    happened.

    `warmup_bars` defaults to zero but is configurable, so the benchmark can be
    made to start where the strategy it is being compared against starts. A
    benchmark that got a head start of fifty bars is not a benchmark.
    """

    warmup: int = 0
    name: str = "buy-and-hold"
    version: str = "v1"

    def __post_init__(self) -> None:
        if not isinstance(self.warmup, int) or isinstance(self.warmup, bool) or self.warmup < 0:
            raise EngineInputError(f"warmup must be a non-negative int, got {self.warmup!r}.")

    @property
    def parameters(self) -> Mapping[str, object]:
        return {"warmup": self.warmup}

    @property
    def warmup_bars(self) -> int:
        return self.warmup

    def generate(self, bars: pd.DataFrame) -> Sequence[ResearchSignal]:
        _require_frame(bars)
        if bars.empty or len(bars) <= self.warmup:
            return ()
        symbol = _single_symbol(bars)
        entry = bars["timestamp"].iloc[self.warmup]
        return (
            ResearchSignal(
                timestamp=entry,
                symbol=symbol,
                action=Action.ENTER_LONG,
                reason="BENCHMARK_BUY_AND_HOLD",
            ),
        )


@dataclass(frozen=True)
class ScriptedEngine:
    """An engine that emits exactly the signals it was handed. Test scaffolding.

    Lets a test drive the replay simulator through an exact sequence - a BUY
    while already long, an EXIT while flat, a signal on the final bar - without
    reverse-engineering a price series that provokes it from a real strategy.
    Never used by a study.
    """

    signals: tuple[ResearchSignal, ...] = field(default_factory=tuple)
    name: str = "scripted"
    version: str = "v1"
    warmup: int = 0

    @property
    def parameters(self) -> Mapping[str, object]:
        return {"signal_count": len(self.signals)}

    @property
    def warmup_bars(self) -> int:
        return self.warmup

    def generate(self, bars: pd.DataFrame) -> Sequence[ResearchSignal]:
        known = set(bars["timestamp"]) if "timestamp" in bars.columns else set()
        return tuple(signal for signal in self.signals if signal.timestamp in known)


__all__ = [
    "Action",
    "BuyAndHoldEngine",
    "DecisionEngine",
    "EmaCrossEngine",
    "EngineInputError",
    "ParametricEmaCross",
    "ResearchSignal",
    "ScriptedEngine",
    "describe",
]
