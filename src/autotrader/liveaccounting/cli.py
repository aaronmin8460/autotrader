"""Operator commands for the real-money accounting ledger. Reads, and one freeze.

Every command here is a read, an append, or a replay from the authoritative
ledger. There is no command that edits the high-water mark, adjusts the bucket,
authorizes a withdrawal or initiates a transfer - because a performance ledger
with such a command is a ledger whose numbers stop meaning anything, and
because the broker surface this repository can reach has no transfer endpoint
to call in any case.

`freeze-inception` is the one command that writes something irreversible, so it
requires `--confirm`, refuses an account that has ever traded, and refuses to
run twice.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import typer

from autotrader.liveaccounting import readmodel, service, store
from autotrader.liveaccounting.models import ProfitReservePolicy

live_accounting_app = typer.Typer(
    name="live-accounting",
    add_completion=False,
    no_args_is_help=True,
    help=(
        "Flow-adjusted performance accounting for the real-money book. "
        "OBSERVE_ONLY: nothing here moves money, authorizes a withdrawal or "
        "reaches a trading decision."
    ),
)

_WIDTH = 34


def _line(label: str, value: object) -> None:
    typer.echo(f"{label + ':':<{_WIDTH}}{value}")


def _open(database: str, fingerprint: str, now: datetime, source_sha: str | None = None):
    return service.open_ledger(
        database, account_fingerprint=fingerprint, now=now, source_sha=source_sha
    )


@live_accounting_app.command("freeze-inception")
def freeze_inception(
    database: str = typer.Option(..., "--db", help="Live accounting ledger path."),
    fingerprint: str = typer.Option(..., "--account-fingerprint", help="The pinned account."),
    equity: str = typer.Option(..., "--equity", help="Broker equity at the snapshot, USD."),
    cash: str = typer.Option(..., "--cash", help="Broker cash at the snapshot, USD."),
    positions: int = typer.Option(..., "--positions", help="Open positions the broker reports."),
    orders: int = typer.Option(..., "--orders", help="Orders the account has EVER placed."),
    source_sha: str = typer.Option(None, "--source-sha", help="Commit that froze this baseline."),
    note: str = typer.Option(None, "--note", help="Why this baseline was taken when it was."),
    confirm: bool = typer.Option(False, "--confirm", help="Required. Freezes the baseline."),
) -> None:
    """Freeze the accounting baseline from a pre-trade broker snapshot.

    The opening equity becomes CAPITAL, not profit and not a deposit. The
    broker activity that delivered it stays in the flow ledger as history and
    contributes nothing to `confirmed_deposits`.

    Refused if the account has ever placed an order or holds a position: the
    baseline shortcut is only honest on an account with no history, and
    silently resetting one that has history is the failure this refuses.
    """
    if not confirm:
        typer.secho(
            "Refusing to freeze the accounting baseline without --confirm. This is the "
            "origin of every figure the system will publish and it is written once.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=2)

    now = datetime.now(UTC)
    connection = _open(database, fingerprint, now, source_sha)
    try:
        service.establish_inception(
            connection,
            service.BrokerSnapshot(
                taken_at=now,
                equity=Decimal(equity),
                cash=Decimal(cash),
                position_count=positions,
                order_count=orders,
            ),
            account_fingerprint=fingerprint,
            policy=ProfitReservePolicy(),
            now=now,
            note=note,
        )
    except Exception as error:  # noqa: BLE001 - the message is the product
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    finally:
        connection.close()

    _line("ACCOUNTING INCEPTION", now.isoformat())
    _line("baseline equity (CAPITAL)", f"${Decimal(equity)}")
    _line("baseline cash", f"${Decimal(cash)}")
    _line("account fingerprint", fingerprint)
    _line("withdrawal mode", "OBSERVE_ONLY")
    typer.secho("Baseline frozen. No money moved and no order was placed.", fg=typer.colors.GREEN)


@live_accounting_app.command()
def checkpoint(
    database: str = typer.Option(..., "--db", help="Live accounting ledger path."),
    fingerprint: str = typer.Option(..., "--account-fingerprint", help="The pinned account."),
    equity: str = typer.Option(..., "--equity", help="Broker equity, USD."),
    cash: str = typer.Option(..., "--cash", help="Broker cash, USD."),
    kind: str = typer.Option("INTRADAY", "--kind", help="INTRADAY or DAILY_CLOSE."),
    positions: int = typer.Option(0, "--positions", help="Open positions."),
) -> None:
    """Record broker truth at this instant. DAILY_CLOSE is the authoritative kind."""
    now = datetime.now(UTC)
    connection = _open(database, fingerprint, now)
    try:
        service.take_checkpoint(
            connection,
            service.BrokerSnapshot(now, Decimal(equity), Decimal(cash), positions, 0),
            account_fingerprint=fingerprint,
            kind=kind,
            now=now,
        )
        service.rebuild(connection, policy=ProfitReservePolicy(), now=now, reason="checkpoint")
    finally:
        connection.close()
    _line("checkpoint", f"{kind} at {now.isoformat()}")


@live_accounting_app.command()
def rebuild(
    database: str = typer.Option(..., "--db", help="Live accounting ledger path."),
    fingerprint: str = typer.Option(..., "--account-fingerprint", help="The pinned account."),
    reason: str = typer.Option("operator replay", "--reason", help="Why the replay was run."),
) -> None:
    """Recompute every derived figure from the authoritative ledger.

    Total, not incremental: the reserve history is dropped and rewritten from
    the baseline, the flows and the checkpoints. A late or backdated flow needs
    nothing more than this.
    """
    now = datetime.now(UTC)
    connection = _open(database, fingerprint, now)
    try:
        count = service.rebuild(connection, policy=ProfitReservePolicy(), now=now, reason=reason)
    finally:
        connection.close()
    _line("rebuilds performed", count)


@live_accounting_app.command()
def summary(
    database: str = typer.Option(..., "--db", help="Live accounting ledger path."),
    fingerprint: str = typer.Option(None, "--account-fingerprint", help="Expected account."),
    as_json: bool = typer.Option(False, "--json", help="Emit the frozen contract payload."),
) -> None:
    """Show the frozen contract payload. Reads only."""
    path = Path(database)
    if not path.exists():
        typer.secho(f"No live accounting ledger at {path}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    with store.connect_read_only(path) as connection:
        payload = readmodel.build_summary(
            connection, now=datetime.now(UTC), expected_fingerprint=fingerprint
        )
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    for label, name in (
        ("accounting status", "accounting_status"),
        ("inception", "accounting_inception_at"),
        ("baseline equity (CAPITAL)", "baseline_equity"),
        ("current broker equity", "current_broker_equity"),
        ("confirmed deposits", "confirmed_deposits"),
        ("confirmed withdrawals", "confirmed_withdrawals"),
        ("net external flows", "net_external_flows"),
        ("FLOW-ADJUSTED EQUITY", "flow_adjusted_equity"),
        ("TRADING P&L since inception", "trading_pnl_since_inception"),
        ("time-weighted return", "time_weighted_return"),
        ("adjusted equity HWM", "adjusted_equity_hwm"),
        ("drawdown from HWM", "current_drawdown_from_adjusted_hwm"),
        ("profit reserve accrued", "profit_reserve_accrued"),
        ("withdrawal bucket", "withdrawal_bucket_balance"),
        ("withdrawal preview", "withdrawal_preview"),
        ("withdrawal authorized", "withdrawal_authorized"),
        ("withdrawal mode", "withdrawal_mode"),
        ("broker withdrawable cash", "broker_withdrawable_cash_status"),
        ("unknown external flows", "unknown_external_flow_count"),
    ):
        value = payload.get(name)
        _line(label, "unknown" if value is None else value)
    detail = payload.get("accounting_status_detail")
    if detail:
        typer.echo("")
        typer.secho(str(detail), fg=typer.colors.YELLOW)


__all__ = ["live_accounting_app"]
