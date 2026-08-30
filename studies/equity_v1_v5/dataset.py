"""Building the pilot dataset: download, session-filter, validate, fingerprint.

The evaluation frame is the provider's 15-minute stock bars, fetched through
`autotrader.equity.data` - the project's only stock market-data path - and then
reduced to regular-session bars by the shipped session arithmetic. Nothing here
reimplements either half.

**The session filter is not optional and it is not cosmetic.** The provider's
IEX feed serves pre-market and post-market candles in the same response as
regular-session ones. Left in, they do more than add rows: the higher-timeframe
aggregator buckets on the UTC clock and keeps a bucket only when it is *full*,
so a handful of pre-market bars can complete a bucket that would otherwise have
been discarded - manufacturing a 1-hour candle that straddles the opening bell
and a 4-hour candle that straddles the overnight gap. Filtering first is what
makes the aggregator's "a partly-observed bucket is dropped" rule mean "no
candle spans a session boundary". `session_bar_mask` does the filtering, per
bar, against that day's own session, which is why an early close comes out right
rather than merely close.

**No bar is ever manufactured.** A session the provider did not publish stays
absent, and the gap is reported rather than filled. The expected-bar count is
computed from the broker's calendar - `regular_session_bar_starts` on each real
session - so "missing" means missing against the exchange's own schedule and not
against a 26-bar-a-day assumption that an early close would break.

**Bars are raw, and that is a recorded fact rather than a preference.** The
shipped request sets no `adjustment`, so the provider serves unadjusted prices.
For SPY and QQQ over this window that is checked to be harmless - neither split -
and the dividend steps that remain are measured and reported. It is *not*
harmless for the ten-symbol universe; see `docs` in the pilot report.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

from autotrader.data.validation import EQUITY_UNIVERSE_LABEL, validate_frame
from autotrader.equity import EQUITY_SYMBOLS, EQUITY_TIMEFRAME, MARKET_TIMEZONE_NAME
from autotrader.equity.data import FEED, output_stem
from autotrader.equity.session import (
    MarketSession,
    market_date,
    regular_session_bar_starts,
    session_bar_mask,
)
from studies.equity_v1_v5.calendar import SnapshotCalendar

#: The provider's sentinel for "no trade printed on this venue in this bar", in
#: a column the canonical schema already declares nullable. Re-encoded, never
#: repaired: a zero volume-weighted average price is not a price of zero.
UNDEFINED_VWAP_SENTINEL = 0.0

#: The base bar interval. Every count in this module is against this grid.
BAR_INTERVAL = pd.Timedelta("15min")

#: How many calendar days one download request covers. The provider paginates
#: internally, but a multi-year single request is one long-lived HTTP call with
#: nothing to show for itself if it fails; a year at a time is resumable and
#: leaves a readable progress trail.
CHUNK_DAYS = 365


class DatasetError(Exception):
    """The pilot dataset cannot be built from what was supplied."""


@dataclass(frozen=True)
class GapReport:
    """Regular-session bars the exchange scheduled and the provider did not publish."""

    expected_bars: int
    observed_bars: int
    missing_bars: int
    gap_events: int
    sessions_expected: int
    sessions_observed: int
    largest_gaps: tuple[dict[str, object], ...] = field(default_factory=tuple)

    def to_json_dict(self) -> dict[str, object]:
        return {
            "expected_bars": self.expected_bars,
            "observed_bars": self.observed_bars,
            "missing_bars": self.missing_bars,
            "missing_fraction": (
                self.missing_bars / self.expected_bars if self.expected_bars else 0.0
            ),
            "gap_events": self.gap_events,
            "sessions_expected": self.sessions_expected,
            "sessions_observed": self.sessions_observed,
            "largest_gaps": list(self.largest_gaps),
        }


@dataclass(frozen=True)
class DatasetProvenance:
    """Everything needed to reproduce or challenge one symbol's evaluation frame."""

    symbol: str
    provider: str
    feed: str
    asset_class: str
    adjustment: str
    timeframe: str
    session_policy: str
    date_timezone: str
    timestamp_timezone: str
    requested_start: str
    requested_end: str
    first_bar_utc: str
    last_bar_utc: str
    raw_rows: int
    regular_session_rows: int
    extended_hours_rows_dropped: int
    duplicate_rows_dropped: int
    renulled_vwap_rows: int
    gaps: GapReport
    validation_ok: bool
    validation_issues: tuple[str, ...]
    raw_sha256: str
    frame_sha256: str
    retrieved_at_utc: str

    def to_json_dict(self) -> dict[str, object]:
        payload = {key: value for key, value in self.__dict__.items() if key not in {"gaps"}}
        payload["validation_issues"] = list(self.validation_issues)
        payload["gaps"] = self.gaps.to_json_dict()
        return payload


