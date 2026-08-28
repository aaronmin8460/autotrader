"""C9: the 24/7 crypto runtime. The loop that operates BTC/USD and ETH/USD.

One synchronous process. It wakes on completed 15-minute UTC boundaries,
fetches a bounded window of recent completed bars for each pair in a fixed
order, validates them, evaluates the existing EMA 20 / EMA 50 strategy, records
the newest completed bar's signal, and - only when every gate is open - hands
that signal to the existing C7 paper execution path.

**No asyncio.** Two symbols, one cycle every fifteen minutes, one broker that
must be spoken to one order at a time. An event loop here would add a
concurrency model to a problem that has no concurrency in it, and would make
"BTC is submitted before ETH is even considered" an emergent property instead
of a written one.

**No polling.** The runtime sleeps until the next boundary. It does not poll
the provider, it does not poll the account, and it does not poll positions.
Account, position, asset and price reads happen inside C7, once, and only when
a signal actually needs sizing.

**Fail closed, in three different directions.**

*Startup.* Broker submission is off until an external startup-safety check
says otherwise, and the shipped default says `UNRESOLVED` because Phase 8 is
not integrated here. Observation continues; trading does not begin.

*Gates.* Unattended paper execution needs the `AUTOTRADER_PAPER_TRADING_ENABLED`
environment gate **and** a runtime-start `PAPER` confirmation that authorizes
this one process for its lifetime. Neither substitutes for the other, and
`--observe-only` removes the execution gateway entirely so submission is not
merely refused but unexpressible.

*Ambiguity.* An `UNKNOWN` submission outcome pauses trading permanently for
this process. Nothing here resolves it, retries it, or reasons about what the
broker might have done - that is Phase 8's, and guessing is how one uncertain
order becomes two real ones.

**Every boundary is injected.** The clock, the sleep, the market data, the
execution path, the startup-safety check and the processed-bar checkpoint are
all constructor arguments, so the entire loop is testable offline and no test
ever waits fifteen real minutes or reaches a network.

There is no reconciliation here, no live mode, no equity symbol, no market
session, and no deployment artefact. See docs/SPEC.md section 8.
"""

from __future__ import annotations

import logging
import signal
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import FrameType

import pandas as pd

from autotrader.backtest.engine import STRATEGY_NAME
from autotrader.data.historical import SUPPORTED_SYMBOLS, HistoricalDataError
from autotrader.data.validation import ValidationResult, validate_frame
from autotrader.execution.models import ExecutionError, OrderSide, format_quantity
from autotrader.execution.paper import (
    AccountNotTradableError,
    AmbiguousSubmissionError,
    ExecutionOutcome,
    MissingCredentialsError,
    PaperExecutionResult,
    paper_trading_enabled,
)
from autotrader.runtime.checkpoint import InMemoryCheckpoint, ProcessedBarCheckpoint
from autotrader.runtime.execution import BrokerAuthenticationError, ExecutionGateway
from autotrader.runtime.market_data import MarketDataSource
from autotrader.runtime.monitoring import (
    Heartbeat,
    HeartbeatSnapshot,
    RuntimeState,
    get_logger,
    log_event,
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
    is_boundary,
    next_wake_time,
    require_lookback_bars,
    require_safety_delay,
    require_utc,
)
from autotrader.state import sqlite as state
from autotrader.strategies.ema_cross import (
    Signal,
    SignalType,
    StrategyInputError,
    generate_ema_cross_signals,
)

#: The processing order, fixed and total. BTC/USD is fully finished - risk
#: sized against the account as it stands, order submitted or refused - before
#: ETH/USD is looked at, so two signals landing on the same boundary can never
#: size themselves against the same stale cash and exposure figures.
PROCESSING_ORDER: tuple[str, ...] = SUPPORTED_SYMBOLS

#: The token that authorizes one runtime process to use the paper execution
#: path for its lifetime. The same word C7's manual command requires, because
#: it authorizes the same thing; a daemon simply cannot be asked to have it
#: typed again every fifteen minutes.
RUNTIME_CONFIRMATION_TOKEN = "PAPER"

#: The `strategy_runs.mode` a runtime session is recorded under.
RUNTIME_RUN_MODE = "PAPER"

#: How long the wait between cycles sleeps at a time. Not a poll: nothing is
#: fetched, asked, or read in a slice - it only bounds how long a SIGTERM waits
#: for the loop to notice it.
SHUTDOWN_POLL_SECONDS = 1.0

