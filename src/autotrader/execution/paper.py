"""Phase 7: the Alpaca **paper** execution boundary. The only file that trades.

This is the one module in the repository that constructs a broker trading
client or submits an order. Everything about live trading being impossible is
checkable by reading it.

**Paper only, structurally.** `create_paper_trading_client` hardcodes
``paper=True`` and takes no parameter that could change it. There is no live
client factory, no `paper` argument on any public function, no `--live` flag,
and no environment variable that selects an environment. Live trading here is
not "disabled by default"; it is **unexpressible**. A source-level test asserts
that ``paper=False`` appears nowhere in the package.

**Two independent gates.** Reading the account, the clock, a position, or a
price needs neither. *Submitting* needs both:

1. the environment gate ``AUTOTRADER_PAPER_TRADING_ENABLED`` set to exactly
   ``true`` - missing, empty, `false`, `TRUE`, `1`, or anything else leaves
   submission off; and
2. an explicit confirmation token typed at the CLI.

Both default to closed, and neither can satisfy the other.

**Everything fails closed.** No current price, an account that cannot trade, a
duplicate check that could not complete - each stops the attempt rather than
proceeding on an assumption. "I could not check for a duplicate" is never
treated as "there is no duplicate".

**The risk engine is not optional.** Every submission is sized by
`evaluate_risk`, and the quantity sent to the broker is
`RiskDecision.approved_quantity` - never the caller's requested quantity. A
rejected decision means no broker request is even constructed.

**Order of operations is a safety property, not a style choice.** The intent
and its `client_order_id` are committed to SQLite *before* the broker is
called, so that a crash between the request and its response still leaves a
durable anchor. Submitting first and recording afterwards would create exactly
the orphaned-order situation that reconciliation exists to prevent.

**An ambiguous outcome is never retried.** If a submission attempt ends in a
timeout, a reset connection, or any other failure that cannot distinguish
"never arrived" from "accepted", the intent is marked `UNKNOWN`, an audit event
is written, and the attempt **stops**. It is not re-sent, and its
`client_order_id` is not regenerated. Phase 8 resolves it by asking the broker
about that exact key. Guessing wrong here duplicates a real order.

**Accepted is not filled.** A stored broker snapshot proves the broker accepted
an order. Nothing in this module infers a position from that: the local
positions table is only ever written from a position actually *observed* at the
broker.

Scope: US equities, the five V0.1 symbols, whole shares, MARKET orders, DAY
time in force, regular hours, long only. No fractional or notional orders, no
limit/stop/bracket/OCO orders, no options, no crypto, no shorts, no streaming,
and no reconciliation - see docs/SPEC.md section 8, "Phase 7".
"""

from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AccountStatus,
    PositionSide,
    TimeInForce,
)
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.models import Order, TradeAccount
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.requests import MarketOrderRequest

from autotrader.execution.models import (
    ExecutionError,
    ExecutionInputError,
    OrderIntent,
    OrderSide,
    normalize_side,
    normalize_symbol,
    require_reference_price,
    require_whole_share_quantity,
)
from autotrader.risk import (
    RiskContext,
    RiskDecision,
    RiskRequest,
    RiskSide,
    evaluate_risk,
)
from autotrader.state import sqlite as state

#: The environment gate on broker submission. Read-only calls ignore it.
PAPER_TRADING_ENABLED_ENV = "AUTOTRADER_PAPER_TRADING_ENABLED"

#: The **only** value that opens the gate. Compared exactly, after stripping
#: surrounding whitespace: `TRUE`, `True`, `1`, `yes`, and `on` all leave
#: submission disabled. A single canonical spelling means an operator can never
#: be unsure whether the gate is open, and a typo always fails closed.
PAPER_TRADING_ENABLED_VALUE = "true"

#: The token `paper-submit` requires. Compared exactly.
CONFIRMATION_TOKEN = "PAPER"

_API_KEY_ENV = "ALPACA_API_KEY"
_SECRET_KEY_ENV = "ALPACA_SECRET_KEY"

#: Market-data feed for the reference price. IEX matches Phase 1's historical
#: feed, so sizing and research see the same source.
REFERENCE_PRICE_FEED = DataFeed.IEX

#: Account states in which this system will submit a paper order. `PAPER_ONLY`
#: is accepted because it describes exactly this environment.
TRADABLE_ACCOUNT_STATUSES = (AccountStatus.ACTIVE, AccountStatus.PAPER_ONLY)

#: HTTP statuses that leave a submission genuinely ambiguous. A timeout or a
#: rate-limit response may or may not have reached the matching engine, so
#: neither may be read as "the order was refused".
_AMBIGUOUS_HTTP_STATUSES = frozenset({408, 429})

