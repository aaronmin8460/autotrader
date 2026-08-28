"""C9: the narrow startup-safety boundary the runtime fails closed against.

Phase 8 - reconciliation and crash recovery - is being built separately and is
**not** in this branch. None of it is implemented, imitated, or approximated
here. What is here is the smallest possible seam for it to be connected to
later: one question, asked once, at startup.

    "Is it safe for this process to trade?"

**The production default is not "yes".** It is `UNRESOLVED`, and unresolved
means no broker order is submitted. A long-running process that assumes its
local view of the world survived the last shutdown is exactly how a duplicate
position is created, so before Phase 8 exists the honest answer is that nobody
has checked - and an unchecked answer must never open the gate.

The runtime still observes while unresolved: it fetches bars, validates them,
runs the strategy, records signals, and logs. It just does not trade.

**This is not a plugin framework.** There is no registry, no discovery, no
entry point, and no configuration that names a class to import. There is a
result type and a zero-argument callable that returns one. The integration
gate will pass Phase 8's reconciliation outcome in as that callable; tests pass
a fixed result in the same way.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

#: Stable, machine-readable startup-safety codes.
#:
#: `SAFE`       something checked, and trading may proceed.
#: `UNRESOLVED` nobody checked. The pre-Phase-8 production default.
#: `UNSAFE`     something checked, and trading may **not** proceed.
#:
#: `UNRESOLVED` and `UNSAFE` both close the gate. They are kept distinct
#: because "we could not tell" and "we looked and the answer is no" call for
#: different operator responses, and a status line that conflates them is not
#: a status line.
STARTUP_SAFETY_SAFE = "SAFE"
STARTUP_SAFETY_UNRESOLVED = "UNRESOLVED"
STARTUP_SAFETY_UNSAFE = "UNSAFE"

STARTUP_SAFETY_CODES: tuple[str, ...] = (
    STARTUP_SAFETY_SAFE,
    STARTUP_SAFETY_UNRESOLVED,
    STARTUP_SAFETY_UNSAFE,
)


@dataclass(frozen=True)
class StartupSafetyResult:
    """The answer to the one startup question, plus why.

    `safe_to_trade` is the whole decision; `code` and `message` exist so an
    operator reading a heartbeat can tell an unresolved startup from a refused
    one without reading source.
    """

    safe_to_trade: bool
    code: str
    message: str

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


#: What the runtime calls at startup. Zero arguments, one result.
StartupSafetyCheck = Callable[[], StartupSafetyResult]


def unresolved_startup_safety() -> StartupSafetyResult:
    """The production default until Phase 8 is integrated: nobody has checked.

    Deliberately not a stub that returns True with a TODO beside it. This is
    the value the shipped runtime uses, and it keeps broker submission off.
    """
    return StartupSafetyResult(
        safe_to_trade=False,
        code=STARTUP_SAFETY_UNRESOLVED,
        message=(
            "Startup safety is unresolved: reconciliation against the broker "
            "(Phase 8) is not integrated in this build, so no process can know "
            "whether local state survived the last shutdown. Paper order "
            "submission stays disabled; observation continues."
        ),
    )


__all__ = [
    "STARTUP_SAFETY_CODES",
    "STARTUP_SAFETY_SAFE",
    "STARTUP_SAFETY_UNRESOLVED",
    "STARTUP_SAFETY_UNSAFE",
    "StartupSafetyCheck",
    "StartupSafetyResult",
    "unresolved_startup_safety",
]
