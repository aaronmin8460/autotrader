"""The Equity Shadow dashboard: what it reads, and what it cannot do.

The tests that matter most here are the ones about *incapability*. The route
table is walked and asserted to be GET-only, no route path may name a trading
control or a promotion, and this module's executable code is audited for the
order-submission entry points - the same three-way audit the operational
dashboard carries, applied to the second API so that adding one did not widen
the surface.

The rest of the file is about honesty rather than safety: that a quiet
overnight shadow does not read as broken, that a confirmed-open session with
no cycles does, that capture ratios stay withheld while the sample is tiny,
that the hypothetical curves are labelled as simulation on every payload, and
that `designation = 'EXECUTED'` - which in this schema means the panel
released a candidate, not that an order followed - never trips the zero-order
invariant.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autotrader.dashboard import equity_shadow
from autotrader.dashboard import equity_shadow_api as shadow_api

# ==========================================================================
# A shadow database, built the way the runtime builds one
# ==========================================================================

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
CREATE TABLE shadow_decisions (
    id                INTEGER PRIMARY KEY,
    strategy_run_id   INTEGER,
    bar_timestamp     TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    engine_version    TEXT NOT NULL,
    signal            TEXT NOT NULL,
    score             REAL NOT NULL,
    confidence        REAL NOT NULL,
    regime            TEXT NOT NULL,
    reasons           TEXT NOT NULL,
    feature_version   TEXT,
    model_version     TEXT,
    execution_version TEXT NOT NULL,
    designation       TEXT NOT NULL,
    client_order_id   TEXT,
    created_at        TEXT NOT NULL,
    UNIQUE (symbol, bar_timestamp, engine_version)
);
CREATE TABLE shadow_regime_state (
    session_date      TEXT PRIMARY KEY,
    participate       INTEGER NOT NULL,
    info_close        REAL,
    info_sma          REAL,
    info_drawdown     REAL,
    sessions_observed INTEGER NOT NULL,
    sma_sessions      INTEGER NOT NULL,
    calm_threshold    REAL NOT NULL,
    lag_sessions      INTEGER NOT NULL,
    reference_symbol  TEXT NOT NULL,
    computed_at       TEXT NOT NULL
);
CREATE TABLE shadow_side_by_side (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    bar_timestamp   TEXT NOT NULL,
    session_date    TEXT NOT NULL,
    participate     INTEGER NOT NULL,
    v3_signal       TEXT NOT NULL,
    v3_stance       INTEGER NOT NULL,
    eda1_signal     TEXT NOT NULL,
    eda1_stance     INTEGER NOT NULL,
    signals_agree   INTEGER NOT NULL,
    stances_agree   INTEGER NOT NULL,
    reference_close REAL NOT NULL,
    recorded_at     TEXT NOT NULL,
    UNIQUE (symbol, bar_timestamp)
);
"""

#: A Monday inside a regular session: 15:00 UTC is 11:00 in New York.
IN_SESSION = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)

#: The same Monday, well after the close.
AFTER_HOURS = datetime(2026, 8, 31, 23, 30, tzinfo=UTC)

SESSION_DATE = "2026-08-31"


def _bar(index: int) -> str:
    moment = datetime(2026, 8, 31, 13, 30, tzinfo=UTC) + timedelta(minutes=15 * index)
    return moment.isoformat()


