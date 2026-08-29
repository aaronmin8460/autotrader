"""V4: the same measurements, judged by a trained model instead of by a rule.

V2 and V3 decide with weights and thresholds that were written down by hand and
argued for in `config.py`. V4 reads the identical seven measurements and asks a
different question of them: given what this market did after bars that looked
like this one, how often did the next interval go up? The answer is a
probability, and a calibrated one, so the layers downstream can treat it as the
number it claims to be rather than as a score that happens to live in ``[0, 1]``.

**The model is data, and the engine holds it as a value.** `ProbabilityArtifact`
arrives already built - from `autotrader.ml.v4`, from a test fixture, from a
registry read performed by whoever constructed the engine - and this module
never touches a file to obtain one. That is not a style choice: the decision
package is forbidden to read the filesystem, and pushing the read outward is
what keeps a training pipeline's dependencies out of a process that trades.

**Nothing here is fitted at decision time.** The standardizer's constants, the
coefficients or trees, and the calibration curve were all fitted on training
folds, are all frozen inside the artifact, and are all applied unchanged.
`decide` is a pure function of the bars and the artifact, which is what makes a
replay of a stored decision reproduce it exactly.

**The gates are V2's gates, deliberately.** Once the probability becomes a
score, the direction is decided by `scoring.decide_signal` under the same
asset-class policy V2 and V3 use: the same confidence floor, the same hold band,
the same asymmetric refusal to enter a high-volatility regime. Reusing them
means V4 differs from V2 in exactly one respect - how the score was arrived at -
and a comparison between the two measures that difference rather than a pile of
incidental threshold changes. It also honours the rule that this branch invents
no session or policy semantics: crypto and equity keep the tolerances already
argued for in `config.py`.

**Score and confidence come from the probability, and coincide by construction.**
The score is ``2p - 1``, which maps an even-odds probability to zero and
certainty to the bounds. The confidence is ``|2p - 1|``, so a model saying 50/50
reports no confidence rather than half of it. For a single calibrated binary
probability the two are the same magnitude, and that is a fact about what a
probability is rather than an oversight: unlike V2, where confidence measures
whether five separate factors agreed, there is only one quantity here and it
cannot corroborate itself.

**An uncalibrated model still answers, and still says that it is uncalibrated.**
`ProbabilityAssessment.calibrated` and a `CALIBRATION_IDENTITY` reason token
travel with every result, so a V5 ensemble can weight - or refuse - a raw score
on purpose instead of discovering the situation from a sizing decision that went
strangely.

**What V5 gets, and what it does not.** `assess` returns the probability, the
model version, the feature version, the reasons, and the measurements they were
read from. That is the whole surface, and it is enough to combine V4 with V3's
deterministic score without reaching into either. V5 does not need this module's
internals, and this module knows nothing about being ensembled.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

import pandas as pd

from autotrader.decision.bars import normalize_bars
from autotrader.decision.config import AssetClassPolicy, policy_for_symbol
from autotrader.decision.contract import (
    VERSION_V4,
    DecisionResult,
    DecisionSignal,
    MarketRegime,
)
from autotrader.decision.features import (
    FEATURE_SCHEMA_VERSION,
    compute_features,
    latest_feature_row,
    missing_scored_features,
)
from autotrader.decision.probability import (
    V4_FEATURE_COLUMNS,
    ProbabilityArtifact,
)
from autotrader.decision.scoring import (
    classify_regime,
    context_reasons,
    decide_signal,
    feature_unavailable_reason,
    insufficient_history_reason,
    regime_reason,
)
from autotrader.decision.timeframes import BASE_TIMEFRAME
from autotrader.decision.v2 import require_policy_matches_symbol
from autotrader.runtime.schedule import BAR_INTERVAL

#: The probability of an even-odds bar. Named because it is the origin of the
#: score scale and the point at which the engine has no opinion, not because
#: 0.5 is hard to type.
EVEN_ODDS = 0.5

#: How far from even odds a probability must sit before its direction token is
#: reported as a direction rather than as balance. Small: this decides the
#: wording of an audit token, never whether a trade happens - the hold band in
#: the asset-class policy does that, and does it on the score.
DIRECTION_EPSILON = 1e-9

#: How many feature drivers a linear model's reasons name. Two, because the
#: point of the tokens is to say what moved this bar, and a list of all seven is
#: the coefficient vector rather than an explanation.
REPORTED_DRIVERS = 2

REASON_MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"


def model_reason(family: str) -> str:
    """The stable token naming which estimator family produced this answer."""
    return f"MODEL_{family.upper()}"


def calibration_reason(method: str) -> str:
    """The stable token naming how - or whether - the probability was calibrated."""
    return f"CALIBRATION_{method.upper()}"


def probability_reason(probability: float) -> str:
    """The stable token naming which side of even odds the probability fell."""
    if probability > EVEN_ODDS + DIRECTION_EPSILON:
        return "PROBABILITY_ABOVE_EVEN"
    if probability < EVEN_ODDS - DIRECTION_EPSILON:
        return "PROBABILITY_BELOW_EVEN"
    return "PROBABILITY_EVEN"


def driver_reason(feature: str, contribution: float) -> str:
    """The stable token naming one feature's direction of influence on this bar."""
    if contribution > 0.0:
        direction = "BULLISH"
    elif contribution < 0.0:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"
    return f"DRIVER_{feature.upper()}_{direction}"


