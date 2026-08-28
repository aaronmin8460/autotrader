"""Integration tests: Phase 8 reconciliation as the Phase 9 runtime's startup gate.

The two phases were built in parallel against one written contract:

> A runtime may begin trading only when a reconciliation result reports
> `safe_to_trade` true.

These are the tests for that sentence, plus the durable per-symbol bar
checkpoint the integration adds so a *restart* cannot replay a completed bar
the previous process already acted on.

**Everything here is offline.** The broker boundary is the same fake C8's own
tests use; the market-data, execution and clock boundaries are the same fakes
C9's own tests use. Nothing opens a socket and nothing waits on a real
fifteen-minute boundary.

**The tests that matter most are again about not acting.** Two open paper gates
plus an unresolved reconciliation must still submit nothing; a restarted
process must not re-decide a bar it already claimed; and a claim must be
committed to disk *before* anything reaches the broker, because the safety
preference this system chose is explicit:

    miss a trade rather than duplicate a trade.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autotrader.cli import app
from autotrader.execution.paper import (
    PAPER_TRADING_ENABLED_ENV,
    PAPER_TRADING_ENABLED_VALUE,
    AmbiguousSubmissionError,
)
from autotrader.reconciliation import (
    CATEGORY_ORDER,
    CATEGORY_RUN,
    ItemOutcome,
    ReconciliationIssue,
    ReconciliationResult,
    ReconciliationStatus,
    reconcile_paper_state,
)
from autotrader.runtime.checkpoint import SqliteCheckpoint
from autotrader.runtime.lock import RuntimeLock, RuntimeLockError, lock_path_for
from autotrader.runtime.monitoring import RuntimeState
from autotrader.runtime.runner import (
    PROCESSING_ORDER,
    RUNTIME_CONFIRMATION_TOKEN,
    CryptoRuntime,
    RuntimeConfig,
)
from autotrader.runtime.safety import (
    RECONCILIATION_NOT_SAFE_BANNER,
    STARTUP_SAFETY_SAFE,
    STARTUP_SAFETY_UNSAFE,
    StartupSafetyResult,
    startup_safety_from_reconciliation,
    startup_safety_from_reconciliation_result,
)
from autotrader.state.sqlite import (
    INTENT_STATUS_CONFIRMED_NOT_SUBMITTED,
    INTENT_STATUS_CREATED,
    INTENT_STATUS_SUBMITTED,
    INTENT_STATUS_UNKNOWN,
    connect,
    get_broker_order_by_intent,
    get_order_intent,
    get_position,
    get_runtime_checkpoint,
    initialize_database,
    list_reconciliation_runs,
    list_system_events,
    upsert_position,
)
from conftest import establish_account_safety
from test_reconciliation import (
    BROKER_ORDER_UUID,
    FakeTradingClient,
    make_account,
    make_order,
    make_position,
    no_sleep,
    store_snapshot,
)
from test_reconciliation import (
    make_intent as make_reconcilable_intent,
)
from test_runtime import (
    BTC,
    ETH,
    T_BAR,
    T_NOW,
    FakeClock,
    FakeExecution,
    FakeMarketData,
    crossover_closes,
    make_bars,
)

from alpaca.trading.enums import OrderStatus  # isort: skip

runner = CliRunner()

#: A Saturday, and the 10:00 bar on it. Crypto does not care, and a test below
#: proves the integrated runtime does not either.
T_BAR_SATURDAY = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
T_NOW_SATURDAY = datetime(2026, 8, 29, 10, 15, 5, tzinfo=UTC)


# ==========================================================================
# Helpers
# ==========================================================================


@pytest.fixture(autouse=True)
def _closed_gates_and_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every gate shut and no credentials, whatever the developer's shell had.

    Autouse because both would otherwise make a "nothing was submitted"
    assertion pass - or fail - for a reason that has nothing to do with the
    code under test, and because a stray credential would let a startup pass
    open a socket.
    """
    monkeypatch.delenv(PAPER_TRADING_ENABLED_ENV, raising=False)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)


