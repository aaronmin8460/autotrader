"""The Equity Shadow read model: V3 and EDA-1, side by side, observation only.

This module reads the shadow database and nothing else. It opens no broker
connection, imports nothing from the execution layer, and holds no credential -
the shadow's own runtime already refuses to hold an execution path, and the
viewer of that record has even less business owning one.

**What the numbers on this page are.** Every figure derived here is
*hypothetical*. The shadow submits no order, holds no position and has no
account; the portfolio curves below are what an equal-weight book would have
done had it followed each engine's recorded stance, compounded from a
normalized 100 and charged **no commission, no spread and no slippage**. They
must never be rendered beside broker account equity, and `HYPOTHETICAL_LABEL`
travels with them so the frontend cannot forget to say so.

**Where the causality lives.** A stance recorded against bar *t* is applied to
the return realized from *t* to *t+1*, never to the bar it was decided on.
That is the only ordering the stored record supports, and it is the ordering a
tradable rule would have had.

**On EDA-1's score and confidence.** The overlay copies V3's score and
confidence verbatim - it is a participation router, not a second probability
model - so this module labels them `SCORE_COPIED_FROM_V3` rather than
presenting them as an independent opinion. A confidence EDA-1 does not have is
not invented here.

**Small samples say small things.** Capture ratios are withheld below
`MIN_STEPS_FOR_CAPTURE` and every metrics payload carries a `sample_warning`.
No annualized figure - Sharpe included - is computed at any sample size,
because there is no honest way to annualize a few sessions of a shadow that
has not yet seen a second regime.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

#: Where the shadow keeps its record. A path only - never a connection string,
#: and never anything with a credential in it. Deliberately a different
#: variable from `AUTOTRADER_DASHBOARD_DB`: pointing this reader at the
#: trading database would be pointing it at operational truth, and the two
#: records are kept apart on purpose.
SHADOW_DATABASE_PATH_ENV = "AUTOTRADER_EQUITY_SHADOW_DB"

DEFAULT_SHADOW_DATABASE_PATH = Path("data/autotrader-shadow.db")

#: Seconds a read may wait on a database the shadow runtime is writing. Short:
#: a viewer that blocks is a viewer that is lying about the present.
READ_TIMEOUT_SECONDS = 5.0

#: The ten symbols, in the order the shadow processes them. Rendering order is
#: this order so two screenshots taken minutes apart line up row for row.
UNIVERSE: tuple[str, ...] = (
    "SPY",
    "QQQ",
    "IWM",
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
)

ENGINE_V3 = "v3"
ENGINE_EDA1 = "eda1"

#: What this page is, in one machine string the frontend renders verbatim.
SHADOW_MODE = "V3 + EDA-1 SIDE-BY-SIDE SHADOW"

#: The three words that must never be missing from this page.
HYPOTHETICAL_LABEL = "SIMULATED / SHADOW - NO REAL ORDERS"

#: Broker mutation, stated as a value rather than left implied by absence.
BROKER_MUTATION_DISABLED = "DISABLED"

#: What `shadow_decisions.designation = 'EXECUTED'` means in *this* database,
#: spelled out on the wire so no reader has to infer it from the word.
#:
#: The decision-shadow schema uses that designation for "the panel judged this
#: decision actionable and released it downstream" - it records the release,
#: not an order. The equity shadow runtime is the whole of what is downstream,
#: and it drops the candidate: no risk pass runs, no intent is created, and
#: `client_order_id` stays NULL on every row forever. A dashboard that counted
#: these as orders would raise an alarm on the system working exactly as
#: designed, which is the failure mode that teaches an operator to ignore
#: alarms.
RELEASED_CANDIDATE_MEANING = (
    "Decisions V3 judged actionable and the panel released. NOT orders: this "
    "process has no execution path, every candidate is dropped with "
    "SHADOW_HAS_NO_EXECUTION_PATH, and client_order_id is NULL on every row."
)

#: The shadow's journal heartbeat carries `startup_safety=UNRESOLVED` and
#: `reconciliation_status=-`, and an operator reading it deserves to know why
#: before they open an incident.
#:
#: Both fields belong to the *trading* runtimes. Those run a reconciliation
#: pass at startup and record whether the result permits order submission;
#: `UNRESOLVED` is the pre-answer default of that question. The shadow never
#: asks it - there is no submission for a safety verdict to gate - so both
#: fields keep their defaults for the life of the process.
#:
#: "Not applicable" and "unresolved" are very different claims, and the
#: heartbeat currently prints the second while meaning the first. Correcting
#: that at source is a one-line change to the shadow runtime, and it is
#: deliberately NOT made here: the deployed observer is pinned bit-identical
#: to the validated SHA, and re-cutting that pin to improve a log cosmetic is
#: the wrong trade. The dashboard states the true semantics instead, which is
#: where an operator actually looks.
STARTUP_SAFETY_NOTE = (
    "Not applicable. `startup_safety=UNRESOLVED` and `reconciliation_status=-` in this "
    "process's heartbeat are the trading runtimes' pre-answer defaults: those runtimes "
    "reconcile at startup to decide whether submission is safe. This process never asks "
    "that question because it has no execution path to authorize, so both fields keep "
    "their defaults. It is not an unresolved safety check."
)

# --------------------------------------------------------------------------
# Service status vocabulary
# --------------------------------------------------------------------------

#: Cycles arrive on 15-minute bar boundaries during a regular session.
CYCLE_INTERVAL = timedelta(minutes=15)

#: How long a session-hours gap may run before the service reads as stale.
#: Two missed boundaries plus the runtime's own safety delay: one late
#: provider publication is not an outage, two in a row is worth a look.
STALE_AFTER = timedelta(minutes=35)

#: Observing. The last cycle landed when one was due.
SHADOW_RUNNING = "RUNNING"

#: Off-session and correctly quiet. **Not a failure**: outside US regular
#: hours no bar exists to observe, and a dashboard that paints a healthy
#: overnight shadow red teaches its operator to ignore red.
SHADOW_IDLE = "IDLE"

#: In session, a regime state was resolved for today - so the broker's own
#: calendar says the market is open - and cycles have stopped arriving.
SHADOW_STALE = "STALE"

#: The shadow recorded a clean shutdown and has not started again.
SHADOW_STOPPED = "STOPPED"

#: The database could not be read at all.
SHADOW_UNAVAILABLE = "UNAVAILABLE"

SHADOW_STATES: tuple[str, ...] = (
    SHADOW_RUNNING,
    SHADOW_IDLE,
    SHADOW_STALE,
    SHADOW_STOPPED,
    SHADOW_UNAVAILABLE,
)

#: Regular US session in exchange-local time. Held in `America/New_York` so
#: daylight saving is the zone database's problem rather than an arithmetic
#: mistake waiting for March.
MARKET_TIMEZONE = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)

#: Started, stopped, and per-cycle markers the runtime writes.
EVENT_STARTED = "EQUITY_SHADOW_STARTED"
EVENT_STOPPED = "EQUITY_SHADOW_STOPPED"
EVENT_CYCLE = "EQUITY_SHADOW_CYCLE"

#: The runtime prints its provenance into the start event. Read rather than
#: guessed: the SHA on the page is the SHA the recording process was built at.
_CODE_SHA_PATTERN = re.compile(r"\bcode ([0-9a-f]{7,40})\b")

# --------------------------------------------------------------------------
# Hypothetical accounting
# --------------------------------------------------------------------------

#: Where both hypothetical curves start. A normalized index, not a currency:
#: the shadow has no capital and stating one would invite the comparison this
#: page exists to prevent.
NORMALIZED_START = 100.0

#: Below this many observed steps, capture ratios are withheld rather than
#: printed with a caveat nobody reads. Roughly ten sessions of 15-minute bars.
MIN_STEPS_FOR_CAPTURE = 250

#: Below this, even the cumulative figures carry a loud warning.
MIN_STEPS_FOR_CONFIDENCE = 1000

SAMPLE_WARNING_TEXT = (
    "Shadow sample is far too small for any performance conclusion. "
    "The pre-registered evaluation needs months spanning multiple regimes; "
    "no winner may be declared from this record."
)

#: Why EDA-1's score column is not its own opinion.
SCORE_COPIED_FROM_V3 = "COPIED_FROM_V3"

# --------------------------------------------------------------------------
# Reasons a panel can be empty
# --------------------------------------------------------------------------

UNAVAILABLE_DATABASE_UNREADABLE = "DATABASE_UNREADABLE"
UNAVAILABLE_NO_DECISIONS = "NO_DECISIONS_RECORDED"
UNAVAILABLE_SAMPLE_TOO_SMALL = "SAMPLE_TOO_SMALL"


# ==========================================================================
# The database read
# ==========================================================================


@contextmanager
def read_only_connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open `path` read-only and close it on exit.

    `mode=ro` makes every write an engine-level error rather than a
    convention, and `query_only` closes the same door from the other side.
    No `PRAGMA journal_mode` is issued: setting a journal mode writes to the
    database header, and a viewer has no business touching the journalling of
    a database another process owns.
    """
    uri = f"file:{quote(str(Path(path).resolve()))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=READ_TIMEOUT_SECONDS, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = 1")
        yield connection
    finally:
        connection.close()


