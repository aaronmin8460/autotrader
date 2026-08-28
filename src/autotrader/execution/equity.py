"""Equity V0.2: the Alpaca **paper** equity execution boundary.

The equity counterpart of C7, and the only place in this branch that submits a
stock order or reads the broker's market calendar. Everything about live
trading being impossible is still checkable by reading one file: this module
constructs **no** trading client of its own. It calls
`autotrader.execution.paper.create_paper_trading_client`, which hardcodes
``paper=True`` and takes no parameter that could change it, and there is no
live factory, no `paper` argument, and no environment variable that selects an
environment.

**Everything reusable is reused, and nothing safety-critical is re-implemented.**
The environment gate, the confirmation token, the account read, the position
read, the short refusal, the duplicate preflight, the exactly-once submission,
the never-retry-an-ambiguous-outcome rule, the intent-before-broker ordering,
the broker snapshot persistence and the audit events are all C7's, called from
here. What this module adds is exactly the four things equities differ in:

1. **Whole shares.** The approved quantity is floored to an integral number of
   shares. Equity V0.2 does not place fractional or notional orders - the
   policy is written down once, in `normalize_share_quantity`, and rounding is
   always **down**, so the broker is never asked for more than risk approved.
2. **DAY, not GTC.** A day order expires with the session it was placed in,
   which is what a regular-hours-only system wants. GTC would leave an unfilled
   order alive across sessions this process has no plan to manage.
3. **Regular hours only.** `extended_hours` is never set, and a submission is
   refused unless the **broker's own clock** says the session is open at that
   moment. The clock is read immediately before submitting rather than trusted
   from the start of the cycle.
4. **No USD minimum notional.** Alpaca's $10 cost-basis floor is a crypto rule.
   One whole share is the equity minimum, and it is enforced as exactly that.

**One account, one risk day.** The daily-loss baseline is the same UTC-day
baseline the crypto path uses, from the same `daily_risk_baselines` table. A
separate equity baseline would mean two different answers to "how much has this
account lost today" for an account that has one - which is precisely the mistake
the combined-integration phase exists to avoid.

**Long only, and it cannot be otherwise.** `OrderSide` has no short side, the
risk engine clamps a SELL to the position, and a short position reported by the
broker is refused by the shared position reader before anything is sized.

**Known limitation, stated rather than hidden.** A whole-share exit cannot
close a *fractional* position: 2.5 shares floors to a 2-share SELL, leaving 0.5
held. Nothing in this branch can create a fractional equity position, and
reconciliation reports the remainder rather than trading it away.

Scope: US equities, the ten Equity V0.2 symbols, whole shares, MARKET orders,
DAY time in force, regular hours, long only, Alpaca paper. No fractional or
notional orders, no limit/stop/bracket/OCO orders, no options, no crypto, no
shorts, no extended hours, and no streaming.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import Enum

from alpaca.common.exceptions import APIError
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus, TimeInForce
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.models import Asset
from alpaca.trading.requests import GetCalendarRequest, MarketOrderRequest

from autotrader.equity import EQUITY_SYMBOLS, EquityError, normalize_symbol
from autotrader.equity.data import FEED, create_client
from autotrader.equity.session import (
    MarketSession,
    SessionError,
    market_date,
    session_from_local,
)
from autotrader.execution.models import (
    ExecutionError,
    ExecutionInputError,
    OrderIntent,
    OrderSide,
    format_quantity,
    normalize_side,
    require_quantity,
    require_reference_price,
)
from autotrader.execution.paper import (
    AssetNotTradableError,
    ExecutionOutcome,
    PaperAccountState,
    PaperExecutionResult,
    QuantityBelowMinimumError,
    ReferencePriceUnavailableError,
    UnsupportedBrokerStateError,
    broker_symbol_key,
    build_risk_context,
    create_paper_trading_client,
    fetch_paper_account_state,
    fetch_paper_positions,
    require_paper_trading_enabled,
    require_tradable_account,
    resolve_daily_baseline_equity,
    submit_order_intent,
)
from autotrader.risk import RiskRequest, RiskSide, evaluate_risk
from autotrader.state import sqlite as state

#: The only time in force Equity V0.2 sends.
#:
#: `DAY` expires with the session it was placed in, which is the right lifetime
#: for a regular-hours-only system: an order this process placed and did not
#: get filled should not outlive the day it was decided on. `GTC` would leave
#: it alive across sessions nobody planned for, and `IOC` would silently cancel
#: the unfilled part of an order this system believes it placed.
EQUITY_ORDER_TIME_IN_FORCE = TimeInForce.DAY

#: The market-data feed the reference price comes from - the same IEX feed the
#: bars come from, so sizing and research see one source.
REFERENCE_PRICE_FEED = FEED

#: The smallest equity order this milestone will place: one whole share.
MINIMUM_SHARE_QUANTITY = Decimal(1)

#: The share quantum. Whole shares, so a quantity is quantized down to this.
SHARE_INCREMENT = Decimal(1)

#: How many extra days a calendar fetch reaches past what was asked for, so a
#: runtime that asks about "today" every fifteen minutes does not re-ask the
#: broker each time the date rolls over.
CALENDAR_FETCH_PAD_DAYS = 14

_ZERO = Decimal(0)


class MarketClosedError(ExecutionError):
    """The regular session is not open, so no equity order may be submitted."""


class EquityAssetNotTradableError(AssetNotTradableError):
    """The broker will not let this equity be traded right now."""


@dataclass(frozen=True)
class EquityAssetSpec:
    """What the broker says about one equity, right now.

    Read live on every attempt rather than remembered. A halted or delisted
    symbol is a fact about today, and a system that cached "tradable" from
    yesterday would keep sending orders into a name the broker has stopped
    accepting them for.

    `min_order_size` and `min_trade_increment` are deliberately absent: Alpaca
    reports both as `null` for equities, and inventing values for them would be
    guessing at a contract the broker does not publish. The whole-share policy
    is this system's, is stated in `normalize_share_quantity`, and is not
    dressed up as broker metadata.
    """

    symbol: str
    asset_class: str
    status: str
    tradable: bool
    fractionable: bool


@dataclass(frozen=True)
class MarketClock:
    """The broker's own answer to "is the regular session open right now?".

    Authoritative, and read immediately before a submission rather than
    inferred from a calendar this process fetched earlier. A calendar says what
    the session times are; the clock says what time it is according to the
    venue that will receive the order.
    """

    is_open: bool
    timestamp: datetime
    next_open: datetime | None = None
    next_close: datetime | None = None


# --------------------------------------------------------------------------
# Broker reads
# --------------------------------------------------------------------------


def create_market_data_client() -> StockHistoricalDataClient:
    """Build the stock market-data client. Market data only; it cannot trade."""
    return create_client()


def _api_error_text(error: APIError) -> str:
    """A safe, short description of a broker error."""
    try:
        return str(error.message)
    except Exception:  # noqa: BLE001 - the provider payload is not always JSON
        return str(error)


def _enum_value(value: object) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def fetch_equity_asset(client: TradingClient, symbol: str) -> EquityAssetSpec:
    """Read one equity's live broker metadata, or fail closed.

    The asset must be a US equity - not a crypto pair and not an option - and
    it must be active and tradable. Anything missing, contradictory, or simply
    unreadable raises rather than being treated as permission to proceed.
    """
    ticker = normalize_symbol(symbol)
    try:
        asset = client.get_asset(ticker)
    except APIError as error:
        raise EquityAssetNotTradableError(
            f"Could not read broker metadata for {ticker}: {_api_error_text(error)}. "
            "Nothing was submitted."
        ) from None
    except Exception as error:  # noqa: BLE001 - any failure here must fail closed
        raise EquityAssetNotTradableError(
            f"Could not read broker metadata for {ticker}: {type(error).__name__}. "
            "Nothing was submitted."
        ) from None

    if not isinstance(asset, Asset):
        raise EquityAssetNotTradableError(
            f"The broker returned {ticker} asset metadata in an unexpected shape. "
            "Nothing was submitted."
        )

    asset_class = _enum_value(asset.asset_class)
    status = _enum_value(asset.status)
    if asset_class != AssetClass.US_EQUITY.value:
        raise EquityAssetNotTradableError(
            f"{ticker} is reported as asset class {asset_class!r}, not "
            f"{AssetClass.US_EQUITY.value!r}. This boundary trades US equities only. "
            "Nothing was submitted."
        )
    if status != AssetStatus.ACTIVE.value:
        raise EquityAssetNotTradableError(
            f"{ticker} is {status!r} at the broker, not {AssetStatus.ACTIVE.value!r}. "
            "Nothing was submitted."
        )
    if not asset.tradable:
        raise EquityAssetNotTradableError(
            f"{ticker} is not tradable at the broker. Nothing was submitted."
        )

    return EquityAssetSpec(
        symbol=ticker,
        asset_class=asset_class,
        status=status,
        tradable=bool(asset.tradable),
        fractionable=bool(asset.fractionable),
    )


def fetch_market_clock(client: TradingClient) -> MarketClock:
    """Read the broker's market clock.

    Unlike the archived equity milestone, this **is** used to gate a
    submission: Equity V0.2 trades regular hours only, and the broker's clock
    is the authority on whether they are running.
    """
    try:
        clock = client.get_clock()
    except APIError as error:
        raise SessionError(
            f"Could not read the broker's market clock: {_api_error_text(error)}. "
            "Nothing was submitted."
        ) from None
    except Exception as error:  # noqa: BLE001 - an unreadable clock must fail closed
        raise SessionError(
            f"Could not read the broker's market clock: {type(error).__name__}. "
            "Nothing was submitted."
        ) from None

    timestamp = getattr(clock, "timestamp", None)
    return MarketClock(
        is_open=bool(getattr(clock, "is_open", False)),
        timestamp=(
            timestamp.astimezone(UTC) if isinstance(timestamp, datetime) else datetime.now(UTC)
        ),
        next_open=_optional_utc(getattr(clock, "next_open", None)),
        next_close=_optional_utc(getattr(clock, "next_close", None)),
    )


def _optional_utc(value: object) -> datetime | None:
    return value.astimezone(UTC) if isinstance(value, datetime) else None


def require_market_open(client: TradingClient) -> MarketClock:
    """Raise unless the broker says the regular session is open right now.

    The last gate before a submission, and the one that makes "regular market
    hours only" a property of the code rather than of the schedule that usually
    calls it. A cycle that started inside the session but took long enough to
    finish outside it is refused here.
    """
    clock = fetch_market_clock(client)
    if not clock.is_open:
        raise MarketClosedError(
            "The US regular market session is not open "
            f"({clock.timestamp.isoformat()}), and Equity V0.2 trades regular hours "
            "only. No order was submitted and no order was queued for a later "
            "session."
        )
    return clock


def fetch_reference_price(client: StockHistoricalDataClient, symbol: str) -> float:
    """Return the latest IEX trade price for `symbol`.

    The *current* market price, not a stored historical bar: sizing a live
    order against yesterday's Parquet close would be wrong. A price that cannot
    be obtained, or that is not finite and positive, fails closed - no order is
    sized or submitted.
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
# The broker's session calendar
# --------------------------------------------------------------------------


