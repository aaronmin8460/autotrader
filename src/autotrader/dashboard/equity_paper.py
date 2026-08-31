"""The Equity EDA-1 PAPER page: what it wants to hold, and what it actually holds.

Read-only, like every other page this package serves, and for the same reason:
a viewer that can act on what it displays is not a viewer. There is no route
here that starts, stops, stages, resizes or cancels anything, and the API that
exposes these panels is asserted GET-only in its own test.

**Three products, three truths, and the page must not blur them.** The Crypto
page shows a paper book that trades. The Equity Shadow page shows an
observation record whose portfolio figures are hypothetical and whose order
count is structurally zero. This page shows a paper book that trades *equities*
against the same account: its positions are real broker positions, its P&L is
real paper P&L, and neither may be added to the Shadow's hypothetical curve.
The labels are therefore part of the contract and not decoration.

**Exposure is account-wide.** The total exposure figure on this page is the
account's, not the equity book's: it includes the crypto position, because the
30% ceiling it is measured against is an account ceiling. Showing an
equity-only total next to an account-wide cap would misreport the headroom by
exactly the size of the crypto book.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from autotrader.equity import EQUITY_SYMBOLS
from autotrader.equity.allocation import AllocationPolicy
from autotrader.equity.paper import (
    EVENT_PAPER_CYCLE,
    EVENT_PAPER_PARITY_MISMATCH,
    EVENT_PAPER_STARTED,
    EVENT_PAPER_STOPPED,
    ROLLOUT_STAGES,
)

#: Where to read the equity paper record from.
PAPER_DATABASE_PATH_ENV = "AUTOTRADER_EQUITY_PAPER_DB"
DEFAULT_PAPER_DATABASE_PATH = Path("data/autotrader-equity-paper.db")

#: The crypto store, read for its position snapshot so the page can show the
#: account-wide exposure the 30% ceiling actually applies to.
CRYPTO_DATABASE_PATH_ENV = "AUTOTRADER_CRYPTO_DB"
DEFAULT_CRYPTO_DATABASE_PATH = Path("data/autotrader.db")

#: The shadow store, read for the parity comparison.
SHADOW_DATABASE_PATH_ENV = "AUTOTRADER_EQUITY_SHADOW_DB"
DEFAULT_SHADOW_DATABASE_PATH = Path("data/autotrader-shadow.db")

READ_TIMEOUT_SECONDS = 5.0

#: The banner this page must always carry. Paper is not a simulation and it is
#: not real money either, and a reader has to be told which.
PAPER_MODE = "ALPACA PAPER - NO REAL MONEY"

#: How stale the last cycle may be before the page calls the service quiet.
#: Two 15-minute boundaries plus the safety delay.
STALE_AFTER = timedelta(minutes=31)

#: Bounded history. A page never renders the whole table.
HISTORY_DEFAULT_LIMIT = 50
HISTORY_MAX_LIMIT = 500

UNAVAILABLE_DATABASE_UNREADABLE = "DATABASE_UNREADABLE"
UNAVAILABLE_NOT_STARTED = "SERVICE_NEVER_STARTED"

_ZERO = Decimal(0)


@contextmanager
def read_only_connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open `path` read-only and close it on exit.

    `mode=ro` makes every write an engine-level error rather than a convention,
    and `query_only` closes the same door from the other side. No
    `PRAGMA journal_mode` is issued: setting a journal mode writes to the
    database header, and a viewer has no business touching the journalling of a
    store another process owns - least of all the crypto store, whose schema
    version this package's normal open path would migrate.
    """
    uri = f"file:{quote(str(Path(path).resolve()))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=READ_TIMEOUT_SECONDS, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = 1")
        yield connection
    finally:
        connection.close()


def database_path() -> Path:
    configured = os.environ.get(PAPER_DATABASE_PATH_ENV)
    return Path(configured) if configured else DEFAULT_PAPER_DATABASE_PATH


def crypto_database_path() -> Path:
    configured = os.environ.get(CRYPTO_DATABASE_PATH_ENV)
    return Path(configured) if configured else DEFAULT_CRYPTO_DATABASE_PATH


