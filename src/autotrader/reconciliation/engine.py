"""C8: crash recovery. Make local SQLite state reflect verified broker truth.

**The authority hierarchy is the whole design.** The broker is the truth about
orders, fills, and positions. The local database is durable *intent*, an audit
trail, and a last-known snapshot. When they disagree, the broker wins and the
snapshot is rewritten - never the other way around.

**Reconciliation never creates an order.** This module reads broker state,
looks up orders that already exist, and rewrites local rows. It does not
resubmit an `UNKNOWN` intent, does not mint a replacement `client_order_id`,
does not retry a submission, and does not place an offsetting trade to correct
a position mismatch. It imports nothing that could place one - no submission
function, no order-request type, no client factory that builds a request - and
a source-level test names each of those identifiers and asserts every one of
them is absent from the executable code here, docstrings and comments stripped.
Recovery that places a trade is not recovery.

The one Alpaca type this module names is the trading client itself, and only as
the type of a parameter it is handed. It never constructs one: the sole client
factory in the repository lives in `execution.paper`, hardcodes paper, and is
called here only when a caller passes no client of its own.

**`safe_to_trade` is the point.** A future 24/7 runtime must be able to ask one
question before it does anything, and get an answer that fails closed. Only
`CLEAN` and `REPAIRED` answer yes. An ambiguous lookup, an unreadable response,
a broker that cannot be reached, a client that cannot be proven to be paper -
each answers no, and each says why.

**The `UNKNOWN` case is the one this phase exists for.** An intent marked
`UNKNOWN` means a submission attempt ended without a knowable outcome: the
order may or may not exist. The recovery anchor is the `client_order_id` that
was committed *before* the request went out and never regenerated. This module
asks the broker about that exact key:

- the broker has it -> record what the broker says, and submit nothing;
- the broker definitively does not, confirmed by more than one read -> mark the
  intent `CONFIRMED_NOT_SUBMITTED`, terminal, so a stale decision from before
  the crash is never executed later;
- the lookup is ambiguous or fails -> leave it alone and block trading.

A single not-found is never enough to conclude anything, because a lookup that
raced a submission would answer "no" about an order that exists.

**Accepted is still not filled.** A snapshot is copied from the broker, fills
included; nothing is inferred. A partial fill stays partial, a missing fill
price stays missing, and a position is only ever written from a position the
broker actually reports.

**Repairs commit as they are made; the audit record commits at the end.** The
alternative - one transaction around the whole pass - would hold a write lock
across network I/O. A crash mid-pass therefore leaves durable repairs and no
run row, which reads correctly: work was done, nothing was concluded, and the
next pass reconciles from broker truth again. Reconciliation is idempotent, so
running it twice costs a second look and changes nothing.

**One precondition.** This is a startup and after-the-fact operation. It must
not run while a submission is in flight in another process: an intent created
seconds ago, whose order has not yet reached the broker, would be confirmed
absent. Nothing in this repository runs concurrently, and the bounded re-check
below gives an in-flight request time to land.

**The universe a pass reconciles is a parameter; the account is not.** Order
intents are always reconciled in full, because one Alpaca account has one
`client_order_id` namespace and an ambiguous order is ambiguous no matter which
product created it. *Positions* are reconciled per universe, because a pass
started by the crypto runner has no local snapshot to repair for an equity it
does not manage - and it says so, recording that holding as observed rather
than silently ignoring it. `symbols` therefore defaults to the crypto pairs,
which is exactly what every existing caller already got, and the equity runtime
passes its own ten.

Scope: Alpaca paper. There is no multi-broker abstraction and no live path -
see docs/SPEC.md section 8, "C8".
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from alpaca.trading.client import TradingClient

from autotrader.execution.models import (
    SUPPORTED_SYMBOLS,
    ExecutionError,
    format_quantity,
)
from autotrader.execution.paper import (
    BrokerOrderSnapshot,
    PaperPosition,
    broker_symbol_key,
    create_paper_trading_client,
    fetch_paper_account_state,
    fetch_paper_positions,
    find_broker_order_by_client_id,
    verify_paper_environment,
)
from autotrader.reconciliation.models import (
    CATEGORY_ORDER,
    CATEGORY_POSITION,
    CATEGORY_RUN,
    ItemOutcome,
    ReconciliationIssue,
    ReconciliationResult,
    ReconciliationStatus,
)
from autotrader.state import sqlite as state

#: The intent statuses whose truth still has to come from the broker.
#:
#: `CREATED` and `SUBMITTING` are both crash residue - the first from a process
#: that died before submitting, the second from one that died mid-call - and
#: neither may be assumed either way. `UNKNOWN` is the ambiguous outcome C7
#: recorded rather than retried. `SUBMITTED` has a broker order whose fills,
#: cancellation, or rejection are only knowable by asking.
RECONCILABLE_INTENT_STATUSES: tuple[str, ...] = (
    state.INTENT_STATUS_CREATED,
    state.INTENT_STATUS_SUBMITTING,
    state.INTENT_STATUS_SUBMITTED,
    state.INTENT_STATUS_UNKNOWN,
)

#: Broker order statuses that cannot change again, in Alpaca's own vocabulary.
#:
#: Deliberately conservative. `partially_filled` is **not** here - it can still
#: fill - and neither is anything pending. An order whose stored snapshot is
#: already terminal and consistent is settled, so re-reading it every startup
#: would spend a broker call to learn nothing. Anything not listed is re-read.
TERMINAL_BROKER_STATUSES: frozenset[str] = frozenset(
    {"filled", "canceled", "cancelled", "expired", "rejected", "replaced"}
)

#: How many *consecutive* definitive not-found answers are needed before an
#: intent is called `CONFIRMED_NOT_SUBMITTED`.
#:
#: More than one, because a single 404 could be a lookup that raced a
#: submission. Small and fixed, because this is a bounded confirmation, not a
#: poll: there is no growing backoff and no "until it settles" loop.
NOT_FOUND_CONFIRMATIONS = 2

#: The pause between those reads. Long enough for an in-flight submission to
#: land, short enough that startup is not held up.
NOT_FOUND_RECHECK_DELAY_SECONDS = 2.0

#: The `system_events.event_type` one finished pass writes, so the single
#: operational event stream mentions reconciliation too.
EVENT_RECONCILED = "RECONCILIATION_COMPLETED"

_ZERO = Decimal(0)


class ReconciliationError(Exception):
    """Base class for controlled reconciliation failures.

    Rare by design: almost everything that goes wrong here is *reported* as a
    `FAILED` or `UNRESOLVED` result rather than raised, because "the broker
    could not be reached" is an answer the caller needs, not an exception it
    needs to handle.
    """


class ReconciliationInputError(ReconciliationError):
    """A caller-supplied value is not something a pass can run with."""


# --------------------------------------------------------------------------
# Comparison helpers
#
# What "local already agrees with the broker" means, field by field. Every
# comparison is on exact values: `Decimal` for quantities, so 0.0004 is not
# 0.001, and a tolerance is never applied to a quantity.
# --------------------------------------------------------------------------


def _snapshot_matches(stored: state.StoredBrokerOrder, broker: BrokerOrderSnapshot) -> bool:
    """Whether a stored snapshot already says exactly what the broker says."""
    return (
        stored.broker_order_id == broker.broker_order_id
        and stored.client_order_id == broker.client_order_id
        and stored.status == broker.status
        and stored.quantity == broker.quantity
        and stored.filled_quantity == broker.filled_quantity
        and stored.filled_average_price == broker.filled_average_price
        and stored.submitted_at == broker.submitted_at
        and stored.filled_at == broker.filled_at
    )


def _describe_order(broker: BrokerOrderSnapshot) -> str:
    """A short, secret-free description of a broker order, for the audit trail."""
    price = "none" if broker.filled_average_price is None else f"{broker.filled_average_price:.10g}"
    return (
        f"broker order {broker.broker_order_id} {broker.side} {broker.symbol} "
        f"status={broker.status} qty={format_quantity(broker.quantity)} "
        f"filled={format_quantity(broker.filled_quantity)} avg_price={price}"
    )


def _position_matches(stored: state.Position | None, broker: PaperPosition | None) -> bool:
    """Whether the local position snapshot already equals broker truth.

    Quantity is the fact that matters and is compared exactly. A missing local
    row and an absent broker position agree: "no snapshot" and "flat" are not
    the same claim, but there is nothing to repair between them, and writing a
    zero row to say so would be a repair that repaired nothing.
    """
    if broker is None:
        return stored is None or stored.quantity == _ZERO
    if stored is None:
        return False
    return stored.quantity == broker.quantity and stored.average_price == (
        broker.average_entry_price
    )


# --------------------------------------------------------------------------
# Broker lookups
# --------------------------------------------------------------------------


class _LookupOutcome(Enum):
    """The three answers a broker order lookup can give. Never two of them.

    `NOT_FOUND` means the broker *said* no such order exists. `AMBIGUOUS` means
    the question could not be answered - a timeout, a 5xx, an unreadable
    response. Keeping them apart is the difference between closing an intent off
    safely and closing it off wrongly.
    """

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"


def _look_up_order(
    client: TradingClient, client_order_id: str
) -> tuple[_LookupOutcome, BrokerOrderSnapshot | None, str]:
    """Ask the broker about one `client_order_id`. Read-only, always.

    Returns `(outcome, snapshot, detail)`. `find_broker_order_by_client_id`
    already draws the line this depends on: it returns None **only** for a
    broker that clearly said no such order exists, and raises for everything
    else. A failed check is never a clean one.
    """
    try:
        snapshot = find_broker_order_by_client_id(client, client_order_id)
    except ExecutionError as error:
        return _LookupOutcome.AMBIGUOUS, None, str(error)
    except Exception as error:  # noqa: BLE001 - any unexpected failure is ambiguous
        return _LookupOutcome.AMBIGUOUS, None, f"{type(error).__name__} during order lookup"
    if snapshot is None:
        return _LookupOutcome.NOT_FOUND, None, "the broker reports no order under this key"
    return _LookupOutcome.FOUND, snapshot, _describe_order(snapshot)


def _confirm_absent(
    client: TradingClient,
    client_order_id: str,
    *,
    confirmations: int,
    delay_seconds: float,
    sleep: Callable[[float], None],
) -> tuple[_LookupOutcome, BrokerOrderSnapshot | None, str]:
    """Re-read a not-found answer a bounded number of times before believing it.

    One 404 is not evidence that an order was never sent - it could be a lookup
    that overtook a submission still in flight. So the read is repeated a fixed
    small number of times with a short pause between, and **every** read must
    agree. A single `FOUND` wins outright, and a single ambiguous answer makes
    the whole thing ambiguous.

    Bounded on purpose: exactly `confirmations` reads, no growing backoff, no
    loop that waits for the answer it wants. If the broker is inconsistent
    across these reads, the honest conclusion is "unresolved", not "keep
    asking".
    """
    for _ in range(1, confirmations):
        if delay_seconds > 0:
            sleep(delay_seconds)
        outcome, snapshot, detail = _look_up_order(client, client_order_id)
        if outcome is not _LookupOutcome.NOT_FOUND:
            return outcome, snapshot, detail
    return (
        _LookupOutcome.NOT_FOUND,
        None,
        f"{confirmations} consecutive broker reads found no order under this key",
    )


# --------------------------------------------------------------------------
# Order reconciliation
# --------------------------------------------------------------------------


def _repair_from_broker_order(
    connection: sqlite3.Connection,
    intent: state.StoredOrderIntent,
    broker: BrokerOrderSnapshot,
    *,
    now: datetime,
) -> ReconciliationIssue:
    """Copy broker truth for one order into local state, atomically.

    The intent moves to `SUBMITTED` because that is the fact being recorded: it
    reached the broker. What the *order* then did - filled, partially filled,
    canceled, rejected - is the broker's own status and lives in the snapshot,
    untranslated. Nothing here maps a broker status onto an intent status, and
    nothing here infers a position from a fill.
    """
    try:
        with state.transaction(connection):
            state.upsert_broker_order(
                connection,
                order_intent_id=intent.id,
                broker_order_id=broker.broker_order_id,
                client_order_id=broker.client_order_id,
                symbol=intent.symbol,
                side=broker.side,
                quantity=broker.quantity,
                filled_quantity=broker.filled_quantity,
                filled_average_price=broker.filled_average_price,
                status=broker.status,
                submitted_at=broker.submitted_at,
                filled_at=broker.filled_at,
                updated_at=now,
            )
            if intent.status != state.INTENT_STATUS_SUBMITTED:
                state.update_order_intent_status(
                    connection,
                    order_intent_id=intent.id,
                    status=state.INTENT_STATUS_SUBMITTED,
                    updated_at=now,
                )
    except state.StateError as error:
        return ReconciliationIssue(
            category=CATEGORY_ORDER,
            outcome=ItemOutcome.UNRESOLVED,
            detail=(
                f"local state contradicts the broker and was not overwritten: {error} "
                f"({_describe_order(broker)})"
            ),
            symbol=intent.symbol,
            client_order_id=intent.client_order_id,
        )
    return ReconciliationIssue(
        category=CATEGORY_ORDER,
        outcome=ItemOutcome.REPAIRED,
        detail=(
            f"local intent was {intent.status}; repaired from broker truth: "
            f"{_describe_order(broker)} submitted_at={_format_time(broker.submitted_at)} "
            f"filled_at={_format_time(broker.filled_at)} "
            f"broker_updated_at={_format_time(broker.broker_updated_at)}. "
            "No order was submitted."
        ),
        symbol=intent.symbol,
        client_order_id=intent.client_order_id,
    )


def _format_time(value: datetime | None) -> str:
    """A timestamp for the audit trail, or an explicit `none`."""
    return "none" if value is None else value.isoformat()


def _reconcile_one_order(
    connection: sqlite3.Connection,
    client: TradingClient,
    intent: state.StoredOrderIntent,
    stored: state.StoredBrokerOrder | None,
    *,
    now: datetime,
    dry_run: bool,
    confirmations: int,
    delay_seconds: float,
    sleep: Callable[[float], None],
) -> ReconciliationIssue:
    """Resolve one intent against the broker and return what happened.

    Never submits. The only three things it can conclude are: the broker has
    this order (record it), the broker definitively does not (mark the intent
    terminal so the stale decision is never sent), or the answer is not
    knowable (leave everything alone and block trading).
    """
    outcome, broker, detail = _look_up_order(client, intent.client_order_id)

    if outcome is _LookupOutcome.NOT_FOUND:
        outcome, broker, detail = _confirm_absent(
            client,
            intent.client_order_id,
            confirmations=confirmations,
            delay_seconds=delay_seconds,
            sleep=sleep,
        )

    if outcome is _LookupOutcome.AMBIGUOUS:
        return ReconciliationIssue(
            category=CATEGORY_ORDER,
            outcome=ItemOutcome.UNRESOLVED,
            detail=(
                f"intent is {intent.status} and the broker could not be asked about it "
                f"conclusively ({detail}). Nothing was changed and nothing was submitted."
            ),
            symbol=intent.symbol,
            client_order_id=intent.client_order_id,
        )

    if outcome is _LookupOutcome.NOT_FOUND:
        return _reconcile_absent_order(
            connection, intent, stored, now=now, dry_run=dry_run, detail=detail
        )

    if broker is None:  # pragma: no cover - FOUND always carries a snapshot
        return ReconciliationIssue(
            category=CATEGORY_ORDER,
            outcome=ItemOutcome.UNRESOLVED,
            detail="the broker lookup reported an order but returned nothing to read.",
            symbol=intent.symbol,
            client_order_id=intent.client_order_id,
        )
    return _reconcile_found_order(connection, intent, stored, broker, now=now, dry_run=dry_run)


def _reconcile_absent_order(
    connection: sqlite3.Connection,
    intent: state.StoredOrderIntent,
    stored: state.StoredBrokerOrder | None,
    *,
    now: datetime,
    dry_run: bool,
    detail: str,
) -> ReconciliationIssue:
    """Handle an intent the broker has confirmed it does not have.

    Two very different situations share this answer. If this process never
    recorded a broker order for the intent, the decision simply never reached
    the broker, and it is closed off as `CONFIRMED_NOT_SUBMITTED` - terminal, so
    a signal from before the crash cannot be executed on a later run. If a
    broker snapshot *is* stored, then local state says the broker once
    acknowledged an order the broker now denies; that contradiction is reported
    and the snapshot is left exactly as it is. Deleting recorded evidence
    because a later read disagreed with it would destroy the audit trail.
    """
    if stored is not None:
        return ReconciliationIssue(
            category=CATEGORY_ORDER,
            outcome=ItemOutcome.UNRESOLVED,
            detail=(
                f"local state holds broker order {stored.broker_order_id} for this key, "
                f"but {detail}. The stored snapshot was left untouched."
            ),
            symbol=intent.symbol,
            client_order_id=intent.client_order_id,
        )

    if dry_run:
        return ReconciliationIssue(
            category=CATEGORY_ORDER,
            outcome=ItemOutcome.REPAIRED,
            detail=(
                f"intent is {intent.status} and {detail}; it would be marked "
                f"{state.INTENT_STATUS_CONFIRMED_NOT_SUBMITTED} and never submitted."
            ),
            symbol=intent.symbol,
            client_order_id=intent.client_order_id,
        )

    try:
        state.update_order_intent_status(
            connection,
            order_intent_id=intent.id,
            status=state.INTENT_STATUS_CONFIRMED_NOT_SUBMITTED,
            updated_at=now,
        )
    except state.StateError as error:
        return ReconciliationIssue(
            category=CATEGORY_ORDER,
            outcome=ItemOutcome.UNRESOLVED,
            detail=f"could not close off an unsubmitted intent: {error}",
            symbol=intent.symbol,
            client_order_id=intent.client_order_id,
        )
    return ReconciliationIssue(
        category=CATEGORY_ORDER,
        outcome=ItemOutcome.REPAIRED,
        detail=(
            f"intent was {intent.status} and {detail}; marked "
            f"{state.INTENT_STATUS_CONFIRMED_NOT_SUBMITTED}. The stale decision was not "
            "submitted and will not be."
        ),
        symbol=intent.symbol,
        client_order_id=intent.client_order_id,
    )


def _reconcile_found_order(
    connection: sqlite3.Connection,
    intent: state.StoredOrderIntent,
    stored: state.StoredBrokerOrder | None,
    broker: BrokerOrderSnapshot,
    *,
    now: datetime,
    dry_run: bool,
) -> ReconciliationIssue:
    """Handle an order the broker has, checking first that it is *this* order.

    An order returned under this key that names a different key, a different
    market, or a different side is not evidence about this intent. Copying it
    in would corrupt local state with a real order belonging somewhere else, so
    it is reported instead and nothing is written.
    """
    mismatch = _identity_mismatch(intent, broker)
    if mismatch is not None:
        return ReconciliationIssue(
            category=CATEGORY_ORDER,
            outcome=ItemOutcome.UNRESOLVED,
            detail=(
                f"the broker returned an order that does not match this intent "
                f"({mismatch}); local state was left untouched and nothing was submitted."
            ),
            symbol=intent.symbol,
            client_order_id=intent.client_order_id,
        )

    if (
        stored is not None
        and _snapshot_matches(stored, broker)
        and intent.status == state.INTENT_STATUS_SUBMITTED
    ):
        return ReconciliationIssue(
            category=CATEGORY_ORDER,
            outcome=ItemOutcome.CLEAN,
            detail=f"local state already matches the broker: {_describe_order(broker)}",
            symbol=intent.symbol,
            client_order_id=intent.client_order_id,
        )

    if dry_run:
        return ReconciliationIssue(
            category=CATEGORY_ORDER,
            outcome=ItemOutcome.REPAIRED,
            detail=(
                f"local intent is {intent.status}; it would be repaired from "
                f"{_describe_order(broker)}"
            ),
            symbol=intent.symbol,
            client_order_id=intent.client_order_id,
        )

    return _repair_from_broker_order(connection, intent, broker, now=now)


def _identity_mismatch(intent: state.StoredOrderIntent, broker: BrokerOrderSnapshot) -> str | None:
    """Describe how a broker order fails to be the one this intent describes."""
    if broker.client_order_id != intent.client_order_id:
        return f"it carries client_order_id {broker.client_order_id}, not {intent.client_order_id}"
    if broker_symbol_key(broker.symbol) != broker_symbol_key(intent.symbol):
        return f"it is for {broker.symbol}, not {intent.symbol}"
    if broker.side.upper() != intent.side.upper():
        return f"it is a {broker.side}, not a {intent.side}"
    return None


def _is_settled(intent: state.StoredOrderIntent, stored: state.StoredBrokerOrder | None) -> bool:
    """Whether this intent is finished and need not be queried again.

    True only when the intent is `SUBMITTED` and its stored snapshot already
    reports a broker status that cannot change. Everything else - including a
    `partially_filled` order and any intent without a snapshot - is re-read.
    """
    if intent.status != state.INTENT_STATUS_SUBMITTED or stored is None:
        return False
    return stored.status.strip().lower() in TERMINAL_BROKER_STATUSES


# --------------------------------------------------------------------------
# Position reconciliation
# --------------------------------------------------------------------------


def _reconcile_one_position(
    connection: sqlite3.Connection,
    symbol: str,
    stored: state.Position | None,
    broker: PaperPosition | None,
    *,
    now: datetime,
    dry_run: bool,
) -> ReconciliationIssue:
    """Make the local snapshot for one pair equal what the broker reports.

    The broker is authoritative in both directions: a position this process
    never saw is written in, and a local position the broker no longer holds
    goes to zero. Nothing is derived from `order_intents`, nothing is inferred
    from a submission, and no offsetting order is placed to make the two agree -
    trading to fix a bookkeeping difference would be the opposite of recovery.

    `fetch_paper_positions` has already refused any short, so `broker` is a long
    position or nothing at all, and a negative quantity cannot reach the write.
    """
    if _position_matches(stored, broker):
        held = "flat" if broker is None else format_quantity(broker.quantity)
        return ReconciliationIssue(
            category=CATEGORY_POSITION,
            outcome=ItemOutcome.CLEAN,
            detail=f"local snapshot already matches the broker ({held})",
            symbol=symbol,
        )

    quantity = _ZERO if broker is None else broker.quantity
    average_price = None if broker is None else broker.average_entry_price
    was = "no local snapshot" if stored is None else format_quantity(stored.quantity)
    detail = (
        f"broker holds {format_quantity(quantity)}; local snapshot was {was}. "
        "Repaired from broker truth; no order was placed."
    )

    if dry_run:
        return ReconciliationIssue(
            category=CATEGORY_POSITION,
            outcome=ItemOutcome.REPAIRED,
            detail=(
                f"broker holds {format_quantity(quantity)}; local snapshot is {was}. "
                "It would be repaired from broker truth."
            ),
            symbol=symbol,
        )

    try:
        state.upsert_position(
            connection,
            symbol=symbol,
            quantity=quantity,
            average_price=average_price,
            updated_at=now,
        )
    except state.StateError as error:
        return ReconciliationIssue(
            category=CATEGORY_POSITION,
            outcome=ItemOutcome.UNRESOLVED,
            detail=f"could not write the broker's position for {symbol}: {error}",
            symbol=symbol,
        )
    return ReconciliationIssue(
        category=CATEGORY_POSITION, outcome=ItemOutcome.REPAIRED, detail=detail, symbol=symbol
    )


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


def _persist_audit(
    connection: sqlite3.Connection,
    result: ReconciliationResult,
) -> int:
    """Write the run and its evidence in one transaction, and return the run id.

    Written at the end rather than incrementally, so a run row always describes
    a pass that finished. `safe_to_trade` is stored as the result computed it,
    because the record has to say what the runtime was actually told.
    """
    with state.transaction(connection):
        run_id = state.record_reconciliation_run(
            connection,
            started_at=result.started_at,
            completed_at=result.completed_at,
            status=result.status.value,
            safe_to_trade=result.safe_to_trade,
            orders_checked=result.orders_checked,
            positions_checked=result.positions_checked,
            issues_count=result.issues_count,
            unresolved_count=result.unresolved_count,
        )
        for issue in result.issues:
            state.record_reconciliation_event(
                connection,
                reconciliation_run_id=run_id,
                event_timestamp=result.completed_at,
                category=issue.category,
                outcome=issue.outcome.value,
                symbol=issue.symbol,
                client_order_id=issue.client_order_id,
                detail=issue.detail,
            )
        state.record_system_event(
            connection,
            event_timestamp=result.completed_at,
            event_type=EVENT_RECONCILED,
            message=(
                f"Reconciliation {result.status.value}: safe_to_trade="
                f"{str(result.safe_to_trade).lower()}, orders_checked="
                f"{result.orders_checked}, positions_checked={result.positions_checked}, "
                f"repaired={result.repaired_count}, unresolved={result.unresolved_count}. "
                "No order was submitted."
            ),
        )
    return run_id


def _record(issues: list[ReconciliationIssue], issue: ReconciliationIssue) -> None:
    """Keep an item outcome only when there is something to say about it.

    A `CLEAN` item means local state already matched the broker, and writing a
    row per agreeing order on every startup would bury the rows that matter
    under rows that do not. What was verified is already reported by
    `orders_checked` and `positions_checked`; `issues` is what changed, what was
    noticed, and what could not be settled.
    """
    if issue.outcome is not ItemOutcome.CLEAN:
        issues.append(issue)


def _status_for(issues: tuple[ReconciliationIssue, ...]) -> ReconciliationStatus:
    """Reduce every item outcome to one run status, worst wins.

    `OBSERVED` never escalates: it is evidence that changed nothing and is not
    a reason to refuse to trade.
    """
    outcomes = {issue.outcome for issue in issues}
    if ItemOutcome.FAILED in outcomes:
        return ReconciliationStatus.FAILED
    if ItemOutcome.UNRESOLVED in outcomes:
        return ReconciliationStatus.UNRESOLVED
    if ItemOutcome.REPAIRED in outcomes:
        return ReconciliationStatus.REPAIRED
    return ReconciliationStatus.CLEAN


def _finish(
    connection: sqlite3.Connection,
    *,
    started_at: datetime,
    completed_at: datetime,
    issues: list[ReconciliationIssue],
    orders_checked: int,
    positions_checked: int,
    dry_run: bool,
) -> ReconciliationResult:
    """Assemble the result and, unless this is a dry run, record it.

    If the audit write itself fails there is no honest way to report success:
    the pass becomes `FAILED`, because a runtime that cannot be told what
    happened must not start trading on the strength of it.
    """
    result = ReconciliationResult(
        status=_status_for(tuple(issues)),
        started_at=started_at,
        completed_at=completed_at,
        orders_checked=orders_checked,
        positions_checked=positions_checked,
        issues=tuple(issues),
        dry_run=dry_run,
    )
    if dry_run:
        return result

    try:
        run_id = _persist_audit(connection, result)
    except state.StateError as error:
        issues.append(
            ReconciliationIssue(
                category=CATEGORY_RUN,
                outcome=ItemOutcome.FAILED,
                detail=f"the reconciliation audit record could not be written: {error}",
            )
        )
        return ReconciliationResult(
            status=ReconciliationStatus.FAILED,
            started_at=started_at,
            completed_at=completed_at,
            orders_checked=orders_checked,
            positions_checked=positions_checked,
            issues=tuple(issues),
            dry_run=dry_run,
        )
    return ReconciliationResult(
        status=result.status,
        started_at=result.started_at,
        completed_at=result.completed_at,
        orders_checked=result.orders_checked,
        positions_checked=result.positions_checked,
        issues=result.issues,
        dry_run=dry_run,
        reconciliation_run_id=run_id,
    )


# --------------------------------------------------------------------------
# The public pass
# --------------------------------------------------------------------------


def reconcile_paper_state(
    connection: sqlite3.Connection,
    *,
    trading_client: TradingClient | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    symbols: tuple[str, ...] = SUPPORTED_SYMBOLS,
    confirmations: int = NOT_FOUND_CONFIRMATIONS,
    recheck_delay_seconds: float = NOT_FOUND_RECHECK_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> ReconciliationResult:
    """Reconcile local state against Alpaca paper, and report whether trading is safe.

    The whole pass, in order:

    1. prove the trading client reaches Alpaca **paper**, or stop;
    2. read the account, which is also the authentication check;
    3. read every open paper position, which refuses any short;
    4. for each intent whose truth still depends on the broker, ask about its
       `client_order_id` and record, close off, or flag what comes back;
    5. for each pair in the active universe, make the local snapshot equal the
       broker's position;
    6. write the run and its evidence.

    Steps 1 to 3 fail the whole pass, because none of them leaves anything
    partially knowable: without them there is no verified truth to repair from,
    and reporting a green result on unread state would be the one dishonest
    thing this module could do. A failure inside step 4 or 5 is local to that
    item, which stays unresolved while the rest of the pass completes - a
    runtime is better served by "these three things are wrong" than by the
    first one.

    **No order is ever submitted, in any branch.** Not to resolve an `UNKNOWN`,
    not to replace a stale intent, not to correct a position difference.

    `dry_run` reports exactly the same findings and writes **nothing** - no
    repair, no run row, no event, no system event. It is the audit mode: run it
    to see what a real pass would change.

    `symbols` is the position universe this pass owns. It defaults to the
    crypto pairs, so an existing caller's behaviour is unchanged. Step 4 -
    order intents - is deliberately **not** filtered by it: a `client_order_id`
    whose outcome is unknown blocks trading for the whole account, which is the
    only correct answer when the account is shared. A position held outside
    `symbols` is recorded as observed and never traded out of.

    `confirmations`, `recheck_delay_seconds`, and `sleep` control the bounded
    re-read that a not-found answer has to survive. They are parameters so a
    test can run the same logic without waiting; the defaults are what an
    operator gets.

    Returns a `ReconciliationResult`. Ask it `safe_to_trade`.
    """
    started_at = now if now is not None else datetime.now(UTC)
    if confirmations < 1:
        raise ReconciliationInputError(
            f"confirmations must be at least 1, got {confirmations}. A not-found answer "
            "has to be read at least once to mean anything."
        )
    if recheck_delay_seconds < 0:
        raise ReconciliationInputError(
            f"recheck_delay_seconds must not be negative, got {recheck_delay_seconds}."
        )
    universe = tuple(symbols)
    if not universe:
        raise ReconciliationInputError(
            "symbols must name at least one instrument. A pass that reconciles no "
            "position could only ever report that nothing was checked, which is not "
            "the same answer as everything matching."
        )

    issues: list[ReconciliationIssue] = []

    def fail(detail: str) -> ReconciliationResult:
        issues.append(
            ReconciliationIssue(category=CATEGORY_RUN, outcome=ItemOutcome.FAILED, detail=detail)
        )
        return _finish(
            connection,
            started_at=started_at,
            completed_at=_moment(now, started_at),
            issues=issues,
            orders_checked=0,
            positions_checked=0,
            dry_run=dry_run,
        )

    try:
        client = trading_client if trading_client is not None else create_paper_trading_client()
    except ExecutionError as error:
        return fail(f"no paper trading client could be built: {error}")

    try:
        verify_paper_environment(client)
    except ExecutionError as error:
        return fail(str(error))

    try:
        account = fetch_paper_account_state(client)
    except ExecutionError as error:
        return fail(f"the paper account could not be read: {error}")
    except Exception as error:  # noqa: BLE001 - an unreadable account must fail closed
        return fail(f"the paper account could not be read: {type(error).__name__}")

    if not account.tradable:
        # Recorded, not blocking: reconciliation is about whether local state
        # matches the broker, and a blocked account does not make it disagree.
        # Execution checks tradability again, immediately before submitting.
        issues.append(
            ReconciliationIssue(
                category=CATEGORY_RUN,
                outcome=ItemOutcome.OBSERVED,
                detail=(
                    f"the paper account reports status {account.status} and is not "
                    "currently able to place orders."
                ),
            )
        )

    try:
        broker_positions = fetch_paper_positions(client)
    except ExecutionError as error:
        return fail(f"paper positions could not be read: {error}")
    except Exception as error:  # noqa: BLE001 - unreadable positions must fail closed
        return fail(f"paper positions could not be read: {type(error).__name__}")

    orders_checked = 0
    try:
        intents = state.list_order_intents(connection)
    except state.StateError as error:
        return fail(f"local order intents could not be read: {error}")

    for intent in intents:
        if intent.status not in RECONCILABLE_INTENT_STATUSES:
            continue
        try:
            stored = state.get_broker_order_by_intent(connection, intent.id)
        except state.StateError as error:
            # An unreadable local snapshot is not a reason to go ask the broker
            # and overwrite it: this row cannot be compared, so it stays
            # unresolved and trading stays blocked.
            issues.append(
                ReconciliationIssue(
                    category=CATEGORY_ORDER,
                    outcome=ItemOutcome.UNRESOLVED,
                    detail=f"the stored broker snapshot could not be read: {error}",
                    symbol=intent.symbol,
                    client_order_id=intent.client_order_id,
                )
            )
            continue
        if _is_settled(intent, stored):
            continue
        orders_checked += 1
        _record(
            issues,
            _reconcile_one_order(
                connection,
                client,
                intent,
                stored,
                now=_moment(now, started_at),
                dry_run=dry_run,
                confirmations=confirmations,
                delay_seconds=recheck_delay_seconds,
                sleep=sleep,
            ),
        )

    for symbol in universe:
        try:
            local_position = state.get_position(connection, symbol)
        except state.StateError as error:
            issues.append(
                ReconciliationIssue(
                    category=CATEGORY_POSITION,
                    outcome=ItemOutcome.UNRESOLVED,
                    detail=f"the stored position snapshot could not be read: {error}",
                    symbol=symbol,
                )
            )
            continue
        _record(
            issues,
            _reconcile_one_position(
                connection,
                symbol,
                local_position,
                broker_positions.get(broker_symbol_key(symbol)),
                now=_moment(now, started_at),
                dry_run=dry_run,
            ),
        )

    active_keys = {broker_symbol_key(symbol) for symbol in universe}
    for key, position in sorted(broker_positions.items()):
        if key in active_keys:
            continue
        # Real broker state this system does not manage. Worth writing down -
        # it is part of the account's exposure - but not a local mismatch, and
        # not something to trade out of.
        issues.append(
            ReconciliationIssue(
                category=CATEGORY_POSITION,
                outcome=ItemOutcome.OBSERVED,
                detail=(
                    f"the paper account holds {format_quantity(position.quantity)} of "
                    f"{position.symbol}, which is outside this system's universe. It was "
                    "not reconciled and no order was placed."
                ),
            )
        )

    return _finish(
        connection,
        started_at=started_at,
        completed_at=_moment(now, started_at),
        issues=issues,
        orders_checked=orders_checked,
        positions_checked=len(universe),
        dry_run=dry_run,
    )


def _moment(supplied: datetime | None, started_at: datetime) -> datetime:
    """The timestamp to stamp a repair with.

    A caller that pinned `now` gets exactly that value everywhere, which makes
    a reconciliation run reproducible in a test. Otherwise the wall clock is
    read afresh, so a long pass does not backdate its later repairs.
    """
    return started_at if supplied is not None else datetime.now(UTC)


__all__ = [
    "EVENT_RECONCILED",
    "NOT_FOUND_CONFIRMATIONS",
    "NOT_FOUND_RECHECK_DELAY_SECONDS",
    "RECONCILABLE_INTENT_STATUSES",
    "TERMINAL_BROKER_STATUSES",
    "ReconciliationError",
    "ReconciliationInputError",
    "reconcile_paper_state",
]
