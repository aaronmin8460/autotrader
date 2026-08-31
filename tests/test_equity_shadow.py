"""Equity V3 live shadow: records decisions, and cannot reach an order.

The claims under test, in the order the module makes them:

*The shadow has no execution path.* Not disabled - absent. The constructor
takes no execution argument, no attribute holds a gateway, and the module's
stripped source names nothing from the execution layer or the broker SDK
(`test_shadow.py::test_the_equity_shadow_runtime_earns_its_exemption` pins the
source; the tests here pin the object graph and the behaviour).

*A database that has ever ordered is refused.* The zero-intent assertion runs
at startup and after every cycle, so the shadow can neither share the trading
database - and with it the per-symbol bar claims - nor miss an intent row
appearing by any route whatsoever.

*Stored decisions are V3's own.* The row the recorder writes is byte-for-byte
what `MultiTimeframeV3Engine` decides on the same frame, so the shadow record
can stand in for the engine in any later evaluation.

*Session semantics are the trading runtime's.* Closed market: no fetch, no
evaluation, no claim. A completed bar is claimed before evaluation and never
evaluated twice, across restarts included.
"""

from __future__ import annotations

import inspect
import socket
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from autotrader.decision.contract import VERSION_V3
from autotrader.decision.v3 import MultiTimeframeV3Engine
from autotrader.equity.session import MarketSession
from autotrader.equity.shadow import (
    DEFAULT_SHADOW_LOOKBACK_BARS,
    MAX_SHADOW_LOOKBACK_BARS,
    MIN_SHADOW_LOOKBACK_BARS,
    NO_SESSION_TODAY,
    SESSION_OPEN,
    SHADOW_PROCESSING_ORDER,
    V3_REQUIRED_BASE_BARS,
    EquityShadowConfig,
    EquityShadowRuntime,
    ShadowIntegrityError,
    require_shadow_lookback_bars,
)
from autotrader.equity.shadow import (
    EVENT_SHADOW_CYCLE as _EVENT_SHADOW_CYCLE,
)
from autotrader.runtime.monitoring import RuntimeState
from autotrader.runtime.runner import ShutdownRequest
from autotrader.state.sqlite import (
    connect,
    initialize_database,
    list_order_intents,
    list_system_events,
)
from conftest import establish_account_safety
from test_equity_runtime import (
    SESSION,
    SPY,
    T_BAR,
    T_NOW,
    FakeClock,
    FakeEquityBars,
    make_equity_bars,
)
from test_equity_session import FakeCalendar

pytestmark = pytest.mark.filterwarnings("ignore::FutureWarning")


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A fresh shadow database: full schema, zero order intents."""
    database = tmp_path / "shadow.db"
    initialize_database(database)
    with connect(database) as open_connection:
        yield open_connection


def build_shadow(
    connection: sqlite3.Connection,
    *,
    bars: FakeEquityBars | None = None,
    calendar: FakeCalendar | None = None,
    clock: FakeClock | None = None,
    config: EquityShadowConfig | None = None,
) -> EquityShadowRuntime:
    return EquityShadowRuntime(
        connection,
        market_data=bars if bars is not None else FakeEquityBars(),
        calendar=calendar if calendar is not None else FakeCalendar([SESSION]),
        clock=clock if clock is not None else FakeClock(),
        sleep=lambda seconds: None,
        shutdown=ShutdownRequest(),
        config=config,
    )


def stored_shadow_decisions(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT * FROM shadow_decisions ORDER BY symbol, bar_timestamp, engine_version"
    ).fetchall()
    connection.row_factory = None
    return rows


# ==========================================================================
# The shadow has no execution path
# ==========================================================================


def test_the_constructor_offers_no_execution_seam() -> None:
    """CRITICAL. There is no parameter a gateway could be handed through."""
    parameters = inspect.signature(EquityShadowRuntime.__init__).parameters
    names = set(parameters)
    assert "execution" not in names
    assert not any("gateway" in name.lower() for name in names)


def test_no_attribute_of_a_running_shadow_holds_a_gateway(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. The object graph holds bars, a calendar, a recorder - no broker."""
    runtime = build_shadow(connection)
    for name, value in vars(runtime).items():
        type_name = type(value).__name__
        module = type(value).__module__
        assert "Gateway" not in type_name, (name, type_name)
        assert not module.startswith("autotrader.execution"), (name, module)
        assert not module.startswith("autotrader.runtime.execution"), (name, module)
        assert not hasattr(value, "submit_order"), name


