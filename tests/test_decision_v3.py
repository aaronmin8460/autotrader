"""Decision Engine tests: V3, the same framework read on three timeframes.

The tests that matter here are about what V3 refuses. A weighted blend of three
scores is easy and would have let a loud 15-minute reading outvote two higher
timeframes pointing the other way; the gates are what stop that, and each is
tested at its own edge. The alignment tests prove the other half: that the
1-hour and 4-hour readings a decision used were fully closed at the moment the
15-minute bar it was made on closed.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from autotrader.decision.config import CRYPTO_POLICY, EQUITY_POLICY, IndicatorPeriods
from autotrader.decision.contract import (
    VERSION_V3,
    DecisionInputError,
    DecisionSignal,
    MarketRegime,
)
from autotrader.decision.scoring import REASON_LOW_CONFIDENCE, REASON_REGIME_BLOCKED
from autotrader.decision.timeframes import (
    FOUR_HOUR_TIMEFRAME,
    HOUR_TIMEFRAME,
    V3_TIMEFRAMES,
    aggregate_bars,
)
from autotrader.decision.v3 import (
    CONFIRM_TIMEFRAME,
    CONTEXT_TIMEFRAME,
    REASON_ALIGNED_BEARISH,
    REASON_ALIGNED_BULLISH,
    REASON_CONFIRM_GATE_UNMET,
    REASON_CONTEXT_GATE_UNMET,
    REASON_TRIGGER_GATE_UNMET,
    TRIGGER_TIMEFRAME,
    MultiTimeframeV3Engine,
)

FIRST_BAR = datetime(2025, 1, 1, tzinfo=UTC)
STEP = timedelta(minutes=15)
ENGINE = MultiTimeframeV3Engine.for_symbol("BTC/USD")
REQUIRED = ENGINE.required_base_bars


def make_bars(closes: list[float], *, symbol: str = "BTC/USD") -> pd.DataFrame:
    prices = [float(close) for close in closes]
    return pd.DataFrame(
        {
            "timestamp": [FIRST_BAR + STEP * index for index in range(len(prices))],
            "symbol": [symbol] * len(prices),
            "open": prices,
            "high": [price + 0.5 for price in prices],
            "low": [price - 0.5 for price in prices],
            "close": prices,
            "volume": [100.0] * len(prices),
            "trade_count": [10] * len(prices),
            "vwap": prices,
        }
    )


def rising(count: int = REQUIRED + 40, step: float = 0.05) -> list[float]:
    return [100.0 + step * index for index in range(count)]


def falling(count: int = REQUIRED + 40, step: float = 0.05) -> list[float]:
    return [100.0 + step * (count - index) for index in range(count)]


def choppy(count: int = REQUIRED + 40) -> list[float]:
    return [
        500.0 + 20.0 * math.sin(index / 37.0) + 6.0 * math.cos(index / 5.0)
        for index in range(count)
    ]


# --------------------------------------------------------------------------
# Alignment
# --------------------------------------------------------------------------


def test_every_timeframe_reading_had_fully_closed_when_the_base_bar_closed() -> None:
    """CRITICAL. docs/SPEC.md section 7F, across three timeframes at once."""
    result = ENGINE.decide(make_bars(rising()))
    base_close = result.timestamp + pd.Timedelta(minutes=15)

    for spec in V3_TIMEFRAMES:
        stamp = pd.Timestamp(result.policy["timeframe_bar_timestamps"][spec.label])
        assert stamp + pd.Timedelta(spec.interval) <= base_close, spec.label


def test_the_higher_timeframe_readings_are_the_newest_that_had_closed() -> None:
    """Not merely safe: the latest bar that was actually knowable, and no older."""
    bars = make_bars(rising())
    result = ENGINE.decide(bars)
    base_close = result.timestamp + pd.Timedelta(minutes=15)

    for spec in (HOUR_TIMEFRAME, FOUR_HOUR_TIMEFRAME):
        used = pd.Timestamp(result.policy["timeframe_bar_timestamps"][spec.label])
        complete = aggregate_bars(bars, spec)
        knowable = complete.loc[
            complete["timestamp"] + pd.Timedelta(spec.interval) <= base_close, "timestamp"
        ]
        assert used == knowable.iloc[-1], spec.label


def test_a_future_bar_cannot_change_a_decision_already_made() -> None:
    """Truncation invariance, at the engine level rather than the feature level."""
    closes = choppy(REQUIRED + 200)
    whole = make_bars(closes)
    prefix = make_bars(closes[: REQUIRED + 100])

    on_prefix = ENGINE.decide(prefix)
    on_whole = ENGINE.decide(make_bars(closes[: REQUIRED + 100]))

    assert on_prefix.to_dict() == on_whole.to_dict()
    assert ENGINE.decide(whole).timestamp > on_prefix.timestamp


def test_the_four_hour_reading_only_advances_once_a_bucket_has_closed() -> None:
    """Sixteen base bars apart, and not one bar sooner."""
    closes = choppy(REQUIRED + 40)
    stamps = []
    for extra in range(17):
        result = ENGINE.decide(make_bars(closes[: REQUIRED + 20 + extra]))
        stamps.append(pd.Timestamp(result.policy["timeframe_bar_timestamps"]["4h"]))

    distinct = sorted(set(stamps))
    assert len(distinct) == 2
    assert distinct[1] - distinct[0] == pd.Timedelta(hours=4)


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


def test_the_engine_states_the_history_the_context_timeframe_costs() -> None:
    periods = IndicatorPeriods()
    assert periods.required_bars * 16 == REQUIRED
    assert ENGINE.describe()["required_base_bars"] == REQUIRED


def test_too_little_history_for_the_context_holds_and_names_that_timeframe() -> None:
    result = ENGINE.decide(make_bars(rising(REQUIRED - 16)))

    assert result.signal is DecisionSignal.HOLD
    assert "INSUFFICIENT_HISTORY_4H" in result.reasons
    assert result.score == 0.0
    assert result.regime is MarketRegime.UNKNOWN


def test_a_missing_timeframe_stops_the_decision_rather_than_reweighting() -> None:
    """Guessing the context is worse than admitting there is none."""
    result = ENGINE.decide(make_bars(rising(200)))

    assert result.signal is DecisionSignal.HOLD
    assert "INSUFFICIENT_HISTORY_1H" in result.reasons
    assert "INSUFFICIENT_HISTORY_4H" in result.reasons


def test_exactly_the_required_history_is_enough() -> None:
    result = ENGINE.decide(make_bars(rising(REQUIRED)))

    assert not any(reason.startswith("INSUFFICIENT_HISTORY") for reason in result.reasons)


def test_the_equity_context_costs_far_more_base_bars_than_the_crypto_one() -> None:
    """A regular session completes one 4-hour bucket; a crypto day completes six."""
    crypto = MultiTimeframeV3Engine(CRYPTO_POLICY).required_base_bars
    equity = MultiTimeframeV3Engine(EQUITY_POLICY).required_base_bars

    assert equity > crypto
    assert equity == IndicatorPeriods().required_bars * 26


# --------------------------------------------------------------------------
# Direction and the gates
# --------------------------------------------------------------------------


def test_three_aligned_bullish_timeframes_buy() -> None:
    result = ENGINE.decide(make_bars(rising()))

    assert result.signal is DecisionSignal.BUY
    assert REASON_ALIGNED_BULLISH in result.reasons
    for spec in V3_TIMEFRAMES:
        assert result.policy["timeframe_scores"][spec.label] > 0.0


def test_three_aligned_bearish_timeframes_sell() -> None:
    result = ENGINE.decide(make_bars(falling()))

    assert result.signal is DecisionSignal.SELL
    assert REASON_ALIGNED_BEARISH in result.reasons


def test_a_bullish_trigger_without_context_does_not_buy() -> None:
    """CRITICAL. The trade a multi-timeframe engine exists to refuse.

    A long decline with a sharp rally at the end: the 15-minute reading turns
    positive while the 4-hour context is still firmly negative. A weighted
    average could be dragged over the line by the tactical score alone; the
    gates cannot be.
    """
    gates = CRYPTO_POLICY.gates
    closes = falling(REQUIRED + 40)
    turn = [closes[-1] + 0.25 * index for index in range(1, 33)]
    result = ENGINE.decide(make_bars([*closes, *turn]))
    scores = result.policy["timeframe_scores"]

    # Trigger and confirmation have both cleared their own gates.
    assert scores["15m"] >= gates.trigger_min
    assert scores["1h"] >= gates.confirm_min
    # The context has not, and it alone is enough to refuse the entry.
    assert scores["4h"] < gates.context_min
    assert result.signal is DecisionSignal.HOLD
    assert result.reasons[0] == REASON_CONTEXT_GATE_UNMET


def test_the_gates_are_checked_against_each_timeframes_own_bar() -> None:
    gates = CRYPTO_POLICY.gates
    result = ENGINE.decide(make_bars(rising()))
    scores = result.policy["timeframe_scores"]

    assert scores[TRIGGER_TIMEFRAME.label] >= gates.trigger_min
    assert scores[CONFIRM_TIMEFRAME.label] >= gates.confirm_min
    assert scores[CONTEXT_TIMEFRAME.label] >= gates.context_min


def test_the_gates_descend_from_trigger_to_context() -> None:
    """The role each timeframe plays, expressed as an ordering."""
    gates = CRYPTO_POLICY.gates

    assert gates.trigger_min >= gates.confirm_min >= gates.context_min


def test_an_entry_needs_the_context_but_an_exit_does_not() -> None:
    """The documented asymmetry, asserted directly on the gate logic."""
    engine = MultiTimeframeV3Engine(CRYPTO_POLICY)
    gates = CRYPTO_POLICY.gates

    from autotrader.decision.v2 import TimeframeEvaluation

    def evaluations(trigger: float, confirm: float, context: float) -> dict[str, object]:
        return {
            "15m": TimeframeEvaluation(label="15m", available=True, bar_count=1, score=trigger),
            "1h": TimeframeEvaluation(label="1h", available=True, bar_count=1, score=confirm),
            "4h": TimeframeEvaluation(label="4h", available=True, bar_count=1, score=context),
        }

    # Bullish trigger and confirmation, context opposed: refused.
    signal, reasons = engine._apply_gates(
        evaluations=evaluations(0.9, 0.9, -0.9),
        score=0.3,
        confidence=1.0,
        regime=MarketRegime.RANGE,
    )
    assert signal is DecisionSignal.HOLD
    assert REASON_CONTEXT_GATE_UNMET in reasons

    # The mirror image on the sell side is allowed: the context is not consulted.
    signal, reasons = engine._apply_gates(
        evaluations=evaluations(-0.9, -0.9, 0.9),
        score=-0.3,
        confidence=1.0,
        regime=MarketRegime.RANGE,
    )
    assert signal is DecisionSignal.SELL
    assert reasons == (REASON_ALIGNED_BEARISH,)

    # And a trigger below its own gate refuses in either direction.
    signal, reasons = engine._apply_gates(
        evaluations=evaluations(gates.trigger_min - 1e-9, 0.9, 0.9),
        score=0.3,
        confidence=1.0,
        regime=MarketRegime.RANGE,
    )
    assert signal is DecisionSignal.HOLD
    assert REASON_TRIGGER_GATE_UNMET in reasons


def test_a_confirmation_below_its_gate_refuses_an_otherwise_aligned_entry() -> None:
    engine = MultiTimeframeV3Engine(CRYPTO_POLICY)
    gates = CRYPTO_POLICY.gates

    from autotrader.decision.v2 import TimeframeEvaluation

    signal, reasons = engine._apply_gates(
        evaluations={
            "15m": TimeframeEvaluation(label="15m", available=True, bar_count=1, score=0.9),
            "1h": TimeframeEvaluation(
                label="1h", available=True, bar_count=1, score=gates.confirm_min - 1e-9
            ),
            "4h": TimeframeEvaluation(label="4h", available=True, bar_count=1, score=0.9),
        },
        score=0.5,
        confidence=1.0,
        regime=MarketRegime.RANGE,
    )

    assert signal is DecisionSignal.HOLD
    assert reasons == (REASON_CONFIRM_GATE_UNMET,)


def test_a_high_volatility_context_blocks_an_aligned_entry() -> None:
    engine = MultiTimeframeV3Engine(CRYPTO_POLICY)

    from autotrader.decision.v2 import TimeframeEvaluation

    signal, reasons = engine._apply_gates(
        evaluations={
            label: TimeframeEvaluation(label=label, available=True, bar_count=1, score=0.9)
            for label in ("15m", "1h", "4h")
        },
        score=0.9,
        confidence=1.0,
        regime=MarketRegime.HIGH_VOLATILITY,
    )

    assert signal is DecisionSignal.HOLD
    assert reasons == (REASON_REGIME_BLOCKED,)


def test_low_confidence_is_checked_before_any_gate() -> None:
    engine = MultiTimeframeV3Engine(CRYPTO_POLICY)

    from autotrader.decision.v2 import TimeframeEvaluation

    signal, reasons = engine._apply_gates(
        evaluations={
            label: TimeframeEvaluation(label=label, available=True, bar_count=1, score=0.9)
            for label in ("15m", "1h", "4h")
        },
        score=0.9,
        confidence=0.0,
        regime=MarketRegime.RANGE,
    )

    assert signal is DecisionSignal.HOLD
    assert reasons == (REASON_LOW_CONFIDENCE,)


# --------------------------------------------------------------------------
# The composite
# --------------------------------------------------------------------------


def test_the_composite_is_the_policy_weighted_mean_of_the_three_scores() -> None:
    result = ENGINE.decide(make_bars(rising()))
    scores = result.policy["timeframe_scores"]
    weights = CRYPTO_POLICY.timeframe_weights

    expected = sum(scores[label] * weight for label, weight in weights.items())
    assert result.score == pytest.approx(expected)


def test_the_composite_is_weighted_towards_context_rather_than_the_trigger() -> None:
    weights = CRYPTO_POLICY.timeframe_weights

    assert weights["4h"] > weights["1h"] > weights["15m"]
    assert sum(weights.values()) == pytest.approx(1.0)


@pytest.mark.parametrize("closes", [rising(), falling(), choppy()])
def test_score_and_confidence_always_respect_their_bounds(closes: list[float]) -> None:
    result = ENGINE.decide(make_bars(closes))

    assert -1.0 <= result.score <= 1.0
    assert 0.0 <= result.confidence <= 1.0


def test_the_reported_regime_is_the_context_timeframes() -> None:
    """ "Regime" in V3 means the broad state, which is what the 4-hour reading is for."""
    result = ENGINE.decide(make_bars(rising()))

    assert result.regime.value == result.policy["timeframe_regimes"]["4h"]


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------


def test_the_result_reports_every_timeframe_separately() -> None:
    result = ENGINE.decide(make_bars(rising()))

    for key in (
        "timeframe_scores",
        "timeframe_confidence",
        "timeframe_bar_counts",
        "timeframe_bar_timestamps",
        "timeframe_regimes",
        "factor_scores",
    ):
        assert set(result.policy[key]) == {"15m", "1h", "4h"}, key


def test_features_are_namespaced_by_timeframe() -> None:
    result = ENGINE.decide(make_bars(rising()))

    assert "15m.ema_spread_z" in result.features
    assert "1h.ema_spread_z" in result.features
    assert "4h.ema_spread_z" in result.features
    assert result.features["15m.ema_spread_z"] != result.features["4h.ema_spread_z"]


def test_the_engine_declares_which_timeframe_plays_which_role() -> None:
    roles = ENGINE.describe()["timeframe_roles"]

    assert roles == {"trigger": "15m", "confirm": "1h", "context": "4h"}


def test_the_audit_record_survives_a_json_round_trip() -> None:
    import json

    payload = ENGINE.decide(make_bars(rising())).to_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert payload["version"] == VERSION_V3


# --------------------------------------------------------------------------
# Determinism and separation
# --------------------------------------------------------------------------


def test_the_same_bars_decide_identically_on_every_call() -> None:
    bars = make_bars(choppy())

    assert ENGINE.decide(bars).to_dict() == ENGINE.decide(bars).to_dict()


def test_two_engines_built_from_the_same_policy_agree_exactly() -> None:
    bars = make_bars(choppy())

    assert (
        MultiTimeframeV3Engine(CRYPTO_POLICY).decide(bars).to_dict()
        == MultiTimeframeV3Engine(CRYPTO_POLICY).decide(bars).to_dict()
    )


def test_deciding_does_not_modify_the_supplied_frame() -> None:
    bars = make_bars(choppy())
    before = bars.copy(deep=True)

    ENGINE.decide(bars)

    assert bars.equals(before)


def test_an_equity_symbol_cannot_be_scored_under_the_crypto_policy() -> None:
    with pytest.raises(DecisionInputError, match="Asset-class policies are not interchangeable"):
        MultiTimeframeV3Engine(CRYPTO_POLICY).decide(make_bars(rising(), symbol="SPY"))
