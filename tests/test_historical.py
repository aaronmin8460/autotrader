"""C1 tests: canonical historical crypto bars, Parquet output, and the CLI.

These tests never touch the network. The only thing faked is the Alpaca
client boundary; bars are built with the real alpaca-py models so the
conversion is exercised against the real response shape.

The crypto pivot's contract lives here: the supported pairs are exactly
BTC/USD and ETH/USD, the canonical symbol keeps its slash everywhere except a
filename, the client is `CryptoHistoricalDataClient`, dates are UTC calendar
dates rather than exchange-session dates, and no IEX equity feed is reachable.
"""

from __future__ import annotations

import ast
import inspect
import json
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.enums import CryptoFeed
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.models.bars import BarSet
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrameUnit
from typer.testing import CliRunner

from autotrader.cli import app
from autotrader.data import historical
from autotrader.data.historical import (
    CANONICAL_COLUMNS,
    SUPPORTED_SYMBOLS,
    SUPPORTED_TIMEFRAME,
    HistoricalDataError,
    bars_to_dataframe,
    build_bars_request,
    build_metadata,
    download_bars,
    filesystem_slug,
    normalize_symbol,
    normalize_timeframe,
    output_paths,
    output_stem,
    parse_utc_date,
    resolve_date_range,
    to_request_window,
)

FIRST_BAR = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)


def raw_bar(timestamp: datetime, **overrides: object) -> dict[str, object]:
    """One bar in Alpaca's wire shape (see alpaca.data.mappings.BAR_MAPPING)."""
    payload: dict[str, object] = {
        "t": timestamp,
        "o": 100.0,
        "h": 101.0,
        "l": 99.5,
        "c": 100.5,
        "v": 12345.0,
        "n": 210.0,
        "vw": 100.25,
    }
    payload.update(overrides)
    return payload


def barset(symbol: str, timestamps: list[datetime], **overrides: object) -> BarSet:
    return BarSet({symbol: [raw_bar(stamp, **overrides) for stamp in timestamps]})


class FakeCryptoClient:
    """Stands in for CryptoHistoricalDataClient. Records calls, returns canned bars."""

    def __init__(self, response: BarSet | None = None, error: APIError | None = None) -> None:
        self._response = response if response is not None else BarSet({})
        self._error = error
        self.requests: list[object] = []
        self.feeds: list[object] = []

    def get_crypto_bars(self, request_params: object, feed: object = CryptoFeed.US) -> BarSet:
        self.requests.append(request_params)
        self.feeds.append(feed)
        if self._error is not None:
            raise self._error
        return self._response


# --------------------------------------------------------------------------
# Symbols
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("btc/usd", "BTC/USD"),
        ("BTC/USD", "BTC/USD"),
        (" eth/usd ", "ETH/USD"),
        ("Eth/Usd", "ETH/USD"),
    ],
)
def test_symbol_is_normalized_to_uppercase(supplied: str, expected: str) -> None:
    assert normalize_symbol(supplied) == expected


def test_the_supported_universe_is_exactly_the_two_crypto_pairs() -> None:
    assert SUPPORTED_SYMBOLS == ("BTC/USD", "ETH/USD")
    for symbol in SUPPORTED_SYMBOLS:
        assert normalize_symbol(symbol.lower()) == symbol


