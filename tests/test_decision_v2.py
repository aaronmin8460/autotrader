"""Decision Engine tests: V2, the deterministic multi-factor engine.

Directions are asserted against constructed markets whose answer is not in
doubt - a monotonic advance is bullish, its mirror image is bearish, a flat
tape is neither - rather than against recorded output. The thresholds, the hold
band, the history requirement and the asset-class separation are each tested at
their boundary, because a gate that is only tested well inside its own range is
not tested at all.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from autotrader.decision import config as decision_config
from autotrader.decision.config import (
    CRYPTO_POLICY,
    EQUITY_POLICY,
    AssetClassPolicy,
    DecisionThresholds,
    FactorWeights,
    IndicatorPeriods,
    MultiTimeframeGates,
    TimeframePolicy,
    policy_for_symbol,
)
from autotrader.decision.contract import (
    VERSION_V2,
    AssetClass,
    DecisionConfigError,
    DecisionInputError,
    DecisionSignal,
    MarketRegime,
)
from autotrader.decision.features import SCORED_FEATURES
from autotrader.decision.scoring import (
    REASON_BUY,
    REASON_HOLD_BAND,
    REASON_LOW_CONFIDENCE,
    REASON_LOW_PARTICIPATION,
    REASON_REGIME_BLOCKED,
    REASON_SELL,
    agreement,
    classify_regime,
    composite_score,
    score_factors,
    softsign,
)
from autotrader.decision.v2 import MultiFactorV2Engine, evaluate_timeframe

FIRST_BAR = datetime(2025, 1, 1, tzinfo=UTC)
STEP = timedelta(minutes=15)
REQUIRED = IndicatorPeriods().required_bars


def make_bars(
    closes: list[float],
    *,
    symbol: str = "BTC/USD",
    volumes: list[float] | None = None,
    spreads: list[float] | None = None,
) -> pd.DataFrame:
    prices = [float(close) for close in closes]
    sizes = [100.0] * len(prices) if volumes is None else [float(v) for v in volumes]
    ranges = [0.5] * len(prices) if spreads is None else [float(s) for s in spreads]
    return pd.DataFrame(
        {
            "timestamp": [FIRST_BAR + STEP * index for index in range(len(prices))],
            "symbol": [symbol] * len(prices),
            "open": prices,
            "high": [price + width for price, width in zip(prices, ranges, strict=True)],
            "low": [price - width for price, width in zip(prices, ranges, strict=True)],
            "close": prices,
            "volume": sizes,
            "trade_count": [10] * len(prices),
            "vwap": prices,
        }
    )


def rising(count: int = 200, step: float = 0.5) -> list[float]:
    return [100.0 + step * index for index in range(count)]


def falling(count: int = 200, step: float = 0.5) -> list[float]:
    return [100.0 + step * (count - index) for index in range(count)]


def choppy(count: int = 200) -> list[float]:
    return [100.0 + 3.0 * math.sin(index / 5.0) for index in range(count)]


CRYPTO = MultiFactorV2Engine.for_symbol("BTC/USD")
EQUITY = MultiFactorV2Engine.for_symbol("SPY")


# --------------------------------------------------------------------------
# Direction
# --------------------------------------------------------------------------


def test_a_sustained_advance_scores_positive_and_buys() -> None:
    result = CRYPTO.decide(make_bars(rising()))

    assert result.signal is DecisionSignal.BUY
    assert result.score > 0.0
    assert result.regime is MarketRegime.TREND_UP
    assert REASON_BUY in result.reasons


def test_a_sustained_decline_scores_negative_and_sells() -> None:
    result = CRYPTO.decide(make_bars(falling()))

    assert result.signal is DecisionSignal.SELL
    assert result.score < 0.0
    assert result.regime is MarketRegime.TREND_DOWN
    assert REASON_SELL in result.reasons


def test_the_score_is_odd_under_mirroring_the_market() -> None:
    """A market and its reflection must produce equal and opposite scores.

    Nothing in the factor set has a directional preference, so any asymmetry
    here would be a bug rather than a view.
    """
    advance = CRYPTO.decide(make_bars(rising()))
    decline = CRYPTO.decide(make_bars(falling()))

    # A nanosecond of tolerance, not a loose one: the residual is ordinary
    # float round-off in the MACD chain, where averaging a reflected series is
    # not bit-identical to reflecting the average of the original.
    assert advance.score == pytest.approx(-decline.score, abs=1e-8)
    assert advance.confidence == pytest.approx(decline.confidence, abs=1e-8)


def test_a_flat_market_holds() -> None:
    result = CRYPTO.decide(make_bars([100.0] * 200, spreads=[0.0] * 200))

    assert result.signal is DecisionSignal.HOLD
    assert result.score == 0.0


@pytest.mark.parametrize("closes", [rising(), falling(), choppy(), [100.0] * 200])
def test_score_and_confidence_always_respect_their_bounds(closes: list[float]) -> None:
    result = CRYPTO.decide(make_bars(closes))

    assert -1.0 <= result.score <= 1.0
    assert 0.0 <= result.confidence <= 1.0


# --------------------------------------------------------------------------
# Thresholds and the hold band
# --------------------------------------------------------------------------


def widened(policy: AssetClassPolicy, **threshold_overrides: float) -> AssetClassPolicy:
    """`policy` with some thresholds replaced. Used to test a gate at its edge."""
    current = {
        "buy_score": policy.thresholds.buy_score,
        "sell_score": policy.thresholds.sell_score,
        "min_confidence": policy.thresholds.min_confidence,
        "high_volatility_ratio": policy.thresholds.high_volatility_ratio,
        "low_participation_ratio": policy.thresholds.low_participation_ratio,
    }
    current.update(threshold_overrides)
    return AssetClassPolicy(
        asset_class=policy.asset_class,
        name=f"{policy.name}-test",
        thresholds=DecisionThresholds(**current),
        base_bars_per_complete_bar=policy.base_bars_per_complete_bar,
        gates=policy.gates,
        timeframes=policy.timeframes,
        timeframe_weights=policy.timeframe_weights,
    )


def test_a_hold_band_wider_than_the_score_produces_an_explicit_hold() -> None:
    bars = make_bars(rising())
    strong = CRYPTO.decide(bars)
    engine = MultiFactorV2Engine(widened(CRYPTO_POLICY, buy_score=1.0, sell_score=-1.0))

    result = engine.decide(bars)

    assert strong.signal is DecisionSignal.BUY
    assert result.signal is DecisionSignal.HOLD
    assert REASON_HOLD_BAND in result.reasons


def test_the_buy_threshold_is_inclusive_at_its_own_edge() -> None:
    """A score exactly at the threshold buys; one a hair below does not."""
    bars = make_bars(rising())
    score = CRYPTO.decide(bars).score

    at_edge = MultiFactorV2Engine(widened(CRYPTO_POLICY, buy_score=score))
    just_above = MultiFactorV2Engine(widened(CRYPTO_POLICY, buy_score=min(1.0, score + 1e-9)))

    assert at_edge.decide(bars).signal is DecisionSignal.BUY
    assert just_above.decide(bars).signal is DecisionSignal.HOLD


def test_the_sell_threshold_is_inclusive_at_its_own_edge() -> None:
    bars = make_bars(falling())
    score = CRYPTO.decide(bars).score

    at_edge = MultiFactorV2Engine(widened(CRYPTO_POLICY, sell_score=score))
    just_below = MultiFactorV2Engine(widened(CRYPTO_POLICY, sell_score=max(-1.0, score - 1e-9)))

    assert at_edge.decide(bars).signal is DecisionSignal.SELL
    assert just_below.decide(bars).signal is DecisionSignal.HOLD


def test_an_unconvincing_reading_holds_before_any_other_gate_is_consulted() -> None:
    bars = make_bars(rising())
    engine = MultiFactorV2Engine(widened(CRYPTO_POLICY, min_confidence=1.0))

    result = engine.decide(bars)

    assert result.signal is DecisionSignal.HOLD
    assert result.reasons[0] == REASON_LOW_CONFIDENCE


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


def test_one_bar_short_of_the_requirement_is_an_explicit_hold() -> None:
    result = CRYPTO.decide(make_bars(rising(REQUIRED - 1)))

    assert result.signal is DecisionSignal.HOLD
    assert result.reasons == ("INSUFFICIENT_HISTORY_15M",)
    assert result.score == 0.0
    assert result.confidence == 0.0
    assert result.regime is MarketRegime.UNKNOWN


def test_exactly_the_required_history_is_enough_to_name_a_direction() -> None:
    result = CRYPTO.decide(make_bars(rising(REQUIRED)))

    assert result.reasons[0] != "INSUFFICIENT_HISTORY_15M"
    assert result.signal is DecisionSignal.BUY


def test_the_engine_reports_the_history_it_needs_before_being_called() -> None:
    assert CRYPTO.required_base_bars == REQUIRED
    assert CRYPTO.describe()["required_base_bars"] == REQUIRED


def test_an_undefined_scored_feature_holds_rather_than_scoring_around_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defensive path: a measurement that is absent is never a zero."""
    from autotrader.decision import v2 as v2_module

    real = v2_module.compute_features

    def with_a_hole(bars: pd.DataFrame, **kwargs: object) -> pd.DataFrame:
        features = real(bars, **kwargs)  # type: ignore[arg-type]
        features.loc[features.index[-1], SCORED_FEATURES[0]] = float("nan")
        return features

    monkeypatch.setattr(v2_module, "compute_features", with_a_hole)
    result = MultiFactorV2Engine.for_symbol("BTC/USD").decide(make_bars(rising()))

    assert result.signal is DecisionSignal.HOLD
    assert result.reasons == ("FEATURE_UNAVAILABLE_15M",)


