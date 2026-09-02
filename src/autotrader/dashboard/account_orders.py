"""Account-wide recent orders: the two paper order stores, merged and labelled.

One brokerage account is traded by two processes that keep two records. The
crypto runtime writes its intents and broker snapshots into the crypto
operational store; the equity paper runtime writes its own into the equity
paper store. The operational API on the first store therefore shows crypto
orders and nothing else, and an operator reading its "recent orders" panel as
the account's would conclude the equity book had not traded.

This module reads **both** stores - each through a `mode=ro` URI and a single
short read - and merges them into one account-wide list, newest first. Every
row carries the store it came from as its `source`, so the label is a fact
about provenance rather than an inference from the symbol.

**What is deliberately not here.** No shadow record is an input. The two
observers keep hypothetical target weights, not orders; nothing they write has
a broker order id, and this module opens neither of their databases. A row on
this panel is a durable intent this system decided to place, joined to what the
broker said about it, and `simulated` is `False` on every one of them - stated
on the wire so a renderer can assert it rather than assume it.

**Deduplication is by broker identity.** The two stores are disjoint by
construction (each runtime trades its own universe), but the merge does not
rely on that: a broker order id seen twice keeps its first row, and a client
order id seen twice does the same. The count of rows dropped that way is
reported, so "zero" is a measured value on the payload rather than an
assumption.

**Reads only.** No `state.connect`, no `initialize_database`, no migration:
the crypto store is a schema version behind this package and a viewer that
opened it the normal way would migrate it out from under the crypto runtime.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from autotrader.dashboard.models import (
    SOURCE_BROKER,
    SOURCE_LOCAL,
    TONE_ATTENTION,
    TONE_MUTED,
    TONE_NEGATIVE,
    TONE_NEUTRAL,
    TONE_POSITIVE,
)
from autotrader.dashboard.service import asset_class_for

#: Where each row came from. Machine strings; labels may be reworded, these
#: may not.
SOURCE_CRYPTO_PAPER = "CRYPTO PAPER"
SOURCE_EQUITY_PAPER = "EQUITY PAPER"
ORDER_SOURCES: tuple[str, ...] = (SOURCE_CRYPTO_PAPER, SOURCE_EQUITY_PAPER)

#: Bounds on the merged list. There is no way to ask for the whole history.
DEFAULT_LIMIT = 20
MAX_LIMIT = 200

#: How many rows are read from each store before the merge. Wider than the
#: page so a busy store cannot crowd a quiet one out of the newest-first cut.
STORE_READ_LIMIT = 400

READ_TIMEOUT_SECONDS = 5.0

UNAVAILABLE_DATABASE_UNREADABLE = "DATABASE_UNREADABLE"

#: Intent statuses copied from the state layer's vocabulary, so this module
#: does not import the persistence package for three strings.
INTENT_STATUS_UNKNOWN = "UNKNOWN"
INTENT_STATUS_REJECTED = "REJECTED"
INTENT_STATUS_CONFIRMED_NOT_SUBMITTED = "CONFIRMED_NOT_SUBMITTED"

_TERMINAL_MUTED = frozenset({"CANCELED", "CANCELLED", "EXPIRED", "DONE_FOR_DAY", "STOPPED"})


@contextmanager
def read_only_connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open `path` read-only and close it on exit. No journal-mode pragma."""
    uri = f"file:{quote(str(Path(path).resolve()))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=READ_TIMEOUT_SECONDS, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = 1")
        yield connection
    finally:
        connection.close()


@dataclass(frozen=True)
class AccountOrderRow:
    """One order this account's runtimes decided to place, and what became of it.

    `authoritative_at` is the sort key: the broker's own submission time when
    the broker answered, else the moment the intent was durably recorded. It
    is carried as a field so the ordering on screen is the ordering on the
    wire, and so a row the broker never answered for still has a time.
    """

    client_order_id: str
    broker_order_id: str | None
    source: str
    simulated: bool
    symbol: str
    asset_class: str
    side: str
    quantity: str
    filled_quantity: str | None
    average_fill_price: float | None
    status: str
    status_tone: str
    status_source: str
    needs_attention: bool
    risk_reason_code: str
    created_at: str
    submitted_at: str | None
    filled_at: str | None
    authoritative_at: str