def shadow_database_path() -> Path:
    configured = os.environ.get(SHADOW_DATABASE_PATH_ENV)
    return Path(configured) if configured else DEFAULT_SHADOW_DATABASE_PATH


def _parse(moment: str | None) -> datetime | None:
    if not moment:
        return None
    try:
        parsed = datetime.fromisoformat(moment)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - a stored value that is not a number reads as zero
        return _ZERO


@dataclass(frozen=True)
class PaperSnapshot:
    """One consistent read of the equity paper store, or why there is none."""

    ok: bool
    reason: str | None = None
    events: tuple[sqlite3.Row, ...] = ()
    intents: tuple[sqlite3.Row, ...] = ()
    broker_orders: tuple[sqlite3.Row, ...] = ()
    positions: tuple[sqlite3.Row, ...] = ()
    risk_events: tuple[sqlite3.Row, ...] = ()
    comparisons: tuple[sqlite3.Row, ...] = ()
    regimes: tuple[sqlite3.Row, ...] = ()
    reconciliation: tuple[sqlite3.Row, ...] = ()
    safety: sqlite3.Row | None = None
    unresolved_intents: int = 0


def read_paper(path: str | Path) -> PaperSnapshot:
    """Read everything one poll needs, in one short read transaction.

    Any failure - a missing file, a store locked beyond the timeout, a schema
    this reader does not recognize - returns `ok=False` rather than raising.
    There is deliberately no repair path and no schema creation: a viewer that
    would build the store it is reading is a viewer that has written to it.
    """
    try:
        with read_only_connection(path) as connection:
            connection.execute("BEGIN DEFERRED")
            events = tuple(
                connection.execute(
                    "SELECT event_timestamp, event_type, message FROM system_events"
                    " WHERE event_type IN (?, ?, ?, ?)"
                    " ORDER BY event_timestamp DESC LIMIT 200",
                    (
                        EVENT_PAPER_STARTED,
                        EVENT_PAPER_STOPPED,
                        EVENT_PAPER_CYCLE,
                        EVENT_PAPER_PARITY_MISMATCH,
                    ),
                ).fetchall()
            )
            intents = tuple(
                connection.execute(
                    "SELECT id, client_order_id, created_at, symbol, side,"
                    " requested_quantity, approved_quantity, reference_price,"
                    " risk_reason_code, status, updated_at"
                    " FROM order_intents ORDER BY id DESC LIMIT 200"
                ).fetchall()
            )
            broker_orders = tuple(
                connection.execute(
                    "SELECT client_order_id, symbol, side, quantity, filled_quantity,"
                    " filled_average_price, status, submitted_at, filled_at"
                    " FROM broker_orders ORDER BY id DESC LIMIT 200"
                ).fetchall()
            )
            positions = tuple(
                connection.execute(
                    "SELECT symbol, quantity, average_price, updated_at FROM positions"
                ).fetchall()
            )
            risk_events = tuple(
                connection.execute(
                    "SELECT event_timestamp, decision, reason_code, symbol, message"
                    " FROM risk_events ORDER BY id DESC LIMIT 200"
                ).fetchall()
            )
            comparisons = tuple(
                connection.execute(
                    "SELECT symbol, bar_timestamp, session_date, participate,"
                    " v3_signal, v3_stance, eda1_signal, eda1_stance, signals_agree,"
                    " stances_agree, reference_close"
                    " FROM shadow_side_by_side ORDER BY bar_timestamp DESC, symbol LIMIT 500"
                ).fetchall()
            )
            regimes = tuple(
                connection.execute(
                    "SELECT session_date, participate, info_close, info_sma,"
                    " info_drawdown, sessions_observed, sma_sessions, calm_threshold,"
                    " lag_sessions, reference_symbol"
                    " FROM shadow_regime_state ORDER BY session_date DESC LIMIT 10"
                ).fetchall()
            )
            reconciliation = tuple(
                connection.execute(
                    "SELECT id, completed_at, status, safe_to_trade, orders_checked,"
                    " positions_checked, unresolved_count"
                    " FROM reconciliation_runs ORDER BY id DESC LIMIT 5"
                ).fetchall()
            )
            safety = connection.execute(
                "SELECT state, reason, source, updated_at FROM account_safety_state WHERE id = 1"
            ).fetchone()
            unresolved = connection.execute(
                "SELECT COUNT(*) FROM order_intents"
                " WHERE status IN ('CREATED', 'SUBMITTING', 'UNKNOWN')"
            ).fetchone()[0]
    except sqlite3.Error:
        return PaperSnapshot(ok=False, reason=UNAVAILABLE_DATABASE_UNREADABLE)

    return PaperSnapshot(
        ok=True,
        events=events,
        intents=intents,
        broker_orders=broker_orders,
        positions=positions,
        risk_events=risk_events,
        comparisons=comparisons,
        regimes=regimes,
        reconciliation=reconciliation,
        safety=safety,
        unresolved_intents=int(unresolved),
    )


