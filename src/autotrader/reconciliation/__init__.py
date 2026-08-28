"""C8: reconciliation and crash recovery against Alpaca **paper**.

One question, answered honestly:

    reconcile_paper_state(connection) -> ReconciliationResult
                                      -> .safe_to_trade

**The authority hierarchy.** The broker is the truth about orders, fills, and
positions. Local SQLite is durable intent, an audit trail, and a last-known
snapshot. Where they disagree, the snapshot is rewritten from the broker.

**Reconciliation observes and repairs. It never invents a trade.** No `UNKNOWN`
intent is resubmitted, no `client_order_id` is regenerated, no submission is
retried, no replacement order is created, and no offsetting order is placed to
correct a position mismatch. This package imports nothing that could place an
order, and a source-level test asserts that against its executable code.

**`safe_to_trade` fails closed.** Only `CLEAN` and `REPAIRED` permit trading.
An ambiguous order lookup, a broker that cannot be read, or a client that
cannot be *proven* to reach Alpaca paper all answer no, and each says why.

`models` is the provider-neutral vocabulary - standard library only, so the
result object a future runtime consults carries no broker SDK with it.
`engine` is the pass, and it reaches the broker exclusively through
`autotrader.execution.paper`'s read-only helpers, so the whole repository still
has exactly one file that speaks to Alpaca.

**Phase 9 is not here.** There is no loop, no scheduler, no bar polling, no
heartbeat, and no daemon. This package runs once, when asked, and returns.
"""

from autotrader.reconciliation.engine import (
    EVENT_RECONCILED,
    NOT_FOUND_CONFIRMATIONS,
    NOT_FOUND_RECHECK_DELAY_SECONDS,
    RECONCILABLE_INTENT_STATUSES,
    TERMINAL_BROKER_STATUSES,
    ReconciliationError,
    ReconciliationInputError,
    reconcile_paper_state,
)
from autotrader.reconciliation.models import (
    BLOCKING_OUTCOMES,
    CATEGORY_ORDER,
    CATEGORY_POSITION,
    CATEGORY_RUN,
    SAFE_TO_TRADE_STATUSES,
    ItemOutcome,
    ReconciliationIssue,
    ReconciliationResult,
    ReconciliationStatus,
)

__all__ = [
    "BLOCKING_OUTCOMES",
    "CATEGORY_ORDER",
    "CATEGORY_POSITION",
    "CATEGORY_RUN",
    "EVENT_RECONCILED",
    "NOT_FOUND_CONFIRMATIONS",
    "NOT_FOUND_RECHECK_DELAY_SECONDS",
    "RECONCILABLE_INTENT_STATUSES",
    "SAFE_TO_TRADE_STATUSES",
    "TERMINAL_BROKER_STATUSES",
    "ItemOutcome",
    "ReconciliationError",
    "ReconciliationInputError",
    "ReconciliationIssue",
    "ReconciliationResult",
    "ReconciliationStatus",
    "reconcile_paper_state",
]
