"""Metrics: hand-calculable cases, and the `None` discipline.

Two things are being pinned here. First, that each formula is the one it claims
to be - checked against values worked out by hand rather than against the
implementation's own output. Second, that an undefined metric is `None` and
never `0.0`, because a zero that means "undefined" survives into a leaderboard
and gets a parameter set selected on the strength of it.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from autotrader.research.engines import EmaCrossEngine
from autotrader.research.metrics import (
    CRYPTO_15M,
    CRYPTO_15M_BARS_PER_YEAR,
    EQUITY_15M,
    EQUITY_15M_BARS_PER_YEAR,
    BarClock,
    MetricsInputError,
    annualized_return,
    annualized_volatility,
    bar_clock_for,
    bar_returns,
    compute_metrics,
    max_drawdown,
    metrics_for_replay,
    sharpe_ratio,
    sortino_ratio,
    trade_statistics,
)
from autotrader.research.replay import ReplayConfig, replay
from autotrader.research.trades import Trade
from research_fixtures import flat, multi_cycle, rally, rally_then_selloff

D = Decimal


def curve(*values: str) -> tuple[Decimal, ...]:
    return tuple(Decimal(value) for value in values)


def trade(net: str, *, quantity: str = "1", entry: str = "100", bars: int = 10) -> Trade:
    """A trade whose net PnL is exactly `net`, for statistics tests.

    Built by solving for the exit price rather than by asserting one, so the
    fixture cannot drift away from the property the test relies on.
    """
    entry_price = Decimal(entry)
    size = Decimal(quantity)
    exit_price = entry_price + Decimal(net) / size
    import pandas as pd

    base = pd.Timestamp("2025-01-01", tz="UTC")
    return Trade(
        symbol="BTC/USD",
        entry_timestamp=base,
        exit_timestamp=base + pd.Timedelta(minutes=15 * bars),
        entry_bar_index=0,
        exit_bar_index=bars,
        quantity=size,
        entry_price=entry_price,
        exit_price=exit_price,
        entry_fee=D(0),
        exit_fee=D(0),
        slippage_cost=D(0),
        entry_reason="E",
        exit_reason="X",
    )


# --------------------------------------------------------------------------
# Returns and drawdown
# --------------------------------------------------------------------------


def test_bar_returns_pairs_each_bar_with_its_predecessor() -> None:
    assert bar_returns(curve("100", "110", "99")) == pytest.approx((0.1, -0.1))


def test_a_single_point_curve_has_no_returns() -> None:
    assert bar_returns(curve("100")) == ()


def test_a_wiped_out_account_contributes_zero_rather_than_dividing_by_zero() -> None:
    assert bar_returns(curve("100", "0", "0")) == pytest.approx((-1.0, 0.0))


def test_max_drawdown_is_the_worst_peak_to_trough_decline() -> None:
    drawdown, _ = max_drawdown(curve("100", "120", "90", "150"))
    assert drawdown == pytest.approx(-0.25), "120 -> 90 is a 25% decline"


def test_a_curve_that_never_declines_has_no_drawdown() -> None:
    drawdown, bars = max_drawdown(curve("100", "110", "120"))
    assert drawdown == 0.0
    assert bars == 0


def test_max_drawdown_is_never_positive() -> None:
    for values in (("100", "150"), ("100", "50"), ("100", "100")):
        assert max_drawdown(curve(*values))[0] <= 0


def test_drawdown_duration_counts_bars_spent_below_the_peak() -> None:
    _, bars = max_drawdown(curve("100", "90", "80", "70", "200"))
    assert bars == 3


def test_an_unrecovered_drawdown_is_counted_to_the_final_bar() -> None:
    _, bars = max_drawdown(curve("100", "90", "80"))
    assert bars == 2


def test_no_future_bar_influences_an_earlier_drawdown() -> None:
    """A drawdown at bar t consults only bars up to t - the same causality rule
    the rest of the package enforces, applied to a metric."""
    prefix = curve("100", "120", "90")
    extended = prefix + curve("1000")
    assert max_drawdown(prefix)[0] == max_drawdown(extended)[0]


# --------------------------------------------------------------------------
# Risk-adjusted return
# --------------------------------------------------------------------------


def test_sharpe_is_mean_over_deviation_scaled_by_the_bar_clock() -> None:
    returns = (0.01, -0.005, 0.02, 0.0, 0.015)
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    expected = mean / math.sqrt(variance) * math.sqrt(CRYPTO_15M_BARS_PER_YEAR)
    assert sharpe_ratio(returns, CRYPTO_15M_BARS_PER_YEAR) == pytest.approx(expected)


def test_sharpe_is_none_on_a_flat_curve_rather_than_zero() -> None:
    """CRITICAL. Zero would rank a strategy that never traded above every
    losing one."""
    assert sharpe_ratio((0.0, 0.0, 0.0), CRYPTO_15M_BARS_PER_YEAR) is None


def test_sharpe_is_none_with_fewer_than_two_returns() -> None:
    assert sharpe_ratio((0.01,), CRYPTO_15M_BARS_PER_YEAR) is None
    assert sharpe_ratio((), CRYPTO_15M_BARS_PER_YEAR) is None


def test_a_risk_free_rate_reduces_sharpe() -> None:
    returns = (0.01, 0.012, 0.008, 0.011)
    without = sharpe_ratio(returns, CRYPTO_15M_BARS_PER_YEAR)
    with_rate = sharpe_ratio(returns, CRYPTO_15M_BARS_PER_YEAR, risk_free_rate=0.05)
    assert with_rate is not None and without is not None
    assert with_rate < without


def test_sortino_penalizes_only_downside() -> None:
    """With the same mean, the series whose variation is all upside must score
    at least as well."""
    downside_heavy = (0.02, -0.02, 0.02, -0.02)
    upside_only = (0.0, 0.0, 0.0, 0.0001)
    assert sortino_ratio(upside_only, CRYPTO_15M_BARS_PER_YEAR) is None, "no downside"
    assert sortino_ratio(downside_heavy, CRYPTO_15M_BARS_PER_YEAR) is not None


def test_sortino_is_none_when_nothing_was_lost() -> None:
    assert sortino_ratio((0.01, 0.02, 0.03), CRYPTO_15M_BARS_PER_YEAR) is None


def test_volatility_scales_with_the_square_root_of_the_bar_clock() -> None:
    returns = (0.01, -0.01, 0.02, -0.02)
    crypto = annualized_volatility(returns, CRYPTO_15M_BARS_PER_YEAR)
    equity = annualized_volatility(returns, EQUITY_15M_BARS_PER_YEAR)
    assert crypto is not None and equity is not None
    assert crypto / equity == pytest.approx(
        math.sqrt(CRYPTO_15M_BARS_PER_YEAR / EQUITY_15M_BARS_PER_YEAR)
    )


# --------------------------------------------------------------------------
# Annualization
# --------------------------------------------------------------------------


def test_annualizing_a_full_year_returns_the_total_return() -> None:
    assert annualized_return(0.25, CRYPTO_15M_BARS_PER_YEAR, CRYPTO_15M_BARS_PER_YEAR) == (
        pytest.approx(0.25)
    )


def test_annualizing_a_half_year_compounds_it() -> None:
    half = CRYPTO_15M_BARS_PER_YEAR // 2
    result = annualized_return(0.2, half, CRYPTO_15M_BARS_PER_YEAR)
    assert result == pytest.approx(1.2**2 - 1)


def test_annualizing_a_total_loss_is_none_rather_than_a_complex_number() -> None:
    assert annualized_return(-1.0, 100, CRYPTO_15M_BARS_PER_YEAR) is None


def test_annualizing_a_one_bar_sample_is_none() -> None:
    assert annualized_return(0.1, 1, CRYPTO_15M_BARS_PER_YEAR) is None


def test_the_sample_length_is_reported_beside_the_annualized_figure() -> None:
    """CRITICAL for honesty. A short sample annualizes to an extreme number
    that is arithmetically correct and says nothing; the sample length is the
    only thing that lets a reader tell that is what they are looking at."""
    metrics = compute_metrics(
        equity_curve=curve("100", "80"),
        trades=(),
        initial_equity=D("100"),
        bar_clock=CRYPTO_15M,
    )
    assert metrics.sample_years == pytest.approx(2 / CRYPTO_15M_BARS_PER_YEAR)
    assert metrics.sample_years < 0.001, "two bars is not a year"
    assert metrics.annualized_return is not None
    assert metrics.to_json_dict()["sample_years"] == metrics.sample_years


def test_the_two_bar_clocks_differ_because_the_markets_do() -> None:
    """A 15-minute equity bar arrives about five times less often than a
    15-minute crypto bar, because the equity market is shut most of the time.
    Annualizing one with the other's constant overstates everything."""
    assert CRYPTO_15M.bars_per_year == 4 * 24 * 365
    assert EQUITY_15M.bars_per_year == 26 * 252
    assert CRYPTO_15M.bars_per_year > EQUITY_15M.bars_per_year * 5


