"""The value types the accounting ledger is written in.

Every quantity, price and money amount here is a `Decimal`. Nothing in this
package accepts a `float` for an authoritative figure, because a cost basis
that drifts in the tenth decimal place is a cost basis that will eventually
disagree with the broker and there will be no way to say which side moved.

The types are frozen. A realized event is a statement about something that
already happened at the broker; a mutable one would be an invitation to
"correct" history in place, which is the failure this package exists to make
impossible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

# --------------------------------------------------------------------------
# Vocabulary
#
# Machine strings. Labels on a screen may be reworded; these may not, because
# they are written into the database and compared on the way back out.
# --------------------------------------------------------------------------

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"
SIDES: tuple[str, ...] = (SIDE_BUY, SIDE_SELL)

#: How much of the execution the broker actually told us about.
#:
#: `EXECUTION` - one row per individual execution, with that execution's own
#: quantity and price. `AGGREGATED_ORDER` - the broker would only say what the
#: whole order came to, so one row stands for an unknown number of executions
#: at an unknown spread of prices. The distinction is stored on every fill
#: rather than assumed globally, so a ledger that mixes the two says so.
GRANULARITY_EXECUTION = "EXECUTION"
GRANULARITY_AGGREGATED_ORDER = "AGGREGATED_BROKER_FILL"
GRANULARITIES: tuple[str, ...] = (GRANULARITY_EXECUTION, GRANULARITY_AGGREGATED_ORDER)

#: Where the order behind an execution came from. Provenance is a *claim about
#: evidence*, not a guess: `EQUITY_RUNTIME` means the order key was found in
#: the equity paper runtime's own store, `MANUAL_OPERATOR` means it was minted
#: by this system's tooling but is in no runtime store, and `UNKNOWN_EXTERNAL`
#: means neither could be established. Nothing is attributed to a strategy on
#: the strength of its symbol.
PROVENANCE_EQUITY_RUNTIME = "EQUITY_RUNTIME"
PROVENANCE_MANUAL_OPERATOR = "MANUAL_OPERATOR"
PROVENANCE_MIGRATION = "MIGRATION"
PROVENANCE_UNKNOWN_EXTERNAL = "UNKNOWN_EXTERNAL"
PROVENANCES: tuple[str, ...] = (
    PROVENANCE_EQUITY_RUNTIME,
    PROVENANCE_MANUAL_OPERATOR,
    PROVENANCE_MIGRATION,
    PROVENANCE_UNKNOWN_EXTERNAL,
)

#: Per-symbol accounting state.
#:
#: `TRACKING` - the ledger believes it knows this symbol's inventory.
#: `ACCOUNTING_MISMATCH` - it does not, and has stopped applying events to it.
#: A symbol never leaves `ACCOUNTING_MISMATCH` by accident: only an explicit,
#: audited repair can move it back.
STATUS_TRACKING = "TRACKING"
STATUS_MISMATCH = "ACCOUNTING_MISMATCH"
STATUSES: tuple[str, ...] = (STATUS_TRACKING, STATUS_MISMATCH)

#: How complete the ledger's history is. Written once, at bootstrap.
COMPLETENESS_EXACT_REPLAY = "EXACT_REPLAY"
COMPLETENESS_CUTOVER = "CUTOVER"
COMPLETENESS_VALUES: tuple[str, ...] = (COMPLETENESS_EXACT_REPLAY, COMPLETENESS_CUTOVER)

#: The accounting method. One value today; named so a second one could never
#: be introduced silently.
BASIS_WEIGHTED_AVERAGE = "WEIGHTED_AVERAGE_COST"

#: Bumped when the *meaning* of a stored realized event changes. Every realized
#: row carries the version that produced it, so a ledger written under two
#: versions can still be read.
ACCOUNTING_VERSION = 1


class AccountingError(Exception):
    """Base for every refusal in this package."""


class AccountingInputError(AccountingError):
    """A value was not something the ledger can be written in."""


class NegativeInventoryError(AccountingError):
    """A sale would drive local inventory below zero.

    Long-only means this is not a small discrepancy to absorb - it is a
    statement that the ledger's picture of the position is wrong. The engine
    refuses, and the caller marks the symbol `ACCOUNTING_MISMATCH`.
    """


class SymbolNotTrackedError(AccountingError):
    """An event was offered for a symbol whose accounting has been stopped."""


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------


def _require_decimal(value: object, field: str, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise AccountingInputError(
            f"{field} must be a Decimal, not {type(value).__name__}. Binary floating "
            "point is not accepted for an authoritative accounting figure."
        )
    if not value.is_finite():
        raise AccountingInputError(f"{field} must be finite; got {value}.")
    if value < 0 or (value == 0 and not allow_zero):
        bound = "zero or greater" if allow_zero else "greater than zero"
        raise AccountingInputError(f"{field} must be {bound}; got {value}.")
    return value


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AccountingInputError(f"{field} must be a non-empty string.")
    return value.strip()


def _require_member(value: object, field: str, allowed: tuple[str, ...]) -> str:
    text = _require_text(value, field)
    if text not in allowed:
        raise AccountingInputError(f"{field} must be one of {allowed}; got {text!r}.")
    return text


def _require_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise AccountingInputError(f"{field} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise AccountingInputError(
            f"{field} must be timezone-aware. A naive timestamp on a ledger that "
            "reports a UTC trading day is an ambiguity, not a convenience."
        )
    return value


# --------------------------------------------------------------------------
# Source events
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionFill:
    """One broker-confirmed execution. The only thing that may move the ledger.

    An intent, a submitted order, a pending order, a requested quantity and a
    simulated observer action are none of them this type, and there is no
    constructor here that turns one into it.

    `execution_id` is the idempotency identity: applying the same
    `execution_id` twice is a no-op, not a doubled position.
    """

    execution_id: str
    order_id: str
    symbol: str
    asset_class: str
    side: str
    quantity: Decimal
    price: Decimal
    executed_at: datetime
    granularity: str = GRANULARITY_EXECUTION
    provenance: str = PROVENANCE_UNKNOWN_EXTERNAL
    fees: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_id", _require_text(self.execution_id, "execution_id"))
        object.__setattr__(self, "order_id", _require_text(self.order_id, "order_id"))
        object.__setattr__(self, "symbol", _require_text(self.symbol, "symbol"))
        object.__setattr__(self, "asset_class", _require_text(self.asset_class, "asset_class"))
        object.__setattr__(self, "side", _require_member(self.side, "side", SIDES))
        object.__setattr__(self, "quantity", _require_decimal(self.quantity, "quantity"))
        object.__setattr__(self, "price", _require_decimal(self.price, "price"))
        object.__setattr__(self, "executed_at", _require_utc(self.executed_at, "executed_at"))
        object.__setattr__(
            self, "granularity", _require_member(self.granularity, "granularity", GRANULARITIES)
        )
        object.__setattr__(
            self, "provenance", _require_member(self.provenance, "provenance", PROVENANCES)
        )
        object.__setattr__(self, "fees", _require_decimal(self.fees, "fees", allow_zero=True))

    @property
    def gross_notional(self) -> Decimal:
        """Quantity times price. Exact - one multiplication, never rounded."""
        return self.quantity * self.price


# --------------------------------------------------------------------------
# Derived state
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CostBasisState:
    """What the ledger currently believes about one symbol.

    `total_cost_basis` is the authoritative number and `average_cost` is
    derived from it, not the other way round. That ordering is deliberate: a
    purchase is then exactly additive and introduces no rounding at all, and
    the single division in the whole engine happens on a sale, where it can be
    stated and audited.
    """

    symbol: str
    quantity: Decimal
    total_cost_basis: Decimal
    status: str = STATUS_TRACKING
    last_execution_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _require_text(self.symbol, "symbol"))
        object.__setattr__(
            self, "quantity", _require_decimal(self.quantity, "quantity", allow_zero=True)
        )
        object.__setattr__(
            self,
            "total_cost_basis",
            _require_decimal(self.total_cost_basis, "total_cost_basis", allow_zero=True),
        )
        object.__setattr__(self, "status", _require_member(self.status, "status", STATUSES))
        if self.quantity == 0 and self.total_cost_basis != 0:
            raise AccountingInputError(
                f"{self.symbol}: a flat position cannot carry a cost basis "
                f"({self.total_cost_basis})."
            )

    @classmethod
    def flat(cls, symbol: str) -> CostBasisState:
        """The state every symbol starts in: no shares, no basis, tracking."""
        return cls(symbol=symbol, quantity=Decimal(0), total_cost_basis=Decimal(0))

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    @property
    def tracking(self) -> bool:
        return self.status == STATUS_TRACKING


@dataclass(frozen=True)
class RealizedEvent:
    """A sale, and the profit or loss it released. Append-only, forever.

    Nothing on this record depends on a current market price, which is why it
    never needs revisiting: a realized event is finished the moment the sale
    executes, and a later quote cannot change it.
    """

    execution_id: str
    order_id: str
    symbol: str
    quantity: Decimal
    execution_price: Decimal
    average_cost_before: Decimal
    released_cost_basis: Decimal
    gross_proceeds: Decimal
    gross_realized_pnl: Decimal
    fees: Decimal
    net_realized_pnl: Decimal
    quantity_before: Decimal
    quantity_after: Decimal
    average_cost_after: Decimal | None
    realized_at: datetime
    provenance: str
    accounting_version: int = ACCOUNTING_VERSION


@dataclass(frozen=True)
class AppliedFill:
    """The engine's whole output: the new state, and the event if there was one.

    A purchase yields `realized is None`. That is not an omission - a purchase
    releases nothing, and manufacturing a zero-valued realized event for one
    would put a row in the ledger that never happened.
    """

    state: CostBasisState
    realized: RealizedEvent | None
    duplicate: bool = False


__all__ = [
    "ACCOUNTING_VERSION",
    "BASIS_WEIGHTED_AVERAGE",
    "COMPLETENESS_CUTOVER",
    "COMPLETENESS_EXACT_REPLAY",
    "COMPLETENESS_VALUES",
    "GRANULARITIES",
    "GRANULARITY_AGGREGATED_ORDER",
    "GRANULARITY_EXECUTION",
    "PROVENANCES",
    "PROVENANCE_EQUITY_RUNTIME",
    "PROVENANCE_MANUAL_OPERATOR",
    "PROVENANCE_MIGRATION",
    "PROVENANCE_UNKNOWN_EXTERNAL",
    "SIDES",
    "SIDE_BUY",
    "SIDE_SELL",
    "STATUSES",
    "STATUS_MISMATCH",
    "STATUS_TRACKING",
    "AccountingError",
    "AccountingInputError",
    "AppliedFill",
    "CostBasisState",
    "ExecutionFill",
    "NegativeInventoryError",
    "RealizedEvent",
    "SymbolNotTrackedError",
]
