"""Causal market-state series for regime-aware architectures.

Everything here is computed from completed-session closes with an explicit
lag: the state that governs session *s* is a function of closes through
session *s − lag* only. A decision bar inside session *s* therefore never
reads its own session's close, let alone a future one.

The state inputs are deliberately tiny — a moving average of session closes
and a trailing-peak drawdown — because every parameter is a hypothesis, and
the governing predeclaration (search ledger, EDA-1) fixes them from external
convention rather than from anything measured on this data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from autotrader.equity.session import market_date

#: Convention: 200 completed sessions, the canonical long-trend average.
DEFAULT_SMA_SESSIONS = 200

#: Convention: the calm/pullback boundary of the published causal labelling.
DEFAULT_CALM_THRESHOLD = -0.05

#: The state governing session ``s`` reads closes through ``s - lag`` only.
DEFAULT_LAG_SESSIONS = 1


class StateInputError(Exception):
    """A market-state request that cannot be answered causally."""


@dataclass(frozen=True)
class ParticipationSpec:
    """The predeclared participation rule: trend intact and near the high."""

    sma_sessions: int = DEFAULT_SMA_SESSIONS
    calm_threshold: float = DEFAULT_CALM_THRESHOLD
    lag_sessions: int = DEFAULT_LAG_SESSIONS

    def __post_init__(self) -> None:
        if self.sma_sessions < 2:
            raise StateInputError(f"sma_sessions must be >= 2, got {self.sma_sessions}.")
        if not -1.0 < self.calm_threshold < 0.0:
            raise StateInputError(
                f"calm_threshold must be a negative fraction, got {self.calm_threshold}."
            )
        if self.lag_sessions < 1:
            raise StateInputError(
                f"lag_sessions must be >= 1 (a session may never read its own close), "
                f"got {self.lag_sessions}."
            )

    def to_json_dict(self) -> dict[str, object]:
        return {
            "sma_sessions": self.sma_sessions,
            "calm_threshold": self.calm_threshold,
            "lag_sessions": self.lag_sessions,
        }


def session_closes(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per session: its date and its last observed close.

    The last *observed* bar of the session, which on an early close or a
    provider outage is simply the latest bar the feed published — exactly what
    a live process reading the same frame would have held at the bell.
    """
    if frame.empty:
        raise StateInputError("Cannot derive session closes from an empty frame.")
    days = [market_date(ts.to_pydatetime()) for ts in frame["timestamp"]]
    working = pd.DataFrame({"session": days, "close": frame["close"].to_numpy(dtype="float64")})
    closes = working.groupby("session", sort=True).last().reset_index()
    return closes


def participation_series(closes: pd.DataFrame, spec: ParticipationSpec) -> pd.DataFrame:
    """Per session: whether the participation regime is on, and why.

    For the session at position ``i`` the information set is closes through
    position ``i - lag`` inclusive. Participation requires *evidence* of an
    intact trend: while fewer than ``sma_sessions`` closes exist, the answer
    is False (defensive) rather than a guess.
    """
    values = closes["close"].to_numpy(dtype="float64")
    sma = pd.Series(values).rolling(spec.sma_sessions).mean().to_numpy()
    peak = pd.Series(values).cummax().to_numpy()
    drawdown = values / peak - 1.0

    rows: list[dict[str, object]] = []
    for i in range(len(closes)):
        j = i - spec.lag_sessions
        if j < 0 or pd.isna(sma[j]):
            participate = False
            info_close = float("nan") if j < 0 else values[j]
            info_sma = float("nan")
            info_dd = float("nan") if j < 0 else drawdown[j]
        else:
            info_close = values[j]
            info_sma = float(sma[j])
            info_dd = float(drawdown[j])
            participate = info_close > info_sma and info_dd > spec.calm_threshold
        rows.append(
            {
                "session": closes["session"].iloc[i],
                "participate": bool(participate),
                "info_close": info_close,
                "info_sma": info_sma,
                "info_drawdown": info_dd,
            }
        )
    return pd.DataFrame(rows)


def per_bar_participation(
    frame: pd.DataFrame,
    participation: pd.DataFrame,
) -> dict[pd.Timestamp, bool]:
    """Map every bar of `frame` to its session's participation state.

    A bar whose session is absent from the participation table is a
    contract violation, not a default — the state series must cover the frame.
    """
    by_session: dict[date, bool] = {
        row["session"]: bool(row["participate"]) for _, row in participation.iterrows()
    }
    result: dict[pd.Timestamp, bool] = {}
    for ts in frame["timestamp"]:
        day = market_date(ts.to_pydatetime())
        if day not in by_session:
            raise StateInputError(f"No participation state for session {day} (bar {ts}).")
        result[pd.Timestamp(ts)] = by_session[day]
    return result


__all__ = [
    "DEFAULT_CALM_THRESHOLD",
    "DEFAULT_LAG_SESSIONS",
    "DEFAULT_SMA_SESSIONS",
    "ParticipationSpec",
    "StateInputError",
    "participation_series",
    "per_bar_participation",
    "session_closes",
]
