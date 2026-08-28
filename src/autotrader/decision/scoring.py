"""Measurements to a direction: bounded factor scores, confidence, and reasons.

The feature layer measures; this layer judges. Keeping them apart is what lets
a research harness score a hundred thousand bars from one vectorized feature
pass, and what lets a later probability model reuse the identical measurements
under a completely different judgement.

**Bounds hold by construction, not by clipping.** Every factor score is mapped
into ``[-1, +1]`` by `softsign`, and the composite is a weighted mean of those
with weights that sum to one. A weighted mean cannot leave the interval its
operands live in, so the composite is bounded because of what it is rather than
because something trimmed it afterwards.

**`softsign` rather than `tanh`.** ``x / (1 + |x|)`` is four IEEE-754
operations and produces the same bits on every platform this will ever run on.
`tanh` is a libm routine whose last ulp is not guaranteed identical across
builds, and a decision engine whose replay can disagree with production in the
sixteenth decimal is a reconciliation problem waiting to be discovered at the
worst moment. The shape is what matters here - odd, monotonic, saturating - and
both have it.

**Score and confidence answer different questions.** The score says which way
and how strongly; confidence says how much the evidence hangs together. A
single factor screaming while four others sit flat produces a middling score
*and* low confidence, and those are two distinct facts about the same bar. An
engine that collapsed them would be unable to distinguish a weak consensus from
a strong disagreement.

**The volatility block is asymmetric, on purpose.** A regime whose range has
stretched far past its own baseline blocks a BUY and never a SELL. Entering
into disorder is optional; declining to say "reduce" because the market has
become disorderly is not a caution, it is the engine going quiet exactly when
it is most useful. The layers downstream refuse a SELL with no position to
reduce as an ordinary no-order outcome, so nothing is risked by saying it.
"""

from __future__ import annotations

from collections.abc import Mapping

from autotrader.decision.config import DIRECTIONAL_FACTORS, DecisionThresholds, FactorWeights
from autotrader.decision.contract import DecisionSignal, MarketRegime

#: Which feature each directional factor scores. The scoring layer reads
#: exactly these columns and no others, so a feature that is measured but not
#: named here contributes to nothing and a test can prove it.
FACTOR_FEATURES: Mapping[str, str] = {
    "trend_ema": "ema_spread_z",
    "trend_slope": "ema_slope_z",
    "momentum_rsi": "rsi_centered",
    "momentum_macd": "macd_hist_z",
    "momentum_return": "return_z",
}

#: How confidence splits between "the factors agree" and "the score is large".
#: Equal, because neither substitutes for the other: unanimous factors at a
#: score of 0.05 and a score of 0.9 from one factor out of five are both weak,
#: and for different reasons.
AGREEMENT_WEIGHT = 0.5
MAGNITUDE_WEIGHT = 0.5

REASON_LOW_CONFIDENCE = "LOW_CONFIDENCE"
REASON_HOLD_BAND = "SCORE_IN_HOLD_BAND"
REASON_REGIME_BLOCKED = "REGIME_BLOCKED_HIGH_VOLATILITY"
REASON_LOW_PARTICIPATION = "LOW_PARTICIPATION"
REASON_BUY = "SCORE_ABOVE_BUY_THRESHOLD"
REASON_SELL = "SCORE_BELOW_SELL_THRESHOLD"


def insufficient_history_reason(label: str) -> str:
    """The stable token for "this timeframe has too little history"."""
    return f"INSUFFICIENT_HISTORY_{label.upper()}"


def feature_unavailable_reason(label: str) -> str:
    """The stable token for "a scored feature is undefined on this bar"."""
    return f"FEATURE_UNAVAILABLE_{label.upper()}"


def regime_reason(regime: MarketRegime) -> str:
    """The stable token naming the classified regime."""
    return f"REGIME_{regime.value}"


def factor_reason(factor: str, score: float) -> str:
    """The stable token naming one factor's direction on this bar."""
    if score > 0.0:
        direction = "BULLISH"
    elif score < 0.0:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"
    return f"{factor.upper()}_{direction}"


