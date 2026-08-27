"""C4 tests: deterministic next-bar-open crypto backtesting.

Every test is offline, needs no credentials, and builds small synthetic
frames. Accounting expectations are derived independently - by hand, or by
replaying the reported executions through a separate ledger - rather than by
snapshotting whatever the engine happened to return.

The no-look-ahead tests are the most important ones here, and the pivot did not
touch them: their fixtures make a signal bar's own prices wildly different from
the next bar's open, so an implementation that filled on the signal bar would
produce obviously wrong numbers instead of quietly plausible ones.

What the pivot *did* change is sizing and cost. Quantities are fractional
`Decimal` values with no whole-unit floor, and a conservative taker fee is
charged on both sides. The tests below pin both, including the boundary that
matters most: a fee must never be able to drive cash negative.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import socket
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from autotrader.backtest import engine
from autotrader.backtest.engine import (
    DEFAULT_INITIAL_CASH,
    QUANTITY_EXPONENT,
    STRATEGY_NAME,
    TAKER_FEE_RATE,
    BacktestInputError,
    BacktestResult,
    Execution,
    ExecutionSide,
    affordable_quantity,
    run_backtest,
)
from autotrader.cli import app
from autotrader.data.historical import CANONICAL_COLUMNS
from autotrader.strategies import ema_cross
from autotrader.strategies.ema_cross import Signal, SignalType, generate_ema_cross_signals

FIRST_BAR = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
STEP = timedelta(minutes=15)

_FLOAT_COLUMNS = ("open", "high", "low", "close", "volume", "trade_count", "vwap")

ZERO = Decimal(0)
ONE = Decimal(1)
ULP = QUANTITY_EXPONENT


def dec(value: object) -> Decimal:
    """A test-side exact Decimal, via the shortest round-tripping form."""
    return Decimal(str(float(value)))


# --------------------------------------------------------------------------
# Synthetic bars
# --------------------------------------------------------------------------


def make_bars(
    closes: list[float],
    opens: list[float] | None = None,
    symbol: str = "BTC/USD",
    start: datetime = FIRST_BAR,
) -> pd.DataFrame:
    """A canonical bar frame with the given closes and (optionally) opens.

    `high` and `low` are widened to bound both, so any open/close pair the
    tests need stays valid under the C2 OHLC rules.
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
    """Strategy signals paired with the bar position each was observed on."""
    position_of = {timestamp: index for index, timestamp in enumerate(bars["timestamp"])}
    return [(position_of[signal.timestamp], signal) for signal in generate_ema_cross_signals(bars)]


def replay_ledger(result: BacktestResult) -> tuple[Decimal, Decimal, Decimal]:
    """Re-derive final cash, holdings, and total fees from the executions alone.

    Deliberately independent of the engine's own bookkeeping, and it asserts
    the long-only single-position invariants as it goes. Each fee is
    recomputed here from the execution's own reported quantity and price, so a
    wrong fee shows up as a mismatch rather than being carried along.
    """
    cash = result.initial_cash
    quantity = ZERO
    fees = ZERO
    for execution in result.executions:
        assert execution.quantity > 0
        expected_fee = execution.quantity * execution.price * TAKER_FEE_RATE
        assert execution.fee == expected_fee
        assert execution.fee > 0
        fees += execution.fee
        if execution.side is ExecutionSide.BUY:
            assert quantity == 0, "a BUY must never add to an existing position"
            cash -= execution.quantity * execution.price + execution.fee
            quantity = execution.quantity
        else:
            assert execution.quantity == quantity, "a SELL must close the whole position"
            cash += execution.quantity * execution.price - execution.fee
            quantity = ZERO
        assert cash >= 0, "cash must never go negative, fee included"
        assert execution.cash_after == cash
        assert quantity >= 0, "holdings must never go short"
    return cash, quantity, fees


def run_fixture(name: str, initial_cash: object = DEFAULT_INITIAL_CASH) -> BacktestResult:
    closes, opens = ALL_FIXTURES[name]
    return run_backtest(make_bars(closes, opens), initial_cash=initial_cash)


