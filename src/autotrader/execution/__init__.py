"""Alpaca **paper** crypto order execution. There is no live mode, and never will be here.

C7 turns a risk-approved decision into one paper order, and stops there. The
public surface is deliberately small and deliberately one-directional:

    account + positions + asset metadata + current price -> RiskContext
                                                         -> evaluate_risk
                                                         -> RiskDecision
                                                         -> broker-increment
                                                            normalization
                                                         -> OrderIntent
                                                            (persisted first)
                                                         -> duplicate preflight
                                                         -> Alpaca PAPER market
                                                            order (GTC)
                                                         -> broker snapshot
                                                            persisted

`models` is the provider-neutral vocabulary - standard library only, no Alpaca
type anywhere in it. `paper` is the single boundary that speaks to Alpaca, and
the only place in the repository that constructs a trading client or submits an
order.

**No live trading.** The trading client is built with `paper=True` hardcoded,
there is no parameter or environment variable that can change it, and no
function here accepts one. Submission additionally requires the
`AUTOTRADER_PAPER_TRADING_ENABLED` environment gate *and* an explicit CLI
confirmation, both of which default to closed.

**No market clock.** Crypto trades continuously, so nothing here reads an
equity session's open/closed state or gates on one.

**No reconciliation here.** C7 creates the durable anchors crash recovery
needs - the persisted intent, its `client_order_id`, and the broker snapshot -
and resolves nothing. An `UNKNOWN` outcome is recorded and left alone, never
retried. `autotrader.reconciliation` resolves it, reading the broker through
this package's read-only helpers so the broker boundary stays one file
(docs/SPEC.md section 8, C8).
"""

from autotrader.execution.models import (
    CLIENT_ORDER_ID_PREFIX,
    MAX_CLIENT_ORDER_ID_LENGTH,
    SUPPORTED_SYMBOLS,
    ExecutionError,
    ExecutionInputError,
    OrderIntent,
    OrderSide,
    format_quantity,
    new_client_order_id,
    normalize_side,
    normalize_symbol,
    parse_quantity,
    require_quantity,
)
from autotrader.execution.paper import (
    CONFIRMATION_TOKEN,
    ORDER_TIME_IN_FORCE,
    PAPER_TRADING_BASE_URL,
    PAPER_TRADING_ENABLED_ENV,
    PAPER_TRADING_ENABLED_VALUE,
    REFERENCE_PRICE_FEED,
    AccountNotTradableError,
    AmbiguousSubmissionError,
    AssetNotTradableError,
    BrokerOrderSnapshot,
    BrokerRejectedOrderError,
    ConfirmationRequiredError,
    CryptoAssetSpec,
    DuplicatePreflightUnavailableError,
    ExecutionOutcome,
    MissingCredentialsError,
    NotPaperEnvironmentError,
    PaperAccountState,
    PaperExecutionResult,
    PaperPosition,
    PaperTradingDisabledError,
    QuantityBelowMinimumError,
    ReferencePriceUnavailableError,
    SubmissionResult,
    UnsupportedBrokerStateError,
    broker_symbol_key,
    build_market_order_request,
    build_risk_context,
    create_paper_trading_client,
    credentials_configured,
    execute_paper_order,
    fetch_crypto_asset,
    fetch_paper_account_state,
    fetch_paper_positions,
    fetch_reference_price,
    find_broker_order_by_client_id,
    normalize_broker_quantity,
    paper_trading_enabled,
    require_confirmation,
    require_paper_trading_enabled,
    require_tradable_account,
    resolve_daily_baseline_equity,
    submit_order_intent,
    to_wire_quantity,
    verify_paper_environment,
)

__all__ = [
    "CLIENT_ORDER_ID_PREFIX",
    "CONFIRMATION_TOKEN",
    "MAX_CLIENT_ORDER_ID_LENGTH",
    "ORDER_TIME_IN_FORCE",
    "PAPER_TRADING_BASE_URL",
    "PAPER_TRADING_ENABLED_ENV",
    "PAPER_TRADING_ENABLED_VALUE",
    "REFERENCE_PRICE_FEED",
    "SUPPORTED_SYMBOLS",
    "AccountNotTradableError",
    "AmbiguousSubmissionError",
    "AssetNotTradableError",
    "BrokerOrderSnapshot",
    "BrokerRejectedOrderError",
    "ConfirmationRequiredError",
    "CryptoAssetSpec",
    "DuplicatePreflightUnavailableError",
    "ExecutionError",
    "ExecutionInputError",
    "ExecutionOutcome",
    "MissingCredentialsError",
    "NotPaperEnvironmentError",
    "OrderIntent",
    "OrderSide",
    "PaperAccountState",
    "PaperExecutionResult",
    "PaperPosition",
    "PaperTradingDisabledError",
    "QuantityBelowMinimumError",
    "ReferencePriceUnavailableError",
    "SubmissionResult",
    "UnsupportedBrokerStateError",
    "broker_symbol_key",
    "build_market_order_request",
    "build_risk_context",
    "create_paper_trading_client",
    "credentials_configured",
    "execute_paper_order",
    "fetch_crypto_asset",
    "fetch_paper_account_state",
    "fetch_paper_positions",
    "fetch_reference_price",
    "find_broker_order_by_client_id",
    "format_quantity",
    "new_client_order_id",
    "normalize_broker_quantity",
    "normalize_side",
    "normalize_symbol",
    "paper_trading_enabled",
    "parse_quantity",
    "require_confirmation",
    "require_paper_trading_enabled",
    "require_quantity",
    "require_tradable_account",
    "resolve_daily_baseline_equity",
    "submit_order_intent",
    "to_wire_quantity",
    "verify_paper_environment",
]
