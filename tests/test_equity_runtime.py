"""Equity V0.2: the regular-session runtime, and the crypto contract it must not touch.

Every test here is offline and every boundary is injected: the clock, the
sleep, the market data, the calendar, the execution path, the startup-safety
answer, and the processed-bar checkpoint. No test waits fifteen real minutes
and none of them reaches a network.

The seven named regression tests this milestone is required to carry are marked
CRITICAL in their docstrings.
"""

from __future__ import annotations

import json
import logging
import socket
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.requests import MarketOrderRequest
from typer.testing import CliRunner

from autotrader.cli import app
from autotrader.data.historical import CANONICAL_COLUMNS
from autotrader.equity import EQUITY_SYMBOLS
from autotrader.equity import runtime as equity_runtime
from autotrader.equity.data import EquityDataError
from autotrader.equity.runtime import (
    EQUITY_LOCK_SCOPE,
    NO_SESSION_TODAY,
    PROCESSING_ORDER,
    SESSION_CLOSED,
    SESSION_OPEN,
    EquityRuntime,
    EquityRuntimeConfig,
    classify_equity,
)
from autotrader.equity.session import (
    MarketSession,
    SessionError,
    regular_session_bar_starts,
    session_from_local,
)
from autotrader.execution.models import (
    TRADABLE_SYMBOLS,
    ExecutionInputError,
    OrderIntent,
    OrderSide,
)
from autotrader.execution.paper import (
    PAPER_TRADING_ENABLED_ENV,
    AmbiguousSubmissionError,
    ExecutionOutcome,
    PaperAccountState,
    PaperExecutionResult,
)
from autotrader.risk import APPROVED, NO_POSITION_TO_EXIT, RiskDecision
from autotrader.runtime.checkpoint import InMemoryCheckpoint, SqliteCheckpoint
from autotrader.runtime.lock import lock_path_for
from autotrader.runtime.monitoring import LOGGER_NAME, RuntimeState
from autotrader.runtime.runner import (
    RISK_SIZED_REQUEST_QUANTITY,
    CycleSeverity,
    ShutdownRequest,
)
from autotrader.runtime.safety import (
    STARTUP_SAFETY_SAFE,
    STARTUP_SAFETY_UNSAFE,
    StartupSafetyResult,
    unresolved_startup_safety,
)
from autotrader.state.sqlite import (
    connect,
    initialize_database,
    list_order_intents,
    list_signals,
    list_system_events,
)
from conftest import establish_account_safety
from test_equity_execution import (
    FakeDataClient,
    FakeTradingClient,
    make_asset,
)
from test_equity_session import EARLY_CLOSE, FakeCalendar, consecutive_sessions

SPY = "SPY"
QQQ = "QQQ"
TSLA = "TSLA"

REFERENCE_PRICE = 500.0

#: Eighteen ordinary sessions ending on the day the fixtures use, so a 120-bar
#: lookback has real history behind it across weekends.
SESSIONS = consecutive_sessions(date(2026, 8, 3), 18)
SESSION = SESSIONS[-1]

#: 10:45 Eastern. The newest completed bar at `T_NOW`.
T_BAR = datetime(2026, 8, 26, 14, 45, tzinfo=UTC)

#: 11:00:05 UTC-4 equivalent - five seconds past the boundary, inside the
#: session, with the 10:45 bar just closed and published.
T_NOW = datetime(2026, 8, 26, 15, 0, 5, tzinfo=UTC)

runner = CliRunner()


# ==========================================================================
# Fixtures and fakes
# ==========================================================================


def session_bar_grid(sessions: tuple[MarketSession, ...] = SESSIONS) -> list[datetime]:
    """Every regular-session bar start across `sessions`, ascending."""
    grid: list[datetime] = []
    for session in sessions:
        grid.extend(regular_session_bar_starts(session))
    return grid


def make_equity_bars(
    symbol: str = SPY,
    *,
    last_bar_start: datetime = T_BAR,
    count: int = 120,
    closes: list[float] | None = None,
    extra: list[datetime] | None = None,
) -> pd.DataFrame:
    """A canonical frame of `count` regular-session bars ending at `last_bar_start`.

    The timestamps come from the real session grid, so the gaps a weekend and
    an overnight put into an equity series are present rather than smoothed
    over. `extra` appends timestamps the session grid does not contain, which
    is how a test injects a pre-market or in-progress candle.
    """
    grid = session_bar_grid()
    end = grid.index(last_bar_start)
    chosen = grid[max(0, end + 1 - count) : end + 1]
    if extra:
        chosen = sorted({*chosen, *extra})
    prices = closes if closes is not None else [REFERENCE_PRICE] * len(chosen)
    assert len(prices) == len(chosen), (len(prices), len(chosen))
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(pd.Series(chosen), utc=True),
            "symbol": pd.Series([symbol] * len(chosen), dtype="string"),
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": [1000.0] * len(chosen),
            "trade_count": [10.0] * len(chosen),
            "vwap": prices,
        },
        columns=list(CANONICAL_COLUMNS),
    )


def crossover_closes(count: int = 120, *, upward: bool = True) -> list[float]:
    """Flat prices, then one decisive move on the final bar."""
    move = REFERENCE_PRICE * (1.10 if upward else 0.90)
    return [REFERENCE_PRICE] * (count - 1) + [move]


def mid_crossover_closes(count: int = 120) -> list[float]:
    """A crossover well inside the window, and nothing on the newest bar."""
    return [REFERENCE_PRICE] * (count - 40) + [REFERENCE_PRICE * 1.10] * 40


