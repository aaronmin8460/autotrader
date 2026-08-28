"""Deterministic, vectorized, backward-looking feature computation.

Everything V2 and V3 measure is computed here, over a whole frame at once, and
scored elsewhere. The split is the boundary the research and model branches
integrate against: a backtester or a training run calls `compute_features` once
over a long history and gets every bar's measurements in one pass, then scores
whichever bars it cares about. Sliding a per-bar engine over a window would be
the same arithmetic done ten thousand times and - far worse - would be a second
implementation that could disagree with the live one.

**No look-ahead, structurally.** Every operation here is `ewm`, `rolling`,
`shift(+n)`, or elementwise. There is no negative shift, no `center=True`, no
`bfill`, no reindex, and no reversal anywhere in this module, and a test
asserts that by inspecting the parse tree rather than trusting this paragraph.
The consequence is the property that matters: truncating the frame after bar
*t* changes no value at or before bar *t* (docs/SPEC.md section 7F).

**Warm-up is NaN, never a partial value.** Every window carries `min_periods`
equal to its own length, so an average that has not yet seen enough bars is
absent rather than under-informed. A NaN here becomes an explicit
insufficient-history HOLD in the scoring layer, which is the whole point: an
engine that quietly scored a 3-bar "50-bar average" would be confidently wrong
in exactly the situation it should be silent.

**Directional features are expressed in ATR, then standardized.** A price
difference is in dollars, and dollars mean different things on BTC/USD and on
SPY. Dividing by ATR makes each one a number of typical bar ranges; dividing
*that* by its own trailing standard deviation makes it a number of typical
deviations. Neither step involves a tuned constant, and the result is
comparable across symbols, across asset classes, and across volatility regimes.

**Degenerate denominators resolve to a stated value, never to infinity.** A
perfectly flat market has zero range, so an ATR-relative measure of a move that
is itself zero is reported as 0.0 - no movement, no signal - rather than as a
division by zero. A baseline of zero volume is reported as 1.0, which is the
neutral "at baseline" reading. Both are choices, so both are written down here
rather than left to whatever IEEE-754 produces.
"""

from __future__ import annotations

import pandas as pd

from autotrader.decision.bars import normalize_bars
from autotrader.decision.config import IndicatorPeriods

#: Bumped whenever a column is added, removed, or redefined. It travels in the
#: policy metadata of every result so a stored decision can be matched to the
#: feature definitions that produced it - which is what makes a model trained
#: on V2 features reproducible after V2 features change.
FEATURE_SCHEMA_VERSION = "1"

#: Every column `compute_features` produces, in report order.
FEATURE_COLUMNS: tuple[str, ...] = (
    # Trend.
    "ema_fast",
    "ema_slow",
    "ema_spread_atr",
    "ema_spread_z",
    "ema_slope_atr",
    "ema_slope_z",
    # Momentum.
    "rsi",
    "rsi_centered",
    "macd",
    "macd_signal_line",
    "macd_hist",
    "macd_hist_atr",
    "macd_hist_z",
    "return_atr",
    "return_z",
    # Volatility.
    "atr",
    "atr_normalized",
    "volatility_baseline",
    "volatility_ratio",
    "realized_volatility",
    # Volume.
    "volume_baseline",
    "volume_ratio",
)

#: The columns a direction cannot be named without. Any of them NaN on the
#: evaluated bar is a HOLD with a feature-unavailable reason, never a zero
#: substituted for a measurement that was not taken.
SCORED_FEATURES: tuple[str, ...] = (
    "ema_spread_z",
    "ema_slope_z",
    "rsi_centered",
    "macd_hist_z",
    "return_z",
    "volatility_ratio",
    "volume_ratio",
)

#: The neutral reading for a ratio whose baseline is zero: exactly at baseline.
NEUTRAL_RATIO = 1.0


