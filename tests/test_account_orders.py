"""Account-wide recent orders: two stores, one list, every row labelled.

The defect these tests pin: the operations page's "recent orders" was the
crypto store's order table, so an account that had just placed twenty-eight
equity paper orders showed none of them. The merged read model reads both
stores, labels each row with the store it came from, sorts by the broker's own
time, deduplicates on broker identity, and takes no shadow record as input.
"""

from __future__ import annotations

import ast
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autotrader.dashboard import account_orders, equity_paper, equity_paper_api
from autotrader.dashboard.account_orders import (
    SOURCE_CRYPTO_PAPER,
    SOURCE_EQUITY_PAPER,
    AccountOrderRow,
    StoreSummary,
    build_account_orders,
    merge_orders,
    read_store_orders,
)

NOW = datetime(2026, 9, 2, 17, 0, tzinfo=UTC)

_SCHEMA = """
CREATE TABLE order_intents (
    id                 INTEGER PRIMARY KEY,
    client_order_id    TEXT NOT NULL UNIQUE,
    strategy_run_id    INTEGER,
    created_at         TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    side               TEXT NOT NULL,
    requested_quantity TEXT NOT NULL,
    approved_quantity  TEXT NOT NULL,
    reference_price    REAL NOT NULL,
    risk_reason_code   TEXT NOT NULL,
    status             TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE TABLE broker_orders (
    id                   INTEGER PRIMARY KEY,
    order_intent_id      INTEGER NOT NULL UNIQUE,
    broker_order_id      TEXT NOT NULL UNIQUE,
    client_order_id      TEXT NOT NULL UNIQUE,
    symbol               TEXT NOT NULL,
    side                 TEXT NOT NULL,
    quantity             TEXT NOT NULL,
    filled_quantity      TEXT NOT NULL,
    filled_average_price REAL,
    status               TEXT NOT NULL,
    submitted_at         TEXT,
    filled_at            TEXT,
    updated_at           TEXT NOT NULL,
    created_at           TEXT NOT NULL
);
"""


def _intent(
    connection: sqlite3.Connection,
    *,
    key: str,
    created_at: str,
    symbol: str,
    side: str,
    quantity: str,
    status: str = "SUBMITTED",
) -> int:
    cursor = connection.execute(
        "INSERT INTO order_intents (client_order_id, created_at, symbol, side,"
        " requested_quantity, approved_quantity, reference_price, risk_reason_code,"
        " status, updated_at) VALUES (?, ?, ?, ?, ?, ?, 100.0, 'APPROVED', ?, ?)",
        (key, created_at, symbol, side, quantity, quantity, status, created_at),
    )
    return int(cursor.lastrowid or 0)


def _broker(
    connection: sqlite3.Connection,
    *,
    intent_id: int,
    key: str,
    broker_id: str,
    symbol: str,
    side: str,
    quantity: str,
    submitted_at: str,
    status: str = "filled",
    price: float = 100.0,
) -> None:
    connection.execute(
        "INSERT INTO broker_orders (order_intent_id, broker_order_id, client_order_id, symbol,"
        " side, quantity, filled_quantity, filled_average_price, status, submitted_at,"
        " filled_at, updated_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            intent_id,
            broker_id,
            key,
            symbol,
            side,
            quantity,
            quantity if status == "filled" else "0",
            price,
            status,
            submitted_at,
            submitted_at if status == "filled" else None,
            submitted_at,
            submitted_at,
        ),
    )


@pytest.fixture
def crypto_db(tmp_path: Path) -> Path:
    path = tmp_path / "crypto.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(_SCHEMA)
        btc = _intent(
            connection,
            key="autotrader-btc-buy",
            created_at="2026-09-02T06:00:05+00:00",
            symbol="BTC/USD",
            side="BUY",
            quantity="0.064126422",
        )
        _broker(
            connection,
            intent_id=btc,
            key="autotrader-btc-buy",
            broker_id="broker-btc-buy",
            symbol="BTC/USD",
            side="BUY",
            quantity="0.064126422",
            submitted_at="2026-09-02T06:00:05.5+00:00",
            price=77630.5,
        )
        eth = _intent(
            connection,
            key="autotrader-eth-sell",
            created_at="2026-09-02T08:45:05+00:00",
            symbol="ETH/USD",
            side="SELL",
            quantity="2.010072429",
        )
        _broker(
            connection,
            intent_id=eth,
            key="autotrader-eth-sell",
            broker_id="broker-eth-sell",
            symbol="ETH/USD",
            side="SELL",
            quantity="2.010072429",
            submitted_at="2026-09-02T08:45:05.4+00:00",
            price=2448.5,
        )
    return path


