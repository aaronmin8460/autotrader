"""Render a finished analysis directory as markdown tables.

The human report quotes these rather than restating them by hand, so a number
in the prose and the number in the stored CSV cannot drift apart.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

ENGINE_ORDER = ["v1", "v2", "v3", "v4", "v5"]


def _fmt(value: object, *, pct: bool = False, places: int = 2) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "n/a"
    if isinstance(value, (int,)) and not isinstance(value, bool):
        return f"{value:,}"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if pct:
        return f"{number * 100:+.{places}f}%"
    return f"{number:,.{places}f}"


def markdown_table(frame: pd.DataFrame, columns: list[tuple[str, str, dict]]) -> str:
    header = "| " + " | ".join(label for label, _, _ in columns) + " |"
    rule = "|" + "|".join("---" for _ in columns) + "|"
    lines = [header, rule]
    for _, row in frame.iterrows():
        cells = []
        for _, key, options in columns:
            cells.append(_fmt(row.get(key), **options) if options else str(row.get(key)))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def headline_block(headline: pd.DataFrame, symbol: str, cost: str) -> str:
    subset = headline[(headline["symbol"] == symbol) & (headline["cost_model"] == cost)].copy()
    subset["order"] = subset["engine"].map({e: i for i, e in enumerate(ENGINE_ORDER)})
    subset = subset.sort_values("order")
    return markdown_table(
        subset,
        [
            ("Engine", "engine", {}),
            ("Total return", "total_return", {"pct": True}),
            ("Annualized", "annualized_return", {"pct": True}),
            ("Sharpe", "sharpe_ratio", {"places": 2}),
            ("Sortino", "sortino_ratio", {"places": 2}),
            ("Max DD", "max_drawdown", {"pct": True}),
            ("DD bars", "max_drawdown_bars", {}),
            ("Trades", "trade_count", {}),
            ("Win rate", "win_rate", {"pct": True}),
            ("Profit factor", "profit_factor", {"places": 2}),
            ("Turnover", "turnover", {"places": 2}),
            ("Exposure", "exposure", {"pct": True}),
            ("Cost drag", "cost_drag", {"pct": True}),
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-dir", required=True)
    args = parser.parse_args()
    root = Path(args.analysis_dir)

    headline = pd.read_csv(root / "headline_metrics.csv")
    windows = pd.read_csv(root / "per_window_metrics.csv")
    stability = pd.read_csv(root / "stability.csv")
    regimes = pd.read_csv(root / "regime_breakdown.csv")
    distribution = pd.read_csv(root / "signal_distribution.csv")
    disagreement = json.loads((root / "disagreement.json").read_text())
    benchmark = json.loads((root / "benchmark.json").read_text())

    for symbol in sorted(headline["symbol"].unique()):
        for cost in ("gross", "net"):
            print(f"\n#### {symbol} — {cost}\n")
            print(headline_block(headline, symbol, cost))

    print("\n#### Buy and hold benchmark\n")
    for symbol, record in benchmark.items():
        print(
            f"- {symbol}: {_fmt(record['buy_and_hold_return'], pct=True)} "
            f"({record['scoring_start'][:10]} to {record['scoring_end'][:10]})"
        )

    print("\n#### Signal distribution\n")
    print(
        markdown_table(
            distribution.sort_values(["symbol", "engine"]),
            [
                ("Symbol", "symbol", {}),
                ("Engine", "engine", {}),
                ("Bars", "bars", {}),
                ("BUY", "buy", {}),
                ("HOLD", "hold", {}),
                ("SELL", "sell", {}),
                ("Actionable", "actionable_rate", {"pct": True}),
                ("Mean conf", "mean_confidence", {"places": 3}),
                ("P90 conf", "p90_confidence", {"places": 3}),
                ("Max conf", "max_confidence", {"places": 3}),
            ],
        )
    )

    print("\n#### Stability (net)\n")
    print(
        markdown_table(
            stability[stability["cost_model"] == "net"].sort_values(["symbol", "engine"]),
            [
                ("Symbol", "symbol", {}),
                ("Engine", "engine", {}),
                ("Windows", "windows", {}),
                ("Positive", "windows_positive", {}),
                ("Won", "windows_won", {}),
                ("Mean ret", "mean_return", {"pct": True}),
                ("Median ret", "median_return", {"pct": True}),
                ("Std", "return_dispersion", {"pct": True}),
                ("Worst", "worst_window_return", {"pct": True}),
                ("Best", "best_window_return", {"pct": True}),
                ("Best share", "concentration", {"places": 2}),
            ],
        )
    )

    print("\n#### Per-window returns (net)\n")
    pivot = (
        windows[windows["cost_model"] == "net"]
        .pivot_table(index=["symbol", "fold_id"], columns="engine", values="total_return")
        .reset_index()
    )
    engines = [column for column in ENGINE_ORDER if column in pivot.columns]
    print(
        markdown_table(
            pivot,
            [("Symbol", "symbol", {}), ("Window", "fold_id", {})]
            + [(engine, engine, {"pct": True}) for engine in engines],
        )
    )

    print("\n#### Regime behaviour\n")
    print(
        markdown_table(
            regimes.sort_values(["symbol", "engine", "regime"]),
            [
                ("Symbol", "symbol", {}),
                ("Engine", "engine", {}),
                ("Regime", "regime", {}),
                ("Bars", "bars", {}),
                ("Share", "share_of_bars", {"pct": True}),
                ("Actionable", "actionable_rate", {"pct": True}),
                ("Mean score", "mean_score", {"places": 3}),
            ],
        )
    )

    print("\n#### Decision disagreement\n")
    for symbol, record in disagreement.items():
        print(f"\n**{symbol}**\n")
        for key, value in record.items():
            printed = _fmt(value, pct=True) if key.endswith("_rate") else _fmt(value)
            print(f"- {key}: {printed}")

    examples = root / "disagreement_examples.csv"
    if examples.is_file():
        print("\n#### Representative disagreements\n")
        frame = pd.read_csv(examples)
        print(
            markdown_table(
                frame,
                [
                    ("Symbol", "symbol", {}),
                    ("Timestamp", "timestamp", {}),
                    ("Pair", "pair", {}),
                    ("Left", "left_signal", {}),
                    ("Right", "right_signal", {}),
                    ("Fwd 24h", "forward_return_24h", {"pct": True}),
                    ("Favoured", "favoured", {}),
                ],
            )
        )


if __name__ == "__main__":
    main()
