"""The A1-B U30 Shadow read model: hypothetical target weights, observation only.

This module reads the A1-B shadow database and nothing else. It opens no
broker connection, imports nothing from the execution layer, and holds no
credential. The process it describes holds no execution path either: its
observation table refuses any non-NULL order linkage by CHECK constraint, and
every row it writes carries the designation ``SIMULATED_SHADOW``.

**What the numbers on this page are.** Every figure derived here is
*hypothetical*. The observer submits no order, holds no position and has no
account. Its record is, per completed 15-minute bar and per symbol, the target
weight the frozen A1-B U30 archetype allocation would have held - and the
book compounded below is what that weight series would have done, charged **no
commission, no spread and no slippage**, from a normalized 100. It must never
be rendered beside broker account equity, and `HYPOTHETICAL_LABEL` travels
with it so the frontend cannot forget to say so.

**Where the causality lives.** A target weight recorded against bar *t* is
applied to the return realized from *t* to *t+1*, never to the bar it was
decided on - the same convention as the sibling V3 + EDA-1 shadow, so the two
observers' curves are comparable step for step.

**Small samples say small things.** This observer is days old. The panel
carries a `sample_warning` on every payload and withholds capture ratios below
`MIN_STEPS_FOR_CAPTURE`; no annualized figure is computed at any sample size.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from autotrader.dashboard.equity_shadow import (
    HYPOTHETICAL_LABEL,
    MIN_STEPS_FOR_CAPTURE,
    MIN_STEPS_FOR_CONFIDENCE,
    NORMALIZED_START,
    SHADOW_IDLE,
    SHADOW_RUNNING,
    SHADOW_STALE,
    SHADOW_STOPPED,
    SHADOW_UNAVAILABLE,
    session_date_for,
    within_regular_session,
)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

#: Where the A1-B observer keeps its record. A path only.
A1B_DATABASE_PATH_ENV = "AUTOTRADER_EQUITY_A1B_SHADOW_DB"

DEFAULT_A1B_DATABASE_PATH = Path("data/autotrader-a1b-shadow.db")

READ_TIMEOUT_SECONDS = 5.0

#: What this page is, in one machine string the frontend renders verbatim.
SHADOW_MODE = "A1-B U30 ARCHETYPE ALLOCATION SHADOW"

#: Every observation row carries this designation, and the table refuses any
#: other value by constraint.
DESIGNATION = "SIMULATED_SHADOW"

BROKER_MUTATION_DISABLED = "DISABLED"

#: Cycles arrive on 15-minute bar boundaries during a regular session.
CYCLE_INTERVAL = timedelta(minutes=15)

#: Two missed boundaries plus the runtime's own safety delay.
STALE_AFTER = timedelta(minutes=35)

EVENT_STARTED = "EQUITY_A1B_SHADOW_STARTED"
EVENT_STOPPED = "EQUITY_A1B_SHADOW_STOPPED"

#: The newest bars read for the hypothetical curve. Twenty-six rows per bar,
#: so this is a few weeks of sessions - enough for every figure the page
#: shows, bounded so the read cannot grow without limit.
CURVE_BARS = 2000

#: History bounds. There is no unbounded query on this API.
HISTORY_DEFAULT_LIMIT = 100
HISTORY_MAX_LIMIT = 1000

SAMPLE_WARNING_TEXT = (
    "Shadow sample is far too small for any performance conclusion. The A1-B "
    "observer records hypothetical target weights; months spanning multiple "
    "regimes are needed before a return figure means anything, and no winner "
    "may be declared from this record."
)

UNAVAILABLE_DATABASE_UNREADABLE = "DATABASE_UNREADABLE"
UNAVAILABLE_NO_OBSERVATIONS = "NO_OBSERVATIONS_RECORDED"
UNAVAILABLE_SAMPLE_TOO_SMALL = "SAMPLE_TOO_SMALL"

#: The start event states the policy hash, the mark cadence and the grid
#: anchor in prose. Read rather than guessed.
_POLICY_PATTERN = re.compile(r"Policy ([0-9a-f]{6,64})")
_MARK_PATTERN = re.compile(r"mark grid every (\d+) sessions from (\d{4}-\d{2}-\d{2})")
_CODE_SHA_PATTERN = re.compile(r"\bcode ([0-9a-f]{7,40})\b")


# ==========================================================================
# The database read
# ==========================================================================


@contextmanager
def read_only_connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open `path` read-only and close it on exit. No journal-mode pragma."""
    uri = f"file:{quote(str(Path(path).resolve()))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=READ_TIMEOUT_SECONDS, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = 1")
        yield connection
    finally:
        connection.close()


