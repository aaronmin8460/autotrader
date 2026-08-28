"""C8 tests: the 24/7 crypto runtime, its schedule, and its refusals.

**Every test here is offline and instantaneous.** The clock, the sleep, the
market-data boundary, the execution boundary, the startup-safety check and the
processed-bar checkpoint are all injected, so nothing waits fifteen real
minutes and nothing opens a socket.

The tests that matter most are again the ones about not acting. A runtime that
trades an unfinished candle, trades the same bar twice, keeps submitting after
an ambiguous outcome, or starts a second copy of itself against one database
is not a runtime with a bug - it is a duplicate-order generator. Those four
have their own section at the top.
"""

from __future__ import annotations

import ast
import logging
import os
import signal
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from autotrader.cli import app
from autotrader.data.historical import CANONICAL_COLUMNS, SUPPORTED_SYMBOLS, HistoricalDataError
from autotrader.execution.models import ExecutionError, OrderIntent, OrderSide
from autotrader.execution.paper import (
    PAPER_TRADING_ENABLED_ENV,
    PAPER_TRADING_ENABLED_VALUE,
    AccountNotTradableError,
    AmbiguousSubmissionError,
    CryptoAssetSpec,
    ExecutionOutcome,
    MissingCredentialsError,
    PaperAccountState,
    PaperExecutionResult,
)
from autotrader.risk import APPROVED, NO_POSITION_TO_EXIT, POSITION_LIMIT, RiskDecision
from autotrader.runtime import execution as runtime_execution
from autotrader.runtime import lock as runtime_lock
from autotrader.runtime import market_data as runtime_market_data
from autotrader.runtime import monitoring as runtime_monitoring
from autotrader.runtime import runner as runtime_runner
from autotrader.runtime import safety as runtime_safety
from autotrader.runtime import schedule as runtime_schedule
from autotrader.runtime.checkpoint import InMemoryCheckpoint
from autotrader.runtime.execution import BrokerAuthenticationError
from autotrader.runtime.lock import RuntimeLock, RuntimeLockError, lock_path_for
from autotrader.runtime.market_data import completed_window
from autotrader.runtime.monitoring import LOGGER_NAME, RuntimeState, format_event
from autotrader.runtime.runner import (
    PROCESSING_ORDER,
    RISK_SIZED_REQUEST_QUANTITY,
    RUNTIME_CONFIRMATION_TOKEN,
    CryptoRuntime,
    CycleSeverity,
    RuntimeConfig,
    ShutdownRequest,
    classify,
)
from autotrader.runtime.safety import (
    STARTUP_SAFETY_SAFE,
    STARTUP_SAFETY_UNRESOLVED,
    STARTUP_SAFETY_UNSAFE,
    StartupSafetyResult,
    unresolved_startup_safety,
)
from autotrader.runtime.schedule import (
    BAR_INTERVAL,
    DEFAULT_LOOKBACK_BARS,
    DEFAULT_SAFETY_DELAY,
    MAX_LOOKBACK_BARS,
    MIN_LOOKBACK_BARS,
    ScheduleError,
    is_bar_complete,
    latest_completed_bar_start,
    next_boundary,
    next_wake_time,
    require_lookback_bars,
)
from autotrader.state.sqlite import (
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    connect,
    initialize_database,
    list_signals,
    list_strategy_runs,
    list_system_events,
)

BTC = "BTC/USD"
ETH = "ETH/USD"

#: A Wednesday. The runtime must not care, and other tests prove it does not.
T_BAR = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)

#: Five seconds past the 10:15 boundary: the 10:00 bar has just completed and
#: the provider has had the safety delay to publish it.
T_NOW = datetime(2026, 8, 26, 10, 15, 5, tzinfo=UTC)

REFERENCE_PRICE = 100_000.0

runner = CliRunner()


# ==========================================================================
# Fakes. The runtime's four boundaries, and nothing else.
# ==========================================================================


def make_bars(
    symbol: str = BTC,
    *,
    last_bar_start: datetime = T_BAR,
    count: int = 120,
    closes: list[float] | None = None,
) -> pd.DataFrame:
    """A canonical bar frame ending at `last_bar_start`.

    OHLC are all the close, which satisfies every C2 relationship, so a frame
    is invalid here only when a test deliberately makes it so.
    """
    prices = closes if closes is not None else [float(REFERENCE_PRICE)] * count
    length = len(prices)
    timestamps = [last_bar_start - (length - 1 - index) * BAR_INTERVAL for index in range(length)]
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(pd.Series(timestamps), utc=True),
            "symbol": pd.Series([symbol] * length, dtype="string"),
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": [1.0] * length,
            "trade_count": [10.0] * length,
            "vwap": prices,
        },
        columns=list(CANONICAL_COLUMNS),
    )
    return frame


def crossover_closes(count: int = 120, *, upward: bool = True) -> list[float]:
    """Flat prices, then one decisive move on the final bar.

    While the price is flat both EMAs sit exactly on it, so the crossover
    condition - "at or below on the previous bar, strictly above on this one" -
    is armed and fires on the last bar and only the last bar.
    """
    base = float(REFERENCE_PRICE)
    move = base * (1.10 if upward else 0.90)
    return [base] * (count - 1) + [move]


def mid_crossover_closes(count: int = 120) -> list[float]:
    """A crossover well inside the window, and nothing on the newest bar."""
    base = float(REFERENCE_PRICE)
    return [base] * (count - 40) + [base * 1.10] * 40


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, now: datetime = T_NOW) -> None:
        self.now = now
        self.reads = 0

    def __call__(self) -> datetime:
        self.reads += 1
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class FakeMarketData:
    """Returns prepared frames per symbol, or raises what a test asks for."""

    def __init__(
        self,
        frames: dict[str, pd.DataFrame] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.frames = frames if frames is not None else {}
        self.error = error
        self.calls: list[tuple[str, datetime, int]] = []
        self.api_calls = 0

    def recent_bars(self, symbol: str, *, now: datetime, lookback_bars: int) -> pd.DataFrame:
        self.calls.append((symbol, now, lookback_bars))
        self.api_calls += 1
        if self.error is not None:
            raise self.error
        if symbol not in self.frames:
            return make_bars(symbol)
        return self.frames[symbol]


def make_execution_result(
    *,
    symbol: str = BTC,
    side: OrderSide = OrderSide.BUY,
    outcome: ExecutionOutcome = ExecutionOutcome.SUBMITTED,
    approved: bool = True,
    reason_code: str = APPROVED,
) -> PaperExecutionResult:
    """A `PaperExecutionResult` shaped like C7's, without touching C7."""
    quantity = Decimal("0.01")
    decision = RiskDecision(
        approved=approved,
        approved_quantity=quantity if approved else Decimal(0),
        reason_code=reason_code,
        message=f"test decision {reason_code}",
        max_allowed_quantity=quantity,
    )
    account = PaperAccountState(
        equity=200_000.0,
        cash=200_000.0,
        status="ACTIVE",
        trading_blocked=False,
        account_blocked=False,
        trade_suspended_by_user=False,
    )
    asset = CryptoAssetSpec(
        symbol=symbol,
        asset_class="crypto",
        status="active",
        tradable=True,
        fractionable=True,
        min_order_size=Decimal("0.0001"),
        min_trade_increment=Decimal("0.000000001"),
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
        daily_baseline_equity=Decimal("200000"),
        message="test execution",
        asset=asset,
        intent=intent,
    )


class FakeExecution:
    """Records every submission attempt, in order, and refuses re-entry.

    The re-entry guard is the test for "no parallel broker submission": if the
    runtime ever overlapped two attempts, this would raise rather than quietly
    pass.
    """

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
            self.calls.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "requested_quantity": requested_quantity,
                    "strategy_run_id": strategy_run_id,
                    "now": now,
                }
            )
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
    """A startup-safety check that says trading is safe. Tests only."""
    return StartupSafetyResult(True, STARTUP_SAFETY_SAFE, "test fixture: reconciled")


