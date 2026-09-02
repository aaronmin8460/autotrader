"""Short-capable shared-account replay (ledger §L5).

This is the inherited `replay_weighted` loop with a signed-weight extension.
The extension is deliberately narrow, because the property that makes any
short number admissible is the **reduction identity**: on a long-only target
series this engine must reproduce the inherited engine's `WeightedResult`
field for field, to exact float equality. That is proven on constructed
frames and on the real EDA-1 U10 bridge targets before any short result is
computed (§L5.1).

What the signed extension adds, and nothing else:

- **negative target weights.** A negative weight means a short position of
  that fraction of equity. Whole shares throughout: `int(target_value/mark)`
  truncates toward zero, so a short is rounded *smaller*, never larger.
- **sign arithmetic.** Opening a short is a SELL (proceeds credited, adverse
  slippage downward); covering is a BUY (cash debited, adverse slippage
  upward). Equity marks short share counts negative, so a rising price
  reduces equity. None of this is special-cased — it is the inherited
  arithmetic with the sign left in.
- **a separate short-side cost model.** A short is harder to execute than a
  long and this program refuses to pretend otherwise (§L7).
- **borrow cost**, charged per bar on the absolute short market value at
  `annual_rate / BARS_PER_YEAR`, debited from cash. Zero rate charges
  exactly zero, which is what keeps the reduction identity exact.
- **exposure accounting**: long gross, short gross, total gross and net,
  every bar, reported and bounded — never net alone.
- **P&L attribution**: every dollar of the equity change is attributed to the
  long book, the short book, or borrow, and the three are asserted to
  reconcile to the equity change. The short sleeve is never hidden inside a
  blended curve.

Causality is unchanged and still lives in the callers: a target decided on
bar `t` fills at `t + 1`'s open, and this module only ever reads the weight
for the bar being processed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from autotrader.research.costs import CostModel, Side
from autotrader.research.metrics import EQUITY_15M, compute_metrics
from studies.equity_eda1_nextgen.weighted_replay import INITIAL_CASH_WEIGHTED

#: The equity 15m clock's bars per year, matching the shipped metrics module.
BARS_PER_YEAR = 26 * 252

#: Reconciliation tolerance for the P&L attribution identity, in dollars on a
#: $1,000,000 account. Decimal arithmetic is exact; this guards float display
#: only and is asserted at 1e-6 in the tests.
RECONCILE_TOLERANCE = Decimal("0.000001")

_ZERO = Decimal(0)


class ShortReplayError(Exception):
    """A signed replay over inputs that cannot support its claims."""


@dataclass(frozen=True)
class BorrowModel:
    """One predeclared annualized borrow assumption (§L7).

    Charged on the absolute market value of the short book, every bar it is
    held. There is no historical borrow series anywhere in this program and
    this class is not pretending to be one — it is a named scenario.
    """

    label: str
    annual_rate: Decimal

    def __post_init__(self) -> None:
        if self.annual_rate < 0:
            raise ShortReplayError(f"Borrow rate {self.annual_rate} cannot be negative.")

    @property
    def per_bar(self) -> Decimal:
        return self.annual_rate / Decimal(BARS_PER_YEAR)

    def to_json_dict(self) -> dict[str, object]:
        return {"label": self.label, "annual_rate": str(self.annual_rate)}


#: The §L7 borrow grid, frozen before any short result. PRIMARY = MODERATE.
BORROW_ZERO = BorrowModel("borrow-zero", Decimal("0"))
BORROW_LOW = BorrowModel("borrow-low", Decimal("0.003"))
BORROW_MODERATE = BorrowModel("borrow-moderate", Decimal("0.010"))
BORROW_HIGH = BorrowModel("borrow-high", Decimal("0.030"))
BORROW_EXTREME = BorrowModel("borrow-extreme", Decimal("0.100"))
BORROW_MODELS: tuple[BorrowModel, ...] = (
    BORROW_ZERO,
    BORROW_LOW,
    BORROW_MODERATE,
    BORROW_HIGH,
    BORROW_EXTREME,
)
PRIMARY_BORROW = BORROW_MODERATE


@dataclass(frozen=True)
class ShortResult:
    """One strategy, one cost pair, one borrow scenario."""

    label: str
    cost_label: str
    short_cost_label: str
    borrow_label: str
    timestamps: tuple[pd.Timestamp, ...]
    equity_curve: tuple[float, ...]
    initial_cash: float
    final_equity: float
    forced_liquidation_net: float
    fill_count: int
    short_fill_count: int
    traded_notional: float
    short_traded_notional: float
    total_fees: float
    total_slippage: float
    borrow_cost: float
    exposure_mean: float
    exposure_bars: int
    long_gross_mean: float
    short_gross_mean: float
    short_gross_max: float
    total_gross_mean: float
    total_gross_max: float
    net_exposure_mean: float
    net_exposure_min: float
    max_active_names: int
    mean_active_names: float
    mean_short_names: float
    max_short_names: int
    max_symbol_weight_assigned: float
    max_short_weight_assigned: float
    turnover: float
    short_turnover: float
    long_pnl: float
    short_pnl: float
    short_bars: int
    #: Per-bar short-book P&L (dollars), aligned to `timestamps`.
    short_pnl_series: tuple[float, ...]
    #: Per-bar long-book P&L (dollars), aligned to `timestamps`.
    long_pnl_series: tuple[float, ...]
    #: Per-bar short gross weight actually held, aligned to `timestamps`.
    short_gross_series: tuple[float, ...]
    reconciliation_error: float

    @property
    def net_return(self) -> float:
        return self.final_equity / self.initial_cash - 1.0

    def metrics(self):
        curve = [Decimal(str(value)) for value in self.equity_curve]
        return compute_metrics(
            equity_curve=curve,
            trades=[],
            initial_equity=Decimal(str(self.initial_cash)),
            bar_clock=EQUITY_15M,
            traded_notional=Decimal(str(self.traded_notional)),
            exposure_bars=self.exposure_bars,
            total_fees=Decimal(str(self.total_fees)),
            total_slippage_cost=Decimal(str(self.total_slippage)),
        )


def replay_signed(
    frames: Mapping[str, pd.DataFrame],
    targets: Mapping[str, Mapping[pd.Timestamp, float]],
    cost_model: CostModel,
    *,
    label: str,
    short_cost_model: CostModel | None = None,
    borrow: BorrowModel = BORROW_ZERO,
    max_short_gross: float = 1.0,
    initial_cash: Decimal = INITIAL_CASH_WEIGHTED,
) -> ShortResult:
    """Replay signed per-bar target weights over aligned 15m frames.

    With every weight >= 0, `short_cost_model` unused, and `borrow` at zero
    rate, this reproduces `replay_weighted` exactly (§L5.1).
    """
    if not frames:
        raise ShortReplayError("At least one frame is required.")
    if set(targets) - set(frames):
        raise ShortReplayError("Targets reference symbols without frames.")
    short_costs = short_cost_model if short_cost_model is not None else cost_model

    symbols = sorted(frames)
    opens: dict[str, dict[pd.Timestamp, Decimal]] = {}
    closes: dict[str, dict[pd.Timestamp, Decimal]] = {}
    all_stamps: set[pd.Timestamp] = set()
    for symbol in symbols:
        frame = frames[symbol]
        o: dict[pd.Timestamp, Decimal] = {}
        c: dict[pd.Timestamp, Decimal] = {}
        for ts, open_price, close_price in zip(
            frame["timestamp"], frame["open"], frame["close"], strict=True
        ):
            stamp = pd.Timestamp(ts)
            o[stamp] = Decimal(str(open_price))
            c[stamp] = Decimal(str(close_price))
        opens[symbol] = o
        closes[symbol] = c
        all_stamps.update(o)
    stamps = sorted(all_stamps)

    cash = initial_cash
    shares: dict[str, Decimal] = dict.fromkeys(symbols, _ZERO)
    acted_weight: dict[str, float] = dict.fromkeys(symbols, 0.0)
    last_mark: dict[str, Decimal] = {}
    previous_mark: dict[str, Decimal] = {}
    pending: list[tuple[str, Decimal]] = []

    curve: list[float] = []
    out_stamps: list[pd.Timestamp] = []
    fills = 0
    short_fills = 0
    traded_notional = _ZERO
    short_traded_notional = _ZERO
    fees_total = _ZERO
    slip_total = _ZERO
    borrow_total = _ZERO
    exposure_sum = 0.0
    exposure_bars = 0
    long_gross_sum = 0.0
    short_gross_sum = 0.0
    short_gross_max = 0.0
    total_gross_sum = 0.0
    total_gross_max = 0.0
    net_sum = 0.0
    net_min = float("inf")
    active_sum = 0
    max_active = 0
    short_names_sum = 0
    max_short_names = 0
    short_bars = 0
    max_weight_assigned = 0.0
    max_short_weight_assigned = 0.0
    long_pnl = _ZERO
    short_pnl = _ZERO
    long_pnl_series: list[float] = []
    short_pnl_series: list[float] = []
    short_gross_series: list[float] = []

    for stamp in stamps:
        bar_long_pnl = _ZERO
        bar_short_pnl = _ZERO

        # 0. Mark-to-market on positions carried into this bar, attributed by
        #    the sign of the position that earned it.
        for symbol in symbols:
            held = shares[symbol]
            if held == _ZERO:
                continue
            current = closes[symbol].get(stamp)
            prior = previous_mark.get(symbol)
            if current is None or prior is None:
                continue
            move = held * (current - prior)
            if held > _ZERO:
                bar_long_pnl += move
            else:
                bar_short_pnl += move

        # 1. Fill pending orders at this bar's open.
        for symbol, delta in pending:
            price = opens[symbol].get(stamp)
            if price is None or delta == _ZERO:
                continue
            before = shares[symbol]
            after = before + delta
            is_short_side = before < _ZERO or after < _ZERO
            model = short_costs if is_short_side else cost_model
            side = Side.BUY if delta > _ZERO else Side.SELL
            fill = model.fill_price(price, side)
            quantity = abs(delta)
            fee = model.fee(quantity, fill)
            if side is Side.BUY:
                cash -= quantity * fill + fee
                shares[symbol] += quantity
            else:
                cash += quantity * fill - fee
                shares[symbol] -= quantity
            fills += 1
            traded_notional += quantity * fill
            fees_total += fee
            slip_total += quantity * abs(fill - price)
            # Attribution: the intraday move on the new shares, the slippage
            # and the fee, all charged to the book the trade belongs to.
            settle = closes[symbol].get(stamp)
            contribution = _ZERO
            if settle is not None:
                contribution += delta * (settle - price)
            contribution -= delta * (fill - price)
            contribution -= fee
            if is_short_side:
                short_fills += 1
                short_traded_notional += quantity * fill
                bar_short_pnl += contribution
            else:
                bar_long_pnl += contribution
        pending = []

        # 2. Mark, charge borrow, then decide new orders from this bar's targets.
        #    The equity accumulation below is written in the inherited engine's
        #    exact order (cash, then symbols in sorted order) so that the
        #    long-only reduction identity is float-exact rather than merely
        #    close — Decimal addition is exact but its context precision is
        #    finite, so grouping is not free.
        short_value = _ZERO
        long_value = _ZERO
        for symbol in symbols:
            mark = closes[symbol].get(stamp)
            if mark is not None:
                last_mark[symbol] = mark
            if shares[symbol] and symbol in last_mark:
                value = shares[symbol] * last_mark[symbol]
                if value < _ZERO:
                    short_value += value
                else:
                    long_value += value

        borrow_charge = (-short_value) * borrow.per_bar
        if borrow_charge != _ZERO:
            cash -= borrow_charge
            borrow_total += borrow_charge
            bar_short_pnl -= borrow_charge

        equity = cash
        for symbol in symbols:
            if shares[symbol] and symbol in last_mark:
                equity += shares[symbol] * last_mark[symbol]

        bar_long_weight = 0.0
        bar_short_weight = 0.0
        for symbol in symbols:
            symbol_targets = targets.get(symbol)
            if symbol_targets is None:
                continue
            target_weight = symbol_targets.get(stamp)
            if target_weight is None:
                target_weight = acted_weight[symbol]
                if target_weight >= 0.0:
                    bar_long_weight += target_weight
                else:
                    bar_short_weight -= target_weight
                continue
            if target_weight >= 0.0:
                bar_long_weight += target_weight
            else:
                bar_short_weight -= target_weight
            if target_weight == acted_weight[symbol]:
                continue
            if target_weight > 1.0 or target_weight < -1.0:
                raise ShortReplayError(
                    f"{symbol}@{stamp}: target weight {target_weight} outside [-1, 1]."
                )
            if symbol not in last_mark:
                continue  # cannot price it yet; retry when a mark exists
            target_value = Decimal(str(target_weight)) * equity
            target_shares = int(target_value / last_mark[symbol])
            delta = Decimal(target_shares) - shares[symbol]
            acted_weight[symbol] = target_weight
            max_weight_assigned = max(max_weight_assigned, target_weight)
            if target_weight < 0.0:
                max_short_weight_assigned = max(max_short_weight_assigned, -target_weight)
            if delta != _ZERO:
                pending.append((symbol, delta))
        if bar_long_weight > 1.0 + 1e-6:
            raise ShortReplayError(
                f"Bar {stamp}: long target weights sum to {bar_long_weight:.6f} > 1."
            )
        if bar_short_weight > max_short_gross + 1e-6:
            raise ShortReplayError(
                f"Bar {stamp}: short target weights sum to {bar_short_weight:.6f} > "
                f"the declared cap {max_short_gross}."
            )

        for symbol in symbols:
            mark = closes[symbol].get(stamp)
            if mark is not None:
                previous_mark[symbol] = mark

        held_value = equity - cash
        long_gross = float(long_value / equity) if equity else 0.0
        short_gross = float(-short_value / equity) if equity else 0.0
        long_gross_sum += long_gross
        short_gross_sum += short_gross
        short_gross_max = max(short_gross_max, short_gross)
        total_gross_sum += long_gross + short_gross
        total_gross_max = max(total_gross_max, long_gross + short_gross)
        net_sum += long_gross - short_gross
        net_min = min(net_min, long_gross - short_gross)
        exposure_sum += float(held_value / equity) if equity else 0.0
        active = sum(1 for symbol in symbols if shares[symbol] != _ZERO)
        shorts_held = sum(1 for symbol in symbols if shares[symbol] < _ZERO)
        if active:
            exposure_bars += 1
        if shorts_held:
            short_bars += 1
        active_sum += active
        max_active = max(max_active, active)
        short_names_sum += shorts_held
        max_short_names = max(max_short_names, shorts_held)
        long_pnl += bar_long_pnl
        short_pnl += bar_short_pnl
        long_pnl_series.append(float(bar_long_pnl))
        short_pnl_series.append(float(bar_short_pnl))
        short_gross_series.append(short_gross)
        curve.append(float(equity))
        out_stamps.append(stamp)
        final_equity_exact = equity

    final_equity = curve[-1]
    forced = cash
    for symbol in symbols:
        held = shares[symbol]
        if held == _ZERO or symbol not in last_mark:
            continue
        if held > _ZERO:
            fill = cost_model.fill_price(last_mark[symbol], Side.SELL)
            forced += held * fill - cost_model.fee(held, fill)
        else:
            quantity = -held
            fill = short_costs.fill_price(last_mark[symbol], Side.BUY)
            forced -= quantity * fill + short_costs.fee(quantity, fill)
    forced_net = float(forced / initial_cash - 1)

    reconcile = final_equity_exact - initial_cash - long_pnl - short_pnl
    bars = len(stamps)
    return ShortResult(
        label=label,
        cost_label=cost_model.label,
        short_cost_label=short_costs.label,
        borrow_label=borrow.label,
        timestamps=tuple(out_stamps),
        equity_curve=tuple(curve),
        initial_cash=float(initial_cash),
        final_equity=final_equity,
        forced_liquidation_net=forced_net,
        fill_count=fills,
        short_fill_count=short_fills,
        traded_notional=float(traded_notional),
        short_traded_notional=float(short_traded_notional),
        total_fees=float(fees_total),
        total_slippage=float(slip_total),
        borrow_cost=float(borrow_total),
        exposure_mean=exposure_sum / bars if bars else 0.0,
        exposure_bars=exposure_bars,
        long_gross_mean=long_gross_sum / bars if bars else 0.0,
        short_gross_mean=short_gross_sum / bars if bars else 0.0,
        short_gross_max=short_gross_max,
        total_gross_mean=total_gross_sum / bars if bars else 0.0,
        total_gross_max=total_gross_max,
        net_exposure_mean=net_sum / bars if bars else 0.0,
        net_exposure_min=net_min if bars else 0.0,
        max_active_names=max_active,
        mean_active_names=active_sum / bars if bars else 0.0,
        mean_short_names=short_names_sum / bars if bars else 0.0,
        max_short_names=max_short_names,
        max_symbol_weight_assigned=max_weight_assigned,
        max_short_weight_assigned=max_short_weight_assigned,
        turnover=float(traded_notional / initial_cash),
        short_turnover=float(short_traded_notional / initial_cash),
        long_pnl=float(long_pnl),
        short_pnl=float(short_pnl),
        short_bars=short_bars,
        short_pnl_series=tuple(short_pnl_series),
        long_pnl_series=tuple(long_pnl_series),
        short_gross_series=tuple(short_gross_series),
        reconciliation_error=float(reconcile),
    )


__all__ = [
    "BARS_PER_YEAR",
    "BORROW_EXTREME",
    "BORROW_HIGH",
    "BORROW_LOW",
    "BORROW_MODELS",
    "BORROW_MODERATE",
    "BORROW_ZERO",
    "PRIMARY_BORROW",
    "RECONCILE_TOLERANCE",
    "BorrowModel",
    "ShortReplayError",
    "ShortResult",
    "replay_signed",
]