def read_crypto_exposure(path: str | Path) -> tuple[str, ...]:
    """The **non-equity** positions the crypto store knows about, read-only.

    A single SELECT through a `mode=ro` URI. The crypto store is schema 6 and
    this package is schema 7; opening it through the normal path would migrate
    it, and a dashboard poll is the last place that should happen.

    Filtered to non-equity symbols on purpose. That store's `positions` table
    holds a snapshot of the **whole account**, not of the crypto book: a
    reconciliation pass covers all twelve tracked symbols, because only a
    full-universe pass can clear the shared account halt, and it records
    holdings outside its own universe as observed rather than ignoring them.
    Rendering that table unfiltered under the label "crypto" would show this
    page the equity positions twice and call one of them crypto.
    """
    equity = {symbol.upper() for symbol in EQUITY_SYMBOLS}
    rows: list[str] = []
    try:
        with read_only_connection(path) as connection:
            for row in connection.execute(
                "SELECT symbol, quantity FROM positions ORDER BY symbol"
            ).fetchall():
                if str(row["symbol"]).upper() in equity:
                    continue
                if _decimal(row["quantity"]) <= _ZERO:
                    continue
                rows.append(f"{row['symbol']}:{row['quantity']}")
    except sqlite3.Error:
        return ()
    return tuple(rows)


@dataclass(frozen=True)
class ServicePanel:
    """Whether the paper runtime is running, and under what settings."""

    mode: str
    environment: str
    running: bool
    stale: bool
    stage: str | None
    execution_universe: tuple[str, ...]
    decision_universe: tuple[str, ...]
    sizing_policy: str | None
    sizing_config_hash: str | None
    started_at: str | None
    stopped_at: str | None
    last_cycle_at: str | None
    unresolved_intents: int
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class RegimePanel:
    """The EDA-1 router's answer for the current session, and its evidence."""

    session_date: str | None
    participate: bool | None
    reference_symbol: str | None
    info_close: float | None
    info_sma: float | None
    info_drawdown: float | None
    sessions_observed: int | None
    spec: dict[str, object]


@dataclass(frozen=True)
class ExposurePanel:
    """Account-wide exposure, because the ceiling it is measured against is."""

    account_equity: float | None
    crypto_positions: tuple[str, ...]
    equity_positions: tuple[str, ...]
    #: When the equity snapshot was last written by a reconciliation pass. The
    #: table is a last-known snapshot, not live broker truth, and a page that
    #: did not say so would read as though it were.
    equity_positions_as_of: str | None
    equity_exposure_note: str
    per_symbol_cap: str
    total_account_cap: str
    daily_loss_halt: str


@dataclass(frozen=True)
class TargetRow:
    """One symbol: what EDA-1 wants, what the account holds, what Risk allowed."""

    symbol: str
    in_execution_universe: bool
    bar_timestamp: str | None
    participate: bool | None
    eda1_signal: str | None
    eda1_stance: int | None
    v3_signal: str | None
    stances_agree: bool | None
    reference_close: float | None
    actual_quantity: str
    last_risk_reason: str | None


@dataclass(frozen=True)
class OrderRow:
    client_order_id: str
    symbol: str
    side: str
    requested_quantity: str
    approved_quantity: str
    status: str
    risk_reason_code: str
    created_at: str | None
    broker_status: str | None
    filled_quantity: str | None
    filled_average_price: float | None