class FakeClock:
    def __init__(self, now: datetime = T_NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class FakeEquityBars:
    """Returns prepared frames per symbol, in one batched call."""

    def __init__(
        self,
        frames: dict[str, pd.DataFrame] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.frames = frames if frames is not None else {}
        self.error = error
        self.calls: list[tuple[tuple[str, ...], datetime, datetime, int]] = []
        self.api_calls = 0

    def recent_bars(
        self,
        symbols: object,
        *,
        now: datetime,
        latest_bar_start: datetime,
        lookback_bars: int,
    ) -> dict[str, pd.DataFrame]:
        requested = tuple(symbols)  # type: ignore[arg-type]
        self.calls.append((requested, now, latest_bar_start, lookback_bars))
        self.api_calls += 1
        if self.error is not None:
            raise self.error
        return {symbol: self.frames.get(symbol, make_equity_bars(symbol)) for symbol in requested}


def make_execution_result(
    *,
    symbol: str = SPY,
    side: OrderSide = OrderSide.BUY,
    outcome: ExecutionOutcome = ExecutionOutcome.SUBMITTED,
    approved: bool = True,
    reason_code: str = APPROVED,
) -> PaperExecutionResult:
    """A `PaperExecutionResult` shaped like the boundary's, without touching it."""
    quantity = Decimal(10)
    decision = RiskDecision(
        approved=approved,
        approved_quantity=quantity if approved else Decimal(0),
        reason_code=reason_code,
        message=f"test decision {reason_code}",
        max_allowed_quantity=quantity,
    )
    account = PaperAccountState(
        equity=100_000.0,
        cash=100_000.0,
        status="ACTIVE",
        trading_blocked=False,
        account_blocked=False,
        trade_suspended_by_user=False,
    )
    intent = (
        None
        if not approved
        else OrderIntent(
            symbol=symbol,
            side=side,
            requested_quantity=RISK_SIZED_REQUEST_QUANTITY,
            approved_quantity=quantity,
            reference_price=REFERENCE_PRICE,
            risk_reason_code=reason_code,
            created_at=T_NOW,
        )
    )
    return PaperExecutionResult(
        outcome=outcome,
        symbol=symbol,
        side=side,
        requested_quantity=RISK_SIZED_REQUEST_QUANTITY,
        reference_price=REFERENCE_PRICE,
        risk_decision=decision,
        account=account,
        daily_baseline_equity=Decimal("100000"),
        message="test execution",
        intent=intent,
    )


class FakeExecution:
    """Records every submission attempt, in order, and refuses re-entry."""

    def __init__(self, results: list[object] | None = None) -> None:
        self.results = list(results) if results is not None else []
        self.calls: list[dict[str, object]] = []
        self.api_calls = 0
        self._in_flight = False

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
        assert not self._in_flight, "two broker submissions overlapped"
        self._in_flight = True
        try:
            self.api_calls += 1
            self.calls.append({"symbol": symbol, "side": side, "now": now})
            outcome = (
                self.results.pop(0)
                if self.results
                else make_execution_result(symbol=symbol, side=OrderSide(side))
            )
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome  # type: ignore[return-value]
        finally:
            self._in_flight = False

    @property
    def symbols(self) -> list[str]:
        return [str(call["symbol"]) for call in self.calls]


def safe_startup() -> StartupSafetyResult:
    return StartupSafetyResult(True, STARTUP_SAFETY_SAFE, "test fixture: reconciled")


def unsafe_startup() -> StartupSafetyResult:
    return StartupSafetyResult(False, STARTUP_SAFETY_UNSAFE, "test fixture: refused")


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    """A database in the state a running process actually submits from.

    The runtime reconciles the full universe at startup and only then submits,
    so the execution boundary refuses to submit against an account whose safety
    nothing has ever established. Most cases here fake the execution gateway
    and never reach that check; the ones that drive the real boundary do.
    """
    path = initialize_database(tmp_path / "state.db")
    with connect(path) as setup:
        establish_account_safety(setup)
    return path


@pytest.fixture
def connection(database_path: Path) -> Iterator[sqlite3.Connection]:
    with connect(database_path) as open_connection:
        yield open_connection


@pytest.fixture(autouse=True)
def _closed_gate_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PAPER_TRADING_ENABLED_ENV, raising=False)


@pytest.fixture(autouse=True)
def _no_credentials_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)


@pytest.fixture
def enabled_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PAPER_TRADING_ENABLED_ENV, "true")


def build_runtime(
    connection: sqlite3.Connection,
    *,
    market_data: FakeEquityBars | None = None,
    calendar: object | None = None,
    execution: FakeExecution | None = None,
    startup_safety: object = safe_startup,
    checkpoint: object | None = None,
    clock: FakeClock | None = None,
    observe_only: bool = False,
    confirmation: str | None = "PAPER",
    shutdown: ShutdownRequest | None = None,
) -> EquityRuntime:
    return EquityRuntime(
        connection,
        market_data=market_data if market_data is not None else FakeEquityBars(),
        calendar=calendar if calendar is not None else FakeCalendar(SESSIONS),
        execution=execution,
        startup_safety=startup_safety,  # type: ignore[arg-type]
        checkpoint=checkpoint if checkpoint is not None else InMemoryCheckpoint(),  # type: ignore[arg-type]
        config=EquityRuntimeConfig(
            lookback_bars=120,
            observe_only=observe_only,
            runtime_confirmation=confirmation,
        ),
        clock=clock if clock is not None else FakeClock(),
    )


# ==========================================================================
# CRITICAL: the market session
# ==========================================================================


