"""Walk-forward evaluation: one out-of-sample record per window, never pooled.

`autotrader.research.splits` decides *where* the windows are;
`autotrader.research.replay` decides what happens *inside* one. This module is
the loop between them, and the discipline it enforces is that a window's result
is computed on that window's bars and nothing else.

**Each window is replayed from flat, with its own starting capital.** A window
does not inherit the position or the cash the previous one ended with. That
makes the windows independent - which is what lets them be compared, averaged,
and reported with a spread - and it means a single lucky early window cannot
compound its way through the whole study and be reported as consistency.

**Warm-up is carried, not skipped.** An engine with a 50-bar warm-up produces
nothing for the first 50 bars of any window, so a window scored from its first
bar is scored partly over a period the engine was blind in. `warmup_bars` from
the engine is prepended to each test window from the bars immediately before it
- data that is strictly in the past of the window, and which the engine would
have had in real time. Those bars are used for indicator state only: no fill is
recorded during them, and the equity curve the metrics see begins at the
window's own first bar. This is the one place where a window reads outside
itself, it reads only backwards, and it is the difference between evaluating a
strategy and evaluating its warm-up ramp.

**The aggregate is a distribution, not a number.** `WalkForwardResult` reports
per-window metrics and the median and spread across them. A mean Sharpe ratio
over eight windows where seven are negative is not a strategy; the spread is
what makes that visible.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from autotrader.research.engines import DecisionEngine, describe
from autotrader.research.metrics import BarClock, PerformanceMetrics, compute_metrics
from autotrader.research.replay import ReplayConfig, ReplayResult, replay
from autotrader.research.splits import TimeSplit


class WalkForwardError(Exception):
    """A walk-forward evaluation could not be run as configured."""


@dataclass(frozen=True)
class WindowResult:
    """One test window's out-of-sample outcome.

    `warmup_bars_used` records how many prior bars were prepended for indicator
    state. It is reported rather than assumed so a reader can tell a window
    that got its full warm-up from one near the start of the dataset that could
    not.
    """

    split: TimeSplit
    metrics: PerformanceMetrics
    result: ReplayResult
    warmup_bars_used: int

    def to_json_dict(self) -> dict[str, object]:
        return {
            "split": self.split.to_json_dict(),
            "warmup_bars_used": self.warmup_bars_used,
            "metrics": self.metrics.to_json_dict(),
        }


@dataclass(frozen=True)
class WalkForwardResult:
    """Every window's result, plus the distribution across them."""

    engine: dict[str, object]
    windows: tuple[WindowResult, ...]
    bar_clock: str

    @property
    def window_count(self) -> int:
        return len(self.windows)

    @property
    def total_trades(self) -> int:
        return sum(window.metrics.trade_count for window in self.windows)

    def values(self, metric: str) -> tuple[float, ...]:
        """Every window's value for `metric`, skipping windows where it is `None`.

        Skipping rather than substituting zero: a window with no trades has no
        win rate, and counting it as 0% would drag a summary toward a number no
        window actually produced.
        """
        collected: list[float] = []
        for window in self.windows:
            value = getattr(window.metrics, metric)
            if value is not None:
                collected.append(float(value))
        return tuple(collected)

    def summary(self, metric: str) -> dict[str, float | int | None]:
        """Median, mean, spread and window count for one metric.

        Median first because a walk-forward distribution over a handful of
        windows is routinely dominated by one outlier, and a mean over eight
        numbers is not robust to it. Both are reported; which to believe is the
        reader's call, and having only the mean takes that call away.
        """
        values = self.values(metric)
        if not values:
            return {
                "windows": 0,
                "median": None,
                "mean": None,
                "stdev": None,
                "minimum": None,
                "maximum": None,
            }
        return {
            "windows": len(values),
            "median": statistics.median(values),
            "mean": statistics.fmean(values),
            "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
            "minimum": min(values),
            "maximum": max(values),
        }

    def positive_window_fraction(self, metric: str = "total_return") -> float | None:
        """What fraction of windows were positive on `metric`.

        The most honest single summary of a walk-forward study: a strategy that
        is profitable in three windows of nine is not a strategy that works
        one third of the time, it is a strategy with no demonstrated edge.
        """
        values = self.values(metric)
        if not values:
            return None
        return sum(1 for value in values if value > 0) / len(values)

    def to_json_dict(self) -> dict[str, object]:
        return {
            "engine": self.engine,
            "bar_clock": self.bar_clock,
            "window_count": self.window_count,
            "total_trades": self.total_trades,
            "windows": [window.to_json_dict() for window in self.windows],
            "summary": {
                metric: self.summary(metric)
                for metric in (
                    "total_return",
                    "sharpe_ratio",
                    "max_drawdown",
                    "profit_factor",
                    "win_rate",
                    "exposure",
                    "turnover",
                )
            },
            "positive_return_window_fraction": self.positive_window_fraction(),
        }


