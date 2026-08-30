"""Stage 2 runner: the V5 downstream diagnostic, one process per symbol.

Run after stage 1 has finished for the same stage's windows - each diagnostic
cell reads the V4 artifact its stage-1 checkpoint stored.

    python -m studies.equity_v4_horizon.run_v5 --symbol SPY --stage selection
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from studies.equity_v4_horizon.horizons import STUDY_HORIZONS
from studies.equity_v4_horizon.run_predictive import (
    DEFAULT_DATASET_ROOT,
    DEFAULT_OUTPUT_ROOT,
    load_frame,
    log,
    windows_for,
)
from studies.equity_v4_horizon.v5_diagnostic import run_symbol


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True, choices=("QQQ", "SPY"))
    parser.add_argument("--stage", default="selection", choices=("selection", "holdout"))
    parser.add_argument("--horizons", nargs="*", type=int, default=list(STUDY_HORIZONS))
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    arguments = parser.parse_args()

    horizons = tuple(int(h) for h in arguments.horizons)
    frame = load_frame(arguments.dataset_root, arguments.symbol)
    started = time.perf_counter()
    results = run_symbol(
        arguments.symbol,
        frame=frame,
        windows=windows_for(arguments.stage),
        horizons=horizons,
        output_root=arguments.output_root,
        log=log,
    )
    log(
        f"{arguments.symbol}: stage-2 {arguments.stage} produced {len(results)} diagnostics "
        f"in {time.perf_counter() - started:.0f}s."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
