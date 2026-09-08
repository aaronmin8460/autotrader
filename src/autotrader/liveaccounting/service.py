"""Establishing inception, taking checkpoints, and replaying the derived state.

Three operations, all read-only at the broker.

**Inception** freezes the baseline. It refuses to run if the account has ever
traded, because the simple baseline shortcut is only honest on an account whose
history is empty - and this account's is, verifiably, today. If a live order,
fill or position exists, the shortcut is refused and the caller is told to
reconstruct instead. Silently resetting history is the failure mode this
refusal exists to prevent.

**Checkpointing** records broker equity at an instant. `DAILY_CLOSE` is
authoritative; `INTRADAY` is an observation that can never raise the mark.

**Rebuild** recomputes every derived row from the authoritative ledger and the
policy. It is not a repair path with special cases - it is the ordinary
evaluation, run again, which is why a backdated flow needs no special handling
beyond being stored.

Nothing here submits, cancels, replaces, liquidates, transfers or withdraws.
The broker is reached through injected read callables that carry no such name.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from autotrader.liveaccounting import engine, store
from autotrader.liveaccounting.models import (
    CHECKPOINT_DAILY_CLOSE,
    CHECKPOINT_INCEPTION,
    CHECKPOINT_INTRADAY,
    WITHDRAWAL_MODE_OBSERVE_ONLY,
    LiveAccountingError,
    ProfitReservePolicy,
)


class InceptionRefused(LiveAccountingError):
    """The baseline shortcut is not honest for this account's history."""


@dataclass(frozen=True)
class BrokerSnapshot:
    """Broker truth at one instant, as a caller read it. All GETs."""

    taken_at: datetime
    equity: Decimal
    cash: Decimal
    position_count: int
    order_count: int
    unrealized_pnl: Decimal | None = None


def open_ledger(
    path: str, *, account_fingerprint: str, now: datetime, source_sha: str | None = None
):
    """Open, migrate and account-scope the ledger. Returns a live connection.

    The caller closes it. `store.connect` is a context manager for ordinary
    use; this exists so the CLI and the service can hold one open across
    several operations without nesting four `with` blocks.
    """
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA journal_mode = {store.JOURNAL_MODE}").fetchone()
    connection.execute(f"PRAGMA busy_timeout = {store.BUSY_TIMEOUT_MS}")
    store.initialize(connection)
    store.stamp_metadata(
        connection, account_fingerprint=account_fingerprint, now=now, source_sha=source_sha
    )
    return connection


def establish_inception(
    connection: sqlite3.Connection,
    snapshot: BrokerSnapshot,
    *,
    account_fingerprint: str,
    policy: ProfitReservePolicy,
    now: datetime,
    note: str | None = None,
) -> None:
    """Freeze the baseline from a pre-trade snapshot, or refuse.

    The opening equity becomes `baseline_equity`, which is **capital**. It is
    not profit and it is not a deposit: the broker activity that delivered it
    stays in the flow ledger as `PRE_INCEPTION`, real history that contributes
    nothing to `confirmed_deposits`. An account funded with $50 that then
    counted its own funding as a deposit would report having doubled before it
    placed a single order.
    """
    store.require_account(connection, account_fingerprint)
    if snapshot.order_count or snapshot.position_count:
        raise InceptionRefused(
            f"This account already has {snapshot.order_count} order(s) and "
            f"{snapshot.position_count} position(s). The pre-trade baseline shortcut would "
            "silently discard whatever trading produced them. Reconstruct the state from "
            "broker activities, orders, fills and equity history instead, and record the "
            "limitation. Nothing was written."
        )
    store.freeze_inception(
        connection,
        inception_at=snapshot.taken_at,
        baseline_equity=snapshot.equity,
        baseline_cash=snapshot.cash,
        method=store.INCEPTION_PRE_TRADE,
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        reserve_rate=policy.reserve_rate,
        withdrawal_mode=WITHDRAWAL_MODE_OBSERVE_ONLY,
        now=now,
        note=note,
    )
    store.record_checkpoint(
        connection,
        account_fingerprint=account_fingerprint,
        taken_at=snapshot.taken_at,
        kind=CHECKPOINT_INCEPTION,
        broker_equity=snapshot.equity,
        broker_cash=snapshot.cash,
        unrealized_pnl=snapshot.unrealized_pnl,
        position_count=snapshot.position_count,
        now=now,
    )


def take_checkpoint(
    connection: sqlite3.Connection,
    snapshot: BrokerSnapshot,
    *,
    account_fingerprint: str,
    kind: str = CHECKPOINT_INTRADAY,
    now: datetime,
) -> int:
    """Record broker equity at an instant. `DAILY_CLOSE` is the authoritative kind."""
    if kind not in (CHECKPOINT_DAILY_CLOSE, CHECKPOINT_INTRADAY):
        raise LiveAccountingError(
            f"{kind} is not a kind a running service may record. INCEPTION is written once, "
            "by establish_inception."
        )
    return store.record_checkpoint(
        connection,
        account_fingerprint=account_fingerprint,
        taken_at=snapshot.taken_at,
        kind=kind,
        broker_equity=snapshot.equity,
        broker_cash=snapshot.cash,
        unrealized_pnl=snapshot.unrealized_pnl,
        position_count=snapshot.position_count,
        now=now,
    )


def rebuild(
    connection: sqlite3.Connection,
    *,
    policy: ProfitReservePolicy,
    now: datetime,
    reason: str = "routine replay",
) -> int:
    """Recompute every derived row from the authoritative ledger. Returns the rebuild count.

    Total, not incremental. The reserve history is dropped and rewritten from
    the baseline, the flows and the checkpoints, which is the same computation
    the read model performs - so a rebuild cannot produce an answer the payload
    disagrees with, and a late flow lands in its correct place in time without
    a repair path having to exist.
    """
    state = store.read_state(connection)
    if state is None:
        raise LiveAccountingError("Nothing to rebuild: inception has not been frozen.")
    baseline = store.text_decimal(state["baseline_equity"])
    result = engine.evaluate_reserve(
        baseline_equity=baseline,
        checkpoints=store.read_checkpoints(connection),
        flows=store.read_flows(connection),
        policy=policy,
    )
    store.replace_reserve_events(
        connection, result.events, reserve_rate=policy.reserve_rate, now=now
    )
    return store.record_rebuild(connection, reason=reason, now=now)


def utc_day_bounds(moment: datetime) -> tuple[datetime, datetime]:
    start = moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start.replace(hour=23, minute=59, second=59, microsecond=999999)


__all__ = [
    "BrokerSnapshot",
    "InceptionRefused",
    "establish_inception",
    "open_ledger",
    "rebuild",
    "take_checkpoint",
    "utc_day_bounds",
]
