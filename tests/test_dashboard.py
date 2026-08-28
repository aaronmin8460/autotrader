"""C10 tests: the read model, and the invariants that make a dashboard safe.

**Nothing here touches the network or a real credential.** The Alpaca boundary
is faked with real alpaca-py models so normalization runs against real response
shapes, and the fake client's `submit_order` raises rather than records - a
dashboard that placed an order would fail loudly here rather than quietly pass
a count assertion someone later deleted.

The tests that matter most are the ones about *incapability*. The route table
is walked and asserted GET-only; the package's executable code is audited for
the order-submission entry points with prose stripped, so the very docstrings
that promise the rule cannot mask a violation of it; and the database
connection is asserted to refuse a write at the engine level rather than by
convention.

After those come the honesty tests. An unreadable broker, an unreadable
database, a database with no reconciliation run in it, and a flat account are
four different answers, and none of them may render as a zero, as `CLEAN`, or
as an empty table that reads like nothing happened.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import socket
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import (
    AccountStatus,
    AssetClass,
    OrderClass,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
)
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.models import Order, Position, TradeAccount
from fastapi.testclient import TestClient

from autotrader import dashboard
from autotrader.dashboard import api as dashboard_api
from autotrader.dashboard import models as dashboard_models
from autotrader.dashboard import service as dashboard_service
from autotrader.dashboard.broker import BrokerRead, SharedBrokerReader, read_broker
from autotrader.dashboard.models import (
    SOURCE_BROKER,
    SOURCE_LOCAL,
    SYSTEM_ATTENTION,
    SYSTEM_HEALTHY,
    SYSTEM_PAUSED,
    UNAVAILABLE_BROKER_NOT_CONFIGURED,
    UNAVAILABLE_BROKER_UNREADABLE,
    UNAVAILABLE_DATABASE_UNREADABLE,
    UNAVAILABLE_NOT_RECORDED,
    Overview,
    RuntimePanel,
)
from autotrader.dashboard.service import build_overview, read_state
from autotrader.execution.models import TRADABLE_SYMBOLS
from autotrader.state.sqlite import (
    INTENT_STATUS_REJECTED,
    INTENT_STATUS_SUBMITTED,
    INTENT_STATUS_UNKNOWN,
    RECONCILIATION_STATUS_CLEAN,
    RECONCILIATION_STATUS_FAILED,
    RECONCILIATION_STATUS_REPAIRED,
    RECONCILIATION_STATUS_UNRESOLVED,
    connect,
    ensure_daily_risk_baseline,
    initialize_database,
    record_order_intent,
    record_reconciliation_event,
    record_reconciliation_run,
    record_strategy_run,
    record_system_event,
    transaction,
    update_order_intent_status,
    upsert_broker_order,
    upsert_position,
    upsert_runtime_checkpoint,
)
from conftest import establish_account_safety

NOW = datetime(2026, 3, 4, 12, 20, 7, tzinfo=UTC)
BTC = "BTC/USD"
ETH = "ETH/USD"
BROKER_ORDER_UUID = "1f2b7c40-8a3d-4c19-9b52-7e6a0d3f5c81"


# --------------------------------------------------------------------------
# Source-level helpers
# --------------------------------------------------------------------------


def code_without_prose(source: str) -> str:
    """`source` with every docstring and comment removed.

    The guarantees below are about *executable code*. This package's own
    documentation explains at length what it must never do, so a naive
    substring scan would trip over the very sentences that state the rule.
    """
    tree = ast.parse(source)
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


def package_code() -> dict[str, str]:
    """Every module in the dashboard package, prose stripped."""
    root = Path(dashboard.__file__).resolve().parent
    return {
        str(path.relative_to(root)): code_without_prose(path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("*.py"))
    }


# --------------------------------------------------------------------------
# Alpaca test doubles
#
# The models are real; only the transport is faked.
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeHTTPError:
    def __init__(self, status_code: int) -> None:
        self.response = _FakeResponse(status_code)


def api_error(status_code: int | None = 401, message: str = "broker said no") -> APIError:
    body = json.dumps({"code": 40110000, "message": message})
    if status_code is None:
        return APIError(body)
    return APIError(body, _FakeHTTPError(status_code))


def make_account(
    *,
    equity: str = "100000",
    cash: str = "82000",
    status: AccountStatus = AccountStatus.ACTIVE,
    trading_blocked: bool = False,
) -> TradeAccount:
    return TradeAccount(
        id=uuid4(),
        account_number="PA0000000000",
        status=status,
        equity=equity,
        cash=cash,
        trading_blocked=trading_blocked,
        account_blocked=False,
        trade_suspended_by_user=False,
    )


def make_position(
    symbol: str = BTC,
    *,
    qty: str = "0.05",
    market_value: str = "4000",
    avg_entry_price: str = "76000",
    side: PositionSide = PositionSide.LONG,
) -> Position:
    return Position(
        asset_id=uuid4(),
        symbol=symbol,
        exchange="CRYPTO",
        asset_class=AssetClass.CRYPTO,
        avg_entry_price=avg_entry_price,
        qty=qty,
        side=side,
        cost_basis=str(float(qty) * float(avg_entry_price)),
        market_value=market_value,
    )


def make_order(
    *,
    client_order_id: str,
    symbol: str = BTC,
    qty: str = "0.05",
    filled_qty: str = "0.05",
    filled_avg_price: str | None = "76500",
    status: OrderStatus = OrderStatus.FILLED,
) -> Order:
    return Order(
        id=BROKER_ORDER_UUID,
        client_order_id=client_order_id,
        created_at=NOW,
        updated_at=NOW,
        submitted_at=NOW,
        filled_at=NOW,
        order_class=OrderClass.SIMPLE,
        time_in_force=TimeInForce.GTC,
        status=status,
        extended_hours=False,
        symbol=symbol,
        qty=qty,
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        side=AlpacaOrderSide.BUY,
        order_type=OrderType.MARKET,
        type=OrderType.MARKET,
    )


class FakeTradingClient:
    """Stands in for `TradingClient`, and refuses to submit anything."""

    def __init__(
        self,
        *,
        account: TradeAccount | BaseException | None = None,
        positions: list[Position] | BaseException | None = None,
    ) -> None:
        self._account = account if account is not None else make_account()
        self._positions = positions if positions is not None else []
        self.account_calls = 0
        self.position_calls = 0

    def get_account(self) -> TradeAccount:
        self.account_calls += 1
        if isinstance(self._account, BaseException):
            raise self._account
        return self._account

    def get_all_positions(self) -> list[Position]:
        self.position_calls += 1
        if isinstance(self._positions, BaseException):
            raise self._positions
        return list(self._positions)

    def submit_order(self, order_data: object) -> Order:
        raise AssertionError("the dashboard must never submit an order; it only reads.")


def broker_ok(**kwargs: object) -> BrokerRead:
    """A successful broker read, built through the real normalization path."""
    return read_broker(FakeTradingClient(**kwargs))  # type: ignore[arg-type]


BROKER_MISSING = BrokerRead(ok=False, reason=UNAVAILABLE_BROKER_NOT_CONFIGURED)
BROKER_BROKEN = BrokerRead(ok=False, reason=UNAVAILABLE_BROKER_UNREADABLE)


# --------------------------------------------------------------------------
# Database fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    """A fresh, empty, correctly versioned operational database."""
    return initialize_database(tmp_path / "autotrader.db")


@pytest.fixture
def populated(database_path: Path) -> Path:
    """A database that looks like a healthy system that has traded once."""
    with connect(database_path) as connection, transaction(connection):
        run_id = record_strategy_run(
            connection,
            strategy_name="EMA20 / EMA50",
            mode="PAPER",
            started_at=NOW - timedelta(hours=2),
        )
        # The crypto runtime records this on every start, and it is what tells
        # the two services apart in a table they share. Without it the panel
        # cannot know a restart happened.
        record_system_event(
            connection,
            event_timestamp=NOW - timedelta(hours=2),
            event_type="RUNTIME_STARTED",
            message="Crypto runtime started.",
        )
        upsert_position(
            connection,
            symbol=BTC,
            quantity=Decimal("0.05"),
            average_price=76000.0,
            updated_at=NOW - timedelta(minutes=5),
        )
        for symbol in (BTC, ETH):
            upsert_runtime_checkpoint(
                connection,
                symbol=symbol,
                last_processed_bar_timestamp=NOW.replace(minute=0, second=0, microsecond=0),
                updated_at=NOW - timedelta(minutes=5),
            )
        intent = record_order_intent(
            connection,
            client_order_id="autotrader-aaaa",
            strategy_run_id=run_id,
            created_at=NOW - timedelta(minutes=30),
            symbol=BTC,
            side="BUY",
            requested_quantity=Decimal("0.05"),
            approved_quantity=Decimal("0.050000000"),
            reference_price=76000.0,
            risk_reason_code="APPROVED",
        )
        update_order_intent_status(
            connection,
            order_intent_id=intent,
            status=INTENT_STATUS_SUBMITTED,
            updated_at=NOW - timedelta(minutes=30),
        )
        upsert_broker_order(
            connection,
            order_intent_id=intent,
            broker_order_id=BROKER_ORDER_UUID,
            client_order_id="autotrader-aaaa",
            symbol=BTC,
            side="BUY",
            quantity=Decimal("0.05"),
            filled_quantity=Decimal("0.05"),
            filled_average_price=76500.0,
            status="filled",
            submitted_at=NOW - timedelta(minutes=30),
            filled_at=NOW - timedelta(minutes=30),
            updated_at=NOW - timedelta(minutes=30),
        )
        record_reconciliation_run(
            connection,
            started_at=NOW - timedelta(minutes=10),
            completed_at=NOW - timedelta(minutes=10) + timedelta(seconds=2),
            status=RECONCILIATION_STATUS_CLEAN,
            safe_to_trade=True,
            orders_checked=1,
            positions_checked=len(TRADABLE_SYMBOLS),
            issues_count=0,
            unresolved_count=0,
        )
        # A real full-universe pass writes the shared account safety row beside
        # its audit row; a database with one and not the other is not a state
        # any pass leaves behind.
        establish_account_safety(connection)
        ensure_daily_risk_baseline(
            connection,
            risk_date_utc=NOW.date(),
            baseline_equity=Decimal("99000"),
            captured_at=NOW - timedelta(hours=6),
        )
    return database_path


def overview(path: Path, broker: BrokerRead = BROKER_MISSING, **kwargs: object):
    """One page, built at a fixed instant."""
    return build_overview(database_path=path, now=NOW, broker=broker, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def client(populated: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The real application, pointed at a real database, with a fake broker."""
    monkeypatch.setenv(dashboard_api.DATABASE_PATH_ENV, str(populated))
    monkeypatch.setattr(dashboard_api, "BROKER", SharedBrokerReader(FakeTradingClient()))
    with TestClient(dashboard_api.create_app()) as test_client:
        yield test_client