def test_an_unknown_bar_clock_lists_the_known_ones() -> None:
    with pytest.raises(MetricsInputError, match="Known clocks"):
        bar_clock_for("nonsense")


def test_a_non_positive_bar_clock_is_refused() -> None:
    with pytest.raises(MetricsInputError):
        BarClock("bad", 0)


# --------------------------------------------------------------------------
# Trade statistics
# --------------------------------------------------------------------------


def test_no_trades_yields_none_everywhere_rather_than_zero() -> None:
    statistics = trade_statistics(())
    assert statistics.trade_count == 0
    assert statistics.win_rate is None
    assert statistics.profit_factor is None
    assert statistics.average_trade_pnl is None
    assert statistics.best_trade_pnl is None


def test_win_rate_counts_only_strictly_profitable_trades() -> None:
    """A scratch trade is not a win. Counting break-even as a victory is the
    easiest way to flatter a win rate."""
    statistics = trade_statistics((trade("10"), trade("0"), trade("-5")))
    assert statistics.win_count == 1
    assert statistics.win_rate == pytest.approx(1 / 3)


def test_profit_factor_is_gross_profit_over_gross_loss() -> None:
    statistics = trade_statistics((trade("30"), trade("-10")))
    assert statistics.gross_profit == D("30")
    assert statistics.gross_loss == D("10"), "reported as a positive magnitude"
    assert statistics.profit_factor == pytest.approx(3.0)


