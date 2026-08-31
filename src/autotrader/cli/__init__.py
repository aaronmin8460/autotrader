"""Command-line entry point for the 24/7 crypto system.

The commands are application metadata, a historical crypto market-data
download, read-only validation of an already-downloaded dataset, a local
backtest of the EMA crossover strategy over a stored dataset, a single,
deliberately awkward **paper** order submission, and a crash-recovery
reconciliation of local state against the paper broker.

Two products share this CLI and one Alpaca paper account. The crypto commands
are BTC/USD and ETH/USD, 15-minute bars, UTC dates, 24/7. The `equity-`
commands are the ten Equity V0.2 symbols, 15-minute bars, US market calendar
dates, and **regular market hours only**. Neither activates the other: there is
no combined runtime here, and each runtime processes its own universe.

`paper-submit` and `crypto-run` are the only commands that can **submit** an
order, and they can only ever reach Alpaca paper. Both require an environment
gate and an explicit confirmation token, both closed by default, and there is
no `--live` option, no `--paper` option, and no way to ask for anything but
paper (docs/SPEC.md section 8, C7).

`reconcile` also reaches the broker, but only to read it. It may rewrite local
SQLite state from what the broker reports; it can never place an order, and it
needs neither gate for that reason (docs/SPEC.md section 8, C8).

`crypto-run` is the 24/7 runtime (C9). It wakes on completed 15-minute UTC
boundaries, every day of the week, and additionally refuses to submit until
startup reconciliation reports that trading is safe - which is now the real
Phase 8 pass (`reconcile_paper_state`) rather than a placeholder, so a
`crypto-run` start reconciles against the paper broker on its own.

`equity-run` is its regular-session counterpart. It is gated identically -
`AUTOTRADER_PAPER_TRADING_ENABLED`, `--confirm-paper-runtime PAPER`, and a safe
startup reconciliation - and adds one gate of its own: the US regular market
session has to be open, read from the broker's calendar and confirmed against
the broker's clock. A cycle outside the session does nothing at all.
"""

import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from autotrader import __version__
from autotrader.backtest import (
    DEFAULT_INITIAL_CASH,
    STRATEGY_NAME,
    TAKER_FEE_RATE,
    BacktestInputError,
    BacktestResult,
    run_backtest,
)
from autotrader.data.historical import (
    SUPPORTED_SYMBOLS,
    SUPPORTED_TIMEFRAME,
    HistoricalDataError,
    download_bars,
)
from autotrader.data.validation import (
    CRYPTO_UNIVERSE_LABEL,
    EQUITY_UNIVERSE_LABEL,
    ValidationInputError,
    ValidationResult,
    read_bars,
    validate_parquet_file,
)
from autotrader.equity import EQUITY_SYMBOLS, EQUITY_TIMEFRAME, EquityError
from autotrader.equity.data import FEED as EQUITY_FEED
from autotrader.equity.data import download_bars as download_equity_bars
from autotrader.equity.market_data import AlpacaEquityBars
from autotrader.equity.runtime import (
    EQUITY_LOCK_SCOPE,
    EquityRuntime,
    EquityRuntimeConfig,
    PaperEquityExecutionGateway,
)
from autotrader.equity.runtime import PROCESSING_ORDER as EQUITY_PROCESSING_ORDER
from autotrader.equity.session import SessionError, is_market_open
from autotrader.equity.shadow import (
    DEFAULT_SHADOW_LOOKBACK_BARS,
    DEFAULT_STATE_SESSIONS,
    EQUITY_SHADOW_LOCK_SCOPE,
    MAX_SHADOW_LOOKBACK_BARS,
    MAX_STATE_SESSIONS,
    MIN_SHADOW_LOOKBACK_BARS,
    MIN_STATE_SESSIONS,
    EquityShadowConfig,
    EquityShadowRuntime,
    RegimeEquityBars,
    ShadowEquityBars,
    ShadowIntegrityError,
)
from autotrader.execution import paper as paper_execution
from autotrader.execution.equity import AlpacaMarketCalendar, execute_equity_paper_order
from autotrader.execution.models import ExecutionError, format_quantity, parse_quantity
from autotrader.execution.paper import (
    AmbiguousSubmissionError,
    ExecutionOutcome,
    PaperExecutionResult,
)
from autotrader.ml.cli import app as ml_app
from autotrader.reconciliation import (
    ItemOutcome,
    ReconciliationError,
    ReconciliationResult,
    ReconciliationStatus,
    reconcile_paper_state,
)
from autotrader.research.cli import app as research_app
from autotrader.runtime.checkpoint import SqliteCheckpoint
from autotrader.runtime.execution import PaperExecutionGateway
from autotrader.runtime.lock import RuntimeLock, RuntimeLockError, lock_path_for
from autotrader.runtime.market_data import AlpacaCryptoBars
from autotrader.runtime.monitoring import LOGGER_NAME, RuntimeState
from autotrader.runtime.runner import (
    PROCESSING_ORDER,
    RUNTIME_CONFIRMATION_TOKEN,
    CryptoRuntime,
    RuntimeConfig,
    ShutdownRequest,
)
from autotrader.runtime.safety import (
    RECONCILIATION_NOT_SAFE_BANNER,
    STARTUP_SAFETY_SAFE,
    startup_safety_from_reconciliation,
)
from autotrader.runtime.schedule import (
    DEFAULT_LOOKBACK_BARS,
    DEFAULT_SAFETY_DELAY,
    ScheduleError,
)
from autotrader.state.sqlite import DEFAULT_DATABASE_PATH, StateError, connect, initialize_database

DEFAULT_OUTPUT_DIR = Path("data/raw")

#: `validate` exit codes. 0 is a valid dataset.
INVALID_DATASET_EXIT_CODE = 1
UNREADABLE_INPUT_EXIT_CODE = 2

#: `backtest` exit codes. 0 is a completed simulation; 2 is a shared
#: unreadable-input failure.
BACKTEST_INPUT_EXIT_CODE = 1

#: `paper-submit` exit codes.
#:
#: 0  the order was submitted, already existed, or a dry run completed.
#: 1  a controlled refusal - a closed gate, a wrong confirmation, a risk
#:    rejection, an untradable account, no price, a broker rejection. Nothing
#:    reached the broker, or the broker definitively refused it. No action is
#:    required beyond fixing the cause.
#: 2  the submission outcome is UNKNOWN. This is **not** an ordinary failure:
#:    an order may exist at the broker. It has its own code so that a script
#:    can never confuse it with a clean refusal.
PAPER_SUBMIT_REFUSED_EXIT_CODE = 1
PAPER_SUBMIT_UNKNOWN_EXIT_CODE = 2

#: `reconcile` exit codes, deliberately mirroring `paper-submit`.
#:
#: 0  CLEAN or REPAIRED - local state matches verified broker truth, and a
#:    runtime may start trading.
#: 1  FAILED - the pass could not complete, so nothing is known. An operational
#:    failure, reported as a message rather than a traceback.
#: 2  UNRESOLVED - the pass completed and something remains ambiguous, which is
#:    the same situation `paper-submit` reports with 2: an order may exist at
#:    the broker. Its own code so a script can never read it as a clean run.
RECONCILE_FAILED_EXIT_CODE = 1
RECONCILE_UNRESOLVED_EXIT_CODE = 2

#: `crypto-run` exit codes, deliberately the same shape as `paper-submit`'s.
#:
#: 0  the runtime ran and stopped cleanly, including a clean SIGINT/SIGTERM.
#: 1  a controlled refusal - another runner holds the lock, the configuration
#:    is unusable, or a cycle failed fatally. Nothing ambiguous happened.
#: 2  trading was paused because a submission outcome is UNKNOWN. An order may
#:    exist at the broker and must be reconciled before anything else is sent.
CRYPTO_RUN_REFUSED_EXIT_CODE = 1
CRYPTO_RUN_PAUSED_EXIT_CODE = 2

#: `equity-run` exit codes, deliberately the same shape and the same meanings
#: as `crypto-run`'s. A closed market is **not** one of them: an equity runtime
#: that woke while the session was shut, observed nothing and stopped cleanly
#: did exactly its job, and exiting non-zero for that would make every weekend
#: look like a failure to whatever supervises it.
EQUITY_RUN_REFUSED_EXIT_CODE = 1
EQUITY_RUN_PAUSED_EXIT_CODE = 2