def database_path() -> Path:
    """Where to read the shadow record from."""
    configured = os.environ.get(SHADOW_DATABASE_PATH_ENV)
    return Path(configured) if configured else DEFAULT_SHADOW_DATABASE_PATH


def _parse(moment: str | None) -> datetime | None:
    """ISO-8601 text to an aware UTC datetime, or `None` if it is not one."""
    if not moment:
        return None
    try:
        parsed = datetime.fromisoformat(moment)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


@dataclass(frozen=True)
class ShadowSnapshot:
    """One consistent read of the shadow database, or the reason there is none.

    Everything a page needs is materialized inside a single read transaction,
    so the status strip, the symbol table and the curves all describe the same
    instant of the record rather than three instants a few milliseconds apart.
    """

    ok: bool
    reason: str | None = None
    decisions: tuple[sqlite3.Row, ...] = ()
    comparisons: tuple[sqlite3.Row, ...] = ()
    regimes: tuple[sqlite3.Row, ...] = ()
    events: tuple[sqlite3.Row, ...] = ()
    order_intent_count: int = 0
    released_candidate_count: int = 0
    linked_order_count: int = 0


def read_shadow(path: str | Path) -> ShadowSnapshot:
    """Read everything one poll needs, in one short read transaction.

    Any failure to read - a missing file, a database locked beyond
    `READ_TIMEOUT_SECONDS`, a schema this reader does not recognize - returns
    `ok=False` rather than raising. There is deliberately no repair path and
    no schema creation: a viewer that would build the database it is reading
    is a viewer that has written to it.
    """
    try:
        with read_only_connection(path) as connection:
            connection.execute("BEGIN DEFERRED")
            comparisons = tuple(
                connection.execute(
                    "SELECT symbol, bar_timestamp, session_date, participate, "
                    "       v3_signal, v3_stance, eda1_signal, eda1_stance, "
                    "       signals_agree, stances_agree, reference_close, recorded_at "
                    "FROM shadow_side_by_side ORDER BY bar_timestamp, symbol"
                ).fetchall()
            )
            decisions = tuple(
                connection.execute(
                    "SELECT symbol, bar_timestamp, engine_version, signal, score, "
                    "       confidence, regime, reasons, designation, created_at "
                    "FROM shadow_decisions ORDER BY bar_timestamp, symbol, engine_version"
                ).fetchall()
            )
            regimes = tuple(
                connection.execute(
                    "SELECT session_date, participate, info_close, info_sma, "
                    "       info_drawdown, sessions_observed, sma_sessions, "
                    "       calm_threshold, lag_sessions, reference_symbol, computed_at "
                    "FROM shadow_regime_state ORDER BY session_date"
                ).fetchall()
            )
            events = tuple(
                connection.execute(
                    "SELECT event_timestamp, event_type, message "
                    "FROM system_events ORDER BY id DESC LIMIT 500"
                ).fetchall()
            )
            # The zero-order invariant, read as evidence rather than asserted
            # as a label. If either of these is ever non-zero the page says so
            # in red instead of printing a reassuring constant.
            intents = int(connection.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0])
            linked = int(
                connection.execute(
                    "SELECT COUNT(*) FROM shadow_decisions WHERE client_order_id IS NOT NULL"
                ).fetchone()[0]
            )
            # NOT part of the invariant - see `ServicePanel.released_candidates`.
            # In the decision-shadow schema this designation means the panel
            # released an actionable candidate downstream, not that an order
            # followed. In this process nothing is downstream.
            released = int(
                connection.execute(
                    "SELECT COUNT(*) FROM shadow_decisions WHERE designation = 'EXECUTED'"
                ).fetchone()[0]
            )
            connection.execute("COMMIT")
    except (sqlite3.Error, OSError, ValueError):
        return ShadowSnapshot(ok=False, reason=UNAVAILABLE_DATABASE_UNREADABLE)

    return ShadowSnapshot(
        ok=True,
        decisions=decisions,
        comparisons=comparisons,
        regimes=regimes,
        events=events,
        order_intent_count=intents,
        released_candidate_count=released,
        linked_order_count=linked,
    )