class AlpacaMarketCalendar:
    """The production `MarketCalendar`, over Alpaca's own calendar endpoint.

    Holidays, weekends and early closes are read rather than assumed: a day the
    market is shut is simply absent from the response, and a half day reports
    its real 13:00 close. Nothing here hardcodes a date or a weekday rule.

    **Alpaca reports session times as naive Eastern wall-clock.**
    `session_from_local` attaches `MARKET_TIMEZONE` and converts to UTC, once,
    so no other module has to know that.

    Results are cached over a contiguous date range and fetched with padding,
    because the runtime asks about "today" every fifteen minutes and the answer
    changes once a day. `api_calls` counts what was actually sent, for the
    later shared crypto+equity API budget.
    """

    def __init__(self, client: TradingClient | None = None) -> None:
        self._client = client
        self._sessions: dict[date, MarketSession] = {}
        self._covered: tuple[date, date] | None = None
        #: Calendar requests actually sent to the provider.
        self.api_calls = 0

    def _resolve_client(self) -> TradingClient:
        if self._client is None:
            self._client = create_paper_trading_client()
        return self._client

    def _fetch(self, start: date, end: date) -> None:
        """Replace the cache with the sessions in an inclusive date range."""
        client = self._resolve_client()
        self.api_calls += 1
        try:
            entries = client.get_calendar(GetCalendarRequest(start=start, end=end))
        except APIError as error:
            raise SessionError(
                f"Could not read the broker's market calendar: {_api_error_text(error)}."
            ) from None
        except Exception as error:  # noqa: BLE001 - an unreadable calendar must fail closed
            raise SessionError(
                f"Could not read the broker's market calendar: {type(error).__name__}."
            ) from None

        sessions: dict[date, MarketSession] = {}
        for entry in entries or ():
            session_date = getattr(entry, "date", None)
            open_local = getattr(entry, "open", None)
            close_local = getattr(entry, "close", None)
            if session_date is None or open_local is None or close_local is None:
                raise SessionError(
                    "The broker returned a calendar entry without a date, an open, or a "
                    "close. Refusing to infer a session from an incomplete one."
                )
            sessions[session_date] = session_from_local(session_date, open_local, close_local)
        self._sessions = sessions
        self._covered = (start, end)

    def _ensure(self, start: date, end: date) -> None:
        covered = self._covered
        if covered is not None and covered[0] <= start and end <= covered[1]:
            return
        wanted_start = min(start, covered[0]) if covered is not None else start
        wanted_end = max(end, covered[1]) if covered is not None else end
        self._fetch(wanted_start, wanted_end + timedelta(days=CALENDAR_FETCH_PAD_DAYS))

    def session_for(self, day: date) -> MarketSession | None:
        """The regular session on `day`, or None when the market is closed."""
        self._ensure(day, day)
        return self._sessions.get(day)

    def sessions_between(self, start: date, end: date) -> tuple[MarketSession, ...]:
        """Every session in the inclusive date range, ascending."""
        self._ensure(start, end)
        return tuple(
            session for day, session in sorted(self._sessions.items()) if start <= day <= end
        )


