"""The end-state check: did the smoke finish, and is exposure back where it started?

Run after the cleanup SELL and after reconciliation. It answers two separate
questions and keeps them separate, because they can disagree:

`SMOKE_COMPLETE` / `SMOKE_INCOMPLETE` - did every end-state condition hold?
`EXPOSURE_RESTORED` / `EXPOSURE_NOT_RESTORED` - does tracked exposure match the
baseline snapshot exactly?

**Exact equality, on purpose.** A crypto position left at `0.000000418` after a
cleanup is not noise to be tolerated - it is residual exposure, it is the exact
shape of the fee-adjustment bug this harness exists to catch, and a tolerance
would hide it. If a remainder genuinely cannot be closed (below the broker's
minimum order value), the cleanup planner says so and the operator records it;
the audit still reports the exposure as not restored, because it is not.

**Nothing here writes, and nothing here reconciles.** Reconciliation is run by
the operator, twice, before this command. This one reads what those passes
concluded.
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
from autotrader.smoke import broker, health, tracking
from autotrader.smoke.baseline import Baseline
from autotrader.smoke.broker import LookupOutcome
from autotrader.smoke.gitinfo import GitState
from autotrader.smoke.models import (
    DO_NOT_RETRY_BANNER,
    ORDER_TRUTH_UNRESOLVED,
    AuditReport,
    BaselineComparison,
    BrokerReadClient,
    BrokerUnreadableError,
    CheckResult,
    DashboardHealth,
    GateReport,
    PositionSnapshot,
    RuntimeHealth,
    SmokeVerdict,
)
from autotrader.smoke.readonly import normalize_smoke_symbol
from autotrader.state import sqlite as state

#: The only reconciliation conclusion this audit accepts as a finished smoke.
#: `REPAIRED` is safe to *trade* on, but it means the pass still had something
#: to fix - so the documented sequence runs a second pass, and that one should
#: come back `CLEAN`. A smoke that ends on `REPAIRED` has not settled yet.
REQUIRED_RECONCILIATION_STATUS = state.RECONCILIATION_STATUS_CLEAN


@dataclass(frozen=True)
class AuditRunReport:
    """The audit's verdicts, plus everything it looked at to reach them."""

    report: AuditReport
    git: GitState
    universe: tuple[str, ...]
    universe_origin: str
    positions: dict[str, PositionSnapshot]
    runtime: tuple[RuntimeHealth, ...]
    dashboard: DashboardHealth
    latest_reconciliation: state.ReconciliationRun | None
    baseline_path: str | None = None
    banners: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.report.complete

    def verdict_text(self) -> str:
        return self.report.verdict_text()

    def exposure_text(self) -> str | None:
        return self.report.exposure_text()


