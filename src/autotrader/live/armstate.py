"""The durable arm switch. DISARMED is the default, and it survives a restart.

A real-money runtime needs an off switch an operator can reach in a hurry, and
"in a hurry" rules out most of the obvious implementations. Stopping the
process is not it: the process comes back, and something has to decide what it
may do when it does. An in-memory flag is not it either, for the same reason
the shared account halt is not one - a variable cannot cross a process boundary
and does not survive `systemctl restart`.

So the switch is **a row on disk**, and the absence of that row means DISARMED.
That inversion is the whole design. A fresh database, a corrupted row, a
schema this build does not recognise, a store that cannot be opened at all -
every one of them is a state in which nobody has said "you may trade real
money", and every one of them therefore answers DISARMED. There is no path
through this module in which an unreadable answer becomes permission.

**Two gates, and neither can satisfy the other.** Arming requires the durable
row *and* an environment gate that lives in a file systemd loads. They are
deliberately different kinds of thing in different places: the row is what an
operator flips at 09:45 without editing anything as root, and the environment
file is what a host must have been deliberately provisioned with before the
row can mean anything at all. A stolen shell that can write the database still
cannot arm a host whose environment file is absent, and a misprovisioned
environment file cannot arm a runtime nobody armed.

**What DISARMED does and does not do.** It blocks new broker mutation. It does
not cancel anything, does not flatten anything, does not delete a position, an
intent, or a row of history, and does not stop the runtime reading the account,
running reconciliation, computing EDA-1, or writing diagnostics. That is
deliberate: an operator hitting the switch during an anomaly needs the system
to *keep observing* so they can see what is happening, and a kill switch that
liquidated a book on the way out would be a far more dangerous thing than the
anomaly it was reacting to.

**Disarming is always allowed; arming is never accidental.** `disarm` takes no
token and can be called by anything, including the runtime disarming itself on
an anomaly. `arm` requires an exact confirmation string, in the same spirit as
the paper runtime's `--confirm-paper-runtime PAPER`: the safe direction is
free, the dangerous one is deliberate.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from autotrader.state import sqlite as state

#: The environment half of the two-gate arm. Read from a file the unit loads
#: after its safe defaults, so removing the file is the off switch - exactly
#: the shape `autotrader-equity-paper.trading.env` already has.
LIVE_ARMED_ENV = "AUTOTRADER_LIVE_ARMED"

#: The **only** value that opens the environment gate. Compared exactly, after
#: stripping surrounding whitespace: `TRUE`, `True`, `1`, `yes`, and `on` all
#: leave it closed. One canonical spelling means a typo can only fail closed.
LIVE_ARMED_VALUE = "true"

#: The token `arm_live_trading` requires, compared exactly. It names what it
#: authorizes: an operator typing it has read the words "real money".
ARM_CONFIRMATION_TOKEN = "ARM-LIVE-REAL-MONEY"

STATE_ARMED = "ARMED"
STATE_DISARMED = "DISARMED"

#: Who moved the switch. Free text in the database; these are what this system
#: writes, so a status line can name the party responsible.
SOURCE_OPERATOR = "operator"
SOURCE_RUNTIME = "live-runtime"
SOURCE_DEFAULT = "default"

#: Audit event types written to `system_events`.
EVENT_ARMED = "LIVE_ARMED"
EVENT_DISARMED = "LIVE_DISARMED"

#: The banner an operator greps for when the arm switch is what refused.
LIVE_DISARMED_BANNER = "LIVE DISARMED - NO BROKER MUTATION"

#: Created on demand rather than added to the versioned schema, for the same
#: reason `equity_paper_targets` is: every command in this lineage migrates a
#: store upward when it opens it, and a schema bump for a table only the live
#: runtime uses would put this store a version ahead of stores that have no use
#: for it. A version gap between stores is the expensive kind of mistake here.
CREATE_ARM_STATE = """
    CREATE TABLE IF NOT EXISTS live_arm_state (
        id           INTEGER PRIMARY KEY CHECK (id = 1),
        state        TEXT NOT NULL CHECK (state IN ('ARMED', 'DISARMED')),
        reason       TEXT NOT NULL,
        source       TEXT NOT NULL,
        changed_at   TEXT NOT NULL,
        code_sha     TEXT
    )
