"""The realized-P&L accounting ledger: engine, store, ingestion, reconciliation.

Everything here runs offline. No credential, no network, no broker SDK call:
the engine is pure, the store is a temporary file, and the synchronizer takes
its two readers as arguments precisely so a test can be the broker.

The suite is organized as the ledger is: arithmetic first, then persistence,
then ingestion, then the properties that have to hold across all of them.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from autotrader.accounting import engine, ingest, readmodel, reconcile, store
from autotrader.accounting import models as accounting_models
from autotrader.accounting.models import (
    GRANULARITY_AGGREGATED_ORDER,
    GRANULARITY_EXECUTION,
    PROVENANCE_EQUITY_RUNTIME,
    PROVENANCE_MANUAL_OPERATOR,
    PROVENANCE_UNKNOWN_EXTERNAL,
    SIDE_BUY,
    SIDE_SELL,
    STATUS_MISMATCH,
    STATUS_TRACKING,
    AccountingInputError,
    CostBasisState,
    ExecutionFill,
    NegativeInventoryError,
    SymbolNotTrackedError,
)

T0 = datetime(2026, 9, 1, 14, 30, tzinfo=UTC)


def fill(
    execution_id: str,
    side: str,
    quantity: str,
    price: str,
    *,
    symbol: str = "SPY",
    at: datetime | None = None,
    fees: str = "0",
    order_id: str | None = None,
    granularity: str = GRANULARITY_EXECUTION,
    provenance: str = PROVENANCE_EQUITY_RUNTIME,
) -> ExecutionFill:
    return ExecutionFill(
        execution_id=execution_id,
        order_id=order_id or f"order-{execution_id}",
        symbol=symbol,
        asset_class="us_equity",
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        executed_at=at or T0,
        granularity=granularity,
        provenance=provenance,
        fees=Decimal(fees),
    )


# --------------------------------------------------------------------------
# The engine: buying
# --------------------------------------------------------------------------


def test_buy_from_flat_sets_quantity_and_basis() -> None:
    applied = engine.apply_fill(CostBasisState.flat("SPY"), fill("e1", SIDE_BUY, "10", "100"))

    assert applied.state.quantity == Decimal("10")
    assert applied.state.total_cost_basis == Decimal("1000")
    assert engine.average_cost(applied.state) == Decimal("100.0000000000")
    assert applied.realized is None


def test_buy_never_creates_a_realized_event() -> None:
    """A purchase releases nothing, so there is nothing to record about it."""
    state = CostBasisState.flat("SPY")
    for index, price in enumerate(("100", "50", "250", "1"), start=1):
        applied = engine.apply_fill(state, fill(f"e{index}", SIDE_BUY, "1", price))
        assert applied.realized is None
        state = applied.state


def test_buy_higher_raises_the_average_and_buy_lower_lowers_it() -> None:
    state = engine.apply_fill(CostBasisState.flat("SPY"), fill("e1", SIDE_BUY, "10", "100")).state

    higher = engine.apply_fill(state, fill("e2", SIDE_BUY, "10", "120")).state
    lower = engine.apply_fill(state, fill("e3", SIDE_BUY, "10", "80")).state

    assert engine.average_cost(higher) == Decimal("110.0000000000")
    assert engine.average_cost(lower) == Decimal("90.0000000000")


def test_fractional_buy_keeps_every_digit() -> None:
    applied = engine.apply_fill(
        CostBasisState.flat("SPY"), fill("e1", SIDE_BUY, "0.174703844", "593.152")
    )

    assert applied.state.quantity == Decimal("0.174703844")
    # Exact: one multiplication of two exact decimals, never rounded.
    assert applied.state.total_cost_basis == Decimal("0.174703844") * Decimal("593.152")


def test_buy_is_exactly_additive_over_many_fills() -> None:
    """The whole reason the total is stored and the average derived.

    A thousand fractional purchases accumulate no error at all, because a
    purchase is an addition of exact decimals with no division anywhere.
    """
    state = CostBasisState.flat("SPY")
    expected = Decimal(0)
    for index in range(1, 1001):
        quantity = Decimal("0.001") * index
        price = Decimal("123.456789")
        state = engine.apply_fill(
            state, fill(f"e{index}", SIDE_BUY, str(quantity), str(price))
        ).state
        expected += quantity * price

    assert state.total_cost_basis == expected


def test_attributable_buy_fees_enter_the_cost_basis() -> None:
    applied = engine.apply_fill(
        CostBasisState.flat("SPY"), fill("e1", SIDE_BUY, "10", "100", fees="2.50")
    )

    assert applied.state.total_cost_basis == Decimal("1002.50")
    assert engine.average_cost(applied.state) == Decimal("100.2500000000")


# --------------------------------------------------------------------------
# The engine: selling
# --------------------------------------------------------------------------


def test_partial_sell_at_a_profit() -> None:
    state = engine.apply_fill(CostBasisState.flat("SPY"), fill("e1", SIDE_BUY, "10", "100")).state

    applied = engine.apply_fill(state, fill("e2", SIDE_SELL, "4", "110"))
    realized = applied.realized

    assert realized is not None
    assert realized.released_cost_basis == Decimal("400.0000000000")
    assert realized.gross_proceeds == Decimal("440.0000000000")
    assert realized.gross_realized_pnl == Decimal("40.0000000000")
    assert realized.net_realized_pnl == Decimal("40.0000000000")
    assert applied.state.quantity == Decimal("6")


def test_partial_sell_at_a_loss() -> None:
    state = engine.apply_fill(CostBasisState.flat("SPY"), fill("e1", SIDE_BUY, "10", "100")).state

    realized = engine.apply_fill(state, fill("e2", SIDE_SELL, "4", "90")).realized

    assert realized is not None
    assert realized.gross_realized_pnl == Decimal("-40.0000000000")


def test_partial_sell_leaves_the_average_cost_untouched() -> None:
    """The defining property of weighted-average cost.

    Selling does not re-price what is left. An engine that recomputed the
    average from the remaining basis and quantity after a sale would drift, and
    the drift would look like a strategy result.
    """
    state = engine.apply_fill(CostBasisState.flat("SPY"), fill("e1", SIDE_BUY, "7", "315.11")).state
    before = engine.average_cost(state)

    for index, quantity in enumerate(("0.5", "1.25", "0.001", "2"), start=2):
        state = engine.apply_fill(state, fill(f"e{index}", SIDE_SELL, quantity, "999")).state
        assert engine.average_cost(state) == before


def test_selling_the_whole_position_leaves_nothing_behind() -> None:
    state = engine.apply_fill(CostBasisState.flat("SPY"), fill("e1", SIDE_BUY, "3", "766.43")).state

    applied = engine.apply_fill(state, fill("e2", SIDE_SELL, "3", "765.98"))

    assert applied.state.quantity == Decimal(0)
    assert applied.state.total_cost_basis == Decimal(0)
    assert applied.state.is_flat
    assert engine.average_cost(applied.state) is None


def test_full_exit_releases_exactly_the_remaining_basis() -> None:
    """No dust. The last sale takes the remainder by construction, not by luck."""
    state = engine.apply_fill(
        CostBasisState.flat("SPY"), fill("e1", SIDE_BUY, "1", "0.333333333333")
    ).state
    opening_basis = state.total_cost_basis

    trim = engine.apply_fill(state, fill("e2", SIDE_SELL, "0.333333333", "1"))
    exit_ = engine.apply_fill(trim.state, fill("e3", SIDE_SELL, "0.666666667", "1"))

    assert trim.realized is not None
    assert exit_.realized is not None
    released = trim.realized.released_cost_basis + exit_.realized.released_cost_basis
    assert released == opening_basis
    assert exit_.state.total_cost_basis == Decimal(0)


def test_multiple_sequential_trims_then_a_re_entry() -> None:
    """The drift-trim shape this book actually produces."""
    nvda = {"symbol": "NVDA"}
    state = engine.apply_fill(
        CostBasisState.flat("NVDA"), fill("e1", SIDE_BUY, "11", "219.64", **nvda)
    ).state
    state = engine.apply_fill(state, fill("e2", SIDE_SELL, "0.446966744", "223.074", **nvda)).state
    state = engine.apply_fill(state, fill("e3", SIDE_SELL, "0.620603768", "226.696", **nvda)).state
    assert engine.average_cost(state) == Decimal("219.6400000000")

    re_entry = engine.apply_fill(state, fill("e4", SIDE_BUY, "5", "230", **nvda))

    assert re_entry.state.quantity == Decimal("11") - Decimal("0.446966744") - Decimal(
        "0.620603768"
    ) + Decimal("5")
    assert engine.average_cost(re_entry.state) > Decimal("219.64")


def test_buy_sell_everything_then_buy_again_starts_a_fresh_basis() -> None:
    state = engine.apply_fill(CostBasisState.flat("SPY"), fill("e1", SIDE_BUY, "2", "100")).state
    state = engine.apply_fill(state, fill("e2", SIDE_SELL, "2", "120")).state
    assert engine.average_cost(state) is None

    state = engine.apply_fill(state, fill("e3", SIDE_BUY, "1", "50")).state

    assert engine.average_cost(state) == Decimal("50.0000000000")
    assert state.total_cost_basis == Decimal("50")


def test_sell_fees_reduce_net_but_not_gross() -> None:
    state = engine.apply_fill(CostBasisState.flat("SPY"), fill("e1", SIDE_BUY, "10", "100")).state

    realized = engine.apply_fill(state, fill("e2", SIDE_SELL, "5", "110", fees="1.25")).realized

    assert realized is not None
    assert realized.gross_realized_pnl == Decimal("50.0000000000")
    assert realized.fees == Decimal("1.25")
    assert realized.net_realized_pnl == Decimal("48.7500000000")


def test_zero_fees_are_stored_explicitly_not_omitted() -> None:
    state = engine.apply_fill(CostBasisState.flat("SPY"), fill("e1", SIDE_BUY, "10", "100")).state
    realized = engine.apply_fill(state, fill("e2", SIDE_SELL, "5", "110")).realized

    assert realized is not None
    assert realized.fees == Decimal(0)
    assert realized.net_realized_pnl == realized.gross_realized_pnl


def test_a_tiny_quantity_is_accounted_not_swallowed() -> None:
    state = engine.apply_fill(CostBasisState.flat("SPY"), fill("e1", SIDE_BUY, "1", "100")).state

    realized = engine.apply_fill(state, fill("e2", SIDE_SELL, "0.000000001", "200")).realized

    assert realized is not None
    assert realized.gross_realized_pnl == Decimal("0.0000001000")


# --------------------------------------------------------------------------
# The engine: refusals
# --------------------------------------------------------------------------


def test_selling_more_than_is_held_is_refused() -> None:
    state = engine.apply_fill(CostBasisState.flat("SPY"), fill("e1", SIDE_BUY, "2.5", "100")).state

    with pytest.raises(NegativeInventoryError) as error:
        engine.apply_fill(state, fill("e2", SIDE_SELL, "3.0", "100"))

    assert "long-only" in str(error.value)
    assert "2.5" in str(error.value)


def test_selling_from_flat_is_refused() -> None:
    with pytest.raises(NegativeInventoryError):
        engine.apply_fill(CostBasisState.flat("SPY"), fill("e1", SIDE_SELL, "1", "100"))


def test_a_stopped_symbol_accepts_nothing_further() -> None:
    state = engine.apply_fill(CostBasisState.flat("SPY"), fill("e1", SIDE_BUY, "1", "100")).state
    stopped = engine.mark_mismatch(state)

    with pytest.raises(SymbolNotTrackedError):
        engine.apply_fill(stopped, fill("e2", SIDE_BUY, "1", "100"))
    with pytest.raises(SymbolNotTrackedError):
        engine.apply_fill(stopped, fill("e3", SIDE_SELL, "1", "100"))


def test_marking_a_mismatch_preserves_the_numbers_that_disagreed() -> None:
    state = engine.apply_fill(CostBasisState.flat("SPY"), fill("e1", SIDE_BUY, "4", "100")).state

    stopped = engine.mark_mismatch(state)

    assert stopped.status == STATUS_MISMATCH
    assert stopped.quantity == state.quantity
    assert stopped.total_cost_basis == state.total_cost_basis


def test_a_fill_for_another_symbol_is_refused() -> None:
    with pytest.raises(SymbolNotTrackedError):
        engine.apply_fill(
            CostBasisState.flat("SPY"), fill("e1", SIDE_BUY, "1", "100", symbol="QQQ")
        )


def test_floats_are_refused_as_authoritative_figures() -> None:
    with pytest.raises(AccountingInputError):
        ExecutionFill(
            execution_id="e1",
            order_id="o1",
            symbol="SPY",
            asset_class="us_equity",
            side=SIDE_BUY,
            quantity=1.5,  # type: ignore[arg-type]
            price=Decimal("100"),
            executed_at=T0,
        )


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(AccountingInputError):
        ExecutionFill(
            execution_id="e1",
            order_id="o1",
            symbol="SPY",
            asset_class="us_equity",
            side=SIDE_BUY,
            quantity=Decimal("1"),
            price=Decimal("100"),
            executed_at=datetime(2026, 9, 1, 14, 30),
        )


def test_a_flat_state_carrying_a_basis_is_unrepresentable() -> None:
    with pytest.raises(AccountingInputError):
        CostBasisState(symbol="SPY", quantity=Decimal(0), total_cost_basis=Decimal("1"))


def test_a_negative_quantity_state_is_unrepresentable() -> None:
    with pytest.raises(AccountingInputError):
        CostBasisState(symbol="SPY", quantity=Decimal("-1"), total_cost_basis=Decimal("1"))


# --------------------------------------------------------------------------
# The engine: replay
# --------------------------------------------------------------------------


def test_replay_folds_a_sequence_and_ignores_repeats() -> None:
    fills = [
        fill("e1", SIDE_BUY, "10", "100"),
        fill("e2", SIDE_SELL, "4", "110"),
        fill("e1", SIDE_BUY, "10", "100"),
        fill("e2", SIDE_SELL, "4", "110"),
    ]

    states, events = engine.replay(fills)

    assert states["SPY"].quantity == Decimal("6")
    assert len(events) == 1


def test_replay_does_not_reorder_its_input() -> None:
    """Chronology is the ingestion layer's job to establish, not the fold's."""
    with pytest.raises(NegativeInventoryError):
        engine.replay([fill("e2", SIDE_SELL, "1", "110"), fill("e1", SIDE_BUY, "10", "100")])


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------


@pytest.fixture
def ledger(tmp_path: Path):
    path = tmp_path / "equity-accounting.db"
    with store.connect(path) as connection:
        store.initialize(connection)
        yield connection


def test_initialize_creates_the_schema_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    with store.connect(path) as connection:
        assert store.initialize(connection) == store.ACCOUNTING_SCHEMA_VERSION
        assert store.initialize(connection) == store.ACCOUNTING_SCHEMA_VERSION
        names = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    assert {
        "accounting_metadata",
        "accounting_fills",
        "realized_pnl_events",
        "position_cost_basis",
    } <= names


def test_the_accounting_store_has_its_own_schema_version() -> None:
    """It must not be the operational store's, or a bump there would migrate it."""
    from autotrader import state

    assert store.ACCOUNTING_SCHEMA_VERSION == 1
    assert store.ACCOUNTING_SCHEMA_VERSION != state.SCHEMA_VERSION


