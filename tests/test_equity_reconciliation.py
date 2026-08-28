"""Equity V0.2: the reconciliation boundary between the two books.

One Alpaca account holds both products, so reconciliation has to answer two
different questions with one pass:

    which *positions* does this runner manage, and are its local snapshots
    right?

    is there any *order* on this account whose outcome nobody knows?

The first is scoped - a crypto runner has no equity snapshot to repair and vice
versa - and the second is not, because an ambiguous `client_order_id` blocks
trading for the whole account no matter which product created it.

Offline. Only the broker transport is faked.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from autotrader.equity import EQUITY_SYMBOLS
from autotrader.execution.models import SUPPORTED_SYMBOLS
from autotrader.reconciliation import (
    ItemOutcome,
    ReconciliationInputError,
    ReconciliationStatus,
    reconcile_paper_state,
)
from autotrader.runtime.safety import (
    STARTUP_SAFETY_SAFE,
    startup_safety_from_reconciliation,
)
from autotrader.state.sqlite import (
    INTENT_STATUS_UNKNOWN,
    connect,
    get_position,
    initialize_database,
    record_order_intent,
    upsert_position,
)
from test_equity_execution import FakeTradingClient, api_error, make_position

T0 = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    database = tmp_path / "state.db"
    initialize_database(database)
    with connect(database) as open_connection:
        yield open_connection


def crypto_position(symbol: str = "BTCUSD", qty: str = "0.5") -> object:
    """A broker position outside the equity universe."""
    position = make_position(symbol=symbol, qty=qty, market_value="30000")
    return position


def run(connection: sqlite3.Connection, client: FakeTradingClient, **kwargs: object):
    return reconcile_paper_state(
        connection,
        trading_client=client,  # type: ignore[arg-type]
        now=T0,
        sleep=lambda _: None,
        recheck_delay_seconds=0.0,
        **kwargs,  # type: ignore[arg-type]
    )


# ==========================================================================
# Position scope
# ==========================================================================


def test_an_equity_pass_reconciles_the_ten_equity_positions(
    connection: sqlite3.Connection,
) -> None:
    client = FakeTradingClient(positions=[make_position(symbol="SPY", qty="7")])

    result = run(connection, client, symbols=EQUITY_SYMBOLS)

    assert result.status is ReconciliationStatus.REPAIRED
    assert result.safe_to_trade is True
    assert result.positions_checked == 10
    stored = get_position(connection, "SPY")
    assert stored is not None
    assert stored.quantity == Decimal(7)


def test_an_equity_pass_observes_a_crypto_holding_without_touching_it(
    connection: sqlite3.Connection,
) -> None:
    """Real exposure this runner does not manage: written down, never traded."""
    client = FakeTradingClient(positions=[crypto_position()])

    result = run(connection, client, symbols=EQUITY_SYMBOLS)

    assert result.safe_to_trade is True
    observed = [issue for issue in result.issues if issue.outcome is ItemOutcome.OBSERVED]
    assert any("BTCUSD" in issue.detail for issue in observed)
    assert get_position(connection, "BTC/USD") is None


def test_the_crypto_default_is_unchanged_and_observes_an_equity_holding(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL: the existing pass behaves exactly as it did."""
    client = FakeTradingClient(positions=[make_position(symbol="SPY", qty="7")])

    result = run(connection, client)

    assert result.positions_checked == len(SUPPORTED_SYMBOLS) == 2
    observed = [issue for issue in result.issues if issue.outcome is ItemOutcome.OBSERVED]
    assert any("SPY" in issue.detail for issue in observed)
    assert get_position(connection, "SPY") is None


def test_a_stale_equity_snapshot_is_repaired_from_the_broker(
    connection: sqlite3.Connection,
) -> None:
    upsert_position(
        connection, symbol="SPY", quantity=Decimal(99), average_price=1.0, updated_at=T0
    )
    client = FakeTradingClient(positions=[make_position(symbol="SPY", qty="7")])

    result = run(connection, client, symbols=EQUITY_SYMBOLS)

    assert result.status is ReconciliationStatus.REPAIRED
    stored = get_position(connection, "SPY")
    assert stored is not None
    assert stored.quantity == Decimal(7)


def test_an_equity_position_the_broker_no_longer_holds_is_flattened_locally(
    connection: sqlite3.Connection,
) -> None:
    upsert_position(
        connection, symbol="TSLA", quantity=Decimal(4), average_price=300.0, updated_at=T0
    )
    client = FakeTradingClient(positions=[])

    result = run(connection, client, symbols=EQUITY_SYMBOLS)

    assert result.safe_to_trade is True
    stored = get_position(connection, "TSLA")
    assert stored is not None
    assert stored.quantity == Decimal(0)


