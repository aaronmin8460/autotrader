"""C9: the 24/7 crypto runtime and its monitoring surface.

The long-running process that operates BTC/USD and ETH/USD on completed
15-minute bars, every day of the week, on fixed UTC boundaries. It joins the
existing layers and adds no trading logic of its own: C1 supplies the bars, C2
validates them, C3 produces the signal, C5 sizes it, C6 records it, and C7 is
the only thing that ever speaks to a broker.

What this package owns:

- **Scheduling** (`schedule`) - fixed `:00/:15/:30/:45` UTC boundaries computed
  from the wall clock, a small explicit safety delay for provider lag, and the
  rule that a bar is complete only once its whole interval has elapsed.
- **Bounded fetching** (`market_data`) - one small request per symbol per
  boundary, never a re-download of history.
- **Startup safety** (`safety`) - the narrow seam Phase 8's reconciliation will
  be connected to. Until it is, the answer is `UNRESOLVED` and no order is sent.
- **Duplicate protection** (`checkpoint`) - one completed bar, one decision,
  per symbol, per process.
- **Monitoring** (`monitoring`) - a heartbeat and structured standard-library
  logging. No agent, no service, no chat integration.
- **Single-instance protection** (`lock`) - an OS file lock, because two
  runners on one database is duplicate trading.
- **The loop** (`runner`) - synchronous, signal-aware, and fail-closed in three
  directions: startup safety, the paper gates, and an ambiguous outcome.

There is no reconciliation here, no live mode, no equity symbol, no market
session, and no deployment artefact (docs/SPEC.md section 8).
"""

from autotrader.runtime.checkpoint import InMemoryCheckpoint, ProcessedBarCheckpoint
from autotrader.runtime.execution import (
    BrokerAuthenticationError,
    ExecutionGateway,
    PaperExecutionGateway,
)
from autotrader.runtime.lock import (
    LOCK_SUFFIX,
    RuntimeLock,
    RuntimeLockError,
    lock_path_for,
)
from autotrader.runtime.market_data import AlpacaCryptoBars, MarketDataSource, completed_window
from autotrader.runtime.monitoring import (
    LOGGER_NAME,
    Heartbeat,
    HeartbeatSnapshot,
    RuntimeState,
    format_event,
    get_logger,
    log_event,
)
from autotrader.runtime.runner import (
    PROCESSING_ORDER,
    RISK_SIZED_REQUEST_QUANTITY,
    RUNTIME_CONFIRMATION_TOKEN,
    RUNTIME_RUN_MODE,
    BarDataError,
    CryptoRuntime,
    CycleReport,
    CycleSeverity,
    ExecutionAuthorization,
    RuntimeConfig,
    RuntimeConfigError,
    RuntimeCycleError,
    ShutdownRequest,
    SymbolCycleResult,
    classify,
)
from autotrader.runtime.safety import (
    STARTUP_SAFETY_CODES,
    STARTUP_SAFETY_SAFE,
    STARTUP_SAFETY_UNRESOLVED,
    STARTUP_SAFETY_UNSAFE,
    StartupSafetyCheck,
    StartupSafetyResult,
    unresolved_startup_safety,
)
from autotrader.runtime.schedule import (
    BAR_INTERVAL,
    BOUNDARY_MINUTES,
    DEFAULT_LOOKBACK_BARS,
    DEFAULT_SAFETY_DELAY,
    MAX_LOOKBACK_BARS,
    MAX_SAFETY_DELAY,
    MIN_LOOKBACK_BARS,
    ScheduleError,
    effective_now,
    floor_to_boundary,
    is_bar_complete,
    is_boundary,
    latest_completed_bar_start,
    lookback_window_start,
    next_boundary,
    next_wake_time,
    require_lookback_bars,
    require_safety_delay,
    require_utc,
)

__all__ = [
    "BAR_INTERVAL",
    "BOUNDARY_MINUTES",
    "DEFAULT_LOOKBACK_BARS",
    "DEFAULT_SAFETY_DELAY",
    "LOCK_SUFFIX",
    "LOGGER_NAME",
    "MAX_LOOKBACK_BARS",
    "MAX_SAFETY_DELAY",
    "MIN_LOOKBACK_BARS",
    "PROCESSING_ORDER",
    "RISK_SIZED_REQUEST_QUANTITY",
    "RUNTIME_CONFIRMATION_TOKEN",
    "RUNTIME_RUN_MODE",
    "STARTUP_SAFETY_CODES",
    "STARTUP_SAFETY_SAFE",
    "STARTUP_SAFETY_UNRESOLVED",
    "STARTUP_SAFETY_UNSAFE",
    "AlpacaCryptoBars",
    "BarDataError",
    "BrokerAuthenticationError",
    "CryptoRuntime",
    "CycleReport",
    "CycleSeverity",
    "ExecutionAuthorization",
    "ExecutionGateway",
    "Heartbeat",
    "HeartbeatSnapshot",
    "InMemoryCheckpoint",
    "MarketDataSource",
    "PaperExecutionGateway",
    "ProcessedBarCheckpoint",
    "RuntimeConfig",
    "RuntimeConfigError",
    "RuntimeCycleError",
    "RuntimeLock",
    "RuntimeLockError",
    "RuntimeState",
    "ScheduleError",
    "ShutdownRequest",
    "StartupSafetyCheck",
    "StartupSafetyResult",
    "SymbolCycleResult",
    "classify",
    "completed_window",
    "effective_now",
    "floor_to_boundary",
    "format_event",
    "get_logger",
    "is_bar_complete",
    "is_boundary",
    "latest_completed_bar_start",
    "lock_path_for",
    "log_event",
    "lookback_window_start",
    "next_boundary",
    "next_wake_time",
    "require_lookback_bars",
    "require_safety_delay",
    "require_utc",
    "unresolved_startup_safety",
]
