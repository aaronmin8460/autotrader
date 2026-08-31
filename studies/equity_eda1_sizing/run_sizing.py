"""Score the three predeclared allocation policies. One run, no grid.

Usage:
    python -m studies.equity_eda1_sizing.run_sizing --stage primary
    python -m studies.equity_eda1_sizing.run_sizing --stage robustness
"""

from __future__ import annotations

import argparse
import json
import os
import time
from decimal import Decimal
from pathlib import Path

import pandas as pd
from studies.equity_eda1_sizing import STUDY_SYMBOLS, WINDOW_NAMES
from studies.equity_eda1_sizing.evidence import (
    default_datasets,
    default_decisions,
    load_session_frame,
    stance_frame,
    verify_wiring,
)
from studies.equity_eda1_sizing.simulate import (
    RebalanceRule,
    SimulationResult,
    simulate,
)

from autotrader.equity.allocation import (
    POLICY_EQUAL_ACTIVE,
    POLICY_FIXED_PRO_RATA,
    POLICY_RESERVED_UNIVERSE,
    AllocationPolicy,
)
from autotrader.research.costs import EQUITY_COST, STRESS_COST, ZERO_COST, CostModel

#: The predeclared external-exposure stress scenarios (ledger §L4). Account
#: contention, not realized crypto history.
EXTERNAL_SCENARIOS: tuple[Decimal, ...] = (Decimal("0.00"), Decimal("0.05"), Decimal("0.10"))

COST_MODELS: tuple[tuple[str, CostModel], ...] = (
    ("frictionless", ZERO_COST),
    ("equity-marketable", EQUITY_COST),
    ("stress", STRESS_COST),
)

PRIMARY_COST = "equity-marketable"

POLICIES: tuple[str, ...] = (
    POLICY_EQUAL_ACTIVE,
    POLICY_FIXED_PRO_RATA,
    POLICY_RESERVED_UNIVERSE,
)

#: Frozen from the published ten-symbol buy-and-hold window means, exactly as
#: the deep-architecture program classified them. Not recomputed here, so the
#: classification cannot move with a challenger.
POSITIVE_WINDOWS: tuple[str, ...] = ("w04", "w05", "w06", "w07", "w08", "w10", "w12")
NEGATIVE_WINDOWS: tuple[str, ...] = ("w01", "w02", "w03", "w09", "w11")


def default_output() -> Path:
    return Path(
        os.environ.get("EDA1_SIZING_OUTPUT", "/Volumes/AUTOTRADER_QA/reports/equity-eda1-sizing")
    )


def window_bounds(decisions: Path) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    """Each window's first and last bar, read off the stored SPY series itself."""
    bounds: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}
    for window in WINDOW_NAMES:
        frame = pd.read_parquet(decisions / f"SPY_{window}_V3.parquet")
        stamps = pd.DatetimeIndex(frame["timestamp"])
        bounds[window] = (stamps.min(), stamps.max())
    return bounds


def price_frames(datasets: Path, index: pd.DatetimeIndex) -> dict[str, pd.DataFrame]:
    """Open and close for every symbol, trimmed to the scored region."""
    frames: dict[str, pd.DataFrame] = {}
    for symbol in STUDY_SYMBOLS:
        raw = load_session_frame(datasets, symbol)
        frame = raw.set_index(pd.DatetimeIndex(raw["timestamp"]))[["open", "close"]]
        frames[symbol] = frame.loc[(frame.index >= index.min()) & (frame.index <= index.max())]
    return frames


def window_returns(
    result: SimulationResult,
    bounds: dict[str, tuple[pd.Timestamp, pd.Timestamp]],
) -> dict[str, float]:
    """Continuous-curve return of each window, the research program's definition."""
    stamps = pd.DatetimeIndex(result.timestamps)
    curve = list(result.equity_curve)
    returns: dict[str, float] = {}
    previous = result.initial_cash
    for window in WINDOW_NAMES:
        _, end = bounds[window]
        inside = stamps <= end
        if not inside.any():
            raise ValueError(f"No portfolio bars inside {window}.")
        last = curve[int(inside.sum()) - 1]
        returns[window] = float(last / previous - 1)
        previous = last
    return returns


def capture(returns: dict[str, float], benchmark: dict[str, float]) -> dict[str, float]:
    """Up/down capture on the frozen window classification."""
    up = sum(returns[w] for w in POSITIVE_WINDOWS) / len(POSITIVE_WINDOWS)
    up_bench = sum(benchmark[w] for w in POSITIVE_WINDOWS) / len(POSITIVE_WINDOWS)
    down = sum(returns[w] for w in NEGATIVE_WINDOWS) / len(NEGATIVE_WINDOWS)
    down_bench = sum(benchmark[w] for w in NEGATIVE_WINDOWS) / len(NEGATIVE_WINDOWS)
    return {
        "up_capture": up / up_bench if up_bench else float("nan"),
        "down_capture": down / down_bench if down_bench else float("nan"),
        "mean_positive_window_return": up,
        "mean_negative_window_return": down,
    }


def spy_drawdown_states(datasets: Path, index: pd.DatetimeIndex) -> pd.Series:
    """The published causal labelling: SPY trailing-peak drawdown state per bar."""
    frame = load_session_frame(datasets, "SPY")
    closes = frame["close"].astype("float64")
    drawdown = closes / closes.cummax() - 1.0
    state = pd.Series("drawdown", index=frame.index)
    state[drawdown >= -0.10] = "pullback"
    state[drawdown >= -0.05] = "calm"
    labelled = pd.Series(state.to_numpy(), index=pd.DatetimeIndex(frame["timestamp"]))
    return labelled.reindex(index)