def test_an_empty_universe_is_refused(connection: sqlite3.Connection) -> None:
    """ "Nothing was checked" is not the same answer as "everything matched"."""
    with pytest.raises(ReconciliationInputError):
        run(connection, FakeTradingClient(), symbols=())


# ==========================================================================
# Order scope: the account, not the product
# ==========================================================================


def make_unknown_intent(connection: sqlite3.Connection, symbol: str) -> str:
    client_order_id = f"autotrader-unknown-{symbol.replace('/', '')}"
    intent_id = record_order_intent(
        connection,
        client_order_id=client_order_id,
        created_at=T0,
        symbol=symbol,
        side="BUY",
        requested_quantity=Decimal(10),
        approved_quantity=Decimal(10),
        reference_price=500.0,
        risk_reason_code="APPROVED",
        status=INTENT_STATUS_UNKNOWN,
    )
    assert intent_id > 0
    return client_order_id


def test_an_unknown_equity_intent_blocks_the_crypto_pass_too(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL: one account, one client_order_id namespace, one answer.

    A crypto runner starting while an equity order's outcome is unknown must
    not be told it is safe to trade. That is the property the combined system
    depends on, and it already holds because order intents are never filtered
    by universe.
    """
    make_unknown_intent(connection, "SPY")
    client = FakeTradingClient(orders={"autotrader-unknown-SPY": api_error(500, "upstream")})

    crypto_result = run(connection, client)
    equity_result = run(connection, client, symbols=EQUITY_SYMBOLS)

    assert crypto_result.safe_to_trade is False
    assert equity_result.safe_to_trade is False
    assert crypto_result.status is ReconciliationStatus.UNRESOLVED


def test_an_unknown_crypto_intent_blocks_the_equity_pass_too(
    connection: sqlite3.Connection,
) -> None:
    make_unknown_intent(connection, "BTC/USD")
    client = FakeTradingClient(orders={"autotrader-unknown-BTCUSD": api_error(500, "upstream")})

    equity_result = run(connection, client, symbols=EQUITY_SYMBOLS)

    assert equity_result.safe_to_trade is False


def test_an_equity_intent_the_broker_never_received_is_closed_off(
    connection: sqlite3.Connection,
) -> None:
    """Resolved by asking, never by submitting a replacement."""
    from autotrader.state.sqlite import INTENT_STATUS_CONFIRMED_NOT_SUBMITTED, list_order_intents

    make_unknown_intent(connection, "SPY")
    client = FakeTradingClient(orders={"autotrader-unknown-SPY": api_error(404, "not found")})

    result = run(connection, client, symbols=EQUITY_SYMBOLS)

    assert result.safe_to_trade is True
    [stored] = list_order_intents(connection)
    assert stored.status == INTENT_STATUS_CONFIRMED_NOT_SUBMITTED
    assert client.submit_calls == []


# ==========================================================================
# The startup-safety seam
# ==========================================================================


def test_the_startup_check_can_be_scoped_to_the_equity_universe(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam the equity runtime uses, asserted rather than assumed."""
    captured: dict[str, object] = {}

    def fake_pass(conn: sqlite3.Connection, **kwargs: object):
        captured.update(kwargs)
        return run(conn, FakeTradingClient(), symbols=EQUITY_SYMBOLS)

    import autotrader.runtime.safety as safety_module

    monkeypatch.setattr(safety_module, "reconcile_paper_state", fake_pass)
    check = startup_safety_from_reconciliation(connection, symbols=EQUITY_SYMBOLS)
    answer = check()

    assert captured["symbols"] == EQUITY_SYMBOLS
    assert answer.code == STARTUP_SAFETY_SAFE


def test_the_startup_check_without_a_scope_is_unchanged(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CRITICAL: the crypto runner's call passes no universe and gets the old pass."""
    captured: dict[str, object] = {}

    def fake_pass(conn: sqlite3.Connection, **kwargs: object):
        captured.update(kwargs)
        return run(conn, FakeTradingClient())

    import autotrader.runtime.safety as safety_module

    monkeypatch.setattr(safety_module, "reconcile_paper_state", fake_pass)
    startup_safety_from_reconciliation(connection)()

    assert "symbols" not in captured


def test_reconciliation_still_places_no_order_in_any_branch(
    connection: sqlite3.Connection,
) -> None:
    make_unknown_intent(connection, "SPY")
    upsert_position(
        connection, symbol="QQQ", quantity=Decimal(3), average_price=400.0, updated_at=T0
    )
    client = FakeTradingClient(
        positions=[make_position(symbol="SPY", qty="7"), crypto_position()],
        orders={"autotrader-unknown-SPY": api_error(404, "not found")},
    )

    run(connection, client, symbols=EQUITY_SYMBOLS)

    assert client.submit_calls == []
