"""The market-charts HTTP surface. GET only, and structurally so.

Three routes, every one of them a read:

    GET /api/market-charts/health    process liveness; contacts nothing
    GET /api/market-charts/ranges    the supported display ranges
    GET /api/market-charts/bars      one batched series set: ?symbols=A,B&range=1D

**A separate process, deliberately.** This is the one dashboard process that
talks to the market-data provider, and it talks to nothing else: no store is
opened, no account is read, and the trading runtimes neither share its cache
nor depend on it. A browser that asks for a chart the provider will not serve
gets an `unavailable_reason`; the account panels beside it are unaffected.

**There is no write endpoint.** No `POST`, `PUT`, `PATCH` or `DELETE`, and
nothing behind a route that could act: the provider clients this process
builds are historical-data clients with no order surface.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query

from autotrader.dashboard import charts

#: Loopback only, and not configurable here.
DEFAULT_HOST = "127.0.0.1"

#: 8004: 8000-8003 are the four record readers.
DEFAULT_PORT = 8004

PORT_ENV = "AUTOTRADER_MARKET_CHARTS_API_PORT"

ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})

_API_PREFIX = "/api/market-charts"

#: One cache for the whole process. See `ChartCache`.
CACHE = charts.ChartCache()


def configured_port() -> int:
    raw = os.environ.get(PORT_ENV)
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_PORT


def create_app(cache: charts.ChartCache | None = None) -> FastAPI:
    store = cache if cache is not None else CACHE
    application = FastAPI(
        title="AutoTrader market charts",
        version="0.1.0",
        summary="Read-only display bars for the dashboard's charts. Not the trading data path.",
        description=(
            "Every route is a GET. Series are provider bars at display timeframes, "
            "batched per request, cached per range, and capped per minute. Nothing "
            "here reads an account, opens a store, or can place an order."
        ),
    )

    @application.get(f"{_API_PREFIX}/health", tags=["market-charts"])
    def health() -> dict[str, Any]:
        """Liveness. Contacts no provider and opens nothing."""
        return {
            "status": "ok",
            "read_only": True,
            "trading_path": False,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    @application.get(f"{_API_PREFIX}/ranges", tags=["market-charts"])
    def ranges() -> dict[str, Any]:
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "ranges": charts.ranges_payload(),
            "max_symbols_per_request": charts.MAX_SYMBOLS_PER_REQUEST,
            "max_provider_calls_per_minute": charts.MAX_PROVIDER_CALLS_PER_MINUTE,
        }

    @application.get(f"{_API_PREFIX}/bars", tags=["market-charts"])
    def bars(
        symbols: Annotated[str, Query(description="Comma-separated symbols, at most twelve.")],
        range: Annotated[  # noqa: A002 - the query parameter is named for the operator
            str, Query(description="One of the supported display ranges.")
        ] = "1D",
    ) -> charts.ChartBatch:
        try:
            return store.read(symbols.split(","), range, now=datetime.now(UTC))
        except charts.ChartRequestError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    return application


app = create_app()


def main() -> None:
    """Serve the read-only market-charts API on loopback."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="python -m autotrader.dashboard.charts_api",
        description=(
            "Serve the read-only AutoTrader market-charts API on 127.0.0.1. Display "
            "bars only; nothing here can read an account or place an order."
        ),
    )
    parser.add_argument("--port", type=int, default=configured_port())
    arguments = parser.parse_args()
    uvicorn.run(
        "autotrader.dashboard.charts_api:app",
        host=DEFAULT_HOST,
        port=arguments.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()


__all__ = [
    "ALLOWED_METHODS",
    "CACHE",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "PORT_ENV",
    "app",
    "configured_port",
    "create_app",
    "main",
]
