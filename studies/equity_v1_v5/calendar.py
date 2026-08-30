"""The broker's session calendar, snapshotted to a file so a study can be re-run.

The production calendar (`autotrader.execution.equity.AlpacaMarketCalendar`) is
the authority on which days are sessions and when each one opens and closes, and
this study does not second-guess it: no weekday rule, no holiday list, no
hardcoded 13:00. But a research result has to be reproducible next month, and a
live endpoint is not reproducible - so the calendar is read **once**, written to
JSON, and every later run reads the file.

**The snapshot is the provenance.** It records the exact open and close the
broker reported for every session in the window, in the broker's own naive
Eastern wall-clock form, alongside the UTC instants they were converted to. A
reader who doubts a DST claim or an early-close claim in the report can check it
against this file without a network or an account.

`SnapshotCalendar` satisfies `autotrader.equity.session.MarketCalendar`, so
every session rule in the production module - `regular_session_bar_starts`,
`session_bar_mask`, `recent_sessions` - runs against the snapshot unchanged.
That is the point: the study proves the shipped session arithmetic, rather than
proving a second implementation of it that happens to live here.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from autotrader.equity import MARKET_TIMEZONE, MARKET_TIMEZONE_NAME
from autotrader.equity.session import MarketSession, SessionError

#: The wall-clock open every ordinary US regular session has had for the whole
#: window. Not used to *build* a session - the broker's value is - only to
#: classify one in the audit, so an unexpected open surfaces as a finding.
ORDINARY_OPEN_LOCAL = "09:30"

#: The wall-clock close of a full session, and of the shortened one.
ORDINARY_CLOSE_LOCAL = "16:00"
EARLY_CLOSE_LOCAL = "13:00"


class CalendarSnapshotError(Exception):
    """The snapshot could not be built, read, or trusted."""


@dataclass(frozen=True)
class SessionRecord:
    """One session exactly as the broker reported it, plus its UTC form.

    Both spellings are stored rather than one. The naive Eastern pair is what
    the provider actually said and is what an early close is visible in; the UTC
    pair is what every comparison in the system is made against. Keeping only
    the second would throw away the evidence that the conversion was right.
    """

    session_date: str
    open_local: str
    close_local: str
    open_utc: str
    close_utc: str

    @classmethod
    def from_session(cls, session: MarketSession) -> SessionRecord:
        return cls(
            session_date=session.session_date.isoformat(),
            open_local=session.open_utc.astimezone(MARKET_TIMEZONE).strftime("%H:%M"),
            close_local=session.close_utc.astimezone(MARKET_TIMEZONE).strftime("%H:%M"),
            open_utc=session.open_utc.isoformat(),
            close_utc=session.close_utc.isoformat(),
        )

    def to_session(self) -> MarketSession:
        """Rebuild the `MarketSession`, from the UTC instants rather than the local ones.

        The UTC pair is unambiguous and the local pair is not - a wall-clock
        time on a DST transition day can name two instants - so the round trip
        goes through the form that cannot be misread.
        """
        return MarketSession(
            session_date=date.fromisoformat(self.session_date),
            open_utc=datetime.fromisoformat(self.open_utc),
            close_utc=datetime.fromisoformat(self.close_utc),
        )

    @property
    def is_early_close(self) -> bool:
        """Whether the broker reported a close before the ordinary one."""
        return self.close_local < ORDINARY_CLOSE_LOCAL

    @property
    def utc_offset_hours(self) -> int:
        """The session's Eastern UTC offset: ``-5`` in winter, ``-4`` in summer.

        Derived from the reported instants rather than from a date rule, so a
        DST claim in the report is a measurement of the broker's own data.
        """
        opened = datetime.fromisoformat(self.open_utc).astimezone(MARKET_TIMEZONE)
        offset = opened.utcoffset()
        if offset is None:  # pragma: no cover - an aware datetime always has one
            raise CalendarSnapshotError(f"Session {self.session_date} has no UTC offset.")
        return int(offset.total_seconds() // 3600)


class SnapshotCalendar:
    """A `MarketCalendar` backed by a file. Offline, deterministic, read-only."""

    def __init__(self, records: Iterable[SessionRecord]) -> None:
        self._records = {
            record.session_date: record
            for record in sorted(records, key=lambda item: item.session_date)
        }
        self._sessions = {
            date.fromisoformat(key): record.to_session() for key, record in self._records.items()
        }
        #: Kept at zero for the shared API budget: a snapshot sends nothing.
        self.api_calls = 0

    def session_for(self, day: date) -> MarketSession | None:
        """The regular session on `day`, or None when the market is closed."""
        return self._sessions.get(day)

    def sessions_between(self, start: date, end: date) -> tuple[MarketSession, ...]:
        """Every session in the inclusive date range, ascending."""
        return tuple(
            session for day, session in sorted(self._sessions.items()) if start <= day <= end
        )

    @property
    def records(self) -> tuple[SessionRecord, ...]:
        """Every snapshotted session, ascending, in the broker's own spelling."""
        return tuple(self._records.values())

    def __len__(self) -> int:
        return len(self._sessions)


