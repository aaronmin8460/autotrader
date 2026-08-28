"""The local SQLite operational-state store (schema v4).

This module is **persistence infrastructure and nothing else**. It opens a
local SQLite database, creates a small fixed schema, and stores a handful of
durable operational records. It does not decide anything, orchestrate
anything, or talk to anyone: no strategy runs here, no risk rule is evaluated,
no order is *placed*, and no broker is contacted. It imports only the standard
library, needs no credentials, and opens no socket.

**Why SQLite.** One user, one local process, one file (docs/SPEC.md section 5).
The standard library ships the driver, so the dependency footprint of this
module is zero - no ORM, no migration framework, and no database service.

**What v3 changed, and why.** The crypto pivot made every stored quantity
fractional. Quantities that were `INTEGER` columns cannot express 0.0001 BTC at
all, so v3 migrates them to **exact decimal text** and the read models return
`decimal.Decimal`. v3 also adds `daily_risk_baselines`, the durable record of
the first account equity observed on a UTC calendar day - the baseline the
crypto daily-loss halt measures against, because a 24/7 market has no
equity-session "previous close" to borrow.

**Prices stayed REAL.** The audit covered every money and quantity column.
`INTEGER` quantities were migrated because whole numbers cannot represent a
fractional coin; `REAL` prices were **not**, because a float already represents
a fractional USD mark, and moving them to text would silently discard the
`CHECK (... > 0)` constraints that make an impossible price unstorable even by
a writer that bypassed this module. A price here is a mark, never a quantity.

**What v4 changed, and why.** Phase 8 reconciles local state against broker
truth, and two facts had nowhere to live. First, an intent whose absence at the
broker has been *confirmed* is neither `CREATED` nor `REJECTED`: it is a stale
decision that will never be sent, so `CONFIRMED_NOT_SUBMITTED` was added to the
intent vocabulary - which is a CHECK constraint, hence a table rebuild. Second,
a reconciliation run has to leave evidence, so `reconciliation_runs` and
`reconciliation_events` record when a run happened, what it concluded, whether
trading was allowed afterwards, and which order or position it touched.

**What is still deliberately absent.** There is no `fills`, `executions`, or
`broker_accounts` table. Order-level `filled_quantity` carries everything
reconciliation actually needs, and a fill-level history would be a table this
system has not yet earned. `risk_events` remains deliberately generic - the
risk engine owns the meaning of a risk decision, and this module stores opaque
text rather than importing or mirroring its model.

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
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

#: The schema this module understands. There is no migration *framework*, but
#: there is an explicit ordered upgrade path - v1 -> v2 -> v3 -> v4 - applied in
#: a single transaction by `initialize_database`. A database written by a
#: **newer** version is refused, never downgraded.
SCHEMA_VERSION = 4

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
#: `initialize_database`, and asserted against in tests so a table this
#: milestone has not earned cannot appear here unnoticed.
REQUIRED_TABLES: tuple[str, ...] = (
    "schema_metadata",
    "strategy_runs",
    "signals",
    "risk_events",
    "system_events",
    "positions",
    "order_intents",
    "broker_orders",
    "daily_risk_baselines",
    "reconciliation_runs",
    "reconciliation_events",
)

#: The tables v2 added on top of v1. Kept separate so the v1 -> v2 migration
#: and the historical v2 shape stay provably the same set of statements.
V2_TABLES: tuple[str, ...] = ("order_intents", "broker_orders")

#: The table v3 adds on top of v2.
V3_TABLES: tuple[str, ...] = ("daily_risk_baselines",)

#: The tables v4 adds on top of v3, both of them Phase 8 reconciliation audit.
V4_TABLES: tuple[str, ...] = ("reconciliation_runs", "reconciliation_events")

#: Transient names the v2 -> v3 rebuild parks the old tables under. They exist
#: only inside the migration transaction and are dropped before it commits, so
#: finding one on disk means a migration was interrupted in a way SQLite is
#: supposed to make impossible.
_PRE_V3_TABLES: tuple[str, ...] = (
    "positions__pre_v3",
    "order_intents__pre_v3",
    "broker_orders__pre_v3",
)

#: The same, for the v3 -> v4 rebuild of `order_intents`.
_PRE_V4_TABLES: tuple[str, ...] = ("order_intents__pre_v4",)

#: How a run was executed. Plain text, not a SQL enum. `PAPER` is a label a
#: future phase may write; this module implements no paper behaviour whatsoever.
RUN_MODES: tuple[str, ...] = ("BACKTEST", "PAPER")

RUN_STATUS_RUNNING = "RUNNING"
RUN_STATUS_COMPLETED = "COMPLETED"
RUN_STATUS_FAILED = "FAILED"

#: A run ends exactly once, in one of these states.
TERMINAL_RUN_STATUSES: tuple[str, ...] = (RUN_STATUS_COMPLETED, RUN_STATUS_FAILED)
RUN_STATUSES: tuple[str, ...] = (RUN_STATUS_RUNNING, *TERMINAL_RUN_STATUSES)

#: The strategy's signal vocabulary, unchanged. `EXIT` is stored as `EXIT`: this
#: layer never translates it into `SELL`, because a signal is not a trade.
SIGNAL_TYPES: tuple[str, ...] = ("BUY", "EXIT")

#: The two sides an order may take. Long only: there is no short vocabulary to
#: store, so a short cannot be recorded even by a caller that bypassed Python.
ORDER_SIDES: tuple[str, ...] = ("BUY", "SELL")

#: An intent's lifecycle, deliberately small (docs/SPEC.md sections 8 C7 and
#: 8 C8).
#:
#: `CREATED`    persisted locally; the broker has not been called for it.
#: `SUBMITTING` a submission is in flight; a row left here means the process
#:              died mid-call and the broker's view is unknown.
#: `SUBMITTED`  the broker returned an order for it. What became of that order
#:              afterwards - filled, partially filled, canceled, rejected - is
#:              the *broker's* status, kept in `broker_orders`, not here.
#: `UNKNOWN`    the submission outcome is genuinely unknown - a timeout or
#:              another ambiguous transport failure. It is **never** retried
#:              automatically; reconciliation resolves it by `client_order_id`.
#: `REJECTED`   the broker refused it outright, so no order exists.
#: `CONFIRMED_NOT_SUBMITTED`
#:              reconciliation asked the broker about this exact
#:              `client_order_id`, more than once, and the broker definitively
#:              answered that no such order exists (schema v4). Terminal: the
#:              decision was never sent and will never be sent, because
#:              executing a stale signal after a restart is not recovery.
INTENT_STATUS_CREATED = "CREATED"
INTENT_STATUS_SUBMITTING = "SUBMITTING"
INTENT_STATUS_SUBMITTED = "SUBMITTED"
INTENT_STATUS_UNKNOWN = "UNKNOWN"
INTENT_STATUS_REJECTED = "REJECTED"
INTENT_STATUS_CONFIRMED_NOT_SUBMITTED = "CONFIRMED_NOT_SUBMITTED"

ORDER_INTENT_STATUSES: tuple[str, ...] = (
    INTENT_STATUS_CREATED,
    INTENT_STATUS_SUBMITTING,
    INTENT_STATUS_SUBMITTED,
    INTENT_STATUS_UNKNOWN,
    INTENT_STATUS_REJECTED,
    INTENT_STATUS_CONFIRMED_NOT_SUBMITTED,
)

#: An intent in one of these states is finished: nothing a broker could say
#: would move it again, so reconciliation does not query it.
TERMINAL_INTENT_STATUSES: tuple[str, ...] = (
    INTENT_STATUS_REJECTED,
    INTENT_STATUS_CONFIRMED_NOT_SUBMITTED,
)

#: What one reconciliation run concluded (schema v4).
#:
#: `CLEAN`      local state already agreed with the broker.
#: `REPAIRED`   differences were resolved from verified broker truth.
#: `UNRESOLVED` at least one item stayed ambiguous.
#: `FAILED`     the run could not complete.
RECONCILIATION_STATUS_CLEAN = "CLEAN"
RECONCILIATION_STATUS_REPAIRED = "REPAIRED"
RECONCILIATION_STATUS_UNRESOLVED = "UNRESOLVED"
RECONCILIATION_STATUS_FAILED = "FAILED"

RECONCILIATION_STATUSES: tuple[str, ...] = (
    RECONCILIATION_STATUS_CLEAN,
    RECONCILIATION_STATUS_REPAIRED,
    RECONCILIATION_STATUS_UNRESOLVED,
    RECONCILIATION_STATUS_FAILED,
)

#: What one reconciliation *event* is about. `RUN` is the run-level summary.
RECONCILIATION_CATEGORIES: tuple[str, ...] = ("ORDER", "POSITION", "RUN")

#: What happened to one reconciled item. `OBSERVED` is evidence that changed
#: nothing locally - something worth recording but not a repair and not a
#: reason to block trading.
RECONCILIATION_OUTCOMES: tuple[str, ...] = (
    RECONCILIATION_STATUS_CLEAN,
    RECONCILIATION_STATUS_REPAIRED,
    RECONCILIATION_STATUS_UNRESOLVED,
    RECONCILIATION_STATUS_FAILED,
    "OBSERVED",
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


class UnknownReconciliationRunError(StateError):
    """The referenced `reconciliation_runs.id` does not exist."""


class DuplicateBrokerOrderError(StateError):
    """A different broker order is already recorded under that identity.

    Exactly one broker order is allowed per intent, so a second distinct
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


