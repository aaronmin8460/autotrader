"""C9: the runtime's adapter onto the existing C7 paper execution path.

This module adds **no** trading behaviour. It constructs no trading client of
its own, contains no `paper` keyword, builds no order request, and makes no
sizing decision. It calls `execute_paper_order` and hands back what that
returns, so every C7 guarantee still holds unchanged when a daemon is the
caller rather than a human at a terminal:

- the `AUTOTRADER_PAPER_TRADING_ENABLED` environment gate;
- `paper=True`, hardcoded and unreachable by any parameter;
- the risk engine sizing every order, and clamping it;
- the order intent committed to SQLite *before* the broker is called;
- the duplicate preflight, failing closed if it cannot complete;
- exactly one submission attempt, and no retry of an ambiguous one.

The one thing it does add is a *classification*: a broker authentication
failure arrives from alpaca-py as a generic `APIError`, and a runtime that
retried that every fifteen minutes forever would be a broken daemon pretending
to work. It is re-raised as `BrokerAuthenticationError` so the loop can stop.

The clients are built lazily and reused. Lazily, because an observation-only
runtime must never construct a trading client at all; reused, because
rebuilding one per cycle re-reads credentials for no benefit.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from alpaca.common.exceptions import APIError

from autotrader.execution.paper import (
    PaperExecutionResult,
    create_market_data_client,
    create_paper_trading_client,
    execute_paper_order,
)

#: HTTP statuses that mean the credentials themselves are the problem. Neither
#: is transient, so neither may be retried on the next boundary.
_AUTHENTICATION_STATUSES = frozenset({401, 403})


class BrokerAuthenticationError(Exception):
    """The broker refused the credentials. Not transient, not retried."""


class ExecutionGateway(Protocol):
    """How the runtime reaches the paper execution path.

    A protocol so a test can substitute the broker boundary wholesale, and so
    the runtime holds no Alpaca type. The runtime never calls this unless every
    gate is open; the gateway is not where authorization is decided.
    """

    def execute(
        self,
        connection: sqlite3.Connection,
        *,
        symbol: str,
        side: str,
        requested_quantity: Decimal,
        strategy_run_id: int | None,
        now: datetime,
    ) -> PaperExecutionResult:
        """Run one paper execution attempt for one symbol."""


def _http_status(error: APIError) -> int | None:
    """The HTTP status behind an `APIError`, or None when it cannot be known.

    Guarded the same way C7 guards it: `status_code` is derived from a response
    the SDK may not have, and an unreadable status must not become a traceback.
    """
    try:
        status = error.status_code
    except Exception:  # noqa: BLE001 - the attribute is derived, not stored
        return None
    return status if isinstance(status, int) else None


def is_authentication_failure(error: APIError) -> bool:
    """Whether `error` says the credentials were rejected."""
    return _http_status(error) in _AUTHENTICATION_STATUSES


class PaperExecutionGateway:
    """The production gateway. A thin call through to C7."""

    def __init__(
        self,
        trading_client: object | None = None,
        data_client: object | None = None,
    ) -> None:
        self._trading_client = trading_client
        self._data_client = data_client
        #: Execution attempts made, for the later API-budget work. Each one is
        #: several provider calls inside C7 (account, positions, asset, price).
        self.api_calls = 0

    def execute(
        self,
        connection: sqlite3.Connection,
        *,
        symbol: str,
        side: str,
        requested_quantity: Decimal,
        strategy_run_id: int | None,
        now: datetime,
    ) -> PaperExecutionResult:
        if self._trading_client is None:
            self._trading_client = create_paper_trading_client()
        if self._data_client is None:
            self._data_client = create_market_data_client()
        self.api_calls += 1
        try:
            return execute_paper_order(
                connection,
                symbol=symbol,
                side=side,
                requested_quantity=requested_quantity,
                trading_client=self._trading_client,  # type: ignore[arg-type]
                data_client=self._data_client,  # type: ignore[arg-type]
                strategy_run_id=strategy_run_id,
                now=now,
            )
        except APIError as error:
            if is_authentication_failure(error):
                raise BrokerAuthenticationError(
                    "The broker rejected the configured credentials. This is not a "
                    "transient failure, so the runtime stops rather than retrying it "
                    "every fifteen minutes."
                ) from None
            raise


__all__ = [
    "BrokerAuthenticationError",
    "ExecutionGateway",
    "PaperExecutionGateway",
    "is_authentication_failure",
]