def build_database(
    path: Path,
    *,
    bars: int = 3,
    participate: bool = True,
    v3_stance: int = 0,
    eda1_stance: int = 1,
    last_cycle: datetime | None = None,
    stopped: bool = False,
    released_candidates: int = 0,
    order_intents: int = 0,
    linked_orders: int = 0,
    price_path: list[float] | None = None,
) -> Path:
    """Write a shadow database shaped like the one the runtime writes."""
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA)

    started = datetime(2026, 8, 31, 13, 37, tzinfo=UTC)
    sha = "50009917975b5093956c1e29748f1c9445a5b3e3"
    connection.execute(
        "INSERT INTO system_events (event_timestamp, event_type, message, created_at) "
        "VALUES (?, ?, ?, ?)",
        (
            started.isoformat(),
            equity_shadow.EVENT_STARTED,
            f"Equity V3 + EDA-1 side-by-side shadow started. code {sha}. No execution path.",
            started.isoformat(),
        ),
    )

    connection.execute(
        "INSERT INTO shadow_regime_state VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            SESSION_DATE,
            int(participate),
            769.28,
            709.817775,
            -0.011,
            1448,
            200,
            -0.05,
            1,
            "SPY",
            started.isoformat(),
        ),
    )

    prices = price_path or [100.0 + index for index in range(bars)]
    for index in range(bars):
        stamp = _bar(index)
        close = prices[index]
        for symbol in equity_shadow.UNIVERSE:
            connection.execute(
                "INSERT INTO shadow_side_by_side "
                "(symbol, bar_timestamp, session_date, participate, v3_signal, v3_stance, "
                " eda1_signal, eda1_stance, signals_agree, stances_agree, reference_close, "
                " recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    symbol,
                    stamp,
                    SESSION_DATE,
                    int(participate),
                    "HOLD",
                    v3_stance,
                    "HOLD",
                    eda1_stance,
                    1,
                    int(v3_stance == eda1_stance),
                    close,
                    stamp,
                ),
            )
            for engine in (equity_shadow.ENGINE_V3, equity_shadow.ENGINE_EDA1):
                connection.execute(
                    "INSERT INTO shadow_decisions (bar_timestamp, symbol, engine_version, "
                    " signal, score, confidence, regime, reasons, execution_version, "
                    " designation, client_order_id, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        stamp,
                        symbol,
                        engine,
                        "HOLD",
                        0.14,
                        0.37,
                        "PARTICIPATE" if engine == equity_shadow.ENGINE_EDA1 else "TREND_DOWN",
                        "EDA1_RGP_HOLD"
                        if engine == equity_shadow.ENGINE_EDA1
                        else "LOW_CONFIDENCE",
                        "v3",
                        "NOT_EXECUTED",
                        None,
                        stamp,
                    ),
                )
        cycle_at = last_cycle if (index == bars - 1 and last_cycle) else _parse(stamp)
        connection.execute(
            "INSERT INTO system_events (event_timestamp, event_type, message, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                cycle_at.isoformat(),
                equity_shadow.EVENT_CYCLE,
                "Shadow cycle: 10 bar(s) recorded, 0 order intents in this database (verified).",
                cycle_at.isoformat(),
            ),
        )

    # Candidates the panel released. In this schema that designation records a
    # release, not an order, and `client_order_id` stays NULL.
    for index in range(released_candidates):
        connection.execute(
            "UPDATE shadow_decisions SET designation = 'EXECUTED' "
            "WHERE engine_version = 'v3' AND symbol = 'IWM' AND bar_timestamp = ?",
            (_bar(index),),
        )

    for index in range(order_intents):
        connection.execute(
            "INSERT INTO order_intents (client_order_id) VALUES (?)", (f"intent-{index}",)
        )

    for index in range(linked_orders):
        connection.execute(
            "UPDATE shadow_decisions SET designation = 'EXECUTED', client_order_id = ? "
            "WHERE engine_version = 'v3' AND symbol = 'SPY' AND bar_timestamp = ?",
            (f"linked-{index}", _bar(index)),
        )

    if stopped:
        moment = datetime(2026, 8, 31, 20, 5, tzinfo=UTC)
        connection.execute(
            "INSERT INTO system_events (event_timestamp, event_type, message, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                moment.isoformat(),
                equity_shadow.EVENT_STOPPED,
                "Equity V3 shadow stopped. Orders submitted by this process: 0, by construction.",
                moment.isoformat(),
            ),
        )

    connection.commit()
    connection.close()
    return path


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


@pytest.fixture
def shadow_db(tmp_path: Path) -> Path:
    return build_database(tmp_path / "shadow.db")


