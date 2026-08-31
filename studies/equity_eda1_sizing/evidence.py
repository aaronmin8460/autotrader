"""Load the stored decision evidence and rebuild EDA-1's stance series.

Nothing here recomputes a strategy. The ten-symbol study's stored **V3**
decision series is read from disk, and EDA-1 is derived from it by the
*production* overlay - `autotrader.equity.regime.participation_overlay`, the
module the live Shadow runs - rather than by a copy of the research transform.
That is deliberate: if the production overlay and the research overlay had
drifted apart, this study would be validating a sizing policy for a strategy
the runtime does not implement, and the wiring check below would fail rather
than quietly score the wrong thing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
from studies.equity_eda1_sizing import STUDY_SYMBOLS, WINDOW_NAMES

from autotrader.decision.contract import DecisionSignal
from autotrader.equity.regime import (
    EDA1_ARCHITECTURE,
    ParticipationSpec,
    SeriesRecord,
    participation_overlay,
    participation_series,
    session_closes,
    source_stance,
)
from autotrader.equity.session import market_date


class EvidenceError(Exception):
    """Stored evidence that cannot support the claims this study makes."""


def default_datasets() -> Path:
    return Path(
        os.environ.get("EQUITY_DATASETS", "/Volumes/AUTOTRADER_QA/datasets/equity-historical")
    )


def default_decisions() -> Path:
    return Path(
        os.environ.get(
            "EQUITY_DECISIONS",
            "/Volumes/AUTOTRADER_QA/reports/equity-10-symbol-full/decisions",
        )
    )


def load_session_frame(datasets: Path, symbol: str) -> pd.DataFrame:
    """One symbol's full regular-session frame, exactly as the research read it."""
    files = sorted(datasets.glob(f"{symbol}_15m_*session.parquet"))
    if len(files) != 1:
        raise EvidenceError(f"Expected exactly one session frame for {symbol}, found {files}.")
    return pd.read_parquet(files[0])


def load_stored_series(decisions: Path, symbol: str, engine: str) -> tuple[SeriesRecord, ...]:
    """One engine's stored decision series for `symbol`, all twelve windows.

    Concatenated in window order and returned as the production overlay's own
    record type, so the overlay consumes the stored evidence without a study
    adapter standing between them.
    """
    records: list[SeriesRecord] = []
    for window in WINDOW_NAMES:
        path = decisions / f"{symbol}_{window}_{engine}.parquet"
        if not path.exists():
            raise EvidenceError(f"Missing stored series {path}.")
        frame = pd.read_parquet(path)
        for row in frame.itertuples(index=False):
            records.append(
                SeriesRecord(
                    timestamp=pd.Timestamp(row.timestamp).to_pydatetime(),
                    symbol=str(row.symbol),
                    signal=DecisionSignal(str(row.signal)),
                    score=float(row.score),
                    confidence=float(row.confidence),
                    regime=str(row.regime),
                    reasons=tuple(str(row.reasons).split("|")) if row.reasons else (),
                )
            )
    records.sort(key=lambda record: record.timestamp)
    return tuple(records)


def participation_by_session(datasets: Path, spec: ParticipationSpec) -> dict:
    """The router's per-session answer over the whole SPY history.

    Built from the complete frame rather than the scored region, because a
    200-session moving average needs 200 completed sessions behind the first
    scored bar and the region does not contain them. This is the research
    program's own construction.
    """
    spy = load_session_frame(datasets, "SPY")
    closes = session_closes(spy)
    series = participation_series(closes, spec)
    return {row.session: bool(row.participate) for row in series.itertuples(index=False)}


def stance_frame(
    datasets: Path,
    decisions: Path,
    *,
    spec: ParticipationSpec | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    """EDA-1 and V3 stances per symbol per bar, plus a participation summary.

    Returns `(eda1, v3, summary)`. Both frames are indexed by timestamp with one
    boolean column per symbol: True where that engine holds the symbol LONG at
    that bar. A symbol with no bar at a timestamp is `NA` rather than False -
    "this market was not open" and "this engine was flat" are different facts
    and the simulator must not confuse them.
    """
    router = spec if spec is not None else ParticipationSpec()
    participate = participation_by_session(datasets, router)

    eda1_columns: dict[str, pd.Series] = {}
    v3_columns: dict[str, pd.Series] = {}
    participate_bars = 0
    region_bars = 0

    for symbol in STUDY_SYMBOLS:
        stored = load_stored_series(decisions, symbol, "V3")
        overlaid = participation_overlay(stored, participate, architecture=EDA1_ARCHITECTURE)
        stamps = pd.DatetimeIndex([record.timestamp for record in overlaid])
        eda1_columns[symbol] = pd.Series(
            [bool(value) for value in source_stance(overlaid)], index=stamps
        )
        v3_columns[symbol] = pd.Series(
            [bool(value) for value in source_stance(stored)], index=stamps
        )
        if symbol == "SPY":
            region_bars = len(stored)
            participate_bars = sum(
                1 for record in stored if participate[market_date(record.timestamp)]
            )

    eda1 = pd.DataFrame(eda1_columns).astype("boolean")
    v3 = pd.DataFrame(v3_columns).astype("boolean")
    summary = {
        "spec": router.to_json_dict(),
        "region_bars": region_bars,
        "participate_bars": participate_bars,
        "participate_fraction": participate_bars / region_bars if region_bars else 0.0,
    }
    return eda1.sort_index(), v3.sort_index(), summary


#: The research program's published participation figures for the primary spec.
#: Reproducing them is the wiring check: it proves the production overlay,
#: driven by the stored series, lands on the same regime series the validated
#: architecture was scored on.
PUBLISHED_REGION_BARS = 31890
PUBLISHED_PARTICIPATE_BARS = 18108


def verify_wiring(summary: dict[str, object]) -> None:
    """Refuse to score anything unless the rebuilt regime matches the research's."""
    region = int(summary["region_bars"])  # type: ignore[arg-type]
    participating = int(summary["participate_bars"])  # type: ignore[arg-type]
    if region != PUBLISHED_REGION_BARS or participating != PUBLISHED_PARTICIPATE_BARS:
        raise EvidenceError(
            "The rebuilt EDA-1 participation series does not reproduce the published "
            f"research figures: region_bars={region} (expected {PUBLISHED_REGION_BARS}), "
            f"participate_bars={participating} (expected {PUBLISHED_PARTICIPATE_BARS}). "
            "Refusing to score a sizing policy against a strategy series that is not "
            "the validated one."
        )


__all__ = [
    "PUBLISHED_PARTICIPATE_BARS",
    "PUBLISHED_REGION_BARS",
    "EvidenceError",
    "default_datasets",
    "default_decisions",
    "load_session_frame",
    "load_stored_series",
    "participation_by_session",
    "stance_frame",
    "verify_wiring",
]
