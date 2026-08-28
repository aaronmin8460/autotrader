"""Deterministic replay: what an engine would have done over stored bars.

This is the research evaluator. It is engine-agnostic by construction - it
consumes `autotrader.research.engines.DecisionEngine` and names no strategy -
and it is deterministic by construction: the same frame, engine and
configuration always produce byte-identical results, because every money value
is an exact `Decimal` and nothing consults a clock, a random source or a
network.

**The no-look-ahead rule is the same one production backtesting obeys**
(docs/SPEC.md section 6F). A proposal derived from bar *t*'s close is first
actionable at the open of bar *t+1*::

    signal on bar t  ->  fill at bar t+1

A proposal is never filled on its own bar, and a proposal on the final bar is
left unexecuted rather than filled at an invented price. The rule lives in one
place - the pending-signal carry in `replay` - so there is exactly one line to
audit rather than a convention spread across the loop.

**What this shares with the production backtester, and what it does not.** The
decimal price conversion, the fee-reserving sizing and the quantity exponent
are imported from `autotrader.backtest.engine` rather than restated, so the two
cannot drift apart on the arithmetic that matters. What is new here is the
engine seam, configurable costs including slippage, per-bar exposure and
turnover accounting, and multi-symbol portfolio aggregation. The production
engine is not modified, not wrapped, and not replaced: it remains what the
`autotrader backtest` command runs.

**Portfolio replay allocates sleeves; it does not model shared cash.**
`replay_portfolio` partitions the starting capital across symbols and replays
each independently, then aggregates the equity curves onto a union timestamp
index. That is a deliberate, stated limitation: two symbols never compete for
the same dollar, so a portfolio result here is the sum of independent sleeves
rather than a simulation of the production runtime's sequential sizing against
one shared account. Modelling that contention faithfully means modelling the
account execution lock and the shared exposure ceiling, which is a runtime
concern rather than a research one, and pretending otherwise would produce a
number that looks like a portfolio backtest and is not.

Nothing here reaches a broker, opens a socket, or needs a credential.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, localcontext

import pandas as pd

from autotrader.backtest.engine import (
    DECIMAL_PRECISION,
    DEFAULT_INITIAL_CASH,
    QUANTITY_EXPONENT,
    to_decimal_price,
)
from autotrader.data.validation import (
    CRYPTO_UNIVERSE_LABEL,
    SUPPORTED_SYMBOLS,
    validate_frame,
)
from autotrader.research.costs import CRYPTO_COST, CostModel, Side
from autotrader.research.engines import Action, DecisionEngine, ResearchSignal, describe
from autotrader.research.trades import (
    Fill,
    FillSide,
    OpenPosition,
    Trade,
    build_trades,
    realized_pnl,
    traded_notional,
)

#: Fills use this bar column, one bar after the signal.
EXECUTION_PRICE_COLUMN = "open"

#: Open positions are marked against this column at each bar close.
MARK_PRICE_COLUMN = "close"

_ZERO = Decimal(0)
_ONE = Decimal(1)
_QUANTITY_STEP = QUANTITY_EXPONENT


class ReplayInputError(Exception):
    """The replay cannot run on what it was given."""


@dataclass(frozen=True)
class ReplayConfig:
    """Everything about a replay that is not the bars or the engine.

    `supported_symbols` and `universe_label` are how the same simulator serves
    crypto pairs and equity tickers: they are handed to the shared C2 validator
    rather than being branched on here, so there is one replay engine and not
    an asset-class fork of one.
    """

    initial_cash: Decimal = DEFAULT_INITIAL_CASH
    cost_model: CostModel = CRYPTO_COST
    supported_symbols: tuple[str, ...] = SUPPORTED_SYMBOLS
    universe_label: str = CRYPTO_UNIVERSE_LABEL
    validate: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.initial_cash, Decimal):
            raise ReplayInputError(
                f"initial_cash must be a Decimal, got {type(self.initial_cash).__name__}. "
                "Money is exact here; a float would make results depend on binary rounding."
            )
        if not self.initial_cash.is_finite() or self.initial_cash <= 0:
            raise ReplayInputError(
                f"initial_cash must be positive and finite, got {self.initial_cash}."
            )

    def to_json_dict(self) -> dict[str, object]:
        return {
            "initial_cash": str(self.initial_cash),
            "cost_model": self.cost_model.to_json_dict(),
            "universe_label": self.universe_label,
        }


@dataclass(frozen=True)
class ReplayResult:
    """Everything one replay produced.

    `equity_curve` holds one end-of-bar equity value per input bar, so its last
    element is `final_equity` and its length is `bar_count`. `exposure_bars`
    counts the bars a position was held at the close, which is what the
    exposure metric divides by the bar count.
    """

    symbol: str
    engine: Mapping[str, object]
    config: ReplayConfig
    bar_count: int
    timestamps: tuple[pd.Timestamp, ...]
    equity_curve: tuple[Decimal, ...]
    fills: tuple[Fill, ...]
    trades: tuple[Trade, ...]
    open_position: OpenPosition | None
    initial_cash: Decimal
    final_cash: Decimal
    final_equity: Decimal
    total_fees: Decimal
    total_slippage_cost: Decimal
    traded_notional: Decimal
    exposure_bars: int
    signal_count: int
    unexecuted_final_signal_count: int
    skipped_signal_count: int

    @property
    def realized_pnl(self) -> Decimal:
        """Profit from closed round trips only."""
        return realized_pnl(self.trades)

    @property
    def unrealized_pnl(self) -> Decimal:
        """Profit still riding on an open position, if any."""
        return _ZERO if self.open_position is None else self.open_position.unrealized_pnl

    @property
    def trade_count(self) -> int:
        return len(self.trades)


# --------------------------------------------------------------------------
# Input contract
# --------------------------------------------------------------------------


def _require_valid_bars(bars: pd.DataFrame, config: ReplayConfig) -> str:
    """Validate with C2 and return the dataset's one symbol.

    Data-quality rules are not restated here. A failing dataset aborts the
    replay rather than being repaired, for the same reason production
    backtesting refuses one: a silently patched dataset produces a result
    nobody can reproduce.
    """
    if "symbol" not in bars.columns:
        raise ReplayInputError("Bars are missing the 'symbol' column.")
    if not config.validate:
        symbols = pd.unique(bars["symbol"])
        if len(symbols) != 1:
            raise ReplayInputError(f"Bars must contain exactly one symbol, found {len(symbols)}.")
        return str(symbols[0])

    result = validate_frame(
        bars,
        supported_symbols=config.supported_symbols,
        universe_label=config.universe_label,
    )
    if not result.valid:
        findings = "\n".join(f"- {issue}" for issue in result.errors)
        raise ReplayInputError(
            f"Bars failed validation with {result.error_count} error(s); "
            f"the replay was not run.\n{findings}"
        )
    return str(result.symbol)


def affordable_quantity(cash: Decimal, fill_price: Decimal, cost_model: CostModel) -> Decimal:
    """The largest quantity `cash` buys at `fill_price` after paying the fee.

    Solves ``q * price * (1 + fee) <= cash`` and quantizes **down**, then steps
    back until the full cost genuinely fits. Same discipline as the production
    sizing helper, and for the same reason: spending all the cash on notional
    and charging the fee afterwards drives the balance negative, which is an
    accounting bug rather than a modelling choice. The step-back is defensive -
    quantizing down already guarantees it - because a cash limit must never be
    breached by a rounding artefact.

    `fill_price` is the slipped price, so sizing is done against what the trade
    will actually cost rather than against the bar's printed open.
    """
    if cash <= 0 or fill_price <= 0:
        return _ZERO
    quantity = (cash / (fill_price * (_ONE + cost_model.fee_rate))).quantize(
        QUANTITY_EXPONENT, rounding=ROUND_DOWN
    )
    while quantity > 0 and cost_model.buy_cost(quantity, fill_price) > cash:
        quantity -= _QUANTITY_STEP
    return quantity if quantity > 0 else _ZERO


# --------------------------------------------------------------------------
# Single-symbol replay
# --------------------------------------------------------------------------


def replay(
    bars: pd.DataFrame,
    engine: DecisionEngine,
    config: ReplayConfig | None = None,
) -> ReplayResult:
    """Replay `engine` over `bars` under `config`. `bars` is never modified.

    Long only, at most one open position, no leverage, fractional `Decimal`
    quantities. An ENTER_LONG while already long, an EXIT_LONG while flat, and
    an entry whose cash cannot cover the smallest representable quantity plus
    its fee are all no-ops rather than fills - counted in
    `skipped_signal_count` rather than silently dropped, so a result that
    generated a hundred proposals and took two trades says so.

    A position still open on the final bar is **not** liquidated. It is marked
    to that bar's close and reported as an `OpenPosition` whose profit is
    unrealized.
    """
    settings = ReplayConfig() if config is None else config
    symbol = _require_valid_bars(bars, settings)

    signals = tuple(engine.generate(bars))
    if len(bars) == 0:
        raise ReplayInputError("Cannot replay an empty dataset.")

    timestamps = list(bars["timestamp"])
    raw_execution_prices = bars[EXECUTION_PRICE_COLUMN].tolist()
    raw_mark_prices = bars[MARK_PRICE_COLUMN].tolist()

    index_of = {timestamp: index for index, timestamp in enumerate(timestamps)}
    signal_at: dict[int, ResearchSignal] = {}
    for signal in signals:
        position = index_of.get(signal.timestamp)
        if position is None:
            raise ReplayInputError(
                f"{engine.name} emitted a signal at {signal.timestamp}, which is not a bar in "
                "the dataset it was given. An engine may only signal on bars it observed."
            )
        signal_at[position] = signal

    fills: list[Fill] = []
    equity_curve: list[Decimal] = []
    costs = settings.cost_model

    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        cash = +settings.initial_cash
        quantity = _ZERO
        total_fees = _ZERO
        total_slippage = _ZERO
        exposure_bars = 0
        skipped = 0
        # The proposal awaiting the next bar. Carrying it forward exactly one
        # bar is the entire no-look-ahead rule; it is never consulted on the
        # bar that produced it.
        pending: ResearchSignal | None = None

        for index in range(len(timestamps)):
            if pending is not None:
                reference = to_decimal_price(raw_execution_prices[index])
                if pending.action is Action.ENTER_LONG and quantity == 0:
                    fill_price = costs.fill_price(reference, Side.BUY)
                    size = affordable_quantity(cash, fill_price, costs)
                    if size > 0:
                        fee = costs.fee(size, fill_price)
                        slippage = costs.slippage_cost(size, reference, Side.BUY)
                        cash -= size * fill_price + fee
                        quantity = size
                        total_fees += fee
                        total_slippage += slippage
                        fills.append(
                            Fill(
                                signal_timestamp=pending.timestamp,
                                timestamp=timestamps[index],
                                bar_index=index,
                                symbol=symbol,
                                side=FillSide.BUY,
                                quantity=size,
                                reference_price=reference,
                                fill_price=fill_price,
                                fee=fee,
                                slippage_cost=slippage,
                                cash_after=cash,
                                reason=pending.reason,
                            )
                        )
                    else:
                        skipped += 1
                elif pending.action is Action.EXIT_LONG and quantity > 0:
                    fill_price = costs.fill_price(reference, Side.SELL)
                    fee = costs.fee(quantity, fill_price)
                    slippage = costs.slippage_cost(quantity, reference, Side.SELL)
                    cash += quantity * fill_price - fee
                    total_fees += fee
                    total_slippage += slippage
                    fills.append(
                        Fill(
                            signal_timestamp=pending.timestamp,
                            timestamp=timestamps[index],
                            bar_index=index,
                            symbol=symbol,
                            side=FillSide.SELL,
                            quantity=quantity,
                            reference_price=reference,
                            fill_price=fill_price,
                            fee=fee,
                            slippage_cost=slippage,
                            cash_after=cash,
                            reason=pending.reason,
                        )
                    )
                    quantity = _ZERO
                else:
                    # An entry while already long or an exit while flat. Never
                    # a short, never a second helping of the same position.
                    skipped += 1
                pending = None

            # Mark only after this bar's open has been acted on.
            mark = to_decimal_price(raw_mark_prices[index])
            equity_curve.append(cash + quantity * mark)
            if quantity > 0:
                exposure_bars += 1
            pending = signal_at.get(index)

        final_mark = to_decimal_price(raw_mark_prices[-1])
        final_equity = cash + quantity * final_mark

    trades, open_position = build_trades(fills, final_mark_price=final_mark)

    return ReplayResult(
        symbol=symbol,
        engine=describe(engine),
        config=settings,
        bar_count=len(timestamps),
        timestamps=tuple(timestamps),
        equity_curve=tuple(equity_curve),
        fills=tuple(fills),
        trades=trades,
        open_position=open_position,
        initial_cash=settings.initial_cash,
        final_cash=cash,
        final_equity=final_equity,
        total_fees=total_fees,
        total_slippage_cost=total_slippage,
        traded_notional=traded_notional(fills),
        exposure_bars=exposure_bars,
        signal_count=len(signals),
        # The final bar has no successor, so a proposal there stays pending.
        unexecuted_final_signal_count=1 if pending is not None else 0,
        skipped_signal_count=skipped,
    )


# --------------------------------------------------------------------------
# Portfolio replay
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioResult:
    """Independent per-symbol sleeves, aggregated onto one timeline.

    `timestamps` is the sorted union of every sleeve's bars and `equity_curve`
    is the total portfolio equity on that index, each sleeve forward-filled
    from its most recent mark and counted at its starting cash before its first
    bar. Forward-filling is the honest choice for a symbol that has no bar at
    an instant - an equity book has no 03:00 bar while a crypto book does - and
    it means the aggregate never invents a price move for a market that was
    closed.
    """

    symbols: tuple[str, ...]
    sleeves: Mapping[str, ReplayResult]
    timestamps: tuple[pd.Timestamp, ...]
    equity_curve: tuple[Decimal, ...]
    initial_cash: Decimal
    final_equity: Decimal
    total_fees: Decimal
    total_slippage_cost: Decimal
    traded_notional: Decimal

    @property
    def trades(self) -> tuple[Trade, ...]:
        """Every sleeve's round trips, ordered by exit time then symbol."""
        everything = [trade for sleeve in self.sleeves.values() for trade in sleeve.trades]
        return tuple(sorted(everything, key=lambda trade: (trade.exit_timestamp, trade.symbol)))

    @property
    def fills(self) -> tuple[Fill, ...]:
        everything = [fill for sleeve in self.sleeves.values() for fill in sleeve.fills]
        return tuple(sorted(everything, key=lambda fill: (fill.timestamp, fill.symbol)))

    @property
    def realized_pnl(self) -> Decimal:
        return sum((sleeve.realized_pnl for sleeve in self.sleeves.values()), _ZERO)

    @property
    def unrealized_pnl(self) -> Decimal:
        return sum((sleeve.unrealized_pnl for sleeve in self.sleeves.values()), _ZERO)

    @property
    def exposure_bars(self) -> int:
        """Sleeve-bars with a position open, summed across sleeves."""
        return sum(sleeve.exposure_bars for sleeve in self.sleeves.values())

    @property
    def total_sleeve_bars(self) -> int:
        return sum(sleeve.bar_count for sleeve in self.sleeves.values())


