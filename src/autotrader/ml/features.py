"""M1: backward-only feature computation over a bar grid.

Thirteen features, every one of them a function of the bar at
`feature_timestamp` and the bars before it. Nothing here reads a later bar,
and the property is enforced three ways rather than asserted once:

1. **Structurally.** Every feature is built from `shift(+k)`, a trailing
   `rolling`, or an `ewm`. There is no `shift(-k)`, no `center=True`, no
   `bfill`, and no `interpolate` in this module, and a test greps for each.
2. **By declaration.** Each feature's `ColumnSpec` states its `lookback_bars`
   and declares `forward_bars=0`, which `ColumnSpec` enforces for the role.
3. **By experiment.** The truncation test computes the features over the first
   *n* bars and over all of them, and requires the first *n* rows to be
   identical. A feature that peeked would change when the future was removed.

**Windows are counted in grid positions, never in wall-clock time.** A
time-based window on an equity grid would treat the three days between Friday
15:45 and Monday 09:30 as a gap to be spanned, and a 32-*minute* window would
silently become a 2-bar window overnight. Positional windows count tradable
bars, which is what a horizon means in both books.

**The overnight gap is a feature, not an error.** On an equity grid,
`return_1` at the first bar of a session is an overnight return: a different
quantity from a fifteen-minute return, with a different distribution.
`prior_bar_crosses_session_gap` marks exactly those rows so a model can learn
the difference rather than have it smeared into one column. On a crypto grid
the flag is 0.0 everywhere, because a midnight rollover is not a gap.

**A missing bar is never filled.** The provider sometimes publishes nothing for
an interval. Those positions carry NaN prices, and because every rolling window
here requires a full complement of observations, a feature whose window covers
a hole is NaN rather than a value computed from a forward-filled price. The
count of real observations rides along in `bars_present_in_window` so the
builder can drop such rows on an explicit policy instead of a guess.

**Nothing here is normalized against the dataset.** No z-score over the whole
file, no min-max, no global mean. Scaling fitted on data that includes the test
period is look-ahead wearing a preprocessing hat; it belongs to a model
pipeline fitted on the training split alone, which is a V4 concern.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from autotrader.ml import MLError
from autotrader.ml.schema import FEATURE_WINDOW_BARS, ColumnRole, ColumnSpec
from autotrader.strategies.ema_cross import (
    FAST_EMA_COLUMN,
    FAST_PERIOD,
    SLOW_EMA_COLUMN,
    SLOW_PERIOD,
    add_ema_columns,
)

#: The columns `compute_features` reads. Produced by `autotrader.ml.dataset`
#: from the bars reindexed onto a grid, so a position with no published bar is
#: present as a row with NaN prices rather than absent.
OBSERVATION_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "is_present",
    "session_id",
    "session_bar_index",
    "session_bar_count",
)

#: Trailing windows, in bars. Named so a lookback in a `ColumnSpec` and the
#: window that produced it cannot drift apart silently.
SHORT_RETURN_BARS = 4
MEDIUM_RETURN_BARS = 16
VOLATILITY_BARS = 16
TRUE_RANGE_BARS = 14
VOLUME_BARS = 32

#: Every feature, in contract order, with the trailing window each one reads.
#:
#: `lookback_bars` counts the bars a value depends on including its own: a
#: one-bar return reads two closes, a 16-bar volatility reads seventeen. All
#: are float64 - including the two flags - so the feature block is one
#: homogeneous matrix a model can consume without a dtype branch.
FEATURE_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec(
        name="return_1",
        dtype="float64",
        role=ColumnRole.FEATURE,
        description=(
            "Simple return from the previous bar's close to this bar's close. "
            "On an equity grid the first bar of a session makes this an "
            "overnight return; prior_bar_crosses_session_gap marks those rows."
        ),
        lookback_bars=2,
    ),
    ColumnSpec(
        name="return_4",
        dtype="float64",
        role=ColumnRole.FEATURE,
        description="Simple close-to-close return over the last 4 grid bars (one hour).",
        lookback_bars=SHORT_RETURN_BARS + 1,
    ),
    ColumnSpec(
        name="return_16",
        dtype="float64",
        role=ColumnRole.FEATURE,
        description="Simple close-to-close return over the last 16 grid bars (four hours).",
        lookback_bars=MEDIUM_RETURN_BARS + 1,
    ),
    ColumnSpec(
        name="ema_20_gap",
        dtype="float64",
        role=ColumnRole.FEATURE,
        description=(
            "(close - EMA20) / EMA20. How far this close sits from the fast EMA "
            "the V0.2 strategy uses, as a fraction."
        ),
        lookback_bars=FAST_PERIOD,
    ),
    ColumnSpec(
        name="ema_20_50_gap",
        dtype="float64",
        role=ColumnRole.FEATURE,
        description=(
            "(EMA20 - EMA50) / EMA50. The crossover state as a continuous "
            "quantity rather than the discrete signal the strategy emits."
        ),
        lookback_bars=SLOW_PERIOD,
    ),
    ColumnSpec(
        name="realized_volatility_16",
        dtype="float64",
        role=ColumnRole.FEATURE,
        description=(
            "Population standard deviation of the last 16 one-bar returns. The "
            "column a volatility-scaled label threshold is measured against."
        ),
        lookback_bars=VOLATILITY_BARS + 1,
    ),
    ColumnSpec(
        name="true_range_ratio",
        dtype="float64",
        role=ColumnRole.FEATURE,
        description=(
            "True range of this bar divided by the previous close: the wider of "
            "the bar's own range and its gap from the previous close."
        ),
        lookback_bars=2,
    ),
    ColumnSpec(
        name="average_true_range_14",
        dtype="float64",
        role=ColumnRole.FEATURE,
        description="Mean true_range_ratio over the last 14 grid bars.",
        lookback_bars=TRUE_RANGE_BARS + 1,
    ),
    ColumnSpec(
        name="volume_ratio_32",
        dtype="float64",
        role=ColumnRole.FEATURE,
        description=(
            "This bar's volume divided by the mean volume of the last 32 bars, "
            "minus one. NaN when that mean is zero rather than infinite."
        ),
        lookback_bars=VOLUME_BARS,
    ),
    ColumnSpec(
        name="close_position_in_bar",
        dtype="float64",
        role=ColumnRole.FEATURE,
        description=(
            "(close - low) / (high - low) for this bar alone. NaN on a bar with "
            "no range. Intra-bar and therefore fully known once the bar closed."
        ),
        lookback_bars=1,
    ),
    ColumnSpec(
        name="bars_since_session_start",
        dtype="float64",
        role=ColumnRole.FEATURE,
        description=(
            "Position of this bar within its session: 0 at 09:30 on an equity "
            "session, 0 at 00:00 UTC on a crypto day. Derived from the calendar, "
            "so it does not depend on where the requested range begins."
        ),
        lookback_bars=1,
    ),
    ColumnSpec(
        name="session_progress",
        dtype="float64",
        role=ColumnRole.FEATURE,
        description=(
            "bars_since_session_start divided by the session's own bar count, so "
            "an early close and a full day are both 0.0 at the open and approach "
            "1.0 at their own last bar."
        ),
        lookback_bars=1,
    ),
    ColumnSpec(
        name="prior_bar_crosses_session_gap",
        dtype="float64",
        role=ColumnRole.FEATURE,
        description=(
            "1.0 when the previous grid bar belongs to a different session, so "
            "return_1 on this row is an overnight or weekend return; 0.0 "
            "otherwise; NaN on the first bar of the grid, which has no previous "
            "bar. Always 0.0 on a continuous crypto grid."
        ),
        lookback_bars=2,
    ),
)

#: The feature names, in contract order.
FEATURE_NAMES: tuple[str, ...] = tuple(column.name for column in FEATURE_COLUMNS)

#: The feature whose value a volatility-scaled label threshold is measured in.
#: Named here because it is a feature - backward-looking by construction - which
#: is what makes a threshold derived from it free of look-ahead.
VOLATILITY_FEATURE = "realized_volatility_16"


class FeatureError(MLError):
    """The observation frame does not satisfy the feature layer's input contract."""


