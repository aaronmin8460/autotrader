"""Combined integration: two runtimes, one Alpaca paper account.

Everything here is about the seam that only exists once crypto and equity run
against the *same* account. The two products keep their own boundaries, their
own schedules and their own process locks; what they cannot keep separate is
the account, and these tests are about the four things that follow from that:

* one total exposure figure, which both books draw on;
* one durable safety answer, which either book can revoke;
* one order-decision path, which the two must not interleave inside;
* one set of API credentials, which they must not both assume is theirs.

**Offline, like every other suite here.** No socket is opened. The broker is
faked at the transport, and the one fake serves *both* boundaries, because a
test that gave crypto and equity a broker each would be testing two accounts
and would pass no matter how badly the real seam behaved.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import AssetClass, AssetStatus, PositionSide
from alpaca.trading.models import Asset, Clock, Position, TradeAccount
from alpaca.trading.requests import MarketOrderRequest

from autotrader import state
from autotrader.account import budget as api_budget
from autotrader.account import safety as account_safety
from autotrader.account.execution import account_execution_section
from autotrader.account.lock import (
    AccountExecutionLock,
    AccountExecutionLockError,
    account_lock_path_for,
)
from autotrader.execution.equity import execute_equity_paper_order
from autotrader.execution.models import EQUITY_SYMBOLS, SUPPORTED_SYMBOLS, TRADABLE_SYMBOLS
from autotrader.execution.paper import (
    PAPER_TRADING_ENABLED_ENV,
    AmbiguousSubmissionError,
    ExecutionOutcome,
    execute_paper_order,
)
from autotrader.reconciliation import ReconciliationStatus, reconcile_paper_state
from autotrader.risk.engine import (
    MAX_DAILY_LOSS_FRACTION,
    MAX_POSITION_FRACTION,
    MAX_TOTAL_EXPOSURE_FRACTION,
)
from autotrader.state.sqlite import connect, initialize_database
from conftest import establish_account_safety
from test_execution_paper import api_error, make_order

BTC = "BTC/USD"
ETH = "ETH/USD"
SPY = "SPY"

T0 = datetime(2026, 3, 4, 15, 0, tzinfo=UTC)

#: One account, sized so the arithmetic in these tests is checkable by hand.
#: 30% of 200,000 is 60,000 of total exposure; 5% is 10,000 per symbol.
EQUITY_USD = 200_000.0
TOTAL_CAP_USD = EQUITY_USD * MAX_TOTAL_EXPOSURE_FRACTION

BTC_PRICE = 100_000.0
SPY_PRICE = 500.0


# ==========================================================================
# One broker, one account, both books
# ==========================================================================


class FakeTrade:
    def __init__(self, price: float) -> None:
        self.price = price


class CombinedDataClient:
    """One market-data double answering for both boundaries.

    The crypto boundary asks `get_crypto_latest_trade` and the equity boundary
    asks `get_stock_latest_trade`; both are served here so a combined test does
    not have to decide which product it is.
    """

    def __init__(self, prices: dict[str, float]) -> None:
        self._prices = dict(prices)
        self.calls: list[str] = []

    def _answer(self, request: object) -> dict[str, FakeTrade]:
        symbols = request.symbol_or_symbols  # type: ignore[attr-defined]
        keys = [symbols] if isinstance(symbols, str) else list(symbols)
        self.calls.extend(keys)
        return {key: FakeTrade(self._prices[key]) for key in keys if key in self._prices}

    def get_crypto_latest_trade(self, request: object, feed: object = None) -> dict[str, FakeTrade]:
        # The crypto boundary passes `feed` explicitly; the equity one has no
        # such concept. Accepting it here is what makes one double serve both.
        return self._answer(request)

    def get_stock_latest_trade(self, request: object) -> dict[str, FakeTrade]:
        return self._answer(request)


def make_account(equity: float = EQUITY_USD, cash: float = EQUITY_USD) -> TradeAccount:
    return TradeAccount(
        id="8f8c8e1a-0000-4000-8000-000000000001",
        account_number="PA0000000000",
        status="ACTIVE",
        equity=str(equity),
        cash=str(cash),
        trading_blocked=False,
        account_blocked=False,
        trade_suspended_by_user=False,
    )


def make_position(symbol: str, *, qty: str, market_value: str, price: str) -> Position:
    crypto = "/" in symbol
    return Position(
        asset_id="8f8c8e1a-0000-4000-8000-000000000002",
        symbol=symbol.replace("/", "") if crypto else symbol,
        exchange="CRYPTO" if crypto else "NASDAQ",
        asset_class=AssetClass.CRYPTO if crypto else AssetClass.US_EQUITY,
        avg_entry_price=price,
        qty=qty,
        side=PositionSide.LONG,
        cost_basis=str(float(qty) * float(price)),
        market_value=market_value,
    )


def make_asset(symbol: str) -> Asset:
    crypto = "/" in symbol
    return Asset(
        id="8f8c8e1a-0000-4000-8000-000000000003",
        **{"class": "crypto" if crypto else "us_equity"},
        exchange="CRYPTO" if crypto else "NASDAQ",
        symbol=symbol,
        status=AssetStatus.ACTIVE,
        tradable=True,
        marginable=False,
        shortable=False,
        easy_to_borrow=False,
        fractionable=True,
        min_order_size=0.0001 if crypto else None,
        min_trade_increment=0.0001 if crypto else None,
        price_increment=1.0,
    )


class PositionReadRendezvous:
    """Forces two callers to both read positions before either submits.

    This is the exact interleaving the account execution lock exists to
    prevent: two processes reading the same free exposure and then each
    consuming it. Without a barrier here a race test would only *sometimes*
    line the two threads up, and a regression test that catches a removed lock
    one run in three is not a regression test.

    With the lock in place the rendezvous can never be satisfied - the second
    caller cannot reach the position read until the first has left the critical
    section - so it times out and `tripped` stays False. That flag is therefore
    a direct, deterministic assertion about the lock rather than an inference
    from the arithmetic that follows it.
    """

    def __init__(self, parties: int = 2, timeout: float = 0.25) -> None:
        self._barrier = threading.Barrier(parties)
        self._timeout = timeout
        self.tripped = False

    def wait(self) -> None:
        try:
            self._barrier.wait(timeout=self._timeout)
        except threading.BrokenBarrierError:
            return
        self.tripped = True


class CombinedBrokerClient:
    """One paper account, serving the crypto boundary and the equity boundary.

    Deliberately a single object. The point of every test in this file is that
    there is one account behind the two products, and handing each boundary its
    own broker double would quietly restore the separation the combined system
    removes.

    `fills_immediately` models what an Alpaca paper market order does in
    practice: the position exists by the time anything reads positions again.
    That is what lets a serialization test observe the *second* caller sizing
    against the exposure the *first* one just consumed.
    """

    def __init__(
        self,
        *,
        positions: list[Position] | None = None,
        submit_error: BaseException | None = None,
        fills_immediately: bool = True,
        is_open: bool = True,
        orders: dict[str, object] | None = None,
        prices: dict[str, float] | None = None,
        rendezvous: PositionReadRendezvous | None = None,
    ) -> None:
        # The two attributes `verify_paper_environment` reads.
        self._base_url = "https://paper-api.alpaca.markets"
        self._sandbox = True
        self._positions = list(positions or [])
        self._submit_error = submit_error
        self._fills = fills_immediately
        self._is_open = is_open
        self._orders = dict(orders or {})
        self._prices = dict(prices or {BTC: BTC_PRICE, SPY: SPY_PRICE})
        self.submit_calls: list[MarketOrderRequest] = []
        self.clock_calls = 0
        self._rendezvous = rendezvous
        self._lock = threading.Lock()

    # -- reads -------------------------------------------------------------

    def get_account(self) -> TradeAccount:
        return make_account()

    def get_all_positions(self) -> list[Position]:
        if self._rendezvous is not None:
            self._rendezvous.wait()
        with self._lock:
            return list(self._positions)

    def get_asset(self, symbol: str) -> Asset:
        return make_asset(str(symbol))

    def get_clock(self) -> Clock:
        self.clock_calls += 1
        return Clock(
            timestamp=T0,
            is_open=self._is_open,
            next_open=T0 + timedelta(hours=1),
            next_close=T0 + timedelta(hours=2),
        )

    def get_calendar(self, filters: object = None) -> list[object]:
        return []

    def get_order_by_client_id(self, client_order_id: str):
        if client_order_id in self._orders:
            answer = self._orders[client_order_id]
            if isinstance(answer, BaseException):
                raise answer
            return answer
        raise api_error(404, "order not found")

    # -- the one write -----------------------------------------------------

    def submit_order(self, order_data: MarketOrderRequest):
        with self._lock:
            self.submit_calls.append(order_data)
        if self._submit_error is not None:
            raise self._submit_error
        symbol = str(order_data.symbol)
        quantity = float(order_data.qty)
        price = self._prices.get(symbol, 1.0)
        if self._fills:
            with self._lock:
                self._positions.append(
                    make_position(
                        symbol,
                        qty=str(quantity),
                        market_value=str(quantity * price),
                        price=str(price),
                    )
                )
        return make_order(
            client_order_id=str(order_data.client_order_id),
            symbol=symbol,
            qty=str(quantity),
        )

    def exposure(self) -> float:
        with self._lock:
            return sum(float(position.market_value) for position in self._positions)


# ==========================================================================
# Fixtures
# ==========================================================================


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    """A database a full-universe reconciliation has already vouched for."""
    path = initialize_database(tmp_path / "combined.db")
    with connect(path) as setup:
        establish_account_safety(setup)
    return path


@pytest.fixture
def connection(database_path: Path):
    with connect(database_path) as open_connection:
        yield open_connection


@pytest.fixture(autouse=True)
def paper_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PAPER_TRADING_ENABLED_ENV, "true")


def occupied_positions(total_usd: float) -> list[Position]:
    """Equity holdings summing to `total_usd`, none of them near the 5% cap.

    Spread across several symbols on purpose: a single position that large
    would be over the per-symbol limit, which is a different rule from the one
    these tests are about.
    """
    symbols = ("QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL")
    each = total_usd / len(symbols)
    return [
        make_position(symbol, qty="10", market_value=str(each), price=str(each / 10))
        for symbol in symbols
    ]


def buy_crypto(connection: sqlite3.Connection, client, quantity: str = "1", **kwargs):
    return execute_paper_order(
        connection,
        symbol=BTC,
        side="BUY",
        requested_quantity=Decimal(quantity),
        trading_client=client,
        data_client=CombinedDataClient({BTC: BTC_PRICE}),
        now=T0,
        **kwargs,
    )


def buy_equity(connection: sqlite3.Connection, client, quantity: str = "100", **kwargs):
    return execute_equity_paper_order(
        connection,
        symbol=SPY,
        side="BUY",
        requested_quantity=Decimal(quantity),
        trading_client=client,
        data_client=CombinedDataClient({SPY: SPY_PRICE}),
        now=T0,
        **kwargs,
    )


# ==========================================================================
# Global risk: one account, one exposure figure
# ==========================================================================


def test_crypto_and_equity_share_one_global_exposure_limit(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. Crypto exposure counts against the equity book's headroom.

    28% of the account is already held in equities. A BTC order sized at 100%
    of the account must be clamped to the 2% that is actually free - not to 2%
    *of the crypto book*, which does not exist as a concept, and not approved
    in full because the crypto boundary only looked at crypto positions.
    """
    already_held = EQUITY_USD * 0.28
    client = CombinedBrokerClient(positions=occupied_positions(already_held))

    result = buy_crypto(connection, client)

    assert result.outcome is ExecutionOutcome.SUBMITTED
    approved = result.risk_decision.approved_quantity
    free_usd = TOTAL_CAP_USD - already_held
    assert approved * Decimal(str(BTC_PRICE)) <= Decimal(str(free_usd))
    assert client.exposure() <= TOTAL_CAP_USD + 1e-6


