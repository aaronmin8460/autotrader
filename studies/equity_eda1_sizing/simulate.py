"""One account, ten symbols, two ceilings: the shared-account portfolio replay.

The shipped `replay_portfolio` cannot answer this study's question. It runs ten
**independent** sleeves - a fixed $10,000 book each, one position, fully
deployed while long - and sleeves never compete for a dollar. That is exactly
the assumption the deep-architecture program declared as a limitation, and
exactly what production violates: one account, one cash balance, a 5% per-symbol
ceiling, a 30% total ceiling, and a crypto book already holding part of it.

So this module is a second simulator, and it is deliberately thin. Everything it
can borrow, it borrows: the shipped `CostModel` for fills and fees, the shipped
`compute_metrics` at the shipped equity clock, and - the point of the exercise -
the **production** allocator for every weight it acts on. The only thing written
here is the accounting loop.

**Causality is the research's rule, unchanged.** A target computed from bar *k*'s
close is acted on at bar *k+1*'s open, never on bar *k*. A symbol is only ever
traded on a bar it actually has.

**Turnover is production's, not a simulator's.** Targets are quantized to whole
shares exactly as `normalize_share_quantity` does, and an order is emitted only
when the integral target differs from the integral holding (ledger §L2a). The
alternative rule - hold shares constant between weight changes - is available as
`RebalanceRule.WEIGHT_CHANGE` and reported alongside, so the result's sensitivity
to the rebalancing rule is visible rather than assumed.

**Trades, deliberately absent.** A continuously re-targeted book has no
well-defined round trip, so no `Trade` objects are synthesized and
`compute_metrics` is handed an empty trade list. Every metric this study grades
on - net, Sharpe, Sortino, max drawdown, volatility, exposure, turnover, cost
drag - is computed from the equity curve and the fills, none from a round-trip
population. Fill counts are reported in place of trade counts and are not
presented as trades.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from enum import Enum

import pandas as pd

from autotrader.equity.allocation import AllocationPolicy, target_weights, whole_shares
from autotrader.research.costs import CostModel, Side
from autotrader.research.metrics import EQUITY_15M, PerformanceMetrics, compute_metrics
from studies.equity_eda1_sizing import STUDY_SYMBOLS

#: The research simulator's working precision, matched so money arithmetic here
#: rounds the way money arithmetic there does.
DECIMAL_PRECISION = 34

INITIAL_CASH = Decimal("100000")

_ZERO = Decimal(0)


class SimulationError(Exception):
    """A shared-account replay over inputs that cannot support its claims."""


class RebalanceRule(Enum):
    """When a symbol's target is allowed to become an order."""

    #: Production's rule (ledger §L2a): act whenever the integral target share
    #: count differs from the integral holding. The whole-share floor is itself
    #: the no-trade band, and it has no parameter.
    WHOLE_SHARE = "whole_share"

    #: The originally predeclared rule (ledger §L2), retained as a robustness
    #: variant: act only when the target *weight* changes from the weight last
    #: acted on for that symbol, holding the share count constant in between.
    WEIGHT_CHANGE = "weight_change"


@dataclass
class Fill:
    """One executed delta. Enough to audit the path, not a round trip."""

    timestamp: pd.Timestamp
    symbol: str
    side: str
    quantity: Decimal
    reference_price: Decimal
    fill_price: Decimal
    fee: Decimal
    slippage_cost: Decimal


@dataclass
class SimulationResult:
    """One policy, one cost model, one external-exposure scenario."""

    label: str
    policy_id: str
    external_exposure_fraction: Decimal
    cost_label: str
    rule: str
    timestamps: tuple[pd.Timestamp, ...]
    equity_curve: tuple[Decimal, ...]
    initial_cash: Decimal
    final_cash: Decimal
    total_fees: Decimal
    total_slippage_cost: Decimal
    traded_notional: Decimal
    exposure_bars: int
    fill_count: int
    max_symbol_weight: Decimal
    max_total_weight: Decimal
    max_realized_symbol_fraction: float
    max_realized_total_fraction: float
    weight_asymmetry_bars: int
    final_positions: dict[str, Decimal] = field(default_factory=dict)
    final_marks: dict[str, Decimal] = field(default_factory=dict)

    @property
    def net_return(self) -> float:
        return float(self.equity_curve[-1] / self.initial_cash - 1)

    def metrics(self) -> PerformanceMetrics:
        """The shipped metrics, at the shipped equity clock."""
        return compute_metrics(
            equity_curve=list(self.equity_curve),
            trades=(),
            initial_equity=self.initial_cash,
            bar_clock=EQUITY_15M,
            traded_notional=self.traded_notional,
            exposure_bars=self.exposure_bars,
            total_fees=self.total_fees,
            total_slippage_cost=self.total_slippage_cost,
        )

    def forced_liquidation_net(self, cost_model: CostModel) -> float:
        """Net return if every terminal open position were sold at the last mark."""
        equity = self.final_cash
        for symbol, quantity in self.final_positions.items():
            if quantity <= _ZERO:
                continue
            mark = self.final_marks[symbol]
            fill = cost_model.fill_price(mark, Side.SELL)
            equity += quantity * fill - cost_model.fee(quantity, fill)
        return float(equity / self.initial_cash - 1)