@dataclass(frozen=True)
class SafetyPanel:
    account_safety: str | None
    account_safety_reason: str | None
    reconciliation_status: str | None
    reconciliation_at: str | None
    reconciliation_unresolved: int | None
    #: Cumulative since this store was created, not since the last cycle. A
    #: page showing a per-cycle count would let a standing disagreement vanish
    #: from view the moment it stopped recurring.
    parity_mismatches: int
    risk_blocked_recent: tuple[str, ...]


@dataclass(frozen=True)
class EquityPaperOverview:
    """The whole page, one poll, one instant of the record."""

    generated_at: str
    mode: str
    read_only: bool
    service: ServicePanel
    regime: RegimePanel
    exposure: ExposurePanel
    targets: tuple[TargetRow, ...]
    orders: tuple[OrderRow, ...]
    safety: SafetyPanel


def _latest_event(snapshot: PaperSnapshot, event_type: str) -> sqlite3.Row | None:
    for row in snapshot.events:
        if row["event_type"] == event_type:
            return row
    return None


def _field_from_message(message: str, prefix: str, suffix: str) -> str | None:
    """Pull one value out of an audit message without inventing a parser.

    The started event states the stage, the universe and the policy in prose
    because that message is also what an operator reads in the journal. Pulling
    them back out is deliberately forgiving: a message that does not match
    yields None and the panel says "unknown" rather than guessing.
    """
    start = message.find(prefix)
    if start < 0:
        return None
    start += len(prefix)
    end = message.find(suffix, start)
    if end < 0:
        return None
    return message[start:end].strip() or None


def build_service(snapshot: PaperSnapshot, *, now: datetime) -> ServicePanel:
    if not snapshot.ok:
        return ServicePanel(
            mode=PAPER_MODE,
            environment="PAPER",
            running=False,
            stale=True,
            stage=None,
            execution_universe=(),
            decision_universe=EQUITY_SYMBOLS,
            sizing_policy=None,
            sizing_config_hash=None,
            started_at=None,
            stopped_at=None,
            last_cycle_at=None,
            unresolved_intents=0,
            unavailable_reason=snapshot.reason,
        )

    started = _latest_event(snapshot, EVENT_PAPER_STARTED)
    stopped = _latest_event(snapshot, EVENT_PAPER_STOPPED)
    cycle = _latest_event(snapshot, EVENT_PAPER_CYCLE)
    if started is None:
        return ServicePanel(
            mode=PAPER_MODE,
            environment="PAPER",
            running=False,
            stale=True,
            stage=None,
            execution_universe=(),
            decision_universe=EQUITY_SYMBOLS,
            sizing_policy=None,
            sizing_config_hash=None,
            started_at=None,
            stopped_at=None,
            last_cycle_at=None,
            unresolved_intents=snapshot.unresolved_intents,
            unavailable_reason=UNAVAILABLE_NOT_STARTED,
        )

    started_at = _parse(started["event_timestamp"])
    stopped_at = _parse(stopped["event_timestamp"]) if stopped is not None else None
    cycle_at = _parse(cycle["event_timestamp"]) if cycle is not None else None
    running = stopped_at is None or (started_at is not None and started_at > stopped_at)
    stale = cycle_at is None or (now - cycle_at) > STALE_AFTER

    message = str(started["message"])
    stage = _field_from_message(message, "rollout stage ", " (")
    policy = _field_from_message(message, "Sizing policy ", " (")
    config_hash = _field_from_message(message, f"{policy} (", ")") if policy else None
    universe = ROLLOUT_STAGES.get(stage or "", ())

    return ServicePanel(
        mode=PAPER_MODE,
        environment="PAPER",
        running=running,
        stale=stale,
        stage=stage,
        execution_universe=universe,
        decision_universe=EQUITY_SYMBOLS,
        sizing_policy=policy,
        sizing_config_hash=config_hash,
        started_at=_iso(started_at),
        stopped_at=_iso(stopped_at),
        last_cycle_at=_iso(cycle_at),
        unresolved_intents=snapshot.unresolved_intents,
    )


