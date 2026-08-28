"""Equity V0.2: the market-session abstraction, and the completed-bar rule.

The smallest thing that can answer the four questions an equity runtime has to
ask before it may do anything:

    is the regular US market session open right now?
    which 15-minute bars belong to that session?
    which of them is the newest one that has fully closed?
    when should this process wake up next?

**The broker's calendar is the authority, not a hardcoded week.** `Mon-Fri
09:30-16:00` is wrong on roughly a dozen days a year: nine or ten full-day
holidays, and a handful of 13:00 early closes around Thanksgiving, Christmas
Eve, and Independence Day. Alpaca publishes the real thing - a list of session
days, each with its own open and close, holidays simply absent - so this module
consumes that list and hardcodes no date, no weekday rule, and no holiday.

**Timezone semantics are explicit in both directions.** Alpaca's calendar
endpoint reports each session's open and close as *naive* wall-clock times on
the session's own date, and they are Eastern. `session_from_local` is the one
place that attaches `MARKET_TIMEZONE` to them, and it converts to UTC
immediately. Everything after that point - every comparison, every bar start,
every wake time, every checkpoint - is UTC. `market_date` is the only function
that converts back, because "which session day is this instant in?" is a
question that can only be answered in the exchange's own zone.

**The completed-bar rule is not re-implemented here.** `is_bar_complete` from
the shared runtime schedule already encodes it - a bar stamped 15:45 covers
``[15:45, 16:00)`` and is in progress until 16:00, minus a provider-lag safety
delay - and a second copy of that rule for equities would be a second thing to
get wrong. This module adds exactly one equity-specific constraint on top of
it: the bar must also be a *regular-session* bar.

**Extended-hours bars are not regular-session bars.** Alpaca's IEX feed serves
pre-market and post-market 15-minute bars, so a fetch that simply asks for a
time window comes back with candles from 04:00 and 19:30 Eastern mixed in.
Those are real data and they are not tradable here: `regular_session_bar_starts`
enumerates only the boundaries whose *whole interval* lies inside the session,
and everything else is filtered out before the strategy ever sees it.

**The last bar of a session is deliberately not actionable.** The bar starting
at 15:45 closes at 16:00, which is the instant the session ends. Acting on it
would mean submitting after the close, and Equity V0.2 trades regular hours
only - so the wake times this module produces stop at the last boundary that
still lands inside the session. That is a real, named consequence of the
regular-hours rule rather than an oversight; see `session_wake_times`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from autotrader.equity import MARKET_TIMEZONE, EquityError
from autotrader.runtime.schedule import (
    BAR_INTERVAL,
    DEFAULT_SAFETY_DELAY,
    ScheduleError,
    floor_to_boundary,
    is_bar_complete,
    require_safety_delay,
    require_utc,
)

#: The fewest regular-session 15-minute bars a real session can contribute.
#:
#: A 09:30-13:00 early close is the shortest session the US market actually
#: runs: three and a half hours, fourteen whole 15-minute intervals. Fourteen
#: is used rather than the twenty-six of a full day so that a lookback sized in
#: sessions is guaranteed to reach far enough back even if every session in the
#: window was a half day.
MIN_REGULAR_BARS_PER_SESSION = 14

#: Extra calendar days added when turning a session count into a date range.
#:
#: Five sessions occupy seven calendar days, and a week containing a holiday
#: occupies eight or nine. This margin is deliberately generous: the range is
#: only used to *ask the calendar* which days are sessions, which is a cached
#: lookup rather than a bar download, so over-asking costs nothing and
#: under-asking would silently shorten the strategy's history.
CALENDAR_MARGIN_DAYS = 10

#: How far ahead `next_wake_time` will look for the next session before giving
#: up. Long enough to clear the longest run of consecutive market closures the
#: US calendar produces; short enough that a broken calendar source surfaces as
#: an error rather than an infinite scan.
MAX_SESSION_SEARCH_DAYS = 10


class SessionError(EquityError):
    """A session could not be described, or the calendar could not be read."""


@dataclass(frozen=True)
class MarketSession:
    """One regular US market session, in UTC.

    `session_date` is the exchange's own calendar date for the session, in
    `MARKET_TIMEZONE`; it is what identifies the session and what the broker
    keys its calendar by. `open_utc` and `close_utc` are the same session's
    boundaries as instants, which is the only form anything else here compares
    against.

    Both boundaries are stored, rather than an open plus a duration, because an
    early close is a different close time and not a shorter day measured from
    somewhere else.
    """

    session_date: date
    open_utc: datetime
    close_utc: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.session_date, date) or isinstance(self.session_date, datetime):
            raise SessionError(
                f"session_date must be a date, got {type(self.session_date).__name__}."
            )
        # `require_utc` is the shared rule and is deliberately reused, but its
        # `ScheduleError` describes a bar boundary rather than a session. A
        # caller building a session should get one kind of error back, so the
        # translation happens here rather than at every call site.
        try:
            object.__setattr__(self, "open_utc", require_utc(self.open_utc, "open_utc"))
            object.__setattr__(self, "close_utc", require_utc(self.close_utc, "close_utc"))
        except ScheduleError as error:
            raise SessionError(str(error)) from None
        if self.close_utc <= self.open_utc:
            raise SessionError(
                f"A session must close after it opens; got open {self.open_utc.isoformat()} "
                f"and close {self.close_utc.isoformat()} for {self.session_date.isoformat()}."
            )

    @property
    def duration(self) -> timedelta:
        """How long the regular session runs."""
        return self.close_utc - self.open_utc

    def contains(self, moment: datetime) -> bool:
        """Whether `moment` falls inside ``[open, close)``.

        Half-open on purpose. At exactly 16:00 the session has ended: an order
        submitted at that instant is an after-hours order, and Equity V0.2 does
        not place one.
        """
        return self.open_utc <= require_utc(moment, "moment") < self.close_utc


def session_from_local(
    session_date: date,
    open_local: datetime,
    close_local: datetime,
) -> MarketSession:
    """Build a session from the naive Eastern times a broker calendar reports.

    A naive datetime is read as `MARKET_TIMEZONE` - which is what it is - and
    converted to UTC. An already-aware datetime is converted rather than
    refused, because the instant is then unambiguous no matter which zone it
    arrived in; only the naive case needs a zone supplied, and supplying the
    wrong one is the failure this function exists to prevent.
    """
    return MarketSession(
        session_date=session_date,
        open_utc=_to_utc(open_local, "session open"),
        close_utc=_to_utc(close_local, "session close"),
    )


def _to_utc(moment: datetime, field: str) -> datetime:
    """A naive Eastern wall-clock time, or any aware time, as a UTC instant."""
    if not isinstance(moment, datetime):
        raise SessionError(f"{field} must be a datetime, got {type(moment).__name__}.")
    if moment.tzinfo is None:
        return moment.replace(tzinfo=MARKET_TIMEZONE).astimezone(UTC)
    return moment.astimezone(UTC)


def market_date(moment: datetime) -> date:
    """The exchange calendar date `moment` falls on, in `MARKET_TIMEZONE`.

    The only conversion out of UTC in this package. A UTC instant at 01:00 on a
    Tuesday is still Monday evening in New York, and asking the calendar about
    the wrong day is how a runtime concludes the market is shut on a day it is
    open.
    """
    return require_utc(moment, "moment").astimezone(MARKET_TIMEZONE).date()


class MarketCalendar(Protocol):
    """Where session times come from.

    A protocol so the runtime never holds a broker client, and so every session
    rule below is testable against a handful of literal sessions instead of a
    network. The production implementation is
    `autotrader.execution.equity.AlpacaMarketCalendar`, which reads Alpaca's
    own calendar endpoint.
    """

    def session_for(self, day: date) -> MarketSession | None:
        """The regular session on `day`, or None when the market is closed."""

    def sessions_between(self, start: date, end: date) -> tuple[MarketSession, ...]:
        """Every session in the inclusive date range, ascending."""


# --------------------------------------------------------------------------
# Bars inside a session
# --------------------------------------------------------------------------


def _ceil_to_boundary(moment: datetime) -> datetime:
    """The 15-minute UTC boundary at or immediately after `moment`."""
    floored = floor_to_boundary(moment)
    return floored if floored == moment else floored + BAR_INTERVAL


def regular_session_bar_starts(session: MarketSession) -> tuple[datetime, ...]:
    """Every 15-minute bar start whose whole interval lies inside `session`.

    A bar qualifies when it begins at or after the open **and** ends at or
    before the close, so a partial interval at either edge is excluded rather
    than treated as a short bar. For an ordinary 09:30-16:00 session that is
    the twenty-six bars 09:30 through 15:45; for a 13:00 early close it is the
    fourteen bars 09:30 through 12:45.

    Starts are UTC quarter-hour boundaries because that is what the provider
    actually stamps: Alpaca aligns intraday stock bars to the hour, and a
    09:30 Eastern open lands on ``13:30`` or ``14:30`` UTC depending on the
    season - a quarter-hour boundary either way. Deriving the grid from the
    boundary rather than from the open means a session that ever opened off the
    quarter hour would drop its partial first bar instead of inventing a bar
    the provider never published.
    """
    starts: list[datetime] = []
    boundary = _ceil_to_boundary(session.open_utc)
    while boundary + BAR_INTERVAL <= session.close_utc:
        starts.append(boundary)
        boundary += BAR_INTERVAL
    return tuple(starts)


def is_regular_session_bar(session: MarketSession, bar_start: datetime) -> bool:
    """Whether `bar_start` is one of `session`'s regular-session bars."""
    moment = require_utc(bar_start, "bar_start")
    return (
        session.open_utc <= moment
        and moment + BAR_INTERVAL <= session.close_utc
        and moment == floor_to_boundary(moment)
    )