def code_without_prose(source: str) -> str:
    """`source` with every docstring and comment removed.

    The source-level guarantees below are about executable code. The engine's
    own documentation explains what it does *not* do - "the real broker's
    metadata", "Alpaca's real crypto fees" - so a naive substring scan would
    trip over the sentences that explain the rule.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


def test_execution_side_is_market_side_not_signal_type() -> None:
    # An EXIT signal becomes a SELL execution; "EXIT" is not a market side.
    assert [member.name for member in ExecutionSide] == ["BUY", "SELL"]
    assert not hasattr(ExecutionSide, "EXIT")
    assert not hasattr(ExecutionSide, "SHORT")


def test_execution_is_frozen_and_records_both_timestamps_and_its_fee() -> None:
    execution = Execution(
        signal_timestamp=pd.Timestamp(FIRST_BAR),
        execution_timestamp=pd.Timestamp(FIRST_BAR + STEP),
        symbol="BTC/USD",
        side=ExecutionSide.BUY,
        quantity=Decimal("0.5"),
        price=Decimal("100"),
        fee=Decimal("0.125"),
        cash_after=ZERO,
    )
    assert {field.name for field in dataclasses.fields(Execution)} == {
        "signal_timestamp",
        "execution_timestamp",
        "symbol",
        "side",
        "quantity",
        "price",
        "fee",
        "cash_after",
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        execution.price = Decimal(1)  # type: ignore[misc]


def test_money_and_quantities_are_decimals_not_floats() -> None:
    result = run_fixture("rally_then_selloff")

    for value in (
        result.initial_cash,
        result.final_cash,
        result.final_equity,
        result.total_fees,
        result.ending_position_quantity,
        result.ending_position_market_value,
    ):
        assert isinstance(value, Decimal), value
    assert all(isinstance(equity, Decimal) for equity in result.equity_curve)
    for execution in result.executions:
        assert isinstance(execution.quantity, Decimal)
        assert isinstance(execution.price, Decimal)
        assert isinstance(execution.fee, Decimal)
        assert isinstance(execution.cash_after, Decimal)
    # Ratios stay plain floats: they are presentation values, not balances.
    assert isinstance(result.total_return, float)
    assert isinstance(result.max_drawdown, float)


# --------------------------------------------------------------------------
# Input contract
# --------------------------------------------------------------------------


def test_valid_canonical_dataset_backtests() -> None:
    result = run_fixture("rally_then_selloff")

    assert isinstance(result, BacktestResult)
    assert result.symbol == "BTC/USD"
    assert result.bar_count == len(RALLY_THEN_SELLOFF)
    assert result.initial_cash == DEFAULT_INITIAL_CASH
    assert result.buy_execution_count == 1
    assert result.sell_execution_count == 1
    assert len(result.equity_curve) == result.bar_count


def test_both_crypto_pairs_backtest() -> None:
    for symbol in ("BTC/USD", "ETH/USD"):
        result = run_backtest(make_bars(RALLY_THEN_SELLOFF, symbol=symbol))
        assert result.symbol == symbol
        assert result.completed_round_trips == 1


def test_invalid_dataset_is_rejected_before_any_strategy_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_run(*args: object, **kwargs: object) -> list[Signal]:
        raise AssertionError("signals must not be generated for an invalid dataset")

    monkeypatch.setattr(engine, "generate_ema_cross_signals", must_not_run)

    bars = make_bars(RALLY)
    bars.loc[bars.index[3], "high"] = 1.0  # high < low: a C2 OHLC violation

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
        pytest.param(lambda: make_bars(RALLY, symbol="SPY"), id="archived-equity-symbol"),
        pytest.param(lambda: make_bars(RALLY, symbol="BTCUSD"), id="non-canonical-pair"),
    ],
)
def test_validation_findings_abort_the_backtest(frame_factory) -> None:
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
    [0, 0.0, -1, -0.01, -100_000.0, float("nan"), float("inf"), float("-inf"), Decimal("-1")],
)
def test_non_positive_initial_cash_is_rejected(initial_cash: object) -> None:
    with pytest.raises(BacktestInputError) as excinfo:
        run_backtest(make_bars(RALLY), initial_cash=initial_cash)
    assert "initial_cash" in str(excinfo.value)


def test_initial_cash_defaults_to_one_hundred_thousand() -> None:
    expected = Decimal("100000")
    assert expected == DEFAULT_INITIAL_CASH
    assert run_backtest(make_bars(RALLY)).initial_cash == expected


def test_a_float_initial_cash_becomes_the_number_it_reads_as() -> None:
    """`100.10` funds a simulation with exactly 100.10, not 100.0999999999999943."""
    assert run_backtest(make_bars(RALLY), initial_cash=100.10).initial_cash == Decimal("100.10")
    assert run_backtest(make_bars(RALLY), initial_cash="100.10").initial_cash == Decimal("100.10")


# --------------------------------------------------------------------------
# No look-ahead - the critical rule (docs/SPEC.md section 6F)
# --------------------------------------------------------------------------


def test_signal_executes_on_next_bar_open_not_signal_bar() -> None:
    """The critical regression. It must fail if fills ever move to the signal bar.

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

    result = run_backtest(bars, initial_cash=Decimal("100000"))
    [execution] = result.executions

    assert execution.price == Decimal("55")
    assert execution.price != Decimal("77"), "must not fill at the signal bar's open"
    assert execution.price != Decimal("120"), "must not fill at the signal bar's close"
    assert execution.execution_timestamp == bars["timestamp"].iloc[signal_index + 1]
    assert execution.signal_timestamp == bars["timestamp"].iloc[signal_index]
    assert execution.execution_timestamp > execution.signal_timestamp


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

    assert sell.price == Decimal("66")
    assert sell.price != Decimal("44")
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
        assert execution.price == dec(opens_by_position[execution_index])


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
    assert result.total_fees == 0
    assert result.total_return == 0.0
    assert result.max_drawdown == 0.0


