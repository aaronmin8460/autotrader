"""Phase 4 tests: deterministic next-bar-open backtesting.

Every test is offline, needs no credentials, and builds small synthetic
frames. Accounting expectations are derived independently - by hand, or by
replaying the reported executions through a separate ledger - rather than by
snapshotting whatever the engine happened to return.

The no-look-ahead tests are the most important ones here. Their fixtures make
a signal bar's own prices wildly different from the next bar's open, so an
implementation that filled on the signal bar would produce obviously wrong
numbers instead of quietly plausible ones.
"""

from __future__ import annotations

import dataclasses
import inspect
import math
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from autotrader.backtest import engine
from autotrader.backtest.engine import (
    DEFAULT_INITIAL_CASH,
    STRATEGY_NAME,
    BacktestInputError,
    BacktestResult,
    Execution,
    ExecutionSide,
    run_backtest,
)
from autotrader.cli import app
from autotrader.data.historical import CANONICAL_COLUMNS
from autotrader.strategies import ema_cross
from autotrader.strategies.ema_cross import Signal, SignalType, generate_ema_cross_signals

FIRST_BAR = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
STEP = timedelta(minutes=15)

_FLOAT_COLUMNS = ("open", "high", "low", "close", "volume", "trade_count", "vwap")


# --------------------------------------------------------------------------
# Synthetic bars
# --------------------------------------------------------------------------


def make_bars(
    closes: list[float],
    opens: list[float] | None = None,
    symbol: str = "SPY",
    start: datetime = FIRST_BAR,
) -> pd.DataFrame:
    """A canonical bar frame with the given closes and (optionally) opens.

    `high` and `low` are widened to bound both, so any open/close pair the
    tests need stays valid under Phase 2's OHLC rules.
    """
    close_prices = [float(close) for close in closes]
    open_prices = close_prices[:] if opens is None else [float(price) for price in opens]
    assert len(open_prices) == len(close_prices)
    timestamps = [start + STEP * index for index in range(len(close_prices))]
    frame = pd.DataFrame(
        {
            "timestamp": pd.Series(timestamps, dtype="datetime64[ns, UTC]"),
            "symbol": pd.Series([symbol] * len(close_prices), dtype="string"),
            "open": open_prices,
            "high": [max(o, c) + 0.5 for o, c in zip(open_prices, close_prices, strict=True)],
            "low": [min(o, c) - 0.5 for o, c in zip(open_prices, close_prices, strict=True)],
            "close": close_prices,
            "volume": [1_000.0] * len(close_prices),
            "trade_count": [10.0] * len(close_prices),
            "vwap": close_prices,
        }
    )
    for column in _FLOAT_COLUMNS:
        frame[column] = frame[column].astype("float64")
    return frame[list(CANONICAL_COLUMNS)]


def flat(count: int, price: float) -> list[float]:
    """A flat run of `count` bars at `price`."""
    return [float(price)] * count


def with_open(bars: pd.DataFrame, index: int, price: float) -> pd.DataFrame:
    """A copy of `bars` with one bar's open replaced. Closes are untouched."""
    changed = bars.copy()
    changed.loc[changed.index[index], "open"] = float(price)
    close = float(changed.loc[changed.index[index], "close"])
    changed.loc[changed.index[index], "high"] = max(price, close) + 0.5
    changed.loc[changed.index[index], "low"] = min(price, close) - 0.5
    return changed


#: Flat, then a rally: exactly one BUY, on bar 60.
RALLY = flat(60, 100.0) + flat(40, 120.0)

#: Flat, rally, selloff: one BUY and one later EXIT.
RALLY_THEN_SELLOFF = RALLY + flat(60, 80.0)

#: Flat, then a decline: the first (and only) signal is an EXIT, while flat.
SELLOFF_FIRST = flat(60, 100.0) + flat(40, 80.0)

#: Two full cycles, ending long.
MULTI_CYCLE = RALLY_THEN_SELLOFF + flat(50, 130.0) + flat(50, 60.0) + flat(50, 140.0)

