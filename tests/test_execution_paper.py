"""C7 tests: crypto paper execution, its gates, and its failure semantics.

**Nothing here touches the network.** The Alpaca boundary is the only thing
faked, and the fakes return *real* alpaca-py models, so normalization is
exercised against the real response shapes rather than against a convenient
approximation of them. No test reads a real credential, and a test asserts that
sockets stay shut.

The tests that matter most are not the happy paths. They are the ones that pin
down what happens when the broker answers badly or does not answer at all:
an ambiguous submission must never be retried, an intent must be durable
before the request goes out, the broker must never be asked for more than risk
approved, and there must be no way to ask for a live order.

The crypto pivot adds three more contracts to that list: the order is GTC and
never DAY, the *broker's* live asset metadata decides order precision and
minimums, and the daily-loss baseline is a durable UTC-day figure rather than
an equity-session `last_equity`.
"""

from __future__ import annotations

import ast
import inspect
import json
import socket
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.enums import CryptoFeed
from alpaca.data.models.trades import Trade
from alpaca.data.requests import CryptoLatestTradeRequest
from alpaca.trading.enums import (
    AccountStatus,
    AssetClass,
    AssetStatus,
    OrderClass,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
)
from alpaca.trading.enums import (
    OrderSide as AlpacaOrderSide,
)
from alpaca.trading.models import Asset, Order, Position, TradeAccount
from alpaca.trading.requests import MarketOrderRequest
from typer.testing import CliRunner

from autotrader.cli import app
from autotrader.execution import models as execution_models
from autotrader.execution import paper
from autotrader.execution.models import (
    CLIENT_ORDER_ID_PREFIX,
    ExecutionError,
    ExecutionInputError,
    OrderIntent,
    OrderSide,
    new_client_order_id,
)
from autotrader.execution.paper import (
    ORDER_TIME_IN_FORCE,
    PAPER_TRADING_ENABLED_ENV,
    PAPER_TRADING_ENABLED_VALUE,
    USD_MINIMUM_ORDER_NOTIONAL,
    AccountNotTradableError,
    AmbiguousSubmissionError,
    AssetNotTradableError,
    BrokerRejectedOrderError,
    ConfirmationRequiredError,
    CryptoAssetSpec,
    DuplicatePreflightUnavailableError,
    ExecutionOutcome,
    MinimumNotionalError,
    MissingCredentialsError,
    PaperTradingDisabledError,
    QuantityBelowMinimumError,
    ReferencePriceUnavailableError,
    UnsupportedBrokerStateError,
    build_market_order_request,
    build_risk_context,
    effective_minimum_quantity,
    execute_paper_order,
    fetch_crypto_asset,
    fetch_paper_account_state,
    fetch_paper_positions,
    fetch_reference_price,
    find_broker_order_by_client_id,
    is_usd_quoted,
    minimum_quantity_from_notional,
    normalize_broker_quantity,
    paper_trading_enabled,
    to_wire_quantity,
)
from autotrader.risk import APPROVED, POSITION_LIMIT, TRADING_DISABLED
from autotrader.state.sqlite import (
    INTENT_STATUS_REJECTED,
    INTENT_STATUS_SUBMITTED,
    INTENT_STATUS_UNKNOWN,
    connect,
    get_broker_order_by_intent,
    get_daily_risk_baseline,
    get_order_intent_by_client_id,
    get_position,
    initialize_database,
    list_broker_orders,
    list_daily_risk_baselines,
    list_order_intents,
    list_risk_events,
    list_system_events,
)
from conftest import establish_account_safety

T0 = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)

#: A round reference price, so the 5% cap of a $200,000 account is exactly
#: 0.1 BTC and every expected quantity below can be read off by hand.
REFERENCE_PRICE = 100_000.0
SYMBOL = "BTC/USD"

#: The broker's own constraints, as the fakes report them. Deliberately awkward
#: numbers: nothing in `src/` may depend on their values.
MIN_ORDER_SIZE = 0.000026575
MIN_TRADE_INCREMENT = 0.000000001

#: A fixed broker order id. Alpaca's `Order.id` is a UUID, so the fakes have to
#: use a real one rather than a readable label.
BROKER_ORDER_UUID = "6f1a3c2e-0b4d-4a55-9d0e-2b7c8a1f4e33"

runner = CliRunner()