def test_a_signal_with_a_following_bar_is_not_counted_as_unexecuted() -> None:
    assert run_backtest(make_bars(RALLY)).unexecuted_last_bar_signal_count == 0


def test_the_engine_models_no_market_session() -> None:
    """There is no open, no close, and no calendar in a 24/7 backtester."""
    source = code_without_prose(inspect.getsource(engine))
    for forbidden in (
        "is_open",
        "market_open",
        "market_close",
        "get_clock",
        "America/New_York",
        "ZoneInfo",
        "NYSE",
        "session",
        "holiday",
    ):
        assert forbidden not in source, forbidden


def test_bars_across_a_weekend_execute_normally() -> None:
    """Crypto keeps trading, so a Saturday bar is the next bar like any other."""
    saturday = datetime(2025, 1, 4, 0, 0, tzinfo=UTC)
    bars = make_bars(RALLY_THEN_SELLOFF, start=saturday)
    result = run_backtest(bars)
    assert result.completed_round_trips == 1


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
    assert result.total_fees == 0


def test_buy_while_already_long_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    # The strategy alternates BUY/EXIT, so a repeated BUY is forced in here to
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
    cash, quantity, fees = replay_ledger(result)
    assert result.final_cash == cash
    assert result.ending_position_quantity == quantity
    assert result.total_fees == fees


def test_a_buy_with_cash_below_one_quantum_of_notional_creates_no_execution() -> None:
    """The only case that now buys nothing: cash too small for 1e-18 of a unit.

    That is astronomically smaller than the old whole-unit floor, which is the
    point: there is no minimum coin count in this simulation.
    """
    bars = make_bars(RALLY)
    [(signal_index, _)] = signal_positions(bars)
    bars = with_open(bars, signal_index + 1, 500.0)

    result = run_backtest(bars, initial_cash=Decimal("1E-18"))

    assert result.signal_count == 1
    assert result.executions == ()
    assert result.ending_position_quantity == 0
    assert result.final_cash == Decimal("1E-18")


# --------------------------------------------------------------------------
# Fractional sizing - the whole-share floor is gone
# --------------------------------------------------------------------------


def test_one_hundred_dollars_buys_a_fraction_of_an_expensive_coin() -> None:
    """$100 against a $100,000 coin. The old engine bought nothing at all."""
    bars = make_bars(RALLY)
    [(signal_index, _)] = signal_positions(bars)
    bars = with_open(bars, signal_index + 1, 100_000.0)

    [execution] = run_backtest(bars, initial_cash=Decimal("100")).executions

    assert 0 < execution.quantity < 1
    assert execution.quantity * execution.price < Decimal("100")
    assert execution.cash_after >= 0


