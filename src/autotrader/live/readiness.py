"""Display-only source for the audited Live readiness declaration.

Readiness is not an arm switch and is not permission to trade. Prompt 1's
completed safety program established readiness; production configuration
passes that result to the two read-only dashboard services. No strategy, risk,
execution, or reconciliation module imports this value.
"""

from __future__ import annotations

import os

LIVE_READY_ENV = "AUTOTRADER_LIVE_READY"


def configured_live_readiness() -> bool | None:
    """Return the declared readiness, preserving unrecognised values as unknown."""
    value = os.environ.get(LIVE_READY_ENV, "").strip()
    if value == "true":
        return True
    if value == "false":
        return False
    return None


__all__ = ["LIVE_READY_ENV", "configured_live_readiness"]