def softsign(value: float) -> float:
    """Map any real number into ``(-1, +1)``, odd and monotonic.

    Saturating rather than clipping: a factor at three standard deviations and
    one at thirty are both "as extreme as this scale can express", but the
    approach to the bound is smooth, so a small change in the input never
    produces a discontinuity in the output.
    """
    numeric = float(value)
    if numeric != numeric:
        raise ValueError("softsign is undefined for NaN; an unmeasured factor is a HOLD.")
    return numeric / (1.0 + abs(numeric))


def _clip_unit(value: float) -> float:
    """Clamp an already-bounded quantity into ``[-1, +1]`` against float drift."""
    return max(-1.0, min(1.0, float(value)))


def score_factors(row: Mapping[str, float]) -> dict[str, float]:
    """The five directional factor scores for one bar, each in ``[-1, +1]``.

    `momentum_rsi` is the one factor that is not standardized upstream and so
    is not passed through `softsign` here. RSI is already bounded by
    construction - it lives in ``[0, 100]`` and is centred to ``[-1, +1]`` - and
    saturating a quantity that is already saturated would compress a reading of
    80 and a reading of 100 into nearly the same number for no reason.
    """
    scores: dict[str, float] = {}
    for factor in DIRECTIONAL_FACTORS:
        raw = float(row[FACTOR_FEATURES[factor]])
        scores[factor] = _clip_unit(raw) if factor == "momentum_rsi" else softsign(raw)
    return scores


def composite_score(scores: Mapping[str, float], weights: FactorWeights) -> float:
    """The weighted mean of the factor scores. Bounded by the weights summing to one."""
    weighting = weights.as_mapping()
    return _clip_unit(sum(scores[factor] * weighting[factor] for factor in DIRECTIONAL_FACTORS))


def agreement(scores: Mapping[str, float], weights: FactorWeights, composite: float) -> float:
    """The weight share of non-flat factors pointing the same way as the composite.

    Flat factors are excluded from the denominator rather than counted against
    agreement. A factor at exactly zero has no opinion, and treating "no
    opinion" as dissent would make an engine less confident for having measured
    something that turned out not to matter on this bar.
    """
    weighting = weights.as_mapping()
    if composite == 0.0:
        return 0.0
    direction = 1.0 if composite > 0.0 else -1.0
    opinionated = sum(weighting[factor] for factor in DIRECTIONAL_FACTORS if scores[factor] != 0.0)
    if opinionated <= 0.0:
        return 0.0
    agreeing = sum(
        weighting[factor]
        for factor in DIRECTIONAL_FACTORS
        if scores[factor] != 0.0 and (scores[factor] > 0.0) == (direction > 0.0)
    )
    return max(0.0, min(1.0, agreeing / opinionated))


def participation_factor(row: Mapping[str, float], thresholds: DecisionThresholds) -> float:
    """How far this bar's volume reaches the policy's participation floor, in ``[0, 1]``.

    Proportional below the floor and flat above it. Volume ten times the median
    does not make a decision ten times more trustworthy - it more often means
    something happened that the indicators have not priced yet - so extra
    participation buys no extra confidence.
    """
    ratio = float(row["volume_ratio"])
    floor = thresholds.low_participation_ratio
    if floor <= 0.0:
        return 1.0
    return max(0.0, min(1.0, ratio / floor))


def volatility_factor(row: Mapping[str, float], thresholds: DecisionThresholds) -> float:
    """How much this bar's range expansion discounts confidence, in ``(0, 1]``.

    One while the range is within the policy's tolerance, then falling as the
    inverse of the excess. Inverse rather than a cliff: a market at 2.6x its
    median range is not categorically different from one at 2.4x, and a
    confidence that fell off a step at the threshold would make the two look it.
    """
    ratio = float(row["volatility_ratio"])
    limit = thresholds.high_volatility_ratio
    if ratio <= limit or ratio <= 0.0:
        return 1.0
    return max(0.0, min(1.0, limit / ratio))


