"""Phase 7 schema tests: the v1 -> v2 migration and the two new tables.

Every test is offline and writes only into pytest's temporary directory. No
credential is read and no socket is opened - this is the persistence layer,
which has never talked to a broker and still does not.

The migration is the risky part of this phase: it runs against a database that
may already hold real operational history. These tests exist to prove it is
additive, transactional, idempotent, and that it leaves Phase 6 data alone.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from autotrader.state import sqlite as state
from autotrader.state.sqlite import (
    MIN_MIGRATABLE_SCHEMA_VERSION,
    REQUIRED_TABLES,
    SCHEMA_VERSION,
    V2_TABLES,
    DatabaseStateError,
    DuplicateBrokerOrderError,
    DuplicateOrderIntentError,
    StateInputError,
    UnknownOrderIntentError,
    UnknownStrategyRunError,
    UnsupportedSchemaVersionError,
    connect,
    get_broker_order_by_client_id,
    get_broker_order_by_intent,
    get_order_intent,
    get_order_intent_by_client_id,
    get_position,
    get_schema_version,
    get_strategy_run,
    initialize_database,
    list_broker_orders,
    list_order_intents,
    list_risk_events,
    list_signals,
    list_strategy_runs,
    record_order_intent,
    record_risk_event,
    record_signal,
    record_strategy_run,
    transaction,
    update_order_intent_status,
    upsert_broker_order,
    upsert_position,
)

T0 = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
STEP = timedelta(minutes=15)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def build_v1_database(path: Path) -> Path:
    """Create a database exactly as Phase 6 would have left it.

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


def populate_phase_six_data(path: Path) -> dict[str, object]:
    """Write one row into every Phase 6 table and describe what was written."""
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
        upsert_position(connection, symbol="SPY", quantity=7, average_price=101.5, updated_at=T0)
    return {"run_id": run_id}


@pytest.fixture
def v1_database(tmp_path: Path) -> Path:
    return build_v1_database(tmp_path / "v1.db")


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
        symbol="SPY",
        side="BUY",
        requested_quantity=10,
        approved_quantity=3,
        reference_price=500.0,
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


def test_a_new_database_initializes_directly_at_version_two(database_path: Path) -> None:
    """A fresh database is never created at v1 and then upgraded."""
    with connect(database_path) as connection:
        assert get_schema_version(connection) == 2
        assert set(REQUIRED_TABLES) <= table_names(connection)


def test_version_one_database_migrates_to_version_two(v1_database: Path) -> None:
    with connect(v1_database) as connection:
        assert get_schema_version(connection) == 1
        assert not set(V2_TABLES) & table_names(connection)

    initialize_database(v1_database)

    with connect(v1_database) as connection:
        assert get_schema_version(connection) == SCHEMA_VERSION
        assert set(V2_TABLES) <= table_names(connection)


def test_migrated_schema_is_identical_to_a_freshly_created_one(
    v1_database: Path, tmp_path: Path
) -> None:
    """ "Migrated to v2" and "created as v2" must be the same database.

    Both run the same statement list, so any drift between the two paths -
    a column added to one and not the other - shows up here rather than as a
    mysterious constraint failure months later.
    """
    initialize_database(v1_database)
    fresh = initialize_database(tmp_path / "fresh.db")

    with connect(v1_database) as migrated_connection, connect(fresh) as fresh_connection:
        assert schema_objects(migrated_connection) == schema_objects(fresh_connection)


def test_existing_phase_six_data_survives_the_migration(v1_database: Path) -> None:
    written = populate_phase_six_data(v1_database)

    initialize_database(v1_database)

    with connect(v1_database) as connection:
        assert get_schema_version(connection) == SCHEMA_VERSION
        assert get_strategy_run(connection, int(written["run_id"])) is not None
        assert len(list_strategy_runs(connection)) == 1
        assert len(list_signals(connection)) == 1
        assert len(list_risk_events(connection)) == 1

        position = get_position(connection, "SPY")
        assert position is not None
        assert position.quantity == 7
        assert position.average_price == 101.5


def test_migration_is_idempotent(v1_database: Path) -> None:
    populate_phase_six_data(v1_database)
    initialize_database(v1_database)

    with connect(v1_database) as connection:
        before = schema_objects(connection)

    initialize_database(v1_database)
    initialize_database(v1_database)

    with connect(v1_database) as connection:
        assert schema_objects(connection) == before
        assert get_schema_version(connection) == SCHEMA_VERSION
        assert len(list_strategy_runs(connection)) == 1


def test_a_failed_migration_rolls_back_completely(v1_database: Path) -> None:
    """A conflicting table must leave the database untouched on v1.

    SQLite DDL is transactional, so a migration that fails part-way must not
    leave one new table behind, a bumped version marker, or any other
    half-applied state.
    """
    populate_phase_six_data(v1_database)
    with connect(v1_database) as connection, transaction(connection):
        # Occupies one of the names the migration is about to create.
        connection.execute("CREATE TABLE broker_orders (surprise TEXT)")

    with pytest.raises(DatabaseStateError) as error:
        initialize_database(v1_database)

    assert "broker_orders" in str(error.value)

    with connect(v1_database) as connection:
        assert get_schema_version(connection) == MIN_MIGRATABLE_SCHEMA_VERSION
        assert "order_intents" not in table_names(connection)
        # The pre-existing table and the Phase 6 rows are both untouched.
        assert connection.execute("SELECT surprise FROM broker_orders").fetchall() == []
        assert len(list_strategy_runs(connection)) == 1


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


