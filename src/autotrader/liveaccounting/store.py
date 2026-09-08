"""Persistence for the real-money accounting ledger. Its own database, again.

The realized-P&L ledger got its own file for a reason that applies here word
for word: every command in the trading lineage runs `initialize_database()`
first and migrates the operational store upward automatically, so putting
accounting tables in the shared schema would migrate a store out from under a
running trader. This ledger repeats that decision rather than borrowing the
other one's file, because the two describe **different accounts** - one paper,
one real - and a single file holding both would be one careless join away from
reporting paper profit as real money.

`connect_read_only` and the rollback journal are inherited for the same reason
they exist there: the dashboard reader is a least-privilege process that must
be able to open this file when no writer is running.

**Authoritative and derived are separated on purpose.**

    authoritative   external_cash_flows, performance_checkpoints
    derived         profit_reserve_events, live_accounting_derived

Everything in the second group is a pure function of the first group plus the
policy, computed by `engine`. Nothing reads it back in order to advance itself.
That is what makes a rebuild total: drop the derived tables, replay, and the
answer is identical - which the suite proves rather than asserts.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from autotrader.liveaccounting.models import (
    CHECKPOINT_KINDS,
    CLASSIFICATIONS,
    CONFIRMATIONS,
    ENVIRONMENT_LIVE,
    RELATIONS,
    EquityCheckpoint,
    ExternalFlow,
    LiveAccountingError,
    LiveAccountingInputError,
)

#: This store's own schema version. Nothing to do with `state.SCHEMA_VERSION`
#: (the operational stores) or `ACCOUNTING_SCHEMA_VERSION` (the realized-P&L
#: ledger). Three counters for three files, which is the point of three files.
LIVE_ACCOUNTING_SCHEMA_VERSION = 1

BUSY_TIMEOUT_MS = 5000

#: Not WAL - see the module docstring and the realized-P&L ledger's.
JOURNAL_MODE = "DELETE"


class LiveAccountingStoreError(LiveAccountingError):
    """The live accounting database could not be used as one."""


class UnsupportedLiveAccountingSchemaError(LiveAccountingStoreError):
    """The file on disk was written by a newer build."""


class AccountScopeError(LiveAccountingStoreError):
    """This ledger belongs to a different broker account.

    Fail closed and never merge. Two accounts' balances added together is a
    number that describes nothing.
    """


def decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal):
        raise LiveAccountingInputError(f"expected Decimal, got {type(value).__name__}")
    return format(value, "f")


def text_decimal(value: object) -> Decimal:
    if value is None:
        raise LiveAccountingInputError("expected a stored decimal, found NULL")
    return Decimal(str(value))


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else decimal_text(value)


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _parse(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


def _optional_parse(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

_CREATE_METADATA = f"""
    CREATE TABLE live_accounting_metadata (
        id                         INTEGER PRIMARY KEY CHECK (id = 1),
        schema_version             INTEGER NOT NULL,
        environment                TEXT    NOT NULL CHECK (environment = '{ENVIRONMENT_LIVE}'),
        broker_account_fingerprint TEXT    NOT NULL CHECK (broker_account_fingerprint <> ''),
        source_sha                 TEXT,
        created_at                 TEXT    NOT NULL,
        updated_at                 TEXT    NOT NULL
    )
    """

#: The baseline, frozen once. `inception_at` and `baseline_equity` are the two
#: numbers every "since inception" figure is measured against, and changing
#: either is a rebuild, not an update.
_CREATE_STATE = """
    CREATE TABLE live_accounting_state (
        id                     INTEGER PRIMARY KEY CHECK (id = 1),
        inception_at           TEXT    NOT NULL CHECK (inception_at <> ''),
        baseline_equity        TEXT    NOT NULL CHECK (baseline_equity <> ''),
        baseline_cash          TEXT    NOT NULL CHECK (baseline_cash <> ''),
        inception_method       TEXT    NOT NULL CHECK (
            inception_method IN ('PRE_TRADE_BASELINE', 'RECONSTRUCTED')
        ),
        inception_note         TEXT,
        reserve_policy_id      TEXT    NOT NULL CHECK (reserve_policy_id <> ''),
        reserve_policy_hash    TEXT    NOT NULL CHECK (reserve_policy_hash <> ''),
        reserve_rate           TEXT    NOT NULL CHECK (reserve_rate <> ''),
        withdrawal_mode        TEXT    NOT NULL CHECK (withdrawal_mode = 'OBSERVE_ONLY'),
        rebuild_required       INTEGER NOT NULL DEFAULT 0 CHECK (rebuild_required IN (0, 1)),
        rebuild_count          INTEGER NOT NULL DEFAULT 0 CHECK (rebuild_count >= 0),
        last_rebuild_at        TEXT,
        last_rebuild_reason    TEXT,
        created_at             TEXT    NOT NULL,
        updated_at             TEXT    NOT NULL
    )
    """

#: AUTHORITATIVE. Append-only; `broker_activity_id` is UNIQUE, which is what
#: makes import idempotent however many times a window is re-read.
#:
#: `source_digest` is a SHA-256 of the raw activity payload rather than the
#: payload itself. It proves at audit time that a stored row still describes
#: the activity it was imported from, without copying a transfer reference and
#: a bank description into a file the dashboard user can read.
_CREATE_FLOWS = f"""
    CREATE TABLE external_cash_flows (
        flow_id             INTEGER PRIMARY KEY,
        broker_activity_id  TEXT NOT NULL UNIQUE CHECK (broker_activity_id <> ''),
        account_fingerprint TEXT NOT NULL CHECK (account_fingerprint <> ''),
        activity_type       TEXT NOT NULL CHECK (activity_type <> ''),
        broker_status       TEXT NOT NULL CHECK (broker_status <> ''),
        classification      TEXT NOT NULL CHECK (
            classification IN ({", ".join(repr(value) for value in CLASSIFICATIONS)})
        ),
        classification_reason TEXT,
        confirmation        TEXT NOT NULL CHECK (
            confirmation IN ({", ".join(repr(value) for value in CONFIRMATIONS)})
        ),
        amount              TEXT NOT NULL CHECK (amount <> ''),
        currency            TEXT NOT NULL CHECK (currency <> ''),
        settle_at           TEXT NOT NULL CHECK (settle_at <> ''),
        relation            TEXT NOT NULL CHECK (
            relation IN ({", ".join(repr(value) for value in RELATIONS)})
        ),
        source_digest       TEXT NOT NULL CHECK (source_digest <> ''),
        imported_at         TEXT NOT NULL,
        backdated           INTEGER NOT NULL DEFAULT 0 CHECK (backdated IN (0, 1))
    )
    """

#: AUTHORITATIVE. Raw broker truth at an instant. Nothing derived is stored
#: here: `flow_adjusted_equity` is deliberately absent, because it is a
#: function of this row and the flow ledger and storing it would create a
#: second answer that could disagree with the first.
_CREATE_CHECKPOINTS = f"""
    CREATE TABLE performance_checkpoints (
        checkpoint_id       INTEGER PRIMARY KEY,
        account_fingerprint TEXT NOT NULL CHECK (account_fingerprint <> ''),
        taken_at            TEXT NOT NULL CHECK (taken_at <> ''),
        utc_date            TEXT NOT NULL CHECK (utc_date <> ''),
        kind                TEXT NOT NULL CHECK (
            kind IN ({", ".join(repr(value) for value in CHECKPOINT_KINDS)})
        ),
        broker_equity       TEXT NOT NULL CHECK (broker_equity <> ''),
        broker_cash         TEXT NOT NULL CHECK (broker_cash <> ''),
        unrealized_pnl      TEXT,
        position_count      INTEGER,
        created_at          TEXT NOT NULL
    )
    """

#: DERIVED. Rewritten wholesale by a rebuild. Kept because an operator asking
#: "why is the bucket $1?" deserves a row that says which checkpoint accrued it.
_CREATE_RESERVE_EVENTS = """
    CREATE TABLE profit_reserve_events (
        event_id              INTEGER PRIMARY KEY,
        event_type            TEXT NOT NULL CHECK (
            event_type IN ('ACCRUAL', 'WITHDRAWAL_ALLOCATION')
        ),
        occurred_at           TEXT NOT NULL CHECK (occurred_at <> ''),
        prior_reserve_hwm     TEXT NOT NULL,
        new_reserve_hwm       TEXT NOT NULL,
        new_hwm_profit        TEXT NOT NULL,
        reserve_rate          TEXT NOT NULL,
        accrual_amount        TEXT NOT NULL,
        withdrawal_amount     TEXT NOT NULL,
        allocated_from_bucket TEXT NOT NULL,
        unreserved_amount     TEXT NOT NULL,
        bucket_before         TEXT NOT NULL,
        bucket_after          TEXT NOT NULL CHECK (CAST(bucket_after AS REAL) >= 0),
        created_at            TEXT NOT NULL
    )
    """

_INDEXES: tuple[str, ...] = (
    "CREATE INDEX idx_flows_settle ON external_cash_flows (settle_at)",
    "CREATE INDEX idx_flows_class ON external_cash_flows (classification, confirmation)",
    "CREATE INDEX idx_checkpoints_taken ON performance_checkpoints (taken_at)",
    "CREATE INDEX idx_checkpoints_kind ON performance_checkpoints (kind, utc_date)",
    # One authoritative completed-day checkpoint per UTC day. A second read of
    # the same closed day must replace, never accumulate, or the reserve would
    # accrue twice off one day's profit.
    "CREATE UNIQUE INDEX idx_checkpoints_daily ON performance_checkpoints (utc_date)"
    " WHERE kind = 'DAILY_CLOSE'",
    "CREATE INDEX idx_reserve_events_at ON profit_reserve_events (occurred_at)",
)

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    _CREATE_METADATA,
    _CREATE_STATE,
    _CREATE_FLOWS,
    _CREATE_CHECKPOINTS,
    _CREATE_RESERVE_EVENTS,
    *_INDEXES,
)

#: Tables a rebuild is allowed to erase, because every row in them is derived.
DERIVED_TABLES: tuple[str, ...] = ("profit_reserve_events",)

#: Tables a rebuild must never touch.
AUTHORITATIVE_TABLES: tuple[str, ...] = ("external_cash_flows", "performance_checkpoints")


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------


@contextmanager
def connect(path: str | Path) -> Iterator[sqlite3.Connection]:
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
    """`mode=ro` and `query_only`. The dashboard's connection, and it can create nothing."""
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
    """Create the schema when absent, verify it when present. Never downgrades."""
    if "live_accounting_metadata" not in _table_names(connection):
        with transaction(connection):
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
        return LIVE_ACCOUNTING_SCHEMA_VERSION

    row = connection.execute(
        "SELECT schema_version FROM live_accounting_metadata WHERE id = 1"
    ).fetchone()
    if row is None:
        return LIVE_ACCOUNTING_SCHEMA_VERSION
    version = int(row["schema_version"])
    if version > LIVE_ACCOUNTING_SCHEMA_VERSION:
        raise UnsupportedLiveAccountingSchemaError(
            f"This ledger is at schema {version} and this build understands "
            f"{LIVE_ACCOUNTING_SCHEMA_VERSION}. Refusing to open it rather than guessing "
            "what a newer build meant."
        )
    return version