# --------------------------------------------------------------------------
# Whole-share quantities
# --------------------------------------------------------------------------


def normalize_share_quantity(quantity: Decimal, symbol: str) -> Decimal:
    """Round `quantity` **down** to a whole number of shares, or refuse it.

    Down, always. Rounding up would send more than the risk engine approved,
    which is the one direction this boundary may never move in. A quantity that
    floors below one share is not an order, and it is reported as such rather
    than rounded back up to one.

    The result is an integral `Decimal`, not an `int`: every quantity in the
    database, the intent, and the risk decision is a `Decimal`, and converting
    to `int` here would put a second numeric type into the one place where
    exactness is the point.
    """
    amount = require_quantity(quantity, "quantity")
    shares = amount.quantize(SHARE_INCREMENT, rounding=ROUND_FLOOR)
    if shares < MINIMUM_SHARE_QUANTITY:
        raise QuantityBelowMinimumError(
            f"{format_quantity(amount)} {symbol} rounds down to "
            f"{format_quantity(shares)} whole shares, and Equity V0.2 places whole-share "
            "orders only. No order was submitted: rounding up would exceed what risk "
            "approved. Request a larger quantity, or wait for more headroom."
        )
    return shares


def to_wire_shares(quantity: Decimal) -> float:
    """Render a whole-share quantity as the float the SDK's request field takes.

    `MarketOrderRequest.qty` is typed as a float, so the exact `Decimal` has to
    become one somewhere; it becomes one here, and only after checking the
    value is integral. A share count is a small integer and converts exactly,
    so - unlike the crypto path's fractional quantities - there is no rounding
    artefact to guard against, and the integral check is what proves it.
    """
    amount = require_quantity(quantity, "quantity")
    if amount != amount.to_integral_value():
        raise QuantityBelowMinimumError(
            f"{format_quantity(amount)} is not a whole number of shares. No order was submitted."
        )
    return float(amount)