@pytest.mark.parametrize(
    ("initial_cash", "price"),
    [
        (Decimal("100000"), Decimal("55")),
        (Decimal("100000"), Decimal("333.33")),
        (Decimal("10000"), Decimal("99.99")),
        (Decimal("1000"), Decimal("7")),
        (Decimal("100"), Decimal("104000.12")),
        (Decimal("2500.75"), Decimal("2612.4")),
    ],
)
def test_buy_quantity_is_the_largest_that_cash_plus_fee_can_fund(
    initial_cash: Decimal, price: Decimal
) -> None:
    """Characterized independently: maximal, affordable, and not one ulp more.

    No formula from the engine is repeated here. The assertion is the property
    the sizing rule is supposed to have.
    """
    bars = make_bars(RALLY)
    [(signal_index, _)] = signal_positions(bars)
    bars = with_open(bars, signal_index + 1, float(price))

    [execution] = run_backtest(bars, initial_cash=initial_cash).executions
    quantity = execution.quantity

    total = quantity * price * (ONE + TAKER_FEE_RATE)
    assert total <= initial_cash, "the fill plus its fee must fit in the cash on hand"
    one_more = (quantity + ULP) * price * (ONE + TAKER_FEE_RATE)
    assert one_more > initial_cash, "sizing must not leave a whole quantum on the table"
    assert quantity == quantity.quantize(QUANTITY_EXPONENT, rounding=ROUND_DOWN)
    assert execution.cash_after == initial_cash - quantity * price - execution.fee
    assert execution.cash_after >= 0


def test_no_whole_unit_floor_survives_anywhere_in_the_engine() -> None:
    source = code_without_prose(inspect.getsource(engine))
    for forbidden in ("math.floor", "//", "int(cash", "whole_share", "whole share"):
        assert forbidden not in source, forbidden


def test_sizing_reserves_the_fee_rather_than_spending_all_cash_on_notional() -> None:
    """The failure this guards against: 100% of cash on notional, then a fee."""
    cash = Decimal("1000")
    price = Decimal("123.45")
    quantity = affordable_quantity(cash, price)

    naive = (cash / price).quantize(QUANTITY_EXPONENT, rounding=ROUND_DOWN)
    assert quantity < naive, "reserving the fee must buy strictly less than all-in sizing"
    assert naive * price * (ONE + TAKER_FEE_RATE) > cash, "the naive size could not pay its fee"
    assert quantity * price * (ONE + TAKER_FEE_RATE) <= cash


# --------------------------------------------------------------------------
# Fees
# --------------------------------------------------------------------------


def test_the_documented_fee_rate_is_twenty_five_basis_points_per_side() -> None:
    documented = Decimal("0.0025")
    assert documented == TAKER_FEE_RATE
    assert isinstance(TAKER_FEE_RATE, Decimal)


def test_a_fee_is_charged_on_the_buy() -> None:
    result = run_fixture("rally_then_selloff")
    buy = next(e for e in result.executions if e.side is ExecutionSide.BUY)

    assert buy.fee == buy.quantity * buy.price * Decimal("0.0025")
    assert buy.fee > 0
    assert buy.cash_after == result.initial_cash - buy.quantity * buy.price - buy.fee


def test_a_fee_is_charged_on_the_sell() -> None:
    result = run_fixture("rally_then_selloff")
    buy = next(e for e in result.executions if e.side is ExecutionSide.BUY)
    sell = next(e for e in result.executions if e.side is ExecutionSide.SELL)

    assert sell.fee == sell.quantity * sell.price * Decimal("0.0025")
    assert sell.fee > 0
    assert sell.cash_after == buy.cash_after + sell.quantity * sell.price - sell.fee


def test_total_fees_is_the_sum_of_both_sides() -> None:
    result = run_fixture("multi_cycle")
    assert result.executions
    assert result.total_fees == sum((execution.fee for execution in result.executions), start=ZERO)
    assert result.total_fees > 0


def test_a_fee_free_run_would_end_richer() -> None:
    """The fee is real money, not a decoration: it must move the result."""
    result = run_fixture("rally_then_selloff")
    buy = next(e for e in result.executions if e.side is ExecutionSide.BUY)
    sell = next(e for e in result.executions if e.side is ExecutionSide.SELL)

    without_fees = result.initial_cash - buy.quantity * buy.price + sell.quantity * sell.price
    assert result.final_equity < without_fees
    assert without_fees - result.final_equity == result.total_fees


