"""Research-only passive-fill simulator.

Evaluates a hypothetical resting BUY or SELL limit against the venue's own
historical tape without contacting any order endpoint. Queue position on
this venue is unknowable from historical data (L1 only), so fills are
governed by the predeclared scenario bracket:

- OPTIMISTIC: a trade printing at or through the limit fills the full
  remaining quantity (classic optimistic backtest bound).
- BASE: only trades printing strictly *through* the limit fill, capped by
  the printed size (price priority guarantees a resting order at the level
  executes before the tape can print beyond it).
- CONSERVATIVE: strictly-through, with only half the printed size credited
  (other resting orders share the prints), and a longer activation latency.

The forbidden simplification — assuming every resting limit fills at its
price — is excluded by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import pandas as pd

#: Terminal outcomes (mandate minimum set).
FULLY_FILLED = "FULLY_FILLED"
PARTIALLY_FILLED = "PARTIALLY_FILLED"
NOT_FILLED = "NOT_FILLED"
PRICE_MOVED_AWAY = "PRICE_MOVED_AWAY"
TIMED_OUT = "TIMED_OUT"

#: Zero-fill classification: the market has "moved away" when the mid at
#: cancel sits at least this far beyond the limit, against the fill.
MOVED_AWAY_BPS = 25.0


@dataclass(frozen=True)
class Scenario:
    name: str
    latency_s: float
    fill_rule: str  # "at_touch" | "strict_through"
    size_cap_fraction: float | None  # None = uncapped


SCENARIOS: dict[str, Scenario] = {
    "OPTIMISTIC": Scenario("OPTIMISTIC", 0.0, "at_touch", None),
    "BASE": Scenario("BASE", 5.0, "strict_through", 1.0),
    "CONSERVATIVE": Scenario("CONSERVATIVE", 10.0, "strict_through", 0.5),
}


@dataclass(frozen=True)
class Policy:
    name: str
    max_wait_s: float
    price_improve_ticks: int
    taker_fallback: bool


POLICIES: dict[str, Policy] = {
    "P0_JOIN": Policy("P0_JOIN", 1800.0, 0, False),
    "P1_IMPROVE": Policy("P1_IMPROVE", 1800.0, 1, False),
    "P2_SHORT": Policy("P2_SHORT", 300.0, 0, False),
    "P3_FALLBACK": Policy("P3_FALLBACK", 1800.0, 0, True),
    # Extension arm (journal 2026-08-31): a 4h rest anchored to the 4-8h
    # economic-horizon band, then taker fallback. Single fixed wait value.
    "P4_LONG": Policy("P4_LONG", 14_400.0, 0, True),
}


@dataclass(frozen=True)
class Fill:
    timestamp: pd.Timestamp
    quantity: float


@dataclass(frozen=True)
class SimulationResult:
    outcome: str
    limit_price: float
    requested_quantity: float
    filled_quantity: float
    fills: tuple[Fill, ...] = field(default_factory=tuple)
    first_fill_ts: pd.Timestamp | None = None
    full_fill_ts: pd.Timestamp | None = None
    wait_to_first_fill_s: float | None = None
    wait_to_full_fill_s: float | None = None

    @property
    def remaining_quantity(self) -> float:
        return self.requested_quantity - self.filled_quantity

    @property
    def fill_fraction(self) -> float:
        if self.requested_quantity <= 0:
            return 0.0
        return self.filled_quantity / self.requested_quantity


def quote_at_or_before(quotes: pd.DataFrame, ts: pd.Timestamp) -> pd.Series | None:
    """The prevailing L1 quote at `ts`, or None if none exists yet."""
    if quotes is None or len(quotes) == 0:
        return None
    eligible = quotes[quotes["t"] <= ts]
    if eligible.empty:
        return None
    return eligible.iloc[-1]


def mid_of(quote: pd.Series | None) -> float | None:
    if quote is None:
        return None
    bid = float(quote["bid_price"])
    ask = float(quote["ask_price"])
    if bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2.0


def spread_bps_of(quote: pd.Series | None) -> float | None:
    if quote is None:
        return None
    bid = float(quote["bid_price"])
    ask = float(quote["ask_price"])
    mid = mid_of(quote)
    if mid is None or mid <= 0:
        return None
    return (ask - bid) / mid * 1e4


def limit_price_for(
    side: str, quote: pd.Series, price_increment: float, improve_ticks: int
) -> float:
    """The policy's resting price: join the same-side quote, optionally
    improving by ticks while staying strictly non-marketable."""
    bid = float(quote["bid_price"])
    ask = float(quote["ask_price"])
    if side == "buy":
        price = bid + improve_ticks * price_increment
        if price >= ask:  # would cross or lock: fall back to joining
            price = bid
        return price
    price = ask - improve_ticks * price_increment
    if price <= bid:
        price = ask
    return price


def simulate_limit(
    *,
    trades: pd.DataFrame,
    quotes: pd.DataFrame,
    side: str,
    limit_price: float,
    quantity: float,
    active_from: pd.Timestamp,
    cancel_at: pd.Timestamp,
    scenario: Scenario,
) -> SimulationResult:
    """Walk the tape between activation and cancel under the scenario's rule."""
    if side not in ("buy", "sell"):
        raise ValueError(f"unknown side {side}")
    fills: list[Fill] = []
    filled = 0.0
    if trades is not None and len(trades):
        window = trades[(trades["t"] >= active_from) & (trades["t"] <= cancel_at)]
        for row in window.itertuples(index=False):
            price = float(row.price)
            if scenario.fill_rule == "at_touch":
                crossed = price <= limit_price if side == "buy" else price >= limit_price
                if crossed:
                    fills.append(Fill(timestamp=row.t, quantity=quantity - filled))
                    filled = quantity
                    break
            else:
                through = price < limit_price if side == "buy" else price > limit_price
                if not through:
                    continue
                credit = float(row.size)
                if scenario.size_cap_fraction is not None:
                    credit *= scenario.size_cap_fraction
                take = min(quantity - filled, credit)
                if take <= 0:
                    continue
                fills.append(Fill(timestamp=row.t, quantity=take))
                filled += take
                if filled >= quantity:
                    break

    first_ts = fills[0].timestamp if fills else None
    full_ts = fills[-1].timestamp if fills and filled >= quantity else None
    result_common = {
        "limit_price": limit_price,
        "requested_quantity": quantity,
        "filled_quantity": filled,
        "fills": tuple(fills),
        "first_fill_ts": first_ts,
        "full_fill_ts": full_ts,
        "wait_to_first_fill_s": (
            (first_ts - active_from).total_seconds() if first_ts is not None else None
        ),
        "wait_to_full_fill_s": (
            (full_ts - active_from).total_seconds() if full_ts is not None else None
        ),
    }
    if filled >= quantity:
        return SimulationResult(outcome=FULLY_FILLED, **result_common)
    if filled > 0:
        return SimulationResult(outcome=PARTIALLY_FILLED, **result_common)
    return SimulationResult(
        outcome=_zero_fill_outcome(quotes, side, limit_price, cancel_at), **result_common
    )


def _zero_fill_outcome(
    quotes: pd.DataFrame, side: str, limit_price: float, cancel_at: pd.Timestamp
) -> str:
    quote = quote_at_or_before(quotes, cancel_at)
    mid = mid_of(quote)
    if mid is None:
        return NOT_FILLED
    away_bps = (
        (mid - limit_price) / limit_price * 1e4
        if side == "buy"
        else (limit_price - mid) / limit_price * 1e4
    )
    if away_bps >= MOVED_AWAY_BPS:
        return PRICE_MOVED_AWAY
    return TIMED_OUT


def activation_and_cancel(
    decision_ts: pd.Timestamp, policy: Policy, scenario: Scenario
) -> tuple[pd.Timestamp, pd.Timestamp]:
    active_from = decision_ts + timedelta(seconds=scenario.latency_s)
    cancel_at = decision_ts + timedelta(seconds=scenario.latency_s + policy.max_wait_s)
    return active_from, cancel_at
