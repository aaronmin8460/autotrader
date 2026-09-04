"""Ledger against broker. The broker wins; the ledger says so out loud.

This is a **second, independent** reconciliation. The trading runtimes already
reconcile their own orders and positions against the broker and gate trading on
the result; nothing here touches that, reads its verdict, or can influence it.
This one answers a narrower question that only the accounting ledger can be
wrong about: *does the cost basis I have been carrying still describe the
position the broker actually holds?*

**Quantity is the hard test.** Two systems that disagree about how many shares
exist do not have a small rounding difference; one of them has missed an
execution. Any quantity difference is `MISMATCH`, and realized P&L stops being
presentable as authoritative until it is explained.

**Average cost is the soft test**, and deliberately so. The broker publishes
`average_entry_price` rounded to six decimal places, so exact equality is not
available at any tolerance the broker can express. A deviation inside
`AVERAGE_COST_TOLERANCE` is agreement at the broker's own precision.

**Outside it, the deviation is not yet a verdict.** Two systems holding the
same shares, bought at the same prices, can still carry different average costs,
because "which shares did that sale consume?" has more than one defensible
answer. This ledger relieves a sale at the running weighted average. This
account's broker restates `avg_entry_price` after the close, relieving the day's
sales against the inventory carried in from prior days - which moves the
published figure away from the weighted average without either side being wrong
about a single fill. Reported as a bare `DEGRADED`, that difference is
indistinguishable from a missing execution, which is the one thing this function
exists to be able to tell apart.

So a deviation beyond tolerance is *tested* rather than assumed: is the broker's
implied cost basis reachable by relieving **this ledger's own recorded purchase
lots** in some order? If it is, the two sides agree about every share and every
price and differ only in which shares they call sold - `BASIS_DIVERGENCE`,
carrying the exact dollar figure. If it is not, no relief order over these fills
produces the broker's number, something other than method is wrong, and the
verdict is `DEGRADED` exactly as before.

The tolerance is not widened by any of this. What changes is what happens after
it is exceeded.

**Nothing here repairs anything.** A reconciliation that silently rewrote the
ledger to match the broker would erase the only record of what the ledger
believed, which is exactly half of what a repair needs to know. It reports, and
stops.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from autotrader.accounting import engine, store
from autotrader.accounting.models import STATUS_MISMATCH, CostBasisState, ExecutionFill

#: Half the broker's last published digit of `average_entry_price`, widened by
#: an order of magnitude for this ledger's own ten-decimal quantization. The
#: whole-history replay that validated this deployment came in at 5e-8 worst
#: case, twenty times inside it.
#:
#: Deliberately unchanged. A ledger whose agreement threshold moves whenever it
#: disagrees is not reconciling against anything.
AVERAGE_COST_TOLERANCE = Decimal("0.000001")

#: Half the broker's last published digit, per share. `avg_entry_price` arrives
#: rounded to six decimals, so the cost basis implied by it carries up to this
#: much error for every share held - which is what the envelope test has to
#: allow for before calling a difference unexplained.
BROKER_PRICE_HALF_ULP = Decimal("0.0000005")


class BrokerPosition(Protocol):
    """A position as the broker reports it."""

    symbol: str
    quantity: Decimal
    average_entry_price: Decimal


@dataclass(frozen=True)
class SymbolReconciliation:
    """One symbol, both sides, and the verdict on the pair."""

    symbol: str
    local_quantity: Decimal
    broker_quantity: Decimal
    quantity_matches: bool
    local_average_cost: Decimal | None
    broker_average_entry: Decimal | None
    average_cost_delta: Decimal | None
    status: str
    #: `broker_average_entry * broker_quantity`, and the range of cost bases the
    #: ledger's own purchase lots can be relieved down to. Populated only when a
    #: deviation had to be judged; `None` when it never came up.
    broker_implied_basis: Decimal | None = None
    relief_basis_low: Decimal | None = None
    relief_basis_high: Decimal | None = None


@dataclass(frozen=True)
class ReconciliationResult:
    """The account-level verdict, and the rows it was computed from."""

    status: str
    symbols: tuple[SymbolReconciliation, ...]
    symbols_checked: int
    quantity_mismatches: int
    cost_deviations: int
    message: str | None
    run_at: datetime
    basis_divergences: int = 0


def _worst(statuses: list[str]) -> str:
    for candidate in (
        store.RECON_MISMATCH,
        store.RECON_UNKNOWN,
        store.RECON_DEGRADED,
        store.RECON_BASIS_DIVERGENCE,
    ):
        if candidate in statuses:
            return candidate
    return store.RECON_CLEAN


@dataclass(frozen=True)
class DivergenceVerdict:
    """Whether a deviation is explained by lot relief, and the range that says so."""

    explained: bool
    broker_implied_basis: Decimal | None
    relief_basis_low: Decimal | None
    relief_basis_high: Decimal | None


def explain_deviation(
    state: CostBasisState,
    broker_average: Decimal,
    broker_quantity: Decimal,
    fills: Sequence[ExecutionFill] | None,
) -> DivergenceVerdict:
    """Is the broker's cost basis reachable by relieving *these* purchase lots?

    The question is deliberately narrow. It does not ask which method the broker
    used, or try to reproduce it - that would make this ledger's verdict depend
    on reverse-engineering an undocumented field, and change silently the day
    the broker changes. It asks the one thing that can be answered from the
    ledger's own evidence: given these purchases and these sales, is there *any*
    order of lot relief that leaves the cost basis the broker is publishing?

    If yes, the two sides hold the same shares at the same prices and disagree
    only about which of them were sold - a difference in when P&L is recognised,
    with the fills themselves in full agreement. If no, no accounting method
    explains it and something else is wrong.

    `fills` is `None` when the caller could not supply the history. That is not
    an explanation, and it is not treated as one.
    """
    if not fills:
        return DivergenceVerdict(False, None, None, None)
    envelope = engine.relief_envelope(fills)
    if envelope is None or envelope.quantity != state.quantity:
        # The stored fills do not fold to the stored position. Whatever the
        # deviation is, it is not a question about relief order.
        return DivergenceVerdict(False, None, None, None)

    implied = broker_average * broker_quantity
    # `broker_average` is published to six decimals, so the basis it implies is
    # uncertain by half a unit of that last digit for every share held. Allowing
    # for the broker's own precision is not a tolerance on the answer; it is a
    # tolerance on the question.
    slack = BROKER_PRICE_HALF_ULP * broker_quantity
    explained = (envelope.low - slack) <= implied <= (envelope.high + slack)
    return DivergenceVerdict(explained, implied, envelope.low, envelope.high)


def compare(
    local: dict[str, CostBasisState],
    broker: dict[str, BrokerPosition],
    *,
    tolerance: Decimal = AVERAGE_COST_TOLERANCE,
    fills: Mapping[str, Sequence[ExecutionFill]] | None = None,
) -> list[SymbolReconciliation]:
    """Compare every symbol either side knows about. Pure - no I/O, no clock.

    A symbol held on one side and absent from the other is compared against
    zero rather than skipped. Skipping it would make "the ledger has never
    heard of this position" look identical to "there is no position", which is
    the single most important difference this function can report.

    `fills` is the ledger's own execution history per symbol. Without it a
    deviation beyond tolerance can only be reported as unexplained, so omitting
    it is safe in the direction that matters: the verdict gets worse, never
    better.
    """
    rows: list[SymbolReconciliation] = []
    for symbol in sorted(set(local) | set(broker)):
        state = local.get(symbol) or CostBasisState.flat(symbol)
        held = broker.get(symbol)
        broker_quantity = held.quantity if held is not None else Decimal(0)
        broker_average = held.average_entry_price if held is not None else None
        local_average = engine.average_cost(state)

        quantity_matches = state.quantity == broker_quantity
        delta: Decimal | None = None
        if local_average is not None and broker_average is not None:
            delta = local_average - broker_average

        verdict = DivergenceVerdict(False, None, None, None)
        if state.status == STATUS_MISMATCH or not quantity_matches:
            status = store.RECON_MISMATCH
        elif delta is not None and abs(delta) > tolerance:
            # Quantity agrees exactly and the averages do not. Before calling
            # that a fault, ask whether lot relief accounts for all of it.
            verdict = explain_deviation(
                state,
                broker_average,  # type: ignore[arg-type]  - non-None with delta
                broker_quantity,
                None if fills is None else fills.get(symbol),
            )
            status = store.RECON_BASIS_DIVERGENCE if verdict.explained else store.RECON_DEGRADED
        elif state.quantity > 0 and broker_average is None:
            status = store.RECON_UNKNOWN
        else:
            status = store.RECON_CLEAN

        rows.append(
            SymbolReconciliation(
                symbol=symbol,
                local_quantity=state.quantity,
                broker_quantity=broker_quantity,
                quantity_matches=quantity_matches,
                local_average_cost=local_average,
                broker_average_entry=broker_average,
                average_cost_delta=delta,
                status=status,
                broker_implied_basis=verdict.broker_implied_basis,
                relief_basis_low=verdict.relief_basis_low,
                relief_basis_high=verdict.relief_basis_high,
            )
        )
    return rows


def reconcile(
    connection: sqlite3.Connection,
    broker: dict[str, BrokerPosition] | None,
    *,
    now: datetime,
    tolerance: Decimal = AVERAGE_COST_TOLERANCE,
    persist: bool = True,
) -> ReconciliationResult:
    """Compare the stored ledger against a broker snapshot and record the run.

    `broker=None` means the broker could not be read. That is `UNKNOWN`, never
    `CLEAN`: an unread broker has not agreed with anything.
    """
    local = store.read_all_cost_basis(connection)

    if broker is None:
        result = ReconciliationResult(
            status=store.RECON_UNKNOWN,
            symbols=(),
            symbols_checked=0,
            quantity_mismatches=0,
            cost_deviations=0,
            message="The broker could not be read; the ledger was not confirmed against anything.",
            run_at=now,
        )
    else:
        rows = compare(
            local, broker, tolerance=tolerance, fills=store.read_fills_by_symbol(connection)
        )
        mismatches = sum(1 for row in rows if row.status == store.RECON_MISMATCH)
        deviations = sum(1 for row in rows if row.status == store.RECON_DEGRADED)
        divergences = sum(1 for row in rows if row.status == store.RECON_BASIS_DIVERGENCE)
        status = _worst([row.status for row in rows])
        notes: list[str] = []
        if mismatches:
            notes.append(f"{mismatches} symbol(s) disagree on quantity or are not being tracked")
        if deviations:
            notes.append(
                f"{deviations} symbol(s) differ on average cost beyond {tolerance} by an "
                "amount no lot-relief order over the recorded fills accounts for"
            )
        if divergences:
            timing = sum(
                (
                    row.broker_implied_basis - _basis(local, row.symbol)
                    for row in rows
                    if row.status == store.RECON_BASIS_DIVERGENCE
                    and row.broker_implied_basis is not None
                ),
                Decimal(0),
            )
            notes.append(
                f"{divergences} symbol(s) hold the same shares at the same prices as the "
                "broker and relieve sold lots differently: the broker's own cost basis "
                f"implies {timing:+.6f} of realized P&L to date against this ledger's, "
                "recognised on a different schedule; no fill is in dispute"
            )
        result = ReconciliationResult(
            status=status,
            symbols=tuple(rows),
            symbols_checked=len(rows),
            quantity_mismatches=mismatches,
            cost_deviations=deviations,
            basis_divergences=divergences,
            message="; ".join(notes) or None,
            run_at=now,
        )

    if persist:
        _persist(connection, result)
    return result


def _text(value: Decimal | None) -> str | None:
    return None if value is None else store.decimal_text(value)


def _basis(local: dict[str, CostBasisState], symbol: str) -> Decimal:
    state = local.get(symbol)
    return Decimal(0) if state is None else state.total_cost_basis


# `broker_implied_basis - ledger_basis` is exactly `broker_realized -
# ledger_realized`, because `basis - realized == sum of buy notional - sum of
# sale proceeds` under every cost-basis method and both sides hold the same
# fills. A positive figure therefore means the broker has recognised that much
# more realized P&L to date than this ledger has - not that money is missing.


def _persist(connection: sqlite3.Connection, result: ReconciliationResult) -> int:
    with store.transaction(connection):
        cursor = connection.execute(
            """
            INSERT INTO accounting_reconciliation_runs (
                run_at, status, symbols_checked, quantity_mismatches,
                cost_deviations, basis_divergences, message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.run_at.astimezone(UTC).isoformat(),
                result.status,
                result.symbols_checked,
                result.quantity_mismatches,
                result.cost_deviations,
                result.basis_divergences,
                result.message,
                result.run_at.astimezone(UTC).isoformat(),
            ),
        )
        run_id = int(cursor.lastrowid or 0)
        for row in result.symbols:
            connection.execute(
                """
                INSERT INTO accounting_reconciliation_symbols (
                    run_id, symbol, local_quantity, broker_quantity, quantity_matches,
                    local_average_cost, broker_average_entry, average_cost_delta,
                    status, broker_implied_basis, relief_basis_low, relief_basis_high,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row.symbol,
                    store.decimal_text(row.local_quantity),
                    store.decimal_text(row.broker_quantity),
                    1 if row.quantity_matches else 0,
                    _text(row.local_average_cost),
                    _text(row.broker_average_entry),
                    _text(row.average_cost_delta),
                    row.status,
                    _text(row.broker_implied_basis),
                    _text(row.relief_basis_low),
                    _text(row.relief_basis_high),
                    result.run_at.astimezone(UTC).isoformat(),
                ),
            )
    return run_id


def latest(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM accounting_reconciliation_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def latest_symbols(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    run = latest(connection)
    if run is None:
        return []
    return list(
        connection.execute(
            "SELECT * FROM accounting_reconciliation_symbols WHERE run_id = ? ORDER BY symbol",
            (int(run["id"]),),
        )
    )


__all__ = [
    "AVERAGE_COST_TOLERANCE",
    "BROKER_PRICE_HALF_ULP",
    "BrokerPosition",
    "DivergenceVerdict",
    "ReconciliationResult",
    "SymbolReconciliation",
    "compare",
    "explain_deviation",
    "latest",
    "latest_symbols",
    "reconcile",
]
