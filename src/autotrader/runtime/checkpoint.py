"""C9: the per-symbol processed-bar checkpoint, in memory and on disk.

One completed bar must produce at most one strategy action per symbol. Sleep
timing is not what guarantees that: a provider can repeat the newest completed
bar across two fetches, a cycle can overrun into the next boundary, `--once`
can be run twice in quick succession, and a process can be restarted a second
after it died. So the runtime records what it has already acted on and checks
it explicitly.

**Two problems, two mechanisms, and both are needed.** The single-instance lock
(`runtime.lock`) stops two runners existing at once. This checkpoint stops one
runner - or its replacement after a crash - acting twice on the same bar. A
lock says "only one of you"; a checkpoint says "and that one has already done
this bar". Neither substitutes for the other.

**The durable implementation is the production one.** `SqliteCheckpoint`
records the claim in `runtime_checkpoints` (schema v5) and commits it before
returning, so the claim outlives the process that made it.
`InMemoryCheckpoint` remains for tests and for a caller that genuinely wants a
process-scoped guard.

**The safety preference is explicit: miss a trade rather than duplicate one.**
A bar is claimed *before* it may cause a decision or a submission, so a crash
between the claim and the broker call loses that bar permanently. That is the
side of the trade this system chooses. The other side - claiming after
submission - would let a restart place a second order for a crossover that
happened once, and no reconciliation pass can un-place an order.

This is not exactly-once execution, and nothing here pretends it is. It is
at-most-once, which is achievable locally with one SQLite file and is the
property actually worth having.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Protocol

from autotrader.runtime.schedule import require_utc
from autotrader.state import sqlite as state


class CheckpointNotDurableError(Exception):
    """A bar claim could not be made durable, so the bar must not be acted on.

    Raised rather than returned, and deliberately not an `ExecutionError`: the
    runtime classifies an unrecognised failure as fatal, which is the right
    answer here. A claim that is not on disk cannot stop a restarted process
    re-deciding the same bar, and acting on a bar whose claim might still be
    rolled back is the duplicate-trade direction.
    """


class ProcessedBarCheckpoint(Protocol):
    """What the runtime needs to know about bars it has already acted on."""

    def last_processed(self, symbol: str) -> datetime | None:
        """The newest bar start already processed for `symbol`, if any."""

    def mark_processed(self, symbol: str, bar_timestamp: datetime) -> None:
        """Record `bar_timestamp` as processed for `symbol`."""


class InMemoryCheckpoint:
    """A dict that dies with the process. For tests and process-scoped use.

    Monotonic on purpose. A bar older than the newest one already processed
    never moves the checkpoint backwards, so an out-of-order provider response
    cannot re-open a bar this process has already acted on.
    """

    def __init__(self, initial: dict[str, datetime] | None = None) -> None:
        self._latest: dict[str, datetime] = {}
        for symbol, timestamp in (initial or {}).items():
            self.mark_processed(symbol, timestamp)

    def last_processed(self, symbol: str) -> datetime | None:
        return self._latest.get(symbol)

    def mark_processed(self, symbol: str, bar_timestamp: datetime) -> None:
        moment = require_utc(bar_timestamp, "bar_timestamp")
        current = self._latest.get(symbol)
        if current is None or moment > current:
            self._latest[symbol] = moment

    def as_dict(self) -> dict[str, datetime]:
        """A copy of the current checkpoints, for status reporting."""
        return dict(self._latest)


class SqliteCheckpoint:
    """The production checkpoint: one committed row per symbol, in schema v5.

    `mark_processed` returns only once the claim is committed, and it now
    checks that rather than assuming it: `upsert_runtime_checkpoint` joins an
    enclosing transaction rather than committing inside one, so a caller that
    wrapped this call would get a claim that no other connection can see and
    that a crash would roll back. That is refused. A second connection -
    another process, or a test holding one open - can read every claim this
    method accepts the instant it returns, which is exactly the property that
    makes a restart safe and exactly what a test can assert without trusting
    this docstring.

    Reads are not cached. The database is the claim; a cache in front of it
    would be a second answer that could disagree with the one that matters, and
    two reads per cycle of a one-row-per-symbol table costs nothing.

    Monotonicity is enforced by the storage layer's upsert rather than here, so
    a bar older than the stored claim cannot move it backwards even if this
    class is bypassed.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def last_processed(self, symbol: str) -> datetime | None:
        checkpoint = state.get_runtime_checkpoint(self._connection, symbol)
        return None if checkpoint is None else checkpoint.last_processed_bar_timestamp

    def mark_processed(self, symbol: str, bar_timestamp: datetime) -> None:
        moment = require_utc(bar_timestamp, "bar_timestamp")
        if self._connection.in_transaction:
            raise CheckpointNotDurableError(
                f"The {symbol} claim for {moment.isoformat()} cannot be made durable: "
                "this connection is inside an open transaction, so the claim would be "
                "invisible to any other process and would be rolled back by a crash. "
                "Refusing to claim a bar that a restarted process could then process "
                "again. Nothing was claimed and nothing was decided."
            )
        state.upsert_runtime_checkpoint(
            self._connection,
            symbol=symbol,
            last_processed_bar_timestamp=moment,
            updated_at=datetime.now(UTC),
        )

    def as_dict(self) -> dict[str, datetime]:
        """A copy of the stored checkpoints, for status reporting."""
        return {
            checkpoint.symbol: checkpoint.last_processed_bar_timestamp
            for checkpoint in state.list_runtime_checkpoints(self._connection)
        }


__all__ = [
    "CheckpointNotDurableError",
    "InMemoryCheckpoint",
    "ProcessedBarCheckpoint",
    "SqliteCheckpoint",
]
