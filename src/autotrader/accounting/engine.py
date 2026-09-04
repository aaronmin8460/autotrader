"""The cost-basis engine: one state, one confirmed execution, one new state.

Pure. No network, no broker client, no database, no clock, no configuration.
Everything it needs arrives as an argument and everything it decides comes back
as a return value, which is what makes the sixty-odd cases in the test suite
cheap enough to actually write.

**Weighted-average cost, long-only.**

A purchase adds quantity and adds cost, and releases nothing:

    quantity      += fill.quantity
    total_basis   += fill.quantity * fill.price + fill.fees

Both are additions of exact Decimals, so a purchase introduces **no rounding
whatsoever**. This is the reason `total_cost_basis` is the stored figure and
the average is derived: the alternative - keeping the average and recomputing
the total - divides on every purchase and accumulates error in the one number
that has to still agree with the broker a thousand fills later.

A sale releases a proportional slice of the basis:

    released      = total_basis * sell_qty / prior_qty
    proceeds      = sell_qty * price
    gross_pnl     = proceeds - released
    net_pnl       = gross_pnl - fees
    quantity     -= sell_qty
    total_basis  -= released

and leaves the average cost of the remaining shares **unchanged**, which is the
defining property of this method and the one the property tests assert
directly.

**The full-exit case is exact, not nearly exact.** When a sale takes the whole
position, `released` is the entire remaining basis by construction rather than
by a division that happens to come out even. So a position that is opened,
partially trimmed any number of times and finally closed releases exactly its
original total cost basis - no residual dust, no symbol left holding four
ten-billionths of a dollar.

**Rounding.** The only division is the partial-sale slice, quantized to
`BASIS_QUANTUM` (ten decimal places, banker's rounding). Ten places on a
dollar figure is four orders of magnitude finer than the cent this is ever
displayed at, and the quantized value is what gets subtracted from the stored
basis, so the ledger's arithmetic closes exactly rather than approximately.
Intermediate values are never rounded to cents.

**Fail closed.** A sale larger than the tracked position is not a small
discrepancy to absorb. Long-only inventory cannot go negative, so the engine
raises rather than inventing a short, and the caller stops accounting for that
symbol until a human has looked at it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from autotrader.accounting.models import (
    ACCOUNTING_VERSION,
    SIDE_BUY,
    STATUS_MISMATCH,
    STATUS_TRACKING,
    AppliedFill,
    CostBasisState,
    ExecutionFill,
    NegativeInventoryError,
    RealizedEvent,
    SymbolNotTrackedError,
)

#: The precision the stored money figures are held to. Ten decimal places on a
#: dollar - far finer than any price or fee the broker publishes, so quantizing
#: to it discards nothing that was ever measured.
BASIS_QUANTUM = Decimal("0.0000000001")

#: Derived averages are reported to the same precision. `average_cost` is a
#: quotient, so unlike the stored basis it has no exact form to preserve.
AVERAGE_QUANTUM = BASIS_QUANTUM

#: Working precision for the two divisions. Thirty-four significant digits is
#: the IEEE decimal128 figure: wide enough that the quantization below is the
#: only place a digit is ever dropped. Applied through `localcontext`, never by
#: touching the process-wide context - a library that reaches into
#: `getcontext()` changes arithmetic for every other caller in the process.
_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)


def _quantize(value: Decimal, quantum: Decimal = BASIS_QUANTUM) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_HALF_EVEN, context=_CONTEXT)


def average_cost(state: CostBasisState) -> Decimal | None:
    """The per-share cost of what is held, or `None` when nothing is held.

    `None` rather than zero. A flat position has no average cost, and zero is a
    number a caller can accidentally do arithmetic with.
    """
    if state.quantity == 0:
        return None
    with localcontext(_CONTEXT):
        return _quantize(state.total_cost_basis / state.quantity, AVERAGE_QUANTUM)


def apply_fill(state: CostBasisState, fill: ExecutionFill) -> AppliedFill:
    """Fold one broker-confirmed execution into one symbol's cost basis.

    Raises `SymbolNotTrackedError` if accounting for the symbol has been
    stopped, and `NegativeInventoryError` if the sale is larger than the
    position. Neither leaves a partially updated state behind: this function
    returns a new state or it raises, and there is nothing in between.

    **On the duplicate check.** Re-offering the execution the state was last
    advanced by returns that state unchanged, flagged `duplicate`. That covers
    exactly one case - a process that died between writing a fill and being
    sure the write landed, and is now retrying - and it is deliberately not
    presented as general idempotency. A pure function holding one symbol's
    current state cannot know whether some execution from last Tuesday has
    been applied before. Exactly-once against the *whole* history is enforced
    where the whole history lives: a UNIQUE constraint in the store, and the
    seen-set in `replay`.
    """
    if fill.symbol != state.symbol:
        raise SymbolNotTrackedError(
            f"Execution for {fill.symbol} offered against {state.symbol}'s cost basis."
        )
    if state.status != STATUS_TRACKING:
        raise SymbolNotTrackedError(
            f"{state.symbol} is {state.status}: accounting for it stopped at "
            f"execution {state.last_execution_id}. It will not resume until the "
            "discrepancy is reconciled."
        )
    if fill.execution_id == state.last_execution_id:
        return AppliedFill(state=state, realized=None, duplicate=True)

    if fill.side == SIDE_BUY:
        return _apply_buy(state, fill)
    return _apply_sell(state, fill)


def _apply_buy(state: CostBasisState, fill: ExecutionFill) -> AppliedFill:
    """Add shares and add cost. No division, no rounding, no realized event."""
    new_state = CostBasisState(
        symbol=state.symbol,
        quantity=state.quantity + fill.quantity,
        total_cost_basis=state.total_cost_basis + fill.gross_notional + fill.fees,
        status=STATUS_TRACKING,
        last_execution_id=fill.execution_id,
    )
    return AppliedFill(state=new_state, realized=None)


def _apply_sell(state: CostBasisState, fill: ExecutionFill) -> AppliedFill:
    """Release a proportional slice of the basis and record what it realized."""
    if fill.quantity > state.quantity:
        raise NegativeInventoryError(
            f"{state.symbol}: a confirmed sale of {fill.quantity} against a tracked "
            f"position of {state.quantity} would leave {state.quantity - fill.quantity}. "
            "This book is long-only, so that is not a rounding difference - the "
            "ledger's picture of the position is wrong. Execution "
            f"{fill.execution_id} was not applied."
        )

    # Reachable only with `state.quantity >= fill.quantity > 0`, so the
    # position is non-empty and the average is a real number.
    prior_average = average_cost(state)
    if prior_average is None:  # pragma: no cover - guarded by the check above
        raise NegativeInventoryError(f"{state.symbol}: a sale was offered against a flat position.")

    with localcontext(_CONTEXT):
        full_exit = fill.quantity == state.quantity
        released = (
            state.total_cost_basis
            if full_exit
            else _quantize(state.total_cost_basis * fill.quantity / state.quantity)
        )
        proceeds = _quantize(fill.gross_notional)
        gross_pnl = _quantize(proceeds - released)
        net_pnl = _quantize(gross_pnl - fill.fees)
        remaining_quantity = state.quantity - fill.quantity
        remaining_basis = Decimal(0) if full_exit else state.total_cost_basis - released

    new_state = CostBasisState(
        symbol=state.symbol,
        quantity=remaining_quantity,
        total_cost_basis=remaining_basis,
        status=STATUS_TRACKING,
        last_execution_id=fill.execution_id,
    )
    realized = RealizedEvent(
        execution_id=fill.execution_id,
        order_id=fill.order_id,
        symbol=fill.symbol,
        quantity=fill.quantity,
        execution_price=fill.price,
        average_cost_before=prior_average,
        released_cost_basis=released,
        gross_proceeds=proceeds,
        gross_realized_pnl=gross_pnl,
        fees=fill.fees,
        net_realized_pnl=net_pnl,
        quantity_before=state.quantity,
        quantity_after=remaining_quantity,
        average_cost_after=average_cost(new_state),
        realized_at=fill.executed_at,
        provenance=fill.provenance,
        accounting_version=ACCOUNTING_VERSION,
    )
    return AppliedFill(state=new_state, realized=realized)


@dataclass(frozen=True)
class ReliefEnvelope:
    """The range of cost bases a fill stream can leave behind.

    `low` is what survives if every sale is relieved against the dearest share
    then available, `high` if against the cheapest. Both are reachable; so is
    everything between them, and nothing outside them.
    """

    quantity: Decimal
    low: Decimal
    high: Decimal


def relief_envelope(fills: Sequence[ExecutionFill]) -> ReliefEnvelope | None:
    """The lowest and highest cost basis any lot-relief order can leave.

    Two systems that hold the same shares, bought at the same prices, can still
    carry different cost bases, because "which shares did that sale consume?"
    has more than one defensible answer - weighted average, FIFO, LIFO, or the
    day-carry convention this account's broker restates to overnight. What they
    cannot do is disagree by more than the choice of answer allows.

    This function measures that allowance. It relieves the same sales twice
    over the same purchases: once always taking the dearest lot on hand, which
    leaves the cheapest possible inventory, and once always taking the cheapest,
    which leaves the dearest. Every admissible method lands between the two.

    **Chronological, not sorted.** Each sale may only consume shares that had
    already been purchased when it happened, so both books are advanced fill by
    fill in the order given. A time-blind envelope would be wider, and would
    accept a basis no real sequence of trades could have produced.

    Returns `None` when the stream is not a usable inventory history - a sale
    larger than the position, or nothing held at the end. Neither is a range to
    reason about, and neither should be quietly reported as agreement.

    Exact throughout: only additions, subtractions and multiplications of
    Decimals. There is no division here and so no rounding, which is what lets
    the caller compare the result to a broker figure and attribute every
    remaining difference to the broker's own published precision.
    """
    dearest: list[list[Decimal]] = []
    cheapest: list[list[Decimal]] = []
    quantity = Decimal(0)
    for fill in fills:
        if fill.side == SIDE_BUY:
            quantity += fill.quantity
            dearest.append([fill.quantity, fill.price])
            cheapest.append([fill.quantity, fill.price])
            continue
        if fill.quantity > quantity:
            return None
        quantity -= fill.quantity
        _relieve(dearest, fill.quantity, take_dearest=True)
        _relieve(cheapest, fill.quantity, take_dearest=False)
    if quantity <= 0:
        return None
    return ReliefEnvelope(
        quantity=quantity,
        low=sum((lot[0] * lot[1] for lot in dearest), Decimal(0)),
        high=sum((lot[0] * lot[1] for lot in cheapest), Decimal(0)),
    )


def _relieve(lots: list[list[Decimal]], quantity: Decimal, *, take_dearest: bool) -> None:
    """Consume `quantity` from `lots`, always from the extreme price on hand."""
    remaining = quantity
    while remaining > 0 and lots:
        index = (max if take_dearest else min)(
            range(len(lots)), key=lambda position: lots[position][1]
        )
        taken = min(remaining, lots[index][0])
        lots[index][0] -= taken
        remaining -= taken
        if lots[index][0] == 0:
            lots.pop(index)


def mark_mismatch(state: CostBasisState, *, at_execution_id: str | None = None) -> CostBasisState:
    """Stop accounting for a symbol, preserving the last state it was sure of.

    The stored quantity and basis are **not** adjusted to match whatever
    contradicted them. Overwriting the ledger to agree with the broker would
    destroy the only evidence of what the ledger thought, which is the half of
    the discrepancy a repair actually needs.
    """
    return CostBasisState(
        symbol=state.symbol,
        quantity=state.quantity,
        total_cost_basis=state.total_cost_basis,
        status=STATUS_MISMATCH,
        last_execution_id=at_execution_id or state.last_execution_id,
    )


def replay(
    fills: list[ExecutionFill], *, initial: dict[str, CostBasisState] | None = None
) -> tuple[dict[str, CostBasisState], list[RealizedEvent]]:
    """Fold a whole sequence of executions in the order given.

    The caller sorts. This function does not reorder its input, because
    "chronological" is a property of the source data that the ingestion layer
    establishes and proves, not something a fold should quietly impose on a
    list it was handed.

    Duplicate `execution_id`s are ignored - each one is applied at most once,
    however many times it appears.
    """
    states: dict[str, CostBasisState] = dict(initial or {})
    events: list[RealizedEvent] = []
    seen: set[str] = set()
    for fill in fills:
        if fill.execution_id in seen:
            continue
        seen.add(fill.execution_id)
        state = states.get(fill.symbol) or CostBasisState.flat(fill.symbol)
        applied = apply_fill(state, fill)
        states[fill.symbol] = applied.state
        if applied.realized is not None:
            events.append(applied.realized)
    return states, events


__all__ = [
    "AVERAGE_QUANTUM",
    "BASIS_QUANTUM",
    "ReliefEnvelope",
    "apply_fill",
    "average_cost",
    "mark_mismatch",
    "relief_envelope",
    "replay",
]