@pytest.fixture
def paper_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open the C7 environment gate. Requested only where a test wants an order."""
    monkeypatch.setenv(PAPER_TRADING_ENABLED_ENV, PAPER_TRADING_ENABLED_VALUE)


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    """A database a real reconciliation pass has already vouched for.

    Cases here hand the runtime a *synthesized* `StartupSafetyResult` rather
    than running a pass, which is what makes them fast and deterministic - but
    it means nothing writes the durable account safety row that a real pass
    writes. Establishing it here puts the account where a genuinely reconciled
    one starts. The cases that are about the halt still create it themselves.
    """
    path = initialize_database(tmp_path / "state.db")
    with connect(path) as setup:
        establish_account_safety(setup)
    return path


@pytest.fixture
def connection(database_path: Path):
    with connect(database_path) as open_connection:
        yield open_connection


def make_result(
    status: ReconciliationStatus,
    *,
    issues: tuple[ReconciliationIssue, ...] = (),
    orders_checked: int = 1,
    positions_checked: int = 2,
) -> ReconciliationResult:
    """A finished C8 pass with the status a test wants to gate on."""
    return ReconciliationResult(
        status=status,
        started_at=T_NOW,
        completed_at=T_NOW,
        orders_checked=orders_checked,
        positions_checked=positions_checked,
        issues=issues,
    )


def blocking_issue(detail: str = "an order lookup timed out") -> ReconciliationIssue:
    return ReconciliationIssue(
        category=CATEGORY_ORDER, outcome=ItemOutcome.UNRESOLVED, detail=detail, symbol=BTC
    )


def repaired_issue(detail: str = "accepted -> filled from broker truth") -> ReconciliationIssue:
    return ReconciliationIssue(
        category=CATEGORY_ORDER, outcome=ItemOutcome.REPAIRED, detail=detail, symbol=BTC
    )


def failed_issue(detail: str = "the paper account could not be read") -> ReconciliationIssue:
    return ReconciliationIssue(category=CATEGORY_RUN, outcome=ItemOutcome.FAILED, detail=detail)


class RecordingCheck:
    """A startup-safety check that counts how many times it was asked.

    A reconciliation pass is a *per-process* question. Counting the calls is
    how the restart tests prove a previous green answer was not carried over.
    """

    def __init__(self, result: StartupSafetyResult) -> None:
        self.result = result
        self.calls = 0

    def __call__(self) -> StartupSafetyResult:
        self.calls += 1
        return self.result


def build(
    connection: sqlite3.Connection,
    *,
    startup_safety,
    market_data: FakeMarketData | None = None,
    execution: FakeExecution | None = None,
    checkpoint=None,
    confirmation: str | None = RUNTIME_CONFIRMATION_TOKEN,
    observe_only: bool = False,
    clock: FakeClock | None = None,
) -> CryptoRuntime:
    """A runtime with the broker faked and the startup gate under test.

    `checkpoint` defaults to None, which means the runtime builds its own
    durable `SqliteCheckpoint` on this connection - the production default, and
    the thing most of these tests are actually about.
    """
    frames = {
        BTC: make_bars(BTC, closes=crossover_closes()),
        ETH: make_bars(ETH, closes=crossover_closes()),
    }
    return CryptoRuntime(
        connection,
        market_data=market_data if market_data is not None else FakeMarketData(frames),
        execution=execution if execution is not None else FakeExecution(),
        startup_safety=startup_safety,
        checkpoint=checkpoint,
        config=RuntimeConfig(observe_only=observe_only, runtime_confirmation=confirmation),
        clock=clock if clock is not None else FakeClock(),
    )


# ==========================================================================
# THE FOUR NAMED CRITICAL REGRESSIONS
# ==========================================================================


def test_runtime_cannot_trade_when_reconciliation_is_unresolved(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NAMED REGRESSION 1. Both paper gates open, reconciliation UNRESOLVED, zero orders.

    This is the invariant the whole integration exists for. The environment
    gate is set, the runtime confirmation token is correct, a crossover signal
    is waiting on the newest completed bar for both symbols - and because the
    startup pass could not settle something, nothing is sent.
    """
    monkeypatch.setenv(PAPER_TRADING_ENABLED_ENV, PAPER_TRADING_ENABLED_VALUE)
    execution = FakeExecution()
    result = make_result(ReconciliationStatus.UNRESOLVED, issues=(blocking_issue(),))
    runtime = build(
        connection,
        startup_safety=lambda: startup_safety_from_reconciliation_result(result),
        execution=execution,
        confirmation=RUNTIME_CONFIRMATION_TOKEN,
    )

    runtime.start()
    report = runtime.run_cycle()

    assert len(execution.calls) == 0
    assert execution.symbols == []
    assert runtime.heartbeat.orders_submitted == 0
    assert runtime.authorization.disabled
    assert runtime.authorization.reason == "STARTUP_SAFETY_UNSAFE"
    assert runtime.heartbeat.reconciliation_status == "UNRESOLVED"
    # The bars were still observed; only submission was withheld.
    assert [entry.skipped_reason for entry in report.results] == [
        "STARTUP_SAFETY_UNSAFE",
        "STARTUP_SAFETY_UNSAFE",
    ]
    assert all(entry.processed for entry in report.results)


def test_completed_bar_checkpoint_survives_process_restart(
    database_path: Path, paper_gate: None
) -> None:
    """NAMED REGRESSION 2. Process A claims a BTC bar; process B must not re-act on it.

    Two genuinely separate connections, opened and closed in turn, standing in
    for two process lifetimes against one database file. The second one sees
    the first one's claim and skips the bar.
    """
    frames = {BTC: make_bars(BTC, closes=crossover_closes()), ETH: make_bars(ETH)}

    # ---- process A ----
    execution_a = FakeExecution()
    with connect(database_path) as connection_a:
        runtime_a = build(
            connection_a,
            startup_safety=lambda: startup_safety_from_reconciliation_result(
                make_result(ReconciliationStatus.CLEAN)
            ),
            market_data=FakeMarketData(frames),
            execution=execution_a,
        )
        runtime_a.start()
        report_a = runtime_a.run_cycle()
        runtime_a.stop()
    assert report_a.results[0].processed
    assert report_a.results[0].bar_timestamp == T_BAR

    # ---- the claim outlived the process ----
    with connect(database_path) as observer:
        checkpoint = get_runtime_checkpoint(observer, BTC)
        assert checkpoint is not None
        assert checkpoint.last_processed_bar_timestamp == T_BAR

    # ---- process B, same database, same newest completed bar ----
    execution_b = FakeExecution()
    with connect(database_path) as connection_b:
        runtime_b = build(
            connection_b,
            startup_safety=lambda: startup_safety_from_reconciliation_result(
                make_result(ReconciliationStatus.CLEAN)
            ),
            market_data=FakeMarketData(frames),
            execution=execution_b,
        )
        runtime_b.start()
        report_b = runtime_b.run_cycle()
        runtime_b.stop()

    btc_result = next(entry for entry in report_b.results if entry.symbol == BTC)
    assert btc_result.skipped_reason == "ALREADY_PROCESSED"
    assert not btc_result.processed
    assert btc_result.signal is None
    assert BTC not in execution_b.symbols


