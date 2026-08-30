"""Aggregation: every summary the report reads, computed from stored artifacts.

Nothing here re-scores an engine. Every figure is derived from the checkpointed
cells, the finalize summaries, the stored decision series and the stored
equity curves, so a number in the report can always be traced to the run that
produced it and regenerating the analysis cannot silently change a result.

    python -m studies.equity_10_full.analysis --datasets <dir> --output <dir> \
        --scope dev
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import pandas as pd

from studies.equity_10_full import STUDY_SYMBOLS
from studies.equity_10_full.benchmarks import BuyAndHoldEngine
from studies.equity_10_full.checkpoint import cell_path, read_json, series_path
from studies.equity_10_full.run_study import load_frame, log
from studies.equity_10_full.windows import DEV_WINDOWS, FULL_WINDOWS
from studies.equity_v1_v5.adapters import DecisionRecord
from studies.equity_v1_v5.scoring import COST_MODELS, INITIAL_CASH, frame_to_decisions
from studies.equity_v1_v5.windows import ScoringWindow

ENGINES = ("V1", "V2", "V3", "V4", "V5")
REALISTIC = "equity-marketable"

#: SPY trailing-peak drawdown depths separating the broad-market regimes.
#: Causal by construction: the peak at bar t reads closes at or before t.
MARKET_DRAWDOWN_BUCKETS = ((-0.05, "calm"), (-0.10, "pullback"), (-1.0, "drawdown"))

#: Bars in the trailing realized-volatility window (one session) and in the
#: baseline it is compared against (sixty sessions).
VOL_WINDOW = 26
VOL_BASELINE = 26 * 60


def windows_for(scope: str) -> tuple[ScoringWindow, ...]:
    return DEV_WINDOWS if scope == "dev" else FULL_WINDOWS


def summary_unit(scope: str) -> str:
    return "summary" if scope == "dev" else "summary_full"


def load_cells(output: Path, scope: str) -> dict[tuple[str, str], dict]:
    cells = {}
    for symbol in STUDY_SYMBOLS:
        for window in windows_for(scope):
            cells[(symbol, window.name)] = read_json(
                cell_path(output, kind="cells", symbol=symbol, unit=window.name)
            )
    return cells


def load_summaries(output: Path, scope: str) -> dict[str, dict]:
    return {
        symbol: read_json(
            cell_path(output, kind="finalize", symbol=symbol, unit=summary_unit(scope))
        )
        for symbol in STUDY_SYMBOLS
    }


# --------------------------------------------------------------------------
# Window and symbol stability
# --------------------------------------------------------------------------


def window_stability(cells: dict, scope: str) -> dict[str, object]:
    """Per-engine stability across chronological windows, realistic cost."""
    result: dict[str, object] = {}
    for engine in ENGINES:
        per_window: dict[str, dict[str, float]] = {}
        for window in windows_for(scope):
            pnls = {}
            for symbol in STUDY_SYMBOLS:
                metrics = cells[(symbol, window.name)]["engines"][engine]["replays"][REALISTIC][
                    "metrics"
                ]
                pnls[symbol] = float(Decimal(metrics["final_equity"]) - INITIAL_CASH)
            per_window[window.name] = {
                "total_pnl": sum(pnls.values()),
                "mean_return": sum(pnls.values()) / (len(pnls) * float(INITIAL_CASH)),
                "positive_symbols": sum(1 for value in pnls.values() if value > 0),
                "per_symbol_pnl": pnls,
            }
        totals = [entry["total_pnl"] for entry in per_window.values()]
        returns = [entry["mean_return"] for entry in per_window.values()]
        ordered = sorted(returns)
        best_window = max(per_window, key=lambda name: per_window[name]["total_pnl"])
        worst_window = min(per_window, key=lambda name: per_window[name]["total_pnl"])
        total_pnl = sum(totals)
        best_pnl = per_window[best_window]["total_pnl"]
        result[engine] = {
            "windows": per_window,
            "positive_windows": sum(1 for value in totals if value > 0),
            "negative_windows": sum(1 for value in totals if value < 0),
            "window_count": len(totals),
            "mean_window_return": sum(returns) / len(returns),
            "median_window_return": ordered[len(ordered) // 2],
            "window_return_stdev": pd.Series(returns).std(ddof=1) if len(returns) > 1 else None,
            "best_window": best_window,
            "worst_window": worst_window,
            "total_pnl": total_pnl,
            "best_window_pnl": best_pnl,
            "best_window_pnl_fraction_of_total": (best_pnl / total_pnl) if total_pnl > 0 else None,
        }
    return result


def symbol_stability(summaries: dict, scope: str) -> dict[str, object]:
    """Per-engine stability across symbols, from the continuous replays."""
    result: dict[str, object] = {}
    for engine in ENGINES:
        nets = {}
        for symbol in STUDY_SYMBOLS:
            metrics = summaries[symbol]["continuous_replays"][engine][REALISTIC]["metrics"]
            nets[symbol] = float(metrics["total_return"])
        pnls = {symbol: value * float(INITIAL_CASH) for symbol, value in nets.items()}
        total = sum(pnls.values())
        best = max(nets, key=nets.get)
        worst = min(nets, key=nets.get)
        loso = {}
        for dropped in STUDY_SYMBOLS:
            rest = [pnls[s] for s in STUDY_SYMBOLS if s != dropped]
            loso[dropped] = sum(rest) / (len(rest) * float(INITIAL_CASH))
        result[engine] = {
            "net_return_by_symbol": nets,
            "positive_symbols": sum(1 for value in nets.values() if value > 0),
            "negative_symbols": sum(1 for value in nets.values() if value < 0),
            "best_symbol": best,
            "worst_symbol": worst,
            "best_symbol_pnl_fraction_of_total": (pnls[best] / total) if total > 0 else None,
            "portfolio_mean_return": total / (len(pnls) * float(INITIAL_CASH)),
            "leave_one_out_mean_return": loso,
            "survives_without_best_symbol": loso[best] > 0,
        }
    return result


# --------------------------------------------------------------------------
# Portfolio
# --------------------------------------------------------------------------


def portfolio_metrics(datasets: Path, output: Path, scope: str) -> dict[str, object]:
    """Equal-capital independent-sleeve portfolio replays, per engine and cost model.

    The shipped `replay_portfolio` semantics, stated plainly in the output:
    sleeves never compete for capital, so this is the sum of ten independent
    books and not a shared-account simulation.
    """
    from autotrader.data.validation import EQUITY_UNIVERSE_LABEL
    from autotrader.equity import EQUITY_SYMBOLS
    from autotrader.research.metrics import EQUITY_15M, metrics_for_replay
    from autotrader.research.replay import ReplayConfig, replay_portfolio
    from studies.equity_v1_v5.adapters import DecisionSeriesEngine

    windows = windows_for(scope)
    region = ScoringWindow(
        name="region", start=windows[0].start, end=windows[-1].end, covers="portfolio region"
    )
    frames = {symbol: region.bars(load_frame(datasets, symbol)) for symbol in STUDY_SYMBOLS}

    result: dict[str, object] = {
        "semantics": (
            "independent capital sleeves - initial cash split equally across the ten "
            "symbols, each replayed alone; sleeves never compete for the same dollar "
            "and no shared-account interaction is simulated"
        ),
        "initial_cash": str(INITIAL_CASH),
        "engines": {},
    }
    for engine in (*ENGINES, "BUY_AND_HOLD"):
        blocks: dict[str, object] = {}
        for cost_model in COST_MODELS:
            config = ReplayConfig(
                initial_cash=INITIAL_CASH,
                cost_model=cost_model,
                supported_symbols=EQUITY_SYMBOLS,
                universe_label=EQUITY_UNIVERSE_LABEL,
            )
            if engine == "BUY_AND_HOLD":
                driver = BuyAndHoldEngine()
            else:
                records: list[DecisionRecord] = []
                for symbol in STUDY_SYMBOLS:
                    for window in windows:
                        stored = pd.read_parquet(
                            series_path(output, symbol=symbol, window=window.name, engine=engine)
                        )
                        records.extend(frame_to_decisions(stored))
                driver = DecisionSeriesEngine(
                    records, name=engine, version=engine.lower(), warmup_bars=0
                )
            replayed = replay_portfolio(frames, driver, config)
            metrics = metrics_for_replay(replayed, EQUITY_15M)
            blocks[cost_model.label] = {
                "metrics": metrics.to_json_dict(),
                "per_sleeve_final_equity": {
                    symbol: str(sleeve.final_equity) for symbol, sleeve in replayed.sleeves.items()
                },
            }
            log(
                f"portfolio {engine}/{cost_model.label}: net "
                f"{metrics.to_json_dict()['total_return']:+.4f}"
            )
        result["engines"][engine] = blocks
    result["cash"] = {"total_return": 0.0, "note": "cash holds its capital by definition"}
    return result


# --------------------------------------------------------------------------
# V4 aggregate
# --------------------------------------------------------------------------


def v4_summary(cells: dict, summaries: dict, scope: str) -> dict[str, object]:
    """Null-selection frequency, OOS behaviour and calibration provenance."""
    selection = []
    thin_models = 0
    fitted_models = 0
    extreme_supports: list[int] = []
    for (symbol, window_name), cell in cells.items():
        train = cell["train"]
        selection.append(
            {
                "symbol": symbol,
                "window": window_name,
                "selected_family": train["selected_family"],
                "beat_baseline": train["beat_baseline"],
                "baseline_log_loss": train["baseline_log_loss"],
                "selected_log_loss": train["selected_log_loss"],
                "log_loss_improvement": train["log_loss_improvement"],
                "label_base_rate": train["label_base_rate"],
                "spanning_fraction": train["spanning_fraction"],
            }
        )
        for audit in train["calibration_audits"].values():
            if audit.get("method") == "IsotonicCalibration":
                fitted_models += 1
                if audit.get("extreme_from_thin_bins"):
                    thin_models += 1
                for step in audit.get("extreme_steps", []):
                    extreme_supports.append(int(step["validation_support"]))
    null_count = sum(1 for entry in selection if not entry["beat_baseline"])

    oos_gains = []
    for symbol in STUDY_SYMBOLS:
        for window_name, record in summaries[symbol]["v4_out_of_sample"].items():
            models = record.get("models", {})
            if "selected" in models:
                oos_gains.append(
                    {
                        "symbol": symbol,
                        "window": window_name,
                        "selected_gain_vs_null": models["selected"]["log_loss_gain_vs_null"],
                        "selected_auc": models["selected"]["metrics"].get("roc_auc"),
                        "shadow_gains": {
                            name: models[name]["log_loss_gain_vs_null"]
                            for name in models
                            if name.startswith("shadow_")
                        },
                    }
                )

    return {
        "cells": len(selection),
        "null_selected": null_count,
        "non_null_selected": len(selection) - null_count,
        "null_fraction": null_count / len(selection) if selection else None,
        "non_null_cells": [entry for entry in selection if entry["beat_baseline"]],
        "selection": selection,
        "calibration": {
            "fitted_models_audited": fitted_models,
            "models_with_thin_extreme_steps": thin_models,
            "thin_fraction": thin_models / fitted_models if fitted_models else None,
            "extreme_step_supports": sorted(extreme_supports)[:20],
        },
        "out_of_sample": oos_gains,
    }


# --------------------------------------------------------------------------
# V3 vs V5 disagreement
# --------------------------------------------------------------------------


def v3_v5_disagreement(output: Path, summaries: dict, scope: str) -> dict[str, object]:
    """Signal-level and economic comparison of V5 against its V3 half."""
    per_symbol: dict[str, object] = {}
    totals = defaultdict(int)
    for symbol in STUDY_SYMBOLS:
        counts = defaultdict(int)
        for window in windows_for(scope):
            v3 = frame_to_decisions(
                pd.read_parquet(series_path(output, symbol=symbol, window=window.name, engine="V3"))
            )
            v5 = frame_to_decisions(
                pd.read_parquet(series_path(output, symbol=symbol, window=window.name, engine="V5"))
            )
            v5_by_ts = {record.timestamp: record for record in v5}
            for v3_record in v3:
                v5_record = v5_by_ts.get(v3_record.timestamp)
                if v5_record is None:
                    continue
                counts["compared"] += 1
                v3_active = v3_record.to_signal() is not None
                v5_active = v5_record.to_signal() is not None
                if v3_record.signal != v5_record.signal:
                    counts["differing"] += 1
                    if v3_active and not v5_active:
                        counts["v5_suppressed_v3"] += 1
                    elif v5_active and not v3_active:
                        counts["v5_added_action"] += 1
                    else:
                        counts["direction_flip"] += 1
        v3_net = float(
            summaries[symbol]["continuous_replays"]["V3"][REALISTIC]["metrics"]["total_return"]
        )
        v5_net = float(
            summaries[symbol]["continuous_replays"]["V5"][REALISTIC]["metrics"]["total_return"]
        )
        per_symbol[symbol] = {
            **dict(counts),
            "v3_net_return": v3_net,
            "v5_net_return": v5_net,
            "v5_minus_v3": v5_net - v3_net,
        }
        for key, value in counts.items():
            totals[key] += value
    return {"per_symbol": per_symbol, "totals": dict(totals)}


# --------------------------------------------------------------------------
# Regimes
# --------------------------------------------------------------------------


def _spy_market_state(datasets: Path, scope: str) -> pd.DataFrame:
    """SPY's causal trailing-peak drawdown state on every bar of the region."""
    windows = windows_for(scope)
    frame = load_frame(datasets, "SPY")
    closes = frame["close"].astype("float64")
    peak = closes.cummax()
    drawdown = closes / peak - 1.0
    region = ScoringWindow(
        name="region", start=windows[0].start, end=windows[-1].end, covers="regime region"
    )
    first, last = region.positions(frame)
    states = []
    for depth in drawdown.iloc[first : last + 1]:
        for threshold, label in MARKET_DRAWDOWN_BUCKETS:
            if depth >= threshold:
                states.append(label)
                break
    return pd.DataFrame(
        {"timestamp": frame["timestamp"].iloc[first : last + 1].to_numpy(), "market_state": states}
    )


