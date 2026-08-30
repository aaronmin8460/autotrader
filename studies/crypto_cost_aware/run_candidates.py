"""Replay the pre-declared candidate policies over the stored decision series.

Writes one row per (symbol, engine, policy, window) so that a candidate can be
read per window and the untouched holdout can be reported without ever having
been selected on. Deliberately cheap: it calls no engine and trains no model.

    python -m studies.crypto_cost_aware.run_candidates \
        --datasets "$AUTOTRADER_QA_DATASETS/crypto-historical" \
        --run-dir  "$AUTOTRADER_QA_REPORTS/crypto-v1-v5-historical" \
        --out      "$AUTOTRADER_QA_REPORTS/crypto-cost-aware"
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

import pandas as pd

from autotrader.research.costs import CRYPTO_COST, STRESS_COST, ZERO_COST

from .costs import breakeven_move_bps
from .policy import build_candidates
from .replay import load_decision_series, replay_candidate, summarize

#: The scoring window the completed study used, and the seven walk-forward
#: windows inside it. W07 is the untouched holdout: it is scored like the
#: others and is never used to choose anything.
SCORING_START = "2025-01-01"
WINDOWS: tuple[tuple[str, str, str], ...] = (
    ("W01", "2025-01-01", "2025-04-01"),
    ("W02", "2025-04-01", "2025-07-01"),
    ("W03", "2025-07-01", "2025-10-01"),
    ("W04", "2025-10-01", "2026-01-01"),
    ("W05", "2026-01-01", "2026-04-01"),
    ("W06", "2026-04-01", "2026-07-01"),
    ("W07", "2026-07-01", "2026-09-01"),
)
HOLDOUT = "W07"

DATASET_FILES = {
    "BTC/USD": "BTC_USD_15m_2024-01-01_2026-08-28.parquet",
    "ETH/USD": "ETH_USD_15m_2024-01-01_2026-08-28.parquet",
}
ENGINES = ("v1", "v2", "v3", "v4", "v5")
INITIAL_CASH = Decimal("100000")
VOLATILITY_BARS = 96


def run(datasets: Path, run_dir: Path, out: Path) -> pd.DataFrame:
    """Replay every candidate over the full window and each walk-forward window."""
    decisions = run_dir / "decisions_selected.parquet"
    policies = build_candidates(CRYPTO_COST)
    cost_models = {"net": CRYPTO_COST, "gross": ZERO_COST, "stress": STRESS_COST}

    rows: list[dict[str, object]] = []
    for symbol, filename in DATASET_FILES.items():
        full = pd.read_parquet(datasets / filename).sort_values("timestamp")
        full = full[full.timestamp >= SCORING_START].reset_index(drop=True)
        for engine in ENGINES:
            upstream = load_decision_series(decisions, symbol, engine, warmup_bars=0)
            spans = [("FULL", SCORING_START, "2026-09-01"), *WINDOWS]
            for window, start, end in spans:
                bars = full[(full.timestamp >= start) & (full.timestamp < end)]
                bars = bars.reset_index(drop=True)
                if bars.empty:
                    continue
                for cost_label, cost_model in cost_models.items():
                    # Only the headline cost model is run per window; the
                    # alternatives are a sensitivity check on the full span and
                    # running them per window would multiply rows without
                    # adding an answer.
                    if window != "FULL" and cost_label != "net":
                        continue
                    for label, policy in policies.items():
                        result = replay_candidate(
                            bars,
                            upstream,
                            policy,
                            cost_model=cost_model,
                            initial_cash=INITIAL_CASH,
                            volatility_bars=VOLATILITY_BARS,
                        )
                        row = summarize(result, label=label, symbol=symbol, engine=engine)
                        row["window"] = window
                        row["cost_model"] = cost_label
                        row["is_holdout"] = window == HOLDOUT
                        rows.append(row)

    frame = pd.DataFrame(rows)
    out.mkdir(parents=True, exist_ok=True)
    frame.to_csv(out / "candidate_results.csv", index=False)
    (out / "candidate_parameters.json").write_text(
        json.dumps(
            {
                "breakeven_bps": {
                    label: float(breakeven_move_bps(model)) for label, model in cost_models.items()
                },
                "policies": {
                    label: {"name": policy.name, **dict(policy.parameters)}
                    for label, policy in policies.items()
                },
                "windows": [{"name": n, "start": s, "end": e} for n, s, e in WINDOWS],
                "holdout": HOLDOUT,
                "initial_cash": str(INITIAL_CASH),
                "volatility_bars": VOLATILITY_BARS,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return frame


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    frame = run(args.datasets, args.run_dir, args.out)
    print(f"wrote {len(frame)} candidate rows to {args.out / 'candidate_results.csv'}")


if __name__ == "__main__":
    main()