def run_audit(
    *,
    client: BrokerReadClient | None,
    connection: sqlite3.Connection,
    database_path: Path | str,
    git: GitState,
    universe: Sequence[str],
    universe_origin: str,
    baseline: Baseline | None = None,
    baseline_path: str | None = None,
    smoke_symbol: str | None = None,
    buy_client_order_id: str | None = None,
    sell_client_order_id: str | None = None,
    dashboard_url: str | None = None,
    now: datetime | None = None,
    stale_after: timedelta = health.DEFAULT_STALE_AFTER,
    broker_error: str | None = None,
) -> AuditRunReport:
    """Check every end-state condition and report. Reads only.

    `buy_client_order_id` and `sell_client_order_id` correlate the smoke's own
    orders. Supplying them turns "no orders are open" into the much stronger
    "the BUY exists exactly once, the SELL exists exactly once, neither has an
    open remainder, and no third order appeared for this symbol".
    """
    moment = now or datetime.now(UTC)
    checks: list[CheckResult] = []
    notes: list[str] = []
    banners: list[str] = []

    checks.append(_git_check(git))

    positions, position_checks, broker_ok = _position_checks(
        client, broker_error, universe, baseline, smoke_symbol
    )
    checks.extend(position_checks)

    comparisons = _comparisons(positions, baseline, universe) if baseline else ()
    if baseline is None:
        notes.append(
            "No baseline snapshot was supplied, so exposure could not be compared "
            "against a recorded 'before'. Any non-zero tracked position is treated as "
            "residual. Run the preflight with --write-baseline next time."
        )

    open_rows = tracking.open_intents(connection)
    unknown_rows = tracking.unknown_intents(connection)
    checks.append(_open_orders_check(open_rows, smoke_symbol))
    checks.append(_unknown_orders_check(unknown_rows))
    if unknown_rows:
        banners.append(DO_NOT_RETRY_BANNER)

    if smoke_symbol:
        correlation, correlation_banners = _correlation_checks(
            client if broker_ok else None,
            connection,
            smoke_symbol,
            buy_client_order_id,
            sell_client_order_id,
        )
        checks.extend(correlation)
        banners.extend(correlation_banners)

    latest = tracking.latest_reconciliation(connection)
    checks.append(_reconciliation_check(latest, database_path))
    checks.append(_account_safety_check(connection, database_path))

    runtime = health.runtime_health(connection, universe, now=moment, stale_after=stale_after)
    checks.append(_runtime_readable_check(runtime))

    dashboard = health.dashboard_health(dashboard_url)
    checks.append(_dashboard_check(dashboard))

    return AuditRunReport(
        report=AuditReport(
            gate=GateReport(checks=tuple(checks)),
            comparisons=comparisons,
            notes=tuple(notes),
        ),
        git=git,
        universe=tuple(normalize_smoke_symbol(symbol) for symbol in universe),
        universe_origin=universe_origin,
        positions=positions,
        runtime=runtime,
        dashboard=dashboard,
        latest_reconciliation=latest,
        baseline_path=baseline_path,
        banners=tuple(dict.fromkeys(banners)),
    )


def _git_check(git: GitState) -> CheckResult:
    """A finished smoke should leave the tree it ran from unchanged."""
    if not git.known:
        return CheckResult("git", SmokeVerdict.FAIL, git.detail)
    location = f"{git.branch or 'DETACHED'} @ {git.short_sha}"
    if git.dirty is None:
        return CheckResult("git", SmokeVerdict.FAIL, f"{location}: working tree state unknown.")
    if git.dirty:
        return CheckResult(
            "git",
            SmokeVerdict.FAIL,
            f"{location}: the working tree has uncommitted changes. A smoke should not "
            "have modified the repository it was run from.",
        )
    return CheckResult("git", SmokeVerdict.PASS, f"{location}: clean.")


def _position_checks(
    client: BrokerReadClient | None,
    broker_error: str | None,
    universe: Sequence[str],
    baseline: Baseline | None,
    smoke_symbol: str | None,
) -> tuple[dict[str, PositionSnapshot], list[CheckResult], bool]:
    """Compare every tracked position against the baseline, or against zero.

    Without a baseline the only defensible reference is flat: the audit cannot
    prove that an existing position predates the smoke, so it reports it as
    residual rather than assuming it was always there.
    """
    if client is None:
        detail = broker_error or "The paper trading client could not be constructed."
        return {}, [CheckResult("positions", SmokeVerdict.FAIL, detail)], False
    try:
        live = broker.read_positions(client)
    except BrokerUnreadableError as error:
        return {}, [CheckResult("positions", SmokeVerdict.FAIL, str(error))], False

    tracked = {
        symbol: broker.position_for(live, symbol)
        for symbol in (normalize_smoke_symbol(item) for item in universe)
    }
    if smoke_symbol:
        ticker = normalize_smoke_symbol(smoke_symbol)
        tracked.setdefault(ticker, broker.position_for(live, ticker))

    residual: list[str] = []
    for symbol, position in sorted(tracked.items()):
        expected = baseline.quantity_for(symbol) if baseline else position.quantity * 0
        if position.quantity != expected:
            residual.append(
                f"{symbol}: now {format_quantity(position.quantity)}, expected "
                f"{format_quantity(expected)}"
                + ("" if baseline else " (no baseline; flat is the only safe reference)")
            )

    if residual:
        return (
            tracked,
            [
                CheckResult(
                    "positions",
                    SmokeVerdict.FAIL,
                    "Residual exposure: "
                    + "; ".join(residual)
                    + ". Exact equality is required - a dust remainder is residual "
                    "exposure, not noise.",
                )
            ],
            True,
        )
    summary = ", ".join(
        f"{symbol}={format_quantity(position.quantity)}"
        for symbol, position in sorted(tracked.items())
    )
    return (
        tracked,
        [
            CheckResult(
                "positions",
                SmokeVerdict.PASS,
                f"Every tracked position matches its expected value: {summary or 'none'}.",
            )
        ],
        True,
    )


