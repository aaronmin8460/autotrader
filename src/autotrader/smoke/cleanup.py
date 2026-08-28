"""Cleanup planning: how much of a position an order *could* close, as text.

**This module generates a command string. It never runs one.** Nothing here
imports `subprocess`, `os.system`, or any other way to start a process, and a
structural test asserts that rather than trusting this sentence. The string
"paper-submit" appears below as output - a line for a human to read, check, and
choose to type - and there is no code path that consumes it.

**The quantity comes from the broker's position, and from nothing else.** Not
from the quantity an order requested, not from the quantity a fill reported,
not from the local `positions` table. Those three have all been wrong here: a
BUY of 0.00016705 BTC settled as a position of 0.000166632 BTC once the taker
fee came out of the base asset, and a cleanup sized from the ordered number
would have tried to sell more than the account held. `tests/test_smoke_harness.py`
pins that exact case.

**Rounding is always down, and the result is always checked.** A plan may close
less than the position; it may never close more. Rounding a cleanup *up* to
clear a broker minimum would sell an asset the account does not hold, which is
a short - something this system cannot express and must not be talked into by
an arithmetic convenience.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from pathlib import Path

from autotrader.execution.models import format_quantity
from autotrader.execution.paper import USD_MINIMUM_ORDER_NOTIONAL, CryptoAssetSpec, is_usd_quoted
from autotrader.smoke.models import (
    USER_MUST_EXECUTE_BANNER,
    CleanupPlan,
    CleanupVerdict,
    PositionSnapshot,
    SmokeError,
)
from autotrader.smoke.readonly import is_crypto_symbol, normalize_smoke_symbol

#: Working precision for the increment arithmetic. Matches the execution
#: layer's own threshold precision so a plan and a submission round identically.
_PRECISION = 60

#: A share is the smallest thing an equity order can move under this system's
#: whole-share policy. Applied only when the broker publishes no metadata of
#: its own for the symbol, which is every equity on current `main`.
_WHOLE_SHARE = Decimal(1)

_ZERO = Decimal(0)


class CleanupPlanError(SmokeError):
    """A plan could not be produced, or violated its own invariant."""


@dataclass(frozen=True)
class AssetPolicy:
    """The precision rules one symbol's orders must obey.

    Assembled from the broker for crypto and from this system's whole-share
    rule for equities, and carrying `source` so a report can say which. That
    distinction matters operationally: a crypto plan is built from metadata
    read seconds ago, an equity plan from a policy this build assumes, and an
    operator should not have to guess which they are looking at.
    """

    symbol: str
    asset_class: str
    min_order_size: Decimal
    min_trade_increment: Decimal
    notional_minimum: Decimal | None
    source: str


def policy_for(symbol: str, asset: CryptoAssetSpec | None) -> AssetPolicy | None:
    """The order-precision policy for `symbol`, or None when it is unknowable.

    None is returned for a crypto pair whose broker metadata could not be read.
    That is a deliberate refusal rather than a default: crypto increments differ
    per pair and change over time, and a guessed increment produces a plan the
    broker rejects at best and a wrong quantity at worst. Fail closed, report
    why, plan nothing.

    Equities take the whole-share policy. Alpaca applies no USD order minimum
    to them, so `notional_minimum` is None and the only floor is one share.
    """
    ticker = normalize_smoke_symbol(symbol)
    if asset is not None:
        return AssetPolicy(
            symbol=ticker,
            asset_class="CRYPTO",
            min_order_size=asset.min_order_size,
            min_trade_increment=asset.min_trade_increment,
            notional_minimum=USD_MINIMUM_ORDER_NOTIONAL if is_usd_quoted(ticker) else None,
            source="live broker asset metadata",
        )
    if is_crypto_symbol(ticker):
        return None
    return AssetPolicy(
        symbol=ticker,
        asset_class="EQUITY",
        min_order_size=_WHOLE_SHARE,
        min_trade_increment=_WHOLE_SHARE,
        notional_minimum=None,
        source="whole-share policy (this build reads no equity broker metadata)",
    )


def floor_to_increment(quantity: Decimal, increment: Decimal) -> Decimal:
    """`quantity` rounded **down** to a whole multiple of `increment`.

    Down, always, and never by more than one increment. This is the operation
    that turns broker truth into a submittable size, and it is the only
    rounding a cleanup quantity ever goes through.
    """
    if increment <= 0:
        raise CleanupPlanError(f"A trade increment must be positive, got {increment}.")
    if quantity <= 0:
        return _ZERO
    with localcontext() as context:
        context.prec = _PRECISION
        steps = (quantity / increment).to_integral_value(rounding=ROUND_FLOOR)
        floored = steps * increment
    if floored > quantity:  # pragma: no cover - ROUND_FLOOR cannot exceed the input
        raise CleanupPlanError("Rounding down increased the quantity, which is never allowed.")
    return +floored


def ceil_to_increment(quantity: Decimal, increment: Decimal) -> Decimal:
    """`quantity` rounded **up** to a whole multiple of `increment`.

    Used only for *thresholds* - the smallest quantity worth the broker's
    minimum order value - and never for a quantity that becomes an order. A
    threshold rounded down would sit below the floor it describes, which is the
    bug this direction exists to avoid. Mirrors
    `paper.minimum_quantity_from_notional`.
    """
    if increment <= 0:
        raise CleanupPlanError(f"A trade increment must be positive, got {increment}.")
    if quantity <= 0:
        return _ZERO
    with localcontext() as context:
        context.prec = _PRECISION
        steps = (quantity / increment).to_integral_value(rounding=ROUND_CEILING)
        return +(steps * increment)


def minimum_valid_quantity(policy: AssetPolicy, reference_price: float | None) -> Decimal | None:
    """The smallest quantity the broker would accept for `policy`, or None.

    The larger of the asset's own minimum order size and the quantity worth the
    USD notional floor - the same "must clear both" rule the execution layer
    applies, restated here because a plan that clears only one of them is a
    command the operator would watch get rejected.

    None when a USD floor applies but no price is available to measure it in.
    Reporting "unknown" beats reporting a floor computed without a price.
    """
    floor = policy.min_order_size
    if policy.notional_minimum is None:
        return floor
    if reference_price is None or reference_price <= 0:
        return None
    price = Decimal(str(reference_price))
    notional_floor = ceil_to_increment(policy.notional_minimum / price, policy.min_trade_increment)
    return max(floor, notional_floor)


def resolve_reference_price(
    position: PositionSnapshot, quoted_price: float | None
) -> tuple[float | None, str]:
    """A usable mark for the position, and where it came from.

    A live quote when one was fetched. Otherwise the broker's own
    `market_value / quantity` for the position it is reporting, which is not a
    guess - it is the broker's valuation of the exact holding being planned
    against, and it is the only price available for an equity on this build.
    """
    if quoted_price is not None and quoted_price > 0:
        return quoted_price, "live market-data quote"
    if position.market_value and position.quantity > 0:
        implied = Decimal(str(position.market_value)) / position.quantity
        if implied > 0:
            return float(implied), "implied from the broker's reported market value"
    return None, "unavailable"


def build_cleanup_command(
    symbol: str, quantity: Decimal, *, database: Path | str | None = None
) -> str:
    """Render the command an operator may choose to type. Text, and only text.

    Returned as a string to be printed. It is not executed here, it is not
    passed to a shell here, and no module in this package imports anything that
    could execute it. The confirmation token and the environment gate it also
    needs are shown alongside by the caller, because a command that looks
    ready-to-run should not hide the two gates that still stand in front of it.
    """
    parts = [
        "autotrader paper-submit",
        f"--symbol {normalize_smoke_symbol(symbol)}",
        "--side SELL",
        f"--qty {format_quantity(quantity)}",
        "--confirm-paper PAPER",
    ]
    if database is not None:
        parts.append(f"--db {database}")
    return " ".join(parts)


def build_dry_run_command(
    symbol: str, side: str, quantity: Decimal, *, database: Path | str | None = None
) -> str:
    """The same command with `--dry-run`, which cannot submit anything.

    Offered first everywhere it appears. `--dry-run` needs neither gate, reads
    the account, the asset and the price, runs the risk engine, prints what
    would happen, and stops - so it is the honest way to check a size before
    committing to one.
    """
    parts = [
        "autotrader paper-submit",
        f"--symbol {normalize_smoke_symbol(symbol)}",
        f"--side {side.strip().upper()}",
        f"--qty {format_quantity(quantity)}",
        "--dry-run",
    ]
    if database is not None:
        parts.append(f"--db {database}")
    return " ".join(parts)


def plan_cleanup(
    *,
    position: PositionSnapshot,
    asset: CryptoAssetSpec | None,
    quoted_price: float | None = None,
    database: Path | str | None = None,
) -> CleanupPlan:
    """Plan the risk-reducing SELL for one position. Pure arithmetic, no I/O.

    Everything it needs is passed in - the broker's position, the broker's
    asset metadata, a price - so the whole decision is testable offline and the
    reads happen once, at the edge, in `collect_cleanup_plan`.

    The invariant is checked before the plan is returned, not assumed: a
    planned quantity greater than the position raises rather than being
    printed. There is no input that should produce that, which is exactly why
    it is worth asserting where a future edit would trip over it.
    """
    symbol = normalize_smoke_symbol(position.symbol)
    quantity = position.quantity

    if quantity <= 0:
        return CleanupPlan(
            symbol=symbol,
            verdict=CleanupVerdict.NONE_REQUIRED,
            position_quantity=_ZERO,
            plan_quantity=_ZERO,
            reference_price=quoted_price,
            estimated_value=_ZERO,
            min_order_size=None,
            min_trade_increment=None,
            minimum_notional_quantity=None,
            full_cleanup_possible=True,
            reason=(
                f"The broker reports no {symbol} position. There is nothing to close, "
                "and no order should be sent."
            ),
        )

    policy = policy_for(symbol, asset)
    price, price_source = resolve_reference_price(position, quoted_price)

    if policy is None:
        return CleanupPlan(
            symbol=symbol,
            verdict=CleanupVerdict.NOT_POSSIBLE,
            position_quantity=quantity,
            plan_quantity=_ZERO,
            reference_price=price,
            estimated_value=_estimated_value(quantity, price),
            min_order_size=None,
            min_trade_increment=None,
            minimum_notional_quantity=None,
            full_cleanup_possible=False,
            reason=(
                f"The broker's precision metadata for {symbol} could not be read, so the "
                "trade increment is unknown. No quantity is proposed: a guessed increment "
                "would produce a plan the broker refuses, or a wrong size. Retry once the "
                "broker answers."
            ),
        )

    planned = floor_to_increment(quantity, policy.min_trade_increment)
    minimum = minimum_valid_quantity(policy, price)
    estimated = _estimated_value(planned, price)
    full = planned == quantity

    if planned <= 0 or planned < policy.min_order_size:
        plan = CleanupPlan(
            symbol=symbol,
            verdict=CleanupVerdict.NOT_POSSIBLE,
            position_quantity=quantity,
            plan_quantity=_ZERO,
            reference_price=price,
            estimated_value=_estimated_value(quantity, price),
            min_order_size=policy.min_order_size,
            min_trade_increment=policy.min_trade_increment,
            minimum_notional_quantity=minimum,
            full_cleanup_possible=False,
            reason=(
                f"The {symbol} position of {format_quantity(quantity)} rounds down to "
                f"{format_quantity(planned)} at a trade increment of "
                f"{format_quantity(policy.min_trade_increment)}, which is below the "
                f"minimum order size of {format_quantity(policy.min_order_size)} "
                f"({policy.source}). No order can close it. Rounding up is not an "
                "option: it would sell more than the account holds."
            ),
        )
        return _verified(plan)

    if minimum is None:
        plan = CleanupPlan(
            symbol=symbol,
            verdict=CleanupVerdict.NOT_POSSIBLE,
            position_quantity=quantity,
            plan_quantity=_ZERO,
            reference_price=price,
            estimated_value=None,
            min_order_size=policy.min_order_size,
            min_trade_increment=policy.min_trade_increment,
            minimum_notional_quantity=None,
            full_cleanup_possible=False,
            reason=(
                f"{symbol} is subject to a ${format_quantity(policy.notional_minimum or _ZERO)} "
                "minimum order value, and no price could be obtained to measure the "
                "position against it. No quantity is proposed while the check cannot be "
                "made."
            ),
        )
        return _verified(plan)

    if planned < minimum:
        plan = CleanupPlan(
            symbol=symbol,
            verdict=CleanupVerdict.NOT_POSSIBLE,
            position_quantity=quantity,
            plan_quantity=_ZERO,
            reference_price=price,
            estimated_value=estimated,
            min_order_size=policy.min_order_size,
            min_trade_increment=policy.min_trade_increment,
            minimum_notional_quantity=minimum,
            full_cleanup_possible=False,
            reason=(
                f"The {symbol} position of {format_quantity(quantity)} is worth about "
                f"${_money(estimated)} at {price} ({price_source}), below the broker's "
                f"minimum order value. The smallest quantity it would accept is "
                f"{format_quantity(minimum)}, which is more than the account holds. "
                "The position cannot be closed until its value recovers above the "
                "minimum; this is the broker's constraint, not this system's. Leave it "
                "and record it - do NOT top the position up to clear the floor, because "
                "that is a second opening order in a smoke that is supposed to place one."
            ),
        )
        return _verified(plan)

    plan = CleanupPlan(
        symbol=symbol,
        verdict=CleanupVerdict.REQUIRED,
        position_quantity=quantity,
        plan_quantity=planned,
        reference_price=price,
        estimated_value=estimated,
        min_order_size=policy.min_order_size,
        min_trade_increment=policy.min_trade_increment,
        minimum_notional_quantity=minimum,
        full_cleanup_possible=full,
        reason=(
            f"Sized from the broker's reported {symbol} position of "
            f"{format_quantity(quantity)}, rounded down to "
            f"{format_quantity(planned)} at a trade increment of "
            f"{format_quantity(policy.min_trade_increment)} ({policy.source}). Priced at "
            f"{price} ({price_source})."
            + (
                ""
                if full
                else f" A residue of {format_quantity(quantity - planned)} cannot be "
                "expressed at the broker's increment and will remain."
            )
        ),
        command=build_cleanup_command(symbol, planned, database=database),
    )
    return _verified(plan)


def plan_minimum_entry(
    *,
    symbol: str,
    asset: CryptoAssetSpec | None,
    quoted_price: float | None,
    database: Path | str | None = None,
) -> tuple[Decimal | None, str, str | None]:
    """The smallest entry the broker would accept right now, and how to check it.

    Returns `(quantity, explanation, dry_run_command)`. This is a **floor**, not
    a recommendation and not a size: it is what the broker will not go below at
    the price read a moment ago, and that price moves. Sizing the actual smoke
    BUY is the operator's decision, made against the account, the risk state,
    and the session at the time - which is why the command handed back is the
    `--dry-run` form that evaluates all of that and submits nothing.
    """
    ticker = normalize_smoke_symbol(symbol)
    policy = policy_for(ticker, asset)
    if policy is None:
        return (
            None,
            (
                f"The broker's precision metadata for {ticker} could not be read, so the "
                "smallest valid order size is unknown."
            ),
            None,
        )
    minimum = minimum_valid_quantity(policy, quoted_price)
    if minimum is None:
        return (
            None,
            (
                f"{ticker} has a USD minimum order value and no price was available to "
                "measure it in, so the smallest valid order size is unknown."
            ),
            None,
        )
    explanation = (
        f"At {quoted_price} the smallest order the broker would accept is "
        f"{format_quantity(minimum)} {ticker} ({policy.source}). This is a floor read "
        "at preflight time, not a recommended size, and the price moves."
    )
    return minimum, explanation, build_dry_run_command(ticker, "BUY", minimum, database=database)


def _estimated_value(quantity: Decimal, price: float | None) -> Decimal | None:
    """`quantity * price`, to the cent, or None without a price."""
    if price is None or price <= 0:
        return None
    return (quantity * Decimal(str(price))).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)


def _money(value: Decimal | None) -> str:
    return "unknown" if value is None else format_quantity(value)


def _verified(plan: CleanupPlan) -> CleanupPlan:
    """Return `plan` only if it closes no more than the account holds.

    The last gate before a quantity is printed for a human to type. Nothing
    above should be able to produce a violation; this raises rather than
    printing one if something ever does.
    """
    if plan.plan_quantity > plan.position_quantity:
        raise CleanupPlanError(
            f"Planned cleanup of {format_quantity(plan.plan_quantity)} {plan.symbol} "
            f"exceeds the broker position of {format_quantity(plan.position_quantity)}. "
            "Refusing to report it: selling more than is held is a short."
        )
    if plan.plan_quantity < 0:
        raise CleanupPlanError("A cleanup quantity may not be negative.")
    return plan


__all__ = [
    "AssetPolicy",
    "CleanupPlanError",
    "build_cleanup_command",
    "build_dry_run_command",
    "ceil_to_increment",
    "floor_to_increment",
    "minimum_valid_quantity",
    "plan_cleanup",
    "plan_minimum_entry",
    "policy_for",
    "resolve_reference_price",
    "USER_MUST_EXECUTE_BANNER",
]
