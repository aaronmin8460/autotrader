"""The run manifest: exactly what was executed, on what, with what code.

Written after scoring rather than before it, so the recorded git SHA is the
commit the cells were actually produced by. It is deliberately a separate
module from the runner: editing the runner while its launch is already armed
behind the compute wait gate would mean the gate starts code that was never
the code under test.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from studies.crypto_funding_basis_pilot import run_pilot
from studies.crypto_funding_basis_pilot.derivative_features import (
    DERIVATIVE_FEATURES,
    MAX_FUNDING_STALENESS,
    MAX_PREMIUM_STALENESS,
)
from studies.crypto_funding_basis_pilot.frozen_data import WINDOWS, exact_break_even

OUTPUT_DIR = Path("/Volumes/AUTOTRADER_QA/reports/crypto-funding-basis-pilot")


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True).strip()
    except (subprocess.CalledProcessError, OSError):
        return "unknown"


def main() -> None:
    cells = [json.loads(p.read_text()) for p in sorted((OUTPUT_DIR / "cells").glob("*.json"))]
    statuses = Counter(c.get("status", "unknown") for c in cells)
    by_arm = Counter(c["arm"] for c in cells if c.get("status") == "ok")
    by_horizon = Counter(c["horizon"] for c in cells if c.get("status") == "ok")
    seconds = sum(c.get("seconds", 0.0) for c in cells)

    payload = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "study": "crypto funding/basis incremental-information pilot",
        "research_only": True,
        "code": {
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "commit": _git("rev-parse", "HEAD"),
            "base": "integration/final-development-candidate @ "
            "aee7a77af090fd9d3dd60f66c400fa2360f2f478",
            "worktree_clean": _git("status", "--porcelain") == "",
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "design": {
            "baseline_features": list(run_pilot.BASELINE_FEATURES),
            "baseline_feature_count": len(run_pilot.BASELINE_FEATURES),
            "derivative_features": list(DERIVATIVE_FEATURES),
            "augmented_feature_count": len(run_pilot.AUGMENTED_FEATURES),
            "funding_only_features": list(run_pilot.FUNDING_FEATURES),
            "basis_only_features": list(run_pilot.BASIS_FEATURES),
            "horizons": list(run_pilot.HORIZONS),
            "primary_horizon": run_pilot.PRIMARY_HORIZON,
            "windows": {w: WINDOWS[w] for w in run_pilot.ALL_WINDOWS},
            "window_count": len(run_pilot.ALL_WINDOWS),
            "model_family": run_pilot.FAMILY,
            "fit_fraction": run_pilot.FIT_FRACTION,
            "embargo": str(run_pilot.EMBARGO),
            "gates": [{"name": n, "enter": e, "exit": x} for n, e, x in run_pilot.GATES],
            "cost_models": list(run_pilot.COST_MODELS),
            "break_even_bps": exact_break_even() * 1e4,
            "decision_cadence": "last completed 15m bar of each UTC day",
            "max_funding_staleness": str(MAX_FUNDING_STALENESS),
            "max_premium_staleness": str(MAX_PREMIUM_STALENESS),
            "shared_row_population": True,
        },
        "execution": {
            "cell_files": len(cells),
            "statuses": dict(statuses),
            "ok_by_arm": dict(by_arm),
            "ok_by_horizon": {str(k): v for k, v in by_horizon.items()},
            "total_cell_seconds": round(seconds, 1),
            "total_cell_hours": round(seconds / 3600.0, 2),
        },
    }
    path = OUTPUT_DIR / "run_manifest.json"
    path.write_text(json.dumps(payload, indent=2, default=str))
    print(json.dumps(payload["execution"], indent=2))
    print(f"commit {payload['code']['commit'][:12]} clean={payload['code']['worktree_clean']}")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