#: Audit event types written to `system_events`.
EVENT_SUBMITTED = "PAPER_ORDER_SUBMITTED"
EVENT_REJECTED = "PAPER_ORDER_REJECTED"
EVENT_UNKNOWN = "PAPER_ORDER_UNKNOWN"
EVENT_DUPLICATE = "PAPER_ORDER_DUPLICATE"


class PaperTradingDisabledError(ExecutionError):
    """The environment gate is closed, so nothing may be submitted."""


class MissingCredentialsError(ExecutionError):
    """Alpaca credentials are not configured.

    Raised before any broker call. The message names the *variables*, never
    their values.
    """


class ConfirmationRequiredError(ExecutionError):
    """The explicit paper-submission confirmation was absent or wrong."""


class AccountNotTradableError(ExecutionError):
    """The paper account is not in a state that may place orders."""


class UnsupportedBrokerStateError(ExecutionError):
    """The broker reports something this long-only phase cannot reason about."""


class ReferencePriceUnavailableError(ExecutionError):
    """No usable current price, so nothing may be sized or submitted."""


class DuplicatePreflightUnavailableError(ExecutionError):
    """The duplicate check could not complete, so submission must not proceed.

    Deliberately distinct from "no duplicate exists". Treating a failed check
    as a clean result is how duplicate orders get placed.
    """


class BrokerRejectedOrderError(ExecutionError):
    """The broker refused the order outright. No order exists."""


class AmbiguousSubmissionError(ExecutionError):
    """A submission attempt ended without a knowable outcome.

    The order may or may not exist at the broker. It is **not** retried and
    the `client_order_id` is **not** regenerated; Phase 8 resolves it.
    """


class ExecutionOutcome(Enum):
    """How one execution attempt ended."""

    DRY_RUN = "DRY_RUN"
    REJECTED_BY_RISK = "REJECTED_BY_RISK"
    SUBMITTED = "SUBMITTED"
    DUPLICATE = "DUPLICATE"
    REJECTED_BY_BROKER = "REJECTED_BY_BROKER"
    UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------------------
# Gates and credentials
# --------------------------------------------------------------------------


def paper_trading_enabled() -> bool:
    """Report whether the environment gate is open.

    Closed unless the variable is exactly `PAPER_TRADING_ENABLED_VALUE`. An
    unset, empty, or unrecognized value is not an error here - it is simply a
    closed gate, which is the safe reading of an ambiguous configuration.
    """
    return os.environ.get(PAPER_TRADING_ENABLED_ENV, "").strip() == PAPER_TRADING_ENABLED_VALUE


def require_paper_trading_enabled() -> None:
    """Raise unless the environment gate is open."""
    if not paper_trading_enabled():
        raise PaperTradingDisabledError(
            f"Paper order submission is disabled. Set {PAPER_TRADING_ENABLED_ENV}="
            f"{PAPER_TRADING_ENABLED_VALUE} (exactly that value) to enable it. "
            "This gate only ever enables PAPER trading; there is no live mode."
        )


def require_confirmation(token: str | None) -> None:
    """Raise unless `token` is exactly the confirmation token."""
    if token != CONFIRMATION_TOKEN:
        raise ConfirmationRequiredError(
            f"Paper submission requires --confirm-paper {CONFIRMATION_TOKEN} exactly. "
            "Nothing was submitted."
        )


def credentials_configured() -> bool:
    """Report whether both credential environment variables hold a value."""
    return bool(os.environ.get(_API_KEY_ENV, "").strip()) and bool(
        os.environ.get(_SECRET_KEY_ENV, "").strip()
    )


def _require_credentials() -> tuple[str, str]:
    """Return the configured credentials, or raise naming only the variables.

    The values are returned for immediate use by a client constructor and are
    never logged, persisted, embedded in a `client_order_id`, or included in
    an exception message.
    """
    if not credentials_configured():
        raise MissingCredentialsError(
            f"Alpaca credentials are not configured. Set {_API_KEY_ENV} and {_SECRET_KEY_ENV}."
        )
    return os.environ[_API_KEY_ENV].strip(), os.environ[_SECRET_KEY_ENV].strip()


# --------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------


def create_paper_trading_client() -> TradingClient:
    """Build the Alpaca **paper** trading client. There is no other kind.

    ``paper=True`` is written literally below and is not derived from a
    parameter, a setting, or the environment. This function takes no argument
    that could change it, and no other function in this package constructs a
    `TradingClient`, so there is exactly one line in the repository that
    decides which Alpaca environment is reachable - and it always says paper.

    The SDK's own request retry is switched off for this client. It retries
    `429` and `504` responses internally, which is harmless for a `GET` but
    unacceptable for `POST /orders`: a gateway timeout there is precisely the
    ambiguous case this phase must classify as `UNKNOWN`, and a silent
    resubmission would defeat that. The constructor cannot express "no
    retries" (it ignores a zero), so the attribute is set directly; a test
    asserts it stays effective.
    """
    api_key, secret_key = _require_credentials()
    client = TradingClient(
        api_key=api_key,
        secret_key=secret_key,
        paper=True,
    )
    client._retry = 0
    return client


