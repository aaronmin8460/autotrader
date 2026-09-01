"""EDA-1 against the paper broker: target semantics, parity, and paper-only.

The four blockers that stopped the first activation attempt each get a test
here, and so does every safety rule the runtime claims:

*Blocker 1 - no execution seam.* There is one now, and it is a new module
wrapped around the validated decision code rather than a change to it: the
EDA-1 answer this runtime records is byte-identical to the shadow's for the
same bar, asserted by running both over the same frames.

*Blocker 2 - no sizing policy.* The runtime refuses to start without one, and
the plan it builds is the frozen allocator's.

*Blocker 3 - the schema split.* A v7 runtime must never open the crypto store
through the migrating path; the cross-store readers here use read-only URIs and
single SELECTs, and a test asserts the crypto store's schema version is
unchanged after the paper runtime has read it.

*Blocker 4 - unresolved intents.* An intent with no settled broker outcome
stops the runtime at startup and blocks mutation mid-session, because a second
order for the same target is exactly what a duplicate is.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from autotrader.equity import EQUITY_SYMBOLS
from autotrader.equity.allocation import (
    POLICY_RESERVED_UNIVERSE,
    AllocationPolicy,
)
from autotrader.equity.paper import (
    EVENT_PAPER_PARITY_MISMATCH,
    PAPER_ACCOUNT_PREFIX,
    PAPER_DECISION_ORDER,
    ROLLOUT_STAGES,
    SESSION_OPEN,
    STAGE_A,
    STAGE_B,
    STAGE_C,
    Disposition,
    EquityPaperConfig,
    EquityPaperError,
    EquityPaperRuntime,
    NotPaperAccountError,
    PaperIntegrityError,
    ParityRecord,
    SqliteExternalSafety,
    SqliteShadowParity,
    non_equity_exposure,
    require_paper_account,
)
from autotrader.equity.regime import ParticipationSpec
from autotrader.equity.shadow import EquityShadowConfig, EquityShadowRuntime
from autotrader.execution.models import OrderSide
from autotrader.execution.paper import ExecutionOutcome, PaperAccountState
from autotrader.risk.engine import RiskDecision
from autotrader.runtime.runner import ShutdownRequest
from autotrader.state import sqlite as state
from autotrader.state.sqlite import connect, initialize_database
from conftest import establish_account_safety
from test_equity_runtime import (
    SESSION,
    T_BAR,
    FakeClock,
    FakeEquityBars,
    make_equity_bars,
)
from test_equity_session import FakeCalendar
from test_equity_shadow import FakeRegimeBars, make_state_frame

pytestmark = pytest.mark.filterwarnings("ignore::FutureWarning")

POLICY = AllocationPolicy(policy_id=POLICY_RESERVED_UNIVERSE)

#: A router the fixture sessions can actually warm up: two completed sessions
#: rather than two hundred. The *rule* is unchanged - close above its own
#: moving average and drawdown above the calm threshold - so a fixture that
#: participates exercises the real branch rather than a special case. The
#: production spec stays the research default and is asserted elsewhere.
FIXTURE_SPEC = ParticipationSpec(sma_sessions=2)


#: Monotonically rising closes, so the fixture regime is PARTICIPATE: the last
#: completed close is above the two-session average and the trailing-peak
#: drawdown is zero.
def participating_state_frame():
    return make_state_frame(closes=[500.0 + 5.0 * index for index in range(17)])


ACCOUNT_EQUITY = 100_000.0

PRICES = dict(
    zip(
        EQUITY_SYMBOLS,
        (765.0, 714.0, 293.0, 314.0, 510.0, 219.0, 261.0, 338.0, 571.0, 366.0),
        strict=True,
    )
)


# ==========================================================================
# Fakes
# ==========================================================================


class FakePosition:
    def __init__(self, symbol: str, quantity: Decimal, market_value: float) -> None:
        self.symbol = symbol
        self.quantity = quantity
        self.market_value = market_value
        self.average_entry_price = market_value / float(quantity) if quantity else None


class FakeBrokerState:
    """Account equity and positions, mutable so a test can settle a fill."""

    def __init__(self, equity: float = ACCOUNT_EQUITY) -> None:
        self.equity = equity
        self.positions: dict[str, FakePosition] = {}
        self.calls = 0

    def hold(self, symbol: str, quantity: Decimal, price: float) -> None:
        key = symbol.replace("/", "").upper()
        self.positions[key] = FakePosition(symbol, quantity, float(quantity) * price)

    def __call__(self) -> tuple[float, dict[str, object]]:
        self.calls += 1
        return self.equity, dict(self.positions)


class RecordingGateway:
    """Accepts every delta and records it. Never touches a network."""

    def __init__(self, *, approved_fraction: Decimal = Decimal(1), approve: bool = True) -> None:
        self.calls: list[tuple[str, OrderSide, Decimal]] = []
        self._approved_fraction = approved_fraction
        self._approve = approve

    def execute(
        self,
        connection: sqlite3.Connection,
        *,
        symbol: str,
        side: OrderSide,
        requested_quantity: Decimal,
        now: datetime,
        strategy_run_id: int | None,
    ):
        self.calls.append((symbol, side, requested_quantity))
        approved = (requested_quantity * self._approved_fraction).to_integral_value()
        from autotrader.execution.paper import PaperExecutionResult

        decision = RiskDecision(
            approved=self._approve,
            approved_quantity=approved if self._approve else Decimal(0),
            reason_code="APPROVED" if self._approve else "POSITION_LIMIT",
            message="fake",
            max_allowed_quantity=approved,
        )
        # The real boundary mints an intent with a FRESH uuid - it is not
        # derived from the decision - and returns it. A fake that returned none
        # would let a runtime that mis-keys its own audit row pass this suite,
        # which is exactly what happened before this line existed.
        from autotrader.execution.models import OrderIntent

        intent = (
            OrderIntent(
                symbol=symbol,
                side=side,
                requested_quantity=requested_quantity,
                approved_quantity=approved,
                reference_price=PRICES[symbol],
                risk_reason_code=decision.reason_code,
                created_at=now,
            )
            if self._approve
            else None
        )
        return PaperExecutionResult(
            outcome=(
                ExecutionOutcome.SUBMITTED if self._approve else ExecutionOutcome.REJECTED_BY_RISK
            ),
            symbol=symbol,
            side=side,
            requested_quantity=requested_quantity,
            reference_price=PRICES[symbol],
            risk_decision=decision,
            intent=intent,
            account=PaperAccountState(
                equity=ACCOUNT_EQUITY,
                cash=ACCOUNT_EQUITY,
                status="ACTIVE",
                trading_blocked=False,
                account_blocked=False,
                trade_suspended_by_user=False,
            ),
            daily_baseline_equity=Decimal(str(ACCOUNT_EQUITY)),
            message="fake",
        )


class RefusingGateway:
    """A gateway a test asserts is never reached."""

    def execute(self, connection, **kwargs):  # noqa: ANN001, ANN003
        raise AssertionError(f"the runtime submitted an order it should not have: {kwargs}")


class AgreeingParity:
    """A shadow that agrees with whatever the paper runtime decided."""

    def __init__(self) -> None:
        self.queries: list[tuple[str, datetime]] = []
        self.answers: dict[tuple[str, datetime], ParityRecord] = {}

    def decision_for(self, symbol: str, bar_timestamp: datetime) -> ParityRecord | None:
        self.queries.append((symbol, bar_timestamp))
        return self.answers.get((symbol, bar_timestamp))


class SafeExternal:
    def unsafe_reason(self) -> str | None:
        return None


class HaltedExternal:
    def unsafe_reason(self) -> str | None:
        return "The crypto store reports UNSAFE_UNKNOWN_ORDER: one order is ambiguous."


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    database = tmp_path / "equity-paper.db"
    initialize_database(database)
    with connect(database) as open_connection:
        establish_account_safety(open_connection)
        yield open_connection


def all_symbol_frames() -> dict[str, object]:
    return {symbol: make_equity_bars(symbol) for symbol in PAPER_DECISION_ORDER}


def build_paper(
    connection: sqlite3.Connection,
    *,
    gateway=None,
    parity=None,
    external=None,
    broker=None,
    stage: str = "C",
    require_parity: bool = False,
    parity_price_tolerance: float = 0.01,
    participating: bool = True,
    clock: FakeClock | None = None,
    calendar: FakeCalendar | None = None,
    bars: FakeEquityBars | None = None,
    policy: AllocationPolicy | None = None,
    open_orders=None,
) -> EquityPaperRuntime:
    return EquityPaperRuntime(
        connection,
        market_data=bars if bars is not None else FakeEquityBars(all_symbol_frames()),
        regime_data=FakeRegimeBars(participating_state_frame() if participating else None),
        calendar=calendar if calendar is not None else FakeCalendar([SESSION]),
        gateway=gateway if gateway is not None else RecordingGateway(),
        parity=parity if parity is not None else AgreeingParity(),
        external_safety=external if external is not None else SafeExternal(),
        regime_spec=FIXTURE_SPEC,
        config=EquityPaperConfig(
            policy=policy if policy is not None else POLICY,
            stage=stage,
            require_parity=require_parity,
            parity_price_tolerance=parity_price_tolerance,
        ),
        broker_state=broker if broker is not None else FakeBrokerState(),
        open_orders=open_orders,
        clock=clock if clock is not None else FakeClock(),
        sleep=lambda seconds: None,
        shutdown=ShutdownRequest(),
    )


# ==========================================================================
# Paper-only environment
# ==========================================================================


class FakePaperClient:
    _base_url = "https://paper-api.alpaca.markets"
    _sandbox = True

    def __init__(self, account_number: str = "PA35G6605TN1") -> None:
        self._account_number = account_number

    def get_account(self) -> object:
        return type("Acct", (), {"account_number": self._account_number})()


class FakeLiveClient(FakePaperClient):
    _base_url = "https://api.alpaca.markets"
    _sandbox = False


def test_a_paper_client_and_a_paper_account_number_are_both_required() -> None:
    """CRITICAL. Two independent confirmations, and both must hold."""
    assert require_paper_account(FakePaperClient()).startswith(PAPER_ACCOUNT_PREFIX)


def test_a_live_endpoint_is_rejected() -> None:
    """CRITICAL. No live path exists; this refuses one anyway."""
    from autotrader.execution.paper import NotPaperEnvironmentError

    with pytest.raises(NotPaperEnvironmentError):
        require_paper_account(FakeLiveClient())


def test_a_paper_host_with_a_non_paper_account_number_is_rejected() -> None:
    """CRITICAL. The host says paper; the account that answered does not."""
    with pytest.raises(NotPaperAccountError):
        require_paper_account(FakePaperClient(account_number="9X1234567"))


def test_a_client_that_does_not_report_itself_a_sandbox_is_rejected() -> None:
    from autotrader.execution.paper import NotPaperEnvironmentError

    class Ambiguous(FakePaperClient):
        _sandbox = None

    with pytest.raises(NotPaperEnvironmentError):
        require_paper_account(Ambiguous())


def test_the_repository_constructs_exactly_one_trading_client() -> None:
    """CRITICAL. The structural proof that a live environment is unreachable."""
    root = Path(__file__).resolve().parents[1] / "src"
    hits = [
        (path, line)
        for path in root.rglob("*.py")
        for line in path.read_text(encoding="utf-8").splitlines()
        if "TradingClient(" in line and "def " not in line and not line.strip().startswith("#")
    ]
    assert len(hits) == 1, hits
    assert hits[0][0].name == "paper.py"
    assert hits[0][0].parent.name == "execution"


def test_no_source_file_names_a_live_endpoint_or_a_paper_false_argument() -> None:
    """CRITICAL. There is nothing to reject because there is nothing to select.

    Docstrings and comments are stripped first. This package's own prose names
    what it forbids - "``paper=False`` appears nowhere in the package" - so a
    naive substring scan would trip over the sentence that states the rule.
    """
    from test_runtime import code_without_prose

    root = Path(__file__).resolve().parents[1] / "src"
    for path in root.rglob("*.py"):
        code = code_without_prose(path.read_text(encoding="utf-8"))
        assert "paper=False" not in code, path
        assert "TRADING_LIVE" not in code, path
        assert "api.alpaca.markets" not in code.replace("paper-api.alpaca.markets", ""), path


def test_the_runtime_config_has_no_field_that_could_select_an_environment() -> None:
    fields = set(EquityPaperConfig.__dataclass_fields__)
    assert not any(
        token in name.lower() for name in fields for token in ("live", "endpoint", "base_url")
    )


def test_a_missing_sizing_policy_cannot_be_defaulted() -> None:
    """CRITICAL. There is no default sizing policy, so none can be assumed."""
    with pytest.raises(TypeError):
        EquityPaperConfig()  # type: ignore[call-arg]


# ==========================================================================
# Staged rollout
# ==========================================================================


def test_the_stages_are_nested_and_end_at_the_frozen_universe() -> None:
    assert STAGE_A == ("SPY",)
    assert set(STAGE_A) < set(STAGE_B) < set(STAGE_C)
    assert STAGE_C == EQUITY_SYMBOLS
    assert set(ROLLOUT_STAGES) == {"A", "B", "C"}


def test_an_unknown_stage_is_refused() -> None:
    with pytest.raises(EquityPaperError, match="Unknown rollout stage"):
        EquityPaperConfig(policy=POLICY, stage="Z")


def test_stage_a_decides_ten_symbols_and_may_mutate_only_spy(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. The decision universe never narrows; the execution universe does."""
    gateway = RecordingGateway()
    runtime = build_paper(connection, gateway=gateway, stage="A")
    report = runtime.run_once()

    assert report.session_state == SESSION_OPEN
    assert report.decided == len(PAPER_DECISION_ORDER)
    assert {symbol for symbol, _, _ in gateway.calls} <= {"SPY"}
    excluded = [
        outcome for outcome in report.outcomes if outcome.disposition is Disposition.NOT_IN_STAGE
    ]
    assert {outcome.symbol for outcome in excluded} == set(EQUITY_SYMBOLS) - {"SPY"}