def test_a_newer_ledger_is_refused_rather_than_downgraded(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    with store.connect(path) as connection:
        store.initialize(connection)
        store.write_metadata(
            connection,
            tracking_started_at=T0,
            bootstrap_method="EXACT_REPLAY",
            historical_completeness="EXACT_REPLAY_FROM_ACCOUNT_OPEN",
            broker_account_fingerprint="abc",
            asset_class_scope="US_EQUITY",
            now=T0,
        )
        connection.execute("UPDATE accounting_metadata SET accounting_schema_version = 99")

        with pytest.raises(store.UnsupportedAccountingSchemaError):
            store.initialize(connection)


def test_recording_a_buy_writes_a_fill_and_a_basis_but_no_realized_row(
    ledger: sqlite3.Connection,
) -> None:
    recorded = store.record_fill(ledger, fill("e1", SIDE_BUY, "10", "100"), now=T0)

    assert recorded.duplicate is False
    assert recorded.accounting_event_id is not None
    assert ledger.execute("SELECT COUNT(*) FROM accounting_fills").fetchone()[0] == 1
    assert ledger.execute("SELECT COUNT(*) FROM realized_pnl_events").fetchone()[0] == 0
    assert store.read_cost_basis(ledger, "SPY").quantity == Decimal("10")


def test_recording_a_sell_writes_the_realized_row(ledger: sqlite3.Connection) -> None:
    store.record_fill(ledger, fill("e1", SIDE_BUY, "10", "100"), now=T0)
    store.record_fill(ledger, fill("e2", SIDE_SELL, "4", "110"), now=T0)

    row = ledger.execute("SELECT * FROM realized_pnl_events").fetchone()

    assert Decimal(row["net_realized_pnl"]) == Decimal("40.0000000000")
    assert row["realized_date_utc"] == "2026-09-01"
    assert Decimal(row["quantity_after"]) == Decimal("6")


def test_the_same_execution_recorded_twice_is_a_no_op(ledger: sqlite3.Connection) -> None:
    store.record_fill(ledger, fill("e1", SIDE_BUY, "10", "100"), now=T0)
    first = store.read_cost_basis(ledger, "SPY")

    again = store.record_fill(ledger, fill("e1", SIDE_BUY, "10", "100"), now=T0)

    assert again.duplicate is True
    assert store.read_cost_basis(ledger, "SPY") == first
    assert ledger.execute("SELECT COUNT(*) FROM accounting_fills").fetchone()[0] == 1


def test_the_execution_id_is_unique_at_the_database_level(ledger: sqlite3.Connection) -> None:
    """Not merely checked before insert - unstorable twice."""
    store.record_fill(ledger, fill("e1", SIDE_BUY, "10", "100"), now=T0)

    with pytest.raises(sqlite3.IntegrityError):
        ledger.execute(
            """
            INSERT INTO accounting_fills (
                idempotency_key, broker_execution_id, broker_order_id, symbol,
                asset_class, side, quantity, execution_price, fees, executed_at,
                execution_granularity, provenance, source, imported_at
            ) VALUES ('e1', 'e1', 'o', 'SPY', 'us_equity', 'BUY', '1', '1', '0',
                      '2026-09-01T00:00:00+00:00', 'EXECUTION', 'EQUITY_RUNTIME',
                      'BROKER_ACTIVITY', '2026-09-01T00:00:00+00:00')
            """
        )


def test_one_realized_row_per_fill_is_enforced_by_the_database(
    ledger: sqlite3.Connection,
) -> None:
    store.record_fill(ledger, fill("e1", SIDE_BUY, "10", "100"), now=T0)
    store.record_fill(ledger, fill("e2", SIDE_SELL, "4", "110"), now=T0)
    row = ledger.execute("SELECT * FROM realized_pnl_events").fetchone()

    with pytest.raises(sqlite3.IntegrityError):
        ledger.execute(
            "INSERT INTO realized_pnl_events (accounting_event_id, symbol, quantity, "
            "execution_price, average_cost_before, released_cost_basis, gross_proceeds, "
            "gross_realized_pnl, fees, net_realized_pnl, quantity_before, quantity_after, "
            "realized_at, realized_date_utc, provenance, accounting_version, created_at) "
            "VALUES (?, 'SPY', '1', '1', '1', '1', '1', '0', '0', '0', '1', '0', "
            "'2026-09-01T00:00:00+00:00', '2026-09-01', 'EQUITY_RUNTIME', 1, "
            "'2026-09-01T00:00:00+00:00')",
            (int(row["accounting_event_id"]),),
        )


def test_a_refused_sale_stores_no_fill_and_stops_the_symbol(ledger: sqlite3.Connection) -> None:
    store.record_fill(ledger, fill("e1", SIDE_BUY, "2.5", "100"), now=T0)

    refused = store.record_fill(ledger, fill("e2", SIDE_SELL, "3", "100"), now=T0)

    assert refused.refused is not None
    assert refused.accounting_event_id is None
    assert ledger.execute("SELECT COUNT(*) FROM accounting_fills").fetchone()[0] == 1
    state = store.read_cost_basis(ledger, "SPY")
    assert state.status == STATUS_MISMATCH
    assert state.quantity == Decimal("2.5")


def test_a_stopped_symbol_does_not_resume_on_the_next_fill(ledger: sqlite3.Connection) -> None:
    store.record_fill(ledger, fill("e1", SIDE_BUY, "1", "100"), now=T0)
    store.record_fill(ledger, fill("e2", SIDE_SELL, "3", "100"), now=T0)

    with pytest.raises(SymbolNotTrackedError):
        store.record_fill(ledger, fill("e3", SIDE_BUY, "1", "100"), now=T0)


def test_a_failed_write_leaves_no_fill_behind(ledger: sqlite3.Connection, monkeypatch) -> None:
    """The fill and its consequences are one transaction, or they are nothing."""
    store.record_fill(ledger, fill("e1", SIDE_BUY, "10", "100"), now=T0)
    before = store.read_cost_basis(ledger, "SPY")

    real_write = store._write_cost_basis

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk went away mid-write")

    monkeypatch.setattr(store, "_write_cost_basis", explode)
    with pytest.raises(RuntimeError):
        store.record_fill(ledger, fill("e2", SIDE_SELL, "4", "110"), now=T0)
    monkeypatch.setattr(store, "_write_cost_basis", real_write)

    assert ledger.execute("SELECT COUNT(*) FROM accounting_fills").fetchone()[0] == 1
    assert ledger.execute("SELECT COUNT(*) FROM realized_pnl_events").fetchone()[0] == 0
    assert store.read_cost_basis(ledger, "SPY") == before


def test_state_survives_reopening_the_database(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    with store.connect(path) as connection:
        store.initialize(connection)
        store.record_fill(connection, fill("e1", SIDE_BUY, "10", "100"), now=T0)
        store.record_fill(connection, fill("e2", SIDE_SELL, "4", "110.5"), now=T0)

    with store.connect(path) as connection:
        state = store.read_cost_basis(connection, "SPY")
        events = connection.execute("SELECT COUNT(*) FROM realized_pnl_events").fetchone()[0]

    assert state.quantity == Decimal("6")
    assert state.total_cost_basis == Decimal("600.0000000000")
    assert state.last_execution_id == "e2"
    assert events == 1


def test_decimals_round_trip_exactly_through_the_store(tmp_path: Path) -> None:
    quantity = Decimal("29.796669247")
    price = Decimal("219.924363")
    path = tmp_path / "ledger.db"
    with store.connect(path) as connection:
        store.initialize(connection)
        store.record_fill(connection, fill("e1", SIDE_BUY, str(quantity), str(price)), now=T0)

    with store.connect_read_only(path) as connection:
        row = connection.execute("SELECT * FROM accounting_fills").fetchone()
        state = store.read_cost_basis(connection, "SPY")

    assert Decimal(row["quantity"]) == quantity
    assert Decimal(row["execution_price"]) == price
    assert state.total_cost_basis == quantity * price


def test_decimal_text_never_uses_exponent_notation() -> None:
    assert store.decimal_text(Decimal("0E-9")) == "0.000000000"
    assert store.decimal_text(Decimal("1E-9")) == "0.000000001"


def test_the_read_only_connection_cannot_write(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    with store.connect(path) as connection:
        store.initialize(connection)

    with store.connect_read_only(path) as connection, pytest.raises(sqlite3.OperationalError):
        connection.execute("DELETE FROM accounting_fills")


def test_an_aggregated_fill_is_labelled_and_carries_no_execution_id(
    ledger: sqlite3.Connection,
) -> None:
    """The mode for a broker that reports only order totals. Not this deployment."""
    store.record_fill(
        ledger,
        fill("order-7", SIDE_BUY, "3", "100", granularity=GRANULARITY_AGGREGATED_ORDER),
        now=T0,
    )

    row = ledger.execute("SELECT * FROM accounting_fills").fetchone()

    assert row["execution_granularity"] == GRANULARITY_AGGREGATED_ORDER
    assert row["broker_execution_id"] is None
    assert row["idempotency_key"] == "order-7"


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------


class Execution:
    def __init__(
        self,
        activity_id: str,
        order_id: str,
        symbol: str,
        side: str,
        quantity: str,
        price: str,
        at: datetime,
    ) -> None:
        self.activity_id = activity_id
        self.broker_order_id = order_id
        self.symbol = symbol
        self.side = side
        self.quantity = Decimal(quantity)
        self.price = Decimal(price)
        self.transaction_time = at


class Order:
    def __init__(self, order_id: str, client_order_id: str, asset_class: str) -> None:
        self.broker_order_id = order_id
        self.client_order_id = client_order_id
        self.asset_class = asset_class


class Position:
    def __init__(self, symbol: str, quantity: str, average: str) -> None:
        self.symbol = symbol
        self.quantity = Decimal(quantity)
        self.average_entry_price = Decimal(average)


def readers(executions: list[Execution], orders: list[Order]):
    calls = {"executions": 0, "orders": 0}

    def read_executions(after: datetime | None):
        calls["executions"] += 1
        rows = [e for e in executions if after is None or e.transaction_time > after]
        return rows, 1

    def read_orders(after: datetime | None):
        calls["orders"] += 1
        return list(orders), 1

    return read_executions, read_orders, calls


def test_ingestion_imports_equity_and_skips_crypto(ledger: sqlite3.Connection) -> None:
    executions = [
        Execution("a1", "o1", "SPY", "BUY", "3", "100", T0),
        Execution("a2", "o2", "BTC/USD", "BUY", "0.5", "70000", T0),
    ]
    orders = [Order("o1", "autotrader-1", "us_equity"), Order("o2", "autotrader-2", "crypto")]
    read_executions, read_orders, _ = readers(executions, orders)

    result = ingest.synchronize(
        ledger,
        read_executions=read_executions,
        read_orders=read_orders,
        runtime_store_path=None,
        now=T0,
    )

    assert result.executions_imported == 1
    assert result.out_of_scope_skipped == 1
    assert store.read_cost_basis(ledger, "BTC/USD").is_flat


def test_an_execution_whose_order_cannot_be_read_is_not_accounted(
    ledger: sqlite3.Connection,
) -> None:
    """Asset class is looked up, never inferred from the ticker."""
    read_executions, read_orders, _ = readers(
        [Execution("a1", "missing", "SPY", "BUY", "3", "100", T0)], []
    )

    result = ingest.synchronize(
        ledger,
        read_executions=read_executions,
        read_orders=read_orders,
        runtime_store_path=None,
        now=T0,
    )

    assert result.executions_imported == 0
    assert result.unresolved_orders == 1
    assert result.status == ingest.SYNC_PARTIAL


def test_ingestion_sorts_by_time_then_execution_id(ledger: sqlite3.Connection) -> None:
    """Same microsecond, two executions: the order must not depend on the page."""
    same = T0 + timedelta(hours=1)
    executions = [
        Execution("a3", "o1", "SPY", "SELL", "1", "120", same),
        Execution("a1", "o1", "SPY", "BUY", "2", "100", T0),
        Execution("a2", "o1", "SPY", "BUY", "2", "110", same),
    ]
    read_executions, read_orders, _ = readers(executions, [Order("o1", "c1", "us_equity")])

    ingest.synchronize(
        ledger,
        read_executions=read_executions,
        read_orders=read_orders,
        runtime_store_path=None,
        now=T0,
    )

    keys = [
        str(row["idempotency_key"])
        for row in ledger.execute(
            "SELECT idempotency_key FROM accounting_fills ORDER BY accounting_event_id"
        )
    ]
    assert keys == ["a1", "a2", "a3"]


def test_out_of_order_arrival_produces_the_same_ledger(tmp_path: Path) -> None:
    executions = [
        Execution("a1", "o1", "SPY", "BUY", "10", "100", T0),
        Execution("a2", "o1", "SPY", "BUY", "5", "120", T0 + timedelta(minutes=5)),
        Execution("a3", "o2", "SPY", "SELL", "4", "130", T0 + timedelta(minutes=10)),
    ]
    orders = [Order("o1", "c1", "us_equity"), Order("o2", "c2", "us_equity")]

    def build(rows: list[Execution]) -> tuple[Decimal, Decimal, list[tuple]]:
        path = tmp_path / f"ledger-{len(rows)}-{rows[0].activity_id}.db"
        read_executions, read_orders, _ = readers(rows, orders)
        with store.connect(path) as connection:
            store.initialize(connection)
            ingest.synchronize(
                connection,
                read_executions=read_executions,
                read_orders=read_orders,
                runtime_store_path=None,
                now=T0,
            )
            state = store.read_cost_basis(connection, "SPY")
            events = [
                tuple(str(value) for value in row)
                for row in connection.execute(
                    "SELECT symbol, quantity, released_cost_basis, net_realized_pnl "
                    "FROM realized_pnl_events ORDER BY realized_at"
                )
            ]
        return state.quantity, state.total_cost_basis, events

    forward = build(executions)
    backward = build(list(reversed(executions)))

    assert forward == backward


def test_re_running_a_sync_imports_nothing_and_changes_nothing(
    ledger: sqlite3.Connection,
) -> None:
    executions = [
        Execution("a1", "o1", "SPY", "BUY", "10", "100", T0),
        Execution("a2", "o2", "SPY", "SELL", "4", "110", T0 + timedelta(minutes=1)),
    ]
    orders = [Order("o1", "c1", "us_equity"), Order("o2", "c2", "us_equity")]
    read_executions, read_orders, _ = readers(executions, orders)

    first = ingest.synchronize(
        ledger,
        read_executions=read_executions,
        read_orders=read_orders,
        runtime_store_path=None,
        now=T0,
    )
    before = store.read_cost_basis(ledger, "SPY")
    second = ingest.synchronize(
        ledger,
        read_executions=read_executions,
        read_orders=read_orders,
        runtime_store_path=None,
        now=T0,
    )

    assert first.executions_imported == 2
    assert second.executions_imported == 0
    assert second.duplicates_skipped == 2
    assert second.realized_events == 0
    assert store.read_cost_basis(ledger, "SPY") == before
    assert ledger.execute("SELECT COUNT(*) FROM realized_pnl_events").fetchone()[0] == 1


def test_the_overlap_window_re_reads_before_the_high_water_mark(
    ledger: sqlite3.Connection,
) -> None:
    """A late execution inside the overlap is caught, not lost.

    A cursor that asked only for strictly-newer executions would never see
    `a-late`, and nothing downstream would ever notice.
    """
    late = T0 + timedelta(hours=1)
    newest = T0 + timedelta(hours=2)
    orders = [Order("o1", "c1", "us_equity")]
    read_executions, read_orders, _ = readers(
        [Execution("a-newest", "o1", "SPY", "BUY", "1", "100", newest)], orders
    )
    ingest.synchronize(
        ledger,
        read_executions=read_executions,
        read_orders=read_orders,
        runtime_store_path=None,
        now=T0,
    )

    read_executions, read_orders, _ = readers(
        [
            Execution("a-newest", "o1", "SPY", "BUY", "1", "100", newest),
            Execution("a-late", "o1", "SPY", "BUY", "2", "50", late),
        ],
        orders,
    )
    result = ingest.synchronize(
        ledger,
        read_executions=read_executions,
        read_orders=read_orders,
        runtime_store_path=None,
        now=T0,
        overlap=timedelta(days=2),
    )

    assert result.executions_imported == 1
    assert store.read_cost_basis(ledger, "SPY").quantity == Decimal("3")


def test_a_refused_execution_makes_the_pass_partial(ledger: sqlite3.Connection) -> None:
    executions = [
        Execution("a1", "o1", "SPY", "BUY", "1", "100", T0),
        Execution("a2", "o1", "SPY", "SELL", "5", "100", T0 + timedelta(minutes=1)),
    ]
    read_executions, read_orders, _ = readers(executions, [Order("o1", "c1", "us_equity")])

    result = ingest.synchronize(
        ledger,
        read_executions=read_executions,
        read_orders=read_orders,
        runtime_store_path=None,
        now=T0,
    )

    assert result.status == ingest.SYNC_PARTIAL
    assert len(result.refusals) == 1
    assert store.read_cost_basis(ledger, "SPY").status == STATUS_MISMATCH


def test_every_pass_writes_a_run_record(ledger: sqlite3.Connection) -> None:
    read_executions, read_orders, _ = readers([], [])

    ingest.synchronize(
        ledger,
        read_executions=read_executions,
        read_orders=read_orders,
        runtime_store_path=None,
        now=T0,
    )

    row = store.latest_sync_run(ledger)
    assert row is not None
    assert row["status"] == ingest.SYNC_OK
    assert int(row["broker_requests"]) == 2


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


def test_an_order_in_the_runtime_store_is_runtime_originated() -> None:
    assert (
        ingest.classify_provenance(
            "o1", runtime_ids=frozenset({"o1"}), client_order_id="autotrader-1"
        )
        == PROVENANCE_EQUITY_RUNTIME
    )


def test_a_system_minted_order_in_no_store_is_operator_run() -> None:
    assert (
        ingest.classify_provenance(
            "o9", runtime_ids=frozenset({"o1"}), client_order_id="autotrader-9"
        )
        == PROVENANCE_MANUAL_OPERATOR
    )


def test_an_unrecognized_order_is_unknown_never_attributed() -> None:
    assert (
        ingest.classify_provenance("o9", runtime_ids=frozenset(), client_order_id="someone-else")
        == PROVENANCE_UNKNOWN_EXTERNAL
    )
    assert (
        ingest.classify_provenance("o9", runtime_ids=frozenset(), client_order_id=None)
        == PROVENANCE_UNKNOWN_EXTERNAL
    )


def test_runtime_order_ids_reads_a_foreign_store_without_writing(tmp_path: Path) -> None:
    """The runtime store is opened read-only and never initialized."""
    path = tmp_path / "equity-paper.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE broker_orders (broker_order_id TEXT)")
    connection.execute("INSERT INTO broker_orders VALUES ('o1'), ('o2'), (NULL)")
    connection.commit()
    connection.close()
    before = path.stat().st_mtime_ns

    assert ingest.runtime_order_ids(path) == frozenset({"o1", "o2"})
    assert path.stat().st_mtime_ns == before


def test_a_missing_runtime_store_is_not_an_error(tmp_path: Path) -> None:
    assert ingest.runtime_order_ids(tmp_path / "absent.db") == frozenset()


def test_manual_broker_fills_are_accounted_and_attributed(ledger: sqlite3.Connection) -> None:
    """A broker-confirmed trade the runtime did not place still moves the book."""
    executions = [
        Execution("a1", "o-runtime", "SPY", "BUY", "10", "100", T0),
        Execution("a2", "o-manual", "SPY", "SELL", "1", "120", T0 + timedelta(minutes=1)),
    ]
    orders = [
        Order("o-runtime", "autotrader-1", "us_equity"),
        Order("o-manual", "autotrader-hand", "us_equity"),
    ]
    read_executions, read_orders, _ = readers(executions, orders)

    def runtime_ids(_path: object) -> frozenset[str]:
        return frozenset({"o-runtime"})

    original = ingest.runtime_order_ids
    ingest.runtime_order_ids = runtime_ids  # type: ignore[assignment]
    try:
        ingest.synchronize(
            ledger,
            read_executions=read_executions,
            read_orders=read_orders,
            runtime_store_path="anything",
            now=T0,
        )
    finally:
        ingest.runtime_order_ids = original  # type: ignore[assignment]

    provenances = {
        str(row["idempotency_key"]): str(row["provenance"])
        for row in ledger.execute("SELECT idempotency_key, provenance FROM accounting_fills")
    }
    assert provenances == {"a1": PROVENANCE_EQUITY_RUNTIME, "a2": PROVENANCE_MANUAL_OPERATOR}
    assert store.read_cost_basis(ledger, "SPY").quantity == Decimal("9")
    realized = ledger.execute("SELECT provenance FROM realized_pnl_events").fetchone()
    assert realized["provenance"] == PROVENANCE_MANUAL_OPERATOR


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


def test_matching_quantities_and_averages_are_clean(ledger: sqlite3.Connection) -> None:
    store.record_fill(ledger, fill("e1", SIDE_BUY, "10", "100"), now=T0)

    result = reconcile.reconcile(ledger, {"SPY": Position("SPY", "10", "100")}, now=T0)

    assert result.status == store.RECON_CLEAN
    assert result.quantity_mismatches == 0


def test_a_quantity_difference_is_a_mismatch(ledger: sqlite3.Connection) -> None:
    store.record_fill(ledger, fill("e1", SIDE_BUY, "10", "100"), now=T0)

    result = reconcile.reconcile(ledger, {"SPY": Position("SPY", "9", "100")}, now=T0)

    assert result.status == store.RECON_MISMATCH
    assert result.quantity_mismatches == 1


def test_an_average_cost_difference_inside_broker_precision_is_clean(
    ledger: sqlite3.Connection,
) -> None:
    store.record_fill(ledger, fill("e1", SIDE_BUY, "3", "322.477677981"), now=T0)

    result = reconcile.reconcile(ledger, {"SPY": Position("SPY", "3", "322.477678")}, now=T0)

    assert result.status == store.RECON_CLEAN


def test_an_average_cost_difference_beyond_tolerance_is_degraded(
    ledger: sqlite3.Connection,
) -> None:
    store.record_fill(ledger, fill("e1", SIDE_BUY, "3", "100"), now=T0)

    result = reconcile.reconcile(ledger, {"SPY": Position("SPY", "3", "100.5")}, now=T0)

    assert result.status == store.RECON_DEGRADED
    assert result.cost_deviations == 1
    assert result.quantity_mismatches == 0


def test_a_position_the_ledger_has_never_heard_of_is_a_mismatch(
    ledger: sqlite3.Connection,
) -> None:
    result = reconcile.reconcile(ledger, {"QQQ": Position("QQQ", "5", "700")}, now=T0)

    assert result.status == store.RECON_MISMATCH
    assert result.symbols[0].local_quantity == Decimal(0)


def test_a_stopped_symbol_reconciles_as_mismatch_even_when_quantities_agree(
    ledger: sqlite3.Connection,
) -> None:
    store.record_fill(ledger, fill("e1", SIDE_BUY, "10", "100"), now=T0)
    store.mark_symbol_mismatch(ledger, "SPY", now=T0)

    result = reconcile.reconcile(ledger, {"SPY": Position("SPY", "10", "100")}, now=T0)

    assert result.status == store.RECON_MISMATCH


def test_an_unreadable_broker_is_unknown_never_clean(ledger: sqlite3.Connection) -> None:
    store.record_fill(ledger, fill("e1", SIDE_BUY, "10", "100"), now=T0)

    result = reconcile.reconcile(ledger, None, now=T0)

    assert result.status == store.RECON_UNKNOWN


def test_reconciliation_never_edits_the_ledger(ledger: sqlite3.Connection) -> None:
    store.record_fill(ledger, fill("e1", SIDE_BUY, "10", "100"), now=T0)
    before = store.read_cost_basis(ledger, "SPY")

    reconcile.reconcile(ledger, {"SPY": Position("SPY", "3", "999")}, now=T0)

    assert store.read_cost_basis(ledger, "SPY") == before


def test_every_reconciliation_leaves_an_audit_row(ledger: sqlite3.Connection) -> None:
    store.record_fill(ledger, fill("e1", SIDE_BUY, "10", "100"), now=T0)

    reconcile.reconcile(ledger, {"SPY": Position("SPY", "10", "100")}, now=T0)

    run = reconcile.latest(ledger)
    assert run is not None and run["status"] == store.RECON_CLEAN
    assert [row["symbol"] for row in reconcile.latest_symbols(ledger)] == ["SPY"]


# --------------------------------------------------------------------------
# Read model
# --------------------------------------------------------------------------


def _seed(connection: sqlite3.Connection) -> None:
    store.record_fill(connection, fill("e1", SIDE_BUY, "10", "100"), now=T0)
    store.record_fill(
        connection, fill("e2", SIDE_SELL, "4", "110", at=T0 + timedelta(hours=1)), now=T0
    )
    store.record_fill(
        connection,
        fill("e3", SIDE_SELL, "1", "90", at=T0 + timedelta(days=1)),
        now=T0,
    )
    store.write_metadata(
        connection,
        tracking_started_at=T0,
        bootstrap_method="EXACT_REPLAY",
        historical_completeness="EXACT_REPLAY_FROM_ACCOUNT_OPEN",
        broker_account_fingerprint="fingerprint",
        asset_class_scope="US_EQUITY",
        now=T0,
    )


def test_today_and_since_tracking_are_different_windows(ledger: sqlite3.Connection) -> None:
    _seed(ledger)

    summary = readmodel.build_summary(ledger, now=T0 + timedelta(days=1))

    assert summary.utc_day == "2026-09-02"
    assert summary.realized_today == -10.0
    assert summary.realized_since_tracking == 30.0
    assert summary.event_count == 2
    assert summary.event_count_today == 1
    assert summary.winning_events == 1
    assert summary.losing_events == 1


def test_the_utc_day_boundary_is_utc(ledger: sqlite3.Connection) -> None:
    """A sale at 23:59Z and one at 00:01Z belong to different days."""
    store.record_fill(ledger, fill("e1", SIDE_BUY, "10", "100"), now=T0)
    store.record_fill(
        ledger,
        fill("e2", SIDE_SELL, "1", "110", at=datetime(2026, 9, 2, 23, 59, tzinfo=UTC)),
        now=T0,
    )
    store.record_fill(
        ledger,
        fill("e3", SIDE_SELL, "1", "110", at=datetime(2026, 9, 3, 0, 1, tzinfo=UTC)),
        now=T0,
    )

    days = {
        str(row["realized_date_utc"])
        for row in ledger.execute("SELECT realized_date_utc FROM realized_pnl_events")
    }
    assert days == {"2026-09-02", "2026-09-03"}


def test_totals_are_summed_exactly_then_rounded_once(ledger: sqlite3.Connection) -> None:
    """Never the sum of rounded parts. The two differ, and only one is right."""
    store.record_fill(ledger, fill("e1", SIDE_BUY, "10", "1"), now=T0)
    for index in range(2, 6):
        store.record_fill(
            ledger,
            fill(f"e{index}", SIDE_SELL, "1", "1.004", at=T0 + timedelta(minutes=index)),
            now=T0,
        )

    summary = readmodel.build_summary(ledger, now=T0)

    assert summary.realized_since_tracking_exact == "0.0160000000"
    assert summary.realized_since_tracking == 0.02


def test_a_symbol_with_no_sales_still_appears_with_its_cost_basis(
    ledger: sqlite3.Connection,
) -> None:
    store.record_fill(ledger, fill("e1", SIDE_BUY, "10", "100", symbol="QQQ"), now=T0)

    rows = {row.symbol: row for row in readmodel.build_by_symbol(ledger, now=T0)}

    assert rows["QQQ"].realized_since_tracking == 0.0
    assert rows["QQQ"].event_count == 0
    assert rows["QQQ"].quantity == "10"
    assert rows["QQQ"].accounting_status == STATUS_TRACKING


def test_realized_events_are_newest_first_and_carry_provenance(
    ledger: sqlite3.Connection,
) -> None:
    _seed(ledger)

    events = readmodel.build_events(ledger, limit=10)

    assert [event.realized_date_utc for event in events] == ["2026-09-02", "2026-09-01"]
    assert all(event.side == "SELL" for event in events)
    assert events[0].provenance == PROVENANCE_EQUITY_RUNTIME
    assert events[0].broker_execution_id == "e3"


def test_the_status_panel_reports_the_tracking_horizon(ledger: sqlite3.Connection) -> None:
    _seed(ledger)
    reconcile.reconcile(ledger, {"SPY": Position("SPY", "5", "100")}, now=T0)

    panel = readmodel.build_status(ledger)

    assert panel.status == store.RECON_CLEAN
    assert panel.tracking_label == "REALIZED SINCE EQUITY PAPER ACTIVATION"
    assert panel.execution_granularity == GRANULARITY_EXECUTION
    assert panel.basis_method == accounting_models.BASIS_WEIGHTED_AVERAGE


def test_an_unbootstrapped_ledger_reports_unknown_not_clean(
    ledger: sqlite3.Connection,
) -> None:
    panel = readmodel.build_status(ledger)

    assert panel.status == store.RECON_UNKNOWN
    assert panel.tracking_started_at is None
    assert "NOT YET TRACKED" in panel.tracking_label


def test_a_ledger_that_was_never_reconciled_is_unknown(ledger: sqlite3.Connection) -> None:
    _seed(ledger)

    panel = readmodel.build_status(ledger)

    assert panel.status == store.RECON_UNKNOWN
    assert panel.message is not None and "never been reconciled" in panel.message


def test_a_stopped_symbol_forces_the_panel_to_mismatch(ledger: sqlite3.Connection) -> None:
    _seed(ledger)
    reconcile.reconcile(ledger, {"SPY": Position("SPY", "5", "100")}, now=T0)
    store.mark_symbol_mismatch(ledger, "SPY", now=T0)

    panel = readmodel.build_status(ledger)

    assert panel.status == store.RECON_MISMATCH
    assert panel.tone == readmodel.TONE_NEGATIVE


def test_the_payload_states_that_the_components_do_not_have_to_sum(
    ledger: sqlite3.Connection,
) -> None:
    _seed(ledger)

    assert readmodel.build_summary(ledger, now=T0).components_are_independent is True


# --------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------


def test_quantity_is_never_negative_across_a_long_random_walk() -> None:
    import random

    rng = random.Random(20260902)
    state = CostBasisState.flat("SPY")
    for index in range(500):
        if state.quantity == 0 or rng.random() < 0.5:
            state = engine.apply_fill(
                state, fill(f"b{index}", SIDE_BUY, str(rng.randint(1, 50)), "100")
            ).state
        else:
            quantity = (state.quantity * Decimal(rng.randint(1, 100))) / Decimal(100)
            if quantity == 0:
                continue
            state = engine.apply_fill(
                state, fill(f"s{index}", SIDE_SELL, str(quantity), "101")
            ).state
        assert state.quantity >= 0
        assert state.total_cost_basis >= 0
        if state.quantity == 0:
            assert state.total_cost_basis == 0


def test_a_full_liquidation_releases_exactly_the_basis_that_was_paid() -> None:
    import random

    rng = random.Random(1)
    state = CostBasisState.flat("SPY")
    paid = Decimal(0)
    for index in range(40):
        quantity = Decimal(rng.randint(1, 999)) / Decimal(1000)
        price = Decimal(rng.randint(1000, 99999)) / Decimal(100)
        state = engine.apply_fill(
            state, fill(f"b{index}", SIDE_BUY, str(quantity), str(price))
        ).state
        paid += quantity * price

    released = Decimal(0)
    trims = 5
    for index in range(trims):
        slice_ = state.quantity / Decimal(trims - index)
        applied = engine.apply_fill(state, fill(f"s{index}", SIDE_SELL, str(slice_), "50"))
        assert applied.realized is not None
        released += applied.realized.released_cost_basis
        state = applied.state

    assert state.quantity == 0
    assert released == paid


def test_realized_totals_do_not_depend_on_how_a_sale_was_split() -> None:
    """One sale of 6, or six sales of 1, release the same basis."""
    opening = engine.apply_fill(CostBasisState.flat("SPY"), fill("e1", SIDE_BUY, "10", "100")).state

    single = engine.apply_fill(opening, fill("s", SIDE_SELL, "6", "110"))
    assert single.realized is not None

    state = opening
    total = Decimal(0)
    for index in range(6):
        applied = engine.apply_fill(state, fill(f"s{index}", SIDE_SELL, "1", "110"))
        assert applied.realized is not None
        total += applied.realized.net_realized_pnl
        state = applied.state

    assert total == single.realized.net_realized_pnl
    assert state.quantity == single.state.quantity
    assert state.total_cost_basis == single.state.total_cost_basis


def test_historical_events_never_change_when_later_ones_arrive(
    ledger: sqlite3.Connection,
) -> None:
    store.record_fill(ledger, fill("e1", SIDE_BUY, "10", "100"), now=T0)
    store.record_fill(ledger, fill("e2", SIDE_SELL, "2", "110", at=T0), now=T0)
    first = tuple(ledger.execute("SELECT * FROM realized_pnl_events").fetchone())

    store.record_fill(ledger, fill("e3", SIDE_BUY, "10", "500", at=T0 + timedelta(days=1)), now=T0)
    store.record_fill(ledger, fill("e4", SIDE_SELL, "1", "600", at=T0 + timedelta(days=2)), now=T0)

    again = tuple(ledger.execute("SELECT * FROM realized_pnl_events ORDER BY event_id").fetchone())
    assert again == first


# --------------------------------------------------------------------------
# Isolation from trading
# --------------------------------------------------------------------------


def _module_source(module: object) -> str:
    """A module's executable code, with every docstring removed.

    Prose in this package necessarily talks about strategies, risk and
    execution; a naive substring scan over the raw file would trip on its own
    explanation of why it does not touch them.
    """
    tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))  # type: ignore[arg-type]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


