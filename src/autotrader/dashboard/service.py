"""C10: the read model. Every number the dashboard shows is derived here.

One function is the whole entry point - `build_overview(...)` - and it does
three things in order: take one short, consistent read of the local database,
attempt one read of the paper broker, and assemble both into the `Overview`
the API returns. Nothing else in this package reads state, and nothing in this
package writes any.

**Structurally read-only, not carefully read-only.** The database is opened
with SQLite's `mode=ro` URI plus `PRAGMA query_only`, so a write is refused by
the engine rather than avoided by discipline - a stray `INSERT` here raises
instead of landing. The read runs inside one `BEGIN DEFERRED` ... `COMMIT`, so
every panel describes the same instant, and in WAL mode a deferred reader takes
no lock a writer waits on: the trading runtime is not slowed, blocked, or
checkpoint-stalled by anyone watching it.

**The unreadable states are designed, not incidental.** A locked or missing
database, absent credentials, and an unreachable broker are three different
answers and the screen says which. None of them is allowed to become a zero, a
stale carry-over, or an empty table that reads like "nothing happened".

**Nothing is inferred from the absence of an exception.** Reconciliation
reports the status that was actually stored; a database with no reconciliation
run reports that no pass has ever completed, which is not `CLEAN`. Runtime
state is read from `strategy_runs`, `system_events`, and `runtime_checkpoints`
- the runtime's live `Heartbeat` belongs to a different process and is not
guessed at from here.

**No equity book is invented.** Rows are produced from broker positions and
from the local snapshot, whatever their asset class; `asset_class_for` reads
the symbol's own notation. If Equity V0.2 later persists an `AAPL` position it
renders as an equity row with no change here, and until something persists one
there is no equity row to render.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from autotrader import state
from autotrader.account import budget as api_budget_module
from autotrader.dashboard import models
from autotrader.dashboard.broker import BrokerRead, read_broker
from autotrader.dashboard.models import (
    ASSET_CLASS_CRYPTO,
    ASSET_CLASS_EQUITY,
    ENVIRONMENT_PAPER,
    SOURCE_BROKER,
    SOURCE_LOCAL,
    SOURCE_UNAVAILABLE,
    SYSTEM_ATTENTION,
    SYSTEM_HEALTHY,
    SYSTEM_PAUSED,
    TONE_ATTENTION,
    TONE_MUTED,
    TONE_NEGATIVE,
    TONE_NEUTRAL,
    TONE_POSITIVE,
    UNAVAILABLE_BROKER_NOT_CONFIGURED,
    UNAVAILABLE_BROKER_UNREADABLE,
    UNAVAILABLE_DATABASE_UNREADABLE,
    UNAVAILABLE_NOT_RECORDED,
    AccountSafetyPanel,
    Amount,
    ApiBudgetRow,
    CheckpointRow,
    ExposureRow,
    HealthComponent,
    OrderRow,
    OrdersPanel,
    Overview,
    PositionRow,
    PositionsPanel,
    PrimaryMetrics,
    ReconciliationPanel,
    RiskLimit,
    RiskPanel,
    RuntimePanel,
)
from autotrader.execution.models import EQUITY_SYMBOLS, SUPPORTED_SYMBOLS
from autotrader.execution.models import TRADABLE_SYMBOLS as TRACKED_SYMBOLS
from autotrader.execution.paper import broker_symbol_key, paper_trading_enabled
from autotrader.risk.engine import (
    MAX_DAILY_LOSS_FRACTION,
    MAX_POSITION_FRACTION,
    MAX_TOTAL_EXPOSURE_FRACTION,
)
from autotrader.runtime.schedule import BAR_INTERVAL, DEFAULT_SAFETY_DELAY, next_wake_time

# --------------------------------------------------------------------------
# Tuning
# --------------------------------------------------------------------------

#: How long the dashboard waits for a busy database before giving up and
#: reporting it unreadable. Deliberately shorter than the runtime's own
#: `BUSY_TIMEOUT_MS`: a viewer that waits is a viewer holding a connection,
#: and being told "unreadable" quickly is better than a page that hangs.
READ_TIMEOUT_SECONDS = 2.0

#: A checkpoint older than this is stale. Two whole bar intervals plus the
#: provider-lag allowance, so a single late boundary is not an alarm and two
#: consecutive misses are.
STALE_AFTER = 2 * BAR_INTERVAL + DEFAULT_SAFETY_DELAY

#: How many orders the panel carries. Enough to see a session, few enough that
#: the table stays a table.
DEFAULT_ORDER_LIMIT = 12

#: How many recent system events are read to find the newest failure line.
SYSTEM_EVENT_SCAN = 50

#: Longest `last_error` text forwarded to the browser. System-event messages
#: are written by this repository and carry no credential, but an unbounded
#: string in a fixed-height card is a layout bug waiting to happen.
MAX_ERROR_LENGTH = 240

#: Event-type fragments that mark a system event as something that went wrong.
#: Matched on `event_type`, which is a closed vocabulary this repository owns,
#: never on the message text.
_FAILURE_EVENT_MARKERS: tuple[str, ...] = (
    "FAILED",
    "REJECTED",
    "PAUSED",
    "ERROR",
    "UNRESOLVED",
)

#: `system_events.event_type` written by the runtime when it stops trading.
_EVENT_TRADING_PAUSED = "RUNTIME_TRADING_PAUSED"

_ZERO = Decimal(0)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


#: The tracked universes, keyed the way the broker might spell them. Alpaca
#: reports a crypto market as `BTC/USD` in some responses and `BTCUSD` in
#: others, so a slash is not a reliable discriminator on its own.
_CRYPTO_KEYS = frozenset(broker_symbol_key(symbol) for symbol in SUPPORTED_SYMBOLS)
_EQUITY_KEYS = frozenset(broker_symbol_key(symbol) for symbol in EQUITY_SYMBOLS)


def asset_class_for(symbol: str) -> str:
    """Which book a symbol belongs to.

    Resolved against the two tracked universes first, using the same
    slash-insensitive key the execution boundary matches positions with. That
    matters now that this classification feeds an exposure *breakdown*: Alpaca
    reports a crypto position as `BTC/USD` or `BTCUSD` depending on the
    response, and a slash test alone would file the second spelling under
    equity - putting real crypto exposure in the equity row.

    Anything outside both universes falls back to the notation: a quoted pair
    is a crypto market, a bare ticker is not. That case is a position this
    system does not track, which reconciliation reports and never trades.
    """
    key = broker_symbol_key(symbol)
    if key in _CRYPTO_KEYS:
        return ASSET_CLASS_CRYPTO
    if key in _EQUITY_KEYS:
        return ASSET_CLASS_EQUITY
    return ASSET_CLASS_CRYPTO if "/" in symbol else ASSET_CLASS_EQUITY


def _iso(moment: datetime | None) -> str | None:
    """One timestamp as ISO-8601 UTC text, or None."""
    return None if moment is None else moment.astimezone(UTC).isoformat()


def _decimal_text(quantity: Decimal) -> str:
    """An exact quantity as canonical decimal text, never as a float."""
    return state.to_decimal_text(quantity)


def _fraction(numerator: float, denominator: float) -> float | None:
    """`numerator / denominator`, or None when the denominator cannot divide."""
    if denominator <= 0:
        return None
    return numerator / denominator


def _truncate(text: str) -> str:
    """`text`, bounded, with an ellipsis when it was cut."""
    if len(text) <= MAX_ERROR_LENGTH:
        return text
    return text[: MAX_ERROR_LENGTH - 1].rstrip() + "…"


# --------------------------------------------------------------------------
# The database read
# --------------------------------------------------------------------------


@contextmanager
def read_only_connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open `path` read-only and close it on exit.

    `mode=ro` makes every write an engine-level error rather than a convention,
    and `query_only` closes the same door from the other side. Neither is
    load-bearing on its own; together they mean the dashboard's inability to
    mutate trading state does not depend on anybody reading this file.

    Unlike `state.connect`, no `PRAGMA journal_mode` is issued here: setting a
    journal mode is a write to the database header, and a viewer has no
    business touching the journalling of a database a trading process owns.
    """
    uri = f"file:{quote(str(Path(path).resolve()))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=READ_TIMEOUT_SECONDS, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = 1")
        yield connection
    finally:
        connection.close()


