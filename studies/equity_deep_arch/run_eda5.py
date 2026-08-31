"""EDA-5 context-feature information test.

Predeclared in the search ledger (including the pooled-training clarification)
before this module was first run. One model per (window x target x family),
fitted on all ten symbols' training rows jointly, scored on the window's rows;
raw sigmoid probabilities, no calibration, no tuning of any hyperparameter.

Usage:
    python -m studies.equity_deep_arch.run_eda5
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from autotrader.decision.probability import sigmoid
from autotrader.ml.v4 import (
    fit_gradient_boosted,
    fit_logistic,
    fit_standardizer,
)
from studies.equity_10_full.windows import FULL_WINDOWS
from studies.equity_deep_arch.context import (
    FEATURE_COLUMNS,
    build_context_frame,
    session_index,
)
from studies.equity_deep_arch.evaluate import write_json
from studies.equity_deep_arch.run_eda1 import default_datasets

OUTPUT = Path("/Volumes/AUTOTRADER_QA/reports/equity-deep-architecture/eda5")

#: Purge margin: a training row's label must resolve at least this many
#: sessions before the test window opens.
EMBARGO_SESSIONS = 2

#: The economic threshold target: next-session return above ~2.5x the
#: realistic round-trip cost.
ECONOMIC_THRESHOLD = 0.001

TARGETS = ("direction", "economic")
FAMILIES = ("null", "logistic", "gradient_boosted")

#: Predeclared PASS rule constants.
PASS_POOLED_AUC = 0.53
PASS_MIN_POSITIVE_WINDOWS = 8


def _labels(frame: pd.DataFrame, target: str) -> np.ndarray:
    returns = frame["forward_return"].to_numpy(dtype="float64")
    threshold = 0.0 if target == "direction" else ECONOMIC_THRESHOLD
    return (returns > threshold).astype("float64")


def _log_loss(labels: np.ndarray, probabilities: np.ndarray) -> float:
    clipped = np.clip(probabilities, 1e-12, 1 - 1e-12)
    return float(-np.mean(labels * np.log(clipped) + (1 - labels) * np.log(1 - clipped)))


def _auc(labels: np.ndarray, scores: np.ndarray) -> float | None:
    positives = scores[labels == 1.0]
    negatives = scores[labels == 0.0]
    if len(positives) == 0 or len(negatives) == 0:
        return None
    order = np.argsort(np.concatenate([positives, negatives]), kind="stable")
    ranks = np.empty(len(order), dtype="float64")
    ranks[order] = np.arange(1, len(order) + 1)
    # Average ranks over ties for an exact Mann-Whitney statistic.
    combined = np.concatenate([positives, negatives])
    for value in np.unique(combined):
        mask = combined == value
        if mask.sum() > 1:
            ranks[mask] = ranks[mask].mean()
    rank_sum = ranks[: len(positives)].sum()
    u_statistic = rank_sum - len(positives) * (len(positives) + 1) / 2
    return float(u_statistic / (len(positives) * len(negatives)))


def _fit_and_score(
    family: str,
    train_matrix: np.ndarray,
    train_labels: np.ndarray,
    test_matrix: np.ndarray,
) -> np.ndarray:
    if family == "null":
        base = float(train_labels.mean())
        return np.full(len(test_matrix), base, dtype="float64")
    standardizer = fit_standardizer(train_matrix)
    standardized_train = np.asarray(
        [standardizer.apply([float(v) for v in row]) for row in train_matrix]
    )
    if family == "logistic":
        estimator = fit_logistic(standardized_train, train_labels)
    else:
        estimator = fit_gradient_boosted(standardized_train, train_labels)
    scores = []
    for row in test_matrix:
        standardized = standardizer.apply([float(v) for v in row])
        scores.append(sigmoid(estimator.raw_score(standardized)))
    return np.asarray(scores, dtype="float64")


def main() -> None:
    datasets = default_datasets()
    started = time.perf_counter()
    OUTPUT.mkdir(parents=True, exist_ok=True)

    cache = OUTPUT / "context_frame.parquet"
    if cache.exists():
        frame = pd.read_parquet(cache)
    else:
        frame = build_context_frame(datasets)
        frame.to_parquet(cache, engine="pyarrow", index=False)
    ordinal = session_index(datasets)
    frame = frame.assign(
        session_ord=[ordinal[s] for s in frame["session"]],
        knowable_ord=[ordinal[s] for s in frame["label_knowable_session"]],
    )
    print(f"context frame: {len(frame)} rows, {frame['session'].min()}..{frame['session'].max()}")

    results: dict[str, object] = {
        "rows": len(frame),
        "feature_columns": list(FEATURE_COLUMNS),
        "targets": {},
    }
    verdict_pass = False
    for target in TARGETS:
        labels_all = _labels(frame, target)
        per_family: dict[str, object] = {}
        for family in FAMILIES:
            windows_out = []
            pooled_scores: list[np.ndarray] = []
            pooled_labels: list[np.ndarray] = []
            for window in FULL_WINDOWS:
                first_ord = ordinal[
                    min(day for day in ordinal if window.start <= day <= window.end)
                ]
                train_mask = (frame["knowable_ord"] <= first_ord - EMBARGO_SESSIONS).to_numpy()
                test_mask = (
                    (frame["session"] >= window.start) & (frame["session"] <= window.end)
                ).to_numpy()
                train_matrix = frame.loc[train_mask, list(FEATURE_COLUMNS)].to_numpy("float64")
                test_matrix = frame.loc[test_mask, list(FEATURE_COLUMNS)].to_numpy("float64")
                train_labels = labels_all[train_mask]
                test_labels = labels_all[test_mask]
                probabilities = _fit_and_score(family, train_matrix, train_labels, test_matrix)
                base = float(train_labels.mean())
                null_probabilities = np.full(len(test_labels), base)
                gain = _log_loss(test_labels, null_probabilities) - _log_loss(
                    test_labels, probabilities
                )
                windows_out.append(
                    {
                        "window": window.name,
                        "train_rows": int(train_mask.sum()),
                        "test_rows": int(test_mask.sum()),
                        "oos_log_loss_gain_vs_null": gain,
                        "oos_auc": _auc(test_labels, probabilities),
                    }
                )
                pooled_scores.append(probabilities)
                pooled_labels.append(test_labels)
                print(
                    f"{target}/{family}/{window.name}: gain {gain:+.5f} "
                    f"auc {_auc(test_labels, probabilities)}",
                    flush=True,
                )
            scores = np.concatenate(pooled_scores)
            outcome = np.concatenate(pooled_labels)
            pooled_auc = _auc(outcome, scores)
            gains = [w["oos_log_loss_gain_vs_null"] for w in windows_out]
            positive_windows = sum(1 for g in gains if g > 0)
            summary = {
                "pooled_oos_auc": pooled_auc,
                "mean_window_gain": float(np.mean(gains)),
                "positive_gain_windows": positive_windows,
                "windows": windows_out,
            }
            if (
                family != "null"
                and pooled_auc is not None
                and pooled_auc >= PASS_POOLED_AUC
                and float(np.mean(gains)) > 0
                and positive_windows >= PASS_MIN_POSITIVE_WINDOWS
            ):
                summary["passes_predeclared_rule"] = True
                verdict_pass = True
            else:
                summary["passes_predeclared_rule"] = False
            per_family[family] = summary
        results["targets"][target] = per_family

    results["verdict"] = "INFORMATION_FOUND" if verdict_pass else "FEATURE_INFORMATION_LIMIT"
    results["elapsed_seconds"] = time.perf_counter() - started
    write_json(OUTPUT / "information_test.json", results)
    print(f"verdict: {results['verdict']} in {results['elapsed_seconds']:.0f}s")


if __name__ == "__main__":
    main()
