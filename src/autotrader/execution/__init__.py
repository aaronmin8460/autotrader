"""Alpaca **paper** order execution. There is no live mode, and never will be here.

Phase 7 turns a risk-approved decision into one paper order, and stops there.
The public surface is deliberately small and deliberately one-directional:

    account + positions + current price -> RiskContext
                                        -> evaluate_risk
                                        -> RiskDecision
                                        -> OrderIntent (persisted first)
                                        -> duplicate preflight
                                        -> Alpaca PAPER market order
                                        -> broker snapshot persisted

`models` is the provider-neutral vocabulary - standard library only, no Alpaca
type anywhere in it. `paper` is the single boundary that speaks to Alpaca, and
the only place in the repository that constructs a trading client or submits an
order.

**No live trading.** The trading client is built with `paper=True` hardcoded,
there is no parameter or environment variable that can change it, and no
function here accepts one. Submission additionally requires the
`AUTOTRADER_PAPER_TRADING_ENABLED` environment gate *and* an explicit CLI
confirmation, both of which default to closed.

**No reconciliation.** Phase 7 creates the durable anchors that crash recovery
will need - the persisted intent, its `client_order_id`, and the broker
snapshot - but resolves nothing. An `UNKNOWN` outcome is recorded and left
alone, never retried. That is Phase 8 (docs/SPEC.md section 8).
"""

from autotrader.execution.models import (
    CLIENT_ORDER_ID_PREFIX,
    MAX_CLIENT_ORDER_ID_LENGTH,
    SUPPORTED_SYMBOLS,
    ExecutionError,
    ExecutionInputError,
    OrderIntent,
    OrderSide,
    new_client_order_id,
    normalize_side,
    normalize_symbol,
)
from autotrader.execution.paper import (
    CONFIRMATION_TOKEN,
    PAPER_TRADING_ENABLED_ENV,
    PAPER_TRADING_ENABLED_VALUE,
    AccountNotTradableError,
    AmbiguousSubmissionError,
    BrokerOrderSnapshot,
    BrokerRejectedOrderError,
    ConfirmationRequiredError,
    DuplicatePreflightUnavailableError,
    ExecutionOutcome,
    MarketClock,
    MissingCredentialsError,
    PaperAccountState,
    PaperExecutionResult,
    PaperPosition,
    PaperTradingDisabledError,
    ReferencePriceUnavailableError,
    SubmissionResult,
    UnsupportedBrokerStateError,
    build_market_order_request,
    build_risk_context,
    create_paper_trading_client,
    credentials_configured,
    execute_paper_order,
    fetch_market_clock,
    fetch_paper_account_state,
    fetch_paper_positions,
    fetch_reference_price,
    find_broker_order_by_client_id,
    paper_trading_enabled,
    require_confirmation,
    require_paper_trading_enabled,
    require_tradable_account,
    submit_order_intent,
)

__all__ = [
    "CLIENT_ORDER_ID_PREFIX",
    "CONFIRMATION_TOKEN",
    "MAX_CLIENT_ORDER_ID_LENGTH",
    "PAPER_TRADING_ENABLED_ENV",
    "PAPER_TRADING_ENABLED_VALUE",
    "SUPPORTED_SYMBOLS",
    "AccountNotTradableError",
    "AmbiguousSubmissionError",
    "BrokerOrderSnapshot",
    "BrokerRejectedOrderError",
    "ConfirmationRequiredError",
    "DuplicatePreflightUnavailableError",
    "ExecutionError",
    "ExecutionInputError",
    "ExecutionOutcome",
    "MarketClock",
    "MissingCredentialsError",
    "OrderIntent",
    "OrderSide",
    "PaperAccountState",
    "PaperExecutionResult",
    "PaperPosition",
    "PaperTradingDisabledError",
    "ReferencePriceUnavailableError",
    "SubmissionResult",
    "UnsupportedBrokerStateError",
    "build_market_order_request",
    "build_risk_context",
    "create_paper_trading_client",
    "credentials_configured",
    "execute_paper_order",
    "fetch_market_clock",
    "fetch_paper_account_state",
    "fetch_paper_positions",
    "fetch_reference_price",
    "find_broker_order_by_client_id",
    "new_client_order_id",
    "normalize_side",
    "normalize_symbol",
    "paper_trading_enabled",
    "require_confirmation",
    "require_paper_trading_enabled",
    "require_tradable_account",
    "submit_order_intent",
]
