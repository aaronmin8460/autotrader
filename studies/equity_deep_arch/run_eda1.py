"""EDA-1 Regime-Gated Participation: build the overlay series and evaluate it.

Predeclared in the search ledger before this module was first run. The single
router variant is fixed there (SMA 200 sessions, calm threshold −5 %, one
session of lag); the perturbation grid is robustness-only and is evaluated by
`--stage perturb` strictly after the primary result is on disk.

Usage:
    python -m studies.equity_deep_arch.run_eda1 --stage cheap
    python -m studies.equity_deep_arch.run_eda1 --stage full
    python -m studies.equity_deep_arch.run_eda1 --stage perturb
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import pandas as pd

from studies.equity_10_full import STUDY_SYMBOLS
from studies.equity_deep_arch.evaluate import (
    evaluate_challenger,
    load_region_frame,
    load_stored_series,
    write_json,
)
from studies.equity_deep_arch.overlay import participation_overlay
from studies.equity_deep_arch.state import (
    ParticipationSpec,
    participation_series,
    per_bar_participation,
    session_closes,
)

ARCHITECTURE = "EDA1_RGP"

#: The cheap first-pass sleeve set fixed in the predeclaration.
CHEAP_SYMBOLS = ("SPY", "NVDA", "META", "TSLA")

#: Robustness-only perturbations, exactly the predeclared grid.
PERTURBATIONS: tuple[tuple[str, ParticipationSpec], ...] = (
    ("sma150_calm4", ParticipationSpec(sma_sessions=150, calm_threshold=-0.04)),
    ("sma150_calm6", ParticipationSpec(sma_sessions=150, calm_threshold=-0.06)),
    ("sma250_calm4", ParticipationSpec(sma_sessions=250, calm_threshold=-0.04)),
    ("sma250_calm6", ParticipationSpec(sma_sessions=250, calm_threshold=-0.06)),
    ("sma150_calm5", ParticipationSpec(sma_sessions=150)),
    ("sma250_calm5", ParticipationSpec(sma_sessions=250)),
    ("sma200_calm4", ParticipationSpec(calm_threshold=-0.04)),
    ("sma200_calm6", ParticipationSpec(calm_threshold=-0.06)),
    ("lag2", ParticipationSpec(lag_sessions=2)),
)


def default_datasets() -> Path:
    return Path(
        os.environ.get("EQUITY_DATASETS", "/Volumes/AUTOTRADER_QA/datasets/equity-historical")
    )


def default_decisions() -> Path:
    return Path(
        os.environ.get(
            "EQUITY_DECISIONS", "/Volumes/AUTOTRADER_QA/reports/equity-10-symbol-full/decisions"
        )
    )


def default_output() -> Path:
    return Path(
        os.environ.get("EDA_OUTPUT", "/Volumes/AUTOTRADER_QA/reports/equity-deep-architecture/eda1")
    )


def build_challenger(
    datasets: Path,
    decisions: Path,
    symbols: tuple[str, ...],
    spec: ParticipationSpec,
) -> dict[str, tuple]:
    """The overlay series for every requested symbol under one router spec."""
    spy_full = pd.read_parquet(sorted(datasets.glob("SPY_15m_*session.parquet"))[0])
    closes = session_closes(spy_full)
    participation = participation_series(closes, spec)

    challenger: dict[str, tuple] = {}
    for symbol in symbols:
        frame = load_region_frame(datasets, symbol)
        by_bar = per_bar_participation(frame, participation)
        stored = load_stored_series(decisions, symbol, "V3")
        challenger[symbol] = participation_overlay(stored, by_bar, architecture=ARCHITECTURE)
    return challenger


def participation_summary(datasets: Path, spec: ParticipationSpec) -> dict[str, object]:
    """How often the router participates over the scored region, per state."""
    spy_full = pd.read_parquet(sorted(datasets.glob("SPY_15m_*session.parquet"))[0])
    closes = session_closes(spy_full)
    participation = participation_series(closes, spec)
    region = load_region_frame(datasets, "SPY")
    by_bar = per_bar_participation(region, participation)
    values = list(by_bar.values())
    return {
        "spec": spec.to_json_dict(),
        "region_bars": len(values),
        "participate_bars": int(sum(values)),
        "participate_fraction": float(sum(values) / len(values)),
    }


def run_stage(stage: str, datasets: Path, decisions: Path, output: Path) -> None:
    spec = ParticipationSpec()
    started = time.perf_counter()

    if stage == "cheap":
        challenger = build_challenger(datasets, decisions, CHEAP_SYMBOLS, spec)
        result = evaluate_challenger(
            datasets,
            decisions,
            challenger,
            label=ARCHITECTURE,
            symbols=CHEAP_SYMBOLS,
            verify_v3_wiring=False,
        )
        result["participation"] = participation_summary(datasets, spec)
        write_json(output / "cheap_screen.json", result)
    elif stage == "full":
        challenger = build_challenger(datasets, decisions, STUDY_SYMBOLS, spec)
        result = evaluate_challenger(
            datasets, decisions, challenger, label=ARCHITECTURE, symbols=STUDY_SYMBOLS
        )
        result["participation"] = participation_summary(datasets, spec)
        write_json(output / "full_evaluation.json", result)
    elif stage == "perturb":
        if not (output / "full_evaluation.json").exists():
            raise SystemExit("perturb refuses to run before the primary full evaluation exists.")
        for name, variant in PERTURBATIONS:
            target = output / f"perturb_{name}.json"
            if target.exists():
                print(f"perturb {name}: exists, skipping", flush=True)
                continue
            challenger = build_challenger(datasets, decisions, STUDY_SYMBOLS, variant)
            result = evaluate_challenger(
                datasets, decisions, challenger, label=ARCHITECTURE, symbols=STUDY_SYMBOLS
            )
            result["participation"] = participation_summary(datasets, variant)
            write_json(target, result)
            print(f"perturb {name}: done at {time.perf_counter() - started:.0f}s", flush=True)
    else:
        raise SystemExit(f"Unknown stage {stage}.")

    print(f"stage {stage} complete in {time.perf_counter() - started:.0f}s", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("cheap", "full", "perturb"))
    parser.add_argument("--datasets", type=Path, default=default_datasets())
    parser.add_argument("--decisions", type=Path, default=default_decisions())
    parser.add_argument("--output", type=Path, default=default_output())
    arguments = parser.parse_args()
    run_stage(arguments.stage, arguments.datasets, arguments.decisions, arguments.output)


if __name__ == "__main__":
    main()
