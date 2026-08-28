"""Equity V0.2: the Alpaca paper equity execution boundary.

Offline. The Alpaca *models* are real - `Asset`, `Order`, `TradeAccount`,
`Position`, `Clock`, `MarketOrderRequest` - and only the transport is faked, so
what these tests exercise is the same translation a real submission would make.

Nothing here submits anything anywhere. The one test that proves the whole-share
order request is well formed builds the request and inspects it.
"""

from __future__ import annotations

import ast
import json
import socket
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import (
    AccountStatus,
    AssetClass,
    AssetExchange,
    AssetStatus,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
)
from alpaca.trading.enums import (
    OrderSide as AlpacaOrderSide,
)
from alpaca.trading.models import Asset, Calendar, Clock, Order, Position, TradeAccount
from alpaca.trading.requests import MarketOrderRequest

from autotrader.equity import EQUITY_SYMBOLS, EquityError
from autotrader.equity.session import SessionError
from autotrader.execution import equity as equity_execution
from autotrader.execution.equity import (
    EQUITY_ORDER_TIME_IN_FORCE,
    MINIMUM_SHARE_QUANTITY,
    AlpacaMarketCalendar,
    EquityAssetNotTradableError,
    MarketClosedError,
    build_equity_market_order_request,
    execute_equity_paper_order,
    fetch_equity_asset,
    fetch_market_clock,
    fetch_reference_price,
    normalize_share_quantity,
    require_market_open,
    to_wire_shares,
)
from autotrader.execution.models import (
    ExecutionInputError,
    OrderIntent,
    OrderSide,
)
from autotrader.execution.paper import (
    PAPER_TRADING_BASE_URL,
    ExecutionOutcome,
    PaperTradingDisabledError,
    QuantityBelowMinimumError,
    ReferencePriceUnavailableError,
)
from autotrader.risk import APPROVED, NO_POSITION_TO_EXIT, POSITION_LIMIT
from autotrader.state.sqlite import (
    INTENT_STATUS_SUBMITTED,
    connect,
    initialize_database,
    list_order_intents,
    list_positions,
)

SYMBOL = "SPY"
REFERENCE_PRICE = 500.0
T0 = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    database = tmp_path / "state.db"
    initialize_database(database)
    with connect(database) as open_connection:
        yield open_connection


@pytest.fixture
def enabled_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOTRADER_PAPER_TRADING_ENABLED", "true")


@pytest.fixture(autouse=True)
def closed_gate_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTOTRADER_PAPER_TRADING_ENABLED", raising=False)


def api_error(status_code: int | None, message: str = "broker said no") -> APIError:
    class _Response:
        def __init__(self, code: int) -> None:
            self.status_code = code

    class _HTTPError:
        def __init__(self, code: int) -> None:
            self.response = _Response(code)

    body = json.dumps({"code": 40010001, "message": message})
    return APIError(body) if status_code is None else APIError(body, _HTTPError(status_code))


