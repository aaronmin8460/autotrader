"""Scoring V1-V5 over the common window, and replaying the result under two cost models.

**Every engine sees the identical bars.** One window list, one lookback, one
frame per symbol. V1 through V5 are scored on exactly the same instants, so a
difference between two engines is a difference between the engines and not
between the intervals they were given.

**Scoring and replaying are separated on purpose.** The expensive part is asking
a live engine for a decision on every bar; the cheap part is turning those
decisions into fills under a cost model. So the decisions are computed once and
stored, and the two cost models replay the same stored series. That is also what
makes the zero-cost and realistic-cost results comparable: they differ by the
cost model alone, because nothing else was recomputed.

**Next-executable-bar semantics, and what that means overnight.** The simulator
fills a proposal at the *following* bar's open. For a proposal on the last
regular-session bar of a day, the following bar is the next session's opening
bar - so the fill happens at the next morning's open, across the overnight gap,
at whatever price the market reopened at. That is the honest execution a
regular-hours strategy actually gets: it cannot fill after 16:00, and pretending
the gap is a fifteen-minute move would be inventing a fill. `overnight_fills`
counts how often it happened so the report can say so.

**A stored decision must equal the live one.** `verify_series_matches_live`
re-drives the real engine over a sample of scored bars and requires the same
answer as the stored series. It is what makes the fast replay path admissible
evidence rather than a shortcut nobody checked.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from autotrader.data.validation import EQUITY_UNIVERSE_LABEL
from autotrader.decision.config import EQUITY_POLICY
from autotrader.decision.probability import ProbabilityArtifact
from autotrader.decision.v1 import EmaCrossV1Engine
from autotrader.decision.v2 import MultiFactorV2Engine
from autotrader.decision.v3 import MultiTimeframeV3Engine
from autotrader.decision.v4 import ProbabilityV4Engine
from autotrader.decision.v5 import EnsembleV5Engine
from autotrader.equity import EQUITY_SYMBOLS
from autotrader.equity.session import market_date
from autotrader.research.costs import EQUITY_COST, STRESS_COST, ZERO_COST, CostModel
from autotrader.research.metrics import EQUITY_15M, metrics_for_replay
from autotrader.research.replay import ReplayConfig, replay
from studies.equity_v1_v5.adapters import (
    DecisionRecord,
    DecisionSeriesEngine,
    LiveDecisionEngine,
)
from studies.equity_v1_v5.windows import LOOKBACK_BARS, ScoringWindow

#: The engines this pilot scores, in version order. The order is the report
#: order and is part of the contract a reader reads the tables under.
ENGINE_NAMES: tuple[str, ...] = ("V1", "V2", "V3", "V4", "V5")

#: Starting capital for every replay. One value across every engine, symbol and
#: cost model, so the equity curves are directly comparable.
INITIAL_CASH = Decimal("100000")

#: The cost models the pilot reports. The zero-cost run is a diagnostic upper
#: bound and is labelled as one; the equity model is the result.
COST_MODELS: tuple[CostModel, ...] = (ZERO_COST, EQUITY_COST, STRESS_COST)


class ScoringError(Exception):
    """A window could not be scored under the study's own rules."""


@dataclass(frozen=True)
class EngineSpec:
    """One engine, and how to build it for a symbol and a window."""

    name: str
    version: str
    build: Callable[[str, ProbabilityArtifact | None], object]
    needs_model: bool


def build_engines() -> tuple[EngineSpec, ...]:
    """The five shipped engines, each under the equity asset-class policy.

    Built through the shipped constructors rather than configured here: the
    pilot evaluates what the system ships, and a locally assembled engine would
    be evaluating this module's idea of one.
    """
    return (
        EngineSpec("V1", "v1", lambda symbol, artifact: EmaCrossV1Engine(), False),
        EngineSpec("V2", "v2", lambda symbol, artifact: MultiFactorV2Engine(EQUITY_POLICY), False),
        EngineSpec(
            "V3",
            "v3",
            lambda symbol, artifact: MultiTimeframeV3Engine(EQUITY_POLICY),
            False,
        ),
        EngineSpec(
            "V4",
            "v4",
            lambda symbol, artifact: ProbabilityV4Engine.for_symbol(symbol, artifact),
            True,
        ),
        EngineSpec(
            "V5",
            "v5",
            lambda symbol, artifact: EnsembleV5Engine.for_symbol(symbol, artifact),
            True,
        ),
    )


def score_window(
    frame: pd.DataFrame,
    window: ScoringWindow,
    spec: EngineSpec,
    *,
    symbol: str,
    artifact: ProbabilityArtifact | None,
    lookback_bars: int = LOOKBACK_BARS,
) -> tuple[DecisionRecord, ...]:
    """Every decision `spec`'s engine reaches on `window`'s bars.

    The engine is handed a window that ends at the scored bar and reaches
    `lookback_bars` back into history that precedes the scoring window. That
    history is read and never scored, which is what a warm-up is.
    """
    if spec.needs_model and artifact is None:
        raise ScoringError(f"{spec.name} needs a trained model and none was supplied.")
    first, last = window.positions(frame)
    if first < lookback_bars - 1:
        raise ScoringError(
            f"{symbol}/{window.name}: the window opens at row {first}, which is less than the "
            f"{lookback_bars}-bar warm-up every engine is given."
        )
    slice_ = frame.iloc[first - lookback_bars + 1 : last + 1].reset_index(drop=True)
    engine = spec.build(symbol, artifact)
    adapter = LiveDecisionEngine(
        engine,
        name=spec.name,
        version=spec.version,
        lookback_bars=lookback_bars,
    )
    return adapter.decisions(slice_)