# --------------------------------------------------------------------------
# Fingerprints
# --------------------------------------------------------------------------


def frame_digest(frame: pd.DataFrame) -> str:
    """A content fingerprint of the evaluation frame.

    Over the canonical CSV rendering rather than the Parquet bytes, because
    Parquet embeds a writer version and compression choices that change the file
    without changing a single price. Two runs that scored the same numbers must
    produce the same digest even on a different pyarrow.
    """
    payload = frame.to_csv(index=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: Path) -> str:
    """The stored file's own digest, for the raw download."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------


def _chunks(start: date, end: date, *, days: int = CHUNK_DAYS) -> list[tuple[date, date]]:
    """Split an inclusive date range into consecutive inclusive sub-ranges."""
    spans: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=days - 1), end)
        spans.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return spans


def download_raw(
    symbols: Sequence[str],
    start: date,
    end: date,
    *,
    client: object | None = None,
    progress: object | None = None,
) -> dict[str, pd.DataFrame]:
    """Fetch raw bars for every symbol over the range, in resumable chunks.

    Returns the provider's rows unmodified apart from the canonical schema the
    market-data boundary already applies - extended-hours candles included, so
    the count of what was dropped later is a measured number.
    """
    from autotrader.equity.data import create_client, fetch_bars_for_symbols, to_request_window

    data_client = create_client() if client is None else client
    collected: dict[str, list[pd.DataFrame]] = {symbol: [] for symbol in symbols}
    for chunk_start, chunk_end in _chunks(start, end):
        window_start, window_end = to_request_window(chunk_start, chunk_end)
        frames = fetch_bars_for_symbols(data_client, symbols, window_start, window_end)
        for symbol, frame in frames.items():
            if not frame.empty:
                collected[symbol].append(frame)
        if progress is not None:
            progress(chunk_start, chunk_end, {s: len(f) for s, f in frames.items()})

    result: dict[str, pd.DataFrame] = {}
    for symbol, parts in collected.items():
        if not parts:
            raise DatasetError(
                f"The provider returned no {EQUITY_TIMEFRAME} bars for {symbol} between "
                f"{start.isoformat()} and {end.isoformat()} on the {FEED.value} feed."
            )
        merged = pd.concat(parts, ignore_index=True)
        result[symbol] = merged.sort_values("timestamp", kind="stable", ignore_index=True)
    return result


def raw_path(directory: Path, symbol: str, start: date, end: date) -> Path:
    """Where one symbol's unmodified download is stored."""
    stem = output_stem(symbol, EQUITY_TIMEFRAME, start, end)
    return Path(directory) / f"{stem}.raw.parquet"


# --------------------------------------------------------------------------
# Session reduction
# --------------------------------------------------------------------------


