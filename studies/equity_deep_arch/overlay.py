"""Deterministic decision-series transforms: challenger = f(champion, state).

The EDA-1 overlay expresses "V3, except fully participating while the market
trend is intact" as a *target-position* series:

    target(t) = 1                if participate(t)
                V3's own stance  otherwise

where V3's stance is reconstructed from its stored decision series (long after
a BUY, flat after a SELL). Signals are emitted only on target transitions, so
the overlay adds no turnover inside a regime — and hands positions back to V3
without ever holding a position neither layer asked for.

Everything here is a pure function of already-recorded, already-causal inputs;
causality of the result is inherited from the causality of the stored series
(audited in its own study) and of the lagged state series (`state.py`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pandas as pd

from autotrader.decision.contract import DecisionSignal
from studies.equity_v1_v5.adapters import DecisionRecord


class OverlayError(Exception):
    """An overlay asked to combine series that do not describe the same bars."""


def source_stance(records: Sequence[DecisionRecord]) -> list[int]:
    """The stance (0 flat, 1 long) implied by a stored series at each record.

    The stance *at* a record reflects that record's own signal: a BUY bar is
    already stance 1, because regenerating a BUY at that bar reproduces the
    identical next-open fill the source engine got.
    """
    stance = 0
    result: list[int] = []
    for record in records:
        if record.signal is DecisionSignal.BUY:
            stance = 1
        elif record.signal is DecisionSignal.SELL:
            stance = 0
        result.append(stance)
    return result


def participation_overlay(
    records: Sequence[DecisionRecord],
    participate: Mapping[pd.Timestamp, bool],
    *,
    architecture: str,
) -> tuple[DecisionRecord, ...]:
    """The challenger series: long while participating, the source otherwise."""
    if not records:
        raise OverlayError("An overlay needs a non-empty source series.")
    ordered = sorted(records, key=lambda record: record.timestamp)
    stances = source_stance(ordered)

    result: list[DecisionRecord] = []
    held = 0
    for record, stance in zip(ordered, stances, strict=True):
        state = participate.get(record.timestamp)
        if state is None:
            raise OverlayError(
                f"No participation state for bar {record.timestamp}; the state series "
                "must cover every bar of the source series."
            )
        target = 1 if state else stance
        if target == 1 and held == 0:
            signal = DecisionSignal.BUY
            reasons = (
                (f"{architecture}_PARTICIPATE_ENTER",)
                if state
                else tuple(record.reasons) or (f"{architecture}_SOURCE_ENTER",)
            )
        elif target == 0 and held == 1:
            signal = DecisionSignal.SELL
            reasons = tuple(record.reasons) or (f"{architecture}_SOURCE_EXIT",)
        else:
            signal = DecisionSignal.HOLD
            reasons = (f"{architecture}_HOLD",)
        held = target
        result.append(
            DecisionRecord(
                timestamp=record.timestamp,
                symbol=record.symbol,
                signal=signal,
                score=record.score,
                confidence=record.confidence,
                regime="PARTICIPATE" if state else record.regime,
                reasons=reasons,
            )
        )
    return tuple(result)


__all__ = ["OverlayError", "participation_overlay", "source_stance"]