def database_path() -> Path:
    """Where to read the A1-B record from."""
    configured = os.environ.get(A1B_DATABASE_PATH_ENV)
    return Path(configured) if configured else DEFAULT_A1B_DATABASE_PATH


def _parse(moment: str | None) -> datetime | None:
    if not moment:
        return None
    try:
        parsed = datetime.fromisoformat(moment)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _maybe_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


@dataclass(frozen=True)
class A1BSnapshot:
    """One consistent read of the A1-B database, or the reason there is none."""

    ok: bool
    reason: str | None = None
    observations: tuple[sqlite3.Row, ...] = ()
    stances: tuple[sqlite3.Row, ...] = ()
    marks: tuple[sqlite3.Row, ...] = ()
    regimes: tuple[sqlite3.Row, ...] = ()
    events: tuple[sqlite3.Row, ...] = ()
    checkpoints: tuple[sqlite3.Row, ...] = ()
    observation_count: int = 0
    bar_count: int = 0
    first_bar: str | None = None
    last_bar: str | None = None
    order_intent_count: int = 0
    linked_order_count: int = 0
    non_simulated_count: int = 0


def read_a1b(path: str | Path) -> A1BSnapshot:
    """Read everything one poll needs, in one short read transaction.

    Any failure - a missing file, a locked store, a schema this reader does not
    recognize - returns `ok=False` rather than raising. There is no repair path
    and no schema creation.
    """
    try:
        with read_only_connection(path) as connection:
            connection.execute("BEGIN DEFERRED")
            try:
                totals = connection.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT bar_timestamp), MIN(bar_timestamp),"
                    " MAX(bar_timestamp) FROM a1b_observations"
                ).fetchone()
                observations = tuple(
                    connection.execute(
                        "SELECT symbol, bar_timestamp, session_date, participate, v3_signal,"
                        " v3_stance, alias_scored, mark_index, mark_date, archetype_label,"
                        " active_weight, reserved_weight, target_weight, reference_close,"
                        " designation, recorded_at"
                        " FROM a1b_observations WHERE bar_timestamp >= COALESCE(("
                        "   SELECT MIN(bar_timestamp) FROM ("
                        "     SELECT DISTINCT bar_timestamp FROM a1b_observations"
                        "     ORDER BY bar_timestamp DESC LIMIT ?)"
                        " ), '')"
                        " ORDER BY bar_timestamp, symbol",
                        (CURVE_BARS,),
                    ).fetchall()
                )
                stances = tuple(
                    connection.execute(
                        "SELECT symbol, stance, bar_timestamp, updated_at FROM a1b_stance"
                        " ORDER BY symbol"
                    ).fetchall()
                )
                marks = tuple(
                    connection.execute(
                        "SELECT mark_index, mark_date, fit_mark, labels_json, multipliers_json,"
                        " active_weights_json, reserved_weights_json, labeled_symbols,"
                        " policy_hash, computed_at FROM a1b_mark_state ORDER BY mark_index"
                    ).fetchall()
                )
                regimes = tuple(
                    connection.execute(
                        "SELECT session_date, participate, info_close, info_sma, info_drawdown,"
                        " sessions_observed, sma_sessions, calm_threshold, lag_sessions,"
                        " reference_symbol, computed_at FROM a1b_regime_state"
                        " ORDER BY session_date"
                    ).fetchall()
                )
                events = tuple(
                    connection.execute(
                        "SELECT event_timestamp, event_type, message FROM system_events"
                        " ORDER BY id DESC LIMIT 200"
                    ).fetchall()
                )
                checkpoints = tuple(
                    connection.execute(
                        "SELECT symbol, last_processed_bar_timestamp, updated_at"
                        " FROM runtime_checkpoints ORDER BY symbol"
                    ).fetchall()
                )
                intents = int(
                    connection.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0]
                )
                linked = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM a1b_observations WHERE client_order_id IS NOT NULL"
                    ).fetchone()[0]
                )
                non_simulated = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM a1b_observations WHERE designation <> ?",
                        (DESIGNATION,),
                    ).fetchone()[0]
                )
            finally:
                connection.execute("COMMIT")
    except (sqlite3.Error, OSError, ValueError):
        return A1BSnapshot(ok=False, reason=UNAVAILABLE_DATABASE_UNREADABLE)

    return A1BSnapshot(
        ok=True,
        observations=observations,
        stances=stances,
        marks=marks,
        regimes=regimes,
        events=events,
        checkpoints=checkpoints,
        observation_count=int(totals[0] or 0),
        bar_count=int(totals[1] or 0),
        first_bar=None if totals[2] is None else str(totals[2]),
        last_bar=None if totals[3] is None else str(totals[3]),
        order_intent_count=intents,
        linked_order_count=linked,
        non_simulated_count=non_simulated,
    )