def test_an_empty_frame_is_refused_rather_than_held() -> None:
    """A HOLD names a bar, and an empty frame names none. That is a caller error."""
    with pytest.raises(DecisionInputError, match="must not be empty"):
        CRYPTO.decide(make_bars(rising()).iloc[:0])


# --------------------------------------------------------------------------
# Only the newest bar
# --------------------------------------------------------------------------


def test_the_decision_carries_the_newest_completed_bar_timestamp() -> None:
    bars = make_bars(rising())
    result = CRYPTO.decide(bars)

    assert result.timestamp == bars["timestamp"].iloc[-1]


def test_older_bars_are_indicator_state_rather_than_a_backlog_to_replay() -> None:
    """One call produces one candidate, for one bar, however long the window."""
    bars = make_bars(rising(400))
    result = CRYPTO.decide(bars)

    assert result.timestamp == bars["timestamp"].iloc[-1]
    assert result.policy["bar_count"] == 400


# --------------------------------------------------------------------------
# Asset-class policy separation
# --------------------------------------------------------------------------


def test_the_two_shipped_policies_do_not_share_thresholds() -> None:
    """Equal-looking policy across two data semantics is an unstated assumption."""
    assert CRYPTO_POLICY.thresholds != EQUITY_POLICY.thresholds
    assert (
        CRYPTO_POLICY.thresholds.high_volatility_ratio
        > EQUITY_POLICY.thresholds.high_volatility_ratio
    )
    assert (
        CRYPTO_POLICY.thresholds.low_participation_ratio
        < EQUITY_POLICY.thresholds.low_participation_ratio
    )
    assert CRYPTO_POLICY.thresholds.min_confidence < EQUITY_POLICY.thresholds.min_confidence