@pytest.mark.parametrize(
    "initial_cash",
    [Decimal("1"), Decimal("10.01"), Decimal("999.99"), Decimal("100000"), Decimal("0.01")],
)
def test_the_fee_can_never_drive_cash_negative(initial_cash: Decimal) -> None:
    """The boundary the sizing rule exists to protect.

    A BUY that spent every dollar on notional would go negative the moment its
    fee was charged. Checked across every fixture and a spread of balances.
    """
    for name in sorted(ALL_FIXTURES):
        result = run_fixture(name, initial_cash=initial_cash)
        assert result.final_cash >= 0, (name, result.final_cash)
        for execution in result.executions:
            assert execution.cash_after >= 0, (name, execution)


def test_an_exactly_affordable_buy_still_leaves_room_for_its_fee() -> None:
    """Price chosen so all-in sizing would land exactly on the cash balance."""
    bars = make_bars(RALLY)
    [(signal_index, _)] = signal_positions(bars)
    bars = with_open(bars, signal_index + 1, 100.0)

    [execution] = run_backtest(bars, initial_cash=Decimal("1000")).executions

    assert execution.quantity < Decimal("10"), "10 units at 100 would leave nothing for the fee"
    assert execution.cash_after >= 0


@pytest.mark.parametrize("name", sorted(ALL_FIXTURES))
def test_cash_and_holdings_replay_independently(name: str) -> None:
    result = run_fixture(name)
    cash, quantity, fees = replay_ledger(result)

    assert result.final_cash == cash
    assert result.ending_position_quantity == quantity
    assert result.total_fees == fees


@pytest.mark.parametrize("name", sorted(ALL_FIXTURES))
def test_no_leverage_and_no_short_position_is_possible(name: str) -> None:
    result = run_fixture(name)

    assert result.ending_position_quantity >= 0
    assert result.final_cash >= 0
    assert all(execution.quantity > 0 for execution in result.executions)
    assert all(
        execution.side in (ExecutionSide.BUY, ExecutionSide.SELL) for execution in result.executions
    )
    # Borrowing would show up as equity above the cash a single fully invested
    # position can represent; every bar's equity stays fully funded.
    assert all(equity > 0 for equity in result.equity_curve)
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
    """Worked out from the fixture prices, with the fee written out longhand.

    60 flat bars at 100 produce a BUY on bar 60. It fills at bar 61's open of
    100.00 with a 0.25% taker fee, so the affordable size solves
    ``q * 100 * 1.0025 <= 100,000``. Bar 61 closes at 90 and bar 62 - the last
    bar - closes at 110, with the position still open.
    """
    result = run_fixture("hand_calculated")
    [execution] = result.executions

    assert result.signal_count == 1
    assert execution.side is ExecutionSide.BUY
    assert execution.price == Decimal("100")

    # 100,000 / 100.25 = 997.5062344139650872817955112..., truncated at 1e-18.
    assert execution.quantity == Decimal("997.506234413965087281")
    assert execution.fee == execution.quantity * Decimal("100") * Decimal("0.0025")
    assert execution.cash_after == (
        Decimal("100000") - execution.quantity * Decimal("100") - execution.fee
    )
    assert execution.cash_after >= 0
    assert execution.cash_after < Decimal("1E-15")

    quantity = execution.quantity
    assert result.final_cash == execution.cash_after
    assert result.ending_position_quantity == quantity
    assert result.ending_position_market_value == quantity * Decimal("110")
    assert result.final_equity == execution.cash_after + quantity * Decimal("110")
    assert result.total_fees == execution.fee

    assert result.equity_curve[59] == Decimal("100000")
    assert result.equity_curve[60] == Decimal("100000")
    assert result.equity_curve[61] == execution.cash_after + quantity * Decimal("90")
    assert result.equity_curve[62] == result.final_equity

    # The fee costs ~0.25% of the position, so the return is a little under 10%.
    assert 0.09 < result.total_return < 0.10


def test_equity_marks_after_the_open_fill_not_before() -> None:
    # Bar 61 fills at its open (100) and then closes at 90. Marking before the
    # fill would leave equity at the flat 100,000 instead of ~89,775.
    result = run_fixture("hand_calculated")

    assert result.equity_curve[61] < Decimal("90000")
    assert result.equity_curve[61] != Decimal("100000")