@dataclass(frozen=True)
class StateSnapshot:
    """One consistent read of the local database, or the reason there is none.

    Every list is materialized inside a single deferred read transaction, so
    the positions, orders, checkpoints, and reconciliation status on one screen
    all describe the same instant of the database rather than four instants a
    few milliseconds apart.
    """

    ok: bool
    reason: str | None = None
    schema_version: int | None = None
    positions: tuple[state.Position, ...] = ()
    intents: tuple[state.StoredOrderIntent, ...] = ()
    broker_orders: tuple[state.StoredBrokerOrder, ...] = ()
    checkpoints: tuple[state.RuntimeCheckpoint, ...] = ()
    strategy_runs: tuple[state.StrategyRun, ...] = ()
    system_events: tuple[state.SystemEvent, ...] = ()
    reconciliation: state.ReconciliationRun | None = None
    reconciliation_repairs: int | None = None
    baseline: state.DailyRiskBaseline | None = None
    account_safety: state.AccountSafetyState | None = None
    api_budget: tuple[state.ApiBudgetWindow, ...] = ()


def read_state(path: str | Path, *, now: datetime) -> StateSnapshot:
    """Read everything one dashboard poll needs, in one short read transaction.

    Any failure to read - a missing file, a database another process has locked
    beyond `READ_TIMEOUT_SECONDS`, an unreadable schema - returns
    `ok=False` rather than raising. There is deliberately no repair path, no
    `initialize_database` call, and no migration: a viewer that creates or
    upgrades a trading database is a viewer that has written to it.
    """
    try:
        with read_only_connection(path) as connection:
            connection.execute("BEGIN DEFERRED")
            try:
                schema_version = state.get_schema_version(connection)
                positions = tuple(state.list_positions(connection))
                intents = tuple(state.list_order_intents(connection))
                broker_orders = tuple(state.list_broker_orders(connection))
                checkpoints = tuple(state.list_runtime_checkpoints(connection))
                strategy_runs = tuple(state.list_strategy_runs(connection))
                system_events = tuple(state.list_system_events(connection))
                reconciliation = state.latest_reconciliation_run(connection)
                repairs = (
                    None
                    if reconciliation is None
                    else sum(
                        1
                        for event in state.list_reconciliation_events(connection, reconciliation.id)
                        if event.outcome == state.RECONCILIATION_STATUS_REPAIRED
                    )
                )
                baseline = state.get_daily_risk_baseline(connection, state.utc_risk_date(now))
                account_safety = state.read_account_safety_state(connection)
                api_budget = tuple(
                    window
                    for window in (
                        state.get_api_budget_window(
                            connection,
                            budget=budget,
                            window_start=api_budget_module.window_start_for(now),
                        )
                        for budget in state.API_BUDGETS
                    )
                    if window is not None
                )
            finally:
                connection.execute("COMMIT")
    except (sqlite3.Error, state.StateError, OSError, ValueError):
        return StateSnapshot(ok=False, reason=UNAVAILABLE_DATABASE_UNREADABLE)

    return StateSnapshot(
        ok=True,
        schema_version=schema_version,
        positions=positions,
        intents=intents,
        broker_orders=broker_orders,
        checkpoints=checkpoints,
        strategy_runs=strategy_runs,
        system_events=system_events[-SYSTEM_EVENT_SCAN:],
        reconciliation=reconciliation,
        reconciliation_repairs=repairs,
        baseline=baseline,
        account_safety=account_safety,
        api_budget=api_budget,
    )


# --------------------------------------------------------------------------
# Panels
# --------------------------------------------------------------------------


def _broker_reason(broker: BrokerRead) -> str:
    """The unavailable reason to attribute to a figure the broker owns."""
    return broker.reason or UNAVAILABLE_BROKER_UNREADABLE


