"""Round-trip friction arithmetic, derived from the shipped cost models.

The completed V1-V5 study charged costs per executed side. This module answers
the question that per-side view does not answer directly: **how far does the
price have to move, in the trade's favour, before a long round trip breaks
even?**

That number is the whole subject of this research. It is derived here from
`autotrader.research.costs` rather than restated, so a change to the shipped
assumption moves this module with it and cannot silently disagree.

Derivation, long-only, quantity `q` held across one round trip:

    buy   at reference P_in   fills at  P_in * (1 + s)   and pays a fee on the fill
    sell  at reference P_out  fills at  P_out * (1 - s)  and pays a fee on the fill

    cash out = q * P_in  * (1 + s) * (1 + f)
    cash in  = q * P_out * (1 - s) * (1 - f)

Setting cash in equal to cash out and solving for the reference-price ratio:

    P_out / P_in  =  (1 + s)(1 + f) / ((1 - s)(1 - f))

so the break-even *reference* move is that ratio minus one. It is strictly
larger than the naive `2f + 2s` sum, because the fee on the exit is charged on
the exit notional rather than the entry notional. At crypto-taker rates the
naive sum is 60.00 bps and the exact figure is 60.18 bps; the gap is small here
and is kept anyway, because a break-even threshold that is biased low is a
threshold that lets uneconomic trades through.

Nothing in this module knows what a position, an order or an account is.
"""

from __future__ import annotations

from decimal import Decimal

from autotrader.research.costs import CostModel

_ONE = Decimal(1)

#: Basis points per unit fraction, for reporting only.
BPS = Decimal(10_000)


class RoundTripError(Exception):
    """A round-trip cost could not be expressed for this cost model."""


def breakeven_move(model: CostModel) -> Decimal:
    """The reference-price move a long round trip must make to break even.

    Returned as a fraction of the entry reference price. Zero for a
    frictionless model, and strictly positive for every model that charges
    anything -- there is no cost model under which trading is free, which is
    the property that makes this usable as a gate.
    """
    f = model.fee_rate
    s = model.slippage_rate
    denominator = (_ONE - s) * (_ONE - f)
    if denominator <= 0:
        raise RoundTripError(
            f"Cost model {model.label!r} charges {f} fee and {s} slippage, which "
            "consumes the entire notional; no break-even move exists."
        )
    return ((_ONE + s) * (_ONE + f)) / denominator - _ONE


def breakeven_move_bps(model: CostModel) -> Decimal:
    """`breakeven_move` in basis points, for tables and reports."""
    return breakeven_move(model) * BPS


def naive_round_trip(model: CostModel) -> Decimal:
    """The `2f + 2s` figure, kept only to show how it differs from the exact one.

    This is what a per-side cost table invites a reader to sum, and it is
    slightly optimistic. Reported beside the exact figure so the difference is
    visible rather than argued about.
    """
    return 2 * (model.fee_rate + model.slippage_rate)


def round_trip_cost_fraction(
    model: CostModel,
    entry_reference: Decimal,
    exit_reference: Decimal,
) -> Decimal:
    """Actual round-trip friction on one trade, as a fraction of entry notional.

    Unlike `breakeven_move` this is measured after the fact on a trade that
    happened, so it depends on where the price ended up: the exit fee is
    charged on the exit notional, and a trade that ran up pays more than a
    trade that ran down. Used to check the diagnostic's ledger arithmetic
    against the study's own recorded fees.
    """
    if entry_reference <= 0 or exit_reference <= 0:
        raise RoundTripError("Reference prices must be positive.")
    f = model.fee_rate
    s = model.slippage_rate
    entry_fill = entry_reference * (_ONE + s)
    exit_fill = exit_reference * (_ONE - s)
    fees = (entry_fill + exit_fill) * f
    slippage = (entry_fill - entry_reference) + (exit_reference - exit_fill)
    return (fees + slippage) / entry_reference


__all__ = [
    "BPS",
    "RoundTripError",
    "breakeven_move",
    "breakeven_move_bps",
    "naive_round_trip",
    "round_trip_cost_fraction",
]
