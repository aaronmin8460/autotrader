"""C2: structural validation of stored historical crypto bar datasets.

C1 writes canonical Parquet bar files. This module answers exactly one
question about a file that already exists on disk: is it structurally and
internally consistent enough for later stages to consume?

It only reads. It never downloads, never repairs, and never mutates the frame
it is handed. Every check is deterministic and local to a single dataset:
schema, timestamps, symbol, OHLC relationships, volume, trade_count, vwap.
The architecture is unchanged from the archived equity milestone; only the
supported-symbol set moved to the crypto pairs.

**Crypto is continuous, so there is no session to validate against.** There is
no exchange calendar here, no NYSE or Nasdaq session logic, and a weekend or
overnight bar is ordinary data rather than a finding.

Deliberately out of scope (docs/SPEC.md section 8, C2): bar-to-bar spacing and
missing-interval detection, bar freshness, outlier and anomaly heuristics, and
cross-provider comparison. A provider outage can legitimately leave a gap, and
"did we receive the newest completed bar?" is a runtime question that belongs
to the future 24/7 runner, not to structural validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.api.types import is_datetime64_any_dtype, is_numeric_dtype

from autotrader.data.historical import CANONICAL_COLUMNS, SUPPORTED_SYMBOLS

#: Stable, machine-readable issue codes. Messages may be reworded; codes may not.
EMPTY_DATASET = "EMPTY_DATASET"
MISSING_COLUMN = "MISSING_COLUMN"
UNEXPECTED_COLUMN = "UNEXPECTED_COLUMN"
INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
DUPLICATE_TIMESTAMP = "DUPLICATE_TIMESTAMP"
UNSORTED_TIMESTAMP = "UNSORTED_TIMESTAMP"
INVALID_SYMBOL = "INVALID_SYMBOL"
NULL_OHLC = "NULL_OHLC"
INVALID_OHLC = "INVALID_OHLC"
INVALID_VOLUME = "INVALID_VOLUME"
INVALID_TRADE_COUNT = "INVALID_TRADE_COUNT"
INVALID_VWAP = "INVALID_VWAP"

ISSUE_CODES: tuple[str, ...] = (
    EMPTY_DATASET,
    MISSING_COLUMN,
    UNEXPECTED_COLUMN,
    INVALID_TIMESTAMP,
    DUPLICATE_TIMESTAMP,
    UNSORTED_TIMESTAMP,
    INVALID_SYMBOL,
    NULL_OHLC,
    INVALID_OHLC,
    INVALID_VOLUME,
    INVALID_TRADE_COUNT,
    INVALID_VWAP,
)

OHLC_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")

#: Intra-bar relationships every valid bar must satisfy.
OHLC_RELATIONSHIPS: tuple[tuple[str, str, str], ...] = (
    ("high", ">=", "low"),
    ("high", ">=", "open"),
    ("high", ">=", "close"),
    ("low", "<=", "open"),
    ("low", "<=", "close"),
)


class ValidationInputError(Exception):
    """The dataset could not be read at all. The CLI reports these without a traceback.

    This is a file/input failure, not a data-quality finding: a missing path or
    an unreadable Parquet file has no rows to collect issues about.
    """


@dataclass(frozen=True)
class ValidationIssue:
    """One summarized validation problem. Never one issue per offending row."""

    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class ValidationResult:
    """The outcome of validating one dataset."""

    row_count: int
    symbol: str | None
    errors: tuple[ValidationIssue, ...]

    @property
    def valid(self) -> bool:
        """True when no validation issue was found."""
        return not self.errors

    @property
    def error_count(self) -> int:
        """How many issues were found."""
        return len(self.errors)

    def codes(self) -> tuple[str, ...]:
        """The issue codes, in the order they were reported."""
        return tuple(issue.code for issue in self.errors)


# --------------------------------------------------------------------------
# Message helpers - violations are always summarized with a count
# --------------------------------------------------------------------------


def _rows(count: int) -> str:
    """``"1 row"`` or ``"7 rows"``."""
    return "1 row" if count == 1 else f"{count} rows"


def _agree(count: int, singular: str, plural: str) -> str:
    """Pick the verb form that agrees with `count`."""
    return singular if count == 1 else plural


def _as_float(series: pd.Series) -> np.ndarray:
    """Read a numeric column as float64 with nulls as NaN. Does not touch `series`."""
    return series.to_numpy(dtype="float64", na_value=np.nan)


# --------------------------------------------------------------------------
# Individual checks
# --------------------------------------------------------------------------


def _check_columns(frame: pd.DataFrame, errors: list[ValidationIssue]) -> bool:
    """Require exactly the canonical columns. Returns whether row checks can run."""
    seen: set[str] = set()
    duplicated = False
    for column in frame.columns:
        if column in seen:
            duplicated = True
            errors.append(
                ValidationIssue(UNEXPECTED_COLUMN, f"Column {column!r} appears more than once.")
            )
        elif column not in CANONICAL_COLUMNS:
            errors.append(
                ValidationIssue(
                    UNEXPECTED_COLUMN,
                    f"Column {column!r} is not part of the canonical schema.",
                )
            )
        seen.add(column)

    for column in CANONICAL_COLUMNS:
        if column not in seen:
            errors.append(
                ValidationIssue(MISSING_COLUMN, f"Required column {column!r} is missing.")
            )

    # A repeated name makes ``frame[column]`` ambiguous, so per-column checks
    # cannot run meaningfully; the schema errors above are the finding.
    return not duplicated


def _check_timestamp(frame: pd.DataFrame, errors: list[ValidationIssue]) -> None:
    series = frame["timestamp"]
    dtype = series.dtype

    if not isinstance(dtype, pd.DatetimeTZDtype):
        detail = (
            "timestamps are timezone-naive"
            if is_datetime64_any_dtype(dtype)
            else f"timestamp has dtype {dtype}"
        )
        errors.append(
            ValidationIssue(
                INVALID_TIMESTAMP, f"{detail}; timezone-aware UTC timestamps are required."
            )
        )
        return

    if str(dtype.tz) != "UTC":
        errors.append(
            ValidationIssue(
                INVALID_TIMESTAMP, f"timestamp timezone is {dtype.tz}; UTC is required."
            )
        )

    null_count = int(series.isna().sum())
    if null_count:
        errors.append(
            ValidationIssue(
                INVALID_TIMESTAMP,
                f"{_rows(null_count)} {_agree(null_count, 'has', 'have')} a null timestamp.",
            )
        )
        # Ordering and uniqueness are not meaningful with missing values.
        return

    duplicate_count = int(series.duplicated().sum())
    if duplicate_count:
        errors.append(
            ValidationIssue(
                DUPLICATE_TIMESTAMP,
                f"{_rows(duplicate_count)} "
                f"{_agree(duplicate_count, 'repeats', 'repeat')} an earlier timestamp.",
            )
        )

    if not series.is_monotonic_increasing:
        errors.append(ValidationIssue(UNSORTED_TIMESTAMP, "timestamps are not in ascending order."))


def _check_symbol(frame: pd.DataFrame, errors: list[ValidationIssue]) -> None:
    series = frame["symbol"]

    null_count = int(series.isna().sum())
    if null_count:
        errors.append(
            ValidationIssue(
                INVALID_SYMBOL,
                f"{_rows(null_count)} {_agree(null_count, 'has', 'have')} a null symbol.",
            )
        )

    values = list(series.dropna().unique())
    if not values:
        return

    non_strings = sorted({repr(value) for value in values if not isinstance(value, str)})
    if non_strings:
        errors.append(
            ValidationIssue(
                INVALID_SYMBOL, f"Symbol values must be strings: {', '.join(non_strings)}."
            )
        )

    if len(values) > 1:
        listed = ", ".join(sorted(str(value) for value in values))
        errors.append(
            ValidationIssue(
                INVALID_SYMBOL,
                f"Dataset contains {len(values)} distinct symbols ({listed}); "
                "exactly one is required.",
            )
        )

    strings = [value for value in values if isinstance(value, str)]
    lowercase = sorted(value for value in strings if value != value.upper())
    if lowercase:
        errors.append(
            ValidationIssue(
                INVALID_SYMBOL, f"Symbol values must be uppercase: {', '.join(lowercase)}."
            )
        )

    unsupported = sorted(value for value in strings if value.upper() not in SUPPORTED_SYMBOLS)
    if unsupported:
        supported = ", ".join(SUPPORTED_SYMBOLS)
        errors.append(
            ValidationIssue(
                INVALID_SYMBOL,
                f"Symbol outside the supported pair universe: {', '.join(unsupported)}. "
                f"Supported symbols are: {supported}.",
            )
        )


def _check_numeric_column(
    frame: pd.DataFrame,
    column: str,
    errors: list[ValidationIssue],
    *,
    code: str,
    null_code: str | None,
    strictly_positive: bool,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Run the shared numeric checks on one column.

    `null_code` is ``None`` for a column the contract allows to be null. Returns
    ``(values, usable)`` where `usable` marks rows whose value is present and
    finite, or ``None`` when the column is unusable.
    """
    series = frame[column]

    if null_code is None and series.isna().all():
        return None  # Nullable by contract; an all-null column is acceptable.

    if not is_numeric_dtype(series):
        errors.append(
            ValidationIssue(code, f"Column {column!r} is not numeric (dtype {series.dtype}).")
        )
        return None

    values = _as_float(series)
    missing = np.isnan(values)
    missing_count = int(missing.sum())
    if missing_count and null_code is not None:
        errors.append(
            ValidationIssue(
                null_code,
                f"{_rows(missing_count)} {_agree(missing_count, 'has', 'have')} a null {column}.",
            )
        )

    present = ~missing
    infinite = present & np.isinf(values)
    infinite_count = int(infinite.sum())
    if infinite_count:
        errors.append(
            ValidationIssue(
                code,
                f"{_rows(infinite_count)} {_agree(infinite_count, 'has', 'have')} "
                f"a non-finite {column}.",
            )
        )

    usable = present & ~infinite
    if strictly_positive:
        out_of_range = usable & (values <= 0)
        description = "that is not greater than zero"
    else:
        out_of_range = usable & (values < 0)
        description = "that is negative"
    out_of_range_count = int(out_of_range.sum())
    if out_of_range_count:
        errors.append(
            ValidationIssue(
                code,
                f"{_rows(out_of_range_count)} "
                f"{_agree(out_of_range_count, 'has', 'have')} a {column} {description}.",
            )
        )

    return values, usable


