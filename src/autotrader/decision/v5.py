"""V5: the versioned ensemble. V3's score and V4's probability, read in context.

V3 answers "do three timeframes of a deterministic rule agree?" and V4 answers
"how often did the next interval go up after bars that looked like this one?".
They are two readings of the same seven measurements taken by two methods that
fail differently, which is the only interesting reason to combine anything. V5
combines them, attenuates the combination by the market regime and by the bar's
own volatility, and reports a candidate together with an exact account of which
input moved it and by how much.

**A candidate, and nothing else.** V5 does not size, does not price, does not
approve, and cannot reach a broker. The pipeline is unchanged - Decision Engine
-> DecisionResult -> Risk Engine -> Order Intent -> Execution (docs/SPEC.md
section 7A) - and this module still occupies only the leftmost box. It imports
the decision package and pandas. Nothing here is wired into either runtime and
no default anywhere prefers it; activating an engine is a separate decision by
whoever wires one in, and this branch does not make it.

**Both components, or no decision.** An unavailable V3 or an unavailable V4 is a
HOLD naming which component was missing and why - never a fallback to the one
that answered. Falling back would silently turn V5 into V4 on exactly the bars
where the multi-timeframe context could not be established, which is when that
context is most load-bearing. `required_base_bars` states the cost of that rule
up front, and it is V3's cost, because V3 is the expensive half.

**Context attenuates, it does not vote.** The regime multiplier and the
volatility multiplier are both in ``[0, 1]`` and both multiply. A regime cannot
manufacture a direction that neither engine named, and cannot push a bounded
score past its bound. `ensemble.combine` holds that arithmetic and the argument
for it.

**The regime is read on both scales.** V3 classifies the 4-hour context and V4
classifies the base bar. Disorder on either is disorder: `combine_regimes` takes
the union, so a violent 15-minute bar inside a calm four hours still blocks an
entry. That is also why V5 introduces no volatility constant of its own - the
tolerance is the asset-class policy's `high_volatility_ratio`, applied through
the same `scoring.volatility_factor` V2 discounts confidence with, and the
higher timeframe's expansion has already entered through the regime.

**An uncalibrated model is refused at construction, not at the bar.** V4 reports
whether its probability was calibrated at all, and blending an uncalibrated
logistic score with a deterministic one as though it were odds is an unstated
assumption buried in a number the layers downstream may read as a probability.
The shipped ensemble refuses it when the engine is built; a deployment that
means to run one sets `requires_calibration=False` on the spec, and every
decision then carries a token saying so.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

import pandas as pd

from autotrader.decision.config import AssetClassPolicy
from autotrader.decision.contract import (
    VERSION_V5,
    DecisionConfigError,
    DecisionInputError,
    DecisionResult,
    DecisionSignal,
    MarketRegime,
)
from autotrader.decision.ensemble import (
    BALANCED_ENSEMBLE,
    COMPONENT_AGREEMENT,
    ENSEMBLE_CONTRACT_VERSION,
    REASON_UNCALIBRATED_MODEL,
    EnsembleAttribution,
    EnsembleSpec,
    agreement_reason,
    blended_score,
    combine,
    combine_regimes,
    contribution_reasons,
    decide_candidate,
)
from autotrader.decision.features import FEATURE_SCHEMA_VERSION
from autotrader.decision.probability import ProbabilityArtifact
from autotrader.decision.scoring import regime_reason, unavailable_reasons, volatility_factor
from autotrader.decision.timeframes import V3_TIMEFRAMES
from autotrader.decision.v3 import CONTEXT_TIMEFRAME, MultiTimeframeV3Engine
from autotrader.decision.v4 import ProbabilityAssessment, ProbabilityV4Engine

#: The score and confidence V5 reports when it could not be produced at all.
#: Zero rather than an absent value, matching what V2 and V3 report for an
#: unscorable timeframe - and paired with `available=False`, which is what a
#: consumer actually branches on.
UNSCORED_SCORE = 0.0
UNSCORED_CONFIDENCE = 0.0

REASON_DETERMINISTIC_UNAVAILABLE = "ENSEMBLE_COMPONENT_UNAVAILABLE_V3"
REASON_PROBABILISTIC_UNAVAILABLE = "ENSEMBLE_COMPONENT_UNAVAILABLE_V4"

#: The feature the volatility attenuation is read from, on the base bar. Named
#: rather than inlined because it is the one column this module reaches for by
#: name and a rename upstream should break here loudly.
VOLATILITY_FEATURE = "volatility_ratio"


@dataclass(frozen=True)
class EnsembleAssessment:
    """V5's complete reading of one symbol on one completed bar.

    Carries both sub-readings whole rather than copies of selected fields. A
    `DecisionResult` from V3 and a `ProbabilityAssessment` from V4 are each
    already a full audit record of their own engine, and an ensemble record that
    paraphrased them would be a third description of the same decision that can
    drift from both.

    `attribution` is `None` exactly when `available` is false. A bar that could
    not be scored has no contributions to explain, and a zeroed attribution
    would look like an explanation of a decision that was never computed.
    """

    symbol: str
    timestamp: pd.Timestamp
    available: bool
    ensemble_version: str
    deterministic: DecisionResult
    probabilistic: ProbabilityAssessment
    regime: MarketRegime
    reasons: tuple[str, ...]
    score: float = UNSCORED_SCORE
    confidence: float = UNSCORED_CONFIDENCE
    regime_multiplier: float = 0.0
    volatility_multiplier: float = 0.0
    attribution: EnsembleAttribution | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))
        if self.available and self.attribution is None:
            raise DecisionConfigError(
                "An available ensemble assessment must carry its attribution: a decision "
                "that cannot be taken apart into its inputs is not explainable."
            )
        if not self.available and self.attribution is not None:
            raise DecisionConfigError(
                "An unavailable ensemble assessment must not carry an attribution; a "
                "zeroed one would look like an explanation of a decision never computed."
            )

    @property
    def context_regime(self) -> MarketRegime:
        """The regime V3 classified on the context timeframe."""
        return self.deterministic.regime

    @property
    def base_regime(self) -> MarketRegime:
        """The regime V4 classified on the base bar."""
        return self.probabilistic.regime

    def to_dict(self) -> dict[str, object]:
        """A JSON-serializable record of this assessment, for audit and replay."""
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
            "available": self.available,
            "ensemble_contract_version": ENSEMBLE_CONTRACT_VERSION,
            "ensemble_version": self.ensemble_version,
            "score": self.score,
            "confidence": self.confidence,
            "regime": self.regime.value,
            "context_regime": self.context_regime.value,
            "base_regime": self.base_regime.value,
            "regime_multiplier": self.regime_multiplier,
            "volatility_multiplier": self.volatility_multiplier,
            "reasons": list(self.reasons),
            "attribution": None if self.attribution is None else self.attribution.to_record(),
            "deterministic": self.deterministic.to_dict(),
            "probabilistic": self.probabilistic.to_dict(),
        }


class EnsembleV5Engine:
    """The V5 decision engine: one V3, one V4, one named ensemble specification.

    Satisfies the `DecisionEngine` protocol, so an integrator that already
    drives V2, V3 or V4 drives this identically. `assess` is the addition, and
    it is where the attribution lives.

    Everything checkable is checked here rather than per bar: that the two
    sub-engines were built under one asset-class policy, that the ensemble band
    is no wider than that policy's own thresholds, and that the model is
    calibrated if the specification says it must be. A mismatch discovered on
    the thousandth bar of a backtest is the same error reported later and worse.
    """

    def __init__(
        self,
        *,
        deterministic: MultiTimeframeV3Engine,
        probabilistic: ProbabilityV4Engine,
        spec: EnsembleSpec = BALANCED_ENSEMBLE,
    ) -> None:
        policy = deterministic.policy
        if dict(policy.describe()) != dict(probabilistic.policy.describe()):
            raise DecisionConfigError(
                f"The V3 engine is configured under policy {policy.name!r} and the V4 "
                f"engine under {probabilistic.policy.name!r}. Blending two scores judged "
                "under different thresholds compares two policies, not two methods."
            )
        spec.band.require_not_wider_than_policy(policy.thresholds)
        if spec.requires_calibration and not probabilistic.artifact.calibrated:
            raise DecisionConfigError(
                f"Ensemble {spec.ensemble_version!r} requires a calibrated model, and "
                f"model {probabilistic.artifact.model_version!r} was fitted with "
                f"{probabilistic.artifact.calibration_method!r} calibration. An "
                "uncalibrated score is not a probability, and blending it with a "
                "deterministic score as though it were would be an unstated assumption "
                "inside a number the layers downstream may read as odds."
            )
        self._deterministic = deterministic
        self._probabilistic = probabilistic
        self._policy = policy
        self._spec = spec

    @classmethod
    def for_symbol(
        cls,
        symbol: str,
        artifact: ProbabilityArtifact,
        *,
        spec: EnsembleSpec = BALANCED_ENSEMBLE,
    ) -> EnsembleV5Engine:
        """Build the engine carrying the shipped policy for `symbol`'s asset class."""
        return cls(
            deterministic=MultiTimeframeV3Engine.for_symbol(symbol),
            probabilistic=ProbabilityV4Engine.for_symbol(symbol, artifact),
            spec=spec,
        )

    @property
    def deterministic(self) -> MultiTimeframeV3Engine:
        """The V3 engine supplying the deterministic half."""
        return self._deterministic

    @property
    def probabilistic(self) -> ProbabilityV4Engine:
        """The V4 engine supplying the calibrated probability."""
        return self._probabilistic

    @property
    def policy(self) -> AssetClassPolicy:
        """The asset-class policy both components are judged under."""
        return self._policy

    @property
    def spec(self) -> EnsembleSpec:
        """The named ensemble specification in force."""
        return self._spec

    @property
    def version(self) -> str:
        """The identifier stored with every decision this engine makes."""
        return VERSION_V5

    @property
    def required_base_bars(self) -> int:
        """Completed base bars needed before both components can answer.

        The larger of the two requirements, which is V3's, and it is large: the
        4-hour context governs. V5 does not decide on the bars V4 alone could
        have scored, because a decision made from one component is not this
        engine's decision, and the honest price of insisting on both is stated
        here rather than discovered as a permanent HOLD.
        """
        return max(self._deterministic.required_base_bars, self._probabilistic.required_base_bars)

    def describe(self) -> Mapping[str, object]:
        """The configuration in force, as serializable values.

        Enough to name the exact ensemble *and* the exact components that
        produced a decision: the ensemble version and every number under it, the
        two engine versions, and the trained model's own identity. A stored
        decision carrying this can be matched to what made it without consulting
        anything outside the record.
        """
        artifact = self._probabilistic.artifact
        return MappingProxyType(
            {
                "engine_version": self.version,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "required_base_bars": self.required_base_bars,
                "ensemble": dict(self._spec.describe()),
                "components": {
                    "deterministic": {
                        "engine_version": self._deterministic.version,
                        "timeframes": [timeframe.label for timeframe in V3_TIMEFRAMES],
                        "context_timeframe": CONTEXT_TIMEFRAME.label,
                    },
                    "probabilistic": {
                        "engine_version": self._probabilistic.version,
                        "model_version": artifact.model_version,
                        "model_family": artifact.family,
                        "feature_version": artifact.feature_version,
                        "label_spec_id": artifact.label_spec_id,
                        "calibration_method": artifact.calibration_method,
                        "calibrated": artifact.calibrated,
                    },
                },
                **dict(self._policy.describe()),
            }
        )

    def assess(self, bars: pd.DataFrame) -> EnsembleAssessment:
        """Read both components on the newest completed bar and combine them.

        The newest bar and nothing else, matching every other engine in this
        package. Both sub-engines are driven over the identical frame, so the
        two readings are of the same bar by construction - and the equality is
        asserted anyway, because a silent disagreement there would mean an
        ensemble of two different moments.
        """
        deterministic = self._deterministic.decide(bars)
        probabilistic = self._probabilistic.assess(bars)
        if deterministic.timestamp != probabilistic.timestamp:
            raise DecisionInputError(
                f"The V3 reading is for {deterministic.timestamp.isoformat()} and the V4 "
                f"reading for {probabilistic.timestamp.isoformat()}. An ensemble of two "
                "different bars is not an ensemble."
            )

        blocking = self._blocking_reasons(deterministic, probabilistic)
        if blocking:
            return self._unavailable(deterministic, probabilistic, blocking)

        regime = combine_regimes(deterministic.regime, probabilistic.regime)
        lean = blended_score(deterministic.score, probabilistic.score, self._spec.weights)
        regime_multiplier = self._spec.regime_adjustments.multiplier(regime, lean=lean)
        volatility_multiplier = volatility_factor(probabilistic.features, self._policy.thresholds)
        attribution = combine(
            deterministic_score=deterministic.score,
            deterministic_confidence=deterministic.confidence,
            probabilistic_score=probabilistic.score,
            probabilistic_confidence=probabilistic.confidence,
            regime_multiplier=regime_multiplier,
            volatility_multiplier=volatility_multiplier,
            spec=self._spec,
        )

        reasons = [
            agreement_reason(attribution.confidence_readings()[COMPONENT_AGREEMENT]),
            *contribution_reasons(attribution.score_components),
            regime_reason(regime),
            *deterministic.reasons,
            *probabilistic.reasons,
        ]
        if not self._probabilistic.artifact.calibrated:
            reasons.insert(0, REASON_UNCALIBRATED_MODEL)
        return EnsembleAssessment(
            symbol=deterministic.symbol,
            timestamp=deterministic.timestamp,
            available=True,
            ensemble_version=self._spec.ensemble_version,
            deterministic=deterministic,
            probabilistic=probabilistic,
            regime=regime,
            reasons=_unique(reasons),
            score=attribution.score,
            confidence=attribution.confidence,
            regime_multiplier=regime_multiplier,
            volatility_multiplier=volatility_multiplier,
            attribution=attribution,
        )

    def decide(self, bars: pd.DataFrame) -> DecisionResult:
        """Evaluate the newest completed bar and return the shared result shape."""
        assessment = self.assess(bars)
        if not assessment.available:
            return self._result(assessment, DecisionSignal.HOLD, assessment.reasons, len(bars))

        signal, band_reasons = decide_candidate(
            score=assessment.score,
            confidence=assessment.confidence,
            regime=assessment.regime,
            band=self._spec.band,
        )
        return self._result(
            assessment, signal, _unique([*band_reasons, *assessment.reasons]), len(bars)
        )

    def _blocking_reasons(
        self, deterministic: DecisionResult, probabilistic: ProbabilityAssessment
    ) -> tuple[str, ...]:
        """Which component could not answer, and the tokens saying why.

        Each component's own unavailability tokens are kept verbatim and led by
        one token naming the component, so an audit reads "V4 was missing, and
        here is the reason V4 gave" rather than a flat list in which the two
        engines' identical-looking history complaints cannot be told apart.
        """
        reasons: list[str] = []
        deterministic_blocked = unavailable_reasons(deterministic.reasons)
        if deterministic_blocked:
            reasons.append(REASON_DETERMINISTIC_UNAVAILABLE)
            reasons.extend(deterministic_blocked)
        if not probabilistic.available:
            reasons.append(REASON_PROBABILISTIC_UNAVAILABLE)
            reasons.extend(probabilistic.reasons)
        return _unique(reasons)

    def _unavailable(
        self,
        deterministic: DecisionResult,
        probabilistic: ProbabilityAssessment,
        reasons: tuple[str, ...],
    ) -> EnsembleAssessment:
        """The assessment for a bar one of the components could not read."""
        return EnsembleAssessment(
            symbol=deterministic.symbol,
            timestamp=deterministic.timestamp,
            available=False,
            ensemble_version=self._spec.ensemble_version,
            deterministic=deterministic,
            probabilistic=probabilistic,
            regime=MarketRegime.UNKNOWN,
            reasons=reasons,
        )

    def _result(
        self,
        assessment: EnsembleAssessment,
        signal: DecisionSignal,
        reasons: tuple[str, ...],
        bar_count: int,
    ) -> DecisionResult:
        """Wrap an assessment in the shared decision contract.

        The features are the base bar's, namespaced by the component that read
        them, so a V5 record can be taken apart the same way a V3 one can. The
        attribution goes into the policy metadata because that is the half of
        the record that survives `to_dict`, and an explanation nobody can read
        back off a stored decision explains nothing.
        """
        metadata = dict(self.describe())
        metadata["bar_count"] = bar_count
        metadata["ensemble_version"] = assessment.ensemble_version
        metadata["context_regime"] = assessment.context_regime.value
        metadata["base_regime"] = assessment.base_regime.value
        metadata["regime_multiplier"] = assessment.regime_multiplier
        metadata["volatility_multiplier"] = assessment.volatility_multiplier
        metadata["volatility_ratio"] = assessment.probabilistic.features.get(VOLATILITY_FEATURE)
        metadata["component_scores"] = {
            "deterministic": assessment.deterministic.score,
            "probabilistic": assessment.probabilistic.score,
        }
        metadata["component_confidence"] = {
            "deterministic": assessment.deterministic.confidence,
            "probabilistic": assessment.probabilistic.confidence,
        }
        metadata["probability_up"] = assessment.probabilistic.probability_up
        metadata["attribution"] = (
            None if assessment.attribution is None else assessment.attribution.to_record()
        )
        features = {
            f"v3.{name}": value for name, value in assessment.deterministic.features.items()
        }
        features.update(
            {f"v4.{name}": value for name, value in assessment.probabilistic.features.items()}
        )
        return DecisionResult(
            version=self.version,
            symbol=assessment.symbol,
            timestamp=assessment.timestamp,
            signal=signal,
            score=assessment.score,
            confidence=assessment.confidence,
            reasons=reasons,
            features=features,
            policy=metadata,
            regime=assessment.regime,
        )


def _unique(reasons: list[str]) -> tuple[str, ...]:
    """`reasons` with repeats removed, first occurrence kept.

    V5 is the first engine whose reasons come from two engines at once, and the
    two genuinely emit the same tokens - both classify a regime, and both
    complain about the base timeframe's history in identical words. A repeated
    token is not a second fact.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            ordered.append(reason)
    return tuple(ordered)


__all__ = [
    "REASON_DETERMINISTIC_UNAVAILABLE",
    "REASON_PROBABILISTIC_UNAVAILABLE",
    "UNSCORED_CONFIDENCE",
    "UNSCORED_SCORE",
    "VOLATILITY_FEATURE",
    "EnsembleAssessment",
    "EnsembleV5Engine",
]
