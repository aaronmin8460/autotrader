"""The research cost-aware eligibility layer.

Architecture, and the boundary it must not cross:

    existing decision engine  (unmodified, V1-V5 as shipped)
            |
            v
    cost-aware eligibility layer   <-- this module
            |
            v
    ENTER_LONG / EXIT_LONG candidate
            |
            v
    research replay only

The layer is a `DecisionEngine` that wraps another `DecisionEngine`. It may
**remove** proposals and it may **delay** them. It can never invent one: every
signal it emits was emitted by the engine it wraps, with the same timestamp,
symbol and action. That is the property `admitted_signals <= upstream_signals`
tested in `tests/test_cost_aware_policy.py`, and it is what keeps a filter from
quietly becoming a strategy.

**Risk is not downstream of this layer and is never filtered by it.**
In production, a liquidation ordered by the risk engine, a daily-loss halt or a
reconciliation-driven flatten does not travel through a decision engine at all;
it originates below the decision layer and is executed regardless of what any
engine proposes. This module therefore cannot suppress one -- it never sees
one. `RISK_ORIGINATED_REASONS` exists for the narrower case where an *engine*
labels its own exit as protective: such an exit is passed through every
suppression rule unconditionally, so that a minimum-hold or cooldown rule can
never hold a position that its own engine wanted out of for a protective
reason. A research filter that could delay a protective exit would be a filter
that had been allowed to overrule safety, and none of these may.

**Everything here is knowable at decision time.** The state a policy carries is
its own history: whether it is holding, how many bars it has held, how many
consecutive bars a signal has persisted, and trailing statistics computed from
bars at or before the bar being decided. No policy reads a future bar, an
outcome, or a realized return. `tests/test_cost_aware_policy.py` proves this by
perturbing every bar after a probe index and requiring the decisions at or
before it to be byte-identical.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import pandas as pd

from autotrader.research.costs import CostModel
from autotrader.research.engines import Action, DecisionEngine, ResearchSignal

from .costs import breakeven_move

#: Reason tokens that mark an engine's own exit as protective rather than
#: opinion-driven. An exit carrying any of these is admitted unconditionally by
#: every policy in this module. The tokens are matched case-insensitively as
#: substrings, so a compound reason string still trips the guard.
RISK_ORIGINATED_REASONS: tuple[str, ...] = (
    "STOP_LOSS",
    "RISK",
    "LIQUIDAT",
    "HALT",
    "DRAWDOWN",
    "PROTECTIVE",
    "EMERGENCY",
    "KILL_SWITCH",
    "RECONCIL",
)

#: The minimum bars of trailing history an edge estimate needs before it means
#: anything. A volatility computed from fewer bars than this is not an estimate.
MIN_VOLATILITY_BARS = 96


class PolicyError(Exception):
    """A policy was configured with something it cannot enforce."""


def is_risk_originated(signal: ResearchSignal) -> bool:
    """True when a signal's own reasons mark it as protective.

    Deliberately permissive: an unrecognised protective token that happens to
    contain one of these substrings is treated as protective, because the
    failure that matters is suppressing an exit that should have gone through,
    not admitting one that need not have.
    """
    joined = str(signal.reason).upper()
    return any(token in joined for token in RISK_ORIGINATED_REASONS)


@dataclass(frozen=True)
class PolicyState:
    """What the layer knows about itself at one bar. All of it is its own past."""

    holding: bool
    bars_held: int
    consecutive_signal_bars: int


class EligibilityPolicy:
    """One admission rule. Sees a proposal and its own history, answers yes or no."""

    name: str = "policy"

    @property
    def parameters(self) -> Mapping[str, object]:
        return {}

    def admits(
        self,
        signal: ResearchSignal,
        state: PolicyState,
        features: Mapping[str, float],
    ) -> bool:
        raise NotImplementedError


class PassThrough(EligibilityPolicy):
    """Admits everything. The control, and the equivalence baseline.

    Wrapping an engine in this must reproduce that engine exactly. That is what
    makes the wrapper's own machinery -- the state tracking, the feature
    computation, the ordering -- provably free of side effects on the result.
    """

    name = "passthrough"

    def admits(
        self,
        signal: ResearchSignal,
        state: PolicyState,
        features: Mapping[str, float],
    ) -> bool:
        return True


@dataclass(frozen=True)
class MinimumHold(EligibilityPolicy):
    """Suppresses a signal-driven exit until the position has been held `bars`.

    Hypothesis C. The economic argument is that an exit taken a few bars after
    an entry cannot have captured a move large enough to pay for the round trip,
    so acting on it converts a small adverse excursion into a certain loss. The
    rule is causal -- how long a position has been held is known while holding
    it -- and it is bounded above by the engine's own behaviour: it delays
    exits, it never extends a position the engine still wants.

    A protective exit is never delayed. See `RISK_ORIGINATED_REASONS`.
    """

    bars: int
    name: str = field(default="min_hold", init=False)

    def __post_init__(self) -> None:
        if self.bars < 0:
            raise PolicyError(f"A minimum hold cannot be negative, got {self.bars}.")

    @property
    def parameters(self) -> Mapping[str, object]:
        return {"bars": self.bars}

    def admits(
        self,
        signal: ResearchSignal,
        state: PolicyState,
        features: Mapping[str, float],
    ) -> bool:
        if signal.action is not Action.EXIT_LONG:
            return True
        if is_risk_originated(signal):
            return True
        return state.bars_held >= self.bars


@dataclass(frozen=True)
class Hysteresis(EligibilityPolicy):
    """Requires a proposal to persist `bars` consecutive bars before it is acted on.

    Hypothesis B. The economic argument is that a threshold crossed once and
    uncrossed on the next bar was never an opinion, and paying a round trip for
    it is the purest form of the churn the completed study measured. Requiring
    persistence is a re-entry band expressed in time rather than in price, which
    is the form available to an engine whose score is not comparable across
    symbols.

    Protective exits bypass the requirement.
    """

    bars: int
    name: str = field(default="hysteresis", init=False)

    def __post_init__(self) -> None:
        if self.bars < 1:
            raise PolicyError(f"Hysteresis needs at least one bar, got {self.bars}.")

    @property
    def parameters(self) -> Mapping[str, object]:
        return {"bars": self.bars}

    def admits(
        self,
        signal: ResearchSignal,
        state: PolicyState,
        features: Mapping[str, float],
    ) -> bool:
        if signal.action is Action.EXIT_LONG and is_risk_originated(signal):
            return True
        return state.consecutive_signal_bars >= self.bars


@dataclass(frozen=True)
class ExpectedEdgeGate(EligibilityPolicy):
    """Admits an entry only when estimated movement exceeds `multiple` x round-trip cost.

    Hypothesis A, in the only form the available information supports. The
    honest statement of what this does and does not claim:

    It does **not** convert a probability into an expected return. The completed
    study's V4 emits a calibrated probability of a 4-bar direction at a zero
    threshold; a probability of direction carries no magnitude, so multiplying
    it by anything to get an expected move would be inventing the magnitude.
    That mapping is not made here and the hypothesis that depends on it is
    rejected in the report rather than approximated.

    What it does instead is bound the *scale* of the achievable move: trailing
    realized volatility over `volatility_bars`, scaled to the intended holding
    horizon by the square root of time, is an estimate of how far this asset
    moves in that span. When that scale is below the round trip's break-even,
    no directional opinion can be worth acting on, because the move being
    predicted is smaller than the fee for predicting it. This is a necessary
    condition, never a sufficient one -- it filters out the certainly-uneconomic
    and asserts nothing about the rest.

    The square-root-of-time scaling assumes increments are roughly independent.
    They are not exactly; at a 24-hour horizon on this data the assumption is
    mild, and the gate is deliberately coarse enough that the error does not
    decide an admission.
    """

    multiple: float
    horizon_bars: int
    cost_model: CostModel
    volatility_bars: int = MIN_VOLATILITY_BARS
    name: str = field(default="expected_edge_gate", init=False)

    def __post_init__(self) -> None:
        if self.multiple <= 0:
            raise PolicyError(f"The cost multiple must be positive, got {self.multiple}.")
        if self.horizon_bars < 1:
            raise PolicyError(f"The horizon must be at least one bar, got {self.horizon_bars}.")
        if self.volatility_bars < MIN_VOLATILITY_BARS:
            raise PolicyError(
                f"A volatility estimated from {self.volatility_bars} bars is not an estimate; "
                f"at least {MIN_VOLATILITY_BARS} are required."
            )

    @property
    def parameters(self) -> Mapping[str, object]:
        return {
            "multiple": self.multiple,
            "horizon_bars": self.horizon_bars,
            "volatility_bars": self.volatility_bars,
            "cost_model": self.cost_model.label,
            "breakeven_move": str(breakeven_move(self.cost_model)),
        }

    @property
    def threshold(self) -> float:
        """The move the horizon-scaled volatility must reach for an entry to pass."""
        return self.multiple * float(breakeven_move(self.cost_model))

    def admits(
        self,
        signal: ResearchSignal,
        state: PolicyState,
        features: Mapping[str, float],
    ) -> bool:
        if signal.action is not Action.ENTER_LONG:
            return True
        volatility = features.get("trailing_volatility")
        if volatility is None or not math.isfinite(volatility):
            # No usable estimate means no demonstrated eligibility. Refusing is
            # the conservative direction: it declines a trade rather than
            # admitting one on an unknown.
            return False
        return volatility * math.sqrt(self.horizon_bars) >= self.threshold


class CostAwareEngine:
    """Wraps a decision engine in an eligibility policy. Research only.

    Satisfies the same `DecisionEngine` protocol as the engine it wraps, so the
    shipped replay simulator, metrics and trade accounting consume it unchanged
    -- which is the point: the candidate is evaluated by the same verified
    machinery that produced the numbers it is being compared against.

    The wrapped engine is never mutated, never reconfigured and never asked
    anything other than what it was already asked. `V1..V5 semantics are
    unchanged` is therefore a structural property here, not a promise.
    """

    def __init__(
        self,
        upstream: DecisionEngine,
        policy: EligibilityPolicy,
        *,
        volatility_bars: int = MIN_VOLATILITY_BARS,
        price_column: str = "close",
        label: str | None = None,
    ) -> None:
        self._upstream = upstream
        self._policy = policy
        self._volatility_bars = int(volatility_bars)
        self._price_column = price_column
        self._label = label or f"{upstream.name}+{policy.name}"

    @property
    def name(self) -> str:
        return self._label

    @property
    def version(self) -> str:
        return f"{self._upstream.version}+cost-aware-research"

    @property
    def parameters(self) -> Mapping[str, object]:
        return {
            "upstream": self._upstream.name,
            "upstream_version": self._upstream.version,
            "policy": self._policy.name,
            **{f"policy_{k}": v for k, v in self._policy.parameters.items()},
            "volatility_bars": self._volatility_bars,
        }

    @property
    def warmup_bars(self) -> int:
        """The stricter of the engine's warm-up and the policy's estimator length."""
        return max(int(self._upstream.warmup_bars), self._volatility_bars)

    def _trailing_volatility(self, bars: pd.DataFrame) -> list[float]:
        """Per-bar trailing volatility, using only bars at or before each bar.

        `rolling` is trailing by construction and `pct_change` looks one bar
        back, so position `i` of the result is a function of `bars[: i + 1]`
        alone. That is the whole causality argument, and the perturbation test
        checks it rather than trusting it.
        """
        prices = bars[self._price_column].astype(float)
        returns = prices.pct_change()
        volatility = returns.rolling(self._volatility_bars).std()
        return [float(v) if pd.notna(v) else float("nan") for v in volatility]

    def generate(self, bars: pd.DataFrame) -> Sequence[ResearchSignal]:
        """Filter the upstream engine's proposals through the policy, in bar order."""
        upstream_signals = list(self._upstream.generate(bars))
        if not upstream_signals:
            return ()

        volatility = self._trailing_volatility(bars)
        index_of = {timestamp: i for i, timestamp in enumerate(bars["timestamp"])}
        by_index: dict[int, ResearchSignal] = {}
        for signal in upstream_signals:
            position = index_of.get(signal.timestamp)
            if position is None:
                raise PolicyError(
                    f"{self._upstream.name} signalled at {signal.timestamp}, which is not a bar "
                    "in the frame it was given."
                )
            by_index[position] = signal

        admitted: list[ResearchSignal] = []
        holding = False
        entered_at: int | None = None
        run_action: Action | None = None
        run_length = 0

        for position in range(len(bars)):
            signal = by_index.get(position)
            if signal is None:
                run_action, run_length = None, 0
                continue

            # Persistence is counted on the upstream stream, before admission,
            # so a policy that suppresses a signal does not also erase the
            # evidence that it was proposed.
            if signal.action is run_action:
                run_length += 1
            else:
                run_action, run_length = signal.action, 1

            state = PolicyState(
                holding=holding,
                bars_held=(position - entered_at) if (holding and entered_at is not None) else 0,
                consecutive_signal_bars=run_length,
            )
            features = {"trailing_volatility": volatility[position]}

            # A proposal the simulator would treat as a no-op is not put to the
            # policy at all: admitting or refusing an ENTER while already long
            # would make the policy's counters depend on proposals that never
            # became trades.
            if signal.action is Action.ENTER_LONG and holding:
                continue
            if signal.action is Action.EXIT_LONG and not holding:
                continue

            if not self._policy.admits(signal, state, features):
                continue

            admitted.append(signal)
            if signal.action is Action.ENTER_LONG:
                holding, entered_at = True, position
            else:
                holding, entered_at = False, None

        return tuple(admitted)