def _ema(values: pd.Series, period: int) -> pd.Series:
    """Recursive EMA with an explicit warm-up, matching C3's `add_ema_columns`.

    Same `adjust=False` recursion and same `min_periods` masking as the V1
    strategy, so that a V2 `ema_fast` on a 20-bar span is the identical series
    V1 calls `ema_20`. A test pins that equality: two exponential averages of
    the same prices that differ in the fourth decimal would make V1 and V2
    incomparable for no reason anyone would ever find.
    """
    return values.ewm(span=period, adjust=False, min_periods=period).mean()


def _wilder(values: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing: the recursion RSI and ATR were defined with.

    An EMA with ``alpha = 1 / period`` rather than ``2 / (period + 1)``. Using a
    conventional EMA here instead would produce numbers that look like RSI and
    ATR without being them, which is worse than either choice made openly.
    """
    return values.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _safe_ratio(
    numerator: pd.Series,
    denominator: pd.Series,
    *,
    degenerate: float = 0.0,
) -> pd.Series:
    """`numerator / denominator`, with a stated answer where the denominator vanishes.

    NaN in either operand propagates, because an undefined input must stay
    undefined rather than become `degenerate` and look measured.
    """
    result = pd.Series(float("nan"), index=numerator.index, dtype="float64")
    defined = numerator.notna() & denominator.notna()
    usable = defined & (denominator > 0)
    result.loc[usable] = numerator.loc[usable] / denominator.loc[usable]
    result.loc[defined & ~usable] = degenerate
    return result


def _true_range(bars: pd.DataFrame) -> pd.Series:
    """Wilder's true range, undefined on the first bar.

    The conventional shortcut sets the first bar's true range to its high-low
    span because there is no previous close. That value is a different quantity
    from every later one, and seeding a fourteen-bar average with it makes the
    first ATR silently wrong. It is left NaN instead, which costs exactly one
    bar of warm-up and makes every ATR in the output the same measurement.
    """
    high = bars["high"].astype("float64")
    low = bars["low"].astype("float64")
    previous_close = bars["close"].astype("float64").shift(1)
    spans = pd.DataFrame(
        {
            "high_low": high - low,
            "high_close": (high - previous_close).abs(),
            "low_close": (low - previous_close).abs(),
        }
    )
    return spans.max(axis=1, skipna=False)


def _rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI on `close`, in ``[0, 100]``.

    The two degenerate cases are resolved explicitly rather than left to
    ``x / 0``: a window with no losses is 100, and a window that is perfectly
    flat - no gains and no losses - is 50, because a market that has not moved
    is neither overbought nor oversold.
    """
    change = close.diff()
    average_gain = _wilder(change.clip(lower=0.0), period)
    average_loss = _wilder((-change).clip(lower=0.0), period)

    rsi = pd.Series(float("nan"), index=close.index, dtype="float64")
    defined = average_gain.notna() & average_loss.notna()
    moving = defined & (average_loss > 0)
    rsi.loc[moving] = 100.0 - (100.0 / (1.0 + average_gain.loc[moving] / average_loss.loc[moving]))
    only_gains = defined & (average_loss <= 0) & (average_gain > 0)
    rsi.loc[only_gains] = 100.0
    flat = defined & (average_loss <= 0) & (average_gain <= 0)
    rsi.loc[flat] = 50.0
    return rsi


def _standardize(raw: pd.Series, window: int) -> pd.Series:
    """`raw` in units of its own trailing standard deviation about zero.

    About zero, not about the trailing mean. Centring on the mean would ask
    "is this move unusual for this market lately?", and a market that has
    trended steadily for fifty bars would answer "no" at the moment the trend is
    strongest. The question here is "how large is this, on this market's own
    scale?", which is the numerator over the spread, with no re-centring.

    A constant feature has zero spread and is reported as 0.0: a measurement
    that has not varied carries no information about direction.
    """
    scale = raw.rolling(window, min_periods=window).std(ddof=0)
    return _safe_ratio(raw, scale)


def compute_features(
    bars: pd.DataFrame,
    *,
    periods: IndicatorPeriods | None = None,
) -> pd.DataFrame:
    """Every feature for every bar in `bars`. The supplied frame is not modified.

    Returns a frame carrying `timestamp`, `symbol`, and `FEATURE_COLUMNS`, with
    a fresh ``0..n-1`` index aligned to the validated bars. Warm-up rows are
    present and NaN rather than dropped, so a caller can join the output back
    onto its bars positionally without an off-by-one.
    """
    settings = periods or IndicatorPeriods()
    frame = normalize_bars(bars)

    close = frame["close"].astype("float64")
    volume = frame["volume"].astype("float64")

    atr = _wilder(_true_range(frame), settings.atr_period)
    atr_normalized = _safe_ratio(atr, close)

    ema_fast = _ema(close, settings.ema_fast)
    ema_slow = _ema(close, settings.ema_slow)
    ema_spread_atr = _safe_ratio(ema_fast - ema_slow, atr)
    ema_slope_atr = _safe_ratio(ema_slow - ema_slow.shift(settings.slope_lookback), atr)

    rsi = _rsi(close, settings.rsi_period)
    macd = _ema(close, settings.macd_fast) - _ema(close, settings.macd_slow)
    macd_signal_line = _ema(macd, settings.macd_signal)
    macd_hist = macd - macd_signal_line
    macd_hist_atr = _safe_ratio(macd_hist, atr)
    return_atr = _safe_ratio(close - close.shift(settings.return_lookback), atr)

    baseline = settings.baseline_bars
    volume_baseline = volume.rolling(baseline, min_periods=baseline).median()
    volatility_baseline = atr_normalized.rolling(baseline, min_periods=baseline).median()
    realized_volatility = close.pct_change().rolling(baseline, min_periods=baseline).std(ddof=0)

    standardization = settings.standardization_bars
    features = pd.DataFrame(
        {
            "timestamp": frame["timestamp"],
            "symbol": frame["symbol"],
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "ema_spread_atr": ema_spread_atr,
            "ema_spread_z": _standardize(ema_spread_atr, standardization),
            "ema_slope_atr": ema_slope_atr,
            "ema_slope_z": _standardize(ema_slope_atr, standardization),
            "rsi": rsi,
            "rsi_centered": (rsi - 50.0) / 50.0,
            "macd": macd,
            "macd_signal_line": macd_signal_line,
            "macd_hist": macd_hist,
            "macd_hist_atr": macd_hist_atr,
            "macd_hist_z": _standardize(macd_hist_atr, standardization),
            "return_atr": return_atr,
            "return_z": _standardize(return_atr, standardization),
            "atr": atr,
            "atr_normalized": atr_normalized,
            "volatility_baseline": volatility_baseline,
            "volatility_ratio": _safe_ratio(
                atr_normalized, volatility_baseline, degenerate=NEUTRAL_RATIO
            ),
            "realized_volatility": realized_volatility,
            "volume_baseline": volume_baseline,
            "volume_ratio": _safe_ratio(volume, volume_baseline, degenerate=NEUTRAL_RATIO),
        }
    )
    return features[["timestamp", "symbol", *FEATURE_COLUMNS]]


def latest_feature_row(features: pd.DataFrame) -> dict[str, float]:
    """The newest bar's features as plain floats, for the scoring layer.

    NaN is preserved rather than filled. A missing measurement has to survive
    into the scoring layer intact so it can become an explicit HOLD there,
    which is the opposite of what filling it would achieve.
    """
    if features.empty:
        return {}
    row = features.iloc[-1]
    return {column: float(row[column]) for column in FEATURE_COLUMNS}


def missing_scored_features(row: dict[str, float]) -> tuple[str, ...]:
    """Which scored features are absent on this bar, in report order."""
    return tuple(name for name in SCORED_FEATURES if name not in row or row[name] != row[name])


__all__ = [
    "FEATURE_COLUMNS",
    "FEATURE_SCHEMA_VERSION",
    "NEUTRAL_RATIO",
    "SCORED_FEATURES",
    "compute_features",
    "latest_feature_row",
    "missing_scored_features",
]
