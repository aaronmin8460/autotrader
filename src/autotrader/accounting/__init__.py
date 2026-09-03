"""Realized P&L accounting for the equity paper book.

**This package is accounting, and only accounting.** Nothing in it is imported
by a strategy, a risk engine, an order sizer, an execution path or a trading
halt, and nothing in it may become an input to one. It reads what the broker
confirms happened and writes down what that means; it never decides anything.
The suite asserts the direction of that dependency, because the property is
only worth having if it cannot quietly stop being true.

**It is subordinate to broker truth.** When the ledger and the broker disagree,
the ledger is what is wrong, and the answer is to say `MISMATCH` - never to
overwrite history until the numbers agree. Displaying "realized P&L unknown" is
always better than displaying a precise wrong figure.

Layers, innermost first:

- `models`   the value types; every money figure a `Decimal`
- `engine`   pure weighted-average cost basis; no I/O of any kind
- `store`    its own SQLite database, its own schema version, append-only
- `ingest`   read-only broker synchronizer; writes only accounting tables
- `reconcile` local ledger against broker positions
- `readmodel` the aggregates the dashboard reads
"""

from autotrader.accounting.models import (
    ACCOUNTING_VERSION,
    BASIS_WEIGHTED_AVERAGE,
    COMPLETENESS_CUTOVER,
    COMPLETENESS_EXACT_REPLAY,
    GRANULARITY_AGGREGATED_ORDER,
    GRANULARITY_EXECUTION,
    PROVENANCE_EQUITY_RUNTIME,
    PROVENANCE_MANUAL_OPERATOR,
    PROVENANCE_MIGRATION,
    PROVENANCE_UNKNOWN_EXTERNAL,
    SIDE_BUY,
    SIDE_SELL,
    STATUS_MISMATCH,
    STATUS_TRACKING,
    AccountingError,
    AccountingInputError,
    AppliedFill,
    CostBasisState,
    ExecutionFill,
    NegativeInventoryError,
    RealizedEvent,
    SymbolNotTrackedError,
)

__all__ = [
    "ACCOUNTING_VERSION",
    "BASIS_WEIGHTED_AVERAGE",
    "COMPLETENESS_CUTOVER",
    "COMPLETENESS_EXACT_REPLAY",
    "GRANULARITY_AGGREGATED_ORDER",
    "GRANULARITY_EXECUTION",
    "PROVENANCE_EQUITY_RUNTIME",
    "PROVENANCE_MANUAL_OPERATOR",
    "PROVENANCE_MIGRATION",
    "PROVENANCE_UNKNOWN_EXTERNAL",
    "SIDE_BUY",
    "SIDE_SELL",
    "STATUS_MISMATCH",
    "STATUS_TRACKING",
    "AccountingError",
    "AccountingInputError",
    "AppliedFill",
    "CostBasisState",
    "ExecutionFill",
    "NegativeInventoryError",
    "RealizedEvent",
    "SymbolNotTrackedError",
]