def test_equity_is_sized_against_crypto_exposure_too(connection: sqlite3.Connection) -> None:
    """The reverse direction. A crypto holding shrinks the equity headroom."""
    client = CombinedBrokerClient(
        positions=[
            make_position(BTC, qty="0.56", market_value=str(EQUITY_USD * 0.28), price="100000")
        ]
    )

    result = buy_equity(connection, client)

    assert result.outcome is ExecutionOutcome.SUBMITTED
    free_usd = TOTAL_CAP_USD - EQUITY_USD * 0.28
    assert float(result.intent.approved_quantity) * SPY_PRICE <= free_usd
    assert client.exposure() <= TOTAL_CAP_USD + 1e-6


def test_the_risk_policy_is_unchanged_and_has_no_per_book_split() -> None:
    """CRITICAL. Combined integration approved no per-book allocation.

    A "crypto 20% / equity 20%" split is not policy, has never been approved,
    and would be a *loosening* of the 30% account cap rather than a tightening
    of it. The three limits are exactly what they were.
    """
    assert (MAX_POSITION_FRACTION, MAX_TOTAL_EXPOSURE_FRACTION, MAX_DAILY_LOSS_FRACTION) == (
        0.05,
        0.30,
        0.02,
    )
    from autotrader.risk import engine as risk_engine

    source = Path(risk_engine.__file__).read_text(encoding="utf-8")
    for forbidden in ("CRYPTO_MAX", "EQUITY_MAX", "MAX_CRYPTO", "MAX_EQUITY", "per_book"):
        assert forbidden not in source, forbidden


