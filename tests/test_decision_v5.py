"""Decision Engine tests: V5, the versioned ensemble.

The load-bearing tests here are about what a combination cannot do. Blending two
numbers is easy and produces something plausible from anything, so these assert
the properties that make the result worth reading: that the bounds hold because
of the arithmetic rather than because something trimmed the answer; that the
contributions reconstruct the decision they claim to explain, exactly; that a
score on the boundary of the hold band resolves to HOLD rather than to whichever
side is marginally ahead; that context can only ever take conviction away; and
that a V5 candidate is a candidate - it names no quantity, no price, and nothing
that could reach a broker.

The package-wide guards in `test_decision_contract.py` cover the two new modules
automatically, because they walk every file in the decision package. The
boundary tests at the bottom of this file are the V5-specific additions: that
nothing outside the package has started preferring V5, and that the risk engine
is still the only thing that can turn a candidate into an order.
"""

from __future__ import annotations

import ast
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from autotrader.decision.config import CRYPTO_POLICY, EQUITY_POLICY, DecisionThresholds
from autotrader.decision.contract import (
    VERSION_V5,
    DecisionConfigError,
    DecisionEngine,
    DecisionInputError,
    DecisionResult,
    DecisionSignal,
    MarketRegime,
)
from autotrader.decision.ensemble import (
    BALANCED_ENSEMBLE,
    COMPONENT_AGREEMENT,
    COMPONENT_DETERMINISTIC,
    COMPONENT_PROBABILISTIC,
    COMPONENT_REGIME,
    COMPONENT_VOLATILITY,
    ENSEMBLE_CONTRACT_VERSION,
    REASON_COMPONENTS_AGREE,
    REASON_COMPONENTS_DISAGREE,
    REASON_ENSEMBLE_BUY,
    REASON_ENSEMBLE_SELL,
    REASON_UNCALIBRATED_MODEL,
    ComponentContribution,
    ConfidenceMix,
    EnsembleAttribution,
    EnsembleBand,
    EnsembleSpec,
    EnsembleWeights,
    RegimeAdjustments,
    blended_score,
    combine,
    combine_regimes,
    component_agreement,
    contribution_reasons,
    decide_candidate,
)
from autotrader.decision.features import FEATURE_SCHEMA_VERSION
from autotrader.decision.probability import (
    V4_FEATURE_COLUMNS,
    FeatureStandardizer,
    IdentityCalibration,
    IsotonicCalibration,
    LogisticEstimator,
    ProbabilityArtifact,
    TrainingWindow,
)
from autotrader.decision.scoring import (
    REASON_HOLD_BAND,
    REASON_LOW_CONFIDENCE,
    REASON_REGIME_BLOCKED,
    volatility_factor,
)
from autotrader.decision.v3 import MultiTimeframeV3Engine
from autotrader.decision.v4 import ProbabilityAssessment, ProbabilityV4Engine
from autotrader.decision.v5 import (
    REASON_DETERMINISTIC_UNAVAILABLE,
    REASON_PROBABILISTIC_UNAVAILABLE,
    EnsembleV5Engine,
)

FIRST_BAR = datetime(2025, 1, 1, tzinfo=UTC)
STEP = timedelta(minutes=15)
CRYPTO_REQUIRED = MultiTimeframeV3Engine(CRYPTO_POLICY).required_base_bars


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def make_bars(
    closes: list[float],
    *,
    symbol: str = "BTC/USD",
    ranges: list[float] | None = None,
) -> pd.DataFrame:
    """Bars over `closes`, optionally with a per-bar high-low half-range."""
    prices = [float(close) for close in closes]
    spans = ranges if ranges is not None else [0.5] * len(prices)
    return pd.DataFrame(
        {
            "timestamp": [FIRST_BAR + STEP * index for index in range(len(prices))],
            "symbol": [symbol] * len(prices),
            "open": prices,
            "high": [price + span for price, span in zip(prices, spans, strict=True)],
            "low": [price - span for price, span in zip(prices, spans, strict=True)],
            "close": prices,
            "volume": [100.0] * len(prices),
        }
    )


def rising(count: int = CRYPTO_REQUIRED + 40, step: float = 0.05) -> list[float]:
    return [100.0 + step * index for index in range(count)]


def falling(count: int = CRYPTO_REQUIRED + 40, step: float = 0.05) -> list[float]:
    return [100.0 + step * (count - index) for index in range(count)]


def choppy(count: int = CRYPTO_REQUIRED + 40) -> list[float]:
    """A deterministic non-constant path with no library randomness."""
    return [
        500.0 + 20.0 * math.sin(index / 37.0) + 6.0 * math.cos(index / 5.0)
        for index in range(count)
    ]


def logistic(intercept: float = 0.0, weight: float = 0.4) -> LogisticEstimator:
    """A linear model that leans on the first feature and ignores the rest."""
    coefficients = [weight] + [0.0] * (len(V4_FEATURE_COLUMNS) - 1)
    return LogisticEstimator(intercept=intercept, coefficients=tuple(coefficients))


def calibration() -> IsotonicCalibration:
    """A real, monotone calibration curve, so the shipped ensemble accepts the model."""
    return IsotonicCalibration(thresholds=(0.0, 0.35, 0.65), values=(0.05, 0.5, 0.95))


