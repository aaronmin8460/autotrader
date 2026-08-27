"""Command-line entry point.

Phase 1 exposes application metadata and a historical market-data download,
Phase 2 read-only validation of an already-downloaded dataset, Phase 4 a local
backtest of the EMA crossover strategy over a stored dataset, and Phase 7 a
single, deliberately awkward **paper** order submission.

`paper-submit` is the only command that can reach a broker, and it can only
ever reach Alpaca **paper**. It requires an environment gate and an explicit
confirmation token, both closed by default, and there is no `--live` option,
no `--paper` option, and no way to ask for anything but paper (docs/SPEC.md
section 8, Phase 7).
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from autotrader import __version__
from autotrader.backtest import (
    DEFAULT_INITIAL_CASH,
    STRATEGY_NAME,
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
    ValidationInputError,
    ValidationResult,
    read_bars,
    validate_parquet_file,
)
from autotrader.execution import paper as paper_execution
from autotrader.execution.models import ExecutionError
from autotrader.execution.paper import (
    AmbiguousSubmissionError,
    ExecutionOutcome,
    PaperExecutionResult,
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

#: Width of the label column in the backtest report.
_LABEL_WIDTH = 23

app = typer.Typer(
    name="autotrader",
    add_completion=False,
    no_args_is_help=True,
)


@app.callback()
def cli() -> None:
    """Personal automated trading system.

    Historical market data, dataset validation, EMA crossover signals, local
    backtesting, a deterministic risk engine, local SQLite state, and Alpaca
    **paper** order submission.

    There is no live trading. `paper-submit` talks to Alpaca's paper
    environment only, behind an environment gate and an explicit confirmation
    token; no command, flag, or environment variable can direct an order at a
    real-money account.
    """


@app.command()
def version() -> None:
    """Show the installed autotrader version."""
    typer.echo(f"autotrader {__version__}")


@app.command()
def download(
    symbol: str = typer.Option(
        ...,
        "--symbol",
        help=f"Ticker to download. One of: {', '.join(SUPPORTED_SYMBOLS)}.",
    ),
    start: str = typer.Option(
        ...,
        "--start",
        help="First market date to include, YYYY-MM-DD (America/New_York).",
    ),
    end: str = typer.Option(
        ...,
        "--end",
        help="Last market date to include, YYYY-MM-DD (America/New_York). Inclusive.",
    ),
    timeframe: str = typer.Option(
        SUPPORTED_TIMEFRAME,
        "--timeframe",
        help=f"Bar timeframe. Only {SUPPORTED_TIMEFRAME!r} is supported.",
    ),
) -> None:
    """Download historical bars from Alpaca and store them as Parquet.

    Credentials are read from the ALPACA_API_KEY and ALPACA_SECRET_KEY
    environment variables. Downloaded files stay local and are git-ignored.
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
    typer.echo(f"Feed:      {result.feed.upper()}")
    typer.echo(f"Saved:     {result.parquet_path}")
    typer.echo(f"Metadata:  {result.metadata_path}")


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
) -> None:
    """Validate a stored Parquet bar dataset against the canonical schema.

    Reads the file only. Nothing is downloaded, modified, or repaired. Exits 0
    when the dataset is valid, 1 when it has validation errors, and 2 when the
    file cannot be read at all.
    """
    try:
        result = validate_parquet_file(path)
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


