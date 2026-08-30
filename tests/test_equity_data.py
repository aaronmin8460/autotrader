"""Equity V0.2: the stock market-data boundary and the bounded runtime window.

Offline. The Alpaca *models* are real - a fake `Bar` would prove nothing about
the conversion - and only the transport is faked.
"""

from __future__ import annotations

import ast
import json
import socket
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.models.bars import Bar
from alpaca.data.requests import StockBarsRequest

from autotrader.backtest import run_backtest
from autotrader.data.historical import CANONICAL_COLUMNS
from autotrader.data.validation import (
    EQUITY_UNIVERSE_LABEL,
    INVALID_SYMBOL,
    validate_frame,
)
from autotrader.equity import EQUITY_SYMBOLS, EquityError
from autotrader.equity import data as equity_data
from autotrader.equity.data import (
    FEED,
    EquityDataError,
    build_bars_request,
    build_metadata,
    create_client,
    download_bars,
    fetch_bars,
    fetch_bars_for_symbols,
    output_paths,
    output_stem,
    resolve_date_range,
    to_request_window,
)
from autotrader.equity.market_data import AlpacaEquityBars, filter_to_sessions
from autotrader.equity.session import session_from_local
from test_equity_session import EARLY_CLOSE, ORDINARY, FakeCalendar, consecutive_sessions

T0 = datetime(2026, 8, 26, 13, 30, tzinfo=UTC)


def make_bar(symbol: str, timestamp: datetime, close: float = 100.0) -> Bar:
    return Bar(
        symbol,
        {
            "t": timestamp,
            "o": close,
            "h": close + 1,
            "l": close - 1,
            "c": close,
            "v": 1000,
            "n": 10,
            "vw": close,
        },
    )


class FakeBarSet:
    def __init__(self, data: dict[str, list[Bar]]) -> None:
        self.data = data


class FakeStockClient:
    """Records requests and answers with whatever it was constructed with."""

    def __init__(
        self,
        data: dict[str, list[Bar]] | None = None,
        error: APIError | None = None,
    ) -> None:
        self._data = data if data is not None else {}
        self._error = error
        self.requests: list[StockBarsRequest] = []

    def get_stock_bars(self, request: StockBarsRequest) -> FakeBarSet:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return FakeBarSet(self._data)


def session_bars(symbol: str, count: int = 30) -> list[Bar]:
    """Regular-session bars for the ordinary session, plus extended-hours ones."""
    bars = [make_bar(symbol, ORDINARY.open_utc - timedelta(hours=1))]
    bars += [
        make_bar(symbol, ORDINARY.open_utc + index * timedelta(minutes=15), 100.0 + index)
        for index in range(count)
    ]
    return bars


# ==========================================================================
# The universe and the feed
# ==========================================================================


def test_only_the_iex_feed_is_used() -> None:
    """Alpaca Basic serves IEX; SIP needs a paid subscription."""
    assert FEED is DataFeed.IEX
    request = build_bars_request("SPY", T0, T0 + timedelta(hours=1))
    assert request.feed is DataFeed.IEX


def test_the_request_asks_for_fifteen_minute_bars() -> None:
    request = build_bars_request("SPY", T0, T0 + timedelta(hours=1))

    assert request.timeframe.amount == 15
    assert request.timeframe.unit.value == "Min"


def test_a_batch_request_carries_every_symbol_in_one_call() -> None:
    """CRITICAL: ten symbols must cost one provider call, not ten."""
    client = FakeStockClient({symbol: session_bars(symbol) for symbol in EQUITY_SYMBOLS})

    frames = fetch_bars_for_symbols(client, EQUITY_SYMBOLS, T0, T0 + timedelta(hours=6))

    assert len(client.requests) == 1
    assert client.requests[0].symbol_or_symbols == list(EQUITY_SYMBOLS)
    assert set(frames) == set(EQUITY_SYMBOLS)


def test_a_symbol_the_provider_returned_nothing_for_still_appears() -> None:
    """ "No prints on this feed" is a different answer from "no such symbol"."""
    client = FakeStockClient({"SPY": session_bars("SPY")})

    frames = fetch_bars_for_symbols(client, ["SPY", "IWM"], T0, T0 + timedelta(hours=6))

    assert set(frames) == {"SPY", "IWM"}
    assert frames["IWM"].empty
    assert list(frames["IWM"].columns) == list(CANONICAL_COLUMNS)


