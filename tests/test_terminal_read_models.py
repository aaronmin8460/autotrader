"""Terminal V4 read models: authoritative detail without a mutation surface."""

from __future__ import annotations

import ast
import inspect
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from autotrader.dashboard import (
    live_accounting_api,
    live_api,
    live_history,
    live_terminal,
)
from autotrader.equity.paper import create_paper_target_table
from autotrader.equity.shadow import create_side_by_side_tables
from autotrader.live.identity import account_fingerprint
from autotrader.liveaccounting import service, store
from autotrader.liveaccounting.models import (
    CHECKPOINT_INTRADAY,
    CONFIRMATION_CONFIRMED,
    FLOW_EXTERNAL_DEPOSIT,
    RELATION_SINCE_INCEPTION,
    ProfitReservePolicy,
)
from autotrader.state.sqlite import initialize_database

NOW = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
ACCOUNT_NUMBER = "terminal-fixture-account"
FINGERPRINT = account_fingerprint(ACCOUNT_NUMBER)


class _Account:
    account_number = ACCOUNT_NUMBER
    equity = "50.00"


class _Position:
    symbol = "SPY"
    qty = "0.01"
    avg_entry_price = "748.00"
    current_price = "750.00"
    market_value = "7.50"
    unrealized_pl = "0.02"
    unrealized_plpc = "0.002673796791"
    change_today = "0.001"


class _Broker:
    def get_account(self) -> object:
        return _Account()

    def get_all_positions(self) -> tuple[object, ...]:
        return (_Position(),)


class _SmallerPosition:
    symbol = "AAPL"
    qty = "0.004"
    avg_entry_price = "245.00"
    current_price = "250.00"
    market_value = "1.00"
    unrealized_pl = "0.02"
    unrealized_plpc = "0.020408163265"
    change_today = "0.001"


class _MultiPositionBroker(_Broker):
    def get_all_positions(self) -> tuple[object, ...]:
        return (_SmallerPosition(), _Position())


def _operational_database(path: Path) -> Path:
    initialize_database(path)
    with sqlite3.connect(path) as connection:
        create_side_by_side_tables(connection)
        create_paper_target_table(connection)
        stamp = NOW.isoformat()
        connection.execute(
            "INSERT INTO shadow_regime_state VALUES (?, 1, 750, 700, -0.02, 500, 200, "
            "-0.05, 1, 'SPY', ?)",
            (NOW.date().isoformat(), stamp),
        )
        connection.execute(
            "INSERT INTO strategy_runs (strategy_name, mode, status, started_at, ended_at, "
            "created_at) VALUES ('EDA-1', 'LIVE', 'COMPLETED', ?, ?, ?)",
            (stamp, stamp, stamp),
        )
        connection.execute(
            "INSERT INTO equity_paper_targets (client_order_id, engine, environment, "
            "sizing_policy, sizing_config_hash, rollout_stage, symbol, side, target_weight, "
            "target_notional, target_quantity, broker_quantity, requested_delta, "
            "approved_quantity, risk_reason_code, reference_price, account_equity, "
            "external_exposure, budget_fraction, bar_timestamp, decided_at) VALUES "
            "('autotrader-live-fixture', 'eda1', 'LIVE', 'LIVE_VALIDATION_100', 'hash', "
            "'A', 'SPY', 'BUY', '0.15', '7.50', '0.01', '0', '0.01', '0.01', "
            "'APPROVED', '749', '50', '0', '0.90', ?, ?)",
            (stamp, stamp),
        )
        connection.execute(
            "INSERT INTO order_intents (client_order_id, strategy_run_id, created_at, "
            "symbol, side, requested_quantity, approved_quantity, reference_price, "
            "risk_reason_code, status, updated_at) VALUES "
            "('autotrader-live-fixture', 1, ?, 'SPY', 'BUY', '0.01', '0.01', 749, "
            "'APPROVED', 'SUBMITTED', ?)",
            (stamp, stamp),
        )
        connection.execute(
            "INSERT INTO broker_orders (order_intent_id, broker_order_id, client_order_id, "
            "symbol, side, quantity, filled_quantity, filled_average_price, status, "
            "submitted_at, filled_at, updated_at, created_at) VALUES "
            "(1, 'broker-fixture', 'autotrader-live-fixture', 'SPY', 'BUY', '0.01', "
            "'0.01', 750, 'FILLED', ?, ?, ?, ?)",
            (
                stamp,
                (NOW + timedelta(milliseconds=250)).isoformat(),
                stamp,
                stamp,
            ),
        )
        connection.execute(
            "INSERT INTO system_events (event_timestamp, event_type, message, created_at) "
            "VALUES (?, 'CYCLE_COMPLETED', 'api_key=must-not-leak', ?)",
            (stamp, stamp),
        )
    return path


