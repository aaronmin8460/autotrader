"""Benchmarks and terminal-state diagnostics: buy-and-hold, cash, forced liquidation.

**Buy-and-hold goes through the same simulator as every engine.** The
`BuyAndHoldEngine` proposes ENTER_LONG on the first bar it observes and never
exits, so the replay fills it at the second bar's open under the same
next-executable-bar rule, charges it the same costs, and marks it to the same
closes. A benchmark computed by a different code path would not be a
comparison; this one differs from an engine result by the decision series
alone. Its one-bar entry delay is the rule every engine pays, stated rather
than corrected.

**Cash is exactly zero.** No replay is run for it: a cash sleeve holds its
starting capital by definition, and simulating that would only add noise from
nothing. The zero is written into the tables where a benchmark row belongs.

**Forced liquidation is a diagnostic, not a change to replay semantics.** The
shipped replay marks a still-open position to the final close and reports its
profit as unrealized. `forced_liquidation` answers the follow-up an operator
must ask: what would the terminal state be if that position were sold at the
final close *under this cost model*? Both terminal states are reported for
every engine, so no engine can look superior only because the final test bar
happens to favour an open position.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

import pandas as pd

from autotrader.research.costs import CostModel, Side
from autotrader.research.engines import Action, ResearchSignal
from autotrader.research.replay import ReplayResult

BUY_AND_HOLD_NAME = "BUY_AND_HOLD"


class BuyAndHoldEngine:
    """Enter long on the first observed bar, hold to the end of the data."""

    audit_ready = False

    name = BUY_AND_HOLD_NAME
    version = "benchmark-v1"
    warmup_bars = 0

    @property
    def parameters(self) -> Mapping[str, object]:
        return {"strategy": "enter on first bar, never exit"}

    def generate(self, bars: pd.DataFrame) -> Sequence[ResearchSignal]:
        if len(bars) == 0:
            return ()
        symbols = pd.unique(bars["symbol"])
        if len(symbols) != 1:
            raise ValueError(f"Buy-and-hold expects one symbol, got {len(symbols)}.")
        return (
            ResearchSignal(
                timestamp=pd.Timestamp(bars["timestamp"].iloc[0]),
                symbol=str(symbols[0]),
                action=Action.ENTER_LONG,
                reason=BUY_AND_HOLD_NAME,
                strength=1.0,
            ),
        )


def forced_liquidation(result: ReplayResult, cost_model: CostModel) -> dict[str, object]:
    """Both terminal states of one replay: as marked, and as if sold at the end.

    The forced sale uses the cost model's own fill price and fee at the final
    mark, so the diagnostic is priced under the same assumptions as every fill
    that preceded it.
    """
    native = result.final_equity
    position = result.open_position
    if position is None:
        return {
            "open_final_position": False,
            "native_final_equity": str(native),
            "forced_final_equity": str(native),
            "forced_exit_cost": "0",
            "native_total_return": float(native / result.initial_cash - 1),
            "forced_total_return": float(native / result.initial_cash - 1),
        }
    fill_price = cost_model.fill_price(position.mark_price, Side.SELL)
    fee = cost_model.fee(position.quantity, fill_price)
    proceeds = position.quantity * fill_price - fee
    forced = result.final_cash + proceeds
    return {
        "open_final_position": True,
        "open_quantity": str(position.quantity),
        "open_entry_timestamp": position.entry_timestamp.isoformat(),
        "open_unrealized_pnl": str(position.unrealized_pnl),
        "native_final_equity": str(native),
        "forced_final_equity": str(forced),
        "forced_exit_cost": str(native - forced),
        "native_total_return": float(native / result.initial_cash - 1),
        "forced_total_return": float(forced / result.initial_cash - 1),
    }


def cash_row(initial: Decimal) -> dict[str, object]:
    """The cash benchmark: its return is identically zero over any window."""
    return {
        "total_return": 0.0,
        "final_equity": str(initial),
        "max_drawdown": 0.0,
        "note": "cash holds its starting capital by definition; no replay is run",
    }


__all__ = [
    "BUY_AND_HOLD_NAME",
    "BuyAndHoldEngine",
    "cash_row",
    "forced_liquidation",
]
