"""Phase 3 tests: EMA 20 / EMA 50 crossover signal generation.

Every test is offline and needs no credentials. Prices are deterministic
synthetic series, and the expected crossovers are derived from an independent
reference EMA implemented here in plain Python - the tests assert the intended
crossing semantics rather than snapshotting whatever the module returns.
"""

from __future__ import annotations

import dataclasses
import inspect
import socket
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from autotrader.data.historical import CANONICAL_COLUMNS
from autotrader.strategies import ema_cross
from autotrader.strategies.ema_cross import (
    BUY_REASON,
    EXIT_REASON,
    FAST_EMA_COLUMN,
    FAST_PERIOD,
    SLOW_EMA_COLUMN,
    SLOW_PERIOD,
    Signal,
    SignalType,
    StrategyInputError,
    add_ema_columns,
    generate_ema_cross_signals,
)

FIRST_BAR = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
STEP = timedelta(minutes=15)

_FLOAT_COLUMNS = ("open", "high", "low", "close", "volume", "trade_count", "vwap")


# --------------------------------------------------------------------------
# Synthetic bars
# --------------------------------------------------------------------------


def make_bars(
    closes: list[float],
    symbol: str = "SPY",
    start: datetime = FIRST_BAR,
) -> pd.DataFrame:
    """Build a canonical bar frame whose closes are exactly `closes`."""
    prices = [float(close) for close in closes]
    timestamps = [start + STEP * index for index in range(len(prices))]
    frame = pd.DataFrame(
        {
            "timestamp": pd.Series(timestamps, dtype="datetime64[ns, UTC]"),
            "symbol": pd.Series([symbol] * len(prices), dtype="string"),
            "open": prices,
            "high": [price + 0.5 for price in prices],
            "low": [price - 0.5 for price in prices],
            "close": prices,
            "volume": [1_000.0] * len(prices),
            "trade_count": [10.0] * len(prices),
            "vwap": prices,
        }
    )
    for column in _FLOAT_COLUMNS:
        frame[column] = frame[column].astype("float64")
    return frame[list(CANONICAL_COLUMNS)]


def level(count: int, price: float) -> list[float]:
    """A flat run of `count` bars at `price`."""
    return [float(price)] * count


#: Flat -> rally: the fast EMA crosses above on the first rally bar (index 60).
RALLY = level(60, 100.0) + level(40, 120.0)

#: Flat -> rally -> selloff: one BUY, then one EXIT.
RALLY_THEN_SELLOFF = RALLY + level(60, 80.0)

#: Two full cycles, used as the oracle-comparison series.
MULTI_CYCLE = RALLY_THEN_SELLOFF + level(50, 130.0) + level(50, 60.0)


# --------------------------------------------------------------------------
# Independent reference implementation
# --------------------------------------------------------------------------


def reference_ema(closes: list[float], period: int) -> list[float | None]:
    """The `adjust=False` EMA, written out longhand.

    ``ema[0] = close[0]`` and ``ema[i] = ema[i-1] + alpha * (close[i] - ema[i-1])``
    with ``alpha = 2 / (period + 1)``. The first ``period - 1`` outputs are
    masked, mirroring ``min_periods=period``.
    """
    alpha = 2.0 / (period + 1.0)
    current: float | None = None
    values: list[float | None] = []
    for index, close in enumerate(closes):
        current = close if current is None else current + alpha * (close - current)
        values.append(current if index >= period - 1 else None)
    return values


def reference_crossings(closes: list[float]) -> list[tuple[int, SignalType]]:
    """Bar indices where the reference EMAs cross, per the specified rules."""
    fast = reference_ema(closes, FAST_PERIOD)
    slow = reference_ema(closes, SLOW_PERIOD)
    crossings: list[tuple[int, SignalType]] = []
    for index in range(1, len(closes)):
        previous_fast, previous_slow = fast[index - 1], slow[index - 1]
        current_fast, current_slow = fast[index], slow[index]
        if None in (previous_fast, previous_slow, current_fast, current_slow):
            continue
        if previous_fast <= previous_slow and current_fast > current_slow:
            crossings.append((index, SignalType.BUY))
        elif previous_fast >= previous_slow and current_fast < current_slow:
            crossings.append((index, SignalType.EXIT))
    return crossings


