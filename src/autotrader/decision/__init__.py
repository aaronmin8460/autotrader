"""The versioned Decision Engine. Produces candidates; never places an order.

    Decision Engine -> DecisionResult -> Risk Engine -> Order Intent -> Execution

This package occupies the leftmost box and can reach none of the others. It
imports pandas, the standard library, `autotrader.equity` for the equity
universe and `autotrader.runtime.schedule` for the bar interval - and nothing
that holds a broker client, opens a socket, reads an account, or writes state.
The existing execution, risk and reconciliation layers remain authoritative
over whether a candidate ever becomes an order, and nothing here weakens,
reinterprets, or bypasses any of them (docs/SPEC.md section 7A).

**The versions.**

``v1`` The EMA 20 / EMA 50 crossover from C3, wrapped in the shared contract by
an adapter. The crossover itself is untouched and is still what the production
runtimes call directly.

``v2`` A deterministic multi-factor score on the base timeframe. Five bounded
directional factors across trend and momentum, with volatility, volume and a
regime classification acting on confidence and on entry rather than voting.

``v3`` The same framework read on 15 minutes, 1 hour and 4 hours, with the
higher timeframes derived from completed base bars and combined by explicit
gates rather than by averaging alone.

``v4`` A trained model over V2's seven unit-free measurements, emitting a
calibrated probability rather than a rule-based score. The model arrives as a
value - `probability.ProbabilityArtifact` - because this package may not read a
file; `autotrader.ml.v4` is what fits one and writes it.

``v5`` The ensemble. V3's deterministic score and V4's calibrated probability,
blended under a named, versioned specification and then attenuated by the market
regime and by the bar's own volatility - both as multipliers in ``[0, 1]``, so
context can take conviction away and can never manufacture it. Every decision
carries an exact decomposition into which input moved it and by how much, and an
explicit hold band, recorded with the decision, that resolves a boundary score to
HOLD rather than to whichever side is marginally ahead. `ensemble.py` is the
specification and the arithmetic; `v5.py` is the engine.

**Nothing here is activated.** No runtime prefers any of these versions, no
default selects one, and V5 in particular opens no gate: it produces a candidate
that the risk engine remains the sole authority over. Wiring an engine into a
runtime is a separate decision that no version in this package makes for itself.

**Boundaries this branch does not cross.** The research backtester belongs to
the quant-research branch, and V4's *training* - datasets, walk-forward
comparison, artifact registration - lives in `autotrader.ml.v4` rather than
here, because it needs numpy and the filesystem and this package is fenced off
from both. `features.compute_features` is the integration point - it is
vectorized, it takes a frame and returns a frame, and it holds no engine state -
and `contract.DecisionResult` is the shape their output should arrive in.
"""

from autotrader.decision.config import (
    CRYPTO_POLICY,
    EQUITY_POLICY,
    AssetClassPolicy,
    DecisionThresholds,
    FactorWeights,
    IndicatorPeriods,
    MultiTimeframeGates,
    TimeframePolicy,
    policy_for,
    policy_for_symbol,
)
from autotrader.decision.contract import (
    VERSION_V1,
    VERSION_V2,
    VERSION_V3,
    VERSION_V4,
    VERSION_V5,
    AssetClass,
    DecisionConfigError,
    DecisionEngine,
    DecisionError,
    DecisionInputError,
    DecisionResult,
    DecisionSignal,
    MarketRegime,
    resolve_asset_class,
)
from autotrader.decision.ensemble import (
    BALANCED_ENSEMBLE,
    ENSEMBLE_CONTRACT_VERSION,
    ComponentContribution,
    ConfidenceMix,
    EnsembleAttribution,
    EnsembleBand,
    EnsembleSpec,
    EnsembleWeights,
    RegimeAdjustments,
    combine_regimes,
    component_agreement,
)
from autotrader.decision.features import (
    FEATURE_COLUMNS,
    FEATURE_SCHEMA_VERSION,
    SCORED_FEATURES,
    compute_features,
)
from autotrader.decision.probability import (
    PROBABILITY_CONTRACT_VERSION,
    V4_FEATURE_COLUMNS,
    ClassFrequencyEstimator,
    FeatureStandardizer,
    GradientBoostedEstimator,
    IdentityCalibration,
    IsotonicCalibration,
    LogisticEstimator,
    ProbabilityArtifact,
    ProbabilityModelError,
    TrainingWindow,
    artifact_from_record,
)
from autotrader.decision.timeframes import (
    BASE_TIMEFRAME,
    FOUR_HOUR_TIMEFRAME,
    HOUR_TIMEFRAME,
    V3_TIMEFRAMES,
    TimeframeSpec,
    aggregate_bars,
    align_timeframes,
    usable_history,
)
from autotrader.decision.v1 import EmaCrossV1Engine, to_legacy_signal
from autotrader.decision.v2 import MultiFactorV2Engine, TimeframeEvaluation, evaluate_timeframe
from autotrader.decision.v3 import MultiTimeframeV3Engine
from autotrader.decision.v4 import ProbabilityAssessment, ProbabilityV4Engine
from autotrader.decision.v5 import EnsembleAssessment, EnsembleV5Engine

__all__ = [
    "BALANCED_ENSEMBLE",
    "BASE_TIMEFRAME",
    "CRYPTO_POLICY",
    "ENSEMBLE_CONTRACT_VERSION",
    "EQUITY_POLICY",
    "FEATURE_COLUMNS",
    "FEATURE_SCHEMA_VERSION",
    "FOUR_HOUR_TIMEFRAME",
    "HOUR_TIMEFRAME",
    "PROBABILITY_CONTRACT_VERSION",
    "SCORED_FEATURES",
    "V3_TIMEFRAMES",
    "V4_FEATURE_COLUMNS",
    "VERSION_V1",
    "VERSION_V2",
    "VERSION_V3",
    "VERSION_V4",
    "VERSION_V5",
    "AssetClass",
    "AssetClassPolicy",
    "ClassFrequencyEstimator",
    "ComponentContribution",
    "ConfidenceMix",
    "DecisionConfigError",
    "DecisionEngine",
    "DecisionError",
    "DecisionInputError",
    "DecisionResult",
    "DecisionSignal",
    "DecisionThresholds",
    "EmaCrossV1Engine",
    "EnsembleAssessment",
    "EnsembleAttribution",
    "EnsembleBand",
    "EnsembleSpec",
    "EnsembleV5Engine",
    "EnsembleWeights",
    "FactorWeights",
    "FeatureStandardizer",
    "GradientBoostedEstimator",
    "IdentityCalibration",
    "IndicatorPeriods",
    "IsotonicCalibration",
    "LogisticEstimator",
    "MarketRegime",
    "MultiFactorV2Engine",
    "MultiTimeframeGates",
    "MultiTimeframeV3Engine",
    "ProbabilityArtifact",
    "ProbabilityAssessment",
    "ProbabilityModelError",
    "ProbabilityV4Engine",
    "RegimeAdjustments",
    "TimeframeEvaluation",
    "TimeframePolicy",
    "TimeframeSpec",
    "TrainingWindow",
    "aggregate_bars",
    "align_timeframes",
    "artifact_from_record",
    "combine_regimes",
    "component_agreement",
    "compute_features",
    "evaluate_timeframe",
    "policy_for",
    "policy_for_symbol",
    "resolve_asset_class",
    "to_legacy_signal",
    "usable_history",
]
