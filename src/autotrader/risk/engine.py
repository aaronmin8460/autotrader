"""Phase 5: the deterministic risk engine.

A strategy says *what* it wants to do; this module says *whether it may*, and
*how large it may be*. It answers exactly one question:

    may this proposed trade be allowed, and if so, what is the largest safe
    whole-share quantity under the V0.1 limits?

**It is a calculator, nothing else.** It submits no order, constructs no
broker client, contacts no network, opens no database, writes no file, and
mutates neither the request nor the context it is handed. Given the same
inputs it always returns the same decision. Turning an approved decision into
an actual order belongs to a later phase (docs/SPEC.md section 6A); this
module is the stage before that one and deliberately cannot reach past it.

**It is not connected to Phase 4.** The backtester keeps its own all-cash
sizing baseline and is not re-plumbed through these limits, so no Phase 4
result changes. Wiring risk into live sizing is a later concern.

**Entries are gated; exits are not.** Every limit here exists to stop the
account from *adding* risk. None of them may stop it from *removing* risk, so
a SELL that only reduces an existing long is evaluated separately: the
`trading_enabled` kill switch, the daily-loss halt, the per-symbol cap, and
the total-exposure cap are all entry gates and none of them can block an exit.
A kill switch that trapped an open position would be a safety defect, not a
safety feature.

**Oversized requests are clamped, not refused.** A BUY for more than the safe
maximum is approved at the maximum rather than rejected outright, and the
decision names the constraint that bound it. A SELL for more than the position
is clamped to the position. Nothing is ever silently allowed to exceed a
limit: when the safe maximum is zero, the request is rejected.

**Long only, structurally.** There is no short side to request - `RiskSide`
has exactly `BUY` and `SELL`, a SELL is always an exit against an existing
long, and it can never be approved for more shares than are held. Because a
short cannot be expressed, there is no reachable "long-only violation" to
report; an attempt to sell while flat surfaces as `NO_POSITION_TO_EXIT`.

**Malformed input is not a risk denial.** A context that cannot describe a
real account - negative cash, zero equity, a symbol exposure larger than the
total - is a programming error and raises `RiskInputError`. A well-formed
context carrying a badly formed request produces an ordinary rejected
decision with `INVALID_REQUEST`. Ordinary risk denials never raise.

Plain floats are used throughout, matching the rest of the project. The only
rounding is the floor to whole shares.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

# --------------------------------------------------------------------------
# V0.1 policy defaults
#
# These are engineering safety limits, not investment advice and not a
# recommended allocation. They are deliberately conservative, deliberately
# strategy-independent, and deliberately not loaded from the environment.
# --------------------------------------------------------------------------

#: Largest market value of any one symbol, as a fraction of current equity.
MAX_POSITION_FRACTION = 0.05

#: Largest aggregate long exposure, as a fraction of current equity.
MAX_TOTAL_EXPOSURE_FRACTION = 0.30

#: Daily loss at which new entries halt, as a fraction of start-of-day equity.
MAX_DAILY_LOSS_FRACTION = 0.02

#: Stable, machine-readable decision codes. Messages may be reworded; codes may not.
APPROVED = "APPROVED"
INVALID_REQUEST = "INVALID_REQUEST"
TRADING_DISABLED = "TRADING_DISABLED"
DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
POSITION_LIMIT = "POSITION_LIMIT"
TOTAL_EXPOSURE_LIMIT = "TOTAL_EXPOSURE_LIMIT"
INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
NO_POSITION_TO_EXIT = "NO_POSITION_TO_EXIT"
EXIT_QUANTITY_EXCEEDS_POSITION = "EXIT_QUANTITY_EXCEEDS_POSITION"

REASON_CODES: tuple[str, ...] = (
    APPROVED,
    INVALID_REQUEST,
    TRADING_DISABLED,
    DAILY_LOSS_LIMIT,
    POSITION_LIMIT,
    TOTAL_EXPOSURE_LIMIT,
    INSUFFICIENT_CASH,
    NO_POSITION_TO_EXIT,
    EXIT_QUANTITY_EXCEEDS_POSITION,
)

#: How each entry-sizing constraint is described in a decision message.
_LIMIT_DESCRIPTIONS = {
    POSITION_LIMIT: "the per-symbol exposure cap",
    TOTAL_EXPOSURE_LIMIT: "the total exposure cap",
    INSUFFICIENT_CASH: "available cash",
}


class RiskInputError(Exception):
    """The request or account context could not describe a real situation.

    Raised for a malformed account context or an unsupported policy - both
    programming errors, not risk outcomes. An ordinary risk denial returns a
    rejected `RiskDecision` instead and never raises.
    """


class RiskSide(Enum):
    """The two sides a long-only account can ask about.

    `BUY` opens or adds to a long; `SELL` reduces one. There is no short side,
    which is how long-only is enforced: it cannot be requested. This vocabulary
    is kept distinct from the strategy's `BUY`/`EXIT` signals and from the
    backtester's `ExecutionSide`, so a risk question is never mistaken for a
    signal or for a fill.
    """

    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class RiskPolicy:
    """The limits a decision is measured against.

    Fractions are decimal, not percentages: ``0.05`` is 5%. The three boolean
    fields record the V0.1 stance explicitly rather than leaving it implicit in
    the code; the engine implements only that stance, so flipping one is
    rejected as an unsupported policy rather than quietly ignored.
    """

    max_position_fraction: float = MAX_POSITION_FRACTION
    max_total_exposure_fraction: float = MAX_TOTAL_EXPOSURE_FRACTION
    max_daily_loss_fraction: float = MAX_DAILY_LOSS_FRACTION
    long_only: bool = True
    allow_leverage: bool = False
    whole_shares_only: bool = True


#: The V0.1 policy: 5% per symbol, 30% total, a 2% daily-loss halt, long only,
#: no leverage, whole shares. There is no other policy in this milestone.
DEFAULT_POLICY = RiskPolicy()


@dataclass(frozen=True)
class RiskRequest:
    """One proposed trade, described only as far as risk needs to see it.

    This is a question about a hypothetical trade, **not** an order and not an
    intent to be persisted: it carries no order type, no time in force, no
    identifier, and no broker field. `reference_price` is the price the sizing
    arithmetic is done against - a mark, not a promised fill.
    """

    symbol: str
    side: RiskSide
    reference_price: float
    requested_quantity: int


@dataclass(frozen=True)
class RiskContext:
    """The account state a decision is measured against.

    A flat snapshot, not a portfolio model: it holds no per-symbol collection,
    no order history, and no position objects. `symbol_exposure` is the current
    market value of the requested symbol and is part of `total_exposure`;
    `current_position_quantity` is that same holding in shares. `daily_pnl` is
    the only field that may be negative.
    """

    equity: float
    cash: float
    total_exposure: float
    symbol_exposure: float
    current_position_quantity: int
    daily_pnl: float
    start_of_day_equity: float
    trading_enabled: bool


@dataclass(frozen=True)
class RiskDecision:
    """The engine's answer.

    `approved_quantity` is the whole-share quantity that may proceed, and is
    ``0`` whenever `approved` is False. It may be **smaller** than the
    requested quantity: an oversized request is clamped rather than refused,
    and `reason_code` then names the constraint that bound it instead of
    `APPROVED`. `max_allowed_quantity` is the cap that applied - the sizing
    ceiling for an entry, the current position for an exit - so a caller can
    see the headroom even when the full request was approved.

    `reason_code` is a stable machine string from `REASON_CODES`; `message` is
    human-readable and is not part of the contract.
    """

    approved: bool
    approved_quantity: int
    reason_code: str
    message: str
    max_allowed_quantity: int


# --------------------------------------------------------------------------
# Input contract
#
# A malformed policy or context raises; a malformed request is answered with a
# rejected decision. The split is deliberate: the first two are assembled by
# the program, the third describes something a caller wanted to do.
# --------------------------------------------------------------------------


def _finite(value: object) -> float | None:
    """`value` as a finite float, or None if it is not one.

    Only real numbers are accepted. A numeric string is *not* coerced: a price
    that arrived as text means something upstream lost its type, and quietly
    parsing it would hide that. `bool` is refused for the same reason - it is
    an `int` subclass, so a flag would otherwise size as $1.

    NaN and both infinities are rejected here rather than being allowed to
    propagate silently through a comparison, where they would quietly evaluate
    False and read as a passing check.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _whole_quantity(value: object) -> int | None:
    """`value` as a share count, or None if it is not a whole number of shares.

    Fractional shares are out of scope, so a float quantity is refused rather
    than rounded. `bool` is an `int` subclass in Python and is refused too - a
    flag reaching a share count is a type confusion, not a quantity of one.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _require_supported_policy(policy: RiskPolicy) -> None:
    """Reject a policy this engine does not actually implement.

    Honouring only part of a policy would be worse than refusing it: a caller
    that set `allow_leverage` would otherwise get long-only, unlevered
    behaviour while believing otherwise. V0.1 implements exactly one stance.
    """
    fractions = (
        ("max_position_fraction", policy.max_position_fraction),
        ("max_total_exposure_fraction", policy.max_total_exposure_fraction),
        ("max_daily_loss_fraction", policy.max_daily_loss_fraction),
    )
    for name, value in fractions:
        number = _finite(value)
        if number is None or not 0.0 < number <= 1.0:
            raise RiskInputError(
                f"policy.{name} must be a finite fraction in (0, 1], got {value!r}."
            )
    if not policy.long_only:
        raise RiskInputError("policy.long_only must be True; short selling is out of scope.")
    if policy.allow_leverage:
        raise RiskInputError("policy.allow_leverage must be False; leverage is out of scope.")
    if not policy.whole_shares_only:
        raise RiskInputError(
            "policy.whole_shares_only must be True; fractional shares are out of scope."
        )


def _require_coherent_context(context: RiskContext) -> None:
    """Reject an account context that cannot describe a real account.

    Nothing is repaired or clamped into range: an impossible balance means the
    caller's bookkeeping is wrong, and sizing against it would produce a
    confidently wrong number.
    """
    positive = (("equity", context.equity), ("start_of_day_equity", context.start_of_day_equity))
    for name, value in positive:
        number = _finite(value)
        if number is None or number <= 0:
            raise RiskInputError(
                f"context.{name} must be a positive, finite number, got {value!r}."
            )

    non_negative = (
        ("cash", context.cash),
        ("total_exposure", context.total_exposure),
        ("symbol_exposure", context.symbol_exposure),
    )
    for name, value in non_negative:
        number = _finite(value)
        if number is None or number < 0:
            raise RiskInputError(
                f"context.{name} must be a non-negative, finite number, got {value!r}."
            )

    if context.symbol_exposure > context.total_exposure:
        raise RiskInputError(
            f"context.symbol_exposure ({context.symbol_exposure!r}) cannot exceed "
            f"context.total_exposure ({context.total_exposure!r}); one symbol's market value "
            "is part of the total."
        )

    quantity = _whole_quantity(context.current_position_quantity)
    if quantity is None or quantity < 0:
        raise RiskInputError(
            "context.current_position_quantity must be a non-negative whole number of shares, "
            f"got {context.current_position_quantity!r}."
        )

    # Only P&L may be negative, so it is checked for finiteness alone.
    if _finite(context.daily_pnl) is None:
        raise RiskInputError(
            f"context.daily_pnl must be a finite number, got {context.daily_pnl!r}."
        )

    if not isinstance(context.trading_enabled, bool):
        raise RiskInputError(
            f"context.trading_enabled must be a bool, got {context.trading_enabled!r}."
        )


def _describe_malformed_request(request: RiskRequest) -> str | None:
    """Why `request` is unusable, or None when it is well formed."""
    if not isinstance(request.symbol, str) or not request.symbol.strip():
        return f"symbol must be a non-empty string, got {request.symbol!r}."
    if not isinstance(request.side, RiskSide):
        return f"side must be a RiskSide (BUY or SELL), got {request.side!r}."
    price = _finite(request.reference_price)
    if price is None or price <= 0:
        return (
            f"reference_price must be a positive, finite number, got {request.reference_price!r}."
        )
    quantity = _whole_quantity(request.requested_quantity)
    if quantity is None:
        return (
            "requested_quantity must be a whole number of shares; fractional shares are out "
            f"of scope. Got {request.requested_quantity!r}."
        )
    if quantity <= 0:
        return f"requested_quantity must be greater than zero, got {quantity!r}."
    return None


# --------------------------------------------------------------------------
# Sizing arithmetic
# --------------------------------------------------------------------------


def _whole_shares(notional: float, price: float) -> int:
    """The largest whole-share quantity `notional` buys at `price`.

    Floored, because fractional shares are out of scope. Floating point can
    land the quotient a hair above what the notional actually covers, so the
    result steps back until it does not - a limit must never be exceeded by a
    rounding artefact.
    """
    if notional <= 0:
        return 0
    quantity = int(notional // price)
    while quantity > 0 and quantity * price > notional:
        quantity -= 1
    return quantity


def _entry_headroom(context: RiskContext, policy: RiskPolicy) -> tuple[float, str]:
    """The notional an entry may still use, and the constraint that caps it.

    Three ceilings apply at once - the per-symbol cap, the total-exposure cap,
    and cash - and the tightest one wins. Ties resolve in that fixed order so
    the reported constraint is deterministic.
    """
    position_remaining = max(
        0.0, context.equity * policy.max_position_fraction - context.symbol_exposure
    )
    portfolio_remaining = max(
        0.0, context.equity * policy.max_total_exposure_fraction - context.total_exposure
    )
    # Cash is the no-leverage rule: an entry may only spend money already held.
    limits = (
        (position_remaining, POSITION_LIMIT),
        (portfolio_remaining, TOTAL_EXPOSURE_LIMIT),
        (float(context.cash), INSUFFICIENT_CASH),
    )
    return min(limits, key=lambda limit: limit[0])


# --------------------------------------------------------------------------
# Entry and exit evaluation
# --------------------------------------------------------------------------


def _evaluate_entry(request: RiskRequest, context: RiskContext, policy: RiskPolicy) -> RiskDecision:
    """Decide a BUY: every gate must pass, then size to the tightest limit."""
    if not context.trading_enabled:
        return RiskDecision(
            approved=False,
            approved_quantity=0,
            reason_code=TRADING_DISABLED,
            message=(
                "New entries are halted because trading_enabled is False. Exits that reduce "
                "an existing position are still allowed."
            ),
            max_allowed_quantity=0,
        )

    loss_ratio = context.daily_pnl / context.start_of_day_equity
    if loss_ratio <= -policy.max_daily_loss_fraction:
        return RiskDecision(
            approved=False,
            approved_quantity=0,
            reason_code=DAILY_LOSS_LIMIT,
            message=(
                f"New entries are halted: the day is down {loss_ratio:.2%} against a "
                f"{-policy.max_daily_loss_fraction:.2%} limit. Exits remain allowed."
            ),
            max_allowed_quantity=0,
        )

    max_notional, binding_code = _entry_headroom(context, policy)
    price = float(request.reference_price)
    max_quantity = _whole_shares(max_notional, price)
    constraint = _LIMIT_DESCRIPTIONS[binding_code]

    if max_quantity <= 0:
        return RiskDecision(
            approved=False,
            approved_quantity=0,
            reason_code=binding_code,
            message=(
                f"BUY rejected: {constraint} leaves ${max_notional:,.2f} of headroom, which "
                f"is not one whole share of {request.symbol} at ${price:,.2f}."
            ),
            max_allowed_quantity=0,
        )

    if request.requested_quantity > max_quantity:
        return RiskDecision(
            approved=True,
            approved_quantity=max_quantity,
            reason_code=binding_code,
            message=(
                f"BUY sized down from {request.requested_quantity} to {max_quantity} share(s) "
                f"of {request.symbol}: {constraint} allows no more at ${price:,.2f}."
            ),
            max_allowed_quantity=max_quantity,
        )

    return RiskDecision(
        approved=True,
        approved_quantity=request.requested_quantity,
        reason_code=APPROVED,
        message=(
            f"BUY {request.requested_quantity} share(s) of {request.symbol} approved; up to "
            f"{max_quantity} would have been allowed."
        ),
        max_allowed_quantity=max_quantity,
    )


def _evaluate_exit(request: RiskRequest, context: RiskContext) -> RiskDecision:
    """Decide a SELL: reducing risk is never blocked by a risk limit.

    The kill switch, the daily-loss halt, and both exposure caps are entry
    gates and are deliberately not consulted here. The one thing an exit may
    not do is cross below zero into a short, so it is clamped to the position.
    """
    position = context.current_position_quantity
    if position <= 0:
        return RiskDecision(
            approved=False,
            approved_quantity=0,
            reason_code=NO_POSITION_TO_EXIT,
            message=(
                f"SELL rejected: there is no long position in {request.symbol} to reduce. "
                "Selling while flat would open a short, which is out of scope."
            ),
            max_allowed_quantity=0,
        )

    if request.requested_quantity > position:
        return RiskDecision(
            approved=True,
            approved_quantity=position,
            reason_code=EXIT_QUANTITY_EXCEEDS_POSITION,
            message=(
                f"SELL sized down from {request.requested_quantity} to {position} share(s) of "
                f"{request.symbol}: an exit may flatten the position but never cross below zero."
            ),
            max_allowed_quantity=position,
        )

    return RiskDecision(
        approved=True,
        approved_quantity=request.requested_quantity,
        reason_code=APPROVED,
        message=(
            f"SELL {request.requested_quantity} share(s) of {request.symbol} approved against "
            f"a {position} share position."
        ),
        max_allowed_quantity=position,
    )


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def evaluate_risk(
    request: RiskRequest,
    context: RiskContext,
    policy: RiskPolicy = DEFAULT_POLICY,
) -> RiskDecision:
    """Decide whether `request` may proceed, and at what whole-share quantity.

    A **BUY** must clear every gate in order: the `trading_enabled` kill
    switch, the daily-loss halt, and then sizing against the tightest of the
    per-symbol cap, the total-exposure cap, and available cash::

        position_remaining  = max(0, equity * 0.05 - symbol_exposure)
        portfolio_remaining = max(0, equity * 0.30 - total_exposure)
        max_notional        = min(position_remaining, portfolio_remaining, cash)
        max_quantity        = floor(max_notional / reference_price)

    An oversized BUY is **clamped** to `max_quantity` and approved, with
    `reason_code` naming the binding constraint; a `max_quantity` of zero is
    rejected. Nothing is ever approved above a limit.

    A **SELL** only reduces an existing long, so no risk limit may block it -
    not the kill switch and not the daily-loss halt. It is rejected only when
    there is no position, and is clamped to the position so it can flatten but
    never open a short.

    Returns a `RiskDecision`. Neither `request` nor `context` is modified, no
    order is created, no broker is contacted, and the same inputs always
    produce the same decision.

    Raises `RiskInputError` when `context` cannot describe a real account or
    when `policy` is one this engine does not implement. A malformed *request*
    is not an exception: it returns a rejected decision with `INVALID_REQUEST`.
    """
    _require_supported_policy(policy)
    _require_coherent_context(context)

    malformed = _describe_malformed_request(request)
    if malformed is not None:
        return RiskDecision(
            approved=False,
            approved_quantity=0,
            reason_code=INVALID_REQUEST,
            message=f"Request rejected: {malformed}",
            max_allowed_quantity=0,
        )

    if request.side is RiskSide.SELL:
        return _evaluate_exit(request, context)
    return _evaluate_entry(request, context, policy)


__all__ = [
    "APPROVED",
    "DAILY_LOSS_LIMIT",
    "DEFAULT_POLICY",
    "EXIT_QUANTITY_EXCEEDS_POSITION",
    "INSUFFICIENT_CASH",
    "INVALID_REQUEST",
    "MAX_DAILY_LOSS_FRACTION",
    "MAX_POSITION_FRACTION",
    "MAX_TOTAL_EXPOSURE_FRACTION",
    "NO_POSITION_TO_EXIT",
    "POSITION_LIMIT",
    "REASON_CODES",
    "TOTAL_EXPOSURE_LIMIT",
    "TRADING_DISABLED",
    "RiskContext",
    "RiskDecision",
    "RiskInputError",
    "RiskPolicy",
    "RiskRequest",
    "RiskSide",
    "evaluate_risk",
]
