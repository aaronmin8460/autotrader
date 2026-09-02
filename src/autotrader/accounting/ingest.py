"""The accounting synchronizer: broker executions in, ledger rows out.

**Where this sits.** Beside the trading path, never inside it.

    confirmed executions -> synchronizer -> accounting store -> dashboard

and never

    execution engine -> wait for accounting -> broker

If this module is broken, stopped, or has never run, the trading runtimes are
unaffected: they do not import it, do not call it, and do not read anything it
writes. Trading continues with no accounting, which is the correct failure
direction - an accounting outage must not become a trading outage.

**Reads the broker, writes only the ledger.** The only database this module
opens for writing is the accounting store. The equity runtime's operational
store is opened read-only, through a `mode=ro` URI and a single SELECT, for
one purpose: to establish which orders that runtime actually placed. No
connection helper from the trading lineage is used, so nothing here can
migrate a store out from under a running service.

**No broker vocabulary.** The concrete client lives behind two structural
protocols carrying only the reads this module needs. That keeps the accounting
package importable - and testable - without a broker SDK, and it means nothing
typed here can be asked to submit an order, because the names of those methods
do not appear.

**Asset class is looked up, never inferred.** An execution activity does not
say which book it belongs to, and guessing from the ticker's punctuation is how
a crypto fill ends up in an equity ledger. The order behind the execution does
say, authoritatively, so every execution is joined to its order and any
execution whose order cannot be found is **skipped and reported**, never
assumed to be equity.

**Overlap, not a high-water mark alone.** Each run re-reads a bounded window
that extends back *before* the last thing it stored, and relies on the store's
UNIQUE constraint to discard what it has already seen. A cursor that advanced
to the newest row and asked only for strictly-newer ones would lose any
execution that arrived late or out of order - and there is no way to notice
afterwards. Re-reading is cheap; a hole in a cost basis is not.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from autotrader.accounting import store
from autotrader.accounting.models import (
    GRANULARITY_EXECUTION,
    PROVENANCE_EQUITY_RUNTIME,
    PROVENANCE_MANUAL_OPERATOR,
    PROVENANCE_UNKNOWN_EXTERNAL,
    ExecutionFill,
)

#: The asset class this ledger accounts for. Everything else is skipped with a
#: reason - see `docs/` and the Phase 0 audit for why crypto is not merely
#: "not yet done" but *measurably wrong* under these semantics: that broker
#: charges crypto pair fees in the coin, as records that reduce inventory
#: without ever appearing in the execution feed.
EQUITY_ASSET_CLASS = "us_equity"

#: How far back before the ledger's newest row each incremental run re-reads.
#: Two days spans a weekend, so a Monday run still re-reads Friday.
DEFAULT_OVERLAP = timedelta(days=2)

#: The client-order-id prefix this system mints. An order carrying it that is
#: in no runtime store was placed by this system's tooling, by hand.
SYSTEM_ORDER_PREFIX = "autotrader-"

READ_TIMEOUT_SECONDS = 5.0

SYNC_OK = "OK"
SYNC_PARTIAL = "PARTIAL"
SYNC_FAILED = "FAILED"


class ConfirmedExecution(Protocol):
    """One execution, as the broker reports it. Read-only, exact decimals."""

    activity_id: str
    broker_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    transaction_time: datetime


class OrderRecord(Protocol):
    """The order behind an execution: asset class, and who minted the key."""

    broker_order_id: str
    client_order_id: str
    asset_class: str


#: Injected so tests never need a network and the sync job never needs to know
#: how paging works.
ExecutionReader = Callable[[datetime | None], tuple[list[ConfirmedExecution], int]]
OrderReader = Callable[[datetime | None], tuple[list[OrderRecord], int]]


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


def runtime_order_ids(path: str | Path) -> frozenset[str]:
    """The broker order ids the equity runtime durably recorded placing.

    One read-only connection, one SELECT, no schema initialization. The store
    belongs to another service at a schema this process may not share, and the
    only safe way to read it is the way that cannot write to it.
    """
    resolved = Path(path)
    if not resolved.exists():
        return frozenset()
    uri = f"file:{quote(str(resolved.resolve()))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=READ_TIMEOUT_SECONDS, isolation_level=None)
    try:
        connection.execute("PRAGMA query_only = 1")
        rows = connection.execute(
            "SELECT broker_order_id FROM broker_orders WHERE broker_order_id IS NOT NULL"
        ).fetchall()
    finally:
        connection.close()
    return frozenset(str(row[0]) for row in rows if row[0])


def classify_provenance(
    broker_order_id: str,
    *,
    runtime_ids: frozenset[str],
    client_order_id: str | None,
) -> str:
    """Say where an order came from, on evidence, or admit it is unknown.

    Three answers, each earned:

    `EQUITY_RUNTIME` - the runtime's own store holds this broker order id.
    That is the runtime's durable record of having placed it, written before
    submission, so it is a fact rather than a resemblance.

    `MANUAL_OPERATOR` - no runtime store holds it, but the client order id
    carries this system's own prefix. Something in this repository minted the
    key and a person ran it.

    `UNKNOWN_EXTERNAL` - neither. Reported as unknown and never attributed to a
    strategy, because a symbol in the traded universe is not evidence of
    anything: the account is one account and anyone with the keys can trade it.
    """
    if broker_order_id in runtime_ids:
        return PROVENANCE_EQUITY_RUNTIME
    if client_order_id and client_order_id.startswith(SYSTEM_ORDER_PREFIX):
        return PROVENANCE_MANUAL_OPERATOR
    return PROVENANCE_UNKNOWN_EXTERNAL


# --------------------------------------------------------------------------
# One synchronization pass
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncResult:
    """What one pass did. Every count is measured, none inferred."""

    status: str
    executions_seen: int
    executions_imported: int
    realized_events: int
    duplicates_skipped: int
    out_of_scope_skipped: int
    unresolved_orders: int
    broker_requests: int
    refusals: tuple[str, ...] = ()
    high_water_mark: str | None = None
    message: str | None = None
    symbols_touched: tuple[str, ...] = field(default=())


def _window_start(connection: sqlite3.Connection, overlap: timedelta) -> datetime | None:
    """Where this run starts reading: before the newest row, or the beginning."""
    mark = store.high_water_mark(connection)
    if mark is None:
        return None
    return datetime.fromisoformat(mark).astimezone(UTC) - overlap


def synchronize(
    connection: sqlite3.Connection,
    *,
    read_executions: ExecutionReader,
    read_orders: OrderReader,
    runtime_store_path: str | Path | None,
    now: datetime,
    overlap: timedelta = DEFAULT_OVERLAP,
    asset_class: str = EQUITY_ASSET_CLASS,
) -> SyncResult:
    """Read confirmed executions since the overlap window and fold them in.

    Executions are applied in `(transaction_time, activity_id)` order. The
    second key matters: several executions of one order can share a
    transaction time to the microsecond, and an unstable sort would make the
    ledger's row order depend on which page they arrived on. Under
    weighted-average cost the *totals* are order-independent, but the
    per-event `average_cost_before` is not, and a replay that produced
    different audit rows each time would not be a replay.
    """
    started = now
    after = _window_start(connection, overlap)

    orders, order_requests = read_orders(after)
    executions, execution_requests = read_executions(after)
    requests = order_requests + execution_requests

    index: Mapping[str, OrderRecord] = {record.broker_order_id: record for record in orders}
    runtime_ids = runtime_order_ids(runtime_store_path) if runtime_store_path else frozenset()

    in_scope: list[tuple[ConfirmedExecution, OrderRecord]] = []
    out_of_scope = 0
    unresolved = 0
    for execution in executions:
        record = index.get(execution.broker_order_id)
        if record is None:
            unresolved += 1
            continue
        if record.asset_class != asset_class:
            out_of_scope += 1
            continue
        in_scope.append((execution, record))

    in_scope.sort(key=lambda pair: (pair[0].transaction_time, pair[0].activity_id))

    imported = 0
    realized = 0
    duplicates = 0
    refusals: list[str] = []
    touched: list[str] = []
    for execution, record in in_scope:
        fill = ExecutionFill(
            execution_id=execution.activity_id,
            order_id=execution.broker_order_id,
            symbol=execution.symbol,
            asset_class=record.asset_class,
            side=execution.side,
            quantity=execution.quantity,
            price=execution.price,
            executed_at=execution.transaction_time,
            granularity=GRANULARITY_EXECUTION,
            provenance=classify_provenance(
                execution.broker_order_id,
                runtime_ids=runtime_ids,
                client_order_id=getattr(record, "client_order_id", None),
            ),
            fees=Decimal(0),
        )
        recorded = store.record_fill(connection, fill, source=store.SOURCE_BROKER_ACTIVITY, now=now)
        if recorded.duplicate:
            duplicates += 1
            continue
        if recorded.refused is not None:
            refusals.append(recorded.refused)
            continue
        imported += 1
        if fill.symbol not in touched:
            touched.append(fill.symbol)
        if recorded.applied is not None and recorded.applied.realized is not None:
            realized += 1

    status = SYNC_OK
    notes: list[str] = []
    if refusals:
        status = SYNC_PARTIAL
        notes.append(f"{len(refusals)} execution(s) refused: inventory would go negative")
    if unresolved:
        status = SYNC_PARTIAL
        notes.append(f"{unresolved} execution(s) had no readable order and were not accounted")

    result = SyncResult(
        status=status,
        executions_seen=len(executions),
        executions_imported=imported,
        realized_events=realized,
        duplicates_skipped=duplicates,
        out_of_scope_skipped=out_of_scope,
        unresolved_orders=unresolved,
        broker_requests=requests,
        refusals=tuple(refusals),
        high_water_mark=store.high_water_mark(connection),
        message="; ".join(notes) or None,
        symbols_touched=tuple(touched),
    )
    store.record_sync_run(
        connection,
        started_at=started,
        completed_at=now,
        status=result.status,
        executions_seen=result.executions_seen,
        executions_imported=result.executions_imported,
        realized_events=result.realized_events,
        duplicates_skipped=result.duplicates_skipped,
        high_water_mark=result.high_water_mark,
        broker_requests=result.broker_requests,
        message=result.message,
    )
    return result


__all__ = [
    "DEFAULT_OVERLAP",
    "EQUITY_ASSET_CLASS",
    "SYNC_FAILED",
    "SYNC_OK",
    "SYNC_PARTIAL",
    "SYSTEM_ORDER_PREFIX",
    "ConfirmedExecution",
    "ExecutionReader",
    "OrderReader",
    "OrderRecord",
    "SyncResult",
    "classify_provenance",
    "runtime_order_ids",
    "synchronize",
]