#: A fully hand-computable case; see `test_hand_calculated_accounting`.
HAND_CALCULATED_CLOSES = flat(60, 100.0) + [130.0, 90.0, 110.0]
HAND_CALCULATED_OPENS = flat(60, 100.0) + [130.0, 100.0, 110.0]

ALL_FIXTURES = {
    "rally": (RALLY, None),
    "rally_then_selloff": (RALLY_THEN_SELLOFF, None),
    "selloff_first": (SELLOFF_FIRST, None),
    "multi_cycle": (MULTI_CYCLE, None),
    "hand_calculated": (HAND_CALCULATED_CLOSES, HAND_CALCULATED_OPENS),
}


# --------------------------------------------------------------------------
# Independent helpers - never call into the engine
# --------------------------------------------------------------------------


def signal_positions(bars: pd.DataFrame) -> list[tuple[int, Signal]]:
    """Phase 3 signals paired with the bar position each was observed on."""
    position_of = {timestamp: index for index, timestamp in enumerate(bars["timestamp"])}
    return [(position_of[signal.timestamp], signal) for signal in generate_ema_cross_signals(bars)]


def replay_ledger(result: BacktestResult) -> tuple[float, int]:
    """Re-derive final cash and holdings from the executions alone.

    Deliberately independent of the engine's own bookkeeping: it asserts the
    long-only single-position invariants as it goes.
    """
    cash = result.initial_cash
    quantity = 0
    for execution in result.executions:
        assert execution.quantity > 0
        if execution.side is ExecutionSide.BUY:
            assert quantity == 0, "a BUY must never add to an existing position"
            cash -= execution.quantity * execution.price
            quantity = execution.quantity
        else:
            assert execution.quantity == quantity, "a SELL must close the whole position"
            cash += execution.quantity * execution.price
            quantity = 0
        assert cash >= 0.0, "cash must never go negative"
        assert quantity >= 0, "holdings must never go short"
    return cash, quantity


def run_fixture(name: str, initial_cash: float = DEFAULT_INITIAL_CASH) -> BacktestResult:
    closes, opens = ALL_FIXTURES[name]
    return run_backtest(make_bars(closes, opens), initial_cash=initial_cash)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


def test_execution_side_is_market_side_not_signal_type() -> None:
    # An EXIT signal becomes a SELL execution; "EXIT" is not a market side.
    assert [member.name for member in ExecutionSide] == ["BUY", "SELL"]
    assert not hasattr(ExecutionSide, "EXIT")
    assert not hasattr(ExecutionSide, "SHORT")


