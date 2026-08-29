"""Tests for the research-only V1-V5 historical evaluation study.

These pin the three claims the study's conclusions rest on: that the dataset
correction touches nothing an engine reads, that the fast replay path produces
exactly what the faithful one produces, and that the faithful path actually
survives the leakage auditor. A study whose adapters were wrong would produce
confident numbers about nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studies.crypto_v1_v5.adapters import (  # noqa: E402
    DecisionRecord,
    DecisionSeriesEngine,
    LiveDecisionEngine,
    memoize_engine_call,
)
from studies.crypto_v1_v5.dataset import renull_undefined_vwap  # noqa: E402
from studies.crypto_v1_v5.scoring import (  # noqa: E402
    SHARED_LOOKBACK_BARS,
    ScoringError,
    is_unavailable,
    score_window,
)
from studies.crypto_v1_v5.walkforward import (  # noqa: E402
    TRAIN_TEST_GAP_BARS,
    WalkForwardError,
    plan_folds,
)

from autotrader.decision.contract import DecisionSignal  # noqa: E402
from autotrader.decision.v1 import EmaCrossV1Engine  # noqa: E402
from autotrader.decision.v2 import MultiFactorV2Engine  # noqa: E402
from autotrader.research.engines import Action  # noqa: E402
from autotrader.research.leakage import audit_engine_causality  # noqa: E402
from research_fixtures import bars_from_closes, wave  # noqa: E402

PRICE_COLUMNS = ["open", "high", "low", "close"]


# --------------------------------------------------------------------------
# Dataset correction
# --------------------------------------------------------------------------


def _frame_with_sentinels() -> pd.DataFrame:
    frame = bars_from_closes([100.0, 101.0, 102.0, 103.0, 104.0])
    frame.loc[1, ["volume", "trade_count", "vwap"]] = [0.0, 0, 0.0]
    frame.loc[3, ["volume", "trade_count", "vwap"]] = [0.0, 0, 0.0]
    return frame


def test_renull_only_touches_no_trade_rows() -> None:
    frame = _frame_with_sentinels()
    corrected, count = renull_undefined_vwap(frame)

    assert count == 2
    assert corrected.loc[[1, 3], "vwap"].isna().all()
    assert corrected.loc[[0, 2, 4], "vwap"].notna().all()


def test_renull_leaves_every_price_and_volume_untouched() -> None:
    frame = _frame_with_sentinels()
    corrected, _ = renull_undefined_vwap(frame)

    for column in [*PRICE_COLUMNS, "volume", "trade_count", "timestamp", "symbol"]:
        pd.testing.assert_series_equal(frame[column], corrected[column])


def test_renull_leaves_a_genuine_zero_vwap_alone_when_the_bar_traded() -> None:
    """A zero vwap on a bar that reported trades is a data fault, not a sentinel.

    Silently nulling it would hide exactly the corruption the validator exists
    to catch, so the correction is conditioned on the provider's own "no trades"
    markers rather than on the zero alone.
    """
    frame = bars_from_closes([100.0, 101.0, 102.0])
    frame.loc[1, "vwap"] = 0.0  # volume and trade_count stay non-zero

    corrected, count = renull_undefined_vwap(frame)

    assert count == 0
    assert corrected.loc[1, "vwap"] == 0.0


def test_renull_never_modifies_its_input() -> None:
    frame = _frame_with_sentinels()
    before = frame.copy()

    renull_undefined_vwap(frame)

    pd.testing.assert_frame_equal(frame, before)


# --------------------------------------------------------------------------
# Adapters
# --------------------------------------------------------------------------


def _live_v1(lookback: int = 60) -> LiveDecisionEngine:
    return LiveDecisionEngine(EmaCrossV1Engine(), name="v1", version="v1", lookback_bars=lookback)


def test_live_adapter_and_series_adapter_emit_identical_signals() -> None:
    """The fast path is only usable because it is the slow path's output."""
    bars = wave(320)
    live = _live_v1()

    records = live.decisions(bars)
    series = DecisionSeriesEngine(records, name="v1", version="v1", warmup_bars=0)

    assert tuple(live.generate(bars)) == tuple(series.generate(bars))


