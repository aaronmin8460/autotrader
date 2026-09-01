"""Phase 10/11 runner: per-asset and per-archetype diagnostics (§ program
prompt phases 10–11). Weight math only — no replays — plus stored attack and
LOSO artifacts where they exist.

Usage:
    python -m studies.equity_asset_character.run_phase10 --universe u30 --scheme A1_B
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date
from pathlib import Path

import numpy as np

from studies.equity_asset_character import REPORT_ROOT
from studies.equity_asset_character.allocation import (
    archetype_multipliers,
    governing_fit,
    load_fit_records,
    retro_labels,
    tilted_weights,
)
from studies.equity_asset_character.fingerprints import cross_sectional_z
from studies.equity_asset_character.run_phase2 import load_panel
from studies.equity_asset_character.run_phase4 import load_lineages, load_marks_regimes
from studies.equity_asset_character.run_phase5 import surviving_features
from studies.equity_deep_arch.evaluate import write_json
from studies.equity_eda1_nextgen.run_phase234 import load_universe

OUT_DIR = Path(REPORT_ROOT) / "phase10"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default="u30")
    parser.add_argument("--scheme", default="A1_B")
    arguments = parser.parse_args()
    started = time.perf_counter()

    universe = sorted(
        load_universe(arguments.universe)
        if arguments.universe != "u10"
        else ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"]
    )
    marks, regime_of, _ = load_marks_regimes()
    panel = load_panel()
    z_panel = cross_sectional_z(panel, surviving_features())
    fit_records = load_fit_records()
    lineage_of = load_lineages()

    weight_sum: dict[str, float] = dict.fromkeys(universe, 0.0)
    base_sum: dict[str, float] = dict.fromkeys(universe, 0.0)
    churn: dict[str, float] = dict.fromkeys(universe, 0.0)
    previous_weights: dict[str, float] | None = None
    weight_share_by_lineage: dict[int, float] = {}
    counted_marks = 0

    for mark in marks:
        fit = governing_fit(fit_records, mark)
        if fit is None:
            continue
        labels = retro_labels(fit, z_panel, mark)
        mults = archetype_multipliers(arguments.scheme, fit, "PARTICIPATE", {})
        multiplier_of = {
            s: mults.get(labels[s], 1.0) if s in labels else 1.0 for s in universe
        }
        weights = tilted_weights(universe, multiplier_of)
        equal = tilted_weights(universe, {})
        counted_marks += 1
        for symbol in universe:
            weight_sum[symbol] += weights[symbol]
            base_sum[symbol] += equal[symbol]
            if previous_weights is not None:
                churn[symbol] += abs(weights[symbol] - previous_weights[symbol])
        for symbol in universe:
            lineage = lineage_of.get((mark, symbol))
            if lineage is not None:
                weight_share_by_lineage[lineage] = (
                    weight_share_by_lineage.get(lineage, 0.0) + weights[symbol]
                )
        previous_weights = weights

    loso_path = Path(REPORT_ROOT) / "phase9" / f"loso_{arguments.universe}_{arguments.scheme}.json"
    loso = (
        json.loads(loso_path.read_text())["loso"] if loso_path.exists() else {}
    )
    a1 = json.loads(
        (Path(REPORT_ROOT) / "phase5" / f"a1_{arguments.universe}.json").read_text()
    )
    full_net = a1[arguments.scheme]["equity-marketable"]["net_return"]

    per_symbol: dict[str, object] = {}
    for symbol in universe:
        shares: dict[str, float] = {}
        total = 0
        for mark in marks:
            lineage = lineage_of.get((mark, symbol))
            if lineage is not None:
                shares[str(lineage)] = shares.get(str(lineage), 0.0) + 1
                total += 1
        per_symbol[symbol] = {
            "lineage_shares": {k: v / total for k, v in sorted(shares.items())} if total else {},
            "mean_weight": weight_sum[symbol] / counted_marks,
            "mean_weight_equal": base_sum[symbol] / counted_marks,
            "weight_ratio_vs_equal": (
                weight_sum[symbol] / base_sum[symbol] if base_sum[symbol] else float("nan")
            ),
            "mark_churn": churn[symbol] / max(counted_marks - 1, 1),
            "loso_contribution_net_pts": (
                (full_net - loso[symbol]["net_return"]) * 100 if symbol in loso else None
            ),
        }

    lineage_total = sum(weight_share_by_lineage.values())
    payload = {
        "universe": universe,
        "scheme": arguments.scheme,
        "counted_marks": counted_marks,
        "per_symbol": per_symbol,
        "phase11_lineage_weight_share": {
            str(k): v / lineage_total for k, v in sorted(weight_share_by_lineage.items())
        },
        "phase11_lineage_member_counts_by_year": lineage_member_counts(lineage_of, marks),
    }
    write_json(OUT_DIR / f"diagnostics_{arguments.universe}_{arguments.scheme}.json", payload)
    print(f"phase10/11 complete in {time.perf_counter() - started:.0f}s", flush=True)


def lineage_member_counts(
    lineage_of: dict[tuple[date, str], int], marks: list[date]
) -> dict[str, dict[str, float]]:
    """Mean member count per lineage, by calendar year (membership drift)."""
    by_year: dict[str, dict[str, list[int]]] = {}
    for mark in marks:
        counts: dict[int, int] = {}
        for (m, _symbol), lineage in lineage_of.items():
            if m == mark:
                counts[lineage] = counts.get(lineage, 0) + 1
        if not counts:
            continue
        year = str(mark.year)
        for lineage, count in counts.items():
            by_year.setdefault(year, {}).setdefault(str(lineage), []).append(count)
    return {
        year: {lineage: float(np.mean(values)) for lineage, values in sorted(counts.items())}
        for year, counts in sorted(by_year.items())
    }


if __name__ == "__main__":
    main()