@pytest.mark.parametrize("symbol", ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA"])
def test_stock_symbols_are_rejected(symbol: str) -> None:
    """The archived equity universe is not tradable or downloadable any more."""
    with pytest.raises(HistoricalDataError) as excinfo:
        normalize_symbol(symbol)
    message = str(excinfo.value)
    assert "Unsupported symbol" in message
    assert "BTC/USD" in message


@pytest.mark.parametrize("symbol", ["BTCUSD", "ETHUSD", "BTC-USD", "BTC/EUR", "SOL/USD", ""])
def test_a_non_canonical_or_out_of_scope_pair_is_rejected(symbol: str) -> None:
    """`BTCUSD` is refused rather than silently reinterpreted as `BTC/USD`.

    Quietly rewriting it would let two spellings of one market end up in two
    stored datasets that never reconcile.
    """
    with pytest.raises(HistoricalDataError):
        normalize_symbol(symbol)


def test_only_usd_quoted_pairs_are_supported() -> None:
    assert historical.QUOTE_CURRENCY == "USD"
    for symbol in SUPPORTED_SYMBOLS:
        assert symbol.endswith("/USD")


# --------------------------------------------------------------------------
# Filesystem slug
# --------------------------------------------------------------------------


def test_the_filesystem_slug_replaces_the_slash_with_an_underscore() -> None:
    assert filesystem_slug("BTC/USD") == "BTC_USD"
    assert filesystem_slug("eth/usd") == "ETH_USD"


def test_the_slug_is_never_the_domain_symbol() -> None:
    """A slug is for filenames only; `BTCUSD` is not a symbol this system uses."""
    for symbol in SUPPORTED_SYMBOLS:
        slug = filesystem_slug(symbol)
        assert "/" not in slug
        assert slug != symbol.replace("/", "")
        with pytest.raises(HistoricalDataError):
            normalize_symbol(slug)


# --------------------------------------------------------------------------
# Timeframe
# --------------------------------------------------------------------------


@pytest.mark.parametrize("supplied", ["15m", "15M", " 15m "])
def test_supported_timeframe_is_normalized(supplied: str) -> None:
    assert normalize_timeframe(supplied) == "15m"
    assert SUPPORTED_TIMEFRAME == "15m"


@pytest.mark.parametrize("supplied", ["1m", "5m", "1d", "15min", "1h", ""])
def test_other_timeframes_are_rejected(supplied: str) -> None:
    with pytest.raises(HistoricalDataError) as excinfo:
        normalize_timeframe(supplied)
    assert "Unsupported timeframe" in str(excinfo.value)


def test_request_is_a_fifteen_minute_crypto_bar_request() -> None:
    start, end = to_request_window(date(2025, 1, 1), date(2025, 1, 31))
    request = build_bars_request("BTC/USD", start, end)

    assert isinstance(request, CryptoBarsRequest)
    assert request.symbol_or_symbols == "BTC/USD"
    assert request.timeframe.amount_value == 15
    assert request.timeframe.unit_value == TimeFrameUnit.Minute
    assert request.timeframe.value == "15Min"
    # alpaca-py normalizes timezone-aware inputs to naive UTC on the request object.
    assert request.start == start.replace(tzinfo=None)
    assert request.end == end.replace(tzinfo=None)
    # Crypto carries no per-request feed field; the feed is a client argument.
    assert not hasattr(request, "feed")


def test_the_crypto_us_feed_is_passed_to_the_client() -> None:
    client = FakeCryptoClient(barset("BTC/USD", [FIRST_BAR]))
    historical.fetch_bars(client, "BTC/USD", *to_request_window(date(2025, 1, 2), date(2025, 1, 2)))

    assert client.feeds == [CryptoFeed.US]
    assert historical.FEED is CryptoFeed.US
    assert historical.FEED.value == "us"


# --------------------------------------------------------------------------
# No equity data path survives
# --------------------------------------------------------------------------


def code_without_prose(source: str) -> str:
    """`source` with every docstring and comment removed.

    The source-level guarantees below are about *executable code*, not about
    prose. The module's own documentation names the things it forbids - "no
    IEX feed", "there is no StockHistoricalDataClient" - so a naive substring
    scan would trip over the very sentences that explain the rule.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    # `ast.unparse` drops comments as a side effect of round-tripping the tree.
    return ast.unparse(tree)


def module_source() -> str:
    return code_without_prose(inspect.getsource(historical))


def test_the_data_module_uses_the_crypto_client_only() -> None:
    tree = ast.parse(module_source())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)

    assert "alpaca.data.historical.crypto.CryptoHistoricalDataClient" in imported
    assert "alpaca.data.requests.CryptoBarsRequest" in imported
    for forbidden in (
        "alpaca.data.historical.stock.StockHistoricalDataClient",
        "alpaca.data.requests.StockBarsRequest",
        "alpaca.data.requests.StockLatestTradeRequest",
    ):
        assert forbidden not in imported, forbidden


def test_no_iex_dependency_remains_in_the_data_path() -> None:
    """The equity feed is gone, not merely unused."""
    source = module_source()
    for forbidden in ("IEX", "DataFeed", "StockHistoricalDataClient", "StockBarsRequest"):
        assert forbidden not in source, forbidden
    assert not hasattr(historical, "MARKET_TIMEZONE")


def test_no_equity_session_calendar_is_imported() -> None:
    source = module_source()
    for forbidden in ("America/New_York", "ZoneInfo", "NYSE", "Nasdaq", "market_calendar"):
        assert forbidden not in source, forbidden


# --------------------------------------------------------------------------
# Credentials: optional for crypto data
# --------------------------------------------------------------------------


def test_the_crypto_client_is_constructible_without_credentials(monkeypatch) -> None:
    """Alpaca serves crypto bars unauthenticated, so a download must not need a key."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    assert historical.credentials_configured() is False
    client = historical.create_client()
    assert isinstance(client, CryptoHistoricalDataClient)


def test_configured_credentials_are_passed_through(monkeypatch) -> None:
    """Credentials are optional, but they are used when present: better rate limits."""
    monkeypatch.setenv("ALPACA_API_KEY", "key-value")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret-value")

    assert historical.credentials_configured() is True
    client = historical.create_client()
    assert isinstance(client, CryptoHistoricalDataClient)
    assert client._api_key == "key-value"


def test_a_half_configured_environment_counts_as_unconfigured(monkeypatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "   ")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "")
    assert historical.credentials_configured() is False
    assert isinstance(historical.create_client(), CryptoHistoricalDataClient)