@pytest.fixture
def client(shadow_db: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(equity_shadow.SHADOW_DATABASE_PATH_ENV, str(shadow_db))
    return TestClient(shadow_api.create_app())


# ==========================================================================
# The critical safety tests
# ==========================================================================


def test_the_shadow_api_has_no_write_surface() -> None:
    """GET-only, no control in any path, and nothing behind one if there were."""
    application = shadow_api.create_app()

    for route in application.routes:
        methods = set(getattr(route, "methods", set()) or set())
        forbidden = methods & {"POST", "PUT", "PATCH", "DELETE"}
        assert not forbidden, f"{getattr(route, 'path', route)} exposes {sorted(forbidden)}"
        assert methods <= shadow_api.ALLOWED_METHODS | {"OPTIONS"}, (
            f"{getattr(route, 'path', route)} exposes {sorted(methods)}"
        )

    # A noun is a resource to read. A verb is a command - and on this API the
    # list includes the two verbs that would end the shadow's whole reason for
    # existing: promoting it, or activating an engine.
    action_verbs = {
        "submit",
        "send",
        "place",
        "cancel",
        "buy",
        "sell",
        "close",
        "flatten",
        "liquidate",
        "start",
        "stop",
        "pause",
        "resume",
        "kill",
        "repair",
        "reconcile",
        "execute",
        "run",
        "create",
        "update",
        "edit",
        "set",
        "delete",
        "reset",
        "override",
        "promote",
        "activate",
        "enable",
        "arm",
    }
    for route in application.routes:
        segments = {segment.lower() for segment in str(getattr(route, "path", "")).split("/")}
        offending = segments & action_verbs
        assert not offending, f"{getattr(route, 'path', route)} names the action {offending}"

    forbidden_symbols = (
        "submit_order",
        "submit_order_intent",
        "execute_paper_order",
        "build_market_order_request",
        "MarketOrderRequest",
        "OrderRequest",
        "record_order_intent",
        "upsert_position",
        "upsert_broker_order",
        "update_order_intent_status",
        "reconcile_paper_state",
        "initialize_database",
        "paper=False",
    )
    root = Path(equity_shadow.__file__).parent
    for name in ("equity_shadow.py", "equity_shadow_api.py"):
        code = (root / name).read_text(encoding="utf-8")
        for symbol in forbidden_symbols:
            assert symbol not in code, f"{symbol} found in dashboard/{name}"


@pytest.mark.parametrize(
    "path",
    [
        "/api/equity-shadow/health",
        "/api/equity-shadow/overview",
        "/api/equity-shadow/status",
        "/api/equity-shadow/latest",
        "/api/equity-shadow/comparison",
        "/api/equity-shadow/history",
    ],
)
@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_no_route_accepts_a_write_method(client: TestClient, path: str, method: str) -> None:
    assert getattr(client, method)(path).status_code in {404, 405}


@pytest.mark.parametrize(
    "path",
    [
        "/api/equity-shadow/promote",
        "/api/equity-shadow/activate",
        "/api/equity-shadow/orders/submit",
        "/api/equity-shadow/execute",
        "/api/equity-shadow/enable",
    ],
)
def test_the_promotion_routes_someone_might_look_for_do_not_exist(
    client: TestClient, path: str
) -> None:
    assert client.get(path).status_code == 404


def test_the_reader_cannot_write_to_the_database(shadow_db: Path) -> None:
    """`mode=ro` and `query_only` both, so neither is load-bearing alone."""
    with equity_shadow.read_only_connection(shadow_db) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO order_intents (client_order_id) VALUES ('x')")
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM shadow_decisions")


def test_no_response_carries_a_credential(client: TestClient) -> None:
    for path in ("/api/equity-shadow/overview", "/api/equity-shadow/latest"):
        body = client.get(path).text
        for forbidden in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "api_key", "secret"):
            assert forbidden not in body, f"{forbidden} in {path}"


# ==========================================================================
# The zero-order invariant
# ==========================================================================


def test_released_candidates_do_not_trip_the_invariant(tmp_path: Path) -> None:
    """`EXECUTED` here means the panel released a candidate, not that it traded.

    Counting these as orders would raise an alarm on the system working
    exactly as designed, which is how an operator learns to ignore alarms.
    """
    path = build_database(tmp_path / "released.db", released_candidates=3)
    panel = equity_shadow.build_service(equity_shadow.read_shadow(path), now=IN_SESSION)

    assert panel.released_candidates == 3
    assert panel.zero_order_invariant_holds is True
    assert panel.order_intents_in_database == 0
    assert panel.linked_orders_in_database == 0
    assert panel.orders_submitted == 0
    assert "NOT orders" in panel.released_candidates_meaning