def unsafe_startup() -> StartupSafetyResult:
    """A startup-safety check that says trading is not safe."""
    return StartupSafetyResult(False, STARTUP_SAFETY_UNSAFE, "test fixture: refused")


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return initialize_database(tmp_path / "state.db")


@pytest.fixture
def connection(database_path: Path):
    with connect(database_path) as open_connection:
        yield open_connection


@pytest.fixture(autouse=True)
def _closed_gate_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with the paper gate shut, whatever the shell had set.

    Autouse so a developer's exported gate can never make a "no order was
    submitted" assertion pass for the wrong reason - or fail one.
    """
    monkeypatch.delenv(PAPER_TRADING_ENABLED_ENV, raising=False)


@pytest.fixture
def enabled_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PAPER_TRADING_ENABLED_ENV, PAPER_TRADING_ENABLED_VALUE)


def build_runtime(
    connection: sqlite3.Connection,
    *,
    market_data: FakeMarketData | None = None,
    execution: FakeExecution | None = None,
    startup_safety=safe_startup,
    clock: FakeClock | None = None,
    checkpoint: InMemoryCheckpoint | None = None,
    shutdown: ShutdownRequest | None = None,
    confirmation: str | None = RUNTIME_CONFIRMATION_TOKEN,
    observe_only: bool = False,
    sleep=None,
) -> CryptoRuntime:
    """A runtime with every boundary faked and every gate open by default.

    The default `sleep` advances the fake clock by exactly what it was asked to
    wait, so a scheduled loop reaches its boundary instantly and no test waits
    on a real one.
    """
    fake_clock = clock if clock is not None else FakeClock()

    def advance(seconds: float) -> None:
        fake_clock.advance(timedelta(seconds=seconds))

    return CryptoRuntime(
        connection,
        market_data=market_data if market_data is not None else FakeMarketData(),
        execution=execution if execution is not None else FakeExecution(),
        startup_safety=startup_safety,
        checkpoint=checkpoint,
        config=RuntimeConfig(
            observe_only=observe_only,
            runtime_confirmation=confirmation,
        ),
        clock=fake_clock,
        sleep=sleep if sleep is not None else advance,
        shutdown=shutdown,
    )


def code_without_prose(source: str) -> str:
    """`source` with every docstring and comment removed.

    The absences asserted below are about *executable code*. This package's own
    prose names most of what it forbids - "no `get_clock`", "no weekday
    filter", "no live mode" - so a naive substring scan would trip over the
    sentences that explain the rules.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


RUNTIME_MODULES = (
    runtime_schedule,
    runtime_safety,
    runtime_market_data,
    runtime_execution,
    runtime_monitoring,
    runtime_runner,
    runtime_lock,
)


def runtime_source() -> str:
    """Every runtime module's executable code, concatenated."""
    return "\n".join(
        code_without_prose(Path(module.__file__).read_text()) for module in RUNTIME_MODULES
    )


# ==========================================================================
# CRITICAL REGRESSION TESTS
# ==========================================================================


def test_incomplete_15m_bar_is_never_processed(connection: sqlite3.Connection) -> None:
    """The single most important scheduling test.

    Alpaca returns the bar for the interval that is still running: at 10:16 it
    already serves a bar stamped 10:15, whose close has not happened. Acting on
    it would be trading a candle that is still moving. The bar becomes
    processable only once its whole interval has elapsed.
    """
    in_progress = datetime(2026, 8, 26, 10, 15, tzinfo=UTC)
    frame = make_bars(BTC, last_bar_start=in_progress, count=120)
    clock = FakeClock(datetime(2026, 8, 26, 10, 16, 17, tzinfo=UTC))
    data = FakeMarketData({BTC: frame, ETH: make_bars(ETH, last_bar_start=in_progress)})
    execution = FakeExecution()

    runtime = build_runtime(connection, market_data=data, execution=execution, clock=clock)
    runtime.start()
    report = runtime.run_cycle()

    # The newest *completed* bar is 10:00, not the 10:15 the provider returned.
    assert [result.bar_timestamp for result in report.results] == [
        datetime(2026, 8, 26, 10, 0, tzinfo=UTC),
        datetime(2026, 8, 26, 10, 0, tzinfo=UTC),
    ]
    assert runtime.heartbeat.last_processed_bars[BTC] == datetime(2026, 8, 26, 10, 0, tzinfo=UTC)

    # And it stays that way until 10:30 has genuinely passed.
    assert not is_bar_complete(in_progress, now=clock.now, safety_delay=DEFAULT_SAFETY_DELAY)
    clock.now = datetime(2026, 8, 26, 10, 29, 59, tzinfo=UTC)
    assert not is_bar_complete(in_progress, now=clock.now, safety_delay=DEFAULT_SAFETY_DELAY)
    clock.now = datetime(2026, 8, 26, 10, 30, 5, tzinfo=UTC)
    assert is_bar_complete(in_progress, now=clock.now, safety_delay=DEFAULT_SAFETY_DELAY)


