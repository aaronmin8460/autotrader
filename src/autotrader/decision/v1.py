"""V1: the existing EMA 20 / EMA 50 crossover, behind the versioned contract.

An adapter, not a reimplementation. Every crossover decision is still computed
by `autotrader.strategies.ema_cross`, which is untouched by this branch and
remains the strategy the production crypto runtime and the equity runtime call
directly. This module wraps its output in a `DecisionResult` so that V1, V2 and
V3 can be compared, stored, replayed, and eventually ensembled through one
shape.

**Why an adapter at all.** A V5 ensemble that wants to weigh the crossover
against a multi-factor score needs both on one scale, and a research harness
comparing versions needs them in one record format. Rewriting the crossover to
produce that format would create a second implementation of the one strategy
this system has actually been running, which is exactly the kind of divergence
this adapter exists to prevent. `to_legacy_signal` converts back, so a caller
already speaking C3's vocabulary loses nothing.

**The mapping is deliberately blunt, because V1 is.** A crossover either
happened on the newest completed bar or it did not: score is ``+1``, ``-1`` or
``0`` and confidence is ``1.0`` or ``0.0``, with no gradation available to
invent. `EXIT` becomes `SELL` - the same instruction under the general name -
and the original reason token is carried through unchanged so a stored V1
decision still says `EMA20_CROSS_BELOW_EMA50`.

**The regime is `UNKNOWN`, and that is not a gap to fill.** V1 measures no
volatility and classifies no regime. Reporting anything else here would put a
judgement in a V1 record that V1 never made.

**One deliberate strictness difference.** C3 accepted duplicate timestamps and
said so, because a duplicate could not make a crossover look like something it
was not. The decision-engine bar contract refuses them (`bars.normalize_bars`),
so this adapter refuses input the underlying strategy would have accepted. That
is the shared contract applying to every version rather than a change to the
crossover, and it only ever rejects data that was already malformed.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

import pandas as pd

from autotrader.decision.bars import normalize_bars
from autotrader.decision.contract import (
    VERSION_V1,
    DecisionResult,
    DecisionSignal,
    MarketRegime,
)
from autotrader.strategies.ema_cross import (
    FAST_EMA_COLUMN,
    FAST_PERIOD,
    SLOW_EMA_COLUMN,
    SLOW_PERIOD,
    Signal,
    SignalType,
    add_ema_columns,
    generate_ema_cross_signals,
)

#: The only HOLD V1 can express: the averages did not cross on this bar.
REASON_NO_CROSSOVER = "NO_CROSSOVER_ON_LATEST_BAR"

#: Bars before a crossover is expressible. The slow EMA needs `SLOW_PERIOD`
#: observations and the crossover additionally reads the previous bar, so this
#: is an arithmetic floor rather than a chosen window.
REQUIRED_BARS = SLOW_PERIOD + 1

#: Which `DecisionSignal` each C3 signal type becomes, and the way back.
_TO_DECISION: Mapping[SignalType, DecisionSignal] = MappingProxyType(
    {SignalType.BUY: DecisionSignal.BUY, SignalType.EXIT: DecisionSignal.SELL}
)
_TO_LEGACY: Mapping[DecisionSignal, SignalType] = MappingProxyType(
    {DecisionSignal.BUY: SignalType.BUY, DecisionSignal.SELL: SignalType.EXIT}
)

_SCORE_FOR: Mapping[DecisionSignal, float] = MappingProxyType(
    {DecisionSignal.BUY: 1.0, DecisionSignal.SELL: -1.0, DecisionSignal.HOLD: 0.0}
)


class EmaCrossV1Engine:
    """The C3 crossover, presented as a `DecisionEngine`.

    Holds no configuration: V1's periods are fixed at 20 and 50 and are
    deliberately not configurable (docs/SPEC.md section 3.3), so there is
    nothing here for a policy to vary.
    """

    @property
    def version(self) -> str:
        """The identifier stored with every decision this engine makes."""
        return VERSION_V1

    @property
    def required_base_bars(self) -> int:
        """Completed base bars before a crossover can be observed at all."""
        return REQUIRED_BARS

    def describe(self) -> Mapping[str, object]:
        """The configuration in force, as serializable values."""
        return MappingProxyType(
            {
                "engine_version": VERSION_V1,
                "policy_name": "ema-cross-v1",
                "timeframes": ["15m"],
                "required_base_bars": REQUIRED_BARS,
                "periods": {"ema_fast": FAST_PERIOD, "ema_slow": SLOW_PERIOD},
            }
        )

    def decide(self, bars: pd.DataFrame) -> DecisionResult:
        """Report whether a crossover occurred on the newest completed bar.

        A crossover on any earlier bar produces HOLD. That is C3's own rule as
        the runtimes apply it - only the newest completed bar may cause an
        action, because every older crossover was already acted on or already
        missed - and it is restated here so the adapter and the runtime cannot
        answer the same question differently.
        """
        frame = normalize_bars(bars)
        symbol = str(frame["symbol"].iloc[0])
        timestamp = pd.Timestamp(frame["timestamp"].iloc[-1])

        enriched = add_ema_columns(frame)
        latest = enriched.iloc[-1]
        features = {
            "ema_fast": float(latest[FAST_EMA_COLUMN]),
            "ema_slow": float(latest[SLOW_EMA_COLUMN]),
        }

        signal, reasons = self._latest_crossover(frame, timestamp)
        metadata = dict(self.describe())
        metadata["bar_count"] = len(frame)
        return DecisionResult(
            version=VERSION_V1,
            symbol=symbol,
            timestamp=timestamp,
            signal=signal,
            score=_SCORE_FOR[signal],
            confidence=0.0 if signal is DecisionSignal.HOLD else 1.0,
            reasons=reasons,
            features=features,
            policy=metadata,
            regime=MarketRegime.UNKNOWN,
        )

    def _latest_crossover(
        self,
        frame: pd.DataFrame,
        timestamp: pd.Timestamp,
    ) -> tuple[DecisionSignal, tuple[str, ...]]:
        """The crossover on `timestamp`, if the newest one landed there."""
        signals = generate_ema_cross_signals(frame)
        if not signals:
            return DecisionSignal.HOLD, (REASON_NO_CROSSOVER,)
        newest = signals[-1]
        if pd.Timestamp(newest.timestamp) != timestamp:
            return DecisionSignal.HOLD, (REASON_NO_CROSSOVER,)
        return _TO_DECISION[newest.type], (newest.reason,)


def to_legacy_signal(result: DecisionResult) -> Signal | None:
    """Convert a `DecisionResult` back into a C3 `Signal`, or None for HOLD.

    The compatibility bridge in the other direction: any engine's actionable
    result can be handed to code written against C3 - the runtimes' signal
    recording and their execution call among it - without that code learning a
    new type. HOLD becomes None because C3 has no way to say "no action" other
    than emitting nothing.
    """
    if result.signal is DecisionSignal.HOLD:
        return None
    return Signal(
        timestamp=result.timestamp,
        symbol=result.symbol,
        type=_TO_LEGACY[result.signal],
        reason=result.reasons[0],
    )


__all__ = [
    "REASON_NO_CROSSOVER",
    "REQUIRED_BARS",
    "EmaCrossV1Engine",
    "to_legacy_signal",
]
