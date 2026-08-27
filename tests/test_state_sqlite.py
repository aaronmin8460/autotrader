"""Phase 6 tests: the local SQLite operational-state foundation.

Every test is offline, needs no credentials, and writes only into pytest's
`tmp_path`. No test creates or touches a real persistent database.

Two groups matter most. The **transaction** tests prove that a failure part
way through a multi-write unit of work leaves nothing behind - verified from a
second connection, so a rollback that only cleared an in-process cache would
still fail. The **scope** tests assert what this module is *not*: no broker
table exists, no non-stdlib module is imported, and no socket is opened.
"""

from __future__ import annotations

import ast
import dataclasses
import socket
import sqlite3
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from autotrader import state
from autotrader.state import sqlite as state_sqlite
from autotrader.state.sqlite import (
    BUSY_TIMEOUT_MS,
    REQUIRED_TABLES,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_RUNNING,
    SCHEMA_VERSION,
    DatabaseStateError,
    DuplicateSignalError,
    Position,
    StateInputError,
    StoredSignal,
    StrategyRun,
    UnknownStrategyRunError,
    UnsupportedSchemaVersionError,
    connect,
    finish_strategy_run,
    from_utc_text,
    get_position,
    get_schema_version,
    get_strategy_run,
    initialize_database,
    list_positions,
    list_risk_events,
    list_signals,
    list_strategy_runs,
    list_system_events,
    record_risk_event,
    record_signal,
    record_strategy_run,
    record_system_event,
    to_utc_text,
    transaction,
    upsert_position,
)

T0 = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
STEP = timedelta(minutes=15)

#: Tables whose semantics belong to a broker this repository does not talk to.
#: Phase 7/8 will define them; guessing them now would be worse than waiting.
FORBIDDEN_BROKER_TABLES = (
    "broker_orders",
    "fills",
    "executions",
    "broker_accounts",
    "reconciliation_runs",
    "orders",
    "order_intents",
)


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    """An initialized database inside pytest's temporary directory."""
    return initialize_database(tmp_path / "state.db")


@pytest.fixture
def connection(database_path: Path):
    with connect(database_path) as open_connection:
        yield open_connection


def open_run(connection: sqlite3.Connection, *, symbol_free: bool = True) -> int:
    """A plain RUNNING backtest run to hang other records off."""
    assert symbol_free
    return record_strategy_run(
        connection,
        strategy_name="EMA20/EMA50",
        mode="BACKTEST",
        started_at=T0,
    )


def table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {str(row["name"]) for row in rows}


# --------------------------------------------------------------------------
# Initialization and schema
# --------------------------------------------------------------------------


def test_initialize_database_creates_the_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.db"
    assert not path.exists()

    returned = initialize_database(path)

    assert returned == path
    assert path.is_file()


def test_schema_version_is_one(connection: sqlite3.Connection) -> None:
    assert SCHEMA_VERSION == 1
    assert get_schema_version(connection) == 1


def test_initialization_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    initialize_database(path)
    with connect(path) as connection:
        run_id = open_run(connection)
        before = sorted(table_names(connection))

    initialize_database(path)
    initialize_database(path)

    with connect(path) as connection:
        assert sorted(table_names(connection)) == before
        assert get_schema_version(connection) == SCHEMA_VERSION
        # Repeated initialization must not wipe existing operational state.
        assert get_strategy_run(connection, run_id) is not None
        assert len(list_strategy_runs(connection)) == 1


def test_expected_tables_exist(connection: sqlite3.Connection) -> None:
    assert set(REQUIRED_TABLES) <= table_names(connection)


def test_no_unexpected_tables_exist(connection: sqlite3.Connection) -> None:
    # `sqlite_sequence` is created by SQLite itself for AUTOINCREMENT tables;
    # this schema uses none, so the table set should be exactly ours.
    assert table_names(connection) == set(REQUIRED_TABLES)


