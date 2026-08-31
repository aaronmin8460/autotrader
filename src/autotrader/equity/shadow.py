"""Equity V3 + EDA-1 live shadow: real session, real decisions, no order path.

This runtime watches the ten Equity V0.2 symbols on completed regular-session
15-minute bars, runs the **V3 multi-timeframe decision engine** on each newest
completed bar, derives the **EDA-1 research champion's decision** for the same
bar through its exact deterministic overlay (`autotrader.equity.regime`), and
records both durably, side by side. That is the whole job. It sizes nothing,
submits nothing, cancels nothing, and holds nothing that could.

**EDA-1 is derived, not fitted, and re-verified on every bar.** The champion
is V3 plus a deterministic participation overlay driven by the reference
symbol's completed-session closes, with one full session of lag. This runtime
resolves that state once per session - before any decision that session - and
persists it. Each bar's EDA-1 decision is then produced by replaying the
research overlay over the *entire stored V3 series*, and every previously
stored EDA-1 row must equal the replay's answer, field by field, or the
process stops: the stored challenger series can never silently drift from the
transform that defines it.

**The guarantee is structural, not behavioural.** The trading runtime removes
its execution path with a flag (`--observe-only` constructs no gateway); this
runtime goes further and has no seam a gateway could be handed through: the
constructor takes no execution argument, no attribute holds one, and this
module imports nothing from the execution layer. A decision that names a
direction is recorded and then *dropped* - the panel's candidate has nowhere to
go, because there is deliberately nowhere for it to go. Tests assert each of
those facts against the parsed source rather than against this docstring.

**One decision engine, named honestly.** The shadow panel is built with V3 as
its only member and V3 as the configured `execution_version`, because the
panel's contract requires the named version to be present. In this process the
designation ``EXECUTED`` on a stored row therefore means exactly what the
decision-shadow schema documents - "the decision was actionable and was
released by the panel" - and nothing more: no risk evaluation follows, no
intent is created, and `client_order_id` stays NULL on every row forever.

**A database that has ever ordered is refused.** The shadow keeps its own
SQLite file. Sharing the trading database would share the per-symbol bar
claims - the shadow would steal completed bars from the trading runtime, or
vice versa - so at startup and after every cycle this runtime asserts that its
database contains **zero order intents** and refuses to run otherwise. The
production trading database always contains intent rows, so pointing this
process at it fails immediately and loudly.

**Session semantics are the trading runtime's, unchanged.** The broker's
calendar is the authority; a cycle outside the regular session does nothing at
all; an in-progress candle is never evaluated; a completed bar is claimed
durably before any engine sees it and is never evaluated twice, within a
process or across restarts. Miss an observation rather than duplicate one -
the same one-sided preference the trading path has, for the same reason: a
replayed bar would be a second opinion about the same fifteen minutes.

**The lookback is V3-sized, not EMA-sized.** V3 needs thousands of base bars
to fill its multi-timeframe context (2,834 declared; the ten-symbol historical
study measured a worst case of 4,552 on real provider gaps and pre-declared a
uniform 4,750). The default window here is that study's 4,750. A short window
does not break anything - V3 answers HOLD naming the missing timeframe - but a
shadow that recorded permanent insufficiency HOLDs would be measuring its own
configuration, so the bound refuses anything below V3's declared requirement.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

import pandas as pd

from autotrader.data.validation import (
    EQUITY_UNIVERSE_LABEL,
    ValidationResult,
    validate_frame,
)
from autotrader.decision.contract import VERSION_V3, DecisionSignal
from autotrader.decision.v3 import MultiTimeframeV3Engine
from autotrader.equity import EQUITY_SYMBOLS, EquityError
from autotrader.equity.regime import (
    EDA1_ENGINE_VERSION,
    REGIME_REFERENCE_SYMBOL,
    ParticipationSpec,
    SeriesRecord,
    participation_overlay,
    session_closes,
    source_stance,
    state_for_session,
)
from autotrader.equity.session import (
    MarketCalendar,
    MarketSession,
    SessionError,
    is_market_open,
    is_regular_session_bar,
    latest_completed_session_bar,
    lookback_window,
    market_date,
    recent_sessions,
    session_bar_mask,
    sessions_needed,
)
from autotrader.equity.session import next_wake_time as next_session_wake_time
from autotrader.runtime.checkpoint import ProcessedBarCheckpoint, SqliteCheckpoint
from autotrader.runtime.monitoring import (
    Heartbeat,
    HeartbeatSnapshot,
    RuntimeState,
    get_logger,
    log_event,
)
from autotrader.runtime.runner import SHUTDOWN_POLL_SECONDS, ShutdownRequest
from autotrader.runtime.schedule import (
    DEFAULT_SAFETY_DELAY,
    is_bar_complete,
    require_safety_delay,
    require_utc,
)
from autotrader.shadow.cycle import SKIPPED_ALREADY_PROCESSED, BarOutcome, ShadowCycle
from autotrader.shadow.panel import EnginePanel, PanelEvaluation, ShadowError
from autotrader.shadow.recorder import ShadowRecorder
from autotrader.state import sqlite as state

#: The universe and its fixed processing order - the trading runtime's, exactly.
SHADOW_PROCESSING_ORDER: tuple[str, ...] = EQUITY_SYMBOLS

#: The one engine version this shadow observes. Also the panel's configured
#: execution version, because a panel refuses a version it does not hold; in
#: this process "execution" ends at the panel's return value, which is dropped.
SHADOW_ENGINE_VERSION = VERSION_V3

#: The lock scope. Distinct from both the crypto runner's and the equity
#: trading runtime's, so the shadow never blocks either - while a second
#: shadow process is still refused.
EQUITY_SHADOW_LOCK_SCOPE = "equity-shadow"

#: V3's own declared minimum, read from the engine rather than copied here.
V3_REQUIRED_BASE_BARS: int = MultiTimeframeV3Engine.for_symbol(
    SHADOW_PROCESSING_ORDER[0]
).required_base_bars

#: Bounds for the shadow's completed-bar lookback. The floor is V3's declared
#: requirement: below it every decision would be an insufficiency HOLD and the
#: record would describe the configuration rather than the market. The default
#: is the ten-symbol historical study's pre-declared uniform lookback (4,750),
#: 4.3% above that study's measured worst case on real provider gaps.
MIN_SHADOW_LOOKBACK_BARS: int = V3_REQUIRED_BASE_BARS
MAX_SHADOW_LOOKBACK_BARS = 6000
DEFAULT_SHADOW_LOOKBACK_BARS = 4750

#: Bounds for the completed sessions behind the EDA-1 regime state. The floor
#: covers the router's 200-session average, its one-session lag, and margin
#: for provider gaps; below it the state would be a permanent warm-up
#: DEFENSIVE that measures the configuration rather than the market. The
#: default approximates the research frame: the trailing-peak drawdown there
#: ran from the start of a multi-year history, and the provider keeps roughly
#: six years, so the state window asks for all of it and uses what exists.
MIN_STATE_SESSIONS = 250
MAX_STATE_SESSIONS = 2500
DEFAULT_STATE_SESSIONS = 1500

#: Audit event types this runtime writes to `system_events`.
EVENT_SHADOW_STARTED = "EQUITY_SHADOW_STARTED"
EVENT_SHADOW_STOPPED = "EQUITY_SHADOW_STOPPED"
EVENT_SHADOW_CYCLE = "EQUITY_SHADOW_CYCLE"

#: Where a cycle found itself in the market calendar. The trading runtime's
#: vocabulary, so one operator reads one set of tokens across both services.
SESSION_OPEN = "SESSION_OPEN"
SESSION_CLOSED = "SESSION_CLOSED"
NO_SESSION_TODAY = "NO_SESSION_TODAY"


class ShadowIntegrityError(ShadowError):
    """The zero-order-mutation invariant could not be verified. Stop."""


def require_shadow_lookback_bars(value: int, field_name: str = "lookback_bars") -> int:
    """Require a lookback V3 can actually fill its timeframes from.

    Deliberately not the trading runtime's bound: that one is sized for an
    EMA 50 and tops out far below V3's multi-timeframe context. The two limits
    answer different questions and neither is reused for the other.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise EquityError(f"{field_name} must be an int, got {type(value).__name__}.")
    if not MIN_SHADOW_LOOKBACK_BARS <= value <= MAX_SHADOW_LOOKBACK_BARS:
        raise EquityError(
            f"{field_name} must be between {MIN_SHADOW_LOOKBACK_BARS} (V3's declared "
            f"base-bar requirement) and {MAX_SHADOW_LOOKBACK_BARS}, got {value}."
        )
    return value