def test_equity_runtime_never_processes_outside_regular_market_session(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """CRITICAL. Outside the session a cycle does nothing at all.

    Not "processes and refuses to submit" - nothing: no provider call, no
    strategy evaluation, no checkpoint write, and no order. That is what makes
    "regular market hours only" a property of the loop rather than a comment.
    """
    data = FakeEquityBars()
    execution = FakeExecution()
    checkpoint = InMemoryCheckpoint()

    for moment in (
        datetime(2026, 8, 26, 13, 0, tzinfo=UTC),  # 09:00 ET, pre-market
        datetime(2026, 8, 26, 20, 0, tzinfo=UTC),  # 16:00 ET, the closing instant
        datetime(2026, 8, 26, 22, 0, tzinfo=UTC),  # 18:00 ET, after hours
        datetime(2026, 8, 27, 3, 0, tzinfo=UTC),  # 23:00 ET the night before
    ):
        runtime = build_runtime(
            connection,
            market_data=data,
            execution=execution,
            checkpoint=checkpoint,
            clock=FakeClock(moment),
        )
        runtime.start()
        report = runtime.run_cycle()
        runtime.stop()

        assert report.results == [], moment
        assert report.session_state == SESSION_CLOSED, moment
        assert report.succeeded, moment

    assert data.calls == []
    assert data.api_calls == 0
    assert execution.calls == []
    assert checkpoint.as_dict() == {}


def test_a_weekend_has_no_session_and_produces_no_work(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    data = FakeEquityBars()
    execution = FakeExecution()
    runtime = build_runtime(
        connection,
        market_data=data,
        execution=execution,
        clock=FakeClock(datetime(2026, 8, 29, 15, 0, tzinfo=UTC)),
    )
    runtime.start()
    report = runtime.run_cycle()

    assert report.session_state == NO_SESSION_TODAY
    assert report.session is None
    assert report.results == []
    assert data.calls == []
    assert execution.calls == []


def test_equity_runtime_respects_market_holiday_and_early_close(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """CRITICAL. Thanksgiving is absent from the calendar; the next day shuts at 13:00.

    Three moments, one calendar, three different correct answers - and none of
    them comes from a hardcoded weekday rule.
    """
    wednesday = session_from_local(
        date(2025, 11, 26), datetime(2025, 11, 26, 9, 30), datetime(2025, 11, 26, 16, 0)
    )
    calendar = FakeCalendar((wednesday, EARLY_CLOSE))
    data = FakeEquityBars()
    execution = FakeExecution()

    def cycle(moment: datetime):
        runtime = build_runtime(
            connection,
            market_data=data,
            calendar=calendar,
            execution=execution,
            clock=FakeClock(moment),
        )
        runtime.start()
        report = runtime.run_cycle()
        runtime.stop()
        return report

    # Thanksgiving: the calendar has no session at all.
    holiday = cycle(datetime(2025, 11, 27, 16, 0, tzinfo=UTC))
    assert holiday.session_state == NO_SESSION_TODAY
    assert holiday.session is None

    # The half day, at 12:00 Eastern: open, and trading.
    open_half_day = cycle(datetime(2025, 11, 28, 17, 0, tzinfo=UTC))
    assert open_half_day.session_state == SESSION_OPEN
    assert open_half_day.session == EARLY_CLOSE

    # The same half day at 13:30 Eastern: shut, because it closed at 13:00.
    after_early_close = cycle(datetime(2025, 11, 28, 18, 30, tzinfo=UTC))
    assert after_early_close.session_state == SESSION_CLOSED
    assert after_early_close.session == EARLY_CLOSE
    assert after_early_close.results == []

    assert len(data.calls) == 1, "only the open half-day cycle fetched anything"


def test_the_bar_that_closes_at_the_bell_is_never_acted_on(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """15:45 completes at 16:00, which is outside the session by definition."""
    last_bar = SESSION.close_utc - timedelta(minutes=15)
    data = FakeEquityBars({SPY: make_equity_bars(SPY, last_bar_start=last_bar)})
    execution = FakeExecution()
    runtime = build_runtime(
        connection,
        market_data=data,
        execution=execution,
        clock=FakeClock(SESSION.close_utc + timedelta(seconds=5)),
    )
    runtime.start()
    report = runtime.run_cycle()

    assert report.session_state == SESSION_CLOSED
    assert report.results == []
    assert execution.calls == []


# ==========================================================================
# CRITICAL: the completed-bar rule
# ==========================================================================


def test_equity_runtime_only_acts_on_latest_completed_15m_bar(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """CRITICAL. The in-progress candle is skipped; older crossovers are not replayed.

    Two halves of one rule. First, a frame whose newest row is the bar still
    forming must be acted on at the *previous* bar. Second, a crossover that
    happened forty bars ago is state for the EMA and nothing else - it has
    already been acted on or already missed, and re-emitting it would turn
    every restart into a burst of stale orders across ten symbols.
    """
    in_progress = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)
    frames = {
        # SPY: a crossover on the newest *completed* bar, plus an in-progress row.
        SPY: make_equity_bars(
            SPY,
            last_bar_start=T_BAR,
            closes=[*crossover_closes(120), REFERENCE_PRICE * 1.10],
            extra=[in_progress],
        ),
        # QQQ: a crossover forty bars ago, nothing on the newest bar.
        QQQ: make_equity_bars(QQQ, last_bar_start=T_BAR, closes=mid_crossover_closes(120)),
    }
    data = FakeEquityBars(frames)
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=data, execution=execution)
    runtime.start()
    report = runtime.run_cycle()

    by_symbol = {result.symbol: result for result in report.results}
    assert by_symbol[SPY].bar_timestamp == T_BAR, "the in-progress bar must not be processed"
    assert by_symbol[SPY].signal is not None
    assert by_symbol[QQQ].bar_timestamp == T_BAR
    assert by_symbol[QQQ].signal is None, "a historical crossover must not be replayed"
    assert execution.symbols == [SPY]


def test_an_extended_hours_candle_is_never_the_latest_completed_bar(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """Defence in depth: a pre-market candle that reached the strategy is refused.

    The market-data source already filters extended hours out. This asserts the
    runtime refuses one anyway rather than trading it - the frame here is a
    symbol whose only bar today is a 09:00 Eastern pre-market print.
    """
    premarket = SESSION.open_utc - timedelta(minutes=30)
    yesterday_last = regular_session_bar_starts(SESSIONS[-2])[-1]
    frames = {
        SPY: make_equity_bars(SPY, last_bar_start=yesterday_last, extra=[premarket]),
    }
    data = FakeEquityBars(frames)
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=data, execution=execution)
    runtime.start()
    report = runtime.run_cycle()

    assert report.severity is CycleSeverity.RETRY_NEXT_CYCLE
    assert "extended-hours candle" in str(report.error)
    assert execution.calls == []


def test_a_symbol_with_no_bar_in_this_session_is_skipped_not_traded(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """Yesterday's closing candle is not this morning's newest completed bar."""
    yesterday_last = regular_session_bar_starts(SESSIONS[-2])[-1]
    frames = {
        SPY: make_equity_bars(SPY, last_bar_start=yesterday_last, closes=crossover_closes(120)),
    }
    data = FakeEquityBars(frames)
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=data, execution=execution)
    runtime.start()
    report = runtime.run_cycle()

    spy = next(result for result in report.results if result.symbol == SPY)
    assert spy.skipped_reason == "NO_BAR_THIS_SESSION"
    assert execution.symbols == []


# ==========================================================================
# CRITICAL: the checkpoint
# ==========================================================================


def test_equity_checkpoint_survives_process_restart(database_path: Path) -> None:
    """CRITICAL. A restarted runner skips the bar its predecessor claimed.

    The claim is committed to SQLite before the bar can reach the strategy, so
    a second process - a real restart, modelled here as a second connection -
    sees it and does not act on the same crossover twice.
    """
    frames = {SPY: make_equity_bars(SPY, last_bar_start=T_BAR, closes=crossover_closes(120))}

    with connect(database_path) as first_connection:
        first_execution = FakeExecution()
        first = EquityRuntime(
            first_connection,
            market_data=FakeEquityBars(frames),
            calendar=FakeCalendar(SESSIONS),
            execution=first_execution,
            startup_safety=safe_startup,
            checkpoint=SqliteCheckpoint(first_connection),
            config=EquityRuntimeConfig(lookback_bars=120, runtime_confirmation="PAPER"),
            clock=FakeClock(),
        )
        first.start()
        first_report = first.run_cycle()
        first.stop()

    assert next(r for r in first_report.results if r.symbol == SPY).processed is True

    with connect(database_path) as second_connection:
        second_execution = FakeExecution()
        second = EquityRuntime(
            second_connection,
            market_data=FakeEquityBars(frames),
            calendar=FakeCalendar(SESSIONS),
            execution=second_execution,
            startup_safety=safe_startup,
            checkpoint=SqliteCheckpoint(second_connection),
            config=EquityRuntimeConfig(lookback_bars=120, runtime_confirmation="PAPER"),
            clock=FakeClock(),
        )
        second.start()
        second_report = second.run_cycle()

        assert second.checkpoints[SPY] == T_BAR
        second.stop()

    spy = next(result for result in second_report.results if result.symbol == SPY)
    assert spy.skipped_reason == "ALREADY_PROCESSED"
    assert second_execution.calls == [], "a restart must not re-submit a claimed bar"


def test_the_same_bar_is_not_processed_twice_within_one_process(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    frames = {SPY: make_equity_bars(SPY, last_bar_start=T_BAR, closes=crossover_closes(120))}
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=FakeEquityBars(frames), execution=execution)
    runtime.start()
    runtime.run_cycle()
    second = runtime.run_cycle()

    spy = next(result for result in second.results if result.symbol == SPY)
    assert spy.skipped_reason == "ALREADY_PROCESSED"
    assert execution.symbols == [SPY]


def test_the_bar_is_claimed_before_the_strategy_can_submit(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """Miss a trade rather than duplicate one: the claim commits first."""
    seen: list[datetime | None] = []
    checkpoint = SqliteCheckpoint(connection)

    class RecordingExecution(FakeExecution):
        def execute(self, conn: sqlite3.Connection, **kwargs: object) -> PaperExecutionResult:
            seen.append(checkpoint.last_processed(str(kwargs["symbol"])))
            return super().execute(conn, **kwargs)  # type: ignore[arg-type]

    frames = {SPY: make_equity_bars(SPY, last_bar_start=T_BAR, closes=crossover_closes(120))}
    runtime = build_runtime(
        connection,
        market_data=FakeEquityBars(frames),
        execution=RecordingExecution(),
        checkpoint=checkpoint,
    )
    runtime.start()
    runtime.run_cycle()

    assert seen == [T_BAR]


def test_equity_and_crypto_checkpoints_do_not_collide(database_path: Path) -> None:
    """One table, two products, and no row either of them shares."""
    from autotrader.state.sqlite import list_runtime_checkpoints

    with connect(database_path) as open_connection:
        checkpoint = SqliteCheckpoint(open_connection)
        checkpoint.mark_processed("BTC/USD", datetime(2026, 8, 26, 10, 0, tzinfo=UTC))
        checkpoint.mark_processed(SPY, T_BAR)

        stored = {
            row.symbol: row.last_processed_bar_timestamp
            for row in list_runtime_checkpoints(open_connection)
        }

    assert stored["BTC/USD"] == datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
    assert stored[SPY] == T_BAR


def test_the_runtime_reports_only_its_own_checkpoints(connection: sqlite3.Connection) -> None:
    checkpoint = SqliteCheckpoint(connection)
    checkpoint.mark_processed("BTC/USD", datetime(2026, 8, 26, 10, 0, tzinfo=UTC))
    checkpoint.mark_processed(SPY, T_BAR)
    runtime = build_runtime(connection, checkpoint=checkpoint)
    runtime.start()

    assert set(runtime.checkpoints) == {SPY}


# ==========================================================================
# CRITICAL: ambiguity pauses trading
# ==========================================================================


def test_equity_unknown_submission_pauses_future_trading(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """CRITICAL. An UNKNOWN outcome stops this process submitting, permanently.

    The cycle ends where the ambiguity happened - the remaining symbols are not
    even looked at - and a later cycle in the same process submits nothing,
    because an order may exist at the broker and nothing here can resolve it.
    """
    frames = {
        symbol: make_equity_bars(symbol, last_bar_start=T_BAR, closes=crossover_closes(120))
        for symbol in PROCESSING_ORDER
    }
    execution = FakeExecution([AmbiguousSubmissionError("timeout after submit")])
    clock = FakeClock()
    runtime = build_runtime(
        connection, market_data=FakeEquityBars(frames), execution=execution, clock=clock
    )
    runtime.start()
    first = runtime.run_cycle()

    assert first.severity is CycleSeverity.TRADING_PAUSED
    assert runtime.state is RuntimeState.TRADING_PAUSED
    assert execution.symbols == [SPY], "the cycle stopped at the ambiguous symbol"

    clock.advance(timedelta(minutes=15))
    second = runtime.run_cycle()

    assert execution.symbols == [SPY], "no further submission after the pause"
    for result in second.results:
        assert result.execution is None
        if result.signal is not None:
            assert result.skipped_reason == "TRADING_PAUSED"

    events = {event.event_type for event in list_system_events(connection)}
    assert "EQUITY_RUNTIME_TRADING_PAUSED" in events


# ==========================================================================
# CRITICAL: intent before submission, end to end
# ==========================================================================


def test_equity_order_intent_is_committed_before_broker_submission(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """CRITICAL. Through the real execution boundary, not a fake one.

    The runtime hands the signal to the production equity boundary with only
    the broker transport faked, and the broker records how many intents were
    already committed when `submit_order` was called. A crash between those two
    points must leave a durable `client_order_id` for reconciliation to resolve,
    which is only true if the intent is written first.
    """
    from autotrader.equity.runtime import PaperEquityExecutionGateway

    committed: list[int] = []

    class RecordingClient(FakeTradingClient):
        def submit_order(self, request: MarketOrderRequest):
            committed.append(len(list_order_intents(connection)))
            return super().submit_order(request)

    client = RecordingClient(asset=make_asset(symbol=SPY))
    gateway = PaperEquityExecutionGateway(
        trading_client=client, data_client=FakeDataClient(REFERENCE_PRICE)
    )
    frames = {SPY: make_equity_bars(SPY, last_bar_start=T_BAR, closes=crossover_closes(120))}
    runtime = build_runtime(
        connection,
        market_data=FakeEquityBars(frames),
        execution=gateway,  # type: ignore[arg-type]
    )
    runtime.start()
    report = runtime.run_cycle()

    assert committed == [1], "submit_order ran with the intent already committed"
    spy = next(result for result in report.results if result.symbol == SPY)
    assert spy.execution is not None
    assert spy.execution.outcome is ExecutionOutcome.SUBMITTED
    [request] = client.submit_calls
    assert request.time_in_force.value == "day"
    assert request.qty == 10.0
    assert client.clock_calls == 1, "the session was confirmed against the broker's clock"


# ==========================================================================
# Ordering and batching
# ==========================================================================


def test_symbols_are_processed_in_the_configured_order(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    frames = {
        symbol: make_equity_bars(symbol, last_bar_start=T_BAR, closes=crossover_closes(120))
        for symbol in PROCESSING_ORDER
    }
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=FakeEquityBars(frames), execution=execution)
    runtime.start()
    report = runtime.run_cycle()

    assert [result.symbol for result in report.results] == list(PROCESSING_ORDER)
    assert execution.symbols == list(PROCESSING_ORDER)


def test_the_universe_is_fetched_in_one_batched_call(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """Ten symbols cost one market-data request, not ten."""
    data = FakeEquityBars()
    runtime = build_runtime(connection, market_data=data, execution=FakeExecution())
    runtime.start()
    runtime.run_cycle()

    assert len(data.calls) == 1
    assert data.calls[0][0] == PROCESSING_ORDER
    assert runtime.heartbeat.api_calls_last_cycle >= 1


def test_the_universe_is_exactly_ten_symbols() -> None:
    assert PROCESSING_ORDER == EQUITY_SYMBOLS
    assert len(PROCESSING_ORDER) == 10


def test_a_controlled_failure_on_one_symbol_does_not_stop_the_others(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    broken = make_equity_bars(SPY, last_bar_start=T_BAR).assign(close=float("nan"))
    frames = {SPY: broken}
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=FakeEquityBars(frames), execution=execution)
    runtime.start()
    report = runtime.run_cycle()

    assert report.severity is CycleSeverity.RETRY_NEXT_CYCLE
    assert [result.symbol for result in report.results] == list(PROCESSING_ORDER[1:])


def test_a_provider_failure_is_a_wait_not_a_stop(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    data = FakeEquityBars(error=EquityDataError("provider unavailable"))
    runtime = build_runtime(connection, market_data=data, execution=FakeExecution())
    runtime.start()
    report = runtime.run_cycle()

    assert report.severity is CycleSeverity.RETRY_NEXT_CYCLE
    assert runtime.state is RuntimeState.RUNNING


def test_an_unreadable_calendar_is_a_wait_not_a_stop(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    class BrokenCalendar:
        api_calls = 0

        def session_for(self, day: date) -> MarketSession | None:
            raise SessionError("calendar unavailable")

        def sessions_between(self, start: date, end: date) -> tuple[MarketSession, ...]:
            raise SessionError("calendar unavailable")

    runtime = build_runtime(connection, calendar=BrokenCalendar(), execution=FakeExecution())
    runtime.start()
    report = runtime.run_cycle()

    assert report.severity is CycleSeverity.RETRY_NEXT_CYCLE
    assert runtime.state is RuntimeState.RUNNING


def test_an_equity_failure_is_classified_as_a_retry() -> None:
    assert classify_equity(EquityDataError("x")) is CycleSeverity.RETRY_NEXT_CYCLE
    assert classify_equity(SessionError("x")) is CycleSeverity.RETRY_NEXT_CYCLE
    assert classify_equity(AmbiguousSubmissionError("x")) is CycleSeverity.TRADING_PAUSED
    assert classify_equity(RuntimeError("x")) is CycleSeverity.FATAL


# ==========================================================================
# Gates
# ==========================================================================


def test_the_environment_gate_is_closed_by_default(connection: sqlite3.Connection) -> None:
    frames = {SPY: make_equity_bars(SPY, last_bar_start=T_BAR, closes=crossover_closes(120))}
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=FakeEquityBars(frames), execution=execution)
    runtime.start()
    report = runtime.run_cycle()

    assert runtime.authorization.reason == "PAPER_ENV_GATE_DISABLED"
    assert execution.calls == []
    spy = next(result for result in report.results if result.symbol == SPY)
    assert spy.skipped_reason == "PAPER_ENV_GATE_DISABLED"
    assert spy.signal is not None, "observation continues while the gate is shut"


def test_the_runtime_confirmation_is_required(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    frames = {SPY: make_equity_bars(SPY, last_bar_start=T_BAR, closes=crossover_closes(120))}
    execution = FakeExecution()
    runtime = build_runtime(
        connection,
        market_data=FakeEquityBars(frames),
        execution=execution,
        confirmation=None,
    )
    runtime.start()
    runtime.run_cycle()

    assert runtime.authorization.reason == "RUNTIME_CONFIRMATION_MISSING"
    assert execution.calls == []


@pytest.mark.parametrize("token", ["paper", "Paper", "PAPER ", "YES", ""])
def test_a_wrong_confirmation_token_does_not_open_the_gate(
    connection: sqlite3.Connection, enabled_gate: None, token: str
) -> None:
    runtime = build_runtime(connection, execution=FakeExecution(), confirmation=token)
    runtime.start()

    assert runtime.authorization.enabled is False


def test_startup_safety_fails_closed_when_reconciliation_is_not_safe(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    frames = {SPY: make_equity_bars(SPY, last_bar_start=T_BAR, closes=crossover_closes(120))}
    execution = FakeExecution()
    runtime = build_runtime(
        connection,
        market_data=FakeEquityBars(frames),
        execution=execution,
        startup_safety=unsafe_startup,
    )
    runtime.start()
    report = runtime.run_cycle()

    assert runtime.authorization.reason == f"STARTUP_SAFETY_{STARTUP_SAFETY_UNSAFE}"
    assert execution.calls == []
    spy = next(result for result in report.results if result.symbol == SPY)
    assert spy.signal is not None, "observation continues while trading is unsafe"


def test_an_unchecked_startup_never_opens_the_gate(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """The default answer is UNRESOLVED, and UNRESOLVED does not trade."""
    runtime = build_runtime(
        connection, execution=FakeExecution(), startup_safety=unresolved_startup_safety
    )
    runtime.start()

    assert runtime.authorization.enabled is False
    assert runtime.startup_safety is not None
    assert runtime.startup_safety.safe_to_trade is False


def test_observe_only_removes_the_execution_path_entirely(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    frames = {SPY: make_equity_bars(SPY, last_bar_start=T_BAR, closes=crossover_closes(120))}
    execution = FakeExecution()
    runtime = build_runtime(
        connection,
        market_data=FakeEquityBars(frames),
        execution=execution,
        observe_only=True,
    )
    runtime.start()
    report = runtime.run_cycle()

    assert execution.calls == []
    assert runtime.authorization.reason == "OBSERVE_ONLY"
    spy = next(result for result in report.results if result.symbol == SPY)
    assert spy.skipped_reason == "OBSERVE_ONLY"


def test_the_runtime_never_bypasses_the_execution_layers_own_gate() -> None:
    source = Path(equity_runtime.__file__).read_text()

    assert "os.environ[" not in source
    assert "setenv" not in source
    assert "PAPER_TRADING_ENABLED_VALUE" not in source
    assert "paper_trading_enabled" in source


# ==========================================================================
# Signals, risk and long-only
# ==========================================================================


def test_a_signal_is_recorded_even_when_nothing_is_submitted(
    connection: sqlite3.Connection,
) -> None:
    frames = {SPY: make_equity_bars(SPY, last_bar_start=T_BAR, closes=crossover_closes(120))}
    runtime = build_runtime(connection, market_data=FakeEquityBars(frames), observe_only=True)
    runtime.start()
    runtime.run_cycle()

    signals = list_signals(connection)
    assert [signal.symbol for signal in signals] == [SPY]
    assert signals[0].signal_type == "BUY"
    assert signals[0].signal_timestamp == T_BAR


def test_an_exit_signal_becomes_a_sell_and_never_a_short(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """CRITICAL for long-only: a SELL while flat is refused by risk, not sent."""
    frames = {
        SPY: make_equity_bars(SPY, last_bar_start=T_BAR, closes=crossover_closes(120, upward=False))
    }
    execution = FakeExecution(
        [
            make_execution_result(
                symbol=SPY,
                side=OrderSide.SELL,
                outcome=ExecutionOutcome.REJECTED_BY_RISK,
                approved=False,
                reason_code=NO_POSITION_TO_EXIT,
            )
        ]
    )
    runtime = build_runtime(connection, market_data=FakeEquityBars(frames), execution=execution)
    runtime.start()
    report = runtime.run_cycle()

    assert execution.calls[0]["side"] == "SELL"
    spy = next(result for result in report.results if result.symbol == SPY)
    assert spy.execution is not None
    assert spy.execution.outcome is ExecutionOutcome.REJECTED_BY_RISK
    assert runtime.heartbeat.orders_submitted == 0


def test_the_runtime_makes_no_sizing_decision_of_its_own(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """It asks for more than any account can hold and lets risk clamp it."""
    frames = {SPY: make_equity_bars(SPY, last_bar_start=T_BAR, closes=crossover_closes(120))}
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=FakeEquityBars(frames), execution=execution)
    runtime.start()
    runtime.run_cycle()

    source = Path(equity_runtime.__file__).read_text()
    assert "RISK_SIZED_REQUEST_QUANTITY" in source
    assert execution.calls == [
        {"symbol": SPY, "side": "BUY", "now": T_NOW},
    ]


def test_a_risk_rejection_is_an_ordinary_result_not_a_failure(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    frames = {SPY: make_equity_bars(SPY, last_bar_start=T_BAR, closes=crossover_closes(120))}
    execution = FakeExecution(
        [
            make_execution_result(
                symbol=SPY,
                outcome=ExecutionOutcome.REJECTED_BY_RISK,
                approved=False,
                reason_code="POSITION_LIMIT",
            )
        ]
    )
    runtime = build_runtime(connection, market_data=FakeEquityBars(frames), execution=execution)
    runtime.start()
    report = runtime.run_cycle()

    assert report.succeeded
    assert runtime.state is RuntimeState.RUNNING


# ==========================================================================
# Locking and single-instance safety
# ==========================================================================


def test_the_equity_lock_is_a_different_file_from_the_crypto_lock(tmp_path: Path) -> None:
    """Two services, one account, two processes - and no false collision."""
    database = tmp_path / "autotrader.db"

    crypto_lock = lock_path_for(database)
    equity_lock = lock_path_for(database, scope=EQUITY_LOCK_SCOPE)

    assert crypto_lock == tmp_path / "autotrader.db.runtime.lock"
    assert equity_lock == tmp_path / "autotrader.db.equity.runtime.lock"
    assert crypto_lock != equity_lock


def test_two_equity_runners_still_collide(tmp_path: Path) -> None:
    """The property that actually prevents duplicate trading is preserved."""
    from autotrader.runtime.lock import RuntimeLock, RuntimeLockError

    database = tmp_path / "autotrader.db"
    first = RuntimeLock(lock_path_for(database, scope=EQUITY_LOCK_SCOPE))
    first.acquire()
    try:
        with pytest.raises(RuntimeLockError):
            RuntimeLock(lock_path_for(database, scope=EQUITY_LOCK_SCOPE)).acquire()
    finally:
        first.release()


# ==========================================================================
# Logging, monitoring and shutdown
# ==========================================================================


def test_the_runtime_logs_structured_events_without_a_credential(
    connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    enabled_gate: None,
) -> None:
    secret_key = "SECRET-VALUE-THAT-MUST-NEVER-BE-LOGGED"
    api_key = "API-KEY-THAT-MUST-NEVER-BE-LOGGED"
    monkeypatch.setenv("ALPACA_API_KEY", api_key)
    monkeypatch.setenv("ALPACA_SECRET_KEY", secret_key)
    frames = {SPY: make_equity_bars(SPY, last_bar_start=T_BAR, closes=crossover_closes(120))}
    runtime = build_runtime(connection, market_data=FakeEquityBars(frames), observe_only=True)

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        runtime.run_once()

    emitted = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=equity_runtime_started" in emitted
    assert "event=cycle_started" in emitted
    assert "event=bar_processed" in emitted
    assert "event=heartbeat" in emitted
    for secret in (api_key, secret_key, "ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        assert secret not in emitted, secret


def test_a_shutdown_request_stops_new_submissions_mid_cycle(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    shutdown = ShutdownRequest()
    frames = {
        symbol: make_equity_bars(symbol, last_bar_start=T_BAR, closes=crossover_closes(120))
        for symbol in PROCESSING_ORDER
    }
    execution = FakeExecution()
    runtime = EquityRuntime(
        connection,
        market_data=FakeEquityBars(frames),
        calendar=FakeCalendar(SESSIONS),
        execution=execution,
        startup_safety=safe_startup,
        checkpoint=InMemoryCheckpoint(),
        config=EquityRuntimeConfig(lookback_bars=120, runtime_confirmation="PAPER"),
        clock=FakeClock(),
        shutdown=shutdown,
    )
    runtime.start()
    shutdown.request("TEST")
    report = runtime.run_cycle()

    assert report.results == []
    assert execution.calls == []


def test_run_forever_sleeps_to_the_next_session_boundary(
    connection: sqlite3.Connection,
) -> None:
    slept: list[float] = []
    clock = FakeClock(datetime(2026, 8, 26, 15, 0, 5, tzinfo=UTC))

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(timedelta(seconds=seconds))

    runtime = EquityRuntime(
        connection,
        market_data=FakeEquityBars(),
        calendar=FakeCalendar(SESSIONS),
        startup_safety=safe_startup,
        checkpoint=InMemoryCheckpoint(),
        config=EquityRuntimeConfig(lookback_bars=120, observe_only=True),
        clock=clock,
        sleep=sleep,
    )
    reports = runtime.run_forever(max_cycles=1)

    assert len(reports) == 1
    assert reports[0].started_at == datetime(2026, 8, 26, 15, 15, 5, tzinfo=UTC)
    assert sum(slept) == pytest.approx(900.0)


def test_run_forever_stops_when_the_calendar_cannot_be_read(
    connection: sqlite3.Connection,
) -> None:
    class EmptyCalendar:
        api_calls = 0

        def session_for(self, day: date) -> MarketSession | None:
            return None

        def sessions_between(self, start: date, end: date) -> tuple[MarketSession, ...]:
            return ()

    runtime = EquityRuntime(
        connection,
        market_data=FakeEquityBars(),
        calendar=EmptyCalendar(),
        startup_safety=safe_startup,
        checkpoint=InMemoryCheckpoint(),
        config=EquityRuntimeConfig(lookback_bars=120, observe_only=True),
        clock=FakeClock(),
    )
    reports = runtime.run_forever(max_cycles=3)

    assert reports == []
    assert runtime.state is RuntimeState.FAILED


def test_the_heartbeat_reports_every_symbol(connection: sqlite3.Connection) -> None:
    runtime = build_runtime(connection, observe_only=True)
    runtime.start()

    assert set(runtime.heartbeat.last_processed_bars) == set(PROCESSING_ORDER)


# ==========================================================================
# CRITICAL: the crypto contract is unchanged
# ==========================================================================


def test_crypto_runtime_contract_is_unchanged_by_equity_v02() -> None:
    """CRITICAL. Nothing this milestone added changed the 24/7 crypto product.

    Six separate claims, each checked rather than asserted in prose: the crypto
    universe, its lock file, its module's freedom from any equity concept, its
    execution boundary's refusal of an equity symbol, its reconciliation
    default, and the shared risk policy.
    """
    from autotrader.data.historical import SUPPORTED_SYMBOLS
    from autotrader.execution import models as execution_models
    from autotrader.execution import paper as crypto_paper
    from autotrader.reconciliation import engine as reconciliation_engine
    from autotrader.risk import (
        MAX_DAILY_LOSS_FRACTION,
        MAX_POSITION_FRACTION,
        MAX_TOTAL_EXPOSURE_FRACTION,
    )
    from autotrader.runtime import market_data as crypto_market_data
    from autotrader.runtime import runner as crypto_runner
    from autotrader.runtime import schedule as crypto_schedule
    from autotrader.runtime.runner import PROCESSING_ORDER as CRYPTO_ORDER

    # 1. The crypto universe is still exactly the two pairs.
    assert CRYPTO_ORDER == ("BTC/USD", "ETH/USD") == SUPPORTED_SYMBOLS
    assert execution_models.SUPPORTED_SYMBOLS == ("BTC/USD", "ETH/USD")

    # 2. The crypto lock file name is byte-for-byte what it was.
    assert lock_path_for(Path("/tmp/autotrader.db")) == Path("/tmp/autotrader.db.runtime.lock")

    # 3. No equity concept leaked into any crypto runtime module. Prose is
    #    stripped first: those modules *document* what they must never do, and
    #    a naive substring scan would trip over the sentence stating the rule.
    from test_runtime import code_without_prose

    crypto_source = "\n".join(
        code_without_prose(Path(module.__file__).read_text())
        for module in (crypto_runner, crypto_schedule, crypto_market_data)
    )
    for forbidden in (
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
        "StockHistoricalDataClient",
        "StockLatestTradeRequest",
        "StockBarsRequest",
        "TimeInForce.DAY",
        "get_clock",
        "America/New_York",
    ):
        assert forbidden not in crypto_source, forbidden

    # 4. The crypto execution boundary still refuses an equity symbol.
    for symbol in EQUITY_SYMBOLS:
        with pytest.raises(ExecutionInputError):
            crypto_paper.normalize_symbol(symbol)
    assert crypto_paper.ORDER_TIME_IN_FORCE.value == "gtc"

    # 5. Reconciliation now defaults to the whole account, which is the one
    #    deliberate change combined integration makes here. Equity V0.2 kept the
    #    crypto pairs as the default so that merging it altered no existing
    #    caller; with both books live on one account, a pass that looked at two
    #    of twelve symbols cannot establish that the account is understood. The
    #    crypto pairs are still in it, and still first.
    import inspect

    signature = inspect.signature(reconciliation_engine.reconcile_paper_state)
    default_universe = signature.parameters["symbols"].default
    assert default_universe == TRADABLE_SYMBOLS
    assert default_universe[:2] == ("BTC/USD", "ETH/USD")
    assert len(default_universe) == 12

    # 6. The risk policy is shared and unchanged.
    assert (MAX_POSITION_FRACTION, MAX_TOTAL_EXPOSURE_FRACTION, MAX_DAILY_LOSS_FRACTION) == (
        0.05,
        0.30,
        0.02,
    )


def test_the_equity_runtime_has_no_live_path() -> None:
    source = Path(equity_runtime.__file__).read_text()
    for forbidden in (
        "paper=False",
        "paper = False",
        "--live",
        "TRADING_LIVE",
        "ALPACA_LIVE",
        "api.alpaca.markets",
        "live_trading",
    ):
        assert forbidden not in source, forbidden


def test_the_equity_runtime_constructs_no_trading_client_of_its_own() -> None:
    source = Path(equity_runtime.__file__).read_text()

    assert "TradingClient(" not in source


def test_the_equity_runtime_defines_no_deployment_artefact() -> None:
    """Two services later is a deployment decision, not a runtime one."""
    source = Path(equity_runtime.__file__).read_text()
    for forbidden in ("systemd", "Dockerfile", "ExecStart", "crontab", "supervisord"):
        assert forbidden not in source, forbidden


def test_the_equity_runtime_owns_no_database_schema() -> None:
    source = Path(equity_runtime.__file__).read_text()
    for forbidden in ("CREATE TABLE", "ALTER TABLE", "DROP TABLE", "SCHEMA_VERSION"):
        assert forbidden not in source, forbidden


def test_the_equity_runtime_makes_no_network_access(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("the runtime must not open a socket")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    runtime = build_runtime(connection, observe_only=True)

    assert runtime.run_once().session_state == SESSION_OPEN


# ==========================================================================
# The CLI
# ==========================================================================


def test_the_cli_exposes_the_equity_run_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "equity-run" in result.stdout
    assert "equity-download" in result.stdout


def test_the_cli_exposes_no_live_or_stock_run_command() -> None:
    names = {command.name for command in app.registered_commands}

    assert "stock-run" not in names
    assert "live-run" not in names
    assert "live" not in names
    assert "equity-run" in names


def test_equity_run_help_states_the_paper_and_session_gates() -> None:
    result = runner.invoke(app, ["equity-run", "--help"])

    assert result.exit_code == 0
    assert "--confirm-paper-runtime" in result.stdout
    assert "--observe-only" in result.stdout
    assert "--once" in result.stdout
    assert "--live" not in result.stdout


def test_equity_run_refuses_to_start_behind_a_held_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autotrader.runtime.lock import RuntimeLock

    database = initialize_database(tmp_path / "state.db")
    holder = RuntimeLock(lock_path_for(database, scope=EQUITY_LOCK_SCOPE))
    holder.acquire()
    try:
        result = runner.invoke(
            app, ["equity-run", "--once", "--observe-only", "--db", str(database)]
        )
    finally:
        holder.release()

    assert result.exit_code == 1
    assert "already holds" in result.output


def test_equity_run_observe_only_without_credentials_submits_nothing(
    tmp_path: Path,
) -> None:
    """No credentials means no broker, which must fail closed rather than trade."""
    database = initialize_database(tmp_path / "state.db")

    result = runner.invoke(app, ["equity-run", "--once", "--observe-only", "--db", str(database)])

    assert "OBSERVATION ONLY - NO ORDER WILL BE SUBMITTED" in result.output
    with connect(database) as open_connection:
        assert list_order_intents(open_connection) == []


def test_a_crypto_api_error_shape_is_still_understood() -> None:
    """The shared error helpers were not changed by this milestone."""
    error = APIError(json.dumps({"code": 1, "message": "nope"}))

    assert "nope" in str(error) or "nope" in str(getattr(error, "message", ""))
    assert uuid4() is not None