@pytest.fixture
def paper_db(tmp_path: Path) -> Path:
    path = tmp_path / "paper.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(_SCHEMA)
        nvda = _intent(
            connection,
            key="autotrader-nvda-sell",
            created_at="2026-09-02T15:17:00+00:00",
            symbol="NVDA",
            side="SELL",
            quantity="0.620603768",
        )
        _broker(
            connection,
            intent_id=nvda,
            key="autotrader-nvda-sell",
            broker_id="broker-nvda-sell",
            symbol="NVDA",
            side="SELL",
            quantity="0.620603768",
            submitted_at="2026-09-02T15:18:52+00:00",
            price=226.696,
        )
        meta = _intent(
            connection,
            key="autotrader-meta-buy",
            created_at="2026-09-02T16:02:00+00:00",
            symbol="META",
            side="BUY",
            quantity="0.174703844",
        )
        _broker(
            connection,
            intent_id=meta,
            key="autotrader-meta-buy",
            broker_id="broker-meta-buy",
            symbol="META",
            side="BUY",
            quantity="0.174703844",
            submitted_at="2026-09-02T16:03:53+00:00",
            price=593.152,
        )
        # The broker never answered for this one. It has no broker row and it
        # is the row an operator most needs to see.
        _intent(
            connection,
            key="autotrader-tsla-unknown",
            created_at="2026-09-02T16:32:00+00:00",
            symbol="TSLA",
            side="BUY",
            quantity="0.25",
            status="UNKNOWN",
        )
    return path


def merged(crypto_db: Path, paper_db: Path, **kwargs: object) -> account_orders.AccountOrdersPanel:
    return build_account_orders(crypto_path=crypto_db, paper_path=paper_db, now=NOW, **kwargs)  # type: ignore[arg-type]


# ==========================================================================
# The merge
# ==========================================================================


def test_equity_paper_orders_are_visible_beside_crypto_orders(
    crypto_db: Path, paper_db: Path
) -> None:
    panel = merged(crypto_db, paper_db)

    symbols = [row.symbol for row in panel.rows]
    assert "META" in symbols and "NVDA" in symbols, "equity paper orders are missing"
    assert "BTC/USD" in symbols and "ETH/USD" in symbols, "crypto orders are missing"
    assert panel.total == 5


def test_rows_are_sorted_by_the_authoritative_time_newest_first(
    crypto_db: Path, paper_db: Path
) -> None:
    """Broker submission time when the broker answered; intent creation otherwise."""
    panel = merged(crypto_db, paper_db)

    assert [row.symbol for row in panel.rows] == ["TSLA", "META", "NVDA", "ETH/USD", "BTC/USD"]
    stamps = [row.authoritative_at for row in panel.rows]
    assert stamps == sorted(stamps, reverse=True)
    unknown = panel.rows[0]
    assert unknown.submitted_at is None
    assert unknown.authoritative_at == unknown.created_at
    assert panel.rows[1].authoritative_at == panel.rows[1].submitted_at


def test_every_row_is_labelled_with_the_store_it_came_from(crypto_db: Path, paper_db: Path) -> None:
    panel = merged(crypto_db, paper_db)

    by_symbol = {row.symbol: row for row in panel.rows}
    assert by_symbol["META"].source == SOURCE_EQUITY_PAPER
    assert by_symbol["NVDA"].source == SOURCE_EQUITY_PAPER
    assert by_symbol["TSLA"].source == SOURCE_EQUITY_PAPER
    assert by_symbol["BTC/USD"].source == SOURCE_CRYPTO_PAPER
    assert by_symbol["ETH/USD"].source == SOURCE_CRYPTO_PAPER
    assert by_symbol["META"].asset_class == "EQUITY"
    assert by_symbol["BTC/USD"].asset_class == "CRYPTO"
    assert {row.source for row in panel.rows} <= set(account_orders.ORDER_SOURCES)