def stamp_metadata(
    connection: sqlite3.Connection,
    *,
    account_fingerprint: str,
    now: datetime,
    source_sha: str | None = None,
) -> None:
    """Bind this file to one broker account, once."""
    if not account_fingerprint:
        raise AccountScopeError("A live accounting ledger cannot be opened without an account pin.")
    existing = connection.execute(
        "SELECT broker_account_fingerprint FROM live_accounting_metadata WHERE id = 1"
    ).fetchone()
    if existing is not None:
        if str(existing["broker_account_fingerprint"]) != account_fingerprint:
            raise AccountScopeError(
                f"This ledger belongs to account {existing['broker_account_fingerprint']} and "
                f"the caller is operating {account_fingerprint}. Refusing to write. Balances "
                "from two accounts are never combined."
            )
        return
    with transaction(connection):
        connection.execute(
            "INSERT INTO live_accounting_metadata (id, schema_version, environment, "
            "broker_account_fingerprint, source_sha, created_at, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?, ?)",
            (
                LIVE_ACCOUNTING_SCHEMA_VERSION,
                ENVIRONMENT_LIVE,
                account_fingerprint,
                source_sha,
                _iso(now),
                _iso(now),
            ),
        )


def require_account(connection: sqlite3.Connection, account_fingerprint: str) -> str:
    """The ledger's own account pin, checked against the caller's. Fail closed."""
    row = connection.execute(
        "SELECT broker_account_fingerprint FROM live_accounting_metadata WHERE id = 1"
    ).fetchone()
    if row is None:
        raise AccountScopeError("This ledger has never been bound to an account.")
    stored = str(row["broker_account_fingerprint"])
    if stored != account_fingerprint:
        raise AccountScopeError(
            f"This ledger belongs to account {stored}, not {account_fingerprint}."
        )
    return stored


