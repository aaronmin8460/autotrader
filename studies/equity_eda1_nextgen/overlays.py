"""Decision-series transforms for the Phase-1 refinements and diagnostics.

Same construction discipline as the validated `participation_overlay`: pure
functions of already-recorded, already-causal inputs; signals only on target
transitions; positions handed back to the source engine without ever holding
a position neither layer asked for.

- `freeze_overlay` (P1-C): STRONG targets 1, PULLBACK freezes the sleeve's
  current target, DEFENSIVE targets the source stance.
- `lite_overlay` (diagnostic D2-0): participation targets 1, everything else
  targets 0 — the incumbent overlay with the defensive engine removed, used
  only to quantify the defensive engine's contribution.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pandas as pd

from autotrader.decision.contract import DecisionSignal
from studies.equity_deep_arch.overlay import OverlayError, source_stance
from studies.equity_eda1_nextgen.refined_states import DEFENSIVE, PULLBACK, STRONG
from studies.equity_v1_v5.adapters import DecisionRecord


def _emit(
    record: DecisionRecord,
    target: int,
    held: int,
    *,
    architecture: str,
    regime: str,
) -> DecisionRecord:
    if target == 1 and held == 0:
        signal = DecisionSignal.BUY
        reasons: tuple[str, ...] = (f"{architecture}_ENTER",)
    elif target == 0 and held == 1:
        signal = DecisionSignal.SELL
        reasons = (f"{architecture}_EXIT",)
    else:
        signal = DecisionSignal.HOLD
        reasons = (f"{architecture}_HOLD",)
    return DecisionRecord(
        timestamp=record.timestamp,
        symbol=record.symbol,
        signal=signal,
        score=record.score,
        confidence=record.confidence,
        regime=regime,
        reasons=reasons,
    )


def freeze_overlay(
    records: Sequence[DecisionRecord],
    states: Mapping[pd.Timestamp, str],
    *,
    architecture: str,
) -> tuple[DecisionRecord, ...]:
    """The P1-C series: participate in STRONG, freeze in PULLBACK, source in DEFENSIVE."""
    if not records:
        raise OverlayError("An overlay needs a non-empty source series.")
    ordered = sorted(records, key=lambda record: record.timestamp)
    stances = source_stance(ordered)

    result: list[DecisionRecord] = []
    held = 0
    for record, stance in zip(ordered, stances, strict=True):
        state = states.get(record.timestamp)
        if state is None:
            raise OverlayError(
                f"No state for bar {record.timestamp}; the state series must cover "
                "every bar of the source series."
            )
        if state == STRONG:
            target = 1
        elif state == PULLBACK:
            target = held
        elif state == DEFENSIVE:
            target = stance
        else:
            raise OverlayError(f"Unknown state {state!r} at {record.timestamp}.")
        result.append(_emit(record, target, held, architecture=architecture, regime=state))
        held = target
    return tuple(result)


def lite_overlay(
    records: Sequence[DecisionRecord],
    participate: Mapping[pd.Timestamp, bool],
    *,
    architecture: str,
) -> tuple[DecisionRecord, ...]:
    """The D2-0 diagnostic series: long while participating, flat otherwise."""
    if not records:
        raise OverlayError("An overlay needs a non-empty source series.")
    ordered = sorted(records, key=lambda record: record.timestamp)

    result: list[DecisionRecord] = []
    held = 0
    for record in ordered:
        state = participate.get(record.timestamp)
        if state is None:
            raise OverlayError(
                f"No participation state for bar {record.timestamp}; the state series "
                "must cover every bar of the source series."
            )
        target = 1 if state else 0
        result.append(
            _emit(
                record,
                target,
                held,
                architecture=architecture,
                regime="PARTICIPATE" if state else "FLAT_DEFENSIVE",
            )
        )
        held = target
    return tuple(result)


__all__ = ["freeze_overlay", "lite_overlay"]