def test_checkpoint_is_durable_before_order_submission(
    database_path: Path, paper_gate: None
) -> None:
    """NAMED REGRESSION 3. The claim is committed before the broker is called.

    The assertion runs *inside* the mocked submission, on a second independent
    SQLite connection. If the checkpoint were written after submission - or
    written inside an uncommitted transaction - the second connection would see
    nothing and this fails.
    """
    seen_during_submission: dict[str, object] = {}

    class ObservingExecution(FakeExecution):
        def execute(self, connection, **kwargs):  # noqa: ANN001, ANN003
            with connect(database_path) as independent:
                checkpoint = get_runtime_checkpoint(independent, str(kwargs["symbol"]))
                seen_during_submission[str(kwargs["symbol"])] = (
                    None if checkpoint is None else checkpoint.last_processed_bar_timestamp
                )
            return super().execute(connection, **kwargs)

    execution = ObservingExecution()
    with connect(database_path) as connection:
        runtime = build(
            connection,
            startup_safety=lambda: startup_safety_from_reconciliation_result(
                make_result(ReconciliationStatus.CLEAN)
            ),
            execution=execution,
        )
        runtime.start()
        runtime.run_cycle()
        runtime.stop()

    assert execution.symbols == [BTC, ETH], "both symbols should have reached the broker"
    assert seen_during_submission == {BTC: T_BAR, ETH: T_BAR}


def test_runtime_runs_reconciliation_again_after_restart(database_path: Path) -> None:
    """NAMED REGRESSION 4. One reconciliation per process start, never cached across.

    A green answer describes the world at the moment it was produced. A second
    process inherits the database, not the conclusion: it asks again.
    """
    check = RecordingCheck(
        startup_safety_from_reconciliation_result(make_result(ReconciliationStatus.CLEAN))
    )

    with connect(database_path) as connection_a:
        runtime_a = build(connection_a, startup_safety=check)
        runtime_a.start()
        runtime_a.stop()
    assert check.calls == 1

    with connect(database_path) as connection_b:
        runtime_b = build(connection_b, startup_safety=check)
        runtime_b.start()
        runtime_b.stop()
    assert check.calls == 2, "the second process must reconcile for itself"


# ==========================================================================
# 1-9. The startup gate
# ==========================================================================