def regime_table(result: SimulationResult, states: pd.Series) -> dict[str, dict[str, float]]:
    """Annualized mean portfolio bar return under each SPY drawdown state."""
    curve = pd.Series(
        [float(value) for value in result.equity_curve],
        index=pd.DatetimeIndex(result.timestamps),
    )
    returns = curve.pct_change().dropna()
    joined = pd.DataFrame({"ret": returns}).join(pd.DataFrame({"state": states}), how="inner")
    table: dict[str, dict[str, float]] = {}
    for state, group in joined.groupby("state"):
        table[str(state)] = {
            "bars": int(len(group)),
            "annualized_mean_return": float(group["ret"].mean() * 26 * 252),
        }
    return table


def describe(result: SimulationResult, cost_model: CostModel) -> dict[str, object]:
    """One simulation's reportable figures."""
    metrics = result.metrics()
    return {
        "label": result.label,
        "policy_id": result.policy_id,
        "cost": result.cost_label,
        "rule": result.rule,
        "external_exposure_fraction": str(result.external_exposure_fraction),
        "net_return": result.net_return,
        "sharpe_ratio": metrics.sharpe_ratio,
        "sortino_ratio": metrics.sortino_ratio,
        "max_drawdown": metrics.max_drawdown,
        "volatility_annualized": metrics.volatility_annualized,
        "exposure": metrics.exposure,
        "turnover": metrics.turnover,
        "cost_drag": metrics.cost_drag,
        "fill_count": result.fill_count,
        "bar_count": metrics.bar_count,
        "max_symbol_weight": str(result.max_symbol_weight),
        "max_total_weight": str(result.max_total_weight),
        "max_realized_symbol_fraction": result.max_realized_symbol_fraction,
        "max_realized_total_fraction": result.max_realized_total_fraction,
        "weight_asymmetry_bars": result.weight_asymmetry_bars,
        "forced_liquidation_net": result.forced_liquidation_net(cost_model),
    }


def run_one(
    *,
    label: str,
    stances: pd.DataFrame,
    frames: dict[str, pd.DataFrame],
    policy: AllocationPolicy,
    cost_label: str,
    cost_model: CostModel,
    external: Decimal,
    symbols: tuple[str, ...] = STUDY_SYMBOLS,
    rule: RebalanceRule = RebalanceRule.WHOLE_SHARE,
) -> SimulationResult:
    return simulate(
        label=label,
        stances=stances,
        frames=frames,
        policy=policy,
        cost_model=cost_model,
        cost_label=cost_label,
        external_exposure_fraction=external,
        symbols=symbols,
        rule=rule,
    )


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def stage_primary(datasets: Path, decisions: Path, output: Path) -> None:
    started = time.perf_counter()
    eda1, v3, summary = stance_frame(datasets, decisions)
    verify_wiring(summary)

    index = pd.DatetimeIndex(eda1.index)
    frames = price_frames(datasets, index)
    bounds = window_bounds(decisions)
    states = spy_drawdown_states(datasets, index)
    buy_hold = pd.DataFrame(True, index=eda1.index, columns=list(STUDY_SYMBOLS)).astype("boolean")

    engines = {"EDA1": eda1, "V3": v3, "BUY_AND_HOLD": buy_hold}
    results: dict[str, object] = {
        "participation": summary,
        "wiring_check": "PASS",
        "policies": {},
    }

    for policy_id in POLICIES:
        policy = AllocationPolicy(policy_id=policy_id)
        entry: dict[str, object] = {
            "config": policy.to_json_dict(),
            "config_hash": policy.config_hash(),
            "scenarios": {},
        }
        for external in EXTERNAL_SCENARIOS:
            scenario: dict[str, object] = {}
            window_by_engine: dict[str, dict[str, float]] = {}
            for engine_name, stance in engines.items():
                per_cost: dict[str, object] = {}
                for cost_label, cost_model in COST_MODELS:
                    result = run_one(
                        label=f"{engine_name}/{policy_id}/X={external}",
                        stances=stance,
                        frames=frames,
                        policy=policy,
                        cost_label=cost_label,
                        cost_model=cost_model,
                        external=external,
                    )
                    per_cost[cost_label] = describe(result, cost_model)
                    if cost_label == PRIMARY_COST:
                        window_by_engine[engine_name] = window_returns(result, bounds)
                        per_cost[cost_label]["regime_table"] = regime_table(result, states)
                        per_cost[cost_label]["window_returns"] = window_by_engine[engine_name]
                scenario[engine_name] = per_cost
            scenario["capture"] = {
                "EDA1_vs_BH": capture(window_by_engine["EDA1"], window_by_engine["BUY_AND_HOLD"]),
                "V3_vs_BH": capture(window_by_engine["V3"], window_by_engine["BUY_AND_HOLD"]),
            }
            scenario["paired_window_diff_EDA1_minus_V3"] = {
                window: window_by_engine["EDA1"][window] - window_by_engine["V3"][window]
                for window in WINDOW_NAMES
            }
            entry["scenarios"][str(external)] = scenario  # type: ignore[index]
        results["policies"][policy_id] = entry  # type: ignore[index]

    results["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    write_json(output / "primary.json", results)
    print(f"primary: {results['elapsed_seconds']}s -> {output / 'primary.json'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("primary", "robustness"))
    parser.add_argument("--datasets", type=Path, default=default_datasets())
    parser.add_argument("--decisions", type=Path, default=default_decisions())
    parser.add_argument("--output", type=Path, default=default_output())
    args = parser.parse_args()
    if args.stage == "primary":
        stage_primary(args.datasets, args.decisions, args.output)
    else:
        from studies.equity_eda1_sizing.run_robustness import stage_robustness

        stage_robustness(args.datasets, args.decisions, args.output)


if __name__ == "__main__":
    main()
