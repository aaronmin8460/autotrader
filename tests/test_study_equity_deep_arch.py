"""Tests for the equity deep-architecture research harness.

Entirely offline, on constructed frames whose answers are known in advance.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from studies.equity_deep_arch.overlay import (
    OverlayError,
    participation_overlay,
    source_stance,
)
from studies.equity_deep_arch.state import (
    ParticipationSpec,
    StateInputError,
    participation_series,
    per_bar_participation,
    session_closes,
)
from studies.equity_v1_v5.adapters import DecisionRecord

from autotrader.decision.contract import DecisionSignal


def _bars(closes: list[float], start_day: int = 1) -> pd.DataFrame:
    """One 15m bar per session at 15:00 UTC, sequential January-onwards days."""
    stamps = []
    day = datetime(2024, 1, start_day, 15, 0, tzinfo=UTC)
    for _ in range(len(closes)):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        stamps.append(pd.Timestamp(day))
        day += timedelta(days=1)
    return pd.DataFrame({"timestamp": stamps, "close": closes})


def _record(ts: pd.Timestamp, signal: DecisionSignal) -> DecisionRecord:
    return DecisionRecord(
        timestamp=ts,
        symbol="SPY",
        signal=signal,
        score=0.5,
        confidence=0.5,
        regime="TEST",
        reasons=("TEST",),
    )


class TestParticipationState:
    def test_state_lags_one_session(self) -> None:
        """Session s's state must not read session s's own close."""
        spec = ParticipationSpec(sma_sessions=3)
        base = [10.0, 10.0, 10.0, 12.0, 1.0]  # crash on the last session
        closes = session_closes(_bars(base))
        series = participation_series(closes, spec)
        # The crash session itself still sees the pre-crash info set.
        assert bool(series["participate"].iloc[4]) is True
        # A following session would see the crash; extend and check.
        extended = session_closes(_bars([*base, 1.0]))
        series2 = participation_series(extended, spec)
        assert bool(series2["participate"].iloc[5]) is False

    def test_warmup_defaults_to_defensive(self) -> None:
        spec = ParticipationSpec(sma_sessions=200)
        closes = session_closes(_bars([100.0 + i for i in range(50)]))
        series = participation_series(closes, spec)
        assert not series["participate"].any()

    def test_drawdown_gate(self) -> None:
        """Above the SMA but more than 5 % off the trailing peak: defensive."""
        spec = ParticipationSpec(sma_sessions=3)
        prices = [100.0, 100.0, 100.0, 200.0, 170.0, 170.0]
        closes = session_closes(_bars(prices))
        series = participation_series(closes, spec)
        # Info set for the last session: close 170 > SMA(100,200,170)=156.7,
        # but drawdown 170/200-1 = -15% <= -5% -> defensive.
        assert bool(series["participate"].iloc[5]) is False

    def test_future_perturbation_does_not_change_past_state(self) -> None:
        spec = ParticipationSpec(sma_sessions=3)
        prices = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
        closes = session_closes(_bars(prices))
        baseline = participation_series(closes, spec)
        shocked = prices.copy()
        shocked[-1] = 1.0
        closes2 = session_closes(_bars(shocked))
        perturbed = participation_series(closes2, spec)
        cut = len(prices) - 1
        assert baseline["participate"].iloc[:cut].tolist() == (
            perturbed["participate"].iloc[:cut].tolist()
        )

    def test_missing_session_state_is_an_error(self) -> None:
        spec = ParticipationSpec(sma_sessions=3)
        frame = _bars([100.0, 101.0, 102.0])
        closes = session_closes(frame.iloc[:2])
        participation = participation_series(closes, spec)
        with pytest.raises(StateInputError):
            per_bar_participation(frame, participation)


class TestOverlay:
    def test_source_stance_tracks_signals(self) -> None:
        frame = _bars([1.0] * 4)
        stamps = list(frame["timestamp"])
        records = [
            _record(stamps[0], DecisionSignal.HOLD),
            _record(stamps[1], DecisionSignal.BUY),
            _record(stamps[2], DecisionSignal.HOLD),
            _record(stamps[3], DecisionSignal.SELL),
        ]
        assert source_stance(records) == [0, 1, 1, 0]

    def test_participation_forces_long_and_hands_back_flat(self) -> None:
        """Participate on bars 0-1 while the source stays flat: enter at 0,
        exit when participation ends and the source stance is flat."""
        frame = _bars([1.0] * 4)
        stamps = list(frame["timestamp"])
        records = [_record(ts, DecisionSignal.HOLD) for ts in stamps]
        state = {stamps[0]: True, stamps[1]: True, stamps[2]: False, stamps[3]: False}
        overlay = participation_overlay(records, state, architecture="X")
        signals = [record.signal for record in overlay]
        assert signals == [
            DecisionSignal.BUY,
            DecisionSignal.HOLD,
            DecisionSignal.SELL,
            DecisionSignal.HOLD,
        ]

    def test_hand_back_to_long_source_keeps_position(self) -> None:
        """If the source is long when participation ends, no churn is emitted."""
        frame = _bars([1.0] * 5)
        stamps = list(frame["timestamp"])
        records = [
            _record(stamps[0], DecisionSignal.BUY),
            _record(stamps[1], DecisionSignal.HOLD),
            _record(stamps[2], DecisionSignal.HOLD),
            _record(stamps[3], DecisionSignal.HOLD),
            _record(stamps[4], DecisionSignal.SELL),
        ]
        state = dict.fromkeys(stamps, False)
        state[stamps[1]] = True
        state[stamps[2]] = True
        overlay = participation_overlay(records, state, architecture="X")
        signals = [record.signal for record in overlay]
        assert signals == [
            DecisionSignal.BUY,
            DecisionSignal.HOLD,
            DecisionSignal.HOLD,
            DecisionSignal.HOLD,
            DecisionSignal.SELL,
        ]

    def test_source_sell_during_participation_defers_exit(self) -> None:
        """A source SELL while participating exits only when participation ends."""
        frame = _bars([1.0] * 4)
        stamps = list(frame["timestamp"])
        records = [
            _record(stamps[0], DecisionSignal.BUY),
            _record(stamps[1], DecisionSignal.SELL),
            _record(stamps[2], DecisionSignal.HOLD),
            _record(stamps[3], DecisionSignal.HOLD),
        ]
        state = {stamps[0]: True, stamps[1]: True, stamps[2]: False, stamps[3]: False}
        overlay = participation_overlay(records, state, architecture="X")
        signals = [record.signal for record in overlay]
        assert signals == [
            DecisionSignal.BUY,
            DecisionSignal.HOLD,
            DecisionSignal.SELL,
            DecisionSignal.HOLD,
        ]

    def test_missing_state_is_an_error(self) -> None:
        frame = _bars([1.0])
        records = [_record(frame["timestamp"].iloc[0], DecisionSignal.HOLD)]
        with pytest.raises(OverlayError):
            participation_overlay(records, {}, architecture="X")