def test_runtime_startup_calls_the_real_reconciliation_seam(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1. The production check is `reconcile_paper_state`, not a placeholder."""
    calls: list[object] = []

    def spy(passed_connection, **kwargs):  # noqa: ANN001, ANN003
        calls.append((passed_connection, kwargs))
        return make_result(ReconciliationStatus.CLEAN)

    monkeypatch.setattr("autotrader.runtime.safety.reconcile_paper_state", spy)

    runtime = build(connection, startup_safety=startup_safety_from_reconciliation(connection))
    runtime.start()

    assert len(calls) == 1
    assert calls[0][0] is connection
    assert runtime.heartbeat.startup_safety_code == STARTUP_SAFETY_SAFE
    assert runtime.heartbeat.reconciliation_status == "CLEAN"
    # Not a dry run: startup is exactly when a repairable difference should be
    # repaired, or the runner blocks on something it could have fixed.
    assert calls[0][1].get("dry_run") in (None, False)


@pytest.mark.parametrize("status", [ReconciliationStatus.CLEAN, ReconciliationStatus.REPAIRED])
def test_a_green_reconciliation_enables_startup_safety(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, status: ReconciliationStatus
) -> None:
    """2 and 3. CLEAN and REPAIRED both open the gate.

    REPAIRED especially: a stale local snapshot that was rewritten *from the
    broker* is now correct, and a runner permanently blocked by a difference it
    already resolved would be a bug, not caution.
    """
    monkeypatch.setenv(PAPER_TRADING_ENABLED_ENV, PAPER_TRADING_ENABLED_VALUE)
    issues = (repaired_issue(),) if status is ReconciliationStatus.REPAIRED else ()
    safety = startup_safety_from_reconciliation_result(make_result(status, issues=issues))

    assert safety.safe_to_trade
    assert safety.code == STARTUP_SAFETY_SAFE
    assert safety.reconciliation_status == status.value

    execution = FakeExecution()
    runtime = build(connection, startup_safety=lambda: safety, execution=execution)
    runtime.start()
    runtime.run_cycle()

    assert runtime.authorization.enabled
    assert runtime.authorization.reason is None
    assert execution.symbols == [BTC, ETH]


@pytest.mark.parametrize(
    ("status", "issues"),
    [
        (ReconciliationStatus.UNRESOLVED, (blocking_issue(),)),
        (ReconciliationStatus.FAILED, (failed_issue(),)),
    ],
)
def test_a_non_green_reconciliation_disables_trading(
    connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    status: ReconciliationStatus,
    issues: tuple[ReconciliationIssue, ...],
) -> None:
    """4, 5 and 6. UNRESOLVED and FAILED both close the gate and submit nothing."""
    monkeypatch.setenv(PAPER_TRADING_ENABLED_ENV, PAPER_TRADING_ENABLED_VALUE)
    safety = startup_safety_from_reconciliation_result(make_result(status, issues=issues))

    assert not safety.safe_to_trade
    assert safety.code == STARTUP_SAFETY_UNSAFE
    assert RECONCILIATION_NOT_SAFE_BANNER in safety.message
    assert status.value in safety.message

    execution = FakeExecution()
    runtime = build(connection, startup_safety=lambda: safety, execution=execution)
    runtime.start()
    runtime.run_cycle()

    assert len(execution.calls) == 0
    assert runtime.heartbeat.orders_submitted == 0
    assert runtime.authorization.reason == "STARTUP_SAFETY_UNSAFE"
    assert runtime.heartbeat.reconciliation_status == status.value


def test_the_paper_env_gate_cannot_bypass_a_failed_reconciliation(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """7. Setting the environment variable does not make an unsafe start safe."""
    monkeypatch.setenv(PAPER_TRADING_ENABLED_ENV, PAPER_TRADING_ENABLED_VALUE)
    execution = FakeExecution()
    runtime = build(
        connection,
        startup_safety=lambda: startup_safety_from_reconciliation_result(
            make_result(ReconciliationStatus.FAILED, issues=(failed_issue(),))
        ),
        execution=execution,
        confirmation=RUNTIME_CONFIRMATION_TOKEN,
    )
    runtime.start()
    runtime.run_cycle()

    assert len(execution.calls) == 0
    assert runtime.authorization.reason == "STARTUP_SAFETY_UNSAFE"


def test_the_runtime_confirmation_cannot_bypass_a_failed_reconciliation(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """8. Typing PAPER does not make an unsafe start safe either.

    The four gates are independent conditions, not alternatives. There is no
    combination of environment and command line that reaches a broker while
    reconciliation says no.
    """
    monkeypatch.setenv(PAPER_TRADING_ENABLED_ENV, PAPER_TRADING_ENABLED_VALUE)
    execution = FakeExecution()
    runtime = build(
        connection,
        startup_safety=lambda: startup_safety_from_reconciliation_result(
            make_result(ReconciliationStatus.UNRESOLVED, issues=(blocking_issue(),))
        ),
        execution=execution,
        confirmation=RUNTIME_CONFIRMATION_TOKEN,
    )
    runtime.start()
    runtime.run_cycle()

    assert runtime.authorization.disabled
    assert len(execution.calls) == 0


def test_both_paper_gates_and_a_clean_reconciliation_permit_paper_execution(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """9. All three conditions together, and only then, open the path."""
    monkeypatch.setenv(PAPER_TRADING_ENABLED_ENV, PAPER_TRADING_ENABLED_VALUE)
    execution = FakeExecution()
    runtime = build(
        connection,
        startup_safety=lambda: startup_safety_from_reconciliation_result(
            make_result(ReconciliationStatus.CLEAN)
        ),
        execution=execution,
        confirmation=RUNTIME_CONFIRMATION_TOKEN,
    )
    runtime.start()
    runtime.run_cycle()

    assert runtime.authorization.enabled
    assert execution.symbols == [BTC, ETH]
    assert runtime.heartbeat.orders_submitted == 2


@pytest.mark.parametrize(
    "status",
    [
        ReconciliationStatus.CLEAN,
        ReconciliationStatus.REPAIRED,
        ReconciliationStatus.UNRESOLVED,
        ReconciliationStatus.FAILED,
    ],
)
def test_observe_only_submits_zero_orders_whatever_reconciliation_said(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, status: ReconciliationStatus
) -> None:
    """10. Observe-only is incapable of submission, green result or not.

    It still *runs* reconciliation - startup-safety visibility is useful even
    when nothing could be sent - but the execution path is not constructed, so
    submission is unexpressible rather than merely refused.
    """
    monkeypatch.setenv(PAPER_TRADING_ENABLED_ENV, PAPER_TRADING_ENABLED_VALUE)
    check = RecordingCheck(startup_safety_from_reconciliation_result(make_result(status)))
    execution = FakeExecution()
    runtime = build(
        connection,
        startup_safety=check,
        execution=execution,
        observe_only=True,
        confirmation=RUNTIME_CONFIRMATION_TOKEN,
    )
    runtime.start()
    report = runtime.run_cycle()

    assert check.calls == 1, "observe-only still reports startup safety"
    assert runtime.heartbeat.reconciliation_status == status.value
    assert len(execution.calls) == 0
    assert runtime.heartbeat.orders_submitted == 0
    assert runtime.authorization.reason == "OBSERVE_ONLY"
    assert all(entry.skipped_reason == "OBSERVE_ONLY" for entry in report.results)


# ==========================================================================
# 11-12. The standalone command and the process lock
# ==========================================================================


def test_the_standalone_reconcile_cli_remains_functional(
    database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """11. `reconcile` still works on its own, for diagnostics and manual repair.

    Integration made the runtime reconcile automatically. It did not remove the
    command an operator runs by hand to see what a pass would find.
    """
    client = FakeTradingClient(account=make_account(), positions=[])
    monkeypatch.setattr(
        "autotrader.reconciliation.engine.create_paper_trading_client", lambda: client
    )

    result = runner.invoke(app, ["reconcile", "--db", str(database_path)])

    assert result.exit_code == 0, result.output
    assert "CLEAN" in result.output
    assert "Safe To Trade" in result.output
    assert client.submit_calls == []

    with connect(database_path) as connection:
        assert len(list_reconciliation_runs(connection)) == 1


def test_the_runtime_process_lock_still_blocks_a_second_instance(database_path: Path) -> None:
    """12. The OS lock and the durable checkpoint solve different problems.

    Both are kept. The lock stops two runners existing; the checkpoint stops
    one runner - or its replacement - acting twice on one bar.
    """
    first = RuntimeLock(lock_path_for(database_path))
    first.acquire()
    try:
        with pytest.raises(RuntimeLockError):
            RuntimeLock(lock_path_for(database_path)).acquire()
    finally:
        first.release()

    # Released, so the next runner may start.
    second = RuntimeLock(lock_path_for(database_path))
    second.acquire()
    assert second.held
    second.release()


# ==========================================================================
# 13-18. The durable checkpoint
# ==========================================================================


@pytest.mark.parametrize("symbol", [BTC, ETH])
def test_a_persistent_checkpoint_survives_a_restart(database_path: Path, symbol: str) -> None:
    """13 and 14. Both symbols get their own durable claim, kept independently."""
    with connect(database_path) as writer:
        SqliteCheckpoint(writer).mark_processed(symbol, T_BAR)

    with connect(database_path) as reader:
        checkpoint = SqliteCheckpoint(reader)
        assert checkpoint.last_processed(symbol) == T_BAR
        other = ETH if symbol == BTC else BTC
        assert checkpoint.last_processed(other) is None, "symbols do not share a claim"


@pytest.mark.parametrize("symbol", [BTC, ETH])
def test_the_same_completed_bar_after_a_restart_produces_no_second_action(
    database_path: Path, symbol: str, paper_gate: None
) -> None:
    """15 and 16. A claimed bar produces no signal, no risk call, no order.

    The claim is seeded as a previous process would have left it, then a fresh
    runtime is pointed at the same newest completed bar.
    """
    with connect(database_path) as seeder:
        SqliteCheckpoint(seeder).mark_processed(symbol, T_BAR)

    frames = {
        BTC: make_bars(BTC, closes=crossover_closes()),
        ETH: make_bars(ETH, closes=crossover_closes()),
    }
    execution = FakeExecution()
    with connect(database_path) as connection:
        runtime = build(
            connection,
            startup_safety=lambda: startup_safety_from_reconciliation_result(
                make_result(ReconciliationStatus.CLEAN)
            ),
            market_data=FakeMarketData(frames),
            execution=execution,
        )
        runtime.start()
        report = runtime.run_cycle()

    entry = next(item for item in report.results if item.symbol == symbol)
    assert entry.skipped_reason == "ALREADY_PROCESSED"
    assert entry.signal is None
    assert symbol not in execution.symbols
    # The unclaimed symbol is unaffected: one claim blocks one symbol.
    other = ETH if symbol == BTC else BTC
    assert other in execution.symbols


def test_a_crash_after_the_claim_but_before_the_intent_does_not_replay_the_bar(
    database_path: Path, paper_gate: None
) -> None:
    """18. The claim survives a hard failure mid-cycle, and the bar is not retried.

    The execution boundary raises after the claim is committed, which is the
    shape of a process dying between claiming a bar and creating its order
    intent. On restart, the bar is already claimed: it is skipped, not redone.

    This is the safety preference made concrete - that BTC trade is now missed,
    permanently, and that is the outcome this system prefers to a duplicate.
    """
    frames = {BTC: make_bars(BTC, closes=crossover_closes()), ETH: make_bars(ETH)}

    crashing = FakeExecution(results=[RuntimeError("process died mid-cycle")])
    with connect(database_path) as connection_a:
        runtime_a = build(
            connection_a,
            startup_safety=lambda: startup_safety_from_reconciliation_result(
                make_result(ReconciliationStatus.CLEAN)
            ),
            market_data=FakeMarketData(frames),
            execution=crashing,
        )
        runtime_a.start()
        report_a = runtime_a.run_cycle()
    assert report_a.severity is not None, "the cycle failed, as the test intends"

    with connect(database_path) as observer:
        checkpoint = get_runtime_checkpoint(observer, BTC)
        assert checkpoint is not None
        assert checkpoint.last_processed_bar_timestamp == T_BAR

    execution_b = FakeExecution()
    with connect(database_path) as connection_b:
        runtime_b = build(
            connection_b,
            startup_safety=lambda: startup_safety_from_reconciliation_result(
                make_result(ReconciliationStatus.CLEAN)
            ),
            market_data=FakeMarketData(frames),
            execution=execution_b,
        )
        runtime_b.start()
        report_b = runtime_b.run_cycle()

    btc = next(entry for entry in report_b.results if entry.symbol == BTC)
    assert btc.skipped_reason == "ALREADY_PROCESSED"
    assert BTC not in execution_b.symbols


def test_a_checkpoint_never_moves_backwards(database_path: Path) -> None:
    """An older bar cannot re-open a claim, even written directly to storage."""
    older = T_BAR - timedelta(minutes=15)
    with connect(database_path) as connection:
        checkpoint = SqliteCheckpoint(connection)
        checkpoint.mark_processed(BTC, T_BAR)
        checkpoint.mark_processed(BTC, older)
        assert checkpoint.last_processed(BTC) == T_BAR


# ==========================================================================
# 19-23. Reconciliation behaviour, reached through the integrated startup
# ==========================================================================


def test_a_created_intent_recovery_never_resubmits_a_stale_bar(
    connection: sqlite3.Connection, paper_gate: None
) -> None:
    """19. A pre-crash CREATED intent is closed off at startup, never executed."""
    make_reconcilable_intent(
        connection, client_order_id="autotrader-stale-created", status=INTENT_STATUS_CREATED
    )
    client = FakeTradingClient(account=make_account(), positions=[])

    result = reconcile_paper_state(
        connection, trading_client=client, confirmations=2, recheck_delay_seconds=0, sleep=no_sleep
    )
    safety = startup_safety_from_reconciliation_result(result)

    assert client.submit_calls == []
    intent = next(
        item
        for item in [get_order_intent(connection, 1)]
        if item is not None and item.client_order_id == "autotrader-stale-created"
    )
    assert intent.status == INTENT_STATUS_CONFIRMED_NOT_SUBMITTED
    assert safety.safe_to_trade, "a settled stale intent must not block the runner forever"

    # And the runtime that starts on that answer sends nothing for the old bar.
    # Flat bars, so the newest completed bar carries no crossover: the only
    # thing that could produce an order here is a replay of the pre-crash
    # signal, and nothing replays it. The runtime is fully authorized, so a
    # zero count is a decision rather than a closed gate.
    execution = FakeExecution()
    runtime = build(
        connection,
        startup_safety=lambda: safety,
        market_data=FakeMarketData({BTC: make_bars(BTC), ETH: make_bars(ETH)}),
        execution=execution,
    )
    runtime.start()
    assert runtime.authorization.enabled, "the gate is open, so nothing below is gate-blocked"
    report = runtime.run_cycle()

    assert [call["symbol"] for call in execution.calls] == []
    assert all(entry.signal is None for entry in report.results)
    assert client.submit_calls == []


def test_unknown_recovery_never_creates_a_replacement_order(
    connection: sqlite3.Connection,
) -> None:
    """20. An UNKNOWN intent whose order exists is adopted, not re-sent."""
    intent_id = make_reconcilable_intent(
        connection, client_order_id="autotrader-unknown-1", status=INTENT_STATUS_UNKNOWN
    )
    client = FakeTradingClient(
        account=make_account(),
        positions=[],
        orders={
            "autotrader-unknown-1": make_order(
                client_order_id="autotrader-unknown-1", status=OrderStatus.ACCEPTED
            )
        },
    )

    result = reconcile_paper_state(connection, trading_client=client, sleep=no_sleep)

    assert client.submit_calls == []
    assert result.safe_to_trade
    intent = get_order_intent(connection, intent_id)
    assert intent is not None
    assert intent.status == INTENT_STATUS_SUBMITTED
    snapshot = get_broker_order_by_intent(connection, intent_id)
    assert snapshot is not None
    assert snapshot.client_order_id == "autotrader-unknown-1"
    assert snapshot.broker_order_id == BROKER_ORDER_UUID


def test_startup_reconciliation_repairs_an_accepted_order_to_its_terminal_state(
    connection: sqlite3.Connection,
) -> None:
    """21. accepted -> filled is read from the broker and written down locally."""
    intent_id = make_reconcilable_intent(
        connection, client_order_id="autotrader-accepted-1", status=INTENT_STATUS_SUBMITTED
    )
    store_snapshot(connection, intent_id, client_order_id="autotrader-accepted-1")
    filled_at = T_NOW
    client = FakeTradingClient(
        account=make_account(),
        positions=[make_position(BTC, qty="0.001")],
        orders={
            "autotrader-accepted-1": make_order(
                client_order_id="autotrader-accepted-1",
                status=OrderStatus.FILLED,
                filled_qty="0.001",
                filled_avg_price="100000",
                filled_at=filled_at,
            )
        },
    )

    result = reconcile_paper_state(connection, trading_client=client, sleep=no_sleep)
    safety = startup_safety_from_reconciliation_result(result)

    assert result.status is ReconciliationStatus.REPAIRED
    assert safety.safe_to_trade, "REPAIRED is safe; a resolved difference must not block"
    snapshot = get_broker_order_by_intent(connection, intent_id)
    assert snapshot is not None
    assert snapshot.status == "filled"
    assert client.submit_calls == []


def test_the_broker_position_remains_authoritative(connection: sqlite3.Connection) -> None:
    """22. Where local and broker disagree, the local snapshot is rewritten."""
    upsert_position(connection, symbol=BTC, quantity=Decimal("9.99"), updated_at=T_NOW)
    client = FakeTradingClient(account=make_account(), positions=[make_position(BTC, qty="0.0005")])

    result = reconcile_paper_state(connection, trading_client=client, sleep=no_sleep)

    assert result.status is ReconciliationStatus.REPAIRED
    stored = get_position(connection, BTC)
    assert stored is not None
    assert stored.quantity == Decimal("0.0005"), "broker truth wins, not the local snapshot"
    assert client.submit_calls == [], "a mismatch is repaired in the database, never by trading"


def test_submitted_is_not_filled_after_integration(connection: sqlite3.Connection) -> None:
    """23. An accepted order still implies no position, and a partial stays partial."""
    intent_id = make_reconcilable_intent(
        connection, client_order_id="autotrader-partial-1", status=INTENT_STATUS_SUBMITTED
    )
    store_snapshot(connection, intent_id, client_order_id="autotrader-partial-1")
    client = FakeTradingClient(
        account=make_account(),
        positions=[],
        orders={
            "autotrader-partial-1": make_order(
                client_order_id="autotrader-partial-1",
                status=OrderStatus.PARTIALLY_FILLED,
                qty="0.001",
                filled_qty="0.0004",
                filled_avg_price="100000",
            )
        },
    )

    reconcile_paper_state(connection, trading_client=client, sleep=no_sleep)

    snapshot = get_broker_order_by_intent(connection, intent_id)
    assert snapshot is not None
    assert snapshot.status == "partially_filled"
    assert snapshot.filled_quantity == Decimal("0.0004")
    assert snapshot.quantity == Decimal("0.001")
    # The broker holds no position, so neither does local state - a submitted
    # order is not a position, and reconciliation does not invent one from an
    # order it just saw partially fill.
    stored = get_position(connection, BTC)
    assert stored is None or stored.quantity == Decimal(0)


# ==========================================================================
# 24-28. Runtime invariants that integration must not have broken
# ==========================================================================


def test_btc_then_eth_ordering_is_preserved(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """24. BTC/USD is finished before ETH/USD is looked at, and nothing overlaps.

    `FakeExecution` raises if two submissions ever overlap, so this also proves
    integration introduced no concurrency.
    """
    monkeypatch.setenv(PAPER_TRADING_ENABLED_ENV, PAPER_TRADING_ENABLED_VALUE)
    execution = FakeExecution()
    runtime = build(
        connection,
        startup_safety=lambda: startup_safety_from_reconciliation_result(
            make_result(ReconciliationStatus.CLEAN)
        ),
        execution=execution,
    )
    runtime.start()
    runtime.run_cycle()

    assert PROCESSING_ORDER == (BTC, ETH)
    assert execution.symbols == [BTC, ETH]


def test_an_incomplete_candle_is_still_never_processed(connection: sqlite3.Connection) -> None:
    """25. The completed-bar rule survives integration, and nothing is claimed."""
    # `now` is one second after the boundary the newest bar *starts* on, so
    # that bar's interval has not elapsed.
    clock = FakeClock(T_BAR + timedelta(seconds=1))
    frames = {BTC: make_bars(BTC, closes=crossover_closes()), ETH: make_bars(ETH)}
    execution = FakeExecution()
    runtime = build(
        connection,
        startup_safety=lambda: startup_safety_from_reconciliation_result(
            make_result(ReconciliationStatus.CLEAN)
        ),
        market_data=FakeMarketData(frames),
        execution=execution,
        clock=clock,
    )
    runtime.start()
    report = runtime.run_cycle()

    btc = next(entry for entry in report.results if entry.symbol == BTC)
    assert btc.bar_timestamp != T_BAR, "the in-progress candle was not treated as complete"
    assert execution.symbols == []
    # An unfinished bar is not claimed either: it has to remain processable
    # once it actually completes, and a claim now would lose it forever.
    claimed = get_runtime_checkpoint(connection, BTC)
    assert claimed is None or claimed.last_processed_bar_timestamp < T_BAR


def test_saturday_and_sunday_remain_normal_crypto_runtime_days(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """26. The weekend is an ordinary trading day, integration included."""
    monkeypatch.setenv(PAPER_TRADING_ENABLED_ENV, PAPER_TRADING_ENABLED_VALUE)
    assert T_BAR_SATURDAY.weekday() == 5
    frames = {
        BTC: make_bars(BTC, last_bar_start=T_BAR_SATURDAY, closes=crossover_closes()),
        ETH: make_bars(ETH, last_bar_start=T_BAR_SATURDAY, closes=crossover_closes()),
    }
    execution = FakeExecution()
    runtime = build(
        connection,
        startup_safety=lambda: startup_safety_from_reconciliation_result(
            make_result(ReconciliationStatus.CLEAN)
        ),
        market_data=FakeMarketData(frames),
        execution=execution,
        clock=FakeClock(T_NOW_SATURDAY),
    )
    runtime.start()
    report = runtime.run_cycle()

    assert report.succeeded
    assert execution.symbols == [BTC, ETH]
    assert get_runtime_checkpoint(connection, BTC).last_processed_bar_timestamp == T_BAR_SATURDAY

    # And Sunday, for the same reason.
    sunday_bar = T_BAR_SATURDAY + timedelta(days=1)
    assert sunday_bar.weekday() == 6
    sunday_frames = {
        BTC: make_bars(BTC, last_bar_start=sunday_bar, closes=crossover_closes()),
        ETH: make_bars(ETH, last_bar_start=sunday_bar, closes=crossover_closes()),
    }
    runtime._market_data = FakeMarketData(sunday_frames)  # noqa: SLF001 - boundary swap
    sunday_report = runtime.run_cycle(now=T_NOW_SATURDAY + timedelta(days=1))
    assert sunday_report.succeeded
    assert get_runtime_checkpoint(connection, BTC).last_processed_bar_timestamp == sunday_bar


def test_unknown_during_a_running_process_pauses_future_submissions(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """27. An ambiguous outcome stops this process trading; nothing resolves it inline.

    ETH is never reached on the cycle BTC went ambiguous on, and no later cycle
    submits either. Recovery requires a new process, which reconciles first.
    """
    monkeypatch.setenv(PAPER_TRADING_ENABLED_ENV, PAPER_TRADING_ENABLED_VALUE)
    execution = FakeExecution(results=[AmbiguousSubmissionError("timed out mid-submission")])
    runtime = build(
        connection,
        startup_safety=lambda: startup_safety_from_reconciliation_result(
            make_result(ReconciliationStatus.CLEAN)
        ),
        execution=execution,
    )
    runtime.start()
    report = runtime.run_cycle()

    assert report.severity is not None
    assert runtime.state is RuntimeState.TRADING_PAUSED
    assert execution.symbols == [BTC], "ETH was not submitted after the ambiguity"
    assert len(execution.calls) == 1, "no retry, no replacement, no second attempt"
    allowed, reason = runtime._may_submit()  # noqa: SLF001 - the gate under test
    assert not allowed
    assert reason == "TRADING_PAUSED"
    assert any(
        event.event_type == "RUNTIME_TRADING_PAUSED" for event in list_system_events(connection)
    )


def test_a_process_restart_requires_reconciliation_again(database_path: Path) -> None:
    """28. A paused process does not hand its permission to its successor.

    The next process starts from scratch: it asks the startup question again,
    and this time the answer is no.
    """
    first = RecordingCheck(
        startup_safety_from_reconciliation_result(make_result(ReconciliationStatus.CLEAN))
    )
    with connect(database_path) as connection_a:
        runtime_a = build(connection_a, startup_safety=first)
        runtime_a.start()
        assert runtime_a.authorization.reason == "PAPER_ENV_GATE_DISABLED"
        runtime_a.stop()

    second = RecordingCheck(
        startup_safety_from_reconciliation_result(
            make_result(ReconciliationStatus.UNRESOLVED, issues=(blocking_issue(),))
        )
    )
    with connect(database_path) as connection_b:
        runtime_b = build(connection_b, startup_safety=second)
        runtime_b.start()
        assert second.calls == 1
        assert runtime_b.heartbeat.reconciliation_status == "UNRESOLVED"
        assert runtime_b.authorization.disabled
        runtime_b.stop()


# ==========================================================================
# 29-30. Scope
# ==========================================================================


def test_the_integrated_system_has_no_live_path() -> None:
    """29. No integrated module gained a live mode, a live flag, or a live host."""
    import autotrader.runtime.checkpoint as integrated_checkpoint
    import autotrader.runtime.safety as integrated_safety
    from test_runtime import code_without_prose

    sources = "\n".join(
        code_without_prose(Path(module.__file__).read_text())
        for module in (integrated_safety, integrated_checkpoint)
    )
    for forbidden in (
        "paper=False",
        "paper = False",
        "--live",
        "TRADING_LIVE",
        "ALPACA_LIVE",
        "api.alpaca.markets",
        "live_trading",
    ):
        assert forbidden not in sources, forbidden

    names = {command.name for command in app.registered_commands}
    assert "live-run" not in names
    assert "live" not in names


def test_the_integrated_system_has_no_stock_path() -> None:
    """30. The universe is exactly the two crypto pairs, everywhere it is named."""
    import autotrader.runtime.checkpoint as integrated_checkpoint
    import autotrader.runtime.safety as integrated_safety
    from test_runtime import code_without_prose

    sources = "\n".join(
        code_without_prose(Path(module.__file__).read_text())
        for module in (integrated_safety, integrated_checkpoint)
    )
    for forbidden in (
        "SPY",
        "QQQ",
        "AAPL",
        "StockHistoricalDataClient",
        "StockLatestTradeRequest",
        "StockBarsRequest",
        "IEX",
        "NYSE",
        "get_clock",
    ):
        assert forbidden not in sources, forbidden

    assert PROCESSING_ORDER == ("BTC/USD", "ETH/USD")
    names = {command.name for command in app.registered_commands}
    assert "stock-run" not in names


# ==========================================================================
# The integrated CLI
# ==========================================================================


def test_crypto_run_reconciles_at_startup_and_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`crypto-run` runs the Phase 8 pass itself - no separate command first."""
    database = tmp_path / "state.db"
    frames = {BTC: make_bars(BTC), ETH: make_bars(ETH)}
    monkeypatch.setattr(
        "autotrader.runtime.market_data.AlpacaCryptoBars.recent_bars",
        lambda self, symbol, *, now, lookback_bars: frames[symbol],
    )
    client = FakeTradingClient(account=make_account(), positions=[])
    monkeypatch.setattr(
        "autotrader.reconciliation.engine.create_paper_trading_client", lambda: client
    )

    result = runner.invoke(app, ["crypto-run", "--once", "--observe-only", "--db", str(database)])

    assert result.exit_code == 0, result.output
    assert "Reconciliation:" in result.output
    assert "CLEAN" in result.output
    assert "OBSERVATION ONLY - NO ORDER WILL BE SUBMITTED" in result.output
    assert client.submit_calls == []
    with connect(database) as connection:
        assert len(list_reconciliation_runs(connection)) == 1


def test_crypto_run_shouts_when_reconciliation_is_not_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator must be able to see "not safe" without reading source."""
    database = tmp_path / "state.db"
    monkeypatch.setenv(PAPER_TRADING_ENABLED_ENV, PAPER_TRADING_ENABLED_VALUE)
    frames = {BTC: make_bars(BTC), ETH: make_bars(ETH)}
    monkeypatch.setattr(
        "autotrader.runtime.market_data.AlpacaCryptoBars.recent_bars",
        lambda self, symbol, *, now, lookback_bars: frames[symbol],
    )
    monkeypatch.setattr(
        "autotrader.runtime.safety.reconcile_paper_state",
        lambda connection, **kwargs: make_result(
            ReconciliationStatus.UNRESOLVED, issues=(blocking_issue(),)
        ),
    )
    monkeypatch.setattr(
        "autotrader.runtime.execution.PaperExecutionGateway.execute",
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
    assert RECONCILIATION_NOT_SAFE_BANNER in result.output
    assert "UNRESOLVED" in result.output
    assert "STARTUP_SAFETY_UNSAFE" in result.output
