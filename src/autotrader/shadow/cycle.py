"""One bar, one claim, five records, at most one candidate.

This is where shadow mode meets the duplicate-bar guard the system already has.
The sequence is the existing one with the panel dropped into the middle of it,
and the order of the steps is the whole safety argument:

1. **Ask the checkpoint.** A bar at or older than the symbol's durable claim has
   already been acted on. Nothing is evaluated and nothing is recorded, because
   a replayed bar that produced a second set of decisions would also have
   produced a second candidate.
2. **Claim the bar, durably, before deciding.** The claim commits before any
   version sees the frame, so a process that dies between the claim and the
   candidate loses that bar permanently. That is the intended side of the trade
   and it is unchanged from C9: **miss a trade rather than duplicate a trade.**
3. **Evaluate every version.** Five decisions, from one frame, in memory.
4. **Record all of them, atomically.**
5. **Only then release the candidate**, and at most one exists to release.

**Step 4 before step 5 is not cosmetic.** The candidate reaches the caller only
after the record commits, so the storage layer's refusal to hold two execution
candidates for one bar is not merely an audit constraint - it is a second, durable
guard on the thing that costs money. A bar whose decisions cannot be written is a
bar that produces no candidate, whatever the in-process panel computed.

**Three independent things now have to fail** before one completed bar could
become two orders: the checkpoint would have to forget the claim, the panel
would have to offer a second candidate, and the database would have to accept a
second executed row. Each of the three is enforced somewhere the other two
cannot reach.

**This module reaches no broker and asks for none.** It takes a checkpoint by
structural protocol rather than importing the runtime package that owns the
production one, so nothing here imports a module that holds a client. The
protocol below is declared locally and a test pins it to
`autotrader.runtime.checkpoint.ProcessedBarCheckpoint`, which is the same
declare-and-pin arrangement the decision package already uses to keep a provider
library out of a research process.

Nothing in this module submits, sizes, or authorizes anything. It returns a
candidate; the risk engine and the execution layer remain the only things that
can turn one into an order, and a caller that has no gateway simply has nowhere
to take it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

import pandas as pd

from autotrader.shadow.panel import (
    EnginePanel,
    ExecutionCandidate,
    PanelEvaluation,
    ShadowConfigError,
    ShadowError,
)
from autotrader.shadow.recorder import ShadowRecorder

#: Why a bar was passed over without being evaluated. The same token the crypto
#: runtime already reports for the same situation, so an operator reading two
#: logs is reading one vocabulary.
SKIPPED_ALREADY_PROCESSED = "ALREADY_PROCESSED"


class ShadowClaimError(ShadowError):
    """A bar could not be evaluated because its claim is not trustworthy."""


@runtime_checkable
class BarClaim(Protocol):
    """What shadow mode needs from a processed-bar checkpoint.

    Two methods, matching `autotrader.runtime.checkpoint.ProcessedBarCheckpoint`
    exactly. Declared rather than imported so that this package's import graph
    stops at the decision layer and the state layer: the runtime package holds
    the production execution gateway, and a shadow recorder that imported it
    would have a path to a broker whether or not it ever walked one.
    """

    def last_processed(self, symbol: str) -> datetime | None:
        """The newest bar start already processed for `symbol`, if any."""

    def mark_processed(self, symbol: str, bar_timestamp: datetime) -> None:
        """Record `bar_timestamp` as processed for `symbol`."""


@dataclass(frozen=True)
class BarOutcome:
    """What one completed bar produced: a record, and at most one candidate.

    `evaluation` is None exactly when the bar was skipped, and `skipped_reason`
    says why. A skipped bar records nothing and releases nothing, which is the
    correct handling of a bar something has already acted on.
    """

    symbol: str
    bar_timestamp: datetime
    evaluation: PanelEvaluation | None = None
    recorded_ids: tuple[int, ...] = ()
    skipped_reason: str | None = None

    @property
    def claimed(self) -> bool:
        """Whether this bar was claimed and evaluated by this call."""
        return self.evaluation is not None

    @property
    def candidate(self) -> ExecutionCandidate | None:
        """The bar's single execution candidate, or None when there is none."""
        return None if self.evaluation is None else self.evaluation.candidate

    @property
    def recorded_versions(self) -> tuple[str, ...]:
        """Every version whose decision reached the database for this bar."""
        return () if self.evaluation is None else self.evaluation.versions