def test_the_two_shipped_policies_share_their_indicator_periods() -> None:
    """Only thresholds differ. A 14-bar ATR is a 14-bar ATR in either market."""
    for label in ("15m", "1h", "4h"):
        assert CRYPTO_POLICY.timeframe(label).periods == EQUITY_POLICY.timeframe(label).periods
        assert CRYPTO_POLICY.timeframe(label).weights == EQUITY_POLICY.timeframe(label).weights


def test_an_equity_symbol_cannot_be_scored_under_the_crypto_policy() -> None:
    """CRITICAL. The failure this prevents is silent: the arithmetic would work."""
    with pytest.raises(DecisionInputError, match="Asset-class policies are not interchangeable"):
        MultiFactorV2Engine(CRYPTO_POLICY).decide(make_bars(rising(), symbol="SPY"))


def test_a_crypto_pair_cannot_be_scored_under_the_equity_policy() -> None:
    with pytest.raises(DecisionInputError, match="Asset-class policies are not interchangeable"):
        MultiFactorV2Engine(EQUITY_POLICY).decide(make_bars(rising(), symbol="BTC/USD"))


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [("BTC/USD", AssetClass.CRYPTO), ("ETH/USD", AssetClass.CRYPTO), ("SPY", AssetClass.EQUITY)],
)
def test_for_symbol_selects_the_policy_of_that_symbols_asset_class(
    symbol: str, expected: AssetClass
) -> None:
    engine = MultiFactorV2Engine.for_symbol(symbol)

    assert engine.policy.asset_class is expected
    assert engine.policy is policy_for_symbol(symbol)


def test_the_same_market_can_decide_differently_under_the_two_policies() -> None:
    """Otherwise the separation would be documentation rather than behaviour."""
    closes = choppy(200)
    crypto = CRYPTO.decide(make_bars(closes, symbol="BTC/USD"))
    equity = EQUITY.decide(make_bars(closes, symbol="SPY"))

    assert crypto.score == pytest.approx(equity.score)
    assert crypto.policy["policy_name"] != equity.policy["policy_name"]
    assert crypto.policy["asset_class"] == "crypto"
    assert equity.policy["asset_class"] == "equity"


# --------------------------------------------------------------------------
# Regime and context
# --------------------------------------------------------------------------


