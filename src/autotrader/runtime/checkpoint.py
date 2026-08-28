"""C8: the in-process per-symbol processed-bar checkpoint.

One completed bar must produce at most one strategy action per symbol per
process. Sleep timing is not what guarantees that: a provider can repeat the
newest completed bar across two fetches, a cycle can overrun into the next
boundary, and `--once` can be run twice in quick succession. So the runtime
records what it has already acted on and checks it explicitly.

**Scope: this process only.** Cross-restart exactly-once recovery is Phase 8's
and is deliberately not invented here - a checkpoint that survived a restart
without being reconciled against the broker would be a *claim* about the
broker's state, which is precisely the claim only reconciliation may make.

The interface is separate from its implementation for exactly that reason. The
runtime depends on `ProcessedBarCheckpoint`; Phase 9 ships
`InMemoryCheckpoint`; the integration gate can supply a durable one without
the runtime changing, and **without this branch touching the SQLite schema**.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from autotrader.runtime.schedule import require_utc


class ProcessedBarCheckpoint(Protocol):
    """What the runtime needs to know about bars it has already acted on."""

    def last_processed(self, symbol: str) -> datetime | None:
        """The newest bar start already processed for `symbol`, if any."""

    def mark_processed(self, symbol: str, bar_timestamp: datetime) -> None:
        """Record `bar_timestamp` as processed for `symbol`."""


class InMemoryCheckpoint:
    """The Phase 9 implementation: a dict that dies with the process.

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


__all__ = ["InMemoryCheckpoint", "ProcessedBarCheckpoint"]