# ==========================================================================
# Session semantics
# ==========================================================================


def session_date_for(moment: datetime) -> str:
    """The exchange-local calendar date `moment` falls on."""
    return moment.astimezone(MARKET_TIMEZONE).date().isoformat()


def within_regular_session(moment: datetime) -> bool:
    """Is `moment` inside a weekday 09:30-16:00 New York window?

    A necessary condition, never a sufficient one. It knows nothing about
    holidays, and callers must pair it with the broker-derived evidence in
    `session_confirmed_open` before treating a quiet shadow as a broken one.
    """
    local = moment.astimezone(MARKET_TIMEZONE)
    if local.weekday() >= 5:  # noqa: PLR2004 - Saturday and Sunday
        return False
    return SESSION_OPEN <= local.time() < SESSION_CLOSE


def session_confirmed_open(snapshot: ShadowSnapshot, *, now: datetime) -> bool:
    """Has the shadow itself resolved a regime state for today's session?

    This is the broker's calendar, arriving by the only route that does not
    require this reader to hold a credential. The runtime resolves exactly one
    regime state per session, before any decision in it, and only after the
    broker's own calendar has said the session exists - so a row for today is
    the broker confirming the market is open, and the absence of one on a
    holiday is why this page does not cry outage every Thanksgiving.
    """
    today = session_date_for(now)
    return any(str(row["session_date"]) == today for row in snapshot.regimes)


# ==========================================================================
# Panels
# ==========================================================================


@dataclass(frozen=True)
class ServicePanel:
    """Is the observer observing, and is it still incapable of trading?"""

    status: str
    status_reason: str
    mode: str
    universe: tuple[str, ...]
    symbols_recorded_last_cycle: int
    last_cycle_at: str | None
    next_expected_cycle_at: str | None
    seconds_since_last_cycle: float | None
    cycles_recorded: int
    code_sha: str | None
    started_at: str | None
    last_error: str | None
    session_confirmed_open: bool
    within_regular_session: bool
    stale_after_seconds: float
    # The invariant, as measured
    broker_mutation: str
    orders_submitted: int
    order_intents_in_database: int
    linked_orders_in_database: int
    zero_order_invariant_holds: bool
    #: Decisions the panel judged actionable and released downstream. **Not an
    #: order and not a violation.** In this process there is nothing
    #: downstream: the candidate is dropped with `SHADOW_HAS_NO_EXECUTION_PATH`
    #: and `client_order_id` stays NULL forever, which is why the invariant
    #: above counts intents and linked orders and deliberately does not count
    #: this. It is shown because it is the interesting number - how often V3
    #: would have traded had this been the production runtime.
    released_candidates: int
    released_candidates_meaning: str
    #: The two heartbeat fields an operator will see reading the journal, and
    #: what they actually mean here. See `STARTUP_SAFETY_NOTE`.
    startup_safety_applicable: bool
    startup_safety_note: str


