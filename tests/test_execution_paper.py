"""Phase 7 tests: paper execution, its gates, and its failure semantics.

**Nothing here touches the network.** The Alpaca boundary is the only thing
faked, and the fakes return *real* alpaca-py models, so normalization is
exercised against the real response shapes rather than against a convenient
approximation of them. No test reads a real credential, and a test asserts that
sockets stay shut.

The tests that matter most are not the happy paths. They are the ones that pin
down what happens when the broker answers badly or does not answer at all:
an ambiguous submission must never be retried, an intent must be durable
before the request goes out, the broker must never be asked for more shares
than risk approved, and there must be no way to ask for a live order.
"""

from __future__ import annotations

import ast
import inspect
import json
import socket
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.models.trades import Trade
from alpaca.trading.enums import (
    AccountStatus,
    AssetClass,
    AssetExchange,
    OrderClass,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
)
from alpaca.trading.enums import (
    OrderSide as AlpacaOrderSide,
)
from alpaca.trading.models import Clock, Order, Position, TradeAccount
from alpaca.trading.requests import MarketOrderRequest
from typer.testing import CliRunner

from autotrader.cli import app
from autotrader.execution import models as execution_models
from autotrader.execution import paper
from autotrader.execution.models import (
    CLIENT_ORDER_ID_PREFIX,
    ExecutionInputError,
    OrderIntent,
    OrderSide,
    new_client_order_id,
)
from autotrader.execution.paper import (
    PAPER_TRADING_ENABLED_ENV,
    PAPER_TRADING_ENABLED_VALUE,
    AccountNotTradableError,
    AmbiguousSubmissionError,
    BrokerRejectedOrderError,
    ConfirmationRequiredError,
    DuplicatePreflightUnavailableError,
    ExecutionOutcome,
    MissingCredentialsError,
    PaperTradingDisabledError,
    ReferencePriceUnavailableError,
    UnsupportedBrokerStateError,
    build_market_order_request,
    build_risk_context,
    execute_paper_order,
    fetch_paper_account_state,
    fetch_paper_positions,
    fetch_reference_price,
    find_broker_order_by_client_id,
    paper_trading_enabled,
)
from autotrader.risk import APPROVED, POSITION_LIMIT, TRADING_DISABLED
from autotrader.state.sqlite import (
    INTENT_STATUS_REJECTED,
    INTENT_STATUS_SUBMITTED,
    INTENT_STATUS_UNKNOWN,
    connect,
    get_broker_order_by_intent,
    get_order_intent_by_client_id,
    get_position,
    initialize_database,
    list_broker_orders,
    list_order_intents,
    list_risk_events,
    list_system_events,
)

T0 = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
REFERENCE_PRICE = 500.0

runner = CliRunner()