def latest_completed_session_bar(
    session: MarketSession,
    *,
    now: datetime,
    safety_delay: timedelta = DEFAULT_SAFETY_DELAY,
) -> datetime | None:
    """The newest regular-session bar of `session` that has fully closed at `now`.

    None when the session has not yet produced one - before 09:45 on an
    ordinary day, the 09:30 bar is still forming and there is nothing to act
    on. The completeness test is the shared one, so an equity bar and a crypto
    bar are judged complete by exactly the same rule; the only thing added here
    is that the bar has to belong to the regular session.
    """
    moment = require_utc(now, "now")
    delay = require_safety_delay(safety_delay)
    completed = [
        start
        for start in regular_session_bar_starts(session)
        if is_bar_complete(start, now=moment, safety_delay=delay)
    ]
    return completed[-1] if completed else None


def session_wake_times(
    session: MarketSession,
    *,
    safety_delay: timedelta = DEFAULT_SAFETY_DELAY,
) -> tuple[datetime, ...]:
    """Every instant in `session` at which a completed bar becomes actionable.

    One wake time per regular-session bar, at ``bar start + 15m + safety
    delay`` - the first moment that bar has closed and the provider has had its
    grace period to publish it.

    The last bar of the session is **not** represented. Its interval ends at
    the close, so its wake time would fall outside the session, and Equity V0.2
    submits only while the regular session is open. That bar is therefore
    observed by no cycle and traded by nobody: on an ordinary day the actionable
    bars are 09:30 through 15:30, acted on at 09:45 through 15:45. Extending
    trading past the close, or acting on a candle before it closes, are the two
    alternatives - and both are worse.
    """
    delay = require_safety_delay(safety_delay)
    return tuple(
        start + BAR_INTERVAL + delay
        for start in regular_session_bar_starts(session)
        if start + BAR_INTERVAL < session.close_utc
    )


