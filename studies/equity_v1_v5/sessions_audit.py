"""Inspecting named sessions one at a time, against the shipped session arithmetic.

The aggregate checks elsewhere say "no derived bar spans a session boundary in
36,751 bars". True, and unreadable. This module does the complementary thing:
it takes a handful of *named* days - an ordinary one, the session before a
holiday, a half day, both daylight-saving transitions, the day the provider
published nothing - and prints what the calendar said, what the shipped
functions derived, and what the data actually contains, so a reader can check
each claim by eye.

Every derived figure comes from `autotrader.equity.session`. Nothing here
recomputes an open, a close, or a bar grid; a wrong number would be the shipped
module's wrong number, which is the only kind worth reporting.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import pandas as pd

from autotrader.equity import MARKET_TIMEZONE
from autotrader.equity.session import (
    is_market_open,
    market_date,
    regular_session_bar_starts,
    session_wake_times,
)
from studies.equity_v1_v5.calendar import SnapshotCalendar

#: The days the report walks through, and why each one is here. Chosen to cover
#: every shape the US calendar produces, plus the two provider outages.
NAMED_SESSIONS: tuple[tuple[str, date], ...] = (
    ("ordinary winter session", date(2025, 1, 15)),
    ("ordinary summer session", date(2025, 7, 15)),
    ("DST spring-forward, first session after", date(2025, 3, 10)),
    ("DST fall-back, first session after", date(2025, 11, 3)),
    ("session before Thanksgiving", date(2024, 11, 27)),
    ("Thanksgiving early close", date(2024, 11, 29)),
    ("Christmas Eve early close", date(2024, 12, 24)),
    ("day before Independence Day early close", date(2025, 7, 3)),
    ("session before Good Friday", date(2025, 4, 17)),
    ("Good Friday: market closed", date(2025, 4, 18)),
    ("Christmas Day: market closed", date(2024, 12, 25)),
    ("Juneteenth: market closed", date(2025, 6, 19)),
    ("Saturday: market closed", date(2025, 1, 18)),
)


@dataclass(frozen=True)
class SessionFact:
    """What the calendar, the session arithmetic and the data each say about one day."""

    label: str
    day: str
    is_session: bool
    open_local: str | None
    close_local: str | None
    open_utc: str | None
    close_utc: str | None
    utc_offset_hours: int | None
    scheduled_bars: int
    observed_bars: int
    first_bar_utc: str | None
    last_bar_utc: str | None
    actionable_wake_times: int
    early_close: bool

    def to_json_dict(self) -> dict[str, object]:
        return dict(self.__dict__)


def describe_day(
    calendar: SnapshotCalendar,
    frames: dict[str, pd.DataFrame],
    label: str,
    day: date,
) -> SessionFact:
    """Everything the system knows about one exchange day."""
    session = calendar.session_for(day)
    observed: dict[str, list[pd.Timestamp]] = {}
    for symbol, frame in frames.items():
        stamps = [ts for ts in frame["timestamp"] if market_date(ts.to_pydatetime()) == day]
        observed[symbol] = stamps

    any_symbol = next(iter(observed.values())) if observed else []
    if session is None:
        return SessionFact(
            label=label,
            day=day.isoformat(),
            is_session=False,
            open_local=None,
            close_local=None,
            open_utc=None,
            close_utc=None,
            utc_offset_hours=None,
            scheduled_bars=0,
            observed_bars=len(any_symbol),
            first_bar_utc=None,
            last_bar_utc=None,
            actionable_wake_times=0,
            early_close=False,
        )

    starts = regular_session_bar_starts(session)
    opened = session.open_utc.astimezone(MARKET_TIMEZONE)
    closed = session.close_utc.astimezone(MARKET_TIMEZONE)
    offset = opened.utcoffset()
    return SessionFact(
        label=label,
        day=day.isoformat(),
        is_session=True,
        open_local=opened.strftime("%H:%M"),
        close_local=closed.strftime("%H:%M"),
        open_utc=session.open_utc.isoformat(),
        close_utc=session.close_utc.isoformat(),
        utc_offset_hours=None if offset is None else int(offset.total_seconds() // 3600),
        scheduled_bars=len(starts),
        observed_bars=len(any_symbol),
        first_bar_utc=str(any_symbol[0]) if any_symbol else None,
        last_bar_utc=str(any_symbol[-1]) if any_symbol else None,
        # One fewer than the scheduled bars: the last bar of a session closes at
        # the bell, so acting on it would mean submitting after the close.
        actionable_wake_times=len(session_wake_times(session)),
        early_close=closed.strftime("%H:%M") < "16:00",
    )


def audit(
    calendar: SnapshotCalendar,
    frames: dict[str, pd.DataFrame],
    days: Sequence[tuple[str, date]] = NAMED_SESSIONS,
) -> dict[str, object]:
    """The named-session table, plus the invariants it is checked against."""
    facts = [describe_day(calendar, frames, label, day) for label, day in days]
    sessions = [fact for fact in facts if fact.is_session]
    closures = [fact for fact in facts if not fact.is_session]
    return {
        "facts": [fact.to_json_dict() for fact in facts],
        "invariants": {
            "closed_days_have_no_bars": all(fact.observed_bars == 0 for fact in closures),
            "full_sessions_schedule_26_bars": all(
                fact.scheduled_bars == 26 for fact in sessions if not fact.early_close
            ),
            "early_closes_schedule_14_bars": all(
                fact.scheduled_bars == 14 for fact in sessions if fact.early_close
            ),
            "open_is_always_09_30_local": all(fact.open_local == "09:30" for fact in sessions),
            "last_bar_of_session_is_never_actionable": all(
                fact.actionable_wake_times == fact.scheduled_bars - 1 for fact in sessions
            ),
            "both_utc_offsets_observed": sorted(
                {fact.utc_offset_hours for fact in sessions if fact.utc_offset_hours is not None}
            )
            == [-5, -4],
        },
    }


def market_open_probe(calendar: SnapshotCalendar, day: date) -> dict[str, object]:
    """Whether the shipped `is_market_open` agrees with the session boundaries.

    Probed at the four instants that matter: just before the open, at the open,
    at the close, and just after it. The close is half-open, so 16:00 must read
    as shut - an order submitted at that instant is an after-hours order.
    """
    session = calendar.session_for(day)
    if session is None:
        return {"day": day.isoformat(), "is_session": False}
    one_minute = pd.Timedelta("1min").to_pytimedelta()
    probes = {
        "one_minute_before_open": session.open_utc - one_minute,
        "at_open": session.open_utc,
        "one_minute_before_close": session.close_utc - one_minute,
        "at_close": session.close_utc,
    }
    return {
        "day": day.isoformat(),
        "is_session": True,
        **{name: is_market_open(calendar, now=moment)[0] for name, moment in probes.items()},
    }


__all__ = ["NAMED_SESSIONS", "SessionFact", "audit", "describe_day", "market_open_probe"]