def code_without_prose(source: str) -> str:
    """`source` with every docstring and comment removed.

    The source-level guarantees below are about *executable code*, not about
    prose. This module's own documentation names the things it forbids -
    "``paper=False`` appears nowhere", "no retry, no backoff" - so a naive
    substring scan would trip over the very sentences that explain the rule.
    Stripping docstrings and comments first makes the assertions mean what they
    say: the construct is absent from the code, not merely unmentioned.
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
    # `ast.unparse` drops comments as a side effect of round-tripping the tree.
    return ast.unparse(tree)


def module_code(module: object) -> str:
    return code_without_prose(Path(module.__file__).read_text())


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
    """An `APIError` shaped like the SDK's, optionally without a readable status.

    A `None` status models the case where the transport failed before a
    response existed - which must read as ambiguous, never as a rejection.
    """
    body = json.dumps({"code": 40010001, "message": message})
    if status_code is None:
        return APIError(body)
    return APIError(body, _FakeHTTPError(status_code))


def make_account(
    *,
    equity: str = "100000",
    cash: str = "100000",
    last_equity: str = "100000",
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
        last_equity=last_equity,
        trading_blocked=trading_blocked,
        account_blocked=account_blocked,
        trade_suspended_by_user=trade_suspended_by_user,
    )


def make_position(
    symbol: str = "SPY",
    *,
    qty: str = "10",
    market_value: str = "5000",
    avg_entry_price: str = "500",
    side: PositionSide = PositionSide.LONG,
) -> Position:
    return Position(
        asset_id=uuid4(),
        symbol=symbol,
        exchange=AssetExchange.NYSE,
        asset_class=AssetClass.US_EQUITY,
        avg_entry_price=avg_entry_price,
        qty=qty,
        side=side,
        cost_basis=str(float(qty) * float(avg_entry_price)),
        market_value=market_value,
    )


def make_order(
    *,
    client_order_id: str,
    symbol: str = "SPY",
    qty: str = "1",
    filled_qty: str = "0",
    filled_avg_price: str | None = None,
    status: OrderStatus = OrderStatus.ACCEPTED,
    side: AlpacaOrderSide = AlpacaOrderSide.BUY,
    order_id: str | None = None,
) -> Order:
    return Order(
        id=order_id or str(uuid4()),
        client_order_id=client_order_id,
        created_at=T0,
        updated_at=T0,
        submitted_at=T0,
        order_class=OrderClass.SIMPLE,
        time_in_force=TimeInForce.DAY,
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
    """Stands in for `TradingClient`. Records every call it receives.

    `preflight` and `submit` each accept a model to return or an exception to
    raise, so a test can describe exactly how the broker misbehaves.
    """

    def __init__(
        self,
        *,
        account: TradeAccount | None = None,
        positions: list[Position] | None = None,
        is_open: bool = True,
        preflight: object = None,
        submit: object = None,
        submit_status: OrderStatus = OrderStatus.ACCEPTED,
        submit_order_id: str | None = None,
    ) -> None:
        self._account = account if account is not None else make_account()
        self._positions = positions if positions is not None else []
        self._is_open = is_open
        # None means "the broker has no such order", modelled as Alpaca's 404.
        self._preflight = preflight if preflight is not None else api_error(404, "order not found")
        self._submit = submit
        self._submit_status = submit_status
        self._submit_order_id = submit_order_id
        self.submit_calls: list[MarketOrderRequest] = []
        self.preflight_calls: list[str] = []
        self.on_submit = None

    def get_account(self) -> TradeAccount:
        return self._account

    def get_all_positions(self) -> list[Position]:
        return list(self._positions)

    def get_clock(self) -> Clock:
        return Clock(timestamp=T0, is_open=self._is_open, next_open=T0, next_close=T0)

    def get_order_by_client_id(self, client_id: str) -> Order:
        self.preflight_calls.append(client_id)
        if isinstance(self._preflight, BaseException):
            raise self._preflight
        return self._preflight  # type: ignore[return-value]

    def submit_order(self, order_data: MarketOrderRequest) -> Order:
        self.submit_calls.append(order_data)
        if self.on_submit is not None:
            self.on_submit(order_data)
        if isinstance(self._submit, BaseException):
            raise self._submit
        if self._submit is not None:
            return self._submit  # type: ignore[return-value]
        return make_order(
            client_order_id=order_data.client_order_id,
            symbol=order_data.symbol,
            qty=str(int(order_data.qty)),
            side=order_data.side,
            status=self._submit_status,
            order_id=self._submit_order_id,
        )


class FakeDataClient:
    """Stands in for `StockHistoricalDataClient`."""

    def __init__(self, price: float | None = REFERENCE_PRICE, error: Exception | None = None):
        self._price = price
        self._error = error
        self.requests: list[object] = []

    def get_stock_latest_trade(self, request_params: object) -> dict[str, Trade]:
        self.requests.append(request_params)
        if self._error is not None:
            raise self._error
        if self._price is None:
            return {}
        symbol = request_params.symbol_or_symbols  # type: ignore[attr-defined]
        return {symbol: Trade(symbol=symbol, raw_data={"t": T0, "p": self._price, "s": 1})}


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
def enabled_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PAPER_TRADING_ENABLED_ENV, PAPER_TRADING_ENABLED_VALUE)


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "test-key-never-real")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret-never-real")


def run_execution(
    connection: sqlite3.Connection,
    client: FakeTradingClient | None = None,
    data_client: FakeDataClient | None = None,
    **kwargs: object,
):
    """Run the pipeline with sensible defaults for a BUY of one share."""
    payload: dict[str, object] = {
        "symbol": "SPY",
        "side": "BUY",
        "requested_quantity": 1,
        "now": T0,
    }
    payload.update(kwargs)
    return execute_paper_order(
        connection,
        trading_client=client if client is not None else FakeTradingClient(),
        data_client=data_client if data_client is not None else FakeDataClient(),
        **payload,  # type: ignore[arg-type]
    )


# ==========================================================================
# CRITICAL REGRESSION TESTS
# ==========================================================================


def test_ambiguous_submit_failure_is_never_retried(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """A timeout after submitting must stop dead, not try again.

    This is the single most important test in Phase 7. The broker may have
    accepted the order; re-sending it - under the same key or a fresh one -
    risks a duplicate real position. The only safe move is to record UNKNOWN
    and stop.
    """
    client = FakeTradingClient(submit=TimeoutError("read timed out"))

    with pytest.raises(AmbiguousSubmissionError):
        run_execution(connection, client)

    assert len(client.submit_calls) == 1, "an ambiguous outcome must not be retried"

    (intent,) = list_order_intents(connection)
    assert intent.status == INTENT_STATUS_UNKNOWN
    assert intent.client_order_id.startswith(CLIENT_ORDER_ID_PREFIX)
    assert intent.client_order_id == client.submit_calls[0].client_order_id

    # No broker order was recorded, because none is known to exist.
    assert list_broker_orders(connection) == []

    unknown_events = [
        event for event in list_system_events(connection) if event.event_type == paper.EVENT_UNKNOWN
    ]
    assert len(unknown_events) == 1
    assert intent.client_order_id in (unknown_events[0].message or "")


def test_order_intent_is_committed_before_broker_submission(
    database_path: Path, enabled_gate: None
) -> None:
    """The intent must be durable at the instant the broker is called.

    Proven from outside the writing connection: a second, independent
    connection opened *during* `submit_order` must already be able to see the
    committed row. If the intent were written in the same open transaction as
    the submission, this connection would see nothing.
    """
    observed: list[object] = []

    client = FakeTradingClient()

    def inspect_database_mid_submission(request: MarketOrderRequest) -> None:
        with connect(database_path) as independent:
            stored = get_order_intent_by_client_id(independent, request.client_order_id)
            observed.append(stored)

    client.on_submit = inspect_database_mid_submission

    with connect(database_path) as connection:
        result = run_execution(connection, client)

    assert len(observed) == 1
    committed = observed[0]
    assert committed is not None, "the intent was not committed before submit_order"
    assert committed.client_order_id == result.intent.client_order_id
    assert committed.symbol == "SPY"
    assert committed.approved_quantity == 1


def test_risk_clamped_quantity_is_the_only_quantity_sent_to_broker(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """Requested 100, risk allows 3, the broker is asked for exactly 3.

    The 5% per-symbol cap on $30,000 of equity is $1,500, which is three whole
    shares at $500. The requested 100 must appear nowhere in the request.
    """
    client = FakeTradingClient(
        account=make_account(equity="30000", cash="30000", last_equity="30000")
    )

    result = run_execution(connection, client, requested_quantity=100)

    assert result.risk_decision.approved is True
    assert result.risk_decision.approved_quantity == 3
    assert result.risk_decision.reason_code == POSITION_LIMIT

    (request,) = client.submit_calls
    assert request.qty == 3
    assert request.qty != 100

    (intent,) = list_order_intents(connection)
    assert intent.requested_quantity == 100
    assert intent.approved_quantity == 3

    (broker_order,) = list_broker_orders(connection)
    assert broker_order.quantity == 3


def test_live_mode_cannot_be_constructed_from_execution_api() -> None:
    """There must be no way to ask this package for a live client.

    Checked three ways: no public callable accepts a `paper`-like argument, the
    one client factory takes no parameters at all, and `paper=False` appears
    nowhere in the shipped source.
    """
    signature = inspect.signature(paper.create_paper_trading_client)
    assert signature.parameters == {}, "the client factory must take no arguments"

    for name in paper.__all__:
        member = getattr(paper, name)
        if not callable(member) or isinstance(member, type):
            continue
        parameters = inspect.signature(member).parameters
        for parameter in parameters:
            assert "paper" not in parameter.lower(), f"{name} exposes a paper switch"
            assert "live" not in parameter.lower(), f"{name} exposes a live switch"

    package_root = Path(paper.__file__).resolve().parents[1]
    for path in sorted(package_root.rglob("*.py")):
        code = code_without_prose(path.read_text())
        for forbidden in ("paper=False", "paper = False", "TRADING_LIVE"):
            assert forbidden not in code, f"{forbidden} found in {path}"


# ==========================================================================
# Paper-only construction
# ==========================================================================


def test_the_trading_client_is_always_constructed_with_paper_true(
    monkeypatch: pytest.MonkeyPatch, credentials: None
) -> None:
    captured: dict[str, object] = {}

    class SpyTradingClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self._retry = 3

    monkeypatch.setattr(paper, "TradingClient", SpyTradingClient)

    paper.create_paper_trading_client()

    assert captured["paper"] is True
    assert "api_key" in captured and "secret_key" in captured


def test_the_trading_client_does_not_silently_resubmit_orders(
    monkeypatch: pytest.MonkeyPatch, credentials: None
) -> None:
    """The SDK retries 429/504 internally; on POST /orders that is unsafe.

    A gateway timeout on an order submission is the ambiguous case this phase
    must classify as UNKNOWN, so the SDK's own retry is switched off. This test
    also pins the attribute name: if a future alpaca-py renames it, this fails
    loudly rather than quietly restoring blind retries.
    """
    from alpaca.trading.client import TradingClient

    assert hasattr(TradingClient("k", "s", paper=True), "_retry")

    client = paper.create_paper_trading_client()
    assert client._retry == 0


def test_the_source_contains_no_live_trading_path() -> None:
    code = module_code(paper)
    assert "paper=True" in code
    assert "paper=False" not in code
    assert "TRADING_LIVE" not in code


def test_no_cli_option_can_request_live_trading() -> None:
    result = runner.invoke(app, ["paper-submit", "--help"])
    assert result.exit_code == 0
    assert "--live" not in result.output
    assert "--paper " not in result.output
    assert "PAPER" in result.output


def test_the_cli_exposes_no_trade_or_live_submit_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "paper-submit" in result.output
    for forbidden in ("live-submit", " trade ", "go-live"):
        assert forbidden not in result.output


# ==========================================================================
# Gates
# ==========================================================================


def test_the_submission_gate_defaults_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PAPER_TRADING_ENABLED_ENV, raising=False)
    assert paper_trading_enabled() is False


@pytest.mark.parametrize(
    "value", ["", "false", "False", "FALSE", "TRUE", "True", "1", "yes", "on", "true ", "truthy"]
)
def test_only_the_exact_documented_value_opens_the_gate(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Anything but the canonical spelling fails closed, including `TRUE`."""
    monkeypatch.setenv(PAPER_TRADING_ENABLED_ENV, value)
    assert paper_trading_enabled() is (value.strip() == PAPER_TRADING_ENABLED_VALUE)


