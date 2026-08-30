"""Run the causality audit for V1-V5 on SPY and QQQ, and write the verdict.

Sequential and single-process on purpose. The audit re-scores its frame once per
probe per engine, which is expensive per bar but small in total, and a study that
is already capped at two workers has nothing to gain from a third.

    python -m studies.equity_v1_v5.run_leakage --output <dir>
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from studies.equity_v1_v5 import PILOT_SYMBOLS
from studies.equity_v1_v5.adapters import LiveDecisionEngine
from studies.equity_v1_v5.calendar import read_snapshot, snapshot_path
from studies.equity_v1_v5.dataset import evaluation_path
from studies.equity_v1_v5.leakage import (
    DEFAULT_PROBES,
    DEFAULT_SCORED_BARS,
    audit_engine,
    audit_frame,
    summarize,
)
from studies.equity_v1_v5.run_pilot import (
    CALENDAR_END,
    CALENDAR_START,
    DATA_END,
    DATA_START,
    SEED,
    TRAINED_AT,
)
from studies.equity_v1_v5.scoring import build_engines
from studies.equity_v1_v5.walkforward import train_for_window
from studies.equity_v1_v5.windows import LOOKBACK_BARS, SCORING_WINDOWS

#: The window whose model V4 and V5 are audited under. The last one, because it
#: is the model fitted on the most history and therefore the one with the most
#: opportunity to have learned something it should not have.
AUDIT_WINDOW = SCORING_WINDOWS[-1]


def run(datasets: Path, output: Path, symbols: list[str]) -> dict[str, object]:
    calendar, _ = read_snapshot(snapshot_path(datasets, CALENDAR_START, CALENDAR_END))
    audits = []
    for symbol in symbols:
        frame = pd.read_parquet(evaluation_path(datasets, symbol, DATA_START, DATA_END))
        model = train_for_window(
            frame, calendar, AUDIT_WINDOW, symbol=symbol, seed=SEED, trained_at=TRAINED_AT
        )
        # Anchor the audit frame at the end of the audited window, so the bars
        # being perturbed are ones the study actually scored.
        _, last = AUDIT_WINDOW.positions(frame)
        window = audit_frame(
            frame,
            end_position=last,
            lookback_bars=LOOKBACK_BARS,
            scored_bars=DEFAULT_SCORED_BARS,
        )
        for spec in build_engines():
            artifact = model.artifact if spec.needs_model else None
            adapter = LiveDecisionEngine(
                spec.build(symbol, artifact),
                name=spec.name,
                version=spec.version,
                lookback_bars=LOOKBACK_BARS,
            )
            started = time.perf_counter()
            audit = audit_engine(
                adapter,
                window,
                probes=DEFAULT_PROBES,
                engine_name=spec.name,
                symbol=symbol,
            )
            audits.append(audit)
            print(
                f"  {symbol}/{spec.name}: {audit.scored_bars} bars, "
                f"{len(audit.probes)} probes, changed={audit.changed}, "
                f"vacuous={audit.vacuous_probes}, ok={audit.ok} "
                f"({time.perf_counter() - started:.0f}s)",
                flush=True,
            )

    report = summarize(audits)
    report["audit_window"] = AUDIT_WINDOW.to_json_dict()
    report["generated_at_utc"] = datetime.now(UTC).isoformat()
    output.mkdir(parents=True, exist_ok=True)
    (output / "leakage_audit.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit V1-V5 causality on equity bars.")
    parser.add_argument("--datasets", default=os.environ.get("EQUITY_DATASETS", "."))
    parser.add_argument("--output", default=os.environ.get("PILOT_REPORTS", "."))
    parser.add_argument("--symbols", nargs="*", default=list(PILOT_SYMBOLS))
    arguments = parser.parse_args()

    report = run(Path(arguments.datasets), Path(arguments.output), arguments.symbols)
    print(
        f"\nall_causal={report['all_causal']} "
        f"engines={report['engines_audited']} "
        f"changed={report['total_changed_decisions']} "
        f"vacuous={report['total_vacuous_probes']}"
    )


if __name__ == "__main__":
    main()
