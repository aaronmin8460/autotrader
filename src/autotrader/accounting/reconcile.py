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
`AVERAGE_COST_TOLERANCE` is agreement at the broker's own precision; outside
it, the two are computing different things and the verdict is `DEGRADED` -
visible, but distinguished from a missing share.

**Nothing here repairs anything.** A reconciliation that silently rewrote the
ledger to match the broker would erase the only record of what the ledger
believed, which is exactly half of what a repair needs to know. It reports, and
stops.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from autotrader.accounting import engine, store
from autotrader.accounting.models import STATUS_MISMATCH, CostBasisState

#: Half the broker's last published digit of `average_entry_price`, widened by
#: an order of magnitude for this ledger's own ten-decimal quantization. The
#: whole-history replay that validated this deployment came in at 5e-8 worst
#: case, twenty times inside it.
AVERAGE_COST_TOLERANCE = Decimal("0.000001")


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


def _worst(statuses: list[str]) -> str:
    for candidate in (store.RECON_MISMATCH, store.RECON_UNKNOWN, store.RECON_DEGRADED):
        if candidate in statuses:
            return candidate
    return store.RECON_CLEAN


def compare(
    local: dict[str, CostBasisState],
    broker: dict[str, BrokerPosition],
    *,
    tolerance: Decimal = AVERAGE_COST_TOLERANCE,
) -> list[SymbolReconciliation]:
    """Compare every symbol either side knows about. Pure - no I/O, no clock.

    A symbol held on one side and absent from the other is compared against
    zero rather than skipped. Skipping it would make "the ledger has never
    heard of this position" look identical to "there is no position", which is
    the single most important difference this function can report.
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

        if state.status == STATUS_MISMATCH or not quantity_matches:
            status = store.RECON_MISMATCH
        elif delta is not None and abs(delta) > tolerance:
            status = store.RECON_DEGRADED
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
        rows = compare(local, broker, tolerance=tolerance)
        mismatches = sum(1 for row in rows if row.status == store.RECON_MISMATCH)
        deviations = sum(1 for row in rows if row.status == store.RECON_DEGRADED)
        status = _worst([row.status for row in rows])
        notes: list[str] = []
        if mismatches:
            notes.append(f"{mismatches} symbol(s) disagree on quantity or are not being tracked")
        if deviations:
            notes.append(f"{deviations} symbol(s) differ on average cost beyond {tolerance}")
        result = ReconciliationResult(
            status=status,
            symbols=tuple(rows),
            symbols_checked=len(rows),
            quantity_mismatches=mismatches,
            cost_deviations=deviations,
            message="; ".join(notes) or None,
            run_at=now,
        )

    if persist:
        _persist(connection, result)
    return result


def _text(value: Decimal | None) -> str | None:
    return None if value is None else store.decimal_text(value)


def _persist(connection: sqlite3.Connection, result: ReconciliationResult) -> int:
    with store.transaction(connection):
        cursor = connection.execute(
            """
            INSERT INTO accounting_reconciliation_runs (
                run_at, status, symbols_checked, quantity_mismatches,
                cost_deviations, message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.run_at.astimezone(UTC).isoformat(),
                result.status,
                result.symbols_checked,
                result.quantity_mismatches,
                result.cost_deviations,
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
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    "BrokerPosition",
    "ReconciliationResult",
    "SymbolReconciliation",
    "compare",
    "latest",
    "latest_symbols",
    "reconcile",
]