#: The quantity a signal requests from the risk engine.
#:
#: The runtime has no sizing policy. C5 is the only sizing authority in the
#: system, and its contract is explicit that an oversized BUY is *clamped* to
#: the safe maximum rather than refused, and an oversized SELL is clamped to
#: the position. So a signal asks for a quantity larger than any position this
#: account can hold or any ceiling this policy can approve, and the risk
#: engine's answer - never this module's - is the size that reaches the broker.
#:
#: Naming a smaller number here would be inventing a second sizing rule in the
#: layer least qualified to hold one, and it would silently cap entries below
#: the risk engine's own limit without anything saying so.
RISK_SIZED_REQUEST_QUANTITY = Decimal("1E9")

#: Audit event types this runtime writes to `system_events`.
EVENT_RUNTIME_STARTED = "RUNTIME_STARTED"
EVENT_RUNTIME_STOPPED = "RUNTIME_STOPPED"
EVENT_RUNTIME_TRADING_PAUSED = "RUNTIME_TRADING_PAUSED"


class CycleSeverity(Enum):
    """How a failure inside one cycle must be handled.

    Deliberately three values and no exception hierarchy. The question a
    running daemon has to answer is only ever "wait, stop trading, or stop" -
    a taxonomy finer than that would be shape without content.
    """

    #: Controlled, local, and expected to pass: a provider hiccup, a bad
    #: batch of bars, a strategy input violation. No order was created.
    RETRY_NEXT_CYCLE = "RETRY_NEXT_CYCLE"

    #: The broker's view and this process's view may disagree. Observation may
    #: continue; submission may not, ever again in this process.
    TRADING_PAUSED = "TRADING_PAUSED"

    #: The runtime cannot safely continue at all.
    FATAL = "FATAL"


class RuntimeCycleError(Exception):
    """Base for runtime failures that carry their own severity."""

    severity = CycleSeverity.RETRY_NEXT_CYCLE


class BarDataError(RuntimeCycleError):
    """The bars for one symbol cannot be trusted, so nothing is done with them."""

    severity = CycleSeverity.RETRY_NEXT_CYCLE


class RuntimeConfigError(Exception):
    """The runtime was configured in a way it will not start with."""


def classify(error: BaseException) -> CycleSeverity:
    """Decide what one failure means for the process.

    The mapping is small and explicit, and its two interesting entries are the
    ones that are not "retry":

    `AmbiguousSubmissionError` pauses trading. An order may exist at the
    broker; continuing to submit would be building on an unknown position.

    A rejected credential, an account the broker will not let trade, and a
    broken local database are fatal. None of them improves by being retried
    every fifteen minutes, and a daemon that loops forever on one is worse than
    a process that stops and says so.
    """
    if isinstance(error, AmbiguousSubmissionError):
        return CycleSeverity.TRADING_PAUSED
    if isinstance(
        error,
        BrokerAuthenticationError | MissingCredentialsError | AccountNotTradableError,
    ):
        return CycleSeverity.FATAL
    if isinstance(error, state.StateError):
        return CycleSeverity.FATAL
    if isinstance(error, RuntimeCycleError):
        return error.severity
    if isinstance(error, ExecutionError | HistoricalDataError | StrategyInputError):
        return CycleSeverity.RETRY_NEXT_CYCLE
    return CycleSeverity.FATAL


@dataclass(frozen=True)
class RuntimeConfig:
    """Everything about how the loop runs, and nothing about what it decides."""

    safety_delay: timedelta = DEFAULT_SAFETY_DELAY
    lookback_bars: int = DEFAULT_LOOKBACK_BARS
    observe_only: bool = False
    runtime_confirmation: str | None = None

    def __post_init__(self) -> None:
        require_safety_delay(self.safety_delay)
        require_lookback_bars(self.lookback_bars)


@dataclass(frozen=True)
class ExecutionAuthorization:
    """Whether this process may submit, and the first reason it may not.

    Resolved once at startup from four independent conditions, every one of
    which defaults to closed. `reason` names the *first* failing condition in a
    fixed order so a status line is stable rather than dependent on how many
    gates happen to be shut.
    """

    enabled: bool
    reason: str | None

    @property
    def disabled(self) -> bool:
        return not self.enabled


@dataclass(frozen=True)
class SymbolCycleResult:
    """What one symbol's turn in one cycle produced."""

    symbol: str
    bar_timestamp: datetime | None = None
    processed: bool = False
    signal: Signal | None = None
    execution: PaperExecutionResult | None = None
    skipped_reason: str | None = None