#: `equity-submit` exit codes, deliberately identical in shape and meaning to
#: `paper-submit`'s. A closed regular session is a controlled refusal (1): the
#: order was never sent and never queued, so nothing is ambiguous about it.
EQUITY_SUBMIT_REFUSED_EXIT_CODE = PAPER_SUBMIT_REFUSED_EXIT_CODE
EQUITY_SUBMIT_UNKNOWN_EXIT_CODE = PAPER_SUBMIT_UNKNOWN_EXIT_CODE

#: `equity-shadow` exit codes. 0 is a clean stop - a shadow that found the
#: market shut and observed nothing did exactly its job. 1 is a controlled
#: refusal or a fatal failure. There is deliberately no code 2: nothing the
#: shadow runs can submit, so no outcome of it can be UNKNOWN.
EQUITY_SHADOW_REFUSED_EXIT_CODE = 1

#: Where the shadow keeps its own state, away from the trading database. The
#: two must not share a file: they would share the per-symbol bar claims, and
#: the shadow refuses any database that has ever held an order intent.
EQUITY_SHADOW_DATABASE_PATH = Path("data/autotrader-shadow.db")

#: Width of the label column in the backtest report.
_LABEL_WIDTH = 23

app = typer.Typer(
    name="autotrader",
    add_completion=False,
    no_args_is_help=True,
)


@app.callback()
def cli() -> None:
    """Personal automated 24/7 crypto trading system.

    Historical crypto market data, dataset validation, EMA crossover signals,
    local backtesting, a deterministic risk engine, local SQLite state, Alpaca
    **paper** order submission, crash-recovery reconciliation, and the 24/7
    runtime that drives all of it. Crypto spot only: BTC/USD and ETH/USD.

    There is no live trading. `paper-submit` and `crypto-run` talk to Alpaca's
    paper environment only, behind an environment gate and an explicit
    confirmation token; no command, flag, or environment variable can direct an
    order at a real-money account. `reconcile` reads that same paper account
    and repairs local state from it, and can never place an order - and
    `crypto-run` runs that same pass itself at startup, refusing to submit
    anything unless it comes back safe.
    """


# The offline ML data foundation (M1). Attached as a sub-application rather than
# as loose commands: it owns its own surface, reaches no broker, and activates
# nothing, so nothing about the trading commands changes when it does.
app.add_typer(ml_app, name="ml")


@app.command()
def version() -> None:
    """Show the installed autotrader version."""
    typer.echo(f"autotrader {__version__}")


@app.command()
def download(
    symbol: str = typer.Option(
        ...,
        "--symbol",
        help=f"Crypto pair to download. One of: {', '.join(SUPPORTED_SYMBOLS)}.",
    ),
    start: str = typer.Option(
        ...,
        "--start",
        help="First UTC calendar date to include, YYYY-MM-DD.",
    ),
    end: str = typer.Option(
        ...,
        "--end",
        help="Last UTC calendar date to include, YYYY-MM-DD. Inclusive.",
    ),
    timeframe: str = typer.Option(
        SUPPORTED_TIMEFRAME,
        "--timeframe",
        help=f"Bar timeframe. Only {SUPPORTED_TIMEFRAME!r} is supported.",
    ),
) -> None:
    """Download historical crypto bars from Alpaca and store them as Parquet.

    Crypto market data does not require credentials. If ALPACA_API_KEY and
    ALPACA_SECRET_KEY are set they are used, which raises the provider's rate
    limit. Downloaded files stay local and are git-ignored, and the pair's
    slash becomes an underscore in the filename (BTC/USD -> BTC_USD) while the
    stored data keeps the canonical BTC/USD symbol.
    """
    try:
        result = download_bars(
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            output_dir=DEFAULT_OUTPUT_DIR,
        )
    except HistoricalDataError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None

    typer.echo("Downloaded historical bars")
    typer.echo("")
    typer.echo(f"Symbol:    {result.symbol}")
    typer.echo(f"Timeframe: {result.timeframe}")
    typer.echo(f"Start:     {result.start.isoformat()}")
    typer.echo(f"End:       {result.end.isoformat()}")
    typer.echo(f"Rows:      {result.row_count}")
    typer.echo(f"Feed:      alpaca crypto ({result.feed})")
    typer.echo(f"Saved:     {result.parquet_path}")
    typer.echo(f"Metadata:  {result.metadata_path}")


def _validation_universe(equity: bool) -> tuple[tuple[str, ...], str]:
    """The symbol universe and its label for a `--equity` flag.

    One place, so `validate` and `backtest` cannot disagree about what the flag
    means.
    """
    if equity:
        return EQUITY_SYMBOLS, EQUITY_UNIVERSE_LABEL
    return SUPPORTED_SYMBOLS, CRYPTO_UNIVERSE_LABEL


def _echo_report(path: Path, result: ValidationResult) -> None:
    """Print the shared VALID/INVALID report body."""
    typer.echo("")
    typer.echo(f"File:   {path}")
    typer.echo(f"Rows:   {result.row_count}")
    if result.symbol is not None:
        typer.echo(f"Symbol: {result.symbol}")
    typer.echo(f"Errors: {result.error_count}")


@app.command()
def validate(
    path: Annotated[
        Path,
        typer.Argument(help="Path to a Parquet bar dataset written by `download`."),
    ],
    equity: bool = typer.Option(
        False,
        "--equity",
        help=(
            "Check the symbol against the ten Equity V0.2 symbols instead of the "
            "crypto pairs. Every other check is identical."
        ),
    ),
) -> None:
    """Validate a stored Parquet bar dataset against the canonical schema.

    Reads the file only. Nothing is downloaded, modified, or repaired. Exits 0
    when the dataset is valid, 1 when it has validation errors, and 2 when the
    file cannot be read at all.

    Only the symbol universe differs between the two asset classes, so
    `--equity` switches that one check rather than selecting a second
    validator: the schema, the timestamps, the OHLC relationships and the
    numeric columns are the same contract either way.
    """
    universe, label = _validation_universe(equity)
    try:
        result = validate_parquet_file(path, supported_symbols=universe, universe_label=label)
    except ValidationInputError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=UNREADABLE_INPUT_EXIT_CODE) from None

    if result.valid:
        typer.secho("VALID", fg=typer.colors.GREEN)
        _echo_report(path, result)
        return

    typer.secho("INVALID", fg=typer.colors.RED)
    _echo_report(path, result)
    typer.echo("")
    for issue in result.errors:
        typer.echo(f"- {issue.code}: {issue.message}")
    raise typer.Exit(code=INVALID_DATASET_EXIT_CODE)


def _field(label: str, value: str) -> str:
    """One aligned `label: value` line of the backtest report."""
    return f"{label + ':':<{_LABEL_WIDTH}}{value}"


def _money(amount: object) -> str:
    return f"${amount:,.2f}"


def _percent(fraction: float) -> str:
    """Render a decimal fraction as a percentage. `-0.25` becomes `-25.00%`."""
    return f"{fraction * 100:.2f}%"


def _echo_backtest_report(result: BacktestResult) -> None:
    """Print the summary. Individual executions are deliberately not listed."""
    typer.echo("AUTO TRADER BACKTEST")
    typer.echo("")
    typer.echo(_field("Symbol", result.symbol))
    typer.echo(_field("Strategy", STRATEGY_NAME))
    typer.echo(_field("Rows", str(result.bar_count)))
    typer.echo("")
    typer.echo(_field("Initial Cash", _money(result.initial_cash)))
    typer.echo(_field("Final Cash", _money(result.final_cash)))
    typer.echo(_field("Final Equity", _money(result.final_equity)))
    typer.echo(_field("Total Fees", _money(result.total_fees)))
    typer.echo("")
    typer.echo(_field("Total Return", _percent(result.total_return)))
    typer.echo(_field("Max Drawdown", _percent(result.max_drawdown)))
    typer.echo("")
    typer.echo(_field("Signals", str(result.signal_count)))
    typer.echo(_field("BUY Executions", str(result.buy_execution_count)))
    typer.echo(_field("SELL Executions", str(result.sell_execution_count)))
    typer.echo(_field("Completed Round Trips", str(result.completed_round_trips)))
    typer.echo(
        _field("Ending Position", f"{format_quantity(result.ending_position_quantity)} units")
    )
    if result.unexecuted_last_bar_signal_count:
        # No bar follows the last one, so its signal could not be filled.
        typer.echo(_field("Unexecuted Last Bar", str(result.unexecuted_last_bar_signal_count)))
    typer.echo("")
    typer.echo("Execution model:")
    typer.echo("Next-bar open, fractional quantity")
    typer.echo("")
    typer.echo("Taker fee / Slippage:")
    typer.echo(f"{_percent(float(TAKER_FEE_RATE))} per side / 0")
    typer.echo("")
    typer.echo("Engineering validation only. Not a profitability claim, and not")
    typer.echo("advice: no order was created and no broker was contacted. The fee")
    typer.echo("is a conservative flat assumption, not a provider fee schedule.")