# ==========================================================================
# Panels
# ==========================================================================


@dataclass(frozen=True)
class ServicePanel:
    """Is the observer observing, and is it still incapable of trading?"""

    status: str
    status_reason: str
    mode: str
    designation: str
    universe: tuple[str, ...]
    universe_size: int
    incumbents: tuple[str, ...]
    alias_scored: tuple[str, ...]
    symbols_recorded_last_cycle: int
    last_cycle_at: str | None
    next_expected_cycle_at: str | None
    seconds_since_last_cycle: float | None
    cycles_recorded: int
    observations_recorded: int
    first_bar: str | None
    last_bar: str | None
    code_sha: str | None
    started_at: str | None
    session_confirmed_open: bool
    within_regular_session: bool
    stale_after_seconds: float
    policy_hash: str | None
    mark_every_sessions: int | None
    grid_anchor: str | None
    mark_index: int | None
    mark_date: str | None
    fit_mark: str | None
    labeled_symbols: int | None
    # The invariant, as measured
    broker_mutation: str
    orders_submitted: int
    order_intents_in_database: int
    linked_orders_in_database: int
    non_simulated_rows: int
    zero_order_invariant_holds: bool
    invariant_note: str


@dataclass(frozen=True)
class RegimePanel:
    """EDA-1's participation state as this observer resolved it."""

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
    """One universe symbol's latest observation and current stance."""

    symbol: str
    incumbent: bool
    alias_scored: bool | None
    bar_timestamp: str | None
    reference_close: float | None
    v3_signal: str | None
    v3_stance: int | None
    stance: int | None
    stance_updated_at: str | None
    participate: bool | None
    archetype_label: int | None
    active_weight: float | None
    reserved_weight: float | None
    target_weight: float | None
    designation: str


@dataclass(frozen=True)
class HypotheticalPanel:
    """The one book, plus the label that must travel with it."""

    label: str
    normalized_start: float
    steps: int
    first_bar: str | None
    last_bar: str | None
    costs_applied: bool
    portfolio_value: float | None
    cumulative_return: float | None
    max_drawdown: float | None
    average_exposure: float | None
    current_exposure: float | None
    long_symbols: int
    weight_changes: int
    turnover_per_step: float | None
    benchmark_return: float | None
    sample_warning: str
    sample_is_sufficient: bool
    capture_unavailable_reason: str | None
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class ObservationSummary:
    """Counts an operator can read without any performance inference."""

    observations: int
    bars: int
    symbols_per_bar: int
    participate_bars: int
    defensive_bars: int
    participate_sessions: int
    defensive_sessions: int
    regime_transitions: int
    buy_signals: int
    sell_signals: int
    hold_signals: int
    alias_scored_observations: int
    marks_computed: int
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class HistoryRow:
    bar_timestamp: str
    symbol: str
    session_date: str
    participate: bool
    v3_signal: str
    v3_stance: int
    alias_scored: bool
    archetype_label: int | None
    target_weight: float
    reference_close: float
    designation: str


