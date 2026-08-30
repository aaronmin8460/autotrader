"""Assemble the pilot's markdown report from the JSON artifacts the runs wrote.

Every table comes from a stored file. Nothing is recomputed, so a figure in the
report can always be traced to the run that produced it, and regenerating the
report after an edit cannot silently change a number.

    python -m studies.equity_v1_v5.build_report --output <dir> --report <path>
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from studies.equity_v1_v5 import PILOT_SYMBOLS
from studies.equity_v1_v5.report import (
    aggregation_table,
    coverage_table,
    dataset_table,
    integrity_table,
    leakage_table,
    metrics_table,
    table,
    walkforward_table,
)


def _optional(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build(output: Path, symbols: list[str]) -> str:
    results = []
    for symbol in symbols:
        payload = _optional(output / f"{symbol}_pilot.json")
        if payload is None:
            raise SystemExit(f"Missing {symbol}_pilot.json in {output}. Run the pilot first.")
        results.append(payload)

    leakage = _optional(output / "leakage_audit.json")
    repro = _optional(output / "reproducibility.json")
    sessions = _optional(output / "sessions_audit.json")

    parts: list[str] = []
    parts.append("### Dataset provenance and quality\n\n" + dataset_table(results))
    parts.append("### Common scoring window\n\n" + coverage_table(results[0]))
    if sessions is not None:
        rows = [
            [
                str(fact["label"]),
                str(fact["day"]),
                "yes" if fact["is_session"] else "no",
                (
                    f"{fact['open_local']}-{fact['close_local']}"
                    if fact["is_session"]
                    else "-"
                ),
                str(fact["utc_offset_hours"] or "-"),
                str(fact["scheduled_bars"]),
                str(fact["observed_bars"]),
                str(fact["actionable_wake_times"]),
            ]
            for fact in sessions["facts"]
        ]
        parts.append(
            "### Named-session validation\n\n"
            + table(
                [
                    "Day",
                    "Date",
                    "Session",
                    "Local hours",
                    "UTC off",
                    "Scheduled bars",
                    "Observed",
                    "Actionable",
                ],
                rows,
            )
            + "\n\nInvariants: "
            + ", ".join(
                f"{name} = {'PASS' if value else 'FAIL'}"
                for name, value in sessions["invariants"].items()
            )
        )
    parts.append("### 15m to 1h to 4h aggregation\n\n" + aggregation_table(results))
    parts.append("### V4 walk-forward training plan\n\n" + walkforward_table(results))
    for label, heading in (
        ("frictionless", "Zero-cost diagnostic (upper bound, not a result)"),
        ("equity-marketable", "Realistic equity cost model"),
        ("stress", "Stress cost model"),
    ):
        parts.append(f"### {heading}\n\n" + metrics_table(results, cost_label=label))
    parts.append("### Scoring integrity\n\n" + integrity_table(results))
    if leakage is not None:
        parts.append("### Causality audit\n\n" + leakage_table(leakage))
    if repro is not None:
        rows = [
            [str(check["symbol"]), str(check["check"]), "PASS" if check["ok"] else "FAIL"]
            for check in repro["checks"]
        ]
        parts.append("### Reproducibility\n\n" + table(["Symbol", "Check", "Result"], rows))

    header = (
        f"<!-- generated {datetime.now(UTC).isoformat()} by "
        "studies.equity_v1_v5.build_report -->\n"
    )
    return header + "\n\n".join(parts) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble the pilot report tables.")
    parser.add_argument("--output", default=os.environ.get("PILOT_REPORTS", "."))
    parser.add_argument("--report", required=True)
    parser.add_argument("--symbols", nargs="*", default=list(PILOT_SYMBOLS))
    arguments = parser.parse_args()
    Path(arguments.report).write_text(
        build(Path(arguments.output), arguments.symbols), encoding="utf-8"
    )
    print(f"wrote {arguments.report}")


if __name__ == "__main__":
    main()
