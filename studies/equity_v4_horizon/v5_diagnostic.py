"""Stage 2: what each horizon's V4 does to the UNCHANGED V5 policy, downstream.

The horizon is selected on predictive evidence; this stage is confirmation and
falsification only (design.md section 8). For every symbol x window x horizon
cell the stage drives the shipped ``EnsembleV5Engine`` - no weight, threshold
or band touched - with that cell's selected V4 artifact, over the identical
window bars and 3,000-bar lookback the pilot validated, and replays the stored
decision series under the pilot's three cost models.

**Reuse is verified, never assumed.** V3 takes no artifact, so the pilot's
stored V3 series is reused after the pilot's own zero-mismatch verification.
The pilot's stored V5 series at the shipped 4-bar horizon may be reused only
if a sampled live recomputation with THIS study's h=4 artifact reproduces it
exactly; one mismatched bar and the whole series is recomputed instead.

**Every series is checkpointed.** A window's decisions land as one parquet
before the next window is scored, and a finished cell is skipped on resume -
the same discipline as stage 1, because this is the stage long enough for a
laptop restart to interrupt.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

from autotrader.decision.probability import ProbabilityArtifact, artifact_from_record
from studies.equity_v1_v5.adapters import DecisionRecord
from studies.equity_v1_v5.scoring import (
    COST_MODELS,
    EngineSpec,
    build_engines,
    decisions_to_frame,
    frame_to_decisions,
    insufficient_history_count,
    metrics_for,
    overnight_fills,
    replay_series,
    score_window,
    verify_series_matches_live,
)
from studies.equity_v1_v5.windows import ScoringWindow
from studies.equity_v4_horizon.checkpoint import read_cell
from studies.equity_v4_horizon.horizons import require_study_horizon

#: How many sampled bars must reproduce exactly before a stored pilot V5
#: series is accepted in place of a fresh scoring pass.
REUSE_SAMPLES = 12

#: Where the pilot's verified decision series live.
PILOT_DECISIONS = Path("/Volumes/AUTOTRADER_QA/reports/equity-spy-qqq-pilot/decisions")


class DiagnosticError(Exception):
    """A V5 diagnostic that cannot be produced honestly."""


def v5_spec() -> EngineSpec:
    """The shipped V5 engine spec, unchanged."""
    for spec in build_engines():
        if spec.name == "V5":
            return spec
    raise DiagnosticError("The pilot harness no longer exposes a V5 engine.")  # pragma: no cover


def artifact_for_cell(output_root: Path, *, symbol: str, window: str, horizon: int):
    """The selected V4 artifact one stage-1 cell trained, rebuilt from its record."""
    from studies.equity_v4_horizon.checkpoint import cell_path

    stored = read_cell(cell_path(output_root, symbol=symbol, window=window, horizon_bars=horizon))
    return artifact_from_record(stored["selected_artifact"]), stored


def decisions_path(root: Path, *, symbol: str, window: str, horizon: int) -> Path:
    return root / "v5_decisions" / f"{symbol}_{window}_h{horizon:02d}_V5.parquet"


def pilot_series(symbol: str, window: str, engine: str) -> tuple[DecisionRecord, ...]:
    """A stored pilot decision series, as records."""
    path = PILOT_DECISIONS / f"{symbol}_{window}_{engine}.parquet"
    if not path.exists():
        raise DiagnosticError(f"No pilot series at {path}.")
    return frame_to_decisions(pd.read_parquet(path))


def can_reuse_pilot_v5(
    frame: pd.DataFrame,
    window: ScoringWindow,
    *,
    symbol: str,
    artifact: ProbabilityArtifact,
) -> bool:
    """Whether the pilot's stored h=4 V5 series matches a sampled live recompute.

    Uses the pilot's own verifier, pointed at the pilot's stored series but
    THIS study's h=4 artifact. Zero mismatches on the sample is the bar; the
    sample step covers the window evenly.
    """
    try:
        records = pilot_series(symbol, window.name, "V5")
    except DiagnosticError:
        return False
    mismatches = verify_series_matches_live(
        frame,
        records,
        v5_spec(),
        symbol=symbol,
        artifact=artifact,
        samples=REUSE_SAMPLES,
    )
    return not mismatches


def score_or_reuse(
    frame: pd.DataFrame,
    window: ScoringWindow,
    *,
    symbol: str,
    horizon: int,
    artifact: ProbabilityArtifact,
    output_root: Path,
    log,
) -> tuple[tuple[DecisionRecord, ...], str]:
    """One cell's V5 decision series: checkpointed, reused, or freshly scored."""
    path = decisions_path(output_root, symbol=symbol, window=window.name, horizon=horizon)
    if path.exists():
        log(f"{symbol}/{window.name}/h{horizon}: V5 series checkpoint exists, loading.")
        return frame_to_decisions(pd.read_parquet(path)), "checkpoint"

    provenance = "fresh"
    if horizon == 4 and can_reuse_pilot_v5(frame, window, symbol=symbol, artifact=artifact):
        records = pilot_series(symbol, window.name, "V5")
        provenance = "pilot-verified"
        log(f"{symbol}/{window.name}/h4: pilot V5 series verified against this artifact, reusing.")
    else:
        started = time.perf_counter()
        records = score_window(frame, window, v5_spec(), symbol=symbol, artifact=artifact)
        log(
            f"{symbol}/{window.name}/h{horizon}: V5 scored {len(records)} bars in "
            f"{time.perf_counter() - started:.0f}s."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    decisions_to_frame(records).to_parquet(path, engine="pyarrow", index=False)
    return records, provenance


def disagreement(
    first: tuple[DecisionRecord, ...], second: tuple[DecisionRecord, ...]
) -> dict[str, object]:
    """Per-bar signal comparison between two decision series on the same bars."""
    by_ts = {record.timestamp: record for record in second}
    total = 0
    differing = 0
    for record in first:
        other = by_ts.get(record.timestamp)
        if other is None:
            continue
        total += 1
        if record.signal != other.signal:
            differing += 1
    return {"compared_bars": total, "differing_signals": differing}


def diagnose_cell(
    frame: pd.DataFrame,
    window: ScoringWindow,
    *,
    symbol: str,
    horizon: int,
    output_root: Path,
    baseline_records: tuple[DecisionRecord, ...] | None,
    log,
) -> dict[str, object]:
    """The full downstream record for one cell."""
    require_study_horizon(horizon)
    artifact, stored_cell = artifact_for_cell(
        output_root, symbol=symbol, window=window.name, horizon=horizon
    )
    records, provenance = score_or_reuse(
        frame,
        window,
        symbol=symbol,
        horizon=horizon,
        artifact=artifact,
        output_root=output_root,
        log=log,
    )
    window_bars = window.bars(frame)
    replays = {}
    for cost_model in COST_MODELS:
        replayed = replay_series(
            window_bars, records, name="V5", version="v5", cost_model=cost_model
        )
        replays[cost_model.label] = metrics_for(replayed)

    entry: dict[str, object] = {
        "symbol": symbol,
        "window": window.name,
        "horizon_bars": horizon,
        "selected_family": stored_cell["selected_family"],
        "series_provenance": provenance,
        "decisions": len(records),
        "signals": sum(1 for record in records if record.to_signal() is not None),
        "insufficient_history": insufficient_history_count(records),
        "overnight_fills": overnight_fills(window_bars, records),
        "replays": replays,
        "v3_disagreement": disagreement(records, pilot_series(symbol, window.name, "V3")),
    }
    if baseline_records is not None:
        entry["vs_h4_v5"] = disagreement(records, baseline_records)
    return entry


def run_symbol(
    symbol: str,
    *,
    frame: pd.DataFrame,
    windows: tuple[ScoringWindow, ...],
    horizons: tuple[int, ...],
    output_root: Path,
    log,
) -> list[dict[str, object]]:
    """Every cell diagnostic for one symbol, h=4 first so it can be the baseline."""
    ordered = (4, *[h for h in horizons if h != 4]) if 4 in horizons else horizons
    results: list[dict[str, object]] = []
    baselines: dict[str, tuple[DecisionRecord, ...]] = {}
    for horizon in ordered:
        for window in windows:
            summary_path = (
                output_root / "v5_diagnostics" / f"{symbol}_{window.name}_h{horizon:02d}.json"
            )
            if summary_path.exists():
                log(f"{symbol}/{window.name}/h{horizon}: diagnostic exists, skipping.")
                results.append(json.loads(summary_path.read_text(encoding="utf-8")))
                if horizon == 4 and window.name not in baselines:
                    baselines[window.name] = frame_to_decisions(
                        pd.read_parquet(
                            decisions_path(
                                output_root, symbol=symbol, window=window.name, horizon=4
                            )
                        )
                    )
                continue
            entry = diagnose_cell(
                frame,
                window,
                symbol=symbol,
                horizon=horizon,
                output_root=output_root,
                baseline_records=baselines.get(window.name) if horizon != 4 else None,
                log=log,
            )
            if horizon == 4:
                baselines[window.name] = frame_to_decisions(
                    pd.read_parquet(
                        decisions_path(output_root, symbol=symbol, window=window.name, horizon=4)
                    )
                )
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(
                json.dumps(entry, indent=2, default=str) + "\n", encoding="utf-8"
            )
            results.append(entry)
    return results


__all__ = [
    "PILOT_DECISIONS",
    "REUSE_SAMPLES",
    "DiagnosticError",
    "artifact_for_cell",
    "can_reuse_pilot_v5",
    "decisions_path",
    "diagnose_cell",
    "disagreement",
    "pilot_series",
    "run_symbol",
    "score_or_reuse",
    "v5_spec",
]
