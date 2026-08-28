"""The research replay simulator: determinism, costs, accounting, no look-ahead.

The most important test in this file is
`test_zero_slippage_replay_reproduces_the_production_backtester_exactly`. The
research simulator and the production backtester are separate code paths that
must agree on the arithmetic that matters, and the only way that stays true is
if a test fails the moment they diverge by one ulp.
"""

from __future__ import annotations

import socket
from decimal import Decimal

import pandas as pd
import pytest

from autotrader.backtest import TAKER_FEE_RATE, run_backtest
from autotrader.data.validation import CRYPTO_UNIVERSE_LABEL, EQUITY_UNIVERSE_LABEL
from autotrader.equity import EQUITY_SYMBOLS
from autotrader.research.costs import CostModel, Side
from autotrader.research.engines import (
    Action,
    BuyAndHoldEngine,
    EmaCrossEngine,
    ResearchSignal,
    ScriptedEngine,
)
from autotrader.research.replay import (
    ReplayConfig,
    ReplayInputError,
    affordable_quantity,
    allocate_sleeves,
    replay,
    replay_portfolio,
)
from autotrader.research.trades import FillSide, TradeAccountingError, build_trades
from research_fixtures import (
    bars_from_closes,
    equity_bars,
    flat,
    multi_cycle,
    rally,
    rally_then_selloff,
    wave,
)

CASH = Decimal("100000")

#: The production fee with slippage switched off. This is the exact cost model
#: under which the two engines must agree.
PRODUCTION_EQUIVALENT = CostModel(
    label="production-equivalent",
    fee_rate=TAKER_FEE_RATE,
    slippage_rate=Decimal("0"),
)


def config(**overrides: object) -> ReplayConfig:
    settings: dict[str, object] = {"initial_cash": CASH, "cost_model": PRODUCTION_EQUIVALENT}
    settings.update(overrides)
    return ReplayConfig(**settings)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Equivalence with the production backtester
# --------------------------------------------------------------------------


@pytest.mark.parametrize("phase", [0.0, 1.3, 2.7, 4.1])
def test_zero_slippage_replay_reproduces_the_production_backtester_exactly(
    phase: float,
) -> None:
    """CRITICAL. Two code paths, one arithmetic. Any divergence fails here.

    The research simulator adds an engine seam, slippage and richer accounting.
    None of that may change what the simulation actually computes: with
    slippage at zero and the production fee, every fill, every fee, every point
    of the equity curve and the final cash must match to the last decimal
    place.
    """
    bars = wave(phase=phase)
    production = run_backtest(bars, initial_cash=CASH)
    research = replay(bars, EmaCrossEngine(), config())

    assert research.final_cash == production.final_cash
    assert research.final_equity == production.final_equity
    assert research.total_fees == production.total_fees
    assert research.equity_curve == production.equity_curve
    assert research.signal_count == production.signal_count
    assert len(research.trades) == production.completed_round_trips
    assert research.unexecuted_final_signal_count == production.unexecuted_last_bar_signal_count

    assert len(research.fills) == len(production.executions)
    for fill, execution in zip(research.fills, production.executions, strict=True):
        assert fill.timestamp == execution.execution_timestamp
        assert fill.signal_timestamp == execution.signal_timestamp
        assert fill.quantity == execution.quantity
        assert fill.fill_price == execution.price
        assert fill.fee == execution.fee
        assert fill.cash_after == execution.cash_after
        assert fill.side.value == execution.side.value


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_the_same_inputs_always_produce_the_same_result() -> None:
    bars = wave()
    first = replay(bars, EmaCrossEngine(), config())
    second = replay(bars, EmaCrossEngine(), config())

    assert first.equity_curve == second.equity_curve
    assert first.final_equity == second.final_equity
    assert [fill.fill_price for fill in first.fills] == [fill.fill_price for fill in second.fills]


def test_the_supplied_frame_is_never_modified() -> None:
    bars = wave()
    before = bars.copy(deep=True)
    replay(bars, EmaCrossEngine(), config())
    pd.testing.assert_frame_equal(bars, before)


def test_money_is_exact_decimal_not_float() -> None:
    result = replay(wave(), EmaCrossEngine(), config())
    assert isinstance(result.final_cash, Decimal)
    assert isinstance(result.final_equity, Decimal)
    assert isinstance(result.total_fees, Decimal)
    for fill in result.fills:
        assert isinstance(fill.quantity, Decimal)
        assert isinstance(fill.fill_price, Decimal)