def code_without_prose(source: str) -> str:
    """`source` with every docstring and comment removed.

    The source-level guarantees below are about *executable code*, not about
    prose. This module's own documentation names the things it forbids -
    "``paper=False`` appears nowhere", "no retry, no backoff", "no DAY" - so a
    naive substring scan would trip over the very sentences that explain the
    rule. Stripping docstrings and comments first makes the assertions mean
    what they say: the construct is absent from the code, not merely unmentioned.
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
    equity: str = "200000",
    cash: str = "200000",
    status: AccountStatus = AccountStatus.ACTIVE,
    trading_blocked: bool = False,
    account_blocked: bool = False,
    trade_suspended_by_user: bool = False,
    **extra: object,
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
        **extra,
    )


def make_position(
    symbol: str = SYMBOL,
    *,
    qty: str = "0.05",
    market_value: str = "5000",
    avg_entry_price: str = "90000",
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


def make_asset(
    symbol: str = SYMBOL,
    *,
    asset_class: str = "crypto",
    status: AssetStatus = AssetStatus.ACTIVE,
    tradable: bool = True,
    fractionable: bool = True,
    min_order_size: float | None = MIN_ORDER_SIZE,
    min_trade_increment: float | None = MIN_TRADE_INCREMENT,
) -> Asset:
    return Asset(
        id=uuid4(),
        **{"class": asset_class},
        exchange="CRYPTO",
        symbol=symbol,
        status=status,
        tradable=tradable,
        marginable=False,
        shortable=False,
        easy_to_borrow=False,
        fractionable=fractionable,
        min_order_size=min_order_size,
        min_trade_increment=min_trade_increment,
        price_increment=1.0,
    )


def make_order(
    *,
    client_order_id: str,
    symbol: str = SYMBOL,
    qty: str = "0.01",
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
    """Stands in for `TradingClient`. Records every call it receives.

    `preflight` and `submit` each accept a model to return or an exception to
    raise, so a test can describe exactly how the broker misbehaves. There is
    deliberately **no** `get_clock`: a crypto execution path that called one
    would fail here with an AttributeError rather than quietly working.
    """

    def __init__(
        self,
        *,
        account: TradeAccount | None = None,
        positions: list[Position] | None = None,
        asset: Asset | BaseException | None = None,
        preflight: object = None,
        submit: object = None,
        submit_status: OrderStatus = OrderStatus.ACCEPTED,
        submit_order_id: str | None = None,
    ) -> None:
        self._account = account if account is not None else make_account()
        self._positions = positions if positions is not None else []
        self._asset = asset if asset is not None else make_asset()
        # None means "the broker has no such order", modelled as Alpaca's 404.
        self._preflight = preflight if preflight is not None else api_error(404, "order not found")
        self._submit = submit
        self._submit_status = submit_status
        self._submit_order_id = submit_order_id
        self.submit_calls: list[MarketOrderRequest] = []
        self.preflight_calls: list[str] = []
        self.asset_calls: list[str] = []
        self.on_submit = None

    def get_account(self) -> TradeAccount:
        return self._account

    def get_all_positions(self) -> list[Position]:
        return list(self._positions)

    def get_asset(self, symbol_or_asset_id: str) -> Asset:
        self.asset_calls.append(str(symbol_or_asset_id))
        if isinstance(self._asset, BaseException):
            raise self._asset
        return self._asset

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
            qty=repr(order_data.qty),
            side=order_data.side,
            status=self._submit_status,
            order_id=self._submit_order_id,
        )


class FakeDataClient:
    """Stands in for `CryptoHistoricalDataClient`.

    There is deliberately no `get_stock_latest_trade`: an execution path that
    reached for the equity endpoint would fail here rather than pass quietly.
    """

    def __init__(
        self,
        price: float | None = REFERENCE_PRICE,
        error: Exception | None = None,
        *,
        key: str | None = None,
    ):
        self._price = price
        self._error = error
        self._key = key
        self.requests: list[object] = []
        self.feeds: list[object] = []

    def get_crypto_latest_trade(
        self, request_params: object, feed: object = CryptoFeed.US
    ) -> dict[str, Trade]:
        self.requests.append(request_params)
        self.feeds.append(feed)
        if self._error is not None:
            raise self._error
        if self._price is None:
            return {}
        symbol = request_params.symbol_or_symbols  # type: ignore[attr-defined]
        key = self._key if self._key is not None else symbol
        return {key: Trade(symbol=symbol, raw_data={"t": T0, "p": self._price, "s": 1})}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    """A database in the state a running process actually submits from.

    A live system reconciles the full universe at startup and only then
    submits, so the execution boundary refuses to submit against an account
    whose safety nothing has ever established. That precondition belongs to the
    *database* rather than to any one connection - several tests here open a
    second connection to stand in for a second process, and all of them are
    looking at the same account.
    """
    path = initialize_database(tmp_path / "state.db")
    with connect(path) as setup:
        establish_account_safety(setup)
    return path


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
    """Run the pipeline with sensible defaults for a BUY of 0.01 BTC."""
    payload: dict[str, object] = {
        "symbol": SYMBOL,
        "side": "BUY",
        "requested_quantity": Decimal("0.01"),
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


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(TimeoutError("read timed out"), id="timeout"),
        pytest.param(ConnectionResetError("connection reset"), id="reset"),
        pytest.param(api_error(500, "internal error"), id="5xx"),
        pytest.param(api_error(504, "gateway timeout"), id="gateway-timeout"),
        pytest.param(api_error(429, "rate limited"), id="rate-limit"),
        pytest.param(api_error(408, "request timeout"), id="request-timeout"),
        pytest.param(api_error(None, "unreadable"), id="unreadable-status"),
    ],
)
def test_ambiguous_submit_failure_is_never_retried(
    connection: sqlite3.Connection, enabled_gate: None, failure: Exception
) -> None:
    """The single most important test in this milestone.

    A submission that ends ambiguously may or may not exist at the broker.
    Re-sending it risks a duplicate position, so `submit_order` is called
    exactly once, the intent is marked UNKNOWN, and its `client_order_id` is
    kept for a later phase to resolve against.
    """
    client = FakeTradingClient(submit=failure)

    with pytest.raises(AmbiguousSubmissionError):
        run_execution(connection, client)

    assert len(client.submit_calls) == 1, "an ambiguous outcome must not be retried"

    [intent] = list_order_intents(connection)
    assert intent.status == INTENT_STATUS_UNKNOWN
    assert intent.client_order_id.startswith(CLIENT_ORDER_ID_PREFIX)
    assert get_broker_order_by_intent(connection, intent.id) is None

    events = [
        event for event in list_system_events(connection) if event.event_type.endswith("UNKNOWN")
    ]
    assert len(events) == 1
    assert intent.client_order_id in (events[0].message or "")


def test_order_intent_is_committed_before_broker_submission(
    connection: sqlite3.Connection, enabled_gate: None, database_path: Path
) -> None:
    """A crash between the request and its response must still leave an anchor.

    Verified from an *independent* connection opened at the moment
    `submit_order` is entered, so a row that existed only in this connection's
    uncommitted transaction would fail here.
    """
    seen: list[str] = []

    def observe(order_data: MarketOrderRequest) -> None:
        with connect(database_path) as observer:
            seen.extend(intent.client_order_id for intent in list_order_intents(observer))

    client = FakeTradingClient()
    client.on_submit = observe

    result = run_execution(connection, client)

    assert result.intent is not None
    assert seen == [result.intent.client_order_id]


def test_risk_clamped_quantity_is_the_only_quantity_sent_to_broker(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """Risk sizes the order, and nothing downstream may enlarge it.

    5% of a $200,000 account is $10,000, which at $100,000 is 0.1 BTC. A
    request for a whole coin is clamped to that, and 0.1 is what the broker is
    asked for - never the 1 that was requested.
    """
    client = FakeTradingClient()

    result = run_execution(connection, client, requested_quantity=Decimal("1"))

    assert result.risk_decision.approved is True
    assert result.risk_decision.approved_quantity == Decimal("0.1")
    assert result.risk_decision.reason_code == POSITION_LIMIT

    [request] = client.submit_calls
    assert Decimal(repr(request.qty)) == Decimal("0.1")
    assert Decimal(repr(request.qty)) < Decimal("1")

    [intent] = list_order_intents(connection)
    assert intent.requested_quantity == Decimal("1")
    assert intent.approved_quantity == Decimal("0.100000000")


def test_live_mode_cannot_be_constructed_from_execution_api() -> None:
    """There must be no way to ask this package for a live client.

    Checked three ways: no public callable accepts a `paper`-like argument, the
    one client factory takes no parameters at all, and `paper=False` appears
    nowhere in the shipped source.
    """
    signature = inspect.signature(paper.create_paper_trading_client)
    assert signature.parameters == {}, "the client factory must take no arguments"

    for module in (paper, execution_models):
        for name in module.__all__:
            member = getattr(module, name)
            if not callable(member) or isinstance(member, type):
                continue
            for parameter in inspect.signature(member).parameters:
                assert "paper" not in parameter.lower(), f"{name} exposes a paper switch"
                assert "live" not in parameter.lower(), f"{name} exposes a live switch"

    package_root = Path(paper.__file__).resolve().parents[1]
    for path in sorted(package_root.rglob("*.py")):
        code = code_without_prose(path.read_text())
        for forbidden in ("paper=False", "paper = False", "TRADING_LIVE", "ALPACA_LIVE"):
            assert forbidden not in code, f"{forbidden} found in {path}"
    assert "paper=True" in module_code(paper)


def test_the_broker_quantity_never_exceeds_the_risk_decision(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """The crypto-era form of the clamped-quantity rule.

    Broker-increment normalization may only ever round **down**, so the exact
    quantity that reaches the broker is bounded by the risk-approved one across
    every increment the broker might report.
    """
    for increment in (0.000000001, 0.00001, 0.001, 0.01):
        client = FakeTradingClient(asset=make_asset(min_trade_increment=increment))
        result = run_execution(
            connection, client, requested_quantity=Decimal("1"), now=T0 + timedelta(days=1)
        )

        [request] = client.submit_calls
        sent = Decimal(repr(request.qty))
        assert sent <= result.risk_decision.approved_quantity, increment
        assert result.intent is not None
        assert result.intent.approved_quantity == sent


# --------------------------------------------------------------------------
# Alpaca's USD crypto minimum order notional
#
# The defect these four cover, exactly as it happened: a real BTC/USD BUY of
# 0.000014901 BTC at roughly $78,000 - about $1.16 - cleared every published
# broker constraint, because Alpaca's `min_order_size` of 0.000012417 BTC still
# encodes an older ~$1 floor. The dry run approved it. The paper endpoint then
# refused it outright:
#
#     cost basis must be >= minimal amount of order 10. No order was created.
#
# The broker's real floor is $10 of cost basis, which its asset metadata does
# not report. It has to be enforced locally, before the request exists.
# --------------------------------------------------------------------------

#: The exact BTC/USD quantity the integrated paper smoke attempted, and the
#: approximate price it attempted it at. Written down here so the regression is
#: anchored to the real event rather than to a convenient round number.
DEFECT_QUANTITY = Decimal("0.000014901")
DEFECT_PRICE = 78_000.0

#: Alpaca's live BTC/USD constraints at the time of the defect. `min_order_size`
#: is worth about $1.16 at `DEFECT_PRICE` - which is the whole problem.
DEFECT_MIN_ORDER_SIZE = 0.000012417
DEFECT_MIN_TRADE_INCREMENT = 0.000000001


def defect_asset_spec() -> CryptoAssetSpec:
    """BTC/USD exactly as the broker reported it when the order was refused."""
    return CryptoAssetSpec(
        symbol=SYMBOL,
        asset_class="crypto",
        status="active",
        tradable=True,
        fractionable=True,
        min_order_size=Decimal(str(DEFECT_MIN_ORDER_SIZE)),
        min_trade_increment=Decimal(str(DEFECT_MIN_TRADE_INCREMENT)),
    )


def test_usd_crypto_order_below_ten_dollars_is_refused_before_submission(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """CRITICAL REGRESSION #1. The exact order the broker refused, refused locally.

    BTC/USD BUY of 0.000014901 at ~$78,000 - about $1.16 of cost basis. The
    risk engine approves it: it is far below every cap, and risk knows nothing
    about broker minimums. The asset metadata permits it too, because
    `min_order_size` is smaller still. The order must nonetheless never leave
    this process.

    What is asserted is the whole contract: a deterministic local refusal, a
    `submit_order` call count of exactly zero, no order intent persisted, and
    an error that is emphatically **not** `AmbiguousSubmissionError` - the
    outcome is known, not unknown.
    """
    client = FakeTradingClient(
        asset=make_asset(
            min_order_size=DEFECT_MIN_ORDER_SIZE,
            min_trade_increment=DEFECT_MIN_TRADE_INCREMENT,
        )
    )
    data_client = FakeDataClient(price=DEFECT_PRICE)

    with pytest.raises(MinimumNotionalError) as error:
        run_execution(
            connection,
            client,
            data_client,
            requested_quantity=DEFECT_QUANTITY,
        )

    # A definite local rejection, never an ambiguous one.
    assert not isinstance(error.value, AmbiguousSubmissionError)
    assert isinstance(error.value, ExecutionError)

    # The number that matters: the broker was never asked.
    assert client.submit_calls == []
    assert client.preflight_calls == []

    # Nothing durable was created either, so there is nothing to reconcile.
    assert list_order_intents(connection) == []
    assert list_broker_orders(connection) == []

    message = str(error.value)
    assert "10" in message
    assert "No order was submitted" in message
    # The risk engine would have allowed it; the broker constraint is what did not.
    assert "Request a larger quantity" in message


def test_usd_crypto_minimum_quantity_uses_decimal_and_rounds_threshold_up() -> None:
    """CRITICAL REGRESSION #2. The threshold rounds UP, in Decimal, with no float.

    $10 at $78,000 is 0.000128205128... BTC, which does not land on the
    broker's 1e-9 increment. Rounding that threshold *down* to 0.000128205
    would produce a floor worth $9.99999... - a minimum that is itself below
    the minimum. It must round up, to 0.000128206.

    The arithmetic is exact: one increment below the threshold is worth less
    than $10 and the threshold itself is worth at least $10, and both
    comparisons are made in `Decimal`. Redoing either in binary floating point
    is what would make a $10 floor depend on a rounding artefact.
    """
    asset = defect_asset_spec()
    increment = asset.min_trade_increment

    threshold = minimum_quantity_from_notional(asset, reference_price=DEFECT_PRICE)

    assert isinstance(threshold, Decimal)
    assert threshold == Decimal("0.000128206")

    # It rounded UP: the exact quotient is strictly smaller than the threshold.
    price = Decimal(str(DEFECT_PRICE))
    assert threshold > USD_MINIMUM_ORDER_NOTIONAL / price
    assert threshold - increment < USD_MINIMUM_ORDER_NOTIONAL / price

    # And it rounded up to a *valid* increment, not to an arbitrary value.
    assert threshold % increment == 0

    # The threshold clears $10 and one increment below it does not. Exact.
    assert threshold * price >= USD_MINIMUM_ORDER_NOTIONAL
    assert (threshold - increment) * price < USD_MINIMUM_ORDER_NOTIONAL

    # Decimal is the authority everywhere: the constant, the threshold, and the
    # effective minimum are all exact, and none of them is a float.
    assert isinstance(USD_MINIMUM_ORDER_NOTIONAL, Decimal)
    effective = effective_minimum_quantity(asset, reference_price=DEFECT_PRICE)
    assert isinstance(effective, Decimal)
    # The notional floor binds here, not the asset's own - which is the defect.
    assert effective == threshold
    assert effective > asset.min_order_size


def test_broker_normalization_never_increases_risk_approved_quantity(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """CRITICAL REGRESSION #3. An undersized order is refused, never enlarged.

    The tempting fix for a below-minimum order is to round it up to the
    minimum. That would send more than the risk engine approved, which is the
    one direction this boundary may never move in - so the refusal stands and
    the quantity is left alone.

    Asserted twice over: directly, that `normalize_broker_quantity` raises
    rather than returning anything larger than its input; and through the whole
    pipeline, that no request was ever built and nothing was persisted.
    """
    asset = defect_asset_spec()
    approved = DEFECT_QUANTITY

    with pytest.raises(MinimumNotionalError):
        normalize_broker_quantity(approved, asset, reference_price=DEFECT_PRICE)

    # Nothing enlarged it on the way past: the effective minimum is strictly
    # larger than what risk approved, and that gap is a refusal, not a nudge.
    effective = effective_minimum_quantity(asset, reference_price=DEFECT_PRICE)
    assert effective > approved

    client = FakeTradingClient(
        asset=make_asset(
            min_order_size=DEFECT_MIN_ORDER_SIZE,
            min_trade_increment=DEFECT_MIN_TRADE_INCREMENT,
        )
    )
    with pytest.raises(MinimumNotionalError):
        run_execution(
            connection,
            client,
            FakeDataClient(price=DEFECT_PRICE),
            requested_quantity=approved,
        )

    assert client.submit_calls == []
    assert list_order_intents(connection) == []


def test_a_usd_crypto_order_above_the_minimum_still_submits(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """CRITICAL REGRESSION #4. The fix refuses undersized orders and nothing else.

    A BTC/USD BUY comfortably above the $10 floor - about $12 at the defect
    price, the same cushion the smoke uses - travels the existing path
    unchanged: risk approves it, it normalizes to the broker's increment, an
    intent is persisted, and exactly one order is submitted.

    The invariant that survives all of it: what reached the broker is not more
    than what risk approved.
    """
    quantity = Decimal("0.000154")  # ~$12.01 at DEFECT_PRICE
    client = FakeTradingClient(
        asset=make_asset(
            min_order_size=DEFECT_MIN_ORDER_SIZE,
            min_trade_increment=DEFECT_MIN_TRADE_INCREMENT,
        )
    )

    result = run_execution(
        connection,
        client,
        FakeDataClient(price=DEFECT_PRICE),
        requested_quantity=quantity,
    )

    assert result.outcome is ExecutionOutcome.SUBMITTED
    [request] = client.submit_calls

    sent = Decimal(repr(request.qty))
    assert sent <= result.risk_decision.approved_quantity
    assert sent <= quantity
    assert result.intent is not None
    assert result.intent.approved_quantity == sent

    # It cleared the broker's floor by construction, and the result says so.
    price = Decimal(str(DEFECT_PRICE))
    assert sent * price >= USD_MINIMUM_ORDER_NOTIONAL
    assert result.effective_minimum_quantity is not None
    assert sent >= result.effective_minimum_quantity


def test_the_ten_dollar_minimum_applies_to_sells_as_well_as_buys(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """Alpaca enforces the cost-basis floor on both sides, so this does too.

    Their crypto documentation states the USD-pair minimum without a side
    distinction, and Alpaca staff describe the cost-basis check as applying to
    buy orders, sell orders, and limit orders alike. A SELL below $10 would be
    refused by the endpoint exactly as a BUY is, so refusing it locally sends
    no request rather than making one that cannot succeed.

    This is the broker's constraint and not this system's, and the consequence
    is real: a position worth less than $10 cannot be closed until it recovers.
    That is why an opening order is sized with room above the floor rather than
    at it - see the smoke sizing in README.md.
    """
    dust = Decimal("0.00002")  # ~$1.56 at DEFECT_PRICE, above min_order_size
    asset = defect_asset_spec()
    assert dust > asset.min_order_size, "the asset floor must not be what refuses this"

    client = FakeTradingClient(
        positions=[make_position(qty="0.00002", market_value="1.56")],
        asset=make_asset(
            min_order_size=DEFECT_MIN_ORDER_SIZE,
            min_trade_increment=DEFECT_MIN_TRADE_INCREMENT,
        ),
    )

    with pytest.raises(MinimumNotionalError):
        run_execution(
            connection,
            client,
            FakeDataClient(price=DEFECT_PRICE),
            side="SELL",
            requested_quantity=dust,
        )

    assert client.submit_calls == []
    assert list_order_intents(connection) == []


def test_the_usd_minimum_is_scoped_to_usd_quoted_pairs() -> None:
    """The rule is Alpaca's USD-pair rule, not a generic broker minimum.

    Alpaca documents a separate `0.000000002` floor for its BTC, ETH, and USDT
    pairs, which the asset metadata already carries. Applying a $10 floor to
    one of those would refuse orders the broker would have taken, so the
    notional rule is conditioned on the quote currency.
    """
    assert is_usd_quoted("BTC/USD")
    assert is_usd_quoted("eth/usd")
    assert not is_usd_quoted("BTC/USDT")
    assert not is_usd_quoted("ETH/BTC")
    assert not is_usd_quoted("BTCUSD"), "a pair with no quote currency is not USD-quoted"

    non_usd = CryptoAssetSpec(
        symbol="ETH/BTC",
        asset_class="crypto",
        status="active",
        tradable=True,
        fractionable=True,
        min_order_size=Decimal("0.000000002"),
        min_trade_increment=Decimal("0.000000001"),
    )
    # Only the asset's own floor applies; the $10 rule is not generalized.
    assert effective_minimum_quantity(non_usd, reference_price=0.05) == non_usd.min_order_size


@pytest.mark.parametrize(
    "price",
    [
        pytest.param(0.0, id="zero"),
        pytest.param(-1.0, id="negative"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinite"),
    ],
)
def test_the_minimum_cannot_be_computed_from_an_unusable_price(price: float) -> None:
    """No trustworthy price means no threshold, which means no submission.

    A NaN in particular would compare False against every check and read as a
    passing one, so it is rejected explicitly rather than allowed to reach a
    comparison.
    """
    with pytest.raises(ReferencePriceUnavailableError):
        minimum_quantity_from_notional(defect_asset_spec(), reference_price=price)


def test_the_dollar_minimum_lives_in_exactly_one_place() -> None:
    """The $10 floor is a broker contract, so it is written down once.

    Freezing it in a single named constant is what makes it correctable when
    Alpaca changes it: there is one line to edit, and nothing downstream has
    its own copy of the number.
    """
    assert Decimal("10") == USD_MINIMUM_ORDER_NOTIONAL

    package_root = Path(paper.__file__).resolve().parents[1]
    literals = []
    for path in sorted(package_root.rglob("*.py")):
        code = code_without_prose(path.read_text())
        for node in ast.walk(ast.parse(code)):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Decimal":
                argument = node.args[0] if node.args else None
                if isinstance(argument, ast.Constant) and str(argument.value) == "10":
                    literals.append(str(path))
    assert literals == [str(Path(paper.__file__).resolve())], (
        f'Decimal("10") should appear only where USD_MINIMUM_ORDER_NOTIONAL is '
        f"defined, found: {literals}"
    )


# ==========================================================================
# PAPER ONLY
# ==========================================================================


def test_the_trading_client_is_always_constructed_with_paper_true(
    monkeypatch: pytest.MonkeyPatch, credentials: None
) -> None:
    captured: dict[str, object] = {}

    class Recording:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(paper, "TradingClient", Recording)
    paper.create_paper_trading_client()

    assert captured["paper"] is True


def test_the_trading_client_does_not_silently_resubmit_orders(
    monkeypatch: pytest.MonkeyPatch, credentials: None
) -> None:
    """The SDK retries 429/504 internally; that must be off for POST /orders."""

    class Recording:
        def __init__(self, **kwargs: object) -> None:
            self._retry = 3

    monkeypatch.setattr(paper, "TradingClient", Recording)
    client = paper.create_paper_trading_client()

    assert client._retry == 0


def test_the_source_contains_no_live_trading_path() -> None:
    source = module_code(paper)
    for forbidden in ("paper=False", "api.alpaca.markets/v2/orders", "LIVE"):
        assert forbidden not in source, forbidden


def test_no_cli_option_can_request_live_trading() -> None:
    from autotrader import cli

    source = code_without_prose(Path(cli.__file__).read_text())
    for forbidden in ("--live", "--paper", "--real", "live_trading"):
        assert forbidden not in source, forbidden


def test_the_cli_exposes_no_trade_or_live_submit_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "paper-submit" in result.output
    for forbidden in ("live-submit", " trade ", "go-live"):
        assert forbidden not in result.output, forbidden


# ==========================================================================
# The two gates
# ==========================================================================


def test_the_submission_gate_defaults_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PAPER_TRADING_ENABLED_ENV, raising=False)
    assert paper_trading_enabled() is False


@pytest.mark.parametrize(
    "value", ["", "false", "TRUE", "True", "1", "yes", "on", "enabled", "true ", " true"]
)
def test_only_the_exact_documented_value_opens_the_gate(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
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
    assert client.preflight_calls == []
    assert list_order_intents(connection) == []


def test_a_wrong_confirmation_token_is_refused() -> None:
    for token in (None, "", "paper", "PAPER ", "YES", "confirm"):
        with pytest.raises(ConfirmationRequiredError):
            paper.require_confirmation(token)
    paper.require_confirmation("PAPER")


def test_missing_credentials_fail_before_any_broker_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    with pytest.raises(MissingCredentialsError) as error:
        paper.create_paper_trading_client()

    message = str(error.value)
    assert "ALPACA_API_KEY" in message
    assert "ALPACA_SECRET_KEY" in message


def test_a_credential_value_never_appears_in_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "SECRET-KEY-VALUE")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "")

    with pytest.raises(MissingCredentialsError) as error:
        paper.create_paper_trading_client()

    assert "SECRET-KEY-VALUE" not in str(error.value)


def test_a_credential_never_reaches_the_client_order_id() -> None:
    for _ in range(50):
        client_order_id = new_client_order_id()
        assert client_order_id.startswith(CLIENT_ORDER_ID_PREFIX)
        assert len(client_order_id) <= execution_models.MAX_CLIENT_ORDER_ID_LENGTH
        suffix = client_order_id[len(CLIENT_ORDER_ID_PREFIX) :]
        assert set(suffix) <= set("0123456789abcdef-")


# ==========================================================================
# Request validation: crypto pairs and fractional quantities
# ==========================================================================


@pytest.mark.parametrize("symbol", ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "BTCUSD", "SOL/USD"])
def test_an_unsupported_symbol_is_rejected(connection: sqlite3.Connection, symbol: str) -> None:
    """The archived equity universe and non-canonical spellings are both out."""
    with pytest.raises(ExecutionInputError):
        run_execution(connection, symbol=symbol)


def test_the_supported_universe_is_exactly_the_two_pairs() -> None:
    from autotrader.data.historical import SUPPORTED_SYMBOLS as data_symbols

    expected = ("BTC/USD", "ETH/USD")
    assert expected == execution_models.SUPPORTED_SYMBOLS
    # The duplication is deliberate (the domain layer is stdlib-only); this is
    # what stops the two copies drifting apart.
    assert data_symbols == expected


@pytest.mark.parametrize("symbol", ["btc/usd", " BTC/USD ", "Eth/Usd"])
def test_a_supported_pair_is_normalized(connection: sqlite3.Connection, symbol: str) -> None:
    result = run_execution(connection, symbol=symbol, dry_run=True)
    assert result.symbol == symbol.strip().upper()


@pytest.mark.parametrize("side", ["SHORT", "sell_short", "", "hold"])
def test_an_invalid_side_is_rejected(connection: sqlite3.Connection, side: str) -> None:
    with pytest.raises(ExecutionInputError):
        run_execution(connection, side=side)


@pytest.mark.parametrize(
    "quantity",
    [Decimal(0), Decimal(-1), Decimal("-0.5"), Decimal("NaN"), Decimal("Infinity")],
)
def test_a_non_positive_or_non_finite_quantity_is_rejected(
    connection: sqlite3.Connection, quantity: Decimal
) -> None:
    with pytest.raises(ExecutionInputError):
        run_execution(connection, requested_quantity=quantity)


@pytest.mark.parametrize("quantity", [0.01, 1.0, "0.01", None, True])
def test_an_inexact_quantity_is_refused_rather_than_converted(
    connection: sqlite3.Connection, quantity: object
) -> None:
    """A binary float is an approximation; an approximation is not an order size."""
    with pytest.raises(ExecutionInputError):
        run_execution(connection, requested_quantity=quantity)


@pytest.mark.parametrize("text", ["0.0001", "1", "0.000000001", "1.25000000", " 0.5 "])
def test_a_quantity_string_parses_to_an_exact_decimal(text: str) -> None:
    assert execution_models.parse_quantity(text) == Decimal(text.strip())


@pytest.mark.parametrize("text", ["", "abc", "0", "-1", "1e", None])
def test_an_unparsable_quantity_string_is_rejected(text: object) -> None:
    with pytest.raises(ExecutionInputError):
        execution_models.parse_quantity(text)  # type: ignore[arg-type]


def test_a_fractional_quantity_reaches_the_broker(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient()

    run_execution(connection, client, requested_quantity=Decimal("0.00012345"))

    [request] = client.submit_calls
    assert Decimal(repr(request.qty)) == Decimal("0.00012345")


# ==========================================================================
# Reference price: crypto, never IEX
# ==========================================================================


def test_the_reference_price_comes_from_the_crypto_feed() -> None:
    data_client = FakeDataClient()

    price = fetch_reference_price(data_client, SYMBOL)

    assert price == REFERENCE_PRICE
    [request] = data_client.requests
    assert isinstance(request, CryptoLatestTradeRequest)
    assert request.symbol_or_symbols == SYMBOL
    assert data_client.feeds == [CryptoFeed.US]
    assert paper.REFERENCE_PRICE_FEED is CryptoFeed.US


def test_the_stock_latest_trade_request_is_absent_from_execution() -> None:
    """The equity price path is gone, not merely unused."""
    source = module_code(paper)
    for forbidden in (
        "StockLatestTradeRequest",
        "StockHistoricalDataClient",
        "get_stock_latest_trade",
        "DataFeed",
        "IEX",
    ):
        assert forbidden not in source, forbidden
    assert "CryptoLatestTradeRequest" in source
    assert "get_crypto_latest_trade" in source


def test_a_provider_symbol_spelling_still_resolves() -> None:
    """Alpaca keys some responses `BTCUSD`; both spell the same market."""
    data_client = FakeDataClient(key="BTCUSD")

    assert fetch_reference_price(data_client, SYMBOL) == REFERENCE_PRICE


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

    with pytest.raises(ReferencePriceUnavailableError):
        run_execution(
            connection, client, data_client=FakeDataClient(error=TimeoutError("no answer"))
        )

    assert client.submit_calls == []


def test_the_price_is_not_read_from_stored_parquet() -> None:
    source = module_code(paper)
    for forbidden in ("read_parquet", "parquet", "validate_frame", "run_backtest"):
        assert forbidden not in source, forbidden


# ==========================================================================
# Crypto asset metadata is the runtime authority
# ==========================================================================


def test_the_asset_metadata_is_read_from_the_broker() -> None:
    client = FakeTradingClient()

    spec = fetch_crypto_asset(client, SYMBOL)

    assert client.asset_calls == [SYMBOL]
    assert isinstance(spec, CryptoAssetSpec)
    assert spec.asset_class == AssetClass.CRYPTO.value
    assert spec.status == AssetStatus.ACTIVE.value
    assert spec.tradable is True
    assert spec.fractionable is True
    assert spec.min_order_size == Decimal(str(MIN_ORDER_SIZE))
    assert spec.min_trade_increment == Decimal(str(MIN_TRADE_INCREMENT))


@pytest.mark.parametrize(
    "asset",
    [
        pytest.param(make_asset(asset_class="us_equity"), id="equity"),
        pytest.param(make_asset(asset_class="crypto_perp"), id="perpetual-future"),
        pytest.param(make_asset(status=AssetStatus.INACTIVE), id="inactive"),
        pytest.param(make_asset(tradable=False), id="not-tradable"),
        pytest.param(make_asset(fractionable=False), id="not-fractionable"),
        pytest.param(make_asset(min_order_size=None), id="no-minimum"),
        pytest.param(make_asset(min_trade_increment=None), id="no-increment"),
        pytest.param(make_asset(min_order_size=0.0), id="zero-minimum"),
        pytest.param(make_asset(min_trade_increment=0.0), id="zero-increment"),
    ],
)
def test_unusable_asset_metadata_fails_closed(
    connection: sqlite3.Connection, enabled_gate: None, asset: Asset
) -> None:
    client = FakeTradingClient(asset=asset)

    with pytest.raises(AssetNotTradableError):
        run_execution(connection, client)

    assert client.submit_calls == []
    assert list_order_intents(connection) == []


def test_an_unreadable_asset_lookup_fails_closed(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(asset=api_error(500, "asset service down"))

    with pytest.raises(AssetNotTradableError):
        run_execution(connection, client)

    assert client.submit_calls == []


def test_no_broker_increment_or_minimum_is_hardcoded() -> None:
    """Provider rules change, so the broker's live metadata is the authority."""
    source = module_code(paper)
    for forbidden in ("0.000000001", "0.0001", "0.00001", "1e-09", "0.000026575"):
        assert forbidden not in source, forbidden
    assert "min_trade_increment" in source
    assert "min_order_size" in source