def test_equity_curve_matches_cash_plus_marked_position_at_every_bar() -> None:
    bars = make_bars(*ALL_FIXTURES["multi_cycle"])
    result = run_backtest(bars)
    closes = bars["close"].tolist()
    position_of = {timestamp: index for index, timestamp in enumerate(bars["timestamp"])}

    cash = result.initial_cash
    quantity = ZERO
    fills = {position_of[e.execution_timestamp]: e for e in result.executions}
    for index, close in enumerate(closes):
        execution = fills.get(index)
        if execution is not None:
            if execution.side is ExecutionSide.BUY:
                cash -= execution.quantity * execution.price + execution.fee
                quantity = execution.quantity
            else:
                cash += execution.quantity * execution.price - execution.fee
                quantity = ZERO
        assert result.equity_curve[index] == cash + quantity * dec(close)


def test_max_drawdown_is_the_worst_running_peak_to_trough_decline() -> None:
    result = run_fixture("multi_cycle")

    peak = result.equity_curve[0]
    worst = ZERO
    for equity in result.equity_curve:
        peak = max(peak, equity)
        worst = min(worst, equity / peak - ONE)

    assert result.max_drawdown == pytest.approx(float(worst))
    assert result.max_drawdown <= 0.0


def test_max_drawdown_is_zero_when_equity_never_declines() -> None:
    result = run_fixture("selloff_first")

    assert result.equity_curve == tuple([DEFAULT_INITIAL_CASH] * result.bar_count)
    assert result.max_drawdown == 0.0


@pytest.mark.parametrize("name", sorted(ALL_FIXTURES))
def test_total_return_is_final_equity_over_initial_cash(name: str) -> None:
    result = run_fixture(name)

    assert result.total_return == pytest.approx(
        float(result.final_equity / result.initial_cash - ONE)
    )
    assert result.final_equity == result.final_cash + result.ending_position_market_value
    assert result.equity_curve[-1] == result.final_equity


# --------------------------------------------------------------------------
# Ending position and round trips
# --------------------------------------------------------------------------


def test_an_open_position_is_not_liquidated_at_the_final_bar() -> None:
    result = run_fixture("hand_calculated")

    assert result.ending_position_quantity > 0
    assert result.sell_execution_count == 0
    assert all(execution.side is ExecutionSide.BUY for execution in result.executions)
    # No closing trade is fabricated, so no closing fee is charged either.
    assert result.total_fees == result.executions[0].fee