def decisions_to_frame(records: Sequence[DecisionRecord]) -> pd.DataFrame:
    """The stored form of a decision series."""
    return pd.DataFrame([record.to_row() for record in records])


def frame_to_decisions(frame: pd.DataFrame) -> tuple[DecisionRecord, ...]:
    """Rebuild a decision series from its stored form."""
    return tuple(DecisionRecord.from_row(row) for _, row in frame.iterrows())


def insufficient_history_count(records: Sequence[DecisionRecord]) -> int:
    """How many scored bars the engine declined for want of history.

    The number that makes the warm-up claim checkable. A study reporting a flat
    equity curve because every engine answered "insufficient history" would look
    identical to one reporting a strategy that never traded.
    """
    return sum(1 for record in records if record.insufficient_history)


def overnight_fills(frame: pd.DataFrame, records: Sequence[DecisionRecord]) -> int:
    """How many proposals would fill at the next session's open rather than intraday.

    A proposal on the last regular-session bar of a day has no later bar that
    day, so the simulator's next-bar fill lands on the next session's opening
    print - across the overnight gap. Counted, not corrected: it is what a
    regular-hours strategy actually gets.
    """
    days = {pd.Timestamp(ts): market_date(ts.to_pydatetime()) for ts in frame["timestamp"]}
    ordered = list(frame["timestamp"])
    position = {pd.Timestamp(ts): index for index, ts in enumerate(ordered)}
    count = 0
    for record in records:
        if record.to_signal() is None:
            continue
        index = position.get(record.timestamp)
        if index is None or index + 1 >= len(ordered):
            continue
        if days[pd.Timestamp(ordered[index])] != days[pd.Timestamp(ordered[index + 1])]:
            count += 1
    return count


def replay_series(
    frame: pd.DataFrame,
    records: Sequence[DecisionRecord],
    *,
    name: str,
    version: str,
    cost_model: CostModel,
    initial_cash: Decimal = INITIAL_CASH,
) -> object:
    """Replay a stored decision series over `frame` under one cost model."""
    engine = DecisionSeriesEngine(
        records,
        name=name,
        version=version,
        warmup_bars=0,
        parameters={"cost_model": cost_model.label},
    )
    config = ReplayConfig(
        initial_cash=initial_cash,
        cost_model=cost_model,
        supported_symbols=EQUITY_SYMBOLS,
        universe_label=EQUITY_UNIVERSE_LABEL,
    )
    return replay(frame, engine, config)


def metrics_for(result: object) -> Mapping[str, object]:
    """The pilot's metric block for one replay, on the equity 15-minute clock."""
    metrics = metrics_for_replay(result, EQUITY_15M)
    return metrics.to_json_dict() if hasattr(metrics, "to_json_dict") else dict(metrics.__dict__)


def verify_series_matches_live(
    frame: pd.DataFrame,
    records: Sequence[DecisionRecord],
    spec: EngineSpec,
    *,
    symbol: str,
    artifact: ProbabilityArtifact | None,
    lookback_bars: int = LOOKBACK_BARS,
    samples: int = 12,
) -> tuple[str, ...]:
    """Re-ask the real engine on a sample of scored bars and require the stored answer.

    What makes the stored series admissible. The engine is rebuilt and driven
    over the identical window the scoring pass used, so a mismatch means the
    stored series is not what the engine computes - which would invalidate every
    number derived from it.
    """
    if not records:
        return ("no decisions to verify",)
    engine = spec.build(symbol, artifact)
    position = {pd.Timestamp(ts): index for index, ts in enumerate(frame["timestamp"])}
    step = max(1, len(records) // samples)
    problems: list[str] = []
    for record in list(records)[::step]:
        index = position.get(record.timestamp)
        if index is None or index < lookback_bars - 1:
            continue
        window = frame.iloc[index - lookback_bars + 1 : index + 1].reset_index(drop=True)
        fresh = DecisionRecord.from_result(engine.decide(window))
        if (fresh.signal, round(fresh.score, 9), round(fresh.confidence, 9)) != (
            record.signal,
            round(record.score, 9),
            round(record.confidence, 9),
        ):
            problems.append(
                f"{spec.name}/{symbol} at {record.timestamp.isoformat()}: stored "
                f"{record.signal.value}/{record.score:.6f} but the live engine returned "
                f"{fresh.signal.value}/{fresh.score:.6f}."
            )
    return tuple(problems)


__all__ = [
    "COST_MODELS",
    "ENGINE_NAMES",
    "INITIAL_CASH",
    "LOOKBACK_BARS",
    "EngineSpec",
    "ScoringError",
    "build_engines",
    "decisions_to_frame",
    "frame_to_decisions",
    "insufficient_history_count",
    "metrics_for",
    "overnight_fills",
    "replay_series",
    "score_window",
    "verify_series_matches_live",
]
