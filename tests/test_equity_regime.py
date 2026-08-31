"""EDA-1 regime state and overlay: causal, deterministic, and replay-verified.

The claims under test:

*The state is causal.* The state governing session ``s`` reads completed
closes through ``s - lag`` only. A session never reads its own close; no
future close can influence an earlier state; fewer than 200 observed closes
answer DEFENSIVE rather than guessing.

*The live path equals the research path.* ``state_for_session`` - the live
resolver over a completed-closes table - answers exactly what the research
``participation_series`` answers for the same session, for every prefix of a
constructed history.

*The overlay is the research transform.* Target-position semantics, reason
tokens, regime override, and stance handoff match the deep-architecture
program's ``participation_overlay`` behaviour, including the refusal to
overlay a bar whose session has no state.

*The runtime records both engines or neither.* Every observed bar stores a V3
row, an EDA-1 row derived by replaying the whole stored V3 series, and one
comparison row - atomically. A stored EDA-1 row that stops matching the
replay stops the process.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from autotrader.decision.contract import VERSION_V3, DecisionSignal
from autotrader.equity import EquityError
from autotrader.equity.regime import (
    EDA1_ARCHITECTURE,
    EDA1_ENGINE_VERSION,
    OverlayError,
    ParticipationSpec,
    SeriesRecord,
    StateInputError,
    participation_overlay,
    participation_series,
    session_closes,
    source_stance,
    state_for_session,
)
from autotrader.equity.session import session_from_local
from autotrader.equity.shadow import (
    SHADOW_PROCESSING_ORDER,
    ShadowIntegrityError,
)
from autotrader.runtime.monitoring import RuntimeState
from autotrader.state.sqlite import connect, initialize_database
from test_equity_runtime import (
    SESSION,
    SPY,
    T_BAR,
    T_NOW,
    FakeClock,
    FakeEquityBars,
    make_equity_bars,
)
from test_equity_session import consecutive_sessions
from test_equity_shadow import (
    FakeRegimeBars,
    build_shadow,
    make_state_frame,
    stored_shadow_decisions,
)

pytestmark = pytest.mark.filterwarnings("ignore::FutureWarning")


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A fresh shadow database: full schema, zero order intents."""
    database = tmp_path / "shadow.db"
    initialize_database(database)
    with connect(database) as open_connection:
        yield open_connection


def closes_table(values: list[float], *, first: date = date(2025, 1, 6)) -> pd.DataFrame:
    """A session-closes table over consecutive weekday sessions."""
    sessions = consecutive_sessions(first, len(values))
    return pd.DataFrame(
        {
            "session": [session.session_date for session in sessions],
            "close": values,
        }
    )


def record(
    timestamp: datetime,
    signal: DecisionSignal,
    *,
    symbol: str = SPY,
    regime: str = "NEUTRAL",
    reasons: tuple[str, ...] = ("V3_TEST",),
) -> SeriesRecord:
    return SeriesRecord(
        timestamp=timestamp,
        symbol=symbol,
        signal=signal,
        score=0.25,
        confidence=0.5,
        regime=regime,
        reasons=reasons,
    )


# ==========================================================================
# The spec refuses what the research predeclaration refuses
# ==========================================================================


def test_the_spec_defaults_are_the_research_convention() -> None:
    spec = ParticipationSpec()
    assert spec.sma_sessions == 200
    assert spec.calm_threshold == -0.05
    assert spec.lag_sessions == 1


def test_the_spec_refuses_impossible_parameters() -> None:
    with pytest.raises(StateInputError):
        ParticipationSpec(sma_sessions=1)
    with pytest.raises(StateInputError):
        ParticipationSpec(calm_threshold=0.0)
    with pytest.raises(StateInputError):
        ParticipationSpec(calm_threshold=-1.0)
    with pytest.raises(StateInputError, match="own close"):
        ParticipationSpec(lag_sessions=0)


# ==========================================================================
# Session closes: the last observed bar of each session
# ==========================================================================


