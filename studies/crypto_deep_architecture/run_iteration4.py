"""Iteration 4: the two-sided directional-expectancy architecture.

Per (symbol, window) fold: fit two binary models on strictly-prior data -
P(forward 96-bar return > +break-even) and P(< -break-even) - calibrate each
isotonically on the chronologically later calibration split, and replay a
daily-stride long/flat policy on the spread of the calibrated probabilities.
Enter above the calibration split's 80th spread percentile, exit below its
median; fills at the next bar's open under each cost model.

Checkpoints one JSON per (symbol, window, family) under `iteration4/cells/`.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from autotrader.ml.features import FEATURE_NAMES
from autotrader.ml.v4 import (
    default_candidates,
    fit_estimator,
    fit_isotonic,
    fit_standardizer,
    roc_auc,
)
from autotrader.research.costs import cost_model_for
from studies.crypto_deep_architecture.data import (
    WINDOWS,
    exact_break_even,
    extended_grid,
    load_extended_symbol_frame,
    load_symbol_frame,
    shared_grid,
    window_mask,
)
from studies.crypto_deep_architecture.features_ext import (
    EXTENSION_FEATURES,
    compute_extension_features,
)
from studies.crypto_deep_architecture.run_iteration2 import window_bounds
from studies.crypto_deep_architecture.run_phase1 import forward_return_label
from studies.crypto_deep_architecture.trend_rules import FLAT, LONG, replay
from studies.crypto_deep_architecture.walkforward_audit import (
    NEUTRAL_IMPUTED,
    apply_isotonic,
    score_rows,
    standardize,
)

OUTPUT_DIR = Path("/Volumes/AUTOTRADER_QA/reports/crypto-deep-architecture/iteration4")

SYMBOLS = ("BTC/USD", "ETH/USD")
FOLDS = ("P3", "W01", "W02", "W03", "W04", "W05")
FOLDS_EXTENDED = tuple(f"X{i:02d}" for i in range(1, 10))
COST_MODELS = ("frictionless", "crypto-taker", "stress")
HORIZON = 96
FIT_FRACTION = 0.7
EMBARGO = pd.Timedelta("24h")
BARS_PER_DAY = 96

#: Entry/exit spread quantiles: (name, enter, exit). The 80/50 pair is the
#: declared rule; 70/50 and 90/50 are bounded sensitivity, reported only.
GATES = (("q80", 0.8, 0.5), ("q70", 0.7, 0.5), ("q90", 0.9, 0.5))

#: I1's 22 features plus the two evidence-driven long-trend extensions.
TREND_EXTRAS = ("return_1344", "return_2688")
ALL_FEATURES: tuple[str, ...] = tuple(FEATURE_NAMES) + EXTENSION_FEATURES + TREND_EXTRAS

FAMILIES = ("logistic-l2", "gradient-boosted")


def build_frame(symbol: str, frames: dict) -> pd.DataFrame:
    """Features + two-sided targets + bookkeeping for one symbol at h=96."""
    other = {"BTC/USD": "ETH/USD", "ETH/USD": "BTC/USD"}[symbol]
    sf = frames[symbol]
    ext = compute_extension_features(
        sf.observations, sf.features, other_close=frames[other].observations["close"]
    )
    features = pd.concat([sf.features, ext], axis=1)
    close = sf.observations["close"].astype("float64")
    for lookback in (1344, 2688):
        past = close.shift(lookback)
        features[f"return_{lookback}"] = close / past.where(past > 0.0) - 1.0
    present = sf.observations["is_present"].astype(bool)
    for name in NEUTRAL_IMPUTED:
        features[name] = features[name].where(features[name].notna() | ~present, 0.0)

    labels = forward_return_label(sf.observations, sf.grid, HORIZON)
    break_even = exact_break_even()
    frame = pd.concat(
        [
            sf.observations[["timestamp", "session_bar_index"]],
            features,
            labels[["label_forward_return", "label_knowable_at", "label_valid"]],
        ],
        axis=1,
    )
    frame["target_up"] = (frame["label_forward_return"] > break_even).astype("float64")
    frame["target_down"] = (frame["label_forward_return"] < -break_even).astype("float64")
    frame["grid_position"] = np.arange(len(frame))
    usable = frame["label_valid"].fillna(False).astype(bool)
    for name in ALL_FEATURES:
        usable &= frame[name].notna()
    return frame.loc[usable].reset_index(drop=True)


def fit_spread_model(train: pd.DataFrame, family: str) -> dict:
    """Fit both targets and their calibrations; return the scoring bundle."""
    fit_rows = int(len(train) * FIT_FRACTION)
    fit_split = train.iloc[:fit_rows]
    calibration_split = train.iloc[fit_rows:]
    matrix_fit = fit_split[list(ALL_FEATURES)].to_numpy(dtype="float64")
    matrix_cal = calibration_split[list(ALL_FEATURES)].to_numpy(dtype="float64")
    standardizer = fit_standardizer(matrix_fit)
    z_fit = standardize(matrix_fit, standardizer)
    z_cal = standardize(matrix_cal, standardizer)

    candidate = next(c for c in default_candidates() if c.name == family)
    bundle: dict = {"standardizer": standardizer, "sides": {}}
    spreads_cal = None
    for side in ("up", "down"):
        y_fit = fit_split[f"target_{side}"].to_numpy(dtype="float64")
        y_cal = calibration_split[f"target_{side}"].to_numpy(dtype="float64")
        estimator = fit_estimator(candidate, z_fit, y_fit)
        raw_cal = score_rows(estimator, z_cal)
        calibration = fit_isotonic(raw_cal, y_cal)
        calibrated_cal = apply_isotonic(calibration, raw_cal)
        bundle["sides"][side] = {"estimator": estimator, "calibration": calibration}
        spreads_cal = calibrated_cal if spreads_cal is None else spreads_cal - calibrated_cal
    bundle["cal_spread_quantiles"] = {
        q: float(np.quantile(spreads_cal, q)) for q in (0.5, 0.7, 0.8, 0.9)
    }
    bundle["cal_rows"] = int(len(calibration_split))
    return bundle


def spread_for(bundle: dict, matrix: np.ndarray) -> np.ndarray:
    z = standardize(matrix, bundle["standardizer"])
    spread = None
    for side, sign in (("up", 1.0), ("down", -1.0)):
        part = bundle["sides"][side]
        p = apply_isotonic(part["calibration"], score_rows(part["estimator"], z))
        spread = sign * p if spread is None else spread + sign * p
    return spread


def run_fold(symbol: str, window: str, family: str, frame: pd.DataFrame, frames: dict) -> dict:
    window_start = pd.Timestamp(WINDOWS[window][0], tz="UTC")
    train = frame.loc[frame["label_knowable_at"] <= window_start - EMBARGO]
    test = frame.loc[window_mask(frame["timestamp"], window)]
    bundle = fit_spread_model(train, family)

    observations = frames[symbol].observations
    timestamps = frames[symbol].timestamps
    start, end = window_bounds(timestamps, window)

    decision_rows = test.loc[test["session_bar_index"] == BARS_PER_DAY - 1]
    spread = spread_for(bundle, decision_rows[list(ALL_FEATURES)].to_numpy(dtype="float64"))
    positions = decision_rows["grid_position"].to_numpy(dtype="int64")

    net_returns = decision_rows["label_forward_return"].to_numpy(dtype="float64")
    up_actual = decision_rows["target_up"].to_numpy(dtype="float64")
    diagnostics = {
        "decision_days": int(len(decision_rows)),
        "spread_rank_ic_net": (
            float(pd.Series(spread).rank().corr(pd.Series(net_returns).rank()))
            if len(decision_rows) > 10
            else None
        ),
        "spread_auc_up": roc_auc(spread, up_actual) if len(decision_rows) > 10 else None,
        "train_rows": int(len(train)),
    }

    gates_out = {}
    for gate_name, enter_q, exit_q in GATES:
        tau_enter = bundle["cal_spread_quantiles"][enter_q]
        tau_exit = bundle["cal_spread_quantiles"][exit_q]
        states = np.zeros(len(observations), dtype="int8")
        state = FLAT
        cursor = 0
        for index in range(start, end + 1):
            while cursor < len(positions) and positions[cursor] == index:
                value = spread[cursor]
                if state == FLAT and value > tau_enter:
                    state = LONG
                elif state == LONG and value < tau_exit:
                    state = FLAT
                cursor += 1
            states[index] = state
        per_cost = {}
        for cost_label in COST_MODELS:
            result = replay(observations, states, cost_model_for(cost_label), start=start, end=end)
            per_cost[cost_label] = {
                "net_return": result.net_return,
                "forced_return": result.forced_liquidation_return,
                "trades": result.trades,
                "time_in_market": result.time_in_market,
                "max_drawdown": result.max_drawdown,
                "open_at_end": result.open_position_at_end,
                "realized_pnl": result.realized_pnl,
                "unrealized_pnl": result.unrealized_pnl,
            }
        gates_out[gate_name] = {
            "tau_enter": tau_enter,
            "tau_exit": tau_exit,
            "costs": per_cost,
        }
    return {
        "symbol": symbol,
        "window": window,
        "family": family,
        "diagnostics": diagnostics,
        "gates": gates_out,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", default=None)
    parser.add_argument("--family", default=None, choices=FAMILIES)
    parser.add_argument(
        "--features",
        default="full",
        choices=("full", "base13"),
        help="base13 ablates the extension features (journal-declared attack)",
    )
    parser.add_argument(
        "--era",
        default="modern",
        choices=("modern", "extended"),
        help="extended scores the frozen candidate on the 2021-2023 attack era",
    )
    args = parser.parse_args()

    if args.features == "base13":
        global ALL_FEATURES
        ALL_FEATURES = tuple(FEATURE_NAMES)
    suffix = "" if args.features == "full" else "_base13"
    if args.era == "extended":
        suffix = "_extended" + suffix
    cells_dir = OUTPUT_DIR / f"cells{suffix}"
    cells_dir.mkdir(parents=True, exist_ok=True)
    if args.era == "extended":
        grid = extended_grid()
        frames = {s: load_extended_symbol_frame(s, grid) for s in SYMBOLS}
        default_folds = FOLDS_EXTENDED
    else:
        grid = shared_grid()
        frames = {s: load_symbol_frame(s, grid) for s in SYMBOLS}
        default_folds = FOLDS

    windows = (args.window,) if args.window else default_folds
    families = (args.family,) if args.family else FAMILIES

    for symbol in SYMBOLS:
        frame = build_frame(symbol, frames)
        for family in families:
            for window in windows:
                tag = f"{symbol.replace('/', '_')}_{family}_{window}"
                path = cells_dir / f"{tag}.json"
                if path.exists():
                    print(f"skip {tag}")
                    continue
                started = time.time()
                record = run_fold(symbol, window, family, frame, frames)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(record, indent=2, default=str))
                tmp.rename(path)
                headline = record["gates"]["q80"]["costs"]["crypto-taker"]
                print(
                    f"{tag} done in {time.time() - started:.0f}s "
                    f"forced={headline['forced_return']:+.4f} trades={headline['trades']} "
                    f"ic={record['diagnostics']['spread_rank_ic_net']}",
                    flush=True,
                )
    print("iteration 4 complete")


if __name__ == "__main__":
    main()
