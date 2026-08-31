"""Phase-3 cross-sectional selection rules and the target-weight builder
(ledger §L4, §L5, and the dated component-isolation amendment).

Everything is computed from completed-session closes with the incumbent's
1-session information lag. Selection is recomputed every 21 sessions (the
monthly convention declared in the ledger); membership computed at rebalance
session ``r`` uses closes through ``r − 1`` and is active for sessions
``r … r + 20`` — the same "state for session s reads closes through s − 1"
convention as the validated participation overlay.

Ties everywhere: (metric descending, symbol lexicographic ascending).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import pandas as pd

from studies.equity_deep_arch.state import session_closes

REBALANCE_EVERY_SESSIONS = 21
RS_PRIMARY = 126
RS_SECONDARY = 63
SMA_SESSIONS = 200
PER_SYMBOL_CAP = 0.10


def close_table(frames: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Sessions × symbols table of completed-session closes (NaN where a
    symbol had no bar that session)."""
    columns: dict[str, pd.Series] = {}
    for symbol in sorted(frames):
        closes = session_closes(frames[symbol])
        columns[symbol] = pd.Series(
            closes["close"].to_numpy(dtype="float64"),
            index=pd.Index(closes["session"], name="session"),
        )
    return pd.DataFrame(columns).sort_index()


def trailing_return(table: pd.DataFrame, horizon: int, lag: int = 1) -> pd.DataFrame:
    """RS at session i = close[i−lag] / close[i−lag−horizon] − 1."""
    shifted = table.shift(lag)
    return shifted / shifted.shift(horizon) - 1.0


def above_sma(table: pd.DataFrame, sma_sessions: int = SMA_SESSIONS, lag: int = 1) -> pd.DataFrame:
    """True at session i iff close[i−lag] > SMA(sma_sessions)[i−lag]."""
    shifted = table.shift(lag)
    sma = shifted.rolling(sma_sessions).mean()
    return (shifted > sma) & sma.notna()


def rank_symbols(
    scores: Mapping[str, float],
    eligible: Sequence[str],
) -> list[str]:
    """Eligible symbols, best score first, deterministic ties."""
    scored = [
        (symbol, scores[symbol])
        for symbol in eligible
        if symbol in scores and pd.notna(scores[symbol])
    ]
    return [symbol for symbol, _ in sorted(scored, key=lambda item: (-item[1], item[0]))]


def rebalance_sessions(region_sessions: Sequence[date]) -> list[date]:
    """Every 21st session of the scored region, starting at its first."""
    return [
        session
        for index, session in enumerate(region_sessions)
        if index % REBALANCE_EVERY_SESSIONS == 0
    ]


def build_membership(
    region_sessions: Sequence[date],
    select_at: Mapping[date, Sequence[str]],
) -> dict[date, tuple[str, ...]]:
    """Per-session active membership from per-rebalance selections."""
    membership: dict[date, tuple[str, ...]] = {}
    current: tuple[str, ...] = ()
    for session in region_sessions:
        if session in select_at:
            current = tuple(select_at[session])
        membership[session] = current
    return membership


def build_targets(
    frames: Mapping[str, pd.DataFrame],
    region_sessions: Sequence[date],
    participate: Mapping[date, bool],
    membership: Mapping[date, tuple[str, ...]],
    stance: Mapping[str, Mapping[pd.Timestamp, int]],
    *,
    active_weight_of: Mapping[date, Mapping[str, float]],
    reserved_weight: float,
) -> dict[str, dict[pd.Timestamp, float]]:
    """Per-bar target weights for the weighted replay.

    PARTICIPATE session: active members hold `active_weight_of[session]`
    (allocator output), everyone else 0. DEFENSIVE session: every universe
    symbol holds `reserved_weight × stance` (bar-level V3 stance), the
    faithful extension of "hand the sleeve back to V3".
    """
    from autotrader.equity.session import market_date

    session_set = set(region_sessions)
    targets: dict[str, dict[pd.Timestamp, float]] = {}
    for symbol in sorted(frames):
        frame = frames[symbol]
        symbol_stance = stance.get(symbol, {})
        series: dict[pd.Timestamp, float] = {}
        for ts in frame["timestamp"]:
            stamp = pd.Timestamp(ts)
            session = market_date(stamp.to_pydatetime())
            if session not in session_set:
                continue
            if participate[session]:
                weight = float(active_weight_of[session].get(symbol, 0.0))
            else:
                weight = reserved_weight * float(symbol_stance.get(stamp, 0))
            series[stamp] = weight
        targets[symbol] = series
    return targets


__all__ = [
    "PER_SYMBOL_CAP",
    "REBALANCE_EVERY_SESSIONS",
    "RS_PRIMARY",
    "RS_SECONDARY",
    "SMA_SESSIONS",
    "above_sma",
    "build_membership",
    "build_targets",
    "close_table",
    "rank_symbols",
    "rebalance_sessions",
    "trailing_return",
]