@dataclass(frozen=True)
class RegimePanel:
    """EDA-1's participation state and the exact information that produced it."""

    session_date: str | None
    state: str | None
    participate: bool | None
    reference_symbol: str | None
    info_close: float | None
    info_sma: float | None
    info_drawdown: float | None
    sessions_observed: int | None
    sma_sessions: int | None
    calm_threshold: float | None
    lag_sessions: int | None
    computed_at: str | None
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class SymbolRow:
    """One symbol's latest recorded bar, both engines side by side."""

    symbol: str
    bar_timestamp: str | None
    reference_close: float | None
    v3_signal: str | None
    v3_score: float | None
    v3_confidence: float | None
    v3_regime: str | None
    v3_reasons: tuple[str, ...]
    v3_stance: int | None
    eda1_signal: str | None
    eda1_regime: str | None
    eda1_reasons: tuple[str, ...]
    eda1_stance: int | None
    #: EDA-1 is a participation router, not a probability model. Its score and
    #: confidence are V3's, copied - so they are labelled, not re-printed as a
    #: second opinion.
    eda1_score_source: str
    signals_agree: bool | None
    stances_agree: bool | None
    participate: bool | None


@dataclass(frozen=True)
class EngineHypothetical:
    """One engine's hypothetical, frictionless, equal-weight book."""

    engine: str
    portfolio_value: float | None
    cumulative_return: float | None
    max_drawdown: float | None
    long_exposure_fraction: float | None
    stance_changes: int
    turnover_per_step: float | None
    current_long_symbols: tuple[str, ...]
    current_stance_summary: str


@dataclass(frozen=True)
class HypotheticalPanel:
    """Both books, plus the label that must travel with them."""

    label: str
    normalized_start: float
    steps: int
    first_bar: str | None
    last_bar: str | None
    costs_applied: bool
    v3: EngineHypothetical | None
    eda1: EngineHypothetical | None
    benchmark_return: float | None
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class ComparisonPanel:
    """How the two engines have differed, and how little that yet means."""

    bars_compared: int
    steps: int
    agreement_count: int
    disagreement_count: int
    agreement_fraction: float | None
    stance_disagreement_count: int
    participate_bars: int
    defensive_bars: int
    participate_sessions: int
    defensive_sessions: int
    regime_transitions: int
    up_capture: float | None
    down_capture: float | None
    capture_unavailable_reason: str | None
    sample_warning: str
    sample_is_sufficient: bool
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class HistoryRow:
    """One recorded comparison, for the paged history view."""

    bar_timestamp: str
    symbol: str
    session_date: str
    participate: bool
    v3_signal: str
    v3_stance: int
    eda1_signal: str
    eda1_stance: int
    signals_agree: bool
    stances_agree: bool
    reference_close: float


@dataclass(frozen=True)
class HistoryPage:
    """A bounded window of history. There is no unbounded query on this API."""

    rows: tuple[HistoryRow, ...]
    limit: int
    offset: int
    total: int
    returned: int


@dataclass(frozen=True)
class EquityShadowOverview:
    """One poll of the Equity Shadow page."""

    generated_at: str
    read_only: bool
    observation_only: bool
    hypothetical_label: str
    service: ServicePanel
    regime: RegimePanel
    symbols: tuple[SymbolRow, ...]
    hypothetical: HypotheticalPanel
    comparison: ComparisonPanel


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _latest_event(snapshot: ShadowSnapshot, event_type: str) -> sqlite3.Row | None:
    """The newest event of `event_type`. `events` is already newest-first."""
    for row in snapshot.events:
        if str(row["event_type"]) == event_type:
            return row
    return None


