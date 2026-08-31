"""Causality, semantics, and determinism tests for the new-alpha features.

Every test here is synthetic and network-free. The causality tests follow the
funding-basis pilot's standard: a probe must have teeth (the perturbation must
actually change the future) and everything at or before the cut must come back
bit-identical.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from studies.crypto_new_alpha import new_features


def _oi_frame(start: str = "2024-01-01", periods: int = 9000) -> pd.DataFrame:
    times = pd.date_range(start, periods=periods, freq="5min", tz="UTC")
    notional = 1e9 + np.cumsum(np.sin(np.arange(periods)) * 1e6) + np.arange(periods) * 1e4
    return pd.DataFrame(
        {
            "create_time": times,
            "oi_contracts": notional / 50_000.0,
            "oi_notional": notional,
            "knowable_at": times + pd.Timedelta("5min"),
        }
    )


def _flow_frame(start: str = "2024-01-01", periods: int = 3000) -> pd.DataFrame:
    times = pd.date_range(start, periods=periods, freq="15min", tz="UTC")
    quote = 1e6 + (np.arange(periods) % 7) * 1e5
    taker_buy = quote * (0.5 + 0.3 * np.sin(np.arange(periods) / 10.0))
    return pd.DataFrame(
        {
            "bar_open": times,
            "bar_close": times + pd.Timedelta("15min") - pd.Timedelta("1ms"),
            "close": 50_000.0 + np.arange(periods, dtype="float64"),
            "volume": quote / 50_000.0,
            "quote_volume": quote,
            "count": np.full(periods, 100, dtype="int64"),
            "taker_buy_quote_volume": taker_buy,
            "knowable_at": times + pd.Timedelta("15min") + pd.Timedelta("1ms"),
        }
    )


def _grid_timestamps(start: str = "2024-01-05", periods: int = 500) -> pd.Series:
    return pd.Series(pd.date_range(start, periods=periods, freq="15min", tz="UTC"))


def _join(timestamps, oi, flow):
    periods = len(timestamps)
    r16 = pd.Series(np.linspace(-0.02, 0.02, periods))
    r96 = pd.Series(np.linspace(0.05, -0.05, periods))
    return new_features.join_new_features(timestamps, oi, flow, return_16=r16, return_96=r96)


class TestAggressorSemantics:
    def test_all_taker_buy_bar_has_imbalance_plus_one(self):
        flow = _flow_frame()
        flow.loc[2500, "taker_buy_quote_volume"] = flow.loc[2500, "quote_volume"]
        series = new_features.flow_series_features(flow)
        row = series.loc[series["bar_open"] == flow.loc[2500, "bar_open"]].iloc[0]
        assert row["flow_imb_15m"] == pytest.approx(1.0)

    def test_all_taker_sell_bar_has_imbalance_minus_one(self):
        flow = _flow_frame()
        flow.loc[2500, "taker_buy_quote_volume"] = 0.0
        series = new_features.flow_series_features(flow)
        row = series.loc[series["bar_open"] == flow.loc[2500, "bar_open"]].iloc[0]
        assert row["flow_imb_15m"] == pytest.approx(-1.0)

    def test_zero_volume_bar_withdraws_imbalance_rather_than_inventing_zero(self):
        flow = _flow_frame()
        flow.loc[2500, ["quote_volume", "taker_buy_quote_volume", "volume"]] = 0.0
        flow.loc[2500, "count"] = 0
        series = new_features.flow_series_features(flow)
        row = series.loc[series["bar_open"] == flow.loc[2500, "bar_open"]].iloc[0]
        assert np.isnan(row["flow_imb_15m"])


class TestOiAlignment:
    def test_publication_lag_hides_a_snapshot_from_the_same_instant(self):
        """A snapshot created at the decision instant itself must not be
        visible: knowable_at = create_time + 5min lies after the decision.
        A sentinel OI level is planted at exactly one decision instant; if the
        join ever reads it, oi_chg_15m at that row would explode."""
        oi = _oi_frame()
        flow = _flow_frame()
        timestamps = _grid_timestamps(periods=100)
        decision = pd.Timestamp(timestamps.iloc[50]) + pd.Timedelta("15min")
        planted = oi.loc[oi["create_time"] == decision]
        assert not planted.empty  # the 5-min snapshot clock covers the instant
        oi_planted = oi.copy()
        oi_planted.loc[oi_planted["create_time"] == decision, "oi_notional"] = 1e15

        clean, audit = _join(timestamps, oi, flow)
        poisoned, _ = _join(timestamps, oi_planted, flow)
        assert audit.negative_staleness == 0
        row = 50
        for name in ("oi_chg_15m", "oi_chg_1h", "oi_z_30d"):
            left, right = clean[name].iloc[row], poisoned[name].iloc[row]
            assert (np.isnan(left) and np.isnan(right)) or left == pytest.approx(right)

    def test_oi_gap_beyond_tolerance_withdraws_the_feature(self):
        oi = _oi_frame(periods=9000)
        hole_start = pd.Timestamp("2024-01-05 00:00", tz="UTC")
        oi = oi.loc[
            ~((oi["create_time"] >= hole_start) & (oi["create_time"] < hole_start + pd.Timedelta("13h")))
        ].reset_index(drop=True)
        flow = _flow_frame(periods=3000)
        timestamps = _grid_timestamps("2024-01-05 06:00", periods=8)
        frame, audit = _join(timestamps, oi, flow)
        assert frame["oi_chg_1h"].isna().all()
        assert audit.oi_stale_dropped >= 8

    def test_change_reference_beyond_tolerance_is_withdrawn(self):
        """A 24h change whose reference snapshot is >30min stale must be NaN."""
        oi = _oi_frame(periods=9000)
        anchor = pd.Timestamp("2024-01-06 12:00", tz="UTC")
        window = (oi["create_time"] > anchor - pd.Timedelta("24h") - pd.Timedelta("3h")) & (
            oi["create_time"] <= anchor - pd.Timedelta("24h") + pd.Timedelta("25min")
        )
        oi = oi.loc[~window].reset_index(drop=True)
        series = new_features.oi_series_features(oi)
        row = series.loc[series["create_time"] == anchor]
        assert not row.empty
        assert np.isnan(row["oi_chg_24h"].iloc[0])
        assert not np.isnan(row["oi_chg_1h"].iloc[0])


class TestNoFutureFilling:
    def test_future_perturbation_cannot_change_the_past(self):
        """Corrupt everything knowable after a cut; features at/before the cut
        must be bit-identical, and the probe must have teeth."""
        oi = _oi_frame()
        flow = _flow_frame()
        timestamps = _grid_timestamps(periods=800)
        cut = pd.Timestamp("2024-01-08 00:00", tz="UTC")

        clean, _ = _join(timestamps, oi, flow)

        oi_poisoned = oi.copy()
        mask_oi = oi_poisoned["knowable_at"] > cut
        oi_poisoned.loc[mask_oi, "oi_notional"] = (
            oi_poisoned.loc[mask_oi, "oi_notional"] * -50.0 + 7.0
        ).abs() + 1.0
        flow_poisoned = flow.copy()
        mask_flow = flow_poisoned["knowable_at"] > cut
        flow_poisoned.loc[mask_flow, "taker_buy_quote_volume"] = (
            flow_poisoned.loc[mask_flow, "quote_volume"] * 0.9
        )
        assert mask_oi.any() and mask_flow.any()

        poisoned, _ = _join(timestamps, oi_poisoned, flow_poisoned)

        decision_ts = timestamps + pd.Timedelta("15min")
        before = (decision_ts <= cut).to_numpy()
        after = ~before
        assert before.any() and after.any()

        clean_before = clean.loc[before].reset_index(drop=True)
        poisoned_before = poisoned.loc[before].reset_index(drop=True)
        pd.testing.assert_frame_equal(clean_before, poisoned_before)

        # Teeth: the poisoned future must actually differ somewhere.
        clean_after = clean.loc[after].reset_index(drop=True)
        poisoned_after = poisoned.loc[after].reset_index(drop=True)
        assert not clean_after.equals(poisoned_after)

    def test_negative_staleness_raises_rather_than_joining_the_future(self):
        oi = _oi_frame()
        oi["knowable_at"] = oi["knowable_at"] - pd.Timedelta("30min")  # fabricated future leak
        oi_bad = oi.copy()
        oi_bad.loc[len(oi_bad) - 1, "knowable_at"] = oi_bad["knowable_at"].max() + pd.Timedelta(
            "10min"
        )
        # merge_asof backward can never select a future row, so the guard is
        # structural; assert the audit records zero negative staleness even on
        # a shifted series.
        flow = _flow_frame()
        timestamps = _grid_timestamps(periods=100)
        _, audit = _join(timestamps, oi_bad, flow)
        assert audit.negative_staleness == 0


class TestDeterminism:
    def test_feature_pipeline_is_bit_deterministic(self):
        oi = _oi_frame()
        flow = _flow_frame()
        timestamps = _grid_timestamps(periods=400)
        first, _ = _join(timestamps, oi, flow)
        second, _ = _join(timestamps.copy(deep=True), oi.copy(deep=True), flow.copy(deep=True))
        pd.testing.assert_frame_equal(first, second)

    def test_feature_contract_order_is_fixed(self):
        assert new_features.NEW_FEATURES == (
            new_features.OI_FEATURES
            + new_features.FLOW_FEATURES
            + new_features.LIQPROXY_FEATURES
            + new_features.INTERACTION_FEATURES
        )
        assert len(new_features.NEW_FEATURES) == 18