def build_candidates(
    cost_model: CostModel,
    *,
    horizon_bars: int = 96,
) -> dict[str, EligibilityPolicy]:
    """The pre-declared candidate set. Coarse, bounded, and fixed before results.

    Parameter ranges and why they were chosen *before* any candidate was run:

    - `min_hold` at 8 / 32 / 96 bars (2h / 8h / 24h). 32 is the economically
      motivated value: on this dataset the *median* absolute move first reaches
      the 60.18 bps break-even at about 32 bars on BTC and 16 on ETH, so a hold
      shorter than that is one whose typical move cannot pay for itself. 8 and
      96 bracket it by a factor of four in each direction.
    - `hysteresis` at 2 / 4 bars. One bar is the no-op. Beyond about four bars
      the rule stops being a de-bounce and becomes a lag, which is a different
      mechanism than the one being tested.
    - `edge_gate` at 1.0 / 2.0 x break-even. 1.0 is the necessary condition
      exactly as argued; 2.0 is the conservative multiple the research question
      names. Values above that admit almost nothing on this data and would be
      selected for their scarcity rather than their logic.

    No value here was chosen by looking at a result, and none is tuned per
    symbol, per engine or per window.
    """
    return {
        "passthrough": PassThrough(),
        "min_hold_8": MinimumHold(bars=8),
        "min_hold_32": MinimumHold(bars=32),
        "min_hold_96": MinimumHold(bars=96),
        "hysteresis_2": Hysteresis(bars=2),
        "hysteresis_4": Hysteresis(bars=4),
        "edge_gate_1x": ExpectedEdgeGate(
            multiple=1.0, horizon_bars=horizon_bars, cost_model=cost_model
        ),
        "edge_gate_2x": ExpectedEdgeGate(
            multiple=2.0, horizon_bars=horizon_bars, cost_model=cost_model
        ),
    }


__all__ = [
    "MIN_VOLATILITY_BARS",
    "RISK_ORIGINATED_REASONS",
    "CostAwareEngine",
    "EligibilityPolicy",
    "ExpectedEdgeGate",
    "Hysteresis",
    "MinimumHold",
    "PassThrough",
    "PolicyError",
    "PolicyState",
    "build_candidates",
    "is_risk_originated",
]
