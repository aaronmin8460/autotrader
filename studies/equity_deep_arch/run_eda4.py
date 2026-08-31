"""EDA-4 meta-labeled V3: learn to ABSTAIN from V3 entries, walk-forward.

Predeclared in the search ledger. The candidate population is V3's own round
trips under the primary cost model; the meta-model is a logistic ACT/ABSTAIN
classifier over market-context features at entry, trained per published
window on trades fully exited before that window, with a fail-safe
pass-through when fewer than 60 training trades exist.

An abstained trade is removed by flipping its BUY records to HOLD from the
entry signal through the exit signal; the trade's SELL then no-ops in replay.

Usage:
    python -m studies.equity_deep_arch.run_eda4
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from autotrader.data.validation import EQUITY_UNIVERSE_LABEL
from autotrader.decision.contract import DecisionSignal
from autotrader.decision.probability import sigmoid
from autotrader.equity import EQUITY_SYMBOLS
from autotrader.equity.session import market_date
from autotrader.ml.v4 import fit_logistic, fit_standardizer
from autotrader.research.costs import EQUITY_COST
from autotrader.research.replay import ReplayConfig, replay
from studies.equity_10_full import STUDY_SYMBOLS
from studies.equity_10_full.windows import FULL_WINDOWS
from studies.equity_deep_arch.context import build_context_frame, session_index
from studies.equity_deep_arch.evaluate import (
    evaluate_challenger,
    load_region_frame,
    load_stored_series,
    write_json,
)
from studies.equity_deep_arch.run_eda1 import default_datasets, default_decisions
from studies.equity_v1_v5.adapters import DecisionRecord, DecisionSeriesEngine
from studies.equity_v1_v5.scoring import INITIAL_CASH

OUTPUT = Path("/Volumes/AUTOTRADER_QA/reports/equity-deep-architecture/eda4")

#: Predeclared: abstain when predicted win probability is materially below
#: V3's ~0.39 base rate.
ABSTAIN_BELOW = 0.35

#: Predeclared: windows with fewer training trades apply no abstention.
MIN_TRAINING_TRADES = 60

META_FEATURES: tuple[str, ...] = (
    "mkt_drawdown",
    "mkt_dist_sma",
    "mkt_vol20",
    "breadth",
    "own_dist_sma",
    "own_rel20",
)


def extract_trades(datasets: Path, decisions: Path) -> pd.DataFrame:
    """Every V3 fill-pair under primary cost, with entry/exit signal bars."""
    rows: list[dict[str, object]] = []
    config = ReplayConfig(
        initial_cash=INITIAL_CASH,
        cost_model=EQUITY_COST,
        supported_symbols=EQUITY_SYMBOLS,
        universe_label=EQUITY_UNIVERSE_LABEL,
    )
    for symbol in STUDY_SYMBOLS:
        frame = load_region_frame(datasets, symbol)
        records = load_stored_series(decisions, symbol, "V3")
        engine = DecisionSeriesEngine(list(records), name="V3", version="v3", warmup_bars=0)
        result = replay(frame, engine, config)
        fills = list(result.fills)
        index = 0
        while index < len(fills):
            entry = fills[index]
            exit_fill = fills[index + 1] if index + 1 < len(fills) else None
            net = None
            exit_signal = None
            if exit_fill is not None:
                gross = (exit_fill.fill_price - entry.fill_price) * entry.quantity
                net = float(gross - entry.fee - exit_fill.fee)
                exit_signal = exit_fill.signal_timestamp
            rows.append(
                {
                    "symbol": symbol,
                    "entry_signal": entry.signal_timestamp,
                    "exit_signal": exit_signal,
                    "net_pnl": net,
                }
            )
            index += 2
    return pd.DataFrame(rows)


def attach_features(trades: pd.DataFrame, context: pd.DataFrame, ordinal) -> pd.DataFrame:
    """Context features at entry, lagged one session as predeclared."""
    keyed = context.set_index(["symbol", "session"])
    ordered_sessions = sorted(ordinal, key=lambda day: ordinal[day])
    values: list[dict[str, object] | None] = []
    for _, trade in trades.iterrows():
        day = market_date(trade["entry_signal"].to_pydatetime())
        position = ordinal.get(day)
        feature_row: dict[str, object] | None = None
        if position is not None and position > 0:
            previous = ordered_sessions[position - 1]
            key = (trade["symbol"], previous)
            if key in keyed.index:
                got = keyed.loc[key]
                feature_row = {name: float(got[name]) for name in META_FEATURES}
        values.append(feature_row)
    enriched = trades.copy()
    for name in META_FEATURES:
        enriched[name] = [None if row is None else row[name] for row in values]
    enriched["features_ok"] = [row is not None for row in values]
    enriched["entry_session_ord"] = [
        ordinal.get(market_date(ts.to_pydatetime())) for ts in enriched["entry_signal"]
    ]
    exit_ords = []
    for ts in enriched["exit_signal"]:
        exit_ords.append(None if ts is None else ordinal.get(market_date(ts.to_pydatetime())))
    enriched["exit_session_ord"] = exit_ords
    return enriched


def decide_abstentions(trades: pd.DataFrame, ordinal) -> tuple[set, dict[str, object]]:
    """Walk-forward ABSTAIN decisions; returns suppressed entry keys and stats."""
    suppressed: set = set()
    stats: list[dict[str, object]] = []
    for window in FULL_WINDOWS:
        first_ord = ordinal[min(day for day in ordinal if window.start <= day <= window.end)]
        last_ord = ordinal[max(day for day in ordinal if window.start <= day <= window.end)]
        train = trades[
            trades["features_ok"]
            & trades["net_pnl"].notna()
            & trades["exit_session_ord"].notna()
            & (trades["exit_session_ord"] < first_ord - 1)
        ]
        entries = trades[
            trades["features_ok"]
            & (trades["entry_session_ord"] >= first_ord)
            & (trades["entry_session_ord"] <= last_ord)
        ]
        if len(train) < MIN_TRAINING_TRADES:
            stats.append(
                {
                    "window": window.name,
                    "training_trades": int(len(train)),
                    "entries": int(len(entries)),
                    "mode": "pass-through",
                    "abstained": 0,
                }
            )
            continue
        matrix = train.loc[:, list(META_FEATURES)].to_numpy("float64")
        labels = (train["net_pnl"].to_numpy("float64") > 0).astype("float64")
        standardizer = fit_standardizer(matrix)
        standardized = np.asarray([standardizer.apply([float(v) for v in r]) for r in matrix])
        estimator = fit_logistic(standardized, labels)
        abstained = 0
        for _, trade in entries.iterrows():
            row = [float(trade[name]) for name in META_FEATURES]
            probability = sigmoid(estimator.raw_score(standardizer.apply(row)))
            if probability < ABSTAIN_BELOW:
                suppressed.add((trade["symbol"], trade["entry_signal"]))
                abstained += 1
        stats.append(
            {
                "window": window.name,
                "training_trades": int(len(train)),
                "training_base_rate": float(labels.mean()),
                "entries": int(len(entries)),
                "mode": "model",
                "abstained": abstained,
            }
        )
    return suppressed, {"windows": stats}


def build_overlay(trades: pd.DataFrame, decisions: Path, suppressed: set) -> dict[str, tuple]:
    """V3 with abstained trades' BUY runs flipped to HOLD."""
    spans: dict[str, list[tuple[pd.Timestamp, pd.Timestamp | None]]] = {}
    for _, trade in trades.iterrows():
        key = (trade["symbol"], trade["entry_signal"])
        if key in suppressed:
            spans.setdefault(trade["symbol"], []).append(
                (trade["entry_signal"], trade["exit_signal"])
            )
    challenger: dict[str, tuple] = {}
    for symbol in STUDY_SYMBOLS:
        records = load_stored_series(decisions, symbol, "V3")
        symbol_spans = spans.get(symbol, [])
        rewritten: list[DecisionRecord] = []
        for record in records:
            replace = False
            if record.signal is DecisionSignal.BUY:
                for start, end in symbol_spans:
                    if record.timestamp >= start and (end is None or record.timestamp < end):
                        replace = True
                        break
            if replace:
                rewritten.append(
                    DecisionRecord(
                        timestamp=record.timestamp,
                        symbol=record.symbol,
                        signal=DecisionSignal.HOLD,
                        score=record.score,
                        confidence=record.confidence,
                        regime=record.regime,
                        reasons=("EDA4_ABSTAIN",),
                    )
                )
            else:
                rewritten.append(record)
        challenger[symbol] = tuple(rewritten)
    return challenger