@dataclass(frozen=True)
class HistoryPage:
    rows: tuple[HistoryRow, ...]
    limit: int
    offset: int
    total: int
    returned: int


@dataclass(frozen=True)
class A1BShadowOverview:
    generated_at: str
    read_only: bool
    observation_only: bool
    hypothetical_label: str
    service: ServicePanel
    regime: RegimePanel
    symbols: tuple[SymbolRow, ...]
    hypothetical: HypotheticalPanel
    summary: ObservationSummary


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _latest_event(snapshot: A1BSnapshot, event_type: str) -> sqlite3.Row | None:
    for row in snapshot.events:
        if str(row["event_type"]) == event_type:
            return row
    return None


def _universe(snapshot: A1BSnapshot) -> tuple[str, ...]:
    """The observed universe: the stance table, else the newest mark's labels.

    Read from the record rather than from a policy artifact, so this module
    carries no copy of the universe that could drift from what the observer
    actually watches.
    """
    symbols = {str(row["symbol"]) for row in snapshot.stances}
    symbols.update(str(row["symbol"]) for row in snapshot.checkpoints)
    if not symbols and snapshot.marks:
        with contextlib.suppress(ValueError, AttributeError):
            symbols.update(json.loads(str(snapshot.marks[-1]["labels_json"])).keys())
    return tuple(sorted(symbols))


def _latest_by_symbol(snapshot: A1BSnapshot) -> dict[str, sqlite3.Row]:
    latest: dict[str, sqlite3.Row] = {}
    for row in snapshot.observations:
        latest[str(row["symbol"])] = row
    return latest


def session_confirmed_open(snapshot: A1BSnapshot, *, now: datetime) -> bool:
    """Has the observer resolved a regime state for today's session?"""
    today = session_date_for(now)
    return any(str(row["session_date"]) == today for row in snapshot.regimes)