def test_a_symbol_outside_the_universe_is_refused_before_a_request_is_made() -> None:
    client = FakeStockClient()

    with pytest.raises(EquityError):
        fetch_bars_for_symbols(client, ["SPY", "GOOG"], T0, T0 + timedelta(hours=1))
    assert client.requests == []


def test_a_provider_refusal_becomes_a_controlled_error() -> None:
    client = FakeStockClient(error=APIError(json.dumps({"message": "no"})))

    with pytest.raises(EquityDataError):
        fetch_bars(client, "SPY", T0, T0 + timedelta(hours=1))


# ==========================================================================
# Canonical conversion
# ==========================================================================


def test_bars_convert_to_the_canonical_schema() -> None:
    client = FakeStockClient({"SPY": session_bars("SPY", count=4)})

    frame = fetch_bars(client, "SPY", T0, T0 + timedelta(hours=6))

    assert list(frame.columns) == list(CANONICAL_COLUMNS)
    dtype = frame["timestamp"].dtype
    assert isinstance(dtype, pd.DatetimeTZDtype)
    assert str(dtype.tz) == "UTC"
    assert set(frame["symbol"]) == {"SPY"}
    assert frame["timestamp"].is_monotonic_increasing


def test_a_downloaded_equity_dataset_validates_against_the_equity_universe() -> None:
    client = FakeStockClient({"SPY": session_bars("SPY", count=10)})
    frame = fetch_bars(client, "SPY", T0, T0 + timedelta(hours=6))

    result = validate_frame(
        frame, supported_symbols=EQUITY_SYMBOLS, universe_label=EQUITY_UNIVERSE_LABEL
    )

    assert result.valid, result.errors


def test_an_equity_dataset_is_rejected_by_the_crypto_universe_and_vice_versa() -> None:
    """The universes stay separate; neither validator silently accepts the other."""
    client = FakeStockClient({"SPY": session_bars("SPY", count=5)})
    equity_frame = fetch_bars(client, "SPY", T0, T0 + timedelta(hours=6))

    crypto_check = validate_frame(equity_frame)

    assert not crypto_check.valid
    assert INVALID_SYMBOL in crypto_check.codes()
    assert any("pair universe" in issue.message for issue in crypto_check.errors)

    crypto_frame = equity_frame.assign(symbol="BTC/USD").astype({"symbol": "string"})
    equity_check = validate_frame(
        crypto_frame, supported_symbols=EQUITY_SYMBOLS, universe_label=EQUITY_UNIVERSE_LABEL
    )

    assert not equity_check.valid
    assert any("equity universe" in issue.message for issue in equity_check.errors)


# ==========================================================================
# Dates and the request window
# ==========================================================================


def test_dates_are_market_dates_and_the_window_is_utc() -> None:
    """A summer exchange day starts at 04:00 UTC, not 00:00 UTC."""
    start, end = to_request_window(date(2026, 8, 26), date(2026, 8, 26))

    assert start == datetime(2026, 8, 26, 4, 0, tzinfo=UTC)
    assert end == datetime(2026, 8, 27, 4, 0, tzinfo=UTC) - timedelta(microseconds=1)


def test_a_winter_market_date_shifts_with_the_offset() -> None:
    start, _ = to_request_window(date(2026, 1, 5), date(2026, 1, 5))

    assert start == datetime(2026, 1, 5, 5, 0, tzinfo=UTC)


def test_an_inverted_date_range_is_refused() -> None:
    with pytest.raises(EquityDataError):
        resolve_date_range("2026-08-26", "2026-08-25")


@pytest.mark.parametrize("value", ["26-08-2026", "2026/08/26", "today", ""])
def test_a_malformed_date_is_refused(value: str) -> None:
    with pytest.raises(EquityDataError):
        resolve_date_range(value, "2026-08-26")


def test_the_output_stem_needs_no_slug() -> None:
    assert output_stem("SPY", "15m", date(2026, 1, 2), date(2026, 8, 27)) == (
        "SPY_15m_2026-01-02_2026-08-27"
    )


def test_the_metadata_records_both_timezone_meanings() -> None:
    metadata = build_metadata(
        symbol="SPY",
        timeframe="15m",
        start=date(2026, 8, 26),
        end=date(2026, 8, 26),
        row_count=26,
        parquet_filename="SPY_15m_2026-08-26_2026-08-26.parquet",
        retrieved_at=T0,
    )

    assert metadata["date_timezone"] == "America/New_York"
    assert metadata["timestamp_timezone"] == "UTC"
    assert metadata["feed"] == "iex"
    assert metadata["asset_class"] == "us_equity"
    for forbidden in ("api_key", "secret", "account"):
        assert not any(forbidden in str(key).lower() for key in metadata)


