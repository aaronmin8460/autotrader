"""C9: the startup-safety boundary, and the C8 reconciliation behind it.

One question, asked once, at process start:

    "Is it safe for this process to trade?"

**The answer now comes from Phase 8.** `startup_safety_from_reconciliation`
builds the zero-argument callable the runtime asks, and that callable runs the
real `reconcile_paper_state` pass and reports its `safe_to_trade`. There is no
second copy of reconciliation here: this module maps one vocabulary onto
another and does nothing else. It reads the broker only through C8, which reads
it only through C7.

**The production default is still not "yes".** A runtime constructed without a
check keeps `unresolved_startup_safety`, which answers `UNRESOLVED` and holds
the gate shut. A process that assumes its local view of the world survived the
last shutdown is exactly how a duplicate position is created, so an unchecked
answer must never open the gate.

**Every non-green reconciliation closes the gate, and every one of them says
which.** `UNRESOLVED` and `FAILED` both map to `UNSAFE`, because from the
runtime's side they mean the same thing - do not trade - and the reason string
carries the reconciliation status so an operator can tell an ambiguous order
from an unreadable broker without reading source.

The runtime still observes while unsafe: it fetches bars, validates them, runs
the strategy, records signals, and logs. It just does not trade.

**This is not a plugin framework.** There is no registry, no discovery, no
entry point, and no configuration that names a class to import. There is a
result type and a zero-argument callable that returns one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from autotrader.reconciliation import (
    ReconciliationError,
    ReconciliationResult,
    reconcile_paper_state,
)

#: Stable, machine-readable startup-safety codes.
#:
#: `SAFE`       something checked, and trading may proceed.
#: `UNRESOLVED` nobody checked. The default when no check is supplied.
#: `UNSAFE`     something checked, and trading may **not** proceed.
#:
#: `UNRESOLVED` and `UNSAFE` both close the gate. They are kept distinct
#: because "we could not tell" and "we looked and the answer is no" call for
#: different operator responses, and a status line that conflates them is not
#: a status line. With reconciliation wired in, production sees `SAFE` or
#: `UNSAFE`; `UNRESOLVED` means no check ran at all.
STARTUP_SAFETY_SAFE = "SAFE"
STARTUP_SAFETY_UNRESOLVED = "UNRESOLVED"
STARTUP_SAFETY_UNSAFE = "UNSAFE"

STARTUP_SAFETY_CODES: tuple[str, ...] = (
    STARTUP_SAFETY_SAFE,
    STARTUP_SAFETY_UNRESOLVED,
    STARTUP_SAFETY_UNSAFE,
)

#: The banner an operator must be able to find in a log or on a terminal when
#: startup reconciliation did not come back green. Written once, here, so the
#: runtime, the heartbeat and the CLI all say the same words.
RECONCILIATION_NOT_SAFE_BANNER = "RECONCILIATION NOT SAFE - TRADING DISABLED"


@dataclass(frozen=True)
class StartupSafetyResult:
    """The answer to the one startup question, plus why.

    `safe_to_trade` is the whole decision; `code` and `message` exist so an
    operator reading a heartbeat can tell an unresolved startup from a refused
    one without reading source. `reconciliation` is the C8 result the answer
    came from, when one was produced - it is evidence, never the decision:
    `safe_to_trade` is what the runtime reads.
    """

    safe_to_trade: bool
    code: str
    message: str
    reconciliation: ReconciliationResult | None = None

    def __post_init__(self) -> None:
        if self.code not in STARTUP_SAFETY_CODES:
            raise ValueError(
                f"code must be one of {', '.join(STARTUP_SAFETY_CODES)}, got {self.code!r}."
            )
        if self.safe_to_trade != (self.code == STARTUP_SAFETY_SAFE):
            raise ValueError(
                f"safe_to_trade={self.safe_to_trade} contradicts code {self.code!r}; a "
                "startup-safety result that disagrees with itself must not exist."
            )
        if self.reconciliation is not None and self.safe_to_trade != (
            self.reconciliation.safe_to_trade
        ):
            raise ValueError(
                f"safe_to_trade={self.safe_to_trade} contradicts the reconciliation "
                f"result it was derived from ({self.reconciliation.status.value}); the "
                "startup gate must never disagree with the pass it quotes."
            )

    @property
    def reconciliation_status(self) -> str | None:
        """The C8 status behind this answer, when one was produced."""
        return None if self.reconciliation is None else self.reconciliation.status.value


#: What the runtime calls at startup. Zero arguments, one result.
StartupSafetyCheck = Callable[[], StartupSafetyResult]


def unresolved_startup_safety() -> StartupSafetyResult:
    """The answer when nothing checked: nobody knows, so nobody trades.

    Deliberately not a stub that returns True with a TODO beside it. It is the
    value a runtime constructed without a check uses, and it keeps broker
    submission off. Production supplies
    `startup_safety_from_reconciliation` instead.
    """
    return StartupSafetyResult(
        safe_to_trade=False,
        code=STARTUP_SAFETY_UNRESOLVED,
        message=(
            "Startup safety is unresolved: no reconciliation pass was run, so no "
            "process can know whether local state survived the last shutdown. Paper "
            "order submission stays disabled; observation continues."
        ),
    )


def startup_safety_from_reconciliation_result(
    result: ReconciliationResult,
) -> StartupSafetyResult:
    """Map one finished C8 pass onto the runtime's startup answer.

    The whole mapping, and the only place it is written:

    | Reconciliation | Startup safety |
    | -------------- | -------------- |
    | `CLEAN`        | `SAFE`         |
    | `REPAIRED`     | `SAFE`         |
    | `UNRESOLVED`   | `UNSAFE`       |
    | `FAILED`       | `UNSAFE`       |

    `REPAIRED` is safe, deliberately. A local snapshot that disagreed with the
    broker and was rewritten *from the broker* is now correct, and refusing to
    trade afterwards would let one stale historical row block the runner
    permanently for a difference that has been resolved.

    The decision is read off `result.safe_to_trade` rather than re-derived from
    `status`, so this function cannot drift from C8's own rule. The table above
    describes that rule; it does not re-implement it.
    """
    if result.safe_to_trade:
        return StartupSafetyResult(
            safe_to_trade=True,
            code=STARTUP_SAFETY_SAFE,
            message=(
                f"Startup reconciliation is {result.status.value}: local state matches "
                f"verified broker truth ({result.orders_checked} order(s) and "
                f"{result.positions_checked} position(s) checked, "
                f"{result.repaired_count} repaired). Paper order submission is "
                "permitted subject to the paper gates."
            ),
            reconciliation=result,
        )

    blocking = result.blocking_issues()
    detail = blocking[0].detail if blocking else "no blocking detail was recorded"
    return StartupSafetyResult(
        safe_to_trade=False,
        code=STARTUP_SAFETY_UNSAFE,
        message=(
            f"{RECONCILIATION_NOT_SAFE_BANNER}. Startup reconciliation is "
            f"{result.status.value} with {result.unresolved_count} blocking issue(s); "
            f"first: {detail}. No paper order will be submitted by this process; "
            "observation continues."
        ),
        reconciliation=result,
    )


def startup_safety_from_reconciliation(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> StartupSafetyCheck:
    """Build the startup check that runs the real C8 pass against Alpaca paper.

    Returns the zero-argument callable the runtime asks once at start. Calling
    it runs `reconcile_paper_state` against the paper broker and reports what
    that pass concluded.

    **Not `dry_run`.** Startup is exactly when a repairable difference should
    be repaired: a runtime that observed a stale local snapshot, declined to
    fix it and then declined to trade because of it would be stuck for a reason
    it was capable of resolving. The pass writes only local rows, and it cannot
    place an order in any branch.

    **Nothing is cached.** Each call runs a fresh pass, so a new process gets a
    new answer. A previous run's green result is not evidence about this
    process: the whole point of asking is that the world may have changed while
    nothing was watching.

    **A pass that raises is unsafe, not fatal.** C8 already converts an
    unreachable or unprovable broker into a `FAILED` *result*; a raise on top
    of that is a bug or a caller error, and the honest response is to close the
    gate and say so rather than to let a traceback decide whether trading is
    allowed.
    """

    def check() -> StartupSafetyResult:
        try:
            result = reconcile_paper_state(connection, now=now)
        except ReconciliationError as error:
            return StartupSafetyResult(
                safe_to_trade=False,
                code=STARTUP_SAFETY_UNSAFE,
                message=(
                    f"{RECONCILIATION_NOT_SAFE_BANNER}. Startup reconciliation could "
                    f"not be run: {error}. No paper order will be submitted by this "
                    "process; observation continues."
                ),
            )
        return startup_safety_from_reconciliation_result(result)

    return check


__all__ = [
    "RECONCILIATION_NOT_SAFE_BANNER",
    "STARTUP_SAFETY_CODES",
    "STARTUP_SAFETY_SAFE",
    "STARTUP_SAFETY_UNRESOLVED",
    "STARTUP_SAFETY_UNSAFE",
    "StartupSafetyCheck",
    "StartupSafetyResult",
    "startup_safety_from_reconciliation",
    "startup_safety_from_reconciliation_result",
    "unresolved_startup_safety",
]
