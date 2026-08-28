"""C7 execution domain models. No broker SDK, no network, no database.

This module is the vocabulary the execution layer thinks in, and it is
deliberately **provider-neutral**: it imports only the standard library, and
it contains no Alpaca type, no HTTP call, and no SQL. An `OrderIntent` is not
an Alpaca request object and must never become one here - translating a domain
intent into a broker payload happens at the boundary, in `paper.py`, and
nowhere else. Keeping that translation in one file is what makes "this system
cannot trade live" checkable by reading a single module.

**An OrderIntent is a decision, not an order.** It records what this system
decided to do, at what size risk allowed, and under which idempotency key -
before any broker is contacted. Constructing one places nothing; it is inert
data. Its `client_order_id` is generated exactly once, at construction, and is
the anchor that lets reconciliation ask the broker what became of a submission
this process did not live to see the answer to (docs/SPEC.md section 6E).

**Quantities are exact Decimals, and already risk-approved.** Crypto is
fractionable, so a quantity is a `decimal.Decimal` amount of the base asset
rather than a share count. A `float` is refused rather than converted: a binary
float is an approximation, and an approximation is not a broker quantity.
`approved_quantity` can never exceed `requested_quantity`; that invariant is
enforced here, again in the database, and again by the submission path, so an
order larger than risk allowed cannot be expressed at any layer.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum

#: The frozen V0.2 pair universe (docs/SPEC.md section 3.1).
#:
#: Duplicated from `autotrader.data.historical` on purpose: this module is
#: stdlib-only by design, and importing the data layer would drag pandas and
#: the Alpaca data SDK into the domain layer. A test asserts the two tuples are
#: equal, so the duplication cannot silently drift.
SUPPORTED_SYMBOLS: tuple[str, ...] = ("BTC/USD", "ETH/USD")

#: Every `client_order_id` this system generates starts with this. It makes an
#: order recognisably ours in the broker's UI and in Phase 8 reconciliation.
CLIENT_ORDER_ID_PREFIX = "autotrader-"

#: Upper bound on a generated or supplied `client_order_id`. Alpaca documents
#: a 128-character limit; ours are `prefix + uuid4` and land far below it.
MAX_CLIENT_ORDER_ID_LENGTH = 128


class ExecutionError(Exception):
    """Base class for every controlled C7 failure.

    The CLI reports these as a concise message rather than a traceback: they
    describe an operational situation - a closed gate, a missing credential, a
    refusing broker - not a programming bug.
    """


class ExecutionInputError(ExecutionError):
    """A caller-supplied value is not a thing this milestone can execute."""


class OrderSide(Enum):
    """The two sides an order may take.

    Kept distinct from `RiskSide` (a question about a hypothetical trade),
    from the backtester's `ExecutionSide` (a simulated fill), and from the
    strategy's `BUY`/`EXIT` signal vocabulary. Same words, different stages: a
    signal is not a risk question, and a risk question is not an order.

    There is no short side, which is how long-only is enforced structurally -
    it cannot be requested.
    """

    BUY = "BUY"
    SELL = "SELL"


def new_client_order_id() -> str:
    """Mint one fresh idempotency key.

    A random UUID4 behind a fixed prefix. It carries **no** account
    information, no credential material, and no timestamp - it is an opaque
    identifier, and anything embedded in it would leak to the broker.

    Call this exactly once per intent. Regenerating a key for a submission
    whose outcome is unknown would strand the original at the broker and is
    precisely the duplicate-order bug the key exists to prevent.
    """
    return f"{CLIENT_ORDER_ID_PREFIX}{uuid.uuid4()}"


def validate_client_order_id(value: str) -> str:
    """Return `value` if it is a usable idempotency key, else raise."""
    if not isinstance(value, str) or not value.strip():
        raise ExecutionInputError("client_order_id must be a non-empty string.")
    if len(value) > MAX_CLIENT_ORDER_ID_LENGTH:
        raise ExecutionInputError(
            f"client_order_id must be at most {MAX_CLIENT_ORDER_ID_LENGTH} characters, "
            f"got {len(value)}."
        )
    return value


def normalize_symbol(symbol: str) -> str:
    """Uppercase `symbol` and confirm it is a pair this milestone may trade.

    Only the canonical pair form is accepted; `BTCUSD` is not silently
    rewritten as `BTC/USD`, because the slash is part of the broker's symbol.
    """
    if not isinstance(symbol, str):
        raise ExecutionInputError(f"symbol must be a string, got {type(symbol).__name__}.")
    normalized = symbol.strip().upper()
    if normalized not in SUPPORTED_SYMBOLS:
        raise ExecutionInputError(
            f"Unsupported symbol: {symbol!r}. Supported symbols are: "
            f"{', '.join(SUPPORTED_SYMBOLS)}."
        )
    return normalized


def normalize_side(side: str | OrderSide) -> OrderSide:
    """Coerce `side` to an `OrderSide`, rejecting anything else.

    Accepts the enum itself or its name in any case, so a CLI can pass `buy`.
    Anything outside `BUY`/`SELL` - notably any attempt to express a short -
    is refused.
    """
    if isinstance(side, OrderSide):
        return side
    if not isinstance(side, str):
        raise ExecutionInputError(f"side must be BUY or SELL, got {side!r}.")
    try:
        return OrderSide(side.strip().upper())
    except ValueError:
        raise ExecutionInputError(
            f"side must be one of {', '.join(member.value for member in OrderSide)}, got {side!r}."
        ) from None


def parse_quantity(value: str, field_name: str = "quantity") -> Decimal:
    """Parse a user-supplied quantity string into an exact `Decimal`.

    Text is parsed once, here, so a fractional quantity typed at the command
    line never passes through a binary float on its way to the broker: the
    string ``0.0001`` becomes exactly ``0.0001``, not the double nearest to it.
    """
    if isinstance(value, Decimal):
        return require_quantity(value, field_name)
    if not isinstance(value, str) or not value.strip():
        raise ExecutionInputError(f"{field_name} must be a decimal number, got {value!r}.")
    try:
        parsed = Decimal(value.strip())
    except InvalidOperation:
        raise ExecutionInputError(
            f"{field_name} must be a decimal number, got {value!r}."
        ) from None
    return require_quantity(parsed, field_name)


def require_quantity(value: Decimal, field_name: str = "quantity") -> Decimal:
    """Require an exact `Decimal` quantity greater than zero.

    Crypto quantities are fractional, so nothing is floored - but a `float` is
    **refused rather than converted**: a binary float is an approximation of the
    number the caller meant, and an approximation must never become the size of
    a real order. `bool` is refused too - it is an `int` subclass, and a flag
    reaching a quantity is a type confusion, not an amount of one.

    NaN and both infinities are rejected explicitly; either would compare False
    against every check and read as a passing one.
    """
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ExecutionInputError(
            f"{field_name} must be an exact Decimal quantity; a float is an "
            f"approximation, not a broker quantity. Got {value!r}."
        )
    quantity = Decimal(value)
    if not quantity.is_finite():
        raise ExecutionInputError(f"{field_name} must be finite, got {value!r}.")
    if quantity <= 0:
        raise ExecutionInputError(
            f"{field_name} must be greater than zero, got {format_quantity(quantity)}."
        )
    return quantity


def format_quantity(value: Decimal) -> str:
    """Render a quantity for humans: no exponent, no trailing-zero noise."""
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def require_reference_price(value: float, field_name: str = "reference_price") -> float:
    """Require a finite price greater than zero.

    NaN and both infinities are rejected explicitly. A NaN price would compare
    False against every limit and read as a passing check, and sizing against
    an infinite price is meaningless - either would produce a confidently
    wrong order quantity.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExecutionInputError(f"{field_name} must be a number, got {type(value).__name__}.")
    price = float(value)
    if not math.isfinite(price) or price <= 0:
        raise ExecutionInputError(
            f"{field_name} must be finite and greater than zero, got {value!r}."
        )
    return price