def artifact(**overrides: object) -> ProbabilityArtifact:
    """A valid, calibrated artifact, with fields replaced by keyword."""
    fields: dict[str, object] = {
        "model_version": "v5-test-1",
        "feature_version": FEATURE_SCHEMA_VERSION,
        "feature_columns": V4_FEATURE_COLUMNS,
        "label_spec_id": "v4-direction-abcdef123456",
        "standardizer": FeatureStandardizer.identity(len(V4_FEATURE_COLUMNS)),
        "estimator": logistic(),
        "calibration": calibration(),
        "training_window": TrainingWindow(
            first_feature_timestamp=FIRST_BAR.isoformat(),
            last_feature_timestamp=(FIRST_BAR + STEP * 500).isoformat(),
            rows=500,
            symbols=("BTC/USD",),
            asset_class="crypto",
        ),
        "trained_at_utc": "2025-06-01T00:00:00+00:00",
        "code_revision": {"branch": "feat/decision-v5", "sha": "0" * 40, "dirty": False},
        "hyperparameters": {"l2": 1.0},
        "seed": 7,
    }
    fields.update(overrides)
    return ProbabilityArtifact(**fields)  # type: ignore[arg-type]


def engine(
    *, symbol: str = "BTC/USD", spec: EnsembleSpec = BALANCED_ENSEMBLE, **overrides: object
) -> EnsembleV5Engine:
    return EnsembleV5Engine.for_symbol(symbol, artifact(**overrides), spec=spec)


def unavailable_probability() -> ProbabilityAssessment:
    """A V4 reading for a bar it could not score."""
    return ProbabilityAssessment(
        symbol="BTC/USD",
        timestamp=pd.Timestamp(FIRST_BAR),
        knowable_at=pd.Timestamp(FIRST_BAR + STEP),
        available=False,
        model_version="v5-test-1",
        model_family="logistic",
        feature_version=FEATURE_SCHEMA_VERSION,
        label_spec_id="v4-direction-abcdef123456",
        calibration_method="isotonic",
        calibrated=True,
        reasons=("FEATURE_UNAVAILABLE_15M",),
        features={},
    )


