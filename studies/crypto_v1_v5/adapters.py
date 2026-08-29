"""The seam between the two engine contracts, crossed without either side moving.

`autotrader.decision` engines answer about **one** bar: `decide(bars)` reads the
newest completed bar in the frame it is handed and returns a `DecisionResult`.
`autotrader.research` engines answer about a **frame**: `generate(bars)` returns
every signal derivable from it. Neither contract is wrong and neither is changed
here. This module supplies the two adapters that let the shipped V1-V5 engines
be evaluated by the shipped replay simulator.

**`LiveDecisionEngine` is the faithful one.** It slides the real engine over the
frame, handing it `bars[i - lookback + 1 : i + 1]` at every step, so bar *i* is
the newest completed bar exactly as it would be in a live runtime. It is also
the only one the leakage auditor can say anything about: that auditor perturbs
future bars and re-asks, which is a question about a computation, and a
computation is what this adapter performs.

**`DecisionSeriesEngine` is the fast one, and it is deliberately not auditable.**
It carries an already-computed decision series and replays it. Running the
causality audit against it would produce a pass that means nothing - a lookup
table cannot see the future because it cannot see anything - so this module
refuses to let that pass be mistaken for evidence: `audit_ready` is `False` on
the series adapter and `True` on the live one, and the study asserts on it.
The two are pinned together by a test that drives both over the same bars and
requires identical signals.

**Why the mapping is what it is.** The research contract is long-only with two
actions, so BUY becomes `ENTER_LONG`, SELL becomes `EXIT_LONG`, and HOLD emits
nothing at all - HOLD is the absence of a proposal, not a third instruction, and
the simulator already treats an ENTER while long and an EXIT while flat as
no-ops it counts. `strength` carries the engine's own confidence, which is
already bounded to ``[0, 1]`` by the decision contract, so nothing is rescaled.
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

#: Separator for the reason tokens carried into a research signal. The tokens
#: stay machine-readable and the order stays the engine's own.
REASON_SEPARATOR = "|"


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


class LiveDecisionEngine:
    """Drives a shipped decision engine over a frame, one completed bar at a time.

    `lookback_bars` is the fixed window handed to the engine. It is a study
    parameter rather than the engine's own `required_base_bars` because that
    property is a lower bound that does not account for the alignment cost of
    aggregating 15-minute bars into 4-hour ones: V3 reports it needs 1744 and
    still answers `INSUFFICIENT_HISTORY_4H` at 1744. The study passes one window
    large enough for every engine and asserts that no scored bar came back
    unavailable, which turns that discrepancy into a checked fact.
    """

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
    """Replays an already-computed decision series. Fast, and not evidence of causality.

    `audit_ready` is `False` because a stored series is insensitive to the bars
    it is replayed against by construction: the leakage auditor's perturbation
    would change nothing and report a clean pass that establishes nothing. The
    causality evidence for these engines comes from `LiveDecisionEngine`.
    """

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
        # Keyed by symbol as well as instant, because a portfolio replay drives
        # one engine across several datasets whose bars share timestamps. Keying
        # on the instant alone would serve BTC's decision for an ETH bar.
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
        """The stored proposals falling on the bars of `bars`, in frame order.

        Restricted to the frame's own instants so a walk-forward window replays
        its own slice of the series rather than the whole of it.
        """
        signals: list[ResearchSignal] = []
        symbols = pd.unique(bars["symbol"])
        if len(symbols) != 1:
            raise AdapterError(
                f"{self._name} was given a frame holding {len(symbols)} symbols; a replay "
                "frame holds one."
            )
        symbol = str(symbols[0])
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
    of them twice per bar. Both methods are pure functions of the frame - the
    decision package says so and its tests enforce it - so remembering the last
    answer changes nothing except how long the study takes.

    The cache is one entry deep and keyed on the frame's last instant together
    with its length, which is exactly the identity of a window in this study.
    Nothing here alters what an engine computes; a test drives the memoized and
    unmemoized engines over the same bars and requires identical results.
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
    "REASON_SEPARATOR",
    "AdapterError",
    "DecisionRecord",
    "DecisionSeriesEngine",
    "LiveDecisionEngine",
    "memoize_engine_call",
]
