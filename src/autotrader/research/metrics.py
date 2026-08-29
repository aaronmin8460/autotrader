"""Performance metrics, with the assumptions behind each one written down.

Every number here is derived from an equity curve and a trade list, both
produced by `autotrader.research.replay`. Nothing is estimated, smoothed or
annualized without saying so.

**A metric that cannot be computed is `None`, never zero.** A Sharpe ratio over
a flat equity curve has an undefined denominator; a win rate over zero trades
has an undefined one too; a profit factor with no losing trade divides by zero.
Reporting ``0.0`` for any of them is a lie that survives into a leaderboard and
gets a parameter set selected. So they are `None`, and a caller that ranks by
them has to decide what to do about it explicitly.

**Annualization needs a bar clock, and the clock differs by market.** 15-minute
crypto bars arrive 35,040 times a year; 15-minute equity bars arrive about
6,552 times, because the market is shut for most of the day and all weekend.
Annualizing one with the other's constant is off by a factor of five and
flatters exactly the thing a researcher wants to believe. `BarClock` makes the
choice explicit and refuses to guess.

**No metric here is a recommendation.** Total return and win rate are the two
easiest numbers to overfit and the two least informative in isolation, which is
why risk-adjusted return, drawdown, exposure, turnover and trade count are
computed alongside them and reported together. A strategy with a 90% win rate
and a profit factor below one loses money; both numbers are present so that is
visible rather than discoverable.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from autotrader.research.trades import Trade

_ZERO = Decimal(0)

#: Bars per year for a 24/7 market on 15-minute bars: 4 per hour, 24 hours,
#: 365 days. Crypto never closes, so there is no session count to apply.
CRYPTO_15M_BARS_PER_YEAR = 4 * 24 * 365

#: Bars per year for US regular-hours equities on 15-minute bars: 26 bars in a
#: 6.5-hour session, 252 sessions. Deliberately not 35,040 - an equity book is
#: shut for about four fifths of the calendar, and annualizing its returns as
#: though it traded through the night overstates both return and volatility.
EQUITY_15M_BARS_PER_YEAR = 26 * 252

#: Daily bars, by the same two conventions.
CRYPTO_DAILY_BARS_PER_YEAR = 365
EQUITY_DAILY_BARS_PER_YEAR = 252


class MetricsInputError(Exception):
    """Metrics were requested over something they cannot describe."""


@dataclass(frozen=True)
class BarClock:
    """How many bars of this kind occur in a year.

    A required, named input rather than a default, because the single most
    common way to overstate an annualized figure is to inherit somebody else's
    bar clock without noticing.
    """

    label: str
    bars_per_year: int

    def __post_init__(self) -> None:
        if self.bars_per_year <= 0:
            raise MetricsInputError(f"bars_per_year must be positive, got {self.bars_per_year}.")


CRYPTO_15M = BarClock("crypto-15m", CRYPTO_15M_BARS_PER_YEAR)
EQUITY_15M = BarClock("equity-15m", EQUITY_15M_BARS_PER_YEAR)
CRYPTO_DAILY = BarClock("crypto-1d", CRYPTO_DAILY_BARS_PER_YEAR)
EQUITY_DAILY = BarClock("equity-1d", EQUITY_DAILY_BARS_PER_YEAR)

BAR_CLOCKS: dict[str, BarClock] = {
    clock.label: clock for clock in (CRYPTO_15M, EQUITY_15M, CRYPTO_DAILY, EQUITY_DAILY)
}


def bar_clock_for(label: str) -> BarClock:
    """Look up a named bar clock, listing the alternatives when unknown."""
    try:
        return BAR_CLOCKS[label]
    except KeyError:
        known = ", ".join(sorted(BAR_CLOCKS))
        raise MetricsInputError(f"Unknown bar clock {label!r}. Known clocks: {known}.") from None


@dataclass(frozen=True)
class PerformanceMetrics:
    """One replay's performance, with every undefined figure left as `None`."""

    bar_count: int
    bar_clock: str
    bars_per_year: int
    initial_equity: Decimal
    final_equity: Decimal

    total_return: float
    annualized_return: float | None
    sample_years: float
    realized_pnl: Decimal
    unrealized_pnl: Decimal

    volatility_annualized: float | None
    sharpe_ratio: float | None
    sortino_ratio: float | None
    max_drawdown: float
    max_drawdown_bars: int

    trade_count: int
    win_rate: float | None
    average_trade_pnl: Decimal | None
    average_trade_return: float | None
    average_bars_held: float | None
    profit_factor: float | None
    gross_profit: Decimal
    gross_loss: Decimal
    best_trade_pnl: Decimal | None
    worst_trade_pnl: Decimal | None

    turnover: float
    exposure: float
    total_fees: Decimal
    total_slippage_cost: Decimal
    cost_drag: float

    def to_json_dict(self) -> dict[str, object]:
        """A JSON-safe form. Decimals become strings; `None` stays `None`.

        Decimals are strings rather than floats so a stored metric round-trips
        exactly - a PnL that becomes ``1234.5600000000001`` on the way to disk
        is no longer the number the simulation produced.
        """
        return {
            "bar_count": self.bar_count,
            "bar_clock": self.bar_clock,
            "bars_per_year": self.bars_per_year,
            "initial_equity": str(self.initial_equity),
            "final_equity": str(self.final_equity),
            "total_return": self.total_return,
            "annualized_return": self.annualized_return,
            "sample_years": self.sample_years,
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "volatility_annualized": self.volatility_annualized,
            "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio,
            "max_drawdown": self.max_drawdown,
            "max_drawdown_bars": self.max_drawdown_bars,
            "trade_count": self.trade_count,
            "win_rate": self.win_rate,
            "average_trade_pnl": None
            if self.average_trade_pnl is None
            else str(self.average_trade_pnl),
            "average_trade_return": self.average_trade_return,
            "average_bars_held": self.average_bars_held,
            "profit_factor": self.profit_factor,
            "gross_profit": str(self.gross_profit),
            "gross_loss": str(self.gross_loss),
            "best_trade_pnl": None if self.best_trade_pnl is None else str(self.best_trade_pnl),
            "worst_trade_pnl": None if self.worst_trade_pnl is None else str(self.worst_trade_pnl),
            "turnover": self.turnover,
            "exposure": self.exposure,
            "total_fees": str(self.total_fees),
            "total_slippage_cost": str(self.total_slippage_cost),
            "cost_drag": self.cost_drag,
        }