def build_service(snapshot: A1BSnapshot, *, now: datetime) -> ServicePanel:
    """Whether the observer is observing - and whether it is still harmless."""
    intents = snapshot.order_intent_count
    linked = snapshot.linked_order_count
    non_simulated = snapshot.non_simulated_count
    invariant = snapshot.ok and intents == 0 and linked == 0 and non_simulated == 0
    invariant_note = (
        "Measured, not asserted: the order-intent table is empty, no observation row "
        "carries an order linkage, and every row is designated SIMULATED_SHADOW. The "
        "process has no execution path to open."
    )

    if not snapshot.ok:
        return ServicePanel(
            status=SHADOW_UNAVAILABLE,
            status_reason=snapshot.reason or UNAVAILABLE_DATABASE_UNREADABLE,
            mode=SHADOW_MODE,
            designation=DESIGNATION,
            universe=(),
            universe_size=0,
            incumbents=(),
            alias_scored=(),
            symbols_recorded_last_cycle=0,
            last_cycle_at=None,
            next_expected_cycle_at=None,
            seconds_since_last_cycle=None,
            cycles_recorded=0,
            observations_recorded=0,
            first_bar=None,
            last_bar=None,
            code_sha=None,
            started_at=None,
            session_confirmed_open=False,
            within_regular_session=within_regular_session(now),
            stale_after_seconds=STALE_AFTER.total_seconds(),
            policy_hash=None,
            mark_every_sessions=None,
            grid_anchor=None,
            mark_index=None,
            mark_date=None,
            fit_mark=None,
            labeled_symbols=None,
            broker_mutation=BROKER_MUTATION_DISABLED,
            orders_submitted=0,
            order_intents_in_database=intents,
            linked_orders_in_database=linked,
            non_simulated_rows=non_simulated,
            zero_order_invariant_holds=False,
            invariant_note=invariant_note,
        )

    universe = _universe(snapshot)
    latest = _latest_by_symbol(snapshot)
    incumbents = tuple(
        symbol
        for symbol in universe
        if symbol in latest and int(latest[symbol]["alias_scored"]) == 0
    )
    aliased = tuple(
        symbol
        for symbol in universe
        if symbol in latest and int(latest[symbol]["alias_scored"]) == 1
    )

    # The observer writes no per-cycle event; the durable evidence of a cycle
    # is the newest observation it recorded.
    last_cycle = max(
        (_parse(str(row["recorded_at"])) for row in snapshot.observations),
        key=lambda moment: moment or datetime.min.replace(tzinfo=UTC),
        default=None,
    )
    started_row = _latest_event(snapshot, EVENT_STARTED)
    stopped_row = _latest_event(snapshot, EVENT_STOPPED)
    started_at = _parse(started_row["event_timestamp"]) if started_row else None
    stopped_at = _parse(stopped_row["event_timestamp"]) if stopped_row else None

    code_sha = policy_hash = grid_anchor = None
    mark_every: int | None = None
    if started_row is not None:
        message = str(started_row["message"] or "")
        code = _CODE_SHA_PATTERN.search(message)
        code_sha = code.group(1) if code else None
        policy = _POLICY_PATTERN.search(message)
        policy_hash = policy.group(1) if policy else None
        mark = _MARK_PATTERN.search(message)
        if mark:
            mark_every = int(mark.group(1))
            grid_anchor = mark.group(2)
    if snapshot.marks:
        policy_hash = str(snapshot.marks[-1]["policy_hash"]) or policy_hash

    in_session = within_regular_session(now)
    confirmed = session_confirmed_open(snapshot, now=now)
    since = (now - last_cycle).total_seconds() if last_cycle is not None else None
    next_expected = last_cycle + CYCLE_INTERVAL if last_cycle is not None and in_session else None

    if not invariant:
        status, reason = SHADOW_STALE, "ZERO_ORDER_INVARIANT_VIOLATED"
    elif stopped_at is not None and (started_at is None or stopped_at > started_at):
        status, reason = SHADOW_STOPPED, "CLEAN_SHUTDOWN_RECORDED"
    elif last_cycle is None:
        status = SHADOW_RUNNING if confirmed else SHADOW_IDLE
        reason = "NO_CYCLE_RECORDED_YET"
    elif since is not None and since <= STALE_AFTER.total_seconds():
        status, reason = SHADOW_RUNNING, "CYCLE_WITHIN_EXPECTED_INTERVAL"
    elif in_session and confirmed:
        status, reason = SHADOW_STALE, "NO_CYCLE_DURING_CONFIRMED_OPEN_SESSION"
    else:
        status, reason = SHADOW_IDLE, "OFF_SESSION_NO_BARS_EXPECTED"

    last_bar = snapshot.last_bar
    recorded_last = sum(1 for row in snapshot.observations if str(row["bar_timestamp"]) == last_bar)
    newest_mark = snapshot.marks[-1] if snapshot.marks else None

    return ServicePanel(
        status=status,
        status_reason=reason,
        mode=SHADOW_MODE,
        designation=DESIGNATION,
        universe=universe,
        universe_size=len(universe),
        incumbents=incumbents,
        alias_scored=aliased,
        symbols_recorded_last_cycle=recorded_last,
        last_cycle_at=_iso(last_cycle),
        next_expected_cycle_at=_iso(next_expected),
        seconds_since_last_cycle=since,
        cycles_recorded=snapshot.bar_count,
        observations_recorded=snapshot.observation_count,
        first_bar=snapshot.first_bar,
        last_bar=snapshot.last_bar,
        code_sha=code_sha,
        started_at=_iso(started_at),
        session_confirmed_open=confirmed,
        within_regular_session=in_session,
        stale_after_seconds=STALE_AFTER.total_seconds(),
        policy_hash=policy_hash,
        mark_every_sessions=mark_every,
        grid_anchor=grid_anchor,
        mark_index=None if newest_mark is None else int(newest_mark["mark_index"]),
        mark_date=None if newest_mark is None else str(newest_mark["mark_date"]),
        fit_mark=(
            None
            if newest_mark is None or newest_mark["fit_mark"] is None
            else str(newest_mark["fit_mark"])
        ),
        labeled_symbols=None if newest_mark is None else int(newest_mark["labeled_symbols"]),
        broker_mutation=BROKER_MUTATION_DISABLED,
        orders_submitted=0,
        order_intents_in_database=intents,
        linked_orders_in_database=linked,
        non_simulated_rows=non_simulated,
        zero_order_invariant_holds=invariant,
        invariant_note=invariant_note,
    )