def test_hold_emits_no_proposal_and_directions_map_to_the_two_actions() -> None:
    moment = pd.Timestamp("2025-01-01", tz="UTC")
    common = {"symbol": "BTC/USD", "score": 0.0, "confidence": 0.5, "regime": "RANGE"}

    hold = DecisionRecord(timestamp=moment, signal=DecisionSignal.HOLD, reasons=("X",), **common)
    buy = DecisionRecord(timestamp=moment, signal=DecisionSignal.BUY, reasons=("X",), **common)
    sell = DecisionRecord(timestamp=moment, signal=DecisionSignal.SELL, reasons=("X",), **common)

    assert hold.to_signal() is None
    assert buy.to_signal().action is Action.ENTER_LONG
    assert sell.to_signal().action is Action.EXIT_LONG


def test_signal_strength_carries_the_engines_own_confidence() -> None:
    record = DecisionRecord(
        timestamp=pd.Timestamp("2025-01-01", tz="UTC"),
        symbol="BTC/USD",
        signal=DecisionSignal.BUY,
        score=0.6,
        confidence=0.42,
        regime="TREND_UP",
        reasons=("A", "B"),
    )

    signal = record.to_signal()

    assert signal.strength == pytest.approx(0.42)
    assert signal.reason == "A|B"


def test_series_adapter_refuses_two_answers_for_one_instant() -> None:
    moment = pd.Timestamp("2025-01-01", tz="UTC")
    duplicate = [
        DecisionRecord(
            timestamp=moment,
            symbol="BTC/USD",
            signal=signal,
            score=0.0,
            confidence=0.5,
            regime="RANGE",
            reasons=("X",),
        )
        for signal in (DecisionSignal.BUY, DecisionSignal.SELL)
    ]

    with pytest.raises(Exception, match="two answers"):
        DecisionSeriesEngine(duplicate, name="v1", version="v1", warmup_bars=0)


def test_series_adapter_restricts_itself_to_the_frame_it_is_given() -> None:
    bars = wave(320)
    live = _live_v1()
    series = DecisionSeriesEngine(live.decisions(bars), name="v1", version="v1", warmup_bars=0)

    half = bars.iloc[:160].reset_index(drop=True)
    emitted = series.generate(half)

    assert all(signal.timestamp in set(half["timestamp"]) for signal in emitted)


# --------------------------------------------------------------------------
# Causality
# --------------------------------------------------------------------------


def test_live_adapter_survives_the_leakage_auditor() -> None:
    """Perturbing the future must not change what the engine already decided."""
    report = audit_engine_causality(_live_v1(), wave(300), probes=4)

    assert report.clean, report.findings


def test_only_the_live_adapter_claims_to_be_auditable() -> None:
    """A lookup table passes a causality audit for a reason that is not evidence."""
    assert LiveDecisionEngine.audit_ready is True
    assert DecisionSeriesEngine.audit_ready is False


# --------------------------------------------------------------------------
# Memoization
# --------------------------------------------------------------------------


def test_memoized_engine_returns_what_the_unmemoized_one_returns() -> None:
    bars = wave(400)
    plain = MultiFactorV2Engine.for_symbol("BTC/USD")
    cached = memoize_engine_call(MultiFactorV2Engine.for_symbol("BTC/USD"), "decide")

    for end in (200, 260, 331, 400):
        window = bars.iloc[end - 150 : end].reset_index(drop=True)
        expected = plain.decide(window)
        actual = cached.decide(window)
        assert (actual.signal, actual.score, actual.confidence) == (
            expected.signal,
            expected.score,
            expected.confidence,
        )


def test_memoization_distinguishes_windows_that_end_at_the_same_bar() -> None:
    """The cache key is the window, not just its last instant.

    Two frames ending on the same bar with different amounts of history are
    different questions, and an EMA seeded over 120 bars is not the one seeded
    over 150. Keying on the final timestamp alone would serve the wrong answer.
    """
    bars = wave(400)
    cached = memoize_engine_call(MultiFactorV2Engine.for_symbol("BTC/USD"), "decide")
    plain = MultiFactorV2Engine.for_symbol("BTC/USD")

    short = bars.iloc[280:400].reset_index(drop=True)
    long = bars.iloc[250:400].reset_index(drop=True)

    assert cached.decide(short).score == plain.decide(short).score
    assert cached.decide(long).score == plain.decide(long).score


# --------------------------------------------------------------------------
# Scoring pass
# --------------------------------------------------------------------------


def test_unavailability_is_detected_from_the_engines_own_reason_tokens() -> None:
    assert is_unavailable(("INSUFFICIENT_HISTORY_4H",))
    assert is_unavailable(("FEATURE_UNAVAILABLE_15M",))
    assert not is_unavailable(("NO_CROSSOVER_ON_LATEST_BAR", "REGIME_RANGE"))


