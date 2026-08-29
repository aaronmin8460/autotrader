"""Bounded parameter sweeps, and the records they leave behind.

A sweep is the most dangerous tool in this package. Given enough parameter sets
and one dataset, something will look excellent, and the probability that it is
noise rises with every combination tried. So the sweep here is **bounded by
construction** and **recorded in full**:

**Bounded.** `MAX_SWEEP_EXPERIMENTS` is a hard ceiling and a grid that exceeds
it is refused, not truncated. Truncation would silently explore a corner of the
space and report it as a search. The ceiling is low on purpose: this is
infrastructure for evaluating a handful of candidate engines carefully, not for
mining a parameter surface.

**Recorded.** Every experiment writes a record carrying the parameter set, the
code version, the dataset digest and interval, the train/test interval, the
cost model, the seed, and the metrics. A number that cannot be traced back to
the inputs that made it is not evidence, and a sweep that keeps only its winner
has thrown away the distribution that would tell you whether the winner is one.

**Selected honestly.** `select_best` ranks by an objective over the study
region's walk-forward windows and returns a `SelectionRecord` that names every
window that informed the choice. The final holdout is not among them, and
`autotrader.research.leakage.audit_holdout` is given that record to prove it.
The selection also carries how many candidates were compared, because a
"best Sharpe of 1.8" means something different out of 3 candidates than out of
200 - that is the multiple-comparisons problem, and the count is the minimum
needed to reason about it.

Nothing here optimizes for a single metric by default. `select_best` requires
an objective to be named, and `Objective` includes composite options that
refuse a candidate on grounds a raw return figure would not - too few trades to
mean anything, a drawdown past a limit, an edge that only exists in one window.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from autotrader.research import reproducibility, storage
from autotrader.research.engines import DecisionEngine
from autotrader.research.metrics import BarClock
from autotrader.research.replay import ReplayConfig
from autotrader.research.splits import HoldoutSplit, TimeSplit
from autotrader.research.walkforward import WalkForwardResult, run_walk_forward

#: The hard ceiling on one sweep. A grid larger than this is refused.
#:
#: Not a performance limit - it is a statistical one. Every additional
#: parameter set is another chance for noise to win, and a search wide enough
#: to need a bigger number is a search whose best result cannot be believed
#: without a correction nothing here implements.
MAX_SWEEP_EXPERIMENTS = 256

#: The minimum number of round trips before a candidate's trade statistics are
#: treated as meaningful. A win rate over four trades is a coin flip with a
#: percentage sign.
MIN_MEANINGFUL_TRADES = 20


class SweepError(Exception):
    """A sweep was requested that cannot be run, or should not be."""


# --------------------------------------------------------------------------
# The parameter grid
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParameterSpace:
    """A named, finite, ordered set of parameter combinations.

    Values are enumerated in the order given and the cartesian product is
    generated in sorted key order, so the same space always produces the same
    experiments in the same sequence - a sweep that is re-run lands on the same
    experiment identifiers and its records line up with the previous run's.
    """

    name: str
    values: Mapping[str, Sequence[object]]
    constraint: Callable[[Mapping[str, object]], bool] | None = None

    def __post_init__(self) -> None:
        if not self.values:
            raise SweepError("A parameter space needs at least one parameter.")
        for key, options in self.values.items():
            if not options:
                raise SweepError(f"Parameter {key!r} has no values to sweep.")

    @property
    def unconstrained_size(self) -> int:
        """How many combinations exist before the constraint is applied."""
        size = 1
        for options in self.values.values():
            size *= len(options)
        return size

    def combinations(self) -> tuple[dict[str, object], ...]:
        """Every valid parameter set, in deterministic order.

        `constraint` filters combinations that are invalid rather than merely
        uninteresting - a fast EMA period at or above the slow one cannot
        cross, and running it would produce an empty result that then has to be
        explained. The bound is checked against the *unconstrained* size, so a
        constraint cannot be used to smuggle a huge grid past the ceiling.
        """
        if self.unconstrained_size > MAX_SWEEP_EXPERIMENTS:
            raise SweepError(
                f"Parameter space {self.name!r} has {self.unconstrained_size} combinations, "
                f"above the {MAX_SWEEP_EXPERIMENTS} ceiling. Narrow the grid rather than "
                "raising the limit: a search this wide will find something that looks good "
                "whether or not anything is there."
            )
        keys = sorted(self.values)
        combinations = [
            dict(zip(keys, chosen, strict=True))
            for chosen in itertools.product(*(self.values[key] for key in keys))
        ]
        if self.constraint is not None:
            combinations = [entry for entry in combinations if self.constraint(entry)]
        if not combinations:
            raise SweepError(
                f"Parameter space {self.name!r} has no combinations left after its constraint."
            )
        return tuple(combinations)

    def to_json_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "values": {key: list(options) for key, options in sorted(self.values.items())},
            "unconstrained_size": self.unconstrained_size,
            "constrained_size": len(self.combinations()),
        }


# --------------------------------------------------------------------------
# The experiment record
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExperimentRecord:
    """Everything one parameter set produced, and everything it came from.

    This is the durable unit of a study. It is written as one JSONL line and is
    self-contained on purpose: a record read a year later identifies its code,
    its data, its cost assumptions and its windows without needing the study
    directory it came from to still make sense.
    """

    experiment_id: str
    study: str
    engine: Mapping[str, object]
    parameters: Mapping[str, object]
    dataset: Mapping[str, object]
    cost_model: Mapping[str, object]
    bar_clock: str
    seed: int | None
    reproducibility: Mapping[str, object]
    walk_forward: Mapping[str, object]
    holdout: Mapping[str, object] | None = None

    def to_json_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "study": self.study,
            "engine": dict(self.engine),
            "parameters": dict(self.parameters),
            "dataset": dict(self.dataset),
            "cost_model": dict(self.cost_model),
            "bar_clock": self.bar_clock,
            "seed": self.seed,
            "reproducibility": dict(self.reproducibility),
            "walk_forward": dict(self.walk_forward),
            "holdout": None if self.holdout is None else dict(self.holdout),
        }


@dataclass(frozen=True)
class SweepResult:
    """Every experiment a sweep produced, and where they were written."""

    study: str
    run_id: str
    directory: Path | None
    records: tuple[ExperimentRecord, ...]
    results: Mapping[str, WalkForwardResult]
    splits: tuple[TimeSplit, ...]
    holdout: HoldoutSplit | None

    @property
    def experiment_count(self) -> int:
        return len(self.records)


# --------------------------------------------------------------------------
# Objectives
# --------------------------------------------------------------------------


def objective_median_sharpe(result: WalkForwardResult) -> float | None:
    """Median Sharpe ratio across windows. `None` when no window has one."""
    return result.summary("sharpe_ratio")["median"]  # type: ignore[return-value]


def objective_median_return(result: WalkForwardResult) -> float | None:
    """Median total return across windows."""
    return result.summary("total_return")["median"]  # type: ignore[return-value]


def objective_consistency(result: WalkForwardResult) -> float | None:
    """Median Sharpe, but only for a candidate that traded and was consistent.

    Refuses - by returning `None` - a candidate that took too few trades to
    mean anything, or that was profitable in half its windows or fewer. A
    parameter set that wins on one window out of nine has not demonstrated an
    edge, and this objective declines to rank it at all rather than ranking it
    highly.

    This is the objective a study should reach for by default. The single-metric
    ones exist because they are sometimes what a question calls for, not
    because ranking by one number is a good idea.
    """
    if result.total_trades < MIN_MEANINGFUL_TRADES:
        return None
    positive = result.positive_window_fraction()
    if positive is None or positive <= 0.5:
        return None
    return result.summary("sharpe_ratio")["median"]  # type: ignore[return-value]


#: Objectives addressable by name from a CLI or study configuration.
OBJECTIVES: dict[str, Callable[[WalkForwardResult], float | None]] = {
    "median-sharpe": objective_median_sharpe,
    "median-return": objective_median_return,
    "consistency": objective_consistency,
}


@dataclass(frozen=True)
class SelectionRecord:
    """Which candidate was chosen, on what basis, against which windows.

    `selection_split_indices` is the evidence that the final holdout was not
    consulted: it names exactly the windows the objective was computed over,
    and `autotrader.research.leakage.audit_holdout` checks none of them reach
    into the holdout.

    `candidates_compared` is carried because a best-of-200 result and a
    best-of-3 result are not the same claim, and the number is the only way a
    later reader can tell which one they are looking at.
    """

    objective: str
    experiment_id: str
    parameters: Mapping[str, object]
    score: float
    candidates_compared: int
    candidates_scored: int
    selection_split_indices: tuple[int, ...]
    ranking: tuple[tuple[str, float], ...]

    def to_json_dict(self) -> dict[str, object]:
        return {
            "objective": self.objective,
            "experiment_id": self.experiment_id,
            "parameters": dict(self.parameters),
            "score": self.score,
            "candidates_compared": self.candidates_compared,
            "candidates_scored": self.candidates_scored,
            "selection_split_indices": list(self.selection_split_indices),
            "ranking": [{"experiment_id": key, "score": value} for key, value in self.ranking],
        }


def select_best(
    sweep: SweepResult,
    *,
    objective: str = "consistency",
) -> SelectionRecord:
    """Rank a sweep's candidates and return the winner, with its evidence.

    Candidates the objective returns `None` for are not ranked at all - they
    are unscoreable rather than worst, and putting them at the bottom of a
    ranking would imply they were measured and found wanting.

    Raises `SweepError` when nothing is scoreable, which is a real and
    informative outcome: it means no parameter set met the bar, and the correct
    response is to report that rather than to pick the least bad.
    """
    try:
        score_of = OBJECTIVES[objective]
    except KeyError:
        known = ", ".join(sorted(OBJECTIVES))
        raise SweepError(f"Unknown objective {objective!r}. Known objectives: {known}.") from None

    scored: list[tuple[str, float]] = []
    for record in sweep.records:
        result = sweep.results[record.experiment_id]
        score = score_of(result)
        if score is not None:
            scored.append((record.experiment_id, float(score)))

    if not scored:
        raise SweepError(
            f"No candidate could be scored under {objective!r}. Every parameter set either "
            "traded too little to measure or failed the objective's consistency bar. That is "
            "a result: this space contains no defensible candidate."
        )

    # Ties broken by experiment id so a re-run selects the same winner.
    ranking = tuple(sorted(scored, key=lambda entry: (-entry[1], entry[0])))
    winner_id, winner_score = ranking[0]
    winner = next(record for record in sweep.records if record.experiment_id == winner_id)

    return SelectionRecord(
        objective=objective,
        experiment_id=winner_id,
        parameters=dict(winner.parameters),
        score=winner_score,
        candidates_compared=len(sweep.records),
        candidates_scored=len(scored),
        selection_split_indices=tuple(split.index for split in sweep.splits),
        ranking=ranking,
    )


# --------------------------------------------------------------------------
# Running a sweep
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StudyConfig:
    """The non-parameter half of a study: data handling and cost assumptions."""

    study: str
    bar_clock: BarClock
    replay: ReplayConfig = field(default_factory=ReplayConfig)
    seed: int | None = None
    risk_free_rate: float = 0.0


def run_sweep(
    bars: pd.DataFrame,
    engine_factory: Callable[[Mapping[str, object]], DecisionEngine],
    space: ParameterSpace,
    splits: Sequence[TimeSplit],
    config: StudyConfig,
    *,
    created_at: datetime,
    holdout: HoldoutSplit | None = None,
    write: bool = True,
    environ: Mapping[str, str] | None = None,
) -> SweepResult:
    """Evaluate every parameter set in `space` over `splits`, recording each.

    `engine_factory` turns one parameter set into a `DecisionEngine`, which is
    what keeps this loop from knowing anything about the strategy: a V2 engine
    is swept by supplying a factory, not by editing this function.

    `write` controls whether records reach external storage. A study that only
    wants the numbers in memory sets it to `False`; anything that will be
    reported leaves it `True`, and the records land under
    `AUTOTRADER_QA_REPORTS` (never inside the repository - `storage` refuses).

    The holdout is **not** evaluated here. It is evaluated once, after a
    selection has been made, by `evaluate_holdout`.
    """
    combinations = space.combinations()
    if len(combinations) > MAX_SWEEP_EXPERIMENTS:  # pragma: no cover - refused in combinations()
        raise SweepError(f"{len(combinations)} experiments exceeds {MAX_SWEEP_EXPERIMENTS}.")
    if not splits:
        raise SweepError("A sweep needs at least one walk-forward window.")

    metadata = reproducibility.collect(created_at=created_at, seed=config.seed)
    fingerprint = reproducibility.fingerprint_dataset(bars)
    run_id = storage.format_run_id(created_at)

    directory: Path | None = None
    if write:
        directory = storage.ensure_run_directory(config.study, run_id, environ)
        storage.write_json(
            directory / storage.MANIFEST_FILENAME,
            {
                "study": config.study,
                "run_id": run_id,
                "parameter_space": space.to_json_dict(),
                "dataset": fingerprint.to_json_dict(),
                "cost_model": config.replay.cost_model.to_json_dict(),
                "replay": config.replay.to_json_dict(),
                "bar_clock": config.bar_clock.label,
                "seed": config.seed,
                "reproducibility": metadata.to_json_dict(),
                "experiment_count": len(combinations),
                "max_experiments": MAX_SWEEP_EXPERIMENTS,
            },
        )
        storage.write_json(
            directory / storage.SPLITS_FILENAME,
            {
                "walk_forward": [split.to_json_dict() for split in splits],
                "holdout": None if holdout is None else holdout.to_json_dict(),
            },
        )

    records: list[ExperimentRecord] = []
    results: dict[str, WalkForwardResult] = {}

    for parameters in combinations:
        engine = engine_factory(parameters)
        walk_forward = run_walk_forward(
            bars,
            engine,
            splits,
            bar_clock=config.bar_clock,
            config=config.replay,
            risk_free_rate=config.risk_free_rate,
        )
        experiment_id = reproducibility.parameter_digest(parameters)
        record = ExperimentRecord(
            experiment_id=experiment_id,
            study=config.study,
            engine=walk_forward.engine,
            parameters=dict(parameters),
            dataset=fingerprint.to_json_dict(),
            cost_model=config.replay.cost_model.to_json_dict(),
            bar_clock=config.bar_clock.label,
            seed=config.seed,
            reproducibility=metadata.to_json_dict(),
            walk_forward=walk_forward.to_json_dict(),
        )
        records.append(record)
        results[experiment_id] = walk_forward
        if directory is not None:
            storage.append_jsonl(directory / storage.EXPERIMENTS_FILENAME, record.to_json_dict())

    return SweepResult(
        study=config.study,
        run_id=run_id,
        directory=directory,
        records=tuple(records),
        results=results,
        splits=tuple(splits),
        holdout=holdout,
    )


def evaluate_holdout(
    bars: pd.DataFrame,
    engine: DecisionEngine,
    holdout: HoldoutSplit,
    config: StudyConfig,
) -> WalkForwardResult:
    """Score one already-selected engine over the final holdout. Once.

    Deliberately a separate call, taking a single engine rather than a sweep:
    there is no way to spell "evaluate every candidate on the holdout and keep
    the best", because that is the thing the holdout exists to prevent. The
    holdout answers one question about one already-chosen configuration, and
    running it a second time with a different candidate makes the first answer
    worthless.

    The holdout is evaluated as a single window with the engine's warm-up drawn
    from the embargo bars immediately before it - bars that no walk-forward
    window scored and no selection saw.
    """
    warmup = engine.warmup_bars
    start = max(0, holdout.holdout_start - warmup)
    used = holdout.holdout_start - start
    split = TimeSplit(
        index=0,
        train_start=max(0, start - 1) if start > 0 else 0,
        train_end=max(1, start),
        test_start=holdout.holdout_start,
        test_end=holdout.holdout_end,
        embargo_bars=0,
        train_start_timestamp=bars["timestamp"].iloc[max(0, start - 1)],
        train_end_timestamp=bars["timestamp"].iloc[max(0, start - 1)],
        test_start_timestamp=bars["timestamp"].iloc[holdout.holdout_start],
        test_end_timestamp=bars["timestamp"].iloc[holdout.holdout_end - 1],
    )
    del used
    return run_walk_forward(
        bars,
        engine,
        (split,),
        bar_clock=config.bar_clock,
        config=config.replay,
        risk_free_rate=config.risk_free_rate,
    )


def write_selection(
    sweep: SweepResult,
    selection: SelectionRecord,
    *,
    holdout_result: WalkForwardResult | None = None,
) -> Path | None:
    """Persist a selection, and the holdout score if one was taken.

    Returns the path written, or `None` when the sweep was run without storage.
    """
    if sweep.directory is None:
        return None
    path = sweep.directory / storage.SELECTION_FILENAME
    storage.write_json(
        path,
        {
            "selection": selection.to_json_dict(),
            "holdout": None if holdout_result is None else holdout_result.to_json_dict(),
        },
    )
    return path


__all__ = [
    "MAX_SWEEP_EXPERIMENTS",
    "MIN_MEANINGFUL_TRADES",
    "OBJECTIVES",
    "ExperimentRecord",
    "ParameterSpace",
    "SelectionRecord",
    "StudyConfig",
    "SweepError",
    "SweepResult",
    "evaluate_holdout",
    "objective_consistency",
    "objective_median_return",
    "objective_median_sharpe",
    "run_sweep",
    "select_best",
    "write_selection",
]