def _window_frame(
    bars: pd.DataFrame,
    split: TimeSplit,
    warmup_bars: int,
) -> tuple[pd.DataFrame, int]:
    """The test window's bars, preceded by whatever warm-up is available.

    Only bars strictly before the window are prepended, and never more than
    exist. Returns the frame and how many warm-up bars it actually got.
    """
    available = min(warmup_bars, split.test_start)
    start = split.test_start - available
    return bars.iloc[start : split.test_end].copy(), available


def run_walk_forward(
    bars: pd.DataFrame,
    engine: DecisionEngine,
    splits: Sequence[TimeSplit],
    *,
    bar_clock: BarClock,
    config: ReplayConfig | None = None,
    risk_free_rate: float = 0.0,
) -> WalkForwardResult:
    """Replay `engine` over each test window independently.

    Every window starts flat with `config.initial_cash`, so windows are
    comparable to one another and no window compounds into the next.

    Metrics are computed over the window's **own** bars: the warm-up prefix
    contributes indicator state and is then excluded from the equity curve, the
    trade list and every metric, so a window is never scored over bars that
    belong to the window before it.
    """
    if not splits:
        raise WalkForwardError("Walk-forward evaluation needs at least one split.")
    settings = ReplayConfig() if config is None else config
    warmup = engine.warmup_bars

    windows: list[WindowResult] = []
    for split in splits:
        frame, used = _window_frame(bars, split, warmup)
        result = replay(frame, engine, settings)

        # Drop the warm-up prefix from everything the metrics see. The prefix
        # exists to prime indicators, not to be traded or scored.
        scored_curve = result.equity_curve[used:]
        if not scored_curve:
            raise WalkForwardError(
                f"Split {split.index} has no bars left after its {used}-bar warm-up prefix."
            )
        scored_trades = tuple(trade for trade in result.trades if trade.exit_bar_index >= used)
        scored_fills = tuple(fill for fill in result.fills if fill.bar_index >= used)
        scored_notional = sum((fill.notional for fill in scored_fills), Decimal(0))
        scored_fees = sum((fill.fee for fill in scored_fills), Decimal(0))
        scored_slippage = sum((fill.slippage_cost for fill in scored_fills), Decimal(0))
        # Equity at the window's first scored bar is the baseline the window's
        # return is measured against - not the starting cash, which was the
        # baseline for a warm-up period the window is not credited with.
        opening_equity = result.equity_curve[used - 1] if used > 0 else settings.initial_cash
        exposure = sum(
            1
            for index in range(used, result.bar_count)
            if index < len(result.equity_curve) and _held_at(result, index)
        )

        metrics = compute_metrics(
            equity_curve=scored_curve,
            trades=scored_trades,
            initial_equity=opening_equity,
            bar_clock=bar_clock,
            traded_notional=scored_notional,
            exposure_bars=exposure,
            total_fees=scored_fees,
            total_slippage_cost=scored_slippage,
            unrealized_pnl=result.unrealized_pnl,
            risk_free_rate=risk_free_rate,
        )
        windows.append(
            WindowResult(split=split, metrics=metrics, result=result, warmup_bars_used=used)
        )

    return WalkForwardResult(
        engine=describe(engine),
        windows=tuple(windows),
        bar_clock=bar_clock.label,
    )


def _held_at(result: ReplayResult, index: int) -> bool:
    """Whether a position was open at the close of bar `index`.

    Reconstructed from the fill sequence rather than stored per bar: fills
    alternate BUY, SELL, so a position is open at `index` exactly when the most
    recent fill at or before it was a BUY.
    """
    open_position = False
    for fill in result.fills:
        if fill.bar_index > index:
            break
        open_position = fill.side.name == "BUY"
    return open_position


__all__ = [
    "WalkForwardError",
    "WalkForwardResult",
    "WindowResult",
    "run_walk_forward",
]
