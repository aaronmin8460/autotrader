"""M1: the bar clock, and the one place crypto and equity are allowed to differ.

Every horizon in this package is counted in *bars*: a feature reads the last 32
of them, a label measures the next 4. That is only meaningful if "the next bar"
is defined, and the two products define it differently.

**Crypto has one continuous grid.** Every 15-minute UTC boundary exists, every
day of the week, forever. The bar after 23:45 on a Saturday is 00:00 on the
Sunday, and nothing about that boundary is special. `session_id` is the UTC
calendar date and is a *label*, not a break: `has_session_gaps` is False, and
`spans_session_gap` is therefore False for every pair of crypto bars. Calling a
midnight rollover a gap would invent a discontinuity the market does not have.

**Equity grids are a chain of regular sessions.** The grid is exactly the
regular-session bars of the sessions a broker calendar reports, concatenated in
order - the twenty-six bars 09:30 to 15:45 on an ordinary day, fourteen on a
13:00 early close, none at all on a holiday. Consecutive grid positions are
consecutive *tradable* bars, so a 4-bar horizon from 15:30 lands on the second
bar of the following session rather than on 16:30, which does not exist.

**The overnight gap is carried, never smoothed.** Positions ``i`` and ``i + 1``
can be fifteen minutes apart or three days apart. `spans_session_gap` is how a
caller finds out, and both the feature layer and the label layer ask it: a
one-bar return across the gap is an overnight return, and a label whose holding
period crosses a weekend is a weekend-holding label. Neither is wrong; both are
recorded, and `autotrader.ml.labels` can refuse the second outright.

**The calendar is data, not a guess.** A grid is never derived from which bars
happen to be in a file: a holiday and a provider outage look identical in a
Parquet file, and a 13:00 early close looks exactly like a full day with six
missing bars. Equity grids therefore require an explicit session list.
`StaticMarketCalendar` satisfies the existing `MarketCalendar` protocol, so an
operator can snapshot the broker's own calendar once and store it as JSON; this
package never constructs a broker client to fetch one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from autotrader.equity.session import (
    MarketCalendar,
    MarketSession,
    SessionError,
    regular_session_bar_starts,
)
from autotrader.ml import AssetClass, MLError
from autotrader.ml.storage import read_json, write_json
from autotrader.runtime.schedule import BAR_INTERVAL, floor_to_boundary, require_utc

#: The key under which a session file stores its list. A wrapper object rather
#: than a bare array so the file can gain a field later without changing shape.
SESSIONS_KEY = "sessions"

#: What a session file records for each session. UTC on both boundaries: the
#: broker reports naive Eastern wall-clock times and `session_from_local` is
#: the one place that interpretation happens, so by the time a session reaches
#: storage it is already an unambiguous pair of instants.
SESSION_FIELDS: tuple[str, ...] = ("session_date", "open_utc", "close_utc")


class GridError(MLError):
    """A bar grid could not be built, or was asked about a bar it does not hold."""


# --------------------------------------------------------------------------
# Session calendars, without a broker
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StaticMarketCalendar:
    """A `MarketCalendar` backed by a fixed list of sessions.

    Satisfies the protocol the equity runtime already defines, so the session
    arithmetic in `autotrader.equity.session` is reused rather than re-derived
    here. It holds no client and reaches no network: the sessions came from
    somewhere else, once, and are now data.
    """

    sessions: tuple[MarketSession, ...]
    _by_date: dict[date, MarketSession] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        ordered = tuple(self.sessions)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current.session_date <= previous.session_date:
                raise GridError(
                    "Sessions must be strictly ascending by session_date; "
                    f"{current.session_date.isoformat()} follows "
                    f"{previous.session_date.isoformat()}."
                )
        object.__setattr__(self, "sessions", ordered)
        object.__setattr__(self, "_by_date", {s.session_date: s for s in ordered})

    def session_for(self, day: date) -> MarketSession | None:
        """The regular session on `day`, or None when the market is closed."""
        return self._by_date.get(day)

    def sessions_between(self, start: date, end: date) -> tuple[MarketSession, ...]:
        """Every session in the inclusive date range, ascending."""
        return tuple(s for s in self.sessions if start <= s.session_date <= end)

    @classmethod
    def from_calendar(
        cls, calendar: MarketCalendar, start: date, end: date
    ) -> StaticMarketCalendar:
        """Snapshot any `MarketCalendar` - including the broker's - into a static one.

        The seam that keeps this package offline. An operator runs this once
        against the live calendar in their own script, writes the result with
        `write_sessions`, and every dataset build afterwards reads the file.
        """
        return cls(sessions=tuple(calendar.sessions_between(start, end)))


def sessions_to_record(sessions: Sequence[MarketSession]) -> dict[str, object]:
    """The serializable form of a session list."""
    return {
        SESSIONS_KEY: [
            {
                "session_date": session.session_date.isoformat(),
                "open_utc": session.open_utc.isoformat(),
                "close_utc": session.close_utc.isoformat(),
            }
            for session in sessions
        ]
    }


def write_sessions(path: Path, sessions: Sequence[MarketSession]) -> Path:
    """Persist a session list as JSON, atomically."""
    return write_json(path, sessions_to_record(sessions))


def sessions_from_record(record: object) -> tuple[MarketSession, ...]:
    """Rebuild sessions from the JSON form, refusing anything malformed.

    Every failure here is a refusal rather than a repair. A session file with a
    missing close is not a session with an assumed 16:00 close: guessing one
    would place bars in the grid that the exchange never ran.
    """
    if not isinstance(record, dict) or SESSIONS_KEY not in record:
        raise GridError(f"A session file must be an object with a {SESSIONS_KEY!r} list.")
    entries = record[SESSIONS_KEY]
    if not isinstance(entries, list):
        raise GridError(f"{SESSIONS_KEY!r} must be a list, got {type(entries).__name__}.")
    sessions: list[MarketSession] = []
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise GridError(f"Session {position} must be an object, got {type(entry).__name__}.")
        missing = [name for name in SESSION_FIELDS if name not in entry]
        if missing:
            raise GridError(f"Session {position} is missing: {', '.join(missing)}.")
        try:
            sessions.append(
                MarketSession(
                    session_date=date.fromisoformat(str(entry["session_date"])),
                    open_utc=datetime.fromisoformat(str(entry["open_utc"])),
                    close_utc=datetime.fromisoformat(str(entry["close_utc"])),
                )
            )
        except (ValueError, SessionError) as error:
            raise GridError(f"Session {position} is not a usable session: {error}") from None
    return tuple(sessions)


def load_sessions(path: Path) -> tuple[MarketSession, ...]:
    """Read a session list written by `write_sessions`."""
    return sessions_from_record(read_json(path))


# --------------------------------------------------------------------------
# The grid
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BarGrid:
    """The ordered sequence of bars one dataset is defined over.

    A grid is *expected* bars, not observed ones. A boundary is on the grid
    because the market was running then, whether or not the provider published
    a bar for it - which is what makes a missing bar visible as a hole in
    `autotrader.ml.dataset` instead of vanishing into a shorter index.
    """

    asset_class: AssetClass
    starts: tuple[datetime, ...]
    session_ids: tuple[str, ...]
    session_bar_indices: tuple[int, ...]
    session_bar_counts: tuple[int, ...]
    has_session_gaps: bool
    _positions: dict[datetime, int] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        lengths = {
            len(self.starts),
            len(self.session_ids),
            len(self.session_bar_indices),
            len(self.session_bar_counts),
        }
        if len(lengths) != 1:
            raise GridError("Every grid column must have the same length.")
        if not self.starts:
            raise GridError("A bar grid needs at least one bar.")
        for previous, current in zip(self.starts, self.starts[1:], strict=False):
            if current <= previous:
                raise GridError(
                    "Grid bars must be strictly ascending; "
                    f"{current.isoformat()} follows {previous.isoformat()}."
                )
        object.__setattr__(self, "_positions", {start: i for i, start in enumerate(self.starts)})

    def __len__(self) -> int:
        return len(self.starts)

    def position_of(self, moment: datetime) -> int:
        """The grid position of the bar starting at `moment`.

        Raises rather than returning the nearest bar. A timestamp that is not
        on the grid is either off-boundary or outside the market's hours, and
        both are data-contract violations this package must not paper over.
        """
        instant = require_utc(moment, "moment")
        position = self._positions.get(instant)
        if position is None:
            raise GridError(
                f"{instant.isoformat()} is not a bar on this {self.asset_class.value} grid "
                f"({self.starts[0].isoformat()} to {self.starts[-1].isoformat()}, "
                f"{len(self)} bars)."
            )
        return position

    def contains(self, moment: datetime) -> bool:
        """Whether `moment` is a bar start on this grid."""
        return require_utc(moment, "moment") in self._positions

    def spans_session_gap(self, start_position: int, end_position: int) -> bool:
        """Whether moving from one position to another crosses a session break.

        Always False on a continuous crypto grid: `session_id` there is a UTC
        date used for split boundaries, and a date changing at midnight is not
        a market closing. On an equity grid it is True whenever the two bars
        belong to different sessions - an overnight hold, a weekend, a holiday.
        """
        if not self.has_session_gaps:
            return False
        return self.session_ids[start_position] != self.session_ids[end_position]

    @property
    def first_start(self) -> datetime:
        return self.starts[0]

    @property
    def last_start(self) -> datetime:
        return self.starts[-1]

    @property
    def session_count(self) -> int:
        """How many distinct sessions the grid spans."""
        return len(set(self.session_ids))

    def to_record(self) -> dict[str, object]:
        """The provenance form stored in a dataset's metadata sidecar."""
        return {
            "asset_class": self.asset_class.value,
            "bar_interval_minutes": int(BAR_INTERVAL.total_seconds() // 60),
            "bar_count": len(self),
            "session_count": self.session_count,
            "has_session_gaps": self.has_session_gaps,
            "first_bar_start_utc": self.first_start.isoformat(),
            "last_bar_start_utc": self.last_start.isoformat(),
        }


#: How many 15-minute bars a UTC day holds. A crypto day is always this long.
BARS_PER_UTC_DAY = 96


def _utc_day_bar_index(moment: datetime) -> int:
    """Which 15-minute slot of its UTC day `moment` starts.

    Derived from the clock rather than from the bar's position in the grid, so
    a grid that begins at 09:15 still reports that bar as slot 37 of its day.
    A grid-relative count would change every time the requested range changed,
    which would make the same market moment produce two different feature rows.
    """
    return (moment.hour * 60 + moment.minute) // 15


def crypto_grid(start: datetime, end: datetime) -> BarGrid:
    """Every 15-minute UTC boundary from `start` to `end`, inclusive.

    Both bounds are floored to a boundary, because a grid is made of bar starts
    and a request that begins at 09:07 means the bar that was running then.
    Weekends and holidays are ordinary bars: this grid has no holes by
    construction, so any bar the provider did not publish shows up downstream
    as a missing observation rather than as a shorter grid.
    """
    first = floor_to_boundary(require_utc(start, "start"))
    last = floor_to_boundary(require_utc(end, "end"))
    if last < first:
        raise GridError(f"Grid end {last.isoformat()} is before start {first.isoformat()}.")
    starts: list[datetime] = []
    boundary = first
    while boundary <= last:
        starts.append(boundary)
        boundary += BAR_INTERVAL
    return BarGrid(
        asset_class=AssetClass.CRYPTO,
        starts=tuple(starts),
        session_ids=tuple(moment.date().isoformat() for moment in starts),
        session_bar_indices=tuple(_utc_day_bar_index(moment) for moment in starts),
        session_bar_counts=(BARS_PER_UTC_DAY,) * len(starts),
        has_session_gaps=False,
    )


def equity_grid(sessions: Sequence[MarketSession]) -> BarGrid:
    """The regular-session bars of `sessions`, concatenated in order.

    Only whole intervals inside a session qualify, which is
    `regular_session_bar_starts`' rule and is reused rather than restated:
    pre-market and post-market candles are not on this grid, and neither is a
    partial interval at either edge. A session that contributes no whole bar is
    refused rather than silently dropped - a calendar reporting one is
    describing something this package should not model.
    """
    ordered = tuple(sessions)
    if not ordered:
        raise GridError(
            "An equity grid needs at least one session. Equity bars are only "
            "defined relative to a calendar, and this package will not infer "
            "one from which bars happen to be present."
        )
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.session_date <= previous.session_date:
            raise GridError(
                "Sessions must be strictly ascending by session_date; "
                f"{current.session_date.isoformat()} follows "
                f"{previous.session_date.isoformat()}."
            )
    starts: list[datetime] = []
    session_ids: list[str] = []
    indices: list[int] = []
    counts: list[int] = []
    for session in ordered:
        bars = regular_session_bar_starts(session)
        if not bars:
            raise GridError(
                f"Session {session.session_date.isoformat()} "
                f"({session.open_utc.isoformat()} to {session.close_utc.isoformat()}) "
                "contains no whole 15-minute regular-session bar."
            )
        starts.extend(bars)
        session_ids.extend([session.session_date.isoformat()] * len(bars))
        indices.extend(range(len(bars)))
        counts.extend([len(bars)] * len(bars))
    return BarGrid(
        asset_class=AssetClass.EQUITY,
        starts=tuple(starts),
        session_ids=tuple(session_ids),
        session_bar_indices=tuple(indices),
        session_bar_counts=tuple(counts),
        has_session_gaps=True,
    )


def build_grid(
    asset_class: AssetClass,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    sessions: Sequence[MarketSession] | None = None,
) -> BarGrid:
    """Build the grid `asset_class` requires, refusing the wrong inputs for it.

    Crypto needs a time range and nothing else; equity needs a calendar and
    cannot be given a range instead. Mixing them up is refused with the reason
    rather than accommodated, because "build an equity grid from a date range"
    is precisely the request that would hardcode a 09:30-16:00 week.
    """
    if asset_class is AssetClass.CRYPTO:
        if sessions is not None:
            raise GridError(
                "A crypto grid takes no session calendar. Crypto trades 24/7: "
                "there is no session to be inside of."
            )
        if start is None or end is None:
            raise GridError("A crypto grid needs both a start and an end instant.")
        return crypto_grid(start, end)
    if sessions is None:
        raise GridError(
            "An equity grid needs an explicit session calendar. A date range "
            "cannot supply one: holidays and early closes are facts a broker "
            "reports, not dates this package may assume."
        )
    if start is not None or end is not None:
        raise GridError(
            "An equity grid is bounded by its sessions, not by a time range. "
            "Pass the sessions you want and nothing else."
        )
    return equity_grid(sessions)


def bar_span(grid: BarGrid, start_position: int, end_position: int) -> timedelta:
    """Wall-clock time between two grid bars' starts.

    Not the same as their distance in bars, and that is the point: four equity
    bars can be an hour or three days, and a caller reporting a holding period
    should say which one it means.
    """
    return grid.starts[end_position] - grid.starts[start_position]


def utc_day_bounds(moments: Iterable[datetime]) -> tuple[datetime, datetime]:
    """The earliest and latest instants in `moments`, as UTC.

    Used to size a crypto grid from a bar file that already exists. Raises on
    an empty iterable rather than inventing a range.
    """
    instants = sorted(require_utc(moment, "moment") for moment in moments)
    if not instants:
        raise GridError("No timestamps were supplied, so no grid range can be derived.")
    return instants[0], instants[-1]


def now_utc() -> datetime:
    """The current instant, in UTC.

    The one clock read in this package, isolated so that everything else is a
    pure function of its inputs and every rule above is testable without one.
    """
    return datetime.now(UTC)


__all__ = [
    "BARS_PER_UTC_DAY",
    "SESSIONS_KEY",
    "SESSION_FIELDS",
    "BarGrid",
    "GridError",
    "StaticMarketCalendar",
    "bar_span",
    "build_grid",
    "crypto_grid",
    "equity_grid",
    "load_sessions",
    "now_utc",
    "sessions_from_record",
    "sessions_to_record",
    "utc_day_bounds",
    "write_sessions",
]