def _accounting_database(path: Path) -> Path:
    policy = ProfitReservePolicy()
    with sqlite3.connect(path, isolation_level=None) as connection:
        connection.row_factory = sqlite3.Row
        store.initialize(connection)
        store.stamp_metadata(connection, account_fingerprint=FINGERPRINT, now=NOW)
        service.establish_inception(
            connection,
            service.BrokerSnapshot(NOW, Decimal("50"), Decimal("50"), 0, 0),
            account_fingerprint=FINGERPRINT,
            policy=policy,
            now=NOW,
        )
        flow_at = NOW + timedelta(minutes=5)
        store.record_flow(
            connection,
            broker_activity_id="deposit-fixture",
            account_fingerprint=FINGERPRINT,
            activity_type="CSD",
            broker_status="executed",
            classification=FLOW_EXTERNAL_DEPOSIT,
            classification_reason=None,
            confirmation=CONFIRMATION_CONFIRMED,
            amount=Decimal("50"),
            currency="USD",
            settle_at=flow_at,
            relation=RELATION_SINCE_INCEPTION,
            source_digest="0" * 64,
            now=flow_at,
        )
        service.take_checkpoint(
            connection,
            service.BrokerSnapshot(
                NOW + timedelta(minutes=10), Decimal("103"), Decimal("103"), 0, 0
            ),
            account_fingerprint=FINGERPRINT,
            kind=CHECKPOINT_INTRADAY,
            now=NOW + timedelta(minutes=10),
        )
    return path


def test_live_terminal_quotes_positions_decisions_and_execution(tmp_path: Path) -> None:
    result = live_terminal.build_terminal(
        now=NOW,
        path=_operational_database(tmp_path / "live.db"),
        broker=_Broker(),
        expected_fingerprint=FINGERPRINT,
    )
    assert result["read_only"] is True
    assert result["operational_status"] == "CLEAN"
    assert result["strategy"]["regime"]["state"] == "PARTICIPATE"  # type: ignore[index]
    position = result["positions"]["rows"][0]  # type: ignore[index]
    assert position["symbol"] == "SPY"
    assert position["market_value"] == "7.50"
    assert position["target_weight"] == "0.15"
    assert result["positions"]["largest_position"] == position  # type: ignore[index]
    metrics = result["execution"]
    assert metrics["order_count"] == 1  # type: ignore[index]
    assert metrics["fill_rate"] == "1"  # type: ignore[index]
    assert metrics["slippage_sample_size"] == 1  # type: ignore[index]
    assert metrics["average_fill_latency_ms"] == "250.0"  # type: ignore[index]
    assert "must-not-leak" not in repr(result)
    assert "[REDACTED]" in repr(result)


def test_live_terminal_refuses_positions_from_an_unpinned_account(tmp_path: Path) -> None:
    result = live_terminal.build_terminal(
        now=NOW,
        path=_operational_database(tmp_path / "live.db"),
        broker=_Broker(),
        expected_fingerprint="0" * 32,
    )
    assert result["positions"] == {
        "status": "ACCOUNT_MISMATCH",
        "as_of": None,
        "largest_position": None,
        "rows": [],
    }


def test_live_terminal_identifies_largest_position_by_absolute_market_value(
    tmp_path: Path,
) -> None:
    result = live_terminal.build_terminal(
        now=NOW,
        path=_operational_database(tmp_path / "live.db"),
        broker=_MultiPositionBroker(),
        expected_fingerprint=FINGERPRINT,
    )
    assert [row["symbol"] for row in result["positions"]["rows"]] == [  # type: ignore[index]
        "AAPL",
        "SPY",
    ]
    assert result["positions"]["largest_position"]["symbol"] == "SPY"  # type: ignore[index]


def test_accounting_history_adjusts_deposit_out_of_performance(tmp_path: Path) -> None:
    result = live_history.build_history(
        now=NOW + timedelta(minutes=10),
        path=_accounting_database(tmp_path / "accounting.db"),
    )
    assert result["status"] == "CLEAN"
    assert result["sample_size"] == 2
    assert result["points"][-1]["broker_equity"] == "103.00"  # type: ignore[index]
    assert result["points"][-1]["flow_adjusted_equity"] == "53.00"  # type: ignore[index]
    assert result["points"][-1]["trading_pnl"] == "3.00"  # type: ignore[index]
    assert result["today_trading_pnl"] == "3.00"
    assert result["flows"][0]["classification"] == FLOW_EXTERNAL_DEPOSIT  # type: ignore[index]


def test_new_routes_are_get_only(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(live_terminal, "build_terminal", lambda **_: {"read_only": True})
    monkeypatch.setattr(live_history, "build_history", lambda **_: {"read_only": True})
    safety_client = TestClient(live_api.create_app())
    accounting_client = TestClient(live_accounting_api.create_app())
    assert safety_client.get("/api/live-safety/terminal").json()["read_only"] is True
    assert accounting_client.get("/api/live-accounting/history").json()["read_only"] is True
    assert safety_client.post("/api/live-safety/terminal").status_code == 405
    assert accounting_client.post("/api/live-accounting/history").status_code == 405


def _executable_source(module: object) -> str:
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_terminal_read_models_expose_no_mutation_call() -> None:
    code = _executable_source(live_terminal) + _executable_source(live_history)
    for forbidden in (
        "submit_order",
        "cancel_order",
        "replace_order",
        "close_position",
        "create_transfer",
        "request_withdrawal",
        ".post(",
        ".put(",
        ".patch(",
        ".delete(",
    ):
        assert forbidden not in code
    assert "mode=ro" in inspect.getsource(live_terminal)
    assert "query_only" in inspect.getsource(live_terminal)
