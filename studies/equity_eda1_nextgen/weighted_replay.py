"""Shared-account weighted portfolio replay for the expanded-universe phases
(ledger §L3–L5 and the dated weighted-replay conventions).

One cash account, per-bar target weights, whole-share positions. The
accounting borrows everything it can from the validated machinery: the shipped
`CostModel` for fills and fees and the shipped `compute_metrics` at the shipped
equity clock. What is written here is the loop, under the ledger's declared
conventions:

- an order decided on bar ``t`` fills at bar ``t + 1``'s open;
- a symbol trades only when its **target weight changes** (share counts hold
  constant between weight changes — measured turnover is selection/regime
  churn, not drift-chasing);
- whole shares, Decimal money, $1,000,000 initial cash;
- forced terminal liquidation priced under the same cost model, both states
  reported.

Causality lives in the callers: target-weight series must be derived from
lagged completed-session information (state machines and selection rules are
tested for that separately). This module only ever *reads* the weight for the
bar being processed.

Determinism: symbols are processed in sorted order everywhere; the result is
invariant to input dict ordering (tested).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from autotrader.research.costs import CostModel, Side
from autotrader.research.metrics import EQUITY_15M, compute_metrics

INITIAL_CASH_WEIGHTED = Decimal("1000000")

_ZERO = Decimal(0)


class WeightedReplayError(Exception):
    """A weighted replay over inputs that cannot support its claims."""


@dataclass(frozen=True)
class WeightedResult:
    """One strategy, one cost model, one universe."""

    label: str
    cost_label: str
    timestamps: tuple[pd.Timestamp, ...]
    equity_curve: tuple[float, ...]
    initial_cash: float
    final_equity: float
    forced_liquidation_net: float
    fill_count: int
    traded_notional: float
    total_fees: float
    total_slippage: float
    exposure_mean: float
    exposure_bars: int
    max_active_names: int
    mean_active_names: float
    max_symbol_weight_assigned: float
    turnover: float

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


def replay_weighted(
    frames: Mapping[str, pd.DataFrame],
    targets: Mapping[str, Mapping[pd.Timestamp, float]],
    cost_model: CostModel,
    *,
    label: str,
    initial_cash: Decimal = INITIAL_CASH_WEIGHTED,
) -> WeightedResult:
    """Replay per-bar target weights over aligned 15m frames.

    ``frames`` maps symbol → session frame (canonical schema); ``targets``
    maps symbol → {bar timestamp → target weight}. A bar absent from a
    symbol's target map keeps that symbol's last acted weight; weights are
    refused when any bar's total exceeds 1 + 1e-6.
    """
    if not frames:
        raise WeightedReplayError("At least one frame is required.")
    if set(targets) - set(frames):
        raise WeightedReplayError("Targets reference symbols without frames.")

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
    pending: list[tuple[str, Decimal]] = []

    curve: list[float] = []
    out_stamps: list[pd.Timestamp] = []
    fills = 0
    traded_notional = _ZERO
    fees_total = _ZERO
    slip_total = _ZERO
    exposure_sum = 0.0
    exposure_bars = 0
    active_sum = 0
    max_active = 0
    max_weight_assigned = 0.0

    for stamp in stamps:
        # 1. Fill pending orders at this bar's open.
        for symbol, delta in pending:
            price = opens[symbol].get(stamp)
            if price is None or delta == _ZERO:
                continue
            side = Side.BUY if delta > _ZERO else Side.SELL
            fill = cost_model.fill_price(price, side)
            quantity = abs(delta)
            fee = cost_model.fee(quantity, fill)
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
        pending = []

        # 2. Mark, then decide new orders from this bar's targets.
        equity = cash
        for symbol in symbols:
            mark = closes[symbol].get(stamp)
            if mark is not None:
                last_mark[symbol] = mark
            if shares[symbol] and symbol in last_mark:
                equity += shares[symbol] * last_mark[symbol]

        bar_total = 0.0
        for symbol in symbols:
            symbol_targets = targets.get(symbol)
            if symbol_targets is None:
                continue
            target_weight = symbol_targets.get(stamp)
            if target_weight is None:
                bar_total += acted_weight[symbol]
                continue
            bar_total += target_weight
            if target_weight == acted_weight[symbol]:
                continue
            if target_weight < 0.0 or target_weight > 1.0:
                raise WeightedReplayError(
                    f"{symbol}@{stamp}: target weight {target_weight} outside [0, 1]."
                )
            if symbol not in last_mark:
                continue  # cannot price it yet; retry when a mark exists
            target_value = Decimal(str(target_weight)) * equity
            target_shares = int(target_value / last_mark[symbol])
            delta = Decimal(target_shares) - shares[symbol]
            acted_weight[symbol] = target_weight
            max_weight_assigned = max(max_weight_assigned, target_weight)
            if delta != _ZERO:
                pending.append((symbol, delta))
        if bar_total > 1.0 + 1e-6:
            raise WeightedReplayError(f"Bar {stamp}: target weights sum to {bar_total:.6f} > 1.")

        held_value = equity - cash
        exposure_sum += float(held_value / equity) if equity else 0.0
        active = sum(1 for symbol in symbols if shares[symbol] != _ZERO)
        if active:
            exposure_bars += 1
        active_sum += active
        max_active = max(max_active, active)
        curve.append(float(equity))
        out_stamps.append(stamp)

    final_equity = curve[-1]
    forced = cash
    for symbol in symbols:
        if shares[symbol] != _ZERO and symbol in last_mark:
            fill = cost_model.fill_price(last_mark[symbol], Side.SELL)
            fee = cost_model.fee(shares[symbol], fill)
            forced += shares[symbol] * fill - fee
    forced_net = float(forced / initial_cash - 1)

    bars = len(stamps)
    return WeightedResult(
        label=label,
        cost_label=cost_model.label,
        timestamps=tuple(out_stamps),
        equity_curve=tuple(curve),
        initial_cash=float(initial_cash),
        final_equity=final_equity,
        forced_liquidation_net=forced_net,
        fill_count=fills,
        traded_notional=float(traded_notional),
        total_fees=float(fees_total),
        total_slippage=float(slip_total),
        exposure_mean=exposure_sum / bars if bars else 0.0,
        exposure_bars=exposure_bars,
        max_active_names=max_active,
        mean_active_names=active_sum / bars if bars else 0.0,
        max_symbol_weight_assigned=max_weight_assigned,
        turnover=float(traded_notional / initial_cash),
    )


__all__ = [
    "INITIAL_CASH_WEIGHTED",
    "WeightedReplayError",
    "WeightedResult",
    "replay_weighted",
]