# --------------------------------------------------------------------------
# Curve statistics
# --------------------------------------------------------------------------


def bar_returns(equity_curve: Sequence[Decimal]) -> tuple[float, ...]:
    """Simple per-bar returns of an equity curve.

    ``r_t = equity_t / equity_{t-1} - 1``, so a curve of *n* points yields
    *n - 1* returns. A non-positive previous equity contributes ``0.0`` rather
    than an undefined ratio: a wiped-out account has no meaningful next return,
    and the alternative is a division that raises in the middle of a sweep.
    """
    returns: list[float] = []
    for previous, current in zip(equity_curve[:-1], equity_curve[1:], strict=True):
        returns.append(float(current / previous - 1) if previous > 0 else 0.0)
    return tuple(returns)


def max_drawdown(equity_curve: Sequence[Decimal]) -> tuple[float, int]:
    """The worst peak-to-trough decline, and how many bars it lasted.

    Returns ``(fraction, bars)`` where the fraction is never positive:
    ``-0.25`` is a 25% decline. Only bars at or before *t* inform the peak at
    *t*, so no future bar can influence a drawdown - the same causality rule
    the rest of this package enforces, applied to a metric.

    The duration is the longest run of bars spent below a previous peak. A
    drawdown that has not recovered by the final bar is counted to that bar,
    which understates it rather than pretending recovery happened.
    """
    if not equity_curve:
        return 0.0, 0
    peak = equity_curve[0]
    worst = _ZERO
    longest = 0
    current_run = 0
    for equity in equity_curve:
        if equity >= peak:
            peak = equity
            current_run = 0
        else:
            current_run += 1
            longest = max(longest, current_run)
        if peak > 0:
            worst = min(worst, equity / peak - 1)
    return float(worst), longest


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _sample_stdev(values: Sequence[float]) -> float | None:
    """Sample standard deviation, or `None` when it is undefined.

    Bessel's correction (``n - 1``) because an equity curve is a sample of the
    strategy's behaviour rather than its population.
    """
    if len(values) < 2:
        return None
    average = _mean(values)
    variance = sum((value - average) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def sharpe_ratio(
    returns: Sequence[float],
    bars_per_year: int,
    *,
    risk_free_rate: float = 0.0,
) -> float | None:
    """Annualized Sharpe ratio of per-bar `returns`, or `None` if undefined.

    ``mean(excess) / stdev(excess) * sqrt(bars_per_year)``. `risk_free_rate` is
    an annual rate, converted to a per-bar rate before subtraction, and
    defaults to zero - which is an assumption, stated here, not a claim that
    cash earns nothing.

    `None` when there are fewer than two returns or the deviation is zero. A
    strategy that never traded has a flat curve and no Sharpe ratio; reporting
    it as ``0.0`` would put it above every losing strategy in a ranking.
    """
    if len(returns) < 2:
        return None
    per_bar_risk_free = risk_free_rate / bars_per_year
    excess = [value - per_bar_risk_free for value in returns]
    deviation = _sample_stdev(excess)
    if deviation is None or deviation == 0.0:
        return None
    return _mean(excess) / deviation * math.sqrt(bars_per_year)


def sortino_ratio(
    returns: Sequence[float],
    bars_per_year: int,
    *,
    risk_free_rate: float = 0.0,
) -> float | None:
    """Like Sharpe, but penalizing only downside deviation.

    `None` when there are fewer than two returns or no downside at all - a
    strategy that never had a losing bar in the sample has an infinite Sortino
    ratio, which is a fact about the sample rather than about the strategy.
    """
    if len(returns) < 2:
        return None
    per_bar_risk_free = risk_free_rate / bars_per_year
    excess = [value - per_bar_risk_free for value in returns]
    downside = [value for value in excess if value < 0]
    if not downside:
        return None
    # Downside deviation is taken about zero, over the full sample length:
    # the question is how much loss there was, not how variable the losses were
    # among themselves.
    squared = sum(value**2 for value in downside) / len(excess)
    deviation = math.sqrt(squared)
    if deviation == 0.0:
        return None
    return _mean(excess) / deviation * math.sqrt(bars_per_year)


def annualized_volatility(returns: Sequence[float], bars_per_year: int) -> float | None:
    """Per-bar standard deviation scaled by the square root of the bar clock."""
    deviation = _sample_stdev(returns)
    return None if deviation is None else deviation * math.sqrt(bars_per_year)


def annualized_return(
    total_return: float,
    bar_count: int,
    bars_per_year: int,
) -> float | None:
    """Geometric annualization of `total_return` over the sample's length.

    ``(1 + total) ** (bars_per_year / bars) - 1``. `None` when the sample holds
    fewer than two bars, and `None` when the account was wiped out - ``-100%``
    compounded to any power is still a total loss, and raising a negative base
    to a fractional power is not a number.

    Annualizing a short sample is arithmetically valid and epistemically
    dangerous: a week of gains extrapolated to a year is not a forecast. The
    figure is computed because it is asked for, and `sample_years` is reported
    beside it so a reader can see how much extrapolation went into it.
    """
    if bar_count < 2:
        return None
    growth = 1.0 + total_return
    if growth <= 0:
        return None
    years = bar_count / bars_per_year
    if years <= 0:
        return None
    return growth ** (1.0 / years) - 1.0


# --------------------------------------------------------------------------
# Trade statistics
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeStatistics:
    """The round-trip half of a performance report."""

    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float | None
    gross_profit: Decimal
    gross_loss: Decimal
    profit_factor: float | None
    average_trade_pnl: Decimal | None
    average_trade_return: float | None
    average_bars_held: float | None
    best_trade_pnl: Decimal | None
    worst_trade_pnl: Decimal | None


def trade_statistics(trades: Sequence[Trade]) -> TradeStatistics:
    """Summarize closed round trips. An empty list yields `None` everywhere.

    `gross_loss` is reported as a positive magnitude, so profit factor is
    ``gross_profit / gross_loss`` without a sign flip hidden in it. Profit
    factor is `None` when nothing was lost: dividing by zero would report an
    infinitely good strategy on the strength of a sample that happened not to
    contain a loss.
    """
    if not trades:
        return TradeStatistics(
            trade_count=0,
            win_count=0,
            loss_count=0,
            win_rate=None,
            gross_profit=_ZERO,
            gross_loss=_ZERO,
            profit_factor=None,
            average_trade_pnl=None,
            average_trade_return=None,
            average_bars_held=None,
            best_trade_pnl=None,
            worst_trade_pnl=None,
        )

    pnls = [trade.net_pnl for trade in trades]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    gross_profit = sum(wins, _ZERO)
    gross_loss = -sum(losses, _ZERO)

    return TradeStatistics(
        trade_count=len(trades),
        win_count=len(wins),
        loss_count=len(losses),
        win_rate=len(wins) / len(trades),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=float(gross_profit / gross_loss) if gross_loss > 0 else None,
        average_trade_pnl=sum(pnls, _ZERO) / len(pnls),
        average_trade_return=float(
            sum((trade.return_fraction for trade in trades), _ZERO) / len(trades)
        ),
        average_bars_held=sum(trade.bars_held for trade in trades) / len(trades),
        best_trade_pnl=max(pnls),
        worst_trade_pnl=min(pnls),
    )


# --------------------------------------------------------------------------
# The full report
# --------------------------------------------------------------------------


def compute_metrics(
    *,
    equity_curve: Sequence[Decimal],
    trades: Sequence[Trade],
    initial_equity: Decimal,
    bar_clock: BarClock,
    traded_notional: Decimal = _ZERO,
    exposure_bars: int = 0,
    total_fees: Decimal = _ZERO,
    total_slippage_cost: Decimal = _ZERO,
    unrealized_pnl: Decimal = _ZERO,
    risk_free_rate: float = 0.0,
) -> PerformanceMetrics:
    """Compute every metric this package reports, from one replay's output.

    `turnover` is total traded notional over starting equity - a multiple, not
    a rate, and deliberately not annualized: annualizing turnover over a short
    sample produces a number that says more about the sample length than about
    the strategy.

    `exposure` is the fraction of bars a position was held at the close. A
    strategy that is flat 95% of the time has an exposure of ``0.05``, and its
    return should be read against that: an unexposed strategy risked nothing
    for most of the sample.

    `cost_drag` is total costs over starting equity - how much of the sample's
    capital went to fees and spread. Reported because a strategy whose gross
    return is positive and whose net return is not has one problem, and it is
    worth being able to name it.
    """
    if not equity_curve:
        raise MetricsInputError("Cannot compute metrics over an empty equity curve.")
    if initial_equity <= 0:
        raise MetricsInputError(f"initial_equity must be positive, got {initial_equity}.")

    final_equity = equity_curve[-1]
    total_return = float(final_equity / initial_equity - 1)
    returns = bar_returns(equity_curve)
    drawdown, drawdown_bars = max_drawdown(equity_curve)
    statistics = trade_statistics(trades)
    bar_count = len(equity_curve)

    realized = sum((trade.net_pnl for trade in trades), _ZERO)
    costs = total_fees + total_slippage_cost

    return PerformanceMetrics(
        bar_count=bar_count,
        bar_clock=bar_clock.label,
        bars_per_year=bar_clock.bars_per_year,
        initial_equity=initial_equity,
        final_equity=final_equity,
        total_return=total_return,
        annualized_return=annualized_return(total_return, bar_count, bar_clock.bars_per_year),
        # How much extrapolation went into the figure above. Annualizing a
        # two-week sample is arithmetically valid and epistemically worthless;
        # this is the number that lets a reader tell the difference.
        sample_years=bar_count / bar_clock.bars_per_year,
        realized_pnl=realized,
        unrealized_pnl=unrealized_pnl,
        volatility_annualized=annualized_volatility(returns, bar_clock.bars_per_year),
        sharpe_ratio=sharpe_ratio(returns, bar_clock.bars_per_year, risk_free_rate=risk_free_rate),
        sortino_ratio=sortino_ratio(
            returns, bar_clock.bars_per_year, risk_free_rate=risk_free_rate
        ),
        max_drawdown=drawdown,
        max_drawdown_bars=drawdown_bars,
        trade_count=statistics.trade_count,
        win_rate=statistics.win_rate,
        average_trade_pnl=statistics.average_trade_pnl,
        average_trade_return=statistics.average_trade_return,
        average_bars_held=statistics.average_bars_held,
        profit_factor=statistics.profit_factor,
        gross_profit=statistics.gross_profit,
        gross_loss=statistics.gross_loss,
        best_trade_pnl=statistics.best_trade_pnl,
        worst_trade_pnl=statistics.worst_trade_pnl,
        turnover=float(traded_notional / initial_equity),
        exposure=exposure_bars / bar_count if bar_count else 0.0,
        total_fees=total_fees,
        total_slippage_cost=total_slippage_cost,
        cost_drag=float(costs / initial_equity),
    )


def metrics_for_replay(result: object, bar_clock: BarClock, **kwargs: object) -> PerformanceMetrics:
    """Compute metrics straight from a `ReplayResult` or a `PortfolioResult`.

    Typed loosely on purpose: both result shapes expose the same handful of
    attributes this needs, and importing `replay` here would make the metrics
    module depend on the simulator it is meant to describe.
    """
    equity_curve = result.equity_curve  # type: ignore[attr-defined]
    initial = getattr(result, "initial_cash", None)
    if initial is None:  # pragma: no cover - both result types define it
        raise MetricsInputError("Result does not report its starting capital.")
    exposure_bars = getattr(result, "exposure_bars", 0)
    if hasattr(result, "total_sleeve_bars"):
        # A portfolio's exposure is measured in sleeve-bars, so it is scaled
        # back onto the aggregate timeline rather than compared against a bar
        # count it does not share.
        sleeve_bars = result.total_sleeve_bars  # type: ignore[attr-defined]
        exposure_bars = round(exposure_bars / sleeve_bars * len(equity_curve)) if sleeve_bars else 0
    return compute_metrics(
        equity_curve=equity_curve,
        trades=result.trades,  # type: ignore[attr-defined]
        initial_equity=initial,
        bar_clock=bar_clock,
        traded_notional=result.traded_notional,  # type: ignore[attr-defined]
        exposure_bars=exposure_bars,
        total_fees=result.total_fees,  # type: ignore[attr-defined]
        total_slippage_cost=result.total_slippage_cost,  # type: ignore[attr-defined]
        unrealized_pnl=result.unrealized_pnl,  # type: ignore[attr-defined]
        **kwargs,  # type: ignore[arg-type]
    )


__all__ = [
    "BAR_CLOCKS",
    "CRYPTO_15M",
    "CRYPTO_15M_BARS_PER_YEAR",
    "CRYPTO_DAILY",
    "EQUITY_15M",
    "EQUITY_15M_BARS_PER_YEAR",
    "EQUITY_DAILY",
    "BarClock",
    "MetricsInputError",
    "PerformanceMetrics",
    "TradeStatistics",
    "annualized_return",
    "annualized_volatility",
    "bar_clock_for",
    "bar_returns",
    "compute_metrics",
    "max_drawdown",
    "metrics_for_replay",
    "sharpe_ratio",
    "sortino_ratio",
    "trade_statistics",
]