def drop_duplicate_bars(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Remove repeated timestamps, keeping the first. Chunk seams can overlap.

    Reported rather than silent: a duplicate inside a single chunk would mean
    the provider published one, which is a data-quality finding, and a duplicate
    at a seam is this module's own doing. The count lets the report say which.
    """
    duplicated = frame["timestamp"].duplicated(keep="first")
    removed = int(duplicated.sum())
    if not removed:
        return frame, 0
    return frame.loc[~duplicated].reset_index(drop=True), removed


def renull_undefined_vwap(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Turn the provider's ``vwap == 0`` sentinel into the null the schema reserves.

    Applied only where the bar also reports no volume and no trades, so a real
    zero-priced print - which cannot happen for an equity - would survive to be
    caught by the validator instead of being quietly erased.
    """
    if "vwap" not in frame.columns:
        return frame, 0
    undefined = (
        (frame["vwap"] == UNDEFINED_VWAP_SENTINEL)
        & (frame["volume"].fillna(0) == 0)
        & (frame.get("trade_count", pd.Series(0, index=frame.index)).fillna(0) == 0)
    )
    count = int(undefined.sum())
    if not count:
        return frame, 0
    corrected = frame.copy()
    corrected.loc[undefined, "vwap"] = pd.NA
    return corrected, count


def filter_regular_session(
    frame: pd.DataFrame,
    calendar: SnapshotCalendar,
) -> tuple[pd.DataFrame, int]:
    """Keep only regular-session bars, judged by each bar's own session.

    Delegates to the shipped `session_bar_mask`, so the rule the runtime filters
    live bars with is the rule the study filters historical ones with. The
    sessions it is checked against are the real ones spanning the frame, which
    is what makes an early-close day drop its 13:00-16:00 bars rather than keep
    them because most days have them.
    """
    if frame.empty:
        return frame, 0
    first = market_date(frame["timestamp"].iloc[0].to_pydatetime())
    last = market_date(frame["timestamp"].iloc[-1].to_pydatetime())
    sessions = calendar.sessions_between(first, last)
    if not sessions:
        raise DatasetError(
            f"The calendar snapshot reports no session between {first} and {last}, "
            "so no bar in this frame can be shown to be a regular-session bar."
        )
    mask = session_bar_mask(sessions, [ts.to_pydatetime() for ts in frame["timestamp"]])
    kept = frame.loc[mask].reset_index(drop=True)
    return kept, len(frame) - len(kept)


def expected_bar_starts(
    sessions: Sequence[MarketSession],
) -> pd.DatetimeIndex:
    """Every regular-session bar the exchange scheduled across `sessions`.

    The denominator for "missing bars". Built from each session's own open and
    close, so a half day contributes fourteen and a full day twenty-six, and a
    holiday contributes nothing because it is not a session at all.
    """
    starts: list[datetime] = []
    for session in sessions:
        starts.extend(regular_session_bar_starts(session))
    return pd.DatetimeIndex(sorted(starts), tz="UTC")


def describe_gaps(
    frame: pd.DataFrame,
    calendar: SnapshotCalendar,
    *,
    top: int = 10,
) -> GapReport:
    """Compare what the exchange scheduled with what the provider published."""
    if frame.empty:
        return GapReport(0, 0, 0, 0, 0, 0, ())
    first = market_date(frame["timestamp"].iloc[0].to_pydatetime())
    last = market_date(frame["timestamp"].iloc[-1].to_pydatetime())
    sessions = calendar.sessions_between(first, last)
    expected = expected_bar_starts(sessions)
    observed = pd.DatetimeIndex(frame["timestamp"])
    missing = expected.difference(observed)

    # Consecutive missing bars are one event, not many: a session the provider
    # skipped entirely should read as one hole rather than twenty-six.
    events: list[dict[str, object]] = []
    if len(missing):
        run_start = missing[0]
        previous = missing[0]
        for moment in missing[1:]:
            if moment - previous > BAR_INTERVAL:
                events.append({"start": run_start, "end": previous})
                run_start = moment
            previous = moment
        events.append({"start": run_start, "end": previous})

    for event in events:
        span = (event["end"] - event["start"]) / BAR_INTERVAL + 1
        event["bars"] = int(span)
        event["start"] = event["start"].isoformat()
        event["end"] = event["end"].isoformat()

    events.sort(key=lambda item: item["bars"], reverse=True)
    observed_sessions = {ts.tz_convert("America/New_York").date() for ts in observed}
    return GapReport(
        expected_bars=len(expected),
        observed_bars=len(observed),
        missing_bars=len(missing),
        gap_events=len(events),
        sessions_expected=len(sessions),
        sessions_observed=len(observed_sessions),
        largest_gaps=tuple(events[:top]),
    )


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def build_evaluation_frame(
    raw: pd.DataFrame,
    calendar: SnapshotCalendar,
    *,
    symbol: str,
    start: date,
    end: date,
    raw_digest: str,
    retrieved_at: datetime,
) -> tuple[pd.DataFrame, DatasetProvenance]:
    """Reduce one symbol's raw download to its evaluation frame, with provenance."""
    deduped, duplicates = drop_duplicate_bars(raw)
    regular, dropped = filter_regular_session(deduped, calendar)
    if regular.empty:
        raise DatasetError(f"{symbol} has no regular-session bars in {start}..{end}.")
    frame, renulled = renull_undefined_vwap(regular)
    gaps = describe_gaps(frame, calendar)
    validation = validate_frame(
        frame, supported_symbols=EQUITY_SYMBOLS, universe_label=EQUITY_UNIVERSE_LABEL
    )
    provenance = DatasetProvenance(
        symbol=symbol,
        provider="alpaca",
        feed=FEED.value,
        asset_class="us_equity",
        # Recorded because it is a decision, not a default nobody made: the
        # shipped request sets no adjustment, so the provider serves raw.
        adjustment="raw",
        timeframe=EQUITY_TIMEFRAME,
        session_policy="regular-session-only (09:30-16:00 America/New_York, broker calendar)",
        date_timezone=MARKET_TIMEZONE_NAME,
        timestamp_timezone="UTC",
        requested_start=start.isoformat(),
        requested_end=end.isoformat(),
        first_bar_utc=frame["timestamp"].iloc[0].isoformat(),
        last_bar_utc=frame["timestamp"].iloc[-1].isoformat(),
        raw_rows=len(raw),
        regular_session_rows=len(frame),
        extended_hours_rows_dropped=dropped,
        duplicate_rows_dropped=duplicates,
        renulled_vwap_rows=renulled,
        gaps=gaps,
        validation_ok=validation.valid,
        validation_issues=tuple(f"{issue.code}: {issue.message}" for issue in validation.errors),
        raw_sha256=raw_digest,
        frame_sha256=frame_digest(frame),
        retrieved_at_utc=retrieved_at.astimezone(UTC).isoformat(),
    )
    return frame, provenance


def evaluation_path(directory: Path, symbol: str, start: date, end: date) -> Path:
    """Where one symbol's regular-session evaluation frame is stored."""
    stem = output_stem(symbol, EQUITY_TIMEFRAME, start, end)
    return Path(directory) / f"{stem}.session.parquet"


def write_provenance(provenance: DatasetProvenance, path: Path) -> None:
    """Persist one symbol's provenance sidecar."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(provenance.to_json_dict(), indent=2, sort_keys=True) + "\n"
    path.write_text(payload, encoding="utf-8")


__all__ = [
    "BAR_INTERVAL",
    "CHUNK_DAYS",
    "UNDEFINED_VWAP_SENTINEL",
    "DatasetError",
    "DatasetProvenance",
    "GapReport",
    "build_evaluation_frame",
    "describe_gaps",
    "download_raw",
    "drop_duplicate_bars",
    "evaluation_path",
    "expected_bar_starts",
    "file_sha256",
    "filter_regular_session",
    "frame_digest",
    "raw_path",
    "renull_undefined_vwap",
    "write_provenance",
]
