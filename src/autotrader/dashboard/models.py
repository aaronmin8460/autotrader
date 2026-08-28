"""C10: the dashboard's wire vocabulary. Standard library only, and read-only.

Every type here is a **frozen dataclass of JSON-ready primitives**: strings,
floats, ints, bools, `None`, and tuples of the same. There is no `Decimal` and
no `datetime` on the wire, because both would have to be coerced by whatever
serializes them and the coercion is where precision quietly disappears. Exact
quantities travel as canonical decimal *text* (`"0.000166632"`), timestamps as
ISO-8601 UTC text. `service` does that conversion once, deliberately, where it
can be read.

**The unavailable state is a first-class value, not a zero.** `Amount` is
either a number or an honest `unavailable_reason`, and there is no third
option: a metric this system cannot truthfully read must not reach a screen as
`$0.00`, because an operator cannot tell a flat account from an unreadable one.
Every headline figure, every risk utilization, and every panel that depends on
the broker carries that distinction all the way to the browser.

**Nothing here can act.** These are records of what was read. There is no
command type, no request body, no mutation, and no field a browser could send
back that would mean anything - the dashboard API is GET-only and this
vocabulary is the whole of what it says.

Like `reconciliation.models`, this module imports only the standard library:
the thing that describes the read model must not drag a broker SDK, a web
framework, or a database driver into whatever imports it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Vocabulary
#
# Stable machine strings. Labels may be reworded; these may not.
# --------------------------------------------------------------------------

#: How the whole system reads at a glance. Exactly three values, because an
#: operator triaging a screen makes exactly three decisions: carry on, look
#: closer, or stop.
SYSTEM_HEALTHY = "HEALTHY"
SYSTEM_ATTENTION = "ATTENTION"
SYSTEM_PAUSED = "PAUSED"

SYSTEM_STATES: tuple[str, ...] = (SYSTEM_HEALTHY, SYSTEM_ATTENTION, SYSTEM_PAUSED)

#: How one value should read, semantically. The frontend maps tone to colour;
#: it never decides tone itself, so "what is bad" is defined once, here.
TONE_NEUTRAL = "NEUTRAL"
TONE_POSITIVE = "POSITIVE"
TONE_NEGATIVE = "NEGATIVE"
TONE_ATTENTION = "ATTENTION"
TONE_MUTED = "MUTED"

TONES: tuple[str, ...] = (
    TONE_NEUTRAL,
    TONE_POSITIVE,
    TONE_NEGATIVE,
    TONE_ATTENTION,
    TONE_MUTED,
)

#: Why a figure is not being shown. Never a message with a path, a header, a
#: credential, or a raw exception string in it - those are the four things a
#: broker error is most likely to carry, and none of them belong in a browser.
UNAVAILABLE_BROKER_NOT_CONFIGURED = "BROKER_NOT_CONFIGURED"
UNAVAILABLE_BROKER_UNREADABLE = "BROKER_UNREADABLE"
UNAVAILABLE_DATABASE_UNREADABLE = "DATABASE_UNREADABLE"
UNAVAILABLE_NOT_RECORDED = "NOT_RECORDED"

UNAVAILABLE_REASONS: tuple[str, ...] = (
    UNAVAILABLE_BROKER_NOT_CONFIGURED,
    UNAVAILABLE_BROKER_UNREADABLE,
    UNAVAILABLE_DATABASE_UNREADABLE,
    UNAVAILABLE_NOT_RECORDED,
)

#: Where a row came from. `BROKER` is authoritative; `LOCAL` is the last
#: snapshot this system wrote down and may be stale, and saying so on the row
#: is the difference between a stale number and a wrong one.
SOURCE_BROKER = "BROKER"
SOURCE_LOCAL = "LOCAL"
SOURCE_UNAVAILABLE = "UNAVAILABLE"

#: Asset classes this read model can represent. `EQUITY` exists here because
#: the shape must survive Equity V0.2 arriving; nothing in this milestone
#: produces an equity row that was not actually persisted by an earlier one.
ASSET_CLASS_CRYPTO = "CRYPTO"
ASSET_CLASS_EQUITY = "EQUITY"

#: The one environment this repository can reach. Rendered in the header so a
#: screenshot is self-identifying.
ENVIRONMENT_PAPER = "PAPER"


# --------------------------------------------------------------------------
# Amounts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Amount:
    """A USD figure, or an honest statement that it could not be read.

    `available` is the whole contract. When it is False, `value` is None and
    `unavailable_reason` names which read failed - and a renderer must show
    that rather than substituting a zero, an em dash with no explanation, or a
    stale figure from an earlier poll.
    """

    value: float | None = None
    available: bool = False
    unavailable_reason: str | None = None

    @classmethod
    def of(cls, value: float) -> Amount:
        """A figure that was actually read."""
        return cls(value=float(value), available=True, unavailable_reason=None)

    @classmethod
    def unavailable(cls, reason: str) -> Amount:
        """A figure that could not be read, and why."""
        return cls(value=None, available=False, unavailable_reason=reason)

    def __post_init__(self) -> None:
        if self.available and self.value is None:
            raise ValueError("An available Amount must carry a value.")
        if not self.available and self.value is not None:
            raise ValueError(
                "An unavailable Amount must not carry a value; a number that is "
                "presented as unknown and also shown would be read as the truth."
            )
        if not self.available and self.unavailable_reason not in UNAVAILABLE_REASONS:
            raise ValueError(
                f"unavailable_reason must be one of {', '.join(UNAVAILABLE_REASONS)}, "
                f"got {self.unavailable_reason!r}."
            )


# --------------------------------------------------------------------------
# Primary metrics
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PrimaryMetrics:
    """The four numbers the header row answers with, and their context.

    `daily_pnl` is measured against the stored UTC-day baseline, never against
    a broker's previous-close field: a market that never closes does not have
    one (see `autotrader.execution.paper.resolve_daily_baseline_equity`). Both
    halves must be readable for the figure to exist, so a day with no stored
    baseline yields `NOT_RECORDED` rather than a P&L of zero.
    """

    equity: Amount
    cash: Amount
    daily_pnl: Amount
    daily_pnl_fraction: float | None
    daily_pnl_baseline: Amount
    daily_pnl_baseline_date: str | None
    exposure: Amount
    exposure_fraction: float | None


# --------------------------------------------------------------------------
# Positions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionRow:
    """One holding, normalized for display.

    `quantity` is exact decimal text - a crypto quantity rounded through a
    binary float is a different quantity. `price` is the current mark implied
    by the broker's own market value, not a quote this dashboard went and
    fetched: one broker read, one set of numbers, no chance of a row whose
    price and market value came from different instants.
    """

    symbol: str
    asset_class: str
    quantity: str
    price: float | None
    market_value: float | None
    average_entry_price: float | None
    unrealized_pnl: float | None
    unrealized_pnl_fraction: float | None
    updated_at: str
    source: str


@dataclass(frozen=True)
class PositionsPanel:
    """What the account holds, and where that answer came from.

    `flat_symbols` are symbols this system has a local row for that currently
    hold nothing. They are reported separately rather than as zero-quantity
    rows: a flat symbol is not a holding, and a positions table padded with
    zeroes is harder to read than an empty one.
    """

    source: str
    as_of: str | None
    rows: tuple[PositionRow, ...] = ()
    flat_symbols: tuple[str, ...] = ()
    unavailable_reason: str | None = None
    note: str | None = None


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderRow:
    """One order this system decided to place, and what became of it.

    Built from the durable intent joined to the broker snapshot, so an intent
    the broker never answered for still appears - that is precisely the row an
    operator needs to see. `status` is the broker's own status when there is
    one, upper-cased for display, and the intent's status when there is not;
    `status_source` says which, because "the broker says filled" and "we
    recorded that we were submitting" are not the same claim.
    """

    client_order_id: str
    created_at: str
    symbol: str
    asset_class: str
    side: str
    quantity: str
    filled_quantity: str | None
    average_fill_price: float | None
    status: str
    status_tone: str
    status_source: str
    needs_attention: bool
    risk_reason_code: str
    broker_order_id: str | None
    submitted_at: str | None
    filled_at: str | None


@dataclass(frozen=True)
class OrdersPanel:
    """The most recent orders, newest first, plus how many exist in total."""

    rows: tuple[OrderRow, ...] = ()
    total: int = 0
    attention_count: int = 0
    unavailable_reason: str | None = None


# --------------------------------------------------------------------------
# System health
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthComponent:
    """One line of the health panel: a subsystem, its status, and the evidence.

    `status` is quoted from stored truth wherever stored truth exists. Nothing
    here is inferred from the absence of an exception: a reconciliation that
    never ran reports that it never ran, and never reports `CLEAN`.
    """

    key: str
    label: str
    status: str
    tone: str
    detail: str | None = None


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationPanel:
    """The latest completed reconciliation pass, as stored.

    A pass that died midway wrote no run row, so `available=False` here means
    "no pass has ever finished", which is not the same as clean and is not
    permission to trade.
    """

    available: bool
    status: str | None = None
    tone: str = TONE_MUTED
    safe_to_trade: bool | None = None
    started_at: str | None = None
    completed_at: str | None = None
    orders_checked: int | None = None
    positions_checked: int | None = None
    issues: int | None = None
    repairs: int | None = None
    unresolved: int | None = None
    unavailable_reason: str | None = None


# --------------------------------------------------------------------------
# Runtime
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckpointRow:
    """The newest completed bar one symbol has durably claimed."""

    symbol: str
    last_processed_bar: str
    updated_at: str
    age_seconds: float
    stale: bool


@dataclass(frozen=True)
class RuntimePanel:
    """What the 24/7 loop's durable trail says about itself.

    The runtime's live `Heartbeat` is an in-process object and this is a
    different process, so nothing here is read from it. Every field comes from
    something the runtime wrote down: `strategy_runs`, `system_events`, and
    `runtime_checkpoints`. `last_cycle_at` is therefore the newest checkpoint
    write, which is the durable evidence of a cycle having completed work -
    and it is labelled as that rather than as a heartbeat.
    """

    state: str
    tone: str
    detail: str | None = None
    strategy_name: str | None = None
    mode: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    startup_safety: str = "UNRESOLVED"
    startup_safety_tone: str = TONE_ATTENTION
    startup_safety_detail: str | None = None
    paper_execution_enabled: bool = False
    paper_execution_detail: str | None = None
    last_cycle_at: str | None = None
    next_cycle_at: str | None = None
    checkpoints: tuple[CheckpointRow, ...] = ()
    last_error: str | None = None
    last_error_at: str | None = None


# --------------------------------------------------------------------------
# Risk
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskLimit:
    """One established V0.2 limit, and how much of it is currently used.

    `limit_fraction` is policy and is always shown: the limits exist whether
    or not the account behind them can be read right now. `used_*` is
    observation and may be unavailable, which is why the two are separate
    fields rather than one ratio.
    """

    key: str
    label: str
    limit_fraction: float
    limit_value: Amount
    used_value: Amount
    used_fraction: float | None
    utilization: float | None
    breached: bool
    subject: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class RiskPanel:
    """The V0.2 policy, plus current utilization when it can be read."""

    limits: tuple[RiskLimit, ...] = ()
    available: bool = False
    unavailable_reason: str | None = None


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Overview:
    """Everything one dashboard poll returns. One read, one consistent picture.

    Assembled from a single short read transaction plus at most one broker
    read, so two panels can never disagree about the same instant the way two
    independently polled endpoints would.
    """

    generated_at: str
    environment: str
    system_state: str
    system_state_tone: str
    attention: tuple[str, ...] = ()
    database: HealthComponent | None = None
    broker: HealthComponent | None = None
    metrics: PrimaryMetrics | None = None
    positions: PositionsPanel | None = None
    orders: OrdersPanel | None = None
    health: tuple[HealthComponent, ...] = ()
    reconciliation: ReconciliationPanel | None = None
    runtime: RuntimePanel | None = None
    risk: RiskPanel | None = None
    notices: tuple[str, ...] = field(default_factory=tuple)


__all__ = [
    "ASSET_CLASS_CRYPTO",
    "ASSET_CLASS_EQUITY",
    "ENVIRONMENT_PAPER",
    "SOURCE_BROKER",
    "SOURCE_LOCAL",
    "SOURCE_UNAVAILABLE",
    "SYSTEM_ATTENTION",
    "SYSTEM_HEALTHY",
    "SYSTEM_PAUSED",
    "SYSTEM_STATES",
    "TONES",
    "TONE_ATTENTION",
    "TONE_MUTED",
    "TONE_NEGATIVE",
    "TONE_NEUTRAL",
    "TONE_POSITIVE",
    "UNAVAILABLE_BROKER_NOT_CONFIGURED",
    "UNAVAILABLE_BROKER_UNREADABLE",
    "UNAVAILABLE_DATABASE_UNREADABLE",
    "UNAVAILABLE_NOT_RECORDED",
    "UNAVAILABLE_REASONS",
    "Amount",
    "CheckpointRow",
    "HealthComponent",
    "OrderRow",
    "OrdersPanel",
    "Overview",
    "PositionRow",
    "PositionsPanel",
    "PrimaryMetrics",
    "ReconciliationPanel",
    "RiskLimit",
    "RiskPanel",
    "RuntimePanel",
]
