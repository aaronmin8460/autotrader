"""The A1-B U30 Shadow read model and its GET-only API.

What is asserted, in order: that the API has no write surface and the module
no execution path; that the zero-order invariant is measured from the record
and trips when the record changes; that the observer's status follows the
session and the record rather than the clock alone; that the hypothetical
book applies each bar's recorded weight to the *next* bar's return; and that
every payload carries the simulation label and the sample warning.
"""

from __future__ import annotations

import ast
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autotrader.dashboard import a1b_shadow, a1b_shadow_api
from autotrader.dashboard.a1b_shadow import (
    DESIGNATION,
    EVENT_STARTED,
    EVENT_STOPPED,
    build_history,
    build_hypothetical,
    build_overview,
    build_service,
    build_summary,
    build_symbols,
    read_a1b,
)

_SCHEMA = """
CREATE TABLE system_events (
    id              INTEGER PRIMARY KEY,
    event_timestamp TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    message         TEXT,
    created_at      TEXT NOT NULL
);
CREATE TABLE order_intents (
    id              INTEGER PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE
);
CREATE TABLE runtime_checkpoints (
    symbol                       TEXT PRIMARY KEY,
    last_processed_bar_timestamp TEXT NOT NULL,
    updated_at                   TEXT NOT NULL
);
CREATE TABLE a1b_regime_state (
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
CREATE TABLE a1b_mark_state (
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
CREATE TABLE a1b_stance (
    symbol TEXT PRIMARY KEY,
    stance INTEGER NOT NULL CHECK (stance IN (0, 1)),
    bar_timestamp TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE a1b_observations (
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

#: A Wednesday inside a regular session: 15:00 UTC is 11:00 in New York.
IN_SESSION = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
#: The same Wednesday, well after the close.
AFTER_HOURS = datetime(2026, 9, 2, 23, 30, tzinfo=UTC)
SESSION_DATE = "2026-09-02"

POLICY_HASH = "cde9bbbd9d88dbd457c891aa601f5242723449c096bb2aff16d222ac7a89ed4e"

#: A small observation universe: two incumbents and two aliased names.
#: Generic symbols, deliberately - the record decides the universe.
UNIVERSE = ("AAA", "BBB", "CCC", "DDD")
WEIGHTS = {"AAA": 0.30, "BBB": 0.20, "CCC": 0.25, "DDD": 0.25}


def _bar(index: int) -> str:
    return (datetime(2026, 9, 2, 13, 45, tzinfo=UTC) + index * timedelta(minutes=15)).isoformat()


def _stamp(index: int) -> str:
    return (datetime(2026, 9, 2, 14, 0, 5, tzinfo=UTC) + index * timedelta(minutes=15)).isoformat()


def _write(path: Path, *, bars: int = 3, closes: dict[str, list[float]] | None = None) -> None:
    prices = closes or {
        "AAA": [100.0, 110.0, 99.0],
        "BBB": [50.0, 50.0, 55.0],
        "CCC": [200.0, 190.0, 190.0],
        "DDD": [10.0, 10.0, 10.0],
    }
    with sqlite3.connect(path) as connection:
        connection.executescript(_SCHEMA)
        connection.execute(
            "INSERT INTO system_events (event_timestamp, event_type, message, created_at)"
            " VALUES (?, ?, ?, ?)",
            (
                "2026-09-02T06:29:29+00:00",
                EVENT_STARTED,
                f"Equity A1-B U30 shadow started for {len(UNIVERSE)} symbols. Policy "
                f"{POLICY_HASH[:12]}, mark grid every 21 sessions from 2021-09-30, lookback "
                "4750 bars, code abcdef1234. This process holds no execution path: zero order "
                "mutation, verified per cycle.",
                "2026-09-02T06:29:29+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO a1b_regime_state VALUES (?, 1, 761.63, 710.6, -0.02, 1448, 200,"
            " -0.05, 1, 'SPY', ?)",
            (SESSION_DATE, "2026-09-02T13:45:05+00:00"),
        )
        connection.execute(
            "INSERT INTO a1b_mark_state VALUES (0, '2026-08-11', '2026-01-08', ?, '{}', ?, ?,"
            " ?, ?, ?)",
            (
                '{"AAA": 0, "BBB": 1, "CCC": 2, "DDD": 3}',
                '{"AAA": 0.3, "BBB": 0.2, "CCC": 0.25, "DDD": 0.25}',
                '{"AAA": 0.25, "BBB": 0.25, "CCC": 0.25, "DDD": 0.25}',
                len(UNIVERSE),
                POLICY_HASH,
                "2026-09-02T13:45:05+00:00",
            ),
        )
        for index in range(bars):
            for symbol in UNIVERSE:
                aliased = symbol in ("CCC", "DDD")
                signal = "BUY" if index == 0 and symbol == "AAA" else "HOLD"
                connection.execute(
                    "INSERT INTO a1b_observations (symbol, bar_timestamp, session_date,"
                    " participate, v3_signal, v3_stance, alias_scored, mark_index, mark_date,"
                    " archetype_label, active_weight, reserved_weight, target_weight,"
                    " reference_close, designation, client_order_id, recorded_at)"
                    " VALUES (?, ?, ?, 1, ?, ?, ?, 0, '2026-08-11', ?, ?, 0.25, ?, ?, ?, NULL, ?)",
                    (
                        symbol,
                        _bar(index),
                        SESSION_DATE,
                        signal,
                        1 if symbol == "AAA" else 0,
                        int(aliased),
                        UNIVERSE.index(symbol),
                        WEIGHTS[symbol],
                        WEIGHTS[symbol],
                        prices[symbol][index],
                        DESIGNATION,
                        _stamp(index),
                    ),
                )
        for symbol in UNIVERSE:
            connection.execute(
                "INSERT INTO a1b_stance VALUES (?, ?, ?, ?)",
                (symbol, 1 if symbol == "AAA" else 0, _bar(bars - 1), _stamp(bars - 1)),
            )
            connection.execute(
                "INSERT INTO runtime_checkpoints VALUES (?, ?, ?)",
                (symbol, _bar(bars - 1), _stamp(bars - 1)),
            )


@pytest.fixture
def a1b_db(tmp_path: Path) -> Path:
    path = tmp_path / "a1b-shadow.db"
    _write(path)
    return path


@pytest.fixture
def client(a1b_db: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(a1b_shadow.A1B_DATABASE_PATH_ENV, str(a1b_db))
    with TestClient(a1b_shadow_api.create_app()) as test_client:
        yield test_client


# ==========================================================================
# No write surface, no execution path
# ==========================================================================


def test_the_a1b_api_has_no_write_surface() -> None:
    application = a1b_shadow_api.create_app()
    for route in application.routes:
        methods = set(getattr(route, "methods", set()) or set())
        forbidden = methods & {"POST", "PUT", "PATCH", "DELETE"}
        assert not forbidden, f"{getattr(route, 'path', route)} exposes {sorted(forbidden)}"
        assert methods <= a1b_shadow_api.ALLOWED_METHODS | {"OPTIONS"}

    control_verbs = {"promote", "activate", "start", "stop", "submit", "cancel", "execute"}
    for route in application.routes:
        segments = {segment.lower() for segment in str(getattr(route, "path", "")).split("/")}
        assert not segments & control_verbs, getattr(route, "path", route)

    for module in (a1b_shadow, a1b_shadow_api):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported = {
            (node.module or "") for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        for banned in ("autotrader.execution", "autotrader.equity", "alpaca", "autotrader.state"):
            assert not any(name.startswith(banned) for name in imported), (
                f"{module.__name__} imports {banned}"
            )
        source = Path(module.__file__).read_text(encoding="utf-8")
        for symbol in (
            "submit_order",
            "state.connect(",
            "state.transaction(",
            "initialize_database",
        ):
            assert symbol not in source, f"{module.__name__} names {symbol}"


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize(
    "path",
    [
        "/api/equity-a1b-shadow/overview",
        "/api/equity-a1b-shadow/status",
        "/api/equity-a1b-shadow/latest",
        "/api/equity-a1b-shadow/comparison",
        "/api/equity-a1b-shadow/history",
    ],
)
def test_no_route_accepts_a_write_method(client: TestClient, path: str, method: str) -> None:
    assert getattr(client, method)(path).status_code == 405


@pytest.mark.parametrize(
    "path",
    [
        "/api/equity-a1b-shadow/promote",
        "/api/equity-a1b-shadow/activate",
        "/api/equity-a1b-shadow/orders/submit",
        "/api/equity-a1b-shadow/runtime/start",
    ],
)
def test_the_promotion_routes_someone_might_look_for_do_not_exist(
    client: TestClient, path: str
) -> None:
    assert client.post(path).status_code in {404, 405}
    assert client.get(path).status_code == 404


def test_the_reader_cannot_write_to_the_database(a1b_db: Path) -> None:
    with (
        a1b_shadow.read_only_connection(a1b_db) as connection,
        pytest.raises(sqlite3.OperationalError),
    ):
        connection.execute("DELETE FROM a1b_observations")


def test_no_response_carries_a_credential(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "PKTESTKEYVALUE0000000"
    secret = "sEcReTvAlUe000000000000000000000000000000"
    monkeypatch.setenv("ALPACA_API_KEY", key)
    monkeypatch.setenv("ALPACA_SECRET_KEY", secret)
    for path in ("overview", "status", "latest", "comparison", "history", "health"):
        body = client.get(f"/api/equity-a1b-shadow/{path}").text
        assert key not in body and secret not in body, path
        for forbidden in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "api_key", "secret"):
            assert forbidden not in body, f"{forbidden} in {path}"


# ==========================================================================
# The zero-order invariant, measured
# ==========================================================================


def test_the_zero_order_invariant_holds_on_a_clean_record(a1b_db: Path) -> None:
    service = build_service(read_a1b(a1b_db), now=IN_SESSION)

    assert service.zero_order_invariant_holds is True
    assert service.orders_submitted == 0
    assert service.order_intents_in_database == 0
    assert service.linked_orders_in_database == 0
    assert service.non_simulated_rows == 0
    assert service.broker_mutation == "DISABLED"
    assert service.designation == DESIGNATION


def test_an_order_intent_trips_the_invariant(a1b_db: Path) -> None:
    with sqlite3.connect(a1b_db) as connection:
        connection.execute("INSERT INTO order_intents (client_order_id) VALUES ('x')")

    service = build_service(read_a1b(a1b_db), now=IN_SESSION)

    assert service.zero_order_invariant_holds is False
    assert service.status == "STALE"
    assert service.status_reason == "ZERO_ORDER_INVARIANT_VIOLATED"


def test_the_record_itself_refuses_an_order_linkage(a1b_db: Path) -> None:
    """The constraint the runtime relies on, exercised against the real DDL."""
    with sqlite3.connect(a1b_db) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO a1b_observations (symbol, bar_timestamp, session_date, participate,"
            " v3_signal, v3_stance, alias_scored, mark_index, mark_date, archetype_label,"
            " active_weight, reserved_weight, target_weight, reference_close, designation,"
            " client_order_id, recorded_at) VALUES ('AAA', 'x', 'd', 1, 'HOLD', 1, 0, 0, 'm',"
            " 0, 0.1, 0.1, 0.1, 1.0, 'SIMULATED_SHADOW', 'autotrader-1', 'r')"
        )


# ==========================================================================
# Status
# ==========================================================================


def test_a_recent_observation_in_session_reads_running(a1b_db: Path) -> None:
    service = build_service(read_a1b(a1b_db), now=IN_SESSION)

    assert service.status == "RUNNING"
    assert service.status_reason == "CYCLE_WITHIN_EXPECTED_INTERVAL"
    assert service.last_cycle_at == _stamp(2)
    assert service.next_expected_cycle_at is not None
    assert service.session_confirmed_open is True


def test_a_quiet_observer_after_hours_is_idle_not_broken(a1b_db: Path) -> None:
    service = build_service(read_a1b(a1b_db), now=AFTER_HOURS)

    assert service.status == "IDLE"
    assert service.status_reason == "OFF_SESSION_NO_BARS_EXPECTED"


def test_a_quiet_observer_during_a_confirmed_session_is_stale(a1b_db: Path) -> None:
    service = build_service(read_a1b(a1b_db), now=IN_SESSION + timedelta(hours=1))

    assert service.status == "STALE"
    assert service.status_reason == "NO_CYCLE_DURING_CONFIRMED_OPEN_SESSION"


def test_a_clean_shutdown_reads_stopped(a1b_db: Path) -> None:
    with sqlite3.connect(a1b_db) as connection:
        connection.execute(
            "INSERT INTO system_events (event_timestamp, event_type, message, created_at)"
            " VALUES (?, ?, ?, ?)",
            (
                "2026-09-02T14:40:00+00:00",
                EVENT_STOPPED,
                "Equity A1-B shadow stopped in state STOPPED. Orders submitted: 0.",
                "2026-09-02T14:40:00+00:00",
            ),
        )

    assert build_service(read_a1b(a1b_db), now=IN_SESSION).status == "STOPPED"


def test_an_unreadable_database_is_unavailable_not_healthy(tmp_path: Path) -> None:
    page = build_overview(path=tmp_path / "absent.db", now=IN_SESSION)

    assert page.service.status == "UNAVAILABLE"
    assert page.service.zero_order_invariant_holds is False
    assert page.hypothetical.unavailable_reason == "DATABASE_UNREADABLE"
    assert page.regime.unavailable_reason == "DATABASE_UNREADABLE"


def test_the_policy_and_mark_grid_are_read_from_the_record(a1b_db: Path) -> None:
    service = build_service(read_a1b(a1b_db), now=IN_SESSION)

    assert service.policy_hash == POLICY_HASH
    assert service.mark_every_sessions == 21
    assert service.grid_anchor == "2021-09-30"
    assert service.code_sha == "abcdef1234"
    assert service.mark_date == "2026-08-11"
    assert service.fit_mark == "2026-01-08"
    assert service.labeled_symbols == len(UNIVERSE)


def test_the_universe_comes_from_the_record_not_a_constant(a1b_db: Path) -> None:
    service = build_service(read_a1b(a1b_db), now=IN_SESSION)

    assert service.universe == UNIVERSE
    assert service.universe_size == len(UNIVERSE)
    assert service.incumbents == ("AAA", "BBB")
    assert service.alias_scored == ("CCC", "DDD")
    assert service.symbols_recorded_last_cycle == len(UNIVERSE)
    assert service.cycles_recorded == 3
    assert service.observations_recorded == 12


def test_a_twenty_six_symbol_record_reports_twenty_six(tmp_path: Path) -> None:
    """The deployed observer watches twenty-six names; the count is the record's."""
    path = tmp_path / "wide.db"
    _write(path, bars=1)
    with sqlite3.connect(path) as connection:
        for index in range(len(UNIVERSE), 26):
            symbol = f"S{index:02d}"
            connection.execute(
                "INSERT INTO a1b_stance VALUES (?, 0, ?, ?)", (symbol, _bar(0), _stamp(0))
            )

    service = build_service(read_a1b(path), now=IN_SESSION)

    assert service.universe_size == 26