def make_account(equity: str = "100000", cash: str = "100000") -> TradeAccount:
    return TradeAccount(
        id=uuid4(),
        account_number="PA0000000000",
        status=AccountStatus.ACTIVE,
        equity=equity,
        cash=cash,
        trading_blocked=False,
        account_blocked=False,
        trade_suspended_by_user=False,
        currency="USD",
        pattern_day_trader=False,
        transfers_blocked=False,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def make_asset(
    symbol: str = SYMBOL,
    *,
    asset_class: str = "us_equity",
    status: AssetStatus = AssetStatus.ACTIVE,
    tradable: bool = True,
) -> Asset:
    """An Alpaca `Asset` shaped exactly as the equity endpoint returns one.

    `min_order_size` and `min_trade_increment` are omitted because Alpaca
    reports both as null for equities - the real response, not a convenience.
    """
    return Asset(
        id=uuid4(),
        **{"class": asset_class},
        exchange=AssetExchange.ARCA,
        symbol=symbol,
        status=status,
        tradable=tradable,
        marginable=True,
        shortable=True,
        easy_to_borrow=True,
        fractionable=True,
    )


def make_position(symbol: str = SYMBOL, qty: str = "10", market_value: str = "5000") -> Position:
    return Position(
        asset_id=uuid4(),
        symbol=symbol,
        exchange=AssetExchange.ARCA,
        asset_class=AssetClass.US_EQUITY,
        avg_entry_price="480",
        qty=qty,
        side=PositionSide.LONG,
        market_value=market_value,
        cost_basis="4800",
        unrealized_pl="200",
        unrealized_plpc="0.04",
        current_price="500",
        lastday_price="495",
        change_today="0.01",
    )


def make_order(client_order_id: str, symbol: str = SYMBOL, qty: str = "10") -> Order:
    return Order(
        id=uuid4(),
        client_order_id=client_order_id,
        created_at=T0,
        updated_at=T0,
        submitted_at=T0,
        symbol=symbol,
        asset_id=uuid4(),
        asset_class=AssetClass.US_EQUITY,
        qty=qty,
        filled_qty="0",
        order_type=OrderType.MARKET,
        type=OrderType.MARKET,
        side=AlpacaOrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.ACCEPTED,
        extended_hours=False,
    )


class FakeTrade:
    def __init__(self, price: float) -> None:
        self.price = price


class FakeDataClient:
    def __init__(self, price: float | None = REFERENCE_PRICE, error: Exception | None = None):
        self._price = price
        self._error = error
        self.requests: list[object] = []

    def get_stock_latest_trade(self, request: object) -> dict[str, FakeTrade]:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        if self._price is None:
            return {}
        symbols = request.symbol_or_symbols  # type: ignore[attr-defined]
        keys = [symbols] if isinstance(symbols, str) else list(symbols)
        return {key: FakeTrade(self._price) for key in keys}


class FakeTradingClient:
    """The broker, faked at the transport. Records everything it is asked."""

    def __init__(
        self,
        *,
        account: TradeAccount | None = None,
        positions: list[Position] | None = None,
        asset: Asset | None = None,
        asset_error: APIError | None = None,
        is_open: bool = True,
        clock_error: APIError | None = None,
        preflight: APIError | Order | None = None,
        submit_error: APIError | None = None,
        calendar: list[Calendar] | None = None,
        calendar_error: APIError | None = None,
        orders: dict[str, object] | None = None,
        base_url: str = PAPER_TRADING_BASE_URL,
        sandbox: bool = True,
    ) -> None:
        # The two attributes `verify_paper_environment` reads, so this double
        # can stand in for the client a reconciliation pass is handed too.
        self._base_url = base_url
        self._sandbox = sandbox
        self._orders = dict(orders or {})
        self._account = account if account is not None else make_account()
        self._positions = positions if positions is not None else []
        self._asset = asset if asset is not None else make_asset()
        self._asset_error = asset_error
        self._is_open = is_open
        self._clock_error = clock_error
        self._preflight = preflight if preflight is not None else api_error(404, "not found")
        self._submit_error = submit_error
        self._calendar = calendar if calendar is not None else []
        self._calendar_error = calendar_error
        self.submit_calls: list[MarketOrderRequest] = []
        self.clock_calls = 0
        self.calendar_calls = 0

    def get_account(self) -> TradeAccount:
        return self._account

    def get_all_positions(self) -> list[Position]:
        return list(self._positions)

    def get_asset(self, symbol: str) -> Asset:
        if self._asset_error is not None:
            raise self._asset_error
        return self._asset

    def get_clock(self) -> Clock:
        self.clock_calls += 1
        if self._clock_error is not None:
            raise self._clock_error
        return Clock(
            timestamp=T0,
            is_open=self._is_open,
            next_open=T0 + timedelta(hours=1),
            next_close=T0 + timedelta(hours=2),
        )

    def get_calendar(self, filters: object = None) -> list[Calendar]:
        self.calendar_calls += 1
        if self._calendar_error is not None:
            raise self._calendar_error
        return list(self._calendar)

    def get_order_by_client_id(self, client_order_id: str) -> Order:
        if client_order_id in self._orders:
            answer = self._orders[client_order_id]
            if isinstance(answer, BaseException):
                raise answer
            return answer  # type: ignore[return-value]
        if isinstance(self._preflight, APIError):
            raise self._preflight
        return self._preflight

    def submit_order(self, request: MarketOrderRequest) -> Order:
        self.submit_calls.append(request)
        if self._submit_error is not None:
            raise self._submit_error
        return make_order(request.client_order_id, request.symbol, str(request.qty))


def run_execution(
    connection: sqlite3.Connection,
    client: FakeTradingClient | None = None,
    *,
    symbol: str = SYMBOL,
    side: str = "BUY",
    requested_quantity: Decimal = Decimal("1E9"),
    price: float | None = REFERENCE_PRICE,
    dry_run: bool = False,
    now: datetime = T0,
):
    return execute_equity_paper_order(
        connection,
        symbol=symbol,
        side=side,
        requested_quantity=requested_quantity,
        trading_client=client if client is not None else FakeTradingClient(),  # type: ignore[arg-type]
        data_client=FakeDataClient(price),  # type: ignore[arg-type]
        dry_run=dry_run,
        now=now,
    )


# ==========================================================================
# Whole-share quantity policy
# ==========================================================================


def test_the_quantity_policy_is_whole_shares_rounded_down() -> None:
    """Documented, tested, and never able to exceed what risk approved."""
    assert normalize_share_quantity(Decimal("3.99"), SYMBOL) == Decimal(3)
    assert normalize_share_quantity(Decimal("1"), SYMBOL) == Decimal(1)
    assert normalize_share_quantity(Decimal("1000.0000001"), SYMBOL) == Decimal(1000)


def test_a_quantity_below_one_share_is_refused_not_rounded_up() -> None:
    """Rounding up would send more than risk approved. That never happens."""
    with pytest.raises(QuantityBelowMinimumError):
        normalize_share_quantity(Decimal("0.99"), SYMBOL)
    assert Decimal(1) == MINIMUM_SHARE_QUANTITY


def test_a_normalized_quantity_never_exceeds_its_input() -> None:
    for value in ("1", "1.5", "7.9999", "250.5", "1000000.25"):
        amount = Decimal(value)
        assert normalize_share_quantity(amount, SYMBOL) <= amount


def test_the_wire_quantity_must_be_a_whole_number_of_shares() -> None:
    assert to_wire_shares(Decimal(3)) == 3.0
    with pytest.raises(QuantityBelowMinimumError):
        to_wire_shares(Decimal("3.5"))


def test_a_float_quantity_is_refused_rather_than_converted() -> None:
    with pytest.raises(ExecutionInputError):
        normalize_share_quantity(3.5, SYMBOL)  # type: ignore[arg-type]


# ==========================================================================
# The order request
# ==========================================================================


def build_intent(quantity: Decimal = Decimal(3), side: OrderSide = OrderSide.BUY) -> OrderIntent:
    return OrderIntent(
        symbol=SYMBOL,
        side=side,
        requested_quantity=Decimal("1E9"),
        approved_quantity=quantity,
        reference_price=REFERENCE_PRICE,
        risk_reason_code=APPROVED,
        created_at=T0,
    )


def test_the_order_is_market_day_and_regular_hours() -> None:
    """CRITICAL: the equity time in force is DAY, not the crypto GTC."""
    request = build_equity_market_order_request(build_intent())

    assert isinstance(request, MarketOrderRequest)
    assert request.time_in_force is TimeInForce.DAY
    assert EQUITY_ORDER_TIME_IN_FORCE is TimeInForce.DAY
    assert request.qty == 3.0
    assert request.notional is None
    assert not request.extended_hours


def test_the_order_carries_the_intents_client_order_id() -> None:
    intent = build_intent()

    assert build_equity_market_order_request(intent).client_order_id == intent.client_order_id


def test_a_sell_is_expressible_and_a_short_is_not() -> None:
    request = build_equity_market_order_request(build_intent(side=OrderSide.SELL))

    assert request.side is AlpacaOrderSide.SELL
    assert {member.value for member in OrderSide} == {"BUY", "SELL"}


def test_an_equity_intent_can_be_recorded_but_a_crypto_one_cannot_be_an_equity_order() -> None:
    """The intent table spans both books; each boundary still narrows its own."""
    assert (
        OrderIntent(
            symbol="TSLA",
            side=OrderSide.BUY,
            requested_quantity=Decimal(10),
            approved_quantity=Decimal(1),
            reference_price=REFERENCE_PRICE,
            risk_reason_code=APPROVED,
            created_at=T0,
        ).symbol
        == "TSLA"
    )

    with pytest.raises(EquityError):
        equity_execution.normalize_symbol("BTC/USD")


# ==========================================================================
# Broker metadata
# ==========================================================================


def test_a_us_equity_asset_is_accepted(connection: sqlite3.Connection) -> None:
    spec = fetch_equity_asset(FakeTradingClient(), SYMBOL)  # type: ignore[arg-type]

    assert spec.symbol == SYMBOL
    assert spec.asset_class == "us_equity"
    assert spec.status == "active"
    assert spec.tradable is True


@pytest.mark.parametrize(
    "asset",
    [
        make_asset(asset_class="crypto"),
        make_asset(status=AssetStatus.INACTIVE),
        make_asset(tradable=False),
    ],
)
def test_an_untradable_asset_fails_closed(asset: Asset) -> None:
    client = FakeTradingClient(asset=asset)

    with pytest.raises(EquityAssetNotTradableError):
        fetch_equity_asset(client, SYMBOL)  # type: ignore[arg-type]


def test_an_unreadable_asset_fails_closed() -> None:
    client = FakeTradingClient(asset_error=api_error(500, "upstream"))

    with pytest.raises(EquityAssetNotTradableError):
        fetch_equity_asset(client, SYMBOL)  # type: ignore[arg-type]


def test_a_reference_price_comes_from_the_iex_feed() -> None:
    data = FakeDataClient(REFERENCE_PRICE)

    price = fetch_reference_price(data, SYMBOL)  # type: ignore[arg-type]

    assert price == REFERENCE_PRICE
    assert data.requests[0].feed is equity_execution.REFERENCE_PRICE_FEED


@pytest.mark.parametrize(
    "client",
    [FakeDataClient(None), FakeDataClient(error=api_error(500)), FakeDataClient(0.0)],
)
def test_a_missing_or_unusable_price_fails_closed(client: FakeDataClient) -> None:
    with pytest.raises(ReferencePriceUnavailableError):
        fetch_reference_price(client, SYMBOL)  # type: ignore[arg-type]


# ==========================================================================
# The regular-hours gate
# ==========================================================================


def test_the_clock_gate_allows_an_open_session() -> None:
    client = FakeTradingClient(is_open=True)

    clock = require_market_open(client)  # type: ignore[arg-type]

    assert clock.is_open is True
    assert client.clock_calls == 1


def test_the_clock_gate_refuses_a_closed_session() -> None:
    """CRITICAL: regular market hours only, checked against the broker itself."""
    client = FakeTradingClient(is_open=False)

    with pytest.raises(MarketClosedError):
        require_market_open(client)  # type: ignore[arg-type]


def test_an_unreadable_clock_fails_closed() -> None:
    client = FakeTradingClient(clock_error=api_error(500))

    with pytest.raises(SessionError):
        fetch_market_clock(client)  # type: ignore[arg-type]


def test_a_closed_market_submits_nothing_and_leaves_no_intent(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """CRITICAL: the gate runs before the intent is written, so nothing dangles."""
    client = FakeTradingClient(is_open=False)

    with pytest.raises(MarketClosedError):
        run_execution(connection, client)

    assert client.submit_calls == []
    assert list_order_intents(connection) == []


# ==========================================================================
# Gates
# ==========================================================================


def test_the_environment_gate_is_closed_by_default(connection: sqlite3.Connection) -> None:
    with pytest.raises(PaperTradingDisabledError):
        run_execution(connection)


def test_a_dry_run_needs_no_gate_and_persists_nothing(connection: sqlite3.Connection) -> None:
    client = FakeTradingClient()

    result = run_execution(connection, client, dry_run=True)

    assert result.outcome is ExecutionOutcome.DRY_RUN
    assert client.submit_calls == []
    assert client.clock_calls == 0
    assert list_order_intents(connection) == []


# ==========================================================================
# Risk
# ==========================================================================


def test_risk_sizes_the_order_and_the_broker_never_sees_more(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """5% of $100,000 at $500 is 10 shares, and 10 is what is sent."""
    client = FakeTradingClient()

    result = run_execution(connection, client)

    assert result.outcome is ExecutionOutcome.SUBMITTED
    assert result.intent is not None
    assert result.intent.approved_quantity == Decimal(10)
    assert result.intent.approved_quantity <= result.risk_decision.approved_quantity
    [request] = client.submit_calls
    assert request.qty == 10.0


def test_an_existing_position_reduces_the_headroom(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(positions=[make_position(qty="8", market_value="4000")])

    result = run_execution(connection, client)

    assert result.risk_decision.reason_code == POSITION_LIMIT
    assert result.intent is not None
    assert result.intent.approved_quantity == Decimal(2)


def test_a_sell_while_flat_is_rejected_and_never_opens_a_short(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """CRITICAL: no short position can be created by any path."""
    client = FakeTradingClient(positions=[])

    result = run_execution(connection, client, side="SELL")

    assert result.outcome is ExecutionOutcome.REJECTED_BY_RISK
    assert result.risk_decision.reason_code == NO_POSITION_TO_EXIT
    assert client.submit_calls == []
    assert list_order_intents(connection) == []


def test_a_sell_is_clamped_to_the_position(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(positions=[make_position(qty="4", market_value="2000")])

    result = run_execution(connection, client, side="SELL")

    assert result.intent is not None
    assert result.intent.approved_quantity == Decimal(4)


def test_a_fractional_position_exits_only_its_whole_shares(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """The documented limitation, asserted rather than hidden."""
    client = FakeTradingClient(positions=[make_position(qty="2.5", market_value="1250")])

    result = run_execution(connection, client, side="SELL")

    assert result.risk_decision.approved_quantity == Decimal("2.5")
    assert result.intent is not None
    assert result.intent.approved_quantity == Decimal(2)


def test_a_risk_rejection_creates_no_intent_and_no_broker_request(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(positions=[make_position(qty="10", market_value="5000")])

    result = run_execution(connection, client)

    assert result.outcome is ExecutionOutcome.REJECTED_BY_RISK
    assert client.submit_calls == []
    assert list_order_intents(connection) == []


def test_a_price_too_high_for_one_share_of_headroom_is_refused(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """Below one share, an order is refused rather than rounded up to one."""
    client = FakeTradingClient(account=make_account(equity="1000", cash="1000"))

    with pytest.raises(QuantityBelowMinimumError):
        run_execution(connection, client, price=100.0)
    assert client.submit_calls == []


# ==========================================================================
# Ordering, duplicates, and ambiguity
# ==========================================================================


def test_the_intent_is_committed_before_the_broker_is_called(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """CRITICAL: test_equity_order_intent_is_committed_before_broker_submission."""
    seen: list[int] = []

    class RecordingClient(FakeTradingClient):
        def submit_order(self, request: MarketOrderRequest) -> Order:
            seen.append(len(list_order_intents(connection)))
            return super().submit_order(request)

    client = RecordingClient()
    result = run_execution(connection, client)

    assert seen == [1], "the intent must already be committed when submit_order runs"
    assert result.outcome is ExecutionOutcome.SUBMITTED
    [stored] = list_order_intents(connection)
    assert stored.status == INTENT_STATUS_SUBMITTED
    assert stored.client_order_id == result.intent.client_order_id  # type: ignore[union-attr]


def test_an_existing_broker_order_prevents_a_second_submission(
    connection: sqlite3.Connection, enabled_gate: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = make_order("autotrader-already-there")
    client = FakeTradingClient(preflight=existing)

    result = run_execution(connection, client)

    assert result.outcome is ExecutionOutcome.DUPLICATE
    assert client.submit_calls == []


def test_a_preflight_that_cannot_complete_refuses_to_submit(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """ "The check failed" is never read as "there is no duplicate"."""
    from autotrader.execution.paper import DuplicatePreflightUnavailableError

    client = FakeTradingClient(preflight=api_error(500, "upstream"))

    with pytest.raises(DuplicatePreflightUnavailableError):
        run_execution(connection, client)
    assert client.submit_calls == []


def test_an_ambiguous_submission_is_recorded_unknown_and_never_retried(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """CRITICAL: one attempt, one client_order_id, no second order."""
    from autotrader.execution.paper import AmbiguousSubmissionError
    from autotrader.state.sqlite import INTENT_STATUS_UNKNOWN

    client = FakeTradingClient(submit_error=api_error(504, "gateway timeout"))

    with pytest.raises(AmbiguousSubmissionError):
        run_execution(connection, client)

    assert len(client.submit_calls) == 1
    [stored] = list_order_intents(connection)
    assert stored.status == INTENT_STATUS_UNKNOWN


def test_a_definite_rejection_is_not_ambiguous(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    from autotrader.execution.paper import BrokerRejectedOrderError
    from autotrader.state.sqlite import INTENT_STATUS_REJECTED

    client = FakeTradingClient(submit_error=api_error(422, "insufficient buying power"))

    with pytest.raises(BrokerRejectedOrderError):
        run_execution(connection, client)

    [stored] = list_order_intents(connection)
    assert stored.status == INTENT_STATUS_REJECTED


def test_a_position_is_recorded_from_the_broker_and_never_from_an_accepted_order(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(positions=[make_position(qty="4", market_value="2000")])

    run_execution(connection, client)

    [stored] = list_positions(connection)
    assert stored.symbol == SYMBOL
    assert stored.quantity == Decimal(4)


# ==========================================================================
# The broker calendar
# ==========================================================================


def calendar_entry(day: str, open_time: str, close_time: str) -> Calendar:
    return Calendar(date=day, open=open_time, close=close_time)


def test_the_calendar_reads_sessions_and_converts_them_to_utc() -> None:
    """CRITICAL: naive Eastern in, UTC out - and holidays are simply absent."""
    client = FakeTradingClient(
        calendar=[
            calendar_entry("2025-11-26", "09:30", "16:00"),
            calendar_entry("2025-11-28", "09:30", "13:00"),
        ]
    )
    calendar = AlpacaMarketCalendar(client)  # type: ignore[arg-type]

    wednesday = calendar.session_for(date(2025, 11, 26))
    thanksgiving = calendar.session_for(date(2025, 11, 27))
    half_day = calendar.session_for(date(2025, 11, 28))

    assert wednesday is not None
    assert wednesday.open_utc == datetime(2025, 11, 26, 14, 30, tzinfo=UTC)
    assert thanksgiving is None
    assert half_day is not None
    assert half_day.close_utc == datetime(2025, 11, 28, 18, 0, tzinfo=UTC)


def test_the_calendar_is_cached_across_repeated_lookups() -> None:
    client = FakeTradingClient(calendar=[calendar_entry("2026-08-26", "09:30", "16:00")])
    calendar = AlpacaMarketCalendar(client)  # type: ignore[arg-type]

    for _ in range(20):
        calendar.session_for(date(2026, 8, 26))

    assert client.calendar_calls == 1
    assert calendar.api_calls == 1


def test_an_unreadable_calendar_fails_closed() -> None:
    client = FakeTradingClient(calendar_error=api_error(500))
    calendar = AlpacaMarketCalendar(client)  # type: ignore[arg-type]

    with pytest.raises(SessionError):
        calendar.session_for(date(2026, 8, 26))


def test_sessions_between_returns_only_the_requested_range() -> None:
    client = FakeTradingClient(
        calendar=[
            calendar_entry("2026-08-24", "09:30", "16:00"),
            calendar_entry("2026-08-25", "09:30", "16:00"),
            calendar_entry("2026-08-26", "09:30", "16:00"),
        ]
    )
    calendar = AlpacaMarketCalendar(client)  # type: ignore[arg-type]

    found = calendar.sessions_between(date(2026, 8, 25), date(2026, 8, 26))

    assert [session.session_date for session in found] == [date(2026, 8, 25), date(2026, 8, 26)]


# ==========================================================================
# Paper only, offline, and scope
# ==========================================================================


def test_the_equity_boundary_has_no_live_path() -> None:
    source = Path(equity_execution.__file__).read_text()
    for forbidden in ("paper=False", "paper = False", "TRADING_LIVE", "ALPACA_LIVE"):
        assert forbidden not in source, forbidden


def test_the_equity_boundary_constructs_no_trading_client_of_its_own() -> None:
    """One factory, in one file, with `paper=True` hardcoded."""
    source = Path(equity_execution.__file__).read_text()

    assert "TradingClient(" not in source
    assert "create_paper_trading_client" in source


def test_the_equity_boundary_exposes_no_paper_or_live_switch() -> None:
    import inspect

    for name in equity_execution.__all__:
        member = getattr(equity_execution, name)
        if not callable(member) or isinstance(member, type):
            continue
        for parameter in inspect.signature(member).parameters:
            assert "paper" not in parameter.lower(), f"{name} exposes a paper switch"
            assert "live" not in parameter.lower(), f"{name} exposes a live switch"


def test_the_equity_boundary_names_no_extended_hours_flag() -> None:
    """Regular hours only; the flag is never set, so it is never named."""
    tree = ast.parse(Path(equity_execution.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword):
            assert node.arg != "extended_hours"


def test_the_equity_execution_makes_no_network_access(
    connection: sqlite3.Connection, enabled_gate: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("the execution tests must not open a socket")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

    assert run_execution(connection, FakeTradingClient()).outcome is ExecutionOutcome.SUBMITTED


@pytest.mark.parametrize("symbol", EQUITY_SYMBOLS)
def test_every_configured_symbol_runs_the_same_path(
    connection: sqlite3.Connection, enabled_gate: None, symbol: str
) -> None:
    """One strategy, one risk policy, one execution path, ten symbols."""
    client = FakeTradingClient(asset=make_asset(symbol=symbol))

    result = run_execution(connection, client, symbol=symbol)

    assert result.outcome is ExecutionOutcome.SUBMITTED
    assert result.symbol == symbol
    assert result.intent is not None
    assert result.intent.approved_quantity == Decimal(10)


@pytest.mark.parametrize("symbol", ["BTC/USD", "ETH/USD", "GOOG", "VOO"])
def test_a_symbol_outside_the_universe_never_reaches_the_broker(
    connection: sqlite3.Connection, enabled_gate: None, symbol: str
) -> None:
    client = FakeTradingClient()

    with pytest.raises(EquityError):
        run_execution(connection, client, symbol=symbol)
    assert client.submit_calls == []