def utc_risk_date(moment: datetime) -> date:
    """The UTC calendar date `moment` falls on.

    The crypto risk day runs 00:00 UTC to the next 00:00 UTC. There is no
    exchange session to anchor it to, so the boundary is the UTC date and
    nothing else. Naive datetimes are rejected for the same reason they are
    everywhere else here: guessing an offset would silently misdate a day.
    """
    if not isinstance(moment, datetime):
        raise StateInputError(f"moment must be a datetime, got {type(moment).__name__}.")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise StateInputError(
            "moment must be timezone-aware; a naive datetime cannot be placed on a UTC "
            "calendar day without guessing its offset."
        )
    return moment.astimezone(UTC).date()


def to_risk_date_text(value: date, field: str = "risk_date_utc") -> str:
    """Serialize a UTC risk day to its canonical ``YYYY-MM-DD`` form."""
    if isinstance(value, datetime) or not isinstance(value, date):
        raise StateInputError(
            f"{field} must be a datetime.date (not a datetime), got {type(value).__name__}. "
            "Use utc_risk_date() to derive one from an aware timestamp."
        )
    return value.isoformat()


def from_risk_date_text(text: str) -> date:
    """Parse a stored risk day back into a `datetime.date`."""
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError) as error:
        raise DatabaseStateError(
            f"Stored risk date {text!r} is not an ISO-8601 calendar date."
        ) from error


# --------------------------------------------------------------------------
# Exact decimal storage
#
# Crypto quantities are fractional, and a binary float cannot hold one exactly.
# They are therefore stored as canonical decimal **text** and read back as
# `decimal.Decimal`, so a persisted quantity round-trips to the same value it
# was written from. The storage string is an implementation detail: no public
# read model exposes it, and every public quantity is a Decimal.
# --------------------------------------------------------------------------


def to_decimal_text(value: Decimal | int, field: str = "quantity") -> str:
    """Serialize an exact quantity to its canonical decimal string.

    Rendered in plain fixed-point notation - never ``1E-4`` - so a stored value
    is readable and unambiguous, and so two writers of the same value produce
    the same text. The scale the caller supplied is preserved: ``1.25000000``
    stays ``1.25000000`` and ``1`` stays ``1``, because a quantity's precision
    is information about how it was derived.

    A `float` is refused rather than converted. A binary float is an
    approximation, and quietly writing one into a column whose whole purpose is
    exactness would defeat the point.
    """
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise StateInputError(
            f"{field} must be a Decimal or an int, got {type(value).__name__}. Floats are "
            "refused: an exact quantity cannot be recovered from a binary approximation."
        )
    number = Decimal(value)
    if not number.is_finite():
        raise StateInputError(f"{field} must be finite, got {value!r}.")
    return format(number, "f")


def from_decimal_text(text: object, field: str = "quantity") -> Decimal:
    """Parse a stored canonical decimal string back into a `Decimal`."""
    try:
        number = Decimal(str(text))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise DatabaseStateError(f"Stored {field} {text!r} is not a decimal number.") from error
    if not number.is_finite():
        raise DatabaseStateError(f"Stored {field} {text!r} is not finite.")
    return number


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
    """Require an uppercase symbol, stored exactly as supplied.

    Case is not normalized here. Stored data is already uppercase by the
    market-data contract, and silently upper-casing would let `btc/usd` and
    `BTC/USD` become one row without the caller ever learning it had a bug.
    The slash in a canonical crypto pair is left alone: it is part of the
    symbol, and this layer stores what it is given.
    """
    symbol = _require_text(value, field)
    if symbol != symbol.upper():
        raise StateInputError(f"{field} must be uppercase, got {symbol!r}.")
    return symbol


def _require_quantity(value: Decimal | int, field: str = "quantity") -> Decimal:
    """Require a non-negative fractional quantity.

    The system is long only (docs/SPEC.md section 3), so a negative quantity
    would represent a short position that no code path can legitimately
    produce. The same rule is a CHECK constraint in the schema.
    """
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise StateInputError(f"{field} must be a Decimal or an int, got {type(value).__name__}.")
    number = Decimal(value)
    if not number.is_finite():
        raise StateInputError(f"{field} must be finite, got {value!r}.")
    if number < 0:
        raise StateInputError(
            f"{field} must be >= 0; this system is long only and cannot hold a short "
            f"position. Got {value}."
        )
    return number