def test_surrounding_whitespace_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PAPER_TRADING_ENABLED_ENV, "  true  ")
    assert paper_trading_enabled() is True


def test_a_closed_gate_blocks_submission_before_any_broker_call(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(PAPER_TRADING_ENABLED_ENV, raising=False)
    client = FakeTradingClient()

    with pytest.raises(PaperTradingDisabledError):
        run_execution(connection, client)

    assert client.submit_calls == []
    assert list_order_intents(connection) == []


def test_a_wrong_confirmation_token_is_refused() -> None:
    for token in ("paper", "Paper", "PAPER ", "", None, "YES"):
        with pytest.raises(ConfirmationRequiredError):
            paper.require_confirmation(token)  # type: ignore[arg-type]

    paper.require_confirmation("PAPER")


def test_missing_credentials_fail_before_any_broker_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    with pytest.raises(MissingCredentialsError) as error:
        paper.create_paper_trading_client()

    assert "ALPACA_API_KEY" in str(error.value)


def test_a_credential_value_never_appears_in_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "   ")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "super-secret-value")

    with pytest.raises(MissingCredentialsError) as error:
        paper.create_paper_trading_client()

    assert "super-secret-value" not in str(error.value)


def test_a_credential_never_reaches_the_client_order_id() -> None:
    for _ in range(20):
        generated = new_client_order_id()
        assert generated.startswith(CLIENT_ORDER_ID_PREFIX)
        assert len(generated) <= execution_models.MAX_CLIENT_ORDER_ID_LENGTH


# ==========================================================================
# Request validation
# ==========================================================================


def test_an_unsupported_symbol_is_rejected(connection: sqlite3.Connection) -> None:
    client = FakeTradingClient()
    with pytest.raises(ExecutionInputError):
        run_execution(connection, client, symbol="TSLA")
    assert client.submit_calls == []


def test_the_supported_universe_matches_phase_one(self_check: None = None) -> None:
    """The duplicated universe tuple must not drift from Phase 1's."""
    from autotrader.data.historical import SUPPORTED_SYMBOLS as PHASE_ONE_SYMBOLS

    assert execution_models.SUPPORTED_SYMBOLS == PHASE_ONE_SYMBOLS


@pytest.mark.parametrize("side", ["SHORT", "sell_short", "", "HOLD", "BUY_TO_COVER"])
def test_an_invalid_side_is_rejected(connection: sqlite3.Connection, side: str) -> None:
    client = FakeTradingClient()
    with pytest.raises(ExecutionInputError):
        run_execution(connection, client, side=side)
    assert client.submit_calls == []


