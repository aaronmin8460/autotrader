"""What a small, partly-funded real-money account may actually spend today.

The validation policy carries two kinds of ceiling - a percentage of equity and
an absolute number of dollars - and this module is where they meet a specific
account balance and become one set of figures an operator can read off. The
rule is a `min` in both directions, and both directions matter:

    target gross   = min(0.90 x verified equity,  $90)
    hard gross     = min(0.95 x verified equity,  $95)
    exposure bound = min(      verified equity,  $100)

At $50 of settled equity that is $45, $47.50 and $50: the *percentages* bind,
because the account is smaller than the authorization. At $150 it is $90, $95
and $100: the *dollars* bind, because the authorization is smaller than the
account. Neither limb is decoration - each one is the binding constraint in a
funding state this validation is expected to pass through.

**Verified means the broker said so, now.** The equity these figures are
computed from is read from the broker on the cycle that uses it. An expected
deposit is not equity, a deposit the operator has initiated is not equity, and
a deposit that has left the sending bank is not equity: only a balance the
broker reports as settled and available is. There is deliberately no
configuration field for "capital I am about to have", because a runtime that
could be told its balance is a runtime that can be told the wrong one - and on
a partly-funded account the difference between the two is the difference
between trading unlevered and trading on money that has not arrived.

**A deposit is not a profit, and the daily-loss halt has to be told.** The halt
compares equity against the first equity seen this UTC day, which is exactly
right while the only thing moving the account is trading, and wrong the moment
anything else does. On a $50 book the 2% halt trips at a dollar; a $50 deposit
landing at noon moves the same figure by fifty. Left alone, the deposit would
read as a +100% day and would go on masking every trading loss until midnight -
the halt would still be armed, still be computing, and still be answering the
wrong question. So a cash movement does not get netted, corrected, or estimated
around here. It **stops new entries for the day** and says why, because the
honest description of that state is that this system no longer knows what the
day's trading result is, and an unknown is not a licence.

Exits stay available throughout, as they do under every other gate in this
system: a control that trapped a position would be a defect and not a control.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal

from autotrader.equity.allocation import AllocationPolicy

_ZERO = Decimal(0)

#: The cent. Ceilings are reported floored to it - down, always, for the same
#: reason every quantity in this system rounds down.
CENT = Decimal("0.01")

#: Broker activity types that move cash without being a trade. Alpaca's
#: non-trade activity vocabulary: a cash deposit, a cash withdrawal, and the
#: journal entries that move cash between accounts. Any of them makes the
#: UTC-day equity baseline describe something other than trading.
CASH_FLOW_ACTIVITY_TYPES: frozenset[str] = frozenset({"CSD", "CSW", "JNLC"})


class LiveBudgetError(Exception):
    """The spendable budget could not be established, so nothing may be sized."""


@dataclass(frozen=True)
class LiveExposureCeilings:
    """The three dollar ceilings, resolved against one verified balance.

    Every field is USD and every field is already the tighter of the policy's
    percentage and its absolute figure. A caller does not need to know which
    limb won - though `binding` says, because an operator reading a runbook
    line wants to know whether they are constrained by their balance or by
    their own authorization, and those call for different actions.
    """

    verified_equity: Decimal
    target_gross: Decimal
    hard_gross: Decimal
    exposure_bound: Decimal
    per_symbol: Decimal
    slot: Decimal
    binding: str

    def to_json_dict(self) -> dict[str, object]:
        return {
            "verified_equity": str(self.verified_equity),
            "target_gross": str(self.target_gross),
            "hard_gross": str(self.hard_gross),
            "exposure_bound": str(self.exposure_bound),
            "per_symbol": str(self.per_symbol),
            "slot": str(self.slot),
            "binding": self.binding,
        }


def _floor_cent(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_FLOOR)


def effective_ceilings(
    policy: AllocationPolicy, *, verified_equity: Decimal
) -> LiveExposureCeilings:
    """The ceilings that actually apply at this balance, as reported figures.

    Reporting only. The Risk Engine enforces the same arithmetic on the live
    path - `min(equity x fraction, absolute)` is written once, in
    `risk.engine._tightest`, and is what refuses an order - and the allocator
    applies the slot ceiling when it sizes. This function exists so the
    dashboard, the runbook and the first-day checklist can *show* the numbers
    that are being enforced, rather than an operator having to rederive them
    from a policy hash and a percentage.

    `verified_equity` must be the balance the broker reported on this cycle.
    A non-positive one is refused rather than clamped: an account whose equity
    could not be read is not an account with no headroom, it is an account
    whose headroom is unknown.
    """
    if not isinstance(verified_equity, Decimal) or not verified_equity.is_finite():
        raise LiveBudgetError(
            "verified_equity must be an exact, finite Decimal number of USD read from "
            f"the broker, got {verified_equity!r}. Nothing was sized."
        )
    if verified_equity <= _ZERO:
        raise LiveBudgetError(
            f"The broker reported equity of {verified_equity}, which cannot fund any "
            "position. Nothing was sized and no order was submitted."
        )

    percentage_target = policy.budget_target * verified_equity
    percentage_hard = policy.total_cap * verified_equity
    percentage_symbol = policy.per_symbol_cap * verified_equity

    target = _tighter(percentage_target, policy.target_gross_notional)
    hard = _tighter(percentage_hard, policy.hard_gross_notional)
    per_symbol = _tighter(percentage_symbol, policy.per_symbol_notional)
    # The outer bound is the account itself or the authorization, whichever is
    # smaller. The account limb is the no-leverage rule stated as a number:
    # exposure may never exceed money actually held.
    exposure = _tighter(verified_equity, policy.capital_bound)
    slot = target / Decimal(policy.universe_size)
    if policy.per_symbol_notional is not None:
        slot = min(slot, policy.per_symbol_notional)

    binding = (
        "balance"
        if policy.target_gross_notional is None or percentage_target <= policy.target_gross_notional
        else "authorization"
    )
    return LiveExposureCeilings(
        verified_equity=_floor_cent(verified_equity),
        target_gross=_floor_cent(target),
        hard_gross=_floor_cent(hard),
        exposure_bound=_floor_cent(exposure),
        per_symbol=_floor_cent(per_symbol),
        slot=_floor_cent(slot),
        binding=binding,
    )


def _tighter(percentage_limb: Decimal, absolute_limb: Decimal | None) -> Decimal:
    """The binding one. `None` means the policy states no absolute limb."""
    if absolute_limb is None:
        return percentage_limb
    return min(percentage_limb, absolute_limb)


@dataclass(frozen=True)
class CashFlowEvent:
    """One non-trade cash movement the broker reports.

    Deliberately not signed or summed anywhere in this module. The guard below
    does not care whether the account gained or lost cash, or how much: any
    movement at all means the day's equity change is no longer a trading
    result, and that is the only fact the halt needs.
    """

    activity_id: str
    activity_type: str
    amount: Decimal
    transaction_time: datetime

    @property
    def is_cash_flow(self) -> bool:
        return self.activity_type.strip().upper() in CASH_FLOW_ACTIVITY_TYPES


def cash_flow_block_reason(
    events: tuple[CashFlowEvent, ...] | list[CashFlowEvent],
    *,
    risk_day: date,
) -> str | None:
    """Why new entries must stop today, or None when the day is trading-only.

    A cash movement dated to `risk_day` means the UTC-day loss baseline was
    established against a different amount of capital than the account now
    holds, so the daily-loss halt is measuring the deposit rather than the
    trading. New entries stop until the next risk day establishes a fresh
    baseline against the settled balance - or until an operator deliberately
    re-baselines, which is a decision and not a calculation.

    Exits are unaffected. This function is consulted on the entry path only,
    and the risk engine would not consult a halt on an exit in any case.

    Returns a reason rather than a bool because the reason is what an operator
    reads at 09:45 on the morning the deposit lands, and "blocked" without it
    is the kind of message that gets overridden.
    """
    relevant = [
        event
        for event in events
        if event.is_cash_flow and event.transaction_time.date() == risk_day
    ]
    if not relevant:
        return None
    kinds = ", ".join(sorted({event.activity_type.strip().upper() for event in relevant}))
    return (
        f"{len(relevant)} non-trade cash movement(s) ({kinds}) settled on {risk_day.isoformat()}. "
        "The UTC-day loss baseline was taken against a different amount of capital, so "
        "today's equity change is not a trading result and the daily-loss halt would be "
        "measuring the transfer. No new entries today; exits remain available. The next "
        "risk day establishes a fresh baseline against the settled balance."
    )


__all__ = [
    "CASH_FLOW_ACTIVITY_TYPES",
    "CENT",
    "CashFlowEvent",
    "LiveBudgetError",
    "LiveExposureCeilings",
    "cash_flow_block_reason",
    "effective_ceilings",
]
