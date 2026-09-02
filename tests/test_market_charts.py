"""The chart layer: batched, cached, capped, read-only, and out of the trading path.

The provider is faked at the client boundary so every assertion is about what
this layer does with a request: how many provider calls it makes, when it
reuses an answer, what it says when it cannot answer, and that nothing here
can reach a store, an account, or an order.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autotrader.dashboard import charts, charts_api
from autotrader.dashboard.charts import ChartCache, ChartRequestError, normalize_symbols
from autotrader.data import chart_bars

NOW = datetime(2026, 9, 2, 17, 40, tzinfo=UTC)


@dataclass
class _Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class _BarSet:
    data: dict[str, list[_Bar]]


def _session_bars(day: datetime, *, count: int, start_price: float = 100.0) -> list[_Bar]:
    """Five-minute bars from 09:30 New York (13:30 UTC in September) for one day."""
    opening = day.replace(hour=13, minute=30, second=0, microsecond=0)
    bars = []
    for index in range(count):
        price = start_price + index
        bars.append(
            _Bar(opening + timedelta(minutes=5 * index), price, price + 1, price - 1, price, 10)
        )
    return bars


class FakeStockClient:
    def __init__(self, bars: dict[str, list[_Bar]] | None = None, *, fail: bool = False) -> None:
        self.requests: list[object] = []
        self.bars = bars or {}
        self.fail = fail

    def get_stock_bars(self, request: object) -> _BarSet:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("provider said no: key PKLEAKED0000")
        symbols = request.symbol_or_symbols  # type: ignore[attr-defined]
        return _BarSet({symbol: self.bars.get(symbol, []) for symbol in symbols})


class FakeCryptoClient:
    def __init__(self, bars: dict[str, list[_Bar]] | None = None) -> None:
        self.requests: list[object] = []
        self.bars = bars or {}

    def get_crypto_bars(self, request: object, feed: object = None) -> _BarSet:
        self.requests.append(request)
        symbols = request.symbol_or_symbols  # type: ignore[attr-defined]
        return _BarSet({symbol: self.bars.get(symbol, []) for symbol in symbols})


def _cache(stock: FakeStockClient, crypto: FakeCryptoClient, **kwargs: object) -> ChartCache:
    return ChartCache(
        stock_client_factory=lambda: stock,
        crypto_client_factory=lambda: crypto,
        credentials_check=lambda: True,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture
def stock() -> FakeStockClient:
    today = _session_bars(NOW, count=60)
    yesterday = _session_bars(NOW - timedelta(days=1), count=60, start_price=90.0)
    # Bars from a previous session and one after-hours bar that must be filtered.
    after_hours = _Bar(NOW.replace(hour=21, minute=0), 1.0, 1.0, 1.0, 1.0, 1)
    return FakeStockClient(
        {
            "SPY": yesterday + today + [after_hours],
            "NVDA": yesterday + today,
            "AAPL": [],
        }
    )


@pytest.fixture
def crypto() -> FakeCryptoClient:
    return FakeCryptoClient({"BTC/USD": _session_bars(NOW, count=30, start_price=70_000.0)})


# ==========================================================================
# Requests
# ==========================================================================


def test_symbols_are_normalized_deduplicated_and_bounded() -> None:
    assert normalize_symbols(["spy", " SPY", "nvda", "btc/usd"]) == ("SPY", "NVDA", "BTC/USD")
    with pytest.raises(ChartRequestError, match="At least one"):
        normalize_symbols(["", " "])
    with pytest.raises(ChartRequestError, match="At most"):
        normalize_symbols(
            [chr(65 + index) * 2 for index in range(charts.MAX_SYMBOLS_PER_REQUEST + 1)]
        )
    for bad in ("DROP TABLE", "../etc", "BTC/EUR", "toolongticker", "A B"):
        with pytest.raises(ChartRequestError):
            normalize_symbols([bad])


def test_every_range_is_defined_once_with_a_ttl_and_a_point_cap() -> None:
    assert set(chart_bars.RANGE_KEYS) == {"1D", "5D", "1M", "3M", "6M"}
    for spec in chart_bars.CHART_RANGES.values():
        assert spec.ttl_seconds > 0 and spec.max_points > 0
        assert spec.timeframe_unit in {"minute", "hour", "day"}
    assert chart_bars.CHART_RANGES["1D"].ttl_seconds < chart_bars.CHART_RANGES["1M"].ttl_seconds
    with pytest.raises(ChartRequestError, match="Unknown chart range"):
        _cache(FakeStockClient(), FakeCryptoClient()).read(["SPY"], "1Y", now=NOW)


# ==========================================================================
# Batching and caching
# ==========================================================================


def test_a_batch_of_symbols_is_one_provider_call_per_asset_class(
    stock: FakeStockClient, crypto: FakeCryptoClient
) -> None:
    batch = _cache(stock, crypto).read(["SPY", "NVDA", "AAPL", "BTC/USD"], "1D", now=NOW)

    assert batch.provider_calls_made == 2
    assert len(stock.requests) == 1 and len(crypto.requests) == 1
    assert sorted(stock.requests[0].symbol_or_symbols) == ["AAPL", "NVDA", "SPY"]  # type: ignore[attr-defined]
    assert [series.symbol for series in batch.series] == ["SPY", "NVDA", "AAPL", "BTC/USD"]


def test_a_repeat_read_inside_the_ttl_makes_no_provider_call(
    stock: FakeStockClient, crypto: FakeCryptoClient
) -> None:
    cache = _cache(stock, crypto)
    first = cache.read(["SPY", "NVDA"], "1D", now=NOW)
    second = cache.read(["NVDA", "SPY"], "1D", now=NOW + timedelta(seconds=30))

    assert first.provider_calls_made == 1 and first.cache_hits == 0
    assert second.provider_calls_made == 0 and second.cache_hits == 2
    assert all(series.from_cache for series in second.series)
    assert len(stock.requests) == 1
    # Deterministic: the cached series draws the same line.
    assert {s.symbol: s.points for s in first.series} == {s.symbol: s.points for s in second.series}


def test_a_read_after_the_ttl_refetches_only_what_expired(
    stock: FakeStockClient, crypto: FakeCryptoClient
) -> None:
    cache = _cache(stock, crypto)
    cache.read(["SPY"], "1D", now=NOW)
    ttl = chart_bars.CHART_RANGES["1D"].ttl_seconds
    later = cache.read(["SPY", "NVDA"], "1D", now=NOW + timedelta(seconds=ttl + 1))

    assert later.provider_calls_made == 1
    assert sorted(stock.requests[-1].symbol_or_symbols) == ["NVDA", "SPY"]  # type: ignore[attr-defined]


def test_ranges_are_cached_separately(stock: FakeStockClient, crypto: FakeCryptoClient) -> None:
    cache = _cache(stock, crypto)
    cache.read(["SPY"], "1D", now=NOW)
    other = cache.read(["SPY"], "5D", now=NOW)

    assert other.provider_calls_made == 1
    assert len(stock.requests) == 2


def test_the_provider_ceiling_turns_misses_into_unavailable_not_calls(
    stock: FakeStockClient, crypto: FakeCryptoClient
) -> None:
    cache = _cache(stock, crypto, max_calls_per_minute=1)
    cache.read(["SPY"], "1D", now=NOW)
    capped = cache.read(["NVDA"], "1D", now=NOW + timedelta(seconds=10))

    assert capped.provider_calls_made == 0
    assert capped.series[0].available is False
    assert capped.series[0].unavailable_reason == "PROVIDER_BUDGET_EXHAUSTED"
    assert len(stock.requests) == 1
    # The window slides: a minute later the ceiling has room again.
    freed = cache.read(["NVDA"], "1D", now=NOW + timedelta(seconds=61))
    assert freed.provider_calls_made == 1


def test_a_provider_failure_becomes_a_reason_code_and_forwards_no_text(
    crypto: FakeCryptoClient,
) -> None:
    broken = FakeStockClient(fail=True)
    batch = _cache(broken, crypto).read(["SPY", "BTC/USD"], "1D", now=NOW)

    spy = batch.series[0]
    assert spy.available is False
    assert spy.unavailable_reason == "PROVIDER_UNREADABLE"
    import dataclasses
    import json

    assert "PKLEAKED0000" not in json.dumps(dataclasses.asdict(batch))
    # The crypto leg is independent of the equity leg's failure.
    assert batch.series[1].available is True


def test_missing_credentials_leave_crypto_readable_and_say_why_for_equities(
    stock: FakeStockClient, crypto: FakeCryptoClient
) -> None:
    cache = ChartCache(
        stock_client_factory=lambda: stock,
        crypto_client_factory=lambda: crypto,
        credentials_check=lambda: False,
    )
    batch = cache.read(["SPY", "BTC/USD"], "1D", now=NOW)

    assert batch.series[0].unavailable_reason == "BROKER_NOT_CONFIGURED"
    assert batch.series[1].available is True
    assert len(stock.requests) == 0


# ==========================================================================
# Shaping
# ==========================================================================


def test_a_symbol_with_no_bars_is_unavailable_not_a_flat_line(
    stock: FakeStockClient, crypto: FakeCryptoClient
) -> None:
    batch = _cache(stock, crypto).read(["AAPL"], "1D", now=NOW)

    series = batch.series[0]
    assert series.available is False
    assert series.unavailable_reason == "NO_BARS"
    assert series.points == ()
    assert series.last_close is None


def test_the_one_day_range_keeps_only_the_latest_regular_session(
    stock: FakeStockClient, crypto: FakeCryptoClient
) -> None:
    batch = _cache(stock, crypto).read(["SPY"], "1D", now=NOW)

    series = batch.series[0]
    stamps = [point[0] for point in series.points]
    assert all(stamp.startswith("2026-09-02T") for stamp in stamps)
    assert not any(stamp.startswith("2026-09-02T21") for stamp in stamps), "after-hours bar kept"
    assert series.first_close == 100.0
    assert series.last_close == 159.0
    assert series.change_fraction == pytest.approx(0.59)


def test_a_long_series_is_downsampled_but_keeps_its_last_bar() -> None:
    spec = chart_bars.CHART_RANGES["6M"]
    bars = [
        chart_bars.ChartBar(NOW - timedelta(days=index), 1.0, 1.0, 1.0, float(index), 1.0)
        for index in range(400, 0, -1)
    ]
    shaped = charts.shape_series(bars, spec, equity=True)

    assert len(shaped) <= spec.max_points + 1
    assert shaped[-1] is bars[-1]
    assert shaped == charts.shape_series(bars, spec, equity=True)


def test_crypto_bars_are_not_session_filtered(
    stock: FakeStockClient, crypto: FakeCryptoClient
) -> None:
    batch = _cache(stock, crypto).read(["BTC/USD"], "1D", now=NOW)

    assert batch.series[0].asset_class == "CRYPTO"
    assert len(batch.series[0].points) == 30


# ==========================================================================
# The process boundary
# ==========================================================================


def test_the_chart_modules_import_no_provider_sdk_store_or_execution_code() -> None:
    """Inside the dashboard package the provider is reached only through data/."""
    for module in (charts, charts_api):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported = {
            (node.module or "") for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        for banned in (
            "alpaca",
            "sqlite3",
            "autotrader.state",
            "autotrader.execution.paper",
            "autotrader.execution.equity",
        ):
            assert not any(name.startswith(banned) for name in imported), (
                f"{module.__name__} imports {banned}"
            )


def test_no_trading_module_imports_the_chart_bar_boundary() -> None:
    """The chart layer is not the trading data path, and nothing trading-side reaches it."""
    root = Path(chart_bars.__file__).resolve().parents[1]
    importers = []
    for path in sorted(root.rglob("*.py")):
        if path.parent.name == "dashboard":
            continue
        if "chart_bars" in path.read_text(encoding="utf-8") and path.name != "chart_bars.py":
            importers.append(str(path.relative_to(root)))
    assert importers == [], importers


def test_the_chart_bar_boundary_builds_market_data_requests_only() -> None:
    source = Path(chart_bars.__file__).read_text(encoding="utf-8")
    for forbidden in ("TradingClient", "submit_order", "OrderRequest", "get_account", "positions"):
        assert forbidden not in source, forbidden
    assert "StockBarsRequest" in source and "CryptoBarsRequest" in source


def test_the_stock_request_is_batched_and_split_adjusted() -> None:
    request = chart_bars.build_stock_request(["SPY", "NVDA"], chart_bars.CHART_RANGES["3M"], NOW)

    assert request.symbol_or_symbols == ["SPY", "NVDA"]
    assert str(request.adjustment).lower().endswith("split")
    assert request.timeframe.amount_value == 1


# ==========================================================================
# The routes
# ==========================================================================


@pytest.fixture
def client(stock: FakeStockClient, crypto: FakeCryptoClient) -> TestClient:
    with TestClient(charts_api.create_app(_cache(stock, crypto))) as test_client:
        yield test_client


def test_the_chart_api_has_no_write_surface() -> None:
    application = charts_api.create_app(ChartCache())
    for route in application.routes:
        methods = set(getattr(route, "methods", set()) or set())
        assert not methods & {"POST", "PUT", "PATCH", "DELETE"}, getattr(route, "path", route)
        assert methods <= charts_api.ALLOWED_METHODS | {"OPTIONS"}
        segments = {segment.lower() for segment in str(getattr(route, "path", "")).split("/")}
        assert not segments & {"submit", "cancel", "start", "stop", "execute", "run", "set"}


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_no_chart_route_accepts_a_write_method(client: TestClient, method: str) -> None:
    assert getattr(client, method)("/api/market-charts/bars?symbols=SPY").status_code == 405


def test_the_bars_route_answers_a_batch(client: TestClient) -> None:
    response = client.get("/api/market-charts/bars?symbols=SPY,NVDA,BTC/USD&range=1D")

    assert response.status_code == 200
    payload = response.json()
    assert [series["symbol"] for series in payload["series"]] == ["SPY", "NVDA", "BTC/USD"]
    assert payload["range"] == "1D"
    assert payload["provider_calls_made"] == 2
    assert payload["series"][0]["available"] is True


def test_the_bars_route_rejects_a_malformed_request(client: TestClient) -> None:
    assert client.get("/api/market-charts/bars?symbols=SPY&range=1Y").status_code == 422
    assert client.get("/api/market-charts/bars?symbols=DROP%20TABLE").status_code == 422
    assert client.get("/api/market-charts/bars").status_code == 422
    too_many = ",".join(chr(65 + index) * 2 for index in range(charts.MAX_SYMBOLS_PER_REQUEST + 1))
    assert client.get(f"/api/market-charts/bars?symbols={too_many}").status_code == 422


def test_the_liveness_and_ranges_routes_contact_nothing(
    client: TestClient, stock: FakeStockClient
) -> None:
    assert client.get("/api/market-charts/health").json()["trading_path"] is False
    ranges = client.get("/api/market-charts/ranges").json()
    assert [entry["key"] for entry in ranges["ranges"]] == list(chart_bars.RANGE_KEYS)
    assert stock.requests == []


def test_no_response_carries_a_credential(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "PKTESTKEYVALUE0000000"
    secret = "sEcReTvAlUe000000000000000000000000000000"
    monkeypatch.setenv("ALPACA_API_KEY", key)
    monkeypatch.setenv("ALPACA_SECRET_KEY", secret)
    for path in ("bars?symbols=SPY,BTC/USD", "ranges", "health"):
        body = client.get(f"/api/market-charts/{path}").text
        assert key not in body and secret not in body, path
        for forbidden in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "api_key", "secret"):
            assert forbidden not in body, f"{forbidden} in {path}"


def test_the_default_binding_is_loopback() -> None:
    assert charts_api.DEFAULT_HOST == "127.0.0.1"
    assert charts_api.DEFAULT_PORT == 8004