def stored_account(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT broker_account_fingerprint FROM live_accounting_metadata WHERE id = 1"
    ).fetchone()
    return None if row is None else str(row["broker_account_fingerprint"])


# --------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------

INCEPTION_PRE_TRADE = "PRE_TRADE_BASELINE"
INCEPTION_RECONSTRUCTED = "RECONSTRUCTED"


def read_state(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM live_accounting_state WHERE id = 1").fetchone()


def freeze_inception(
    connection: sqlite3.Connection,
    *,
    inception_at: datetime,
    baseline_equity: Decimal,
    baseline_cash: Decimal,
    method: str,
    policy_id: str,
    policy_hash: str,
    reserve_rate: Decimal,
    withdrawal_mode: str,
    now: datetime,
    note: str | None = None,
) -> None:
    """Write the baseline. Refuses to overwrite an existing one.

    The baseline is the origin of every figure this system publishes. Silently
    replacing it would silently rewrite the account's entire history, so the
    only way past this refusal is a deliberate rebuild that says so.
    """
    if read_state(connection) is not None:
        raise LiveAccountingStoreError(
            "Accounting inception is already frozen for this ledger. Re-freezing would "
            "rewrite every 'since inception' figure without saying so. Nothing was changed."
        )
    if method not in (INCEPTION_PRE_TRADE, INCEPTION_RECONSTRUCTED):
        raise LiveAccountingInputError(f"unknown inception method {method!r}")
    with transaction(connection):
        connection.execute(
            "INSERT INTO live_accounting_state (id, inception_at, baseline_equity, "
            "baseline_cash, inception_method, inception_note, reserve_policy_id, "
            "reserve_policy_hash, reserve_rate, withdrawal_mode, rebuild_required, "
            "rebuild_count, created_at, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)",
            (
                _iso(inception_at),
                decimal_text(baseline_equity),
                decimal_text(baseline_cash),
                method,
                note,
                policy_id,
                policy_hash,
                decimal_text(reserve_rate),
                withdrawal_mode,
                _iso(now),
                _iso(now),
            ),
        )


# --------------------------------------------------------------------------
# Flows
# --------------------------------------------------------------------------


def record_flow(
    connection: sqlite3.Connection,
    *,
    broker_activity_id: str,
    account_fingerprint: str,
    activity_type: str,
    broker_status: str,
    classification: str,
    classification_reason: str | None,
    confirmation: str,
    amount: Decimal,
    currency: str,
    settle_at: datetime,
    relation: str,
    source_digest: str,
    now: datetime,
    backdated: bool = False,
) -> bool:
    """Insert one flow. Returns False when the activity was already stored.

    Idempotency is the UNIQUE constraint on `broker_activity_id`, not a
    membership test in Python: two synchronizers racing on the same window both
    try to insert and exactly one succeeds, which a read-then-write could not
    promise.
    """
    require_account(connection, account_fingerprint)
    try:
        with transaction(connection):
            connection.execute(
                "INSERT INTO external_cash_flows (broker_activity_id, account_fingerprint, "
                "activity_type, broker_status, classification, classification_reason, "
                "confirmation, amount, currency, settle_at, relation, source_digest, "
                "imported_at, backdated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    broker_activity_id,
                    account_fingerprint,
                    activity_type,
                    broker_status,
                    classification,
                    classification_reason,
                    confirmation,
                    decimal_text(amount),
                    currency,
                    _iso(settle_at),
                    relation,
                    source_digest,
                    _iso(now),
                    1 if backdated else 0,
                ),
            )
    except sqlite3.IntegrityError:
        return False
    return True


