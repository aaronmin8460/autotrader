"""Deterministic short-accounting tests (ledger §L5).

No short number in this program's reports may predate a green run of this
file. The tests are the admissibility proof for `replay_signed`: the
reduction identity against the inherited engine, the sign arithmetic, the
cover side, the equity definition, the exposure accounting, the borrow
charge, terminal liquidation, the P&L reconciliation, and determinism.
"""

from __future__ import annotations

from dataclasses import fields
from decimal import Decimal

import pandas as pd
import pytest
from studies.equity_eda1_nextgen.weighted_replay import replay_weighted
from studies.equity_short_sleeve.shorts import (
    BARS_PER_YEAR,
    BORROW_MODERATE,
    BORROW_ZERO,
    BorrowModel,
    ShortReplayError,
    replay_signed,
)

from autotrader.research.costs import EQUITY_COST, ZERO_COST, CostModel

SHARED_FIELDS = (
    "timestamps",
    "equity_curve",
    "initial_cash",
    "final_equity",
    "forced_liquidation_net",
    "fill_count",
    "traded_notional",
    "total_fees",
    "total_slippage",
    "exposure_mean",
    "exposure_bars",
    "max_active_names",
    "mean_active_names",
    "max_symbol_weight_assigned",
    "turnover",
)


def frame(prices: list[tuple[str, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": [pd.Timestamp(stamp) for stamp, _, _ in prices],
            "open": [o for _, o, _ in prices],
            "close": [c for _, _, c in prices],
            "volume": [1000.0] * len(prices),
        }
    )


@pytest.fixture
def two_bar_frames() -> dict[str, pd.DataFrame]:
    """One symbol, four bars, a clean 10 % fall then a 10 % rise."""
    return {
        "AAA": frame(
            [
                ("2024-01-02 14:30", 100.0, 100.0),
                ("2024-01-02 14:45", 100.0, 90.0),
                ("2024-01-02 15:00", 90.0, 99.0),
                ("2024-01-02 15:15", 99.0, 99.0),
            ]
        )
    }


@pytest.fixture
def multi_frames() -> dict[str, pd.DataFrame]:
    stamps = [
        f"2024-01-02 1{4 if i < 2 else 5}:{['30', '45', '00', '15', '30', '45'][i]}"
        for i in range(6)
    ]
    aaa = [100.0, 102.0, 99.0, 97.0, 101.0, 105.0]
    bbb = [50.0, 49.0, 52.0, 55.0, 53.0, 51.0]
    ccc = [20.0, 21.0, 21.5, 20.5, 19.0, 22.0]
    return {
        "AAA": frame([(s, p, p) for s, p in zip(stamps, aaa, strict=True)]),
        "BBB": frame([(s, p, p) for s, p in zip(stamps, bbb, strict=True)]),
        "CCC": frame([(s, p, p) for s, p in zip(stamps, ccc, strict=True)]),
    }


def constant_targets(frames, weights: dict[str, float]):
    return {
        symbol: dict.fromkeys((pd.Timestamp(t) for t in frames[symbol]["timestamp"]), weight)
        for symbol, weight in weights.items()
    }


# --------------------------------------------------------------------------
# §L5.1 reduction identity
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cost", [ZERO_COST, EQUITY_COST])
@pytest.mark.parametrize(
    "weights",
    [
        {"AAA": 0.5, "BBB": 0.3, "CCC": 0.0},
        {"AAA": 0.1, "BBB": 0.1, "CCC": 0.1},
        {"AAA": 0.0, "BBB": 0.0, "CCC": 0.0},
    ],
)
def test_reduction_identity_on_constructed_frames(multi_frames, cost, weights):
    """Long-only, zero borrow: every shared field is float-exact."""
    targets = constant_targets(multi_frames, weights)
    inherited = replay_weighted(multi_frames, targets, cost, label="X")
    signed = replay_signed(multi_frames, targets, cost, label="X", borrow=BORROW_ZERO)
    for name in SHARED_FIELDS:
        assert getattr(signed, name) == getattr(inherited, name), name


def test_reduction_identity_ignores_short_cost_model_when_long_only(multi_frames):
    """A short cost model that is never reached cannot change a long result."""
    targets = constant_targets(multi_frames, {"AAA": 0.4, "BBB": 0.2, "CCC": 0.1})
    punitive = CostModel(label="punitive", fee_rate=Decimal("0.01"), slippage_rate=Decimal("0.01"))
    inherited = replay_weighted(multi_frames, targets, EQUITY_COST, label="X")
    signed = replay_signed(multi_frames, targets, EQUITY_COST, label="X", short_cost_model=punitive)
    assert signed.final_equity == inherited.final_equity
    assert signed.short_fill_count == 0
    assert signed.short_pnl == 0.0


def test_zero_borrow_rate_charges_exactly_nothing(multi_frames):
    targets = constant_targets(multi_frames, {"AAA": -0.1, "BBB": 0.2, "CCC": 0.0})
    result = replay_signed(multi_frames, targets, ZERO_COST, label="X", borrow=BORROW_ZERO)
    assert result.borrow_cost == 0.0


# --------------------------------------------------------------------------
# §L5.2 / §L5.3 / §L5.4 sign arithmetic, cover side, equity definition
# --------------------------------------------------------------------------


def test_short_profits_when_price_falls(two_bar_frames):
    """A short opened before a 10 % fall must gain, and by the right amount."""
    stamps = [pd.Timestamp(t) for t in two_bar_frames["AAA"]["timestamp"]]
    targets = {"AAA": {stamps[0]: -0.10, stamps[1]: -0.10, stamps[2]: 0.0, stamps[3]: 0.0}}
    result = replay_signed(two_bar_frames, targets, ZERO_COST, label="S", borrow=BORROW_ZERO)
    # Bar 0 decides -10 % of $1,000,000 at mark 100 -> -1,000 shares, filled at
    # bar 1's open of 100. Bar 1 closes at 90: +10,000 of profit. Bar 2 rebounds
    # to 99 while the position is still on: -9,000 given back. Bar 3 covers at
    # 99, flat. Every one of those four numbers is pinned, both signs included.
    assert result.equity_curve == pytest.approx(
        (1_000_000.0, 1_010_000.0, 1_001_000.0, 1_001_000.0)
    )
    assert result.short_pnl_series == pytest.approx((0.0, 10_000.0, -9_000.0, 0.0))
    assert result.short_pnl == pytest.approx(1_000.0)
    assert result.long_pnl == pytest.approx(0.0)
    assert result.reconciliation_error == pytest.approx(0.0, abs=1e-9)


def test_short_loses_when_price_rises(two_bar_frames):
    """The same short held into the rebound gives it back — signs both ways."""
    stamps = [pd.Timestamp(t) for t in two_bar_frames["AAA"]["timestamp"]]
    targets = dict.fromkeys(["AAA"])
    targets["AAA"] = dict.fromkeys(stamps, -0.10)
    result = replay_signed(two_bar_frames, targets, ZERO_COST, label="S", borrow=BORROW_ZERO)
    # bar1 close 90 (+10,000), bar2 close 99 (-9,000 from 90 on 1,000 shares).
    assert result.equity_curve[1] == pytest.approx(1_010_000.0)
    assert result.equity_curve[2] == pytest.approx(1_001_000.0)
    assert result.short_pnl < result.equity_curve[1] - 1_000_000.0


def test_cover_is_a_buy_and_pays_adverse_slippage(two_bar_frames):
    """Opening SELLs low, covering BUYs high — friction on both sides."""
    stamps = [pd.Timestamp(t) for t in two_bar_frames["AAA"]["timestamp"]]
    targets = {"AAA": {stamps[0]: -0.10, stamps[1]: 0.0, stamps[2]: 0.0, stamps[3]: 0.0}}
    frictionless = replay_signed(two_bar_frames, targets, ZERO_COST, label="S")
    charged = replay_signed(
        two_bar_frames, targets, ZERO_COST, label="S", short_cost_model=EQUITY_COST
    )
    assert charged.short_fill_count == 2  # open and cover
    assert charged.final_equity < frictionless.final_equity
    assert charged.total_slippage > 0.0


def test_equity_marks_shorts_negative(multi_frames):
    """A short's market value reduces equity; gross and net say so."""
    targets = constant_targets(multi_frames, {"AAA": 0.0, "BBB": 0.0, "CCC": -0.10})
    result = replay_signed(multi_frames, targets, ZERO_COST, label="S")
    assert result.short_gross_mean > 0.0
    assert result.long_gross_mean == 0.0
    assert result.net_exposure_mean < 0.0
    assert result.max_short_names == 1


def test_whole_shares_round_a_short_smaller_never_larger(multi_frames):
    """int() truncation toward zero must shrink a short, not enlarge it.

    The bound is on the share count at the decision, not on realized gross:
    realized short gross drifts ABOVE its target whenever the short moves
    against the book, because share counts are held between weight changes.
    That drift is a real property of the strategy and is reported (it is why
    every candidate carries an average AND a maximum realized short gross),
    so the test pins the assignment, not the drift.
    """
    # A flat-price symbol isolates truncation from mark-to-market drift.
    flat = {"FLT": frame([(f"2024-01-02 14:{m}", 7.0, 7.0) for m in ("30", "45", "00", "15")])}
    stamps = [pd.Timestamp(t) for t in flat["FLT"]["timestamp"]]
    targets = {"FLT": dict.fromkeys(stamps, -0.0333)}
    result = replay_signed(flat, targets, ZERO_COST, label="S")
    assert result.max_short_weight_assigned == pytest.approx(0.0333)
    # 3.33 % of $1,000,000 at 7.00 is 4,757.14 shares; truncation toward zero
    # gives 4,757 -> $33,299 of gross, strictly under the $33,300 target and
    # never over it.
    assert result.short_gross_series[1] == pytest.approx(33_299.0 / 1_000_000.0, rel=1e-9)
    assert max(result.short_gross_series) <= 0.0333


def test_realized_short_gross_drifts_above_target_when_the_short_loses(multi_frames):
    """Recorded as a property, not a defect: between weight changes the share
    count is fixed, so an adverse move raises realized gross above target."""
    targets = constant_targets(multi_frames, {"AAA": 0.0, "BBB": 0.0, "CCC": -0.10})
    result = replay_signed(multi_frames, targets, ZERO_COST, label="S")
    assert result.max_short_weight_assigned == pytest.approx(0.10)
    assert result.short_gross_max > 0.10  # CCC ends higher than it started


# --------------------------------------------------------------------------
# §L5.5 exposure accounting and bounds
# --------------------------------------------------------------------------


def test_gross_and_net_definitions(multi_frames):
    targets = constant_targets(multi_frames, {"AAA": 0.40, "BBB": 0.20, "CCC": -0.10})
    result = replay_signed(multi_frames, targets, ZERO_COST, label="S", max_short_gross=0.15)
    assert result.total_gross_mean == pytest.approx(
        result.long_gross_mean + result.short_gross_mean, abs=1e-12
    )
    assert result.net_exposure_mean == pytest.approx(
        result.long_gross_mean - result.short_gross_mean, abs=1e-12
    )


def test_short_gross_cap_is_enforced(multi_frames):
    targets = constant_targets(multi_frames, {"AAA": -0.10, "BBB": -0.10, "CCC": 0.0})
    with pytest.raises(ShortReplayError, match="short target weights"):
        replay_signed(multi_frames, targets, ZERO_COST, label="S", max_short_gross=0.15)


def test_long_gross_cap_is_enforced(multi_frames):
    targets = constant_targets(multi_frames, {"AAA": 0.6, "BBB": 0.6, "CCC": 0.0})
    with pytest.raises(ShortReplayError, match="long target weights"):
        replay_signed(multi_frames, targets, ZERO_COST, label="S")


def test_weight_outside_unit_interval_is_refused(multi_frames):
    targets = constant_targets(multi_frames, {"AAA": -1.5, "BBB": 0.0, "CCC": 0.0})
    with pytest.raises(ShortReplayError, match="outside"):
        replay_signed(multi_frames, targets, ZERO_COST, label="S", max_short_gross=2.0)


# --------------------------------------------------------------------------
# §L5.6 borrow cost
# --------------------------------------------------------------------------


def test_borrow_is_charged_on_absolute_short_value(multi_frames):
    targets = constant_targets(multi_frames, {"AAA": 0.0, "BBB": 0.0, "CCC": -0.10})
    free = replay_signed(multi_frames, targets, ZERO_COST, label="S", borrow=BORROW_ZERO)
    charged = replay_signed(multi_frames, targets, ZERO_COST, label="S", borrow=BORROW_MODERATE)
    assert charged.borrow_cost > 0.0
    assert charged.final_equity < free.final_equity
    assert charged.final_equity == pytest.approx(free.final_equity - charged.borrow_cost, rel=1e-6)


def test_borrow_scales_with_rate_and_duration(multi_frames):
    targets = constant_targets(multi_frames, {"AAA": 0.0, "BBB": 0.0, "CCC": -0.10})
    low = replay_signed(
        multi_frames, targets, ZERO_COST, label="S", borrow=BorrowModel("a", Decimal("0.01"))
    )
    high = replay_signed(
        multi_frames, targets, ZERO_COST, label="S", borrow=BorrowModel("b", Decimal("0.03"))
    )
    assert high.borrow_cost == pytest.approx(3.0 * low.borrow_cost, rel=1e-9)


def test_borrow_per_bar_uses_the_equity_clock():
    assert BARS_PER_YEAR == 6552
    assert BORROW_MODERATE.per_bar == Decimal("0.010") / Decimal(6552)


def test_a_long_book_alone_is_never_charged_borrow(multi_frames):
    targets = constant_targets(multi_frames, {"AAA": 0.4, "BBB": 0.2, "CCC": 0.0})
    result = replay_signed(multi_frames, targets, ZERO_COST, label="S", borrow=BORROW_MODERATE)
    assert result.borrow_cost == 0.0


# --------------------------------------------------------------------------
# §L5.7 terminal liquidation
# --------------------------------------------------------------------------


def test_terminal_short_is_covered_not_sold(multi_frames):
    """Forced liquidation BUYs a short back and pays the buy-side friction."""
    targets = constant_targets(multi_frames, {"AAA": 0.0, "BBB": 0.0, "CCC": -0.10})
    frictionless = replay_signed(multi_frames, targets, ZERO_COST, label="S")
    charged = replay_signed(
        multi_frames, targets, ZERO_COST, label="S", short_cost_model=EQUITY_COST
    )
    assert charged.forced_liquidation_net < frictionless.forced_liquidation_net
    # A cover at the final mark costs the short book, so forced net is below
    # the marked net rather than above it.
    assert charged.forced_liquidation_net < charged.net_return


# --------------------------------------------------------------------------
# §L5 P&L attribution reconciliation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "weights",
    [
        {"AAA": 0.4, "BBB": 0.2, "CCC": -0.10},
        {"AAA": -0.05, "BBB": -0.05, "CCC": 0.3},
        {"AAA": 0.0, "BBB": 0.0, "CCC": -0.15},
    ],
)
def test_pnl_attribution_reconciles_to_the_equity_change(multi_frames, weights):
    targets = constant_targets(multi_frames, weights)
    result = replay_signed(
        multi_frames,
        targets,
        EQUITY_COST,
        label="S",
        short_cost_model=CostModel(
            label="short", fee_rate=Decimal("0"), slippage_rate=Decimal("0.0004")
        ),
        borrow=BORROW_MODERATE,
        max_short_gross=0.20,
    )
    assert abs(result.reconciliation_error) < 1e-6
    assert result.long_pnl + result.short_pnl == pytest.approx(
        result.final_equity - result.initial_cash, abs=1e-6
    )
    assert sum(result.short_pnl_series) == pytest.approx(result.short_pnl, abs=1e-6)
    assert sum(result.long_pnl_series) == pytest.approx(result.long_pnl, abs=1e-6)


# --------------------------------------------------------------------------
# §L5.9 determinism
# --------------------------------------------------------------------------


def test_symbol_order_invariance(multi_frames):
    weights = {"AAA": 0.4, "BBB": 0.2, "CCC": -0.10}
    targets = constant_targets(multi_frames, weights)
    forward = replay_signed(multi_frames, targets, EQUITY_COST, label="S", max_short_gross=0.15)
    reversed_frames = dict(reversed(list(multi_frames.items())))
    reversed_targets = dict(reversed(list(targets.items())))
    backward = replay_signed(
        reversed_frames, reversed_targets, EQUITY_COST, label="S", max_short_gross=0.15
    )
    for field in fields(forward):
        assert getattr(forward, field.name) == getattr(backward, field.name), field.name


def test_repeated_runs_are_identical(multi_frames):
    targets = constant_targets(multi_frames, {"AAA": 0.3, "BBB": 0.0, "CCC": -0.10})
    first = replay_signed(multi_frames, targets, EQUITY_COST, label="S")
    second = replay_signed(multi_frames, targets, EQUITY_COST, label="S")
    assert first == second