def _require_positive_quantity(value: Decimal | int, field: str) -> Decimal:
    """Require a fractional quantity strictly greater than zero.

    An order for zero of an asset is not an order. The same rule is a CHECK
    constraint in the schema.
    """
    number = _require_quantity(value, field)
    if number <= 0:
        raise StateInputError(f"{field} must be greater than zero, got {value}.")
    return number


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
    """A persisted strategy signal.

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
    (C5). This module neither interprets nor constrains their values.
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
    requested_quantity: Decimal
    approved_quantity: Decimal
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
    quantity: Decimal
    filled_quantity: Decimal
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
    quantity: Decimal
    average_price: float | None
    updated_at: datetime


@dataclass(frozen=True)
class ReconciliationRun:
    """One completed reconciliation pass against the broker (schema v4).

    The durable answer to "when did reconciliation last run, what did it
    conclude, and was trading allowed afterwards?". `safe_to_trade` is stored
    rather than re-derived, so the record still says what the runtime was
    actually told even if the rule that produced it is later changed.

    A row appears only once a pass has finished. A process that died mid-pass
    leaves no run row, which is the honest reading: repairs it had already
    committed are durable, but nothing concluded.
    """

    id: int
    started_at: datetime
    completed_at: datetime
    status: str
    safe_to_trade: bool
    orders_checked: int
    positions_checked: int
    issues_count: int
    unresolved_count: int
    created_at: datetime


@dataclass(frozen=True)
class ReconciliationEvent:
    """One thing a reconciliation run observed, repaired, or could not resolve.

    Deliberately narrow: an evidence line, not an event-sourcing record. It
    answers which order or position was touched and why, and nothing here can
    be replayed to reconstruct state - `order_intents`, `broker_orders`, and
    `positions` hold that.
    """

    id: int
    reconciliation_run_id: int
    event_timestamp: datetime
    category: str
    outcome: str
    symbol: str | None
    client_order_id: str | None
    detail: str
    created_at: datetime


@dataclass(frozen=True)
class DailyRiskBaseline:
    """The account equity a UTC risk day is measured against.

    Written once per UTC calendar date, by whichever observation happens first
    on that date, and never overwritten afterwards: a baseline that moved
    during the day would silently reset the daily-loss halt. `baseline_equity`
    is the equity that was observed, and `captured_at` is when.
    """

    risk_date_utc: date
    baseline_equity: Decimal
    captured_at: datetime


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

_CREATE_SCHEMA_METADATA = """
    CREATE TABLE schema_metadata (
        id             INTEGER PRIMARY KEY CHECK (id = 1),
        schema_version INTEGER NOT NULL,
        created_at     TEXT    NOT NULL
    )
    """
_CREATE_STRATEGY_RUNS = """
    CREATE TABLE strategy_runs (
        id            INTEGER PRIMARY KEY,
        strategy_name TEXT NOT NULL CHECK (strategy_name <> ''),
        mode          TEXT NOT NULL CHECK (mode <> ''),
        status        TEXT NOT NULL CHECK (status <> ''),
        started_at    TEXT NOT NULL,
        ended_at      TEXT,
        created_at    TEXT NOT NULL
    )
    """
_CREATE_SIGNALS = """
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
    """
_CREATE_RISK_EVENTS = """
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
    """
_CREATE_SYSTEM_EVENTS = """
    CREATE TABLE system_events (
        id              INTEGER PRIMARY KEY,
        event_timestamp TEXT NOT NULL,
        event_type      TEXT NOT NULL CHECK (event_type <> ''),
        message         TEXT,
        created_at      TEXT NOT NULL
    )
    """
_CREATE_INDEX_SIGNALS = "CREATE INDEX idx_signals_strategy_run ON signals (strategy_run_id)"
_CREATE_INDEX_RISK_EVENTS = (
    "CREATE INDEX idx_risk_events_strategy_run ON risk_events (strategy_run_id)"
)
_CREATE_INDEX_ORDER_INTENTS_STATUS = (
    "CREATE INDEX idx_order_intents_status ON order_intents (status)"
)

# --------------------------------------------------------------------------
# Historical shapes
#
# These are the v1 and v2 tables exactly as they were written on disk. They are
# retained because a database created by an older release still holds them and
# the migration path has to read them - not because anything creates them
# afresh. A new database is built directly at the current version.
# --------------------------------------------------------------------------

_CREATE_POSITIONS_V1 = """
    CREATE TABLE positions (
        symbol        TEXT PRIMARY KEY CHECK (symbol <> ''),
        quantity      INTEGER NOT NULL CHECK (quantity >= 0),
        average_price REAL CHECK (average_price IS NULL OR average_price > 0),
        updated_at    TEXT NOT NULL
    )
    """

_V1_SCHEMA_STATEMENTS: tuple[str, ...] = (
    _CREATE_SCHEMA_METADATA,
    _CREATE_STRATEGY_RUNS,
    _CREATE_SIGNALS,
    _CREATE_RISK_EVENTS,
    _CREATE_SYSTEM_EVENTS,
    _CREATE_POSITIONS_V1,
    _CREATE_INDEX_SIGNALS,
    _CREATE_INDEX_RISK_EVENTS,
)

_CREATE_ORDER_INTENTS_V2 = """
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
    """
_CREATE_BROKER_ORDERS_V2 = """
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
    """

#: Everything v2 added on top of v1. One list, used by the v1 -> v2 migration
#: and by the tests that reconstruct a historical database.
_V2_SCHEMA_STATEMENTS: tuple[str, ...] = (
    _CREATE_ORDER_INTENTS_V2,
    _CREATE_BROKER_ORDERS_V2,
    _CREATE_INDEX_ORDER_INTENTS_STATUS,
)

# --------------------------------------------------------------------------
# Current shapes (v3)
#
# Quantities are exact decimal TEXT. The CHECK constraints CAST to REAL so that
# a negative or zero quantity is still unstorable by a writer that bypassed
# this module; the cast is a coarse guard, not the value - Python holds the
# exact Decimal, and the text column holds the exact string.
# --------------------------------------------------------------------------

_CREATE_POSITIONS = """
    CREATE TABLE positions (
        symbol        TEXT PRIMARY KEY CHECK (symbol <> ''),
        quantity      TEXT NOT NULL CHECK (quantity <> '' AND CAST(quantity AS REAL) >= 0),
        average_price REAL CHECK (average_price IS NULL OR average_price > 0),
        updated_at    TEXT NOT NULL
    )
    """
_CREATE_ORDER_INTENTS_V3 = """
    CREATE TABLE order_intents (
        id                 INTEGER PRIMARY KEY,
        client_order_id    TEXT NOT NULL UNIQUE CHECK (client_order_id <> ''),
        strategy_run_id    INTEGER REFERENCES strategy_runs (id),
        created_at         TEXT NOT NULL,
        symbol             TEXT NOT NULL CHECK (symbol <> ''),
        side               TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
        requested_quantity TEXT NOT NULL CHECK (
            requested_quantity <> '' AND CAST(requested_quantity AS REAL) > 0
        ),
        approved_quantity  TEXT NOT NULL CHECK (
            approved_quantity <> '' AND CAST(approved_quantity AS REAL) > 0
        ),
        reference_price    REAL NOT NULL CHECK (reference_price > 0),
        risk_reason_code   TEXT NOT NULL CHECK (risk_reason_code <> ''),
        status             TEXT NOT NULL CHECK (
            status IN ('CREATED', 'SUBMITTING', 'SUBMITTED', 'UNKNOWN', 'REJECTED')
        ),
        updated_at         TEXT NOT NULL,
        CHECK (CAST(approved_quantity AS REAL) <= CAST(requested_quantity AS REAL))
    )
    """
_CREATE_BROKER_ORDERS = """
    CREATE TABLE broker_orders (
        id                   INTEGER PRIMARY KEY,
        order_intent_id      INTEGER NOT NULL UNIQUE REFERENCES order_intents (id),
        broker_order_id      TEXT NOT NULL UNIQUE CHECK (broker_order_id <> ''),
        client_order_id      TEXT NOT NULL UNIQUE CHECK (client_order_id <> ''),
        symbol               TEXT NOT NULL CHECK (symbol <> ''),
        side                 TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
        quantity             TEXT NOT NULL CHECK (
            quantity <> '' AND CAST(quantity AS REAL) > 0
        ),
        filled_quantity      TEXT NOT NULL CHECK (
            filled_quantity <> '' AND CAST(filled_quantity AS REAL) >= 0
        ),
        filled_average_price REAL CHECK (filled_average_price IS NULL OR filled_average_price > 0),
        status               TEXT NOT NULL CHECK (status <> ''),
        submitted_at         TEXT,
        filled_at            TEXT,
        updated_at           TEXT NOT NULL,
        created_at           TEXT NOT NULL
    )
    """
_CREATE_DAILY_RISK_BASELINES = """
    CREATE TABLE daily_risk_baselines (
        risk_date_utc   TEXT PRIMARY KEY CHECK (risk_date_utc <> ''),
        baseline_equity TEXT NOT NULL CHECK (
            baseline_equity <> '' AND CAST(baseline_equity AS REAL) > 0
        ),
        captured_at     TEXT NOT NULL
    )
    """

# --------------------------------------------------------------------------
# Current shapes (v4)
#
# `order_intents` gains one status. `reconciliation_runs` and
# `reconciliation_events` are the audit trail of Phase 8 reconciliation:
# `safe_to_trade` is stored as 0/1 with a CHECK, so an unreadable third value
# cannot appear in the one field a runtime consults before trading.
# --------------------------------------------------------------------------

_CREATE_ORDER_INTENTS = """
    CREATE TABLE order_intents (
        id                 INTEGER PRIMARY KEY,
        client_order_id    TEXT NOT NULL UNIQUE CHECK (client_order_id <> ''),
        strategy_run_id    INTEGER REFERENCES strategy_runs (id),
        created_at         TEXT NOT NULL,
        symbol             TEXT NOT NULL CHECK (symbol <> ''),
        side               TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
        requested_quantity TEXT NOT NULL CHECK (
            requested_quantity <> '' AND CAST(requested_quantity AS REAL) > 0
        ),
        approved_quantity  TEXT NOT NULL CHECK (
            approved_quantity <> '' AND CAST(approved_quantity AS REAL) > 0
        ),
        reference_price    REAL NOT NULL CHECK (reference_price > 0),
        risk_reason_code   TEXT NOT NULL CHECK (risk_reason_code <> ''),
        status             TEXT NOT NULL CHECK (
            status IN (
                'CREATED', 'SUBMITTING', 'SUBMITTED', 'UNKNOWN', 'REJECTED',
                'CONFIRMED_NOT_SUBMITTED'
            )
        ),
        updated_at         TEXT NOT NULL,
        CHECK (CAST(approved_quantity AS REAL) <= CAST(requested_quantity AS REAL))
    )
    """
_CREATE_RECONCILIATION_RUNS = """
    CREATE TABLE reconciliation_runs (
        id                INTEGER PRIMARY KEY,
        started_at        TEXT NOT NULL,
        completed_at      TEXT NOT NULL,
        status            TEXT NOT NULL CHECK (
            status IN ('CLEAN', 'REPAIRED', 'UNRESOLVED', 'FAILED')
        ),
        safe_to_trade     INTEGER NOT NULL CHECK (safe_to_trade IN (0, 1)),
        orders_checked    INTEGER NOT NULL CHECK (orders_checked >= 0),
        positions_checked INTEGER NOT NULL CHECK (positions_checked >= 0),
        issues_count      INTEGER NOT NULL CHECK (issues_count >= 0),
        unresolved_count  INTEGER NOT NULL CHECK (unresolved_count >= 0),
        created_at        TEXT NOT NULL
    )
    """
_CREATE_RECONCILIATION_EVENTS = """
    CREATE TABLE reconciliation_events (
        id                    INTEGER PRIMARY KEY,
        reconciliation_run_id INTEGER NOT NULL REFERENCES reconciliation_runs (id),
        event_timestamp       TEXT NOT NULL,
        category              TEXT NOT NULL CHECK (
            category IN ('ORDER', 'POSITION', 'RUN')
        ),
        outcome               TEXT NOT NULL CHECK (
            outcome IN ('CLEAN', 'REPAIRED', 'UNRESOLVED', 'FAILED', 'OBSERVED')
        ),
        symbol                TEXT,
        client_order_id       TEXT,
        detail                TEXT NOT NULL CHECK (detail <> ''),
        created_at            TEXT NOT NULL
    )
    """
_CREATE_INDEX_RECONCILIATION_EVENTS = (
    "CREATE INDEX idx_reconciliation_events_run ON reconciliation_events (reconciliation_run_id)"
)

#: A fresh database is built directly from these, at `SCHEMA_VERSION`. It is
#: never created at v1 and then migrated forward.
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    _CREATE_SCHEMA_METADATA,
    _CREATE_STRATEGY_RUNS,
    _CREATE_SIGNALS,
    _CREATE_RISK_EVENTS,
    _CREATE_SYSTEM_EVENTS,
    _CREATE_POSITIONS,
    _CREATE_ORDER_INTENTS,
    _CREATE_BROKER_ORDERS,
    _CREATE_DAILY_RISK_BASELINES,
    _CREATE_RECONCILIATION_RUNS,
    _CREATE_RECONCILIATION_EVENTS,
    _CREATE_INDEX_SIGNALS,
    _CREATE_INDEX_RISK_EVENTS,
    _CREATE_INDEX_ORDER_INTENTS_STATUS,
    _CREATE_INDEX_RECONCILIATION_EVENTS,
)

# --------------------------------------------------------------------------
# v2 -> v3 rebuild statements
#
# SQLite cannot change a column's type in place, so the three tables holding
# quantities are rebuilt: the old table is renamed aside, the current shape is
# created under the real name from the *same* literal a fresh database uses,
# every row is copied across with its quantities converted, and the renamed
# table is dropped. Because the new table is created rather than renamed into
# place, a migrated database's stored schema is byte-identical to a fresh one.
# --------------------------------------------------------------------------

_RENAME_POSITIONS_PRE_V3 = "ALTER TABLE positions RENAME TO positions__pre_v3"
_RENAME_ORDER_INTENTS_PRE_V3 = "ALTER TABLE order_intents RENAME TO order_intents__pre_v3"
_RENAME_BROKER_ORDERS_PRE_V3 = "ALTER TABLE broker_orders RENAME TO broker_orders__pre_v3"

_SELECT_PRE_V3_POSITIONS = """
SELECT symbol, quantity, average_price, updated_at FROM positions__pre_v3 ORDER BY symbol
"""
_SELECT_PRE_V3_ORDER_INTENTS = """
SELECT id, client_order_id, strategy_run_id, created_at, symbol, side, requested_quantity,
       approved_quantity, reference_price, risk_reason_code, status, updated_at