def test_execution_is_frozen_and_records_both_timestamps() -> None:
    execution = Execution(
        signal_timestamp=pd.Timestamp(FIRST_BAR),
        execution_timestamp=pd.Timestamp(FIRST_BAR + STEP),
        symbol="SPY",
        side=ExecutionSide.BUY,
        quantity=10,
        price=100.0,
        cash_after=0.0,
    )
    assert {field.name for field in dataclasses.fields(Execution)} == {
        "signal_timestamp",
        "execution_timestamp",
        "symbol",
        "side",
        "quantity",
        "price",
        "cash_after",
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        execution.price = 1.0  # type: ignore[misc]


# --------------------------------------------------------------------------
# Input contract
# --------------------------------------------------------------------------


def test_valid_canonical_dataset_backtests() -> None:
    result = run_fixture("rally_then_selloff")

    assert isinstance(result, BacktestResult)
    assert result.symbol == "SPY"
    assert result.bar_count == len(RALLY_THEN_SELLOFF)
    assert result.initial_cash == DEFAULT_INITIAL_CASH
    assert result.buy_execution_count == 1
    assert result.sell_execution_count == 1
    assert len(result.equity_curve) == result.bar_count


def test_invalid_dataset_is_rejected_before_any_strategy_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_run(*args: object, **kwargs: object) -> list[Signal]:
        raise AssertionError("signals must not be generated for an invalid dataset")

    monkeypatch.setattr(engine, "generate_ema_cross_signals", must_not_run)

    bars = make_bars(RALLY)
    bars.loc[bars.index[3], "high"] = 1.0  # high < low: a Phase 2 OHLC violation

    with pytest.raises(BacktestInputError) as excinfo:
        run_backtest(bars)
    assert "validation" in str(excinfo.value)
    assert "high >= low" in str(excinfo.value)


@pytest.mark.parametrize(
    "frame_factory",
    [
        pytest.param(lambda: make_bars(RALLY).drop(columns=["vwap"]), id="missing-column"),
        pytest.param(lambda: make_bars([]), id="empty"),
        pytest.param(lambda: make_bars(RALLY).iloc[::-1].reset_index(drop=True), id="unsorted"),
        pytest.param(
            lambda: pd.concat([make_bars(RALLY), make_bars(RALLY)], ignore_index=True),
            id="duplicate-timestamps",
        ),
        pytest.param(lambda: make_bars(RALLY, symbol="ZZZZ"), id="unsupported-symbol"),
    ],
)
def test_phase_two_findings_abort_the_backtest(frame_factory) -> None:
    with pytest.raises(BacktestInputError):
        run_backtest(frame_factory())


def test_invalid_input_is_never_silently_repaired() -> None:
    unsorted_bars = make_bars(RALLY).iloc[::-1].reset_index(drop=True)
    before = unsorted_bars.copy(deep=True)

    with pytest.raises(BacktestInputError):
        run_backtest(unsorted_bars)

    assert unsorted_bars.equals(before)


@pytest.mark.parametrize(
    "initial_cash",
    [0, 0.0, -1, -0.01, -100_000.0, float("nan"), float("inf"), float("-inf")],
)
def test_non_positive_initial_cash_is_rejected(initial_cash: float) -> None:
    with pytest.raises(BacktestInputError) as excinfo:
        run_backtest(make_bars(RALLY), initial_cash=initial_cash)
    assert "initial_cash" in str(excinfo.value)


def test_initial_cash_defaults_to_one_hundred_thousand() -> None:
    assert DEFAULT_INITIAL_CASH == 100_000.0
    assert run_backtest(make_bars(RALLY)).initial_cash == 100_000.0


# --------------------------------------------------------------------------
# No look-ahead - the critical rule (docs/SPEC.md section 6F)
# --------------------------------------------------------------------------


def test_signal_executes_on_next_bar_open_not_signal_bar() -> None:
    """The Phase 4 regression test. It must fail if fills ever move to the signal bar.

    The signal bar's own open (77.0) and close (120.0) are deliberately far
    from the next bar's open (55.0). Only 55.0 is a legal fill price.
    """
    bars = make_bars(RALLY)
    [(signal_index, signal)] = signal_positions(bars)
    assert signal.type is SignalType.BUY

    bars = with_open(bars, signal_index, 77.0)
    bars = with_open(bars, signal_index + 1, 55.0)
    assert float(bars["close"].iloc[signal_index]) == 120.0
    # Changing opens must not have disturbed the crossover itself.
    assert [index for index, _ in signal_positions(bars)] == [signal_index]

    result = run_backtest(bars, initial_cash=100_000.0)
    [execution] = result.executions

    assert execution.price == 55.0
    assert execution.price != 77.0, "must not fill at the signal bar's open"
    assert execution.price != 120.0, "must not fill at the signal bar's close"
    assert execution.execution_timestamp == bars["timestamp"].iloc[signal_index + 1]
    assert execution.signal_timestamp == bars["timestamp"].iloc[signal_index]
    assert execution.execution_timestamp > execution.signal_timestamp

    # floor(100000 / 55) = 1818 shares, costing 99,990 and leaving 10.
    assert execution.quantity == 1818
    assert execution.cash_after == pytest.approx(10.0)


def test_exit_signal_executes_on_next_bar_open() -> None:
    bars = make_bars(RALLY_THEN_SELLOFF)
    positions = signal_positions(bars)
    exit_index, exit_signal = next(
        (index, signal) for index, signal in positions if signal.type is SignalType.EXIT
    )

    bars = with_open(bars, exit_index, 44.0)
    bars = with_open(bars, exit_index + 1, 66.0)
    assert [index for index, _ in signal_positions(bars)] == [index for index, _ in positions]

    result = run_backtest(bars)
    sells = [e for e in result.executions if e.side is ExecutionSide.SELL]
    [sell] = sells

    assert sell.price == 66.0
    assert sell.price != 44.0
    assert sell.execution_timestamp == bars["timestamp"].iloc[exit_index + 1]
    assert sell.signal_timestamp == exit_signal.timestamp
    assert sell.execution_timestamp > sell.signal_timestamp


@pytest.mark.parametrize("name", sorted(ALL_FIXTURES))
def test_no_execution_shares_its_signals_timestamp(name: str) -> None:
    result = run_fixture(name)
    closes, opens = ALL_FIXTURES[name]
    signal_timestamps = {
        signal.timestamp for _, signal in signal_positions(make_bars(closes, opens))
    }

    for execution in result.executions:
        assert execution.execution_timestamp > execution.signal_timestamp
        assert execution.execution_timestamp not in signal_timestamps


@pytest.mark.parametrize("name", sorted(ALL_FIXTURES))
def test_every_execution_is_exactly_one_bar_after_its_signal(name: str) -> None:
    closes, opens = ALL_FIXTURES[name]
    bars = make_bars(closes, opens)
    position_of = {timestamp: index for index, timestamp in enumerate(bars["timestamp"])}
    opens_by_position = bars["open"].tolist()

    for execution in run_backtest(bars).executions:
        signal_index = position_of[execution.signal_timestamp]
        execution_index = position_of[execution.execution_timestamp]
        assert execution_index == signal_index + 1
        assert execution.price == opens_by_position[execution_index]


def test_a_signal_on_the_final_bar_is_never_executed() -> None:
    # RALLY's only signal is on bar 60; truncating there leaves it with no
    # successor bar to fill against.
    truncated = make_bars(RALLY[:61])
    [(signal_index, _)] = signal_positions(truncated)
    assert signal_index == len(truncated) - 1

    result = run_backtest(truncated)

    assert result.signal_count == 1
    assert result.unexecuted_last_bar_signal_count == 1
    assert result.executions == ()
    assert result.ending_position_quantity == 0
    assert result.final_cash == DEFAULT_INITIAL_CASH
    assert result.final_equity == DEFAULT_INITIAL_CASH
    assert result.total_return == 0.0
    assert result.max_drawdown == 0.0


def test_a_signal_with_a_following_bar_is_not_counted_as_unexecuted() -> None:
    assert run_backtest(make_bars(RALLY)).unexecuted_last_bar_signal_count == 0


# --------------------------------------------------------------------------
# No-op signals
# --------------------------------------------------------------------------


def test_exit_while_flat_is_a_no_op() -> None:
    result = run_fixture("selloff_first")

    assert result.signal_count == 1
    assert result.executions == ()
    assert result.ending_position_quantity == 0
    assert result.completed_round_trips == 0
    assert result.final_cash == DEFAULT_INITIAL_CASH
    assert result.final_equity == DEFAULT_INITIAL_CASH


def test_buy_while_already_long_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    # Phase 3 alternates BUY/EXIT, so a repeated BUY is forced in here to
    # exercise the engine's own guard against pyramiding.
    bars = make_bars(RALLY)
    [(signal_index, first)] = signal_positions(bars)
    repeated = [
        first,
        dataclasses.replace(first, timestamp=bars["timestamp"].iloc[signal_index + 2]),
    ]
    monkeypatch.setattr(engine, "generate_ema_cross_signals", lambda _bars: repeated)

    result = run_backtest(bars)

    assert result.signal_count == 2
    assert result.buy_execution_count == 1
    assert result.sell_execution_count == 0
    cash, quantity = replay_ledger(result)
    assert result.final_cash == cash
    assert result.ending_position_quantity == quantity


def test_a_buy_that_cannot_afford_one_share_creates_no_execution() -> None:
    bars = make_bars(RALLY)
    [(signal_index, _)] = signal_positions(bars)
    bars = with_open(bars, signal_index + 1, 500.0)

    result = run_backtest(bars, initial_cash=499.99)

    assert result.signal_count == 1
    assert result.executions == ()
    assert result.ending_position_quantity == 0
    assert result.final_cash == 499.99


# --------------------------------------------------------------------------
# Sizing, cash, and long-only invariants
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("initial_cash", "price"),
    [(100_000.0, 55.0), (100_000.0, 333.33), (10_000.0, 99.99), (1_000.0, 7.0)],
)
def test_buy_quantity_is_floor_of_cash_over_price(initial_cash: float, price: float) -> None:
    bars = make_bars(RALLY)
    [(signal_index, _)] = signal_positions(bars)
    bars = with_open(bars, signal_index + 1, price)

    [execution] = run_backtest(bars, initial_cash=initial_cash).executions

    assert execution.quantity == math.floor(initial_cash / price)
    assert execution.quantity * price <= initial_cash
    assert (execution.quantity + 1) * price > initial_cash
    assert execution.cash_after == pytest.approx(initial_cash - execution.quantity * price)
    assert execution.cash_after >= 0.0


