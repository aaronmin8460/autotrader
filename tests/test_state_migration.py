"""C6/C8 schema tests: the v1 -> v2 -> v3 -> v4 migration path and the order tables.

Every test is offline and writes only into pytest's temporary directory. No
credential is read and no socket is opened - this is the persistence layer,
which has never talked to a broker and still does not.

The migration is the risky part of any schema change: it runs against a
database that may already hold real operational history. v3 had to *rebuild*
three tables to widen their quantity columns from whole integers to exact
decimal text; v4 rebuilds `order_intents` again, this time only to widen a
CHECK constraint so reconciliation can record that the broker definitively
never received an order. These tests exist to prove each rebuild is
transactional, idempotent, schema-identical to a fresh database, and that it
carries every existing row across unchanged - an integer `100` becoming the
decimal `"100"` in v3, and nothing at all moving in v4.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from autotrader.state import sqlite as state
from autotrader.state.sqlite import (
    INTENT_STATUS_CONFIRMED_NOT_SUBMITTED,
    MIN_MIGRATABLE_SCHEMA_VERSION,
    REQUIRED_TABLES,
    SCHEMA_VERSION,
    V2_TABLES,
    V3_TABLES,
    V4_TABLES,
    V5_TABLES,
    DatabaseStateError,
    DuplicateBrokerOrderError,
    DuplicateOrderIntentError,
    StateInputError,
    UnknownOrderIntentError,
    UnknownStrategyRunError,
    UnsupportedSchemaVersionError,
    connect,
    ensure_daily_risk_baseline,
    get_broker_order_by_client_id,
    get_broker_order_by_intent,
    get_daily_risk_baseline,
    get_order_intent,
    get_order_intent_by_client_id,
    get_position,
    get_runtime_checkpoint,
    get_schema_version,
    get_strategy_run,
    initialize_database,
    list_broker_orders,
    list_daily_risk_baselines,
    list_order_intents,
    list_reconciliation_events,
    list_reconciliation_runs,
    list_risk_events,
    list_runtime_checkpoints,
    list_signals,
    list_strategy_runs,
    record_order_intent,
    record_reconciliation_event,
    record_reconciliation_run,
    record_risk_event,
    record_signal,
    record_strategy_run,
    transaction,
    update_order_intent_status,
    upsert_broker_order,
    upsert_position,
    upsert_runtime_checkpoint,
)

T0 = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
STEP = timedelta(minutes=15)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def build_v1_database(path: Path) -> Path:
    """Create a database exactly as the v1 release would have left it.

    Uses the module's own retained v1 statements, so this fixture is the real
    historical shape rather than a hand-copied approximation of it.
    """
    with connect(path) as connection, transaction(connection):
        for statement in state._V1_SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            state._INSERT_SCHEMA_VERSION,
            (MIN_MIGRATABLE_SCHEMA_VERSION, "2025-01-02T00:00:00.000000+00:00"),
        )
    return path


def build_v2_database(path: Path) -> Path:
    """Create a database exactly as the v2 (equity) release would have left it."""
    build_v1_database(path)
    with connect(path) as connection, transaction(connection):
        for statement in state._V2_SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute(state._UPDATE_SCHEMA_VERSION, (2,))
    return path


#: The pre-pivot equity rows a real v1/v2 database would hold. Written with raw
#: SQL because the current Python API refuses both an integer-only quantity
#: column and an equity ticker - which is the point: this is history, and the
#: migration has to carry it forward without judging it.
LEGACY_POSITION = ("SPY", 7, 101.5)
LEGACY_INTENT = ("autotrader-legacy-1", "SPY", "BUY", 10, 3, 500.0)
LEGACY_BROKER_ORDER = ("broker-legacy-1", "autotrader-legacy-1", "SPY", "BUY", 100, 25, 501.5)


def populate_v1_data(path: Path) -> dict[str, object]:
    """Write one row into every v1 table and describe what was written."""
    with connect(path) as connection:
        run_id = record_strategy_run(
            connection, strategy_name="EMA20/EMA50", mode="BACKTEST", started_at=T0
        )
        record_signal(
            connection,
            strategy_run_id=run_id,
            signal_timestamp=T0,
            symbol="SPY",
            signal_type="BUY",
            reason="ema20 crossed above ema50",
        )
        record_risk_event(
            connection,
            event_timestamp=T0,
            decision="APPROVED",
            reason_code="APPROVED",
            symbol="SPY",
            strategy_run_id=run_id,
        )
        with transaction(connection):
            connection.execute(
                "INSERT INTO positions (symbol, quantity, average_price, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (*LEGACY_POSITION, state.to_utc_text(T0)),
            )
    return {"run_id": run_id}


def populate_v2_order_data(path: Path) -> None:
    """Write one legacy integer-quantity order intent and broker order."""
    stamp = state.to_utc_text(T0)
    with connect(path) as connection, transaction(connection):
        connection.execute(
            "INSERT INTO order_intents (id, client_order_id, strategy_run_id, created_at, "
            "symbol, side, requested_quantity, approved_quantity, reference_price, "
            "risk_reason_code, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                LEGACY_INTENT[0],
                None,
                stamp,
                LEGACY_INTENT[1],
                LEGACY_INTENT[2],
                LEGACY_INTENT[3],
                LEGACY_INTENT[4],
                LEGACY_INTENT[5],
                "POSITION_LIMIT",
                "SUBMITTED",
                stamp,
            ),
        )
        connection.execute(
            "INSERT INTO broker_orders (id, order_intent_id, broker_order_id, client_order_id, "
            "symbol, side, quantity, filled_quantity, filled_average_price, status, "
            "submitted_at, filled_at, updated_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                1,
                LEGACY_BROKER_ORDER[0],
                LEGACY_BROKER_ORDER[1],
                LEGACY_BROKER_ORDER[2],
                LEGACY_BROKER_ORDER[3],
                LEGACY_BROKER_ORDER[4],
                LEGACY_BROKER_ORDER[5],
                LEGACY_BROKER_ORDER[6],
                "accepted",
                stamp,
                None,
                stamp,
                stamp,
            ),
        )


@pytest.fixture
def v1_database(tmp_path: Path) -> Path:
    return build_v1_database(tmp_path / "v1.db")


def build_v3_database(path: Path) -> Path:
    """Create a database exactly as the v3 (crypto pivot) release would have left it.

    Built from the module's own retained v3 `order_intents` literal, so this is
    the real historical shape - narrower status vocabulary included - rather
    than a hand-copied approximation of it.
    """
    with connect(path) as connection, transaction(connection):
        for statement in (
            state._CREATE_SCHEMA_METADATA,
            state._CREATE_STRATEGY_RUNS,
            state._CREATE_SIGNALS,
            state._CREATE_RISK_EVENTS,
            state._CREATE_SYSTEM_EVENTS,
            state._CREATE_POSITIONS,
            state._CREATE_ORDER_INTENTS_V3,
            state._CREATE_BROKER_ORDERS,
            state._CREATE_DAILY_RISK_BASELINES,
            state._CREATE_INDEX_SIGNALS,
            state._CREATE_INDEX_RISK_EVENTS,
            state._CREATE_INDEX_ORDER_INTENTS_STATUS,
        ):
            connection.execute(statement)
        connection.execute(state._INSERT_SCHEMA_VERSION, (3, "2025-01-02T00:00:00.000000+00:00"))
    return path


@pytest.fixture
def v2_database(tmp_path: Path) -> Path:
    return build_v2_database(tmp_path / "v2.db")


@pytest.fixture
def v3_database(tmp_path: Path) -> Path:
    return build_v3_database(tmp_path / "v3.db")


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return initialize_database(tmp_path / "state.db")


@pytest.fixture
def connection(database_path: Path):
    with connect(database_path) as open_connection:
        yield open_connection


@pytest.fixture
def intent_id(connection: sqlite3.Connection) -> int:
    """One CREATED buy intent to hang broker-order tests off."""
    return record_order_intent(
        connection,
        client_order_id="autotrader-fixture-1",
        created_at=T0,
        symbol="BTC/USD",
        side="BUY",
        requested_quantity=Decimal("0.01"),
        approved_quantity=Decimal("0.0025"),
        reference_price=104_000.0,
        risk_reason_code="POSITION_LIMIT",
    )


def table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {str(row["name"]) for row in rows}


def schema_objects(connection: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Every schema object, normalized for comparison."""
    return sorted(
        (str(row[0]), str(row[1]), (row[2] or "").strip())
        for row in connection.execute("SELECT type, name, sql FROM sqlite_master")
    )


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------


