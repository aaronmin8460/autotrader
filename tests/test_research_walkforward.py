"""Time-series splits and walk-forward evaluation.

The properties pinned here are the ones that make a walk-forward number mean
what it says: windows are contiguous and ordered, test windows do not overlap
by default, the embargo is real, the holdout is carved off first, and each
window is evaluated from flat over its own bars.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from autotrader.research.engines import BuyAndHoldEngine, EmaCrossEngine, ParametricEmaCross
from autotrader.research.metrics import CRYPTO_15M
from autotrader.research.replay import ReplayConfig
from autotrader.research.splits import (
    SplitError,
    SplitScheme,
    holdout_split,
    require_ordered_timestamps,
    walk_forward_splits,
)
from autotrader.research.walkforward import WalkForwardError, run_walk_forward
from research_fixtures import multi_cycle, wave

BARS = wave(800)
TIMESTAMPS = list(BARS["timestamp"])


# --------------------------------------------------------------------------
# Split generation
# --------------------------------------------------------------------------


def test_windows_are_contiguous_ordered_and_forward_only() -> None:
    splits = walk_forward_splits(
        TIMESTAMPS, train_bars=200, test_bars=100, scheme=SplitScheme.ROLLING
    )
    assert splits
    for split in splits:
        assert split.train_start < split.train_end <= split.test_start < split.test_end
        assert split.train_start_timestamp <= split.train_end_timestamp
        assert split.test_start_timestamp > split.train_end_timestamp


def test_test_windows_do_not_overlap_by_default() -> None:
    """The default step is the test length, so every bar is scored at most
    once and the windows are a partition rather than a resampling."""
    splits = walk_forward_splits(
        TIMESTAMPS, train_bars=200, test_bars=100, scheme=SplitScheme.ROLLING
    )
    scored: set[int] = set()
    for split in splits:
        window = set(range(split.test_start, split.test_end))
        assert not (window & scored), "a bar was scored twice"
        scored |= window


def test_a_rolling_window_keeps_a_fixed_train_length() -> None:
    splits = walk_forward_splits(
        TIMESTAMPS, train_bars=200, test_bars=100, scheme=SplitScheme.ROLLING
    )
    assert {split.train_length for split in splits} == {200}


def test_an_anchored_window_grows_from_a_fixed_origin() -> None:
    splits = walk_forward_splits(
        TIMESTAMPS, train_bars=200, test_bars=100, scheme=SplitScheme.ANCHORED
    )
    assert {split.train_start for split in splits} == {0}
    lengths = [split.train_length for split in splits]
    assert lengths == sorted(lengths) and lengths[0] < lengths[-1]


def test_the_embargo_is_a_real_gap_between_train_and_test() -> None:
    splits = walk_forward_splits(
        TIMESTAMPS,
        train_bars=200,
        test_bars=100,
        scheme=SplitScheme.ROLLING,
        embargo_bars=37,
    )
    for split in splits:
        assert split.gap == 37
        assert split.test_start - split.train_end == 37


def test_a_trailing_remainder_is_dropped_rather_than_tested_short() -> None:
    """A final window of a different length is not comparable to the others and
    would be averaged in as though it were."""
    splits = walk_forward_splits(
        TIMESTAMPS, train_bars=200, test_bars=100, scheme=SplitScheme.ROLLING
    )
    assert {split.test_length for split in splits} == {100}
    assert splits[-1].test_end <= len(TIMESTAMPS)


def test_too_few_bars_for_one_window_is_refused() -> None:
    with pytest.raises(SplitError, match="cannot produce"):
        walk_forward_splits(
            TIMESTAMPS[:50], train_bars=200, test_bars=100, scheme=SplitScheme.ROLLING
        )


@pytest.mark.parametrize(
    ("train", "test", "embargo"),
    [(0, 10, 0), (10, 0, 0), (10, 10, -1)],
)
def test_nonsensical_window_sizes_are_refused(train: int, test: int, embargo: int) -> None:
    with pytest.raises(SplitError):
        walk_forward_splits(
            TIMESTAMPS,
            train_bars=train,
            test_bars=test,
            scheme=SplitScheme.ROLLING,
            embargo_bars=embargo,
        )


def test_unordered_timestamps_are_refused_rather_than_sorted() -> None:
    """Sorting here would hide the upstream violation and turn a shuffled
    dataset into a plausible-looking walk-forward study."""
    scrambled = list(TIMESTAMPS)
    scrambled[10], scrambled[200] = scrambled[200], scrambled[10]
    with pytest.raises(SplitError, match="not ascending"):
        require_ordered_timestamps(scrambled)


def test_duplicate_timestamps_are_refused() -> None:
    duplicated = TIMESTAMPS[:100] + [TIMESTAMPS[99]] + TIMESTAMPS[100:]
    with pytest.raises(SplitError, match="Duplicate timestamp"):
        require_ordered_timestamps(duplicated)


def test_there_is_no_shuffle_option_anywhere_in_the_split_api() -> None:
    """A knob that must never be turned should not exist."""
    import inspect

    from autotrader.research import splits as splits_module

    source = inspect.getsource(splits_module)
    for forbidden in ("shuffle", "random_state", "train_test_split", "KFold"):
        assert forbidden not in source.replace("shuffled split", "").replace("shuffling", ""), (
            forbidden
        )


# --------------------------------------------------------------------------
# The final holdout
# --------------------------------------------------------------------------


def test_the_holdout_is_the_final_stretch_of_the_dataset() -> None:
    holdout = holdout_split(TIMESTAMPS, holdout_bars=150, embargo_bars=25)
    assert holdout.holdout_end == len(TIMESTAMPS)
    assert holdout.holdout_length == 150
    assert holdout.study_end == len(TIMESTAMPS) - 150 - 25


def test_the_embargo_bars_belong_to_neither_region() -> None:
    holdout = holdout_split(TIMESTAMPS, holdout_bars=150, embargo_bars=25)
    assert holdout.holdout_start - holdout.study_end == 25


def test_study_and_holdout_slices_do_not_overlap() -> None:
    holdout = holdout_split(TIMESTAMPS, holdout_bars=150, embargo_bars=25)
    study = holdout.study_slice(BARS)
    withheld = holdout.holdout_slice(BARS)
    assert set(study["timestamp"]) & set(withheld["timestamp"]) == set()


def test_a_holdout_that_would_leave_no_study_region_is_refused() -> None:
    with pytest.raises(SplitError, match="no study region"):
        holdout_split(TIMESTAMPS[:100], holdout_bars=100, embargo_bars=10)


def test_splitting_the_study_region_never_reaches_the_holdout() -> None:
    """The intended sequence: carve the holdout off first, then split what is
    left. Windows generated this way cannot touch it."""
    holdout = holdout_split(TIMESTAMPS, holdout_bars=150, embargo_bars=25)
    splits = walk_forward_splits(
        TIMESTAMPS[: holdout.study_end],
        train_bars=200,
        test_bars=100,
        scheme=SplitScheme.ROLLING,
        embargo_bars=25,
    )
    assert max(split.test_end for split in splits) <= holdout.study_end


# --------------------------------------------------------------------------
# Walk-forward evaluation
# --------------------------------------------------------------------------


def splits_for(bars_count: int = 800) -> tuple:
    return walk_forward_splits(
        list(wave(bars_count)["timestamp"]),
        train_bars=200,
        test_bars=100,
        scheme=SplitScheme.ROLLING,
        embargo_bars=25,
    )


def test_each_window_is_evaluated_and_reported_separately() -> None:
    splits = splits_for()
    result = run_walk_forward(BARS, EmaCrossEngine(), splits, bar_clock=CRYPTO_15M)
    assert result.window_count == len(splits)
    assert [window.split.index for window in result.windows] == list(range(len(splits)))


def test_every_window_starts_flat_with_the_same_capital() -> None:
    """Windows must not compound into one another, or one lucky early window
    is reported as consistency."""
    config = ReplayConfig(initial_cash=Decimal("50000"))
    result = run_walk_forward(
        BARS, EmaCrossEngine(), splits_for(), bar_clock=CRYPTO_15M, config=config
    )
    for window in result.windows:
        assert window.result.initial_cash == Decimal("50000")


def test_a_window_is_scored_only_over_its_own_bars() -> None:
    """The warm-up prefix primes indicators and is then excluded, so a window
    is never credited with bars belonging to the window before it."""
    engine = EmaCrossEngine()
    result = run_walk_forward(BARS, engine, splits_for(), bar_clock=CRYPTO_15M)
    for window in result.windows:
        assert window.metrics.bar_count == window.split.test_length
        assert window.warmup_bars_used <= engine.warmup_bars


def test_warmup_is_drawn_from_bars_before_the_window_only() -> None:
    result = run_walk_forward(BARS, EmaCrossEngine(), splits_for(), bar_clock=CRYPTO_15M)
    for window in result.windows:
        assert window.warmup_bars_used <= window.split.test_start


def test_the_summary_reports_a_distribution_not_a_single_number() -> None:
    result = run_walk_forward(BARS, EmaCrossEngine(), splits_for(), bar_clock=CRYPTO_15M)
    summary = result.summary("total_return")
    assert summary["windows"] == result.window_count
    for key in ("median", "mean", "stdev", "minimum", "maximum"):
        assert key in summary
    assert summary["minimum"] <= summary["median"] <= summary["maximum"]


def test_windows_where_a_metric_is_undefined_are_skipped_not_zeroed() -> None:
    """A window with no trades has no win rate; counting it as 0% would drag
    the summary toward a number no window produced."""
    result = run_walk_forward(BARS, EmaCrossEngine(), splits_for(), bar_clock=CRYPTO_15M)
    win_rates = result.values("win_rate")
    assert len(win_rates) <= result.window_count
    assert all(rate is not None for rate in win_rates)


def test_the_positive_window_fraction_is_reported() -> None:
    result = run_walk_forward(BARS, EmaCrossEngine(), splits_for(), bar_clock=CRYPTO_15M)
    fraction = result.positive_window_fraction()
    assert fraction is None or 0.0 <= fraction <= 1.0


def test_walk_forward_needs_at_least_one_window() -> None:
    with pytest.raises(WalkForwardError, match="at least one split"):
        run_walk_forward(BARS, EmaCrossEngine(), (), bar_clock=CRYPTO_15M)


def test_a_walk_forward_result_serializes_completely() -> None:
    result = run_walk_forward(BARS, EmaCrossEngine(), splits_for(), bar_clock=CRYPTO_15M)
    document = result.to_json_dict()

    assert document["engine"]["name"] == "ema-cross"
    assert document["window_count"] == result.window_count
    assert len(document["windows"]) == result.window_count
    assert "sharpe_ratio" in document["summary"]
    assert document["windows"][0]["split"]["test_start"] is not None


def test_two_engines_are_evaluated_over_identical_windows() -> None:
    """Comparability: a strategy and its benchmark must be scored on the same
    bars, or the comparison measures the windows rather than the engines."""
    splits = splits_for()
    strategy = run_walk_forward(BARS, EmaCrossEngine(), splits, bar_clock=CRYPTO_15M)
    benchmark = run_walk_forward(BARS, BuyAndHoldEngine(warmup=50), splits, bar_clock=CRYPTO_15M)
    assert strategy.window_count == benchmark.window_count
    for left, right in zip(strategy.windows, benchmark.windows, strict=True):
        assert left.split.test_start == right.split.test_start
        assert left.metrics.bar_count == right.metrics.bar_count


def test_a_parametric_engine_walks_forward_too() -> None:
    result = run_walk_forward(
        BARS,
        ParametricEmaCross(fast_period=5, slow_period=20),
        splits_for(),
        bar_clock=CRYPTO_15M,
    )
    assert result.engine["parameters"] == {"fast_period": 5, "slow_period": 20}
    assert result.window_count > 0


def test_walk_forward_is_deterministic() -> None:
    splits = splits_for()
    first = run_walk_forward(BARS, EmaCrossEngine(), splits, bar_clock=CRYPTO_15M)
    second = run_walk_forward(BARS, EmaCrossEngine(), splits, bar_clock=CRYPTO_15M)
    assert [w.metrics.total_return for w in first.windows] == [
        w.metrics.total_return for w in second.windows
    ]


def test_multi_cycle_windows_actually_trade() -> None:
    """A walk-forward test over a series that never trades proves nothing."""
    bars = multi_cycle()
    splits = walk_forward_splits(
        list(bars["timestamp"]),
        train_bars=60,
        test_bars=100,
        scheme=SplitScheme.ROLLING,
        embargo_bars=0,
    )
    result = run_walk_forward(bars, EmaCrossEngine(), splits, bar_clock=CRYPTO_15M)
    assert result.total_trades > 0