def build_positions(snapshot: StateSnapshot, broker: BrokerRead) -> PositionsPanel:
    """What the account holds.

    The broker is the authority and is used whenever it can be read. The local
    `positions` table is a fallback and is labelled as one: it is a snapshot
    this system wrote down, nothing keeps it live, and presenting it as though
    it were the broker's current book is exactly the confusion reconciliation
    exists to prevent. Its rows therefore carry no market price and no P&L -
    `average_price` is an entry price, and dividing an entry price into a
    market value it did not produce would manufacture a number.
    """
    if broker.ok and broker.positions is not None:
        rows: list[PositionRow] = []
        as_of = _iso(datetime.now(UTC))
        for position in sorted(broker.positions.values(), key=lambda item: item.symbol):
            if position.quantity <= _ZERO:
                continue
            quantity = float(position.quantity)
            price = position.market_value / quantity if quantity else None
            entry = position.average_entry_price
            cost = entry * quantity if entry is not None else None
            pnl = position.market_value - cost if cost is not None else None
            rows.append(
                PositionRow(
                    symbol=position.symbol,
                    asset_class=asset_class_for(position.symbol),
                    quantity=_decimal_text(position.quantity),
                    price=price,
                    market_value=position.market_value,
                    average_entry_price=entry,
                    unrealized_pnl=pnl,
                    unrealized_pnl_fraction=(
                        None if pnl is None or not cost else _fraction(pnl, cost)
                    ),
                    updated_at=as_of or "",
                    source=SOURCE_BROKER,
                )
            )
        held = {broker_symbol_key(row.symbol) for row in rows}
        # Every symbol this system tracks, both books. A flat equity is as much
        # a tracked-and-flat symbol as a flat pair, and listing only the pairs
        # would read as "the equities are not being watched".
        flat = tuple(symbol for symbol in TRACKED_SYMBOLS if broker_symbol_key(symbol) not in held)
        return PositionsPanel(
            source=SOURCE_BROKER, as_of=as_of, rows=tuple(rows), flat_symbols=flat
        )

    if not snapshot.ok:
        return PositionsPanel(
            source=SOURCE_UNAVAILABLE,
            as_of=None,
            unavailable_reason=UNAVAILABLE_DATABASE_UNREADABLE,
        )

    local_rows = tuple(
        PositionRow(
            symbol=position.symbol,
            asset_class=asset_class_for(position.symbol),
            quantity=_decimal_text(position.quantity),
            price=None,
            market_value=None,
            average_entry_price=position.average_price,
            unrealized_pnl=None,
            unrealized_pnl_fraction=None,
            updated_at=_iso(position.updated_at) or "",
            source=SOURCE_LOCAL,
        )
        for position in snapshot.positions
        if position.quantity > _ZERO
    )
    flat = tuple(position.symbol for position in snapshot.positions if position.quantity <= _ZERO)
    newest = max((position.updated_at for position in snapshot.positions), default=None)
    return PositionsPanel(
        source=SOURCE_LOCAL,
        as_of=_iso(newest),
        rows=local_rows,
        flat_symbols=flat,
        unavailable_reason=_broker_reason(broker),
        note=(
            "Local snapshot. The broker could not be read, so market prices and "
            "unrealized P&L are unavailable and these quantities may be stale."
        ),
    )


def _order_status(
    intent: state.StoredOrderIntent, order: state.StoredBrokerOrder | None
) -> tuple[str, str, str, bool]:
    """One order's display status, tone, source, and whether it needs a human.

    An `UNKNOWN` intent is the one status on this screen that means *nobody
    knows what the broker did*. It is the reason reconciliation exists, and it
    is the only status here that raises the whole page to ATTENTION.
    """
    if intent.status == state.INTENT_STATUS_UNKNOWN:
        return state.INTENT_STATUS_UNKNOWN, TONE_ATTENTION, SOURCE_LOCAL, True
    if order is not None:
        status = order.status.upper()
        if "REJECT" in status:
            return status, TONE_NEGATIVE, SOURCE_BROKER, False
        if status == "FILLED":
            return status, TONE_POSITIVE, SOURCE_BROKER, False
        if status in {"CANCELED", "CANCELLED", "EXPIRED", "DONE_FOR_DAY", "STOPPED"}:
            return status, TONE_MUTED, SOURCE_BROKER, False
        return status, TONE_NEUTRAL, SOURCE_BROKER, False
    if intent.status == state.INTENT_STATUS_REJECTED:
        return state.INTENT_STATUS_REJECTED, TONE_NEGATIVE, SOURCE_LOCAL, False
    if intent.status == state.INTENT_STATUS_CONFIRMED_NOT_SUBMITTED:
        return intent.status, TONE_MUTED, SOURCE_LOCAL, False
    return intent.status, TONE_NEUTRAL, SOURCE_LOCAL, False


def build_orders(snapshot: StateSnapshot, *, limit: int = DEFAULT_ORDER_LIMIT) -> OrdersPanel:
    """The most recent orders, newest first.

    Driven by `order_intents`, not by `broker_orders`: an intent the broker
    never answered for has no snapshot row, and it is the row that most needs
    to be on the screen. The broker snapshot is joined on when it exists.
    """
    if not snapshot.ok:
        return OrdersPanel(unavailable_reason=UNAVAILABLE_DATABASE_UNREADABLE)

    by_intent = {order.order_intent_id: order for order in snapshot.broker_orders}
    ordered = sorted(
        snapshot.intents, key=lambda intent: (intent.created_at, intent.id), reverse=True
    )
    rows: list[OrderRow] = []
    attention = 0
    for intent in ordered:
        order = by_intent.get(intent.id)
        status, tone, source, needs_attention = _order_status(intent, order)
        attention += 1 if needs_attention else 0
        if len(rows) >= limit:
            continue
        rows.append(
            OrderRow(
                client_order_id=intent.client_order_id,
                created_at=_iso(intent.created_at) or "",
                symbol=intent.symbol,
                asset_class=asset_class_for(intent.symbol),
                side=intent.side,
                quantity=_decimal_text(intent.approved_quantity),
                filled_quantity=None if order is None else _decimal_text(order.filled_quantity),
                average_fill_price=None if order is None else order.filled_average_price,
                status=status,
                status_tone=tone,
                status_source=source,
                needs_attention=needs_attention,
                risk_reason_code=intent.risk_reason_code,
                broker_order_id=None if order is None else order.broker_order_id,
                submitted_at=None if order is None else _iso(order.submitted_at),
                filled_at=None if order is None else _iso(order.filled_at),
            )
        )
    return OrdersPanel(rows=tuple(rows), total=len(ordered), attention_count=attention)


def build_reconciliation(snapshot: StateSnapshot) -> ReconciliationPanel:
    """The latest completed reconciliation pass, exactly as it was stored.

    `CLEAN` is never inferred. A database that cannot be read, and a database
    with no run in it, are two distinct unavailable answers and neither of them
    is permission to trade.
    """
    if not snapshot.ok:
        return ReconciliationPanel(
            available=False,
            unavailable_reason=UNAVAILABLE_DATABASE_UNREADABLE,
            tone=TONE_ATTENTION,
        )
    run = snapshot.reconciliation
    if run is None:
        return ReconciliationPanel(
            available=False,
            unavailable_reason=UNAVAILABLE_NOT_RECORDED,
            tone=TONE_ATTENTION,
        )
    tone = {
        state.RECONCILIATION_STATUS_CLEAN: TONE_POSITIVE,
        state.RECONCILIATION_STATUS_REPAIRED: TONE_NEUTRAL,
        state.RECONCILIATION_STATUS_UNRESOLVED: TONE_ATTENTION,
        state.RECONCILIATION_STATUS_FAILED: TONE_NEGATIVE,
    }.get(run.status, TONE_ATTENTION)
    return ReconciliationPanel(
        available=True,
        status=run.status,
        tone=tone,
        safe_to_trade=run.safe_to_trade,
        started_at=_iso(run.started_at),
        completed_at=_iso(run.completed_at),
        orders_checked=run.orders_checked,
        positions_checked=run.positions_checked,
        issues=run.issues_count,
        repairs=snapshot.reconciliation_repairs,
        unresolved=run.unresolved_count,
    )