# ==========================================================================
# Download
# ==========================================================================


def test_a_download_writes_parquet_and_its_sidecar(tmp_path: Path) -> None:
    client = FakeStockClient({"SPY": session_bars("SPY", count=26)})

    result = download_bars("SPY", "15m", "2026-08-26", "2026-08-26", tmp_path, client=client)

    assert result.parquet_path.exists()
    assert result.metadata_path.exists()
    assert result.row_count == 27
    assert result.feed == "iex"
    stored = pd.read_parquet(result.parquet_path, engine="pyarrow")
    assert list(stored.columns) == list(CANONICAL_COLUMNS)


def test_an_empty_download_writes_nothing(tmp_path: Path) -> None:
    client = FakeStockClient({})

    with pytest.raises(EquityDataError):
        download_bars("SPY", "15m", "2026-08-26", "2026-08-26", tmp_path, client=client)
    assert list(tmp_path.iterdir()) == []


def test_the_expected_output_paths_are_deterministic(tmp_path: Path) -> None:
    parquet, metadata = output_paths(tmp_path, "SPY", "15m", date(2026, 8, 26), date(2026, 8, 26))

    assert parquet.name.endswith(".parquet")
    assert metadata.name.endswith(".metadata.json")


def test_stock_market_data_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlike crypto, Alpaca will not serve stock bars unauthenticated."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    with pytest.raises(EquityDataError):
        create_client()


# ==========================================================================
# The bounded runtime window
# ==========================================================================


def test_extended_hours_bars_are_filtered_out_of_the_runtime_window() -> None:
    """CRITICAL: IEX serves pre- and post-market candles in the same response."""
    bars = [
        make_bar("SPY", ORDINARY.open_utc - timedelta(hours=1)),  # 08:30 ET
        make_bar("SPY", ORDINARY.open_utc),  # 09:30 ET
        make_bar("SPY", ORDINARY.close_utc - timedelta(minutes=15)),  # 15:45 ET
        make_bar("SPY", ORDINARY.close_utc),  # 16:00 ET
        make_bar("SPY", ORDINARY.close_utc + timedelta(minutes=30)),  # 16:30 ET
    ]
    client = FakeStockClient({"SPY": bars})
    frame = fetch_bars(client, "SPY", T0, T0 + timedelta(hours=12))

    filtered = filter_to_sessions(frame, (ORDINARY,), lookback_bars=200)

    assert list(filtered["timestamp"]) == [
        pd.Timestamp(ORDINARY.open_utc),
        pd.Timestamp(ORDINARY.close_utc - timedelta(minutes=15)),
    ]


def test_the_window_is_trimmed_to_the_lookback_after_filtering() -> None:
    """Order matters: a pre-market bar must not consume an EMA slot."""
    bars = [make_bar("SPY", ORDINARY.open_utc - timedelta(hours=1))]
    bars += [
        make_bar("SPY", ORDINARY.open_utc + index * timedelta(minutes=15)) for index in range(26)
    ]
    client = FakeStockClient({"SPY": bars})
    frame = fetch_bars(client, "SPY", T0, T0 + timedelta(hours=12))

    filtered = filter_to_sessions(frame, (ORDINARY,), lookback_bars=100)

    assert len(filtered) == 26
    assert filtered["timestamp"].iloc[0] == pd.Timestamp(ORDINARY.open_utc)


def test_an_early_close_day_filters_against_its_own_close() -> None:
    bars = [
        make_bar("SPY", EARLY_CLOSE.close_utc - timedelta(minutes=15)),  # 12:45 ET, in
        make_bar("SPY", EARLY_CLOSE.close_utc + timedelta(minutes=30)),  # 13:30 ET, out
    ]
    client = FakeStockClient({"SPY": bars})
    frame = fetch_bars(client, "SPY", T0, T0 + timedelta(hours=12))

    filtered = filter_to_sessions(frame, (EARLY_CLOSE,), lookback_bars=100)

    assert len(filtered) == 1


