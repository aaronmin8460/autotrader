"""C8 tests: crash recovery, and the invariants that make it safe.

**Nothing here touches the network.** The Alpaca boundary is the only thing
faked, the fakes return *real* alpaca-py models so normalization runs against
real response shapes, no real credential is read, and a test asserts sockets
stay shut.

The fake trading client's `submit_order` does not merely record a call - it
raises. Reconciliation that placed an order would fail loudly here rather than
quietly pass a count assertion that someone later deleted.

The tests that matter most are the ones about not acting. An `UNKNOWN` intent
whose order the broker already has must never be sent again; one whose absence
cannot be established must leave everything alone and block startup; a stale
`CREATED` intent must be closed off rather than executed after a restart; and a
position mismatch must be repaired in the database, never by trading.
"""

from __future__ import annotations

import ast
import json
import socket
import sqlite3
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
from typer.testing import CliRunner

from autotrader import reconciliation
from autotrader.cli import app
from autotrader.execution.paper import (
    PAPER_TRADING_BASE_URL,
    NotPaperEnvironmentError,
    create_paper_trading_client,
    verify_paper_environment,
)
from autotrader.reconciliation import engine as reconciliation_engine
from autotrader.reconciliation import models as reconciliation_models
from autotrader.reconciliation.engine import (
    EVENT_RECONCILED,
    NOT_FOUND_CONFIRMATIONS,
    ReconciliationInputError,
    reconcile_paper_state,
)
from autotrader.reconciliation.models import (
    ItemOutcome,
    ReconciliationResult,
    ReconciliationStatus,
)
from autotrader.state.sqlite import (
    INTENT_STATUS_CONFIRMED_NOT_SUBMITTED,
    INTENT_STATUS_CREATED,
    INTENT_STATUS_REJECTED,
    INTENT_STATUS_SUBMITTED,
    INTENT_STATUS_SUBMITTING,
    INTENT_STATUS_UNKNOWN,
    connect,
    get_broker_order_by_intent,
    get_order_intent,
    get_position,
    initialize_database,
    latest_reconciliation_run,
    list_reconciliation_events,
    list_reconciliation_runs,
    list_system_events,
    record_order_intent,
    update_order_intent_status,
    upsert_broker_order,
    upsert_position,
)

T0 = datetime(2026, 3, 4, 9, 15, tzinfo=UTC)
BTC = "BTC/USD"
ETH = "ETH/USD"

#: A fixed broker order id. Alpaca's `Order.id` is a UUID, so the fakes need a
#: real one rather than a readable label.
BROKER_ORDER_UUID = "1f2b7c40-8a3d-4c19-9b52-7e6a0d3f5c81"

runner = CliRunner()


# --------------------------------------------------------------------------
# Source-level helpers
# --------------------------------------------------------------------------


def code_without_prose(source: str) -> str:
    """`source` with every docstring and comment removed.

    The guarantees below are about *executable code*. This package's own
    documentation explains what it must never do, so a naive substring scan
    would trip over the very sentences that state the rule.
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
    """Every module in the reconciliation package, prose stripped."""
    root = Path(reconciliation.__file__).resolve().parent
    return {
        str(path.relative_to(root)): code_without_prose(path.read_text())
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


def api_error(status_code: int | None, message: str = "broker said no") -> APIError:
    """An `APIError` shaped like the SDK's, optionally without a readable status."""
    body = json.dumps({"code": 40010001, "message": message})
    if status_code is None:
        return APIError(body)
    return APIError(body, _FakeHTTPError(status_code))


def not_found() -> APIError:
    """The broker's definitive "no order under this key"."""
    return api_error(404, "order not found")


def make_account(
    *,
    equity: str = "200000",
    cash: str = "200000",
    status: AccountStatus = AccountStatus.ACTIVE,
    trading_blocked: bool = False,
    account_blocked: bool = False,
    trade_suspended_by_user: bool = False,
) -> TradeAccount:
    return TradeAccount(
        id=uuid4(),
        account_number="PA0000000000",
        status=status,
        equity=equity,
        cash=cash,
        trading_blocked=trading_blocked,
        account_blocked=account_blocked,
        trade_suspended_by_user=trade_suspended_by_user,
    )


def make_position(
    symbol: str = BTC,
    *,
    qty: str = "0.0005",
    market_value: str = "50",
    avg_entry_price: str = "100000",
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
    qty: str | None = "0.001",
    filled_qty: str = "0",
    filled_avg_price: str | None = None,
    status: OrderStatus = OrderStatus.ACCEPTED,
    side: AlpacaOrderSide = AlpacaOrderSide.BUY,
    order_id: str = BROKER_ORDER_UUID,
    filled_at: datetime | None = None,
    updated_at: datetime = T0,
) -> Order:
    return Order(
        id=order_id,
        client_order_id=client_order_id,
        created_at=T0,
        updated_at=updated_at,
        submitted_at=T0,
        filled_at=filled_at,
        order_class=OrderClass.SIMPLE,
        time_in_force=TimeInForce.GTC,
        status=status,
        extended_hours=False,
        symbol=symbol,
        qty=qty,
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        side=side,
        order_type=OrderType.MARKET,
        type=OrderType.MARKET,
    )


class FakeTradingClient:
    """Stands in for `TradingClient`, and refuses to submit anything.

    `orders` maps a `client_order_id` to what the broker answers with: an
    `Order`, an exception to raise, or a *list* of either, consumed one per
    lookup with the last entry repeating. The list form is what lets a test
    describe a broker that answers differently on the bounded re-check.

    A key that is absent answers with Alpaca's 404 - the definitive
    "no such order" - which is the case reconciliation is allowed to act on.
    """

    def __init__(
        self,
        *,
        account: TradeAccount | BaseException | None = None,
        positions: list[Position] | BaseException | None = None,
        orders: dict[str, object] | None = None,
        base_url: str = PAPER_TRADING_BASE_URL,
        sandbox: bool = True,
    ) -> None:
        self._base_url = base_url
        self._sandbox = sandbox
        self._account = account if account is not None else make_account()
        self._positions = positions if positions is not None else []
        self._orders = dict(orders or {})
        self._consumed: dict[str, int] = {}
        self.lookup_calls: list[str] = []
        self.account_calls = 0
        self.position_calls = 0
        self.submit_calls: list[object] = []

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

    def get_order_by_client_id(self, client_id: str) -> Order:
        self.lookup_calls.append(client_id)
        answer = self._orders.get(client_id, not_found())
        if isinstance(answer, list):
            index = min(self._consumed.get(client_id, 0), len(answer) - 1)
            self._consumed[client_id] = index + 1
            answer = answer[index]
        if isinstance(answer, BaseException):
            raise answer
        return answer  # type: ignore[return-value]

    def submit_order(self, order_data: object) -> Order:
        self.submit_calls.append(order_data)
        raise AssertionError("reconciliation must never submit an order; it observes and repairs.")