def _comparisons(
    positions: dict[str, PositionSnapshot], baseline: Baseline, universe: Sequence[str]
) -> tuple[BaselineComparison, ...]:
    """Before/after for every symbol in the baseline or the current universe."""
    # One row per market. The three sources spell the same market differently -
    # the universe and the baseline use `BTC/USD`, the broker's position list
    # uses `BTCUSD` - so they are deduped by `broker_symbol_key` and rendered in
    # the canonical spelling where one is known. Unioning the raw strings would
    # emit two rows for one holding and compare each against the wrong half.
    chosen: dict[str, str] = {}
    for symbol in (
        *(normalize_smoke_symbol(item) for item in universe),
        *baseline.positions,
    ):
        chosen.setdefault(broker_symbol_key(symbol), symbol)
    for symbol in positions:
        chosen.setdefault(broker_symbol_key(symbol), symbol)

    # Indexed by market rather than read by key, because callers key this dict
    # in either vocabulary: the tracked map is keyed by the universe's canonical
    # names, a raw broker read by the broker's own spelling.
    by_market = {broker_symbol_key(symbol): position for symbol, position in positions.items()}

    return tuple(
        BaselineComparison(
            symbol=symbol,
            before=baseline.quantity_for(symbol),
            after=(
                by_market[broker_symbol_key(symbol)].quantity
                if broker_symbol_key(symbol) in by_market
                else Decimal(0)
            ),
        )
        for symbol in sorted(chosen.values())
    )


def _open_orders_check(
    open_rows: Sequence[state.StoredOrderIntent], smoke_symbol: str | None
) -> CheckResult:
    """A finished smoke leaves nothing working at the broker."""
    if not open_rows:
        return CheckResult(
            "orders.open", SmokeVerdict.PASS, "No tracked order intent is still open."
        )
    described = ", ".join(
        f"{intent.client_order_id} ({intent.symbol} {intent.side} {intent.status})"
        for intent in open_rows
    )
    scope = f" for {normalize_smoke_symbol(smoke_symbol)}" if smoke_symbol else ""
    return CheckResult(
        "orders.open",
        SmokeVerdict.FAIL,
        f"{len(open_rows)} tracked order intent(s) are still open{scope}: {described}. "
        "The smoke has not finished.",
    )


def _unknown_orders_check(unknown_rows: Sequence[state.StoredOrderIntent]) -> CheckResult:
    if not unknown_rows:
        return CheckResult(
            "orders.unknown", SmokeVerdict.PASS, "No tracked order intent is UNKNOWN."
        )
    described = ", ".join(intent.client_order_id for intent in unknown_rows)
    return CheckResult(
        "orders.unknown",
        SmokeVerdict.FAIL,
        f"{len(unknown_rows)} order intent(s) are UNKNOWN: {described}. An order may "
        f"exist at the broker under each key. {ORDER_TRUTH_UNRESOLVED}. Resolve with "
        "`autotrader reconcile`; do not re-send anything.",
    )


