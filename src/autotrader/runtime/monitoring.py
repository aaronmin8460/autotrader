"""C9: heartbeat state and structured logging. The whole monitoring surface.

Two things, both deliberately small:

**A heartbeat.** One mutable object the runtime updates and one frozen
snapshot of it that anything else may read. It answers the questions an
operator actually asks of a process that is supposed to be awake every fifteen
minutes: when did it start, when did it last try, when did it last succeed,
which bar has each symbol reached, is it allowed to trade and if not why not,
and what went wrong last.

**Structured log lines.** `event=... key=value` on the standard library's
`logging`, written to whatever handler the process configured - stdout under
systemd, captured under pytest. There is no monitoring dependency, no paid
agent, no Telegram, no Slack, no webhook, and no log file this repository
insists on owning: a process that writes to stdout is one journald already
knows how to collect.

**Nothing here ever sees a credential.** The runtime passes symbols,
timestamps, decisions, and reason codes. API keys and secrets are read inside
the execution boundary, are never returned from it, and are never arguments to
anything in this module - so there is no line for them to leak through.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

#: The one logger name this package logs under, so an operator can raise or
#: silence the whole runtime with a single logging configuration line.
LOGGER_NAME = "autotrader.runtime"

#: Rendered in a key=value field when a value is not yet known. Chosen over an
#: empty string so a missing value is visible in a log line rather than blank.
NONE_FIELD = "-"


class RuntimeState(Enum):
    """Where the runtime is in its own lifecycle.

    `TRADING_PAUSED` is terminal for submission: it is entered on an ambiguous
    broker outcome and nothing in this branch may leave it, because only
    reconciliation can establish what actually happened at the broker.
    """

    STARTING = "STARTING"
    RUNNING = "RUNNING"
    TRADING_PAUSED = "TRADING_PAUSED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


def get_logger(name: str = LOGGER_NAME) -> logging.Logger:
    """The runtime logger. Handlers and level belong to the process, not here."""
    return logging.getLogger(name)


def _render(value: object) -> str:
    """One field value as a single parseable token."""
    if value is None:
        return NONE_FIELD
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    text = str(value)
    if not text:
        return NONE_FIELD
    if any(character.isspace() for character in text) or '"' in text:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def format_event(event: str, **fields: object) -> str:
    """Render one structured line: ``event=cycle_started symbol=BTC/USD ...``.

    Field order is the caller's order, so related lines stay diffable. Values
    containing whitespace are quoted, which keeps a message field from turning
    one line into several apparent fields.
    """
    parts = [f"event={_render(event)}"]
    parts.extend(f"{key}={_render(value)}" for key, value in fields.items())
    return " ".join(parts)


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: object,
) -> None:
    """Emit one structured runtime event."""
    logger.log(level, format_event(event, **fields))


@dataclass(frozen=True)
class HeartbeatSnapshot:
    """An immutable read of the runtime's health at one instant."""

    state: RuntimeState
    started_at: datetime | None
    last_cycle_started_at: datetime | None
    last_successful_cycle_at: datetime | None
    last_processed_bars: Mapping[str, datetime | None]
    paper_execution_enabled: bool
    execution_disabled_reason: str | None
    startup_safety_code: str
    reconciliation_status: str | None
    cycles_started: int
    cycles_completed: int
    orders_submitted: int
    api_calls_total: int
    api_calls_last_cycle: int
    last_error: str | None

    def as_fields(self) -> dict[str, object]:
        """The snapshot as log fields, one per line of `event=heartbeat`."""
        fields: dict[str, object] = {
            "state": self.state,
            "started_at": self.started_at,
            "last_cycle_started": self.last_cycle_started_at,
            "last_successful_cycle": self.last_successful_cycle_at,
        }
        for symbol, timestamp in self.last_processed_bars.items():
            fields[f"last_bar[{symbol}]"] = timestamp
        fields.update(
            {
                "paper_execution_enabled": self.paper_execution_enabled,
                "execution_disabled_reason": self.execution_disabled_reason,
                "startup_safety": self.startup_safety_code,
                "reconciliation_status": self.reconciliation_status,
                "cycles_started": self.cycles_started,
                "cycles_completed": self.cycles_completed,
                "orders_submitted": self.orders_submitted,
                "api_calls_total": self.api_calls_total,
                "api_calls_last_cycle": self.api_calls_last_cycle,
                "last_error": self.last_error,
            }
        )
        return fields


@dataclass
class Heartbeat:
    """The runtime's live health record.

    Mutable because it is updated in place by the loop that owns it; readers
    take a `snapshot()` instead of holding this object, so a status line can
    never disagree with itself halfway through being rendered.
    """

    state: RuntimeState = RuntimeState.STARTING
    started_at: datetime | None = None
    last_cycle_started_at: datetime | None = None
    last_successful_cycle_at: datetime | None = None
    last_processed_bars: dict[str, datetime | None] = field(default_factory=dict)
    paper_execution_enabled: bool = False
    execution_disabled_reason: str | None = None
    startup_safety_code: str = "UNRESOLVED"
    #: The C8 status the startup answer came from, or None when no pass ran.
    reconciliation_status: str | None = None
    cycles_started: int = 0
    cycles_completed: int = 0
    orders_submitted: int = 0
    api_calls_total: int = 0
    api_calls_last_cycle: int = 0
    last_error: str | None = None

    def snapshot(self) -> HeartbeatSnapshot:
        """A frozen copy of the current health."""
        return HeartbeatSnapshot(
            state=self.state,
            started_at=self.started_at,
            last_cycle_started_at=self.last_cycle_started_at,
            last_successful_cycle_at=self.last_successful_cycle_at,
            last_processed_bars=dict(self.last_processed_bars),
            paper_execution_enabled=self.paper_execution_enabled,
            execution_disabled_reason=self.execution_disabled_reason,
            startup_safety_code=self.startup_safety_code,
            reconciliation_status=self.reconciliation_status,
            cycles_started=self.cycles_started,
            cycles_completed=self.cycles_completed,
            orders_submitted=self.orders_submitted,
            api_calls_total=self.api_calls_total,
            api_calls_last_cycle=self.api_calls_last_cycle,
            last_error=self.last_error,
        )


__all__ = [
    "LOGGER_NAME",
    "NONE_FIELD",
    "Heartbeat",
    "HeartbeatSnapshot",
    "RuntimeState",
    "format_event",
    "get_logger",
    "log_event",
]
