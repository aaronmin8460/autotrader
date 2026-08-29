"""V2: the deterministic multi-factor engine, on one timeframe.

V1 asked one question - has the fast average crossed the slow one? - and
answered it on the bar the crossing happened and on no other bar. V2 asks five
questions on every bar, scores each into ``[-1, +1]``, and combines them into
one bounded composite with an explicit hold band around zero. The change is not
"more indicators": it is that V2 has an opinion on every completed bar, which
is what a probability model (V4) or an ensemble (V5) needs from the versions
beneath it.

**The five factors, and why these five.** Two read trend from the same pair of
exponential averages - where they sit relative to each other, and which way the
slow one is going. Three read momentum from genuinely different places: RSI
reads position within the recent range, the MACD histogram reads whether two
averages are converging or separating, and the lookback return reads realized
displacement. Volatility, volume, and the regime classification are measured
too, but they are deliberately *not* directional: they discount confidence and
they can block an entry, and neither of those is the same thing as voting on
which way the market is going.

**One timeframe.** V2 evaluates the base timeframe and nothing else. Combining
timeframes is V3's entire subject, and a V2 that quietly consulted an hourly
chart would make the difference between the two versions unmeasurable.

**HOLD is returned, never raised.** Too little history, an undefined feature, a
weak reading, a score inside the band, and a blocked regime are five different
HOLDs and each carries its own token. The one thing that *is* raised is a
violated input contract - unsorted bars, a mixed symbol, a naive timestamp -
because that is a caller error rather than a market condition, and returning
HOLD for it would let a broken data path look like a quiet market forever.

**Nothing here can reach a broker.** This module imports pandas, the decision
package, and nothing else. The candidate it produces is handed to the existing
risk engine, which remains the sole authority on whether it becomes an order.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

import pandas as pd

from autotrader.decision.bars import normalize_bars
from autotrader.decision.config import AssetClassPolicy, TimeframePolicy, policy_for_symbol
from autotrader.decision.contract import (
    VERSION_V2,
    DecisionInputError,
    DecisionResult,
    DecisionSignal,
    MarketRegime,
    resolve_asset_class,
)
from autotrader.decision.features import (
    FEATURE_SCHEMA_VERSION,
    compute_features,
    latest_feature_row,
    missing_scored_features,
)
from autotrader.decision.scoring import (
    classify_regime,
    composite_score,
    confidence_for,
    context_reasons,
    decide_signal,
    factor_reasons,
    feature_unavailable_reason,
    insufficient_history_reason,
    regime_reason,
    score_factors,
)
from autotrader.decision.timeframes import BASE_TIMEFRAME, TimeframeSpec

#: The score and confidence a timeframe reports when it could not be scored at
#: all. Zero rather than an absent value, so an aggregate over several
#: timeframes is arithmetic rather than a special case - and paired with
#: `available=False`, which is what V3 actually gates on.
UNSCORED_SCORE = 0.0
UNSCORED_CONFIDENCE = 0.0


@dataclass(frozen=True)
class TimeframeEvaluation:
    """One timeframe's complete reading, scored or explicitly unscorable.

    Shared by V2, which uses exactly one of these, and V3, which uses three.
    `available` is the flag every consumer branches on: when it is false the
    score and confidence are zero and `blocking_reason` says which of the two
    reasons - too little history, or a feature that is undefined on this bar -
    applies.
    """

    label: str
    available: bool
    bar_count: int
    bar_timestamp: pd.Timestamp | None = None
    blocking_reason: str | None = None
    score: float = UNSCORED_SCORE
    confidence: float = UNSCORED_CONFIDENCE
    regime: MarketRegime = MarketRegime.UNKNOWN
    factor_scores: Mapping[str, float] = field(default_factory=dict)
    features: Mapping[str, float] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()


def evaluate_timeframe(
    bars: pd.DataFrame,
    *,
    spec: TimeframeSpec,
    timeframe_policy: TimeframePolicy,
    policy: AssetClassPolicy,
) -> TimeframeEvaluation:
    """Score the newest bar of one timeframe, or say precisely why it cannot be scored.

    The single scoring path in this package. V2 calls it once on the base
    timeframe and V3 calls it once per timeframe, so the two versions cannot
    drift apart on what a 15-minute score means.
    """
    periods = timeframe_policy.periods
    required = periods.required_bars
    if len(bars) < required:
        return TimeframeEvaluation(
            label=spec.label,
            available=False,
            bar_count=len(bars),
            bar_timestamp=(pd.Timestamp(bars["timestamp"].iloc[-1]) if len(bars) else None),
            blocking_reason=insufficient_history_reason(spec.reason_token),
        )

    features = compute_features(bars, periods=periods)
    row = latest_feature_row(features)
    timestamp = pd.Timestamp(features["timestamp"].iloc[-1])

    missing = missing_scored_features(row)
    if missing:
        return TimeframeEvaluation(
            label=spec.label,
            available=False,
            bar_count=len(bars),
            bar_timestamp=timestamp,
            blocking_reason=feature_unavailable_reason(spec.reason_token),
            features=MappingProxyType(dict(row)),
        )

    scores = score_factors(row)
    composite = composite_score(scores, timeframe_policy.weights)
    regime = classify_regime(row, policy.thresholds)
    confidence = confidence_for(
        row,
        scores,
        weights=timeframe_policy.weights,
        thresholds=policy.thresholds,
        composite=composite,
    )
    return TimeframeEvaluation(
        label=spec.label,
        available=True,
        bar_count=len(bars),
        bar_timestamp=timestamp,
        score=composite,
        confidence=confidence,
        regime=regime,
        factor_scores=MappingProxyType(dict(scores)),
        features=MappingProxyType(dict(row)),
        reasons=(
            *factor_reasons(scores),
            regime_reason(regime),
            *context_reasons(row, policy.thresholds),
        ),
    )


def require_policy_matches_symbol(symbol: str, policy: AssetClassPolicy) -> None:
    """Refuse to score a symbol under another asset class's policy.

    The check exists because the failure it prevents is silent. Scoring SPY
    under the crypto policy produces a perfectly plausible number - the
    arithmetic does not care - computed against a volatility tolerance chosen
    for a market that moves several times as much. Nothing downstream could
    detect that, so it is refused here.
    """
    actual = resolve_asset_class(symbol)
    if actual is not policy.asset_class:
        raise DecisionInputError(
            f"Symbol {symbol!r} is {actual.value} but policy {policy.name!r} is "
            f"{policy.asset_class.value}. Asset-class policies are not interchangeable: "
            "their thresholds are calibrated to different data semantics."
        )


class MultiFactorV2Engine:
    """The V2 decision engine for one asset class.

    Stateless between calls and cheap to construct. It holds a policy and
    nothing else - no bars, no cache, no client - so two engines built from the
    same policy are interchangeable and a replay cannot be influenced by the
    order calls happened to arrive in.
    """

    def __init__(
        self,
        policy: AssetClassPolicy,
        *,
        spec: TimeframeSpec = BASE_TIMEFRAME,
    ) -> None:
        self._policy = policy
        self._spec = spec
        self._timeframe_policy = policy.timeframe(spec.label)

    @classmethod
    def for_symbol(cls, symbol: str) -> MultiFactorV2Engine:
        """Build the engine carrying the shipped policy for `symbol`'s asset class."""
        return cls(policy_for_symbol(symbol))

    @property
    def policy(self) -> AssetClassPolicy:
        """The policy in force."""
        return self._policy

    @property
    def version(self) -> str:
        """The identifier stored with every decision this engine makes."""
        return VERSION_V2

    @property
    def required_base_bars(self) -> int:
        """Completed base bars needed before this engine can name a direction."""
        return self._policy.required_base_bars((self._spec.label,))

    def describe(self) -> Mapping[str, object]:
        """The configuration in force, as serializable values."""
        return MappingProxyType(
            {
                "engine_version": self.version,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "timeframes": [self._spec.label],
                "required_base_bars": self.required_base_bars,
                **dict(self._policy.describe()),
            }
        )

    def decide(self, bars: pd.DataFrame) -> DecisionResult:
        """Score the newest completed bar in `bars`.

        Older bars are indicator state, not a backlog. A candidate is produced
        for the newest bar only, which is what keeps a restart from replaying
        every score of the last two days as though each were new.
        """
        frame = normalize_bars(bars)
        symbol = str(frame["symbol"].iloc[0])
        require_policy_matches_symbol(symbol, self._policy)
        timestamp = pd.Timestamp(frame["timestamp"].iloc[-1])

        evaluation = evaluate_timeframe(
            frame,
            spec=self._spec,
            timeframe_policy=self._timeframe_policy,
            policy=self._policy,
        )
        if not evaluation.available:
            return self._result(
                symbol=symbol,
                timestamp=timestamp,
                signal=DecisionSignal.HOLD,
                score=UNSCORED_SCORE,
                confidence=UNSCORED_CONFIDENCE,
                regime=MarketRegime.UNKNOWN,
                reasons=(str(evaluation.blocking_reason),),
                features=evaluation.features,
                bar_count=len(frame),
            )

        signal, signal_reasons = decide_signal(
            score=evaluation.score,
            confidence=evaluation.confidence,
            regime=evaluation.regime,
            thresholds=self._policy.thresholds,
        )
        return self._result(
            symbol=symbol,
            timestamp=timestamp,
            signal=signal,
            score=evaluation.score,
            confidence=evaluation.confidence,
            regime=evaluation.regime,
            reasons=(*signal_reasons, *evaluation.reasons),
            features=evaluation.features,
            bar_count=len(frame),
            factor_scores=evaluation.factor_scores,
        )

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
        features: Mapping[str, float],
        bar_count: int,
        factor_scores: Mapping[str, float] | None = None,
    ) -> DecisionResult:
        metadata = dict(self.describe())
        metadata["bar_count"] = bar_count
        metadata["factor_scores"] = dict(factor_scores or {})
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


__all__ = [
    "UNSCORED_CONFIDENCE",
    "UNSCORED_SCORE",
    "MultiFactorV2Engine",
    "TimeframeEvaluation",
    "evaluate_timeframe",
    "require_policy_matches_symbol",
]