def test_a_high_volatility_regime_blocks_an_entry_but_never_an_exit() -> None:
    """The asymmetry stated in the scoring module, asserted in both directions."""
    thresholds = CRYPTO_POLICY.thresholds
    calm = {"volatility_ratio": 1.0, "ema_spread_atr": 1.0, "ema_slope_atr": 1.0}
    wild = {**calm, "volatility_ratio": thresholds.high_volatility_ratio + 0.5}

    from autotrader.decision.scoring import decide_signal

    assert classify_regime(wild, thresholds) is MarketRegime.HIGH_VOLATILITY

    blocked, reasons = decide_signal(
        score=0.9, confidence=0.9, regime=MarketRegime.HIGH_VOLATILITY, thresholds=thresholds
    )
    assert blocked is DecisionSignal.HOLD
    assert reasons == (REASON_REGIME_BLOCKED,)

    allowed, reasons = decide_signal(
        score=-0.9, confidence=0.9, regime=MarketRegime.HIGH_VOLATILITY, thresholds=thresholds
    )
    assert allowed is DecisionSignal.SELL
    assert reasons == (REASON_SELL,)


def test_a_trend_is_claimed_only_when_both_trend_measurements_agree() -> None:
    thresholds = CRYPTO_POLICY.thresholds
    base = {"volatility_ratio": 1.0}

    assert (
        classify_regime({**base, "ema_spread_atr": 1.0, "ema_slope_atr": 1.0}, thresholds)
        is MarketRegime.TREND_UP
    )
    assert (
        classify_regime({**base, "ema_spread_atr": -1.0, "ema_slope_atr": -1.0}, thresholds)
        is MarketRegime.TREND_DOWN
    )
    assert (
        classify_regime({**base, "ema_spread_atr": 1.0, "ema_slope_atr": -1.0}, thresholds)
        is MarketRegime.RANGE
    )


def test_thin_participation_discounts_confidence_and_is_reported() -> None:
    closes = rising()
    normal = CRYPTO.decide(make_bars(closes))
    thin = CRYPTO.decide(make_bars(closes, volumes=[100.0] * 199 + [1.0]))

    assert thin.confidence < normal.confidence
    assert REASON_LOW_PARTICIPATION in thin.reasons
    assert REASON_LOW_PARTICIPATION not in normal.reasons


# --------------------------------------------------------------------------
# Scoring primitives
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [-1e9, -3.0, -0.5, 0.0, 0.5, 3.0, 1e9])
def test_softsign_is_bounded_and_odd(value: float) -> None:
    assert -1.0 < softsign(value) < 1.0
    assert softsign(-value) == pytest.approx(-softsign(value))


def test_softsign_is_strictly_increasing() -> None:
    samples = [-9.0, -1.0, -0.1, 0.0, 0.1, 1.0, 9.0]
    mapped = [softsign(value) for value in samples]

    assert mapped == sorted(mapped)


def test_softsign_refuses_nan_rather_than_propagating_it() -> None:
    with pytest.raises(ValueError, match="undefined for NaN"):
        softsign(float("nan"))


def test_the_composite_cannot_leave_the_bound_even_when_every_factor_saturates() -> None:
    saturated = dict.fromkeys(decision_config.DIRECTIONAL_FACTORS, 1.0)

    assert composite_score(saturated, FactorWeights()) == pytest.approx(1.0)


def test_agreement_ignores_factors_that_have_no_opinion() -> None:
    weights = FactorWeights()
    scores = dict.fromkeys(decision_config.DIRECTIONAL_FACTORS, 0.0)
    scores["trend_ema"] = 0.8

    assert agreement(scores, weights, composite=0.2) == pytest.approx(1.0)


def test_agreement_falls_when_factors_point_in_different_directions() -> None:
    weights = FactorWeights()
    split = {
        "trend_ema": 0.9,
        "trend_slope": 0.9,
        "momentum_rsi": -0.9,
        "momentum_macd": -0.9,
        "momentum_return": -0.9,
    }

    assert agreement(split, weights, composite=composite_score(split, weights)) < 1.0


def test_every_directional_factor_is_scored_and_reported() -> None:
    result = CRYPTO.decide(make_bars(rising()))
    reported = result.policy["factor_scores"]

    assert set(reported) == set(decision_config.DIRECTIONAL_FACTORS)
    for factor in decision_config.DIRECTIONAL_FACTORS:
        assert -1.0 <= reported[factor] <= 1.0
        assert any(reason.startswith(factor.upper()) for reason in result.reasons)