@dataclass(frozen=True)
class StoreSummary:
    """Whether one store answered, and how much of it the merge saw."""

    source: str
    available: bool
    rows_read: int
    total: int
    attention_count: int
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class AccountOrdersPanel:
    """The merged list, newest first, plus the provenance of every input."""

    generated_at: str
    rows: tuple[AccountOrderRow, ...]
    total: int
    attention_count: int
    stores: tuple[StoreSummary, ...]
    duplicates_dropped: int
    includes_simulated: bool
    note: str
    limit: int


def _status(intent_status: str, broker_status: str | None) -> tuple[str, str, str, bool]:
    """One order's display status, tone, source, and whether it needs a human.

    The same rule the operational read model applies, restated here so the
    merged panel cannot disagree with the per-store one about the same row.
    """
    if intent_status == INTENT_STATUS_UNKNOWN:
        return INTENT_STATUS_UNKNOWN, TONE_ATTENTION, SOURCE_LOCAL, True
    if broker_status is not None:
        status = broker_status.upper()
        if "REJECT" in status:
            return status, TONE_NEGATIVE, SOURCE_BROKER, False
        if status == "FILLED":
            return status, TONE_POSITIVE, SOURCE_BROKER, False
        if status in _TERMINAL_MUTED:
            return status, TONE_MUTED, SOURCE_BROKER, False
        return status, TONE_NEUTRAL, SOURCE_BROKER, False
    if intent_status == INTENT_STATUS_REJECTED:
        return INTENT_STATUS_REJECTED, TONE_NEGATIVE, SOURCE_LOCAL, False
    if intent_status == INTENT_STATUS_CONFIRMED_NOT_SUBMITTED:
        return intent_status, TONE_MUTED, SOURCE_LOCAL, False
    return intent_status, TONE_NEUTRAL, SOURCE_LOCAL, False


_STORE_QUERY = (
    "SELECT i.client_order_id, i.created_at, i.symbol, i.side, i.approved_quantity,"
    " i.risk_reason_code, i.status AS intent_status,"
    " b.broker_order_id, b.filled_quantity, b.filled_average_price,"
    " b.status AS broker_status, b.submitted_at, b.filled_at"
    " FROM order_intents AS i"
    " LEFT JOIN broker_orders AS b ON b.order_intent_id = i.id"
    " ORDER BY i.created_at DESC, i.id DESC LIMIT ?"
)


def read_store_orders(
    path: str | Path, *, source: str
) -> tuple[StoreSummary, list[AccountOrderRow]]:
    """One store's recent orders, labelled with `source`, or why there are none.

    Never raises. A missing file, a locked store, or a schema without the two
    tables all come back as an unavailable summary and an empty list; the
    merge then says so per store rather than rendering a half-account as the
    whole one.
    """
    if source not in ORDER_SOURCES:
        raise ValueError(f"Unknown order source {source!r}; expected one of {ORDER_SOURCES}.")
    try:
        with read_only_connection(path) as connection:
            connection.execute("BEGIN DEFERRED")
            try:
                total = int(connection.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0])
                unknown = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM order_intents WHERE status = ?",
                        (INTENT_STATUS_UNKNOWN,),
                    ).fetchone()[0]
                )
                records = connection.execute(_STORE_QUERY, (STORE_READ_LIMIT,)).fetchall()
            finally:
                connection.execute("COMMIT")
    except (sqlite3.Error, OSError, ValueError):
        return (
            StoreSummary(
                source=source,
                available=False,
                rows_read=0,
                total=0,
                attention_count=0,
                unavailable_reason=UNAVAILABLE_DATABASE_UNREADABLE,
            ),
            [],
        )

    rows: list[AccountOrderRow] = []
    for record in records:
        broker_status = record["broker_status"]
        status, tone, status_source, attention = _status(
            str(record["intent_status"]), None if broker_status is None else str(broker_status)
        )
        created_at = str(record["created_at"])
        submitted_at = record["submitted_at"]
        rows.append(
            AccountOrderRow(
                client_order_id=str(record["client_order_id"]),
                broker_order_id=(
                    None if record["broker_order_id"] is None else str(record["broker_order_id"])
                ),
                source=source,
                simulated=False,
                symbol=str(record["symbol"]),
                asset_class=asset_class_for(str(record["symbol"])),
                side=str(record["side"]),
                quantity=str(record["approved_quantity"]),
                filled_quantity=(
                    None if record["filled_quantity"] is None else str(record["filled_quantity"])
                ),
                average_fill_price=record["filled_average_price"],
                status=status,
                status_tone=tone,
                status_source=status_source,
                needs_attention=attention,
                risk_reason_code=str(record["risk_reason_code"]),
                created_at=created_at,
                submitted_at=None if submitted_at is None else str(submitted_at),
                filled_at=None if record["filled_at"] is None else str(record["filled_at"]),
                authoritative_at=created_at if submitted_at is None else str(submitted_at),
            )
        )
    return (
        StoreSummary(
            source=source,
            available=True,
            rows_read=len(rows),
            total=total,
            attention_count=unknown,
        ),
        rows,
    )


