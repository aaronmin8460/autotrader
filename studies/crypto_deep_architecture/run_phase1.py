"""Iteration 1, Phase 1: label economics and univariate feature information.

Cheap, single-threaded, no model fitting. Everything here reads development
data only: no statistic in this module touches a feature timestamp at or
after `DEVELOPMENT_CUTOFF` (the start of the W06 confirmation window), and
the univariate screening reads only the early screening slice declared in the
research journal before any result existed.

Run from the study worktree:

    PYTHONPATH=src:. python -m studies.crypto_deep_architecture.run_phase1
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from autotrader.ml.features import FEATURE_NAMES
from autotrader.ml.labels import LabelKind, LabelSpec, compute_labels
from studies.crypto_deep_architecture.data import (
    DEVELOPMENT_CUTOFF,
    DEVELOPMENT_WINDOWS,
    SCREENING_END,
    SCREENING_START,
    exact_break_even,
    load_symbol_frame,
    shared_grid,
    window_mask,
)
from studies.crypto_deep_architecture.features_ext import (
    EXTENSION_FEATURES,
    compute_extension_features,
)

OUTPUT_DIR = Path("/Volumes/AUTOTRADER_QA/reports/crypto-deep-architecture/phase1")

SYMBOLS = ("BTC/USD", "ETH/USD")
HORIZONS = (16, 32, 96)

ALL_FEATURES: tuple[str, ...] = tuple(FEATURE_NAMES) + EXTENSION_FEATURES


def forward_return_label(frame_observations: pd.DataFrame, grid, horizon: int) -> pd.DataFrame:
    spec = LabelSpec(
        name=f"deep-arch-fwd-{horizon}",
        kind=LabelKind.FORWARD_RETURN,
        horizon_bars=horizon,
        entry_price_column="open",
        exit_price_column="open",
    )
    return compute_labels(frame_observations, grid, spec)


def cost_adjusted(returns: pd.Series, break_even: float) -> pd.Series:
    """Net multiplier of one all-in round trip, as a return: (1+r)/(1+B) - 1."""
    return (1.0 + returns) / (1.0 + break_even) - 1.0


def rank_auc(scores: pd.Series, outcomes: pd.Series) -> float:
    """Mann-Whitney AUC from average ranks; NaN when a class is absent."""
    mask = scores.notna() & outcomes.notna()
    s = scores[mask]
    y = outcomes[mask].astype(bool)
    n_pos = int(y.sum())
    n_neg = int((~y).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    ranks = s.rank(method="average")
    total_pos = float(ranks[y].sum())
    return (total_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def rank_ic(feature: pd.Series, target: pd.Series) -> float:
    """Spearman correlation as Pearson on ranks (this venv carries no scipy)."""
    mask = feature.notna() & target.notna()
    if int(mask.sum()) < 100:
        return float("nan")
    return float(feature[mask].rank().corr(target[mask].rank()))


def excursions(observations: pd.DataFrame, horizon: int) -> tuple[np.ndarray, np.ndarray]:
    """Max favorable / adverse excursion over bars t+1..t+horizon, vs entry at open(t+1)."""
    high = observations["high"].to_numpy(dtype="float64")
    low = observations["low"].to_numpy(dtype="float64")
    entry = observations["open"].to_numpy(dtype="float64")
    count = len(observations)
    mfe = np.full(count, np.nan)
    mae = np.full(count, np.nan)
    if count <= horizon + 1:
        return mfe, mae
    high_windows = np.lib.stride_tricks.sliding_window_view(high, horizon)
    low_windows = np.lib.stride_tricks.sliding_window_view(low, horizon)
    with np.errstate(all="ignore"):
        window_max = np.nanmax(high_windows, axis=1)
        window_min = np.nanmin(low_windows, axis=1)
    # Feature bar t: entry fills at open of bar t+1; the excursion window is
    # bars t+1 .. t+horizon, which is the sliding window starting at t+1.
    last_t = count - horizon - 1
    entry_price = entry[1 : last_t + 2]
    usable = np.isfinite(entry_price) & (entry_price > 0.0)
    mfe_vals = np.where(
        usable, window_max[1 : last_t + 2] / np.where(usable, entry_price, 1.0) - 1.0, np.nan
    )
    mae_vals = np.where(
        usable, window_min[1 : last_t + 2] / np.where(usable, entry_price, 1.0) - 1.0, np.nan
    )
    mfe[: last_t + 1] = mfe_vals
    mae[: last_t + 1] = mae_vals
    return mfe, mae


def quantiles(series: pd.Series, points=(0.1, 0.25, 0.5, 0.75, 0.9)) -> dict[str, float]:
    clean = series.dropna()
    return {f"q{int(p * 100):02d}": float(clean.quantile(p)) for p in points}


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    break_even = exact_break_even()
    grid = shared_grid()
    frames = {symbol: load_symbol_frame(symbol, grid) for symbol in SYMBOLS}
    other = {"BTC/USD": "ETH/USD", "ETH/USD": "BTC/USD"}

    label_stats: dict[str, dict] = {"break_even": break_even}
    screening_rows: list[dict] = []
    excursion_stats: dict[str, dict] = {}

    for symbol in SYMBOLS:
        sf = frames[symbol]
        ext = compute_extension_features(
            sf.observations,
            sf.features,
            other_close=frames[other[symbol]].observations["close"],
        )
        features = pd.concat([sf.features, ext], axis=1)
        timestamps = sf.timestamps
        development = timestamps < DEVELOPMENT_CUTOFF
        screening = (timestamps >= SCREENING_START) & (timestamps <= SCREENING_END)

        for horizon in HORIZONS:
            labels = forward_return_label(sf.observations, grid, horizon)
            raw = labels["label_forward_return"]
            valid = labels["label_valid"].fillna(False).astype(bool) & development
            net = cost_adjusted(raw, break_even)
            clears_up = (raw > break_even) & valid
            clears_down = (raw < -break_even) & valid

            key = f"{symbol}|h{horizon}"
            scoring = valid & (timestamps >= pd.Timestamp("2025-01-01", tz="UTC"))
            per_window = {}
            for window in DEVELOPMENT_WINDOWS:
                in_window = window_mask(timestamps, window) & valid
                n_window = int(in_window.sum())
                per_window[window] = {
                    "rows": n_window,
                    "p_clears_up": float(clears_up[in_window].mean()) if n_window else None,
                    "mean_net": float(net[in_window].mean()) if n_window else None,
                    "median_abs_move_bps": (
                        float(raw[in_window].abs().median() * 1e4) if n_window else None
                    ),
                }
            label_stats[key] = {
                "development_rows": int(valid.sum()),
                "scoring_rows": int(scoring.sum()),
                "effective_independent_samples": int(scoring.sum()) // horizon,
                "p_clears_up_scoring": float(clears_up[scoring].mean()),
                "p_clears_down_scoring": float(clears_down[scoring].mean()),
                "median_abs_move_bps_scoring": float(raw[scoring].abs().median() * 1e4),
                "mean_raw_return_bps_scoring": float(raw[scoring].mean() * 1e4),
                "mean_net_return_bps_scoring": float(net[scoring].mean() * 1e4),
                "per_window": per_window,
            }

            target_net = net.where(valid & screening)
            target_up = pd.Series(
                np.where(valid & screening, (raw > break_even).astype(float), np.nan)
            )
            for feature_name in ALL_FEATURES:
                feature = features[feature_name].where(screening)
                screen_mask = feature.notna() & target_net.notna()
                screening_rows.append(
                    {
                        "symbol": symbol,
                        "horizon": horizon,
                        "feature": feature_name,
                        "rows": int(screen_mask.sum()),
                        "coverage": float(feature.notna().sum() / max(int(screening.sum()), 1)),
                        "rank_ic_net": rank_ic(feature, target_net),
                        "auc_clears_up": rank_auc(feature, target_up),
                    }
                )

        for horizon in (32, 96):
            mfe, mae = excursions(sf.observations, horizon)
            mfe_s = pd.Series(mfe).where(development)
            mae_s = pd.Series(mae).where(development)
            up_mask = mfe_s.notna()
            excursion_stats[f"{symbol}|h{horizon}"] = {
                "rows": int(up_mask.sum()),
                "mfe_bps": {k: v * 1e4 for k, v in quantiles(mfe_s).items()},
                "mae_bps": {k: v * 1e4 for k, v in quantiles(mae_s).items()},
                "p_mfe_clears_1x": float((mfe_s > break_even).mean()),
                "p_mfe_clears_2x": float((mfe_s > 2 * break_even).mean()),
                "p_mae_worse_than_minus_1x": float((mae_s < -break_even).mean()),
                "p_mae_worse_than_minus_2x": float((mae_s < -2 * break_even).mean()),
            }

    (OUTPUT_DIR / "label_stats.json").write_text(json.dumps(label_stats, indent=2))
    pd.DataFrame(screening_rows).to_csv(OUTPUT_DIR / "screening.csv", index=False)
    (OUTPUT_DIR / "excursions.json").write_text(json.dumps(excursion_stats, indent=2))
    print(f"phase 1 complete -> {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
