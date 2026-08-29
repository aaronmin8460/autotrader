"""The pass that asks all five engines about every bar, under one shared window.

**One window for all five.** Every engine is handed the identical frame at every
decision instant, so the comparison measures the engines rather than the amount
of history each was given. The window is a study constant rather than each
engine's own `required_base_bars`, and it is larger than the largest of them:
V3 reports 1744 and still answers `INSUFFICIENT_HISTORY_4H` at 1744, because
that figure does not include the cost of aligning 15-minute bars onto 4-hour
boundaries when bars are also missing. `SHARED_LOOKBACK_BARS` is set past the
observed worst case, and `score_window` asserts that nothing came back
unavailable, which converts an assumption into a checked property of the run.

**V5 recomputes its own components, so the components are computed once.**
`EnsembleV5Engine.assess` drives a V3 and a V4 over the frame it was given. A
study that also reports V3's and V4's own answers would compute each of them
twice per bar. Building V5 out of the same two engine objects the study asks
directly, with their pure methods memoized one frame deep, removes the
duplication without changing a single number - and the equivalence is tested
rather than asserted in prose.

**The pass is chunked so it can be run in parallel.** A chunk is a contiguous
run of decision instants plus the lookback each of them needs. Chunks share no
state and their results concatenate in timestamp order, so splitting the work
across processes produces the same series as running it in one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pandas as pd

from autotrader.decision.contract import (
    VERSION_V1,
    VERSION_V2,
    VERSION_V3,
    VERSION_V4,
    VERSION_V5,
)
from autotrader.decision.ensemble import BALANCED_ENSEMBLE, EnsembleSpec
from autotrader.decision.probability import ProbabilityArtifact, artifact_from_record
from autotrader.decision.v1 import EmaCrossV1Engine
from autotrader.decision.v2 import MultiFactorV2Engine
from autotrader.decision.v3 import MultiTimeframeV3Engine
from autotrader.decision.v4 import ProbabilityV4Engine
from autotrader.decision.v5 import EnsembleV5Engine
from studies.crypto_v1_v5.adapters import DecisionRecord, memoize_engine_call

#: The window every engine is handed at every decision instant.
#:
#: Above the largest declared requirement (V3's 1744) and above the worst case
#: actually observed on this dataset (1869), with room left over. Larger than
#: necessary costs time and nothing else: an engine reads its own trailing
#: window out of whatever it is handed.
SHARED_LOOKBACK_BARS = 2400

#: The versions this study compares, in the order they were built.
STUDY_VERSIONS: tuple[str, ...] = (VERSION_V1, VERSION_V2, VERSION_V3, VERSION_V4, VERSION_V5)

#: Reason tokens that mean "this engine could not answer for this bar".
UNAVAILABLE_TOKEN = "INSUFFICIENT_HISTORY"
FEATURE_UNAVAILABLE_TOKEN = "FEATURE_UNAVAILABLE"

#: Columns of a scored decision series.
DECISION_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "symbol",
    "engine",
    "signal",
    "score",
    "confidence",
    "regime",
    "reasons",
)


class ScoringError(Exception):
    """The scoring pass cannot produce a trustworthy series."""


@dataclass(frozen=True)
class EnginePanel:
    """The five shipped engines for one symbol, wired so nothing is computed twice."""

    symbol: str
    v1: EmaCrossV1Engine
    v2: MultiFactorV2Engine
    v3: MultiTimeframeV3Engine
    v4: ProbabilityV4Engine
    v5: EnsembleV5Engine

    def ordered(self) -> tuple[tuple[str, object], ...]:
        """The engines in evaluation order.

        V3 and V4 are asked before V5 so that V5's own calls to them are served
        from the memo rather than recomputed. The order changes performance and
        not results: each engine's answer depends on the frame alone.
        """
        return (
            (VERSION_V1, self.v1),
            (VERSION_V2, self.v2),
            (VERSION_V3, self.v3),
            (VERSION_V4, self.v4),
            (VERSION_V5, self.v5),
        )

    def describe(self) -> dict[str, object]:
        return {version: dict(engine.describe()) for version, engine in self.ordered()}


def build_panel(
    symbol: str,
    artifact: ProbabilityArtifact,
    *,
    spec: EnsembleSpec = BALANCED_ENSEMBLE,
    memoize: bool = True,
) -> EnginePanel:
    """Build V1-V5 for `symbol`, with V5 sharing the study's own V3 and V4.

    `EnsembleV5Engine.for_symbol` would construct a second V3 and a second V4.
    Passing the study's instances instead yields the identical engine - same
    classes, same policy, same artifact, same specification - whose components
    the study can also interrogate directly.
    """
    v3 = MultiTimeframeV3Engine.for_symbol(symbol)
    v4 = ProbabilityV4Engine.for_symbol(symbol, artifact)
    v5 = EnsembleV5Engine(deterministic=v3, probabilistic=v4, spec=spec)
    if memoize:
        memoize_engine_call(v3, "decide")
        memoize_engine_call(v4, "assess")
    return EnginePanel(
        symbol=symbol,
        v1=EmaCrossV1Engine(),
        v2=MultiFactorV2Engine.for_symbol(symbol),
        v3=v3,
        v4=v4,
        v5=v5,
    )


def is_unavailable(reasons: Sequence[str]) -> bool:
    """Whether an engine declined this bar for want of history or a feature."""
    return any(
        UNAVAILABLE_TOKEN in reason or FEATURE_UNAVAILABLE_TOKEN in reason for reason in reasons
    )


def score_window(
    bars: pd.DataFrame,
    panel: EnginePanel,
    *,
    first_decision_index: int,
    last_decision_index: int,
    lookback_bars: int = SHARED_LOOKBACK_BARS,
    require_available: bool = True,
) -> pd.DataFrame:
    """Ask every engine about every bar in ``[first, last]``, inclusive.

    `first_decision_index` must leave `lookback_bars` of history behind it. The
    frame handed to an engine ends at the bar being decided and never extends
    past it, which is the no-look-ahead property of this pass.
    """
    frame = bars.reset_index(drop=True)
    if first_decision_index < lookback_bars - 1:
        raise ScoringError(
            f"Decision index {first_decision_index} leaves fewer than {lookback_bars} bars "
            "of history behind it; the window would run off the start of the frame."
        )
    if last_decision_index >= len(frame):
        raise ScoringError(
            f"Decision index {last_decision_index} is past the end of a {len(frame)}-bar frame."
        )

    rows: list[dict[str, object]] = []
    engines = panel.ordered()
    for index in range(first_decision_index, last_decision_index + 1):
        window = frame.iloc[index - lookback_bars + 1 : index + 1].reset_index(drop=True)
        for version, engine in engines:
            result = engine.decide(window)
            reasons = tuple(result.reasons)
            if require_available and is_unavailable(reasons):
                raise ScoringError(
                    f"{version} could not answer for {result.timestamp.isoformat()} with "
                    f"{lookback_bars} bars of history: {reasons}. The shared lookback is too "
                    "short, and scoring an engine on bars it declined would compare a "
                    "strategy with a warm-up notice."
                )
            rows.append(
                {
                    "timestamp": result.timestamp,
                    "symbol": result.symbol,
                    "engine": version,
                    "signal": result.signal.value,
                    "score": float(result.score),
                    "confidence": float(result.confidence),
                    "regime": result.regime.value,
                    "reasons": "|".join(reasons),
                }
            )
    return pd.DataFrame(rows, columns=list(DECISION_COLUMNS))


def records_from_frame(decisions: pd.DataFrame, engine: str) -> tuple[DecisionRecord, ...]:
    """One engine's rows of a scored series, as the records an adapter replays."""
    from autotrader.decision.contract import DecisionSignal

    subset = decisions[decisions["engine"] == engine]
    return tuple(
        DecisionRecord(
            timestamp=pd.Timestamp(row.timestamp),
            symbol=str(row.symbol),
            signal=DecisionSignal(row.signal),
            score=float(row.score),
            confidence=float(row.confidence),
            regime=str(row.regime),
            reasons=tuple(str(row.reasons).split("|")) if row.reasons else ("",),
        )
        for row in subset.itertuples(index=False)
    )