@app.command()
def backtest(
    path: Annotated[
        Path,
        typer.Argument(help="Path to a Parquet bar dataset written by `download`."),
    ],
    initial_cash: Annotated[
        float,
        typer.Option("--initial-cash", help="Starting simulated cash, in USD."),
    ] = DEFAULT_INITIAL_CASH,
    equity: bool = typer.Option(
        False,
        "--equity",
        help="Accept a stored Equity V0.2 dataset instead of a crypto one.",
    ),
) -> None:
    """Backtest the EMA 20 / EMA 50 crossover over a stored Parquet dataset.

    The dataset is validated first and the backtest is abandoned if it fails.
    Signals fill at the next bar's open - never on the signal's own bar - with
    a conservative flat taker fee on both sides and zero slippage. Long only,
    fractional Decimal quantities, no leverage. This is a local simulation:
    nothing is downloaded, written, or ordered.

    Exits 0 on a completed simulation, 1 when the dataset or the starting cash
    is unusable, and 2 when the file cannot be read at all.
    """
    try:
        bars = read_bars(path)
    except ValidationInputError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=UNREADABLE_INPUT_EXIT_CODE) from None

    universe, label = _validation_universe(equity)
    try:
        result = run_backtest(
            bars,
            initial_cash=initial_cash,
            supported_symbols=universe,
            universe_label=label,
        )
    except BacktestInputError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=BACKTEST_INPUT_EXIT_CODE) from None

    _echo_backtest_report(result)


def _echo_paper_preview(result: PaperExecutionResult, *, dry_run: bool) -> None:
    """Print the pre-submission preview.

    Deliberately contains no credential, no authorization header, and no
    account number - only the numbers an operator needs to judge whether the
    order about to be sent is the one they meant.
    """
    decision = result.risk_decision
    typer.echo("AUTO TRADER - PAPER CRYPTO ORDER")
    typer.echo("")
    typer.echo(_field("Environment", "PAPER ONLY"))
    typer.echo(_field("Trading", "CRYPTO SPOT, 24/7"))
    typer.echo(_field("Symbol", result.symbol))
    typer.echo(_field("Side", result.side.value))
    typer.echo(_field("Requested Qty", format_quantity(result.requested_quantity)))
    typer.echo(_field("Reference Price", _money(result.reference_price)))
    typer.echo("")
    typer.echo(_field("Account Equity", _money(result.account.equity)))
    typer.echo(_field("Account Cash", _money(result.account.cash)))
    typer.echo(_field("UTC Day Baseline", _money(result.daily_baseline_equity)))
    daily_pnl = Decimal(str(result.account.equity)) - result.daily_baseline_equity
    typer.echo(_field("Daily P&L", _money(daily_pnl)))
    typer.echo("")
    if result.asset is not None:
        typer.echo(_field("Asset Min Order", format_quantity(result.asset.min_order_size)))
        typer.echo(_field("Asset Increment", format_quantity(result.asset.min_trade_increment)))
    if result.effective_minimum_quantity is not None:
        # The broker's $10 USD minimum is not in the asset metadata above, so
        # the threshold an order is actually measured against is printed
        # explicitly - it is the number an operator needs to size the request.
        typer.echo(
            _field(
                "Broker Min Qty",
                f"{format_quantity(result.effective_minimum_quantity)} "
                f"(>= ${format_quantity(paper_execution.USD_MINIMUM_ORDER_NOTIONAL)})",
            )
        )
    if result.asset is not None:
        typer.echo("")
    typer.echo(_field("Risk Decision", "APPROVED" if decision.approved else "REJECTED"))
    typer.echo(_field("Risk Reason", decision.reason_code))
    typer.echo(_field("Approved Qty", format_quantity(decision.approved_quantity)))
    if result.intent is not None:
        typer.echo(_field("Broker Qty", format_quantity(result.intent.approved_quantity)))
        typer.echo(_field("Client Order ID", result.intent.client_order_id))
    if dry_run:
        typer.echo("")
        typer.echo("DRY RUN - no order was submitted and nothing was persisted.")


@app.command(name="paper-submit")
def paper_submit(
    symbol: str = typer.Option(
        ...,
        "--symbol",
        help=f"Crypto pair to trade. One of: {', '.join(SUPPORTED_SYMBOLS)}.",
    ),
    side: str = typer.Option(..., "--side", help="BUY or SELL. Long only; no shorts."),
    qty: str = typer.Option(
        ...,
        "--qty",
        help=(
            "Quantity of the base asset to request, as a decimal number "
            "(e.g. 0.0001). Must be > 0; fractional quantities are supported."
        ),
    ),
    confirm_paper: str = typer.Option(
        "",
        "--confirm-paper",
        help=(
            f"Type {paper_execution.CONFIRMATION_TOKEN} exactly to confirm a real paper "
            "submission. Not required for --dry-run."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Evaluate everything and print the preview, but never submit.",
    ),
    database: Annotated[
        Path,
        typer.Option("--db", help="Local operational-state database."),
    ] = DEFAULT_DATABASE_PATH,
) -> None:
    """Submit one MARKET order to the Alpaca **PAPER** crypto account.

    This is the only command that reaches a broker, and it can only reach the
    paper environment. There is no live mode: no flag, option, or environment
    variable selects one, and the trading client is constructed with
    `paper=True` hardcoded.

    A real submission needs **both** gates, which are independent and both
    closed by default:

    \b
      1. AUTOTRADER_PAPER_TRADING_ENABLED=true in the environment
      2. --confirm-paper PAPER on the command line

    `--dry-run` needs neither, because it cannot submit: it reads the account,
    positions, the asset's broker metadata, and the current price, runs the
    risk engine, prints the preview, and stops without persisting an intent or
    calling the broker. Running it first is the intended way to check an order.

    The quantity sent to the broker is never larger than the risk engine's
    approved quantity, which may itself be smaller than `--qty`; it is then
    rounded **down** to the broker's own trade increment. If risk approves
    nothing, no broker request is created at all.

    Crypto trades continuously, so there is no market session to wait for and
    nothing here consults one.

    Exits 0 when the order was submitted, already existed, or a dry run
    completed; 1 on a controlled refusal; and 2 when the outcome is UNKNOWN -
    meaning an order may exist at the broker and must be reconciled before
    anything else is sent.
    """
    if not dry_run:
        try:
            paper_execution.require_paper_trading_enabled()
            paper_execution.require_confirmation(confirm_paper)
        except ExecutionError as error:
            typer.secho(str(error), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=PAPER_SUBMIT_REFUSED_EXIT_CODE) from None

    try:
        quantity = parse_quantity(qty, "--qty")
    except ExecutionError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=PAPER_SUBMIT_REFUSED_EXIT_CODE) from None

    try:
        initialize_database(database)
    except StateError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=PAPER_SUBMIT_REFUSED_EXIT_CODE) from None

    with connect(database) as connection:
        try:
            result = paper_execution.execute_paper_order(
                connection,
                symbol=symbol,
                side=side,
                requested_quantity=quantity,
                dry_run=dry_run,
                now=datetime.now(UTC),
            )
        except AmbiguousSubmissionError as error:
            # Its own exit code: an order may exist at the broker, which is a
            # different situation from "nothing happened".
            typer.secho(str(error), fg=typer.colors.YELLOW, err=True)
            raise typer.Exit(code=PAPER_SUBMIT_UNKNOWN_EXIT_CODE) from None
        except (ExecutionError, StateError) as error:
            typer.secho(str(error), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=PAPER_SUBMIT_REFUSED_EXIT_CODE) from None

    _echo_paper_preview(result, dry_run=dry_run)

    if result.outcome is ExecutionOutcome.REJECTED_BY_RISK:
        typer.echo("")
        typer.secho("REJECTED BY RISK ENGINE", fg=typer.colors.RED)
        typer.echo(result.message)
        typer.echo("No order was created and no broker request was made.")
        raise typer.Exit(code=PAPER_SUBMIT_REFUSED_EXIT_CODE)

    if result.outcome is ExecutionOutcome.DRY_RUN:
        return

    snapshot = result.broker_order
    typer.echo("")
    if result.outcome is ExecutionOutcome.DUPLICATE:
        typer.secho("ALREADY SUBMITTED", fg=typer.colors.YELLOW)
        typer.echo("The broker already had an order under this client order ID.")
    else:
        typer.secho("SUBMITTED TO PAPER ACCOUNT", fg=typer.colors.GREEN)
    if snapshot is not None:
        typer.echo("")
        typer.echo(_field("Broker Order ID", snapshot.broker_order_id))
        typer.echo(_field("Client Order ID", snapshot.client_order_id))
        typer.echo(_field("Submitted Qty", format_quantity(snapshot.quantity)))
        typer.echo(_field("Broker Status", snapshot.status))
        typer.echo(_field("Filled Qty", format_quantity(snapshot.filled_quantity)))
    typer.echo("")
    typer.echo("Accepted is not filled. Local positions are not updated from an")
    typer.echo("accepted order. Run `autotrader reconcile` to settle it against the broker.")


