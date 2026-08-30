"""One V5 pass, three decision series: V3, V4 and V5 from a single computation.

The pilot verified the property this module exploits: ``EnsembleAssessment``
carries V3's **complete** ``DecisionResult`` in ``.deterministic`` (identical to
a standalone V3 decision on the same window - same signal, same score to 1e-12)
and V4's complete ``ProbabilityAssessment`` in ``.probabilistic``. Scoring V3,
V4 and V5 separately therefore computes V3 and V4 twice each; the pilot
measured the single-pass recovery as a 42% saving on total scoring cost. This
study implements it - and then **proves bit-identity on sampled real bars for
every symbol and window** rather than trusting the pilot's two symbols
(`verify_recovered_records`).

**How each record is recovered.**

- *V5*: ``engine.decide(window)`` - the shipped path, unchanged. The engine's
  ``assess`` is memoized one call deep (`memoize_engine_call`, the pilot's own
  helper), so ``decide`` and the recovery below share one computation.
- *V3*: ``DecisionRecord.from_result(assessment.deterministic)`` - the V3
  result verbatim, no reconstruction at all.
- *V4*: the shipped ``ProbabilityV4Engine.decide`` is ``assess`` plus the
  shipped ``decide_signal`` gate; the assessment is in hand, so the same gate
  is applied to it here, token for token. Any drift between this reconstruction
  and the real ``decide`` is exactly what `verify_recovered_records` exists to
  catch, and it compares whole records - signal, score, confidence, regime and
  every reason token.
"""

from __future__ import annotations

import pandas as pd

from autotrader.decision.contract import DecisionSignal
from autotrader.decision.probability import ProbabilityArtifact
from autotrader.decision.scoring import decide_signal
from autotrader.decision.v3 import MultiTimeframeV3Engine
from autotrader.decision.v4 import ProbabilityAssessment, ProbabilityV4Engine
from autotrader.decision.v5 import EnsembleV5Engine
from studies.equity_10_full.windows import LOOKBACK_BARS
from studies.equity_v1_v5.adapters import DecisionRecord, memoize_engine_call
from studies.equity_v1_v5.windows import ScoringWindow


class TripleScoringError(Exception):
    """The single-pass recovery could not be run or verified honestly."""


def v4_record_from_assessment(assessment: ProbabilityAssessment, thresholds) -> DecisionRecord:
    """The V4 decision record the shipped ``decide`` would emit for `assessment`."""
    if not assessment.available:
        signal = DecisionSignal.HOLD
        reasons: tuple[str, ...] = tuple(assessment.reasons)
    else:
        signal, gate_reasons = decide_signal(
            score=assessment.score,
            confidence=assessment.confidence,
            regime=assessment.regime,
            thresholds=thresholds,
        )
        reasons = (*gate_reasons, *assessment.reasons)
    return DecisionRecord(
        timestamp=assessment.timestamp,
        symbol=assessment.symbol,
        signal=signal,
        score=float(assessment.score),
        confidence=float(assessment.confidence),
        regime=assessment.regime.value,
        reasons=reasons,
    )


def score_window_triple(
    frame: pd.DataFrame,
    window: ScoringWindow,
    *,
    symbol: str,
    artifact: ProbabilityArtifact,
    lookback_bars: int = LOOKBACK_BARS,
    max_bars: int | None = None,
) -> dict[str, tuple[DecisionRecord, ...]]:
    """Every V3, V4 and V5 decision on `window`'s bars, from one V5 drive.

    `max_bars` exists for the deterministic-reproduction check, which rescores
    the head of a window and compares; a full scoring pass leaves it `None`.
    """
    first, last = window.positions(frame)
    if first < lookback_bars - 1:
        raise TripleScoringError(
            f"{symbol}/{window.name}: the window opens at row {first}, which is less than "
            f"the {lookback_bars}-bar warm-up every engine is given."
        )
    if max_bars is not None:
        last = min(last, first + max_bars - 1)

    engine = EnsembleV5Engine.for_symbol(symbol, artifact)
    memoize_engine_call(engine, "assess")
    thresholds = engine.probabilistic.policy.thresholds

    v3_records: list[DecisionRecord] = []
    v4_records: list[DecisionRecord] = []
    v5_records: list[DecisionRecord] = []
    for index in range(first, last + 1):
        window_slice = frame.iloc[index - lookback_bars + 1 : index + 1].reset_index(drop=True)
        v5_records.append(DecisionRecord.from_result(engine.decide(window_slice)))
        assessment = engine.assess(window_slice)
        v3_records.append(DecisionRecord.from_result(assessment.deterministic))
        v4_records.append(v4_record_from_assessment(assessment.probabilistic, thresholds))

    return {"V3": tuple(v3_records), "V4": tuple(v4_records), "V5": tuple(v5_records)}


def _whole_record(record: DecisionRecord) -> tuple[object, ...]:
    """Everything a stored decision is, reduced for exact comparison."""
    return (
        str(record.timestamp),
        record.symbol,
        record.signal.value,
        record.score,
        record.confidence,
        record.regime,
        record.reasons,
    )


def verify_recovered_records(
    frame: pd.DataFrame,
    *,
    symbol: str,
    artifact: ProbabilityArtifact,
    recovered_v3: tuple[DecisionRecord, ...],
    recovered_v4: tuple[DecisionRecord, ...],
    lookback_bars: int = LOOKBACK_BARS,
    samples: int = 8,
) -> tuple[str, ...]:
    """Prove sampled recovered V3/V4 records equal the standalone engines' own.

    Bit-identity, not tolerance: signal, score, confidence, regime and every
    reason token must match exactly. A single mismatch disqualifies the
    single-pass recovery for that window and the caller must rescore the
    engines separately.
    """
    if not recovered_v3:
        return ("no records to verify",)
    v3_engine = MultiTimeframeV3Engine.for_symbol(symbol)
    v4_engine = ProbabilityV4Engine.for_symbol(symbol, artifact)
    position = {pd.Timestamp(ts): index for index, ts in enumerate(frame["timestamp"])}
    step = max(1, len(recovered_v3) // samples)
    problems: list[str] = []
    for v3_stored, v4_stored in zip(
        list(recovered_v3)[::step], list(recovered_v4)[::step], strict=True
    ):
        index = position.get(v3_stored.timestamp)
        if index is None or index < lookback_bars - 1:
            continue
        window_slice = frame.iloc[index - lookback_bars + 1 : index + 1].reset_index(drop=True)
        v3_fresh = DecisionRecord.from_result(v3_engine.decide(window_slice))
        v4_fresh = DecisionRecord.from_result(v4_engine.decide(window_slice))
        if _whole_record(v3_fresh) != _whole_record(v3_stored):
            problems.append(
                f"V3/{symbol} at {v3_stored.timestamp.isoformat()}: recovered record is not "
                f"bit-identical to the standalone engine's."
            )
        if _whole_record(v4_fresh) != _whole_record(v4_stored):
            problems.append(
                f"V4/{symbol} at {v4_stored.timestamp.isoformat()}: recovered record is not "
                f"bit-identical to the standalone engine's."
            )
    return tuple(problems)


__all__ = [
    "TripleScoringError",
    "score_window_triple",
    "v4_record_from_assessment",
    "verify_recovered_records",
]