ACCOUNTING_CORE = (accounting_models, engine, store, ingest, reconcile, readmodel)


@pytest.mark.parametrize("module", ACCOUNTING_CORE, ids=lambda m: m.__name__)
def test_the_accounting_core_imports_no_trading_module(module: object) -> None:
    """Accounting may not reach a strategy, the risk engine or execution.

    The direction that matters most is this one: if accounting cannot import
    them, it cannot become an input to them by accident either, because there
    is no shared object for a value to travel through.
    """
    source = _module_source(module)
    for forbidden in (
        "autotrader.risk",
        "autotrader.strategies",
        "autotrader.decision",
        "autotrader.equity.runtime",
        "autotrader.equity.paper",
        "autotrader.execution",
        "autotrader.runtime",
        "autotrader.shadow",
        "alpaca",
    ):
        assert forbidden not in source, f"{module.__name__} reaches {forbidden}"


@pytest.mark.parametrize("module", ACCOUNTING_CORE, ids=lambda m: m.__name__)
def test_the_accounting_core_cannot_submit_cancel_or_replace(module: object) -> None:
    source = _module_source(module)
    for forbidden in (
        "submit_order",
        "cancel_order",
        "replace_order",
        "close_position",
        "close_all_positions",
    ):
        assert forbidden not in source, f"{module.__name__} names {forbidden}"


