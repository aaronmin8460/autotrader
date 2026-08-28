"""The shared API budget: two local processes, one set of provider credentials.

The crypto and equity runtimes authenticate as the same account against the
same provider. Neither can see the other's traffic, so without coordination
each one behaves as though the whole request allowance belongs to it. That is
fine right up to the moment something goes wrong in both at once.

**What this is for, precisely.** Not throughput. A fifteen-minute system makes
a few dozen calls per cycle and nothing here is trying to make it faster. It is
a backstop against the three ways a low-frequency system still manages to
hammer a provider: a retry storm, two services bursting in parallel on the same
boundary, and a loop that starts making calls it was never designed to make.

**Two budgets, because there are two services.** This was read off the code
rather than assumed: the execution boundary builds one client against the paper
*trading* host and a separate one against the *market data* host, and the calls
this system makes divide cleanly between them - account, positions, asset,
clock, calendar, duplicate preflight and submission on the first; bars and
latest trades on the second. Different hosts, different subscriptions,
different allowances. Metering them into one counter would make a bar fetch
spend a trading allowance, which is a limit this system would have invented
rather than observed.

**The ceilings are ours, not the provider's.** `DEFAULT_TRADING_LIMIT` and
`DEFAULT_MARKET_DATA_LIMIT` are deliberately *not* transcriptions of a
published provider rate limit. This module does not know the provider's limit
and does not pretend to: it enforces a conservative local ceiling derived from
this system's own worst realistic cycle - roughly seventy trading calls a
minute when both runtimes execute on the same boundary across all twelve
symbols - with better than two times headroom on top. It is a runaway detector
sized to sit above normal operation and below anything that could get an
account throttled.

**Refusal, never deferral.** A spend that does not fit is refused and the
caller fails that action closed. Nothing here sleeps, queues, or grants the
call later. A strategy signal belongs to the completed bar that produced it,
and submitting it minutes afterwards because a token freed up would be sending
a stale decision - which is worse than missing the trade.

**Fail-safe.** If the budget cannot be read or written at all, the caller does
not get a quiet "probably fine": `require_api_budget` raises, and the action
that needed it does not happen.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from autotrader import state

#: The metering window. One minute is short enough that a burst is visible
#: while it is still happening, and long enough that a fifteen-minute system
#: spends most of its life in an empty window.
WINDOW_SECONDS = 60

#: This system's own ceiling on trading-host calls per window, across **both**
#: runtimes. See the module docstring: a runaway detector, not a transcription
#: of a provider limit.
DEFAULT_TRADING_LIMIT = 180

#: The same, for market-data-host calls. Market data is fetched in batched
#: requests - one call covers all ten equities - so this sits well above any
#: cycle's real usage.
DEFAULT_MARKET_DATA_LIMIT = 180

#: Declared costs for the sections that spend. Each names how many calls the
#: section it guards actually makes, so the accounting is something a reader
#: can check against the code rather than a number that drifted.
#:
#: The execution critical section reads the account, reads every position,
#: reads the asset, preflights the duplicate check and submits: five trading
#: calls. The equity path also reads the broker clock, making six.
CRYPTO_EXECUTION_TRADING_CALLS = 5
EQUITY_EXECUTION_TRADING_CALLS = 6

#: One reference-price read on the market-data host, per execution.
EXECUTION_MARKET_DATA_CALLS = 1


class ApiBudgetError(Exception):
    """Base class for shared API budget failures."""


class ApiBudgetExceededError(ApiBudgetError):
    """This action's calls do not fit in the current window, so it does not happen.

    Deliberately **not** retried and deliberately not deferred. The caller
    abandons the action; the next boundary gets a fresh window and a fresh
    decision based on a fresh bar.
    """

    def __init__(self, grant: BudgetGrant, message: str) -> None:
        super().__init__(message)
        self.grant = grant


@dataclass(frozen=True)
class BudgetGrant:
    """The answer to one spend request, and the window it was measured against."""

    budget: str
    granted: bool
    window_start: datetime
    limit: int
    spent: int
    requested: int

    @property
    def remaining(self) -> int:
        """Calls still available in this window. Never negative."""
        return max(0, self.limit - self.spent)


def window_start_for(moment: datetime, *, window_seconds: int = WINDOW_SECONDS) -> datetime:
    """The opening instant of the window `moment` falls in.

    Floored against the UTC epoch rather than against any process's start time,
    so two processes bucketing the same instant necessarily agree on which
    window it is - which is the whole point of a *shared* budget.
    """
    if window_seconds < 1:
        raise ValueError(f"window_seconds must be at least 1, got {window_seconds}.")
    aware = moment.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = int((aware - epoch).total_seconds())
    return epoch + timedelta(seconds=elapsed - (elapsed % window_seconds))


def limit_for(budget: str) -> int:
    """This system's ceiling for one budget."""
    if budget == state.API_BUDGET_TRADING:
        return DEFAULT_TRADING_LIMIT
    if budget == state.API_BUDGET_MARKET_DATA:
        return DEFAULT_MARKET_DATA_LIMIT
    raise ApiBudgetError(f"Unknown API budget {budget!r}.")