def create_market_data_client() -> StockHistoricalDataClient:
    """Build the market-data client used for the current reference price.

    Reuses the Phase 1 factory rather than duplicating credential handling.
    Market data is read-only and carries no trading permission.
    """
    from autotrader.data.historical import HistoricalDataError, create_client

    try:
        return create_client()
    except HistoricalDataError as error:
        raise MissingCredentialsError(str(error)) from None


# --------------------------------------------------------------------------
# Normalized broker reads
#
# Alpaca returns money and quantities as strings. They are converted once,
# here, so no downstream arithmetic is done on text.
# --------------------------------------------------------------------------


def _to_float(value: object, field_name: str) -> float:
    """Convert a broker numeric field to a finite float, or raise."""
    if value is None:
        raise UnsupportedBrokerStateError(
            f"The broker did not report {field_name}, which is required to size an "
            "order safely. Refusing to guess it."
        )
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise UnsupportedBrokerStateError(
            f"The broker reported a non-numeric {field_name}."
        ) from None
    if not math.isfinite(number):
        raise UnsupportedBrokerStateError(f"The broker reported a non-finite {field_name}.")
    return number


def _optional_float(value: object) -> float | None:
    """Convert an optional broker numeric field, treating 0 and blank as absent."""
    if value is None or value == "":
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _to_whole_shares(value: object, field_name: str) -> int:
    """Convert a broker quantity to whole shares, refusing a fractional one."""
    number = _to_float(value, field_name)
    if number != int(number):
        raise UnsupportedBrokerStateError(
            f"The broker reported a fractional {field_name} ({number}); fractional "
            "shares are out of scope for this phase."
        )
    return int(number)


@dataclass(frozen=True)
class PaperAccountState:
    """The paper account, normalized to the numbers risk actually needs.

    `start_of_day_equity` is Alpaca's `last_equity`: the account's equity at
    the previous trading day's close, which is the baseline Alpaca itself uses
    for a day's P&L. `daily_pnl` is derived from it as `equity - last_equity`
    rather than read from a separate field, so the two can never disagree.
    """

    equity: float
    cash: float
    start_of_day_equity: float
    daily_pnl: float
    status: str
    trading_blocked: bool
    account_blocked: bool
    trade_suspended_by_user: bool

    @property
    def tradable(self) -> bool:
        """Whether this account may place an order at all."""
        return (
            self.status in {member.value for member in TRADABLE_ACCOUNT_STATUSES}
            and not self.trading_blocked
            and not self.account_blocked
            and not self.trade_suspended_by_user
        )


@dataclass(frozen=True)
class PaperPosition:
    """One long position, normalized. Shorts are rejected before this exists."""

    symbol: str
    quantity: int
    market_value: float
    average_entry_price: float | None


@dataclass(frozen=True)
class MarketClock:
    """The broker's market clock. Informational only.

    A closed market does **not** block a DAY market order: Alpaca queues it for
    the next session. Nothing here infers a fill from an open market either.
    """

    is_open: bool
    timestamp: datetime


@dataclass(frozen=True)
class BrokerOrderSnapshot:
    """What the broker said about an order, normalized.

    `status` is the broker's own vocabulary, kept as opaque text. A snapshot
    means the order was **accepted**; it never means it filled.
    """

    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: int
    filled_quantity: int
    filled_average_price: float | None
    status: str
    submitted_at: datetime | None
    filled_at: datetime | None


def fetch_paper_account_state(client: TradingClient) -> PaperAccountState:
    """Read the current paper account and normalize it."""
    account = client.get_account()
    if not isinstance(account, TradeAccount):
        raise UnsupportedBrokerStateError(
            "The broker returned an account in an unexpected shape; refusing to size "
            "an order against it."
        )

    equity = _to_float(account.equity, "account equity")
    cash = _to_float(account.cash, "account cash")
    start_of_day_equity = _to_float(account.last_equity, "account last_equity")
    if start_of_day_equity <= 0 or equity <= 0:
        raise UnsupportedBrokerStateError(
            "The broker reported a non-positive account equity, which cannot describe "
            "a usable account. Refusing to size an order against it."
        )

    status = account.status.value if isinstance(account.status, Enum) else str(account.status)
    return PaperAccountState(
        equity=equity,
        cash=cash,
        start_of_day_equity=start_of_day_equity,
        daily_pnl=equity - start_of_day_equity,
        status=status,
        trading_blocked=bool(account.trading_blocked),
        account_blocked=bool(account.account_blocked),
        trade_suspended_by_user=bool(account.trade_suspended_by_user),
    )


