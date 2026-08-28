"""Equity V0.2: the regular-session equity runtime. The loop that operates the ten.

Conceptually parallel to the 24/7 crypto runtime and deliberately **not** the
same object. The two share everything that is genuinely shared - the durable
processed-bar checkpoint, the single-instance lock, the startup-safety
boundary, the heartbeat and structured logging, the failure classification, the
shutdown flag - by importing it. What differs is scheduling, and scheduling is
most of what a runtime *is* here, so it is written out rather than hidden
behind a policy object that would have to be read twice to understand either
product.

**Regular US market hours only, and that is enforced twice.** A cycle that
starts while the session is shut does nothing at all: no fetch, no strategy, no
checkpoint, no submission, and no provider call. Then, at the far end, the
execution boundary refuses again against the broker's own clock. Two checks
because they answer different questions - "should this process be working right
now?" and "is this order legal at this instant?" - and because the second one
catches a cycle that started inside the session and ran past the close.

**The calendar is the authority.** Holidays, weekends and early closes are read
from the broker, never assumed. `Mon-Fri 09:30-16:00` is wrong about a dozen
days a year and this module contains no such rule.

**One request per cycle for ten symbols.** The universe is fetched in a single
batched market-data call before the per-symbol loop begins. Batching bars is
not the same thing as batching *orders*: symbols are still processed strictly
in order, one finished before the next is looked at, and there is never more
than one broker submission in flight.

**One completed bar, one action, across restarts.** The per-symbol bar
checkpoint is claimed *before* the strategy sees the bar, durably, in the same
`runtime_checkpoints` table the crypto runner uses. Equity tickers and crypto
pairs cannot collide there - `SPY` is not `BTC/USD` - so the two products share
a table without sharing a row. The safety preference is unchanged and
one-sided: **miss a trade rather than duplicate a trade.**

**Fail closed, in the same three directions.** Startup safety must come back
safe, both paper gates must be open, and an `UNKNOWN` submission outcome pauses
trading permanently for this process.

**No asyncio, no polling, no live mode, no deployment artefact.** Ten symbols,
one cycle every fifteen minutes, one broker that must be spoken to one order at
a time.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

import pandas as pd

from autotrader.account import safety as account_safety
from autotrader.backtest.engine import STRATEGY_NAME
from autotrader.data.validation import (
    EQUITY_UNIVERSE_LABEL,
    ValidationResult,
    validate_frame,
)
from autotrader.equity import EQUITY_SYMBOLS, EquityError
from autotrader.equity.market_data import EquityBarSource
from autotrader.equity.session import (
    MarketCalendar,
    MarketSession,
    SessionError,
    is_market_open,
    is_regular_session_bar,
    market_date,
)
from autotrader.equity.session import next_wake_time as next_session_wake_time
from autotrader.execution.models import ExecutionError, OrderSide, format_quantity
from autotrader.execution.paper import (
    ExecutionOutcome,
    PaperExecutionResult,
    paper_trading_enabled,
)
from autotrader.runtime.checkpoint import ProcessedBarCheckpoint, SqliteCheckpoint
from autotrader.runtime.monitoring import (
    Heartbeat,
    HeartbeatSnapshot,
    RuntimeState,
    get_logger,
    log_event,
)
from autotrader.runtime.runner import (
    RISK_SIZED_REQUEST_QUANTITY,
    RUNTIME_CONFIRMATION_TOKEN,
    RUNTIME_RUN_MODE,
    SHUTDOWN_POLL_SECONDS,
    BarDataError,
    CycleReport,
    CycleSeverity,
    ExecutionAuthorization,
    RuntimeConfigError,
    ShutdownRequest,
    SymbolCycleResult,
    classify,
)
from autotrader.runtime.safety import (
    StartupSafetyCheck,
    StartupSafetyResult,
    unresolved_startup_safety,
)
from autotrader.runtime.schedule import (
    DEFAULT_LOOKBACK_BARS,
    DEFAULT_SAFETY_DELAY,
    is_bar_complete,
    require_lookback_bars,
    require_safety_delay,
    require_utc,
)
from autotrader.state import sqlite as state
from autotrader.strategies.ema_cross import (
    Signal,
    SignalType,
    generate_ema_cross_signals,
)

#: The processing order, fixed and total. One symbol is finished - risk sized
#: against the account as it stands, order submitted or refused - before the
#: next is looked at, so ten signals landing on the same bar can never size
#: themselves against the same stale cash and exposure figures.
PROCESSING_ORDER: tuple[str, ...] = EQUITY_SYMBOLS

#: The lock scope that keeps the future equity service from colliding with the
#: crypto one while still stopping a second *equity* runner. Two products, one
#: account, two processes; two runners of the same product remains an error.
EQUITY_LOCK_SCOPE = "equity"

#: Where a cycle found itself in the market calendar.
#:
#: The two closed states are kept distinct because they call for different
#: operator responses: `SESSION_CLOSED` means today is a trading day and this
#: is not trading time, while `NO_SESSION_TODAY` means the broker's calendar
#: has no session on this exchange date at all - a weekend, or a holiday.
SESSION_OPEN = "SESSION_OPEN"
SESSION_CLOSED = "SESSION_CLOSED"
NO_SESSION_TODAY = "NO_SESSION_TODAY"

#: Audit event types this runtime writes to `system_events`.
EVENT_RUNTIME_STARTED = "EQUITY_RUNTIME_STARTED"
EVENT_RUNTIME_STOPPED = "EQUITY_RUNTIME_STOPPED"
EVENT_RUNTIME_TRADING_PAUSED = "EQUITY_RUNTIME_TRADING_PAUSED"


def classify_equity(error: BaseException) -> CycleSeverity:
    """Decide what one failure means for an equity process.

    The crypto classification is the base and is not re-derived: an ambiguous
    submission still pauses trading, a rejected credential and an untradable
    account and a broken database are still fatal, and everything C7 treats as
    controlled is still a retry. The one thing added is that an equity-specific
    controlled failure - an unreadable calendar, a provider refusal, a symbol
    the broker will not quote - is a wait rather than a stop.
    """
    severity = classify(error)
    if severity is CycleSeverity.FATAL and isinstance(error, EquityError):
        return CycleSeverity.RETRY_NEXT_CYCLE
    return severity


@dataclass(frozen=True)
class EquityRuntimeConfig:
    """Everything about how the loop runs, and nothing about what it decides."""

    safety_delay: timedelta = DEFAULT_SAFETY_DELAY
    lookback_bars: int = DEFAULT_LOOKBACK_BARS
    observe_only: bool = False
    runtime_confirmation: str | None = None

    def __post_init__(self) -> None:
        require_safety_delay(self.safety_delay)
        require_lookback_bars(self.lookback_bars)


@dataclass
class EquityCycleReport(CycleReport):
    """What one whole cycle produced, plus the session it ran in.

    The session state is part of the report rather than only a log line,
    because "nothing happened" and "nothing happened *because the market is
    shut*" are different results and a caller has to be able to tell them
    apart.
    """

    session_state: str = SESSION_CLOSED
    session: MarketSession | None = None


class EquityExecutionGateway(Protocol):
    """How the equity runtime reaches the paper execution path.

    A protocol so a test can substitute the broker boundary wholesale, and so
    the runtime holds no Alpaca type. The runtime never calls this unless every
    gate is open; the gateway is not where authorization is decided.
    """

    def execute(
        self,
        connection: sqlite3.Connection,
        *,
        symbol: str,
        side: str,
        requested_quantity: Decimal,
        strategy_run_id: int | None,
        now: datetime,
    ) -> PaperExecutionResult:
        """Run one equity paper execution attempt for one symbol."""


class PaperEquityExecutionGateway:
    """The production gateway. A thin call through to the equity boundary.

    Adds no trading behaviour: it constructs no trading client of its own,
    contains no `paper` keyword, builds no order request, and makes no sizing
    decision. It re-raises a rejected credential the same way the crypto
    gateway does, so a daemon stops rather than retrying a broken key every
    fifteen minutes.
    """

    def __init__(
        self,
        trading_client: object | None = None,
        data_client: object | None = None,
    ) -> None:
        self._trading_client = trading_client
        self._data_client = data_client
        #: Execution attempts made, for the later API-budget work. Each one is
        #: several provider calls inside the execution boundary.
        self.api_calls = 0

    def execute(
        self,
        connection: sqlite3.Connection,
        *,
        symbol: str,
        side: str,
        requested_quantity: Decimal,
        strategy_run_id: int | None,
        now: datetime,
    ) -> PaperExecutionResult:
        from alpaca.common.exceptions import APIError

        from autotrader.execution.equity import (
            create_market_data_client,
            execute_equity_paper_order,
        )
        from autotrader.execution.paper import create_paper_trading_client
        from autotrader.runtime.execution import (
            BrokerAuthenticationError,
            is_authentication_failure,
        )

        if self._trading_client is None:
            self._trading_client = create_paper_trading_client()
        if self._data_client is None:
            self._data_client = create_market_data_client()
        self.api_calls += 1
        try:
            return execute_equity_paper_order(
                connection,
                symbol=symbol,
                side=side,
                requested_quantity=requested_quantity,
                trading_client=self._trading_client,  # type: ignore[arg-type]
                data_client=self._data_client,  # type: ignore[arg-type]
                strategy_run_id=strategy_run_id,
                now=now,
            )
        except APIError as error:
            if is_authentication_failure(error):
                raise BrokerAuthenticationError(
                    "The broker rejected the configured credentials. This is not a "
                    "transient failure, so the runtime stops rather than retrying it "
                    "every fifteen minutes."
                ) from None
            raise


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _describe_validation(result: ValidationResult) -> str:
    return "; ".join(str(issue) for issue in result.errors)


@dataclass
class _SymbolBars:
    """One symbol's completed regular-session bars for this cycle."""

    frame: pd.DataFrame = field(default_factory=pd.DataFrame)
    latest: datetime | None = None


