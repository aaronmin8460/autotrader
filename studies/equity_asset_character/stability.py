"""Phase-2 fingerprint stability measurement (ledger §L4).

Rank stability of each structural fingerprint's cross-section across marks.
Spearman correlations are computed as Pearson correlations of ranks (no
scipy); a lagged comparison requires at least ``MIN_COMMON_SYMBOLS`` symbols
non-NaN at both marks.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

MIN_COMMON_SYMBOLS = 20

#: Lags measured, in marks (~months); 6 is the §L4 gate lag.
STABILITY_LAGS: tuple[int, ...] = (1, 3, 6, 12)

#: §L4: a feature is structural iff median lag-6 Spearman ≥ 0.50.
STRUCTURAL_GATE_LAG = 6
STRUCTURAL_GATE_MIN = 0.50


def spearman(a: pd.Series, b: pd.Series) -> float:
    """Spearman rank correlation over jointly non-NaN entries."""
    joined = pd.concat({"a": a, "b": b}, axis=1).dropna()
    if len(joined) < MIN_COMMON_SYMBOLS:
        return float("nan")
    ra = joined["a"].rank().to_numpy(dtype="float64")
    rb = joined["b"].rank().to_numpy(dtype="float64")
    if ra.std(ddof=1) <= 0.0 or rb.std(ddof=1) <= 0.0:
        return float("nan")
    return float(np.corrcoef(ra, rb)[0, 1])


def rank_stability(
    panel: pd.DataFrame,
    features: Sequence[str],
    *,
    lags: Sequence[int] = STABILITY_LAGS,
) -> dict[str, dict[str, object]]:
    """Per-feature median (and count) of lagged Spearman correlations."""
    marks = list(panel.index.get_level_values("mark").unique())
    report: dict[str, dict[str, object]] = {}
    for feature in features:
        by_lag: dict[str, object] = {}
        for lag in lags:
            values = [
                spearman(panel.loc[marks[k - lag], feature], panel.loc[marks[k], feature])
                for k in range(lag, len(marks))
            ]
            clean = [v for v in values if not np.isnan(v)]
            by_lag[f"lag_{lag}"] = {
                "median": float(np.median(clean)) if clean else float("nan"),
                "q25": float(np.percentile(clean, 25)) if clean else float("nan"),
                "q75": float(np.percentile(clean, 75)) if clean else float("nan"),
                "comparisons": len(clean),
            }
        gate_median = by_lag[f"lag_{STRUCTURAL_GATE_LAG}"]["median"]  # type: ignore[index]
        report[feature] = {
            **by_lag,
            "structural": bool(not np.isnan(gate_median) and gate_median >= STRUCTURAL_GATE_MIN),
        }
    return report


__all__ = [
    "MIN_COMMON_SYMBOLS",
    "STABILITY_LAGS",
    "STRUCTURAL_GATE_LAG",
    "STRUCTURAL_GATE_MIN",
    "rank_stability",
    "spearman",
]