@dataclass(frozen=True)
class OrderIntent:
    """One order this system has decided to place, before any broker call.

    Frozen: an intent is a record of a decision already made, and its
    `client_order_id` in particular must never change once persisted.

    `approved_quantity` is the exact quantity that will be sent - the risk
    engine's number after normalization to the broker's own trade increment,
    never larger than either. `reference_price` is the USD mark risk sized
    against; it is not a limit price and not a promised fill.
    `risk_reason_code` is the code that produced the approval, kept so the
    audit trail can later show *why* an order was this size (`APPROVED`, or the
    constraint that clamped it).
    """

    symbol: str
    side: OrderSide
    requested_quantity: Decimal
    approved_quantity: Decimal
    reference_price: float
    risk_reason_code: str
    created_at: datetime
    client_order_id: str = field(default_factory=new_client_order_id)
    strategy_run_id: int | None = None

    def __post_init__(self) -> None:
        """Validate on construction, so a malformed intent cannot exist."""
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "side", normalize_side(self.side))
        object.__setattr__(
            self,
            "requested_quantity",
            require_quantity(self.requested_quantity, "requested_quantity"),
        )
        object.__setattr__(
            self,
            "approved_quantity",
            require_quantity(self.approved_quantity, "approved_quantity"),
        )
        object.__setattr__(self, "reference_price", require_reference_price(self.reference_price))
        object.__setattr__(self, "client_order_id", validate_client_order_id(self.client_order_id))

        if self.approved_quantity > self.requested_quantity:
            raise ExecutionInputError(
                f"approved_quantity ({format_quantity(self.approved_quantity)}) must not "
                f"exceed requested_quantity ({format_quantity(self.requested_quantity)}); "
                "risk may only ever size an order down."
            )
        if not isinstance(self.risk_reason_code, str) or not self.risk_reason_code.strip():
            raise ExecutionInputError("risk_reason_code must be a non-empty string.")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ExecutionInputError(
                "created_at must be a timezone-aware datetime; a naive one would be "
                "stored with a guessed offset and silently misdate the audit trail."
            )


__all__ = [
    "CLIENT_ORDER_ID_PREFIX",
    "MAX_CLIENT_ORDER_ID_LENGTH",
    "SUPPORTED_SYMBOLS",
    "ExecutionError",
    "ExecutionInputError",
    "OrderIntent",
    "OrderSide",
    "format_quantity",
    "new_client_order_id",
    "normalize_side",
    "normalize_symbol",
    "parse_quantity",
    "require_quantity",
    "require_reference_price",
    "validate_client_order_id",
]
