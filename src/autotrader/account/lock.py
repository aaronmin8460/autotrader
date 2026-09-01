"""The account execution lock: one order decision at a time, account-wide.

The crypto runtime and the equity runtime hold **different** runtime locks and
are meant to run at the same time. That stays true. What must not overlap is
the short stretch in the middle of each of them where an order is decided and
sent.

**The race this closes.** Total account exposure is capped at 30%. Suppose 28%
is used and both runtimes wake with a signal. Each reads the account, each sees
2% of headroom, each sizes an order into it, and each submits. Neither did
anything wrong on its own; together they put the account at 32%. The exposure
figure was accurate when it was read and stale by the time it was used, and no
amount of care inside either process can fix that - the two reads have to be
serialized against each other.

**Scope: the critical section, not the cycle.** This lock is held across
"read the account, decide, persist the intent, submit once, record the reply"
and released immediately after. It is *not* held across a fifteen-minute wait,
a market-data fetch, or a strategy evaluation. A lock held for a whole runtime
cycle would make the two services take turns rather than run concurrently,
which is neither wanted nor needed: only the account arithmetic conflicts.

**Blocking, unlike the runtime lock, and that difference is deliberate.**
`RuntimeLock` fails immediately, because a second copy of the *same* runtime is
an operator error to report. Here, contention is the normal expected case - two
different services legitimately reaching their critical sections at once - so
the second one waits its turn and then re-reads the account it was going to
size against. The wait is bounded: an acquire that cannot be satisfied inside
the timeout raises rather than waiting forever, because a decision belongs to
the bar that produced it and a lock held by something wedged must not turn into
an order sent minutes late.

Built on the same `fcntl` primitive as `RuntimeLock` - a real OS lock released
by the kernel when the holder dies, including on `SIGKILL`. No Redis, no lock
service, no network.
"""

from __future__ import annotations

import errno
import fcntl
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType

#: The suffix appended to the database path to name the shared account lock.
#: One database is one account's operational state, so deriving the lock from
#: the database path is what makes two services pointed at the same account
#: contend and two pointed at different accounts not.
ACCOUNT_LOCK_SUFFIX = ".account.execution.lock"

#: How long a caller waits for the other service's critical section before
#: giving up. Comfortably longer than a critical section - a handful of broker
#: calls - and far shorter than the fifteen-minute cadence, so a wedged holder
#: surfaces as a failed cycle rather than as a late order.
DEFAULT_ACCOUNT_LOCK_TIMEOUT_SECONDS = 30.0

#: How often a waiter retries. Small enough to be invisible next to a broker
#: round trip, large enough not to spin.
_POLL_INTERVAL_SECONDS = 0.02


class AccountExecutionLockError(Exception):
    """The shared account execution lock could not be taken in time.

    Fails the action closed. It is never converted into "submit anyway", and
    the caller must not retry it later: the signal belonged to a completed bar,
    and re-deciding it minutes on is a stale trade.
    """


def account_lock_path_for(database: str | Path) -> Path:
    """The shared account execution lock file for one operational database.

    Derived from the database path for the same reason the runtime lock is:
    the lock then necessarily guards the account whose state that file holds,
    and it cannot be pointed somewhere else by configuration.
    """
    path = Path(database)
    return path.with_name(path.name + ACCOUNT_LOCK_SUFFIX)


def database_path_of(connection: sqlite3.Connection) -> Path | None:
    """The file one connection's `main` database lives in, or None.

    Asked of the connection rather than passed alongside it, so the lock
    necessarily guards the database the caller is actually writing to and there
    is no second place for the two to disagree.

    None means there is no file: an in-memory database, which by definition no
    other process can open, and which therefore has nothing to serialize
    against.
    """
    try:
        rows = connection.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:  # pragma: no cover - a dead connection fails later anyway
        return None
    for row in rows:
        name, filename = row[1], row[2]
        if name == "main":
            return Path(filename) if filename else None
    return None  # pragma: no cover - every connection has a main database