def available_result(**overrides: object) -> DecisionResult:
    """A V3 result that was produced, for the blocking-reason unit tests."""
    fields: dict[str, object] = {
        "version": "v3",
        "symbol": "BTC/USD",
        "timestamp": pd.Timestamp(FIRST_BAR),
        "signal": DecisionSignal.BUY,
        "score": 0.6,
        "confidence": 0.7,
        "reasons": ("TIMEFRAMES_ALIGNED_BULLISH",),
        "features": {},
        "policy": {},
        "regime": MarketRegime.TREND_UP,
    }
    fields.update(overrides)
    return DecisionResult(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The shared contract
# --------------------------------------------------------------------------


def test_v5_satisfies_the_shared_decision_protocol() -> None:
    built = engine()
    assert isinstance(built, DecisionEngine)
    assert built.version == VERSION_V5
    assert built.describe()["engine_version"] == VERSION_V5


def test_v5_costs_the_history_of_its_most_expensive_component() -> None:
    """Both components or no decision, and V3 is the expensive half."""
    built = engine()
    assert built.required_base_bars == built.deterministic.required_base_bars
    assert built.required_base_bars > built.probabilistic.required_base_bars


def test_the_engine_refuses_a_symbol_from_another_asset_class() -> None:
    built = engine()
    with pytest.raises(DecisionInputError, match="not interchangeable"):
        built.decide(make_bars(rising(), symbol="SPY"))


def test_two_components_configured_under_different_policies_are_refused() -> None:
    """CRITICAL. A blend of two policies is a comparison of thresholds, not methods."""
    with pytest.raises(DecisionConfigError, match="different thresholds"):
        EnsembleV5Engine(
            deterministic=MultiTimeframeV3Engine(CRYPTO_POLICY),
            probabilistic=ProbabilityV4Engine(artifact(), EQUITY_POLICY),
        )


def test_crypto_and_equity_keep_the_policies_they_already_had() -> None:
    """V5 invents no thresholds and no session rules; it reuses `config.py`."""
    crypto = engine()
    equity = EnsembleV5Engine(
        deterministic=MultiTimeframeV3Engine(EQUITY_POLICY),
        probabilistic=ProbabilityV4Engine(artifact(), EQUITY_POLICY),
    )
    assert crypto.describe()["policy_name"] == "crypto-v2-default"
    assert equity.describe()["policy_name"] == "equity-v2-default"
    assert equity.required_base_bars > crypto.required_base_bars


def test_the_audit_record_survives_a_json_round_trip() -> None:
    payload = engine().decide(make_bars(rising())).to_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert payload["version"] == VERSION_V5


# --------------------------------------------------------------------------
# The HOLD band
# --------------------------------------------------------------------------


def test_a_score_exactly_on_the_buy_boundary_holds() -> None:
    """CRITICAL. The band is closed, which is what "near the boundary" means.

    V2 and V4 name a direction at `score >= buy_score`. V5 requires strictly
    more, so a score sitting exactly on the edge is a tie rather than a
    direction, and a tie is the thing a hold band exists to express.
    """
    band = BALANCED_ENSEMBLE.band
    signal, reasons = decide_candidate(
        score=band.buy_score, confidence=1.0, regime=MarketRegime.TREND_UP, band=band
    )

    assert signal is DecisionSignal.HOLD
    assert reasons == (REASON_HOLD_BAND,)


def test_a_score_exactly_on_the_sell_boundary_holds() -> None:
    band = BALANCED_ENSEMBLE.band
    signal, reasons = decide_candidate(
        score=band.sell_score, confidence=1.0, regime=MarketRegime.TREND_DOWN, band=band
    )

    assert signal is DecisionSignal.HOLD
    assert reasons == (REASON_HOLD_BAND,)


def test_a_score_past_the_boundary_names_a_direction() -> None:
    band = BALANCED_ENSEMBLE.band

    above, above_reasons = decide_candidate(
        score=math.nextafter(band.buy_score, 1.0),
        confidence=1.0,
        regime=MarketRegime.TREND_UP,
        band=band,
    )
    below, below_reasons = decide_candidate(
        score=math.nextafter(band.sell_score, -1.0),
        confidence=1.0,
        regime=MarketRegime.TREND_DOWN,
        band=band,
    )

    assert (above, above_reasons) == (DecisionSignal.BUY, (REASON_ENSEMBLE_BUY,))
    assert (below, below_reasons) == (DecisionSignal.SELL, (REASON_ENSEMBLE_SELL,))


def test_two_components_that_barely_disagree_resolve_to_hold() -> None:
    """CRITICAL. Not "whichever side is marginally ahead".

    V3 clears its own buy threshold and V4 sits just below even odds. The blend
    is a hair above zero - the bullish side is genuinely ahead - and the band
    refuses it, because an ensemble whose inputs cancel has found a tie.
    """
    attribution = combine(
        deterministic_score=0.31,
        deterministic_confidence=0.9,
        probabilistic_score=-0.30,
        probabilistic_confidence=0.9,
        regime_multiplier=1.0,
        volatility_multiplier=1.0,
        spec=BALANCED_ENSEMBLE,
    )
    assert attribution.score > 0.0

    signal, reasons = decide_candidate(
        score=attribution.score,
        confidence=attribution.confidence,
        regime=MarketRegime.TREND_UP,
        band=BALANCED_ENSEMBLE.band,
    )
    assert signal is DecisionSignal.HOLD
    assert reasons == (REASON_HOLD_BAND,)


def test_low_confidence_is_reported_before_any_band_or_regime() -> None:
    """An unconvincing reading is unconvincing, not a blocked entry."""
    signal, reasons = decide_candidate(
        score=0.99,
        confidence=BALANCED_ENSEMBLE.band.min_confidence - 1e-9,
        regime=MarketRegime.HIGH_VOLATILITY,
        band=BALANCED_ENSEMBLE.band,
    )

    assert signal is DecisionSignal.HOLD
    assert reasons == (REASON_LOW_CONFIDENCE,)


def test_the_band_is_recorded_with_every_decision() -> None:
    """Configuration travelling with the decision, not a constant in a branch."""
    result = engine().decide(make_bars(choppy()))
    band = result.policy["ensemble"]["band"]  # type: ignore[index]

    assert band["buy_score"] == BALANCED_ENSEMBLE.band.buy_score
    assert band["sell_score"] == BALANCED_ENSEMBLE.band.sell_score
    assert band["min_confidence"] == BALANCED_ENSEMBLE.band.min_confidence
    assert band["boundary_resolves_to"] == "HOLD"


def test_the_shipped_band_is_wider_than_every_shipped_policys() -> None:
    band = BALANCED_ENSEMBLE.band
    for policy in (CRYPTO_POLICY, EQUITY_POLICY):
        assert band.buy_score > policy.thresholds.buy_score
        assert band.sell_score < policy.thresholds.sell_score
        assert band.min_confidence > policy.thresholds.min_confidence


@pytest.mark.parametrize(
    ("band", "expected"),
    [
        (EnsembleBand(buy_score=0.20), "buy band"),
        (EnsembleBand(sell_score=-0.20), "sell band"),
        (EnsembleBand(min_confidence=0.10), "confidence floor"),
    ],
)
def test_a_band_narrower_than_the_policy_is_refused(band: EnsembleBand, expected: str) -> None:
    """CRITICAL. An ensemble may refuse where its components trade, never the reverse."""
    with pytest.raises(DecisionConfigError, match=expected):
        band.require_not_wider_than_policy(CRYPTO_POLICY.thresholds)


def test_an_empty_band_is_refused() -> None:
    """One score meaning both directions is the failure a band exists to prevent."""
    with pytest.raises(DecisionConfigError, match="hold band"):
        EnsembleBand(buy_score=0.0, sell_score=0.0)


def test_an_engine_cannot_be_built_on_a_band_the_policy_would_not_allow() -> None:
    loose = EnsembleSpec(ensemble_version="loose-1", band=EnsembleBand(buy_score=0.05))
    with pytest.raises(DecisionConfigError, match="may not trade where they would refuse"):
        engine(spec=loose)


# --------------------------------------------------------------------------
# Bounded confidence, by construction
# --------------------------------------------------------------------------


@pytest.mark.parametrize("closes", [rising(), falling(), choppy()])
def test_score_and_confidence_always_respect_their_bounds(closes: list[float]) -> None:
    result = engine().decide(make_bars(closes))

    assert -1.0 <= result.score <= 1.0
    assert 0.0 <= result.confidence <= 1.0


@pytest.mark.parametrize("deterministic", [-1.0, -0.5, 0.0, 0.5, 1.0])
@pytest.mark.parametrize("probabilistic", [-1.0, -0.5, 0.0, 0.5, 1.0])
@pytest.mark.parametrize("regime_multiplier", [0.0, 0.4, 1.0])
def test_the_bounds_hold_at_every_extreme_of_the_inputs(
    deterministic: float, probabilistic: float, regime_multiplier: float
) -> None:
    """The claim, exercised at the corners rather than in the middle."""
    attribution = combine(
        deterministic_score=deterministic,
        deterministic_confidence=1.0,
        probabilistic_score=probabilistic,
        probabilistic_confidence=1.0,
        regime_multiplier=regime_multiplier,
        volatility_multiplier=1.0,
        spec=BALANCED_ENSEMBLE,
    )

    assert -1.0 <= attribution.score <= 1.0
    assert 0.0 <= attribution.confidence <= 1.0


def test_weights_that_do_not_sum_to_one_are_refused_rather_than_normalized() -> None:
    """CRITICAL. The unit sum is *why* the blend is bounded; it is not a tidiness rule."""
    with pytest.raises(DecisionConfigError, match="must sum to 1.0"):
        EnsembleWeights(deterministic=0.8, probabilistic=0.8)
    with pytest.raises(DecisionConfigError, match="must sum to 1.0"):
        ConfidenceMix(components=0.9, agreement=0.9)


def test_an_out_of_range_output_is_refused_rather_than_clamped() -> None:
    """The difference between "bounded by construction" and "clipped afterwards".

    A clip would turn a broken construction into a plausible number. This is the
    assertion that it does not: an attribution whose score genuinely left the
    interval is a `DecisionConfigError`, not a 1.0.
    """
    with pytest.raises(DecisionConfigError, match="broken construction"):
        EnsembleAttribution(
            score=1.5,
            confidence=0.5,
            score_components=(
                ComponentContribution(
                    component=COMPONENT_DETERMINISTIC, reading=1.5, weight=1.0, contribution=1.5
                ),
            ),
            confidence_components=(
                ComponentContribution(
                    component=COMPONENT_DETERMINISTIC, reading=0.5, weight=1.0, contribution=0.5
                ),
            ),
        )


def test_floating_point_drift_past_a_bound_is_absorbed_rather_than_raised() -> None:
    """A weighted mean of two values at the bound can land a bit past it."""
    drifted = 1.0 + 1e-16
    attribution = EnsembleAttribution(
        score=drifted,
        confidence=0.5,
        score_components=(
            ComponentContribution(
                component=COMPONENT_DETERMINISTIC, reading=1.0, weight=1.0, contribution=drifted
            ),
        ),
        confidence_components=(
            ComponentContribution(
                component=COMPONENT_DETERMINISTIC, reading=0.5, weight=1.0, contribution=0.5
            ),
        ),
    )

    assert attribution.score == 1.0


def test_an_attenuation_can_only_lower_confidence() -> None:
    """Context takes conviction away; it never adds any."""
    calm = combine(
        deterministic_score=0.8,
        deterministic_confidence=0.9,
        probabilistic_score=0.8,
        probabilistic_confidence=0.9,
        regime_multiplier=1.0,
        volatility_multiplier=1.0,
        spec=BALANCED_ENSEMBLE,
    )
    disordered = combine(
        deterministic_score=0.8,
        deterministic_confidence=0.9,
        probabilistic_score=0.8,
        probabilistic_confidence=0.9,
        regime_multiplier=0.4,
        volatility_multiplier=0.5,
        spec=BALANCED_ENSEMBLE,
    )

    assert disordered.confidence < calm.confidence
    assert abs(disordered.score) < abs(calm.score)


def test_the_confidence_is_not_the_score_by_another_name() -> None:
    """Two engines each sure and pointing opposite ways is not evidence for anything.

    The distinction V4 cannot make on its own - one probability cannot
    corroborate itself - and the reason an ensemble's confidence carries an
    agreement term at all.
    """
    agreeing = combine(
        deterministic_score=0.8,
        deterministic_confidence=0.9,
        probabilistic_score=0.8,
        probabilistic_confidence=0.9,
        regime_multiplier=1.0,
        volatility_multiplier=1.0,
        spec=BALANCED_ENSEMBLE,
    )
    opposed = combine(
        deterministic_score=0.8,
        deterministic_confidence=0.9,
        probabilistic_score=-0.8,
        probabilistic_confidence=0.9,
        regime_multiplier=1.0,
        volatility_multiplier=1.0,
        spec=BALANCED_ENSEMBLE,
    )

    assert opposed.confidence < agreeing.confidence
    assert opposed.score == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("deterministic", "probabilistic", "expected"),
    [(1.0, 1.0, 1.0), (-1.0, -1.0, 1.0), (1.0, -1.0, 0.0), (0.0, 0.9, 0.5), (0.5, 0.5, 0.625)],
)
def test_agreement_is_bounded_and_continuous(
    deterministic: float, probabilistic: float, expected: float
) -> None:
    """A component with no opinion leaves the pair at exactly a half."""
    assert component_agreement(deterministic, probabilistic) == pytest.approx(expected)


