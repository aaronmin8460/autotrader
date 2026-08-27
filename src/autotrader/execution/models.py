"""Phase 7 execution domain models. No broker SDK, no network, no database.

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
the anchor that lets a later phase ask the broker what became of a submission
this process did not live to see the answer to (docs/SPEC.md section 6E).

**Quantities are already risk-approved.** `approved_quantity` is the risk
engine's number and can never exceed `requested_quantity`. That invariant is
enforced here, again in the database, and again by the submission path, so an
order larger than risk allowed cannot be expressed at any layer.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

#: The frozen V0.1 universe (docs/SPEC.md section 3.1).
#:
#: Duplicated from `autotrader.data.historical` on purpose: this module is
#: stdlib-only by design, and importing the Phase 1 downloader would drag
#: pandas and the Alpaca data SDK into the domain layer. A test asserts the two
#: tuples are equal, so the duplication cannot silently drift.
SUPPORTED_SYMBOLS: tuple[str, ...] = ("SPY", "QQQ", "AAPL", "MSFT", "NVDA")

#: Every `client_order_id` this system generates starts with this. It makes an
#: order recognisably ours in the broker's UI and in Phase 8 reconciliation.
CLIENT_ORDER_ID_PREFIX = "autotrader-"

#: Upper bound on a generated or supplied `client_order_id`. Alpaca documents
#: a 128-character limit; ours are `prefix + uuid4` and land far below it.
MAX_CLIENT_ORDER_ID_LENGTH = 128


class ExecutionError(Exception):
    """Base class for every controlled Phase 7 failure.

    The CLI reports these as a concise message rather than a traceback: they
    describe an operational situation - a closed gate, a missing credential, a
    refusing broker - not a programming bug.
    """


class ExecutionInputError(ExecutionError):
    """A caller-supplied value is not a thing this phase can execute."""


class OrderSide(Enum):
    """The two sides an order may take.

    Kept distinct from `RiskSide` (a question about a hypothetical trade),
    from the backtester's `ExecutionSide` (a simulated fill), and from the
    Phase 3 `BUY`/`EXIT` signal vocabulary. Same words, different stages: a
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
    """Uppercase `symbol` and confirm it is one this phase may trade."""
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


def require_whole_share_quantity(value: int, field_name: str = "quantity") -> int:
    """Require a whole number of shares greater than zero.

    Fractional shares are out of scope, so a float is **refused rather than
    rounded**: silently turning 1.5 into 1 would execute an order nobody asked
    for. `bool` is refused too - it is an `int` subclass, and a flag reaching a
    share count is a type confusion, not a quantity of one.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionInputError(
            f"{field_name} must be a whole number of shares; fractional shares are out "
            f"of scope. Got {value!r}."
        )
    if value <= 0:
        raise ExecutionInputError(f"{field_name} must be greater than zero, got {value}.")
    return value


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

    `reference_price` is the mark the risk engine sized against - it is not a
    limit price and not a promised fill. `risk_reason_code` is the Phase 5 code
    that produced `approved_quantity`, kept so the audit trail can later show
    *why* an order was this size (`APPROVED`, or the constraint that clamped
    it).
    """

    symbol: str
    side: OrderSide
    requested_quantity: int
    approved_quantity: int
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
            require_whole_share_quantity(self.requested_quantity, "requested_quantity"),
        )
        object.__setattr__(
            self,
            "approved_quantity",
            require_whole_share_quantity(self.approved_quantity, "approved_quantity"),
        )
        object.__setattr__(self, "reference_price", require_reference_price(self.reference_price))
        object.__setattr__(self, "client_order_id", validate_client_order_id(self.client_order_id))

        if self.approved_quantity > self.requested_quantity:
            raise ExecutionInputError(
                f"approved_quantity ({self.approved_quantity}) must not exceed "
                f"requested_quantity ({self.requested_quantity}); risk may only ever "
                "size an order down."
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
    "new_client_order_id",
    "normalize_side",
    "normalize_symbol",
    "require_reference_price",
    "require_whole_share_quantity",
    "validate_client_order_id",
]
