"""Venue execution facts and exact cost arithmetic.

Fee schedule re-verified against the venue's primary crypto-fee
documentation on 2026-08-31 (tier-1: maker 15 bps, taker 25 bps; fees are
charged in the asset received). Order semantics re-verified the same day:
order types ``market``/``limit``/``stop_limit``; time in force ``gtc`` and
``ioc`` only; **no post-only flag exists** — a crossing limit silently
executes as taker; $10 minimum notional; price/quantity increments below.

These are recorded venue facts, not assumptions; the shipped research
assumption they replace is the 25 bps + 5 bps ``crypto-taker`` model whose
exact round-trip break-even is 60.18 bps.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Tier-1 fee rates (fraction of notional per executed side), 30-day volume
#: under $100k — the tier this system's scale actually occupies.
MAKER_FEE = 0.0015
TAKER_FEE = 0.0025

#: The venue enforces a $10 minimum cost basis per order on USD pairs.
MINIMUM_NOTIONAL_USD = 10.0


@dataclass(frozen=True)
class SymbolSpec:
    symbol: str
    price_increment: float
    quantity_increment: float


SYMBOLS: dict[str, SymbolSpec] = {
    "BTC/USD": SymbolSpec("BTC/USD", price_increment=1.0, quantity_increment=0.0001),
    "ETH/USD": SymbolSpec("ETH/USD", price_increment=0.1, quantity_increment=0.001),
}


def round_trip_break_even(fee_in: float, slip_in: float, fee_out: float, slip_out: float) -> float:
    """Exact round-trip break-even move as a fraction.

    ``cash out = q * P_in * (1 + slip_in) * (1 + fee_in)`` and
    ``cash in = q * P_out * (1 - slip_out) * (1 - fee_out)``; the break-even
    price ratio follows. This is the same derivation the cost-aware study
    fixed, never restated as a constant that could drift.
    """
    return (1.0 + slip_in) * (1.0 + fee_in) / ((1.0 - slip_out) * (1.0 - fee_out)) - 1.0


def taker_baseline_break_even() -> float:
    """The shipped ``crypto-taker`` round trip: 25 bps fee + 5 bps slip/side."""
    return round_trip_break_even(0.0025, 0.0005, 0.0025, 0.0005)


def maker_fee_only_break_even() -> float:
    """Both sides maker at tier 1, zero residual — the fee-only floor."""
    return round_trip_break_even(MAKER_FEE, 0.0, MAKER_FEE, 0.0)
