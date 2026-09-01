"""Phase-8 runner: pooled/hierarchical OOS information test (§L10).

H1: ridge on state features + regime indicator + state×regime interactions.
H2: H1 + causal lineage one-hots + lineage×regime + shrunk symbol
intercepts. H3 (GBDT) runs only if H1 or H2 passes the information gate.

Usage:
    python -m studies.equity_asset_character.run_phase8
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

from studies.equity_asset_character import REPORT_ROOT
from studies.equity_asset_character.archetypes import fit_dates
from studies.equity_asset_character.fingerprints import (
    STATE_FEATURES,
    build_series,
    cross_sectional_z,
    symbol_sessions,
)
from studies.equity_asset_character.hierarchical import (
    PooledRow,
    demeaned_targets,
    walk_forward_ic,
)
from studies.equity_asset_character.response import forward_observations
from studies.equity_asset_character.run_phase2 import load_panel
from studies.equity_asset_character.run_phase4 import load_lineages, load_marks_regimes
from studies.equity_deep_arch.evaluate import write_json
from studies.equity_eda1_nextgen.run_phase234 import load_frame, load_universe

OUT_PATH = Path(REPORT_ROOT) / "phase8" / "hierarchical.json"


def build_rows() -> tuple[list[PooledRow], list[str], list[str], list[date]]:
    universe = load_universe("u50")
    tables = {s: symbol_sessions(load_frame(s)) for s in universe}
    series = {s: build_series(t, tables["SPY"]) for s, t in tables.items()}
    marks, regime_of, _ = load_marks_regimes()
    panel = load_panel()
    z_state = cross_sectional_z(panel, STATE_FEATURES)
    lineage_of = load_lineages()

    observations = forward_observations(series, marks, 21)
    forward_of = {
        (obs.mark, obs.symbol): (obs.own_return, obs.window_closes) for obs in observations
    }
    targets = demeaned_targets(forward_of, marks)

    lineages = sorted({lineage for lineage in lineage_of.values()})
    h1_features = [*STATE_FEATURES, "regime"] + [f"{f}_x_regime" for f in STATE_FEATURES]
    h2_features = (
        h1_features
        + [f"lineage_{lin}" for lin in lineages]
        + [f"lineage_{lin}_x_regime" for lin in lineages]
    )

    rows: list[PooledRow] = []
    for (mark, symbol), (target, closes) in sorted(targets.items()):
        try:
            state = z_state.loc[(mark, symbol)]
        except KeyError:
            continue
        values = {f: float(state[f]) for f in STATE_FEATURES}
        if any(v != v for v in values.values()):
            continue
        regime = 1.0 if regime_of.get(mark) == "PARTICIPATE" else 0.0
        features = dict(values)
        features["regime"] = regime
        for f in STATE_FEATURES:
            features[f"{f}_x_regime"] = values[f] * regime
        lineage = lineage_of.get((mark, symbol))
        for lin in lineages:
            on = 1.0 if lineage == lin else 0.0
            features[f"lineage_{lin}"] = on
            features[f"lineage_{lin}_x_regime"] = on * regime
        rows.append(
            PooledRow(
                mark=mark,
                symbol=symbol,
                target=target,
                window_closes=closes,
                features=features,
            )
        )
    return rows, h1_features, h2_features, marks


def main() -> None:
    started = time.perf_counter()
    rows, h1_features, h2_features, marks = build_rows()
    schedule = fit_dates(sorted({row.mark for row in rows}))
    print(f"{len(rows)} pooled rows, fits at {[d.isoformat() for d in schedule]}", flush=True)

    h1 = walk_forward_ic(rows, h1_features, schedule, marks, with_symbol_effects=False)
    print(
        f"H1: mean IC {h1['mean_ic']:.4f}, positive {h1['positive_share']:.2f}, "
        f"marks {h1['scored_marks']}, gate {h1['passes_gate']}",
        flush=True,
    )
    h2 = walk_forward_ic(rows, h2_features, schedule, marks, with_symbol_effects=True)
    print(
        f"H2: mean IC {h2['mean_ic']:.4f}, positive {h2['positive_share']:.2f}, "
        f"marks {h2['scored_marks']}, gate {h2['passes_gate']}",
        flush=True,
    )
    payload = {
        "rows": len(rows),
        "fit_schedule": [d.isoformat() for d in schedule],
        "H1": {k: v for k, v in h1.items() if k != "predictions"},
        "H2": {k: v for k, v in h2.items() if k != "predictions"},
        "h3_authorized": bool(h1["passes_gate"] or h2["passes_gate"]),
    }
    write_json(OUT_PATH, payload)
    print(f"phase8 complete in {time.perf_counter() - started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