def test_no_row_is_simulated_and_the_panel_says_so(crypto_db: Path, paper_db: Path) -> None:
    panel = merged(crypto_db, paper_db)

    assert panel.includes_simulated is False
    assert all(row.simulated is False for row in panel.rows)
    assert "No shadow or simulated action" in panel.note


def test_the_merge_takes_no_shadow_record_as_input() -> None:
    """The module never names a shadow table; it cannot read one by accident."""
    source = Path(account_orders.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    joined = " ".join(literals)
    for table in ("a1b_observations", "shadow_decisions", "shadow_side_by_side", "a1b_stance"):
        assert table not in joined, table
    assert "order_intents" in joined and "broker_orders" in joined


def test_an_unknown_intent_is_flagged_and_counted(crypto_db: Path, paper_db: Path) -> None:
    panel = merged(crypto_db, paper_db)

    unknown = next(row for row in panel.rows if row.symbol == "TSLA")
    assert unknown.status == "UNKNOWN"
    assert unknown.needs_attention is True
    assert unknown.broker_order_id is None
    assert panel.attention_count == 1
    filled = next(row for row in panel.rows if row.symbol == "META")
    assert filled.status == "FILLED"
    assert filled.status_tone == "POSITIVE"
    assert filled.status_source == "BROKER"
    assert filled.average_fill_price == 593.152
    assert filled.filled_quantity == "0.174703844"


# ==========================================================================
# Deduplication
# ==========================================================================


def test_a_broker_order_seen_in_both_stores_appears_once(crypto_db: Path, paper_db: Path) -> None:
    """Both stores snapshot the whole account; a shared broker id must not double."""
    with sqlite3.connect(crypto_db) as connection:
        copy = _intent(
            connection,
            key="autotrader-meta-buy-copy",
            created_at="2026-09-02T16:02:00+00:00",
            symbol="META",
            side="BUY",
            quantity="0.174703844",
        )
        _broker(
            connection,
            intent_id=copy,
            key="autotrader-meta-buy-copy",
            broker_id="broker-meta-buy",
            symbol="META",
            side="BUY",
            quantity="0.174703844",
            submitted_at="2026-09-02T16:03:53+00:00",
            price=593.152,
        )

    panel = merged(crypto_db, paper_db)

    assert [row.broker_order_id for row in panel.rows].count("broker-meta-buy") == 1
    assert panel.duplicates_dropped == 1
    assert len(panel.rows) == 5


def test_a_client_order_id_seen_twice_appears_once() -> None:
    def row(source: str, key: str, at: str) -> AccountOrderRow:
        return AccountOrderRow(
            client_order_id=key,
            broker_order_id=None,
            source=source,
            simulated=False,
            symbol="SPY",
            asset_class="EQUITY",
            side="BUY",
            quantity="1",
            filled_quantity=None,
            average_fill_price=None,
            status="SUBMITTED",
            status_tone="NEUTRAL",
            status_source="LOCAL",
            needs_attention=False,
            risk_reason_code="APPROVED",
            created_at=at,
            submitted_at=None,
            filled_at=None,
            authoritative_at=at,
        )

    summary = StoreSummary(
        source=SOURCE_EQUITY_PAPER, available=True, rows_read=1, total=1, attention_count=0
    )
    other = StoreSummary(
        source=SOURCE_CRYPTO_PAPER, available=True, rows_read=1, total=1, attention_count=0
    )
    panel = merge_orders(
        [
            (summary, [row(SOURCE_EQUITY_PAPER, "same-key", "2026-09-02T10:00:00+00:00")]),
            (other, [row(SOURCE_CRYPTO_PAPER, "same-key", "2026-09-02T10:00:00+00:00")]),
        ],
        now=NOW,
    )

    assert len(panel.rows) == 1
    assert panel.duplicates_dropped == 1


def test_no_duplicate_exists_in_a_normal_merge(crypto_db: Path, paper_db: Path) -> None:
    panel = merged(crypto_db, paper_db)

    broker_ids = [row.broker_order_id for row in panel.rows if row.broker_order_id]
    client_ids = [row.client_order_id for row in panel.rows]
    assert len(set(broker_ids)) == len(broker_ids)
    assert len(set(client_ids)) == len(client_ids)
    assert panel.duplicates_dropped == 0


def test_the_merge_is_deterministic_whatever_the_read_order(
    crypto_db: Path, paper_db: Path
) -> None:
    forward = merge_orders(
        [
            read_store_orders(crypto_db, source=SOURCE_CRYPTO_PAPER),
            read_store_orders(paper_db, source=SOURCE_EQUITY_PAPER),
        ],
        now=NOW,
    )
    backward = merge_orders(
        [
            read_store_orders(paper_db, source=SOURCE_EQUITY_PAPER),
            read_store_orders(crypto_db, source=SOURCE_CRYPTO_PAPER),
        ],
        now=NOW,
    )

    assert forward.rows == backward.rows


# ==========================================================================
# Bounds and unavailability
# ==========================================================================


def test_the_list_is_bounded_but_reports_the_true_total(crypto_db: Path, paper_db: Path) -> None:
    panel = merged(crypto_db, paper_db, limit=2)

    assert len(panel.rows) == 2
    assert panel.total == 5
    assert panel.limit == 2
    assert merged(crypto_db, paper_db, limit=10_000).limit == account_orders.MAX_LIMIT


def test_an_unreadable_store_is_reported_per_store_not_hidden(
    crypto_db: Path, tmp_path: Path
) -> None:
    panel = build_account_orders(crypto_path=crypto_db, paper_path=tmp_path / "absent.db", now=NOW)

    by_source = {summary.source: summary for summary in panel.stores}
    assert by_source[SOURCE_CRYPTO_PAPER].available is True
    assert by_source[SOURCE_EQUITY_PAPER].available is False
    assert by_source[SOURCE_EQUITY_PAPER].unavailable_reason == "DATABASE_UNREADABLE"
    assert [row.source for row in panel.rows] == [SOURCE_CRYPTO_PAPER, SOURCE_CRYPTO_PAPER]


def test_an_unknown_source_label_is_refused(crypto_db: Path) -> None:
    with pytest.raises(ValueError, match="Unknown order source"):
        read_store_orders(crypto_db, source="SHADOW")


def test_the_reader_cannot_write_to_either_store(paper_db: Path) -> None:
    with (
        account_orders.read_only_connection(paper_db) as connection,
        pytest.raises(sqlite3.OperationalError),
    ):
        connection.execute("DELETE FROM order_intents")


# ==========================================================================
# The route
# ==========================================================================


@pytest.fixture
def client(crypto_db: Path, paper_db: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(equity_paper.PAPER_DATABASE_PATH_ENV, str(paper_db))
    monkeypatch.setenv(equity_paper.CRYPTO_DATABASE_PATH_ENV, str(crypto_db))
    with TestClient(equity_paper_api.create_app()) as test_client:
        yield test_client


def test_the_account_orders_route_is_a_get_that_serializes_both_stores(client: TestClient) -> None:
    response = client.get("/api/equity-paper/account-orders")

    assert response.status_code == 200
    payload = response.json()
    assert {row["source"] for row in payload["rows"]} == {SOURCE_CRYPTO_PAPER, SOURCE_EQUITY_PAPER}
    assert payload["includes_simulated"] is False
    assert payload["duplicates_dropped"] == 0
    # Exact quantities survive the wire as text.
    assert any(row["quantity"] == "0.174703844" for row in payload["rows"])


def test_the_account_orders_route_bounds_its_limit(client: TestClient) -> None:
    assert client.get("/api/equity-paper/account-orders?limit=0").status_code == 422
    assert client.get("/api/equity-paper/account-orders?limit=201").status_code == 422
    assert len(client.get("/api/equity-paper/account-orders?limit=1").json()["rows"]) == 1


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_the_account_orders_route_accepts_no_write(client: TestClient, method: str) -> None:
    assert getattr(client, method)("/api/equity-paper/account-orders").status_code == 405


def test_no_response_carries_a_credential(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "PKTESTKEYVALUE0000000"
    secret = "sEcReTvAlUe000000000000000000000000000000"
    monkeypatch.setenv("ALPACA_API_KEY", key)
    monkeypatch.setenv("ALPACA_SECRET_KEY", secret)

    body = client.get("/api/equity-paper/account-orders").text
    assert key not in body and secret not in body
    for forbidden in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "api_key", "secret"):
        assert forbidden not in body