def test_a_float_starting_balance_is_refused() -> None:
    """Money is exact here; accepting a float would make results depend on
    binary rounding a caller never asked for."""
    with pytest.raises(ReplayInputError, match="must be a Decimal"):
        ReplayConfig(initial_cash=100000.0)  # type: ignore[arg-type]


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-1")])
def test_a_non_positive_starting_balance_is_refused(amount: Decimal) -> None:
    with pytest.raises(ReplayInputError, match="positive and finite"):
        ReplayConfig(initial_cash=amount)


# --------------------------------------------------------------------------
# No look-ahead
# --------------------------------------------------------------------------


def test_a_signal_never_fills_on_its_own_bar() -> None:
    """docs/SPEC.md section 6F. The whole rule, asserted directly."""
    result = replay(wave(), EmaCrossEngine(), config())
    assert result.fills, "the fixture must produce fills for this to mean anything"
    for fill in result.fills:
        assert fill.timestamp > fill.signal_timestamp


def test_a_fill_uses_the_next_bars_open_and_no_later_bar() -> None:
    bars = wave()
    result = replay(bars, EmaCrossEngine(), PRODUCTION_EQUIVALENT and config())
    index_of = {timestamp: index for index, timestamp in enumerate(bars["timestamp"])}
    opens = bars["open"].tolist()

    for fill in result.fills:
        signal_index = index_of[fill.signal_timestamp]
        assert fill.bar_index == signal_index + 1, "a fill is exactly one bar later"
        assert fill.reference_price == Decimal(str(float(opens[fill.bar_index])))


def test_a_signal_on_the_final_bar_is_left_unexecuted() -> None:
    """No bar follows the last one, so its signal cannot be filled at a real
    price. Inventing one is exactly the look-ahead the rule forbids."""
    bars = flat(120)
    timestamps = list(bars["timestamp"])
    engine = ScriptedEngine(
        signals=(
            ResearchSignal(
                timestamp=timestamps[-1],
                symbol="BTC/USD",
                action=Action.ENTER_LONG,
                reason="TEST",
            ),
        )
    )
    result = replay(bars, engine, config())

    assert result.fills == ()
    assert result.unexecuted_final_signal_count == 1
    assert result.final_cash == CASH


def test_an_engine_signalling_off_the_dataset_is_refused() -> None:
    bars = flat(50)
    stray = ResearchSignal(
        timestamp=pd.Timestamp("2030-01-01", tz="UTC"),
        symbol="BTC/USD",
        action=Action.ENTER_LONG,
        reason="TEST",
    )
    engine = ScriptedEngine(signals=(stray,))
    # ScriptedEngine filters to known bars, so a raw engine is needed to prove
    # the simulator itself refuses rather than trusting its input.

    class Stray:
        name = "stray"
        version = "v1"
        parameters: dict[str, object] = {}
        warmup_bars = 0

        def generate(self, frame: pd.DataFrame) -> tuple[ResearchSignal, ...]:
            return (stray,)

    assert engine.generate(bars) == ()
    with pytest.raises(ReplayInputError, match="not a bar in the dataset"):
        replay(bars, Stray(), config())


# --------------------------------------------------------------------------
# Cash and position invariants
# --------------------------------------------------------------------------


def test_cash_is_never_negative_and_the_position_is_never_short() -> None:
    for phase in (0.0, 1.1, 2.2, 3.3):
        result = replay(wave(phase=phase), EmaCrossEngine(), config())
        for fill in result.fills:
            assert fill.cash_after >= 0, "cash must never go negative, fee included"
        for trade in result.trades:
            assert trade.quantity > 0, "holdings must never go short"


def test_sizing_reserves_the_fee_rather_than_spending_all_cash() -> None:
    cash = Decimal("1000")
    price = Decimal("123.45")
    model = CostModel(label="t", fee_rate=Decimal("0.0025"), slippage_rate=Decimal("0"))
    quantity = affordable_quantity(cash, price, model)

    assert model.buy_cost(quantity, price) <= cash
    naive = cash / price
    assert quantity < naive, "reserving the fee must buy strictly less than all-in sizing"


def test_an_entry_that_cannot_afford_any_quantity_is_a_no_op() -> None:
    """A tiny balance against a huge price is skipped, not filled at zero."""
    bars = bars_from_closes([1_000_000.0] * 10 + [2_000_000.0] * 10)
    timestamps = list(bars["timestamp"])
    engine = ScriptedEngine(
        signals=(
            ResearchSignal(
                timestamp=timestamps[2],
                symbol="BTC/USD",
                action=Action.ENTER_LONG,
                reason="TEST",
            ),
        )
    )
    result = replay(bars, engine, config(initial_cash=Decimal("0.0000000000000000001")))
    assert result.fills == ()
    assert result.skipped_signal_count == 1


