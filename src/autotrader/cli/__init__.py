"""Command-line entry point.

Phase 0 exposes only application metadata and help. Trading, data, and
backtest commands belong to later phases (see docs/SPEC.md).
"""

import typer

from autotrader import __version__

app = typer.Typer(
    name="autotrader",
    add_completion=False,
    no_args_is_help=True,
)


@app.callback()
def cli() -> None:
    """Personal automated trading system.

    Phase 0: repository foundation only - no market data, no strategies,
    no broker connectivity, no live trading.
    """


@app.command()
def version() -> None:
    """Show the installed autotrader version."""
    typer.echo(f"autotrader {__version__}")


def main() -> None:
    """Run the CLI application."""
    app()


__all__ = ["app", "main"]