def _latest_failure(snapshot: StateSnapshot) -> state.SystemEvent | None:
    """The newest system event whose *type* says something went wrong.

    Matched on `event_type`, a closed vocabulary this repository writes, rather
    than by searching message prose for alarming words.
    """
    failures = [
        event
        for event in snapshot.system_events
        if any(marker in event.event_type for marker in _FAILURE_EVENT_MARKERS)
    ]
    if not failures:
        return None
    return max(failures, key=lambda event: (event.event_timestamp, event.id))


#: The two services, and the durable evidence that tells them apart. Each
#: runtime writes its own lifecycle event types and claims bars only for its own
#: symbols, so a panel per service is derived rather than guessed. What is
#: *not* here is the strategy run: both services open one under the same
#: strategy name, so attributing a run to a service would be a guess.
RUNTIME_SPECS: tuple[dict[str, object], ...] = (
    {
        "key": "crypto",
        "label": "Crypto runtime",
        "symbols": frozenset(SUPPORTED_SYMBOLS),
        "started": "RUNTIME_STARTED",
        "stopped": "RUNTIME_STOPPED",
        "paused": "RUNTIME_TRADING_PAUSED",
    },
    {
        "key": "equity",
        "label": "Equity runtime",
        "symbols": frozenset(EQUITY_SYMBOLS),
        "started": "EQUITY_RUNTIME_STARTED",
        "stopped": "EQUITY_RUNTIME_STOPPED",
        "paused": "EQUITY_RUNTIME_TRADING_PAUSED",
    },
)


def _latest_event_at(snapshot: StateSnapshot, event_type: str) -> datetime | None:
    """When one event type was last recorded, or None."""
    return max(
        (
            event.event_timestamp
            for event in snapshot.system_events
            if event.event_type == event_type
        ),
        default=None,
    )


def build_runtime(
    snapshot: StateSnapshot,
    *,
    now: datetime,
    spec: dict[str, object],
    account_safety: AccountSafetyPanel | None = None,
) -> RuntimePanel:
    """What one runtime's durable trail says about itself.

    Derived, never guessed. This service's own lifecycle events say whether it
    started, stopped, or paused; this service's own symbols' checkpoints say how
    recently it did work. A runtime that started and has not moved one of its
    checkpoints for two whole bar intervals is `STALE`, because a loop that is
    not looping is not running.

    **The shared account halt is reported here too**, and on *both* panels,
    because that is what it does: an ambiguous order raised by either service
    stops both. A runtime whose own trail looks perfectly healthy is still
    `PAUSED` while the account is halted, and saying otherwise on one of the two
    panels would be the single most misleading thing this screen could do.
    """
    paper_enabled = paper_trading_enabled()
    paper_detail = (
        "AUTOTRADER_PAPER_TRADING_ENABLED is set. The runtime additionally requires a "
        "per-process confirmation that is not persisted."
        if paper_enabled
        else "AUTOTRADER_PAPER_TRADING_ENABLED is not set to true. No order can be submitted."
    )
    key = str(spec["key"])
    label = str(spec["label"])

    if not snapshot.ok:
        return RuntimePanel(
            key=key,
            label=label,
            state="UNAVAILABLE",
            tone=TONE_ATTENTION,
            detail="The operational database could not be read.",
            paper_execution_enabled=paper_enabled,
            paper_execution_detail=paper_detail,
        )

    symbols = spec["symbols"]
    assert isinstance(symbols, frozenset)  # noqa: S101 - RUNTIME_SPECS is a module literal
    mine = tuple(checkpoint for checkpoint in snapshot.checkpoints if checkpoint.symbol in symbols)
    checkpoints = tuple(
        CheckpointRow(
            symbol=checkpoint.symbol,
            last_processed_bar=_iso(checkpoint.last_processed_bar_timestamp) or "",
            updated_at=_iso(checkpoint.updated_at) or "",
            age_seconds=(now - checkpoint.updated_at).total_seconds(),
            stale=(now - checkpoint.updated_at) > STALE_AFTER,
        )
        for checkpoint in mine
    )
    last_cycle = max((checkpoint.updated_at for checkpoint in mine), default=None)

    started_at = _latest_event_at(snapshot, str(spec["started"]))
    stopped_at = _latest_event_at(snapshot, str(spec["stopped"]))
    paused_at = _latest_event_at(snapshot, str(spec["paused"]))
    if started_at is not None and paused_at is not None and paused_at < started_at:
        paused_at = None
    if started_at is not None and stopped_at is not None and stopped_at < started_at:
        stopped_at = None

    if started_at is None and last_cycle is None:
        runtime_state, tone = "NEVER STARTED", TONE_MUTED
        detail = f"No {key} runtime cycle is recorded."
    elif paused_at is not None:
        runtime_state, tone = "PAUSED", TONE_NEGATIVE
        detail = "This runtime recorded that it paused trading. It submits nothing further."
    elif stopped_at is not None:
        runtime_state, tone = "STOPPED", TONE_MUTED
        detail = "This runtime recorded a clean shutdown."
    else:
        stale = last_cycle is None or (now - last_cycle) > STALE_AFTER
        runtime_state = "STALE" if stale else "RUNNING"
        tone = TONE_ATTENTION if stale else TONE_POSITIVE
        detail = (
            "This runtime started but has not claimed a bar for two whole intervals."
            if stale
            else None
        )

    # An outstanding ambiguous order outranks this service's own view of itself:
    # it stops every service on the account, so a panel reporting RUNNING would
    # be describing a loop that is running and cannot trade. Applied after the
    # local verdict rather than mixed into it, so the reason shown is the
    # account's rather than a guess about this process.
    #
    # `UNSAFE_RECONCILIATION` deliberately does *not* override it. That state
    # means nothing has cleared trading yet - the ordinary condition of a system
    # that has not reconciled - and a loop that is observing really is running.
    # The reconciliation row and the trading-safety row both already say that
    # nothing may submit.
    if (
        account_safety is not None
        and account_safety.available
        and account_safety.state == state.ACCOUNT_SAFETY_UNSAFE_UNKNOWN
        and runtime_state in {"RUNNING", "STALE"}
    ):
        runtime_state, tone = "PAUSED", TONE_NEGATIVE
        detail = (
            "Held by the shared account safety halt: an order with an unresolved "
            "broker outcome stops every service on this account, not only the one "
            "that hit it."
        )

    reconciliation = snapshot.reconciliation
    if reconciliation is None:
        safety, safety_tone = "UNRESOLVED", TONE_ATTENTION
        safety_detail = "No reconciliation pass has completed, so nothing has cleared trading."
    elif reconciliation.safe_to_trade:
        safety, safety_tone = "SAFE", TONE_POSITIVE
        safety_detail = f"Latest reconciliation is {reconciliation.status}."
    else:
        safety, safety_tone = "UNSAFE", TONE_NEGATIVE
        safety_detail = f"Latest reconciliation is {reconciliation.status}."

    return RuntimePanel(
        key=key,
        label=label,
        state=runtime_state,
        tone=tone,
        detail=detail,
        started_at=_iso(started_at),
        ended_at=_iso(stopped_at),
        startup_safety=safety,
        startup_safety_tone=safety_tone,
        startup_safety_detail=safety_detail,
        paper_execution_enabled=paper_enabled,
        paper_execution_detail=paper_detail,
        last_cycle_at=_iso(last_cycle),
        next_cycle_at=_iso(next_wake_time(now)),
        checkpoints=checkpoints,
    )


