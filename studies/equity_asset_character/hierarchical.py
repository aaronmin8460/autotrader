"""Phase-8 pooled / hierarchical models (ledger §L10).

Numpy ridge, closed form, with block penalties: λ_ridge = 10 on ordinary
features, λ_sym = 100 on symbol intercepts (H2). Target: 21-session forward
log return minus the equal-weight mean of the same across all symbols with
an observation at that mark (allocation attractiveness, not direction).

Walk-forward on the §L5 fit schedule. Training rows for a fit at F: the
row's forward window closes strictly before F AND the row's mark sits at
least three marks before F (one mark for the window itself, one full mark
of embargo — the §L10 purge/embargo). Predictions cover marks from F to the
next fit. OOS information = per-mark Spearman rank IC of prediction vs
realized target.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from studies.equity_asset_character.stability import spearman

RIDGE_LAMBDA = 10.0
SYMBOL_LAMBDA = 100.0
EMBARGO_MARKS = 3

#: §L10 information gate.
IC_MEAN_MIN = 0.03
IC_POSITIVE_SHARE_MIN = 0.55


@dataclass(frozen=True)
class PooledRow:
    mark: date
    symbol: str
    target: float
    window_closes: date
    features: dict[str, float]


def demeaned_targets(
    forward_of: Mapping[tuple[date, str], tuple[float, date]],
    marks: Sequence[date],
) -> dict[tuple[date, str], tuple[float, date]]:
    """own forward return − equal-weight basket mean at the same mark."""
    out: dict[tuple[date, str], tuple[float, date]] = {}
    for mark in marks:
        rows = {symbol: value for (m, symbol), value in forward_of.items() if m == mark}
        if len(rows) < 10:
            continue
        mean = float(np.mean([v[0] for v in rows.values()]))
        for symbol, (value, closes) in rows.items():
            out[(mark, symbol)] = (value - mean, closes)
    return out


def ridge_fit(
    matrix: np.ndarray,
    target: np.ndarray,
    penalties: np.ndarray,
) -> np.ndarray:
    """(X'X + diag(penalties)) β = X'y — deterministic closed form."""
    gram = matrix.T @ matrix + np.diag(penalties)
    return np.linalg.solve(gram, matrix.T @ target)


def build_design(
    rows: Sequence[PooledRow],
    feature_names: Sequence[str],
    symbol_index: Mapping[str, int] | None,
) -> tuple[np.ndarray, np.ndarray]:
    """Design matrix (+ optional shrunk symbol intercepts) and penalties."""
    n = len(rows)
    base = np.zeros((n, len(feature_names) + 1))
    base[:, 0] = 1.0  # intercept, unpenalized-ish (tiny penalty for stability)
    for i, row in enumerate(rows):
        for j, name in enumerate(feature_names):
            base[i, j + 1] = row.features.get(name, 0.0)
    penalties = np.full(base.shape[1], RIDGE_LAMBDA)
    penalties[0] = 1e-8
    if symbol_index is None:
        return base, penalties
    dummies = np.zeros((n, len(symbol_index)))
    for i, row in enumerate(rows):
        j = symbol_index.get(row.symbol)
        if j is not None:
            dummies[i, j] = 1.0
    matrix = np.hstack([base, dummies])
    penalties = np.concatenate([penalties, np.full(len(symbol_index), SYMBOL_LAMBDA)])
    return matrix, penalties


def walk_forward_ic(
    rows: Sequence[PooledRow],
    feature_names: Sequence[str],
    fit_marks: Sequence[date],
    marks: Sequence[date],
    *,
    with_symbol_effects: bool,
) -> dict[str, object]:
    """Causal OOS rank IC of one model over the fit schedule (§L10)."""
    ordered_marks = sorted(marks)
    index_of = {mark: i for i, mark in enumerate(ordered_marks)}
    fit_list = sorted(fit_marks)
    predictions: dict[tuple[date, str], float] = {}
    coefficient_record: dict[str, list[float]] = {}

    for f, fit_mark in enumerate(fit_list):
        next_fit = fit_list[f + 1] if f + 1 < len(fit_list) else None
        fit_index = index_of[
            min((m for m in ordered_marks if m >= fit_mark), default=ordered_marks[-1])
        ]
        train = [
            row
            for row in rows
            if row.window_closes < fit_mark and index_of[row.mark] <= fit_index - EMBARGO_MARKS
        ]
        if len(train) < 200:
            continue
        symbol_index = None
        if with_symbol_effects:
            symbols = sorted({row.symbol for row in train})
            symbol_index = {s: i for i, s in enumerate(symbols)}
        matrix, penalties = build_design(train, feature_names, symbol_index)
        beta = ridge_fit(matrix, np.array([row.target for row in train]), penalties)

        scoring = [
            row
            for row in rows
            if row.mark >= fit_mark and (next_fit is None or row.mark < next_fit)
        ]
        if not scoring:
            continue
        s_matrix, _ = build_design(scoring, feature_names, symbol_index)
        predicted = s_matrix @ beta
        for row, value in zip(scoring, predicted, strict=True):
            predictions[(row.mark, row.symbol)] = float(value)
        for j, name in enumerate(feature_names):
            coefficient_record.setdefault(name, []).append(float(beta[j + 1]))

    ics: list[float] = []
    realized_of = {(row.mark, row.symbol): row.target for row in rows}
    scored_marks = sorted({mark for (mark, _s) in predictions})
    for mark in scored_marks:
        symbols = [s for (m, s) in predictions if m == mark]
        if len(symbols) < 10:
            continue
        p = pd.Series({s: predictions[(mark, s)] for s in symbols})
        r = pd.Series({s: realized_of[(mark, s)] for s in symbols if (mark, s) in realized_of})
        value = spearman(p, r)
        if not np.isnan(value):
            ics.append(value)

    mean_ic = float(np.mean(ics)) if ics else float("nan")
    positive = float(np.mean([ic > 0 for ic in ics])) if ics else float("nan")
    return {
        "mean_ic": mean_ic,
        "positive_share": positive,
        "scored_marks": len(ics),
        "passes_gate": bool(ics and mean_ic >= IC_MEAN_MIN and positive >= IC_POSITIVE_SHARE_MIN),
        "mean_coefficients": {
            name: float(np.mean(values)) for name, values in sorted(coefficient_record.items())
        },
        "predictions": {
            f"{mark.isoformat()}|{symbol}": value for (mark, symbol), value in predictions.items()
        },
    }


__all__ = [
    "EMBARGO_MARKS",
    "IC_MEAN_MIN",
    "IC_POSITIVE_SHARE_MIN",
    "RIDGE_LAMBDA",
    "SYMBOL_LAMBDA",
    "PooledRow",
    "build_design",
    "demeaned_targets",
    "ridge_fit",
    "walk_forward_ic",
]