def require_tradable_account(account: PaperAccountState) -> None:
    """Raise unless the paper account may place orders.

    Fails closed for both sides. A blocked account cannot be relied upon to
    process an exit correctly either, so this is checked before any submission
    rather than only before a BUY.
    """
    if not account.tradable:
        raise AccountNotTradableError(
            f"The paper account cannot trade (status={account.status}, "
            f"trading_blocked={account.trading_blocked}, "
            f"account_blocked={account.account_blocked}, "
            f"trade_suspended_by_user={account.trade_suspended_by_user}). "
            "Nothing was submitted."
        )


def fetch_paper_positions(client: TradingClient) -> dict[str, PaperPosition]:
    """Read every open paper position, keyed by symbol.

    A **short** position raises rather than being coerced into a long. This
    system cannot reason about one: it would make a SELL an increase in risk
    rather than a reduction, inverting the rule that exits are never blocked.
    """
    positions = client.get_all_positions()
    normalized: dict[str, PaperPosition] = {}
    for position in positions:
        if not isinstance(position, AlpacaPosition):
            raise UnsupportedBrokerStateError(
                "The broker returned a position in an unexpected shape."
            )
        if position.side is PositionSide.SHORT:
            raise UnsupportedBrokerStateError(
                f"The paper account holds a SHORT position in {position.symbol}. This "
                "system is long only and will not trade around a short. Nothing was "
                "submitted."
            )
        symbol = str(position.symbol)
        normalized[symbol] = PaperPosition(
            symbol=symbol,
            quantity=_to_whole_shares(position.qty, f"{symbol} position quantity"),
            market_value=_to_float(position.market_value, f"{symbol} position market value"),
            average_entry_price=_optional_float(position.avg_entry_price),
        )
    return normalized


def fetch_market_clock(client: TradingClient) -> MarketClock:
    """Read the broker's market clock. Never used to gate a submission."""
    clock = client.get_clock()
    timestamp = getattr(clock, "timestamp", None)
    return MarketClock(
        is_open=bool(clock.is_open),
        timestamp=timestamp if isinstance(timestamp, datetime) else datetime.now(UTC),
    )


def fetch_reference_price(client: StockHistoricalDataClient, symbol: str) -> float:
    """Return the latest IEX trade price for `symbol`.

    This is the *current* market price, not a stored historical bar: sizing a
    live order against yesterday's Parquet close would be wrong. A price that
    cannot be obtained, or that is not finite and positive, fails closed - no
    order is sized or submitted.
    """
    ticker = normalize_symbol(symbol)
    request = StockLatestTradeRequest(symbol_or_symbols=ticker, feed=REFERENCE_PRICE_FEED)
    try:
        latest = client.get_stock_latest_trade(request)
    except APIError as error:
        raise ReferencePriceUnavailableError(
            f"Could not get a current price for {ticker}: {_api_error_text(error)}. "
            "Nothing was submitted."
        ) from None
    except Exception as error:  # noqa: BLE001 - any failure here must fail closed
        raise ReferencePriceUnavailableError(
            f"Could not get a current price for {ticker}: {type(error).__name__}. "
            "Nothing was submitted."
        ) from None

    trade = latest.get(ticker) if hasattr(latest, "get") else None
    price = getattr(trade, "price", None)
    if price is None:
        raise ReferencePriceUnavailableError(
            f"The market-data feed returned no current trade for {ticker}. Nothing was submitted."
        )
    try:
        return require_reference_price(float(price), "reference_price")
    except (ExecutionInputError, TypeError, ValueError):
        raise ReferencePriceUnavailableError(
            f"The market-data feed returned an unusable price for {ticker}. Nothing was submitted."
        ) from None


# --------------------------------------------------------------------------
# Risk context
# --------------------------------------------------------------------------


def build_risk_context(
    account: PaperAccountState,
    positions: dict[str, PaperPosition],
    symbol: str,
    *,
    trading_enabled: bool = True,
) -> RiskContext:
    """Map current paper broker state onto Phase 5's account context.

    The mapping, field by field:

    ==========================  ====================================================
    `RiskContext`               Source
    ==========================  ====================================================
    `equity`                    `TradeAccount.equity`
    `cash`                      `TradeAccount.cash`
    `start_of_day_equity`       `TradeAccount.last_equity` (prior close)
    `daily_pnl`                 `equity - last_equity`
    `total_exposure`            sum of positive **long** position market values
    `symbol_exposure`           that symbol's long market value, else 0
    `current_position_quantity` that symbol's long share count, else 0
    `trading_enabled`           caller-supplied kill switch
    ==========================  ====================================================

    `total_exposure` is summed from the positions themselves rather than read
    from `long_market_value`, so the total and the per-symbol figure it must
    contain always come from one source and cannot disagree.

    `trading_enabled` is Phase 5's kill switch and is a parameter, not an
    environment variable: the operational off switch for this phase is the
    submission gate, and a second env-driven switch would make it ambiguous
    which one stopped a trade. Turning it off blocks new entries while still
    permitting a risk-reducing exit, which is exactly Phase 5's contract.
    """
    ticker = normalize_symbol(symbol)
    total_exposure = sum(
        position.market_value for position in positions.values() if position.market_value > 0
    )
    held = positions.get(ticker)
    symbol_exposure = max(0.0, held.market_value) if held is not None else 0.0
    return RiskContext(
        equity=account.equity,
        cash=max(0.0, account.cash),
        total_exposure=total_exposure,
        symbol_exposure=symbol_exposure,
        current_position_quantity=held.quantity if held is not None else 0,
        daily_pnl=account.daily_pnl,
        start_of_day_equity=account.start_of_day_equity,
        trading_enabled=trading_enabled,
    )


