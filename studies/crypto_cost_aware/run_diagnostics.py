"""Regenerate every diagnostic table the cost-aware report cites.

Reads only completed artifacts and the verified datasets; calls no engine and
trains nothing. Writes CSVs beside the candidate results so each figure in the
report has a machine-readable source.

    python -m studies.crypto_cost_aware.run_diagnostics \
        --datasets "$AUTOTRADER_QA_DATASETS/crypto-historical" \
        --run-dir  "$AUTOTRADER_QA_REPORTS/crypto-v1-v5-historical" \
        --out      "$AUTOTRADER_QA_REPORTS/crypto-cost-aware"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from autotrader.research.costs import CRYPTO_COST, STRESS_COST, ZERO_COST

from .costs import breakeven_move, breakeven_move_bps, naive_round_trip
from .diagnostics import (
    churn_table,
    decompose,
    load_trades,
    reconcile,
    schedules_agree,
    trade_edge_table,
    turnover_sensitivity,
)

DATASET_FILES = {
    "BTC/USD": "BTC_USD_15m_2024-01-01_2026-08-28.parquet",
    "ETH/USD": "ETH_USD_15m_2024-01-01_2026-08-28.parquet",
}
SCORING_START = "2025-01-01"

#: Horizons the unconditional move distribution is measured over, in 15-minute
#: bars. 4 is V4's shipped label horizon and is the reason this table exists.
HORIZONS = (1, 2, 4, 8, 16, 32, 96, 192, 384, 672)

#: Trailing bars used for the decision-time volatility estimate: 24 hours.
VOLATILITY_BARS = 96

WINDOWS = (
    ("W01", "2025-01-01", "2025-04-01"),
    ("W02", "2025-04-01", "2025-07-01"),
    ("W03", "2025-07-01", "2025-10-01"),
    ("W04", "2025-10-01", "2026-01-01"),
    ("W05", "2026-01-01", "2026-04-01"),
    ("W06", "2026-04-01", "2026-07-01"),
    ("W07", "2026-07-01", "2026-09-01"),
)


def horizon_table(datasets: Path) -> pd.DataFrame:
    """Unconditional |k-bar move| against the round-trip cost.

    The question this answers is hypothesis E's: is the move the shipped V4
    label asks about even large enough to be worth a round trip? Entry is the
    next bar's open and exit is `k` bars later, matching the simulator's
    execution rule, so the figure is comparable to a trade's reference return.
    """
    breakeven = float(breakeven_move(CRYPTO_COST))
    records = []
    for symbol, filename in DATASET_FILES.items():
        bars = pd.read_parquet(datasets / filename).sort_values("timestamp")
        bars = bars[bars.timestamp >= SCORING_START].reset_index(drop=True)
        for k in HORIZONS:
            forward = bars.open.shift(-(k + 1)) / bars.open.shift(-1) - 1.0
            forward = forward.dropna()
            records.append(
                {
                    "symbol": symbol,
                    "bars": k,
                    "hours": k * 0.25,
                    "median_abs_move_bps": forward.abs().median() * 1e4,
                    "mean_abs_move_bps": forward.abs().mean() * 1e4,
                    "pct_abs_above_breakeven": (forward.abs() > breakeven).mean() * 100,
                    "pct_up_above_breakeven": (forward > breakeven).mean() * 100,
                    "mean_move_bps": forward.mean() * 1e4,
                }
            )
    return pd.DataFrame(records)


def entry_feature_table(datasets: Path, trades: pd.DataFrame) -> pd.DataFrame:
    """Attach decision-time volatility to every trade the study took.

    The decision bar is the bar **before** the fill bar: the ledger's
    `entry_timestamp` is where the fill happened, and the proposal that caused
    it was derived from the preceding bar's close. Everything joined here is
    therefore knowable at the moment the decision was made.
    """
    rows = trades[trades.cost_model == "gross"]
    records = []
    for symbol, filename in DATASET_FILES.items():
        bars = pd.read_parquet(datasets / filename).sort_values("timestamp").reset_index(drop=True)
        log_returns = np.log(bars.close / bars.close.shift(1))
        volatility = log_returns.rolling(VOLATILITY_BARS).std()
        position_of = {timestamp: i for i, timestamp in enumerate(bars.timestamp)}
        for trade in rows[rows.symbol == symbol].itertuples():
            position = position_of.get(trade.entry_timestamp)
            if not position:
                continue
            decision = position - 1
            records.append(
                {
                    "symbol": symbol,
                    "engine": trade.engine,
                    "entry_timestamp": trade.entry_timestamp,
                    "reference_return": trade.reference_return,
                    "bars_held": trade.bars_held,
                    "decision_volatility": float(volatility.iat[decision]),
                }
            )
    frame = pd.DataFrame(records).dropna(subset=["decision_volatility"])
    frame["abs_return"] = frame.reference_return.abs()
    return frame


def _rank_corr(left: pd.Series, right: pd.Series) -> float:
    """Spearman correlation as Pearson on ranks. No SciPy in this environment."""
    if len(left) < 3:
        return float("nan")
    return float(left.rank().corr(right.rank()))


def volatility_edge_table(features: pd.DataFrame) -> pd.DataFrame:
    """Does decision-time volatility predict magnitude, and does it predict direction?

    Two columns, and the research question turns on their disagreeing. If
    volatility predicts `|move|` but predicts signed `move` negatively, then a
    gate that admits high-volatility entries buys larger moves in a worse
    direction, and the two effects have to be weighed rather than assumed.
    """
    records = []
    for (symbol, engine), group in features.groupby(["symbol", "engine"], sort=True):
        records.append(
            {
                "symbol": symbol,
                "engine": engine,
                "trades": len(group),
                "corr_volatility_abs_move": _rank_corr(group.decision_volatility, group.abs_return),
                "corr_volatility_signed_move": _rank_corr(
                    group.decision_volatility, group.reference_return
                ),
            }
        )
    return pd.DataFrame(records)


def volatility_quintile_table(features: pd.DataFrame) -> pd.DataFrame:
    """Mean edge after cost within each decision-time volatility quintile.

    Pooled across the deterministic engines per symbol, because the per-engine
    buckets are too small to read. A positive figure in any quintile would be
    the evidence hypothesis A needs; the table is reported whether or not one
    appears.
    """
    breakeven_bps = float(breakeven_move_bps(CRYPTO_COST))
    records = []
    for symbol, group in features.groupby("symbol", sort=True):
        group = group.copy()
        group["quintile"] = pd.qcut(group.decision_volatility, 5, labels=[1, 2, 3, 4, 5])
        for quintile, bucket in group.groupby("quintile", observed=True):
            records.append(
                {
                    "symbol": symbol,
                    "quintile": int(quintile),
                    "trades": len(bucket),
                    "mean_move_bps": bucket.reference_return.mean() * 1e4,
                    "mean_abs_move_bps": bucket.abs_return.mean() * 1e4,
                    "pct_clearing_breakeven": (bucket.reference_return > breakeven_bps / 1e4).mean()
                    * 100,
                    "edge_after_cost_bps": bucket.reference_return.mean() * 1e4 - breakeven_bps,
                }
            )
    return pd.DataFrame(records)


def volatility_stability_table(features: pd.DataFrame) -> pd.DataFrame:
    """The volatility-to-direction relationship, per walk-forward window.

    A relationship that only holds in one window is a window, not a
    relationship. W07 is the untouched holdout and is reported like the rest.
    """
    records = []
    for name, start, end in WINDOWS:
        for symbol, group in features.groupby("symbol", sort=True):
            window = group[(group.entry_timestamp >= start) & (group.entry_timestamp < end)]
            records.append(
                {
                    "window": name,
                    "symbol": symbol,
                    "trades": len(window),
                    "corr_volatility_signed_move": _rank_corr(
                        window.decision_volatility, window.reference_return
                    )
                    if len(window) > 25
                    else float("nan"),
                    "is_holdout": name == "W07",
                }
            )
    return pd.DataFrame(records)


def signal_persistence_table(run_dir: Path) -> pd.DataFrame:
    """Longest consecutive run of each engine's non-HOLD signal.

    The check that decides whether a persistence rule *means* anything for an
    engine. An engine whose signal never repeats is an event engine, and
    requiring two consecutive bars from it removes every trade it would ever
    take -- which is a way of switching it off, not of filtering it.
    """
    decisions = pd.read_parquet(run_dir / "decisions_selected.parquet")
    records = []
    for (symbol, engine), group in decisions.groupby(["symbol", "engine"], sort=True):
        signals = group.sort_values("timestamp").signal.to_numpy()
        longest: dict[str, int] = {}
        current, run = None, 0
        for signal in signals:
            run = run + 1 if signal == current else 1
            current = signal
            if signal != "HOLD":
                longest[signal] = max(longest.get(signal, 0), run)
        records.append(
            {
                "symbol": symbol,
                "engine": engine,
                "actionable_bars": int((group.signal != "HOLD").sum()),
                "longest_buy_run": longest.get("BUY", 0),
                "longest_sell_run": longest.get("SELL", 0),
            }
        )
    return pd.DataFrame(records)


def run(datasets: Path, run_dir: Path, out: Path) -> None:
    analysis = run_dir / "analysis_selected"
    trades = load_trades(analysis)
    headline = pd.read_csv(analysis / "headline_metrics.csv")
    decompositions = decompose(trades, CRYPTO_COST)
    features = entry_feature_table(datasets, trades)

    out.mkdir(parents=True, exist_ok=True)
    tables = {
        "cost_reconciliation": reconcile(decompositions, headline, "net"),
        "trade_edge": trade_edge_table(trades, CRYPTO_COST),
        "churn": churn_table(trades),
        "turnover_sensitivity": turnover_sensitivity(decompositions, CRYPTO_COST),
        "horizon_vs_cost": horizon_table(datasets),
        "volatility_edge": volatility_edge_table(features),
        "volatility_quintiles": volatility_quintile_table(features),
        "volatility_stability": volatility_stability_table(features),
        "signal_persistence": signal_persistence_table(run_dir),
    }
    for name, frame in tables.items():
        frame.to_csv(out / f"diag_{name}.csv", index=False)

    (out / "diag_cost_assumptions.json").write_text(
        json.dumps(
            {
                "models": {
                    model.label: {
                        "fee_rate_per_side": str(model.fee_rate),
                        "slippage_rate_per_side": str(model.slippage_rate),
                        "exact_round_trip_breakeven_bps": float(breakeven_move_bps(model)),
                        "naive_two_sided_sum_bps": float(naive_round_trip(model) * 10_000),
                    }
                    for model in (ZERO_COST, CRYPTO_COST, STRESS_COST)
                },
                "cost_model_schedules_agree": bool(schedules_agree(trades)),
                "volatility_bars": VOLATILITY_BARS,
                "scoring_start": SCORING_START,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {len(tables) + 1} diagnostic artifacts to {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    run(args.datasets, args.run_dir, args.out)


if __name__ == "__main__":
    main()
