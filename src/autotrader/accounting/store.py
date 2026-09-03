"""Persistence for the accounting ledger. Its own database, on purpose.

**Why this is not a set of tables in the operational store.** Three stores
describe this one account and they sit at two different schema versions,
because every command in the trading lineage runs `initialize_database()`
first and that migrates upward automatically. Adding accounting tables to the
shared schema would mean bumping its version, and the next time anything from
this lineage opened the equity paper store it would migrate it out from under
a *running trader* that only understands the version it was installed at. The
trader would then refuse to open its own database.

So the ledger gets its own file, its own version counter, and its own
migration path, and touches nothing the trading runtimes read or write. The
operational store is consulted exactly once - read-only, one SELECT, no
connection helper from the trading lineage - to establish provenance. That is
the entire coupling.

**Exact decimals on the wire and on disk.** Quantities, prices and money are
stored as TEXT holding the plain (never exponent-notated) decimal string, and
read back with `Decimal(text)`, which round-trips exactly. The CHECK
constraints CAST to REAL, which is a coarse guard against a writer that
bypassed this module storing something absurd - the cast is not the value.

**A rollback journal, not WAL** - the one place this store deliberately
departs from the operational stores' convention. A `mode=ro` connection to a
WAL database has to *create* the `-shm` side file if no writer is currently
holding one, which needs write access to the directory. The dashboard reader
is a least-privilege process under `ProtectSystem=strict` with no write access
to the ledger's directory, so under WAL it could read the ledger only while a
writer happened to be running - which is a few seconds in every five minutes.
It failed closed and reported the ledger unreadable the rest of the time,
which is the correct behaviour for a wrong configuration and not a
configuration to keep.

WAL buys concurrent reads during a write. Here the writer is a oneshot that
runs for about a second every five minutes and the reader polls every thirty,
so that is worth approximately nothing, and it costs the reader the ability to
open the file at all. A rollback journal needs no side file, so the read-only
viewer works whether or not anything is writing.

**Append-only where it matters.** `accounting_fills` and
`realized_pnl_events` have no UPDATE path in this module at all.
`position_cost_basis` is derived current state and is rewritten as the ledger
advances. Historical P&L is never recomputed because a price moved.

**One execution, one transaction.** Recording a fill, writing its realized
event and advancing the cost basis happen inside a single `BEGIN IMMEDIATE`.
A crash between them leaves none of them, so a fill can never exist without
the state transition it caused.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from autotrader.accounting import engine
from autotrader.accounting.models import (
    ACCOUNTING_VERSION,
    BASIS_WEIGHTED_AVERAGE,
    COMPLETENESS_VALUES,
    GRANULARITY_EXECUTION,
    PROVENANCES,
    SIDES,
    STATUS_MISMATCH,
    STATUS_TRACKING,
    STATUSES,
    AccountingError,
    AccountingInputError,
    AppliedFill,
    CostBasisState,
    ExecutionFill,
    NegativeInventoryError,
)

#: The accounting store's own schema version, independent of the operational
#: store's. It starts at 1 and has nothing to do with `state.SCHEMA_VERSION`.
ACCOUNTING_SCHEMA_VERSION = 1

BUSY_TIMEOUT_MS = 5000

#: Where each fill came into the ledger from.
SOURCE_BROKER_ACTIVITY = "BROKER_ACTIVITY"
SOURCE_BOOTSTRAP_REPLAY = "BOOTSTRAP_REPLAY"
FILL_SOURCES: tuple[str, ...] = (SOURCE_BROKER_ACTIVITY, SOURCE_BOOTSTRAP_REPLAY)

#: Reconciliation verdicts, worst-first when several apply.
RECON_CLEAN = "CLEAN"
RECON_DEGRADED = "DEGRADED"
RECON_MISMATCH = "MISMATCH"
RECON_UNKNOWN = "UNKNOWN"
RECON_STATUSES: tuple[str, ...] = (RECON_CLEAN, RECON_DEGRADED, RECON_MISMATCH, RECON_UNKNOWN)


class AccountingStoreError(AccountingError):
    """The ledger's database could not be used as the ledger's database."""


class UnsupportedAccountingSchemaError(AccountingStoreError):
    """The file on disk was written by a newer accounting build."""


# --------------------------------------------------------------------------
# Decimal <-> TEXT
# --------------------------------------------------------------------------


def decimal_text(value: Decimal) -> str:
    """The plain decimal string for `value`: exact, and never `1E-9`.

    `str(Decimal("0E-9"))` is `'0E-9'`, which is exact but reads like a bug in
    a database viewer and sorts like nothing at all. `format(v, 'f')` is the
    same number without the exponent.
    """
    if not isinstance(value, Decimal):
        raise AccountingInputError(f"expected Decimal, got {type(value).__name__}")
    return format(value, "f")


def text_decimal(value: object) -> Decimal:
    if value is None:
        raise AccountingInputError("expected a stored decimal, found NULL")
    return Decimal(str(value))


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else decimal_text(value)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _utc_date(moment: datetime) -> str:
    return moment.astimezone(UTC).date().isoformat()


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

_CREATE_METADATA = """
    CREATE TABLE accounting_metadata (
        id                         INTEGER PRIMARY KEY CHECK (id = 1),
        accounting_schema_version  INTEGER NOT NULL,
        accounting_version         INTEGER NOT NULL,
        basis_method               TEXT    NOT NULL CHECK (basis_method <> ''),
        asset_class_scope          TEXT    NOT NULL CHECK (asset_class_scope <> ''),
        tracking_started_at        TEXT    NOT NULL CHECK (tracking_started_at <> ''),
        bootstrap_method           TEXT    NOT NULL CHECK (
            bootstrap_method IN ('EXACT_REPLAY', 'CUTOVER')
        ),
        historical_completeness    TEXT    NOT NULL CHECK (historical_completeness <> ''),
        broker_account_fingerprint TEXT    NOT NULL CHECK (broker_account_fingerprint <> ''),
        source_sha                 TEXT,
        notes                      TEXT,
        created_at                 TEXT    NOT NULL,
        updated_at                 TEXT    NOT NULL
    )
    """

#: The immutable source events.
#:
#: `broker_execution_id` is UNIQUE and is the idempotency identity when the
#: broker publishes execution-level detail. `idempotency_key` is UNIQUE and
#: NOT NULL always - it is the execution id when there is one, and a
#: documented order-level key when the broker would only report an aggregate.
#: Two columns rather than one so a reader can tell which regime a row is in
#: without parsing the key.
_CREATE_FILLS = """
    CREATE TABLE accounting_fills (
        accounting_event_id   INTEGER PRIMARY KEY,
        idempotency_key       TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
        broker_execution_id   TEXT UNIQUE,
        broker_order_id       TEXT NOT NULL CHECK (broker_order_id <> ''),
        symbol                TEXT NOT NULL CHECK (symbol <> ''),
        asset_class           TEXT NOT NULL CHECK (asset_class <> ''),
        side                  TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
        quantity              TEXT NOT NULL CHECK (
            quantity <> '' AND CAST(quantity AS REAL) > 0
        ),
        execution_price       TEXT NOT NULL CHECK (
            execution_price <> '' AND CAST(execution_price AS REAL) > 0
        ),
        fees                  TEXT NOT NULL CHECK (
            fees <> '' AND CAST(fees AS REAL) >= 0
        ),
        executed_at           TEXT NOT NULL CHECK (executed_at <> ''),
        execution_granularity TEXT NOT NULL CHECK (
            execution_granularity IN ('EXECUTION', 'AGGREGATED_BROKER_FILL')
        ),
        provenance            TEXT NOT NULL CHECK (
            provenance IN ('EQUITY_RUNTIME', 'MANUAL_OPERATOR', 'MIGRATION', 'UNKNOWN_EXTERNAL')
        ),
        source                TEXT NOT NULL CHECK (
            source IN ('BROKER_ACTIVITY', 'BOOTSTRAP_REPLAY')
        ),
        imported_at           TEXT NOT NULL
    )
    """

#: One row per sale. `accounting_event_id` is UNIQUE, so a fill can realize at
#: most once however many times ingestion is retried.
_CREATE_REALIZED = """
    CREATE TABLE realized_pnl_events (
        event_id            INTEGER PRIMARY KEY,
        accounting_event_id INTEGER NOT NULL UNIQUE
                            REFERENCES accounting_fills (accounting_event_id),
        symbol              TEXT NOT NULL CHECK (symbol <> ''),
        quantity            TEXT NOT NULL CHECK (
            quantity <> '' AND CAST(quantity AS REAL) > 0
        ),
        execution_price     TEXT NOT NULL CHECK (execution_price <> ''),
        average_cost_before TEXT NOT NULL CHECK (average_cost_before <> ''),
        released_cost_basis TEXT NOT NULL CHECK (released_cost_basis <> ''),
        gross_proceeds      TEXT NOT NULL CHECK (gross_proceeds <> ''),
        gross_realized_pnl  TEXT NOT NULL CHECK (gross_realized_pnl <> ''),
        fees                TEXT NOT NULL CHECK (fees <> ''),
        net_realized_pnl    TEXT NOT NULL CHECK (net_realized_pnl <> ''),
        quantity_before     TEXT NOT NULL CHECK (quantity_before <> ''),
        quantity_after      TEXT NOT NULL CHECK (
            quantity_after <> '' AND CAST(quantity_after AS REAL) >= 0
        ),
        average_cost_after  TEXT,
        realized_at         TEXT NOT NULL CHECK (realized_at <> ''),
        realized_date_utc   TEXT NOT NULL CHECK (realized_date_utc <> ''),
        provenance          TEXT NOT NULL CHECK (provenance <> ''),
        accounting_version  INTEGER NOT NULL CHECK (accounting_version >= 1),
        created_at          TEXT NOT NULL
    )
    """

#: Derived current state. The one table here that is rewritten.
_CREATE_COST_BASIS = """
    CREATE TABLE position_cost_basis (
        symbol                   TEXT PRIMARY KEY CHECK (symbol <> ''),
        quantity                 TEXT NOT NULL CHECK (
            quantity <> '' AND CAST(quantity AS REAL) >= 0
        ),
        average_cost             TEXT,
        total_cost_basis         TEXT NOT NULL CHECK (
            total_cost_basis <> '' AND CAST(total_cost_basis AS REAL) >= 0
        ),
        last_accounting_event_id INTEGER REFERENCES accounting_fills (accounting_event_id),
        accounting_status        TEXT NOT NULL CHECK (
            accounting_status IN ('TRACKING', 'ACCOUNTING_MISMATCH')
        ),
        updated_at               TEXT NOT NULL
    )
    """

_CREATE_SYNC_RUNS = """
    CREATE TABLE accounting_sync_runs (
        id                    INTEGER PRIMARY KEY,
        started_at            TEXT NOT NULL,
        completed_at          TEXT NOT NULL,
        status                TEXT NOT NULL CHECK (
            status IN ('OK', 'PARTIAL', 'FAILED')
        ),
        executions_seen       INTEGER NOT NULL CHECK (executions_seen >= 0),
        executions_imported   INTEGER NOT NULL CHECK (executions_imported >= 0),
        realized_events       INTEGER NOT NULL CHECK (realized_events >= 0),
        duplicates_skipped    INTEGER NOT NULL CHECK (duplicates_skipped >= 0),
        high_water_mark       TEXT,
        broker_requests       INTEGER NOT NULL CHECK (broker_requests >= 0),
        message               TEXT,
        created_at            TEXT NOT NULL
    )
    """

_CREATE_RECON_RUNS = """
    CREATE TABLE accounting_reconciliation_runs (
        id                  INTEGER PRIMARY KEY,
        run_at              TEXT NOT NULL,
        status              TEXT NOT NULL CHECK (
            status IN ('CLEAN', 'DEGRADED', 'MISMATCH', 'UNKNOWN')
        ),
        symbols_checked     INTEGER NOT NULL CHECK (symbols_checked >= 0),
        quantity_mismatches INTEGER NOT NULL CHECK (quantity_mismatches >= 0),
        cost_deviations     INTEGER NOT NULL CHECK (cost_deviations >= 0),
        message             TEXT,
        created_at          TEXT NOT NULL
    )
    """

_CREATE_RECON_SYMBOLS = """
    CREATE TABLE accounting_reconciliation_symbols (
        id                    INTEGER PRIMARY KEY,
        run_id                INTEGER NOT NULL
                              REFERENCES accounting_reconciliation_runs (id),
        symbol                TEXT NOT NULL CHECK (symbol <> ''),
        local_quantity        TEXT NOT NULL,
        broker_quantity       TEXT NOT NULL,
        quantity_matches      INTEGER NOT NULL CHECK (quantity_matches IN (0, 1)),
        local_average_cost    TEXT,
        broker_average_entry  TEXT,
        average_cost_delta    TEXT,
        status                TEXT NOT NULL CHECK (
            status IN ('CLEAN', 'DEGRADED', 'MISMATCH', 'UNKNOWN')
        ),
        created_at            TEXT NOT NULL,
        UNIQUE (run_id, symbol)
    )
    """

_INDEXES: tuple[str, ...] = (
    "CREATE INDEX idx_accounting_fills_symbol ON accounting_fills (symbol, executed_at)",
    "CREATE INDEX idx_accounting_fills_executed ON accounting_fills (executed_at)",
    "CREATE INDEX idx_accounting_fills_order ON accounting_fills (broker_order_id)",
    "CREATE INDEX idx_realized_symbol ON realized_pnl_events (symbol, realized_at)",
    "CREATE INDEX idx_realized_date ON realized_pnl_events (realized_date_utc)",
    "CREATE INDEX idx_recon_symbols_run ON accounting_reconciliation_symbols (run_id)",
)

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    _CREATE_METADATA,
    _CREATE_FILLS,
    _CREATE_REALIZED,
    _CREATE_COST_BASIS,
    _CREATE_SYNC_RUNS,
    _CREATE_RECON_RUNS,
    _CREATE_RECON_SYMBOLS,
    *_INDEXES,
)

_TABLE_NAMES: tuple[str, ...] = (
    "accounting_metadata",
    "accounting_fills",
    "realized_pnl_events",
    "position_cost_basis",
    "accounting_sync_runs",
    "accounting_reconciliation_runs",
    "accounting_reconciliation_symbols",
)


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------


#: The ledger's journal mode. **Not** WAL - see the module docstring. A
#: read-only connection must be able to open this file when nothing is writing
#: it, and a WAL database cannot be opened read-only without creating a side
#: file the least-privilege reader is not allowed to create.
JOURNAL_MODE = "DELETE"


@contextmanager
def connect(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open the accounting database for writing: foreign keys, journal, timeout.

    Setting `journal_mode` on every connection, rather than once at creation,
    is what converts a ledger that was created under WAL by an older build -
    and would therefore be unreadable by the dashboard - back to a mode the
    reader can open. The pragma checkpoints and removes the side files.
    """
    connection = sqlite3.connect(Path(path), isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA journal_mode = {JOURNAL_MODE}").fetchone()
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        yield connection
    finally:
        connection.close()


@contextmanager
def connect_read_only(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open the accounting database read-only. For the dashboard's readers.

    A viewer opening this file must not be able to create it, migrate it or
    write to it, so it gets a `mode=ro` URI and `query_only`, not the helper
    above with good intentions. That is the reason the ledger does not use
    WAL: this connection has to work when the writer is not running, from a
    process with no write access to the directory the file is in.
    """
    uri = f"file:{quote(str(Path(path).resolve()))}?mode=ro"
    connection = sqlite3.connect(
        uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = 1")
        yield connection
    finally:
        connection.close()


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on any exception. Nesting joins the outer one."""
    if connection.in_transaction:
        yield connection
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


# --------------------------------------------------------------------------
# Initialization
# --------------------------------------------------------------------------


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def initialize(connection: sqlite3.Connection) -> int:
    """Create the accounting schema if it is absent; verify it if it is present.

    Returns the schema version in force. Raises rather than migrating anything
    it does not recognise - there is exactly one version today, and inventing a
    silent downgrade path for a file written by a future build is how a ledger
    loses rows.
    """
    existing = _table_names(connection)
    if "accounting_metadata" not in existing:
        with transaction(connection):
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
        return ACCOUNTING_SCHEMA_VERSION

    row = connection.execute(
        "SELECT accounting_schema_version FROM accounting_metadata WHERE id = 1"
    ).fetchone()
    if row is None:
        # Schema present, never stamped: bootstrap has not run yet.
        return ACCOUNTING_SCHEMA_VERSION
    version = int(row["accounting_schema_version"])
    if version > ACCOUNTING_SCHEMA_VERSION:
        raise UnsupportedAccountingSchemaError(
            f"The accounting store is at schema {version}; this build understands "
            f"{ACCOUNTING_SCHEMA_VERSION}. Refusing to open it."
        )
    if version < ACCOUNTING_SCHEMA_VERSION:  # pragma: no cover - no older version exists
        raise UnsupportedAccountingSchemaError(
            f"The accounting store is at schema {version} and no migration to "
            f"{ACCOUNTING_SCHEMA_VERSION} is defined."
        )
    missing = set(_TABLE_NAMES) - existing
    if missing:
        raise UnsupportedAccountingSchemaError(
            f"The accounting store is missing tables: {sorted(missing)}."
        )
    return version


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountingMetadata:
    accounting_schema_version: int
    accounting_version: int
    basis_method: str
    asset_class_scope: str
    tracking_started_at: str
    bootstrap_method: str
    historical_completeness: str
    broker_account_fingerprint: str
    source_sha: str | None
    notes: str | None
    created_at: str
    updated_at: str


def write_metadata(
    connection: sqlite3.Connection,
    *,
    tracking_started_at: datetime,
    bootstrap_method: str,
    historical_completeness: str,
    broker_account_fingerprint: str,
    asset_class_scope: str,
    source_sha: str | None = None,
    notes: str | None = None,
    now: datetime,
) -> None:
    """Stamp the single metadata row. Written at bootstrap, rarely after."""
    if bootstrap_method not in COMPLETENESS_VALUES:
        raise AccountingInputError(f"bootstrap_method must be one of {COMPLETENESS_VALUES}")
    existing = read_metadata(connection)
    created = existing.created_at if existing else _iso(now)
    connection.execute(
        """
        INSERT INTO accounting_metadata (
            id, accounting_schema_version, accounting_version, basis_method,
            asset_class_scope, tracking_started_at, bootstrap_method,
            historical_completeness, broker_account_fingerprint, source_sha,
            notes, created_at, updated_at
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            accounting_schema_version = excluded.accounting_schema_version,
            accounting_version        = excluded.accounting_version,
            basis_method              = excluded.basis_method,
            asset_class_scope         = excluded.asset_class_scope,
            tracking_started_at       = excluded.tracking_started_at,
            bootstrap_method          = excluded.bootstrap_method,
            historical_completeness   = excluded.historical_completeness,
            broker_account_fingerprint= excluded.broker_account_fingerprint,
            source_sha                = excluded.source_sha,
            notes                     = excluded.notes,
            updated_at                = excluded.updated_at
        """,
        (
            ACCOUNTING_SCHEMA_VERSION,
            ACCOUNTING_VERSION,
            BASIS_WEIGHTED_AVERAGE,
            asset_class_scope,
            _iso(tracking_started_at),
            bootstrap_method,
            historical_completeness,
            broker_account_fingerprint,
            source_sha,
            notes,
            created,
            _iso(now),
        ),
    )


def read_metadata(connection: sqlite3.Connection) -> AccountingMetadata | None:
    row = connection.execute("SELECT * FROM accounting_metadata WHERE id = 1").fetchone()
    if row is None:
        return None
    return AccountingMetadata(
        accounting_schema_version=int(row["accounting_schema_version"]),
        accounting_version=int(row["accounting_version"]),
        basis_method=str(row["basis_method"]),
        asset_class_scope=str(row["asset_class_scope"]),
        tracking_started_at=str(row["tracking_started_at"]),
        bootstrap_method=str(row["bootstrap_method"]),
        historical_completeness=str(row["historical_completeness"]),
        broker_account_fingerprint=str(row["broker_account_fingerprint"]),
        source_sha=row["source_sha"],
        notes=row["notes"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


# --------------------------------------------------------------------------
# Cost basis
# --------------------------------------------------------------------------


def read_cost_basis(connection: sqlite3.Connection, symbol: str) -> CostBasisState:
    row = connection.execute(
        "SELECT * FROM position_cost_basis WHERE symbol = ?", (symbol,)
    ).fetchone()
    if row is None:
        return CostBasisState.flat(symbol)
    return CostBasisState(
        symbol=str(row["symbol"]),
        quantity=text_decimal(row["quantity"]),
        total_cost_basis=text_decimal(row["total_cost_basis"]),
        status=str(row["accounting_status"]),
        last_execution_id=_last_execution_id(connection, row["last_accounting_event_id"]),
    )


def _last_execution_id(connection: sqlite3.Connection, event_id: object) -> str | None:
    if event_id is None:
        return None
    row = connection.execute(
        "SELECT idempotency_key FROM accounting_fills WHERE accounting_event_id = ?",
        (int(event_id),),
    ).fetchone()
    return None if row is None else str(row["idempotency_key"])


def read_all_cost_basis(connection: sqlite3.Connection) -> dict[str, CostBasisState]:
    return {
        str(row["symbol"]): CostBasisState(
            symbol=str(row["symbol"]),
            quantity=text_decimal(row["quantity"]),
            total_cost_basis=text_decimal(row["total_cost_basis"]),
            status=str(row["accounting_status"]),
            last_execution_id=_last_execution_id(connection, row["last_accounting_event_id"]),
        )
        for row in connection.execute("SELECT * FROM position_cost_basis ORDER BY symbol")
    }


def _write_cost_basis(
    connection: sqlite3.Connection,
    state: CostBasisState,
    *,
    last_accounting_event_id: int | None,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO position_cost_basis (
            symbol, quantity, average_cost, total_cost_basis,
            last_accounting_event_id, accounting_status, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (symbol) DO UPDATE SET
            quantity                 = excluded.quantity,
            average_cost             = excluded.average_cost,
            total_cost_basis         = excluded.total_cost_basis,
            last_accounting_event_id = excluded.last_accounting_event_id,
            accounting_status        = excluded.accounting_status,
            updated_at               = excluded.updated_at
        """,
        (
            state.symbol,
            decimal_text(state.quantity),
            _optional_decimal_text(engine.average_cost(state)),
            decimal_text(state.total_cost_basis),
            last_accounting_event_id,
            state.status,
            _iso(now),
        ),
    )


def mark_symbol_mismatch(
    connection: sqlite3.Connection, symbol: str, *, now: datetime
) -> CostBasisState:
    """Stop accounting for a symbol, keeping the numbers that disagreed."""
    current = read_cost_basis(connection, symbol)
    stopped = engine.mark_mismatch(current)
    row = connection.execute(
        "SELECT last_accounting_event_id FROM position_cost_basis WHERE symbol = ?", (symbol,)
    ).fetchone()
    with transaction(connection):
        _write_cost_basis(
            connection,
            stopped,
            last_accounting_event_id=None if row is None else row["last_accounting_event_id"],
            now=now,
        )
    return stopped


# --------------------------------------------------------------------------
# Applying one execution
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedFill:
    """What `record_fill` did: applied, or recognised as already applied."""

    accounting_event_id: int | None
    applied: AppliedFill | None
    duplicate: bool
    refused: str | None = None


def already_recorded(connection: sqlite3.Connection, idempotency_key: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM accounting_fills WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()
    return row is not None


def record_fill(
    connection: sqlite3.Connection,
    fill: ExecutionFill,
    *,
    source: str = SOURCE_BROKER_ACTIVITY,
    now: datetime,
) -> RecordedFill:
    """Record one confirmed execution and everything that follows from it.

    Atomic: the fill row, the realized event and the new cost basis are one
    transaction. A duplicate `idempotency_key` is a no-op that reports itself
    as one - not an error, because retrying an ingestion that already partly
    succeeded is normal and must be boring.

    A sale that would drive inventory negative refuses, marks the symbol
    `ACCOUNTING_MISMATCH` in its own committed transaction, and returns the
    refusal. The fill is deliberately **not** stored: it is not a fact this
    ledger can represent, and storing it would leave a fill with no state
    transition.
    """
    if source not in FILL_SOURCES:
        raise AccountingInputError(f"source must be one of {FILL_SOURCES}")

    if already_recorded(connection, fill.execution_id):
        return RecordedFill(accounting_event_id=None, applied=None, duplicate=True)

    state = read_cost_basis(connection, fill.symbol)
    try:
        applied = engine.apply_fill(state, fill)
    except NegativeInventoryError as error:
        mark_symbol_mismatch(connection, fill.symbol, now=now)
        return RecordedFill(
            accounting_event_id=None, applied=None, duplicate=False, refused=str(error)
        )

    with transaction(connection):
        cursor = connection.execute(
            """
            INSERT INTO accounting_fills (
                idempotency_key, broker_execution_id, broker_order_id, symbol,
                asset_class, side, quantity, execution_price, fees, executed_at,
                execution_granularity, provenance, source, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.execution_id,
                fill.execution_id if fill.granularity == GRANULARITY_EXECUTION else None,
                fill.order_id,
                fill.symbol,
                fill.asset_class,
                fill.side,
                decimal_text(fill.quantity),
                decimal_text(fill.price),
                decimal_text(fill.fees),
                _iso(fill.executed_at),
                fill.granularity,
                fill.provenance,
                source,
                _iso(now),
            ),
        )
        event_id = int(cursor.lastrowid or 0)

        realized = applied.realized
        if realized is not None:
            connection.execute(
                """
                INSERT INTO realized_pnl_events (
                    accounting_event_id, symbol, quantity, execution_price,
                    average_cost_before, released_cost_basis, gross_proceeds,
                    gross_realized_pnl, fees, net_realized_pnl, quantity_before,
                    quantity_after, average_cost_after, realized_at,
                    realized_date_utc, provenance, accounting_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    realized.symbol,
                    decimal_text(realized.quantity),
                    decimal_text(realized.execution_price),
                    decimal_text(realized.average_cost_before),
                    decimal_text(realized.released_cost_basis),
                    decimal_text(realized.gross_proceeds),
                    decimal_text(realized.gross_realized_pnl),
                    decimal_text(realized.fees),
                    decimal_text(realized.net_realized_pnl),
                    decimal_text(realized.quantity_before),
                    decimal_text(realized.quantity_after),
                    _optional_decimal_text(realized.average_cost_after),
                    _iso(realized.realized_at),
                    _utc_date(realized.realized_at),
                    realized.provenance,
                    realized.accounting_version,
                    _iso(now),
                ),
            )

        _write_cost_basis(connection, applied.state, last_accounting_event_id=event_id, now=now)

    return RecordedFill(accounting_event_id=event_id, applied=applied, duplicate=False)


# --------------------------------------------------------------------------
# Run records
# --------------------------------------------------------------------------


def record_sync_run(
    connection: sqlite3.Connection,
    *,
    started_at: datetime,
    completed_at: datetime,
    status: str,
    executions_seen: int,
    executions_imported: int,
    realized_events: int,
    duplicates_skipped: int,
    high_water_mark: str | None,
    broker_requests: int,
    message: str | None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO accounting_sync_runs (
            started_at, completed_at, status, executions_seen, executions_imported,
            realized_events, duplicates_skipped, high_water_mark, broker_requests,
            message, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _iso(started_at),
            _iso(completed_at),
            status,
            executions_seen,
            executions_imported,
            realized_events,
            duplicates_skipped,
            high_water_mark,
            broker_requests,
            message,
            _iso(completed_at),
        ),
    )
    return int(cursor.lastrowid or 0)


def latest_sync_run(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM accounting_sync_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def high_water_mark(connection: sqlite3.Connection) -> str | None:
    """The latest `executed_at` in the ledger, or None when it is empty."""
    row = connection.execute("SELECT MAX(executed_at) AS m FROM accounting_fills").fetchone()
    return None if row is None or row["m"] is None else str(row["m"])


__all__ = [
    "ACCOUNTING_SCHEMA_VERSION",
    "JOURNAL_MODE",
    "FILL_SOURCES",
    "RECON_CLEAN",
    "RECON_DEGRADED",
    "RECON_MISMATCH",
    "RECON_STATUSES",
    "RECON_UNKNOWN",
    "SOURCE_BOOTSTRAP_REPLAY",
    "SOURCE_BROKER_ACTIVITY",
    "STATUSES",
    "STATUS_MISMATCH",
    "STATUS_TRACKING",
    "AccountingMetadata",
    "AccountingStoreError",
    "PROVENANCES",
    "RecordedFill",
    "SIDES",
    "UnsupportedAccountingSchemaError",
    "already_recorded",
    "connect",
    "connect_read_only",
    "decimal_text",
    "high_water_mark",
    "initialize",
    "latest_sync_run",
    "mark_symbol_mismatch",
    "read_all_cost_basis",
    "read_cost_basis",
    "read_metadata",
    "record_fill",
    "record_sync_run",
    "text_decimal",
    "transaction",
    "write_metadata",
]