def snapshot_path(directory: Path, start: date, end: date) -> Path:
    """The deterministic filename for one snapshotted range."""
    return Path(directory) / f"market_calendar_{start.isoformat()}_{end.isoformat()}.json"


def write_snapshot(
    sessions: Iterable[MarketSession],
    path: Path,
    *,
    start: date,
    end: date,
    retrieved_at: datetime,
) -> dict[str, object]:
    """Persist the broker's sessions, with the provenance needed to challenge them."""
    records = [SessionRecord.from_session(session) for session in sessions]
    payload: dict[str, object] = {
        "provider": "alpaca",
        "endpoint": "GET /v2/calendar",
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "session_timezone": MARKET_TIMEZONE_NAME,
        "retrieved_at_utc": retrieved_at.astimezone(UTC).isoformat(),
        "session_count": len(records),
        "sessions": [record.__dict__ for record in records],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def read_snapshot(path: Path) -> tuple[SnapshotCalendar, dict[str, object]]:
    """Load a snapshot and its provenance, refusing one that is empty or malformed."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CalendarSnapshotError(
            f"No calendar snapshot at {path}. Build one first; this study never "
            "guesses a session the broker did not report."
        ) from None
    except json.JSONDecodeError as error:
        raise CalendarSnapshotError(
            f"Calendar snapshot {path} is not valid JSON: {error}"
        ) from None

    entries = payload.get("sessions")
    if not isinstance(entries, list) or not entries:
        raise CalendarSnapshotError(f"Calendar snapshot {path} contains no sessions.")
    try:
        records = [SessionRecord(**entry) for entry in entries]
    except TypeError as error:
        raise CalendarSnapshotError(
            f"Calendar snapshot {path} has an unexpected shape: {error}"
        ) from None
    return SnapshotCalendar(records), payload


def fetch_sessions(start: date, end: date) -> tuple[MarketSession, ...]:
    """Read the broker's calendar for an inclusive range. One GET, nothing else.

    Imported lazily so that every other function in this module - and every test
    over a snapshot - stays importable without a broker SDK or a credential.
    """
    from autotrader.execution.equity import AlpacaMarketCalendar

    try:
        return AlpacaMarketCalendar().sessions_between(start, end)
    except SessionError as error:
        raise CalendarSnapshotError(str(error)) from None


__all__ = [
    "EARLY_CLOSE_LOCAL",
    "ORDINARY_CLOSE_LOCAL",
    "ORDINARY_OPEN_LOCAL",
    "CalendarSnapshotError",
    "SessionRecord",
    "SnapshotCalendar",
    "fetch_sessions",
    "read_snapshot",
    "snapshot_path",
    "write_snapshot",
]
