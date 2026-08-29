"""Decision Engine tests: the vectorized feature layer.

Every indicator here is checked against an independent reference implemented in
plain Python in this file, rather than against a snapshot of what the module
happened to return. A snapshot test passes just as happily when the module is
wrong from the first commit.

The load-bearing test is `test_truncating_the_bars_changes_no_earlier_feature`.
It is the observable form of "no look-ahead": if a feature at bar *t* were
influenced by any bar after *t*, then computing over the first *t* bars would
produce a different value than computing over all of them and reading row *t*.
It does not, for any column, at any truncation point.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from autotrader.decision.config import IndicatorPeriods
from autotrader.decision.contract import DecisionInputError
from autotrader.decision.features import (
    FEATURE_COLUMNS,
    NEUTRAL_RATIO,
    SCORED_FEATURES,
    compute_features,
    latest_feature_row,
    missing_scored_features,
)
from autotrader.strategies.ema_cross import add_ema_columns

FIRST_BAR = datetime(2025, 1, 1, tzinfo=UTC)
STEP = timedelta(minutes=15)
PERIODS = IndicatorPeriods()


def make_bars(
    closes: list[float],
    *,
    symbol: str = "BTC/USD",
    volumes: list[float] | None = None,
    spread: float = 0.5,
    start: datetime = FIRST_BAR,
) -> pd.DataFrame:
    """A canonical frame whose closes are exactly `closes`.

    Highs and lows sit a fixed distance either side of the close, so the true
    range is predictable and a test that cares about ATR can reason about it.
    """
    prices = [float(close) for close in closes]
    sizes = [100.0] * len(prices) if volumes is None else [float(v) for v in volumes]
    return pd.DataFrame(
        {
            "timestamp": [start + STEP * index for index in range(len(prices))],
            "symbol": [symbol] * len(prices),
            "open": prices,
            "high": [price + spread for price in prices],
            "low": [price - spread for price in prices],
            "close": prices,
            "volume": sizes,
            "trade_count": [10] * len(prices),
            "vwap": prices,
        }
    )


def wobbly_closes(count: int) -> list[float]:
    """A deterministic series with trend, cycle, and a short shock."""
    closes = []
    for index in range(count):
        value = 100.0 + 0.04 * index + 6.0 * math.sin(index / 11.0) + 2.0 * math.cos(index / 3.0)
        if 150 <= index < 158:
            value += 9.0
        closes.append(value)
    return closes


# --------------------------------------------------------------------------
# Independent references
# --------------------------------------------------------------------------


def reference_ema(values: list[float], period: int) -> list[float]:
    """`adjust=False` recursion seeded on the first value, masked during warm-up."""
    alpha = 2.0 / (period + 1.0)
    return _recursive_average(values, alpha, period)


def reference_wilder(values: list[float | None], period: int) -> list[float]:
    """Wilder's smoothing: the same recursion with ``alpha = 1 / period``."""
    return _recursive_average(values, 1.0 / period, period)


def _recursive_average(values: list[float | None], alpha: float, period: int) -> list[float]:
    """Shared recursion. NaN inputs are skipped and do not count towards warm-up."""
    output: list[float] = []
    average: float | None = None
    seen = 0
    for value in values:
        if value is None or value != value:
            output.append(float("nan"))
            continue
        seen += 1
        average = value if average is None else average + alpha * (value - average)
        output.append(average if seen >= period else float("nan"))
    return output


def reference_true_range(bars: pd.DataFrame) -> list[float]:
    """Undefined on the first bar, because there is no previous close."""
    highs = bars["high"].tolist()
    lows = bars["low"].tolist()
    closes = bars["close"].tolist()
    ranges: list[float] = [float("nan")]
    for index in range(1, len(closes)):
        previous = closes[index - 1]
        ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - previous),
                abs(lows[index] - previous),
            )
        )
    return ranges