@dataclass
class CycleReport:
    """What one whole cycle produced, in processing order."""

    started_at: datetime
    results: list[SymbolCycleResult] = field(default_factory=list)
    severity: CycleSeverity | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """Whether every symbol in the cycle completed without a failure."""
        return self.severity is None


class ShutdownRequest:
    """A SIGINT/SIGTERM flag the loop checks at its own safe points.

    The handler sets a boolean and returns. It does not raise, does not cancel
    anything, and does not touch the database: a signal can arrive in the
    middle of a broker call, and the only correct thing to do there is finish
    that call and stop afterwards.
    """

    def __init__(self) -> None:
        self.requested = False
        self.signal_name: str | None = None
        self._previous: dict[int, object] = {}

    def request(self, name: str = "manual") -> None:
        """Ask the loop to stop after its current safe point."""
        if not self.requested:
            self.requested = True
            self.signal_name = name

    def _handle(self, signal_number: int, frame: FrameType | None) -> None:
        self.request(signal.Signals(signal_number).name)

    def install(self) -> None:
        """Take over SIGINT and SIGTERM, remembering the previous handlers."""
        for number in (signal.SIGINT, signal.SIGTERM):
            self._previous[number] = signal.getsignal(number)
            signal.signal(number, self._handle)

    def restore(self) -> None:
        """Put the previous handlers back."""
        while self._previous:
            number, handler = self._previous.popitem()
            signal.signal(number, handler)  # type: ignore[arg-type]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _bar_timestamps(frame: pd.DataFrame) -> list[datetime]:
    """The frame's timestamps as timezone-aware UTC datetimes."""
    return [require_utc(value, "bar timestamp") for value in frame["timestamp"]]


def _describe_validation(result: ValidationResult) -> str:
    return "; ".join(str(issue) for issue in result.errors)


