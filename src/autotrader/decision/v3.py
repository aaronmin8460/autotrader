"""V3: the same multi-factor framework, read on three timeframes at once.

V2 scores one timeframe. V3 scores three - 15 minutes, 1 hour, 4 hours - with
the identical feature and scoring code on each, and combines them under a rule
that is a gate rather than a blur.

**The three timeframes have named jobs.** The 15-minute score is the *trigger*:
it is what actually points at a bar. The 1-hour score is *confirmation*: it is
asked only whether the hour agrees. The 4-hour score is *context*: it is asked
only whether the broader move is not opposed. Those roles are why the gates
descend - a full trigger-strength reading is demanded of the timeframe that
triggers, and progressively less of the ones that merely have to not disagree.

**A weighted average alone would not have been multi-timeframe.** Blending
three scores into one number lets a very strong 15-minute reading outvote two
higher timeframes pointing the other way, which is precisely the trade a
multi-timeframe system exists to refuse. So the composite score is reported -
V4 and V5 will want a continuous number - but the *signal* comes from the
gates, and every gate must pass on its own.

**Entries need alignment; exits do not.** A BUY requires all three timeframes
and can be blocked by the 4-hour regime. A SELL requires only the trigger and
the confirmation, and nothing blocks it. The asymmetry is deliberate: refusing
to enter because the timeframes disagree is caution, while refusing to say
"reduce" for the same reason is the engine going quiet exactly when a position
is most exposed. The layers downstream treat a SELL with nothing to reduce as
an ordinary no-order outcome, so saying it costs nothing.

**Higher-timeframe bars are derived and never borrowed from the future.**
`timeframes.aggregate_bars` builds them from the same completed 15-minute bars,
keeps only buckets whose every constituent is present, and
`timeframes.usable_history` admits a bucket only once it has fully closed by
the time the evaluated base bar closed. A 4-hour candle that is three quarters
formed is not "nearly usable"; it is unavailable.

**A timeframe that cannot be scored stops the decision.** Not a fallback to the
two that can, not a reduced-weight blend: an explicit HOLD naming the timeframe
and whether the problem was history or an undefined feature. Guessing the
context is worse than admitting there is none, and the cost of the honest
answer is stated in advance by `required_base_bars`.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

import pandas as pd

from autotrader.decision.bars import normalize_bars
from autotrader.decision.config import AssetClassPolicy, policy_for_symbol
from autotrader.decision.contract import (
    VERSION_V3,
    DecisionResult,
    DecisionSignal,
    MarketRegime,
)
from autotrader.decision.features import FEATURE_SCHEMA_VERSION
from autotrader.decision.scoring import (
    REASON_LOW_CONFIDENCE,
    REASON_REGIME_BLOCKED,
    regime_reason,
)
from autotrader.decision.timeframes import (
    BASE_TIMEFRAME,
    FOUR_HOUR_TIMEFRAME,
    HOUR_TIMEFRAME,
    V3_TIMEFRAMES,
    align_timeframes,
)
from autotrader.decision.v2 import (
    TimeframeEvaluation,
    evaluate_timeframe,
    require_policy_matches_symbol,
)

#: The three roles, bound to the three timeframes. Written down rather than
#: taken positionally from a tuple, because "the second timeframe" is not a
#: description of anything and a reordered tuple would silently swap which
#: score has to clear which gate.
TRIGGER_TIMEFRAME = BASE_TIMEFRAME
CONFIRM_TIMEFRAME = HOUR_TIMEFRAME
CONTEXT_TIMEFRAME = FOUR_HOUR_TIMEFRAME

REASON_ALIGNED_BULLISH = "TIMEFRAMES_ALIGNED_BULLISH"
REASON_ALIGNED_BEARISH = "TIMEFRAMES_ALIGNED_BEARISH"
#: Named "unmet" rather than "below": on a bearish attempt the gate a score
#: fails is the *negated* one, and a token reading "below the gate" would then
#: be reported for a score that was too high. Direction-neutral is the only
#: wording that is true in both.
REASON_TRIGGER_GATE_UNMET = "TRIGGER_GATE_UNMET_15M"
REASON_CONFIRM_GATE_UNMET = "CONFIRMATION_GATE_UNMET_1H"
REASON_CONTEXT_GATE_UNMET = "CONTEXT_GATE_UNMET_4H"


def timeframe_summary_reason(label: str, score: float) -> str:
    """One token summarizing a timeframe's direction, e.g. ``TF_4H_BULLISH``."""
    if score > 0.0:
        direction = "BULLISH"
    elif score < 0.0:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"
    return f"TF_{label.upper()}_{direction}"


