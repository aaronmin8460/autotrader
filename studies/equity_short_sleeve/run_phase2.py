"""Phase-2 runner: the short information study (ledger §L4, amendment A1).

Within DEFENSIVE regimes only, does any stable risk characteristic identify
names that subsequently perform worse? Three resolutions are computed; the
§L4.1 gate binds on the mark resolution alone.

Usage:
    python -m studies.equity_short_sleeve.run_phase2
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from studies.equity_deep_arch.evaluate import write_json
from studies.equity_eda1_nextgen.run_phase234 import (
    load_frame,
    load_universe,
    participation_map,
    region_sessions_of,
)
from studies.equity_eda1_nextgen.selection import close_table
from studies.equity_short_sleeve import CHARACTER_DATASETS, REPORT_ROOT
from studies.equity_short_sleeve.information import (
    CHARACTERISTICS,
    HORIZONS,
    PRIMARY_HORIZON,
    bootstrap_mean,
    cluster_bootstrap_mean,
    forward_targets,
    gate_l41,
    governing_mark_of,
    panel_at,
    rank_corr,
)

OUT = Path(REPORT_ROOT) / "phase2"
ENTRY_SESSIONS = 10


def _log(message: str) -> None:
    print(message, flush=True)


def load_panel() -> pd.DataFrame:
    panel = pd.read_parquet(Path(CHARACTER_DATASETS) / "fingerprints.parquet")
    panel["mark"] = [pd.Timestamp(m).date() for m in panel["mark"]]
    return panel


def defensive_runs(sessions: list[date], participate: dict[date, bool]) -> dict[date, int]:
    """Label each DEFENSIVE session with the index of its DEFENSIVE run."""
    run_of: dict[date, int] = {}
    run = -1
    previous = None
    for session in sessions:
        state = bool(participate[session])
        if not state:
            if previous is not True and run >= 0:
                pass
            if previous is None or previous is True:
                run += 1
            run_of[session] = run
        previous = state
    return run_of


def entry_window(sessions: list[date], run_of: dict[date, int]) -> set[date]:
    """The first `ENTRY_SESSIONS` sessions of each DEFENSIVE run."""
    seen: dict[int, int] = {}
    inside: set[date] = set()
    for session in sessions:
        run = run_of.get(session)
        if run is None:
            continue
        count = seen.get(run, 0)
        if count < ENTRY_SESSIONS:
            inside.add(session)
        seen[run] = count + 1
    return inside


def main() -> None:
    started = time.perf_counter()
    universe = sorted(load_universe("u30"))
    frames_full = {symbol: load_frame(symbol) for symbol in universe}
    closes = close_table(frames_full)
    participate = participation_map()
    sessions = region_sessions_of(frames_full["SPY"])
    region = [s for s in sessions if s in participate]
    closes = closes.loc[[s for s in closes.index if s in set(region)]]
    _log(f"universe {len(universe)}, sessions {len(region)}, closes {closes.shape}")

    panel = load_panel()
    marks = sorted({m for m in panel["mark"].unique()})
    mark_of = governing_mark_of(region, marks)
    run_of = defensive_runs(region, participate)
    entry = entry_window(region, run_of)
    _log(f"marks {len(marks)}, defensive sessions {len(run_of)}, runs {len(set(run_of.values()))}")

    targets = {h: forward_targets(closes, h) for h in HORIZONS}

    # Cache one cross-section per mark.
    cross = {m: panel_at(panel, m, universe) for m in marks}

    def evaluate(events: list[date], label: str, cluster_of) -> dict[str, object]:
        """Rank correlations for every (characteristic, target, horizon)."""
        block: dict[str, object] = {"resolution": label, "events": len(events)}
        per_char: dict[str, object] = {}
        for characteristic in CHARACTERISTICS:
            per_target: dict[str, object] = {}
            for target_name in ("fwd_ret", "fwd_exc", "fwd_mdd", "fwd_tail", "fwd_crash"):
                per_horizon: dict[int, object] = {}
                for horizon in HORIZONS:
                    table = targets[horizon].as_map()[target_name]
                    values: list[float] = []
                    clusters: list[object] = []
                    for session in events:
                        mark = mark_of.get(session)
                        if mark is None:
                            continue
                        row = cross.get(mark)
                        if row is None or characteristic not in row.columns:
                            continue
                        if session not in table.index:
                            continue
                        left = row[characteristic]
                        right = table.loc[session].reindex(left.index)
                        rho = rank_corr(left, right)
                        if np.isfinite(rho):
                            values.append(rho)
                            clusters.append(cluster_of(session))
                    if not values:
                        per_horizon[horizon] = {"n": 0}
                        continue
                    positives = sum(1 for v in values if v > 0)
                    mean = float(np.mean(values))
                    consistency = (positives if mean > 0 else len(values) - positives) / len(values)
                    stats = (
                        bootstrap_mean(values)
                        if label == "mark"
                        else cluster_bootstrap_mean(values, clusters)
                    )
                    stats["sign_consistency"] = consistency
                    stats["positive_fraction"] = positives / len(values)
                    per_horizon[horizon] = stats
                per_target[target_name] = per_horizon
            per_char[characteristic] = per_target
        block["by_characteristic"] = per_char
        return block

    defensive_sessions = [s for s in region if s in run_of]
    # A mark qualifies when the causal EDA-1 state at the mark's own session
    # is DEFENSIVE (amendment A1).
    mark_events = sorted({m for m in marks if m in run_of})
    _log(f"defensive marks {len(mark_events)} of {len(marks)}")

    results: dict[str, object] = {
        "universe": universe,
        "region_sessions": len(region),
        "defensive_sessions": len(defensive_sessions),
        "defensive_runs": len(set(run_of.values())),
        "defensive_marks": len(mark_events),
        "total_marks": len(marks),
        "horizons": list(HORIZONS),
        "primary_horizon": PRIMARY_HORIZON,
        "characteristics": list(CHARACTERISTICS),
    }
    results["mark"] = evaluate(mark_events, "mark", lambda s: s)
    _log(f"mark resolution done ({time.perf_counter() - started:.0f}s)")
    results["session"] = evaluate(defensive_sessions, "session", lambda s: run_of[s])
    _log(f"session resolution done ({time.perf_counter() - started:.0f}s)")
    entry_events = [s for s in defensive_sessions if s in entry]
    results["entry"] = evaluate(entry_events, "entry", lambda s: run_of[s])
    _log(f"entry resolution done ({time.perf_counter() - started:.0f}s)")

    # The §L4.1 gate, on the mark resolution's fwd_ret block only.
    gates: dict[str, object] = {}
    for characteristic in CHARACTERISTICS:
        by_horizon = results["mark"]["by_characteristic"][characteristic]["fwd_ret"]
        gates[characteristic] = gate_l41(
            {h: by_horizon[h] for h in HORIZONS if by_horizon[h].get("n")}
        )
    results["gate_l41"] = gates
    passers = [c for c, g in gates.items() if g.get("pass")]
    results["gate_l41_passers"] = passers
    results["gate_l41_verdict"] = "PASS" if passers else "FAIL"

    write_json(OUT / "information.json", results)
    _log(f"§L4.1 verdict: {results['gate_l41_verdict']} (passers: {passers or 'none'})")
    _log(f"phase2 complete in {time.perf_counter() - started:.0f}s")


if __name__ == "__main__":
    main()
