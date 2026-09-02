"""The A1-B U30 Shadow HTTP surface. GET only, and structurally so.

Six routes, every one of them a read:

    GET /api/equity-a1b-shadow/health       process liveness; opens no database
    GET /api/equity-a1b-shadow/overview     the whole page, in one consistent read
    GET /api/equity-a1b-shadow/status       is the observer observing
    GET /api/equity-a1b-shadow/latest       the universe, one row per symbol
    GET /api/equity-a1b-shadow/comparison   counts and the hypothetical book
    GET /api/equity-a1b-shadow/history      a bounded window of observations

**A fourth process, deliberately.** The A1-B store is readable only by the
identity that writes it, and that is the identity this API runs as. Serving it
from any of the other readers would have widened their reach to a record they
have no business opening; a separate loopback process costs nothing and keeps
the separation kernel-enforced rather than conventional.

**There is no write endpoint, and no engine behind one.** This module defines
no `POST`, `PUT`, `PATCH` or `DELETE`, and the observer behind it holds no
execution seam.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import FastAPI, Query

from autotrader.dashboard import a1b_shadow

#: Loopback only, and not configurable here.
DEFAULT_HOST = "127.0.0.1"

#: 8003: 8000 is the operational API, 8001 the equity shadow's, 8002 the
#: equity paper's.
DEFAULT_PORT = 8003

PORT_ENV = "AUTOTRADER_EQUITY_A1B_SHADOW_API_PORT"

#: The HTTP methods this application is allowed to expose.
ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})

_API_PREFIX = "/api/equity-a1b-shadow"


def configured_port() -> int:
    raw = os.environ.get(PORT_ENV)
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_PORT


def build_overview() -> a1b_shadow.A1BShadowOverview:
    """One poll of the A1-B page, from the configured database."""
    return a1b_shadow.build_overview(path=a1b_shadow.database_path(), now=datetime.now(UTC))


def create_app() -> FastAPI:
    application = FastAPI(
        title="AutoTrader A1-B U30 Shadow",
        version="0.1.0",
        summary="Read-only observation record of the A1-B U30 shadow. ZERO ORDER MUTATION.",
        description=(
            "Every route is a GET over an observation record. The process behind it "
            "has never submitted an order and holds no path by which it could. Weight "
            "and portfolio figures are HYPOTHETICAL, compounded from a normalized 100 "
            "with no costs applied, and are not account equity."
        ),
    )

    @application.get(f"{_API_PREFIX}/health", tags=["equity-a1b-shadow"])
    def health() -> dict[str, Any]:
        """Liveness. Opens no database and contacts no broker."""
        return {
            "status": "ok",
            "read_only": True,
            "observation_only": True,
            "broker_mutation": a1b_shadow.BROKER_MUTATION_DISABLED,
            "mode": a1b_shadow.SHADOW_MODE,
            "designation": a1b_shadow.DESIGNATION,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    @application.get(f"{_API_PREFIX}/overview", tags=["equity-a1b-shadow"])
    def overview() -> a1b_shadow.A1BShadowOverview:
        return build_overview()

    @application.get(f"{_API_PREFIX}/status", tags=["equity-a1b-shadow"])
    def status() -> a1b_shadow.ServicePanel:
        return build_overview().service

    @application.get(f"{_API_PREFIX}/latest", tags=["equity-a1b-shadow"])
    def latest() -> dict[str, Any]:
        page = build_overview()
        return {
            "generated_at": page.generated_at,
            "hypothetical_label": page.hypothetical_label,
            "regime": page.regime,
            "symbols": list(page.symbols),
        }

    @application.get(f"{_API_PREFIX}/comparison", tags=["equity-a1b-shadow"])
    def comparison() -> dict[str, Any]:
        page = build_overview()
        return {
            "generated_at": page.generated_at,
            "hypothetical_label": page.hypothetical_label,
            "summary": page.summary,
            "hypothetical": page.hypothetical,
        }

    @application.get(f"{_API_PREFIX}/history", tags=["equity-a1b-shadow"])
    def history(
        limit: Annotated[
            int,
            Query(ge=1, le=a1b_shadow.HISTORY_MAX_LIMIT, description="Rows, newest first."),
        ] = a1b_shadow.HISTORY_DEFAULT_LIMIT,
        offset: Annotated[int, Query(ge=0, description="Rows to skip from the newest end.")] = 0,
        symbol: Annotated[str | None, Query(description="Restrict to one universe symbol.")] = None,
    ) -> a1b_shadow.HistoryPage:
        snapshot = a1b_shadow.read_a1b(a1b_shadow.database_path())
        return a1b_shadow.build_history(snapshot, limit=limit, offset=offset, symbol=symbol)

    return application


app = create_app()


def main() -> None:
    """Serve the read-only A1-B Shadow API on loopback."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="python -m autotrader.dashboard.a1b_shadow_api",
        description=(
            "Serve the read-only AutoTrader A1-B U30 Shadow API on 127.0.0.1. Every "
            "route is a GET over an observation record; nothing here can place, "
            "modify, or authorize an order."
        ),
    )
    parser.add_argument("--port", type=int, default=configured_port())
    arguments = parser.parse_args()
    uvicorn.run(
        "autotrader.dashboard.a1b_shadow_api:app",
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