_RECONCILE_EXIT_CODES = {
    ReconciliationStatus.CLEAN: 0,
    ReconciliationStatus.REPAIRED: 0,
    ReconciliationStatus.UNRESOLVED: RECONCILE_UNRESOLVED_EXIT_CODE,
    ReconciliationStatus.FAILED: RECONCILE_FAILED_EXIT_CODE,
}

_RECONCILE_STATUS_COLOURS = {
    ReconciliationStatus.CLEAN: typer.colors.GREEN,
    ReconciliationStatus.REPAIRED: typer.colors.GREEN,
    ReconciliationStatus.UNRESOLVED: typer.colors.YELLOW,
    ReconciliationStatus.FAILED: typer.colors.RED,
}


def _echo_reconcile_report(result: ReconciliationResult, *, dry_run: bool) -> None:
    """Print what the pass found, grouped so the blocking items are last.

    Contains no credential and no account number: every line is built from
    symbols, quantities, statuses, and `client_order_id` values.
    """
    typer.echo("AUTO TRADER - PAPER RECONCILIATION")
    typer.echo("")
    typer.echo(_field("Environment", "PAPER ONLY"))
    typer.echo(_field("Mode", "DRY RUN (read-only)" if dry_run else "REPAIR"))
    typer.secho(
        _field("Status", result.status.value),
        fg=_RECONCILE_STATUS_COLOURS[result.status],
    )
    typer.secho(
        _field("Safe To Trade", "YES" if result.safe_to_trade else "NO"),
        fg=typer.colors.GREEN if result.safe_to_trade else typer.colors.RED,
    )
    typer.echo("")
    typer.echo(_field("Orders Checked", str(result.orders_checked)))
    typer.echo(_field("Positions Checked", str(result.positions_checked)))
    typer.echo(_field("Repaired", str(result.repaired_count)))
    typer.echo(_field("Unresolved", str(result.unresolved_count)))
    if result.reconciliation_run_id is not None:
        typer.echo(_field("Run ID", str(result.reconciliation_run_id)))

    for heading, outcome in (
        ("Repaired:", ItemOutcome.REPAIRED),
        ("Observed:", ItemOutcome.OBSERVED),
        ("Unresolved:", ItemOutcome.UNRESOLVED),
        ("Failed:", ItemOutcome.FAILED),
    ):
        matching = [issue for issue in result.issues if issue.outcome is outcome]
        if not matching:
            continue
        typer.echo("")
        typer.echo(heading)
        for issue in matching:
            target = issue.symbol or issue.client_order_id or issue.category
            typer.echo(f"- {target}: {issue.detail}")

    typer.echo("")
    if dry_run:
        typer.echo("DRY RUN - nothing was written to the database.")
    typer.echo("No order was submitted. Reconciliation never places a trade.")


@app.command()
def reconcile(
    database: Annotated[
        Path,
        typer.Option("--db", help="Local operational-state database."),
    ] = DEFAULT_DATABASE_PATH,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report what would be repaired without writing anything.",
    ),
) -> None:
    """Reconcile local state against the Alpaca **PAPER** broker after a crash.

    The broker is the truth about orders, fills, and positions; the local
    database is durable intent, an audit trail, and a last-known snapshot.
    This command reads the broker and rewrites local rows to match it.

    **It can never submit an order.** An `UNKNOWN` intent is resolved by asking
    the broker about its existing `client_order_id`, never by sending a second
    order; a stale intent the broker confirms it never received is closed off
    rather than executed; and a position mismatch is corrected in the database,
    never by trading. That is why this command needs neither the environment
    gate nor a confirmation token: there is nothing here to confirm.

    **The pass covers the whole account: all twelve tracked symbols**, both
    crypto pairs and all ten equities, plus every order intent regardless of
    which book created it. That scope is what lets it clear the shared account
    safety halt - a pass over fewer symbols reports honestly on what it
    covered, and is refused as evidence that the account is understood.

    `--dry-run` reports exactly the same findings and reconciles nothing into
    the database - no repair, no audit row. Run it first to see what a real
    pass would change. It does still *open* the database, which applies any
    pending schema migration, exactly as every other command here does; that is
    a structural upgrade, not a reconciliation result.

    Exits 0 when the result is CLEAN or REPAIRED, which is the answer a 24/7
    runtime needs before it may start trading; 1 when the pass FAILED and
    nothing is known; and 2 when it completed but something is UNRESOLVED -
    meaning an order may exist at the broker and must be settled by hand.
    """
    try:
        initialize_database(database)
    except StateError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=RECONCILE_FAILED_EXIT_CODE) from None

    with connect(database) as connection:
        try:
            result = reconcile_paper_state(
                connection,
                dry_run=dry_run,
                now=None,
            )
        except (ReconciliationError, ExecutionError, StateError) as error:
            typer.secho(str(error), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=RECONCILE_FAILED_EXIT_CODE) from None

    _echo_reconcile_report(result, dry_run=dry_run)

    exit_code = _RECONCILE_EXIT_CODES[result.status]
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