def test_same_completed_bar_is_never_processed_twice_in_one_process(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """Two cycles, the same newest completed bar, one strategy action.

    A provider repeating the newest bar, a cycle that overran its boundary, or
    `--once` run twice in a minute all produce this shape. Sleep timing is not
    what prevents the second action; the checkpoint is.
    """
    frame = make_bars(BTC, count=120, closes=crossover_closes())
    data = FakeMarketData({BTC: frame, ETH: make_bars(ETH)})
    execution = FakeExecution()
    clock = FakeClock()

    runtime = build_runtime(connection, market_data=data, execution=execution, clock=clock)
    runtime.start()
    first = runtime.run_cycle()
    second = runtime.run_cycle()

    assert first.results[0].processed is True
    assert first.results[0].signal is not None
    assert second.results[0].processed is False
    assert second.results[0].skipped_reason == "ALREADY_PROCESSED"
    assert second.results[0].signal is None

    assert execution.symbols == [BTC], "the same bar produced a second submission"
    assert len(list_signals(connection)) == 1


def test_unknown_execution_outcome_pauses_future_trading(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """An ambiguous outcome stops submission for the life of the process.

    The order may or may not exist at the broker. A later signal must not be
    sent on top of a position nobody can describe: only reconciliation may
    resolve that, and it is not in this branch.
    """
    first_bar = T_BAR
    second_bar = T_BAR + BAR_INTERVAL
    data = FakeMarketData(
        {
            BTC: make_bars(BTC, last_bar_start=first_bar, closes=crossover_closes()),
            ETH: make_bars(ETH, last_bar_start=first_bar),
        }
    )
    execution = FakeExecution([AmbiguousSubmissionError("outcome unknown")])
    clock = FakeClock()
    runtime = build_runtime(connection, market_data=data, execution=execution, clock=clock)
    runtime.start()

    first = runtime.run_cycle()
    assert first.severity is CycleSeverity.TRADING_PAUSED
    assert runtime.state is RuntimeState.TRADING_PAUSED
    assert len(execution.calls) == 1

    # A brand-new bar with a brand-new signal, one cycle later.
    data.frames = {
        BTC: make_bars(BTC, last_bar_start=second_bar, closes=crossover_closes()),
        ETH: make_bars(ETH, last_bar_start=second_bar, closes=crossover_closes()),
    }
    clock.advance(BAR_INTERVAL)
    second = runtime.run_cycle()

    assert len(execution.calls) == 1, "a second order was submitted after an UNKNOWN outcome"
    assert [result.skipped_reason for result in second.results] == [
        "TRADING_PAUSED",
        "TRADING_PAUSED",
    ]
    # Observation continues: the bars were still processed and recorded.
    assert all(result.processed for result in second.results)
    assert {signal.symbol for signal in list_signals(connection)} == {BTC, ETH}


def test_second_runner_instance_is_refused(tmp_path: Path, monkeypatch) -> None:
    """A second runner exits before it processes a bar or reaches a broker.

    Two runners on one database would each hold their own in-process
    checkpoint, neither able to see the other's, and both would act on the same
    completed bar.
    """
    database = tmp_path / "state.db"
    initialize_database(database)

    def refuse_fetch(*args: object, **kwargs: object) -> None:
        raise AssertionError("the second runner reached market data")

    def refuse_execute(*args: object, **kwargs: object) -> None:
        raise AssertionError("the second runner reached the broker")

    monkeypatch.setattr(runtime_market_data.AlpacaCryptoBars, "recent_bars", refuse_fetch)
    monkeypatch.setattr(runtime_execution.PaperExecutionGateway, "execute", refuse_execute)

    holder = RuntimeLock(lock_path_for(database))
    holder.acquire()
    try:
        result = runner.invoke(
            app, ["crypto-run", "--once", "--observe-only", "--db", str(database)]
        )
    finally:
        holder.release()

    assert result.exit_code == 1
    assert "already holds" in result.output
    assert list_strategy_runs_for(database) == [], "the refused runner opened a strategy run"


def list_strategy_runs_for(database: Path) -> list[object]:
    with connect(database) as open_connection:
        return list(list_strategy_runs(open_connection))


# ==========================================================================
# 1-3, 8. UTC boundary arithmetic
# ==========================================================================


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        pytest.param(
            datetime(2026, 8, 26, 10, 7, 32, tzinfo=UTC),
            datetime(2026, 8, 26, 10, 15, tzinfo=UTC),
            id="10:07:32-to-10:15",
        ),
        pytest.param(
            datetime(2026, 8, 26, 10, 15, 1, tzinfo=UTC),
            datetime(2026, 8, 26, 10, 30, tzinfo=UTC),
            id="10:15:01-to-10:30",
        ),
        pytest.param(
            datetime(2026, 8, 26, 10, 15, tzinfo=UTC),
            datetime(2026, 8, 26, 10, 30, tzinfo=UTC),
            id="exactly-on-a-boundary-advances",
        ),
        pytest.param(
            datetime(2026, 8, 26, 23, 52, 9, tzinfo=UTC),
            datetime(2026, 8, 27, 0, 0, tzinfo=UTC),
            id="midnight-utc-rollover",
        ),
        pytest.param(
            datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC),
            datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
            id="new-year-rollover",
        ),
    ],
)
def test_next_boundary(now: datetime, expected: datetime) -> None:
    assert next_boundary(now) == expected


def test_the_safety_delay_is_applied_after_the_bar_boundary() -> None:
    """The wake-up is the boundary plus the delay, not the boundary itself."""
    now = datetime(2026, 8, 26, 10, 7, 32, tzinfo=UTC)
    assert next_wake_time(now, safety_delay=DEFAULT_SAFETY_DELAY) == datetime(
        2026, 8, 26, 10, 15, 5, tzinfo=UTC
    )
    assert next_wake_time(now, safety_delay=timedelta(seconds=30)) == datetime(
        2026, 8, 26, 10, 15, 30, tzinfo=UTC
    )


def test_the_safety_delay_holds_a_bar_back_until_the_provider_has_had_it() -> None:
    """An early wake-up cannot smuggle an unpublished bar through."""
    bar = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
    delay = timedelta(seconds=5)
    assert not is_bar_complete(
        bar, now=datetime(2026, 8, 26, 10, 15, tzinfo=UTC), safety_delay=delay
    )
    assert not is_bar_complete(
        bar, now=datetime(2026, 8, 26, 10, 15, 4, tzinfo=UTC), safety_delay=delay
    )
    assert is_bar_complete(
        bar, now=datetime(2026, 8, 26, 10, 15, 5, tzinfo=UTC), safety_delay=delay
    )


def test_latest_completed_bar_start_excludes_the_running_interval() -> None:
    assert latest_completed_bar_start(
        datetime(2026, 8, 26, 10, 15, 5, tzinfo=UTC), safety_delay=DEFAULT_SAFETY_DELAY
    ) == datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
    assert latest_completed_bar_start(
        datetime(2026, 8, 26, 10, 29, 59, tzinfo=UTC), safety_delay=DEFAULT_SAFETY_DELAY
    ) == datetime(2026, 8, 26, 10, 0, tzinfo=UTC)


def test_a_naive_datetime_is_refused_rather_than_assumed_utc() -> None:
    with pytest.raises(ScheduleError, match="timezone-aware"):
        next_boundary(datetime(2026, 8, 26, 10, 7))


def test_a_non_utc_aware_datetime_is_converted_not_refused() -> None:
    """The instant is unambiguous; only the frame differs."""
    plus_nine = datetime(2026, 8, 26, 19, 7, 32, tzinfo=timezone_of(9))
    assert next_boundary(plus_nine) == datetime(2026, 8, 26, 10, 15, tzinfo=UTC)


def timezone_of(hours: int):
    from datetime import timezone

    return timezone(timedelta(hours=hours))


# ==========================================================================
# 4-7. Crypto runs every day. There is no session anywhere.
# ==========================================================================