@pytest.mark.parametrize("name", sorted(ALL_FIXTURES))
def test_cash_and_holdings_replay_independently(name: str) -> None:
    result = run_fixture(name)
    cash, quantity = replay_ledger(result)

    assert result.final_cash == pytest.approx(cash)
    assert result.ending_position_quantity == quantity
    assert all(execution.cash_after >= 0.0 for execution in result.executions)


@pytest.mark.parametrize("name", sorted(ALL_FIXTURES))
def test_no_leverage_and_no_short_position_is_possible(name: str) -> None:
    result = run_fixture(name)

    assert result.ending_position_quantity >= 0
    assert result.final_cash >= 0.0
    assert all(execution.quantity > 0 for execution in result.executions)
    assert all(
        execution.side in (ExecutionSide.BUY, ExecutionSide.SELL) for execution in result.executions
    )
    # Borrowing would show up as equity above the cash a single fully invested
    # position can represent; every bar's equity stays fully funded.
    assert all(equity > 0.0 for equity in result.equity_curve)
    for execution in result.executions:
        if execution.side is ExecutionSide.BUY:
            assert execution.quantity * execution.price <= result.initial_cash


def test_sell_exits_the_entire_position() -> None:
    result = run_fixture("rally_then_selloff")
    buy = next(e for e in result.executions if e.side is ExecutionSide.BUY)
    sell = next(e for e in result.executions if e.side is ExecutionSide.SELL)

    assert sell.quantity == buy.quantity
    assert result.ending_position_quantity == 0
    assert result.final_cash == result.final_equity


