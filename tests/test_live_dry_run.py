"""Deterministic dry runs of the real-money path, at $50 and at $100.

Every case the readiness program named, run through the exact
`LIVE_VALIDATION_100` policy against a faked broker, with the assertions that
matter stated per case rather than inferred from a total. Nothing here reaches
a network and nothing here submits: the broker is a double that records what it
was asked for, and several cases exist precisely to assert that it was asked
for nothing.

The two funding states are both real. The account is funded at ~$50 today and
is expected to reach ~$100 after a second deposit, so every sizing case runs at
both and asserts the ceilings that bind in each.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from autotrader.equity import EQUITY_SYMBOLS
from autotrader.equity.allocation import (
    POLICY_LIVE_VALIDATION_100,
    allocation_policy_for,
    plan_allocation,
    risk_policy_for,
)
from autotrader.equity.live import (
    ArmedLiveGateway,
    LiveStartupError,
    ReconciliationBlockedError,
    verify_live_startup,
)
from autotrader.execution.live import NotLiveEnvironmentError
from autotrader.execution.models import OrderSide
from autotrader.execution.paper import (
    AmbiguousSubmissionError,
    BrokerRejectedOrderError,
    DuplicatePreflightUnavailableError,
    ExecutionOutcome,
)
from autotrader.live.armstate import (
    ARM_CONFIRMATION_TOKEN,
    LIVE_ARMED_ENV,
    LiveDisarmedError,
    arm_live_trading,
    disarm_live_trading,
)
from autotrader.live.identity import (
    LIVE_ACCOUNT_FINGERPRINT_ENV,
    AccountIdentityError,
    account_fingerprint,
)
from autotrader.reconciliation.models import ReconciliationResult, ReconciliationStatus
from autotrader.state import sqlite as state
from autotrader.state.sqlite import connect, initialize_database
from conftest import establish_account_safety
from test_equity_execution import (
    FakeDataClient,
    FakeTradingClient,
    api_error,
    make_account,
    make_order,
    make_position,
    run_execution,
)

pytestmark = pytest.mark.filterwarnings("ignore::FutureWarning")

LIVE = allocation_policy_for(POLICY_LIVE_VALIDATION_100)
LIVE_RISK = risk_policy_for(LIVE)
U10 = EQUITY_SYMBOLS
T0 = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)

#: A price every symbol shares, so a plan's arithmetic is readable by eye.
PRICE = Decimal("10")
PRICES = {symbol: PRICE for symbol in U10}

#: The two funding states this validation actually passes through.
HALF_FUNDED = "50"
FULLY_FUNDED = "100"


@pytest.fixture(autouse=True)
def closed_gate_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every gate is shut unless a case deliberately opens it."""
    monkeypatch.delenv("AUTOTRADER_PAPER_TRADING_ENABLED", raising=False)
    monkeypatch.delenv(LIVE_ARMED_ENV, raising=False)
    monkeypatch.delenv(LIVE_ACCOUNT_FINGERPRINT_ENV, raising=False)


