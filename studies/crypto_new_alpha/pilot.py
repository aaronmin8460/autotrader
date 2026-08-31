"""The incremental-information pilot: arms x windows x horizons x families.

Both model families run on identical rows, identical chronological splits,
identical standardizer policy, identical isotonic calibration - the only
thing that varies between arms is the feature list (search-ledger.md §6-7).

Per cell (arm, symbol, window, horizon) this fits:

* Model 0 - the class-frequency null (train base rate, predicted flat)
* Model 2 - in-repo L2 logistic (lambda = 1.0, fixed; no search)
* Model 3 - in-repo gradient boosting (the DA-frozen hyperparameters; no search)

and reports predictive metrics on every usable row of the window plus the
daily-stride economic read: mean gross forward return conditional on the
calibrated score's calibration-split quintiles, and the top-minus-bottom
spread in bps, measured against nothing but the label - no trading rule, no
threshold tuned on test data.

Checkpoints: one JSON per cell, temp-file + rename; a rerun skips complete
cells, so an interrupted run loses only the cell in flight.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

from autotrader.ml.v4 import fit_gradient_boosted as fit_gbt
from autotrader.ml.v4 import fit_isotonic, fit_logistic, fit_standardizer
from studies.crypto_new_alpha.frames import (
    BARS_PER_DAY,
    BASELINE_FEATURES,
    HORIZONS,
    PRIMARY_HORIZON,
    SYMBOLS,
    study_frames,
)
from studies.crypto_new_alpha.frozen_data import WINDOWS, window_mask
from studies.crypto_new_alpha.new_features import (
    FLOW_FEATURES,
    INTERACTION_FEATURES,
    LIQPROXY_FEATURES,
    OI_FEATURES,
)

OUTPUT_DIR = Path("/Volumes/AUTOTRADER_QA/reports/crypto-new-alpha-oi-liq-flow/models")
CELLS_DIR = OUTPUT_DIR / "cells"
PROGRESS_LOG = Path("/Volumes/AUTOTRADER_QA/logs/crypto-new-alpha-pilot.log")

MODERN_WINDOWS = ("P3", "W01", "W02", "W03", "W04", "W05", "W06", "W07")
EXTENDED_WINDOWS = tuple(f"X{i:02d}" for i in range(1, 10))
ALL_WINDOWS = MODERN_WINDOWS + EXTENDED_WINDOWS

#: W07 is the final holdout: excluded from default runs, scored once at the end.
HOLDOUT_WINDOW = "W07"
DEFAULT_WINDOWS = tuple(w for w in ALL_WINDOWS if w != HOLDOUT_WINDOW)

#: Predeclared arms (search-ledger.md §7). The feature lists are disjoint
#: unions of the frozen baseline and the new families - nothing else.
ARM_FEATURES: dict[str, tuple[str, ...]] = {
    "baseline": BASELINE_FEATURES,
    "full": BASELINE_FEATURES
    + OI_FEATURES
    + FLOW_FEATURES
    + LIQPROXY_FEATURES
    + INTERACTION_FEATURES,
    "oi_only": BASELINE_FEATURES + OI_FEATURES,
    "flow_only": BASELINE_FEATURES + FLOW_FEATURES,
    "liqproxy_only": BASELINE_FEATURES + LIQPROXY_FEATURES,
    "oi_flow": BASELINE_FEATURES + OI_FEATURES + FLOW_FEATURES,
    "oi_liqproxy": BASELINE_FEATURES + OI_FEATURES + LIQPROXY_FEATURES,
}
MAIN_ARMS = ("baseline", "full")
ABLATION_ARMS = ("oi_only", "flow_only", "liqproxy_only", "oi_flow", "oi_liqproxy")

FIT_FRACTION = 0.7
EMBARGO = pd.Timedelta("24h")
MIN_TRAIN_ROWS = 500
MIN_TEST_ROWS = 50
LOG_LOSS_EPSILON = 1e-15

#: Calibration-split quantiles that define the economic quintiles.
QUINTILE_LOW = 0.2
QUINTILE_HIGH = 0.8


def sigmoid(raw: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(raw, -500.0, 500.0)))


def standardize(matrix: np.ndarray, standardizer) -> np.ndarray:
    means = np.asarray(standardizer.means, dtype="float64")
    scales = np.asarray(standardizer.scales, dtype="float64")
    return (matrix - means) / scales


def apply_isotonic(calibration, probabilities: np.ndarray) -> np.ndarray:
    thresholds = np.asarray(calibration.thresholds, dtype="float64")
    values = np.asarray(calibration.values, dtype="float64")
    positions = np.searchsorted(thresholds, probabilities, side="right") - 1
    return values[np.clip(positions, 0, len(values) - 1)]


def score_logistic(estimator, standardized: np.ndarray) -> np.ndarray:
    coefficients = np.asarray(estimator.coefficients, dtype="float64")
    return sigmoid(float(estimator.intercept) + standardized @ coefficients)


def score_gbt(estimator, standardized: np.ndarray) -> np.ndarray:
    return sigmoid(np.asarray([estimator.raw_score(row) for row in standardized], dtype="float64"))


def log_loss_of(probabilities: np.ndarray, actual: np.ndarray) -> float:
    clipped = np.clip(probabilities, LOG_LOSS_EPSILON, 1.0 - LOG_LOSS_EPSILON)
    return float(-np.mean(actual * np.log(clipped) + (1.0 - actual) * np.log(1.0 - clipped)))


def roc_auc_of(probabilities: np.ndarray, actual: np.ndarray) -> float | None:
    positives = actual.sum()
    if positives == 0 or positives == len(actual):
        return None
    # midranks for ties, so calibrated step-function scores are handled exactly
    ranks = pd.Series(probabilities).rank(method="average").to_numpy()
    positive_ranks = ranks[actual > 0.5].sum()
    negatives = len(actual) - positives
    return float((positive_ranks - positives * (positives + 1) / 2) / (positives * negatives))


def calibration_error(
    probabilities: np.ndarray, actual: np.ndarray, bins: int = 10
) -> float | None:
    if len(probabilities) < bins:
        return None
    edges = np.linspace(0.0, 1.0, bins + 1)
    index = np.clip(np.digitize(probabilities, edges[1:-1], right=False), 0, bins - 1)
    total = 0.0
    for bucket in range(bins):
        mask = index == bucket
        count = int(mask.sum())
        if count:
            total += count * abs(float(probabilities[mask].mean()) - float(actual[mask].mean()))
    return float(total / len(probabilities))


def prediction_metrics(probabilities: np.ndarray, actual: np.ndarray, forward: np.ndarray) -> dict:
    return {
        "log_loss": log_loss_of(probabilities, actual),
        "brier": float(np.mean((probabilities - actual) ** 2)),
        "roc_auc": roc_auc_of(probabilities, actual),
        "ece": calibration_error(probabilities, actual),
        "rank_ic": (
            float(pd.Series(probabilities).rank().corr(pd.Series(forward).rank()))
            if len(forward) > 10
            else None
        ),
        "mean_prediction": float(probabilities.mean()),
        "prediction_std": float(probabilities.std()),
        "base_rate": float(actual.mean()),
        "rows": int(len(actual)),
    }


def economic_read(
    calibrated: np.ndarray,
    forward: np.ndarray,
    day_mask: np.ndarray,
    low_threshold: float,
    high_threshold: float,
) -> dict:
    """Daily-stride conditional forward returns by calibration-split quintile."""
    scores = calibrated[day_mask]
    returns = forward[day_mask]
    top = scores >= high_threshold
    bottom = scores <= low_threshold
    out = {
        "decision_days": int(day_mask.sum()),
        "top_n": int(top.sum()),
        "bottom_n": int(bottom.sum()),
        "top_mean_bps": float(np.mean(returns[top])) * 1e4 if top.any() else None,
        "bottom_mean_bps": float(np.mean(returns[bottom])) * 1e4 if bottom.any() else None,
        "all_mean_bps": float(np.mean(returns)) * 1e4 if len(returns) else None,
    }
    if top.any() and bottom.any():
        out["spread_bps"] = out["top_mean_bps"] - out["bottom_mean_bps"]
        membership = np.where(top, 1, np.where(bottom, -1, 0))
        out["signal_change_rate"] = (
            float(np.mean(membership[1:] != membership[:-1])) if len(membership) > 1 else None
        )
    else:
        out["spread_bps"] = None
        out["signal_change_rate"] = None
    return out


def run_cell(arm: str, symbol: str, window: str, horizon: int) -> dict:
    era = "modern" if window in MODERN_WINDOWS else "extended"
    study = study_frames(era)[symbol]
    frame = study.frame.loc[study.frame[f"usable_{horizon}"]].reset_index(drop=True)
    columns = list(ARM_FEATURES[arm])

    window_start = pd.Timestamp(WINDOWS[window][0], tz="UTC")
    train = frame.loc[frame[f"knowable_{horizon}"] <= window_start - EMBARGO]
    test = frame.loc[window_mask(frame["timestamp"], window)]
    base = {
        "arm": arm,
        "symbol": symbol,
        "window": window,
        "horizon": horizon,
        "era": era,
        "feature_count": len(columns),
        "train_rows": int(len(train)),
        "test_rows": int(len(test)),
    }
    if len(train) < MIN_TRAIN_ROWS or len(test) < MIN_TEST_ROWS:
        return {**base, "status": "insufficient-rows"}

    fit_rows = int(len(train) * FIT_FRACTION)
    fit_split = train.iloc[:fit_rows]
    calibration_split = train.iloc[fit_rows:]
    matrix_fit = fit_split[columns].to_numpy(dtype="float64")
    matrix_cal = calibration_split[columns].to_numpy(dtype="float64")
    matrix_test = test[columns].to_numpy(dtype="float64")
    standardizer = fit_standardizer(matrix_fit)
    z_fit = standardize(matrix_fit, standardizer)
    z_cal = standardize(matrix_cal, standardizer)
    z_test = standardize(matrix_test, standardizer)

    y_fit = (fit_split[f"fwd_{horizon}"] > 0).to_numpy(dtype="float64")
    y_cal = (calibration_split[f"fwd_{horizon}"] > 0).to_numpy(dtype="float64")
    y_test = (test[f"fwd_{horizon}"] > 0).to_numpy(dtype="float64")
    fwd_test = test[f"fwd_{horizon}"].to_numpy(dtype="float64")
    day_mask = (test["session_bar_index"] == BARS_PER_DAY - 1).to_numpy()

    null_probability = float(y_fit.mean())
    null_test = np.full(len(y_test), null_probability)
    record = {
        **base,
        "status": "ok",
        "fit_rows": int(fit_rows),
        "calibration_rows": int(len(calibration_split)),
        "null": {
            "train_base_rate": null_probability,
            "log_loss": log_loss_of(null_test, y_test),
            "brier": float(np.mean((null_test - y_test) ** 2)),
        },
        "families": {},
    }

    for family, fitter, scorer in (
        ("logistic", lambda z, y: fit_logistic(z, y, l2=1.0), score_logistic),
        ("gbt", lambda z, y: fit_gbt(z, y), score_gbt),
    ):
        estimator = fitter(z_fit, y_fit)
        raw_cal = scorer(estimator, z_cal)
        calibration = fit_isotonic(raw_cal, y_cal)
        calibrated_cal = apply_isotonic(calibration, raw_cal)
        calibrated_test = apply_isotonic(calibration, scorer(estimator, z_test))
        low = float(np.quantile(calibrated_cal, QUINTILE_LOW))
        high = float(np.quantile(calibrated_cal, QUINTILE_HIGH))
        record["families"][family] = {
            "predictive": prediction_metrics(calibrated_test, y_test, fwd_test),
            "log_loss_vs_null": (log_loss_of(calibrated_test, y_test) - record["null"]["log_loss"]),
            "quintile_thresholds": {"low": low, "high": high},
            "economic": economic_read(calibrated_test, fwd_test, day_mask, low, high),
        }
    return record


def cell_path(arm: str, symbol: str, horizon: int, window: str) -> Path:
    slug = symbol.replace("/", "_")
    return CELLS_DIR / f"{arm}__{slug}__h{horizon}__{window}.json"


def _worker(task: tuple[str, str, str, int]) -> dict:
    arm, symbol, window, horizon = task
    path = cell_path(arm, symbol, horizon, window)
    if path.exists():
        return {"task": task, "status": "skipped", "seconds": 0.0}
    started = time.time()
    record = run_cell(arm, symbol, window, horizon)
    record["completed_at"] = datetime.now(tz=UTC).isoformat()
    elapsed = time.time() - started
    record["seconds"] = round(elapsed, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, default=str))
    os.replace(tmp, path)
    summary = {"task": task, "status": record.get("status"), "seconds": round(elapsed, 1)}
    if record.get("status") == "ok":
        summary["gbt_log_loss"] = record["families"]["gbt"]["predictive"]["log_loss"]
        summary["lr_vs_null"] = record["families"]["logistic"]["log_loss_vs_null"]
    return summary


def build_tasks(arms, horizons, windows) -> list[tuple[str, str, str, int]]:
    tasks = []
    for horizon in horizons:
        for window in windows:
            for symbol in SYMBOLS:
                for arm in arms:
                    tasks.append((arm, symbol, window, horizon))
    return tasks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--arms", default=",".join(MAIN_ARMS))
    parser.add_argument("--horizons", default=",".join(str(h) for h in HORIZONS))
    parser.add_argument("--windows", default=",".join(DEFAULT_WINDOWS))
    parser.add_argument("--tag", default="main")
    args = parser.parse_args()

    arms = tuple(a.strip() for a in args.arms.split(",") if a.strip())
    horizons = tuple(int(h) for h in args.horizons.split(",") if h.strip())
    windows = tuple(w.strip() for w in args.windows.split(",") if w.strip())
    for arm in arms:
        if arm not in ARM_FEATURES:
            raise SystemExit(f"unknown arm {arm!r}")
    for horizon in horizons:
        if horizon not in HORIZONS:
            raise SystemExit(f"undeclared horizon {horizon}")

    CELLS_DIR.mkdir(parents=True, exist_ok=True)
    PROGRESS_LOG.parent.mkdir(parents=True, exist_ok=True)
    tasks = build_tasks(arms, horizons, windows)
    total = len(tasks)
    started = time.time()

    def emit(line: str) -> None:
        stamp = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with PROGRESS_LOG.open("a") as handle:
            handle.write(f"[{stamp}] {line}\n")
        print(f"[{stamp}] {line}", flush=True)

    emit(
        f"PHASE=pilot tag={args.tag} workers={args.workers} arms={','.join(arms)} "
        f"horizons={','.join(str(h) for h in horizons)} windows={len(windows)} total={total}"
    )
    done = 0
    with Pool(processes=args.workers) as pool:
        for outcome in pool.imap_unordered(_worker, tasks, chunksize=1):
            done += 1
            arm, symbol, window, horizon = outcome["task"]
            elapsed = time.time() - started
            rate = done / elapsed if elapsed > 0 else 0.0
            eta = (total - done) / rate if rate > 0 else float("inf")
            emit(
                f"PHASE=pilot arm={arm} symbol={symbol} h={horizon} window={window} "
                f"status={outcome['status']} done={done}/{total} "
                f"elapsed={elapsed / 60:.1f}m eta={eta / 60:.1f}m "
                f"cell_s={outcome['seconds']}"
            )
    emit(
        f"PHASE=pilot tag={args.tag} COMPLETE units={total} "
        f"elapsed={(time.time() - started) / 60:.1f}m"
    )


if __name__ == "__main__":
    main()


PRIMARY = PRIMARY_HORIZON  # re-exported for the analysis module

__all__ = [
    "ABLATION_ARMS",
    "ALL_WINDOWS",
    "ARM_FEATURES",
    "DEFAULT_WINDOWS",
    "HOLDOUT_WINDOW",
    "MAIN_ARMS",
    "cell_path",
    "run_cell",
]