def _check_ohlc(frame: pd.DataFrame, errors: list[ValidationIssue]) -> None:
    checked: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for column in OHLC_COLUMNS:
        if column not in frame.columns:
            continue
        outcome = _check_numeric_column(
            frame,
            column,
            errors,
            code=INVALID_OHLC,
            null_code=NULL_OHLC,
            strictly_positive=True,
        )
        if outcome is not None:
            checked[column] = outcome

    for left, relation, right in OHLC_RELATIONSHIPS:
        if left not in checked or right not in checked:
            continue
        left_values, left_usable = checked[left]
        right_values, right_usable = checked[right]
        comparable = left_usable & right_usable
        holds = left_values >= right_values if relation == ">=" else left_values <= right_values
        violations = comparable & ~holds
        count = int(violations.sum())
        if count:
            errors.append(
                ValidationIssue(
                    INVALID_OHLC,
                    f"{_rows(count)} {_agree(count, 'violates', 'violate')} "
                    f"{left} {relation} {right}.",
                )
            )


def _resolve_symbol(frame: pd.DataFrame) -> str | None:
    """The dataset's symbol, when a single one is determinable."""
    if "symbol" not in frame.columns or frame.columns.duplicated().any():
        return None
    values = frame["symbol"].dropna().unique()
    if len(values) != 1:
        return None
    value = values[0]
    return value if isinstance(value, str) else None


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------


