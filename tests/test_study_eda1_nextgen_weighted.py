"""Weighted-replay and selection semantics (ledger §L12). Constructed frames
only — no dataset, no network, no runtime."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from studies.equity_eda1_nextgen.selection import (
    above_sma,
    build_membership,
    rank_symbols,
    rebalance_sessions,
    trailing_return,
)
from studies.equity_eda1_nextgen.weighted_replay import (
    WeightedReplayError,
    replay_weighted,
)
from studies.equity_v1_v5.scoring import COST_MODELS

ZERO_COST = COST_MODELS[0]


def bars(symbol: str, prices: list[float], start_hour: int = 14) -> pd.DataFrame:
    stamps = [
        pd.Timestamp(datetime(2024, 6, 3, start_hour, 30, tzinfo=UTC)) + pd.Timedelta(minutes=15 * i)
        for i in range(len(prices))
    ]
    return pd.DataFrame(
        {
            "timestamp": stamps,
            "open": prices,
            "high": [p * 1.01 for p in prices],
            "low": [p * 0.99 for p in prices],
            "close": prices,
            "volume": [1000.0] * len(prices),
            "symbol": [symbol] * len(prices),
        }
    )


class TestWeightedReplay:
    def test_next_open_fill_and_weight_change_rule(self) -> None:
        frame = bars("AAA", [100.0, 100, 110, 110, 110])
        stamps = [pd.Timestamp(ts) for ts in frame["timestamp"]]
        targets = {"AAA": {stamps[0]: 0.5, stamps[4]: 0.0}}
        result = replay_weighted(
            {"AAA": frame}, targets, ZERO_COST, label="t", initial_cash=Decimal("1000")
        )
        # Bar 0 decides 0.5 → 5 shares; fills at bar 1 open (100).
        # Weight stays 0.5 while price moves to 110: no rebalance (drift kept).
        # Bar 4 decides 0: sell fills next bar — but there is none, so the
        # position is still open at the end and forced liquidation prices it.
        assert result.fill_count == 1
        assert result.equity_curve[-1] == pytest.approx(1050.0)  # 5 sh × 110 + 500 cash
        assert result.forced_liquidation_net == pytest.approx(0.05)

    def test_input_order_invariance(self) -> None:
        fa = bars("AAA", [100.0, 101, 102, 103])
        fb = bars("BBB", [50.0, 51, 52, 53])
        stamps = [pd.Timestamp(ts) for ts in fa["timestamp"]]
        targets_ab = {"AAA": {stamps[0]: 0.4}, "BBB": {stamps[0]: 0.4}}
        targets_ba = {"BBB": {stamps[0]: 0.4}, "AAA": {stamps[0]: 0.4}}
        r1 = replay_weighted(
            {"AAA": fa, "BBB": fb}, targets_ab, ZERO_COST, label="t", initial_cash=Decimal("1000")
        )
        r2 = replay_weighted(
            {"BBB": fb, "AAA": fa}, targets_ba, ZERO_COST, label="t", initial_cash=Decimal("1000")
        )
        assert r1.equity_curve == r2.equity_curve
        assert r1.fill_count == r2.fill_count

    def test_overweight_bar_refused(self) -> None:
        frame = bars("AAA", [100.0, 100])
        stamps = [pd.Timestamp(ts) for ts in frame["timestamp"]]
        with pytest.raises(WeightedReplayError):
            replay_weighted(
                {"AAA": frame},
                {"AAA": {stamps[0]: 1.2}},
                ZERO_COST,
                label="t",
            )

    def test_costs_reduce_equity(self) -> None:
        frame = bars("AAA", [100.0] * 6)
        stamps = [pd.Timestamp(ts) for ts in frame["timestamp"]]
        targets = {"AAA": {stamps[0]: 0.5, stamps[2]: 0.0, stamps[3]: 0.5}}
        priced = COST_MODELS[1]  # equity-marketable, 2 bp/side
        free = replay_weighted(
            {"AAA": frame}, dict(targets), ZERO_COST, label="t", initial_cash=Decimal("100000")
        )
        costly = replay_weighted(
            {"AAA": frame}, dict(targets), priced, label="t", initial_cash=Decimal("100000")
        )
        assert costly.equity_curve[-1] < free.equity_curve[-1]
        assert costly.total_slippage > 0


class TestSelection:
    def _table(self) -> pd.DataFrame:
        sessions = [date(2024, 1, 1) + timedelta(days=i) for i in range(10)]
        return pd.DataFrame(
            {
                "AAA": [100 + i for i in range(10)],
                "BBB": [100 - i for i in range(10)],
            },
            index=pd.Index(sessions, name="session"),
        ).astype(float)

    def test_trailing_return_is_lagged(self) -> None:
        table = self._table()
        rs = trailing_return(table, horizon=3, lag=1)
        # At row i the value uses closes i−1 and i−4 only.
        expected = table["AAA"].iloc[8] / table["AAA"].iloc[5] - 1
        assert rs["AAA"].iloc[9] == pytest.approx(expected)
        # Perturbing the final close changes nothing at row 9's own value...
        perturbed = table.copy()
        perturbed.loc[perturbed.index[9], "AAA"] = 500.0
        rs2 = trailing_return(perturbed, horizon=3, lag=1)
        assert rs2["AAA"].iloc[9] == pytest.approx(expected)

    def test_above_sma_lagged_and_warmup_nan(self) -> None:
        table = self._table()
        ok = above_sma(table, sma_sessions=3, lag=1)
        assert not ok["AAA"].iloc[0]  # no lagged history yet
        assert bool(ok["AAA"].iloc[9]) is True  # rising series above its SMA
        assert bool(ok["BBB"].iloc[9]) is False  # falling series below its SMA

    def test_rank_ties_lexicographic(self) -> None:
        scores = {"ZZZ": 1.0, "AAA": 1.0, "MMM": 2.0}
        assert rank_symbols(scores, ["ZZZ", "AAA", "MMM"]) == ["MMM", "AAA", "ZZZ"]

    def test_rank_drops_nan(self) -> None:
        scores = {"AAA": float("nan"), "BBB": 0.5}
        assert rank_symbols(scores, ["AAA", "BBB"]) == ["BBB"]

    def test_rebalance_schedule_and_membership_carry(self) -> None:
        sessions = [date(2024, 1, 1) + timedelta(days=i) for i in range(45)]
        marks = rebalance_sessions(sessions)
        assert marks == [sessions[0], sessions[21], sessions[42]]
        membership = build_membership(
            sessions, {sessions[0]: ("AAA",), sessions[21]: ("BBB",)}
        )
        assert membership[sessions[5]] == ("AAA",)
        assert membership[sessions[21]] == ("BBB",)
        assert membership[sessions[40]] == ("BBB",)
