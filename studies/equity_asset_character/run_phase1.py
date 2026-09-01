"""Phase-1 runner: causal fingerprints for the U45 research universe (§L3).

Builds the (mark × symbol) structural + state panel from the frozen session
frames, writes it as parquet under this program's dataset root, and a
coverage summary under the report root. Marks are the inherited 21-session
rebalance sessions of the scored region; every value at mark m reads
completed sessions strictly before m.

Usage:
    python -m studies.equity_asset_character.run_phase1
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from studies.equity_asset_character import CHARACTER_DATASETS, REPORT_ROOT
from studies.equity_asset_character.fingerprints import (
    STATE_FEATURES,
    STRUCTURAL_FEATURES,
    fingerprint_panel,
    symbol_sessions,
)
from studies.equity_deep_arch.evaluate import write_json
from studies.equity_deep_arch.state import ParticipationSpec, participation_series, session_closes
from studies.equity_eda1_nextgen.run_phase234 import (
    load_frame,
    load_universe,
    region_sessions_of,
)
from studies.equity_eda1_nextgen.selection import rebalance_sessions

PANEL_PATH = Path(CHARACTER_DATASETS) / "fingerprints.parquet"
MARKS_PATH = Path(REPORT_ROOT) / "phase1" / "marks.json"
SUMMARY_PATH = Path(REPORT_ROOT) / "phase1" / "fingerprint_coverage.json"


def mark_regimes(marks: list) -> list[dict[str, object]]:
    """EDA-1 participation state and SPY drawdown state at each mark, causal.

    Both read SPY completed-session closes strictly before the mark: the
    participation series is the validated lag-1 machine; the drawdown state is
    close[m−1] vs the running peak of closes through m−1 (calm > −5 %,
    pullback −10..−5 %, drawdown ≤ −10 %).
    """
    spy = load_frame("SPY")
    closes = session_closes(spy)
    participation = participation_series(closes, ParticipationSpec())
    participate_of = {
        row["session"]: bool(row["participate"]) for _, row in participation.iterrows()
    }
    sessions = list(closes["session"])
    values = closes["close"].to_numpy(dtype="float64")

    rows = []
    for mark in marks:
        end = int(np.searchsorted(np.asarray(sessions, dtype=object), mark, side="left"))
        if end == 0:
            raise SystemExit(f"No SPY history before mark {mark}.")
        history = values[:end]
        drawdown = float(history[-1] / history.max() - 1.0)
        if drawdown > -0.05:
            state = "calm"
        elif drawdown > -0.10:
            state = "pullback"
        else:
            state = "drawdown"
        rows.append(
            {
                "mark": mark.isoformat(),
                "participate": participate_of[mark],
                "spy_state": state,
                "spy_drawdown": drawdown,
            }
        )
    return rows


def main() -> None:
    started = time.perf_counter()
    universe = load_universe("u50")
    frames = {symbol: load_frame(symbol) for symbol in universe}
    tables = {symbol: symbol_sessions(frame) for symbol, frame in frames.items()}
    marks = rebalance_sessions(region_sessions_of(frames["SPY"]))
    print(f"{len(universe)} symbols, {len(marks)} marks", flush=True)

    panel = fingerprint_panel(tables, marks)
    PANEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    stored = panel.reset_index()
    stored["mark"] = stored["mark"].astype(str)
    stored.to_parquet(PANEL_PATH, engine="pyarrow", index=False)

    coverage = {
        "universe": sorted(universe),
        "marks": len(marks),
        "first_mark": marks[0].isoformat(),
        "last_mark": marks[-1].isoformat(),
        "structural_features": list(STRUCTURAL_FEATURES),
        "state_features": list(STATE_FEATURES),
        "non_nan_share_by_feature": {
            feature: float(panel[feature].notna().mean())
            for feature in (*STRUCTURAL_FEATURES, *STATE_FEATURES)
        },
        "non_nan_symbols_at_first_mark": {
            feature: int(panel.loc[marks[0]][feature].notna().sum())
            for feature in STRUCTURAL_FEATURES
        },
        "non_nan_symbols_at_last_mark": {
            feature: int(panel.loc[marks[-1]][feature].notna().sum())
            for feature in STRUCTURAL_FEATURES
        },
    }
    write_json(SUMMARY_PATH, coverage)
    write_json(MARKS_PATH, {"marks": mark_regimes(marks)})
    print(f"phase1 complete in {time.perf_counter() - started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
