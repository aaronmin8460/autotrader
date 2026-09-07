"""Real-money performance accounting: what the account earned, net of what was paid in.

**Accounting, and only accounting.** Nothing in this package is imported by a
strategy, an allocator, a risk engine, an execution path or a trading halt, and
nothing in it may become an input to one. It reads what the broker confirms and
writes down what that means; it decides nothing. The suite asserts the direction
of that dependency, because the property is only worth having if it cannot
quietly stop being true.

**Withdrawals are OBSERVE_ONLY.** This package contains no ACH call, no wire
call, no transfer client, no scheduler and no withdrawal endpoint - and the
broker's Trading API, the only surface this repository can reach, publishes no
transfer endpoint to call even if one were written. The Profit Reserve and the
Withdrawal Bucket are ledger lines. Every dollar stays in the broker account.

**The Profit Reserve is not the EDA-1 cash reserve.** That one is trading
liquidity, lives in `autotrader.equity.allocation`, and changes position sizing.
This one is a notional earmark of newly created high-water-mark profit, and it
changes nothing at all.

The one invariant everything here serves:

    money moving in is not profit; money moving out is not loss.

Layers, innermost first:

- `models`      the value types and the vocabulary; every money figure a Decimal
- `engine`      pure arithmetic - flow adjustment, TWR, high-water mark, reserve
- `classify`    broker activity to flow category, failing closed on anything unclear
- `store`       its own SQLite database, its own schema version, account-scoped
- `ingest`      read-only broker activity import, idempotent by construction
- `service`     inception, checkpoints, and deterministic replay
- `readmodel`   the frozen contract payload the dashboard reads
"""

from autotrader.liveaccounting.models import (
    ENVIRONMENT_LIVE,
    PROFIT_RESERVE_OBSERVE_V1,
    STATUS_CLEAN,
    WITHDRAWAL_MODE_OBSERVE_ONLY,
    EquityCheckpoint,
    ExternalFlow,
    LiveAccountingError,
    ProfitReservePolicy,
)

__all__ = [
    "ENVIRONMENT_LIVE",
    "PROFIT_RESERVE_OBSERVE_V1",
    "STATUS_CLEAN",
    "WITHDRAWAL_MODE_OBSERVE_ONLY",
    "EquityCheckpoint",
    "ExternalFlow",
    "LiveAccountingError",
    "ProfitReservePolicy",
]