def test_a_new_database_initializes_directly_at_the_current_version(database_path: Path) -> None:
    """A fresh database is never created at an older version and then upgraded."""
    with connect(database_path) as connection:
        assert get_schema_version(connection) == SCHEMA_VERSION == 5
        assert set(REQUIRED_TABLES) <= table_names(connection)
        assert set(V3_TABLES) <= table_names(connection)
        assert set(V4_TABLES) <= table_names(connection)
        assert set(V5_TABLES) <= table_names(connection)


def test_a_version_one_database_migrates_all_the_way_to_three(v1_database: Path) -> None:
    with connect(v1_database) as connection:
        assert get_schema_version(connection) == 1
        assert not (set(V2_TABLES) | set(V3_TABLES)) & table_names(connection)

    initialize_database(v1_database)

    with connect(v1_database) as connection:
        assert get_schema_version(connection) == SCHEMA_VERSION
        assert set(V2_TABLES) <= table_names(connection)
        assert set(V3_TABLES) <= table_names(connection)


def test_a_version_two_database_migrates_to_three(v2_database: Path) -> None:
    with connect(v2_database) as connection:
        assert get_schema_version(connection) == 2
        assert not set(V3_TABLES) & table_names(connection)

    initialize_database(v2_database)

    with connect(v2_database) as connection:
        assert get_schema_version(connection) == SCHEMA_VERSION
        assert set(V3_TABLES) <= table_names(connection)


@pytest.mark.parametrize("builder", [build_v1_database, build_v2_database, build_v3_database])
def test_a_migrated_schema_is_identical_to_a_freshly_created_one(tmp_path: Path, builder) -> None:
    """ "Migrated to v3" and "created as v3" must be the same database.

    Byte-identical, `sqlite_master.sql` included. v3 rebuilds three tables, and
    the rebuild deliberately creates each one under its real name from the same
    literal a fresh database uses - rather than renaming a temporary table into
    place, which would leave SQLite's own quoting in the stored schema and make
    the two paths quietly different.
    """
    legacy = builder(tmp_path / "legacy.db")
    initialize_database(legacy)
    fresh = initialize_database(tmp_path / "fresh.db")

    with connect(legacy) as migrated_connection, connect(fresh) as fresh_connection:
        assert schema_objects(migrated_connection) == schema_objects(fresh_connection)


def test_existing_v1_data_survives_the_migration(v1_database: Path) -> None:
    written = populate_v1_data(v1_database)

    initialize_database(v1_database)

    with connect(v1_database) as connection:
        assert get_schema_version(connection) == SCHEMA_VERSION
        assert get_strategy_run(connection, int(written["run_id"])) is not None
        assert len(list_strategy_runs(connection)) == 1
        assert len(list_signals(connection)) == 1
        assert len(list_risk_events(connection)) == 1

        position = get_position(connection, "SPY")
        assert position is not None
        assert position.quantity == Decimal(7)
        assert position.average_price == 101.5
        assert position.updated_at == T0


def test_legacy_integer_quantities_migrate_to_exact_decimals(v2_database: Path) -> None:
    """`1` becomes `"1"` and `100` becomes `"100"` - the same number, written out.

    Nothing is scaled, rounded, or given an invented precision, and no row is
    dropped: ids, keys, prices, statuses, and timestamps all come through.
    """
    populate_v1_data(v2_database)
    populate_v2_order_data(v2_database)

    initialize_database(v2_database)

    with connect(v2_database) as connection:
        position = get_position(connection, "SPY")
        assert position is not None
        assert position.quantity == Decimal(7)
        assert str(position.quantity) == "7"

        [intent] = list_order_intents(connection)
        assert intent.id == 1
        assert intent.client_order_id == LEGACY_INTENT[0]
        assert intent.requested_quantity == Decimal(10)
        assert intent.approved_quantity == Decimal(3)
        assert str(intent.requested_quantity) == "10"
        assert str(intent.approved_quantity) == "3"
        assert intent.reference_price == 500.0
        assert intent.status == "SUBMITTED"
        assert intent.created_at == T0

        [order] = list_broker_orders(connection)
        assert order.id == 1
        assert order.order_intent_id == 1
        assert order.broker_order_id == LEGACY_BROKER_ORDER[0]
        assert order.quantity == Decimal(100)
        assert order.filled_quantity == Decimal(25)
        assert str(order.quantity) == "100"
        assert str(order.filled_quantity) == "25"
        assert order.filled_average_price == 501.5
        assert order.status == "accepted"


def test_migrated_quantities_are_stored_as_text_not_numbers(v2_database: Path) -> None:
    populate_v1_data(v2_database)
    populate_v2_order_data(v2_database)

    initialize_database(v2_database)

    with connect(v2_database) as connection:
        assert connection.execute("SELECT typeof(quantity) FROM positions").fetchone()[0] == "text"
        assert (
            connection.execute("SELECT typeof(requested_quantity) FROM order_intents").fetchone()[0]
            == "text"
        )
        assert (
            connection.execute("SELECT typeof(filled_quantity) FROM broker_orders").fetchone()[0]
            == "text"
        )


def test_the_foreign_key_from_a_migrated_broker_order_still_resolves(
    v2_database: Path,
) -> None:
    """The rebuild drops and recreates both tables; the link must survive it."""
    populate_v1_data(v2_database)
    populate_v2_order_data(v2_database)

    initialize_database(v2_database)

    with connect(v2_database) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        order = get_broker_order_by_client_id(connection, LEGACY_INTENT[0])
        assert order is not None
        intent = get_order_intent(connection, order.order_intent_id)
        assert intent is not None and intent.client_order_id == LEGACY_INTENT[0]


def test_a_fractional_quantity_round_trips_after_the_migration(v2_database: Path) -> None:
    """The reason for the whole rebuild: 0.0001 BTC is now storable."""
    initialize_database(v2_database)

    with connect(v2_database) as connection:
        upsert_position(connection, symbol="BTC/USD", quantity=Decimal("0.00012345"), updated_at=T0)
        intent = record_order_intent(
            connection,
            client_order_id="autotrader-post-migration",
            created_at=T0,
            symbol="BTC/USD",
            side="BUY",
            requested_quantity=Decimal("0.01"),
            approved_quantity=Decimal("0.000123456789012345"),
            reference_price=104_000.0,
            risk_reason_code="POSITION_LIMIT",
        )
        upsert_broker_order(
            connection,
            order_intent_id=intent,
            broker_order_id="broker-post-migration",
            client_order_id="autotrader-post-migration",
            symbol="BTC/USD",
            side="BUY",
            quantity=Decimal("0.000123456789012345"),
            filled_quantity=Decimal("0.000000000000000001"),
            status="accepted",
            updated_at=T0,
        )

        position = get_position(connection, "BTC/USD")
        assert position is not None and position.quantity == Decimal("0.00012345")
        stored_intent = get_order_intent(connection, intent)
        assert stored_intent is not None
        assert stored_intent.approved_quantity == Decimal("0.000123456789012345")
        order = get_broker_order_by_intent(connection, intent)
        assert order is not None
        assert order.quantity == Decimal("0.000123456789012345")
        assert order.filled_quantity == Decimal("0.000000000000000001")


def test_the_daily_risk_baseline_table_arrives_with_v3(v2_database: Path) -> None:
    initialize_database(v2_database)

    with connect(v2_database) as connection:
        assert "daily_risk_baselines" in table_names(connection)
        assert list_daily_risk_baselines(connection) == []
        ensure_daily_risk_baseline(
            connection,
            risk_date_utc=date(2025, 1, 2),
            baseline_equity=Decimal("100000"),
            captured_at=T0,
        )
        stored = get_daily_risk_baseline(connection, date(2025, 1, 2))
        assert stored is not None and stored.baseline_equity == Decimal("100000")