def test_downloading_never_raises_a_missing_credentials_error(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    client = FakeCryptoClient(barset("BTC/USD", [FIRST_BAR]))

    result = download_bars(
        symbol="BTC/USD",
        timeframe="15m",
        start="2025-01-02",
        end="2025-01-02",
        output_dir=tmp_path,
        client=client,
    )
    assert result.row_count == 1


# --------------------------------------------------------------------------
# Dates: UTC, not an exchange session
# --------------------------------------------------------------------------


def test_valid_dates_parse() -> None:
    assert parse_utc_date("2025-01-01", "start") == date(2025, 1, 1)
    assert parse_utc_date("2025-12-31", "end") == date(2025, 12, 31)
    assert parse_utc_date(" 2024-02-29 ", "start") == date(2024, 2, 29)


@pytest.mark.parametrize("supplied", ["2025-1-1", "20250101", "01/01/2025", "not-a-date", ""])
def test_malformed_dates_are_rejected(supplied: str) -> None:
    with pytest.raises(HistoricalDataError) as excinfo:
        parse_utc_date(supplied, "start")
    assert "YYYY-MM-DD" in str(excinfo.value)


@pytest.mark.parametrize("supplied", ["2025-13-01", "2025-02-30", "2025-00-10"])
def test_impossible_calendar_dates_are_rejected(supplied: str) -> None:
    with pytest.raises(HistoricalDataError):
        parse_utc_date(supplied, "end")


def test_end_before_start_is_rejected() -> None:
    with pytest.raises(HistoricalDataError) as excinfo:
        resolve_date_range("2025-06-01", "2025-05-31")
    assert "before" in str(excinfo.value)


def test_single_day_range_is_allowed() -> None:
    assert resolve_date_range("2025-06-02", "2025-06-02") == (
        date(2025, 6, 2),
        date(2025, 6, 2),
    )


def test_request_window_starts_at_utc_midnight() -> None:
    """Crypto has no exchange session, so a day boundary is 00:00 UTC."""
    start, _ = to_request_window(date(2025, 1, 2), date(2025, 1, 3))

    assert start == datetime(2025, 1, 2, 0, 0, tzinfo=UTC)


def test_the_request_window_ends_on_the_last_instant_of_the_end_date() -> None:
    """Alpaca's crypto `end` is inclusive, and 00:00 is a real bar.

    Asking for the next day's midnight would pull one bar belonging to the day
    *after* the requested range - which is how a file named `..._2025-12-31`
    ends up holding a 2026-01-01 bar.
    """
    start, end = to_request_window(date(2025, 1, 2), date(2025, 1, 3))

    assert end == datetime(2025, 1, 3, 23, 59, 59, 999999, tzinfo=UTC)
    assert end < datetime(2025, 1, 4, 0, 0, tzinfo=UTC)
    assert end - start == timedelta(days=2) - timedelta(microseconds=1)


def test_a_single_day_window_covers_exactly_that_day() -> None:
    start, end = to_request_window(date(2025, 6, 1), date(2025, 6, 1))

    assert start == datetime(2025, 6, 1, 0, 0, tzinfo=UTC)
    assert end.date() == date(2025, 6, 1)
    assert end.hour == 23 and end.minute == 59


def test_the_request_window_does_not_shift_with_daylight_saving() -> None:
    """A New-York-anchored window would move by an hour in July. A UTC one does not."""
    winter, _ = to_request_window(date(2025, 1, 1), date(2025, 1, 1))
    summer, _ = to_request_window(date(2025, 7, 1), date(2025, 7, 1))

    assert winter.hour == 0
    assert summer.hour == 0
    assert winter.utcoffset() == timedelta(0)
    assert summer.utcoffset() == timedelta(0)


def test_a_weekend_range_is_an_ordinary_request() -> None:
    """2025-01-04 and 05 are a Saturday and a Sunday. Crypto trades through them."""
    start, end = to_request_window(date(2025, 1, 4), date(2025, 1, 5))
    assert end - start == timedelta(days=2) - timedelta(microseconds=1)


# --------------------------------------------------------------------------
# Canonical conversion
# --------------------------------------------------------------------------


def test_bars_convert_to_the_canonical_schema() -> None:
    bars = barset("BTC/USD", [FIRST_BAR]).data["BTC/USD"]
    frame = bars_to_dataframe(bars, "btc/usd")

    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["symbol"] == "BTC/USD"
    assert row["open"] == 100.0
    assert row["high"] == 101.0
    assert row["low"] == 99.5
    assert row["close"] == 100.5
    assert row["volume"] == 12345.0
    assert row["trade_count"] == 210.0
    assert row["vwap"] == 100.25
    assert row["timestamp"] == pd.Timestamp(FIRST_BAR)


def test_the_stored_symbol_keeps_its_slash() -> None:
    """The DataFrame carries `BTC/USD`, never the `BTC_USD` filename slug."""
    frame = bars_to_dataframe(barset("BTC/USD", [FIRST_BAR]).data["BTC/USD"], "BTC/USD")
    assert set(frame["symbol"]) == {"BTC/USD"}
    assert "BTC_USD" not in set(frame["symbol"])


def test_canonical_columns_are_exactly_present_and_ordered() -> None:
    frame = bars_to_dataframe(barset("ETH/USD", [FIRST_BAR]).data["ETH/USD"], "ETH/USD")
    assert tuple(frame.columns) == CANONICAL_COLUMNS
    assert CANONICAL_COLUMNS == (
        "timestamp",
        "symbol",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "trade_count",
        "vwap",
    )


def test_timestamps_are_timezone_aware_utc() -> None:
    # Alpaca can hand back a non-UTC offset; the canonical frame normalizes it.
    offset_stamp = datetime.fromisoformat("2025-01-02T09:30:00-05:00")
    frame = bars_to_dataframe(barset("BTC/USD", [offset_stamp]).data["BTC/USD"], "BTC/USD")

    dtype = frame["timestamp"].dtype
    assert isinstance(dtype, pd.DatetimeTZDtype)
    assert str(dtype.tz) == "UTC"
    assert frame["timestamp"].iloc[0] == pd.Timestamp("2025-01-02T14:30:00Z")


def test_bars_are_sorted_ascending_by_timestamp() -> None:
    unordered = [
        FIRST_BAR + timedelta(minutes=30),
        FIRST_BAR,
        FIRST_BAR + timedelta(minutes=15),
    ]
    frame = bars_to_dataframe(barset("ETH/USD", unordered).data["ETH/USD"], "ETH/USD")

    assert frame["timestamp"].is_monotonic_increasing
    assert list(frame["timestamp"]) == [
        pd.Timestamp(FIRST_BAR),
        pd.Timestamp(FIRST_BAR + timedelta(minutes=15)),
        pd.Timestamp(FIRST_BAR + timedelta(minutes=30)),
    ]
    assert list(frame.index) == [0, 1, 2]


def test_missing_trade_count_and_vwap_stay_nullable() -> None:
    bars = barset("ETH/USD", [FIRST_BAR], n=None, vw=None).data["ETH/USD"]
    frame = bars_to_dataframe(bars, "ETH/USD")

    assert frame["trade_count"].isna().all()
    assert frame["vwap"].isna().all()
    assert frame["close"].iloc[0] == 100.5


def test_empty_bars_produce_an_empty_canonical_frame() -> None:
    frame = bars_to_dataframe([], "BTC/USD")
    assert frame.empty
    assert tuple(frame.columns) == CANONICAL_COLUMNS


def test_overnight_and_weekend_bars_are_ordinary_rows() -> None:
    """A 24/7 dataset is continuous; nothing here treats a 03:00 UTC bar as odd."""
    saturday_night = datetime(2025, 1, 4, 3, 0, tzinfo=UTC)
    stamps = [saturday_night + timedelta(minutes=15 * offset) for offset in range(4)]
    frame = bars_to_dataframe(barset("BTC/USD", stamps).data["BTC/USD"], "BTC/USD")
    assert len(frame) == 4


# --------------------------------------------------------------------------
# Output naming and files
# --------------------------------------------------------------------------


def test_output_filename_uses_the_slug_and_is_date_ranged(tmp_path) -> None:
    stem = output_stem("BTC/USD", "15m", date(2025, 1, 1), date(2025, 12, 31))
    assert stem == "BTC_USD_15m_2025-01-01_2025-12-31"

    parquet_path, metadata_path = output_paths(
        tmp_path, "BTC/USD", "15m", date(2025, 1, 1), date(2025, 12, 31)
    )
    assert parquet_path == tmp_path / "BTC_USD_15m_2025-01-01_2025-12-31.parquet"
    assert metadata_path == tmp_path / "BTC_USD_15m_2025-01-01_2025-12-31.metadata.json"
    assert "/" not in parquet_path.name

    # Same inputs always give the same paths; different ranges never collide.
    assert output_paths(tmp_path, "BTC/USD", "15m", date(2025, 1, 1), date(2025, 12, 31)) == (
        parquet_path,
        metadata_path,
    )
    other, _ = output_paths(tmp_path, "BTC/USD", "15m", date(2025, 1, 1), date(2025, 6, 30))
    assert other != parquet_path


def test_the_two_pairs_never_share_a_filename(tmp_path) -> None:
    btc, _ = output_paths(tmp_path, "BTC/USD", "15m", date(2025, 1, 1), date(2025, 12, 31))
    eth, _ = output_paths(tmp_path, "ETH/USD", "15m", date(2025, 1, 1), date(2025, 12, 31))
    assert btc != eth
    assert btc.name == "BTC_USD_15m_2025-01-01_2025-12-31.parquet"
    assert eth.name == "ETH_USD_15m_2025-01-01_2025-12-31.parquet"


def test_download_round_trips_through_parquet(tmp_path) -> None:
    timestamps = [FIRST_BAR + timedelta(minutes=15 * offset) for offset in range(4)]
    client = FakeCryptoClient(barset("BTC/USD", timestamps))

    result = download_bars(
        symbol="btc/usd",
        timeframe="15m",
        start="2025-01-02",
        end="2025-01-02",
        output_dir=tmp_path,
        client=client,
    )

    assert result.row_count == 4
    assert result.symbol == "BTC/USD"
    assert result.parquet_path.exists()
    assert result.parquet_path == tmp_path / "BTC_USD_15m_2025-01-02_2025-01-02.parquet"

    stored = pd.read_parquet(result.parquet_path)
    assert tuple(stored.columns) == CANONICAL_COLUMNS
    assert len(stored) == 4
    assert isinstance(stored["timestamp"].dtype, pd.DatetimeTZDtype)
    assert str(stored["timestamp"].dtype.tz) == "UTC"
    assert stored["timestamp"].is_monotonic_increasing
    # The filename is slugged; the data is not.
    assert set(stored["symbol"]) == {"BTC/USD"}


def test_download_writes_metadata_without_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "test-key-must-not-be-written")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret-must-not-be-written")
    client = FakeCryptoClient(barset("ETH/USD", [FIRST_BAR]))

    result = download_bars(
        symbol="ETH/USD",
        timeframe="15m",
        start="2025-01-02",
        end="2025-01-02",
        output_dir=tmp_path,
        client=client,
    )

    text = result.metadata_path.read_text(encoding="utf-8")
    assert "test-key-must-not-be-written" not in text
    assert "test-secret-must-not-be-written" not in text

    metadata = json.loads(text)
    assert metadata == {
        "feed": "us",
        "parquet_filename": "ETH_USD_15m_2025-01-02_2025-01-02.parquet",
        "provider": "alpaca",
        "requested_end": "2025-01-02",
        "requested_start": "2025-01-02",
        "retrieved_at_utc": metadata["retrieved_at_utc"],
        "row_count": 1,
        "symbol": "ETH/USD",
        "symbol_slug": "ETH_USD",
        "timeframe": "15m",
        "timestamp_timezone": "UTC",
    }
    assert datetime.fromisoformat(metadata["retrieved_at_utc"]).tzinfo is not None


