"""EDA-1 on the paper broker: the execution adapter around the validated champion.

The research champion is `autotrader.equity.regime`, and it is observation-only
*by construction* - no execution seam, source-pinned against importing the
execution layer. That is its safety property and this module does not touch it.
What this module adds is everything **around** it:

    market data
        -> the exact validated EDA-1 decision (regime overlay over V3)
        -> Shadow/Paper parity check for the same bar
        -> frozen shared-account allocator  (autotrader.equity.allocation)
        -> Risk Engine
        -> desired-versus-broker-actual delta
        -> durable OrderIntent
        -> execution boundary
        -> the paper broker

**The decision half is the Shadow's, not a re-derivation.** V3 is evaluated by
the same `EnginePanel`, EDA-1 is derived by the same
`SideBySideShadowRecorder`, and the same replay-verification runs on every bar:
the stored challenger series must equal the overlay replay of the stored V3
series, or the process stops. On top of that, every bar's EDA-1 decision is
compared against the *independently computed* Shadow row for the same bar, and
a symbol whose two answers disagree is excluded from mutation for that bar.
Two processes, two databases, two computations, one answer required.

**Target state, not a signal stream.** EDA-1 is a target-position architecture:
while PARTICIPATE the target is "long", every bar, for five years. A runtime
that translated that into a BUY every fifteen minutes would be wrong about the
strategy and ruinous at the broker. So the runtime computes a *desired share
count* from the frozen allocator and subtracts what the broker says it actually
holds. Equal means no order. That is the normal case and it is silent.

**The allocator is not a risk bypass.** It computes what EDA-1 wants; the Risk
Engine still evaluates it and still has final authority, and its ceilings are
neither read from nor writable by the allocator. The change from the previous
design is only that Risk is now asked for something it will grant, instead of
being asked for a billion shares and used as the sizing rule.

**Paper, and provably so.** The repository builds exactly one broker client,
in `execution.paper`, and that one line hardcodes the paper environment; there
is no live factory to reject. (The type's name is deliberately not written
here: a source guard pins the broker vocabulary to the execution boundary, and
naming it in prose would weaken a check that is meant to be mechanical.) This
runtime adds two independent confirmations anyway, because a process that
submits orders should not infer its environment from the absence of an
alternative: the client must prove it reaches the paper host, and the account
number must carry the paper namespace prefix. Either failing stops the runtime
before a single bar is fetched.

**Staged by universe.** The decision universe is always all ten symbols - the
EDA-1 series must stay complete or the overlay replay breaks - but the
*execution* universe is the rollout stage, and a symbol outside it records its
target and mutates nothing.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Protocol

import pandas as pd

from autotrader.account.safety import ACCOUNT_UNSAFE_BANNER, AccountUnsafeError
from autotrader.data.validation import (
    EQUITY_UNIVERSE_LABEL,
    ValidationResult,
    validate_frame,
)
from autotrader.decision.contract import VERSION_V3
from autotrader.decision.v3 import MultiTimeframeV3Engine
from autotrader.equity import EQUITY_SYMBOLS, EquityError
from autotrader.equity.allocation import (
    AllocationPlan,
    AllocationPolicy,
    SymbolAllocation,
    external_exposure_fraction_from,
    plan_allocation,
)
from autotrader.equity.regime import (
    EDA1_ENGINE_VERSION,
    REGIME_REFERENCE_SYMBOL,
    ParticipationSpec,
    session_closes,
    state_for_session,
)
from autotrader.equity.session import (
    MarketCalendar,
    MarketSession,
    SessionError,
    is_market_open,
    is_regular_session_bar,
    latest_completed_session_bar,
    market_date,
)
from autotrader.equity.session import next_wake_time as next_session_wake_time
from autotrader.equity.shadow import (
    DEFAULT_SHADOW_LOOKBACK_BARS,
    DEFAULT_STATE_SESSIONS,
    RegimeBarSource,
    ShadowBarSource,
    SideBySideShadowRecorder,
    create_side_by_side_tables,
    require_shadow_lookback_bars,
    require_state_sessions,
)
from autotrader.execution.equity import (
    MarketClosedError,
    equity_positions,
    execute_equity_paper_order,
)
from autotrader.execution.models import ExecutionError, OrderSide
from autotrader.execution.paper import (
    ExecutionOutcome,
    PaperExecutionResult,
    broker_symbol_key,
    verify_paper_environment,
)
from autotrader.runtime.checkpoint import ProcessedBarCheckpoint, SqliteCheckpoint
from autotrader.runtime.monitoring import (
    Heartbeat,
    HeartbeatSnapshot,
    RuntimeState,
    get_logger,
    log_event,
)
from autotrader.runtime.runner import SHUTDOWN_POLL_SECONDS, ShutdownRequest
from autotrader.runtime.schedule import (
    DEFAULT_SAFETY_DELAY,
    is_bar_complete,
    require_utc,
)
from autotrader.shadow.cycle import SKIPPED_ALREADY_PROCESSED, ShadowCycle
from autotrader.shadow.panel import EnginePanel, ShadowError
from autotrader.state import sqlite as state

#: The engine the panel evaluates. EDA-1 is derived from its record by the
#: research overlay, exactly as the Shadow derives it.
PAPER_PANEL_ENGINE_VERSION = VERSION_V3

#: The decision universe. Always all ten: the EDA-1 overlay replays the whole
#: stored series on every bar, so a gap in one symbol's series is not a smaller
#: record, it is an unreproducible one.
PAPER_DECISION_ORDER: tuple[str, ...] = EQUITY_SYMBOLS

#: The runtime lock scope. Distinct from the crypto runner's, the equity
#: trading runtime's and the shadow's, so none of them blocks another - while a
#: second paper process is still refused.
EQUITY_PAPER_LOCK_SCOPE = "equity-paper"

#: The staged execution universes (Phase 19 of the activation program). The
#: decision universe never narrows; only what may be mutated does.
STAGE_A: tuple[str, ...] = ("SPY",)
STAGE_B: tuple[str, ...] = ("SPY", "QQQ", "IWM")
STAGE_C: tuple[str, ...] = EQUITY_SYMBOLS

ROLLOUT_STAGES: Mapping[str, tuple[str, ...]] = {"A": STAGE_A, "B": STAGE_B, "C": STAGE_C}

#: The namespace prefix a paper account number carries. A second, independent
#: confirmation of the environment: `verify_paper_environment` proves which host
#: the client will reach, and this proves which account answered.
PAPER_ACCOUNT_PREFIX = "PA"

#: Audit event types this runtime writes to `system_events`.
EVENT_PAPER_STARTED = "EQUITY_PAPER_STARTED"
EVENT_PAPER_STOPPED = "EQUITY_PAPER_STOPPED"
EVENT_PAPER_CYCLE = "EQUITY_PAPER_CYCLE"
EVENT_PAPER_PARITY_MISMATCH = "EQUITY_PAPER_PARITY_MISMATCH"

#: The log token an operator greps for when the two EDA-1 computations disagree.
SHADOW_PAPER_DECISION_MISMATCH = "SHADOW_PAPER_DECISION_MISMATCH"

#: Where a cycle found itself in the market calendar. The crypto and shadow
#: runtimes' vocabulary, so one operator reads one set of tokens.
SESSION_OPEN = "SESSION_OPEN"
SESSION_CLOSED = "SESSION_CLOSED"
NO_SESSION_TODAY = "NO_SESSION_TODAY"

_ZERO = Decimal(0)


class EquityPaperError(EquityError):
    """A paper-runtime condition that stops a cycle or the process."""


class NotPaperAccountError(EquityPaperError):
    """The broker this runtime reached could not be proven to be paper. Stop."""


class PaperIntegrityError(EquityPaperError):
    """A durable invariant of the paper store could not be verified. Stop."""


class Disposition(Enum):
    """What happened to one symbol's target on one bar."""

    EXECUTED = "EXECUTED"
    PARTIALLY_ALLOWED = "PARTIALLY_ALLOWED"
    RISK_BLOCKED = "RISK_BLOCKED"
    ALREADY_SATISFIED = "ALREADY_SATISFIED"
    NOT_IN_STAGE = "NOT_IN_STAGE"
    PARITY_MISMATCH = "PARITY_MISMATCH"
    UNRESOLVED_INTENT = "UNRESOLVED_INTENT"
    MARKET_CLOSED = "MARKET_CLOSED"
    ACCOUNT_UNSAFE = "ACCOUNT_UNSAFE"
    FAILED = "FAILED"
    NO_DECISION = "NO_DECISION"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _describe_validation(result: ValidationResult) -> str:
    return "; ".join(issue.message for issue in result.issues) or "no detail reported"


