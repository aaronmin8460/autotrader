"""C7: the Alpaca **paper** crypto execution boundary. The only file that trades.

This is the one module in the repository that constructs a broker trading
client or submits an order. Everything about live trading being impossible is
checkable by reading it.

**Paper only, structurally.** `create_paper_trading_client` hardcodes
``paper=True`` and takes no parameter that could change it. There is no live
client factory, no `paper` argument on any public function, no `--live` flag,
and no environment variable that selects an environment. Live trading here is
not "disabled by default"; it is **unexpressible**. A source-level test asserts
that ``paper=False`` appears nowhere in the package.

**Two independent gates.** Reading the account, a position, an asset, or a
price needs neither. *Submitting* needs both:

1. the environment gate ``AUTOTRADER_PAPER_TRADING_ENABLED`` set to exactly
   ``true`` - missing, empty, `false`, `TRUE`, `1`, or anything else leaves
   submission off; and
2. an explicit confirmation token typed at the CLI.

Both default to closed, and neither can satisfy the other.

**Everything fails closed.** No current price, an account that cannot trade, an
asset whose broker metadata is missing or contradictory, a duplicate check that
could not complete - each stops the attempt rather than proceeding on an
assumption. "I could not check for a duplicate" is never treated as "there is
no duplicate".

**The risk engine is not optional.** Every submission is sized by
`evaluate_risk`, and the quantity sent to the broker is never larger than
`RiskDecision.approved_quantity` - never the caller's requested quantity. A
rejected decision means no broker request is even constructed.

**The broker owns order precision.** The exact quantity sent is the
risk-approved quantity normalized to the asset's *current*
`min_trade_increment`, always rounding **down**, and refused outright if it
lands below `min_order_size`. No BTC or ETH increment is hardcoded anywhere:
provider rules change, so the broker's live asset metadata is the authority.

**Asset metadata is necessary and not sufficient.** Alpaca enforces a **$10
minimum cost basis** on a USD-quoted crypto order that its asset metadata does
not report - `min_order_size` still carries an older ~$1-notional floor - so an
order can clear every published constraint and still be refused with "cost
basis must be >= minimal amount of order 10. No order was created." That floor
is written down once, as `USD_MINIMUM_ORDER_NOTIONAL`, and combined with the
metadata in `effective_minimum_quantity`. An order below it is refused **here**,
before any broker request exists, so the outcome is a definite local rejection
rather than a round trip that ends in one. The quantity is never raised to
clear the floor: that would send more than risk approved, so the caller is told
to ask for more instead.

**There is no market clock here.** Crypto trades continuously, so there is no
session to open or close and nothing to gate a submission on. `get_clock()` is
an equity-market concept and is not called.

**The risk day is a UTC calendar day.** The daily-loss baseline is the first
account equity observed on a UTC date, recorded durably in
`daily_risk_baselines`. Alpaca's `last_equity` is an equity-session previous
close and is deliberately **not** used: a 24/7 market has no previous close.

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

**Reconciliation lives elsewhere, and reads through here.** Nothing in this
module resolves an `UNKNOWN` intent or repairs a position;
`autotrader.reconciliation` does that, and it reaches the broker through the
read-only helpers below - `fetch_paper_account_state`, `fetch_paper_positions`,
`find_broker_order_by_client_id` - so that the broker boundary stays one file.
`verify_paper_environment` is the check it runs first: a process about to
rewrite local state from what a broker says must prove which broker that is.

Scope: crypto spot, BTC/USD and ETH/USD, fractional quantities, MARKET orders,
GTC time in force, long only. No DAY and no IOC, no notional orders, no
limit/stop/bracket/OCO orders, no options, no equities, no shorts, and no
streaming - see docs/SPEC.md section 8, "C7".
"""

from __future__ import annotations

import math
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation, localcontext
from enum import Enum

from alpaca.common.enums import BaseURL
from alpaca.common.exceptions import APIError
from alpaca.data.enums import CryptoFeed
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AccountStatus,
    AssetClass,
    AssetStatus,
    PositionSide,
    TimeInForce,
)
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.models import Asset, Order, TradeAccount
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.requests import MarketOrderRequest