def build_service(snapshot: ShadowSnapshot, *, now: datetime) -> ServicePanel:
    """Whether the observer is observing - and whether it is still harmless."""
    intents = snapshot.order_intent_count
    linked = snapshot.linked_order_count
    released = snapshot.released_candidate_count
    invariant = snapshot.ok and intents == 0 and linked == 0

    if not snapshot.ok:
        return ServicePanel(
            status=SHADOW_UNAVAILABLE,
            status_reason=snapshot.reason or UNAVAILABLE_DATABASE_UNREADABLE,
            mode=SHADOW_MODE,
            universe=UNIVERSE,
            symbols_recorded_last_cycle=0,
            last_cycle_at=None,
            next_expected_cycle_at=None,
            seconds_since_last_cycle=None,
            cycles_recorded=0,
            code_sha=None,
            started_at=None,
            last_error=None,
            session_confirmed_open=False,
            within_regular_session=within_regular_session(now),
            stale_after_seconds=STALE_AFTER.total_seconds(),
            broker_mutation=BROKER_MUTATION_DISABLED,
            orders_submitted=0,
            order_intents_in_database=intents,
            linked_orders_in_database=linked,
            zero_order_invariant_holds=False,
            released_candidates=released,
            released_candidates_meaning=RELEASED_CANDIDATE_MEANING,
            startup_safety_applicable=False,
            startup_safety_note=STARTUP_SAFETY_NOTE,
        )

    cycle_events = [row for row in snapshot.events if str(row["event_type"]) == EVENT_CYCLE]
    last_cycle = _parse(cycle_events[0]["event_timestamp"]) if cycle_events else None

    started_row = _latest_event(snapshot, EVENT_STARTED)
    stopped_row = _latest_event(snapshot, EVENT_STOPPED)
    started_at = _parse(started_row["event_timestamp"]) if started_row else None
    stopped_at = _parse(stopped_row["event_timestamp"]) if stopped_row else None

    code_sha: str | None = None
    if started_row is not None:
        match = _CODE_SHA_PATTERN.search(str(started_row["message"] or ""))
        code_sha = match.group(1) if match else None

    in_session = within_regular_session(now)
    confirmed = session_confirmed_open(snapshot, now=now)

    since = (now - last_cycle).total_seconds() if last_cycle is not None else None
    next_expected: datetime | None = None
    if last_cycle is not None and in_session:
        next_expected = last_cycle + CYCLE_INTERVAL

    # Order matters. A clean shutdown outranks a quiet clock, and the
    # invariant outranks everything: a shadow that grew an order intent is not
    # "running", whatever its cycle timer says.
    if not invariant:
        status = SHADOW_STALE
        reason = "ZERO_ORDER_INVARIANT_VIOLATED"
    elif stopped_at is not None and (started_at is None or stopped_at > started_at):
        status = SHADOW_STOPPED
        reason = "CLEAN_SHUTDOWN_RECORDED"
    elif last_cycle is None:
        status = SHADOW_IDLE if not confirmed else SHADOW_RUNNING
        reason = "NO_CYCLE_RECORDED_YET"
    elif since is not None and since <= STALE_AFTER.total_seconds():
        status = SHADOW_RUNNING
        reason = "CYCLE_WITHIN_EXPECTED_INTERVAL"
    elif in_session and confirmed:
        status = SHADOW_STALE
        reason = "NO_CYCLE_DURING_CONFIRMED_OPEN_SESSION"
    else:
        # Off-session, or a day the broker's calendar never opened. Quiet is
        # correct here and must not read as broken.
        status = SHADOW_IDLE
        reason = "OFF_SESSION_NO_BARS_EXPECTED"

    last_bar = snapshot.comparisons[-1]["bar_timestamp"] if snapshot.comparisons else None
    recorded_last = sum(1 for row in snapshot.comparisons if row["bar_timestamp"] == last_bar)

    return ServicePanel(
        status=status,
        status_reason=reason,
        mode=SHADOW_MODE,
        universe=UNIVERSE,
        symbols_recorded_last_cycle=recorded_last,
        last_cycle_at=_iso(last_cycle),
        next_expected_cycle_at=_iso(next_expected),
        seconds_since_last_cycle=since,
        cycles_recorded=len(cycle_events),
        code_sha=code_sha,
        started_at=_iso(started_at),
        last_error=None,
        session_confirmed_open=confirmed,
        within_regular_session=in_session,
        stale_after_seconds=STALE_AFTER.total_seconds(),
        broker_mutation=BROKER_MUTATION_DISABLED,
        orders_submitted=0,
        order_intents_in_database=intents,
        linked_orders_in_database=linked,
        zero_order_invariant_holds=invariant,
        released_candidates=released,
        released_candidates_meaning=RELEASED_CANDIDATE_MEANING,
        startup_safety_applicable=False,
        startup_safety_note=STARTUP_SAFETY_NOTE,
    )


def build_regime(snapshot: ShadowSnapshot) -> RegimePanel:
    """The newest participation state, with the closes that decided it."""
    if not snapshot.ok or not snapshot.regimes:
        return RegimePanel(
            session_date=None,
            state=None,
            participate=None,
            reference_symbol=None,
            info_close=None,
            info_sma=None,
            info_drawdown=None,
            sessions_observed=None,
            sma_sessions=None,
            calm_threshold=None,
            lag_sessions=None,
            computed_at=None,
            unavailable_reason=(snapshot.reason if not snapshot.ok else UNAVAILABLE_NO_DECISIONS),
        )

    row = snapshot.regimes[-1]
    participate = bool(row["participate"])
    return RegimePanel(
        session_date=str(row["session_date"]),
        state="PARTICIPATE" if participate else "DEFENSIVE_V3",
        participate=participate,
        reference_symbol=str(row["reference_symbol"]),
        info_close=_maybe_float(row["info_close"]),
        info_sma=_maybe_float(row["info_sma"]),
        info_drawdown=_maybe_float(row["info_drawdown"]),
        sessions_observed=int(row["sessions_observed"]),
        sma_sessions=int(row["sma_sessions"]),
        calm_threshold=float(row["calm_threshold"]),
        lag_sessions=int(row["lag_sessions"]),
        computed_at=str(row["computed_at"]),
    )


