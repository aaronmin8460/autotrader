"""M1: the historical feature-dataset builder.

Takes a canonical Parquet bar file that already exists on disk, a bar grid, and
a label specification, and produces one versioned dataset: a Parquet file whose
columns are exactly the schema, plus a metadata sidecar that records everything
needed to rebuild it.

**Nothing is downloaded.** The input is a file written earlier by
`autotrader.data.historical` or `autotrader.equity.data`. That is what makes a
build reproducible: the same bar file, the same grid and the same label
specification produce a byte-for-byte identical frame, today and next year.

**The bars are validated before they are used, and never repaired.** C2's
`validate_frame` is the project's structural bar contract - duplicates, OHLC
relationships, symbol universe - and it is called rather than re-implemented. A
dataset that fails it aborts the build. No row is dropped, corrected, or
back-filled to make a bad input usable.

**The grid decides which rows exist; the file decides which are populated.**
Bars are reindexed onto the grid, so an interval the provider never published
is present as a row with NaN prices and `is_present=False`. Its neighbours can
see the hole, every rolling window that covers it yields NaN, and
`bars_present_in_window` counts what was really there. A missing bar is never
forward-filled: a stale price wearing a fresh timestamp is worse than an
absence, because only the absence is visible downstream.

**Two timestamps, because they are two different facts.**
`feature_timestamp` is the bar the features end on. `knowable_at` is when that
row could first have existed. A live V4 scoring the same symbol would produce
this row at `knowable_at` and not a moment before, which is what makes a
backtest over this dataset a claim about something that could have happened.

**Unlabelled rows are kept.** The last `exit_offset_bars` rows of any dataset
have no future to measure, and rows excluded by the session policy have no
target either. They keep their features and carry `label_valid=False`. They are
exactly the rows a live system scores, so a dataset that silently dropped them
would not describe the thing it is meant to describe; `labelled_frame` is the
one-line filter a training run applies.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from autotrader import __version__
from autotrader.data.validation import (
    CRYPTO_UNIVERSE_LABEL,
    EQUITY_UNIVERSE_LABEL,
    read_bars,
    validate_frame,
)
from autotrader.equity.session import MarketSession
from autotrader.ml import (
    SUPPORTED_TIMEFRAME,
    AssetClass,
    MLError,
    asset_class_for_symbol,
    filesystem_slug,
    normalize_symbol,
    normalize_timeframe,
    symbols_for,
)
from autotrader.ml.features import (
    FEATURE_COLUMNS,
    FEATURE_NAMES,
    VOLATILITY_FEATURE,
    bars_present_in_window,
    compute_features,
)
from autotrader.ml.grid import BarGrid, build_grid, now_utc, utc_day_bounds
from autotrader.ml.labels import LabelSpec, ThresholdMode, compute_labels, label_columns
from autotrader.ml.schema import (
    FEATURE_SCHEMA_VERSION,
    FEATURE_WINDOW_BARS,
    FeatureSchema,
    build_schema,
)
from autotrader.ml.storage import (
    canonical_json,
    dataset_root,
    ensure_directory,
    sha256_of_file,
    sha256_of_record,
    write_json,
)
from autotrader.runtime.schedule import BAR_INTERVAL

#: The bar columns the builder reads. A subset of the canonical contract: this
#: package has no use for `trade_count` or `vwap`, and reading columns it does
#: not use would make a dataset depend on fields that may be null.
BAR_COLUMNS: tuple[str, ...] = ("timestamp", "symbol", "open", "high", "low", "close", "volume")

#: Suffix of the metadata sidecar written beside every dataset.
METADATA_SUFFIX = ".metadata.json"


class DatasetError(MLError):
    """A dataset could not be built from what it was given."""


@dataclass(frozen=True)
class DatasetSpec:
    """Everything that decides what a built dataset contains.

    Fingerprinted as a whole, so "the same dataset" is a checkable claim rather
    than a filename convention.
    """

    symbol: str
    label: LabelSpec
    timeframe: str = SUPPORTED_TIMEFRAME
    minimum_bars_present_in_window: int = FEATURE_WINDOW_BARS

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "timeframe", normalize_timeframe(self.timeframe))
        if not isinstance(self.label, LabelSpec):
            raise DatasetError(f"label must be a LabelSpec, got {type(self.label).__name__}.")
        minimum = self.minimum_bars_present_in_window
        if isinstance(minimum, bool) or not isinstance(minimum, int):
            raise DatasetError("minimum_bars_present_in_window must be an int.")
        if not 1 <= minimum <= FEATURE_WINDOW_BARS:
            raise DatasetError(
                f"minimum_bars_present_in_window must be between 1 and "
                f"{FEATURE_WINDOW_BARS} (the longest feature window), got {minimum}. "
                "Anything below the window length admits rows whose slowest "
                "features are still NaN."
            )

    @property
    def asset_class(self) -> AssetClass:
        """Which universe - and therefore which bar clock - this symbol belongs to."""
        return asset_class_for_symbol(self.symbol)

    def to_record(self) -> dict[str, object]:
        """The serializable, fingerprinted form."""
        return {
            "symbol": self.symbol,
            "asset_class": self.asset_class.value,
            "timeframe": self.timeframe,
            "minimum_bars_present_in_window": self.minimum_bars_present_in_window,
            "label": self.label.to_record(),
        }

    @property
    def fingerprint(self) -> str:
        """SHA-256 over the specification."""
        return sha256_of_record(self.to_record())

    def stem(self, grid: BarGrid) -> str:
        """The deterministic basename of the dataset this specification builds."""
        first = grid.first_start.date().isoformat()
        last = grid.last_start.date().isoformat()
        return (
            f"{filesystem_slug(self.symbol)}_{self.timeframe}_{first}_{last}"
            f"_{self.label.identifier}_fs{FEATURE_SCHEMA_VERSION}"
        )


@dataclass(frozen=True)
class DatasetBuild:
    """A built dataset, in memory, with the provenance of how it was built."""

    frame: pd.DataFrame
    schema: FeatureSchema
    spec: DatasetSpec
    grid: BarGrid
    grid_row_count: int
    missing_bar_count: int
    dropped_incomplete_window_count: int

    @property
    def row_count(self) -> int:
        return len(self.frame)

    @property
    def labelled_row_count(self) -> int:
        """How many rows carry a usable target."""
        return int(self.frame["label_valid"].fillna(False).sum())

    @property
    def fingerprint(self) -> str:
        """SHA-256 over the frame's contents. Equal builds, equal fingerprints."""
        return frame_fingerprint(self.frame)