class CryptoRuntime:
    """The 24/7 crypto runtime.

    Construct it with an open state connection and a market-data source; pass
    an `ExecutionGateway` only when this process is meant to be able to trade.
    Call `run_once()` for a single completed-bar cycle or `run_forever()` for
    the daemon.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        market_data: MarketDataSource,
        execution: ExecutionGateway | None = None,
        startup_safety: StartupSafetyCheck = unresolved_startup_safety,
        checkpoint: ProcessedBarCheckpoint | None = None,
        config: RuntimeConfig | None = None,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
        shutdown: ShutdownRequest | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._connection = connection
        self._config = config if config is not None else RuntimeConfig()
        self._market_data = market_data
        # `--observe-only` does not refuse submission, it removes the thing that
        # could submit. A gateway that is not held cannot be called by a later
        # edit that forgets to check a flag.
        self._execution = None if self._config.observe_only else execution
        self._startup_safety = startup_safety
        self._checkpoint: ProcessedBarCheckpoint = (
            checkpoint if checkpoint is not None else InMemoryCheckpoint()
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

        Order matters only for which reason gets reported; every condition must
        hold for submission to be possible at all.
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

        Re-asked immediately before every execution attempt rather than trusted
        from startup, because two things can change afterwards: an ambiguous
        outcome pauses trading, and a shutdown request must stop new orders
        even mid-cycle.
        """
        if self._authorization.disabled:
            return False, self._authorization.reason
        if self._heartbeat.state is RuntimeState.TRADING_PAUSED:
            return False, "TRADING_PAUSED"
        if self._shutdown.requested:
            return False, "SHUTTING_DOWN"
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
        self._heartbeat.startup_safety_code = safety.code
        self._heartbeat.paper_execution_enabled = self._authorization.enabled
        self._heartbeat.execution_disabled_reason = self._authorization.reason
        self._started = True

        log_event(
            self._logger,
            "runtime_started",
            started_at=now,
            strategy_run_id=self._strategy_run_id,
            symbols=",".join(PROCESSING_ORDER),
            lookback_bars=self._config.lookback_bars,
            safety_delay_seconds=self._config.safety_delay.total_seconds(),
            startup_safety=safety.code,
            paper_execution_enabled=self._authorization.enabled,
            execution_disabled_reason=self._authorization.reason,
        )
        log_event(self._logger, "startup_safety", code=safety.code, message=safety.message)
        execution_status = (
            "enabled" if self._authorization.enabled else f"disabled ({self._authorization.reason})"
        )
        state.record_system_event(
            self._connection,
            event_timestamp=now,
            event_type=EVENT_RUNTIME_STARTED,
            message=(
                f"Crypto runtime started for {', '.join(PROCESSING_ORDER)}. "
                f"Startup safety {safety.code}; paper execution {execution_status}."
            ),
        )

    def stop(self, *, status: str | None = None) -> None:
        """Close the strategy run and log the shutdown. Idempotent.

        A run that ended paused or failed is recorded as `FAILED`, because it
        did not finish the job it was started for. Only a clean stop -
        `--once` completing, or a signal arriving at an idle loop - completes.
        """
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
            message=f"Crypto runtime stopped in state {self._heartbeat.state.value}.",
        )
        log_event(
            self._logger,
            "runtime_stopped",
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
                f"Trading paused: {reason}. No further order will be submitted by this "
                "process. Reconciliation (Phase 8) must resolve the outcome before "
                "trading resumes."
            ),
        )
        log_event(self._logger, "trading_paused", reason=reason)

    def log_heartbeat(self) -> None:
        """Emit the current health as one structured line."""
        log_event(self._logger, "heartbeat", **self._heartbeat.snapshot().as_fields())

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def run_once(self) -> CycleReport:
        """Process the current completed-bar cycle once and stop.

        Does not wait for a boundary: the newest completed bar at this instant
        is what a single cycle is about. Used by `--once`, by cron-like manual
        validation, and by every test that would otherwise have to wait fifteen
        minutes.
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

    def run_forever(self, *, max_cycles: int | None = None) -> list[CycleReport]:
        """Run cycles on completed 15-minute UTC boundaries until told to stop.

        Stops on SIGINT or SIGTERM, on a fatal failure, and when trading has
        been paused - a process that may no longer submit should not keep a
        24/7 slot warm pretending it can. `max_cycles` bounds the loop for
        tests; production passes nothing.
        """
        self.start()
        reports: list[CycleReport] = []
        try:
            while not self._shutdown.requested:
                if max_cycles is not None and len(reports) >= max_cycles:
                    break
                target = next_wake_time(self._clock(), safety_delay=self._config.safety_delay)
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
        """Sleep until `target`, in slices, so a signal is noticed promptly.

        Nothing is fetched or asked during the wait. The slices exist only so
        SIGTERM does not have to wait out a fifteen-minute sleep.
        """
        while not self._shutdown.requested:
            remaining = (target - require_utc(self._clock(), "now")).total_seconds()
            if remaining <= 0:
                return
            self._sleep(min(remaining, SHUTDOWN_POLL_SECONDS))

    def run_cycle(self, now: datetime | None = None) -> CycleReport:
        """Process one completed-bar cycle: BTC/USD first, then ETH/USD.

        Each symbol is processed to completion before the next one starts, and
        a controlled failure on one does not stop the other - but a paused or
        fatal outcome ends the cycle immediately rather than carrying on to a
        second broker interaction.
        """
        moment = require_utc(now if now is not None else self._clock(), "now")
        report = CycleReport(started_at=moment)
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

        for symbol in PROCESSING_ORDER:
            if self._shutdown.requested:
                log_event(self._logger, "cycle_interrupted", symbol=symbol, reason="shutdown")
                break
            try:
                report.results.append(self._process_symbol(symbol, moment))
            except Exception as error:  # noqa: BLE001 - classified rather than propagated
                severity = classify(error)
                self._record_cycle_error(symbol, error, severity, report)
                if severity is CycleSeverity.TRADING_PAUSED:
                    self._pause_trading(f"{type(error).__name__} on {symbol}")
                    break
                if severity is CycleSeverity.FATAL:
                    self._heartbeat.state = RuntimeState.FAILED
                    break

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
            api_calls=self._heartbeat.api_calls_last_cycle,
        )
        self.log_heartbeat()
        return report

    def _record_cycle_error(
        self,
        symbol: str,
        error: BaseException,
        severity: CycleSeverity,
        report: CycleReport,
    ) -> None:
        detail = f"{type(error).__name__}: {error}"
        report.severity = severity
        report.error = detail
        self._heartbeat.last_error = detail
        log_event(
            self._logger,
            "cycle_error",
            level=logging.ERROR,
            symbol=symbol,
            severity=severity,
            error=detail,
        )

    def _collect_api_calls(self) -> None:
        """Add up the provider calls the injected boundaries report making.

        Best effort and duck-typed: a boundary that does not count is simply
        not counted. This is instrumentation for a later shared crypto+equity
        API budget, not a control.
        """
        total = 0
        for boundary in (self._market_data, self._execution):
            count = getattr(boundary, "api_calls", None)
            if isinstance(count, int):
                total += count
        self._heartbeat.api_calls_last_cycle = total - self._heartbeat.api_calls_total
        self._heartbeat.api_calls_total = total

    # ------------------------------------------------------------------
    # One symbol
    # ------------------------------------------------------------------

    def _process_symbol(self, symbol: str, now: datetime) -> SymbolCycleResult:
        """Fetch, validate, evaluate, record, and - if allowed - execute."""
        frame = self._market_data.recent_bars(
            symbol, now=now, lookback_bars=self._config.lookback_bars
        )

        validation = validate_frame(frame)
        if not validation.valid:
            raise BarDataError(
                f"Bars for {symbol} failed validation, so nothing was evaluated and no "
                f"order was created: {_describe_validation(validation)}"
            )

        timestamps = _bar_timestamps(frame)
        completed_positions = [
            index
            for index, timestamp in enumerate(timestamps)
            if is_bar_complete(timestamp, now=now, safety_delay=self._config.safety_delay)
        ]
        if not completed_positions:
            log_event(self._logger, "no_completed_bar", symbol=symbol, at=now)
            return SymbolCycleResult(symbol=symbol, skipped_reason="NO_COMPLETED_BAR")

        completed = frame.iloc[completed_positions].reset_index(drop=True)
        latest_timestamp = timestamps[completed_positions[-1]]

        if not is_boundary(latest_timestamp):
            raise BarDataError(
                f"The newest completed {symbol} bar is stamped {latest_timestamp.isoformat()}, "
                "which is not a 15-minute UTC boundary. Refusing to evaluate bars whose "
                "interval cannot be reasoned about rather than rounding them into place."
            )

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

        # Claimed before anything is decided or sent. A failure after this
        # point must not hand the same bar to the strategy a second time: one
        # completed bar is one decision, and a retry of the decision is how a
        # second order gets placed for a crossover that happened once.
        self._checkpoint.mark_processed(symbol, latest_timestamp)
        self._heartbeat.last_processed_bars[symbol] = latest_timestamp
        log_event(
            self._logger,
            "bar_processed",
            symbol=symbol,
            timestamp=latest_timestamp,
            bars=len(completed),
        )

        signal_for_bar = self._latest_bar_signal(completed, latest_timestamp)
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

    def _latest_bar_signal(self, bars: pd.DataFrame, latest_timestamp: datetime) -> Signal | None:
        """The signal on the newest completed bar, or None.

        The lookback exists to give the recursive EMA its state, not to be
        replayed. Every crossover older than this bar has already happened,
        was already acted on or already missed, and re-emitting it now would
        turn a restart into a burst of stale orders. Only the newest completed
        bar may cause an action.
        """
        signals = generate_ema_cross_signals(bars)
        if not signals:
            return None
        newest = signals[-1]
        if require_utc(newest.timestamp, "signal timestamp") != latest_timestamp:
            return None
        return newest

    def _record_signal(self, signal_for_bar: Signal) -> None:
        """Persist the signal against this runtime's strategy run.

        A repeat of the same logical signal is the storage layer's invariant to
        enforce, and it enforces it by raising. That is not a runtime failure -
        it means the fact is already recorded - so it is logged and passed over.
        """
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
        """Hand one signal to the existing paper execution path.

        `EXIT` becomes a `SELL`, which the risk engine refuses outright when
        there is no position to reduce - that refusal is an ordinary no-order
        result, not an error. Nothing here fabricates an order simply because a
        bar was processed.
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


__all__ = [
    "EVENT_RUNTIME_STARTED",
    "EVENT_RUNTIME_STOPPED",
    "EVENT_RUNTIME_TRADING_PAUSED",
    "PROCESSING_ORDER",
    "RISK_SIZED_REQUEST_QUANTITY",
    "RUNTIME_CONFIRMATION_TOKEN",
    "RUNTIME_RUN_MODE",
    "SHUTDOWN_POLL_SECONDS",
    "BarDataError",
    "CryptoRuntime",
    "CycleReport",
    "CycleSeverity",
    "ExecutionAuthorization",
    "RuntimeConfig",
    "RuntimeConfigError",
    "RuntimeCycleError",
    "ShutdownRequest",
    "SymbolCycleResult",
    "classify",
]
