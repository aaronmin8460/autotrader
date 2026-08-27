"""Phase 2 tests: validation of stored canonical bar datasets and the CLI.

Every test is offline and builds small synthetic frames. Nothing here
downloads, and no test needs Alpaca credentials.
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from autotrader.cli import app
from autotrader.data.historical import CANONICAL_COLUMNS
from autotrader.data.validation import (
    DUPLICATE_TIMESTAMP,
    EMPTY_DATASET,
    INVALID_OHLC,
    INVALID_SYMBOL,
    INVALID_TIMESTAMP,
    INVALID_TRADE_COUNT,
    INVALID_VOLUME,
    INVALID_VWAP,
    MISSING_COLUMN,
    NULL_OHLC,
    UNEXPECTED_COLUMN,
    UNSORTED_TIMESTAMP,
    ValidationInputError,
    read_bars,
    validate_frame,
    validate_parquet_file,
)

FIRST_BAR = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)


def canonical_frame(row_count: int = 3, symbol: str = "SPY") -> pd.DataFrame:
    """A minimal dataset that satisfies the Phase 1 contract in full."""
    offsets = list(range(row_count))
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                pd.Series([FIRST_BAR + timedelta(minutes=15 * n) for n in offsets]), utc=True
            ),
            "symbol": pd.Series([symbol] * row_count, dtype="string"),
            "open": [100.0 + n for n in offsets],
            "high": [101.0 + n for n in offsets],
            "low": [99.0 + n for n in offsets],
            "close": [100.5 + n for n in offsets],
            "volume": [12345.0 + n for n in offsets],
            "trade_count": [210.0 + n for n in offsets],
            "vwap": [100.25 + n for n in offsets],
        }
    )


def altered(frame: pd.DataFrame, column: str, index: int, value: object) -> pd.DataFrame:
    """A copy of `frame` with one cell replaced."""
    changed = frame.copy()
    changed.loc[index, column] = value
    return changed


def write_parquet(frame: pd.DataFrame, path: Path) -> Path:
    frame.to_parquet(path, engine="pyarrow", index=False)
    return path


def codes(frame: pd.DataFrame) -> tuple[str, ...]:
    return validate_frame(frame).codes()


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_canonical_dataset_is_valid() -> None:
    result = validate_frame(canonical_frame())

    assert result.valid is True
    assert result.errors == ()
    assert result.error_count == 0
    assert result.row_count == 3
    assert result.symbol == "SPY"


@pytest.mark.parametrize("symbol", ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"])
def test_every_supported_symbol_is_accepted(symbol: str) -> None:
    result = validate_frame(canonical_frame(symbol=symbol))
    assert result.valid, result.errors
    assert result.symbol == symbol


def test_a_single_bar_is_a_valid_dataset() -> None:
    assert validate_frame(canonical_frame(row_count=1)).valid


def test_large_timestamp_gaps_are_allowed() -> None:
    # Weekends, holidays, and overnight closures are normal; Phase 2 does not
    # require continuous 15-minute spacing.
    frame = canonical_frame(row_count=2)
    frame.loc[1, "timestamp"] = pd.Timestamp(FIRST_BAR + timedelta(days=4))

    assert validate_frame(frame).valid


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


def test_empty_dataset_is_rejected() -> None:
    result = validate_frame(canonical_frame(row_count=0))

    assert result.valid is False
    assert result.codes() == (EMPTY_DATASET,)
    assert result.row_count == 0
    assert result.symbol is None


@pytest.mark.parametrize("column", CANONICAL_COLUMNS)
def test_missing_column_is_reported(column: str) -> None:
    result = validate_frame(canonical_frame().drop(columns=[column]))

    assert not result.valid
    assert MISSING_COLUMN in result.codes()
    assert any(column in issue.message for issue in result.errors)


def test_unexpected_column_is_reported() -> None:
    frame = canonical_frame()
    frame["adjusted_close"] = frame["close"]

    result = validate_frame(frame)

    assert not result.valid
    assert UNEXPECTED_COLUMN in result.codes()
    assert any("adjusted_close" in issue.message for issue in result.errors)


def test_missing_column_does_not_block_other_checks() -> None:
    frame = altered(canonical_frame(), "volume", 0, -1.0).drop(columns=["vwap"])
    result = validate_frame(frame)

    assert MISSING_COLUMN in result.codes()
    assert INVALID_VOLUME in result.codes()


# --------------------------------------------------------------------------
# Timestamp
# --------------------------------------------------------------------------


def test_null_timestamp_is_rejected() -> None:
    result = validate_frame(altered(canonical_frame(), "timestamp", 1, pd.NaT))

    assert not result.valid
    assert INVALID_TIMESTAMP in result.codes()
    assert any("null timestamp" in issue.message for issue in result.errors)


def test_naive_timestamp_is_rejected() -> None:
    frame = canonical_frame()
    frame["timestamp"] = frame["timestamp"].dt.tz_localize(None)

    result = validate_frame(frame)

    assert not result.valid
    assert INVALID_TIMESTAMP in result.codes()
    assert any("timezone-naive" in issue.message for issue in result.errors)


def test_non_utc_timezone_is_rejected() -> None:
    frame = canonical_frame()
    frame["timestamp"] = frame["timestamp"].dt.tz_convert("America/New_York")

    result = validate_frame(frame)

    assert not result.valid
    assert INVALID_TIMESTAMP in result.codes()
    assert any("America/New_York" in issue.message for issue in result.errors)


def test_non_datetime_timestamp_is_rejected() -> None:
    frame = canonical_frame()
    frame["timestamp"] = frame["timestamp"].astype("string")

    result = validate_frame(frame)

    assert INVALID_TIMESTAMP in result.codes()


def test_duplicate_timestamps_are_rejected() -> None:
    frame = canonical_frame()
    frame.loc[2, "timestamp"] = frame.loc[1, "timestamp"]

    result = validate_frame(frame)

    assert not result.valid
    assert DUPLICATE_TIMESTAMP in result.codes()
    # Repeated values are still ascending, so this is not an ordering problem.
    assert UNSORTED_TIMESTAMP not in result.codes()


def test_unsorted_timestamps_are_rejected() -> None:
    frame = canonical_frame().iloc[::-1].reset_index(drop=True)

    result = validate_frame(frame)

    assert not result.valid
    assert UNSORTED_TIMESTAMP in result.codes()
    assert DUPLICATE_TIMESTAMP not in result.codes()


# --------------------------------------------------------------------------
# Symbol
# --------------------------------------------------------------------------


def test_null_symbol_is_rejected() -> None:
    result = validate_frame(altered(canonical_frame(), "symbol", 0, None))

    assert INVALID_SYMBOL in result.codes()
    assert any("null symbol" in issue.message for issue in result.errors)


def test_multiple_symbols_are_rejected() -> None:
    result = validate_frame(altered(canonical_frame(), "symbol", 2, "QQQ"))

    assert not result.valid
    assert INVALID_SYMBOL in result.codes()
    assert any("distinct symbols" in issue.message for issue in result.errors)
    assert result.symbol is None


def test_unsupported_symbol_is_rejected() -> None:
    result = validate_frame(canonical_frame(symbol="TSLA"))

    assert not result.valid
    assert INVALID_SYMBOL in result.codes()
    assert any("supported universe" in issue.message for issue in result.errors)


def test_lowercase_symbol_is_rejected() -> None:
    result = validate_frame(canonical_frame(symbol="spy"))

    assert not result.valid
    assert INVALID_SYMBOL in result.codes()
    assert any("uppercase" in issue.message for issue in result.errors)


# --------------------------------------------------------------------------
# OHLC
# --------------------------------------------------------------------------


@pytest.mark.parametrize("column", ["open", "high", "low", "close"])
def test_null_ohlc_is_rejected(column: str) -> None:
    result = validate_frame(altered(canonical_frame(), column, 1, np.nan))

    assert not result.valid
    assert NULL_OHLC in result.codes()
    assert any(f"null {column}" in issue.message for issue in result.errors)


@pytest.mark.parametrize("column", ["open", "high", "low", "close"])
@pytest.mark.parametrize("value", [0.0, -1.0])
def test_non_positive_ohlc_is_rejected(column: str, value: float) -> None:
    result = validate_frame(altered(canonical_frame(), column, 1, value))

    assert not result.valid
    assert INVALID_OHLC in result.codes()
    assert any("not greater than zero" in issue.message for issue in result.errors)


@pytest.mark.parametrize(
    ("column", "value", "relationship"),
    [
        ("high", 98.0, "high >= low"),
        ("high", 99.5, "high >= open"),
        ("high", 100.0, "high >= close"),
        ("low", 100.5, "low <= open"),
        ("low", 100.6, "low <= close"),
    ],
)
def test_broken_ohlc_relationships_are_rejected(
    column: str, value: float, relationship: str
) -> None:
    # Row 0 is open=100.0, high=101.0, low=99.0, close=100.5.
    result = validate_frame(altered(canonical_frame(), column, 0, value))

    assert not result.valid
    assert INVALID_OHLC in result.codes()
    assert any(relationship in issue.message for issue in result.errors)


def test_repeated_violations_are_summarized_not_listed_per_row() -> None:
    frame = canonical_frame(row_count=3)
    frame["high"] = frame["low"] - 1.0

    result = validate_frame(frame)

    violations = [issue for issue in result.errors if "high >= low" in issue.message]
    assert len(violations) == 1
    assert violations[0].code == INVALID_OHLC
    assert violations[0].message == "3 rows violate high >= low."


def test_a_single_violation_reads_in_the_singular() -> None:
    result = validate_frame(altered(canonical_frame(), "high", 0, 98.0))

    assert any(issue.message == "1 row violates high >= low." for issue in result.errors)


# --------------------------------------------------------------------------
# Volume, trade_count, vwap
# --------------------------------------------------------------------------


def test_zero_volume_is_allowed() -> None:
    # A bar with no trades is normal market data, not a defect.
    assert validate_frame(altered(canonical_frame(), "volume", 1, 0.0)).valid


def test_negative_volume_is_rejected() -> None:
    result = validate_frame(altered(canonical_frame(), "volume", 1, -5.0))

    assert not result.valid
    assert INVALID_VOLUME in result.codes()


def test_null_volume_is_rejected() -> None:
    result = validate_frame(altered(canonical_frame(), "volume", 1, np.nan))

    assert not result.valid
    assert INVALID_VOLUME in result.codes()


def test_null_trade_count_is_allowed() -> None:
    frame = canonical_frame()
    frame["trade_count"] = np.nan

    assert validate_frame(frame).valid
    assert validate_frame(altered(canonical_frame(), "trade_count", 1, np.nan)).valid


def test_zero_trade_count_is_allowed() -> None:
    assert validate_frame(altered(canonical_frame(), "trade_count", 1, 0.0)).valid


def test_negative_trade_count_is_rejected() -> None:
    result = validate_frame(altered(canonical_frame(), "trade_count", 1, -1.0))

    assert not result.valid
    assert INVALID_TRADE_COUNT in result.codes()


def test_null_vwap_is_allowed() -> None:
    frame = canonical_frame()
    frame["vwap"] = np.nan

    assert validate_frame(frame).valid
    assert validate_frame(altered(canonical_frame(), "vwap", 1, np.nan)).valid


@pytest.mark.parametrize("value", [0.0, -0.5])
def test_non_positive_vwap_is_rejected(value: float) -> None:
    result = validate_frame(altered(canonical_frame(), "vwap", 1, value))

    assert not result.valid
    assert INVALID_VWAP in result.codes()


def test_vwap_outside_the_bar_range_is_not_checked() -> None:
    # Phase 2 deliberately imposes no vwap-versus-OHLC rule.
    assert validate_frame(altered(canonical_frame(), "vwap", 0, 5_000.0)).valid


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        ("open", INVALID_OHLC),
        ("close", INVALID_OHLC),
        ("volume", INVALID_VOLUME),
        ("trade_count", INVALID_TRADE_COUNT),
        ("vwap", INVALID_VWAP),
    ],
)
def test_infinite_values_are_rejected(column: str, expected: str) -> None:
    result = validate_frame(altered(canonical_frame(), column, 1, np.inf))

    assert not result.valid
    assert expected in result.codes()
    assert any("non-finite" in issue.message for issue in result.errors)


def test_negative_infinity_is_rejected() -> None:
    result = validate_frame(altered(canonical_frame(), "low", 1, -np.inf))

    assert INVALID_OHLC in result.codes()


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        ("open", INVALID_OHLC),
        ("volume", INVALID_VOLUME),
        ("trade_count", INVALID_TRADE_COUNT),
        ("vwap", INVALID_VWAP),
    ],
)
def test_non_numeric_columns_are_rejected(column: str, expected: str) -> None:
    frame = canonical_frame()
    frame[column] = frame[column].astype("string")

    result = validate_frame(frame)

    assert not result.valid
    assert expected in result.codes()
    assert any("not numeric" in issue.message for issue in result.errors)


# --------------------------------------------------------------------------
# Purity and error collection
# --------------------------------------------------------------------------


def test_validation_does_not_mutate_the_input_frame() -> None:
    frame = altered(canonical_frame(), "high", 0, 1.0)
    frame.loc[1, "vwap"] = np.nan
    before = frame.copy(deep=True)

    validate_frame(frame)

    pd.testing.assert_frame_equal(frame, before)
    assert list(frame.columns) == list(before.columns)


def test_multiple_problems_are_collected_together() -> None:
    frame = altered(canonical_frame(symbol="tsla"), "volume", 0, -1.0)
    frame.loc[2, "timestamp"] = frame.loc[1, "timestamp"]
    frame["extra"] = 1

    result = validate_frame(frame)

    assert not result.valid
    assert result.error_count >= 4
    for expected in (UNEXPECTED_COLUMN, DUPLICATE_TIMESTAMP, INVALID_SYMBOL, INVALID_VOLUME):
        assert expected in result.codes()


def test_issue_renders_as_code_and_message() -> None:
    issue = validate_frame(canonical_frame(row_count=0)).errors[0]
    assert str(issue) == f"{EMPTY_DATASET}: Dataset contains no rows."


# --------------------------------------------------------------------------
# Reading files
# --------------------------------------------------------------------------


def test_valid_parquet_file_round_trips(tmp_path) -> None:
    path = write_parquet(canonical_frame(), tmp_path / "SPY_15m_2025-01-02_2025-01-02.parquet")

    result = validate_parquet_file(path)

    assert result.valid, result.errors
    assert result.row_count == 3
    assert result.symbol == "SPY"


def test_missing_file_raises_a_controlled_error(tmp_path) -> None:
    with pytest.raises(ValidationInputError) as excinfo:
        read_bars(tmp_path / "absent.parquet")

    assert "No such file" in str(excinfo.value)


def test_directory_raises_a_controlled_error(tmp_path) -> None:
    with pytest.raises(ValidationInputError) as excinfo:
        read_bars(tmp_path)

    assert "Not a file" in str(excinfo.value)


def test_unreadable_parquet_raises_a_controlled_error(tmp_path) -> None:
    path = tmp_path / "corrupt.parquet"
    path.write_bytes(b"this is not parquet")

    with pytest.raises(ValidationInputError) as excinfo:
        read_bars(path)

    assert "Could not read" in str(excinfo.value)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_validate_help_describes_the_command() -> None:
    result = CliRunner(env={"COLUMNS": "120"}).invoke(app, ["validate", "--help"])

    assert result.exit_code == 0
    assert "autotrader validate" in result.output
    assert "path" in result.output
    assert "Parquet" in result.output


def test_cli_validate_accepts_a_valid_dataset(tmp_path) -> None:
    path = write_parquet(canonical_frame(), tmp_path / "SPY_15m_2025-01-02_2025-01-02.parquet")

    result = CliRunner().invoke(app, ["validate", str(path)])

    assert result.exit_code == 0, result.output
    assert "VALID" in result.output
    assert "INVALID" not in result.output
    assert "Rows:   3" in result.output
    assert "Symbol: SPY" in result.output
    assert "Errors: 0" in result.output


def test_cli_validate_rejects_an_invalid_dataset(tmp_path) -> None:
    frame = altered(canonical_frame(), "high", 0, 1.0)
    path = write_parquet(frame, tmp_path / "SPY_15m_2025-01-02_2025-01-02.parquet")

    result = CliRunner().invoke(app, ["validate", str(path)])

    assert result.exit_code == 1
    assert "INVALID" in result.output
    assert f"- {INVALID_OHLC}: " in result.output
    assert "Errors: 0" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_cli_validate_reports_a_missing_file_without_a_traceback(tmp_path) -> None:
    result = CliRunner().invoke(app, ["validate", str(tmp_path / "absent.parquet")])

    assert result.exit_code == 2
    assert "No such file" in result.output
    assert not isinstance(result.exception, ValidationInputError)


def test_cli_validate_reports_an_empty_dataset(tmp_path) -> None:
    path = write_parquet(canonical_frame(row_count=0), tmp_path / "empty.parquet")

    result = CliRunner().invoke(app, ["validate", str(path)])

    assert result.exit_code == 1
    assert f"- {EMPTY_DATASET}: " in result.output
    assert "Rows:   0" in result.output


# --------------------------------------------------------------------------
# Offline guarantee
# --------------------------------------------------------------------------


def test_validation_never_touches_the_network(tmp_path, monkeypatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("validation must not use the network")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    path = write_parquet(canonical_frame(), tmp_path / "SPY_15m_2025-01-02_2025-01-02.parquet")
    assert validate_parquet_file(path).valid

    result = CliRunner().invoke(app, ["validate", str(path)])
    assert result.exit_code == 0, result.output