def reference_rsi(closes: list[float], period: int) -> list[float]:
    """Wilder's RSI, with both degenerate cases resolved explicitly."""
    gains: list[float | None] = [None]
    losses: list[float | None] = [None]
    for index in range(1, len(closes)):
        change = closes[index] - closes[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    average_gain = reference_wilder(gains, period)
    average_loss = reference_wilder(losses, period)

    output: list[float] = []
    for gain, loss in zip(average_gain, average_loss, strict=True):
        if gain != gain or loss != loss:
            output.append(float("nan"))
        elif loss > 0:
            output.append(100.0 - 100.0 / (1.0 + gain / loss))
        elif gain > 0:
            output.append(100.0)
        else:
            output.append(50.0)
    return output


def assert_series_matches(actual: pd.Series, expected: list[float]) -> None:
    """Element-wise comparison that treats NaN as equal to NaN."""
    assert len(actual) == len(expected)
    for index, (left, right) in enumerate(zip(actual.tolist(), expected, strict=True)):
        if left != left or right != right:
            assert left != left and right != right, f"row {index}: {left} vs {right}"
        else:
            assert left == pytest.approx(right, rel=1e-12, abs=1e-12), f"row {index}"


# --------------------------------------------------------------------------
# Indicators against the references
# --------------------------------------------------------------------------


def test_the_exponential_averages_match_an_independent_reference() -> None:
    closes = wobbly_closes(300)
    features = compute_features(make_bars(closes))

    assert_series_matches(features["ema_fast"], reference_ema(closes, PERIODS.ema_fast))
    assert_series_matches(features["ema_slow"], reference_ema(closes, PERIODS.ema_slow))


def test_the_exponential_averages_are_bit_identical_to_the_v1_strategy() -> None:
    """V2's `ema_fast` is the same series V1 calls `ema_20`, not merely close to it."""
    bars = make_bars(wobbly_closes(300))
    v1 = add_ema_columns(bars)
    v2 = compute_features(bars)

    assert v1["ema_20"].equals(v2["ema_fast"])
    assert v1["ema_50"].equals(v2["ema_slow"])


def test_atr_matches_wilder_smoothing_of_the_true_range() -> None:
    bars = make_bars(wobbly_closes(300))
    features = compute_features(bars)
    expected = reference_wilder(reference_true_range(bars), PERIODS.atr_period)

    assert_series_matches(features["atr"], expected)


def test_the_first_bar_contributes_no_true_range() -> None:
    """ATR warm-up costs one extra bar because the first has no previous close."""
    features = compute_features(make_bars(wobbly_closes(120)))

    assert features["atr"].first_valid_index() == PERIODS.atr_warmup - 1


def test_rsi_matches_an_independent_wilder_implementation() -> None:
    closes = wobbly_closes(300)
    features = compute_features(make_bars(closes))

    assert_series_matches(features["rsi"], reference_rsi(closes, PERIODS.rsi_period))


def test_rsi_saturates_at_one_hundred_when_every_bar_gains() -> None:
    features = compute_features(make_bars([100.0 + index for index in range(60)]))

    assert features["rsi"].dropna().eq(100.0).all()
    assert features["rsi_centered"].dropna().eq(1.0).all()


def test_rsi_is_fifty_on_a_perfectly_flat_market() -> None:
    """No gains and no losses is neither overbought nor oversold, not a divide by zero."""
    features = compute_features(make_bars([100.0] * 60))

    assert features["rsi"].dropna().eq(50.0).all()
    assert features["rsi_centered"].dropna().eq(0.0).all()


def test_the_macd_histogram_matches_the_reference_chain() -> None:
    closes = wobbly_closes(300)
    features = compute_features(make_bars(closes))

    macd = [
        fast - slow
        for fast, slow in zip(
            reference_ema(closes, PERIODS.macd_fast),
            reference_ema(closes, PERIODS.macd_slow),
            strict=True,
        )
    ]
    signal = reference_ema(macd, PERIODS.macd_signal)

    assert_series_matches(features["macd"], macd)
    assert_series_matches(features["macd_signal_line"], signal)
    assert_series_matches(
        features["macd_hist"],
        [line - point for line, point in zip(macd, signal, strict=True)],
    )


# --------------------------------------------------------------------------
# No look-ahead
# --------------------------------------------------------------------------


@pytest.mark.parametrize("truncation", [120, 150, 199, 240, 299])
def test_truncating_the_bars_changes_no_earlier_feature(truncation: int) -> None:
    """CRITICAL. docs/SPEC.md section 7F, as an observable property.

    A feature at bar *t* that used any bar after *t* would necessarily differ
    when the later bars are not there. Every column is compared, warm-up NaNs
    included, and the comparison is exact rather than approximate: this is a
    forward recursion over the same values in the same order, so anything other
    than bit equality would itself be a finding.
    """
    bars = make_bars(wobbly_closes(300))
    whole = compute_features(bars).iloc[:truncation].reset_index(drop=True)
    truncated = compute_features(bars.iloc[:truncation].copy())

    for column in FEATURE_COLUMNS:
        assert whole[column].equals(truncated[column]), column


def test_a_future_shock_cannot_reach_back_into_an_earlier_bar() -> None:
    """The same property stated the way it would actually bite."""
    calm = wobbly_closes(300)
    shocked = list(calm)
    shocked[250] = shocked[250] * 3.0

    before = compute_features(make_bars(calm)).iloc[:250]
    after = compute_features(make_bars(shocked)).iloc[:250]

    for column in FEATURE_COLUMNS:
        assert before[column].equals(after[column]), column


# --------------------------------------------------------------------------
# Warm-up
# --------------------------------------------------------------------------


def test_every_scored_feature_becomes_available_at_exactly_required_bars() -> None:
    """`required_bars` is the truth, not an estimate padded for comfort."""
    features = compute_features(make_bars(wobbly_closes(400)))
    required = PERIODS.required_bars

    for column in SCORED_FEATURES:
        assert not pd.isna(features[column].iloc[required - 1]), column


def test_at_least_one_scored_feature_is_still_missing_one_bar_earlier() -> None:
    """Otherwise `required_bars` would be overstating the cost of a decision."""
    features = compute_features(make_bars(wobbly_closes(400)))
    row = features.iloc[PERIODS.required_bars - 2]

    assert missing_scored_features({column: float(row[column]) for column in FEATURE_COLUMNS})


def test_the_binding_warm_up_is_the_slow_ema_slope() -> None:
    """Stated in the config docstring; asserted here so it cannot quietly change."""
    features = compute_features(make_bars(wobbly_closes(400)))

    assert features["ema_slope_z"].first_valid_index() == PERIODS.required_bars - 1


def test_warm_up_rows_are_present_and_nan_rather_than_dropped() -> None:
    bars = make_bars(wobbly_closes(200))
    features = compute_features(bars)

    assert len(features) == len(bars)
    assert features["timestamp"].tolist() == bars["timestamp"].tolist()


# --------------------------------------------------------------------------
# Determinism and purity
# --------------------------------------------------------------------------


def test_the_same_bars_produce_identical_features_every_time() -> None:
    bars = make_bars(wobbly_closes(300))

    assert compute_features(bars).equals(compute_features(bars))


def test_computing_features_does_not_modify_the_supplied_frame() -> None:
    bars = make_bars(wobbly_closes(200))
    before = bars.copy(deep=True)

    compute_features(bars)

    assert bars.equals(before)


def test_scored_features_are_invariant_to_the_price_scale() -> None:
    """A ten-times more expensive market is not a ten-times stronger signal.

    Every directional feature is a ratio to ATR and then to its own spread, so
    multiplying every price by a constant must leave the scored values alone.
    This is what makes one set of thresholds meaningful across BTC/USD at five
    figures and a share priced in tens.
    """
    closes = wobbly_closes(300)
    cheap = compute_features(make_bars(closes, spread=0.5)).iloc[-1]
    dear = compute_features(make_bars([close * 10.0 for close in closes], spread=5.0)).iloc[-1]

    for column in ("ema_spread_z", "ema_slope_z", "macd_hist_z", "return_z", "rsi_centered"):
        assert float(cheap[column]) == pytest.approx(float(dear[column]), rel=1e-9, abs=1e-9)


# --------------------------------------------------------------------------
# Degenerate inputs
# --------------------------------------------------------------------------


def test_a_flat_market_produces_zero_rather_than_a_division_by_zero() -> None:
    """Zero range means no measurable move, which is 0.0 and never an infinity."""
    features = compute_features(make_bars([100.0] * 200, spread=0.0))
    row = latest_feature_row(features)

    assert row["atr"] == 0.0
    for column in ("ema_spread_atr", "ema_slope_atr", "macd_hist_atr", "return_atr"):
        assert row[column] == 0.0
    assert not missing_scored_features(row)


def test_a_zero_volume_baseline_reads_as_neutral_participation() -> None:
    """No baseline to compare against is "at baseline", not "no participation"."""
    features = compute_features(make_bars(wobbly_closes(200), volumes=[0.0] * 200))

    assert float(features["volume_ratio"].iloc[-1]) == NEUTRAL_RATIO


def test_volume_ratio_is_the_bar_over_its_own_rolling_median() -> None:
    volumes = [100.0] * 199 + [250.0]
    features = compute_features(make_bars(wobbly_closes(200), volumes=volumes))

    assert float(features["volume_ratio"].iloc[-1]) == pytest.approx(2.5)


def test_a_constant_feature_standardizes_to_zero_not_infinity() -> None:
    """A measurement that has not varied carries no directional information."""
    features = compute_features(make_bars([100.0] * 200, spread=0.0))

    assert float(features["ema_spread_z"].iloc[-1]) == 0.0
    assert float(features["return_z"].iloc[-1]) == 0.0


# --------------------------------------------------------------------------
# Schema and input contract
# --------------------------------------------------------------------------


def test_the_output_carries_exactly_the_declared_schema() -> None:
    features = compute_features(make_bars(wobbly_closes(150)))

    assert list(features.columns) == ["timestamp", "symbol", *FEATURE_COLUMNS]
    assert set(SCORED_FEATURES) <= set(FEATURE_COLUMNS)


def test_latest_feature_row_reports_every_declared_column() -> None:
    row = latest_feature_row(compute_features(make_bars(wobbly_closes(150))))

    assert set(row) == set(FEATURE_COLUMNS)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda frame: frame.drop(columns=["close"]), "missing required column"),
        (lambda frame: frame.iloc[::-1], "ordered ascending"),
        (lambda frame: pd.concat([frame, frame.iloc[[-1]]]), "must not repeat a timestamp"),
        (lambda frame: frame.assign(symbol="MIXED").iloc[:0], "must not be empty"),
    ],
)
def test_a_violated_bar_contract_is_refused_rather_than_worked_around(
    mutate: object, expected: str
) -> None:
    bars = make_bars(wobbly_closes(120))

    with pytest.raises(DecisionInputError, match=expected):
        compute_features(mutate(bars))  # type: ignore[operator]


def test_a_mixed_symbol_frame_is_refused() -> None:
    bars = make_bars(wobbly_closes(120))
    bars.loc[5, "symbol"] = "ETH/USD"

    with pytest.raises(DecisionInputError, match="exactly one symbol"):
        compute_features(bars)


def test_a_naive_timestamp_is_refused() -> None:
    bars = make_bars(wobbly_closes(120))
    bars["timestamp"] = bars["timestamp"].dt.tz_localize(None)

    with pytest.raises(DecisionInputError, match="timezone-aware"):
        compute_features(bars)


def test_a_bar_off_the_fifteen_minute_grid_is_refused() -> None:
    """A bar stamped 10:07 belongs to no bucket, and guessing one would be a fiction."""
    bars = make_bars(wobbly_closes(120))
    bars.loc[7, "timestamp"] = bars.loc[7, "timestamp"] + timedelta(minutes=7)

    with pytest.raises(DecisionInputError, match="boundary anchored to the UTC"):
        compute_features(bars)


def test_a_non_finite_price_is_refused_at_the_door() -> None:
    bars = make_bars(wobbly_closes(120))
    bars.loc[30, "close"] = float("nan")

    with pytest.raises(DecisionInputError, match="must be finite"):
        compute_features(bars)
