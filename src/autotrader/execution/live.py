"""The real-money broker boundary. The ONE file in this repository that can reach it.

Everything the rest of the codebase says about live trading being impossible
was true, and was worth having: for the whole life of the project the sole
`TradingClient` construction hardcoded the paper environment and there was no
argument, flag, or variable that could change it. Live trading was not disabled
by default; it was unexpressible.

This module ends that, for exactly one file, and the shape of the exception is
the point. The repository-wide source guard was not relaxed into a wildcard -
it now names this path explicitly and asserts that **no second file** may say
what this one says. The guard is stricter after this change than before it,
because it previously had no exception to bound and now has exactly one.

Read this file to know what real money can reach.

**One construction, and it takes no argument.** `create_live_trading_client`
writes the live flag literally. It has no parameter that could change it, and
`create_paper_trading_client` remains untouched and remains the only thing the
paper runtime, the shadow, the crypto runner, and the reconciler ever call.

**The credential names are disjoint from paper's, and that is the isolation.**
This module reads `ALPACA_LIVE_API_KEY` and `ALPACA_LIVE_SECRET_KEY` and
nothing else; the paper factory reads `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`
and nothing else. Neither can observe the other's value. A paper secrets file
loaded into a live process yields no credential at all and the process refuses
to start rather than trading a paper key against real money; the reverse is
equally true. This is a property of the names, not of anybody's discipline.

**It submits nothing.** There is deliberately no submission function here. The
live path reaches the broker through `execution.equity`, which reaches it
through `execution.paper.submit_order_intent` - the one place the durable
intent, the duplicate preflight, the exactly-once rule and the
never-retry-an-ambiguous-outcome rule actually live. Copying that logic for
real money would have been the single most dangerous thing this program could
do: two implementations of at-most-once is zero implementations of it.

**Four gates, and every one of them fails closed.** Before a live client is
handed to anything that could mutate, all four must pass:

1. the credentials exist, and are not the paper ones wearing a new name;
2. the client provably reaches the real-money host and reports itself as not a
   sandbox - an attribute that is missing, of an unexpected type, or pointing
   elsewhere is a refusal, never a "probably";
3. the account that answers matches the pinned fingerprint - see
   `autotrader.live.identity`, and note that this is a different question from
   "is this live", because every live account looks like every other one;
4. the durable arm row and the environment arm gate are both open - see
   `autotrader.live.armstate`.

Scope: US equities, the ten U10 symbols, fractional quantities, MARKET orders,
DAY time in force, regular hours, long only. No crypto, no options, no shorts,
no extended hours, no margin, no streaming - and no cancel, replace, liquidate,
transfer or withdrawal call, here or anywhere else in this repository.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from alpaca.common.enums import BaseURL
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.trading.client import TradingClient

from autotrader.execution.models import ExecutionError
from autotrader.live.armstate import ArmState, require_armed
from autotrader.live.identity import require_expected_account

#: The real-money credential variables. Deliberately not the paper names.
LIVE_API_KEY_ENV = "ALPACA_LIVE_API_KEY"
LIVE_SECRET_KEY_ENV = "ALPACA_LIVE_SECRET_KEY"

#: The paper names, referenced only so this module can refuse a configuration
#: in which somebody has put one credential in both places.
_PAPER_API_KEY_ENV = "ALPACA_API_KEY"

#: The one real-money host. Taken from the SDK's own enum rather than typed
#: out, so it cannot drift from the URL the live flag actually selects - and so
#: the literal hostname still appears nowhere in this repository's source.
LIVE_TRADING_BASE_URL = BaseURL.TRADING_LIVE.value


class NotLiveEnvironmentError(ExecutionError):
    """A trading client could not be **proven** to reach the real-money host.

    Deliberately not the same as "this client is paper": a client whose
    environment cannot be read at all fails here too. Proving the environment
    is the caller's burden, and an unproven client is refused rather than
    assumed to be whichever one would be convenient.
    """


class MissingLiveCredentialsError(ExecutionError):
    """Real-money credentials are not configured, or are the paper ones.

    Raised before any broker call. The message names the *variables*, never
    their values.
    """


def live_credentials_configured() -> bool:
    """Whether both real-money credential variables hold a value."""
    return bool(os.environ.get(LIVE_API_KEY_ENV, "").strip()) and bool(
        os.environ.get(LIVE_SECRET_KEY_ENV, "").strip()
    )


def _require_live_credentials() -> tuple[str, str]:
    """The configured real-money credentials, or a refusal naming the variables.

    The values are returned for immediate use by a client constructor and are
    never logged, persisted, embedded in a `client_order_id`, or included in an
    exception message.

    The second check is the one worth explaining. Disjoint variable names stop
    a paper key from reaching the live host by accident, but they cannot stop
    somebody pasting the same key into both files - and a system that did that
    would have two environments sharing one credential while every report it
    produced claimed they were separate. So the two are compared, and an
    identical pair is refused. The comparison is between two values this
    process already holds; nothing is printed and nothing is sent.
    """
    if not live_credentials_configured():
        raise MissingLiveCredentialsError(
            f"Real-money credentials are not configured. Set {LIVE_API_KEY_ENV} and "
            f"{LIVE_SECRET_KEY_ENV}. Note that these are deliberately NOT the paper "
            "variables: a paper credential cannot be used here under any name."
        )
    api_key = os.environ[LIVE_API_KEY_ENV].strip()
    secret_key = os.environ[LIVE_SECRET_KEY_ENV].strip()

    paper_key = os.environ.get(_PAPER_API_KEY_ENV, "").strip()
    if paper_key and paper_key == api_key:
        raise MissingLiveCredentialsError(
            f"{LIVE_API_KEY_ENV} holds the same value as {_PAPER_API_KEY_ENV}. Two "
            "environments sharing one credential is not two environments. Nothing was "
            "fetched and no order was submitted."
        )
    return api_key, secret_key


def create_live_trading_client() -> TradingClient:
    """Build the real-money trading client. The one construction in this repository.

    The environment is written literally below and is not derived from a
    parameter, a setting, or the environment. This function takes no argument
    that could change it.

    The SDK's own request retry is switched off, for the same reason the paper
    factory switches it off: the SDK retries `429` and `504` internally, which
    is harmless for a `GET` and unacceptable for `POST /orders`. A gateway
    timeout there is precisely the ambiguous case that must be classified
    `UNKNOWN`, and a silent resubmission would defeat the entire at-most-once
    design. The constructor cannot express "no retries" - it ignores a zero -
    so the attribute is set directly, and a test asserts it stays effective.
    """
    api_key, secret_key = _require_live_credentials()
    client = TradingClient(
        api_key=api_key,
        secret_key=secret_key,
        paper=False,
    )
    client._retry = 0
    return client


def create_live_market_data_client() -> StockHistoricalDataClient:
    """Build a stock-data reader from the isolated Live credential pair."""
    api_key, secret_key = _require_live_credentials()
    return StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)


class LiveReadOnlyClient:
    """An allowlisted read facade over a real-money trading client.

    Observer processes receive this object rather than the SDK client it
    contains. There is no generic attribute forwarding and no generic request
    method, so every capability is visible in this class.
    """

    def __init__(self, client: TradingClient) -> None:
        self.__client = client
        self.read_count = 0

    @property
    def _base_url(self) -> object:
        return getattr(self.__client, "_base_url", None)

    @property
    def _sandbox(self) -> object:
        return getattr(self.__client, "_sandbox", None)

    def get_account(self) -> object:
        self.read_count += 1
        return self.__client.get_account()

    def get_all_positions(self) -> object:
        self.read_count += 1
        return self.__client.get_all_positions()

    def get_orders(self, request: object | None = None) -> object:
        self.read_count += 1
        return self.__client.get_orders(request)  # type: ignore[arg-type]

    def get_order_by_client_id(self, client_order_id: str) -> object:
        self.read_count += 1
        return self.__client.get_order_by_client_id(client_order_id)

    def get_clock(self) -> object:
        self.read_count += 1
        return self.__client.get_clock()

    def get_asset(self, symbol: str) -> object:
        self.read_count += 1
        return self.__client.get_asset(symbol)

    def get_calendar(self, request: object) -> object:
        self.read_count += 1
        return self.__client.get_calendar(request)  # type: ignore[arg-type]

    def get_account_activities(
        self,
        activity_type: str,
        after: datetime | None = None,
        *,
        max_requests: int = 20,
        page_size: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        """Read one account-activity type completely, with bounded paging."""
        kind = str(activity_type).strip().upper()
        if not kind or not kind.isalnum():
            raise ValueError("activity_type must be a non-empty alphanumeric broker type")
        if max_requests < 1 or page_size < 1:
            raise ValueError("activity pagination bounds must be positive")

        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        page_token: str | None = None
        for _ in range(max_requests):
            params: dict[str, object] = {"direction": "asc", "page_size": page_size}
            if after is not None:
                params["after"] = after.astimezone(UTC).isoformat()
            if page_token is not None:
                params["page_token"] = page_token
            self.read_count += 1
            payload = self.__client.get(f"/account/activities/{kind}", params)
            if payload is None:
                return tuple(rows)
            if not isinstance(payload, list):
                raise RuntimeError("the broker returned account activities in an unknown shape")
            if not payload:
                return tuple(rows)
            fresh = 0
            for item in payload:
                if not isinstance(item, dict):
                    raise RuntimeError(
                        "the broker returned an account activity in an unknown shape"
                    )
                identity = str(item.get("id", "")).strip()
                if identity and identity in seen:
                    continue
                if identity:
                    seen.add(identity)
                rows.append(item)
                fresh += 1
            page_token = str(payload[-1].get("id", "")).strip() or None
            if fresh == 0 or page_token is None or len(payload) < page_size:
                return tuple(rows)
        raise RuntimeError(f"the account activity record did not end within {max_requests} reads")


def create_live_read_only_client() -> LiveReadOnlyClient:
    """Build the allowlisted facade used by Live observer processes."""
    return LiveReadOnlyClient(create_live_trading_client())


def verify_live_environment(client: TradingClient) -> str:
    """Return the real-money base URL, or raise unless `client` provably reaches it.

    The mirror of `execution.paper.verify_paper_environment`, and it exists for
    the mirror-image reason. There, the check is defensive: no live factory
    exists, so the check confirms what could not have been otherwise. Here it
    is load-bearing, because a *paper* client can be constructed - it is what
    the rest of this system builds - and handing one to a real-money runtime
    would mean a process reporting real-money activity while trading paper.

    Two facts are required and both are read defensively: the base URL the
    client will actually send requests to must be the real-money host, and the
    SDK's own sandbox flag must be explicitly False. An attribute that is
    missing, of an unexpected type, or pointing anywhere else **fails closed**.

    Read-only. It sends no request, so it costs nothing and cannot itself fail
    on the network.
    """
    base_url = getattr(client, "_base_url", None)
    if isinstance(base_url, Enum):
        base_url = base_url.value
    if not isinstance(base_url, str) or base_url.strip().rstrip("/") != LIVE_TRADING_BASE_URL:
        raise NotLiveEnvironmentError(
            "Refusing to operate: this trading client could not be proven to reach the "
            "real-money environment. A client that cannot prove which broker it reaches "
            "is not one this runtime will act on. Nothing was submitted."
        )
    if getattr(client, "_sandbox", None) is not False:
        raise NotLiveEnvironmentError(
            "Refusing to operate: this trading client points at the real-money host but "
            "does not report itself as a non-sandbox client. Nothing was submitted."
        )
    return LIVE_TRADING_BASE_URL


def require_live_submission_allowed(
    connection: sqlite3.Connection, client: TradingClient
) -> tuple[str, ArmState]:
    """Every gate that stands between this process and a real-money order.

    Called immediately before a mutation, not once at startup, because an arm
    switch that only mattered at boot would not be a kill switch and an account
    pin checked once would not survive a credential rotated under a running
    process.

    The order is deliberate and is cheapest-first only by coincidence; what
    actually determines it is which refusal is most informative. Environment
    before identity, because a client that reaches the wrong host makes the
    account question meaningless. Identity before arming, because being armed
    against the wrong account is a more alarming thing to report than being
    disarmed against the right one.

    Returns the account fingerprint that matched and the arm state that
    permitted it, so a caller can log both without logging either secret.

    Submits nothing. Every call inside is a `GET` or a local read.
    """
    verify_live_environment(client)
    fingerprint = require_expected_account(client)
    arm_state = require_armed(connection)
    return fingerprint, arm_state


__all__ = [
    "LIVE_API_KEY_ENV",
    "LIVE_SECRET_KEY_ENV",
    "LIVE_TRADING_BASE_URL",
    "LiveReadOnlyClient",
    "MissingLiveCredentialsError",
    "NotLiveEnvironmentError",
    "create_live_market_data_client",
    "create_live_read_only_client",
    "create_live_trading_client",
    "live_credentials_configured",
    "require_live_submission_allowed",
    "verify_live_environment",
]