def next_wake_time(
    calendar: MarketCalendar,
    *,
    now: datetime,
    safety_delay: timedelta = DEFAULT_SAFETY_DELAY,
    max_search_days: int = MAX_SESSION_SEARCH_DAYS,
) -> datetime:
    """When an equity runtime should wake next, given the broker's calendar.

    Inside a session, that is the next bar boundary still within it. Outside
    one - overnight, at a weekend, on a holiday, or after the close - it is the
    first actionable bar of the next session the calendar offers, so a closed
    market costs one sleep rather than ninety-six no-op wake-ups a day.

    Raises `SessionError` when no session can be found within `max_search_days`.
    A calendar that reports the market shut for a fortnight is either wrong or
    describing something this system should stop for, and quietly sleeping
    forever would look identical to working.
    """
    moment = require_utc(now, "now")
    delay = require_safety_delay(safety_delay)
    day = market_date(moment)
    for offset in range(max_search_days + 1):
        session = calendar.session_for(day + timedelta(days=offset))
        if session is None:
            continue
        for wake in session_wake_times(session, safety_delay=delay):
            if wake > moment:
                return wake
    raise SessionError(
        f"No regular market session found within {max_search_days} days of "
        f"{day.isoformat()}. Refusing to guess a session the broker's calendar "
        "did not report."
    )


# --------------------------------------------------------------------------
# Lookback windows
# --------------------------------------------------------------------------


