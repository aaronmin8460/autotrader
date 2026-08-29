"""The go / no-go check run immediately before a Combined Paper Smoke.

One command, one answer: `READY_FOR_PAPER_SMOKE` or `BLOCKED`. Every input is
read, nothing is written except - when asked - the baseline snapshot, and no
gate here can be argued with by a flag.

**Read and repair are kept apart.** Production reconciliation may rewrite local
SQLite from broker truth, which is the right thing for it to do and the wrong
thing for an inspection to do behind an operator's back. So this command reads
the *latest persisted* reconciliation run and, when that is missing, stale, or
not green, prints the command the operator should run themselves. It never
starts a pass.

**Every gate fails closed.** A check that cannot be answered - an unreadable
database, a broker that will not respond - is a `FAIL`, not a skipped line. The
question this command answers is "may I place a real paper order now?", and the
only safe reading of "I could not tell" is no.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from autotrader.execution.models import format_quantity
from autotrader.execution.paper import broker_symbol_key
from autotrader.smoke import broker, cleanup, health, tracking
from autotrader.smoke.baseline import Baseline
from autotrader.smoke.gitinfo import GitState
from autotrader.smoke.models import (
    BrokerReadClient,
    BrokerUnreadableError,
    CheckResult,
    DashboardHealth,
    GateReport,
    PositionSnapshot,
    RuntimeHealth,
    SmokeVerdict,
)
from autotrader.smoke.readonly import (
    is_query_only,
    journal_mode,
    normalize_smoke_symbol,
    schema_version,
)
from autotrader.state import sqlite as state

#: How old the latest reconciliation run may be and still count as evidence
#: about *now*. Beyond this the preflight blocks and asks for a fresh pass:
#: positions and orders move, and a green result from yesterday says nothing
#: about the account an order is about to be sent to.
DEFAULT_RECONCILIATION_MAX_AGE = timedelta(hours=1)

#: The reconciliation conclusions that permit trading. Mirrors
#: `reconciliation.models.SAFE_TO_TRADE_STATUSES`, restated over the *stored*
#: row because that is what this command reads.
_GREEN_RECONCILIATION = (
    state.RECONCILIATION_STATUS_CLEAN,
    state.RECONCILIATION_STATUS_REPAIRED,
)

#: Printed whenever reconciliation needs running. The operator runs it; this
#: harness does not, because that pass writes.
RECONCILE_COMMAND = "autotrader reconcile --db {database}"


@dataclass(frozen=True)
class PreflightReport:
    """Everything the preflight looked at, and the one answer it produced."""

    gate: GateReport
    git: GitState
    universe: tuple[str, ...]
    universe_origin: str
    positions: dict[str, PositionSnapshot]
    account_equity: float | None
    account_cash: float | None
    account_status: str | None
    runtime: tuple[RuntimeHealth, ...]
    dashboard: DashboardHealth
    latest_reconciliation: state.ReconciliationRun | None
    account_safety: state.AccountSafetyState | None
    open_intents: tuple[state.StoredOrderIntent, ...]
    unknown_intents: tuple[state.StoredOrderIntent, ...]
    paper_gate_open: bool
    entry_minimum: Decimal | None = None
    entry_note: str | None = None
    entry_dry_run_command: str | None = None
    smoke_symbol: str | None = None

    @property
    def ready(self) -> bool:
        return self.gate.ready

    def verdict_text(self) -> str:
        return self.gate.verdict_text()

    def to_baseline(self, *, database_path: str | None, schema: int | None) -> Baseline:
        """The snapshot this preflight would write.

        Built from the numbers already read, so the snapshot describes the same
        moment the verdict does rather than a second, later look at the account.
        """
        return Baseline(
            captured_at=datetime.now(UTC),
            universe=self.universe,
            positions={
                symbol: position.quantity for symbol, position in sorted(self.positions.items())
            },
            account_equity=self.account_equity,
            account_cash=self.account_cash,
            account_status=self.account_status,
            git_branch=self.git.branch,
            git_sha=self.git.sha,
            git_dirty=self.git.dirty,
            database_path=database_path,
            schema_version=schema,
            open_order_client_ids=tuple(intent.client_order_id for intent in self.open_intents),
            unknown_order_client_ids=tuple(
                intent.client_order_id for intent in self.unknown_intents
            ),
            reconciliation_run_id=(
                self.latest_reconciliation.id if self.latest_reconciliation else None
            ),
            reconciliation_status=(
                self.latest_reconciliation.status if self.latest_reconciliation else None
            ),
            reconciliation_safe_to_trade=(
                self.latest_reconciliation.safe_to_trade if self.latest_reconciliation else None
            ),
            account_safety_state=(self.account_safety.state if self.account_safety else None),
            account_safety_safe_to_trade=(
                self.account_safety.safe_to_trade if self.account_safety else None
            ),
        )


def run_preflight(
    *,
    client: BrokerReadClient | None,
    connection: sqlite3.Connection,
    database_path: Path | str,
    git: GitState,
    universe: Sequence[str],
    universe_origin: str,
    smoke_symbol: str | None = None,
    allow_dirty: bool = False,
    dashboard_url: str | None = None,
    now: datetime | None = None,
    stale_after: timedelta = health.DEFAULT_STALE_AFTER,
    reconciliation_max_age: timedelta = DEFAULT_RECONCILIATION_MAX_AGE,
    broker_error: str | None = None,
) -> PreflightReport:
    """Run every gate and return the report. Reads only; writes nothing.

    `client` may be None when the broker could not be constructed at all, with
    `broker_error` carrying why. That is a `FAIL`, not a crash: an operator
    whose credentials are missing should see the whole preflight - the database
    checks, the reconciliation state, the git commit - alongside the one thing
    that is wrong, rather than a traceback about the first thing that failed.
    """
    moment = now or datetime.now(UTC)
    checks: list[CheckResult] = []

    checks.append(_git_check(git, allow_dirty=allow_dirty))
    checks.append(_credentials_check())
    paper_gate = broker.paper_gate_open()

    positions, account, broker_checks = _broker_checks(client, broker_error, universe)
    checks.extend(broker_checks)

    database = Path(database_path)
    checks.extend(_database_checks(connection, database))

    open_rows = tracking.open_intents(connection)
    unknown_rows = tracking.unknown_intents(connection)
    checks.append(_open_orders_check(open_rows))
    checks.append(_unknown_orders_check(unknown_rows))

    latest = tracking.latest_reconciliation(connection)
    checks.append(_reconciliation_check(latest, database, moment, reconciliation_max_age))

    safety, safety_check = _account_safety_check(connection, database)
    checks.append(safety_check)

    runtime = health.runtime_health(connection, universe, now=moment, stale_after=stale_after)
    checks.append(_runtime_check(runtime))

    dashboard = health.dashboard_health(dashboard_url)
    checks.append(_dashboard_check(dashboard))

    entry_minimum, entry_note, entry_command = _entry_plan(client, smoke_symbol, database)

    return PreflightReport(
        gate=GateReport(checks=tuple(checks)),
        git=git,
        universe=tuple(normalize_smoke_symbol(symbol) for symbol in universe),
        universe_origin=universe_origin,
        positions=positions,
        account_equity=account.equity if account else None,
        account_cash=account.cash if account else None,
        account_status=account.status if account else None,
        runtime=runtime,
        dashboard=dashboard,
        latest_reconciliation=latest,
        account_safety=safety,
        open_intents=open_rows,
        unknown_intents=unknown_rows,
        paper_gate_open=paper_gate,
        entry_minimum=entry_minimum,
        entry_note=entry_note,
        entry_dry_run_command=entry_command,
        smoke_symbol=normalize_smoke_symbol(smoke_symbol) if smoke_symbol else None,
    )


def _git_check(git: GitState, *, allow_dirty: bool) -> CheckResult:
    """The commit a smoke's result will be attributed to.

    A dirty tree blocks by default. The whole value of a smoke is being able to
    say "this commit places and closes a paper order correctly", and that
    sentence is false if the working tree held changes the SHA does not
    describe. `--allow-dirty` downgrades it for an operator who has a reason;
    it relaxes a *reporting* requirement and enables no action.
    """
    if not git.known:
        return CheckResult("git", SmokeVerdict.FAIL, git.detail)
    location = f"{git.branch or 'DETACHED'} @ {git.short_sha}"
    if git.dirty is None:
        return CheckResult(
            "git",
            SmokeVerdict.FAIL,
            f"{location}: the working tree state could not be determined.",
        )
    if git.dirty and not allow_dirty:
        return CheckResult(
            "git",
            SmokeVerdict.FAIL,
            f"{location}: the working tree has uncommitted changes, so this SHA does "
            "not describe what would run. Commit or stash first, or pass --allow-dirty "
            "to record the smoke against a tree you know is modified.",
        )
    if git.dirty:
        return CheckResult(
            "git",
            SmokeVerdict.PASS,
            f"{location}: DIRTY, accepted via --allow-dirty. The recorded SHA does not "
            "fully describe what will run.",
        )
    return CheckResult("git", SmokeVerdict.PASS, f"{location}: clean.")


def _credentials_check() -> CheckResult:
    """Presence only. No value is read into this process for reporting."""
    if broker.credentials_present():
        return CheckResult(
            "credentials",
            SmokeVerdict.PASS,
            "ALPACA_API_KEY and ALPACA_SECRET_KEY are SET. Their values are never read, "
            "printed, or written to a snapshot by this harness.",
        )
    return CheckResult(
        "credentials",
        SmokeVerdict.FAIL,
        "ALPACA_API_KEY and/or ALPACA_SECRET_KEY are NOT SET, so the broker cannot be "
        "read and no smoke can be verified.",
    )


def _broker_checks(
    client: BrokerReadClient | None, broker_error: str | None, universe: Sequence[str]
) -> tuple[dict[str, PositionSnapshot], object, list[CheckResult]]:
    """Account reachability, tradability, and the tracked position list."""
    if client is None:
        detail = broker_error or "The paper trading client could not be constructed."
        return (
            {},
            None,
            [
                CheckResult("broker.paper_environment", SmokeVerdict.FAIL, detail),
                CheckResult("broker.account", SmokeVerdict.FAIL, "Not reached: no broker client."),
                CheckResult(
                    "broker.positions", SmokeVerdict.FAIL, "Not reached: no broker client."
                ),
            ],
        )

    checks = [
        CheckResult(
            "broker.paper_environment",
            SmokeVerdict.PASS,
            "The trading client was proven to reach the Alpaca PAPER environment.",
        )
    ]

    try:
        account = broker.read_account(client)
    except BrokerUnreadableError as error:
        return (
            {},
            None,
            [
                *checks,
                CheckResult("broker.account", SmokeVerdict.FAIL, str(error)),
                CheckResult(
                    "broker.positions",
                    SmokeVerdict.FAIL,
                    "Not reached: the account could not be read.",
                ),
            ],
        )

    if account.tradable:
        checks.append(
            CheckResult(
                "broker.account",
                SmokeVerdict.PASS,
                f"status={account.status}, equity={account.equity}, cash={account.cash}, tradable.",
            )
        )
    else:
        checks.append(
            CheckResult(
                "broker.account",
                SmokeVerdict.FAIL,
                f"The paper account cannot trade (status={account.status}, "
                f"trading_blocked={account.trading_blocked}, "
                f"account_blocked={account.account_blocked}, "
                f"trade_suspended_by_user={account.trade_suspended_by_user}).",
            )
        )

    try:
        positions = broker.read_positions(client)
    except BrokerUnreadableError as error:
        checks.append(CheckResult("broker.positions", SmokeVerdict.FAIL, str(error)))
        return {}, account, checks

    tracked = {
        symbol: broker.position_for(positions, symbol)
        for symbol in (normalize_smoke_symbol(item) for item in universe)
    }
    # Per market, not per spelling. `positions` is keyed by `broker_symbol_key`
    # and `tracked` by the universe's canonical names, so a plain set difference
    # reports BTCUSD as an untracked holding alongside the BTC/USD it *is*.
    tracked_keys = {broker_symbol_key(symbol) for symbol in tracked}
    untracked = sorted(
        position.symbol for key, position in positions.items() if key not in tracked_keys
    )
    summary = ", ".join(
        f"{symbol}={format_quantity(position.quantity)}"
        for symbol, position in sorted(tracked.items())
    )
    detail = f"Broker positions for the tracked universe: {summary or 'none'}."
    if untracked:
        detail += (
            f" The account also holds untracked positions in {', '.join(untracked)}; they "
            "are reported but not part of this smoke's exposure comparison."
        )
    checks.append(CheckResult("broker.positions", SmokeVerdict.PASS, detail))
    return tracked, account, checks


def _database_checks(connection: sqlite3.Connection, database: Path) -> list[CheckResult]:
    """Readability, schema version, journal mode, and the read-only guarantee."""
    checks: list[CheckResult] = []
    version = schema_version(connection)
    if version is None:
        checks.append(
            CheckResult(
                "sqlite.schema",
                SmokeVerdict.FAIL,
                f"{database} has no schema_metadata table, so it is not an autotrader "
                "operational database. Nothing was created or migrated.",
            )
        )
    elif version != state.SCHEMA_VERSION:
        checks.append(
            CheckResult(
                "sqlite.schema",
                SmokeVerdict.FAIL,
                f"{database} is at schema v{version}; this build expects "
                f"v{state.SCHEMA_VERSION}. The harness will not migrate it - opening it "
                "read-write is what applies a migration, and an audit must not change "
                "what it audits. Run any writing autotrader command to migrate.",
            )
        )
    else:
        checks.append(
            CheckResult(
                "sqlite.schema",
                SmokeVerdict.PASS,
                f"{database} is readable at schema v{version}.",
            )
        )

    mode = journal_mode(connection)
    checks.append(
        CheckResult(
            "sqlite.journal_mode",
            SmokeVerdict.PASS if mode == "wal" else SmokeVerdict.FAIL,
            f"journal_mode={mode}"
            + (
                "."
                if mode == "wal"
                else ". The runtime writes this database in WAL; a different mode means "
                "this is not the file the runtime uses, or it was rewritten by something "
                "else."
            ),
        )
    )

    checks.append(
        CheckResult(
            "sqlite.read_only",
            SmokeVerdict.PASS if is_query_only(connection) else SmokeVerdict.FAIL,
            "The connection is query_only and was opened with mode=ro, so this command "
            "cannot write to the database."
            if is_query_only(connection)
            else "The connection is NOT query_only. Refusing to treat this as a read-only audit.",
        )
    )
    return checks


def _open_orders_check(open_rows: Sequence[state.StoredOrderIntent]) -> CheckResult:
    """A smoke must start from an empty order book, or its result is unreadable."""
    if not open_rows:
        return CheckResult(
            "orders.open", SmokeVerdict.PASS, "No tracked order intents are still open."
        )
    described = ", ".join(
        f"{intent.client_order_id} ({intent.symbol} {intent.side} {intent.status})"
        for intent in open_rows
    )
    return CheckResult(
        "orders.open",
        SmokeVerdict.FAIL,
        f"{len(open_rows)} tracked order intent(s) are still open: {described}. Settle "
        "them before starting a smoke - otherwise the smoke's own order cannot be told "
        "apart from what was already there.",
    )


def _unknown_orders_check(unknown_rows: Sequence[state.StoredOrderIntent]) -> CheckResult:
    """The hardest block. An UNKNOWN intent may already be a live order."""
    if not unknown_rows:
        return CheckResult(
            "orders.unknown", SmokeVerdict.PASS, "No tracked order intent is UNKNOWN."
        )
    described = ", ".join(intent.client_order_id for intent in unknown_rows)
    return CheckResult(
        "orders.unknown",
        SmokeVerdict.FAIL,
        f"{len(unknown_rows)} order intent(s) are UNKNOWN: {described}. An order may "
        "exist at the broker under each of these keys. Resolve them with `autotrader "
        "reconcile`, which asks the broker about the same client_order_id and never "
        "sends a second order. DO NOT RETRY THE ORIGINAL ORDER.",
    )


def _reconciliation_check(
    latest: state.ReconciliationRun | None,
    database: Path,
    now: datetime,
    max_age: timedelta,
) -> CheckResult:
    """Read the last persisted pass. Never start one.

    Starting a pass would repair local state as a side effect of an inspection.
    So this reports what is on record and, when that is not good enough, prints
    the command for the operator to run deliberately.
    """
    command = RECONCILE_COMMAND.format(database=database)
    if latest is None:
        return CheckResult(
            "reconciliation",
            SmokeVerdict.FAIL,
            f"No reconciliation run has ever completed against this database. Run it "
            f"yourself - this harness will not, because that pass writes: {command}",
        )
    age = now - latest.completed_at
    if latest.status not in _GREEN_RECONCILIATION or not latest.safe_to_trade:
        return CheckResult(
            "reconciliation",
            SmokeVerdict.FAIL,
            f"The latest reconciliation run (#{latest.id}, completed "
            f"{latest.completed_at.isoformat()}) concluded {latest.status} with "
            f"safe_to_trade={latest.safe_to_trade} and "
            f"{latest.unresolved_count} unresolved item(s). Trading is not safe. "
            f"Resolve it, then re-run: {command}",
        )
    if age > max_age:
        return CheckResult(
            "reconciliation",
            SmokeVerdict.FAIL,
            f"The latest reconciliation run (#{latest.id}) concluded {latest.status} but "
            f"finished {_describe_age(age)} ago, longer than the {_describe_age(max_age)} "
            f"this check accepts. Positions and orders move; a stale green result is not "
            f"evidence about the account an order is about to reach. Run: {command}",
        )
    return CheckResult(
        "reconciliation",
        SmokeVerdict.PASS,
        f"Run #{latest.id} concluded {latest.status}, safe_to_trade=True, "
        f"{_describe_age(age)} ago ({latest.orders_checked} order(s), "
        f"{latest.positions_checked} position(s) checked).",
    )


def _account_safety_check(
    connection: sqlite3.Connection, database: Path
) -> tuple[state.AccountSafetyState | None, CheckResult]:
    """The shared account halt: the gate both runtimes actually pass through.

    Distinct from the reconciliation check above, and both are kept. That one
    asks whether the last pass concluded something green and recently enough to
    be evidence; this one asks whether the account is *currently* open for
    business. They can disagree in the direction that matters: a green pass
    narrower than the tracked universe leaves an existing halt standing, so a
    preflight that read only the pass would report ready for a smoke that the
    execution boundary would then refuse.

    A halt is a `FAIL`, never a warning, and the harness will not lift one -
    only a full-universe reconciliation does that. The recorded
    `client_order_id` is surfaced when there is one, because it is the exact
    key an operator has to ask the broker about.
    """
    try:
        safety = tracking.account_safety(connection)
    except sqlite3.Error as error:
        return None, CheckResult(
            "account.safety",
            SmokeVerdict.FAIL,
            f"The shared account safety state could not be read from {database} "
            f"({error}). It is the gate every submission passes through, so an "
            "unreadable one is treated as unsafe rather than assumed open.",
        )

    if not safety.established:
        return safety, CheckResult(
            "account.safety",
            SmokeVerdict.FAIL,
            "No reconciliation has ever established that this account is safe to "
            f"trade, so the shared safety state is {safety.state}. 'Nobody has "
            "checked' is not 'checked and fine'. Run: "
            f"{RECONCILE_COMMAND.format(database=database)}",
        )

    if not safety.safe_to_trade:
        anchor_text = (
            ""
            if safety.client_order_id is None
            else f" The unresolved client_order_id is {safety.client_order_id}."
        )
        return safety, CheckResult(
            "account.safety",
            SmokeVerdict.FAIL,
            f"The shared account safety state is {safety.state}, set by "
            f"{safety.source}: {safety.reason}{anchor_text} No order from either "
            "asset class may be submitted while this stands, and it is cleared "
            "only by a full-universe reconciliation that resolves it - never by "
            f"waiting and never by retrying. Run: "
            f"{RECONCILE_COMMAND.format(database=database)}",
        )

    updated = "never" if safety.updated_at is None else safety.updated_at.isoformat()
    return safety, CheckResult(
        "account.safety",
        SmokeVerdict.PASS,
        f"The shared account safety state is {safety.state}, safe_to_trade=True "
        f"(set by {safety.source}, updated {updated}). Both books are open.",
    )


def _runtime_check(runtime: Sequence[RuntimeHealth]) -> CheckResult:
    """Report checkpoint freshness. Never blocking, and never a start or stop.

    A stale or absent checkpoint before a *manual* smoke is normal - the
    operator may well have stopped the runtime on purpose, which is the
    recommended way to run one. So this reports and does not gate.
    """
    described = ", ".join(
        f"{item.symbol}={item.freshness.value}"
        + ("" if item.age_seconds is None else f" ({_describe_age_seconds(item.age_seconds)})")
        for item in runtime
    )
    return CheckResult(
        "runtime.checkpoints",
        SmokeVerdict.PASS,
        f"{described or 'no symbols'}. Reported, not gated: stopping the runtime before "
        "a manual smoke is the recommended way to run one, and a stopped runtime shows "
        "as STALE. Nothing here starts or stops a runtime.",
    )


def _dashboard_check(dashboard: DashboardHealth) -> CheckResult:
    """The dashboard is a view of state. It never blocks a broker decision.

    The single exception is a dashboard that answers *and* exposes a
    credential-shaped field, which is a real finding and is failed on.
    """
    if dashboard.credential_fields:
        return CheckResult("dashboard", SmokeVerdict.FAIL, dashboard.detail)
    return CheckResult("dashboard", SmokeVerdict.PASS, dashboard.detail)


def _entry_plan(
    client: BrokerReadClient | None, symbol: str | None, database: Path
) -> tuple[Decimal | None, str | None, str | None]:
    """The smallest order the broker would accept for the smoke symbol, and how to check it.

    A floor, not a recommendation. Sizing the actual BUY is the operator's
    decision at the time, against the account, the risk state and the session -
    which is why what comes back is a `--dry-run` command that evaluates all of
    that and submits nothing, rather than a ready-to-send order.
    """
    if client is None or not symbol:
        return None, None, None
    ticker = normalize_smoke_symbol(symbol)
    asset = broker.read_asset_spec(client, ticker)
    price = broker.read_reference_price(ticker)
    return cleanup.plan_minimum_entry(
        symbol=ticker, asset=asset, quoted_price=price, database=database
    )


def _describe_age(delta: timedelta) -> str:
    return _describe_age_seconds(delta.total_seconds())


def _describe_age_seconds(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


__all__ = [
    "DEFAULT_RECONCILIATION_MAX_AGE",
    "RECONCILE_COMMAND",
    "PreflightReport",
    "run_preflight",
]