@pytest.mark.parametrize(
    ("label", "day"),
    [
        ("monday", datetime(2026, 8, 24, 10, 15, 5, tzinfo=UTC)),
        ("tuesday", datetime(2026, 8, 25, 10, 15, 5, tzinfo=UTC)),
        ("wednesday", datetime(2026, 8, 26, 10, 15, 5, tzinfo=UTC)),
        ("thursday", datetime(2026, 8, 27, 10, 15, 5, tzinfo=UTC)),
        ("friday", datetime(2026, 8, 28, 10, 15, 5, tzinfo=UTC)),
        ("saturday", datetime(2026, 8, 29, 10, 15, 5, tzinfo=UTC)),
        ("sunday", datetime(2026, 8, 30, 10, 15, 5, tzinfo=UTC)),
    ],
)
def test_every_day_of_the_week_operates_identically(
    database_path: Path, label: str, day: datetime, enabled_gate: None
) -> None:
    """Saturday and Sunday are ordinary trading days. So is every other one."""
    expected_bar = day.replace(minute=0, second=0, microsecond=0)
    with connect(database_path) as open_connection:
        data = FakeMarketData(
            {
                BTC: make_bars(BTC, last_bar_start=expected_bar, closes=crossover_closes()),
                ETH: make_bars(ETH, last_bar_start=expected_bar),
            }
        )
        execution = FakeExecution()
        runtime = build_runtime(
            open_connection, market_data=data, execution=execution, clock=FakeClock(day)
        )
        runtime.start()
        report = runtime.run_cycle()

    assert report.succeeded, label
    assert report.results[0].bar_timestamp == expected_bar, label
    assert execution.symbols == [BTC], label
    assert next_boundary(day) == day.replace(minute=30, second=0, microsecond=0), label


def test_the_runtime_source_contains_no_market_session_concept() -> None:
    """No `get_clock`, no calendar, no weekday filter - crypto has no session."""
    source = runtime_source()
    for forbidden in (
        "get_clock",
        "is_open",
        "next_open",
        "next_close",
        "market_open",
        "market_close",
        "America/New_York",
        "NYSE",
        "Nasdaq",
        "MarketCalendar",
        "weekday",
        "isoweekday",
        "dayofweek",
        "day_of_week",
        "is_weekend",
        "holiday",
    ):
        assert forbidden not in source, forbidden


def test_the_runtime_holds_no_timezone_but_utc() -> None:
    source = runtime_source()
    assert "ZoneInfo" not in source
    assert "pytz" not in source


# ==========================================================================
# 9-11. Completed bars, the latest bar, and a bounded lookback
# ==========================================================================


def test_the_latest_completed_bar_is_the_one_processed(connection: sqlite3.Connection) -> None:
    latest = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
    data = FakeMarketData(
        {
            BTC: make_bars(BTC, last_bar_start=latest),
            ETH: make_bars(ETH, last_bar_start=latest),
        }
    )
    runtime = build_runtime(connection, market_data=data)
    runtime.start()
    report = runtime.run_cycle()
    assert [result.bar_timestamp for result in report.results] == [latest, latest]


def test_the_lookback_is_bounded_between_100_and_200_bars() -> None:
    assert (MIN_LOOKBACK_BARS, MAX_LOOKBACK_BARS) == (100, 200)
    assert MIN_LOOKBACK_BARS <= DEFAULT_LOOKBACK_BARS <= MAX_LOOKBACK_BARS
    for accepted in (MIN_LOOKBACK_BARS, DEFAULT_LOOKBACK_BARS, MAX_LOOKBACK_BARS):
        assert require_lookback_bars(accepted) == accepted
    for refused in (0, 1, 51, 99, 201, 5_000, 35_040):
        with pytest.raises(ScheduleError):
            require_lookback_bars(refused)


def test_the_request_window_spans_exactly_the_lookback_and_stops_before_the_open_bar() -> None:
    """One bounded request, and never the candle that is still forming."""
    start, end = completed_window(T_NOW, lookback_bars=200, safety_delay=DEFAULT_SAFETY_DELAY)
    latest = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
    assert start == latest - 199 * BAR_INTERVAL
    assert end < latest + BAR_INTERVAL
    assert (end - start) < 200 * BAR_INTERVAL
    assert (end - start) > 199 * BAR_INTERVAL


def test_the_runtime_asks_for_a_bounded_window_every_cycle(
    connection: sqlite3.Connection,
) -> None:
    data = FakeMarketData()
    runtime = build_runtime(connection, market_data=data)
    runtime.start()
    runtime.run_cycle()
    assert [call[2] for call in data.calls] == [DEFAULT_LOOKBACK_BARS, DEFAULT_LOOKBACK_BARS]
    assert all(MIN_LOOKBACK_BARS <= call[2] <= MAX_LOOKBACK_BARS for call in data.calls)


# ==========================================================================
# 12-13. Only the newest completed bar may cause an action
# ==========================================================================


def test_historical_signals_are_not_replayed(connection: sqlite3.Connection) -> None:
    """The lookback establishes EMA state. It is not a queue of missed trades."""
    data = FakeMarketData(
        {
            BTC: make_bars(BTC, closes=mid_crossover_closes()),
            ETH: make_bars(ETH, closes=mid_crossover_closes()),
        }
    )
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=data, execution=execution)
    runtime.start()
    report = runtime.run_cycle()

    assert execution.calls == []
    assert [result.signal for result in report.results] == [None, None]
    assert list_signals(connection) == []


def test_only_the_latest_bar_signal_triggers_an_action(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    data = FakeMarketData(
        {
            BTC: make_bars(BTC, closes=crossover_closes()),
            ETH: make_bars(ETH, closes=mid_crossover_closes()),
        }
    )
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=data, execution=execution)
    runtime.start()
    report = runtime.run_cycle()

    assert execution.symbols == [BTC]
    assert report.results[0].signal is not None
    assert report.results[0].signal.timestamp == T_BAR
    assert report.results[1].signal is None
    assert [signal.symbol for signal in list_signals(connection)] == [BTC]


def test_an_exit_signal_becomes_a_sell(connection: sqlite3.Connection, enabled_gate: None) -> None:
    data = FakeMarketData(
        {
            BTC: make_bars(BTC, closes=crossover_closes(upward=False)),
            ETH: make_bars(ETH),
        }
    )
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=data, execution=execution)
    runtime.start()
    runtime.run_cycle()
    assert [call["side"] for call in execution.calls] == ["SELL"]


# ==========================================================================
# 14-15. Duplicate protection, per symbol
# ==========================================================================


@pytest.mark.parametrize("symbol", [BTC, ETH])
def test_a_completed_bar_is_processed_once_per_symbol(
    connection: sqlite3.Connection, symbol: str, enabled_gate: None
) -> None:
    other = ETH if symbol == BTC else BTC
    data = FakeMarketData(
        {
            symbol: make_bars(symbol, closes=crossover_closes()),
            other: make_bars(other),
        }
    )
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=data, execution=execution)
    runtime.start()
    runtime.run_cycle()
    runtime.run_cycle()
    runtime.run_cycle()
    assert execution.symbols == [symbol]


def test_the_checkpoint_never_moves_backwards() -> None:
    checkpoint = InMemoryCheckpoint()
    checkpoint.mark_processed(BTC, T_BAR)
    checkpoint.mark_processed(BTC, T_BAR - BAR_INTERVAL)
    assert checkpoint.last_processed(BTC) == T_BAR
    assert checkpoint.last_processed(ETH) is None