def test_no_trading_module_imports_the_accounting_package() -> None:
    """Nothing that decides a trade may read what this package writes."""
    root = Path(inspect.getfile(accounting_models)).parents[1]
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(("accounting/", "cli/", "dashboard/")):
            continue
        if "autotrader.accounting" in path.read_text(encoding="utf-8"):
            offenders.append(relative)

    assert offenders == []


def test_the_engine_is_pure() -> None:
    """No I/O of any kind: no sqlite, no clock, no environment, no filesystem."""
    source = _module_source(engine)
    for forbidden in ("sqlite3", "datetime.now", "os.environ", "open(", "Path("):
        assert forbidden not in source, forbidden


def test_the_engine_never_touches_the_process_decimal_context() -> None:
    """A library that calls `getcontext()` changes arithmetic for its whole process."""
    source = _module_source(engine)
    assert "getcontext" not in source
    assert "setcontext" not in source
    assert "localcontext" in source


# --------------------------------------------------------------------------
# Dashboard routes
# --------------------------------------------------------------------------


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch):
    """A paper API whose ledger is a temporary file with a known history."""
    from fastapi.testclient import TestClient

    from autotrader.accounting.service import ACCOUNTING_DATABASE_PATH_ENV
    from autotrader.dashboard import equity_paper_api

    path = tmp_path / "equity-accounting.db"
    with store.connect(path) as connection:
        store.initialize(connection)
        _seed(connection)
        reconcile.reconcile(connection, {"SPY": Position("SPY", "5", "100")}, now=T0)

    monkeypatch.setenv(ACCOUNTING_DATABASE_PATH_ENV, str(path))
    with TestClient(equity_paper_api.create_app()) as client:
        yield client


