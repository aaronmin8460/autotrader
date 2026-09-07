"""The accounting arithmetic. Pure, total, and the only place any of it is written.

No I/O, no clock, no broker, no database. Given a baseline, a flow ledger, a
checkpoint series and a reserve policy, this module produces the entire derived
state - and produces it the same way every time, from scratch, in one pass.

That property is not a nicety. It is what makes the whole design rebuildable:
there is no incrementally-maintained high-water mark to corrupt, no running
bucket total that could drift from its own history, and no state that a
backdated activity could invalidate. A flow that arrives late is inserted into
the ledger and the answer is recomputed, which puts it in its correct place in
time by construction rather than by a repair path somebody has to remember to
write.

The one invariant everything else serves:

    a deposit is not a profit, and a withdrawal is not a loss.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from autotrader.liveaccounting.models import (
    CHECKPOINT_INCEPTION,
    FLOW_EXTERNAL_DEPOSIT,
    FLOW_EXTERNAL_WITHDRAWAL,
    FLOW_UNKNOWN_EXTERNAL,
    RESERVE_EVENT_ACCRUAL,
    RESERVE_EVENT_WITHDRAWAL_ALLOCATION,
    TWR_CONVENTION_CONSERVATIVE_MIN,
    ZERO,
    EquityCheckpoint,
    ExternalFlow,
    LiveAccountingInputError,
    ProfitReservePolicy,
    ReserveEvent,
    floor_cent,
)

#: Ratios are reported at ten decimal places, floored. Flooring a return - in
#: both directions - is the conservative choice: it can understate a gain and
#: can overstate a drawdown, and never the reverse.
RATIO_QUANTUM = Decimal("0.0000000001")


def _floor_ratio(value: Decimal) -> Decimal:
    return value.quantize(RATIO_QUANTUM, rounding=ROUND_FLOOR)


# --------------------------------------------------------------------------
# External flows
# --------------------------------------------------------------------------


def net_external_flows_at(flows: tuple[ExternalFlow, ...], moment: datetime) -> Decimal:
    """Confirmed since-inception external flows settled at or before `moment`, netted.

    Signed: positive means capital was added on net. Pending money, void
    activities, unknown activities and the pre-inception funding all contribute
    exactly nothing, which is the whole point of the four categories.
    """
    return sum(
        (flow.amount for flow in flows if flow.counts_for_performance and flow.settle_at <= moment),
        ZERO,
    )


def confirmed_deposits(flows: tuple[ExternalFlow, ...]) -> Decimal:
    return sum(
        (
            flow.amount
            for flow in flows
            if flow.counts_for_performance and flow.classification == FLOW_EXTERNAL_DEPOSIT
        ),
        ZERO,
    )


def confirmed_withdrawals(flows: tuple[ExternalFlow, ...]) -> Decimal:
    """As a positive magnitude, though the ledger stores withdrawals negative."""
    return -sum(
        (
            flow.amount
            for flow in flows
            if flow.counts_for_performance and flow.classification == FLOW_EXTERNAL_WITHDRAWAL
        ),
        ZERO,
    )


def flow_adjusted_equity(broker_equity: Decimal, net_flows: Decimal) -> Decimal:
    """Broker equity with external capital taken back out.

    THE canonical quantity. A $50 deposit raises broker equity by $50 and
    raises net flows by $50, so this figure does not move - which is the
    arithmetic statement of "money moving in is not profit".
    """
    return broker_equity - net_flows


# --------------------------------------------------------------------------
# Time-weighted return
# --------------------------------------------------------------------------


def subperiod_return(
    *, begin_equity: Decimal, end_equity: Decimal, net_flow: Decimal
) -> Decimal | None:
    """One sub-period's return, bounded conservatively when a flow occurred.

    The two standard endpoint assumptions for a flow of `F` inside a period
    running from `B` to `E`:

        flow at the end     (E - F) / B      - 1
        flow at the start    E / (B + F)     - 1

    and this returns the **lower of the two**. Neither assumption is wrong;
    each one flatters a different direction of money movement. A deposit that
    arrived at noon and rode an afternoon rally is credited entirely to the
    manager by the flow-at-end limb. A withdrawal taken after a morning gain is
    credited entirely to the manager by the flow-at-start limb. Taking the
    minimum means no transfer, in either direction, can make the strategy look
    better than it was.

    Two properties follow directly, and the suite pins both:

    * `F = 0` makes the limbs identical, so an ordinary sub-period is
      undistorted - this convention costs nothing when nothing moved;
    * `E = B + F` (the market did not move) makes both limbs exactly zero, so a
      deposit alone, or a withdrawal alone, moves the return by nothing at all.

    Returns None when neither base is a positive amount of money, which is not
    a return of zero - it is a sub-period whose return is undefined, and the
    caller drops it from the chain rather than pretending it was flat.
    """
    flow_at_end = None
    if begin_equity > ZERO:
        flow_at_end = (end_equity - net_flow) / begin_equity - 1

    flow_at_start = None
    start_base = begin_equity + net_flow
    if start_base > ZERO:
        flow_at_start = end_equity / start_base - 1

    candidates = [limb for limb in (flow_at_end, flow_at_start) if limb is not None]
    if not candidates:
        return None
    return min(candidates)


@dataclass(frozen=True)
class TwrResult:
    value: Decimal | None
    subperiods: int
    bounded_subperiods: int
    convention: str = TWR_CONVENTION_CONSERVATIVE_MIN


def time_weighted_return(
    checkpoints: tuple[EquityCheckpoint, ...], flows: tuple[ExternalFlow, ...]
) -> TwrResult:
    """The chained sub-period return since inception. Flow-neutral by construction.

    Sub-periods run between consecutive checkpoints of any kind - an intraday
    read is a perfectly good boundary for a *return*, even though it is not
    good enough to set a high-water mark. Denser checkpoints make the bound
    tighter, and a flow landing exactly on a boundary makes it exact.
    """
    ordered = tuple(sorted(checkpoints, key=lambda point: point.taken_at))
    if len(ordered) < 2:
        return TwrResult(value=None, subperiods=0, bounded_subperiods=0)

    counted = [flow for flow in flows if flow.counts_for_performance]
    product = Decimal(1)
    subperiods = 0
    bounded = 0
    for previous, current in zip(ordered, ordered[1:], strict=False):
        net_flow = sum(
            (
                flow.amount
                for flow in counted
                if previous.taken_at < flow.settle_at <= current.taken_at
            ),
            ZERO,
        )
        step = subperiod_return(
            begin_equity=previous.broker_equity,
            end_equity=current.broker_equity,
            net_flow=net_flow,
        )
        if step is None:
            continue
        product *= Decimal(1) + step
        subperiods += 1
        if net_flow != ZERO:
            bounded += 1

    if subperiods == 0:
        return TwrResult(value=None, subperiods=0, bounded_subperiods=0)
    return TwrResult(
        value=_floor_ratio(product - 1), subperiods=subperiods, bounded_subperiods=bounded
    )


# --------------------------------------------------------------------------
# High-water mark, reserve and bucket
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReserveResult:
    adjusted_equity_hwm: Decimal
    adjusted_equity_hwm_at: datetime | None
    observed_intraday_peak: Decimal | None
    new_hwm_profit: Decimal
    profit_reserve_accrued: Decimal
    withdrawal_bucket_balance: Decimal
    unreserved_withdrawal_total: Decimal
    events: tuple[ReserveEvent, ...] = field(default=())


def evaluate_reserve(
    *,
    baseline_equity: Decimal,
    checkpoints: tuple[EquityCheckpoint, ...],
    flows: tuple[ExternalFlow, ...],
    policy: ProfitReservePolicy,
) -> ReserveResult:
    """Replay the whole history of the high-water mark and the virtual bucket.

    One merged timeline: authoritative checkpoints, which may raise the mark
    and accrue reserve, and confirmed withdrawals, which may draw the bucket
    down. Replayed in time order from the baseline every time, so the answer
    depends only on the ledger's contents and never on the order the ledger was
    written in.

    **Only completed checkpoints may raise the mark.** Intraday reads are
    tracked separately as an observed peak and confer nothing: a print at 11:04
    that is gone by the close must not create a permanent entitlement to money.

    **Only growth above the previous mark is new profit.** Recovering to an old
    mark accrues nothing, because that profit was already counted the first
    time the account reached it.
    """
    ordered = sorted(checkpoints, key=lambda point: point.taken_at)
    counted_flows = [flow for flow in flows if flow.counts_for_performance]
    withdrawals = sorted(
        (flow for flow in counted_flows if flow.classification == FLOW_EXTERNAL_WITHDRAWAL),
        key=lambda flow: flow.settle_at,
    )

    def net_at(moment: datetime) -> Decimal:
        return sum((flow.amount for flow in counted_flows if flow.settle_at <= moment), ZERO)

    # (moment, order-within-instant, kind, payload). A withdrawal settling at
    # the same instant as a checkpoint is applied after it: the checkpoint
    # describes the account before the money left.
    timeline: list[tuple[datetime, int, str, object]] = []
    for point in ordered:
        timeline.append((point.taken_at, 0, "CHECKPOINT", point))
    for flow in withdrawals:
        timeline.append((flow.settle_at, 1, "WITHDRAWAL", flow))
    timeline.sort(key=lambda row: (row[0], row[1]))

    reserve_hwm = baseline_equity
    hwm = baseline_equity
    hwm_at: datetime | None = None
    intraday_peak: Decimal | None = None
    accrued = ZERO
    bucket = ZERO
    unreserved = ZERO
    last_new_profit = ZERO
    events: list[ReserveEvent] = []

    for moment, _, kind, payload in timeline:
        if kind == "CHECKPOINT":
            point = payload  # type: ignore[assignment]
            adjusted = flow_adjusted_equity(point.broker_equity, net_at(moment))  # type: ignore[attr-defined]
            if intraday_peak is None or adjusted > intraday_peak:
                intraday_peak = adjusted
            if not point.is_authoritative:  # type: ignore[attr-defined]
                continue
            if adjusted > hwm:
                hwm, hwm_at = adjusted, moment
            elif hwm_at is None and point.kind == CHECKPOINT_INCEPTION:  # type: ignore[attr-defined]
                hwm_at = moment
            new_profit = adjusted - reserve_hwm
            last_new_profit = new_profit if new_profit > ZERO else ZERO
            if new_profit <= ZERO:
                continue
            accrual = floor_cent(policy.reserve_rate * new_profit)
            prior = reserve_hwm
            reserve_hwm = adjusted
            if accrual <= ZERO:
                continue
            before = bucket
            bucket += accrual
            accrued += accrual
            events.append(
                ReserveEvent(
                    event_type=RESERVE_EVENT_ACCRUAL,
                    occurred_at=moment,
                    prior_reserve_hwm=prior,
                    new_reserve_hwm=reserve_hwm,
                    new_hwm_profit=new_profit,
                    accrual_amount=accrual,
                    withdrawal_amount=ZERO,
                    allocated_from_bucket=ZERO,
                    unreserved_amount=ZERO,
                    bucket_before=before,
                    bucket_after=bucket,
                )
            )
            continue

        flow = payload  # type: ignore[assignment]
        magnitude = -flow.amount  # type: ignore[attr-defined]
        allocated = min(bucket, magnitude)
        excess = magnitude - allocated
        before = bucket
        bucket -= allocated
        unreserved += excess
        events.append(
            ReserveEvent(
                event_type=RESERVE_EVENT_WITHDRAWAL_ALLOCATION,
                occurred_at=moment,
                prior_reserve_hwm=reserve_hwm,
                new_reserve_hwm=reserve_hwm,
                new_hwm_profit=ZERO,
                accrual_amount=ZERO,
                withdrawal_amount=magnitude,
                allocated_from_bucket=allocated,
                unreserved_amount=excess,
                bucket_before=before,
                bucket_after=bucket,
            )
        )

    return ReserveResult(
        adjusted_equity_hwm=hwm,
        adjusted_equity_hwm_at=hwm_at,
        observed_intraday_peak=intraday_peak,
        new_hwm_profit=last_new_profit,
        profit_reserve_accrued=accrued,
        withdrawal_bucket_balance=bucket,
        unreserved_withdrawal_total=unreserved,
        events=tuple(events),
    )


def current_drawdown(adjusted: Decimal, hwm: Decimal) -> Decimal | None:
    """`adjusted / hwm - 1`. Zero at the mark, negative below it, never positive.

    A withdrawal cannot create drawdown and a deposit cannot erase it, because
    both sides of the ratio are flow-adjusted.
    """
    if hwm <= ZERO:
        return None
    return _floor_ratio(adjusted / hwm - 1)


# --------------------------------------------------------------------------
# Harvest
# --------------------------------------------------------------------------


def month_start(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month_start(moment: datetime) -> datetime:
    start = month_start(moment)
    return month_start(start + timedelta(days=32))


def harvest_eligible(*, inception_at: datetime, latest_authoritative_at: datetime | None) -> bool:
    """Whether a whole calendar month has completed since inception.

    Reserve accrual is continuous; harvesting is monthly. The distinction is
    what keeps this system from producing a daily transfer recommendation,
    which is not what the operator wants and is not what a $50 book should be
    doing anyway.

    True only once an authoritative checkpoint lands in a **later** UTC month
    than inception, so the first month is unambiguously observation.
    """
    if latest_authoritative_at is None:
        return False
    return month_start(latest_authoritative_at) > month_start(inception_at)


def withdrawal_preview(
    *,
    bucket_balance: Decimal,
    adjusted: Decimal,
    hwm: Decimal,
    eligible: bool,
) -> Decimal:
    """The informational preview. Zero in drawdown, whatever the bucket holds.

    Nobody should harvest out of a drawdown, and a number on a screen suggesting
    they could is how it happens. The bucket stays recorded; availability
    returns when the account is back at its mark and a harvest checkpoint has
    occurred.
    """
    if not eligible:
        return ZERO
    if adjusted < hwm:
        return ZERO
    return max(ZERO, bucket_balance)


def require_decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise LiveAccountingInputError(
            f"{name} must be an exact, finite Decimal number of USD, got {value!r}"
        )
    return value


def unknown_flow_count(flows: tuple[ExternalFlow, ...]) -> int:
    return sum(1 for flow in flows if flow.classification == FLOW_UNKNOWN_EXTERNAL)


__all__ = [
    "RATIO_QUANTUM",
    "ReserveResult",
    "TwrResult",
    "confirmed_deposits",
    "confirmed_withdrawals",
    "current_drawdown",
    "evaluate_reserve",
    "flow_adjusted_equity",
    "harvest_eligible",
    "month_start",
    "net_external_flows_at",
    "next_month_start",
    "require_decimal",
    "subperiod_return",
    "time_weighted_return",
    "unknown_flow_count",
    "withdrawal_preview",
]