def no_sleep(seconds: float) -> None:
    """Stand in for `time.sleep`, so the bounded re-check costs nothing."""
    del seconds


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return initialize_database(tmp_path / "state.db")


@pytest.fixture
def connection(database_path: Path):
    with connect(database_path) as open_connection:
        yield open_connection


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "test-key-never-real")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret-never-real")


def make_intent(
    connection: sqlite3.Connection,
    *,
    client_order_id: str = "autotrader-recovery-1",
    status: str = INTENT_STATUS_UNKNOWN,
    symbol: str = BTC,
    side: str = "BUY",
    approved: str = "0.001",
    created_at: datetime = T0,
) -> int:
    """One persisted intent in whatever lifecycle state a test needs."""
    return record_order_intent(
        connection,
        client_order_id=client_order_id,
        created_at=created_at,
        symbol=symbol,
        side=side,
        requested_quantity=Decimal(approved),
        approved_quantity=Decimal(approved),
        reference_price=100_000.0,
        risk_reason_code="APPROVED",
        status=status,
    )


def store_snapshot(
    connection: sqlite3.Connection,
    intent_id: int,
    *,
    client_order_id: str = "autotrader-recovery-1",
    symbol: str = BTC,
    side: str = "BUY",
    quantity: str = "0.001",
    filled_quantity: str = "0",
    filled_average_price: float | None = None,
    status: str = "accepted",
    broker_order_id: str = BROKER_ORDER_UUID,
    submitted_at: datetime | None = T0,
    filled_at: datetime | None = None,
) -> None:
    """A stale local broker snapshot, as a crashed process would have left it."""
    upsert_broker_order(
        connection,
        order_intent_id=intent_id,
        broker_order_id=broker_order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        quantity=Decimal(quantity),
        filled_quantity=Decimal(filled_quantity),
        filled_average_price=filled_average_price,
        status=status,
        submitted_at=submitted_at,
        filled_at=filled_at,
        updated_at=T0,
    )


def run(
    connection: sqlite3.Connection,
    client: FakeTradingClient,
    *,
    dry_run: bool = False,
    now: datetime = T0,
) -> ReconciliationResult:
    """One reconciliation pass with the bounded re-check made instantaneous."""
    return reconcile_paper_state(
        connection,
        trading_client=client,
        now=now,
        dry_run=dry_run,
        recheck_delay_seconds=0.0,
        sleep=no_sleep,
    )


# --------------------------------------------------------------------------
# Paper environment verification
# --------------------------------------------------------------------------


def test_the_real_client_factory_produces_a_provably_paper_client(credentials: None) -> None:
    """Ties the private-attribute check to the SDK the repository installs.

    Constructing a client opens no socket, so this runs offline. If a future
    alpaca-py renamed what `verify_paper_environment` reads, this fails rather
    than the check silently starting to pass everything.
    """
    assert verify_paper_environment(create_paper_trading_client()) == PAPER_TRADING_BASE_URL


def test_a_live_base_url_is_refused() -> None:
    client = FakeTradingClient(base_url="https://api.alpaca.markets")

    with pytest.raises(NotPaperEnvironmentError):
        verify_paper_environment(client)


def test_a_client_whose_environment_cannot_be_read_is_refused() -> None:
    """Unproven is refused, exactly like proven-live. There is no benefit of the doubt."""

    class Opaque:
        pass

    with pytest.raises(NotPaperEnvironmentError):
        verify_paper_environment(Opaque())  # type: ignore[arg-type]


def test_a_paper_url_without_the_sandbox_flag_is_refused() -> None:
    client = FakeTradingClient(sandbox=False)

    with pytest.raises(NotPaperEnvironmentError):
        verify_paper_environment(client)


def test_paper_environment_verification_failure_blocks_startup(
    connection: sqlite3.Connection,
) -> None:
    client = FakeTradingClient(base_url="https://api.alpaca.markets")

    result = run(connection, client)

    assert result.status is ReconciliationStatus.FAILED
    assert result.safe_to_trade is False
    assert client.account_calls == 0
    assert client.submit_calls == []


def test_verification_happens_before_any_local_write(connection: sqlite3.Connection) -> None:
    """A client that cannot be proven paper never gets to change local state."""
    intent_id = make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(sandbox=False)

    run(connection, client)

    stored = get_order_intent(connection, intent_id)
    assert stored is not None
    assert stored.status == INTENT_STATUS_UNKNOWN


# --------------------------------------------------------------------------
# UNKNOWN recovery - the case this phase exists for
# --------------------------------------------------------------------------