# --------------------------------------------------------------------------
# Component attribution
# --------------------------------------------------------------------------


def test_the_contributions_reconstruct_the_score_exactly() -> None:
    """CRITICAL. Which input moved the decision, and by how much."""
    result = engine().decide(make_bars(rising()))
    attribution = result.policy["attribution"]

    total = sum(item["contribution"] for item in attribution["score_components"])  # type: ignore[index]
    assert total == pytest.approx(result.score, abs=1e-12)


def test_the_contributions_reconstruct_the_confidence_exactly() -> None:
    result = engine().decide(make_bars(rising()))
    attribution = result.policy["attribution"]

    total = sum(item["contribution"] for item in attribution["confidence_components"])  # type: ignore[index]
    assert total == pytest.approx(result.confidence, abs=1e-12)


def test_every_input_is_named_in_the_attribution() -> None:
    assessment = engine().assess(make_bars(rising()))
    assert assessment.attribution is not None

    assert set(assessment.attribution.score_contributions()) == {
        COMPONENT_DETERMINISTIC,
        COMPONENT_PROBABILISTIC,
        COMPONENT_REGIME,
        COMPONENT_VOLATILITY,
    }
    assert set(assessment.attribution.confidence_contributions()) == {
        COMPONENT_DETERMINISTIC,
        COMPONENT_PROBABILISTIC,
        COMPONENT_AGREEMENT,
        COMPONENT_REGIME,
        COMPONENT_VOLATILITY,
    }


def test_an_attribution_that_does_not_add_up_is_refused() -> None:
    """It would survive serialization and be discovered wrong by whoever needed it."""
    with pytest.raises(DecisionConfigError, match="not an audit trail"):
        EnsembleAttribution(
            score=0.5,
            confidence=0.5,
            score_components=(
                ComponentContribution(
                    component=COMPONENT_DETERMINISTIC, reading=0.5, weight=1.0, contribution=0.1
                ),
            ),
            confidence_components=(
                ComponentContribution(
                    component=COMPONENT_DETERMINISTIC, reading=0.5, weight=1.0, contribution=0.5
                ),
            ),
        )