# --------------------------------------------------------------------------
# Chunked execution, for running the pass across processes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoringChunk:
    """A contiguous run of decision instants, and the model in force over it."""

    symbol: str
    first_decision_index: int
    last_decision_index: int
    artifact_record: Mapping[str, object]
    fold_id: str

    @property
    def decision_count(self) -> int:
        return self.last_decision_index - self.first_decision_index + 1


def score_chunk(
    bars: pd.DataFrame,
    chunk: ScoringChunk,
    *,
    lookback_bars: int = SHARED_LOOKBACK_BARS,
    spec: EnsembleSpec = BALANCED_ENSEMBLE,
) -> pd.DataFrame:
    """Score one chunk. Self-contained, so it can run in any process."""
    artifact = artifact_from_record(chunk.artifact_record)
    panel = build_panel(chunk.symbol, artifact, spec=spec)
    scored = score_window(
        bars,
        panel,
        first_decision_index=chunk.first_decision_index,
        last_decision_index=chunk.last_decision_index,
        lookback_bars=lookback_bars,
    )
    scored["fold_id"] = chunk.fold_id
    scored["model_version"] = artifact.model_version
    scored["model_family"] = artifact.family
    return scored


def plan_chunks(
    symbol: str,
    *,
    first_decision_index: int,
    last_decision_index: int,
    artifact_record: Mapping[str, object],
    fold_id: str,
    chunk_size: int,
) -> tuple[ScoringChunk, ...]:
    """Split one fold's decision range into chunks of at most `chunk_size` instants."""
    if chunk_size < 1:
        raise ScoringError(f"chunk_size must be positive, got {chunk_size}.")
    chunks: list[ScoringChunk] = []
    start = first_decision_index
    while start <= last_decision_index:
        stop = min(start + chunk_size - 1, last_decision_index)
        chunks.append(
            ScoringChunk(
                symbol=symbol,
                first_decision_index=start,
                last_decision_index=stop,
                artifact_record=dict(artifact_record),
                fold_id=fold_id,
            )
        )
        start = stop + 1
    return tuple(chunks)


__all__ = [
    "DECISION_COLUMNS",
    "SHARED_LOOKBACK_BARS",
    "STUDY_VERSIONS",
    "EnginePanel",
    "ScoringChunk",
    "ScoringError",
    "build_panel",
    "is_unavailable",
    "plan_chunks",
    "records_from_frame",
    "score_chunk",
    "score_window",
]
