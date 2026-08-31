"""Equity deep-architecture champion/challenger research harness.

Research only. Nothing here reaches a broker, places an order, or changes a
production decision path. Challenger architectures are expressed as decision
series and evaluated through the shipped research replay machinery — the same
accounting the ten-symbol full historical evaluation used.

The search ledger governing this package lives at
``/Volumes/AUTOTRADER_QA/reports/equity-deep-architecture/search-ledger.md``.
Every architecture is predeclared there before its first run.
"""

from __future__ import annotations

PROGRAM = "equity-deep-architecture"

__all__ = ["PROGRAM"]
