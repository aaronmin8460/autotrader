"""Phase-2 runner: fingerprint stability and the structural gate (§L4).

Reads the Phase-1 panel, measures per-feature rank stability, applies the
predeclared structural gate (median lag-6 Spearman ≥ 0.50), and records the
surviving structural feature set that Phase 3 clustering may use.

Usage:
    python -m studies.equity_asset_character.run_phase2
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import pandas as pd

from studies.equity_asset_character import CHARACTER_DATASETS, REPORT_ROOT
from studies.equity_asset_character.fingerprints import STATE_FEATURES, STRUCTURAL_FEATURES
from studies.equity_asset_character.stability import rank_stability
from studies.equity_deep_arch.evaluate import write_json

PANEL_PATH = Path(CHARACTER_DATASETS) / "fingerprints.parquet"
OUT_PATH = Path(REPORT_ROOT) / "phase2" / "stability.json"


def load_panel() -> pd.DataFrame:
    stored = pd.read_parquet(PANEL_PATH)
    stored["mark"] = [date.fromisoformat(value) for value in stored["mark"]]
    return stored.set_index(["mark", "symbol"]).sort_index()


def main() -> None:
    started = time.perf_counter()
    panel = load_panel()

    structural = rank_stability(panel, STRUCTURAL_FEATURES)
    state = rank_stability(panel, STATE_FEATURES)
    surviving = [f for f in STRUCTURAL_FEATURES if structural[f]["structural"]]

    write_json(
        OUT_PATH,
        {
            "structural": structural,
            "state_for_reference": state,
            "surviving_structural_features": surviving,
            "gate": "median lag-6 Spearman >= 0.50",
        },
    )
    print(f"surviving structural features ({len(surviving)}):", flush=True)
    for feature in surviving:
        print(f"  {feature}", flush=True)
    dropped = [f for f in STRUCTURAL_FEATURES if f not in surviving]
    print(f"dropped ({len(dropped)}): {', '.join(dropped) or 'none'}", flush=True)
    print(f"phase2 complete in {time.perf_counter() - started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
