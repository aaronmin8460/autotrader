"""The Equity Paper read model's V2 additions: policy figures and target-vs-actual.

The defect these pin: the operations page rendered the crypto build's risk
engine constants as the account's policy, so a book sized to a 90% target
under a 95% hard cap was painted red against a 30% line. The policy figures
now come from the paper runtime's own start event, resolved in the allocation
registry, as numbers - and the stale constants must never reach the page while
that policy is the deployed one.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autotrader.dashboard import equity_paper, equity_paper_api
from autotrader.dashboard.equity_paper import (
    POLICY_SOURCE_FALLBACK,
    POLICY_SOURCE_RUNTIME,
    TARGET_SOURCE_FLAT,
    TARGET_SOURCE_NOT_RECORDED,
    TARGET_SOURCE_RECORDED,
    build_overview,
)
from autotrader.equity import EQUITY_SYMBOLS

NOW = datetime(2026, 9, 2, 17, 40, tzinfo=UTC)
LATEST_BAR = "2026-09-02T17:15:00.000000+00:00"
OLDER_BAR = "2026-09-02T13:30:00.000000+00:00"
POLICY_ID = "EDA1_FRACTIONAL_RESERVED_90"
POLICY_HASH = "e081e1f6bad9fb8eb35ea0b2671b99f8ddbf083063c05c2932f90e4d3eb380f3"

_SCHEMA = """
CREATE TABLE system_events (
    id INTEGER PRIMARY KEY, event_timestamp TEXT NOT NULL, event_type TEXT NOT NULL,
    message TEXT, created_at TEXT NOT NULL
);
CREATE TABLE order_intents (
    id INTEGER PRIMARY KEY, client_order_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
    symbol TEXT NOT NULL, side TEXT NOT NULL, requested_quantity TEXT NOT NULL,
    approved_quantity TEXT NOT NULL, reference_price REAL NOT NULL,
    risk_reason_code TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE broker_orders (
    id INTEGER PRIMARY KEY, order_intent_id INTEGER NOT NULL, broker_order_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    quantity TEXT NOT NULL, filled_quantity TEXT NOT NULL, filled_average_price REAL,
    status TEXT NOT NULL, submitted_at TEXT, filled_at TEXT, updated_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE positions (
    symbol TEXT PRIMARY KEY, quantity TEXT NOT NULL, average_price REAL, updated_at TEXT NOT NULL
);
CREATE TABLE risk_events (
    id INTEGER PRIMARY KEY, event_timestamp TEXT NOT NULL, decision TEXT NOT NULL,
    reason_code TEXT NOT NULL, symbol TEXT, message TEXT
);
CREATE TABLE shadow_side_by_side (
    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, bar_timestamp TEXT NOT NULL,
    session_date TEXT NOT NULL, participate INTEGER NOT NULL, v3_signal TEXT NOT NULL,
    v3_stance INTEGER NOT NULL, eda1_signal TEXT NOT NULL, eda1_stance INTEGER NOT NULL,
    signals_agree INTEGER NOT NULL, stances_agree INTEGER NOT NULL,
    reference_close REAL NOT NULL, recorded_at TEXT NOT NULL
);
CREATE TABLE shadow_regime_state (
    session_date TEXT PRIMARY KEY, participate INTEGER NOT NULL, info_close REAL,
    info_sma REAL, info_drawdown REAL, sessions_observed INTEGER NOT NULL,
    sma_sessions INTEGER NOT NULL, calm_threshold REAL NOT NULL, lag_sessions INTEGER NOT NULL,
    reference_symbol TEXT NOT NULL, computed_at TEXT NOT NULL
);
CREATE TABLE reconciliation_runs (
    id INTEGER PRIMARY KEY, completed_at TEXT, status TEXT NOT NULL, safe_to_trade INTEGER,
    orders_checked INTEGER, positions_checked INTEGER, unresolved_count INTEGER
);
CREATE TABLE account_safety_state (
    id INTEGER PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL, source TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE equity_paper_targets (
    id INTEGER PRIMARY KEY, client_order_id TEXT UNIQUE, engine TEXT NOT NULL,
    environment TEXT NOT NULL, sizing_policy TEXT NOT NULL, sizing_config_hash TEXT NOT NULL,
    rollout_stage TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    target_weight TEXT NOT NULL, target_notional TEXT NOT NULL, target_quantity TEXT NOT NULL,
    broker_quantity TEXT NOT NULL, requested_delta TEXT NOT NULL, approved_quantity TEXT,
    risk_reason_code TEXT, reference_price TEXT NOT NULL, account_equity TEXT NOT NULL,
    external_exposure TEXT NOT NULL, budget_fraction TEXT NOT NULL, bar_timestamp TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
"""


def _write(path: Path, *, policy_line: str | None = POLICY_ID, targets: bool = True) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(_SCHEMA)
        started = (
            "Equity EDA-1 PAPER runtime started at rollout stage C (execution universe "
            f"{', '.join(EQUITY_SYMBOLS)}; decision universe {', '.join(EQUITY_SYMBOLS)}). "
            + (f"Sizing policy {policy_line} ({POLICY_HASH[:12]}), " if policy_line else "")
            + "per-symbol cap 0.11, total cap 0.95. Environment: PAPER ONLY."
        )
        connection.execute(
            "INSERT INTO system_events (event_timestamp, event_type, message, created_at)"
            " VALUES (?, 'EQUITY_PAPER_STARTED', ?, ?)",
            ("2026-09-02T13:53:56+00:00", started, "2026-09-02T13:53:56+00:00"),
        )
        connection.execute(
            "INSERT INTO system_events (event_timestamp, event_type, message, created_at)"
            " VALUES (?, 'EQUITY_PAPER_CYCLE', 'cycle', ?)",
            ("2026-09-02T17:17:00+00:00", "2026-09-02T17:17:00+00:00"),
        )
        connection.execute(
            "INSERT INTO shadow_regime_state VALUES ('2026-09-02', 1, 761.63, 710.6, -0.02, 1448,"
            " 200, -0.05, 1, 'SPY', '2026-09-02T13:45:05+00:00')"
        )
        for symbol in EQUITY_SYMBOLS:
            stance = 0 if symbol == "TSLA" else 1
            connection.execute(
                "INSERT INTO shadow_side_by_side (symbol, bar_timestamp, session_date, participate,"
                " v3_signal, v3_stance, eda1_signal, eda1_stance, signals_agree, stances_agree,"
                " reference_close, recorded_at)"
                " VALUES (?, ?, '2026-09-02', 1, 'HOLD', ?, ?, ?, 1, 1, 100.0, ?)",
                (
                    symbol,
                    LATEST_BAR,
                    stance,
                    "SELL" if stance == 0 else "HOLD",
                    stance,
                    NOW.isoformat(),
                ),
            )
            connection.execute(
                "INSERT INTO positions VALUES (?, ?, 100.0, ?)",
                (symbol, "0" if stance == 0 else "27.5", NOW.isoformat()),
            )
        if targets:
            for index, symbol in enumerate(EQUITY_SYMBOLS):
                if symbol == "GOOGL":
                    continue  # a LONG stance with no recorded decision
                bar = LATEST_BAR if symbol == "META" else OLDER_BAR
                side = "SELL" if symbol in ("META", "TSLA") else "BUY"
                connection.execute(
                    "INSERT INTO equity_paper_targets (client_order_id, engine, environment,"
                    " sizing_policy, sizing_config_hash, rollout_stage, symbol, side,"
                    " target_weight, target_notional, target_quantity, broker_quantity,"
                    " requested_delta, approved_quantity, risk_reason_code, reference_price,"
                    " account_equity, external_exposure, budget_fraction, bar_timestamp,"
                    " decided_at) VALUES (?, 'eda1', 'PAPER', ?, ?, 'C', ?, ?, '0.090000000000',"
                    " '8958.54', '27.5', '20', '7.5', '7.5', 'APPROVED', '325.6', '99539.34',"
                    " '0', '0.90', ?, ?)",
                    (
                        f"autotrader-{index}",
                        POLICY_ID,
                        POLICY_HASH,
                        symbol,
                        side,
                        bar,
                        NOW.isoformat(),
                    ),
                )


@pytest.fixture
def paper_db(tmp_path: Path) -> Path:
    path = tmp_path / "equity-paper.db"
    _write(path)
    return path


@pytest.fixture
def crypto_db(tmp_path: Path) -> Path:
    path = tmp_path / "crypto.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE positions (symbol TEXT PRIMARY KEY, quantity TEXT, average_price REAL,"
            " updated_at TEXT)"
        )
    return path


def page(paper_db: Path, crypto_db: Path) -> equity_paper.EquityPaperOverview:
    return build_overview(path=paper_db, crypto_path=crypto_db, now=NOW)


# ==========================================================================
# Policy
# ==========================================================================


def test_the_policy_figures_are_the_deployed_fractional_policy(
    paper_db: Path, crypto_db: Path
) -> None:
    policy = page(paper_db, crypto_db).policy

    assert policy is not None
    assert policy.policy_id == POLICY_ID
    assert policy.authoritative is True
    assert policy.source == POLICY_SOURCE_RUNTIME
    assert policy.config_hash == POLICY_HASH[:12]
    assert policy.target_gross == pytest.approx(0.90)
    assert policy.hard_gross_cap == pytest.approx(0.95)
    assert policy.hard_symbol_cap == pytest.approx(0.11)
    assert policy.cash_reserve_target == pytest.approx(0.10)
    assert policy.target_slot_weight == pytest.approx(0.09)
    assert policy.universe_size == 10
    assert policy.fractional is True
    assert policy.daily_loss_halt == pytest.approx(0.02)


def test_the_stale_legacy_limits_never_reach_the_policy_panel(
    paper_db: Path, crypto_db: Path
) -> None:
    """Under the fractional policy, 5% and 30% are not this account's limits."""
    policy = page(paper_db, crypto_db).policy

    assert policy is not None
    assert policy.hard_symbol_cap != pytest.approx(0.05)
    assert policy.hard_gross_cap != pytest.approx(0.30)
    assert policy.target_gross > 0.30
    exposure = page(paper_db, crypto_db).exposure
    assert exposure.per_symbol_cap == "11%"
    assert exposure.total_account_cap == "95%"
    assert exposure.target_account_gross == "90%"
    assert exposure.cash_reserve_target == "10%"


def test_an_unreadable_policy_name_is_a_labelled_fallback_not_a_claim(
    tmp_path: Path, crypto_db: Path
) -> None:
    path = tmp_path / "no-policy.db"
    _write(path, policy_line=None)

    policy = page(path, crypto_db).policy

    assert policy is not None
    assert policy.authoritative is False
    assert policy.source == POLICY_SOURCE_FALLBACK
    assert policy.policy_id == equity_paper.FALLBACK_POLICY_ID
    assert policy.config_hash is None
    assert "not authoritative" in policy.note


# ==========================================================================
# Target vs actual
# ==========================================================================


def test_a_long_symbol_carries_its_recorded_target(paper_db: Path, crypto_db: Path) -> None:
    rows = {row.symbol: row for row in page(paper_db, crypto_db).targets}

    aapl = rows["AAPL"]
    assert aapl.stance_label == "LONG"
    assert aapl.target_weight == pytest.approx(0.09)
    assert aapl.target_source == TARGET_SOURCE_RECORDED
    assert aapl.target_notional == pytest.approx(8958.54)
    assert aapl.target_quantity == "27.5"
    assert aapl.target_external_exposure == pytest.approx(0.0)
    assert aapl.last_order_side == "BUY"
    assert aapl.actual_quantity == "27.5"


def test_a_flat_symbol_targets_zero_and_says_so(paper_db: Path, crypto_db: Path) -> None:
    tsla = next(row for row in page(paper_db, crypto_db).targets if row.symbol == "TSLA")

    assert tsla.stance_label == "FLAT"
    assert tsla.target_weight == 0.0
    assert tsla.target_source == TARGET_SOURCE_FLAT
    assert tsla.target_notional is None
    assert tsla.actual_quantity == "0"


def test_a_long_symbol_with_no_recorded_decision_is_not_given_a_target(
    paper_db: Path, crypto_db: Path
) -> None:
    googl = next(row for row in page(paper_db, crypto_db).targets if row.symbol == "GOOGL")

    assert googl.stance_label == "LONG"
    assert googl.target_weight is None
    assert googl.target_source == TARGET_SOURCE_NOT_RECORDED


def test_the_action_is_the_decided_side_only_on_the_latest_bar(
    paper_db: Path, crypto_db: Path
) -> None:
    rows = {row.symbol: row for row in page(paper_db, crypto_db).targets}

    assert rows["META"].action == "SELL"
    assert rows["META"].target_bar_timestamp == LATEST_BAR
    assert rows["AAPL"].action == "HOLD"
    assert rows["AAPL"].target_bar_timestamp == OLDER_BAR
    assert rows["GOOGL"].action == "HOLD"


def test_a_store_without_the_target_table_still_serves_the_page(
    tmp_path: Path, crypto_db: Path
) -> None:
    path = tmp_path / "legacy.db"
    _write(path, targets=False)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE equity_paper_targets")

    overview = page(path, crypto_db)

    assert overview.policy is not None and overview.policy.authoritative is True
    assert all(row.target_source != TARGET_SOURCE_RECORDED for row in overview.targets)


# ==========================================================================
# Routes
# ==========================================================================


@pytest.fixture
def client(paper_db: Path, crypto_db: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(equity_paper.PAPER_DATABASE_PATH_ENV, str(paper_db))
    monkeypatch.setenv(equity_paper.CRYPTO_DATABASE_PATH_ENV, str(crypto_db))
    with TestClient(equity_paper_api.create_app()) as test_client:
        yield test_client


def test_the_policy_route_serializes_the_numbers(client: TestClient) -> None:
    payload = client.get("/api/equity-paper/policy").json()

    assert payload["target_gross"] == pytest.approx(0.90)
    assert payload["hard_gross_cap"] == pytest.approx(0.95)
    assert payload["hard_symbol_cap"] == pytest.approx(0.11)
    assert payload["authoritative"] is True
    overview = client.get("/api/equity-paper/overview").json()
    assert overview["policy"] == payload
    assert overview["targets"][3]["target_weight"] == pytest.approx(0.09)


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_the_policy_route_accepts_no_write(client: TestClient, method: str) -> None:
    assert getattr(client, method)("/api/equity-paper/policy").status_code == 405


def test_the_paper_api_still_has_no_write_surface() -> None:
    application = equity_paper_api.create_app()
    for route in application.routes:
        methods = set(getattr(route, "methods", set()) or set())
        assert not methods & {"POST", "PUT", "PATCH", "DELETE"}, getattr(route, "path", route)
        segments = {segment.lower() for segment in str(getattr(route, "path", "")).split("/")}
        assert not segments & {"submit", "cancel", "start", "stop", "set", "update", "execute"}
