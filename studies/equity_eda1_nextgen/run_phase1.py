"""Phase-1 runner: pullback/whipsaw refinements (ledger §L2).

Variants are exactly the ledger's predeclarations. `--stage wiring` proves the
generalized machine reduces to the incumbent rule before any variant runs;
`--stage variants` evaluates the seven predeclared refinements; `--stage lite`
runs the D2-0 diagnostic.

Usage:
    python -m studies.equity_eda1_nextgen.run_phase1 --stage wiring
    python -m studies.equity_eda1_nextgen.run_phase1 --stage variants
    python -m studies.equity_eda1_nextgen.run_phase1 --stage lite
"""

from __future__ import annotations

import argparse
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
from studies.equity_deep_arch.run_eda1 import default_datasets, default_decisions
from studies.equity_deep_arch.state import (
    ParticipationSpec,
    participation_series,
    per_bar_participation,
    session_closes,
)
from studies.equity_eda1_nextgen import REPORT_ROOT
from studies.equity_eda1_nextgen.overlays import freeze_overlay, lite_overlay
from studies.equity_eda1_nextgen.refined_states import (
    FreezeSpec,
    RefinedSpec,
    freeze_state_series,
    refined_participation_series,
    state_flip_count,
)

#: The seven predeclared Phase-1 variants (ledger §L2), exactly.
VARIANTS: tuple[tuple[str, object], ...] = (
    ("A1_dd_band", RefinedSpec(enter_dd=-0.04, exit_dd=-0.06)),
    ("A2_trend_band", RefinedSpec(exit_sma_ratio=0.98)),
    ("A3_both_bands", RefinedSpec(enter_dd=-0.04, exit_dd=-0.06, exit_sma_ratio=0.98)),
    ("B1_enter2_exit1", RefinedSpec(k_enter=2, k_exit=1)),
    ("B2_enter1_exit2", RefinedSpec(k_enter=1, k_exit=2)),
    ("B3_enter2_exit2", RefinedSpec(k_enter=2, k_exit=2)),
    ("C1_freeze", FreezeSpec()),
)

#: Winner-only robustness grid (ledger §L2 criterion 7): built by
#: `perturbations_for` from the winning variant's spec, never widened.


def _spy_closes(datasets: Path) -> pd.DataFrame:
    spy_full = pd.read_parquet(sorted(datasets.glob("SPY_15m_*session.parquet"))[0])
    return session_closes(spy_full)


def build_series_maps(datasets: Path, name: str, spec: object) -> tuple[dict, int]:
    """Per-bar state map for one variant, plus its session-level flip count."""
    closes = _spy_closes(datasets)
    if isinstance(spec, RefinedSpec):
        series = refined_participation_series(closes, spec)
        flips = state_flip_count(series, "participate")
        column = "participate"
    elif isinstance(spec, FreezeSpec):
        series = freeze_state_series(closes, spec)
        flips = state_flip_count(series, "state")
        column = "state"
    else:
        raise SystemExit(f"Unknown spec type for {name}.")
    by_session = {row["session"]: row[column] for _, row in series.iterrows()}
    return by_session, flips


def build_challenger(
    datasets: Path,
    decisions: Path,
    symbols: tuple[str, ...],
    name: str,
    spec: object,
) -> tuple[dict[str, tuple], int]:
    from autotrader.equity.session import market_date

    by_session, flips = build_series_maps(datasets, name, spec)

    challenger: dict[str, tuple] = {}
    for symbol in symbols:
        frame = load_region_frame(datasets, symbol)
        stored = load_stored_series(decisions, symbol, "V3")
        by_bar = {}
        for ts in frame["timestamp"]:
            day = market_date(ts.to_pydatetime())
            if day not in by_session:
                raise SystemExit(f"No state for session {day} (bar {ts}).")
            by_bar[pd.Timestamp(ts)] = by_session[day]
        if isinstance(spec, RefinedSpec):
            challenger[symbol] = participation_overlay(
                stored, by_bar, architecture=f"P1_{name}"
            )
        else:
            challenger[symbol] = freeze_overlay(stored, by_bar, architecture=f"P1_{name}")
    return challenger, flips


def run_wiring(datasets: Path, output: Path) -> None:
    """The generalized machine with incumbent parameters must reproduce the
    incumbent participation series session-for-session."""
    closes = _spy_closes(datasets)
    incumbent = participation_series(closes, ParticipationSpec())
    reduced = refined_participation_series(closes, RefinedSpec())
    mismatches = int(
        (incumbent["participate"].to_numpy() != reduced["participate"].to_numpy()).sum()
    )
    payload = {
        "sessions": int(len(incumbent)),
        "mismatched_sessions": mismatches,
        "incumbent_flips": state_flip_count(incumbent, "participate"),
        "wiring_check": "PASS" if mismatches == 0 else "FAIL",
    }
    write_json(output / "phase1_wiring.json", payload)
    if mismatches:
        raise SystemExit(f"Phase-1 wiring check FAILED: {mismatches} mismatched sessions.")
    print("phase1 wiring: PASS", flush=True)