def test_stage_c_may_mutate_every_symbol(connection: sqlite3.Connection) -> None:
    gateway = RecordingGateway()
    runtime = build_paper(connection, gateway=gateway, stage="C")
    runtime.run_once()
    assert {symbol for symbol, _, _ in gateway.calls} == set(EQUITY_SYMBOLS)


def test_every_funded_symbol_is_sized_by_the_frozen_allocator(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. Not by tuple order: ten simultaneous entries, all funded."""
    gateway = RecordingGateway()
    runtime = build_paper(connection, gateway=gateway, stage="C")
    report = runtime.run_once()

    assert report.plan is not None
    weights = {item.target_weight for item in report.plan.allocations}
    assert len(weights) == 1, weights
    assert all(quantity > 0 for _, _, quantity in gateway.calls)
    assert len(gateway.calls) == len(EQUITY_SYMBOLS)


# ==========================================================================
# Target semantics - the HOLD case that must not repeat a BUY
# ==========================================================================


def test_a_settled_target_produces_no_second_order(connection: sqlite3.Connection) -> None:
    """CRITICAL. PARTICIPATE every bar for years must not mean BUY every bar."""
    broker = FakeBrokerState()
    gateway = RecordingGateway()
    clock = FakeClock()
    runtime = build_paper(connection, gateway=gateway, broker=broker, stage="A", clock=clock)
    runtime.start()
    runtime.run_cycle()

    assert len(gateway.calls) == 1
    symbol, side, quantity = gateway.calls[0]
    assert side is OrderSide.BUY

    # The broker now holds exactly what was asked for, and the same bar is
    # re-offered: the runtime must want nothing.
    broker.hold(symbol, quantity, PRICES[symbol])
    gateway.calls.clear()
    second = runtime.run_cycle()
    runtime.stop()

    assert gateway.calls == []
    spy = next(item for item in second.outcomes if item.symbol == "SPY")
    assert spy.disposition is Disposition.ALREADY_SATISFIED


def test_an_oversized_holding_sells_only_the_excess(connection: sqlite3.Connection) -> None:
    """CRITICAL. A reduction is a partial SELL, never a full exit and re-entry."""
    broker = FakeBrokerState()
    broker.hold("SPY", Decimal(20), PRICES["SPY"])
    gateway = RecordingGateway()
    runtime = build_paper(connection, gateway=gateway, broker=broker, stage="A")
    runtime.run_once()

    assert len(gateway.calls) == 1
    symbol, side, quantity = gateway.calls[0]
    assert (symbol, side) == ("SPY", OrderSide.SELL)
    assert Decimal(0) < quantity < Decimal(20)


def test_a_partially_approved_delta_is_recorded_as_partially_allowed(
    connection: sqlite3.Connection,
) -> None:
    gateway = RecordingGateway(approved_fraction=Decimal("0.5"))
    runtime = build_paper(connection, gateway=gateway, stage="A")
    report = runtime.run_once()
    spy = next(item for item in report.outcomes if item.symbol == "SPY")
    assert spy.disposition is Disposition.PARTIALLY_ALLOWED


def test_a_risk_refusal_is_recorded_as_risk_blocked(connection: sqlite3.Connection) -> None:
    gateway = RecordingGateway(approve=False)
    runtime = build_paper(connection, gateway=gateway, stage="A")
    report = runtime.run_once()
    spy = next(item for item in report.outcomes if item.symbol == "SPY")
    assert spy.disposition is Disposition.RISK_BLOCKED
    assert spy.risk_reason_code == "POSITION_LIMIT"


# ==========================================================================
# Startup idempotence
# ==========================================================================


def _leave_unresolved_intent(connection: sqlite3.Connection) -> None:
    state.record_order_intent(
        connection,
        client_order_id="autotrader-unsettled-0001",
        created_at=datetime(2026, 8, 31, 14, 0, tzinfo=UTC),
        symbol="SPY",
        side="BUY",
        requested_quantity=Decimal(3),
        approved_quantity=Decimal(3),
        reference_price=765.0,
        risk_reason_code="APPROVED",
        strategy_run_id=None,
        status=state.INTENT_STATUS_CREATED,
    )


def test_an_unsettled_intent_stops_the_runtime_at_startup(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. A restart must not create a duplicate BUY."""
    _leave_unresolved_intent(connection)
    runtime = build_paper(connection, gateway=RefusingGateway(), stage="A")
    with pytest.raises(PaperIntegrityError, match="no settled broker outcome"):
        runtime.start()


def test_an_unsettled_intent_appearing_mid_session_blocks_further_mutation(
    connection: sqlite3.Connection,
) -> None:
    runtime = build_paper(connection, gateway=RefusingGateway(), stage="A")
    runtime.start()
    _leave_unresolved_intent(connection)
    report = runtime.run_cycle()
    spy = next(item for item in report.outcomes if item.symbol == "SPY")
    assert spy.disposition is Disposition.UNRESOLVED_INTENT


def test_a_restart_does_not_re_decide_a_bar_it_already_claimed(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. The bar claim is durable, so a restart holds its existing target."""
    broker = FakeBrokerState()
    gateway = RecordingGateway()
    first = build_paper(connection, gateway=gateway, broker=broker, stage="A")
    first.run_once()
    symbol, _, quantity = gateway.calls[0]
    broker.hold(symbol, quantity, PRICES[symbol])

    gateway.calls.clear()
    second = build_paper(connection, gateway=gateway, broker=broker, stage="A")
    report = second.run_once()

    assert gateway.calls == []
    spy = next(item for item in report.outcomes if item.symbol == "SPY")
    assert spy.disposition is Disposition.ALREADY_SATISFIED


# ==========================================================================
# Shadow / Paper parity
# ==========================================================================


def test_a_bar_the_shadow_has_not_recorded_blocks_mutation_when_parity_is_required(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. A missing comparison is not an agreement."""
    runtime = build_paper(connection, gateway=RefusingGateway(), stage="A", require_parity=True)
    report = runtime.run_once()
    assert report.parity_mismatches == len(PAPER_DECISION_ORDER)
    spy = next(item for item in report.outcomes if item.symbol == "SPY")
    assert spy.disposition is Disposition.PARITY_MISMATCH


def test_a_disagreeing_shadow_row_blocks_only_that_symbol(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. The smallest safe action: this symbol, this bar, nothing wider."""
    parity = AgreeingParity()

    class Disagreeing(AgreeingParity):
        def decision_for(self, symbol: str, bar_timestamp: datetime):
            if symbol != "SPY":
                return ParityRecord(
                    symbol=symbol,
                    bar_timestamp=bar_timestamp,
                    reference_close=0.0,
                    participate=True,
                    eda1_signal="BUY",
                    eda1_stance=1,
                )
            return ParityRecord(
                symbol=symbol,
                bar_timestamp=bar_timestamp,
                reference_close=0.0,
                participate=True,
                eda1_signal="SELL",
                eda1_stance=0,
            )

    gateway = RecordingGateway()
    runtime = build_paper(
        connection,
        gateway=gateway,
        parity=Disagreeing(),
        stage="C",
        require_parity=True,
        # The fake reports no real close, so the price comparison is widened out
        # of the way; this test is about the decision fields.
        parity_price_tolerance=1e9,
    )
    report = runtime.run_once()

    spy = next(item for item in report.outcomes if item.symbol == "SPY")
    assert spy.disposition is Disposition.PARITY_MISMATCH
    assert "SPY" not in {symbol for symbol, _, _ in gateway.calls}
    assert len(gateway.calls) == len(EQUITY_SYMBOLS) - 1
    assert parity.queries == []


def test_a_parity_mismatch_is_written_to_the_audit_trail(
    connection: sqlite3.Connection,
) -> None:
    runtime = build_paper(connection, gateway=RefusingGateway(), stage="A", require_parity=True)
    runtime.run_once()
    events = state.list_system_events(connection)
    assert any(event.event_type == EVENT_PAPER_PARITY_MISMATCH for event in events)


def test_the_paper_runtime_and_the_shadow_derive_the_same_eda1_decision(
    tmp_path: Path,
) -> None:
    """CRITICAL. Two processes, two stores, two computations, one answer.

    This is the proof that the paper adapter did not change EDA-1's semantics:
    the shadow's stored answer and the paper runtime's stored answer for the
    same bars, over identical frames, are compared field by field.
    """
    shadow_db = tmp_path / "shadow.db"
    paper_db = tmp_path / "paper.db"
    initialize_database(shadow_db)
    initialize_database(paper_db)

    with connect(shadow_db) as shadow_connection:
        shadow = EquityShadowRuntime(
            shadow_connection,
            market_data=FakeEquityBars(all_symbol_frames()),
            regime_data=FakeRegimeBars(participating_state_frame()),
            calendar=FakeCalendar([SESSION]),
            clock=FakeClock(),
            sleep=lambda seconds: None,
            shutdown=ShutdownRequest(),
            config=EquityShadowConfig(),
            regime_spec=FIXTURE_SPEC,
        )
        shadow.run_once()
        shadow_rows = shadow_connection.execute(
            "SELECT symbol, bar_timestamp, participate, eda1_signal, eda1_stance"
            " FROM shadow_side_by_side ORDER BY symbol"
        ).fetchall()

    with connect(paper_db) as paper_connection:
        establish_account_safety(paper_connection)
        paper = build_paper(paper_connection, gateway=RecordingGateway(), stage="C")
        paper.run_once()
        paper_rows = paper_connection.execute(
            "SELECT symbol, bar_timestamp, participate, eda1_signal, eda1_stance"
            " FROM shadow_side_by_side ORDER BY symbol"
        ).fetchall()

    assert len(shadow_rows) == len(PAPER_DECISION_ORDER)
    assert [tuple(row) for row in shadow_rows] == [tuple(row) for row in paper_rows]


# ==========================================================================
# Cross-store account safety and schema isolation
# ==========================================================================


def test_a_halt_in_another_products_store_stops_this_runtime(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. Separate stores must not mean a separate account halt."""
    runtime = build_paper(
        connection, gateway=RefusingGateway(), external=HaltedExternal(), stage="A"
    )
    report = runtime.run_once()
    spy = next(item for item in report.outcomes if item.symbol == "SPY")
    assert spy.disposition is Disposition.ACCOUNT_UNSAFE


def test_an_unreadable_external_store_fails_closed(tmp_path: Path) -> None:
    """CRITICAL. "Nobody could tell me" is not "it is fine"."""
    reader = SqliteExternalSafety(tmp_path / "absent.db")
    assert reader.unsafe_reason() is not None


def test_a_safe_external_store_reads_as_safe(tmp_path: Path) -> None:
    other = tmp_path / "crypto.db"
    initialize_database(other)
    with connect(other) as crypto_connection:
        establish_account_safety(crypto_connection)
    assert SqliteExternalSafety(other).unsafe_reason() is None


def test_reading_the_external_store_does_not_migrate_it(tmp_path: Path) -> None:
    """CRITICAL. Blocker 3: a v7 process must never upgrade the crypto store."""
    other = tmp_path / "crypto.db"
    initialize_database(other)
    with connect(other) as crypto_connection:
        establish_account_safety(crypto_connection)
        crypto_connection.execute("UPDATE schema_metadata SET schema_version = 6 WHERE id = 1")
        crypto_connection.commit()

    SqliteExternalSafety(other).unsafe_reason()
    SqliteShadowParity(other).decision_for("SPY", T_BAR)

    with sqlite3.connect(f"file:{other}?mode=ro", uri=True) as check:
        version = check.execute("SELECT schema_version FROM schema_metadata").fetchone()[0]
    assert version == 6


def test_the_cross_store_readers_open_no_writable_connection() -> None:
    """CRITICAL. Read-only URIs, asserted against the source rather than assumed."""
    import inspect

    for source in (
        inspect.getsource(SqliteExternalSafety.unsafe_reason),
        inspect.getsource(SqliteShadowParity.decision_for),
    ):
        assert "mode=ro" in source
        assert "initialize_database" not in source
        assert "INSERT" not in source.upper().replace("INSERTED", "")
        assert "UPDATE " not in source.upper()


# ==========================================================================
# Exposure, and what counts against the shared account
# ==========================================================================


def test_non_equity_positions_are_what_reduces_the_equity_budget() -> None:
    """CRITICAL. Crypto counts, and the ten equities do not count twice."""
    positions = {
        "ETHUSD": FakePosition("ETHUSD", Decimal("2.03"), 4997.40),
        "SPY": FakePosition("SPY", Decimal(3), 2295.0),
    }
    assert non_equity_exposure(positions) == pytest.approx(4997.40)


def test_the_allocator_sees_the_crypto_book_through_broker_truth(
    connection: sqlite3.Connection,
) -> None:
    broker = FakeBrokerState()
    broker.hold("ETHUSD", Decimal("2.03"), 2462.0)
    runtime = build_paper(connection, gateway=RecordingGateway(), broker=broker, stage="C")
    report = runtime.run_once()
    assert report.external_exposure_fraction is not None
    assert report.external_exposure_fraction > Decimal("0.04")
    assert report.plan is not None
    assert report.plan.total_target_weight + report.external_exposure_fraction <= POLICY.total_cap


def test_the_market_calendar_is_the_only_thing_that_opens_a_cycle(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. No local-time inference: a day with no session does nothing."""
    runtime = build_paper(
        connection, gateway=RefusingGateway(), calendar=FakeCalendar([]), stage="C"
    )
    report = runtime.run_once()
    assert report.session_state != SESSION_OPEN
    assert report.outcomes == []


# ==========================================================================
# Parity compares the target stance, not the transition signal
#
# Observed on the first live cycle of 2026-08-31: the shadow had been running
# since 15:22 and recorded its EDA-1 entry as a BUY on its own first bar, so by
# 16:45 it was HOLDing; the paper runtime started at 17:16 and recorded its
# entry as a BUY on *its* first bar, which was 16:45. Every decision field
# agreed - participate 1, stance 1, reference close 765.89 to the cent - and
# the signal differed on all ten symbols purely because the two series began on
# different days. Comparing the signal blocked every mutation. It is the wrong
# field to compare, and these tests pin the right one.
# ==========================================================================


def _record(signal: str, stance: int, *, participate: bool = True, close: float = 765.89):
    return ParityRecord(
        symbol="SPY",
        bar_timestamp=T_BAR,
        reference_close=close,
        participate=participate,
        eda1_signal=signal,
        eda1_stance=stance,
    )


def test_a_phase_shifted_transition_signal_is_not_a_disagreement() -> None:
    """CRITICAL. The exact 2026-08-31 first-cycle case, pinned."""
    paper = _record("BUY", 1)
    shadow = _record("HOLD", 1)
    assert paper.disagreement(shadow, price_tolerance=0.01) is None
    note = paper.phase_note(shadow)
    assert note is not None
    assert "phase-shifted" in note


def test_a_differing_stance_is_always_a_disagreement() -> None:
    """CRITICAL. The decision itself. Same signal cannot excuse it."""
    paper = _record("HOLD", 1)
    shadow = _record("HOLD", 0)
    assert paper.disagreement(shadow, price_tolerance=0.01) is not None
    assert paper.phase_note(shadow) is None


def test_a_differing_participate_state_is_a_disagreement() -> None:
    paper = _record("HOLD", 1)
    shadow = _record("HOLD", 1, participate=False)
    assert "participate" in (paper.disagreement(shadow, price_tolerance=0.01) or "")


def test_a_materially_different_reference_close_is_a_disagreement() -> None:
    paper = _record("HOLD", 1, close=765.89)
    shadow = _record("HOLD", 1, close=770.00)
    assert "reference_close" in (paper.disagreement(shadow, price_tolerance=0.01) or "")


def test_a_last_decimal_place_difference_is_tolerated() -> None:
    """Two provider reads of the same bar may differ in the last place."""
    paper = _record("HOLD", 1, close=765.89)
    shadow = _record("HOLD", 1, close=765.895)
    assert paper.disagreement(shadow, price_tolerance=0.01) is None


def test_identical_records_produce_neither_a_disagreement_nor_a_note() -> None:
    paper = _record("HOLD", 1)
    assert paper.disagreement(paper, price_tolerance=0.01) is None
    assert paper.phase_note(paper) is None


def test_a_phase_shifted_shadow_does_not_block_mutation(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. End to end: agreeing stance, differing signal, order still placed."""

    class PhaseShifted(AgreeingParity):
        def decision_for(self, symbol: str, bar_timestamp: datetime) -> ParityRecord | None:
            return ParityRecord(
                symbol=symbol,
                bar_timestamp=bar_timestamp,
                reference_close=0.0,
                participate=True,
                eda1_signal="HOLD",
                eda1_stance=1,
            )

    gateway = RecordingGateway()
    runtime = build_paper(
        connection,
        gateway=gateway,
        parity=PhaseShifted(),
        stage="A",
        require_parity=True,
        parity_price_tolerance=1e9,
    )
    report = runtime.run_once()

    assert report.parity_mismatches == 0
    assert runtime.parity_phase_notes > 0
    assert [symbol for symbol, _, _ in gateway.calls] == ["SPY"]


def test_the_heartbeat_reports_the_durable_safety_answer(
    connection: sqlite3.Connection,
) -> None:
    """A beat that printed a code this runtime never computed would be the
    crypto heartbeat's own defect: echoing a stale verdict as current."""
    runtime = build_paper(connection, gateway=RecordingGateway(), stage="A")
    runtime.start()
    beat = runtime.heartbeat
    assert beat.paper_execution_enabled is True
    assert beat.startup_safety_code == state.ACCOUNT_SAFETY_SAFE
    runtime.stop()


def test_a_restart_re_checks_parity_for_a_bar_it_already_claimed(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. A process bounce must not clear a shadow disagreement.

    Restarting re-reads the stored decision and re-derives a target from it, so
    a symbol the shadow disagreed with on its first claim would otherwise
    become eligible again simply because the process restarted - the block
    would last until the next restart rather than until the disagreement was
    resolved.
    """

    class Disagreeing(AgreeingParity):
        def decision_for(self, symbol: str, bar_timestamp: datetime) -> ParityRecord | None:
            return ParityRecord(
                symbol=symbol,
                bar_timestamp=bar_timestamp,
                reference_close=0.0,
                participate=True,
                eda1_signal="SELL",
                eda1_stance=0,
            )

    # First pass: claim the bars with an agreeing shadow and no execution.
    first = build_paper(connection, gateway=RecordingGateway(), stage="A")
    first.run_once()

    # Restart against a shadow that now disagrees, on the same already-claimed
    # bars, with a gateway that fails the test if it is ever reached.
    second = build_paper(
        connection,
        gateway=RefusingGateway(),
        parity=Disagreeing(),
        stage="A",
        require_parity=True,
        parity_price_tolerance=1e9,
    )
    report = second.run_once()

    spy = next(item for item in report.outcomes if item.symbol == "SPY")
    assert spy.disposition is Disposition.PARITY_MISMATCH
    assert report.parity_mismatches > 0


# ==========================================================================
# Blocker 3: the schema split, as repository tests rather than only as a
# proof run on the host
# ==========================================================================


def test_a_store_one_version_ahead_is_refused_rather_than_downgraded(tmp_path: Path) -> None:
    """CRITICAL. What the crypto build does when handed this lineage's store.

    The crypto service runs a build whose SCHEMA_VERSION is one below this
    one's. Simulated by stamping a current store one version ahead and asking
    this build to open it - the same code path, the same refusal, without
    needing two installs in one interpreter.
    """
    from autotrader.state.sqlite import SCHEMA_VERSION, UnsupportedSchemaVersionError

    database = tmp_path / "ahead.db"
    initialize_database(database)
    with connect(database) as open_connection:
        open_connection.execute(
            "UPDATE schema_metadata SET schema_version = ? WHERE id = 1", (SCHEMA_VERSION + 1,)
        )
        open_connection.commit()

    with pytest.raises(UnsupportedSchemaVersionError, match="newer than the supported version"):
        initialize_database(database)


def test_opening_an_older_store_the_normal_way_migrates_it(tmp_path: Path) -> None:
    """CRITICAL. Why the crypto store is never opened the normal way.

    This is not a defect - upward migration is what every command in this
    package relies on. It is the reason the crypto operational store must be
    reached only through a read-only URI: opening it through `connect` or
    `initialize_database` would take it to this lineage's version, and the
    running crypto service would then refuse to open its own file.
    """
    from autotrader.state.sqlite import SCHEMA_VERSION

    database = tmp_path / "behind.db"
    initialize_database(database)
    with connect(database) as open_connection:
        # A genuine previous-version store: the version stamp AND the table the
        # last migration added. Stamping alone leaves a database the migration
        # correctly refuses as inconsistent.
        open_connection.execute("DROP TABLE shadow_decisions")
        open_connection.execute(
            "UPDATE schema_metadata SET schema_version = ? WHERE id = 1", (SCHEMA_VERSION - 1,)
        )
        open_connection.commit()

    initialize_database(database)

    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as check:
        after = check.execute("SELECT schema_version FROM schema_metadata").fetchone()[0]
    assert after == SCHEMA_VERSION


def test_the_paper_runtime_never_opens_another_store_the_normal_way() -> None:
    """CRITICAL. The isolation is in the code path, not in an operator's care."""
    import inspect

    from test_runtime import code_without_prose

    source = code_without_prose(
        inspect.getsource(SqliteExternalSafety) + inspect.getsource(SqliteShadowParity)
    )
    assert "initialize_database" not in source
    assert "connect(" not in source.replace("sqlite3.connect(", "")
    assert source.count("mode=ro") >= 2


# ==========================================================================
# Partial fills, and the delta that follows one
# ==========================================================================


def test_a_partial_fill_leaves_the_remainder_as_the_next_delta(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. Accepted is not filled, and half-filled is not filled.

    The next bar's delta is computed against what the broker says the account
    *holds*, so a partial fill produces a smaller follow-up order rather than a
    repeat of the original one. Nothing infers a position from an accepted
    order.
    """
    broker = FakeBrokerState()
    gateway = RecordingGateway()
    runtime = build_paper(connection, gateway=gateway, broker=broker, stage="A")
    runtime.start()
    runtime.run_cycle()

    symbol, side, wanted = gateway.calls[0]
    assert side is OrderSide.BUY
    assert wanted >= Decimal(2)

    # The broker filled one share of the order and no more.
    broker.hold(symbol, Decimal(1), PRICES[symbol])
    gateway.calls.clear()
    report = runtime.run_cycle()
    runtime.stop()

    assert len(gateway.calls) == 1
    _, again_side, remainder = gateway.calls[0]
    assert again_side is OrderSide.BUY
    assert remainder == wanted - Decimal(1)
    spy = next(item for item in report.outcomes if item.symbol == "SPY")
    assert spy.actual_quantity == Decimal(1)


def test_an_over_fill_is_sold_back_down_and_never_shorted(
    connection: sqlite3.Connection,
) -> None:
    """A holding above target reduces to target. It does not cross zero."""
    broker = FakeBrokerState()
    broker.hold("SPY", Decimal(500), PRICES["SPY"])
    gateway = RecordingGateway()
    runtime = build_paper(connection, gateway=gateway, broker=broker, stage="A")
    report = runtime.run_once()

    _, side, quantity = gateway.calls[0]
    assert side is OrderSide.SELL
    assert quantity < Decimal(500)
    spy = next(item for item in report.outcomes if item.symbol == "SPY")
    assert spy.target_quantity is not None
    assert spy.actual_quantity - spy.delta_quantity == spy.target_quantity


# ==========================================================================
# The durable target record: what was wanted, and why
# ==========================================================================


def test_every_submitted_target_leaves_a_durable_record_of_why(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. `order_intents` says what was sent. This says what was wanted.

    After the fact those are different questions: "the broker was asked for 3
    shares of SPY" does not say which engine wanted it, under which sizing
    policy, against which target weight, or what the account already held.
    """
    gateway = RecordingGateway()
    runtime = build_paper(connection, gateway=gateway, stage="A")
    runtime.run_once()

    connection.row_factory = sqlite3.Row
    rows = connection.execute("SELECT * FROM equity_paper_targets").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["engine"] == "eda1"
    assert row["environment"] == "PAPER"
    assert row["sizing_policy"] == POLICY.policy_id
    assert row["sizing_config_hash"] == POLICY.config_hash()
    assert row["rollout_stage"] == "A"
    assert row["symbol"] == "SPY"
    assert row["side"] == "BUY"
    assert Decimal(row["target_weight"]) > 0
    assert Decimal(row["broker_quantity"]) == Decimal(0)
    assert Decimal(row["requested_delta"]) == Decimal(row["target_quantity"])
    assert row["bar_timestamp"]
    assert row["decided_at"]
    # Back-filled after the attempt returned, not guessed before it.
    assert row["risk_reason_code"] == "APPROVED"
    assert Decimal(row["approved_quantity"]) == Decimal(row["requested_delta"])


def test_the_target_record_shares_the_key_with_the_order_intent(
    connection: sqlite3.Connection,
) -> None:
    """One durable key for both rows, without either knowing about the other."""
    runtime = build_paper(connection, gateway=RecordingGateway(), stage="A")
    report = runtime.run_once()
    spy = next(item for item in report.outcomes if item.symbol == "SPY")

    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT client_order_id FROM equity_paper_targets WHERE symbol = 'SPY'"
    ).fetchone()
    assert spy.client_order_id is not None
    assert row["client_order_id"] == spy.client_order_id


def test_a_cycle_that_submits_nothing_writes_no_target_record(
    connection: sqlite3.Connection,
) -> None:
    """The record describes attempts, not opinions. A satisfied target is silent."""
    broker = FakeBrokerState()
    gateway = RecordingGateway()
    runtime = build_paper(connection, gateway=gateway, broker=broker, stage="A")
    runtime.start()
    runtime.run_cycle()
    symbol, _, quantity = gateway.calls[0]
    broker.hold(symbol, quantity, PRICES[symbol])
    before = connection.execute("SELECT COUNT(*) FROM equity_paper_targets").fetchone()[0]
    runtime.run_cycle()
    runtime.stop()
    after = connection.execute("SELECT COUNT(*) FROM equity_paper_targets").fetchone()[0]
    assert after == before