@dataclass(frozen=True)
class DatasetArtifact:
    """Where a built dataset was written, and what identifies it."""

    parquet_path: Path
    metadata_path: Path
    row_count: int
    labelled_row_count: int
    fingerprint: str
    schema_version: str
    schema_fingerprint: str


# --------------------------------------------------------------------------
# Schema composition
# --------------------------------------------------------------------------


def dataset_schema(label: LabelSpec) -> FeatureSchema:
    """The full column contract a dataset with this label carries.

    Composed here because the three owners are separate on purpose: the fixed
    columns belong to `schema`, the features to `features`, and the target to
    `labels`. Nobody restates anybody else's list.
    """
    return build_schema(FEATURE_COLUMNS, label_columns(label))


# --------------------------------------------------------------------------
# Bars onto a grid
# --------------------------------------------------------------------------


def _validate_bars(bars: pd.DataFrame, asset_class: AssetClass, symbol: str) -> None:
    """Refuse bars that do not satisfy the project's structural contract."""
    label = CRYPTO_UNIVERSE_LABEL if asset_class is AssetClass.CRYPTO else EQUITY_UNIVERSE_LABEL
    result = validate_frame(bars, supported_symbols=symbols_for(asset_class), universe_label=label)
    if not result.valid:
        issues = "; ".join(str(issue) for issue in result.errors)
        raise DatasetError(
            f"The bar dataset for {symbol} is not valid and will not be used to build "
            f"features: {issues}"
        )
    if result.symbol is not None and result.symbol != symbol:
        raise DatasetError(
            f"The bar dataset holds {result.symbol!r} but the dataset specification "
            f"names {symbol!r}."
        )


def build_observations(bars: pd.DataFrame, grid: BarGrid, symbol: str) -> pd.DataFrame:
    """Place `bars` onto `grid`, one row per grid bar, holes included.

    Any bar in the file that is not on the grid is a contract violation rather
    than surplus data: on an equity grid it is an extended-hours candle or a
    bar from a session the supplied calendar does not contain, and silently
    discarding it would hide a mismatched calendar.
    """
    frame = bars.loc[:, list(BAR_COLUMNS)].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    index = pd.DatetimeIndex(grid.starts, name="timestamp")

    off_grid = frame.loc[~frame["timestamp"].isin(index), "timestamp"]
    if not off_grid.empty:
        sample = ", ".join(moment.isoformat() for moment in off_grid.head(3))
        raise DatasetError(
            f"{len(off_grid)} bar(s) fall outside the {grid.asset_class.value} grid "
            f"(for example {sample}). On an equity grid this usually means the bar "
            "file holds extended-hours candles, or the session calendar does not "
            "cover the file's range."
        )

    placed = frame.set_index("timestamp").reindex(index)
    observations = pd.DataFrame(
        {
            "timestamp": index,
            "symbol": pd.array([symbol] * len(grid), dtype="string"),
            "open": placed["open"].astype("float64").to_numpy(),
            "high": placed["high"].astype("float64").to_numpy(),
            "low": placed["low"].astype("float64").to_numpy(),
            "close": placed["close"].astype("float64").to_numpy(),
            "volume": placed["volume"].astype("float64").to_numpy(),
            "is_present": placed["close"].notna().to_numpy(),
            "session_id": pd.array(list(grid.session_ids), dtype="string"),
            "session_bar_index": np.asarray(grid.session_bar_indices, dtype="int64"),
            "session_bar_count": np.asarray(grid.session_bar_counts, dtype="int64"),
        }
    )
    return observations.reset_index(drop=True)


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------