@dataclass(frozen=True)
class ParityRecord:
    """One engine's answer for one bar, as the parity check compares them."""

    symbol: str
    bar_timestamp: datetime
    reference_close: float
    participate: bool
    eda1_signal: str
    eda1_stance: int

    def disagreement(self, other: ParityRecord, *, price_tolerance: float) -> str | None:
        """What the two computations actually disagree about, or None.

        **The comparison is on the target stance, not the transition signal,
        and that distinction is the whole point.** EDA-1 is a target-position
        architecture: the decision for a bar is "hold this symbol or do not",
        and `eda1_stance` is that decision. `eda1_signal` is BUY/SELL/HOLD -
        emitted only where the target *changes* - so it is a function of where
        a series began, not of what the market did. Two correct series that
        started on different days therefore differ on it forever: the one that
        began while the regime was already on records its entry as a BUY on its
        own first bar, and the older one has been HOLDing since its own.

        Comparing the signal would make that startup phase difference look like
        a decision disagreement and block every mutation for as long as both
        series live - which is exactly what it did on this runtime's first live
        cycle, on all ten symbols, while every decision field agreed to the
        cent. The stance is what the allocator consumes and the stance is what
        is compared.

        A signal difference where the stance agrees is reported separately by
        `phase_note` and is informational.

        The reference close is compared with a tolerance and everything else
        exactly. The tolerance exists because two processes fetch bars in two
        separate provider requests and a late revision can move the last
        decimal place; the decision fields have no such excuse.
        """
        if self.bar_timestamp != other.bar_timestamp:
            return f"bar_timestamp {self.bar_timestamp} != {other.bar_timestamp}"
        if self.participate != other.participate:
            return f"participate {self.participate} != {other.participate}"
        if self.eda1_stance != other.eda1_stance:
            return f"eda1_stance {self.eda1_stance} != {other.eda1_stance}"
        if abs(self.reference_close - other.reference_close) > price_tolerance:
            return f"reference_close {self.reference_close} != {other.reference_close}"
        return None

    def phase_note(self, other: ParityRecord) -> str | None:
        """A transition-signal difference the agreeing stance explains, or None.

        Reported so the difference is visible in the record rather than
        silently dropped - an operator should be able to see that the two
        series are phase-shifted, and see it stop mattering once both have
        lived through a transition together.
        """
        if self.eda1_stance != other.eda1_stance:
            return None
        if self.eda1_signal == other.eda1_signal:
            return None
        return (
            f"eda1_signal {self.eda1_signal} != {other.eda1_signal} with stance "
            f"{self.eda1_stance} agreed: the two series began on different bars, so "
            "their transition signals are phase-shifted. Not a decision disagreement."
        )


class ShadowParitySource(Protocol):
    """Where the independently computed Shadow answer is read from."""

    def decision_for(self, symbol: str, bar_timestamp: datetime) -> ParityRecord | None:
        """The Shadow's row for that bar, or None if it has not recorded it yet."""


class SqliteShadowParity:
    """Read the Shadow's side-by-side table, read-only, without migrating it.

    The connection is opened with a ``mode=ro`` URI and no
    `initialize_database` call. That matters for more than politeness: opening a
    store through this package's normal path applies pending migrations, and a
    runtime that silently upgraded the Shadow's database would be doing to the
    Shadow exactly what the v7 lineage nearly did to the crypto store.
    """

    def __init__(self, database: Path) -> None:
        self._database = Path(database)

    def decision_for(self, symbol: str, bar_timestamp: datetime) -> ParityRecord | None:
        if not self._database.exists():
            return None
        uri = f"file:{self._database}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as connection:
                row = connection.execute(
                    "SELECT participate, eda1_signal, eda1_stance, reference_close"
                    " FROM shadow_side_by_side WHERE symbol = ? AND bar_timestamp = ?",
                    (symbol, state.to_utc_text(bar_timestamp, "bar_timestamp")),
                ).fetchone()
        except sqlite3.Error:
            # A store that cannot be queried - no comparison table yet, a file
            # that is not this schema, a locked read - is a *missing*
            # comparison, and `require_parity` decides what that means. It is
            # never silently an agreement.
            return None
        if row is None:
            return None
        return ParityRecord(
            symbol=symbol,
            bar_timestamp=bar_timestamp,
            reference_close=float(row[3]),
            participate=bool(row[0]),
            eda1_signal=str(row[1]),
            eda1_stance=int(row[2]),
        )


#: The durable record of *why* an order was the size it was.
#:
#: `order_intents` holds what was sent - symbol, side, quantities, reference
#: price, risk reason, `client_order_id`. It does not hold what was wanted, and
#: after the fact those are different questions: "the broker was asked for 3
#: shares of SPY" does not say which engine wanted it, under which sizing
#: policy, against which target weight, or what the account already held. That
#: context lives in the journal, and a journal is rotated.
#:
#: Created on demand rather than added to the versioned schema, exactly as the
#: side-by-side tables are. A schema bump here would put this store a version
#: ahead of the shadow's for a table the shadow has no use for - and the whole
#: point of this deployment is that a version gap between stores is expensive.
CREATE_PAPER_TARGETS = """
    CREATE TABLE IF NOT EXISTS equity_paper_targets (
        id                 INTEGER PRIMARY KEY,
        -- Filled in after the attempt returns, because the execution boundary
        -- mints the id: `client_order_id` defaults to a fresh UUID per intent
        -- and is deliberately NOT derived from the decision, so this row cannot
        -- know it in advance. Writing the row first and the key second is the
        -- right way round anyway - the row must survive a crash that happens
        -- before an id exists.
        client_order_id    TEXT UNIQUE,
        engine             TEXT NOT NULL CHECK (engine <> ''),
        environment        TEXT NOT NULL CHECK (environment = 'PAPER'),
        sizing_policy      TEXT NOT NULL CHECK (sizing_policy <> ''),
        sizing_config_hash TEXT NOT NULL CHECK (sizing_config_hash <> ''),
        rollout_stage      TEXT NOT NULL CHECK (rollout_stage <> ''),
        symbol             TEXT NOT NULL CHECK (symbol <> ''),
        side               TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
        target_weight      TEXT NOT NULL,
        target_notional    TEXT NOT NULL,
        target_quantity    TEXT NOT NULL,
        broker_quantity    TEXT NOT NULL,
        requested_delta    TEXT NOT NULL,
        approved_quantity  TEXT,
        risk_reason_code   TEXT,
        reference_price    TEXT NOT NULL,
        account_equity     TEXT NOT NULL,
        external_exposure  TEXT NOT NULL,
        budget_fraction    TEXT NOT NULL,
        bar_timestamp      TEXT NOT NULL,
        decided_at         TEXT NOT NULL
    )
"""


def create_paper_target_table(connection: sqlite3.Connection) -> None:
    """Ensure the durable target record exists. Idempotent."""
    with state.transaction(connection):
        connection.execute(CREATE_PAPER_TARGETS)


class ExternalSafetySource(Protocol):
    """Another product's durable account-safety answer, read from its own store."""

    def unsafe_reason(self) -> str | None:
        """Why that store says the account is halted, or None if it says SAFE."""