def sessions_needed(lookback_bars: int) -> int:
    """How many sessions certainly contain `lookback_bars` regular-session bars.

    Sized against the *shortest* real session rather than a full day, so a
    lookback that happens to land on a run of early closes still reaches far
    enough back for EMA 50 to be defined.
    """
    if isinstance(lookback_bars, bool) or not isinstance(lookback_bars, int):
        raise SessionError(f"lookback_bars must be an int, got {type(lookback_bars).__name__}.")
    if lookback_bars <= 0:
        raise SessionError(f"lookback_bars must be greater than zero, got {lookback_bars}.")
    return -(-lookback_bars // MIN_REGULAR_BARS_PER_SESSION)


def calendar_range_for_sessions(day: date, *, count: int) -> tuple[date, date]:
    """An inclusive date range certain to contain `count` sessions ending at `day`.

    Weekends and holidays mean a session count is not a day count, so the range
    is deliberately over-wide: five sessions per seven days, plus a fixed
    margin. It bounds a *calendar* query, not a bar download - the sessions it
    returns are then counted exactly - so asking for too many days costs one
    cached lookup and asking for too few would silently truncate history.
    """
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise SessionError(f"count must be a positive int, got {count!r}.")
    span = count * 7 // 5 + CALENDAR_MARGIN_DAYS
    return day - timedelta(days=span), day


def recent_sessions(
    calendar: MarketCalendar,
    *,
    day: date,
    count: int,
) -> tuple[MarketSession, ...]:
    """The `count` most recent sessions at or before `day`, ascending.

    Fewer are returned when the calendar simply does not go back that far,
    which the caller sees as a shorter history rather than as an error: a
    strategy window that cannot be filled is C2's and C3's business to reject,
    not this module's to fabricate.
    """
    start, end = calendar_range_for_sessions(day, count=count)
    sessions = calendar.sessions_between(start, end)
    return tuple(sessions[-count:])


def lookback_window(
    sessions: Sequence[MarketSession],
    *,
    latest_bar_start: datetime,
) -> tuple[datetime, datetime]:
    """The ``(start, end)`` UTC request window covering `sessions` up to a bar.

    `end` is the last instant of the newest completed bar's interval rather
    than the next boundary, matching the inclusive-`end` handling the crypto
    path already uses, so the provider is never even asked for the candle that
    is still forming.
    """
    if not sessions:
        raise SessionError(
            "A lookback window needs at least one session; the broker's calendar "
            "reported none, so no bars were requested."
        )
    latest = require_utc(latest_bar_start, "latest_bar_start")
    return sessions[0].open_utc, latest + BAR_INTERVAL


def session_bar_mask(
    sessions: Sequence[MarketSession],
    bar_starts: Sequence[datetime],
) -> list[bool]:
    """Which of `bar_starts` are regular-session bars of any of `sessions`.

    The filter that keeps pre-market and post-market candles out of the
    strategy. Evaluated per bar against the session covering that bar's own
    day, so a fortnight of history containing an early close is filtered by
    that day's real close rather than by a single assumed one.
    """
    by_date = {session.session_date: session for session in sessions}
    mask: list[bool] = []
    for start in bar_starts:
        moment = require_utc(start, "bar timestamp")
        session = by_date.get(market_date(moment))
        mask.append(session is not None and is_regular_session_bar(session, moment))
    return mask


def is_market_open(
    calendar: MarketCalendar,
    *,
    now: datetime,
) -> tuple[bool, MarketSession | None]:
    """Whether the regular session is open at `now`, and the session it is in.

    Returns the day's session even when it is closed - before the open, or
    after it - so a caller can report *which* session it is outside of rather
    than only that it is. `(False, None)` means the calendar has no session on
    that exchange date at all: a weekend or a holiday.
    """
    moment = require_utc(now, "now")
    session = calendar.session_for(market_date(moment))
    if session is None:
        return False, None
    return session.contains(moment), session


__all__ = [
    "CALENDAR_MARGIN_DAYS",
    "MAX_SESSION_SEARCH_DAYS",
    "MIN_REGULAR_BARS_PER_SESSION",
    "MarketCalendar",
    "MarketSession",
    "SessionError",
    "calendar_range_for_sessions",
    "is_market_open",
    "is_regular_session_bar",
    "latest_completed_session_bar",
    "lookback_window",
    "market_date",
    "next_wake_time",
    "recent_sessions",
    "regular_session_bar_starts",
    "session_bar_mask",
    "session_from_local",
    "session_wake_times",
    "sessions_needed",
]