def test_session_closes_take_the_last_observed_bar_per_session() -> None:
    """Two sessions across a weekend, three bars each: the 15:45 close wins."""
    friday = session_from_local(
        date(2026, 3, 6),
        datetime(2026, 3, 6, 9, 30),
        datetime(2026, 3, 6, 16, 0),
    )
    monday = session_from_local(
        date(2026, 3, 9),
        datetime(2026, 3, 9, 9, 30),
        datetime(2026, 3, 9, 16, 0),
    )
    stamps = [
        friday.open_utc,
        friday.close_utc - timedelta(minutes=15),
        monday.open_utc,
        monday.close_utc - timedelta(minutes=15),
    ]
    frame = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp(stamp) for stamp in stamps],
            "close": [10.0, 11.0, 12.0, 13.0],
        }
    )
    closes = session_closes(frame)

    assert list(closes["session"]) == [date(2026, 3, 6), date(2026, 3, 9)]
    assert list(closes["close"]) == [11.0, 13.0]
    # The Friday and Monday sessions straddle the US spring-forward change:
    # 20:45 UTC and 19:45 UTC are both 15:45 New York, and both bars land on
    # their own session's date rather than bleeding across the transition.
    assert stamps[1].hour == 20 and stamps[3].hour == 19


def test_an_early_close_contributes_its_real_last_bar() -> None:
    half_day = session_from_local(
        date(2026, 11, 27),
        datetime(2026, 11, 27, 9, 30),
        datetime(2026, 11, 27, 13, 0),
    )
    frame = pd.DataFrame(
        {
            "timestamp": [
                pd.Timestamp(half_day.open_utc),
                pd.Timestamp(half_day.close_utc - timedelta(minutes=15)),
            ],
            "close": [20.0, 21.0],
        }
    )
    closes = session_closes(frame)
    assert list(closes["close"]) == [21.0]


def test_an_empty_frame_is_refused() -> None:
    with pytest.raises(StateInputError):
        session_closes(pd.DataFrame({"timestamp": [], "close": []}))


# ==========================================================================
# The participation state is lagged, warm-up-safe, and future-blind
# ==========================================================================


def test_fewer_closes_than_the_average_needs_answer_defensive() -> None:
    spec = ParticipationSpec(sma_sessions=5, calm_threshold=-0.05, lag_sessions=1)
    series = participation_series(closes_table([100.0, 101.0, 102.0, 103.0]), spec)
    assert not series["participate"].any()


def test_a_session_never_reads_its_own_close() -> None:
    """A collapse on session i shows up in the state governing i+1, not i."""
    spec = ParticipationSpec(sma_sessions=2, calm_threshold=-0.05, lag_sessions=1)
    series = participation_series(closes_table([100.0, 101.0, 102.0, 50.0]), spec)
    # Session 3's own close crashed, but its governing state read session 2.
    assert bool(series["participate"].iloc[3]) is True
    assert series["info_close"].iloc[3] == 102.0


def test_no_future_close_influences_an_earlier_state() -> None:
    spec = ParticipationSpec(sma_sessions=3, calm_threshold=-0.05, lag_sessions=1)
    values = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0]
    base = participation_series(closes_table(values), spec)

    probe = 4
    perturbed_values = values[: probe + 1] + [value * 1.5 for value in values[probe + 1 :]]
    perturbed = participation_series(closes_table(perturbed_values), spec)

    unchanged = probe + spec.lag_sessions
    for i in range(unchanged + 1):
        assert bool(base["participate"].iloc[i]) == bool(perturbed["participate"].iloc[i])
        original = base["info_close"].iloc[i]
        shifted = perturbed["info_close"].iloc[i]
        assert (pd.isna(original) and pd.isna(shifted)) or original == shifted
    # Non-vacuous: the perturbation does change something later.
    assert perturbed["info_close"].iloc[unchanged + 1] != base["info_close"].iloc[unchanged + 1]


def test_participation_needs_both_trend_and_calm() -> None:
    spec = ParticipationSpec(sma_sessions=2, calm_threshold=-0.05, lag_sessions=1)
    # Session 3 reads session 2: close 90 is 10% off the 100 peak - defensive
    # even though it is above the (95, 90) average? 92.5 < 90 is false, so the
    # trend leg fails too; session 4 reads 96: above the (90, 96)=93 average
    # but still 4% below its 100 peak - participation returns.
    series = participation_series(closes_table([100.0, 95.0, 90.0, 96.0, 97.0]), spec)
    assert bool(series["participate"].iloc[3]) is False
    assert bool(series["participate"].iloc[4]) is True
    assert series["info_drawdown"].iloc[4] == pytest.approx(-0.04)