def assert_crossover_at(closes: list[float], index: int, signal_type: SignalType) -> None:
    """Assert the reference EMAs really do cross at `index`, and only there."""
    fast = reference_ema(closes, FAST_PERIOD)
    slow = reference_ema(closes, SLOW_PERIOD)
    previous_fast, previous_slow = fast[index - 1], slow[index - 1]
    current_fast, current_slow = fast[index], slow[index]
    assert previous_fast is not None and previous_slow is not None
    if signal_type is SignalType.BUY:
        assert previous_fast <= previous_slow
        assert current_fast > current_slow
    else:
        assert previous_fast >= previous_slow
        assert current_fast < current_slow


# --------------------------------------------------------------------------
# Signal model
# --------------------------------------------------------------------------


def test_signal_type_has_exactly_buy_and_exit() -> None:
    assert [member.name for member in SignalType] == ["BUY", "EXIT"]
    assert SignalType.BUY.value == "BUY"
    assert SignalType.EXIT.value == "EXIT"


def test_signal_is_frozen_and_compares_by_value() -> None:
    timestamp = pd.Timestamp(FIRST_BAR)
    signal = Signal(timestamp=timestamp, symbol="SPY", type=SignalType.BUY, reason=BUY_REASON)

    assert signal.timestamp == timestamp
    assert signal.symbol == "SPY"
    assert signal.type is SignalType.BUY
    assert signal.reason == BUY_REASON
    assert signal == Signal(
        timestamp=timestamp, symbol="SPY", type=SignalType.BUY, reason=BUY_REASON
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        signal.symbol = "QQQ"  # type: ignore[misc]


def test_signal_carries_no_price_or_execution_fields() -> None:
    # A signal is an observation, not a trade: Phase 4 decides execution.
    field_names = {field.name for field in dataclasses.fields(Signal)}
    assert field_names == {"timestamp", "symbol", "type", "reason"}


def test_periods_are_fixed_at_twenty_and_fifty() -> None:
    assert FAST_PERIOD == 20
    assert SLOW_PERIOD == 50


# --------------------------------------------------------------------------
# Input contract
# --------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["timestamp", "symbol", "close"])
def test_missing_required_column_is_rejected(missing: str) -> None:
    bars = make_bars(RALLY).drop(columns=[missing])
    with pytest.raises(StrategyInputError) as excinfo:
        generate_ema_cross_signals(bars)
    assert missing in str(excinfo.value)


def test_empty_bars_produce_no_signals() -> None:
    assert generate_ema_cross_signals(make_bars([])) == []


def test_multiple_symbols_are_rejected() -> None:
    spy = make_bars(level(60, 100.0), symbol="SPY")
    qqq = make_bars(level(60, 100.0), symbol="QQQ", start=FIRST_BAR + STEP * 60)
    bars = pd.concat([spy, qqq], ignore_index=True)

    # Timestamps stay ascending, so only the symbol rule is violated.
    assert bars["timestamp"].is_monotonic_increasing
    with pytest.raises(StrategyInputError) as excinfo:
        generate_ema_cross_signals(bars)
    message = str(excinfo.value)
    assert "exactly one symbol" in message
    assert "QQQ" in message and "SPY" in message


def test_unsorted_timestamps_are_rejected() -> None:
    bars = make_bars(RALLY).iloc[::-1].reset_index(drop=True)
    with pytest.raises(StrategyInputError) as excinfo:
        generate_ema_cross_signals(bars)
    assert "ascending" in str(excinfo.value)


def test_a_single_out_of_order_bar_is_rejected() -> None:
    bars = make_bars(RALLY)
    swapped = bars.iloc[[*range(70), 71, 70, *range(72, len(bars))]].reset_index(drop=True)
    with pytest.raises(StrategyInputError):
        generate_ema_cross_signals(swapped)


def test_result_does_not_depend_on_the_frame_index() -> None:
    # Bars may arrive with any index; signals must be read positionally.
    bars = make_bars(MULTI_CYCLE)
    baseline = generate_ema_cross_signals(bars)
    assert baseline

    duplicated_index = bars.copy()
    duplicated_index.index = [7] * len(bars)
    descending_index = bars.copy()
    descending_index.index = range(len(bars) - 1, -1, -1)

    assert generate_ema_cross_signals(duplicated_index) == baseline
    assert generate_ema_cross_signals(descending_index) == baseline


def test_input_frame_is_not_mutated() -> None:
    bars = make_bars(RALLY_THEN_SELLOFF)
    before = bars.copy(deep=True)

    generate_ema_cross_signals(bars)
    add_ema_columns(bars)

    assert list(bars.columns) == list(CANONICAL_COLUMNS)
    assert bars.equals(before)


# --------------------------------------------------------------------------
# EMA calculation and warm-up
# --------------------------------------------------------------------------


def test_ema_columns_match_the_reference_recursion() -> None:
    closes = MULTI_CYCLE
    enriched = add_ema_columns(make_bars(closes))

    for column, period in ((FAST_EMA_COLUMN, FAST_PERIOD), (SLOW_EMA_COLUMN, SLOW_PERIOD)):
        expected = reference_ema(closes, period)
        produced = enriched[column].tolist()
        assert len(produced) == len(expected)
        for index, (value, reference) in enumerate(zip(produced, expected, strict=True)):
            if reference is None:
                assert pd.isna(value), f"{column}[{index}] should be masked during warm-up"
            else:
                assert value == pytest.approx(reference, rel=1e-12), f"{column}[{index}]"


def test_ema_warm_up_is_masked_until_the_period_is_observed() -> None:
    enriched = add_ema_columns(make_bars(MULTI_CYCLE))

    assert enriched[FAST_EMA_COLUMN].iloc[: FAST_PERIOD - 1].isna().all()
    assert not pd.isna(enriched[FAST_EMA_COLUMN].iloc[FAST_PERIOD - 1])
    assert enriched[SLOW_EMA_COLUMN].iloc[: SLOW_PERIOD - 1].isna().all()
    assert not pd.isna(enriched[SLOW_EMA_COLUMN].iloc[SLOW_PERIOD - 1])


def test_ema_of_a_constant_series_is_that_constant() -> None:
    enriched = add_ema_columns(make_bars(level(60, 100.0)))
    assert enriched[FAST_EMA_COLUMN].iloc[-1] == pytest.approx(100.0)
    assert enriched[SLOW_EMA_COLUMN].iloc[-1] == pytest.approx(100.0)


def test_no_signal_before_the_slow_ema_has_warmed_up() -> None:
    # A violent crossover inside the first 40 bars must still produce nothing,
    # because EMA50 has no value yet.
    early = level(10, 100.0) + level(15, 140.0) + level(15, 60.0)
    assert len(early) < SLOW_PERIOD
    assert generate_ema_cross_signals(make_bars(early)) == []


def test_first_actionable_bar_is_after_the_slow_ema_warm_up() -> None:
    bars = make_bars(MULTI_CYCLE)
    signals = generate_ema_cross_signals(bars)
    earliest_allowed = bars["timestamp"].iloc[SLOW_PERIOD]

    assert signals
    assert all(signal.timestamp >= earliest_allowed for signal in signals)


# --------------------------------------------------------------------------
# Crossover semantics
# --------------------------------------------------------------------------


def test_buy_is_emitted_on_the_bar_the_fast_ema_crosses_above() -> None:
    bars = make_bars(RALLY)
    signals = generate_ema_cross_signals(bars)

    assert len(signals) == 1
    signal = signals[0]
    assert signal.type is SignalType.BUY
    assert signal.symbol == "SPY"
    assert signal.reason == BUY_REASON

    # The rally begins at index 60; that is the first bar whose close can move
    # the fast EMA above the slow one.
    crossover_index = 60
    assert_crossover_at(RALLY, crossover_index, SignalType.BUY)
    assert signal.timestamp == bars["timestamp"].iloc[crossover_index]


def test_exit_is_emitted_on_the_bar_the_fast_ema_crosses_below() -> None:
    bars = make_bars(RALLY_THEN_SELLOFF)
    signals = generate_ema_cross_signals(bars)
    exits = [signal for signal in signals if signal.type is SignalType.EXIT]

    assert len(exits) == 1
    exit_signal = exits[0]
    assert exit_signal.reason == EXIT_REASON

    expected_index = next(
        index for index, kind in reference_crossings(RALLY_THEN_SELLOFF) if kind is SignalType.EXIT
    )
    assert expected_index > 100  # inside the selloff, not on its first bar
    assert_crossover_at(RALLY_THEN_SELLOFF, expected_index, SignalType.EXIT)
    assert exit_signal.timestamp == bars["timestamp"].iloc[expected_index]


def test_buy_is_not_repeated_while_the_fast_ema_stays_above() -> None:
    closes = RALLY
    signals = generate_ema_cross_signals(make_bars(closes))

    fast = reference_ema(closes, FAST_PERIOD)
    slow = reference_ema(closes, SLOW_PERIOD)
    bars_above = [
        index
        for index in range(61, len(closes))
        if fast[index] is not None and slow[index] is not None and fast[index] > slow[index]
    ]

    # The condition holds on every remaining bar, yet only the crossing bar signals.
    assert len(bars_above) == len(closes) - 61
    assert [signal.type for signal in signals] == [SignalType.BUY]


def test_exit_is_not_repeated_while_the_fast_ema_stays_below() -> None:
    closes = RALLY_THEN_SELLOFF
    signals = generate_ema_cross_signals(make_bars(closes))
    exit_index = next(
        index for index, kind in reference_crossings(closes) if kind is SignalType.EXIT
    )

    fast = reference_ema(closes, FAST_PERIOD)
    slow = reference_ema(closes, SLOW_PERIOD)
    bars_below = [
        index for index in range(exit_index + 1, len(closes)) if fast[index] < slow[index]
    ]

    assert len(bars_below) == len(closes) - exit_index - 1
    assert [signal.type for signal in signals] == [SignalType.BUY, SignalType.EXIT]


def test_signals_match_an_independent_crossover_oracle() -> None:
    bars = make_bars(MULTI_CYCLE)
    signals = generate_ema_cross_signals(bars)
    expected = reference_crossings(MULTI_CYCLE)

    # The fixture must actually exercise both directions more than once.
    assert [kind for _, kind in expected].count(SignalType.BUY) >= 2
    assert [kind for _, kind in expected].count(SignalType.EXIT) >= 2

    assert [signal.type for signal in signals] == [kind for _, kind in expected]
    assert [signal.timestamp for signal in signals] == [
        bars["timestamp"].iloc[index] for index, _ in expected
    ]


def test_signals_are_sorted_ascending_by_timestamp() -> None:
    signals = generate_ema_cross_signals(make_bars(MULTI_CYCLE))
    timestamps = [signal.timestamp for signal in signals]

    assert len(timestamps) >= 4
    assert all(earlier < later for earlier, later in zip(timestamps, timestamps[1:], strict=False))


def test_reasons_are_stable_machine_strings() -> None:
    assert BUY_REASON == "EMA20_CROSS_ABOVE_EMA50"
    assert EXIT_REASON == "EMA20_CROSS_BELOW_EMA50"

    for signal in generate_ema_cross_signals(make_bars(MULTI_CYCLE)):
        expected = BUY_REASON if signal.type is SignalType.BUY else EXIT_REASON
        assert signal.reason == expected


def test_strategy_never_produces_a_short_signal() -> None:
    assert not hasattr(SignalType, "SHORT")
    assert {member.value for member in SignalType} == {"BUY", "EXIT"}

    for closes in (RALLY, RALLY_THEN_SELLOFF, MULTI_CYCLE, level(80, 100.0)):
        for signal in generate_ema_cross_signals(make_bars(closes)):
            assert signal.type in (SignalType.BUY, SignalType.EXIT)


def test_repeated_runs_on_identical_input_are_identical() -> None:
    bars = make_bars(MULTI_CYCLE)
    runs = [generate_ema_cross_signals(bars) for _ in range(3)]

    assert runs[0]
    assert runs[0] == runs[1] == runs[2]
    assert runs[0] == generate_ema_cross_signals(make_bars(MULTI_CYCLE))


# --------------------------------------------------------------------------
# Offline / no-broker guarantees
# --------------------------------------------------------------------------


def test_signal_generation_makes_no_network_access(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("the strategy must not open a socket")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

    assert generate_ema_cross_signals(make_bars(RALLY_THEN_SELLOFF))


def test_signal_generation_needs_no_alpaca_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    assert generate_ema_cross_signals(make_bars(RALLY_THEN_SELLOFF))


def test_strategy_module_imports_no_broker_client() -> None:
    # docs/SPEC.md section 6A: a strategy module must never import a broker client.
    assert "alpaca" not in inspect.getsource(ema_cross).lower()
    imported = {
        getattr(value, "__name__", "")
        for value in vars(ema_cross).values()
        if inspect.ismodule(value)
    }
    assert not any(name.startswith("alpaca") for name in imported)