def validate_frame(frame: pd.DataFrame) -> ValidationResult:
    """Validate an in-memory canonical bar frame. `frame` is never modified."""
    errors: list[ValidationIssue] = []
    row_checks_possible = _check_columns(frame, errors)

    row_count = len(frame)
    if row_count == 0:
        errors.append(ValidationIssue(EMPTY_DATASET, "Dataset contains no rows."))
        return ValidationResult(row_count=0, symbol=None, errors=tuple(errors))

    symbol = _resolve_symbol(frame)
    if not row_checks_possible:
        return ValidationResult(row_count=row_count, symbol=symbol, errors=tuple(errors))

    if "timestamp" in frame.columns:
        _check_timestamp(frame, errors)
    if "symbol" in frame.columns:
        _check_symbol(frame, errors)

    _check_ohlc(frame, errors)

    if "volume" in frame.columns:
        _check_numeric_column(
            frame,
            "volume",
            errors,
            code=INVALID_VOLUME,
            null_code=INVALID_VOLUME,
            strictly_positive=False,
        )
    if "trade_count" in frame.columns:
        _check_numeric_column(
            frame,
            "trade_count",
            errors,
            code=INVALID_TRADE_COUNT,
            null_code=None,
            strictly_positive=False,
        )
    if "vwap" in frame.columns:
        _check_numeric_column(
            frame,
            "vwap",
            errors,
            code=INVALID_VWAP,
            null_code=None,
            strictly_positive=True,
        )

    return ValidationResult(row_count=row_count, symbol=symbol, errors=tuple(errors))


def read_bars(path: Path) -> pd.DataFrame:
    """Read a stored Parquet bar dataset, or raise a controlled input error."""
    dataset_path = Path(path)
    if not dataset_path.exists():
        raise ValidationInputError(f"No such file: {dataset_path}")
    if not dataset_path.is_file():
        raise ValidationInputError(f"Not a file: {dataset_path}")
    try:
        return pd.read_parquet(dataset_path, engine="pyarrow")
    except Exception as exc:  # noqa: BLE001 - any reader failure is one input error
        raise ValidationInputError(f"Could not read {dataset_path} as Parquet: {exc}") from exc


def validate_parquet_file(path: Path) -> ValidationResult:
    """Read a stored Parquet bar dataset and validate it."""
    return validate_frame(read_bars(path))


__all__ = [
    "DUPLICATE_TIMESTAMP",
    "EMPTY_DATASET",
    "INVALID_OHLC",
    "INVALID_SYMBOL",
    "INVALID_TIMESTAMP",
    "INVALID_TRADE_COUNT",
    "INVALID_VOLUME",
    "INVALID_VWAP",
    "ISSUE_CODES",
    "MISSING_COLUMN",
    "NULL_OHLC",
    "OHLC_COLUMNS",
    "OHLC_RELATIONSHIPS",
    "UNEXPECTED_COLUMN",
    "UNSORTED_TIMESTAMP",
    "ValidationInputError",
    "ValidationIssue",
    "ValidationResult",
    "read_bars",
    "validate_frame",
    "validate_parquet_file",
]
