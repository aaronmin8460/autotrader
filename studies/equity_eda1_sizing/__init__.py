"""EDA-1 shared-account sizing validation.

Predeclared in `/Volumes/AUTOTRADER_QA/reports/equity-eda1-sizing/search-ledger.md`
before its first result-producing run. The study exists to answer one question:
under a 5% per-symbol ceiling, a 30% total account ceiling and a crypto book
competing for the same account, is there an allocation rule that is
order-independent and symmetric and that preserves EDA-1's behaviour?

It scores the **production** allocator (`autotrader.equity.allocation`) rather
than a study re-implementation, so the policy that is frozen at the end is
literally the code the Paper runtime executes.
"""

from __future__ import annotations

STUDY_SYMBOLS: tuple[str, ...] = (
    "SPY",
    "QQQ",
    "IWM",
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
)

WINDOW_NAMES: tuple[str, ...] = tuple(f"w{index:02d}" for index in range(1, 13))

__all__ = ["STUDY_SYMBOLS", "WINDOW_NAMES"]
