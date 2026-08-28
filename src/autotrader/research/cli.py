"""The `autotrader research` command group. Read-only, offline, local.

Every command here reads a stored Parquet dataset and writes, at most, files
under the external reports root. None of them downloads anything, contacts a
broker, or touches the operational SQLite state - a research command cannot
change what the trading system does.

The group lives in this package rather than in `autotrader.cli` so that
research surface area accumulates here instead of in the production command
module, and so the trading CLI stays readable as the study tooling grows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from autotrader.data.validation import (
    CRYPTO_UNIVERSE_LABEL,
    EQUITY_UNIVERSE_LABEL,
    SUPPORTED_SYMBOLS,
    ValidationInputError,
    read_bars,
)
from autotrader.equity import EQUITY_SYMBOLS
from autotrader.research.costs import CostInputError, cost_model_for
from autotrader.research.engines import BuyAndHoldEngine, EmaCrossEngine, ParametricEmaCross
from autotrader.research.experiments import (
    ParameterSpace,
    StudyConfig,
    SweepError,
    evaluate_holdout,
    run_sweep,
    select_best,
    write_selection,
)
from autotrader.research.leakage import audit_study
from autotrader.research.metrics import MetricsInputError, bar_clock_for, metrics_for_replay
from autotrader.research.replay import ReplayConfig, ReplayInputError, replay
from autotrader.research.splits import SplitError, SplitScheme, holdout_split, walk_forward_splits
from autotrader.research.storage import ResearchStorageError

#: Exit codes. Distinct so a supervising script can tell "the data was bad"
#: from "the study was configured wrongly" from "leakage was detected".
INPUT_EXIT_CODE = 1
UNREADABLE_INPUT_EXIT_CODE = 2
LEAKAGE_EXIT_CODE = 3
STORAGE_EXIT_CODE = 4

_LABEL_WIDTH = 26

app = typer.Typer(
    name="research",
    add_completion=False,
    no_args_is_help=True,
    help="Backtest research: replay, walk-forward, leakage audit, parameter sweeps.",
)


def _field(label: str, value: str) -> str:
    return f"{label + ':':<{_LABEL_WIDTH}}{value}"


def _money(amount: object) -> str:
    return f"${amount:,.2f}"


def _percent(fraction: float | None) -> str:
    """A decimal fraction as a percentage, or `n/a` when it is undefined.

    An undefined metric is printed as `n/a` rather than `0.00%`, for the same
    reason it is `None` in the data: a reader must not mistake "could not be
    computed" for "was computed and came out at zero".
    """
    return "n/a" if fraction is None else f"{fraction * 100:.2f}%"


def _number(value: float | None, places: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


def _universe(equity: bool) -> tuple[tuple[str, ...], str]:
    if equity:
        return EQUITY_SYMBOLS, EQUITY_UNIVERSE_LABEL
    return SUPPORTED_SYMBOLS, CRYPTO_UNIVERSE_LABEL


def _load(path: Path) -> object:
    try:
        return read_bars(path)
    except ValidationInputError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=UNREADABLE_INPUT_EXIT_CODE) from None


def _engine(name: str, fast: int, slow: int) -> object:
    if name == "ema-cross":
        return EmaCrossEngine()
    if name == "parametric-ema-cross":
        return ParametricEmaCross(fast_period=fast, slow_period=slow)
    if name == "buy-and-hold":
        return BuyAndHoldEngine(warmup=slow)
    typer.secho(
        f"Unknown engine {name!r}. Known engines: buy-and-hold, ema-cross, parametric-ema-cross.",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(code=INPUT_EXIT_CODE)


def _echo_metrics(metrics: object, *, heading: str) -> None:
    """Print one metrics block. Risk figures before return figures, on purpose.

    Total return is the number a reader's eye goes to and the number that means
    least on its own, so it is not the first thing on the page.
    """
    typer.echo(heading)
    typer.echo("")
    typer.echo(_field("Bars", str(metrics.bar_count)))
    typer.echo(_field("Bar clock", f"{metrics.bar_clock} ({metrics.bars_per_year}/yr)"))
    typer.echo(_field("Exposure", _percent(metrics.exposure)))
    typer.echo(_field("Turnover", f"{metrics.turnover:.2f}x"))
    typer.echo("")
    typer.echo(_field("Max drawdown", _percent(metrics.max_drawdown)))
    typer.echo(_field("Drawdown length", f"{metrics.max_drawdown_bars} bars"))
    typer.echo(_field("Volatility (ann.)", _percent(metrics.volatility_annualized)))
    typer.echo(_field("Sharpe (ann.)", _number(metrics.sharpe_ratio)))
    typer.echo(_field("Sortino (ann.)", _number(metrics.sortino_ratio)))
    typer.echo("")
    typer.echo(_field("Total return", _percent(metrics.total_return)))
    typer.echo(_field("Annualized return", _percent(metrics.annualized_return)))
    # Printed immediately beneath it: annualizing a two-week sample is
    # arithmetically valid and epistemically worthless, and the only way a
    # reader can tell which they are looking at is to see the sample length.
    typer.echo(_field("Sample length", f"{metrics.sample_years:.3f} years"))
    typer.echo(_field("Realized PnL", _money(metrics.realized_pnl)))
    typer.echo(_field("Unrealized PnL", _money(metrics.unrealized_pnl)))
    typer.echo("")
    typer.echo(_field("Trades", str(metrics.trade_count)))
    typer.echo(_field("Win rate", _percent(metrics.win_rate)))
    typer.echo(_field("Profit factor", _number(metrics.profit_factor)))
    typer.echo(
        _field(
            "Average trade",
            "n/a" if metrics.average_trade_pnl is None else _money(metrics.average_trade_pnl),
        )
    )
    typer.echo(
        _field(
            "Average hold",
            "n/a" if metrics.average_bars_held is None else f"{metrics.average_bars_held:.1f} bars",
        )
    )
    typer.echo("")
    typer.echo(_field("Fees", _money(metrics.total_fees)))
    typer.echo(_field("Slippage", _money(metrics.total_slippage_cost)))
    typer.echo(_field("Cost drag", _percent(metrics.cost_drag)))


def _disclaimer() -> None:
    typer.echo("")
    typer.echo("Research only. No order was created and no broker was contacted.")
    typer.echo("Costs are stated assumptions, not a provider fee schedule, and")
    typer.echo("no result here is a profitability claim or investment advice.")


@app.command()
def replay_dataset(
    path: Annotated[Path, typer.Argument(help="Parquet bar dataset to replay.")],
    engine: Annotated[str, typer.Option("--engine", help="Engine to evaluate.")] = "ema-cross",
    fast: Annotated[int, typer.Option("--fast", help="Fast EMA period.")] = 20,
    slow: Annotated[int, typer.Option("--slow", help="Slow EMA period.")] = 50,
    initial_cash: Annotated[float, typer.Option("--initial-cash")] = 100000.0,
    cost: Annotated[str, typer.Option("--cost", help="Named cost model.")] = "crypto-taker",
    clock: Annotated[str, typer.Option("--clock", help="Named bar clock.")] = "crypto-15m",
    equity: Annotated[bool, typer.Option("--equity", help="Accept an equity dataset.")] = False,
) -> None:
    """Replay one engine over a stored dataset and report full metrics.

    Fills happen at the bar **after** the signal, never on the signal's own bar.
    Nothing is downloaded, ordered, or written.
    """
    bars = _load(path)
    universe, label = _universe(equity)
    try:
        model = cost_model_for(cost)
        bar_clock = bar_clock_for(clock)
        result = replay(
            bars,
            _engine(engine, fast, slow),
            ReplayConfig(
                initial_cash=Decimal(str(initial_cash)),
                cost_model=model,
                supported_symbols=universe,
                universe_label=label,
            ),
        )
        metrics = metrics_for_replay(result, bar_clock)
    except (CostInputError, MetricsInputError, ReplayInputError) as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=INPUT_EXIT_CODE) from None

    _echo_metrics(metrics, heading=f"RESEARCH REPLAY  {result.symbol}  {engine}")
    typer.echo("")
    typer.echo(_field("Signals", str(result.signal_count)))
    typer.echo(_field("Signals not acted on", str(result.skipped_signal_count)))
    if result.unexecuted_final_signal_count:
        typer.echo(_field("Unexecuted final bar", str(result.unexecuted_final_signal_count)))
    typer.echo(_field("Cost model", model.label))
    _disclaimer()


@app.command()
def audit(
    path: Annotated[Path, typer.Argument(help="Parquet bar dataset to audit.")],
    engine: Annotated[str, typer.Option("--engine")] = "ema-cross",
    fast: Annotated[int, typer.Option("--fast")] = 20,
    slow: Annotated[int, typer.Option("--slow")] = 50,
    train_bars: Annotated[int, typer.Option("--train-bars")] = 500,
    test_bars: Annotated[int, typer.Option("--test-bars")] = 100,
    embargo_bars: Annotated[int, typer.Option("--embargo-bars")] = 50,
    holdout_bars: Annotated[int, typer.Option("--holdout-bars")] = 200,
    anchored: Annotated[bool, typer.Option("--anchored")] = False,
    probes: Annotated[int, typer.Option("--probes", help="Perturbation probe count.")] = 5,
    equity: Annotated[bool, typer.Option("--equity")] = False,
) -> None:
    """Audit a study configuration for look-ahead and contamination.

    Runs the structural checks over the split layout and the perturbation
    checks over the engine: future bars are changed and the engine re-asked, and
    any signal at or before the change is a finding.

    Exits 0 when clean, 3 when leakage is found, 1 on a bad configuration.
    """
    bars = _load(path)
    del equity  # the audit reads timestamps and prices; the universe is irrelevant
    chosen = _engine(engine, fast, slow)
    try:
        timestamps = list(bars["timestamp"])
        holdout = holdout_split(timestamps, holdout_bars=holdout_bars, embargo_bars=embargo_bars)
        splits = walk_forward_splits(
            timestamps[: holdout.study_end],
            train_bars=train_bars,
            test_bars=test_bars,
            scheme=SplitScheme.ANCHORED if anchored else SplitScheme.ROLLING,
            embargo_bars=embargo_bars,
        )
    except SplitError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=INPUT_EXIT_CODE) from None

    report = audit_study(
        engine=chosen,
        bars=bars,
        splits=splits,
        holdout=holdout,
        required_embargo=embargo_bars,
        probes=probes,
    )

    typer.echo("RESEARCH LEAKAGE AUDIT")
    typer.echo("")
    typer.echo(_field("Engine", f"{chosen.name} {chosen.version}"))
    typer.echo(_field("Windows", str(len(splits))))
    typer.echo(_field("Embargo", f"{embargo_bars} bars"))
    typer.echo(_field("Holdout", f"{holdout.holdout_length} bars, withheld"))
    typer.echo(_field("Checks run", ", ".join(report.checks)))
    typer.echo(_field("Perturbation probes", str(report.probes)))
    typer.echo("")

    if report.clean:
        typer.secho("No leakage detected.", fg=typer.colors.GREEN)
        typer.echo("")
        typer.echo("A clean audit samples probe points; it is strong evidence,")
        typer.echo("not a proof that no leak exists anywhere in the series.")
        return

    typer.secho(f"{len(report.findings)} finding(s):", fg=typer.colors.RED)
    for finding in report.findings:
        typer.echo(f"- {finding}")
    raise typer.Exit(code=LEAKAGE_EXIT_CODE)


@app.command()
def sweep(
    path: Annotated[Path, typer.Argument(help="Parquet bar dataset to study.")],
    study: Annotated[str, typer.Option("--study", help="Study name; a directory slug.")],
    fast_periods: Annotated[str, typer.Option("--fast-periods")] = "10,20,30",
    slow_periods: Annotated[str, typer.Option("--slow-periods")] = "50,80,120",
    train_bars: Annotated[int, typer.Option("--train-bars")] = 500,
    test_bars: Annotated[int, typer.Option("--test-bars")] = 100,
    embargo_bars: Annotated[int, typer.Option("--embargo-bars")] = 50,
    holdout_bars: Annotated[int, typer.Option("--holdout-bars")] = 200,
    initial_cash: Annotated[float, typer.Option("--initial-cash")] = 100000.0,
    cost: Annotated[str, typer.Option("--cost")] = "crypto-taker",
    clock: Annotated[str, typer.Option("--clock")] = "crypto-15m",
    objective: Annotated[str, typer.Option("--objective")] = "consistency",
    anchored: Annotated[bool, typer.Option("--anchored")] = False,
    equity: Annotated[bool, typer.Option("--equity")] = False,
    evaluate_final_holdout: Annotated[
        bool,
        typer.Option(
            "--evaluate-holdout",
            help="Score the selected candidate on the withheld holdout. Once, at the end.",
        ),
    ] = False,
) -> None:
    """Sweep EMA periods under walk-forward validation and record every result.

    The grid is bounded; an oversized one is refused rather than truncated. The
    final holdout is withheld from selection and is scored only when
    `--evaluate-holdout` is passed, and only for the one candidate the objective
    already chose.

    Results are written under `AUTOTRADER_QA_REPORTS`, never into the repository.
    """
    bars = _load(path)
    universe, label = _universe(equity)

    try:
        fast_values = tuple(int(value) for value in fast_periods.split(",") if value.strip())
        slow_values = tuple(int(value) for value in slow_periods.split(",") if value.strip())
    except ValueError:
        typer.secho("Periods must be comma-separated integers.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=INPUT_EXIT_CODE) from None

    space = ParameterSpace(
        name=f"{study}-ema-periods",
        values={"fast_period": fast_values, "slow_period": slow_values},
        # A fast period at or above the slow one cannot cross; excluded so the
        # sweep does not spend experiments on configurations that emit nothing.
        constraint=lambda entry: int(entry["fast_period"]) < int(entry["slow_period"]),
    )

    try:
        timestamps = list(bars["timestamp"])
        holdout_definition = holdout_split(
            timestamps, holdout_bars=holdout_bars, embargo_bars=embargo_bars
        )
        splits = walk_forward_splits(
            timestamps[: holdout_definition.study_end],
            train_bars=train_bars,
            test_bars=test_bars,
            scheme=SplitScheme.ANCHORED if anchored else SplitScheme.ROLLING,
            embargo_bars=embargo_bars,
        )
        config = StudyConfig(
            study=study,
            bar_clock=bar_clock_for(clock),
            replay=ReplayConfig(
                initial_cash=Decimal(str(initial_cash)),
                cost_model=cost_model_for(cost),
                supported_symbols=universe,
                universe_label=label,
            ),
        )
        result = run_sweep(
            bars,
            lambda parameters: ParametricEmaCross(
                fast_period=int(parameters["fast_period"]),
                slow_period=int(parameters["slow_period"]),
            ),
            space,
            splits,
            config,
            created_at=datetime.now(UTC),
            holdout=holdout_definition,
        )
    except ResearchStorageError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=STORAGE_EXIT_CODE) from None
    except (CostInputError, MetricsInputError, SplitError, SweepError) as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=INPUT_EXIT_CODE) from None

    typer.echo("RESEARCH PARAMETER SWEEP")
    typer.echo("")
    typer.echo(_field("Study", result.study))
    typer.echo(_field("Run", result.run_id))
    typer.echo(_field("Experiments", str(result.experiment_count)))
    typer.echo(_field("Walk-forward windows", str(len(splits))))
    typer.echo(_field("Holdout", f"{holdout_definition.holdout_length} bars, withheld"))
    typer.echo(_field("Written to", str(result.directory)))
    typer.echo("")

    try:
        selection = select_best(result, objective=objective)
    except SweepError as error:
        typer.secho(str(error), fg=typer.colors.YELLOW, err=True)
        _disclaimer()
        return

    typer.echo(_field("Objective", selection.objective))
    typer.echo(_field("Selected", str(dict(selection.parameters))))
    typer.echo(_field("Score", _number(selection.score)))
    typer.echo(
        _field(
            "Candidates",
            f"{selection.candidates_scored} scored of {selection.candidates_compared} compared",
        )
    )
    typer.echo("")
    typer.echo("A best-of-many score is optimistic by construction: the more")
    typer.echo("candidates compared, the more of that score is selection luck.")

    holdout_result = None
    if evaluate_final_holdout:
        # Exactly one candidate is scored here - the one the objective already
        # chose above, over windows that never touched the holdout. Scoring the
        # rest of the grid against it would turn the holdout into another
        # selection set, which is the one thing it exists to prevent.
        selected_engine = ParametricEmaCross(
            fast_period=int(selection.parameters["fast_period"]),
            slow_period=int(selection.parameters["slow_period"]),
        )
        holdout_result = evaluate_holdout(bars, selected_engine, holdout_definition, config)
        window = holdout_result.windows[0]
        typer.echo("")
        typer.echo("FINAL HOLDOUT  (one candidate, one evaluation)")
        typer.echo("")
        typer.echo(_field("Bars", str(window.metrics.bar_count)))
        typer.echo(_field("Total return", _percent(window.metrics.total_return)))
        typer.echo(_field("Sharpe (ann.)", _number(window.metrics.sharpe_ratio)))
        typer.echo(_field("Max drawdown", _percent(window.metrics.max_drawdown)))
        typer.echo(_field("Trades", str(window.metrics.trade_count)))
        typer.echo(_field("Exposure", _percent(window.metrics.exposure)))

    written = write_selection(result, selection, holdout_result=holdout_result)
    if written is not None:
        typer.echo("")
        typer.echo(_field("Selection written", str(written)))
    _disclaimer()


__all__ = [
    "INPUT_EXIT_CODE",
    "LEAKAGE_EXIT_CODE",
    "STORAGE_EXIT_CODE",
    "UNREADABLE_INPUT_EXIT_CODE",
    "app",
]
