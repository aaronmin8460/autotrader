"""EDA-1 regime state and overlay: the research champion's exact semantics.

This module is a faithful port of the deep-architecture research program's
``studies/equity_deep_arch/state.py`` and ``overlay.py`` (research branch
``research/equity-deep-architecture``, frozen at ``ef42da7``), whose result was
classified NEW CHAMPION FOR SHADOW. Nothing here was re-derived: the router,
its parameters, its warm-up fail-safe, and the overlay's stance semantics are
the predeclared, causality-audited, byte-reproduced originals, restated over
the production decision contract so a live shadow can record what the champion
would have decided. It stays a pure transform - no storage, no network, no
execution import - and a decision it produces goes exactly where a V3 shadow
decision goes: into a row, and nowhere else.

**The state is causal by construction.** The state governing session ``s`` is
a function of completed-session closes through ``s - lag`` only (lag >= 1), so
a decision bar inside session ``s`` never reads its own session's close, let
alone a future one. While fewer than ``sma_sessions`` closes exist the answer
is DEFENSIVE rather than a guess - participation requires *evidence* of an
intact trend.

**The router has zero fitted parameters.** PARTICIPATE iff the reference
symbol's completed-session close is above its 200-session moving average AND
its trailing-peak drawdown is above -5%. Both parameters are external
conventions fixed by the research predeclaration, not tuned values.

**The overlay is a target-position transform.** target = 1 while PARTICIPATE,
else the source engine's own stance reconstructed from its stored series (long
after a BUY, flat after a SELL). Signals are emitted only on target
transitions, so the overlay adds no turnover inside a regime and hands
positions back to the source without ever holding a position neither layer
asked for.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from autotrader.decision.contract import DecisionSignal
from autotrader.equity.session import market_date

#: The research architecture token, verbatim. Every reason token the overlay
#: emits is prefixed with it, so a stored shadow row and a stored research row
#: read identically.
EDA1_ARCHITECTURE = "EDA1_RGP"

#: The engine-version label EDA-1 rows are stored under in `shadow_decisions`.
EDA1_ENGINE_VERSION = "eda1"

#: The reference symbol whose completed-session closes drive the state.
REGIME_REFERENCE_SYMBOL = "SPY"

#: Convention: 200 completed sessions, the canonical long-trend average.
DEFAULT_SMA_SESSIONS = 200

#: Convention: the calm/pullback boundary of the published causal labelling.
DEFAULT_CALM_THRESHOLD = -0.05

#: The state governing session ``s`` reads closes through ``s - lag`` only.
DEFAULT_LAG_SESSIONS = 1


class StateInputError(Exception):
    """A market-state request that cannot be answered causally."""


class OverlayError(Exception):
    """An overlay asked to combine series that do not describe the same bars."""


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


@dataclass(frozen=True)
class ParticipationState:
    """One session's resolved regime state, with the evidence that produced it.

    ``info_*`` values are the lagged information set - the completed close, the
    moving average, and the trailing-peak drawdown the router actually read.
    They are None during warm-up, when there was nothing causal to read; the
    state is then DEFENSIVE by the fail-safe, not by measurement.
    """

    session_date: date
    participate: bool
    info_close: float | None
    info_sma: float | None
    info_drawdown: float | None
    sessions_observed: int


def session_closes(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per session: its date and its last observed close.

    The last *observed* bar of the session, which on an early close or a
    provider outage is simply the latest bar the feed published - exactly what
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


def state_for_session(
    closes: pd.DataFrame,
    spec: ParticipationSpec,
    *,
    session_date: date,
) -> ParticipationState:
    """The state governing `session_date`, from completed closes strictly before it.

    `closes` must contain only sessions before `session_date` - the live
    caller's frame of completed sessions. The state is read exactly as
    `participation_series` would read it for a row appended at the next
    position: the information set is the last ``spec.lag_sessions - 1``-shifted
    close, i.e. with the default lag of one, the newest completed session. A
    test pins this equivalence against `participation_series` itself, so the
    live path and the research path cannot drift apart.
    """
    if closes.empty:
        raise StateInputError("Cannot resolve a participation state from zero sessions.")
    newest = closes["session"].iloc[len(closes) - 1]
    if newest >= session_date:
        raise StateInputError(
            f"The completed-closes table reaches {newest.isoformat()}, which is not "
            f"strictly before {session_date.isoformat()}. A session's state may only "
            "read closes from completed prior sessions."
        )
    values = closes["close"].to_numpy(dtype="float64")
    j = len(values) - spec.lag_sessions
    if j < 0:
        return ParticipationState(
            session_date=session_date,
            participate=False,
            info_close=None,
            info_sma=None,
            info_drawdown=None,
            sessions_observed=len(values),
        )
    sma = pd.Series(values).rolling(spec.sma_sessions).mean().to_numpy()
    peak = pd.Series(values).cummax().to_numpy()
    drawdown = values / peak - 1.0
    if pd.isna(sma[j]):
        return ParticipationState(
            session_date=session_date,
            participate=False,
            info_close=float(values[j]),
            info_sma=None,
            info_drawdown=float(drawdown[j]),
            sessions_observed=len(values),
        )
    info_close = float(values[j])
    info_sma = float(sma[j])
    info_dd = float(drawdown[j])
    return ParticipationState(
        session_date=session_date,
        participate=bool(info_close > info_sma and info_dd > spec.calm_threshold),
        info_close=info_close,
        info_sma=info_sma,
        info_drawdown=info_dd,
        sessions_observed=len(values),
    )


@dataclass(frozen=True)
class SeriesRecord:
    """One engine decision as the overlay consumes and produces it.

    The research adapter's record shape, restated over aware UTC datetimes so
    a stored shadow row round-trips through it without a third timestamp type.
    """

    timestamp: datetime
    symbol: str
    signal: DecisionSignal
    score: float
    confidence: float
    regime: str
    reasons: tuple[str, ...]


def source_stance(records: Sequence[SeriesRecord]) -> list[int]:
    """The stance (0 flat, 1 long) implied by a stored series at each record.

    The stance *at* a record reflects that record's own signal: a BUY bar is
    already stance 1, because regenerating a BUY at that bar reproduces the
    identical next-open fill the source engine got.
    """
    stance = 0
    result: list[int] = []
    for record in records:
        if record.signal is DecisionSignal.BUY:
            stance = 1
        elif record.signal is DecisionSignal.SELL:
            stance = 0
        result.append(stance)
    return result


def participation_overlay(
    records: Sequence[SeriesRecord],
    participate: Mapping[date, bool],
    *,
    architecture: str = EDA1_ARCHITECTURE,
) -> tuple[SeriesRecord, ...]:
    """The challenger series: long while participating, the source otherwise.

    `participate` is keyed by session date; each record is mapped to its
    session through `market_date`, exactly as the research per-bar map was
    built from its per-session table. A bar whose session is absent is a
    contract violation, not a default - the state series must cover the series.
    """
    if not records:
        raise OverlayError("An overlay needs a non-empty source series.")
    ordered = sorted(records, key=lambda record: record.timestamp)
    stances = source_stance(ordered)

    result: list[SeriesRecord] = []
    held = 0
    for record, stance in zip(ordered, stances, strict=True):
        state = participate.get(market_date(record.timestamp))
        if state is None:
            raise OverlayError(
                f"No participation state for the session of bar "
                f"{record.timestamp.isoformat()}; the state series must cover every "
                "bar of the source series."
            )
        target = 1 if state else stance
        if target == 1 and held == 0:
            signal = DecisionSignal.BUY
            reasons = (
                (f"{architecture}_PARTICIPATE_ENTER",)
                if state
                else tuple(record.reasons) or (f"{architecture}_SOURCE_ENTER",)
            )
        elif target == 0 and held == 1:
            signal = DecisionSignal.SELL
            reasons = tuple(record.reasons) or (f"{architecture}_SOURCE_EXIT",)
        else:
            signal = DecisionSignal.HOLD
            reasons = (f"{architecture}_HOLD",)
        held = target
        result.append(
            SeriesRecord(
                timestamp=record.timestamp,
                symbol=record.symbol,
                signal=signal,
                score=record.score,
                confidence=record.confidence,
                regime="PARTICIPATE" if state else record.regime,
                reasons=reasons,
            )
        )
    return tuple(result)


__all__ = [
    "DEFAULT_CALM_THRESHOLD",
    "DEFAULT_LAG_SESSIONS",
    "DEFAULT_SMA_SESSIONS",
    "EDA1_ARCHITECTURE",
    "EDA1_ENGINE_VERSION",
    "REGIME_REFERENCE_SYMBOL",
    "OverlayError",
    "ParticipationSpec",
    "ParticipationState",
    "SeriesRecord",
    "StateInputError",
    "participation_overlay",
    "participation_series",
    "session_closes",
    "source_stance",
    "state_for_session",
]