@pytest.mark.parametrize("builder", [build_v1_database, build_v2_database, build_v3_database])
def test_migration_is_idempotent(tmp_path: Path, builder) -> None:
    legacy = builder(tmp_path / "legacy.db")
    populate_v1_data(legacy)
    initialize_database(legacy)

    with connect(legacy) as connection:
        before = schema_objects(connection)
        before_position = get_position(connection, "SPY")

    initialize_database(legacy)
    initialize_database(legacy)

    with connect(legacy) as connection:
        assert schema_objects(connection) == before
        assert get_schema_version(connection) == SCHEMA_VERSION
        assert len(list_strategy_runs(connection)) == 1
        assert get_position(connection, "SPY") == before_position


def test_a_failed_v1_to_v2_migration_rolls_back_completely(v1_database: Path) -> None:
    """A conflicting table must leave the database untouched on v1.

    SQLite DDL is transactional, so a migration that fails part-way must not
    leave one new table behind, a bumped version marker, or any other
    half-applied state.
    """
    populate_v1_data(v1_database)
    with connect(v1_database) as connection, transaction(connection):
        # Occupies one of the names the migration is about to create.
        connection.execute("CREATE TABLE broker_orders (surprise TEXT)")

    with pytest.raises(DatabaseStateError) as error:
        initialize_database(v1_database)

    assert "broker_orders" in str(error.value)

    with connect(v1_database) as connection:
        assert get_schema_version(connection) == MIN_MIGRATABLE_SCHEMA_VERSION
        assert "order_intents" not in table_names(connection)
        assert "daily_risk_baselines" not in table_names(connection)
        # The pre-existing table and the v1 rows are both untouched.
        assert connection.execute("SELECT surprise FROM broker_orders").fetchall() == []
        assert len(list_strategy_runs(connection)) == 1


def test_a_failed_v2_to_v3_migration_rolls_back_completely(v2_database: Path) -> None:
    """The rebuild is the risky step, so its rollback is tested on its own.

    A conflicting `daily_risk_baselines` table makes v3 refuse *before* any
    table is renamed. The database must still be a working v2 afterwards, with
    every row and the original integer-quantity schema intact.
    """
    populate_v1_data(v2_database)
    populate_v2_order_data(v2_database)
    with connect(v2_database) as connection, transaction(connection):
        connection.execute("CREATE TABLE daily_risk_baselines (surprise TEXT)")

    with pytest.raises(DatabaseStateError) as error:
        initialize_database(v2_database)

    assert "daily_risk_baselines" in str(error.value)

    with connect(v2_database) as connection:
        assert get_schema_version(connection) == 2
        assert not set(state._PRE_V3_TABLES) & table_names(connection)
        # Untouched: still the integer-quantity v2 shape, with its rows.
        assert connection.execute("SELECT typeof(quantity) FROM positions").fetchone()[0] == (
            "integer"
        )
        assert connection.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM broker_orders").fetchone()[0] == 1
        assert len(list_strategy_runs(connection)) == 1
        assert connection.execute("SELECT surprise FROM daily_risk_baselines").fetchall() == []