def build_equity_market_order_request(intent: OrderIntent) -> MarketOrderRequest:
    """Translate an approved equity intent into the Alpaca request to send.

    Always a MARKET order, `EQUITY_ORDER_TIME_IN_FORCE` (DAY), and carrying the
    intent's `client_order_id`. `notional` is never set - a notional order
    would be sized in dollars by the broker rather than by the risk engine.
    `extended_hours` is never set either: leaving it at its default is what
    makes this a regular-hours order, and setting it would contradict the
    session gate that ran a moment earlier.

    The quantity is `approved_quantity` - risk's number after flooring to whole
    shares. The requested quantity is deliberately not reachable from here.
    """
    return MarketOrderRequest(
        symbol=intent.symbol,
        qty=to_wire_shares(intent.approved_quantity),
        side=AlpacaOrderSide.BUY if intent.side is OrderSide.BUY else AlpacaOrderSide.SELL,
        time_in_force=EQUITY_ORDER_TIME_IN_FORCE,
        client_order_id=intent.client_order_id,
    )


def _to_risk_side(side: OrderSide) -> RiskSide:
    """Translate an execution side into the risk engine's vocabulary."""
    return RiskSide.BUY if side is OrderSide.BUY else RiskSide.SELL


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def execute_equity_paper_order(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    side: str | OrderSide,
    requested_quantity: Decimal,
    trading_client: TradingClient | None = None,
    data_client: StockHistoricalDataClient | None = None,
    dry_run: bool = False,
    trading_enabled: bool = True,
    strategy_run_id: int | None = None,
    now: datetime | None = None,
) -> PaperExecutionResult:
    """Run the full paper execution pipeline for one equity order.

    The order of operations is the safety contract, and it is C7's with one
    step inserted:

    1. validate the request against the Equity V0.2 universe;
    2. read the paper account and refuse a non-tradable one;
    3. read positions, which refuses any short;
    4. read the equity's live broker metadata, failing closed without it;
    5. read the **current** IEX reference price, failing closed without one;
    6. resolve the durable UTC-day equity baseline - the account's, shared with
       the crypto book;
    7. evaluate risk against the real account state;
    8. persist the risk decision;
    9. stop here if risk refused - no intent, no broker request;
    10. floor the approved quantity to whole shares, refusing below one;
    11. stop here if this is a dry run - nothing is persisted or sent;
    12. **refuse unless the broker's clock says the session is open**;
    13. create the intent with its `client_order_id` and **commit** it;
    14. preflight for a duplicate, failing closed if the check cannot complete;
    15. submit exactly once;
    16. persist whatever the broker said.

    Step 12 is the equity-specific gate and it sits *before* the intent is
    written on purpose: a closed market is not a submission that might have
    happened, so it must not leave a `CREATED` intent behind for reconciliation
    to chase. Steps 13 and 15 remain in that order for the opposite reason - a
    crash between them leaves a durable key Phase 8 can resolve.

    The broker quantity is never more than `RiskDecision.approved_quantity`.
    Returns a `PaperExecutionResult`; expected operational failures raise an
    `ExecutionError` subclass rather than returning quietly.
    """
    moment = now if now is not None else datetime.now(UTC)
    ticker = normalize_symbol(symbol)
    order_side = normalize_side(side)
    quantity = require_quantity(requested_quantity, "requested_quantity")

    if not dry_run:
        require_paper_trading_enabled()

    client = trading_client if trading_client is not None else create_paper_trading_client()
    prices = data_client if data_client is not None else create_market_data_client()

    account = fetch_paper_account_state(client)
    require_tradable_account(account)
    positions = fetch_paper_positions(client)
    asset = fetch_equity_asset(client, ticker)
    reference_price = fetch_reference_price(prices, ticker)

    baseline_equity = resolve_daily_baseline_equity(connection, equity=account.equity, now=moment)
    context = build_risk_context(
        account,
        positions,
        ticker,
        daily_baseline_equity=baseline_equity,
        trading_enabled=trading_enabled,
    )
    decision = evaluate_risk(
        RiskRequest(
            symbol=ticker,
            side=_to_risk_side(order_side),
            reference_price=reference_price,
            requested_quantity=quantity,
        ),
        context,
    )

    # Recorded from what the broker actually reports, before anything is
    # submitted. An accepted order is not a fill, and inferring a position from
    # one would be a fabrication.
    held = positions.get(broker_symbol_key(ticker))
    state.upsert_position(
        connection,
        symbol=ticker,
        quantity=held.quantity if held is not None else _ZERO,
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

    common: dict[str, object] = {
        "symbol": ticker,
        "side": order_side,
        "requested_quantity": quantity,
        "reference_price": reference_price,
        "risk_decision": decision,
        "account": account,
        "daily_baseline_equity": baseline_equity,
        # The equity asset carries no broker-published minimum, so there is no
        # effective-minimum figure to report; the whole-share floor is this
        # system's policy and is stated in the message when it bites.
        "asset": None,
        "effective_minimum_quantity": None,
    }

    if not decision.approved:
        return PaperExecutionResult(
            outcome=ExecutionOutcome.REJECTED_BY_RISK,
            message=decision.message,
            **common,  # type: ignore[arg-type]
        )

    share_quantity = normalize_share_quantity(decision.approved_quantity, asset.symbol)

    intent = OrderIntent(
        symbol=ticker,
        side=order_side,
        requested_quantity=quantity,
        approved_quantity=share_quantity,
        reference_price=reference_price,
        risk_reason_code=decision.reason_code,
        created_at=moment,
        strategy_run_id=strategy_run_id,
    )

    if dry_run:
        # Deliberately not persisted. No broker attempt will follow, so a row
        # here would be an intent that never had a chance to become an order.
        return PaperExecutionResult(
            outcome=ExecutionOutcome.DRY_RUN,
            message="Dry run: nothing was persisted and no order was submitted.",
            intent=intent,
            **common,  # type: ignore[arg-type]
        )

    require_market_open(client)

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

    submission = submit_order_intent(
        connection,
        client,
        intent,
        order_intent_id,
        now=moment,
        build_request=build_equity_market_order_request,
    )
    snapshot = submission.snapshot
    prefix = (
        "The broker already had an order under this client_order_id, so nothing was submitted"
        if submission.duplicate
        else "Paper equity order accepted"
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
        **common,  # type: ignore[arg-type]
    )


def equity_positions(
    positions: dict[str, object],
) -> dict[str, object]:
    """The subset of broker positions belonging to the Equity V0.2 universe.

    A small helper for status reporting and for the combined-integration seam:
    one account holds both books, and telling them apart is a lookup against
    the universe rather than a guess from the symbol's shape.
    """
    wanted = {broker_symbol_key(symbol) for symbol in EQUITY_SYMBOLS}
    return {key: value for key, value in positions.items() if key in wanted}


__all__ = [
    "CALENDAR_FETCH_PAD_DAYS",
    "EQUITY_ORDER_TIME_IN_FORCE",
    "MINIMUM_SHARE_QUANTITY",
    "REFERENCE_PRICE_FEED",
    "SHARE_INCREMENT",
    "AlpacaMarketCalendar",
    "EquityAssetNotTradableError",
    "EquityAssetSpec",
    "EquityError",
    "MarketClock",
    "MarketClosedError",
    "MarketSession",
    "PaperAccountState",
    "UnsupportedBrokerStateError",
    "build_equity_market_order_request",
    "create_market_data_client",
    "equity_positions",
    "execute_equity_paper_order",
    "fetch_equity_asset",
    "fetch_market_clock",
    "fetch_reference_price",
    "market_date",
    "normalize_share_quantity",
    "require_market_open",
    "to_wire_shares",
]
