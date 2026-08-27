"""The local SQLite operational-state store (Phase 6, extended by Phase 7).

This module is **persistence infrastructure and nothing else**. It opens a
local SQLite database, creates a small fixed schema, and stores a handful of
durable operational records. It does not decide anything, orchestrate
anything, or talk to anyone: no strategy runs here, no risk rule is evaluated,
no order is *placed*, and no broker is contacted. It imports only the standard
library, needs no credentials, and opens no socket. Phase 7 added two tables to
it; it did not add a broker client to it.

**Why SQLite.** One user, one local process, one file (docs/SPEC.md section 5).
The standard library ships the driver, so the dependency footprint of this
phase is zero - no ORM, no migration framework, and no database service.

**What Phase 7 added, and why only now.** Schema v2 introduces `order_intents`
and `broker_orders`. They were deliberately absent from v1: they describe an
external system this repository did not yet talk to, and their semantics
cannot be guessed correctly in advance. They exist now because Phase 7 has
actually read the broker's vocabulary. `broker_orders.status` is still stored
as **opaque text** - this module does not model a broker state machine, and a
stored row means an order was accepted, never that it filled.

**What is still deliberately absent.** There is no `fills`, `executions`,
`broker_accounts`, or `reconciliation_runs` table. Phase 8 owns those, and the
same reasoning applies: better added later, correctly, than guessed at now.
`risk_events` remains deliberately generic - Phase 5 owns the meaning of a risk
decision, and this module stores opaque text rather than importing or
mirroring its model.

**Transactions.** Connections run with `isolation_level=None`, so nothing is
implicitly in a transaction and nothing implicitly commits. Every write goes
through `transaction()`, which commits on success and rolls back on *any*
exception. Nested use joins the outer transaction, so a caller can group
several writes into one atomic unit and be certain a failure halfway through
leaves none of them behind.

**Timestamps.** Every persisted timestamp is ISO-8601 UTC in one canonical
fixed-width form, `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`. Aware inputs in another
zone are converted; **naive datetimes are rejected** rather than guessed at,
because interpreting one as local time would silently corrupt an audit trail.
Domain times (`started_at`, `signal_timestamp`, ...) are supplied by the
caller; `created_at` is stamped here and records when the row was written.

**SQL safety.** Every statement is a literal; every value is bound as a
parameter. No SQL is ever built by string interpolation.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: The schema this module understands. There is no migration *framework*, but
#: there is one small explicit upgrade path, v1 -> v2, applied in a single
#: transaction by `initialize_database`. A database written by a **newer**
#: version is refused, never downgraded.
SCHEMA_VERSION = 2

#: The oldest on-disk version this module can still open. Anything below it
#: predates the migration path and is refused rather than guessed at.
MIN_MIGRATABLE_SCHEMA_VERSION = 1

#: Conventional local database location. Nothing here creates it implicitly,
#: and everything under this pattern is git-ignored (docs/SPEC.md section 5).
DEFAULT_DATABASE_PATH = Path("data/autotrader.db")

#: How long a connection waits on a locked database before giving up. Kept in
#: sync with `_PRAGMA_BUSY_TIMEOUT` by a test - a PRAGMA argument cannot be a
#: bound parameter, so the statement below has to spell the number out.
BUSY_TIMEOUT_MS = 5_000

#: Every table this schema owns. Used for the lightweight consistency check in
#: `initialize_database`, and asserted against in tests so a table this phase
#: has not earned cannot appear here unnoticed. The last two arrived with v2.
REQUIRED_TABLES: tuple[str, ...] = (
    "schema_metadata",
    "strategy_runs",
    "signals",
    "risk_events",
    "system_events",
    "positions",
    "order_intents",
    "broker_orders",
)

#: The tables v2 adds on top of v1. Kept separate so the migration and a fresh
#: initialization are provably the same set of statements.
V2_TABLES: tuple[str, ...] = ("order_intents", "broker_orders")

#: How a run was executed. Plain text, not a SQL enum. `PAPER` is a label a
#: future phase may write; Phase 6 implements no paper behaviour whatsoever.
RUN_MODES: tuple[str, ...] = ("BACKTEST", "PAPER")

RUN_STATUS_RUNNING = "RUNNING"
RUN_STATUS_COMPLETED = "COMPLETED"
RUN_STATUS_FAILED = "FAILED"

#: A run ends exactly once, in one of these states.
TERMINAL_RUN_STATUSES: tuple[str, ...] = (RUN_STATUS_COMPLETED, RUN_STATUS_FAILED)
RUN_STATUSES: tuple[str, ...] = (RUN_STATUS_RUNNING, *TERMINAL_RUN_STATUSES)

#: The Phase 3 signal vocabulary, unchanged. `EXIT` is stored as `EXIT`: this
#: layer never translates it into `SELL`, because a signal is not a trade.
SIGNAL_TYPES: tuple[str, ...] = ("BUY", "EXIT")

#: The two sides an order may take. Long only: there is no short vocabulary to
#: store, so a short cannot be recorded even by a caller that bypassed Python.
ORDER_SIDES: tuple[str, ...] = ("BUY", "SELL")

#: An intent's lifecycle, deliberately small (docs/SPEC.md section 8, Phase 7).
#:
#: `CREATED`    persisted locally; the broker has not been called for it.
#: `SUBMITTING` a submission is in flight; a row left here means the process
#:              died mid-call and the broker's view is unknown.
#: `SUBMITTED`  the broker returned an order for it.
#: `UNKNOWN`    the submission outcome is genuinely unknown - a timeout or
#:              another ambiguous transport failure. It is **never** retried
#:              automatically; Phase 8 resolves it by `client_order_id`.
#: `REJECTED`   the broker refused it outright, so no order exists.
INTENT_STATUS_CREATED = "CREATED"
INTENT_STATUS_SUBMITTING = "SUBMITTING"
INTENT_STATUS_SUBMITTED = "SUBMITTED"
INTENT_STATUS_UNKNOWN = "UNKNOWN"
INTENT_STATUS_REJECTED = "REJECTED"

ORDER_INTENT_STATUSES: tuple[str, ...] = (
    INTENT_STATUS_CREATED,
    INTENT_STATUS_SUBMITTING,
    INTENT_STATUS_SUBMITTED,
    INTENT_STATUS_UNKNOWN,
    INTENT_STATUS_REJECTED,
)

#: The one persisted timestamp form. Fixed width, so text ordering is also
#: chronological ordering.
TIMESTAMP_FORMAT = "YYYY-MM-DDTHH:MM:SS.ffffff+00:00"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class StateError(Exception):
    """Base class for every controlled failure in this module."""


class StateInputError(StateError):
    """A caller-supplied value violates the persistence contract."""


class DatabaseStateError(StateError):
    """The database on disk is not in a state this module can use.

    Raised instead of attempting a repair. A malformed or inconsistent
    operational database is an operator problem, not something persistence
    code should quietly rewrite.
    """


class UnsupportedSchemaVersionError(DatabaseStateError):
    """The database was written by a schema version this code does not know."""


class DuplicateSignalError(StateError):
    """The exact same logical signal is already recorded for that run."""


class UnknownStrategyRunError(StateError):
    """The referenced `strategy_runs.id` does not exist."""


class DuplicateOrderIntentError(StateError):
    """That `client_order_id` is already recorded.

    The idempotency key is unique by construction, so a repeat means a caller
    tried to create a *second* local intent for an order the broker may
    already know about. Storing it twice would defeat the whole purpose of the
    key, so the write is refused.
    """


class UnknownOrderIntentError(StateError):
    """The referenced `order_intents.id` does not exist."""


class DuplicateBrokerOrderError(StateError):
    """A different broker order is already recorded under that identity.

    Phase 7 allows exactly one broker order per intent, so a second distinct
    `broker_order_id` for the same intent - or the same `broker_order_id`
    under a different intent - is a contradiction rather than an update.
    """


# --------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------


def to_utc_text(value: datetime, field: str = "timestamp") -> str:
    """Serialize an aware datetime to the canonical UTC text form.

    Aware values in any zone are converted to UTC. A naive value is rejected:
    there is no correct guess for its offset, and assuming local time would
    silently misdate an audit record.
    """
    if not isinstance(value, datetime):
        raise StateInputError(f"{field} must be a datetime, got {type(value).__name__}.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise StateInputError(
            f"{field} must be timezone-aware; naive datetimes are rejected rather than "
            "assumed to be UTC or local time. Pass a value with tzinfo set."
        )
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def from_utc_text(text: str) -> datetime:
    """Parse a stored timestamp back into an aware UTC datetime."""
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError) as error:
        raise DatabaseStateError(
            f"Stored timestamp {text!r} is not ISO-8601; expected {TIMESTAMP_FORMAT}."
        ) from error
    if parsed.tzinfo is None:
        raise DatabaseStateError(
            f"Stored timestamp {text!r} has no UTC offset; expected {TIMESTAMP_FORMAT}."
        )
    return parsed.astimezone(UTC)


def _now_text() -> str:
    """The current instant in canonical form, for `created_at` columns."""
    return to_utc_text(datetime.now(UTC))


# --------------------------------------------------------------------------
# Value validation
# --------------------------------------------------------------------------


def _require_text(value: str, field: str) -> str:
    """Require a non-blank string, stored exactly as supplied."""
    if not isinstance(value, str):
        raise StateInputError(f"{field} must be a string, got {type(value).__name__}.")
    if not value.strip():
        raise StateInputError(f"{field} must not be empty.")
    return value


def _optional_text(value: str | None, field: str) -> str | None:
    """Allow NULL, but reject a blank string masquerading as a value."""
    if value is None:
        return None
    return _require_text(value, field)


def _require_choice(value: str, field: str, allowed: tuple[str, ...]) -> str:
    _require_text(value, field)
    if value not in allowed:
        raise StateInputError(f"{field} must be one of {', '.join(allowed)}, got {value!r}.")
    return value


def _require_symbol(value: str, field: str = "symbol") -> str:
    """Require an uppercase ticker.

    Case is not normalized here. Stored data is already uppercase by the
    Phase 1 contract, and silently upper-casing would let `spy` and `SPY`
    become one row without the caller ever learning it had a bug.
    """
    symbol = _require_text(value, field)
    if symbol != symbol.upper():
        raise StateInputError(f"{field} must be uppercase, got {symbol!r}.")
    return symbol


def _require_quantity(value: int, field: str = "quantity") -> int:
    """Require a non-negative whole-share quantity.

    The system is long only (docs/SPEC.md section 3), so a negative quantity
    would represent a short position that no code path can legitimately
    produce. The same rule is a CHECK constraint in the schema.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise StateInputError(f"{field} must be an int, got {type(value).__name__}.")
    if value < 0:
        raise StateInputError(
            f"{field} must be >= 0; this system is long only and cannot hold a short "
            f"position. Got {value}."
        )
    return value


