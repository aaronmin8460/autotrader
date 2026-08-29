"""Dataset provenance: what was downloaded, what was corrected, and its fingerprint.

The evaluation dataset is the provider's 15-minute crypto bars, downloaded
through `autotrader.data.historical` - the project's only market-data path -
and stored unmodified. This module reads that raw file, applies exactly one
documented correction, and records everything a reader needs to reproduce or
challenge the result.

**The one correction, and why it is not a repair.** The provider publishes
``vwap = 0`` on bars where no trade occurred on its own venue. Zero is not a
volume-weighted average price; it is the provider's spelling of "undefined",
and the canonical schema already has a spelling for that - the C2 contract
treats `vwap` as nullable and checks only the values that are present. So the
correction is a re-encoding, from a sentinel the validator reads as a price of
zero into the null the schema reserves for a measurement nobody took. It is
applied only to rows that carry `trade_count == 0` and `volume == 0`, it is
counted and reported, and it touches no field any engine reads: `vwap` is
absent from the V4 feature contract and from every V1/V2/V3 factor, and the
simulator fills on `open` and marks on `close`.

**No price is ever invented.** Missing bars stay missing. Gaps are reported,
not interpolated - `autotrader.ml.grid` exists precisely so a hole stays
visible as a hole, and a study that filled one would be scoring a bar the
market never printed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from autotrader.data.validation import validate_frame
from autotrader.research.reproducibility import dataset_digest

#: The provider's sentinel for "this bar had no trades, so its vwap is undefined".
UNDEFINED_VWAP_SENTINEL = 0.0

#: The base timeframe every engine in this study decides on.
BAR_INTERVAL = pd.Timedelta("15min")


class DatasetError(Exception):
    """The evaluation dataset cannot be built from what was supplied."""


@dataclass(frozen=True)
class GapReport:
    """Where the provider published nothing, described rather than filled."""

    expected_bars: int
    observed_bars: int
    missing_bars: int
    gap_events: int
    longest_gap: str | None
    largest_gaps: tuple[dict[str, object], ...] = field(default_factory=tuple)

    @property
    def missing_fraction(self) -> float:
        return 0.0 if not self.expected_bars else self.missing_bars / self.expected_bars

    def to_record(self) -> dict[str, object]:
        return {
            "expected_bars": self.expected_bars,
            "observed_bars": self.observed_bars,
            "missing_bars": self.missing_bars,
            "missing_fraction": self.missing_fraction,
            "gap_events": self.gap_events,
            "longest_gap": self.longest_gap,
            "largest_gaps": list(self.largest_gaps),
        }


@dataclass(frozen=True)
class DatasetProvenance:
    """Everything that identifies one symbol's evaluation dataset."""

    symbol: str
    source_path: str
    provider: str
    feed: str
    timeframe: str
    timezone: str
    first_timestamp: str
    last_timestamp: str
    row_count: int
    duplicate_timestamps: int
    monotonic: bool
    gaps: GapReport
    zero_volume_bars: int
    zero_trade_bars: int
    undefined_vwap_rows_renulled: int
    ohlc_violations: int
    non_positive_prices: int
    raw_file_sha256: str
    frame_digest: str
    validation_passed: bool
    validation_errors: tuple[str, ...]

    def to_record(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "source_path": self.source_path,
            "provider": self.provider,
            "feed": self.feed,
            "timeframe": self.timeframe,
            "timezone": self.timezone,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "row_count": self.row_count,
            "duplicate_timestamps": self.duplicate_timestamps,
            "timestamps_monotonic": self.monotonic,
            "gaps": self.gaps.to_record(),
            "zero_volume_bars": self.zero_volume_bars,
            "zero_trade_bars": self.zero_trade_bars,
            "zero_volume_fraction": (
                0.0 if not self.row_count else self.zero_volume_bars / self.row_count
            ),
            "corrections": {
                "undefined_vwap_renulled": self.undefined_vwap_rows_renulled,
                "description": (
                    "vwap sentinel 0 re-encoded as null on bars with trade_count == 0 "
                    "and volume == 0; no price field altered, no bar synthesized"
                ),
            },
            "ohlc_violations": self.ohlc_violations,
            "non_positive_prices": self.non_positive_prices,
            "raw_file_sha256": self.raw_file_sha256,
            "frame_digest": self.frame_digest,
            "validation_passed": self.validation_passed,
            "validation_errors": list(self.validation_errors),
        }


