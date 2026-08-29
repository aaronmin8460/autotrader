"""Turning a scored decision series into the evidence the report is written from.

Every number here comes out of `autotrader.research`: the simulator, the cost
models, the metrics and the walk-forward runner. This module chooses what to
measure and how to slice it; it computes no performance statistic of its own.

**Windows are replayed independently.** `run_walk_forward` starts each window
flat with the same capital, so seven windows are seven comparable samples rather
than one compounding path in which the first window decides the shape of the
last. That is what makes "how many windows did this engine win" a question with
an answer.

**Costs are reported as a pair, never as one number.** Every engine is replayed
under the zero-cost model and under the shipped crypto model, because the gap
between them *is* the finding for a strategy that trades often: an edge that
exists gross and vanishes net is not an edge, and the only way to see that is to
compute both on purpose.

**Regime and disagreement come from the decision series, not from a new model.**
The regime label is the one the engines themselves recorded on each bar
(`MarketRegime`, classified deterministically by the shipped policy), so this
module does not invent a classifier and cannot overfit one.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from autotrader.research.costs import CRYPTO_COST, STRESS_COST, ZERO_COST, CostModel
from autotrader.research.metrics import CRYPTO_15M, PerformanceMetrics, metrics_for_replay
from autotrader.research.replay import ReplayConfig, ReplayResult, replay
from autotrader.research.splits import TimeSplit
from studies.crypto_v1_v5.adapters import DecisionSeriesEngine
from studies.crypto_v1_v5.scoring import STUDY_VERSIONS, records_from_frame
from studies.crypto_v1_v5.walkforward import TRAIN_TEST_GAP_BARS

#: The cost assumptions every engine is measured under.
#:
#: `gross` isolates how much of a result is the strategy and how much is the
#: fee schedule. `net` is the shipped crypto assumption and is the headline.
#: `stress` is the shipped punitive model, and it is here because a conclusion
#: that reverses between `net` and `stress` is a conclusion about a fee
#: schedule rather than about an engine.
COST_MODELS: Mapping[str, CostModel] = {
    "gross": ZERO_COST,
    "net": CRYPTO_COST,
    "stress": STRESS_COST,
}


class AnalysisError(Exception):
    """The evidence cannot be assembled from what was supplied."""


def series_engine(decisions: pd.DataFrame, engine: str) -> DecisionSeriesEngine:
    """The replayable form of one engine's scored series.

    `warmup_bars` is zero because the warm-up already happened: the series was
    produced by handing each engine a full lookback window at every instant, and
    only instants it could actually answer are in it. Declaring a warm-up here
    would make the walk-forward runner prepend bars the series has no decisions
    for and then exclude them from scoring, which is the same window with extra
    steps.
    """
    records = records_from_frame(decisions, engine)
    if not records:
        raise AnalysisError(f"The decision series holds no rows for {engine!r}.")
    return DecisionSeriesEngine(
        records,
        name=engine,
        version=engine,
        warmup_bars=0,
        parameters={"source": "walk-forward scored decision series"},
    )


def replay_engine(
    bars: pd.DataFrame, decisions: pd.DataFrame, engine: str, *, cost_model: CostModel
) -> ReplayResult:
    """Replay one engine over the bars its series covers."""
    covered = decisions[decisions["engine"] == engine]["timestamp"]
    if covered.empty:
        raise AnalysisError(f"No decisions for {engine!r}.")
    window = bars[
        (bars["timestamp"] >= covered.min()) & (bars["timestamp"] <= covered.max())
    ].reset_index(drop=True)
    return replay(window, series_engine(decisions, engine), ReplayConfig(cost_model=cost_model))


def metrics_of(result: ReplayResult) -> PerformanceMetrics:
    return metrics_for_replay(result, CRYPTO_15M)


def splits_for_folds(
    bars: pd.DataFrame,
    folds: Sequence[Mapping[str, object]],
    *,
    embargo_bars: int = TRAIN_TEST_GAP_BARS,
) -> tuple[TimeSplit, ...]:
    """One `TimeSplit` per out-of-sample window, positioned into `bars`.

    The train range and the embargo describe the structure V4 was actually
    fitted under, so `audit_splits` checks the real design rather than a
    re-derivation of it. Each window's training data ends `embargo_bars` before
    the window opens: the label horizon, so no training outcome resolves inside
    the window being scored, plus a one-day embargo on top of it.

    Declaring the gap is not a formality. The auditor refuses a split set that
    leaves no embargo precisely because adjacent train and test bars share
    indicator lookback and label horizon, and a study that quietly passed zero
    here would be asserting a property it had not arranged.
    """
    timestamps = bars["timestamp"].reset_index(drop=True)
    splits: list[TimeSplit] = []
    for index, fold in enumerate(folds):
        test_start = pd.Timestamp(str(fold["test_start"]))
        test_end = pd.Timestamp(str(fold["test_end"]))
        inside = timestamps[(timestamps >= test_start) & (timestamps <= test_end)]
        if inside.empty:
            continue
        start = int(inside.index[0])
        stop = int(inside.index[-1]) + 1
        train_end = start - embargo_bars
        if train_end <= 0:
            continue
        splits.append(
            TimeSplit(
                index=index,
                train_start=0,
                train_end=train_end,
                test_start=start,
                test_end=stop,
                embargo_bars=embargo_bars,
                train_start_timestamp=timestamps.iloc[0],
                train_end_timestamp=timestamps.iloc[train_end - 1],
                test_start_timestamp=timestamps.iloc[start],
                test_end_timestamp=timestamps.iloc[stop - 1],
            )
        )
    return tuple(splits)


# --------------------------------------------------------------------------
# Slicing the evidence
# --------------------------------------------------------------------------


def per_window_metrics(
    bars: pd.DataFrame,
    decisions: pd.DataFrame,
    folds: Sequence[Mapping[str, object]],
    *,
    cost_model: CostModel,
) -> pd.DataFrame:
    """Each engine's outcome in each out-of-sample window, replayed independently."""
    rows: list[dict[str, object]] = []
    for fold in folds:
        test_start = pd.Timestamp(str(fold["test_start"]))
        test_end = pd.Timestamp(str(fold["test_end"]))
        window_bars = bars[
            (bars["timestamp"] >= test_start) & (bars["timestamp"] <= test_end)
        ].reset_index(drop=True)
        window_decisions = decisions[
            (decisions["timestamp"] >= test_start) & (decisions["timestamp"] <= test_end)
        ]
        if window_bars.empty or window_decisions.empty:
            continue
        for engine in STUDY_VERSIONS:
            subset = window_decisions[window_decisions["engine"] == engine]
            if subset.empty:
                continue
            result = replay(
                window_bars,
                series_engine(window_decisions, engine),
                ReplayConfig(cost_model=cost_model),
            )
            metrics = metrics_of(result)
            rows.append(
                {
                    "fold_id": fold["fold_id"],
                    "is_holdout": bool(fold["is_holdout"]),
                    "test_start": test_start,
                    "test_end": test_end,
                    "engine": engine,
                    "bars": metrics.bar_count,
                    "total_return": metrics.total_return,
                    "sharpe_ratio": metrics.sharpe_ratio,
                    "sortino_ratio": metrics.sortino_ratio,
                    "max_drawdown": metrics.max_drawdown,
                    "max_drawdown_bars": metrics.max_drawdown_bars,
                    "trade_count": metrics.trade_count,
                    "win_rate": metrics.win_rate,
                    "profit_factor": metrics.profit_factor,
                    "turnover": metrics.turnover,
                    "exposure": metrics.exposure,
                    "cost_drag": metrics.cost_drag,
                }
            )
    return pd.DataFrame(rows)