def _require_positive_quantity(value: int, field: str) -> int:
    """Require a whole-share quantity strictly greater than zero.

    An order for zero shares is not an order. The same rule is a CHECK
    constraint in the schema.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise StateInputError(f"{field} must be an int, got {type(value).__name__}.")
    if value <= 0:
        raise StateInputError(f"{field} must be greater than zero, got {value}.")
    return value


def _require_positive_price(value: float, field: str) -> float:
    """Require a finite price greater than zero.

    Finiteness is checked here rather than in SQL: a SQLite `CHECK (x > 0)`
    rejects NaN and negative infinity but happily accepts positive infinity,
    which would size an order against a meaningless mark.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StateInputError(f"{field} must be a number, got {type(value).__name__}.")
    price = float(value)
    if not math.isfinite(price) or price <= 0:
        raise StateInputError(f"{field} must be finite and greater than zero, got {value!r}.")
    return price


def _require_average_price(value: float | None, field: str = "average_price") -> float | None:
    """Allow NULL - a flat position has no average price - else require > 0."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StateInputError(f"{field} must be a number, got {type(value).__name__}.")
    price = float(value)
    if not math.isfinite(price) or price <= 0:
        raise StateInputError(f"{field} must be finite and greater than zero, got {value!r}.")
    return price


# --------------------------------------------------------------------------
# Read models
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyRun:
    """One logical strategy session. `ended_at` is None while it is running."""

    id: int
    strategy_name: str
    mode: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class StoredSignal:
    """A persisted Phase 3 signal.

    Named to stay distinct from `autotrader.strategies.Signal`: that one is a
    freshly computed observation, this one is a durable record of it.
    `signal_timestamp` is the bar that made the crossover knowable - it is not
    an execution time, and there is deliberately no price.
    """

    id: int
    strategy_run_id: int
    signal_timestamp: datetime
    symbol: str
    signal_type: str
    reason: str
    created_at: datetime


@dataclass(frozen=True)
class RiskEvent:
    """A risk decision, stored generically.

    `decision` and `reason_code` are opaque strings owned by the risk engine
    (Phase 5). This module neither interprets nor constrains their values.
    """

    id: int
    strategy_run_id: int | None
    event_timestamp: datetime
    symbol: str | None
    decision: str
    reason_code: str
    message: str | None
    created_at: datetime


@dataclass(frozen=True)
class SystemEvent:
    """A general operational/audit event."""

    id: int
    event_timestamp: datetime
    event_type: str
    message: str | None
    created_at: datetime


@dataclass(frozen=True)
class StoredOrderIntent:
    """A durable local record of an order this system decided to place.

    Named distinctly from `autotrader.execution.OrderIntent`: that one is the
    freshly built domain object, this one is the row it became. It is written
    **before** the broker is called, and its `client_order_id` is the anchor
    that lets a later phase ask the broker what happened to it.

    `approved_quantity` is the risk engine's number, never the caller's
    original request. There is deliberately no broker order id here - that
    belongs to `StoredBrokerOrder`, which only exists once a broker answered.
    """

    id: int
    client_order_id: str
    strategy_run_id: int | None
    created_at: datetime
    symbol: str
    side: str
    requested_quantity: int
    approved_quantity: int
    reference_price: float
    risk_reason_code: str
    status: str
    updated_at: datetime


@dataclass(frozen=True)
class StoredBrokerOrder:
    """The latest normalized snapshot of what the broker said about an order.

    A snapshot, not a state machine: `status` is the broker's own vocabulary,
    stored as opaque text. This module neither interprets it nor decides what
    a transition means - Phase 8 owns that. In particular a stored row proves
    the order was **accepted**, never that it was filled.
    """

    id: int
    order_intent_id: int
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: int
    filled_quantity: int
    filled_average_price: float | None
    status: str
    submitted_at: datetime | None
    filled_at: datetime | None
    updated_at: datetime
    created_at: datetime


@dataclass(frozen=True)
class Position:
    """The latest known **local** position snapshot.

    Nothing synchronizes this table, and it must not be read as the broker's
    authoritative state. It may only ever be written from a position actually
    *observed* at the broker - never inferred from an order this system sent,
    because an accepted order is not a fill. Reconciling it is Phase 8's job.
    """

    symbol: str
    quantity: int
    average_price: float | None
    updated_at: datetime


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

_V1_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE schema_metadata (
        id             INTEGER PRIMARY KEY CHECK (id = 1),
        schema_version INTEGER NOT NULL,
        created_at     TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE strategy_runs (
        id            INTEGER PRIMARY KEY,
        strategy_name TEXT NOT NULL CHECK (strategy_name <> ''),
        mode          TEXT NOT NULL CHECK (mode <> ''),
        status        TEXT NOT NULL CHECK (status <> ''),
        started_at    TEXT NOT NULL,
        ended_at      TEXT,
        created_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE signals (
        id               INTEGER PRIMARY KEY,
        strategy_run_id  INTEGER NOT NULL REFERENCES strategy_runs (id),
        signal_timestamp TEXT NOT NULL,
        symbol           TEXT NOT NULL CHECK (symbol <> ''),
        signal_type      TEXT NOT NULL CHECK (signal_type IN ('BUY', 'EXIT')),
        reason           TEXT NOT NULL CHECK (reason <> ''),
        created_at       TEXT NOT NULL,
        UNIQUE (strategy_run_id, signal_timestamp, symbol, signal_type, reason)
    )
    """,
    """
    CREATE TABLE risk_events (
        id              INTEGER PRIMARY KEY,
        strategy_run_id INTEGER REFERENCES strategy_runs (id),
        event_timestamp TEXT NOT NULL,
        symbol          TEXT,
        decision        TEXT NOT NULL CHECK (decision <> ''),
        reason_code     TEXT NOT NULL CHECK (reason_code <> ''),
        message         TEXT,
        created_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE system_events (
        id              INTEGER PRIMARY KEY,
        event_timestamp TEXT NOT NULL,
        event_type      TEXT NOT NULL CHECK (event_type <> ''),
        message         TEXT,
        created_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE positions (
        symbol        TEXT PRIMARY KEY CHECK (symbol <> ''),
        quantity      INTEGER NOT NULL CHECK (quantity >= 0),
        average_price REAL CHECK (average_price IS NULL OR average_price > 0),
        updated_at    TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_signals_strategy_run ON signals (strategy_run_id)",
    "CREATE INDEX idx_risk_events_strategy_run ON risk_events (strategy_run_id)",
)

#: Everything v2 adds. A fresh database runs v1 then v2 and is stamped 2; an
#: existing v1 database runs exactly these statements and is re-stamped. One
#: list, two callers, so "migrated to v2" and "created as v2" cannot drift -
#: a test compares the resulting `sqlite_master` of both and requires equality.
_V2_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE order_intents (
        id                 INTEGER PRIMARY KEY,
        client_order_id    TEXT NOT NULL UNIQUE CHECK (client_order_id <> ''),
        strategy_run_id    INTEGER REFERENCES strategy_runs (id),
        created_at         TEXT NOT NULL,
        symbol             TEXT NOT NULL CHECK (symbol <> ''),
        side               TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
        requested_quantity INTEGER NOT NULL CHECK (requested_quantity > 0),
        approved_quantity  INTEGER NOT NULL CHECK (approved_quantity > 0),
        reference_price    REAL NOT NULL CHECK (reference_price > 0),
        risk_reason_code   TEXT NOT NULL CHECK (risk_reason_code <> ''),
        status             TEXT NOT NULL CHECK (
            status IN ('CREATED', 'SUBMITTING', 'SUBMITTED', 'UNKNOWN', 'REJECTED')
        ),
        updated_at         TEXT NOT NULL,
        CHECK (approved_quantity <= requested_quantity)
    )
    """,
    """
    CREATE TABLE broker_orders (
        id                   INTEGER PRIMARY KEY,
        order_intent_id      INTEGER NOT NULL UNIQUE REFERENCES order_intents (id),
        broker_order_id      TEXT NOT NULL UNIQUE CHECK (broker_order_id <> ''),
        client_order_id      TEXT NOT NULL UNIQUE CHECK (client_order_id <> ''),
        symbol               TEXT NOT NULL CHECK (symbol <> ''),
        side                 TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
        quantity             INTEGER NOT NULL CHECK (quantity > 0),
        filled_quantity      INTEGER NOT NULL CHECK (filled_quantity >= 0),
        filled_average_price REAL CHECK (filled_average_price IS NULL OR filled_average_price > 0),
        status               TEXT NOT NULL CHECK (status <> ''),
        submitted_at         TEXT,
        filled_at            TEXT,
        updated_at           TEXT NOT NULL,
        created_at           TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_order_intents_status ON order_intents (status)",
)

_SCHEMA_STATEMENTS: tuple[str, ...] = _V1_SCHEMA_STATEMENTS + _V2_SCHEMA_STATEMENTS

_PRAGMA_FOREIGN_KEYS = "PRAGMA foreign_keys = ON"
_PRAGMA_JOURNAL_MODE = "PRAGMA journal_mode = WAL"
# A PRAGMA argument cannot be a bound parameter, so the timeout is spelled out
# here and `BUSY_TIMEOUT_MS` is checked against the live value by a test.
_PRAGMA_BUSY_TIMEOUT = "PRAGMA busy_timeout = 5000"

_SELECT_TABLE_NAMES = "SELECT name FROM sqlite_master WHERE type = 'table'"
_SELECT_SCHEMA_VERSION = "SELECT schema_version FROM schema_metadata WHERE id = 1"
_INSERT_SCHEMA_VERSION = (
    "INSERT INTO schema_metadata (id, schema_version, created_at) VALUES (1, ?, ?)"
)
_UPDATE_SCHEMA_VERSION = "UPDATE schema_metadata SET schema_version = ? WHERE id = 1"


# --------------------------------------------------------------------------
# Connections and transactions
# --------------------------------------------------------------------------


@contextmanager
def connect(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open a configured connection to `path` and close it on exit.

    Foreign keys are enforced, journalling is WAL, and a busy timeout is set,
    on **every** connection - SQLite defaults foreign keys to off per
    connection, so forgetting this once would silently disable referential
    integrity for that caller. There is no connection pool: a single-user
    local process does not need one.

    This helper does not verify the schema. Call `initialize_database` first;
    like `sqlite3.connect`, it will otherwise happily create an empty file.
    """
    connection = sqlite3.connect(
        Path(path),
        isolation_level=None,
        timeout=BUSY_TIMEOUT_MS / 1000,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute(_PRAGMA_FOREIGN_KEYS)
        # Returns "wal" for a file database and "memory" for an in-memory one,
        # which has no journal to configure.
        connection.execute(_PRAGMA_JOURNAL_MODE).fetchone()
        connection.execute(_PRAGMA_BUSY_TIMEOUT)
        yield connection
    finally:
        connection.close()


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a unit of work atomically: commit on success, roll back on failure.

    Any exception - a constraint violation, a validation error, anything -
    rolls the whole block back, so a caller never observes a partially written
    multi-step state. Nested use joins the outer transaction and leaves the
    commit to it, which lets the small `record_*` helpers be composed into one
    atomic unit without any of them committing early.
    """
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


def _existing_table_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row["name"]) for row in connection.execute(_SELECT_TABLE_NAMES)}


def _read_schema_version(connection: sqlite3.Connection) -> int:
    rows = connection.execute(_SELECT_SCHEMA_VERSION).fetchall()
    if len(rows) != 1:
        raise DatabaseStateError(
            f"schema_metadata must hold exactly one row, found {len(rows)}. The database "
            "is inconsistent; this module will not repair it."
        )
    return int(rows[0]["schema_version"])


def _require_expected_tables(connection: sqlite3.Connection, version: int) -> None:
    """Refuse a database whose version marker and tables disagree."""
    existing = _existing_table_names(connection)
    missing = [table for table in REQUIRED_TABLES if table not in existing]
    if missing:
        raise DatabaseStateError(
            f"Database reports schema version {version} but is missing table(s): "
            f"{', '.join(missing)}. The database is inconsistent; this module will not "
            "repair it."
        )


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """Add the Phase 7 order tables to a v1 database, in place.

    Additive only: it creates `order_intents` and `broker_orders` and re-stamps
    the version marker. No existing table is dropped, recreated, renamed, or
    rewritten, so every Phase 6 row is left exactly as it was.

    The caller runs this inside `transaction()`, and SQLite's DDL is
    transactional, so a failure part-way through leaves the database on v1
    with neither new table present rather than half-upgraded.
    """
    existing = _existing_table_names(connection)
    conflicting = sorted(table for table in V2_TABLES if table in existing)
    if conflicting:
        raise DatabaseStateError(
            f"Cannot upgrade this database to schema version {SCHEMA_VERSION}: it is "
            f"marked version {MIN_MIGRATABLE_SCHEMA_VERSION} but already contains "
            f"table(s) {', '.join(conflicting)}. Refusing to migrate over an "
            "inconsistent database."
        )
    for statement in _V2_SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.execute(_UPDATE_SCHEMA_VERSION, (SCHEMA_VERSION,))


def _verify_or_migrate_schema(connection: sqlite3.Connection) -> None:
    """Bring an already-initialized database to the supported version, or refuse it.

    A **newer** database is refused and left untouched: downgrading would
    discard data this code cannot understand. A database at exactly
    `MIN_MIGRATABLE_SCHEMA_VERSION` is upgraded through the one explicit
    migration path. Anything older has no path and is refused rather than
    guessed at.
    """
    version = _read_schema_version(connection)
    if version > SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"Database schema version {version} is newer than the supported version "
            f"{SCHEMA_VERSION}. Refusing to open it; downgrading would discard data "
            "written by a newer version of this application."
        )
    if version < MIN_MIGRATABLE_SCHEMA_VERSION:
        raise DatabaseStateError(
            f"Database schema version {version} is older than the oldest supported "
            f"version {MIN_MIGRATABLE_SCHEMA_VERSION}, and there is no migration path."
        )
    if version < SCHEMA_VERSION:
        _migrate_v1_to_v2(connection)
    _require_expected_tables(connection, SCHEMA_VERSION)


def _create_schema(connection: sqlite3.Connection, existing: set[str]) -> None:
    """Create the current schema in a database that has none of it yet.

    A new database is built directly at `SCHEMA_VERSION`; it is never created
    at v1 and then migrated.
    """
    conflicting = sorted(table for table in REQUIRED_TABLES if table in existing)
    if conflicting:
        raise DatabaseStateError(
            f"Database has no schema_metadata but already contains table(s): "
            f"{', '.join(conflicting)}. Refusing to initialize over an inconsistent "
            "database."
        )
    for statement in _SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.execute(_INSERT_SCHEMA_VERSION, (SCHEMA_VERSION, _now_text()))


def initialize_database(path: str | Path) -> Path:
    """Create or verify the operational-state database at `path`.

    A new database is created directly at the current `SCHEMA_VERSION`. An
    existing database at the previous version is upgraded through one small
    explicit migration, additively: no Phase 6 table is dropped, recreated, or
    rewritten, so existing rows survive untouched. The whole thing runs in a
    single transaction, so a failed upgrade rolls back and leaves the database
    on its original version rather than half-migrated.

    Idempotent: once at the current version, repeated calls verify and change
    nothing. A database written by a **newer** schema version is refused and
    left untouched, and an inconsistent one raises rather than being repaired.

    Returns the database path. Missing parent directories are created.
    """
    database_path = Path(path)
    if str(database_path) != ":memory:":
        database_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(database_path) as connection, transaction(connection):
        existing = _existing_table_names(connection)
        if "schema_metadata" in existing:
            _verify_or_migrate_schema(connection)
        else:
            _create_schema(connection, existing)
    return database_path


def get_schema_version(connection: sqlite3.Connection) -> int:
    """Return the schema version recorded in the database."""
    return _read_schema_version(connection)


# --------------------------------------------------------------------------
# Strategy runs
# --------------------------------------------------------------------------

_INSERT_STRATEGY_RUN = """
INSERT INTO strategy_runs (strategy_name, mode, status, started_at, ended_at, created_at)
VALUES (?, ?, ?, ?, NULL, ?)
"""
_SELECT_STRATEGY_RUN = """
SELECT id, strategy_name, mode, status, started_at, ended_at, created_at
FROM strategy_runs
WHERE id = ?
"""
_SELECT_STRATEGY_RUNS = """
SELECT id, strategy_name, mode, status, started_at, ended_at, created_at
FROM strategy_runs
ORDER BY started_at, id
"""
_UPDATE_STRATEGY_RUN_END = "UPDATE strategy_runs SET status = ?, ended_at = ? WHERE id = ?"


def _to_strategy_run(row: sqlite3.Row) -> StrategyRun:
    ended_at = row["ended_at"]
    return StrategyRun(
        id=int(row["id"]),
        strategy_name=str(row["strategy_name"]),
        mode=str(row["mode"]),
        status=str(row["status"]),
        started_at=from_utc_text(row["started_at"]),
        ended_at=None if ended_at is None else from_utc_text(ended_at),
        created_at=from_utc_text(row["created_at"]),
    )


def record_strategy_run(
    connection: sqlite3.Connection,
    *,
    strategy_name: str,
    mode: str,
    started_at: datetime,
    status: str = RUN_STATUS_RUNNING,
) -> int:
    """Open a strategy run and return its local row id.

    The id is a local database identifier. It is not, and must never be
    presented as, a broker identifier.
    """
    name = _require_text(strategy_name, "strategy_name")
    run_mode = _require_choice(mode, "mode", RUN_MODES)
    run_status = _require_choice(status, "status", RUN_STATUSES)
    started_text = to_utc_text(started_at, "started_at")
    with transaction(connection):
        cursor = connection.execute(
            _INSERT_STRATEGY_RUN,
            (name, run_mode, run_status, started_text, _now_text()),
        )
    return int(cursor.lastrowid)


def finish_strategy_run(
    connection: sqlite3.Connection,
    strategy_run_id: int,
    *,
    ended_at: datetime,
    status: str = RUN_STATUS_COMPLETED,
) -> None:
    """Close a running strategy run.

    A run ends once. Finishing a run that is already finished is rejected
    rather than allowed to overwrite the recorded end of an audit trail, and
    an end before the start is rejected as incoherent.
    """
    run_status = _require_choice(status, "status", TERMINAL_RUN_STATUSES)
    ended_text = to_utc_text(ended_at, "ended_at")
    with transaction(connection):
        row = connection.execute(_SELECT_STRATEGY_RUN, (strategy_run_id,)).fetchone()
        if row is None:
            raise UnknownStrategyRunError(f"No strategy run with id {strategy_run_id!r}.")
        run = _to_strategy_run(row)
        if run.status != RUN_STATUS_RUNNING:
            raise StateInputError(
                f"Strategy run {strategy_run_id} is already {run.status}; a run ends once."
            )
        if from_utc_text(ended_text) < run.started_at:
            raise StateInputError(
                f"ended_at {ended_text} precedes strategy run {strategy_run_id}'s "
                f"started_at {to_utc_text(run.started_at)}."
            )
        connection.execute(_UPDATE_STRATEGY_RUN_END, (run_status, ended_text, strategy_run_id))


def get_strategy_run(connection: sqlite3.Connection, strategy_run_id: int) -> StrategyRun | None:
    """Return one strategy run, or None when it does not exist."""
    row = connection.execute(_SELECT_STRATEGY_RUN, (strategy_run_id,)).fetchone()
    return None if row is None else _to_strategy_run(row)


def list_strategy_runs(connection: sqlite3.Connection) -> list[StrategyRun]:
    """Every strategy run, ordered by start time then id."""
    return [_to_strategy_run(row) for row in connection.execute(_SELECT_STRATEGY_RUNS)]


# --------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------

_INSERT_SIGNAL = """
INSERT INTO signals
    (strategy_run_id, signal_timestamp, symbol, signal_type, reason, created_at)
VALUES (?, ?, ?, ?, ?, ?)
"""
_SELECT_SIGNALS = """
SELECT id, strategy_run_id, signal_timestamp, symbol, signal_type, reason, created_at
FROM signals
ORDER BY signal_timestamp, id
"""
_SELECT_SIGNALS_FOR_RUN = """
SELECT id, strategy_run_id, signal_timestamp, symbol, signal_type, reason, created_at
FROM signals
WHERE strategy_run_id = ?
ORDER BY signal_timestamp, id
"""


def _to_stored_signal(row: sqlite3.Row) -> StoredSignal:
    return StoredSignal(
        id=int(row["id"]),
        strategy_run_id=int(row["strategy_run_id"]),
        signal_timestamp=from_utc_text(row["signal_timestamp"]),
        symbol=str(row["symbol"]),
        signal_type=str(row["signal_type"]),
        reason=str(row["reason"]),
        created_at=from_utc_text(row["created_at"]),
    )


def record_signal(
    connection: sqlite3.Connection,
    *,
    strategy_run_id: int,
    signal_timestamp: datetime,
    symbol: str,
    signal_type: str,
    reason: str,
) -> int:
    """Record one strategy signal against a run, and return its row id.

    A signal is an immutable fact and is stored exactly as produced: `EXIT`
    stays `EXIT` and is never rewritten as `SELL`, no EMA is recomputed, no
    risk rule is consulted, and nothing is executed. Persistence is not
    orchestration.

    The same logical signal - same run, timestamp, symbol, type, and reason -
    cannot be stored twice: a repeat raises `DuplicateSignalError` rather than
    silently creating a second copy. This is a storage invariant only; the
    order idempotency required before any live trading (docs/SPEC.md section
    6E) is Phase 7's problem and is not attempted here.
    """
    ticker = _require_symbol(symbol)
    kind = _require_choice(signal_type, "signal_type", SIGNAL_TYPES)
    reason_text = _require_text(reason, "reason")
    timestamp_text = to_utc_text(signal_timestamp, "signal_timestamp")
    try:
        with transaction(connection):
            cursor = connection.execute(
                _INSERT_SIGNAL,
                (strategy_run_id, timestamp_text, ticker, kind, reason_text, _now_text()),
            )
    except sqlite3.IntegrityError as error:
        raise _translate_signal_integrity_error(error, strategy_run_id) from None
    return int(cursor.lastrowid)


def _translate_signal_integrity_error(
    error: sqlite3.IntegrityError, strategy_run_id: int
) -> Exception:
    """Turn a signal-insert constraint failure into a specific state error."""
    if error.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE":
        return DuplicateSignalError(
            "This exact signal is already recorded for strategy run "
            f"{strategy_run_id}; refusing to store a duplicate."
        )
    if error.sqlite_errorname == "SQLITE_CONSTRAINT_FOREIGNKEY":
        return UnknownStrategyRunError(
            f"No strategy run with id {strategy_run_id!r}; a signal must belong to a run."
        )
    return error


def list_signals(
    connection: sqlite3.Connection, *, strategy_run_id: int | None = None
) -> list[StoredSignal]:
    """Signals ordered by signal timestamp then id, optionally for one run."""
    if strategy_run_id is None:
        rows = connection.execute(_SELECT_SIGNALS)
    else:
        rows = connection.execute(_SELECT_SIGNALS_FOR_RUN, (strategy_run_id,))
    return [_to_stored_signal(row) for row in rows]


# --------------------------------------------------------------------------
# Risk events
# --------------------------------------------------------------------------

_INSERT_RISK_EVENT = """
INSERT INTO risk_events
    (strategy_run_id, event_timestamp, symbol, decision, reason_code, message, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""
_SELECT_RISK_EVENTS = """
SELECT id, strategy_run_id, event_timestamp, symbol, decision, reason_code, message, created_at
FROM risk_events
ORDER BY event_timestamp, id
"""


def _to_risk_event(row: sqlite3.Row) -> RiskEvent:
    run_id = row["strategy_run_id"]
    symbol = row["symbol"]
    message = row["message"]
    return RiskEvent(
        id=int(row["id"]),
        strategy_run_id=None if run_id is None else int(run_id),
        event_timestamp=from_utc_text(row["event_timestamp"]),
        symbol=None if symbol is None else str(symbol),
        decision=str(row["decision"]),
        reason_code=str(row["reason_code"]),
        message=None if message is None else str(message),
        created_at=from_utc_text(row["created_at"]),
    )


def record_risk_event(
    connection: sqlite3.Connection,
    *,
    event_timestamp: datetime,
    decision: str,
    reason_code: str,
    strategy_run_id: int | None = None,
    symbol: str | None = None,
    message: str | None = None,
) -> int:
    """Append a risk-decision record to the audit trail.

    Deliberately generic. `decision` and `reason_code` are opaque non-empty
    strings whose vocabulary belongs to the risk engine (Phase 5), which is
    developed independently and is not imported, mirrored, or constrained
    here. `strategy_run_id` and `symbol` are nullable, because a risk decision
    can be global (a daily loss limit) rather than tied to one run or one
    ticker.
    """
    decision_text = _require_text(decision, "decision")
    reason_text = _require_text(reason_code, "reason_code")
    ticker = None if symbol is None else _require_symbol(symbol)
    message_text = _optional_text(message, "message")
    timestamp_text = to_utc_text(event_timestamp, "event_timestamp")
    try:
        with transaction(connection):
            cursor = connection.execute(
                _INSERT_RISK_EVENT,
                (
                    strategy_run_id,
                    timestamp_text,
                    ticker,
                    decision_text,
                    reason_text,
                    message_text,
                    _now_text(),
                ),
            )
    except sqlite3.IntegrityError as error:
        if error.sqlite_errorname == "SQLITE_CONSTRAINT_FOREIGNKEY":
            raise UnknownStrategyRunError(f"No strategy run with id {strategy_run_id!r}.") from None
        raise
    return int(cursor.lastrowid)


def list_risk_events(connection: sqlite3.Connection) -> list[RiskEvent]:
    """Risk events ordered by event timestamp then id."""
    return [_to_risk_event(row) for row in connection.execute(_SELECT_RISK_EVENTS)]


# --------------------------------------------------------------------------
# System events
# --------------------------------------------------------------------------

_INSERT_SYSTEM_EVENT = """
INSERT INTO system_events (event_timestamp, event_type, message, created_at)
VALUES (?, ?, ?, ?)
"""
_SELECT_SYSTEM_EVENTS = """
SELECT id, event_timestamp, event_type, message, created_at
FROM system_events
ORDER BY event_timestamp, id
"""


def _to_system_event(row: sqlite3.Row) -> SystemEvent:
    message = row["message"]
    return SystemEvent(
        id=int(row["id"]),
        event_timestamp=from_utc_text(row["event_timestamp"]),
        event_type=str(row["event_type"]),
        message=None if message is None else str(message),
        created_at=from_utc_text(row["created_at"]),
    )


def record_system_event(
    connection: sqlite3.Connection,
    *,
    event_timestamp: datetime,
    event_type: str,
    message: str | None = None,
) -> int:
    """Append a general operational event and return its row id."""
    type_text = _require_text(event_type, "event_type")
    message_text = _optional_text(message, "message")
    timestamp_text = to_utc_text(event_timestamp, "event_timestamp")
    with transaction(connection):
        cursor = connection.execute(
            _INSERT_SYSTEM_EVENT,
            (timestamp_text, type_text, message_text, _now_text()),
        )
    return int(cursor.lastrowid)


def list_system_events(connection: sqlite3.Connection) -> list[SystemEvent]:
    """System events ordered by event timestamp then id."""
    return [_to_system_event(row) for row in connection.execute(_SELECT_SYSTEM_EVENTS)]


# --------------------------------------------------------------------------
# Positions
# --------------------------------------------------------------------------

_UPSERT_POSITION = """
INSERT INTO positions (symbol, quantity, average_price, updated_at)
VALUES (?, ?, ?, ?)
ON CONFLICT (symbol) DO UPDATE SET
    quantity = excluded.quantity,
    average_price = excluded.average_price,
    updated_at = excluded.updated_at
"""
_SELECT_POSITION = """
SELECT symbol, quantity, average_price, updated_at FROM positions WHERE symbol = ?
"""
_SELECT_POSITIONS = """
SELECT symbol, quantity, average_price, updated_at FROM positions ORDER BY symbol
"""


def _to_position(row: sqlite3.Row) -> Position:
    average_price = row["average_price"]
    return Position(
        symbol=str(row["symbol"]),
        quantity=int(row["quantity"]),
        average_price=None if average_price is None else float(average_price),
        updated_at=from_utc_text(row["updated_at"]),
    )


def upsert_position(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    quantity: int,
    updated_at: datetime,
    average_price: float | None = None,
) -> None:
    """Store the latest local position snapshot for `symbol`.

    One row per symbol: an existing snapshot is replaced, not appended to.
    This is **local** state. Phase 6 populates nothing from a broker, and
    reconciling this table against a broker's authoritative positions is
    Phase 8's job (docs/SPEC.md section 6E).

    `quantity` must be a non-negative whole number - the system is long only -
    and `average_price` is either NULL, which is the natural value for a flat
    position, or a finite number greater than zero. Both rules are also CHECK
    constraints in the schema, so a write that bypassed this function still
    cannot store a short position. No P&L is computed or stored.
    """
    ticker = _require_symbol(symbol)
    shares = _require_quantity(quantity)
    price = _require_average_price(average_price)
    updated_text = to_utc_text(updated_at, "updated_at")
    with transaction(connection):
        connection.execute(_UPSERT_POSITION, (ticker, shares, price, updated_text))


def get_position(connection: sqlite3.Connection, symbol: str) -> Position | None:
    """Return the stored snapshot for `symbol`, or None when there is none.

    None means "no local snapshot", which is not the same claim as "flat at
    the broker". Nothing here has ever spoken to a broker.
    """
    row = connection.execute(_SELECT_POSITION, (_require_symbol(symbol),)).fetchone()
    return None if row is None else _to_position(row)


def list_positions(connection: sqlite3.Connection) -> list[Position]:
    """Every stored position snapshot, ordered by symbol."""
    return [_to_position(row) for row in connection.execute(_SELECT_POSITIONS)]


# --------------------------------------------------------------------------
# Order intents (schema v2)
#
# The durable local record written *before* the broker is called. Its
# `client_order_id` is the idempotency key required by docs/SPEC.md section
# 6E, and it is what a later phase uses to ask the broker what happened to a
# submission this process did not live to see the answer to.
# --------------------------------------------------------------------------

_INSERT_ORDER_INTENT = """
INSERT INTO order_intents
    (client_order_id, strategy_run_id, created_at, symbol, side, requested_quantity,
     approved_quantity, reference_price, risk_reason_code, status, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_SELECT_ORDER_INTENT = """
SELECT id, client_order_id, strategy_run_id, created_at, symbol, side, requested_quantity,
       approved_quantity, reference_price, risk_reason_code, status, updated_at
FROM order_intents
WHERE id = ?
"""
_SELECT_ORDER_INTENT_BY_CLIENT_ID = """
SELECT id, client_order_id, strategy_run_id, created_at, symbol, side, requested_quantity,
       approved_quantity, reference_price, risk_reason_code, status, updated_at
FROM order_intents
WHERE client_order_id = ?
"""
_SELECT_ORDER_INTENTS = """
SELECT id, client_order_id, strategy_run_id, created_at, symbol, side, requested_quantity,
       approved_quantity, reference_price, risk_reason_code, status, updated_at
FROM order_intents
ORDER BY id
"""
_UPDATE_ORDER_INTENT_STATUS = "UPDATE order_intents SET status = ?, updated_at = ? WHERE id = ?"


def _to_stored_order_intent(row: sqlite3.Row) -> StoredOrderIntent:
    run_id = row["strategy_run_id"]
    return StoredOrderIntent(
        id=int(row["id"]),
        client_order_id=str(row["client_order_id"]),
        strategy_run_id=None if run_id is None else int(run_id),
        created_at=from_utc_text(row["created_at"]),
        symbol=str(row["symbol"]),
        side=str(row["side"]),
        requested_quantity=int(row["requested_quantity"]),
        approved_quantity=int(row["approved_quantity"]),
        reference_price=float(row["reference_price"]),
        risk_reason_code=str(row["risk_reason_code"]),
        status=str(row["status"]),
        updated_at=from_utc_text(row["updated_at"]),
    )


def record_order_intent(
    connection: sqlite3.Connection,
    *,
    client_order_id: str,
    created_at: datetime,
    symbol: str,
    side: str,
    requested_quantity: int,
    approved_quantity: int,
    reference_price: float,
    risk_reason_code: str,
    strategy_run_id: int | None = None,
    status: str = INTENT_STATUS_CREATED,
) -> int:
    """Store one order intent and return its row id.

    `approved_quantity` is the risk engine's number and may never exceed
    `requested_quantity` - enforced here and again as a CHECK constraint, so a
    write that bypassed this function still cannot record an order larger than
    risk allowed.

    `client_order_id` is unique. A repeat raises `DuplicateOrderIntentError`
    rather than creating a second intent, because the whole point of the key
    is that one intent maps to at most one broker order.
    """
    client_id = _require_text(client_order_id, "client_order_id")
    ticker = _require_symbol(symbol)
    order_side = _require_choice(side, "side", ORDER_SIDES)
    requested = _require_positive_quantity(requested_quantity, "requested_quantity")
    approved = _require_positive_quantity(approved_quantity, "approved_quantity")
    if approved > requested:
        raise StateInputError(
            f"approved_quantity ({approved}) must not exceed requested_quantity "
            f"({requested}); risk may only ever size an order down."
        )
    price = _require_positive_price(reference_price, "reference_price")
    reason = _require_text(risk_reason_code, "risk_reason_code")
    intent_status = _require_choice(status, "status", ORDER_INTENT_STATUSES)
    created_text = to_utc_text(created_at, "created_at")

    try:
        with transaction(connection):
            cursor = connection.execute(
                _INSERT_ORDER_INTENT,
                (
                    client_id,
                    strategy_run_id,
                    created_text,
                    ticker,
                    order_side,
                    requested,
                    approved,
                    price,
                    reason,
                    intent_status,
                    _now_text(),
                ),
            )
    except sqlite3.IntegrityError as error:
        if error.sqlite_errorname == "SQLITE_CONSTRAINT_FOREIGNKEY":
            raise UnknownStrategyRunError(f"No strategy run with id {strategy_run_id!r}.") from None
        if error.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE":
            raise DuplicateOrderIntentError(
                f"An order intent with client_order_id {client_id!r} already exists. "
                "Reuse it rather than creating a second intent for the same order."
            ) from None
        raise
    return int(cursor.lastrowid)


def update_order_intent_status(
    connection: sqlite3.Connection,
    *,
    order_intent_id: int,
    status: str,
    updated_at: datetime,
) -> None:
    """Move one intent to a new lifecycle status.

    Only the status and its timestamp change. The `client_order_id` is
    immutable by design: a submission whose outcome is unknown must keep the
    exact key the broker may already have seen, so that it can be resolved
    later instead of being re-sent under a new identity.

    This module does not police which transitions are legal - Phase 8 owns the
    reconciliation state machine.
    """
    intent_status = _require_choice(status, "status", ORDER_INTENT_STATUSES)
    updated_text = to_utc_text(updated_at, "updated_at")
    with transaction(connection):
        cursor = connection.execute(
            _UPDATE_ORDER_INTENT_STATUS, (intent_status, updated_text, order_intent_id)
        )
        if cursor.rowcount != 1:
            raise UnknownOrderIntentError(f"No order intent with id {order_intent_id!r}.")


def get_order_intent(
    connection: sqlite3.Connection, order_intent_id: int
) -> StoredOrderIntent | None:
    """Return one intent by row id, or None."""
    row = connection.execute(_SELECT_ORDER_INTENT, (order_intent_id,)).fetchone()
    return None if row is None else _to_stored_order_intent(row)


def get_order_intent_by_client_id(
    connection: sqlite3.Connection, client_order_id: str
) -> StoredOrderIntent | None:
    """Return one intent by its idempotency key, or None.

    This is the lookup a crash-recovery pass needs: the key is generated once,
    committed before the broker call, and never regenerated.
    """
    client_id = _require_text(client_order_id, "client_order_id")
    row = connection.execute(_SELECT_ORDER_INTENT_BY_CLIENT_ID, (client_id,)).fetchone()
    return None if row is None else _to_stored_order_intent(row)


def list_order_intents(connection: sqlite3.Connection) -> list[StoredOrderIntent]:
    """Every stored order intent, oldest first."""
    return [_to_stored_order_intent(row) for row in connection.execute(_SELECT_ORDER_INTENTS)]


# --------------------------------------------------------------------------
# Broker orders (schema v2)
#
# The latest normalized snapshot of what the broker reported. One row per
# intent: Phase 7 submits at most one order per intent, and re-reading that
# order updates the snapshot rather than appending a second row. There is no
# fills or executions table - a snapshot is not an execution history.
# --------------------------------------------------------------------------

_UPSERT_BROKER_ORDER = """
INSERT INTO broker_orders
    (order_intent_id, broker_order_id, client_order_id, symbol, side, quantity,
     filled_quantity, filled_average_price, status, submitted_at, filled_at,
     updated_at, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (order_intent_id) DO UPDATE SET
    quantity = excluded.quantity,
    filled_quantity = excluded.filled_quantity,
    filled_average_price = excluded.filled_average_price,
    status = excluded.status,
    submitted_at = excluded.submitted_at,
    filled_at = excluded.filled_at,
    updated_at = excluded.updated_at
WHERE broker_orders.broker_order_id = excluded.broker_order_id
"""
_SELECT_BROKER_ORDER_BY_INTENT = """
SELECT id, order_intent_id, broker_order_id, client_order_id, symbol, side, quantity,
       filled_quantity, filled_average_price, status, submitted_at, filled_at,
       updated_at, created_at
FROM broker_orders
WHERE order_intent_id = ?
"""
_SELECT_BROKER_ORDER_BY_CLIENT_ID = """
SELECT id, order_intent_id, broker_order_id, client_order_id, symbol, side, quantity,
       filled_quantity, filled_average_price, status, submitted_at, filled_at,
       updated_at, created_at
FROM broker_orders
WHERE client_order_id = ?
"""
_SELECT_BROKER_ORDERS = """
SELECT id, order_intent_id, broker_order_id, client_order_id, symbol, side, quantity,
       filled_quantity, filled_average_price, status, submitted_at, filled_at,
       updated_at, created_at
FROM broker_orders
ORDER BY id
"""


def _to_stored_broker_order(row: sqlite3.Row) -> StoredBrokerOrder:
    average_price = row["filled_average_price"]
    submitted_at = row["submitted_at"]
    filled_at = row["filled_at"]
    return StoredBrokerOrder(
        id=int(row["id"]),
        order_intent_id=int(row["order_intent_id"]),
        broker_order_id=str(row["broker_order_id"]),
        client_order_id=str(row["client_order_id"]),
        symbol=str(row["symbol"]),
        side=str(row["side"]),
        quantity=int(row["quantity"]),
        filled_quantity=int(row["filled_quantity"]),
        filled_average_price=None if average_price is None else float(average_price),
        status=str(row["status"]),
        submitted_at=None if submitted_at is None else from_utc_text(submitted_at),
        filled_at=None if filled_at is None else from_utc_text(filled_at),
        updated_at=from_utc_text(row["updated_at"]),
        created_at=from_utc_text(row["created_at"]),
    )


def upsert_broker_order(
    connection: sqlite3.Connection,
    *,
    order_intent_id: int,
    broker_order_id: str,
    client_order_id: str,
    symbol: str,
    side: str,
    quantity: int,
    status: str,
    updated_at: datetime,
    filled_quantity: int = 0,
    filled_average_price: float | None = None,
    submitted_at: datetime | None = None,
    filled_at: datetime | None = None,
) -> int:
    """Store or refresh the broker's snapshot for one intent, returning its row id.

    `status` is the broker's own vocabulary, stored as opaque non-empty text.
    Nothing here interprets it, maps it onto a local state machine, or infers
    a position from it: a stored row means the broker **accepted** an order,
    never that it filled.

    Re-reading the same broker order updates the mutable fields in place. A
    *different* `broker_order_id` for the same intent - or the same broker
    order arriving under a different intent - raises
    `DuplicateBrokerOrderError`, because Phase 7 submits exactly one order per
    intent and a second one would be a contradiction, not an update.
    """
    broker_id = _require_text(broker_order_id, "broker_order_id")
    client_id = _require_text(client_order_id, "client_order_id")
    ticker = _require_symbol(symbol)
    order_side = _require_choice(side, "side", ORDER_SIDES)
    shares = _require_positive_quantity(quantity, "quantity")
    filled = _require_quantity(filled_quantity, "filled_quantity")
    average_price = _require_average_price(filled_average_price, "filled_average_price")
    status_text = _require_text(status, "status")
    updated_text = to_utc_text(updated_at, "updated_at")
    submitted_text = None if submitted_at is None else to_utc_text(submitted_at, "submitted_at")
    filled_text = None if filled_at is None else to_utc_text(filled_at, "filled_at")

    try:
        with transaction(connection):
            cursor = connection.execute(
                _UPSERT_BROKER_ORDER,
                (
                    order_intent_id,
                    broker_id,
                    client_id,
                    ticker,
                    order_side,
                    shares,
                    filled,
                    average_price,
                    status_text,
                    submitted_text,
                    filled_text,
                    updated_text,
                    _now_text(),
                ),
            )
            # The ON CONFLICT clause updates nothing when the broker order id
            # differs, so a contradicting snapshot is a silent no-op here and
            # must be reported rather than passed off as a successful write.
            if cursor.rowcount != 1:
                raise DuplicateBrokerOrderError(
                    f"Order intent {order_intent_id!r} is already recorded against a "
                    f"different broker order than {broker_id!r}. Phase 7 allows exactly "
                    "one broker order per intent."
                )
            # `lastrowid` is only meaningful for the INSERT path, so the row id
            # is read back rather than inferred.
            stored = connection.execute(
                _SELECT_BROKER_ORDER_BY_INTENT, (order_intent_id,)
            ).fetchone()
    except sqlite3.IntegrityError as error:
        if error.sqlite_errorname == "SQLITE_CONSTRAINT_FOREIGNKEY":
            raise UnknownOrderIntentError(f"No order intent with id {order_intent_id!r}.") from None
        if error.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE":
            raise DuplicateBrokerOrderError(
                f"Broker order {broker_id!r} (client_order_id {client_id!r}) is already "
                "recorded against a different order intent."
            ) from None
        raise
    return int(stored["id"])


def get_broker_order_by_intent(
    connection: sqlite3.Connection, order_intent_id: int
) -> StoredBrokerOrder | None:
    """Return the broker snapshot for one intent, or None if it has none.

    None means "this process never recorded a broker answer", which is **not**
    the same claim as "the broker has no such order". Resolving that
    difference is Phase 8's job.
    """
    row = connection.execute(_SELECT_BROKER_ORDER_BY_INTENT, (order_intent_id,)).fetchone()
    return None if row is None else _to_stored_broker_order(row)


def get_broker_order_by_client_id(
    connection: sqlite3.Connection, client_order_id: str
) -> StoredBrokerOrder | None:
    """Return the broker snapshot carrying that idempotency key, or None."""
    client_id = _require_text(client_order_id, "client_order_id")
    row = connection.execute(_SELECT_BROKER_ORDER_BY_CLIENT_ID, (client_id,)).fetchone()
    return None if row is None else _to_stored_broker_order(row)


def list_broker_orders(connection: sqlite3.Connection) -> list[StoredBrokerOrder]:
    """Every stored broker-order snapshot, oldest first."""
    return [_to_stored_broker_order(row) for row in connection.execute(_SELECT_BROKER_ORDERS)]


__all__ = [
    "BUSY_TIMEOUT_MS",
    "DEFAULT_DATABASE_PATH",
    "INTENT_STATUS_CREATED",
    "INTENT_STATUS_REJECTED",
    "INTENT_STATUS_SUBMITTED",
    "INTENT_STATUS_SUBMITTING",
    "INTENT_STATUS_UNKNOWN",
    "MIN_MIGRATABLE_SCHEMA_VERSION",
    "ORDER_INTENT_STATUSES",
    "ORDER_SIDES",
    "REQUIRED_TABLES",
    "RUN_MODES",
    "RUN_STATUSES",
    "RUN_STATUS_COMPLETED",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_RUNNING",
    "SCHEMA_VERSION",
    "SIGNAL_TYPES",
    "TERMINAL_RUN_STATUSES",
    "TIMESTAMP_FORMAT",
    "V2_TABLES",
    "DatabaseStateError",
    "DuplicateBrokerOrderError",
    "DuplicateOrderIntentError",
    "DuplicateSignalError",
    "Position",
    "RiskEvent",
    "StateError",
    "StateInputError",
    "StoredBrokerOrder",
    "StoredOrderIntent",
    "StoredSignal",
    "StrategyRun",
    "SystemEvent",
    "UnknownOrderIntentError",
    "UnknownStrategyRunError",
    "UnsupportedSchemaVersionError",
    "connect",
    "finish_strategy_run",
    "from_utc_text",
    "get_broker_order_by_client_id",
    "get_broker_order_by_intent",
    "get_order_intent",
    "get_order_intent_by_client_id",
    "get_position",
    "get_schema_version",
    "get_strategy_run",
    "initialize_database",
    "list_broker_orders",
    "list_order_intents",
    "list_positions",
    "list_risk_events",
    "list_signals",
    "list_strategy_runs",
    "list_system_events",
    "record_order_intent",
    "record_risk_event",
    "record_signal",
    "record_strategy_run",
    "record_system_event",
    "to_utc_text",
    "transaction",
    "update_order_intent_status",
    "upsert_broker_order",
    "upsert_position",
]