class AccountExecutionLock:
    """An exclusive, bounded-wait lock over one account's order-decision path.

    Re-entrant within a single object so a caller already inside the critical
    section does not deadlock itself on a nested guard; the underlying `flock`
    is taken once and released when the outermost holder exits.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        timeout_seconds: float = DEFAULT_ACCOUNT_LOCK_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        read_only: bool = False,
    ) -> None:
        """`read_only` takes the same exclusive kernel lock through a read-only
        descriptor on a file that must already exist.

        This is how one service contends on *another* service's lock file: the
        peer's store directory is mounted read-only into this one's sandbox, so
        the file can be opened but never created, truncated, or written. The
        `flock` primitive does not care - an exclusive lock on a read-only
        descriptor excludes the peer's writable one identically. A file that is
        missing or unreadable **fails closed**: "I could not contend with the
        other service" and "there is no other service" are different answers,
        and only one of them may precede an order.
        """
        if timeout_seconds < 0:
            raise ValueError(f"timeout_seconds must not be negative, got {timeout_seconds}.")
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self.read_only = read_only
        self._sleep = sleep
        self._monotonic = monotonic
        self._descriptor: int | None = None
        self._depth = 0

    @property
    def held(self) -> bool:
        """Whether this object currently holds the lock."""
        return self._descriptor is not None

    def acquire(self) -> None:
        """Take the lock, waiting up to `timeout_seconds`, or raise.

        Polls a non-blocking `flock` rather than making one blocking call:
        `flock` has no timeout of its own, and a wait that could not be bounded
        would be a wait that could outlive the bar its decision belongs to.
        """
        if self._descriptor is not None:
            self._depth += 1
            return

        if self.read_only:
            try:
                descriptor = os.open(self.path, os.O_RDONLY)
            except OSError as error:
                raise AccountExecutionLockError(
                    f"The peer account execution lock {self.path} could not be opened "
                    f"read-only ({error.strerror or error}). Cross-service order "
                    "serialization cannot be proven without it, so nothing was "
                    "submitted. The peer service creates this file on its first "
                    "execution; a missing file means the peer's store path is wrong "
                    "or its boundary has never run."
                ) from None
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        deadline = self._monotonic() + self.timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    os.close(descriptor)
                    raise AccountExecutionLockError(
                        f"The account execution lock {self.path} could not be taken: "
                        f"{error.strerror or error}. Nothing was submitted."
                    ) from None
                if self._monotonic() >= deadline:
                    os.close(descriptor)
                    raise AccountExecutionLockError(
                        f"The account execution lock {self.path} was still held by "
                        f"another service after {self.timeout_seconds:g}s. This "
                        "decision belongs to the bar that produced it, so it is "
                        "abandoned rather than submitted late. Nothing was submitted."
                    ) from None
                self._sleep(_POLL_INTERVAL_SECONDS)
                continue
            break

        if not self.read_only:
            # Written for a human reading the file, never read back as
            # authority - the lock itself is the authority. A read-only holder
            # cannot write and does not need to: the peer's own PID note is
            # simply left in place.
            os.ftruncate(descriptor, 0)
            os.write(descriptor, f"{os.getpid()}\n".encode())
        self._descriptor = descriptor
        self._depth = 1

    def release(self) -> None:
        """Release the lock. Safe to call when not held; honours re-entry."""
        if self._descriptor is None:
            return
        if self._depth > 1:
            self._depth -= 1
            return
        descriptor = self._descriptor
        self._descriptor = None
        self._depth = 0
        # The file is deliberately *not* unlinked. Unlinking it lets a waiter
        # that already opened the old inode take a lock on a file nobody else
        # can see any more, which is two holders of a lock that is supposed to
        # have one. An empty lock file left on disk costs nothing.
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> AccountExecutionLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Always release, including when the body raised."""
        self.release()


class CompositeAccountLock:
    """Several account execution locks held as one, in one fixed order.

    Exists for the split-store deployment: one broker account, two operational
    stores, and therefore two lock files that both mean "this account's order
    path". A runtime that must exclude the *other* service takes the peer's
    lock and then its own, always in the order the constructor received them,
    and releases in reverse. The peer never takes this side's lock, so the
    fixed order cannot form a cycle.

    Acquisition is all-or-nothing: a failure on the second lock releases the
    first before the error propagates, so a refused critical section leaves
    nothing held.
    """

    def __init__(self, locks: tuple[AccountExecutionLock, ...]) -> None:
        if not locks:
            raise ValueError("CompositeAccountLock needs at least one lock.")
        self._locks = tuple(locks)
        self._depth = 0

    @property
    def held(self) -> bool:
        """Whether this object currently holds every constituent lock."""
        return self._depth > 0

    @property
    def locks(self) -> tuple[AccountExecutionLock, ...]:
        return self._locks

    def acquire(self) -> None:
        if self._depth > 0:
            self._depth += 1
            return
        taken: list[AccountExecutionLock] = []
        try:
            for lock in self._locks:
                lock.acquire()
                taken.append(lock)
        except BaseException:
            for lock in reversed(taken):
                lock.release()
            raise
        self._depth = 1

    def release(self) -> None:
        if self._depth == 0:
            return
        if self._depth > 1:
            self._depth -= 1
            return
        self._depth = 0
        for lock in reversed(self._locks):
            lock.release()

    def __enter__(self) -> CompositeAccountLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


@contextmanager
def account_execution_lock(
    database: str | Path | None,
    *,
    lock: AccountExecutionLock | None = None,
    timeout_seconds: float = DEFAULT_ACCOUNT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[AccountExecutionLock | None]:
    """Hold the account execution lock around a critical section.

    `lock` lets a caller pass an already-constructed lock - a runtime holds one
    for its lifetime rather than building one per order, and a test injects one
    with a short timeout and a fake clock.

    A `None` database with no `lock` yields `None` and locks nothing. That is
    the in-memory case: a test database with no path on disk has no other
    process to contend with, and inventing a lock file for it would be locking
    against nobody. Every path that reaches a real broker has a real database
    file behind it.
    """
    if lock is None:
        if database is None:
            yield None
            return
        lock = AccountExecutionLock(
            account_lock_path_for(database), timeout_seconds=timeout_seconds
        )
    with lock:
        yield lock


__all__ = [
    "ACCOUNT_LOCK_SUFFIX",
    "DEFAULT_ACCOUNT_LOCK_TIMEOUT_SECONDS",
    "AccountExecutionLock",
    "AccountExecutionLockError",
    "CompositeAccountLock",
    "account_execution_lock",
    "account_lock_path_for",
    "database_path_of",
]
