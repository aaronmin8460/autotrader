"""The predeclared long-lookback feature extension.

The 13 shipped M1 features read at most ~50 bars (~12.5 hours) of history,
which is structurally too short-sighted to describe context for a 24-hour
target. This module adds exactly the nine features the research journal
declared before any result was computed - trailing windows only, no search,
no alternatives tried.

Causality is by construction: every value is a function of the bar at
`feature_timestamp` and the bars before it, built from `shift(+k)` and
trailing `rolling` windows. There is no negative shift, no centered window,
no backfill and no interpolation in this module.

**Missing-bar tolerance, stated rather than hidden.** The M1 layer's policy -
a window covering any hole is NaN - is correct at 16 bars and fatal at 672:
the feed is missing 0.16% of bars, and a 672-bar window that refuses every
hole would be NaN on essentially every row. Long windows here therefore
require 98% of their observations (`LONG_WINDOW_MIN_FRACTION`) and compute
over what was published. The tolerance is a research-dataset decision, it is
recorded in every artifact, and endpoint returns still refuse a missing
endpoint outright.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

#: Long trailing windows, in 15-minute bars.
DAY_BARS = 96
FOUR_DAY_BARS = 384
WEEK_BARS = 672

#: A long rolling window must have observed at least this fraction of its bars.
LONG_WINDOW_MIN_FRACTION = 0.98

#: The extension features, in contract order.
EXTENSION_FEATURES: tuple[str, ...] = (
    "return_96",
    "return_384",
    "return_672",
    "realized_volatility_96",
    "realized_volatility_672",
    "vol_ratio_16_96",
    "high_672_proximity",
    "low_672_proximity",
    "xasset_return_96",
)


def _min_periods(window: int) -> int:
    return int(np.ceil(window * LONG_WINDOW_MIN_FRACTION))


def _endpoint_return(close: pd.Series, bars: int) -> pd.Series:
    """Simple return over `bars` grid positions; NaN when either endpoint is missing."""
    past = close.shift(bars)
    return close / past.where(past > 0.0) - 1.0


def compute_extension_features(
    observations: pd.DataFrame,
    base_features: pd.DataFrame,
    *,
    other_close: pd.Series,
) -> pd.DataFrame:
    """The nine predeclared long-lookback features, positionally aligned.

    `other_close` is the *other* symbol's close series on the identical shared
    grid, so `xasset_return_96` at position i reads only the other market's
    bars at or before position i - the same instant, never later.
    """
    close = observations["close"].astype("float64")
    high = observations["high"].astype("float64")
    low = observations["low"].astype("float64")
    return_1 = base_features["return_1"].astype("float64")
    rv_16 = base_features["realized_volatility_16"].astype("float64")

    rv_96 = return_1.rolling(DAY_BARS, min_periods=_min_periods(DAY_BARS)).std(ddof=0)
    rv_672 = return_1.rolling(WEEK_BARS, min_periods=_min_periods(WEEK_BARS)).std(ddof=0)
    high_672 = high.rolling(WEEK_BARS, min_periods=_min_periods(WEEK_BARS)).max()
    low_672 = low.rolling(WEEK_BARS, min_periods=_min_periods(WEEK_BARS)).min()

    computed = {
        "return_96": _endpoint_return(close, DAY_BARS),
        "return_384": _endpoint_return(close, FOUR_DAY_BARS),
        "return_672": _endpoint_return(close, WEEK_BARS),
        "realized_volatility_96": rv_96,
        "realized_volatility_672": rv_672,
        "vol_ratio_16_96": rv_16 / rv_96.where(rv_96 > 0.0) - 1.0,
        "high_672_proximity": close / high_672.where(high_672 > 0.0) - 1.0,
        "low_672_proximity": close / low_672.where(low_672 > 0.0) - 1.0,
        "xasset_return_96": _endpoint_return(other_close.astype("float64"), DAY_BARS),
    }
    frame = pd.DataFrame(
        {name: computed[name].astype("float64") for name in EXTENSION_FEATURES},
        index=observations.index,
    )
    return frame[list(EXTENSION_FEATURES)]


__all__ = [
    "DAY_BARS",
    "EXTENSION_FEATURES",
    "FOUR_DAY_BARS",
    "LONG_WINDOW_MIN_FRACTION",
    "WEEK_BARS",
    "compute_extension_features",
]