def score_from_probability(probability: float) -> float:
    """Map a probability in ``[0, 1]`` onto the contract's ``[-1, +1]`` score.

    Affine and centred on even odds, so the sign of the score is the side the
    model favours and zero means it favours neither. Clamped only against
    floating-point drift past a bound the arithmetic already respects.
    """
    return max(-1.0, min(1.0, 2.0 * float(probability) - 1.0))


@dataclass(frozen=True)
class ProbabilityAssessment:
    """V4's answer about one symbol on one completed bar. The contract V5 consumes.

    Deliberately a separate value from `DecisionResult`. A `DecisionResult`
    carries a direction and a bounded score, which is what the risk engine and
    an audit record want; an ensemble wants the probability itself, the identity
    of the model that produced it, and the identity of the feature contract it
    was read under, so that combining V4 with V3 is arithmetic on two known
    quantities rather than an inference from two scores of unknown provenance.

    `probability_up` is `None` exactly when `available` is false. A bar with too
    little history behind it has no probability, and reporting even odds for one
    would be a measurement rather than an absence - the one substitution that
    would be invisible to every consumer downstream.
    """

    symbol: str
    timestamp: pd.Timestamp
    knowable_at: pd.Timestamp
    available: bool
    model_version: str
    model_family: str
    feature_version: str
    label_spec_id: str
    calibration_method: str
    calibrated: bool
    reasons: tuple[str, ...]
    features: Mapping[str, float]
    probability_up: float | None = None
    uncalibrated_probability_up: float | None = None
    feature_contributions: Mapping[str, float] = field(default_factory=dict)
    regime: MarketRegime = MarketRegime.UNKNOWN

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))
        object.__setattr__(self, "features", MappingProxyType(dict(self.features)))
        object.__setattr__(
            self, "feature_contributions", MappingProxyType(dict(self.feature_contributions))
        )
        if self.available and self.probability_up is None:
            raise ValueError("An available assessment must carry a probability.")
        if not self.available and self.probability_up is not None:
            raise ValueError("An unavailable assessment must not carry a probability.")

    @property
    def probability_down(self) -> float | None:
        """The complement, for a consumer that would otherwise compute it itself."""
        return None if self.probability_up is None else 1.0 - float(self.probability_up)

    @property
    def score(self) -> float:
        """The probability on the contract's ``[-1, +1]`` scale. Zero when unavailable."""
        if self.probability_up is None:
            return 0.0
        return score_from_probability(self.probability_up)

    @property
    def confidence(self) -> float:
        """How far from even odds the model is, in ``[0, 1]``. Zero when unavailable."""
        return abs(self.score)

    def to_dict(self) -> dict[str, object]:
        """A JSON-serializable record of this assessment, for audit and replay."""
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
            "knowable_at": self.knowable_at.isoformat(),
            "available": self.available,
            "model_version": self.model_version,
            "model_family": self.model_family,
            "feature_version": self.feature_version,
            "label_spec_id": self.label_spec_id,
            "calibration_method": self.calibration_method,
            "calibrated": self.calibrated,
            "probability_up": self.probability_up,
            "probability_down": self.probability_down,
            "uncalibrated_probability_up": self.uncalibrated_probability_up,
            "score": self.score,
            "confidence": self.confidence,
            "regime": self.regime.value,
            "reasons": list(self.reasons),
            "features": dict(self.features),
            "feature_contributions": dict(self.feature_contributions),
        }