class SqliteExternalSafety:
    """Read the crypto store's `account_safety_state`, read-only, never migrating it.

    Separate operational databases are the right answer to the schema split, but
    they do not make the *account* separate: one paper account carries both
    books, and this system's stated invariant is that an ambiguous order raised
    by either product stops both. With one shared store that invariant was a
    single row both processes read. With two stores it has to be read across, so
    this class reads it across.

    **Read-only, and by a path that cannot migrate.** A ``mode=ro`` URI and a
    single SELECT: no `initialize_database`, no `connect`, nothing from this
    package's normal open path. That is the whole point - the crypto store is
    schema 6 and this lineage is schema 7, and opening it the normal way is
    precisely the mistake that would take the crypto service down.

    **It fails closed.** A store that cannot be read, or that holds no safety
    row, returns a reason rather than None: "nobody could tell me" and "it is
    fine" are different answers and only one of them opens a gate.

    **The gap this does not close, stated plainly.** This is one-directional.
    The equity paper runtime refuses to submit while the crypto store reports a
    halt; the crypto runtime cannot see an equity halt, because it runs an older
    build that knows nothing about this store. An ambiguous *equity* order
    therefore stops the equity runtime and not the crypto one. Closing that
    direction means changing the crypto build, which is a separate change with
    its own restart.
    """

    def __init__(self, database: Path) -> None:
        self._database = Path(database)

    def unsafe_reason(self) -> str | None:
        if not self._database.exists():
            return (
                f"The crypto operational store {self._database} does not exist, so its "
                "account safety answer could not be read."
            )
        try:
            with sqlite3.connect(f"file:{self._database}?mode=ro", uri=True) as connection:
                row = connection.execute(
                    "SELECT state, reason FROM account_safety_state WHERE id = 1"
                ).fetchone()
        except sqlite3.Error as error:
            return f"The crypto operational store could not be read: {error}"
        if row is None:
            return (
                "The crypto operational store holds no account safety row, so nothing "
                "has established that the account is understood."
            )
        if str(row[0]) == state.ACCOUNT_SAFETY_SAFE:
            return None
        return f"The crypto store reports {row[0]}: {row[1]}"


@dataclass(frozen=True)
class EquityPaperConfig:
    """Everything about the runtime that is not code.

    `stage` is the rollout stage letter and it selects the execution universe;
    `policy` is the frozen allocator, carried whole so its `config_hash` can be
    logged next to every decision it sizes.
    """

    policy: AllocationPolicy
    stage: str = "A"
    lookback_bars: int = DEFAULT_SHADOW_LOOKBACK_BARS
    state_sessions: int = DEFAULT_STATE_SESSIONS
    safety_delay: timedelta = DEFAULT_SAFETY_DELAY
    code_sha: str | None = None
    #: How far two independently fetched reference closes may differ before the
    #: bars are treated as describing different market data.
    parity_price_tolerance: float = 0.01
    #: A bar the Shadow has not recorded yet is a missing comparison, not an
    #: agreement. True refuses to mutate without one, which is the safe default.
    require_parity: bool = True
    #: Whether a halt recorded in another product's store stops this one. True
    #: preserves "UNKNOWN from any asset = no new orders from any asset" across
    #: the store split; False is for a host where no other product runs.
    require_external_safety: bool = True

    def __post_init__(self) -> None:
        require_shadow_lookback_bars(self.lookback_bars)
        require_state_sessions(self.state_sessions)
        if self.stage not in ROLLOUT_STAGES:
            raise EquityPaperError(
                f"Unknown rollout stage {self.stage!r}. Known stages: "
                f"{', '.join(sorted(ROLLOUT_STAGES))}."
            )
        if self.parity_price_tolerance < 0:
            raise EquityPaperError(
                f"parity_price_tolerance cannot be negative, got {self.parity_price_tolerance}."
            )

    @property
    def execution_universe(self) -> tuple[str, ...]:
        """The symbols this stage may mutate. The decision universe is always ten."""
        return ROLLOUT_STAGES[self.stage]


@dataclass
class SymbolOutcome:
    """What this cycle decided, wanted, and did for one symbol."""

    symbol: str
    disposition: Disposition
    bar_timestamp: datetime | None = None
    eda1_signal: str | None = None
    eda1_stance: int | None = None
    participate: bool | None = None
    target_weight: Decimal | None = None
    target_quantity: Decimal | None = None
    actual_quantity: Decimal | None = None
    delta_quantity: Decimal | None = None
    side: str | None = None
    risk_reason_code: str | None = None
    approved_quantity: Decimal | None = None
    client_order_id: str | None = None
    broker_status: str | None = None
    message: str | None = None

    def as_fields(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "disposition": self.disposition.value,
            "bar_timestamp": self.bar_timestamp,
            "eda1_signal": self.eda1_signal,
            "eda1_stance": self.eda1_stance,
            "participate": self.participate,
            "target_weight": self.target_weight,
            "target_qty": self.target_quantity,
            "broker_qty": self.actual_quantity,
            "delta_qty": self.delta_quantity,
            "side": self.side,
            "risk_reason": self.risk_reason_code,
            "approved_qty": self.approved_quantity,
            "client_order_id": self.client_order_id,
            "broker_status": self.broker_status,
        }


@dataclass
class EquityPaperCycleReport:
    """One cycle's record. Enough to reconstruct why every symbol did or did not trade."""

    started_at: datetime
    session_state: str = SESSION_CLOSED
    session: MarketSession | None = None
    outcomes: list[SymbolOutcome] = field(default_factory=list)
    plan: AllocationPlan | None = None
    account_equity: float | None = None
    external_exposure_fraction: Decimal | None = None
    parity_mismatches: int = 0
    error: str | None = None
    fatal: bool = False

    @property
    def submitted(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.disposition is Disposition.EXECUTED)

    @property
    def decided(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.bar_timestamp is not None)


@dataclass
class _SymbolBars:
    frame: pd.DataFrame | None = None
    latest: datetime | None = None


class EquityPaperExecutionGateway(Protocol):
    """The seam between a sized target and the paper broker."""

    def execute(
        self,
        connection: sqlite3.Connection,
        *,
        symbol: str,
        side: OrderSide,
        requested_quantity: Decimal,
        now: datetime,
        strategy_run_id: int | None,
    ) -> PaperExecutionResult:
        """Submit one delta, or explain why it was not submitted."""


class AlpacaEquityPaperGateway:
    """The real gateway: the validated equity execution boundary, unchanged.

    Deliberately thin. Every safety step - the account read, the position read,
    the short refusal, the risk evaluation, the whole-share floor, the broker
    clock gate, the durable intent before the request, the exactly-once
    submission, the never-retry-an-ambiguous-outcome rule - belongs to
    `execute_equity_paper_order` and is not re-implemented, re-ordered or
    relaxed here.
    """

    def __init__(
        self,
        *,
        trading_client: object | None = None,
        data_client: object | None = None,
        account_lock: object | None = None,
    ) -> None:
        self._trading_client = trading_client
        self._data_client = data_client
        self._account_lock = account_lock

    def execute(
        self,
        connection: sqlite3.Connection,
        *,
        symbol: str,
        side: OrderSide,
        requested_quantity: Decimal,
        now: datetime,
        strategy_run_id: int | None,
    ) -> PaperExecutionResult:
        return execute_equity_paper_order(
            connection,
            symbol=symbol,
            side=side,
            requested_quantity=requested_quantity,
            trading_client=self._trading_client,  # type: ignore[arg-type]
            data_client=self._data_client,  # type: ignore[arg-type]
            now=now,
            strategy_run_id=strategy_run_id,
            account_lock=self._account_lock,  # type: ignore[arg-type]
        )


def require_paper_account(client: object) -> str:
    """Prove twice that this client reaches a paper account, or refuse to run.

    First that the client will send its requests to the paper host - the
    structural check the reconciler already makes - and then that the account
    which answers carries the paper namespace prefix. The second check is not
    redundant: the first describes an intention encoded in a client object, and
    the second describes the account a live request actually reached.

    Returns the account number so a caller can log which account it opened.
    """
    verify_paper_environment(client)  # type: ignore[arg-type]
    account = client.get_account()  # type: ignore[attr-defined]
    number = str(getattr(account, "account_number", "") or "")
    if not number.startswith(PAPER_ACCOUNT_PREFIX):
        raise NotPaperAccountError(
            "Refusing to start: the trading client reaches the paper host but the "
            f"account that answered does not carry the {PAPER_ACCOUNT_PREFIX!r} paper "
            "namespace prefix. Nothing was fetched and no order was submitted."
        )
    return number