def test_a_final_open_position_is_marked_to_the_final_close() -> None:
    bars = make_bars(*ALL_FIXTURES["multi_cycle"])
    result = run_backtest(bars)
    final_close = dec(bars["close"].iloc[-1])

    assert result.ending_position_quantity > 0
    assert result.ending_position_market_value == result.ending_position_quantity * final_close
    assert result.final_equity == (
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


def test_decimal_results_are_bit_for_bit_repeatable() -> None:
    """Not merely approximately equal: the same exact Decimal, scale included."""
    bars = make_bars(*ALL_FIXTURES["multi_cycle"])
    first, second = run_backtest(bars), run_backtest(bars)

    assert str(first.final_cash) == str(second.final_cash)
    assert str(first.total_fees) == str(second.total_fees)
    assert [str(value) for value in first.equity_curve] == [
        str(value) for value in second.equity_curve
    ]


def test_the_decimal_context_does_not_leak_out_of_the_engine() -> None:
    """The engine raises its working precision locally, never globally."""
    import decimal

    before = decimal.getcontext().prec
    run_fixture("multi_cycle")
    assert decimal.getcontext().prec == before


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
# Reuse of the other layers
# --------------------------------------------------------------------------


def test_strategy_signals_are_reused_not_reimplemented(
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


def test_validation_is_reused_not_reimplemented() -> None:
    from autotrader.data import validation

    assert engine.validate_frame is validation.validate_frame
    source = inspect.getsource(engine)
    for rule in ("high >= low", "is_monotonic_increasing", "DUPLICATE_TIMESTAMP"):
        assert rule not in source, "C2 rules must not be duplicated in the engine"


def test_signal_count_matches_the_strategy_exactly() -> None:
    for name in sorted(ALL_FIXTURES):
        closes, opens = ALL_FIXTURES[name]
        bars = make_bars(closes, opens)
        assert run_backtest(bars).signal_count == len(generate_ema_cross_signals(bars))


def test_the_engine_hardcodes_no_broker_increment() -> None:
    """Provider minimums change; the historical simulation must not pin one."""
    source = code_without_prose(inspect.getsource(engine))
    for forbidden in ("min_order_size", "min_trade_increment", "0.000000001", "0.0001"):
        assert forbidden not in source, forbidden


# --------------------------------------------------------------------------
# Broker safety and offline guarantees
# --------------------------------------------------------------------------


def test_backtest_imports_no_broker_client() -> None:
    assert "alpaca" not in code_without_prose(inspect.getsource(engine)).lower()
    imported = {
        getattr(value, "__name__", "") for value in vars(engine).values() if inspect.ismodule(value)
    }
    assert not any(name.startswith("alpaca") for name in imported)


def test_the_order_api_exists_only_inside_the_paper_execution_boundary() -> None:
    """The broker vocabulary is pinned to the one place it may live.

    If it ever leaks into a strategy, the backtester, the risk engine, or the
    state layer, that is a violation of docs/SPEC.md section 6A and this fails.
    """
    forbidden = ("TradingClient", "submit_order", "OrderRequest", "MarketOrderRequest")
    source_root = Path(engine.__file__).resolve().parents[1]
    allowed = {source_root / "execution", source_root / "cli"}

    for path in sorted(source_root.rglob("*.py")):
        if any(parent in allowed for parent in path.parents):
            continue
        text = path.read_text()
        for token in forbidden:
            assert token not in text, f"{token} found outside the execution boundary: {path}"


def test_only_the_execution_package_imports_a_broker_trading_client() -> None:
    """`TradingClient` is constructed in exactly one module."""
    source_root = Path(engine.__file__).resolve().parents[1]
    constructing = [
        path for path in sorted(source_root.rglob("*.py")) if "TradingClient(" in path.read_text()
    ]
    assert constructing == [source_root / "execution" / "paper.py"], constructing


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
    bars = make_bars(*ALL_FIXTURES["hand_calculated"])
    path = write_parquet(bars, tmp_path / "BTC_USD_15m.parquet")
    expected = run_backtest(bars)

    result = CliRunner().invoke(app, ["backtest", str(path)])

    assert result.exit_code == 0, result.output
    assert "AUTO TRADER BACKTEST" in result.output
    assert "Symbol:                BTC/USD" in result.output
    assert f"Strategy:              {STRATEGY_NAME}" in result.output
    assert "Rows:                  63" in result.output
    assert "Initial Cash:          $100,000.00" in result.output
    assert f"Final Equity:          ${expected.final_equity:,.2f}" in result.output
    assert f"Total Fees:            ${expected.total_fees:,.2f}" in result.output
    assert "Completed Round Trips: 0" in result.output
    assert "Ending Position:       997.506234413965087281 units" in result.output
    assert "Next-bar open, fractional quantity" in result.output
    assert "0.25% per side / 0" in result.output
    assert "shares" not in result.output
    # A summary, not a trade blotter.
    assert " @ " not in result.output


def test_cli_backtest_accepts_an_explicit_initial_cash(tmp_path) -> None:
    path = write_parquet(
        make_bars(*ALL_FIXTURES["hand_calculated"]), tmp_path / "BTC_USD_15m.parquet"
    )

    result = CliRunner().invoke(app, ["backtest", str(path), "--initial-cash", "50000"])

    assert result.exit_code == 0, result.output
    assert "Initial Cash:          $50,000.00" in result.output


def test_cli_backtest_rejects_an_invalid_dataset_cleanly(tmp_path) -> None:
    frame = make_bars(RALLY)
    frame.loc[frame.index[2], "high"] = 1.0
    path = write_parquet(frame, tmp_path / "BTC_USD_15m.parquet")

    result = CliRunner().invoke(app, ["backtest", str(path)])

    assert result.exit_code == 1
    assert "validation" in result.output
    assert "AUTO TRADER BACKTEST" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_cli_backtest_rejects_non_positive_initial_cash_cleanly(tmp_path) -> None:
    path = write_parquet(make_bars(RALLY), tmp_path / "BTC_USD_15m.parquet")

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
    path = write_parquet(make_bars(RALLY[:61]), tmp_path / "BTC_USD_15m.parquet")

    result = CliRunner().invoke(app, ["backtest", str(path)])

    assert result.exit_code == 0, result.output
    assert "Unexecuted Last Bar:   1" in result.output
    assert "BUY Executions:        0" in result.output


def test_cli_backtest_makes_no_network_access(tmp_path, monkeypatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("the backtest command must not use the network")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

    path = write_parquet(make_bars(RALLY_THEN_SELLOFF), tmp_path / "BTC_USD_15m.parquet")
    assert CliRunner().invoke(app, ["backtest", str(path)]).exit_code == 0