def build_regime(snapshot: A1BSnapshot) -> RegimePanel:
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
            unavailable_reason=(
                snapshot.reason if not snapshot.ok else UNAVAILABLE_NO_OBSERVATIONS
            ),
        )
    row = snapshot.regimes[-1]
    participate = bool(row["participate"])
    return RegimePanel(
        session_date=str(row["session_date"]),
        state="PARTICIPATE" if participate else "DEFENSIVE",
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


def build_symbols(snapshot: A1BSnapshot) -> tuple[SymbolRow, ...]:
    """Every universe symbol: its newest observation and its current stance."""
    if not snapshot.ok:
        return ()
    latest = _latest_by_symbol(snapshot)
    stances = {str(row["symbol"]): row for row in snapshot.stances}
    rows: list[SymbolRow] = []
    for symbol in _universe(snapshot):
        observation = latest.get(symbol)
        stance = stances.get(symbol)
        rows.append(
            SymbolRow(
                symbol=symbol,
                incumbent=(observation is not None and int(observation["alias_scored"]) == 0),
                alias_scored=(None if observation is None else bool(observation["alias_scored"])),
                bar_timestamp=None if observation is None else str(observation["bar_timestamp"]),
                reference_close=(
                    None if observation is None else float(observation["reference_close"])
                ),
                v3_signal=None if observation is None else str(observation["v3_signal"]),
                v3_stance=None if observation is None else int(observation["v3_stance"]),
                stance=None if stance is None else int(stance["stance"]),
                stance_updated_at=None if stance is None else str(stance["updated_at"]),
                participate=None if observation is None else bool(observation["participate"]),
                archetype_label=(
                    None
                    if observation is None or observation["archetype_label"] is None
                    else int(observation["archetype_label"])
                ),
                active_weight=None if observation is None else float(observation["active_weight"]),
                reserved_weight=(
                    None if observation is None else float(observation["reserved_weight"])
                ),
                target_weight=None if observation is None else float(observation["target_weight"]),
                designation=DESIGNATION if observation is None else str(observation["designation"]),
            )
        )
    return tuple(rows)


# --------------------------------------------------------------------------
# Hypothetical accounting
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Series:
    bars: tuple[str, ...]
    book_steps: tuple[float, ...]
    benchmark_steps: tuple[float, ...]
    exposures: tuple[float, ...]
    weight_changes: int
    turnover: float


def _build_series(snapshot: A1BSnapshot) -> _Series | None:
    """Compound the recorded weights into per-step returns.

    The book holds each symbol at its recorded `target_weight` from bar *t*
    to *t+1*; whatever the weights do not claim sits in cash at zero. The
    benchmark holds every symbol observed at *t* at equal weight. No cost of
    any kind is charged.
    """
    if not snapshot.ok or not snapshot.observations:
        return None
    by_bar: dict[str, dict[str, sqlite3.Row]] = {}
    for row in snapshot.observations:
        by_bar.setdefault(str(row["bar_timestamp"]), {})[str(row["symbol"])] = row
    bars = sorted(by_bar)
    if len(bars) < 2:  # noqa: PLR2004 - one bar yields no realized step
        return None

    book_steps: list[float] = []
    benchmark_steps: list[float] = []
    exposures: list[float] = []
    changes = 0
    turnover = 0.0
    previous_weight: dict[str, float] = {}
    for index in range(len(bars) - 1):
        current, following = by_bar[bars[index]], by_bar[bars[index + 1]]
        shared = sorted(symbol for symbol in current if symbol in following)
        if not shared:
            continue
        book = benchmark = exposure = 0.0
        for symbol in shared:
            start = float(current[symbol]["reference_close"])
            end = float(following[symbol]["reference_close"])
            weight = float(current[symbol]["target_weight"])
            exposure += weight
            if start > 0:
                step = end / start - 1.0
                book += weight * step
                benchmark += step
            previous = previous_weight.get(symbol)
            if previous is not None and abs(previous - weight) > 1e-12:
                changes += 1
                turnover += abs(previous - weight)
            previous_weight[symbol] = weight
        book_steps.append(book)
        benchmark_steps.append(benchmark / float(len(shared)))
        exposures.append(exposure)
    if not book_steps:
        return None
    return _Series(
        bars=tuple(bars),
        book_steps=tuple(book_steps),
        benchmark_steps=tuple(benchmark_steps),
        exposures=tuple(exposures),
        weight_changes=changes,
        turnover=turnover,
    )


def _compound(steps: Sequence[float]) -> tuple[float, float]:
    value = NORMALIZED_START
    peak = NORMALIZED_START
    worst = 0.0
    for step in steps:
        value *= 1.0 + step
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return value, worst


def build_hypothetical(snapshot: A1BSnapshot) -> HypotheticalPanel:
    """The hypothetical A1-B book, labelled as simulation on every payload."""
    series = _build_series(snapshot)
    if series is None:
        return HypotheticalPanel(
            label=HYPOTHETICAL_LABEL,
            normalized_start=NORMALIZED_START,
            steps=0,
            first_bar=None,
            last_bar=None,
            costs_applied=False,
            portfolio_value=None,
            cumulative_return=None,
            max_drawdown=None,
            average_exposure=None,
            current_exposure=None,
            long_symbols=0,
            weight_changes=0,
            turnover_per_step=None,
            benchmark_return=None,
            sample_warning=SAMPLE_WARNING_TEXT,
            sample_is_sufficient=False,
            capture_unavailable_reason=UNAVAILABLE_SAMPLE_TOO_SMALL,
            unavailable_reason=(
                snapshot.reason if not snapshot.ok else UNAVAILABLE_NO_OBSERVATIONS
            ),
        )
    value, drawdown = _compound(series.book_steps)
    benchmark_value, _ = _compound(series.benchmark_steps)
    steps = len(series.book_steps)
    last_bar = series.bars[-1]
    current = [
        float(row["target_weight"])
        for row in snapshot.observations
        if str(row["bar_timestamp"]) == last_bar
    ]
    return HypotheticalPanel(
        label=HYPOTHETICAL_LABEL,
        normalized_start=NORMALIZED_START,
        steps=steps,
        first_bar=series.bars[0],
        last_bar=last_bar,
        costs_applied=False,
        portfolio_value=round(value, 4),
        cumulative_return=round(value / NORMALIZED_START - 1.0, 8),
        max_drawdown=round(drawdown, 8),
        average_exposure=round(sum(series.exposures) / len(series.exposures), 6),
        current_exposure=round(sum(current), 6),
        long_symbols=sum(1 for weight in current if weight > 0),
        weight_changes=series.weight_changes,
        turnover_per_step=round(series.turnover / steps, 6),
        benchmark_return=round(benchmark_value / NORMALIZED_START - 1.0, 8),
        sample_warning=SAMPLE_WARNING_TEXT,
        sample_is_sufficient=steps >= MIN_STEPS_FOR_CONFIDENCE,
        capture_unavailable_reason=(
            None if steps >= MIN_STEPS_FOR_CAPTURE else UNAVAILABLE_SAMPLE_TOO_SMALL
        ),
    )


def build_summary(snapshot: A1BSnapshot) -> ObservationSummary:
    """Raw counts from the record. Nothing here is a performance claim."""
    if not snapshot.ok or not snapshot.observations:
        return ObservationSummary(
            observations=0,
            bars=0,
            symbols_per_bar=0,
            participate_bars=0,
            defensive_bars=0,
            participate_sessions=0,
            defensive_sessions=0,
            regime_transitions=0,
            buy_signals=0,
            sell_signals=0,
            hold_signals=0,
            alias_scored_observations=0,
            marks_computed=len(snapshot.marks) if snapshot.ok else 0,
            unavailable_reason=(
                snapshot.reason if not snapshot.ok else UNAVAILABLE_NO_OBSERVATIONS
            ),
        )
    rows = snapshot.observations
    bars = {str(row["bar_timestamp"]) for row in rows}
    participate_bars = len({str(r["bar_timestamp"]) for r in rows if int(r["participate"]) == 1})
    sessions = [bool(row["participate"]) for row in snapshot.regimes]
    transitions = sum(1 for a, b in zip(sessions, sessions[1:], strict=False) if a != b)
    signals = [str(row["v3_signal"]) for row in rows]
    return ObservationSummary(
        observations=snapshot.observation_count,
        bars=snapshot.bar_count,
        symbols_per_bar=(len(rows) // len(bars)) if bars else 0,
        participate_bars=participate_bars,
        defensive_bars=len(bars) - participate_bars,
        participate_sessions=sum(1 for flag in sessions if flag),
        defensive_sessions=sum(1 for flag in sessions if not flag),
        regime_transitions=transitions,
        buy_signals=sum(1 for signal in signals if signal == "BUY"),
        sell_signals=sum(1 for signal in signals if signal == "SELL"),
        hold_signals=sum(1 for signal in signals if signal == "HOLD"),
        alias_scored_observations=sum(1 for row in rows if int(row["alias_scored"]) == 1),
        marks_computed=len(snapshot.marks),
    )


def build_history(
    snapshot: A1BSnapshot,
    *,
    limit: int = HISTORY_DEFAULT_LIMIT,
    offset: int = 0,
    symbol: str | None = None,
) -> HistoryPage:
    """A bounded, newest-first window of recorded observations."""
    bounded = max(1, min(int(limit), HISTORY_MAX_LIMIT))
    start = max(0, int(offset))
    rows = list(reversed(snapshot.observations)) if snapshot.ok else []
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
                alias_scored=bool(row["alias_scored"]),
                archetype_label=(
                    None if row["archetype_label"] is None else int(row["archetype_label"])
                ),
                target_weight=float(row["target_weight"]),
                reference_close=float(row["reference_close"]),
                designation=str(row["designation"]),
            )
            for row in window
        ),
        limit=bounded,
        offset=start,
        total=len(rows),
        returned=len(window),
    )


