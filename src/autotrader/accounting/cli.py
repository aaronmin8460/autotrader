"""Administrative commands for the realized-P&L ledger. Inspection first.

Every command here is a read, an append, or a rebuild from immutable source
events. There is no command that edits a realized total, adjusts a cost basis
to make a discrepancy go away, or deletes an accounting row - because a ledger
with such a command is a ledger whose numbers cannot be trusted afterwards.

`inspect` is the one to reach for when reconciliation reports a mismatch. It
shows both sides and names the candidate executions that would explain the
difference. It changes nothing, and it is deliberately not `repair`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer

from autotrader.accounting import ingest, readmodel, reconcile, service, store

accounting_app = typer.Typer(
    name="equity-accounting",
    add_completion=False,
    no_args_is_help=True,
    help=(
        "Realized P&L accounting for the equity paper book. Accounting only: "
        "nothing here places, cancels or modifies an order, and no trading "
        "decision reads anything these commands write."
    ),
)

_WIDTH = 26


def _line(label: str, value: object) -> None:
    typer.echo(f"{label + ':':<{_WIDTH}}{value}")


def _database(database: str | None) -> Path:
    return Path(database) if database else service.accounting_database_path()


@accounting_app.command()
def bootstrap(
    database: str = typer.Option(None, "--db", help="Accounting database path."),
    paper_database: str = typer.Option(None, "--paper-db", help="Equity runtime store, read-only."),
    source_sha: str = typer.Option(None, "--source-sha", help="Commit that built this ledger."),
    confirm: bool = typer.Option(
        False, "--confirm", help="Required. Writes the ledger's metadata row."
    ),
) -> None:
    """Build the ledger by replaying the account's whole execution record.

    Safe to run against an existing ledger: every execution is keyed by the
    broker's own execution id under a UNIQUE constraint, so a second run
    imports nothing and changes nothing.
    """
    if not confirm:
        typer.secho(
            "Refusing to bootstrap without --confirm. This writes the ledger's "
            "tracking horizon, which everything downstream reports against.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=2)

    path = _database(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    result = service.bootstrap_exact_replay(
        database=path,
        paper_database=Path(paper_database) if paper_database else None,
        source_sha=source_sha,
    )
    _report_pass(path, result)


@accounting_app.command()
def sync(
    database: str = typer.Option(None, "--db", help="Accounting database path."),
    paper_database: str = typer.Option(None, "--paper-db", help="Equity runtime store, read-only."),
    quiet: bool = typer.Option(False, "--quiet", help="Print one line instead of a report."),
) -> None:
    """Ingest confirmed executions since the overlap window, then reconcile."""
    path = _database(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    result = service.synchronize_once(
        database=path,
        paper_database=Path(paper_database) if paper_database else None,
    )
    if quiet:
        typer.echo(
            f"{result.sync.status} imported={result.sync.executions_imported} "
            f"realized={result.sync.realized_events} recon={result.reconciliation.status}"
        )
    else:
        _report_pass(path, result)
    if result.reconciliation.status == store.RECON_MISMATCH:
        raise typer.Exit(code=1)


def _report_pass(path: Path, result: service.PassResult) -> None:
    typer.echo("Accounting synchronization")
    typer.echo("")
    _line("Ledger", path)
    _line("Status", result.sync.status)
    _line("Executions seen", result.sync.executions_seen)
    _line("Imported", result.sync.executions_imported)
    _line("Realized events", result.sync.realized_events)
    _line("Duplicates skipped", result.sync.duplicates_skipped)
    _line("Out of scope skipped", result.sync.out_of_scope_skipped)
    _line("Unresolved orders", result.sync.unresolved_orders)
    _line("Broker requests", result.sync.broker_requests)
    _line("High-water mark", result.sync.high_water_mark or "-")
    if result.sync.message:
        _line("Note", result.sync.message)
    for refusal in result.sync.refusals:
        typer.secho(f"  REFUSED: {refusal}", fg=typer.colors.RED)
    typer.echo("")
    _line("Reconciliation", result.reconciliation.status)
    _line("Symbols checked", result.reconciliation.symbols_checked)
    _line("Quantity mismatches", result.reconciliation.quantity_mismatches)
    _line("Cost deviations", result.reconciliation.cost_deviations)
    if result.reconciliation.message:
        _line("Note", result.reconciliation.message)


@accounting_app.command()
def status(
    database: str = typer.Option(None, "--db", help="Accounting database path."),
) -> None:
    """Show the ledger's horizon, totals and reconciliation verdict. Read-only."""
    path = _database(database)
    now = datetime.now(UTC)
    with store.connect_read_only(path) as connection:
        summary = readmodel.build_summary(connection, now=now)
        symbols = reconcile.latest_symbols(connection)

    panel = summary.status
    typer.echo("Equity realized P&L accounting")
    typer.echo("")
    _line("Ledger", path)
    _line("Accounting status", panel.status)
    _line("Tracking label", panel.tracking_label)
    _line("Tracking started", panel.tracking_started_at or "-")
    _line("Bootstrap", panel.bootstrap_method or "-")
    _line("Completeness", panel.historical_completeness or "-")
    _line("Basis method", panel.basis_method or "-")
    _line("Granularity", panel.execution_granularity)
    _line("Fills imported", summary.fills_imported)
    _line("Realized events", summary.event_count)
    _line("Realized today", f"{summary.realized_today:+.2f} ({summary.utc_day} UTC)")
    _line("Realized since start", f"{summary.realized_since_tracking:+.2f}")
    _line("Winners / losers", f"{summary.winning_events} / {summary.losing_events}")
    _line("Last sync", f"{panel.last_sync_at or '-'} ({panel.last_sync_status or '-'})")
    _line("Last reconciled", panel.last_reconciled_at or "-")
    if panel.message:
        _line("Note", panel.message)

    if symbols:
        typer.echo("")
        typer.echo(
            f"{'SYMBOL':<8}{'LOCAL QTY':>18}{'BROKER QTY':>18}{'LOCAL AVG':>16}"
            f"{'BROKER AVG':>16}  STATUS"
        )
        for row in symbols:
            typer.echo(
                f"{row['symbol']:<8}{row['local_quantity']:>18}{row['broker_quantity']:>18}"
                f"{str(row['local_average_cost'] or '-')[:15]:>16}"
                f"{str(row['broker_average_entry'] or '-')[:15]:>16}  {row['status']}"
            )

    if summary.symbols:
        typer.echo("")
        typer.echo(f"{'SYMBOL':<8}{'REALIZED TODAY':>18}{'REALIZED SINCE':>18}{'EVENTS':>9}")
        for entry in summary.symbols:
            typer.echo(
                f"{entry.symbol:<8}{entry.realized_today:>18.2f}"
                f"{entry.realized_since_tracking:>18.2f}{entry.event_count:>9}"
            )


