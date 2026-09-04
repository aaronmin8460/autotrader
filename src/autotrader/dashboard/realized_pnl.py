"""The dashboard's read-only view of the realized-P&L ledger.

Three properties, all of them load-bearing:

**It cannot write.** The ledger is opened through a `mode=ro` URI with
`query_only`, so a viewer process cannot create the file, initialize it,
migrate it or edit a row. The accounting synchronizer is the only writer, and
it is a different process.

**A missing ledger is a value, not a traceback.** Before the ledger is
bootstrapped - and if it is ever unreadable - these builders return a panel
whose status is `UNKNOWN` and whose figures are absent. A dashboard that
returned 500 because accounting had not been deployed yet would be worse than
one that says so.

**It never claims more than the ledger does.** The tracking horizon, the
reconciliation verdict and the execution granularity come out of the ledger's
own metadata and are rendered verbatim. Nothing here computes a realized
figure; it reads one.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from autotrader.accounting import readmodel, reconcile, store
from autotrader.accounting.service import (
    ACCOUNTING_DATABASE_PATH_ENV,
    DEFAULT_ACCOUNTING_DATABASE_PATH,
)
from autotrader.dashboard.models import UNAVAILABLE_DATABASE_UNREADABLE

#: How many realized events a page may ask for.
DEFAULT_EVENT_LIMIT = 50
MAX_EVENT_LIMIT = 500


def database_path() -> Path:
    configured = os.environ.get(ACCOUNTING_DATABASE_PATH_ENV)
    return Path(configured) if configured else DEFAULT_ACCOUNTING_DATABASE_PATH


@dataclass(frozen=True)
class RealizedPnlPanel:
    """Realized P&L for the equity book, or a stated reason there is none.

    `available` is the whole contract. When it is False every figure is None
    and `unavailable_reason` says why - there is no partially populated
    success, and no zero standing in for "not known".
    """

    generated_at: str
    available: bool
    unavailable_reason: str | None
    summary: readmodel.RealizedSummary | None
    status: readmodel.AccountingStatusPanel | None
    #: Stated on the wire: daily account P&L, realized P&L and unrealized P&L
    #: are three separate measurements over different windows and are not
    #: required to sum. A renderer asserts this rather than a reader assuming.
    components_are_independent: bool = True


@dataclass(frozen=True)
class SymbolRealizedPanel:
    """One symbol's realized detail, for the drawer."""

    generated_at: str
    available: bool
    unavailable_reason: str | None
    symbol: str
    realized: readmodel.SymbolRealized | None
    events: tuple[readmodel.RealizedEventRow, ...]
    status: readmodel.AccountingStatusPanel | None


@dataclass(frozen=True)
class ReconciliationRow:
    symbol: str
    local_quantity: str
    broker_quantity: str
    quantity_matches: bool
    local_average_cost: str | None
    broker_average_entry: str | None
    average_cost_delta: str | None
    status: str
    #: Present only on a row whose deviation had to be judged. The broker's
    #: implied cost basis, and the range this ledger's own purchase lots can be
    #: relieved down to - so a reader can see *why* a divergence was called
    #: explained rather than being asked to take the word for it.
    broker_implied_basis: str | None = None
    relief_basis_low: str | None = None
    relief_basis_high: str | None = None


def _unreadable_panel(moment: datetime) -> RealizedPnlPanel:
    return RealizedPnlPanel(
        generated_at=moment.astimezone(UTC).isoformat(),
        available=False,
        unavailable_reason=UNAVAILABLE_DATABASE_UNREADABLE,
        summary=None,
        status=None,
    )


def _open(path: Path | None = None):
    return store.connect_read_only(path or database_path())


def build_panel(*, now: datetime | None = None, path: Path | None = None) -> RealizedPnlPanel:
    """The account-level realized figures, or a stated reason there are none."""
    moment = now or datetime.now(UTC)
    try:
        with _open(path) as connection:
            summary = readmodel.build_summary(connection, now=moment)
    except (sqlite3.Error, OSError, store.AccountingStoreError):
        return _unreadable_panel(moment)
    return RealizedPnlPanel(
        generated_at=moment.astimezone(UTC).isoformat(),
        available=True,
        unavailable_reason=None,
        summary=summary,
        status=summary.status,
    )