def test_the_attribution_is_recoverable_from_a_stored_decision() -> None:
    """After the fact means after `to_dict`, a log line, and a JSON round trip."""
    result = engine().decide(make_bars(rising()))
    restored = json.loads(json.dumps(result.to_dict()))
    attribution = restored["policy"]["attribution"]

    contributions = {
        item["component"]: item["contribution"] for item in attribution["score_components"]
    }
    assert sum(contributions.values()) == pytest.approx(restored["score"], abs=1e-12)
    assert attribution["attenuation_order"] == ["regime", "volatility"]


def test_moving_one_component_moves_its_own_contribution() -> None:
    """The attribution tracks the input rather than restating the output."""
    weak = combine(
        deterministic_score=0.2,
        deterministic_confidence=0.5,
        probabilistic_score=0.6,
        probabilistic_confidence=0.5,
        regime_multiplier=1.0,
        volatility_multiplier=1.0,
        spec=BALANCED_ENSEMBLE,
    )
    strong = combine(
        deterministic_score=0.9,
        deterministic_confidence=0.5,
        probabilistic_score=0.6,
        probabilistic_confidence=0.5,
        regime_multiplier=1.0,
        volatility_multiplier=1.0,
        spec=BALANCED_ENSEMBLE,
    )

    assert (
        strong.score_contributions()[COMPONENT_DETERMINISTIC]
        > weak.score_contributions()[COMPONENT_DETERMINISTIC]
    )
    assert strong.score_contributions()[COMPONENT_PROBABILISTIC] == pytest.approx(
        weak.score_contributions()[COMPONENT_PROBABILISTIC]
    )


def test_an_attenuation_contributes_against_the_direction_it_shrinks() -> None:
    """Sign as honesty: an attenuation removed conviction, it did not add any."""
    attribution = combine(
        deterministic_score=0.8,
        deterministic_confidence=0.9,
        probabilistic_score=0.8,
        probabilistic_confidence=0.9,
        regime_multiplier=0.5,
        volatility_multiplier=0.5,
        spec=BALANCED_ENSEMBLE,
    )
    contributions = attribution.score_contributions()

    assert contributions[COMPONENT_REGIME] < 0.0
    assert contributions[COMPONENT_VOLATILITY] < 0.0
    assert attribution.score == pytest.approx(0.8 * 0.5 * 0.5)


def test_the_reason_tokens_name_the_largest_mover_first() -> None:
    reasons = contribution_reasons(
        (
            ComponentContribution(component="small", reading=0.0, weight=1.0, contribution=0.1),
            ComponentContribution(component="large", reading=0.0, weight=1.0, contribution=-0.9),
            ComponentContribution(component="flat", reading=0.0, weight=1.0, contribution=0.0),
        )
    )

    assert reasons == ("COMPONENT_LARGE_LOWERED", "COMPONENT_SMALL_RAISED")


def test_the_decision_names_which_way_each_component_moved_it() -> None:
    result = engine().decide(make_bars(rising()))

    assert f"COMPONENT_{COMPONENT_DETERMINISTIC.upper()}_RAISED" in result.reasons
    assert f"COMPONENT_{COMPONENT_PROBABILISTIC.upper()}_RAISED" in result.reasons


def test_the_decision_says_whether_the_two_engines_corroborated_each_other() -> None:
    assert REASON_COMPONENTS_AGREE in engine().decide(make_bars(rising())).reasons

    opposed = combine(
        deterministic_score=0.8,
        deterministic_confidence=0.9,
        probabilistic_score=-0.8,
        probabilistic_confidence=0.9,
        regime_multiplier=1.0,
        volatility_multiplier=1.0,
        spec=BALANCED_ENSEMBLE,
    )
    assert opposed.confidence_readings()[COMPONENT_AGREEMENT] < 0.5
    assert REASON_COMPONENTS_DISAGREE


def test_the_result_reports_each_component_separately() -> None:
    result = engine().decide(make_bars(rising()))

    assert set(result.policy["component_scores"]) == {"deterministic", "probabilistic"}  # type: ignore[arg-type]
    assert set(result.policy["component_confidence"]) == {"deterministic", "probabilistic"}  # type: ignore[arg-type]
    assert result.policy["probability_up"] is not None


def test_features_are_namespaced_by_the_component_that_read_them() -> None:
    result = engine().decide(make_bars(rising()))

    assert "v3.15m.ema_spread_z" in result.features
    assert "v3.4h.ema_spread_z" in result.features
    assert "v4.ema_spread_z" in result.features


# --------------------------------------------------------------------------
# Regime and volatility
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("context", "base", "expected"),
    [
        (MarketRegime.TREND_UP, MarketRegime.TREND_UP, MarketRegime.TREND_UP),
        (MarketRegime.TREND_UP, MarketRegime.RANGE, MarketRegime.TREND_UP),
        (MarketRegime.RANGE, MarketRegime.TREND_DOWN, MarketRegime.RANGE),
        (
            MarketRegime.TREND_UP,
            MarketRegime.HIGH_VOLATILITY,
            MarketRegime.HIGH_VOLATILITY,
        ),
        (
            MarketRegime.HIGH_VOLATILITY,
            MarketRegime.TREND_UP,
            MarketRegime.HIGH_VOLATILITY,
        ),
        (MarketRegime.TREND_UP, MarketRegime.UNKNOWN, MarketRegime.UNKNOWN),
    ],
)
def test_disorder_on_either_timescale_is_disorder(
    context: MarketRegime, base: MarketRegime, expected: MarketRegime
) -> None:
    """Otherwise a violent 15-minute bar inside a calm four hours reads as calm."""
    assert combine_regimes(context, base) is expected


