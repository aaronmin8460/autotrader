"""Operator CLI for the isolated real-money stack.

The process can observe while DISARMED. Broker mutation remains behind the
Prompt-1 account pin, durable arm row, environment arm gate, startup
reconciliation, deposit-day entry guard, and the shared at-most-once boundary.
This program installs and runs it with both arm gates closed.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from autotrader.account.lock import AccountExecutionLock, account_lock_path_for
from autotrader.dashboard import live_api
from autotrader.equity import EQUITY_SYMBOLS
from autotrader.equity.allocation import POLICY_LIVE_VALIDATION_100, allocation_policy_for
from autotrader.equity.live import (
    EQUITY_LIVE_LOCK_SCOPE,
    LIVE_RUNTIME_CONFIRMATION,
    ArmedLiveGateway,
    LiveStartupError,
    verify_live_startup,
)
from autotrader.equity.paper import (
    ENVIRONMENT_LIVE,
    EquityPaperConfig,
    EquityPaperRuntime,
    PaperIntegrityError,
    SqliteShadowParity,
)
from autotrader.equity.shadow import (
    DEFAULT_SHADOW_LOOKBACK_BARS,
    DEFAULT_STATE_SESSIONS,
    RegimeEquityBars,
    ShadowEquityBars,
)
from autotrader.execution.equity import AlpacaMarketCalendar, fetch_open_paper_orders
from autotrader.execution.live import (
    LiveReadOnlyClient,
    create_live_market_data_client,
    create_live_read_only_client,
    create_live_trading_client,
    verify_live_environment,
)
from autotrader.execution.models import ExecutionError, OrderSide
from autotrader.execution.paper import fetch_paper_account_state, fetch_paper_positions
from autotrader.live.activity import LiveActivityError, read_cash_flow_events
from autotrader.live.armstate import (
    LiveDisarmedError,
    arm_live_trading,
    disarm_live_trading,
)
from autotrader.live.budget import cash_flow_block_reason
from autotrader.live.identity import AccountIdentityError, require_expected_account
from autotrader.liveops import operations
from autotrader.reconciliation.engine import reconcile_paper_state
from autotrader.runtime.lock import RuntimeLock, RuntimeLockError, lock_path_for
from autotrader.runtime.runner import ShutdownRequest
from autotrader.state import sqlite as state
from autotrader.state.sqlite import StateError, connect, initialize_database

LIVE_DATABASE_ENV = "AUTOTRADER_EQUITY_LIVE_DB"
LIVE_SHADOW_DATABASE_ENV = "AUTOTRADER_EQUITY_SHADOW_DB"
LIVE_ACCOUNTING_DATABASE_ENV = "AUTOTRADER_LIVE_ACCOUNTING_DB"
DEPLOYED_SHA_ENV = "AUTOTRADER_DEPLOYED_SHA"

DEFAULT_LIVE_DATABASE = Path("data/autotrader-equity-live.db")
DEFAULT_SHADOW_DATABASE = Path("data/autotrader-shadow.db")
DEFAULT_ACCOUNTING_DATABASE = Path("data/live-accounting.db")

live_app = typer.Typer(
    name="live",
    add_completion=False,
    no_args_is_help=True,
    help="REAL MONEY operations. Readiness is not arming; all fresh state is DISARMED.",
)


def _path(option: Path | None, environment: str, fallback: Path) -> Path:
    return option or Path(os.environ.get(environment) or fallback)


def _code_sha() -> str | None:
    configured = os.environ.get(DEPLOYED_SHA_ENV, "").strip()
    if configured:
        return configured
    try:
        from autotrader.smoke.gitinfo import git_state

        return git_state(Path.cwd()).sha
    except Exception:  # noqa: BLE001 - provenance is useful but not a permission gate
        return None


def _verified_reader() -> tuple[LiveReadOnlyClient, str]:
    client = create_live_read_only_client()
    verify_live_environment(client)  # type: ignore[arg-type]
    return client, require_expected_account(client)


def _json(value: object) -> None:
    typer.echo(json.dumps(value, indent=2, default=str))


def _operation(value: operations.AccountingOperationResult) -> None:
    payload = asdict(value)
    payload["snapshot_at"] = value.snapshot_at.isoformat()
    for field in ("broker_equity", "broker_cash"):
        payload[field] = str(payload[field])
    _json(payload)


@live_app.command("status")
def status() -> None:
    """Read the real broker, pin, risk envelope, arm row and service state."""
    _json(asdict(live_api.build_panel()))


@live_app.command("reconcile")
def reconcile(
    database: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Reconcile the dedicated Live store from broker truth. Broker reads only."""
    target = _path(database, LIVE_DATABASE_ENV, DEFAULT_LIVE_DATABASE)
    try:
        client, fingerprint = _verified_reader()
        initialize_database(target)
        with connect(target) as connection:
            result = reconcile_paper_state(
                connection,
                trading_client=client,
                now=datetime.now(UTC),
                verify_environment=verify_live_environment,
                symbols=EQUITY_SYMBOLS,
                required_symbols=EQUITY_SYMBOLS,
            )
    except (ExecutionError, AccountIdentityError, StateError) as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    _json(
        {
            "account_fingerprint": fingerprint,
            "status": result.status.value,
            "safe_to_trade": result.safe_to_trade,
            "issues": result.issues_count,
            "unresolved": result.unresolved_count,
            "broker_reads": client.read_count,
        }
    )
    if not result.safe_to_trade:
        raise typer.Exit(code=2)