def test_profit_factor_is_none_when_nothing_was_lost() -> None:
    """CRITICAL. Dividing by zero would report an infinitely good strategy on
    the strength of a sample that happened to contain no loss."""
    assert trade_statistics((trade("10"), trade("20"))).profit_factor is None


def test_a_high_win_rate_with_a_losing_profit_factor_is_visible() -> None:
    """The whole reason both numbers are reported: nine small wins and one
    large loss is a losing strategy that looks like a 90% win rate."""
    trades = tuple(trade("1") for _ in range(9)) + (trade("-50"),)
    statistics = trade_statistics(trades)
    assert statistics.win_rate == pytest.approx(0.9)
    assert statistics.profit_factor is not None
    assert statistics.profit_factor < 1.0


def test_average_trade_is_the_mean_net_pnl() -> None:
    statistics = trade_statistics((trade("10"), trade("20"), trade("-6")))
    assert statistics.average_trade_pnl == D("8")


def test_best_and_worst_trades_are_reported() -> None:
    statistics = trade_statistics((trade("10"), trade("-30"), trade("25")))
    assert statistics.best_trade_pnl == D("25")
    assert statistics.worst_trade_pnl == D("-30")


# --------------------------------------------------------------------------
# The full report
# --------------------------------------------------------------------------


def test_metrics_over_an_empty_curve_are_refused() -> None:
    with pytest.raises(MetricsInputError, match="empty equity curve"):
        compute_metrics(equity_curve=(), trades=(), initial_equity=D("100"), bar_clock=CRYPTO_15M)


