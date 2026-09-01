"""Phase 9/12 runner: robustness attacks on the surviving challenger and the
final tournament assembly (ledger §L11, §L12).

Usage:
    python -m studies.equity_asset_character.run_phase9 --stage attacks --universe u30
    python -m studies.equity_asset_character.run_phase9 --stage loso --universe u30
    python -m studies.equity_asset_character.run_phase9 --stage window-perturb --window 105
    python -m studies.equity_asset_character.run_phase9 --stage year-window
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

from studies.equity_asset_character import REPORT_ROOT
from studies.equity_asset_character.run_phase4 import load_lineages
from studies.equity_asset_character.run_phase5 import TiltContext
from studies.equity_deep_arch.evaluate import write_json

OUT_DIR = Path(REPORT_ROOT) / "phase9"

STRONGEST_SYMBOL = "NVDA"

#: The frozen calendar-year convention inherited from the prior program:
#: 2024 ≈ windows w06–w08.
YEAR_2024_WINDOWS = ("w06", "w07", "w08")


def strongest_archetype_members(min_share: float = 0.5) -> list[str]:
    """Symbols spending ≥ half their assigned marks in the strongest
    (high-beta) lineage — the §L12 archetype-removal set."""
    lineage_of = load_lineages()
    counts: dict[str, Counter] = {}
    for (_mark, symbol), lineage in lineage_of.items():
        counts.setdefault(symbol, Counter())[lineage] += 1
    members = []
    for symbol, counter in counts.items():
        total = sum(counter.values())
        if total and counter.get(2, 0) / total >= min_share:
            members.append(symbol)
    return sorted(members)


def run_attacks(universe_name: str, scheme: str) -> None:
    context = TiltContext(universe_name)
    arch_members = strongest_archetype_members()
    payload: dict[str, object] = {
        "universe": context.context.universe,
        "scheme": scheme,
        "strongest_archetype_members": arch_members,
    }

    jobs: list[tuple[str, dict]] = [
        ("equal_ex_nvda", {"scheme": "EQUAL", "exclude_symbols": frozenset({STRONGEST_SYMBOL})}),
        (f"{scheme}_ex_nvda", {"scheme": scheme, "exclude_symbols": frozenset({STRONGEST_SYMBOL})}),
        ("equal_ex_archetype", {"scheme": "EQUAL", "exclude_symbols": frozenset(arch_members)}),
        (f"{scheme}_ex_archetype", {"scheme": scheme, "exclude_symbols": frozenset(arch_members)}),
        (f"{scheme}_delay1", {"scheme": scheme, "delay_one_session": True}),
        (f"{scheme}_clip_tight", {"scheme": scheme, "clip_override": (0.75, 1.25)}),
        (f"{scheme}_clip_wide", {"scheme": scheme, "clip_override": (0.5, 1.5)}),
    ]
    for label, kwargs in jobs:
        started = time.perf_counter()
        scheme_arg = kwargs.pop("scheme")
        payload[label] = context.evaluate(label, scheme_arg, **kwargs)
        print(f"{label}: done in {time.perf_counter() - started:.0f}s", flush=True)

    write_json(OUT_DIR / f"attacks_{universe_name}_{scheme}.json", payload)


def run_loso(universe_name: str, scheme: str) -> None:
    context = TiltContext(universe_name)
    payload: dict[str, object] = {"universe": context.context.universe, "scheme": scheme}
    results: dict[str, object] = {}
    for symbol in context.context.universe:
        started = time.perf_counter()
        block = context.evaluate(
            f"{scheme}_ex_{symbol}", scheme, exclude_symbols=frozenset({symbol})
        )
        primary = block["equity-marketable"]
        results[symbol] = {
            "net_return": primary["net_return"],
            "sharpe": float(primary["metrics"]["sharpe_ratio"]),
            "max_drawdown": float(primary["metrics"]["max_drawdown"]),
        }
        print(
            f"LOSO {symbol}: net {primary['net_return']:+.4f} "
            f"({time.perf_counter() - started:.0f}s)",
            flush=True,
        )
    payload["loso"] = results
    write_json(OUT_DIR / f"loso_{universe_name}_{scheme}.json", payload)


def run_window_perturb(window: int, universe_name: str, scheme: str) -> None:
    """§L12 archetype-label perturbation: structural 126-window → 105/147.

    Rebuilds the structural panel with the patched window, refits the §L5
    schedule, and replays the challenger with the perturbed labels. The
    surviving-feature list and every other constant stay fixed.
    """
    import studies.equity_asset_character.fingerprints as fp
    from studies.equity_asset_character.archetypes import fit_archetypes, fit_dates
    from studies.equity_asset_character.fingerprints import (
        cross_sectional_z,
        fingerprint_panel,
        symbol_sessions,
    )
    from studies.equity_asset_character.run_phase5 import surviving_features
    from studies.equity_eda1_nextgen.run_phase234 import (
        load_frame,
        load_universe,
        region_sessions_of,
    )
    from studies.equity_eda1_nextgen.selection import rebalance_sessions

    original = fp.WINDOW_6M
    fp.WINDOW_6M = window
    try:
        universe = load_universe("u50")
        frames = {s: load_frame(s) for s in universe}
        tables = {s: symbol_sessions(f) for s, f in frames.items()}
        marks = rebalance_sessions(region_sessions_of(frames["SPY"]))
        panel = fingerprint_panel(tables, marks)
    finally:
        fp.WINDOW_6M = original

    features = surviving_features()
    z_panel = cross_sectional_z(panel, features)
    fits = []
    for fit_mark in fit_dates(list(marks)):
        fit = fit_archetypes(z_panel, features, fit_mark, list(marks))
        train_marks = [m for m in marks if m < fit_mark]
        members: dict[int, list[str]] = {}
        for symbol, label in zip(fit.symbols, fit.labels, strict=True):
            members.setdefault(int(label), []).append(symbol)
        raw_medians = {}
        for label, group in sorted(members.items()):
            block = panel.loc[(slice(None), group), list(features)].loc[list(train_marks)]
            raw_medians[str(label)] = {f: float(block[f].median()) for f in features}
        fits.append(
            {
                "fit_mark": fit.fit_mark.isoformat(),
                "k": fit.k,
                "centroids_z": [list(row) for row in fit.centroids],
                "features": list(features),
                "raw_feature_medians": raw_medians,
            }
        )
        print(f"perturbed fit {fit.fit_mark}: k={fit.k}", flush=True)

    context = TiltContext(universe_name, fit_records=fits, z_structural=z_panel)
    payload = {
        "window": window,
        "fits": [{k: v for k, v in f.items() if k != "centroids_z"} for f in fits],
        scheme: context.evaluate(f"{scheme}_w{window}", scheme),
    }
    write_json(OUT_DIR / f"perturb_window{window}_{universe_name}_{scheme}.json", payload)


def _windows_net(window_returns: dict[str, float], drop: tuple[str, ...]) -> float:
    net = 1.0
    for name, value in window_returns.items():
        if name not in drop:
            net *= 1.0 + value
    return net - 1.0


def run_year_window(universe_name: str, scheme: str) -> None:
    """Strongest-window and strongest-year (w06–w08) removal, from stored
    window returns of the challenger, its control, and the U10 bridge."""
    a1 = json.loads((Path(REPORT_ROOT) / "phase5" / f"a1_{universe_name}.json").read_text())
    control = json.loads(
        (Path(REPORT_ROOT) / "baseline" / f"base_{universe_name}.json").read_text()
    )["ALL_ELIGIBLE"]
    bridge = json.loads((Path(REPORT_ROOT) / "baseline" / "bridge_u10.json").read_text())[
        "EDA1_weighted_bridge"
    ]

    payload: dict[str, object] = {}
    for name, block in (
        (scheme, a1[scheme]["equity-marketable"]),
        ("control", control["equity-marketable"]),
        ("bridge_u10", bridge["equity-marketable"]),
    ):
        returns = block["window_returns"]
        strongest = max(returns, key=lambda w: returns[w])
        payload[name] = {
            "net": block["net_return"],
            "strongest_window": strongest,
            "net_drop_strongest_window": _windows_net(returns, (strongest,)),
            "net_drop_2024": _windows_net(returns, YEAR_2024_WINDOWS),
        }
    write_json(OUT_DIR / f"year_window_{universe_name}_{scheme}.json", payload)
    for name, stats in payload.items():
        print(name, stats, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", required=True, choices=("attacks", "loso", "window-perturb", "year-window")
    )
    parser.add_argument("--universe", default="u30")
    parser.add_argument("--scheme", default="A1_B")
    parser.add_argument("--window", type=int, default=105)
    arguments = parser.parse_args()
    started = time.perf_counter()
    if arguments.stage == "attacks":
        run_attacks(arguments.universe, arguments.scheme)
    elif arguments.stage == "loso":
        run_loso(arguments.universe, arguments.scheme)
    elif arguments.stage == "window-perturb":
        run_window_perturb(arguments.window, arguments.universe, arguments.scheme)
    else:
        run_year_window(arguments.universe, arguments.scheme)
    print(f"stage {arguments.stage} complete in {time.perf_counter() - started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