def build_runtimes(
    snapshot: StateSnapshot,
    *,
    now: datetime,
    account_safety: AccountSafetyPanel | None = None,
) -> tuple[RuntimePanel, ...]:
    """One panel per service, in a fixed order: crypto first, then equity."""
    return tuple(
        build_runtime(snapshot, now=now, spec=spec, account_safety=account_safety)
        for spec in RUNTIME_SPECS
    )


def build_metrics(snapshot: StateSnapshot, broker: BrokerRead) -> PrimaryMetrics:
    """The four headline numbers, or the honest reason each is missing.

    Equity, cash, and exposure are broker facts and exist only when the broker
    was read. The daily figure needs both halves - live equity and the stored
    UTC-day baseline - and reports `NOT_RECORDED` when today has no baseline
    row rather than measuring against something else.
    """
    reason = _broker_reason(broker)
    if not (broker.ok and broker.account is not None and broker.positions is not None):
        unavailable = Amount.unavailable(reason)
        baseline = (
            Amount.of(float(snapshot.baseline.baseline_equity))
            if snapshot.ok and snapshot.baseline is not None
            else Amount.unavailable(
                UNAVAILABLE_DATABASE_UNREADABLE if not snapshot.ok else UNAVAILABLE_NOT_RECORDED
            )
        )
        return PrimaryMetrics(
            equity=unavailable,
            cash=unavailable,
            daily_pnl=unavailable,
            daily_pnl_fraction=None,
            daily_pnl_baseline=baseline,
            daily_pnl_baseline_date=(
                None
                if not snapshot.ok or snapshot.baseline is None
                else snapshot.baseline.risk_date_utc.isoformat()
            ),
            exposure=unavailable,
            exposure_fraction=None,
        )

    account = broker.account
    exposure = sum(
        position.market_value for position in broker.positions.values() if position.market_value > 0
    )

    if snapshot.ok and snapshot.baseline is not None:
        baseline_value = float(snapshot.baseline.baseline_equity)
        baseline = Amount.of(baseline_value)
        baseline_date = snapshot.baseline.risk_date_utc.isoformat()
        pnl = Amount.of(account.equity - baseline_value)
        pnl_fraction = _fraction(account.equity - baseline_value, baseline_value)
    else:
        baseline = Amount.unavailable(
            UNAVAILABLE_DATABASE_UNREADABLE if not snapshot.ok else UNAVAILABLE_NOT_RECORDED
        )
        baseline_date = None
        pnl = Amount.unavailable(baseline.unavailable_reason or UNAVAILABLE_NOT_RECORDED)
        pnl_fraction = None

    return PrimaryMetrics(
        equity=Amount.of(account.equity),
        cash=Amount.of(account.cash),
        daily_pnl=pnl,
        daily_pnl_fraction=pnl_fraction,
        daily_pnl_baseline=baseline,
        daily_pnl_baseline_date=baseline_date,
        exposure=Amount.of(exposure),
        exposure_fraction=_fraction(exposure, account.equity),
    )


def _limit(
    *,
    key: str,
    label: str,
    limit_fraction: float,
    basis: Amount,
    used: float | None,
    used_fraction: float | None,
    used_reason: str,
    subject: str | None = None,
    detail: str | None = None,
) -> RiskLimit:
    """One policy limit plus its utilization, with unavailability preserved.

    `basis` is what the limit is a fraction *of* - equity for the exposure
    caps, the UTC-day baseline for the loss halt - and `used_reason` is why the
    observation is missing when it is. They are separate because they fail
    separately: a stored baseline with no readable equity yields a limit in
    dollars and no utilization, and saying "not recorded" there would name the
    wrong missing thing.
    """
    limit_value = (
        Amount.of(basis.value * limit_fraction)
        if basis.available and basis.value is not None
        else Amount.unavailable(basis.unavailable_reason or UNAVAILABLE_NOT_RECORDED)
    )
    used_amount = Amount.of(used) if used is not None else Amount.unavailable(used_reason)
    utilization = None if used_fraction is None else _fraction(used_fraction, limit_fraction)
    return RiskLimit(
        key=key,
        label=label,
        limit_fraction=limit_fraction,
        limit_value=limit_value,
        used_value=used_amount,
        used_fraction=used_fraction,
        utilization=utilization,
        breached=bool(used_fraction is not None and used_fraction > limit_fraction),
        subject=subject,
        detail=detail,
    )


