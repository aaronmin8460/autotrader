"""Per-symbol study frames: baseline features + new features + targets + regimes.

One frame per (era, symbol): every 15m grid row carries the 24 frozen OHLCV
baseline features, the 18 predeclared new features, forward-return labels for
the three predeclared horizons, causal regime labels, and the usable-row masks.
The event studies and the model pilot both read exactly this frame, so no
result can rest on a privately different feature definition.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from autotrader.ml.features import FEATURE_NAMES
from autotrader.ml.labels import LabelKind, LabelSpec, compute_labels
from studies.crypto_new_alpha.frozen_data import (
    SymbolFrame,
    extended_grid,
    load_extended_symbol_frame,
    load_symbol_frame,
    shared_grid,
)
from studies.crypto_new_alpha.frozen_features_ext import (
    EXTENSION_FEATURES,
    compute_extension_features,
)
from studies.crypto_new_alpha.new_features import NEW_FEATURES, JoinAudit, join_new_features

NORMALIZED_DIR = Path("/Volumes/AUTOTRADER_QA/datasets/crypto-new-alpha/normalized")

SYMBOLS = ("BTC/USD", "ETH/USD")
PERP_OF = {"BTC/USD": "BTCUSDT", "ETH/USD": "ETHUSDT"}

#: Predeclared horizons in 15m bars; primary first (search-ledger.md §4).
HORIZONS = (96, 32, 16)
PRIMARY_HORIZON = 96

#: The frozen 24-feature OHLCV baseline (identical to the funding pilot's).
BASELINE_FEATURES: tuple[str, ...] = (
    tuple(FEATURE_NAMES) + EXTENSION_FEATURES + ("return_1344", "return_2688")
)
FULL_FEATURES: tuple[str, ...] = BASELINE_FEATURES + NEW_FEATURES

#: The live engines' neutral imputation, inherited unchanged.
NEUTRAL_IMPUTED = ("volume_ratio_32", "close_position_in_bar")

BARS_PER_DAY = 96

#: Causal regime thresholds (search-ledger.md §11).
TREND_WINDOW_BARS = 8640  # 90 days
TREND_BULL = 0.10
TREND_BEAR = -0.10
CRASH_WINDOW_BARS = 672  # 7 days
CRASH_THRESHOLD = -0.15
RECOVERY_WINDOW_BARS = 8640  # 90 days after a crash bar
VOL_WINDOW_BARS = 2880  # 30 days
VOL_REFERENCE_BARS = 70080  # 2 years
VOL_REFERENCE_MIN = 35040  # at least 1 year before the split answers


@dataclass(frozen=True)
class StudyFrame:
    """One symbol's rows with everything the study reads."""

    symbol: str
    era: str
    frame: pd.DataFrame
    join_audit: JoinAudit
    coverage: dict