def _correlation_checks(
    client: BrokerReadClient | None,
    connection: sqlite3.Connection,
    smoke_symbol: str,
    buy_client_order_id: str | None,
    sell_client_order_id: str | None,
) -> tuple[list[CheckResult], list[str]]:
    """Prove the smoke placed exactly the orders it was supposed to.

    Local intents are the complete record of what this system attempted - every
    submission path writes one before calling the broker - so counting them
    answers "was there an unexpected replacement order?" without listing the
    broker's whole history. An order placed by hand outside this system leaves
    no row, and would instead surface as a position mismatch; the caller notes
    that limit in its output.
    """
    ticker = normalize_smoke_symbol(smoke_symbol)
    checks: list[CheckResult] = []
    banners: list[str] = []
    intents = tracking.intents_for_symbol(connection, ticker)

    expected = [
        identifier for identifier in (buy_client_order_id, sell_client_order_id) if identifier
    ]
    if not expected:
        checks.append(
            CheckResult(
                "orders.correlation",
                SmokeVerdict.PASS,
                f"No smoke order ids were supplied, so order correlation was not "
                f"checked. {len(intents)} intent(s) exist for {ticker}. Pass "
                "--buy-client-order-id and --sell-client-order-id for the stronger "
                "check.",
            )
        )
        return checks, banners

    for label, identifier in (
        ("BUY", buy_client_order_id),
        ("SELL", sell_client_order_id),
    ):
        if not identifier:
            continue
        checks.append(_one_order_check(client, connection, label, identifier.strip()))
        if checks[-1].blocking and ORDER_TRUTH_UNRESOLVED in checks[-1].detail:
            banners.append(DO_NOT_RETRY_BANNER)

    unexpected = [intent for intent in intents if intent.client_order_id not in set(expected)]
    if unexpected:
        described = ", ".join(
            f"{intent.client_order_id} ({intent.side} {intent.status} at "
            f"{intent.created_at.isoformat()})"
            for intent in unexpected
        )
        checks.append(
            CheckResult(
                "orders.unexpected",
                SmokeVerdict.FAIL,
                f"{len(unexpected)} order intent(s) for {ticker} were not part of this "
                f"smoke: {described}. A smoke places one BUY and at most one cleanup "
                "SELL; anything else is a replacement or a duplicate and must be "
                "explained before the gate passes.",
            )
        )
    else:
        checks.append(
            CheckResult(
                "orders.unexpected",
                SmokeVerdict.PASS,
                f"Exactly {len(expected)} order intent(s) exist for {ticker}, and each "
                "is one of the ids supplied. No replacement or duplicate order was "
                "recorded. (This counts what this system attempted; an order placed by "
                "hand in the broker's own UI leaves no local row and would instead show "
                "as a position mismatch.)",
            )
        )
    return checks, banners


def _one_order_check(
    client: BrokerReadClient | None,
    connection: sqlite3.Connection,
    label: str,
    client_order_id: str,
) -> CheckResult:
    """One smoke order: recorded once locally, terminal at the broker, no remainder."""
    name = f"orders.{label.lower()}"
    intent = tracking.find_intent(connection, client_order_id)
    if intent is None:
        return CheckResult(
            name,
            SmokeVerdict.FAIL,
            f"No local order intent exists for the {label} id {client_order_id}. Either "
            "the id is wrong or the order was never recorded by this system.",
        )
    if client is None:
        return CheckResult(
            name,
            SmokeVerdict.FAIL,
            f"The {label} intent {client_order_id} is recorded locally as "
            f"{intent.status}, but the broker could not be read to confirm it.",
        )

    outcome, snapshot, detail = broker.read_order(client, client_order_id=client_order_id)
    if outcome is LookupOutcome.UNRESOLVED:
        return CheckResult(
            name,
            SmokeVerdict.FAIL,
            f"{ORDER_TRUTH_UNRESOLVED}: the broker could not be asked about the {label} "
            f"order {client_order_id} ({detail}). {DO_NOT_RETRY_BANNER}.",
        )
    if outcome is LookupOutcome.NOT_FOUND:
        return CheckResult(
            name,
            SmokeVerdict.FAIL,
            f"The broker reports no order under the {label} id {client_order_id}, but a "
            f"local intent exists in status {intent.status}. Run `autotrader reconcile` "
            "to settle the difference.",
        )

    assert snapshot is not None  # noqa: S101 - FOUND always carries a snapshot
    remainder = snapshot.quantity - snapshot.filled_quantity
    if remainder > 0 and not tracking.is_terminal_broker_status(snapshot.status):
        return CheckResult(
            name,
            SmokeVerdict.FAIL,
            f"The {label} order {client_order_id} is {snapshot.status} with "
            f"{format_quantity(remainder)} still open of "
            f"{format_quantity(snapshot.quantity)}. The smoke has not finished.",
        )
    return CheckResult(
        name,
        SmokeVerdict.PASS,
        f"Recorded once locally ({intent.status}); the broker reports {snapshot.status}, "
        f"filled {format_quantity(snapshot.filled_quantity)} of "
        f"{format_quantity(snapshot.quantity)}, no open remainder.",
    )


