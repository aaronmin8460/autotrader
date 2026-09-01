"""Phase-3 runner: walk-forward archetype discovery on U45 (§L5).

Fits the frozen archetype schedule on the Phase-2 surviving structural
features, writes per-fit records (k, silhouettes, Ward agreement, members,
raw-feature medians for interpretability), causal per-mark assignments, and
the §L5 stability measurements.

Usage:
    python -m studies.equity_asset_character.run_phase3
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import numpy as np

from studies.equity_asset_character import CHARACTER_DATASETS, REPORT_ROOT
from studies.equity_asset_character.archetypes import (
    adjusted_rand,
    assignments_over_marks,
    fit_archetypes,
    fit_dates,
)
from studies.equity_asset_character.fingerprints import cross_sectional_z
from studies.equity_asset_character.run_phase2 import load_panel
from studies.equity_deep_arch.evaluate import write_json

ASSIGNMENTS_PATH = Path(CHARACTER_DATASETS) / "archetype_assignments.parquet"
FITS_PATH = Path(REPORT_ROOT) / "phase3" / "fits.json"
STABILITY_PATH = Path(REPORT_ROOT) / "phase3" / "archetype_stability.json"


def surviving_features() -> tuple[str, ...]:
    stability = json.loads((Path(REPORT_ROOT) / "phase2" / "stability.json").read_text())
    return tuple(stability["surviving_structural_features"])


def match_clusters(previous_centroids: np.ndarray, current_centroids: np.ndarray) -> dict[int, int]:
    """Greedy nearest-centroid mapping current → previous (for cross-fit
    turnover accounting only; never used in allocation)."""
    mapping: dict[int, int] = {}
    taken: set[int] = set()
    pairs = sorted(
        (
            (float(((c - p) ** 2).sum()), i, j)
            for i, c in enumerate(current_centroids)
            for j, p in enumerate(previous_centroids)
        ),
    )
    for _, i, j in pairs:
        if i in mapping or j in taken:
            continue
        mapping[i] = j
        taken.add(j)
    for i in range(len(current_centroids)):
        mapping.setdefault(i, i)
    return mapping


def main() -> None:
    started = time.perf_counter()
    panel = load_panel()
    features = surviving_features()
    marks = sorted(panel.index.get_level_values("mark").unique())
    z_panel = cross_sectional_z(panel, features)

    schedule = fit_dates(list(marks))
    fits = []
    fit_records = []
    for fit_mark in schedule:
        fit = fit_archetypes(z_panel, features, fit_mark, list(marks))
        fits.append(fit)
        members: dict[int, list[str]] = {}
        for symbol, label in zip(fit.symbols, fit.labels, strict=True):
            members.setdefault(int(label), []).append(symbol)
        train_marks = [m for m in marks if m < fit_mark]
        raw_medians = {}
        for label, group in sorted(members.items()):
            block = panel.loc[(slice(None), group), list(features)].loc[[m for m in train_marks]]
            raw_medians[str(label)] = {
                feature: float(block[feature].median()) for feature in features
            }
        fit_records.append(
            {
                "fit_mark": fit.fit_mark.isoformat(),
                "k": fit.k,
                "silhouette_by_k": fit.silhouette_by_k,
                "ward_agreement": fit.ward_agreement,
                "singleton_note": fit.singleton_note,
                "members": {str(k): v for k, v in sorted(members.items())},
                "cluster_sizes": {str(k): len(v) for k, v in sorted(members.items())},
                "centroids_z": [list(row) for row in fit.centroids],
                "raw_feature_medians": raw_medians,
                "features": list(features),
            }
        )
        print(
            f"fit {fit_mark}: k={fit.k} sil={fit.silhouette_by_k} "
            f"ward_ari={fit.ward_agreement:.3f} sizes="
            f"{[len(v) for _, v in sorted(members.items())]} {fit.singleton_note}",
            flush=True,
        )

    table = assignments_over_marks(z_panel, fits, list(marks))
    stored = table.reset_index()
    stored["mark"] = stored["mark"].astype(str)
    stored["fit_mark"] = stored["fit_mark"].astype(str)
    ASSIGNMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    stored.to_parquet(ASSIGNMENTS_PATH, engine="pyarrow", index=False)

    # §L5 stability: month-over-month label retention, cross-fit boundaries
    # bridged by nearest-centroid mapping.
    fit_by_mark: dict[date, int] = {}
    ordered_fits = sorted(fits, key=lambda fit: fit.fit_mark)
    for mark in marks:
        governing = None
        for index, fit in enumerate(ordered_fits):
            if fit.fit_mark <= mark:
                governing = index
        if governing is not None:
            fit_by_mark[mark] = governing

    retention: list[float] = []
    boundary_records = []
    assigned_marks = [m for m in marks if m in fit_by_mark]
    for previous, current in zip(assigned_marks[:-1], assigned_marks[1:], strict=False):
        prev_labels = table.loc[previous]["archetype"].dropna()
        curr_labels = table.loc[current]["archetype"].dropna()
        common = prev_labels.index.intersection(curr_labels.index)
        if len(common) < 10:
            continue
        prev_of = prev_labels[common].astype(int)
        curr_of = curr_labels[common].astype(int)
        if fit_by_mark[previous] != fit_by_mark[current]:
            mapping = match_clusters(
                np.asarray(ordered_fits[fit_by_mark[previous]].centroids),
                np.asarray(ordered_fits[fit_by_mark[current]].centroids),
            )
            curr_of = curr_of.map(lambda label, mapping=mapping: mapping[int(label)])
            boundary_records.append(
                {
                    "boundary": current.isoformat(),
                    "adjusted_rand": adjusted_rand(prev_of.to_numpy(), curr_of.to_numpy()),
                }
            )
        retention.append(float((prev_of == curr_of).mean()))

    payload = {
        "median_month_over_month_retention": float(np.median(retention)),
        "min_month_over_month_retention": float(np.min(retention)),
        "retention_series_length": len(retention),
        "fit_boundaries": boundary_records,
        "gate": "median month-over-month membership stability >= 0.80",
        "stable": bool(np.median(retention) >= 0.80),
    }
    write_json(FITS_PATH, {"fits": fit_records})
    write_json(STABILITY_PATH, payload)
    print(
        f"stability: median retention {payload['median_month_over_month_retention']:.3f} "
        f"min {payload['min_month_over_month_retention']:.3f} stable={payload['stable']}",
        flush=True,
    )
    print(f"phase3 complete in {time.perf_counter() - started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
