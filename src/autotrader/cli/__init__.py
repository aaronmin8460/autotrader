"""Command-line entry point.

Phase 1 exposes application metadata and a historical market-data download,
Phase 2 read-only validation of an already-downloaded dataset, and Phase 4 a
local backtest of the EMA crossover strategy over a stored dataset. There is
no trading command and no broker order path (see docs/SPEC.md).
"""

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

DEFAULT_OUTPUT_DIR = Path("data/raw")

#: `validate` exit codes. 0 is a valid dataset.
INVALID_DATASET_EXIT_CODE = 1
UNREADABLE_INPUT_EXIT_CODE = 2

#: `backtest` exit codes. 0 is a completed simulation; 2 is a shared
#: unreadable-input failure.
BACKTEST_INPUT_EXIT_CODE = 1

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

    Phase 4: historical market data, dataset validation, EMA crossover
    signals, and local backtesting only - no broker connectivity, no order
    submission, and no live trading.
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


def main() -> None:
    """Run the CLI application."""
    app()


__all__ = ["app", "main"]