def _reconciliation_check(
    latest: state.ReconciliationRun | None, database: Path | str
) -> CheckResult:
    """The last pass must be CLEAN, not merely safe."""
    command = f"autotrader reconcile --db {database}"
    if latest is None:
        return CheckResult(
            "reconciliation",
            SmokeVerdict.FAIL,
            f"No reconciliation run has completed against this database. Run it twice, "
            f"yourself, before this audit: {command}",
        )
    if latest.status != REQUIRED_RECONCILIATION_STATUS or not latest.safe_to_trade:
        return CheckResult(
            "reconciliation",
            SmokeVerdict.FAIL,
            f"The latest reconciliation run (#{latest.id}) concluded {latest.status} "
            f"with safe_to_trade={latest.safe_to_trade} and {latest.unresolved_count} "
            f"unresolved item(s). A finished smoke ends on "
            f"{REQUIRED_RECONCILIATION_STATUS}: REPAIRED means the pass still had "
            f"something to fix, so run it again and check the second result. {command}",
        )
    return CheckResult(
        "reconciliation",
        SmokeVerdict.PASS,
        f"Run #{latest.id} concluded {REQUIRED_RECONCILIATION_STATUS}, "
        f"safe_to_trade=True, {latest.issues_count} issue(s) recorded.",
    )


def _account_safety_check(connection: sqlite3.Connection, database: Path | str) -> CheckResult:
    """A finished smoke leaves the account open for business, not halted.

    The end-state counterpart of the preflight's gate, and the reason it is
    checked separately from the reconciliation run: a smoke that ended with an
    ambiguous submission can leave a `CLEAN` pass on record from *before* the
    ambiguity while the shared halt raised by it still stands. Reading only the
    pass would call that a restored account. Reading this row cannot.
    """
    command = f"autotrader reconcile --db {database}"
    try:
        safety = tracking.account_safety(connection)
    except sqlite3.Error as error:
        return CheckResult(
            "account.safety",
            SmokeVerdict.FAIL,
            f"The shared account safety state could not be read from {database} "
            f"({error}). An unreadable gate is treated as unsafe.",
        )

    if not safety.established:
        return CheckResult(
            "account.safety",
            SmokeVerdict.FAIL,
            f"The shared account safety state is {safety.state}: no reconciliation "
            f"has ever established that this account is safe to trade. {command}",
        )

    if not safety.safe_to_trade:
        anchor_text = (
            ""
            if safety.client_order_id is None
            else f" The unresolved client_order_id is {safety.client_order_id}."
        )
        return CheckResult(
            "account.safety",
            SmokeVerdict.FAIL,
            f"The shared account safety state is {safety.state}, set by "
            f"{safety.source}: {safety.reason}{anchor_text} The smoke did not leave "
            f"the account open for business. Resolve it with a full-universe "
            f"pass: {command}",
        )

    return CheckResult(
        "account.safety",
        SmokeVerdict.PASS,
        f"The shared account safety state is {safety.state}, safe_to_trade=True "
        f"(set by {safety.source}). Both books are open.",
    )


def _runtime_readable_check(runtime: Sequence[RuntimeHealth]) -> CheckResult:
    """Checkpoints must be *readable*. Freshness is reported, not required."""
    described = ", ".join(f"{item.symbol}={item.freshness.value}" for item in runtime)
    return CheckResult(
        "runtime.checkpoints",
        SmokeVerdict.PASS,
        f"Runtime checkpoints are readable: {described or 'no symbols'}. Freshness is "
        "reported rather than required - the runtime is normally stopped during a "
        "manual smoke.",
    )


def _dashboard_check(dashboard: DashboardHealth) -> CheckResult:
    if dashboard.credential_fields:
        return CheckResult("dashboard", SmokeVerdict.FAIL, dashboard.detail)
    return CheckResult("dashboard", SmokeVerdict.PASS, dashboard.detail)


__all__ = ["REQUIRED_RECONCILIATION_STATUS", "AuditRunReport", "run_audit"]
