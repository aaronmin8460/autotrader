"""Assemble the machine-readable evidence from a completed scoring run.

Reads the decision series the scoring pass wrote and produces the tables the
human report is written from: headline metrics per engine per symbol under both
cost assumptions, per-window metrics, stability, regime behaviour, decision
disagreement, and the buy-and-hold benchmark the whole thing has to be read
against.

Writes JSON and CSV under the external reports root. Nothing is written inside
the repository and nothing here can change what the trading system does.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from studies.crypto_v1_v5.analysis import (
    COST_MODELS,
    buy_and_hold_return,
    disagreement_summary,
    headline_metrics,
    per_window_metrics,
    portfolio_metrics,
    regime_breakdown,
    representative_disagreements,
    signal_distribution,
    stability,
)
from studies.crypto_v1_v5.dataset import load_evaluation_frame
from studies.crypto_v1_v5.run_scoring import dataset_paths


def engine_rows(metrics_by_cost, symbol: str) -> list[dict]:
    rows: list[dict] = []
    for cost_label, per_engine in metrics_by_cost.items():
        for engine, metrics in per_engine.items():
            record = metrics.to_json_dict()
            rows.append(
                {
                    "symbol": symbol,
                    "cost_model": cost_label,
                    "cost_label": COST_MODELS[cost_label].label,
                    "engine": engine,
                    **record,
                }
            )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--variant", default="selected")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    decisions_all = pd.read_parquet(run_dir / f"decisions_{args.variant}.parquet")
    decisions_all["timestamp"] = pd.to_datetime(decisions_all["timestamp"], utc=True)
    folds = json.loads((run_dir / "v4_walkforward_folds.json").read_text())

    out = run_dir / f"analysis_{args.variant}"
    out.mkdir(exist_ok=True)

    headline_rows: list[dict] = []
    window_frames: list[pd.DataFrame] = []
    regime_frames: list[pd.DataFrame] = []
    distribution_frames: list[pd.DataFrame] = []
    stability_frames: list[pd.DataFrame] = []
    disagreement: dict[str, object] = {}
    benchmark: dict[str, object] = {}
    example_frames: list[pd.DataFrame] = []
    all_frames: dict[str, pd.DataFrame] = {}

    for symbol, path in dataset_paths(Path(args.datasets)).items():
        bars, _ = load_evaluation_frame(Path(path))
        decisions = decisions_all[decisions_all["symbol"] == symbol]
        if decisions.empty:
            continue

        all_frames[symbol] = bars
        headline_rows.extend(engine_rows(headline_metrics(bars, decisions), symbol))

        symbol_folds = [f for f in folds if f["symbol"] == symbol and f["variant"] == args.variant]
        seen: set[str] = set()
        unique_folds = []
        for fold in symbol_folds:
            if fold["fold_id"] not in seen:
                seen.add(fold["fold_id"])
                unique_folds.append(fold)

        for cost_label, model in COST_MODELS.items():
            windows = per_window_metrics(bars, decisions, unique_folds, cost_model=model)
            windows["symbol"] = symbol
            windows["cost_model"] = cost_label
            window_frames.append(windows)
            table = stability(windows)
            table["symbol"] = symbol
            table["cost_model"] = cost_label
            stability_frames.append(table)

        regimes = regime_breakdown(decisions)
        regimes["symbol"] = symbol
        regime_frames.append(regimes)

        distribution = signal_distribution(decisions)
        distribution["symbol"] = symbol
        distribution_frames.append(distribution)

        disagreement[symbol] = disagreement_summary(decisions)

        for left, right in (("v1", "v5"), ("v3", "v5")):
            examples = representative_disagreements(bars, decisions, left=left, right=right)
            if not examples.empty:
                examples["symbol"] = symbol
                example_frames.append(examples.assign(pair=f"{left}_vs_{right}"))

        start = decisions["timestamp"].min()
        end = decisions["timestamp"].max()
        benchmark[symbol] = {
            "scoring_start": start.isoformat(),
            "scoring_end": end.isoformat(),
            "buy_and_hold_return": buy_and_hold_return(bars, start, end),
        }

    portfolio_rows: list[dict] = []
    if len(all_frames) > 1:
        for cost_label, model in COST_MODELS.items():
            combined = portfolio_metrics(all_frames, decisions_all, cost_model=model)
            for engine, metrics in combined.items():
                portfolio_rows.append(
                    {
                        "symbol": "PORTFOLIO",
                        "cost_model": cost_label,
                        "cost_label": model.label,
                        "engine": engine,
                        **metrics.to_json_dict(),
                    }
                )
    pd.DataFrame(headline_rows + portfolio_rows).to_csv(out / "headline_metrics.csv", index=False)
    pd.concat(window_frames, ignore_index=True).to_csv(out / "per_window_metrics.csv", index=False)
    pd.concat(stability_frames, ignore_index=True).to_csv(out / "stability.csv", index=False)
    pd.concat(regime_frames, ignore_index=True).to_csv(out / "regime_breakdown.csv", index=False)
    pd.concat(distribution_frames, ignore_index=True).to_csv(
        out / "signal_distribution.csv", index=False
    )
    if example_frames:
        pd.concat(example_frames, ignore_index=True).to_csv(
            out / "disagreement_examples.csv", index=False
        )
    (out / "disagreement.json").write_text(json.dumps(disagreement, indent=2))
    (out / "benchmark.json").write_text(json.dumps(benchmark, indent=2, default=str))
    print(f"wrote analysis for variant={args.variant} to {out}")


if __name__ == "__main__":
    main()