def read_flows(connection: sqlite3.Connection) -> tuple[ExternalFlow, ...]:
    rows = connection.execute("SELECT * FROM external_cash_flows ORDER BY settle_at, flow_id")
    return tuple(
        ExternalFlow(
            broker_activity_id=str(row["broker_activity_id"]),
            activity_type=str(row["activity_type"]),
            classification=str(row["classification"]),
            confirmation=str(row["confirmation"]),
            amount=text_decimal(row["amount"]),
            settle_at=_parse(row["settle_at"]),
            currency=str(row["currency"]),
            relation=str(row["relation"]),
        )
        for row in rows
    )


def unknown_flow_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT * FROM external_cash_flows WHERE classification = 'UNKNOWN_EXTERNAL_FLOW' "
            "ORDER BY settle_at"
        )
    )


# --------------------------------------------------------------------------
# Checkpoints
# --------------------------------------------------------------------------


def record_checkpoint(
    connection: sqlite3.Connection,
    *,
    account_fingerprint: str,
    taken_at: datetime,
    kind: str,
    broker_equity: Decimal,
    broker_cash: Decimal,
    now: datetime,
    unrealized_pnl: Decimal | None = None,
    position_count: int | None = None,
) -> int:
    """Record broker truth at an instant.

    A `DAILY_CLOSE` for a UTC date that already has one **replaces** it. Two
    authoritative closes for one day would let the reserve accrue twice off the
    same day's profit, so the unique index makes that unrepresentable and this
    function resolves the collision explicitly rather than raising at a caller
    that just re-read a settled day.
    """
    require_account(connection, account_fingerprint)
    if kind not in CHECKPOINT_KINDS:
        raise LiveAccountingInputError(f"unknown checkpoint kind {kind!r}")
    utc_date = taken_at.astimezone(UTC).date().isoformat()
    with transaction(connection):
        if kind == "DAILY_CLOSE":
            connection.execute(
                "DELETE FROM performance_checkpoints WHERE kind = 'DAILY_CLOSE' AND utc_date = ?",
                (utc_date,),
            )
        cursor = connection.execute(
            "INSERT INTO performance_checkpoints (account_fingerprint, taken_at, utc_date, "
            "kind, broker_equity, broker_cash, unrealized_pnl, position_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account_fingerprint,
                _iso(taken_at),
                utc_date,
                kind,
                decimal_text(broker_equity),
                decimal_text(broker_cash),
                _optional_decimal_text(unrealized_pnl),
                position_count,
                _iso(now),
            ),
        )
    return int(cursor.lastrowid or 0)