def buy_and_hold_return(bars: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> float:
    """What the sample itself paid, which most of a long-only crypto result is."""
    window = bars[(bars["timestamp"] >= start) & (bars["timestamp"] <= end)]
    if window.empty:
        return float("nan")
    first = float(window["open"].iloc[0])
    last = float(window["close"].iloc[-1])
    return (last - first) / first if first else float("nan")


def regime_breakdown(decisions: pd.DataFrame) -> pd.DataFrame:
    """How each engine behaved in each regime the shipped classifier recorded."""
    rows: list[dict[str, object]] = []
    for (engine, regime), group in decisions.groupby(["engine", "regime"], observed=True):
        counts = group["signal"].value_counts()
        total = int(len(group))
        rows.append(
            {
                "engine": engine,
                "regime": regime,
                "bars": total,
                "share_of_bars": total / len(decisions[decisions["engine"] == engine]),
                "buy": int(counts.get("BUY", 0)),
                "hold": int(counts.get("HOLD", 0)),
                "sell": int(counts.get("SELL", 0)),
                "actionable_rate": float((group["signal"] != "HOLD").mean()),
                "mean_score": float(group["score"].mean()),
                "mean_confidence": float(group["confidence"].mean()),
            }
        )
    return pd.DataFrame(rows).sort_values(["engine", "regime"]).reset_index(drop=True)


def pivot_signals(decisions: pd.DataFrame) -> pd.DataFrame:
    """One row per instant, one column per engine, holding that engine's direction."""
    wide = decisions.pivot_table(
        index="timestamp", columns="engine", values="signal", aggfunc="first"
    )
    return wide.dropna(how="any")


def disagreement_summary(decisions: pd.DataFrame) -> dict[str, object]:
    """How often the engines agree, and where V5 differs from what it is built on."""
    wide = pivot_signals(decisions)
    if wide.empty:
        return {}
    present = [engine for engine in STUDY_VERSIONS if engine in wide.columns]
    unanimous = wide[present].nunique(axis=1) == 1
    summary: dict[str, object] = {
        "common_instants": int(len(wide)),
        "all_engines_agree": int(unanimous.sum()),
        "all_engines_agree_rate": float(unanimous.mean()),
    }
    for left, right in (("v1", "v5"), ("v3", "v5"), ("v4", "v5"), ("v2", "v5"), ("v3", "v4")):
        if left in wide.columns and right in wide.columns:
            differs = wide[left] != wide[right]
            summary[f"{left}_vs_{right}_disagreement_rate"] = float(differs.mean())
            summary[f"{left}_vs_{right}_disagreements"] = int(differs.sum())
    if "v1" in wide.columns and "v5" in wide.columns:
        summary["v5_blocks_v1_trade"] = int(((wide["v1"] != "HOLD") & (wide["v5"] == "HOLD")).sum())
        summary["v5_adds_trade_v1_lacks"] = int(
            ((wide["v1"] == "HOLD") & (wide["v5"] != "HOLD")).sum()
        )
    if "v3" in wide.columns and "v5" in wide.columns:
        summary["v5_blocks_v3_trade"] = int(((wide["v3"] != "HOLD") & (wide["v5"] == "HOLD")).sum())
        summary["v5_adds_trade_v3_lacks"] = int(
            ((wide["v3"] == "HOLD") & (wide["v5"] != "HOLD")).sum()
        )
    return summary


def signal_distribution(decisions: pd.DataFrame) -> pd.DataFrame:
    """The BUY/HOLD/SELL mix and confidence profile of each engine."""
    rows: list[dict[str, object]] = []
    for engine, group in decisions.groupby("engine", observed=True):
        counts = group["signal"].value_counts()
        rows.append(
            {
                "engine": engine,
                "bars": int(len(group)),
                "buy": int(counts.get("BUY", 0)),
                "hold": int(counts.get("HOLD", 0)),
                "sell": int(counts.get("SELL", 0)),
                "actionable_rate": float((group["signal"] != "HOLD").mean()),
                "mean_confidence": float(group["confidence"].mean()),
                "median_confidence": float(group["confidence"].median()),
                "p90_confidence": float(group["confidence"].quantile(0.90)),
                "max_confidence": float(group["confidence"].max()),
                "mean_score": float(group["score"].mean()),
            }
        )
    return pd.DataFrame(rows).sort_values("engine").reset_index(drop=True)


@dataclass(frozen=True)
class StabilityRecord:
    """How consistent one engine was across the out-of-sample windows."""

    engine: str
    windows: int
    windows_positive: int
    windows_won: int
    mean_return: float
    median_return: float
    return_dispersion: float
    worst_window_return: float
    best_window_return: float
    sharpe_dispersion: float | None
    concentration: float

    def to_record(self) -> dict[str, object]:
        return {
            "engine": self.engine,
            "windows": self.windows,
            "windows_positive": self.windows_positive,
            "windows_won": self.windows_won,
            "mean_return": self.mean_return,
            "median_return": self.median_return,
            "return_dispersion": self.return_dispersion,
            "worst_window_return": self.worst_window_return,
            "best_window_return": self.best_window_return,
            "sharpe_dispersion": self.sharpe_dispersion,
            "best_window_share_of_total": self.concentration,
        }


def stability(per_window: pd.DataFrame) -> pd.DataFrame:
    """Per-engine dispersion across windows, and how concentrated the result is.

    `best_window_share_of_total` is the share of an engine's summed window
    returns contributed by its single best window. A figure near or above one
    means the result rests on one period, which is the difference between an
    edge and a lucky quarter.
    """
    if per_window.empty:
        return pd.DataFrame()
    winners = per_window.loc[per_window.groupby("fold_id")["total_return"].idxmax()]
    win_counts = winners["engine"].value_counts()
    rows: list[dict[str, object]] = []
    for engine, group in per_window.groupby("engine", observed=True):
        returns = group["total_return"].astype(float)
        total = returns.sum()
        best = returns.max()
        sharpes = group["sharpe_ratio"].dropna().astype(float)
        rows.append(
            StabilityRecord(
                engine=str(engine),
                windows=int(len(group)),
                windows_positive=int((returns > 0).sum()),
                windows_won=int(win_counts.get(engine, 0)),
                mean_return=float(returns.mean()),
                median_return=float(returns.median()),
                return_dispersion=float(returns.std(ddof=1)) if len(returns) > 1 else 0.0,
                worst_window_return=float(returns.min()),
                best_window_return=float(best),
                sharpe_dispersion=(float(sharpes.std(ddof=1)) if len(sharpes) > 1 else None),
                concentration=float(best / total)
                if total not in (0.0,) and not math.isnan(total)
                else float("nan"),
            ).to_record()
        )
    return pd.DataFrame(rows).sort_values("engine").reset_index(drop=True)


def headline_metrics(
    bars: pd.DataFrame, decisions: pd.DataFrame
) -> dict[str, dict[str, PerformanceMetrics]]:
    """Every engine's full-period result under both cost assumptions."""
    out: dict[str, dict[str, PerformanceMetrics]] = {}
    for label, model in COST_MODELS.items():
        out[label] = {}
        for engine in STUDY_VERSIONS:
            if decisions[decisions["engine"] == engine].empty:
                continue
            out[label][engine] = metrics_of(
                replay_engine(bars, decisions, engine, cost_model=model)
            )
    return out


def as_float(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


__all__ = [
    "COST_MODELS",
    "AnalysisError",
    "StabilityRecord",
    "as_float",
    "buy_and_hold_return",
    "disagreement_summary",
    "headline_metrics",
    "metrics_of",
    "per_window_metrics",
    "pivot_signals",
    "portfolio_metrics",
    "regime_breakdown",
    "replay_engine",
    "representative_disagreements",
    "series_engine",
    "signal_distribution",
    "splits_for_folds",
    "stability",
]


def representative_disagreements(
    bars: pd.DataFrame,
    decisions: pd.DataFrame,
    *,
    left: str = "v1",
    right: str = "v5",
    count: int = 6,
    forward_bars: int = 96,
) -> pd.DataFrame:
    """A deterministic, evenly spaced sample of instants where two engines differed.

    **Chosen by position, never by outcome.** The disagreements are ordered in
    time and sampled at even intervals, so the sample cannot be steered towards
    the cases that flatter either engine. The forward return over the next
    `forward_bars` is attached to each one and reported whatever it says: an
    example set drawn only from the occasions an engine was right is an
    advertisement, not evidence.

    The forward return is what the market did next, not what either engine
    earned. Whether a proposal became a position depends on the state the
    simulator was in, which is the replay's question rather than this table's.
    """
    wide = pivot_signals(decisions)
    if wide.empty or left not in wide.columns or right not in wide.columns:
        return pd.DataFrame()
    differing = wide[wide[left] != wide[right]]
    if differing.empty:
        return pd.DataFrame()

    positions = (
        range(0, len(differing), max(1, len(differing) // count))
        if len(differing) > count
        else range(len(differing))
    )
    closes = bars.set_index("timestamp")["close"]
    rows: list[dict[str, object]] = []
    for position in list(positions)[:count]:
        moment = differing.index[position]
        if moment not in closes.index:
            continue
        start_index = int(closes.index.get_loc(moment))
        end_index = min(start_index + forward_bars, len(closes) - 1)
        entry = float(closes.iloc[start_index])
        forward = (float(closes.iloc[end_index]) - entry) / entry if entry else float("nan")
        rows.append(
            {
                "timestamp": moment,
                "left_engine": left,
                "right_engine": right,
                "left_signal": differing.loc[moment, left],
                "right_signal": differing.loc[moment, right],
                "forward_return_24h": forward,
                "favoured": (
                    "neither"
                    if abs(forward) < 1e-9
                    else _favoured(
                        differing.loc[moment, left], differing.loc[moment, right], forward
                    )
                ),
            }
        )
    return pd.DataFrame(rows)


def _favoured(left_signal: str, right_signal: str, forward: float) -> str:
    """Which of two differing directions the next move actually rewarded."""

    def scored(signal: str) -> float:
        if signal == "BUY":
            return forward
        if signal == "SELL":
            return -forward
        return 0.0

    left_score, right_score = scored(left_signal), scored(right_signal)
    if left_score == right_score:
        return "neither"
    return "left" if left_score > right_score else "right"


def portfolio_metrics(
    frames: Mapping[str, pd.DataFrame],
    decisions: pd.DataFrame,
    *,
    cost_model: CostModel,
) -> dict[str, PerformanceMetrics]:
    """Each engine's combined result across both symbols, as independent sleeves.

    `replay_portfolio` splits the starting capital evenly and replays each
    symbol on its own, then aggregates the equity curves. Two symbols therefore
    never compete for the same dollar. That is the research package's stated
    limitation rather than this study's choice, and it is the reason a combined
    figure here is the sum of two sleeves and not a simulation of the live
    runtime's sequential sizing against one shared account.
    """
    from autotrader.research.replay import replay_portfolio

    out: dict[str, PerformanceMetrics] = {}
    for engine in STUDY_VERSIONS:
        subset = decisions[decisions["engine"] == engine]
        if subset.empty:
            continue
        covered_start = subset["timestamp"].min()
        covered_end = subset["timestamp"].max()
        datasets = {
            symbol: frame[
                (frame["timestamp"] >= covered_start) & (frame["timestamp"] <= covered_end)
            ].reset_index(drop=True)
            for symbol, frame in frames.items()
        }
        result = replay_portfolio(
            datasets, series_engine(subset, engine), ReplayConfig(cost_model=cost_model)
        )
        out[engine] = metrics_for_replay(result, CRYPTO_15M)
    return out
