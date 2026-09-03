"""Wiring: paths, the broker adapter, one synchronization pass, one bootstrap.

The only module in the accounting package that knows a broker SDK exists, and
it reaches it the way the rest of this repository does - by importing named
**read** helpers out of the execution boundary and nothing else. The three
names it imports can read an account's positions, its orders and its
executions. None of them can place, cancel or modify anything, and the
submission entry points in that module are never imported here.

Everything below `synchronize_once` is composition. The decisions live in
`engine`, `store`, `ingest` and `reconcile`, all of which are reachable
without credentials, without a network and without this module.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from autotrader.accounting import ingest, reconcile, store
from autotrader.accounting.models import (
    BASIS_WEIGHTED_AVERAGE,
    COMPLETENESS_EXACT_REPLAY,
)

#: Where the ledger lives. Its own file, deliberately not inside any trading
#: runtime's data directory - see `store` for why it is not even in the same
#: schema.
ACCOUNTING_DATABASE_PATH_ENV = "AUTOTRADER_EQUITY_ACCOUNTING_DB"
DEFAULT_ACCOUNTING_DATABASE_PATH = Path("/var/lib/autotrader-accounting/equity-accounting.db")

#: The equity runtime's operational store. Read-only, one SELECT, for
#: provenance only.
PAPER_DATABASE_PATH_ENV = "AUTOTRADER_EQUITY_PAPER_DB"
DEFAULT_PAPER_DATABASE_PATH = Path("/var/lib/autotrader-equity-paper/equity-paper.db")

#: `historical_completeness` when the ledger was replayed from the first
#: execution the account ever had, rather than from a declared cutover.
COMPLETENESS_FROM_ACCOUNT_OPEN = "EXACT_REPLAY_FROM_ACCOUNT_OPEN"

#: The asset class scope stamped into the ledger's metadata.
ASSET_CLASS_SCOPE = "US_EQUITY"


def accounting_database_path() -> Path:
    configured = os.environ.get(ACCOUNTING_DATABASE_PATH_ENV)
    return Path(configured) if configured else DEFAULT_ACCOUNTING_DATABASE_PATH


def paper_database_path() -> Path:
    configured = os.environ.get(PAPER_DATABASE_PATH_ENV)
    return Path(configured) if configured else DEFAULT_PAPER_DATABASE_PATH


def account_fingerprint(account_id: object) -> str:
    """A stable, non-secret identity for the account the ledger describes.

    A hash prefix, never the identifier itself. The ledger has to be able to
    say "these numbers are about *that* account" - so that restoring a backup
    against a different one is detectable - without writing an account number
    into a file that ends up in a report.
    """
    return hashlib.sha256(str(account_id).encode("utf-8")).hexdigest()[:16]


class UnsupportedBrokerPositionError(Exception):
    """The broker holds something this ledger has no way to account for."""


@dataclass(frozen=True)
class BrokerPositionView:
    """A broker position, reduced to what reconciliation compares."""

    symbol: str
    quantity: Decimal
    average_entry_price: Decimal


# --------------------------------------------------------------------------
# The broker adapter
# --------------------------------------------------------------------------


def _read_helpers() -> tuple[object, object, object, object]:
    """Import the four read helpers, and only those four.

    Imported inside a function so that importing this module - which the CLI
    and the tests do - does not require a broker SDK to be installed at all.
    """
    from autotrader.execution.paper import (  # noqa: PLC0415 - deliberate late import
        create_paper_trading_client,
        fetch_execution_activities,
        fetch_order_records,
        fetch_position_records,
    )

    return (
        create_paper_trading_client,
        fetch_execution_activities,
        fetch_order_records,
        fetch_position_records,
    )


def build_readers(
    client: object | None = None,
) -> tuple[object, ingest.ExecutionReader, ingest.OrderReader]:
    """A client and the two bounded readers the synchronizer takes."""
    create_client, read_activities, read_orders, _ = _read_helpers()
    broker = client if client is not None else create_client()  # type: ignore[operator]

    def executions(after: datetime | None) -> tuple[list[ingest.ConfirmedExecution], int]:
        return read_activities(broker, after=after)  # type: ignore[operator,no-any-return]

    def orders(after: datetime | None) -> tuple[list[ingest.OrderRecord], int]:
        return read_orders(broker, after=after)  # type: ignore[operator,no-any-return]

    return broker, executions, orders


def read_broker_positions(client: object, *, asset_class: str = ingest.EQUITY_ASSET_CLASS) -> dict:
    """The broker's current equity positions, keyed by symbol.

    Crypto rows are dropped here rather than in reconciliation, so a crypto
    position can never register as an equity ledger mismatch.
    """
    _, _, _, fetch_positions = _read_helpers()
    view: dict[str, BrokerPositionView] = {}
    for position in fetch_positions(client):  # type: ignore[operator]
        if position.asset_class != asset_class:
            continue
        if position.side and position.side != "LONG":
            raise UnsupportedBrokerPositionError(
                f"The broker reports a {position.side} position in {position.symbol}. "
                "This ledger accounts for long inventory only and will not guess at "
                "the cost basis of a short."
            )
        view[position.symbol] = BrokerPositionView(
            symbol=position.symbol,
            quantity=position.quantity,
            average_entry_price=position.average_entry_price,
        )
    return view


# --------------------------------------------------------------------------
# One pass
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PassResult:
    sync: ingest.SyncResult
    reconciliation: reconcile.ReconciliationResult


def synchronize_once(
    *,
    database: Path | None = None,
    paper_database: Path | None = None,
    client: object | None = None,
    now: datetime | None = None,
    overlap: timedelta = ingest.DEFAULT_OVERLAP,
) -> PassResult:
    """Ingest new executions, then reconcile against the broker. Read-only outward.

    The order matters: reconciling *after* ingestion means the verdict
    describes the ledger as it now stands, so a run that imported the sale that
    explains a discrepancy reports `CLEAN` rather than reporting the
    discrepancy it just fixed.
    """
    moment = now or datetime.now(UTC)
    path = database or accounting_database_path()
    runtime_store = paper_database or paper_database_path()

    broker, executions, orders = build_readers(client)

    with store.connect(path) as connection:
        store.initialize(connection)
        sync = ingest.synchronize(
            connection,
            read_executions=executions,
            read_orders=orders,
            runtime_store_path=runtime_store,
            now=moment,
            overlap=overlap,
        )
        try:
            positions = read_broker_positions(broker)
        except Exception:  # noqa: BLE001 - an unread broker is UNKNOWN, not an outage
            positions = None
        verdict = reconcile.reconcile(connection, positions, now=moment)

    return PassResult(sync=sync, reconciliation=verdict)


# --------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------


def bootstrap_exact_replay(
    *,
    database: Path | None = None,
    paper_database: Path | None = None,
    client: object | None = None,
    now: datetime | None = None,
    source_sha: str | None = None,
    notes: str | None = None,
) -> PassResult:
    """Build the ledger from the account's whole confirmed execution record.

    `overlap` is irrelevant here because the ledger is empty and the
    synchronizer therefore reads from the beginning. The same code path runs
    afterwards for every incremental pass - there is no separate "historical"
    engine whose behaviour could drift from the live one, which is the point.

    **The race is closed by overlap, not by a fence.** An execution that lands
    while this is running is either already in the window this pass reads, or
    it is inside the window the *next* pass re-reads. There is no interval in
    which an execution is too late for one and too early for the other.
    """
    moment = now or datetime.now(UTC)
    path = database or accounting_database_path()

    broker, _, _ = build_readers(client)
    account = broker.get_account()  # type: ignore[attr-defined]
    fingerprint = account_fingerprint(getattr(account, "id", ""))

    result = synchronize_once(
        database=path,
        paper_database=paper_database,
        client=broker,
        now=moment,
    )

    with store.connect(path) as connection:
        store.initialize(connection)
        first = connection.execute("SELECT MIN(executed_at) AS m FROM accounting_fills").fetchone()
        tracking_started = (
            datetime.fromisoformat(str(first["m"])).astimezone(UTC)
            if first is not None and first["m"] is not None
            else moment
        )
        with store.transaction(connection):
            store.write_metadata(
                connection,
                tracking_started_at=tracking_started,
                bootstrap_method=COMPLETENESS_EXACT_REPLAY,
                historical_completeness=COMPLETENESS_FROM_ACCOUNT_OPEN,
                broker_account_fingerprint=fingerprint,
                asset_class_scope=ASSET_CLASS_SCOPE,
                source_sha=source_sha,
                notes=notes
                or (
                    f"{BASIS_WEIGHTED_AVERAGE}; execution-level fills; "
                    "crypto excluded (in-kind pair fees reduce inventory outside the "
                    "execution feed)"
                ),
                now=moment,
            )

    return result


__all__ = [
    "ACCOUNTING_DATABASE_PATH_ENV",
    "ASSET_CLASS_SCOPE",
    "COMPLETENESS_FROM_ACCOUNT_OPEN",
    "DEFAULT_ACCOUNTING_DATABASE_PATH",
    "PAPER_DATABASE_PATH_ENV",
    "BrokerPositionView",
    "UnsupportedBrokerPositionError",
    "PassResult",
    "account_fingerprint",
    "accounting_database_path",
    "bootstrap_exact_replay",
    "build_readers",
    "paper_database_path",
    "read_broker_positions",
    "synchronize_once",
]
