"""The Equity Shadow HTTP surface. GET only, and structurally so.

Six routes, every one of them a read:

    GET /api/equity-shadow/health       process liveness; opens no database
    GET /api/equity-shadow/overview     the whole page, in one consistent read
    GET /api/equity-shadow/status       is the observer observing
    GET /api/equity-shadow/latest       the ten symbols, both engines
    GET /api/equity-shadow/comparison   agreement, regime and hypothetical books
    GET /api/equity-shadow/history      a bounded window of recorded decisions

**A separate process from the operational dashboard, deliberately.** The
dashboard on `:8000` reads the trading database as the trading service user.
This one reads the shadow's database as `atshadow`, an identity that cannot
open the trading database, cannot read the broker credentials, and cannot read
the activation file that authorizes paper submission. Merging them would have
given the operational reader a route into research evidence and the research
reader an identity with production reach; keeping them apart costs one
uvicorn process.

**There is no write endpoint, and no engine behind one.** This module defines
no `POST`, `PUT`, `PATCH` or `DELETE`, and there is nothing it could call if
it did: the shadow runtime has no execution seam, and this package imports
nothing that can submit, cancel or replace anything. No route promotes the
shadow to paper, activates an engine, or transitions observation into
execution - those are not disabled here, they do not exist.

**Nothing on the wire is an account.** The payload is quantities, timestamps,
signals, machine reason codes, and clearly-labelled *hypothetical* index
values compounded from a normalized 100. No broker equity, no position, no
credential, no filesystem path.

**Local by default.** `python -m autotrader.dashboard.equity_shadow_api` binds
`127.0.0.1`. Publishing it is the reverse proxy's job, behind the same
authentication as the rest of the dashboard.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import FastAPI, Query

from autotrader.dashboard import equity_shadow

#: Loopback only, and not configurable here. Making "listen on every
#: interface" a one-flag decision is how an unauthenticated internal tool ends
#: up on a public port.
DEFAULT_HOST = "127.0.0.1"

#: 8001, because 8000 is the operational dashboard API and the two must not
#: contend for it.
DEFAULT_PORT = 8001

PORT_ENV = "AUTOTRADER_EQUITY_SHADOW_API_PORT"

#: The HTTP methods this application is allowed to expose. Asserted in tests
#: against the assembled route table, so adding a write route fails the suite
#: rather than shipping.
ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})

_API_PREFIX = "/api/equity-shadow"


def configured_port() -> int:
    """The loopback port to bind, from the environment or the default."""
    raw = os.environ.get(PORT_ENV)
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_PORT


def build_overview() -> equity_shadow.EquityShadowOverview:
    """One poll of the Equity Shadow page, from the configured database."""
    return equity_shadow.build_overview(path=equity_shadow.database_path(), now=datetime.now(UTC))


def create_app() -> FastAPI:
    """Assemble the read-only Equity Shadow API.

    A factory rather than a module-level singleton so a test can build an app
    per case without leaking configuration between them. `app` below is the
    one the ASGI server imports.
    """
    application = FastAPI(
        title="AutoTrader Equity Shadow",
        version="0.1.0",
        summary=(
            "Read-only observation record of the V3 + EDA-1 equity shadow. ZERO ORDER MUTATION."
        ),
        description=(
            "Every route is a GET over an observation record. The process behind it "
            "has never submitted an order and holds no path by which it could: it "
            "cannot place, cancel or modify an order, promote the shadow to paper or "
            "live execution, activate an engine, or edit stored state - there is no "
            "endpoint for any of it. Portfolio figures are HYPOTHETICAL, compounded "
            "from a normalized 100 with no costs applied, and are not account equity."
        ),
    )

    @application.get(f"{_API_PREFIX}/health", tags=["equity-shadow"])
    def health() -> dict[str, Any]:
        """Liveness. Opens no database and contacts no broker."""
        return {
            "status": "ok",
            "read_only": True,
            "observation_only": True,
            "broker_mutation": equity_shadow.BROKER_MUTATION_DISABLED,
            "mode": equity_shadow.SHADOW_MODE,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    @application.get(f"{_API_PREFIX}/overview", tags=["equity-shadow"])
    def overview() -> equity_shadow.EquityShadowOverview:
        """The whole page: service, regime, symbols, hypothetical, comparison."""
        return build_overview()

    @application.get(f"{_API_PREFIX}/status", tags=["equity-shadow"])
    def status() -> equity_shadow.ServicePanel:
        """Whether the observer is observing, and the zero-order invariant."""
        return build_overview().service

    @application.get(f"{_API_PREFIX}/latest", tags=["equity-shadow"])
    def latest() -> dict[str, Any]:
        """The ten symbols' latest recorded bar, both engines side by side."""
        page = build_overview()
        return {
            "generated_at": page.generated_at,
            "regime": page.regime,
            "symbols": list(page.symbols),
        }

    @application.get(f"{_API_PREFIX}/comparison", tags=["equity-shadow"])
    def comparison() -> dict[str, Any]:
        """Agreement, regime behaviour, and the two hypothetical books."""
        page = build_overview()
        return {
            "generated_at": page.generated_at,
            "hypothetical_label": page.hypothetical_label,
            "comparison": page.comparison,
            "hypothetical": page.hypothetical,
        }

    @application.get(f"{_API_PREFIX}/history", tags=["equity-shadow"])
    def history(
        limit: Annotated[
            int,
            Query(
                ge=1,
                le=equity_shadow.HISTORY_MAX_LIMIT,
                description="Rows to return, newest first.",
            ),
        ] = equity_shadow.HISTORY_DEFAULT_LIMIT,
        offset: Annotated[int, Query(ge=0, description="Rows to skip from the newest end.")] = 0,
        symbol: Annotated[str | None, Query(description="Restrict to one universe symbol.")] = None,
    ) -> equity_shadow.HistoryPage:
        """A bounded window of recorded comparisons. Never the whole table."""
        snapshot = equity_shadow.read_shadow(equity_shadow.database_path())
        return equity_shadow.build_history(snapshot, limit=limit, offset=offset, symbol=symbol)

    return application


app = create_app()


def main() -> None:
    """Serve the read-only Equity Shadow API on loopback.

    `uvicorn` is imported here rather than at module scope so that importing
    this module - which the test suite does on every run - does not drag an
    ASGI server in with it.
    """
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="python -m autotrader.dashboard.equity_shadow_api",
        description=(
            "Serve the read-only AutoTrader Equity Shadow API on 127.0.0.1. "
            "Every route is a GET over an observation record; nothing here can "
            "place, modify, or authorize an order."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=configured_port(),
        help=f"Loopback port to bind (default {DEFAULT_PORT}).",
    )
    arguments = parser.parse_args()
    uvicorn.run(
        "autotrader.dashboard.equity_shadow_api:app",
        host=DEFAULT_HOST,
        port=arguments.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()


__all__ = [
    "ALLOWED_METHODS",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "PORT_ENV",
    "app",
    "build_overview",
    "configured_port",
    "create_app",
    "main",
]