def test_a_migration_that_fails_mid_rebuild_rolls_back(
    v2_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injected failure *after* the tables have been renamed aside.

    This is the state a partial migration would be most damaging in: the old
    tables renamed, the new ones created, the rows half-copied. SQLite's
    transactional DDL must undo all of it.
    """
    populate_v1_data(v2_database)
    populate_v2_order_data(v2_database)

    def explode(*args: object, **kwargs: object) -> str:
        raise RuntimeError("injected failure during the rebuild")

    monkeypatch.setattr(state, "_legacy_quantity_text", explode)

    with pytest.raises(RuntimeError):
        initialize_database(v2_database)

    with connect(v2_database) as connection:
        assert get_schema_version(connection) == 2
        assert not set(state._PRE_V3_TABLES) & table_names(connection)
        assert "daily_risk_baselines" not in table_names(connection)
        assert connection.execute("SELECT typeof(quantity) FROM positions").fetchone()[0] == (
            "integer"
        )
        assert connection.execute("SELECT COUNT(*) FROM broker_orders").fetchone()[0] == 1


def test_foreign_keys_are_restored_after_a_failed_migration(
    v2_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rebuild suspends referential enforcement; a failure must not leave it off."""

    def explode(*args: object, **kwargs: object) -> str:
        raise RuntimeError("injected failure during the rebuild")

    monkeypatch.setattr(state, "_legacy_quantity_text", explode)
    populate_v1_data(v2_database)
    populate_v2_order_data(v2_database)

    with pytest.raises(RuntimeError):
        initialize_database(v2_database)

    with connect(v2_database) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_a_migration_that_would_break_a_reference_is_refused(
    v2_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Referential integrity is re-checked before the migration commits."""
    populate_v1_data(v2_database)
    populate_v2_order_data(v2_database)

    real_migrate = state._migrate_v2_to_v3

    def migrate_then_orphan(connection: sqlite3.Connection) -> None:
        real_migrate(connection)
        connection.execute("DELETE FROM order_intents")

    monkeypatch.setattr(
        state, "_MIGRATIONS", ((2, state._migrate_v1_to_v2), (3, migrate_then_orphan))
    )

    with pytest.raises(DatabaseStateError) as error:
        initialize_database(v2_database)
    assert "foreign-key" in str(error.value)

    with connect(v2_database) as connection:
        assert get_schema_version(connection) == 2
        assert connection.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0] == 1


def test_a_newer_schema_version_is_still_refused_and_left_alone(database_path: Path) -> None:
    with connect(database_path) as connection, transaction(connection):
        connection.execute("UPDATE schema_metadata SET schema_version = ?", (SCHEMA_VERSION + 1,))

    with pytest.raises(UnsupportedSchemaVersionError):
        initialize_database(database_path)

    with connect(database_path) as connection:
        assert get_schema_version(connection) == SCHEMA_VERSION + 1


def test_a_version_below_the_migration_path_is_refused(v1_database: Path) -> None:
    with connect(v1_database) as connection, transaction(connection):
        connection.execute("UPDATE schema_metadata SET schema_version = 0")

    with pytest.raises(DatabaseStateError):
        initialize_database(v1_database)


def test_the_migration_rebuilds_only_the_tables_that_hold_quantities(
    v2_database: Path,
) -> None:
    """Every other v1/v2 object must come through byte-identical.

    A rebuild is a real cost - it drops and recreates a table - so it is spent
    only where it buys something: the three tables whose quantity columns
    cannot express a fractional coin. Everything else is left exactly alone,
    and this asserts that rather than trusting it.
    """
    rebuilt = {"positions", "order_intents", "broker_orders"}

    with connect(v2_database) as connection:
        before = {name: sql for _type, name, sql in schema_objects(connection)}

    initialize_database(v2_database)

    with connect(v2_database) as connection:
        after = {name: sql for _type, name, sql in schema_objects(connection)}

    untouched = {name: sql for name, sql in before.items() if name not in rebuilt}
    assert untouched.items() <= after.items()
    for name in rebuilt:
        assert after[name] != before[name], name
        assert "TEXT" in after[name]
    assert "daily_risk_baselines" in after


# --------------------------------------------------------------------------
# v3 -> v4: the reconciliation schema
# --------------------------------------------------------------------------


def test_a_version_three_database_migrates_through_four_to_the_current_version(
    v3_database: Path,
) -> None:
    """v3 -> v4 -> v5 in one pass, and the v4 step is not skipped on the way."""
    with connect(v3_database) as connection:
        assert get_schema_version(connection) == 3
        assert not (set(V4_TABLES) | set(V5_TABLES)) & table_names(connection)

    initialize_database(v3_database)

    with connect(v3_database) as connection:
        assert get_schema_version(connection) == SCHEMA_VERSION == 5
        assert set(V4_TABLES) <= table_names(connection)
        assert set(V5_TABLES) <= table_names(connection)


def test_the_v4_migration_carries_every_intent_across_unchanged(v3_database: Path) -> None:
    """Nothing in an intent changes. Only one more status becomes storable."""
    with connect(v3_database) as connection:
        record_order_intent(
            connection,
            client_order_id="autotrader-v3-1",
            created_at=T0,
            symbol="ETH/USD",
            side="BUY",
            requested_quantity=Decimal("0.50000"),
            approved_quantity=Decimal("0.25"),
            reference_price=3_000.0,
            risk_reason_code="POSITION_LIMIT",
            status="UNKNOWN",
        )
        before = list_order_intents(connection)

    initialize_database(v3_database)

    with connect(v3_database) as connection:
        assert list_order_intents(connection) == before


def test_a_v3_intent_status_that_v4_adds_was_previously_unstorable(
    v3_database: Path,
) -> None:
    """The rebuild is not cosmetic: the old CHECK constraint really did refuse it."""
    with (
        connect(v3_database) as connection,
        pytest.raises(sqlite3.IntegrityError),
        transaction(connection),
    ):
        connection.execute(
            "INSERT INTO order_intents (client_order_id, created_at, symbol, side, "
            "requested_quantity, approved_quantity, reference_price, risk_reason_code, "
            "status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "autotrader-v3-blocked",
                state.to_utc_text(T0),
                "BTC/USD",
                "BUY",
                "0.001",
                "0.001",
                100_000.0,
                "APPROVED",
                INTENT_STATUS_CONFIRMED_NOT_SUBMITTED,
                state.to_utc_text(T0),
            ),
        )


def test_the_new_intent_status_is_storable_after_the_migration(v3_database: Path) -> None:
    initialize_database(v3_database)

    with connect(v3_database) as connection:
        intent = record_order_intent(
            connection,
            client_order_id="autotrader-v4-1",
            created_at=T0,
            symbol="BTC/USD",
            side="BUY",
            requested_quantity=Decimal("0.001"),
            approved_quantity=Decimal("0.001"),
            reference_price=100_000.0,
            risk_reason_code="APPROVED",
            status=INTENT_STATUS_CONFIRMED_NOT_SUBMITTED,
        )
        stored = get_order_intent(connection, intent)

    assert stored is not None
    assert stored.status == INTENT_STATUS_CONFIRMED_NOT_SUBMITTED


def test_the_v4_migration_leaves_a_decimal_quantity_exact(v3_database: Path) -> None:
    with connect(v3_database) as connection:
        upsert_position(
            connection, symbol="BTC/USD", quantity=Decimal("0.000123456789"), updated_at=T0
        )

    initialize_database(v3_database)

    with connect(v3_database) as connection:
        position = get_position(connection, "BTC/USD")
    assert position is not None
    assert position.quantity == Decimal("0.000123456789")
    assert str(position.quantity) == "0.000123456789"


def test_the_v4_migration_rebuilds_only_order_intents(v3_database: Path) -> None:
    """A rebuild is a real cost, spent only where widening a CHECK requires it."""
    with connect(v3_database) as connection:
        before = {name: sql for _type, name, sql in schema_objects(connection)}

    initialize_database(v3_database)

    with connect(v3_database) as connection:
        after = {name: sql for _type, name, sql in schema_objects(connection)}

    untouched = {name: sql for name, sql in before.items() if name != "order_intents"}
    assert untouched.items() <= after.items()
    assert after["order_intents"] != before["order_intents"]
    assert INTENT_STATUS_CONFIRMED_NOT_SUBMITTED in after["order_intents"]
    assert set(V4_TABLES) <= set(after)


def test_a_failed_v3_to_v4_migration_rolls_back_completely(v3_database: Path) -> None:
    """A half-upgraded database is worse than an un-upgraded one."""
    with connect(v3_database) as connection, transaction(connection):
        connection.execute("CREATE TABLE reconciliation_runs (id INTEGER PRIMARY KEY)")

    with pytest.raises(DatabaseStateError):
        initialize_database(v3_database)

    with connect(v3_database) as connection:
        assert get_schema_version(connection) == 3
        assert not set(state._PRE_V4_TABLES) & table_names(connection)
        assert "order_intents" in table_names(connection)
        assert "reconciliation_events" not in table_names(connection)


def test_foreign_keys_and_wal_survive_the_v4_migration(v3_database: Path) -> None:
    initialize_database(v3_database)

    with connect(v3_database) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_broker_order_still_references_its_intent_after_the_v4_rebuild(
    v3_database: Path,
) -> None:
    """`order_intents` is renamed aside and recreated; the reference must follow."""
    with connect(v3_database) as connection:
        intent = record_order_intent(
            connection,
            client_order_id="autotrader-v3-fk",
            created_at=T0,
            symbol="BTC/USD",
            side="BUY",
            requested_quantity=Decimal("0.001"),
            approved_quantity=Decimal("0.001"),
            reference_price=100_000.0,
            risk_reason_code="APPROVED",
            status="SUBMITTED",
        )
        upsert_broker_order(
            connection,
            order_intent_id=intent,
            broker_order_id="broker-v3-fk",
            client_order_id="autotrader-v3-fk",
            symbol="BTC/USD",
            side="BUY",
            quantity=Decimal("0.001"),
            status="accepted",
            updated_at=T0,
        )

    initialize_database(v3_database)

    with connect(v3_database) as connection:
        stored = get_broker_order_by_intent(connection, intent)
        assert stored is not None and stored.broker_order_id == "broker-v3-fk"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError), transaction(connection):
            connection.execute(
                "INSERT INTO broker_orders (order_intent_id, broker_order_id, "
                "client_order_id, symbol, side, quantity, filled_quantity, status, "
                "updated_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    9_999,
                    "broker-orphan",
                    "autotrader-orphan",
                    "BTC/USD",
                    "BUY",
                    "0.001",
                    "0",
                    "accepted",
                    state.to_utc_text(T0),
                    state.to_utc_text(T0),
                ),
            )


def test_the_migration_backfills_no_reconciliation_history(v3_database: Path) -> None:
    """An invented audit trail would be worse than an empty one."""
    initialize_database(v3_database)

    with connect(v3_database) as connection:
        assert list_reconciliation_runs(connection) == []
        assert list_reconciliation_events(connection) == []


# --------------------------------------------------------------------------
# reconciliation_runs and reconciliation_events
# --------------------------------------------------------------------------


def record_clean_run(connection: sqlite3.Connection, **overrides: object) -> int:
    fields: dict[str, object] = {
        "started_at": T0,
        "completed_at": T0 + timedelta(seconds=2),
        "status": "CLEAN",
        "safe_to_trade": True,
        "orders_checked": 1,
        "positions_checked": 2,
        "issues_count": 0,
        "unresolved_count": 0,
    }
    fields.update(overrides)
    return record_reconciliation_run(connection, **fields)  # type: ignore[arg-type]


def test_a_reconciliation_run_round_trips(connection: sqlite3.Connection) -> None:
    run_id = record_clean_run(connection, status="REPAIRED", issues_count=3)

    stored = list_reconciliation_runs(connection)
    assert len(stored) == 1
    assert stored[0].id == run_id
    assert stored[0].status == "REPAIRED"
    assert stored[0].safe_to_trade is True
    assert stored[0].orders_checked == 1
    assert stored[0].positions_checked == 2
    assert stored[0].issues_count == 3
    assert stored[0].started_at == T0
    assert stored[0].completed_at == T0 + timedelta(seconds=2)


def test_the_latest_run_is_the_most_recent_one(connection: sqlite3.Connection) -> None:
    record_clean_run(connection)
    second = record_clean_run(connection, status="REPAIRED")

    latest = state.latest_reconciliation_run(connection)
    assert latest is not None and latest.id == second


def test_no_run_at_all_is_not_permission_to_trade(connection: sqlite3.Connection) -> None:
    assert state.latest_reconciliation_run(connection) is None


@pytest.mark.parametrize("status", ["CLEAN", "REPAIRED", "UNRESOLVED", "FAILED"])
def test_every_reconciliation_status_is_storable(
    connection: sqlite3.Connection, status: str
) -> None:
    assert record_clean_run(
        connection, status=status, safe_to_trade=status in {"CLEAN", "REPAIRED"}
    )


def test_an_unknown_reconciliation_status_is_rejected(connection: sqlite3.Connection) -> None:
    with pytest.raises(StateInputError):
        record_clean_run(connection, status="PROBABLY_FINE")


def test_the_database_itself_rejects_an_unknown_status(connection: sqlite3.Connection) -> None:
    """The CHECK constraint holds even for a writer that bypassed this module."""
    with pytest.raises(sqlite3.IntegrityError), transaction(connection):
        connection.execute(
            "INSERT INTO reconciliation_runs (started_at, completed_at, status, "
            "safe_to_trade, orders_checked, positions_checked, issues_count, "
            "unresolved_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                state.to_utc_text(T0),
                state.to_utc_text(T0),
                "PROBABLY_FINE",
                1,
                0,
                0,
                0,
                0,
                state.to_utc_text(T0),
            ),
        )


def test_safe_to_trade_can_only_be_zero_or_one(connection: sqlite3.Connection) -> None:
    """The one field a runtime consults cannot hold an unreadable third value."""
    with pytest.raises(sqlite3.IntegrityError), transaction(connection):
        connection.execute(
            "INSERT INTO reconciliation_runs (started_at, completed_at, status, "
            "safe_to_trade, orders_checked, positions_checked, issues_count, "
            "unresolved_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                state.to_utc_text(T0),
                state.to_utc_text(T0),
                "CLEAN",
                2,
                0,
                0,
                0,
                0,
                state.to_utc_text(T0),
            ),
        )


def test_a_run_that_finished_before_it_began_is_refused(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(StateInputError):
        record_clean_run(connection, completed_at=T0 - timedelta(seconds=1))


def test_more_unresolved_items_than_issues_is_refused(connection: sqlite3.Connection) -> None:
    """Every unresolved item is an issue, so it can never be the larger number."""
    with pytest.raises(StateInputError):
        record_clean_run(connection, issues_count=1, unresolved_count=2)


def test_a_negative_count_is_refused(connection: sqlite3.Connection) -> None:
    with pytest.raises(StateInputError):
        record_clean_run(connection, orders_checked=-1)


def test_a_non_boolean_safe_to_trade_is_refused(connection: sqlite3.Connection) -> None:
    with pytest.raises(StateInputError):
        record_clean_run(connection, safe_to_trade=1)


def test_a_reconciliation_event_round_trips(connection: sqlite3.Connection) -> None:
    run_id = record_clean_run(connection, status="REPAIRED", issues_count=1)

    record_reconciliation_event(
        connection,
        reconciliation_run_id=run_id,
        event_timestamp=T0,
        category="ORDER",
        outcome="REPAIRED",
        symbol="BTC/USD",
        client_order_id="autotrader-1",
        detail="repaired from broker truth",
    )

    events = list_reconciliation_events(connection, run_id)
    assert len(events) == 1
    assert events[0].category == "ORDER"
    assert events[0].outcome == "REPAIRED"
    assert events[0].symbol == "BTC/USD"
    assert events[0].client_order_id == "autotrader-1"
    assert events[0].detail == "repaired from broker truth"


def test_an_event_needs_a_real_run(connection: sqlite3.Connection) -> None:
    with pytest.raises(state.UnknownReconciliationRunError):
        record_reconciliation_event(
            connection,
            reconciliation_run_id=9_999,
            event_timestamp=T0,
            category="RUN",
            outcome="FAILED",
            detail="orphan",
        )


def test_an_event_that_says_nothing_is_refused(connection: sqlite3.Connection) -> None:
    run_id = record_clean_run(connection)

    with pytest.raises(StateInputError):
        record_reconciliation_event(
            connection,
            reconciliation_run_id=run_id,
            event_timestamp=T0,
            category="RUN",
            outcome="CLEAN",
            detail="   ",
        )


@pytest.mark.parametrize("category", ["ORDER", "POSITION", "RUN"])
def test_every_event_category_is_storable(connection: sqlite3.Connection, category: str) -> None:
    run_id = record_clean_run(connection)

    assert record_reconciliation_event(
        connection,
        reconciliation_run_id=run_id,
        event_timestamp=T0,
        category=category,
        outcome="OBSERVED",
        detail="something happened",
    )


def test_an_unknown_event_category_is_rejected(connection: sqlite3.Connection) -> None:
    run_id = record_clean_run(connection)

    with pytest.raises(StateInputError):
        record_reconciliation_event(
            connection,
            reconciliation_run_id=run_id,
            event_timestamp=T0,
            category="VIBES",
            outcome="OBSERVED",
            detail="something happened",
        )


def test_events_can_be_listed_for_one_run_only(connection: sqlite3.Connection) -> None:
    first = record_clean_run(connection, status="REPAIRED", issues_count=1)
    second = record_clean_run(connection, status="REPAIRED", issues_count=1)
    for run_id, detail in ((first, "first"), (second, "second")):
        record_reconciliation_event(
            connection,
            reconciliation_run_id=run_id,
            event_timestamp=T0,
            category="RUN",
            outcome="OBSERVED",
            detail=detail,
        )

    assert [event.detail for event in list_reconciliation_events(connection, second)] == ["second"]
    assert len(list_reconciliation_events(connection)) == 2


# --------------------------------------------------------------------------
# Tables Phase 8 owns must still not exist
# --------------------------------------------------------------------------


@pytest.mark.parametrize("forbidden", ["fills", "executions", "broker_accounts"])
def test_no_unearned_broker_table_exists(connection: sqlite3.Connection, forbidden: str) -> None:
    """Reconciliation arrived in v4; a fill-level history still has not.

    Order-level `filled_quantity` is what reconciliation actually settles, so
    these shapes would be guessed at rather than needed.
    """
    assert forbidden not in table_names(connection)


def test_the_schema_has_exactly_the_expected_tables(connection: sqlite3.Connection) -> None:
    assert table_names(connection) == set(REQUIRED_TABLES)


# --------------------------------------------------------------------------
# order_intents constraints
# --------------------------------------------------------------------------


def test_an_order_intent_round_trips(connection: sqlite3.Connection, intent_id: int) -> None:
    stored = get_order_intent(connection, intent_id)
    assert stored is not None
    assert stored.client_order_id == "autotrader-fixture-1"
    assert stored.symbol == "BTC/USD"
    assert stored.side == "BUY"
    assert stored.requested_quantity == Decimal("0.01")
    assert stored.approved_quantity == Decimal("0.0025")
    assert stored.reference_price == 104_000.0
    assert stored.risk_reason_code == "POSITION_LIMIT"
    assert stored.status == state.INTENT_STATUS_CREATED
    assert stored.created_at == T0
    assert stored.strategy_run_id is None


def test_an_intent_is_findable_by_its_client_order_id(
    connection: sqlite3.Connection, intent_id: int
) -> None:
    found = get_order_intent_by_client_id(connection, "autotrader-fixture-1")
    assert found is not None and found.id == intent_id
    assert get_order_intent_by_client_id(connection, "autotrader-nonexistent") is None


def test_a_duplicate_client_order_id_is_rejected(
    connection: sqlite3.Connection, intent_id: int
) -> None:
    """The UNIQUE constraint is what makes the idempotency key meaningful."""
    with pytest.raises(DuplicateOrderIntentError):
        record_order_intent(
            connection,
            client_order_id="autotrader-fixture-1",
            created_at=T0,
            symbol="ETH/USD",
            side="SELL",
            requested_quantity=Decimal(1),
            approved_quantity=Decimal(1),
            reference_price=400.0,
            risk_reason_code="APPROVED",
        )

    assert len(list_order_intents(connection)) == 1


def test_approved_quantity_may_not_exceed_requested_quantity(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(StateInputError):
        record_order_intent(
            connection,
            client_order_id="autotrader-oversized",
            created_at=T0,
            symbol="BTC/USD",
            side="BUY",
            requested_quantity=1,
            approved_quantity=2,
            reference_price=500.0,
            risk_reason_code="APPROVED",
        )


def test_the_database_itself_rejects_an_over_approved_intent(
    connection: sqlite3.Connection,
) -> None:
    """Enforced in SQL too, so bypassing Python cannot store an oversized order."""
    with pytest.raises(sqlite3.IntegrityError), transaction(connection):
        connection.execute(
            "INSERT INTO order_intents (client_order_id, created_at, symbol, side, "
            "requested_quantity, approved_quantity, reference_price, risk_reason_code, "
            "status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("raw-1", "t", "BTC/USD", "BUY", "1", "5", 100.0, "APPROVED", "CREATED", "t"),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested_quantity", Decimal(0)),
        ("requested_quantity", Decimal(-1)),
        ("requested_quantity", Decimal("-0.0001")),
        ("approved_quantity", Decimal(0)),
        ("approved_quantity", Decimal("-0.0003")),
        ("requested_quantity", 0.5),
        ("approved_quantity", 0.5),
        ("approved_quantity", Decimal("NaN")),
    ],
)
def test_an_unusable_quantity_is_rejected(
    connection: sqlite3.Connection, field: str, value: object
) -> None:
    """Zero, negative, non-finite, and float quantities are all refused."""
    payload = {
        "client_order_id": f"autotrader-{field}-{value}",
        "created_at": T0,
        "symbol": "BTC/USD",
        "side": "BUY",
        "requested_quantity": Decimal(5),
        "approved_quantity": Decimal(5),
        "reference_price": 500.0,
        "risk_reason_code": "APPROVED",
        field: value,
    }
    with pytest.raises(StateInputError):
        record_order_intent(connection, **payload)


def test_a_fractional_intent_quantity_round_trips_exactly(
    connection: sqlite3.Connection,
) -> None:
    intent = record_order_intent(
        connection,
        client_order_id="autotrader-fractional",
        created_at=T0,
        symbol="BTC/USD",
        side="BUY",
        requested_quantity=Decimal("0.5"),
        approved_quantity=Decimal("0.000123456789012345"),
        reference_price=104_000.0,
        risk_reason_code="POSITION_LIMIT",
    )

    stored = get_order_intent(connection, intent)
    assert stored is not None
    assert stored.requested_quantity == Decimal("0.5")
    assert stored.approved_quantity == Decimal("0.000123456789012345")
    assert isinstance(stored.approved_quantity, Decimal)


def test_the_database_rejects_a_fractionally_over_approved_intent(
    connection: sqlite3.Connection,
) -> None:
    """The CHECK compares numerically, so a fractional overshoot is caught too."""
    with pytest.raises(sqlite3.IntegrityError), transaction(connection):
        connection.execute(
            "INSERT INTO order_intents (client_order_id, created_at, symbol, side, "
            "requested_quantity, approved_quantity, reference_price, risk_reason_code, "
            "status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("raw-2", "t", "BTC/USD", "BUY", "0.001", "0.002", 100.0, "APPROVED", "CREATED", "t"),
        )


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
def test_an_unusable_reference_price_is_rejected(
    connection: sqlite3.Connection, price: float
) -> None:
    with pytest.raises(StateInputError):
        record_order_intent(
            connection,
            client_order_id=f"autotrader-price-{price}",
            created_at=T0,
            symbol="BTC/USD",
            side="BUY",
            requested_quantity=Decimal(1),
            approved_quantity=Decimal(1),
            reference_price=price,
            risk_reason_code="APPROVED",
        )


@pytest.mark.parametrize("side", ["SHORT", "buy", "", "SELL_SHORT"])
def test_only_buy_and_sell_are_storable_sides(connection: sqlite3.Connection, side: str) -> None:
    with pytest.raises(StateInputError):
        record_order_intent(
            connection,
            client_order_id=f"autotrader-side-{side}",
            created_at=T0,
            symbol="BTC/USD",
            side=side,
            requested_quantity=Decimal(1),
            approved_quantity=Decimal(1),
            reference_price=500.0,
            risk_reason_code="APPROVED",
        )


def test_an_unsupported_status_is_rejected(connection: sqlite3.Connection) -> None:
    with pytest.raises(StateInputError):
        record_order_intent(
            connection,
            client_order_id="autotrader-status",
            created_at=T0,
            symbol="BTC/USD",
            side="BUY",
            requested_quantity=Decimal(1),
            approved_quantity=Decimal(1),
            reference_price=500.0,
            risk_reason_code="APPROVED",
            status="FILLED",
        )


def test_an_intent_may_reference_a_strategy_run(connection: sqlite3.Connection) -> None:
    run_id = record_strategy_run(
        connection, strategy_name="EMA20/EMA50", mode="PAPER", started_at=T0
    )
    intent = record_order_intent(
        connection,
        client_order_id="autotrader-with-run",
        created_at=T0,
        symbol="BTC/USD",
        side="BUY",
        requested_quantity=Decimal(1),
        approved_quantity=Decimal(1),
        reference_price=500.0,
        risk_reason_code="APPROVED",
        strategy_run_id=run_id,
    )
    stored = get_order_intent(connection, intent)
    assert stored is not None and stored.strategy_run_id == run_id


def test_an_unknown_strategy_run_is_rejected(connection: sqlite3.Connection) -> None:
    with pytest.raises(UnknownStrategyRunError):
        record_order_intent(
            connection,
            client_order_id="autotrader-bad-run",
            created_at=T0,
            symbol="BTC/USD",
            side="BUY",
            requested_quantity=Decimal(1),
            approved_quantity=Decimal(1),
            reference_price=500.0,
            risk_reason_code="APPROVED",
            strategy_run_id=9999,
        )


def test_status_updates_never_change_the_client_order_id(
    connection: sqlite3.Connection, intent_id: int
) -> None:
    """The key must survive every transition; that is what makes it an anchor."""
    for status in (
        state.INTENT_STATUS_SUBMITTING,
        state.INTENT_STATUS_UNKNOWN,
        state.INTENT_STATUS_SUBMITTED,
    ):
        update_order_intent_status(
            connection, order_intent_id=intent_id, status=status, updated_at=T0 + STEP
        )
        stored = get_order_intent(connection, intent_id)
        assert stored is not None
        assert stored.status == status
        assert stored.client_order_id == "autotrader-fixture-1"


def test_updating_an_unknown_intent_is_reported(connection: sqlite3.Connection) -> None:
    with pytest.raises(UnknownOrderIntentError):
        update_order_intent_status(
            connection,
            order_intent_id=4242,
            status=state.INTENT_STATUS_SUBMITTED,
            updated_at=T0,
        )


# --------------------------------------------------------------------------
# broker_orders constraints
# --------------------------------------------------------------------------


def store_broker_order(connection: sqlite3.Connection, intent_id: int, **overrides: object) -> int:
    payload: dict[str, object] = {
        "order_intent_id": intent_id,
        "broker_order_id": "broker-1",
        "client_order_id": "autotrader-fixture-1",
        "symbol": "BTC/USD",
        "side": "BUY",
        "quantity": Decimal("0.0025"),
        "status": "accepted",
        "updated_at": T0,
        "submitted_at": T0,
    }
    payload.update(overrides)
    return upsert_broker_order(connection, **payload)  # type: ignore[arg-type]


def test_a_broker_order_round_trips(connection: sqlite3.Connection, intent_id: int) -> None:
    store_broker_order(connection, intent_id)

    stored = get_broker_order_by_intent(connection, intent_id)
    assert stored is not None
    assert stored.broker_order_id == "broker-1"
    assert stored.client_order_id == "autotrader-fixture-1"
    assert stored.quantity == Decimal("0.0025")
    assert stored.filled_quantity == 0
    assert stored.filled_average_price is None
    assert stored.status == "accepted"
    assert stored.submitted_at == T0
    assert stored.filled_at is None


def test_a_broker_order_is_findable_by_client_order_id(
    connection: sqlite3.Connection, intent_id: int
) -> None:
    store_broker_order(connection, intent_id)
    found = get_broker_order_by_client_id(connection, "autotrader-fixture-1")
    assert found is not None and found.broker_order_id == "broker-1"


def test_re_reading_the_same_broker_order_updates_the_snapshot(
    connection: sqlite3.Connection, intent_id: int
) -> None:
    """A later read refreshes the row; it does not append a second one."""
    store_broker_order(connection, intent_id)
    store_broker_order(
        connection,
        intent_id,
        status="filled",
        filled_quantity=Decimal("0.0025"),
        filled_average_price=501.25,
        filled_at=T0 + STEP,
        updated_at=T0 + STEP,
    )

    orders = list_broker_orders(connection)
    assert len(orders) == 1
    assert orders[0].status == "filled"
    assert orders[0].filled_quantity == Decimal("0.0025")
    assert orders[0].filled_average_price == 501.25


def test_one_broker_order_per_intent(connection: sqlite3.Connection, intent_id: int) -> None:
    store_broker_order(connection, intent_id)

    with pytest.raises(DuplicateBrokerOrderError):
        store_broker_order(
            connection, intent_id, broker_order_id="broker-2", client_order_id="autotrader-2"
        )

    assert len(list_broker_orders(connection)) == 1


def test_broker_order_id_is_unique_across_intents(
    connection: sqlite3.Connection, intent_id: int
) -> None:
    store_broker_order(connection, intent_id)
    other = record_order_intent(
        connection,
        client_order_id="autotrader-fixture-2",
        created_at=T0,
        symbol="ETH/USD",
        side="BUY",
        requested_quantity=Decimal(1),
        approved_quantity=Decimal(1),
        reference_price=400.0,
        risk_reason_code="APPROVED",
    )

    with pytest.raises(DuplicateBrokerOrderError):
        store_broker_order(
            connection, other, broker_order_id="broker-1", client_order_id="autotrader-fixture-2"
        )


def test_client_order_id_is_unique_across_broker_orders(
    connection: sqlite3.Connection, intent_id: int
) -> None:
    store_broker_order(connection, intent_id)
    other = record_order_intent(
        connection,
        client_order_id="autotrader-fixture-3",
        created_at=T0,
        symbol="ETH/USD",
        side="BUY",
        requested_quantity=Decimal(1),
        approved_quantity=Decimal(1),
        reference_price=400.0,
        risk_reason_code="APPROVED",
    )

    with pytest.raises(DuplicateBrokerOrderError):
        store_broker_order(
            connection, other, broker_order_id="broker-3", client_order_id="autotrader-fixture-1"
        )


def test_a_broker_order_needs_a_real_intent(connection: sqlite3.Connection) -> None:
    with pytest.raises(UnknownOrderIntentError):
        store_broker_order(connection, 9999)


def test_a_broker_order_snapshot_defaults_to_unfilled(
    connection: sqlite3.Connection, intent_id: int
) -> None:
    """Storing an accepted order must never imply a fill."""
    store_broker_order(connection, intent_id)
    stored = get_broker_order_by_intent(connection, intent_id)
    assert stored is not None
    assert stored.filled_quantity == 0
    assert stored.filled_average_price is None


@pytest.mark.parametrize(
    "quantity", [Decimal(0), Decimal(-1), Decimal("-0.0001"), 0.5, Decimal("NaN")]
)
def test_a_broker_order_quantity_must_be_a_positive_decimal(
    connection: sqlite3.Connection, intent_id: int, quantity: object
) -> None:
    with pytest.raises(StateInputError):
        store_broker_order(connection, intent_id, quantity=quantity)


@pytest.mark.parametrize("filled", [Decimal(-1), Decimal("-0.0000001"), 0.5])
def test_an_unusable_filled_quantity_is_rejected(
    connection: sqlite3.Connection, intent_id: int, filled: object
) -> None:
    with pytest.raises(StateInputError):
        store_broker_order(connection, intent_id, filled_quantity=filled)


def test_a_fractional_broker_fill_round_trips_exactly(
    connection: sqlite3.Connection, intent_id: int
) -> None:
    store_broker_order(
        connection,
        intent_id,
        quantity=Decimal("0.000123456789012345"),
        filled_quantity=Decimal("0.000000000000000001"),
        filled_average_price=104_123.45,
        status="filled",
    )

    stored = get_broker_order_by_intent(connection, intent_id)
    assert stored is not None
    assert stored.quantity == Decimal("0.000123456789012345")
    assert stored.filled_quantity == Decimal("0.000000000000000001")
    assert isinstance(stored.filled_quantity, Decimal)


def test_an_empty_broker_status_is_rejected(connection: sqlite3.Connection, intent_id: int) -> None:
    with pytest.raises(StateInputError):
        store_broker_order(connection, intent_id, status="  ")


def test_broker_status_text_is_stored_verbatim(
    connection: sqlite3.Connection, intent_id: int
) -> None:
    """The broker's vocabulary is opaque; this layer must not normalize it."""
    store_broker_order(connection, intent_id, status="pending_new")
    stored = get_broker_order_by_intent(connection, intent_id)
    assert stored is not None and stored.status == "pending_new"


def test_an_intent_without_a_broker_order_reports_none(
    connection: sqlite3.Connection, intent_id: int
) -> None:
    assert get_broker_order_by_intent(connection, intent_id) is None


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------


def test_the_state_module_still_imports_no_broker_client() -> None:
    """Adding order tables must not drag a trading client into persistence."""
    source = Path(state.__file__).read_text(encoding="utf-8")
    for token in ("alpaca", "TradingClient", "submit_order", "requests"):
        assert token not in source, token


def test_the_state_module_needs_no_credentials(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    record_order_intent(
        connection,
        client_order_id="autotrader-no-creds",
        created_at=T0,
        symbol="BTC/USD",
        side="BUY",
        requested_quantity=Decimal(1),
        approved_quantity=Decimal(1),
        reference_price=500.0,
        risk_reason_code="APPROVED",
    )
    assert len(list_order_intents(connection)) == 1


# --------------------------------------------------------------------------
# v4 -> v5: the durable runtime bar checkpoint
#
# The integration schema. It exists so a restarted 24/7 runtime cannot replay a
# completed bar its predecessor already acted on, and the thing these tests are
# really protecting is that adding it did not disturb anything Phase 8 wrote.
# --------------------------------------------------------------------------


def build_v4_database(path: Path) -> Path:
    """A database exactly as the Phase 8 (v4) release would have left it.

    Built from the module's own current statements minus the v5 table, so it is
    the real historical shape rather than an approximation of it.
    """
    with connect(path) as connection, transaction(connection):
        for statement in (
            state._CREATE_SCHEMA_METADATA,
            state._CREATE_STRATEGY_RUNS,
            state._CREATE_SIGNALS,
            state._CREATE_RISK_EVENTS,
            state._CREATE_SYSTEM_EVENTS,
            state._CREATE_POSITIONS,
            state._CREATE_ORDER_INTENTS,
            state._CREATE_BROKER_ORDERS,
            state._CREATE_DAILY_RISK_BASELINES,
            state._CREATE_RECONCILIATION_RUNS,
            state._CREATE_RECONCILIATION_EVENTS,
            state._CREATE_INDEX_SIGNALS,
            state._CREATE_INDEX_RISK_EVENTS,
            state._CREATE_INDEX_ORDER_INTENTS_STATUS,
            state._CREATE_INDEX_RECONCILIATION_EVENTS,
        ):
            connection.execute(statement)
        connection.execute(state._INSERT_SCHEMA_VERSION, (4, "2026-08-01T00:00:00.000000+00:00"))
    return path


@pytest.fixture
def v4_database(tmp_path: Path) -> Path:
    return build_v4_database(tmp_path / "v4.db")


def populate_v4_reconciliation_history(path: Path) -> tuple[int, int]:
    """One finished reconciliation run and one event, as Phase 8 would have written them."""
    with connect(path) as connection:
        run_id = record_reconciliation_run(
            connection,
            started_at=T0,
            completed_at=T0 + STEP,
            status="REPAIRED",
            safe_to_trade=True,
            orders_checked=3,
            positions_checked=2,
            issues_count=1,
            unresolved_count=0,
        )
        event_id = record_reconciliation_event(
            connection,
            reconciliation_run_id=run_id,
            event_timestamp=T0 + STEP,
            category="ORDER",
            outcome="REPAIRED",
            detail="accepted -> filled from verified broker truth",
            symbol="BTC/USD",
            client_order_id="autotrader-v4-history",
        )
    return run_id, event_id


def test_a_version_four_database_migrates_to_five(v4_database: Path) -> None:
    with connect(v4_database) as connection:
        assert get_schema_version(connection) == 4
        assert not set(V5_TABLES) & table_names(connection)

    initialize_database(v4_database)

    with connect(v4_database) as connection:
        assert get_schema_version(connection) == SCHEMA_VERSION == 5
        assert set(V5_TABLES) <= table_names(connection)
        assert set(REQUIRED_TABLES) <= table_names(connection)


def test_the_v5_migration_preserves_existing_reconciliation_runs(v4_database: Path) -> None:
    """Phase 8's audit history is not disturbed by adding a table beside it."""
    populate_v4_reconciliation_history(v4_database)
    with connect(v4_database) as connection:
        before = list_reconciliation_runs(connection)
    assert len(before) == 1

    initialize_database(v4_database)

    with connect(v4_database) as connection:
        assert list_reconciliation_runs(connection) == before


def test_the_v5_migration_preserves_existing_reconciliation_events(v4_database: Path) -> None:
    populate_v4_reconciliation_history(v4_database)
    with connect(v4_database) as connection:
        before = list_reconciliation_events(connection)
    assert len(before) == 1

    initialize_database(v4_database)

    with connect(v4_database) as connection:
        after = list_reconciliation_events(connection)
    assert after == before
    assert after[0].detail == "accepted -> filled from verified broker truth"
    assert after[0].client_order_id == "autotrader-v4-history"


def test_the_v5_migration_leaves_fractional_quantities_exact(v4_database: Path) -> None:
    """A crypto position is fractional, and an upgrade must not round it."""
    with connect(v4_database) as connection:
        upsert_position(
            connection, symbol="BTC/USD", quantity=Decimal("0.000123456789"), updated_at=T0
        )

    initialize_database(v4_database)

    with connect(v4_database) as connection:
        position = get_position(connection, "BTC/USD")
    assert position is not None
    assert position.quantity == Decimal("0.000123456789")
    assert str(position.quantity) == "0.000123456789"


def test_the_v5_migration_preserves_daily_risk_baselines(v4_database: Path) -> None:
    with connect(v4_database) as connection:
        ensure_daily_risk_baseline(
            connection,
            risk_date_utc=date(2026, 8, 26),
            baseline_equity=Decimal("200000"),
            captured_at=T0,
        )
        before = list_daily_risk_baselines(connection)
    assert len(before) == 1

    initialize_database(v4_database)

    with connect(v4_database) as connection:
        assert list_daily_risk_baselines(connection) == before


def test_the_v5_migration_adds_only_the_checkpoint_table(v4_database: Path) -> None:
    """Purely additive. No existing table is rebuilt, renamed, or reindexed."""
    with connect(v4_database) as connection:
        before = {name: sql for _type, name, sql in schema_objects(connection)}

    initialize_database(v4_database)

    with connect(v4_database) as connection:
        after = {name: sql for _type, name, sql in schema_objects(connection)}

    assert before.items() <= after.items(), "every pre-existing object is byte-identical"
    # The new table's TEXT PRIMARY KEY brings SQLite's implicit autoindex with
    # it; that belongs to the new table, not to anything that already existed.
    added = set(after) - set(before)
    assert added == set(V5_TABLES) | {"sqlite_autoindex_runtime_checkpoints_1"}


def test_a_failed_v4_to_v5_migration_rolls_back_completely(v4_database: Path) -> None:
    """A conflicting table refuses the upgrade, and leaves a working v4 behind."""
    populate_v4_reconciliation_history(v4_database)
    with connect(v4_database) as connection, transaction(connection):
        connection.execute("CREATE TABLE runtime_checkpoints (surprise TEXT)")

    with pytest.raises(DatabaseStateError) as error:
        initialize_database(v4_database)

    assert "runtime_checkpoints" in str(error.value)

    with connect(v4_database) as connection:
        assert get_schema_version(connection) == 4
        # The pre-existing table is untouched and the Phase 8 history survives.
        assert connection.execute("SELECT surprise FROM runtime_checkpoints").fetchall() == []
        assert len(list_reconciliation_runs(connection)) == 1
        assert len(list_reconciliation_events(connection)) == 1


def test_foreign_keys_and_wal_survive_the_v5_migration(v4_database: Path) -> None:
    initialize_database(v4_database)

    with connect(v4_database) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_database_newer_than_version_five_is_still_refused(database_path: Path) -> None:
    """Fail closed on the future: downgrading would discard data this code cannot read."""
    with connect(database_path) as connection, transaction(connection):
        connection.execute(state._UPDATE_SCHEMA_VERSION, (SCHEMA_VERSION + 1,))

    with pytest.raises(UnsupportedSchemaVersionError):
        initialize_database(database_path)

    with connect(database_path) as connection:
        assert get_schema_version(connection) == SCHEMA_VERSION + 1, "left untouched"


def test_a_version_one_database_migrates_all_the_way_to_five(v1_database: Path) -> None:
    """The whole chain in one transaction: v1 -> v2 -> v3 -> v4 -> v5."""
    populate_v1_data(v1_database)
    with connect(v1_database) as connection:
        assert get_schema_version(connection) == 1

    initialize_database(v1_database)

    with connect(v1_database) as connection:
        assert get_schema_version(connection) == SCHEMA_VERSION == 5
        assert set(REQUIRED_TABLES) <= table_names(connection)
        assert len(list_strategy_runs(connection)) == 1


# --------------------------------------------------------------------------
# Runtime checkpoints (schema v5)
# --------------------------------------------------------------------------


def test_a_checkpoint_timestamp_round_trips_exactly(connection: sqlite3.Connection) -> None:
    """A bar start is an identity, so microsecond drift would break the guard."""
    moment = datetime(2026, 8, 26, 10, 15, 0, 123456, tzinfo=UTC)
    upsert_runtime_checkpoint(
        connection,
        symbol="BTC/USD",
        last_processed_bar_timestamp=moment,
        updated_at=T0,
    )

    stored = get_runtime_checkpoint(connection, "BTC/USD")
    assert stored is not None
    assert stored.last_processed_bar_timestamp == moment
    assert stored.updated_at == T0


def test_a_repeated_checkpoint_for_the_same_bar_changes_nothing(
    connection: sqlite3.Connection,
) -> None:
    """One row per symbol, and claiming the same bar twice is not an error.

    A cycle that re-reads the newest completed bar must be able to say so
    without the storage layer treating it as a conflict.
    """
    upsert_runtime_checkpoint(
        connection, symbol="BTC/USD", last_processed_bar_timestamp=T0, updated_at=T0
    )
    upsert_runtime_checkpoint(
        connection, symbol="BTC/USD", last_processed_bar_timestamp=T0, updated_at=T0 + STEP
    )

    assert len(list_runtime_checkpoints(connection)) == 1
    stored = get_runtime_checkpoint(connection, "BTC/USD")
    assert stored is not None
    assert stored.last_processed_bar_timestamp == T0


def test_a_checkpoint_never_moves_backwards(connection: sqlite3.Connection) -> None:
    """Monotonic in SQL, so an out-of-order write cannot re-open a claimed bar."""
    upsert_runtime_checkpoint(
        connection, symbol="BTC/USD", last_processed_bar_timestamp=T0 + STEP, updated_at=T0
    )
    upsert_runtime_checkpoint(
        connection, symbol="BTC/USD", last_processed_bar_timestamp=T0, updated_at=T0 + 2 * STEP
    )

    stored = get_runtime_checkpoint(connection, "BTC/USD")
    assert stored is not None
    assert stored.last_processed_bar_timestamp == T0 + STEP


def test_each_symbol_keeps_its_own_checkpoint(connection: sqlite3.Connection) -> None:
    upsert_runtime_checkpoint(
        connection, symbol="BTC/USD", last_processed_bar_timestamp=T0, updated_at=T0
    )
    upsert_runtime_checkpoint(
        connection, symbol="ETH/USD", last_processed_bar_timestamp=T0 + STEP, updated_at=T0
    )

    checkpoints = {
        item.symbol: item.last_processed_bar_timestamp
        for item in list_runtime_checkpoints(connection)
    }
    assert checkpoints == {"BTC/USD": T0, "ETH/USD": T0 + STEP}


def test_an_unclaimed_symbol_reports_none(connection: sqlite3.Connection) -> None:
    assert get_runtime_checkpoint(connection, "ETH/USD") is None


def test_a_checkpoint_is_committed_and_visible_to_another_connection(
    database_path: Path,
) -> None:
    """The whole point of the table: the claim outlives the connection that made it."""
    with connect(database_path) as writer:
        upsert_runtime_checkpoint(
            writer, symbol="BTC/USD", last_processed_bar_timestamp=T0, updated_at=T0
        )

    with connect(database_path) as reader:
        stored = get_runtime_checkpoint(reader, "BTC/USD")
    assert stored is not None
    assert stored.last_processed_bar_timestamp == T0


def test_a_naive_checkpoint_timestamp_is_refused(connection: sqlite3.Connection) -> None:
    """There is no correct guess for a naive datetime's offset."""
    with pytest.raises(StateInputError):
        upsert_runtime_checkpoint(
            connection,
            symbol="BTC/USD",
            last_processed_bar_timestamp=datetime(2026, 8, 26, 10, 0),
            updated_at=T0,
        )