# ==========================================================================
# Symbols
# ==========================================================================


def test_every_universe_symbol_gets_a_row_with_its_stance_and_weight(a1b_db: Path) -> None:
    rows = {row.symbol: row for row in build_symbols(read_a1b(a1b_db))}

    assert set(rows) == set(UNIVERSE)
    assert rows["AAA"].stance == 1 and rows["BBB"].stance == 0
    assert rows["AAA"].incumbent is True and rows["CCC"].alias_scored is True
    assert rows["CCC"].target_weight == pytest.approx(0.25)
    assert rows["AAA"].archetype_label == 0
    assert all(row.designation == DESIGNATION for row in rows.values())
    assert rows["AAA"].bar_timestamp == _bar(2)


# ==========================================================================
# Hypothetical accounting
# ==========================================================================


def test_the_weight_is_applied_to_the_next_bars_return(a1b_db: Path) -> None:
    """Bar 0 -> 1: AAA +10% at 0.30, CCC -5% at 0.25. Bar 1 -> 2: AAA -10%, BBB +10%."""
    panel = build_hypothetical(read_a1b(a1b_db))

    step_one = 0.30 * 0.10 + 0.25 * (-0.05)
    step_two = 0.30 * (-0.10) + 0.20 * 0.10
    expected = 100.0 * (1 + step_one) * (1 + step_two)
    assert panel.steps == 2
    assert panel.portfolio_value == pytest.approx(expected, abs=1e-3)
    assert panel.cumulative_return == pytest.approx(expected / 100.0 - 1.0, abs=1e-6)
    assert panel.max_drawdown < 0
    assert panel.current_exposure == pytest.approx(1.0)
    assert panel.average_exposure == pytest.approx(1.0)
    assert panel.long_symbols == 4


