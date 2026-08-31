"""Phase-1 refined participation state machines (ledger §L2).

Three deterministic refinements of the incumbent participation rule, all over
SPY completed-session closes with the incumbent's information lag and its
defensive-until-evidence warm-up:

- **hysteresis / persistence** (P1-A / P1-B): one generalized two-state
  machine. Entering and leaving PARTICIPATE may use different thresholds
  (a band), and may require the raw condition to hold for k consecutive
  sessions. With equal thresholds, unit ratios and k = 1 the machine reduces
  *exactly* to the incumbent rule, which is asserted by test and used as a
  wiring check.
- **freeze** (P1-C): a three-state series on the published causal drawdown
  boundaries — STRONG participates, PULLBACK freezes the sleeve's current
  position, DEFENSIVE hands back to the source engine.

The state governing session ``s`` reads closes through ``s − lag`` only,
identical to the validated incumbent implementation.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from studies.equity_deep_arch.state import StateInputError

STRONG = "STRONG"
PULLBACK = "PULLBACK"
DEFENSIVE = "DEFENSIVE"


@dataclass(frozen=True)
class RefinedSpec:
    """A hysteresis/persistence refinement of the participation rule.

    ENTER (from defensive) requires, at the lagged information session:
    ``close > enter_sma_ratio * sma`` AND ``drawdown > enter_dd`` for
    ``k_enter`` consecutive sessions. EXIT (from participate) requires
    ``close <= exit_sma_ratio * sma`` OR ``drawdown <= exit_dd`` for
    ``k_exit`` consecutive sessions. Between the bands neither condition
    holds and the state persists.
    """

    sma_sessions: int = 200
    enter_dd: float = -0.05
    exit_dd: float = -0.05
    enter_sma_ratio: float = 1.0
    exit_sma_ratio: float = 1.0
    k_enter: int = 1
    k_exit: int = 1
    lag_sessions: int = 1

    def __post_init__(self) -> None:
        if self.sma_sessions < 2:
            raise StateInputError(f"sma_sessions must be >= 2, got {self.sma_sessions}.")
        for name, value in (("enter_dd", self.enter_dd), ("exit_dd", self.exit_dd)):
            if not -1.0 < value < 0.0:
                raise StateInputError(f"{name} must be a negative fraction, got {value}.")
        if self.exit_dd > self.enter_dd:
            raise StateInputError(
                "exit_dd must be at or below enter_dd (the band may not invert): "
                f"enter {self.enter_dd}, exit {self.exit_dd}."
            )
        if self.exit_sma_ratio > self.enter_sma_ratio:
            raise StateInputError(
                "exit_sma_ratio must be at or below enter_sma_ratio: "
                f"enter {self.enter_sma_ratio}, exit {self.exit_sma_ratio}."
            )
        if self.k_enter < 1 or self.k_exit < 1:
            raise StateInputError("k_enter and k_exit must be >= 1.")
        if self.lag_sessions < 1:
            raise StateInputError("lag_sessions must be >= 1 (a session never reads its own close).")

    def to_json_dict(self) -> dict[str, object]:
        return {
            "sma_sessions": self.sma_sessions,
            "enter_dd": self.enter_dd,
            "exit_dd": self.exit_dd,
            "enter_sma_ratio": self.enter_sma_ratio,
            "exit_sma_ratio": self.exit_sma_ratio,
            "k_enter": self.k_enter,
            "k_exit": self.k_exit,
            "lag_sessions": self.lag_sessions,
        }


@dataclass(frozen=True)
class FreezeSpec:
    """The P1-C three-state rule on the published causal boundaries."""

    sma_sessions: int = 200
    calm_threshold: float = -0.05
    drawdown_threshold: float = -0.10
    lag_sessions: int = 1

    def __post_init__(self) -> None:
        if self.sma_sessions < 2:
            raise StateInputError(f"sma_sessions must be >= 2, got {self.sma_sessions}.")
        if not -1.0 < self.drawdown_threshold < self.calm_threshold < 0.0:
            raise StateInputError(
                "Thresholds must satisfy -1 < drawdown < calm < 0, got "
                f"drawdown {self.drawdown_threshold}, calm {self.calm_threshold}."
            )
        if self.lag_sessions < 1:
            raise StateInputError("lag_sessions must be >= 1 (a session never reads its own close).")

    def to_json_dict(self) -> dict[str, object]:
        return {
            "sma_sessions": self.sma_sessions,
            "calm_threshold": self.calm_threshold,
            "drawdown_threshold": self.drawdown_threshold,
            "lag_sessions": self.lag_sessions,
        }


def _indicators(closes: pd.DataFrame, sma_sessions: int) -> tuple:
    values = closes["close"].to_numpy(dtype="float64")
    sma = pd.Series(values).rolling(sma_sessions).mean().to_numpy()
    peak = pd.Series(values).cummax().to_numpy()
    drawdown = values / peak - 1.0
    return values, sma, drawdown


def refined_participation_series(closes: pd.DataFrame, spec: RefinedSpec) -> pd.DataFrame:
    """Per session: the hysteresis/persistence participation state.

    While fewer than ``sma_sessions`` lagged closes exist the state is False
    and both persistence counters are zero — defensive until evidence, the
    incumbent's warm-up semantics.
    """
    values, sma, drawdown = _indicators(closes, spec.sma_sessions)

    state = False
    enter_run = 0
    exit_run = 0
    rows: list[dict[str, object]] = []
    for i in range(len(closes)):
        j = i - spec.lag_sessions
        if j < 0 or pd.isna(sma[j]):
            state, enter_run, exit_run = False, 0, 0
        else:
            raw_enter = (
                values[j] > spec.enter_sma_ratio * sma[j] and drawdown[j] > spec.enter_dd
            )
            raw_exit = (
                values[j] <= spec.exit_sma_ratio * sma[j] or drawdown[j] <= spec.exit_dd
            )
            if not state:
                enter_run = enter_run + 1 if raw_enter else 0
                exit_run = 0
                if enter_run >= spec.k_enter:
                    state, enter_run = True, 0
            else:
                exit_run = exit_run + 1 if raw_exit else 0
                enter_run = 0
                if exit_run >= spec.k_exit:
                    state, exit_run = False, 0
        rows.append({"session": closes["session"].iloc[i], "participate": bool(state)})
    return pd.DataFrame(rows)


def freeze_state_series(closes: pd.DataFrame, spec: FreezeSpec) -> pd.DataFrame:
    """Per session: STRONG / PULLBACK / DEFENSIVE on the published boundaries."""
    values, sma, drawdown = _indicators(closes, spec.sma_sessions)

    rows: list[dict[str, object]] = []
    for i in range(len(closes)):
        j = i - spec.lag_sessions
        if j < 0 or pd.isna(sma[j]):
            label = DEFENSIVE
        elif values[j] <= sma[j] or drawdown[j] <= spec.drawdown_threshold:
            label = DEFENSIVE
        elif drawdown[j] > spec.calm_threshold:
            label = STRONG
        else:
            label = PULLBACK
        rows.append({"session": closes["session"].iloc[i], "state": label})
    return pd.DataFrame(rows)


def state_flip_count(series: pd.DataFrame, column: str) -> int:
    """How many times the session-level state changes over the series."""
    states = series[column].tolist()
    return sum(1 for a, b in zip(states, states[1:], strict=False) if a != b)


__all__ = [
    "DEFENSIVE",
    "FreezeSpec",
    "PULLBACK",
    "RefinedSpec",
    "STRONG",
    "freeze_state_series",
    "refined_participation_series",
    "state_flip_count",
]
