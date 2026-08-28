"""Bounded sweeps, reproducibility records, external storage, and selection.

The properties that matter here are the ones that stop a sweep from becoming a
machine for producing false positives: a hard ceiling on the grid, a record for
every experiment, a selection that names the windows it used, and a holdout
that selection cannot reach.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from autotrader.research import reproducibility, storage
from autotrader.research.costs import CRYPTO_COST, ZERO_COST
from autotrader.research.engines import ParametricEmaCross
from autotrader.research.experiments import (
    MAX_SWEEP_EXPERIMENTS,
    MIN_MEANINGFUL_TRADES,
    ParameterSpace,
    StudyConfig,
    SweepError,
    evaluate_holdout,
    objective_consistency,
    run_sweep,
    select_best,
    write_selection,
)
from autotrader.research.leakage import HOLDOUT_USED_IN_SELECTION, audit_holdout
from autotrader.research.metrics import CRYPTO_15M
from autotrader.research.replay import ReplayConfig
from autotrader.research.splits import SplitScheme, holdout_split, walk_forward_splits
from autotrader.research.storage import ResearchStorageError
from research_fixtures import multi_cycle, wave

STAMP = datetime(2026, 8, 28, 19, 4, 0, tzinfo=UTC)
BARS = wave(900)
TIMESTAMPS = list(BARS["timestamp"])


@pytest.fixture
def reports_root(tmp_path: Path) -> dict[str, str]:
    """An environment pointing the reports root at a temporary directory.

    A dict rather than a monkeypatched `os.environ`, because every storage
    function takes the mapping explicitly - which is what makes a test able to
    prove the repository is never written to.
    """
    root = tmp_path / "reports"
    root.mkdir()
    return {
        storage.REPORTS_ENV: str(root),
        storage.DATASETS_ENV: str(tmp_path / "datasets"),
    }


def study_splits() -> tuple:
    holdout = holdout_split(TIMESTAMPS, holdout_bars=150, embargo_bars=50)
    splits = walk_forward_splits(
        TIMESTAMPS[: holdout.study_end],
        train_bars=200,
        test_bars=100,
        scheme=SplitScheme.ROLLING,
        embargo_bars=50,
    )
    return splits, holdout


def engine_from(parameters: dict) -> ParametricEmaCross:
    return ParametricEmaCross(
        fast_period=int(parameters["fast_period"]),
        slow_period=int(parameters["slow_period"]),
    )


# ==========================================================================
# Storage: external only, never the repository
# ==========================================================================


def test_an_unset_reports_root_is_refused_rather_than_defaulted() -> None:
    """A silent fallback to a local directory is how a research run fills the
    internal disk with artifacts git is watching."""
    with pytest.raises(ResearchStorageError, match="is not set"):
        storage.resolve_reports_root({})


def test_a_blank_reports_root_is_refused() -> None:
    with pytest.raises(ResearchStorageError, match="is not set"):
        storage.resolve_reports_root({storage.REPORTS_ENV: "   "})


def test_a_relative_reports_root_is_refused() -> None:
    with pytest.raises(ResearchStorageError, match="absolute path"):
        storage.resolve_reports_root({storage.REPORTS_ENV: "reports"})


def test_a_reports_root_inside_the_repository_is_refused() -> None:
    """CRITICAL. Pointing the reports root at the checkout defeats the whole
    arrangement, and a typo is the likely cause."""
    inside = storage.repository_root() / "reports"
    with pytest.raises(ResearchStorageError, match="inside the repository"):
        storage.resolve_reports_root({storage.REPORTS_ENV: str(inside)})


def test_the_repository_root_is_derived_from_the_package_not_the_cwd() -> None:
    """So the containment check cannot be defeated by running from elsewhere."""
    assert (storage.repository_root() / "src" / "autotrader").is_dir()


def test_a_run_directory_is_namespaced_by_study_and_run(reports_root: dict) -> None:
    directory = storage.run_directory("BTC Momentum", "20260828T190400Z", reports_root)
    assert directory.parts[-3:] == ("research", "btc-momentum", "20260828T190400Z")


def test_a_symbol_slug_never_contains_a_path_separator() -> None:
    assert storage.slugify("BTC/USD") == "btc-usd"
    assert "/" not in storage.slugify("BTC/USD")


def test_a_run_id_is_derived_from_a_supplied_instant_not_the_clock() -> None:
    """So a re-run with the same stamp lands in the same directory and a test
    can assert on the path."""
    assert storage.format_run_id(STAMP) == "20260828T190400Z"


def test_an_interrupted_write_leaves_no_partial_file(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    storage.write_json(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}
    assert not list(tmp_path.glob("*.tmp"))


def test_jsonl_records_append_rather_than_replace(tmp_path: Path) -> None:
    """A sweep killed halfway still leaves every experiment it completed."""
    path = tmp_path / "experiments.jsonl"
    storage.append_jsonl(path, {"id": "a"})
    storage.append_jsonl(path, {"id": "b"})
    assert [record["id"] for record in storage.read_jsonl(path)] == ["a", "b"]


# ==========================================================================
# Reproducibility
# ==========================================================================


def test_the_same_frame_always_digests_the_same() -> None:
    assert reproducibility.dataset_digest(BARS) == reproducibility.dataset_digest(BARS.copy())


def test_a_changed_bar_changes_the_digest() -> None:
    """A commit hash says which code ran; it says nothing about which bars did.
    A revised dataset must stop looking like the original."""
    revised = BARS.copy()
    revised.loc[10, "close"] = float(revised.loc[10, "close"]) + 0.01
    assert reproducibility.dataset_digest(revised) != reproducibility.dataset_digest(BARS)


def test_reordering_rows_changes_the_digest() -> None:
    shuffled = BARS.sample(frac=1.0, random_state=1).reset_index(drop=True)
    assert reproducibility.dataset_digest(shuffled) != reproducibility.dataset_digest(BARS)


def test_a_fingerprint_records_the_dataset_interval() -> None:
    fingerprint = reproducibility.fingerprint_dataset(BARS)
    assert fingerprint.symbol == "BTC/USD"
    assert fingerprint.row_count == len(BARS)
    assert fingerprint.first_timestamp is not None
    assert fingerprint.last_timestamp is not None


def test_metadata_records_code_and_library_versions() -> None:
    metadata = reproducibility.collect(created_at=STAMP, seed=42)
    document = metadata.to_json_dict()
    assert document["seed"] == 42
    assert document["created_at_utc"].startswith("2026-08-28")
    assert document["pandas_version"]
    assert document["python_version"]


def test_a_dirty_checkout_is_recorded_rather_than_hidden() -> None:
    """A dirty tree is not a version, and a result from one must not be
    reported as if it were."""
    metadata = reproducibility.collect(created_at=STAMP)
    if metadata.git_commit is not None and metadata.git_dirty:
        assert metadata.code_version.endswith(reproducibility.DIRTY_SUFFIX)
        assert metadata.reproducible is False


def test_git_metadata_is_best_effort_and_never_raises(tmp_path: Path) -> None:
    """A study run from an export with no checkout records `None` rather than
    failing."""
    commit, dirty, branch = reproducibility.git_identity(tmp_path)
    assert (commit, dirty, branch) == (None, None, None)


def test_the_same_parameters_digest_the_same_regardless_of_key_order() -> None:
    left = reproducibility.parameter_digest({"fast": 5, "slow": 20})
    right = reproducibility.parameter_digest({"slow": 20, "fast": 5})
    assert left == right


# ==========================================================================
# The parameter grid is bounded
# ==========================================================================


def test_an_oversized_grid_is_refused_rather_than_truncated() -> None:
    """CRITICAL. Truncation would silently explore a corner of the space and
    report it as a search."""
    huge = ParameterSpace(
        name="huge",
        values={"a": tuple(range(20)), "b": tuple(range(20))},
    )
    assert huge.unconstrained_size == 400 > MAX_SWEEP_EXPERIMENTS
    with pytest.raises(SweepError, match="above the .* ceiling"):
        huge.combinations()


def test_a_constraint_cannot_smuggle_a_huge_grid_past_the_ceiling() -> None:
    """The bound is checked before the constraint, so a filter that happens to
    reduce the count does not license a wider search."""
    sneaky = ParameterSpace(
        name="sneaky",
        values={"a": tuple(range(20)), "b": tuple(range(20))},
        constraint=lambda entry: entry["a"] == 0 and entry["b"] == 0,
    )
    with pytest.raises(SweepError, match="ceiling"):
        sneaky.combinations()


def test_the_grid_is_deterministic_and_sorted_by_key() -> None:
    space = ParameterSpace(name="s", values={"slow": (50, 80), "fast": (10, 20)})
    first = space.combinations()
    assert first == space.combinations()
    assert list(first[0]) == ["fast", "slow"], "keys in sorted order"
    assert len(first) == 4


def test_a_constraint_removes_invalid_combinations() -> None:
    space = ParameterSpace(
        name="s",
        values={"fast_period": (10, 60), "slow_period": (50,)},
        constraint=lambda entry: entry["fast_period"] < entry["slow_period"],
    )
    assert space.combinations() == ({"fast_period": 10, "slow_period": 50},)


def test_a_space_with_no_valid_combination_is_refused() -> None:
    space = ParameterSpace(
        name="s",
        values={"fast_period": (60,), "slow_period": (50,)},
        constraint=lambda entry: entry["fast_period"] < entry["slow_period"],
    )
    with pytest.raises(SweepError, match="no combinations left"):
        space.combinations()


def test_an_empty_parameter_list_is_refused() -> None:
    with pytest.raises(SweepError, match="no values to sweep"):
        ParameterSpace(name="s", values={"fast": ()})


# ==========================================================================
# Running a sweep
# ==========================================================================


def small_space() -> ParameterSpace:
    return ParameterSpace(
        name="ema",
        values={"fast_period": (5, 10), "slow_period": (20, 40)},
        constraint=lambda entry: entry["fast_period"] < entry["slow_period"],
    )


def config_for() -> StudyConfig:
    return StudyConfig(
        study="test-study",
        bar_clock=CRYPTO_15M,
        replay=ReplayConfig(initial_cash=Decimal("100000"), cost_model=CRYPTO_COST),
        seed=7,
    )


def test_a_sweep_evaluates_every_combination(reports_root: dict) -> None:
    splits, holdout = study_splits()
    result = run_sweep(
        BARS,
        engine_from,
        small_space(),
        splits,
        config_for(),
        created_at=STAMP,
        holdout=holdout,
        environ=reports_root,
    )
    assert result.experiment_count == len(small_space().combinations())
    assert len(result.results) == result.experiment_count


def test_every_experiment_record_carries_its_full_provenance(
    reports_root: dict,
) -> None:
    """Parameter set, code version, dataset digest and interval, train/test
    interval, cost model, seed, metrics. A number that cannot be traced back to
    its inputs is not evidence."""
    splits, holdout = study_splits()
    result = run_sweep(
        BARS,
        engine_from,
        small_space(),
        splits,
        config_for(),
        created_at=STAMP,
        holdout=holdout,
        environ=reports_root,
    )
    record = result.records[0].to_json_dict()

    assert record["parameters"]
    assert record["reproducibility"]["code_version"]
    assert record["dataset"]["digest"]
    assert record["dataset"]["first_timestamp"]
    assert record["cost_model"]["label"] == CRYPTO_COST.label
    assert record["seed"] == 7
    assert record["bar_clock"] == "crypto-15m"
    assert record["walk_forward"]["window_count"] == len(splits)
    assert record["walk_forward"]["windows"][0]["split"]["train_start_timestamp"]


def test_a_sweep_writes_its_records_to_external_storage(reports_root: dict) -> None:
    splits, holdout = study_splits()
    result = run_sweep(
        BARS,
        engine_from,
        small_space(),
        splits,
        config_for(),
        created_at=STAMP,
        holdout=holdout,
        environ=reports_root,
    )
    assert result.directory is not None
    assert (result.directory / storage.MANIFEST_FILENAME).exists()
    assert (result.directory / storage.SPLITS_FILENAME).exists()

    records = storage.read_jsonl(result.directory / storage.EXPERIMENTS_FILENAME)
    assert len(records) == result.experiment_count


def test_nothing_is_written_inside_the_repository(reports_root: dict) -> None:
    """CRITICAL. The whole point of the external root."""
    splits, holdout = study_splits()
    result = run_sweep(
        BARS,
        engine_from,
        small_space(),
        splits,
        config_for(),
        created_at=STAMP,
        holdout=holdout,
        environ=reports_root,
    )
    assert result.directory is not None
    assert storage.repository_root() not in result.directory.parents


def test_a_sweep_can_run_without_writing_anything(reports_root: dict) -> None:
    splits, holdout = study_splits()
    result = run_sweep(
        BARS,
        engine_from,
        small_space(),
        splits,
        config_for(),
        created_at=STAMP,
        holdout=holdout,
        write=False,
        environ=reports_root,
    )
    assert result.directory is None
    assert result.experiment_count > 0


def test_the_manifest_records_the_ceiling_the_sweep_ran_under(
    reports_root: dict,
) -> None:
    splits, holdout = study_splits()
    result = run_sweep(
        BARS,
        engine_from,
        small_space(),
        splits,
        config_for(),
        created_at=STAMP,
        holdout=holdout,
        environ=reports_root,
    )
    assert result.directory is not None
    manifest = json.loads((result.directory / storage.MANIFEST_FILENAME).read_text())
    assert manifest["max_experiments"] == MAX_SWEEP_EXPERIMENTS
    assert manifest["experiment_count"] == result.experiment_count
    assert manifest["dataset"]["digest"]


def test_a_sweep_needs_at_least_one_window(reports_root: dict) -> None:
    with pytest.raises(SweepError, match="at least one walk-forward window"):
        run_sweep(
            BARS,
            engine_from,
            small_space(),
            (),
            config_for(),
            created_at=STAMP,
            environ=reports_root,
        )


def test_re_running_a_sweep_produces_the_same_experiment_ids(
    reports_root: dict,
) -> None:
    splits, holdout = study_splits()
    kwargs = dict(created_at=STAMP, holdout=holdout, write=False, environ=reports_root)
    first = run_sweep(BARS, engine_from, small_space(), splits, config_for(), **kwargs)
    second = run_sweep(BARS, engine_from, small_space(), splits, config_for(), **kwargs)
    assert [r.experiment_id for r in first.records] == [r.experiment_id for r in second.records]


# ==========================================================================
# Selection
# ==========================================================================


def swept(reports_root: dict, bars=None):
    splits, holdout = study_splits()
    return (
        run_sweep(
            BARS if bars is None else bars,
            engine_from,
            small_space(),
            splits,
            config_for(),
            created_at=STAMP,
            holdout=holdout,
            environ=reports_root,
        ),
        splits,
        holdout,
    )


def test_a_selection_names_every_window_that_informed_it(reports_root: dict) -> None:
    sweep, splits, _ = swept(reports_root)
    try:
        selection = select_best(sweep, objective="median-return")
    except SweepError:
        pytest.skip("no candidate was scoreable on this fixture")
    assert selection.selection_split_indices == tuple(split.index for split in splits)


def test_a_selection_records_how_many_candidates_it_compared(
    reports_root: dict,
) -> None:
    """A best-of-200 score and a best-of-3 score are not the same claim."""
    sweep, _, _ = swept(reports_root)
    selection = select_best(sweep, objective="median-return")
    assert selection.candidates_compared == sweep.experiment_count
    assert selection.candidates_scored <= selection.candidates_compared


def test_selection_windows_never_reach_the_holdout(reports_root: dict) -> None:
    """CRITICAL. This is what makes the final number an honest one."""
    sweep, splits, holdout = swept(reports_root)
    report = audit_holdout(holdout, total_bars=len(BARS), selection_splits=splits)
    assert HOLDOUT_USED_IN_SELECTION not in report.codes
    assert report.clean, report.codes


def test_an_unknown_objective_lists_the_known_ones(reports_root: dict) -> None:
    sweep, _, _ = swept(reports_root)
    with pytest.raises(SweepError, match="Known objectives"):
        select_best(sweep, objective="make-money")


def test_the_consistency_objective_refuses_a_candidate_that_barely_traded(
    reports_root: dict,
) -> None:
    """A win rate over four trades is a coin flip with a percentage sign."""
    sweep, _, _ = swept(reports_root)
    for record in sweep.records:
        result = sweep.results[record.experiment_id]
        if result.total_trades < MIN_MEANINGFUL_TRADES:
            assert objective_consistency(result) is None


def test_a_sweep_where_nothing_is_scoreable_says_so(reports_root: dict) -> None:
    """That is a result, not an error to work around: the space contains no
    defensible candidate."""
    sweep, _, _ = swept(reports_root)
    scoreable = [
        objective_consistency(sweep.results[record.experiment_id]) for record in sweep.records
    ]
    if all(score is None for score in scoreable):
        with pytest.raises(SweepError, match="No candidate could be scored"):
            select_best(sweep, objective="consistency")


def test_ties_are_broken_deterministically(reports_root: dict) -> None:
    sweep, _, _ = swept(reports_root)
    first = select_best(sweep, objective="median-return")
    second = select_best(sweep, objective="median-return")
    assert first.experiment_id == second.experiment_id
    assert first.ranking == second.ranking


def test_a_selection_is_written_alongside_the_experiments(
    reports_root: dict,
) -> None:
    sweep, _, _ = swept(reports_root)
    selection = select_best(sweep, objective="median-return")
    path = write_selection(sweep, selection)
    assert path is not None and path.exists()
    document = json.loads(path.read_text())
    assert document["selection"]["objective"] == "median-return"
    assert document["selection"]["candidates_compared"] == sweep.experiment_count


# ==========================================================================
# The holdout is evaluated once, for one already-chosen candidate
# ==========================================================================


def test_the_holdout_is_evaluated_for_a_single_engine(reports_root: dict) -> None:
    sweep, _, holdout = swept(reports_root)
    selection = select_best(sweep, objective="median-return")
    engine = engine_from(dict(selection.parameters))

    result = evaluate_holdout(BARS, engine, holdout, config_for())
    assert result.window_count == 1
    assert result.windows[0].metrics.bar_count == holdout.holdout_length


def test_the_holdout_result_is_stored_with_the_selection(reports_root: dict) -> None:
    sweep, _, holdout = swept(reports_root)
    selection = select_best(sweep, objective="median-return")
    engine = engine_from(dict(selection.parameters))
    holdout_result = evaluate_holdout(BARS, engine, holdout, config_for())

    path = write_selection(sweep, selection, holdout_result=holdout_result)
    assert path is not None
    document = json.loads(path.read_text())
    assert document["holdout"]["window_count"] == 1


def test_a_zero_cost_sweep_scores_at_least_as_well_as_a_costed_one(
    reports_root: dict,
) -> None:
    """Costs can only subtract. A study reported under `frictionless` is
    reporting an upper bound, and this pins that it really is one."""
    splits, holdout = study_splits()
    bars = multi_cycle()
    timestamps = list(bars["timestamp"])
    local_splits = walk_forward_splits(
        timestamps, train_bars=60, test_bars=100, scheme=SplitScheme.ROLLING
    )

    def sweep_with(cost) -> float:
        config = StudyConfig(
            study="cost-comparison",
            bar_clock=CRYPTO_15M,
            replay=ReplayConfig(initial_cash=Decimal("100000"), cost_model=cost),
        )
        result = run_sweep(
            bars,
            engine_from,
            ParameterSpace(name="one", values={"fast_period": (5,), "slow_period": (20,)}),
            local_splits,
            config,
            created_at=STAMP,
            write=False,
            environ=reports_root,
        )
        walk_forward = result.results[result.records[0].experiment_id]
        return sum(window.metrics.total_return for window in walk_forward.windows)

    assert sweep_with(ZERO_COST) >= sweep_with(CRYPTO_COST)
