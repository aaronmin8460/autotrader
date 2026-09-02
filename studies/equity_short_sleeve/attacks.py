"""Robustness-attack machinery (ledger §L12).

Attacks are applied IDENTICALLY to the candidate and to B0 wherever the
attack is defined on the incumbent too (year removal, window removal, cost,
delay), so what is compared is always a difference between architectures
under one attack, never a candidate under attack against an unattacked
baseline.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import numpy as np
import pandas as pd

from studies.equity_short_sleeve.shorts import ShortResult


def session_series(result: ShortResult) -> pd.DataFrame:
    """Session-level equity, returns and sleeve P&L for one replay."""
    from autotrader.equity.session import market_date

    days = [market_date(ts.to_pydatetime()) for ts in result.timestamps]
    frame = pd.DataFrame(
        {
            "session": days,
            "equity": list(result.equity_curve),
            "short_pnl": list(result.short_pnl_series),
            "long_pnl": list(result.long_pnl_series),
        }
    )
    grouped = frame.groupby("session", sort=True).agg(
        equity=("equity", "last"),
        short_pnl=("short_pnl", "sum"),
        long_pnl=("long_pnl", "sum"),
    )
    grouped["ret"] = grouped["equity"].pct_change()
    grouped.loc[grouped.index[0], "ret"] = grouped["equity"].iloc[0] / result.initial_cash - 1.0
    return grouped


def compound_excluding(frame: pd.DataFrame, excluded: Sequence[date]) -> float:
    """Net return with the excluded sessions' returns removed from the chain.

    The standard year/window-removal attack in this research lineage: the
    strategy is not re-run, the excluded interval's compounding is simply
    dropped, so the comparison isolates the interval's contribution.
    """
    drop = set(excluded)
    kept = frame.loc[[s not in drop for s in frame.index], "ret"]
    return float((1.0 + kept).prod() - 1.0)


def sessions_in_year(frame: pd.DataFrame, year: int) -> list[date]:
    return [s for s in frame.index if s.year == year]


def max_drawdown_excluding(frame: pd.DataFrame, excluded: Sequence[date]) -> float:
    drop = set(excluded)
    kept = frame.loc[[s not in drop for s in frame.index], "ret"]
    curve = (1.0 + kept).cumprod()
    running = curve.cummax()
    return float((curve / running - 1.0).min())


def sharpe_excluding(frame: pd.DataFrame, excluded: Sequence[date]) -> float:
    drop = set(excluded)
    kept = frame.loc[[s not in drop for s in frame.index], "ret"]
    if kept.std() == 0 or not len(kept):
        return 0.0
    return float(kept.mean() / kept.std() * np.sqrt(252))


def short_pnl_by_year(frame: pd.DataFrame) -> dict[int, float]:
    tally: dict[int, float] = {}
    for session, value in zip(frame.index, frame["short_pnl"], strict=True):
        tally[session.year] = tally.get(session.year, 0.0) + float(value)
    return dict(sorted(tally.items()))


__all__ = [
    "compound_excluding",
    "max_drawdown_excluding",
    "session_series",
    "sessions_in_year",
    "sharpe_excluding",
    "short_pnl_by_year",
]
