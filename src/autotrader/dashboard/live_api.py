"""The read-only real-money safety API. GET routes, and structurally so.

The sixth API process, and the one that matters most that it cannot write. Its
siblings describe paper books and observation records; this one describes an
account holding real money, so the absence of a write route is the only thing
standing between a dashboard viewer and a real-money order. There is no route
here that arms, disarms, starts, stops, places, cancels, transfers or withdraws,
and `ALLOWED_METHODS` is `{GET, HEAD}` asserted against the assembled route
table, so adding one fails the suite rather than shipping.

**A separate process on a separate port, deliberately.** The paper dashboard API
on :8000 holds a paper client; this one would hold a real-money read client.
Merging them would put both credentials in one process and would make "which
account is this figure from" a question about a code path rather than about a
port. Prompt 1's isolation doctrine keeps real-money credentials disjoint from
paper's, and this process keeps that true at the service boundary too.

    :8000  paper operational dashboard
    :8001  equity shadow
    :8002  equity paper
    :8003  A1-B shadow
    :8004  market charts
    :8005  live accounting          <- Prompt 2, the frozen contract
    :8006  live safety              <- here

**This process reads no money.** The accounting figures - flow-adjusted equity,
trading P&L, the high-water mark, the reserve, the bucket, the preview - are
Prompt 2's, are served on :8005 under the frozen contract, and are not computed,
cached, mirrored or second-guessed here. This process answers a different
question: may the system trade, against which account, and inside what ceilings.

**Fails closed and says which way.** A missing credential, an unreachable
broker, an unreadable arm switch and an account that does not match the pin are
four different values, never one blank. None of them becomes a zero.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from autotrader.dashboard import live_safety, live_terminal
from autotrader.dashboard.service_units import read_unit_properties
from autotrader.execution.equity import fetch_open_paper_orders
from autotrader.live.activity import LiveActivityError, read_cash_flow_events
from autotrader.live.armstate import read_arm_state
from autotrader.live.identity import account_fingerprint
from autotrader.live.readiness import configured_live_readiness
from autotrader.state import sqlite as state

#: Loopback only, and not configurable here. Making "listen on every interface"
#: a one-flag decision is how an unauthenticated internal tool ends up on a
#: public port - and this one names a real-money account.
DEFAULT_HOST = "127.0.0.1"

#: 8006: 8005 is reserved by the frozen accounting contract for Prompt 2.
DEFAULT_PORT = 8006

PORT_ENV = "AUTOTRADER_LIVE_SAFETY_API_PORT"
DATABASE_ENV = "AUTOTRADER_EQUITY_LIVE_DB"
DEPLOYED_SHA_ENV = "AUTOTRADER_DEPLOYED_SHA"

#: The HTTP methods this application is allowed to expose.
ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})

_API_PREFIX = "/api/live-safety"


def _live_database_path() -> Path | None:
    raw = os.environ.get(DATABASE_ENV, "").strip()
    return Path(raw) if raw else None


def _local_state() -> tuple[object | None, live_safety.LiveReconciliationPanel]:
    """Read the durable arm row and latest finished reconciliation, without writes."""
    database = _live_database_path()
    if database is None or not database.exists():
        return None, live_safety.LiveReconciliationPanel(
            available=False,
            detail="The dedicated Live operational store does not exist.",
        )
    try:
        uri = f"file:{database.resolve()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = 1")
            arm = read_arm_state(connection)
            latest = state.latest_reconciliation_run(connection)
        finally:
            connection.close()
    except Exception as error:  # noqa: BLE001 - unknown remains unknown, and details are sanitized
        return None, live_safety.LiveReconciliationPanel(
            available=False,
            detail=(
                f"The dedicated Live operational store could not be read ({type(error).__name__})."
            ),
        )
    if latest is None:
        return arm, live_safety.LiveReconciliationPanel(
            available=False,
            detail="No real-money reconciliation run has completed in this store.",
        )
    return arm, live_safety.LiveReconciliationPanel(
        available=True,
        status=latest.status,
        safe_to_trade=latest.safe_to_trade,
        completed_at=latest.completed_at.astimezone(UTC).isoformat(),
        issues=latest.issues_count,
        unresolved=latest.unresolved_count,
    )


def _service_panel() -> live_safety.LiveServicePanel:
    properties = read_unit_properties(live_safety.LIVE_SERVICE_NAME)
    if properties is None:
        return live_safety.LiveServicePanel(
            state=live_safety.SERVICE_UNKNOWN,
            detail="The service manager could not be queried.",
        )
    load = properties.get("LoadState")
    active = properties.get("ActiveState")
    unit_file = properties.get("UnitFileState")
    if load == "not-found":
        service_state = live_safety.SERVICE_NOT_INSTALLED
    elif active == "active":
        service_state = live_safety.SERVICE_RUNNING
    elif active == "inactive" and unit_file == "disabled":
        service_state = live_safety.SERVICE_DISABLED
    elif active == "inactive":
        service_state = live_safety.SERVICE_STOPPED
    else:
        service_state = live_safety.SERVICE_UNKNOWN
    return live_safety.LiveServicePanel(
        state=service_state,
        detail=(
            f"load={load or 'unknown'}, active={active or 'unknown'}, "
            f"enabled={unit_file or 'unknown'}"
        ),
    )


def _gross_exposure(positions: tuple[object, ...]) -> Decimal | None:
    total = Decimal(0)
    try:
        for position in positions:
            amount = Decimal(str(getattr(position, "market_value", None)))
            if not amount.is_finite():
                return None
            total += abs(amount)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return total


def configured_port() -> int:
    raw = os.environ.get(PORT_ENV)
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_PORT


def build_panel(now: datetime | None = None) -> live_safety.LiveSafetyPanel:
    """One poll of the Live safety panel.

    The policy is the frozen `LIVE_VALIDATION_100` parameter set, resolved from
    the allocator rather than restated here, so the ceilings this API reports
    and the ceilings the risk engine enforces come from one definition.

    All broker capabilities come through the audited allowlisted read facade.
    Failure of any independent read stays unknown; it is never replaced with a
    zero or a remembered value.
    """
    from autotrader.equity.allocation import (
        POLICY_LIVE_VALIDATION_100,
        allocation_policy_for,
    )

    moment = now or datetime.now(UTC)
    client = live_safety.create_live_read_broker()
    account = live_safety.read_live_account(None, now=moment)
    observed_fingerprint: str | None = None
    positions: tuple[object, ...] = ()
    open_order_count: int | None = None
    gross_exposure: Decimal | None = None
    cash_flow_events = None
    if client is not None:
        try:
            raw_account = client.get_account()
            raw_number = str(getattr(raw_account, "account_number", "") or "")
            if raw_number:
                observed_fingerprint = account_fingerprint(raw_number)
            try:
                positions = tuple(client.get_all_positions() or ())
                gross_exposure = _gross_exposure(positions)
            except Exception:  # noqa: BLE001 - an unreadable holding set remains unknown
                positions = ()
                gross_exposure = None
            try:
                open_order_count = len(fetch_open_paper_orders(client))  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001 - an unreadable order set remains unknown
                open_order_count = None
            account = live_safety.account_facts_from_broker(
                raw_account,
                position_count=len(positions) if gross_exposure is not None else None,
                open_order_count=open_order_count,
                now=moment,
            )
        except Exception:  # noqa: BLE001 - broker failure text must not reach the browser
            account = live_safety.LiveAccountFacts(status=live_safety.ACCOUNT_UNREADABLE)

        start = moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            cash_flow_events = read_cash_flow_events(
                client.get_account_activities,
                after=start,
            )
        except LiveActivityError:
            cash_flow_events = None

    arm, reconciliation = _local_state()
    return live_safety.build_panel(
        now=moment,
        policy=allocation_policy_for(POLICY_LIVE_VALIDATION_100),
        account=account,
        arm_state=arm,  # type: ignore[arg-type]
        observed_fingerprint=observed_fingerprint,
        expected_fingerprint=live_safety.configured_fingerprint(),
        gross_exposure=gross_exposure,
        cash_flow_events=cash_flow_events,
        reconciliation=reconciliation,
        service=_service_panel(),
        live_ready=configured_live_readiness(),
        code_sha=os.environ.get(DEPLOYED_SHA_ENV, "").strip() or None,
    )


def create_app() -> FastAPI:
    """Assemble the read-only real-money safety API.

    A factory rather than a module-level singleton so a test can build an app
    per case without leaking configuration between them.
    """
    application = FastAPI(
        title="AutoTrader Live safety",
        version="0.1.0",
        summary="Read-only real-money safety state. REAL MONEY. Displays it; changes none.",
        description=(
            "Every route is a GET over real-money safety state. Nothing here can arm or "
            "disarm real-money trading, place, cancel or replace an order, move money, "
            "install or start a service, or edit stored state - there is no endpoint for "
            "any of it. Accounting figures are NOT served here: flow-adjusted equity, "
            "trading P&L, the high-water mark, the profit reserve and the withdrawal "
            "bucket come from the frozen live-accounting contract on its own service."
        ),
    )

    @application.get(f"{_API_PREFIX}/health", tags=["live-safety"])
    def health() -> dict[str, Any]:
        """Liveness. Opens no database and contacts no broker."""
        return {
            "status": "ok",
            "environment": live_safety.ENVIRONMENT_LIVE,
            "read_only": True,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    @application.get(f"{_API_PREFIX}/summary", tags=["live-safety"])
    def summary() -> live_safety.LiveSafetyPanel:
        """The whole Live safety panel: arm, identity, account, ceilings, guard."""
        return build_panel()

    @application.get(f"{_API_PREFIX}/terminal", tags=["live-safety"])
    def terminal() -> dict[str, Any]:
        """Live positions, recorded decisions/orders, metrics, and event tape."""
        return live_terminal.build_terminal(path=_live_database_path())

    @application.middleware("http")
    async def _no_store(request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    return application


def route_methods(application: FastAPI) -> set[str]:
    """Every HTTP method the assembled application exposes."""
    methods: set[str] = set()
    for route in application.routes:
        methods |= set(getattr(route, "methods", set()) or set())
    return methods


def main() -> None:
    """Run the loopback-only Live safety process."""
    import uvicorn

    uvicorn.run(app, host=DEFAULT_HOST, port=configured_port(), log_level="info")


app = create_app()


__all__ = [
    "ALLOWED_METHODS",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DATABASE_ENV",
    "DEPLOYED_SHA_ENV",
    "PORT_ENV",
    "app",
    "build_panel",
    "configured_port",
    "create_app",
    "main",
    "route_methods",
]


if __name__ == "__main__":  # pragma: no cover - exercised by the service manager
    main()