# --------------------------------------------------------------------------
# Hand-calculated accounting
# --------------------------------------------------------------------------


def test_hand_calculated_accounting() -> None:
    """Every number below is worked out by hand from the fixture prices.

    60 flat bars at 100 produce a BUY on bar 60. It fills at bar 61's open of
    100.00: floor(100,000 / 100) = 1,000 shares, leaving exactly 0 cash. Bar
    61 closes at 90 (equity 90,000) and bar 62 - the last bar - closes at 110
    (equity 110,000), with the position still open.
    """
    result = run_fixture("hand_calculated")
    [execution] = result.executions

    assert result.signal_count == 1
    assert execution.side is ExecutionSide.BUY
    assert execution.price == 100.0
    assert execution.quantity == 1_000
    assert execution.cash_after == 0.0

    assert result.final_cash == 0.0
    assert result.ending_position_quantity == 1_000
    assert result.ending_position_market_value == 110_000.0
    assert result.final_equity == 110_000.0

    # (110,000 / 100,000) - 1
    assert result.total_return == pytest.approx(0.10)
    # Peak equity is 100,000 through bar 60; bar 61 marks at 90,000.
    assert result.max_drawdown == pytest.approx(-0.10)

    assert result.equity_curve[59] == 100_000.0
    assert result.equity_curve[60] == 100_000.0
    assert result.equity_curve[61] == 90_000.0
    assert result.equity_curve[62] == 110_000.0


