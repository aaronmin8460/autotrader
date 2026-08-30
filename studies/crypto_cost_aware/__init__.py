"""Cost-aware crypto decision research.

Research only. Nothing in this package activates an engine, changes a runtime,
touches a broker, or places an order. It reads the completed V1-V5 historical
evaluation's artifacts and asks one question of them: does requiring an
estimated edge to exceed estimated round-trip friction, *before* a trade is
allowed, produce a materially better decision policy?

The package is deliberately downstream-blind. It produces BUY/HOLD/SELL
candidates for a research replay and nothing else. Risk and Execution remain
authoritative and are not imported, referenced or modelled here.
"""

from __future__ import annotations

__all__: list[str] = []