def test_metadata_records_the_canonical_symbol_and_the_slug_separately() -> None:
    metadata = build_metadata(
        symbol="BTC/USD",
        timeframe="15m",
        start=date(2025, 1, 1),
        end=date(2025, 12, 31),
        row_count=10,
        parquet_filename="BTC_USD_15m_2025-01-01_2025-12-31.parquet",
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert metadata["symbol"] == "BTC/USD"
    assert metadata["symbol_slug"] == "BTC_USD"
    assert metadata["feed"] == "us"


def test_metadata_contains_no_credential_keys() -> None:
    metadata = build_metadata(
        symbol="BTC/USD",
        timeframe="15m",
        start=date(2025, 1, 1),
        end=date(2025, 12, 31),
        row_count=10,
        parquet_filename="BTC_USD_15m_2025-01-01_2025-12-31.parquet",
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    serialized = json.dumps(metadata).lower()
    for forbidden in ("api_key", "secret", "token", "account", "password"):
        assert forbidden not in serialized


# --------------------------------------------------------------------------
# Failure paths
# --------------------------------------------------------------------------


def test_empty_alpaca_response_fails_clearly_and_writes_nothing(tmp_path) -> None:
    client = FakeCryptoClient(BarSet({}))

    with pytest.raises(HistoricalDataError) as excinfo:
        download_bars(
            symbol="BTC/USD",
            timeframe="15m",
            start="2025-01-01",
            end="2025-01-02",
            output_dir=tmp_path,
            client=client,
        )

    assert "no 15m bars" in str(excinfo.value)
    assert list(tmp_path.iterdir()) == []


def test_alpaca_api_error_becomes_a_controlled_error(tmp_path) -> None:
    client = FakeCryptoClient(error=APIError('{"code": 40110000, "message": "forbidden"}'))

    with pytest.raises(HistoricalDataError) as excinfo:
        download_bars(
            symbol="BTC/USD",
            timeframe="15m",
            start="2025-01-01",
            end="2025-01-02",
            output_dir=tmp_path,
            client=client,
        )

    assert "Alpaca rejected" in str(excinfo.value)
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_download_help_lists_the_options() -> None:
    result = CliRunner().invoke(app, ["download", "--help"])
    assert result.exit_code == 0
    for option in ("--symbol", "--timeframe", "--start", "--end"):
        assert option in result.output


def test_cli_download_succeeds_without_network(tmp_path, monkeypatch) -> None:
    timestamps = [FIRST_BAR + timedelta(minutes=15 * offset) for offset in range(3)]
    client = FakeCryptoClient(barset("BTC/USD", timestamps))
    monkeypatch.setattr(historical, "create_client", lambda: client)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "download",
            "--symbol",
            "btc/usd",
            "--timeframe",
            "15m",
            "--start",
            "2025-01-02",
            "--end",
            "2025-01-02",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Downloaded historical bars" in result.output
    assert "Symbol:    BTC/USD" in result.output
    assert "Timeframe: 15m" in result.output
    assert "Rows:      3" in result.output
    assert "alpaca crypto (us)" in result.output
    assert "IEX" not in result.output

    expected = tmp_path / "data" / "raw" / "BTC_USD_15m_2025-01-02_2025-01-02.parquet"
    assert expected.exists()
    assert (tmp_path / "data" / "raw" / "BTC_USD_15m_2025-01-02_2025-01-02.metadata.json").exists()
    assert len(client.requests) == 1


def test_cli_download_defaults_to_fifteen_minute_bars(tmp_path, monkeypatch) -> None:
    client = FakeCryptoClient(barset("ETH/USD", [FIRST_BAR]))
    monkeypatch.setattr(historical, "create_client", lambda: client)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        app,
        ["download", "--symbol", "ETH/USD", "--start", "2025-01-02", "--end", "2025-01-02"],
    )

    assert result.exit_code == 0, result.output
    assert "Timeframe: 15m" in result.output


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            ["--symbol", "SPY", "--start", "2025-01-01", "--end", "2025-01-02"],
            "Unsupported symbol",
        ),
        (
            ["--symbol", "BTCUSD", "--start", "2025-01-01", "--end", "2025-01-02"],
            "Unsupported symbol",
        ),
        (["--symbol", "BTC/USD", "--start", "2025-01-01", "--end", "2024-12-31"], "before"),
        (["--symbol", "BTC/USD", "--start", "01-01-2025", "--end", "2025-01-02"], "YYYY-MM-DD"),
        (
            [
                "--symbol",
                "BTC/USD",
                "--start",
                "2025-01-01",
                "--end",
                "2025-01-02",
                "--timeframe",
                "1d",
            ],
            "Unsupported timeframe",
        ),
    ],
)
def test_cli_download_rejects_bad_input_without_a_traceback(
    tmp_path, monkeypatch, args: list[str], expected: str
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["download", *args])

    assert result.exit_code == 1
    assert expected in result.output
    assert not isinstance(result.exception, HistoricalDataError)
    assert not (tmp_path / "data").exists()


def test_cli_help_offers_only_the_two_crypto_pairs() -> None:
    result = CliRunner().invoke(app, ["download", "--help"])
    assert result.exit_code == 0
    output = result.output.replace("\n", " ")
    assert "BTC/USD" in output
    for forbidden in ("SPY", "QQQ", "AAPL", "MSFT", "NVDA"):
        assert forbidden not in output, forbidden