def require_state_sessions(value: int, field_name: str = "state_sessions") -> int:
    """Require a session count the regime state can actually be resolved from."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise EquityError(f"{field_name} must be an int, got {type(value).__name__}.")
    if not MIN_STATE_SESSIONS <= value <= MAX_STATE_SESSIONS:
        raise EquityError(
            f"{field_name} must be between {MIN_STATE_SESSIONS} (the router's "
            f"200-session average plus lag and margin) and {MAX_STATE_SESSIONS}, "
            f"got {value}."
        )
    return value


@dataclass(frozen=True)
class EquityShadowConfig:
    """How the shadow loop runs. Nothing here decides or authorizes anything.

    `code_sha` is provenance for the record: the commit the process was started
    from, resolved by the caller (the CLI reads it from the repository) because
    this module runs no subprocess. None is recorded honestly as unknown.
    """

    safety_delay: timedelta = DEFAULT_SAFETY_DELAY
    lookback_bars: int = DEFAULT_SHADOW_LOOKBACK_BARS
    state_sessions: int = DEFAULT_STATE_SESSIONS
    code_sha: str | None = None

    def __post_init__(self) -> None:
        require_safety_delay(self.safety_delay)
        require_shadow_lookback_bars(self.lookback_bars)
        require_state_sessions(self.state_sessions)


class ShadowBarSource(Protocol):
    """Where the shadow gets its V3-sized completed-bar window from.

    Structurally identical to the trading runtime's bar source protocol, but
    declared separately: the two runtimes accept different lookback bounds, and
    a test pins this protocol's shape rather than importing the other.
    """

    def recent_bars(
        self,
        symbols: Sequence[str],
        *,
        now: datetime,
        latest_bar_start: datetime,
        lookback_bars: int,
    ) -> dict[str, pd.DataFrame]:
        """Canonical regular-session bars per symbol, newest bar last."""


def filter_to_shadow_sessions(
    frame: pd.DataFrame,
    sessions: Sequence[MarketSession],
    *,
    lookback_bars: int,
) -> pd.DataFrame:
    """Keep only regular-session bars, then only the newest `lookback_bars`.

    The trading path's two-step trim under the shadow's own bound: session
    membership first - so a pre-market candle cannot consume a slot the V3
    context relies on - then the count.
    """
    count = require_shadow_lookback_bars(lookback_bars)
    if frame.empty:
        return frame
    mask = session_bar_mask(sessions, list(frame["timestamp"]))
    regular = frame.loc[mask].reset_index(drop=True)
    if len(regular) <= count:
        return regular
    return regular.iloc[-count:].reset_index(drop=True)


class ShadowEquityBars:
    """The production `ShadowBarSource`: one batched provider request per cycle.

    The same market-data boundary the trading runtime reads through - the
    historical stock-bars client, which has no order surface of any kind - with
    a window sized for V3's multi-timeframe context instead of for an EMA 50.
    Ten symbols still cost one request.
    """

    def __init__(
        self,
        calendar: MarketCalendar,
        client: object | None = None,
    ) -> None:
        self._calendar = calendar
        self._client = client
        #: Provider calls actually made, for the shared API-budget accounting.
        self.api_calls = 0

    def _resolve_client(self) -> object:
        if self._client is None:
            from autotrader.equity.data import create_client

            self._client = create_client()
        return self._client

    def recent_bars(
        self,
        symbols: Sequence[str],
        *,
        now: datetime,
        latest_bar_start: datetime,
        lookback_bars: int,
    ) -> dict[str, pd.DataFrame]:
        """Fetch the bounded completed window for the whole universe at once."""
        from autotrader.data.historical import RESOLUTION
        from autotrader.equity.data import fetch_bars_for_symbols

        count = require_shadow_lookback_bars(lookback_bars)
        latest = require_utc(latest_bar_start, "latest_bar_start")
        require_utc(now, "now")
        sessions = recent_sessions(
            self._calendar,
            day=market_date(latest),
            count=sessions_needed(count),
        )
        start, end = lookback_window(sessions, latest_bar_start=latest)
        self.api_calls += 1
        frames = fetch_bars_for_symbols(self._resolve_client(), symbols, start, end - RESOLUTION)
        return {
            symbol: filter_to_shadow_sessions(frame, sessions, lookback_bars=count)
            for symbol, frame in frames.items()
        }


class RegimeBarSource(Protocol):
    """Where the EDA-1 regime state gets its completed-session closes from.

    A separate protocol from `ShadowBarSource` because it answers a different
    question: not "the newest V3-sized window ending at this bar" but "every
    completed regular-session bar of the reference symbol across the sessions
    strictly before this one". The state must never see the governing
    session's own bars, so the boundary is a session date, not a bar.
    """

    def state_frame(
        self,
        *,
        before: date,
        now: datetime,
        sessions: int,
    ) -> pd.DataFrame:
        """Reference-symbol bars over completed sessions strictly before `before`."""


class RegimeEquityBars:
    """The production `RegimeBarSource`: one reference-symbol request per session.

    The same market-data boundary the shadow's universe fetch reads through -
    the historical stock-bars client, which has no order surface of any kind.
    Called once per session (the resolved state is persisted and reused), so
    the multi-year window costs one request a day.
    """

    def __init__(
        self,
        calendar: MarketCalendar,
        client: object | None = None,
    ) -> None:
        self._calendar = calendar
        self._client = client
        #: Provider calls actually made, for the shared API-budget accounting.
        self.api_calls = 0

    def _resolve_client(self) -> object:
        if self._client is None:
            from autotrader.equity.data import create_client

            self._client = create_client()
        return self._client

    def state_frame(
        self,
        *,
        before: date,
        now: datetime,
        sessions: int,
    ) -> pd.DataFrame:
        """Fetch the reference symbol's bars for the sessions before `before`."""
        from autotrader.data.historical import RESOLUTION
        from autotrader.equity.data import fetch_bars_for_symbols

        count = require_state_sessions(sessions)
        require_utc(now, "now")
        window = recent_sessions(
            self._calendar,
            day=before - timedelta(days=1),
            count=count,
        )
        if not window:
            raise EquityError(
                f"The broker's calendar reports no session before {before.isoformat()}, "
                "so no regime state can be resolved and nothing was fetched."
            )
        if window[-1].session_date >= before:
            raise SessionError(
                f"The calendar returned a session on {window[-1].session_date.isoformat()} "
                f"for a state window that must end before {before.isoformat()}. Refusing "
                "to resolve a regime state that could read the governing session's own "
                "closes."
            )
        start = window[0].open_utc
        end = window[-1].close_utc
        self.api_calls += 1
        frames = fetch_bars_for_symbols(
            self._resolve_client(),
            [REGIME_REFERENCE_SYMBOL],
            start,
            end - RESOLUTION,
        )
        frame = frames[REGIME_REFERENCE_SYMBOL]
        if frame.empty:
            return frame
        mask = session_bar_mask(window, list(frame["timestamp"]))
        return frame.loc[mask].reset_index(drop=True)


