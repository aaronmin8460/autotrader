"""Run the repository's leakage audits against the real engines on the real data.

The study's tests prove the adapters behave on fixtures. This proves the claim
that matters on the actual dataset: that every shipped engine, driven exactly as
the scoring pass drives it, cannot see the future.

**Why this runs against `LiveDecisionEngine` and not the series.** The auditor
perturbs the bars after a probe index and re-asks, then requires that everything
the engine said at or before that index is unchanged. That question is only
meaningful for something that computes. A precomputed series would answer it
identically no matter what was done to the bars, and report a clean pass that
established nothing - which is why the series adapter declares `audit_ready`
false and this module refuses to audit it.

**V3, V4 and V5 are audited on a short frame.** Each probe re-runs the engine
over the whole frame, and V3's window is 2400 bars, so a full-length audit would
cost more than the study it is checking. The frame is long enough to hold
several decisions past every engine's warm-up, which is what the perturbation
argument needs; it does not need the whole history to establish that a
computation is causal.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from autotrader.decision.probability import artifact_from_record
from autotrader.research.leakage import audit_engine_causality, audit_splits
from studies.crypto_v1_v5.adapters import DecisionSeriesEngine, LiveDecisionEngine
from studies.crypto_v1_v5.analysis import splits_for_folds
from studies.crypto_v1_v5.dataset import load_evaluation_frame
from studies.crypto_v1_v5.run_scoring import dataset_paths
from studies.crypto_v1_v5.scoring import SHARED_LOOKBACK_BARS, build_panel, score_window

#: Decisions to audit past the warm-up. Each probe replays the frame, so this is
#: the term that decides how long the audit takes.
AUDIT_DECISIONS = 12
AUDIT_PROBES = 3


def audit_engines(bars: pd.DataFrame, symbol: str, artifact_record: dict) -> list[dict]:
    """Audit every shipped engine, driven exactly as the scoring pass drives it."""
    artifact = artifact_from_record(artifact_record)
    panel = build_panel(symbol, artifact, memoize=False)
    frame = bars.iloc[-(SHARED_LOOKBACK_BARS + AUDIT_DECISIONS) :].reset_index(drop=True)

    findings: list[dict] = []
    for version, engine in panel.ordered():
        adapter = LiveDecisionEngine(
            engine, name=version, version=version, lookback_bars=SHARED_LOOKBACK_BARS
        )
        assert adapter.audit_ready, "causality evidence must come from a computing adapter"
        report = audit_engine_causality(adapter, frame, probes=AUDIT_PROBES)
        findings.append(
            {
                "symbol": symbol,
                "engine": version,
                "probes": AUDIT_PROBES,
                "decisions_audited": AUDIT_DECISIONS,
                "clean": bool(report.clean),
                "findings": [str(item) for item in report.findings],
            }
        )
        print(f"  {symbol} {version}: {'CLEAN' if report.clean else 'FINDINGS'}", flush=True)
    return findings


def audit_series_equivalence(
    bars: pd.DataFrame, symbol: str, artifact_record: dict
) -> dict[str, object]:
    """Prove the replayed series is what the live engines actually produced.

    The whole study replays a stored series rather than re-deciding, so the two
    must be shown to agree on the real data and not only on a fixture.
    """
    artifact = artifact_from_record(artifact_record)
    last = len(bars) - 1
    first = last - AUDIT_DECISIONS + 1
    scored = score_window(
        bars,
        build_panel(symbol, artifact),
        first_decision_index=first,
        last_decision_index=last,
    )

    mismatches: list[str] = []
    panel = build_panel(symbol, artifact, memoize=False)
    for version, engine in panel.ordered():
        adapter = LiveDecisionEngine(
            engine, name=version, version=version, lookback_bars=SHARED_LOOKBACK_BARS
        )
        live = adapter.decisions(
            bars.iloc[first - SHARED_LOOKBACK_BARS + 1 :].reset_index(drop=True)
        )
        series = DecisionSeriesEngine(
            tuple(record for record in _records(scored, version)),
            name=version,
            version=version,
            warmup_bars=0,
        )
        live_signals = tuple(
            signal for signal in (record.to_signal() for record in live) if signal is not None
        )
        frame = bars.iloc[first:].reset_index(drop=True)
        if live_signals != tuple(series.generate(frame)):
            mismatches.append(version)
    return {
        "symbol": symbol,
        "decisions_compared": AUDIT_DECISIONS,
        "mismatched_engines": mismatches,
    }


def _records(scored: pd.DataFrame, engine: str):
    from studies.crypto_v1_v5.scoring import records_from_frame

    return records_from_frame(scored, engine)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--variant", default="selected")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    folds = json.loads((run_dir / "v4_walkforward_folds.json").read_text())

    report: dict[str, object] = {"engine_causality": [], "split_structure": [], "equivalence": []}
    print("auditing engine causality on real bars...", flush=True)
    for symbol, path in dataset_paths(Path(args.datasets)).items():
        bars, _ = load_evaluation_frame(Path(path))
        record_path = run_dir / "artifacts" / f"{symbol.replace('/', '_')}_W01_{args.variant}.json"
        artifact_record = json.loads(record_path.read_text())
        report["engine_causality"].extend(audit_engines(bars, symbol, artifact_record))
        report["equivalence"].append(audit_series_equivalence(bars, symbol, artifact_record))

        symbol_folds = [f for f in folds if f["symbol"] == symbol and f["variant"] == args.variant]
        seen: set[str] = set()
        unique = []
        for fold in symbol_folds:
            if fold["fold_id"] not in seen:
                seen.add(fold["fold_id"])
                unique.append(fold)
        splits = splits_for_folds(bars, unique)
        split_report = audit_splits(splits, require_disjoint_tests=True)
        report["split_structure"].append(
            {
                "symbol": symbol,
                "splits": len(splits),
                "clean": bool(split_report.clean),
                "findings": [str(item) for item in split_report.findings],
            }
        )

    target = run_dir / f"leakage_audit_{args.variant}.json"
    target.write_text(json.dumps(report, indent=2))
    clean = all(entry["clean"] for entry in report["engine_causality"]) and all(
        entry["clean"] for entry in report["split_structure"]
    )
    equivalent = all(not entry["mismatched_engines"] for entry in report["equivalence"])
    print(f"engine causality + split structure clean: {clean}")
    print(f"series matches live decisions:            {equivalent}")
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