class MultiTimeframeV3Engine:
    """The V3 decision engine for one asset class.

    Fixed to the three timeframes above. The *policy* may vary the periods,
    factor weights and thresholds each timeframe is scored under; the set of
    timeframes and which role each plays is structure, not configuration.
    """

    def __init__(self, policy: AssetClassPolicy) -> None:
        self._policy = policy
        for spec in V3_TIMEFRAMES:
            policy.timeframe(spec.label)

    @classmethod
    def for_symbol(cls, symbol: str) -> MultiTimeframeV3Engine:
        """Build the engine carrying the shipped policy for `symbol`'s asset class."""
        return cls(policy_for_symbol(symbol))

    @property
    def policy(self) -> AssetClassPolicy:
        """The policy in force."""
        return self._policy

    @property
    def version(self) -> str:
        """The identifier stored with every decision this engine makes."""
        return VERSION_V3

    @property
    def required_base_bars(self) -> int:
        """Completed base bars needed before all three timeframes can be scored.

        Governed by the 4-hour timeframe, and large. Sixteen base bars complete
        one 4-hour bar for a continuously traded pair, and a whole regular
        session completes one for an equity, so the context is the expensive
        part of V3 by a wide margin. The number is stated here so a caller sizes
        its window from it rather than discovering a permanent HOLD.
        """
        return self._policy.required_base_bars(tuple(spec.label for spec in V3_TIMEFRAMES))

    def describe(self) -> Mapping[str, object]:
        """The configuration in force, as serializable values."""
        return MappingProxyType(
            {
                "engine_version": self.version,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "timeframes": [spec.label for spec in V3_TIMEFRAMES],
                "timeframe_roles": {
                    "trigger": TRIGGER_TIMEFRAME.label,
                    "confirm": CONFIRM_TIMEFRAME.label,
                    "context": CONTEXT_TIMEFRAME.label,
                },
                "required_base_bars": self.required_base_bars,
                **dict(self._policy.describe()),
            }
        )

    def decide(self, bars: pd.DataFrame) -> DecisionResult:
        """Score the newest completed base bar across all three timeframes."""
        frame = normalize_bars(bars)
        symbol = str(frame["symbol"].iloc[0])
        require_policy_matches_symbol(symbol, self._policy)
        timestamp = pd.Timestamp(frame["timestamp"].iloc[-1])

        aligned = align_timeframes(frame, V3_TIMEFRAMES, base_bar_start=timestamp)
        evaluations = {
            spec.label: evaluate_timeframe(
                aligned[spec.label],
                spec=spec,
                timeframe_policy=self._policy.timeframe(spec.label),
                policy=self._policy,
            )
            for spec in V3_TIMEFRAMES
        }

        blocked = tuple(
            str(evaluation.blocking_reason)
            for evaluation in evaluations.values()
            if not evaluation.available
        )
        if blocked:
            return self._result(
                symbol=symbol,
                timestamp=timestamp,
                signal=DecisionSignal.HOLD,
                score=0.0,
                confidence=0.0,
                regime=MarketRegime.UNKNOWN,
                reasons=blocked,
                evaluations=evaluations,
                bar_count=len(frame),
            )

        weights = self._policy.timeframe_weights
        score = _bounded(
            sum(evaluations[label].score * weight for label, weight in weights.items())
        )
        confidence = _bounded(
            sum(evaluations[label].confidence * weight for label, weight in weights.items()),
            lower=0.0,
        )
        regime = evaluations[CONTEXT_TIMEFRAME.label].regime

        signal, gate_reasons = self._apply_gates(
            evaluations=evaluations,
            score=score,
            confidence=confidence,
            regime=regime,
        )
        summaries = tuple(
            timeframe_summary_reason(spec.label, evaluations[spec.label].score)
            for spec in V3_TIMEFRAMES
        )
        return self._result(
            symbol=symbol,
            timestamp=timestamp,
            signal=signal,
            score=score,
            confidence=confidence,
            regime=regime,
            reasons=(*gate_reasons, *summaries, regime_reason(regime)),
            evaluations=evaluations,
            bar_count=len(frame),
        )

    def _apply_gates(
        self,
        *,
        evaluations: Mapping[str, TimeframeEvaluation],
        score: float,
        confidence: float,
        regime: MarketRegime,
    ) -> tuple[DecisionSignal, tuple[str, ...]]:
        """Turn three scores into a direction, or into the gates that refused one."""
        gates = self._policy.gates
        thresholds = self._policy.thresholds
        if confidence < thresholds.min_confidence:
            return DecisionSignal.HOLD, (REASON_LOW_CONFIDENCE,)

        trigger = evaluations[TRIGGER_TIMEFRAME.label].score
        confirm = evaluations[CONFIRM_TIMEFRAME.label].score
        context = evaluations[CONTEXT_TIMEFRAME.label].score

        bullish = (
            trigger >= gates.trigger_min
            and confirm >= gates.confirm_min
            and context >= gates.context_min
        )
        if bullish:
            if regime is MarketRegime.HIGH_VOLATILITY:
                return DecisionSignal.HOLD, (REASON_REGIME_BLOCKED,)
            return DecisionSignal.BUY, (REASON_ALIGNED_BULLISH,)

        bearish = trigger <= -gates.trigger_min and confirm <= -gates.confirm_min
        if bearish:
            return DecisionSignal.SELL, (REASON_ALIGNED_BEARISH,)

        return DecisionSignal.HOLD, self._gate_failures(
            trigger=trigger,
            confirm=confirm,
            context=context,
            bearish_attempt=score < 0.0,
        )

    def _gate_failures(
        self,
        *,
        trigger: float,
        confirm: float,
        context: float,
        bearish_attempt: bool,
    ) -> tuple[str, ...]:
        """Which gates refused the direction the composite was leaning towards.

        Reported for the leaning direction only. Listing the bullish gates a
        clearly bearish bar failed would be true and useless; the question an
        audit asks is why the direction that was nearly taken was not taken.
        """
        gates = self._policy.gates
        failures: list[str] = []
        if bearish_attempt:
            if trigger > -gates.trigger_min:
                failures.append(REASON_TRIGGER_GATE_UNMET)
            if confirm > -gates.confirm_min:
                failures.append(REASON_CONFIRM_GATE_UNMET)
        else:
            if trigger < gates.trigger_min:
                failures.append(REASON_TRIGGER_GATE_UNMET)
            if confirm < gates.confirm_min:
                failures.append(REASON_CONFIRM_GATE_UNMET)
            if context < gates.context_min:
                failures.append(REASON_CONTEXT_GATE_UNMET)
        return tuple(failures)

    def _result(
        self,
        *,
        symbol: str,
        timestamp: pd.Timestamp,
        signal: DecisionSignal,
        score: float,
        confidence: float,
        regime: MarketRegime,
        reasons: tuple[str, ...],
        evaluations: Mapping[str, TimeframeEvaluation],
        bar_count: int,
    ) -> DecisionResult:
        features: dict[str, float] = {}
        for label, evaluation in evaluations.items():
            for name, value in evaluation.features.items():
                features[f"{label}.{name}"] = value

        metadata = dict(self.describe())
        metadata["bar_count"] = bar_count
        metadata["timeframe_scores"] = {
            label: evaluation.score for label, evaluation in evaluations.items()
        }
        metadata["timeframe_confidence"] = {
            label: evaluation.confidence for label, evaluation in evaluations.items()
        }
        metadata["timeframe_bar_counts"] = {
            label: evaluation.bar_count for label, evaluation in evaluations.items()
        }
        metadata["timeframe_bar_timestamps"] = {
            label: (
                None if evaluation.bar_timestamp is None else evaluation.bar_timestamp.isoformat()
            )
            for label, evaluation in evaluations.items()
        }
        metadata["timeframe_regimes"] = {
            label: evaluation.regime.value for label, evaluation in evaluations.items()
        }
        metadata["factor_scores"] = {
            label: dict(evaluation.factor_scores) for label, evaluation in evaluations.items()
        }
        return DecisionResult(
            version=self.version,
            symbol=symbol,
            timestamp=timestamp,
            signal=signal,
            score=score,
            confidence=confidence,
            reasons=reasons,
            features=features,
            policy=metadata,
            regime=regime,
        )


def _bounded(value: float, *, lower: float = -1.0, upper: float = 1.0) -> float:
    """Clamp a weighted mean against floating-point drift past its own bound."""
    return max(lower, min(upper, float(value)))


__all__ = [
    "CONFIRM_TIMEFRAME",
    "CONTEXT_TIMEFRAME",
    "REASON_ALIGNED_BEARISH",
    "REASON_ALIGNED_BULLISH",
    "REASON_CONFIRM_GATE_UNMET",
    "REASON_CONTEXT_GATE_UNMET",
    "REASON_TRIGGER_GATE_UNMET",
    "TRIGGER_TIMEFRAME",
    "MultiTimeframeV3Engine",
    "timeframe_summary_reason",
]
