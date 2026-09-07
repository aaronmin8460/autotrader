"""Deterministic risk decisions for a proposed trade. No orders, no broker.

C5 provides `engine.evaluate_risk`, which answers whether a proposed trade may
proceed and at what fractional `Decimal` quantity under the V0.2 limits: 5%
per symbol, 30% total exposure, a 2% daily-loss halt on new entries, long only,
no leverage. Every limit is a USD **notional** ceiling; only the quantity
representation changed when the project pivoted to crypto.

A policy may additionally carry an absolute USD ceiling - `max_position_notional`
and `max_total_notional` - which binds beside the fractional one, tighter of the
two winning. It exists for a small-capital operational validation, whose promise
is a number of dollars rather than a share of the account; a percentage-only
ceiling would authorize a larger position every day the account gained.

It is the stage between a signal and an order intent (docs/SPEC.md section
6A), and it deliberately cannot reach past itself: nothing here submits an
order, constructs a broker client, touches the network, or persists anything.
Risk limits gate *entries* only - an exit that reduces an existing long is
never blocked, because a kill switch must not trap an open position.
"""

from autotrader.risk.engine import (
    APPROVED,
    DAILY_LOSS_LIMIT,
    DEFAULT_POLICY,
    EXIT_QUANTITY_EXCEEDS_POSITION,
    INSUFFICIENT_CASH,
    INVALID_REQUEST,
    MAX_DAILY_LOSS_FRACTION,
    MAX_POSITION_FRACTION,
    MAX_TOTAL_EXPOSURE_FRACTION,
    NO_ABSOLUTE_CEILING,
    NO_POSITION_TO_EXIT,
    POSITION_LIMIT,
    QUANTITY_EXPONENT,
    REASON_CODES,
    TOTAL_EXPOSURE_LIMIT,
    TRADING_DISABLED,
    RiskContext,
    RiskDecision,
    RiskInputError,
    RiskPolicy,
    RiskRequest,
    RiskSide,
    evaluate_risk,
    format_quantity,
    normalize_quantity,
)

__all__ = [
    "APPROVED",
    "DAILY_LOSS_LIMIT",
    "DEFAULT_POLICY",
    "EXIT_QUANTITY_EXCEEDS_POSITION",
    "INSUFFICIENT_CASH",
    "INVALID_REQUEST",
    "MAX_DAILY_LOSS_FRACTION",
    "MAX_POSITION_FRACTION",
    "MAX_TOTAL_EXPOSURE_FRACTION",
    "NO_ABSOLUTE_CEILING",
    "NO_POSITION_TO_EXIT",
    "POSITION_LIMIT",
    "QUANTITY_EXPONENT",
    "REASON_CODES",
    "TOTAL_EXPOSURE_LIMIT",
    "TRADING_DISABLED",
    "RiskContext",
    "RiskDecision",
    "RiskInputError",
    "RiskPolicy",
    "RiskRequest",
    "RiskSide",
    "evaluate_risk",
    "format_quantity",
    "normalize_quantity",
]