def test_equity_marks_after_the_open_fill_not_before() -> None:
    # Bar 61 fills at its open (100) and then closes at 90. Marking before the
    # fill would leave equity at the flat 100,000 instead of 90,000.
    result = run_fixture("hand_calculated")

    assert result.equity_curve[61] == 90_000.0
    assert result.equity_curve[61] != 100_000.0


def test_equity_curve_matches_cash_plus_marked_position_at_every_bar() -> None:
    bars = make_bars(*ALL_FIXTURES["multi_cycle"])
    result = run_backtest(bars)
    closes = bars["close"].tolist()
    position_of = {timestamp: index for index, timestamp in enumerate(bars["timestamp"])}

    cash = result.initial_cash
    quantity = 0
    fills = {position_of[e.execution_timestamp]: e for e in result.executions}
    for index, close in enumerate(closes):
        execution = fills.get(index)
        if execution is not None:
            if execution.side is ExecutionSide.BUY:
                cash -= execution.quantity * execution.price
                quantity = execution.quantity
            else:
                cash += execution.quantity * execution.price
                quantity = 0
        assert result.equity_curve[index] == pytest.approx(cash + quantity * close)


def test_max_drawdown_is_the_worst_running_peak_to_trough_decline() -> None:
    result = run_fixture("multi_cycle")

    peak = result.equity_curve[0]
    worst = 0.0
    for equity in result.equity_curve:
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)

    assert result.max_drawdown == pytest.approx(worst)
    assert result.max_drawdown <= 0.0


def test_max_drawdown_is_zero_when_equity_never_declines() -> None:
    result = run_fixture("selloff_first")

    assert result.equity_curve == tuple([DEFAULT_INITIAL_CASH] * result.bar_count)
    assert result.max_drawdown == 0.0


@pytest.mark.parametrize("name", sorted(ALL_FIXTURES))
def test_total_return_is_final_equity_over_initial_cash(name: str) -> None:
    result = run_fixture(name)

    assert result.total_return == pytest.approx(result.final_equity / result.initial_cash - 1.0)
    assert result.final_equity == pytest.approx(
        result.final_cash + result.ending_position_market_value
    )
    assert result.equity_curve[-1] == pytest.approx(result.final_equity)


# --------------------------------------------------------------------------
# Ending position and round trips
# --------------------------------------------------------------------------


def test_an_open_position_is_not_liquidated_at_the_final_bar() -> None:
    result = run_fixture("hand_calculated")

    assert result.ending_position_quantity > 0
    assert result.sell_execution_count == 0
    assert all(execution.side is ExecutionSide.BUY for execution in result.executions)
    assert result.final_cash == 0.0


def test_a_final_open_position_is_marked_to_the_final_close() -> None:
    bars = make_bars(*ALL_FIXTURES["multi_cycle"])
    result = run_backtest(bars)
    final_close = float(bars["close"].iloc[-1])

    assert result.ending_position_quantity > 0
    assert result.ending_position_market_value == pytest.approx(
        result.ending_position_quantity * final_close
    )
    assert result.final_equity == pytest.approx(
        result.final_cash + result.ending_position_quantity * final_close
    )


def test_completed_round_trips_count_buy_then_sell_pairs() -> None:
    result = run_fixture("rally_then_selloff")

    assert result.buy_execution_count == 1
    assert result.sell_execution_count == 1
    assert result.completed_round_trips == 1


def test_an_ending_open_position_is_not_a_completed_round_trip() -> None:
    result = run_fixture("multi_cycle")

    assert result.ending_position_quantity > 0
    assert result.buy_execution_count == result.sell_execution_count + 1
    assert result.completed_round_trips == result.sell_execution_count