def file_sha256(path: Path) -> str:
    """The digest of the stored file exactly as the provider's download left it."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def describe_gaps(timestamps: pd.Series, *, top: int = 5) -> GapReport:
    """Where bars are absent between the first and last observed one."""
    index = pd.DatetimeIndex(timestamps)
    expected = pd.date_range(index[0], index[-1], freq=BAR_INTERVAL, tz="UTC")
    missing = expected.difference(index)
    deltas = index.to_series().diff().dropna()
    jumps = deltas[deltas > BAR_INTERVAL]
    ranked = jumps.sort_values(ascending=False).head(top)
    return GapReport(
        expected_bars=len(expected),
        observed_bars=len(index),
        missing_bars=len(missing),
        gap_events=int(len(jumps)),
        longest_gap=str(jumps.max()) if len(jumps) else None,
        largest_gaps=tuple(
            {"resumed_at": str(moment), "gap": str(size)} for moment, size in ranked.items()
        ),
    )


def renull_undefined_vwap(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Re-encode the provider's zero-vwap sentinel as the schema's null.

    Applied only where the provider also reported no trades and no volume, so a
    genuine zero - which would be a data fault worth failing on - is left alone
    to fail validation. Returns a copy; the input is never modified.
    """
    corrected = frame.copy()
    undefined = (
        (corrected["vwap"] <= UNDEFINED_VWAP_SENTINEL)
        & (corrected["trade_count"] == 0)
        & (corrected["volume"] == 0)
    )
    count = int(undefined.sum())
    if count:
        corrected.loc[undefined, "vwap"] = pd.NA
        corrected["vwap"] = corrected["vwap"].astype("Float64").astype("float64")
    return corrected, count


def load_evaluation_frame(path: Path) -> tuple[pd.DataFrame, DatasetProvenance]:
    """Read one raw download and return the frame this study evaluates, plus its provenance."""
    source = Path(path)
    if not source.is_file():
        raise DatasetError(f"No such dataset file: {source}")

    raw = pd.read_parquet(source, engine="pyarrow")
    if raw.empty:
        raise DatasetError(f"{source} contains no rows.")

    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    raw = raw.sort_values("timestamp").reset_index(drop=True)
    symbols = pd.unique(raw["symbol"])
    if len(symbols) != 1:
        raise DatasetError(f"{source} holds {len(symbols)} symbols; a study frame holds one.")
    symbol = str(symbols[0])

    frame, renulled = renull_undefined_vwap(raw)
    result = validate_frame(frame)

    highs = frame[["open", "close", "low"]].max(axis=1)
    lows = frame[["open", "close", "high"]].min(axis=1)
    violations = int(((frame["high"] < highs) | (frame["low"] > lows)).sum())

    metadata_path = source.with_name(f"{source.stem}.metadata.json")
    metadata: dict[str, object] = {}
    if metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text())

    provenance = DatasetProvenance(
        symbol=symbol,
        source_path=str(source),
        provider=str(metadata.get("provider", "unknown")),
        feed=str(metadata.get("feed", "unknown")),
        timeframe=str(metadata.get("timeframe", "15m")),
        timezone="UTC",
        first_timestamp=frame["timestamp"].iloc[0].isoformat(),
        last_timestamp=frame["timestamp"].iloc[-1].isoformat(),
        row_count=len(frame),
        duplicate_timestamps=int(frame["timestamp"].duplicated().sum()),
        monotonic=bool(frame["timestamp"].is_monotonic_increasing),
        gaps=describe_gaps(frame["timestamp"]),
        zero_volume_bars=int((frame["volume"] == 0).sum()),
        zero_trade_bars=int((frame["trade_count"] == 0).sum()),
        undefined_vwap_rows_renulled=renulled,
        ohlc_violations=violations,
        non_positive_prices=int((frame[["open", "high", "low", "close"]] <= 0).sum().sum()),
        raw_file_sha256=file_sha256(source),
        frame_digest=dataset_digest(frame),
        validation_passed=result.valid,
        validation_errors=tuple(str(issue) for issue in result.errors),
    )
    return frame, provenance


__all__ = [
    "BAR_INTERVAL",
    "UNDEFINED_VWAP_SENTINEL",
    "DatasetError",
    "DatasetProvenance",
    "GapReport",
    "describe_gaps",
    "file_sha256",
    "load_evaluation_frame",
    "renull_undefined_vwap",
]