def read_checkpoints(connection: sqlite3.Connection) -> tuple[EquityCheckpoint, ...]:
    rows = connection.execute(
        "SELECT * FROM performance_checkpoints ORDER BY taken_at, checkpoint_id"
    )
    return tuple(
        EquityCheckpoint(
            taken_at=_parse(row["taken_at"]),
            kind=str(row["kind"]),
            broker_equity=text_decimal(row["broker_equity"]),
            broker_cash=text_decimal(row["broker_cash"]),
            unrealized_pnl=_optional_decimal(row["unrealized_pnl"]),
        )
        for row in rows
    )


def latest_checkpoint_row(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM performance_checkpoints ORDER BY taken_at DESC, checkpoint_id DESC LIMIT 1"
    ).fetchone()


def latest_authoritative_row(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM performance_checkpoints WHERE kind IN ('INCEPTION', 'DAILY_CLOSE') "
        "ORDER BY taken_at DESC, checkpoint_id DESC LIMIT 1"
    ).fetchone()


# --------------------------------------------------------------------------
# Derived state
# --------------------------------------------------------------------------


def replace_reserve_events(
    connection: sqlite3.Connection, events: tuple, *, reserve_rate: Decimal, now: datetime
) -> None:
    """Rewrite the derived reserve history wholesale.

    Wholesale, not incrementally, because these rows are a pure function of the
    authoritative tables. Appending to them would create a second history that
    could disagree with the first, and the disagreement would be invisible.
    """
    with transaction(connection):
        connection.execute("DELETE FROM profit_reserve_events")
        for event in events:
            connection.execute(
                "INSERT INTO profit_reserve_events (event_type, occurred_at, "
                "prior_reserve_hwm, new_reserve_hwm, new_hwm_profit, reserve_rate, "
                "accrual_amount, withdrawal_amount, allocated_from_bucket, unreserved_amount, "
                "bucket_before, bucket_after, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_type,
                    _iso(event.occurred_at),
                    decimal_text(event.prior_reserve_hwm),
                    decimal_text(event.new_reserve_hwm),
                    decimal_text(event.new_hwm_profit),
                    decimal_text(reserve_rate),
                    decimal_text(event.accrual_amount),
                    decimal_text(event.withdrawal_amount),
                    decimal_text(event.allocated_from_bucket),
                    decimal_text(event.unreserved_amount),
                    decimal_text(event.bucket_before),
                    decimal_text(event.bucket_after),
                    _iso(now),
                ),
            )


