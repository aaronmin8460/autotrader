"""Phase-4 runner: regime × archetype response matrices and the §L6
character-effect test.

Usage:
    python -m studies.equity_asset_character.run_phase4
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import pandas as pd

from studies.equity_asset_character import CHARACTER_DATASETS, REPORT_ROOT
from studies.equity_asset_character.fingerprints import build_series, symbol_sessions
from studies.equity_asset_character.response import (
    HORIZON_PRIMARY,
    HORIZON_SECONDARY,
    character_effect_test,
    forward_observations,
    response_matrix,
)
from studies.equity_deep_arch.evaluate import write_json
from studies.equity_eda1_nextgen.run_phase234 import load_frame, load_universe

ASSIGNMENTS_PATH = Path(CHARACTER_DATASETS) / "archetype_assignments.parquet"
OUT_DIR = Path(REPORT_ROOT) / "phase4"

#: §L12: the explicitly named strongest-symbol candidate.
STRONGEST_SYMBOL = "NVDA"


def load_marks_regimes() -> tuple[list[date], dict[date, str], dict[date, str]]:
    rows = json.loads((Path(REPORT_ROOT) / "phase1" / "marks.json").read_text())["marks"]
    marks = [date.fromisoformat(row["mark"]) for row in rows]
    primary = {
        date.fromisoformat(row["mark"]): ("PARTICIPATE" if row["participate"] else "DEFENSIVE")
        for row in rows
    }
    descriptive = {date.fromisoformat(row["mark"]): row["spy_state"] for row in rows}
    return marks, primary, descriptive


def load_lineages() -> dict[tuple[date, str], int]:
    """(mark, symbol) → lineage id, from stored fits + assignments."""
    import numpy as np

    fits = json.loads((Path(REPORT_ROOT) / "phase3" / "fits.json").read_text())["fits"]
    # Chained nearest-centroid lineage mapping (dated amendment).
    maps: list[dict[int, int]] = []
    next_lineage = 0
    previous: list[list[float]] | None = None
    previous_map: dict[int, int] = {}
    for fit in fits:
        centroids = fit["centroids_z"]
        mapping: dict[int, int] = {}
        if previous is None:
            for label in range(len(centroids)):
                mapping[label] = next_lineage
                next_lineage += 1
        else:
            pairs = sorted(
                (
                    float(np.sum((np.array(c) - np.array(p)) ** 2)),
                    i,
                    j,
                )
                for i, c in enumerate(centroids)
                for j, p in enumerate(previous)
            )
            taken: set[int] = set()
            for _, i, j in pairs:
                if i in mapping or j in taken:
                    continue
                mapping[i] = previous_map[j]
                taken.add(j)
            for label in range(len(centroids)):
                if label not in mapping:
                    mapping[label] = next_lineage
                    next_lineage += 1
        maps.append(mapping)
        previous, previous_map = centroids, mapping

    map_by_fit_mark = {fit["fit_mark"]: mapping for fit, mapping in zip(fits, maps, strict=True)}
    stored = pd.read_parquet(ASSIGNMENTS_PATH)
    lineage_of: dict[tuple[date, str], int] = {}
    for row in stored.itertuples(index=False):
        if pd.isna(row.archetype) or row.fit_mark in ("None", "NaT"):
            continue
        mapping = map_by_fit_mark.get(str(row.fit_mark))
        if mapping is None:
            continue
        lineage_of[(date.fromisoformat(row.mark), row.symbol)] = mapping[int(row.archetype)]
    return lineage_of


def main() -> None:
    started = time.perf_counter()
    universe = load_universe("u50")
    tables = {s: symbol_sessions(load_frame(s)) for s in universe}
    spy_table = tables["SPY"]
    series = {s: build_series(t, spy_table) for s, t in tables.items()}
    marks, primary, descriptive = load_marks_regimes()
    lineage_of = load_lineages()

    payload: dict[str, object] = {"strongest_symbol": STRONGEST_SYMBOL}
    for horizon in (HORIZON_PRIMARY, HORIZON_SECONDARY):
        observations = forward_observations(series, marks, horizon)
        payload[f"matrix_primary_h{horizon}"] = response_matrix(
            observations, lineage_of, primary, horizon
        )
        payload[f"matrix_spy_state_h{horizon}"] = response_matrix(
            observations, lineage_of, descriptive, horizon
        )
        payload[f"character_test_h{horizon}"] = character_effect_test(
            observations, lineage_of, primary, horizon, strongest_symbol=STRONGEST_SYMBOL
        )
    write_json(OUT_DIR / "response.json", payload)

    test = payload[f"character_test_h{HORIZON_PRIMARY}"]
    for regime, stats in test["by_regime"].items():  # type: ignore[index]
        print(
            f"{regime}: cells={stats.get('qualified_cells')} "
            f"spread={stats.get('spread')} gate={stats.get('passes_spread_gate')} "
            f"survives={stats.get('ordering_survives_attacks')}",
            flush=True,
        )
    print(f"phase4 complete in {time.perf_counter() - started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