def _to_risk_side(side: OrderSide) -> RiskSide:
    """Translate an execution side into the risk engine's vocabulary."""
    return RiskSide.BUY if side is OrderSide.BUY else RiskSide.SELL


# --------------------------------------------------------------------------
# Broker request construction and error classification
# --------------------------------------------------------------------------


def build_market_order_request(intent: OrderIntent) -> MarketOrderRequest:
    """Translate an approved intent into the Alpaca request to send.

    Always a MARKET order, DAY time in force, regular hours, whole shares, and
    carrying the intent's `client_order_id`. `notional` is never set - a
    notional order would be sized in dollars by the broker rather than by the
    risk engine, which would put sizing outside this system's control.

    The quantity is `approved_quantity`, the risk engine's number. The
    requested quantity is deliberately not reachable from here.
    """
    return MarketOrderRequest(
        symbol=intent.symbol,
        qty=intent.approved_quantity,
        side=AlpacaOrderSide.BUY if intent.side is OrderSide.BUY else AlpacaOrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        client_order_id=intent.client_order_id,
        extended_hours=False,
    )


def _api_error_text(error: APIError) -> str:
    """A safe, short description of a broker error.

    Guarded: `APIError.message` parses the response body as JSON and raises on
    anything else, which would turn a broker hiccup into an unrelated
    traceback.
    """
    try:
        return str(error.message)
    except Exception:  # noqa: BLE001 - the provider payload is not always JSON
        return str(error)


def _http_status(error: APIError) -> int | None:
    """The HTTP status behind an `APIError`, or None when it cannot be known."""
    try:
        status = error.status_code
    except Exception:  # noqa: BLE001 - the attribute is derived, not stored
        return None
    return status if isinstance(status, int) else None


def _is_definite_rejection(error: APIError) -> bool:
    """Whether the broker definitively refused, so no order can exist.

    Only a `4xx` that is not a timeout or a rate limit qualifies. A `5xx`, a
    timeout, a rate limit, or a status that cannot be read at all leaves the
    outcome unknown - the request may have reached the matching engine - and
    must never be reported as a rejection.
    """
    status = _http_status(error)
    if status is None:
        return False
    return 400 <= status < 500 and status not in _AMBIGUOUS_HTTP_STATUSES


def _to_snapshot(order: Order) -> BrokerOrderSnapshot:
    """Normalize an Alpaca order into the snapshot this phase stores."""
    status = order.status.value if isinstance(order.status, Enum) else str(order.status)
    side = order.side.value.upper() if isinstance(order.side, Enum) else str(order.side).upper()
    return BrokerOrderSnapshot(
        broker_order_id=str(order.id),
        client_order_id=str(order.client_order_id),
        symbol=str(order.symbol),
        side=side,
        quantity=_to_whole_shares(order.qty, "order quantity"),
        filled_quantity=_to_whole_shares(order.filled_qty or 0, "filled quantity"),
        filled_average_price=_optional_float(order.filled_avg_price),
        status=status,
        submitted_at=order.submitted_at,
        filled_at=order.filled_at,
    )


# --------------------------------------------------------------------------
# Duplicate preflight
# --------------------------------------------------------------------------