# ==========================================================================
# The critical safety test
# ==========================================================================


def test_dashboard_has_no_trading_write_surface() -> None:
    """The invariant this whole milestone rests on, asserted three ways.

    First: the assembled application exposes no method that could carry a
    command. Second: no route path names a trading control, so a future GET
    that *acted* would still be caught. Third: the package's executable code
    never names the order-submission entry points, so there is nothing behind
    a route to call even if one were added.

    Hiding a button would have left the endpoint. There is no endpoint.
    """
    application = dashboard_api.create_app()

    for route in application.routes:
        methods = set(getattr(route, "methods", set()) or set())
        forbidden = methods & {"POST", "PUT", "PATCH", "DELETE"}
        assert not forbidden, f"{getattr(route, 'path', route)} exposes {sorted(forbidden)}"
        assert methods <= dashboard_api.ALLOWED_METHODS | {"OPTIONS"}, (
            f"{getattr(route, 'path', route)} exposes {sorted(methods)}"
        )

    # A noun is a resource to read - `/orders` is a list of orders. A verb is a
    # command, so the audit looks for one occupying a path segment of its own.
    action_verbs = {
        "submit",
        "send",
        "place",
        "cancel",
        "buy",
        "sell",
        "close",
        "flatten",
        "liquidate",
        "start",
        "stop",
        "pause",
        "resume",
        "kill",
        "repair",
        "reconcile",
        "execute",
        "run",
        "create",
        "update",
        "edit",
        "set",
        "delete",
        "reset",
        "override",
    }
    for route in application.routes:
        segments = {segment.lower() for segment in str(getattr(route, "path", "")).split("/")}
        offending = segments & action_verbs
        assert not offending, f"{getattr(route, 'path', route)} names the action {offending}"

    forbidden_symbols = (
        "submit_order",
        "submit_order_intent",
        "execute_paper_order",
        "build_market_order_request",
        "MarketOrderRequest",
        "OrderRequest",
        "record_order_intent",
        "upsert_position",
        "upsert_broker_order",
        "update_order_intent_status",
        "record_reconciliation_run",
        "reconcile_paper_state",
        "resolve_daily_baseline_equity",
        "initialize_database",
        "paper=False",
    )
    for name, code in package_code().items():
        for symbol in forbidden_symbols:
            assert symbol not in code, f"{symbol} found in dashboard/{name}"