def test_the_benchmark_is_equal_weight(a1b_db: Path) -> None:
    panel = build_hypothetical(read_a1b(a1b_db))

    step_one = (0.10 + 0.0 - 0.05 + 0.0) / 4
    step_two = (-0.10 + 0.10 + 0.0 + 0.0) / 4
    assert panel.benchmark_return == pytest.approx((1 + step_one) * (1 + step_two) - 1, abs=1e-6)


def test_the_hypothetical_panel_is_labelled_simulation_and_charges_no_costs(a1b_db: Path) -> None:
    page = build_overview(path=a1b_db, now=IN_SESSION)

    assert page.hypothetical.label == "SIMULATED / SHADOW - NO REAL ORDERS"
    assert page.hypothetical_label == page.hypothetical.label
    assert page.hypothetical.costs_applied is False
    assert page.observation_only is True and page.read_only is True
    assert page.hypothetical.sample_is_sufficient is False
    assert "no winner" in page.hypothetical.sample_warning
    assert page.hypothetical.capture_unavailable_reason == "SAMPLE_TOO_SMALL"


def test_a_single_bar_yields_no_curve(tmp_path: Path) -> None:
    path = tmp_path / "one.db"
    _write(path, bars=1)

    panel = build_hypothetical(read_a1b(path))

    assert panel.steps == 0
    assert panel.portfolio_value is None
    assert panel.unavailable_reason == "NO_OBSERVATIONS_RECORDED"


