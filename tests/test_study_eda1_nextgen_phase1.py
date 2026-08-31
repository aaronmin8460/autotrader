"""Phase-1 state-machine and overlay semantics (ledger §L12).

Constructed frames only — no dataset, no network, no runtime."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from studies.equity_deep_arch.state import (
    ParticipationSpec,
    StateInputError,
    participation_series,
)
from studies.equity_eda1_nextgen.overlays import freeze_overlay, lite_overlay
from studies.equity_eda1_nextgen.refined_states import (
    DEFENSIVE,
    PULLBACK,
    STRONG,
    FreezeSpec,
    RefinedSpec,
    freeze_state_series,
    refined_participation_series,
    state_flip_count,
)
from studies.equity_v1_v5.adapters import DecisionRecord

from autotrader.decision.contract import DecisionSignal


def closes_frame(values: list[float]) -> pd.DataFrame:
    start = datetime(2024, 1, 1, tzinfo=UTC).date()
    return pd.DataFrame(
        {
            "session": [start + timedelta(days=i) for i in range(len(values))],
            "close": values,
        }
    )


def records_for(signals: list[DecisionSignal]) -> list[DecisionRecord]:
    base = pd.Timestamp("2024-06-03 14:30:00+00:00")
    return [
        DecisionRecord(
            timestamp=base + pd.Timedelta(minutes=15 * i),
            symbol="TEST",
            signal=signal,
            score=0.0,
            confidence=0.5,
            regime="RANGING",
            reasons=("SOURCE",),
        )
        for i, signal in enumerate(signals)
    ]


class TestRefinedReducesToIncumbent:
    def test_identical_on_trend_and_drawdown_path(self) -> None:
        values = [100.0 + i * 0.5 for i in range(220)]
        values += [values[-1] * f for f in (0.97, 0.93, 0.90, 0.95, 1.01, 1.03)]
        frame = closes_frame(values)
        incumbent = participation_series(frame, ParticipationSpec(sma_sessions=200))
        reduced = refined_participation_series(frame, RefinedSpec(sma_sessions=200))
        assert incumbent["participate"].tolist() == reduced["participate"].tolist()


class TestHysteresis:
    def test_state_persists_inside_the_band(self) -> None:
        spec = RefinedSpec(sma_sessions=5, enter_dd=-0.04, exit_dd=-0.06)
        # Peak 120, then a dip to −5% that stays above the SMA: inside the
        # band, so participation entered at the peak persists.
        values = [100.0, 100, 100, 100, 100, 120, 114]
        series = refined_participation_series(closes_frame(values), spec)
        assert bool(series["participate"].iloc[-1]) is True
        # A dip to −6% touches exit_dd (dd leg, close still above SMA): exit.
        deep = [100.0, 100, 100, 100, 100, 120, 112.8, 112.8]
        series_deep = refined_participation_series(closes_frame(deep), spec)
        assert bool(series_deep["participate"].iloc[-1]) is False

    def test_entry_requires_the_stricter_threshold(self) -> None:
        spec = RefinedSpec(sma_sessions=5, enter_dd=-0.04, exit_dd=-0.06)
        # Peak during warm-up: dd −5% at a close above the SMA is not enough to
        # ENTER (needs dd > −4%), though it would have been enough to persist.
        values = [120.0, 100, 100, 100, 100, 100, 114, 114]
        series = refined_participation_series(closes_frame(values), spec)
        assert bool(series["participate"].iloc[-1]) is False
        # dd −3% at a close above the SMA does enter.
        values_calm = [120.0, 100, 100, 100, 100, 100, 116.4, 116.4]
        series_calm = refined_participation_series(closes_frame(values_calm), spec)
        assert bool(series_calm["participate"].iloc[-1]) is True

    def test_band_may_not_invert(self) -> None:
        with pytest.raises(StateInputError):
            RefinedSpec(enter_dd=-0.06, exit_dd=-0.04)


class TestPersistence:
    def test_two_session_confirmation_delays_entry(self) -> None:
        spec_fast = RefinedSpec(sma_sessions=5, k_enter=1)
        spec_slow = RefinedSpec(sma_sessions=5, k_enter=2)
        values = [100.0, 100, 100, 100, 100, 100, 101, 102]
        fast = refined_participation_series(closes_frame(values), spec_fast)
        slow = refined_participation_series(closes_frame(values), spec_slow)
        assert bool(fast["participate"].iloc[-1]) is True
        assert bool(slow["participate"].iloc[-1]) is False
        extended = refined_participation_series(closes_frame(values + [103.0]), spec_slow)
        assert bool(extended["participate"].iloc[-1]) is True

    def test_interrupted_run_resets_the_counter(self) -> None:
        spec = RefinedSpec(sma_sessions=3, k_enter=2)
        # enter-condition true, false, true: never two consecutive — no entry.
        values = [100.0, 100, 100, 101, 90, 101]
        series = refined_participation_series(closes_frame(values), spec)
        assert not series["participate"].any()


class TestNoFutureData:
    def test_future_perturbation_never_changes_earlier_states(self) -> None:
        values = [100.0 + (i % 7) for i in range(40)]
        spec = RefinedSpec(sma_sessions=5, enter_dd=-0.04, exit_dd=-0.06, k_exit=2)
        base = refined_participation_series(closes_frame(values), spec)
        perturbed_values = values[:30] + [v * 1.5 for v in values[30:]]
        perturbed = refined_participation_series(closes_frame(perturbed_values), spec)
        lag = spec.lag_sessions
        assert (
            base["participate"].tolist()[: 30 + lag]
            == perturbed["participate"].tolist()[: 30 + lag]
        )

    def test_freeze_states_are_lagged_too(self) -> None:
        values = [100.0 + (i % 5) for i in range(30)]
        spec = FreezeSpec(sma_sessions=5)
        base = freeze_state_series(closes_frame(values), spec)
        perturbed = freeze_state_series(
            closes_frame(values[:20] + [v * 0.5 for v in values[20:]]), spec
        )
        assert base["state"].tolist()[:21] == perturbed["state"].tolist()[:21]


class TestFreezeSemantics:
    def test_boundaries_and_warmup(self) -> None:
        spec = FreezeSpec(sma_sessions=3)
        values = [60.0, 70, 80, 90, 120, 113, 106, 100]
        series = freeze_state_series(closes_frame(values), spec)
        assert series["state"].iloc[0] == DEFENSIVE  # warm-up
        assert series["state"].iloc[5] == STRONG  # reads 120 @ dd 0, above SMA
        assert series["state"].iloc[6] == PULLBACK  # reads 113: dd −5.8%, above SMA
        assert series["state"].iloc[7] == DEFENSIVE  # reads 106: dd −11.7%

    def test_freeze_overlay_holds_position_through_pullback(self) -> None:
        records = records_for([DecisionSignal.HOLD] * 6)
        states = {
            records[0].timestamp: DEFENSIVE,
            records[1].timestamp: STRONG,
            records[2].timestamp: PULLBACK,
            records[3].timestamp: PULLBACK,
            records[4].timestamp: DEFENSIVE,
            records[5].timestamp: DEFENSIVE,
        }
        overlaid = freeze_overlay(records, states, architecture="T")
        signals = [record.signal for record in overlaid]
        assert signals == [
            DecisionSignal.HOLD,  # defensive, source flat
            DecisionSignal.BUY,  # strong: enter
            DecisionSignal.HOLD,  # pullback: freeze holds the long
            DecisionSignal.HOLD,
            DecisionSignal.SELL,  # defensive with flat source: exit
            DecisionSignal.HOLD,
        ]

    def test_freeze_overlay_stays_flat_in_pullback_when_flat(self) -> None:
        records = records_for([DecisionSignal.HOLD] * 3)
        states = {
            records[0].timestamp: DEFENSIVE,
            records[1].timestamp: PULLBACK,
            records[2].timestamp: PULLBACK,
        }
        overlaid = freeze_overlay(records, states, architecture="T")
        assert all(record.signal is DecisionSignal.HOLD for record in overlaid)

    def test_freeze_defensive_follows_source_stance(self) -> None:
        records = records_for([DecisionSignal.BUY, DecisionSignal.HOLD, DecisionSignal.SELL])
        states = {record.timestamp: DEFENSIVE for record in records}
        overlaid = freeze_overlay(records, states, architecture="T")
        assert [r.signal for r in overlaid] == [
            DecisionSignal.BUY,
            DecisionSignal.HOLD,
            DecisionSignal.SELL,
        ]


class TestLiteOverlay:
    def test_flat_in_defensive_regardless_of_source(self) -> None:
        records = records_for([DecisionSignal.BUY, DecisionSignal.HOLD, DecisionSignal.HOLD])
        participate = {
            records[0].timestamp: False,
            records[1].timestamp: True,
            records[2].timestamp: False,
        }
        overlaid = lite_overlay(records, participate, architecture="T")
        assert [r.signal for r in overlaid] == [
            DecisionSignal.HOLD,
            DecisionSignal.BUY,
            DecisionSignal.SELL,
        ]


class TestFlipCount:
    def test_counts_state_changes(self) -> None:
        frame = pd.DataFrame({"participate": [False, True, True, False, True]})
        assert state_flip_count(frame, "participate") == 3
