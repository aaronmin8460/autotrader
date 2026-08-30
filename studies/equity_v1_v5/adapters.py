"""The seam between the shipped engine contract and the shipped replay contract.

`autotrader.decision` engines answer about **one** bar: `decide(bars)` reads the
newest completed bar of the frame it is handed. `autotrader.research` engines
answer about a **frame**: `generate(bars)` returns every signal derivable from
it. Neither contract moves here. This module supplies the adapters that let the
shipped V1-V5 engines be evaluated by the shipped replay simulator, on equities.

**Nothing in this seam is asset-class specific, and that is the claim under
test.** The same two adapters serve crypto; if they serve equities unchanged,
then whatever is equity-specific lives in the data, the session filter and the
policy - which is where it should live - rather than in the evaluation harness.
The pilot's job is to check that, not to assume it.

**`LiveDecisionEngine` is the faithful one.** It slides the real engine over the
frame, handing it ``bars[i - lookback + 1 : i + 1]`` at every step, so bar *i* is
the newest completed bar exactly as it would be in a live runtime. Because the
frame it slides over is regular-session-only, the window it hands the engine is
regular-session-only too, and the engine's own 15m -> 1h -> 4h derivation
therefore never sees an extended-hours candle. It is also the only adapter the
leakage auditor can say anything about: that auditor perturbs future bars and
re-asks, which is a question about a computation, and a computation is what this
adapter performs.

**`DecisionSeriesEngine` is the fast one, and it is deliberately not auditable.**
It carries an already-computed decision series and replays it. Running the
causality audit against it would produce a pass that means nothing - a lookup
table cannot see the future because it cannot see anything - so `audit_ready` is
`False` here and `True` on the live adapter, and the study asserts on it.

**The lookback is a study parameter, not the engine's own `required_base_bars`.**
That property is a lower bound: for equities it assumes every session is a full
one and every scheduled bar was published. The pilot measures the real worst case
over the real dataset and passes a window larger than it, then asserts that no
scored bar came back `INSUFFICIENT_HISTORY` - which turns the discrepancy into a
checked fact rather than a silent stream of HOLDs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pandas as pd

from autotrader.decision.contract import DecisionResult, DecisionSignal
from autotrader.research.engines import Action, ResearchSignal

#: How a decision direction becomes a research action. HOLD is absent on
#: purpose: a mapping entry for it would have to invent an action.
ACTION_FOR_SIGNAL: Mapping[DecisionSignal, Action] = {
    DecisionSignal.BUY: Action.ENTER_LONG,
    DecisionSignal.SELL: Action.EXIT_LONG,
}

#: Separator for the reason tokens carried into a research signal.
REASON_SEPARATOR = "|"

#: The reason token an engine emits when it has not been given enough history.
#: Counted rather than tolerated: a study whose engines all answered
#: "insufficient history" would otherwise report a clean flat equity curve.
INSUFFICIENT_PREFIX = "INSUFFICIENT"


class AdapterError(Exception):
    """An adapter was given something it cannot evaluate."""


@dataclass(frozen=True)
class DecisionRecord:
    """One engine's answer on one bar, reduced to what this study replays and reports."""

    timestamp: pd.Timestamp
    symbol: str
    signal: DecisionSignal
    score: float
    confidence: float
    regime: str
    reasons: tuple[str, ...]

    @classmethod
    def from_result(cls, result: DecisionResult) -> DecisionRecord:
        return cls(
            timestamp=result.timestamp,
            symbol=result.symbol,
            signal=result.signal,
            score=float(result.score),
            confidence=float(result.confidence),
            regime=result.regime.value,
            reasons=tuple(result.reasons),
        )

    @property
    def insufficient_history(self) -> bool:
        """Whether the engine declined for want of history rather than deciding."""
        return any(reason.startswith(INSUFFICIENT_PREFIX) for reason in self.reasons)

    def to_signal(self) -> ResearchSignal | None:
        """The research proposal this decision is, or `None` for a HOLD."""
        action = ACTION_FOR_SIGNAL.get(self.signal)
        if action is None:
            return None
        return ResearchSignal(
            timestamp=self.timestamp,
            symbol=self.symbol,
            action=action,
            reason=REASON_SEPARATOR.join(self.reasons),
            strength=self.confidence,
        )

    def to_row(self) -> dict[str, object]:
        """The flat form the stored decision series is written in."""
        return {
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "signal": self.signal.value,
            "score": self.score,
            "confidence": self.confidence,
            "regime": self.regime,
            "reasons": REASON_SEPARATOR.join(self.reasons),
        }

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> DecisionRecord:
        """Rebuild a record from its stored form."""
        reasons = str(row["reasons"])
        return cls(
            timestamp=pd.Timestamp(row["timestamp"]),
            symbol=str(row["symbol"]),
            signal=DecisionSignal(str(row["signal"])),
            score=float(row["score"]),
            confidence=float(row["confidence"]),
            regime=str(row["regime"]),
            reasons=tuple(reasons.split(REASON_SEPARATOR)) if reasons else (),
        )