def _maybe_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


def _split_reasons(value: object) -> tuple[str, ...]:
    return tuple(str(value or "").split()) if value else ()


def build_symbols(snapshot: ShadowSnapshot) -> tuple[SymbolRow, ...]:
    """The latest recorded bar per symbol, both engines on one row."""
    if not snapshot.ok:
        return ()

    latest_comparison: dict[str, sqlite3.Row] = {}
    for row in snapshot.comparisons:
        latest_comparison[str(row["symbol"])] = row

    latest_decision: dict[tuple[str, str], sqlite3.Row] = {}
    for row in snapshot.decisions:
        latest_decision[(str(row["symbol"]), str(row["engine_version"]))] = row

    rows: list[SymbolRow] = []
    for symbol in UNIVERSE:
        comparison = latest_comparison.get(symbol)
        v3 = latest_decision.get((symbol, ENGINE_V3))
        eda1 = latest_decision.get((symbol, ENGINE_EDA1))
        rows.append(
            SymbolRow(
                symbol=symbol,
                bar_timestamp=str(comparison["bar_timestamp"]) if comparison else None,
                reference_close=(float(comparison["reference_close"]) if comparison else None),
                v3_signal=str(v3["signal"]) if v3 else None,
                v3_score=float(v3["score"]) if v3 else None,
                v3_confidence=float(v3["confidence"]) if v3 else None,
                v3_regime=str(v3["regime"]) if v3 else None,
                v3_reasons=_split_reasons(v3["reasons"]) if v3 else (),
                v3_stance=int(comparison["v3_stance"]) if comparison else None,
                eda1_signal=str(eda1["signal"]) if eda1 else None,
                eda1_regime=str(eda1["regime"]) if eda1 else None,
                eda1_reasons=_split_reasons(eda1["reasons"]) if eda1 else (),
                eda1_stance=int(comparison["eda1_stance"]) if comparison else None,
                eda1_score_source=SCORE_COPIED_FROM_V3,
                signals_agree=bool(comparison["signals_agree"]) if comparison else None,
                stances_agree=bool(comparison["stances_agree"]) if comparison else None,
                participate=bool(comparison["participate"]) if comparison else None,
            )
        )
    return tuple(rows)


# --------------------------------------------------------------------------
# Hypothetical accounting
#
# One transform, used by both the portfolio panel and the comparison metrics,
# so the two cannot disagree about the same record.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Series:
    """Per-step equal-weight returns for both engines and the benchmark."""

    bars: tuple[str, ...]
    v3_steps: tuple[float, ...]
    eda1_steps: tuple[float, ...]
    benchmark_steps: tuple[float, ...]
    v3_changes: int
    eda1_changes: int
    v3_exposure: float | None
    eda1_exposure: float | None


def _build_series(snapshot: ShadowSnapshot) -> _Series | None:
    """Compound each engine's recorded stances into per-step returns.

    A stance recorded against bar *t* earns the return from *t* to *t+1*. The
    book is equal-weight across the symbols observed at *t*; weight a stance
    does not claim sits in cash at zero. No cost of any kind is charged, which
    is why `costs_applied` is `False` on the panel and why these curves are
    an upper bound rather than a forecast.
    """
    if not snapshot.ok or not snapshot.comparisons:
        return None

    by_bar: dict[str, dict[str, sqlite3.Row]] = {}
    for row in snapshot.comparisons:
        by_bar.setdefault(str(row["bar_timestamp"]), {})[str(row["symbol"])] = row

    bars = sorted(by_bar)
    if len(bars) < 2:  # noqa: PLR2004 - one bar yields no realized step
        return None

    v3_steps: list[float] = []
    eda1_steps: list[float] = []
    benchmark_steps: list[float] = []
    v3_changes = 0
    eda1_changes = 0
    v3_long = 0
    eda1_long = 0
    slots = 0

    previous_stance: dict[str, tuple[int, int]] = {}
    for index in range(len(bars) - 1):
        current, following = by_bar[bars[index]], by_bar[bars[index + 1]]
        shared = [symbol for symbol in UNIVERSE if symbol in current and symbol in following]
        if not shared:
            continue

        v3_total = eda1_total = benchmark_total = 0.0
        for symbol in shared:
            start = float(current[symbol]["reference_close"])
            end = float(following[symbol]["reference_close"])
            if start <= 0:
                continue
            step = end / start - 1.0
            v3_stance = int(current[symbol]["v3_stance"])
            eda1_stance = int(current[symbol]["eda1_stance"])
            v3_total += v3_stance * step
            eda1_total += eda1_stance * step
            benchmark_total += step

            previous = previous_stance.get(symbol)
            if previous is not None:
                v3_changes += int(previous[0] != v3_stance)
                eda1_changes += int(previous[1] != eda1_stance)
            previous_stance[symbol] = (v3_stance, eda1_stance)

            v3_long += v3_stance
            eda1_long += eda1_stance
            slots += 1

        width = float(len(shared))
        v3_steps.append(v3_total / width)
        eda1_steps.append(eda1_total / width)
        benchmark_steps.append(benchmark_total / width)

    if not v3_steps:
        return None

    return _Series(
        bars=tuple(bars),
        v3_steps=tuple(v3_steps),
        eda1_steps=tuple(eda1_steps),
        benchmark_steps=tuple(benchmark_steps),
        v3_changes=v3_changes,
        eda1_changes=eda1_changes,
        v3_exposure=(v3_long / slots) if slots else None,
        eda1_exposure=(eda1_long / slots) if slots else None,
    )