@live_app.command("freeze-accounting-inception")
def freeze_accounting_inception(
    database: Annotated[Path | None, typer.Option("--db")] = None,
    confirm: Annotated[bool, typer.Option("--confirm")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Freeze opening capital after proving no order or position history."""
    if not dry_run and not confirm:
        typer.secho("Refusing the irreversible inception freeze without --confirm.", err=True)
        raise typer.Exit(code=2)
    target = _path(database, LIVE_ACCOUNTING_DATABASE_ENV, DEFAULT_ACCOUNTING_DATABASE)
    try:
        client, fingerprint = _verified_reader()
        result = operations.freeze_accounting_inception(
            target,
            client,
            account_fingerprint=fingerprint,
            now=datetime.now(UTC),
            source_sha=_code_sha(),
            dry_run=dry_run,
        )
    except Exception as error:  # noqa: BLE001 - this is an operator command
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    _operation(result)


@live_app.command("accounting-sync")
def accounting_sync(
    database: Annotated[Path | None, typer.Option("--db")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Import flows and record a non-authoritative intraday broker snapshot."""
    target = _path(database, LIVE_ACCOUNTING_DATABASE_ENV, DEFAULT_ACCOUNTING_DATABASE)
    try:
        client, fingerprint = _verified_reader()
        result = operations.sync_accounting(
            target,
            client,
            account_fingerprint=fingerprint,
            now=datetime.now(UTC),
            dry_run=dry_run,
        )
    except Exception as error:  # noqa: BLE001 - this is an operator command
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    _operation(result)


@live_app.command("daily-close")
def daily_close(
    database: Annotated[Path | None, typer.Option("--db")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Record today's completed broker session exactly once."""
    target = _path(database, LIVE_ACCOUNTING_DATABASE_ENV, DEFAULT_ACCOUNTING_DATABASE)
    try:
        client, fingerprint = _verified_reader()
        result = operations.daily_close(
            target,
            client,
            AlpacaMarketCalendar(client=client),  # type: ignore[arg-type]
            account_fingerprint=fingerprint,
            now=datetime.now(UTC),
            dry_run=dry_run,
        )
    except Exception as error:  # noqa: BLE001 - this is an operator command
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    _operation(result)


@live_app.command("arm")
def arm(
    reason: Annotated[str, typer.Option("--reason")],
    confirmation: Annotated[str, typer.Option("--confirm")],
    database: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Arm only the durable row. The independent environment gate must also exist."""
    target = _path(database, LIVE_DATABASE_ENV, DEFAULT_LIVE_DATABASE)
    try:
        _, fingerprint = _verified_reader()
        initialize_database(target)
        with connect(target) as connection:
            arm_state = arm_live_trading(
                connection,
                now=datetime.now(UTC),
                reason=reason,
                confirmation=confirmation,
                code_sha=_code_sha(),
            )
    except (ExecutionError, AccountIdentityError, LiveDisarmedError, StateError) as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    _json(
        {
            "account_fingerprint": fingerprint,
            "durable_state": arm_state.state,
            "environment_gate": "INDEPENDENT",
        }
    )


@live_app.command("disarm")
def disarm(
    reason: Annotated[str, typer.Option("--reason")],
    database: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Write DISARMED immediately. This never needs a confirmation token."""
    target = _path(database, LIVE_DATABASE_ENV, DEFAULT_LIVE_DATABASE)
    try:
        initialize_database(target)
        with connect(target) as connection:
            arm_state = disarm_live_trading(
                connection,
                now=datetime.now(UTC),
                reason=reason,
                code_sha=_code_sha(),
            )
    except StateError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    _json({"durable_state": arm_state.state, "reason": arm_state.reason})


def _entry_guard(client: LiveReadOnlyClient, now: datetime) -> str | None:
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        events = read_cash_flow_events(client.get_account_activities, after=start)
    except LiveActivityError as error:
        return (
            f"The non-trade cash record is unreadable ({type(error).__name__}). No new "
            "entries today; exits remain available."
        )
    return cash_flow_block_reason(events, risk_day=now.astimezone(UTC).date())


@live_app.command("soak")
def soak(
    cycles: Annotated[int, typer.Option("--cycles", min=1, max=20)] = 3,
    database: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Repeat the real-broker startup and prove the DISARM gate refuses each cycle."""
    target = _path(database, LIVE_DATABASE_ENV, DEFAULT_LIVE_DATABASE)
    initialize_database(target)
    client, _ = _verified_reader()
    policy = allocation_policy_for(POLICY_LIVE_VALIDATION_100)
    records: list[dict[str, object]] = []
    try:
        with connect(target) as connection:
            before_intents = len(state.list_order_intents(connection))
            for cycle in range(1, cycles + 1):
                now = datetime.now(UTC)
                report = verify_live_startup(
                    connection,
                    client,
                    policy=policy,
                    now=now,
                )
                gateway = ArmedLiveGateway(
                    trading_client=client,
                    policy=policy,
                    entry_guard=lambda moment: _entry_guard(client, moment),
                )
                refused = False
                try:
                    gateway.execute(
                        connection,
                        symbol="SPY",
                        side=OrderSide.BUY,
                        requested_quantity=Decimal("0.001"),
                        now=now,
                        strategy_run_id=None,
                    )
                except LiveDisarmedError:
                    refused = True
                if not refused:
                    raise RuntimeError("the DISARM gate did not refuse the soak intent")
                records.append(
                    {
                        "cycle": cycle,
                        "account_fingerprint": report.account_fingerprint,
                        "reconciliation_status": report.reconciliation_status,
                        "armed": report.armed,
                        "ceilings": report.ceilings.to_json_dict(),
                        "gateway": "REFUSED_DISARMED",
                    }
                )
            after_intents = len(state.list_order_intents(connection))
    except Exception as error:  # noqa: BLE001 - a soak failure must stop visibly
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    _json(
        {
            "cycles": records,
            "broker_reads": client.read_count,
            "order_intents_before": before_intents,
            "order_intents_after": after_intents,
            "broker_mutations": 0,
        }
    )


@live_app.command("run")
def run(
    confirm_runtime_environment: Annotated[str, typer.Option("--confirm-runtime-environment")] = "",
    database: Annotated[Path | None, typer.Option("--db")] = None,
    shadow_database: Annotated[Path | None, typer.Option("--shadow-db")] = None,
    stage: Annotated[str, typer.Option("--stage")] = "A",
    sizing_policy: Annotated[str, typer.Option("--sizing-policy")] = POLICY_LIVE_VALIDATION_100,
    safety_delay: Annotated[float, typer.Option("--safety-delay", min=0)] = 120.0,
    once: Annotated[bool, typer.Option("--once")] = False,
) -> None:
    """Run EDA-1 against the pinned real account, observing while DISARMED."""
    if confirm_runtime_environment != LIVE_RUNTIME_CONFIRMATION:
        typer.secho(
            f"Refusing to start without --confirm-runtime-environment {LIVE_RUNTIME_CONFIRMATION}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    if sizing_policy != POLICY_LIVE_VALIDATION_100:
        typer.secho(
            f"The Live runtime accepts only {POLICY_LIVE_VALIDATION_100}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    target = _path(database, LIVE_DATABASE_ENV, DEFAULT_LIVE_DATABASE)
    shadow = _path(shadow_database, LIVE_SHADOW_DATABASE_ENV, DEFAULT_SHADOW_DATABASE)
    policy = allocation_policy_for(POLICY_LIVE_VALIDATION_100)
    config = EquityPaperConfig(
        policy=policy,
        stage=stage,
        safety_delay=timedelta(seconds=safety_delay),
        lookback_bars=DEFAULT_SHADOW_LOOKBACK_BARS,
        state_sessions=DEFAULT_STATE_SESSIONS,
        code_sha=_code_sha(),
        require_parity=True,
        require_external_safety=False,
    )
    initialize_database(target)
    lock = RuntimeLock(lock_path_for(target, scope=EQUITY_LIVE_LOCK_SCOPE))
    shutdown = ShutdownRequest()
    try:
        lock.acquire()
        shutdown.install()
        with connect(target) as connection:
            trading_client = create_live_trading_client()
            read_client = LiveReadOnlyClient(trading_client)
            startup = verify_live_startup(
                connection,
                trading_client,
                policy=policy,
                now=datetime.now(UTC),
            )
            data_client = create_live_market_data_client()

            def broker_state() -> tuple[float, dict[str, object]]:
                account = fetch_paper_account_state(trading_client)
                return account.equity, dict(fetch_paper_positions(trading_client))

            calendar = AlpacaMarketCalendar(client=trading_client)
            runtime = EquityPaperRuntime(
                connection,
                market_data=ShadowEquityBars(calendar, client=data_client),
                regime_data=RegimeEquityBars(calendar, client=data_client),
                calendar=calendar,
                gateway=ArmedLiveGateway(
                    trading_client=trading_client,
                    data_client=data_client,
                    account_lock=AccountExecutionLock(account_lock_path_for(target)),
                    policy=policy,
                    entry_guard=lambda moment: _entry_guard(read_client, moment),
                ),
                parity=SqliteShadowParity(shadow),
                config=config,
                broker_state=broker_state,
                open_orders=lambda: fetch_open_paper_orders(trading_client),
                shutdown=shutdown,
                environment=ENVIRONMENT_LIVE,
            )
            typer.echo("AUTO TRADER - EQUITY EDA-1 LIVE · REAL MONEY")
            typer.echo(f"Account fingerprint: {startup.account_fingerprint}")
            typer.echo(f"Policy: {POLICY_LIVE_VALIDATION_100}")
            typer.echo(f"Reconciliation: {startup.reconciliation_status}")
            typer.echo(f"Live armed: {'YES' if startup.armed else 'NO'}")
            typer.echo(json.dumps(startup.ceilings.to_json_dict(), sort_keys=True))
            if once:
                runtime.run_once()
            else:
                runtime.run_forever()
            if runtime.state.value == "FAILED":
                raise typer.Exit(code=1)
    except (RuntimeLockError, StateError, ExecutionError, AccountIdentityError) as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    except (PaperIntegrityError, LiveStartupError) as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None
    finally:
        shutdown.restore()
        lock.release()


__all__ = [
    "DEFAULT_ACCOUNTING_DATABASE",
    "DEFAULT_LIVE_DATABASE",
    "DEFAULT_SHADOW_DATABASE",
    "live_app",
]
