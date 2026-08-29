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

V4 (a probability model) and V5 (an ensemble) are anticipated and deliberately
absent. What they need in order to exist is here: one result shape, one
vectorized feature layer they can train on and score from, and one policy
object describing the configuration a stored decision was made under.

**Boundaries this branch does not cross.** The research backtester belongs to
the quant-research branch and model training to the ml-foundation branch.
Neither is implemented here. `features.compute_features` is the integration
point for both - it is vectorized, it takes a frame and returns a frame, and it
holds no engine state - and `contract.DecisionResult` is the shape their output
should arrive in.
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
from autotrader.decision.features import (
    FEATURE_COLUMNS,
    FEATURE_SCHEMA_VERSION,
    SCORED_FEATURES,
    compute_features,
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

__all__ = [
    "BASE_TIMEFRAME",
    "CRYPTO_POLICY",
    "EQUITY_POLICY",
    "FEATURE_COLUMNS",
    "FEATURE_SCHEMA_VERSION",
    "FOUR_HOUR_TIMEFRAME",
    "HOUR_TIMEFRAME",
    "SCORED_FEATURES",
    "V3_TIMEFRAMES",
    "VERSION_V1",
    "VERSION_V2",
    "VERSION_V3",
    "AssetClass",
    "AssetClassPolicy",
    "DecisionConfigError",
    "DecisionEngine",
    "DecisionError",
    "DecisionInputError",
    "DecisionResult",
    "DecisionSignal",
    "DecisionThresholds",
    "EmaCrossV1Engine",
    "FactorWeights",
    "IndicatorPeriods",
    "MarketRegime",
    "MultiFactorV2Engine",
    "MultiTimeframeGates",
    "MultiTimeframeV3Engine",
    "TimeframeEvaluation",
    "TimeframePolicy",
    "TimeframeSpec",
    "aggregate_bars",
    "align_timeframes",
    "compute_features",
    "evaluate_timeframe",
    "policy_for",
    "policy_for_symbol",
    "resolve_asset_class",
    "to_legacy_signal",
    "usable_history",
]