def _configure_runtime_logging(verbose: bool) -> None:
    """Send structured runtime events to stdout, once, without stomping on a host.

    stdout rather than a repository file: journald, `docker logs`, and a shell
    redirect all already know what to do with it, and a daemon that insists on
    owning a log path is a deployment decision made in the wrong place.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)sZ %(levelname)s %(message)s"))
        handler.formatter.converter = time.gmtime  # type: ignore[union-attr]
        logger.addHandler(handler)
        logger.propagate = False


def _echo_runtime_banner(runtime: CryptoRuntime, *, once: bool, lock: Path) -> None:
    """Print what this process is and is not allowed to do, before it does it."""
    authorization = runtime.authorization
    heartbeat = runtime.heartbeat
    typer.echo("AUTO TRADER - 24/7 CRYPTO RUNTIME")
    typer.echo("")
    typer.echo(_field("Environment", "PAPER ONLY"))
    typer.echo(_field("Trading", "CRYPTO SPOT, 24/7"))
    typer.echo(_field("Symbols", ", ".join(PROCESSING_ORDER)))
    typer.echo(_field("Mode", "ONCE" if once else "RUN"))
    typer.echo(_field("Bar Interval", "15m, completed bars only"))
    typer.echo(_field("Reconciliation", heartbeat.reconciliation_status or "NOT RUN"))
    typer.echo(_field("Startup Safety", heartbeat.startup_safety_code))
    typer.echo(_field("Lock File", str(lock)))
    typer.echo("")
    if heartbeat.startup_safety_code != STARTUP_SAFETY_SAFE:
        # Loud, and separate from the execution line below: a closed paper gate
        # and an unsafe reconciliation both end in "no order", and an operator
        # has to be able to tell which one they are looking at.
        typer.secho(RECONCILIATION_NOT_SAFE_BANNER, fg=typer.colors.RED)
        typer.echo(runtime.startup_safety_message)
        typer.echo("")
    if authorization.enabled:
        typer.secho("PAPER EXECUTION ENABLED", fg=typer.colors.YELLOW)
        typer.echo("Signals on completed bars may be submitted to the PAPER account.")
    else:
        typer.secho("OBSERVATION ONLY - NO ORDER WILL BE SUBMITTED", fg=typer.colors.GREEN)
        typer.echo(f"Reason: {authorization.reason}")
    typer.echo("")


def _echo_runtime_summary(runtime: CryptoRuntime) -> None:
    """Print the final heartbeat as an operator-readable block."""
    heartbeat = runtime.heartbeat
    typer.echo("")
    typer.echo(_field("Final State", heartbeat.state.value))
    typer.echo(_field("Cycles Started", str(heartbeat.cycles_started)))
    typer.echo(_field("Cycles Completed", str(heartbeat.cycles_completed)))
    for symbol, timestamp in heartbeat.last_processed_bars.items():
        label = f"Last {symbol} Bar"
        typer.echo(_field(label, timestamp.isoformat() if timestamp else "none"))
    for symbol, timestamp in runtime.checkpoints.items():
        typer.echo(_field(f"Checkpoint {symbol}", timestamp.isoformat()))
    typer.echo(_field("Orders Submitted", str(heartbeat.orders_submitted)))
    typer.echo(_field("Provider Calls", str(heartbeat.api_calls_total)))
    if heartbeat.last_error is not None:
        typer.echo(_field("Last Error", heartbeat.last_error))


@app.command(name="crypto-run")
def crypto_run(
    once: bool = typer.Option(
        False,
        "--once",
        help="Process the current completed-bar cycle once and exit, without waiting.",
    ),
    confirm_paper_runtime: str = typer.Option(
        "",
        "--confirm-paper-runtime",
        help=(
            f"Type {RUNTIME_CONFIRMATION_TOKEN} exactly to authorize THIS process to use "
            "the paper execution path for its lifetime. Without it the runtime only "
            "observes."
        ),
    ),
    observe_only: bool = typer.Option(
        False,
        "--observe-only",
        help=(
            "Run without an execution path at all: bars, validation, strategy and "
            "signals only. Submission is not refused, it is unavailable."
        ),
    ),
    safety_delay: float = typer.Option(
        DEFAULT_SAFETY_DELAY.total_seconds(),
        "--safety-delay",
        help=(
            "Seconds to wait after a 15-minute UTC boundary before treating the bar "
            "that just closed as fetchable. Covers provider publication lag."
        ),
    ),
    database: Annotated[
        Path,
        typer.Option("--db", help="Local operational-state database."),
    ] = DEFAULT_DATABASE_PATH,
) -> None:
    """Run the 24/7 crypto runtime on completed 15-minute UTC bars.

    Wakes at 00, 15, 30 and 45 minutes past every hour - every day, weekends
    included - waits a small safety delay for the provider to publish, then
    processes BTC/USD and then ETH/USD in that fixed order: fetch a bounded
    window of recent completed bars, validate it, evaluate EMA 20 / EMA 50 on
    the newest completed bar only, record any signal, and - if and only if
    every gate is open - hand that signal to the existing paper execution path.

    An in-progress candle is never processed, and a completed bar is never
    processed twice - not within one process, and not across a restart: the
    per-symbol bar checkpoint is committed to SQLite before the bar can reach
    the strategy, so a restarted runner skips what its predecessor claimed. The
    preference that encodes is deliberate and one-sided: miss a trade rather
    than duplicate a trade.

    \b
    Every start reconciles first, in this order:
      1. acquire the single-instance crypto runtime lock - a different file
         from the equity runner's, so the two services run side by side
      2. open the database and apply any pending migration
      3. run a FULL-UNIVERSE reconciliation against the Alpaca PAPER account:
         all twelve tracked symbols, both books, plus every order intent
      4. CLEAN or REPAIRED over the whole universe -> the shared account
         safety state becomes SAFE; anything else leaves it halted
      5. safe_to_trade -> the crypto runtime starts

    Running `autotrader reconcile` beforehand is NOT required; that command
    remains available for diagnostics and manual repair.

    \b
    Unattended paper execution requires ALL of:
      1. AUTOTRADER_PAPER_TRADING_ENABLED=true in the environment
      2. --confirm-paper-runtime PAPER on the command line
      3. startup reconciliation reporting that trading is safe
      4. the shared account safety state being SAFE at the moment of submission

    No environment variable and no flag combination bypasses the third or the
    fourth. The fourth is account-wide and durable: an ambiguous submission
    raised by the *equity* service halts this one too, across processes and
    across restarts, and only a full-universe reconciliation clears it.

    A start that is not safe prints RECONCILIATION NOT SAFE - TRADING DISABLED,
    keeps observing - fetching, validating, evaluating, recording, logging -
    and submits nothing.

    There is no live mode. `--observe-only` goes further than the gates and
    constructs no execution path at all; it still reports startup safety.

    Only one runner may hold a given database at a time; a second exits
    immediately rather than processing the same bar twice.

    Exits 0 on a clean stop including SIGINT/SIGTERM, 1 on a controlled refusal
    or a fatal cycle failure, and 2 when trading was paused by an UNKNOWN
    submission outcome that must be reconciled.
    """
    _configure_runtime_logging(verbose=False)

    try:
        config = RuntimeConfig(
            safety_delay=timedelta(seconds=safety_delay),
            lookback_bars=DEFAULT_LOOKBACK_BARS,
            observe_only=observe_only,
            runtime_confirmation=confirm_paper_runtime or None,
        )
    except ScheduleError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=CRYPTO_RUN_REFUSED_EXIT_CODE) from None

    try:
        initialize_database(database)
    except StateError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=CRYPTO_RUN_REFUSED_EXIT_CODE) from None

    lock = RuntimeLock(lock_path_for(database))
    try:
        lock.acquire()
    except RuntimeLockError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=CRYPTO_RUN_REFUSED_EXIT_CODE) from None

    shutdown = ShutdownRequest()
    shutdown.install()
    try:
        with connect(database) as connection:
            runtime = CryptoRuntime(
                connection,
                market_data=AlpacaCryptoBars(safety_delay=config.safety_delay),
                execution=None if observe_only else PaperExecutionGateway(),
                # The startup trading authority, over the **whole account**:
                # this process sizes against total exposure, which includes the
                # equity book it does not trade, and only a full-universe pass
                # may clear the shared account halt. Runs on every start,
                # including `--observe-only`, because knowing whether local
                # state survived is useful even when nothing could be submitted.
                startup_safety=startup_safety_from_reconciliation(connection),
                checkpoint=SqliteCheckpoint(connection),
                config=config,
                shutdown=shutdown,
            )
            runtime.start()
            _echo_runtime_banner(runtime, once=once, lock=lock.path)
            try:
                if once:
                    runtime.run_cycle()
                else:
                    runtime.run_forever()
            finally:
                # `stop` is idempotent, so the long-running path's own call
                # makes this a no-op. It matters for `--once`: a cycle that
                # raised must still close its strategy run rather than leave a
                # RUNNING row behind for the next start to trip over.
                runtime.stop()
            _echo_runtime_summary(runtime)
            state = runtime.heartbeat.state
    finally:
        # `finally`, not a happy-path release: a lock that outlives a crashed
        # runner would refuse every later start for a process that no longer
        # exists.
        shutdown.restore()
        lock.release()

    if state is RuntimeState.TRADING_PAUSED:
        typer.echo("")
        typer.secho("TRADING PAUSED - SUBMISSION OUTCOME UNKNOWN", fg=typer.colors.YELLOW, err=True)
        typer.echo(
            "An order may exist at the broker. Nothing further was submitted, and "
            "nothing here resolves it: reconcile against the broker before starting "
            "the runtime again.",
            err=True,
        )
        raise typer.Exit(code=CRYPTO_RUN_PAUSED_EXIT_CODE)
    if state is RuntimeState.FAILED:
        typer.echo("")
        typer.secho("RUNTIME STOPPED ON A FATAL ERROR", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=CRYPTO_RUN_REFUSED_EXIT_CODE)


@app.command(name="equity-download")
def equity_download(
    symbol: str = typer.Option(
        ...,
        "--symbol",
        help=f"Equity to download. One of: {', '.join(EQUITY_SYMBOLS)}.",
    ),
    start: str = typer.Option(
        ...,
        "--start",
        help="First US market calendar date to include, YYYY-MM-DD.",
    ),
    end: str = typer.Option(
        ...,
        "--end",
        help="Last US market calendar date to include, YYYY-MM-DD. Inclusive.",
    ),
    timeframe: str = typer.Option(
        EQUITY_TIMEFRAME,
        "--timeframe",
        help=f"Bar timeframe. Only {EQUITY_TIMEFRAME!r} is supported.",
    ),
) -> None:
    """Download historical equity bars from Alpaca and store them as Parquet.

    Stock market data requires credentials - unlike crypto, Alpaca does not
    serve it unauthenticated - so ALPACA_API_KEY and ALPACA_SECRET_KEY must
    both be set. The feed is IEX, which is what an Alpaca Basic account is
    entitled to.

    `--start` and `--end` are **US market calendar dates**, not UTC dates. The
    stored timestamps are UTC either way; the sidecar records both facts so a
    dataset can be reproduced without guessing which one was meant.

    Downloaded files stay local and are git-ignored. Extended-hours bars are
    included as the provider published them; the runtime is what decides which
    candles belong to a regular session.
    """
    try:
        result = download_equity_bars(
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            output_dir=DEFAULT_OUTPUT_DIR,
        )
    except EquityError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None

    typer.echo("Downloaded historical equity bars")
    typer.echo("")
    typer.echo(f"Symbol:    {result.symbol}")
    typer.echo(f"Timeframe: {result.timeframe}")
    typer.echo(f"Start:     {result.start.isoformat()} (US market date)")
    typer.echo(f"End:       {result.end.isoformat()} (US market date)")
    typer.echo(f"Rows:      {result.row_count}")
    typer.echo(f"Feed:      alpaca stock ({result.feed})")
    typer.echo(f"Saved:     {result.parquet_path}")
    typer.echo(f"Metadata:  {result.metadata_path}")


def _describe_equity_session(calendar: AlpacaMarketCalendar) -> str:
    """One line describing where `now` sits in the US market calendar.

    Read from the broker rather than computed from a weekday rule, so a
    holiday and an early close both show up as what they are.
    """
    try:
        open_now, session = is_market_open(calendar, now=datetime.now(UTC))
    except (SessionError, ExecutionError) as error:
        # A missing credential, an unreachable broker, or an unreadable
        # calendar all leave the session unknown. That is a status line, not a
        # reason to abandon a start that was only ever going to observe.
        return f"UNKNOWN ({error})"
    if session is None:
        return "CLOSED - no session on this US market date (weekend or holiday)"
    window = (
        f"{session.open_utc.isoformat()} to {session.close_utc.isoformat()} "
        f"({session.session_date.isoformat()})"
    )
    return f"{'OPEN' if open_now else 'CLOSED'} - regular session {window}"


def _echo_equity_runtime_banner(
    runtime: EquityRuntime,
    *,
    once: bool,
    lock: Path,
    calendar: AlpacaMarketCalendar,
) -> None:
    """Print what this process is and is not allowed to do, before it does it."""
    authorization = runtime.authorization
    heartbeat = runtime.heartbeat
    typer.echo("AUTO TRADER - EQUITY RUNTIME")
    typer.echo("")
    typer.echo(_field("Environment", "PAPER ONLY"))
    typer.echo(_field("Trading", "US EQUITY, REGULAR HOURS"))
    typer.echo(_field("Symbols", ", ".join(EQUITY_PROCESSING_ORDER)))
    typer.echo(_field("Mode", "ONCE" if once else "RUN"))
    typer.echo(_field("Bar Interval", "15m, completed regular-session bars only"))
    typer.echo(_field("Data Feed", f"alpaca stock ({EQUITY_FEED.value})"))
    typer.echo(_field("Market Session", _describe_equity_session(calendar)))
    typer.echo(_field("Reconciliation", heartbeat.reconciliation_status or "NOT RUN"))
    typer.echo(_field("Startup Safety", heartbeat.startup_safety_code))
    typer.echo(_field("Lock File", str(lock)))
    typer.echo("")
    if heartbeat.startup_safety_code != STARTUP_SAFETY_SAFE:
        typer.secho(RECONCILIATION_NOT_SAFE_BANNER, fg=typer.colors.RED)
        typer.echo(runtime.startup_safety_message)
        typer.echo("")
    if authorization.enabled:
        typer.secho("PAPER EXECUTION ENABLED", fg=typer.colors.YELLOW)
        typer.echo(
            "Signals on completed regular-session bars may be submitted to the PAPER "
            "account, and only while the session is open."
        )
    else:
        typer.secho("OBSERVATION ONLY - NO ORDER WILL BE SUBMITTED", fg=typer.colors.GREEN)
        typer.echo(f"Reason: {authorization.reason}")
    typer.echo("")


def _echo_equity_runtime_summary(runtime: EquityRuntime) -> None:
    """Print the final heartbeat as an operator-readable block."""
    heartbeat = runtime.heartbeat
    typer.echo("")
    typer.echo(_field("Final State", heartbeat.state.value))
    typer.echo(_field("Cycles Started", str(heartbeat.cycles_started)))
    typer.echo(_field("Cycles Completed", str(heartbeat.cycles_completed)))
    for symbol, timestamp in heartbeat.last_processed_bars.items():
        typer.echo(_field(f"Last {symbol} Bar", timestamp.isoformat() if timestamp else "none"))
    for symbol, timestamp in runtime.checkpoints.items():
        typer.echo(_field(f"Checkpoint {symbol}", timestamp.isoformat()))
    typer.echo(_field("Orders Submitted", str(heartbeat.orders_submitted)))
    typer.echo(_field("Provider Calls", str(heartbeat.api_calls_total)))
    if heartbeat.last_error is not None:
        typer.echo(_field("Last Error", heartbeat.last_error))


@app.command(name="equity-run")
def equity_run(
    once: bool = typer.Option(
        False,
        "--once",
        help="Process the current cycle once and exit, without waiting.",
    ),
    confirm_paper_runtime: str = typer.Option(
        "",
        "--confirm-paper-runtime",
        help=(
            f"Type {RUNTIME_CONFIRMATION_TOKEN} exactly to authorize THIS process to use "
            "the paper execution path for its lifetime. Without it the runtime only "
            "observes."
        ),
    ),
    observe_only: bool = typer.Option(
        False,
        "--observe-only",
        help=(
            "Run without an execution path at all: bars, validation, strategy and "
            "signals only. Submission is not refused, it is unavailable."
        ),
    ),
    safety_delay: float = typer.Option(
        DEFAULT_SAFETY_DELAY.total_seconds(),
        "--safety-delay",
        help=(
            "Seconds to wait after a 15-minute boundary before treating the bar that "
            "just closed as fetchable. Covers provider publication lag."
        ),
    ),
    database: Annotated[
        Path,
        typer.Option("--db", help="Local operational-state database."),
    ] = DEFAULT_DATABASE_PATH,
) -> None:
    """Run the equity runtime on completed 15-minute regular-session bars.

    Processes SPY, QQQ, IWM, AAPL, MSFT, NVDA, AMZN, GOOGL, META and TSLA in
    that fixed order: fetch one bounded window of recent completed bars for the
    whole universe in a single request, validate it per symbol, evaluate
    EMA 20 / EMA 50 on the newest completed regular-session bar only, record
    any signal, and - if and only if every gate is open - hand that signal to
    the equity paper execution path.

    \b
    The session rules, which are the whole point of a separate runtime:
      * session times come from the broker's calendar - holidays and early
        closes are read, never assumed
      * a cycle outside the regular session does nothing at all: no fetch, no
        strategy, no checkpoint, no order, no provider call
      * a submission is refused again at the boundary unless the broker's own
        clock says the session is open
      * the bar that closes *at* the bell is observed by no cycle, because
        acting on it would mean submitting after the close

    An in-progress candle is never processed, and a completed bar is never
    processed twice - not within one process, and not across a restart: the
    per-symbol bar checkpoint is committed to SQLite before the bar can reach
    the strategy. The preference is deliberate and one-sided: miss a trade
    rather than duplicate a trade.

    \b
    Every start reconciles first, in this order:
      1. acquire the equity runtime lock - a different file from the crypto
         runner's, so the two services never block each other, while a second
         equity runner is still refused
      2. open the database and apply any pending migration
      3. run a FULL-UNIVERSE reconciliation against the Alpaca PAPER account:
         all twelve tracked symbols, both books, plus every order intent. Not
         just the ten equities - this process sizes against total account
         exposure, which includes the crypto book, and only a full-universe
         pass may clear the shared account halt
      4. CLEAN or REPAIRED over the whole universe -> the shared account
         safety state becomes SAFE; anything else leaves it halted

    \b
    Unattended paper execution requires ALL of:
      1. AUTOTRADER_PAPER_TRADING_ENABLED=true in the environment
      2. --confirm-paper-runtime PAPER on the command line
      3. startup reconciliation reporting that trading is safe
      4. the regular market session being open at the moment of submission

    No environment variable and no flag combination bypasses the last two.
    There is no live mode. `--observe-only` goes further than the gates and
    constructs no execution path at all.

    Exits 0 on a clean stop including SIGINT/SIGTERM - a run that found the
    market shut and observed nothing is a clean stop - 1 on a controlled
    refusal or a fatal cycle failure, and 2 when trading was paused by an
    UNKNOWN submission outcome that must be reconciled.
    """
    _configure_runtime_logging(verbose=False)

    try:
        config = EquityRuntimeConfig(
            safety_delay=timedelta(seconds=safety_delay),
            lookback_bars=DEFAULT_LOOKBACK_BARS,
            observe_only=observe_only,
            runtime_confirmation=confirm_paper_runtime or None,
        )
    except ScheduleError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EQUITY_RUN_REFUSED_EXIT_CODE) from None

    try:
        initialize_database(database)
    except StateError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EQUITY_RUN_REFUSED_EXIT_CODE) from None

    lock = RuntimeLock(lock_path_for(database, scope=EQUITY_LOCK_SCOPE))
    try:
        lock.acquire()
    except RuntimeLockError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EQUITY_RUN_REFUSED_EXIT_CODE) from None

    shutdown = ShutdownRequest()
    shutdown.install()
    try:
        with connect(database) as connection:
            calendar = AlpacaMarketCalendar()
            runtime = EquityRuntime(
                connection,
                market_data=AlpacaEquityBars(calendar),
                calendar=calendar,
                execution=None if observe_only else PaperEquityExecutionGateway(),
                # The startup trading authority, over the **whole account**
                # rather than the ten positions this process manages. Two
                # things make that necessary rather than merely tidy: only a
                # full-universe pass can clear the shared account halt, so a
                # ten-symbol pass could never let this runtime start after a
                # crypto ambiguity; and this process sizes its orders against
                # total account exposure, which includes the crypto book it
                # does not trade. Order intents were always reconciled in full -
                # one account, one client_order_id space.
                startup_safety=startup_safety_from_reconciliation(connection),
                checkpoint=SqliteCheckpoint(connection),
                config=config,
                shutdown=shutdown,
            )
            runtime.start()
            _echo_equity_runtime_banner(runtime, once=once, lock=lock.path, calendar=calendar)
            try:
                if once:
                    runtime.run_cycle()
                else:
                    runtime.run_forever()
            finally:
                runtime.stop()
            _echo_equity_runtime_summary(runtime)
            state = runtime.heartbeat.state
    finally:
        shutdown.restore()
        lock.release()

    if state is RuntimeState.TRADING_PAUSED:
        typer.echo("")
        typer.secho("TRADING PAUSED - SUBMISSION OUTCOME UNKNOWN", fg=typer.colors.YELLOW, err=True)
        typer.echo(
            "An order may exist at the broker. Nothing further was submitted, and "
            "nothing here resolves it: reconcile against the broker before starting "
            "the runtime again.",
            err=True,
        )
        raise typer.Exit(code=EQUITY_RUN_PAUSED_EXIT_CODE)
    if state is RuntimeState.FAILED:
        typer.echo("")
        typer.secho("RUNTIME STOPPED ON A FATAL ERROR", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EQUITY_RUN_REFUSED_EXIT_CODE)


def _echo_equity_submit_preview(result: PaperExecutionResult, *, dry_run: bool) -> None:
    """Print the pre-submission preview for one equity order.

    The crypto preview's shape with the crypto-only lines removed: an equity
    asset carries no broker-published minimum, so the whole-share floor is this
    system's policy and is stated by the risk message when it bites. No
    credential, no authorization header, and no account number appears here.
    """
    decision = result.risk_decision
    typer.echo("AUTO TRADER - PAPER EQUITY ORDER")
    typer.echo("")
    typer.echo(_field("Environment", "PAPER ONLY"))
    typer.echo(_field("Trading", "US EQUITY, REGULAR SESSION ONLY"))
    typer.echo(_field("Symbol", result.symbol))
    typer.echo(_field("Side", result.side.value))
    typer.echo(_field("Requested Qty", format_quantity(result.requested_quantity)))
    typer.echo(_field("Reference Price", _money(result.reference_price)))
    typer.echo("")
    typer.echo(_field("Account Equity", _money(result.account.equity)))
    typer.echo(_field("Account Cash", _money(result.account.cash)))
    typer.echo(_field("UTC Day Baseline", _money(result.daily_baseline_equity)))
    daily_pnl = Decimal(str(result.account.equity)) - result.daily_baseline_equity
    typer.echo(_field("Daily P&L", _money(daily_pnl)))
    typer.echo("")
    typer.echo(_field("Risk Decision", "APPROVED" if decision.approved else "REJECTED"))
    typer.echo(_field("Risk Reason", decision.reason_code))
    typer.echo(_field("Approved Qty", format_quantity(decision.approved_quantity)))
    if result.intent is not None:
        typer.echo(_field("Broker Qty", format_quantity(result.intent.approved_quantity)))
        typer.echo(_field("Client Order ID", result.intent.client_order_id))
    if dry_run:
        typer.echo("")
        typer.echo("DRY RUN - no order was submitted and nothing was persisted.")


@app.command(name="equity-submit")
def equity_submit(
    symbol: str = typer.Option(
        ...,
        "--symbol",
        help=f"Equity ticker to trade. One of: {', '.join(EQUITY_SYMBOLS)}.",
    ),
    side: str = typer.Option(..., "--side", help="BUY or SELL. Long only; no shorts."),
    qty: str = typer.Option(
        ...,
        "--qty",
        help=(
            "Whole shares to request, as a decimal number (e.g. 1). The approved "
            "quantity is floored to whole shares; below one share is refused."
        ),
    ),
    confirm_paper: str = typer.Option(
        "",
        "--confirm-paper",
        help=(
            f"Type {paper_execution.CONFIRMATION_TOKEN} exactly to confirm a real paper "
            "submission. Not required for --dry-run."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Evaluate everything and print the preview, but never submit.",
    ),
    database: Annotated[
        Path,
        typer.Option("--db", help="Local operational-state database."),
    ] = DEFAULT_DATABASE_PATH,
) -> None:
    """Submit one MARKET order for one equity to the Alpaca **PAPER** account.

    `paper-submit`'s equity counterpart, running the same execution boundary
    the equity runtime uses (`execute_equity_paper_order`): the same risk
    engine, the same durable intent before any broker call, the same
    at-most-once submission, and one gate the crypto path does not have - the
    broker's own clock must say the **regular session is open**, or nothing is
    submitted and nothing is queued for a later session.

    There is no live mode: the trading client is constructed with `paper=True`
    hardcoded, and no flag, option, or environment variable selects anything
    else.

    A real submission needs **both** gates, independent and closed by default:

    \b
      1. AUTOTRADER_PAPER_TRADING_ENABLED=true in the environment
      2. --confirm-paper PAPER on the command line

    `--dry-run` needs neither and works while the market is closed: it reads
    the account, positions, the asset's broker metadata, and the current IEX
    price, runs the risk engine, prints the preview, and stops without
    persisting an intent or submitting. Running it first is the intended way
    to check an order.

    Exits 0 when the order was submitted, already existed, or a dry run
    completed; 1 on a controlled refusal - including a closed session and a
    risk rejection; and 2 when the outcome is UNKNOWN, meaning an order may
    exist at the broker and must be reconciled before anything else is sent.
    """
    if not dry_run:
        try:
            paper_execution.require_paper_trading_enabled()
            paper_execution.require_confirmation(confirm_paper)
        except ExecutionError as error:
            typer.secho(str(error), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=EQUITY_SUBMIT_REFUSED_EXIT_CODE) from None

    try:
        quantity = parse_quantity(qty, "--qty")
    except ExecutionError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EQUITY_SUBMIT_REFUSED_EXIT_CODE) from None

    try:
        initialize_database(database)
    except StateError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EQUITY_SUBMIT_REFUSED_EXIT_CODE) from None

    with connect(database) as connection:
        try:
            result = execute_equity_paper_order(
                connection,
                symbol=symbol,
                side=side,
                requested_quantity=quantity,
                dry_run=dry_run,
                now=datetime.now(UTC),
            )
        except AmbiguousSubmissionError as error:
            # Its own exit code: an order may exist at the broker, which is a
            # different situation from "nothing happened".
            typer.secho(str(error), fg=typer.colors.YELLOW, err=True)
            raise typer.Exit(code=EQUITY_SUBMIT_UNKNOWN_EXIT_CODE) from None
        except (ExecutionError, StateError, EquityError) as error:
            typer.secho(str(error), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=EQUITY_SUBMIT_REFUSED_EXIT_CODE) from None

    _echo_equity_submit_preview(result, dry_run=dry_run)

    if result.outcome is ExecutionOutcome.REJECTED_BY_RISK:
        typer.echo("")
        typer.secho("REJECTED BY RISK ENGINE", fg=typer.colors.RED)
        typer.echo(result.message)
        typer.echo("No order was created and no broker request was made.")
        raise typer.Exit(code=EQUITY_SUBMIT_REFUSED_EXIT_CODE)

    if result.outcome is ExecutionOutcome.DRY_RUN:
        return

    snapshot = result.broker_order
    typer.echo("")
    if result.outcome is ExecutionOutcome.DUPLICATE:
        typer.secho("ALREADY SUBMITTED", fg=typer.colors.YELLOW)
        typer.echo("The broker already had an order under this client order ID.")
    else:
        typer.secho("SUBMITTED TO PAPER ACCOUNT", fg=typer.colors.GREEN)
    if snapshot is not None:
        typer.echo("")
        typer.echo(_field("Broker Order ID", snapshot.broker_order_id))
        typer.echo(_field("Client Order ID", snapshot.client_order_id))
        typer.echo(_field("Submitted Qty", format_quantity(snapshot.quantity)))
        typer.echo(_field("Broker Status", snapshot.status))
        typer.echo(_field("Filled Qty", format_quantity(snapshot.filled_quantity)))
    typer.echo("")
    typer.echo("Accepted is not filled. Local positions are not updated from an")
    typer.echo("accepted order. Run `autotrader reconcile` to settle it against the broker.")


def _echo_equity_shadow_banner(
    runtime: EquityShadowRuntime, *, once: bool, lock: Path, database: Path
) -> None:
    spec = runtime.regime_spec
    typer.echo("AUTO TRADER - EQUITY V3 + EDA-1 SIDE-BY-SIDE LIVE SHADOW")
    typer.echo("")
    typer.echo(_field("Environment", "OBSERVATION ONLY"))
    typer.echo(_field("Engine", runtime.engine_version))
    typer.echo(
        _field(
            "Derived Engine",
            (
                f"{runtime.derived_engine_version} (EDA1_RGP overlay: participate iff "
                f"SPY close > SMA{spec.sma_sessions} and drawdown > "
                f"{spec.calm_threshold:.0%}, lag {spec.lag_sessions} session)"
            ),
        )
    )
    typer.echo(_field("Universe", ", ".join(EQUITY_PROCESSING_ORDER)))
    typer.echo(_field("Session", "US regular hours, broker calendar"))
    typer.echo(_field("Lookback Bars", str(runtime.lookback_bars)))
    typer.echo(_field("Mode", "single cycle" if once else "daemon"))
    typer.echo(_field("Database", str(database)))
    typer.echo(_field("Lock", str(lock)))
    typer.echo("")
    typer.secho(
        "ZERO ORDER MUTATION: this process holds no execution path. Decisions are",
        bold=True,
    )
    typer.secho(
        "recorded and dropped; no order can be submitted, cancelled, or replaced.",
        bold=True,
    )
    typer.echo("")


@app.command(name="equity-shadow")
def equity_shadow(
    once: bool = typer.Option(
        False,
        "--once",
        help="Process the current cycle once and exit, without waiting.",
    ),
    safety_delay: float = typer.Option(
        DEFAULT_SAFETY_DELAY.total_seconds(),
        "--safety-delay",
        help=(
            "Seconds to wait after a 15-minute boundary before treating the bar that "
            "just closed as fetchable. Covers provider publication lag."
        ),
    ),
    lookback_bars: int = typer.Option(
        DEFAULT_SHADOW_LOOKBACK_BARS,
        "--lookback-bars",
        help=(
            f"Completed base bars fetched per cycle, between {MIN_SHADOW_LOOKBACK_BARS} "
            f"(V3's declared requirement) and {MAX_SHADOW_LOOKBACK_BARS}. The default "
            "is the historical study's pre-declared uniform lookback."
        ),
    ),
    state_sessions: int = typer.Option(
        DEFAULT_STATE_SESSIONS,
        "--state-sessions",
        help=(
            f"Completed sessions behind the EDA-1 regime state, between "
            f"{MIN_STATE_SESSIONS} and {MAX_STATE_SESSIONS}. The default approximates "
            "the research frame; the provider returns what its history holds."
        ),
    ),
    database: Annotated[
        Path,
        typer.Option(
            "--db",
            help=(
                "The shadow's own state database. Never the trading database: a "
                "database that has ever held an order intent is refused."
            ),
        ),
    ] = EQUITY_SHADOW_DATABASE_PATH,
) -> None:
    """Run the Equity V3 + EDA-1 LIVE SHADOW: real decisions recorded, zero orders.

    Watches the ten Equity V0.2 symbols on completed regular-session 15-minute
    bars - the broker's calendar and clock are the authority, exactly as in the
    trading runtime - runs the V3 decision engine on each newest completed bar,
    derives the EDA-1 research champion's decision through its deterministic
    participation overlay (SPY completed-session closes, one session of lag),
    and records both durably in `shadow_decisions`, side by side, with the
    per-session regime state and a per-bar comparison row.

    \b
    What makes this a shadow rather than a runtime with its gates shut:
      * there is no execution path to disable - the shadow runtime's
        constructor has no execution parameter and holds no gateway
      * no environment gate and no confirmation token exist here, because
        there is nothing they could authorize
      * the shadow keeps its own database and refuses one that has ever held
        an order intent, so it can never share (or steal) the trading
        runtime's per-symbol bar claims
      * after every cycle the process re-verifies that its database holds
        zero order intents, and stops if that is ever untrue

    Reads the market-data and calendar endpoints only. A completed bar is
    claimed durably before V3 sees it and is never evaluated twice, across
    restarts included: miss an observation rather than duplicate one.

    Exits 0 on a clean stop - a shadow that found the market shut and observed
    nothing did its job - and 1 on a controlled refusal or a fatal failure.
    """
    _configure_runtime_logging(verbose=False)

    code_sha: str | None = None
    try:
        from autotrader.smoke.gitinfo import git_state

        code_sha = git_state(Path(".")).sha
    except Exception:  # noqa: BLE001 - provenance is recorded, never required
        code_sha = None

    try:
        config = EquityShadowConfig(
            safety_delay=timedelta(seconds=safety_delay),
            lookback_bars=lookback_bars,
            state_sessions=state_sessions,
            code_sha=code_sha,
        )
    except (ScheduleError, EquityError) as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EQUITY_SHADOW_REFUSED_EXIT_CODE) from None

    try:
        initialize_database(database)
    except StateError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EQUITY_SHADOW_REFUSED_EXIT_CODE) from None

    lock = RuntimeLock(lock_path_for(database, scope=EQUITY_SHADOW_LOCK_SCOPE))
    try:
        lock.acquire()
    except RuntimeLockError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EQUITY_SHADOW_REFUSED_EXIT_CODE) from None

    shutdown = ShutdownRequest()
    shutdown.install()
    try:
        with connect(database) as connection:
            calendar = AlpacaMarketCalendar()
            runtime = EquityShadowRuntime(
                connection,
                market_data=ShadowEquityBars(calendar),
                regime_data=RegimeEquityBars(calendar),
                calendar=calendar,
                config=config,
                shutdown=shutdown,
            )
            try:
                runtime.start()
            except ShadowIntegrityError as error:
                typer.secho(str(error), fg=typer.colors.RED, err=True)
                raise typer.Exit(code=EQUITY_SHADOW_REFUSED_EXIT_CODE) from None
            _echo_equity_shadow_banner(runtime, once=once, lock=lock.path, database=database)
            try:
                if once:
                    runtime.run_cycle()
                else:
                    runtime.run_forever()
            finally:
                runtime.stop()
            state_value = runtime.state
    finally:
        shutdown.restore()
        lock.release()

    if state_value is RuntimeState.FAILED:
        typer.echo("")
        typer.secho("SHADOW STOPPED ON A FATAL ERROR", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EQUITY_SHADOW_REFUSED_EXIT_CODE)


#: The research command group. Registered here rather than defined here: study
#: tooling lives in `autotrader.research` so it can grow without this module
#: growing with it, and so nothing under `research` is importable as part of
#: the trading path. Every command in it is offline and read-only.
app.add_typer(research_app, name="research")


def main() -> None:
    """Run the CLI application."""
    app()


__all__ = ["app", "main"]