def test_an_entry_while_already_long_and_an_exit_while_flat_are_no_ops() -> None:
    bars = flat(60)
    timestamps = list(bars["timestamp"])
    engine = ScriptedEngine(
        signals=(
            # An exit before anything was bought, then two consecutive entries.
            ResearchSignal(timestamps[5], "BTC/USD", Action.EXIT_LONG, "EARLY_EXIT"),
            ResearchSignal(timestamps[10], "BTC/USD", Action.ENTER_LONG, "ENTER"),
            ResearchSignal(timestamps[20], "BTC/USD", Action.ENTER_LONG, "PYRAMID"),
        )
    )
    result = replay(bars, engine, config())

    assert len(result.fills) == 1, "only the first entry may fill"
    assert result.fills[0].side is FillSide.BUY
    assert result.skipped_signal_count == 2


def test_an_open_position_at_the_end_is_unrealized_and_not_a_trade() -> None:
    """Folding it into the trade list would invent a round trip that never
    happened and make the result depend on where the dataset ends."""
    result = replay(rally(), EmaCrossEngine(), config())

    assert result.open_position is not None
    assert result.trades == (), "a rally produces an entry and no exit"
    assert result.realized_pnl == 0
    assert result.unrealized_pnl != 0
    assert result.open_position.mark_price == Decimal(str(float(rally()["close"].iloc[-1])))
    assert result.open_position.quantity == result.fills[0].quantity


def test_a_completed_round_trip_is_a_trade() -> None:
    result = replay(rally_then_selloff(), EmaCrossEngine(), config())
    assert len(result.trades) >= 1
    trade = result.trades[0]
    assert trade.exit_timestamp > trade.entry_timestamp
    assert trade.bars_held > 0
    assert trade.net_pnl == trade.gross_pnl - trade.fees


def test_a_flat_series_trades_nothing_and_keeps_every_dollar() -> None:
    result = replay(flat(), EmaCrossEngine(), config())
    assert result.fills == ()
    assert result.trades == ()
    assert result.final_cash == CASH
    assert result.final_equity == CASH
    assert result.total_fees == 0
    assert result.exposure_bars == 0


# --------------------------------------------------------------------------
# Costs
# --------------------------------------------------------------------------


def test_slippage_makes_a_buy_dearer_and_a_sell_cheaper() -> None:
    model = CostModel(label="s", fee_rate=Decimal("0"), slippage_rate=Decimal("0.01"))
    reference = Decimal("100")
    assert model.fill_price(reference, Side.BUY) == Decimal("101")
    assert model.fill_price(reference, Side.SELL) == Decimal("99")


def test_adding_slippage_can_only_reduce_the_result() -> None:
    """Adverse by construction: there is no configuration in which crossing the
    spread pays you."""
    bars = rally_then_selloff()
    frictionless = replay(
        bars,
        EmaCrossEngine(),
        config(cost_model=CostModel("z", Decimal("0"), Decimal("0"))),
    )
    slipped = replay(
        bars,
        EmaCrossEngine(),
        config(cost_model=CostModel("s", Decimal("0"), Decimal("0.002"))),
    )
    assert slipped.final_equity < frictionless.final_equity
    assert slipped.total_slippage_cost > 0
    assert frictionless.total_slippage_cost == 0


def test_higher_fees_can_only_reduce_the_result() -> None:
    bars = rally_then_selloff()
    cheap = replay(
        bars, EmaCrossEngine(), config(cost_model=CostModel("c", Decimal("0.001"), Decimal("0")))
    )
    dear = replay(
        bars, EmaCrossEngine(), config(cost_model=CostModel("d", Decimal("0.01"), Decimal("0")))
    )
    assert dear.final_equity < cheap.final_equity
    assert dear.total_fees > cheap.total_fees


def test_costs_are_reported_separately_from_one_another() -> None:
    model = CostModel(label="both", fee_rate=Decimal("0.001"), slippage_rate=Decimal("0.001"))
    result = replay(rally_then_selloff(), EmaCrossEngine(), config(cost_model=model))
    assert result.total_fees > 0
    assert result.total_slippage_cost > 0
    for fill in result.fills:
        assert fill.fee > 0
        assert fill.slippage_cost > 0


# --------------------------------------------------------------------------
# The equity universe travels the same code path
# --------------------------------------------------------------------------


def test_an_equity_dataset_replays_through_the_same_engine() -> None:
    """One simulator, two universes. The only asset-class-aware thing is which
    symbol list the shared validator is given."""
    bars = equity_bars(symbol="SPY")
    result = replay(
        bars,
        EmaCrossEngine(),
        config(supported_symbols=EQUITY_SYMBOLS, universe_label=EQUITY_UNIVERSE_LABEL),
    )
    assert result.symbol == "SPY"
    assert result.bar_count == len(bars)