def test_hand_calculated_open_position_completes_no_round_trip() -> None:
    result = run_fixture("hand_calculated")

    assert result.buy_execution_count == 1
    assert result.completed_round_trips == 0


# --------------------------------------------------------------------------
# Determinism and purity
# --------------------------------------------------------------------------


def test_repeated_runs_return_an_identical_result() -> None:
    bars = make_bars(*ALL_FIXTURES["multi_cycle"])
    runs = [run_backtest(bars) for _ in range(3)]

    assert runs[0].executions
    assert runs[0] == runs[1] == runs[2]
    assert runs[0] == run_backtest(make_bars(*ALL_FIXTURES["multi_cycle"]))


def test_input_frame_is_not_mutated() -> None:
    bars = make_bars(*ALL_FIXTURES["multi_cycle"])
    before = bars.copy(deep=True)

    run_backtest(bars)

    assert list(bars.columns) == list(CANONICAL_COLUMNS)
    assert bars.equals(before)


def test_result_does_not_depend_on_the_frame_index() -> None:
    bars = make_bars(*ALL_FIXTURES["multi_cycle"])
    baseline = run_backtest(bars)

    shifted = bars.copy()
    shifted.index = range(1_000, 1_000 + len(bars))

    assert run_backtest(shifted) == baseline


# --------------------------------------------------------------------------
# Reuse of earlier phases
# --------------------------------------------------------------------------


def test_phase_three_signals_are_reused_not_reimplemented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert engine.generate_ema_cross_signals is ema_cross.generate_ema_cross_signals

    source = inspect.getsource(engine)
    assert "ewm" not in source, "the engine must not recompute an EMA"
    assert "adjust=False" not in source

    consulted: list[int] = []

    def spy(bars: pd.DataFrame) -> list[Signal]:
        consulted.append(len(bars))
        return generate_ema_cross_signals(bars)

    monkeypatch.setattr(engine, "generate_ema_cross_signals", spy)
    bars = make_bars(RALLY_THEN_SELLOFF)
    result = run_backtest(bars)

    assert consulted == [len(bars)]
    assert result.signal_count == len(generate_ema_cross_signals(bars))


def test_phase_two_validation_is_reused_not_reimplemented() -> None:
    from autotrader.data import validation

    assert engine.validate_frame is validation.validate_frame
    source = inspect.getsource(engine)
    for rule in ("high >= low", "is_monotonic_increasing", "DUPLICATE_TIMESTAMP"):
        assert rule not in source, "Phase 2 rules must not be duplicated in the engine"


def test_signal_count_matches_phase_three_exactly() -> None:
    for name in sorted(ALL_FIXTURES):
        closes, opens = ALL_FIXTURES[name]
        bars = make_bars(closes, opens)
        assert run_backtest(bars).signal_count == len(generate_ema_cross_signals(bars))


# --------------------------------------------------------------------------
# Broker safety and offline guarantees
# --------------------------------------------------------------------------


def test_backtest_imports_no_broker_client() -> None:
    assert "alpaca" not in inspect.getsource(engine).lower()
    imported = {
        getattr(value, "__name__", "") for value in vars(engine).values() if inspect.ismodule(value)
    }
    assert not any(name.startswith("alpaca") for name in imported)


def test_no_trading_or_order_api_exists_anywhere_in_the_package() -> None:
    forbidden = ("TradingClient", "submit_order", "OrderRequest", "MarketOrderRequest")
    source_root = Path(engine.__file__).resolve().parents[1]
    for path in sorted(source_root.rglob("*.py")):
        text = path.read_text()
        for token in forbidden:
            assert token not in text, f"{token} found in {path}"


def test_backtest_needs_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    assert run_fixture("rally_then_selloff").executions