@pytest.fixture()
def gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open the submission gate. Requested by name, never by default.

    The real-money runtime has its own two-gate arm on top of this one; this is
    the boundary's own environment gate, and these cases exercise the pipeline
    behind it rather than the arm, which `test_live_100_safety.py` covers.
    """
    monkeypatch.setenv("AUTOTRADER_PAPER_TRADING_ENABLED", "true")


@pytest.fixture()
def store(tmp_path) -> Iterator[sqlite3.Connection]:
    database = tmp_path / "live.db"
    initialize_database(database)
    with connect(database) as connection:
        establish_account_safety(connection)
        yield connection


def plan(
    *,
    equity: str,
    active: tuple[str, ...] = U10,
    actual: dict[str, Decimal] | None = None,
    prices: dict[str, Decimal] | None = None,
):
    return plan_allocation(
        LIVE,
        active_symbols=active,
        account_equity=Decimal(equity),
        external_exposure_fraction=Decimal(0),
        reference_prices=prices if prices is not None else PRICES,
        actual_quantities=actual if actual is not None else {},
    )


def gross(p) -> Decimal:
    return sum((i.target_quantity * i.reference_price for i in p.allocations), Decimal(0))


def live_execution(store: sqlite3.Connection, client: FakeTradingClient, **kwargs):
    """One order through the real pipeline, under the real-money risk limits."""
    return run_execution(store, client, fractional=True, risk_policy=LIVE_RISK, **kwargs)


# ==========================================================================
# Sizing: the shape of the book at each funding state
# ==========================================================================


@pytest.mark.parametrize(
    ("equity", "expected_gross", "expected_slot", "reserve"),
    [
        (HALF_FUNDED, Decimal(45), Decimal("4.5"), Decimal(5)),
        (FULLY_FUNDED, Decimal(90), Decimal(9), Decimal(10)),
    ],
)
def test_all_ten_long(
    equity: str, expected_gross: Decimal, expected_slot: Decimal, reserve: Decimal
) -> None:
    """CRITICAL. Ten LONG symbols, the reserve preserved at both funding states."""
    result = plan(equity=equity)
    assert gross(result) == expected_gross
    assert Decimal(equity) - expected_gross == reserve
    for item in result.allocations:
        assert item.target_quantity * item.reference_price == expected_slot


@pytest.mark.parametrize("equity", [HALF_FUNDED, FULLY_FUNDED])
def test_one_symbol_flat_leaves_its_slot_in_cash(equity: str) -> None:
    """CRITICAL. A flat slot is cash, never redistributed to the other nine."""
    nine = tuple(symbol for symbol in U10 if symbol != "TSLA")
    result = plan(equity=equity, active=nine)
    per_slot = Decimal(equity) * Decimal("0.09")
    assert gross(result) == per_slot * 9
    assert "TSLA" not in {item.symbol for item in result.allocations}


@pytest.mark.parametrize("equity", [HALF_FUNDED, FULLY_FUNDED])
def test_long_to_long_rebalance_buy(equity: str) -> None:
    """Holding under target buys only the difference."""
    slot_shares = (Decimal(equity) * Decimal("0.09")) / PRICE
    held = {symbol: slot_shares for symbol in U10}
    held["SPY"] = slot_shares - Decimal("0.2")
    result = plan(equity=equity, actual=held)
    spy = next(item for item in result.allocations if item.symbol == "SPY")
    assert spy.side is OrderSide.BUY
    assert spy.delta_quantity == Decimal("0.2")


@pytest.mark.parametrize("equity", [HALF_FUNDED, FULLY_FUNDED])
def test_long_to_long_rebalance_sell(equity: str) -> None:
    """Holding over target sells only the excess, never the position."""
    slot_shares = (Decimal(equity) * Decimal("0.09")) / PRICE
    held = {symbol: slot_shares for symbol in U10}
    held["SPY"] = slot_shares + Decimal("0.2")
    result = plan(equity=equity, actual=held)
    spy = next(item for item in result.allocations if item.symbol == "SPY")
    assert spy.side is OrderSide.SELL
    assert spy.delta_quantity == Decimal("0.2")
    assert spy.delta_quantity < held["SPY"]


@pytest.mark.parametrize("equity", [HALF_FUNDED, FULLY_FUNDED])
def test_long_to_flat_exits_in_full(equity: str) -> None:
    """CRITICAL. A transition exits the whole position and never further."""
    nine = tuple(symbol for symbol in U10 if symbol != "TSLA")
    result = plan(equity=equity, active=nine, actual={"TSLA": Decimal("0.45")})
    tsla = next(item for item in result.allocations if item.symbol == "TSLA")
    assert tsla.side is OrderSide.SELL
    assert tsla.delta_quantity == Decimal("0.45")
    assert tsla.target_quantity == Decimal(0)


def test_an_exit_is_exempt_from_the_deadband() -> None:
    """A dust position must still be closable, or the band would trap it."""
    result = plan(equity=HALF_FUNDED, active=(), actual={"SPY": Decimal("0.001")})
    (spy,) = result.allocations
    assert spy.side is OrderSide.SELL
    assert spy.delta_quantity == Decimal("0.001")


@pytest.mark.parametrize("equity", [HALF_FUNDED, FULLY_FUNDED])
def test_a_market_move_during_sizing_never_raises_the_target(equity: str) -> None:
    """A price that moved between the decision and the sizing rounds DOWN."""
    moved = {symbol: Decimal("10.37") for symbol in U10}
    result = plan(equity=equity, prices=moved)
    for item in result.allocations:
        assert item.target_quantity * item.reference_price <= Decimal(equity) * Decimal("0.09")


def test_a_tiny_rebalance_is_not_an_order() -> None:
    """The deadband: below $1 and below 1% of the slot is silence, not churn."""
    slot_shares = (Decimal(HALF_FUNDED) * Decimal("0.09")) / PRICE
    held = {symbol: slot_shares for symbol in U10}
    held["SPY"] = slot_shares - Decimal("0.01")  # $0.10 of drift
    result = plan(equity=HALF_FUNDED, actual=held)
    spy = next(item for item in result.allocations if item.symbol == "SPY")
    assert spy.side is None
    assert not spy.orders


# ==========================================================================
# Ceilings, against a book that is already near them
# ==========================================================================


def test_the_hard_ceiling_refuses_an_entry_that_would_cross_it(
    store: sqlite3.Connection, gate: None
) -> None:
    """CRITICAL. $47.50 of gross on a $50 account, and no more."""
    positions = [make_position("QQQ", qty="4.75", market_value="47.5")]
    client = FakeTradingClient(account=make_account(equity="50", cash="2.5"), positions=positions)
    result = live_execution(store, client, symbol="SPY", price=10.0)
    assert result.outcome is ExecutionOutcome.REJECTED_BY_RISK
    assert client.submit_calls == []


def test_the_symbol_ceiling_refuses_a_concentrated_entry(
    store: sqlite3.Connection, gate: None
) -> None:
    """CRITICAL. No single name past $5.50 on a $50 account."""
    positions = [make_position("SPY", qty="0.55", market_value="5.5")]
    client = FakeTradingClient(account=make_account(equity="50", cash="44.5"), positions=positions)
    result = live_execution(store, client, symbol="SPY", price=10.0)
    assert result.outcome is ExecutionOutcome.REJECTED_BY_RISK
    assert result.risk_decision.reason_code == "POSITION_LIMIT"
    assert client.submit_calls == []


def test_an_oversized_request_is_clamped_not_refused(store: sqlite3.Connection, gate: None) -> None:
    """Risk sizes down to the ceiling and names the constraint that bound it."""
    client = FakeTradingClient(account=make_account(equity="50", cash="50"))
    result = live_execution(store, client, symbol="SPY", price=10.0)
    assert result.outcome is ExecutionOutcome.SUBMITTED
    assert result.risk_decision.approved_quantity * Decimal(10) <= Decimal("5.5")


def test_the_daily_loss_halt_blocks_a_new_entry(store: sqlite3.Connection, gate: None) -> None:
    """CRITICAL. 2% of a $50 baseline is one dollar."""
    state.ensure_daily_risk_baseline(
        store,
        risk_date_utc=T0.date(),
        baseline_equity=Decimal(50),
        captured_at=T0,
    )
    client = FakeTradingClient(account=make_account(equity="49", cash="49"))
    result = live_execution(store, client, symbol="SPY", price=10.0)
    assert result.outcome is ExecutionOutcome.REJECTED_BY_RISK
    assert result.risk_decision.reason_code == "DAILY_LOSS_LIMIT"
    assert client.submit_calls == []


def test_an_exit_survives_the_daily_loss_halt(store: sqlite3.Connection, gate: None) -> None:
    """CRITICAL. The halt is an entry gate. A trapped position is a defect."""
    state.ensure_daily_risk_baseline(
        store,
        risk_date_utc=T0.date(),
        baseline_equity=Decimal(50),
        captured_at=T0,
    )
    positions = [make_position("SPY", qty="0.5", market_value="5")]
    client = FakeTradingClient(account=make_account(equity="45", cash="40"), positions=positions)
    result = live_execution(
        store, client, symbol="SPY", side="SELL", requested_quantity=Decimal("0.5"), price=10.0
    )
    assert result.outcome is ExecutionOutcome.SUBMITTED
    assert len(client.submit_calls) == 1


# ==========================================================================
# Broker outcomes
# ==========================================================================


def test_a_broker_rejection_is_definite_and_leaves_no_order(
    store: sqlite3.Connection, gate: None
) -> None:
    client = FakeTradingClient(
        account=make_account(equity="50", cash="50"), submit_error=api_error(422, "no")
    )
    with pytest.raises(BrokerRejectedOrderError):
        live_execution(store, client, symbol="SPY", price=10.0)
    intents = state.list_order_intents(store)
    assert [intent.status for intent in intents] == [state.INTENT_STATUS_REJECTED]


@pytest.mark.parametrize("status", [408, 429, 500, 502, 504, None])
def test_an_ambiguous_submission_is_never_retried(
    store: sqlite3.Connection, gate: None, status: int | None
) -> None:
    """CRITICAL. Timeout, rate limit, 5xx, unreadable - none is a rejection."""
    client = FakeTradingClient(
        account=make_account(equity="50", cash="50"),
        submit_error=api_error(status, "ambiguous"),
    )
    with pytest.raises(AmbiguousSubmissionError):
        live_execution(store, client, symbol="SPY", price=10.0)
    intents = state.list_order_intents(store)
    assert [intent.status for intent in intents] == [state.INTENT_STATUS_UNKNOWN]
    assert len(client.submit_calls) == 1  # exactly once, never twice


def test_an_ambiguous_submission_halts_the_whole_account(
    store: sqlite3.Connection, gate: None
) -> None:
    """CRITICAL. An order that may exist makes every exposure figure unreliable."""
    client = FakeTradingClient(
        account=make_account(equity="50", cash="50"), submit_error=api_error(504, "gateway")
    )
    with pytest.raises(AmbiguousSubmissionError):
        live_execution(store, client, symbol="SPY", price=10.0)
    assert not state.read_account_safety_state(store).safe_to_trade


def test_a_broker_that_cannot_be_reached_submits_nothing(
    store: sqlite3.Connection, gate: None
) -> None:
    class Unreachable(FakeTradingClient):
        def get_account(self):
            raise ConnectionError("no route to host")

    client = Unreachable(account=make_account(equity="50", cash="50"))
    with pytest.raises(ConnectionError):
        live_execution(store, client, symbol="SPY", price=10.0)
    assert client.submit_calls == []
    assert state.list_order_intents(store) == []


def test_a_duplicate_is_recorded_and_never_submitted_twice(
    store: sqlite3.Connection, gate: None
) -> None:
    """CRITICAL. The broker already has this key, so nothing new is sent."""
    existing = make_order("autotrader-already-there", symbol="SPY", qty="0.5")
    client = FakeTradingClient(account=make_account(equity="50", cash="50"), preflight=existing)
    result = live_execution(store, client, symbol="SPY", price=10.0)
    assert result.outcome is ExecutionOutcome.DUPLICATE
    assert client.submit_calls == []


def test_an_unreadable_duplicate_check_refuses_to_submit(
    store: sqlite3.Connection, gate: None
) -> None:
    """CRITICAL. "The check failed" is never "there is no duplicate"."""
    client = FakeTradingClient(
        account=make_account(equity="50", cash="50"), preflight=api_error(500, "down")
    )
    with pytest.raises(DuplicatePreflightUnavailableError):
        live_execution(store, client, symbol="SPY", price=10.0)
    assert client.submit_calls == []


def test_a_closed_market_submits_nothing_and_leaves_no_intent(
    store: sqlite3.Connection, gate: None
) -> None:
    """CRITICAL. A closed market must not leave a CREATED intent to chase."""
    from autotrader.execution.equity import MarketClosedError

    client = FakeTradingClient(account=make_account(equity="50", cash="50"), is_open=False)
    with pytest.raises(MarketClosedError):
        live_execution(store, client, symbol="SPY", price=10.0)
    assert state.list_order_intents(store) == []
    assert client.submit_calls == []


# ==========================================================================
# Structural invariants across every case above
# ==========================================================================


@pytest.mark.parametrize("equity", [HALF_FUNDED, FULLY_FUNDED, "150", "1000"])
def test_no_plan_at_any_equity_exceeds_the_authorization(equity: str) -> None:
    """CRITICAL. The single assertion the whole validation rests on."""
    result = plan(equity=equity)
    assert gross(result) <= Decimal(90)
    assert gross(result) <= Decimal(equity)
    for item in result.allocations:
        assert item.target_quantity >= 0
        assert item.target_quantity * item.reference_price <= Decimal(11)


@pytest.mark.parametrize("equity", [HALF_FUNDED, FULLY_FUNDED, "150", "1000"])
def test_no_plan_at_any_equity_produces_a_short_or_leverage(equity: str) -> None:
    held = {symbol: Decimal("100") for symbol in U10}
    result = plan(equity=equity, actual=held)
    for item in result.allocations:
        assert item.delta_quantity >= 0
        if item.side is OrderSide.SELL:
            assert item.delta_quantity <= item.actual_quantity


# ==========================================================================
# Startup: the sequence a real-money process must complete before it may trade
# ==========================================================================


class StartupClient(FakeTradingClient):
    """A client that looks real-money to every structural check.

    `get_orders` is added rather than inherited: the paper fakes never needed
    one, and the startup sequence reads the open-order list because an order
    already at the broker is the single most important thing a real-money
    process can discover about itself before it starts.
    """

    def __init__(self, open_orders: list | None = None, **kwargs) -> None:
        kwargs.setdefault("account", make_account(equity="50", cash="50"))
        super().__init__(base_url="https://api.alpaca.markets", sandbox=False, **kwargs)
        self._open_orders = open_orders or []

    def get_orders(self, request=None):
        return list(self._open_orders)


def clean_pass(_connection=None, **_kwargs):
    return ReconciliationResult(
        status=ReconciliationStatus.CLEAN,
        started_at=T0,
        completed_at=T0,
        issues=(),
        orders_checked=0,
        positions_checked=10,
        symbols=U10,
        dry_run=False,
    )


def verdict(status: ReconciliationStatus):
    def _pass(_connection=None, **_kwargs):
        return ReconciliationResult(
            status=status,
            started_at=T0,
            completed_at=T0,
            issues=(),
            orders_checked=0,
            positions_checked=0,
            symbols=(),
            dry_run=False,
        )

    return _pass


@pytest.fixture()
def pinned(monkeypatch: pytest.MonkeyPatch) -> str:
    pin = account_fingerprint("PA0000000000")
    monkeypatch.setenv(LIVE_ACCOUNT_FINGERPRINT_ENV, pin)
    return pin


def test_startup_establishes_the_ceilings_from_the_settled_balance(
    store: sqlite3.Connection, pinned: str
) -> None:
    """CRITICAL. $50 verified means $45 / $47.50 / $50, computed at startup."""
    report = verify_live_startup(store, StartupClient(), policy=LIVE, now=T0, reconcile=clean_pass)
    assert report.account_fingerprint == pinned
    assert report.ceilings.target_gross == Decimal("45.00")
    assert report.ceilings.hard_gross == Decimal("47.50")
    assert report.ceilings.exposure_bound == Decimal("50.00")
    assert report.armed is False


@pytest.mark.parametrize("status", [ReconciliationStatus.UNRESOLVED, ReconciliationStatus.FAILED])
def test_startup_refuses_when_reconciliation_does_not_permit_trading(
    store: sqlite3.Connection, pinned: str, status: ReconciliationStatus
) -> None:
    """CRITICAL. Only CLEAN and REPAIRED are permission."""
    with pytest.raises(ReconciliationBlockedError, match=status.value):
        verify_live_startup(store, StartupClient(), policy=LIVE, now=T0, reconcile=verdict(status))


def test_startup_accepts_a_repaired_pass(store: sqlite3.Connection, pinned: str) -> None:
    report = verify_live_startup(
        store,
        StartupClient(),
        policy=LIVE,
        now=T0,
        reconcile=verdict(ReconciliationStatus.REPAIRED),
    )
    assert report.reconciliation_status == "REPAIRED"


def test_startup_refuses_a_paper_client(store: sqlite3.Connection, pinned: str) -> None:
    """CRITICAL. A real-money runtime handed a paper client must stop."""
    with pytest.raises(NotLiveEnvironmentError):
        verify_live_startup(store, FakeTradingClient(), policy=LIVE, now=T0, reconcile=clean_pass)


def test_startup_refuses_an_unpinned_runtime(
    store: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LIVE_ACCOUNT_FINGERPRINT_ENV, raising=False)
    with pytest.raises(AccountIdentityError):
        verify_live_startup(store, StartupClient(), policy=LIVE, now=T0, reconcile=clean_pass)


def test_startup_refuses_a_non_tradable_account(store: sqlite3.Connection, pinned: str) -> None:
    blocked = make_account(equity="50", cash="50")
    object.__setattr__(blocked, "trading_blocked", True)
    with pytest.raises(LiveStartupError, match="cannot place orders"):
        verify_live_startup(
            store, StartupClient(account=blocked), policy=LIVE, now=T0, reconcile=clean_pass
        )


def test_startup_reports_an_open_order_rather_than_hiding_it(
    store: sqlite3.Connection, pinned: str
) -> None:
    """An order found at startup is surfaced; the cycle guard then blocks BUYs."""
    client = StartupClient(open_orders=[make_order("autotrader-x", symbol="SPY")])
    report = verify_live_startup(store, client, policy=LIVE, now=T0, reconcile=clean_pass)
    assert report.open_order_count == 1


def test_startup_completes_while_disarmed(store: sqlite3.Connection, pinned: str) -> None:
    """CRITICAL. A disarmed runtime must still observe, reconcile and report.

    This is what the read-only soak is: the whole sequence runs, the account is
    read, reconciliation runs, the ceilings are computed - and the arm switch is
    reported as off rather than demanded.
    """
    report = verify_live_startup(store, StartupClient(), policy=LIVE, now=T0, reconcile=clean_pass)
    assert report.armed is False
    assert report.reconciliation_status == "CLEAN"


# ==========================================================================
# The armed gateway: the switch is asked per order, not per process
# ==========================================================================


def test_a_disarmed_gateway_never_reaches_the_broker(
    store: sqlite3.Connection, gate: None, pinned: str
) -> None:
    """CRITICAL. DISARMED means zero broker mutation calls."""
    client = StartupClient()
    gateway = ArmedLiveGateway(trading_client=client, data_client=FakeDataClient(10.0), policy=LIVE)
    with pytest.raises(LiveDisarmedError):
        gateway.execute(
            store,
            symbol="SPY",
            side=OrderSide.BUY,
            requested_quantity=Decimal("0.4"),
            now=T0,
            strategy_run_id=None,
        )
    assert client.submit_calls == []
    assert state.list_order_intents(store) == []


def test_an_armed_gateway_against_the_wrong_account_never_reaches_the_broker(
    store: sqlite3.Connection,
    gate: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CRITICAL. Armed against the wrong account is not permission."""
    monkeypatch.setenv(LIVE_ACCOUNT_FINGERPRINT_ENV, "0" * 32)
    monkeypatch.setenv(LIVE_ARMED_ENV, "true")
    arm_live_trading(store, now=T0, reason="test", confirmation=ARM_CONFIRMATION_TOKEN)
    client = StartupClient()
    gateway = ArmedLiveGateway(trading_client=client, data_client=FakeDataClient(10.0), policy=LIVE)
    with pytest.raises(AccountIdentityError):
        gateway.execute(
            store,
            symbol="SPY",
            side=OrderSide.BUY,
            requested_quantity=Decimal("0.4"),
            now=T0,
            strategy_run_id=None,
        )
    assert client.submit_calls == []


def test_disarming_mid_session_stops_the_very_next_order(
    store: sqlite3.Connection,
    gate: None,
    pinned: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CRITICAL. A switch that waited for a restart would not be a kill switch."""
    monkeypatch.setenv(LIVE_ARMED_ENV, "true")
    arm_live_trading(store, now=T0, reason="first day", confirmation=ARM_CONFIRMATION_TOKEN)
    client = StartupClient()
    gateway = ArmedLiveGateway(trading_client=client, data_client=FakeDataClient(10.0), policy=LIVE)

    first = gateway.execute(
        store,
        symbol="SPY",
        side=OrderSide.BUY,
        requested_quantity=Decimal("0.4"),
        now=T0,
        strategy_run_id=None,
    )
    assert first.outcome is ExecutionOutcome.SUBMITTED
    assert len(client.submit_calls) == 1

    disarm_live_trading(store, now=T0, reason="operator saw something odd")
    with pytest.raises(LiveDisarmedError):
        gateway.execute(
            store,
            symbol="QQQ",
            side=OrderSide.BUY,
            requested_quantity=Decimal("0.4"),
            now=T0,
            strategy_run_id=None,
        )
    assert len(client.submit_calls) == 1  # unchanged
