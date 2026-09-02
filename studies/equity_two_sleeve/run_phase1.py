"""Phase-1 runner: sleeve complementarity analysis (ledger §L5).

Reads the Phase-0 primary equity curves of sleeve E (EDA1_BRIDGE) and sleeve
A (A1_B), reduces them to session-close returns, and records the predeclared
correlation/coincidence battery BEFORE any blend result exists. Descriptive
only — no gate.

Usage:
    python -m studies.equity_two_sleeve.run_phase1
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from studies.equity_deep_arch.evaluate import write_json
from studies.equity_two_sleeve import REPORT_ROOT, TWO_SLEEVE_DATASETS

CURVES = Path(TWO_SLEEVE_DATASETS) / "curves"
OUT = Path(REPORT_ROOT) / "phase1"

ROLL = 63
UNDERWATER = -0.05
N_EXTREME = 20


def _log(message: str) -> None:
    print(message, flush=True)


def session_curve(path: Path) -> pd.Series:
    from autotrader.equity.session import market_date

    frame = pd.read_parquet(path)
    days = [market_date(pd.Timestamp(ts).to_pydatetime()) for ts in frame["timestamp"]]
    series = pd.Series(frame["equity"].to_numpy(), index=pd.Index(days, name="session"))
    return series.groupby(level=0).last()


def spy_session_states() -> pd.Series:
    from autotrader.equity.session import market_date
    from studies.equity_eda1_nextgen.run_phase234 import spy_states

    states = spy_states()
    days = [market_date(ts.to_pydatetime()) for ts in states.index]
    return (
        pd.Series(states.to_numpy(), index=pd.Index(days, name="session")).groupby(level=0).last()
    )


def spy_session_returns() -> pd.Series:
    from studies.equity_deep_arch.state import session_closes
    from studies.equity_eda1_nextgen.run_phase234 import load_frame, region_frame

    spy = region_frame(load_frame("SPY"))
    closes = session_closes(spy)
    series = pd.Series(
        closes["close"].astype(float).to_numpy(), index=pd.Index(closes["session"], name="session")
    )
    return series.pct_change().dropna()


def _corr(a: pd.Series, b: pd.Series) -> dict[str, float | int]:
    joined = pd.concat([a, b], axis=1, join="inner").dropna()
    if len(joined) < 3:
        return {"n": int(len(joined)), "pearson": float("nan"), "spearman": float("nan")}
    x, y = joined.iloc[:, 0], joined.iloc[:, 1]
    return {
        "n": int(len(joined)),
        "pearson": float(x.corr(y)),
        # Spearman as Pearson-of-ranks: the venv carries no scipy by repo
        # convention (the study stack is numpy/pandas-native).
        "spearman": float(x.rank().corr(y.rank())),
    }


def _turnover_series(targets: dict[str, dict[pd.Timestamp, float]]) -> pd.Series:
    """Per-session Σ|Δ session-close target| across symbols — the ledger's
    machinery-free turnover-coincidence proxy."""
    from autotrader.equity.session import market_date

    frames: list[pd.Series] = []
    for symbol in sorted(targets):
        series = targets[symbol]
        stamps = sorted(series)
        days = [market_date(s.to_pydatetime()) for s in stamps]
        per_session = (
            pd.Series([series[s] for s in stamps], index=pd.Index(days, name="session"))
            .groupby(level=0)
            .last()
        )
        frames.append(per_session.diff().abs().rename(symbol))
    table = pd.concat(frames, axis=1)
    return table.sum(axis=1).dropna()


def main() -> None:
    started = time.perf_counter()
    curve_e = session_curve(CURVES / "EDA1_BRIDGE_equity-marketable.parquet")
    curve_a = session_curve(CURVES / "A1_B_equity-marketable.parquet")
    ret_e = curve_e.pct_change().dropna().rename("E")
    ret_a = curve_a.pct_change().dropna().rename("A")
    states = spy_session_states()
    spy_ret = spy_session_returns()

    payload: dict[str, object] = {"sessions": int(len(ret_e))}
    payload["full_period"] = _corr(ret_e, ret_a)

    rolling = ret_e.rolling(ROLL).corr(ret_a).dropna()
    payload["rolling_63"] = {
        "min": float(rolling.min()),
        "median": float(rolling.median()),
        "max": float(rolling.max()),
        "share_above_0.9": float((rolling > 0.9).mean()),
    }

    by_state: dict[str, object] = {}
    for state in ("calm", "pullback", "drawdown"):
        mask = states[states == state].index
        by_state[state] = _corr(
            ret_e.loc[ret_e.index.isin(mask)], ret_a.loc[ret_a.index.isin(mask)]
        )
    stress_mask = states[states.isin(("pullback", "drawdown"))].index
    by_state["stress_pooled"] = _corr(
        ret_e.loc[ret_e.index.isin(stress_mask)], ret_a.loc[ret_a.index.isin(stress_mask)]
    )
    payload["by_spy_state"] = by_state

    down_sessions = spy_ret[spy_ret < 0].index
    payload["spy_down_sessions"] = _corr(
        ret_e.loc[ret_e.index.isin(down_sessions)], ret_a.loc[ret_a.index.isin(down_sessions)]
    )

    for label, ret_x, ret_y in (("E_worst_decile", ret_e, ret_a), ("A_worst_decile", ret_a, ret_e)):
        threshold = ret_x.quantile(0.10)
        mask = ret_x[ret_x <= threshold].index
        payload[label] = _corr(ret_x.loc[mask], ret_y.loc[ret_y.index.isin(mask)])

    dd_e = curve_e / curve_e.cummax() - 1.0
    dd_a = curve_a / curve_a.cummax() - 1.0
    both = ((dd_e < UNDERWATER) & (dd_a < UNDERWATER)).mean()
    either = ((dd_e < UNDERWATER) | (dd_a < UNDERWATER)).mean()
    payload["drawdown_overlap"] = {
        "underwater_threshold": UNDERWATER,
        "share_E_underwater": float((dd_e < UNDERWATER).mean()),
        "share_A_underwater": float((dd_a < UNDERWATER).mean()),
        "share_both": float(both),
        "overlap_given_either": float(both / either) if either else float("nan"),
    }

    worst_e = set(ret_e.nsmallest(N_EXTREME).index)
    worst_a = set(ret_a.nsmallest(N_EXTREME).index)
    best_e = set(ret_e.nlargest(N_EXTREME).index)
    best_a = set(ret_a.nlargest(N_EXTREME).index)
    payload["extreme_coincidence"] = {
        "n": N_EXTREME,
        "worst_shared": len(worst_e & worst_a),
        "best_shared": len(best_e & best_a),
        "worst_shared_sessions": sorted(str(s) for s in (worst_e & worst_a)),
    }

    from studies.equity_asset_character.run_phase5 import TiltContext
    from studies.equity_two_sleeve.blend import a_sleeve_targets, e_sleeve_targets

    context = TiltContext("u30")
    targets_a = a_sleeve_targets(context)
    targets_e = e_sleeve_targets(
        context.context.frames,
        context.context.sessions,
        context.context.participate,
        context.context.stance,
    )
    to_e = _turnover_series(targets_e).rename("E")
    to_a = _turnover_series(targets_a).rename("A")
    payload["turnover_coincidence"] = _corr(to_e, to_a)
    active_e = to_e[to_e > 1e-9]
    active_a = to_a[to_a > 1e-9]
    payload["turnover_activity"] = {
        "E_active_sessions": int(len(active_e)),
        "A_active_sessions": int(len(active_a)),
        "shared_active_sessions": int(len(set(active_e.index) & set(active_a.index))),
    }

    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "complementarity.json", payload)
    _log(f"phase1 complete in {time.perf_counter() - started:.0f}s")


if __name__ == "__main__":
    main()
