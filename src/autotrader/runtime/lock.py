"""C9: the single-instance lock. Two runners on one database is duplicate trading.

Two copies of the 24/7 runner pointed at the same SQLite file would each wake
on the same boundary, each see the same completed bar, each hold their own
in-process checkpoint - which knows nothing about the other's - and each
submit. The in-process duplicate guard cannot see across processes, so the
protection has to be at the process boundary.

**A real OS lock, not a PID file.** A PID file records an intention; it does
not enforce anything, it is left behind by a crash, and the pid it names may
have been reused by something else entirely. `flock` is held by the open file
description and is released by the kernel when the process dies for any
reason - including `SIGKILL` and a power loss - so a stale lock file cannot
wedge the next start.

Local and dependency-free on purpose: no Redis, no lock service, no network.
The thing being protected is one local database file.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType

#: The suffix appended to the database path to name its lock.
LOCK_SUFFIX = ".runtime.lock"

#: The default lock scope, kept nameless so the crypto runner's lock file is
#: exactly what it always was. A named scope produces
#: ``<database>.<scope>.runtime.lock`` instead.
DEFAULT_LOCK_SCOPE: str | None = None


class RuntimeLockError(Exception):
    """The runtime lock could not be taken. Another runner most likely holds it."""


def lock_path_for(database: str | Path, *, scope: str | None = DEFAULT_LOCK_SCOPE) -> Path:
    """The lock file that guards one runner's state, within one database.

    Derived from the database path rather than configured separately, so the
    lock always guards the state it is supposed to guard: two runners can only
    collide if they share a database, and sharing a database means sharing this
    file.

    `scope` is what lets two *different* products run as two processes against
    the same account without either blocking the other. Later deployment has a
    crypto service and an equity service; they share one Alpaca account, and
    they must not share a lock, because a lock that stopped the equity runner
    from starting while the crypto runner was up would be enforcing something
    nobody wanted. Two runners of the *same* product still collide, which is
    the property that actually prevents duplicate trading, because they resolve
    to the same file.

    The unscoped form is unchanged and is still the crypto runner's lock. This
    is a name, not a permission: it does not weaken account-level order safety,
    which lives in the duplicate preflight, the per-symbol checkpoint, and the
    `client_order_id` - none of which this parameter can reach.
    """
    path = Path(database)
    suffix = LOCK_SUFFIX if scope is None else f".{scope}{LOCK_SUFFIX}"
    return path.with_name(path.name + suffix)


class RuntimeLock:
    """An exclusive, non-blocking lock on one runtime's local state.

    Acquire fails immediately rather than waiting: a second runner is an
    operator error to report, not a queue to join.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._descriptor: int | None = None

    @property
    def held(self) -> bool:
        """Whether this object currently holds the lock."""
        return self._descriptor is not None

    def acquire(self) -> None:
        """Take the lock, or raise `RuntimeLockError`."""
        if self._descriptor is not None:
            raise RuntimeLockError(f"This process already holds {self.path}.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            raise RuntimeLockError(
                f"Another runtime already holds {self.path}. Refusing to start a "
                "second runner against the same local state: two runners would process "
                f"the same completed bar and submit twice ({error.strerror or error})."
            ) from None
        # The pid is written for a human reading the file, never read back as
        # authority - the lock itself is the authority.
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()}\n".encode())
        self._descriptor = descriptor

    def release(self) -> None:
        """Release the lock and remove the file. Safe to call when not held."""
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            self.path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - the lock still releases below
            pass
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def __enter__(self) -> RuntimeLock:
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


__all__ = [
    "DEFAULT_LOCK_SCOPE",
    "LOCK_SUFFIX",
    "RuntimeLock",
    "RuntimeLockError",
    "lock_path_for",
]