def test_no_annualized_metric_is_ever_produced(a1b_db: Path) -> None:
    page = build_overview(path=a1b_db, now=IN_SESSION)
    import dataclasses
    import json

    body = json.dumps(dataclasses.asdict(page)).lower()
    for word in ("sharpe", "annualized", "annualised", "sortino", "calmar"):
        assert word not in body, word


def test_the_summary_counts_match_the_record(a1b_db: Path) -> None:
    summary = build_summary(read_a1b(a1b_db))

    assert summary.observations == 12
    assert summary.bars == 3
    assert summary.symbols_per_bar == 4
    assert summary.participate_bars == 3 and summary.defensive_bars == 0
    assert summary.buy_signals == 1 and summary.hold_signals == 11
    assert summary.alias_scored_observations == 6
    assert summary.marks_computed == 1


# ==========================================================================
# History and the routes
# ==========================================================================


def test_history_is_bounded_and_newest_first(a1b_db: Path) -> None:
    page = build_history(read_a1b(a1b_db), limit=3)

    assert page.returned == 3 and page.total == 12
    assert page.rows[0].bar_timestamp == _bar(2)
    assert build_history(read_a1b(a1b_db), limit=10_000).limit == a1b_shadow.HISTORY_MAX_LIMIT
    assert build_history(read_a1b(a1b_db), symbol="aaa").total == 3


