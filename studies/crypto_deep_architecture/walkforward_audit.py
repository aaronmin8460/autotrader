"""Iteration 1, Phases 2-3: the walk-forward predictability audit.

For each (symbol, horizon) cell and each development window W01..W05, the
three shipped model families - class-frequency null, L2 logistic, small
gradient-boosted ensemble - are fitted on strictly-prior data and scored on
the window. The question is exactly the falsification test the journal fixed
in advance: does any family beat the null's log loss materially and
consistently, and does its top-decile probability slice carry positive net
cost-adjusted forward return out of sample?

Leakage protections, all structural:

- Training rows are selected on `label_knowable_at` at least 24 hours (96
  bars) before the test window opens, so no training outcome resolves inside
  or near the window.
- The standardizer is fitted on the fit split alone; isotonic calibration is
  fitted on the chronologically later calibration split's uncalibrated
  scores; test rows are touched once.
- The probability-selection threshold used for the economic translation is
  the 90th percentile of *calibration-split* probabilities, never of test
  probabilities.

Checkpointing: one JSON per (symbol, horizon, window) cell under
`phase3/cells/`; a finished cell is durable before the next starts, and the
runner skips cells whose file already exists, so a restart loses only the
cell in flight.
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
    log_loss,
    roc_auc,
)
from studies.crypto_deep_architecture.data import (
    DEVELOPMENT_WINDOWS,
    WINDOWS,
    exact_break_even,
    load_symbol_frame,
    shared_grid,
    window_mask,
)
from studies.crypto_deep_architecture.features_ext import (
    EXTENSION_FEATURES,
    compute_extension_features,
)
from studies.crypto_deep_architecture.run_phase1 import cost_adjusted, forward_return_label

OUTPUT_DIR = Path("/Volumes/AUTOTRADER_QA/reports/crypto-deep-architecture/phase3")

SYMBOLS = ("BTC/USD", "ETH/USD")
HORIZONS = (16, 32, 96)

#: Features whose NaN means "denominator degenerate on a bar that exists";
#: imputed to the neutral 0.0 exactly as the live engines fall back to
#: neutral. Every other NaN drops its row.
NEUTRAL_IMPUTED = ("volume_ratio_32", "close_position_in_bar")

#: Chronological share of the training rows used to fit; the rest calibrates.
FIT_FRACTION = 0.7

#: Embargo between the last usable `label_knowable_at` and the window start.
EMBARGO = pd.Timedelta("24h")

ALL_FEATURES: tuple[str, ...] = tuple(FEATURE_NAMES) + EXTENSION_FEATURES


def sigmoid(raw: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(raw, -500.0, 500.0)))


def standardize(matrix: np.ndarray, standardizer) -> np.ndarray:
    means = np.asarray(standardizer.means, dtype="float64")
    scales = np.asarray(standardizer.scales, dtype="float64")
    return (matrix - means) / scales


def score_rows(estimator, standardized: np.ndarray) -> np.ndarray:
    """Uncalibrated probabilities, produced by the estimator's own scoring path."""
    return sigmoid(np.asarray([estimator.raw_score(row) for row in standardized], dtype="float64"))


def apply_isotonic(calibration, probabilities: np.ndarray) -> np.ndarray:
    thresholds = np.asarray(calibration.thresholds, dtype="float64")
    values = np.asarray(calibration.values, dtype="float64")
    positions = np.searchsorted(thresholds, probabilities, side="right") - 1
    return values[np.clip(positions, 0, len(values) - 1)]


def build_cell_frame(symbol: str, horizon: int, frames: dict) -> pd.DataFrame:
    """Features, label and bookkeeping columns for one (symbol, horizon) cell."""
    other = {"BTC/USD": "ETH/USD", "ETH/USD": "BTC/USD"}[symbol]
    sf = frames[symbol]
    ext = compute_extension_features(
        sf.observations, sf.features, other_close=frames[other].observations["close"]
    )
    features = pd.concat([sf.features, ext], axis=1)
    for name in NEUTRAL_IMPUTED:
        present = sf.observations["is_present"].astype(bool)
        features[name] = features[name].where(features[name].notna() | ~present, 0.0)

    labels = forward_return_label(sf.observations, sf.grid, horizon)
    frame = pd.concat(
        [
            sf.observations[["timestamp"]],
            features,
            labels[["label_forward_return", "label_knowable_at", "label_valid"]],
        ],
        axis=1,
    )
    break_even = exact_break_even()
    frame["net_return"] = cost_adjusted(frame["label_forward_return"], break_even)
    frame["target"] = (frame["label_forward_return"] > break_even).astype("float64")

    usable = frame["label_valid"].fillna(False).astype(bool)
    for name in ALL_FEATURES:
        usable &= frame[name].notna()
    return frame.loc[usable].reset_index(drop=True)