@pytest.fixture
def api_client_without_ledger(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient

    from autotrader.accounting.service import ACCOUNTING_DATABASE_PATH_ENV
    from autotrader.dashboard import equity_paper_api

    monkeypatch.setenv(ACCOUNTING_DATABASE_PATH_ENV, str(tmp_path / "absent.db"))
    with TestClient(equity_paper_api.create_app()) as client:
        yield client


def test_the_summary_route_reports_totals_and_status(api_client) -> None:
    payload = api_client.get("/api/equity-paper/realized-pnl/summary").json()

    assert payload["available"] is True
    assert payload["summary"]["realized_since_tracking"] == 30.0
    assert payload["summary"]["event_count"] == 2
    assert payload["status"]["status"] == store.RECON_CLEAN
    assert payload["components_are_independent"] is True


def test_the_by_symbol_route_returns_one_row_per_symbol(api_client) -> None:
    payload = api_client.get("/api/equity-paper/realized-pnl/by-symbol").json()

    symbols = {row["symbol"]: row for row in payload["symbols"]}
    assert symbols["SPY"]["event_count"] == 2
    assert symbols["SPY"]["accounting_status"] == STATUS_TRACKING


def test_the_events_route_filters_and_bounds(api_client) -> None:
    payload = api_client.get(
        "/api/equity-paper/realized-pnl/events", params={"symbol": "spy", "limit": 1}
    ).json()

    assert len(payload["events"]) == 1
    assert payload["events"][0]["symbol"] == "SPY"
    assert payload["events"][0]["side"] == "SELL"
    assert payload["events"][0]["net_realized_pnl_exact"]

    assert (
        api_client.get(
            "/api/equity-paper/realized-pnl/events", params={"limit": 100000}
        ).status_code
        == 422
    )


def test_the_status_route_carries_the_reconciliation_rows(api_client) -> None:
    payload = api_client.get("/api/equity-paper/realized-pnl/status").json()

    assert payload["status"]["tracking_label"] == "REALIZED SINCE EQUITY PAPER ACTIVATION"
    assert payload["reconciliation"][0]["symbol"] == "SPY"
    assert payload["reconciliation"][0]["quantity_matches"] is True


def test_the_symbol_route_returns_the_drawer_payload(api_client) -> None:
    payload = api_client.get("/api/equity-paper/symbols/spy/realized-pnl").json()

    assert payload["available"] is True
    assert payload["symbol"] == "SPY"
    assert payload["realized"]["event_count"] == 2
    assert len(payload["events"]) == 2
    assert payload["status"]["status"] == store.RECON_CLEAN


def test_a_symbol_with_no_ledger_history_is_absent_not_zero(api_client) -> None:
    payload = api_client.get("/api/equity-paper/symbols/AMZN/realized-pnl").json()

    assert payload["available"] is True
    assert payload["realized"] is None
    assert payload["events"] == []


def test_a_missing_ledger_is_reported_not_raised(api_client_without_ledger) -> None:
    response = api_client_without_ledger.get("/api/equity-paper/realized-pnl/summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert payload["unavailable_reason"] == "DATABASE_UNREADABLE"
    assert payload["summary"] is None


def test_a_missing_ledger_leaves_the_other_routes_empty_not_broken(
    api_client_without_ledger,
) -> None:
    assert api_client_without_ledger.get("/api/equity-paper/realized-pnl/by-symbol").json() == {
        "symbols": []
    }
    assert api_client_without_ledger.get("/api/equity-paper/realized-pnl/events").json() == {
        "events": []
    }
    status = api_client_without_ledger.get("/api/equity-paper/realized-pnl/status").json()
    assert status == {"status": None, "reconciliation": []}


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize(
    "path",
    [
        "/api/equity-paper/realized-pnl/summary",
        "/api/equity-paper/realized-pnl/by-symbol",
        "/api/equity-paper/realized-pnl/events",
        "/api/equity-paper/realized-pnl/status",
        "/api/equity-paper/symbols/SPY/realized-pnl",
    ],
)
def test_the_realized_pnl_routes_accept_no_write(api_client, method: str, path: str) -> None:
    assert getattr(api_client, method)(path).status_code == 405


def test_the_realized_pnl_routes_add_no_write_surface() -> None:
    """Re-asserted here so adding a route to this feature fails the suite."""
    from autotrader.dashboard import equity_paper_api

    application = equity_paper_api.create_app()
    realized_routes = [
        route for route in application.routes if "realized-pnl" in str(getattr(route, "path", ""))
    ]

    assert realized_routes
    for route in realized_routes:
        methods = set(getattr(route, "methods", set()) or set())
        assert methods <= equity_paper_api.ALLOWED_METHODS
        segments = {segment.lower() for segment in str(getattr(route, "path", "")).split("/")}
        assert not segments & {"submit", "cancel", "start", "stop", "set", "update", "execute"}


def test_the_dashboard_reader_cannot_write_to_the_ledger(tmp_path: Path) -> None:
    """It opens the ledger read-only, so a viewer cannot create or migrate it."""
    from autotrader.dashboard import realized_pnl as dashboard_realized

    source = _module_source(dashboard_realized)
    assert "connect_read_only" in source
    assert "store.connect(" not in source
    assert "initialize" not in source