"""


class LiveDisarmedError(Exception):
    """Live trading is not armed, so no broker mutation may happen.

    Raised *before* a broker is contacted. It is not a broker condition and
    retrying changes nothing: the switch is moved by an operator, never by the
    passage of time and never by the runtime deciding it feels ready.
    """


@dataclass(frozen=True)
class ArmState:
    """The durable answer, as it was read.

    `armed` is a property rather than a stored flag so there is exactly one
    place that decides what the text means, and it decides in the safe
    direction: anything that is not the literal `ARMED` string is not armed.
    """

    state: str
    reason: str
    source: str
    changed_at: datetime | None
    code_sha: str | None = None

    @property
    def armed(self) -> bool:
        return self.state == STATE_ARMED


#: What a store with no row reports. Not an error: a database that has never
#: been armed is in a perfectly well-defined state, and that state is off.
UNARMED_DEFAULT = ArmState(
    state=STATE_DISARMED,
    reason=(
        "No arm state has been recorded in this store, so nothing has ever authorized "
        "real-money order submission from it."
    ),
    source=SOURCE_DEFAULT,
    changed_at=None,
)


def create_arm_state_table(connection: sqlite3.Connection) -> None:
    """Ensure the arm record exists. Idempotent, and creates no row.

    Deliberately does **not** insert a DISARMED row. An absent row already
    means disarmed, and writing one would make "this store has never been
    armed" and "this store was disarmed by somebody" look identical in the
    audit trail. They are different facts.
    """
    with state.transaction(connection):
        connection.execute(CREATE_ARM_STATE)


def read_arm_state(connection: sqlite3.Connection) -> ArmState:
    """The durable arm state, failing closed on every unreadable answer.

    A missing table, an unreadable row, a value this build does not recognise -
    each returns a DISARMED state that says which, rather than raising. The
    caller's question is "may I mutate", and for that question every one of
    those is the same answer.
    """
    try:
        row = connection.execute(
            "SELECT state, reason, source, changed_at, code_sha FROM live_arm_state WHERE id = 1"
        ).fetchone()
    except sqlite3.Error as error:
        return ArmState(
            state=STATE_DISARMED,
            reason=f"The live arm state could not be read ({error}), so it is not armed.",
            source=SOURCE_DEFAULT,
            changed_at=None,
        )
    if row is None:
        return UNARMED_DEFAULT
    recorded = str(row[0])
    if recorded not in (STATE_ARMED, STATE_DISARMED):
        return ArmState(
            state=STATE_DISARMED,
            reason=(
                f"The live arm state holds the unrecognised value {recorded!r}. A state this "
                "build cannot interpret is not permission."
            ),
            source=SOURCE_DEFAULT,
            changed_at=None,
        )
    changed_at: datetime | None
    try:
        changed_at = state.from_utc_text(str(row[3]))
    except Exception:  # noqa: BLE001 - an unreadable timestamp is not a reason to refuse a read
        changed_at = None
    return ArmState(
        state=recorded,
        reason=str(row[1]),
        source=str(row[2]),
        changed_at=changed_at,
        code_sha=str(row[4]) if row[4] is not None else None,
    )


def environment_arm_gate_open() -> bool:
    """Whether the environment half of the two-gate arm is open.

    Closed unless the variable is exactly `LIVE_ARMED_VALUE`. An unset, empty
    or unrecognized value is not an error - it is a closed gate, which is the
    safe reading of an ambiguous configuration.
    """
    return os.environ.get(LIVE_ARMED_ENV, "").strip() == LIVE_ARMED_VALUE


def require_armed(connection: sqlite3.Connection) -> ArmState:
    """Raise unless **both** gates are open. The one function a mutation asks.

    Called immediately before a broker mutation rather than once at startup,
    because the point of a fast off switch is that it takes effect on the next
    decision and not on the next restart. Reading one row per order is cheap;
    a switch that only mattered at boot would not be a kill switch.

    Returns the state on success so a caller can log which arming it acted
    under - and, more usefully, when that arming happened.
    """
    if not environment_arm_gate_open():
        raise LiveDisarmedError(
            f"{LIVE_DISARMED_BANNER}: the environment gate {LIVE_ARMED_ENV} is not set to "
            f"{LIVE_ARMED_VALUE!r}, so this host has not been provisioned to submit "
            "real-money orders. Nothing was submitted."
        )
    current = read_arm_state(connection)
    if not current.armed:
        raise LiveDisarmedError(f"{LIVE_DISARMED_BANNER}: {current.reason} Nothing was submitted.")
    return current


def arm_live_trading(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    reason: str,
    confirmation: str | None,
    source: str = SOURCE_OPERATOR,
    code_sha: str | None = None,
) -> ArmState:
    """Record the durable half of the arm. Deliberate, and never a side effect.

    The confirmation token is checked here rather than by the caller so that
    every path to an armed row goes through the same check, including a future
    one nobody has written yet. It arms the *database*; the environment gate is
    a separate provisioning act and this function neither reads nor can open
    it, which is what keeps the two gates independent.

    Submits nothing. Arming is permission to mutate later, under every other
    gate this system has - reconciliation, account identity, the market clock,
    the risk policy, the unresolved-intent check. It is not itself an action.
    """
    if confirmation != ARM_CONFIRMATION_TOKEN:
        raise LiveDisarmedError(
            f"Arming real-money trading requires the exact confirmation "
            f"{ARM_CONFIRMATION_TOKEN}. Nothing was changed and nothing was submitted."
        )
    if not reason.strip():
        raise LiveDisarmedError(
            "Arming requires a reason. An armed account with no recorded reason is a "
            "state nobody can audit later. Nothing was changed."
        )
    return _write_state(
        connection,
        state_value=STATE_ARMED,
        reason=reason.strip(),
        source=source,
        now=now,
        code_sha=code_sha,
        event_type=EVENT_ARMED,
    )


def disarm_live_trading(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    reason: str,
    source: str = SOURCE_OPERATOR,
    code_sha: str | None = None,
) -> ArmState:
    """Record DISARMED. No token, no ceremony, no precondition.

    The safe direction is deliberately frictionless: an operator watching
    something go wrong, and the runtime itself reacting to an anomaly, must
    both be able to reach it without a password, a flag, or a second thought.

    It deletes nothing. Positions, intents, orders, fills and history are all
    left exactly as they are - a disarmed system is one that has stopped
    *adding*, not one that has thrown its record away.
    """
    return _write_state(
        connection,
        state_value=STATE_DISARMED,
        reason=reason.strip() or "Disarmed without a stated reason.",
        source=source,
        now=now,
        code_sha=code_sha,
        event_type=EVENT_DISARMED,
    )


def _write_state(
    connection: sqlite3.Connection,
    *,
    state_value: str,
    reason: str,
    source: str,
    now: datetime,
    code_sha: str | None,
    event_type: str,
) -> ArmState:
    """Write the row and its audit event in one transaction.

    Together or not at all: a switch that moved without an audit event, or an
    event describing a move that did not commit, would each be a record an
    operator could reasonably act on and be wrong.
    """
    create_arm_state_table(connection)
    stamped = state.to_utc_text(now, "changed_at")
    with state.transaction(connection):
        connection.execute(
            "INSERT INTO live_arm_state (id, state, reason, source, changed_at, code_sha)"
            " VALUES (1, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET state = excluded.state, reason = excluded.reason,"
            " source = excluded.source, changed_at = excluded.changed_at,"
            " code_sha = excluded.code_sha",
            (state_value, reason, source, stamped, code_sha),
        )
        state.record_system_event(
            connection,
            event_timestamp=now,
            event_type=event_type,
            message=f"Live trading {state_value} by {source}: {reason}",
        )
    return ArmState(
        state=state_value,
        reason=reason,
        source=source,
        changed_at=now,
        code_sha=code_sha,
    )


__all__ = [
    "ARM_CONFIRMATION_TOKEN",
    "CREATE_ARM_STATE",
    "EVENT_ARMED",
    "EVENT_DISARMED",
    "LIVE_ARMED_ENV",
    "LIVE_ARMED_VALUE",
    "LIVE_DISARMED_BANNER",
    "SOURCE_DEFAULT",
    "SOURCE_OPERATOR",
    "SOURCE_RUNTIME",
    "STATE_ARMED",
    "STATE_DISARMED",
    "UNARMED_DEFAULT",
    "ArmState",
    "LiveDisarmedError",
    "arm_live_trading",
    "create_arm_state_table",
    "disarm_live_trading",
    "environment_arm_gate_open",
    "read_arm_state",
    "require_armed",
]