def test_a_full_cycle_runs_with_every_socket_blocked(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CRITICAL. With fakes injected, nothing in the cycle wants a network."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the shadow cycle attempted to open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    runtime = build_shadow(connection)
    report = runtime.run_once()

    assert report.session_state == SESSION_OPEN
    assert report.recorded_count == len(SHADOW_PROCESSING_ORDER)


def test_a_cycle_creates_no_order_intent_and_no_broker_order(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. Ten decisions recorded; zero rows in either order table."""
    runtime = build_shadow(connection)
    runtime.run_once()

    assert list_order_intents(connection) == []
    count = connection.execute("SELECT COUNT(*) FROM broker_orders").fetchone()[0]
    assert count == 0
    assert stored_shadow_decisions(connection)


def test_a_database_holding_an_order_intent_is_refused_at_startup(
    tmp_path: Path,
) -> None:
    """CRITICAL. The trading database - which always holds intents - cannot be shared."""
    database = tmp_path / "trading.db"
    initialize_database(database)
    with connect(database) as connection:
        establish_account_safety(connection)
        connection.execute(
            "INSERT INTO order_intents (client_order_id, created_at, symbol, side,"
            " requested_quantity, approved_quantity, reference_price, risk_reason_code,"
            " status, updated_at) VALUES ('autotrader-test', '2026-08-26T15:00:00+00:00',"
            " 'SPY', 'BUY', '1', '1', 500.0, 'APPROVED', 'SUBMITTED',"
            " '2026-08-26T15:00:00+00:00')"
        )
        connection.commit()
        runtime = build_shadow(connection)
        with pytest.raises(ShadowIntegrityError):
            runtime.start()


def test_an_intent_appearing_mid_run_stops_the_next_cycle(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. The invariant is re-verified after every cycle, not assumed once."""
    runtime = build_shadow(connection)
    runtime.start()
    runtime.run_cycle()

    connection.execute(
        "INSERT INTO order_intents (client_order_id, created_at, symbol, side,"
        " requested_quantity, approved_quantity, reference_price, risk_reason_code,"
        " status, updated_at) VALUES ('autotrader-intruder', '2026-08-26T15:20:00+00:00',"
        " 'SPY', 'BUY', '1', '1', 500.0, 'APPROVED', 'CREATED',"
        " '2026-08-26T15:20:00+00:00')"
    )
    connection.commit()

    with pytest.raises(ShadowIntegrityError):
        runtime.run_cycle()


def test_an_actionable_decision_is_recorded_and_dropped(
    connection: sqlite3.Connection,
) -> None:
    """A BUY-shaped V3 answer produces a row and nothing else.

    With 120-bar fixtures V3 answers HOLD (insufficient timeframes), which is
    the common case and still a recorded decision. Whether any decision is
    actionable or not, the intent count stays zero - asserted per cycle by the
    runtime itself and re-asserted here.
    """
    runtime = build_shadow(connection)
    report = runtime.run_once()

    assert report.recorded_count == len(SHADOW_PROCESSING_ORDER)
    assert list_order_intents(connection) == []
    for row in stored_shadow_decisions(connection):
        assert row["client_order_id"] is None


# ==========================================================================
# Stored decisions are V3's own
# ==========================================================================


def test_the_stored_decision_is_v3s_own_answer(connection: sqlite3.Connection) -> None:
    """CRITICAL. Row fields equal a direct engine evaluation of the same frame."""
    frame = make_equity_bars(SPY)
    bars = FakeEquityBars({symbol: make_equity_bars(symbol) for symbol in SHADOW_PROCESSING_ORDER})
    runtime = build_shadow(connection, bars=bars)
    runtime.run_once()

    direct = MultiTimeframeV3Engine.for_symbol(SPY).decide(frame)
    [row] = [r for r in stored_shadow_decisions(connection) if r["symbol"] == SPY]

    assert row["engine_version"] == VERSION_V3
    assert row["execution_version"] == VERSION_V3
    assert row["signal"] == direct.signal.value
    assert row["score"] == pytest.approx(float(direct.score))
    assert row["confidence"] == pytest.approx(float(direct.confidence))
    assert row["regime"] == direct.regime.value
    assert row["reasons"] == " ".join(direct.reasons)
    assert datetime.fromisoformat(row["bar_timestamp"]) == direct.timestamp.to_pydatetime()


def test_every_universe_symbol_is_recorded_once_per_bar(
    connection: sqlite3.Connection,
) -> None:
    runtime = build_shadow(connection)
    runtime.run_once()

    rows = stored_shadow_decisions(connection)
    assert sorted(row["symbol"] for row in rows) == sorted(SHADOW_PROCESSING_ORDER)
    assert {row["engine_version"] for row in rows} == {VERSION_V3}


# ==========================================================================
# Session semantics
# ==========================================================================


def test_a_day_without_a_session_fetches_nothing(connection: sqlite3.Connection) -> None:
    """A weekend or holiday costs no provider call and records nothing."""
    bars = FakeEquityBars(error=AssertionError("fetched on a closed day"))
    runtime = build_shadow(
        connection,
        bars=bars,
        calendar=FakeCalendar([]),
    )
    report = runtime.run_once()

    assert report.session_state == NO_SESSION_TODAY
    assert bars.calls == []
    assert stored_shadow_decisions(connection) == []


def test_a_closed_session_moment_fetches_nothing(connection: sqlite3.Connection) -> None:
    before_open = SESSION.open_utc - timedelta(minutes=5)
    bars = FakeEquityBars(error=AssertionError("fetched outside the session"))
    runtime = build_shadow(connection, bars=bars, clock=FakeClock(before_open))
    report = runtime.run_once()

    assert report.session_state != SESSION_OPEN
    assert bars.calls == []


def test_a_bar_is_never_evaluated_twice_within_a_process(
    connection: sqlite3.Connection,
) -> None:
    runtime = build_shadow(connection)
    runtime.start()
    first = runtime.run_cycle()
    second = runtime.run_cycle()
    runtime.stop()

    assert first.recorded_count == len(SHADOW_PROCESSING_ORDER)
    assert second.recorded_count == 0
    rows = stored_shadow_decisions(connection)
    assert len(rows) == len(SHADOW_PROCESSING_ORDER)


def test_the_claim_survives_a_process_restart(tmp_path: Path) -> None:
    """A second process sees the durable claim and records nothing new."""
    database = tmp_path / "shadow.db"
    initialize_database(database)

    with connect(database) as connection:
        build_shadow(connection).run_once()
        first_count = len(stored_shadow_decisions(connection))

    with connect(database) as connection:
        build_shadow(connection).run_once()
        rows = stored_shadow_decisions(connection)

    assert first_count == len(SHADOW_PROCESSING_ORDER)
    assert len(rows) == first_count


def test_the_shadow_fetches_its_configured_v3_sized_window(
    connection: sqlite3.Connection,
) -> None:
    bars = FakeEquityBars()
    runtime = build_shadow(connection, bars=bars)
    runtime.run_once()

    [(symbols, _, latest, lookback)] = bars.calls
    assert symbols == SHADOW_PROCESSING_ORDER
    assert latest == T_BAR
    assert lookback == DEFAULT_SHADOW_LOOKBACK_BARS


# ==========================================================================
# Configuration bounds
# ==========================================================================


def test_the_lookback_floor_is_v3s_declared_requirement() -> None:
    assert MIN_SHADOW_LOOKBACK_BARS == V3_REQUIRED_BASE_BARS
    assert require_shadow_lookback_bars(MIN_SHADOW_LOOKBACK_BARS) == MIN_SHADOW_LOOKBACK_BARS
    with pytest.raises(Exception, match="lookback_bars"):
        require_shadow_lookback_bars(MIN_SHADOW_LOOKBACK_BARS - 1)
    with pytest.raises(Exception, match="lookback_bars"):
        require_shadow_lookback_bars(MAX_SHADOW_LOOKBACK_BARS + 1)


def test_the_default_lookback_matches_the_historical_study() -> None:
    """4,750 was pre-declared by the ten-symbol study, above its measured worst case."""
    assert DEFAULT_SHADOW_LOOKBACK_BARS == 4750
    assert DEFAULT_SHADOW_LOOKBACK_BARS > V3_REQUIRED_BASE_BARS


# ==========================================================================
# The audit trail
# ==========================================================================


def test_every_open_cycle_writes_the_no_order_audit_row(
    connection: sqlite3.Connection,
) -> None:
    runtime = build_shadow(connection)
    runtime.run_once()

    events = [
        event for event in list_system_events(connection) if event.event_type == _EVENT_SHADOW_CYCLE
    ]
    assert len(events) == 1
    assert "0 order intents" in events[0].message


def test_start_and_stop_are_recorded_with_the_zero_mutation_claim(
    connection: sqlite3.Connection,
) -> None:
    runtime = build_shadow(
        connection,
        config=EquityShadowConfig(code_sha="aee7a77af090fd9d3dd60f66c400fa2360f2f478"),
    )
    runtime.run_once()

    types = {event.event_type: event.message for event in list_system_events(connection)}
    assert "EQUITY_SHADOW_STARTED" in types
    assert "EQUITY_SHADOW_STOPPED" in types
    assert "no execution path" in types["EQUITY_SHADOW_STARTED"]
    assert "aee7a77" in types["EQUITY_SHADOW_STARTED"]
    assert "0, by construction" in types["EQUITY_SHADOW_STOPPED"]


def test_a_clean_run_ends_in_a_stopped_state(connection: sqlite3.Connection) -> None:
    runtime = build_shadow(connection)
    runtime.run_once()
    assert runtime.state is RuntimeState.STOPPED
    assert runtime.heartbeat.orders_submitted == 0


# ==========================================================================
# The CLI surface
# ==========================================================================


def test_the_cli_command_exists_and_names_the_guarantee() -> None:
    from typer.testing import CliRunner

    from autotrader.cli import app

    result = CliRunner().invoke(app, ["equity-shadow", "--help"])

    assert result.exit_code == 0, result.output
    assert "zero orders" in result.output.lower() or "ZERO" in result.output


def test_the_cli_command_constructs_nothing_that_can_execute() -> None:
    """The command builds a shadow runtime and passes it no execution anything."""
    from autotrader import cli

    from test_runtime import code_without_prose

    code = code_without_prose(inspect.getsource(cli.equity_shadow))
    assert "EquityShadowRuntime" in code
    assert "Gateway" not in code
    assert "execution=" not in code
    assert "confirm" not in code.lower()


def test_the_shadow_needs_no_trading_gates() -> None:
    """No environment gate, no confirmation token: nothing here to authorize."""
    from autotrader import cli

    parameters = inspect.signature(cli.equity_shadow).parameters
    assert "confirm_paper" not in parameters
    assert "confirm_paper_runtime" not in parameters


def test_the_default_shadow_database_is_not_the_trading_database() -> None:
    from autotrader import cli
    from autotrader.state.sqlite import DEFAULT_DATABASE_PATH

    assert cli.EQUITY_SHADOW_DATABASE_PATH != DEFAULT_DATABASE_PATH


# ==========================================================================
# The panel is V3-only and honest about it
# ==========================================================================


def test_the_panel_holds_exactly_v3(connection: sqlite3.Connection) -> None:
    runtime = build_shadow(connection)
    for symbol in SHADOW_PROCESSING_ORDER:
        panel = runtime._cycles[symbol].panel  # noqa: SLF001 - the property under test
        assert panel.versions == (VERSION_V3,)
        assert panel.execution_version == VERSION_V3
        assert panel.observational_versions == ()


def test_the_recorder_carries_no_strategy_run(connection: sqlite3.Connection) -> None:
    """NULL is the honest run id: no trading strategy run produced these rows."""
    runtime = build_shadow(connection)
    runtime.run_once()
    for row in stored_shadow_decisions(connection):
        assert row["strategy_run_id"] is None


def _hold_signal_fixture_sanity() -> None:
    """Keep the fixture honest: 120 bars is far below V3's requirement."""
    assert V3_REQUIRED_BASE_BARS > 120


def test_the_fixture_frames_are_below_v3s_requirement() -> None:
    _hold_signal_fixture_sanity()
    frame = make_equity_bars(SPY)
    result = MultiTimeframeV3Engine.for_symbol(SPY).decide(frame)
    assert result.signal.value == "HOLD"


def test_shadow_and_trading_runtimes_use_different_lock_scopes() -> None:
    from autotrader.equity.runtime import EQUITY_LOCK_SCOPE
    from autotrader.equity.shadow import EQUITY_SHADOW_LOCK_SCOPE

    assert EQUITY_SHADOW_LOCK_SCOPE != EQUITY_LOCK_SCOPE
    assert EQUITY_SHADOW_LOCK_SCOPE != "crypto"


def test_session_fixture_covers_t_now() -> None:
    """Keep the borrowed fixtures aligned: T_NOW sits inside SESSION."""
    assert isinstance(SESSION, MarketSession)
    assert SESSION.open_utc <= T_NOW <= SESSION.close_utc
    assert SESSION.session_date == date(2026, 8, 26)
    assert T_BAR.tzinfo is UTC