def test_an_order_intent_trips_the_invariant(tmp_path: Path) -> None:
    path = build_database(tmp_path / "intent.db", order_intents=1)
    panel = equity_shadow.build_service(equity_shadow.read_shadow(path), now=IN_SESSION)

    assert panel.zero_order_invariant_holds is False
    assert panel.status == equity_shadow.SHADOW_STALE
    assert panel.status_reason == "ZERO_ORDER_INVARIANT_VIOLATED"


def test_a_decision_linked_to_an_order_trips_the_invariant(tmp_path: Path) -> None:
    path = build_database(tmp_path / "linked.db", linked_orders=1)
    panel = equity_shadow.build_service(equity_shadow.read_shadow(path), now=IN_SESSION)

    assert panel.linked_orders_in_database == 1
    assert panel.zero_order_invariant_holds is False


def test_the_startup_safety_field_is_reported_as_not_applicable(
    shadow_db: Path,
) -> None:
    """The heartbeat prints `UNRESOLVED`; the page must not repeat the implication."""
    panel = equity_shadow.build_service(equity_shadow.read_shadow(shadow_db), now=IN_SESSION)

    assert panel.startup_safety_applicable is False
    assert "Not applicable" in panel.startup_safety_note
    assert "no execution path" in panel.startup_safety_note


# ==========================================================================
# Status semantics
# ==========================================================================


def test_a_recent_cycle_in_session_reads_running(shadow_db: Path) -> None:
    panel = equity_shadow.build_service(
        equity_shadow.read_shadow(shadow_db), now=_parse(_bar(2)) + timedelta(minutes=5)
    )
    assert panel.status == equity_shadow.SHADOW_RUNNING
    assert panel.session_confirmed_open is True


def test_a_quiet_shadow_after_hours_is_idle_not_broken(shadow_db: Path) -> None:
    """The whole point of the off-session branch.

    Overnight there is no bar to observe, so a red service card would be a
    false alarm every single night.
    """
    panel = equity_shadow.build_service(equity_shadow.read_shadow(shadow_db), now=AFTER_HOURS)
    assert panel.status == equity_shadow.SHADOW_IDLE
    assert panel.status_reason == "OFF_SESSION_NO_BARS_EXPECTED"
    assert panel.within_regular_session is False


def test_a_quiet_shadow_during_a_confirmed_session_is_stale(shadow_db: Path) -> None:
    late = _parse(_bar(2)) + equity_shadow.STALE_AFTER + timedelta(minutes=1)
    panel = equity_shadow.build_service(equity_shadow.read_shadow(shadow_db), now=late)
    assert panel.status == equity_shadow.SHADOW_STALE
    assert panel.status_reason == "NO_CYCLE_DURING_CONFIRMED_OPEN_SESSION"


def test_a_session_the_broker_never_opened_is_idle(tmp_path: Path) -> None:
    """A holiday has no regime state, so the clock alone must not cry outage."""
    path = build_database(tmp_path / "holiday.db")
    # A Thursday inside clock hours, but with no regime row for that date.
    thanksgiving = datetime(2026, 11, 26, 16, 0, tzinfo=UTC)
    panel = equity_shadow.build_service(equity_shadow.read_shadow(path), now=thanksgiving)

    assert panel.within_regular_session is True
    assert panel.session_confirmed_open is False
    assert panel.status == equity_shadow.SHADOW_IDLE


def test_a_clean_shutdown_reads_stopped(tmp_path: Path) -> None:
    path = build_database(tmp_path / "stopped.db", stopped=True)
    panel = equity_shadow.build_service(
        equity_shadow.read_shadow(path), now=datetime(2026, 8, 31, 20, 30, tzinfo=UTC)
    )
    assert panel.status == equity_shadow.SHADOW_STOPPED


def test_an_unreadable_database_is_unavailable_not_healthy(tmp_path: Path) -> None:
    panel = equity_shadow.build_service(
        equity_shadow.read_shadow(tmp_path / "absent.db"), now=IN_SESSION
    )
    assert panel.status == equity_shadow.SHADOW_UNAVAILABLE
    assert panel.zero_order_invariant_holds is False


