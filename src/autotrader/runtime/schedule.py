"""C9: fixed UTC 15-minute scheduling arithmetic and the completed-bar rule.

Pure functions over `datetime`. Nothing here sleeps, reads a clock, opens a
socket, or touches a database, so every rule below is testable without waiting
fifteen real minutes for anything.

**Crypto runs every day.** There is no exchange session in this module: no
market open, no close, no holiday calendar, no weekday filter, no
`America/New_York`, and no `get_clock`. A boundary on a Sunday is the same
boundary as one on a Tuesday. The only calendar concept the system recognises
is a UTC day (docs/SPEC.md section 3.4).

**Boundaries are wall-clock, not elapsed time.** The next wake-up is computed
from the current UTC time on every cycle - `00`, `15`, `30`, `45` minutes past
the hour - rather than by repeatedly sleeping 900 seconds. Sleeping a fixed
interval accumulates every scheduler delay and every millisecond a cycle took,
so a process that starts on the boundary drifts off it within a day. Recomputing
from the wall clock cannot drift.

**Bar timestamps are interval *start*.** This is not an assumption: Alpaca's
crypto bars endpoint returns 15-minute bars stamped at `:00`, `:15`, `:30` and
`:45` with zero seconds, and it serves the bar for the interval that is still
running - at 00:16:17 UTC it already returns a bar stamped 00:15:00, whose
interval does not end until 00:30. That bar is *in progress*. Acting on it
would be trading a candle whose close has not happened yet.

So a bar is only complete when::

    bar_timestamp + 15 minutes <= now - safety_delay

**The safety delay is provider lag, not slack.** A bar's interval ending at
exactly 10:15:00 does not mean the provider has published it at 10:15:00.000.
`DEFAULT_SAFETY_DELAY` is the small, explicit grace period the runtime waits
after a boundary before asking for the bar that just closed, and it is
subtracted from `now` everywhere completeness is judged, so an early wake-up
can never smuggle an unpublished bar through.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

#: The one bar interval this milestone runs on (docs/SPEC.md section 3.2).
BAR_INTERVAL = timedelta(minutes=15)

#: The minutes past every hour at which an interval starts. Every day, all day.
BOUNDARY_MINUTES: tuple[int, ...] = (0, 15, 30, 45)

#: How long to wait after a boundary before treating the bar that just closed
#: as fetchable. Small and explicit: provider availability lags the boundary
#: slightly, and this is the only allowance made for it.
DEFAULT_SAFETY_DELAY = timedelta(seconds=5)

#: The delay may never reach a whole interval - a runtime that waited a full
#: bar before reading one would always be a bar behind.
MAX_SAFETY_DELAY = BAR_INTERVAL - timedelta(seconds=1)

#: How many completed bars one strategy evaluation may look back over.
#:
#: EMA 50 needs 50 observations before it produces a value at all, and the
#: crossover additionally reads the previous bar, so 51 is the arithmetic
#: floor. The bounds below are the *operational* range: enough history for the
#: recursive EMA to have forgotten its seed - after 200 bars a span-50 EMA
#: retains about 0.04% of its first observation - and few enough that a cycle
#: is one small request rather than a re-download of the whole history.
MIN_LOOKBACK_BARS = 100
MAX_LOOKBACK_BARS = 200
DEFAULT_LOOKBACK_BARS = 200


class ScheduleError(Exception):
    """A scheduling input that cannot describe a real moment or interval."""


def require_utc(moment: datetime, field: str = "moment") -> datetime:
    """Return `moment` as UTC, refusing a naive datetime.

    A naive datetime has no offset to convert from, so treating one as UTC
    would silently misdate every boundary by the operator's local offset. An
    aware datetime in another zone is converted rather than refused: the
    instant is unambiguous, and UTC is the only frame this system schedules in.
    """
    if not isinstance(moment, datetime):
        raise ScheduleError(f"{field} must be a datetime, got {type(moment).__name__}.")
    if moment.tzinfo is None:
        raise ScheduleError(
            f"{field} must be timezone-aware; a naive datetime would be read as UTC "
            "and silently misdate every bar boundary."
        )
    return moment.astimezone(UTC)


def require_safety_delay(value: timedelta, field: str = "safety_delay") -> timedelta:
    """Require a non-negative delay shorter than one bar interval."""
    if not isinstance(value, timedelta):
        raise ScheduleError(f"{field} must be a timedelta, got {type(value).__name__}.")
    if value < timedelta(0):
        raise ScheduleError(f"{field} must not be negative, got {value}.")
    if value > MAX_SAFETY_DELAY:
        raise ScheduleError(
            f"{field} must be at most {MAX_SAFETY_DELAY} - shorter than one "
            f"{BAR_INTERVAL} bar - got {value}."
        )
    return value


def require_lookback_bars(value: int, field: str = "lookback_bars") -> int:
    """Require a bounded completed-bar lookback.

    Bounded in both directions on purpose. Too few bars and the EMA 50 is
    either undefined or still dominated by its seed; too many and every cycle
    re-downloads history the strategy cannot use. This is a strategy window,
    not a data warehouse.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScheduleError(f"{field} must be an int, got {type(value).__name__}.")
    if not MIN_LOOKBACK_BARS <= value <= MAX_LOOKBACK_BARS:
        raise ScheduleError(
            f"{field} must be between {MIN_LOOKBACK_BARS} and {MAX_LOOKBACK_BARS} "
            f"completed bars, got {value}."
        )
    return value