class ProbabilityV4Engine:
    """The V4 decision engine: one trained model, one asset-class policy.

    Satisfies the `DecisionEngine` protocol, so an integrator that already
    drives V2 or V3 drives this identically. `assess` is the addition, and it is
    what V5 will consume.

    The artifact's feature contract is checked once, here, rather than on every
    bar. A model that cannot be applied to the features this package computes is
    a configuration error at construction, and discovering it on the thousandth
    bar of a backtest instead would be the same error reported later and worse.
    """

    def __init__(self, artifact: ProbabilityArtifact, policy: AssetClassPolicy) -> None:
        artifact.require_compatible_with(
            feature_version=FEATURE_SCHEMA_VERSION, feature_columns=V4_FEATURE_COLUMNS
        )
        self._artifact = artifact
        self._policy = policy

    @classmethod
    def for_symbol(cls, symbol: str, artifact: ProbabilityArtifact) -> ProbabilityV4Engine:
        """Build the engine carrying the shipped policy for `symbol`'s asset class."""
        return cls(artifact, policy_for_symbol(symbol))

    @property
    def artifact(self) -> ProbabilityArtifact:
        """The trained model in force."""
        return self._artifact

    @property
    def policy(self) -> AssetClassPolicy:
        """The asset-class policy in force."""
        return self._policy

    @property
    def version(self) -> str:
        """The identifier stored with every decision this engine makes."""
        return VERSION_V4

    @property
    def required_base_bars(self) -> int:
        """Completed base bars needed before a probability is possible.

        V2's requirement exactly, because V4 reads V2's features on V2's single
        timeframe. A model does not shorten a warm-up: a standardized feature is
        undefined until its window is full, whatever is going to consume it.
        """
        return self._policy.required_base_bars((BASE_TIMEFRAME.label,))

    def describe(self) -> Mapping[str, object]:
        """The configuration in force, as serializable values."""
        return MappingProxyType(
            {
                "engine_version": self.version,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "feature_columns": list(V4_FEATURE_COLUMNS),
                "timeframes": [BASE_TIMEFRAME.label],
                "required_base_bars": self.required_base_bars,
                "model_version": self._artifact.model_version,
                "model_family": self._artifact.family,
                "label_spec_id": self._artifact.label_spec_id,
                "calibration": dict(self._artifact.calibration.to_record()),
                "calibrated": self._artifact.calibrated,
                "trained_at_utc": self._artifact.trained_at_utc,
                "training_window": self._artifact.training_window.to_record(),
                "code_revision": dict(self._artifact.code_revision),
                "hyperparameters": dict(self._artifact.hyperparameters),
                "training_metrics": dict(self._artifact.metrics),
                "seed": self._artifact.seed,
                **dict(self._policy.describe()),
            }
        )

    def assess(self, bars: pd.DataFrame) -> ProbabilityAssessment:
        """Produce the calibrated probability for the newest completed bar in `bars`.

        The newest bar and nothing else, matching every other engine in this
        package. Older bars are the indicator state that bar needs; they are not
        a backlog to be scored, and a research harness that wants every bar
        should drive the vectorized feature layer directly.
        """
        frame = normalize_bars(bars)
        symbol = str(frame["symbol"].iloc[0])
        require_policy_matches_symbol(symbol, self._policy)
        timestamp = pd.Timestamp(frame["timestamp"].iloc[-1])
        periods = self._policy.timeframe(BASE_TIMEFRAME.label).periods

        if len(frame) < periods.required_bars:
            return self._unavailable(
                symbol=symbol,
                timestamp=timestamp,
                reasons=(insufficient_history_reason(BASE_TIMEFRAME.reason_token),),
                features={},
            )

        features = compute_features(frame, periods=periods)
        row = latest_feature_row(features)
        missing = missing_scored_features(row)
        if missing:
            return self._unavailable(
                symbol=symbol,
                timestamp=timestamp,
                reasons=(feature_unavailable_reason(BASE_TIMEFRAME.reason_token),),
                features=row,
            )

        values = [float(row[name]) for name in V4_FEATURE_COLUMNS]
        probability = self._artifact.probability_up(values)
        uncalibrated = self._artifact.uncalibrated_probability_up(values)
        contributions = self._artifact.feature_contributions(values)
        regime = classify_regime(row, self._policy.thresholds)

        reasons = (
            probability_reason(probability),
            model_reason(self._artifact.family),
            calibration_reason(self._artifact.calibration_method),
            *_driver_reasons(contributions),
            regime_reason(regime),
            *context_reasons(row, self._policy.thresholds),
        )
        return ProbabilityAssessment(
            symbol=symbol,
            timestamp=timestamp,
            knowable_at=timestamp + BAR_INTERVAL,
            available=True,
            model_version=self._artifact.model_version,
            model_family=self._artifact.family,
            feature_version=self._artifact.feature_version,
            label_spec_id=self._artifact.label_spec_id,
            calibration_method=self._artifact.calibration_method,
            calibrated=self._artifact.calibrated,
            reasons=reasons,
            features=row,
            probability_up=probability,
            uncalibrated_probability_up=uncalibrated,
            feature_contributions=contributions,
            regime=regime,
        )

    def decide(self, bars: pd.DataFrame) -> DecisionResult:
        """Evaluate the newest completed bar and return the shared result shape."""
        assessment = self.assess(bars)
        if not assessment.available:
            return self._result(assessment, DecisionSignal.HOLD, assessment.reasons, len(bars))

        signal, gate_reasons = decide_signal(
            score=assessment.score,
            confidence=assessment.confidence,
            regime=assessment.regime,
            thresholds=self._policy.thresholds,
        )
        return self._result(assessment, signal, (*gate_reasons, *assessment.reasons), len(bars))

    def _unavailable(
        self,
        *,
        symbol: str,
        timestamp: pd.Timestamp,
        reasons: tuple[str, ...],
        features: Mapping[str, float],
    ) -> ProbabilityAssessment:
        """The assessment for a bar that cannot be scored, and why."""
        return ProbabilityAssessment(
            symbol=symbol,
            timestamp=timestamp,
            knowable_at=timestamp + BAR_INTERVAL,
            available=False,
            model_version=self._artifact.model_version,
            model_family=self._artifact.family,
            feature_version=self._artifact.feature_version,
            label_spec_id=self._artifact.label_spec_id,
            calibration_method=self._artifact.calibration_method,
            calibrated=self._artifact.calibrated,
            reasons=reasons,
            features=features,
        )

    def _result(
        self,
        assessment: ProbabilityAssessment,
        signal: DecisionSignal,
        reasons: tuple[str, ...],
        bar_count: int,
    ) -> DecisionResult:
        """Wrap an assessment in the shared decision contract."""
        metadata = dict(self.describe())
        metadata["bar_count"] = bar_count
        metadata["probability_up"] = assessment.probability_up
        metadata["uncalibrated_probability_up"] = assessment.uncalibrated_probability_up
        metadata["feature_contributions"] = dict(assessment.feature_contributions)
        return DecisionResult(
            version=self.version,
            symbol=assessment.symbol,
            timestamp=assessment.timestamp,
            signal=signal,
            score=assessment.score,
            confidence=assessment.confidence,
            reasons=reasons,
            features=assessment.features,
            policy=metadata,
            regime=assessment.regime,
        )


def _driver_reasons(contributions: Mapping[str, float]) -> tuple[str, ...]:
    """Tokens for the features that moved this bar's log-odds the most.

    Sorted by absolute contribution, then by name so that two features of equal
    influence are reported in a stable order rather than in whichever order the
    mapping happened to be built. Empty for an estimator that has no exact
    per-feature attribution, which is the honest answer for a tree ensemble.
    """
    if not contributions:
        return ()
    ranked = sorted(contributions.items(), key=lambda item: (-abs(item[1]), item[0]))
    return tuple(
        driver_reason(name, value) for name, value in ranked[:REPORTED_DRIVERS] if value != 0.0
    )


__all__ = [
    "DIRECTION_EPSILON",
    "EVEN_ODDS",
    "REASON_MODEL_UNAVAILABLE",
    "REPORTED_DRIVERS",
    "ProbabilityAssessment",
    "ProbabilityV4Engine",
    "calibration_reason",
    "driver_reason",
    "model_reason",
    "probability_reason",
    "score_from_probability",
]