def run_cell(symbol: str, horizon: int, window: str, cell_frame: pd.DataFrame) -> dict:
    window_start = pd.Timestamp(WINDOWS[window][0], tz="UTC")
    cutoff = window_start - EMBARGO
    train = cell_frame.loc[cell_frame["label_knowable_at"] <= cutoff]
    test = cell_frame.loc[window_mask(cell_frame["timestamp"], window)]

    fit_rows = int(len(train) * FIT_FRACTION)
    fit_split = train.iloc[:fit_rows]
    calibration_split = train.iloc[fit_rows:]

    matrix_fit = fit_split[list(ALL_FEATURES)].to_numpy(dtype="float64")
    matrix_cal = calibration_split[list(ALL_FEATURES)].to_numpy(dtype="float64")
    matrix_test = test[list(ALL_FEATURES)].to_numpy(dtype="float64")
    y_fit = fit_split["target"].to_numpy(dtype="float64")
    y_cal = calibration_split["target"].to_numpy(dtype="float64")
    y_test = test["target"].to_numpy(dtype="float64")
    net_test = test["net_return"].to_numpy(dtype="float64")

    standardizer = fit_standardizer(matrix_fit)
    z_fit = standardize(matrix_fit, standardizer)
    z_cal = standardize(matrix_cal, standardizer)
    z_test = standardize(matrix_test, standardizer)

    results = {}
    for candidate in default_candidates():
        started = time.time()
        estimator = fit_estimator(candidate, z_fit, y_fit)
        p_cal_raw = score_rows(estimator, z_cal)
        calibration = fit_isotonic(p_cal_raw, y_cal)
        p_cal = apply_isotonic(calibration, p_cal_raw)
        p_test = apply_isotonic(calibration, score_rows(estimator, z_test))

        tau = float(np.quantile(p_cal, 0.9))
        selected = p_test > tau
        n_selected = int(selected.sum())
        results[candidate.name] = {
            "train_rows": int(len(train)),
            "fit_rows": int(fit_rows),
            "calibration_rows": int(len(calibration_split)),
            "test_rows": int(len(test)),
            "train_base_rate": float(y_fit.mean()),
            "test_base_rate": float(y_test.mean()),
            "log_loss": log_loss(p_test, y_test),
            "roc_auc": roc_auc(p_test, y_test),
            "mean_predicted": float(p_test.mean()),
            "tau_top_decile": tau,
            "selected_rows": n_selected,
            "selected_mean_net_bps": (
                float(net_test[selected].mean() * 1e4) if n_selected else None
            ),
            "selected_hit_rate": float(y_test[selected].mean()) if n_selected else None,
            "fit_seconds": round(time.time() - started, 2),
        }
    null_ll = results["baseline-frequency"]["log_loss"]
    for name in results:
        results[name]["delta_log_loss_vs_null"] = null_ll - results[name]["log_loss"]
    return {
        "symbol": symbol,
        "horizon": horizon,
        "window": window,
        "candidates": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", choices=SYMBOLS, default=None)
    parser.add_argument("--horizon", type=int, choices=HORIZONS, default=None)
    parser.add_argument("--window", choices=DEVELOPMENT_WINDOWS, default=None)
    args = parser.parse_args()

    cells_dir = OUTPUT_DIR / "cells"
    cells_dir.mkdir(parents=True, exist_ok=True)

    grid = shared_grid()
    frames = {s: load_symbol_frame(s, grid) for s in SYMBOLS}

    symbols = (args.symbol,) if args.symbol else SYMBOLS
    horizons = (args.horizon,) if args.horizon else HORIZONS
    windows = (args.window,) if args.window else DEVELOPMENT_WINDOWS

    for symbol in symbols:
        for horizon in horizons:
            cell_frame = build_cell_frame(symbol, horizon, frames)
            for window in windows:
                tag = f"{symbol.replace('/', '_')}_h{horizon}_{window}"
                path = cells_dir / f"{tag}.json"
                if path.exists():
                    print(f"skip {tag} (checkpoint exists)")
                    continue
                started = time.time()
                record = run_cell(symbol, horizon, window, cell_frame)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(record, indent=2))
                tmp.rename(path)
                elapsed = time.time() - started
                summary = {
                    name: (
                        f"dLL={value['delta_log_loss_vs_null']:+.5f} "
                        f"auc={value['roc_auc']:.3f} "
                        f"sel_net={value['selected_mean_net_bps']}"
                    )
                    for name, value in record["candidates"].items()
                    if name != "baseline-frequency"
                }
                print(f"{tag} done in {elapsed:.0f}s: {summary}", flush=True)
    print("audit complete")


if __name__ == "__main__":
    main()