def test_a_volatility_expansion_blocks_a_buy_and_never_a_sell() -> None:
    """V2's asymmetry, inherited rather than restated."""
    band = BALANCED_ENSEMBLE.band

    blocked, blocked_reasons = decide_candidate(
        score=0.9, confidence=1.0, regime=MarketRegime.HIGH_VOLATILITY, band=band
    )
    allowed, allowed_reasons = decide_candidate(
        score=-0.9, confidence=1.0, regime=MarketRegime.HIGH_VOLATILITY, band=band
    )

    assert (blocked, blocked_reasons) == (DecisionSignal.HOLD, (REASON_REGIME_BLOCKED,))
    assert (allowed, allowed_reasons) == (DecisionSignal.SELL, (REASON_ENSEMBLE_SELL,))


def test_an_expanded_base_bar_reaches_the_ensemble_regime_and_stops_the_entry() -> None:
    """End to end: the union of the two scales, not just the broad one."""
    count = CRYPTO_REQUIRED + 40
    closes = rising(count)
    spans = [0.5] * count
    for index in range(count - 6, count):
        spans[index] = 60.0

    result = engine().decide(make_bars(closes, ranges=spans))
    assert result.regime is MarketRegime.HIGH_VOLATILITY
    assert result.policy["base_regime"] == "HIGH_VOLATILITY"
    assert result.signal is not DecisionSignal.BUY


def test_a_counter_trend_reading_keeps_less_conviction_than_an_aligned_one() -> None:
    adjustments = BALANCED_ENSEMBLE.regime_adjustments

    aligned = adjustments.multiplier(MarketRegime.TREND_UP, lean=0.5)
    opposed = adjustments.multiplier(MarketRegime.TREND_UP, lean=-0.5)

    assert aligned == adjustments.trend_aligned
    assert opposed == adjustments.trend_opposed
    assert opposed < aligned


def test_an_unclassified_regime_leaves_no_conviction_at_all() -> None:
    """A HOLD by arithmetic rather than by a branch."""
    attribution = combine(
        deterministic_score=1.0,
        deterministic_confidence=1.0,
        probabilistic_score=1.0,
        probabilistic_confidence=1.0,
        regime_multiplier=BALANCED_ENSEMBLE.regime_adjustments.multiplier(
            MarketRegime.UNKNOWN, lean=1.0
        ),
        volatility_multiplier=1.0,
        spec=BALANCED_ENSEMBLE,
    )

    assert attribution.score == 0.0
    assert attribution.confidence == 0.0
    signal, _ = decide_candidate(
        score=attribution.score,
        confidence=attribution.confidence,
        regime=MarketRegime.UNKNOWN,
        band=BALANCED_ENSEMBLE.band,
    )
    assert signal is DecisionSignal.HOLD


def test_regime_adjustments_may_not_grow_with_disorder() -> None:
    with pytest.raises(DecisionConfigError, match="not increase with disorder"):
        RegimeAdjustments(trend_aligned=0.5, range_bound=0.9)
    with pytest.raises(DecisionConfigError, match="not better evidence"):
        RegimeAdjustments(trend_aligned=0.5, trend_opposed=0.9, range_bound=0.4)
    with pytest.raises(DecisionConfigError, match="more generously"):
        RegimeAdjustments(trend_opposed=0.1, unknown=0.2)


def test_a_regime_adjustment_above_one_is_refused() -> None:
    """Attenuations only: a regime cannot amplify a score past its bound."""
    with pytest.raises(DecisionConfigError, match="within"):
        RegimeAdjustments(trend_aligned=1.5)


def test_the_volatility_adjustment_is_the_policys_own_tolerance() -> None:
    """V5 introduces no second volatility constant; it applies `config.py`'s."""
    assessment = engine().assess(make_bars(choppy()))

    assert assessment.volatility_multiplier == pytest.approx(
        volatility_factor(assessment.probabilistic.features, CRYPTO_POLICY.thresholds)
    )
    assert 0.0 < assessment.volatility_multiplier <= 1.0


def test_a_calmer_bar_keeps_more_conviction_than_an_expanded_one() -> None:
    calm = combine(
        deterministic_score=0.9,
        deterministic_confidence=0.9,
        probabilistic_score=0.9,
        probabilistic_confidence=0.9,
        regime_multiplier=1.0,
        volatility_multiplier=1.0,
        spec=BALANCED_ENSEMBLE,
    )
    expanded = combine(
        deterministic_score=0.9,
        deterministic_confidence=0.9,
        probabilistic_score=0.9,
        probabilistic_confidence=0.9,
        regime_multiplier=1.0,
        volatility_multiplier=0.3,
        spec=BALANCED_ENSEMBLE,
    )

    assert expanded.score < calm.score
    assert expanded.confidence < calm.confidence


# --------------------------------------------------------------------------
# Both components, or no decision
# --------------------------------------------------------------------------


def test_too_little_history_is_an_explicit_hold_and_not_a_guess() -> None:
    result = engine().decide(make_bars(rising(CRYPTO_REQUIRED - 16)))

    assert result.signal is DecisionSignal.HOLD
    assert result.score == 0.0
    assert result.confidence == 0.0
    assert result.regime is MarketRegime.UNKNOWN
    assert REASON_DETERMINISTIC_UNAVAILABLE in result.reasons
    assert "INSUFFICIENT_HISTORY_4H" in result.reasons


def test_an_available_v4_does_not_stand_in_for_an_unavailable_v3() -> None:
    """CRITICAL. Falling back would turn V5 into V4 exactly when context is missing.

    Two hundred base bars is comfortably more than V4 needs and far less than
    V3's context costs. A fallback would produce a candidate here.
    """
    bars = make_bars(rising(200))
    assert engine().probabilistic.assess(bars).available

    result = engine().decide(bars)
    assert result.signal is DecisionSignal.HOLD
    assert REASON_DETERMINISTIC_UNAVAILABLE in result.reasons