def test_the_drawdown_boundary_is_strict() -> None:
    """Exactly -5% off the peak is not calm; the research rule is strictly greater."""
    spec = ParticipationSpec(sma_sessions=2, calm_threshold=-0.05, lag_sessions=1)
    series = participation_series(closes_table([100.0, 90.0, 95.0, 95.0]), spec)
    # Session 3 reads session 2: close 95.0, peak 100.0, drawdown exactly -0.05.
    assert series["info_drawdown"].iloc[3] == pytest.approx(-0.05)
    assert bool(series["participate"].iloc[3]) is False


# ==========================================================================
# The live resolver equals the research series, prefix by prefix
# ==========================================================================


def test_state_for_session_equals_the_research_series_on_every_prefix() -> None:
    spec = ParticipationSpec(sma_sessions=4, calm_threshold=-0.05, lag_sessions=1)
    values = [100.0, 104.0, 99.0, 103.0, 108.0, 102.0, 95.0, 101.0, 109.0, 110.0]
    table = closes_table(values)
    series = participation_series(table, spec)

    for i in range(1, len(values)):
        prefix = table.iloc[:i].reset_index(drop=True)
        resolved = state_for_session(prefix, spec, session_date=table["session"].iloc[i])
        assert resolved.participate == bool(series["participate"].iloc[i]), i
        if resolved.info_sma is not None:
            assert resolved.info_sma == pytest.approx(series["info_sma"].iloc[i])
            assert resolved.info_close == pytest.approx(series["info_close"].iloc[i])
            assert resolved.info_drawdown == pytest.approx(series["info_drawdown"].iloc[i])


def test_the_resolver_refuses_closes_from_the_governing_session_or_later() -> None:
    spec = ParticipationSpec(sma_sessions=2, calm_threshold=-0.05, lag_sessions=1)
    table = closes_table([100.0, 101.0, 102.0])
    with pytest.raises(StateInputError, match="strictly before"):
        state_for_session(table, spec, session_date=table["session"].iloc[2])


def test_warm_up_states_carry_no_measured_average() -> None:
    spec = ParticipationSpec(sma_sessions=5, calm_threshold=-0.05, lag_sessions=1)
    table = closes_table([100.0, 101.0])
    resolved = state_for_session(table, spec, session_date=date(2026, 1, 12))
    assert resolved.participate is False
    assert resolved.info_sma is None
    assert resolved.sessions_observed == 2


# ==========================================================================
# The overlay is the research transform
# ==========================================================================


def bar_at(session_day: date, *, bar: int = 0) -> datetime:
    """A regular-session bar timestamp inside `session_day` (EST winter clock)."""
    return datetime(
        session_day.year, session_day.month, session_day.day, 14, 30, tzinfo=UTC
    ) + timedelta(minutes=15 * bar)


def test_participation_forces_entry_and_hands_back_to_the_source() -> None:
    days = [session.session_date for session in consecutive_sessions(date(2025, 1, 6), 3)]
    records = [
        record(bar_at(days[0]), DecisionSignal.HOLD),
        record(bar_at(days[1]), DecisionSignal.HOLD),
        record(bar_at(days[2]), DecisionSignal.HOLD),
    ]
    participate = {days[0]: False, days[1]: True, days[2]: False}
    derived = participation_overlay(records, participate)

    assert [item.signal for item in derived] == [
        DecisionSignal.HOLD,
        DecisionSignal.BUY,
        DecisionSignal.SELL,
    ]
    assert derived[1].reasons == (f"{EDA1_ARCHITECTURE}_PARTICIPATE_ENTER",)
    assert derived[1].regime == "PARTICIPATE"
    # Hand-back: the source was flat, so participation's end exits. The exit
    # carries the source's own reasons - V3 explains the defensive stance.
    assert derived[2].reasons == records[2].reasons
    assert derived[2].regime == records[2].regime