def test_an_equity_symbol_is_refused_under_the_crypto_universe() -> None:
    with pytest.raises(ReplayInputError, match="failed validation"):
        replay(
            equity_bars(symbol="SPY"),
            EmaCrossEngine(),
            config(universe_label=CRYPTO_UNIVERSE_LABEL),
        )


def test_a_dataset_that_fails_validation_aborts_the_replay() -> None:
    """A failing dataset is never silently repaired."""
    bars = wave(200)
    broken = bars.copy()
    broken.loc[5, "high"] = 0.0  # high below low: an OHLC violation
    with pytest.raises(ReplayInputError, match="failed validation"):
        replay(broken, EmaCrossEngine(), config())


# --------------------------------------------------------------------------
# Portfolio
# --------------------------------------------------------------------------


def test_sleeves_are_allocated_exactly_with_no_lost_cents() -> None:
    allocation = allocate_sleeves(("A", "B", "C"), Decimal("100000"))
    assert sum(allocation.values()) == Decimal("100000")
    assert len(set(allocation.values())) <= 2, "an even split, plus the remainder"


def test_a_portfolio_aggregates_its_sleeves_exactly() -> None:
    datasets = {"BTC/USD": wave(300), "ETH/USD": wave(300, phase=1.5, symbol="ETH/USD")}
    portfolio = replay_portfolio(datasets, EmaCrossEngine(), config())

    assert portfolio.symbols == ("BTC/USD", "ETH/USD")
    assert portfolio.final_equity == sum(
        sleeve.final_equity for sleeve in portfolio.sleeves.values()
    )
    assert portfolio.equity_curve[-1] == portfolio.final_equity
    assert portfolio.initial_cash == CASH


def test_a_portfolio_over_misaligned_timelines_forward_fills_each_sleeve() -> None:
    """An equity book has no overnight bar while a crypto book does. The
    aggregate must not invent a price move for a market that had no bar."""
    crypto = wave(200, symbol="BTC/USD")
    # Every third bar only: a sparser timeline than the crypto sleeve's.
    sparse = wave(200, symbol="ETH/USD").iloc[::3].reset_index(drop=True)
    portfolio = replay_portfolio({"BTC/USD": crypto, "ETH/USD": sparse}, EmaCrossEngine(), config())

    assert len(portfolio.timestamps) == len(set(crypto["timestamp"]) | set(sparse["timestamp"]))
    assert portfolio.equity_curve[-1] == portfolio.final_equity
    assert all(value > 0 for value in portfolio.equity_curve)


def test_a_duplicate_symbol_in_a_portfolio_is_refused() -> None:
    with pytest.raises(ReplayInputError, match="Duplicate symbols"):
        allocate_sleeves(("BTC/USD", "BTC/USD"), CASH)


def test_a_ten_symbol_equity_portfolio_replays() -> None:
    """The full Equity V0.2 universe, through the same simulator."""
    datasets = {symbol: equity_bars(200, symbol=symbol) for symbol in EQUITY_SYMBOLS}
    portfolio = replay_portfolio(
        datasets,
        EmaCrossEngine(),
        config(supported_symbols=EQUITY_SYMBOLS, universe_label=EQUITY_UNIVERSE_LABEL),
    )
    assert len(portfolio.symbols) == 10
    assert portfolio.final_equity > 0


# --------------------------------------------------------------------------
# Trade accounting invariants
# --------------------------------------------------------------------------


def test_pairing_refuses_a_sell_with_no_matching_buy() -> None:
    result = replay(rally_then_selloff(), EmaCrossEngine(), config())
    sells = [fill for fill in result.fills if fill.side is FillSide.SELL]
    with pytest.raises(TradeAccountingError, match="no matching BUY"):
        build_trades(sells[:1])


def test_pairing_refuses_two_consecutive_buys() -> None:
    result = replay(multi_cycle(), EmaCrossEngine(), config())
    buys = [fill for fill in result.fills if fill.side is FillSide.BUY]
    assert len(buys) >= 2
    with pytest.raises(TradeAccountingError, match="still open"):
        build_trades(buys[:2])


def test_benchmark_engine_enters_once_and_holds() -> None:
    result = replay(rally(), BuyAndHoldEngine(warmup=10), config())
    assert len(result.fills) == 1
    assert result.fills[0].side is FillSide.BUY
    assert result.open_position is not None


# --------------------------------------------------------------------------
# Offline guarantees
# --------------------------------------------------------------------------


def test_replay_needs_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    assert replay(rally_then_selloff(), EmaCrossEngine(), config()).fills


def test_replay_opens_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("the research simulator must not open a socket")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    assert replay(wave(), EmaCrossEngine(), config()).bar_count == 600