@dataclass
class ShadowSymbolResult:
    """What one symbol's pass through one cycle produced."""

    symbol: str
    bar_timestamp: datetime | None = None
    recorded: bool = False
    signal: str | None = None
    eda1_signal: str | None = None
    candidate_dropped: bool = False
    skipped_reason: str | None = None


@dataclass
class ShadowCycleReport:
    """What one whole cycle produced, plus the session it ran in."""

    started_at: datetime
    session_state: str = SESSION_CLOSED
    session: MarketSession | None = None
    results: list[ShadowSymbolResult] = field(default_factory=list)
    error: str | None = None
    fatal: bool = False

    @property
    def recorded_count(self) -> int:
        return sum(1 for result in self.results if result.recorded)

    @property
    def dropped_candidates(self) -> int:
        return sum(1 for result in self.results if result.candidate_dropped)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _describe_validation(result: ValidationResult) -> str:
    return "; ".join(str(issue) for issue in result.errors)


@dataclass
class _SymbolBars:
    """One symbol's completed regular-session bars for this cycle."""

    frame: pd.DataFrame = field(default_factory=pd.DataFrame)
    latest: datetime | None = None


#: The shadow's own side-by-side tables. They live only in the shadow's
#: database - which refuses to be the trading database - and hold the regime
#: state series and the per-bar V3/EDA-1 comparison the later evaluation joins
#: on. Created idempotently at startup; the shared schema is not changed.
_SIDE_BY_SIDE_TABLES = """
CREATE TABLE IF NOT EXISTS shadow_regime_state (
    session_date TEXT PRIMARY KEY,
    participate INTEGER NOT NULL CHECK (participate IN (0, 1)),
    info_close REAL,
    info_sma REAL,
    info_drawdown REAL,
    sessions_observed INTEGER NOT NULL,
    sma_sessions INTEGER NOT NULL,
    calm_threshold REAL NOT NULL,
    lag_sessions INTEGER NOT NULL,
    reference_symbol TEXT NOT NULL,
    computed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_side_by_side (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    bar_timestamp TEXT NOT NULL,
    session_date TEXT NOT NULL,
    participate INTEGER NOT NULL CHECK (participate IN (0, 1)),
    v3_signal TEXT NOT NULL,
    v3_stance INTEGER NOT NULL CHECK (v3_stance IN (0, 1)),
    eda1_signal TEXT NOT NULL,
    eda1_stance INTEGER NOT NULL CHECK (eda1_stance IN (0, 1)),
    signals_agree INTEGER NOT NULL CHECK (signals_agree IN (0, 1)),
    stances_agree INTEGER NOT NULL CHECK (stances_agree IN (0, 1)),
    reference_close REAL NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (symbol, bar_timestamp)
);
"""


def create_side_by_side_tables(connection: sqlite3.Connection) -> None:
    """Create the shadow-only regime-state and comparison tables, idempotently."""
    connection.executescript(_SIDE_BY_SIDE_TABLES)


