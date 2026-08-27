"""Phase 1 tests: canonical historical bars, Parquet output, and the CLI.

These tests never touch the network. The only thing faked is the Alpaca
client boundary; bars are built with the real alpaca-py models so the
conversion is exercised against the real response shape.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.models.bars import BarSet
from alpaca.data.timeframe import TimeFrameUnit
from typer.testing import CliRunner

from autotrader.cli import app
from autotrader.data import historical
from autotrader.data.historical import (
    CANONICAL_COLUMNS,
    SUPPORTED_SYMBOLS,
    HistoricalDataError,
    bars_to_dataframe,
    build_bars_request,
    build_metadata,
    download_bars,
    normalize_symbol,
    normalize_timeframe,
    output_paths,
    output_stem,
    parse_market_date,
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


class FakeStockClient:
    """Stands in for StockHistoricalDataClient. Records requests, returns canned bars."""

    def __init__(self, response: BarSet | None = None, error: APIError | None = None) -> None:
        self._response = response if response is not None else BarSet({})
        self._error = error
        self.requests: list[object] = []

    def get_stock_bars(self, request_params: object) -> BarSet:
        self.requests.append(request_params)
        if self._error is not None:
            raise self._error
        return self._response


# --------------------------------------------------------------------------
# Symbols
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [("spy", "SPY"), ("SPY", "SPY"), (" nvda ", "NVDA"), ("Msft", "MSFT")],
)
def test_symbol_is_normalized_to_uppercase(supplied: str, expected: str) -> None:
    assert normalize_symbol(supplied) == expected


def test_every_v01_symbol_is_supported() -> None:
    assert SUPPORTED_SYMBOLS == ("SPY", "QQQ", "AAPL", "MSFT", "NVDA")
    for symbol in SUPPORTED_SYMBOLS:
        assert normalize_symbol(symbol.lower()) == symbol


@pytest.mark.parametrize("symbol", ["TSLA", "AMZN", "", "SP Y", "BTCUSD"])
def test_unsupported_symbol_is_rejected(symbol: str) -> None:
    with pytest.raises(HistoricalDataError) as excinfo:
        normalize_symbol(symbol)
    message = str(excinfo.value)
    assert "Unsupported symbol" in message
    assert "SPY" in message


# --------------------------------------------------------------------------
# Timeframe
# --------------------------------------------------------------------------


@pytest.mark.parametrize("supplied", ["15m", "15M", " 15m "])
def test_supported_timeframe_is_normalized(supplied: str) -> None:
    assert normalize_timeframe(supplied) == "15m"


@pytest.mark.parametrize("supplied", ["1m", "5m", "1d", "15min", "1h", ""])
def test_other_timeframes_are_rejected(supplied: str) -> None:
    with pytest.raises(HistoricalDataError) as excinfo:
        normalize_timeframe(supplied)
    assert "Unsupported timeframe" in str(excinfo.value)


def test_request_uses_a_fifteen_minute_iex_bar_request() -> None:
    start, end = to_request_window(date(2025, 1, 1), date(2025, 1, 31))
    request = build_bars_request("SPY", start, end)

    assert request.symbol_or_symbols == "SPY"
    assert request.timeframe.amount_value == 15
    assert request.timeframe.unit_value == TimeFrameUnit.Minute
    assert request.timeframe.value == "15Min"
    assert request.feed == DataFeed.IEX
    # alpaca-py normalizes timezone-aware inputs to naive UTC on the request object.
    assert request.start == start.replace(tzinfo=None)
    assert request.end == end.replace(tzinfo=None)


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------


def test_valid_dates_parse() -> None:
    assert parse_market_date("2025-01-01", "start") == date(2025, 1, 1)
    assert parse_market_date("2025-12-31", "end") == date(2025, 12, 31)
    assert parse_market_date(" 2024-02-29 ", "start") == date(2024, 2, 29)


@pytest.mark.parametrize("supplied", ["2025-1-1", "20250101", "01/01/2025", "not-a-date", ""])
def test_malformed_dates_are_rejected(supplied: str) -> None:
    with pytest.raises(HistoricalDataError) as excinfo:
        parse_market_date(supplied, "start")
    assert "YYYY-MM-DD" in str(excinfo.value)


@pytest.mark.parametrize("supplied", ["2025-13-01", "2025-02-30", "2025-00-10"])
def test_impossible_calendar_dates_are_rejected(supplied: str) -> None:
    with pytest.raises(HistoricalDataError):
        parse_market_date(supplied, "end")


def test_end_before_start_is_rejected() -> None:
    with pytest.raises(HistoricalDataError) as excinfo:
        resolve_date_range("2025-06-01", "2025-05-31")
    assert "before" in str(excinfo.value)


def test_single_day_range_is_allowed() -> None:
    assert resolve_date_range("2025-06-02", "2025-06-02") == (
        date(2025, 6, 2),
        date(2025, 6, 2),
    )


def test_request_window_is_new_york_midnight_to_next_day_in_utc() -> None:
    start, end = to_request_window(date(2025, 1, 2), date(2025, 1, 3))

    # January: New York is UTC-5, so local midnight is 05:00 UTC.
    assert start == datetime(2025, 1, 2, 5, 0, tzinfo=UTC)
    # The user's end date is inclusive, so the boundary is the next day's midnight.
    assert end == datetime(2025, 1, 4, 5, 0, tzinfo=UTC)
    assert end - start == timedelta(days=2)


def test_request_window_respects_daylight_saving_time() -> None:
    start, _ = to_request_window(date(2025, 7, 1), date(2025, 7, 1))
    # July: New York is UTC-4.
    assert start == datetime(2025, 7, 1, 4, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Canonical conversion
# --------------------------------------------------------------------------


def test_bars_convert_to_the_canonical_schema() -> None:
    bars = barset("SPY", [FIRST_BAR]).data["SPY"]
    frame = bars_to_dataframe(bars, "spy")

    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["symbol"] == "SPY"
    assert row["open"] == 100.0
    assert row["high"] == 101.0
    assert row["low"] == 99.5
    assert row["close"] == 100.5
    assert row["volume"] == 12345.0
    assert row["trade_count"] == 210.0
    assert row["vwap"] == 100.25
    assert row["timestamp"] == pd.Timestamp(FIRST_BAR)


def test_canonical_columns_are_exactly_present_and_ordered() -> None:
    frame = bars_to_dataframe(barset("QQQ", [FIRST_BAR]).data["QQQ"], "QQQ")
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
    eastern_stamp = datetime.fromisoformat("2025-01-02T09:30:00-05:00")
    frame = bars_to_dataframe(barset("AAPL", [eastern_stamp]).data["AAPL"], "AAPL")

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
    frame = bars_to_dataframe(barset("MSFT", unordered).data["MSFT"], "MSFT")

    assert frame["timestamp"].is_monotonic_increasing
    assert list(frame["timestamp"]) == [
        pd.Timestamp(FIRST_BAR),
        pd.Timestamp(FIRST_BAR + timedelta(minutes=15)),
        pd.Timestamp(FIRST_BAR + timedelta(minutes=30)),
    ]
    assert list(frame.index) == [0, 1, 2]


def test_missing_trade_count_and_vwap_stay_nullable() -> None:
    bars = barset("NVDA", [FIRST_BAR], n=None, vw=None).data["NVDA"]
    frame = bars_to_dataframe(bars, "NVDA")

    assert frame["trade_count"].isna().all()
    assert frame["vwap"].isna().all()
    assert frame["close"].iloc[0] == 100.5


def test_empty_bars_produce_an_empty_canonical_frame() -> None:
    frame = bars_to_dataframe([], "SPY")
    assert frame.empty
    assert tuple(frame.columns) == CANONICAL_COLUMNS


# --------------------------------------------------------------------------
# Output naming and files
# --------------------------------------------------------------------------


def test_output_filename_is_deterministic_and_date_ranged(tmp_path) -> None:
    stem = output_stem("SPY", "15m", date(2025, 1, 1), date(2025, 12, 31))
    assert stem == "SPY_15m_2025-01-01_2025-12-31"

    parquet_path, metadata_path = output_paths(
        tmp_path, "SPY", "15m", date(2025, 1, 1), date(2025, 12, 31)
    )
    assert parquet_path == tmp_path / "SPY_15m_2025-01-01_2025-12-31.parquet"
    assert metadata_path == tmp_path / "SPY_15m_2025-01-01_2025-12-31.metadata.json"

    # Same inputs always give the same paths; different ranges never collide.
    assert output_paths(tmp_path, "SPY", "15m", date(2025, 1, 1), date(2025, 12, 31)) == (
        parquet_path,
        metadata_path,
    )
    other, _ = output_paths(tmp_path, "SPY", "15m", date(2025, 1, 1), date(2025, 6, 30))
    assert other != parquet_path


def test_download_round_trips_through_parquet(tmp_path) -> None:
    timestamps = [FIRST_BAR + timedelta(minutes=15 * offset) for offset in range(4)]
    client = FakeStockClient(barset("SPY", timestamps))

    result = download_bars(
        symbol="spy",
        timeframe="15m",
        start="2025-01-02",
        end="2025-01-02",
        output_dir=tmp_path,
        client=client,
    )

    assert result.row_count == 4
    assert result.parquet_path.exists()
    assert result.parquet_path == tmp_path / "SPY_15m_2025-01-02_2025-01-02.parquet"

    stored = pd.read_parquet(result.parquet_path)
    assert tuple(stored.columns) == CANONICAL_COLUMNS
    assert len(stored) == 4
    assert isinstance(stored["timestamp"].dtype, pd.DatetimeTZDtype)
    assert str(stored["timestamp"].dtype.tz) == "UTC"
    assert stored["timestamp"].is_monotonic_increasing
    assert set(stored["symbol"]) == {"SPY"}


def test_download_writes_metadata_without_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "test-key-must-not-be-written")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret-must-not-be-written")
    client = FakeStockClient(barset("AAPL", [FIRST_BAR]))

    result = download_bars(
        symbol="AAPL",
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
        "feed": "iex",
        "parquet_filename": "AAPL_15m_2025-01-02_2025-01-02.parquet",
        "provider": "alpaca",
        "requested_end": "2025-01-02",
        "requested_start": "2025-01-02",
        "retrieved_at_utc": metadata["retrieved_at_utc"],
        "row_count": 1,
        "symbol": "AAPL",
        "timeframe": "15m",
        "timestamp_timezone": "UTC",
    }
    assert datetime.fromisoformat(metadata["retrieved_at_utc"]).tzinfo is not None


def test_metadata_contains_no_credential_keys() -> None:
    metadata = build_metadata(
        symbol="SPY",
        timeframe="15m",
        start=date(2025, 1, 1),
        end=date(2025, 12, 31),
        row_count=10,
        parquet_filename="SPY_15m_2025-01-01_2025-12-31.parquet",
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    serialized = json.dumps(metadata).lower()
    for forbidden in ("api_key", "secret", "token", "account", "password"):
        assert forbidden not in serialized


# --------------------------------------------------------------------------
# Failure paths
# --------------------------------------------------------------------------


def test_empty_alpaca_response_fails_clearly_and_writes_nothing(tmp_path) -> None:
    client = FakeStockClient(BarSet({}))

    with pytest.raises(HistoricalDataError) as excinfo:
        download_bars(
            symbol="SPY",
            timeframe="15m",
            start="2025-01-01",
            end="2025-01-02",
            output_dir=tmp_path,
            client=client,
        )

    assert "no 15m bars" in str(excinfo.value)
    assert list(tmp_path.iterdir()) == []


def test_alpaca_api_error_becomes_a_controlled_error(tmp_path) -> None:
    client = FakeStockClient(error=APIError('{"code": 40110000, "message": "forbidden"}'))

    with pytest.raises(HistoricalDataError) as excinfo:
        download_bars(
            symbol="SPY",
            timeframe="15m",
            start="2025-01-01",
            end="2025-01-02",
            output_dir=tmp_path,
            client=client,
        )

    assert "Alpaca rejected" in str(excinfo.value)
    assert list(tmp_path.iterdir()) == []


def test_missing_credentials_fail_clearly(monkeypatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    assert historical.credentials_configured() is False
    with pytest.raises(HistoricalDataError) as excinfo:
        historical.create_client()

    message = str(excinfo.value)
    assert "Alpaca credentials are not configured." in message
    assert "ALPACA_API_KEY" in message
    assert "ALPACA_SECRET_KEY" in message


def test_blank_credentials_count_as_missing(monkeypatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "   ")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "")
    assert historical.credentials_configured() is False


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
    client = FakeStockClient(barset("SPY", timestamps))
    monkeypatch.setattr(historical, "create_client", lambda: client)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "download",
            "--symbol",
            "spy",
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
    assert "Symbol:    SPY" in result.output
    assert "Timeframe: 15m" in result.output
    assert "Rows:      3" in result.output
    assert "Feed:      IEX" in result.output

    expected = tmp_path / "data" / "raw" / "SPY_15m_2025-01-02_2025-01-02.parquet"
    assert expected.exists()
    assert expected.with_suffix("").with_suffix(".metadata.json").exists()
    assert len(client.requests) == 1


def test_cli_download_defaults_to_fifteen_minute_bars(tmp_path, monkeypatch) -> None:
    client = FakeStockClient(barset("QQQ", [FIRST_BAR]))
    monkeypatch.setattr(historical, "create_client", lambda: client)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        app,
        ["download", "--symbol", "QQQ", "--start", "2025-01-02", "--end", "2025-01-02"],
    )

    assert result.exit_code == 0, result.output
    assert "Timeframe: 15m" in result.output


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            ["--symbol", "TSLA", "--start", "2025-01-01", "--end", "2025-01-02"],
            "Unsupported symbol",
        ),
        (["--symbol", "SPY", "--start", "2025-01-01", "--end", "2024-12-31"], "before"),
        (["--symbol", "SPY", "--start", "01-01-2025", "--end", "2025-01-02"], "YYYY-MM-DD"),
        (
            [
                "--symbol",
                "SPY",
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


def test_cli_download_reports_missing_credentials(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        app,
        ["download", "--symbol", "SPY", "--start", "2025-01-02", "--end", "2025-01-02"],
    )

    assert result.exit_code == 1
    assert "Alpaca credentials are not configured." in result.output