def test_the_runtime_source_makes_exactly_one_batched_request() -> None:
    sessions = consecutive_sessions(date(2026, 8, 3), 20)
    calendar = FakeCalendar(sessions)
    latest = sessions[-1].open_utc + timedelta(minutes=15)
    client = FakeStockClient({symbol: session_bars(symbol) for symbol in EQUITY_SYMBOLS})
    source = AlpacaEquityBars(calendar, client=client)

    frames = source.recent_bars(
        EQUITY_SYMBOLS,
        now=latest + timedelta(minutes=20),
        latest_bar_start=latest,
        lookback_bars=200,
    )

    assert len(client.requests) == 1
    assert source.api_calls == 1
    assert set(frames) == set(EQUITY_SYMBOLS)


def test_the_request_window_never_reaches_the_in_progress_candle() -> None:
    sessions = consecutive_sessions(date(2026, 8, 3), 20)
    calendar = FakeCalendar(sessions)
    latest = sessions[-1].open_utc + timedelta(minutes=15)
    client = FakeStockClient({})
    source = AlpacaEquityBars(calendar, client=client)

    source.recent_bars(
        EQUITY_SYMBOLS,
        now=latest + timedelta(minutes=20),
        latest_bar_start=latest,
        lookback_bars=200,
    )

    request = client.requests[0]
    assert request.end < (latest + timedelta(minutes=15)).replace(tzinfo=None)


def test_the_window_is_anchored_on_sessions_not_calendar_days() -> None:
    """Fifteen sessions of history reach back over weekends, not eight days."""
    sessions = consecutive_sessions(date(2026, 8, 3), 20)
    calendar = FakeCalendar(sessions)
    latest = sessions[-1].open_utc + timedelta(minutes=15)
    source = AlpacaEquityBars(calendar, client=FakeStockClient({}))

    spanned = source.sessions_for(latest_bar_start=latest, lookback_bars=200)

    assert len(spanned) == 15
    assert spanned[-1].session_date == sessions[-1].session_date


def test_the_window_does_not_redownload_a_year() -> None:
    sessions = consecutive_sessions(date(2026, 1, 2), 200)
    calendar = FakeCalendar(sessions)
    latest = sessions[-1].open_utc + timedelta(minutes=15)
    client = FakeStockClient({})
    source = AlpacaEquityBars(calendar, client=client)

    source.recent_bars(
        EQUITY_SYMBOLS,
        now=latest + timedelta(minutes=20),
        latest_bar_start=latest,
        lookback_bars=200,
    )

    request = client.requests[0]
    span = request.end - request.start
    assert span < timedelta(days=40), span


# ==========================================================================
# Backtest compatibility
# ==========================================================================


def test_the_same_backtester_runs_an_equity_dataset() -> None:
    """The EMA strategy and the simulation are reused, not reimplemented."""
    closes = [100.0 + index for index in range(60)] + [160.0 - index * 2 for index in range(60)]
    bars = [
        make_bar("SPY", ORDINARY.open_utc + index * timedelta(minutes=15), close)
        for index, close in enumerate(closes)
    ]
    client = FakeStockClient({"SPY": bars})
    frame = fetch_bars(client, "SPY", T0, T0 + timedelta(days=3))

    result = run_backtest(
        frame, supported_symbols=EQUITY_SYMBOLS, universe_label=EQUITY_UNIVERSE_LABEL
    )

    assert result.symbol == "SPY"
    assert result.signal_count > 0
    assert result.bar_count == len(closes)


def test_a_crypto_backtest_still_defaults_to_the_pair_universe() -> None:
    """CRITICAL: no crypto backtest changed behaviour."""
    closes = [100.0 + index for index in range(60)] + [160.0 - index * 2 for index in range(60)]
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [T0 + index * timedelta(minutes=15) for index in range(len(closes))], utc=True
            ),
            "symbol": pd.Series(["BTC/USD"] * len(closes), dtype="string"),
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
            "trade_count": [10.0] * len(closes),
            "vwap": closes,
        }
    )

    assert run_backtest(frame).symbol == "BTC/USD"


# ==========================================================================
# Offline and scope guarantees
# ==========================================================================


def test_the_equity_data_boundary_opens_no_socket_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("the data boundary must not open a socket")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    client = FakeStockClient({"SPY": session_bars("SPY", count=4)})

    assert not fetch_bars(client, "SPY", T0, T0 + timedelta(hours=6)).empty


def test_the_equity_data_boundary_constructs_no_trading_client() -> None:
    """It reads market data. It cannot trade, and the source shows it."""
    source = Path(equity_data.__file__).read_text()
    for forbidden in ("TradingClient", "submit_order", "MarketOrderRequest", "paper=False"):
        assert forbidden not in source, forbidden