def test_the_code_sha_is_read_from_the_start_event(shadow_db: Path) -> None:
    panel = equity_shadow.build_service(equity_shadow.read_shadow(shadow_db), now=IN_SESSION)
    assert panel.code_sha == "50009917975b5093956c1e29748f1c9445a5b3e3"


def test_weekend_is_outside_the_session_window() -> None:
    saturday = datetime(2026, 9, 5, 15, 0, tzinfo=UTC)
    assert equity_shadow.within_regular_session(saturday) is False


def test_the_session_window_follows_new_york_not_utc() -> None:
    """13:30 UTC is the open in winter and 13:30 UTC is late-morning in summer.

    A hardcoded UTC window would be wrong for half the year; the zone database
    is what keeps the March and November boundaries honest.
    """
    # 2026-01-05 is a Monday. 14:00 UTC is 09:00 ET - before the open.
    assert equity_shadow.within_regular_session(datetime(2026, 1, 5, 14, 0, tzinfo=UTC)) is False
    assert equity_shadow.within_regular_session(datetime(2026, 1, 5, 15, 0, tzinfo=UTC)) is True
    # 2026-07-06 is a Monday in DST. 13:45 UTC is 09:45 ET - inside the session.
    assert equity_shadow.within_regular_session(datetime(2026, 7, 6, 13, 45, tzinfo=UTC)) is True
    assert equity_shadow.within_regular_session(datetime(2026, 7, 6, 20, 30, tzinfo=UTC)) is False


# ==========================================================================
# Regime
# ==========================================================================


def test_the_regime_panel_carries_its_whole_information_set(shadow_db: Path) -> None:
    panel = equity_shadow.build_regime(equity_shadow.read_shadow(shadow_db))
    assert panel.state == "PARTICIPATE"
    assert panel.participate is True
    assert panel.reference_symbol == "SPY"
    assert panel.info_close == pytest.approx(769.28)
    assert panel.info_sma == pytest.approx(709.817775)
    assert panel.sma_sessions == 200
    assert panel.calm_threshold == pytest.approx(-0.05)
    assert panel.lag_sessions == 1
    assert panel.sessions_observed == 1448


def test_a_defensive_regime_reads_as_handing_back_to_v3(tmp_path: Path) -> None:
    path = build_database(tmp_path / "defensive.db", participate=False, eda1_stance=0)
    panel = equity_shadow.build_regime(equity_shadow.read_shadow(path))
    assert panel.state == "DEFENSIVE_V3"
    assert panel.participate is False


# ==========================================================================
# The side-by-side table
# ==========================================================================


def test_every_universe_symbol_gets_a_row(shadow_db: Path) -> None:
    rows = equity_shadow.build_symbols(equity_shadow.read_shadow(shadow_db))
    assert tuple(row.symbol for row in rows) == equity_shadow.UNIVERSE


def test_eda1_score_is_labelled_as_copied_rather_than_invented(shadow_db: Path) -> None:
    """EDA-1 is a router, not a probability model. The page must not imply one."""
    rows = equity_shadow.build_symbols(equity_shadow.read_shadow(shadow_db))
    for row in rows:
        assert row.eda1_score_source == equity_shadow.SCORE_COPIED_FROM_V3
        assert not hasattr(row, "eda1_score")
        assert not hasattr(row, "eda1_confidence")


def test_the_latest_bar_is_the_one_reported(shadow_db: Path) -> None:
    rows = equity_shadow.build_symbols(equity_shadow.read_shadow(shadow_db))
    assert all(row.bar_timestamp == _bar(2) for row in rows)


# ==========================================================================
# Hypothetical accounting
# ==========================================================================


def test_a_long_everything_book_equals_the_equal_weight_benchmark(tmp_path: Path) -> None:
    path = build_database(tmp_path / "long.db", bars=4, v3_stance=0, eda1_stance=1)
    panel = equity_shadow.build_hypothetical(equity_shadow.read_shadow(path))

    assert panel.eda1 is not None
    assert panel.eda1.cumulative_return == pytest.approx(panel.benchmark_return)
    assert panel.eda1.long_exposure_fraction == pytest.approx(1.0)