from autotrader.execution.models import (
    ExecutionError,
    ExecutionInputError,
    OrderIntent,
    OrderSide,
    format_quantity,
    normalize_side,
    normalize_symbol,
    normalize_tradable_symbol,
    require_quantity,
    require_reference_price,
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

#: The one host this system may reach. Taken from the SDK's own enum rather
#: than typed out, so it cannot drift from the URL `paper=True` actually
#: selects; a test asserts the client the factory builds matches it.
PAPER_TRADING_BASE_URL = BaseURL.TRADING_PAPER.value

#: Market-data feed for the reference price. Alpaca's crypto feed, matching the
#: historical feed, so sizing and research see the same source.
REFERENCE_PRICE_FEED = CryptoFeed.US

#: The only time in force this milestone sends.
#:
#: `DAY` is an equity-session concept - it expires at a close that a 24/7
#: market does not have - and `IOC` would silently cancel the unfilled part of
#: an order this system believes it placed. `GTC` is the one that means what it
#: says here.
ORDER_TIME_IN_FORCE = TimeInForce.GTC

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

_ZERO = Decimal(0)

#: The quote currency whose minimum-notional rule this module encodes.
USD_QUOTE_CURRENCY = "USD"

#: Alpaca's **current** minimum cost basis for a USD-quoted crypto order, in
#: USD. Their crypto documentation states the rule as a quantity formula -
#: "For ``USD`` pairs, the minimum order size calculation is: 10/USD asset
#: price" - and the trading endpoint enforces the same floor as a cost basis,
#: refusing anything smaller with "cost basis must be >= minimal amount of
#: order 10. No order was created."
#:
#: It is written down here, as one number in one place, because the broker does
#: **not** report it in asset metadata. `Asset.min_order_size` still carries the
#: older $1-notional-equivalent floor - 0.000012417 BTC, about $1 at an $78,000
#: BTC - so an order sized only against that metadata clears the asset minimum
#: and is still rejected by the endpoint. Reading the metadata is necessary and
#: not sufficient; this constant closes the gap.
#:
#: Scoped deliberately to Alpaca USD crypto pairs. It is not a generic broker
#: minimum, it is not applied to non-USD pairs (Alpaca documents a separate
#: `0.000000002` floor for BTC, ETH, and USDT pairs, which the asset metadata
#: already carries), and it is not applied to equities - this system has none.
USD_MINIMUM_ORDER_NOTIONAL = Decimal("10")

#: Working precision for the threshold arithmetic below. The default context's
#: 28 significant digits are ample for a $10 floor at any real price; this is
#: set higher anyway so the ceiling is computed from a division that has not
#: itself been rounded, and the exact check that follows it never has to work.
_THRESHOLD_PRECISION = 60


class PaperTradingDisabledError(ExecutionError):
    """The environment gate is closed, so nothing may be submitted."""


class NotPaperEnvironmentError(ExecutionError):
    """A trading client could not be **proven** to reach Alpaca paper.

    Raised by `verify_paper_environment`, and deliberately not the same as
    "this client is live": a client whose environment cannot be read at all
    fails here too. Proving paper is the caller's burden, and an unproven
    client is refused rather than assumed harmless.
    """


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


class AssetNotTradableError(ExecutionError):
    """The broker's own metadata says this asset may not be traded here.

    Covers a missing, inactive, non-crypto, non-fractionable, or incoherent
    asset. Nothing is assumed on the broker's behalf: an asset whose
    constraints cannot be read is one this system will not size against.
    """


class QuantityBelowMinimumError(ExecutionError):
    """Normalizing to the broker's increment left less than its minimum order.

    Not an error to work around by rounding up: the broker's minimum is a
    floor, and exceeding a risk-approved quantity to clear it would put sizing
    outside the risk engine's control.
    """


class MinimumNotionalError(QuantityBelowMinimumError):
    """The order's estimated cost basis is below the broker's USD minimum.

    A **definitive local refusal**, raised before any broker request exists, so
    an undersized order is never sent and never has an ambiguous outcome. It is
    not a broker failure and not an `UNKNOWN`: nothing was submitted, and the
    caller knows that with certainty.

    A subclass of `QuantityBelowMinimumError` because it is the same kind of
    answer - "this quantity is too small to be an order" - reached through a
    different floor, and because every existing caller that already handles a
    too-small quantity should handle this one identically.

    The fix is never to round the quantity up. Exceeding the risk-approved
    quantity to satisfy a broker minimum would put sizing outside the risk
    engine's control, which is the one thing this boundary may not do. The
    caller must request more.
    """


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


class NonDurableIntentError(ExecutionError):
    """The order intent is not committed to disk, so nothing may be submitted.

    The one invariant the whole recovery design rests on is that a
    `client_order_id` reaches durable storage *before* it reaches the broker.
    An intent that exists only inside an open transaction is invisible to every
    other connection and disappears entirely if the process dies - so an order
    placed against one would sit at the broker under a key no restart could
    ever find, and therefore under a key nothing could stop this system from
    duplicating.

    This is a caller error, not a broker or network condition. It cannot be
    retried into working and it is fatal to the runtime.
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
    ambiguous case this milestone must classify as `UNKNOWN`, and a silent
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


def verify_paper_environment(client: TradingClient) -> str:
    """Return the paper base URL, or raise unless `client` provably reaches it.

    `create_paper_trading_client` is the only client factory here and it
    hardcodes ``paper=True``, so within this repository a live client cannot be
    constructed. This function checks the resulting object anyway, because a
    process that is about to **rewrite local state from what a broker says**
    should confirm which broker it is talking to rather than infer it from the
    fact that no other factory exists. A caller may pass any client in - a
    later phase, a test double - and the check must not depend on where the
    object came from.

    Two facts are required, and both are read defensively: the base URL the
    client will actually send requests to must be Alpaca's paper host, and the
    SDK's own sandbox flag must be set. An attribute that is missing, of an
    unexpected type, or pointing anywhere else **fails closed** - there is no
    "probably paper".

    Read-only. It sends no request, so it costs nothing and cannot itself fail
    on the network.
    """
    base_url = getattr(client, "_base_url", None)
    if isinstance(base_url, Enum):
        base_url = base_url.value
    if not isinstance(base_url, str) or base_url.strip().rstrip("/") != PAPER_TRADING_BASE_URL:
        raise NotPaperEnvironmentError(
            "Refusing to reconcile: this trading client could not be proven to reach "
            f"the Alpaca paper environment ({PAPER_TRADING_BASE_URL}). Local state is "
            "only ever repaired from paper broker truth."
        )
    if getattr(client, "_sandbox", None) is not True:
        raise NotPaperEnvironmentError(
            "Refusing to reconcile: this trading client points at the Alpaca paper host "
            "but does not report itself as a sandbox client. Nothing was changed."
        )
    return PAPER_TRADING_BASE_URL


def create_market_data_client() -> CryptoHistoricalDataClient:
    """Build the crypto market-data client used for the current reference price.

    Reuses the C1 factory rather than duplicating credential handling. Crypto
    market data is served without authentication, so this succeeds with or
    without credentials; submitting an order still requires them.
    """
    from autotrader.data.historical import create_client

    return create_client()


# --------------------------------------------------------------------------
# Normalized broker reads
#
# Alpaca returns money and quantities as strings. They are converted once,
# here, so no downstream arithmetic is done on text - and quantities become
# exact `Decimal` values rather than binary floats.
# --------------------------------------------------------------------------


def broker_symbol_key(symbol: str) -> str:
    """The provider-agnostic key for one market.

    Alpaca reports a crypto market as ``BTC/USD`` in some responses and
    ``BTCUSD`` in others. Both name the same market, so both key to ``BTCUSD``
    here and a position is matched to the pair it belongs to either way. This
    is a lookup key only: the canonical ``BTC/USD`` spelling is what the domain
    models, the stored data, and the database all use.
    """
    return symbol.strip().upper().replace("/", "")


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


def to_broker_decimal(value: object, field_name: str) -> Decimal:
    """Convert a broker quantity to an exact `Decimal`.

    Alpaca sends quantities as decimal strings, which convert exactly. A float
    is routed through its shortest round-tripping form rather than
    `Decimal(float)`, so ``0.0001`` stays ``0.0001``.
    """
    if value is None:
        raise UnsupportedBrokerStateError(
            f"The broker did not report {field_name}, which is required to reason about "
            "an order safely. Refusing to guess it."
        )
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise UnsupportedBrokerStateError(f"The broker reported a non-finite {field_name}.")
        candidate = Decimal(str(value))
    else:
        try:
            candidate = Decimal(str(value))
        except InvalidOperation:
            raise UnsupportedBrokerStateError(
                f"The broker reported a non-numeric {field_name}."
            ) from None
    if not candidate.is_finite():
        raise UnsupportedBrokerStateError(f"The broker reported a non-finite {field_name}.")
    return candidate


@dataclass(frozen=True)
class PaperAccountState:
    """The paper account, normalized to the numbers risk actually needs.

    There is deliberately **no** `last_equity` here. Alpaca's `last_equity` is
    the previous *trading day's* close, which is an equity-session concept; a
    24/7 crypto account has no such boundary, so the daily-loss baseline comes
    from `daily_risk_baselines` instead (see `resolve_daily_baseline_equity`).
    """

    equity: float
    cash: float
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
    quantity: Decimal
    market_value: float
    average_entry_price: float | None


@dataclass(frozen=True)
class CryptoAssetSpec:
    """What the broker says about one crypto asset, right now.

    This is the runtime authority on order precision. Nothing here is
    remembered from documentation: `min_order_size` and `min_trade_increment`
    are read from the broker on every attempt, because provider rules change
    and a stale constant would produce orders the broker silently refuses - or
    worse, accepts at the wrong size.
    """

    symbol: str
    asset_class: str
    status: str
    tradable: bool
    fractionable: bool
    min_order_size: Decimal
    min_trade_increment: Decimal


@dataclass(frozen=True)
class BrokerOrderSnapshot:
    """What the broker said about an order, normalized.

    `status` is the broker's own vocabulary, kept as opaque text. A snapshot
    means the order was **accepted**; it never means it filled.

    `broker_updated_at` is the broker's *own* last-modified time for the order,
    which is not the same thing as when this system wrote the snapshot down.
    It is carried for the audit trail and deliberately not persisted: the
    `broker_orders.updated_at` column already means "when this snapshot was
    refreshed", and overloading one column with two different clocks would make
    both unreadable.
    """

    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    filled_quantity: Decimal
    filled_average_price: float | None
    status: str
    submitted_at: datetime | None
    filled_at: datetime | None
    broker_updated_at: datetime | None = None


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
    if equity <= 0:
        raise UnsupportedBrokerStateError(
            "The broker reported a non-positive account equity, which cannot describe "
            "a usable account. Refusing to size an order against it."
        )

    status = account.status.value if isinstance(account.status, Enum) else str(account.status)
    return PaperAccountState(
        equity=equity,
        cash=cash,
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
    """Read every open paper position, keyed by `broker_symbol_key`.

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
        quantity = to_broker_decimal(position.qty, f"{symbol} position quantity")
        if quantity < 0:
            raise UnsupportedBrokerStateError(
                f"The broker reported a negative quantity for {symbol}. This system is "
                "long only. Nothing was submitted."
            )
        normalized[broker_symbol_key(symbol)] = PaperPosition(
            symbol=symbol,
            quantity=quantity,
            market_value=_to_float(position.market_value, f"{symbol} position market value"),
            average_entry_price=_optional_float(position.avg_entry_price),
        )
    return normalized


def _require_positive_decimal(value: object, field_name: str) -> Decimal:
    """A broker-supplied constraint that must exist and be strictly positive."""
    if value is None:
        raise AssetNotTradableError(
            f"The broker did not report {field_name}. Order precision cannot be derived "
            "from anything else, and guessing it is not acceptable. Nothing was submitted."
        )
    amount = to_broker_decimal(value, field_name)
    if amount <= 0:
        raise AssetNotTradableError(
            f"The broker reported a non-positive {field_name} ({amount}). Nothing was submitted."
        )
    return amount


def fetch_crypto_asset(client: TradingClient, symbol: str) -> CryptoAssetSpec:
    """Read one crypto asset's live broker metadata, or fail closed.

    Every constraint an order depends on is read here rather than remembered:
    the asset must be crypto (not an equity and not a perpetual future), active,
    tradable, and fractionable, and it must report both a minimum order size and
    a trade increment. Anything missing or contradictory raises.
    """
    ticker = normalize_symbol(symbol)
    try:
        asset = client.get_asset(ticker)
    except APIError as error:
        raise AssetNotTradableError(
            f"Could not read broker metadata for {ticker}: {_api_error_text(error)}. "
            "Nothing was submitted."
        ) from None
    except Exception as error:  # noqa: BLE001 - any failure here must fail closed
        raise AssetNotTradableError(
            f"Could not read broker metadata for {ticker}: {type(error).__name__}. "
            "Nothing was submitted."
        ) from None

    if not isinstance(asset, Asset):
        raise AssetNotTradableError(
            f"The broker returned {ticker} asset metadata in an unexpected shape. "
            "Nothing was submitted."
        )

    asset_class = (
        asset.asset_class.value if isinstance(asset.asset_class, Enum) else str(asset.asset_class)
    )
    status = asset.status.value if isinstance(asset.status, Enum) else str(asset.status)

    if asset_class != AssetClass.CRYPTO.value:
        raise AssetNotTradableError(
            f"{ticker} is reported as asset class {asset_class!r}, not "
            f"{AssetClass.CRYPTO.value!r}. This system trades crypto spot only. "
            "Nothing was submitted."
        )
    if status != AssetStatus.ACTIVE.value:
        raise AssetNotTradableError(
            f"{ticker} is {status!r} at the broker, not {AssetStatus.ACTIVE.value!r}. "
            "Nothing was submitted."
        )
    if not asset.tradable:
        raise AssetNotTradableError(
            f"{ticker} is not tradable at the broker. Nothing was submitted."
        )
    if not asset.fractionable:
        raise AssetNotTradableError(
            f"The broker reports {ticker} as not fractionable, which contradicts the "
            "fractional sizing this system depends on. Nothing was submitted."
        )

    min_order_size = _require_positive_decimal(asset.min_order_size, f"{ticker} min_order_size")
    min_trade_increment = _require_positive_decimal(
        asset.min_trade_increment, f"{ticker} min_trade_increment"
    )
    return CryptoAssetSpec(
        symbol=ticker,
        asset_class=asset_class,
        status=status,
        tradable=bool(asset.tradable),
        fractionable=bool(asset.fractionable),
        min_order_size=min_order_size,
        min_trade_increment=min_trade_increment,
    )


# --------------------------------------------------------------------------
# The broker's order minimums
#
# Two floors apply, and an order has to clear both:
#
#   1. `Asset.min_order_size`, read live from the broker on every attempt; and
#   2. Alpaca's USD minimum cost basis, `USD_MINIMUM_ORDER_NOTIONAL`, which the
#      broker enforces but does not report.
#
# `effective_minimum_quantity` is the one place the two are combined, and the
# only expression of the rule anywhere in this repository.
#
# Every number here is a `Decimal`. A binary float would make a threshold that
# decides whether a real order is sent depend on a rounding artefact.
# --------------------------------------------------------------------------


def is_usd_quoted(symbol: str) -> bool:
    """Whether `symbol` is a USD-quoted pair, so the USD notional rule applies.

    The quote currency, not the base: ``BTC/USD`` is USD-quoted, ``BTC/USDT``
    is not. Alpaca states a different minimum for its BTC, ETH, and USDT pairs,
    and applying a $10 floor to one of those would refuse orders the broker
    would have taken.
    """
    _, separator, quote = str(symbol).strip().upper().partition("/")
    return bool(separator) and quote == USD_QUOTE_CURRENCY


def reference_price_decimal(reference_price: float) -> Decimal:
    """The reference price as an exact `Decimal`, or raise.

    Validated here rather than trusted, because this value divides into a
    threshold that decides whether an order is sent: a zero, a negative, a NaN,
    or an infinity would each produce a confidently wrong floor - and a NaN in
    particular would compare False against every check and read as a pass.

    Converted through the float's shortest round-tripping decimal form, so
    ``77722.3`` is 77722.3 rather than the binary value a hair away from it.
    """
    try:
        price = require_reference_price(reference_price, "reference_price")
    except ExecutionInputError as error:
        raise ReferencePriceUnavailableError(
            f"Cannot check the broker's minimum order value without a usable current "
            f"price ({error}). Nothing was submitted."
        ) from None
    return Decimal(str(price))


def minimum_quantity_from_notional(
    asset: CryptoAssetSpec,
    *,
    reference_price: float,
    notional: Decimal = USD_MINIMUM_ORDER_NOTIONAL,
) -> Decimal:
    """The smallest quantity of `asset` worth at least `notional` USD.

    ``notional / reference_price``, rounded **UP** to the next whole trade
    increment. Up, because this is a *threshold*: rounding it down would
    produce a floor that itself buys less than $10, which is the bug this
    function exists to prevent.

    Rounding a threshold up and rounding a submitted quantity down are
    different operations on different values, and this module does both. This
    one computes a number an order is *compared against*; it never becomes an
    order quantity, so nothing here can enlarge what risk approved.
    """
    price = reference_price_decimal(reference_price)
    increment = _require_positive_decimal(asset.min_trade_increment, "min_trade_increment")
    with localcontext() as context:
        context.prec = _THRESHOLD_PRECISION
        steps = (notional / (price * increment)).to_integral_value(rounding=ROUND_CEILING)
        minimum = steps * increment
        # The ceiling above is exact at this precision. The check is kept
        # anyway, because "the threshold is at least the floor it was derived
        # from" is the property that matters, and asserting it costs nothing.
        while minimum * price < notional:  # pragma: no cover - exact at _THRESHOLD_PRECISION
            steps += 1
            minimum = steps * increment
    return +minimum


def effective_minimum_quantity(asset: CryptoAssetSpec, *, reference_price: float) -> Decimal:
    """The smallest quantity of `asset` the broker will actually accept.

    The **larger** of the two floors - the asset's own `min_order_size` and the
    quantity worth `USD_MINIMUM_ORDER_NOTIONAL` - because an order has to clear
    both and clearing only one is what produced the defect this encodes.

    For a pair that is not USD-quoted the notional rule does not apply and the
    asset's metadata stands alone; see `is_usd_quoted`.
    """
    floor = _require_positive_decimal(asset.min_order_size, "min_order_size")
    if not is_usd_quoted(asset.symbol):
        return floor
    return max(floor, minimum_quantity_from_notional(asset, reference_price=reference_price))


def require_minimum_notional(
    quantity: Decimal, asset: CryptoAssetSpec, *, reference_price: float
) -> Decimal:
    """Raise unless `quantity` is worth at least the broker's USD minimum.

    Returns the effective minimum when the quantity clears it, so a caller that
    wants to report the threshold does not recompute it.

    This applies to **both sides**. Alpaca documents the USD minimum without a
    side distinction, and their staff describe the cost-basis check as covering
    buy orders, sell orders, and limit orders alike, so a SELL below $10 is
    refused by the endpoint exactly as a BUY is. Enforcing it locally on both
    sides does not strand anything the broker would have accepted - it turns a
    broker rejection into a local one, with zero broker calls and a message
    that says what to do. It does mean a position whose value falls below $10
    cannot be closed until it recovers or is topped up, which is Alpaca's
    constraint rather than this system's, and is why an opening order should be
    sized with room above the floor rather than at it.
    """
    minimum = effective_minimum_quantity(asset, reference_price=reference_price)
    if quantity >= minimum:
        return minimum
    price = reference_price_decimal(reference_price)
    raise MinimumNotionalError(
        f"{format_quantity(quantity)} {asset.symbol} is worth about "
        f"${format_quantity((quantity * price).quantize(Decimal('0.01'), rounding=ROUND_FLOOR))} "
        f"at {price}, below the broker's minimum order value of "
        f"${format_quantity(USD_MINIMUM_ORDER_NOTIONAL)}. The smallest quantity it "
        f"would accept right now is {format_quantity(minimum)} {asset.symbol}. No "
        "order was submitted and no broker request was made: the quantity is not "
        "raised to clear the minimum, because that would send more than risk "
        "approved. Request a larger quantity."
    )


def normalize_broker_quantity(
    quantity: Decimal, asset: CryptoAssetSpec, *, reference_price: float
) -> Decimal:
    """Round `quantity` **down** to the asset's trade increment, or refuse it.

    Down, always. Rounding up would send more than the risk engine approved,
    which is the one direction this boundary may never move in. If the result
    lands below either floor the broker enforces - its `min_order_size`, or the
    quantity worth `USD_MINIMUM_ORDER_NOTIONAL` - there is no valid order to
    place, and that is reported rather than papered over by rounding back up.

    `reference_price` is required rather than optional so that the value that
    goes to the broker cannot be produced without the price its minimum is
    measured in. There is no way to normalize a quantity here and skip the
    check.
    """
    amount = require_quantity(quantity, "quantity")
    steps = (amount / asset.min_trade_increment).to_integral_value(rounding=ROUND_FLOOR)
    normalized = (steps * asset.min_trade_increment).quantize(
        asset.min_trade_increment, rounding=ROUND_FLOOR
    )
    if normalized > amount:  # pragma: no cover - ROUND_FLOOR cannot exceed the input
        raise QuantityBelowMinimumError(
            "Quantity normalization increased the order size, which is never allowed."
        )
    if normalized < asset.min_order_size or normalized <= 0:
        raise QuantityBelowMinimumError(
            f"{format_quantity(amount)} {asset.symbol} normalizes down to "
            f"{format_quantity(normalized)} at the broker's trade increment of "
            f"{format_quantity(asset.min_trade_increment)}, which is below its minimum "
            f"order size of {format_quantity(asset.min_order_size)}. No order was "
            "submitted: rounding up would exceed what risk approved."
        )
    require_minimum_notional(normalized, asset, reference_price=reference_price)
    return normalized


def to_wire_quantity(quantity: Decimal) -> float:
    """Render an exact quantity as the float the installed SDK's request takes.

    `MarketOrderRequest.qty` is typed as a float, so the exact Decimal has to
    become one somewhere. It becomes one here, and only after checking that the
    value the broker will actually receive - the float's shortest round-tripping
    decimal form, which is what JSON serialization emits - is not *larger* than
    the approved quantity. If a conversion artefact pushed it up, the value
    steps down to the next representable float instead. A quantity may shrink
    on the way to the broker; it may never grow.
    """
    amount = require_quantity(quantity, "quantity")
    value = float(amount)
    while value > 0 and Decimal(repr(value)) > amount:  # pragma: no branch
        value = math.nextafter(value, 0.0)
    if value <= 0:
        raise QuantityBelowMinimumError(
            f"{format_quantity(amount)} cannot be represented as a positive order "
            "quantity. No order was submitted."
        )
    return value


def fetch_reference_price(client: CryptoHistoricalDataClient, symbol: str) -> float:
    """Return the latest crypto trade price for `symbol`.

    This is the *current* market price, not a stored historical bar: sizing a
    live order against yesterday's Parquet close would be wrong. A price that
    cannot be obtained, or that is not finite and positive, fails closed - no
    order is sized or submitted.
    """
    ticker = normalize_symbol(symbol)
    request = CryptoLatestTradeRequest(symbol_or_symbols=ticker)
    try:
        latest = client.get_crypto_latest_trade(request, feed=REFERENCE_PRICE_FEED)
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

    trade = _select_by_symbol(latest, ticker)
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


def _select_by_symbol(payload: object, ticker: str) -> object | None:
    """Pull one symbol's entry out of a keyed market-data response.

    The provider keys the mapping by its own spelling of the pair, which is not
    guaranteed to be the canonical one, so a direct hit is tried first and a
    `broker_symbol_key` match second.
    """
    if not hasattr(payload, "get"):
        return None
    direct = payload.get(ticker)  # type: ignore[union-attr]
    if direct is not None:
        return direct
    wanted = broker_symbol_key(ticker)
    items = payload.items() if hasattr(payload, "items") else ()
    for key, value in items:
        if broker_symbol_key(str(key)) == wanted:
            return value
    return None


# --------------------------------------------------------------------------
# Risk context
# --------------------------------------------------------------------------


def resolve_daily_baseline_equity(
    connection: sqlite3.Connection, *, equity: float, now: datetime
) -> Decimal:
    """Return the equity baseline the UTC risk day is measured against.

    The first equity observed on a UTC calendar date establishes that date's
    baseline durably; every later check on the same date reuses it, so the
    daily-loss halt survives a process restart and cannot be reset by
    re-running a command.

    **Honest limitation.** This is the first equity this system *observed* on
    the date, not the equity at exactly 00:00 UTC. A day whose first
    observation is at 14:00 UTC is measured from 14:00 UTC. The stored
    `captured_at` records how close that was; the C9 runner, running
    continuously, is what makes the first observation land near the boundary.
    """
    baseline = state.ensure_daily_risk_baseline(
        connection,
        risk_date_utc=state.utc_risk_date(now),
        baseline_equity=Decimal(str(equity)),
        captured_at=now,
    )
    return baseline.baseline_equity


def build_risk_context(
    account: PaperAccountState,
    positions: dict[str, PaperPosition],
    symbol: str,
    *,
    daily_baseline_equity: Decimal,
    trading_enabled: bool = True,
) -> RiskContext:
    """Map current paper broker state onto the risk engine's account context.

    The mapping, field by field:

    ==========================  ====================================================
    `RiskContext`               Source
    ==========================  ====================================================
    `equity`                    `TradeAccount.equity`
    `cash`                      `TradeAccount.cash`
    `start_of_day_equity`       the stored UTC-day baseline
    `daily_pnl`                 `equity - baseline`
    `total_exposure`            sum of positive **long** position market values
    `symbol_exposure`           that pair's long market value, else 0
    `current_position_quantity` that pair's long quantity, else 0
    `trading_enabled`           caller-supplied kill switch
    ==========================  ====================================================

    `total_exposure` is summed from the positions themselves rather than read
    from `long_market_value`, so the total and the per-symbol figure it must
    contain always come from one source and cannot disagree.

    The daily baseline is the **UTC-day** figure, never `TradeAccount.last_equity`:
    that field is an equity-session previous close, and a market that never
    closes does not have one.

    `trading_enabled` is the risk engine's kill switch and is a parameter, not
    an environment variable: the operational off switch for this milestone is
    the submission gate, and a second env-driven switch would make it ambiguous
    which one stopped a trade. Turning it off blocks new entries while still
    permitting a risk-reducing exit.

    The symbol is checked against the whole tradable universe rather than the
    crypto pairs alone, because this mapping is the same arithmetic for either
    product: one account, one equity figure, one cash figure, and one total
    exposure summed over every position the account actually holds. Narrowing
    to a product happens at the execution boundary that owns it, not here - and
    an equity book that this function refused to look at would be an equity
    book the total-exposure cap silently ignored.
    """
    ticker = normalize_tradable_symbol(symbol)
    total_exposure = sum(
        position.market_value for position in positions.values() if position.market_value > 0
    )
    held = positions.get(broker_symbol_key(ticker))
    symbol_exposure = max(0.0, held.market_value) if held is not None else 0.0
    baseline = float(daily_baseline_equity)
    return RiskContext(
        equity=account.equity,
        cash=max(0.0, account.cash),
        total_exposure=total_exposure,
        symbol_exposure=symbol_exposure,
        current_position_quantity=held.quantity if held is not None else _ZERO,
        daily_pnl=account.equity - baseline,
        start_of_day_equity=baseline,
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

    Always a MARKET order, `ORDER_TIME_IN_FORCE` (GTC), and carrying the
    intent's `client_order_id`. `notional` is never set - a notional order
    would be sized in dollars by the broker rather than by the risk engine,
    which would put sizing outside this system's control. `extended_hours` is
    not set either: it is an equity-session flag with no meaning for a market
    that never closes.

    The quantity is `approved_quantity` - risk's number after rounding down to
    the broker's own trade increment. The requested quantity is deliberately
    not reachable from here.
    """
    return MarketOrderRequest(
        symbol=intent.symbol,
        qty=to_wire_quantity(intent.approved_quantity),
        side=AlpacaOrderSide.BUY if intent.side is OrderSide.BUY else AlpacaOrderSide.SELL,
        time_in_force=ORDER_TIME_IN_FORCE,
        client_order_id=intent.client_order_id,
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
    """Normalize an Alpaca order into the snapshot this milestone stores."""
    status = order.status.value if isinstance(order.status, Enum) else str(order.status)
    side = order.side.value.upper() if isinstance(order.side, Enum) else str(order.side).upper()
    filled = order.filled_qty if order.filled_qty is not None else 0
    return BrokerOrderSnapshot(
        broker_order_id=str(order.id),
        client_order_id=str(order.client_order_id),
        symbol=str(order.symbol),
        side=side,
        quantity=to_broker_decimal(order.qty, "order quantity"),
        filled_quantity=to_broker_decimal(filled, "filled quantity"),
        filled_average_price=_optional_float(order.filled_avg_price),
        status=status,
        submitted_at=order.submitted_at,
        filled_at=order.filled_at,
        broker_updated_at=order.updated_at,
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
    requested_quantity: Decimal
    reference_price: float
    risk_decision: RiskDecision
    account: PaperAccountState
    daily_baseline_equity: Decimal
    message: str
    asset: CryptoAssetSpec | None = None
    effective_minimum_quantity: Decimal | None = None
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

    @property
    def submitted_quantity(self) -> Decimal | None:
        """The exact quantity this attempt would send, once one exists."""
        return None if self.intent is None else self.intent.approved_quantity


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
    build_request: Callable[[OrderIntent], MarketOrderRequest] | None = None,
) -> SubmissionResult:
    """Submit one already-persisted intent, exactly once.

    The intent **must** already be committed, and that is now checked rather
    than assumed: this function is the point of no return, and the caller's
    durable `client_order_id` is the only thing that makes the outcome
    recoverable. An open transaction on `connection` means the intent has not
    been committed - `record_order_intent` joins an enclosing transaction
    instead of committing inside it - so the attempt is refused before the
    broker is touched at all.

    A duplicate preflight runs first. If the broker already has an order under
    this key, that order is recorded and returned and **nothing is submitted**.

    `submit_order` is called at most once. There is no retry, no backoff, and
    no second attempt under any circumstances:

    - a returned order that can be read and stored marks the intent
      `SUBMITTED`;
    - a definite broker rejection marks it `REJECTED` and raises
      `BrokerRejectedOrderError`;
    - anything ambiguous marks it `UNKNOWN`, writes an audit event, and raises
      `AmbiguousSubmissionError`.

    "Anything ambiguous" includes both halves of the response, not just the
    call: once `submit_order` has been entered a request has gone out, so a
    reply that cannot be parsed and a reply that cannot be written to disk are
    both states in which an order may exist and this process does not know what
    it is. Reporting either as an ordinary failure would let the caller carry
    on trading over an order it never recorded.

    The `client_order_id` is never regenerated, so every ambiguous case stays
    resolvable by asking the broker about that exact key.

    `build_request` translates the intent into the broker payload and defaults
    to this module's crypto form - MARKET, GTC, no extended-hours flag. It is a
    parameter so that the equity boundary can supply its own translation
    (MARKET, DAY, regular hours) without a second copy of the ordering,
    duplicate-preflight, one-attempt and never-retry logic below, which is
    where every safety property of a submission actually lives.
    """
    if connection.in_transaction:
        raise NonDurableIntentError(
            f"The order intent for client_order_id {intent.client_order_id} is not "
            "committed: this connection is inside an open transaction, so the intent "
            "is invisible to any other process and would be rolled back by a crash. "
            "Refusing to submit an order whose recovery anchor is not durable. "
            "Nothing was submitted."
        )

    translate = build_request if build_request is not None else build_market_order_request
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

    request = translate(intent)
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

    # Past this line a request has definitely reached the broker. Every failure
    # from here on is therefore ambiguous rather than merely unfortunate: the
    # order may exist, and the honest record of "it may exist and I could not
    # find out what it is" is UNKNOWN under the same client_order_id.
    try:
        snapshot = _to_snapshot(order)
    except Exception as error:  # noqa: BLE001 - an unreadable reply is not a refusal
        raise _mark_unknown(
            connection,
            intent,
            order_intent_id,
            now,
            f"the broker's reply could not be read ({type(error).__name__}: {error})",
        ) from None

    try:
        _persist_broker_snapshot(
            connection,
            order_intent_id=order_intent_id,
            snapshot=snapshot,
            status=state.INTENT_STATUS_SUBMITTED,
            now=now,
        )
    except Exception as error:  # noqa: BLE001 - an unstorable reply is not a refusal
        # `_mark_unknown` writes too, so a database that is broken rather than
        # briefly busy will raise from in there instead. That is the correct
        # outcome as well: it is fatal, and a runtime that cannot record what it
        # did must stop rather than continue.
        raise _mark_unknown(
            connection,
            intent,
            order_intent_id,
            now,
            f"the broker's reply could not be stored ({type(error).__name__}: {error})",
        ) from None

    state.record_system_event(
        connection,
        event_timestamp=now,
        event_type=EVENT_SUBMITTED,
        message=(
            f"Paper order {snapshot.broker_order_id} accepted for client_order_id "
            f"{intent.client_order_id} ({snapshot.side} "
            f"{format_quantity(snapshot.quantity)} {snapshot.symbol}, broker status "
            f"{snapshot.status})."
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
    requested_quantity: Decimal,
    trading_client: TradingClient | None = None,
    data_client: CryptoHistoricalDataClient | None = None,
    dry_run: bool = False,
    trading_enabled: bool = True,
    strategy_run_id: int | None = None,
    now: datetime | None = None,
) -> PaperExecutionResult:
    """Run the full paper execution pipeline for one crypto order.

    The order of operations is the safety contract:

    1. validate the request against this milestone's scope;
    2. read the paper account and refuse a non-tradable one;
    3. read positions;
    4. read the asset's live broker metadata, failing closed without it;
    5. read the **current** crypto reference price, failing closed without one;
    6. resolve the durable UTC-day equity baseline;
    7. evaluate risk against the real account state;
    8. persist the risk decision;
    9. stop here if risk refused - no intent, no broker request;
    10. round the approved quantity **down** to the broker's trade increment,
        refusing it if that lands below the broker's minimum;
    11. stop here if this is a dry run - nothing is persisted or sent;
    12. create the intent with its `client_order_id` and **commit** it;
    13. preflight for a duplicate, failing closed if the check cannot complete;
    14. submit exactly once;
    15. persist whatever the broker said.

    Steps 12 and 14 are in that order deliberately: a crash between them leaves
    a durable key that Phase 8 can resolve, whereas submitting first would
    leave a real order with no local trace.

    The broker quantity is never more than `RiskDecision.approved_quantity`.
    If risk clamps a request to 0.05 BTC, the broker is asked for 0.05 BTC or
    the largest whole multiple of its trade increment below that - never more.

    Returns a `PaperExecutionResult`. Expected operational failures - a closed
    gate, missing credentials, an untradable account or asset, no price, a
    quantity below the broker's minimum, an incompletable duplicate check, a
    broker rejection, an ambiguous outcome - raise an `ExecutionError` subclass
    rather than returning quietly.
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
    asset = fetch_crypto_asset(client, ticker)
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

    # The observed position is recorded from what the broker actually reports,
    # before anything is submitted. Nothing later in this function updates it:
    # an accepted order is not a fill, and inferring a position from one would
    # be a fabrication. Phase 8 reconciles this table properly.
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

    common = {
        "symbol": ticker,
        "side": order_side,
        "requested_quantity": quantity,
        "reference_price": reference_price,
        "risk_decision": decision,
        "account": account,
        "daily_baseline_equity": baseline_equity,
        "asset": asset,
        # Reported on every outcome, including a risk rejection: an operator
        # sizing the next attempt needs the threshold whether or not this
        # attempt got as far as being measured against it.
        "effective_minimum_quantity": effective_minimum_quantity(
            asset, reference_price=reference_price
        ),
    }

    if not decision.approved:
        return PaperExecutionResult(
            outcome=ExecutionOutcome.REJECTED_BY_RISK,
            message=decision.message,
            **common,
        )

    broker_quantity = normalize_broker_quantity(
        decision.approved_quantity, asset, reference_price=reference_price
    )

    intent = OrderIntent(
        symbol=ticker,
        side=order_side,
        requested_quantity=quantity,
        approved_quantity=broker_quantity,
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
    "PAPER_TRADING_BASE_URL",
    "EVENT_DUPLICATE",
    "EVENT_REJECTED",
    "EVENT_SUBMITTED",
    "EVENT_UNKNOWN",
    "ORDER_TIME_IN_FORCE",
    "PAPER_TRADING_ENABLED_ENV",
    "PAPER_TRADING_ENABLED_VALUE",
    "REFERENCE_PRICE_FEED",
    "USD_MINIMUM_ORDER_NOTIONAL",
    "USD_QUOTE_CURRENCY",
    "TRADABLE_ACCOUNT_STATUSES",
    "AccountNotTradableError",
    "AmbiguousSubmissionError",
    "AssetNotTradableError",
    "BrokerOrderSnapshot",
    "BrokerRejectedOrderError",
    "ConfirmationRequiredError",
    "CryptoAssetSpec",
    "DuplicatePreflightUnavailableError",
    "ExecutionOutcome",
    "MinimumNotionalError",
    "MissingCredentialsError",
    "NonDurableIntentError",
    "NotPaperEnvironmentError",
    "PaperAccountState",
    "PaperExecutionResult",
    "PaperPosition",
    "PaperTradingDisabledError",
    "QuantityBelowMinimumError",
    "ReferencePriceUnavailableError",
    "SubmissionResult",
    "UnsupportedBrokerStateError",
    "broker_symbol_key",
    "build_market_order_request",
    "build_risk_context",
    "create_market_data_client",
    "create_paper_trading_client",
    "credentials_configured",
    "effective_minimum_quantity",
    "execute_paper_order",
    "fetch_crypto_asset",
    "fetch_paper_account_state",
    "fetch_paper_positions",
    "fetch_reference_price",
    "find_broker_order_by_client_id",
    "is_usd_quoted",
    "minimum_quantity_from_notional",
    "normalize_broker_quantity",
    "paper_trading_enabled",
    "reference_price_decimal",
    "require_confirmation",
    "require_minimum_notional",
    "require_paper_trading_enabled",
    "require_tradable_account",
    "resolve_daily_baseline_equity",
    "submit_order_intent",
    "to_broker_decimal",
    "to_wire_quantity",
    "verify_paper_environment",
]