def build_dataset(bars: pd.DataFrame, *, spec: DatasetSpec, grid: BarGrid) -> DatasetBuild:
    """Build the feature dataset in memory. Pure: no clock, no filesystem, no network.

    The supplied bars are validated, placed on the grid, turned into features
    and labels, filtered by the window-completeness policy, and returned in
    exactly the schema's column order and dtypes.
    """
    if grid.asset_class is not spec.asset_class:
        raise DatasetError(
            f"{spec.symbol} is a {spec.asset_class.value} symbol but the grid is a "
            f"{grid.asset_class.value} grid."
        )
    _validate_bars(bars, spec.asset_class, spec.symbol)

    observations = build_observations(bars, grid, spec.symbol)
    features = compute_features(observations, has_session_gaps=grid.has_session_gaps)
    present_counts = bars_present_in_window(observations)
    volatility = (
        features[VOLATILITY_FEATURE]
        if spec.label.threshold_mode is ThresholdMode.VOLATILITY
        else None
    )
    labels = compute_labels(observations, grid, spec.label, volatility=volatility)

    schema = dataset_schema(spec.label)
    assembled = pd.DataFrame(
        {
            "symbol": observations["symbol"],
            "feature_timestamp": observations["timestamp"],
            "knowable_at": observations["timestamp"] + BAR_INTERVAL,
            "asset_class": pd.array([grid.asset_class.value] * len(grid), dtype="string"),
            "grid_index": np.arange(len(grid), dtype="int64"),
            "session_id": observations["session_id"],
            "session_bar_count": observations["session_bar_count"],
            "bars_present_in_window": present_counts,
        }
    )
    for name in FEATURE_NAMES:
        assembled[name] = features[name]
    for column in label_columns(spec.label):
        assembled[column.name] = labels[column.name]

    keep = observations["is_present"].to_numpy(dtype=bool) & (
        present_counts.to_numpy(dtype="int64") >= spec.minimum_bars_present_in_window
    )
    dropped_windows = int((observations["is_present"].to_numpy(dtype=bool) & ~keep).sum())
    frame = assembled.loc[keep].reset_index(drop=True)
    frame = frame[list(schema.names)]
    for name, dtype in schema.dtypes.items():
        frame[name] = frame[name].astype(dtype)
    schema.validate_frame(frame)

    return DatasetBuild(
        frame=frame,
        schema=schema,
        spec=spec,
        grid=grid,
        grid_row_count=len(grid),
        missing_bar_count=int((~observations["is_present"].to_numpy(dtype=bool)).sum()),
        dropped_incomplete_window_count=dropped_windows,
    )


def labelled_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """The rows of a built dataset that carry a usable target.

    The one filter a training run applies. Separate from the build so that a
    dataset keeps the unlabelled tail a live system would still score.
    """
    if "label_valid" not in frame.columns:
        raise DatasetError("This frame has no label_valid column and is not a built dataset.")
    return frame.loc[frame["label_valid"].fillna(False).to_numpy(dtype=bool)].reset_index(drop=True)


def frame_fingerprint(frame: pd.DataFrame) -> str:
    """A content fingerprint of a built dataset.

    Covers the column names, the declared dtypes and every value, so two builds
    of the same specification over the same bars agree and any difference at
    all disagrees. Computed from a canonical JSON rendering rather than from
    the Parquet bytes: Parquet embeds a writer version and compression choices,
    so identical data can produce different files, and a fingerprint that moved
    when pyarrow was upgraded would report a change that never happened.
    """
    body = frame.to_json(orient="split", date_format="iso", double_precision=15, index=False)
    header = canonical_json(
        {
            "columns": [str(name) for name in frame.columns],
            "dtypes": [str(dtype) for dtype in frame.dtypes],
            "row_count": len(frame),
        }
    )
    return sha256_of_record({"header": header, "body": body})


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def build_metadata(
    build: DatasetBuild,
    *,
    source_path: Path | None,
    source_sha256: str | None,
    built_at: datetime,
    parquet_filename: str,
) -> dict[str, object]:
    """The reproducibility sidecar. Never contains a credential or account detail.

    Everything an operator needs to answer "what is this file?" without opening
    it: the schema and its fingerprint, the label and the interval it measures
    in words, the grid, the source bars and their hash, the row counts, and the
    code version that produced it.
    """
    return {
        "dataset_fingerprint": build.fingerprint,
        "built_at_utc": built_at.isoformat(),
        "autotrader_version": __version__,
        "pandas_version": pd.__version__,
        "parquet_filename": parquet_filename,
        "specification": build.spec.to_record(),
        "specification_fingerprint": build.spec.fingerprint,
        "label_interval": build.spec.label.describe(),
        "label_classes": build.spec.label.classes,
        "feature_schema": build.schema.to_record(),
        "feature_schema_fingerprint": build.schema.fingerprint,
        "grid": build.grid.to_record(),
        "source_bars": {
            "path": None if source_path is None else str(source_path),
            "sha256": source_sha256,
        },
        "counts": {
            "grid_bars": build.grid_row_count,
            "missing_bars": build.missing_bar_count,
            "dropped_incomplete_window_rows": build.dropped_incomplete_window_count,
            "rows": build.row_count,
            "labelled_rows": build.labelled_row_count,
        },
    }


