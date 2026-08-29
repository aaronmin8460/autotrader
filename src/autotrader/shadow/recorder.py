"""Persisting what every version decided. It cannot act on any of them.

The recorder takes a `PanelEvaluation` and writes one row per version into
`shadow_decisions` (schema v7). That is its whole surface. It holds a SQLite
connection and a strategy run id, and there is nothing else in it to hold: no
client, no gateway, no credentials, no socket, and no import that could supply
one. Recording a decision does not require the ability to execute it, and this
module is where that stops being a claim and becomes a fact about what the code
can reach.

**Five rows or none.** One evaluation is written in one transaction, so a bar
never leaves a partial record behind. A comparison across versions that was
missing V4 on some bars because a write failed halfway would be a silently
biased comparison rather than an obviously broken one, and the second is much
easier to notice.

**It records the release, not the order.** The row designated `EXECUTED` says a
candidate was handed to the layers downstream. Whether an order followed is a
separate fact that arrives later, through `link_execution`, and the four
observational rows can never carry it - the schema refuses to attach an order to
a decision that was never allowed to produce one.

**Nothing here decides.** Every value written came out of `autotrader.decision`
and is copied across unchanged. This module does not re-derive a score, does not
recompute a signal, does not re-run the hold band, and cannot promote a version:
which version executes was settled by the panel's configuration and is written
onto every row as a fact about the record, not read from one as an instruction.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from autotrader.shadow.panel import ExecutionCandidate, PanelEvaluation, ShadowObservation
from autotrader.state import sqlite as state


class ShadowRecorder:
    """Writes decisions down. Reaches nothing that could act on one.

    Construct it with an open state connection and, when the decisions belong to
    a runtime session, that session's `strategy_runs` id - which makes every
    recorded decision attributable to the run that produced it, exactly like a
    signal or a risk event.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        strategy_run_id: int | None = None,
    ) -> None:
        self._connection = connection
        self._strategy_run_id = strategy_run_id

    @property
    def strategy_run_id(self) -> int | None:
        """The run every decision recorded through this recorder belongs to."""
        return self._strategy_run_id

    def record(self, evaluation: PanelEvaluation) -> tuple[int, ...]:
        """Write every version's decision for one bar, atomically.

        Returns the row ids in evaluation order. A duplicate - the same version
        deciding the same bar again - raises `DuplicateShadowDecisionError`, and
        a second execution candidate for one bar raises
        `ConflictingExecutedDecisionError`. Both roll the whole evaluation back,
        so a replayed bar leaves the original record exactly as it was rather
        than half-overwritten by a second opinion about the same fifteen minutes.

        The caller gets its candidate only after this returns, which is
        deliberate: the durable record of what was decided is written before the
        one decision that may act is released, so a bar that cannot be recorded
        is also a bar that cannot be executed.
        """
        with state.transaction(self._connection):
            return tuple(self._record_one(observation) for observation in evaluation.observations)

    def _record_one(self, observation: ShadowObservation) -> int:
        """Write one version's decision, copying every value across unchanged."""
        result = observation.result
        return state.record_shadow_decision(
            self._connection,
            strategy_run_id=self._strategy_run_id,
            bar_timestamp=_as_datetime(result.timestamp),
            symbol=result.symbol,
            engine_version=result.version,
            signal=result.signal.value,
            score=float(result.score),
            confidence=float(result.confidence),
            regime=result.regime.value,
            reasons=result.reasons,
            feature_version=observation.feature_version,
            model_version=observation.model_version,
            execution_version=observation.execution_version,
            designation=(
                state.SHADOW_DESIGNATION_EXECUTED
                if observation.executed
                else state.SHADOW_DESIGNATION_NOT_EXECUTED
            ),
        )

    def link_execution(self, candidate: ExecutionCandidate, *, client_order_id: str) -> None:
        """Anchor the executed decision to the order intent it produced.

        Called by whatever created the intent, after risk sized it - never from
        inside this module's own work, because the anchor does not exist when the
        decision is recorded. Writing a string into a column is the entire
        operation; it neither creates the intent nor confirms one exists.
        """
        state.link_shadow_decision_order(
            self._connection,
            symbol=candidate.symbol,
            bar_timestamp=_as_datetime(candidate.timestamp),
            engine_version=candidate.version,
            client_order_id=client_order_id,
        )


def _as_datetime(timestamp: object) -> datetime:
    """A pandas bar timestamp as the aware `datetime` the storage layer takes.

    Converted explicitly rather than passed through. A `pd.Timestamp` is a
    `datetime` subclass and would be accepted, but it serializes on its own
    terms, and an audit column whose exact text depends on which library built
    the value is a column that can disagree with itself.
    """
    converted = timestamp.to_pydatetime() if hasattr(timestamp, "to_pydatetime") else timestamp
    if not isinstance(converted, datetime):
        raise TypeError(f"bar timestamp must be a datetime, got {type(timestamp).__name__}.")
    return converted


__all__ = ["ShadowRecorder"]
