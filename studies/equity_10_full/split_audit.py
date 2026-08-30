"""Prove the frames are split-adjusted by measuring what a split would leave behind.

The pilot found the one defect that would have invalidated this study: raw bars
turn NVDA's ten-for-one split into a fabricated -89.91% overnight step. Four
universe symbols split inside the data window, so this audit is not a formality.

For every symbol the audit computes every session-boundary close-to-open step
(the last regular-session bar of one session against the first of the next) and
reports:

- every step whose magnitude exceeds ``LARGE_STEP_THRESHOLD``, so real market
  events (earnings gaps, the 2024-08-05 unwind) are listed and named rather
  than hidden behind an assertion that nothing moved;
- for each known split, the measured step across the split date, which must be
  a market-sized move and not the ``-(1 - 1/ratio)`` crater a raw frame shows.

A single-name equity legitimately gaps more than an index ETF, so a fixed
±15% screen cannot be a pass/fail rule here the way it was for SPY/QQQ. The
rule that can be: **no overnight step may reproduce a known split's arithmetic
signature**, and every large step must land on a date the audit lists for a
human to check against the market record.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from studies.equity_10_full import DATA_END, DATA_START, KNOWN_SPLITS
from studies.equity_v1_v5.dataset import evaluation_path

#: Session-boundary steps at or beyond this magnitude are listed individually.
LARGE_STEP_THRESHOLD = 0.15

#: How close a measured step must come to a split's arithmetic signature to be
#: flagged as an unadjusted split. Half the distance between the split crater
#: and zero is far more than any real overnight move gets to a 3:1 crater.
SPLIT_SIGNATURE_TOLERANCE = 0.5


class SplitAuditError(Exception):
    """A frame shows the arithmetic signature of an unadjusted split."""


def session_boundary_steps(frame: pd.DataFrame) -> pd.DataFrame:
    """Every close->open step across a session boundary, as a small frame."""
    eastern = frame["timestamp"].dt.tz_convert("America/New_York")
    days = eastern.dt.date
    closes = frame["close"].astype("float64")
    opens = frame["open"].astype("float64")

    boundaries: list[dict[str, object]] = []
    previous_day: date | None = None
    previous_close: float | None = None
    previous_ts = None
    for day, close, open_, ts in zip(days, closes, opens, frame["timestamp"], strict=True):
        if previous_day is not None and day != previous_day:
            boundaries.append(
                {
                    "from_session": previous_day.isoformat(),
                    "to_session": day.isoformat(),
                    "prior_close": previous_close,
                    "next_open": float(open_),
                    "step": float(open_) / float(previous_close) - 1.0,
                    "boundary_ts": ts.isoformat(),
                }
            )
        previous_day = day
        previous_close = float(close)
        previous_ts = ts
    del previous_ts
    return pd.DataFrame(boundaries)


def audit_symbol(frame: pd.DataFrame, symbol: str) -> dict[str, object]:
    """The split-step audit for one symbol's session frame."""
    steps = session_boundary_steps(frame)
    large = steps.loc[steps["step"].abs() >= LARGE_STEP_THRESHOLD]

    split_checks: list[dict[str, object]] = []
    for split_date, ratio in KNOWN_SPLITS.get(symbol, ()):
        crater = -(1.0 - 1.0 / ratio)
        into = steps.loc[steps["to_session"] == split_date]
        measured = float(into["step"].iloc[0]) if len(into) else None
        looks_unadjusted = measured is not None and (
            abs(measured - crater) < abs(crater) * SPLIT_SIGNATURE_TOLERANCE
        )
        split_checks.append(
            {
                "split_date": split_date,
                "ratio": ratio,
                "raw_signature_step": crater,
                "measured_step": measured,
                "looks_unadjusted": looks_unadjusted,
            }
        )
        if looks_unadjusted:
            raise SplitAuditError(
                f"{symbol}: the overnight step into {split_date} is {measured:+.4f}, which "
                f"matches the {ratio}:1 split's raw signature of {crater:+.4f}. This frame "
                "is not split-adjusted and must not be scored."
            )

    return {
        "symbol": symbol,
        "session_boundaries": int(len(steps)),
        "mean_abs_step": float(steps["step"].abs().mean()) if len(steps) else 0.0,
        "max_abs_step": float(steps["step"].abs().max()) if len(steps) else 0.0,
        "large_steps": large.to_dict(orient="records"),
        "known_split_checks": split_checks,
    }


def audit_overnight_steps(datasets: Path, symbols: list[str]) -> dict[str, object]:
    """The audit across every supplied symbol, reading the stored session frames."""
    entries = []
    for symbol in symbols:
        frame = pd.read_parquet(evaluation_path(datasets, symbol, DATA_START, DATA_END))
        entries.append(audit_symbol(frame, symbol))
    return {
        "large_step_threshold": LARGE_STEP_THRESHOLD,
        "split_signature_tolerance": SPLIT_SIGNATURE_TOLERANCE,
        "symbols": entries,
    }


__all__ = [
    "LARGE_STEP_THRESHOLD",
    "SPLIT_SIGNATURE_TOLERANCE",
    "SplitAuditError",
    "audit_overnight_steps",
    "audit_symbol",
    "session_boundary_steps",
]