def find_broker_order_by_client_id(
    client: TradingClient, client_order_id: str
) -> BrokerOrderSnapshot | None:
    """Ask the broker whether it already has an order under this key.

    Returns the existing order, or None **only** when the broker clearly says
    no such order exists (a `404`). Any other failure - a `5xx`, a timeout, an
    unreadable status - raises `DuplicatePreflightUnavailableError` and stops
    the attempt.

    That asymmetry is the whole point: "the check failed" and "there is no
    duplicate" are different answers, and conflating them is how a system
    submits an order it has already submitted.
    """
    try:
        order = client.get_order_by_client_id(client_order_id)
    except APIError as error:
        if _http_status(error) == 404:
            return None
        raise DuplicatePreflightUnavailableError(
            "Could not check whether this order already exists at the broker "
            f"({_api_error_text(error)}). Refusing to submit, because a failed check "
            "is not the same as a clean one. Nothing was submitted."
        ) from None
    except Exception as error:  # noqa: BLE001 - any failure here must fail closed
        raise DuplicatePreflightUnavailableError(
            "Could not check whether this order already exists at the broker "
            f"({type(error).__name__}). Refusing to submit. Nothing was submitted."
        ) from None

    if order is None:
        return None
    if not isinstance(order, Order):
        raise DuplicatePreflightUnavailableError(
            "The broker returned a duplicate check in an unexpected shape. Refusing to submit."
        )
    return _to_snapshot(order)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SubmissionResult:
    """What one submission attempt produced.

    `duplicate` distinguishes "the broker already had this order, so nothing
    was sent" from "this call submitted it". Both leave a stored snapshot, but
    only one of them placed an order, and an audit trail that cannot tell them
    apart is not an audit trail.
    """

    snapshot: BrokerOrderSnapshot
    duplicate: bool


@dataclass(frozen=True)
class PaperExecutionResult:
    """Everything one execution attempt produced, for reporting and testing."""

    outcome: ExecutionOutcome
    symbol: str
    side: OrderSide
    requested_quantity: int
    reference_price: float
    risk_decision: RiskDecision
    account: PaperAccountState
    clock: MarketClock
    message: str
    intent: OrderIntent | None = None
    order_intent_id: int | None = None
    broker_order: BrokerOrderSnapshot | None = None

    @property
    def submitted(self) -> bool:
        """Whether a broker order is known to exist for this attempt.

        False for `UNKNOWN`: that is the honest answer, and treating it as a
        submission would let a caller assume a fill that may not exist.
        """
        return self.outcome in (ExecutionOutcome.SUBMITTED, ExecutionOutcome.DUPLICATE)


def _persist_broker_snapshot(
    connection: sqlite3.Connection,
    *,
    order_intent_id: int,
    snapshot: BrokerOrderSnapshot,
    status: str,
    now: datetime,
) -> None:
    """Store the broker's answer and move the intent, atomically."""
    with state.transaction(connection):
        state.upsert_broker_order(
            connection,
            order_intent_id=order_intent_id,
            broker_order_id=snapshot.broker_order_id,
            client_order_id=snapshot.client_order_id,
            symbol=snapshot.symbol,
            side=snapshot.side,
            quantity=snapshot.quantity,
            filled_quantity=snapshot.filled_quantity,
            filled_average_price=snapshot.filled_average_price,
            status=snapshot.status,
            submitted_at=snapshot.submitted_at,
            filled_at=snapshot.filled_at,
            updated_at=now,
        )
        state.update_order_intent_status(
            connection, order_intent_id=order_intent_id, status=status, updated_at=now
        )


def submit_order_intent(
    connection: sqlite3.Connection,
    client: TradingClient,
    intent: OrderIntent,
    order_intent_id: int,
    *,
    now: datetime,
) -> SubmissionResult:
    """Submit one already-persisted intent, exactly once.

    The intent **must** already be committed: this function is the point of no
    return, and the caller's durable `client_order_id` is what makes the
    outcome recoverable.

    A duplicate preflight runs first. If the broker already has an order under
    this key, that order is recorded and returned and **nothing is submitted**.

    `submit_order` is called at most once. There is no retry, no backoff, and
    no second attempt under any circumstances:

    - a returned order marks the intent `SUBMITTED`;
    - a definite broker rejection marks it `REJECTED` and raises
      `BrokerRejectedOrderError`;
    - anything ambiguous marks it `UNKNOWN`, writes an audit event, and raises
      `AmbiguousSubmissionError`.

    The `client_order_id` is never regenerated, so the ambiguous case stays
    resolvable by asking the broker about that exact key.
    """
    existing = find_broker_order_by_client_id(client, intent.client_order_id)
    if existing is not None:
        _persist_broker_snapshot(
            connection,
            order_intent_id=order_intent_id,
            snapshot=existing,
            status=state.INTENT_STATUS_SUBMITTED,
            now=now,
        )
        state.record_system_event(
            connection,
            event_timestamp=now,
            event_type=EVENT_DUPLICATE,
            message=(
                f"Broker already had an order for client_order_id "
                f"{intent.client_order_id}; no second order was submitted."
            ),
        )
        return SubmissionResult(snapshot=existing, duplicate=True)

    state.update_order_intent_status(
        connection,
        order_intent_id=order_intent_id,
        status=state.INTENT_STATUS_SUBMITTING,
        updated_at=now,
    )

    request = build_market_order_request(intent)
    try:
        order = client.submit_order(request)
    except APIError as error:
        if _is_definite_rejection(error):
            detail = _api_error_text(error)
            state.update_order_intent_status(
                connection,
                order_intent_id=order_intent_id,
                status=state.INTENT_STATUS_REJECTED,
                updated_at=now,
            )
            state.record_system_event(
                connection,
                event_timestamp=now,
                event_type=EVENT_REJECTED,
                message=(f"Broker rejected client_order_id {intent.client_order_id}: {detail}"),
            )
            raise BrokerRejectedOrderError(
                f"The broker rejected the order: {detail}. No order was created."
            ) from None
        raise _mark_unknown(
            connection, intent, order_intent_id, now, _api_error_text(error)
        ) from None
    except Exception as error:  # noqa: BLE001 - an ambiguous failure must not be retried
        raise _mark_unknown(
            connection, intent, order_intent_id, now, type(error).__name__
        ) from None

    snapshot = _to_snapshot(order)
    _persist_broker_snapshot(
        connection,
        order_intent_id=order_intent_id,
        snapshot=snapshot,
        status=state.INTENT_STATUS_SUBMITTED,
        now=now,
    )
    state.record_system_event(
        connection,
        event_timestamp=now,
        event_type=EVENT_SUBMITTED,
        message=(
            f"Paper order {snapshot.broker_order_id} accepted for client_order_id "
            f"{intent.client_order_id} ({snapshot.side} {snapshot.quantity} "
            f"{snapshot.symbol}, broker status {snapshot.status})."
        ),
    )
    return SubmissionResult(snapshot=snapshot, duplicate=False)