def test_a_flat_book_earns_exactly_nothing(tmp_path: Path) -> None:
    path = build_database(tmp_path / "flat.db", bars=4, v3_stance=0)
    panel = equity_shadow.build_hypothetical(equity_shadow.read_shadow(path))

    assert panel.v3 is not None
    assert panel.v3.cumulative_return == pytest.approx(0.0)
    assert panel.v3.portfolio_value == pytest.approx(equity_shadow.NORMALIZED_START)
    assert panel.v3.current_stance_summary == "FLAT (no position)"


def test_the_stance_is_applied_to_the_next_bars_return_not_its_own(tmp_path: Path) -> None:
    """Causality, asserted arithmetically.

    Three closes 100 -> 110 -> 121 give two steps of exactly +10%, so a book
    long throughout must compound to 121. A book that credited the stance with
    the bar it was decided on would produce a different number.
    """
    path = build_database(
        tmp_path / "causal.db", bars=3, eda1_stance=1, price_path=[100.0, 110.0, 121.0]
    )
    panel = equity_shadow.build_hypothetical(equity_shadow.read_shadow(path))

    assert panel.steps == 2
    assert panel.eda1 is not None
    assert panel.eda1.portfolio_value == pytest.approx(121.0)


def test_a_drawdown_is_reported_negative(tmp_path: Path) -> None:
    path = build_database(tmp_path / "dd.db", bars=3, eda1_stance=1, price_path=[100.0, 90.0, 95.0])
    panel = equity_shadow.build_hypothetical(equity_shadow.read_shadow(path))
    assert panel.eda1 is not None
    assert panel.eda1.max_drawdown == pytest.approx(-0.10)


def test_the_hypothetical_panel_is_labelled_simulation_and_charges_no_costs(
    shadow_db: Path,
) -> None:
    panel = equity_shadow.build_hypothetical(equity_shadow.read_shadow(shadow_db))
    assert panel.label == equity_shadow.HYPOTHETICAL_LABEL
    assert "NO REAL ORDERS" in panel.label
    assert panel.costs_applied is False
    assert panel.normalized_start == equity_shadow.NORMALIZED_START


def test_a_single_bar_yields_no_curve(tmp_path: Path) -> None:
    path = build_database(tmp_path / "one.db", bars=1)
    panel = equity_shadow.build_hypothetical(equity_shadow.read_shadow(path))
    assert panel.unavailable_reason is not None
    assert panel.v3 is None


# ==========================================================================
# Comparison metrics
# ==========================================================================


def test_capture_ratios_are_withheld_on_a_tiny_sample(shadow_db: Path) -> None:
    panel = equity_shadow.build_comparison(equity_shadow.read_shadow(shadow_db))
    assert panel.up_capture is None
    assert panel.down_capture is None
    assert panel.capture_unavailable_reason == equity_shadow.UNAVAILABLE_SAMPLE_TOO_SMALL
    assert panel.sample_is_sufficient is False
    assert panel.sample_warning


def test_the_sample_warning_is_present_even_when_capture_is_available(
    tmp_path: Path,
) -> None:
    """A large sample earns the ratio; it does not earn silence about the size."""
    bars = equity_shadow.MIN_STEPS_FOR_CAPTURE + 5
    path = build_database(
        tmp_path / "big.db",
        bars=bars,
        eda1_stance=1,
        price_path=[100.0 + (index % 7) for index in range(bars)],
    )
    panel = equity_shadow.build_comparison(equity_shadow.read_shadow(path))
    assert panel.up_capture is not None
    assert panel.capture_unavailable_reason is None
    assert panel.sample_warning
    assert panel.sample_is_sufficient is False


def test_no_annualized_metric_is_ever_produced(shadow_db: Path) -> None:
    """Sharpe and friends are absent by construction, at every sample size."""
    panel = equity_shadow.build_comparison(equity_shadow.read_shadow(shadow_db))
    fields = set(vars(panel))
    for banned in ("sharpe", "annualized", "annual_return", "volatility", "cagr"):
        assert not any(banned in name for name in fields)


