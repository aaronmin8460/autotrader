"""`equity-submit`: the equity counterpart of `paper-submit`, same discipline.

The command is CLI wiring over `execute_equity_paper_order` - the boundary
`tests/test_equity_execution.py` already pins exhaustively - so these tests
cover exactly what the wiring adds: the two closed-by-default gates, the exit
codes (0 done, 1 refused, 2 UNKNOWN), the equity-only symbol surface, and the
promise that a dry run needs no gate and persists nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autotrader.cli import (
    EQUITY_SUBMIT_REFUSED_EXIT_CODE,
    EQUITY_SUBMIT_UNKNOWN_EXIT_CODE,
    app,
)
from autotrader.execution import equity as equity_execution
from autotrader.state.sqlite import connect, initialize_database, list_order_intents
from conftest import establish_account_safety
from test_equity_execution import (
    FakeDataClient,
    FakeTradingClient,
    api_error,
)

runner = CliRunner()

SYMBOL = "SPY"


@pytest.fixture(autouse=True)
def closed_gate_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTOTRADER_PAPER_TRADING_ENABLED", raising=False)


@pytest.fixture
def enabled_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOTRADER_PAPER_TRADING_ENABLED", "true")


@pytest.fixture
def patched_broker(monkeypatch: pytest.MonkeyPatch) -> FakeTradingClient:
    client = FakeTradingClient()
    monkeypatch.setattr(equity_execution, "create_paper_trading_client", lambda: client)
    monkeypatch.setattr(equity_execution, "create_market_data_client", lambda: FakeDataClient())
    return client


def cli_database(tmp_path: Path) -> Path:
    """A database in the state an operator submits from: reconciled and safe."""
    path = initialize_database(tmp_path / "cli.db")
    with connect(path) as setup:
        establish_account_safety(setup)
    return path


def open_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


def cli_args(tmp_path: Path, *extra: str, qty: str = "1", symbol: str = SYMBOL) -> list[str]:
    return [
        "equity-submit",
        "--symbol",
        symbol,
        "--side",
        "BUY",
        "--qty",
        qty,
        "--db",
        str(cli_database(tmp_path)),
        *extra,
    ]


# ==========================================================================
# Gates
# ==========================================================================


def test_the_environment_gate_is_closed_by_default(
    tmp_path: Path, patched_broker: FakeTradingClient
) -> None:
    result = runner.invoke(app, cli_args(tmp_path, "--confirm-paper", "PAPER"))

    assert result.exit_code == EQUITY_SUBMIT_REFUSED_EXIT_CODE, result.output
    assert "AUTOTRADER_PAPER_TRADING_ENABLED" in result.output
    assert patched_broker.submit_calls == []


def test_the_confirmation_token_is_required(
    tmp_path: Path, enabled_gate: None, patched_broker: FakeTradingClient
) -> None:
    result = runner.invoke(app, cli_args(tmp_path))

    assert result.exit_code == EQUITY_SUBMIT_REFUSED_EXIT_CODE, result.output
    assert patched_broker.submit_calls == []


def test_a_wrong_token_is_refused(
    tmp_path: Path, enabled_gate: None, patched_broker: FakeTradingClient
) -> None:
    result = runner.invoke(app, cli_args(tmp_path, "--confirm-paper", "paper"))

    assert result.exit_code == EQUITY_SUBMIT_REFUSED_EXIT_CODE, result.output
    assert patched_broker.submit_calls == []


# ==========================================================================
# Dry run
# ==========================================================================


def test_a_dry_run_needs_no_gate_and_persists_nothing(
    tmp_path: Path, patched_broker: FakeTradingClient
) -> None:
    database = cli_database(tmp_path)
    result = runner.invoke(
        app,
        [
            "equity-submit",
            "--symbol",
            SYMBOL,
            "--side",
            "BUY",
            "--qty",
            "1",
            "--dry-run",
            "--db",
            str(database),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER EQUITY ORDER" in result.output
    assert "US EQUITY, REGULAR SESSION ONLY" in result.output
    assert "DRY RUN" in result.output
    assert patched_broker.submit_calls == []
    with connect(database) as connection:
        assert list_order_intents(connection) == []


def test_a_dry_run_shows_the_risk_answer(tmp_path: Path, patched_broker: FakeTradingClient) -> None:
    result = runner.invoke(app, cli_args(tmp_path, "--dry-run"))

    assert result.exit_code == 0, result.output
    assert "Risk Decision" in result.output
    assert "Client Order ID" in result.output


# ==========================================================================
# Symbol surface
# ==========================================================================


def test_a_crypto_pair_is_refused_before_any_broker_contact(tmp_path: Path) -> None:
    """No fake is installed: a refusal this early needs no client at all."""
    result = runner.invoke(app, cli_args(tmp_path, "--dry-run", symbol="BTC/USD"))

    assert result.exit_code == EQUITY_SUBMIT_REFUSED_EXIT_CODE, result.output


def test_an_unknown_ticker_is_refused(tmp_path: Path) -> None:
    result = runner.invoke(app, cli_args(tmp_path, "--dry-run", symbol="VOO"))

    assert result.exit_code == EQUITY_SUBMIT_REFUSED_EXIT_CODE, result.output


# ==========================================================================
# Submission outcomes
# ==========================================================================


def test_a_submission_with_both_gates_open_is_at_most_once(
    tmp_path: Path, enabled_gate: None, patched_broker: FakeTradingClient
) -> None:
    database = cli_database(tmp_path)
    result = runner.invoke(
        app,
        [
            "equity-submit",
            "--symbol",
            SYMBOL,
            "--side",
            "BUY",
            "--qty",
            "1",
            "--confirm-paper",
            "PAPER",
            "--db",
            str(database),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "SUBMITTED TO PAPER ACCOUNT" in result.output
    assert "Accepted is not filled" in result.output
    assert len(patched_broker.submit_calls) == 1
    assert patched_broker.clock_calls >= 1, "the session gate must consult the broker clock"
    with connect(database) as connection:
        [intent] = list_order_intents(connection)
    assert intent.symbol == SYMBOL
    assert patched_broker.submit_calls[0].client_order_id == intent.client_order_id


def test_a_closed_session_is_a_refusal_with_no_intent(
    tmp_path: Path,
    enabled_gate: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeTradingClient(is_open=False)
    monkeypatch.setattr(equity_execution, "create_paper_trading_client", lambda: client)
    monkeypatch.setattr(equity_execution, "create_market_data_client", lambda: FakeDataClient())
    database = cli_database(tmp_path)

    result = runner.invoke(
        app,
        [
            "equity-submit",
            "--symbol",
            SYMBOL,
            "--side",
            "BUY",
            "--qty",
            "1",
            "--confirm-paper",
            "PAPER",
            "--db",
            str(database),
        ],
    )

    assert result.exit_code == EQUITY_SUBMIT_REFUSED_EXIT_CODE, result.output
    assert "regular market session is not open" in result.output
    assert client.submit_calls == []
    with connect(database) as connection:
        assert list_order_intents(connection) == []


def test_an_unknown_outcome_exits_two_and_says_do_not_retry(
    tmp_path: Path,
    enabled_gate: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CRITICAL. Exit code 2 is reserved for 'an order may exist at the broker'."""
    client = FakeTradingClient(submit_error=api_error(504, "gateway timeout"))
    monkeypatch.setattr(equity_execution, "create_paper_trading_client", lambda: client)
    monkeypatch.setattr(equity_execution, "create_market_data_client", lambda: FakeDataClient())
    database = cli_database(tmp_path)

    result = runner.invoke(
        app,
        [
            "equity-submit",
            "--symbol",
            SYMBOL,
            "--side",
            "BUY",
            "--qty",
            "1",
            "--confirm-paper",
            "PAPER",
            "--db",
            str(database),
        ],
    )

    assert result.exit_code == EQUITY_SUBMIT_UNKNOWN_EXIT_CODE, result.output
    assert len(client.submit_calls) == 1, "an UNKNOWN outcome must never be retried"


def test_a_risk_rejection_exits_one_and_touches_no_broker(
    tmp_path: Path,
    enabled_gate: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SELL while flat is refused by risk before any order can exist."""
    client = FakeTradingClient()
    monkeypatch.setattr(equity_execution, "create_paper_trading_client", lambda: client)
    monkeypatch.setattr(equity_execution, "create_market_data_client", lambda: FakeDataClient())
    database = cli_database(tmp_path)

    result = runner.invoke(
        app,
        [
            "equity-submit",
            "--symbol",
            SYMBOL,
            "--side",
            "SELL",
            "--qty",
            "1",
            "--confirm-paper",
            "PAPER",
            "--db",
            str(database),
        ],
    )

    assert result.exit_code == EQUITY_SUBMIT_REFUSED_EXIT_CODE, result.output
    assert "REJECTED BY RISK ENGINE" in result.output
    assert client.submit_calls == []
    with connect(database) as connection:
        assert list_order_intents(connection) == []


# ==========================================================================
# Help surface
# ==========================================================================


def test_the_help_names_paper_only_and_both_gates() -> None:
    result = runner.invoke(app, ["equity-submit", "--help"])

    assert result.exit_code == 0, result.output
    assert "PAPER" in result.output