def build_regime(snapshot: PaperSnapshot) -> RegimePanel:
    if not snapshot.ok or not snapshot.regimes:
        return RegimePanel(
            session_date=None,
            participate=None,
            reference_symbol=None,
            info_close=None,
            info_sma=None,
            info_drawdown=None,
            sessions_observed=None,
            spec={},
        )
    row = snapshot.regimes[0]
    return RegimePanel(
        session_date=str(row["session_date"]),
        participate=bool(row["participate"]),
        reference_symbol=str(row["reference_symbol"]),
        info_close=row["info_close"],
        info_sma=row["info_sma"],
        info_drawdown=row["info_drawdown"],
        sessions_observed=int(row["sessions_observed"]),
        spec={
            "sma_sessions": int(row["sma_sessions"]),
            "calm_threshold": float(row["calm_threshold"]),
            "lag_sessions": int(row["lag_sessions"]),
        },
    )


def build_exposure(
    snapshot: PaperSnapshot,
    *,
    policy: AllocationPolicy,
    crypto_positions: Sequence[str],
) -> ExposurePanel:
    held = [
        row
        for row in snapshot.positions
        if str(row["symbol"]) in EQUITY_SYMBOLS and _decimal(row["quantity"]) > _ZERO
    ]
    equity_rows = tuple(f"{row['symbol']}:{row['quantity']}" for row in held)
    as_of = max((str(row["updated_at"]) for row in held), default=None)
    return ExposurePanel(
        account_equity=None,
        crypto_positions=tuple(crypto_positions),
        equity_positions=equity_rows,
        equity_positions_as_of=as_of,
        equity_exposure_note=(
            "Positions here are the last snapshot a reconciliation pass wrote, not "
            "live broker truth, so a fill since that pass is not shown yet. Exposure "
            "percentages are enforced against BROKER truth at submission time, not "
            "against this snapshot. The total ceiling is an ACCOUNT ceiling and "
            "includes the crypto book."
        ),
        per_symbol_cap=f"{policy.per_symbol_cap:%}",
        total_account_cap=f"{policy.total_cap:%}",
        daily_loss_halt="2%",
    )


def build_targets(
    snapshot: PaperSnapshot, *, execution_universe: Sequence[str]
) -> tuple[TargetRow, ...]:
    """The newest recorded bar per symbol, with the account's actual holding."""
    latest: dict[str, sqlite3.Row] = {}
    for row in snapshot.comparisons:
        symbol = str(row["symbol"])
        if symbol not in latest:
            latest[symbol] = row
    held = {str(row["symbol"]): str(row["quantity"]) for row in snapshot.positions}
    risk_by_symbol: dict[str, str] = {}
    for row in snapshot.risk_events:
        symbol = str(row["symbol"] or "")
        if symbol and symbol not in risk_by_symbol:
            risk_by_symbol[symbol] = str(row["reason_code"])

    rows: list[TargetRow] = []
    for symbol in EQUITY_SYMBOLS:
        record = latest.get(symbol)
        rows.append(
            TargetRow(
                symbol=symbol,
                in_execution_universe=symbol in execution_universe,
                bar_timestamp=str(record["bar_timestamp"]) if record is not None else None,
                participate=bool(record["participate"]) if record is not None else None,
                eda1_signal=str(record["eda1_signal"]) if record is not None else None,
                eda1_stance=int(record["eda1_stance"]) if record is not None else None,
                v3_signal=str(record["v3_signal"]) if record is not None else None,
                stances_agree=bool(record["stances_agree"]) if record is not None else None,
                reference_close=record["reference_close"] if record is not None else None,
                actual_quantity=held.get(symbol, "0"),
                last_risk_reason=risk_by_symbol.get(symbol),
            )
        )
    return tuple(rows)


def build_orders(
    snapshot: PaperSnapshot, *, limit: int = HISTORY_DEFAULT_LIMIT
) -> tuple[OrderRow, ...]:
    by_client_id = {str(row["client_order_id"]): row for row in snapshot.broker_orders}
    rows: list[OrderRow] = []
    for intent in snapshot.intents[:limit]:
        client_order_id = str(intent["client_order_id"])
        broker = by_client_id.get(client_order_id)
        rows.append(
            OrderRow(
                client_order_id=client_order_id,
                symbol=str(intent["symbol"]),
                side=str(intent["side"]),
                requested_quantity=str(intent["requested_quantity"]),
                approved_quantity=str(intent["approved_quantity"]),
                status=str(intent["status"]),
                risk_reason_code=str(intent["risk_reason_code"]),
                created_at=str(intent["created_at"]),
                broker_status=str(broker["status"]) if broker is not None else None,
                filled_quantity=str(broker["filled_quantity"]) if broker is not None else None,
                filled_average_price=(
                    broker["filled_average_price"] if broker is not None else None
                ),
            )
        )
    return tuple(rows)


