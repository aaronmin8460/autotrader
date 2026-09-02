"""The read-only Equity Paper API. GET routes over an execution record.

The sibling of `equity_shadow_api`, and read-only for a stronger reason. The
shadow's API is read-only because the process behind it cannot mutate anything;
this one describes a process that *can*, so the absence of a write route is the
only thing standing between a dashboard viewer and a broker order. There is no
route here that starts, stops, advances a stage, resizes a target, cancels an
order or edits stored state, and `ALLOWED_METHODS` is asserted against the
assembled route table so adding one fails the suite rather than shipping.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import FastAPI, Query

from autotrader.dashboard import account_orders, equity_paper, realized_pnl, service_units

#: Loopback only, and not configurable here. Making "listen on every interface"
#: a one-flag decision is how an unauthenticated internal tool ends up on a
#: public port.
DEFAULT_HOST = "127.0.0.1"

#: 8002: 8000 is the operational dashboard API and 8001 is the equity shadow's.
DEFAULT_PORT = 8002

PORT_ENV = "AUTOTRADER_EQUITY_PAPER_API_PORT"

#: The HTTP methods this application is allowed to expose.
ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})

_API_PREFIX = "/api/equity-paper"


def configured_port() -> int:
    raw = os.environ.get(PORT_ENV)
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_PORT


def build_overview() -> equity_paper.EquityPaperOverview:
    """One poll of the Equity Paper page, from the configured stores."""
    return equity_paper.build_overview(now=datetime.now(UTC))


def create_app() -> FastAPI:
    application = FastAPI(
        title="AutoTrader Equity Paper",
        version="0.1.0",
        summary="Read-only record of the EDA-1 equity paper book. ALPACA PAPER, NO REAL MONEY.",
        description=(
            "Every route is a GET over an execution record. Positions and fills here "
            "are REAL paper-broker facts, not the shadow's hypothetical curve, and "
            "the two must never be added together. Exposure is reported ACCOUNT-WIDE "
            "because the 30% ceiling it is measured against is an account ceiling and "
            "includes the crypto book. Nothing here can place, cancel or modify an "
            "order, advance the rollout stage, change the sizing policy, or edit "
            "stored state: there is no endpoint for any of it."
        ),
    )

    @application.get(f"{_API_PREFIX}/health", tags=["equity-paper"])
    def health() -> dict[str, Any]:
        """Liveness. Opens no database and contacts no broker."""
        return {
            "status": "ok",
            "read_only": True,
            "mode": equity_paper.PAPER_MODE,
            "environment": "PAPER",
            "real_money": False,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    @application.get(f"{_API_PREFIX}/overview", tags=["equity-paper"])
    def overview() -> equity_paper.EquityPaperOverview:
        """The whole page: service, regime, exposure, targets, orders, safety."""
        return build_overview()

    @application.get(f"{_API_PREFIX}/status", tags=["equity-paper"])
    def status() -> equity_paper.ServicePanel:
        """Whether the runtime is running, at what stage, under which policy."""
        return build_overview().service

    @application.get(f"{_API_PREFIX}/targets", tags=["equity-paper"])
    def targets() -> dict[str, Any]:
        """Per symbol: the EDA-1 decision, and what the account actually holds."""
        page = build_overview()
        return {
            "generated_at": page.generated_at,
            "regime": page.regime,
            "exposure": page.exposure,
            "targets": list(page.targets),
        }

    @application.get(f"{_API_PREFIX}/orders", tags=["equity-paper"])
    def orders() -> dict[str, Any]:
        """Durable intents and what the broker said about each of them."""
        page = build_overview()
        return {"generated_at": page.generated_at, "orders": list(page.orders)}

    @application.get(f"{_API_PREFIX}/services", tags=["runtime-units"])
    def services() -> service_units.ServiceUnitsPanel:
        """Every AutoTrader runtime unit on this host, as the manager reports it.

        Wider than this API's name, and here on purpose. Three separate
        services answer to the word "equity" on this host, and the panel that
        has to tell them apart cannot be built from any one store: the store
        that records the legacy runtime knows nothing about the paper runtime
        that replaced it, which is precisely how the operations page came to
        report equity trading as stopped while it was running.

        So the question is asked of the service manager instead, and it is
        asked from this process because this is the least privileged of the
        three readers - it holds no broker credential and cannot open the
        trading database. A read-only property query needs neither.

        GET, like everything else here. Nothing in this application can start,
        stop, restart, enable, disable or unmask a unit, and `service_units`
        knows exactly one verb.
        """
        return service_units.build_panel(now=datetime.now(UTC))

    @application.get(f"{_API_PREFIX}/safety", tags=["equity-paper"])
    def safety() -> dict[str, Any]:
        """Account safety, reconciliation, parity, and risk-blocked targets."""
        page = build_overview()
        return {
            "generated_at": page.generated_at,
            "safety": page.safety,
            "unresolved_intents": page.service.unresolved_intents,
        }

    @application.get(f"{_API_PREFIX}/policy", tags=["equity-paper"])
    def policy() -> equity_paper.PolicyPanel | None:
        """The deployed sizing policy's target and hard caps, as numbers."""
        return build_overview().policy

    @application.get(f"{_API_PREFIX}/account-orders", tags=["account"])
    def account_wide_orders(
        limit: Annotated[
            int,
            Query(
                ge=1,
                le=account_orders.MAX_LIMIT,
                description="Rows to return, newest first, across both stores.",
            ),
        ] = account_orders.DEFAULT_LIMIT,
    ) -> account_orders.AccountOrdersPanel:
        """Recent orders across the whole account: crypto and equity paper, merged.

        Served from this process because it is the one reader that can open
        both order stores read-only. Rows are labelled with the store they came
        from; no shadow record is an input.
        """
        return account_orders.build_account_orders(
            crypto_path=equity_paper.crypto_database_path(),
            paper_path=equity_paper.database_path(),
            now=datetime.now(UTC),
            limit=limit,
        )

    # ----------------------------------------------------------------------
    # Realized P&L
    #
    # Accounting, served from a store no trading process writes and this
    # process cannot write either. Reads only, like everything else here, but
    # with one extra property worth stating: these routes describe a ledger
    # that is subordinate to the broker. When the two disagree the payload's
    # `status` says `MISMATCH` or `UNKNOWN`, and a renderer must show that
    # rather than the figures beside it.
    # ----------------------------------------------------------------------

    @application.get(f"{_API_PREFIX}/realized-pnl/summary", tags=["realized-pnl"])
    def realized_pnl_summary() -> realized_pnl.RealizedPnlPanel:
        """Realized trade P&L today and since tracking started, with status.

        Not the daily account P&L, which is account equity against the stored
        UTC-day baseline and belongs to the risk engine; not unrealized P&L,
        which is the broker's figure over broker positions. Three different
        measurements, and `components_are_independent` says on the wire that
        they are not required to sum.
        """
        return realized_pnl.build_panel(now=datetime.now(UTC))

    @application.get(f"{_API_PREFIX}/realized-pnl/by-symbol", tags=["realized-pnl"])
    def realized_pnl_by_symbol() -> dict[str, Any]:
        """Per-symbol realized totals joined to the cost basis the ledger holds."""
        return {"symbols": realized_pnl.build_by_symbol(now=datetime.now(UTC))}

    @application.get(f"{_API_PREFIX}/realized-pnl/events", tags=["realized-pnl"])
    def realized_pnl_events(
        symbol: Annotated[
            str | None, Query(description="Restrict to one symbol.", max_length=16)
        ] = None,
        limit: Annotated[
            int,
            Query(
                ge=1,
                le=realized_pnl.MAX_EVENT_LIMIT,
                description="Realized events to return, newest first.",
            ),
        ] = realized_pnl.DEFAULT_EVENT_LIMIT,
    ) -> dict[str, Any]:
        """Realized events, newest first. One row per broker-confirmed sale.

        Deliberately *events*, not trades. This book trims on drift, so a
        symbol can produce many sales without closing anything, and round-trip
        trade boundaries are not something this system defines.
        """
        key = symbol.strip().upper() if symbol else None
        return {"events": realized_pnl.build_events(symbol=key, limit=limit)}

    @application.get(f"{_API_PREFIX}/realized-pnl/status", tags=["realized-pnl"])
    def realized_pnl_status() -> dict[str, Any]:
        """The accounting horizon and the ledger-against-broker verdict."""
        return {
            "status": realized_pnl.build_status(),
            "reconciliation": realized_pnl.build_reconciliation(),
        }

    @application.get(f"{_API_PREFIX}/symbols/{{symbol}}/realized-pnl", tags=["realized-pnl"])
    def symbol_realized_pnl(symbol: str) -> realized_pnl.SymbolRealizedPanel:
        """One symbol's realized totals and its realized-event history."""
        return realized_pnl.build_symbol_panel(symbol, now=datetime.now(UTC))

    return application


app = create_app()


def main() -> None:
    """Serve the read-only Equity Paper API on loopback."""
    import uvicorn

    uvicorn.run(app, host=DEFAULT_HOST, port=configured_port(), log_level="info")


if __name__ == "__main__":  # pragma: no cover - process entry point
    main()
