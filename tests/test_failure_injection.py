"""Failure injection: what the crypto system does when things break mid-trade.

Every other test file asks "does this work?". This one asks "what happens when
it stops working at the worst possible instant?" - and the answer that has to
come back, every time, is some form of *nothing*.

**The preference being defended is one-sided and explicit.**

    miss a trade rather than duplicate a trade

A missed trade costs an opportunity. A duplicated trade costs a position
nobody decided to take, and no reconciliation pass can un-place an order. So
every ambiguity below resolves towards not acting, and several of these tests
assert that a *legitimate* trade was lost - that is the correct outcome, not a
regression to be tuned away later.

**The seven crash windows.** A process can die between any two statements. The
windows that actually matter are the ones that straddle a durable write or a
broker call, and each has its own section below:

===========  =======================================  ==========================
Window       Death happens                            Required outcome
===========  =======================================  ==========================
1            after the bars arrive, before the claim  bar may be processed later
2            after the claim, before the signal       bar is skipped forever
3            after the intent commits, before submit  intent closed off, unsent
4            during submit, response lost             UNKNOWN, never retried
5            after the broker accepts, before storing reconciliation repairs it
6            after a partial fill                     partial stays partial
7            after death, the broker fills            restart discovers the fill
===========  =======================================  ==========================

**Everything here is offline.** The broker, the market-data provider, the clock
and the sleep are all injected fakes; the only real I/O is SQLite in `tmp_path`.
No test opens a socket, submits a paper order, cancels anything, or waits on a
real fifteen-minute boundary. The credentials and the submission gate are
cleared by an autouse fixture, so a developer's exported environment can never
make a "nothing was submitted" assertion pass for the wrong reason.

**Failure injection lives here, not in production.** There is no chaos mode, no
fault-injection endpoint, and no environment variable that makes the system
misbehave on purpose. Everything below is injected through the dependency seams
the system already has - the trading client, the data client, the market-data
source, the clock, and a second connection holding a real SQLite write lock.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alpaca.trading.enums import OrderStatus
from typer.testing import CliRunner

from autotrader.cli import app
from autotrader.data.historical import HistoricalDataError
from autotrader.execution.models import ExecutionError
from autotrader.execution.paper import (
    PAPER_TRADING_ENABLED_ENV,
    PAPER_TRADING_ENABLED_VALUE,
    AmbiguousSubmissionError,
    BrokerRejectedOrderError,
    DuplicatePreflightUnavailableError,
    PaperExecutionResult,
    execute_paper_order,
)
from autotrader.reconciliation import ReconciliationStatus, reconcile_paper_state
from autotrader.runtime import execution as runtime_execution
from autotrader.runtime import market_data as runtime_market_data
from autotrader.runtime.checkpoint import SqliteCheckpoint
from autotrader.runtime.lock import RuntimeLock, lock_path_for
from autotrader.runtime.monitoring import RuntimeState
from autotrader.runtime.runner import (
    RUNTIME_CONFIRMATION_TOKEN,
    CryptoRuntime,
    CycleSeverity,
    RuntimeConfig,
    classify,
)
from autotrader.runtime.safety import STARTUP_SAFETY_SAFE, StartupSafetyResult
from autotrader.state.sqlite import (
    INTENT_STATUS_CONFIRMED_NOT_SUBMITTED,
    INTENT_STATUS_CREATED,
    INTENT_STATUS_SUBMITTED,
    INTENT_STATUS_SUBMITTING,
    INTENT_STATUS_UNKNOWN,
    connect,
    get_broker_order_by_intent,
    get_position,
    get_runtime_checkpoint,
    initialize_database,
    list_broker_orders,
    list_order_intents,
    list_reconciliation_runs,
    list_signals,
    list_system_events,
    record_order_intent,
    upsert_broker_order,
    upsert_position,
)
from test_execution_paper import FakeDataClient, api_error
from test_execution_paper import FakeTradingClient as FakeBrokerClient
from test_execution_paper import make_order as make_broker_order
from test_reconciliation import BROKER_ORDER_UUID, make_position, no_sleep
from test_reconciliation import FakeTradingClient as FakeReconClient
from test_reconciliation import make_order as make_recon_order
from test_runtime import (
    BTC,
    ETH,
    T_BAR,
    FakeClock,
    FakeExecution,
    FakeMarketData,
    crossover_closes,
    make_bars,
)

runner = CliRunner()

#: The instant every persisted row in this file is stamped with, so a repair
#: and the thing it repaired can be compared without clock skew entering it.
T0 = datetime(2026, 3, 4, 9, 15, tzinfo=UTC)

#: The HTTP statuses that leave a submission genuinely ambiguous: the request
#: may or may not have reached the matching engine. Each one must produce
#: UNKNOWN, never a rejection and never a retry.
AMBIGUOUS_STATUSES = (408, 429, 500, 502, 503, 504)

#: Statuses the broker uses to say "no, and it never existed". These are the
#: only ones that may be read as a definitive refusal.
DEFINITE_REJECTION_STATUSES = (400, 403, 404, 422)


# ==========================================================================
# Fixtures and fakes
# ==========================================================================


@pytest.fixture(autouse=True)
def _offline_and_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """No credentials and a shut gate, whatever the developer's shell exported.

    Autouse because both would otherwise decide the outcome of a "nothing was
    submitted" assertion for reasons unrelated to the code under test, and
    because a stray credential would let a startup reconciliation open a real
    socket out of a test that is supposed to be offline.
    """
    monkeypatch.delenv(PAPER_TRADING_ENABLED_ENV, raising=False)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)


@pytest.fixture
def paper_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open the environment gate. Requested only by tests that want an order."""
    monkeypatch.setenv(PAPER_TRADING_ENABLED_ENV, PAPER_TRADING_ENABLED_VALUE)


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return initialize_database(tmp_path / "state.db")


@pytest.fixture
def connection(database_path: Path):
    with connect(database_path) as open_connection:
        yield open_connection


class WriteLockHolder:
    """A second connection holding a real SQLite write lock on one database.

    Not a mock of a lock: it opens the file and runs `BEGIN IMMEDIATE`, which is
    exactly what a concurrent writer - another process, a backup, a crashed
    transaction - leaves behind. Writes from the connection under test then fail
    the way they fail in production, with a real `database is locked`.

    The busy timeout is dropped to a few milliseconds so a locked-out write
    fails now rather than after the five-second production default.
    """

    def __init__(self, database: Path, *, busy_timeout_ms: int = 20) -> None:
        self.database = database
        self.busy_timeout_ms = busy_timeout_ms
        self._holder: sqlite3.Connection | None = None

    def __enter__(self) -> WriteLockHolder:
        holder = sqlite3.connect(self.database, isolation_level=None, timeout=0.05)
        holder.execute("BEGIN IMMEDIATE")
        self._holder = holder
        return self

    def __exit__(self, *_: object) -> None:
        holder, self._holder = self._holder, None
        if holder is not None:
            holder.execute("ROLLBACK")
            holder.close()

    def impatient(self, connection: sqlite3.Connection) -> sqlite3.Connection:
        """Make `connection` give up on the lock immediately instead of waiting."""
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection


class RealPathGateway:
    """The runtime's execution boundary wired to the **real** C7 pipeline.

    `FakeExecution` is the right tool when a test is about the runtime's own
    decisions. It is the wrong tool here: the crash windows this file exists for
    live *inside* `execute_paper_order` - between the intent commit and the
    submit, between the submit and the snapshot - and a fake that returns a
    finished result has no such interior to crash in.

    So only the two Alpaca clients are faked, and everything between the
    runtime's `execute` call and the wire is the code that runs in production.
    """

    def __init__(
        self,
        trading_client: object,
        data_client: object | None = None,
    ) -> None:
        self.trading_client = trading_client
        self.data_client = data_client if data_client is not None else FakeDataClient()
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
        self.api_calls += 1
        return execute_paper_order(
            connection,
            symbol=symbol,
            side=side,
            requested_quantity=requested_quantity,
            trading_client=self.trading_client,  # type: ignore[arg-type]
            data_client=self.data_client,  # type: ignore[arg-type]
            strategy_run_id=strategy_run_id,
            now=now,
        )


class Crash(BaseException):
    """A simulated process death.

    Deliberately a `BaseException`. A crash is not a Python error the system
    gets to classify, log and carry on from - it is the process ceasing to
    exist - so it must not be catchable by any `except Exception` on the way
    out. Anything durable that survives it survived a real crash; anything that
    did not is gone, and the tests below say which.
    """


def safe_startup() -> StartupSafetyResult:
    """A startup gate that says trading is permitted."""
    return StartupSafetyResult(True, STARTUP_SAFETY_SAFE, "failure-injection fixture: reconciled")


def build_runtime(
    connection: sqlite3.Connection,
    *,
    market_data: object | None = None,
    execution: object | None = None,
    startup_safety=safe_startup,
    checkpoint: object | None = None,
    clock: FakeClock | None = None,
    confirmation: str | None = RUNTIME_CONFIRMATION_TOKEN,
    observe_only: bool = False,
) -> CryptoRuntime:
    """A runtime whose checkpoint is the durable production one by default.

    `checkpoint=None` means `SqliteCheckpoint` on this connection, which is
    what makes a "restart" in these tests a real restart: the claim is on disk,
    not in an object the test happens to keep alive.

    `sleep` advances the fake clock by exactly what it was asked to wait, so a
    scheduled loop reaches its next boundary instantly. Without it a test that
    calls `run_forever` waits on the real fifteen-minute clock forever.
    """
    frames = {
        BTC: make_bars(BTC, closes=crossover_closes()),
        ETH: make_bars(ETH, closes=crossover_closes()),
    }
    fake_clock = clock if clock is not None else FakeClock()

    def advance(seconds: float) -> None:
        fake_clock.advance(timedelta(seconds=seconds))

    return CryptoRuntime(
        connection,
        market_data=market_data if market_data is not None else FakeMarketData(frames),
        execution=execution if execution is not None else FakeExecution(),
        startup_safety=startup_safety,
        checkpoint=checkpoint,
        config=RuntimeConfig(observe_only=observe_only, runtime_confirmation=confirmation),
        clock=fake_clock,
        sleep=advance,
    )


