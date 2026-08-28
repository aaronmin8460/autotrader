"""Run the dashboard API on loopback: ``python -m autotrader.dashboard``.

A development entry point, and deliberately a small one. It binds `127.0.0.1`
and takes no host argument: this API has no authentication, and making "listen
on every interface" a one-flag decision is how an unauthenticated internal tool
ends up on a public port. Serving it beyond this machine needs a reverse proxy
and an authentication layer in front of it, which is a deployment concern and
is not part of this milestone.

`--port` exists because 8000 is a busy number on a developer's machine.
Nothing else is configurable here; the database path comes from
`AUTOTRADER_DASHBOARD_DB` (see `api.database_path`).
"""

from __future__ import annotations

import argparse

import uvicorn

from autotrader.dashboard.api import DEFAULT_HOST, DEFAULT_PORT


def main() -> None:
    """Serve the read-only dashboard API on loopback."""
    parser = argparse.ArgumentParser(
        prog="python -m autotrader.dashboard",
        description=(
            "Serve the read-only AutoTrader dashboard API on 127.0.0.1. "
            "Every route is a GET; nothing here can place or modify an order."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Loopback port to bind (default {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Reload on source changes. Development only.",
    )
    arguments = parser.parse_args()
    uvicorn.run(
        "autotrader.dashboard.api:app",
        host=DEFAULT_HOST,
        port=arguments.port,
        reload=arguments.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