def non_equity_exposure(positions: Mapping[str, object]) -> float:
    """Everything the account holds that is not one of the ten equities.

    In this deployment that is the crypto book, and it is the figure the
    allocator's budget is reduced by. Taken from broker positions rather than
    from any local store, because the total exposure ceiling is an account-wide
    ceiling and the equity book cannot see the crypto book's database.
    """
    equity_keys = {broker_symbol_key(symbol) for symbol in EQUITY_SYMBOLS}
    total = 0.0
    for key, position in positions.items():
        if key in equity_keys:
            continue
        value = float(getattr(position, "market_value", 0.0))
        if value > 0:
            total += value
    return total


class EquityPaperRuntime:
    """EDA-1 against the paper broker, on completed regular-session bars.

    Construct it with an open connection to the **equity paper** operational
    store - never the crypto store and never the Shadow's - a V3-sized bar
    source, a regime bar source, the broker's market calendar, an execution
    gateway and a parity source.

    A cycle is: gate on the session, resolve the regime state once for the
    session, fetch one batch of bars for all ten symbols, decide all ten, check
    each against the Shadow, allocate across the ones the stage may trade, and
    submit only the deltas. Everything except the last two steps is what the
    Shadow already does; the last two are what this module exists for.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        market_data: ShadowBarSource,
        regime_data: RegimeBarSource,
        calendar: MarketCalendar,
        gateway: EquityPaperExecutionGateway,
        parity: ShadowParitySource,
        config: EquityPaperConfig,
        broker_state: Callable[[], tuple[float, dict[str, object]]],
        external_safety: ExternalSafetySource | None = None,
        checkpoint: ProcessedBarCheckpoint | None = None,
        regime_spec: ParticipationSpec | None = None,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
        shutdown: ShutdownRequest | None = None,
        logger: logging.Logger | None = None,
        strategy_run_id: int | None = None,
    ) -> None:
        self._connection = connection
        self._config = config
        self._market_data = market_data
        self._regime_data = regime_data
        self._calendar = calendar
        self._gateway = gateway
        self._parity = parity
        self._broker_state = broker_state
        self._external_safety = external_safety
        self._checkpoint: ProcessedBarCheckpoint = (
            checkpoint if checkpoint is not None else SqliteCheckpoint(connection)
        )
        self._spec = regime_spec if regime_spec is not None else ParticipationSpec()
        self._clock = clock
        self._sleep = sleep
        self._shutdown = shutdown if shutdown is not None else ShutdownRequest()
        self._logger = logger if logger is not None else get_logger()
        self._strategy_run_id = strategy_run_id

        self._heartbeat = Heartbeat()
        self._heartbeat.last_processed_bars = {symbol: None for symbol in PAPER_DECISION_ORDER}
        # The heartbeat's shared fields, set honestly for this runtime: it does
        # hold an execution path, and its account-safety answer comes from the
        # durable row reconciliation wrote rather than from a startup pass this
        # process runs itself.
        self._heartbeat.paper_execution_enabled = True
        self._recorder = SideBySideShadowRecorder(
            connection, spec=self._spec, strategy_run_id=strategy_run_id
        )
        self._cycles: dict[str, ShadowCycle] = {
            symbol: ShadowCycle(
                panel=EnginePanel(
                    (MultiTimeframeV3Engine.for_symbol(symbol),),
                    execution_version=PAPER_PANEL_ENGINE_VERSION,
                ),
                recorder=self._recorder,
                checkpoint=self._checkpoint,
            )
            for symbol in PAPER_DECISION_ORDER
        }
        self._started = False
        self._parity_mismatches = 0
        self._parity_phase_notes = 0

    # ------------------------------------------------------------------
    # Observable state
    # ------------------------------------------------------------------

    @property
    def heartbeat(self) -> HeartbeatSnapshot:
        return self._heartbeat.snapshot()

    @property
    def state(self) -> RuntimeState:
        return self._heartbeat.state

    @property
    def shutdown(self) -> ShutdownRequest:
        return self._shutdown

    @property
    def policy(self) -> AllocationPolicy:
        return self._config.policy

    @property
    def execution_universe(self) -> tuple[str, ...]:
        return self._config.execution_universe

    @property
    def parity_mismatches(self) -> int:
        return self._parity_mismatches

    @property
    def parity_phase_notes(self) -> int:
        """Transition-signal differences an agreeing stance explains. Not mismatches."""
        return self._parity_phase_notes

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the record after proving the store is this runtime's own."""
        if self._started:
            return
        now = require_utc(self._clock(), "now")
        create_side_by_side_tables(self._connection)
        create_paper_target_table(self._connection)
        self._require_consistent_regime_spec()
        self._require_no_unresolved_intents()

        self._heartbeat.state = RuntimeState.RUNNING
        self._heartbeat.started_at = now
        for symbol in PAPER_DECISION_ORDER:
            self._heartbeat.last_processed_bars[symbol] = self._checkpoint.last_processed(symbol)
        self._refresh_safety_heartbeat()
        self._started = True

        policy = self._config.policy
        state.record_system_event(
            self._connection,
            event_timestamp=now,
            event_type=EVENT_PAPER_STARTED,
            message=(
                f"Equity EDA-1 PAPER runtime started at rollout stage "
                f"{self._config.stage} (execution universe "
                f"{', '.join(self._config.execution_universe)}; decision universe "
                f"{', '.join(PAPER_DECISION_ORDER)}). Sizing policy "
                f"{policy.policy_id} ({policy.config_hash()[:12]}), per-symbol cap "
                f"{policy.per_symbol_cap}, total cap {policy.total_cap}. Environment: "
                "PAPER ONLY."
            ),
        )
        log_event(
            self._logger,
            "equity_paper_started",
            started_at=now,
            stage=self._config.stage,
            execution_universe=",".join(self._config.execution_universe),
            sizing_policy=policy.policy_id,
            sizing_config_hash=policy.config_hash(),
            per_symbol_cap=str(policy.per_symbol_cap),
            total_cap=str(policy.total_cap),
            lookback_bars=self._config.lookback_bars,
            state_sessions=self._config.state_sessions,
            code_sha=self._config.code_sha,
            environment="PAPER",
        )

    def _refresh_safety_heartbeat(self) -> None:
        """Show the durable account-safety answer and the freshest pass in the beat.

        Read rather than assumed. A heartbeat that printed a startup code this
        runtime never computed would be the same defect the crypto heartbeat
        had: echoing a stale verdict as though it were current.
        """
        safety = state.read_account_safety_state(self._connection)
        self._heartbeat.startup_safety_code = safety.state
        row = self._connection.execute(
            "SELECT status FROM reconciliation_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self._heartbeat.reconciliation_status = str(row[0]) if row is not None else None

    def _require_consistent_regime_spec(self) -> None:
        """Refuse a store whose regime states were computed by a different router."""
        rows = self._connection.execute(
            "SELECT DISTINCT sma_sessions, calm_threshold, lag_sessions, reference_symbol"
            " FROM shadow_regime_state"
        ).fetchall()
        expected = (
            self._spec.sma_sessions,
            self._spec.calm_threshold,
            self._spec.lag_sessions,
            REGIME_REFERENCE_SYMBOL,
        )
        for row in rows:
            found = (int(row[0]), float(row[1]), int(row[2]), str(row[3]))
            if found != expected:
                raise PaperIntegrityError(
                    f"This store holds regime states computed under "
                    f"(sma, calm, lag, reference)={found}, but this process is "
                    f"configured with {expected}. A state series from another router "
                    "would make the stored EDA-1 series unreproducible; refusing to "
                    "mix them. Nothing was evaluated."
                )

    def unresolved_intents(self) -> tuple[str, ...]:
        """`client_order_id`s whose broker outcome this store does not yet know.

        `CREATED` means an intent was committed and the request may or may not
        have gone out; `SUBMITTING` means one was in flight; `UNKNOWN` means the
        response was uninterpretable. Every one of them is a possible live order
        under a durable key, and none may be joined by a second order for the
        same target until reconciliation has settled it.
        """
        rows = self._connection.execute(
            "SELECT client_order_id FROM order_intents WHERE status IN (?, ?, ?)"
            " ORDER BY client_order_id",
            (
                state.INTENT_STATUS_CREATED,
                state.INTENT_STATUS_SUBMITTING,
                state.INTENT_STATUS_UNKNOWN,
            ),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _require_no_unresolved_intents(self) -> None:
        """Startup idempotence: settle the past before proposing a future.

        A restart must not create a duplicate BUY. The durable evidence that it
        would be one is an intent this store opened and never closed, so the
        runtime refuses to start while any exists. The resolution path is
        reconciliation - which asks the broker about that exact
        `client_order_id` and never submits - not a retry and not a second
        order.
        """
        outstanding = self.unresolved_intents()
        if outstanding:
            raise PaperIntegrityError(
                f"{len(outstanding)} order intent(s) in this store have no settled broker "
                f"outcome ({', '.join(outstanding[:5])}"
                f"{', ...' if len(outstanding) > 5 else ''}). An order may exist at the "
                "broker under each of those keys, so proposing a new target for the same "
                "symbol could duplicate it. Run `autotrader reconcile` against this "
                "database and start again. Nothing was evaluated and no order was "
                "submitted."
            )

    def stop(self, *, status: str | None = None) -> None:
        """Close the record. Idempotent."""
        if not self._started:
            return
        self._started = False
        now = require_utc(self._clock(), "now")
        if self._heartbeat.state is not RuntimeState.FAILED:
            self._heartbeat.state = RuntimeState.STOPPED
        state.record_system_event(
            self._connection,
            event_timestamp=now,
            event_type=EVENT_PAPER_STOPPED,
            message=(
                f"Equity EDA-1 PAPER runtime stopped in state "
                f"{status or self._heartbeat.state.value}. Orders submitted this run: "
                f"{self._heartbeat.orders_submitted}. Shadow/Paper decision mismatches: "
                f"{self._parity_mismatches}."
            ),
        )
        log_event(
            self._logger,
            "equity_paper_stopped",
            stopped_at=now,
            state=self._heartbeat.state,
            signal=self._shutdown.signal_name,
            cycles_started=self._heartbeat.cycles_started,
            cycles_completed=self._heartbeat.cycles_completed,
            orders_submitted=self._heartbeat.orders_submitted,
            parity_mismatches=self._parity_mismatches,
        )
        self.log_heartbeat()

    def log_heartbeat(self) -> None:
        log_event(self._logger, "heartbeat", **self._heartbeat.snapshot().as_fields())

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def run_once(self) -> EquityPaperCycleReport:
        """Process the current cycle once and stop."""
        self.start()
        try:
            report = self.run_cycle()
        except BaseException:
            self._heartbeat.state = RuntimeState.FAILED
            self.stop()
            raise
        self.stop()
        return report

    def run_forever(self, *, max_cycles: int | None = None) -> list[EquityPaperCycleReport]:
        """Run on the session's own bar boundaries until told to stop."""
        self.start()
        reports: list[EquityPaperCycleReport] = []
        try:
            while not self._shutdown.requested:
                if max_cycles is not None and len(reports) >= max_cycles:
                    break
                report = self.run_cycle()
                reports.append(report)
                if report.fatal:
                    self._heartbeat.state = RuntimeState.FAILED
                    break
                target = next_session_wake_time(
                    self._calendar,
                    now=require_utc(self._clock(), "now"),
                    safety_delay=self._config.safety_delay,
                )
                log_event(self._logger, "cycle_scheduled", wake_at=target)
                self._wait_until(target)
        finally:
            self.stop()
        return reports

    def _wait_until(self, target: datetime) -> None:
        while not self._shutdown.requested:
            remaining = (target - require_utc(self._clock(), "now")).total_seconds()
            if remaining <= 0:
                return
            self._sleep(min(SHUTDOWN_POLL_SECONDS, remaining))

    def run_cycle(self, now: datetime | None = None) -> EquityPaperCycleReport:
        """One cycle: decide ten, check parity, allocate, submit only deltas."""
        moment = require_utc(now if now is not None else self._clock(), "now")
        report = EquityPaperCycleReport(started_at=moment)
        self._heartbeat.cycles_started += 1
        self._heartbeat.last_cycle_started_at = moment
        log_event(
            self._logger,
            "cycle_started",
            at=moment,
            state=self._heartbeat.state,
            stage=self._config.stage,
            mode="EQUITY_PAPER",
        )

        try:
            session = self._resolve_session(moment, report)
            if session is not None:
                self._decide(session, moment, report)
                self._settle(moment, report)
        except PaperIntegrityError:
            raise
        except Exception as error:  # noqa: BLE001 - classified rather than propagated
            report.error = f"{type(error).__name__}: {error}"
            self._heartbeat.last_error = report.error
            log_event(
                self._logger,
                "cycle_failed",
                level=logging.ERROR,
                at=moment,
                error=report.error,
            )

        if report.error is None:
            self._heartbeat.cycles_completed += 1
            self._heartbeat.last_successful_cycle_at = moment
        if report.session_state == SESSION_OPEN:
            self._record_cycle_audit(moment, report)
        self._refresh_safety_heartbeat()

        log_event(
            self._logger,
            "cycle_finished",
            at=moment,
            session=report.session_state,
            decided=report.decided,
            submitted=report.submitted,
            parity_mismatches=report.parity_mismatches,
            error=report.error,
            state=self._heartbeat.state,
        )
        self.log_heartbeat()
        return report

    # ------------------------------------------------------------------
    # Decision half - the Shadow's, unchanged
    # ------------------------------------------------------------------

    def _resolve_session(
        self, moment: datetime, report: EquityPaperCycleReport
    ) -> MarketSession | None:
        """The broker's calendar is the authority. Local time decides nothing."""
        open_now, session = is_market_open(self._calendar, now=moment)
        report.session = session
        if session is None:
            report.session_state = NO_SESSION_TODAY
            log_event(
                self._logger,
                "session_closed",
                at=moment,
                reason=NO_SESSION_TODAY,
                market_date=market_date(moment).isoformat(),
            )
            return None
        if not open_now:
            report.session_state = SESSION_CLOSED
            log_event(
                self._logger,
                "session_closed",
                at=moment,
                reason=SESSION_CLOSED,
                market_date=session.session_date.isoformat(),
                session_open=session.open_utc,
                session_close=session.close_utc,
            )
            return None
        report.session_state = SESSION_OPEN
        return session

    def _decide(
        self, session: MarketSession, moment: datetime, report: EquityPaperCycleReport
    ) -> None:
        """Evaluate all ten symbols and record both engines' answers."""
        latest = latest_completed_session_bar(
            session, now=moment, safety_delay=self._config.safety_delay
        )
        if latest is None:
            log_event(
                self._logger,
                "no_completed_bar",
                at=moment,
                market_date=session.session_date.isoformat(),
            )
            return
        self._ensure_regime_state(session, moment)
        frames = self._market_data.recent_bars(
            PAPER_DECISION_ORDER,
            now=moment,
            latest_bar_start=latest,
            lookback_bars=self._config.lookback_bars,
        )
        for symbol in PAPER_DECISION_ORDER:
            if self._shutdown.requested:
                log_event(self._logger, "cycle_interrupted", symbol=symbol, reason="shutdown")
                break
            try:
                report.outcomes.append(
                    self._decide_symbol(symbol, session, frames.get(symbol), moment)
                )
            except PaperIntegrityError:
                raise
            except (EquityError, SessionError, ShadowError, state.StateError) as error:
                report.outcomes.append(
                    SymbolOutcome(
                        symbol=symbol,
                        disposition=Disposition.FAILED,
                        message=f"{type(error).__name__}: {error}",
                    )
                )
                log_event(
                    self._logger,
                    "symbol_failed",
                    level=logging.ERROR,
                    symbol=symbol,
                    error=str(error),
                )

    def _decide_symbol(
        self,
        symbol: str,
        session: MarketSession,
        frame: pd.DataFrame | None,
        now: datetime,
    ) -> SymbolOutcome:
        """Validate, trim, claim, evaluate V3, derive EDA-1, compare with the Shadow."""
        if frame is None or frame.empty:
            log_event(self._logger, "no_bars", symbol=symbol, at=now)
            return SymbolOutcome(symbol=symbol, disposition=Disposition.NO_DECISION)

        validation = validate_frame(
            frame,
            supported_symbols=PAPER_DECISION_ORDER,
            universe_label=EQUITY_UNIVERSE_LABEL,
        )
        if not validation.valid:
            raise EquityPaperError(
                f"Bars for {symbol} failed validation, so nothing was evaluated: "
                f"{_describe_validation(validation)}"
            )

        bars = self._completed_session_bars(symbol, session, frame, now)
        if bars.latest is None or bars.frame is None:
            return SymbolOutcome(symbol=symbol, disposition=Disposition.NO_DECISION)

        reference_close = float(bars.frame["close"].iloc[-1])
        self._recorder.begin_bar(symbol, reference_close=reference_close)
        outcome = self._cycles[symbol].evaluate_bar(symbol, bars.frame, bar_timestamp=bars.latest)
        if not outcome.claimed:
            # Already processed. Not an error and not a second opinion: this
            # runtime holds the target it already computed for that bar.
            #
            # The parity check is re-run against the restored answer, and that
            # is not redundant. A restart re-reads the stored decision and
            # re-derives a target from it, so without this a symbol the shadow
            # disagreed with on its first claim would become eligible again
            # simply because the process bounced - the block would last until
            # the next restart instead of until the disagreement was resolved.
            log_event(
                self._logger,
                "bar_already_processed",
                symbol=symbol,
                timestamp=bars.latest,
                reason=outcome.skipped_reason or SKIPPED_ALREADY_PROCESSED,
            )
            restored = self._outcome_from_store(symbol, bars.latest)
            self._check_parity(restored, reference_close)
            return restored

        self._heartbeat.last_processed_bars[symbol] = bars.latest
        eda1 = self._recorder.last_eda1.get(symbol)
        participate = self._recorder.last_participate.get(symbol)
        if eda1 is None or participate is None:
            raise PaperIntegrityError(
                f"No EDA-1 decision was recorded for {symbol} at {bars.latest.isoformat()}, "
                "so no target can be derived. Nothing was submitted."
            )
        stance = self._stored_stance(symbol)
        result = SymbolOutcome(
            symbol=symbol,
            disposition=Disposition.NO_DECISION,
            bar_timestamp=bars.latest,
            eda1_signal=eda1.signal.value,
            eda1_stance=stance,
            participate=participate,
        )
        log_event(
            self._logger,
            "paper_decision",
            symbol=symbol,
            bar_timestamp=bars.latest,
            engine=EDA1_ENGINE_VERSION,
            eda1_signal=eda1.signal.value,
            eda1_stance=stance,
            participate=participate,
            reference_close=reference_close,
            in_execution_universe=symbol in self._config.execution_universe,
        )
        self._check_parity(result, reference_close)
        return result

    def _outcome_from_store(self, symbol: str, bar_timestamp: datetime) -> SymbolOutcome:
        """This runtime's own recorded answer for a bar it already claimed.

        A restart mid-session re-reads what it decided rather than re-deciding:
        the bar claim is durable, and a second evaluation of the same fifteen
        minutes would be a second opinion the target semantics have no room for.
        """
        row = self._connection.execute(
            "SELECT participate, eda1_signal, eda1_stance FROM shadow_side_by_side"
            " WHERE symbol = ? AND bar_timestamp = ?",
            (symbol, state.to_utc_text(bar_timestamp, "bar_timestamp")),
        ).fetchone()
        if row is None:
            return SymbolOutcome(
                symbol=symbol,
                disposition=Disposition.NO_DECISION,
                bar_timestamp=bar_timestamp,
            )
        return SymbolOutcome(
            symbol=symbol,
            disposition=Disposition.NO_DECISION,
            bar_timestamp=bar_timestamp,
            participate=bool(row[0]),
            eda1_signal=str(row[1]),
            eda1_stance=int(row[2]),
        )

    def _stored_stance(self, symbol: str) -> int:
        """EDA-1's stance at the newest stored bar: 1 long, 0 flat."""
        rows = self._connection.execute(
            "SELECT eda1_stance FROM shadow_side_by_side WHERE symbol = ?"
            " ORDER BY bar_timestamp DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        if rows is None:
            raise PaperIntegrityError(
                f"No stored EDA-1 stance for {symbol}. Nothing was submitted."
            )
        return int(rows[0])

    def _check_parity(self, outcome: SymbolOutcome, reference_close: float) -> None:
        """Compare this runtime's EDA-1 answer with the Shadow's for the same bar.

        A mismatch does not stop the process and does not stop the other
        symbols: it disqualifies **this symbol on this bar** from any broker
        mutation, which is the smallest action that is still safe. A bar the
        Shadow has not yet recorded counts as a missing comparison and, under
        `require_parity`, is treated the same way - the two runtimes wake on the
        same boundary and the Shadow's row normally lands first, so a persistent
        absence is a real signal about the Shadow, not a timing quirk to ignore.
        """
        if outcome.bar_timestamp is None or outcome.eda1_signal is None:
            return
        mine = ParityRecord(
            symbol=outcome.symbol,
            bar_timestamp=outcome.bar_timestamp,
            reference_close=reference_close,
            participate=bool(outcome.participate),
            eda1_signal=outcome.eda1_signal,
            eda1_stance=int(outcome.eda1_stance or 0),
        )
        theirs = self._parity.decision_for(outcome.symbol, outcome.bar_timestamp)
        if theirs is None:
            if self._config.require_parity:
                outcome.disposition = Disposition.PARITY_MISMATCH
                outcome.message = "The shadow has not recorded this bar."
                self._record_parity_mismatch(outcome, outcome.message)
            return
        difference = mine.disagreement(theirs, price_tolerance=self._config.parity_price_tolerance)
        if difference is not None:
            outcome.disposition = Disposition.PARITY_MISMATCH
            outcome.message = difference
            self._record_parity_mismatch(outcome, difference)
            return
        phase = mine.phase_note(theirs)
        if phase is not None:
            self._parity_phase_notes += 1
            log_event(
                self._logger,
                "shadow_paper_signal_phase",
                symbol=outcome.symbol,
                bar_timestamp=outcome.bar_timestamp,
                detail=phase,
                action="MUTATION_STILL_ALLOWED",
            )

    def _record_parity_mismatch(self, outcome: SymbolOutcome, detail: str) -> None:
        self._parity_mismatches += 1
        log_event(
            self._logger,
            SHADOW_PAPER_DECISION_MISMATCH.lower(),
            level=logging.WARNING,
            symbol=outcome.symbol,
            bar_timestamp=outcome.bar_timestamp,
            detail=detail,
            action="NO_MUTATION_FOR_THIS_SYMBOL_BAR",
        )
        state.record_system_event(
            self._connection,
            event_timestamp=require_utc(self._clock(), "now"),
            event_type=EVENT_PAPER_PARITY_MISMATCH,
            message=(
                f"{SHADOW_PAPER_DECISION_MISMATCH}: {outcome.symbol} at "
                f"{outcome.bar_timestamp}: {detail}. No broker mutation for this "
                "symbol on this bar."
            ),
        )

    def _ensure_regime_state(self, session: MarketSession, moment: datetime) -> None:
        """Resolve and persist the EDA-1 state governing `session`, once."""
        day = session.session_date
        existing = self._connection.execute(
            "SELECT participate FROM shadow_regime_state WHERE session_date = ?",
            (day.isoformat(),),
        ).fetchone()
        if existing is not None:
            return
        frame = self._regime_data.state_frame(
            before=day, now=moment, sessions=self._config.state_sessions
        )
        if frame is None or frame.empty:
            raise EquityPaperError(
                f"The reference symbol {REGIME_REFERENCE_SYMBOL} returned no completed "
                f"bars before {day.isoformat()}, so the regime state governing this "
                "session cannot be resolved. Nothing was evaluated."
            )
        closes = session_closes(frame)
        resolved = state_for_session(closes, self._spec, session_date=day)
        with state.transaction(self._connection):
            self._connection.execute(
                "INSERT INTO shadow_regime_state ("
                " session_date, participate, info_close, info_sma, info_drawdown,"
                " sessions_observed, sma_sessions, calm_threshold, lag_sessions,"
                " reference_symbol, computed_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    day.isoformat(),
                    int(resolved.participate),
                    resolved.info_close,
                    resolved.info_sma,
                    resolved.info_drawdown,
                    resolved.sessions_observed,
                    self._spec.sma_sessions,
                    self._spec.calm_threshold,
                    self._spec.lag_sessions,
                    REGIME_REFERENCE_SYMBOL,
                    state.to_utc_text(moment, "computed_at"),
                ),
            )
        log_event(
            self._logger,
            "regime_state_resolved",
            session=day.isoformat(),
            participate=resolved.participate,
            info_close=resolved.info_close,
            info_sma=resolved.info_sma,
            info_drawdown=resolved.info_drawdown,
            sessions_observed=resolved.sessions_observed,
            reference=REGIME_REFERENCE_SYMBOL,
        )

    def _completed_session_bars(
        self,
        symbol: str,
        session: MarketSession,
        frame: pd.DataFrame,
        now: datetime,
    ) -> _SymbolBars:
        """The frame trimmed to completed regular-session bars of this session."""
        timestamps = [require_utc(value, "bar timestamp") for value in frame["timestamp"]]
        completed = [
            index
            for index, timestamp in enumerate(timestamps)
            if is_bar_complete(timestamp, now=now, safety_delay=self._config.safety_delay)
        ]
        if not completed:
            log_event(self._logger, "no_completed_bar", symbol=symbol, at=now)
            return _SymbolBars()
        trimmed = frame.iloc[completed].reset_index(drop=True)
        latest = timestamps[completed[-1]]
        if market_date(latest) != session.session_date:
            log_event(
                self._logger,
                "no_bar_this_session",
                symbol=symbol,
                newest=latest,
                market_date=session.session_date.isoformat(),
            )
            return _SymbolBars(frame=trimmed)
        if not is_regular_session_bar(session, latest):
            raise EquityPaperError(
                f"The newest completed {symbol} bar is stamped {latest.isoformat()}, "
                f"which is not a regular-session 15-minute bar of the "
                f"{session.session_date.isoformat()} session. Refusing to evaluate an "
                "extended-hours candle rather than rounding it into place."
            )
        return _SymbolBars(frame=trimmed, latest=latest)

    # ------------------------------------------------------------------
    # Execution half - allocate, delta, submit
    # ------------------------------------------------------------------

    def _settle(self, moment: datetime, report: EquityPaperCycleReport) -> None:
        """Turn this cycle's targets into the smallest set of orders that reaches them."""
        report.parity_mismatches = sum(
            1 for outcome in report.outcomes if outcome.disposition is Disposition.PARITY_MISMATCH
        )

        eligible = [
            outcome
            for outcome in report.outcomes
            if outcome.bar_timestamp is not None and outcome.disposition is Disposition.NO_DECISION
        ]
        for outcome in report.outcomes:
            if (
                outcome.disposition is Disposition.NO_DECISION
                and outcome.symbol not in self._config.execution_universe
            ):
                outcome.disposition = Disposition.NOT_IN_STAGE
        tradable = [
            outcome for outcome in eligible if outcome.symbol in self._config.execution_universe
        ]
        if not tradable:
            return

        outstanding = self.unresolved_intents()
        if outstanding:
            # Mid-session, not at startup: a submission this cycle left
            # unsettled. No further mutation until reconciliation settles it.
            for outcome in tradable:
                outcome.disposition = Disposition.UNRESOLVED_INTENT
                outcome.message = (
                    f"{len(outstanding)} unsettled order intent(s) in this store; "
                    "no new target may be proposed until reconciliation settles them."
                )
            log_event(
                self._logger,
                "unresolved_intents_block_mutation",
                level=logging.WARNING,
                count=len(outstanding),
                client_order_ids=",".join(outstanding[:5]),
            )
            return

        blocked = self._external_halt()
        if blocked is not None:
            for outcome in tradable:
                outcome.disposition = Disposition.ACCOUNT_UNSAFE
                outcome.message = blocked
            log_event(
                self._logger,
                "external_account_halt",
                level=logging.ERROR,
                banner=ACCOUNT_UNSAFE_BANNER,
                detail=blocked,
            )
            return

        account_equity, positions = self._broker_state()
        external = external_exposure_fraction_from(
            account_equity=account_equity,
            non_equity_exposure=non_equity_exposure(positions),
        )
        report.account_equity = account_equity
        report.external_exposure_fraction = external

        active = {outcome.symbol for outcome in tradable if outcome.eda1_stance == 1}
        held_equity = equity_positions(positions)  # type: ignore[arg-type]
        actual = {
            symbol: Decimal(str(getattr(position, "quantity", 0)))
            for symbol, position in held_equity.items()
        }
        prices = {
            outcome.symbol: Decimal(str(self._reference_close(outcome)))
            for outcome in tradable
            if self._reference_close(outcome) is not None
        }
        # A symbol held but no longer decided this cycle still needs a price
        # only if it has a target, and a stale holding's target is zero - so a
        # missing price for it is not a failure.
        plan = plan_allocation(
            self._config.policy,
            active_symbols=active,
            account_equity=Decimal(str(account_equity)),
            external_exposure_fraction=external,
            reference_prices=prices,
            actual_quantities={
                symbol: quantity
                for symbol, quantity in actual.items()
                if symbol in self._config.execution_universe
            },
        )
        report.plan = plan
        by_symbol = {item.symbol: item for item in plan.allocations}

        log_event(
            self._logger,
            "allocation_planned",
            stage=self._config.stage,
            policy=self._config.policy.policy_id,
            config_hash=self._config.policy.config_hash(),
            account_equity=account_equity,
            external_exposure_fraction=str(external),
            budget_fraction=str(plan.budget_fraction),
            active=",".join(sorted(active)) or "-",
            total_target_weight=str(plan.total_target_weight),
            orders=len(plan.ordering),
        )

        for outcome in tradable:
            allocation = by_symbol.get(outcome.symbol)
            if allocation is None:
                outcome.disposition = Disposition.ALREADY_SATISFIED
                outcome.target_weight = _ZERO
                outcome.target_quantity = _ZERO
                outcome.actual_quantity = actual.get(outcome.symbol, _ZERO)
                continue
            self._apply(outcome, allocation, moment, report)

    def _external_halt(self) -> str | None:
        """Whether another product's store says the shared account is halted.

        Checked before the broker is read and before anything is sized, because
        a halt raised by the crypto book is a statement about the *account*: while
        one is outstanding the account's true position and true exposure are both
        unknown, and every number an equity risk decision would be measured
        against is unreliable.
        """
        if not self._config.require_external_safety or self._external_safety is None:
            return None
        return self._external_safety.unsafe_reason()

    def _reference_close(self, outcome: SymbolOutcome) -> float | None:
        """The close of the bar this symbol was decided on."""
        if outcome.bar_timestamp is None:
            return None
        row = self._connection.execute(
            "SELECT reference_close FROM shadow_side_by_side"
            " WHERE symbol = ? AND bar_timestamp = ?",
            (outcome.symbol, state.to_utc_text(outcome.bar_timestamp, "bar_timestamp")),
        ).fetchone()
        return float(row[0]) if row is not None else None

    def _apply(
        self,
        outcome: SymbolOutcome,
        allocation: SymbolAllocation,
        moment: datetime,
        report: EquityPaperCycleReport,
    ) -> None:
        """Submit one symbol's delta, or record why there was nothing to submit."""
        outcome.target_weight = allocation.target_weight
        outcome.target_quantity = allocation.target_quantity
        outcome.actual_quantity = allocation.actual_quantity
        outcome.delta_quantity = allocation.delta_quantity
        outcome.side = allocation.side.value if allocation.side is not None else None

        if not allocation.orders:
            # The normal case, and the one that must stay silent: the target has
            # not moved a whole share, so there is nothing to send.
            outcome.disposition = Disposition.ALREADY_SATISFIED
            return

        # Durable, and BEFORE the broker is asked for anything. A crash between
        # this row and the intent leaves a target nobody acted on, which reads
        # correctly; the reverse order would leave an order nobody can explain.
        target_id = self._record_target(outcome, allocation, moment, report)

        try:
            result = self._gateway.execute(
                self._connection,
                symbol=allocation.symbol,
                side=allocation.side,  # type: ignore[arg-type]
                requested_quantity=allocation.delta_quantity,
                now=moment,
                strategy_run_id=self._strategy_run_id,
            )
        except MarketClosedError as error:
            outcome.disposition = Disposition.MARKET_CLOSED
            outcome.message = str(error)
            log_event(
                self._logger,
                "market_closed",
                level=logging.WARNING,
                symbol=allocation.symbol,
                error=str(error),
            )
            return
        except AccountUnsafeError as error:
            outcome.disposition = Disposition.ACCOUNT_UNSAFE
            outcome.message = str(error)
            log_event(
                self._logger,
                "account_unsafe",
                level=logging.ERROR,
                symbol=allocation.symbol,
                banner=ACCOUNT_UNSAFE_BANNER,
                error=str(error),
            )
            return
        except ExecutionError as error:
            outcome.disposition = Disposition.FAILED
            outcome.message = f"{type(error).__name__}: {error}"
            log_event(
                self._logger,
                "execution_failed",
                level=logging.ERROR,
                symbol=allocation.symbol,
                error=str(error),
            )
            return

        outcome.risk_reason_code = result.risk_decision.reason_code
        outcome.approved_quantity = result.risk_decision.approved_quantity
        if result.intent is not None:
            outcome.client_order_id = result.intent.client_order_id
        if result.broker_order is not None:
            outcome.broker_status = result.broker_order.status
        outcome.message = result.message

        if result.outcome is ExecutionOutcome.REJECTED_BY_RISK:
            outcome.disposition = Disposition.RISK_BLOCKED
        elif result.outcome in (ExecutionOutcome.SUBMITTED, ExecutionOutcome.DUPLICATE):
            approved = result.risk_decision.approved_quantity
            outcome.disposition = (
                Disposition.EXECUTED
                if approved >= allocation.delta_quantity
                else Disposition.PARTIALLY_ALLOWED
            )
            self._heartbeat.orders_submitted += 1
        else:
            outcome.disposition = Disposition.FAILED

        self._settle_target_risk(target_id, outcome)
        log_event(
            self._logger,
            "paper_order",
            **outcome.as_fields(),
            outcome=result.outcome.value,
        )

    def _record_target(
        self,
        outcome: SymbolOutcome,
        allocation: SymbolAllocation,
        moment: datetime,
        report: EquityPaperCycleReport,
    ) -> int:
        """Persist what was wanted, and why, before anything is sent.

        Written before the submission attempt, and *without* a
        `client_order_id`, because the execution boundary mints one per intent
        from a fresh UUID rather than deriving it from the decision - this row
        cannot know it in advance and must not pretend to. The id and what Risk
        allowed are both filled in by `_settle_target_risk` once the attempt has
        returned. That ordering is the right way round regardless: the row has
        to survive a crash that happens before any id exists.

        Returns the row id, so the settle step can find this exact row.
        """
        policy = self._config.policy
        with state.transaction(self._connection):
            cursor = self._connection.execute(
                "INSERT INTO equity_paper_targets ("
                " client_order_id, engine, environment, sizing_policy,"
                " sizing_config_hash, rollout_stage, symbol, side, target_weight,"
                " target_notional, target_quantity, broker_quantity, requested_delta,"
                " approved_quantity, risk_reason_code, reference_price, account_equity,"
                " external_exposure, budget_fraction, bar_timestamp, decided_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    None,
                    EDA1_ENGINE_VERSION,
                    "PAPER",
                    policy.policy_id,
                    policy.config_hash(),
                    self._config.stage,
                    allocation.symbol,
                    allocation.side.value,  # type: ignore[union-attr]
                    str(allocation.target_weight),
                    str(allocation.target_notional),
                    str(allocation.target_quantity),
                    str(allocation.actual_quantity),
                    str(allocation.delta_quantity),
                    None,
                    None,
                    str(allocation.reference_price),
                    str(report.account_equity),
                    str(report.external_exposure_fraction),
                    str(report.plan.budget_fraction if report.plan is not None else ""),
                    state.to_utc_text(outcome.bar_timestamp, "bar_timestamp")
                    if outcome.bar_timestamp is not None
                    else "",
                    state.to_utc_text(moment, "decided_at"),
                ),
            )
        return int(cursor.lastrowid or 0)

    def _settle_target_risk(self, target_id: int, outcome: SymbolOutcome) -> None:
        """Back-fill the broker key and what Risk allowed, once the attempt returned.

        The key comes second on purpose - see `_record_target`. A row that never
        gets one describes an attempt that failed before the boundary minted an
        id, which is exactly what it should look like.
        """
        if not target_id:
            return
        with state.transaction(self._connection):
            self._connection.execute(
                "UPDATE equity_paper_targets SET client_order_id = ?,"
                " approved_quantity = ?, risk_reason_code = ? WHERE id = ?",
                (
                    outcome.client_order_id,
                    str(outcome.approved_quantity)
                    if outcome.approved_quantity is not None
                    else None,
                    outcome.risk_reason_code,
                    target_id,
                ),
            )

    def _record_cycle_audit(self, moment: datetime, report: EquityPaperCycleReport) -> None:
        counts: dict[str, int] = {}
        for outcome in report.outcomes:
            counts[outcome.disposition.value] = counts.get(outcome.disposition.value, 0) + 1
        summary = ", ".join(f"{name}={value}" for name, value in sorted(counts.items()))
        state.record_system_event(
            self._connection,
            event_timestamp=moment,
            event_type=EVENT_PAPER_CYCLE,
            message=(
                f"Equity EDA-1 PAPER cycle at stage {self._config.stage}: {summary}. "
                f"Sizing policy {self._config.policy.policy_id} "
                f"({self._config.policy.config_hash()[:12]}). Environment: PAPER ONLY."
            ),
        )


__all__ = [
    "EQUITY_PAPER_LOCK_SCOPE",
    "EVENT_PAPER_CYCLE",
    "EVENT_PAPER_PARITY_MISMATCH",
    "EVENT_PAPER_STARTED",
    "EVENT_PAPER_STOPPED",
    "PAPER_ACCOUNT_PREFIX",
    "PAPER_DECISION_ORDER",
    "PAPER_PANEL_ENGINE_VERSION",
    "ROLLOUT_STAGES",
    "SHADOW_PAPER_DECISION_MISMATCH",
    "STAGE_A",
    "STAGE_B",
    "STAGE_C",
    "AlpacaEquityPaperGateway",
    "Disposition",
    "EquityPaperRuntime",
    "EquityPaperConfig",
    "EquityPaperCycleReport",
    "EquityPaperError",
    "EquityPaperExecutionGateway",
    "ExternalSafetySource",
    "CREATE_PAPER_TARGETS",
    "SqliteExternalSafety",
    "create_paper_target_table",
    "NotPaperAccountError",
    "PaperIntegrityError",
    "ParityRecord",
    "ShadowParitySource",
    "SqliteShadowParity",
    "SymbolOutcome",
    "non_equity_exposure",
    "require_paper_account",
]
