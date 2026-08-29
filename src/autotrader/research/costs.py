"""Transaction cost assumptions, stated explicitly and per asset class.

A backtest that ignores costs is a random number generator with a plausible
shape. This module holds the two costs a research replay charges, both as exact
`Decimal` fractions of notional:

``fee_rate``       what the venue takes, per executed side.
``slippage_rate``  how far the fill is from the reference price, adversely.

**Slippage is adverse by construction.** A BUY fills *above* the bar's open and
a SELL fills *below* it. There is no configuration that makes slippage helpful,
because a model in which trading sometimes pays you is a model that will
eventually be used to justify a strategy.

**These are assumptions, not fee schedules.** The real venue's crypto fees
depend on trailing volume tiers, equity commissions are zero but the spread is
not, and both change. None of that is implemented here and none of it is
billing logic. The point of naming a cost model is that a result carries the
assumption that produced it, so two studies can be compared only when they
agree on what trading cost - and `ZERO_COST` exists so the cost-free result can
be computed on purpose and labelled as such, rather than arrived at by
forgetting.

The 24/7 and the session-bound markets get different defaults because their
microstructure differs, not because the arithmetic does: the same code charges
both, and the only thing that varies is the two numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

#: Rates are fractions of notional. A rate at or above this is refused: a
#: hundred-percent cost is certainly a units mistake (basis points typed as a
#: fraction), and silently accepting it would produce a plausible-looking
#: equity curve that means nothing.
MAX_RATE = Decimal("0.5")

_ZERO = Decimal(0)
_ONE = Decimal(1)


class CostInputError(Exception):
    """A cost model was configured with something that cannot be a cost."""


class Side(Enum):
    """The market side a cost is being charged on."""

    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class CostModel:
    """One named set of transaction cost assumptions.

    `label` is carried into every experiment record, so a metric is never
    reported without the cost assumption that produced it.
    """

    label: str
    fee_rate: Decimal
    slippage_rate: Decimal

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise CostInputError("A cost model must be labelled.")
        for name, rate in (("fee_rate", self.fee_rate), ("slippage_rate", self.slippage_rate)):
            if not isinstance(rate, Decimal):
                raise CostInputError(f"{name} must be a Decimal, got {type(rate).__name__}.")
            if not rate.is_finite():
                raise CostInputError(f"{name} must be finite, got {rate}.")
            if rate < 0:
                raise CostInputError(f"{name} must not be negative, got {rate}.")
            if rate >= MAX_RATE:
                raise CostInputError(
                    f"{name} of {rate} is at or above {MAX_RATE}; that is almost certainly "
                    "basis points written as a fraction rather than a cost."
                )

    @property
    def frictionless(self) -> bool:
        """True when this model charges nothing at all."""
        return self.fee_rate == 0 and self.slippage_rate == 0

    def fill_price(self, reference_price: Decimal, side: Side) -> Decimal:
        """The price a fill actually happens at, given the bar's reference price.

        A BUY crosses the spread upwards and a SELL downwards, so the direction
        of the adjustment is decided by the side rather than supplied by the
        caller. With `slippage_rate` at zero this returns the reference price
        unchanged, which is what makes a zero-slippage study exactly comparable
        to the production backtester.
        """
        if reference_price <= 0:
            raise CostInputError(f"reference_price must be positive, got {reference_price}.")
        if side is Side.BUY:
            return reference_price * (_ONE + self.slippage_rate)
        return reference_price * (_ONE - self.slippage_rate)

    def fee(self, quantity: Decimal, fill_price: Decimal) -> Decimal:
        """The venue fee on one executed side. Always positive or zero."""
        if quantity < 0:
            raise CostInputError(f"quantity must not be negative, got {quantity}.")
        return quantity * fill_price * self.fee_rate

    def slippage_cost(self, quantity: Decimal, reference_price: Decimal, side: Side) -> Decimal:
        """What crossing the spread cost, relative to the reference price.

        Reported separately from the fee so a study can say how much of its
        drag was the venue and how much was the assumption about liquidity.
        Always positive or zero, on both sides.
        """
        fill = self.fill_price(reference_price, side)
        return abs(fill - reference_price) * quantity

    def buy_cost(self, quantity: Decimal, fill_price: Decimal) -> Decimal:
        """Total cash a BUY consumes: notional at the fill price, plus its fee."""
        return quantity * fill_price + self.fee(quantity, fill_price)

    def sell_proceeds(self, quantity: Decimal, fill_price: Decimal) -> Decimal:
        """Cash a SELL returns: notional at the fill price, less its fee."""
        return quantity * fill_price - self.fee(quantity, fill_price)

    def to_json_dict(self) -> dict[str, object]:
        """The JSON form recorded with every experiment."""
        return {
            "label": self.label,
            "fee_rate": str(self.fee_rate),
            "slippage_rate": str(self.slippage_rate),
        }


#: The 24/7 crypto assumption: a flat taker fee on both sides, plus five basis
#: points of adverse slippage. The fee matches the production backtester's
#: `TAKER_FEE_RATE`, so a crypto research replay with `CRYPTO_COST` and zero
#: slippage reproduces that engine's cash arithmetic exactly - which is the
#: property a test pins, and the reason the number is not re-tuned here.
CRYPTO_COST = CostModel(
    label="crypto-taker",
    fee_rate=Decimal("0.0025"),
    slippage_rate=Decimal("0.0005"),
)

#: The US equity assumption: no commission, two basis points of adverse
#: slippage. Zero commission is the retail reality at this venue and is not a
#: claim that the trade was free - the cost moved into the spread, which is
#: what the slippage term is for.
EQUITY_COST = CostModel(
    label="equity-marketable",
    fee_rate=Decimal("0"),
    slippage_rate=Decimal("0.0002"),
)

#: Costs switched off, on purpose and under a name. Useful for isolating how
#: much of a result is the signal and how much is the cost assumption; a study
#: reported under this label is reporting an upper bound, not a result.
ZERO_COST = CostModel(
    label="frictionless",
    fee_rate=Decimal("0"),
    slippage_rate=Decimal("0"),
)

#: A deliberately punitive model, for asking whether an edge survives being
#: wrong about costs by a wide margin.
STRESS_COST = CostModel(
    label="stress",
    fee_rate=Decimal("0.005"),
    slippage_rate=Decimal("0.002"),
)

#: Cost models addressable by name from a CLI or a study configuration.
COST_MODELS: dict[str, CostModel] = {
    model.label: model for model in (CRYPTO_COST, EQUITY_COST, ZERO_COST, STRESS_COST)
}


def cost_model_for(label: str) -> CostModel:
    """Look up a named cost model, listing the alternatives when it is unknown."""
    try:
        return COST_MODELS[label]
    except KeyError:
        known = ", ".join(sorted(COST_MODELS))
        raise CostInputError(f"Unknown cost model {label!r}. Known models: {known}.") from None


__all__ = [
    "COST_MODELS",
    "CRYPTO_COST",
    "EQUITY_COST",
    "MAX_RATE",
    "STRESS_COST",
    "ZERO_COST",
    "CostInputError",
    "CostModel",
    "Side",
    "cost_model_for",
]
