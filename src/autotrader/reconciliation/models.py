"""C8 reconciliation vocabulary. No broker SDK, no network, no database.

The result of a reconciliation pass, expressed in terms a caller can act on
without knowing anything about Alpaca or SQLite. Like `execution.models`, this
module imports only the standard library, so the thing a future runtime asks
its question of cannot drag a broker client into whatever process asks.

**The question this vocabulary exists to answer is `safe_to_trade`.** It is a
derived property, not a stored field: it reads off `status` through one rule
written down once, so a result cannot be constructed that says `UNRESOLVED`
and `safe_to_trade=True` at the same time. Only `CLEAN` and `REPAIRED` permit
trading; `UNRESOLVED` and `FAILED` do not, and a pass that never finished
produces no result at all.

**An issue is evidence, not an alarm.** A `REPAIRED` issue records a difference
that was resolved from broker truth, and an `OBSERVED` one records something
worth writing down that changed nothing. Neither blocks. Only `UNRESOLVED` and
`FAILED` do, and `unresolved_count` counts exactly those.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

#: The audit categories an issue can belong to. `RUN` is the pass itself -
#: a failure to reach the broker at all belongs to no single order or symbol.
CATEGORY_ORDER = "ORDER"
CATEGORY_POSITION = "POSITION"
CATEGORY_RUN = "RUN"


class ReconciliationStatus(Enum):
    """What one reconciliation pass concluded.

    Four values, and deliberately no fifth. The distinction that matters is
    between `UNRESOLVED` - the pass ran and found something it could not settle
    - and `FAILED` - the pass could not run. Both stop trading, but they call
    for different operator action, and collapsing them would hide which.
    """

    CLEAN = "CLEAN"
    REPAIRED = "REPAIRED"
    UNRESOLVED = "UNRESOLVED"
    FAILED = "FAILED"


#: The only statuses that permit trading. Written once, here.
SAFE_TO_TRADE_STATUSES: tuple[ReconciliationStatus, ...] = (
    ReconciliationStatus.CLEAN,
    ReconciliationStatus.REPAIRED,
)


class ItemOutcome(Enum):
    """What happened to one reconciled order or position.

    `OBSERVED` is the outcome for evidence that changed nothing locally and is
    not a problem - a broker position in a pair this system does not trade, an
    account flag worth recording. It is kept distinct from `CLEAN` so that
    "nothing to say" and "something to say that is not wrong" do not read the
    same in the audit trail.
    """

    CLEAN = "CLEAN"
    REPAIRED = "REPAIRED"
    UNRESOLVED = "UNRESOLVED"
    FAILED = "FAILED"
    OBSERVED = "OBSERVED"


#: Outcomes that stop a runtime from trading.
BLOCKING_OUTCOMES: tuple[ItemOutcome, ...] = (ItemOutcome.UNRESOLVED, ItemOutcome.FAILED)


@dataclass(frozen=True)
class ReconciliationIssue:
    """One thing a pass repaired, observed, or could not settle.

    `detail` is the human-readable *why*, and it is assembled from symbols,
    quantities, statuses, and `client_order_id` values only - never from a
    credential, a header, or an account number. It goes into the audit trail
    verbatim.
    """

    category: str
    outcome: ItemOutcome
    detail: str
    symbol: str | None = None
    client_order_id: str | None = None

    @property
    def blocking(self) -> bool:
        """Whether this issue on its own is a reason not to start trading."""
        return self.outcome in BLOCKING_OUTCOMES


@dataclass(frozen=True)
class ReconciliationResult:
    """Everything one reconciliation pass concluded.

    The single object a future runtime consults before it trades. It is
    returned only by a pass that *finished*: a process that died mid-pass
    returns nothing at all, which is the honest answer and is not permission.

    `orders_checked` counts intents actually queried at the broker, not intents
    examined - an intent whose broker order is already in a terminal state is
    settled and is not re-queried, and counting it would overstate what this
    pass verified. `positions_checked` counts the symbols in the active
    universe, each of which is reconciled whether or not the broker holds one.
    """

    status: ReconciliationStatus
    started_at: datetime
    completed_at: datetime
    orders_checked: int = 0
    positions_checked: int = 0
    issues: tuple[ReconciliationIssue, ...] = ()
    dry_run: bool = False
    reconciliation_run_id: int | None = None

    @property
    def safe_to_trade(self) -> bool:
        """Whether a runtime may begin trading. **The** startup question.

        Derived from `status` rather than stored alongside it, so the two can
        never disagree. `UNRESOLVED` and `FAILED` both answer no.
        """
        return self.status in SAFE_TO_TRADE_STATUSES

    @property
    def unresolved_count(self) -> int:
        """How many issues are, on their own, reasons not to trade."""
        return sum(1 for issue in self.issues if issue.blocking)

    @property
    def repaired_count(self) -> int:
        """How many differences were resolved from verified broker truth."""
        return sum(1 for issue in self.issues if issue.outcome is ItemOutcome.REPAIRED)

    @property
    def issues_count(self) -> int:
        """Every recorded issue: repairs, observations, and blockers alike."""
        return len(self.issues)

    def blocking_issues(self) -> tuple[ReconciliationIssue, ...]:
        """Just the issues that stop trading, in the order they were found."""
        return tuple(issue for issue in self.issues if issue.blocking)


__all__ = [
    "BLOCKING_OUTCOMES",
    "CATEGORY_ORDER",
    "CATEGORY_POSITION",
    "CATEGORY_RUN",
    "SAFE_TO_TRADE_STATUSES",
    "ItemOutcome",
    "ReconciliationIssue",
    "ReconciliationResult",
    "ReconciliationStatus",
]