def confidence_for(
    row: Mapping[str, float],
    scores: Mapping[str, float],
    *,
    weights: FactorWeights,
    thresholds: DecisionThresholds,
    composite: float,
) -> float:
    """How much the evidence on this bar hangs together, in ``[0, 1]``.

    Half from factor agreement and half from composite magnitude, then
    discounted by participation and by range expansion. Multiplicative
    discounting so either context problem alone is enough to matter: a strong,
    unanimous score on a bar nobody traded is not a confident reading.
    """
    base = AGREEMENT_WEIGHT * agreement(scores, weights, composite) + MAGNITUDE_WEIGHT * abs(
        composite
    )
    context = participation_factor(row, thresholds) * volatility_factor(row, thresholds)
    return max(0.0, min(1.0, base * context))


def classify_regime(row: Mapping[str, float], thresholds: DecisionThresholds) -> MarketRegime:
    """The coarse market state on this bar. Volatility is checked first.

    Trend is claimed only when the two trend measurements agree: the fast
    average on the correct side of the slow one, *and* the slow one moving that
    way. A spread that is positive while the slow average rolls over is a trend
    ending, and calling it `TREND_UP` is how a trend-follower buys a top.
    """
    if float(row["volatility_ratio"]) > thresholds.high_volatility_ratio:
        return MarketRegime.HIGH_VOLATILITY
    spread = float(row["ema_spread_atr"])
    slope = float(row["ema_slope_atr"])
    if spread > 0.0 and slope > 0.0:
        return MarketRegime.TREND_UP
    if spread < 0.0 and slope < 0.0:
        return MarketRegime.TREND_DOWN
    return MarketRegime.RANGE


def decide_signal(
    *,
    score: float,
    confidence: float,
    regime: MarketRegime,
    thresholds: DecisionThresholds,
) -> tuple[DecisionSignal, tuple[str, ...]]:
    """Turn a score, a confidence, and a regime into a direction and its reasons.

    Gate order is the contract: confidence first, because an unconvincing
    reading should be reported as unconvincing rather than as a blocked entry;
    then the buy side, where the volatility regime can still refuse; then the
    sell side, which nothing here refuses; then the hold band.
    """
    if confidence < thresholds.min_confidence:
        return DecisionSignal.HOLD, (REASON_LOW_CONFIDENCE,)
    if score >= thresholds.buy_score:
        if regime is MarketRegime.HIGH_VOLATILITY:
            return DecisionSignal.HOLD, (REASON_REGIME_BLOCKED,)
        return DecisionSignal.BUY, (REASON_BUY,)
    if score <= thresholds.sell_score:
        return DecisionSignal.SELL, (REASON_SELL,)
    return DecisionSignal.HOLD, (REASON_HOLD_BAND,)


def context_reasons(row: Mapping[str, float], thresholds: DecisionThresholds) -> tuple[str, ...]:
    """Tokens for context that discounted confidence but did not decide direction."""
    reasons: list[str] = []
    if float(row["volume_ratio"]) < thresholds.low_participation_ratio:
        reasons.append(REASON_LOW_PARTICIPATION)
    return tuple(reasons)


def factor_reasons(scores: Mapping[str, float]) -> tuple[str, ...]:
    """One token per directional factor, in report order."""
    return tuple(factor_reason(factor, scores[factor]) for factor in DIRECTIONAL_FACTORS)


__all__ = [
    "AGREEMENT_WEIGHT",
    "FACTOR_FEATURES",
    "MAGNITUDE_WEIGHT",
    "REASON_BUY",
    "REASON_HOLD_BAND",
    "REASON_LOW_CONFIDENCE",
    "REASON_LOW_PARTICIPATION",
    "REASON_REGIME_BLOCKED",
    "REASON_SELL",
    "agreement",
    "classify_regime",
    "composite_score",
    "confidence_for",
    "context_reasons",
    "decide_signal",
    "factor_reason",
    "factor_reasons",
    "feature_unavailable_reason",
    "insufficient_history_reason",
    "participation_factor",
    "regime_reason",
    "score_factors",
    "softsign",
    "volatility_factor",
]