class SideBySideShadowRecorder(ShadowRecorder):
    """Records V3's decision and EDA-1's derived decision for every bar, atomically.

    The V3 rows are the shipped recorder's, unchanged. In the same transaction
    this recorder then replays the research overlay over the entire stored V3
    series - under the persisted per-session participation states - verifies
    that every previously stored EDA-1 row equals the replay's answer field by
    field, writes the newest EDA-1 row, and writes one comparison row holding
    both signals, both hypothetical stances, the regime state, and the bar's
    reference close. Two decision rows and one comparison row, or none.

    Like its parent it reaches nothing that could act on a decision: no
    client, no gateway, no socket. It reads and writes rows.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        spec: ParticipationSpec,
        strategy_run_id: int | None = None,
    ) -> None:
        super().__init__(connection, strategy_run_id=strategy_run_id)
        self._spec = spec
        self._context_symbol: str | None = None
        self._context_close: float | None = None
        #: The newest EDA-1 record per symbol, for the runtime's log line.
        self.last_eda1: dict[str, SeriesRecord] = {}
        #: The participation state behind the newest record, per symbol.
        self.last_participate: dict[str, bool] = {}

    def begin_bar(self, symbol: str, *, reference_close: float) -> None:
        """Hand the recorder the one per-bar fact the evaluation cannot carry."""
        self._context_symbol = symbol
        self._context_close = float(reference_close)

    def record(self, evaluation: PanelEvaluation) -> tuple[int, ...]:
        """Write the V3 rows, the EDA-1 row, and the comparison row, atomically."""
        with state.transaction(self._connection):
            recorded = super().record(evaluation)
            eda1_id = self._record_eda1(evaluation)
        return (*recorded, eda1_id)

    # ------------------------------------------------------------------
    # EDA-1 derivation
    # ------------------------------------------------------------------

    def _record_eda1(self, evaluation: PanelEvaluation) -> int:
        observation = evaluation.observation_for(SHADOW_ENGINE_VERSION)
        if observation is None:
            raise ShadowIntegrityError(
                "The panel evaluation carries no V3 observation, so no EDA-1 decision "
                "can be derived. Nothing was recorded."
            )
        symbol = evaluation.symbol
        if self._context_symbol != symbol or self._context_close is None:
            raise ShadowIntegrityError(
                f"No reference close was staged for {symbol}; begin_bar must be called "
                "before every evaluation so the comparison row is complete. Nothing "
                "was recorded."
            )
        reference_close = self._context_close
        self._context_symbol = None
        self._context_close = None

        source = self._stored_series(symbol, SHADOW_ENGINE_VERSION)
        if not source:
            raise ShadowIntegrityError(
                f"The V3 row for {symbol} is not visible in this transaction; the "
                "EDA-1 derivation has nothing to replay. Nothing was recorded."
            )
        participate = self._participation_by_session()
        derived = participation_overlay(source, participate)
        stored = self._stored_series(symbol, EDA1_ENGINE_VERSION)
        self._require_replay_matches(symbol, stored, derived)

        current = derived[-1]
        row_id = state.record_shadow_decision(
            self._connection,
            strategy_run_id=self.strategy_run_id,
            bar_timestamp=current.timestamp,
            symbol=symbol,
            engine_version=EDA1_ENGINE_VERSION,
            signal=current.signal.value,
            score=current.score,
            confidence=current.confidence,
            regime=current.regime,
            reasons=current.reasons,
            feature_version=None,
            model_version=None,
            execution_version=observation.execution_version,
            designation=state.SHADOW_DESIGNATION_NOT_EXECUTED,
        )

        session_day = market_date(current.timestamp)
        state_on = participate[session_day]
        v3_signal = source[-1].signal
        v3_stance = source_stance(list(source))[-1]
        eda1_stance = source_stance(list(derived))[-1]
        self._connection.execute(
            "INSERT INTO shadow_side_by_side ("
            " symbol, bar_timestamp, session_date, participate,"
            " v3_signal, v3_stance, eda1_signal, eda1_stance,"
            " signals_agree, stances_agree, reference_close, recorded_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                symbol,
                state.to_utc_text(current.timestamp, "bar_timestamp"),
                session_day.isoformat(),
                int(state_on),
                v3_signal.value,
                int(v3_stance),
                current.signal.value,
                int(eda1_stance),
                int(v3_signal is current.signal),
                int(v3_stance == eda1_stance),
                reference_close,
                state.to_utc_text(datetime.now(UTC), "recorded_at"),
            ),
        )
        self.last_eda1[symbol] = current
        self.last_participate[symbol] = state_on
        return row_id

    def _stored_series(self, symbol: str, engine_version: str) -> tuple[SeriesRecord, ...]:
        rows = self._connection.execute(
            "SELECT bar_timestamp, signal, score, confidence, regime, reasons"
            " FROM shadow_decisions WHERE symbol = ? AND engine_version = ?"
            " ORDER BY bar_timestamp",
            (symbol, engine_version),
        ).fetchall()
        return tuple(
            SeriesRecord(
                timestamp=state.from_utc_text(row[0]),
                symbol=symbol,
                signal=DecisionSignal(row[1]),
                score=float(row[2]),
                confidence=float(row[3]),
                regime=str(row[4]),
                reasons=tuple(str(row[5]).split(state.REASON_SEPARATOR)),
            )
            for row in rows
        )

    def _participation_by_session(self) -> dict[date, bool]:
        rows = self._connection.execute(
            "SELECT session_date, participate FROM shadow_regime_state"
        ).fetchall()
        return {date.fromisoformat(str(row[0])): bool(row[1]) for row in rows}

    def _require_replay_matches(
        self,
        symbol: str,
        stored: tuple[SeriesRecord, ...],
        derived: tuple[SeriesRecord, ...],
    ) -> None:
        """The stored EDA-1 series must be exactly the replay minus the newest bar."""
        expected = derived[:-1]
        if len(stored) != len(expected):
            raise ShadowIntegrityError(
                f"The stored EDA-1 series for {symbol} holds {len(stored)} row(s) but "
                f"the overlay replay of the stored V3 series produces {len(expected)}. "
                "The stored challenger series no longer matches the transform that "
                "defines it; nothing was recorded."
            )
        for stored_record, expected_record in zip(stored, expected, strict=True):
            if stored_record != expected_record:
                raise ShadowIntegrityError(
                    f"The stored EDA-1 decision for {symbol} at "
                    f"{stored_record.timestamp.isoformat()} does not equal the overlay "
                    "replay of the stored V3 series. The stored challenger series no "
                    "longer matches the transform that defines it; nothing was recorded."
                )


class EquityShadowRuntime:
    """The V3 equity live shadow. Observes, records, and cannot trade.

    Construct it with an open state connection on the shadow's **own**
    database, a V3-sized bar source and a market calendar. There is no
    execution parameter, no gateway attribute, and no authorization to
    resolve: nothing in this object's reach can create an order intent, and
    the zero-intent assertion below turns that from a design intention into a
    per-cycle checked invariant.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        market_data: ShadowBarSource,
        regime_data: RegimeBarSource,
        calendar: MarketCalendar,
        checkpoint: ProcessedBarCheckpoint | None = None,
        config: EquityShadowConfig | None = None,
        regime_spec: ParticipationSpec | None = None,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
        shutdown: ShutdownRequest | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._connection = connection
        self._config = config if config is not None else EquityShadowConfig()
        self._market_data = market_data
        self._regime_data = regime_data
        self._calendar = calendar
        self._checkpoint: ProcessedBarCheckpoint = (
            checkpoint if checkpoint is not None else SqliteCheckpoint(connection)
        )
        self._spec = regime_spec if regime_spec is not None else ParticipationSpec()
        self._clock = clock
        self._sleep = sleep
        self._shutdown = shutdown if shutdown is not None else ShutdownRequest()
        self._logger = logger if logger is not None else get_logger()

        self._heartbeat = Heartbeat()
        self._heartbeat.last_processed_bars = {symbol: None for symbol in SHADOW_PROCESSING_ORDER}
        self._recorder = SideBySideShadowRecorder(connection, spec=self._spec, strategy_run_id=None)
        self._cycles: dict[str, ShadowCycle] = {
            symbol: ShadowCycle(
                panel=EnginePanel(
                    (MultiTimeframeV3Engine.for_symbol(symbol),),
                    execution_version=SHADOW_ENGINE_VERSION,
                ),
                recorder=self._recorder,
                checkpoint=self._checkpoint,
            )
            for symbol in SHADOW_PROCESSING_ORDER
        }
        self._started = False

    # ------------------------------------------------------------------
    # Observable state
    # ------------------------------------------------------------------

    @property
    def heartbeat(self) -> HeartbeatSnapshot:
        return self._heartbeat.snapshot()

    @property
    def state(self) -> RuntimeState:
        return self._heartbeat.state

    @property
    def shutdown(self) -> ShutdownRequest:
        return self._shutdown

    @property
    def engine_version(self) -> str:
        """The version the panel evaluates; EDA-1 is derived from its record."""
        return SHADOW_ENGINE_VERSION

    @property
    def derived_engine_version(self) -> str:
        """The challenger recorded beside the panel's engine on every bar."""
        return EDA1_ENGINE_VERSION

    @property
    def regime_spec(self) -> ParticipationSpec:
        """The research champion's predeclared router parameters."""
        return self._spec

    @property
    def lookback_bars(self) -> int:
        return self._config.lookback_bars

    # ------------------------------------------------------------------
    # The zero-order invariant
    # ------------------------------------------------------------------

    def assert_no_order_intents(self) -> None:
        """Refuse to proceed if this database has ever held an order intent.

        Two protections in one check. It proves, durably and after every
        cycle, that nothing reachable from this process created an intent -
        the row every submission path writes before touching a broker. And it
        refuses the trading database outright at startup: that file always
        contains intent rows, so a shadow misconfigured to share it - and
        thereby share the trading runtime's bar claims - stops before
        claiming a single bar.
        """
        row = self._connection.execute("SELECT COUNT(*) FROM order_intents").fetchone()
        count = int(row[0])
        if count != 0:
            raise ShadowIntegrityError(
                f"The shadow database contains {count} order intent(s). This process "
                "records decisions and must have no path to an order, so a database "
                "that has ever held one is refused - it is either the trading "
                "database, which the shadow must not share, or evidence that "
                "something with an execution path wrote here. Nothing was evaluated."
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Verify the zero-order invariant, then open the record."""
        if self._started:
            return
        now = require_utc(self._clock(), "now")
        self.assert_no_order_intents()
        create_side_by_side_tables(self._connection)
        self._require_consistent_regime_spec()

        self._heartbeat.state = RuntimeState.RUNNING
        self._heartbeat.started_at = now
        for symbol in SHADOW_PROCESSING_ORDER:
            self._heartbeat.last_processed_bars[symbol] = self._checkpoint.last_processed(symbol)
        self._started = True

        spec = self._spec
        state.record_system_event(
            self._connection,
            event_timestamp=now,
            event_type=EVENT_SHADOW_STARTED,
            message=(
                f"Equity V3 + EDA-1 side-by-side shadow started for "
                f"{', '.join(SHADOW_PROCESSING_ORDER)}. Engines {SHADOW_ENGINE_VERSION} "
                f"and {EDA1_ENGINE_VERSION} (router sma={spec.sma_sessions}, "
                f"calm={spec.calm_threshold}, lag={spec.lag_sessions}, "
                f"reference {REGIME_REFERENCE_SYMBOL}), lookback "
                f"{self._config.lookback_bars} bars, state window "
                f"{self._config.state_sessions} sessions, code "
                f"{self._config.code_sha or 'unknown'}. This process holds no "
                "execution path: zero order mutation, verified per cycle."
            ),
        )
        log_event(
            self._logger,
            "equity_shadow_started",
            started_at=now,
            engine=SHADOW_ENGINE_VERSION,
            derived_engine=EDA1_ENGINE_VERSION,
            regime_sma_sessions=spec.sma_sessions,
            regime_calm_threshold=spec.calm_threshold,
            regime_lag_sessions=spec.lag_sessions,
            regime_reference=REGIME_REFERENCE_SYMBOL,
            symbols=",".join(SHADOW_PROCESSING_ORDER),
            lookback_bars=self._config.lookback_bars,
            state_sessions=self._config.state_sessions,
            safety_delay_seconds=self._config.safety_delay.total_seconds(),
            code_sha=self._config.code_sha,
            order_paths="NONE",
        )

    def _require_consistent_regime_spec(self) -> None:
        """Refuse a database whose stored regime states used different parameters.

        The overlay replay treats every stored state as produced by *this*
        process's router. A state series computed under another spec would make
        the stored EDA-1 rows unreproducible - so it is refused at startup, the
        same way a database with intents is.
        """
        rows = self._connection.execute(
            "SELECT DISTINCT sma_sessions, calm_threshold, lag_sessions, reference_symbol"
            " FROM shadow_regime_state"
        ).fetchall()
        expected = (
            self._spec.sma_sessions,
            self._spec.calm_threshold,
            self._spec.lag_sessions,
            REGIME_REFERENCE_SYMBOL,
        )
        for row in rows:
            found = (int(row[0]), float(row[1]), int(row[2]), str(row[3]))
            if found != expected:
                raise ShadowIntegrityError(
                    f"This database holds regime states computed under "
                    f"(sma, calm, lag, reference)={found}, but this process is "
                    f"configured with {expected}. A state series from another router "
                    "would make the stored EDA-1 series unreproducible; refusing to "
                    "mix them. Nothing was evaluated."
                )

    def stop(self) -> None:
        """Close the record. Idempotent."""
        if not self._started:
            return
        self._started = False
        now = require_utc(self._clock(), "now")
        if self._heartbeat.state is not RuntimeState.FAILED:
            self._heartbeat.state = RuntimeState.STOPPED
        state.record_system_event(
            self._connection,
            event_timestamp=now,
            event_type=EVENT_SHADOW_STOPPED,
            message=(
                f"Equity V3 shadow stopped in state {self._heartbeat.state.value}. "
                "Orders submitted by this process: 0, by construction."
            ),
        )
        log_event(
            self._logger,
            "equity_shadow_stopped",
            stopped_at=now,
            state=self._heartbeat.state,
            signal=self._shutdown.signal_name,
            cycles_started=self._heartbeat.cycles_started,
            cycles_completed=self._heartbeat.cycles_completed,
            orders_submitted=self._heartbeat.orders_submitted,
        )
        self.log_heartbeat()

    def log_heartbeat(self) -> None:
        log_event(self._logger, "heartbeat", **self._heartbeat.snapshot().as_fields())

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def run_once(self) -> ShadowCycleReport:
        """Process the current cycle once and stop. Honest about a shut market."""
        self.start()
        try:
            report = self.run_cycle()
        except BaseException:
            self._heartbeat.state = RuntimeState.FAILED
            self.stop()
            raise
        self.stop()
        return report

    def run_forever(self, *, max_cycles: int | None = None) -> list[ShadowCycleReport]:
        """Run on the session's own bar boundaries until told to stop."""
        self.start()
        reports: list[ShadowCycleReport] = []
        try:
            while not self._shutdown.requested:
                if max_cycles is not None and len(reports) >= max_cycles:
                    break
                try:
                    target = next_session_wake_time(
                        self._calendar,
                        now=self._clock(),
                        safety_delay=self._config.safety_delay,
                    )
                except SessionError as error:
                    self._heartbeat.state = RuntimeState.FAILED
                    self._heartbeat.last_error = f"{type(error).__name__}: {error}"
                    log_event(
                        self._logger,
                        "cycle_error",
                        level=logging.ERROR,
                        severity="FATAL",
                        error=self._heartbeat.last_error,
                    )
                    break
                log_event(self._logger, "cycle_scheduled", wake_at=target)
                self._wait_until(target)
                if self._shutdown.requested:
                    break
                report = self.run_cycle()
                reports.append(report)
                if report.fatal:
                    break
        finally:
            self.stop()
        return reports

    def _wait_until(self, target: datetime) -> None:
        while not self._shutdown.requested:
            remaining = (target - require_utc(self._clock(), "now")).total_seconds()
            if remaining <= 0:
                return
            self._sleep(min(remaining, SHUTDOWN_POLL_SECONDS))

    def run_cycle(self, now: datetime | None = None) -> ShadowCycleReport:
        """One cycle: session gate, one batched fetch, ten evaluations, one audit row."""
        moment = require_utc(now if now is not None else self._clock(), "now")
        report = ShadowCycleReport(started_at=moment)
        self._heartbeat.cycles_started += 1
        self._heartbeat.last_cycle_started_at = moment
        log_event(
            self._logger,
            "cycle_started",
            at=moment,
            state=self._heartbeat.state,
            mode="SHADOW_OBSERVE_ONLY",
        )

        try:
            session = self._resolve_session(moment, report)
            if session is not None:
                self._run_symbols(session, moment, report)
        except ShadowIntegrityError:
            raise
        except Exception as error:  # noqa: BLE001 - classified rather than propagated
            self._record_cycle_error(error, report)

        if report.error is None:
            self._heartbeat.cycles_completed += 1
            self._heartbeat.last_successful_cycle_at = moment
        if report.session_state == SESSION_OPEN:
            self._record_cycle_audit(moment, report)

        # The invariant, re-verified after every cycle so a violation is
        # caught within fifteen minutes of existing, whatever caused it.
        self.assert_no_order_intents()

        log_event(
            self._logger,
            "cycle_finished",
            at=moment,
            session=report.session_state,
            recorded=report.recorded_count,
            candidates_dropped=report.dropped_candidates,
            error=report.error,
            state=self._heartbeat.state,
        )
        self.log_heartbeat()
        return report

    # ------------------------------------------------------------------
    # Cycle internals
    # ------------------------------------------------------------------

    def _resolve_session(self, moment: datetime, report: ShadowCycleReport) -> MarketSession | None:
        open_now, session = is_market_open(self._calendar, now=moment)
        report.session = session
        if session is None:
            report.session_state = NO_SESSION_TODAY
            log_event(
                self._logger,
                "session_closed",
                at=moment,
                reason=NO_SESSION_TODAY,
                market_date=market_date(moment).isoformat(),
            )
            return None
        if not open_now:
            report.session_state = SESSION_CLOSED
            log_event(
                self._logger,
                "session_closed",
                at=moment,
                reason=SESSION_CLOSED,
                market_date=session.session_date.isoformat(),
                session_open=session.open_utc,
                session_close=session.close_utc,
            )
            return None
        report.session_state = SESSION_OPEN
        return session

    def _run_symbols(
        self, session: MarketSession, moment: datetime, report: ShadowCycleReport
    ) -> None:
        latest = latest_completed_session_bar(
            session, now=moment, safety_delay=self._config.safety_delay
        )
        if latest is None:
            log_event(
                self._logger,
                "no_completed_bar",
                at=moment,
                market_date=session.session_date.isoformat(),
            )
            return
        # The regime state is resolved before any symbol is evaluated: without
        # it EDA-1's decision for this session is undefined, and recording V3
        # alone would break the side-by-side invariant that every observed bar
        # carries both answers. A failure here skips the whole cycle - miss an
        # observation rather than record half of one.
        self._ensure_regime_state(session, moment)
        frames = self._market_data.recent_bars(
            SHADOW_PROCESSING_ORDER,
            now=moment,
            latest_bar_start=latest,
            lookback_bars=self._config.lookback_bars,
        )
        for symbol in SHADOW_PROCESSING_ORDER:
            if self._shutdown.requested:
                log_event(self._logger, "cycle_interrupted", symbol=symbol, reason="shutdown")
                break
            try:
                report.results.append(
                    self._observe_symbol(symbol, session, frames.get(symbol), moment)
                )
            except ShadowIntegrityError:
                raise
            except (EquityError, SessionError, ShadowError, state.StateError) as error:
                self._record_symbol_error(symbol, error, report)
            except Exception as error:  # noqa: BLE001 - a defect, not a refusal
                self._record_symbol_error(symbol, error, report)
                self._heartbeat.state = RuntimeState.FAILED
                report.fatal = True
                break

    def _ensure_regime_state(self, session: MarketSession, moment: datetime) -> None:
        """Resolve and persist the EDA-1 state governing `session`, once.

        The state is a deterministic function of completed-session closes
        strictly before the session, so it is computed at most once per
        session and read back on every later cycle - a restart mid-session
        reuses the stored answer rather than asking the provider again.
        """
        day = session.session_date
        existing = self._connection.execute(
            "SELECT participate FROM shadow_regime_state WHERE session_date = ?",
            (day.isoformat(),),
        ).fetchone()
        if existing is not None:
            return
        frame = self._regime_data.state_frame(
            before=day,
            now=moment,
            sessions=self._config.state_sessions,
        )
        if frame is None or frame.empty:
            raise EquityError(
                f"The reference symbol {REGIME_REFERENCE_SYMBOL} returned no completed "
                f"bars before {day.isoformat()}, so the regime state governing this "
                "session cannot be resolved. Nothing was evaluated."
            )
        closes = session_closes(frame)
        resolved = state_for_session(closes, self._spec, session_date=day)
        with state.transaction(self._connection):
            self._connection.execute(
                "INSERT INTO shadow_regime_state ("
                " session_date, participate, info_close, info_sma, info_drawdown,"
                " sessions_observed, sma_sessions, calm_threshold, lag_sessions,"
                " reference_symbol, computed_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    day.isoformat(),
                    int(resolved.participate),
                    resolved.info_close,
                    resolved.info_sma,
                    resolved.info_drawdown,
                    resolved.sessions_observed,
                    self._spec.sma_sessions,
                    self._spec.calm_threshold,
                    self._spec.lag_sessions,
                    REGIME_REFERENCE_SYMBOL,
                    state.to_utc_text(moment, "computed_at"),
                ),
            )
        log_event(
            self._logger,
            "regime_state_resolved",
            session=day.isoformat(),
            participate=resolved.participate,
            info_close=resolved.info_close,
            info_sma=resolved.info_sma,
            info_drawdown=resolved.info_drawdown,
            sessions_observed=resolved.sessions_observed,
            reference=REGIME_REFERENCE_SYMBOL,
        )

    def _observe_symbol(
        self,
        symbol: str,
        session: MarketSession,
        frame: pd.DataFrame | None,
        now: datetime,
    ) -> ShadowSymbolResult:
        """Validate, trim, claim, evaluate with V3, record - and release nothing."""
        if frame is None or frame.empty:
            log_event(self._logger, "no_bars", symbol=symbol, at=now)
            return ShadowSymbolResult(symbol=symbol, skipped_reason="NO_BARS")

        validation = validate_frame(
            frame,
            supported_symbols=SHADOW_PROCESSING_ORDER,
            universe_label=EQUITY_UNIVERSE_LABEL,
        )
        if not validation.valid:
            raise EquityError(
                f"Bars for {symbol} failed validation, so nothing was evaluated: "
                f"{_describe_validation(validation)}"
            )

        bars = self._completed_session_bars(symbol, session, frame, now)
        if bars.latest is None:
            return ShadowSymbolResult(symbol=symbol, skipped_reason="NO_BAR_THIS_SESSION")

        self._recorder.begin_bar(symbol, reference_close=float(bars.frame["close"].iloc[-1]))
        outcome = self._cycles[symbol].evaluate_bar(symbol, bars.frame, bar_timestamp=bars.latest)
        if not outcome.claimed:
            log_event(
                self._logger,
                "bar_already_processed",
                symbol=symbol,
                timestamp=bars.latest,
                reason=outcome.skipped_reason,
            )
            return ShadowSymbolResult(
                symbol=symbol,
                bar_timestamp=bars.latest,
                skipped_reason=outcome.skipped_reason or SKIPPED_ALREADY_PROCESSED,
            )
        self._heartbeat.last_processed_bars[symbol] = bars.latest
        return self._record_observation(symbol, bars, outcome)

    def _record_observation(
        self, symbol: str, bars: _SymbolBars, outcome: BarOutcome
    ) -> ShadowSymbolResult:
        """Log the decision in full, and drop the candidate on the floor, loudly."""
        evaluation = outcome.evaluation
        assert evaluation is not None  # guarded by `outcome.claimed`
        observation = evaluation.observation_for(SHADOW_ENGINE_VERSION)
        reference_close = float(bars.frame["close"].iloc[-1])
        signal_value: str | None = None
        eda1_signal_value: str | None = None
        if observation is not None:
            result = observation.result
            signal_value = result.signal.value
            eda1 = self._recorder.last_eda1.get(symbol)
            participate = self._recorder.last_participate.get(symbol)
            eda1_signal_value = eda1.signal.value if eda1 is not None else None
            log_event(
                self._logger,
                "shadow_decision",
                symbol=symbol,
                bar_timestamp=outcome.bar_timestamp,
                engine=result.version,
                signal=result.signal.value,
                score=float(result.score),
                confidence=float(result.confidence),
                regime=result.regime.value,
                reasons=" ".join(result.reasons),
                feature_version=observation.feature_version,
                model_version=observation.model_version,
                eda1_signal=eda1_signal_value,
                eda1_regime=eda1.regime if eda1 is not None else None,
                eda1_reasons=" ".join(eda1.reasons) if eda1 is not None else None,
                participate=participate,
                signals_agree=(
                    eda1_signal_value == signal_value if eda1_signal_value is not None else None
                ),
                reference_close=reference_close,
                code_sha=self._config.code_sha,
                order_created="NO_ORDER_PATH",
            )
        for failure in evaluation.failures:
            log_event(
                self._logger,
                "shadow_engine_failure",
                level=logging.WARNING,
                symbol=symbol,
                bar_timestamp=outcome.bar_timestamp,
                engine=failure.version,
                error=failure.error,
            )

        candidate = outcome.candidate
        if candidate is not None:
            # The one moment a trading runtime would hand the decision onward.
            # This runtime records that the moment occurred and does nothing,
            # because there is nothing here that could be done with it.
            log_event(
                self._logger,
                "shadow_candidate_dropped",
                symbol=symbol,
                bar_timestamp=outcome.bar_timestamp,
                signal=candidate.signal.value,
                reason="SHADOW_HAS_NO_EXECUTION_PATH",
            )
        return ShadowSymbolResult(
            symbol=symbol,
            bar_timestamp=(
                outcome.bar_timestamp if isinstance(outcome.bar_timestamp, datetime) else None
            ),
            recorded=True,
            signal=signal_value,
            eda1_signal=eda1_signal_value,
            candidate_dropped=candidate is not None,
        )

    def _completed_session_bars(
        self,
        symbol: str,
        session: MarketSession,
        frame: pd.DataFrame,
        now: datetime,
    ) -> _SymbolBars:
        """The frame trimmed to completed bars, plus the newest one to observe.

        The trading runtime's three conditions, unchanged: the newest bar's
        interval has fully elapsed, it belongs to today's session, and it sits
        on the regular-session grid. An extended-hours candle stamped today is
        refused rather than rounded into place.
        """
        timestamps = [require_utc(value, "bar timestamp") for value in frame["timestamp"]]
        completed = [
            index
            for index, timestamp in enumerate(timestamps)
            if is_bar_complete(timestamp, now=now, safety_delay=self._config.safety_delay)
        ]
        if not completed:
            log_event(self._logger, "no_completed_bar", symbol=symbol, at=now)
            return _SymbolBars()

        trimmed = frame.iloc[completed].reset_index(drop=True)
        latest = timestamps[completed[-1]]
        if market_date(latest) != session.session_date:
            log_event(
                self._logger,
                "no_bar_this_session",
                symbol=symbol,
                newest=latest,
                market_date=session.session_date.isoformat(),
            )
            return _SymbolBars(frame=trimmed)
        if not is_regular_session_bar(session, latest):
            raise EquityError(
                f"The newest completed {symbol} bar is stamped {latest.isoformat()}, "
                f"which is not a regular-session 15-minute bar of the "
                f"{session.session_date.isoformat()} session. Refusing to evaluate an "
                "extended-hours candle rather than rounding it into place."
            )
        return _SymbolBars(frame=trimmed, latest=latest)

    # ------------------------------------------------------------------
    # Bookkeeping
    # ------------------------------------------------------------------

    def _record_cycle_audit(self, moment: datetime, report: ShadowCycleReport) -> None:
        state.record_system_event(
            self._connection,
            event_timestamp=moment,
            event_type=EVENT_SHADOW_CYCLE,
            message=(
                f"Shadow cycle: {report.recorded_count} bar(s) recorded side by side "
                f"({SHADOW_ENGINE_VERSION} and {EDA1_ENGINE_VERSION}, replay-verified), "
                f"{report.dropped_candidates} actionable candidate(s) dropped "
                "unexecuted, 0 order intents in this database (verified)."
            ),
        )

    def _record_symbol_error(
        self, symbol: str, error: BaseException, report: ShadowCycleReport
    ) -> None:
        detail = f"{type(error).__name__}: {error}"
        report.error = detail
        self._heartbeat.last_error = detail
        log_event(
            self._logger,
            "cycle_error",
            level=logging.ERROR,
            symbol=symbol,
            error=detail,
        )

    def _record_cycle_error(self, error: BaseException, report: ShadowCycleReport) -> None:
        detail = f"{type(error).__name__}: {error}"
        report.error = detail
        self._heartbeat.last_error = detail
        if not isinstance(error, (EquityError, SessionError, ShadowError, state.StateError)):
            self._heartbeat.state = RuntimeState.FAILED
            report.fatal = True
        log_event(
            self._logger,
            "cycle_error",
            level=logging.ERROR,
            severity="FATAL" if report.fatal else "RETRY_NEXT_CYCLE",
            error=detail,
        )


