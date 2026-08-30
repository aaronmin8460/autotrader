"""The common scoring window, and the calendar events it was chosen to contain.

A pilot cannot afford to score five and a half years five times over, and it does
not need to: its question is whether the pipeline is correct, and correctness is
demonstrated by the awkward days, not by the ordinary ones. So the scored
interval is a set of contiguous chronological windows chosen so that every
session shape the US calendar produces appears in at least one of them - and the
choice is written down here, with the event each window exists to cover, rather
than being a range someone picked.

**Every engine scores exactly these bars.** The window list is the *common*
scoring window: V1 through V5 are handed the identical bars, so no engine gets
an easier interval than another. The warm-up that precedes each window is
history the engines read and nobody scores.

**The windows are contiguous in market time, not in calendar time.** A window is
named by two exchange dates and resolves to the regular-session bars between
them. Weekends, holidays and the sessions the provider never published are
simply not in it, because they are not bars.

**Coverage is the selection criterion and it is checkable.** `coverage_report`
re-derives, from the snapshotted calendar, which DST transitions, early closes,
holidays and data outages actually fall inside the chosen windows. If a window
is edited and stops covering what it claims to, that function says so instead of
this docstring quietly becoming false.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import pandas as pd

from autotrader.equity.session import market_date, regular_session_bar_starts
from studies.equity_v1_v5.calendar import SnapshotCalendar

#: The base-bar lookback handed to every engine on every scored bar.
#:
#: Not `EQUITY_POLICY.required_base_bars(("15m", "1h", "4h"))`, which is 2834.
#: That figure assumes every session is a full one and every scheduled bar was
#: published; measured against the real SPY and QQQ frames the true worst case
#: is 2885, because an early close yields no 4-hour bar at all and a single
#: missing 15-minute bar destroys the one 4-hour bucket its session had. 3000
#: clears the measured worst case with margin, and the study asserts that no
#: scored bar came back short of history rather than trusting either number.
LOOKBACK_BARS = 3000

#: Bars between the last training row and the first scored bar, for V4.
#:
#: The label horizon, so no training outcome resolves inside the scored window,
#: plus one whole regular session on top of it. A session rather than a fixed
#: bar count because the thing being kept apart is market time: twenty-six bars
#: is one equity trading day, and an embargo shorter than a day would let a
#: model be fitted on the morning of a day it is about to be graded on.
EMBARGO_BARS = 26


@dataclass(frozen=True)
class ScoringWindow:
    """One contiguous stretch of exchange days, and why it is in the study."""

    name: str
    start: date
    end: date
    covers: str

    def bars(self, frame: pd.DataFrame) -> pd.DataFrame:
        """The frame's regular-session bars falling inside this window."""
        days = pd.Index([market_date(ts.to_pydatetime()) for ts in frame["timestamp"]])
        mask = (days >= self.start) & (days <= self.end)
        return frame.loc[mask].reset_index(drop=True)

    def positions(self, frame: pd.DataFrame) -> tuple[int, int]:
        """The ``[first, last]`` row positions of this window in `frame`.

        Positions rather than timestamps because the warm-up a live adapter
        needs is counted in rows of this very frame: the bar 3000 rows back is
        the history the engine would have held, whatever dates it spans.
        """
        days = pd.Index([market_date(ts.to_pydatetime()) for ts in frame["timestamp"]])
        inside = [index for index, day in enumerate(days) if self.start <= day <= self.end]
        if not inside:
            raise ValueError(f"Window {self.name} contains no bars of this frame.")
        return inside[0], inside[-1]

    def to_json_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "covers": self.covers,
        }


#: The pilot's common scoring window, oldest first.
#:
#: Six stretches of roughly two months each. Between them they contain both
#: daylight-saving transitions in each direction, every kind of early close the
#: US calendar produces (Thanksgiving, Christmas Eve, the day before
#: Independence Day), the long Christmas-New Year holiday run, a Good Friday,
#: Juneteenth, and both of the provider outages the dataset audit found. The
#: first window opens far enough into the frame for a 3000-bar warm-up to exist.
SCORING_WINDOWS: tuple[ScoringWindow, ...] = (
    ScoringWindow(
        name="2021-autumn",
        start=date(2021, 10, 15),
        end=date(2021, 12, 15),
        covers="DST fall-back 2021-11-07; Thanksgiving holiday and the 2021-11-26 early close",
    ),
    ScoringWindow(
        name="2022-spring",
        start=date(2022, 2, 15),
        end=date(2022, 4, 18),
        covers="DST spring-forward 2022-03-13; Good Friday 2022-04-15; a bear-market regime",
    ),
    ScoringWindow(
        name="2023-summer",
        start=date(2023, 6, 1),
        end=date(2023, 7, 31),
        covers="Juneteenth 2023-06-19; the 2023-07-03 early close and Independence Day",
    ),
    ScoringWindow(
        name="2024-yearend",
        start=date(2024, 11, 15),
        end=date(2025, 1, 15),
        covers=(
            "the 2024-11-29 and 2024-12-24 early closes; Christmas and New Year; "
            "the 2024-12-23 partial provider outage"
        ),
    ),
    ScoringWindow(
        name="2025-spring",
        start=date(2025, 2, 15),
        end=date(2025, 4, 21),
        covers=(
            "DST spring-forward 2025-03-09; the 2025-03-10 session the provider "
            "published nothing for; Good Friday 2025-04-18"
        ),
    ),
    ScoringWindow(
        name="2026-summer",
        start=date(2026, 6, 1),
        end=date(2026, 8, 28),
        covers="the 2026-07-03 early close; the most recent data the feed serves",
    ),
)


def coverage_report(
    calendar: SnapshotCalendar,
    frame: pd.DataFrame,
    windows: Sequence[ScoringWindow] = SCORING_WINDOWS,
) -> dict[str, object]:
    """Re-derive what the chosen windows actually contain, from the calendar itself.

    The check that this module's claims are true. Every figure here is counted
    from the snapshot and from the observed bars, so a window that stopped
    covering an early close would show a zero rather than leave a stale sentence
    in a docstring.
    """
    observed = {market_date(ts.to_pydatetime()) for ts in frame["timestamp"]}
    entries: list[dict[str, object]] = []
    for window in windows:
        sessions = calendar.sessions_between(window.start, window.end)
        records = [
            record
            for record in calendar.records
            if window.start <= date.fromisoformat(record.session_date) <= window.end
        ]
        early = [r.session_date for r in records if r.is_early_close]
        offsets = sorted({r.utc_offset_hours for r in records})
        scheduled_bars = sum(len(regular_session_bar_starts(s)) for s in sessions)
        missing_sessions = [
            r.session_date for r in records if date.fromisoformat(r.session_date) not in observed
        ]
        entries.append(
            {
                "name": window.name,
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
                "covers": window.covers,
                "sessions": len(sessions),
                "scheduled_bars": scheduled_bars,
                "observed_bars": len(window.bars(frame)),
                "early_closes": early,
                "utc_offsets_seen": offsets,
                "dst_transition": len(offsets) > 1,
                "sessions_with_no_data": missing_sessions,
            }
        )
    return {
        "lookback_bars": LOOKBACK_BARS,
        "embargo_bars": EMBARGO_BARS,
        "window_count": len(entries),
        "total_scored_bars": sum(int(entry["observed_bars"]) for entry in entries),
        "windows": entries,
    }


__all__ = [
    "EMBARGO_BARS",
    "LOOKBACK_BARS",
    "SCORING_WINDOWS",
    "ScoringWindow",
    "coverage_report",
]