def _require_observations(observations: pd.DataFrame) -> None:
    """Reject an observation frame missing a column the features read."""
    if not isinstance(observations, pd.DataFrame):
        raise FeatureError(f"Expected a DataFrame, got {type(observations).__name__}.")
    missing = [name for name in OBSERVATION_COLUMNS if name not in observations.columns]
    if missing:
        raise FeatureError(
            f"Observations are missing required column(s): {', '.join(missing)}. "
            f"The feature layer requires: {', '.join(OBSERVATION_COLUMNS)}."
        )
    if not observations.index.equals(pd.RangeIndex(len(observations))):
        raise FeatureError(
            "Observations must be indexed by grid position (a 0-based RangeIndex). "
            "Every window in this module counts bars, and a non-positional index "
            "would silently change what a window covers."
        )


def _safe_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """`numerator / denominator`, with NaN where the denominator is zero.

    Not infinity. An infinite feature survives every dtype check and every
    null check, then destroys whatever consumes it; a NaN is the same absence
    of information stated in the form the rest of the pipeline already handles.
    """
    safe = denominator.astype("float64").replace(0.0, np.nan)
    return numerator.astype("float64") / safe


def _trailing_return(close: pd.Series, bars: int) -> pd.Series:
    """Simple return over `bars` grid positions, ending at this bar."""
    return _safe_ratio(close, close.shift(bars)) - 1.0