def build_overview(*, path: str | Path, now: datetime) -> A1BShadowOverview:
    """One consistent read, assembled into one page."""
    snapshot = read_a1b(path)
    return A1BShadowOverview(
        generated_at=now.isoformat(),
        read_only=True,
        observation_only=True,
        hypothetical_label=HYPOTHETICAL_LABEL,
        service=build_service(snapshot, now=now),
        regime=build_regime(snapshot),
        symbols=build_symbols(snapshot),
        hypothetical=build_hypothetical(snapshot),
        summary=build_summary(snapshot),
    )


__all__ = [
    "A1B_DATABASE_PATH_ENV",
    "BROKER_MUTATION_DISABLED",
    "CURVE_BARS",
    "CYCLE_INTERVAL",
    "DEFAULT_A1B_DATABASE_PATH",
    "DESIGNATION",
    "EVENT_STARTED",
    "EVENT_STOPPED",
    "HISTORY_DEFAULT_LIMIT",
    "HISTORY_MAX_LIMIT",
    "SAMPLE_WARNING_TEXT",
    "SHADOW_MODE",
    "STALE_AFTER",
    "UNAVAILABLE_DATABASE_UNREADABLE",
    "UNAVAILABLE_NO_OBSERVATIONS",
    "UNAVAILABLE_SAMPLE_TOO_SMALL",
    "A1BShadowOverview",
    "A1BSnapshot",
    "HistoryPage",
    "HistoryRow",
    "HypotheticalPanel",
    "ObservationSummary",
    "RegimePanel",
    "ServicePanel",
    "SymbolRow",
    "build_history",
    "build_hypothetical",
    "build_overview",
    "build_regime",
    "build_service",
    "build_summary",
    "build_symbols",
    "database_path",
    "read_a1b",
    "read_only_connection",
    "session_confirmed_open",
]
