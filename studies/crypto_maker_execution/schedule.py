"""Deterministic event schedules — fixed in the journal before any result.

SCHEDULE-U: within each calendar quarter from 2023-Q3 through 2026-Q3,
every 4th day starting with the quarter's 2nd day; the decision instant is
00:00:00 UTC of the selected day (the close of the previous UTC day's last
completed 15m bar — exactly the daily-stride decision instant that
survived the friction analysis). The pilot subset takes every 12th day
starting with the 2nd. No randomness anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

FIRST_QUARTER = (2023, 3)  # 2023-Q3
LAST_QUARTER = (2026, 3)  # 2026-Q3
#: The +24h reference series (digest-verified 15m parquet) ends 2026-08-28,
#: so the last event day must leave a full day of reference data.
LAST_EVENT_DAY = date(2026, 8, 27)

FULL_STRIDE_DAYS = 4
PILOT_STRIDE_DAYS = 12
START_OFFSET_DAYS = 1  # the quarter's 2nd day


@dataclass(frozen=True)
class Event:
    symbol: str
    decision_ts: datetime  # tz-aware UTC, 00:00:00 of the event day
    quarter: str  # e.g. "2024Q1"

    @property
    def event_day(self) -> date:
        return self.decision_ts.date()


def quarters() -> list[tuple[int, int]]:
    out = []
    year, quarter = FIRST_QUARTER
    while (year, quarter) <= LAST_QUARTER:
        out.append((year, quarter))
        quarter += 1
        if quarter == 5:
            year, quarter = year + 1, 1
    return out


def quarter_days(year: int, quarter: int) -> list[date]:
    start = date(year, 3 * (quarter - 1) + 1, 1)
    end = date(year + 1, 1, 1) if quarter == 4 else date(year, 3 * quarter + 1, 1)
    days = []
    day = start
    while day < end:
        days.append(day)
        day += timedelta(days=1)
    return days


def events_for(symbol: str, *, pilot: bool) -> list[Event]:
    stride = PILOT_STRIDE_DAYS if pilot else FULL_STRIDE_DAYS
    out: list[Event] = []
    for year, quarter in quarters():
        label = f"{year}Q{quarter}"
        days = quarter_days(year, quarter)
        for index in range(START_OFFSET_DAYS, len(days), stride):
            day = days[index]
            if day > LAST_EVENT_DAY:
                continue
            decision = datetime(day.year, day.month, day.day, tzinfo=UTC)
            out.append(Event(symbol=symbol, decision_ts=decision, quarter=label))
    return out


def pilot_is_subset_of_full(symbol: str) -> bool:
    """The pilot must never simulate an event the full run would not."""
    full = {event.decision_ts for event in events_for(symbol, pilot=False)}
    return all(event.decision_ts in full for event in events_for(symbol, pilot=True))