# ==========================================================================
# The account execution lock
# ==========================================================================


def test_account_execution_lock_serializes_crypto_and_equity_orders(
    database_path: Path,
) -> None:
    """CRITICAL. The two boundaries cannot be inside the critical section at once.

    Two threads, two connections, two lock objects on the same lock file -
    which is what two processes would have. The section records when it is
    entered and left; the intervals must not overlap.
    """
    entries: list[tuple[str, str]] = []
    guard = threading.Lock()
    barrier = threading.Barrier(2)

    def section(name: str) -> None:
        with connect(database_path) as own_connection:
            lock = AccountExecutionLock(account_lock_path_for(database_path), timeout_seconds=10)
            barrier.wait(timeout=10)
            with account_execution_section(own_connection, now=T0, trading_calls=1, lock=lock):
                with guard:
                    entries.append((name, "enter"))
                # Long enough that an unserialized pair would certainly overlap.
                threading.Event().wait(0.05)
                with guard:
                    entries.append((name, "exit"))

    threads = [
        threading.Thread(target=section, args=("crypto",)),
        threading.Thread(target=section, args=("equity",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert len(entries) == 4, entries
    # enter/exit must alternate in pairs: no interleaving.
    assert entries[0][1] == "enter"
    assert entries[1] == (entries[0][0], "exit")
    assert entries[2][1] == "enter"
    assert entries[3] == (entries[2][0], "exit")
    assert {entries[0][0], entries[2][0]} == {"crypto", "equity"}


def test_the_two_runtime_locks_stay_separate_from_the_account_lock(
    database_path: Path,
) -> None:
    """The services still run simultaneously; only the decision path is shared."""
    from autotrader.runtime.lock import lock_path_for

    crypto_runtime_lock = lock_path_for(database_path)
    equity_runtime_lock = lock_path_for(database_path, scope="equity")
    account_lock = account_lock_path_for(database_path)

    assert crypto_runtime_lock != equity_runtime_lock
    assert account_lock not in {crypto_runtime_lock, equity_runtime_lock}

    # Both runtime locks can be held at once. The account lock is a third file.
    from autotrader.runtime.lock import RuntimeLock

    with (
        RuntimeLock(crypto_runtime_lock),
        RuntimeLock(equity_runtime_lock),
        AccountExecutionLock(account_lock, timeout_seconds=1),
    ):
        pass


def test_an_unobtainable_account_lock_fails_the_action_closed(database_path: Path) -> None:
    """A wedged holder produces no order, and no order sent late either."""
    held = AccountExecutionLock(account_lock_path_for(database_path), timeout_seconds=5)
    held.acquire()
    try:
        waiter = AccountExecutionLock(account_lock_path_for(database_path), timeout_seconds=0.05)
        with (
            connect(database_path) as own_connection,
            pytest.raises(AccountExecutionLockError) as error,
            account_execution_section(own_connection, now=T0, trading_calls=1, lock=waiter),
        ):
            pytest.fail("the critical section must not be entered")
        assert "Nothing was submitted" in str(error.value)
    finally:
        held.release()


def test_global_exposure_race_cannot_approve_two_orders_from_same_free_capacity(
    database_path: Path,
) -> None:
    """CRITICAL REGRESSION. Two callers, one pool of free exposure.

    28% is held, so 2% - 4,000 USD - is free under the 30% account cap. A BTC
    buy and a SPY buy are launched concurrently from two connections, each
    asking for far more than the account can take.

    Without serialization both read 28%, both size into the same 4,000, and the
    account ends at 32%. With the account execution lock the second caller reads
    the exposure the first one just consumed and is refused by the existing risk
    contract. The assertion is on the *account*, not on either caller: whichever
    order wins, total exposure may not exceed the cap.
    """
    already_held = EQUITY_USD * 0.28
    rendezvous = PositionReadRendezvous()
    client = CombinedBrokerClient(positions=occupied_positions(already_held), rendezvous=rendezvous)
    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}
    guard = threading.Lock()

    def run(name: str, buy) -> None:
        try:
            with connect(database_path) as own_connection:
                lock = AccountExecutionLock(
                    account_lock_path_for(database_path), timeout_seconds=10
                )
                barrier.wait(timeout=10)
                result = buy(own_connection, client, account_lock=lock)
            with guard:
                outcomes[name] = result.outcome
        except Exception as error:  # noqa: BLE001 - recorded and asserted on below
            with guard:
                outcomes[name] = error

    threads = [
        threading.Thread(target=run, args=("crypto", buy_crypto)),
        threading.Thread(target=run, args=("equity", buy_equity)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert set(outcomes) == {"crypto", "equity"}, outcomes

    # THE deterministic assertion: the two position reads could not overlap.
    # The fake broker makes both callers rendezvous inside `get_all_positions`,
    # which is precisely the read the account lock must serialize. If the lock
    # is doing its job the second caller cannot get there while the first holds
    # it, the rendezvous times out, and this flag stays False. Remove the lock
    # and it trips on every run.
    assert not rendezvous.tripped, (
        "both callers read account positions concurrently; the account execution "
        "lock did not serialize the critical section"
    )

    # THE arithmetic assertion: the free capacity is consumed once, not twice. Whichever
    # caller reached the critical section first, the second was measured against
    # the exposure the first had already consumed - so the two approvals add up
    # to the capacity that existed, rather than each taking all of it.
    consumed = client.exposure() - already_held
    free_usd = TOTAL_CAP_USD - already_held
    assert consumed <= free_usd + 1e-6, (
        f"{consumed:.2f} USD of exposure was added against {free_usd:.2f} USD of free "
        "capacity; two callers sized into the same headroom"
    )
    assert client.exposure() <= TOTAL_CAP_USD + 1e-6

    # The existing risk contract decides *how* the second one is stopped -
    # rejected outright, or clamped to what is left, or refused because what is
    # left rounds below one whole share. All three are correct outcomes and this
    # test does not pin which; what it pins is that the capacity was not double
    # counted. It must not, however, be vacuous: something got through.
    assert client.submit_calls, "no order was submitted at all; the test proved nothing"
    assert len(client.submit_calls) <= 2


# ==========================================================================
# The shared halt: UNKNOWN from any asset stops every asset
# ==========================================================================


def test_crypto_cannot_trade_after_equity_unknown_order(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. A SPY submission of unknown outcome stops BTC."""
    unknown_client = CombinedBrokerClient(submit_error=api_error(504, "gateway timeout"))

    with pytest.raises(AmbiguousSubmissionError):
        buy_equity(connection, unknown_client, quantity="1")

    safety = account_safety.read_account_safety(connection)
    assert safety.state == state.ACCOUNT_SAFETY_UNSAFE_UNKNOWN
    assert not safety.safe_to_trade
    assert safety.source == account_safety.SOURCE_EQUITY
    assert safety.client_order_id is not None

    crypto_client = CombinedBrokerClient()
    with pytest.raises(account_safety.AccountUnsafeError) as error:
        buy_crypto(connection, crypto_client)

    assert crypto_client.submit_calls == [], "BTC broker submit count must be 0"
    assert "NO NEW ORDERS FROM ANY ASSET CLASS" in str(error.value)


def test_equity_cannot_trade_after_crypto_unknown_order(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. The reverse direction, and it must be symmetrical."""
    unknown_client = CombinedBrokerClient(submit_error=api_error(504, "gateway timeout"))

    with pytest.raises(AmbiguousSubmissionError):
        buy_crypto(connection, unknown_client, quantity="0.01")

    safety = account_safety.read_account_safety(connection)
    assert safety.state == state.ACCOUNT_SAFETY_UNSAFE_UNKNOWN
    assert safety.source == account_safety.SOURCE_CRYPTO

    equity_client = CombinedBrokerClient()
    with pytest.raises(account_safety.AccountUnsafeError):
        buy_equity(connection, equity_client)

    assert equity_client.submit_calls == [], "SPY broker submit count must be 0"


def test_unknown_in_one_asset_class_halts_the_other_asset_class(
    database_path: Path,
) -> None:
    """CRITICAL REGRESSION. Across two connections, as two processes would be.

    The halt has to survive leaving the process that raised it. A second
    connection - one that never saw the ambiguous submission - must refuse.
    """
    with connect(database_path) as equity_process, pytest.raises(AmbiguousSubmissionError):
        buy_equity(
            equity_process,
            CombinedBrokerClient(submit_error=api_error(504, "gateway timeout")),
            quantity="1",
        )

    crypto_client = CombinedBrokerClient()
    with connect(database_path) as crypto_process:
        assert not account_safety.read_account_safety(crypto_process).safe_to_trade
        with pytest.raises(account_safety.AccountUnsafeError):
            buy_crypto(crypto_process, crypto_client)

    assert crypto_client.submit_calls == []


def test_the_halt_is_not_cleared_by_time_passing(connection: sqlite3.Connection) -> None:
    """A halt is cleared by evidence, never by a later clock reading."""
    with pytest.raises(AmbiguousSubmissionError):
        buy_crypto(
            connection,
            CombinedBrokerClient(submit_error=api_error(504, "gateway timeout")),
            quantity="0.01",
        )

    much_later = T0 + timedelta(days=3)
    client = CombinedBrokerClient()
    with pytest.raises(account_safety.AccountUnsafeError):
        execute_paper_order(
            connection,
            symbol=BTC,
            side="BUY",
            requested_quantity=Decimal("0.01"),
            trading_client=client,
            data_client=CombinedDataClient({BTC: BTC_PRICE}),
            now=much_later,
        )
    assert client.submit_calls == []


def test_a_dry_run_still_observes_while_the_account_is_halted(
    connection: sqlite3.Connection,
) -> None:
    """Observation is never gated. It is what an operator needs while halted."""
    with pytest.raises(AmbiguousSubmissionError):
        buy_crypto(
            connection,
            CombinedBrokerClient(submit_error=api_error(504, "gateway timeout")),
            quantity="0.01",
        )

    client = CombinedBrokerClient()
    result = buy_crypto(connection, client, dry_run=True)

    assert result.outcome is ExecutionOutcome.DRY_RUN
    assert client.submit_calls == []


# ==========================================================================
# Full-universe reconciliation
# ==========================================================================


def reconcile(connection: sqlite3.Connection, client, **kwargs):
    return reconcile_paper_state(
        connection,
        trading_client=client,
        now=T0,
        confirmations=1,
        recheck_delay_seconds=0,
        sleep=lambda _seconds: None,
        **kwargs,
    )


def test_full_universe_reconciliation_is_account_authoritative(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL REGRESSION. One crypto position, one equity position, one ambiguity.

    All three have to be in the pass's view, and the ambiguous order has to keep
    `safe_to_trade` false until it is resolved - regardless of which book it
    belongs to.
    """
    intent_id = state.record_order_intent(
        connection,
        client_order_id="autotrader-ambiguous",
        created_at=T0 - timedelta(minutes=5),
        symbol=SPY,
        side="BUY",
        requested_quantity=Decimal(1),
        approved_quantity=Decimal(1),
        reference_price=SPY_PRICE,
        risk_reason_code="APPROVED",
    )
    state.update_order_intent_status(
        connection,
        order_intent_id=intent_id,
        status=state.INTENT_STATUS_UNKNOWN,
        updated_at=T0 - timedelta(minutes=5),
    )

    unresolvable = CombinedBrokerClient(
        positions=[
            make_position(BTC, qty="0.05", market_value="5000", price="100000"),
            make_position(SPY, qty="10", market_value="5000", price="500"),
        ],
        orders={"autotrader-ambiguous": APIError("the broker could not be asked")},
    )

    result = reconcile(connection, unresolvable)

    assert set(result.symbols) == set(TRADABLE_SYMBOLS)
    assert result.positions_checked == 12
    assert result.safe_to_trade is False
    assert not account_safety.read_account_safety(connection).safe_to_trade

    # Both positions were adopted from broker truth, not just the crypto one.
    assert state.get_position(connection, BTC) is not None
    spy = state.get_position(connection, SPY)
    assert spy is not None and spy.quantity == Decimal(10)

    # Resolve the ambiguity: the broker now answers about that key.
    resolved = CombinedBrokerClient(
        positions=list(unresolvable.get_all_positions()),
        orders={
            "autotrader-ambiguous": make_order(
                client_order_id="autotrader-ambiguous", symbol=SPY, qty="1", filled_qty="1"
            )
        },
    )
    second = reconcile(connection, resolved)

    assert second.safe_to_trade is True
    assert account_safety.read_account_safety(connection).safe_to_trade


def test_full_universe_reconciliation_clears_global_halt_only_when_safe(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. The halt is cleared by evidence, and only complete evidence."""
    with pytest.raises(AmbiguousSubmissionError):
        buy_crypto(
            connection,
            CombinedBrokerClient(submit_error=api_error(504, "gateway timeout")),
            quantity="0.01",
        )
    assert not account_safety.read_account_safety(connection).safe_to_trade

    client = CombinedBrokerClient()
    intents = state.list_order_intents(connection)
    unknown = next(item for item in intents if item.status == state.INTENT_STATUS_UNKNOWN)

    # 1. A pass that cannot resolve the ambiguity leaves the halt in place.
    stuck = CombinedBrokerClient(
        orders={unknown.client_order_id: APIError("the broker could not be asked")}
    )
    assert reconcile(connection, stuck).safe_to_trade is False
    assert not account_safety.read_account_safety(connection).safe_to_trade

    # 2. A *narrow* pass, even a clean one, may not clear an account-wide halt.
    narrow = reconcile(connection, client, symbols=SUPPORTED_SYMBOLS)
    assert narrow.status in {ReconciliationStatus.CLEAN, ReconciliationStatus.REPAIRED}
    assert account_safety.missing_universe_symbols(narrow) == EQUITY_SYMBOLS
    assert not account_safety.read_account_safety(connection).safe_to_trade, (
        "a two-symbol pass cannot vouch for a twelve-symbol account"
    )

    # 3. A full-universe pass that resolves it does clear the halt.
    full = reconcile(connection, client)
    assert full.safe_to_trade is True
    assert set(full.symbols) == set(TRADABLE_SYMBOLS)
    assert account_safety.read_account_safety(connection).safe_to_trade


def test_repeated_full_universe_reconciliation_is_idempotent(
    connection: sqlite3.Connection,
) -> None:
    """REPAIRED, then CLEAN. Reconciling twice changes nothing the second time."""
    state.upsert_position(
        connection, symbol=SPY, quantity=Decimal(99), average_price=1.0, updated_at=T0
    )
    client = CombinedBrokerClient(
        positions=[make_position(SPY, qty="10", market_value="5000", price="500")]
    )

    first = reconcile(connection, client)
    second = reconcile(connection, client)

    assert first.status is ReconciliationStatus.REPAIRED
    assert second.status is ReconciliationStatus.CLEAN
    assert second.safe_to_trade is True
    assert account_safety.read_account_safety(connection).safe_to_trade


def test_a_dry_run_reconciliation_never_moves_the_halt(
    connection: sqlite3.Connection,
) -> None:
    """The audit mode audits. It does not quietly change what it is auditing."""
    with pytest.raises(AmbiguousSubmissionError):
        buy_crypto(
            connection,
            CombinedBrokerClient(submit_error=api_error(504, "gateway timeout")),
            quantity="0.01",
        )
    before = account_safety.read_account_safety(connection)

    reconcile(connection, CombinedBrokerClient(), dry_run=True)

    after = account_safety.read_account_safety(connection)
    assert (after.state, after.client_order_id) == (before.state, before.client_order_id)


# ==========================================================================
# One daily baseline for one account
# ==========================================================================


def test_concurrent_daily_baseline_initialization_produces_one_authoritative_value(
    database_path: Path,
) -> None:
    """CRITICAL. Two processes, one first observation.

    Both runtimes resolve the UTC-day baseline at their first cycle of the day.
    They must agree, because the daily-loss halt is measured against it and two
    competing baselines would be two different halts.
    """
    risk_date = state.utc_risk_date(T0)
    barrier = threading.Barrier(2)
    seen: list[Decimal] = []
    guard = threading.Lock()

    def observe(equity: str) -> None:
        with connect(database_path) as own_connection:
            barrier.wait(timeout=10)
            baseline = state.ensure_daily_risk_baseline(
                own_connection,
                risk_date_utc=risk_date,
                baseline_equity=Decimal(equity),
                captured_at=T0,
            )
        with guard:
            seen.append(baseline.baseline_equity)

    threads = [
        threading.Thread(target=observe, args=("200000",)),
        threading.Thread(target=observe, args=("199000",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert len(seen) == 2
    assert seen[0] == seen[1], "two processes disagreed about the day's baseline"
    assert seen[0] in {Decimal("200000"), Decimal("199000")}

    with connect(database_path) as reader:
        stored = state.list_daily_risk_baselines(reader)
    assert len(stored) == 1, "a race created competing baselines"
    assert stored[0].baseline_equity == seen[0]


# ==========================================================================
# The shared API budget
# ==========================================================================


def test_shared_api_budget_coordinates_two_runtime_processes(
    database_path: Path,
) -> None:
    """CRITICAL. Two processes spend from one allowance, not one each."""
    limit = 10
    with connect(database_path) as crypto_process, connect(database_path) as equity_process:
        for _ in range(6):
            grant = api_budget.try_consume(
                crypto_process,
                budget=state.API_BUDGET_TRADING,
                calls=1,
                now=T0,
                limit=limit,
            )
            assert grant.granted

        # The equity process sees what the crypto process already spent.
        usage = api_budget.current_usage(equity_process, budget=state.API_BUDGET_TRADING, now=T0)
        assert usage.spent == 6

        for _ in range(4):
            assert api_budget.try_consume(
                equity_process,
                budget=state.API_BUDGET_TRADING,
                calls=1,
                now=T0,
                limit=limit,
            ).granted

        refused = api_budget.try_consume(
            equity_process, budget=state.API_BUDGET_TRADING, calls=1, now=T0, limit=limit
        )
        assert not refused.granted
        assert refused.spent == limit, "a refused call must not be counted"


def test_the_two_api_budgets_are_metered_separately(database_path: Path) -> None:
    """Trading and market data are different services with different allowances."""
    with connect(database_path) as own_connection:
        for _ in range(3):
            api_budget.try_consume(
                own_connection, budget=state.API_BUDGET_TRADING, calls=1, now=T0, limit=3
            )
        exhausted = api_budget.try_consume(
            own_connection, budget=state.API_BUDGET_TRADING, calls=1, now=T0, limit=3
        )
        market_data = api_budget.try_consume(
            own_connection, budget=state.API_BUDGET_MARKET_DATA, calls=1, now=T0, limit=3
        )

    assert not exhausted.granted
    assert market_data.granted, "a spent trading budget must not spend the data budget"


def test_an_exhausted_budget_fails_the_order_closed_rather_than_deferring_it(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. A signal belongs to its bar. It is abandoned, never delayed."""
    window_calls = api_budget.limit_for(state.API_BUDGET_TRADING)
    granted, _ = state.consume_api_budget(
        connection,
        budget=state.API_BUDGET_TRADING,
        window_start=api_budget.window_start_for(T0),
        calls=window_calls,
        limit=window_calls,
        updated_at=T0,
    )
    assert granted

    client = CombinedBrokerClient()
    with pytest.raises(api_budget.ApiBudgetExceededError) as error:
        buy_crypto(connection, client)

    assert client.submit_calls == []
    assert "abandoned rather than delayed" in str(error.value)

    # The next window is a clean slate; nothing was queued into it.
    later = T0 + timedelta(minutes=1)
    usage = api_budget.current_usage(connection, budget=state.API_BUDGET_TRADING, now=later)
    assert usage.spent == 0


def test_both_runtimes_spending_the_same_window_is_visible_in_one_count(
    database_path: Path,
) -> None:
    """The whole point: one counter, not two."""
    client = CombinedBrokerClient(positions=occupied_positions(0.0))
    with connect(database_path) as crypto_process:
        buy_crypto(crypto_process, client, quantity="0.01")
    with connect(database_path) as equity_process:
        buy_equity(equity_process, client, quantity="1")

    with connect(database_path) as reader:
        usage = api_budget.current_usage(reader, budget=state.API_BUDGET_TRADING, now=T0)

    expected = api_budget.CRYPTO_EXECUTION_TRADING_CALLS + api_budget.EQUITY_EXECUTION_TRADING_CALLS
    assert usage.spent == expected, "the two runtimes must count into one window"


# ==========================================================================
# The two product contracts survive the merge
# ==========================================================================


def test_crypto_hardening_guarantees_survive_equity_merge(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. Every C10 guarantee still holds with the equity book present."""
    from autotrader.execution import paper as crypto_paper
    from autotrader.runtime import checkpoint as runtime_checkpoint

    source = Path(crypto_paper.__file__).read_text(encoding="utf-8")

    # 1. A non-durable intent still cannot submit.
    assert "NonDurableIntentError" in source
    assert "if connection.in_transaction:" in source

    # 2. A non-durable checkpoint claim still cannot trade.
    checkpoint_source = Path(runtime_checkpoint.__file__).read_text(encoding="utf-8")
    assert "in_transaction" in checkpoint_source

    # 3. No replacement-order or resubmission call exists on the path. Scanning
    #    for the word "retry" would be meaningless here - the module's prose is
    #    largely *about* retries being switched off - so this looks for the
    #    calls a retry would actually have to make.
    for forbidden in ("replace_order(", "resubmit", "cancel_order(", "time.sleep"):
        assert forbidden not in source, forbidden
    assert "client._retry = 0" in source, "SDK-level retries must be switched off"

    # 4. An ambiguous outcome is still UNKNOWN, still keeps its client_order_id,
    #    and is still not retried - now with an equity book on the account.
    client = CombinedBrokerClient(submit_error=api_error(504, "gateway timeout"))
    with pytest.raises(AmbiguousSubmissionError):
        buy_crypto(connection, client, quantity="0.01")

    assert len(client.submit_calls) == 1, "exactly one attempt, never a second"
    intents = state.list_order_intents(connection)
    assert len(intents) == 1
    assert intents[0].status == state.INTENT_STATUS_UNKNOWN
    assert intents[0].client_order_id.startswith("autotrader-")


def test_crypto_still_submits_gtc_and_never_day(connection: sqlite3.Connection) -> None:
    """Crypto keeps its own order semantics. Equity's must not leak into it."""
    client = CombinedBrokerClient()
    buy_crypto(connection, client, quantity="0.01")

    [request] = client.submit_calls
    assert request.time_in_force.value == "gtc"
    assert getattr(request, "extended_hours", None) in (None, False)
    assert client.clock_calls == 0, "crypto must not consult a market session"


def test_equity_regular_session_contract_survives_combined_merge(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. Whole shares, DAY, no extended hours, session checked."""
    client = CombinedBrokerClient()
    result = buy_equity(connection, client, quantity="10")

    [request] = client.submit_calls
    assert request.time_in_force.value == "day"
    assert request.qty == 10.0
    assert float(request.qty).is_integer(), "whole shares only"
    assert getattr(request, "extended_hours", None) in (None, False)
    assert client.clock_calls == 1, "the session was confirmed against the broker's clock"
    assert result.outcome is ExecutionOutcome.SUBMITTED


def test_a_closed_session_still_blocks_equity_and_never_blocks_crypto(
    connection: sqlite3.Connection,
) -> None:
    """The two schedules stay independent on one account."""
    from autotrader.execution.equity import MarketClosedError

    closed = CombinedBrokerClient(is_open=False)
    with pytest.raises(MarketClosedError):
        buy_equity(connection, closed, quantity="10")
    assert closed.submit_calls == []

    # The same closed market is irrelevant to crypto.
    crypto_client = CombinedBrokerClient(is_open=False)
    result = buy_crypto(connection, crypto_client, quantity="0.01")
    assert result.outcome is ExecutionOutcome.SUBMITTED


def test_all_twelve_symbols_share_one_checkpoint_table_without_colliding(
    connection: sqlite3.Connection,
) -> None:
    """One table, twelve unique claims, and restart safety stays independent."""
    for index, symbol in enumerate(TRADABLE_SYMBOLS):
        state.upsert_runtime_checkpoint(
            connection,
            symbol=symbol,
            last_processed_bar_timestamp=T0 - timedelta(minutes=15 * (index + 1)),
            updated_at=T0,
        )

    stored = state.list_runtime_checkpoints(connection)
    assert len(stored) == len(TRADABLE_SYMBOLS) == 12
    assert len({item.symbol for item in stored}) == 12

    # Moving the crypto claim does not touch any equity claim.
    before = {item.symbol: item.last_processed_bar_timestamp for item in stored}
    state.upsert_runtime_checkpoint(
        connection,
        symbol=BTC,
        last_processed_bar_timestamp=T0,
        updated_at=T0,
    )
    after = {
        item.symbol: item.last_processed_bar_timestamp
        for item in state.list_runtime_checkpoints(connection)
    }
    assert after[BTC] != before[BTC]
    for symbol in EQUITY_SYMBOLS:
        assert after[symbol] == before[symbol]


# ==========================================================================
# Dashboard V0.2
# ==========================================================================


def dashboard_page(database_path: Path, client: CombinedBrokerClient | None = None):
    """One dashboard poll against a real database, with the broker read faked."""
    from autotrader.dashboard.broker import BrokerRead
    from autotrader.dashboard.service import build_overview
    from autotrader.execution.paper import fetch_paper_positions

    if client is None:
        return build_overview(
            database_path=database_path,
            now=T0,
            broker=BrokerRead(ok=False, reason="BROKER_NOT_CONFIGURED"),
        )
    return build_overview(
        database_path=database_path,
        now=T0,
        broker=BrokerRead(
            ok=True,
            account=None,
            positions=fetch_paper_positions(client),
            reason=None,
        ),
    )


def test_dashboard_v02_shows_crypto_and_equity_exposure(database_path: Path) -> None:
    """CRITICAL. Both books appear, split, against the one account limit.

    The breakdown is display only: the crypto and equity rows carry no limit of
    their own, and the total is the only row the 30% account cap is attached
    to. A per-book cap on this screen would name a rule nothing enforces.
    """
    from autotrader.dashboard.broker import BrokerRead
    from autotrader.dashboard.service import build_overview
    from autotrader.execution.paper import fetch_paper_account_state

    client = CombinedBrokerClient(
        positions=[
            make_position(BTC, qty="0.24", market_value="24000", price="100000"),
            make_position(SPY, qty="32", market_value="16000", price="500"),
        ]
    )
    from autotrader.execution.paper import fetch_paper_positions

    page = build_overview(
        database_path=database_path,
        now=T0,
        broker=BrokerRead(
            ok=True,
            account=fetch_paper_account_state(client),
            positions=fetch_paper_positions(client),
            reason=None,
        ),
    )

    assert page.risk is not None
    rows = {row.key: row for row in page.risk.exposure}
    assert set(rows) == {"crypto", "equity", "total"}

    # 24,000 and 16,000 against 200,000 of equity: 12% and 8%, totalling 20%.
    assert rows["crypto"].value.value == pytest.approx(24_000.0)
    assert rows["equity"].value.value == pytest.approx(16_000.0)
    assert rows["total"].value.value == pytest.approx(40_000.0)
    assert rows["crypto"].fraction == pytest.approx(0.12)
    assert rows["equity"].fraction == pytest.approx(0.08)
    assert rows["total"].fraction == pytest.approx(0.20)

    # The books are a breakdown; only the total is measured against a limit.
    assert rows["crypto"].enforced is False
    assert rows["equity"].enforced is False
    assert rows["total"].enforced is True
    assert page.risk.total_exposure_limit_fraction == MAX_TOTAL_EXPOSURE_FRACTION == 0.30

    # And the enforced limits are still exactly the three that exist.
    assert {limit.key for limit in page.risk.limits} == {
        "position",
        "total_exposure",
        "daily_loss",
    }


def test_dashboard_v02_shows_both_runtimes_separately(database_path: Path) -> None:
    """Crypto and equity report their own state, from their own durable trail."""
    with connect(database_path) as setup:
        state.record_system_event(
            connection=setup,
            event_timestamp=T0 - timedelta(minutes=30),
            event_type="RUNTIME_STARTED",
            message="Crypto runtime started.",
        )
        state.upsert_runtime_checkpoint(
            setup,
            symbol=BTC,
            last_processed_bar_timestamp=T0 - timedelta(minutes=15),
            updated_at=T0 - timedelta(minutes=1),
        )

    page = dashboard_page(database_path)
    panels = {panel.key: panel for panel in page.runtimes}

    assert set(panels) == {"crypto", "equity"}
    assert panels["crypto"].label == "Crypto runtime"
    assert panels["equity"].label == "Equity runtime"
    assert panels["crypto"].state == "RUNNING"
    # The equity service has recorded nothing, and the screen says exactly that
    # rather than borrowing the crypto runtime's state.
    assert panels["equity"].state == "NEVER STARTED"
    assert [row.symbol for row in panels["crypto"].checkpoints] == [BTC]
    assert panels["equity"].checkpoints == ()


def test_dashboard_global_status_reflects_shared_account_halt(
    database_path: Path,
) -> None:
    """CRITICAL REGRESSION. An UNKNOWN order shows as PAUSED, account-wide.

    And the dashboard performs zero writes getting there: the read connection
    is opened `mode=ro` with `PRAGMA query_only`, so a write would be refused
    by the engine rather than avoided by convention.
    """
    healthy = dashboard_page(database_path)
    assert healthy.account_safety is not None
    assert healthy.account_safety.safe_to_trade is True
    assert healthy.system_state != "PAUSED"

    with connect(database_path) as trading_process, pytest.raises(AmbiguousSubmissionError):
        buy_equity(
            trading_process,
            CombinedBrokerClient(submit_error=api_error(504, "gateway timeout")),
            quantity="1",
        )

    before = _database_fingerprint(database_path)
    page = dashboard_page(database_path)
    after = _database_fingerprint(database_path)

    assert page.account_safety is not None
    assert page.account_safety.state == state.ACCOUNT_SAFETY_UNSAFE_UNKNOWN
    assert page.account_safety.safe_to_trade is False
    assert page.account_safety.client_order_id is not None
    assert page.system_state == "PAUSED"
    assert any("UNSAFE_UNKNOWN" in reason for reason in page.attention)

    # Both runtimes are shown as held, not just the one that hit it.
    assert {panel.state for panel in page.runtimes} <= {"PAUSED", "NEVER STARTED"}

    # The trading-safety health row says the account is blocked.
    rows = {row.key: row for row in page.health}
    assert rows["account_safety"].status == state.ACCOUNT_SAFETY_UNSAFE_UNKNOWN
    assert rows["trading_safety"].status == "BLOCKED"

    assert after == before, "the dashboard wrote to the operational database"


def _database_fingerprint(path: Path) -> tuple[int, int]:
    """Size and mtime-ns of the database file, to prove nothing wrote to it."""
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def test_dashboard_v02_has_no_trading_write_surface() -> None:
    """CRITICAL. Still GET-only after the V0.2 additions, and still inert.

    The new panels are reads of new tables. Nothing in them created a route
    that could carry a command, and nothing in the package can reach an order
    submission even if a route were added.
    """
    from autotrader import dashboard as dashboard_package
    from autotrader.dashboard import api as dashboard_api

    application = dashboard_api.create_app()
    for route in application.routes:
        methods = set(getattr(route, "methods", set()) or set())
        assert not methods & {"POST", "PUT", "PATCH", "DELETE"}, getattr(route, "path", route)

    package_root = Path(dashboard_package.__file__).parent
    for module in sorted(package_root.rglob("*.py")):
        source = module.read_text(encoding="utf-8")
        for forbidden in (
            "submit_order",
            "execute_paper_order",
            "execute_equity_paper_order",
            "reconcile_paper_state",
            "set_account_safety_state",
            "consume_api_budget",
            "TradingClient(",
        ):
            assert forbidden not in source, f"{module.name} names {forbidden}"


def test_dashboard_reports_the_shared_api_budget_truthfully(
    database_path: Path,
) -> None:
    """Both budgets, always shown, counted across both runtimes."""
    with connect(database_path) as spender:
        api_budget.try_consume(spender, budget=state.API_BUDGET_TRADING, calls=7, now=T0)

    page = dashboard_page(database_path)
    rows = {row.key: row for row in page.api_budget}

    assert set(rows) == {"trading", "market_data"}
    assert rows["trading"].used == 7
    assert rows["trading"].limit == api_budget.DEFAULT_TRADING_LIMIT
    assert rows["trading"].remaining == api_budget.DEFAULT_TRADING_LIMIT - 7
    # An untouched budget is shown at zero rather than hidden: "no traffic" and
    # "not metered" are different states.
    assert rows["market_data"].used == 0


def test_a_slashless_crypto_symbol_is_not_counted_as_equity_exposure(
    database_path: Path,
) -> None:
    """REGRESSION. `BTCUSD` and `BTC/USD` are the same market.

    Alpaca reports a crypto position under either spelling depending on the
    response. Classifying on the slash alone put the slash-less one in the
    equity row, which is real crypto exposure attributed to the wrong book -
    harmless while only one book existed, wrong the moment the screen split
    them.
    """
    from autotrader.dashboard.broker import BrokerRead
    from autotrader.dashboard.service import asset_class_for, build_overview
    from autotrader.execution.paper import fetch_paper_account_state, fetch_paper_positions

    assert asset_class_for("BTCUSD") == asset_class_for("BTC/USD") == "CRYPTO"
    assert asset_class_for("SPY") == "EQUITY"

    client = CombinedBrokerClient(
        positions=[make_position(BTC, qty="0.1", market_value="10000", price="100000")]
    )
    page = build_overview(
        database_path=database_path,
        now=T0,
        broker=BrokerRead(
            ok=True,
            account=fetch_paper_account_state(client),
            positions=fetch_paper_positions(client),
            reason=None,
        ),
    )

    assert page.risk is not None
    rows = {row.key: row for row in page.risk.exposure}
    # The fake reports it the way Alpaca reports a crypto position: no slash.
    assert client.get_all_positions()[0].symbol == "BTCUSD"
    assert rows["crypto"].value.value == pytest.approx(10_000.0)
    assert rows["equity"].value.value == pytest.approx(0.0)