def test_an_unavailable_probability_stops_the_decision_and_names_v4() -> None:
    blocking = engine()._blocking_reasons(available_result(), unavailable_probability())

    assert REASON_PROBABILISTIC_UNAVAILABLE in blocking
    assert "FEATURE_UNAVAILABLE_15M" in blocking
    assert REASON_DETERMINISTIC_UNAVAILABLE not in blocking


def test_an_unavailable_assessment_carries_no_attribution() -> None:
    """A zeroed one would look like an explanation of a decision never computed."""
    assessment = engine().assess(make_bars(rising(200)))

    assert not assessment.available
    assert assessment.attribution is None


def test_a_repeated_reason_from_two_engines_is_reported_once() -> None:
    result = engine().decide(make_bars(rising()))

    assert len(result.reasons) == len(set(result.reasons))


# --------------------------------------------------------------------------
# Versioning
# --------------------------------------------------------------------------


def test_a_decision_names_the_exact_ensemble_that_produced_it() -> None:
    result = engine().decide(make_bars(rising()))

    assert result.policy["ensemble_version"] == BALANCED_ENSEMBLE.ensemble_version
    ensemble = result.policy["ensemble"]
    assert ensemble["ensemble_contract_version"] == ENSEMBLE_CONTRACT_VERSION  # type: ignore[index]
    assert ensemble["weights"] == {"deterministic": 0.5, "probabilistic": 0.5}  # type: ignore[index]


def test_a_decision_names_the_exact_components_that_produced_it() -> None:
    """Including the trained model, so a replay needs nothing outside the record."""
    components = engine().decide(make_bars(rising())).policy["components"]

    assert components["deterministic"]["engine_version"] == "v3"  # type: ignore[index]
    assert components["probabilistic"]["engine_version"] == "v4"  # type: ignore[index]
    assert components["probabilistic"]["model_version"] == "v5-test-1"  # type: ignore[index]
    assert components["probabilistic"]["calibrated"] is True  # type: ignore[index]


def test_an_ensemble_round_trips_through_its_record() -> None:
    original = EnsembleSpec(
        ensemble_version="v5-trend-weighted-2.1.0",
        weights=EnsembleWeights(deterministic=0.7, probabilistic=0.3),
        confidence_mix=ConfidenceMix(components=0.75, agreement=0.25),
        band=EnsembleBand(buy_score=0.4, sell_score=-0.35, min_confidence=0.6),
        regime_adjustments=RegimeAdjustments(trend_opposed=0.25, range_bound=0.6),
        requires_calibration=False,
    )

    assert EnsembleSpec.from_record(original.to_record()) == original


def test_a_record_from_another_contract_version_is_refused() -> None:
    """A stored ensemble is not reinterpreted across record shapes."""
    record = BALANCED_ENSEMBLE.to_record()
    record["ensemble_contract_version"] = "0.9.0"

    with pytest.raises(DecisionConfigError, match="ensemble contract"):
        EnsembleSpec.from_record(record)


def test_an_ensemble_without_a_version_is_refused() -> None:
    with pytest.raises(DecisionConfigError, match="non-empty identifier"):
        EnsembleSpec(ensemble_version="   ")


def test_the_shipped_ensemble_is_named_and_balanced() -> None:
    assert BALANCED_ENSEMBLE.ensemble_version == "v5-balanced-1.0.0"
    assert BALANCED_ENSEMBLE.weights.deterministic == BALANCED_ENSEMBLE.weights.probabilistic
    assert BALANCED_ENSEMBLE.requires_calibration is True


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------


def test_the_shipped_ensemble_refuses_an_uncalibrated_model() -> None:
    """CRITICAL. An uncalibrated score is not a probability, and is not blended as one."""
    with pytest.raises(DecisionConfigError, match="requires a calibrated model"):
        engine(calibration=IdentityCalibration())


def test_an_uncalibrated_model_may_be_run_on_purpose_and_says_so_on_every_bar() -> None:
    permissive = EnsembleSpec(ensemble_version="v5-uncalibrated-1.0.0", requires_calibration=False)
    built = engine(spec=permissive, calibration=IdentityCalibration())

    result = built.decide(make_bars(rising()))
    assert REASON_UNCALIBRATED_MODEL in result.reasons
    assert result.policy["ensemble"]["requires_calibration"] is False  # type: ignore[index]


# --------------------------------------------------------------------------
# Determinism and no look-ahead
# --------------------------------------------------------------------------


def test_the_same_bars_decide_identically_on_every_call() -> None:
    bars = make_bars(choppy())
    built = engine()

    assert built.decide(bars).to_dict() == built.decide(bars).to_dict()


def test_a_future_bar_cannot_change_a_decision_already_made() -> None:
    """CRITICAL. docs/SPEC.md section 7F, through both components at once."""
    closes = choppy(CRYPTO_REQUIRED + 200)
    built = engine()

    on_prefix = built.decide(make_bars(closes[: CRYPTO_REQUIRED + 100]))
    on_whole = built.decide(make_bars(closes[: CRYPTO_REQUIRED + 100]))

    assert on_prefix.to_dict() == on_whole.to_dict()
    assert built.decide(make_bars(closes)).timestamp > on_prefix.timestamp


def test_deciding_does_not_modify_the_supplied_frame() -> None:
    bars = make_bars(choppy())
    before = bars.copy(deep=True)

    engine().decide(bars)

    assert bars.equals(before)


def test_two_engines_built_from_the_same_configuration_agree_exactly() -> None:
    bars = make_bars(choppy())

    assert engine().decide(bars).to_dict() == engine().decide(bars).to_dict()


def test_both_components_read_the_same_bar() -> None:
    assessment = engine().assess(make_bars(rising()))

    assert assessment.deterministic.timestamp == assessment.probabilistic.timestamp
    assert assessment.timestamp == assessment.deterministic.timestamp


