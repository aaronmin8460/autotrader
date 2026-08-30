"""The incremental-information pilot: BASELINE vs AUGMENTED, one harness.

Both arms run the frozen DA-SPREAD-96 architecture, unchanged in every element
the mandate names constant: grid, windows, label, horizon, model family and
hyper-parameters, 70/30 chronological fit/calibration split, standardizer
policy, isotonic calibration, calibration-quantile thresholds, cost models,
daily decision cadence and the ledger-exact replay. The **only** difference
between the arms is the feature matrix: 24 OHLCV columns, or those same 24
plus the 8 predeclared funding/basis columns.

**Shared row population.** Both arms score exactly the same rows. The usable
mask is the intersection - a row survives only if the label is valid *and*
every baseline feature *and* every derivative feature is available on it.
Letting the arms score different populations would confound information value
with sample selection: the augmented arm could look better purely by
declining the days its extra inputs were missing. The price is that BASELINE
here is a re-run on a slightly smaller population than the original
DA-SPREAD-96 figures, which are therefore a reference point, not the
comparison baseline. The comparison baseline is the arm run here.

Checkpoints: one JSON per (arm, symbol, horizon, window), written to a temp
file and renamed, so an interrupted run loses only the cell in flight.
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

from autotrader.ml.features import FEATURE_NAMES
from autotrader.ml.labels import LabelKind, LabelSpec, compute_labels
from autotrader.ml.v4 import (
    default_candidates,
    fit_estimator,
    fit_isotonic,
    fit_standardizer,
    roc_auc,
)
from autotrader.research.costs import cost_model_for
from studies.crypto_funding_basis_pilot.derivative_features import (
    DERIVATIVE_FEATURES,
    join_derivative_features,
)
from studies.crypto_funding_basis_pilot.frozen_data import (
    WINDOWS,
    exact_break_even,
    extended_grid,
    load_extended_symbol_frame,
    load_symbol_frame,
    shared_grid,
    window_mask,
)
from studies.crypto_funding_basis_pilot.frozen_features_ext import (
    EXTENSION_FEATURES,
    compute_extension_features,
)
from studies.crypto_funding_basis_pilot.frozen_trend_rules import FLAT, LONG, replay
from studies.crypto_funding_basis_pilot.ledger import (
    ledger_matches_replay,
    ledger_statistics,
    trade_ledger,
)

OUTPUT_DIR = Path("/Volumes/AUTOTRADER_QA/reports/crypto-funding-basis-pilot")
CELLS_DIR = OUTPUT_DIR / "cells"
PROGRESS_LOG = Path("/Volumes/AUTOTRADER_QA/logs/crypto-funding-basis-pilot.log")
NORMALIZED_DIR = Path("/Volumes/AUTOTRADER_QA/datasets/crypto-funding-basis/normalized")

SYMBOLS = ("BTC/USD", "ETH/USD")
PERP_OF = {"BTC/USD": "BTCUSDT", "ETH/USD": "ETHUSDT"}

#: The frozen 17. `P3` plus `W01..W07` sit on the 2024-26 grid; `X01..X09` on
#: the 2021-23 grid. No window is added, removed or reordered by results.
MODERN_WINDOWS = ("P3", "W01", "W02", "W03", "W04", "W05", "W06", "W07")
EXTENDED_WINDOWS = tuple(f"X{i:02d}" for i in range(1, 10))
ALL_WINDOWS = MODERN_WINDOWS + EXTENDED_WINDOWS

#: Predeclared bounded horizon set. 96 is the frozen DA-SPREAD-96 horizon and
#: the one the success threshold is read on; it runs first so the primary
#: result is complete before the secondary evidence starts.
HORIZONS = (96, 16, 32)
PRIMARY_HORIZON = 96

ARMS = ("baseline", "augmented")

#: Ablation arms, run only if the augmented arm shows meaningful improvement.
ABLATION_ARMS = ("funding_only", "basis_only")

FUNDING_FEATURES = (
    "funding_current",
    "funding_z_30",
    "funding_delta",
    "funding_trend_interaction",
)
BASIS_FEATURES = (
    "premium_close",
    "premium_mean_24h",
    "premium_pct_90d",
    "premium_vol_interaction",
)

BASELINE_FEATURES: tuple[str, ...] = (
    tuple(FEATURE_NAMES) + EXTENSION_FEATURES + ("return_1344", "return_2688")
)
AUGMENTED_FEATURES: tuple[str, ...] = BASELINE_FEATURES + DERIVATIVE_FEATURES

ARM_FEATURES: dict[str, tuple[str, ...]] = {
    "baseline": BASELINE_FEATURES,
    "augmented": AUGMENTED_FEATURES,
    "funding_only": BASELINE_FEATURES + FUNDING_FEATURES,
    "basis_only": BASELINE_FEATURES + BASIS_FEATURES,
}

COST_MODELS = ("frictionless", "crypto-taker", "stress")
FIT_FRACTION = 0.7
EMBARGO = pd.Timedelta("24h")
BARS_PER_DAY = 96
FAMILY = "gradient-boosted"
GATES = (("q80", 0.8, 0.5), ("q70", 0.7, 0.5), ("q90", 0.9, 0.5))
NEUTRAL_IMPUTED = ("volume_ratio_32", "close_position_in_bar")

#: Probability clip for log loss - the frozen calibration can emit exact 0/1.
LOG_LOSS_EPSILON = 1e-15


# ---------------------------------------------------------------------------
# Frozen scoring helpers, copied from the frozen harness's own audit module.


def sigmoid(raw: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(raw, -500.0, 500.0)))


def standardize(matrix: np.ndarray, standardizer) -> np.ndarray:
    means = np.asarray(standardizer.means, dtype="float64")
    scales = np.asarray(standardizer.scales, dtype="float64")
    return (matrix - means) / scales


def score_rows(estimator, standardized: np.ndarray) -> np.ndarray:
    return sigmoid(np.asarray([estimator.raw_score(row) for row in standardized], dtype="float64"))


def apply_isotonic(calibration, probabilities: np.ndarray) -> np.ndarray:
    thresholds = np.asarray(calibration.thresholds, dtype="float64")
    values = np.asarray(calibration.values, dtype="float64")
    positions = np.searchsorted(thresholds, probabilities, side="right") - 1
    return values[np.clip(positions, 0, len(values) - 1)]


def forward_return_label(observations: pd.DataFrame, grid, horizon: int) -> pd.DataFrame:
    spec = LabelSpec(
        name=f"funding-basis-fwd-{horizon}",
        kind=LabelKind.FORWARD_RETURN,
        horizon_bars=horizon,
        entry_price_column="open",
        exit_price_column="open",
    )
    return compute_labels(observations, grid, spec)


# ---------------------------------------------------------------------------
# Metrics


def log_loss_of(probabilities: np.ndarray, actual: np.ndarray) -> float:
    p = np.clip(probabilities, LOG_LOSS_EPSILON, 1.0 - LOG_LOSS_EPSILON)
    return float(-np.mean(actual * np.log(p) + (1.0 - actual) * np.log(1.0 - p)))


def brier_of(probabilities: np.ndarray, actual: np.ndarray) -> float:
    return float(np.mean((probabilities - actual) ** 2))


def pr_auc(probabilities: np.ndarray, actual: np.ndarray) -> float | None:
    """Average precision, computed from the ranked scores."""
    if actual.sum() == 0 or actual.sum() == len(actual):
        return None
    order = np.argsort(-probabilities, kind="stable")
    labels = actual[order]
    cumulative_true = np.cumsum(labels)
    precision = cumulative_true / np.arange(1, len(labels) + 1)
    positives = labels.sum()
    return float((precision * labels).sum() / positives)


def calibration_error(
    probabilities: np.ndarray, actual: np.ndarray, bins: int = 10
) -> float | None:
    """Expected calibration error over equal-width probability bins."""
    if len(probabilities) < bins:
        return None
    edges = np.linspace(0.0, 1.0, bins + 1)
    index = np.clip(np.digitize(probabilities, edges[1:-1], right=False), 0, bins - 1)
    total = 0.0
    for b in range(bins):
        mask = index == b
        count = int(mask.sum())
        if count:
            total += count * abs(float(probabilities[mask].mean()) - float(actual[mask].mean()))
    return float(total / len(probabilities))


def side_metrics(probabilities: np.ndarray, actual: np.ndarray) -> dict:
    return {
        "log_loss": log_loss_of(probabilities, actual),
        "brier": brier_of(probabilities, actual),
        "roc_auc": roc_auc(probabilities, actual) if 0 < actual.sum() < len(actual) else None,
        "pr_auc": pr_auc(probabilities, actual),
        "ece": calibration_error(probabilities, actual),
        "base_rate": float(actual.mean()),
        "mean_prediction": float(probabilities.mean()),
        "prediction_std": float(probabilities.std()),
        "rows": int(len(actual)),
    }


def null_log_loss(train_rate: float, actual: np.ndarray) -> float:
    """The class-frequency null: the training base rate, predicted flat."""
    return log_loss_of(np.full(len(actual), train_rate, dtype="float64"), actual)


# ---------------------------------------------------------------------------
# Frames


def load_normalized(perp: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    funding = pd.read_parquet(NORMALIZED_DIR / f"{perp}_funding.parquet")
    premium = pd.read_parquet(NORMALIZED_DIR / f"{perp}_premium.parquet")
    return funding, premium


def build_frame(symbol: str, frames: dict, horizon: int) -> tuple[pd.DataFrame, dict]:
    """Features (24 + 8), two-sided targets and the shared usable population."""
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

    funding, premium = load_normalized(PERP_OF[symbol])
    derivative, audit = join_derivative_features(
        sf.observations["timestamp"],
        funding,
        premium,
        return_2688=features["return_2688"],
        realized_volatility_96=features["realized_volatility_96"],
    )
    features = pd.concat([features, derivative], axis=1)

    labels = forward_return_label(sf.observations, sf.grid, horizon)
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
    baseline_only = usable.copy()
    for name in BASELINE_FEATURES:
        baseline_only &= frame[name].notna()
    # The shared population: both arms score identical rows.
    for name in AUGMENTED_FEATURES:
        usable &= frame[name].notna()

    coverage = {
        "join_audit": audit.as_dict(),
        "rows_total": int(len(frame)),
        "rows_label_valid": int(frame["label_valid"].fillna(False).astype(bool).sum()),
        "rows_baseline_usable": int(baseline_only.sum()),
        "rows_shared_usable": int(usable.sum()),
        "shared_population_retention": (
            float(usable.sum() / baseline_only.sum()) if baseline_only.sum() else 0.0
        ),
    }
    return frame.loc[usable].reset_index(drop=True), coverage


def window_bounds(timestamps: pd.Series, window: str) -> tuple[int, int]:
    mask = window_mask(timestamps, window)
    positions = np.flatnonzero(mask.to_numpy())
    return int(positions[0]), int(positions[-1])


# ---------------------------------------------------------------------------
# One cell


def fit_spread_model(train: pd.DataFrame, columns: tuple[str, ...]) -> dict:
    fit_rows = int(len(train) * FIT_FRACTION)
    fit_split = train.iloc[:fit_rows]
    calibration_split = train.iloc[fit_rows:]
    matrix_fit = fit_split[list(columns)].to_numpy(dtype="float64")
    matrix_cal = calibration_split[list(columns)].to_numpy(dtype="float64")
    standardizer = fit_standardizer(matrix_fit)
    z_fit = standardize(matrix_fit, standardizer)
    z_cal = standardize(matrix_cal, standardizer)

    candidate = next(c for c in default_candidates() if c.name == FAMILY)
    bundle: dict = {"standardizer": standardizer, "sides": {}, "train_base_rate": {}}
    spreads_cal = None
    for side in ("up", "down"):
        y_fit = fit_split[f"target_{side}"].to_numpy(dtype="float64")
        y_cal = calibration_split[f"target_{side}"].to_numpy(dtype="float64")
        estimator = fit_estimator(candidate, z_fit, y_fit)
        raw_cal = score_rows(estimator, z_cal)
        calibration = fit_isotonic(raw_cal, y_cal)
        calibrated_cal = apply_isotonic(calibration, raw_cal)
        bundle["sides"][side] = {"estimator": estimator, "calibration": calibration}
        bundle["train_base_rate"][side] = float(y_fit.mean())
        spreads_cal = calibrated_cal if spreads_cal is None else spreads_cal - calibrated_cal
    bundle["cal_spread_quantiles"] = {
        q: float(np.quantile(spreads_cal, q)) for q in (0.5, 0.7, 0.8, 0.9)
    }
    bundle["cal_rows"] = int(len(calibration_split))
    bundle["fit_rows"] = int(fit_rows)
    return bundle


def probabilities_for(bundle: dict, matrix: np.ndarray) -> dict[str, np.ndarray]:
    z = standardize(matrix, bundle["standardizer"])
    out = {}
    for side in ("up", "down"):
        part = bundle["sides"][side]
        out[side] = apply_isotonic(part["calibration"], score_rows(part["estimator"], z))
    return out


def run_cell(
    arm: str, symbol: str, window: str, horizon: int, frame: pd.DataFrame, frames: dict
) -> dict:
    columns = ARM_FEATURES[arm]
    window_start = pd.Timestamp(WINDOWS[window][0], tz="UTC")
    train = frame.loc[frame["label_knowable_at"] <= window_start - EMBARGO]
    test = frame.loc[window_mask(frame["timestamp"], window)]
    if len(train) < 500 or len(test) < 50:
        return {
            "arm": arm,
            "symbol": symbol,
            "window": window,
            "horizon": horizon,
            "status": "insufficient-rows",
            "train_rows": int(len(train)),
            "test_rows": int(len(test)),
        }
    bundle = fit_spread_model(train, columns)

    # Predictive metrics on every valid row of the window - the same prediction
    # problem the model was trained on, and the reading with the most power.
    test_matrix = test[list(columns)].to_numpy(dtype="float64")
    test_probabilities = probabilities_for(bundle, test_matrix)
    predictive: dict = {"per_side": {}, "null": {}}
    log_losses = []
    for side in ("up", "down"):
        actual = test[f"target_{side}"].to_numpy(dtype="float64")
        metrics = side_metrics(test_probabilities[side], actual)
        predictive["per_side"][side] = metrics
        predictive["null"][side] = null_log_loss(bundle["train_base_rate"][side], actual)
        log_losses.append(metrics["log_loss"])
    predictive["log_loss"] = float(np.mean(log_losses))
    predictive["null_log_loss"] = float(np.mean(list(predictive["null"].values())))
    predictive["log_loss_vs_null"] = predictive["log_loss"] - predictive["null_log_loss"]

    spread_all = test_probabilities["up"] - test_probabilities["down"]
    net_all = test["label_forward_return"].to_numpy(dtype="float64")
    predictive["spread_rank_ic_all_rows"] = (
        float(pd.Series(spread_all).rank().corr(pd.Series(net_all).rank()))
        if len(test) > 10
        else None
    )
    predictive["spread_dispersion_all_rows"] = float(spread_all.std())

    # The decision population: last completed bar of each UTC day.
    decision_rows = test.loc[test["session_bar_index"] == BARS_PER_DAY - 1]
    decision_probabilities = probabilities_for(
        bundle, decision_rows[list(columns)].to_numpy(dtype="float64")
    )
    spread = decision_probabilities["up"] - decision_probabilities["down"]
    positions = decision_rows["grid_position"].to_numpy(dtype="int64")
    net_returns = decision_rows["label_forward_return"].to_numpy(dtype="float64")

    decision_metrics: dict = {"per_side": {}}
    decision_log_losses = []
    for side in ("up", "down"):
        actual = decision_rows[f"target_{side}"].to_numpy(dtype="float64")
        metrics = side_metrics(decision_probabilities[side], actual)
        decision_metrics["per_side"][side] = metrics
        decision_log_losses.append(metrics["log_loss"])
    decision_metrics["log_loss"] = float(np.mean(decision_log_losses))
    decision_metrics["decision_days"] = int(len(decision_rows))
    decision_metrics["spread_rank_ic"] = (
        float(pd.Series(spread).rank().corr(pd.Series(net_returns).rank()))
        if len(decision_rows) > 10
        else None
    )
    decision_metrics["spread_dispersion"] = float(spread.std()) if len(spread) else None

    observations = frames[symbol].observations
    start, end = window_bounds(frames[symbol].timestamps, window)

    gates_out: dict = {}
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
            model = cost_model_for(cost_label)
            result = replay(observations, states, model, start=start, end=end)
            trades = trade_ledger(observations, states, model, start=start, end=end)
            statistics = ledger_statistics(trades)
            per_cost[cost_label] = {
                "net_return": result.net_return,
                "forced_return": result.forced_liquidation_return,
                "trades": result.trades,
                "time_in_market": result.time_in_market,
                "max_drawdown": result.max_drawdown,
                "open_at_end": result.open_position_at_end,
                "realized_pnl": result.realized_pnl,
                "unrealized_pnl": result.unrealized_pnl,
                "fees_paid": result.fees_paid,
                "ledger": statistics,
                "ledger_consistent": ledger_matches_replay(trades, result, tolerance=1e-6),
            }
        gates_out[gate_name] = {
            "tau_enter": tau_enter,
            "tau_exit": tau_exit,
            "costs": per_cost,
        }

    return {
        "arm": arm,
        "symbol": symbol,
        "window": window,
        "horizon": horizon,
        "status": "ok",
        "features": list(columns),
        "feature_count": len(columns),
        "train_rows": int(len(train)),
        "fit_rows": bundle["fit_rows"],
        "calibration_rows": bundle["cal_rows"],
        "test_rows": int(len(test)),
        "predictive": predictive,
        "decision": decision_metrics,
        "gates": gates_out,
    }


# ---------------------------------------------------------------------------
# Orchestration


_FRAME_CACHE: dict = {}


def _era_of(window: str) -> str:
    return "modern" if window in MODERN_WINDOWS else "extended"


def _frames_for(era: str) -> dict:
    key = ("frames", era)
    if key not in _FRAME_CACHE:
        if era == "modern":
            grid = shared_grid()
            _FRAME_CACHE[key] = {s: load_symbol_frame(s, grid) for s in SYMBOLS}
        else:
            grid = extended_grid()
            _FRAME_CACHE[key] = {s: load_extended_symbol_frame(s, grid) for s in SYMBOLS}
    return _FRAME_CACHE[key]


def _frame_for(era: str, symbol: str, horizon: int) -> tuple[pd.DataFrame, dict]:
    key = ("frame", era, symbol, horizon)
    if key not in _FRAME_CACHE:
        frames = _frames_for(era)
        _FRAME_CACHE[key] = build_frame(symbol, frames, horizon)
    return _FRAME_CACHE[key]


def cell_path(arm: str, symbol: str, horizon: int, window: str) -> Path:
    slug = symbol.replace("/", "_")
    return CELLS_DIR / f"{arm}__{slug}__h{horizon}__{window}.json"


def _worker(task: tuple[str, str, str, int]) -> dict:
    arm, symbol, window, horizon = task
    path = cell_path(arm, symbol, horizon, window)
    if path.exists():
        return {"task": task, "status": "skipped", "seconds": 0.0}
    era = _era_of(window)
    frames = _frames_for(era)
    frame, coverage = _frame_for(era, symbol, horizon)
    started = time.time()
    record = run_cell(arm, symbol, window, horizon, frame, frames)
    record["coverage"] = coverage
    record["completed_at"] = datetime.now(tz=UTC).isoformat()
    elapsed = time.time() - started
    record["seconds"] = round(elapsed, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, default=str))
    os.replace(tmp, path)
    headline = None
    if record.get("status") == "ok":
        headline = record["gates"]["q80"]["costs"]["crypto-taker"]["forced_return"]
    return {
        "task": task,
        "status": record.get("status", "ok"),
        "seconds": round(elapsed, 1),
        "log_loss": record.get("predictive", {}).get("log_loss"),
        "forced": headline,
    }


def build_tasks(arms: tuple[str, ...], horizons: tuple[int, ...], windows: tuple[str, ...]):
    tasks = []
    for horizon in horizons:
        for era in ("modern", "extended"):
            era_windows = [w for w in windows if _era_of(w) == era]
            for symbol in SYMBOLS:
                for window in era_windows:
                    for arm in arms:
                        tasks.append((arm, symbol, window, horizon))
    return tasks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--arms", default=",".join(ARMS))
    parser.add_argument("--horizons", default=",".join(str(h) for h in HORIZONS))
    parser.add_argument("--windows", default=",".join(ALL_WINDOWS))
    parser.add_argument("--tag", default="main")
    args = parser.parse_args()

    arms = tuple(a.strip() for a in args.arms.split(",") if a.strip())
    horizons = tuple(int(h) for h in args.horizons.split(",") if h.strip())
    windows = tuple(w.strip() for w in args.windows.split(",") if w.strip())
    for arm in arms:
        if arm not in ARM_FEATURES:
            raise SystemExit(f"unknown arm {arm!r}")

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
        f"PHASE=heavy-scoring tag={args.tag} workers={args.workers} "
        f"arms={','.join(arms)} horizons={','.join(str(h) for h in horizons)} "
        f"windows={len(windows)} total_units={total}"
    )

    done = 0
    with Pool(processes=args.workers) as pool:
        for outcome in pool.imap_unordered(_worker, tasks, chunksize=1):
            done += 1
            arm, symbol, window, horizon = outcome["task"]
            elapsed = time.time() - started
            rate = done / elapsed if elapsed > 0 else 0.0
            eta = (total - done) / rate if rate > 0 else float("inf")
            log_loss = outcome.get("log_loss")
            forced = outcome.get("forced")
            emit(
                f"PHASE=heavy-scoring arm={arm} symbol={symbol} horizon={horizon} "
                f"window={window} status={outcome['status']} "
                f"logloss={log_loss if log_loss is None else round(log_loss, 6)} "
                f"forced={forced if forced is None else round(forced, 4)} "
                f"completed={done}/{total} pct={100.0 * done / total:.1f}% "
                f"elapsed={elapsed / 60:.1f}m eta={eta / 60:.1f}m "
                f"cell_seconds={outcome['seconds']} workers={args.workers}"
            )

    emit(
        f"PHASE=heavy-scoring tag={args.tag} COMPLETE units={total} "
        f"elapsed={(time.time() - started) / 60:.1f}m"
    )


if __name__ == "__main__":
    main()