def test_a_long_source_keeps_its_position_when_participation_ends() -> None:
    days = [session.session_date for session in consecutive_sessions(date(2025, 1, 6), 3)]
    records = [
        record(bar_at(days[0]), DecisionSignal.BUY),
        record(bar_at(days[1]), DecisionSignal.HOLD),
        record(bar_at(days[2]), DecisionSignal.HOLD),
    ]
    participate = {days[0]: True, days[1]: True, days[2]: False}
    derived = participation_overlay(records, participate)

    # Entered under participation, and the source is long throughout - no exit.
    assert [item.signal for item in derived] == [
        DecisionSignal.BUY,
        DecisionSignal.HOLD,
        DecisionSignal.HOLD,
    ]
    assert derived[0].reasons == (f"{EDA1_ARCHITECTURE}_PARTICIPATE_ENTER",)
    assert derived[2].regime == records[2].regime


def test_the_defensive_regime_is_the_sources_verbatim() -> None:
    days = [session.session_date for session in consecutive_sessions(date(2025, 1, 6), 2)]
    records = [
        record(bar_at(days[0]), DecisionSignal.BUY, regime="TRENDING"),
        record(bar_at(days[1]), DecisionSignal.SELL, regime="VOLATILE"),
    ]
    participate = {days[0]: False, days[1]: False}
    derived = participation_overlay(records, participate)

    assert [item.signal for item in derived] == [DecisionSignal.BUY, DecisionSignal.SELL]
    assert derived[0].regime == "TRENDING"
    assert derived[0].reasons == records[0].reasons
    assert derived[1].regime == "VOLATILE"
    assert derived[1].reasons == records[1].reasons
    assert derived[0].score == records[0].score
    assert derived[0].confidence == records[0].confidence


def test_bars_within_one_session_share_one_state() -> None:
    days = [session.session_date for session in consecutive_sessions(date(2025, 1, 6), 2)]
    records = [
        record(bar_at(days[0], bar=0), DecisionSignal.HOLD),
        record(bar_at(days[0], bar=1), DecisionSignal.HOLD),
        record(bar_at(days[1], bar=0), DecisionSignal.HOLD),
    ]
    participate = {days[0]: True, days[1]: True}
    derived = participation_overlay(records, participate)
    # Entry on the session's first bar, then held: the state cannot flip
    # mid-session because it is keyed by session, not by bar.
    assert [item.signal for item in derived] == [
        DecisionSignal.BUY,
        DecisionSignal.HOLD,
        DecisionSignal.HOLD,
    ]
    assert derived[1].reasons == (f"{EDA1_ARCHITECTURE}_HOLD",)


def test_a_bar_without_a_session_state_is_refused() -> None:
    days = [session.session_date for session in consecutive_sessions(date(2025, 1, 6), 2)]
    records = [
        record(bar_at(days[0]), DecisionSignal.HOLD),
        record(bar_at(days[1]), DecisionSignal.HOLD),
    ]
    with pytest.raises(OverlayError, match="No participation state"):
        participation_overlay(records, {days[0]: False})


def test_source_stance_reconstruction() -> None:
    days = [session.session_date for session in consecutive_sessions(date(2025, 1, 6), 4)]
    records = [
        record(bar_at(days[0]), DecisionSignal.HOLD),
        record(bar_at(days[1]), DecisionSignal.BUY),
        record(bar_at(days[2]), DecisionSignal.HOLD),
        record(bar_at(days[3]), DecisionSignal.SELL),
    ]
    assert source_stance(records) == [0, 1, 1, 0]


def test_an_empty_series_is_refused() -> None:
    with pytest.raises(OverlayError):
        participation_overlay([], {})


# ==========================================================================
# The runtime records both engines, atomically and replay-verified
# ==========================================================================

#: 220 completed sessions of steadily rising closes: above the 200-session
#: average, at the trailing peak - the state governing SESSION participates.
RISING_SESSIONS = consecutive_sessions(date(2025, 9, 1), 220)
RISING_FRAME_CLOSES = [100.0 + 0.5 * i for i in range(len(RISING_SESSIONS))]


def rising_regime() -> FakeRegimeBars:
    return FakeRegimeBars(make_state_frame(RISING_SESSIONS, closes=RISING_FRAME_CLOSES))


