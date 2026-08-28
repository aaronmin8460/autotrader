"""The harness command line. Four read-only commands, and no way to place an order.

Deliberately a **separate** application from `autotrader`. The main CLI owns
`paper-submit` and `crypto-run`, the two commands that can reach a broker's
order endpoint; this one owns nothing of the kind and imports nothing that
does. Keeping them apart is what makes the guarantee checkable at a glance:
`autotrader-smoke --help` lists every action this program can take, and none of
them is an order.

There is no `--execute`, no `--yes`, no `--auto-cleanup`, and no environment
variable that turns any command here into a submission. Those options do not
exist to be refused - they are absent, and a test asserts their absence against
the parsed source.

Exit codes mirror the main CLI's, so a script cannot misread one for another:

    0  the check passed - ready, complete, or the order was found
    1  a controlled negative - BLOCKED, INCOMPLETE, or no such order
    2  something is UNRESOLVED: an order may exist at the broker
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Annotated

import typer

from autotrader.execution.models import format_quantity
from autotrader.smoke import audit as audit_module
from autotrader.smoke import baseline as baseline_module
from autotrader.smoke import broker, cleanup, health, inspector
from autotrader.smoke import preflight as preflight_module
from autotrader.smoke.gitinfo import git_state
from autotrader.smoke.models import (
    USER_MUST_EXECUTE_BANNER,
    BrokerReadClient,
    BrokerUnreadableError,
    CleanupVerdict,
    SmokeError,
    SmokeVerdict,
)
from autotrader.smoke.readonly import (
    load_universe_file,
    normalize_smoke_symbol,
    open_readonly,
    resolve_universe,
    schema_version,
    universe_source,
)
from autotrader.state.sqlite import DEFAULT_DATABASE_PATH

#: A negative but fully understood answer: BLOCKED, INCOMPLETE, or not found.
BLOCKED_EXIT_CODE = 1

#: Something could not be determined and an order may exist at the broker. Its
#: own code, exactly as `paper-submit` and `reconcile` use 2, so no script can
#: read "I do not know" as "no".
UNRESOLVED_EXIT_CODE = 2

_LABEL_WIDTH = 22

#: Read once at import so the `preflight` command's default does not have to
#: name the `health` module, which its own parameter list would shadow.
_DEFAULT_STALE_AFTER_SECONDS = health.DEFAULT_STALE_AFTER.total_seconds()

app = typer.Typer(
    name="autotrader-smoke",
    add_completion=False,
    no_args_is_help=True,
)


@app.callback()
def cli() -> None:
    """Read-only operational harness for the Combined Paper Smoke.

    Preflight, order inspection, cleanup planning, and a final audit. Every
    command reads: the broker is only ever queried with GETs, the operational
    database is opened `mode=ro` with `query_only` set, and no command here can
    submit, cancel, replace, or close anything.

    `cleanup-plan` prints a command for you to run. It does not run it, and
    nothing in this program can: no module here imports `subprocess` except the
    one that shells out to `git`, and that one takes no command line.
    """


# --------------------------------------------------------------------------
# Shared plumbing
# --------------------------------------------------------------------------


def _open_broker() -> tuple[BrokerReadClient | None, str | None]:
    """The paper client, or None and the reason why.

    Returned rather than raised so a preflight can still report the database,
    the git commit, and the reconciliation state when credentials are missing.
    Seeing the whole picture with one line marked FAIL is more useful than a
    traceback about the first thing that went wrong.
    """
    try:
        return broker.open_paper_client(), None
    except BrokerUnreadableError as error:
        return None, str(error)


def _resolve_universe(
    universe: str | None, universe_file: Path | None
) -> tuple[tuple[str, ...], str]:
    """The tracked symbols and a note about where they came from."""
    if universe_file is not None:
        return load_universe_file(universe_file), f"{universe_file}"
    explicit = (
        tuple(part.strip() for part in universe.split(",") if part.strip()) if universe else None
    )
    return resolve_universe(explicit), universe_source(explicit)


def _field(label: str, value: object) -> str:
    return f"  {label + ':':<{_LABEL_WIDTH}} {value}"


def _heading(title: str) -> None:
    typer.echo("")
    typer.secho(title, bold=True)


def _echo_checks(checks: tuple, /) -> None:
    """One line per gate, failures coloured, evidence indented underneath."""
    for check in checks:
        colour = typer.colors.GREEN if check.verdict is SmokeVerdict.PASS else typer.colors.RED
        typer.secho(f"  [{check.verdict.value}] {check.name}", fg=colour)
        typer.echo(f"        {check.detail}")


def _echo_verdict(text: str, *, good: bool) -> None:
    typer.echo("")
    typer.secho(text, fg=typer.colors.GREEN if good else typer.colors.RED, bold=True)


def _fail(message: str, code: int = BLOCKED_EXIT_CODE) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code=code)


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------


@app.command()
def preflight(
    database: Annotated[
        Path, typer.Option("--db", help="Local operational-state database. Opened read-only.")
    ] = DEFAULT_DATABASE_PATH,
    symbol: Annotated[
        str | None,
        typer.Option("--symbol", help="The symbol the smoke will trade, if known."),
    ] = None,
    universe: Annotated[
        str | None,
        typer.Option("--universe", help="Comma-separated tracked symbols to inspect."),
    ] = None,
    universe_file: Annotated[
        Path | None,
        typer.Option("--universe-file", help="JSON file holding the tracked universe."),
    ] = None,
    repo: Annotated[
        Path, typer.Option("--repo", help="Repository whose git state to report.")
    ] = Path("."),
    dashboard_url: Annotated[
        str | None,
        typer.Option("--dashboard-url", help="Optional dashboard health endpoint to GET."),
    ] = None,
    write_baseline: Annotated[
        bool,
        typer.Option("--write-baseline", help="Write a local baseline snapshot JSON file."),
    ] = False,
    baseline_path: Annotated[
        Path,
        typer.Option("--baseline-path", help="Where to write the baseline snapshot."),
    ] = baseline_module.DEFAULT_BASELINE_PATH,
    allow_dirty: Annotated[
        bool,
        typer.Option("--allow-dirty", help="Do not block on an uncommitted working tree."),
    ] = False,
    stale_after: Annotated[
        float,
        typer.Option("--stale-after", help="Seconds after which a checkpoint is STALE."),
    ] = _DEFAULT_STALE_AFTER_SECONDS,
) -> None:
    """Report whether a Combined Paper Smoke may begin. Reads only.

    Checks git, credentials, the paper environment, the account, tracked
    positions, open and UNKNOWN order intents, the database's schema and
    journal mode, the latest persisted reconciliation run, runtime checkpoint
    freshness, and - optionally - a dashboard endpoint.

    **It never reconciles.** A reconciliation pass may rewrite local state from
    broker truth, and an inspection must not do that behind you. When the
    recorded pass is missing, stale, or not green, this prints the command for
    you to run.

    `--write-baseline` records the "before" numbers so `final-audit` can compare
    exposure automatically. The snapshot holds no credentials: it is built from
    a named allowlist and then scanned, and the write is refused rather than
    redacted if anything credential-shaped is found.

    Exits 0 for READY_FOR_PAPER_SMOKE and 1 for BLOCKED.
    """
    client, broker_error = _open_broker()
    symbols, origin = _resolve_universe(universe, universe_file)

    try:
        with open_readonly(database) as connection:
            report = preflight_module.run_preflight(
                client=client,
                connection=connection,
                database_path=database,
                git=git_state(repo),
                universe=symbols,
                universe_origin=origin,
                smoke_symbol=symbol,
                allow_dirty=allow_dirty,
                dashboard_url=dashboard_url,
                stale_after=timedelta(seconds=stale_after),
                broker_error=broker_error,
            )
            schema = schema_version(connection)
    except SmokeError as error:
        raise _fail(str(error)) from None

    _echo_preflight(report)

    if write_baseline:
        try:
            written = baseline_module.write_baseline(
                report.to_baseline(database_path=str(database), schema=schema),
                baseline_path,
            )
        except SmokeError as error:
            raise _fail(str(error)) from None
        _heading("BASELINE SNAPSHOT")
        typer.echo(_field("Written to", written))
        typer.echo(
            "  Local operational scratch. It holds no credentials and must not be committed."
        )

    _echo_verdict(report.verdict_text(), good=report.ready)
    if not report.ready:
        raise typer.Exit(code=BLOCKED_EXIT_CODE)


def _echo_preflight(report: preflight_module.PreflightReport) -> None:
    _heading("SMOKE PREFLIGHT")
    typer.echo(_field("Universe", ", ".join(report.universe)))
    typer.echo(_field("Universe source", report.universe_origin))
    typer.echo(
        _field(
            "Paper submit gate",
            "OPEN" if report.paper_gate_open else "CLOSED (needed for the BUY you run)",
        )
    )
    # The shared halt is a gate, not a detail, so it is named in the header as
    # well as in the checks below. One account, both books, one answer.
    safety = report.account_safety
    typer.echo(
        _field(
            "Account safety",
            "UNREADABLE"
            if safety is None
            else f"{safety.state} (safe_to_trade={safety.safe_to_trade})",
        )
    )

    _heading("CHECKS")
    _echo_checks(report.gate.checks)

    if report.entry_minimum is not None or report.entry_note:
        _heading(f"SMALLEST VALID ORDER — {report.smoke_symbol}")
        if report.entry_minimum is not None:
            typer.echo(_field("Broker minimum", format_quantity(report.entry_minimum)))
        if report.entry_note:
            typer.echo(f"  {report.entry_note}")
        if report.entry_dry_run_command:
            typer.echo("")
            typer.echo("  Size the BUY yourself against the account, the risk state and")
            typer.echo("  the session. Start with the dry run, which cannot submit:")
            typer.echo("")
            typer.secho(f"    {report.entry_dry_run_command}", fg=typer.colors.CYAN)


# --------------------------------------------------------------------------
# inspect-order
# --------------------------------------------------------------------------


@app.command(name="inspect-order")
def inspect_order(
    client_order_id: Annotated[
        str | None,
        typer.Option("--client-order-id", help="The idempotency key this system minted."),
    ] = None,
    broker_order_id: Annotated[
        str | None,
        typer.Option("--broker-order-id", help="The order id the broker assigned."),
    ] = None,
    database: Annotated[
        Path, typer.Option("--db", help="Local operational-state database. Opened read-only.")
    ] = DEFAULT_DATABASE_PATH,
) -> None:
    """Ask the broker what became of exactly one order. Reads only.

    Supply exactly one identifier. Reports the broker's own status, the ordered
    and filled quantities separately, any open remainder, and the current
    broker position in the symbol - which is the authoritative number, and is
    not the same as the filled quantity once a crypto fee has come out of the
    base asset.

    A lookup that cannot be answered reports ORDER_TRUTH_UNRESOLVED and DO NOT
    RETRY ORIGINAL ORDER, and exits 2. That is not a failure to find the order;
    it means the order's status is unknown, and re-sending it is the one action
    that would turn an unknown into a duplicate.

    Exits 0 when the order was found, 1 when the broker says it does not exist,
    and 2 when the truth could not be established.
    """
    client, broker_error = _open_broker()
    if client is None:
        raise _fail(broker_error or "The paper trading client could not be constructed.")

    try:
        with open_readonly(database) as connection:
            result = inspector.inspect_order(
                client,
                client_order_id=client_order_id,
                broker_order_id=broker_order_id,
                connection=connection,
            )
    except SmokeError as error:
        raise _fail(str(error)) from None

    _heading("ORDER INSPECTION")
    typer.echo(_field("Identifier", result.identifier))
    if result.local_intent_status:
        typer.echo(_field("Local intent status", result.local_intent_status))
    if result.local_snapshot_status:
        typer.echo(_field("Local snapshot", result.local_snapshot_status))

    report = result.report
    if report is not None:
        typer.echo(_field("Broker order id", report.broker_order_id))
        typer.echo(_field("Client order id", report.client_order_id))
        typer.echo(_field("Symbol", report.symbol))
        typer.echo(_field("Side", report.side))
        typer.echo(_field("Broker status", report.status))
        typer.echo(_field("Ordered qty", format_quantity(report.quantity)))
        typer.echo(_field("Filled qty", format_quantity(report.filled_quantity)))
        typer.echo(_field("Filled avg price", report.filled_average_price))
        typer.echo(_field("Open remainder", format_quantity(report.open_remainder)))
        typer.echo(_field("Submitted at", report.submitted_at))
        typer.echo(_field("Filled at", report.filled_at))
        typer.echo(_field("Broker updated at", report.broker_updated_at))
        position = report.broker_position
        typer.echo(
            _field(
                "Broker position",
                "unreadable"
                if position is None
                else f"{format_quantity(position.quantity)} {report.symbol} (AUTHORITATIVE)",
            )
        )
        typer.echo("")
        typer.echo("  Accepted is not filled, and filled is not a position.")
        note = inspector.fill_versus_position_note(report)
        if note:
            typer.secho(f"  {note}", fg=typer.colors.YELLOW)

    if result.position_detail:
        typer.secho(f"  {result.position_detail}", fg=typer.colors.YELLOW)

    typer.echo("")
    typer.echo(f"  {result.detail}")

    for banner in result.banners():
        typer.echo("")
        typer.secho(banner, fg=typer.colors.RED, bold=True)

    _echo_verdict(result.verdict_text, good=result.outcome is broker.LookupOutcome.FOUND)
    if result.unresolved:
        raise typer.Exit(code=UNRESOLVED_EXIT_CODE)
    if result.outcome is broker.LookupOutcome.NOT_FOUND:
        raise typer.Exit(code=BLOCKED_EXIT_CODE)


# --------------------------------------------------------------------------
# cleanup-plan
# --------------------------------------------------------------------------


@app.command(name="cleanup-plan")
def cleanup_plan(
    symbol: Annotated[str, typer.Option("--symbol", help="The symbol the smoke traded.")],
    database: Annotated[
        Path, typer.Option("--db", help="Local operational-state database. Opened read-only.")
    ] = DEFAULT_DATABASE_PATH,
) -> None:
    """Plan the cleanup SELL from the broker's position. Prints it; never runs it.

    The quantity is sized from what the broker says the account holds, rounded
    **down** to the broker's own trade increment - never from the quantity the
    BUY requested and never from the quantity it filled. Those differ: on
    Alpaca crypto the taker fee comes out of the base asset, so a fill of
    0.00016705 BTC settles as a position of 0.000166632 BTC, and a cleanup
    sized from the fill would try to sell more than the account holds.

    The command it prints is text. This program cannot execute it: no module
    here imports anything that starts a process, apart from the one that runs
    `git`, and that one accepts no command line. Running it is your decision,
    once.

    Exits 0 when a plan exists or no cleanup is needed, and 1 when the position
    cannot be closed by any order the broker would accept.
    """
    ticker = normalize_smoke_symbol(symbol)
    client, broker_error = _open_broker()
    if client is None:
        raise _fail(broker_error or "The paper trading client could not be constructed.")

    try:
        positions = broker.read_positions(client)
    except BrokerUnreadableError as error:
        raise _fail(str(error)) from None

    position = broker.position_for(positions, ticker)
    plan = cleanup.plan_cleanup(
        position=position,
        asset=broker.read_asset_spec(client, ticker),
        quoted_price=broker.read_reference_price(ticker),
        database=database,
    )

    _heading(f"CLEANUP PLAN — {plan.symbol}")
    held = format_quantity(plan.position_quantity)
    typer.echo(_field("Broker position", f"{held} (AUTHORITATIVE)"))
    typer.echo(_field("Reference price", plan.reference_price))
    typer.echo(_field("Estimated value", plan.estimated_value))
    typer.echo(_field("Min order size", plan.min_order_size))
    typer.echo(_field("Trade increment", plan.min_trade_increment))
    typer.echo(_field("Min notional qty", plan.minimum_notional_quantity))
    typer.echo(_field("Planned SELL qty", format_quantity(plan.plan_quantity)))
    typer.echo(_field("Residual after", format_quantity(plan.residual_quantity)))
    typer.echo(_field("Full cleanup", "yes" if plan.full_cleanup_possible else "no"))
    typer.echo("")
    typer.echo(f"  {plan.reason}")

    if plan.verdict is CleanupVerdict.REQUIRED and plan.command:
        _heading("CANDIDATE COMMAND")
        typer.secho(f"  {USER_MUST_EXECUTE_BANNER}", fg=typer.colors.YELLOW, bold=True)
        typer.echo("")
        typer.secho(f"    {plan.command}", fg=typer.colors.CYAN)
        typer.echo("")
        typer.echo("  This harness printed that line. It did not run it and cannot.")
        typer.echo("  Two gates still stand in front of it, and both are yours to open:")
        typer.echo("    1. AUTOTRADER_PAPER_TRADING_ENABLED=true in the environment")
        typer.echo("    2. --confirm-paper PAPER on the command line")
        typer.echo("")
        typer.echo("  Check it first with the same command plus --dry-run, which")
        typer.echo("  evaluates everything and submits nothing.")

    _echo_verdict(plan.verdict.value, good=plan.verdict is not CleanupVerdict.NOT_POSSIBLE)
    if plan.verdict is CleanupVerdict.NOT_POSSIBLE:
        raise typer.Exit(code=BLOCKED_EXIT_CODE)


# --------------------------------------------------------------------------
# final-audit
# --------------------------------------------------------------------------


@app.command(name="final-audit")
def final_audit(
    database: Annotated[
        Path, typer.Option("--db", help="Local operational-state database. Opened read-only.")
    ] = DEFAULT_DATABASE_PATH,
    symbol: Annotated[
        str | None, typer.Option("--symbol", help="The symbol the smoke traded.")
    ] = None,
    baseline_path: Annotated[
        Path,
        typer.Option("--baseline", help="Baseline snapshot written by the preflight."),
    ] = baseline_module.DEFAULT_BASELINE_PATH,
    no_baseline: Annotated[
        bool,
        typer.Option("--no-baseline", help="Audit without comparing against a snapshot."),
    ] = False,
    buy_client_order_id: Annotated[
        str | None,
        typer.Option("--buy-client-order-id", help="The smoke BUY's client order id."),
    ] = None,
    sell_client_order_id: Annotated[
        str | None,
        typer.Option("--sell-client-order-id", help="The cleanup SELL's client order id."),
    ] = None,
    universe: Annotated[
        str | None,
        typer.Option("--universe", help="Comma-separated tracked symbols to inspect."),
    ] = None,
    universe_file: Annotated[
        Path | None,
        typer.Option("--universe-file", help="JSON file holding the tracked universe."),
    ] = None,
    repo: Annotated[
        Path, typer.Option("--repo", help="Repository whose git state to report.")
    ] = Path("."),
    dashboard_url: Annotated[
        str | None,
        typer.Option("--dashboard-url", help="Optional dashboard health endpoint to GET."),
    ] = None,
) -> None:
    """Confirm the smoke finished and exposure is back where it started. Reads only.

    Requires: every tracked position equal to its baseline, no open tracked
    order, no UNKNOWN intent, the latest reconciliation run CLEAN and safe,
    readable runtime checkpoints, and a clean working tree. Supplying the BUY
    and SELL client order ids adds the stronger correlation check - each exists
    exactly once, neither has an open remainder, and no third order appeared
    for the symbol.

    Exposure comparison is exact. A dust remainder is residual exposure, not
    noise, and a tolerance here would hide the fee-adjustment case this harness
    exists to catch.

    Exits 0 for SMOKE_COMPLETE, 1 for SMOKE_INCOMPLETE, and 2 when an order's
    truth could not be established.
    """
    client, broker_error = _open_broker()
    symbols, origin = _resolve_universe(universe, universe_file)

    loaded = None
    if not no_baseline:
        try:
            loaded = baseline_module.read_baseline(baseline_module.require_existing(baseline_path))
        except SmokeError as error:
            raise _fail(str(error)) from None

    try:
        with open_readonly(database) as connection:
            result = audit_module.run_audit(
                client=client,
                connection=connection,
                database_path=database,
                git=git_state(repo),
                universe=symbols,
                universe_origin=origin,
                baseline=loaded,
                baseline_path=None if loaded is None else str(baseline_path),
                smoke_symbol=symbol,
                buy_client_order_id=buy_client_order_id,
                sell_client_order_id=sell_client_order_id,
                dashboard_url=dashboard_url,
                broker_error=broker_error,
            )
    except SmokeError as error:
        raise _fail(str(error)) from None

    _heading("SMOKE FINAL AUDIT")
    typer.echo(_field("Universe", ", ".join(result.universe)))
    typer.echo(_field("Universe source", result.universe_origin))
    typer.echo(_field("Baseline", result.baseline_path or "not compared"))

    _heading("CHECKS")
    _echo_checks(result.report.gate.checks)

    if result.report.comparisons:
        _heading("EXPOSURE — BEFORE vs AFTER")
        for comparison in result.report.comparisons:
            marker = "ok " if comparison.restored else "DIFF"
            colour = typer.colors.GREEN if comparison.restored else typer.colors.RED
            typer.secho(
                f"  [{marker}] {comparison.symbol:<10} "
                f"{format_quantity(comparison.before)} -> {format_quantity(comparison.after)}",
                fg=colour,
            )

    for note in result.report.notes:
        typer.echo("")
        typer.secho(f"  {note}", fg=typer.colors.YELLOW)

    for banner in result.banners:
        typer.echo("")
        typer.secho(banner, fg=typer.colors.RED, bold=True)

    exposure = result.exposure_text()
    if exposure is not None:
        _echo_verdict(exposure, good=bool(result.report.exposure_restored))
    _echo_verdict(result.verdict_text(), good=result.complete)

    if any(
        check.blocking and "ORDER_TRUTH_UNRESOLVED" in check.detail
        for check in result.report.gate.checks
    ):
        raise typer.Exit(code=UNRESOLVED_EXIT_CODE)
    if not result.complete:
        raise typer.Exit(code=BLOCKED_EXIT_CODE)


# --------------------------------------------------------------------------
# sequence
# --------------------------------------------------------------------------


@app.command()
def sequence(
    database: Annotated[
        Path, typer.Option("--db", help="Database path to embed in the printed commands.")
    ] = DEFAULT_DATABASE_PATH,
    symbol: Annotated[
        str, typer.Option("--symbol", help="The symbol the smoke will trade.")
    ] = "BTC/USD",
) -> None:
    """Print the operator's command sequence for a Combined Paper Smoke.

    A checklist, printed as text. The two steps that place orders are marked as
    yours and are not generated ready-to-run here - `cleanup-plan` produces the
    SELL from the broker's real position at the time, and the BUY is sized by
    you from the preflight's dry run.
    """
    ticker = normalize_smoke_symbol(symbol)
    steps = (
        (
            "1",
            f"autotrader-smoke preflight --db {database} --symbol {ticker} --write-baseline",
            False,
        ),
        ("2", "YOU run ONE paper BUY, sized from the preflight's --dry-run output", True),
        ("3", f"autotrader-smoke inspect-order --client-order-id <BUY id> --db {database}", False),
        ("4", f"autotrader reconcile --db {database}   (only if step 3 says to)", True),
        ("5", f"autotrader-smoke cleanup-plan --symbol {ticker} --db {database}", False),
        ("6", "YOU run ONE cleanup SELL, exactly the command step 5 printed", True),
        ("7", f"autotrader-smoke inspect-order --client-order-id <SELL id> --db {database}", False),
        ("8", f"autotrader reconcile --db {database}   (pass 1)", True),
        ("9", f"autotrader reconcile --db {database}   (pass 2 - expect CLEAN)", True),
        ("10", f"autotrader-smoke final-audit --db {database} --symbol {ticker} \\", False),
        ("", "    --buy-client-order-id <BUY id> --sell-client-order-id <SELL id>", False),
        ("11", f"autotrader crypto-run --once --observe-only --db {database}", True),
        (
            "12",
            f"autotrader equity-run --once --observe-only --db {database}   (when it exists)",
            True,
        ),
        ("13", "autotrader-smoke preflight --dashboard-url <url>   (dashboard health)", False),
    )
    _heading("COMBINED PAPER SMOKE — OPERATOR SEQUENCE")
    typer.echo("")
    for number, text, operator in steps:
        prefix = f"  {number:>2}. " if number else "      "
        if operator:
            typer.secho(f"{prefix}{text}", fg=typer.colors.YELLOW)
        else:
            typer.echo(f"{prefix}{text}")
    typer.echo("")
    typer.secho("  Yellow steps are yours to run. This harness runs none of them.", bold=True)
    typer.echo("  Steps 2 and 6 are the only ones that place an order, and both need")
    typer.echo("  AUTOTRADER_PAPER_TRADING_ENABLED=true plus --confirm-paper PAPER.")


def main() -> None:
    """Console-script entry point."""
    app()


__all__ = [
    "BLOCKED_EXIT_CODE",
    "UNRESOLVED_EXIT_CODE",
    "app",
    "main",
]
