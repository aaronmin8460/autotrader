"""What the local database says this system has already tried to do.

Read-only queries over `order_intents`, `broker_orders`, `reconciliation_runs`
and `positions`, phrased as the questions a smoke actually asks: what is still
open, what is `UNKNOWN`, what did the last reconciliation conclude, and how
many orders exist for the smoke symbol.

**Local intents are a complete record of this system's attempts.** Every
submission path persists an intent *before* it calls the broker
(`execution.paper.execute_paper_order`), so an order this system placed cannot
exist at the broker without a row here. That is what lets the final audit
answer "was there an unexpected second order?" from the database rather than by
listing the broker's whole order history - and it is also the limit of the
claim: an order placed by hand in Alpaca's web UI leaves no row, and would show
up only as a position that does not match. The audit says so in its output.

Nothing here writes, and the connection it is handed cannot write anyway.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from autotrader.reconciliation.engine import TERMINAL_BROKER_STATUSES
from autotrader.smoke.readonly import normalize_smoke_symbol
from autotrader.state import sqlite as state

#: Intent statuses whose real outcome is still only knowable from the broker.
#: Imported in spirit from `reconciliation.engine.RECONCILABLE_INTENT_STATUSES`
#: and narrowed: `SUBMITTED` is handled separately, because whether it is still
#: open depends on its broker snapshot rather than on the intent status alone.
UNSETTLED_INTENT_STATUSES: tuple[str, ...] = (
    state.INTENT_STATUS_CREATED,
    state.INTENT_STATUS_SUBMITTING,
    state.INTENT_STATUS_UNKNOWN,
)


def unknown_intents(connection: sqlite3.Connection) -> tuple[state.StoredOrderIntent, ...]:
    """Every intent recorded as `UNKNOWN`. The one that stops a smoke starting.

    An `UNKNOWN` intent means a submission ended without a knowable outcome and
    an order may exist at the broker. Beginning a new smoke on top of one is
    how a single test order becomes two live ones, so the preflight blocks on
    any row here.
    """
    return tuple(
        intent
        for intent in state.list_order_intents(connection)
        if intent.status == state.INTENT_STATUS_UNKNOWN
    )


def open_intents(connection: sqlite3.Connection) -> tuple[state.StoredOrderIntent, ...]:
    """Every intent that is not finished, by the same rule reconciliation uses.

    An intent is finished only when it is `SUBMITTED` and its stored broker
    snapshot already reports a status that cannot change, or when it reached a
    terminal intent status. Everything else is open - including a
    `partially_filled` order, which can still fill, and any `SUBMITTED` intent
    with no snapshot at all, which is a gap this harness must not paper over.
    """
    snapshots = {order.order_intent_id: order for order in state.list_broker_orders(connection)}
    open_rows: list[state.StoredOrderIntent] = []
    for intent in state.list_order_intents(connection):
        if intent.status in state.TERMINAL_INTENT_STATUSES:
            continue
        if intent.status in UNSETTLED_INTENT_STATUSES:
            open_rows.append(intent)
            continue
        snapshot = snapshots.get(intent.id)
        if snapshot is None or not is_terminal_broker_status(snapshot.status):
            open_rows.append(intent)
    return tuple(open_rows)


def is_terminal_broker_status(status: str) -> bool:
    """Whether a broker status cannot change again.

    Uses reconciliation's own conservative set rather than a second list here,
    so the harness and the runtime agree on what "finished" means.
    """
    return str(status).strip().lower() in TERMINAL_BROKER_STATUSES


def intents_for_symbol(
    connection: sqlite3.Connection, symbol: str
) -> tuple[state.StoredOrderIntent, ...]:
    """Every intent ever recorded for one symbol, oldest first."""
    ticker = normalize_smoke_symbol(symbol)
    return tuple(
        intent
        for intent in state.list_order_intents(connection)
        if normalize_smoke_symbol(intent.symbol) == ticker
    )


def intents_by_side(
    intents: Sequence[state.StoredOrderIntent], side: str
) -> tuple[state.StoredOrderIntent, ...]:
    """The subset of `intents` on one side, `BUY` or `SELL`."""
    wanted = side.strip().upper()
    return tuple(intent for intent in intents if intent.side.strip().upper() == wanted)


def find_intent(
    connection: sqlite3.Connection, client_order_id: str
) -> state.StoredOrderIntent | None:
    """The locally recorded intent for one `client_order_id`, if any."""
    return state.get_order_intent_by_client_id(connection, client_order_id.strip())


def find_broker_snapshot(
    connection: sqlite3.Connection, client_order_id: str
) -> state.StoredBrokerOrder | None:
    """The locally stored broker snapshot for one `client_order_id`, if any.

    A stored snapshot proves the order was *accepted*, never that it filled -
    the same warning the storage layer carries. The inspector reads the broker
    itself for the current answer and uses this only to show what local state
    believes, so a divergence between the two is visible rather than hidden.
    """
    return state.get_broker_order_by_client_id(connection, client_order_id.strip())


def latest_reconciliation(connection: sqlite3.Connection) -> state.ReconciliationRun | None:
    """The most recently completed reconciliation pass, or None.

    **Read only.** The harness never triggers a pass of its own: reconciliation
    may rewrite local state from broker truth, and an operator running what
    they believe is an inspection should not have their database repaired as a
    side effect. When a pass is needed the harness says so and prints the
    command; the operator runs it.
    """
    return state.latest_reconciliation_run(connection)


def local_positions(connection: sqlite3.Connection) -> dict[str, state.Position]:
    """The local position snapshot table, keyed by symbol.

    Reported for comparison only. It is *not* authoritative - nothing keeps it
    in step with the broker between reconciliation passes - and every quantity
    a plan is built from comes from the broker instead.
    """
    return {
        normalize_smoke_symbol(position.symbol): position
        for position in state.list_positions(connection)
    }


__all__ = [
    "UNSETTLED_INTENT_STATUSES",
    "find_broker_snapshot",
    "find_intent",
    "intents_by_side",
    "intents_for_symbol",
    "is_terminal_broker_status",
    "latest_reconciliation",
    "local_positions",
    "open_intents",
    "unknown_intents",
]