def main() -> None:
    datasets = default_datasets()
    decisions = default_decisions()
    started = time.perf_counter()
    OUTPUT.mkdir(parents=True, exist_ok=True)

    context_cache = Path(
        "/Volumes/AUTOTRADER_QA/reports/equity-deep-architecture/eda5/context_frame.parquet"
    )
    context = (
        pd.read_parquet(context_cache) if context_cache.exists() else build_context_frame(datasets)
    )
    ordinal = session_index(datasets)

    trades = extract_trades(datasets, decisions)
    print(f"extracted {len(trades)} V3 fill-pairs", flush=True)
    enriched = attach_features(trades, context, ordinal)
    suppressed, stats = decide_abstentions(enriched, ordinal)
    closed = enriched["net_pnl"].notna()
    stats["total_trades"] = int(len(enriched))
    stats["closed_trades"] = int(closed.sum())
    stats["base_win_rate"] = float((enriched.loc[closed, "net_pnl"] > 0).mean())
    stats["abstained_total"] = len(suppressed)
    stats["abstention_rate_of_modeled_entries"] = len(suppressed) / max(
        1, sum(s["entries"] for s in stats["windows"] if s["mode"] == "model")
    )
    write_json(OUTPUT / "abstention_stats.json", stats)
    print(f"abstained on {len(suppressed)} entries", flush=True)

    challenger = build_overlay(enriched, decisions, suppressed)
    result = evaluate_challenger(
        datasets, decisions, challenger, label="EDA4_MLV3", symbols=STUDY_SYMBOLS
    )
    write_json(OUTPUT / "full_evaluation.json", result)
    print(f"complete in {time.perf_counter() - started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