def _mark_unknown(
    connection: sqlite3.Connection,
    intent: OrderIntent,
    order_intent_id: int,
    now: datetime,
    detail: str,
) -> AmbiguousSubmissionError:
    """Record an ambiguous submission outcome and build the error to raise.

    The intent moves to `UNKNOWN` and keeps its `client_order_id`. No second
    submission is attempted here or anywhere else - that is the entire point.
    """
    state.update_order_intent_status(
        connection,
        order_intent_id=order_intent_id,
        status=state.INTENT_STATUS_UNKNOWN,
        updated_at=now,
    )
    state.record_system_event(
        connection,
        event_timestamp=now,
        event_type=EVENT_UNKNOWN,
        message=(
            f"Submission outcome unknown for client_order_id {intent.client_order_id} "
            f"({detail}). The order may or may not exist at the broker. It was NOT "
            "retried and the client_order_id was NOT regenerated."
        ),
    )
    return AmbiguousSubmissionError(
        "The submission outcome is unknown: the broker may or may not have accepted "
        f"the order ({detail}). It was not retried. The intent is recorded as UNKNOWN "
        f"under client_order_id {intent.client_order_id}; resolve it against the "
        "broker before submitting anything else for this symbol."
    )


def execute_paper_order(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    side: str | OrderSide,
    requested_quantity: int,
    trading_client: TradingClient | None = None,
    data_client: StockHistoricalDataClient | None = None,
    dry_run: bool = False,
    trading_enabled: bool = True,
    strategy_run_id: int | None = None,
    now: datetime | None = None,
) -> PaperExecutionResult:
    """Run the full paper execution pipeline for one order.

    The order of operations is the safety contract:

    1. validate the request against this phase's scope;
    2. read the paper account and refuse a non-tradable one;
    3. read positions and the market clock;
    4. read the **current** IEX reference price, failing closed without one;
    5. evaluate risk against the real account state;
    6. persist the risk decision;
    7. stop here if risk refused - no intent, no broker request;
    8. stop here if this is a dry run - nothing is persisted or sent;
    9. create the intent with its `client_order_id` and **commit** it;
    10. preflight for a duplicate, failing closed if the check cannot complete;
    11. submit exactly once;
    12. persist whatever the broker said.

    Steps 9 and 11 are in that order deliberately: a crash between them leaves
    a durable key that Phase 8 can resolve, whereas submitting first would
    leave a real order with no local trace.

    The broker quantity is always `RiskDecision.approved_quantity`. If risk
    clamps 100 shares to 3, the broker is asked for 3.

    Returns a `PaperExecutionResult`. Expected operational failures - a closed
    gate, missing credentials, an untradable account, no price, an
    incompletable duplicate check, a broker rejection, an ambiguous outcome -
    raise an `ExecutionError` subclass rather than returning quietly.
    """
    moment = now if now is not None else datetime.now(UTC)
    ticker = normalize_symbol(symbol)
    order_side = normalize_side(side)
    quantity = require_whole_share_quantity(requested_quantity, "requested_quantity")

    if not dry_run:
        require_paper_trading_enabled()

    client = trading_client if trading_client is not None else create_paper_trading_client()
    prices = data_client if data_client is not None else create_market_data_client()

    account = fetch_paper_account_state(client)
    require_tradable_account(account)
    positions = fetch_paper_positions(client)
    clock = fetch_market_clock(client)
    reference_price = fetch_reference_price(prices, ticker)

    context = build_risk_context(account, positions, ticker, trading_enabled=trading_enabled)
    decision = evaluate_risk(
        RiskRequest(
            symbol=ticker,
            side=_to_risk_side(order_side),
            reference_price=reference_price,
            requested_quantity=quantity,
        ),
        context,
    )

    # The observed position is recorded from what the broker actually reports,
    # before anything is submitted. Nothing later in this function updates it:
    # an accepted order is not a fill, and inferring a position from one would
    # be a fabrication. Phase 8 reconciles this table properly.
    held = positions.get(ticker)
    state.upsert_position(
        connection,
        symbol=ticker,
        quantity=held.quantity if held is not None else 0,
        average_price=held.average_entry_price if held is not None else None,
        updated_at=moment,
    )

    state.record_risk_event(
        connection,
        event_timestamp=moment,
        decision="APPROVED" if decision.approved else "REJECTED",
        reason_code=decision.reason_code,
        symbol=ticker,
        message=decision.message,
        strategy_run_id=strategy_run_id,
    )

    common = {
        "symbol": ticker,
        "side": order_side,
        "requested_quantity": quantity,
        "reference_price": reference_price,
        "risk_decision": decision,
        "account": account,
        "clock": clock,
    }

    if not decision.approved:
        return PaperExecutionResult(
            outcome=ExecutionOutcome.REJECTED_BY_RISK,
            message=decision.message,
            **common,
        )

    intent = OrderIntent(
        symbol=ticker,
        side=order_side,
        requested_quantity=quantity,
        approved_quantity=decision.approved_quantity,
        reference_price=reference_price,
        risk_reason_code=decision.reason_code,
        created_at=moment,
        strategy_run_id=strategy_run_id,
    )

    if dry_run:
        # Deliberately not persisted. No broker attempt will follow, so a row
        # here would be an intent that never had a chance to become an order -
        # noise in the very table crash recovery has to trust.
        return PaperExecutionResult(
            outcome=ExecutionOutcome.DRY_RUN,
            message="Dry run: nothing was persisted and no order was submitted.",
            intent=intent,
            **common,
        )

    order_intent_id = state.record_order_intent(
        connection,
        client_order_id=intent.client_order_id,
        created_at=intent.created_at,
        symbol=intent.symbol,
        side=intent.side.value,
        requested_quantity=intent.requested_quantity,
        approved_quantity=intent.approved_quantity,
        reference_price=intent.reference_price,
        risk_reason_code=intent.risk_reason_code,
        strategy_run_id=strategy_run_id,
        status=state.INTENT_STATUS_CREATED,
    )

    submission = submit_order_intent(connection, client, intent, order_intent_id, now=moment)
    snapshot = submission.snapshot
    prefix = (
        "The broker already had an order under this client_order_id, so nothing was submitted"
        if submission.duplicate
        else "Paper order accepted"
    )
    return PaperExecutionResult(
        outcome=(
            ExecutionOutcome.DUPLICATE if submission.duplicate else ExecutionOutcome.SUBMITTED
        ),
        message=(
            f"{prefix}: broker order {snapshot.broker_order_id}, status "
            f"{snapshot.status}. Accepted is not filled."
        ),
        intent=intent,
        order_intent_id=order_intent_id,
        broker_order=snapshot,
        **common,
    )