def only_intent(connection: sqlite3.Connection):
    """The single order intent in the database, asserting there is exactly one."""
    intents = list_order_intents(connection)
    assert len(intents) == 1, f"expected exactly one intent, found {len(intents)}"
    return intents[0]


def unknown_events(connection: sqlite3.Connection) -> list[str]:
    """Every UNKNOWN audit message written so far."""
    return [
        event.message or ""
        for event in list_system_events(connection)
        if event.event_type.endswith("UNKNOWN")
    ]


def seed_accepted_order(
    connection: sqlite3.Connection,
    *,
    client_order_id: str,
    symbol: str = BTC,
    side: str = "BUY",
    quantity: str = "0.01",
    intent_status: str = INTENT_STATUS_SUBMITTED,
    stored_status: str | None = "accepted",
) -> int:
    """Local state exactly as a process that submitted and then died left it.

    The intent is committed, the broker acknowledged the order, and the last
    thing this database heard was `accepted`. Whatever the order did afterwards
    happened while nothing was watching, which is the situation every recovery
    test in this file starts from.

    `stored_status=None` leaves the intent with no broker snapshot at all -
    window 5, where the acknowledgement never made it to disk.
    """
    intent_id = record_order_intent(
        connection,
        client_order_id=client_order_id,
        created_at=T0,
        symbol=symbol,
        side=side,
        requested_quantity=Decimal(quantity),
        approved_quantity=Decimal(quantity),
        reference_price=100_000.0,
        risk_reason_code="APPROVED",
        status=intent_status,
    )
    if stored_status is not None:
        upsert_broker_order(
            connection,
            order_intent_id=intent_id,
            broker_order_id=BROKER_ORDER_UUID,
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            quantity=Decimal(quantity),
            filled_quantity=Decimal("0"),
            filled_average_price=None,
            status=stored_status,
            submitted_at=T0,
            filled_at=None,
            updated_at=T0,
        )
    return intent_id


# ==========================================================================
# THE NAMED CRITICAL TESTS
#
# Eight sentences the system must keep true. Everything after this section
# widens the coverage; nothing after it matters if one of these fails.
# ==========================================================================


def test_crash_after_checkpoint_does_not_replay_bar_after_restart(
    database_path: Path, paper_gate: None
) -> None:
    """Window 2. The claim commits, the process dies, and the bar is gone forever.

    A legitimate crossover is lost here, and that is the intended outcome. The
    alternative - claiming the bar only after the order is placed - would let
    the restarted process submit a second order for a crossover that happened
    once, and there is no way to take that back.
    """
    market_data = FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes())})

    class DiesAfterTheClaim(SqliteCheckpoint):
        def mark_processed(self, symbol: str, bar_timestamp: datetime) -> None:
            super().mark_processed(symbol, bar_timestamp)
            raise Crash("power lost immediately after the claim committed")

    with connect(database_path) as first_process:
        runtime = build_runtime(
            first_process,
            market_data=market_data,
            checkpoint=DiesAfterTheClaim(first_process),
        )
        runtime.start()
        with pytest.raises(Crash):
            runtime.run_cycle()

    # The claim outlived the process that made it.
    with connect(database_path) as after_crash:
        claim = get_runtime_checkpoint(after_crash, BTC)
        assert claim is not None, "the claim did not survive the crash"
        assert claim.last_processed_bar_timestamp == T_BAR

    execution = FakeExecution()
    with connect(database_path) as second_process:
        restarted = build_runtime(
            second_process,
            market_data=FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes())}),
            execution=execution,
        )
        restarted.start()
        report = restarted.run_cycle()

        assert list_signals(second_process) == [], "the claimed bar reached the strategy again"

    entry = next(item for item in report.results if item.symbol == BTC)
    assert entry.skipped_reason == "ALREADY_PROCESSED"
    assert entry.signal is None
    assert execution.calls == [], "the restarted process re-traded a claimed bar"


@pytest.mark.parametrize(
    ("crash_point", "expected_status"),
    [
        pytest.param("preflight", INTENT_STATUS_CREATED, id="window-3-before-submit"),
        pytest.param("submit", INTENT_STATUS_SUBMITTING, id="window-4-during-submit"),
    ],
)
def test_crash_after_intent_commit_never_submits_stale_intent_on_restart(
    database_path: Path, paper_gate: None, crash_point: str, expected_status: str
) -> None:
    """Window 3 and its neighbour: a committed intent is closed off, never sent.

    The decision behind it is stale by the time anyone looks: it was sized
    against an account and a price from before the crash, on a bar whose
    crossover has long since stopped being news. Reconciliation confirms the
    broker never saw it - twice, because one 404 could be a lookup that raced a
    submission - and marks it terminal so nothing can execute it later.

    Both crash points are covered because they leave *different* residue -
    `CREATED` from a process that died before the call, `SUBMITTING` from one
    that died inside it - and recovery must not care which. Neither may be
    assumed to have reached the broker, and neither may be re-sent.
    """
    client = FakeBrokerClient()

    def die(*_args: object, **_kwargs: object) -> None:
        raise Crash(f"power lost at the {crash_point}")

    if crash_point == "preflight":
        client.get_order_by_client_id = die  # type: ignore[method-assign]
    else:
        client.submit_order = die  # type: ignore[method-assign]

    with connect(database_path) as first_process, pytest.raises(Crash):
        execute_paper_order(
            first_process,
            symbol=BTC,
            side="BUY",
            requested_quantity=Decimal("0.01"),
            trading_client=client,
            data_client=FakeDataClient(),
            now=T0,
        )

    # The anchor survived: durable, and still carrying its original key.
    with connect(database_path) as after_crash:
        stale = only_intent(after_crash)
        assert stale.status == expected_status
        assert get_broker_order_by_intent(after_crash, stale.id) is None

        broker = FakeReconClient()  # a client that answers 404 for every key
        result = reconcile_paper_state(
            after_crash,
            trading_client=broker,
            now=T0,
            recheck_delay_seconds=0.0,
            sleep=no_sleep,
        )

        assert result.status is ReconciliationStatus.REPAIRED
        assert result.safe_to_trade is True
        assert broker.submit_calls == [], "recovery submitted the stale intent"
        assert broker.lookup_calls.count(stale.client_order_id) >= 2, (
            "one not-found answer is not enough to conclude an order was never sent"
        )

        settled = only_intent(after_crash)
        assert settled.status == INTENT_STATUS_CONFIRMED_NOT_SUBMITTED
        assert settled.client_order_id == stale.client_order_id
        assert list_broker_orders(after_crash) == []


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(TimeoutError("read timed out"), id="timeout"),
        pytest.param(ConnectionResetError("connection reset by peer"), id="connection-reset"),
        *[
            pytest.param(api_error(status, f"http {status}"), id=f"http-{status}")
            for status in AMBIGUOUS_STATUSES
        ],
        pytest.param(api_error(None, "unreadable status"), id="unreadable-status"),
    ],
)
def test_ambiguous_submit_never_retries_and_blocks_future_trading(
    database_path: Path, paper_gate: None, failure: Exception
) -> None:
    """Window 4. The response is lost, so the order may exist. Stop, do not re-send.

    Three things have to hold together, and each one alone is insufficient:
    `submit_order` is called exactly once, the intent keeps the *same*
    `client_order_id` so the outcome stays resolvable, and this process refuses
    to submit for the rest of its life.
    """
    client = FakeBrokerClient(submit=failure)
    market_data = FakeMarketData(
        {
            BTC: make_bars(BTC, closes=crossover_closes()),
            ETH: make_bars(ETH, closes=crossover_closes()),
        }
    )

    with connect(database_path) as process:
        runtime = build_runtime(
            process,
            market_data=market_data,
            execution=RealPathGateway(client),
        )
        runtime.start()
        report = runtime.run_cycle()

        assert len(client.submit_calls) == 1, "an ambiguous submission was retried"
        assert report.severity is CycleSeverity.TRADING_PAUSED
        assert runtime.state is RuntimeState.TRADING_PAUSED

        intent = only_intent(process)
        assert intent.status == INTENT_STATUS_UNKNOWN
        assert get_broker_order_by_intent(process, intent.id) is None
        assert any(intent.client_order_id in message for message in unknown_events(process))

        # ETH's turn never arrives: the cycle stops at the paused symbol.
        assert [symbol for symbol, _, _ in market_data.calls] == [BTC]

        # And a further cycle submits nothing either. BTC is refused by its own
        # claim, ETH by the pause - two independent reasons, one outcome.
        follow_up = runtime.run_cycle()
        assert len(client.submit_calls) == 1, "a paused runtime submitted again"
        assert {item.symbol: item.skipped_reason for item in follow_up.results} == {
            BTC: "ALREADY_PROCESSED",
            ETH: "TRADING_PAUSED",
        }
        assert len(list_order_intents(process)) == 1, "a paused runtime created a second intent"