@pytest.mark.parametrize("quantity", [0, -1, -100])
def test_a_non_positive_quantity_is_rejected(connection: sqlite3.Connection, quantity: int) -> None:
    client = FakeTradingClient()
    with pytest.raises(ExecutionInputError):
        run_execution(connection, client, requested_quantity=quantity)
    assert client.submit_calls == []


@pytest.mark.parametrize("quantity", [1.5, 0.1, 2.0, "3"])
def test_a_fractional_quantity_is_refused_not_rounded(
    connection: sqlite3.Connection, quantity: object
) -> None:
    """Even `2.0` is refused: a float quantity means something upstream lost its type."""
    client = FakeTradingClient()
    with pytest.raises(ExecutionInputError):
        run_execution(connection, client, requested_quantity=quantity)
    assert client.submit_calls == []


# ==========================================================================
# Reference price
# ==========================================================================


def test_the_reference_price_comes_from_the_iex_feed() -> None:
    data_client = FakeDataClient(price=501.25)

    price = fetch_reference_price(data_client, "SPY")

    assert price == 501.25
    (request,) = data_client.requests
    assert request.feed is DataFeed.IEX
    assert request.symbol_or_symbols == "SPY"


def test_a_missing_price_fails_closed(connection: sqlite3.Connection, enabled_gate: None) -> None:
    client = FakeTradingClient()

    with pytest.raises(ReferencePriceUnavailableError):
        run_execution(connection, client, data_client=FakeDataClient(price=None))

    assert client.submit_calls == []
    assert list_order_intents(connection) == []


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
def test_an_invalid_price_fails_closed(
    connection: sqlite3.Connection, enabled_gate: None, price: float
) -> None:
    client = FakeTradingClient()

    with pytest.raises(ReferencePriceUnavailableError):
        run_execution(connection, client, data_client=FakeDataClient(price=price))

    assert client.submit_calls == []


def test_a_market_data_failure_fails_closed(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient()
    data_client = FakeDataClient(error=api_error(500, "data feed down"))

    with pytest.raises(ReferencePriceUnavailableError):
        run_execution(connection, client, data_client=data_client)

    assert client.submit_calls == []


def test_the_price_is_not_read_from_stored_parquet() -> None:
    """Sizing a live order against yesterday's file would be wrong."""
    code = module_code(paper).lower()
    for token in ("parquet", "read_bars", "data/raw"):
        assert token not in code, token


# ==========================================================================
# Account state and the risk context mapping
# ==========================================================================


def test_the_account_maps_onto_the_risk_context() -> None:
    account = fetch_paper_account_state(
        FakeTradingClient(account=make_account(equity="100000", cash="40000", last_equity="102000"))
    )

    assert account.equity == 100000.0
    assert account.cash == 40000.0
    assert account.start_of_day_equity == 102000.0
    assert account.daily_pnl == pytest.approx(-2000.0)


def test_total_exposure_is_the_sum_of_long_market_values() -> None:
    client = FakeTradingClient(
        positions=[
            make_position("SPY", qty="10", market_value="5000"),
            make_position("QQQ", qty="5", market_value="2500"),
            make_position("AAPL", qty="1", market_value="200"),
        ]
    )
    account = fetch_paper_account_state(client)
    positions = fetch_paper_positions(client)

    context = build_risk_context(account, positions, "SPY")

    assert context.total_exposure == pytest.approx(7700.0)


def test_symbol_exposure_and_position_come_from_that_symbol() -> None:
    client = FakeTradingClient(
        positions=[
            make_position("SPY", qty="10", market_value="5000", avg_entry_price="480"),
            make_position("QQQ", qty="5", market_value="2500"),
        ]
    )
    account = fetch_paper_account_state(client)
    positions = fetch_paper_positions(client)

    spy = build_risk_context(account, positions, "SPY")
    assert spy.symbol_exposure == pytest.approx(5000.0)
    assert spy.current_position_quantity == 10

    nvda = build_risk_context(account, positions, "NVDA")
    assert nvda.symbol_exposure == 0.0
    assert nvda.current_position_quantity == 0
    assert nvda.total_exposure == pytest.approx(7500.0)


def test_daily_pnl_is_derived_from_last_equity() -> None:
    client = FakeTradingClient(
        account=make_account(equity="98000", cash="98000", last_equity="100000")
    )
    account = fetch_paper_account_state(client)
    context = build_risk_context(account, {}, "SPY")

    assert context.start_of_day_equity == 100000.0
    assert context.daily_pnl == pytest.approx(-2000.0)
    # -2% is exactly the halt threshold, so new entries stop.
    assert context.daily_pnl / context.start_of_day_equity == pytest.approx(-0.02)


def test_a_missing_last_equity_is_reported_not_guessed() -> None:
    client = FakeTradingClient(account=make_account(last_equity=None))  # type: ignore[arg-type]

    with pytest.raises(UnsupportedBrokerStateError) as error:
        fetch_paper_account_state(client)

    assert "last_equity" in str(error.value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"trading_blocked": True},
        {"account_blocked": True},
        {"trade_suspended_by_user": True},
        {"status": AccountStatus.INACTIVE},
        {"status": AccountStatus.ACCOUNT_CLOSED},
    ],
)
def test_a_blocked_account_fails_closed(
    connection: sqlite3.Connection, enabled_gate: None, kwargs: dict[str, object]
) -> None:
    client = FakeTradingClient(account=make_account(**kwargs))  # type: ignore[arg-type]

    with pytest.raises(AccountNotTradableError):
        run_execution(connection, client)

    assert client.submit_calls == []
    assert list_order_intents(connection) == []


