"""Finalize the surviving challenger: LOSO, curves, per-year returns, repro.

Runs only for the architecture the ledger records as the sole survivor
(EDA-1). Produces:

- leave-one-symbol-out evaluation (drop NVDA, the strongest symbol for every
  engine in the ten-symbol study) for challenger / V3 / buy-and-hold;
- primary-cost portfolio equity curves for all three engines, stored as
  parquet, with per-calendar-year returns, max-drawdown duration and
  strongest-year-drop arithmetic;
- a reproducibility manifest: code SHA, dataset digests, router spec, and a
  full re-evaluation compared byte-for-byte against the stored primary
  artifact.

Usage:
    python -m studies.equity_deep_arch.run_finalize
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pandas as pd

from studies.equity_10_full import STUDY_SYMBOLS
from studies.equity_deep_arch.evaluate import (
    evaluate_challenger,
    load_region_frame,
    replay_engine,
    write_json,
)
from studies.equity_deep_arch.run_eda1 import (
    ARCHITECTURE,
    build_challenger,
    default_datasets,
    default_decisions,
    default_output,
)
from studies.equity_deep_arch.state import ParticipationSpec
from studies.equity_v1_v5.adapters import DecisionSeriesEngine
from studies.equity_v1_v5.dataset import frame_digest
from studies.equity_v1_v5.scoring import COST_MODELS

OUTPUT = Path("/Volumes/AUTOTRADER_QA/reports/equity-deep-architecture/finalize")

LOSO_DROP = "NVDA"


def primary_cost_model():
    for model in COST_MODELS:
        if model.label == "equity-marketable":
            return model
    raise RuntimeError("primary cost model missing")


def curve_frame(result) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": list(result.timestamps),
            "equity": [float(value) for value in result.equity_curve],
        }
    )


def yearly_returns(curve: pd.DataFrame) -> dict[str, float]:
    indexed = curve.set_index("timestamp")["equity"]
    out: dict[str, float] = {}
    previous = None
    for year, chunk in indexed.groupby(indexed.index.year):
        start = indexed.iloc[0] if previous is None else previous
        out[str(year)] = float(chunk.iloc[-1] / start - 1)
        previous = chunk.iloc[-1]
    return out


def drawdown_duration_bars(curve: pd.DataFrame) -> int:
    equity = curve["equity"]
    longest = 0
    current = 0
    peak = equity.iloc[0]
    for value in equity:
        if value >= peak:
            peak = value
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return int(longest)


def main() -> None:
    datasets = default_datasets()
    decisions = default_decisions()
    started = time.perf_counter()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    spec = ParticipationSpec()

    # 1. LOSO: drop the strongest symbol.
    loso_symbols = tuple(s for s in STUDY_SYMBOLS if s != LOSO_DROP)
    challenger = build_challenger(datasets, decisions, loso_symbols, spec)
    loso = evaluate_challenger(
        datasets,
        decisions,
        challenger,
        label=ARCHITECTURE,
        symbols=loso_symbols,
        verify_v3_wiring=False,
    )
    write_json(OUTPUT / "loso_evaluation.json", loso)
    print("LOSO done", flush=True)

    # 2. Curves and per-year figures, primary cost, full universe.
    full = build_challenger(datasets, decisions, STUDY_SYMBOLS, spec)
    frames = {symbol: load_region_frame(datasets, symbol) for symbol in STUDY_SYMBOLS}
    cost = primary_cost_model()
    from studies.equity_10_full.benchmarks import BuyAndHoldEngine
    from studies.equity_deep_arch.evaluate import load_stored_series

    engines = {
        ARCHITECTURE: DecisionSeriesEngine(
            [r for s in STUDY_SYMBOLS for r in full[s]],
            name=ARCHITECTURE,
            version="eda",
            warmup_bars=0,
        ),
        "V3": DecisionSeriesEngine(
            [r for s in STUDY_SYMBOLS for r in load_stored_series(decisions, s, "V3")],
            name="V3",
            version="v3",
            warmup_bars=0,
        ),
        "BUY_AND_HOLD": BuyAndHoldEngine(),
    }
    yearly: dict[str, object] = {}
    durations: dict[str, int] = {}
    for name, engine in engines.items():
        replayed = replay_engine(frames, engine, cost)
        curve = curve_frame(replayed)
        curve.to_parquet(OUTPUT / f"curve_{name}.parquet", engine="pyarrow", index=False)
        yearly[name] = yearly_returns(curve)
        durations[name] = drawdown_duration_bars(curve)
        print(f"curve {name}: {yearly[name]} duration {durations[name]}", flush=True)

    def drop_year(returns: dict[str, float], year: str) -> float:
        total = 1.0
        for key, value in returns.items():
            if key != year:
                total *= 1 + value
        return total - 1

    challenger_years = yearly[ARCHITECTURE]
    strongest = max(challenger_years, key=lambda y: challenger_years[y])
    year_drop = {
        "strongest_challenger_year": strongest,
        "challenger_without_it": drop_year(challenger_years, strongest),
        "v3_without_it": drop_year(yearly["V3"], strongest),
        "bh_without_it": drop_year(yearly["BUY_AND_HOLD"], strongest),
    }
    write_json(
        OUTPUT / "yearly_and_duration.json",
        {
            "yearly_returns": yearly,
            "max_drawdown_duration_bars": durations,
            "strongest_year_drop": year_drop,
        },
    )

    # 3. Reproducibility: full re-evaluation must reproduce the stored artifact.
    rerun = evaluate_challenger(
        datasets, decisions, full, label=ARCHITECTURE, symbols=STUDY_SYMBOLS
    )
    stored = json.loads((default_output() / "full_evaluation.json").read_text())
    stored.pop("participation", None)
    rerun_serialized = json.loads(json.dumps(rerun, sort_keys=True, default=str))
    identical = rerun_serialized == stored
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
        check=True,
    ).stdout.strip()
    digests = {}
    for symbol in STUDY_SYMBOLS:
        frame = pd.read_parquet(sorted(datasets.glob(f"{symbol}_15m_*session.parquet"))[0])
        digests[symbol] = frame_digest(frame)
    write_json(
        OUTPUT / "reproducibility.json",
        {
            "code_sha": sha,
            "router_spec": spec.to_json_dict(),
            "dataset_digests": digests,
            "full_reevaluation_identical": identical,
            "elapsed_seconds": time.perf_counter() - started,
        },
    )
    print(f"reproducibility identical={identical} sha={sha[:12]}", flush=True)


if __name__ == "__main__":
    main()