def _compound(steps: Sequence[float]) -> tuple[float, float]:
    """Normalized value and maximum drawdown of a step-return series."""
    value = NORMALIZED_START
    peak = NORMALIZED_START
    worst = 0.0
    for step in steps:
        value *= 1.0 + step
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return value, worst


def _engine_hypothetical(
    engine: str,
    steps: Sequence[float],
    *,
    changes: int,
    exposure: float | None,
    long_symbols: tuple[str, ...],
) -> EngineHypothetical:
    value, drawdown = _compound(steps)
    summary = f"LONG {len(long_symbols)}/{len(UNIVERSE)}" if long_symbols else "FLAT (no position)"
    return EngineHypothetical(
        engine=engine,
        portfolio_value=round(value, 4),
        cumulative_return=round(value / NORMALIZED_START - 1.0, 8),
        max_drawdown=round(drawdown, 8),
        long_exposure_fraction=None if exposure is None else round(exposure, 6),
        stance_changes=changes,
        turnover_per_step=(round(changes / len(steps), 6) if steps else None),
        current_long_symbols=long_symbols,
        current_stance_summary=summary,
    )


def _current_long(snapshot: ShadowSnapshot, column: str) -> tuple[str, ...]:
    if not snapshot.comparisons:
        return ()
    last_bar = snapshot.comparisons[-1]["bar_timestamp"]
    return tuple(
        str(row["symbol"])
        for row in snapshot.comparisons
        if row["bar_timestamp"] == last_bar and int(row[column]) == 1
    )


def build_hypothetical(snapshot: ShadowSnapshot) -> HypotheticalPanel:
    """Both hypothetical books, labelled as simulation on every payload."""
    series = _build_series(snapshot)
    if series is None:
        return HypotheticalPanel(
            label=HYPOTHETICAL_LABEL,
            normalized_start=NORMALIZED_START,
            steps=0,
            first_bar=None,
            last_bar=None,
            costs_applied=False,
            v3=None,
            eda1=None,
            benchmark_return=None,
            unavailable_reason=(snapshot.reason if not snapshot.ok else UNAVAILABLE_NO_DECISIONS),
        )

    benchmark_value, _ = _compound(series.benchmark_steps)
    return HypotheticalPanel(
        label=HYPOTHETICAL_LABEL,
        normalized_start=NORMALIZED_START,
        steps=len(series.v3_steps),
        first_bar=series.bars[0],
        last_bar=series.bars[-1],
        costs_applied=False,
        v3=_engine_hypothetical(
            ENGINE_V3,
            series.v3_steps,
            changes=series.v3_changes,
            exposure=series.v3_exposure,
            long_symbols=_current_long(snapshot, "v3_stance"),
        ),
        eda1=_engine_hypothetical(
            ENGINE_EDA1,
            series.eda1_steps,
            changes=series.eda1_changes,
            exposure=series.eda1_exposure,
            long_symbols=_current_long(snapshot, "eda1_stance"),
        ),
        benchmark_return=round(benchmark_value / NORMALIZED_START - 1.0, 8),
    )


def _capture(
    engine_steps: Sequence[float], benchmark_steps: Sequence[float], *, upside: bool
) -> float | None:
    """Mean engine return over mean benchmark return, on the chosen side."""
    pairs = [
        (engine, benchmark)
        for engine, benchmark in zip(engine_steps, benchmark_steps, strict=True)
        if (benchmark > 0) is upside and benchmark != 0
    ]
    if not pairs:
        return None
    benchmark_mean = sum(benchmark for _, benchmark in pairs) / len(pairs)
    if benchmark_mean == 0:
        return None
    engine_mean = sum(engine for engine, _ in pairs) / len(pairs)
    return round(engine_mean / benchmark_mean, 6)