def test_database_failure_before_intent_commit_prevents_broker_submission(
    database_path: Path, paper_gate: None
) -> None:
    """No durable intent, no broker submission. The hard edge of the whole design.

    A real write lock is held by a second connection, so the intent insert fails
    the way it fails in production. The broker must never be called: an order
    placed without a committed `client_order_id` is an order that a restart has
    no way to find, and therefore an order that can be duplicated.
    """
    client = FakeBrokerClient()

    with connect(database_path) as process, WriteLockHolder(database_path) as lock:
        lock.impatient(process)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            execute_paper_order(
                process,
                symbol=BTC,
                side="BUY",
                requested_quantity=Decimal("0.01"),
                trading_client=client,
                data_client=FakeDataClient(),
                now=T0,
            )

    assert client.submit_calls == [], "an order was submitted without a durable intent"
    assert client.preflight_calls == [], "the broker was reached at all"

    with connect(database_path) as after:
        assert list_order_intents(after) == []
        assert list_broker_orders(after) == []


def test_broker_fill_after_process_death_is_recovered_by_reconciliation(
    database_path: Path,
) -> None:
    """Window 7. The order fills while nobody is watching, and the restart finds it.

    Local state stops at `accepted` because that is what the dying process
    knew. The broker went on to fill it. A restart asks about the same
    `client_order_id`, records the fill, and adopts the broker's position -
    without placing anything.
    """
    with connect(database_path) as first_process:
        seed_accepted_order(first_process, client_order_id="autotrader-fill-after-death")

    filled_at = T0 + timedelta(minutes=2)
    broker = FakeReconClient(
        positions=[make_position(BTC, qty="0.01", avg_entry_price="100000", market_value="1000")],
        orders={
            "autotrader-fill-after-death": make_recon_order(
                client_order_id="autotrader-fill-after-death",
                qty="0.01",
                filled_qty="0.01",
                filled_avg_price="100000",
                status=OrderStatus.FILLED,
                filled_at=filled_at,
            )
        },
    )

    with connect(database_path) as restarted:
        result = reconcile_paper_state(
            restarted,
            trading_client=broker,
            now=T0,
            recheck_delay_seconds=0.0,
            sleep=no_sleep,
        )

        assert result.status is ReconciliationStatus.REPAIRED
        assert result.safe_to_trade is True
        assert broker.submit_calls == [], "recovery placed an order"

        intent = only_intent(restarted)
        assert intent.status == INTENT_STATUS_SUBMITTED
        snapshot = get_broker_order_by_intent(restarted, intent.id)
        assert snapshot is not None
        assert snapshot.status == "filled"
        assert snapshot.filled_quantity == Decimal("0.01")
        assert snapshot.filled_average_price == 100_000.0
        assert snapshot.filled_at == filled_at

        position = get_position(restarted, BTC)
        assert position is not None
        assert position.quantity == Decimal("0.01"), "the broker's fill did not become the position"


def test_partial_fill_after_restart_preserves_actual_filled_quantity(
    database_path: Path,
) -> None:
    """Window 6. Ordered 0.01, filled 0.004. Local state must say 0.004.

    Rounding a partial up to the ordered quantity would tell the risk engine
    this account holds more than it does, and every later sizing decision would
    inherit the lie. Nothing here fills the gap with a replacement order either:
    the remaining 0.006 is the broker's business, not this system's.
    """
    with connect(database_path) as first_process:
        seed_accepted_order(first_process, client_order_id="autotrader-partial")

    broker = FakeReconClient(
        positions=[make_position(BTC, qty="0.004", avg_entry_price="99000", market_value="396")],
        orders={
            "autotrader-partial": make_recon_order(
                client_order_id="autotrader-partial",
                qty="0.01",
                filled_qty="0.004",
                filled_avg_price="99000",
                status=OrderStatus.PARTIALLY_FILLED,
            )
        },
    )

    with connect(database_path) as restarted:
        result = reconcile_paper_state(
            restarted,
            trading_client=broker,
            now=T0,
            recheck_delay_seconds=0.0,
            sleep=no_sleep,
        )

        assert result.safe_to_trade is True
        assert broker.submit_calls == [], "the unfilled remainder was re-ordered"

        intent = only_intent(restarted)
        snapshot = get_broker_order_by_intent(restarted, intent.id)
        assert snapshot is not None
        assert snapshot.quantity == Decimal("0.01"), "the ordered quantity was rewritten"
        assert snapshot.filled_quantity == Decimal("0.004"), "a partial fill was rounded up"
        assert snapshot.filled_average_price == 99_000.0
        assert snapshot.status == "partially_filled"

        position = get_position(restarted, BTC)
        assert position is not None
        assert position.quantity == Decimal("0.004")

        # Still open, so a later pass must ask again rather than assume it settled.
        assert len(list_order_intents(restarted)) == 1


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(TimeoutError("read timed out"), id="timeout"),
        *[
            pytest.param(api_error(status, f"http {status}"), id=f"http-{status}")
            for status in (408, 429, 500, 502, 503)
        ],
    ],
)
def test_rate_limit_during_market_data_cycle_causes_no_order(
    database_path: Path, paper_gate: None, failure: Exception
) -> None:
    """A provider that will not answer produces no trade and no second attempt.

    The bar is never claimed, so the *next* scheduled cycle may still act on it
    if the provider recovers in time. What must not happen is a retry inside
    this cycle: two symbols every fifteen minutes is the whole API budget, and
    a tight loop against a rate limiter is how an account gets throttled out of
    the trades it can still make.
    """
    market_data = FakeMarketData(error=HistoricalDataError(f"provider refused: {failure}"))
    client = FakeBrokerClient()

    with connect(database_path) as process:
        runtime = build_runtime(
            process,
            market_data=market_data,
            execution=RealPathGateway(client),
        )
        runtime.start()
        report = runtime.run_cycle()

        assert client.submit_calls == [], "a provider failure produced an order"
        assert list_order_intents(process) == [], "a provider failure created an intent"
        assert get_runtime_checkpoint(process, BTC) is None, "an unfetched bar was claimed"
        assert report.severity is CycleSeverity.RETRY_NEXT_CYCLE

    # One attempt per symbol per cycle, and BTC's failure did not cancel ETH's turn.
    assert len(market_data.calls) == len(set(symbol for symbol, _, _ in market_data.calls))