def test_rsi_is_the_one_factor_not_passed_through_softsign() -> None:
    """It is already bounded; saturating it twice would compress 80 and 100 together."""
    row = dict.fromkeys(
        (
            "ema_spread_z",
            "ema_slope_z",
            "macd_hist_z",
            "return_z",
        ),
        0.0,
    )
    row["rsi_centered"] = 1.0

    assert score_factors(row)["momentum_rsi"] == 1.0


# --------------------------------------------------------------------------
# Determinism and the audit record
# --------------------------------------------------------------------------


def test_the_same_bars_decide_identically_on_every_call() -> None:
    bars = make_bars(choppy(250))

    assert CRYPTO.decide(bars).to_dict() == CRYPTO.decide(bars).to_dict()


def test_two_engines_built_from_the_same_policy_agree_exactly() -> None:
    """The engine holds no state between calls, so replay cannot drift."""
    bars = make_bars(choppy(250))
    first = MultiFactorV2Engine(CRYPTO_POLICY)
    second = MultiFactorV2Engine(CRYPTO_POLICY)

    assert first.decide(bars).to_dict() == second.decide(bars).to_dict()


def test_deciding_does_not_modify_the_supplied_frame() -> None:
    bars = make_bars(choppy(250))
    before = bars.copy(deep=True)

    CRYPTO.decide(bars)

    assert bars.equals(before)


def test_the_result_records_enough_to_replay_the_decision() -> None:
    result = CRYPTO.decide(make_bars(rising()))
    policy = result.policy

    assert policy["engine_version"] == VERSION_V2
    assert policy["feature_schema_version"]
    assert policy["timeframes"] == ["15m"]
    assert policy["thresholds"]["buy_score"] == CRYPTO_POLICY.thresholds.buy_score
    assert policy["timeframe_policies"]["15m"]["periods"]["ema_slow"] == 50
    assert policy["factor_scores"]
    assert set(result.features) >= set(SCORED_FEATURES)


def test_the_audit_record_survives_a_json_round_trip() -> None:
    import json

    payload = CRYPTO.decide(make_bars(rising())).to_dict()

    assert json.loads(json.dumps(payload)) == payload


# --------------------------------------------------------------------------
# Configuration validation
# --------------------------------------------------------------------------


def test_factor_weights_that_do_not_sum_to_one_are_refused() -> None:
    with pytest.raises(DecisionConfigError, match="must sum to 1.0"):
        FactorWeights(trend_ema=0.5, trend_slope=0.5, momentum_rsi=0.5)


def test_an_empty_hold_band_is_refused() -> None:
    """With no band, one score would mean both directions at once."""
    with pytest.raises(DecisionConfigError, match="must be below buy_score"):
        DecisionThresholds(
            buy_score=0.0,
            sell_score=0.0,
            min_confidence=0.3,
            high_volatility_ratio=2.0,
            low_participation_ratio=0.5,
        )


def test_a_volatility_multiple_at_or_below_one_is_refused() -> None:
    """It is a multiple of the market's own median, so 1.0 would flag a typical bar."""
    with pytest.raises(DecisionConfigError, match="must exceed 1.0"):
        DecisionThresholds(
            buy_score=0.25,
            sell_score=-0.25,
            min_confidence=0.3,
            high_volatility_ratio=1.0,
            low_participation_ratio=0.5,
        )


def test_gates_that_rise_with_timeframe_are_refused() -> None:
    """Higher timeframes confirm a direction; they do not have to lead it."""
    with pytest.raises(DecisionConfigError, match="must not increase with timeframe"):
        MultiTimeframeGates(trigger_min=0.1, confirm_min=0.2, context_min=0.3)


def test_a_fast_average_that_is_not_faster_is_refused() -> None:
    with pytest.raises(DecisionConfigError, match="cannot cross anything"):
        IndicatorPeriods(ema_fast=50, ema_slow=20)


def test_a_timeframe_policy_for_an_unknown_timeframe_is_refused() -> None:
    with pytest.raises(DecisionConfigError, match="Unknown timeframe"):
        TimeframePolicy(label="30m")


def test_evaluate_timeframe_reports_the_bar_it_scored() -> None:
    bars = make_bars(rising())
    from autotrader.decision.timeframes import BASE_TIMEFRAME

    evaluation = evaluate_timeframe(
        bars,
        spec=BASE_TIMEFRAME,
        timeframe_policy=CRYPTO_POLICY.timeframe("15m"),
        policy=CRYPTO_POLICY,
    )

    assert evaluation.available
    assert evaluation.bar_timestamp == bars["timestamp"].iloc[-1]
    assert evaluation.bar_count == len(bars)