def test_score_window_refuses_a_start_without_room_for_its_lookback() -> None:
    with pytest.raises(ScoringError, match="history"):
        score_window(
            wave(300),
            panel=None,  # never reached: the range is validated first
            first_decision_index=10,
            last_decision_index=20,
            lookback_bars=100,
        )


def test_shared_lookback_exceeds_every_declared_requirement() -> None:
    """The study's window must clear the largest engine warm-up, with room to spare.

    V3 declares 1744 and needs more than that in practice, because aggregating
    15-minute bars onto 4-hour boundaries costs alignment that the declared
    figure does not include.
    """
    from autotrader.decision.v3 import MultiTimeframeV3Engine

    declared = MultiTimeframeV3Engine.for_symbol("BTC/USD").required_base_bars

    assert declared < SHARED_LOOKBACK_BARS


# --------------------------------------------------------------------------
# Walk-forward plan
# --------------------------------------------------------------------------


def _plans():
    return plan_folds(
        oos_start=pd.Timestamp("2025-01-01", tz="UTC"),
        oos_end=pd.Timestamp("2026-08-28 23:45", tz="UTC"),
        dataset_start=pd.Timestamp("2024-01-01", tz="UTC"),
        holdout_windows=1,
    )


def test_every_fold_trains_strictly_before_the_window_it_scores() -> None:
    for plan in _plans():
        assert plan.train_end < plan.test_start


def test_the_gap_removes_the_label_horizon_and_an_embargo() -> None:
    gap = pd.Timedelta("15min") * TRAIN_TEST_GAP_BARS
    for plan in _plans():
        assert plan.test_start - plan.train_end == gap


def test_windows_are_consecutive_and_do_not_overlap() -> None:
    plans = _plans()
    for earlier, later in zip(plans, plans[1:], strict=False):
        assert earlier.test_end < later.test_start


def test_exactly_the_requested_number_of_windows_is_held_out_and_it_is_the_last() -> None:
    plans = _plans()
    held = [plan for plan in plans if plan.is_holdout]

    assert len(held) == 1
    assert held[0].fold_id == plans[-1].fold_id


def test_an_empty_out_of_sample_range_is_refused() -> None:
    with pytest.raises(WalkForwardError):
        plan_folds(
            oos_start=pd.Timestamp("2026-01-01", tz="UTC"),
            oos_end=pd.Timestamp("2025-01-01", tz="UTC"),
            dataset_start=pd.Timestamp("2024-01-01", tz="UTC"),
        )


def test_series_adapter_keys_decisions_by_symbol_as_well_as_instant() -> None:
    """A portfolio replay drives one engine over two datasets whose bars share instants.

    Keying on the timestamp alone would hand one symbol's decision to the other
    symbol's bar, which is the kind of error that produces a plausible combined
    equity curve for a strategy nobody ran.
    """
    moment = pd.Timestamp("2025-01-01", tz="UTC")
    records = [
        DecisionRecord(
            timestamp=moment,
            symbol=symbol,
            signal=signal,
            score=0.0,
            confidence=0.5,
            regime="RANGE",
            reasons=("X",),
        )
        for symbol, signal in (("BTC/USD", DecisionSignal.BUY), ("ETH/USD", DecisionSignal.SELL))
    ]
    series = DecisionSeriesEngine(records, name="v1", version="v1", warmup_bars=0)

    btc = bars_from_closes([100.0], symbol="BTC/USD")
    btc.loc[0, "timestamp"] = moment
    eth = bars_from_closes([100.0], symbol="ETH/USD")
    eth.loc[0, "timestamp"] = moment

    assert series.generate(btc)[0].action is Action.ENTER_LONG
    assert series.generate(eth)[0].action is Action.EXIT_LONG


def test_series_adapter_refuses_a_frame_holding_two_symbols() -> None:
    records = [
        DecisionRecord(
            timestamp=pd.Timestamp("2025-01-01", tz="UTC"),
            symbol="BTC/USD",
            signal=DecisionSignal.BUY,
            score=0.0,
            confidence=0.5,
            regime="RANGE",
            reasons=("X",),
        )
    ]
    series = DecisionSeriesEngine(records, name="v1", version="v1", warmup_bars=0)
    mixed = pd.concat(
        [bars_from_closes([100.0], symbol="BTC/USD"), bars_from_closes([100.0], symbol="ETH/USD")],
        ignore_index=True,
    )

    with pytest.raises(Exception, match="2 symbols"):
        series.generate(mixed)