def _sort_key(row: AccountOrderRow) -> tuple[str, str, str]:
    return (row.authoritative_at, row.created_at, row.client_order_id)


def merge_orders(
    reads: list[tuple[StoreSummary, list[AccountOrderRow]]],
    *,
    now: datetime,
    limit: int = DEFAULT_LIMIT,
) -> AccountOrdersPanel:
    """Merge per-store reads into one newest-first list, deduplicated.

    Deterministic: the same inputs produce the same output regardless of the
    order the stores were read in, because the sort key is the row's own
    authoritative time (with the client order id as the final tie-break) and
    the dedup keeps the first row *after* sorting.
    """
    bounded = max(1, min(int(limit), MAX_LIMIT))
    everything = [row for _, rows in reads for row in rows]
    everything.sort(key=_sort_key, reverse=True)

    seen_broker: set[str] = set()
    seen_client: set[str] = set()
    kept: list[AccountOrderRow] = []
    dropped = 0
    for row in everything:
        if row.broker_order_id is not None and row.broker_order_id in seen_broker:
            dropped += 1
            continue
        if row.client_order_id in seen_client:
            dropped += 1
            continue
        if row.broker_order_id is not None:
            seen_broker.add(row.broker_order_id)
        seen_client.add(row.client_order_id)
        kept.append(row)

    stores = tuple(summary for summary, _ in reads)
    return AccountOrdersPanel(
        generated_at=now.astimezone(UTC).isoformat(),
        rows=tuple(kept[:bounded]),
        total=sum(summary.total for summary in stores),
        attention_count=sum(summary.attention_count for summary in stores),
        stores=stores,
        duplicates_dropped=dropped,
        includes_simulated=False,
        note=(
            "Real paper-broker orders from the crypto and equity paper stores, merged "
            "and sorted by the broker's submission time (or the intent's creation time "
            "when the broker never answered). No shadow or simulated action is an "
            "input to this list."
        ),
        limit=bounded,
    )


def build_account_orders(
    *,
    crypto_path: str | Path,
    paper_path: str | Path,
    now: datetime,
    limit: int = DEFAULT_LIMIT,
) -> AccountOrdersPanel:
    """Read both stores and merge them. The only entry point the API needs."""
    reads = [
        read_store_orders(crypto_path, source=SOURCE_CRYPTO_PAPER),
        read_store_orders(paper_path, source=SOURCE_EQUITY_PAPER),
    ]
    return merge_orders(reads, now=now, limit=limit)


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "ORDER_SOURCES",
    "SOURCE_CRYPTO_PAPER",
    "SOURCE_EQUITY_PAPER",
    "STORE_READ_LIMIT",
    "UNAVAILABLE_DATABASE_UNREADABLE",
    "AccountOrderRow",
    "AccountOrdersPanel",
    "StoreSummary",
    "build_account_orders",
    "merge_orders",
    "read_only_connection",
    "read_store_orders",
]