def build_risk(metrics: PrimaryMetrics, broker: BrokerRead) -> RiskPanel:
    """The established V0.2 limits, and how much of each is currently used.

    The limits are read from `autotrader.risk.engine`, which is the module that
    enforces them, so this screen cannot drift from the policy it describes.
    They are always shown - a limit does not stop existing because the account
    behind it is unreadable - while utilization is an observation and carries
    its own unavailable state.

    There is deliberately no separate crypto **limit** and no separate equity
    limit: the risk engine has one set of limits and inventing a second would
    put a number on this screen that nothing enforces. The crypto/equity split
    that `build_exposure` returns alongside these is a *breakdown* of the one
    total, carried in its own field and flagged `enforced=False`, so the two
    can never be confused for each other.
    """
    equity = metrics.equity
    largest_symbol: str | None = None
    largest_exposure: float | None = None
    if broker.ok and broker.positions is not None:
        held = [position for position in broker.positions.values() if position.market_value > 0]
        if held:
            top = max(held, key=lambda position: position.market_value)
            largest_symbol, largest_exposure = top.symbol, top.market_value
        else:
            largest_exposure = 0.0

    symbol_fraction = (
        None
        if largest_exposure is None or not equity.available or not equity.value
        else _fraction(largest_exposure, equity.value)
    )

    loss = None if metrics.daily_pnl.value is None else max(0.0, -metrics.daily_pnl.value)
    loss_fraction = (
        None
        if loss is None
        or not metrics.daily_pnl_baseline.available
        or not metrics.daily_pnl_baseline.value
        else _fraction(loss, metrics.daily_pnl_baseline.value)
    )

    broker_reason = _broker_reason(broker)
    limits = (
        _limit(
            key="position",
            label="Per-symbol exposure",
            limit_fraction=MAX_POSITION_FRACTION,
            basis=equity,
            used=largest_exposure,
            used_fraction=symbol_fraction,
            used_reason=broker_reason,
            subject=largest_symbol,
            detail="Largest single-symbol market value against equity.",
        ),
        _limit(
            key="total_exposure",
            label="Total exposure",
            limit_fraction=MAX_TOTAL_EXPOSURE_FRACTION,
            basis=equity,
            used=metrics.exposure.value,
            used_fraction=metrics.exposure_fraction,
            used_reason=broker_reason,
            detail="Aggregate long market value against equity.",
        ),
        _limit(
            key="daily_loss",
            label="UTC daily loss halt",
            limit_fraction=MAX_DAILY_LOSS_FRACTION,
            basis=metrics.daily_pnl_baseline,
            used=loss,
            used_fraction=loss_fraction,
            used_reason=metrics.daily_pnl.unavailable_reason or broker_reason,
            detail="Loss against the stored UTC-day baseline equity.",
        ),
    )
    available = all(limit.used_value.available for limit in limits)
    reason = next(
        (limit.used_value.unavailable_reason for limit in limits if not limit.used_value.available),
        None,
    )
    return RiskPanel(
        limits=limits,
        exposure=build_exposure(metrics, broker),
        total_exposure_limit_fraction=MAX_TOTAL_EXPOSURE_FRACTION,
        available=available,
        unavailable_reason=reason,
    )


def build_exposure(metrics: PrimaryMetrics, broker: BrokerRead) -> tuple[ExposureRow, ...]:
    """Total account exposure, split by book. **A display breakdown, not limits.**

    One account holds both books and one 30% cap covers both, so this answers
    "where is the exposure?" and never "how much may each book have?". No
    per-book allocation exists in the risk engine, and none is implied here:
    every row but the total carries `enforced=False`, and only the total row is
    paired with the cap that actually exists.

    The split is by the symbol's own notation, the same rule the rest of this
    package uses - a quoted pair is crypto, a bare ticker is not - and it is
    summed from the broker's positions, so the two rows necessarily add up to
    the total rather than being three independently sourced numbers.
    """
    if not broker.ok or broker.positions is None:
        reason = _broker_reason(broker)
        unavailable = Amount.unavailable(reason)
        return (
            ExposureRow(key="crypto", label="Crypto", value=unavailable),
            ExposureRow(key="equity", label="Equity", value=unavailable),
            ExposureRow(key="total", label="Total", value=unavailable, enforced=True),
        )

    totals = {ASSET_CLASS_CRYPTO: 0.0, ASSET_CLASS_EQUITY: 0.0}
    for position in broker.positions.values():
        if position.market_value > 0:
            totals[asset_class_for(position.symbol)] += position.market_value
    combined = totals[ASSET_CLASS_CRYPTO] + totals[ASSET_CLASS_EQUITY]

    equity = metrics.equity

    def row(key: str, label: str, value: float, *, enforced: bool = False) -> ExposureRow:
        return ExposureRow(
            key=key,
            label=label,
            value=Amount.of(value),
            fraction=(
                None if not equity.available or not equity.value else _fraction(value, equity.value)
            ),
            enforced=enforced,
        )

    return (
        row("crypto", "Crypto", totals[ASSET_CLASS_CRYPTO]),
        row("equity", "Equity", totals[ASSET_CLASS_EQUITY]),
        row("total", "Total", combined, enforced=True),
    )


def build_account_safety(snapshot: StateSnapshot) -> AccountSafetyPanel:
    """The shared account halt, read from the row both runtimes read.

    Not inferred from what either runtime logged: the halt is a property of the
    account, and the whole reason it is durable is that neither process's own
    view of itself can be trusted to describe it. `UNSAFE_UNKNOWN` carries the
    `client_order_id` an operator needs in order to ask the broker what actually
    happened.
    """
    if not snapshot.ok or snapshot.account_safety is None:
        return AccountSafetyPanel(
            state="UNAVAILABLE",
            tone=TONE_ATTENTION,
            safe_to_trade=False,
            detail="The shared account safety state could not be read.",
            available=False,
            unavailable_reason=UNAVAILABLE_DATABASE_UNREADABLE,
        )

    safety = snapshot.account_safety
    if safety.safe_to_trade:
        tone = TONE_POSITIVE
    elif safety.state == state.ACCOUNT_SAFETY_UNSAFE_UNKNOWN:
        tone = TONE_NEGATIVE
    else:
        tone = TONE_ATTENTION

    return AccountSafetyPanel(
        state=safety.state,
        tone=tone,
        safe_to_trade=safety.safe_to_trade,
        detail=_truncate(safety.reason),
        source=safety.source,
        client_order_id=safety.client_order_id,
        updated_at=_iso(safety.updated_at),
    )


#: How each shared budget is labelled on screen.
_BUDGET_LABELS = {
    state.API_BUDGET_TRADING: "Trading API",
    state.API_BUDGET_MARKET_DATA: "Market data API",
}


def build_api_budget(snapshot: StateSnapshot, *, now: datetime) -> tuple[ApiBudgetRow, ...]:
    """The shared API budgets' usage in the current window.

    Both rows are always shown, including at zero: an empty window is a real
    and common state for a fifteen-minute system, and a row that vanished when
    nothing had been spent would make "no traffic" look like "not metered".

    The limits here are this system's own conservative ceilings, not published
    provider rate limits, and the row is labelled that way on screen.
    """
    if not snapshot.ok:
        return ()

    spent = {window.budget: window for window in snapshot.api_budget}
    window_start = api_budget_module.window_start_for(now)
    rows: list[ApiBudgetRow] = []
    for budget in state.API_BUDGETS:
        limit = api_budget_module.limit_for(budget)
        used = spent[budget].call_count if budget in spent else 0
        remaining = max(0, limit - used)
        rows.append(
            ApiBudgetRow(
                key=budget.lower(),
                label=_BUDGET_LABELS.get(budget, budget),
                used=used,
                limit=limit,
                remaining=remaining,
                window_start=_iso(window_start),
                tone=TONE_ATTENTION if remaining == 0 else TONE_MUTED,
            )
        )
    return tuple(rows)