@pytest.mark.parametrize(
    ("quantity", "increment", "expected"),
    [
        ("0.1234", "0.001", "0.123"),
        ("0.999999999999", "0.000000001", "0.999999999"),
        ("1.5", "1", "1"),
        ("0.05", "0.01", "0.05"),
        ("0.059", "0.01", "0.05"),
    ],
)
def test_normalization_always_rounds_down(quantity: str, increment: str, expected: str) -> None:
    spec = CryptoAssetSpec(
        symbol=SYMBOL,
        asset_class="crypto",
        status="active",
        tradable=True,
        fractionable=True,
        min_order_size=Decimal("0.00000001"),
        min_trade_increment=Decimal(increment),
    )
    # A deliberately huge price, so every quantity here clears the broker's $10
    # minimum and this test stays about rounding direction and nothing else.
    normalized = normalize_broker_quantity(Decimal(quantity), spec, reference_price=1_000_000.0)

    assert normalized == Decimal(expected)
    assert normalized <= Decimal(quantity), "normalization must never increase a quantity"


def test_a_quantity_below_the_brokers_minimum_is_never_submitted(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """Rounding up to clear the minimum would exceed what risk approved."""
    client = FakeTradingClient(asset=make_asset(min_order_size=0.5))

    with pytest.raises(QuantityBelowMinimumError) as error:
        run_execution(connection, client, requested_quantity=Decimal("0.01"))

    assert "minimum order size" in str(error.value)
    assert client.submit_calls == []
    assert list_order_intents(connection) == []


def test_a_quantity_that_normalizes_to_nothing_is_never_submitted(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(asset=make_asset(min_trade_increment=1.0, min_order_size=1.0))

    with pytest.raises(QuantityBelowMinimumError):
        run_execution(connection, client, requested_quantity=Decimal("0.01"))

    assert client.submit_calls == []


def test_the_wire_quantity_never_exceeds_the_exact_decimal() -> None:
    """The SDK's request takes a float; the value the broker sees must not grow.

    JSON serialization emits the float's shortest round-tripping form, so that
    is what is compared against the exact approved quantity.
    """
    for text in (
        "0.1",
        "0.0001",
        "0.000000001",
        "0.123456789012345",
        "1",
        "0.999999999",
        "0.3",
    ):
        exact = Decimal(text)
        wire = to_wire_quantity(exact)
        assert Decimal(repr(wire)) <= exact, text
        assert wire > 0


# ==========================================================================
# The UTC risk day
# ==========================================================================


def test_the_daily_baseline_is_established_from_the_first_observation(
    connection: sqlite3.Connection,
) -> None:
    run_execution(connection, dry_run=True)

    [baseline] = list_daily_risk_baselines(connection)
    assert baseline.risk_date_utc == date(2025, 1, 2)
    assert baseline.baseline_equity == Decimal("200000.0")
    assert baseline.captured_at == T0


def test_a_later_observation_on_the_same_utc_day_reuses_the_baseline(
    connection: sqlite3.Connection,
) -> None:
    """A baseline that moved intraday would silently reset the daily-loss halt."""
    run_execution(connection, dry_run=True)

    later = FakeTradingClient(account=make_account(equity="150000", cash="150000"))
    result = run_execution(connection, later, dry_run=True, now=T0 + timedelta(hours=5))

    assert result.daily_baseline_equity == Decimal("200000.0")
    assert len(list_daily_risk_baselines(connection)) == 1
    # -50,000 against a 200,000 baseline is -25%, well past the 2% halt.
    assert result.risk_decision.approved is False
    assert result.risk_decision.reason_code == "DAILY_LOSS_LIMIT"


def test_a_new_utc_day_establishes_its_own_baseline(connection: sqlite3.Connection) -> None:
    run_execution(connection, dry_run=True)

    next_day = FakeTradingClient(account=make_account(equity="150000", cash="150000"))
    result = run_execution(connection, next_day, dry_run=True, now=T0 + timedelta(days=1))

    assert result.daily_baseline_equity == Decimal("150000.0")
    assert len(list_daily_risk_baselines(connection)) == 2
    assert get_daily_risk_baseline(connection, date(2025, 1, 3)) is not None
    # A fresh day starts flat, so the previous day's loss no longer halts it.
    assert result.risk_decision.approved is True


def test_the_utc_day_rolls_at_midnight_utc_not_at_a_market_close(
    connection: sqlite3.Connection,
) -> None:
    late = datetime(2025, 1, 2, 23, 45, tzinfo=UTC)
    just_after = datetime(2025, 1, 3, 0, 15, tzinfo=UTC)

    run_execution(connection, dry_run=True, now=late)
    run_execution(connection, dry_run=True, now=just_after)

    assert [row.risk_date_utc for row in list_daily_risk_baselines(connection)] == [
        date(2025, 1, 2),
        date(2025, 1, 3),
    ]


def test_the_broker_last_equity_is_never_the_daily_baseline(connection: sqlite3.Connection) -> None:
    """Alpaca's `last_equity` is an equity-session previous close.

    A 24/7 market has no such boundary. The account whose `last_equity` claims a
    catastrophic loss must still be measured against the stored UTC-day
    baseline - which here is the current equity, so nothing is halted.
    """
    client = FakeTradingClient(
        account=make_account(equity="200000", cash="200000", last_equity="1000000")
    )

    result = run_execution(connection, client, dry_run=True)

    assert result.daily_baseline_equity == Decimal("200000.0")
    assert result.risk_decision.approved is True
    assert not hasattr(result.account, "start_of_day_equity")
    assert not hasattr(result.account, "daily_pnl")


def test_the_execution_source_never_reads_last_equity() -> None:
    source = module_code(paper)
    assert "last_equity" not in source


# ==========================================================================
# Risk context
# ==========================================================================


def test_the_account_maps_onto_the_risk_context() -> None:
    account = fetch_paper_account_state(FakeTradingClient())
    context = build_risk_context(account, {}, SYMBOL, daily_baseline_equity=Decimal("200000"))

    assert context.equity == 200_000.0
    assert context.cash == 200_000.0
    assert context.total_exposure == 0.0
    assert context.symbol_exposure == 0.0
    assert context.current_position_quantity == Decimal(0)
    assert context.start_of_day_equity == 200_000.0
    assert context.daily_pnl == 0.0
    assert context.trading_enabled is True


def test_total_exposure_is_the_sum_of_long_market_values() -> None:
    positions = fetch_paper_positions(
        FakeTradingClient(
            positions=[
                make_position(SYMBOL, qty="0.05", market_value="5000"),
                make_position("ETH/USD", qty="2", market_value="7000"),
            ]
        )
    )
    context = build_risk_context(
        fetch_paper_account_state(FakeTradingClient()),
        positions,
        SYMBOL,
        daily_baseline_equity=Decimal("200000"),
    )

    assert context.total_exposure == 12_000.0
    assert context.symbol_exposure == 5_000.0


def test_symbol_exposure_and_position_come_from_that_pair() -> None:
    positions = fetch_paper_positions(
        FakeTradingClient(
            positions=[
                make_position(SYMBOL, qty="0.05", market_value="5000"),
                make_position("ETH/USD", qty="2", market_value="7000"),
            ]
        )
    )
    eth = build_risk_context(
        fetch_paper_account_state(FakeTradingClient()),
        positions,
        "ETH/USD",
        daily_baseline_equity=Decimal("200000"),
    )

    assert eth.symbol_exposure == 7_000.0
    assert eth.current_position_quantity == Decimal("2")


def test_a_broker_position_keyed_without_the_slash_is_still_matched() -> None:
    """Alpaca reports a crypto position as `BTCUSD` in some responses."""
    positions = fetch_paper_positions(
        FakeTradingClient(positions=[make_position("BTCUSD", qty="0.05", market_value="5000")])
    )
    context = build_risk_context(
        fetch_paper_account_state(FakeTradingClient()),
        positions,
        SYMBOL,
        daily_baseline_equity=Decimal("200000"),
    )

    assert context.symbol_exposure == 5_000.0
    assert context.current_position_quantity == Decimal("0.05")


def test_a_fractional_broker_position_is_read_exactly() -> None:
    positions = fetch_paper_positions(
        FakeTradingClient(
            positions=[make_position(SYMBOL, qty="0.000123456789", market_value="12")]
        )
    )

    assert positions["BTCUSD"].quantity == Decimal("0.000123456789")
    assert isinstance(positions["BTCUSD"].quantity, Decimal)


def test_a_missing_account_field_is_reported_not_guessed() -> None:
    class BrokenAccount(FakeTradingClient):
        def get_account(self) -> TradeAccount:
            return make_account(equity=None)  # type: ignore[arg-type]

    with pytest.raises(UnsupportedBrokerStateError):
        fetch_paper_account_state(BrokenAccount())


# ==========================================================================
# Account safety
# ==========================================================================


@pytest.mark.parametrize(
    "account",
    [
        pytest.param(make_account(status=AccountStatus.ACCOUNT_CLOSED), id="closed"),
        pytest.param(make_account(trading_blocked=True), id="trading-blocked"),
        pytest.param(make_account(account_blocked=True), id="account-blocked"),
        pytest.param(make_account(trade_suspended_by_user=True), id="suspended"),
    ],
)
def test_a_blocked_account_fails_closed(
    connection: sqlite3.Connection, enabled_gate: None, account: TradeAccount
) -> None:
    client = FakeTradingClient(account=account)

    with pytest.raises(AccountNotTradableError):
        run_execution(connection, client)

    assert client.submit_calls == []


def test_a_blocked_account_also_blocks_a_sell(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """A blocked account cannot be relied on to process an exit either."""
    client = FakeTradingClient(
        account=make_account(trading_blocked=True), positions=[make_position()]
    )

    with pytest.raises(AccountNotTradableError):
        run_execution(connection, client, side="SELL")

    assert client.submit_calls == []


def test_a_paper_only_account_status_is_accepted() -> None:
    account = fetch_paper_account_state(
        FakeTradingClient(account=make_account(status=AccountStatus.PAPER_ONLY))
    )
    assert account.tradable is True


def test_a_short_position_is_refused_rather_than_treated_as_long() -> None:
    client = FakeTradingClient(positions=[make_position(side=PositionSide.SHORT)])

    with pytest.raises(UnsupportedBrokerStateError) as error:
        fetch_paper_positions(client)

    assert "SHORT" in str(error.value)


# ==========================================================================
# Risk integration
# ==========================================================================


def test_a_risk_rejected_buy_never_reaches_the_broker(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(account=make_account(equity="200000", cash="0"))

    result = run_execution(connection, client)

    assert result.outcome is ExecutionOutcome.REJECTED_BY_RISK
    assert result.risk_decision.approved is False
    assert client.submit_calls == []
    assert client.preflight_calls == []
    assert list_order_intents(connection) == []


def test_an_approved_buy_submits_the_risk_approved_quantity(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient()

    result = run_execution(connection, client, requested_quantity=Decimal("0.02"))

    assert result.outcome is ExecutionOutcome.SUBMITTED
    assert result.risk_decision.reason_code == APPROVED
    [request] = client.submit_calls
    assert Decimal(repr(request.qty)) == Decimal("0.02")


def test_a_sell_uses_the_risk_approved_quantity(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(positions=[make_position(qty="0.05", market_value="5000")])

    result = run_execution(connection, client, side="SELL", requested_quantity=Decimal("0.5"))

    assert result.risk_decision.approved is True
    # Clamped to the position: an exit may flatten but never cross into a short.
    assert result.risk_decision.approved_quantity == Decimal("0.05")
    [request] = client.submit_calls
    assert Decimal(repr(request.qty)) == Decimal("0.05")
    assert request.side is AlpacaOrderSide.SELL


def test_a_risk_reducing_sell_still_works_under_the_kill_switch(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """A halt must never trap an open position."""
    client = FakeTradingClient(positions=[make_position(qty="0.05", market_value="5000")])

    blocked = run_execution(connection, client, trading_enabled=False)
    assert blocked.outcome is ExecutionOutcome.REJECTED_BY_RISK
    assert blocked.risk_decision.reason_code == TRADING_DISABLED
    assert client.submit_calls == []

    allowed = run_execution(
        connection,
        client,
        side="SELL",
        requested_quantity=Decimal("0.05"),
        trading_enabled=False,
    )
    assert allowed.outcome is ExecutionOutcome.SUBMITTED
    assert len(client.submit_calls) == 1


def test_a_sell_while_flat_is_rejected(connection: sqlite3.Connection, enabled_gate: None) -> None:
    client = FakeTradingClient()

    result = run_execution(connection, client, side="SELL")

    assert result.outcome is ExecutionOutcome.REJECTED_BY_RISK
    assert result.risk_decision.reason_code == "NO_POSITION_TO_EXIT"
    assert client.submit_calls == []


def test_the_risk_decision_is_persisted_for_both_outcomes(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    run_execution(connection, FakeTradingClient())
    run_execution(
        connection,
        FakeTradingClient(account=make_account(equity="200000", cash="0")),
        now=T0 + timedelta(minutes=1),
    )

    events = list_risk_events(connection)
    assert [event.decision for event in events] == ["APPROVED", "REJECTED"]
    assert all(event.symbol == SYMBOL for event in events)


def test_the_risk_engine_is_not_modified_to_persist_itself() -> None:
    from autotrader.risk import engine as risk_engine

    source = code_without_prose(Path(risk_engine.__file__).read_text())
    for forbidden in ("sqlite3", "record_risk_event", "autotrader.state"):
        assert forbidden not in source, forbidden


# ==========================================================================
# client_order_id
# ==========================================================================


def test_the_client_order_id_is_created_once_and_stored_before_the_broker_call(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient()

    result = run_execution(connection, client)

    assert result.intent is not None
    key = result.intent.client_order_id
    [stored] = list_order_intents(connection)
    assert stored.client_order_id == key
    assert client.preflight_calls == [key]
    [request] = client.submit_calls
    assert request.client_order_id == key


def test_each_execution_gets_its_own_client_order_id(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    first = run_execution(connection, FakeTradingClient())
    second = run_execution(connection, FakeTradingClient(), now=T0 + timedelta(minutes=1))

    assert first.intent is not None and second.intent is not None
    assert first.intent.client_order_id != second.intent.client_order_id


def test_an_intent_cannot_be_built_with_more_than_risk_approved() -> None:
    with pytest.raises(ExecutionInputError):
        OrderIntent(
            symbol=SYMBOL,
            side=OrderSide.BUY,
            requested_quantity=Decimal("0.01"),
            approved_quantity=Decimal("0.02"),
            reference_price=REFERENCE_PRICE,
            risk_reason_code=APPROVED,
            created_at=T0,
        )


def test_an_intent_requires_an_aware_timestamp() -> None:
    with pytest.raises(ExecutionInputError):
        OrderIntent(
            symbol=SYMBOL,
            side=OrderSide.BUY,
            requested_quantity=Decimal("0.01"),
            approved_quantity=Decimal("0.01"),
            reference_price=REFERENCE_PRICE,
            risk_reason_code=APPROVED,
            created_at=datetime(2025, 1, 2, 14, 30),
        )


def test_an_intent_holds_exact_decimal_quantities() -> None:
    intent = OrderIntent(
        symbol=SYMBOL,
        side=OrderSide.BUY,
        requested_quantity=Decimal("0.5"),
        approved_quantity=Decimal("0.000123456789012345"),
        reference_price=REFERENCE_PRICE,
        risk_reason_code=POSITION_LIMIT,
        created_at=T0,
    )

    assert isinstance(intent.approved_quantity, Decimal)
    assert intent.approved_quantity == Decimal("0.000123456789012345")


# ==========================================================================
# Duplicate preflight
# ==========================================================================


def test_a_clear_not_found_preflight_proceeds_to_submit(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(preflight=api_error(404, "not found"))

    result = run_execution(connection, client)

    assert result.outcome is ExecutionOutcome.SUBMITTED
    assert len(client.submit_calls) == 1


def test_an_existing_broker_order_prevents_a_second_submission(
    connection: sqlite3.Connection, enabled_gate: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "autotrader-fixed-key-for-duplicate"
    monkeypatch.setattr(execution_models, "new_client_order_id", lambda: key)
    monkeypatch.setattr(paper, "OrderIntent", execution_models.OrderIntent)
    existing = make_order(client_order_id=key, qty="0.01", status=OrderStatus.NEW)
    client = FakeTradingClient(preflight=existing)

    result = run_execution(connection, client)

    assert result.outcome is ExecutionOutcome.DUPLICATE
    assert client.submit_calls == [], "nothing may be submitted when one already exists"
    [stored] = list_broker_orders(connection)
    assert stored.broker_order_id == str(existing.id)
    assert stored.quantity == Decimal("0.01")


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(api_error(500, "server error"), id="5xx"),
        pytest.param(api_error(None, "unreadable"), id="unreadable-status"),
        pytest.param(TimeoutError("timed out"), id="timeout"),
    ],
)
def test_an_ambiguous_preflight_failure_fails_closed(
    connection: sqlite3.Connection, enabled_gate: None, failure: Exception
) -> None:
    """ "Could not check" is never "there is no duplicate"."""
    client = FakeTradingClient(preflight=failure)

    with pytest.raises(DuplicatePreflightUnavailableError):
        run_execution(connection, client)

    assert client.submit_calls == []


def test_the_preflight_uses_the_persisted_client_order_id(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient()

    result = run_execution(connection, client)

    assert result.intent is not None
    [asked] = client.preflight_calls
    stored = get_order_intent_by_client_id(connection, asked)
    assert stored is not None
    assert stored.client_order_id == result.intent.client_order_id


def test_a_preflight_not_found_returns_none() -> None:
    assert (
        find_broker_order_by_client_id(
            FakeTradingClient(preflight=api_error(404)), "autotrader-absent"
        )
        is None
    )


# ==========================================================================
# Submission outcomes
# ==========================================================================


def test_a_successful_submission_persists_the_broker_snapshot(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(submit_order_id=BROKER_ORDER_UUID)

    result = run_execution(connection, client)

    assert result.outcome is ExecutionOutcome.SUBMITTED
    assert result.broker_order is not None
    assert result.broker_order.broker_order_id == BROKER_ORDER_UUID

    [intent] = list_order_intents(connection)
    assert intent.status == INTENT_STATUS_SUBMITTED
    stored = get_broker_order_by_intent(connection, intent.id)
    assert stored is not None
    assert stored.broker_order_id == BROKER_ORDER_UUID
    assert stored.client_order_id == intent.client_order_id
    assert stored.symbol == SYMBOL
    assert stored.side == "BUY"
    assert stored.status == "accepted"
    assert stored.submitted_at == T0
    assert stored.quantity == Decimal("0.01")
    assert stored.filled_quantity == 0


def test_the_broker_status_is_stored_verbatim(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(submit_status=OrderStatus.PENDING_NEW)

    run_execution(connection, client)

    [stored] = list_broker_orders(connection)
    assert stored.status == OrderStatus.PENDING_NEW.value


def test_a_definite_broker_rejection_marks_the_intent_rejected(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(submit=api_error(422, "insufficient balance"))

    with pytest.raises(BrokerRejectedOrderError) as error:
        run_execution(connection, client)

    assert "insufficient balance" in str(error.value)
    [intent] = list_order_intents(connection)
    assert intent.status == INTENT_STATUS_REJECTED
    assert get_broker_order_by_intent(connection, intent.id) is None
    assert len(client.submit_calls) == 1


def test_an_unknown_intent_keeps_its_client_order_id_for_recovery(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(submit=TimeoutError("no answer"))

    with pytest.raises(AmbiguousSubmissionError):
        run_execution(connection, client)

    [intent] = list_order_intents(connection)
    assert intent.status == INTENT_STATUS_UNKNOWN
    assert get_order_intent_by_client_id(connection, intent.client_order_id) is not None


def test_the_source_implements_no_submission_retry_or_backoff() -> None:
    source = module_code(paper)
    for forbidden in ("time.sleep", "backoff", "for attempt", "while attempt", "max_retries"):
        assert forbidden not in source, forbidden


# ==========================================================================
# Accepted is not filled
# ==========================================================================


def test_a_successful_submission_does_not_fabricate_a_position(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(positions=[make_position(qty="0.05", market_value="5000")])

    run_execution(connection, client)

    stored = get_position(connection, SYMBOL)
    assert stored is not None
    # The observed broker position, unchanged by the order just accepted.
    assert stored.quantity == Decimal("0.05")


def test_a_flat_symbol_stays_flat_after_an_accepted_order(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    run_execution(connection, FakeTradingClient())

    stored = get_position(connection, SYMBOL)
    assert stored is not None
    assert stored.quantity == 0


def test_an_accepted_order_snapshot_records_no_fill(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    run_execution(connection, FakeTradingClient())

    [stored] = list_broker_orders(connection)
    assert stored.filled_quantity == 0
    assert stored.filled_average_price is None
    assert stored.filled_at is None


def test_the_result_does_not_claim_submission_for_an_unknown_outcome(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(submit=TimeoutError("no answer"))

    with pytest.raises(AmbiguousSubmissionError):
        run_execution(connection, client)

    assert ExecutionOutcome.UNKNOWN.value == "UNKNOWN"


# ==========================================================================
# The broker request: MARKET, GTC, fractional
# ==========================================================================


def intent_for(quantity: str = "0.01", side: OrderSide = OrderSide.BUY) -> OrderIntent:
    return OrderIntent(
        symbol=SYMBOL,
        side=side,
        requested_quantity=Decimal(quantity),
        approved_quantity=Decimal(quantity),
        reference_price=REFERENCE_PRICE,
        risk_reason_code=APPROVED,
        created_at=T0,
    )


def test_the_request_is_a_crypto_market_gtc_order() -> None:
    request = build_market_order_request(intent_for())

    assert isinstance(request, MarketOrderRequest)
    assert request.symbol == SYMBOL
    assert request.type is OrderType.MARKET
    assert request.time_in_force is TimeInForce.GTC
    assert ORDER_TIME_IN_FORCE is TimeInForce.GTC
    assert Decimal(repr(request.qty)) == Decimal("0.01")


def test_day_time_in_force_is_never_used() -> None:
    """DAY expires at a session close that a 24/7 market does not have."""
    request = build_market_order_request(intent_for())
    assert request.time_in_force is not TimeInForce.DAY

    source = module_code(paper)
    for forbidden in ("TimeInForce.DAY", "TimeInForce.IOC", "TimeInForce.FOK", "TimeInForce.OPG"):
        assert forbidden not in source, forbidden
    assert "TimeInForce.GTC" in source


def test_the_request_carries_the_client_order_id() -> None:
    intent = intent_for()
    request = build_market_order_request(intent)

    assert request.client_order_id == intent.client_order_id


def test_the_request_is_never_notional() -> None:
    """A notional order would be sized in dollars by the broker, not by risk."""
    assert build_market_order_request(intent_for()).notional is None
    assert "notional=" not in module_code(paper)


def test_a_sell_request_maps_to_the_sell_side() -> None:
    request = build_market_order_request(intent_for(side=OrderSide.SELL))
    assert request.side is AlpacaOrderSide.SELL


def test_no_advanced_order_type_is_constructible() -> None:
    source = module_code(paper)
    for forbidden in (
        "LimitOrderRequest",
        "StopOrderRequest",
        "StopLimitOrderRequest",
        "TrailingStopOrderRequest",
        "OrderClass.BRACKET",
        "OrderClass.OCO",
        "take_profit",
        "stop_loss",
    ):
        assert forbidden not in source, forbidden


def test_no_streaming_client_is_used() -> None:
    source = module_code(paper)
    for forbidden in ("TradingStream", "websocket", "StockDataStream", "CryptoDataStream"):
        assert forbidden not in source, forbidden


# ==========================================================================
# No equity market clock
# ==========================================================================


def test_the_execution_path_never_calls_the_market_clock() -> None:
    """Crypto trades continuously; there is no session to gate on."""
    source = module_code(paper)
    for forbidden in ("get_clock", "is_open", "Clock", "next_open", "next_close", "market_open"):
        assert forbidden not in source, forbidden


def test_the_result_carries_no_market_clock(connection: sqlite3.Connection) -> None:
    result = run_execution(connection, dry_run=True)

    assert not hasattr(result, "clock")
    assert not hasattr(paper, "MarketClock")
    assert not hasattr(paper, "fetch_market_clock")


def test_a_client_without_a_clock_method_works_end_to_end(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """`FakeTradingClient` has no `get_clock`; reaching for one would raise."""
    client = FakeTradingClient()
    assert not hasattr(client, "get_clock")

    result = run_execution(connection, client)

    assert result.outcome is ExecutionOutcome.SUBMITTED


def test_the_cli_reports_no_market_state(
    connection: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paper, "create_paper_trading_client", FakeTradingClient)
    monkeypatch.setattr(paper, "create_market_data_client", FakeDataClient)

    result = runner.invoke(
        app,
        [
            "paper-submit",
            "--symbol",
            SYMBOL,
            "--side",
            "BUY",
            "--qty",
            "0.01",
            "--dry-run",
            "--db",
            str(tmp_path / "cli.db"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Market:" not in result.output
    assert "OPEN" not in result.output
    assert "CLOSED" not in result.output
    assert "CRYPTO SPOT, 24/7" in result.output


# ==========================================================================
# Dry run
# ==========================================================================


def test_a_dry_run_never_calls_submit_order(connection: sqlite3.Connection) -> None:
    client = FakeTradingClient()

    result = run_execution(connection, client, dry_run=True)

    assert result.outcome is ExecutionOutcome.DRY_RUN
    assert client.submit_calls == []
    assert client.preflight_calls == []


def test_a_dry_run_persists_no_order_intent(connection: sqlite3.Connection) -> None:
    run_execution(connection, dry_run=True)

    assert list_order_intents(connection) == []
    assert list_broker_orders(connection) == []


def test_a_dry_run_still_evaluates_and_records_risk(connection: sqlite3.Connection) -> None:
    result = run_execution(connection, dry_run=True)

    assert result.risk_decision.approved is True
    assert len(list_risk_events(connection)) == 1


def test_a_dry_run_shows_the_quantity_that_would_be_sent(
    connection: sqlite3.Connection,
) -> None:
    result = run_execution(
        connection,
        FakeTradingClient(asset=make_asset(min_trade_increment=0.001)),
        requested_quantity=Decimal("0.012345"),
        dry_run=True,
    )

    assert result.intent is not None
    assert result.intent.approved_quantity == Decimal("0.012")
    assert result.submitted_quantity == Decimal("0.012")


def test_a_dry_run_works_without_the_environment_gate(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(PAPER_TRADING_ENABLED_ENV, raising=False)

    assert run_execution(connection, dry_run=True).outcome is ExecutionOutcome.DRY_RUN


# ==========================================================================
# CLI
# ==========================================================================


@pytest.fixture
def patched_broker(monkeypatch: pytest.MonkeyPatch) -> FakeTradingClient:
    client = FakeTradingClient()
    monkeypatch.setattr(paper, "create_paper_trading_client", lambda: client)
    monkeypatch.setattr(paper, "create_market_data_client", lambda: FakeDataClient())
    return client


def cli_database(tmp_path: Path) -> Path:
    """The database the CLI cases below submit against, already reconciled.

    `paper-submit` initializes its own database, and a database nothing has
    reconciled is one the execution boundary refuses to submit from. An
    operator gets there by running `reconcile` first; a test gets there by
    establishing the same starting state.
    """
    path = initialize_database(tmp_path / "cli.db")
    with connect(path) as setup:
        establish_account_safety(setup)
    return path


def cli_args(tmp_path: Path, *extra: str, qty: str = "0.01") -> list[str]:
    return [
        "paper-submit",
        "--symbol",
        SYMBOL,
        "--side",
        "BUY",
        "--qty",
        qty,
        "--db",
        str(cli_database(tmp_path)),
        *extra,
    ]


def test_the_cli_submits_with_both_gates_open(
    tmp_path: Path, enabled_gate: None, patched_broker: FakeTradingClient
) -> None:
    result = runner.invoke(app, cli_args(tmp_path, "--confirm-paper", "PAPER"))

    assert result.exit_code == 0, result.output
    assert "SUBMITTED TO PAPER ACCOUNT" in result.output
    assert len(patched_broker.submit_calls) == 1


def test_the_cli_never_submits_with_a_wrong_confirmation(
    tmp_path: Path, enabled_gate: None, patched_broker: FakeTradingClient
) -> None:
    result = runner.invoke(app, cli_args(tmp_path, "--confirm-paper", "paper"))

    assert result.exit_code == 1
    assert patched_broker.submit_calls == []


def test_the_cli_never_submits_without_a_confirmation(
    tmp_path: Path, enabled_gate: None, patched_broker: FakeTradingClient
) -> None:
    result = runner.invoke(app, cli_args(tmp_path))

    assert result.exit_code == 1
    assert patched_broker.submit_calls == []


def test_the_cli_never_submits_with_the_gate_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_broker: FakeTradingClient
) -> None:
    monkeypatch.delenv(PAPER_TRADING_ENABLED_ENV, raising=False)

    result = runner.invoke(app, cli_args(tmp_path, "--confirm-paper", "PAPER"))

    assert result.exit_code == 1
    assert PAPER_TRADING_ENABLED_ENV in result.output
    assert patched_broker.submit_calls == []


def test_the_cli_dry_run_needs_neither_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_broker: FakeTradingClient
) -> None:
    monkeypatch.delenv(PAPER_TRADING_ENABLED_ENV, raising=False)

    result = runner.invoke(app, cli_args(tmp_path, "--dry-run"))

    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert patched_broker.submit_calls == []


def test_the_cli_accepts_a_fractional_quantity(
    tmp_path: Path, enabled_gate: None, patched_broker: FakeTradingClient
) -> None:
    result = runner.invoke(app, cli_args(tmp_path, "--confirm-paper", "PAPER", qty="0.00012345"))

    assert result.exit_code == 0, result.output
    [request] = patched_broker.submit_calls
    assert Decimal(repr(request.qty)) == Decimal("0.00012345")
    assert "Requested Qty:         0.00012345" in result.output


def test_the_cli_rejects_an_unparsable_quantity(
    tmp_path: Path, enabled_gate: None, patched_broker: FakeTradingClient
) -> None:
    result = runner.invoke(app, cli_args(tmp_path, "--dry-run", qty="not-a-number"))

    assert result.exit_code == 1
    assert "--qty" in result.output
    assert patched_broker.submit_calls == []


def test_the_cli_reports_a_risk_rejection_without_a_traceback(
    tmp_path: Path, enabled_gate: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeTradingClient(account=make_account(equity="200000", cash="0"))
    monkeypatch.setattr(paper, "create_paper_trading_client", lambda: client)
    monkeypatch.setattr(paper, "create_market_data_client", lambda: FakeDataClient())

    result = runner.invoke(app, cli_args(tmp_path, "--confirm-paper", "PAPER"))

    assert result.exit_code == 1
    assert "REJECTED BY RISK ENGINE" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert client.submit_calls == []


def test_the_cli_uses_a_distinct_exit_code_for_an_unknown_outcome(
    tmp_path: Path, enabled_gate: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 2 means an order may exist at the broker. A script must be able to tell."""
    client = FakeTradingClient(submit=TimeoutError("no answer"))
    monkeypatch.setattr(paper, "create_paper_trading_client", lambda: client)
    monkeypatch.setattr(paper, "create_market_data_client", lambda: FakeDataClient())

    result = runner.invoke(app, cli_args(tmp_path, "--confirm-paper", "PAPER"))

    assert result.exit_code == 2
    assert "unknown" in result.output.lower()


def test_the_cli_reports_an_unsupported_symbol_cleanly(
    tmp_path: Path, enabled_gate: None, patched_broker: FakeTradingClient
) -> None:
    result = runner.invoke(
        app,
        [
            "paper-submit",
            "--symbol",
            "SPY",
            "--side",
            "BUY",
            "--qty",
            "1",
            "--dry-run",
            "--db",
            str(tmp_path / "cli.db"),
        ],
    )

    assert result.exit_code == 1
    assert "Unsupported symbol" in result.output
    assert "BTC/USD" in result.output


def test_the_cli_preview_never_prints_a_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_broker: FakeTradingClient
) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "SECRET-KEY-VALUE")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SECRET-SECRET-VALUE")

    result = runner.invoke(app, cli_args(tmp_path, "--dry-run"))

    assert result.exit_code == 0, result.output
    assert "SECRET-KEY-VALUE" not in result.output
    assert "SECRET-SECRET-VALUE" not in result.output
    assert "Authorization" not in result.output


def test_the_cli_shows_the_asset_constraints_and_the_broker_quantity(
    tmp_path: Path, patched_broker: FakeTradingClient
) -> None:
    result = runner.invoke(app, cli_args(tmp_path, "--dry-run"))

    assert result.exit_code == 0, result.output
    assert "Asset Min Order:" in result.output
    assert "Asset Increment:" in result.output
    assert "Broker Qty:" in result.output
    assert "UTC Day Baseline:" in result.output


# ==========================================================================
# Offline guarantees and layering
# ==========================================================================


def test_the_tests_need_no_real_credentials(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    assert run_execution(connection, dry_run=True).outcome is ExecutionOutcome.DRY_RUN


def test_execution_makes_no_network_access(
    connection: sqlite3.Connection, enabled_gate: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("the execution tests must not open a socket")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

    assert run_execution(connection, FakeTradingClient()).outcome is ExecutionOutcome.SUBMITTED


def test_the_domain_models_import_no_broker_sdk() -> None:
    tree = ast.parse(Path(execution_models.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    assert imported == {"__future__", "dataclasses", "datetime", "decimal", "enum", "math", "uuid"}


def test_the_domain_layer_exposes_no_alpaca_model() -> None:
    source = module_code(execution_models)
    for forbidden in ("alpaca", "MarketOrderRequest", "TimeInForce", "TradingClient"):
        assert forbidden not in source, forbidden


def test_the_state_layer_still_knows_nothing_about_a_broker_client() -> None:
    from autotrader.state import sqlite as state_sqlite

    source = code_without_prose(Path(state_sqlite.__file__).read_text())
    for forbidden in ("alpaca", "TradingClient", "submit_order"):
        assert forbidden not in source, forbidden