def build_safety(snapshot: PaperSnapshot) -> SafetyPanel:
    reconciliation = snapshot.reconciliation[0] if snapshot.reconciliation else None
    mismatches = sum(
        1 for row in snapshot.events if row["event_type"] == EVENT_PAPER_PARITY_MISMATCH
    )
    blocked = tuple(
        f"{row['symbol']}:{row['reason_code']}"
        for row in snapshot.risk_events
        if str(row["decision"]) == "REJECTED"
    )[:20]
    return SafetyPanel(
        account_safety=str(snapshot.safety["state"]) if snapshot.safety is not None else None,
        account_safety_reason=(
            str(snapshot.safety["reason"]) if snapshot.safety is not None else None
        ),
        reconciliation_status=(
            str(reconciliation["status"]) if reconciliation is not None else None
        ),
        reconciliation_at=(
            str(reconciliation["completed_at"]) if reconciliation is not None else None
        ),
        reconciliation_unresolved=(
            int(reconciliation["unresolved_count"]) if reconciliation is not None else None
        ),
        parity_mismatches=mismatches,
        risk_blocked_recent=blocked,
    )


def build_overview(
    *,
    path: str | Path | None = None,
    crypto_path: str | Path | None = None,
    now: datetime | None = None,
    policy: AllocationPolicy | None = None,
) -> EquityPaperOverview:
    """One poll of the Equity Paper page."""
    moment = now if now is not None else datetime.now(UTC)
    snapshot = read_paper(path if path is not None else database_path())
    service = build_service(snapshot, now=moment)
    resolved_policy = policy
    if resolved_policy is None and service.sizing_policy:
        try:
            resolved_policy = AllocationPolicy(policy_id=service.sizing_policy)
        except Exception:  # noqa: BLE001 - an unknown policy name reads as unknown
            resolved_policy = None
    if resolved_policy is None:
        # The caps are the Risk Engine's whatever the page knows about the
        # policy, so an unreadable policy name never understates a ceiling.
        resolved_policy = AllocationPolicy(policy_id="C_RESERVED_UNIVERSE")
    crypto = read_crypto_exposure(
        crypto_path if crypto_path is not None else crypto_database_path()
    )
    return EquityPaperOverview(
        generated_at=moment.isoformat(),
        mode=PAPER_MODE,
        read_only=True,
        service=service,
        regime=build_regime(snapshot),
        exposure=build_exposure(snapshot, policy=resolved_policy, crypto_positions=crypto),
        targets=build_targets(snapshot, execution_universe=service.execution_universe),
        orders=build_orders(snapshot),
        safety=build_safety(snapshot),
    )


__all__ = [
    "CRYPTO_DATABASE_PATH_ENV",
    "DEFAULT_PAPER_DATABASE_PATH",
    "HISTORY_DEFAULT_LIMIT",
    "HISTORY_MAX_LIMIT",
    "PAPER_DATABASE_PATH_ENV",
    "PAPER_MODE",
    "STALE_AFTER",
    "UNAVAILABLE_DATABASE_UNREADABLE",
    "UNAVAILABLE_NOT_STARTED",
    "EquityPaperOverview",
    "ExposurePanel",
    "OrderRow",
    "PaperSnapshot",
    "RegimePanel",
    "SafetyPanel",
    "ServicePanel",
    "TargetRow",
    "build_exposure",
    "build_orders",
    "build_overview",
    "build_regime",
    "build_safety",
    "build_service",
    "build_targets",
    "crypto_database_path",
    "database_path",
    "read_crypto_exposure",
    "read_only_connection",
    "read_paper",
    "shadow_database_path",
]
