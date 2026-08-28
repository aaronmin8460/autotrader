"""C10: the read-only operations dashboard.

One page, one question at a time:

    is the system healthy, is reconciliation clean, is trading allowed, what
    is held, what happened recently, how much risk is used, are the runtimes
    and checkpoints current, and does anything need a person?

**Read-only, structurally.** There is no `POST`, `PUT`, `PATCH`, or `DELETE`
route anywhere in this package, so there is nothing a browser can send that
places an order, cancels one, moves a risk limit, starts or stops the runtime,
edits a row, or triggers a reconciliation repair. The database is opened with
SQLite's `mode=ro` URI and `PRAGMA query_only`, so even a mistake in this
package's own code is refused by the engine. The order-submission entry points
are never imported, and `tests/test_dashboard.py` asserts both the route table
and the import audit. Hiding a control would have left the capability; there is
no capability.

**It owns no state.** No dashboard database, no cache of trading state, no new
table, and no migration. Every figure is derived from the existing schema v5
tables through `autotrader.state`'s own read helpers, or read live from the
broker through `autotrader.execution.paper`'s read-only helpers. Nothing here
is a second source of truth.

**It does not interfere.** Reads run in one short deferred transaction, which
in WAL mode takes no lock a writer waits on, and no journal-mode pragma is
issued against a database the trading runtime owns. A busy or missing database
is reported as unreadable within a couple of seconds rather than waited on.

**It does not invent numbers.** A figure this system cannot truthfully read is
`Amount.unavailable(...)` with a reason code, all the way to the browser. There
is no placeholder equity, no sample position, no fabricated equity curve, and
no chart - `data/autotrader.db` persists no equity time series, and a beautiful
graph of numbers nobody recorded is a lie with axes on it.

Module map:

- `models`  the wire vocabulary; standard library only
- `broker`  the read-only paper-account boundary
- `service` the read model: one database read, one broker read, one `Overview`
- `api`     six GET routes
- `__main__` a loopback-bound development server

Deployment - authentication, TLS, a reverse proxy, a supervised process - is
not here and is not implied by anything here. See the README.
"""

from autotrader.dashboard.api import (
    ALLOWED_METHODS,
    DATABASE_PATH_ENV,
    DEFAULT_HOST,
    DEFAULT_PORT,
    app,
    create_app,
    database_path,
)
from autotrader.dashboard.broker import (
    BrokerRead,
    ReadableBroker,
    SharedBrokerReader,
    read_broker,
)
from autotrader.dashboard.models import (
    ASSET_CLASS_CRYPTO,
    ASSET_CLASS_EQUITY,
    ENVIRONMENT_PAPER,
    SOURCE_BROKER,
    SOURCE_LOCAL,
    SOURCE_UNAVAILABLE,
    SYSTEM_ATTENTION,
    SYSTEM_HEALTHY,
    SYSTEM_PAUSED,
    UNAVAILABLE_BROKER_NOT_CONFIGURED,
    UNAVAILABLE_BROKER_UNREADABLE,
    UNAVAILABLE_DATABASE_UNREADABLE,
    UNAVAILABLE_NOT_RECORDED,
    Amount,
    CheckpointRow,
    HealthComponent,
    OrderRow,
    OrdersPanel,
    Overview,
    PositionRow,
    PositionsPanel,
    PrimaryMetrics,
    ReconciliationPanel,
    RiskLimit,
    RiskPanel,
    RuntimePanel,
)
from autotrader.dashboard.service import (
    DEFAULT_ORDER_LIMIT,
    READ_TIMEOUT_SECONDS,
    STALE_AFTER,
    StateSnapshot,
    asset_class_for,
    build_overview,
    read_only_connection,
    read_state,
)

__all__ = [
    "ALLOWED_METHODS",
    "ASSET_CLASS_CRYPTO",
    "ASSET_CLASS_EQUITY",
    "DATABASE_PATH_ENV",
    "DEFAULT_HOST",
    "DEFAULT_ORDER_LIMIT",
    "DEFAULT_PORT",
    "ENVIRONMENT_PAPER",
    "READ_TIMEOUT_SECONDS",
    "SOURCE_BROKER",
    "SOURCE_LOCAL",
    "SOURCE_UNAVAILABLE",
    "STALE_AFTER",
    "SYSTEM_ATTENTION",
    "SYSTEM_HEALTHY",
    "SYSTEM_PAUSED",
    "UNAVAILABLE_BROKER_NOT_CONFIGURED",
    "UNAVAILABLE_BROKER_UNREADABLE",
    "UNAVAILABLE_DATABASE_UNREADABLE",
    "UNAVAILABLE_NOT_RECORDED",
    "Amount",
    "BrokerRead",
    "CheckpointRow",
    "HealthComponent",
    "OrderRow",
    "OrdersPanel",
    "Overview",
    "PositionRow",
    "PositionsPanel",
    "PrimaryMetrics",
    "ReadableBroker",
    "ReconciliationPanel",
    "RiskLimit",
    "RiskPanel",
    "RuntimePanel",
    "SharedBrokerReader",
    "StateSnapshot",
    "app",
    "asset_class_for",
    "build_overview",
    "create_app",
    "database_path",
    "read_broker",
    "read_only_connection",
    "read_state",
]