def test_agreement_counts_match_the_stored_rows(shadow_db: Path) -> None:
    snapshot = equity_shadow.read_shadow(shadow_db)
    panel = equity_shadow.build_comparison(snapshot)
    assert panel.bars_compared == len(snapshot.comparisons)
    assert panel.agreement_count + panel.disagreement_count == panel.bars_compared


def test_stance_disagreement_is_counted_separately_from_signal_agreement(
    shadow_db: Path,
) -> None:
    """The two engines can agree HOLD while holding opposite positions.

    That is exactly today's live case - V3 flat, EDA-1 long, both saying HOLD -
    and collapsing the two would hide the only disagreement that matters.
    """
    panel = equity_shadow.build_comparison(equity_shadow.read_shadow(shadow_db))
    assert panel.agreement_count == panel.bars_compared
    assert panel.stance_disagreement_count == panel.bars_compared


# ==========================================================================
# History
# ==========================================================================


def test_history_is_bounded_and_newest_first(shadow_db: Path) -> None:
    page = equity_shadow.build_history(equity_shadow.read_shadow(shadow_db), limit=5)
    assert page.returned == 5
    assert page.limit == 5
    assert page.rows[0].bar_timestamp == _bar(2)


def test_history_refuses_an_unbounded_query(shadow_db: Path) -> None:
    page = equity_shadow.build_history(equity_shadow.read_shadow(shadow_db), limit=10_000_000)
    assert page.limit == equity_shadow.HISTORY_MAX_LIMIT


def test_history_can_be_narrowed_to_one_symbol(shadow_db: Path) -> None:
    page = equity_shadow.build_history(equity_shadow.read_shadow(shadow_db), symbol="spy")
    assert {row.symbol for row in page.rows} == {"SPY"}


def test_the_history_route_rejects_an_out_of_range_limit(client: TestClient) -> None:
    assert client.get("/api/equity-shadow/history?limit=0").status_code == 422
    over = equity_shadow.HISTORY_MAX_LIMIT + 1
    assert client.get(f"/api/equity-shadow/history?limit={over}").status_code == 422


# ==========================================================================
# Serialization
# ==========================================================================


def test_every_documented_route_answers_a_get(client: TestClient) -> None:
    for path in (
        "/api/equity-shadow/health",
        "/api/equity-shadow/overview",
        "/api/equity-shadow/status",
        "/api/equity-shadow/latest",
        "/api/equity-shadow/comparison",
        "/api/equity-shadow/history",
    ):
        assert client.get(path).status_code == 200, path


def test_the_liveness_route_opens_no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(equity_shadow.SHADOW_DATABASE_PATH_ENV, "/nonexistent/nowhere.db")
    payload = TestClient(shadow_api.create_app()).get("/api/equity-shadow/health").json()
    assert payload["status"] == "ok"
    assert payload["read_only"] is True
    assert payload["broker_mutation"] == equity_shadow.BROKER_MUTATION_DISABLED


def test_the_overview_route_serializes_the_whole_page(client: TestClient) -> None:
    payload = client.get("/api/equity-shadow/overview").json()
    assert payload["read_only"] is True
    assert payload["observation_only"] is True
    assert "NO REAL ORDERS" in payload["hypothetical_label"]
    for key in ("service", "regime", "symbols", "hypothetical", "comparison"):
        assert key in payload
    assert len(payload["symbols"]) == len(equity_shadow.UNIVERSE)


def test_the_sub_routes_are_slices_of_the_same_read(client: TestClient) -> None:
    page = client.get("/api/equity-shadow/overview").json()
    assert client.get("/api/equity-shadow/status").json()["status"] == page["service"]["status"]
    assert client.get("/api/equity-shadow/latest").json()["symbols"] == page["symbols"]
    assert (
        client.get("/api/equity-shadow/comparison").json()["comparison"]["bars_compared"]
        == page["comparison"]["bars_compared"]
    )


def test_an_unreadable_database_still_serializes_a_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing database is a status, not a 500."""
    monkeypatch.setenv(equity_shadow.SHADOW_DATABASE_PATH_ENV, str(tmp_path / "gone.db"))
    response = TestClient(shadow_api.create_app()).get("/api/equity-shadow/overview")
    assert response.status_code == 200
    assert response.json()["service"]["status"] == equity_shadow.SHADOW_UNAVAILABLE