class LiveDecisionEngine:
    """Drives a shipped decision engine over a frame, one completed bar at a time."""

    audit_ready = True

    def __init__(
        self,
        engine: object,
        *,
        name: str,
        version: str,
        lookback_bars: int,
        parameters: Mapping[str, object] | None = None,
    ) -> None:
        if lookback_bars < 1:
            raise AdapterError(f"lookback_bars must be positive, got {lookback_bars}.")
        if not hasattr(engine, "decide"):
            raise AdapterError(f"{name} does not provide decide().")
        self._engine = engine
        self._name = name
        self._version = version
        self._lookback = int(lookback_bars)
        self._parameters = dict(parameters or {})

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return self._version

    @property
    def parameters(self) -> Mapping[str, object]:
        return dict(self._parameters)

    @property
    def warmup_bars(self) -> int:
        return self._lookback

    def decisions(self, bars: pd.DataFrame) -> tuple[DecisionRecord, ...]:
        """Every decision the engine reaches on `bars`, oldest first.

        The window handed to the engine ends at bar *i* and never extends past
        it, which is the whole no-look-ahead property of this adapter.
        """
        records: list[DecisionRecord] = []
        frame = bars.reset_index(drop=True)
        for index in range(self._lookback - 1, len(frame)):
            window = frame.iloc[index - self._lookback + 1 : index + 1].reset_index(drop=True)
            records.append(DecisionRecord.from_result(self._engine.decide(window)))
        return tuple(records)

    def generate(self, bars: pd.DataFrame) -> Sequence[ResearchSignal]:
        return tuple(
            signal
            for signal in (record.to_signal() for record in self.decisions(bars))
            if signal is not None
        )


class DecisionSeriesEngine:
    """Replays an already-computed decision series. Fast, and not evidence of causality."""

    audit_ready = False

    def __init__(
        self,
        records: Sequence[DecisionRecord],
        *,
        name: str,
        version: str,
        warmup_bars: int,
        parameters: Mapping[str, object] | None = None,
    ) -> None:
        self._name = name
        self._version = version
        self._warmup = int(warmup_bars)
        self._parameters = dict(parameters or {})
        # Keyed by symbol as well as instant: a portfolio replay drives one
        # engine across several datasets whose bars share timestamps, and
        # keying on the instant alone would serve SPY's decision for a QQQ bar.
        self._by_key: dict[tuple[str, pd.Timestamp], DecisionRecord] = {
            (record.symbol, record.timestamp): record for record in records
        }
        if len(self._by_key) != len(records):
            raise AdapterError(
                f"{name} was given more than one decision for the same symbol and instant; "
                "a series with two answers for one bar cannot be replayed."
            )

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return self._version

    @property
    def parameters(self) -> Mapping[str, object]:
        return dict(self._parameters)

    @property
    def warmup_bars(self) -> int:
        return self._warmup

    def generate(self, bars: pd.DataFrame) -> Sequence[ResearchSignal]:
        """The stored proposals falling on the bars of `bars`, in frame order."""
        symbols = pd.unique(bars["symbol"])
        if len(symbols) != 1:
            raise AdapterError(
                f"{self._name} was given a frame holding {len(symbols)} symbols; a replay "
                "frame holds one."
            )
        symbol = str(symbols[0])
        signals: list[ResearchSignal] = []
        for timestamp in bars["timestamp"]:
            record = self._by_key.get((symbol, pd.Timestamp(timestamp)))
            if record is None:
                continue
            signal = record.to_signal()
            if signal is not None:
                signals.append(signal)
        return tuple(signals)


def memoize_engine_call(engine: object, method: str) -> object:
    """Cache one pure engine method against the frame it was last asked about.

    V5 is an ensemble that drives a V3 and a V4 over the identical frame, so a
    study that also wants V3's and V4's own answers would otherwise compute each
    of them twice per bar. Both methods are pure functions of the frame, so
    remembering the last answer changes nothing except how long the study takes.
    The cache is one entry deep and keyed on the frame's last instant together
    with its length - exactly the identity of a window in this study.
    """
    original = getattr(engine, method)
    state: dict[str, object] = {}

    def cached(bars: pd.DataFrame):  # noqa: ANN202 - returns whatever the method returns
        key = (pd.Timestamp(bars["timestamp"].iloc[-1]), len(bars))
        if state.get("key") != key:
            state["key"] = key
            state["value"] = original(bars)
        return state["value"]

    setattr(engine, method, cached)
    return engine


__all__ = [
    "ACTION_FOR_SIGNAL",
    "INSUFFICIENT_PREFIX",
    "REASON_SEPARATOR",
    "AdapterError",
    "DecisionRecord",
    "DecisionSeriesEngine",
    "LiveDecisionEngine",
    "memoize_engine_call",
]