def build_health(
    snapshot: StateSnapshot,
    broker: BrokerRead,
    reconciliation: ReconciliationPanel,
    runtimes: tuple[RuntimePanel, ...],
    account_safety: AccountSafetyPanel,
) -> tuple[HealthComponent, HealthComponent, tuple[HealthComponent, ...]]:
    """The health panel, plus the database and broker rows the header also uses.

    Returned as `(database, broker, all_components)` so the header does not
    have to search the list by key for the two subsystems it reports on.

    There is one row per runtime and one row for the shared account safety
    state, in that order: the account row is the one that answers "may anything
    trade", and the runtime rows answer "is each service doing its job".
    """
    if snapshot.ok:
        database = HealthComponent(
            key="database",
            label="Database",
            status="CONNECTED",
            tone=TONE_POSITIVE,
            detail=f"SQLite schema v{snapshot.schema_version}, opened read-only.",
        )
    else:
        database = HealthComponent(
            key="database",
            label="Database",
            status="UNAVAILABLE",
            tone=TONE_ATTENTION,
            detail="The operational database could not be opened or read.",
        )

    if broker.ok:
        tradable = broker.tradable
        broker_row = HealthComponent(
            key="broker",
            label="Broker",
            status="CONNECTED" if tradable else "ATTENTION",
            tone=TONE_POSITIVE if tradable else TONE_ATTENTION,
            detail=(
                "Alpaca paper account, read-only."
                if tradable
                else "The paper account is not currently able to trade."
            ),
        )
    elif broker.reason == UNAVAILABLE_BROKER_NOT_CONFIGURED:
        broker_row = HealthComponent(
            key="broker",
            label="Broker",
            status="NOT CONFIGURED",
            tone=TONE_MUTED,
            detail="No Alpaca paper credentials are configured for this process.",
        )
    else:
        broker_row = HealthComponent(
            key="broker",
            label="Broker",
            status="UNAVAILABLE",
            tone=TONE_ATTENTION,
            detail="The Alpaca paper account could not be read.",
        )

    if reconciliation.available and reconciliation.status is not None:
        reconciliation_row = HealthComponent(
            key="reconciliation",
            label="Reconciliation",
            status=reconciliation.status,
            tone=reconciliation.tone,
            detail=(
                f"{reconciliation.orders_checked} orders and "
                f"{reconciliation.positions_checked} positions checked, "
                f"{reconciliation.unresolved} unresolved."
            ),
        )
    elif reconciliation.unavailable_reason == UNAVAILABLE_NOT_RECORDED:
        reconciliation_row = HealthComponent(
            key="reconciliation",
            label="Reconciliation",
            status="NEVER RUN",
            tone=TONE_ATTENTION,
            detail="No reconciliation pass has completed against this database.",
        )
    else:
        reconciliation_row = HealthComponent(
            key="reconciliation",
            label="Reconciliation",
            status="UNAVAILABLE",
            tone=TONE_ATTENTION,
            detail="Reconciliation state could not be read.",
        )

    runtime_rows = tuple(
        HealthComponent(
            key=f"runtime_{runtime.key}",
            label=runtime.label,
            status=runtime.state,
            tone=runtime.tone,
            detail=runtime.detail,
        )
        for runtime in runtimes
    )
    account_row = HealthComponent(
        key="account_safety",
        label="Account safety",
        status=account_safety.state,
        tone=account_safety.tone,
        detail=account_safety.detail,
    )

    if not snapshot.ok:
        safety_row = HealthComponent(
            key="trading_safety",
            label="Trading safety",
            status="UNKNOWN",
            tone=TONE_ATTENTION,
            detail="Trading permission cannot be established without the database.",
        )
    elif not account_safety.safe_to_trade:
        safety_row = HealthComponent(
            key="trading_safety",
            label="Trading safety",
            status="BLOCKED",
            tone=account_safety.tone,
            detail=(
                f"The shared account safety state is {account_safety.state}. No service "
                "on this account may submit."
            ),
        )
    elif reconciliation.safe_to_trade is not True:
        safety_row = HealthComponent(
            key="trading_safety",
            label="Trading safety",
            status="BLOCKED",
            tone=TONE_NEGATIVE if reconciliation.available else TONE_ATTENTION,
            detail=(
                f"Latest reconciliation is {reconciliation.status}."
                if reconciliation.available
                else "No completed reconciliation has cleared trading."
            ),
        )
    elif any(runtime.state == "PAUSED" for runtime in runtimes):
        paused = ", ".join(r.label for r in runtimes if r.state == "PAUSED")
        safety_row = HealthComponent(
            key="trading_safety",
            label="Trading safety",
            status="PAUSED",
            tone=TONE_NEGATIVE,
            detail=f"{paused} paused trading and will not submit again.",
        )
    elif any(runtime.paper_execution_enabled for runtime in runtimes):
        safety_row = HealthComponent(
            key="trading_safety",
            label="Trading safety",
            status="ALLOWED",
            tone=TONE_NEUTRAL,
            detail="Reconciliation cleared trading and the paper submission gate is open.",
        )
    else:
        safety_row = HealthComponent(
            key="trading_safety",
            label="Trading safety",
            status="OBSERVE ONLY",
            tone=TONE_MUTED,
            detail="Reconciliation cleared trading, but the paper submission gate is closed.",
        )

    components = (
        account_row,
        reconciliation_row,
        *runtime_rows,
        broker_row,
        database,
        safety_row,
    )
    return database, broker_row, components