def test_a_new_bar_is_processed_after_the_previous_one(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """The guard blocks repeats, not progress."""
    data = FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes()), ETH: make_bars(ETH)})
    execution = FakeExecution()
    clock = FakeClock()
    runtime = build_runtime(connection, market_data=data, execution=execution, clock=clock)
    runtime.start()
    runtime.run_cycle()

    next_bar = T_BAR + BAR_INTERVAL
    data.frames = {
        BTC: make_bars(BTC, last_bar_start=next_bar, closes=crossover_closes()),
        ETH: make_bars(ETH, last_bar_start=next_bar),
    }
    clock.advance(BAR_INTERVAL)
    runtime.run_cycle()
    assert execution.symbols == [BTC, BTC]
    assert runtime.heartbeat.last_processed_bars[BTC] == next_bar


# ==========================================================================
# 16-17. Deterministic, sequential, one broker at a time
# ==========================================================================


def test_btc_is_processed_before_eth(connection: sqlite3.Connection, enabled_gate: None) -> None:
    assert PROCESSING_ORDER == (BTC, ETH) == SUPPORTED_SYMBOLS
    data = FakeMarketData(
        {
            BTC: make_bars(BTC, closes=crossover_closes()),
            ETH: make_bars(ETH, closes=crossover_closes()),
        }
    )
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=data, execution=execution)
    runtime.start()
    report = runtime.run_cycle()

    assert [call[0] for call in data.calls] == [BTC, ETH]
    assert execution.symbols == [BTC, ETH]
    assert [result.symbol for result in report.results] == [BTC, ETH]


def test_no_broker_submission_overlaps_another(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """`FakeExecution` raises on re-entry; a passing run proves they are serial."""
    data = FakeMarketData(
        {
            BTC: make_bars(BTC, closes=crossover_closes()),
            ETH: make_bars(ETH, closes=crossover_closes()),
        }
    )
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=data, execution=execution)
    runtime.start()
    runtime.run_cycle()
    assert len(execution.calls) == 2


def test_the_runtime_uses_no_concurrency_primitive() -> None:
    """Two symbols and one cycle every fifteen minutes need no event loop."""
    source = runtime_source()
    for forbidden in (
        "asyncio",
        "async def",
        "await ",
        "threading",
        "Thread(",
        "ThreadPool",
        "ProcessPool",
        "concurrent.futures",
        "multiprocessing",
    ):
        assert forbidden not in source, forbidden


# ==========================================================================
# 18-20. No order is fabricated
# ==========================================================================


def test_invalid_bars_cause_no_order(connection: sqlite3.Connection) -> None:
    """Validation failure fails the cycle closed. Nothing is sorted or repaired."""
    broken = make_bars(BTC)
    broken.loc[10, "high"] = 1.0  # high < low
    data = FakeMarketData({BTC: broken, ETH: make_bars(ETH)})
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=data, execution=execution)
    runtime.start()
    report = runtime.run_cycle()

    assert execution.calls == []
    assert report.severity is CycleSeverity.RETRY_NEXT_CYCLE
    assert "INVALID_OHLC" in (report.error or "")
    assert runtime.heartbeat.last_processed_bars[BTC] is None


def test_a_bar_off_a_15_minute_boundary_is_refused(connection: sqlite3.Connection) -> None:
    misaligned = make_bars(BTC, last_bar_start=datetime(2026, 8, 26, 10, 0, tzinfo=UTC))
    misaligned["timestamp"] = misaligned["timestamp"] + pd.Timedelta(seconds=7)
    data = FakeMarketData({BTC: misaligned, ETH: make_bars(ETH)})
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=data, execution=execution)
    runtime.start()
    report = runtime.run_cycle()
    assert execution.calls == []
    assert report.severity is CycleSeverity.RETRY_NEXT_CYCLE
    assert "not a 15-minute UTC boundary" in (report.error or "")


def test_no_signal_causes_no_order(connection: sqlite3.Connection) -> None:
    """A processed bar is not a reason to trade."""
    data = FakeMarketData({BTC: make_bars(BTC), ETH: make_bars(ETH)})
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=data, execution=execution)
    runtime.start()
    report = runtime.run_cycle()

    assert execution.calls == []
    assert report.succeeded
    assert all(result.processed for result in report.results)
    assert list_signals(connection) == []


def test_a_risk_rejection_is_an_ordinary_no_order_result(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """An EXIT while flat is refused by C5, and that is not a runtime failure."""
    data = FakeMarketData(
        {BTC: make_bars(BTC, closes=crossover_closes(upward=False)), ETH: make_bars(ETH)}
    )
    execution = FakeExecution(
        [
            make_execution_result(
                side=OrderSide.SELL,
                outcome=ExecutionOutcome.REJECTED_BY_RISK,
                approved=False,
                reason_code=NO_POSITION_TO_EXIT,
            )
        ]
    )
    runtime = build_runtime(connection, market_data=data, execution=execution)
    runtime.start()
    report = runtime.run_cycle()

    assert report.succeeded
    assert runtime.state is RuntimeState.RUNNING
    assert report.results[0].execution is not None
    assert report.results[0].execution.outcome is ExecutionOutcome.REJECTED_BY_RISK
    assert runtime.heartbeat.orders_submitted == 0


def test_risk_is_the_only_sizing_authority(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """The runtime asks for more than any ceiling and lets C5 clamp it."""
    data = FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes()), ETH: make_bars(ETH)})
    execution = FakeExecution([make_execution_result(reason_code=POSITION_LIMIT)])
    runtime = build_runtime(connection, market_data=data, execution=execution)
    runtime.start()
    runtime.run_cycle()

    assert execution.calls[0]["requested_quantity"] == RISK_SIZED_REQUEST_QUANTITY
    assert isinstance(RISK_SIZED_REQUEST_QUANTITY, Decimal)
    assert Decimal("1000000") < RISK_SIZED_REQUEST_QUANTITY


# ==========================================================================
# 21-25. The gates. Every one closed by default.
# ==========================================================================


def test_the_paper_environment_gate_being_closed_causes_no_order(
    connection: sqlite3.Connection,
) -> None:
    data = FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes()), ETH: make_bars(ETH)})
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=data, execution=execution)
    runtime.start()
    report = runtime.run_cycle()

    assert execution.calls == []
    assert runtime.authorization.enabled is False
    assert runtime.authorization.reason == "PAPER_ENV_GATE_DISABLED"
    assert report.results[0].skipped_reason == "PAPER_ENV_GATE_DISABLED"


def test_a_missing_runtime_confirmation_causes_no_order(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    data = FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes()), ETH: make_bars(ETH)})
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=data, execution=execution, confirmation=None)
    runtime.start()
    runtime.run_cycle()
    assert execution.calls == []
    assert runtime.authorization.reason == "RUNTIME_CONFIRMATION_MISSING"


@pytest.mark.parametrize("token", ["", "paper", "Paper", "PAPER ", "YES", "CONFIRM"])
def test_only_the_exact_runtime_token_authorizes_execution(
    connection: sqlite3.Connection, enabled_gate: None, token: str
) -> None:
    runtime = build_runtime(connection, confirmation=token)
    runtime.start()
    assert runtime.authorization.enabled is False
    assert runtime.authorization.reason == "RUNTIME_CONFIRMATION_MISSING"