def allocate_sleeves(
    symbols: Sequence[str],
    total_cash: Decimal,
) -> dict[str, Decimal]:
    """Split `total_cash` equally across `symbols`, exactly.

    Quantized to cents, with any remainder from an indivisible split given to
    the first symbol so the sleeves sum to the total exactly rather than
    approximately. An allocation that loses a fraction of a cent per symbol
    would make the portfolio's starting equity depend on how many symbols it
    held.
    """
    if not symbols:
        raise ReplayInputError("A portfolio needs at least one symbol.")
    if len(set(symbols)) != len(symbols):
        raise ReplayInputError(f"Duplicate symbols in the portfolio: {list(symbols)}.")

    cent = Decimal("0.01")
    each = (total_cash / len(symbols)).quantize(cent, rounding=ROUND_DOWN)
    if each <= 0:
        raise ReplayInputError(
            f"{total_cash} split across {len(symbols)} symbols leaves nothing per sleeve."
        )
    allocation = {symbol: each for symbol in symbols}
    allocation[symbols[0]] += total_cash - each * len(symbols)
    return allocation


def replay_portfolio(
    datasets: Mapping[str, pd.DataFrame],
    engine: DecisionEngine,
    config: ReplayConfig | None = None,
) -> PortfolioResult:
    """Replay `engine` across every dataset in `datasets` as independent sleeves.

    `datasets` maps symbol to that symbol's bars. Each sleeve gets an equal
    share of `config.initial_cash` and is replayed on its own; sleeves never
    compete for capital. See this module's docstring for why that limitation is
    stated rather than papered over.

    Symbols are processed in sorted order so the result never depends on
    dictionary insertion order.
    """
    settings = ReplayConfig() if config is None else config
    symbols = tuple(sorted(datasets))
    allocation = allocate_sleeves(symbols, settings.initial_cash)

    sleeves: dict[str, ReplayResult] = {}
    for symbol in symbols:
        sleeve_config = ReplayConfig(
            initial_cash=allocation[symbol],
            cost_model=settings.cost_model,
            supported_symbols=settings.supported_symbols,
            universe_label=settings.universe_label,
            validate=settings.validate,
        )
        sleeves[symbol] = replay(datasets[symbol], engine, sleeve_config)

    union = sorted({timestamp for sleeve in sleeves.values() for timestamp in sleeve.timestamps})
    curve: list[Decimal] = []
    cursors = dict.fromkeys(symbols, 0)
    # Before a sleeve's first bar its equity is simply the cash it was given;
    # it has not had the chance to be worth anything else yet.
    last_known = {symbol: allocation[symbol] for symbol in symbols}

    for timestamp in union:
        for symbol in symbols:
            sleeve = sleeves[symbol]
            cursor = cursors[symbol]
            while cursor < sleeve.bar_count and sleeve.timestamps[cursor] <= timestamp:
                last_known[symbol] = sleeve.equity_curve[cursor]
                cursor += 1
            cursors[symbol] = cursor
        curve.append(sum(last_known.values(), _ZERO))

    return PortfolioResult(
        symbols=symbols,
        sleeves=sleeves,
        timestamps=tuple(union),
        equity_curve=tuple(curve),
        initial_cash=settings.initial_cash,
        final_equity=sum((sleeve.final_equity for sleeve in sleeves.values()), _ZERO),
        total_fees=sum((sleeve.total_fees for sleeve in sleeves.values()), _ZERO),
        total_slippage_cost=sum((sleeve.total_slippage_cost for sleeve in sleeves.values()), _ZERO),
        traded_notional=sum((sleeve.traded_notional for sleeve in sleeves.values()), _ZERO),
    )


__all__ = [
    "EXECUTION_PRICE_COLUMN",
    "MARK_PRICE_COLUMN",
    "PortfolioResult",
    "ReplayConfig",
    "ReplayInputError",
    "ReplayResult",
    "affordable_quantity",
    "allocate_sleeves",
    "replay",
    "replay_portfolio",
]