def test_second_process_cannot_submit_against_same_database(
    database_path: Path, paper_gate: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two runners on one database is duplicate trading, so the second one stops.

    It stops *early*: before market data, before the broker, and before the
    startup reconciliation pass that would itself read the broker. The proof is
    negative on all three counts - no provider call, no gateway call, and no
    reconciliation run row written to the database the second process shares
    with the first.
    """

    def refuse_fetch(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the second runner reached market data")

    def refuse_execute(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the second runner reached the broker")

    monkeypatch.setattr(runtime_market_data.AlpacaCryptoBars, "recent_bars", refuse_fetch)
    monkeypatch.setattr(runtime_execution.PaperExecutionGateway, "execute", refuse_execute)

    with connect(database_path) as before:
        runs_before = len(list_reconciliation_runs(before))

    holder = RuntimeLock(lock_path_for(database_path))
    holder.acquire()
    try:
        # Fully authorized on the command line: the only thing standing between
        # this process and a submission is the lock.
        result = runner.invoke(
            app,
            [
                "crypto-run",
                "--once",
                "--confirm-paper-runtime",
                RUNTIME_CONFIRMATION_TOKEN,
                "--db",
                str(database_path),
            ],
        )
    finally:
        holder.release()

    assert result.exit_code == 1
    assert "already holds" in result.output

    with connect(database_path) as after:
        assert list_order_intents(after) == []
        assert list_broker_orders(after) == []
        assert len(list_reconciliation_runs(after)) == runs_before, (
            "the refused runner reached the broker to reconcile"
        )


# ==========================================================================
# WINDOW 5 - the broker accepted, and the answer never reached the disk
#
# The order exists. What this process knows about it does not. Everything in
# this section is about refusing to treat "I could not record it" as "it did
# not happen" - which is the same mistake as treating a timeout as a rejection,
# committed one step later.
# ==========================================================================


def test_a_malformed_submit_response_is_ambiguous_and_pauses_trading(
    database_path: Path, paper_gate: None
) -> None:
    """An unreadable answer to a submission means the order may exist. Stop.

    `submit_order` returned, so a request definitely reached the broker; the
    response could not be turned into a snapshot, so nothing is known about
    what came back. That is the *definition* of an ambiguous outcome, and it
    must be handled like one - marked UNKNOWN under the same
    `client_order_id`, never retried, and trading paused - rather than
    classified as an ordinary controlled failure the daemon shrugs off and
    trades through fifteen minutes later.
    """
    unreadable = make_broker_order(client_order_id="ignored", qty="not-a-number")
    client = FakeBrokerClient(submit=unreadable)

    with connect(database_path) as process:
        runtime = build_runtime(
            process,
            market_data=FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes())}),
            execution=RealPathGateway(client),
        )
        runtime.start()
        report = runtime.run_cycle()

        assert len(client.submit_calls) == 1, "an unreadable response was retried"
        assert report.severity is CycleSeverity.TRADING_PAUSED
        assert runtime.state is RuntimeState.TRADING_PAUSED

        intent = only_intent(process)
        assert intent.status == INTENT_STATUS_UNKNOWN, (
            "an order whose acknowledgement could not be read was not recorded as unknown"
        )
        assert any(intent.client_order_id in message for message in unknown_events(process))

        # Nothing was invented about an order nobody could read.
        assert get_broker_order_by_intent(process, intent.id) is None


def test_a_reply_the_database_rejects_is_ambiguous_not_successful(
    database_path: Path, paper_gate: None
) -> None:
    """The broker said yes and the answer would not go into the database.

    Here the reply parses but does not survive storage validation: the broker
    acknowledges an order of quantity zero, which is not a thing an order can
    be and which the `broker_orders` column refuses to hold. Reporting an
    ordinary failure would leave the caller free to trade on the next boundary
    while a real order sits at the broker with nothing recorded against it. The
    attempt must end ambiguous so a restart goes and asks.
    """
    impossible = make_broker_order(client_order_id="ignored", qty="0")
    client = FakeBrokerClient(submit=impossible)

    with connect(database_path) as process:
        with pytest.raises(AmbiguousSubmissionError):
            execute_paper_order(
                process,
                symbol=BTC,
                side="BUY",
                requested_quantity=Decimal("0.01"),
                trading_client=client,
                data_client=FakeDataClient(),
                now=T0,
            )

        assert len(client.submit_calls) == 1, "a submission that could not be stored was retried"
        intent = only_intent(process)
        assert intent.status == INTENT_STATUS_UNKNOWN, (
            "local state does not record that an order may exist at the broker"
        )
        assert get_broker_order_by_intent(process, intent.id) is None
        assert any(intent.client_order_id in message for message in unknown_events(process))


def test_a_reply_that_arrives_while_the_database_is_locked_is_still_recoverable(
    database_path: Path, paper_gate: None
) -> None:
    """The write lock is taken by someone else at exactly the wrong moment.

    Marking the intent UNKNOWN needs the same database that just refused the
    snapshot, so that write fails too and the failure surfaces raw - which the
    runtime treats as fatal, and a stopped process submits nothing. What has to
    survive is the recovery anchor: a committed `client_order_id` in a
    non-terminal state, so the restart finds the order the broker really has.
    """
    client = FakeBrokerClient()
    lock = WriteLockHolder(database_path)

    def take_the_lock_on_the_way_back(_request: object) -> None:
        lock.__enter__()

    client.on_submit = take_the_lock_on_the_way_back

    try:
        with connect(database_path) as process:
            lock.impatient(process)
            with pytest.raises(BaseException) as raised:
                execute_paper_order(
                    process,
                    symbol=BTC,
                    side="BUY",
                    requested_quantity=Decimal("0.01"),
                    trading_client=client,
                    data_client=FakeDataClient(),
                    now=T0,
                )
    finally:
        lock.__exit__()

    assert len(client.submit_calls) == 1, "a submission that could not be stored was retried"
    assert classify(raised.value) is CycleSeverity.FATAL, (
        "a process that cannot record what it did must stop, not carry on"
    )

    with connect(database_path) as after:
        intent = only_intent(after)
        assert intent.status != INTENT_STATUS_SUBMITTED, (
            "local state claims a stored order it never managed to store"
        )
        assert intent.status in (INTENT_STATUS_SUBMITTING, INTENT_STATUS_UNKNOWN)
        assert intent.client_order_id.startswith("autotrader-")
        assert get_broker_order_by_intent(after, intent.id) is None

        # The residue is reconcilable, which is the only thing that matters.
        broker = FakeReconClient(
            orders={
                intent.client_order_id: make_recon_order(
                    client_order_id=intent.client_order_id, qty="0.01"
                )
            }
        )
        result = reconcile_paper_state(
            after, trading_client=broker, now=T0, recheck_delay_seconds=0.0, sleep=no_sleep
        )
        assert result.status is ReconciliationStatus.REPAIRED
        assert broker.submit_calls == []
        assert only_intent(after).status == INTENT_STATUS_SUBMITTED


def test_crash_after_broker_accept_before_local_snapshot_is_repaired_not_replaced(
    database_path: Path,
) -> None:
    """Window 5 end to end: the restart finds the order and adopts it.

    No replacement order, no second `client_order_id`, and no inference that
    an accepted order is a filled one.
    """
    with connect(database_path) as first_process:
        seed_accepted_order(
            database_or_connection := first_process,
            client_order_id="autotrader-accepted-not-stored",
            intent_status=INTENT_STATUS_SUBMITTING,
            stored_status=None,
        )
        assert database_or_connection is first_process

    broker = FakeReconClient(
        orders={
            "autotrader-accepted-not-stored": make_recon_order(
                client_order_id="autotrader-accepted-not-stored",
                qty="0.01",
                status=OrderStatus.ACCEPTED,
            )
        }
    )

    with connect(database_path) as restarted:
        result = reconcile_paper_state(
            restarted,
            trading_client=broker,
            now=T0,
            recheck_delay_seconds=0.0,
            sleep=no_sleep,
        )

        assert result.status is ReconciliationStatus.REPAIRED
        assert result.safe_to_trade is True
        assert broker.submit_calls == [], "a replacement order was placed"

        intent = only_intent(restarted)
        assert intent.status == INTENT_STATUS_SUBMITTED
        assert intent.client_order_id == "autotrader-accepted-not-stored"

        snapshot = get_broker_order_by_intent(restarted, intent.id)
        assert snapshot is not None
        assert snapshot.status == "accepted"
        assert snapshot.filled_quantity == Decimal("0"), "an accepted order was read as filled"
        assert get_position(restarted, BTC) is None, "a position was inferred from an acceptance"


# ==========================================================================
# DURABILITY - the two writes that must be on disk before anything happens
#
# `record_order_intent` and `upsert_runtime_checkpoint` both commit, and both
# join an enclosing transaction if one is open rather than committing inside
# it. That composability is deliberate and useful everywhere else in the
# storage layer. At these two call sites it is a loaded gun: a caller that
# wrapped either one would keep the write invisible and roll it back on a
# crash, while the irreversible thing it was supposed to protect - a broker
# order, a strategy action - had already happened.
#
# So both are checked rather than merely arranged.
# ==========================================================================


def test_a_non_durable_intent_can_never_reach_the_broker(
    database_path: Path, paper_gate: None
) -> None:
    """An uncommitted intent is not an anchor, so no order may be placed against it.

    An intent that exists only inside an open transaction is invisible to every
    other connection and vanishes entirely if the process dies. Submitting
    against one would put a real order at the broker under a
    `client_order_id` that no restart could ever find - an order this system
    could neither reconcile nor cancel, and would happily duplicate.
    """
    client = FakeBrokerClient()
    visible_to_others: list[str] = []

    def look_from_another_connection(_request: object) -> None:
        with connect(database_path) as observer:
            visible_to_others.extend(item.client_order_id for item in list_order_intents(observer))

    client.on_submit = look_from_another_connection

    with connect(database_path) as process:
        process.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(ExecutionError):
                execute_paper_order(
                    process,
                    symbol=BTC,
                    side="BUY",
                    requested_quantity=Decimal("0.01"),
                    trading_client=client,
                    data_client=FakeDataClient(),
                    now=T0,
                )
        finally:
            process.execute("ROLLBACK")

    assert client.submit_calls == [], "an order was submitted against an uncommitted intent"
    assert visible_to_others == []

    with connect(database_path) as after:
        assert list_order_intents(after) == []
        assert list_broker_orders(after) == []


def test_the_durable_path_still_submits_normally(database_path: Path, paper_gate: None) -> None:
    """The guard above must refuse only the undurable case, never the ordinary one.

    A check that fails closed on everything is not a safety property, it is an
    outage. This is the same call with no enclosing transaction, and it has to
    go all the way through.
    """
    client = FakeBrokerClient()
    seen: list[str] = []

    def observe(_request: object) -> None:
        with connect(database_path) as observer:
            seen.extend(item.client_order_id for item in list_order_intents(observer))

    client.on_submit = observe

    with connect(database_path) as process:
        result = execute_paper_order(
            process,
            symbol=BTC,
            side="BUY",
            requested_quantity=Decimal("0.01"),
            trading_client=client,
            data_client=FakeDataClient(),
            now=T0,
        )

    assert len(client.submit_calls) == 1
    assert result.intent is not None
    assert seen == [result.intent.client_order_id], (
        "the intent was not durable on another connection at the moment of submission"
    )


def test_a_non_durable_checkpoint_claim_is_refused(database_path: Path) -> None:
    """A claim inside an open transaction is not a claim, so the bar is not claimed.

    If this were allowed through, the strategy would act on a bar whose claim
    could still be rolled back - and a crash between the action and the commit
    would leave the bar looking unprocessed to the next process. That is the
    duplicate-trade direction, which is the one this system refuses to take.
    """
    with connect(database_path) as process:
        checkpoint = SqliteCheckpoint(process)
        process.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(Exception) as raised:
                checkpoint.mark_processed(BTC, T_BAR)
        finally:
            process.execute("ROLLBACK")

    assert not isinstance(raised.value, AssertionError)

    with connect(database_path) as after:
        assert get_runtime_checkpoint(after, BTC) is None


def test_a_durable_checkpoint_claim_is_visible_to_another_connection(
    database_path: Path,
) -> None:
    """And the ordinary path still works, immediately, from a different connection."""
    with connect(database_path) as process:
        SqliteCheckpoint(process).mark_processed(BTC, T_BAR)
        with connect(database_path) as observer:
            claim = get_runtime_checkpoint(observer, BTC)
    assert claim is not None
    assert claim.last_processed_bar_timestamp == T_BAR


# ==========================================================================
# WINDOW 1 - death before the claim
#
# The only window whose correct answer is "the bar is still available". No
# durable decision was taken, so nothing has to be honoured; the opportunity
# is simply still there if the next process gets to it in time.
# ==========================================================================


def test_crash_before_the_checkpoint_leaves_the_bar_available_after_restart(
    database_path: Path, paper_gate: None
) -> None:
    """Window 1. Bars fetched, process dies, and nothing durable happened.

    The distinction from window 2 is the whole point of claiming *before*
    deciding: up to the claim there is nothing to be consistent with, so a
    restart may legitimately act on the bar. After it, there is, and it may
    not.
    """
    dying_provider = FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes())})
    original = dying_provider.recent_bars

    def fetch_then_die(symbol: str, **kwargs: object):
        # The fetch really happens - and is really thrown away, which is the
        # point: work done before the claim buys the system nothing.
        original(symbol, **kwargs)  # type: ignore[arg-type]
        raise Crash("power lost after the bars arrived and before anything was claimed")

    dying_provider.recent_bars = fetch_then_die  # type: ignore[method-assign]

    with connect(database_path) as first_process:
        runtime = build_runtime(first_process, market_data=dying_provider)
        runtime.start()
        with pytest.raises(Crash):
            runtime.run_cycle()

    with connect(database_path) as after_crash:
        assert get_runtime_checkpoint(after_crash, BTC) is None, "an unprocessed bar was claimed"
        assert list_order_intents(after_crash) == []

    execution = FakeExecution()
    with connect(database_path) as second_process:
        restarted = build_runtime(
            second_process,
            market_data=FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes())}),
            execution=execution,
        )
        restarted.start()
        report = restarted.run_cycle()

    entry = next(item for item in report.results if item.symbol == BTC)
    assert entry.processed is True, "an unclaimed bar was refused after a restart"
    assert entry.bar_timestamp == T_BAR
    assert BTC in execution.symbols


# ==========================================================================
# NETWORK AND HTTP - the difference between "no" and "I do not know"
#
# One classification decision runs through every test here: a broker that
# *refused* leaves no order, and a broker that did not answer might. The first
# is safe to treat as final; reading the second the same way is how a system
# convinces itself an order does not exist and places it again.
# ==========================================================================


@pytest.mark.parametrize("status", AMBIGUOUS_STATUSES)
def test_an_ambiguous_status_is_never_read_as_a_rejection(
    database_path: Path, paper_gate: None, status: int
) -> None:
    """408, 429 and every 5xx may have reached the matching engine.

    A rate limit is the subtle one: it *feels* like a refusal, and Alpaca
    returns it before the order is worked - usually. "Usually" is not a
    property a trading system can rely on, so 429 is ambiguous like the rest.
    """
    client = FakeBrokerClient(submit=api_error(status, f"http {status}"))

    with connect(database_path) as process:
        with pytest.raises(AmbiguousSubmissionError):
            execute_paper_order(
                process,
                symbol=BTC,
                side="BUY",
                requested_quantity=Decimal("0.01"),
                trading_client=client,
                data_client=FakeDataClient(),
                now=T0,
            )

        assert len(client.submit_calls) == 1
        assert only_intent(process).status == INTENT_STATUS_UNKNOWN


@pytest.mark.parametrize("status", (400, 403, 422))
def test_a_definitive_rejection_is_terminal_and_never_retried(
    database_path: Path, paper_gate: None, status: int
) -> None:
    """A 4xx that is not a timeout or a rate limit means no order was created.

    Recorded as `REJECTED` rather than `UNKNOWN`, because the two call for
    completely different follow-ups: a rejection is finished, and an unknown
    has to be settled against the broker before anything else is sent.
    """
    client = FakeBrokerClient(submit=api_error(status, "no"))

    with connect(database_path) as process:
        with pytest.raises(BrokerRejectedOrderError):
            execute_paper_order(
                process,
                symbol=BTC,
                side="BUY",
                requested_quantity=Decimal("0.01"),
                trading_client=client,
                data_client=FakeDataClient(),
                now=T0,
            )

        assert len(client.submit_calls) == 1, "a definitive rejection was retried"
        assert only_intent(process).status == "REJECTED"
        assert unknown_events(process) == []


def test_a_rejected_order_does_not_pause_the_runtime_but_places_nothing(
    database_path: Path, paper_gate: None
) -> None:
    """A refusal is an ordinary outcome; only ambiguity stops the process.

    Distinguishing them matters operationally: a runtime that halted on every
    broker "no" would need a human for something the broker settled by itself.
    """
    client = FakeBrokerClient(submit=api_error(422, "cost basis too small"))

    with connect(database_path) as process:
        runtime = build_runtime(
            process,
            market_data=FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes())}),
            execution=RealPathGateway(client),
        )
        runtime.start()
        report = runtime.run_cycle()

        assert report.severity is CycleSeverity.RETRY_NEXT_CYCLE
        assert runtime.state is not RuntimeState.TRADING_PAUSED
        assert len(client.submit_calls) == 1
        assert list_broker_orders(process) == []


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(TimeoutError("read timed out"), id="timeout"),
        *[
            pytest.param(api_error(status, f"http {status}"), id=f"http-{status}")
            for status in (408, 429, 500, 502, 503)
        ],
        pytest.param(api_error(None, "unreadable"), id="unreadable-status"),
    ],
)
def test_a_failed_duplicate_preflight_prevents_submission(
    database_path: Path, paper_gate: None, failure: Exception
) -> None:
    """ "The check failed" and "there is no duplicate" are different answers.

    Only a definitive 404 means no order exists under this key. Everything else
    leaves the question open, and submitting into an open question is how an
    order gets placed twice.
    """
    client = FakeBrokerClient(preflight=failure)

    with connect(database_path) as process:
        with pytest.raises(DuplicatePreflightUnavailableError):
            execute_paper_order(
                process,
                symbol=BTC,
                side="BUY",
                requested_quantity=Decimal("0.01"),
                trading_client=client,
                data_client=FakeDataClient(),
                now=T0,
            )

        assert client.submit_calls == [], "an order was submitted on an incomplete check"
        assert only_intent(process).status == INTENT_STATUS_CREATED


def test_a_malformed_preflight_reply_prevents_submission(
    database_path: Path, paper_gate: None
) -> None:
    """A duplicate check that answers with something unreadable answers nothing."""
    client = FakeBrokerClient(preflight={"not": "an order"})

    with connect(database_path) as process, pytest.raises(DuplicatePreflightUnavailableError):
        execute_paper_order(
            process,
            symbol=BTC,
            side="BUY",
            requested_quantity=Decimal("0.01"),
            trading_client=client,
            data_client=FakeDataClient(),
            now=T0,
        )

    assert client.submit_calls == []


def test_a_preflight_that_finds_the_order_submits_nothing(
    database_path: Path, paper_gate: None
) -> None:
    """The recovery path in miniature: the key is already at the broker.

    This is what makes a restart safe even in the window where the previous
    process had submitted but not recorded: the same `client_order_id` is
    presented, the broker recognises it, and the answer is adopted rather than
    re-sent.
    """
    existing = make_broker_order(client_order_id="ignored", qty="0.01")
    client = FakeBrokerClient(preflight=existing)

    with connect(database_path) as process:
        result = execute_paper_order(
            process,
            symbol=BTC,
            side="BUY",
            requested_quantity=Decimal("0.01"),
            trading_client=client,
            data_client=FakeDataClient(),
            now=T0,
        )

        assert client.submit_calls == [], "a known-duplicate order was submitted anyway"
        assert result.outcome.value == "DUPLICATE"
        assert only_intent(process).status == INTENT_STATUS_SUBMITTED


def test_a_market_data_failure_leaves_the_next_wake_on_its_own_boundary(
    database_path: Path, paper_gate: None
) -> None:
    """A failed cycle must not turn into a retry loop against the provider.

    The scheduler is wall-clock, not elapsed-time, so a cycle that fails in a
    millisecond still waits for the next 15-minute boundary. Proven by running
    the loop and counting: three cycles, three provider calls per symbol, and
    the clock advanced a full interval between each.
    """
    market_data = FakeMarketData(error=HistoricalDataError("provider rate limited"))
    clock = FakeClock()
    sleeps: list[float] = []

    def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(timedelta(seconds=seconds))

    with connect(database_path) as process:
        runtime = CryptoRuntime(
            process,
            market_data=market_data,
            execution=FakeExecution(),
            startup_safety=safe_startup,
            config=RuntimeConfig(runtime_confirmation=RUNTIME_CONFIRMATION_TOKEN),
            clock=clock,
            sleep=record_sleep,
        )
        runtime.start()
        reports = runtime.run_forever(max_cycles=3)

    assert len(reports) == 3
    assert all(report.severity is CycleSeverity.RETRY_NEXT_CYCLE for report in reports)
    # Two symbols per cycle, one attempt each. Anything more is a retry.
    assert len(market_data.calls) == 6
    # Total slept is at least two whole intervals: the loop waited for
    # boundaries rather than spinning after each failure.
    assert sum(sleeps) >= 2 * 15 * 60


def test_a_failing_cycle_makes_no_broker_call_at_all(database_path: Path, paper_gate: None) -> None:
    """A provider failure must not spend the account's broker budget either.

    The account, position, asset and price reads all live behind the execution
    boundary and only happen when a signal needs sizing. No bars means no
    signal, which must mean no broker traffic whatsoever.
    """
    client = FakeBrokerClient()
    data_client = FakeDataClient()

    with connect(database_path) as process:
        runtime = build_runtime(
            process,
            market_data=FakeMarketData(error=HistoricalDataError("gateway timeout")),
            execution=RealPathGateway(client, data_client),
        )
        runtime.start()
        runtime.run_cycle()

    assert client.submit_calls == []
    assert client.preflight_calls == []
    assert client.asset_calls == []
    assert data_client.requests == []


# ==========================================================================
# SQLITE - locked, busy, or refusing the write
#
# Two writes in this system gate an irreversible action, and both must fail
# closed: the bar claim gates the strategy, and the order intent gates the
# broker. A database that will not accept either has to stop the thing it was
# supposed to authorize, not let it through unrecorded.
# ==========================================================================


def test_a_locked_database_before_the_checkpoint_prevents_any_strategy_action(
    database_path: Path, paper_gate: None
) -> None:
    """No durable claim, no decision. The claim is not bookkeeping, it is the gate.

    Acting on a bar whose claim did not commit would leave the bar looking
    unprocessed to the next process, which would then act on it again.
    """
    client = FakeBrokerClient()

    with connect(database_path) as process:
        runtime = build_runtime(
            process,
            market_data=FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes())}),
            execution=RealPathGateway(client),
        )
        # The process started cleanly; the database is taken from under it
        # afterwards, which is what a concurrent writer actually looks like.
        runtime.start()

        with WriteLockHolder(database_path) as lock:
            lock.impatient(process)
            report = runtime.run_cycle()

        assert report.severity is CycleSeverity.FATAL, "a lost claim did not stop the cycle"
        assert client.submit_calls == []
        assert client.preflight_calls == []

    with connect(database_path) as after:
        assert get_runtime_checkpoint(after, BTC) is None
        assert list_order_intents(after) == []
        assert list_signals(after) == [], "the strategy acted on an unclaimed bar"


def test_a_locked_database_during_signal_persistence_creates_no_order(
    database_path: Path, paper_gate: None
) -> None:
    """A signal that cannot be recorded must not become an order.

    The audit trail is not decoration. An order whose originating signal was
    never written down cannot be explained afterwards, and "we could not write
    it down" is a database in a state nothing should be trading through.
    """
    client = FakeBrokerClient()

    with connect(database_path) as process:
        runtime = build_runtime(
            process,
            market_data=FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes())}),
            execution=RealPathGateway(client),
        )
        runtime.start()

        # Claim the bar first, so the lock lands between the claim and the signal.
        with WriteLockHolder(database_path) as lock:
            lock.impatient(process)
            report = runtime.run_cycle()

        assert report.severity is CycleSeverity.FATAL
        assert client.submit_calls == []

    with connect(database_path) as after:
        assert list_order_intents(after) == []
        assert list_broker_orders(after) == []


def test_a_locked_database_during_a_runtime_cycle_never_reaches_the_broker(
    database_path: Path, paper_gate: None
) -> None:
    """The whole-cycle version of the rule, from the runtime's own severity table.

    A broken local database is fatal rather than retryable: it will not fix
    itself on the next boundary, and a daemon that keeps waking up to fail is
    worse than one that stops and says why.

    The loop is asked for five cycles and gets one. Shutting down writes to the
    same locked database, so the process ends by raising rather than returning
    its reports - which is the honest outcome and, for the purposes of this
    test, still an exit: the provider was asked once, and the broker never.
    """
    client = FakeBrokerClient()
    market_data = FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes())})

    with connect(database_path) as process:
        runtime = build_runtime(process, market_data=market_data, execution=RealPathGateway(client))
        runtime.start()
        with WriteLockHolder(database_path) as lock:
            lock.impatient(process)
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                runtime.run_forever(max_cycles=5)

        assert runtime.state is RuntimeState.FAILED

    assert len(market_data.calls) == 1, "the runtime kept looping on a fatal database failure"
    assert client.submit_calls == []
    assert client.preflight_calls == []


def test_a_write_failure_during_reconciliation_leaves_trading_blocked(
    database_path: Path,
) -> None:
    """A repair that cannot be written is unresolved, and unresolved means no trading.

    The broker here returns an order whose `broker_order_id` is already
    recorded against a *different* intent - a contradiction the storage layer
    refuses rather than silently overwrites. The pass reports it and closes the
    gate rather than pretending local state now matches.
    """
    with connect(database_path) as process:
        seed_accepted_order(process, client_order_id="autotrader-owns-the-id")
        seed_accepted_order(
            process,
            client_order_id="autotrader-wants-the-id",
            intent_status=INTENT_STATUS_UNKNOWN,
            stored_status=None,
        )

    broker = FakeReconClient(
        orders={
            "autotrader-owns-the-id": make_recon_order(
                client_order_id="autotrader-owns-the-id", qty="0.01"
            ),
            # Same broker order id, different key: an impossible answer.
            "autotrader-wants-the-id": make_recon_order(
                client_order_id="autotrader-wants-the-id",
                qty="0.01",
                order_id=BROKER_ORDER_UUID,
            ),
        }
    )

    with connect(database_path) as process:
        result = reconcile_paper_state(
            process, trading_client=broker, now=T0, recheck_delay_seconds=0.0, sleep=no_sleep
        )

        assert result.status is ReconciliationStatus.UNRESOLVED
        assert result.safe_to_trade is False, "an unwritable repair still permitted trading"
        assert broker.submit_calls == []

        contested = next(
            item
            for item in list_order_intents(process)
            if item.client_order_id == "autotrader-wants-the-id"
        )
        assert contested.status == INTENT_STATUS_UNKNOWN, "an unresolved intent was closed off"


def test_an_unresolved_reconciliation_keeps_the_runtime_observing_and_silent(
    database_path: Path, paper_gate: None
) -> None:
    """Unresolved reconciliation blocks trading without blocking observation.

    The distinction matters: a system that stopped fetching and evaluating
    while it was blocked would come back with no idea what had happened in the
    meantime.
    """
    with connect(database_path) as process:
        seed_accepted_order(process, client_order_id="autotrader-unresolvable")

    execution = FakeExecution()
    unreachable = FakeReconClient(
        orders={"autotrader-unresolvable": api_error(503, "service unavailable")}
    )

    with connect(database_path) as process:

        def check() -> StartupSafetyResult:
            from autotrader.runtime.safety import startup_safety_from_reconciliation_result

            return startup_safety_from_reconciliation_result(
                reconcile_paper_state(
                    process,
                    trading_client=unreachable,
                    now=T0,
                    recheck_delay_seconds=0.0,
                    sleep=no_sleep,
                )
            )

        runtime = build_runtime(process, execution=execution, startup_safety=check)
        runtime.start()
        report = runtime.run_cycle()

        assert runtime.startup_safety is not None
        assert runtime.startup_safety.safe_to_trade is False
        assert execution.calls == [], "trading continued over an unresolved order"
        assert unreachable.submit_calls == []

        # Observation continued: bars were fetched, claimed and evaluated.
        assert all(item.processed for item in report.results)
        assert len(list_signals(process)) == len(report.results)


# ==========================================================================
# DUPLICATES - the same bar, the same signal, the same process, twice
# ==========================================================================


def test_a_duplicate_completed_bar_causes_no_second_strategy_action(
    database_path: Path, paper_gate: None
) -> None:
    """A provider that repeats the newest completed bar must not produce two orders.

    Nothing about the second fetch is wrong - the same bar really is the newest
    completed one until the next boundary - so the guard cannot be "notice the
    provider repeated itself". It has to be a claim the runtime made and kept.
    """
    execution = FakeExecution()
    frames = {
        BTC: make_bars(BTC, closes=crossover_closes()),
        ETH: make_bars(ETH, closes=crossover_closes()),
    }

    with connect(database_path) as process:
        runtime = build_runtime(process, market_data=FakeMarketData(frames), execution=execution)
        runtime.start()
        runtime.run_cycle()
        first_round = list(execution.symbols)

        second = runtime.run_cycle()
        third = runtime.run_cycle()

    assert first_round == [BTC, ETH]
    assert execution.symbols == first_round, "a repeated bar was acted on again"
    for report in (second, third):
        assert {item.skipped_reason for item in report.results} == {"ALREADY_PROCESSED"}


def test_a_duplicate_signal_is_recorded_once_and_does_not_multiply_orders(
    database_path: Path, paper_gate: None
) -> None:
    """The signal table's uniqueness is a second, independent record of the same rule.

    It is scoped to one strategy run, so it cannot substitute for the durable
    checkpoint across a restart - and this test says so by checking both:
    exactly one signal row, and exactly one execution attempt.
    """
    execution = FakeExecution()

    with connect(database_path) as process:
        runtime = build_runtime(
            process,
            market_data=FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes())}),
            execution=execution,
        )
        runtime.start()
        runtime.run_cycle()
        runtime.run_cycle()

        signals = [item for item in list_signals(process) if item.symbol == BTC]

    assert len(signals) == 1, "one crossover produced two signal rows"
    assert execution.symbols.count(BTC) == 1


def test_two_runtimes_on_one_database_cannot_both_claim_the_same_bar(
    database_path: Path, paper_gate: None
) -> None:
    """Belt and braces behind the process lock: the claim is shared, not per-process.

    If the lock were ever bypassed - a different lock path, an operator running
    the module directly - the durable checkpoint is what stops the second
    runner acting on the bar the first one already took.
    """
    first_execution = FakeExecution()
    second_execution = FakeExecution()
    frames = {BTC: make_bars(BTC, closes=crossover_closes())}

    with connect(database_path) as first, connect(database_path) as second:
        first_runtime = build_runtime(
            first, market_data=FakeMarketData(frames), execution=first_execution
        )
        second_runtime = build_runtime(
            second, market_data=FakeMarketData(frames), execution=second_execution
        )
        first_runtime.start()
        second_runtime.start()

        first_runtime.run_cycle()
        second_runtime.run_cycle()

    assert first_execution.symbols == [BTC]
    assert second_execution.calls == [], "a second runtime acted on an already-claimed bar"


# ==========================================================================
# RECONCILIATION - idempotent, authoritative, and never a trader
#
# Recovery is allowed to rewrite local rows and nothing else. Running it twice
# must cost a second look and change nothing, because a startup pass runs on
# every start and an operator will run it by hand whenever they are unsure.
# ==========================================================================


def test_repeated_reconciliation_is_repaired_then_clean(database_path: Path) -> None:
    """First pass repairs, second pass finds nothing left to do.

    A recovery pass that kept "repairing" the same row would make every restart
    look like an incident, and would make a genuine repair impossible to spot.
    """
    with connect(database_path) as process:
        seed_accepted_order(process, client_order_id="autotrader-idempotent")

    broker = FakeReconClient(
        positions=[make_position(BTC, qty="0.01", avg_entry_price="100000", market_value="1000")],
        orders={
            "autotrader-idempotent": make_recon_order(
                client_order_id="autotrader-idempotent",
                qty="0.01",
                filled_qty="0.01",
                filled_avg_price="100000",
                status=OrderStatus.FILLED,
                filled_at=T0,
            )
        },
    )

    with connect(database_path) as process:
        first = reconcile_paper_state(
            process, trading_client=broker, now=T0, recheck_delay_seconds=0.0, sleep=no_sleep
        )
        assert first.status is ReconciliationStatus.REPAIRED
        assert first.repaired_count >= 1

        after_first = get_broker_order_by_intent(process, only_intent(process).id)
        position_after_first = get_position(process, BTC)

        second = reconcile_paper_state(
            process, trading_client=broker, now=T0, recheck_delay_seconds=0.0, sleep=no_sleep
        )
        assert second.status is ReconciliationStatus.CLEAN
        assert second.repaired_count == 0
        assert second.unresolved_count == 0

        # Nothing moved on the second pass: same snapshot, same position.
        assert get_broker_order_by_intent(process, only_intent(process).id) == after_first
        assert get_position(process, BTC) == position_after_first

    assert broker.submit_calls == [], "reconciliation placed an order"


def test_a_settled_order_is_not_even_queried_on_the_next_pass(
    database_path: Path,
) -> None:
    """Idempotency is cheap as well as harmless: a finished order is not re-read.

    `filled`, `canceled` and `rejected` cannot change again, so asking about
    them on every startup would spend a broker call to learn nothing. An open
    or partially filled order is re-read, because it still can.
    """
    with connect(database_path) as process:
        seed_accepted_order(process, client_order_id="autotrader-settled")

    broker = FakeReconClient(
        orders={
            "autotrader-settled": make_recon_order(
                client_order_id="autotrader-settled",
                qty="0.01",
                filled_qty="0.01",
                filled_avg_price="100000",
                status=OrderStatus.FILLED,
                filled_at=T0,
            )
        },
        positions=[make_position(BTC, qty="0.01", avg_entry_price="100000", market_value="1000")],
    )

    with connect(database_path) as process:
        reconcile_paper_state(
            process, trading_client=broker, now=T0, recheck_delay_seconds=0.0, sleep=no_sleep
        )
        lookups_after_first = len(broker.lookup_calls)

        reconcile_paper_state(
            process, trading_client=broker, now=T0, recheck_delay_seconds=0.0, sleep=no_sleep
        )

    assert len(broker.lookup_calls) == lookups_after_first, "a settled order was queried again"


def test_a_partially_filled_order_is_still_queried_on_the_next_pass(
    database_path: Path,
) -> None:
    """A partial fill is not settled, so it is asked about again.

    This is the other half of the rule above, and it is the half that matters:
    treating `partially_filled` as terminal would freeze local state at the
    partial forever while the broker went on filling it.
    """
    with connect(database_path) as process:
        seed_accepted_order(process, client_order_id="autotrader-still-open")

    broker = FakeReconClient(
        orders={
            "autotrader-still-open": make_recon_order(
                client_order_id="autotrader-still-open",
                qty="0.01",
                filled_qty="0.004",
                filled_avg_price="99000",
                status=OrderStatus.PARTIALLY_FILLED,
            )
        },
        positions=[make_position(BTC, qty="0.004", avg_entry_price="99000", market_value="396")],
    )

    with connect(database_path) as process:
        reconcile_paper_state(
            process, trading_client=broker, now=T0, recheck_delay_seconds=0.0, sleep=no_sleep
        )
        lookups_after_first = len(broker.lookup_calls)

        reconcile_paper_state(
            process, trading_client=broker, now=T0, recheck_delay_seconds=0.0, sleep=no_sleep
        )

    assert len(broker.lookup_calls) > lookups_after_first, "an open order was assumed settled"


def test_a_partial_fill_never_produces_a_replacement_remainder_order(
    database_path: Path,
) -> None:
    """Nothing in this system tops up an under-filled order.

    A remainder order is a *new* trading decision, taken by a recovery pass, on
    a signal that fired at some earlier price. That is exactly the class of
    thing recovery must never do.
    """
    with connect(database_path) as process:
        seed_accepted_order(process, client_order_id="autotrader-no-topup")

    broker = FakeReconClient(
        orders={
            "autotrader-no-topup": make_recon_order(
                client_order_id="autotrader-no-topup",
                qty="0.01",
                filled_qty="0.001",
                filled_avg_price="100500",
                status=OrderStatus.PARTIALLY_FILLED,
            )
        },
        positions=[make_position(BTC, qty="0.001", avg_entry_price="100500", market_value="100")],
    )

    with connect(database_path) as process:
        for _ in range(3):
            reconcile_paper_state(
                process, trading_client=broker, now=T0, recheck_delay_seconds=0.0, sleep=no_sleep
            )

        assert broker.submit_calls == [], "a remainder order was placed"
        assert len(list_broker_orders(process)) == 1, "a second broker order row appeared"
        assert len(list_order_intents(process)) == 1, "a second intent appeared"

        snapshot = get_broker_order_by_intent(process, only_intent(process).id)
        assert snapshot is not None
        assert snapshot.filled_quantity == Decimal("0.001")


@pytest.mark.parametrize(
    ("status", "label"),
    [
        pytest.param(OrderStatus.CANCELED, "canceled", id="canceled"),
        pytest.param(OrderStatus.REJECTED, "rejected", id="rejected"),
        pytest.param(OrderStatus.EXPIRED, "expired", id="expired"),
    ],
)
def test_a_terminal_broker_outcome_after_process_death_is_recorded_not_replaced(
    database_path: Path, status: OrderStatus, label: str
) -> None:
    """The order died at the broker while nobody was watching. Record it, stop there.

    Resubmitting would be a brand new decision made by a recovery pass on stale
    information, and a cancelled order is not an invitation to try again.
    """
    with connect(database_path) as process:
        seed_accepted_order(process, client_order_id=f"autotrader-{label}")

    broker = FakeReconClient(
        orders={
            f"autotrader-{label}": make_recon_order(
                client_order_id=f"autotrader-{label}", qty="0.01", status=status
            )
        }
    )

    with connect(database_path) as process:
        result = reconcile_paper_state(
            process, trading_client=broker, now=T0, recheck_delay_seconds=0.0, sleep=no_sleep
        )

        assert result.safe_to_trade is True
        assert broker.submit_calls == [], "a terminal order was replaced"

        snapshot = get_broker_order_by_intent(process, only_intent(process).id)
        assert snapshot is not None
        assert snapshot.status == label
        assert snapshot.filled_quantity == Decimal("0")
        assert get_position(process, BTC) is None, "a position was invented for a dead order"


def test_a_stale_local_position_is_overwritten_by_broker_truth(
    database_path: Path,
) -> None:
    """The broker is authoritative in both directions, including downwards.

    A local row saying this account holds half a Bitcoin, and a broker that
    holds none, is a difference the risk engine would size against if it were
    left alone. It is corrected in the database - never by trading out of it.
    """
    with connect(database_path) as process:
        upsert_position(
            process, symbol=BTC, quantity=Decimal("0.5"), average_price=90_000.0, updated_at=T0
        )
        upsert_position(
            process, symbol=ETH, quantity=Decimal("0"), average_price=None, updated_at=T0
        )

    broker = FakeReconClient(
        positions=[make_position(ETH, qty="2", avg_entry_price="3000", market_value="6000")]
    )

    with connect(database_path) as process:
        result = reconcile_paper_state(
            process, trading_client=broker, now=T0, recheck_delay_seconds=0.0, sleep=no_sleep
        )

        assert result.status is ReconciliationStatus.REPAIRED
        assert broker.submit_calls == [], "a position mismatch was corrected by trading"

        btc = get_position(process, BTC)
        eth = get_position(process, ETH)
        assert btc is not None and btc.quantity == Decimal("0"), "a phantom position survived"
        assert eth is not None and eth.quantity == Decimal("2"), "an unseen position was ignored"


def test_a_stale_local_order_snapshot_is_repaired_from_broker_truth(
    database_path: Path,
) -> None:
    """Local state remembers `accepted`; the broker has moved on. The broker wins.

    Note what is *not* asserted: nothing maps the broker's `filled` onto the
    intent. The intent records that the order reached the broker; what the
    order then did lives in the snapshot, in the broker's own vocabulary.
    """
    with connect(database_path) as process:
        seed_accepted_order(process, client_order_id="autotrader-stale-snapshot")
        before = get_broker_order_by_intent(process, only_intent(process).id)
        assert before is not None and before.status == "accepted"

    broker = FakeReconClient(
        positions=[make_position(BTC, qty="0.01", avg_entry_price="101000", market_value="1010")],
        orders={
            "autotrader-stale-snapshot": make_recon_order(
                client_order_id="autotrader-stale-snapshot",
                qty="0.01",
                filled_qty="0.01",
                filled_avg_price="101000",
                status=OrderStatus.FILLED,
                filled_at=T0,
            )
        },
    )

    with connect(database_path) as process:
        reconcile_paper_state(
            process, trading_client=broker, now=T0, recheck_delay_seconds=0.0, sleep=no_sleep
        )

        after = get_broker_order_by_intent(process, only_intent(process).id)
        assert after is not None
        assert after.status == "filled"
        assert after.filled_quantity == Decimal("0.01")
        assert after.filled_average_price == 101_000.0
        assert only_intent(process).status == INTENT_STATUS_SUBMITTED


def test_reconciliation_places_no_order_in_any_failure_branch(
    database_path: Path,
) -> None:
    """Every way a pass can go wrong, and not one of them submits.

    The individual outcomes are asserted elsewhere; what this collects is the
    single property that has to hold across all of them at once.
    """
    scenarios = {
        "found": make_recon_order(client_order_id="k", qty="0.01"),
        "ambiguous-5xx": api_error(503, "unavailable"),
        "ambiguous-timeout": TimeoutError("read timed out"),
        "rate-limited": api_error(429, "slow down"),
        "unreadable-status": api_error(None, "no status"),
        "malformed": {"not": "an order"},
    }

    for label, answer in scenarios.items():
        database = initialize_database(database_path.parent / f"{label}.db")
        with connect(database) as process:
            seed_accepted_order(process, client_order_id="k", intent_status=INTENT_STATUS_UNKNOWN)
            broker = FakeReconClient(orders={"k": answer})
            reconcile_paper_state(
                process, trading_client=broker, now=T0, recheck_delay_seconds=0.0, sleep=no_sleep
            )
            assert broker.submit_calls == [], f"{label} caused a submission"
            assert len(list_order_intents(process)) == 1, f"{label} created a second intent"


# ==========================================================================
# THE RISK CEILING - failures must not enlarge an order
#
# Every failure above is about *whether* an order goes out. This one is about
# how big it is when it does: the risk engine's approved quantity is a ceiling,
# and no recovery, retry or repair may raise it.
# ==========================================================================


def test_the_submitted_quantity_never_exceeds_the_risk_approved_quantity(
    database_path: Path, paper_gate: None
) -> None:
    """The runtime asks for a billion units; risk clamps it; the clamp is what ships.

    The runtime deliberately asks for more than any account could hold so that
    the risk engine - the only sizing authority - decides the size. What reaches
    the wire must be that decision rounded *down* to the broker's increment, and
    never the request.
    """
    client = FakeBrokerClient()

    with connect(database_path) as process:
        runtime = build_runtime(
            process,
            market_data=FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes())}),
            execution=RealPathGateway(client),
        )
        runtime.start()
        report = runtime.run_cycle()

        entry = next(item for item in report.results if item.symbol == BTC)
        assert entry.execution is not None
        decision = entry.execution.risk_decision
        assert decision.approved

        [request] = client.submit_calls
        wire_quantity = Decimal(str(request.qty))
        assert wire_quantity <= decision.approved_quantity, (
            "the broker was asked for more than risk approved"
        )
        assert wire_quantity < entry.execution.requested_quantity

        intent = only_intent(process)
        assert intent.approved_quantity == wire_quantity
        assert intent.approved_quantity <= intent.requested_quantity


def test_a_recovered_order_is_never_resized_upwards(database_path: Path) -> None:
    """Recovery copies the broker's quantity; it does not re-derive one.

    If a repair recomputed the size against a *current* account it would be
    making a fresh sizing decision on an order that has already been placed.
    """
    with connect(database_path) as process:
        seed_accepted_order(process, client_order_id="autotrader-sized", quantity="0.01")

    broker = FakeReconClient(
        orders={
            "autotrader-sized": make_recon_order(
                client_order_id="autotrader-sized",
                qty="0.01",
                filled_qty="0.004",
                filled_avg_price="100000",
                status=OrderStatus.PARTIALLY_FILLED,
            )
        },
        positions=[make_position(BTC, qty="0.004", avg_entry_price="100000", market_value="400")],
    )

    with connect(database_path) as process:
        reconcile_paper_state(
            process, trading_client=broker, now=T0, recheck_delay_seconds=0.0, sleep=no_sleep
        )

        intent = only_intent(process)
        snapshot = get_broker_order_by_intent(process, intent.id)
        assert snapshot is not None
        assert snapshot.quantity == intent.approved_quantity == Decimal("0.01")
        assert snapshot.filled_quantity <= snapshot.quantity


# ==========================================================================
# THE PROPERTIES THAT HOLD ACROSS EVERY FAILURE ABOVE
# ==========================================================================


@pytest.mark.parametrize(
    ("submit_answer", "expected_error", "expected_intent_status"),
    [
        pytest.param(None, None, INTENT_STATUS_SUBMITTED, id="accepted"),
        pytest.param(
            api_error(504, "gateway timeout"),
            AmbiguousSubmissionError,
            INTENT_STATUS_UNKNOWN,
            id="ambiguous",
        ),
        pytest.param(
            api_error(422, "cost basis too small"),
            BrokerRejectedOrderError,
            "REJECTED",
            id="rejected",
        ),
    ],
)
def test_submitted_is_never_read_as_filled_under_any_outcome(
    database_path: Path,
    paper_gate: None,
    submit_answer: Exception | None,
    expected_error: type[Exception] | None,
    expected_intent_status: str,
) -> None:
    """An acknowledgement is not a fill, and neither is an unknown outcome.

    Three endings, and none of them may leave this account holding a position
    it does not hold. The risk engine sizes against `positions`, so a
    fabricated fill there becomes a real oversized order on the next boundary -
    which is how one bookkeeping shortcut turns into a trade nobody decided to
    make.

    The position row written here comes from what the broker reported *before*
    the attempt, and nothing after the attempt touches it.
    """
    client = FakeBrokerClient(submit=submit_answer)

    with connect(database_path) as process:
        if expected_error is None:
            result = execute_paper_order(
                process,
                symbol=BTC,
                side="BUY",
                requested_quantity=Decimal("0.01"),
                trading_client=client,
                data_client=FakeDataClient(),
                now=T0,
            )
            assert result.outcome.value == "SUBMITTED"
            assert result.submitted is True
        else:
            with pytest.raises(expected_error):
                execute_paper_order(
                    process,
                    symbol=BTC,
                    side="BUY",
                    requested_quantity=Decimal("0.01"),
                    trading_client=client,
                    data_client=FakeDataClient(),
                    now=T0,
                )

        assert only_intent(process).status == expected_intent_status

        position = get_position(process, BTC)
        assert position is not None
        assert position.quantity == Decimal("0"), (
            "a position was inferred from a submission rather than read from the broker"
        )


def test_no_failure_path_here_opens_a_socket(
    database_path: Path, paper_gate: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of injecting failures is that no real broker is involved.

    A test that quietly reached the network would be measuring Alpaca's
    availability rather than this system's behaviour, and on a bad day it would
    be doing so with real credentials against a real account. So the two paths
    that *could* reach out - a runtime cycle that submits, and a reconciliation
    pass that looks orders up - are run here with sockets removed entirely.
    """
    import socket

    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a failure-injection test must not open a socket")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

    client = FakeBrokerClient(submit=api_error(503, "unavailable"))
    with connect(database_path) as process:
        runtime = build_runtime(
            process,
            market_data=FakeMarketData({BTC: make_bars(BTC, closes=crossover_closes())}),
            execution=RealPathGateway(client),
        )
        runtime.start()
        runtime.run_cycle()
        assert runtime.state is RuntimeState.TRADING_PAUSED

        broker = FakeReconClient(
            orders={only_intent(process).client_order_id: api_error(503, "unavailable")}
        )
        result = reconcile_paper_state(
            process, trading_client=broker, now=T0, recheck_delay_seconds=0.0, sleep=no_sleep
        )
        assert result.safe_to_trade is False


def test_no_production_module_gained_a_fault_injection_switch() -> None:
    """The failures are injected from the tests, never from the shipped code.

    A trading system with a chaos mode has a way to be told to misbehave, and
    that way is reachable in production by definition. Everything in this file
    goes through a dependency the system already had - the trading client, the
    data client, the market-data source, the clock, a second connection - so
    nothing had to be added for it to be testable.

    The scan is over executable code with docstrings and comments stripped, so
    the prose that explains this rule cannot be what satisfies it.
    """
    import autotrader
    from test_reconciliation import code_without_prose

    banned = (
        "CHAOS",
        "FAULT_INJECT",
        "SIMULATE_FAILURE",
        "FAIL_ON_PURPOSE",
        "AUTOTRADER_TEST_MODE",
        "AUTOTRADER_DEBUG_ENDPOINT",
    )
    root = Path(autotrader.__file__).resolve().parent

    for path in sorted(root.rglob("*.py")):
        code = code_without_prose(path.read_text()).upper()
        for token in banned:
            assert token not in code, f"{path.relative_to(root)} contains a fault switch: {token}"