def try_consume(
    connection: sqlite3.Connection,
    *,
    budget: str,
    calls: int,
    now: datetime,
    limit: int | None = None,
    window_seconds: int = WINDOW_SECONDS,
) -> BudgetGrant:
    """Spend `calls` from a budget if they fit in the current window.

    Returns a `BudgetGrant`. When `granted` is False **nothing was counted**:
    a refused action makes no calls, so charging it would make the budget
    describe traffic that never happened.

    The check and the increment happen in one immediate transaction inside
    `autotrader.state`, so two processes racing on the last token of a window
    cannot both be granted it.
    """
    ceiling = limit_for(budget) if limit is None else limit
    window = window_start_for(now, window_seconds=window_seconds)
    granted, stored = state.consume_api_budget(
        connection,
        budget=budget,
        window_start=window,
        calls=calls,
        limit=ceiling,
        updated_at=now,
    )
    return BudgetGrant(
        budget=budget,
        granted=granted,
        window_start=window,
        limit=ceiling,
        spent=0 if stored is None else stored.call_count,
        requested=calls,
    )


def require_api_budget(
    connection: sqlite3.Connection,
    *,
    budget: str,
    calls: int,
    now: datetime,
    limit: int | None = None,
    window_seconds: int = WINDOW_SECONDS,
) -> BudgetGrant:
    """Spend from a budget, or raise `ApiBudgetExceededError` and do nothing.

    The form every guarded action uses. A raise rather than a flag, for the
    same reason the account safety guard raises: a caller that forgot to check
    a returned boolean would carry on and make the calls anyway.
    """
    grant = try_consume(
        connection,
        budget=budget,
        calls=calls,
        now=now,
        limit=limit,
        window_seconds=window_seconds,
    )
    if grant.granted:
        return grant
    raise ApiBudgetExceededError(
        grant,
        f"The shared {budget} API budget is exhausted for the window opening at "
        f"{grant.window_start.isoformat()}: {grant.spent}/{grant.limit} call(s) already "
        f"spent across both runtimes and this action needs {calls}. The action is "
        "abandoned rather than delayed - a decision belongs to the bar that produced "
        "it. Nothing was submitted.",
    )


def current_usage(
    connection: sqlite3.Connection,
    *,
    budget: str,
    now: datetime,
    window_seconds: int = WINDOW_SECONDS,
) -> BudgetGrant:
    """Read one budget's current window without spending anything.

    Instrumentation, for a status line or the dashboard. `granted` is False
    because nothing was requested; read `spent`, `limit` and `remaining`.
    """
    window = window_start_for(now, window_seconds=window_seconds)
    stored = state.get_api_budget_window(connection, budget=budget, window_start=window)
    return BudgetGrant(
        budget=budget,
        granted=False,
        window_start=window,
        limit=limit_for(budget),
        spent=0 if stored is None else stored.call_count,
        requested=0,
    )


__all__ = [
    "CRYPTO_EXECUTION_TRADING_CALLS",
    "DEFAULT_MARKET_DATA_LIMIT",
    "DEFAULT_TRADING_LIMIT",
    "EQUITY_EXECUTION_TRADING_CALLS",
    "EXECUTION_MARKET_DATA_CALLS",
    "WINDOW_SECONDS",
    "ApiBudgetError",
    "ApiBudgetExceededError",
    "BudgetGrant",
    "current_usage",
    "limit_for",
    "require_api_budget",
    "try_consume",
    "window_start_for",
]
