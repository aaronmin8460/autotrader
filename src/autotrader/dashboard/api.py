"""C10: the dashboard HTTP surface. GET only, and structurally so.

Six routes, every one of them a read:

    GET /api/dashboard/overview     the whole page, in one consistent read
    GET /api/dashboard/positions    what the account holds
    GET /api/dashboard/orders       recent orders
    GET /api/dashboard/risk         the V0.2 limits and their utilization
    GET /api/dashboard/system       health, reconciliation, and runtime
    GET /api/dashboard/health       process liveness; touches nothing

**There is no write endpoint, and that is the point.** Not a hidden one, not a
disabled one, not one behind a flag: this module defines no `POST`, `PUT`,
`PATCH`, or `DELETE` route, so there is no path by which a browser could place
an order, cancel one, change a limit, start or stop the runtime, edit a row, or
trigger a reconciliation repair. `tests/test_dashboard.py` walks the assembled
application's route table and asserts it, and separately audits this package's
executable code for the order-submission entry points. Hiding a button would
have left the endpoint; there is no endpoint.

**The sub-routes are slices, not second opinions.** Each one calls the same
builder `overview` calls, so two routes cannot disagree about the same
database. `overview` is what the frontend polls; the rest exist for an operator
with `curl`.

**Nothing sensitive reaches the response.** The payload is assembled from
`dashboard.models`, whose entire vocabulary is quantities, timestamps,
statuses, and machine reason codes. Credentials are read inside the execution
boundary and never leave it, broker exception text is discarded at
`dashboard.broker` rather than forwarded, and no route echoes a filesystem
path, an environment variable, or an account identifier.

**Local by default.** `python -m autotrader.dashboard` binds `127.0.0.1`.
Exposing this beyond the machine it runs on is a deployment concern - it needs
authentication and a reverse proxy in front of it - and deployment is not part
of this milestone. See the README.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from autotrader.dashboard import service
from autotrader.dashboard.broker import SharedBrokerReader
from autotrader.dashboard.models import (
    ENVIRONMENT_PAPER,
    AccountSafetyPanel,
    HealthComponent,
    OrdersPanel,
    Overview,
    PositionsPanel,
    ReconciliationPanel,
    RiskPanel,
    RuntimePanel,
)
from autotrader.state import DEFAULT_DATABASE_PATH

#: Where the operational database lives, overridable for a non-default layout.
#: A path only - never a connection string, and never anything with a
#: credential in it.
DATABASE_PATH_ENV = "AUTOTRADER_DASHBOARD_DB"

#: The loopback address `python -m autotrader.dashboard` binds. Not
#: configurable here on purpose: making "listen on every interface" a one-flag
#: decision is how an unauthenticated internal tool ends up on a public port.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

#: The HTTP methods this application is allowed to expose. Asserted in tests
#: against the assembled route table, so adding a write route fails the suite
#: rather than shipping.
ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})

_API_PREFIX = "/api/dashboard"

#: One shared, lock-guarded paper client for the whole process. See
#: `SharedBrokerReader`: a dashboard must not multiply broker load by the
#: number of open tabs.
BROKER = SharedBrokerReader()


def database_path() -> Path:
    """Where to read operational state from."""
    configured = os.environ.get(DATABASE_PATH_ENV)
    return Path(configured) if configured else Path(DEFAULT_DATABASE_PATH)


def build_overview() -> Overview:
    """One dashboard poll, from the configured database and the paper account."""
    return service.build_overview(
        database_path=database_path(),
        now=datetime.now(UTC),
        broker=BROKER.read(),
    )


def create_app() -> FastAPI:
    """Assemble the read-only dashboard API.

    A factory rather than a module-level singleton so a test can build an app
    per case without leaking configuration between them. `app` below is the one
    the ASGI server imports.
    """
    application = FastAPI(
        title="AutoTrader dashboard",
        version="0.1.0",
        summary="Read-only operations view of the AutoTrader paper system.",
        description=(
            "Every route is a GET. This API cannot place, cancel, or modify an order, "
            "change a risk limit, start or stop the runtime, edit stored state, or "
            "trigger a reconciliation repair - there is no endpoint for any of it."
        ),
    )

    @application.get(f"{_API_PREFIX}/health", tags=["dashboard"])
    def health() -> dict[str, Any]:
        """Liveness. Opens no database and contacts no broker."""
        return {
            "status": "ok",
            "environment": ENVIRONMENT_PAPER,
            "read_only": True,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    @application.get(f"{_API_PREFIX}/overview", tags=["dashboard"])
    def overview() -> Overview:
        """The whole page: metrics, positions, orders, health, risk, runtime."""
        return build_overview()

    @application.get(f"{_API_PREFIX}/positions", tags=["dashboard"])
    def positions() -> PositionsPanel:
        """What the account holds, from the broker when it can be read."""
        panel = build_overview().positions
        assert panel is not None  # noqa: S101 - build_overview always populates it
        return panel

    @application.get(f"{_API_PREFIX}/orders", tags=["dashboard"])
    def orders() -> OrdersPanel:
        """Recent orders, newest first."""
        panel = build_overview().orders
        assert panel is not None  # noqa: S101 - build_overview always populates it
        return panel

    @application.get(f"{_API_PREFIX}/risk", tags=["dashboard"])
    def risk() -> RiskPanel:
        """The established V0.2 limits and their current utilization."""
        panel = build_overview().risk
        assert panel is not None  # noqa: S101 - build_overview always populates it
        return panel

    @application.get(f"{_API_PREFIX}/system", tags=["dashboard"])
    def system() -> dict[str, Any]:
        """System state, health, reconciliation, both runtimes, and account safety."""
        page = build_overview()
        return {
            "generated_at": page.generated_at,
            "environment": page.environment,
            "system_state": page.system_state,
            "system_state_tone": page.system_state_tone,
            "attention": list(page.attention),
            "health": list(page.health),
            "reconciliation": page.reconciliation,
            "runtimes": list(page.runtimes),
            "account_safety": page.account_safety,
            "api_budget": list(page.api_budget),
        }

    return application


app = create_app()


__all__ = [
    "ALLOWED_METHODS",
    "BROKER",
    "DATABASE_PATH_ENV",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "AccountSafetyPanel",
    "HealthComponent",
    "ReconciliationPanel",
    "RuntimePanel",
    "app",
    "build_overview",
    "create_app",
    "database_path",
]
