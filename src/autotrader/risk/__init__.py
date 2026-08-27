"""Deterministic risk decisions for a proposed trade. No orders, no broker.

C5 provides `engine.evaluate_risk`, which answers whether a proposed trade may
proceed and at what fractional `Decimal` quantity under the V0.2 limits: 5%
per symbol, 30% total exposure, a 2% daily-loss halt on new entries, long only,
no leverage. Every limit is a USD **notional** ceiling; only the quantity
representation changed when the project pivoted to crypto.

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
