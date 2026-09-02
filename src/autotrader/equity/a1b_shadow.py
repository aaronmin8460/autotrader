"""A1-B U30 live shadow: real session, real decisions, no order path.

This runtime watches the frozen 26-name U30 universe on completed
regular-session 15-minute bars, runs the **V3 decision engine** on each
newest completed bar, resolves the **EDA-1 participation state** once per
session (reference-symbol completed-session closes, one session of lag), and
computes the **frozen A1-B archetype allocation** at each 21-session
rebalance mark — recording, per bar and per symbol, the hypothetical target
weight the A1-B U30 sleeve would hold. That is the whole job. It sizes
nothing, submits nothing, cancels nothing, and holds nothing that could.

**The guarantee is structural, not behavioural.** Like the validated V3 +
EDA-1 shadow this is modelled on, there is no seam an execution gateway
could be handed through: the constructor takes no execution argument, no
attribute holds one, and this module imports nothing from the execution
layer. Every recorded row carries the designation ``SIMULATED_SHADOW`` and a
NULL-forever order linkage; the database refuses to proceed if it ever holds
an order intent, re-verified after every cycle.

**Non-incumbent symbols are scored under the reference alias.** The V3
engine's input guard admits only the frozen ten-name equity universe; the
sixteen additional U30 names are therefore evaluated with their frames
labelled as the reference symbol — the exact alias mechanism two research
programs proved whole-record invariant — and recorded under their own
symbol with ``alias_scored = 1``.

**A1-B is frozen research policy, not a fit.** Archetype centroids, feature
list, multiplier rule, bounds, universe manifests, and the mark grid come
from the packaged research artifact (`a1b_policy.py`); this process never
refits anything. Weights change only at 21-session marks; DEFENSIVE
sessions hold equal reserved weight × the per-bar V3 stance.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import pandas as pd

from autotrader.data.validation import validate_frame
from autotrader.decision.contract import DecisionSignal
from autotrader.decision.v3 import MultiTimeframeV3Engine
from autotrader.equity import EquityError
from autotrader.equity.a1b_policy import (
    A1BFit,
    A1BPolicy,
    build_series,
    cross_sectional_z_at_mark,
    governing_fit,
    governing_mark,
    load_policy,
    mark_weights,
    structural_at,
    symbol_sessions,
)
from autotrader.equity.regime import (
    REGIME_REFERENCE_SYMBOL,
    ParticipationSpec,
    session_closes,
    state_for_session,
)
from autotrader.equity.session import (
    MarketCalendar,
    MarketSession,
    is_market_open,
    latest_completed_session_bar,
    lookback_window,
    market_date,
    recent_sessions,
    session_bar_mask,
)
from autotrader.equity.session import next_wake_time as next_session_wake_time
from autotrader.equity.shadow import (
    DEFAULT_SAFETY_DELAY,
    DEFAULT_SHADOW_LOOKBACK_BARS,
    DEFAULT_STATE_SESSIONS,
    RegimeBarSource,
    ShadowBarSource,
    ShadowIntegrityError,
    require_shadow_lookback_bars,
    require_state_sessions,
)
from autotrader.runtime.checkpoint import ProcessedBarCheckpoint, SqliteCheckpoint
from autotrader.runtime.monitoring import Heartbeat, RuntimeState, get_logger, log_event
from autotrader.runtime.runner import SHUTDOWN_POLL_SECONDS, ShutdownRequest
from autotrader.runtime.schedule import require_utc
from autotrader.state import sqlite as state

#: Session states, mirrored from the sibling shadow for identical reporting.
SESSION_OPEN = "OPEN"
SESSION_CLOSED = "CLOSED"
NO_SESSION_TODAY = "NO_SESSION"

#: Every recorded row carries this designation; nothing downstream exists.
DESIGNATION = "SIMULATED_SHADOW"

#: Completed sessions fetched for a mark's fingerprint window: 252 own-axis
#: sessions plus alignment/pairing headroom.
MARK_HISTORY_SESSIONS = 280

EVENT_A1B_SHADOW_STARTED = "EQUITY_A1B_SHADOW_STARTED"
EVENT_A1B_SHADOW_STOPPED = "EQUITY_A1B_SHADOW_STOPPED"

#: A lock scope of its own: this observer must never contend with — or be
#: mistaken for — the trading runtimes or the sibling shadow.
EQUITY_A1B_SHADOW_LOCK_SCOPE = "equity-a1b-shadow"

_A1B_TABLES = """
CREATE TABLE IF NOT EXISTS a1b_regime_state (
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
CREATE TABLE IF NOT EXISTS a1b_mark_state (
    mark_index INTEGER PRIMARY KEY,
    mark_date TEXT NOT NULL UNIQUE,
    fit_mark TEXT,
    labels_json TEXT NOT NULL,
    multipliers_json TEXT NOT NULL,
    active_weights_json TEXT NOT NULL,
    reserved_weights_json TEXT NOT NULL,
    labeled_symbols INTEGER NOT NULL,
    policy_hash TEXT NOT NULL,
    computed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS a1b_stance (
    symbol TEXT PRIMARY KEY,
    stance INTEGER NOT NULL CHECK (stance IN (0, 1)),
    bar_timestamp TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS a1b_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    bar_timestamp TEXT NOT NULL,
    session_date TEXT NOT NULL,
    participate INTEGER NOT NULL CHECK (participate IN (0, 1)),
    v3_signal TEXT NOT NULL,
    v3_stance INTEGER NOT NULL CHECK (v3_stance IN (0, 1)),
    alias_scored INTEGER NOT NULL CHECK (alias_scored IN (0, 1)),
    mark_index INTEGER NOT NULL,
    mark_date TEXT NOT NULL,
    archetype_label INTEGER,
    active_weight REAL NOT NULL,
    reserved_weight REAL NOT NULL,
    target_weight REAL NOT NULL,
    reference_close REAL NOT NULL,
    designation TEXT NOT NULL DEFAULT 'SIMULATED_SHADOW',
    client_order_id TEXT,
    recorded_at TEXT NOT NULL,
    UNIQUE (symbol, bar_timestamp),
    CHECK (designation = 'SIMULATED_SHADOW'),
    CHECK (client_order_id IS NULL)
);
"""


def create_a1b_tables(connection: sqlite3.Connection) -> None:
    """Create the A1-B shadow's own tables, idempotently."""
    connection.executescript(_A1B_TABLES)


class A1BUniverseBars:
    """The per-cycle `ShadowBarSource` for the 26-name observation universe.

    The sibling's `ShadowEquityBars` goes through the trading data boundary,
    whose closed ten-name whitelist this universe exceeds; the request here
    is therefore built directly against the historical stock-bars client —
    which has no order surface of any kind — with symbols validated against
    the frozen policy manifest instead. Bars are UNADJUSTED, exactly like the
    sibling's and the trading runtime's V3 windows, so the ten shared names'
    decisions stay comparable bar-for-bar.
    """

    def __init__(
        self,
        calendar: MarketCalendar,
        universe: tuple[str, ...],
        client: object | None = None,
    ) -> None:
        self._calendar = calendar
        self._universe = tuple(universe)
        self._client = client
        self.api_calls = 0

    def _resolve_client(self) -> object:
        if self._client is None:
            from autotrader.equity.data import create_client

            self._client = create_client()
        return self._client

    def recent_bars(
        self,
        symbols,
        *,
        now: datetime,
        latest_bar_start: datetime,
        lookback_bars: int,
    ) -> dict[str, pd.DataFrame]:
        """Fetch the bounded completed window for the whole universe at once."""
        from autotrader.data.historical import RESOLUTION
        from autotrader.equity.data import build_bars_request, to_canonical_frame
        from autotrader.equity.session import sessions_needed
        from autotrader.equity.shadow import filter_to_shadow_sessions

        count = require_shadow_lookback_bars(lookback_bars)
        latest = require_utc(latest_bar_start, "latest_bar_start")
        require_utc(now, "now")
        allowed = set(self._universe)
        tickers = []
        for symbol in symbols:
            normalized = str(symbol).strip().upper()
            if normalized not in allowed:
                raise EquityError(
                    f"{symbol!r} is not in the frozen U30 observation manifest; "
                    "this bar source refuses symbols outside the policy artifact."
                )
            tickers.append(normalized)
        sessions = recent_sessions(
            self._calendar,
            day=market_date(latest),
            count=sessions_needed(count),
        )
        start, end = lookback_window(sessions, latest_bar_start=latest)
        request = build_bars_request(tickers, start, end - RESOLUTION, None)
        self.api_calls += 1
        barset = self._resolve_client().get_stock_bars(request)
        data = getattr(barset, "data", None)
        returned = (
            {str(key): list(value) for key, value in data.items()} if isinstance(data, dict) else {}
        )
        return {
            ticker: filter_to_shadow_sessions(
                to_canonical_frame(returned.get(ticker, []), ticker),
                sessions,
                lookback_bars=count,
            )
            for ticker in tickers
        }


class A1BMarkBars:
    """Session-history source for mark fingerprints: one batched request per
    mark over the frozen 45-name z cross-section. The same market-data
    boundary as every other read here — no order surface of any kind."""

    def __init__(self, calendar: MarketCalendar, client: object | None = None) -> None:
        self._calendar = calendar
        self._client = client
        self.api_calls = 0

    def _resolve_client(self) -> object:
        if self._client is None:
            from autotrader.equity.data import create_client

            self._client = create_client()
        return self._client

    def history(
        self,
        symbols: tuple[str, ...],
        *,
        before: date,
        now: datetime,
    ) -> dict[str, pd.DataFrame]:
        """15m bars over the completed sessions strictly before `before`.

        This request is built directly rather than through the trading data
        boundary's `fetch_bars_for_symbols`, whose closed ten-name whitelist
        protects the trading system's risk arithmetic, API budget, and
        reconciliation scope — none of which this observation-only read can
        reach. Symbols are validated against the frozen policy cross-section
        instead, and the request is **split-adjusted** (the research
        fingerprint convention: raw bars turn a split into a phantom crash,
        which would corrupt every beta/vol/drawdown fingerprint).
        """
        from alpaca.data.enums import Adjustment

        from autotrader.data.historical import RESOLUTION
        from autotrader.equity.data import build_bars_request, to_canonical_frame

        require_utc(now, "now")
        allowed = set(symbols)
        tickers = []
        for symbol in symbols:
            normalized = symbol.strip().upper()
            if normalized not in allowed:
                raise EquityError(
                    f"{symbol!r} is not in the frozen fingerprint cross-section; "
                    "the mark fetch refuses symbols outside the policy artifact."
                )
            tickers.append(normalized)
        window = recent_sessions(
            self._calendar,
            day=before - timedelta(days=1),
            count=MARK_HISTORY_SESSIONS,
        )
        if not window or window[-1].session_date >= before:
            raise EquityError(
                f"No completed session window strictly before {before.isoformat()} "
                "could be resolved for the mark fingerprints; nothing was computed."
            )
        # The newest completed bar of the window's final session starts one
        # resolution before that session's close — the sibling convention.
        start, end = lookback_window(window, latest_bar_start=window[-1].close_utc - RESOLUTION)
        request = build_bars_request(tickers, start, end - RESOLUTION, Adjustment.SPLIT)
        self.api_calls += 1
        barset = self._resolve_client().get_stock_bars(request)
        data = getattr(barset, "data", None)
        returned = (
            {str(key): list(value) for key, value in data.items()} if isinstance(data, dict) else {}
        )
        out: dict[str, pd.DataFrame] = {}
        for ticker in tickers:
            frame = to_canonical_frame(returned.get(ticker, []), ticker)
            if frame.empty:
                out[ticker] = frame
                continue
            mask = session_bar_mask(window, list(frame["timestamp"]))
            out[ticker] = frame.loc[mask].reset_index(drop=True)
        return out


@dataclass
class A1BSymbolResult:
    """What one symbol's pass through one cycle produced."""

    symbol: str
    bar_timestamp: datetime | None = None
    recorded: bool = False
    signal: str | None = None
    stance: int | None = None
    target_weight: float | None = None
    skipped_reason: str | None = None


@dataclass
class A1BCycleReport:
    """What one whole cycle produced, plus the session it ran in."""

    started_at: datetime
    session_state: str = SESSION_CLOSED
    session: MarketSession | None = None
    results: list[A1BSymbolResult] = field(default_factory=list)
    error: str | None = None
    fatal: bool = False

    @property
    def recorded_count(self) -> int:
        return sum(1 for result in self.results if result.recorded)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class A1BShadowConfig:
    """How the loop runs. Nothing here decides or authorizes anything."""

    safety_delay: timedelta = DEFAULT_SAFETY_DELAY
    lookback_bars: int = DEFAULT_SHADOW_LOOKBACK_BARS
    state_sessions: int = DEFAULT_STATE_SESSIONS
    code_sha: str | None = None

    def __post_init__(self) -> None:
        require_shadow_lookback_bars(self.lookback_bars)
        require_state_sessions(self.state_sessions)


class EquityA1BShadowRuntime:
    """The A1-B U30 live shadow. Observes, records, and cannot trade.

    Construct it with an open state connection on the shadow's **own**
    database, a V3-sized bar source, a regime bar source, a mark-history
    source and a market calendar. There is no execution parameter, no
    gateway attribute, and no authorization to resolve; the zero-intent
    assertion below is a per-cycle checked invariant.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        market_data: ShadowBarSource,
        regime_data: RegimeBarSource,
        mark_data: A1BMarkBars,
        calendar: MarketCalendar,
        checkpoint: ProcessedBarCheckpoint | None = None,
        config: A1BShadowConfig | None = None,
        regime_spec: ParticipationSpec | None = None,
        policy: A1BPolicy | None = None,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
        shutdown: ShutdownRequest | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._connection = connection
        self._config = config if config is not None else A1BShadowConfig()
        self._market_data = market_data
        self._regime_data = regime_data
        self._mark_data = mark_data
        self._calendar = calendar
        self._checkpoint: ProcessedBarCheckpoint = (
            checkpoint if checkpoint is not None else SqliteCheckpoint(connection)
        )
        self._spec = regime_spec if regime_spec is not None else ParticipationSpec()
        self._policy = policy if policy is not None else load_policy()
        self._clock = clock
        self._sleep = sleep
        self._shutdown = shutdown if shutdown is not None else ShutdownRequest()
        self._logger = logger if logger is not None else get_logger()

        self._heartbeat = Heartbeat()
        self._heartbeat.last_processed_bars = {symbol: None for symbol in self._policy.u30}
        self._engines = {
            symbol: MultiTimeframeV3Engine.for_symbol(
                symbol if symbol in self._policy.incumbents else REGIME_REFERENCE_SYMBOL
            )
            for symbol in self._policy.u30
        }
        self._mark_cache: dict[date, tuple[int, date, tuple[dict, dict]]] = {}
        self._started = False

    # ------------------------------------------------------------------
    # Observable state
    # ------------------------------------------------------------------

    @property
    def heartbeat(self):
        return self._heartbeat.snapshot()

    @property
    def state(self) -> RuntimeState:
        return self._heartbeat.state

    @property
    def shutdown(self) -> ShutdownRequest:
        return self._shutdown

    @property
    def policy(self) -> A1BPolicy:
        return self._policy

    @property
    def universe(self) -> tuple[str, ...]:
        return self._policy.u30

    # ------------------------------------------------------------------
    # The zero-order invariant
    # ------------------------------------------------------------------

    def assert_no_order_intents(self) -> None:
        """Refuse to proceed if this database has ever held an order intent."""
        row = self._connection.execute("SELECT COUNT(*) FROM order_intents").fetchone()
        count = int(row[0])
        if count:
            raise ShadowIntegrityError(
                f"This database holds {count} order intent(s). The A1-B shadow "
                "records observations only and refuses a database that has ever "
                "ordered — it may be the trading database. Nothing was evaluated."
            )
        linked = self._connection.execute(
            "SELECT COUNT(*) FROM a1b_observations WHERE client_order_id IS NOT NULL"
        ).fetchone()
        if int(linked[0]):
            raise ShadowIntegrityError(
                "An A1-B observation row carries an order linkage; that cannot "
                "happen in this process and the record is not trustworthy."
            )

    def _require_consistent_policy(self) -> None:
        """Refuse a database whose mark states used a different policy."""
        rows = self._connection.execute(
            "SELECT DISTINCT policy_hash FROM a1b_mark_state"
        ).fetchall()
        for row in rows:
            if str(row[0]) != self._policy.policy_hash:
                raise ShadowIntegrityError(
                    f"This database holds mark states computed under policy "
                    f"{row[0][:12]}…, but this process ships policy "
                    f"{self._policy.policy_hash[:12]}…. Mixing them would make the "
                    "stored weight series unreproducible; refusing."
                )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        now = require_utc(self._clock(), "now")
        create_a1b_tables(self._connection)
        self.assert_no_order_intents()
        self._require_consistent_policy()
        self._heartbeat.state = RuntimeState.RUNNING
        self._heartbeat.started_at = now
        self._started = True
        state.record_system_event(
            self._connection,
            event_timestamp=now,
            event_type=EVENT_A1B_SHADOW_STARTED,
            message=(
                f"Equity A1-B U30 shadow started for {len(self._policy.u30)} symbols. "
                f"Policy {self._policy.policy_hash[:12]}, mark grid every "
                f"{self._policy.mark_every_sessions} sessions from "
                f"{self._policy.mark_anchor.isoformat()}, lookback "
                f"{self._config.lookback_bars} bars, code "
                f"{self._config.code_sha or 'unknown'}. This process holds no "
                "execution path: zero order mutation, verified per cycle."
            ),
        )
        log_event(
            self._logger,
            "a1b_shadow_started",
            started_at=now,
            symbols=",".join(self._policy.u30),
            policy_hash=self._policy.policy_hash,
            lookback_bars=self._config.lookback_bars,
            code_sha=self._config.code_sha,
            order_paths="NONE",
        )

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        now = require_utc(self._clock(), "now")
        if self._heartbeat.state is not RuntimeState.FAILED:
            self._heartbeat.state = RuntimeState.STOPPED
        state.record_system_event(
            self._connection,
            event_timestamp=now,
            event_type=EVENT_A1B_SHADOW_STOPPED,
            message=(
                f"Equity A1-B shadow stopped in state {self._heartbeat.state.value}. "
                "Orders submitted by this process: 0, by construction."
            ),
        )
        log_event(
            self._logger,
            "a1b_shadow_stopped",
            stopped_at=now,
            state=self._heartbeat.state,
            signal=self._shutdown.signal_name,
            cycles_started=self._heartbeat.cycles_started,
            cycles_completed=self._heartbeat.cycles_completed,
            orders_submitted=self._heartbeat.orders_submitted,
        )

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def run_once(self) -> A1BCycleReport:
        self.start()
        try:
            report = self.run_cycle()
        except BaseException:
            self._heartbeat.state = RuntimeState.FAILED
            self.stop()
            raise
        self.stop()
        return report

    def run_forever(self, *, max_cycles: int | None = None) -> list[A1BCycleReport]:
        self.start()
        reports: list[A1BCycleReport] = []
        try:
            while not self._shutdown.requested:
                if max_cycles is not None and len(reports) >= max_cycles:
                    break
                target = next_session_wake_time(
                    self._calendar,
                    now=self._clock(),
                    safety_delay=self._config.safety_delay,
                )
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

    def run_cycle(self, now: datetime | None = None) -> A1BCycleReport:
        """One cycle: session gate, regime + mark state, one batched fetch,
        twenty-six evaluations, zero orders."""
        moment = require_utc(now if now is not None else self._clock(), "now")
        report = A1BCycleReport(started_at=moment)
        self._heartbeat.cycles_started += 1
        self._heartbeat.last_cycle_started_at = moment
        log_event(
            self._logger,
            "cycle_started",
            at=moment,
            state=self._heartbeat.state,
            mode="A1B_SHADOW_OBSERVE_ONLY",
        )
        try:
            session = self._resolve_session(moment, report)
            if session is not None:
                self._run_symbols(session, moment, report)
        except ShadowIntegrityError:
            raise
        except Exception as error:  # noqa: BLE001 - classified rather than propagated
            report.error = f"{type(error).__name__}: {error}"
            self._heartbeat.last_error = report.error
            log_event(
                self._logger,
                "cycle_error",
                level=logging.ERROR,
                error=report.error,
            )
        if report.error is None:
            self._heartbeat.cycles_completed += 1
            self._heartbeat.last_successful_cycle_at = moment
        self.assert_no_order_intents()
        log_event(
            self._logger,
            "cycle_finished",
            at=moment,
            session=report.session_state,
            recorded=report.recorded_count,
            error=report.error,
            state=self._heartbeat.state,
        )
        log_event(self._logger, "heartbeat", **self._heartbeat.snapshot().as_fields())
        return report

    # ------------------------------------------------------------------
    # Cycle internals
    # ------------------------------------------------------------------

    def _resolve_session(self, moment: datetime, report: A1BCycleReport) -> MarketSession | None:
        open_now, session = is_market_open(self._calendar, now=moment)
        report.session = session
        if session is None:
            report.session_state = NO_SESSION_TODAY
            log_event(self._logger, "session_closed", at=moment, reason=NO_SESSION_TODAY)
            return None
        if not open_now:
            report.session_state = SESSION_CLOSED
            log_event(
                self._logger,
                "session_closed",
                at=moment,
                reason=SESSION_CLOSED,
                market_date=session.session_date.isoformat(),
            )
            return None
        report.session_state = SESSION_OPEN
        return session

    def _run_symbols(
        self, session: MarketSession, moment: datetime, report: A1BCycleReport
    ) -> None:
        latest = latest_completed_session_bar(
            session, now=moment, safety_delay=self._config.safety_delay
        )
        if latest is None:
            log_event(self._logger, "no_completed_bar", at=moment)
            return
        participate = self._ensure_regime_state(session, moment)
        mark_index, mark_date_value, weights = self._ensure_mark_state(session, moment)
        frames = self._market_data.recent_bars(
            list(self._policy.u30),
            now=moment,
            latest_bar_start=latest,
            lookback_bars=self._config.lookback_bars,
        )
        for symbol in self._policy.u30:
            if self._shutdown.requested:
                log_event(self._logger, "cycle_interrupted", symbol=symbol, reason="shutdown")
                break
            try:
                report.results.append(
                    self._observe_symbol(
                        symbol,
                        session,
                        frames.get(symbol),
                        moment,
                        participate=participate,
                        mark_index=mark_index,
                        mark_date_value=mark_date_value,
                        weights=weights,
                    )
                )
            except ShadowIntegrityError:
                raise
            except Exception as error:  # noqa: BLE001 - one symbol must not sink the cycle
                report.results.append(
                    A1BSymbolResult(
                        symbol=symbol, skipped_reason=f"{type(error).__name__}: {error}"
                    )
                )
                log_event(
                    self._logger,
                    "symbol_error",
                    level=logging.ERROR,
                    symbol=symbol,
                    error=f"{type(error).__name__}: {error}",
                )

    def _ensure_regime_state(self, session: MarketSession, moment: datetime) -> bool:
        day = session.session_date
        existing = self._connection.execute(
            "SELECT participate FROM a1b_regime_state WHERE session_date = ?",
            (day.isoformat(),),
        ).fetchone()
        if existing is not None:
            return bool(existing[0])
        frame = self._regime_data.state_frame(
            before=day, now=moment, sessions=self._config.state_sessions
        )
        if frame is None or frame.empty:
            raise EquityError(
                f"The reference symbol returned no completed bars before "
                f"{day.isoformat()}; the regime state cannot be resolved."
            )
        closes = session_closes(frame)
        resolved = state_for_session(closes, self._spec, session_date=day)
        with state.transaction(self._connection):
            self._connection.execute(
                "INSERT INTO a1b_regime_state ("
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
        )
        return resolved.participate

    def _reference_axis(self, day: date):
        """Calendar sessions from the grid reference mark through `day`.

        The research grid ran on the reference symbol's observed-session
        axis, which lacks one exchange session the calendar has; anchoring
        at the research grid's final mark makes the two grids identical at
        the handover and unambiguous forward.
        """
        sessions = self._calendar.sessions_between(self._policy.grid_reference_mark, day)
        if not sessions or sessions[0].session_date != self._policy.grid_reference_mark:
            raise EquityError(
                f"The calendar's session axis does not begin at the grid reference "
                f"mark {self._policy.grid_reference_mark.isoformat()}; the mark grid "
                "cannot be resolved."
            )
        if sessions[-1].session_date != day:
            raise EquityError(
                f"{day.isoformat()} is not on the calendar's session axis; the "
                "mark grid cannot be resolved."
            )
        return sessions

    def _ensure_mark_state(
        self, session: MarketSession, moment: datetime
    ) -> tuple[int, date, tuple[dict[str, float], dict[str, float]]]:
        """Resolve (and compute at most once) the mark governing `session`.

        The resolved answer is cached in memory per session date: the mark
        cannot change within a session, so later cycles of the same session
        cost neither a calendar call nor a database read.
        """
        day = session.session_date
        cached = self._mark_cache.get(day)
        if cached is not None:
            return cached
        axis = self._reference_axis(day)
        index = len(axis) - 1
        mark_index = governing_mark(self._policy, index)
        row = self._connection.execute(
            "SELECT mark_date, active_weights_json, reserved_weights_json"
            " FROM a1b_mark_state WHERE mark_index = ?",
            (mark_index,),
        ).fetchone()
        if row is not None:
            resolved = (
                mark_index,
                date.fromisoformat(row[0]),
                (json.loads(row[1]), json.loads(row[2])),
            )
            self._mark_cache[day] = resolved
            return resolved

        mark_day = axis[mark_index].session_date
        fit = governing_fit(self._policy, mark_day)
        z_by_symbol: dict[str, dict[str, float]] = {}
        labels: dict[str, int] = {}
        if fit is not None:
            frames = self._mark_data.history(
                self._policy.u45_z_cross_section, before=mark_day, now=moment
            )
            reference_frame = frames.get(REGIME_REFERENCE_SYMBOL)
            if reference_frame is None or reference_frame.empty:
                raise EquityError(
                    "The reference symbol returned no history for the mark "
                    "fingerprints; the mark state cannot be computed."
                )
            reference_table = symbol_sessions(reference_frame)
            values: dict[str, dict[str, float]] = {}
            for symbol in self._policy.u45_z_cross_section:
                frame = frames.get(symbol)
                if frame is None or frame.empty:
                    continue
                series = build_series(symbol_sessions(frame), reference_table)
                values[symbol] = structural_at(series, mark_day)
            z_by_symbol = cross_sectional_z_at_mark(
                values,
                self._policy.surviving_features,
                winsor=self._policy.z_winsor,
                min_symbols=self._policy.z_min_symbols,
            )
        active, reserved, labels = mark_weights(self._policy, fit, z_by_symbol)
        multipliers = a1b_multipliers_for_report(self._policy, fit)
        with state.transaction(self._connection):
            self._connection.execute(
                "INSERT INTO a1b_mark_state ("
                " mark_index, mark_date, fit_mark, labels_json, multipliers_json,"
                " active_weights_json, reserved_weights_json, labeled_symbols,"
                " policy_hash, computed_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    mark_index,
                    mark_day.isoformat(),
                    fit.fit_mark.isoformat() if fit is not None else None,
                    json.dumps(labels, sort_keys=True),
                    json.dumps(multipliers, sort_keys=True),
                    json.dumps(active, sort_keys=True),
                    json.dumps(reserved, sort_keys=True),
                    len(labels),
                    self._policy.policy_hash,
                    state.to_utc_text(moment, "computed_at"),
                ),
            )
        log_event(
            self._logger,
            "mark_state_resolved",
            mark_index=mark_index,
            mark_date=mark_day.isoformat(),
            fit_mark=fit.fit_mark.isoformat() if fit is not None else None,
            labeled_symbols=len(labels),
        )
        resolved = (mark_index, mark_day, (active, reserved))
        self._mark_cache[day] = resolved
        return resolved

    def _observe_symbol(
        self,
        symbol: str,
        session: MarketSession,
        frame: pd.DataFrame | None,
        now: datetime,
        *,
        participate: bool,
        mark_index: int,
        mark_date_value: date,
        weights: tuple[dict[str, float], dict[str, float]],
    ) -> A1BSymbolResult:
        if frame is None or frame.empty:
            log_event(self._logger, "no_bars", symbol=symbol, at=now)
            return A1BSymbolResult(symbol=symbol, skipped_reason="NO_BARS")
        validation = validate_frame(
            frame,
            supported_symbols=self._policy.u30,
            universe_label="the A1-B U30 observation universe",
        )
        if not validation.valid:
            raise EquityError(
                f"Bars for {symbol} failed validation, so nothing was evaluated: "
                + "; ".join(str(issue) for issue in validation.errors)
            )
        # The bar source already filtered to regular-session bars and capped
        # the window at the configured lookback; the frame is used as-is.
        trimmed = frame
        if trimmed.empty:
            return A1BSymbolResult(symbol=symbol, skipped_reason="NO_SESSION_BARS")
        latest = pd.Timestamp(trimmed["timestamp"].iloc[-1]).to_pydatetime()
        if market_date(latest) != session.session_date:
            return A1BSymbolResult(symbol=symbol, skipped_reason="NO_BAR_THIS_SESSION")
        already = self._checkpoint.last_processed(symbol)
        if already is not None and latest <= already:
            return A1BSymbolResult(
                symbol=symbol, bar_timestamp=latest, skipped_reason="ALREADY_PROCESSED"
            )
        self._checkpoint.mark_processed(symbol, latest)
        self._heartbeat.last_processed_bars[symbol] = latest

        alias_scored = symbol not in self._policy.incumbents
        scored = trimmed
        if alias_scored:
            scored = trimmed.copy()
            scored["symbol"] = REGIME_REFERENCE_SYMBOL
        result = self._engines[symbol].decide(scored)
        stance = self._advance_stance(symbol, result.signal, latest, now)

        active, reserved = weights
        active_weight = float(active.get(symbol, 0.0))
        reserved_weight = float(reserved.get(symbol, 0.0))
        target = active_weight if participate else reserved_weight * stance
        label_row = self._connection.execute(
            "SELECT labels_json FROM a1b_mark_state WHERE mark_index = ?",
            (mark_index,),
        ).fetchone()
        label = None
        if label_row is not None:
            label = json.loads(label_row[0]).get(symbol)
        reference_close = float(trimmed["close"].iloc[-1])
        with state.transaction(self._connection):
            self._connection.execute(
                "INSERT INTO a1b_observations ("
                " symbol, bar_timestamp, session_date, participate, v3_signal,"
                " v3_stance, alias_scored, mark_index, mark_date, archetype_label,"
                " active_weight, reserved_weight, target_weight, reference_close,"
                " designation, client_order_id, recorded_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    symbol,
                    state.to_utc_text(latest, "bar_timestamp"),
                    session.session_date.isoformat(),
                    int(participate),
                    result.signal.value,
                    stance,
                    int(alias_scored),
                    mark_index,
                    mark_date_value.isoformat(),
                    label,
                    active_weight,
                    reserved_weight,
                    target,
                    reference_close,
                    DESIGNATION,
                    state.to_utc_text(now, "recorded_at"),
                ),
            )
        return A1BSymbolResult(
            symbol=symbol,
            bar_timestamp=latest,
            recorded=True,
            signal=result.signal.value,
            stance=stance,
            target_weight=target,
        )

    def _advance_stance(
        self, symbol: str, signal: DecisionSignal, bar_timestamp: datetime, now: datetime
    ) -> int:
        row = self._connection.execute(
            "SELECT stance FROM a1b_stance WHERE symbol = ?", (symbol,)
        ).fetchone()
        stance = int(row[0]) if row is not None else 0
        if signal is DecisionSignal.BUY:
            stance = 1
        elif signal is DecisionSignal.SELL:
            stance = 0
        with state.transaction(self._connection):
            self._connection.execute(
                "INSERT INTO a1b_stance (symbol, stance, bar_timestamp, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT (symbol) DO UPDATE SET"
                " stance = excluded.stance, bar_timestamp = excluded.bar_timestamp,"
                " updated_at = excluded.updated_at",
                (
                    symbol,
                    stance,
                    state.to_utc_text(bar_timestamp, "bar_timestamp"),
                    state.to_utc_text(now, "updated_at"),
                ),
            )
        return stance


def a1b_multipliers_for_report(policy: A1BPolicy, fit: A1BFit | None) -> dict[str, float]:
    """label → multiplier as a JSON-friendly mapping (empty pre-initial-fit)."""
    if fit is None:
        return {}
    from autotrader.equity.a1b_policy import a1b_multipliers

    return {str(label): value for label, value in a1b_multipliers(policy, fit).items()}


__all__ = [
    "DESIGNATION",
    "EVENT_A1B_SHADOW_STARTED",
    "EVENT_A1B_SHADOW_STOPPED",
    "MARK_HISTORY_SESSIONS",
    "A1BCycleReport",
    "A1BMarkBars",
    "A1BUniverseBars",
    "A1BShadowConfig",
    "A1BSymbolResult",
    "EquityA1BShadowRuntime",
    "create_a1b_tables",
]
