"""Pilot harness tests: purged split, arm isolation, determinism, de-overlap."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from studies.crypto_new_alpha import events, pilot
from studies.crypto_new_alpha.frames import BASELINE_FEATURES, StudyFrame
from studies.crypto_new_alpha.new_features import (
    FLOW_FEATURES,
    LIQPROXY_FEATURES,
    NEW_FEATURES,
    OI_FEATURES,
)


class TestArmIsolation:
    def test_baseline_and_new_features_are_disjoint(self):
        assert not set(BASELINE_FEATURES) & set(NEW_FEATURES)

    def test_full_is_exactly_baseline_plus_new(self):
        assert pilot.ARM_FEATURES["full"] == BASELINE_FEATURES + NEW_FEATURES

    def test_each_ablation_arm_adds_exactly_its_families(self):
        expected = {
            "oi_only": OI_FEATURES,
            "flow_only": FLOW_FEATURES,
            "liqproxy_only": LIQPROXY_FEATURES,
            "oi_flow": OI_FEATURES + FLOW_FEATURES,
            "oi_liqproxy": OI_FEATURES + LIQPROXY_FEATURES,
        }
        for arm, families in expected.items():
            assert pilot.ARM_FEATURES[arm] == BASELINE_FEATURES + families

    def test_holdout_window_is_excluded_from_default_runs(self):
        assert pilot.HOLDOUT_WINDOW == "W07"
        assert "W07" not in pilot.DEFAULT_WINDOWS
        assert len(pilot.DEFAULT_WINDOWS) == 16


def _synthetic_study_frame(columns: tuple[str, ...]) -> StudyFrame:
    periods = 30_000
    timestamps = pd.date_range("2024-06-01", periods=periods, freq="15min", tz="UTC")
    rng = np.random.default_rng(7)
    frame = pd.DataFrame({"timestamp": timestamps})
    frame["session_bar_index"] = (
        (timestamps - timestamps.normalize()) // pd.Timedelta("15min")
    ).astype("int64")
    for name in columns:
        frame[name] = rng.normal(size=periods)
    frame["fwd_96"] = rng.normal(scale=0.01, size=periods)
    frame["knowable_96"] = timestamps + pd.Timedelta("15min") + pd.Timedelta("24h")
    frame["valid_96"] = True
    frame["usable_96"] = True
    frame["grid_position"] = np.arange(periods)
    audit = object()
    return StudyFrame(symbol="BTC/USD", era="modern", frame=frame, join_audit=audit, coverage={})


class TestPurgedSplit:
    def test_no_training_row_is_knowable_inside_the_embargo(self, monkeypatch):
        columns = pilot.ARM_FEATURES["full"]
        study = _synthetic_study_frame(columns)
        monkeypatch.setattr(pilot, "study_frames", lambda era: {"BTC/USD": study})

        captured = {}
        original = pilot.fit_standardizer

        def spy(matrix):
            captured.setdefault("rows", matrix.shape[0])
            return original(matrix)

        monkeypatch.setattr(pilot, "fit_standardizer", spy)
        record = pilot.run_cell("full", "BTC/USD", "W01", 96)
        assert record["status"] == "ok"
        window_start = pd.Timestamp("2025-01-01", tz="UTC")
        frame = study.frame
        eligible = frame.loc[frame["knowable_96"] <= window_start - pd.Timedelta("24h")]
        assert record["train_rows"] == len(eligible)
        # The last eligible label resolves at least 24h before the window opens.
        assert eligible["knowable_96"].max() <= window_start - pd.Timedelta("24h")

    def test_cell_is_deterministic(self, monkeypatch):
        columns = pilot.ARM_FEATURES["full"]
        study = _synthetic_study_frame(columns)
        monkeypatch.setattr(pilot, "study_frames", lambda era: {"BTC/USD": study})
        first = pilot.run_cell("full", "BTC/USD", "W01", 96)
        second = pilot.run_cell("full", "BTC/USD", "W01", 96)
        assert first == second

    def test_insufficient_rows_refuses_to_score(self, monkeypatch):
        columns = pilot.ARM_FEATURES["full"]
        study = _synthetic_study_frame(columns)
        study.frame.drop(study.frame.index[500:], inplace=True)
        monkeypatch.setattr(pilot, "study_frames", lambda era: {"BTC/USD": study})
        record = pilot.run_cell("full", "BTC/USD", "W01", 96)
        assert record["status"] == "insufficient-rows"


class TestEventMachinery:
    def test_deoverlap_enforces_minimum_spacing(self):
        positions = np.asarray([0, 10, 50, 96, 97, 200, 290, 296])
        kept = events.deoverlap(positions, 96)
        assert list(kept) == [0, 96, 200, 296]

    def test_trailing_quantile_is_past_only(self):
        rng = np.random.default_rng(3)
        series = pd.Series(rng.normal(size=events.THRESHOLD_MIN * 3))
        thresholds = events._trailing_quantile(series, 0.95)
        probe = events.THRESHOLD_MIN + 100
        poisoned = series.copy()
        poisoned.iloc[probe:] = 1e9
        poisoned_thresholds = events._trailing_quantile(poisoned, 0.95)
        pd.testing.assert_series_equal(
            thresholds.iloc[: probe + 1], poisoned_thresholds.iloc[: probe + 1]
        )
        assert not thresholds.iloc[probe + 1 :].equals(poisoned_thresholds.iloc[probe + 1 :])

    def test_event_stats_shapes(self):
        stats = events._stats(np.asarray([0.01, -0.02, 0.03]))
        assert stats["n"] == 3
        assert stats["pct_positive"] == pytest.approx(2 / 3)