__all__ = [
    "DEFAULT_SHADOW_LOOKBACK_BARS",
    "DEFAULT_STATE_SESSIONS",
    "EQUITY_SHADOW_LOCK_SCOPE",
    "EVENT_SHADOW_CYCLE",
    "EVENT_SHADOW_STARTED",
    "EVENT_SHADOW_STOPPED",
    "MAX_SHADOW_LOOKBACK_BARS",
    "MAX_STATE_SESSIONS",
    "MIN_SHADOW_LOOKBACK_BARS",
    "MIN_STATE_SESSIONS",
    "NO_SESSION_TODAY",
    "SESSION_CLOSED",
    "SESSION_OPEN",
    "SHADOW_ENGINE_VERSION",
    "SHADOW_PROCESSING_ORDER",
    "V3_REQUIRED_BASE_BARS",
    "EquityShadowConfig",
    "EquityShadowRuntime",
    "RegimeBarSource",
    "RegimeEquityBars",
    "ShadowBarSource",
    "ShadowCycleReport",
    "ShadowEquityBars",
    "ShadowIntegrityError",
    "ShadowSymbolResult",
    "SideBySideShadowRecorder",
    "create_side_by_side_tables",
    "filter_to_shadow_sessions",
    "require_shadow_lookback_bars",
    "require_state_sessions",
]