def derive_system_state(
    *,
    snapshot: StateSnapshot,
    broker: BrokerRead,
    reconciliation: ReconciliationPanel,
    runtimes: tuple[RuntimePanel, ...],
    account_safety: AccountSafetyPanel,
    orders: OrdersPanel,
) -> tuple[str, str, tuple[str, ...]]:
    """The header verdict, its tone, and the reasons behind it.

    The rule, written down once:

    ``PAUSED``     trading is durably blocked - the shared account safety state
                   is `UNSAFE_UNKNOWN`, the latest reconciliation is not
                   `safe_to_trade`, or either runtime recorded that it stopped
                   trading. This is the state that means *stop and look*.

                   `UNSAFE_RECONCILIATION` is deliberately **not** here. It is
                   the ordinary state of a system that has not reconciled yet -
                   a fresh database is in it - so it belongs with the other
                   "needs a person" conditions rather than with the ones that
                   mean something has gone wrong.

                   The account halt is listed first because it is the widest of
                   the three: one ambiguous order stops both services, and a
                   header that stayed green while an order of unknown status sat
                   at the broker would be the worst thing this screen could do.
    ``ATTENTION``  something needs a person: an `UNKNOWN` order, an unresolved
                   reconciliation issue, a reconciliation that never ran, a
                   database or broker that cannot be read, a paper account the
                   broker says cannot trade, or a runtime that claims to be
                   running while its checkpoints have gone stale.
    ``HEALTHY``    none of the above.

    A runtime that was stopped cleanly is deliberately none of the three
    problems: it is reported as `STOPPED` in the health panel, and a header
    that shouted about every intentional shutdown would be a header nobody
    reads.
    """
    reasons: list[str] = []

    if account_safety.available and account_safety.state == state.ACCOUNT_SAFETY_UNSAFE_UNKNOWN:
        anchor = (
            "" if account_safety.client_order_id is None else f" ({account_safety.client_order_id})"
        )
        reasons.append(
            f"Account safety {account_safety.state}{anchor} - an order's broker outcome "
            "is unresolved and no service on this account may submit."
        )
    if reconciliation.available and reconciliation.safe_to_trade is False:
        reasons.append(f"Reconciliation {reconciliation.status} - trading is not cleared.")
    for runtime in runtimes:
        if runtime.state == "PAUSED":
            reasons.append(f"{runtime.label} paused trading and will not submit again.")
    if reasons:
        return SYSTEM_PAUSED, TONE_NEGATIVE, tuple(reasons)

    if orders.attention_count:
        plural = "s" if orders.attention_count > 1 else ""
        reasons.append(
            f"{orders.attention_count} order{plural} in UNKNOWN - the broker outcome "
            "was never established."
        )
    if reconciliation.available and (reconciliation.unresolved or 0) > 0:
        reasons.append(f"{reconciliation.unresolved} unresolved reconciliation issue(s).")
    if not reconciliation.available:
        reasons.append(
            "No completed reconciliation."
            if reconciliation.unavailable_reason == UNAVAILABLE_NOT_RECORDED
            else "Reconciliation state could not be read."
        )
    if not snapshot.ok:
        reasons.append("The operational database could not be read.")
    if not account_safety.available:
        reasons.append("The shared account safety state could not be read.")
    elif not account_safety.safe_to_trade and (
        account_safety.state != state.ACCOUNT_SAFETY_UNSAFE_UNKNOWN
    ):
        # Nothing has cleared trading yet. That needs a person, but it is not
        # the same thing as an order loose at the broker, and giving it the same
        # colour would make the colour that matters mean less.
        reasons.append(
            f"Account safety {account_safety.state} - no full-universe reconciliation "
            "has established that this account may trade."
        )
    if not broker.ok and broker.reason == UNAVAILABLE_BROKER_UNREADABLE:
        reasons.append("The Alpaca paper account could not be read.")
    if broker.ok and broker.tradable is False:
        reasons.append("The paper account is not currently able to trade.")
    for runtime in runtimes:
        if runtime.state in {"STALE", "FAILED", "UNAVAILABLE"}:
            reasons.append(f"{runtime.label} {runtime.state}.")

    if reasons:
        return SYSTEM_ATTENTION, TONE_ATTENTION, tuple(reasons)
    return SYSTEM_HEALTHY, TONE_POSITIVE, ()


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------


def build_overview(
    *,
    database_path: str | Path,
    now: datetime | None = None,
    broker: BrokerRead | None = None,
    order_limit: int = DEFAULT_ORDER_LIMIT,
) -> Overview:
    """Assemble one dashboard poll. The only entry point this package needs.

    `now` and `broker` are injected so the whole read model is testable without
    a clock or a network, which is how every other layer in this repository is
    tested. Production injects the API's shared reader; a test injects a
    `BrokerRead` it built from a fake client, or one that says the broker could
    not be read at all. With neither supplied, one read is made here.
    """
    moment = now or datetime.now(UTC)
    snapshot = read_state(database_path, now=moment)
    if broker is None:
        broker = read_broker()

    metrics = build_metrics(snapshot, broker)
    positions = build_positions(snapshot, broker)
    orders = build_orders(snapshot, limit=order_limit)
    reconciliation = build_reconciliation(snapshot)
    account_safety = build_account_safety(snapshot)
    runtimes = build_runtimes(snapshot, now=moment, account_safety=account_safety)
    api_budget = build_api_budget(snapshot, now=moment)
    risk = build_risk(metrics, broker)
    database_row, broker_row, health = build_health(
        snapshot, broker, reconciliation, runtimes, account_safety
    )
    system_state, tone, reasons = derive_system_state(
        snapshot=snapshot,
        broker=broker,
        reconciliation=reconciliation,
        runtimes=runtimes,
        account_safety=account_safety,
        orders=orders,
    )

    failure = _latest_failure(snapshot) if snapshot.ok else None

    notices: list[str] = []
    if not broker.ok and broker.reason == UNAVAILABLE_BROKER_NOT_CONFIGURED:
        notices.append(
            "No Alpaca paper credentials are configured, so account equity, cash, "
            "exposure and live positions are unavailable."
        )

    return Overview(
        generated_at=_iso(moment) or "",
        environment=ENVIRONMENT_PAPER,
        system_state=system_state,
        system_state_tone=tone,
        attention=reasons,
        database=database_row,
        broker=broker_row,
        metrics=metrics,
        positions=positions,
        orders=orders,
        health=health,
        reconciliation=reconciliation,
        runtimes=runtimes,
        account_safety=account_safety,
        api_budget=api_budget,
        last_failure=(
            None if failure is None else _truncate(failure.message or failure.event_type)
        ),
        last_failure_at=None if failure is None else _iso(failure.event_timestamp),
        risk=risk,
        notices=tuple(notices),
    )


__all__ = [
    "DEFAULT_ORDER_LIMIT",
    "READ_TIMEOUT_SECONDS",
    "STALE_AFTER",
    "StateSnapshot",
    "asset_class_for",
    "build_health",
    "build_metrics",
    "build_orders",
    "build_overview",
    "build_positions",
    "build_account_safety",
    "build_api_budget",
    "build_exposure",
    "build_reconciliation",
    "build_risk",
    "build_runtime",
    "build_runtimes",
    "derive_system_state",
    "models",
    "read_only_connection",
    "read_state",
]
