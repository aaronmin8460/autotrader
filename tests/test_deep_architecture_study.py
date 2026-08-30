"""Causality and ledger invariants for the deep-architecture study code.

These tests are cheap and synthetic-first: the causality properties are
demonstrated by truncation experiments (a causal feature computed over the
first n bars must be identical to the same rows computed over all bars), and
the replay's ledger is reconciled exactly. Dataset-dependent checks skip
cleanly when the external volume is unmounted - a skip says "not checked
here", which is honest; a substituted fixture would say "checked" and lie.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from studies.crypto_deep_architecture.features_ext import compute_extension_features
from studies.crypto_deep_architecture.trend_rules import (
    donchian_states,
    replay,
    tsmom_states,
)

from autotrader.research.costs import cost_model_for


def _synthetic_observations(count: int, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 0.004, count)
    close = 100.0 * np.exp(np.cumsum(steps))
    spread = np.abs(rng.normal(0.0, 0.002, count))
    high = close * (1.0 + spread)
    low = close * (1.0 - spread)
    open_ = np.roll(close, 1) * (1.0 + rng.normal(0.0, 0.0005, count))
    open_[0] = 100.0
    timestamps = pd.date_range("2024-01-01", periods=count, freq="15min", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "symbol": pd.array(["TEST/USD"] * count, dtype="string"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.abs(rng.normal(1.0, 0.5, count)),
            "is_present": True,
            "session_id": pd.array(
                [moment.date().isoformat() for moment in timestamps], dtype="string"
            ),
            "session_bar_index": np.array([i % 96 for i in range(count)], dtype="int64"),
            "session_bar_count": np.full(count, 96, dtype="int64"),
        }
    )


def _base_features(observations: pd.DataFrame) -> pd.DataFrame:
    from autotrader.ml.features import compute_features

    return compute_features(observations, has_session_gaps=False)


class TestExtensionFeatureCausality:
    def test_truncation_leaves_prefix_identical(self) -> None:
        full = _synthetic_observations(2200)
        prefix_length = 1500
        prefix = full.iloc[:prefix_length].reset_index(drop=True)

        features_full = compute_extension_features(
            full, _base_features(full), other_close=full["close"] * 1.01
        )
        features_prefix = compute_extension_features(
            prefix,
            _base_features(prefix),
            other_close=(full["close"] * 1.01).iloc[:prefix_length].reset_index(drop=True),
        )
        head_full = features_full.iloc[:prefix_length].reset_index(drop=True)
        pd.testing.assert_frame_equal(head_full, features_prefix)

    def test_tail_perturbation_changes_nothing_before_it(self) -> None:
        clean = _synthetic_observations(2200)
        poisoned = clean.copy()
        cut = 1800
        poisoned.loc[cut:, ["open", "high", "low", "close"]] *= 3.7

        clean_features = compute_extension_features(
            clean, _base_features(clean), other_close=clean["close"]
        )
        poisoned_features = compute_extension_features(
            poisoned, _base_features(poisoned), other_close=poisoned["close"]
        )
        pd.testing.assert_frame_equal(
            clean_features.iloc[:cut].reset_index(drop=True),
            poisoned_features.iloc[:cut].reset_index(drop=True),
        )
        assert not clean_features.iloc[cut:].equals(poisoned_features.iloc[cut:])


class TestTrendStateCausality:
    def test_tsmom_states_ignore_the_future(self) -> None:
        clean = _synthetic_observations(3000)
        poisoned = clean.copy()
        cut = 2500
        poisoned.loc[cut:, "close"] *= 0.2
        states_clean = tsmom_states(clean["close"], 672, 0.0)
        states_poisoned = tsmom_states(poisoned["close"], 672, 0.0)
        assert np.array_equal(states_clean[:cut], states_poisoned[:cut])

    def test_donchian_states_ignore_the_future(self) -> None:
        clean = _synthetic_observations(3000)
        poisoned = clean.copy()
        cut = 2500
        poisoned.loc[cut:, ["high", "low", "close"]] *= 5.0
        args_clean = (clean["close"], clean["high"], clean["low"], 672)
        args_poisoned = (poisoned["close"], poisoned["high"], poisoned["low"], 672)
        assert np.array_equal(
            donchian_states(*args_clean)[:cut], donchian_states(*args_poisoned)[:cut]
        )


class TestReplayLedger:
    def test_equity_reconciles_to_realized_plus_unrealized(self) -> None:
        observations = _synthetic_observations(3000)
        states = tsmom_states(observations["close"], 96, 0.0)
        cost = cost_model_for("crypto-taker")
        result = replay(observations, states, cost, start=200, end=2999)
        final_equity = 100_000.0 * (1.0 + result.net_return)
        reconstructed = 100_000.0 + result.realized_pnl + result.unrealized_pnl
        assert final_equity == pytest.approx(reconstructed, abs=1e-6)

    def test_forced_liquidation_never_beats_the_mark_with_costs(self) -> None:
        observations = _synthetic_observations(3000)
        states = tsmom_states(observations["close"], 96, 0.0)
        cost = cost_model_for("crypto-taker")
        result = replay(observations, states, cost, start=200, end=2999)
        if result.open_position_at_end:
            assert result.forced_liquidation_return < result.net_return
        else:
            assert result.forced_liquidation_return == pytest.approx(result.net_return)

    def test_frictionless_replay_of_constant_long_matches_price_ratio(self) -> None:
        observations = _synthetic_observations(600)
        states = np.ones(600, dtype="int8")
        cost = cost_model_for("frictionless")
        result = replay(observations, states, cost, start=10, end=599)
        entry_open = float(observations["open"].iloc[11])
        final_close = float(observations["close"].iloc[599])
        assert result.net_return == pytest.approx(final_close / entry_open - 1.0, rel=1e-9)
        assert result.trades == 1