def test_a_blocked_account_also_blocks_a_sell(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """An account that cannot trade cannot be trusted to process an exit either."""
    client = FakeTradingClient(
        account=make_account(trading_blocked=True),
        positions=[make_position("SPY", qty="10", market_value="5000")],
    )

    with pytest.raises(AccountNotTradableError):
        run_execution(connection, client, side="SELL")

    assert client.submit_calls == []


def test_a_short_position_is_refused_rather_than_treated_as_long() -> None:
    client = FakeTradingClient(
        positions=[make_position("SPY", qty="-5", market_value="-2500", side=PositionSide.SHORT)]
    )

    with pytest.raises(UnsupportedBrokerStateError) as error:
        fetch_paper_positions(client)

    assert "SHORT" in str(error.value)


# ==========================================================================
# Risk integration
# ==========================================================================


def test_a_risk_rejected_buy_never_reaches_the_broker(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """The kill switch stops an entry before an intent or a request exists."""
    client = FakeTradingClient()

    result = run_execution(connection, client, trading_enabled=False)

    assert result.outcome is ExecutionOutcome.REJECTED_BY_RISK
    assert result.risk_decision.reason_code == TRADING_DISABLED
    assert client.submit_calls == []
    assert client.preflight_calls == []
    assert list_order_intents(connection) == []
    assert list_broker_orders(connection) == []


def test_an_approved_buy_submits_the_risk_approved_quantity(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient()

    result = run_execution(connection, client, requested_quantity=2)

    assert result.risk_decision.reason_code == APPROVED
    assert result.risk_decision.approved_quantity == 2
    (request,) = client.submit_calls
    assert request.qty == 2


def test_a_sell_uses_the_risk_approved_quantity(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """An oversized exit is clamped to the position, never allowed to short."""
    client = FakeTradingClient(positions=[make_position("SPY", qty="4", market_value="2000")])

    result = run_execution(connection, client, side="SELL", requested_quantity=10)

    assert result.risk_decision.approved is True
    assert result.risk_decision.approved_quantity == 4
    (request,) = client.submit_calls
    assert request.qty == 4
    assert request.side is AlpacaOrderSide.SELL


def test_a_risk_reducing_sell_still_works_under_the_kill_switch(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """Phase 5's contract: a kill switch must never trap an open position."""
    client = FakeTradingClient(positions=[make_position("SPY", qty="3", market_value="1500")])

    result = run_execution(
        connection, client, side="SELL", requested_quantity=3, trading_enabled=False
    )

    assert result.outcome is ExecutionOutcome.SUBMITTED
    (request,) = client.submit_calls
    assert request.qty == 3
    assert request.side is AlpacaOrderSide.SELL


def test_a_sell_while_flat_is_rejected(connection: sqlite3.Connection, enabled_gate: None) -> None:
    client = FakeTradingClient(positions=[])

    result = run_execution(connection, client, side="SELL")

    assert result.outcome is ExecutionOutcome.REJECTED_BY_RISK
    assert client.submit_calls == []


def test_the_risk_decision_is_persisted_for_both_outcomes(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    run_execution(connection, FakeTradingClient(), trading_enabled=False)
    run_execution(connection, FakeTradingClient())

    events = list_risk_events(connection)
    assert [event.decision for event in events] == ["REJECTED", "APPROVED"]
    assert events[0].reason_code == TRADING_DISABLED
    assert events[1].reason_code == APPROVED
    assert all(event.symbol == "SPY" for event in events)


def test_the_risk_engine_is_not_modified_to_persist_itself() -> None:
    """Persistence stays the caller's job; the engine remains a pure calculator."""
    from autotrader.risk import engine

    source = inspect.getsource(engine).lower()
    assert "sqlite" not in source
    assert "record_risk_event" not in source


# ==========================================================================
# Order intent and the client order id
# ==========================================================================


def test_the_client_order_id_is_created_once_and_stored_before_the_broker_call(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient()

    result = run_execution(connection, client)

    (stored,) = list_order_intents(connection)
    (request,) = client.submit_calls
    assert result.intent is not None
    assert stored.client_order_id == result.intent.client_order_id
    assert stored.client_order_id == request.client_order_id
    assert client.preflight_calls == [stored.client_order_id]


def test_each_execution_gets_its_own_client_order_id(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    first = run_execution(connection, FakeTradingClient())
    second = run_execution(connection, FakeTradingClient())

    assert first.intent.client_order_id != second.intent.client_order_id
    assert len({intent.client_order_id for intent in list_order_intents(connection)}) == 2


def test_a_duplicate_local_client_order_id_is_rejected(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    from autotrader.state.sqlite import DuplicateOrderIntentError, record_order_intent

    record_order_intent(
        connection,
        client_order_id="autotrader-fixed",
        created_at=T0,
        symbol="SPY",
        side="BUY",
        requested_quantity=1,
        approved_quantity=1,
        reference_price=REFERENCE_PRICE,
        risk_reason_code=APPROVED,
    )

    with pytest.raises(DuplicateOrderIntentError):
        record_order_intent(
            connection,
            client_order_id="autotrader-fixed",
            created_at=T0,
            symbol="SPY",
            side="BUY",
            requested_quantity=1,
            approved_quantity=1,
            reference_price=REFERENCE_PRICE,
            risk_reason_code=APPROVED,
        )


def test_an_intent_cannot_be_built_with_more_than_risk_approved() -> None:
    with pytest.raises(ExecutionInputError):
        OrderIntent(
            symbol="SPY",
            side=OrderSide.BUY,
            requested_quantity=1,
            approved_quantity=5,
            reference_price=REFERENCE_PRICE,
            risk_reason_code=APPROVED,
            created_at=T0,
        )


def test_an_intent_requires_an_aware_timestamp() -> None:
    with pytest.raises(ExecutionInputError):
        OrderIntent(
            symbol="SPY",
            side=OrderSide.BUY,
            requested_quantity=1,
            approved_quantity=1,
            reference_price=REFERENCE_PRICE,
            risk_reason_code=APPROVED,
            created_at=datetime(2025, 1, 2, 14, 30),
        )


# ==========================================================================
# Duplicate preflight
# ==========================================================================


def test_a_clear_not_found_preflight_proceeds_to_submit(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(preflight=api_error(404, "order not found"))

    result = run_execution(connection, client)

    assert result.outcome is ExecutionOutcome.SUBMITTED
    assert len(client.submit_calls) == 1


def test_an_existing_broker_order_prevents_a_second_submission(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """If the broker already has it, nothing new is sent."""
    existing = make_order(client_order_id="autotrader-preexisting", qty="1")
    client = FakeTradingClient(preflight=existing)

    result = run_execution(connection, client)

    assert result.outcome is ExecutionOutcome.DUPLICATE
    assert client.submit_calls == [], "no order may be submitted when one already exists"

    (broker_order,) = list_broker_orders(connection)
    assert broker_order.broker_order_id == str(existing.id)

    (intent,) = list_order_intents(connection)
    assert intent.status == INTENT_STATUS_SUBMITTED

    duplicate_events = [
        event
        for event in list_system_events(connection)
        if event.event_type == paper.EVENT_DUPLICATE
    ]
    assert len(duplicate_events) == 1


@pytest.mark.parametrize(
    "failure",
    [
        api_error(500, "internal error"),
        api_error(503, "service unavailable"),
        api_error(None, "no status available"),
        TimeoutError("read timed out"),
        ConnectionResetError("connection reset by peer"),
    ],
)
def test_an_ambiguous_preflight_failure_fails_closed(
    connection: sqlite3.Connection, enabled_gate: None, failure: Exception
) -> None:
    """ "Could not check" must never be read as "there is no duplicate"."""
    client = FakeTradingClient(preflight=failure)

    with pytest.raises(DuplicatePreflightUnavailableError):
        run_execution(connection, client)

    assert client.submit_calls == [], "a failed duplicate check must block submission"


def test_the_preflight_uses_the_persisted_client_order_id(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient()

    result = run_execution(connection, client)

    assert client.preflight_calls == [result.intent.client_order_id]


def test_a_preflight_not_found_returns_none() -> None:
    client = FakeTradingClient(preflight=api_error(404))
    assert find_broker_order_by_client_id(client, "autotrader-x") is None


# ==========================================================================
# Submission outcomes
# ==========================================================================


BROKER_ORDER_UUID = "11111111-2222-3333-4444-555555555555"


def test_a_successful_submission_persists_the_broker_snapshot(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(submit_order_id=BROKER_ORDER_UUID)

    result = run_execution(connection, client)

    assert result.outcome is ExecutionOutcome.SUBMITTED
    (intent,) = list_order_intents(connection)
    assert intent.status == INTENT_STATUS_SUBMITTED

    stored = get_broker_order_by_intent(connection, result.order_intent_id)
    assert stored is not None
    assert stored.broker_order_id == BROKER_ORDER_UUID
    assert stored.client_order_id == intent.client_order_id
    assert stored.status == "accepted"
    assert stored.quantity == 1
    assert stored.submitted_at == T0
    assert stored.symbol == "SPY"
    assert stored.side == "BUY"


@pytest.mark.parametrize(
    "status",
    [
        OrderStatus.NEW,
        OrderStatus.PENDING_NEW,
        OrderStatus.ACCEPTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
    ],
)
def test_the_broker_status_is_stored_verbatim(
    connection: sqlite3.Connection, enabled_gate: None, status: OrderStatus
) -> None:
    """Phase 7 does not invent a local state machine over the broker's words."""
    client = FakeTradingClient(submit_status=status)

    result = run_execution(connection, client)

    stored = get_broker_order_by_intent(connection, result.order_intent_id)
    assert stored is not None
    assert stored.status == status.value


@pytest.mark.parametrize("status_code", [400, 403, 404, 422])
def test_a_definite_broker_rejection_marks_the_intent_rejected(
    connection: sqlite3.Connection, enabled_gate: None, status_code: int
) -> None:
    client = FakeTradingClient(submit=api_error(status_code, "insufficient buying power"))

    with pytest.raises(BrokerRejectedOrderError):
        run_execution(connection, client)

    assert len(client.submit_calls) == 1
    (intent,) = list_order_intents(connection)
    assert intent.status == INTENT_STATUS_REJECTED
    assert list_broker_orders(connection) == []


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("read timed out"),
        ConnectionResetError("connection reset by peer"),
        api_error(500, "internal server error"),
        api_error(502, "bad gateway"),
        api_error(504, "gateway timeout"),
        api_error(408, "request timeout"),
        api_error(429, "rate limited"),
        api_error(None, "transport failed before a response"),
    ],
)
def test_every_ambiguous_outcome_becomes_unknown_and_stops(
    connection: sqlite3.Connection, enabled_gate: None, failure: Exception
) -> None:
    """A 5xx, a timeout, a rate limit, or an unreadable status: all UNKNOWN.

    None of these prove the order was refused, so none may be reported as a
    rejection and none may be retried.
    """
    client = FakeTradingClient(submit=failure)

    with pytest.raises(AmbiguousSubmissionError):
        run_execution(connection, client)

    assert len(client.submit_calls) == 1
    (intent,) = list_order_intents(connection)
    assert intent.status == INTENT_STATUS_UNKNOWN
    assert list_broker_orders(connection) == []


def test_an_unknown_intent_keeps_its_client_order_id_for_recovery(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(submit=TimeoutError("boom"))

    with pytest.raises(AmbiguousSubmissionError):
        run_execution(connection, client)

    (intent,) = list_order_intents(connection)
    submitted_key = client.submit_calls[0].client_order_id
    assert intent.client_order_id == submitted_key

    found = get_order_intent_by_client_id(connection, submitted_key)
    assert found is not None and found.status == INTENT_STATUS_UNKNOWN


def test_the_source_implements_no_submission_retry_or_backoff() -> None:
    code = module_code(paper).lower()
    for token in ("backoff", "sleep", "while true", "for attempt in", "tenacity"):
        assert token not in code, token
    # `submit_order` is called from exactly one place, and not inside a loop.
    assert code.count("client.submit_order(") == 1


# ==========================================================================
# Submitted is not filled
# ==========================================================================


def test_a_successful_submission_does_not_fabricate_a_position(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """Acceptance is not a fill, so the local position must not move."""
    client = FakeTradingClient(
        positions=[make_position("SPY", qty="10", market_value="5000", avg_entry_price="480")]
    )

    run_execution(connection, client, requested_quantity=1)

    position = get_position(connection, "SPY")
    assert position is not None
    assert position.quantity == 10, "the observed position must not be incremented"
    assert position.average_price == 480.0


def test_a_flat_symbol_stays_flat_after_an_accepted_order(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(positions=[])

    result = run_execution(connection, client)

    assert result.outcome is ExecutionOutcome.SUBMITTED
    position = get_position(connection, "SPY")
    assert position is not None
    assert position.quantity == 0


def test_an_accepted_order_snapshot_records_no_fill(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(
        submit=make_order(client_order_id="x", qty="1", filled_qty="0", status=OrderStatus.NEW)
    )

    result = run_execution(connection, client)

    stored = get_broker_order_by_intent(connection, result.order_intent_id)
    assert stored is not None
    assert stored.filled_quantity == 0
    assert stored.filled_average_price is None
    assert stored.filled_at is None


def test_the_result_does_not_claim_submission_for_an_unknown_outcome(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(submit=TimeoutError("boom"))
    with pytest.raises(AmbiguousSubmissionError):
        run_execution(connection, client)
    # The honest answer for UNKNOWN is "not known to be submitted".
    assert paper.ExecutionOutcome.UNKNOWN not in (
        paper.ExecutionOutcome.SUBMITTED,
        paper.ExecutionOutcome.DUPLICATE,
    )


# ==========================================================================
# The broker request itself
# ==========================================================================


def build_intent(**overrides: object) -> OrderIntent:
    payload: dict[str, object] = {
        "symbol": "SPY",
        "side": OrderSide.BUY,
        "requested_quantity": 5,
        "approved_quantity": 3,
        "reference_price": REFERENCE_PRICE,
        "risk_reason_code": POSITION_LIMIT,
        "created_at": T0,
    }
    payload.update(overrides)
    return OrderIntent(**payload)  # type: ignore[arg-type]


def test_the_request_is_a_whole_share_market_day_order() -> None:
    request = build_market_order_request(build_intent())

    assert request.type is OrderType.MARKET
    assert request.time_in_force is TimeInForce.DAY
    assert request.qty == 3
    assert float(request.qty).is_integer()
    assert request.side is AlpacaOrderSide.BUY


def test_the_request_carries_the_client_order_id() -> None:
    intent = build_intent()
    request = build_market_order_request(intent)
    assert request.client_order_id == intent.client_order_id


def test_the_request_does_not_enable_extended_hours() -> None:
    request = build_market_order_request(build_intent())
    assert request.extended_hours is False
    assert request.to_request_fields()["extended_hours"] is False


def test_the_request_is_never_notional() -> None:
    request = build_market_order_request(build_intent())
    assert request.notional is None
    assert "notional" not in request.to_request_fields()


def test_a_sell_request_maps_to_the_sell_side() -> None:
    request = build_market_order_request(build_intent(side=OrderSide.SELL))
    assert request.side is AlpacaOrderSide.SELL


def test_no_advanced_order_type_is_constructible() -> None:
    code = module_code(paper)
    for token in (
        "LimitOrderRequest",
        "StopOrderRequest",
        "StopLimitOrderRequest",
        "TrailingStopOrderRequest",
        "TakeProfitRequest",
        "StopLossRequest",
        "OrderClass.BRACKET",
        "OrderClass.OCO",
        "notional=",
    ):
        assert token not in code, token


def test_no_streaming_client_is_used() -> None:
    code = module_code(paper)
    for token in ("TradingStream", "StockDataStream", "websocket"):
        assert token not in code, token


# ==========================================================================
# Dry run
# ==========================================================================


def test_a_dry_run_never_calls_submit_order(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient()

    result = run_execution(connection, client, dry_run=True)

    assert result.outcome is ExecutionOutcome.DRY_RUN
    assert client.submit_calls == []
    assert client.preflight_calls == []


def test_a_dry_run_persists_no_order_intent(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """No broker attempt will follow, so a stored intent would be noise."""
    run_execution(connection, FakeTradingClient(), dry_run=True)

    assert list_order_intents(connection) == []
    assert list_broker_orders(connection) == []


def test_a_dry_run_still_evaluates_and_records_risk(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(
        account=make_account(equity="30000", cash="30000", last_equity="30000")
    )

    result = run_execution(connection, client, requested_quantity=100, dry_run=True)

    assert result.risk_decision.approved_quantity == 3
    assert result.intent is not None
    assert result.intent.approved_quantity == 3
    assert len(list_risk_events(connection)) == 1


def test_a_dry_run_works_without_the_environment_gate(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checking an order must not require opening the submission gate."""
    monkeypatch.delenv(PAPER_TRADING_ENABLED_ENV, raising=False)
    client = FakeTradingClient()

    result = run_execution(connection, client, dry_run=True)

    assert result.outcome is ExecutionOutcome.DRY_RUN
    assert client.submit_calls == []


# ==========================================================================
# Market clock
# ==========================================================================


def test_a_closed_market_does_not_block_a_day_order(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """Alpaca queues a DAY order placed while closed; that is not an error."""
    client = FakeTradingClient(is_open=False)

    result = run_execution(connection, client)

    assert result.outcome is ExecutionOutcome.SUBMITTED
    assert result.clock.is_open is False


def test_the_clock_is_reported_in_the_result(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    result = run_execution(connection, FakeTradingClient(is_open=True))
    assert result.clock.is_open is True


# ==========================================================================
# CLI
# ==========================================================================


def invoke_paper_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: FakeTradingClient,
    *args: str,
    data_client: FakeDataClient | None = None,
):
    monkeypatch.setattr(paper, "create_paper_trading_client", lambda: client)
    monkeypatch.setattr(
        paper,
        "create_market_data_client",
        lambda: data_client if data_client is not None else FakeDataClient(),
    )
    return runner.invoke(
        app,
        ["paper-submit", "--db", str(tmp_path / "cli.db"), *args],
    )


def test_the_cli_submits_with_both_gates_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled_gate: None
) -> None:
    client = FakeTradingClient()

    result = invoke_paper_submit(
        tmp_path,
        monkeypatch,
        client,
        "--symbol",
        "SPY",
        "--side",
        "BUY",
        "--qty",
        "1",
        "--confirm-paper",
        "PAPER",
    )

    assert result.exit_code == 0, result.output
    assert len(client.submit_calls) == 1
    assert "PAPER ONLY" in result.output
    assert "SUBMITTED TO PAPER ACCOUNT" in result.output


@pytest.mark.parametrize("token", ["paper", "yes", "PAPERR", "Paper"])
def test_the_cli_never_submits_with_a_wrong_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled_gate: None, token: str
) -> None:
    client = FakeTradingClient()

    result = invoke_paper_submit(
        tmp_path,
        monkeypatch,
        client,
        "--symbol",
        "SPY",
        "--side",
        "BUY",
        "--qty",
        "1",
        "--confirm-paper",
        token,
    )

    assert result.exit_code == 1
    assert client.submit_calls == []


def test_the_cli_never_submits_without_a_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled_gate: None
) -> None:
    client = FakeTradingClient()

    result = invoke_paper_submit(
        tmp_path, monkeypatch, client, "--symbol", "SPY", "--side", "BUY", "--qty", "1"
    )

    assert result.exit_code == 1
    assert client.submit_calls == []


def test_the_cli_never_submits_with_the_gate_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(PAPER_TRADING_ENABLED_ENV, raising=False)
    client = FakeTradingClient()

    result = invoke_paper_submit(
        tmp_path,
        monkeypatch,
        client,
        "--symbol",
        "SPY",
        "--side",
        "BUY",
        "--qty",
        "1",
        "--confirm-paper",
        "PAPER",
    )

    assert result.exit_code == 1
    assert client.submit_calls == []
    assert PAPER_TRADING_ENABLED_ENV in result.output


def test_the_cli_dry_run_needs_neither_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(PAPER_TRADING_ENABLED_ENV, raising=False)
    client = FakeTradingClient()

    result = invoke_paper_submit(
        tmp_path,
        monkeypatch,
        client,
        "--symbol",
        "SPY",
        "--side",
        "BUY",
        "--qty",
        "1",
        "--dry-run",
    )

    assert result.exit_code == 0, result.output
    assert client.submit_calls == []
    assert "DRY RUN" in result.output


def test_the_cli_reports_a_risk_rejection_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled_gate: None
) -> None:
    client = FakeTradingClient(
        account=make_account(equity="100000", cash="100000", last_equity="110000")
    )

    result = invoke_paper_submit(
        tmp_path,
        monkeypatch,
        client,
        "--symbol",
        "SPY",
        "--side",
        "BUY",
        "--qty",
        "1",
        "--confirm-paper",
        "PAPER",
    )

    assert result.exit_code == 1
    assert "REJECTED BY RISK ENGINE" in result.output
    assert "Traceback" not in result.output
    assert client.submit_calls == []


def test_the_cli_uses_a_distinct_exit_code_for_an_unknown_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled_gate: None
) -> None:
    """An ambiguous outcome must never look like an ordinary refusal."""
    client = FakeTradingClient(submit=TimeoutError("read timed out"))

    result = invoke_paper_submit(
        tmp_path,
        monkeypatch,
        client,
        "--symbol",
        "SPY",
        "--side",
        "BUY",
        "--qty",
        "1",
        "--confirm-paper",
        "PAPER",
    )

    assert result.exit_code == 2
    assert "Traceback" not in result.output
    assert len(client.submit_calls) == 1


def test_the_cli_reports_an_unsupported_symbol_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled_gate: None
) -> None:
    client = FakeTradingClient()

    result = invoke_paper_submit(
        tmp_path,
        monkeypatch,
        client,
        "--symbol",
        "TSLA",
        "--side",
        "BUY",
        "--qty",
        "1",
        "--confirm-paper",
        "PAPER",
    )

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert client.submit_calls == []


def test_the_cli_preview_never_prints_a_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled_gate: None, credentials: None
) -> None:
    client = FakeTradingClient()

    result = invoke_paper_submit(
        tmp_path,
        monkeypatch,
        client,
        "--symbol",
        "SPY",
        "--side",
        "BUY",
        "--qty",
        "1",
        "--confirm-paper",
        "PAPER",
    )

    assert result.exit_code == 0, result.output
    for secret in ("test-key-never-real", "test-secret-never-real", "Authorization", "Bearer"):
        assert secret not in result.output


def test_the_cli_shows_the_market_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled_gate: None
) -> None:
    result = invoke_paper_submit(
        tmp_path,
        monkeypatch,
        FakeTradingClient(is_open=False),
        "--symbol",
        "SPY",
        "--side",
        "BUY",
        "--qty",
        "1",
        "--dry-run",
    )

    assert result.exit_code == 0, result.output
    assert "CLOSED" in result.output


# ==========================================================================
# Offline guarantees
# ==========================================================================


def test_the_tests_need_no_real_credentials(
    connection: sqlite3.Connection, enabled_gate: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    result = run_execution(connection, FakeTradingClient())

    assert result.outcome is ExecutionOutcome.SUBMITTED


def test_execution_makes_no_network_access(
    connection: sqlite3.Connection, enabled_gate: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("the execution path must not open a socket in tests")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

    result = run_execution(connection, FakeTradingClient())

    assert result.outcome is ExecutionOutcome.SUBMITTED


def test_the_domain_models_import_no_broker_sdk() -> None:
    """`models` must stay provider-neutral and standard-library only."""
    code = module_code(execution_models).lower()
    for token in ("alpaca", "requests", "sqlite", "http"):
        assert token not in code, token

    imported = {
        getattr(value, "__name__", "")
        for value in vars(execution_models).values()
        if inspect.ismodule(value)
    }
    assert imported <= {"math", "uuid"}, imported


def test_the_domain_layer_exposes_no_alpaca_model() -> None:
    for name in execution_models.__all__:
        member = getattr(execution_models, name)
        module = getattr(member, "__module__", "")
        assert not module.startswith("alpaca"), name
