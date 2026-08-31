"""Phases 2–4 runner: expanded-universe base comparison, cross-sectional
selection, allocators (ledger §L3–L5 and dated amendments).

Universe frames come from the frozen incumbent directory (read-only) and the
program's own dataset directory. The incumbent participation rule gates
everything (component isolation); defensive sessions hold
`reserved_weight × V3 stance` per symbol, with stances from the stored series
(incumbents) or the alias-scored drive (new symbols).

Usage:
    python -m studies.equity_eda1_nextgen.run_phase234 --stage base --universe u30
    python -m studies.equity_eda1_nextgen.run_phase234 --stage selection --universe u30
    python -m studies.equity_eda1_nextgen.run_phase234 --stage allocators \\
        --universe u30 --rule <winner>
    python -m studies.equity_eda1_nextgen.run_phase234 --stage bridge
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date
from pathlib import Path

import pandas as pd

from studies.equity_10_full.windows import FULL_WINDOWS
from studies.equity_deep_arch.evaluate import (
    NEGATIVE_WINDOWS,
    POSITIVE_WINDOWS,
    write_json,
)
from studies.equity_deep_arch.overlay import source_stance
from studies.equity_deep_arch.state import (
    ParticipationSpec,
    participation_series,
    session_closes,
)
from studies.equity_eda1_nextgen import NEXTGEN_DATASETS, REPORT_ROOT
from studies.equity_eda1_nextgen.selection import (
    PER_SYMBOL_CAP,
    RS_PRIMARY,
    RS_SECONDARY,
    above_sma,
    build_membership,
    build_targets,
    close_table,
    rank_symbols,
    rebalance_sessions,
    trailing_return,
)
from studies.equity_eda1_nextgen.universe import INCUMBENTS, SECTOR_OF
from studies.equity_eda1_nextgen.weighted_replay import WeightedResult, replay_weighted
from studies.equity_v1_v5.scoring import COST_MODELS, frame_to_decisions

FROZEN_DATASETS = Path("/Volumes/AUTOTRADER_QA/datasets/equity-historical")
FROZEN_DECISIONS = Path("/Volumes/AUTOTRADER_QA/reports/equity-10-symbol-full/decisions")
DRIVE_DECISIONS = Path(NEXTGEN_DATASETS) / "v3-decisions"
MANIFEST = Path(REPORT_ROOT) / "phase2" / "universe_manifest.json"

REGION_START = FULL_WINDOWS[0].start
REGION_END = FULL_WINDOWS[-1].end


def _log(message: str) -> None:
    print(message, flush=True)


def load_universe(name: str) -> list[str]:
    manifest = json.loads(MANIFEST.read_text())
    return list(manifest["manifests"][name])


def load_frame(symbol: str) -> pd.DataFrame:
    for directory in (FROZEN_DATASETS, Path(NEXTGEN_DATASETS)):
        files = sorted(directory.glob(f"{symbol}_15m_*session.parquet"))
        if files:
            return pd.read_parquet(files[0])
    raise SystemExit(f"No session frame for {symbol}.")


def region_frame(frame: pd.DataFrame) -> pd.DataFrame:
    from autotrader.equity.session import market_date

    days = [market_date(ts.to_pydatetime()) for ts in frame["timestamp"]]
    mask = [(REGION_START <= day <= REGION_END) for day in days]
    return frame.loc[mask].reset_index(drop=True)


def load_stance(symbol: str, frame: pd.DataFrame) -> dict[pd.Timestamp, int]:
    """Bar-level V3 stance for one symbol over the scored region."""
    records = []
    for window in FULL_WINDOWS:
        for directory in (FROZEN_DECISIONS, DRIVE_DECISIONS):
            path = directory / f"{symbol}_{window.name}_V3.parquet"
            if path.exists():
                records.extend(frame_to_decisions(pd.read_parquet(path)))
                break
        else:
            raise SystemExit(f"No V3 series for {symbol}/{window.name}.")
    ordered = sorted(records, key=lambda record: record.timestamp)
    stances = source_stance(ordered)
    return {pd.Timestamp(r.timestamp): s for r, s in zip(ordered, stances, strict=True)}


def participation_map() -> dict[date, bool]:
    spy = load_frame("SPY")
    series = participation_series(session_closes(spy), ParticipationSpec())
    return {row["session"]: bool(row["participate"]) for _, row in series.iterrows()}


def region_sessions_of(frame: pd.DataFrame) -> list[date]:
    from autotrader.equity.session import market_date

    seen: list[date] = []
    last = None
    for ts in frame["timestamp"]:
        day = market_date(ts.to_pydatetime())
        if REGION_START <= day <= REGION_END and day != last:
            seen.append(day)
            last = day
    return seen


def spy_states() -> pd.Series:
    spy = load_frame("SPY")
    closes = spy["close"].astype("float64")
    drawdown = closes / closes.cummax() - 1.0
    state = pd.Series("drawdown", index=spy.index)
    state[drawdown >= -0.10] = "pullback"
    state[drawdown >= -0.05] = "calm"
    labelled = pd.Series(state.to_numpy(), index=pd.DatetimeIndex(spy["timestamp"]))
    region = region_frame(spy)
    return labelled.loc[pd.DatetimeIndex(region["timestamp"])]


def weighted_report(result: WeightedResult, states: pd.Series) -> dict[str, object]:
    from autotrader.equity.session import market_date

    metrics = result.metrics().to_json_dict()
    curve = pd.Series(result.equity_curve, index=pd.DatetimeIndex(result.timestamps))

    day_of = [market_date(ts.to_pydatetime()) for ts in result.timestamps]
    window_returns: dict[str, float] = {}
    previous = result.initial_cash
    index = 0
    for window in FULL_WINDOWS:
        last_inside = None
        while index < len(result.timestamps) and day_of[index] <= window.end:
            last_inside = result.equity_curve[index]
            index += 1
        if last_inside is None:
            raise SystemExit(f"No bars inside {window.name}.")
        window_returns[window.name] = float(last_inside / previous - 1)
        previous = last_inside

    bar_returns = curve.pct_change().dropna()
    joined = pd.DataFrame({"ret": bar_returns}).join(pd.DataFrame({"state": states}), how="inner")
    regime = {
        str(state): {
            "bars": int(len(group)),
            "annualized_mean_return": float(group["ret"].mean() * 26 * 252),
        }
        for state, group in joined.groupby("state")
    }

    up = sum(window_returns[w] for w in POSITIVE_WINDOWS) / len(POSITIVE_WINDOWS)
    down = sum(window_returns[w] for w in NEGATIVE_WINDOWS) / len(NEGATIVE_WINDOWS)

    return {
        "label": result.label,
        "cost": result.cost_label,
        "net_return": result.net_return,
        "metrics": metrics,
        "window_returns": window_returns,
        "regime_table": regime,
        "mean_positive_window_return": up,
        "mean_negative_window_return": down,
        "forced_liquidation_net": result.forced_liquidation_net,
        "fills": result.fill_count,
        "turnover": result.turnover,
        "exposure_mean": result.exposure_mean,
        "mean_active_names": result.mean_active_names,
        "max_active_names": result.max_active_names,
        "max_symbol_weight_assigned": result.max_symbol_weight_assigned,
    }


class UniverseContext:
    """Everything the strategies share for one universe, loaded once."""

    def __init__(self, universe: list[str]) -> None:
        self.universe = sorted(universe)
        self.frames_full = {symbol: load_frame(symbol) for symbol in self.universe}
        self.frames = {s: region_frame(f) for s, f in self.frames_full.items()}
        self.participate = participation_map()
        self.sessions = region_sessions_of(self.frames_full["SPY"])
        self.stance = {symbol: load_stance(symbol, self.frames[symbol]) for symbol in self.universe}
        self.closes = close_table(self.frames_full)
        self.states = spy_states()
        self.reserved = min(1.0 / len(self.universe), PER_SYMBOL_CAP)

    def evaluate(self, label: str, active_weight_of, membership) -> dict[str, object]:
        targets = build_targets(
            self.frames,
            self.sessions,
            self.participate,
            membership,
            self.stance,
            active_weight_of=active_weight_of,
            reserved_weight=self.reserved,
        )
        blocks: dict[str, object] = {}
        for cost_model in COST_MODELS:
            result = replay_weighted(self.frames, targets, cost_model, label=label)
            blocks[cost_model.label] = weighted_report(result, self.states)
        return blocks


def equal_weights(members: tuple[str, ...], slots: int) -> dict[str, float]:
    if not members:
        return {}
    weight = min(1.0 / slots, PER_SYMBOL_CAP)
    return dict.fromkeys(members, weight)


def run_base(universe_name: str) -> None:
    context = UniverseContext(load_universe(universe_name))
    m = len(context.universe)
    out = Path(REPORT_ROOT) / "phase2" / f"base_{universe_name}.json"

    all_members = {session: tuple(context.universe) for session in context.sessions}
    payload: dict[str, object] = {"universe": context.universe, "size": m}

    # Equal-weight buy-and-hold of the universe.
    bh_weights = {
        session: equal_weights(tuple(context.universe), m) for session in context.sessions
    }
    always_on = dict.fromkeys(context.sessions, True)
    bh_targets = build_targets(
        context.frames,
        context.sessions,
        always_on,
        all_members,
        context.stance,
        active_weight_of=bh_weights,
        reserved_weight=context.reserved,
    )
    blocks = {}
    for cost_model in COST_MODELS:
        result = replay_weighted(context.frames, bh_targets, cost_model, label="BH_EW")
        blocks[cost_model.label] = weighted_report(result, context.states)
    payload["BH_EW"] = blocks

    # The base strategy: regime overlay + all universe names while on.
    weights = {session: equal_weights(tuple(context.universe), m) for session in context.sessions}
    payload["ALL_ELIGIBLE"] = context.evaluate("ALL_ELIGIBLE", weights, all_members)

    write_json(out, payload)
    _log(f"base {universe_name}: done")


def selection_rules(context: UniverseContext) -> dict[str, dict[date, tuple[str, ...]]]:
    """Membership per predeclared rule; ETFs and stocks compete as declared."""
    rs126 = trailing_return(context.closes, RS_PRIMARY)
    rs63 = trailing_return(context.closes, RS_SECONDARY)
    trend = above_sma(context.closes)
    marks = rebalance_sessions(context.sessions)

    def sector_ok_at(mark: date, symbol: str) -> bool:
        sector = SECTOR_OF.get(symbol, symbol)  # ETFs map to themselves
        if sector not in context.closes.columns:
            return True  # broad ETFs and unmapped names: always eligible
        return bool(trend.loc[mark, sector]) if mark in trend.index else False

    rules: dict[str, dict[date, tuple[str, ...]]] = {}
    for rule_name, scores_tbl, need_trend, need_sector in (
        ("CS_A_rs126", rs126, False, False),
        ("CS_A_rs63", rs63, False, False),
        ("CS_B_trend_rs126", rs126, True, False),
        ("CS_C_sector_rs126", rs126, True, True),
    ):
        for top_n in (10, 15):
            select_at: dict[date, tuple[str, ...]] = {}
            for mark in marks:
                if mark not in scores_tbl.index:
                    select_at[mark] = ()
                    continue
                row = scores_tbl.loc[mark]
                eligible = list(context.universe)
                if need_trend:
                    eligible = [
                        s for s in eligible if mark in trend.index and bool(trend.loc[mark, s])
                    ]
                if need_sector:
                    eligible = [s for s in eligible if sector_ok_at(mark, s)]
                ranked = rank_symbols(row.to_dict(), eligible)
                select_at[mark] = tuple(ranked[:top_n])
            rules[f"{rule_name}_top{top_n}"] = build_membership(context.sessions, select_at)
    return rules


def run_selection(universe_name: str) -> None:
    context = UniverseContext(load_universe(universe_name))
    out = Path(REPORT_ROOT) / "phase3" / f"selection_{universe_name}.json"
    payload: dict[str, object] = {"universe": context.universe}

    for rule_name, membership in selection_rules(context).items():
        top_n = int(rule_name.rsplit("top", 1)[1])
        weights = {
            session: equal_weights(membership[session], top_n) for session in context.sessions
        }
        started = time.perf_counter()
        payload[rule_name] = context.evaluate(rule_name, weights, membership)
        _log(f"{rule_name}: done in {time.perf_counter() - started:.0f}s")

    write_json(out, payload)
    _log(f"selection {universe_name}: done")


def inverse_vol_weights(
    context: UniverseContext,
    membership: dict[date, tuple[str, ...]],
    slots: int,
) -> dict[date, dict[str, float]]:
    """AL-C: weights ∝ 1/σ (trailing 63-session vol of session returns,
    lagged), recomputed at rebalance marks, capped, residual to cash."""
    returns = context.closes.pct_change()
    vol = returns.rolling(63).std().shift(1)
    marks = rebalance_sessions(context.sessions)
    current: dict[str, float] = {}
    result: dict[date, dict[str, float]] = {}
    for session in context.sessions:
        if session in marks or not current:
            members = membership.get(session, ())
            weights: dict[str, float] = {}
            if members and session in vol.index:
                inv = {}
                for symbol in members:
                    v = vol.loc[session, symbol] if symbol in vol.columns else float("nan")
                    if pd.notna(v) and v > 0:
                        inv[symbol] = 1.0 / float(v)
                total = sum(inv.values())
                if total > 0:
                    budget = min(1.0, slots * PER_SYMBOL_CAP)
                    weights = {
                        s: min(budget * share / total, PER_SYMBOL_CAP) for s, share in inv.items()
                    }
            current = weights
        result[session] = current
    return result


def run_allocators(universe_name: str, rule_name: str) -> None:
    context = UniverseContext(load_universe(universe_name))
    membership = selection_rules(context)[rule_name]
    top_n = int(rule_name.rsplit("top", 1)[1])
    out = Path(REPORT_ROOT) / "phase4" / f"allocators_{universe_name}_{rule_name}.json"
    payload: dict[str, object] = {"universe": context.universe, "rule": rule_name}

    # AL-A equal-active: 1/|A| capped.
    weights_a = {
        session: (
            dict.fromkeys(
                membership[session],
                min(1.0 / len(membership[session]), PER_SYMBOL_CAP),
            )
            if membership[session]
            else {}
        )
        for session in context.sessions
    }
    payload["AL_A_equal_active"] = context.evaluate("AL_A", weights_a, membership)

    # AL-B reserved-slot: 1/top_n capped, idle when fewer eligible.
    weights_b = {session: equal_weights(membership[session], top_n) for session in context.sessions}
    payload["AL_B_reserved"] = context.evaluate("AL_B", weights_b, membership)

    # AL-C inverse-volatility.
    weights_c = inverse_vol_weights(context, membership, top_n)
    payload["AL_C_inverse_vol"] = context.evaluate("AL_C", weights_c, membership)

    write_json(out, payload)
    _log(f"allocators {universe_name}/{rule_name}: done")


def run_bridge() -> None:
    """T1 through the weighted machinery on U10 (ledger §L10's bridge)."""
    context = UniverseContext(list(INCUMBENTS))
    membership = {session: tuple(sorted(INCUMBENTS)) for session in context.sessions}
    weights = {
        session: equal_weights(tuple(sorted(INCUMBENTS)), 10) for session in context.sessions
    }
    payload = {
        "universe": context.universe,
        "EDA1_weighted_bridge": context.evaluate("EDA1_BRIDGE", weights, membership),
    }
    write_json(Path(REPORT_ROOT) / "phase2" / "bridge_u10.json", payload)
    _log("bridge: done")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", required=True, choices=("base", "selection", "allocators", "bridge")
    )
    parser.add_argument("--universe", default="u30")
    parser.add_argument("--rule", default=None)
    arguments = parser.parse_args()

    started = time.perf_counter()
    if arguments.stage == "base":
        run_base(arguments.universe)
    elif arguments.stage == "selection":
        run_selection(arguments.universe)
    elif arguments.stage == "allocators":
        if not arguments.rule:
            raise SystemExit("--rule is required for allocators.")
        run_allocators(arguments.universe, arguments.rule)
    else:
        run_bridge()
    _log(f"stage {arguments.stage} complete in {time.perf_counter() - started:.0f}s")


if __name__ == "__main__":
    main()