def mark_rebuild_required(connection: sqlite3.Connection, *, reason: str, now: datetime) -> None:
    with transaction(connection):
        connection.execute(
            "UPDATE live_accounting_state SET rebuild_required = 1, last_rebuild_reason = ?, "
            "updated_at = ? WHERE id = 1",
            (reason, _iso(now)),
        )


def record_rebuild(connection: sqlite3.Connection, *, reason: str, now: datetime) -> int:
    """Clear the rebuild flag and count the rebuild. A rebuild is never silent."""
    with transaction(connection):
        connection.execute(
            "UPDATE live_accounting_state SET rebuild_required = 0, "
            "rebuild_count = rebuild_count + 1, last_rebuild_at = ?, "
            "last_rebuild_reason = ?, updated_at = ? WHERE id = 1",
            (_iso(now), reason, _iso(now)),
        )
    row = read_state(connection)
    return 0 if row is None else int(row["rebuild_count"])


__all__ = [
    "AUTHORITATIVE_TABLES",
    "BUSY_TIMEOUT_MS",
    "DERIVED_TABLES",
    "INCEPTION_PRE_TRADE",
    "INCEPTION_RECONSTRUCTED",
    "JOURNAL_MODE",
    "LIVE_ACCOUNTING_SCHEMA_VERSION",
    "AccountScopeError",
    "LiveAccountingStoreError",
    "UnsupportedLiveAccountingSchemaError",
    "connect",
    "connect_read_only",
    "decimal_text",
    "freeze_inception",
    "initialize",
    "latest_authoritative_row",
    "latest_checkpoint_row",
    "mark_rebuild_required",
    "read_checkpoints",
    "read_flows",
    "read_state",
    "record_checkpoint",
    "record_flow",
    "record_rebuild",
    "replace_reserve_events",
    "require_account",
    "stamp_metadata",
    "stored_account",
    "text_decimal",
    "transaction",
    "unknown_flow_rows",
]