def write_dataset(
    build: DatasetBuild,
    *,
    output_dir: Path | None = None,
    source_path: Path | None = None,
    built_at: datetime | None = None,
) -> DatasetArtifact:
    """Write the built dataset and its metadata sidecar to external storage.

    `output_dir` defaults to `AUTOTRADER_QA_DATASETS`, which is where heavy
    artifacts belong; passing one explicitly is for tests. The Parquet file is
    written through C1's atomic helper by way of pandas, and the sidecar
    through the storage boundary's secret check.
    """
    directory = ensure_directory(Path(output_dir) if output_dir is not None else dataset_root())
    stem = build.spec.stem(build.grid)
    parquet_path = directory / f"{stem}.parquet"
    metadata_path = directory / f"{stem}{METADATA_SUFFIX}"

    build.frame.to_parquet(parquet_path, engine="pyarrow", index=False)
    metadata = build_metadata(
        build,
        source_path=source_path,
        source_sha256=None if source_path is None else sha256_of_file(source_path),
        built_at=built_at if built_at is not None else now_utc(),
        parquet_filename=parquet_path.name,
    )
    write_json(metadata_path, metadata)
    return DatasetArtifact(
        parquet_path=parquet_path,
        metadata_path=metadata_path,
        row_count=build.row_count,
        labelled_row_count=build.labelled_row_count,
        fingerprint=build.fingerprint,
        schema_version=build.schema.version,
        schema_fingerprint=build.schema.fingerprint,
    )


def read_dataset(path: Path) -> pd.DataFrame:
    """Read a built dataset back from Parquet."""
    target = Path(path)
    if not target.is_file():
        raise DatasetError(f"No such dataset file: {target}")
    try:
        return pd.read_parquet(target, engine="pyarrow")
    except Exception as error:  # noqa: BLE001 - any reader failure is one input error
        raise DatasetError(f"Could not read {target} as Parquet: {error}") from error


def grid_for_bars(
    bars: pd.DataFrame,
    spec: DatasetSpec,
    *,
    sessions: Sequence[MarketSession] | None = None,
) -> BarGrid:
    """The grid a bar file should be built on.

    Crypto sizes its grid from the file's own first and last timestamps,
    because every boundary between them existed. Equity cannot: the file's
    range says nothing about which days were sessions or when they closed, so
    an explicit calendar is required and the grid is exactly what it reports.
    """
    if spec.asset_class is AssetClass.CRYPTO:
        first, last = utc_day_bounds(pd.to_datetime(bars["timestamp"], utc=True).dt.to_pydatetime())
        return build_grid(AssetClass.CRYPTO, start=first, end=last)
    return build_grid(AssetClass.EQUITY, sessions=sessions)


def build_dataset_from_parquet(
    bars_path: Path,
    *,
    spec: DatasetSpec,
    sessions: Sequence[MarketSession] | None = None,
    output_dir: Path | None = None,
    built_at: datetime | None = None,
) -> DatasetArtifact:
    """Read stored bars, build the dataset, and write it. The CLI's whole job."""
    source = Path(bars_path)
    bars = read_bars(source)
    grid = grid_for_bars(bars, spec, sessions=sessions)
    build = build_dataset(bars, spec=spec, grid=grid)
    return write_dataset(build, output_dir=output_dir, source_path=source, built_at=built_at)


__all__ = [
    "BAR_COLUMNS",
    "METADATA_SUFFIX",
    "DatasetArtifact",
    "DatasetBuild",
    "DatasetError",
    "DatasetSpec",
    "build_dataset",
    "build_dataset_from_parquet",
    "build_metadata",
    "build_observations",
    "dataset_schema",
    "frame_fingerprint",
    "grid_for_bars",
    "labelled_frame",
    "read_dataset",
    "write_dataset",
]
