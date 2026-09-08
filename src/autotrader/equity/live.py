"""EDA-1 against real money: the startup sequence, and nothing else.

This module is deliberately small, and what it does *not* contain is the point.
It holds no decision logic, no allocator, no risk arithmetic, no order path and
no cycle loop. All of that is `autotrader.equity.paper`, unchanged and shared,
because the champion running against real money must be the same code that ran
against paper - a second implementation would be a second strategy nobody
validated, and a second at-most-once would be none.

What real money genuinely needs that paper does not is a **startup sequence**,
and that is what lives here:

    process start
      -> real-money credentials, disjoint from paper's
      -> a client provably reaching the real-money host
      -> the account that answers matches its pinned fingerprint
      -> the broker account, positions and open orders are read
      -> local operational state is read
      -> reconciliation runs against real-money broker truth
      -> the reconciliation verdict permits trading, or the process stops
      -> the risk state is established from the settled balance
      -> ONLY THEN is submission eligibility possible at all
      -> and even then, every mutation re-asks the arm switch

Each arrow is a place this module refuses. None of them is recoverable by
retrying, and none of them has a default that means "probably fine".

**Reconciliation is a precondition, not a chore.** `CLEAN` and `REPAIRED` are
the only verdicts that permit submission. `UNRESOLVED` means the pass ran and
found something it could not settle; `FAILED` means it could not run. Both stop
the process, and they are kept distinct because they call for different
operator action even though they have the same consequence.

**The arm switch is asked per mutation, not per process.** A switch consulted
once at boot would not be a kill switch: an operator hitting it at 10:15 needs
the next decision to see it, not the next restart.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from autotrader.equity import EQUITY_SYMBOLS, EquityError
from autotrader.equity.allocation import AllocationPolicy
from autotrader.equity.paper import (
    ENVIRONMENT_LIVE,
    AlpacaEquityPaperGateway,
    EquityPaperExecutionGateway,
)
from autotrader.execution.live import (
    require_live_submission_allowed,
    verify_live_environment,
)
from autotrader.execution.models import ExecutionError, OrderSide
from autotrader.execution.paper import PaperExecutionResult
from autotrader.live.armstate import LiveDisarmedError, read_arm_state
from autotrader.live.budget import LiveExposureCeilings, effective_ceilings
from autotrader.live.identity import AccountIdentityError, require_expected_account
from autotrader.reconciliation.engine import reconcile_paper_state
from autotrader.reconciliation.models import ReconciliationResult

#: The runtime lock scope. Distinct from the paper runtime's, the shadow's and
#: the crypto runner's, so none blocks another - while a second real-money
#: process against the same store is still refused.
EQUITY_LIVE_LOCK_SCOPE = "equity-live"

#: The confirmation token the command line requires. It names what it starts.
LIVE_RUNTIME_CONFIRMATION = "REAL-MONEY"

#: Alpaca permits 48 characters. The prefix and UUID hex fill that allowance
#: exactly, putting Live idempotency keys in a broker-visible namespace that
#: can never collide with Paper's ``autotrader-<hyphenated UUID>`` namespace.
LIVE_CLIENT_ORDER_ID_PREFIX = "autotrader-live-"


def new_live_client_order_id() -> str:
    """Mint one opaque idempotency key in the dedicated Live namespace."""
    return f"{LIVE_CLIENT_ORDER_ID_PREFIX}{uuid.uuid4().hex}"


#: Audit event types this startup writes.
EVENT_LIVE_STARTUP_VERIFIED = "LIVE_STARTUP_VERIFIED"
EVENT_LIVE_STARTUP_REFUSED = "LIVE_STARTUP_REFUSED"


class LiveStartupError(EquityError):
    """A real-money precondition failed. Nothing was fetched or submitted."""


class ReconciliationBlockedError(LiveStartupError):
    """Reconciliation did not conclude that trading is safe.

    Deliberately its own class. A caller catching a generic startup failure
    should still be able to see that the *specific* reason was an unsettled
    account, because that is the one an operator resolves by reconciling rather
    than by fixing configuration.
    """


class LiveEntryGuardError(ExecutionError):
    """A new entry was refused by the non-trade cash-movement guard."""


@dataclass(frozen=True)
class LiveStartupReport:
    """What the startup sequence established, for the log and the dashboard."""

    account_fingerprint: str
    base_url: str
    equity: Decimal
    cash: Decimal
    spendable_cash: Decimal
    ceilings: LiveExposureCeilings
    reconciliation_status: str
    open_order_count: int
    position_count: int
    armed: bool
    arm_reason: str

    def to_json_dict(self) -> dict[str, object]:
        return {
            "account_fingerprint": self.account_fingerprint,
            "equity": str(self.equity),
            "cash": str(self.cash),
            "spendable_cash": str(self.spendable_cash),
            "ceilings": self.ceilings.to_json_dict(),
            "reconciliation_status": self.reconciliation_status,
            "open_order_count": self.open_order_count,
            "position_count": self.position_count,
            "armed": self.armed,
            "arm_reason": self.arm_reason,
        }


def verify_live_startup(
    connection: sqlite3.Connection,
    client: object,
    *,
    policy: AllocationPolicy,
    now: datetime,
    reconcile: Callable[..., ReconciliationResult] = reconcile_paper_state,
) -> LiveStartupReport:
    """Run every real-money precondition, in order, or refuse.

    Returns a report describing what was established. Raises on any refusal,
    and the exception type says which gate: `AccountIdentityError` for the pin,
    `ReconciliationBlockedError` for an unsettled account, `LiveStartupError`
    for everything else.

    **Nothing here submits.** Every broker call in this function and in the
    reconciliation pass it runs is a read. The arm switch is *reported* rather
    than required, because a disarmed process must still be able to complete
    this whole sequence - observing, reconciling and reporting is exactly what
    a disarmed real-money runtime is for.
    """
    from autotrader.execution.equity import fetch_open_paper_orders
    from autotrader.execution.paper import fetch_paper_account_state, fetch_paper_positions

    base_url = verify_live_environment(client)  # type: ignore[arg-type]
    fingerprint = require_expected_account(client)

    account = fetch_paper_account_state(client)  # type: ignore[arg-type]
    if not account.tradable:
        raise LiveStartupError(
            f"Refusing to start: the account reports status {account.status} and cannot "
            "place orders. Nothing was submitted."
        )

    positions = fetch_paper_positions(client)  # type: ignore[arg-type]
    open_orders = fetch_open_paper_orders(client)  # type: ignore[arg-type]

    # Reconciliation, against real-money broker truth. The environment verifier
    # is passed explicitly: the default proves paper, and a real-money pass that
    # accepted the default would be repairing local state from the wrong broker.
    result = reconcile(
        connection,
        trading_client=client,
        now=now,
        symbols=EQUITY_SYMBOLS,
        verify_environment=verify_live_environment,
    )
    if not result.safe_to_trade:
        raise ReconciliationBlockedError(
            f"Refusing to start: reconciliation concluded {result.status.value}, and only "
            "CLEAN or REPAIRED permit trading. An account whose true position is not "
            "established is one every risk number would be measured against wrongly. "
            "Nothing was submitted."
        )

    equity = Decimal(str(account.equity))
    ceilings = effective_ceilings(policy, verified_equity=equity)
    arm_state = read_arm_state(connection)

    return LiveStartupReport(
        account_fingerprint=fingerprint,
        base_url=base_url,
        equity=equity,
        cash=Decimal(str(account.cash)),
        spendable_cash=Decimal(str(account.spendable_cash)),
        ceilings=ceilings,
        reconciliation_status=result.status.value,
        open_order_count=len(open_orders),
        position_count=len(positions),
        armed=arm_state.armed,
        arm_reason=arm_state.reason,
    )


class ArmedLiveGateway:
    """The paper gateway, with the arm switch asked immediately before each order.

    Composition rather than a second gateway: `AlpacaEquityPaperGateway` already
    holds the validated route into `execute_equity_paper_order`, and every
    safety step on that route - the account read, the short refusal, the risk
    evaluation, the market-clock gate, the durable intent, the duplicate
    preflight, the exactly-once submission - belongs there and is not
    re-implemented, re-ordered or relaxed here.

    What this adds is one question asked at the last possible moment: *may this
    process mutate real money right now?* Asked here rather than at startup
    because that is the difference between a kill switch and a boot flag.
    """

    def __init__(
        self,
        *,
        trading_client: object,
        data_client: object | None = None,
        account_lock: object | None = None,
        policy: AllocationPolicy | None = None,
        entry_guard: Callable[[datetime], str | None] | None = None,
    ) -> None:
        self._client = trading_client
        self._entry_guard = entry_guard
        self._inner = AlpacaEquityPaperGateway(
            trading_client=trading_client,
            data_client=data_client,
            account_lock=account_lock,
            policy=policy,
            before_mutation=self._require_mutation_allowed,
            client_order_id_factory=new_live_client_order_id,
        )

    def _require_mutation_allowed(
        self,
        connection: sqlite3.Connection,
        client: object,
        side: OrderSide,
        now: datetime,
    ) -> None:
        require_live_submission_allowed(connection, client)  # type: ignore[arg-type]
        if side is not OrderSide.BUY:
            return
        if self._entry_guard is None:
            raise LiveEntryGuardError(
                "The Live entry cash-flow guard is not configured. No new entry was "
                "submitted; exits remain available."
            )
        reason = self._entry_guard(now)
        if reason:
            raise LiveEntryGuardError(reason)

    def execute(
        self,
        connection: sqlite3.Connection,
        *,
        symbol: str,
        side: OrderSide,
        requested_quantity: Decimal,
        now: datetime,
        strategy_run_id: int | None,
    ) -> PaperExecutionResult:
        # Raises LiveDisarmedError or AccountIdentityError before the inner
        # gateway - and therefore before the broker - is reached at all.
        self._require_mutation_allowed(connection, self._client, side, now)
        return self._inner.execute(
            connection,
            symbol=symbol,
            side=side,
            requested_quantity=requested_quantity,
            now=now,
            strategy_run_id=strategy_run_id,
        )


def _assert_protocol() -> EquityPaperExecutionGateway:  # pragma: no cover - typing only
    """Structural proof that the armed gateway still satisfies the seam."""
    return ArmedLiveGateway(trading_client=object())  # type: ignore[return-value]


__all__ = [
    "ENVIRONMENT_LIVE",
    "EQUITY_LIVE_LOCK_SCOPE",
    "EVENT_LIVE_STARTUP_REFUSED",
    "EVENT_LIVE_STARTUP_VERIFIED",
    "LIVE_RUNTIME_CONFIRMATION",
    "LIVE_CLIENT_ORDER_ID_PREFIX",
    "AccountIdentityError",
    "ArmedLiveGateway",
    "LiveDisarmedError",
    "LiveStartupError",
    "LiveEntryGuardError",
    "LiveStartupReport",
    "ReconciliationBlockedError",
    "verify_live_startup",
    "new_live_client_order_id",
]