def load_normalized(perp: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    oi = pd.read_parquet(NORMALIZED_DIR / f"{perp}_oi.parquet")
    flow = pd.read_parquet(NORMALIZED_DIR / f"{perp}_flow.parquet")
    return oi, flow


def _endpoint_return(close: pd.Series, bars: int) -> pd.Series:
    past = close.shift(bars)
    return close / past.where(past > 0.0) - 1.0


def _regime_columns(observations: pd.DataFrame) -> pd.DataFrame:
    """Causal regime labels from the decision stream itself."""
    close = observations["close"].astype("float64")
    return_1 = close.pct_change()

    trend_return = _endpoint_return(close, TREND_WINDOW_BARS)
    crash_return = _endpoint_return(close, CRASH_WINDOW_BARS)
    crash = crash_return < CRASH_THRESHOLD
    crash_recent = (
        crash.astype("float64")
        .rolling(RECOVERY_WINDOW_BARS, min_periods=1)
        .max()
        .astype(bool)
    )

    regime = pd.Series("sideways", index=observations.index, dtype="object")
    regime = regime.mask(trend_return > TREND_BULL, "bull")
    regime = regime.mask(trend_return < TREND_BEAR, "bear")
    regime = regime.mask(~crash & crash_recent, "recovery")
    regime = regime.mask(crash, "crash")
    regime = regime.mask(trend_return.isna(), None)

    rv_30d = return_1.rolling(VOL_WINDOW_BARS, min_periods=int(VOL_WINDOW_BARS * 0.8)).std(ddof=0)
    rv_reference = rv_30d.rolling(VOL_REFERENCE_BARS, min_periods=VOL_REFERENCE_MIN).median()
    vol_regime = pd.Series(None, index=observations.index, dtype="object")
    vol_regime = vol_regime.mask(rv_30d > rv_reference, "high-vol")
    vol_regime = vol_regime.mask(rv_30d <= rv_reference, "low-vol")

    return pd.DataFrame({"regime": regime, "vol_regime": vol_regime})


def build_study_frame(symbol: str, era: str, frames: dict[str, SymbolFrame]) -> StudyFrame:
    """Everything the study reads for one symbol on one era grid."""
    other = {"BTC/USD": "ETH/USD", "ETH/USD": "BTC/USD"}[symbol]
    sf = frames[symbol]
    ext = compute_extension_features(
        sf.observations, sf.features, other_close=frames[other].observations["close"]
    )
    features = pd.concat([sf.features, ext], axis=1)
    close = sf.observations["close"].astype("float64")
    for lookback in (1344, 2688):
        features[f"return_{lookback}"] = _endpoint_return(close, lookback)
    present = sf.observations["is_present"].astype(bool)
    for name in NEUTRAL_IMPUTED:
        features[name] = features[name].where(features[name].notna() | ~present, 0.0)

    oi, flow = load_normalized(PERP_OF[symbol])
    new, audit = join_new_features(
        sf.observations["timestamp"],
        oi,
        flow,
        return_16=features["return_16"],
        return_96=features["return_96"],
    )
    features = pd.concat([features, new], axis=1)

    columns = [sf.observations[["timestamp", "session_bar_index"]], features]
    for horizon in HORIZONS:
        spec = LabelSpec(
            name=f"new-alpha-fwd-{horizon}",
            kind=LabelKind.FORWARD_RETURN,
            horizon_bars=horizon,
            entry_price_column="open",
            exit_price_column="open",
        )
        labels = compute_labels(sf.observations, sf.grid, spec)
        columns.append(
            labels[["label_forward_return", "label_knowable_at", "label_valid"]].rename(
                columns={
                    "label_forward_return": f"fwd_{horizon}",
                    "label_knowable_at": f"knowable_{horizon}",
                    "label_valid": f"valid_{horizon}",
                }
            )
        )
    columns.append(_regime_columns(sf.observations))
    frame = pd.concat(columns, axis=1)
    frame["grid_position"] = np.arange(len(frame))

    feature_complete = pd.Series(True, index=frame.index)
    for name in FULL_FEATURES:
        feature_complete &= frame[name].notna()
    frame["features_complete"] = feature_complete
    for horizon in HORIZONS:
        frame[f"usable_{horizon}"] = (
            frame[f"valid_{horizon}"].fillna(False).astype(bool) & feature_complete
        )

    baseline_complete = pd.Series(True, index=frame.index)
    for name in BASELINE_FEATURES:
        baseline_complete &= frame[name].notna()
    coverage = {
        "era": era,
        "symbol": symbol,
        "rows_total": int(len(frame)),
        "rows_baseline_complete": int(baseline_complete.sum()),
        "rows_full_complete": int(feature_complete.sum()),
        "full_over_baseline_retention": (
            float(feature_complete.sum() / baseline_complete.sum())
            if baseline_complete.sum()
            else 0.0
        ),
        "join_audit": audit.as_dict(),
        "usable_rows": {
            str(horizon): int(frame[f"usable_{horizon}"].sum()) for horizon in HORIZONS
        },
    }
    return StudyFrame(symbol=symbol, era=era, frame=frame, join_audit=audit, coverage=coverage)


_CACHE: dict = {}


def study_frames(era: str) -> dict[str, StudyFrame]:
    """Both symbols' study frames for one era, cached per process."""
    key = ("study", era)
    if key not in _CACHE:
        if era == "modern":
            grid = shared_grid()
            base = {s: load_symbol_frame(s, grid) for s in SYMBOLS}
        elif era == "extended":
            grid = extended_grid()
            base = {s: load_extended_symbol_frame(s, grid) for s in SYMBOLS}
        else:
            raise ValueError(f"unknown era {era!r}")
        _CACHE[key] = {s: build_study_frame(s, era, base) for s in SYMBOLS}
    return _CACHE[key]


__all__ = [
    "BARS_PER_DAY",
    "BASELINE_FEATURES",
    "FULL_FEATURES",
    "HORIZONS",
    "PERP_OF",
    "PRIMARY_HORIZON",
    "SYMBOLS",
    "StudyFrame",
    "build_study_frame",
    "study_frames",
]
