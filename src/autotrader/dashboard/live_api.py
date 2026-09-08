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
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI

from autotrader.dashboard import live_safety

#: Loopback only, and not configurable here. Making "listen on every interface"
#: a one-flag decision is how an unauthenticated internal tool ends up on a
#: public port - and this one names a real-money account.
DEFAULT_HOST = "127.0.0.1"

#: 8006: 8005 is reserved by the frozen accounting contract for Prompt 2.
DEFAULT_PORT = 8006

PORT_ENV = "AUTOTRADER_LIVE_SAFETY_API_PORT"

#: The HTTP methods this application is allowed to expose.
ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})

_API_PREFIX = "/api/live-safety"


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

    The broker client is `None` until real-money read credentials are present in
    this process's environment. That is the state on this program's timeline,
    and it renders as `NOT_CONFIGURED` - not as an empty account, and not as an
    account with no exposure.
    """
    from autotrader.equity.allocation import (
        POLICY_LIVE_VALIDATION_100,
        allocation_policy_for,
    )

    moment = now or datetime.now(UTC)
    account = live_safety.read_live_account(None, now=moment)
    return live_safety.build_panel(
        now=moment,
        policy=allocation_policy_for(POLICY_LIVE_VALIDATION_100),
        account=account,
        arm_state=None,
        observed_fingerprint=None,
        live_ready=None,
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

    return application


app = create_app()


__all__ = [
    "ALLOWED_METHODS",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "PORT_ENV",
    "app",
    "build_panel",
    "configured_port",
    "create_app",
]