def compute_features(observations: pd.DataFrame, *, has_session_gaps: bool) -> pd.DataFrame:
    """Compute every feature over `observations`, positionally and backwards only.

    Returns a new frame with exactly `FEATURE_NAMES`, in contract order, all
    float64, indexed like the input. The supplied frame is not modified.

    `has_session_gaps` comes from the grid and decides one thing: whether a
    change of `session_id` between two adjacent bars is a real market break.
    On an equity grid it is; on a crypto grid the identifier is a UTC date
    that rolls over at midnight while the market keeps trading, so the flag
    stays 0.0 and the caller cannot accidentally teach a model that midnight
    is a weekend.

    Rows early in the grid are NaN until their window is filled, and rows whose
    rolling window covers a bar the provider never published stay NaN too:
    every `rolling` below demands a full complement of observations, so a hole
    propagates instead of being bridged by a stale price. The two EMA features
    are the documented exception - `ewm` continues across a missing bar rather
    than resetting - which is why `bars_present_in_window` travels with every
    row and the builder can drop rows whose window was not fully observed.
    """
    _require_observations(observations)
    frame = observations.reset_index(drop=True)

    close = frame["close"].astype("float64")
    high = frame["high"].astype("float64")
    low = frame["low"].astype("float64")
    volume = frame["volume"].astype("float64")
    previous_close = close.shift(1)

    # `add_ema_columns` is the strategy layer's own definition of an EMA -
    # recursive, adjust=False, masked through its warm-up. Reused rather than
    # re-derived so a feature named ema_20 and the strategy's ema_20 cannot
    # come to mean two different numbers.
    ema = add_ema_columns(frame[["timestamp", "symbol", "close"]])
    fast_ema = ema[FAST_EMA_COLUMN].astype("float64")
    slow_ema = ema[SLOW_EMA_COLUMN].astype("float64")

    return_1 = _trailing_return(close, 1)

    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    # `.max(axis=1)` skips NaN, so the first bar - which has no previous close -
    # would otherwise report the bar's own range as a true range computed from
    # three terms. Mask it back to NaN: the quantity is undefined there.
    true_range = true_range.where(previous_close.notna())
    true_range_ratio = _safe_ratio(true_range, previous_close)

    session_index = frame["session_bar_index"].astype("float64")
    session_count = frame["session_bar_count"].astype("float64")
    session_id = frame["session_id"].astype("string")
    previous_session_id = session_id.shift(1)
    if has_session_gaps:
        crosses_gap = (session_id != previous_session_id).astype("float64")
    else:
        crosses_gap = pd.Series(0.0, index=frame.index, dtype="float64")
    crosses_gap = crosses_gap.where(previous_session_id.notna())

    computed = {
        "return_1": return_1,
        "return_4": _trailing_return(close, SHORT_RETURN_BARS),
        "return_16": _trailing_return(close, MEDIUM_RETURN_BARS),
        "ema_20_gap": _safe_ratio(close - fast_ema, fast_ema),
        "ema_20_50_gap": _safe_ratio(fast_ema - slow_ema, slow_ema),
        "realized_volatility_16": return_1.rolling(
            VOLATILITY_BARS, min_periods=VOLATILITY_BARS
        ).std(ddof=0),
        "true_range_ratio": true_range_ratio,
        "average_true_range_14": true_range_ratio.rolling(
            TRUE_RANGE_BARS, min_periods=TRUE_RANGE_BARS
        ).mean(),
        "volume_ratio_32": _safe_ratio(
            volume, volume.rolling(VOLUME_BARS, min_periods=VOLUME_BARS).mean()
        )
        - 1.0,
        "close_position_in_bar": _safe_ratio(close - low, high - low),
        "bars_since_session_start": session_index,
        "session_progress": _safe_ratio(session_index, session_count),
        "prior_bar_crosses_session_gap": crosses_gap,
    }
    features = pd.DataFrame(
        {name: computed[name].astype("float64") for name in FEATURE_NAMES},
        index=frame.index,
    )
    return features[list(FEATURE_NAMES)]


def bars_present_in_window(observations: pd.DataFrame) -> pd.Series:
    """How many of the trailing `FEATURE_WINDOW_BARS` bars were published.

    Counts this bar too, so a fully populated window reports
    `FEATURE_WINDOW_BARS`. Early rows report fewer simply because fewer bars
    exist yet, which is why the builder judges completeness only from the row
    where the window is first full.
    """
    _require_observations(observations)
    present = observations["is_present"].astype("float64").reset_index(drop=True)
    counted = present.rolling(FEATURE_WINDOW_BARS, min_periods=1).sum()
    return counted.astype("int64")


__all__ = [
    "FEATURE_COLUMNS",
    "FEATURE_NAMES",
    "MEDIUM_RETURN_BARS",
    "OBSERVATION_COLUMNS",
    "SHORT_RETURN_BARS",
    "TRUE_RANGE_BARS",
    "VOLATILITY_BARS",
    "VOLATILITY_FEATURE",
    "VOLUME_BARS",
    "FeatureError",
    "bars_present_in_window",
    "compute_features",
]