FROM order_intents__pre_v3
ORDER BY id
"""
_SELECT_PRE_V3_BROKER_ORDERS = """
SELECT id, order_intent_id, broker_order_id, client_order_id, symbol, side, quantity,
       filled_quantity, filled_average_price, status, submitted_at, filled_at,
       updated_at, created_at
FROM broker_orders__pre_v3
ORDER BY id
"""

_INSERT_MIGRATED_POSITION = """
INSERT INTO positions (symbol, quantity, average_price, updated_at) VALUES (?, ?, ?, ?)
"""
_INSERT_MIGRATED_ORDER_INTENT = """
INSERT INTO order_intents
    (id, client_order_id, strategy_run_id, created_at, symbol, side, requested_quantity,
     approved_quantity, reference_price, risk_reason_code, status, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_INSERT_MIGRATED_BROKER_ORDER = """
INSERT INTO broker_orders
    (id, order_intent_id, broker_order_id, client_order_id, symbol, side, quantity,
     filled_quantity, filled_average_price, status, submitted_at, filled_at,
     updated_at, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_DROP_PRE_V3_POSITIONS = "DROP TABLE positions__pre_v3"
_DROP_PRE_V3_ORDER_INTENTS = "DROP TABLE order_intents__pre_v3"
_DROP_PRE_V3_BROKER_ORDERS = "DROP TABLE broker_orders__pre_v3"

# --------------------------------------------------------------------------
# v3 -> v4 rebuild statements
#
# Only `order_intents` is rebuilt, and only because widening a CHECK constraint
# is not something SQLite can do in place. Nothing about the data changes: every
# column is copied across verbatim, and no existing row can hold the status the
# rebuild makes storable, so no row's meaning moves.
# --------------------------------------------------------------------------

_RENAME_ORDER_INTENTS_PRE_V4 = "ALTER TABLE order_intents RENAME TO order_intents__pre_v4"
_SELECT_PRE_V4_ORDER_INTENTS = """
SELECT id, client_order_id, strategy_run_id, created_at, symbol, side, requested_quantity,
       approved_quantity, reference_price, risk_reason_code, status, updated_at
FROM order_intents__pre_v4
ORDER BY id
"""
_DROP_PRE_V4_ORDER_INTENTS = "DROP TABLE order_intents__pre_v4"

_PRAGMA_FOREIGN_KEYS = "PRAGMA foreign_keys = ON"
_PRAGMA_FOREIGN_KEYS_OFF = "PRAGMA foreign_keys = OFF"
_PRAGMA_FOREIGN_KEY_CHECK = "PRAGMA foreign_key_check"
# Renaming a table normally rewrites every reference to it in other tables'
# foreign-key clauses. During the rebuild that is exactly wrong: `broker_orders`
# must keep pointing at `order_intents`, which is about to be recreated under
# that name. Legacy rename semantics leave those references alone.
_PRAGMA_LEGACY_ALTER_TABLE_ON = "PRAGMA legacy_alter_table = ON"
_PRAGMA_LEGACY_ALTER_TABLE_OFF = "PRAGMA legacy_alter_table = OFF"
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
    """Add the order tables to a v1 database, in place.

    Additive only: it creates `order_intents` and `broker_orders`. No existing
    table is dropped, recreated, renamed, or rewritten, so every v1 row is left
    exactly as it was. The tables are created in their **historical v2 shape**;
    the v2 -> v3 step that follows is what brings them to the current one, so
    an upgrade from v1 and an upgrade from v2 converge on the same result.

    The caller runs this inside `transaction()`, and SQLite's DDL is
    transactional, so a failure part-way through leaves the database on v1 with
    neither new table present rather than half-upgraded.
    """
    existing = _existing_table_names(connection)
    conflicting = sorted(table for table in V2_TABLES if table in existing)
    if conflicting:
        raise DatabaseStateError(
            "Cannot upgrade this database to schema version 2: it is "
            f"marked version {MIN_MIGRATABLE_SCHEMA_VERSION} but already contains "
            f"table(s) {', '.join(conflicting)}. Refusing to migrate over an "
            "inconsistent database."
        )
    for statement in _V2_SCHEMA_STATEMENTS:
        connection.execute(statement)


def _legacy_quantity_text(value: object, field: str) -> str:
    """Convert a v1/v2 integer quantity to the canonical decimal string.

    Both older schemas stored quantities as `INTEGER` and enforced whole
    numbers in Python and in SQL, so every stored value is an exact integer:
    ``1`` becomes ``"1"`` and ``100`` becomes ``"100"``, with no scale invented
    and no value changed. Anything else means the column already holds
    something this module never wrote, and the migration fails closed rather
    than guessing at it - which rolls the whole upgrade back.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise DatabaseStateError(
            f"Cannot migrate {field} {value!r} to an exact decimal quantity: the "
            "pre-v3 schema stored whole numbers, and this value is not one. Refusing "
            "to guess; the database is unchanged."
        )
    return to_decimal_text(Decimal(value), field)


def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
    """Rebuild the quantity columns as exact decimal text, and add the baseline table.

    Crypto quantities are fractional, and `INTEGER` cannot hold 0.0001 of a
    coin. SQLite cannot retype a column in place, so `positions`,
    `order_intents`, and `broker_orders` are rebuilt: renamed aside, recreated
    from the current literals, copied across row by row with quantities
    converted, and the old copies dropped.

    **Every row survives.** The copy is explicit and column-by-column, ids
    included, so foreign keys and `client_order_id` values are preserved
    exactly. Only the quantity columns change representation, and an integer
    quantity converts to the same number written as decimal text.

    Runs inside the caller's transaction with foreign-key enforcement and
    modern rename semantics suspended, and the caller re-checks referential
    integrity before committing.
    """
    existing = _existing_table_names(connection)
    conflicting = sorted(table for table in (*V3_TABLES, *_PRE_V3_TABLES) if table in existing)
    if conflicting:
        raise DatabaseStateError(
            f"Cannot upgrade this database to schema version {SCHEMA_VERSION}: it "
            f"already contains table(s) {', '.join(conflicting)}. Refusing to "
            "migrate over an inconsistent database."
        )

    connection.execute(_RENAME_POSITIONS_PRE_V3)
    connection.execute(_RENAME_ORDER_INTENTS_PRE_V3)
    connection.execute(_RENAME_BROKER_ORDERS_PRE_V3)

    connection.execute(_CREATE_POSITIONS)
    connection.execute(_CREATE_ORDER_INTENTS_V3)
    connection.execute(_CREATE_BROKER_ORDERS)
    connection.execute(_CREATE_DAILY_RISK_BASELINES)

    positions = connection.execute(_SELECT_PRE_V3_POSITIONS).fetchall()
    intents = connection.execute(_SELECT_PRE_V3_ORDER_INTENTS).fetchall()
    orders = connection.execute(_SELECT_PRE_V3_BROKER_ORDERS).fetchall()

    for row in positions:
        connection.execute(
            _INSERT_MIGRATED_POSITION,
            (
                row["symbol"],
                _legacy_quantity_text(row["quantity"], "positions.quantity"),
                row["average_price"],
                row["updated_at"],
            ),
        )
    for row in intents:
        connection.execute(
            _INSERT_MIGRATED_ORDER_INTENT,
            (
                row["id"],
                row["client_order_id"],
                row["strategy_run_id"],
                row["created_at"],
                row["symbol"],
                row["side"],
                _legacy_quantity_text(
                    row["requested_quantity"], "order_intents.requested_quantity"
                ),
                _legacy_quantity_text(row["approved_quantity"], "order_intents.approved_quantity"),
                row["reference_price"],
                row["risk_reason_code"],
                row["status"],
                row["updated_at"],
            ),
        )
    for row in orders:
        connection.execute(
            _INSERT_MIGRATED_BROKER_ORDER,
            (
                row["id"],
                row["order_intent_id"],
                row["broker_order_id"],
                row["client_order_id"],
                row["symbol"],
                row["side"],
                _legacy_quantity_text(row["quantity"], "broker_orders.quantity"),
                _legacy_quantity_text(row["filled_quantity"], "broker_orders.filled_quantity"),
                row["filled_average_price"],
                row["status"],
                row["submitted_at"],
                row["filled_at"],
                row["updated_at"],
                row["created_at"],
            ),
        )

    connection.execute(_DROP_PRE_V3_BROKER_ORDERS)
    connection.execute(_DROP_PRE_V3_ORDER_INTENTS)
    connection.execute(_DROP_PRE_V3_POSITIONS)
    connection.execute(_CREATE_INDEX_ORDER_INTENTS_STATUS)


def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
    """Widen the intent status vocabulary and add the reconciliation audit tables.

    Reconciliation needs to record that the broker *definitively* has no order
    under an intent's `client_order_id`. That is neither `CREATED` nor
    `REJECTED`, so `CONFIRMED_NOT_SUBMITTED` joins the vocabulary - and because
    the vocabulary is a CHECK constraint, SQLite requires a table rebuild to
    change it. `order_intents` is renamed aside, recreated from the same
    literal a fresh database uses, copied across column by column with ids and
    `client_order_id` values preserved exactly, and the old copy dropped.

    **No row changes meaning.** The rebuild only makes one more status
    *storable*; it writes no row into it. A v3 database migrated here and then
    reconciled is in the same state as one that was always v4.

    `reconciliation_runs` and `reconciliation_events` are new and empty: there
    is no history to backfill, and inventing one would be a fabricated audit
    trail.

    Runs inside the caller's transaction, with foreign-key enforcement and
    modern rename semantics suspended; the caller re-checks referential
    integrity before committing.
    """
    existing = _existing_table_names(connection)
    conflicting = sorted(table for table in (*V4_TABLES, *_PRE_V4_TABLES) if table in existing)
    if conflicting:
        raise DatabaseStateError(
            f"Cannot upgrade this database to schema version {SCHEMA_VERSION}: it "
            f"already contains table(s) {', '.join(conflicting)}. Refusing to "
            "migrate over an inconsistent database."
        )

    connection.execute(_RENAME_ORDER_INTENTS_PRE_V4)
    connection.execute(_CREATE_ORDER_INTENTS)

    for row in connection.execute(_SELECT_PRE_V4_ORDER_INTENTS).fetchall():
        connection.execute(
            _INSERT_MIGRATED_ORDER_INTENT,
            (
                row["id"],
                row["client_order_id"],
                row["strategy_run_id"],
                row["created_at"],
                row["symbol"],
                row["side"],
                row["requested_quantity"],
                row["approved_quantity"],
                row["reference_price"],
                row["risk_reason_code"],
                row["status"],
                row["updated_at"],
            ),
        )

    connection.execute(_DROP_PRE_V4_ORDER_INTENTS)
    connection.execute(_CREATE_INDEX_ORDER_INTENTS_STATUS)
    connection.execute(_CREATE_RECONCILIATION_RUNS)
    connection.execute(_CREATE_RECONCILIATION_EVENTS)
    connection.execute(_CREATE_INDEX_RECONCILIATION_EVENTS)


#: Each supported upgrade, in order: the version it produces and how to get
#: there. `initialize_database` runs every step above the database's current
#: version, in one transaction, so v1 -> v4 is v1 -> v2 -> v3 -> v4 and never a
#: separate shortcut path that could drift from any of them.
_MIGRATIONS = (
    (2, _migrate_v1_to_v2),
    (3, _migrate_v2_to_v3),
    (4, _migrate_v3_to_v4),
)


def _require_no_foreign_key_violations(connection: sqlite3.Connection) -> None:
    """Refuse to commit a migration that broke a reference.

    Referential enforcement is suspended while tables are rebuilt, so it is
    re-checked explicitly before the transaction commits. A violation raises,
    which rolls the whole upgrade back.
    """
    violations = connection.execute(_PRAGMA_FOREIGN_KEY_CHECK).fetchall()
    if violations:
        raise DatabaseStateError(
            f"The schema migration left {len(violations)} foreign-key violation(s). "
            "The upgrade was rolled back and the database is unchanged."
        )


def _apply_migrations(connection: sqlite3.Connection, version: int) -> None:
    """Run every upgrade above `version`, atomically.

    The two PRAGMAs are set **before** `BEGIN` because SQLite ignores both
    inside a transaction. They are restored afterwards whatever happens, so a
    failed migration cannot leave a connection with foreign keys silently off.
    The migration itself is one transaction: SQLite's DDL is transactional, so
    a failure anywhere rolls back to the original version with no table half
    rebuilt and no version marker moved.
    """
    connection.execute(_PRAGMA_FOREIGN_KEYS_OFF)
    connection.execute(_PRAGMA_LEGACY_ALTER_TABLE_ON)
    try:
        with transaction(connection):
            for target, migrate in _MIGRATIONS:
                if version < target:
                    migrate(connection)
            connection.execute(_UPDATE_SCHEMA_VERSION, (SCHEMA_VERSION,))
            _require_no_foreign_key_violations(connection)
    finally:
        connection.execute(_PRAGMA_LEGACY_ALTER_TABLE_OFF)
        connection.execute(_PRAGMA_FOREIGN_KEYS)


def _verify_or_migrate_schema(connection: sqlite3.Connection) -> None:
    """Bring an already-initialized database to the supported version, or refuse it.

    A **newer** database is refused and left untouched: downgrading would
    discard data this code cannot understand. A database at or above
    `MIN_MIGRATABLE_SCHEMA_VERSION` is upgraded through the explicit ordered
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
        _apply_migrations(connection, version)
    _require_expected_tables(connection, SCHEMA_VERSION)


def _create_schema(connection: sqlite3.Connection, existing: set[str]) -> None:
    """Create the current schema in a database that has none of it yet.

    A new database is built directly at `SCHEMA_VERSION`; it is never created
    at an older version and then migrated.
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
    existing older database is upgraded through the explicit ordered migration
    path, in a single transaction, so a failed upgrade rolls back and leaves
    the database on its original version rather than half-migrated. Every row
    survives an upgrade: v3 rebuilds three tables to widen their quantity
    columns and copies every row across unchanged apart from that
    representation, and v4 rebuilds `order_intents` to widen a CHECK constraint
    and copies every row across entirely unchanged.

    Idempotent: once at the current version, repeated calls verify and change
    nothing. A database written by a **newer** schema version is refused and
    left untouched, and an inconsistent one raises rather than being repaired.

    Returns the database path. Missing parent directories are created.
    """
    database_path = Path(path)
    if str(database_path) != ":memory:":
        database_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(database_path) as connection:
        existing = _existing_table_names(connection)
        if "schema_metadata" in existing:
            # Not wrapped in one outer transaction: a migration has to toggle
            # PRAGMAs that SQLite ignores inside one, so it opens its own.
            _verify_or_migrate_schema(connection)
        else:
            with transaction(connection):
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
    6E) is the execution layer's problem and is not attempted here.
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
    strings whose vocabulary belongs to the risk engine (C5), which is
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
        quantity=from_decimal_text(row["quantity"], "positions.quantity"),
        average_price=None if average_price is None else float(average_price),
        updated_at=from_utc_text(row["updated_at"]),
    )


def upsert_position(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    quantity: Decimal | int,
    updated_at: datetime,
    average_price: float | None = None,
) -> None:
    """Store the latest local position snapshot for `symbol`.

    One row per symbol: an existing snapshot is replaced, not appended to.
    This is **local** state. Nothing here populates it from a broker, and
    reconciling this table against a broker's authoritative positions is
    Phase 8's job (docs/SPEC.md section 6E).

    `quantity` is an exact `Decimal` and must be non-negative - the system is
    long only - and is stored as canonical decimal text so a fractional coin
    round-trips exactly. `average_price` is either NULL, which is the natural
    value for a flat position, or a finite number greater than zero. Both rules
    are also CHECK constraints in the schema, so a write that bypassed this
    function still cannot store a short position. No P&L is computed or stored.
    """
    ticker = _require_symbol(symbol)
    amount = to_decimal_text(_require_quantity(quantity), "quantity")
    price = _require_average_price(average_price)
    updated_text = to_utc_text(updated_at, "updated_at")
    with transaction(connection):
        connection.execute(_UPSERT_POSITION, (ticker, amount, price, updated_text))


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
        requested_quantity=from_decimal_text(
            row["requested_quantity"], "order_intents.requested_quantity"
        ),
        approved_quantity=from_decimal_text(
            row["approved_quantity"], "order_intents.approved_quantity"
        ),
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
    requested_quantity: Decimal | int,
    approved_quantity: Decimal | int,
    reference_price: float,
    risk_reason_code: str,
    strategy_run_id: int | None = None,
    status: str = INTENT_STATUS_CREATED,
) -> int:
    """Store one order intent and return its row id.

    Quantities are exact `Decimal` values, stored as canonical decimal text:
    a fractional crypto order size round-trips unchanged.

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
    requested_text = to_decimal_text(requested, "requested_quantity")
    approved_text = to_decimal_text(approved, "approved_quantity")
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
                    requested_text,
                    approved_text,
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
# intent: execution submits at most one order per intent, and re-reading that
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
        quantity=from_decimal_text(row["quantity"], "broker_orders.quantity"),
        filled_quantity=from_decimal_text(row["filled_quantity"], "broker_orders.filled_quantity"),
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
    quantity: Decimal | int,
    status: str,
    updated_at: datetime,
    filled_quantity: Decimal | int = 0,
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
    `DuplicateBrokerOrderError`, because execution submits exactly one order per
    intent and a second one would be a contradiction, not an update.
    """
    broker_id = _require_text(broker_order_id, "broker_order_id")
    client_id = _require_text(client_order_id, "client_order_id")
    ticker = _require_symbol(symbol)
    order_side = _require_choice(side, "side", ORDER_SIDES)
    amount = to_decimal_text(_require_positive_quantity(quantity, "quantity"), "quantity")
    filled = to_decimal_text(
        _require_quantity(filled_quantity, "filled_quantity"), "filled_quantity"
    )
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
                    amount,
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
                    f"different broker order than {broker_id!r}. Exactly one broker "
                    "order per intent is allowed."
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


# --------------------------------------------------------------------------
# Daily risk baselines (schema v3)
#
# Crypto trades 24/7, so a "trading day" is a UTC calendar day and there is no
# equity-market previous close to measure a day's P&L against. The first
# account equity observed on a UTC date is recorded here and reused for the
# rest of that date, which is what makes the daily-loss halt reproducible
# across process restarts.
#
# This is a persistence primitive only. Nothing here schedules anything,
# watches a clock, or decides when an observation should happen.
# --------------------------------------------------------------------------

_INSERT_DAILY_RISK_BASELINE = """
INSERT INTO daily_risk_baselines (risk_date_utc, baseline_equity, captured_at)
VALUES (?, ?, ?)
ON CONFLICT (risk_date_utc) DO NOTHING
"""
_SELECT_DAILY_RISK_BASELINE = """
SELECT risk_date_utc, baseline_equity, captured_at
FROM daily_risk_baselines
WHERE risk_date_utc = ?
"""
_SELECT_DAILY_RISK_BASELINES = """
SELECT risk_date_utc, baseline_equity, captured_at
FROM daily_risk_baselines
ORDER BY risk_date_utc
"""


def _to_daily_risk_baseline(row: sqlite3.Row) -> DailyRiskBaseline:
    return DailyRiskBaseline(
        risk_date_utc=from_risk_date_text(row["risk_date_utc"]),
        baseline_equity=from_decimal_text(
            row["baseline_equity"], "daily_risk_baselines.baseline_equity"
        ),
        captured_at=from_utc_text(row["captured_at"]),
    )


def ensure_daily_risk_baseline(
    connection: sqlite3.Connection,
    *,
    risk_date_utc: date,
    baseline_equity: Decimal | int,
    captured_at: datetime,
) -> DailyRiskBaseline:
    """Return the baseline for `risk_date_utc`, establishing it if there is none.

    **First observation wins, permanently.** If a row already exists for that
    UTC date it is returned untouched and the supplied equity is discarded; a
    baseline that drifted during the day would silently reset the daily-loss
    halt, which is the one thing it exists to prevent.

    Exactly one row per UTC date, enforced by the primary key as well as here.
    The insert and the read run in one transaction, so two callers racing on a
    fresh date still agree on the same baseline.

    Honest limitation: this records the first equity this system *observed*,
    not the equity at exactly 00:00 UTC. Nothing in this milestone runs
    continuously, so if the first observation of a day happens hours in, the
    baseline is that later figure. A 24/7 runner (Phase 9) is what will make
    the first observation land near the boundary; until then, the recorded
    `captured_at` is what says how close it actually was.
    """
    date_text = to_risk_date_text(risk_date_utc)
    equity_text = to_decimal_text(_require_positive_quantity(baseline_equity, "baseline_equity"))
    captured_text = to_utc_text(captured_at, "captured_at")
    with transaction(connection):
        connection.execute(_INSERT_DAILY_RISK_BASELINE, (date_text, equity_text, captured_text))
        row = connection.execute(_SELECT_DAILY_RISK_BASELINE, (date_text,)).fetchone()
    if row is None:  # pragma: no cover - the insert above guarantees a row
        raise DatabaseStateError(f"No daily risk baseline could be established for {date_text}.")
    return _to_daily_risk_baseline(row)


def get_daily_risk_baseline(
    connection: sqlite3.Connection, risk_date_utc: date
) -> DailyRiskBaseline | None:
    """Return the stored baseline for one UTC date, or None when there is none."""
    row = connection.execute(
        _SELECT_DAILY_RISK_BASELINE, (to_risk_date_text(risk_date_utc),)
    ).fetchone()
    return None if row is None else _to_daily_risk_baseline(row)


def list_daily_risk_baselines(connection: sqlite3.Connection) -> list[DailyRiskBaseline]:
    """Every stored baseline, oldest UTC date first."""
    return [
        _to_daily_risk_baseline(row) for row in connection.execute(_SELECT_DAILY_RISK_BASELINES)
    ]


# --------------------------------------------------------------------------
# Reconciliation audit (schema v4)
#
# The durable evidence one reconciliation pass leaves behind. This is
# persistence only: nothing here contacts a broker, decides what "reconciled"
# means, or computes `safe_to_trade` - `autotrader.reconciliation` owns all of
# that and hands the conclusion here to be written down.
#
# A run row is written once, when a pass has finished, together with its
# events in a single transaction. There is deliberately no in-progress row: a
# half-written run that a crash left behind would be indistinguishable from a
# genuine one, and "reconciliation ran" is precisely the claim a runtime is
# about to trust.
# --------------------------------------------------------------------------

_INSERT_RECONCILIATION_RUN = """
INSERT INTO reconciliation_runs
    (started_at, completed_at, status, safe_to_trade, orders_checked, positions_checked,
     issues_count, unresolved_count, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_SELECT_RECONCILIATION_RUN = """
SELECT id, started_at, completed_at, status, safe_to_trade, orders_checked,
       positions_checked, issues_count, unresolved_count, created_at
FROM reconciliation_runs
WHERE id = ?
"""
_SELECT_RECONCILIATION_RUNS = """
SELECT id, started_at, completed_at, status, safe_to_trade, orders_checked,
       positions_checked, issues_count, unresolved_count, created_at
FROM reconciliation_runs
ORDER BY id
"""
_SELECT_LATEST_RECONCILIATION_RUN = """
SELECT id, started_at, completed_at, status, safe_to_trade, orders_checked,
       positions_checked, issues_count, unresolved_count, created_at
FROM reconciliation_runs
ORDER BY id DESC
LIMIT 1
"""
_INSERT_RECONCILIATION_EVENT = """
INSERT INTO reconciliation_events
    (reconciliation_run_id, event_timestamp, category, outcome, symbol, client_order_id,
     detail, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""
_SELECT_RECONCILIATION_EVENTS = """
SELECT id, reconciliation_run_id, event_timestamp, category, outcome, symbol,
       client_order_id, detail, created_at
FROM reconciliation_events
ORDER BY id
"""
_SELECT_RECONCILIATION_EVENTS_FOR_RUN = """
SELECT id, reconciliation_run_id, event_timestamp, category, outcome, symbol,
       client_order_id, detail, created_at
FROM reconciliation_events
WHERE reconciliation_run_id = ?
ORDER BY id
"""


def _require_count(value: int, field: str) -> int:
    """A non-negative row count. `bool` is refused as a type confusion."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise StateInputError(f"{field} must be an integer, got {type(value).__name__}.")
    if value < 0:
        raise StateInputError(f"{field} must not be negative, got {value}.")
    return value


def _to_reconciliation_run(row: sqlite3.Row) -> ReconciliationRun:
    return ReconciliationRun(
        id=int(row["id"]),
        started_at=from_utc_text(row["started_at"]),
        completed_at=from_utc_text(row["completed_at"]),
        status=str(row["status"]),
        safe_to_trade=bool(row["safe_to_trade"]),
        orders_checked=int(row["orders_checked"]),
        positions_checked=int(row["positions_checked"]),
        issues_count=int(row["issues_count"]),
        unresolved_count=int(row["unresolved_count"]),
        created_at=from_utc_text(row["created_at"]),
    )


def _to_reconciliation_event(row: sqlite3.Row) -> ReconciliationEvent:
    symbol = row["symbol"]
    client_order_id = row["client_order_id"]
    return ReconciliationEvent(
        id=int(row["id"]),
        reconciliation_run_id=int(row["reconciliation_run_id"]),
        event_timestamp=from_utc_text(row["event_timestamp"]),
        category=str(row["category"]),
        outcome=str(row["outcome"]),
        symbol=None if symbol is None else str(symbol),
        client_order_id=None if client_order_id is None else str(client_order_id),
        detail=str(row["detail"]),
        created_at=from_utc_text(row["created_at"]),
    )


def record_reconciliation_run(
    connection: sqlite3.Connection,
    *,
    started_at: datetime,
    completed_at: datetime,
    status: str,
    safe_to_trade: bool,
    orders_checked: int,
    positions_checked: int,
    issues_count: int,
    unresolved_count: int,
) -> int:
    """Store one finished reconciliation run and return its row id.

    `safe_to_trade` is persisted as the caller computed it rather than derived
    from `status` here. The two must agree - and a test asserts they do - but
    an audit record that silently recomputed the answer would no longer be
    evidence of what the runtime was actually told.

    `completed_at` may not precede `started_at`: a run that finished before it
    began is a corrupt record, not a fast one.
    """
    run_status = _require_choice(status, "status", RECONCILIATION_STATUSES)
    if not isinstance(safe_to_trade, bool):
        raise StateInputError(f"safe_to_trade must be a bool, got {type(safe_to_trade).__name__}.")
    started_text = to_utc_text(started_at, "started_at")
    completed_text = to_utc_text(completed_at, "completed_at")
    if completed_text < started_text:
        raise StateInputError(
            f"completed_at must not precede started_at ({completed_text} is before {started_text})."
        )
    orders = _require_count(orders_checked, "orders_checked")
    positions = _require_count(positions_checked, "positions_checked")
    issues = _require_count(issues_count, "issues_count")
    unresolved = _require_count(unresolved_count, "unresolved_count")
    if unresolved > issues:
        raise StateInputError(
            f"unresolved_count ({unresolved}) cannot exceed issues_count ({issues}); "
            "every unresolved item is an issue."
        )

    with transaction(connection):
        cursor = connection.execute(
            _INSERT_RECONCILIATION_RUN,
            (
                started_text,
                completed_text,
                run_status,
                int(safe_to_trade),
                orders,
                positions,
                issues,
                unresolved,
                _now_text(),
            ),
        )
    return int(cursor.lastrowid)


def record_reconciliation_event(
    connection: sqlite3.Connection,
    *,
    reconciliation_run_id: int,
    event_timestamp: datetime,
    category: str,
    outcome: str,
    detail: str,
    symbol: str | None = None,
    client_order_id: str | None = None,
) -> int:
    """Store one piece of reconciliation evidence and return its row id.

    `detail` is free text written by the reconciler and must not be empty: an
    event that cannot say what it observed is not evidence. It must never
    contain a credential - the reconciler composes it from symbols, quantities,
    statuses, and `client_order_id` values only.
    """
    event_category = _require_choice(category, "category", RECONCILIATION_CATEGORIES)
    event_outcome = _require_choice(outcome, "outcome", RECONCILIATION_OUTCOMES)
    detail_text = _require_text(detail, "detail")
    ticker = None if symbol is None else _require_symbol(symbol)
    client_id = _optional_text(client_order_id, "client_order_id")
    timestamp_text = to_utc_text(event_timestamp, "event_timestamp")

    try:
        with transaction(connection):
            cursor = connection.execute(
                _INSERT_RECONCILIATION_EVENT,
                (
                    reconciliation_run_id,
                    timestamp_text,
                    event_category,
                    event_outcome,
                    ticker,
                    client_id,
                    detail_text,
                    _now_text(),
                ),
            )
    except sqlite3.IntegrityError as error:
        if error.sqlite_errorname == "SQLITE_CONSTRAINT_FOREIGNKEY":
            raise UnknownReconciliationRunError(
                f"No reconciliation run with id {reconciliation_run_id!r}."
            ) from None
        raise
    return int(cursor.lastrowid)


def get_reconciliation_run(
    connection: sqlite3.Connection, reconciliation_run_id: int
) -> ReconciliationRun | None:
    """Return one reconciliation run by row id, or None."""
    row = connection.execute(_SELECT_RECONCILIATION_RUN, (reconciliation_run_id,)).fetchone()
    return None if row is None else _to_reconciliation_run(row)


def latest_reconciliation_run(connection: sqlite3.Connection) -> ReconciliationRun | None:
    """Return the most recent finished reconciliation run, or None.

    The lookup a runtime makes on startup to ask what the last pass concluded.
    None means no pass has ever finished, which is **not** permission to trade.
    """
    row = connection.execute(_SELECT_LATEST_RECONCILIATION_RUN).fetchone()
    return None if row is None else _to_reconciliation_run(row)


def list_reconciliation_runs(connection: sqlite3.Connection) -> list[ReconciliationRun]:
    """Every stored reconciliation run, oldest first."""
    return [_to_reconciliation_run(row) for row in connection.execute(_SELECT_RECONCILIATION_RUNS)]


def list_reconciliation_events(
    connection: sqlite3.Connection, reconciliation_run_id: int | None = None
) -> list[ReconciliationEvent]:
    """Reconciliation evidence, oldest first, optionally for one run only."""
    if reconciliation_run_id is None:
        rows = connection.execute(_SELECT_RECONCILIATION_EVENTS)
    else:
        rows = connection.execute(_SELECT_RECONCILIATION_EVENTS_FOR_RUN, (reconciliation_run_id,))
    return [_to_reconciliation_event(row) for row in rows]


__all__ = [
    "BUSY_TIMEOUT_MS",
    "DEFAULT_DATABASE_PATH",
    "INTENT_STATUS_CONFIRMED_NOT_SUBMITTED",
    "INTENT_STATUS_CREATED",
    "INTENT_STATUS_REJECTED",
    "INTENT_STATUS_SUBMITTED",
    "INTENT_STATUS_SUBMITTING",
    "INTENT_STATUS_UNKNOWN",
    "MIN_MIGRATABLE_SCHEMA_VERSION",
    "ORDER_INTENT_STATUSES",
    "ORDER_SIDES",
    "RECONCILIATION_CATEGORIES",
    "RECONCILIATION_OUTCOMES",
    "RECONCILIATION_STATUSES",
    "RECONCILIATION_STATUS_CLEAN",
    "RECONCILIATION_STATUS_FAILED",
    "RECONCILIATION_STATUS_REPAIRED",
    "RECONCILIATION_STATUS_UNRESOLVED",
    "REQUIRED_TABLES",
    "RUN_MODES",
    "RUN_STATUSES",
    "RUN_STATUS_COMPLETED",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_RUNNING",
    "SCHEMA_VERSION",
    "SIGNAL_TYPES",
    "TERMINAL_INTENT_STATUSES",
    "TERMINAL_RUN_STATUSES",
    "TIMESTAMP_FORMAT",
    "V2_TABLES",
    "V3_TABLES",
    "V4_TABLES",
    "DailyRiskBaseline",
    "DatabaseStateError",
    "DuplicateBrokerOrderError",
    "DuplicateOrderIntentError",
    "DuplicateSignalError",
    "Position",
    "ReconciliationEvent",
    "ReconciliationRun",
    "RiskEvent",
    "StateError",
    "StateInputError",
    "StoredBrokerOrder",
    "StoredOrderIntent",
    "StoredSignal",
    "StrategyRun",
    "SystemEvent",
    "UnknownOrderIntentError",
    "UnknownReconciliationRunError",
    "UnknownStrategyRunError",
    "UnsupportedSchemaVersionError",
    "connect",
    "ensure_daily_risk_baseline",
    "finish_strategy_run",
    "from_decimal_text",
    "from_risk_date_text",
    "from_utc_text",
    "get_broker_order_by_client_id",
    "get_broker_order_by_intent",
    "get_daily_risk_baseline",
    "get_order_intent",
    "get_order_intent_by_client_id",
    "get_position",
    "get_reconciliation_run",
    "get_schema_version",
    "get_strategy_run",
    "initialize_database",
    "latest_reconciliation_run",
    "list_broker_orders",
    "list_daily_risk_baselines",
    "list_order_intents",
    "list_positions",
    "list_reconciliation_events",
    "list_reconciliation_runs",
    "list_risk_events",
    "list_signals",
    "list_strategy_runs",
    "list_system_events",
    "record_order_intent",
    "record_reconciliation_event",
    "record_reconciliation_run",
    "record_risk_event",
    "record_signal",
    "record_strategy_run",
    "record_system_event",
    "to_decimal_text",
    "to_risk_date_text",
    "to_utc_text",
    "transaction",
    "update_order_intent_status",
    "upsert_broker_order",
    "upsert_position",
    "utc_risk_date",
]
