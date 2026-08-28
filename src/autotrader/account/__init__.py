"""The shared account layer: what two runtimes on one brokerage account agree on.

Everything below this package is per-product. `autotrader.runtime` is the 24/7
crypto loop; `autotrader.equity` is the regular-session equity loop; each owns
its own schedule, its own universe, its own order semantics and its own process
lock, and neither is a mode of the other. That separation is deliberate and is
kept.

What they cannot own separately is the account. There is one Alpaca paper
account, so there is one exposure figure, one daily baseline, one
`client_order_id` namespace, one set of API credentials, and one answer to
whether it is safe to send an order. This package holds exactly those things
and nothing else:

`safety`  the durable account-wide halt. An ambiguous submission from either
          runtime stops **both**, across processes and across restarts, and
          only a full-universe reconciliation clears it.

`lock`    the account execution lock. The two runtime locks stay separate so
          the services run concurrently; this one serializes the short stretch
          where an order is sized against the account and sent, so two
          processes cannot both size into the same free exposure.

`budget`  the shared API budget. Two processes, one set of credentials, one
          set of counters, and a refusal rather than a delay when a window is
          exhausted.

**Nothing here trades.** No module in this package submits, cancels, or
replaces an order, and none of them contacts a broker at all. They are the
constraints an order passes through, not a path an order travels down.
"""

from autotrader.account.budget import (
    CRYPTO_EXECUTION_TRADING_CALLS,
    DEFAULT_MARKET_DATA_LIMIT,
    DEFAULT_TRADING_LIMIT,
    EQUITY_EXECUTION_TRADING_CALLS,
    EXECUTION_MARKET_DATA_CALLS,
    WINDOW_SECONDS,
    ApiBudgetError,
    ApiBudgetExceededError,
    BudgetGrant,
    current_usage,
    limit_for,
    require_api_budget,
    try_consume,
    window_start_for,
)
from autotrader.account.lock import (
    ACCOUNT_LOCK_SUFFIX,
    DEFAULT_ACCOUNT_LOCK_TIMEOUT_SECONDS,
    AccountExecutionLock,
    AccountExecutionLockError,
    account_execution_lock,
    account_lock_path_for,
)
from autotrader.account.safety import (
    ACCOUNT_UNSAFE_BANNER,
    SOURCE_CRYPTO,
    SOURCE_EQUITY,
    SOURCE_OPERATOR,
    SOURCE_RECONCILIATION,
    AccountSafetyError,
    AccountUnsafeError,
    apply_reconciliation_result,
    halt_account_for_reconciliation,
    halt_account_for_unknown,
    missing_universe_symbols,
    read_account_safety,
    require_account_safe,
)

__all__ = [
    "ACCOUNT_LOCK_SUFFIX",
    "ACCOUNT_UNSAFE_BANNER",
    "CRYPTO_EXECUTION_TRADING_CALLS",
    "DEFAULT_ACCOUNT_LOCK_TIMEOUT_SECONDS",
    "DEFAULT_MARKET_DATA_LIMIT",
    "DEFAULT_TRADING_LIMIT",
    "EQUITY_EXECUTION_TRADING_CALLS",
    "EXECUTION_MARKET_DATA_CALLS",
    "SOURCE_CRYPTO",
    "SOURCE_EQUITY",
    "SOURCE_OPERATOR",
    "SOURCE_RECONCILIATION",
    "WINDOW_SECONDS",
    "AccountExecutionLock",
    "AccountExecutionLockError",
    "AccountSafetyError",
    "AccountUnsafeError",
    "ApiBudgetError",
    "ApiBudgetExceededError",
    "BudgetGrant",
    "account_execution_lock",
    "account_lock_path_for",
    "current_usage",
    "halt_account_for_reconciliation",
    "halt_account_for_unknown",
    "limit_for",
    "missing_universe_symbols",
    "apply_reconciliation_result",
    "read_account_safety",
    "require_account_safe",
    "require_api_budget",
    "try_consume",
    "window_start_for",
]