def test_unknown_order_found_by_client_order_id_is_never_resubmitted(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL REGRESSION #1.

    An intent whose submission outcome was unknown, and whose order the broker
    actually has. The recovery anchor must be the *same* `client_order_id` that
    was committed before the request went out, the broker snapshot must be
    repaired from what the broker says, and nothing may be submitted.
    """
    intent_id = make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(
        orders={"autotrader-recovery-1": make_order(client_order_id="autotrader-recovery-1")}
    )

    result = run(connection, client)

    assert client.lookup_calls == ["autotrader-recovery-1"]
    assert client.submit_calls == []
    assert result.status is ReconciliationStatus.REPAIRED
    assert result.safe_to_trade is True

    stored = get_order_intent(connection, intent_id)
    snapshot = get_broker_order_by_intent(connection, intent_id)
    assert stored is not None and stored.status == INTENT_STATUS_SUBMITTED
    assert stored.client_order_id == "autotrader-recovery-1"
    assert snapshot is not None
    assert snapshot.broker_order_id == BROKER_ORDER_UUID
    assert snapshot.client_order_id == "autotrader-recovery-1"
    assert snapshot.status == "accepted"


def test_unknown_lookup_failure_blocks_startup(connection: sqlite3.Connection) -> None:
    """CRITICAL REGRESSION #2.

    A lookup that times out answers nothing. The intent stays exactly as it
    was, trading is blocked, and no order is sent to "resolve" it.
    """
    intent_id = make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(orders={"autotrader-recovery-1": TimeoutError("read timed out")})

    result = run(connection, client)

    assert result.status in (ReconciliationStatus.UNRESOLVED, ReconciliationStatus.FAILED)
    assert result.safe_to_trade is False
    assert result.unresolved_count == 1
    assert client.submit_calls == []

    stored = get_order_intent(connection, intent_id)
    assert stored is not None and stored.status == INTENT_STATUS_UNKNOWN


def test_unknown_with_a_filled_broker_order_records_the_fill(
    connection: sqlite3.Connection,
) -> None:
    intent_id = make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(
        orders={
            "autotrader-recovery-1": make_order(
                client_order_id="autotrader-recovery-1",
                status=OrderStatus.FILLED,
                filled_qty="0.001",
                filled_avg_price="99500.25",
                filled_at=T0 + timedelta(seconds=3),
            )
        }
    )

    result = run(connection, client)

    snapshot = get_broker_order_by_intent(connection, intent_id)
    assert result.safe_to_trade is True
    assert snapshot is not None
    assert snapshot.status == "filled"
    assert snapshot.filled_quantity == Decimal("0.001")
    assert snapshot.filled_average_price == 99_500.25
    assert snapshot.filled_at == T0 + timedelta(seconds=3)
    assert client.submit_calls == []


def test_unknown_with_a_rejected_broker_order_records_the_rejection(
    connection: sqlite3.Connection,
) -> None:
    intent_id = make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(
        orders={
            "autotrader-recovery-1": make_order(
                client_order_id="autotrader-recovery-1", status=OrderStatus.REJECTED
            )
        }
    )

    result = run(connection, client)

    snapshot = get_broker_order_by_intent(connection, intent_id)
    assert result.status is ReconciliationStatus.REPAIRED
    assert snapshot is not None and snapshot.status == "rejected"
    assert snapshot.filled_quantity == Decimal(0)
    assert client.submit_calls == []


def test_unknown_with_a_canceled_broker_order_records_the_cancellation(
    connection: sqlite3.Connection,
) -> None:
    intent_id = make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(
        orders={
            "autotrader-recovery-1": make_order(
                client_order_id="autotrader-recovery-1", status=OrderStatus.CANCELED
            )
        }
    )

    run(connection, client)

    snapshot = get_broker_order_by_intent(connection, intent_id)
    assert snapshot is not None and snapshot.status == "canceled"
    assert client.submit_calls == []


def test_a_confirmed_absent_unknown_intent_becomes_terminal(
    connection: sqlite3.Connection,
) -> None:
    """The broker definitively has no such order, confirmed more than once.

    The decision is closed off rather than executed: a signal from before the
    crash must not become an order now.
    """
    intent_id = make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient()

    result = run(connection, client)

    assert client.lookup_calls == ["autotrader-recovery-1"] * NOT_FOUND_CONFIRMATIONS
    assert result.status is ReconciliationStatus.REPAIRED
    assert result.safe_to_trade is True
    stored = get_order_intent(connection, intent_id)
    assert stored is not None
    assert stored.status == INTENT_STATUS_CONFIRMED_NOT_SUBMITTED
    assert client.submit_calls == []


def test_one_not_found_is_never_enough_on_its_own(connection: sqlite3.Connection) -> None:
    """A single 404 could be a lookup that overtook a submission in flight."""
    intent_id = make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(
        orders={
            "autotrader-recovery-1": [
                not_found(),
                make_order(client_order_id="autotrader-recovery-1"),
            ]
        }
    )

    result = run(connection, client)

    stored = get_order_intent(connection, intent_id)
    assert stored is not None and stored.status == INTENT_STATUS_SUBMITTED
    assert result.safe_to_trade is True
    assert client.submit_calls == []


def test_an_ambiguous_recheck_after_a_not_found_stays_unresolved(
    connection: sqlite3.Connection,
) -> None:
    """Every read in the bounded confirmation has to agree, or nothing is concluded."""
    intent_id = make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(
        orders={"autotrader-recovery-1": [not_found(), api_error(503, "gateway")]}
    )

    result = run(connection, client)

    assert result.status is ReconciliationStatus.UNRESOLVED
    assert result.safe_to_trade is False
    stored = get_order_intent(connection, intent_id)
    assert stored is not None and stored.status == INTENT_STATUS_UNKNOWN


def test_the_absence_check_is_bounded_and_does_not_poll(
    connection: sqlite3.Connection,
) -> None:
    """Fixed number of reads, fixed pauses. No growing backoff, no waiting loop."""
    make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient()
    slept: list[float] = []

    reconcile_paper_state(
        connection,
        trading_client=client,
        now=T0,
        confirmations=3,
        recheck_delay_seconds=0.5,
        sleep=slept.append,
    )

    assert len(client.lookup_calls) == 3
    assert slept == [0.5, 0.5]


def test_a_confirmation_count_below_one_is_refused(connection: sqlite3.Connection) -> None:
    with pytest.raises(ReconciliationInputError):
        reconcile_paper_state(connection, trading_client=FakeTradingClient(), confirmations=0)


def test_a_negative_recheck_delay_is_refused(connection: sqlite3.Connection) -> None:
    with pytest.raises(ReconciliationInputError):
        reconcile_paper_state(
            connection, trading_client=FakeTradingClient(), recheck_delay_seconds=-1.0
        )


def test_a_broker_order_denied_after_it_was_recorded_is_not_erased(
    connection: sqlite3.Connection,
) -> None:
    """Local evidence is never deleted because a later read disagreed with it.

    A stored snapshot means the broker once acknowledged this order. If it now
    denies it, that contradiction is reported - the snapshot stays, and trading
    stops until a human settles it.
    """
    intent_id = make_intent(connection, status=INTENT_STATUS_SUBMITTED)
    store_snapshot(connection, intent_id)
    client = FakeTradingClient()

    result = run(connection, client)

    assert result.status is ReconciliationStatus.UNRESOLVED
    assert result.safe_to_trade is False
    snapshot = get_broker_order_by_intent(connection, intent_id)
    assert snapshot is not None and snapshot.broker_order_id == BROKER_ORDER_UUID
    stored = get_order_intent(connection, intent_id)
    assert stored is not None and stored.status == INTENT_STATUS_SUBMITTED


def test_an_order_belonging_to_another_intent_is_not_copied_in(
    connection: sqlite3.Connection,
) -> None:
    """A response that names a different key is not evidence about this intent."""
    intent_id = make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(
        orders={"autotrader-recovery-1": make_order(client_order_id="autotrader-someone-else")}
    )

    result = run(connection, client)

    assert result.status is ReconciliationStatus.UNRESOLVED
    assert get_broker_order_by_intent(connection, intent_id) is None


def test_an_order_for_the_wrong_market_is_not_copied_in(
    connection: sqlite3.Connection,
) -> None:
    intent_id = make_intent(connection, status=INTENT_STATUS_UNKNOWN, symbol=BTC)
    client = FakeTradingClient(
        orders={
            "autotrader-recovery-1": make_order(client_order_id="autotrader-recovery-1", symbol=ETH)
        }
    )

    result = run(connection, client)

    assert result.status is ReconciliationStatus.UNRESOLVED
    assert get_broker_order_by_intent(connection, intent_id) is None


def test_an_order_for_the_wrong_side_is_not_copied_in(connection: sqlite3.Connection) -> None:
    intent_id = make_intent(connection, status=INTENT_STATUS_UNKNOWN, side="BUY")
    client = FakeTradingClient(
        orders={
            "autotrader-recovery-1": make_order(
                client_order_id="autotrader-recovery-1", side=AlpacaOrderSide.SELL
            )
        }
    )

    result = run(connection, client)

    assert result.status is ReconciliationStatus.UNRESOLVED
    assert get_broker_order_by_intent(connection, intent_id) is None


# --------------------------------------------------------------------------
# CREATED and SUBMITTING: crash before, and during, the request
# --------------------------------------------------------------------------


def test_a_created_intent_with_a_broker_order_is_repaired_not_resubmitted(
    connection: sqlite3.Connection,
) -> None:
    """The order reached the broker even though local state never learned it did."""
    intent_id = make_intent(connection, status=INTENT_STATUS_CREATED)
    client = FakeTradingClient(
        orders={"autotrader-recovery-1": make_order(client_order_id="autotrader-recovery-1")}
    )

    result = run(connection, client)

    stored = get_order_intent(connection, intent_id)
    assert stored is not None and stored.status == INTENT_STATUS_SUBMITTED
    assert get_broker_order_by_intent(connection, intent_id) is not None
    assert result.safe_to_trade is True
    assert client.submit_calls == []


def test_a_created_intent_the_broker_never_saw_is_closed_off(
    connection: sqlite3.Connection,
) -> None:
    """The crash happened before submission. The stale decision is not executed later."""
    intent_id = make_intent(connection, status=INTENT_STATUS_CREATED)
    client = FakeTradingClient()

    result = run(connection, client)

    stored = get_order_intent(connection, intent_id)
    assert stored is not None
    assert stored.status == INTENT_STATUS_CONFIRMED_NOT_SUBMITTED
    assert result.status is ReconciliationStatus.REPAIRED
    assert client.submit_calls == []


def test_a_created_intent_is_never_automatically_submitted(
    connection: sqlite3.Connection,
) -> None:
    make_intent(connection, status=INTENT_STATUS_CREATED)
    client = FakeTradingClient()

    run(connection, client)

    assert client.submit_calls == []


def test_a_submitting_intent_is_treated_as_ambiguous_not_as_unsent(
    connection: sqlite3.Connection,
) -> None:
    """A process that died mid-call may or may not have reached the broker."""
    intent_id = make_intent(connection, status=INTENT_STATUS_SUBMITTING)
    client = FakeTradingClient(
        orders={"autotrader-recovery-1": make_order(client_order_id="autotrader-recovery-1")}
    )

    run(connection, client)

    stored = get_order_intent(connection, intent_id)
    assert stored is not None and stored.status == INTENT_STATUS_SUBMITTED
    assert client.submit_calls == []


def test_a_rejected_intent_is_terminal_and_is_not_queried(
    connection: sqlite3.Connection,
) -> None:
    """The broker refused it outright, so there is nothing to ask about."""
    make_intent(connection, status=INTENT_STATUS_REJECTED)
    client = FakeTradingClient()

    result = run(connection, client)

    assert client.lookup_calls == []
    assert result.orders_checked == 0
    assert result.status is ReconciliationStatus.CLEAN


def test_a_confirmed_not_submitted_intent_is_never_queried_again(
    connection: sqlite3.Connection,
) -> None:
    intent_id = make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    update_order_intent_status(
        connection,
        order_intent_id=intent_id,
        status=INTENT_STATUS_CONFIRMED_NOT_SUBMITTED,
        updated_at=T0,
    )
    client = FakeTradingClient()

    result = run(connection, client)

    assert client.lookup_calls == []
    assert result.orders_checked == 0


# --------------------------------------------------------------------------
# Snapshot repair for orders already known to be submitted
# --------------------------------------------------------------------------


def test_accepted_locally_and_filled_at_the_broker_becomes_filled(
    connection: sqlite3.Connection,
) -> None:
    intent_id = make_intent(connection, status=INTENT_STATUS_SUBMITTED)
    store_snapshot(connection, intent_id, status="accepted")
    client = FakeTradingClient(
        orders={
            "autotrader-recovery-1": make_order(
                client_order_id="autotrader-recovery-1",
                status=OrderStatus.FILLED,
                filled_qty="0.001",
                filled_avg_price="101000.5",
                filled_at=T0 + timedelta(minutes=1),
            )
        }
    )

    result = run(connection, client)

    snapshot = get_broker_order_by_intent(connection, intent_id)
    assert result.status is ReconciliationStatus.REPAIRED
    assert snapshot is not None
    assert snapshot.status == "filled"
    assert snapshot.filled_quantity == Decimal("0.001")
    assert snapshot.filled_average_price == 101_000.5
    assert client.submit_calls == []


def test_accepted_locally_and_partially_filled_at_the_broker_stays_partial(
    connection: sqlite3.Connection,
) -> None:
    """A partial fill must never round up into a full one."""
    intent_id = make_intent(connection, status=INTENT_STATUS_SUBMITTED)
    store_snapshot(connection, intent_id, status="accepted")
    client = FakeTradingClient(
        orders={
            "autotrader-recovery-1": make_order(
                client_order_id="autotrader-recovery-1",
                status=OrderStatus.PARTIALLY_FILLED,
                qty="0.001",
                filled_qty="0.0004",
                filled_avg_price="100200.75",
            )
        }
    )

    run(connection, client)

    snapshot = get_broker_order_by_intent(connection, intent_id)
    assert snapshot is not None
    assert snapshot.status == "partially_filled"
    assert snapshot.quantity == Decimal("0.001")
    assert snapshot.filled_quantity == Decimal("0.0004")
    assert snapshot.filled_quantity != snapshot.quantity


def test_a_partial_fill_quantity_is_preserved_exactly(
    connection: sqlite3.Connection,
) -> None:
    """Decimal in, Decimal out: 0.0004 is 0.0004, not the nearest double."""
    intent_id = make_intent(connection, status=INTENT_STATUS_SUBMITTED, approved="0.001")
    store_snapshot(connection, intent_id, status="accepted")
    client = FakeTradingClient(
        orders={
            "autotrader-recovery-1": make_order(
                client_order_id="autotrader-recovery-1",
                status=OrderStatus.PARTIALLY_FILLED,
                qty="0.001",
                filled_qty="0.00040000",
            )
        }
    )

    run(connection, client)

    snapshot = get_broker_order_by_intent(connection, intent_id)
    assert snapshot is not None
    assert snapshot.filled_quantity == Decimal("0.0004")
    assert str(snapshot.filled_quantity) == "0.00040000"


def test_a_partially_filled_order_is_re_read_on_the_next_pass(
    connection: sqlite3.Connection,
) -> None:
    """`partially_filled` can still fill, so it is never treated as settled."""
    intent_id = make_intent(connection, status=INTENT_STATUS_SUBMITTED)
    store_snapshot(connection, intent_id, status="partially_filled", filled_quantity="0.0004")
    client = FakeTradingClient(
        orders={
            "autotrader-recovery-1": make_order(
                client_order_id="autotrader-recovery-1",
                status=OrderStatus.FILLED,
                filled_qty="0.001",
                filled_avg_price="100000",
                filled_at=T0,
            )
        }
    )

    run(connection, client)

    snapshot = get_broker_order_by_intent(connection, intent_id)
    assert snapshot is not None and snapshot.status == "filled"


def test_a_filled_average_price_is_preserved_when_the_broker_gives_one(
    connection: sqlite3.Connection,
) -> None:
    intent_id = make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(
        orders={
            "autotrader-recovery-1": make_order(
                client_order_id="autotrader-recovery-1",
                status=OrderStatus.FILLED,
                filled_qty="0.001",
                filled_avg_price="98765.4321",
            )
        }
    )

    run(connection, client)

    snapshot = get_broker_order_by_intent(connection, intent_id)
    assert snapshot is not None and snapshot.filled_average_price == 98_765.4321


def test_a_missing_fill_price_is_not_invented(connection: sqlite3.Connection) -> None:
    """No reference price, no average of anything: absent stays absent."""
    intent_id = make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(
        orders={
            "autotrader-recovery-1": make_order(
                client_order_id="autotrader-recovery-1",
                status=OrderStatus.PARTIALLY_FILLED,
                filled_qty="0.0002",
                filled_avg_price=None,
            )
        }
    )

    run(connection, client)

    snapshot = get_broker_order_by_intent(connection, intent_id)
    assert snapshot is not None
    assert snapshot.filled_average_price is None
    assert snapshot.filled_quantity == Decimal("0.0002")


def test_submitted_is_not_filled_during_reconciliation(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL REGRESSION #3.

    A submitted, unfilled order stays unfilled, and no position is conjured out
    of the fact that an order exists.
    """
    intent_id = make_intent(connection, status=INTENT_STATUS_SUBMITTED)
    store_snapshot(connection, intent_id, status="accepted", filled_quantity="0")
    client = FakeTradingClient(
        orders={
            "autotrader-recovery-1": make_order(
                client_order_id="autotrader-recovery-1",
                status=OrderStatus.ACCEPTED,
                qty="0.001",
                filled_qty="0",
            )
        }
    )

    result = run(connection, client)

    snapshot = get_broker_order_by_intent(connection, intent_id)
    assert snapshot is not None
    assert snapshot.filled_quantity == Decimal(0)
    assert snapshot.filled_average_price is None
    # The broker holds no position, so neither may the local snapshot.
    assert get_position(connection, BTC) is None
    assert result.safe_to_trade is True
    assert client.submit_calls == []


def test_a_matching_snapshot_is_left_untouched(connection: sqlite3.Connection) -> None:
    intent_id = make_intent(connection, status=INTENT_STATUS_SUBMITTED)
    store_snapshot(connection, intent_id, status="accepted")
    client = FakeTradingClient(
        orders={"autotrader-recovery-1": make_order(client_order_id="autotrader-recovery-1")}
    )

    result = run(connection, client)

    assert result.status is ReconciliationStatus.CLEAN
    assert result.orders_checked == 1
    assert result.issues == ()


def test_a_settled_broker_order_is_not_queried_again(connection: sqlite3.Connection) -> None:
    """A filled order cannot change, so startup does not spend a call on it."""
    intent_id = make_intent(connection, status=INTENT_STATUS_SUBMITTED)
    store_snapshot(
        connection,
        intent_id,
        status="filled",
        filled_quantity="0.001",
        filled_average_price=100_000.0,
        filled_at=T0,
    )
    client = FakeTradingClient()

    result = run(connection, client)

    assert client.lookup_calls == []
    assert result.orders_checked == 0
    assert result.status is ReconciliationStatus.CLEAN


# --------------------------------------------------------------------------
# Positions: the broker is authoritative in both directions
# --------------------------------------------------------------------------


def test_broker_position_is_authoritative_over_local_snapshot(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL REGRESSION #4.

    Local believes it is flat; the broker holds a fractional position. After
    reconciliation the local snapshot equals the broker's exactly, and nothing
    was traded to make that true.
    """
    upsert_position(connection, symbol=BTC, quantity=Decimal(0), updated_at=T0)
    client = FakeTradingClient(positions=[make_position(BTC, qty="0.0005")])

    result = run(connection, client)

    position = get_position(connection, BTC)
    assert position is not None
    assert position.quantity == Decimal("0.0005")
    assert result.status is ReconciliationStatus.REPAIRED
    assert result.safe_to_trade is True
    assert client.submit_calls == []


def test_a_position_the_broker_no_longer_holds_goes_to_zero(
    connection: sqlite3.Connection,
) -> None:
    upsert_position(
        connection, symbol=ETH, quantity=Decimal("0.2"), average_price=3000.0, updated_at=T0
    )
    client = FakeTradingClient(positions=[])

    result = run(connection, client)

    position = get_position(connection, ETH)
    assert position is not None
    assert position.quantity == Decimal(0)
    assert position.average_price is None
    assert result.status is ReconciliationStatus.REPAIRED
    assert client.submit_calls == []


def test_btc_and_eth_are_reconciled_independently(connection: sqlite3.Connection) -> None:
    upsert_position(connection, symbol=ETH, quantity=Decimal("0.2"), updated_at=T0)
    client = FakeTradingClient(positions=[make_position(BTC, qty="0.0005")])

    result = run(connection, client)

    btc = get_position(connection, BTC)
    eth = get_position(connection, ETH)
    assert btc is not None and btc.quantity == Decimal("0.0005")
    assert eth is not None and eth.quantity == Decimal(0)
    assert result.positions_checked == 2


def test_a_fractional_position_quantity_round_trips_exactly(
    connection: sqlite3.Connection,
) -> None:
    client = FakeTradingClient(positions=[make_position(BTC, qty="0.000123456789")])

    run(connection, client)

    position = get_position(connection, BTC)
    assert position is not None
    assert position.quantity == Decimal("0.000123456789")
    assert str(position.quantity) == "0.000123456789"


def test_a_short_broker_position_fails_the_whole_pass(
    connection: sqlite3.Connection,
) -> None:
    """This system is long only, so a short is state it cannot reason about."""
    client = FakeTradingClient(
        positions=[make_position(BTC, qty="0.0005", side=PositionSide.SHORT)]
    )

    result = run(connection, client)

    assert result.status is ReconciliationStatus.FAILED
    assert result.safe_to_trade is False
    assert get_position(connection, BTC) is None


def test_no_short_position_can_be_written_locally(connection: sqlite3.Connection) -> None:
    """Belt and braces: the column itself refuses a negative quantity."""
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO positions (symbol, quantity, average_price, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (BTC, "-0.5", None, "2026-03-04T09:15:00.000000+00:00"),
        )


def test_matching_positions_are_left_alone(connection: sqlite3.Connection) -> None:
    upsert_position(
        connection,
        symbol=BTC,
        quantity=Decimal("0.0005"),
        average_price=100_000.0,
        updated_at=T0,
    )
    client = FakeTradingClient(positions=[make_position(BTC, qty="0.0005")])

    result = run(connection, client)

    assert result.status is ReconciliationStatus.CLEAN
    assert result.issues == ()


def test_a_flat_pair_with_no_local_row_needs_no_repair(
    connection: sqlite3.Connection,
) -> None:
    """ "No snapshot" and "flat" are different claims with nothing between them."""
    client = FakeTradingClient(positions=[])

    result = run(connection, client)

    assert result.status is ReconciliationStatus.CLEAN
    assert get_position(connection, BTC) is None
    assert get_position(connection, ETH) is None


def test_a_position_outside_the_universe_is_recorded_but_does_not_block(
    connection: sqlite3.Connection,
) -> None:
    """Real broker exposure this system does not manage. Noted, never traded out of."""
    client = FakeTradingClient(
        positions=[make_position("SOL/USD", qty="3", avg_entry_price="150", market_value="450")]
    )

    result = run(connection, client)

    observed = [issue for issue in result.issues if issue.outcome is ItemOutcome.OBSERVED]
    assert len(observed) == 1
    assert "SOL/USD" in observed[0].detail
    assert result.status is ReconciliationStatus.CLEAN
    assert result.safe_to_trade is True
    assert client.submit_calls == []


def test_a_position_is_never_derived_from_an_order_intent(
    connection: sqlite3.Connection,
) -> None:
    """An intent for 0.001 BTC, an accepted order, and a broker that is flat."""
    intent_id = make_intent(connection, status=INTENT_STATUS_SUBMITTED)
    store_snapshot(connection, intent_id, status="accepted")
    client = FakeTradingClient(
        positions=[],
        orders={"autotrader-recovery-1": make_order(client_order_id="autotrader-recovery-1")},
    )

    run(connection, client)

    assert get_position(connection, BTC) is None


# --------------------------------------------------------------------------
# Broker read failures
# --------------------------------------------------------------------------


def test_an_authentication_failure_blocks_startup(connection: sqlite3.Connection) -> None:
    client = FakeTradingClient(account=api_error(401, "unauthorized"))

    result = run(connection, client)

    assert result.status is ReconciliationStatus.FAILED
    assert result.safe_to_trade is False
    assert client.position_calls == 0


def test_a_position_read_failure_blocks_startup(connection: sqlite3.Connection) -> None:
    client = FakeTradingClient(positions=TimeoutError("read timed out"))

    result = run(connection, client)

    assert result.status is ReconciliationStatus.FAILED
    assert result.safe_to_trade is False


def test_a_malformed_broker_order_blocks_startup(connection: sqlite3.Connection) -> None:
    """An order with no quantity cannot be normalized, so it is not guessed at."""
    intent_id = make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(
        orders={
            "autotrader-recovery-1": make_order(client_order_id="autotrader-recovery-1", qty=None)
        }
    )

    result = run(connection, client)

    assert result.status is ReconciliationStatus.UNRESOLVED
    assert result.safe_to_trade is False
    assert get_broker_order_by_intent(connection, intent_id) is None


def test_an_unreadable_account_shape_blocks_startup(connection: sqlite3.Connection) -> None:
    class NotAnAccount:
        pass

    client = FakeTradingClient()
    client._account = NotAnAccount()  # type: ignore[assignment]

    result = run(connection, client)

    assert result.status is ReconciliationStatus.FAILED
    assert result.safe_to_trade is False


def test_a_non_tradable_account_is_recorded_without_blocking(
    connection: sqlite3.Connection,
) -> None:
    """Reconciliation answers whether local state matches the broker.

    A blocked account does not make them disagree, and execution checks
    tradability again immediately before submitting anything.
    """
    client = FakeTradingClient(account=make_account(trading_blocked=True))

    result = run(connection, client)

    observed = [issue for issue in result.issues if issue.outcome is ItemOutcome.OBSERVED]
    assert len(observed) == 1
    assert result.safe_to_trade is True


def test_one_unresolved_order_does_not_stop_the_rest_of_the_pass(
    connection: sqlite3.Connection,
) -> None:
    """A runtime is better served by every problem than by the first one."""
    make_intent(connection, client_order_id="autotrader-a", status=INTENT_STATUS_UNKNOWN)
    make_intent(connection, client_order_id="autotrader-b", status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(
        orders={
            "autotrader-a": TimeoutError("read timed out"),
            "autotrader-b": make_order(client_order_id="autotrader-b"),
        },
        positions=[make_position(BTC, qty="0.0005")],
    )

    result = run(connection, client)

    assert result.orders_checked == 2
    assert result.status is ReconciliationStatus.UNRESOLVED
    assert result.unresolved_count == 1
    assert result.repaired_count == 2
    assert get_position(connection, BTC) is not None


# --------------------------------------------------------------------------
# Status and the safe_to_trade contract
# --------------------------------------------------------------------------


def test_a_clean_pass_permits_trading(connection: sqlite3.Connection) -> None:
    result = run(connection, FakeTradingClient())

    assert result.status is ReconciliationStatus.CLEAN
    assert result.safe_to_trade is True


def test_a_repaired_pass_permits_trading(connection: sqlite3.Connection) -> None:
    client = FakeTradingClient(positions=[make_position(BTC, qty="0.0005")])

    result = run(connection, client)

    assert result.status is ReconciliationStatus.REPAIRED
    assert result.safe_to_trade is True


@pytest.mark.parametrize(
    "status",
    [ReconciliationStatus.UNRESOLVED, ReconciliationStatus.FAILED],
)
def test_an_unsettled_pass_never_permits_trading(status: ReconciliationStatus) -> None:
    result = ReconciliationResult(status=status, started_at=T0, completed_at=T0)

    assert result.safe_to_trade is False


@pytest.mark.parametrize("status", [ReconciliationStatus.CLEAN, ReconciliationStatus.REPAIRED])
def test_a_settled_pass_permits_trading(status: ReconciliationStatus) -> None:
    result = ReconciliationResult(status=status, started_at=T0, completed_at=T0)

    assert result.safe_to_trade is True


def test_safe_to_trade_cannot_disagree_with_status() -> None:
    """It is derived, not stored, so the two are one fact rather than two."""
    assert "safe_to_trade" not in {field for field in ReconciliationResult.__dataclass_fields__}
    for status in ReconciliationStatus:
        result = ReconciliationResult(status=status, started_at=T0, completed_at=T0)
        assert result.safe_to_trade == (status in reconciliation_models.SAFE_TO_TRADE_STATUSES)


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_repeated_reconciliation_is_idempotent(connection: sqlite3.Connection) -> None:
    intent_id = make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(
        orders={"autotrader-recovery-1": make_order(client_order_id="autotrader-recovery-1")},
        positions=[make_position(BTC, qty="0.0005")],
    )

    first = run(connection, client)
    snapshot_after_first = get_broker_order_by_intent(connection, intent_id)
    position_after_first = get_position(connection, BTC)
    second = run(connection, client)

    assert first.status is ReconciliationStatus.REPAIRED
    assert second.status is ReconciliationStatus.CLEAN
    assert second.safe_to_trade is True
    assert get_broker_order_by_intent(connection, intent_id) == snapshot_after_first
    assert get_position(connection, BTC) == position_after_first


def test_a_second_pass_after_a_confirmed_absence_is_clean(
    connection: sqlite3.Connection,
) -> None:
    make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient()

    first = run(connection, client)
    lookups_after_first = len(client.lookup_calls)
    second = run(connection, client)

    assert first.status is ReconciliationStatus.REPAIRED
    assert second.status is ReconciliationStatus.CLEAN
    assert len(client.lookup_calls) == lookups_after_first


def test_a_third_pass_still_reports_clean(connection: sqlite3.Connection) -> None:
    client = FakeTradingClient(positions=[make_position(BTC, qty="0.0005")])

    run(connection, client)
    run(connection, client)
    third = run(connection, client)

    assert third.status is ReconciliationStatus.CLEAN


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


def test_a_pass_records_what_it_concluded(connection: sqlite3.Connection) -> None:
    make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(positions=[make_position(BTC, qty="0.0005")])

    result = run(connection, client)

    runs = list_reconciliation_runs(connection)
    assert len(runs) == 1
    stored = runs[0]
    assert stored.id == result.reconciliation_run_id
    assert stored.status == "REPAIRED"
    assert stored.safe_to_trade is True
    assert stored.orders_checked == 1
    assert stored.positions_checked == 2
    assert stored.issues_count == result.issues_count
    assert stored.unresolved_count == 0
    assert stored.started_at == T0
    assert stored.completed_at == T0
    assert latest_reconciliation_run(connection) == stored


def test_the_audit_says_which_order_and_which_position_changed(
    connection: sqlite3.Connection,
) -> None:
    make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(positions=[make_position(BTC, qty="0.0005")])

    result = run(connection, client)

    events = list_reconciliation_events(connection, result.reconciliation_run_id)
    orders = [event for event in events if event.category == "ORDER"]
    positions = [event for event in events if event.category == "POSITION"]
    assert [event.client_order_id for event in orders] == ["autotrader-recovery-1"]
    assert [event.symbol for event in positions] == [BTC]
    assert all(event.detail for event in events)


def test_the_audit_says_why_an_item_was_unresolved(connection: sqlite3.Connection) -> None:
    make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(orders={"autotrader-recovery-1": TimeoutError("read timed out")})

    result = run(connection, client)

    events = list_reconciliation_events(connection, result.reconciliation_run_id)
    unresolved = [event for event in events if event.outcome == "UNRESOLVED"]
    assert len(unresolved) == 1
    assert unresolved[0].client_order_id == "autotrader-recovery-1"
    assert unresolved[0].symbol == BTC
    assert "could not be asked about it conclusively" in unresolved[0].detail
    assert "Nothing was changed" in unresolved[0].detail


def test_a_clean_pass_writes_a_run_row_and_no_noise(connection: sqlite3.Connection) -> None:
    """Every stored event has to mean something, or the table stops being readable."""
    result = run(connection, FakeTradingClient())

    assert len(list_reconciliation_runs(connection)) == 1
    assert list_reconciliation_events(connection, result.reconciliation_run_id) == []


def test_a_failed_pass_is_still_recorded(connection: sqlite3.Connection) -> None:
    client = FakeTradingClient(positions=TimeoutError("read timed out"))

    run(connection, client)

    runs = list_reconciliation_runs(connection)
    assert len(runs) == 1
    assert runs[0].status == "FAILED"
    assert runs[0].safe_to_trade is False


def test_a_pass_appears_in_the_operational_event_stream(
    connection: sqlite3.Connection,
) -> None:
    run(connection, FakeTradingClient())

    events = [
        event for event in list_system_events(connection) if event.event_type == EVENT_RECONCILED
    ]
    assert len(events) == 1
    assert events[0].message is not None
    assert "safe_to_trade=true" in events[0].message
    assert "No order was submitted" in events[0].message


def test_no_credential_is_ever_persisted(
    connection: sqlite3.Connection, credentials: None, database_path: Path
) -> None:
    """The whole file is scanned, not just the tables reconciliation writes."""
    make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(positions=[make_position(BTC, qty="0.0005")])

    run(connection, client)

    dumped = "\n".join(connection.iterdump())
    assert "test-key-never-real" not in dumped
    assert "test-secret-never-real" not in dumped
    assert "ALPACA_API_KEY" not in dumped
    assert "ALPACA_SECRET_KEY" not in dumped


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------


def test_a_dry_run_changes_nothing(connection: sqlite3.Connection) -> None:
    intent_id = make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    upsert_position(connection, symbol=ETH, quantity=Decimal("0.2"), updated_at=T0)
    client = FakeTradingClient(positions=[make_position(BTC, qty="0.0005")])

    result = run(connection, client, dry_run=True)

    stored = get_order_intent(connection, intent_id)
    assert stored is not None and stored.status == INTENT_STATUS_UNKNOWN
    assert get_position(connection, BTC) is None
    eth = get_position(connection, ETH)
    assert eth is not None and eth.quantity == Decimal("0.2")
    assert list_reconciliation_runs(connection) == []
    assert list_reconciliation_events(connection) == []
    assert result.reconciliation_run_id is None
    assert result.dry_run is True


def test_a_dry_run_reports_the_same_findings_as_a_real_pass(
    connection: sqlite3.Connection,
) -> None:
    make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(positions=[make_position(BTC, qty="0.0005")])

    preview = run(connection, client, dry_run=True)
    real = run(connection, client)

    assert preview.status == real.status
    assert preview.safe_to_trade == real.safe_to_trade
    assert preview.orders_checked == real.orders_checked
    assert preview.positions_checked == real.positions_checked
    assert preview.repaired_count == real.repaired_count


def test_a_dry_run_never_submits(connection: sqlite3.Connection) -> None:
    make_intent(connection, status=INTENT_STATUS_CREATED)
    client = FakeTradingClient()

    run(connection, client, dry_run=True)

    assert client.submit_calls == []


# --------------------------------------------------------------------------
# The CLI
# --------------------------------------------------------------------------


def invoke_reconcile(database: Path, client: FakeTradingClient, *extra: str):
    """Run `autotrader reconcile` with the broker boundary faked out."""
    import autotrader.cli as cli_module

    original = cli_module.reconcile_paper_state

    def patched(connection, **kwargs):
        kwargs["trading_client"] = client
        kwargs["recheck_delay_seconds"] = 0.0
        kwargs["sleep"] = no_sleep
        return original(connection, **kwargs)

    cli_module.reconcile_paper_state = patched  # type: ignore[assignment]
    try:
        return runner.invoke(app, ["reconcile", "--db", str(database), *extra])
    finally:
        cli_module.reconcile_paper_state = original  # type: ignore[assignment]


def test_the_cli_exits_zero_when_state_is_clean(database_path: Path) -> None:
    result = invoke_reconcile(database_path, FakeTradingClient())

    assert result.exit_code == 0
    assert "CLEAN" in result.stdout
    assert "Safe To Trade" in result.stdout
    assert "YES" in result.stdout


def test_the_cli_exits_zero_after_a_repair(database_path: Path) -> None:
    with connect(database_path) as connection:
        make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(
        orders={"autotrader-recovery-1": make_order(client_order_id="autotrader-recovery-1")}
    )

    result = invoke_reconcile(database_path, client)

    assert result.exit_code == 0
    assert "REPAIRED" in result.stdout
    assert "No order was submitted" in result.stdout


def test_the_cli_exits_non_zero_when_unresolved(database_path: Path) -> None:
    with connect(database_path) as connection:
        make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient(orders={"autotrader-recovery-1": TimeoutError("read timed out")})

    result = invoke_reconcile(database_path, client)

    assert result.exit_code == 2
    assert "UNRESOLVED" in result.stdout
    assert "NO" in result.stdout


def test_the_cli_exits_non_zero_when_the_pass_failed(database_path: Path) -> None:
    client = FakeTradingClient(base_url="https://api.alpaca.markets")

    result = invoke_reconcile(database_path, client)

    assert result.exit_code == 1
    assert "FAILED" in result.stdout


def test_the_cli_reports_counts(database_path: Path) -> None:
    client = FakeTradingClient(positions=[make_position(BTC, qty="0.0005")])

    result = invoke_reconcile(database_path, client)

    assert "Orders Checked" in result.stdout
    assert "Positions Checked" in result.stdout
    assert "Repaired" in result.stdout
    assert "Unresolved" in result.stdout


def test_the_cli_dry_run_writes_nothing(database_path: Path) -> None:
    with connect(database_path) as connection:
        make_intent(connection, status=INTENT_STATUS_UNKNOWN)
    client = FakeTradingClient()

    result = invoke_reconcile(database_path, client, "--dry-run")

    assert result.exit_code == 0
    assert "DRY RUN" in result.stdout
    with connect(database_path) as connection:
        assert list_reconciliation_runs(connection) == []


def test_the_cli_prints_no_traceback_on_an_operational_failure(database_path: Path) -> None:
    client = FakeTradingClient(account=api_error(401, "unauthorized"))

    result = invoke_reconcile(database_path, client)

    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    assert "Traceback" not in (result.stderr or "")


def test_the_reconcile_command_has_no_live_option() -> None:
    result = runner.invoke(app, ["reconcile", "--help"])

    assert result.exit_code == 0
    assert "--live" not in result.stdout
    assert "--paper" not in result.stdout


# --------------------------------------------------------------------------
# Source-level guarantees
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden",
    [
        "submit_order",
        "submit_order_intent",
        "execute_paper_order",
        "build_market_order_request",
        "MarketOrderRequest",
        "OrderRequest",
        "paper=False",
    ],
)
def test_reconciliation_source_cannot_place_an_order(forbidden: str) -> None:
    """The no-resubmit invariant, asserted against executable code.

    Docstrings and comments are stripped first, so this means the construct is
    absent from the code rather than merely unmentioned in the prose.
    """
    for name, code in package_code().items():
        assert forbidden not in code, f"{forbidden} found in reconciliation/{name}"


def test_reconciliation_constructs_no_trading_client_of_its_own() -> None:
    """The one paper factory is called; no client is built here."""
    for name, code in package_code().items():
        assert "TradingClient(" not in code, name


def test_the_result_vocabulary_imports_only_the_standard_library() -> None:
    """A runtime asking `safe_to_trade` must not have to import a broker SDK."""
    tree = ast.parse(Path(reconciliation_models.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "dataclasses", "datetime", "enum"}


def test_reconciliation_reaches_the_broker_only_through_the_execution_boundary() -> None:
    """One file in the repository speaks to Alpaca. This is not a second one."""
    tree = ast.parse(Path(reconciliation_engine.__file__).read_text())
    alpaca_imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("alpaca")
    }
    assert alpaca_imports == {"alpaca.trading.client"}


def test_reconciliation_makes_no_network_access(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("reconciliation must not open a socket")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    make_intent(connection, status=INTENT_STATUS_UNKNOWN)

    assert run(connection, FakeTradingClient()).safe_to_trade is True


def test_reconciliation_needs_no_credentials(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only building a client needs them; a caller-supplied client does not."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    assert run(connection, FakeTradingClient()).safe_to_trade is True


def test_missing_credentials_fail_the_pass_rather_than_raising(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no client supplied, the factory is what needs credentials."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    result = reconcile_paper_state(connection, now=T0)

    assert result.status is ReconciliationStatus.FAILED
    assert result.safe_to_trade is False


def test_no_phase_nine_runtime_exists() -> None:
    """C8 runs once when asked. Looping, scheduling, and polling are Phase 9."""
    forbidden = ("while True", "schedule", "heartbeat", "asyncio", "threading")
    for name, code in package_code().items():
        for token in forbidden:
            assert token not in code, f"{token} found in reconciliation/{name}"
