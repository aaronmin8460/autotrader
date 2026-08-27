"""Command-line entry point.

Phase 1 exposes application metadata and a historical market-data download.
There is no trading command and no broker order path (see docs/SPEC.md).
"""

from pathlib import Path

import typer

from autotrader import __version__
from autotrader.data.historical import (
    SUPPORTED_SYMBOLS,
    SUPPORTED_TIMEFRAME,
    HistoricalDataError,
    download_bars,
)

DEFAULT_OUTPUT_DIR = Path("data/raw")

app = typer.Typer(
    name="autotrader",
    add_completion=False,
    no_args_is_help=True,
)


@app.callback()
def cli() -> None:
    """Personal automated trading system.

    Phase 1: historical market data only - no strategies, no backtests,
    no broker connectivity, no live trading.
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


def main() -> None:
    """Run the CLI application."""
    app()


__all__ = ["app", "main"]