def build_price_tables(
    frames: Mapping[str, pd.DataFrame],
    symbols: Sequence[str],
) -> tuple[pd.DatetimeIndex, dict[str, list[float | None]], dict[str, list[float | None]]]:
    """The union clock, and each symbol's open/close aligned onto it.

    `open` is None on a bar the symbol does not have - that market was not
    trading and no order may fill there. `close` is forward-filled, because a
    position still exists on a bar its symbol did not print and must be marked
    at the last price anyone saw.
    """
    stamps = pd.DatetimeIndex(sorted({ts for symbol in symbols for ts in frames[symbol].index}))
    opens: dict[str, list[float | None]] = {}
    closes: dict[str, list[float | None]] = {}
    for symbol in symbols:
        frame = frames[symbol]
        aligned = frame.reindex(stamps)
        opens[symbol] = [None if pd.isna(v) else float(v) for v in aligned["open"]]
        marks = aligned["close"].ffill()
        closes[symbol] = [None if pd.isna(v) else float(v) for v in marks]
    return stamps, opens, closes


def simulate(
    *,
    label: str,
    stances: pd.DataFrame,
    frames: Mapping[str, pd.DataFrame],
    policy: AllocationPolicy,
    cost_model: CostModel,
    cost_label: str,
    external_exposure_fraction: Decimal,
    symbols: Sequence[str] = STUDY_SYMBOLS,
    rule: RebalanceRule = RebalanceRule.WHOLE_SHARE,
    initial_cash: Decimal = INITIAL_CASH,
) -> SimulationResult:
    """Replay one stance series through one allocation policy on one account.

    `stances` is a boolean frame indexed by timestamp with one column per
    symbol: True where the engine holds that symbol LONG at that bar. The active
    set handed to the allocator is built from it as an unordered `set`, so the
    column order of the frame cannot influence a weight - which is criterion 1
    of the ledger, made structurally true rather than merely tested.
    """
    ordered = [symbol for symbol in symbols]
    stamps, opens, closes = build_price_tables(frames, ordered)
    stance_aligned = stances.reindex(stamps)

    fills: list[Fill] = []
    equity_curve: list[Decimal] = []

    max_symbol_weight = _ZERO
    max_total_weight = _ZERO
    max_realized_symbol = 0.0
    max_realized_total = 0.0
    asymmetry_bars = 0
    exposure_bars = 0
    traded_notional = _ZERO

    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        cash = +initial_cash
        held: dict[str, Decimal] = {symbol: _ZERO for symbol in ordered}
        acted_weight: dict[str, Decimal] = {symbol: _ZERO for symbol in ordered}
        pending: dict[str, Decimal] = {}
        pending_weight: dict[str, Decimal] = {}

        for index in range(len(stamps)):
            # ---- 1. act on the previous bar's targets, at this bar's open ----
            if pending:
                # SELLs first: an exit frees cash the same bar's entries may use,
                # which is what a real account does and what keeps the no-leverage
                # invariant from failing on a pure rotation.
                sells_first = sorted(pending, key=lambda name: pending[name] > held[name])
                for symbol in sells_first:
                    target = pending[symbol]
                    reference_raw = opens[symbol][index]
                    if reference_raw is None:
                        continue
                    reference = Decimal(str(reference_raw))
                    current = held[symbol]
                    if target == current:
                        continue
                    if target > current:
                        quantity = target - current
                        fill_price = cost_model.fill_price(reference, Side.BUY)
                        fee = cost_model.fee(quantity, fill_price)
                        cost = quantity * fill_price + fee
                        if cost > cash:
                            # No leverage, ever. An order the account cannot fund
                            # does not happen; the next bar re-targets from what
                            # it actually holds.
                            continue
                        cash -= cost
                        held[symbol] = target
                        side = "BUY"
                    else:
                        quantity = current - target
                        fill_price = cost_model.fill_price(reference, Side.SELL)
                        fee = cost_model.fee(quantity, fill_price)
                        cash += quantity * fill_price - fee
                        held[symbol] = target
                        side = "SELL"
                    slippage = cost_model.slippage_cost(
                        quantity, reference, Side.BUY if side == "BUY" else Side.SELL
                    )
                    traded_notional += quantity * fill_price
                    acted_weight[symbol] = pending_weight[symbol]
                    fills.append(
                        Fill(
                            timestamp=stamps[index],
                            symbol=symbol,
                            side=side,
                            quantity=quantity,
                            reference_price=reference,
                            fill_price=fill_price,
                            fee=fee,
                            slippage_cost=slippage,
                        )
                    )
                pending = {}
                pending_weight = {}

            # ---- 2. mark to this bar's close ----
            marks: dict[str, Decimal] = {}
            exposure = _ZERO
            for symbol in ordered:
                mark_raw = closes[symbol][index]
                if mark_raw is None:
                    if held[symbol] > _ZERO:
                        raise SimulationError(
                            f"{symbol} holds {held[symbol]} at {stamps[index]} with no mark."
                        )
                    continue
                mark = Decimal(str(mark_raw))
                marks[symbol] = mark
                exposure += held[symbol] * mark
            equity = cash + exposure
            equity_curve.append(equity)
            if exposure > _ZERO:
                exposure_bars += 1
            if equity > _ZERO:
                total_fraction = float(exposure / equity)
                max_realized_total = max(max_realized_total, total_fraction)
                for symbol in ordered:
                    if held[symbol] > _ZERO and symbol in marks:
                        fraction = float(held[symbol] * marks[symbol] / equity)
                        max_realized_symbol = max(max_realized_symbol, fraction)

            # ---- 3. compute the next bar's targets from this bar's close ----
            row = stance_aligned.iloc[index]
            active = {
                symbol
                for symbol in ordered
                if symbol in marks and row.get(symbol) is not pd.NA and bool(row.get(symbol))
            }
            weights = target_weights(
                policy,
                active_symbols=active,
                external_exposure_fraction=external_exposure_fraction,
            )
            if weights:
                distinct = set(weights.values())
                if len(distinct) > 1:
                    # Criterion 2: two symbols that are both active must never be
                    # sized differently. Counted per bar over the whole region.
                    asymmetry_bars += 1
                max_symbol_weight = max(max_symbol_weight, max(distinct))
                max_total_weight = max(max_total_weight, sum(weights.values(), _ZERO))

            if equity <= _ZERO:
                raise SimulationError(f"Account equity reached {equity} at {stamps[index]}.")

            for symbol in ordered:
                weight = weights.get(symbol, _ZERO)
                mark = marks.get(symbol)
                if mark is None:
                    continue
                if weight <= _ZERO:
                    target = _ZERO
                elif rule is RebalanceRule.WEIGHT_CHANGE and weight == acted_weight[symbol]:
                    # Held constant between weight changes, by the L2 rule.
                    continue
                else:
                    target = whole_shares(weight * equity, mark)
                if target != held[symbol]:
                    pending[symbol] = target
                    pending_weight[symbol] = weight

        final_marks = {
            symbol: Decimal(str(closes[symbol][-1]))
            for symbol in ordered
            if closes[symbol][-1] is not None
        }

    return SimulationResult(
        label=label,
        policy_id=policy.policy_id,
        external_exposure_fraction=external_exposure_fraction,
        cost_label=cost_label,
        rule=rule.value,
        timestamps=tuple(stamps),
        equity_curve=tuple(equity_curve),
        initial_cash=initial_cash,
        final_cash=cash,
        total_fees=sum((fill.fee for fill in fills), _ZERO),
        total_slippage_cost=sum((fill.slippage_cost for fill in fills), _ZERO),
        traded_notional=traded_notional,
        exposure_bars=exposure_bars,
        fill_count=len(fills),
        max_symbol_weight=max_symbol_weight,
        max_total_weight=max_total_weight,
        max_realized_symbol_fraction=max_realized_symbol,
        max_realized_total_fraction=max_realized_total,
        weight_asymmetry_bars=asymmetry_bars,
        final_positions=dict(held),
        final_marks=final_marks,
    )


__all__ = [
    "DECIMAL_PRECISION",
    "INITIAL_CASH",
    "Fill",
    "RebalanceRule",
    "SimulationError",
    "SimulationResult",
    "build_price_tables",
    "simulate",
]