def test_startup_safety_false_causes_no_order(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    data = FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes()), ETH: make_bars(ETH)})
    execution = FakeExecution()
    runtime = build_runtime(
        connection, market_data=data, execution=execution, startup_safety=unsafe_startup
    )
    runtime.start()
    report = runtime.run_cycle()

    assert execution.calls == []
    assert runtime.authorization.reason == "STARTUP_SAFETY_UNSAFE"
    assert report.results[0].skipped_reason == "STARTUP_SAFETY_UNSAFE"
    # Observation is unaffected.
    assert report.results[0].processed is True
    assert [signal.symbol for signal in list_signals(connection)] == [BTC]


def test_startup_safety_unresolved_causes_no_order(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """The shipped default. Nobody has checked, so nothing is sent."""
    data = FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes()), ETH: make_bars(ETH)})
    execution = FakeExecution()
    runtime = build_runtime(
        connection,
        market_data=data,
        execution=execution,
        startup_safety=unresolved_startup_safety,
    )
    runtime.start()
    report = runtime.run_cycle()

    assert execution.calls == []
    assert runtime.authorization.reason == "STARTUP_SAFETY_UNRESOLVED"
    assert runtime.heartbeat.startup_safety_code == STARTUP_SAFETY_UNRESOLVED
    assert report.results[0].skipped_reason == "STARTUP_SAFETY_UNRESOLVED"


def test_the_production_default_startup_safety_is_not_safe() -> None:
    result = unresolved_startup_safety()
    assert result.safe_to_trade is False
    assert result.code == STARTUP_SAFETY_UNRESOLVED


def test_a_startup_safety_result_cannot_contradict_itself() -> None:
    with pytest.raises(ValueError, match="contradicts"):
        StartupSafetyResult(True, STARTUP_SAFETY_UNRESOLVED, "impossible")
    with pytest.raises(ValueError, match="contradicts"):
        StartupSafetyResult(False, STARTUP_SAFETY_SAFE, "impossible")


def test_a_safe_startup_check_can_enable_execution(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """The seam works in the affirmative direction too - for tests, not for prod."""
    data = FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes()), ETH: make_bars(ETH)})
    execution = FakeExecution()
    runtime = build_runtime(
        connection, market_data=data, execution=execution, startup_safety=safe_startup
    )
    runtime.start()
    report = runtime.run_cycle()

    assert runtime.authorization.enabled is True
    assert runtime.authorization.reason is None
    assert execution.symbols == [BTC]
    assert report.results[0].execution is not None
    assert runtime.heartbeat.orders_submitted == 1


def test_observe_only_removes_the_execution_path_entirely(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """Not a refused submission - an unavailable one."""
    data = FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes()), ETH: make_bars(ETH)})
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=data, execution=execution, observe_only=True)
    runtime.start()
    report = runtime.run_cycle()

    assert execution.calls == []
    assert runtime.authorization.reason == "OBSERVE_ONLY"
    assert report.results[0].skipped_reason == "OBSERVE_ONLY"


def test_the_runtime_start_does_not_bypass_the_execution_layers_own_gate() -> None:
    """The runtime checks the env gate; it never sets, clears, or forges it."""
    source = runtime_source()
    assert "os.environ[" not in source
    assert "setenv" not in source
    assert "PAPER_TRADING_ENABLED_VALUE" not in source
    assert "paper_trading_enabled" in source


# ==========================================================================
# 26-29. Runtime failure policy
# ==========================================================================


def test_a_temporary_data_error_waits_for_the_next_cycle(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    data = FakeMarketData(error=HistoricalDataError("provider unavailable"))
    execution = FakeExecution()
    clock = FakeClock()
    runtime = build_runtime(connection, market_data=data, execution=execution, clock=clock)
    runtime.start()
    first = runtime.run_cycle()

    assert first.severity is CycleSeverity.RETRY_NEXT_CYCLE
    assert runtime.state is RuntimeState.RUNNING
    assert execution.calls == []

    data.error = None
    data.frames = {BTC: make_bars(BTC, closes=crossover_closes()), ETH: make_bars(ETH)}
    second = runtime.run_cycle()
    assert second.succeeded
    assert execution.symbols == [BTC]


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(BrokerAuthenticationError("credentials rejected"), id="auth"),
        pytest.param(MissingCredentialsError("not configured"), id="missing-credentials"),
        pytest.param(AccountNotTradableError("account blocked"), id="account-blocked"),
    ],
)
def test_a_broker_authentication_or_account_failure_fails_closed(
    connection: sqlite3.Connection, failure: Exception, enabled_gate: None
) -> None:
    """None of these improves by being retried every fifteen minutes."""
    data = FakeMarketData(
        {
            BTC: make_bars(BTC, closes=crossover_closes()),
            ETH: make_bars(ETH, closes=crossover_closes()),
        }
    )
    execution = FakeExecution([failure])
    runtime = build_runtime(connection, market_data=data, execution=execution)
    runtime.start()
    report = runtime.run_cycle()

    assert report.severity is CycleSeverity.FATAL
    assert runtime.state is RuntimeState.FAILED
    assert len(execution.calls) == 1, "the cycle continued to the next symbol after a fatal error"


def test_the_failure_classification_is_small_and_explicit() -> None:
    assert classify(AmbiguousSubmissionError("x")) is CycleSeverity.TRADING_PAUSED
    assert classify(BrokerAuthenticationError("x")) is CycleSeverity.FATAL
    assert classify(MissingCredentialsError("x")) is CycleSeverity.FATAL
    assert classify(AccountNotTradableError("x")) is CycleSeverity.FATAL
    assert classify(HistoricalDataError("x")) is CycleSeverity.RETRY_NEXT_CYCLE
    assert classify(ExecutionError("x")) is CycleSeverity.RETRY_NEXT_CYCLE
    assert classify(ValueError("unexpected")) is CycleSeverity.FATAL


def test_a_paused_runtime_stops_scheduling_further_cycles(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    data = FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes()), ETH: make_bars(ETH)})
    execution = FakeExecution([AmbiguousSubmissionError("outcome unknown")])
    runtime = build_runtime(connection, market_data=data, execution=execution)
    reports = runtime.run_forever(max_cycles=5)

    assert len(reports) == 1
    assert runtime.state is RuntimeState.TRADING_PAUSED
    (run,) = list_strategy_runs(connection)
    assert run.status == RUN_STATUS_FAILED
    assert any(
        event.event_type == "RUNTIME_TRADING_PAUSED" for event in list_system_events(connection)
    )


def test_the_runtime_never_resolves_an_unknown_outcome() -> None:
    """Resolution is Phase 8's. This branch must not attempt any part of it."""
    source = runtime_source()
    for forbidden in (
        "get_order_by_client_id",
        "find_broker_order_by_client_id",
        "list_orders",
        "cancel_order",
        "close_position",
        "INTENT_STATUS_UNKNOWN",
    ):
        assert forbidden not in source, forbidden