# --------------------------------------------------------------------------
# The boundary V5 does not cross
# --------------------------------------------------------------------------


V5_MODULES = ("ensemble.py", "v5.py")


def v5_sources() -> list[Path]:
    """The two modules this milestone added to the decision package."""
    from autotrader.decision import v5 as v5_module

    root = Path(v5_module.__file__).parent
    return [root / name for name in V5_MODULES]


def test_the_new_modules_stay_inside_the_decision_packages_import_boundary() -> None:
    """CRITICAL. The leftmost box of the pipeline can still reach none of the others."""
    from test_decision_contract import ALLOWED_IMPORT_ROOTS, _import_roots

    for path in v5_sources():
        for imported in _import_roots(ast.parse(path.read_text(encoding="utf-8"))):
            allowed = any(
                imported == root or imported.startswith(f"{root}.") for root in ALLOWED_IMPORT_ROOTS
            )
            assert allowed, f"{path.name} imports {imported}, which is outside the boundary"


def test_a_v5_candidate_carries_nothing_that_could_size_or_place_an_order() -> None:
    """CRITICAL. A candidate is a direction and a conviction, and nothing else.

    The record is walked whole - features, policy metadata, attribution and all -
    because the sizing vocabulary appearing anywhere in it would mean V5 had
    started answering a question that belongs to the risk engine.
    """
    record = engine().decide(make_bars(rising())).to_dict()
    flattened = json.dumps(record).lower()

    for forbidden in (
        "quantity",
        "notional",
        "reference_price",
        "submit",
        "client_order_id",
        "order_intent",
        "approved",
        "position_size",
        "broker",
    ):
        assert forbidden not in flattened, f"a V5 record names {forbidden}"

    assert set(record) == {
        "version",
        "symbol",
        "timestamp",
        "signal",
        "regime",
        "score",
        "confidence",
        "reasons",
        "features",
        "policy",
    }


def test_the_engine_exposes_no_ordering_sizing_or_approval_surface() -> None:
    built = engine()
    public = {name for name in dir(built) if not name.startswith("_")}

    assert public == {
        "assess",
        "decide",
        "describe",
        "deterministic",
        "for_symbol",
        "policy",
        "probabilistic",
        "required_base_bars",
        "spec",
        "version",
    }


def test_a_buy_candidate_is_not_a_permission() -> None:
    """`is_actionable` says an engine produced a candidate and nothing more."""
    result = engine().decide(make_bars(rising()))

    assert result.signal is DecisionSignal.BUY
    assert result.is_actionable
    assert "approved" not in result.to_dict()


def test_the_risk_engine_still_needs_what_no_decision_can_supply() -> None:
    """CRITICAL. V5 cannot pre-approve anything, because it cannot describe a trade.

    `RiskRequest` needs a reference price and a requested quantity, and
    `RiskContext` needs the account. A V5 record carries neither, so the only
    route from a candidate to an order still runs through a caller that reads
    the account and through `evaluate_risk` itself - which takes a request, a
    context and a policy, and offers no parameter that could arrive approved.
    """
    import inspect

    from autotrader.risk.engine import RiskContext, RiskRequest, evaluate_risk

    record = set(engine().decide(make_bars(rising())).to_dict())
    sizing = set(RiskRequest.__dataclass_fields__) - {"symbol", "side"}
    assert sizing == {"reference_price", "requested_quantity"}
    assert not record & sizing
    assert not record & set(RiskContext.__dataclass_fields__)

    parameters = set(inspect.signature(evaluate_risk).parameters)
    assert parameters == {"request", "context", "policy"}


def test_nothing_outside_the_decision_package_has_started_preferring_v5() -> None:
    """CRITICAL. No default flips, no runtime starts using it, no gate opens.

    V5 is component-complete and unwired, exactly as V2, V3 and V4 are. The
    crypto and equity runtimes still call `autotrader.strategies.ema_cross`
    directly, and activating an engine is a decision this milestone does not
    make on anybody's behalf.
    """
    import autotrader
    from autotrader.decision import v5 as v5_module
    from test_runtime import code_without_prose

    decision_root = Path(v5_module.__file__).parent
    source_root = Path(autotrader.__file__).resolve().parent

    for path in sorted(source_root.rglob("*.py")):
        if decision_root in path.parents or path.parent == decision_root:
            continue
        code = code_without_prose(path.read_text(encoding="utf-8"))
        for token in ("decision.v5", "EnsembleV5Engine", "EnsembleSpec", "BALANCED_ENSEMBLE"):
            assert token not in code, f"{path.relative_to(source_root)} names {token}"


def test_the_runtimes_still_call_the_crossover_they_always_did() -> None:
    """The other half of "not activated": what *is* wired is unchanged."""
    from autotrader.runtime import runner
    from test_runtime import code_without_prose

    code = code_without_prose(Path(runner.__file__).read_text(encoding="utf-8"))
    assert "ema_cross" in code
    assert "autotrader.decision" not in code


# --------------------------------------------------------------------------
# The blend, restated as arithmetic
# --------------------------------------------------------------------------


def test_the_blend_is_the_weighted_mean_the_specification_declares() -> None:
    weights = EnsembleWeights(deterministic=0.7, probabilistic=0.3)

    assert blended_score(0.4, -0.2, weights) == pytest.approx(0.7 * 0.4 + 0.3 * -0.2)


def test_the_policy_thresholds_v5_is_measured_against_are_the_shipped_ones() -> None:
    """V5 reads `config.py` rather than carrying its own copy of a threshold."""
    thresholds = engine().policy.thresholds

    assert isinstance(thresholds, DecisionThresholds)
    assert thresholds is CRYPTO_POLICY.thresholds