def regime_analysis(datasets: Path, output: Path, summaries: dict, scope: str) -> dict[str, object]:
    """Per-engine mean per-bar net return under causal regime labels.

    Three independent, deterministic labellings per bar:
    - the engine-reported V3 context regime (stored with every V3 decision);
    - SPY's trailing-peak drawdown state (broad market drawdown / recovery);
    - the symbol's own trailing one-session realized volatility against its
      trailing sixty-session median (high / low volatility).
    All three read only bars at or before the labelled bar.
    """
    spy_state = _spy_market_state(datasets, scope).set_index("timestamp")["market_state"]
    windows = windows_for(scope)
    region = ScoringWindow(
        name="region", start=windows[0].start, end=windows[-1].end, covers="regime region"
    )

    accumulators: dict[str, dict[tuple[str, str], list[float]]] = {
        engine: defaultdict(list) for engine in ENGINES
    }
    for symbol in STUDY_SYMBOLS:
        frame = load_frame(datasets, symbol)
        first, last = region.positions(frame)
        closes = frame["close"].astype("float64")
        returns = closes.pct_change()
        vol = returns.rolling(VOL_WINDOW).std()
        vol_median = vol.rolling(VOL_BASELINE).median()
        high_vol = (vol > vol_median).iloc[first : last + 1].to_numpy()
        timestamps = frame["timestamp"].iloc[first : last + 1].to_numpy()

        v3_regime: dict[pd.Timestamp, str] = {}
        for window in windows:
            stored = pd.read_parquet(
                series_path(output, symbol=symbol, window=window.name, engine="V3")
            )
            for ts, regime in zip(stored["timestamp"], stored["regime"], strict=True):
                v3_regime[pd.Timestamp(ts)] = str(regime)

        curves = pd.read_parquet(summaries[symbol]["equity_curves_parquet"])
        curve_ts = [pd.Timestamp(ts) for ts in curves["timestamp"]]
        for engine in ENGINES:
            equity = curves[engine].astype("float64").to_numpy()
            bar_returns = pd.Series(equity).pct_change().to_numpy()
            for position, ts in enumerate(curve_ts):
                if position == 0:
                    continue
                value = float(bar_returns[position])
                state = spy_state.get(ts, "calm")
                accumulators[engine][("market", state)].append(value)
                regime = v3_regime.get(ts)
                if regime is not None:
                    accumulators[engine][("v3_regime", regime)].append(value)
                idx = position if position < len(high_vol) else len(high_vol) - 1
                bucket = "high_vol" if bool(high_vol[idx]) else "low_vol"
                accumulators[engine][("volatility", bucket)].append(value)
        del timestamps
        log(f"regimes: {symbol} pooled.")

    report: dict[str, object] = {
        "definitions": {
            "market": "SPY trailing-peak drawdown: calm > -5%, pullback -5..-10%, drawdown <= -10%",
            "v3_regime": "the context regime stored with each V3 decision",
            "volatility": (
                f"trailing {VOL_WINDOW}-bar realized vol vs trailing "
                f"{VOL_BASELINE}-bar median, per symbol"
            ),
        },
        "engines": {},
    }
    for engine in ENGINES:
        entries = {}
        for (family, bucket), values in sorted(accumulators[engine].items()):
            series = pd.Series(values)
            entries[f"{family}:{bucket}"] = {
                "bars": int(len(series)),
                "mean_bar_return": float(series.mean()),
                "annualized_mean": float(series.mean() * 26 * 252),
                "positive_fraction": float((series > 0).mean()),
            }
        report["engines"][engine] = entries
    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate the study's stored artifacts.")
    parser.add_argument("--datasets", default=os.environ.get("EQUITY_DATASETS", "."))
    parser.add_argument("--output", default=os.environ.get("STUDY_REPORTS", "."))
    parser.add_argument("--scope", choices=["dev", "full"], default="dev")
    arguments = parser.parse_args()
    datasets, output = Path(arguments.datasets), Path(arguments.output)
    scope = arguments.scope
    analysis_dir = output / f"analysis_{scope}"
    analysis_dir.mkdir(parents=True, exist_ok=True)

    log(f"analysis({scope}): loading cells and summaries…")
    cells = load_cells(output, scope)
    summaries = load_summaries(output, scope)

    stages = {
        "window_stability.json": lambda: window_stability(cells, scope),
        "symbol_stability.json": lambda: symbol_stability(summaries, scope),
        "v4_summary.json": lambda: v4_summary(cells, summaries, scope),
        "v3_v5_disagreement.json": lambda: v3_v5_disagreement(output, summaries, scope),
        "portfolio_metrics.json": lambda: portfolio_metrics(datasets, output, scope),
        "regime_analysis.json": lambda: regime_analysis(datasets, output, summaries, scope),
    }
    for name, builder in stages.items():
        target = analysis_dir / name
        if target.exists():
            log(f"analysis({scope}): {name} exists, skipping.")
            continue
        log(f"analysis({scope}): building {name}…")
        payload = builder()
        target.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
        log(f"analysis({scope}): wrote {target}")
    log(f"analysis({scope}): complete.")


if __name__ == "__main__":
    main()