def build_by_symbol(
    *, now: datetime | None = None, path: Path | None = None
) -> list[readmodel.SymbolRealized]:
    moment = now or datetime.now(UTC)
    try:
        with _open(path) as connection:
            return readmodel.build_by_symbol(connection, now=moment)
    except (sqlite3.Error, OSError, store.AccountingStoreError):
        return []


def build_events(
    *,
    symbol: str | None = None,
    limit: int = DEFAULT_EVENT_LIMIT,
    path: Path | None = None,
) -> list[readmodel.RealizedEventRow]:
    bounded = max(1, min(int(limit), MAX_EVENT_LIMIT))
    try:
        with _open(path) as connection:
            return readmodel.build_events(connection, symbol=symbol, limit=bounded)
    except (sqlite3.Error, OSError, store.AccountingStoreError):
        return []


def build_status(*, path: Path | None = None) -> readmodel.AccountingStatusPanel | None:
    try:
        with _open(path) as connection:
            return readmodel.build_status(connection)
    except (sqlite3.Error, OSError, store.AccountingStoreError):
        return None


def _optional(row: sqlite3.Row, name: str) -> str | None:
    """A column this build knows about, or `None` when the file predates it."""
    try:
        value = row[name]
    except (IndexError, KeyError):
        return None
    return None if value is None else str(value)


def build_reconciliation(*, path: Path | None = None) -> list[ReconciliationRow]:
    """The most recent per-symbol comparison, ledger against broker."""
    try:
        with _open(path) as connection:
            rows = reconcile.latest_symbols(connection)
    except (sqlite3.Error, OSError, store.AccountingStoreError):
        return []
    return [
        ReconciliationRow(
            symbol=str(row["symbol"]),
            local_quantity=str(row["local_quantity"]),
            broker_quantity=str(row["broker_quantity"]),
            quantity_matches=bool(row["quantity_matches"]),
            local_average_cost=row["local_average_cost"],
            broker_average_entry=row["broker_average_entry"],
            average_cost_delta=row["average_cost_delta"],
            status=str(row["status"]),
            broker_implied_basis=_optional(row, "broker_implied_basis"),
            relief_basis_low=_optional(row, "relief_basis_low"),
            relief_basis_high=_optional(row, "relief_basis_high"),
        )
        for row in rows
    ]


def build_symbol_panel(
    symbol: str, *, now: datetime | None = None, path: Path | None = None, limit: int = 50
) -> SymbolRealizedPanel:
    """Everything the symbol drawer shows about realized P&L for one symbol."""
    moment = now or datetime.now(UTC)
    key = symbol.strip().upper()
    try:
        with _open(path) as connection:
            rows = {row.symbol: row for row in readmodel.build_by_symbol(connection, now=moment)}
            events = readmodel.build_events(connection, symbol=key, limit=limit)
            status = readmodel.build_status(connection)
    except (sqlite3.Error, OSError, store.AccountingStoreError):
        return SymbolRealizedPanel(
            generated_at=moment.astimezone(UTC).isoformat(),
            available=False,
            unavailable_reason=UNAVAILABLE_DATABASE_UNREADABLE,
            symbol=key,
            realized=None,
            events=(),
            status=None,
        )
    return SymbolRealizedPanel(
        generated_at=moment.astimezone(UTC).isoformat(),
        available=True,
        unavailable_reason=None,
        symbol=key,
        realized=rows.get(key),
        events=tuple(events),
        status=status,
    )


__all__ = [
    "DEFAULT_EVENT_LIMIT",
    "MAX_EVENT_LIMIT",
    "RealizedPnlPanel",
    "ReconciliationRow",
    "SymbolRealizedPanel",
    "build_by_symbol",
    "build_events",
    "build_panel",
    "build_reconciliation",
    "build_status",
    "build_symbol_panel",
    "database_path",
]