@accounting_app.command()
def events(
    database: str = typer.Option(None, "--db", help="Accounting database path."),
    symbol: str = typer.Option(None, "--symbol", help="One symbol only."),
    limit: int = typer.Option(50, "--limit", min=1, max=1000),
) -> None:
    """List realized events, newest first. Read-only."""
    with store.connect_read_only(_database(database)) as connection:
        rows = readmodel.build_events(connection, symbol=symbol, limit=limit)
    if not rows:
        typer.echo("No realized events.")
        return
    typer.echo(
        f"{'TIME (UTC)':<21}{'SYM':<7}{'QTY':>16}{'PRICE':>11}{'COST':>11}{'REALIZED':>12}  SOURCE"
    )
    for row in rows:
        # Seconds, not microseconds: the extra six digits push every other
        # column out of alignment and say nothing an operator reads.
        stamp = row.realized_at[:19].replace("T", " ")
        typer.echo(
            f"{stamp:<21}{row.symbol:<7}{row.quantity:>16}"
            f"{row.execution_price:>11.2f}{row.average_cost_before:>11.2f}"
            f"{row.net_realized_pnl:>+12.2f}  {row.provenance}"
        )


@accounting_app.command()
def inspect(
    database: str = typer.Option(None, "--db", help="Accounting database path."),
    paper_database: str = typer.Option(None, "--paper-db", help="Equity runtime store, read-only."),
    symbol: str = typer.Option(None, "--symbol", help="Restrict to one symbol."),
) -> None:
    """Show both sides of a discrepancy. Changes nothing, repairs nothing.

    Reads the broker's execution record and the ledger, and reports:
    the broker's quantity against the ledger's, the last execution the ledger
    processed for each symbol, executions the broker has that the ledger does
    not, and rows the ledger has that the broker's record no longer shows.

    There is no `--fix`. A discrepancy is resolved by understanding it and then
    rebuilding from the source events, not by editing a total.
    """
    path = _database(database)
    now = datetime.now(UTC)
    broker, read_executions, read_orders = service.build_readers()

    executions, _ = read_executions(None)
    orders, _ = read_orders(None)
    index = {record.broker_order_id: record for record in orders}
    positions = service.read_broker_positions(broker)

    with store.connect_read_only(path) as connection:
        local = store.read_all_cost_basis(connection)
        known = {
            str(row["idempotency_key"]): row
            for row in connection.execute(
                "SELECT idempotency_key, symbol, side, quantity, executed_at FROM accounting_fills"
            )
        }
        verdict = reconcile.compare(local, positions)

    typer.echo("Accounting inspection")
    typer.echo("")
    _line("Ledger", path)
    _line("Broker executions read", len(executions))
    _line("Ledger fills", len(known))
    typer.echo("")

    typer.echo(f"{'SYMBOL':<8}{'LEDGER QTY':>18}{'BROKER QTY':>18}  STATUS  LAST EXECUTION")
    for row in verdict:
        if symbol and row.symbol != symbol:
            continue
        state = local.get(row.symbol)
        last = state.last_execution_id if state else None
        typer.echo(
            f"{row.symbol:<8}{str(row.local_quantity):>18}{str(row.broker_quantity):>18}  "
            f"{row.status:<8}{last or '-'}"
        )

    missing = [
        execution
        for execution in executions
        if execution.activity_id not in known
        and (record := index.get(execution.broker_order_id)) is not None
        and record.asset_class == ingest.EQUITY_ASSET_CLASS
        and (not symbol or execution.symbol == symbol)
    ]
    typer.echo("")
    _line("Candidate missing fills", len(missing))
    for execution in missing[:50]:
        typer.echo(
            f"  {execution.transaction_time.isoformat()} {execution.symbol:<7}"
            f"{execution.side:<5}{execution.quantity} @ {execution.price}  "
            f"{execution.activity_id}"
        )

    broker_ids = {execution.activity_id for execution in executions}
    extra = [key for key in known if key not in broker_ids]
    _line("Ledger rows not in broker record", len(extra))
    for key in extra[:50]:
        row = known[key]
        typer.echo(f"  {row['executed_at']} {row['symbol']} {row['side']} {row['quantity']}  {key}")

    typer.echo("")
    typer.secho(
        "This command does not repair anything. To rebuild from the immutable "
        "source events, use `rebuild --into <new path> --confirm` and compare "
        "the result before adopting it.",
        fg=typer.colors.YELLOW,
    )
    _line("Inspected at", now.isoformat())


@accounting_app.command()
def rebuild(
    into: str = typer.Option(..., "--into", help="A NEW database path to build into."),
    paper_database: str = typer.Option(None, "--paper-db", help="Equity runtime store, read-only."),
    source_sha: str = typer.Option(None, "--source-sha"),
    confirm: bool = typer.Option(False, "--confirm", help="Required."),
) -> None:
    """Replay the broker's execution record into a fresh ledger, elsewhere.

    Never writes to an existing ledger and never overwrites one: the target
    must not exist. The way to adopt a rebuild is to compare it against the
    ledger in service and then swap the files deliberately, which is a decision
    a person makes with both files in front of them.
    """
    target = Path(into)
    if target.exists():
        typer.secho(
            f"{target} already exists. A rebuild writes a new ledger; it does not overwrite one.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    if not confirm:
        typer.secho("Refusing to rebuild without --confirm.", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=2)

    target.parent.mkdir(parents=True, exist_ok=True)
    result = service.bootstrap_exact_replay(
        database=target,
        paper_database=Path(paper_database) if paper_database else None,
        source_sha=source_sha,
        notes="Rebuilt from broker execution record by `equity-accounting rebuild`.",
    )
    _report_pass(target, result)


__all__ = ["accounting_app"]
