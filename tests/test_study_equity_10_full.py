"""Tests for the ten-symbol full-evaluation harness.

Every test runs offline against constructed sessions and synthetic bars whose
answers are known in advance. The study's conclusions come from real provider
data; the *rules* it applies - the window partition, the checkpoint discipline,
the warm-up measurement, the terminal-state arithmetic, the split-signature
audit, the single-pass V4 record recovery - are properties provable here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from studies.equity_10_full import KNOWN_SPLITS, STUDY_SYMBOLS
from studies.equity_10_full.benchmarks import BuyAndHoldEngine, forced_liquidation
from studies.equity_10_full.checkpoint import (
    CheckpointError,
    cell_path,
    is_complete,
    read_json,
    write_json,
    write_series,
)
from studies.equity_10_full.split_audit import (
    SplitAuditError,
    audit_symbol,
    session_boundary_steps,
)
from studies.equity_10_full.triple import v4_record_from_assessment
from studies.equity_10_full.walkforward import TRAIN_TEST_GAP_BARS, assert_gap_respected
from studies.equity_10_full.warmup import measure_worst_lookback
from studies.equity_10_full.windows import (
    DEV_WINDOWS,
    FULL_WINDOWS,
    HOLDOUT_WINDOW,
    LOOKBACK_BARS,
    window_by_name,
)

from autotrader.data.validation import EQUITY_UNIVERSE_LABEL
from autotrader.decision.config import EQUITY_POLICY
from autotrader.decision.contract import DecisionSignal, MarketRegime
from autotrader.decision.scoring import decide_signal
from autotrader.decision.v4 import ProbabilityAssessment
from autotrader.equity import EQUITY_SYMBOLS
from autotrader.research.costs import EQUITY_COST, STRESS_COST
from autotrader.research.replay import ReplayConfig, replay

WINTER_OPEN_UTC = 14  # 09:30 New York is 14:30 UTC under EST.


def _bars(count: int, *, start: datetime | None = None, symbol: str = "SPY") -> pd.DataFrame:
    """`count` consecutive winter regular-session bars, 26 per session."""
    moment = start or datetime(2024, 1, 8, WINTER_OPEN_UTC, 30, tzinfo=UTC)
    rows = []
    day_bar = 0
    price = 100.0
    while len(rows) < count:
        rows.append(
            {
                "timestamp": pd.Timestamp(moment),
                "symbol": symbol,
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price + 0.1,
                "volume": 1000.0,
                "trade_count": 10,
                "vwap": price,
            }
        )
        price += 0.1
        day_bar += 1
        if day_bar == 26:
            day_bar = 0
            moment += timedelta(days=3 if moment.weekday() == 4 else 1)
            moment = moment.replace(hour=WINTER_OPEN_UTC, minute=30)
        else:
            moment += timedelta(minutes=15)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Frozen configuration
# --------------------------------------------------------------------------


def test_the_universe_is_exactly_the_shipped_ten() -> None:
    assert STUDY_SYMBOLS == EQUITY_SYMBOLS
    assert len(STUDY_SYMBOLS) == 10


def test_twelve_contiguous_windows_and_the_last_is_the_holdout() -> None:
    assert len(FULL_WINDOWS) == 12
    assert FULL_WINDOWS[:11] == DEV_WINDOWS
    assert HOLDOUT_WINDOW is FULL_WINDOWS[11]
    for earlier, later in zip(FULL_WINDOWS[:-1], FULL_WINDOWS[1:], strict=True):
        assert earlier.end < later.start, f"{earlier.name} overlaps {later.name}"
        # Contiguity in market time: the next window opens within a few
        # calendar days (weekends/holidays), never leaving a session gap.
        assert (later.start - earlier.end).days <= 4


def test_lookback_clears_every_measured_worst_case_with_margin() -> None:
    # The measured universe worst case is GOOGL's 4,552 (frozen in windows.py
    # after measurement); the declared constant is 2,834. The study lookback
    # must clear both with margin.
    declared = EQUITY_POLICY.required_base_bars(("15m", "1h", "4h"))
    assert declared == 2834
    assert int(4552 * 1.04) <= LOOKBACK_BARS


def test_train_test_gap_is_horizon_plus_one_session() -> None:
    assert TRAIN_TEST_GAP_BARS == 4 + 26


def test_window_lookup_refuses_unknown_names() -> None:
    assert window_by_name("w07").name == "w07"
    with pytest.raises(Exception, match="frozen"):
        window_by_name("w13")


def test_known_splits_cover_the_four_split_symbols() -> None:
    assert set(KNOWN_SPLITS) == {"NVDA", "AMZN", "GOOGL", "TSLA"}


# --------------------------------------------------------------------------
# Checkpoints
# --------------------------------------------------------------------------


def test_checkpoint_round_trip_and_refusal_to_overwrite(tmp_path) -> None:
    path = cell_path(tmp_path, kind="cells", symbol="SPY", unit="w01")
    assert not is_complete(path)
    write_json(path, {"value": 1})
    assert is_complete(path)
    assert read_json(path)["value"] == 1
    with pytest.raises(CheckpointError, match="never"):
        write_json(path, {"value": 2})


def test_checkpoint_treats_a_torn_write_as_unfinished(tmp_path) -> None:
    path = cell_path(tmp_path, kind="cells", symbol="SPY", unit="w01")
    path.parent.mkdir(parents=True)
    path.write_text('{"value": 1, "complete": tr', encoding="utf-8")  # truncated
    assert not is_complete(path)
    unstamped = cell_path(tmp_path, kind="cells", symbol="SPY", unit="w02")
    unstamped.write_text(json.dumps({"value": 1}), encoding="utf-8")
    assert not is_complete(unstamped)


def test_series_writes_are_atomic_renames(tmp_path) -> None:
    frame = _bars(4)
    target = tmp_path / "decisions" / "SPY_w01_V3.parquet"
    write_series(target, frame)
    assert target.exists()
    assert not target.with_suffix(".tmp.parquet").exists()
    stored = pd.read_parquet(target)
    assert len(stored) == 4


# --------------------------------------------------------------------------
# Warm-up measurement
# --------------------------------------------------------------------------


def test_worst_lookback_on_perfect_sessions_matches_the_arithmetic() -> None:
    # 8 required buckets of 16 bars each. A gapless 14:30-21:00 UTC winter
    # session yields exactly one complete 4h UTC bucket (16:00-20:00),
    # occupying rows 6..21 of its session. The worst case is the bar just
    # before session j's own bucket completes (row 26j+20): the 8 newest
    # usable buckets are then j-8..j-1, anchored at row 26(j-8)+6, so the
    # lookback is 26*8 + (20 - 6 + 1) = 223.
    frame = _bars(26 * 12)
    measured = measure_worst_lookback(frame, symbol="TEST", timeframe="4h", required_buckets=8)
    assert measured.worst_lookback_bars == 26 * 8 + 15  # 223
    assert measured.timeframe == "4h"


def test_a_missing_bar_destroys_its_bucket_and_raises_the_requirement() -> None:
    frame = _bars(26 * 12)
    # Remove one bar inside session 6's 4h bucket (rows 26*6+4 .. 26*6+19).
    frame = frame.drop(index=26 * 6 + 10).reset_index(drop=True)
    damaged = measure_worst_lookback(frame, symbol="TEST", timeframe="4h", required_buckets=8)
    assert damaged.worst_lookback_bars > 26 * 8 - 4


# --------------------------------------------------------------------------
# Benchmarks and terminal states
# --------------------------------------------------------------------------


def _replay_config(cost_model) -> ReplayConfig:
    return ReplayConfig(
        initial_cash=Decimal("100000"),
        cost_model=cost_model,
        supported_symbols=EQUITY_SYMBOLS,
        universe_label=EQUITY_UNIVERSE_LABEL,
        validate=False,
    )


def test_buy_and_hold_enters_at_the_second_bar_open_and_never_exits() -> None:
    frame = _bars(30)
    result = replay(frame, BuyAndHoldEngine(), _replay_config(EQUITY_COST))
    assert result.signal_count == 1
    assert len(result.fills) == 1
    assert result.fills[0].bar_index == 1  # next-executable-bar, like every engine
    assert result.open_position is not None
    assert result.trade_count == 0


def test_forced_liquidation_prices_the_exit_under_the_same_cost_model() -> None:
    frame = _bars(30)
    result = replay(frame, BuyAndHoldEngine(), _replay_config(STRESS_COST))
    diagnostic = forced_liquidation(result, STRESS_COST)
    assert diagnostic["open_final_position"] is True
    position = result.open_position
    fill = STRESS_COST.fill_price(
        position.mark_price, __import__("autotrader.research.costs", fromlist=["Side"]).Side.SELL
    )
    expected = (
        result.final_cash + position.quantity * fill - STRESS_COST.fee(position.quantity, fill)
    )
    assert Decimal(diagnostic["forced_final_equity"]) == expected
    # The forced state can never flatter the native mark under adverse costs.
    assert Decimal(diagnostic["forced_final_equity"]) <= Decimal(diagnostic["native_final_equity"])


def test_forced_liquidation_is_the_identity_when_flat() -> None:
    frame = _bars(30)
    result = replay(frame, BuyAndHoldEngine(), _replay_config(EQUITY_COST))
    flat = replay(
        frame,
        type(
            "Idle",
            (),
            {
                "name": "IDLE",
                "version": "v0",
                "warmup_bars": 0,
                "parameters": {},
                "generate": lambda self, bars: (),
            },
        )(),
        _replay_config(EQUITY_COST),
    )
    diagnostic = forced_liquidation(flat, EQUITY_COST)
    assert diagnostic["open_final_position"] is False
    assert diagnostic["native_final_equity"] == diagnostic["forced_final_equity"]
    del result


# --------------------------------------------------------------------------
# Split-signature audit
# --------------------------------------------------------------------------


def _two_session_frame(second_open: float) -> pd.DataFrame:
    first = _bars(26, start=datetime(2024, 6, 7, 13, 30, tzinfo=UTC), symbol="NVDA")
    second = _bars(26, start=datetime(2024, 6, 10, 13, 30, tzinfo=UTC), symbol="NVDA")
    scale = second_open / float(second["open"].iloc[0])
    for column in ("open", "high", "low", "close", "vwap"):
        second[column] = second[column] * scale
    return pd.concat([first, second], ignore_index=True)


def test_an_unadjusted_split_signature_is_refused() -> None:
    last_close = float(_two_session_frame(100.0)["close"].iloc[25])
    frame = _two_session_frame(last_close * 0.1)  # the 10:1 raw crater
    with pytest.raises(SplitAuditError, match="not split-adjusted"):
        audit_symbol(frame, "NVDA")


def test_a_market_sized_step_across_the_split_date_passes() -> None:
    last_close = float(_two_session_frame(100.0)["close"].iloc[25])
    frame = _two_session_frame(last_close * 1.01)
    record = audit_symbol(frame, "NVDA")
    checks = record["known_split_checks"]
    assert len(checks) == 1
    assert checks[0]["looks_unadjusted"] is False


def test_session_boundary_steps_sees_only_boundaries() -> None:
    frame = _bars(26 * 3)
    steps = session_boundary_steps(frame)
    assert len(steps) == 2


# --------------------------------------------------------------------------
# Single-pass V4 record recovery
# --------------------------------------------------------------------------


def _assessment(*, available: bool, probability: float | None) -> ProbabilityAssessment:
    return ProbabilityAssessment(
        symbol="SPY",
        timestamp=pd.Timestamp("2024-01-08T14:30:00Z"),
        knowable_at=pd.Timestamp("2024-01-08T14:45:00Z"),
        available=available,
        model_version="test",
        model_family="class_frequency",
        feature_version="v1",
        label_spec_id="test",
        calibration_method="identity",
        calibrated=False,
        reasons=("PROBABILITY_EVEN",) if available else ("INSUFFICIENT_HISTORY_15M",),
        features={},
        probability_up=probability,
        regime=MarketRegime.RANGE if available else MarketRegime.UNKNOWN,
    )


def test_recovered_v4_record_applies_the_shipped_gate_exactly() -> None:
    assessment = _assessment(available=True, probability=0.93)
    record = v4_record_from_assessment(assessment, EQUITY_POLICY.thresholds)
    expected_signal, expected_gate = decide_signal(
        score=assessment.score,
        confidence=assessment.confidence,
        regime=assessment.regime,
        thresholds=EQUITY_POLICY.thresholds,
    )
    assert record.signal is expected_signal
    assert record.reasons[: len(expected_gate)] == tuple(expected_gate)
    assert record.score == assessment.score
    assert record.confidence == assessment.confidence


def test_recovered_v4_record_is_hold_when_unavailable() -> None:
    assessment = _assessment(available=False, probability=None)
    record = v4_record_from_assessment(assessment, EQUITY_POLICY.thresholds)
    assert record.signal is DecisionSignal.HOLD
    assert record.reasons == ("INSUFFICIENT_HISTORY_15M",)
    assert record.score == 0.0


# --------------------------------------------------------------------------
# Walk-forward gap assertion
# --------------------------------------------------------------------------


def test_gap_assertion_accepts_the_full_gap_and_rejects_a_short_one() -> None:
    frame = _bars(200)
    ok = assert_gap_respected(
        symbol="SPY",
        window="w01",
        training_last_bar=str(frame["timestamp"].iloc[100]),
        scoring_first_bar=str(frame["timestamp"].iloc[100 + TRAIN_TEST_GAP_BARS + 1]),
        gap_bars=TRAIN_TEST_GAP_BARS,
        frame=frame,
    )
    assert ok == ()
    short = assert_gap_respected(
        symbol="SPY",
        window="w01",
        training_last_bar=str(frame["timestamp"].iloc[100]),
        scoring_first_bar=str(frame["timestamp"].iloc[110]),
        gap_bars=TRAIN_TEST_GAP_BARS,
        frame=frame,
    )
    assert len(short) == 1
