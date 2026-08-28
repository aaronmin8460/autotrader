"""Smoke-harness vocabulary. No broker SDK, no network, no database, no orders.

Standard library only, exactly like `execution.models` and
`reconciliation.models`, and for the same reason: the words an operator reads
off a preflight report must not require importing anything that can trade.

**Everything here is a verdict about state that already exists.** A
`CleanupPlan` is not an order and cannot become one - it is arithmetic over a
position the broker reported, rendered as text a human may choose to type. The
harness has no code path that submits it, and `tests/test_smoke_harness.py`
asserts that structurally rather than trusting this sentence.

`SmokeVerdict` is deliberately two-valued at the top level. An operator about
to place a real (paper) order needs one answer - go or do not go - and a third
"probably" would be read as a yes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Protocol

#: Printed by `preflight` when every gate passed and a smoke may begin.
READY_FOR_PAPER_SMOKE = "READY_FOR_PAPER_SMOKE"

#: Printed by `preflight` when at least one gate failed. Not a suggestion.
BLOCKED = "BLOCKED"

#: Printed by `inspect-order` when the broker could not be asked. It is *not*
#: an order status - it means this harness does not know the order's status,
#: which is the one situation where acting would duplicate an order.
ORDER_TRUTH_UNRESOLVED = "ORDER_TRUTH_UNRESOLVED"

#: Printed alongside `ORDER_TRUTH_UNRESOLVED`. The original submission may have
#: reached the matching engine; re-sending it is how one smoke becomes two.
DO_NOT_RETRY_BANNER = "DO NOT RETRY ORIGINAL ORDER"

#: Printed by `final-audit` when tracked exposure matches the baseline exactly.
EXPOSURE_RESTORED = "EXPOSURE_RESTORED"
EXPOSURE_NOT_RESTORED = "EXPOSURE_NOT_RESTORED"

#: Printed by `final-audit`.
SMOKE_COMPLETE = "SMOKE_COMPLETE"
SMOKE_INCOMPLETE = "SMOKE_INCOMPLETE"

#: The label every generated command carries. The harness prints commands; the
#: operator runs them. There is no third party in between.
USER_MUST_EXECUTE_BANNER = "USER MUST EXECUTE EXACTLY ONCE"


class SmokeError(Exception):
    """Base class for a controlled harness failure.

    Reported as a message rather than a traceback: these describe an
    operational situation - an unreadable database, a broker that will not
    answer - not a programming bug.
    """


class SmokeInputError(SmokeError):
    """A caller-supplied value is not something the harness can inspect."""


class StateUnreadableError(SmokeError):
    """The operational database could not be opened **read-only**.

    Raised rather than falling back to a writable connection. A harness that
    quietly opened the database read-write to work around a WAL sidecar file
    would be able to migrate it, and an audit that can modify what it audits
    is not an audit.
    """


class BrokerUnreadableError(SmokeError):
    """The broker could not be read, so its truth is unknown.

    Distinct from "the broker answered and the answer was empty". Every caller
    here must treat the two differently; conflating them is what turns an
    unknown order into a duplicate one.
    """


class SmokeVerdict(Enum):
    """The two answers a gate may give. There is deliberately no third.

    `PASS` means the check ran and found what it required. `FAIL` means the
    check ran and did not, **or** could not run at all - an unanswerable check
    fails closed, because "I could not tell" is not permission.
    """

    PASS = "PASS"
    FAIL = "FAIL"


class Freshness(Enum):
    """How recently a runtime last wrote a checkpoint or heartbeat.

    `NOT_RECORDED` is kept apart from `STALE`: a runtime that has never written
    a checkpoint and one that wrote one three days ago call for different
    operator action, and collapsing them would hide which.
    """

    FRESH = "FRESH"
    STALE = "STALE"
    NOT_RECORDED = "NOT_RECORDED"


class CleanupVerdict(Enum):
    """What the cleanup planner concluded about one symbol.

    `NOT_POSSIBLE` is the interesting one: a residual position too small to
    clear the broker's own minimum order value cannot be closed by any order,
    and reporting that plainly is more useful than emitting a command the
    broker would refuse.
    """

    NONE_REQUIRED = "NO_CLEANUP_REQUIRED"
    REQUIRED = "CLEANUP_REQUIRED"
    NOT_POSSIBLE = "CLEANUP_NOT_POSSIBLE"


class BrokerReadClient(Protocol):
    """The read-only broker surface this harness is allowed to touch.

    A Protocol rather than an import, so no module under `autotrader.smoke`
    imports the Alpaca SDK at all. That is asserted by a test: the harness is
    handed a client built by the one existing paper factory, and it can only
    call methods that read.

    Note what is absent: every method that submits, cancels, replaces, or
    closes. Those names are not written out even as prose, because this
    repository's boundary test forbids the broker's order vocabulary outside
    `autotrader.execution` and this package is held to that stricter rule.

    A client passed in here is not narrowed at runtime by a Protocol, so this
    is documentation plus a structural test rather than an enforcement
    mechanism; the enforcement is that no call site exists.
    """

    def get_account(self) -> object: ...

    def get_all_positions(self) -> list[object]: ...


@dataclass(frozen=True)
class CheckResult:
    """One named gate, its verdict, and the evidence behind it.

    `detail` is assembled from symbols, quantities, statuses, paths, and
    `client_order_id` values only - never from a credential, a header, or an
    account number. It is printed verbatim and may be written to a snapshot
    file, so anything secret placed here would leak twice.
    """

    name: str
    verdict: SmokeVerdict
    detail: str

    @property
    def blocking(self) -> bool:
        """Whether this check on its own stops a smoke from starting."""
        return self.verdict is SmokeVerdict.FAIL


@dataclass(frozen=True)
class GateReport:
    """A collection of checks and the single answer they add up to.

    The answer is derived, never stored: a report cannot be constructed that
    says `READY_FOR_PAPER_SMOKE` while holding a failing check.
    """

    checks: tuple[CheckResult, ...] = ()

    @property
    def ready(self) -> bool:
        """True only when every check passed."""
        return all(not check.blocking for check in self.checks)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        """Just the checks that block, in the order they were run."""
        return tuple(check for check in self.checks if check.blocking)

    def verdict_text(self) -> str:
        """`READY_FOR_PAPER_SMOKE` or `BLOCKED`. The whole point of the report."""
        return READY_FOR_PAPER_SMOKE if self.ready else BLOCKED


@dataclass(frozen=True)
class PositionSnapshot:
    """One symbol's quantity as the **broker** reported it.

    Named for where it came from. This is never derived from an order this
    system sent, never from a quantity it requested, and never from a local
    `positions` row - all three have been wrong at some point in this
    repository's history, and the cleanup planner sizes from this one.
    """

    symbol: str
    quantity: Decimal
    market_value: float | None = None
    average_entry_price: float | None = None


@dataclass(frozen=True)
class OrderReport:
    """What the broker says about one order, plus what it does *not* say.

    `open_remainder` is `quantity - filled_quantity`, computed here rather than
    inferred from status text. `broker_position` is carried alongside on
    purpose: a reader comparing "filled 0.00016705" against "position
    0.000166632" should see both numbers at once and reach for the second.
    """

    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    status: str
    quantity: Decimal
    filled_quantity: Decimal
    filled_average_price: float | None
    submitted_at: datetime | None
    filled_at: datetime | None
    broker_updated_at: datetime | None
    broker_position: PositionSnapshot | None

    @property
    def open_remainder(self) -> Decimal:
        """Quantity the broker still has working. Never negative."""
        remainder = self.quantity - self.filled_quantity
        return remainder if remainder > 0 else Decimal(0)

    @property
    def is_open(self) -> bool:
        """Whether the broker still has working quantity for this order.

        Deliberately derived from quantities rather than from `status`: status
        vocabularies drift between providers and versions, and a leftover
        remainder is the fact that matters to an audit.
        """
        return self.open_remainder > 0


@dataclass(frozen=True)
class CleanupPlan:
    """A risk-reducing quantity, and the arithmetic that produced it.

    Every field is reported so the operator can check the harness rather than
    trust it. `position_quantity` is broker truth; `plan_quantity` is that
    number rounded **down** to what the broker will accept. The planner
    asserts `plan_quantity <= position_quantity`; a plan that closed more than
    is held would open a short, which this system cannot express.
    """

    symbol: str
    verdict: CleanupVerdict
    position_quantity: Decimal
    plan_quantity: Decimal
    reference_price: float | None
    estimated_value: Decimal | None
    min_order_size: Decimal | None
    min_trade_increment: Decimal | None
    minimum_notional_quantity: Decimal | None
    full_cleanup_possible: bool
    reason: str
    command: str | None = None

    @property
    def residual_quantity(self) -> Decimal:
        """What would remain if the planned quantity were sold. Never negative."""
        residual = self.position_quantity - self.plan_quantity
        return residual if residual > 0 else Decimal(0)


@dataclass(frozen=True)
class RuntimeHealth:
    """How stale one runtime's durable checkpoint is, per symbol."""

    symbol: str
    freshness: Freshness
    last_processed_bar: datetime | None
    updated_at: datetime | None
    age_seconds: float | None


