"""Shared-account allocation: what EDA-1 wants to hold, before Risk is asked.

The deep-architecture program validated EDA-1 as **ten independent equal-capital
sleeves** - $10,000 each, one position per sleeve, a sleeve fully deployed while
long, and the Risk Engine absent. Production has one account, a 5% per-symbol
ceiling, a 30% total ceiling, and a crypto book competing for the same dollars.
Nothing in the research says how to spend one account across ten simultaneous
LONGs, and the runtime had no answer either: it asked for
``RISK_SIZED_REQUEST_QUANTITY = 1E9`` and let the Risk Engine clamp, which funds
whichever symbols the universe tuple happens to name first and starves the rest.
Six of ten got capital and four got none, chosen by their index in a Python
tuple. That is a sizing policy, and a bad one - so this module holds the sizing
policy explicitly, where it can be read, tested and frozen.

**This is not a risk bypass.** The allocator computes a *desired* target. The
Risk Engine still evaluates every resulting order and still has final authority;
its ceilings are not read from here and cannot be widened from here. What the
allocator changes is that Risk is now asked for something it will grant, rather
than asked for a billion shares and used as the sizing rule.

**Order independence is the property that matters.** Every function here takes
the active set as an unordered collection and derives weights from its
*cardinality*, never from any symbol's position in it. Two symbols that are both
LONG receive the same weight, always. The plan comes back sorted by symbol so a
caller cannot reintroduce an ordering effect by iterating a dict.

**Whole shares are the natural no-trade band.** `plan_allocation` floors every
target to an integral share count, exactly as the equity execution boundary
does. A target that has not moved a whole share produces no order at all, so a
15-minute runtime holding a stable target submits nothing - which is what makes
"desired == actual -> NO ORDER" a real rule rather than an unreachable one.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from autotrader.equity import EQUITY_SYMBOLS, EquityError, normalize_symbol
from autotrader.execution.models import OrderSide
from autotrader.risk.engine import MAX_POSITION_FRACTION, MAX_TOTAL_EXPOSURE_FRACTION

#: Policy A - the available account budget split equally across the symbols that
#: are actually LONG, subject to the per-symbol ceiling. Budget-filling: a flat
#: symbol does not sterilize its share of the account's permitted risk.
POLICY_EQUAL_ACTIVE = "A_EQUAL_ACTIVE"

#: Policy B - a fixed per-symbol target, scaled down pro rata across *all*
#: active symbols when the aggregate would exceed the available budget. No
#: symbol is zeroed before another. Constant per-name risk.
POLICY_FIXED_PRO_RATA = "B_FIXED_PRO_RATA"

#: Policy C - each of the ten symbols owns a fixed reserved share of the budget
#: which it uses while LONG and leaves idle while flat. Capital is never
#: transferred between symbols, which is the research architecture's own
#: per-symbol behaviour restated under a cap.
POLICY_RESERVED_UNIVERSE = "C_RESERVED_UNIVERSE"

POLICY_IDS: tuple[str, ...] = (
    POLICY_EQUAL_ACTIVE,
    POLICY_FIXED_PRO_RATA,
    POLICY_RESERVED_UNIVERSE,
)

#: Weights are quantized to this many decimal places, rounding **down**.
#:
#: `budget / count` is a repeating decimal for most counts, and ten copies of
#: the 28-significant-digit result sum to the total cap plus about 2e-31 - which
#: is arithmetically nothing but makes "the weights never exceed the total cap"
#: read as false. Flooring at twelve places makes the invariant exactly true and
#: costs at most a hundred-billionth of a weight, which is 0.001 cents on a
#: $100,000 account: far below the whole share that is the smallest thing this
#: system can actually trade.
WEIGHT_QUANTUM = Decimal("0.000000000001")

#: Policy B's predeclared per-symbol target. Not a chosen number: it is the
#: weight at which ten simultaneous LONGs exactly fill the 30% total ceiling,
#: so it is a consequence of the two production caps.
DEFAULT_FIXED_TARGET = Decimal("0.03")

_ZERO = Decimal(0)
_ONE = Decimal(1)


class AllocationError(EquityError):
    """An allocation request that cannot be answered."""


@dataclass(frozen=True)
class AllocationPolicy:
    """One allocation rule, with the ceilings it is required to respect.

    The ceilings are carried here as *the values the allocator must not exceed*,
    defaulted from the Risk Engine's own constants so the two cannot drift
    apart. They are a constraint on this module, never an instruction to the
    Risk Engine: nothing here can widen a cap, and a plan that somehow exceeded
    one would still be refused downstream.
    """

    policy_id: str
    per_symbol_cap: Decimal = Decimal(str(MAX_POSITION_FRACTION))
    total_cap: Decimal = Decimal(str(MAX_TOTAL_EXPOSURE_FRACTION))
    universe_size: int = len(EQUITY_SYMBOLS)
    fixed_target: Decimal = DEFAULT_FIXED_TARGET

    def __post_init__(self) -> None:
        if self.policy_id not in POLICY_IDS:
            raise AllocationError(
                f"Unknown allocation policy {self.policy_id!r}. Known policies: "
                f"{', '.join(POLICY_IDS)}."
            )
        if not _ZERO < self.per_symbol_cap <= _ONE:
            raise AllocationError(
                f"per_symbol_cap must be a fraction in (0, 1], got {self.per_symbol_cap}."
            )
        if not _ZERO < self.total_cap <= _ONE:
            raise AllocationError(f"total_cap must be a fraction in (0, 1], got {self.total_cap}.")
        if self.universe_size < 1:
            raise AllocationError(f"universe_size must be >= 1, got {self.universe_size}.")
        if not _ZERO < self.fixed_target <= _ONE:
            raise AllocationError(
                f"fixed_target must be a fraction in (0, 1], got {self.fixed_target}."
            )

    def to_json_dict(self) -> dict[str, object]:
        """The policy as canonical JSON-able data. The basis of `config_hash`."""
        return {
            "policy_id": self.policy_id,
            "per_symbol_cap": str(self.per_symbol_cap),
            "total_cap": str(self.total_cap),
            "universe_size": self.universe_size,
            "fixed_target": str(self.fixed_target),
        }

    def config_hash(self) -> str:
        """A stable digest of the frozen configuration.

        What a report cites and what a service can log to prove the policy it is
        running is the policy that was validated. Sorted keys and no whitespace,
        so the digest depends on the values and not on how they were written.
        """
        payload = json.dumps(self.to_json_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def available_budget_fraction(
    policy: AllocationPolicy,
    external_exposure_fraction: Decimal,
) -> Decimal:
    """The share of account equity this book may hold, given what else does.

    ``max(0, total_cap - external)``. The external figure is every non-equity
    position the account already carries - in this deployment, the crypto book -
    measured against the same equity. Clamped at zero: a crypto position that
    has drifted past the total ceiling on its own leaves the equity book with no
    budget, not a negative one.
    """
    if external_exposure_fraction < _ZERO:
        raise AllocationError(
            f"external_exposure_fraction cannot be negative, got {external_exposure_fraction}."
        )
    return max(_ZERO, policy.total_cap - external_exposure_fraction)


def target_weights(
    policy: AllocationPolicy,
    *,
    active_symbols: Iterable[str],
    external_exposure_fraction: Decimal,
) -> dict[str, Decimal]:
    """Target exposure per symbol, as a fraction of account equity.

    `active_symbols` is consumed as a **set**: the result is a function of which
    symbols are active and how many, never of the order they arrive in. Symbols
    outside the frozen universe are refused rather than silently allocated.

    Every branch ends in ``min(..., per_symbol_cap)``, so no weight can exceed
    the per-symbol ceiling by construction; and each policy's own arithmetic
    keeps the sum at or under the available budget, so the total ceiling holds
    too. Both are re-checked on the realized path by the allocator's tests
    rather than trusted from this docstring.
    """
    active = {normalize_symbol(symbol) for symbol in active_symbols}
    count = len(active)
    if count == 0:
        return {}
    if count > policy.universe_size:
        raise AllocationError(
            f"{count} active symbols exceeds the frozen universe size "
            f"{policy.universe_size}. Refusing to allocate against a universe this "
            "policy was not validated on."
        )

    budget = available_budget_fraction(policy, external_exposure_fraction)

    if policy.policy_id == POLICY_EQUAL_ACTIVE:
        weight = min(budget / Decimal(count), policy.per_symbol_cap)
    elif policy.policy_id == POLICY_RESERVED_UNIVERSE:
        weight = min(budget / Decimal(policy.universe_size), policy.per_symbol_cap)
    elif policy.policy_id == POLICY_FIXED_PRO_RATA:
        desired_total = policy.fixed_target * Decimal(count)
        scale = min(_ONE, budget / desired_total) if desired_total > _ZERO else _ONE
        weight = min(scale * policy.fixed_target, policy.per_symbol_cap)
    else:  # pragma: no cover - constructor already refused anything else
        raise AllocationError(f"Unknown allocation policy {policy.policy_id!r}.")

    quantized = weight.quantize(WEIGHT_QUANTUM, rounding=ROUND_FLOOR)
    return {symbol: quantized for symbol in sorted(active)}


def whole_shares(notional: Decimal, price: Decimal) -> Decimal:
    """How many whole shares `notional` buys at `price`, rounding **down**.

    Down, always, for the same reason the execution boundary rounds down: the
    broker must never be asked for more than the target justified. A price that
    is zero or negative cannot size anything and is refused rather than treated
    as free.
    """
    if price <= _ZERO:
        raise AllocationError(f"Cannot size a target at a non-positive price {price}.")
    if notional <= _ZERO:
        return _ZERO
    return (notional / price).to_integral_value(rounding=ROUND_FLOOR)


@dataclass(frozen=True)
class SymbolAllocation:
    """One symbol's desired state, and the delta that would reach it.

    `delta_quantity` is always non-negative and is paired with `side`: a `None`
    side means no order, and is the normal case for a target that has not moved.
    A SELL is never larger than `actual_quantity`, so an exit cannot become a
    short.
    """

    symbol: str
    target_weight: Decimal
    target_notional: Decimal
    reference_price: Decimal
    target_quantity: Decimal
    actual_quantity: Decimal
    delta_quantity: Decimal
    side: OrderSide | None

    @property
    def orders(self) -> bool:
        """Whether this allocation asks for a broker mutation at all."""
        return self.side is not None and self.delta_quantity > _ZERO

    def to_json_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "target_weight": str(self.target_weight),
            "target_notional": str(self.target_notional),
            "reference_price": str(self.reference_price),
            "target_quantity": str(self.target_quantity),
            "actual_quantity": str(self.actual_quantity),
            "delta_quantity": str(self.delta_quantity),
            "side": self.side.value if self.side is not None else None,
        }


@dataclass(frozen=True)
class AllocationPlan:
    """What the whole book should look like, and what it would take to get there."""

    policy: AllocationPolicy
    account_equity: Decimal
    external_exposure_fraction: Decimal
    budget_fraction: Decimal
    allocations: tuple[SymbolAllocation, ...]

    @property
    def total_target_weight(self) -> Decimal:
        """The sum of the desired weights. Never above the available budget."""
        return sum((item.target_weight for item in self.allocations), _ZERO)

    @property
    def ordering(self) -> tuple[SymbolAllocation, ...]:
        """Only the allocations that actually ask for a broker mutation."""
        return tuple(item for item in self.allocations if item.orders)

    def to_json_dict(self) -> dict[str, object]:
        return {
            "policy": self.policy.to_json_dict(),
            "policy_config_hash": self.policy.config_hash(),
            "account_equity": str(self.account_equity),
            "external_exposure_fraction": str(self.external_exposure_fraction),
            "budget_fraction": str(self.budget_fraction),
            "total_target_weight": str(self.total_target_weight),
            "allocations": [item.to_json_dict() for item in self.allocations],
        }


def plan_allocation(
    policy: AllocationPolicy,
    *,
    active_symbols: Iterable[str],
    account_equity: Decimal,
    external_exposure_fraction: Decimal,
    reference_prices: Mapping[str, Decimal],
    actual_quantities: Mapping[str, Decimal],
) -> AllocationPlan:
    """The full desired-versus-actual plan for one bar.

    Every symbol the account currently holds is included even when it is no
    longer active, because a target of zero against a held position is exactly
    the case that must produce a SELL rather than silence. Symbols that are
    neither active nor held are omitted: there is nothing to say about them.

    `reference_prices` and `actual_quantities` come from the broker, not from
    local state - the delta is computed against what the account demonstrably
    holds, so a restart, a partial fill, or a fill this process never saw all
    converge on the same answer instead of producing a second order.
    """
    if account_equity <= _ZERO:
        raise AllocationError(
            f"Cannot allocate against a non-positive account equity {account_equity}."
        )

    weights = target_weights(
        policy,
        active_symbols=active_symbols,
        external_exposure_fraction=external_exposure_fraction,
    )
    held = {
        normalize_symbol(symbol): quantity
        for symbol, quantity in actual_quantities.items()
        if quantity > _ZERO
    }

    allocations: list[SymbolAllocation] = []
    for symbol in sorted(set(weights) | set(held)):
        weight = weights.get(symbol, _ZERO)
        actual = held.get(symbol, _ZERO)
        if weight > _ZERO:
            price = reference_prices.get(symbol)
            if price is None:
                raise AllocationError(
                    f"No reference price for {symbol}, which this plan wants to hold at "
                    f"weight {weight}. A target cannot be sized without a price."
                )
            price = Decimal(price)
            notional = policy_notional(weight, account_equity)
            target = whole_shares(notional, price)
        else:
            # A target of zero needs no price: the delta is the whole position.
            price = Decimal(reference_prices.get(symbol, _ZERO))
            notional = _ZERO
            target = _ZERO

        if target > actual:
            side: OrderSide | None = OrderSide.BUY
            delta = target - actual
        elif target < actual:
            side = OrderSide.SELL
            # Never more than the account holds: an exit may not become a short.
            delta = min(actual - target, actual)
        else:
            side = None
            delta = _ZERO

        allocations.append(
            SymbolAllocation(
                symbol=symbol,
                target_weight=weight,
                target_notional=notional,
                reference_price=price,
                target_quantity=target,
                actual_quantity=actual,
                delta_quantity=delta,
                side=side,
            )
        )

    return AllocationPlan(
        policy=policy,
        account_equity=account_equity,
        external_exposure_fraction=external_exposure_fraction,
        budget_fraction=available_budget_fraction(policy, external_exposure_fraction),
        allocations=tuple(allocations),
    )


def policy_notional(weight: Decimal, account_equity: Decimal) -> Decimal:
    """The dollar target a weight names against this account."""
    return weight * account_equity


def external_exposure_fraction_from(
    *,
    account_equity: float | Decimal,
    non_equity_exposure: float | Decimal,
) -> Decimal:
    """Crypto (and anything else non-equity) as a fraction of account equity.

    Taken from broker truth by the caller: this system's total exposure ceiling
    is an account-wide ceiling, and an equity book that measured itself against
    only its own positions would size into headroom the crypto book is already
    using. Clamped at zero so a negative or absent figure cannot manufacture
    budget.
    """
    equity = Decimal(str(account_equity))
    if equity <= _ZERO:
        raise AllocationError(
            f"Cannot express external exposure against a non-positive equity {equity}."
        )
    exposure = Decimal(str(non_equity_exposure))
    return max(_ZERO, exposure / equity)


__all__ = [
    "DEFAULT_FIXED_TARGET",
    "POLICY_EQUAL_ACTIVE",
    "WEIGHT_QUANTUM",
    "POLICY_FIXED_PRO_RATA",
    "POLICY_IDS",
    "POLICY_RESERVED_UNIVERSE",
    "AllocationError",
    "AllocationPlan",
    "AllocationPolicy",
    "SymbolAllocation",
    "available_budget_fraction",
    "external_exposure_fraction_from",
    "plan_allocation",
    "policy_notional",
    "target_weights",
    "whole_shares",
]