__all__ = [
    "CONFIRMATION_TOKEN",
    "EVENT_DUPLICATE",
    "EVENT_REJECTED",
    "EVENT_SUBMITTED",
    "EVENT_UNKNOWN",
    "PAPER_TRADING_ENABLED_ENV",
    "PAPER_TRADING_ENABLED_VALUE",
    "REFERENCE_PRICE_FEED",
    "TRADABLE_ACCOUNT_STATUSES",
    "AccountNotTradableError",
    "AmbiguousSubmissionError",
    "BrokerOrderSnapshot",
    "BrokerRejectedOrderError",
    "ConfirmationRequiredError",
    "DuplicatePreflightUnavailableError",
    "ExecutionOutcome",
    "MarketClock",
    "MissingCredentialsError",
    "PaperAccountState",
    "PaperExecutionResult",
    "PaperPosition",
    "PaperTradingDisabledError",
    "ReferencePriceUnavailableError",
    "SubmissionResult",
    "UnsupportedBrokerStateError",
    "build_market_order_request",
    "build_risk_context",
    "create_market_data_client",
    "create_paper_trading_client",
    "credentials_configured",
    "execute_paper_order",
    "fetch_market_clock",
    "fetch_paper_account_state",
    "fetch_paper_positions",
    "fetch_reference_price",
    "find_broker_order_by_client_id",
    "paper_trading_enabled",
    "require_confirmation",
    "require_paper_trading_enabled",
    "require_tradable_account",
    "submit_order_intent",
]