def is_boundary(moment: datetime) -> bool:
    """Whether `moment` lands exactly on a 15-minute UTC boundary.

    Sub-microsecond precision is checked too: a pandas timestamp carries
    nanoseconds that `datetime.microsecond` cannot see, and a bar stamped a
    few nanoseconds off a boundary is a provider anomaly this runtime should
    notice rather than round away.
    """
    moment_utc = require_utc(moment)
    return (
        moment_utc.minute in BOUNDARY_MINUTES
        and moment_utc.second == 0
        and moment_utc.microsecond == 0
        and getattr(moment_utc, "nanosecond", 0) % 1_000 == 0
    )


def floor_to_boundary(moment: datetime) -> datetime:
    """The 15-minute UTC boundary at or immediately before `moment`."""
    moment_utc = require_utc(moment)
    return moment_utc.replace(minute=(moment_utc.minute // 15) * 15, second=0, microsecond=0)


def next_boundary(now: datetime) -> datetime:
    """The first 15-minute UTC boundary strictly after `now`.

    Strictly after, so a runtime that wakes exactly on a boundary schedules the
    following one instead of the one it is standing on. That is what makes the
    loop always advance, and it rolls over midnight, month, and year without a
    special case because the arithmetic is done on the instant, not the clock
    face.
    """
    return floor_to_boundary(now) + BAR_INTERVAL


def next_wake_time(now: datetime, *, safety_delay: timedelta = DEFAULT_SAFETY_DELAY) -> datetime:
    """When the runtime should wake next: the next boundary plus the delay.

    Recomputed from the wall clock every cycle, so a slow cycle costs that
    cycle and nothing after it.
    """
    delay = require_safety_delay(safety_delay)
    return next_boundary(now) + delay


def effective_now(now: datetime, *, safety_delay: timedelta = DEFAULT_SAFETY_DELAY) -> datetime:
    """`now` pulled back by the provider-lag allowance.

    Every completeness judgement is made against this instant rather than the
    raw clock, so waking early - for any reason - cannot make an unpublished
    bar look available.
    """
    return require_utc(now, "now") - require_safety_delay(safety_delay)


def is_bar_complete(
    bar_timestamp: datetime,
    *,
    now: datetime,
    safety_delay: timedelta = DEFAULT_SAFETY_DELAY,
) -> bool:
    """Whether the interval that *starts* at `bar_timestamp` has fully elapsed.

    The bar stamped 10:15 covers ``[10:15, 10:30)``. It is in progress until
    10:30, and the provider will happily return it before then, so this is the
    single rule that keeps the runtime off an unfinished candle.
    """
    return require_utc(bar_timestamp, "bar_timestamp") + BAR_INTERVAL <= effective_now(
        now, safety_delay=safety_delay
    )


def latest_completed_bar_start(
    now: datetime, *, safety_delay: timedelta = DEFAULT_SAFETY_DELAY
) -> datetime:
    """The start of the newest bar whose interval is complete at `now`.

    The largest boundary `B` for which ``B + 15m <= now - safety_delay``. At
    10:15:05 UTC with the default delay that is 10:00 - the 10:15 bar has only
    just begun.
    """
    return floor_to_boundary(effective_now(now, safety_delay=safety_delay)) - BAR_INTERVAL


def lookback_window_start(latest_bar_start: datetime, *, lookback_bars: int) -> datetime:
    """The start of the oldest bar in a `lookback_bars`-long completed window."""
    count = require_lookback_bars(lookback_bars)
    return require_utc(latest_bar_start, "latest_bar_start") - (count - 1) * BAR_INTERVAL


__all__ = [
    "BAR_INTERVAL",
    "BOUNDARY_MINUTES",
    "DEFAULT_LOOKBACK_BARS",
    "DEFAULT_SAFETY_DELAY",
    "MAX_LOOKBACK_BARS",
    "MAX_SAFETY_DELAY",
    "MIN_LOOKBACK_BARS",
    "ScheduleError",
    "effective_now",
    "floor_to_boundary",
    "is_bar_complete",
    "is_boundary",
    "latest_completed_bar_start",
    "lookback_window_start",
    "next_boundary",
    "next_wake_time",
    "require_lookback_bars",
    "require_safety_delay",
    "require_utc",
]
