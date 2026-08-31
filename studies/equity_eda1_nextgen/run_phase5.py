"""Phase-5 runner: deterministic market/sector context rules (ledger §L6).

Each CTX rule is an additional AND-condition on the incumbent participation
rule, evaluated on the 10-symbol universe through the validated sleeve
machinery (paired against the incumbent baseline). Registered prediction:
stacked conditions most likely subtract capture without shrinking the
drawdown episode (the EDA-2/EDA-3 evidence); a rule that changes fewer than
3 % of sessions relative to the base rule is recorded as NO-OP.

Usage:
    python -m studies.equity_eda1_nextgen.run_phase5 --stage contexts
"""

from __future__ import annotations

import argparse
import time
from datetime import date
from pathlib import Path

import pandas as pd

from studies.equity_10_full import STUDY_SYMBOLS
from studies.equity_deep_arch.evaluate import (
    evaluate_challenger,
    load_region_frame,
    load_stored_series,
    write_json,
)
from studies.equity_deep_arch.overlay import participation_overlay
from studies.equity_deep_arch.run_eda1 import default_datasets, default_decisions
from studies.equity_deep_arch.state import (
    ParticipationSpec,
    participation_series,
    session_closes,
)
from studies.equity_eda1_nextgen import NEXTGEN_DATASETS, REPORT_ROOT
from studies.equity_eda1_nextgen.refined_states import state_flip_count

NEXTGEN = Path(NEXTGEN_DATASETS)


def _closes(symbol: str, datasets: Path) -> pd.Series:
    for directory in (datasets, NEXTGEN):
        files = sorted(directory.glob(f"{symbol}_15m_*session.parquet"))
        if files:
            closes = session_closes(pd.read_parquet(files[0]))
            return pd.Series(
                closes["close"].to_numpy(dtype="float64"),
                index=pd.Index(closes["session"], name="session"),
            )
    raise SystemExit(f"No frame for {symbol}.")


def _healthy(series: pd.Series, sessions: pd.Index) -> pd.Series:
    """Lagged 'own trend intact and near own high' per session: close > SMA200
    and trailing-peak dd > −5 %, on closes through the previous session."""
    aligned = series.reindex(sessions)
    shifted = aligned.shift(1)
    sma = shifted.rolling(200).mean()
    peak = shifted.cummax()
    drawdown = shifted / peak - 1.0
    return (shifted > sma) & (drawdown > -0.05) & sma.notna()


def build_context_series(datasets: Path) -> dict[str, pd.DataFrame]:
    spy = _closes("SPY", datasets)
    sessions = spy.index

    base = participation_series(
        session_closes(pd.read_parquet(sorted(datasets.glob("SPY_15m_*session.parquet"))[0])),
        ParticipationSpec(),
    )
    base_map = pd.Series(
        base["participate"].to_numpy(), index=pd.Index(base["session"]), dtype=bool
    ).reindex(sessions, fill_value=False)

    out: dict[str, pd.DataFrame] = {}

    # CTX-1: ≥ 2 of {SPY, QQQ, IWM} healthy.
    healthy = {s: _healthy(_closes(s, datasets), sessions) for s in ("SPY", "QQQ", "IWM")}
    votes = sum(h.fillna(False).astype(int) for h in healthy.values())
    ctx1 = base_map & (votes >= 2)
    out["CTX1_multi_index"] = pd.DataFrame({"session": sessions, "participate": ctx1.to_numpy()})

    # CTX-2: offence ≥ defence — RS-63(XLY) ≥ RS-63(XLP), lagged.
    xly = _closes("XLY", datasets).reindex(sessions).shift(1)
    xlp = _closes("XLP", datasets).reindex(sessions).shift(1)
    rs_xly = xly / xly.shift(63) - 1.0
    rs_xlp = xlp / xlp.shift(63) - 1.0
    ctx2 = base_map & (rs_xly >= rs_xlp).fillna(False)
    out["CTX2_offence_defence"] = pd.DataFrame(
        {"session": sessions, "participate": ctx2.to_numpy()}
    )

    # CTX-3: SPY 21-session realized vol below its trailing 252-session median.
    import numpy as np

    log_returns = pd.Series(np.log(spy.to_numpy()), index=sessions).diff().shift(1)
    vol21 = log_returns.rolling(21).std()
    median252 = vol21.rolling(252).median()
    ctx3 = base_map & (vol21 < median252).fillna(False)
    out["CTX3_vol_regime"] = pd.DataFrame({"session": sessions, "participate": ctx3.to_numpy()})

    out["_base"] = pd.DataFrame({"session": sessions, "participate": base_map.to_numpy()})
    return out


def run_contexts(datasets: Path, decisions: Path, output: Path) -> None:
    from autotrader.equity.session import market_date

    contexts = build_context_series(datasets)
    base = contexts.pop("_base")
    base_states = dict(zip(base["session"], base["participate"], strict=True))

    for name, series in contexts.items():
        target = output / f"phase5_{name}.json"
        if target.exists():
            print(f"{name}: exists, skipping", flush=True)
            continue
        started = time.perf_counter()
        states: dict[date, bool] = dict(
            zip(series["session"], (bool(v) for v in series["participate"]), strict=True)
        )
        changed = sum(1 for s, v in states.items() if v != base_states.get(s, False))
        flips = state_flip_count(series, "participate")

        challenger: dict[str, tuple] = {}
        for symbol in STUDY_SYMBOLS:
            frame = load_region_frame(datasets, symbol)
            stored = load_stored_series(decisions, symbol, "V3")
            by_bar = {
                pd.Timestamp(ts): states[market_date(ts.to_pydatetime())]
                for ts in frame["timestamp"]
            }
            challenger[symbol] = participation_overlay(stored, by_bar, architecture=name)
        result = evaluate_challenger(
            datasets, decisions, challenger, label=name, symbols=STUDY_SYMBOLS
        )
        result["sessions_changed_vs_base"] = changed
        result["changed_fraction"] = changed / len(states)
        result["session_state_flips"] = flips
        result["noop"] = bool(changed / len(states) < 0.03)
        write_json(target, result)
        print(
            f"{name}: done in {time.perf_counter() - started:.0f}s "
            f"(changed {changed} sessions, noop={changed / len(states) < 0.03})",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("contexts",))
    parser.add_argument("--datasets", type=Path, default=default_datasets())
    parser.add_argument("--decisions", type=Path, default=default_decisions())
    parser.add_argument("--output", type=Path, default=Path(REPORT_ROOT) / "phase5")
    arguments = parser.parse_args()
    run_contexts(arguments.datasets, arguments.decisions, arguments.output)


if __name__ == "__main__":
    main()
