"""Read-only-broker orchestration for the production Live accounting ledger.

The broker is only observed. Mutations in this module are local SQLite writes:
freezing a one-time baseline, importing observed cash activity, and recording
equity checkpoints. A dry run performs those writes in an in-memory copy.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from autotrader.equity.session import MarketCalendar, MarketSession, market_date
from autotrader.execution.paper import fetch_order_records, fetch_paper_account_state
from autotrader.liveaccounting import ingest, service, store
from autotrader.liveaccounting.models import CHECKPOINT_DAILY_CLOSE, ProfitReservePolicy


class ReadableLiveBroker(Protocol):
    def get_account(self) -> object: ...

    def get_all_positions(self) -> object: ...

    def get_orders(self, request: object | None = None) -> object: ...

    def get_account_activities(
        self, activity_type: str, after: datetime | None = None
    ) -> object: ...


class LiveOperationsError(Exception):
    """An operational fact could not be established safely."""


@dataclass(frozen=True)
class AccountingOperationResult:
    operation: str
    dry_run: bool
    skipped: bool
    account_fingerprint: str
    snapshot_at: datetime
    broker_equity: Decimal
    broker_cash: Decimal
    position_count: int
    order_count: int
    activities_seen: int
    activities_imported: int
    activities_unknown: int
    pre_inception_activities: int
    checkpoint_kind: str
    checkpoint_id: int | None = None
    session_date: str | None = None
    detail: str | None = None


def _scratch_or_disk_ledger(
    database: Path,
    *,
    account_fingerprint: str,
    now: datetime,
    source_sha: str | None,
    dry_run: bool,
) -> sqlite3.Connection:
    if not dry_run:
        database.parent.mkdir(parents=True, exist_ok=True)
        return service.open_ledger(
            str(database),
            account_fingerprint=account_fingerprint,
            now=now,
            source_sha=source_sha,
        )

    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    if database.exists():
        uri = f"file:{quote(str(database.resolve()))}?mode=ro"
        source = sqlite3.connect(uri, uri=True)
        try:
            source.backup(connection)
        finally:
            source.close()
    connection.execute("PRAGMA foreign_keys = ON")
    store.initialize(connection)
    store.stamp_metadata(
        connection,
        account_fingerprint=account_fingerprint,
        now=now,
        source_sha=source_sha,
    )
    return connection


def _snapshot(client: ReadableLiveBroker, *, taken_at: datetime) -> service.BrokerSnapshot:
    account = fetch_paper_account_state(client)  # type: ignore[arg-type]
    if not account.tradable:
        raise LiveOperationsError(
            f"the pinned account reports status {account.status} and is not tradable"
        )
    try:
        positions = tuple(client.get_all_positions() or ())  # type: ignore[call-overload]
    except Exception as error:  # noqa: BLE001 - an unknown holding set fails closed
        raise LiveOperationsError(
            f"the broker position record could not be read ({type(error).__name__})"
        ) from None
    unrealized = Decimal(0)
    for position in positions:
        raw = getattr(position, "unrealized_pl", None)
        if raw is None:
            continue
        try:
            unrealized += Decimal(str(raw))
        except Exception:  # noqa: BLE001 - an unreadable component makes the aggregate unknown
            unrealized = Decimal("NaN")
            break
    return service.BrokerSnapshot(
        taken_at=taken_at,
        equity=Decimal(str(account.equity)),
        cash=Decimal(str(account.cash)),
        position_count=len(positions),
        order_count=0,
        unrealized_pnl=unrealized if unrealized.is_finite() else None,
    )


def _import(
    connection: sqlite3.Connection,
    client: ReadableLiveBroker,
    *,
    account_fingerprint: str,
    now: datetime,
) -> ingest.IngestResult:
    result = ingest.import_activities(
        connection,
        client.get_account_activities,  # type: ignore[arg-type]
        account_fingerprint=account_fingerprint,
        now=now,
    )
    if result.unknown_classified:
        raise LiveOperationsError(
            f"{result.unknown_classified} account activity row(s) could not be classified; "
            "UNKNOWN_EXTERNAL_FLOW fails closed and no checkpoint was recorded"
        )
    return result


def freeze_accounting_inception(
    database: Path,
    client: ReadableLiveBroker,
    *,
    account_fingerprint: str,
    now: datetime,
    source_sha: str | None,
    dry_run: bool = False,
) -> AccountingOperationResult:
    """Freeze a pre-trade baseline after proving the entire order record empty."""
    snapshot = _snapshot(client, taken_at=now)
    orders, _ = fetch_order_records(client)  # type: ignore[arg-type]
    snapshot = service.BrokerSnapshot(
        taken_at=snapshot.taken_at,
        equity=snapshot.equity,
        cash=snapshot.cash,
        position_count=snapshot.position_count,
        order_count=len(orders),
        unrealized_pnl=snapshot.unrealized_pnl,
    )
    if orders or snapshot.position_count:
        raise LiveOperationsError(
            f"the account has {len(orders)} order(s) and {snapshot.position_count} "
            "position(s); the pre-trade inception shortcut is not permitted"
        )

    connection = _scratch_or_disk_ledger(
        database,
        account_fingerprint=account_fingerprint,
        now=now,
        source_sha=source_sha,
        dry_run=dry_run,
    )
    try:
        service.establish_inception(
            connection,
            snapshot,
            account_fingerprint=account_fingerprint,
            policy=ProfitReservePolicy(),
            now=now,
            note="Production pre-trade broker baseline; all prior capital is opening capital.",
        )
        imported = _import(
            connection,
            client,
            account_fingerprint=account_fingerprint,
            now=now,
        )
        service.rebuild(
            connection,
            policy=ProfitReservePolicy(),
            now=now,
            reason="production accounting inception",
        )
    finally:
        connection.close()
    return AccountingOperationResult(
        operation="FREEZE_INCEPTION",
        dry_run=dry_run,
        skipped=False,
        account_fingerprint=account_fingerprint,
        snapshot_at=now,
        broker_equity=snapshot.equity,
        broker_cash=snapshot.cash,
        position_count=0,
        order_count=0,
        activities_seen=imported.activities_seen,
        activities_imported=imported.imported,
        activities_unknown=imported.unknown_classified,
        pre_inception_activities=imported.pre_inception,
        checkpoint_kind="INCEPTION",
    )


def sync_accounting(
    database: Path,
    client: ReadableLiveBroker,
    *,
    account_fingerprint: str,
    now: datetime,
    dry_run: bool = False,
) -> AccountingOperationResult:
    """Refresh account facts without advancing the authoritative daily mark."""
    connection = _scratch_or_disk_ledger(
        database,
        account_fingerprint=account_fingerprint,
        now=now,
        source_sha=None,
        dry_run=dry_run,
    )
    try:
        if store.read_state(connection) is None:
            raise LiveOperationsError("accounting inception has not been frozen")
        imported = _import(
            connection,
            client,
            account_fingerprint=account_fingerprint,
            now=now,
        )
        snapshot = _snapshot(client, taken_at=now)
        checkpoint_id = service.take_checkpoint(
            connection,
            snapshot,
            account_fingerprint=account_fingerprint,
            kind="INTRADAY",
            now=now,
        )
        service.rebuild(
            connection,
            policy=ProfitReservePolicy(),
            now=now,
            reason="read-only broker synchronization",
        )
    finally:
        connection.close()
    return AccountingOperationResult(
        operation="SYNC",
        dry_run=dry_run,
        skipped=False,
        account_fingerprint=account_fingerprint,
        snapshot_at=now,
        broker_equity=snapshot.equity,
        broker_cash=snapshot.cash,
        position_count=snapshot.position_count,
        order_count=0,
        activities_seen=imported.activities_seen,
        activities_imported=imported.imported,
        activities_unknown=imported.unknown_classified,
        pre_inception_activities=imported.pre_inception,
        checkpoint_kind="INTRADAY",
        checkpoint_id=checkpoint_id,
    )


def _session_for_close(calendar: MarketCalendar, now: datetime) -> MarketSession | None:
    session = calendar.session_for(market_date(now))
    if session is None or now.astimezone(UTC) < session.close_utc:
        return None
    return session


def daily_close(
    database: Path,
    client: ReadableLiveBroker,
    calendar: MarketCalendar,
    *,
    account_fingerprint: str,
    now: datetime,
    dry_run: bool = False,
) -> AccountingOperationResult:
    """Record the broker-calendar session close once, idempotently."""
    connection = _scratch_or_disk_ledger(
        database,
        account_fingerprint=account_fingerprint,
        now=now,
        source_sha=None,
        dry_run=dry_run,
    )
    try:
        state_row = store.read_state(connection)
        if state_row is None:
            raise LiveOperationsError("accounting inception has not been frozen")
        session = _session_for_close(calendar, now)
        if session is None:
            return AccountingOperationResult(
                operation="DAILY_CLOSE",
                dry_run=dry_run,
                skipped=True,
                account_fingerprint=account_fingerprint,
                snapshot_at=now,
                broker_equity=Decimal(0),
                broker_cash=Decimal(0),
                position_count=0,
                order_count=0,
                activities_seen=0,
                activities_imported=0,
                activities_unknown=0,
                pre_inception_activities=0,
                checkpoint_kind=CHECKPOINT_DAILY_CLOSE,
                detail="No broker-calendar session has completed on the current market date.",
            )
        inception_at = datetime.fromisoformat(str(state_row["inception_at"]))
        if session.close_utc <= inception_at:
            return AccountingOperationResult(
                operation="DAILY_CLOSE",
                dry_run=dry_run,
                skipped=True,
                account_fingerprint=account_fingerprint,
                snapshot_at=session.close_utc,
                broker_equity=Decimal(0),
                broker_cash=Decimal(0),
                position_count=0,
                order_count=0,
                activities_seen=0,
                activities_imported=0,
                activities_unknown=0,
                pre_inception_activities=0,
                checkpoint_kind=CHECKPOINT_DAILY_CLOSE,
                session_date=session.session_date.isoformat(),
                detail="The completed session predates accounting inception.",
            )

        imported = _import(
            connection,
            client,
            account_fingerprint=account_fingerprint,
            now=now,
        )
        utc_date = session.close_utc.date().isoformat()
        existing = connection.execute(
            "SELECT checkpoint_id FROM performance_checkpoints "
            "WHERE kind = 'DAILY_CLOSE' AND utc_date = ?",
            (utc_date,),
        ).fetchone()
        snapshot = _snapshot(client, taken_at=session.close_utc)
        if existing is not None:
            service.rebuild(
                connection,
                policy=ProfitReservePolicy(),
                now=now,
                reason="idempotent daily-close activity refresh",
            )
            return AccountingOperationResult(
                operation="DAILY_CLOSE",
                dry_run=dry_run,
                skipped=True,
                account_fingerprint=account_fingerprint,
                snapshot_at=session.close_utc,
                broker_equity=snapshot.equity,
                broker_cash=snapshot.cash,
                position_count=snapshot.position_count,
                order_count=0,
                activities_seen=imported.activities_seen,
                activities_imported=imported.imported,
                activities_unknown=imported.unknown_classified,
                pre_inception_activities=imported.pre_inception,
                checkpoint_kind=CHECKPOINT_DAILY_CLOSE,
                checkpoint_id=int(existing["checkpoint_id"]),
                session_date=session.session_date.isoformat(),
                detail="The authoritative checkpoint already exists; no duplicate was added.",
            )

        checkpoint_id = service.take_checkpoint(
            connection,
            snapshot,
            account_fingerprint=account_fingerprint,
            kind=CHECKPOINT_DAILY_CLOSE,
            now=now,
        )
        service.rebuild(
            connection,
            policy=ProfitReservePolicy(),
            now=now,
            reason=f"broker-calendar daily close {session.session_date.isoformat()}",
        )
    finally:
        connection.close()
    return AccountingOperationResult(
        operation="DAILY_CLOSE",
        dry_run=dry_run,
        skipped=False,
        account_fingerprint=account_fingerprint,
        snapshot_at=session.close_utc,
        broker_equity=snapshot.equity,
        broker_cash=snapshot.cash,
        position_count=snapshot.position_count,
        order_count=0,
        activities_seen=imported.activities_seen,
        activities_imported=imported.imported,
        activities_unknown=imported.unknown_classified,
        pre_inception_activities=imported.pre_inception,
        checkpoint_kind=CHECKPOINT_DAILY_CLOSE,
        checkpoint_id=checkpoint_id,
        session_date=session.session_date.isoformat(),
    )


__all__ = [
    "AccountingOperationResult",
    "LiveOperationsError",
    "ReadableLiveBroker",
    "daily_close",
    "freeze_accounting_inception",
    "sync_accounting",
]