def build_comparison(snapshot: ShadowSnapshot) -> ComparisonPanel:
    """Agreement, regime behaviour, and capture when the sample allows it."""
    if not snapshot.ok or not snapshot.comparisons:
        return ComparisonPanel(
            bars_compared=0,
            steps=0,
            agreement_count=0,
            disagreement_count=0,
            agreement_fraction=None,
            stance_disagreement_count=0,
            participate_bars=0,
            defensive_bars=0,
            participate_sessions=0,
            defensive_sessions=0,
            regime_transitions=0,
            up_capture=None,
            down_capture=None,
            capture_unavailable_reason=UNAVAILABLE_SAMPLE_TOO_SMALL,
            sample_warning=SAMPLE_WARNING_TEXT,
            sample_is_sufficient=False,
            unavailable_reason=(snapshot.reason if not snapshot.ok else UNAVAILABLE_NO_DECISIONS),
        )

    rows = snapshot.comparisons
    agree = sum(1 for row in rows if int(row["signals_agree"]) == 1)
    stance_disagree = sum(1 for row in rows if int(row["stances_agree"]) == 0)
    participate_bars = sum(1 for row in rows if int(row["participate"]) == 1)

    ordered_sessions = [bool(row["participate"]) for row in snapshot.regimes]
    transitions = sum(
        1
        for previous, current in zip(ordered_sessions, ordered_sessions[1:], strict=False)
        if previous != current
    )

    series = _build_series(snapshot)
    steps = len(series.v3_steps) if series else 0
    sufficient = steps >= MIN_STEPS_FOR_CAPTURE

    up = down = None
    capture_reason: str | None = UNAVAILABLE_SAMPLE_TOO_SMALL
    if series is not None and sufficient:
        up = _capture(series.eda1_steps, series.benchmark_steps, upside=True)
        down = _capture(series.eda1_steps, series.benchmark_steps, upside=False)
        capture_reason = None

    return ComparisonPanel(
        bars_compared=len(rows),
        steps=steps,
        agreement_count=agree,
        disagreement_count=len(rows) - agree,
        agreement_fraction=round(agree / len(rows), 6) if rows else None,
        stance_disagreement_count=stance_disagree,
        participate_bars=participate_bars,
        defensive_bars=len(rows) - participate_bars,
        participate_sessions=sum(1 for state in ordered_sessions if state),
        defensive_sessions=sum(1 for state in ordered_sessions if not state),
        regime_transitions=transitions,
        up_capture=up,
        down_capture=down,
        capture_unavailable_reason=capture_reason,
        sample_warning=SAMPLE_WARNING_TEXT,
        sample_is_sufficient=steps >= MIN_STEPS_FOR_CONFIDENCE,
    )


#: Bounds on the history query. There is no way to ask this API for the whole
#: table: an unbounded range is a denial-of-service endpoint with a friendly
#: name, and a viewer never needs one.
HISTORY_DEFAULT_LIMIT = 100
HISTORY_MAX_LIMIT = 1000


def build_history(
    snapshot: ShadowSnapshot,
    *,
    limit: int = HISTORY_DEFAULT_LIMIT,
    offset: int = 0,
    symbol: str | None = None,
) -> HistoryPage:
    """A bounded, newest-first window of recorded comparisons."""
    bounded = max(1, min(int(limit), HISTORY_MAX_LIMIT))
    start = max(0, int(offset))

    rows = list(reversed(snapshot.comparisons)) if snapshot.ok else []
    if symbol is not None:
        wanted = symbol.upper()
        rows = [row for row in rows if str(row["symbol"]) == wanted]

    window = rows[start : start + bounded]
    return HistoryPage(
        rows=tuple(
            HistoryRow(
                bar_timestamp=str(row["bar_timestamp"]),
                symbol=str(row["symbol"]),
                session_date=str(row["session_date"]),
                participate=bool(row["participate"]),
                v3_signal=str(row["v3_signal"]),
                v3_stance=int(row["v3_stance"]),
                eda1_signal=str(row["eda1_signal"]),
                eda1_stance=int(row["eda1_stance"]),
                signals_agree=bool(row["signals_agree"]),
                stances_agree=bool(row["stances_agree"]),
                reference_close=float(row["reference_close"]),
            )
            for row in window
        ),
        limit=bounded,
        offset=start,
        total=len(rows),
        returned=len(window),
    )


def build_overview(*, path: str | Path, now: datetime) -> EquityShadowOverview:
    """One consistent read, assembled into one page."""
    snapshot = read_shadow(path)
    return EquityShadowOverview(
        generated_at=now.isoformat(),
        read_only=True,
        observation_only=True,
        hypothetical_label=HYPOTHETICAL_LABEL,
        service=build_service(snapshot, now=now),
        regime=build_regime(snapshot),
        symbols=build_symbols(snapshot),
        hypothetical=build_hypothetical(snapshot),
        comparison=build_comparison(snapshot),
    )


__all__ = [
    "BROKER_MUTATION_DISABLED",
    "CYCLE_INTERVAL",
    "DEFAULT_SHADOW_DATABASE_PATH",
    "ENGINE_EDA1",
    "ENGINE_V3",
    "HISTORY_DEFAULT_LIMIT",
    "HISTORY_MAX_LIMIT",
    "HYPOTHETICAL_LABEL",
    "MIN_STEPS_FOR_CAPTURE",
    "NORMALIZED_START",
    "RELEASED_CANDIDATE_MEANING",
    "SHADOW_DATABASE_PATH_ENV",
    "STARTUP_SAFETY_NOTE",
    "SHADOW_IDLE",
    "SHADOW_MODE",
    "SHADOW_RUNNING",
    "SHADOW_STALE",
    "SHADOW_STATES",
    "SHADOW_STOPPED",
    "SHADOW_UNAVAILABLE",
    "STALE_AFTER",
    "UNIVERSE",
    "ComparisonPanel",
    "EngineHypothetical",
    "EquityShadowOverview",
    "HistoryPage",
    "HistoryRow",
    "HypotheticalPanel",
    "RegimePanel",
    "ServicePanel",
    "ShadowSnapshot",
    "SymbolRow",
    "build_comparison",
    "build_history",
    "build_hypothetical",
    "build_overview",
    "build_regime",
    "build_service",
    "build_symbols",
    "database_path",
    "read_only_connection",
    "read_shadow",
    "session_confirmed_open",
    "session_date_for",
    "within_regular_session",
]