def _money(amount: float) -> str:
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
    typer.echo("")
    typer.echo(_field("Total Return", _percent(result.total_return)))
    typer.echo(_field("Max Drawdown", _percent(result.max_drawdown)))
    typer.echo("")
    typer.echo(_field("Signals", str(result.signal_count)))
    typer.echo(_field("BUY Executions", str(result.buy_execution_count)))
    typer.echo(_field("SELL Executions", str(result.sell_execution_count)))
    typer.echo(_field("Completed Round Trips", str(result.completed_round_trips)))
    typer.echo(_field("Ending Position", f"{result.ending_position_quantity} shares"))
    if result.unexecuted_last_bar_signal_count:
        # No bar follows the last one, so its signal could not be filled.
        typer.echo(_field("Unexecuted Last Bar", str(result.unexecuted_last_bar_signal_count)))
    typer.echo("")
    typer.echo("Execution model:")
    typer.echo("Next-bar open")
    typer.echo("")
    typer.echo("Fees / Slippage:")
    typer.echo("0 / 0")
    typer.echo("")
    typer.echo("Engineering validation only. Not a profitability claim, and not")
    typer.echo("advice: no order was created and no broker was contacted.")


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
) -> None:
    """Backtest the EMA 20 / EMA 50 crossover over a stored Parquet dataset.

    The dataset is validated first and the backtest is abandoned if it fails.
    Signals fill at the next bar's open - never on the signal's own bar - with
    zero commission, fees, and slippage. Long only, whole shares, no leverage.
    This is a local simulation: nothing is downloaded, written, or ordered.

    Exits 0 on a completed simulation, 1 when the dataset or the starting cash
    is unusable, and 2 when the file cannot be read at all.
    """
    try:
        bars = read_bars(path)
    except ValidationInputError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=UNREADABLE_INPUT_EXIT_CODE) from None

    try:
        result = run_backtest(bars, initial_cash=initial_cash)
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
    typer.echo("AUTO TRADER - PAPER ORDER")
    typer.echo("")
    typer.echo(_field("Environment", "PAPER ONLY"))
    typer.echo(_field("Market", "OPEN" if result.clock.is_open else "CLOSED"))
    typer.echo(_field("Symbol", result.symbol))
    typer.echo(_field("Side", result.side.value))
    typer.echo(_field("Requested Qty", str(result.requested_quantity)))
    typer.echo(_field("Reference Price", _money(result.reference_price)))
    typer.echo("")
    typer.echo(_field("Account Equity", _money(result.account.equity)))
    typer.echo(_field("Account Cash", _money(result.account.cash)))
    typer.echo(_field("Start-of-Day Equity", _money(result.account.start_of_day_equity)))
    typer.echo(_field("Daily P&L", _money(result.account.daily_pnl)))
    typer.echo("")
    typer.echo(_field("Risk Decision", "APPROVED" if decision.approved else "REJECTED"))
    typer.echo(_field("Risk Reason", decision.reason_code))
    typer.echo(_field("Approved Qty", str(decision.approved_quantity)))
    if result.intent is not None:
        typer.echo(_field("Client Order ID", result.intent.client_order_id))
    if dry_run:
        typer.echo("")
        typer.echo("DRY RUN - no order was submitted and nothing was persisted.")


@app.command(name="paper-submit")
def paper_submit(
    symbol: str = typer.Option(
        ...,
        "--symbol",
        help=f"Ticker to trade. One of: {', '.join(SUPPORTED_SYMBOLS)}.",
    ),
    side: str = typer.Option(..., "--side", help="BUY or SELL. Long only; no shorts."),
    qty: int = typer.Option(..., "--qty", help="Whole shares to request. Must be > 0."),
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
    """Submit one market order to the Alpaca **PAPER** account.

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
    positions, the clock, and the current price, runs the risk engine, prints
    the preview, and stops without persisting an intent or calling the broker.
    Running it first is the intended way to check an order.

    The quantity sent to the broker is always the risk engine's approved
    quantity, which may be smaller than `--qty`. If risk approves nothing, no
    broker request is created at all.

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
                requested_quantity=qty,
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
        typer.echo(_field("Submitted Qty", str(snapshot.quantity)))
        typer.echo(_field("Broker Status", snapshot.status))
        typer.echo(_field("Filled Qty", str(snapshot.filled_quantity)))
    typer.echo("")
    typer.echo("Accepted is not filled. Local positions are not updated from an")
    typer.echo("accepted order; reconciliation against the broker is a later phase.")


def main() -> None:
    """Run the CLI application."""
    app()


__all__ = ["app", "main"]