def test_backtest_makes_no_network_access(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("the backtester must not open a socket")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

    assert run_fixture("rally_then_selloff").executions


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def write_parquet(frame: pd.DataFrame, path: Path) -> Path:
    frame.to_parquet(path, engine="pyarrow", index=False)
    return path


def test_cli_backtest_help_succeeds() -> None:
    result = CliRunner(env={"COLUMNS": "120"}).invoke(app, ["backtest", "--help"])

    assert result.exit_code == 0
    assert "autotrader backtest" in result.output
    assert "--initial-cash" in result.output


def test_cli_backtest_reports_a_valid_dataset(tmp_path) -> None:
    path = write_parquet(make_bars(*ALL_FIXTURES["hand_calculated"]), tmp_path / "SPY_15m.parquet")

    result = CliRunner().invoke(app, ["backtest", str(path)])

    assert result.exit_code == 0, result.output
    assert "AUTO TRADER BACKTEST" in result.output
    assert "Symbol:                SPY" in result.output
    assert f"Strategy:              {STRATEGY_NAME}" in result.output
    assert "Rows:                  63" in result.output
    assert "Initial Cash:          $100,000.00" in result.output
    assert "Final Equity:          $110,000.00" in result.output
    assert "Total Return:          10.00%" in result.output
    assert "Max Drawdown:          -10.00%" in result.output
    assert "Completed Round Trips: 0" in result.output
    assert "Ending Position:       1000 shares" in result.output
    assert "Next-bar open" in result.output
    assert "0 / 0" in result.output
    # A summary, not a trade blotter.
    assert "1000 @ " not in result.output


def test_cli_backtest_accepts_an_explicit_initial_cash(tmp_path) -> None:
    path = write_parquet(make_bars(*ALL_FIXTURES["hand_calculated"]), tmp_path / "SPY_15m.parquet")

    result = CliRunner().invoke(app, ["backtest", str(path), "--initial-cash", "50000"])

    assert result.exit_code == 0, result.output
    assert "Initial Cash:          $50,000.00" in result.output


def test_cli_backtest_rejects_an_invalid_dataset_cleanly(tmp_path) -> None:
    frame = make_bars(RALLY)
    frame.loc[frame.index[2], "high"] = 1.0
    path = write_parquet(frame, tmp_path / "SPY_15m.parquet")

    result = CliRunner().invoke(app, ["backtest", str(path)])

    assert result.exit_code == 1
    assert "validation" in result.output
    assert "AUTO TRADER BACKTEST" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_cli_backtest_rejects_non_positive_initial_cash_cleanly(tmp_path) -> None:
    path = write_parquet(make_bars(RALLY), tmp_path / "SPY_15m.parquet")

    result = CliRunner().invoke(app, ["backtest", str(path), "--initial-cash", "0"])

    assert result.exit_code == 1
    assert "initial_cash" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_cli_backtest_reports_a_missing_file_without_a_traceback(tmp_path) -> None:
    result = CliRunner().invoke(app, ["backtest", str(tmp_path / "absent.parquet")])

    assert result.exit_code == 2
    assert "No such file" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_cli_backtest_reports_an_unreadable_file_without_a_traceback(tmp_path) -> None:
    path = tmp_path / "not-parquet.parquet"
    path.write_text("this is not a parquet file")

    result = CliRunner().invoke(app, ["backtest", str(path)])

    assert result.exit_code == 2
    assert "Parquet" in result.output


def test_cli_backtest_notes_an_unexecuted_last_bar_signal(tmp_path) -> None:
    path = write_parquet(make_bars(RALLY[:61]), tmp_path / "SPY_15m.parquet")

    result = CliRunner().invoke(app, ["backtest", str(path)])

    assert result.exit_code == 0, result.output
    assert "Unexecuted Last Bar:   1" in result.output
    assert "BUY Executions:        0" in result.output


def test_cli_backtest_makes_no_network_access(tmp_path, monkeypatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("the backtest command must not use the network")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

    path = write_parquet(make_bars(RALLY_THEN_SELLOFF), tmp_path / "SPY_15m.parquet")
    assert CliRunner().invoke(app, ["backtest", str(path)]).exit_code == 0