class ShadowCycle:
    """Evaluates completed bars under the existing at-most-once discipline.

    Holds a panel, a recorder and a checkpoint. It is deliberately not a runtime:
    it does not fetch bars, does not validate them, does not know what time it
    is, and does not decide whether the process may trade. A runtime does all of
    that and calls this for the one bar it has already established is complete.
    """

    def __init__(
        self,
        *,
        panel: EnginePanel,
        recorder: ShadowRecorder,
        checkpoint: BarClaim,
    ) -> None:
        self._panel = panel
        self._recorder = recorder
        self._checkpoint = checkpoint

    @property
    def panel(self) -> EnginePanel:
        return self._panel

    @property
    def recorder(self) -> ShadowRecorder:
        """The recorder this cycle writes through, for the later order linkage."""
        return self._recorder

    @property
    def execution_version(self) -> str:
        """The one version this cycle may release a candidate from."""
        return self._panel.execution_version

    def evaluate_bar(
        self,
        symbol: str,
        bars: pd.DataFrame,
        *,
        bar_timestamp: datetime,
    ) -> BarOutcome:
        """Claim, evaluate, record, and return at most one candidate.

        `bar_timestamp` is the start of the newest completed bar, established by
        the caller. It is checked against what the engines actually decided
        rather than trusted: recording five decisions under a bar label none of
        them used would corrupt the one column a later evaluation joins on.

        A bar already claimed returns a skipped outcome without evaluating
        anything. A claim that cannot be made durable propagates from the
        checkpoint, and nothing is decided - a claim that a restart cannot see is
        not a claim.
        """
        moment = _require_utc(bar_timestamp, "bar_timestamp")
        already = self._checkpoint.last_processed(symbol)
        if already is not None and moment <= _require_utc(already, "last_processed"):
            return BarOutcome(
                symbol=symbol,
                bar_timestamp=moment,
                skipped_reason=SKIPPED_ALREADY_PROCESSED,
            )

        # Claimed before anything is decided, and committed before this returns.
        # A failure after this point loses the bar rather than re-opening it: the
        # decisions below must not be handed to a second attempt, because the
        # candidate among them would be a second candidate for one bar.
        self._checkpoint.mark_processed(symbol, moment)

        evaluation = self._panel.evaluate(bars)
        _require_evaluation_matches(evaluation, symbol, moment)
        recorded = self._recorder.record(evaluation)
        return BarOutcome(
            symbol=symbol,
            bar_timestamp=moment,
            evaluation=evaluation,
            recorded_ids=recorded,
        )


def _require_evaluation_matches(
    evaluation: PanelEvaluation, symbol: str, bar_timestamp: datetime
) -> None:
    """Refuse an evaluation that is not about the bar that was claimed."""
    if evaluation.symbol != symbol:
        raise ShadowConfigError(
            f"The supplied bars produced decisions about {evaluation.symbol!r}, but "
            f"{symbol!r} was claimed. Nothing was recorded."
        )
    if evaluation.timestamp != pd.Timestamp(bar_timestamp):
        raise ShadowConfigError(
            f"The supplied bars produced decisions about the bar at "
            f"{evaluation.timestamp.isoformat()}, but {bar_timestamp.isoformat()} was "
            "claimed. Nothing was recorded."
        )


def _require_utc(moment: datetime, field: str) -> datetime:
    """Return `moment` in UTC, refusing a naive one.

    Naive is refused rather than assumed, for the same reason it is everywhere
    else here: a bar claim compared against a misdated timestamp is a bar claim
    that does not stop anything.
    """
    if not isinstance(moment, datetime):
        raise ShadowClaimError(f"{field} must be a datetime, got {type(moment).__name__}.")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ShadowClaimError(
            f"{field} must be timezone-aware; a naive bar timestamp would be compared "
            "against a claim it cannot be ordered against."
        )
    return moment.astimezone(UTC)


__all__ = [
    "SKIPPED_ALREADY_PROCESSED",
    "BarClaim",
    "BarOutcome",
    "ShadowClaimError",
    "ShadowCycle",
]