class EquityRuntime:
    """The regular-session equity runtime.

    Construct it with an open state connection, a market-data source and a
    market calendar; pass an `EquityExecutionGateway` only when this process is
    meant to be able to trade. Call `run_once()` for a single cycle or
    `run_forever()` for the daemon.

    Two arguments carry the safety integration and both default
    closed-and-durable: `startup_safety` defaults to
    `unresolved_startup_safety` (production passes
    `startup_safety_from_reconciliation(connection, symbols=PROCESSING_ORDER)`)
    and `checkpoint` defaults to the durable `SqliteCheckpoint`.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        market_data: EquityBarSource,
        calendar: MarketCalendar,
        execution: EquityExecutionGateway | None = None,
        startup_safety: StartupSafetyCheck = unresolved_startup_safety,
        checkpoint: ProcessedBarCheckpoint | None = None,
        config: EquityRuntimeConfig | None = None,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
        shutdown: ShutdownRequest | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._connection = connection
        self._config = config if config is not None else EquityRuntimeConfig()
        self._market_data = market_data
        self._calendar = calendar
        # `--observe-only` does not refuse submission, it removes the thing that
        # could submit. A gateway that is not held cannot be called by a later
        # edit that forgets to check a flag.
        self._execution = None if self._config.observe_only else execution
        self._startup_safety = startup_safety
        self._checkpoint: ProcessedBarCheckpoint = (
            checkpoint if checkpoint is not None else SqliteCheckpoint(connection)
        )
        self._clock = clock
        self._sleep = sleep
        self._shutdown = shutdown if shutdown is not None else ShutdownRequest()
        self._logger = logger if logger is not None else get_logger()

        self._heartbeat = Heartbeat()
        self._heartbeat.last_processed_bars = {symbol: None for symbol in PROCESSING_ORDER}
        self._authorization = ExecutionAuthorization(enabled=False, reason="NOT_STARTED")
        self._safety: StartupSafetyResult | None = None
        self._strategy_run_id: int | None = None
        self._started = False

    # ------------------------------------------------------------------
    # Observable state
    # ------------------------------------------------------------------

    @property
    def heartbeat(self) -> HeartbeatSnapshot:
        """The current health snapshot."""
        return self._heartbeat.snapshot()

    @property
    def state(self) -> RuntimeState:
        return self._heartbeat.state

    @property
    def authorization(self) -> ExecutionAuthorization:
        return self._authorization

    @property
    def startup_safety(self) -> StartupSafetyResult | None:
        """The startup answer this process got, or None before `start()`."""
        return self._safety

    @property
    def startup_safety_message(self) -> str:
        """Why this process may or may not trade, in one operator-readable line."""
        if self._safety is None:
            return "Startup safety has not been resolved yet."
        return self._safety.message

    @property
    def checkpoints(self) -> dict[str, datetime]:
        """The bar claims this runtime can currently see, per equity symbol.

        Read from the checkpoint itself rather than from the heartbeat, so a
        durable claim written by an *earlier* process shows up too. Filtered to
        this runtime's universe, because the same table also holds the crypto
        runner's rows and reporting those here would be reporting another
        process's state as this one's.
        """
        as_dict = getattr(self._checkpoint, "as_dict", None)
        if callable(as_dict):
            stored = dict(as_dict())
            return {symbol: stored[symbol] for symbol in PROCESSING_ORDER if symbol in stored}
        found: dict[str, datetime] = {}
        for symbol in PROCESSING_ORDER:
            claimed = self._checkpoint.last_processed(symbol)
            if claimed is not None:
                found[symbol] = claimed
        return found

    @property
    def strategy_run_id(self) -> int | None:
        return self._strategy_run_id

    @property
    def shutdown(self) -> ShutdownRequest:
        return self._shutdown

    # ------------------------------------------------------------------
    # Authorization
    # ------------------------------------------------------------------

    def _resolve_authorization(self, safety: StartupSafetyResult) -> ExecutionAuthorization:
        """Decide whether this process may submit, from four closed-by-default gates.

        Identical in shape and in order to the crypto runtime's, because it is
        the same decision about the same account. The session is deliberately
        **not** one of these gates: it changes during a process's lifetime, so
        it is asked per submission rather than resolved once at startup.
        """
        if self._config.observe_only or self._execution is None:
            return ExecutionAuthorization(False, "OBSERVE_ONLY")
        if not paper_trading_enabled():
            return ExecutionAuthorization(False, "PAPER_ENV_GATE_DISABLED")
        if self._config.runtime_confirmation != RUNTIME_CONFIRMATION_TOKEN:
            return ExecutionAuthorization(False, "RUNTIME_CONFIRMATION_MISSING")
        if not safety.safe_to_trade:
            return ExecutionAuthorization(False, f"STARTUP_SAFETY_{safety.code}")
        return ExecutionAuthorization(True, None)

    def _may_submit(self) -> tuple[bool, str | None]:
        """Whether a submission may happen *right now*.

        The last check reads durable state rather than this process's memory,
        and it is what makes "UNKNOWN from any asset = no new orders from any
        asset" true across processes. The crypto runtime hitting an ambiguous
        BTC/USD submission writes the halt; this runner sees it on its next
        in-session cycle and does not submit, without the two ever talking to
        each other. The execution boundary refuses independently as well, so
        removing this check would cost a clean status line rather than the
        guarantee itself.
        """
        if self._authorization.disabled:
            return False, self._authorization.reason
        if self._heartbeat.state is RuntimeState.TRADING_PAUSED:
            return False, "TRADING_PAUSED"
        if self._shutdown.requested:
            return False, "SHUTTING_DOWN"
        safety = account_safety.read_account_safety(self._connection)
        if not safety.safe_to_trade:
            return False, f"ACCOUNT_{safety.state}"
        return True, None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Resolve startup safety, open a strategy run, and log the configuration."""
        if self._started:
            return
        now = require_utc(self._clock(), "now")
        safety = self._startup_safety()
        if not isinstance(safety, StartupSafetyResult):
            raise RuntimeConfigError(
                "The startup safety check must return a StartupSafetyResult; a runtime "
                f"that cannot read its own safety answer must not trade. Got {safety!r}."
            )
        self._safety = safety
        self._authorization = self._resolve_authorization(safety)

        self._strategy_run_id = state.record_strategy_run(
            self._connection,
            strategy_name=STRATEGY_NAME,
            mode=RUNTIME_RUN_MODE,
            started_at=now,
        )

        self._heartbeat.state = RuntimeState.RUNNING
        self._heartbeat.started_at = now
        for symbol in PROCESSING_ORDER:
            self._heartbeat.last_processed_bars[symbol] = self._checkpoint.last_processed(symbol)
        self._heartbeat.startup_safety_code = safety.code
        self._heartbeat.reconciliation_status = safety.reconciliation_status
        self._heartbeat.paper_execution_enabled = self._authorization.enabled
        self._heartbeat.execution_disabled_reason = self._authorization.reason
        self._started = True

        log_event(
            self._logger,
            "equity_runtime_started",
            started_at=now,
            strategy_run_id=self._strategy_run_id,
            symbols=",".join(PROCESSING_ORDER),
            lookback_bars=self._config.lookback_bars,
            safety_delay_seconds=self._config.safety_delay.total_seconds(),
            startup_safety=safety.code,
            reconciliation_status=safety.reconciliation_status,
            paper_execution_enabled=self._authorization.enabled,
            execution_disabled_reason=self._authorization.reason,
        )
        log_event(
            self._logger,
            "startup_safety",
            level=logging.INFO if safety.safe_to_trade else logging.WARNING,
            code=safety.code,
            reconciliation_status=safety.reconciliation_status,
            message=safety.message,
        )
        execution_status = (
            "enabled" if self._authorization.enabled else f"disabled ({self._authorization.reason})"
        )
        state.record_system_event(
            self._connection,
            event_timestamp=now,
            event_type=EVENT_RUNTIME_STARTED,
            message=(
                f"Equity runtime started for {', '.join(PROCESSING_ORDER)}. "
                f"Startup safety {safety.code} "
                f"(reconciliation {safety.reconciliation_status or 'NOT RUN'}); "
                f"paper execution {execution_status}."
            ),
        )

    def stop(self, *, status: str | None = None) -> None:
        """Close the strategy run and log the shutdown. Idempotent."""
        if not self._started:
            return
        self._started = False
        now = require_utc(self._clock(), "now")
        if self._heartbeat.state not in (RuntimeState.TRADING_PAUSED, RuntimeState.FAILED):
            self._heartbeat.state = RuntimeState.STOPPED
        run_status = status if status is not None else self._run_status()
        if self._strategy_run_id is not None:
            try:
                state.finish_strategy_run(
                    self._connection, self._strategy_run_id, ended_at=now, status=run_status
                )
            except state.StateError as error:
                log_event(self._logger, "strategy_run_finish_failed", error=str(error))
        state.record_system_event(
            self._connection,
            event_timestamp=now,
            event_type=EVENT_RUNTIME_STOPPED,
            message=f"Equity runtime stopped in state {self._heartbeat.state.value}.",
        )
        log_event(
            self._logger,
            "equity_runtime_stopped",
            stopped_at=now,
            state=self._heartbeat.state,
            signal=self._shutdown.signal_name,
            cycles_started=self._heartbeat.cycles_started,
            cycles_completed=self._heartbeat.cycles_completed,
            orders_submitted=self._heartbeat.orders_submitted,
            api_calls_total=self._heartbeat.api_calls_total,
        )
        self.log_heartbeat()

    def _run_status(self) -> str:
        """The terminal `strategy_runs.status` matching the current state."""
        if self._heartbeat.state in (RuntimeState.FAILED, RuntimeState.TRADING_PAUSED):
            return state.RUN_STATUS_FAILED
        return state.RUN_STATUS_COMPLETED

    def _pause_trading(self, reason: str) -> None:
        """Enter `TRADING_PAUSED`. Nothing in this branch leaves it."""
        if self._heartbeat.state is RuntimeState.TRADING_PAUSED:
            return
        self._heartbeat.state = RuntimeState.TRADING_PAUSED
        now = require_utc(self._clock(), "now")
        state.record_system_event(
            self._connection,
            event_timestamp=now,
            event_type=EVENT_RUNTIME_TRADING_PAUSED,
            message=(
                f"Equity trading paused: {reason}. No further order will be submitted by "
                "this process. A new process must run startup reconciliation and get a "
                "safe_to_trade answer before trading resumes; nothing in this cycle "
                "resolves the outcome or submits again."
            ),
        )
        log_event(self._logger, "trading_paused", reason=reason)

    def log_heartbeat(self) -> None:
        """Emit the current health as one structured line."""
        log_event(self._logger, "heartbeat", **self._heartbeat.snapshot().as_fields())

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def run_once(self) -> EquityCycleReport:
        """Process the current cycle once and stop.

        Does not wait for a boundary, and does not wait for the market to open:
        a cycle run while the session is shut reports that honestly rather than
        blocking. Used by `--once`, by manual validation, and by every test
        that would otherwise have to wait fifteen minutes.
        """
        self.start()
        try:
            report = self.run_cycle()
        except BaseException:
            self._heartbeat.state = RuntimeState.FAILED
            self.stop()
            raise
        self.stop()
        return report

    def run_forever(self, *, max_cycles: int | None = None) -> list[EquityCycleReport]:
        """Run cycles on the session's own bar boundaries until told to stop.

        Sleeps to the next boundary *inside a session*, and over a closed market
        straight to the first actionable bar of the next one - so a weekend
        costs one sleep rather than two hundred no-op wake-ups.
        """
        self.start()
        reports: list[EquityCycleReport] = []
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
                        severity=CycleSeverity.FATAL,
                        error=self._heartbeat.last_error,
                    )
                    break
                log_event(self._logger, "cycle_scheduled", wake_at=target)
                self._wait_until(target)
                if self._shutdown.requested:
                    break
                report = self.run_cycle()
                reports.append(report)
                if report.severity in (CycleSeverity.FATAL, CycleSeverity.TRADING_PAUSED):
                    break
        finally:
            self.stop()
        return reports

    def _wait_until(self, target: datetime) -> None:
        """Sleep until `target`, in slices, so a signal is noticed promptly."""
        while not self._shutdown.requested:
            remaining = (target - require_utc(self._clock(), "now")).total_seconds()
            if remaining <= 0:
                return
            self._sleep(min(remaining, SHUTDOWN_POLL_SECONDS))

    def run_cycle(self, now: datetime | None = None) -> EquityCycleReport:
        """Process one cycle: the session gate first, then the ten in order.

        The session gate comes before everything, including the market-data
        fetch, so a closed market costs nothing at all - no provider call, no
        strategy evaluation, and no checkpoint write. Each symbol is processed
        to completion before the next one starts, and a controlled failure on
        one does not stop the others - but a paused or fatal outcome ends the
        cycle immediately rather than carrying on to another broker interaction.
        """
        moment = require_utc(now if now is not None else self._clock(), "now")
        report = EquityCycleReport(started_at=moment)
        self._heartbeat.cycles_started += 1
        self._heartbeat.last_cycle_started_at = moment
        self._heartbeat.api_calls_last_cycle = 0
        log_event(
            self._logger,
            "cycle_started",
            at=moment,
            state=self._heartbeat.state,
            paper_execution_enabled=self._authorization.enabled,
        )

        try:
            session = self._resolve_session(moment, report)
        except Exception as error:  # noqa: BLE001 - classified rather than propagated
            self._finish_cycle_with_error("calendar", error, report, moment)
            return report

        if session is not None:
            self._run_symbols(session, moment, report)

        if report.succeeded:
            self._heartbeat.cycles_completed += 1
            self._heartbeat.last_successful_cycle_at = moment
        self._collect_api_calls()
        log_event(
            self._logger,
            "cycle_finished",
            at=moment,
            severity=report.severity,
            state=self._heartbeat.state,
            session=report.session_state,
            api_calls=self._heartbeat.api_calls_last_cycle,
        )
        self.log_heartbeat()
        return report

    def _resolve_session(self, moment: datetime, report: EquityCycleReport) -> MarketSession | None:
        """The open session at `moment`, or None when this cycle must do nothing."""
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
        self, session: MarketSession, moment: datetime, report: EquityCycleReport
    ) -> None:
        """Fetch the universe once, then process each symbol in the fixed order."""
        try:
            frames = self._fetch_universe(session, moment)
        except Exception as error:  # noqa: BLE001 - classified rather than propagated
            self._finish_cycle_with_error("market-data", error, report, moment)
            return

        for symbol in PROCESSING_ORDER:
            if self._shutdown.requested:
                log_event(self._logger, "cycle_interrupted", symbol=symbol, reason="shutdown")
                break
            try:
                report.results.append(
                    self._process_symbol(symbol, session, frames.get(symbol), moment)
                )
            except Exception as error:  # noqa: BLE001 - classified rather than propagated
                severity = classify_equity(error)
                self._record_cycle_error(symbol, error, severity, report)
                if severity is CycleSeverity.TRADING_PAUSED:
                    self._pause_trading(f"{type(error).__name__} on {symbol}")
                    break
                if severity is CycleSeverity.FATAL:
                    self._heartbeat.state = RuntimeState.FAILED
                    break

    def _fetch_universe(self, session: MarketSession, moment: datetime) -> dict[str, pd.DataFrame]:
        """One batched market-data call for all ten symbols, or nothing.

        Anchored on the session's own newest completed bar rather than on the
        wall clock, so the window a provider is asked for is a window of
        regular-session bars and never reaches into the candle still forming.
        """
        from autotrader.equity.session import latest_completed_session_bar

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
            return {}
        return self._market_data.recent_bars(
            PROCESSING_ORDER,
            now=moment,
            latest_bar_start=latest,
            lookback_bars=self._config.lookback_bars,
        )

    def _finish_cycle_with_error(
        self,
        stage: str,
        error: BaseException,
        report: EquityCycleReport,
        moment: datetime,
    ) -> None:
        """Record a whole-cycle failure and bring the cycle to a clean stop."""
        severity = classify_equity(error)
        self._record_cycle_error(stage, error, severity, report)
        if severity is CycleSeverity.TRADING_PAUSED:
            self._pause_trading(f"{type(error).__name__} during {stage}")
        elif severity is CycleSeverity.FATAL:
            self._heartbeat.state = RuntimeState.FAILED
        self._collect_api_calls()
        log_event(
            self._logger,
            "cycle_finished",
            at=moment,
            severity=report.severity,
            state=self._heartbeat.state,
            session=report.session_state,
            api_calls=self._heartbeat.api_calls_last_cycle,
        )
        self.log_heartbeat()

    def _record_cycle_error(
        self,
        subject: str,
        error: BaseException,
        severity: CycleSeverity,
        report: EquityCycleReport,
    ) -> None:
        detail = f"{type(error).__name__}: {error}"
        report.severity = severity
        report.error = detail
        self._heartbeat.last_error = detail
        log_event(
            self._logger,
            "cycle_error",
            level=logging.ERROR,
            symbol=subject,
            severity=severity,
            error=detail,
        )

    def _collect_api_calls(self) -> None:
        """Add up the provider calls the injected boundaries report making.

        Best effort and duck-typed. The calendar counts too: it is a real
        provider call and the later shared crypto+equity budget has to see it.
        """
        total = 0
        for boundary in (self._market_data, self._calendar, self._execution):
            count = getattr(boundary, "api_calls", None)
            if isinstance(count, int):
                total += count
        self._heartbeat.api_calls_last_cycle = total - self._heartbeat.api_calls_total
        self._heartbeat.api_calls_total = total

    # ------------------------------------------------------------------
    # One symbol
    # ------------------------------------------------------------------

    def _process_symbol(
        self,
        symbol: str,
        session: MarketSession,
        frame: pd.DataFrame | None,
        now: datetime,
    ) -> SymbolCycleResult:
        """Validate, evaluate, record, and - if allowed - execute one symbol."""
        if frame is None or frame.empty:
            log_event(self._logger, "no_bars", symbol=symbol, at=now)
            return SymbolCycleResult(symbol=symbol, skipped_reason="NO_BARS")

        validation = validate_frame(
            frame,
            supported_symbols=PROCESSING_ORDER,
            universe_label=EQUITY_UNIVERSE_LABEL,
        )
        if not validation.valid:
            raise BarDataError(
                f"Bars for {symbol} failed validation, so nothing was evaluated and no "
                f"order was created: {_describe_validation(validation)}"
            )

        bars = self._completed_session_bars(symbol, session, frame, now)
        if bars.latest is None:
            return SymbolCycleResult(symbol=symbol, skipped_reason="NO_BAR_THIS_SESSION")
        latest_timestamp = bars.latest

        already = self._checkpoint.last_processed(symbol)
        if already is not None and latest_timestamp <= already:
            log_event(
                self._logger,
                "bar_already_processed",
                symbol=symbol,
                timestamp=latest_timestamp,
                last_processed=already,
            )
            return SymbolCycleResult(
                symbol=symbol,
                bar_timestamp=latest_timestamp,
                skipped_reason="ALREADY_PROCESSED",
            )

        # Claimed - durably, in production - before anything is decided or
        # sent. A failure after this point must not hand the same bar to the
        # strategy a second time: one completed bar is one decision, and a
        # retry of the decision is how a second order gets placed for a
        # crossover that happened once. The claim commits before the broker is
        # reachable from here, so a crash in between loses the trade rather
        # than duplicating it. That is the intended side of the trade.
        self._checkpoint.mark_processed(symbol, latest_timestamp)
        self._heartbeat.last_processed_bars[symbol] = latest_timestamp
        log_event(
            self._logger,
            "bar_processed",
            symbol=symbol,
            timestamp=latest_timestamp,
            bars=len(bars.frame),
        )

        signal_for_bar = self._latest_bar_signal(bars.frame, latest_timestamp)
        if signal_for_bar is None:
            log_event(self._logger, "no_signal", symbol=symbol, timestamp=latest_timestamp)
            return SymbolCycleResult(symbol=symbol, bar_timestamp=latest_timestamp, processed=True)

        self._record_signal(signal_for_bar)
        log_event(
            self._logger,
            "signal",
            symbol=symbol,
            timestamp=latest_timestamp,
            type=signal_for_bar.type.value,
            reason=signal_for_bar.reason,
        )

        allowed, reason = self._may_submit()
        if not allowed:
            log_event(
                self._logger,
                "execution_skipped",
                symbol=symbol,
                timestamp=latest_timestamp,
                type=signal_for_bar.type.value,
                reason=reason,
            )
            return SymbolCycleResult(
                symbol=symbol,
                bar_timestamp=latest_timestamp,
                processed=True,
                signal=signal_for_bar,
                skipped_reason=reason,
            )

        execution = self._execute(signal_for_bar, now)
        return SymbolCycleResult(
            symbol=symbol,
            bar_timestamp=latest_timestamp,
            processed=True,
            signal=signal_for_bar,
            execution=execution,
        )

    def _completed_session_bars(
        self,
        symbol: str,
        session: MarketSession,
        frame: pd.DataFrame,
        now: datetime,
    ) -> _SymbolBars:
        """The frame trimmed to completed bars, plus the newest one to act on.

        Three conditions, and the newest bar has to meet all of them: its whole
        interval has elapsed, it belongs to *today's* session, and it is one of
        that session's regular-hours bars.

        The order of the last two is the whole point. A newest bar from an
        *earlier* session is the ordinary case of a symbol with no prints yet
        this morning - benign, common on a single-venue feed, and answered by
        skipping the symbol rather than by acting on a crossover that was
        already old when the market shut. A newest bar stamped *today* but
        outside the regular grid is a different thing entirely: an
        extended-hours candle that reached the strategy, which is a data-contract
        violation this runtime refuses rather than rounds into place.
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
            raise BarDataError(
                f"The newest completed {symbol} bar is stamped {latest.isoformat()}, which "
                f"is not a regular-session 15-minute bar of the "
                f"{session.session_date.isoformat()} session. Refusing to evaluate an "
                "extended-hours candle rather than rounding it into place."
            )
        return _SymbolBars(frame=trimmed, latest=latest)

    def _latest_bar_signal(self, bars: pd.DataFrame, latest_timestamp: datetime) -> Signal | None:
        """The signal on the newest completed bar, or None.

        The lookback exists to give the recursive EMA its state, not to be
        replayed. Every crossover older than this bar has already happened, was
        already acted on or already missed, and re-emitting it now would turn a
        restart - or the first cycle of a new session - into a burst of stale
        orders across ten symbols at once.
        """
        signals = generate_ema_cross_signals(bars)
        if not signals:
            return None
        newest = signals[-1]
        if require_utc(newest.timestamp, "signal timestamp") != latest_timestamp:
            return None
        return newest

    def _record_signal(self, signal_for_bar: Signal) -> None:
        """Persist the signal against this runtime's strategy run."""
        if self._strategy_run_id is None:  # pragma: no cover - start() always sets it
            return
        try:
            state.record_signal(
                self._connection,
                strategy_run_id=self._strategy_run_id,
                signal_timestamp=require_utc(signal_for_bar.timestamp, "signal timestamp"),
                symbol=signal_for_bar.symbol,
                signal_type=signal_for_bar.type.value,
                reason=signal_for_bar.reason,
            )
        except state.DuplicateSignalError:
            log_event(
                self._logger,
                "signal_already_recorded",
                symbol=signal_for_bar.symbol,
                timestamp=signal_for_bar.timestamp,
            )

    def _execute(self, signal_for_bar: Signal, now: datetime) -> PaperExecutionResult:
        """Hand one signal to the equity paper execution path.

        `EXIT` becomes a `SELL`, which the risk engine refuses outright when
        there is no position to reduce - an ordinary no-order result, not an
        error. Nothing here fabricates an order simply because a bar was
        processed, and nothing here decides a size: the request is deliberately
        larger than any position this account can hold, and the risk engine's
        answer is what reaches the broker.
        """
        assert self._execution is not None  # guarded by `_may_submit`
        side = OrderSide.BUY if signal_for_bar.type is SignalType.BUY else OrderSide.SELL
        result = self._execution.execute(
            self._connection,
            symbol=signal_for_bar.symbol,
            side=side.value,
            requested_quantity=RISK_SIZED_REQUEST_QUANTITY,
            strategy_run_id=self._strategy_run_id,
            now=now,
        )
        self._log_execution(result)
        return result

    def _log_execution(self, result: PaperExecutionResult) -> None:
        decision = result.risk_decision
        if not decision.approved:
            log_event(
                self._logger,
                "risk_rejected",
                symbol=result.symbol,
                side=result.side.value,
                reason=decision.reason_code,
                message=decision.message,
            )
            return
        if result.outcome is ExecutionOutcome.SUBMITTED:
            self._heartbeat.orders_submitted += 1
        snapshot = result.broker_order
        log_event(
            self._logger,
            "paper_order_submitted"
            if result.outcome is ExecutionOutcome.SUBMITTED
            else "paper_order_result",
            symbol=result.symbol,
            side=result.side.value,
            outcome=result.outcome.value,
            risk_reason=decision.reason_code,
            quantity=(
                None
                if result.submitted_quantity is None
                else format_quantity(result.submitted_quantity)
            ),
            client_order_id=None if snapshot is None else snapshot.client_order_id,
            broker_order_id=None if snapshot is None else snapshot.broker_order_id,
            broker_status=None if snapshot is None else snapshot.status,
        )


def universe_symbols() -> Sequence[str]:
    """The universe this runtime processes, in order. For status reporting."""
    return PROCESSING_ORDER


__all__ = [
    "EQUITY_LOCK_SCOPE",
    "EVENT_RUNTIME_STARTED",
    "EVENT_RUNTIME_STOPPED",
    "EVENT_RUNTIME_TRADING_PAUSED",
    "PROCESSING_ORDER",
    "RISK_SIZED_REQUEST_QUANTITY",
    "RUNTIME_CONFIRMATION_TOKEN",
    "NO_SESSION_TODAY",
    "SESSION_CLOSED",
    "SESSION_OPEN",
    "EquityCycleReport",
    "EquityExecutionGateway",
    "EquityRuntime",
    "EquityRuntimeConfig",
    "ExecutionError",
    "PaperEquityExecutionGateway",
    "classify_equity",
    "universe_symbols",
]