def test_migration_does_not_drop_or_recreate_a_phase_six_object(v1_database: Path) -> None:
    """Every v1 object must come through byte-identical, not rebuilt.

    Checked as a subset rather than an equality: the migration is allowed to
    *add* objects, and only forbidden to change or remove one that was already
    there. A dropped-and-recreated table would show up as a missing key or a
    differing `sql`.
    """
    with connect(v1_database) as connection:
        before = {name: sql for _type, name, sql in schema_objects(connection)}

    initialize_database(v1_database)

    with connect(v1_database) as connection:
        after = {name: sql for _type, name, sql in schema_objects(connection)}

    assert before.items() <= after.items()
    assert set(after) - set(before), "the migration should have added something"


# --------------------------------------------------------------------------
# Tables Phase 8 owns must still not exist
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden", ["fills", "executions", "broker_accounts", "reconciliation_runs"]
)
def test_no_reconciliation_table_exists(connection: sqlite3.Connection, forbidden: str) -> None:
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
    assert stored.symbol == "SPY"
    assert stored.side == "BUY"
    assert stored.requested_quantity == 10
    assert stored.approved_quantity == 3
    assert stored.reference_price == 500.0
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
            symbol="QQQ",
            side="SELL",
            requested_quantity=1,
            approved_quantity=1,
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
            symbol="SPY",
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
            ("raw-1", "t", "SPY", "BUY", 1, 5, 100.0, "APPROVED", "CREATED", "t"),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested_quantity", 0),
        ("requested_quantity", -1),
        ("approved_quantity", 0),
        ("approved_quantity", -3),
    ],
)
def test_non_positive_quantities_are_rejected(
    connection: sqlite3.Connection, field: str, value: int
) -> None:
    payload = {
        "client_order_id": f"autotrader-{field}-{value}",
        "created_at": T0,
        "symbol": "SPY",
        "side": "BUY",
        "requested_quantity": 5,
        "approved_quantity": 5,
        "reference_price": 500.0,
        "risk_reason_code": "APPROVED",
        field: value,
    }
    with pytest.raises(StateInputError):
        record_order_intent(connection, **payload)


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
def test_an_unusable_reference_price_is_rejected(
    connection: sqlite3.Connection, price: float
) -> None:
    with pytest.raises(StateInputError):
        record_order_intent(
            connection,
            client_order_id=f"autotrader-price-{price}",
            created_at=T0,
            symbol="SPY",
            side="BUY",
            requested_quantity=1,
            approved_quantity=1,
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
            symbol="SPY",
            side=side,
            requested_quantity=1,
            approved_quantity=1,
            reference_price=500.0,
            risk_reason_code="APPROVED",
        )


def test_an_unsupported_status_is_rejected(connection: sqlite3.Connection) -> None:
    with pytest.raises(StateInputError):
        record_order_intent(
            connection,
            client_order_id="autotrader-status",
            created_at=T0,
            symbol="SPY",
            side="BUY",
            requested_quantity=1,
            approved_quantity=1,
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
        symbol="SPY",
        side="BUY",
        requested_quantity=1,
        approved_quantity=1,
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
            symbol="SPY",
            side="BUY",
            requested_quantity=1,
            approved_quantity=1,
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
        "symbol": "SPY",
        "side": "BUY",
        "quantity": 3,
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
    assert stored.quantity == 3
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
        filled_quantity=3,
        filled_average_price=501.25,
        filled_at=T0 + STEP,
        updated_at=T0 + STEP,
    )

    orders = list_broker_orders(connection)
    assert len(orders) == 1
    assert orders[0].status == "filled"
    assert orders[0].filled_quantity == 3
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
        symbol="QQQ",
        side="BUY",
        requested_quantity=1,
        approved_quantity=1,
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
        symbol="QQQ",
        side="BUY",
        requested_quantity=1,
        approved_quantity=1,
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


@pytest.mark.parametrize("quantity", [0, -1])
def test_a_broker_order_quantity_must_be_positive(
    connection: sqlite3.Connection, intent_id: int, quantity: int
) -> None:
    with pytest.raises(StateInputError):
        store_broker_order(connection, intent_id, quantity=quantity)


def test_a_negative_filled_quantity_is_rejected(
    connection: sqlite3.Connection, intent_id: int
) -> None:
    with pytest.raises(StateInputError):
        store_broker_order(connection, intent_id, filled_quantity=-1)


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
        symbol="SPY",
        side="BUY",
        requested_quantity=1,
        approved_quantity=1,
        reference_price=500.0,
        risk_reason_code="APPROVED",
    )
    assert len(list_order_intents(connection)) == 1