@dataclass(frozen=True)
class DashboardHealth:
    """The optional dashboard read. Never a reason to block broker cleanup.

    A dashboard is a view of state, not the state. If it is down, the broker
    and the database still answer, and those are what a cleanup decision is
    made from - so `available=False` is reported and nothing is gated on it.
    """

    available: bool
    url: str | None
    status_code: int | None
    detail: str
    payload_keys: tuple[str, ...] = ()
    credential_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class BaselineComparison:
    """Tracked exposure before the smoke against tracked exposure after it."""

    symbol: str
    before: Decimal
    after: Decimal

    @property
    def restored(self) -> bool:
        """Exact equality. A crypto dust remainder is a difference, not noise."""
        return self.before == self.after

    @property
    def delta(self) -> Decimal:
        return self.after - self.before


@dataclass(frozen=True)
class AuditReport:
    """The end-state answer, and the comparisons behind it."""

    gate: GateReport
    comparisons: tuple[BaselineComparison, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        return self.gate.ready

    @property
    def exposure_restored(self) -> bool | None:
        """True/False when a baseline was compared, None when none was given."""
        if not self.comparisons:
            return None
        return all(comparison.restored for comparison in self.comparisons)

    def verdict_text(self) -> str:
        return SMOKE_COMPLETE if self.complete else SMOKE_INCOMPLETE

    def exposure_text(self) -> str | None:
        restored = self.exposure_restored
        if restored is None:
            return None
        return EXPOSURE_RESTORED if restored else EXPOSURE_NOT_RESTORED


__all__ = [
    "BLOCKED",
    "DO_NOT_RETRY_BANNER",
    "EXPOSURE_NOT_RESTORED",
    "EXPOSURE_RESTORED",
    "ORDER_TRUTH_UNRESOLVED",
    "READY_FOR_PAPER_SMOKE",
    "SMOKE_COMPLETE",
    "SMOKE_INCOMPLETE",
    "USER_MUST_EXECUTE_BANNER",
    "AuditReport",
    "BaselineComparison",
    "BrokerUnreadableError",
    "CheckResult",
    "CleanupPlan",
    "CleanupVerdict",
    "DashboardHealth",
    "Freshness",
    "GateReport",
    "OrderReport",
    "PositionSnapshot",
    "RuntimeHealth",
    "SmokeError",
    "SmokeInputError",
    "SmokeVerdict",
    "StateUnreadableError",
    "BrokerReadClient",
]
