"""The account execution critical section: the one place an order may be decided.

Both products submit through their own boundary - fractional GTC crypto through
`execution.paper`, whole-share DAY equities through `execution.equity` - and
those boundaries stay separate, because the two order forms genuinely differ.
What they must not do separately is *decide against the account*, because there
is only one account.

This module is the guard both boundaries enter before they read the account and
leave after they have recorded the broker's reply. In order:

1. **take the shared account execution lock**, so the other runtime cannot be
   reading the same free exposure at the same moment;
2. **verify the durable account safety state**, so an ambiguous order raised by
   either product stops both;
3. **charge the API budget** for the calls this section is about to make, and
   fail closed rather than defer if they do not fit.

Then the caller does its work - account read, position read, global risk
context, risk decision, durable intent, duplicate preflight, one submission,
broker snapshot - and the lock is released on the way out, including when the
body raised.

**Order matters and is not arbitrary.** The lock comes first so the safety
answer cannot go stale between reading it and acting on it: without that, one
runtime could read `SAFE`, the other could halt the account, and the first could
then submit against a halt that already existed. Reading it *inside* the lock
means the halt either landed before this section started or waits until after it
finishes.

**Observation is never gated.** A dry run takes none of the three: it reads the
broker and submits nothing, so it cannot change the account, and refusing an
operator a read *because* the account is halted would remove the diagnostics
exactly when they are needed most. This matches what the runtimes already do -
they keep fetching bars, running the strategy and recording signals while
unsafe, and only stop trading.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from autotrader import state
from autotrader.account import budget as api_budget
from autotrader.account import safety as account_safety
from autotrader.account.lock import (
    DEFAULT_ACCOUNT_LOCK_TIMEOUT_SECONDS,
    AccountExecutionLock,
    account_lock_path_for,
    database_path_of,
)


def account_execution_lock_for(
    connection: sqlite3.Connection,
    *,
    timeout_seconds: float = DEFAULT_ACCOUNT_LOCK_TIMEOUT_SECONDS,
) -> AccountExecutionLock | None:
    """The shared lock guarding the account whose state `connection` holds.

    None for an in-memory database, which no second process can open and which
    therefore has nothing to contend with. Every path that reaches a real broker
    is backed by a real file.
    """
    path = database_path_of(connection)
    if path is None:
        return None
    return AccountExecutionLock(account_lock_path_for(path), timeout_seconds=timeout_seconds)


@contextmanager
def account_execution_section(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    trading_calls: int,
    market_data_calls: int = 0,
    engaged: bool = True,
    lock: AccountExecutionLock | None = None,
    timeout_seconds: float = DEFAULT_ACCOUNT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[state.AccountSafetyState | None]:
    """Hold the account-wide guards around one order decision.

    `engaged` is False for a dry run: nothing is locked, no halt is enforced and
    no budget is charged, because nothing will be submitted.

    `lock` lets a runtime reuse one lock object across its lifetime and lets a
    test inject one with a short timeout and a fake clock. Omitted, the lock is
    derived from the connection's own database file.

    `trading_calls` and `market_data_calls` are the number of provider calls the
    guarded section will make. They are declared by the caller rather than
    counted automatically, so the accounting is something a reader can check
    against the code rather than a number that silently drifted from it.

    Yields the verified `AccountSafetyState` (None when not engaged), so a
    caller that wants to record *why* it was allowed to proceed can.

    Raises before yielding - so the body never runs, and nothing is submitted -
    on any of: the lock not being obtainable in time
    (`AccountExecutionLockError`), the account being halted
    (`AccountUnsafeError`), or the budget being exhausted
    (`ApiBudgetExceededError`).
    """
    if not engaged:
        yield None
        return

    resolved = (
        lock
        if lock is not None
        else account_execution_lock_for(connection, timeout_seconds=timeout_seconds)
    )
    if resolved is None:
        # An in-memory database: no second process can see it, so there is
        # nothing to serialize against. The halt and the budget still apply -
        # they are about this account's truth and this system's traffic, not
        # about contention.
        yield _verify_and_charge(
            connection, now=now, trading_calls=trading_calls, market_data_calls=market_data_calls
        )
        return

    with resolved:
        yield _verify_and_charge(
            connection, now=now, trading_calls=trading_calls, market_data_calls=market_data_calls
        )


def _verify_and_charge(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    trading_calls: int,
    market_data_calls: int,
) -> state.AccountSafetyState:
    """Check the halt, then the budget. Raises rather than returning a flag.

    Safety before budget: a halted account must be refused for the reason that
    actually matters, and charging a budget for calls that are about to be
    refused anyway would make the meter describe traffic that never happened.
    """
    safety = account_safety.require_account_safe(connection)
    if trading_calls > 0:
        api_budget.require_api_budget(
            connection,
            budget=state.API_BUDGET_TRADING,
            calls=trading_calls,
            now=now,
        )
    if market_data_calls > 0:
        api_budget.require_api_budget(
            connection,
            budget=state.API_BUDGET_MARKET_DATA,
            calls=market_data_calls,
            now=now,
        )
    return safety


__all__ = [
    "account_execution_lock_for",
    "account_execution_section",
]