# ==========================================================================
# 30-32. Heartbeat
# ==========================================================================


def test_the_heartbeat_is_updated_after_a_successful_cycle(
    connection: sqlite3.Connection,
) -> None:
    clock = FakeClock()
    runtime = build_runtime(connection, clock=clock)
    runtime.start()

    before = runtime.heartbeat
    assert before.started_at == T_NOW
    assert before.last_successful_cycle_at is None

    runtime.run_cycle()
    after = runtime.heartbeat
    assert after.last_cycle_started_at == T_NOW
    assert after.last_successful_cycle_at == T_NOW
    assert after.cycles_started == 1
    assert after.cycles_completed == 1
    assert after.api_calls_total == 2


def test_the_heartbeat_exposes_the_last_processed_bar_per_symbol(
    connection: sqlite3.Connection,
) -> None:
    runtime = build_runtime(connection)
    runtime.start()
    assert runtime.heartbeat.last_processed_bars == {BTC: None, ETH: None}
    runtime.run_cycle()
    assert runtime.heartbeat.last_processed_bars == {BTC: T_BAR, ETH: T_BAR}


def test_errors_are_reflected_in_the_heartbeat(connection: sqlite3.Connection) -> None:
    data = FakeMarketData(error=HistoricalDataError("provider unavailable"))
    runtime = build_runtime(connection, market_data=data)
    runtime.start()
    runtime.run_cycle()
    heartbeat = runtime.heartbeat
    assert heartbeat.last_error is not None
    assert "provider unavailable" in heartbeat.last_error
    assert heartbeat.cycles_started == 1
    assert heartbeat.cycles_completed == 0
    assert heartbeat.last_successful_cycle_at is None


def test_the_heartbeat_reports_why_execution_is_disabled(connection: sqlite3.Connection) -> None:
    runtime = build_runtime(connection)
    runtime.start()
    heartbeat = runtime.heartbeat
    assert heartbeat.paper_execution_enabled is False
    assert heartbeat.execution_disabled_reason == "PAPER_ENV_GATE_DISABLED"
    assert "paper_execution_enabled=false" in format_event("heartbeat", **heartbeat.as_fields())


def test_a_strategy_run_spans_the_runtime_session(connection: sqlite3.Connection) -> None:
    runtime = build_runtime(connection)
    report = runtime.run_once()
    assert report.succeeded
    (run,) = list_strategy_runs(connection)
    assert run.mode == "PAPER"
    assert run.status == RUN_STATUS_COMPLETED
    assert run.ended_at is not None
    types = {event.event_type for event in list_system_events(connection)}
    assert {"RUNTIME_STARTED", "RUNTIME_STOPPED"} <= types


def test_an_identical_signal_is_not_recorded_twice(connection: sqlite3.Connection) -> None:
    """The storage invariant is respected, not worked around."""
    data = FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes()), ETH: make_bars(ETH)})
    runtime = build_runtime(connection, market_data=data, checkpoint=InMemoryCheckpoint())
    runtime.start()
    runtime.run_cycle()
    # A fresh checkpoint re-offers the same bar; the signal must not duplicate.
    runtime._checkpoint = InMemoryCheckpoint()  # noqa: SLF001 - exercising the storage guard
    runtime.run_cycle()
    assert len(list_signals(connection)) == 1


# ==========================================================================
# 33-34. Graceful shutdown
# ==========================================================================


@pytest.mark.parametrize(
    ("signal_number", "name"),
    [(signal.SIGTERM, "SIGTERM"), (signal.SIGINT, "SIGINT")],
)
def test_a_signal_stops_the_runtime_cleanly(
    connection: sqlite3.Connection, signal_number: int, name: str
) -> None:
    """The handler sets a flag; the loop stops at its next safe point."""
    shutdown = ShutdownRequest()

    def sleeper(seconds: float) -> None:
        os.kill(os.getpid(), signal_number)

    runtime = build_runtime(connection, shutdown=shutdown, sleep=sleeper)
    shutdown.install()
    try:
        reports = runtime.run_forever(max_cycles=5)
    finally:
        shutdown.restore()

    assert reports == []
    assert shutdown.requested is True
    assert shutdown.signal_name == name
    assert runtime.state is RuntimeState.STOPPED
    (run,) = list_strategy_runs(connection)
    assert run.status == RUN_STATUS_COMPLETED
    assert run.ended_at is not None