def test_the_equity_universe_is_written_down_once_per_layer() -> None:
    """The duplication in the stdlib-only domain layer cannot silently drift."""
    from autotrader.execution import models as execution_models

    assert execution_models.EQUITY_SYMBOLS == EQUITY_SYMBOLS
    assert execution_models.TRADABLE_SYMBOLS == (
        execution_models.SUPPORTED_SYMBOLS + EQUITY_SYMBOLS
    )


def test_no_extra_equity_symbol_is_named_anywhere_in_the_package() -> None:
    """CRITICAL: symbol creep is caught by scanning, not by good intentions."""
    package_root = Path(equity_data.__file__).resolve().parents[1]
    allowed = set(EQUITY_SYMBOLS)
    creeping = {"GOOG", "BRK.B", "VOO", "VTI", "AMD", "NFLX", "INTC", "DIA", "ARKK"}
    for path in sorted(package_root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in creeping, f"{node.value} named in {path}"
    assert allowed.isdisjoint(creeping)


def test_the_equity_download_writes_only_under_the_requested_directory(
    tmp_path: Path,
) -> None:
    client = FakeStockClient({"SPY": session_bars("SPY", count=4)})

    result = download_bars("SPY", "15m", "2026-08-26", "2026-08-26", tmp_path, client=client)

    assert result.parquet_path.parent == tmp_path
    assert result.metadata_path.parent == tmp_path


def test_a_session_built_from_broker_shaped_values_round_trips() -> None:
    session = session_from_local(
        date(2026, 8, 26), datetime(2026, 8, 26, 9, 30), datetime(2026, 8, 26, 16, 0)
    )

    assert session == ORDINARY


# --------------------------------------------------------------------------
# Corporate-action adjustment
# --------------------------------------------------------------------------


def test_default_request_sends_no_adjustment_field():
    """The default is exactly what this module did before the parameter existed.

    `None` means the request carries no `adjustment`, which the provider reads
    as `raw`. Pinned because raw prices are the ones an order fills at and the
    reconciliation path compares against them; changing this default silently
    would change what the live runtime trades on.
    """
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    end = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
    request = equity_data.build_bars_request("SPY", start, end)
    assert equity_data.DEFAULT_ADJUSTMENT is None
    assert request.adjustment is None


def test_a_caller_may_ask_for_split_adjusted_bars():
    """What a historical study must pass.

    A raw series steps by about ninety percent across a ten-for-one split, and
    every trailing indicator reads that step as a return. A multi-year study
    therefore asks for split-adjusted prices explicitly.
    """
    start = datetime(2024, 6, 6, 13, 30, tzinfo=UTC)
    end = datetime(2024, 6, 12, 20, 0, tzinfo=UTC)
    request = equity_data.build_bars_request("NVDA", start, end, Adjustment.SPLIT)
    assert request.adjustment is Adjustment.SPLIT


def test_metadata_records_the_adjustment_that_produced_the_file():
    """Two files built under different adjustments must not look identical."""
    raw = equity_data.build_metadata(
        symbol="SPY",
        timeframe="15m",
        start=date(2026, 1, 2),
        end=date(2026, 1, 30),
        row_count=1,
        parquet_filename="SPY_15m_2026-01-02_2026-01-30.parquet",
        retrieved_at=datetime(2026, 8, 29, tzinfo=UTC),
    )
    split = equity_data.build_metadata(
        symbol="SPY",
        timeframe="15m",
        start=date(2026, 1, 2),
        end=date(2026, 1, 30),
        row_count=1,
        parquet_filename="SPY_15m_2026-01-02_2026-01-30.parquet",
        retrieved_at=datetime(2026, 8, 29, tzinfo=UTC),
        adjustment=Adjustment.SPLIT,
    )
    assert raw["adjustment"] == "raw"
    assert split["adjustment"] == "split"


def test_fetch_passes_the_adjustment_through_to_the_request():
    """The parameter must reach the provider, not stop at the boundary."""
    seen: list[object] = []

    class RecordingClient:
        def get_stock_bars(self, request):
            seen.append(request.adjustment)
            return SimpleNamespace(data={"SPY": []})

    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    end = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
    equity_data.fetch_bars_for_symbols(RecordingClient(), ["SPY"], start, end)
    equity_data.fetch_bars_for_symbols(RecordingClient(), ["SPY"], start, end, Adjustment.SPLIT)
    assert seen == [None, Adjustment.SPLIT]
