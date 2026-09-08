"""The dashboard's reader for the real-money accounting ledger. One read-only connection.

Mirrors `dashboard.realized_pnl`: a `mode=ro` connection, an unreadable ledger
reported as unreadable rather than as zero, and no path that could write.

The live safety state - `live_ready` and `live_armed` - is **passed through**
from the Prompt-1 gates, never recomputed here. A `None` means the state could
not be read, and the frontend must render that as unknown: an unreadable arm
state is not a disarmed one.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from autotrader.live.readiness import configured_live_readiness
from autotrader.liveaccounting import readmodel, store
from autotrader.liveaccounting.models import (
    STATUS_BROKER_UNAVAILABLE,
    STATUS_NOT_INITIALIZED,
    WITHDRAWABLE_NOT_EXPOSED,
    WITHDRAWAL_MODE_OBSERVE_ONLY,
)

#: Where the live accounting ledger lives. Its own file, never the paper one.
DATABASE_ENV = "AUTOTRADER_LIVE_ACCOUNTING_DB"
DEFAULT_DATABASE = "data/live_accounting.db"

#: The pinned account, read from the same variable the live runtime pins to.
FINGERPRINT_ENV = "AUTOTRADER_LIVE_ACCOUNT_FINGERPRINT"

# The dedicated operational store that owns the durable arm row.
LIVE_DATABASE_ENV = "AUTOTRADER_EQUITY_LIVE_DB"


def database_path() -> Path:
    return Path(os.environ.get(DATABASE_ENV) or DEFAULT_DATABASE)


def expected_fingerprint() -> str | None:
    value = os.environ.get(FINGERPRINT_ENV, "").strip().lower()
    return value or None


def _unreadable(now: datetime, detail: str) -> dict[str, object]:
    """The payload for a ledger that could not be opened at all.

    Every derived figure is `null` and the status says why. The shape still
    satisfies the contract, so a dashboard written against it renders "unknown"
    rather than crashing - which is the behaviour that makes an outage look
    like an outage instead of like a $0 account.
    """
    payload: dict[str, object] = {
        "contract_version": readmodel.CONTRACT_VERSION,
        "generated_at": now.astimezone(UTC).isoformat(),
        "account_fingerprint": expected_fingerprint() or "",
        "environment": "LIVE",
        "accounting_status": STATUS_NOT_INITIALIZED,
        "accounting_status_detail": detail,
        "withdrawal_mode": WITHDRAWAL_MODE_OBSERVE_ONLY,
        "withdrawal_authorized": False,
        "authorized_withdrawal_amount": "0.00",
        "automatic_transfer_enabled": False,
        "broker_withdrawable_cash": None,
        "broker_withdrawable_cash_status": WITHDRAWABLE_NOT_EXPOSED,
    }
    return payload


def build_summary(*, now: datetime | None = None, path: Path | None = None) -> dict[str, object]:
    """One poll of the Live accounting panel. Read-only from first call to last."""
    moment = now or datetime.now(UTC)
    target = path or database_path()
    if not target.exists():
        return _unreadable(
            moment,
            "The live accounting ledger does not exist yet. Accounting inception has not "
            "been established for the real-money account.",
        )
    try:
        with store.connect_read_only(target) as connection:
            return readmodel.build_summary(
                connection,
                now=moment,
                expected_fingerprint=expected_fingerprint(),
                safety=read_safety_state(),
            )
    except Exception as error:  # noqa: BLE001 - an unreadable ledger reports, never guesses
        return _unreadable(
            moment,
            f"The live accounting ledger could not be read ({type(error).__name__}). No "
            "figure was published.",
        )


def read_safety_state() -> readmodel.LiveSafetyState:
    """`live_ready` / `live_armed`, from the Prompt-1 gates. Never computed here.

    Both stay `None` unless the arm state can actually be read. Reporting
    `live_armed = False` because the state was unreadable would be the single
    most dangerous substitution this dashboard could make, so it is not made:
    unknown stays unknown all the way to the screen.
    """
    from autotrader.live.armstate import (
        STATE_ARMED,
        environment_arm_gate_open,
        read_arm_state,
    )

    readiness = configured_live_readiness()
    path = os.environ.get(LIVE_DATABASE_ENV, "").strip()
    if not path or not Path(path).exists():
        return readmodel.LiveSafetyState(live_ready=readiness, live_armed=None)
    try:
        import sqlite3

        uri = f"file:{Path(path).resolve()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            connection.row_factory = sqlite3.Row
            state = read_arm_state(connection)
        finally:
            connection.close()
    except Exception:  # noqa: BLE001
        return readmodel.LiveSafetyState(live_ready=readiness, live_armed=None)
    return readmodel.LiveSafetyState(
        live_ready=readiness,
        live_armed=(getattr(state, "state", None) == STATE_ARMED and environment_arm_gate_open()),
    )


__all__ = [
    "DATABASE_ENV",
    "DEFAULT_DATABASE",
    "FINGERPRINT_ENV",
    "LIVE_DATABASE_ENV",
    "STATUS_BROKER_UNAVAILABLE",
    "build_summary",
    "database_path",
    "expected_fingerprint",
    "read_safety_state",
]