def test_every_documented_route_answers_a_get(client: TestClient) -> None:
    for path in ("health", "overview", "status", "latest", "comparison", "history"):
        response = client.get(f"/api/equity-a1b-shadow/{path}")
        assert response.status_code == 200, path
        assert response.json()


def test_the_overview_route_serializes_the_whole_page(client: TestClient) -> None:
    payload = client.get("/api/equity-a1b-shadow/overview").json()

    assert set(payload) >= {"service", "regime", "symbols", "hypothetical", "summary"}
    assert payload["service"]["universe_size"] == len(UNIVERSE)
    assert payload["service"]["orders_submitted"] == 0
    assert payload["service"]["zero_order_invariant_holds"] is True
    assert payload["service"]["designation"] == DESIGNATION
    assert payload["hypothetical_label"] == "SIMULATED / SHADOW - NO REAL ORDERS"


def test_the_liveness_route_opens_no_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(a1b_shadow.A1B_DATABASE_PATH_ENV, str(tmp_path / "absent.db"))
    with TestClient(a1b_shadow_api.create_app()) as test_client:
        payload = test_client.get("/api/equity-a1b-shadow/health").json()

    assert payload["observation_only"] is True
    assert payload["broker_mutation"] == "DISABLED"


def test_the_default_binding_is_loopback() -> None:
    assert a1b_shadow_api.DEFAULT_HOST == "127.0.0.1"
    assert a1b_shadow_api.DEFAULT_PORT == 8003