def test_a_non_positive_starting_equity_is_refused() -> None:
    with pytest.raises(MetricsInputError, match="must be positive"):
        compute_metrics(
            equity_curve=curve("100"), trades=(), initial_equity=D("0"), bar_clock=CRYPTO_15M
        )


def test_exposure_is_the_fraction_of_bars_holding_a_position() -> None:
    metrics = compute_metrics(
        equity_curve=curve("100", "100", "100", "100"),
        trades=(),
        initial_equity=D("100"),
        bar_clock=CRYPTO_15M,
        exposure_bars=1,
    )
    assert metrics.exposure == pytest.approx(0.25)


def test_turnover_is_traded_notional_over_starting_equity() -> None:
    metrics = compute_metrics(
        equity_curve=curve("100", "100"),
        trades=(),
        initial_equity=D("100"),
        bar_clock=CRYPTO_15M,
        traded_notional=D("450"),
    )
    assert metrics.turnover == pytest.approx(4.5)


def test_cost_drag_reports_what_trading_consumed() -> None:
    metrics = compute_metrics(
        equity_curve=curve("100", "100"),
        trades=(),
        initial_equity=D("1000"),
        bar_clock=CRYPTO_15M,
        total_fees=D("20"),
        total_slippage_cost=D("5"),
    )
    assert metrics.cost_drag == pytest.approx(0.025)


def test_realized_and_unrealized_are_kept_apart() -> None:
    """An open position's profit depends on where the sample ends, so it is
    never folded into realized PnL."""
    result = replay(rally(), EmaCrossEngine(), ReplayConfig(initial_cash=D("100000")))
    metrics = metrics_for_replay(result, CRYPTO_15M)
    assert metrics.realized_pnl == 0, "a rally never closes its position"
    assert metrics.unrealized_pnl != 0
    assert metrics.trade_count == 0


def test_a_flat_market_produces_a_zero_return_and_no_sharpe() -> None:
    result = replay(flat(), EmaCrossEngine(), ReplayConfig(initial_cash=D("100000")))
    metrics = metrics_for_replay(result, CRYPTO_15M)
    assert metrics.total_return == 0.0
    assert metrics.max_drawdown == 0.0
    assert metrics.sharpe_ratio is None
    assert metrics.win_rate is None
    assert metrics.trade_count == 0


def test_a_full_report_serializes_with_none_preserved() -> None:
    """`None` must survive to JSON as `null`, not become `0.0` on the way."""
    result = replay(flat(), EmaCrossEngine(), ReplayConfig(initial_cash=D("100000")))
    document = metrics_for_replay(result, CRYPTO_15M).to_json_dict()

    assert document["sharpe_ratio"] is None
    assert document["win_rate"] is None
    assert document["profit_factor"] is None
    assert isinstance(document["realized_pnl"], str), "decimals round-trip as strings"


def test_metrics_over_a_real_replay_are_internally_consistent() -> None:
    result = replay(multi_cycle(), EmaCrossEngine(), ReplayConfig(initial_cash=D("100000")))
    metrics = metrics_for_replay(result, CRYPTO_15M)

    assert metrics.bar_count == len(result.equity_curve)
    assert metrics.final_equity == result.equity_curve[-1]
    assert metrics.trade_count == len(result.trades)
    assert metrics.realized_pnl == sum(t.net_pnl for t in result.trades)
    assert metrics.total_fees == result.total_fees
    assert 0.0 <= metrics.exposure <= 1.0
    assert metrics.max_drawdown <= 0.0


def test_the_report_carries_the_bar_clock_it_was_computed_under() -> None:
    result = replay(rally_then_selloff(), EmaCrossEngine(), ReplayConfig(initial_cash=D("100000")))
    assert metrics_for_replay(result, CRYPTO_15M).bar_clock == "crypto-15m"
    assert metrics_for_replay(result, EQUITY_15M).bar_clock == "equity-15m"