def stored_regime_states(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    return connection.execute("SELECT * FROM shadow_regime_state ORDER BY session_date").fetchall()


def stored_side_by_side(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    return connection.execute(
        "SELECT * FROM shadow_side_by_side ORDER BY symbol, bar_timestamp"
    ).fetchall()


def test_the_regime_state_is_resolved_once_and_persisted(
    connection: sqlite3.Connection,
) -> None:
    regime = FakeRegimeBars()
    runtime = build_shadow(connection, regime=regime)
    runtime.start()
    runtime.run_cycle()
    runtime.run_cycle()
    runtime.stop()

    assert len(regime.calls) == 1
    (before, _, sessions) = regime.calls[0]
    assert before == SESSION.session_date
    [row] = stored_regime_states(connection)
    assert row["session_date"] == SESSION.session_date.isoformat()
    assert row["participate"] == 0  # 17 observed sessions: warm-up DEFENSIVE
    assert row["info_sma"] is None
    assert row["sessions_observed"] == 17
    assert row["sma_sessions"] == 200
    assert row["calm_threshold"] == -0.05
    assert row["lag_sessions"] == 1
    assert row["reference_symbol"] == "SPY"


def test_a_restart_reuses_the_stored_state(tmp_path: Path) -> None:
    database = tmp_path / "shadow.db"
    initialize_database(database)
    with connect(database) as connection:
        first = FakeRegimeBars()
        build_shadow(connection, regime=first).run_once()
        assert len(first.calls) == 1
    with connect(database) as connection:
        second = FakeRegimeBars(error=AssertionError("refetched a stored state"))
        build_shadow(connection, regime=second).run_once()
        assert second.calls == []


def test_a_state_fetch_failure_records_nothing_and_is_not_fatal(
    connection: sqlite3.Connection,
) -> None:
    regime = FakeRegimeBars(error=EquityError("provider unavailable"))
    runtime = build_shadow(connection, regime=regime)
    report = runtime.run_once()

    assert report.error is not None
    assert not report.fatal
    assert stored_shadow_decisions(connection) == []
    assert stored_side_by_side(connection) == []
    assert runtime.state is not RuntimeState.FAILED


def test_every_bar_stores_v3_and_a_replay_verified_eda1_row(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. Stored EDA-1 equals an independent overlay recomputation."""
    runtime = build_shadow(connection, regime=rising_regime())
    runtime.run_once()

    rows = stored_shadow_decisions(connection)
    v3_rows = [row for row in rows if row["engine_version"] == VERSION_V3]
    eda1_rows = [row for row in rows if row["engine_version"] == EDA1_ENGINE_VERSION]
    assert len(v3_rows) == len(SHADOW_PROCESSING_ORDER)
    assert len(eda1_rows) == len(SHADOW_PROCESSING_ORDER)

    participate = {SESSION.session_date: True}
    for v3_row, eda1_row in zip(v3_rows, eda1_rows, strict=True):
        source = SeriesRecord(
            timestamp=datetime.fromisoformat(v3_row["bar_timestamp"]),
            symbol=v3_row["symbol"],
            signal=DecisionSignal(v3_row["signal"]),
            score=v3_row["score"],
            confidence=v3_row["confidence"],
            regime=v3_row["regime"],
            reasons=tuple(v3_row["reasons"].split(" ")),
        )
        [direct] = participation_overlay([source], participate)
        assert eda1_row["symbol"] == v3_row["symbol"]
        assert eda1_row["signal"] == direct.signal.value
        assert eda1_row["score"] == direct.score
        assert eda1_row["confidence"] == direct.confidence
        assert eda1_row["regime"] == direct.regime
        assert eda1_row["reasons"] == " ".join(direct.reasons)
        assert eda1_row["designation"] == "NOT_EXECUTED"
        assert eda1_row["client_order_id"] is None


def test_a_participating_session_disagrees_with_a_flat_v3(
    connection: sqlite3.Connection,
) -> None:
    """V3 answers HOLD on short fixtures; EDA-1 enters. The record shows both."""
    runtime = build_shadow(connection, regime=rising_regime())
    runtime.run_once()

    for row in stored_side_by_side(connection):
        assert row["participate"] == 1
        assert row["v3_signal"] == "HOLD"
        assert row["v3_stance"] == 0
        assert row["eda1_signal"] == "BUY"
        assert row["eda1_stance"] == 1
        assert row["signals_agree"] == 0
        assert row["stances_agree"] == 0
        assert row["reference_close"] == 500.0
        assert row["session_date"] == SESSION.session_date.isoformat()


def test_a_defensive_session_mirrors_v3_exactly(connection: sqlite3.Connection) -> None:
    runtime = build_shadow(connection)  # default: 17 sessions, warm-up DEFENSIVE
    runtime.run_once()

    for row in stored_side_by_side(connection):
        assert row["participate"] == 0
        assert row["eda1_signal"] == row["v3_signal"]
        assert row["eda1_stance"] == row["v3_stance"]
        assert row["signals_agree"] == 1
        assert row["stances_agree"] == 1


def test_a_second_bar_extends_the_replay_and_holds_the_position(
    connection: sqlite3.Connection,
) -> None:
    bars = FakeEquityBars({symbol: make_equity_bars(symbol) for symbol in SHADOW_PROCESSING_ORDER})
    clock = FakeClock()
    runtime = build_shadow(connection, bars=bars, regime=rising_regime(), clock=clock)
    runtime.start()
    runtime.run_cycle()

    next_bar = T_BAR + timedelta(minutes=15)
    bars.frames = {
        symbol: make_equity_bars(symbol, last_bar_start=next_bar)
        for symbol in SHADOW_PROCESSING_ORDER
    }
    clock.now = T_NOW + timedelta(minutes=15)
    runtime.run_cycle()
    runtime.stop()

    spy_rows = [
        row
        for row in stored_shadow_decisions(connection)
        if row["symbol"] == SPY and row["engine_version"] == EDA1_ENGINE_VERSION
    ]
    assert [row["signal"] for row in spy_rows] == ["BUY", "HOLD"]
    assert spy_rows[1]["reasons"] == f"{EDA1_ARCHITECTURE}_HOLD"
    spy_pairs = [row for row in stored_side_by_side(connection) if row["symbol"] == SPY]
    assert [row["eda1_stance"] for row in spy_pairs] == [1, 1]
    assert [row["v3_stance"] for row in spy_pairs] == [0, 0]


def test_a_tampered_eda1_row_stops_the_shadow(connection: sqlite3.Connection) -> None:
    """CRITICAL. The stored challenger series cannot silently drift from the replay."""
    bars = FakeEquityBars({symbol: make_equity_bars(symbol) for symbol in SHADOW_PROCESSING_ORDER})
    clock = FakeClock()
    runtime = build_shadow(connection, bars=bars, regime=rising_regime(), clock=clock)
    runtime.start()
    runtime.run_cycle()

    connection.execute(
        "UPDATE shadow_decisions SET signal = 'SELL' WHERE symbol = ? AND engine_version = ?",
        (SPY, EDA1_ENGINE_VERSION),
    )
    connection.commit()
    before = len(stored_shadow_decisions(connection))

    next_bar = T_BAR + timedelta(minutes=15)
    bars.frames = {
        symbol: make_equity_bars(symbol, last_bar_start=next_bar)
        for symbol in SHADOW_PROCESSING_ORDER
    }
    clock.now = T_NOW + timedelta(minutes=15)
    with pytest.raises(ShadowIntegrityError, match="no longer matches the transform"):
        runtime.run_cycle()
    # The refused bar wrote nothing: the whole transaction rolled back.
    assert len(stored_shadow_decisions(connection)) == before


def test_a_database_with_a_foreign_regime_spec_is_refused(
    connection: sqlite3.Connection,
) -> None:
    build_shadow(connection).run_once()
    connection.execute("UPDATE shadow_regime_state SET sma_sessions = 150")
    connection.commit()

    runtime = build_shadow(connection)
    with pytest.raises(ShadowIntegrityError, match="another router"):
        runtime.start()


def test_the_recorder_still_reaches_no_execution_module(
    connection: sqlite3.Connection,
) -> None:
    """The side-by-side recorder's object graph holds rows-and-specs, nothing more."""
    runtime = build_shadow(connection)
    recorder = runtime._recorder  # noqa: SLF001 - the property under test
    for name, value in vars(recorder).items():
        module = type(value).__module__
        assert not module.startswith("autotrader.execution"), (name, module)
        assert not hasattr(value, "submit_order"), name
