"""The read-only Live accounting API. GET routes over a performance ledger.

Read-only for the strongest reason of any API in this repository. The other
dashboards describe processes that place orders; this one describes **money the
operator might withdraw**, and the absence of a write route is what stands
between a dashboard viewer and a transfer.

There is no route here that initiates a transfer, schedules one, authorizes a
withdrawal, edits the bucket, adjusts the high-water mark or overrides the
reserve rate. `ALLOWED_METHODS` is asserted against the assembled route table,
so adding a POST fails the suite rather than shipping.

Underneath the policy sits a stronger fact: the broker's Trading API - the only
surface this repository can reach - publishes no transfer endpoint at all, so
there is nothing for a write route to call even if somebody wrote one.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI

from autotrader.dashboard import live_accounting, live_history

#: Loopback only, and not configurable here.
DEFAULT_HOST = "127.0.0.1"

#: 8005: 8000 operational, 8001 equity shadow, 8002 equity paper, 8003 A1-B
#: shadow, 8004 market charts.
DEFAULT_PORT = 8005

PORT_ENV = "AUTOTRADER_LIVE_ACCOUNTING_API_PORT"

#: The HTTP methods this application is allowed to expose. Asserted.
ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})

_API_PREFIX = "/api/live-accounting"


def configured_port() -> int:
    raw = os.environ.get(PORT_ENV)
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_PORT


def create_app() -> FastAPI:
    application = FastAPI(
        title="AutoTrader Live Accounting",
        version="1.0.0",
        summary=(
            "Read-only flow-adjusted performance for the real-money book. "
            "OBSERVE_ONLY: no transfer, no withdrawal, no money movement."
        ),
        description=(
            "Every route is a GET. External deposits and withdrawals are normalized out "
            "of performance, so a deposit never reads as profit and a withdrawal never "
            "reads as a loss. The Profit Reserve and the Withdrawal Bucket are accounting "
            "lines only - they are NOT the EDA-1 cash reserve, they move no money, and "
            "they change no position size. Fields are frozen by "
            "docs/LIVE_ACCOUNTING_API_CONTRACT.md."
        ),
    )

    @application.get(f"{_API_PREFIX}/health", tags=["live-accounting"])
    def health() -> dict[str, Any]:
        """Process liveness. Touches no database and reads no broker."""
        return {
            "status": "ok",
            "checked_at": datetime.now(UTC).isoformat(),
            "withdrawal_mode": "OBSERVE_ONLY",
        }

    @application.get(f"{_API_PREFIX}/summary", tags=["live-accounting"])
    def summary() -> dict[str, Any]:
        """The frozen contract payload."""
        return live_accounting.build_summary()

    @application.get(f"{_API_PREFIX}/history", tags=["live-accounting"])
    def history() -> dict[str, Any]:
        """Authoritative checkpoint series and external-flow markers."""
        return live_history.build_history()

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


app = create_app()


def main() -> None:
    """Run the loopback-only frozen accounting API."""
    import uvicorn

    uvicorn.run(app, host=DEFAULT_HOST, port=configured_port(), log_level="info")


__all__ = [
    "ALLOWED_METHODS",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "PORT_ENV",
    "app",
    "configured_port",
    "create_app",
    "main",
    "route_methods",
]


if __name__ == "__main__":  # pragma: no cover - exercised by the service manager
    main()