def run_variants(datasets: Path, decisions: Path, output: Path) -> None:
    for name, spec in VARIANTS:
        target = output / f"phase1_{name}.json"
        if target.exists():
            print(f"{name}: exists, skipping", flush=True)
            continue
        started = time.perf_counter()
        challenger, flips = build_challenger(datasets, decisions, STUDY_SYMBOLS, name, spec)
        result = evaluate_challenger(
            datasets, decisions, challenger, label=f"P1_{name}", symbols=STUDY_SYMBOLS
        )
        result["spec"] = spec.to_json_dict()
        result["session_state_flips"] = flips
        write_json(target, result)
        print(f"{name}: done in {time.perf_counter() - started:.0f}s", flush=True)


def run_lite(datasets: Path, decisions: Path, output: Path) -> None:
    target = output / "d2_0_lite.json"
    if target.exists():
        print("lite: exists, skipping", flush=True)
        return
    from autotrader.equity.session import market_date

    closes = _spy_closes(datasets)
    participation = participation_series(closes, ParticipationSpec())
    by_session = {row["session"]: bool(row["participate"]) for _, row in participation.iterrows()}

    challenger: dict[str, tuple] = {}
    for symbol in STUDY_SYMBOLS:
        frame = load_region_frame(datasets, symbol)
        stored = load_stored_series(decisions, symbol, "V3")
        by_bar = {
            pd.Timestamp(ts): by_session[market_date(ts.to_pydatetime())]
            for ts in frame["timestamp"]
        }
        challenger[symbol] = lite_overlay(stored, by_bar, architecture="D2_0_LITE")
    result = evaluate_challenger(
        datasets, decisions, challenger, label="D2_0_LITE", symbols=STUDY_SYMBOLS
    )
    write_json(target, result)
    print("lite: done", flush=True)


def perturbations_for(name: str, spec: object) -> tuple[tuple[str, object], ...]:
    """The winner-only robustness grid: SMA {150,250}, boundaries ±1 pt, lag 2."""
    if isinstance(spec, RefinedSpec):
        return (
            ("sma150", RefinedSpec(**{**spec.to_json_dict(), "sma_sessions": 150})),
            ("sma250", RefinedSpec(**{**spec.to_json_dict(), "sma_sessions": 250})),
            (
                "band_tighter",
                RefinedSpec(
                    **{
                        **spec.to_json_dict(),
                        "enter_dd": spec.enter_dd + 0.01,
                        "exit_dd": spec.exit_dd + 0.01,
                    }
                ),
            ),
            (
                "band_looser",
                RefinedSpec(
                    **{
                        **spec.to_json_dict(),
                        "enter_dd": spec.enter_dd - 0.01,
                        "exit_dd": spec.exit_dd - 0.01,
                    }
                ),
            ),
            ("lag2", RefinedSpec(**{**spec.to_json_dict(), "lag_sessions": 2})),
        )
    if isinstance(spec, FreezeSpec):
        return (
            ("sma150", FreezeSpec(**{**spec.to_json_dict(), "sma_sessions": 150})),
            ("sma250", FreezeSpec(**{**spec.to_json_dict(), "sma_sessions": 250})),
            (
                "dd9",
                FreezeSpec(**{**spec.to_json_dict(), "drawdown_threshold": -0.09}),
            ),
            (
                "dd11",
                FreezeSpec(**{**spec.to_json_dict(), "drawdown_threshold": -0.11}),
            ),
            ("lag2", FreezeSpec(**{**spec.to_json_dict(), "lag_sessions": 2})),
        )
    raise SystemExit(f"Unknown spec type for {name}.")


def run_perturb(datasets: Path, decisions: Path, output: Path, winner: str) -> None:
    matches = [entry for entry in VARIANTS if entry[0] == winner]
    if not matches:
        raise SystemExit(f"Unknown winner {winner}.")
    if not (output / f"phase1_{winner}.json").exists():
        raise SystemExit("perturb refuses to run before the winner's primary result exists.")
    name, spec = matches[0]
    for pname, pspec in perturbations_for(name, spec):
        target = output / f"phase1_{name}_perturb_{pname}.json"
        if target.exists():
            print(f"perturb {pname}: exists, skipping", flush=True)
            continue
        challenger, flips = build_challenger(
            datasets, decisions, STUDY_SYMBOLS, f"{name}_{pname}", pspec
        )
        result = evaluate_challenger(
            datasets, decisions, challenger, label=f"P1_{name}_{pname}", symbols=STUDY_SYMBOLS
        )
        result["spec"] = pspec.to_json_dict()
        result["session_state_flips"] = flips
        write_json(target, result)
        print(f"perturb {pname}: done", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", required=True, choices=("wiring", "variants", "lite", "perturb")
    )
    parser.add_argument("--winner", default=None)
    parser.add_argument("--datasets", type=Path, default=default_datasets())
    parser.add_argument("--decisions", type=Path, default=default_decisions())
    parser.add_argument("--output", type=Path, default=Path(REPORT_ROOT) / "phase1")
    arguments = parser.parse_args()

    if arguments.stage == "wiring":
        run_wiring(arguments.datasets, arguments.output)
    elif arguments.stage == "variants":
        run_variants(arguments.datasets, arguments.decisions, arguments.output)
    elif arguments.stage == "lite":
        run_lite(arguments.datasets, arguments.decisions, arguments.output)
    elif arguments.stage == "perturb":
        if not arguments.winner:
            raise SystemExit("--winner is required for perturb.")
        run_perturb(arguments.datasets, arguments.decisions, arguments.output, arguments.winner)


if __name__ == "__main__":
    main()
