"""The durable, account-wide answer to "may anything submit a new order?".

One Alpaca paper account carries both books. Crypto and equity run as two
separate processes, and the thing that makes them one system rather than two is
that a broker uncertainty raised by either of them is an uncertainty about the
*account* - not about the asset class that happened to hit it.

**The rule, in one line:**

    UNKNOWN FROM ANY ASSET = NO NEW ORDERS FROM ANY ASSET.

An `UNKNOWN` order is one that may or may not exist at the broker. While one is
outstanding, the account's true position and true exposure are both unknown, so
*every* number a risk decision would be measured against is unreliable - the
crypto runtime's included, even when the ambiguous order was for SPY. A halt
that only stopped the process that raised it would let the other process keep
sizing orders against an account it cannot describe.

**Why this is durable and not in-process.** Both runtimes already pause
themselves on an ambiguous submission, and that pause is correct and is kept.
It is also invisible to the other process and gone on restart, which are
exactly the two cases this module exists for: the equity process pauses, and
the crypto process wakes at the next fifteen-minute boundary knowing nothing
about it. The halt therefore lives in SQLite, where both processes and every
future restart can see it.

**Only reconciliation clears it, and only a full-universe pass.** Time passing
resolves nothing; a restart resolves nothing; a runtime deciding it feels fine
resolves nothing. The single path back to `SAFE` is a completed reconciliation
that covered every tracked symbol and found nothing unresolved - because that
is the only procedure in this system that actually establishes what the broker
holds. `restore_account_safety` refuses anything less and says why.

**No order is ever placed from here.** Recovery is reconciliation-driven, not
trading-driven: nothing in this module submits, cancels, or replaces anything.
It reads one row and writes one row.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING

from autotrader import state
from autotrader.execution.models import TRADABLE_SYMBOLS

if TYPE_CHECKING:  # pragma: no cover - import-time only
    # A type-checking import, deliberately. The execution boundary calls this
    # module the moment a submission turns out ambiguous, and `reconciliation`
    # imports that boundary - so importing it here for real would close a cycle
    # at interpreter start. Nothing below needs the class at runtime: it is read
    # through its attributes, and `from __future__ import annotations` keeps the
    # annotations as strings.
    from autotrader.reconciliation.models import ReconciliationResult

#: Who raised or cleared a halt. Free text in the database; these are the
#: values this system writes, so a status line can name the runtime responsible.
SOURCE_CRYPTO = "crypto-runtime"
SOURCE_EQUITY = "equity-runtime"
SOURCE_RECONCILIATION = "reconciliation"
SOURCE_OPERATOR = "operator"

#: The banner an operator must be able to find in a log when the shared halt is
#: what stopped a submission. Written once, here, so every caller says the same
#: words.
ACCOUNT_UNSAFE_BANNER = "ACCOUNT SAFETY HALT - NO NEW ORDERS FROM ANY ASSET CLASS"


class AccountSafetyError(Exception):
    """Base class for shared-account safety failures."""


class AccountUnsafeError(AccountSafetyError):
    """The account is halted, so this submission must not happen.

    Raised *before* a broker is contacted. It is not a broker condition and
    retrying it changes nothing: the halt is cleared by reconciliation, never
    by trying again.
    """

    def __init__(self, safety: state.AccountSafetyState, message: str) -> None:
        super().__init__(message)
        self.safety = safety


def read_account_safety(connection: sqlite3.Connection) -> state.AccountSafetyState:
    """The account's current durable safety answer. Never None, never optimistic.

    A database in which no reconciliation has ever established safety reports
    `UNSAFE_RECONCILIATION`, not `SAFE`. "Nobody has checked" and "we checked
    and it is fine" are different answers and only one of them opens a gate.
    """
    return state.read_account_safety_state(connection)


def require_account_safe(connection: sqlite3.Connection) -> state.AccountSafetyState:
    """Return the safety state, or raise `AccountUnsafeError` if it is not safe.

    The guard every risk-increasing submission passes through, on both sides of
    the system. It is deliberately a raise rather than a boolean: a caller that
    forgot to check a returned flag would submit, and this is the one check
    where forgetting must not be possible.
    """
    safety = read_account_safety(connection)
    if safety.safe_to_trade:
        return safety

    anchor = (
        ""
        if safety.client_order_id is None
        else f" The unresolved client_order_id is {safety.client_order_id}."
    )
    raise AccountUnsafeError(
        safety,
        f"{ACCOUNT_UNSAFE_BANNER}. The shared account safety state is "
        f"{safety.state} (set by {safety.source}): {safety.reason}{anchor} Nothing "
        "was submitted. This halt is cleared only by a full-universe "
        "reconciliation that resolves it, never by retrying and never by waiting.",
    )


def halt_account_for_unknown(
    connection: sqlite3.Connection,
    *,
    source: str,
    client_order_id: str,
    detail: str,
    now: datetime,
) -> state.AccountSafetyState:
    """Record that a submission's outcome is unknown, halting the whole account.

    Called from the one place an `UNKNOWN` is ever recorded, so a new caller
    cannot create an ambiguous order without also creating the halt.

    `client_order_id` is carried into the row because it is the recovery
    anchor: it names the exact key reconciliation must ask the broker about,
    and an operator reading the halt needs it without going digging.

    This **overwrites** a `UNSAFE_RECONCILIATION` state, which is a downgrade in
    certainty and therefore always correct to apply. It also overwrites an
    existing `UNSAFE_UNKNOWN`, so the most recent ambiguity is the one named;
    both remain unsafe either way, and reconciliation resolves every
    outstanding intent rather than only the one quoted here.
    """
    return state.set_account_safety_state(
        connection,
        account_state=state.ACCOUNT_SAFETY_UNSAFE_UNKNOWN,
        reason=detail,
        source=source,
        client_order_id=client_order_id,
        updated_at=now,
    )


def halt_account_for_reconciliation(
    connection: sqlite3.Connection,
    *,
    source: str,
    detail: str,
    now: datetime,
) -> state.AccountSafetyState:
    """Record that reconciliation has not established that the account is safe.

    Used when a pass fails, leaves something unresolved, or covers less than
    the full universe. Distinct from `UNSAFE_UNKNOWN` because no order of ours
    is known to be ambiguous - what is missing is the verification, not the
    order.

    **An existing `UNSAFE_UNKNOWN` is never overwritten by this.** A pass that
    failed does not resolve an ambiguous order, and downgrading the halt to the
    weaker reason would discard the `client_order_id` an operator needs. Both
    states stop new orders identically, so keeping the stronger one costs
    nothing and loses nothing.
    """
    current = read_account_safety(connection)
    if current.state == state.ACCOUNT_SAFETY_UNSAFE_UNKNOWN:
        return current
    return state.set_account_safety_state(
        connection,
        account_state=state.ACCOUNT_SAFETY_UNSAFE_RECONCILIATION,
        reason=detail,
        source=source,
        client_order_id=None,
        updated_at=now,
    )


def missing_universe_symbols(result: ReconciliationResult) -> tuple[str, ...]:
    """Tracked symbols a pass did not cover, in the frozen universe's order.

    Empty means the pass was account-wide. Anything else means it was narrower
    than the account it is being asked to vouch for.
    """
    covered = {symbol.upper() for symbol in result.symbols}
    return tuple(symbol for symbol in TRADABLE_SYMBOLS if symbol.upper() not in covered)


def apply_reconciliation_result(
    connection: sqlite3.Connection,
    result: ReconciliationResult,
    *,
    source: str = SOURCE_RECONCILIATION,
    now: datetime,
) -> state.AccountSafetyState:
    """The one place a finished reconciliation pass moves the shared halt.

    Three outcomes, and the distinction between the last two is what keeps a
    narrow pass from lying in either direction:

    **The pass is not safe** - `UNRESOLVED` or `FAILED` - so the account is
    halted, whatever universe the pass covered. Order intents are reconciled in
    full regardless of the position universe, so even a narrow pass can discover
    an ambiguous `client_order_id`, and a discovery like that is account-wide
    news. An existing `UNSAFE_UNKNOWN` is left in place rather than downgraded.

    **The pass is safe and covered every tracked symbol** - so the account is
    `SAFE`. This is the only transition that opens the gate, and it requires
    both halves: a clean answer, and a complete view.

    **The pass is safe but narrower than the account** - so nothing changes. A
    crypto-only pass has not established that the equity book is understood, so
    it may not clear a halt; but it also found nothing wrong, so inventing a
    halt from it would stop the system for a fact nobody observed. Vouching for
    less than it looked at, in either direction, is the failure mode here.

    A `dry_run` pass never moves the halt at all. It repairs nothing and records
    nothing, so it has established nothing - it is the audit mode, and an audit
    that silently changed the thing it was auditing would not be one.
    """
    if result.dry_run:
        return read_account_safety(connection)

    if not result.safe_to_trade:
        blocking = result.blocking_issues()
        first = blocking[0].detail if blocking else "no blocking detail was recorded"
        return halt_account_for_reconciliation(
            connection,
            source=source,
            detail=(
                f"A reconciliation pass over {len(result.symbols)} symbol(s) is "
                f"{result.status.value} with {result.unresolved_count} blocking "
                f"issue(s); first: {first}."
            ),
            now=now,
        )

    missing = missing_universe_symbols(result)
    if missing:
        # Safe, but narrower than the account. Reported, not acted on.
        return read_account_safety(connection)

    return state.set_account_safety_state(
        connection,
        account_state=state.ACCOUNT_SAFETY_SAFE,
        reason=(
            f"A full-universe reconciliation pass over all {len(TRADABLE_SYMBOLS)} "
            f"tracked symbols is {result.status.value}: {result.orders_checked} "
            f"order(s) verified against the broker, {result.repaired_count} repaired, "
            "nothing unresolved."
        ),
        source=source,
        client_order_id=None,
        updated_at=now,
    )


__all__ = [
    "ACCOUNT_UNSAFE_BANNER",
    "SOURCE_CRYPTO",
    "SOURCE_EQUITY",
    "SOURCE_OPERATOR",
    "SOURCE_RECONCILIATION",
    "AccountSafetyError",
    "AccountUnsafeError",
    "halt_account_for_reconciliation",
    "halt_account_for_unknown",
    "missing_universe_symbols",
    "apply_reconciliation_result",
    "read_account_safety",
    "require_account_safe",
]