def test_foreign_keys_pragma_is_on(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_journal_mode_is_wal_for_a_filesystem_database(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_busy_timeout_is_configured(connection: sqlite3.Connection) -> None:
    assert BUSY_TIMEOUT_MS > 0
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS


def test_every_connection_configures_its_own_pragmas(database_path: Path) -> None:
    # Foreign keys are per-connection in SQLite, so a helper that set them only
    # at creation time would silently disable them for every later caller.
    for _ in range(2):
        with connect(database_path) as connection:
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS


def test_unsupported_future_schema_version_fails(database_path: Path) -> None:
    with connect(database_path) as connection, transaction(connection):
        connection.execute("UPDATE schema_metadata SET schema_version = ?", (SCHEMA_VERSION + 1,))

    with pytest.raises(UnsupportedSchemaVersionError) as error:
        initialize_database(database_path)

    assert str(SCHEMA_VERSION + 1) in str(error.value)

    # The newer database must be left exactly as it was, not downgraded.
    with connect(database_path) as connection:
        assert get_schema_version(connection) == SCHEMA_VERSION + 1


def test_older_schema_version_fails_without_migrating(database_path: Path) -> None:
    with connect(database_path) as connection, transaction(connection):
        connection.execute("UPDATE schema_metadata SET schema_version = 0")

    with pytest.raises(DatabaseStateError):
        initialize_database(database_path)


def test_missing_table_is_reported_as_an_inconsistent_database(database_path: Path) -> None:
    with connect(database_path) as connection, transaction(connection):
        connection.execute("DROP TABLE signals")

    with pytest.raises(DatabaseStateError) as error:
        initialize_database(database_path)

    assert "signals" in str(error.value)


def test_schema_metadata_without_tables_is_rejected(tmp_path: Path) -> None:
    # A database carrying our tables but no version marker is inconsistent;
    # initializing over it must not be attempted.
    path = tmp_path / "partial.db"
    with connect(path) as connection, transaction(connection):
        connection.execute("CREATE TABLE positions (symbol TEXT PRIMARY KEY)")

    with pytest.raises(DatabaseStateError) as error:
        initialize_database(path)

    assert "positions" in str(error.value)


def test_initialization_does_not_repair_a_damaged_database(database_path: Path) -> None:
    with connect(database_path) as connection, transaction(connection):
        connection.execute("DROP TABLE positions")

    with pytest.raises(DatabaseStateError):
        initialize_database(database_path)

    with connect(database_path) as connection:
        assert "positions" not in table_names(connection)


# --------------------------------------------------------------------------
# Strategy runs
# --------------------------------------------------------------------------


def test_strategy_run_round_trips(connection: sqlite3.Connection) -> None:
    run_id = record_strategy_run(
        connection,
        strategy_name="EMA20/EMA50",
        mode="BACKTEST",
        started_at=T0,
    )

    run = get_strategy_run(connection, run_id)

    assert run == StrategyRun(
        id=run_id,
        strategy_name="EMA20/EMA50",
        mode="BACKTEST",
        status=RUN_STATUS_RUNNING,
        started_at=T0,
        ended_at=None,
        created_at=run.created_at,
    )
    assert run.created_at.tzinfo is not None


def test_unknown_strategy_run_reads_as_none(connection: sqlite3.Connection) -> None:
    assert get_strategy_run(connection, 999) is None


def test_strategy_run_can_be_completed(connection: sqlite3.Connection) -> None:
    run_id = open_run(connection)
    ended = T0 + timedelta(hours=6)

    finish_strategy_run(connection, run_id, ended_at=ended, status=RUN_STATUS_COMPLETED)

    run = get_strategy_run(connection, run_id)
    assert run is not None
    assert run.status == RUN_STATUS_COMPLETED
    assert run.ended_at == ended


def test_strategy_run_can_be_marked_failed(connection: sqlite3.Connection) -> None:
    run_id = open_run(connection)

    finish_strategy_run(connection, run_id, ended_at=T0 + STEP, status=RUN_STATUS_FAILED)

    run = get_strategy_run(connection, run_id)
    assert run is not None
    assert run.status == RUN_STATUS_FAILED


def test_finishing_a_run_twice_is_rejected(connection: sqlite3.Connection) -> None:
    run_id = open_run(connection)
    finish_strategy_run(connection, run_id, ended_at=T0 + STEP)

    with pytest.raises(StateInputError):
        finish_strategy_run(connection, run_id, ended_at=T0 + 2 * STEP)

    run = get_strategy_run(connection, run_id)
    assert run is not None
    assert run.ended_at == T0 + STEP


def test_finishing_an_unknown_run_is_rejected(connection: sqlite3.Connection) -> None:
    with pytest.raises(UnknownStrategyRunError):
        finish_strategy_run(connection, 999, ended_at=T0)


def test_run_cannot_end_before_it_started(connection: sqlite3.Connection) -> None:
    run_id = open_run(connection)

    with pytest.raises(StateInputError):
        finish_strategy_run(connection, run_id, ended_at=T0 - STEP)

    run = get_strategy_run(connection, run_id)
    assert run is not None
    assert run.status == RUN_STATUS_RUNNING
    assert run.ended_at is None


def test_run_status_must_be_terminal_when_finishing(connection: sqlite3.Connection) -> None:
    run_id = open_run(connection)

    with pytest.raises(StateInputError):
        finish_strategy_run(connection, run_id, ended_at=T0 + STEP, status=RUN_STATUS_RUNNING)


def test_unknown_run_mode_is_rejected(connection: sqlite3.Connection) -> None:
    with pytest.raises(StateInputError):
        record_strategy_run(connection, strategy_name="EMA20/EMA50", mode="LIVE", started_at=T0)


def test_paper_mode_is_only_a_label(connection: sqlite3.Connection) -> None:
    # Phase 6 stores the word and implements none of the behaviour: recording
    # a PAPER run must not create an order, a position, or anything else.
    run_id = record_strategy_run(
        connection, strategy_name="EMA20/EMA50", mode="PAPER", started_at=T0
    )

    run = get_strategy_run(connection, run_id)
    assert run is not None
    assert run.mode == "PAPER"
    assert list_positions(connection) == []
    assert list_signals(connection) == []


def test_blank_strategy_name_is_rejected(connection: sqlite3.Connection) -> None:
    with pytest.raises(StateInputError):
        record_strategy_run(connection, strategy_name="   ", mode="BACKTEST", started_at=T0)


def test_strategy_runs_list_in_deterministic_order(connection: sqlite3.Connection) -> None:
    second = record_strategy_run(
        connection, strategy_name="B", mode="BACKTEST", started_at=T0 + STEP
    )
    first = record_strategy_run(connection, strategy_name="A", mode="BACKTEST", started_at=T0)

    assert [run.id for run in list_strategy_runs(connection)] == [first, second]


# --------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------


def test_signal_insert_round_trips(connection: sqlite3.Connection) -> None:
    run_id = open_run(connection)

    signal_id = record_signal(
        connection,
        strategy_run_id=run_id,
        signal_timestamp=T0 + STEP,
        symbol="SPY",
        signal_type="BUY",
        reason="EMA20_CROSS_ABOVE_EMA50",
    )

    stored = list_signals(connection)
    assert len(stored) == 1
    assert stored[0] == StoredSignal(
        id=signal_id,
        strategy_run_id=run_id,
        signal_timestamp=T0 + STEP,
        symbol="SPY",
        signal_type="BUY",
        reason="EMA20_CROSS_ABOVE_EMA50",
        created_at=stored[0].created_at,
    )


def test_signal_links_to_its_strategy_run(connection: sqlite3.Connection) -> None:
    first_run = open_run(connection)
    second_run = record_strategy_run(
        connection, strategy_name="EMA20/EMA50", mode="BACKTEST", started_at=T0 + STEP
    )
    record_signal(
        connection,
        strategy_run_id=first_run,
        signal_timestamp=T0 + STEP,
        symbol="SPY",
        signal_type="BUY",
        reason="EMA20_CROSS_ABOVE_EMA50",
    )
    record_signal(
        connection,
        strategy_run_id=second_run,
        signal_timestamp=T0 + STEP,
        symbol="QQQ",
        signal_type="EXIT",
        reason="EMA20_CROSS_BELOW_EMA50",
    )

    # "Which strategy run produced this signal?" must be answerable.
    assert [s.symbol for s in list_signals(connection, strategy_run_id=first_run)] == ["SPY"]
    assert [s.symbol for s in list_signals(connection, strategy_run_id=second_run)] == ["QQQ"]
    assert len(list_signals(connection)) == 2


def test_signal_with_invalid_strategy_run_is_rejected(connection: sqlite3.Connection) -> None:
    with pytest.raises(UnknownStrategyRunError):
        record_signal(
            connection,
            strategy_run_id=999,
            signal_timestamp=T0,
            symbol="SPY",
            signal_type="BUY",
            reason="EMA20_CROSS_ABOVE_EMA50",
        )

    assert list_signals(connection) == []


def test_duplicate_logical_signal_is_rejected(connection: sqlite3.Connection) -> None:
    run_id = open_run(connection)
    fields = {
        "strategy_run_id": run_id,
        "signal_timestamp": T0 + STEP,
        "symbol": "SPY",
        "signal_type": "BUY",
        "reason": "EMA20_CROSS_ABOVE_EMA50",
    }
    record_signal(connection, **fields)

    with pytest.raises(DuplicateSignalError):
        record_signal(connection, **fields)

    assert len(list_signals(connection)) == 1


def test_signals_differing_in_any_key_field_are_both_stored(
    connection: sqlite3.Connection,
) -> None:
    run_id = open_run(connection)
    base = {
        "strategy_run_id": run_id,
        "signal_timestamp": T0 + STEP,
        "symbol": "SPY",
        "signal_type": "BUY",
        "reason": "EMA20_CROSS_ABOVE_EMA50",
    }
    record_signal(connection, **base)
    record_signal(connection, **{**base, "signal_timestamp": T0 + 2 * STEP})
    record_signal(connection, **{**base, "symbol": "QQQ"})
    record_signal(
        connection,
        **{**base, "signal_type": "EXIT", "reason": "EMA20_CROSS_BELOW_EMA50"},
    )

    assert len(list_signals(connection)) == 4


def test_buy_signal_persists_exactly(connection: sqlite3.Connection) -> None:
    run_id = open_run(connection)
    record_signal(
        connection,
        strategy_run_id=run_id,
        signal_timestamp=T0,
        symbol="SPY",
        signal_type="BUY",
        reason="EMA20_CROSS_ABOVE_EMA50",
    )

    stored = list_signals(connection)[0]
    assert stored.signal_type == "BUY"
    assert stored.reason == "EMA20_CROSS_ABOVE_EMA50"
    assert stored.symbol == "SPY"
    assert stored.signal_timestamp == T0


def test_exit_signal_persists_as_exit_not_sell(connection: sqlite3.Connection) -> None:
    # An EXIT signal is not a trade. Translating it into a SELL is Phase 4's
    # decision inside a simulation, and would be Phase 7's inside execution;
    # persistence must never make it on the caller's behalf.
    run_id = open_run(connection)
    record_signal(
        connection,
        strategy_run_id=run_id,
        signal_timestamp=T0,
        symbol="SPY",
        signal_type="EXIT",
        reason="EMA20_CROSS_BELOW_EMA50",
    )

    stored = list_signals(connection)[0]
    assert stored.signal_type == "EXIT"
    assert stored.signal_type != "SELL"

    raw = connection.execute("SELECT signal_type FROM signals").fetchall()
    assert [row["signal_type"] for row in raw] == ["EXIT"]


def test_sell_is_not_a_storable_signal_type(connection: sqlite3.Connection) -> None:
    run_id = open_run(connection)

    with pytest.raises(StateInputError):
        record_signal(
            connection,
            strategy_run_id=run_id,
            signal_timestamp=T0,
            symbol="SPY",
            signal_type="SELL",
            reason="EMA20_CROSS_BELOW_EMA50",
        )

    # The schema refuses it too, so a write that bypassed validation still cannot
    # smuggle an execution vocabulary into the signal table.
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO signals "
            "(strategy_run_id, signal_timestamp, symbol, signal_type, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, to_utc_text(T0), "SPY", "SELL", "R", to_utc_text(T0)),
        )


def test_lowercase_symbol_is_rejected(connection: sqlite3.Connection) -> None:
    run_id = open_run(connection)

    with pytest.raises(StateInputError):
        record_signal(
            connection,
            strategy_run_id=run_id,
            signal_timestamp=T0,
            symbol="spy",
            signal_type="BUY",
            reason="EMA20_CROSS_ABOVE_EMA50",
        )


def test_signals_list_in_deterministic_order(connection: sqlite3.Connection) -> None:
    run_id = open_run(connection)
    for offset in (3, 1, 2):
        record_signal(
            connection,
            strategy_run_id=run_id,
            signal_timestamp=T0 + offset * STEP,
            symbol="SPY",
            signal_type="BUY",
            reason="EMA20_CROSS_ABOVE_EMA50",
        )

    timestamps = [signal.signal_timestamp for signal in list_signals(connection)]
    assert timestamps == [T0 + STEP, T0 + 2 * STEP, T0 + 3 * STEP]


# --------------------------------------------------------------------------
# Risk events
# --------------------------------------------------------------------------


def test_risk_event_persists(connection: sqlite3.Connection) -> None:
    run_id = open_run(connection)

    record_risk_event(
        connection,
        strategy_run_id=run_id,
        event_timestamp=T0,
        symbol="SPY",
        decision="REJECTED",
        reason_code="EXAMPLE_LIMIT",
        message="illustrative only",
    )

    events = list_risk_events(connection)
    assert len(events) == 1
    event = events[0]
    assert event.strategy_run_id == run_id
    assert event.symbol == "SPY"
    assert event.decision == "REJECTED"
    assert event.reason_code == "EXAMPLE_LIMIT"
    assert event.message == "illustrative only"
    assert event.event_timestamp == T0


def test_risk_event_run_id_and_symbol_are_nullable(connection: sqlite3.Connection) -> None:
    # A risk decision can be global - a daily loss limit - rather than tied to
    # one run or one ticker.
    record_risk_event(
        connection,
        event_timestamp=T0,
        decision="HALTED",
        reason_code="EXAMPLE_GLOBAL",
    )

    event = list_risk_events(connection)[0]
    assert event.strategy_run_id is None
    assert event.symbol is None
    assert event.message is None


def test_risk_event_decision_vocabulary_is_not_constrained(
    connection: sqlite3.Connection,
) -> None:
    # Phase 5 owns what a decision means. This table stores opaque text and
    # must not encode a guess at that vocabulary.
    for decision in ("ALLOW", "allow", "SOMETHING_ELSE", "42"):
        record_risk_event(
            connection,
            event_timestamp=T0,
            decision=decision,
            reason_code="EXAMPLE",
        )

    assert [event.decision for event in list_risk_events(connection)] == [
        "ALLOW",
        "allow",
        "SOMETHING_ELSE",
        "42",
    ]


def test_risk_event_with_unknown_run_is_rejected(connection: sqlite3.Connection) -> None:
    with pytest.raises(UnknownStrategyRunError):
        record_risk_event(
            connection,
            strategy_run_id=999,
            event_timestamp=T0,
            decision="ALLOW",
            reason_code="EXAMPLE",
        )


def test_risk_event_requires_a_reason_code(connection: sqlite3.Connection) -> None:
    with pytest.raises(StateInputError):
        record_risk_event(connection, event_timestamp=T0, decision="ALLOW", reason_code="")


# --------------------------------------------------------------------------
# System events
# --------------------------------------------------------------------------


def test_system_event_persists(connection: sqlite3.Connection) -> None:
    record_system_event(
        connection,
        event_timestamp=T0,
        event_type="DATABASE_INITIALIZED",
        message="schema version 1",
    )

    events = list_system_events(connection)
    assert len(events) == 1
    assert events[0].event_type == "DATABASE_INITIALIZED"
    assert events[0].message == "schema version 1"
    assert events[0].event_timestamp == T0


def test_system_event_message_is_optional(connection: sqlite3.Connection) -> None:
    record_system_event(connection, event_timestamp=T0, event_type="STARTED")

    assert list_system_events(connection)[0].message is None


def test_system_events_list_in_deterministic_order(connection: sqlite3.Connection) -> None:
    for offset in (2, 0, 1):
        record_system_event(connection, event_timestamp=T0 + offset * STEP, event_type=f"E{offset}")

    assert [event.event_type for event in list_system_events(connection)] == ["E0", "E1", "E2"]


# --------------------------------------------------------------------------
# Positions
# --------------------------------------------------------------------------


def test_position_upsert_creates_a_position(connection: sqlite3.Connection) -> None:
    upsert_position(connection, symbol="SPY", quantity=10, average_price=123.45, updated_at=T0)

    assert get_position(connection, "SPY") == Position(
        symbol="SPY", quantity=10, average_price=123.45, updated_at=T0
    )


def test_position_upsert_updates_a_position(connection: sqlite3.Connection) -> None:
    upsert_position(connection, symbol="SPY", quantity=10, average_price=100.0, updated_at=T0)

    upsert_position(
        connection, symbol="SPY", quantity=25, average_price=110.5, updated_at=T0 + STEP
    )

    assert get_position(connection, "SPY") == Position(
        symbol="SPY", quantity=25, average_price=110.5, updated_at=T0 + STEP
    )
    # One row per symbol: the snapshot is replaced, never appended to.
    assert len(list_positions(connection)) == 1


def test_zero_quantity_is_allowed(connection: sqlite3.Connection) -> None:
    upsert_position(connection, symbol="SPY", quantity=0, updated_at=T0)

    position = get_position(connection, "SPY")
    assert position is not None
    assert position.quantity == 0
    assert position.average_price is None


def test_negative_position_is_rejected(connection: sqlite3.Connection) -> None:
    with pytest.raises(StateInputError):
        upsert_position(connection, symbol="SPY", quantity=-1, updated_at=T0)

    assert get_position(connection, "SPY") is None


def test_negative_position_is_rejected_by_the_schema_too(
    connection: sqlite3.Connection,
) -> None:
    # Defence in depth: the long-only invariant must survive a write that did
    # not go through this module's validation.
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO positions (symbol, quantity, average_price, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("SPY", -1, None, to_utc_text(T0)),
        )


def test_fractional_quantity_is_rejected(connection: sqlite3.Connection) -> None:
    with pytest.raises(StateInputError):
        upsert_position(connection, symbol="SPY", quantity=1.5, updated_at=T0)


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_average_price_is_rejected(connection: sqlite3.Connection, price: float) -> None:
    with pytest.raises(StateInputError):
        upsert_position(connection, symbol="SPY", quantity=1, average_price=price, updated_at=T0)

    assert get_position(connection, "SPY") is None


def test_non_positive_average_price_is_rejected_by_the_schema_too(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO positions (symbol, quantity, average_price, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("SPY", 1, 0.0, to_utc_text(T0)),
        )


def test_unknown_position_reads_as_none(connection: sqlite3.Connection) -> None:
    # None means "no local snapshot", not "flat at the broker".
    assert get_position(connection, "SPY") is None


def test_list_positions_is_deterministically_ordered(connection: sqlite3.Connection) -> None:
    for symbol in ("QQQ", "AAPL", "SPY", "MSFT"):
        upsert_position(connection, symbol=symbol, quantity=1, updated_at=T0)

    assert [position.symbol for position in list_positions(connection)] == [
        "AAPL",
        "MSFT",
        "QQQ",
        "SPY",
    ]


def test_no_pnl_is_stored(connection: sqlite3.Connection) -> None:
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(positions)")}
    assert columns == {"symbol", "quantity", "average_price", "updated_at"}


# --------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------


def test_utc_timestamp_round_trips(connection: sqlite3.Connection) -> None:
    moment = datetime(2025, 3, 14, 15, 9, 26, 535897, tzinfo=UTC)
    record_system_event(connection, event_timestamp=moment, event_type="ROUND_TRIP")

    stored = list_system_events(connection)[0].event_timestamp
    assert stored == moment
    assert stored.tzinfo is not None
    assert stored.utcoffset() == timedelta(0)


def test_non_utc_aware_timestamp_normalizes_to_utc(connection: sqlite3.Connection) -> None:
    eastern = datetime(2025, 1, 2, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    record_system_event(connection, event_timestamp=eastern, event_type="NORMALIZED")

    stored = list_system_events(connection)[0].event_timestamp
    assert stored == eastern
    assert stored.utcoffset() == timedelta(0)
    assert stored == datetime(2025, 1, 2, 14, 30, tzinfo=UTC)


def test_fixed_offset_timestamp_normalizes_to_utc() -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    assert to_utc_text(datetime(2025, 1, 2, 20, 0, tzinfo=offset)) == (
        "2025-01-02T14:30:00.000000+00:00"
    )


def test_naive_datetime_is_rejected(connection: sqlite3.Connection) -> None:
    naive = datetime(2025, 1, 2, 14, 30)

    with pytest.raises(StateInputError) as error:
        record_system_event(connection, event_timestamp=naive, event_type="NAIVE")

    assert "timezone-aware" in str(error.value)
    assert list_system_events(connection) == []


def test_naive_datetime_is_rejected_everywhere_a_timestamp_is_accepted(
    connection: sqlite3.Connection,
) -> None:
    naive = datetime(2025, 1, 2, 14, 30)
    run_id = open_run(connection)

    with pytest.raises(StateInputError):
        record_strategy_run(connection, strategy_name="X", mode="BACKTEST", started_at=naive)
    with pytest.raises(StateInputError):
        finish_strategy_run(connection, run_id, ended_at=naive)
    with pytest.raises(StateInputError):
        record_signal(
            connection,
            strategy_run_id=run_id,
            signal_timestamp=naive,
            symbol="SPY",
            signal_type="BUY",
            reason="R",
        )
    with pytest.raises(StateInputError):
        record_risk_event(connection, event_timestamp=naive, decision="ALLOW", reason_code="R")
    with pytest.raises(StateInputError):
        upsert_position(connection, symbol="SPY", quantity=1, updated_at=naive)


def test_persisted_timestamps_use_one_canonical_utc_form(
    connection: sqlite3.Connection,
) -> None:
    run_id = record_strategy_run(
        connection,
        strategy_name="EMA20/EMA50",
        mode="BACKTEST",
        started_at=datetime(2025, 1, 2, 9, 30, tzinfo=ZoneInfo("America/New_York")),
    )
    record_signal(
        connection,
        strategy_run_id=run_id,
        signal_timestamp=T0,
        symbol="SPY",
        signal_type="BUY",
        reason="R",
    )
    upsert_position(connection, symbol="SPY", quantity=1, updated_at=T0)

    stored: list[str] = []
    for query in (
        "SELECT started_at AS t, created_at AS c FROM strategy_runs",
        "SELECT signal_timestamp AS t, created_at AS c FROM signals",
        "SELECT updated_at AS t, updated_at AS c FROM positions",
        "SELECT created_at AS t, created_at AS c FROM schema_metadata",
    ):
        for row in connection.execute(query):
            stored.extend([str(row["t"]), str(row["c"])])

    assert stored
    for value in stored:
        assert value.endswith("+00:00"), value
        assert len(value) == len("2025-01-02T14:30:00.000000+00:00"), value
        assert from_utc_text(value).utcoffset() == timedelta(0)


def test_stored_timestamp_text_sorts_chronologically() -> None:
    # Fixed-width canonical text means ORDER BY on the column is also
    # chronological, without parsing anything.
    moments = [
        datetime(2025, 1, 2, 14, 30, tzinfo=UTC),
        datetime(2025, 1, 2, 14, 30, 0, 500_000, tzinfo=UTC),
        datetime(2025, 1, 2, 14, 30, 1, tzinfo=UTC),
        datetime(2025, 1, 2, 14, 31, tzinfo=UTC),
        datetime(2025, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC),
    ]
    texts = [to_utc_text(moment) for moment in moments]

    assert texts == sorted(texts)


def test_non_datetime_timestamp_is_rejected(connection: sqlite3.Connection) -> None:
    with pytest.raises(StateInputError):
        record_system_event(connection, event_timestamp="2025-01-02T14:30:00+00:00", event_type="X")


def test_unparsable_stored_timestamp_is_a_controlled_error(
    connection: sqlite3.Connection,
) -> None:
    record_system_event(connection, event_timestamp=T0, event_type="X")
    with transaction(connection):
        connection.execute("UPDATE system_events SET event_timestamp = 'not-a-timestamp'")

    with pytest.raises(DatabaseStateError):
        list_system_events(connection)


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------


def test_successful_transaction_commits(database_path: Path) -> None:
    with connect(database_path) as connection, transaction(connection):
        record_system_event(connection, event_timestamp=T0, event_type="FIRST")
        record_system_event(connection, event_timestamp=T0 + STEP, event_type="SECOND")

    with connect(database_path) as connection:
        assert [event.event_type for event in list_system_events(connection)] == [
            "FIRST",
            "SECOND",
        ]


def test_transaction_rolls_back_all_writes_on_failure(database_path: Path) -> None:
    """The Phase 6 rollback regression test.

    One transaction writes a valid record and then violates a constraint. Once
    the failure propagates, *neither* write may remain committed - a partially
    applied unit of operational trading state is exactly the kind of corruption
    this foundation exists to prevent.
    """
    with connect(database_path) as connection:
        run_id = open_run(connection)

        with pytest.raises(UnknownStrategyRunError), transaction(connection):
            record_system_event(connection, event_timestamp=T0, event_type="ABOUT_TO_FAIL")
            upsert_position(connection, symbol="SPY", quantity=7, updated_at=T0)
            # No such strategy run: a foreign-key violation inside the same
            # transaction as the two writes above.
            record_signal(
                connection,
                strategy_run_id=run_id + 1000,
                signal_timestamp=T0,
                symbol="SPY",
                signal_type="BUY",
                reason="EMA20_CROSS_ABOVE_EMA50",
            )

    # Verified from a *fresh* connection, so a rollback that only cleared an
    # in-process cache would still fail this test.
    with connect(database_path) as connection:
        assert list_system_events(connection) == []
        assert list_positions(connection) == []
        assert list_signals(connection) == []
        # The run committed before the transaction opened is untouched.
        assert get_strategy_run(connection, run_id) is not None


def test_transaction_rolls_back_on_a_validation_failure(database_path: Path) -> None:
    with (
        connect(database_path) as connection,
        pytest.raises(StateInputError),
        transaction(connection),
    ):
        record_system_event(connection, event_timestamp=T0, event_type="FIRST")
        # Rejected in Python, before any SQL is issued; the rollback must
        # still discard the write that already succeeded.
        upsert_position(connection, symbol="SPY", quantity=-5, updated_at=T0)

    with connect(database_path) as connection:
        assert list_system_events(connection) == []


def test_uncommitted_state_is_not_visible_to_another_connection(database_path: Path) -> None:
    with connect(database_path) as writer, connect(database_path) as reader:
        with transaction(writer):
            record_system_event(writer, event_timestamp=T0, event_type="PENDING")
            assert len(list_system_events(writer)) == 1
            assert list_system_events(reader) == []

        assert len(list_system_events(reader)) == 1


def test_two_connections_see_committed_state(database_path: Path) -> None:
    with connect(database_path) as writer:
        run_id = open_run(writer)
        record_signal(
            writer,
            strategy_run_id=run_id,
            signal_timestamp=T0,
            symbol="SPY",
            signal_type="BUY",
            reason="EMA20_CROSS_ABOVE_EMA50",
        )

    with connect(database_path) as reader:
        assert get_strategy_run(reader, run_id) is not None
        assert len(list_signals(reader)) == 1


def test_nested_transaction_joins_the_outer_one(database_path: Path) -> None:
    # Each record_* helper opens a transaction of its own. Nested inside a
    # caller's transaction they must join it, not commit early.
    with (
        connect(database_path) as connection,
        pytest.raises(RuntimeError),
        transaction(connection),
    ):
        record_system_event(connection, event_timestamp=T0, event_type="FIRST")
        assert connection.in_transaction
        raise RuntimeError("caller aborted the unit of work")

    with connect(database_path) as connection:
        assert list_system_events(connection) == []


def test_a_single_record_call_commits_on_its_own(database_path: Path) -> None:
    with connect(database_path) as connection:
        record_system_event(connection, event_timestamp=T0, event_type="STANDALONE")
        assert not connection.in_transaction

    with connect(database_path) as connection:
        assert len(list_system_events(connection)) == 1


def test_failed_write_leaves_the_connection_usable(connection: sqlite3.Connection) -> None:
    with pytest.raises(StateInputError):
        upsert_position(connection, symbol="SPY", quantity=-1, updated_at=T0)

    upsert_position(connection, symbol="SPY", quantity=1, updated_at=T0)
    assert get_position(connection, "SPY") is not None


# --------------------------------------------------------------------------
# SQL safety
# --------------------------------------------------------------------------


def module_source() -> str:
    return Path(state_sqlite.__file__).read_text(encoding="utf-8")


def test_every_sql_statement_is_a_literal_or_a_module_constant() -> None:
    """Source-level assertion: no SQL is ever built by string interpolation."""
    tree = ast.parse(module_source())
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"execute", "executemany", "executescript"}:
            continue
        assert node.args, "a SQL call must carry a statement"
        statement = node.args[0]
        assert isinstance(statement, ast.Constant | ast.Name), (
            f"SQL at line {node.lineno} is built dynamically "
            f"({type(statement).__name__}); it must be a literal or a module constant"
        )
        if isinstance(statement, ast.Constant):
            assert isinstance(statement.value, str)
        checked += 1
    assert checked > 0


def test_module_never_formats_sql_strings() -> None:
    source = module_source()
    for forbidden in ('execute(f"', "execute(f'", ".format(", '" % ', "' % "):
        assert forbidden not in source, forbidden


def test_hostile_values_are_stored_verbatim_not_executed(
    connection: sqlite3.Connection,
) -> None:
    injection = "DROP TABLE system_events;--"
    record_system_event(
        connection,
        event_timestamp=T0,
        event_type=injection,
        message=f"'); {injection}",
    )

    events = list_system_events(connection)
    assert len(events) == 1
    assert events[0].event_type == injection
    assert events[0].message == f"'); {injection}"
    assert "system_events" in table_names(connection)


def test_hostile_symbol_is_stored_verbatim(connection: sqlite3.Connection) -> None:
    injection = "SPY'); DROP TABLE POSITIONS;--"
    upsert_position(connection, symbol=injection, quantity=1, updated_at=T0)

    assert get_position(connection, injection) is not None
    assert "positions" in table_names(connection)


def test_hostile_strategy_name_is_stored_verbatim(connection: sqlite3.Connection) -> None:
    injection = "x'; DROP TABLE strategy_runs;--"
    run_id = record_strategy_run(
        connection, strategy_name=injection, mode="BACKTEST", started_at=T0
    )

    run = get_strategy_run(connection, run_id)
    assert run is not None
    assert run.strategy_name == injection
    assert "strategy_runs" in table_names(connection)


# --------------------------------------------------------------------------
# Scope: no broker, no credentials, no network
# --------------------------------------------------------------------------


def test_no_phase_seven_broker_tables_exist(connection: sqlite3.Connection) -> None:
    present = table_names(connection)
    for forbidden in FORBIDDEN_BROKER_TABLES:
        assert forbidden not in present, forbidden


def test_no_broker_table_is_referenced_in_the_source() -> None:
    source = module_source()
    for forbidden in FORBIDDEN_BROKER_TABLES:
        assert f"CREATE TABLE {forbidden}" not in source, forbidden


def test_module_imports_only_the_standard_library() -> None:
    tree = ast.parse(module_source())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    assert imported == {
        "__future__",
        "collections",
        "contextlib",
        "dataclasses",
        "datetime",
        "math",
        "pathlib",
        "sqlite3",
    }
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}


def test_database_code_requires_no_alpaca_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_PAPER"):
        monkeypatch.delenv(name, raising=False)
    assert "alpaca" not in module_source().lower()

    path = initialize_database(tmp_path / "no-credentials.db")
    with connect(path) as connection:
        run_id = open_run(connection)
        record_signal(
            connection,
            strategy_run_id=run_id,
            signal_timestamp=T0,
            symbol="SPY",
            signal_type="BUY",
            reason="EMA20_CROSS_ABOVE_EMA50",
        )
        upsert_position(connection, symbol="SPY", quantity=1, updated_at=T0)
        assert len(list_signals(connection)) == 1


def test_database_code_makes_no_network_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def blocked(*args: object, **kwargs: object):
        raise AssertionError("the state module must not open a socket")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

    path = initialize_database(tmp_path / "offline.db")
    with connect(path) as connection:
        run_id = open_run(connection)
        record_risk_event(
            connection,
            strategy_run_id=run_id,
            event_timestamp=T0,
            decision="ALLOW",
            reason_code="EXAMPLE",
        )
        record_system_event(connection, event_timestamp=T0, event_type="OFFLINE")
        upsert_position(connection, symbol="SPY", quantity=1, updated_at=T0)
        finish_strategy_run(connection, run_id, ended_at=T0 + STEP)

    assert path.is_file()


def test_no_database_file_is_created_outside_the_temporary_directory(
    tmp_path: Path,
) -> None:
    # The production database path is a policy constant, not something the
    # tests are allowed to touch.
    assert not state.DEFAULT_DATABASE_PATH.is_absolute()
    assert state.DEFAULT_DATABASE_PATH.suffix == ".db"

    path = initialize_database(tmp_path / "scoped.db")
    assert tmp_path in path.parents


# --------------------------------------------------------------------------
# Read models and public surface
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model", [StrategyRun, StoredSignal, Position, state.RiskEvent, state.SystemEvent]
)
def test_read_models_are_frozen_dataclasses(model: type) -> None:
    assert dataclasses.is_dataclass(model)
    assert model.__dataclass_params__.frozen


def test_public_api_is_re_exported_by_the_package() -> None:
    for name in state_sqlite.__all__:
        assert hasattr(state, name), name
        assert getattr(state, name) is getattr(state_sqlite, name)


def test_reads_return_typed_records_not_raw_rows(connection: sqlite3.Connection) -> None:
    run_id = open_run(connection)
    upsert_position(connection, symbol="SPY", quantity=1, updated_at=T0)

    assert isinstance(get_strategy_run(connection, run_id), StrategyRun)
    assert isinstance(get_position(connection, "SPY"), Position)
    assert not isinstance(get_position(connection, "SPY"), sqlite3.Row)