# ==========================================================================
# Read-only, structurally
# ==========================================================================


def test_the_dashboard_connection_refuses_a_write(populated: Path) -> None:
    """`mode=ro` plus `query_only`: a write is an engine error, not a habit."""
    with (
        dashboard_service.read_only_connection(populated) as connection,
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        connection.execute(
            "INSERT INTO system_events (event_timestamp, event_type, message, created_at)"
            " VALUES ('a', 'b', 'c', 'd')"
        )


def test_reading_never_creates_a_database(tmp_path: Path) -> None:
    """A viewer that creates a trading database has written to one."""
    missing = tmp_path / "nothing-here.db"

    snapshot = read_state(missing, now=NOW)

    assert snapshot.ok is False
    assert snapshot.reason == UNAVAILABLE_DATABASE_UNREADABLE
    assert not missing.exists()


def test_reading_leaves_the_database_byte_identical(populated: Path) -> None:
    """The whole page is assembled without the file changing."""
    before = populated.read_bytes()

    overview(populated, broker_ok(positions=[make_position()]))

    assert populated.read_bytes() == before


def test_the_dashboard_issues_no_journal_mode_pragma() -> None:
    """Setting a journal mode writes the header of a database it does not own."""
    for name, code in package_code().items():
        assert "journal_mode" not in code, f"journal_mode set in dashboard/{name}"


def test_the_dashboard_makes_no_socket_of_its_own(
    populated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the injected broker client may reach a network, and it is a fake."""

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("the dashboard must not open a socket")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

    assert overview(populated, broker_ok()).system_state in {SYSTEM_HEALTHY, SYSTEM_ATTENTION}


# ==========================================================================
# Overview
# ==========================================================================


def test_the_overview_reports_a_healthy_system_as_healthy(populated: Path) -> None:
    page = overview(populated, broker_ok(positions=[make_position()]))

    assert page.system_state == SYSTEM_HEALTHY
    assert page.attention == ()
    assert page.environment == "PAPER"
    assert page.generated_at == NOW.isoformat()


def test_every_metric_is_present_when_both_reads_succeed(populated: Path) -> None:
    page = overview(populated, broker_ok(positions=[make_position(market_value="4000")]))
    metrics = page.metrics
    assert metrics is not None

    assert metrics.equity.value == pytest.approx(100000.0)
    assert metrics.cash.value == pytest.approx(82000.0)
    assert metrics.exposure.value == pytest.approx(4000.0)
    assert metrics.exposure_fraction == pytest.approx(0.04)
    # 100000 live equity against the 99000 baseline stored for this UTC day.
    assert metrics.daily_pnl.value == pytest.approx(1000.0)
    assert metrics.daily_pnl_baseline.value == pytest.approx(99000.0)
    assert metrics.daily_pnl_baseline_date == NOW.date().isoformat()


def test_the_daily_figure_needs_a_stored_baseline_and_says_so(database_path: Path) -> None:
    """No baseline row means no P&L. It does not mean a P&L of zero."""
    page = overview(database_path, broker_ok())
    metrics = page.metrics
    assert metrics is not None

    assert metrics.equity.available is True
    assert metrics.daily_pnl.available is False
    assert metrics.daily_pnl.value is None
    assert metrics.daily_pnl.unavailable_reason == UNAVAILABLE_NOT_RECORDED


# ==========================================================================
# Positions
# ==========================================================================


def test_positions_come_from_the_broker_when_it_can_be_read(populated: Path) -> None:
    page = overview(
        populated,
        broker_ok(
            positions=[make_position(qty="0.05", market_value="4000", avg_entry_price="76000")]
        ),
    )
    panel = page.positions
    assert panel is not None

    assert panel.source == SOURCE_BROKER
    (row,) = panel.rows
    assert row.symbol == BTC
    assert row.asset_class == "CRYPTO"
    assert row.quantity == "0.05"
    assert row.market_value == pytest.approx(4000.0)
    assert row.price == pytest.approx(80000.0)
    assert row.unrealized_pnl == pytest.approx(200.0)


def test_an_unreadable_broker_falls_back_to_the_local_snapshot_and_labels_it(
    populated: Path,
) -> None:
    """A stale snapshot is fine. A stale snapshot presented as live is not."""
    panel = overview(populated, BROKER_BROKEN).positions
    assert panel is not None

    assert panel.source == SOURCE_LOCAL
    assert panel.unavailable_reason == UNAVAILABLE_BROKER_UNREADABLE
    assert panel.note is not None
    (row,) = panel.rows
    assert row.symbol == BTC
    assert row.quantity == "0.05"
    # An entry price is not a market price, so no market value is invented.
    assert row.price is None
    assert row.market_value is None
    assert row.unrealized_pnl is None


def test_a_flat_symbol_is_not_a_position_row(populated: Path) -> None:
    """A zero-quantity local row is reported as flat, not as a holding."""
    with connect(populated) as connection, transaction(connection):
        upsert_position(connection, symbol=BTC, quantity=Decimal(0), updated_at=NOW)

    panel = overview(populated, BROKER_BROKEN).positions
    assert panel is not None

    assert panel.rows == ()
    assert BTC in panel.flat_symbols


def test_a_broker_with_nothing_open_reports_the_tracked_symbols_flat(populated: Path) -> None:
    panel = overview(populated, broker_ok(positions=[])).positions
    assert panel is not None

    assert panel.source == SOURCE_BROKER
    assert panel.rows == ()
    assert set(panel.flat_symbols) == set(TRADABLE_SYMBOLS)


def test_an_equity_symbol_classifies_without_an_equity_implementation(populated: Path) -> None:
    """The read model represents a generic asset; it does not invent one."""
    assert dashboard_service.asset_class_for("SPY") == "EQUITY"
    assert dashboard_service.asset_class_for(BTC) == "CRYPTO"

    # Nothing in a crypto-only database produces an equity row.
    panel = overview(populated, broker_ok(positions=[make_position()])).positions
    assert panel is not None
    assert {row.asset_class for row in panel.rows} == {"CRYPTO"}


# ==========================================================================
# Orders
# ==========================================================================


def test_orders_are_newest_first_and_carry_the_broker_status(populated: Path) -> None:
    panel = overview(populated).orders
    assert panel is not None

    (row,) = panel.rows
    assert row.symbol == BTC
    assert row.side == "BUY"
    assert row.quantity == "0.050000000"
    assert row.filled_quantity == "0.05"
    assert row.average_fill_price == pytest.approx(76500.0)
    assert row.status == "FILLED"
    assert row.status_source == SOURCE_BROKER
    assert row.needs_attention is False


def test_an_unknown_intent_is_visible_and_flagged_for_attention(populated: Path) -> None:
    """The status that means nobody knows what the broker did.

    It must reach the screen, it must be marked, and it must raise the whole
    page - an order this system cannot account for is the definition of
    something needing a person.
    """
    with connect(populated) as connection, transaction(connection):
        intent = record_order_intent(
            connection,
            client_order_id="autotrader-unknown",
            strategy_run_id=None,
            created_at=NOW - timedelta(minutes=3),
            symbol=ETH,
            side="BUY",
            requested_quantity=Decimal("0.4"),
            approved_quantity=Decimal("0.4"),
            reference_price=2800.0,
            risk_reason_code="APPROVED",
        )
        update_order_intent_status(
            connection,
            order_intent_id=intent,
            status=INTENT_STATUS_UNKNOWN,
            updated_at=NOW - timedelta(minutes=3),
        )

    page = overview(populated, broker_ok())
    panel = page.orders
    assert panel is not None

    flagged = [row for row in panel.rows if row.needs_attention]
    assert [row.client_order_id for row in flagged] == ["autotrader-unknown"]
    assert flagged[0].status == INTENT_STATUS_UNKNOWN
    assert flagged[0].status_tone == dashboard_models.TONE_ATTENTION
    assert panel.attention_count == 1
    assert page.system_state == SYSTEM_ATTENTION
    assert any("UNKNOWN" in reason for reason in page.attention)


def test_an_intent_the_broker_never_answered_for_still_has_a_row(populated: Path) -> None:
    """The row with no broker snapshot is the one that most needs to be seen."""
    with connect(populated) as connection, transaction(connection):
        intent = record_order_intent(
            connection,
            client_order_id="autotrader-rejected",
            strategy_run_id=None,
            created_at=NOW - timedelta(minutes=1),
            symbol=BTC,
            side="BUY",
            requested_quantity=Decimal("0.00001"),
            approved_quantity=Decimal("0.00001"),
            reference_price=76000.0,
            risk_reason_code="APPROVED",
        )
        update_order_intent_status(
            connection,
            order_intent_id=intent,
            status=INTENT_STATUS_REJECTED,
            updated_at=NOW - timedelta(minutes=1),
        )

    panel = overview(populated).orders
    assert panel is not None

    row = panel.rows[0]
    assert row.client_order_id == "autotrader-rejected"
    assert row.status == INTENT_STATUS_REJECTED
    assert row.status_source == SOURCE_LOCAL
    assert row.filled_quantity is None
    assert row.average_fill_price is None


def test_the_order_panel_is_bounded_but_reports_the_true_total(populated: Path) -> None:
    with connect(populated) as connection, transaction(connection):
        for index in range(6):
            record_order_intent(
                connection,
                client_order_id=f"autotrader-bulk-{index}",
                strategy_run_id=None,
                created_at=NOW - timedelta(minutes=index + 1),
                symbol=BTC,
                side="BUY",
                requested_quantity=Decimal("0.01"),
                approved_quantity=Decimal("0.01"),
                reference_price=76000.0,
                risk_reason_code="APPROVED",
            )

    panel = overview(populated, order_limit=3).orders
    assert panel is not None

    assert len(panel.rows) == 3
    assert panel.total == 7


# ==========================================================================
# Reconciliation
# ==========================================================================


def test_reconciliation_is_quoted_from_the_stored_run(populated: Path) -> None:
    panel = overview(populated).reconciliation
    assert panel is not None

    assert panel.available is True
    assert panel.status == RECONCILIATION_STATUS_CLEAN
    assert panel.safe_to_trade is True
    assert panel.orders_checked == 1
    assert panel.positions_checked == len(TRADABLE_SYMBOLS) == 12
    assert panel.unresolved == 0
    assert panel.repairs == 0


def test_a_database_with_no_reconciliation_run_never_reports_clean(database_path: Path) -> None:
    """`CLEAN` is a stored conclusion, never the absence of an exception."""
    page = overview(database_path)
    panel = page.reconciliation
    assert panel is not None

    assert panel.available is False
    assert panel.status is None
    assert panel.unavailable_reason == UNAVAILABLE_NOT_RECORDED
    assert page.system_state == SYSTEM_ATTENTION

    safety = next(row for row in page.health if row.key == "trading_safety")
    assert safety.status == "BLOCKED"


def test_an_unresolved_reconciliation_pauses_the_whole_page(populated: Path) -> None:
    with connect(populated) as connection, transaction(connection):
        run = record_reconciliation_run(
            connection,
            started_at=NOW - timedelta(minutes=2),
            completed_at=NOW - timedelta(minutes=2) + timedelta(seconds=1),
            status=RECONCILIATION_STATUS_UNRESOLVED,
            safe_to_trade=False,
            orders_checked=2,
            positions_checked=2,
            issues_count=2,
            unresolved_count=1,
        )
        record_reconciliation_event(
            connection,
            reconciliation_run_id=run,
            event_timestamp=NOW - timedelta(minutes=2),
            category="ORDER",
            outcome=RECONCILIATION_STATUS_REPAIRED,
            symbol=BTC,
            client_order_id="autotrader-aaaa",
            detail="Snapshot refreshed from broker truth.",
        )

    page = overview(populated, broker_ok())
    panel = page.reconciliation
    assert panel is not None

    assert panel.status == RECONCILIATION_STATUS_UNRESOLVED
    assert panel.safe_to_trade is False
    assert panel.unresolved == 1
    assert panel.repairs == 1
    assert page.system_state == SYSTEM_PAUSED
    assert any("UNRESOLVED" in reason for reason in page.attention)


def test_a_failed_reconciliation_also_pauses_the_page(populated: Path) -> None:
    with connect(populated) as connection, transaction(connection):
        record_reconciliation_run(
            connection,
            started_at=NOW - timedelta(minutes=1),
            completed_at=NOW - timedelta(minutes=1) + timedelta(seconds=1),
            status=RECONCILIATION_STATUS_FAILED,
            safe_to_trade=False,
            orders_checked=0,
            positions_checked=0,
            issues_count=1,
            unresolved_count=1,
        )

    page = overview(populated, broker_ok())

    assert page.system_state == SYSTEM_PAUSED
    assert page.reconciliation is not None
    assert page.reconciliation.status == RECONCILIATION_STATUS_FAILED


# ==========================================================================
# Runtime and checkpoints
# ==========================================================================


def runtime_named(page: Overview, key: str) -> RuntimePanel:
    """The panel for one service. There are two of them now."""
    panel = next((item for item in page.runtimes if item.key == key), None)
    assert panel is not None, f"no {key} runtime panel"
    return panel


def test_runtime_state_is_derived_from_the_durable_trail(populated: Path) -> None:
    page = overview(populated)
    panel = runtime_named(page, "crypto")

    assert panel.label == "Crypto runtime"
    assert panel.state == "RUNNING"
    assert panel.started_at == (NOW - timedelta(hours=2)).isoformat()
    assert panel.startup_safety == "SAFE"
    assert panel.last_cycle_at == (NOW - timedelta(minutes=5)).isoformat()
    assert {checkpoint.symbol for checkpoint in panel.checkpoints} == {BTC, ETH}
    assert all(checkpoint.stale is False for checkpoint in panel.checkpoints)


def test_the_next_cycle_is_the_real_schedule_not_a_guess(populated: Path) -> None:
    """12:20:07 UTC falls in the 12:15 bar, so the next wake is 12:30 plus the
    provider-lag allowance the runtime itself uses."""
    panel = runtime_named(overview(populated), "crypto")

    assert panel.next_cycle_at == datetime(2026, 3, 4, 12, 30, 5, tzinfo=UTC).isoformat()


def test_a_runtime_that_stopped_claiming_bars_is_stale_not_running(populated: Path) -> None:
    with connect(populated) as connection, transaction(connection):
        for symbol in (BTC, ETH):
            upsert_runtime_checkpoint(
                connection,
                symbol=symbol,
                last_processed_bar_timestamp=NOW.replace(minute=15, second=0, microsecond=0),
                updated_at=NOW - timedelta(hours=4),
            )

    page = overview(populated, broker_ok())
    panel = runtime_named(page, "crypto")

    assert panel.state == "STALE"
    assert all(checkpoint.stale is True for checkpoint in panel.checkpoints)
    assert page.system_state == SYSTEM_ATTENTION


def test_a_recorded_trading_pause_outranks_an_open_strategy_run(populated: Path) -> None:
    """The run row still says RUNNING; the pause event is the truth that matters."""
    with connect(populated) as connection, transaction(connection):
        record_system_event(
            connection,
            event_timestamp=NOW - timedelta(minutes=1),
            event_type="RUNTIME_TRADING_PAUSED",
            message="Trading paused: an ambiguous submission outcome.",
        )

    page = overview(populated, broker_ok())
    panel = runtime_named(page, "crypto")

    assert panel.state == "PAUSED"
    assert page.system_state == SYSTEM_PAUSED
    # The failure event is account-level, not per-service, so it is reported
    # once on the page rather than on each runtime card.
    assert page.last_failure is not None
    assert page.last_failure_at == (NOW - timedelta(minutes=1)).isoformat()


def test_a_pause_from_before_the_current_run_does_not_pause_it(populated: Path) -> None:
    """A restart clears the previous process's pause; only this run's counts."""
    with connect(populated) as connection, transaction(connection):
        record_system_event(
            connection,
            event_timestamp=NOW - timedelta(hours=5),
            event_type="RUNTIME_TRADING_PAUSED",
            message="An older process paused, and then was restarted.",
        )

    panel = runtime_named(overview(populated, broker_ok()), "crypto")

    assert panel.state == "RUNNING"


def test_a_database_with_no_strategy_run_reports_never_started(database_path: Path) -> None:
    panel = runtime_named(overview(database_path), "crypto")

    assert panel.state == "NEVER STARTED"
    assert panel.checkpoints == ()
    assert panel.last_cycle_at is None
    assert panel.startup_safety == "UNRESOLVED"


def test_the_paper_execution_gate_is_reported_as_the_environment_sets_it(
    populated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOTRADER_PAPER_TRADING_ENABLED", "true")
    enabled = runtime_named(overview(populated), "crypto")
    assert enabled.paper_execution_enabled is True

    monkeypatch.setenv("AUTOTRADER_PAPER_TRADING_ENABLED", "TRUE")
    typo = runtime_named(overview(populated), "crypto")
    assert typo.paper_execution_enabled is False


# ==========================================================================
# Risk
# ==========================================================================


def test_risk_limits_are_the_engine_s_own_and_are_always_shown(populated: Path) -> None:
    """The policy exists whether or not the account behind it can be read."""
    from autotrader.risk.engine import (
        MAX_DAILY_LOSS_FRACTION,
        MAX_POSITION_FRACTION,
        MAX_TOTAL_EXPOSURE_FRACTION,
    )

    panel = overview(populated, BROKER_BROKEN).risk
    assert panel is not None

    assert [limit.limit_fraction for limit in panel.limits] == [
        MAX_POSITION_FRACTION,
        MAX_TOTAL_EXPOSURE_FRACTION,
        MAX_DAILY_LOSS_FRACTION,
    ]
    assert panel.available is False
    assert all(limit.used_value.available is False for limit in panel.limits)
    assert all(
        limit.used_value.unavailable_reason == UNAVAILABLE_BROKER_UNREADABLE
        for limit in panel.limits
    )


def test_risk_utilization_is_computed_against_live_equity(populated: Path) -> None:
    page = overview(
        populated,
        broker_ok(
            positions=[
                make_position(BTC, qty="0.05", market_value="4000"),
                make_position(ETH, qty="1.0", market_value="2800"),
            ]
        ),
    )
    panel = page.risk
    assert panel is not None
    limits = {limit.key: limit for limit in panel.limits}

    assert panel.available is True
    assert limits["position"].subject == BTC
    assert limits["position"].used_fraction == pytest.approx(0.04)
    assert limits["position"].utilization == pytest.approx(0.8)
    assert limits["position"].breached is False
    assert limits["total_exposure"].used_fraction == pytest.approx(0.068)
    # Equity is above the stored baseline, so the loss halt is at zero usage.
    assert limits["daily_loss"].used_value.value == pytest.approx(0.0)


def test_a_symbol_over_its_cap_is_reported_as_breached(populated: Path) -> None:
    panel = overview(
        populated, broker_ok(positions=[make_position(BTC, qty="0.2", market_value="9000")])
    ).risk
    assert panel is not None
    limits = {limit.key: limit for limit in panel.limits}

    assert limits["position"].used_fraction == pytest.approx(0.09)
    assert limits["position"].breached is True


def test_no_separate_crypto_and_equity_books_are_invented(populated: Path) -> None:
    """Those limits do not exist yet, so this screen must not show them."""
    panel = overview(populated, broker_ok()).risk
    assert panel is not None

    assert [limit.key for limit in panel.limits] == ["position", "total_exposure", "daily_loss"]


# ==========================================================================
# Degraded reads
# ==========================================================================


def test_an_unreadable_database_degrades_every_panel_honestly(tmp_path: Path) -> None:
    page = overview(tmp_path / "absent.db", broker_ok())

    assert page.system_state == SYSTEM_ATTENTION
    assert page.database is not None
    assert page.database.status == "UNAVAILABLE"
    assert page.orders is not None
    assert page.orders.unavailable_reason == UNAVAILABLE_DATABASE_UNREADABLE
    assert page.reconciliation is not None
    assert page.reconciliation.available is False
    assert page.runtimes and all(panel.state == "UNAVAILABLE" for panel in page.runtimes)
    safety = next(row for row in page.health if row.key == "trading_safety")
    assert safety.status == "UNKNOWN"


def test_a_corrupt_database_reports_unreadable_rather_than_raising(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a SQLite file, and never was")

    snapshot = read_state(corrupt, now=NOW)

    assert snapshot.ok is False
    assert snapshot.reason == UNAVAILABLE_DATABASE_UNREADABLE


def test_a_broker_error_never_becomes_an_exception(populated: Path) -> None:
    result = read_broker(FakeTradingClient(account=api_error(401, "unauthorized")))

    assert result.ok is False
    assert result.reason == UNAVAILABLE_BROKER_UNREADABLE
    assert result.account is None
    assert result.positions is None


def test_a_short_position_is_refused_rather_than_shown_as_a_long(populated: Path) -> None:
    """The execution boundary refuses to normalize one; the dashboard reports it."""
    result = read_broker(FakeTradingClient(positions=[make_position(BTC, side=PositionSide.SHORT)]))

    assert result.ok is False
    assert result.reason == UNAVAILABLE_BROKER_UNREADABLE


def test_missing_credentials_are_a_distinct_state_from_a_broken_broker(
    monkeypatch: pytest.MonkeyPatch, populated: Path
) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    result = read_broker()
    assert result.ok is False
    assert result.reason == UNAVAILABLE_BROKER_NOT_CONFIGURED

    page = overview(populated, result)
    assert page.broker is not None
    assert page.broker.status == "NOT CONFIGURED"
    # Not configured is a local choice, not a fault: it must not raise an alarm.
    assert page.system_state == SYSTEM_HEALTHY
    assert page.notices != ()


def test_a_blocked_paper_account_is_flagged(populated: Path) -> None:
    page = overview(populated, broker_ok(account=make_account(trading_blocked=True)))
    assert page.broker is not None

    assert page.broker.status == "ATTENTION"
    assert page.system_state == SYSTEM_ATTENTION


def test_the_shared_reader_drops_a_client_that_failed(populated: Path) -> None:
    """A broker that recovers must not need a process restart to be seen."""
    failing = FakeTradingClient(account=api_error(500, "upstream"))
    reader = SharedBrokerReader(failing)

    assert reader.read().ok is False
    # The failed client was dropped, so the next read has no credentials to
    # build a replacement with and says exactly that.
    assert reader.read().reason in {
        UNAVAILABLE_BROKER_NOT_CONFIGURED,
        UNAVAILABLE_BROKER_UNREADABLE,
    }


# ==========================================================================
# HTTP surface
# ==========================================================================


def test_every_documented_route_answers_a_get(client: TestClient) -> None:
    for path in (
        "/api/dashboard/health",
        "/api/dashboard/overview",
        "/api/dashboard/positions",
        "/api/dashboard/orders",
        "/api/dashboard/risk",
        "/api/dashboard/system",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.json()


@pytest.mark.parametrize(
    "path",
    [
        "/api/dashboard/overview",
        "/api/dashboard/positions",
        "/api/dashboard/orders",
        "/api/dashboard/risk",
        "/api/dashboard/system",
    ],
)
@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_no_route_accepts_a_write_method(client: TestClient, path: str, method: str) -> None:
    """405, every time. There is no verb here that carries an instruction."""
    response = getattr(client, method)(path)

    assert response.status_code == 405


@pytest.mark.parametrize(
    "path",
    [
        "/api/dashboard/orders/submit",
        "/api/dashboard/orders/cancel",
        "/api/dashboard/risk/limits",
        "/api/dashboard/runtime/start",
        "/api/dashboard/runtime/stop",
        "/api/dashboard/reconciliation/repair",
    ],
)
def test_the_trading_control_routes_someone_might_look_for_do_not_exist(
    client: TestClient, path: str
) -> None:
    assert client.post(path).status_code in {404, 405}
    assert client.get(path).status_code == 404


def test_the_liveness_route_opens_no_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(dashboard_api.DATABASE_PATH_ENV, str(tmp_path / "absent.db"))
    with TestClient(dashboard_api.create_app()) as test_client:
        response = test_client.get("/api/dashboard/health")

    assert response.status_code == 200
    assert response.json()["read_only"] is True


def test_the_overview_route_serializes_the_whole_page(client: TestClient) -> None:
    payload = client.get("/api/dashboard/overview").json()

    assert set(payload) >= {
        "generated_at",
        "environment",
        "system_state",
        "metrics",
        "positions",
        "orders",
        "health",
        "reconciliation",
        "runtimes",
        "account_safety",
        "api_budget",
        "risk",
    }
    assert payload["environment"] == "PAPER"
    # An exact quantity survives the wire as text, not as a float.
    assert payload["orders"]["rows"][0]["quantity"] == "0.050000000"


def test_the_sub_routes_are_slices_of_the_same_read(client: TestClient) -> None:
    """Five endpoints, one read model: they cannot disagree with each other."""
    page = client.get("/api/dashboard/overview").json()

    assert client.get("/api/dashboard/positions").json()["source"] == page["positions"]["source"]
    assert client.get("/api/dashboard/orders").json()["total"] == page["orders"]["total"]
    risk = client.get("/api/dashboard/risk").json()
    assert [limit["key"] for limit in risk["limits"]] == [
        limit["key"] for limit in page["risk"]["limits"]
    ]
    assert client.get("/api/dashboard/system").json()["system_state"] == page["system_state"]


# ==========================================================================
# Secrets
# ==========================================================================


def test_no_response_can_carry_a_credential(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole payload is searched for the configured secrets, verbatim."""
    key = "PKTESTKEYVALUE0000000"
    secret = "sEcReTvAlUe000000000000000000000000000000"
    monkeypatch.setenv("ALPACA_API_KEY", key)
    monkeypatch.setenv("ALPACA_SECRET_KEY", secret)

    for path in (
        "/api/dashboard/overview",
        "/api/dashboard/positions",
        "/api/dashboard/orders",
        "/api/dashboard/risk",
        "/api/dashboard/system",
        "/api/dashboard/health",
    ):
        body = client.get(path).text
        assert key not in body, path
        assert secret not in body, path
        for forbidden in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "api_key", "secret"):
            assert forbidden not in body, f"{forbidden} in {path}"


def test_a_broker_failure_forwards_no_message_from_the_broker(populated: Path) -> None:
    """An auth error's text is the likeliest place for a key to appear."""
    leaky = api_error(401, "invalid key PKLEAKED0000 for account PA123456")
    page = overview(populated, read_broker(FakeTradingClient(account=leaky)))

    body = json.dumps(dataclasses.asdict(page))
    assert "PKLEAKED0000" not in body
    assert "PA123456" not in body
    assert page.broker is not None
    assert page.broker.status == "UNAVAILABLE"


def test_no_response_exposes_a_filesystem_path(client: TestClient, populated: Path) -> None:
    body = client.get("/api/dashboard/overview").text

    assert str(populated) not in body
    assert str(populated.parent) not in body


def test_the_default_binding_is_loopback() -> None:
    """Public exposure is a deployment concern and is not one flag away."""
    assert dashboard_api.DEFAULT_HOST == "127.0.0.1"
    for name, code in package_code().items():
        assert "0.0.0.0" not in code, f"a wildcard bind address appears in dashboard/{name}"


# ==========================================================================
# Source-level guarantees
# ==========================================================================


def test_the_wire_vocabulary_imports_only_the_standard_library() -> None:
    """Whatever describes the read model must not drag in a framework."""
    tree = ast.parse(Path(dashboard_models.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported <= {"__future__", "dataclasses"}


def test_the_dashboard_constructs_no_trading_client_of_its_own() -> None:
    """The one paper factory is called; no client is built here."""
    for name, code in package_code().items():
        assert "TradingClient(" not in code, name


def test_the_dashboard_imports_no_alpaca_module_directly() -> None:
    """One file in the repository speaks to Alpaca. This is not a second one.

    Not even for a type annotation: the broker client is typed structurally, so
    the concrete class that carries a submission method is never named here.
    """
    root = Path(dashboard.__file__).resolve().parent
    for path in package_code():
        tree = ast.parse((root / path).read_text(encoding="utf-8"))
        alpaca_imports = {
            (node.module or "")
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("alpaca")
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name.startswith("alpaca")
        }
        assert alpaca_imports == set(), f"dashboard/{path} imports {alpaca_imports}"


def test_the_broker_boundary_imports_only_read_helpers() -> None:
    """The import list is the audit: what is not named cannot be called."""
    from autotrader.dashboard import broker as dashboard_broker

    tree = ast.parse(Path(dashboard_broker.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "autotrader.execution.paper"
        for alias in node.names
    }

    assert imported == {
        "PaperAccountState",
        "PaperPosition",
        "create_paper_trading_client",
        "credentials_configured",
        "fetch_paper_account_state",
        "fetch_paper_positions",
    }


def test_the_dashboard_never_opens_a_writable_connection() -> None:
    """`state.connect` grants write access. This package must not call it."""
    for name, code in package_code().items():
        assert "state.connect(" not in code, f"a writable connection is opened in dashboard/{name}"
        assert "state.transaction(" not in code, f"a write transaction opens in dashboard/{name}"
