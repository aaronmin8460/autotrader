"""Normalise the acquired dumps into causal, fingerprinted frames.

Two outputs per symbol, both carrying the `knowable_at` stamp that every
downstream join is required to respect:

* funding  - one row per settlement: `source_timestamp` (`calc_time`),
  `publication_timestamp` (the settlement instant itself - the venue
  publishes the settled rate at settlement), `knowable_at` = `ceil(calc_time, 1s)`.
* premium  - one row per 15m bar: `bar_open`, `bar_close`
  (= `bar_open + 15m - 1ms`), `knowable_at` = `bar_close + 1ms`.

The one-second ceil on funding absorbs the venue's observed sub-second
settlement jitter conservatively: it can only ever make a datum knowable
*later* than it truly was, never earlier.

Validation is fail-loud, matching the pilot's predeclared hard rejection
triggers: schema drift, an off-grid settlement, a funding interval that is
not 8h without a matching interval column, duplicate timestamps, or a
non-monotonic series each raise rather than being silently repaired.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from studies.crypto_funding_basis_pilot.acquire import DATASET_DIR, SYMBOL_MAP

NORMALIZED_DIR = DATASET_DIR / "normalized"

FUNDING_COLUMNS = ("calc_time", "funding_interval_hours", "last_funding_rate")
PREMIUM_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
)

#: Settlements sit on the 00:00/08:00/16:00 UTC grid. The feasibility study
#: measured at most 25 ms of jitter; 1000 ms is a generous acceptance band
#: that still catches a genuinely off-grid settlement.
GRID_JITTER_TOLERANCE_MS = 1000
EXPECTED_INTERVAL_HOURS = 8
BAR_MS = 15 * 60 * 1000


class NormalizationError(Exception):
    """A dump file is not what the predeclared design says it is."""


@dataclass(frozen=True)
class Normalized:
    symbol: str
    kind: str
    frame: pd.DataFrame
    sources: list[dict]


def _read_csv_member(path: Path, columns: tuple[str, ...]) -> pd.DataFrame:
    """One zip's single CSV member, header-tolerant.

    Older dumps ship headerless; newer ones carry a header row. The first
    field of a data row always parses as an integer epoch, so a first row
    that does not is a header.
    """
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise NormalizationError(f"{path.name}: expected exactly one CSV, found {names}")
        blob = archive.read(names[0])
    first_field = blob.split(b"\n", 1)[0].split(b",", 1)[0].strip()
    has_header = not first_field.isdigit()
    frame = pd.read_csv(
        io.BytesIO(blob),
        header=0 if has_header else None,
        names=None if has_header else list(columns),
    )
    if has_header:
        got = tuple(str(c).strip() for c in frame.columns)
        if got != columns:
            raise NormalizationError(f"{path.name}: schema drift, columns {got}")
    return frame


def _file_records(kind: str, symbol: str) -> list[Path]:
    directory = DATASET_DIR / kind / symbol
    if not directory.exists():
        return []
    return sorted(directory.glob("*.zip"))


def normalize_funding(symbol: str) -> Normalized:
    """Settled funding for one perp symbol, causally stamped and validated."""
    parts: list[pd.DataFrame] = []
    sources: list[dict] = []
    for path in _file_records("fundingRate", symbol):
        frame = _read_csv_member(path, FUNDING_COLUMNS)
        parts.append(frame)
        sources.append(
            {
                "file": path.name,
                "rows": int(len(frame)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    if not parts:
        raise NormalizationError(f"no funding files acquired for {symbol}")
    raw = pd.concat(parts, ignore_index=True)

    calc_ms = raw["calc_time"].astype("int64")
    intervals = raw["funding_interval_hours"].astype("int64")
    if not (intervals == EXPECTED_INTERVAL_HOURS).all():
        offenders = sorted(set(intervals[intervals != EXPECTED_INTERVAL_HOURS].tolist()))
        raise NormalizationError(
            f"{symbol}: funding_interval_hours values {offenders} != {EXPECTED_INTERVAL_HOURS}; "
            "the predeclared design assumes a uniform 8h settlement grid"
        )
    offset = calc_ms % (EXPECTED_INTERVAL_HOURS * 3_600_000)
    off_grid = np.minimum(offset, EXPECTED_INTERVAL_HOURS * 3_600_000 - offset)
    if int(off_grid.max()) > GRID_JITTER_TOLERANCE_MS:
        raise NormalizationError(
            f"{symbol}: settlement off the 8h UTC grid by {int(off_grid.max())} ms"
        )

    source_ts = pd.to_datetime(calc_ms, unit="ms", utc=True)
    frame = pd.DataFrame(
        {
            "source_timestamp": source_ts,
            # The venue settles and publishes the realised rate at the same
            # instant; the dump's own T+1 file lag is an artefact of the
            # archive, not of when the number became knowable to a live reader.
            "publication_timestamp": source_ts,
            "knowable_at": source_ts.dt.ceil("1s"),
            "funding_rate": raw["last_funding_rate"].astype("float64"),
            "funding_interval_hours": intervals,
        }
    )
    frame = frame.sort_values("source_timestamp", kind="stable").reset_index(drop=True)
    duplicates = int(frame["source_timestamp"].duplicated().sum())
    if duplicates:
        raise NormalizationError(f"{symbol}: {duplicates} duplicate funding settlements")
    if not frame["source_timestamp"].is_monotonic_increasing:
        raise NormalizationError(f"{symbol}: funding settlements are not monotonic")
    if (frame["knowable_at"] < frame["source_timestamp"]).any():
        raise NormalizationError(f"{symbol}: a funding knowable_at precedes its settlement")
    return Normalized(symbol=symbol, kind="funding", frame=frame, sources=sources)


def normalize_premium(symbol: str) -> Normalized:
    """Premium-index 15m bars for one perp symbol, causally stamped."""
    parts: list[pd.DataFrame] = []
    sources: list[dict] = []
    # Monthly archives first, then the daily files that close their known
    # holes. Overlapping bars are reconciled by the duplicate rule below:
    # identical repeats collapse, conflicting ones are a hard failure.
    premium_paths = _file_records("premiumIndexKlines15m", symbol) + _file_records(
        "premiumIndexKlines15m_daily", symbol
    )
    for path in premium_paths:
        frame = _read_csv_member(path, PREMIUM_COLUMNS)
        parts.append(frame)
        sources.append(
            {
                "file": path.name,
                "rows": int(len(frame)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    if not parts:
        raise NormalizationError(f"no premium files acquired for {symbol}")
    raw = pd.concat(parts, ignore_index=True)

    open_ms = raw["open_time"].astype("int64")
    close_ms = raw["close_time"].astype("int64")
    # Some archive months stamp close_time in microseconds; detect and fold.
    scale = np.where(close_ms - open_ms > BAR_MS * 10, 1000, 1)
    close_ms = (close_ms // scale).astype("int64")
    if int((open_ms % BAR_MS).max()) != 0:
        raise NormalizationError(f"{symbol}: premium open_time off the 15m grid")
    span = close_ms - open_ms
    if not ((span == BAR_MS - 1).all()):
        bad = sorted(set(span[span != BAR_MS - 1].tolist()))[:5]
        raise NormalizationError(f"{symbol}: premium bar spans {bad} != {BAR_MS - 1} ms")

    bar_open = pd.to_datetime(open_ms, unit="ms", utc=True)
    bar_close = pd.to_datetime(close_ms, unit="ms", utc=True)
    frame = pd.DataFrame(
        {
            "bar_open": bar_open,
            "bar_close": bar_close,
            "knowable_at": bar_close + pd.Timedelta(1, unit="ms"),
            "premium_open": raw["open"].astype("float64"),
            "premium_high": raw["high"].astype("float64"),
            "premium_low": raw["low"].astype("float64"),
            "premium_close": raw["close"].astype("float64"),
            "sample_count": raw["count"].astype("int64"),
        }
    )
    frame = frame.sort_values("bar_open", kind="stable").reset_index(drop=True)
    duplicates = int(frame["bar_open"].duplicated().sum())
    if duplicates:
        # Overlapping monthly archives can legitimately repeat a boundary bar;
        # identical repeats collapse, conflicting ones are a hard failure.
        deduped = frame.drop_duplicates(subset="bar_open", keep="first").reset_index(drop=True)
        conflict = frame.drop_duplicates(subset=list(frame.columns), keep="first")
        if len(conflict) != len(deduped):
            raise NormalizationError(f"{symbol}: conflicting duplicate premium bars")
        frame = deduped
    if not frame["bar_open"].is_monotonic_increasing:
        raise NormalizationError(f"{symbol}: premium bars are not monotonic")
    if (frame["knowable_at"] <= frame["bar_close"]).any():
        raise NormalizationError(f"{symbol}: a premium knowable_at does not follow its bar close")
    return Normalized(symbol=symbol, kind="premium", frame=frame, sources=sources)


def _write(normalized: Normalized) -> dict:
    NORMALIZED_DIR.mkdir(parents=True, exist_ok=True)
    slug = normalized.symbol
    path = NORMALIZED_DIR / f"{slug}_{normalized.kind}.parquet"
    tmp = path.with_suffix(".parquet.tmp")
    normalized.frame.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    time_column = "source_timestamp" if normalized.kind == "funding" else "bar_open"
    return {
        "symbol": normalized.symbol,
        "spot_symbol": SYMBOL_MAP[normalized.symbol],
        "kind": normalized.kind,
        "path": str(path),
        "rows": int(len(normalized.frame)),
        "first": str(normalized.frame[time_column].iloc[0]),
        "last": str(normalized.frame[time_column].iloc[-1]),
        "output_sha256": digest,
        "source_files": normalized.sources,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default="/Volumes/AUTOTRADER_QA/reports/crypto-funding-basis-pilot/dataset_manifest.json",
    )
    args = parser.parse_args()

    entries = []
    for symbol in SYMBOL_MAP:
        for builder in (normalize_funding, normalize_premium):
            normalized = builder(symbol)
            entry = _write(normalized)
            entries.append(entry)
            print(
                f"{entry['symbol']} {entry['kind']}: {entry['rows']} rows "
                f"{entry['first']} .. {entry['last']} sha256={entry['output_sha256'][:16]}…",
                flush=True,
            )

    manifest = Path(args.manifest)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "symbol_map": SYMBOL_MAP,
        "datasets": entries,
    }
    tmp = manifest.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, manifest)
    print(f"wrote {manifest}")


if __name__ == "__main__":
    main()