def test_a_shutdown_request_stops_new_submissions_mid_cycle(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """A signal arriving mid-cycle must not start a new order."""
    shutdown = ShutdownRequest()
    shutdown.request("test")
    data = FakeMarketData(
        {
            BTC: make_bars(BTC, closes=crossover_closes()),
            ETH: make_bars(ETH, closes=crossover_closes()),
        }
    )
    execution = FakeExecution()
    runtime = build_runtime(connection, market_data=data, execution=execution, shutdown=shutdown)
    runtime.start()
    report = runtime.run_cycle()

    assert execution.calls == []
    assert report.results == []


def test_the_shutdown_handler_restores_the_previous_handlers() -> None:
    previous = signal.getsignal(signal.SIGTERM)
    shutdown = ShutdownRequest()
    shutdown.install()
    assert signal.getsignal(signal.SIGTERM) is not previous
    shutdown.restore()
    assert signal.getsignal(signal.SIGTERM) is previous


# ==========================================================================
# 35-37. Single-instance lock
# ==========================================================================


def test_a_second_runner_cannot_take_the_lock(tmp_path: Path) -> None:
    path = tmp_path / "state.db.runtime.lock"
    first = RuntimeLock(path)
    first.acquire()
    try:
        with pytest.raises(RuntimeLockError, match="already holds"):
            RuntimeLock(path).acquire()
    finally:
        first.release()


def test_the_lock_is_released_on_a_clean_exit(tmp_path: Path) -> None:
    path = tmp_path / "state.db.runtime.lock"
    with RuntimeLock(path) as held:
        assert held.held is True
        assert path.exists()
    assert not path.exists()

    second = RuntimeLock(path)
    second.acquire()
    assert second.held is True
    second.release()


def test_the_lock_is_released_when_the_body_raises(tmp_path: Path) -> None:
    """`finally` semantics: a crashed runner must not wedge the next start."""
    path = tmp_path / "state.db.runtime.lock"
    with pytest.raises(RuntimeError, match="boom"), RuntimeLock(path):
        raise RuntimeError("boom")

    second = RuntimeLock(path)
    second.acquire()
    assert second.held is True
    second.release()


def test_the_lock_path_is_derived_from_the_database(tmp_path: Path) -> None:
    assert lock_path_for(tmp_path / "autotrader.db") == tmp_path / "autotrader.db.runtime.lock"


def test_the_lock_uses_an_os_lock_not_a_bare_pid_file() -> None:
    source = code_without_prose(Path(runtime_lock.__file__).read_text())
    assert "fcntl.flock" in source
    assert "LOCK_EX" in source and "LOCK_NB" in source


def test_releasing_an_unheld_lock_is_harmless(tmp_path: Path) -> None:
    lock = RuntimeLock(tmp_path / "state.db.runtime.lock")
    lock.release()
    assert lock.held is False


# ==========================================================================
# 38. Logging never carries a secret
# ==========================================================================


def test_runtime_logs_contain_no_credential(
    connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    enabled_gate: None,
) -> None:
    secret_key = "SECRET-VALUE-THAT-MUST-NEVER-BE-LOGGED"
    api_key = "API-KEY-THAT-MUST-NEVER-BE-LOGGED"
    monkeypatch.setenv("ALPACA_API_KEY", api_key)
    monkeypatch.setenv("ALPACA_SECRET_KEY", secret_key)

    data = FakeMarketData(
        {
            BTC: make_bars(BTC, closes=crossover_closes()),
            ETH: make_bars(ETH, closes=crossover_closes(upward=False)),
        }
    )
    runtime = build_runtime(connection, market_data=data)
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        runtime.run_once()

    emitted = "\n".join(record.getMessage() for record in caplog.records)
    assert emitted, "the runtime logged nothing at all"
    assert "event=runtime_started" in emitted
    assert "event=cycle_started" in emitted
    assert "event=bar_processed" in emitted
    assert "event=heartbeat" in emitted
    for secret in (api_key, secret_key, "ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        assert secret not in emitted, secret


def test_the_runtime_source_never_reads_a_credential() -> None:
    source = runtime_source()
    for forbidden in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "api_key", "secret_key"):
        assert forbidden not in source, forbidden


def test_structured_events_are_parseable_key_value_lines() -> None:
    line = format_event("bar_processed", symbol=BTC, timestamp=T_BAR, bars=200)
    assert line == f"event=bar_processed symbol=BTC/USD timestamp={T_BAR.isoformat()} bars=200"
    quoted = format_event("cycle_error", error="two words")
    assert quoted == 'event=cycle_error error="two words"'


def test_the_runtime_adds_no_monitoring_dependency() -> None:
    """Standard-library logging only. No agent, no chat, no webhook."""
    source = runtime_source()
    for forbidden in (
        "telegram",
        "Telegram",
        "slack",
        "Slack",
        "discord",
        "Discord",
        "smtplib",
        "sendmail",
        "twilio",
        "requests.post",
        "httpx",
        "webhook",
        "prometheus",
        "statsd",
        "datadog",
        "sentry",
    ):
        assert forbidden not in source, forbidden


# ==========================================================================
# 39-41. Scope
# ==========================================================================


def test_there_is_no_live_mode_in_the_runtime() -> None:
    source = runtime_source()
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


def test_the_runtime_constructs_no_trading_client_of_its_own() -> None:
    """Only C7 builds a trading client; the runtime asks C7 for one."""
    source = code_without_prose(Path(runtime_runner.__file__).read_text())
    assert "TradingClient(" not in source
    assert "paper=" not in source


def test_no_cli_option_can_request_live_trading() -> None:
    from autotrader import cli

    source = code_without_prose(Path(cli.__file__).read_text())
    for forbidden in ("--live", "paper=False", "live=True", "stock-run", "live-run"):
        assert forbidden not in source, forbidden


def test_the_universe_is_exactly_btc_and_eth() -> None:
    assert PROCESSING_ORDER == ("BTC/USD", "ETH/USD")
    assert len(PROCESSING_ORDER) == 2


def test_no_equity_symbol_appears_in_the_runtime() -> None:
    source = runtime_source()
    for forbidden in (
        "SPY",
        "QQQ",
        "AAPL",
        "MSFT",
        "NVDA",
        "StockHistoricalDataClient",
        "StockLatestTradeRequest",
        "StockBarsRequest",
        "TimeInForce.DAY",
    ):
        assert forbidden not in source, forbidden


def test_the_runtime_defines_no_deployment_artefact() -> None:
    """Systemd units, containers and provisioning are Phase 10."""
    source = runtime_source()
    for forbidden in ("systemd", "Dockerfile", "docker", "ExecStart", "crontab", "supervisord"):
        assert forbidden not in source, forbidden


def test_the_runtime_modifies_no_database_schema() -> None:
    """Phase 9 stores nothing new. The checkpoint lives in memory."""
    source = runtime_source()
    for forbidden in (
        "CREATE TABLE",
        "ALTER TABLE",
        "DROP TABLE",
        "SCHEMA_VERSION",
        "executescript",
    ):
        assert forbidden not in source, forbidden


# ==========================================================================
# CLI
# ==========================================================================


def test_crypto_run_once_observes_without_submitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.db"
    frames = {BTC: make_bars(BTC, closes=crossover_closes()), ETH: make_bars(ETH)}

    def fake_recent_bars(self, symbol, *, now, lookback_bars):  # noqa: ANN001, ANN202
        return frames[symbol]

    monkeypatch.setattr(runtime_market_data.AlpacaCryptoBars, "recent_bars", fake_recent_bars)
    monkeypatch.setattr(
        runtime_execution.PaperExecutionGateway,
        "execute",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("submitted in observe mode")),
    )

    result = runner.invoke(app, ["crypto-run", "--once", "--observe-only", "--db", str(database)])
    assert result.exit_code == 0, result.output
    assert "OBSERVATION ONLY - NO ORDER WILL BE SUBMITTED" in result.output
    assert "OBSERVE_ONLY" in result.output
    assert "BTC/USD, ETH/USD" in result.output


def test_crypto_run_reports_the_startup_safety_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled_gate: None
) -> None:
    """Both gates open, and it still refuses - because nothing reconciled."""
    database = tmp_path / "state.db"
    frames = {BTC: make_bars(BTC), ETH: make_bars(ETH)}
    monkeypatch.setattr(
        runtime_market_data.AlpacaCryptoBars,
        "recent_bars",
        lambda self, symbol, *, now, lookback_bars: frames[symbol],
    )
    monkeypatch.setattr(
        runtime_execution.PaperExecutionGateway,
        "execute",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("submitted")),
    )

    result = runner.invoke(
        app,
        [
            "crypto-run",
            "--once",
            "--confirm-paper-runtime",
            RUNTIME_CONFIRMATION_TOKEN,
            "--db",
            str(database),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "STARTUP_SAFETY_UNRESOLVED" in result.output
    assert "OBSERVATION ONLY" in result.output


def test_crypto_run_rejects_an_impossible_safety_delay(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["crypto-run", "--once", "--safety-delay", "1200", "--db", str(tmp_path / "state.db")],
    )
    assert result.exit_code == 1
    assert "safety_delay" in result.output


def test_crypto_run_help_documents_the_gates() -> None:
    result = runner.invoke(app, ["crypto-run", "--help"])
    assert result.exit_code == 0
    assert "--once" in result.output
    assert "--confirm-paper-runtime" in result.output
    assert "--observe-only" in result.output
    assert "AUTOTRADER_PAPER_TRADING_ENABLED" in result.output


def test_there_is_no_stock_run_or_live_run_command() -> None:
    names = {command.name for command in app.registered_commands}
    assert "crypto-run" in names
    assert "stock-run" not in names
    assert "live-run" not in names
    assert "live" not in names
