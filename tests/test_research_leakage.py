"""Leakage detection, proved by injecting the leaks it is supposed to catch.

The structure of this file is deliberate. For every protection there are two
tests: one that a deliberately leaking construct is **caught**, and one that a
legitimate construct is **not** flagged. A detector with only the first kind of
test can pass by rejecting everything; a detector with only the second can pass
by rejecting nothing.

The leaking engines and features below are written to be realistic mistakes -
a negative shift, a centered window, a normalization fitted over the whole
series, a backfill, a forward-looking label - rather than obviously absurd
ones, because those are the forms this actually takes in practice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from autotrader.research.engines import Action, EmaCrossEngine, ParametricEmaCross, ResearchSignal
from autotrader.research.leakage import (
    CROSS_WINDOW_TEST_OVERLAP,
    DUPLICATE_TIMESTAMP,
    EMBARGO_TOO_SHORT,
    FUTURE_FEATURE_DEPENDENCE,
    FUTURE_SIGNAL_DEPENDENCE,
    HOLDOUT_NOT_LAST,
    HOLDOUT_USED_IN_SELECTION,
    INCOMPLETE_FINAL_BAR,
    NO_EMBARGO,
    TRAIN_TEST_OVERLAP,
    UNORDERED_TIMESTAMPS,
    WARMUP_LONGER_THAN_WINDOW,
    LeakageError,
    audit_bar_completeness,
    audit_engine_causality,
    audit_feature_causality,
    audit_holdout,
    audit_splits,
    audit_study,
    audit_timestamps,
    audit_warmup,
    perturb_after,
    probe_indices,
    require_causal_engine,
)
from autotrader.research.splits import (
    HoldoutSplit,
    SplitScheme,
    TimeSplit,
    holdout_split,
    walk_forward_splits,
)
from research_fixtures import BAR_INTERVAL, wave

BARS = wave(400)
TIMESTAMPS = list(BARS["timestamp"])


# ==========================================================================
# Leaking engines: each is a realistic mistake, not a strawman
# ==========================================================================


class NextBarOracle:
    """Reads the *next* bar's close. The canonical look-ahead bug.

    Written the way it actually appears: a `shift(-1)` that looks like the
    `shift(1)` beside it.
    """

    name = "next-bar-oracle"
    version = "v1"
    parameters: dict[str, object] = {}
    warmup_bars = 1

    def generate(self, bars: pd.DataFrame) -> tuple[ResearchSignal, ...]:
        close = bars["close"].astype("float64")
        tomorrow = close.shift(-1)
        signals = []
        for position, timestamp in enumerate(bars["timestamp"]):
            future = tomorrow.iat[position]
            if pd.isna(future):
                continue
            if future > close.iat[position] * 1.02:
                signals.append(
                    ResearchSignal(timestamp, str(bars["symbol"].iat[0]), Action.ENTER_LONG, "UP")
                )
            elif future < close.iat[position] * 0.98:
                signals.append(
                    ResearchSignal(timestamp, str(bars["symbol"].iat[0]), Action.EXIT_LONG, "DOWN")
                )
        return tuple(signals)


class GlobalNormalizationEngine:
    """Normalizes against statistics fitted over the **whole** series.

    Subtler than a shift: no bar is read out of order, but the mean and
    deviation every bar is scored against were computed from data that had not
    arrived yet. This is the leak that survives code review.
    """

    name = "global-normalization"
    version = "v1"
    parameters: dict[str, object] = {}
    warmup_bars = 20

    def generate(self, bars: pd.DataFrame) -> tuple[ResearchSignal, ...]:
        close = bars["close"].astype("float64")
        standardized = (close - close.mean()) / (close.std() or 1.0)
        signals = []
        for position, timestamp in enumerate(bars["timestamp"]):
            if standardized.iat[position] > 1.0:
                signals.append(
                    ResearchSignal(timestamp, str(bars["symbol"].iat[0]), Action.ENTER_LONG, "HIGH")
                )
        return tuple(signals)


class CenteredWindowEngine:
    """A centered rolling mean: half its window is in the future."""

    name = "centered-window"
    version = "v1"
    parameters: dict[str, object] = {}
    warmup_bars = 20

    def generate(self, bars: pd.DataFrame) -> tuple[ResearchSignal, ...]:
        close = bars["close"].astype("float64")
        smooth = close.rolling(21, center=True, min_periods=21).mean()
        signals = []
        for position, timestamp in enumerate(bars["timestamp"]):
            value = smooth.iat[position]
            if pd.isna(value):
                continue
            if close.iat[position] > value * 1.01:
                signals.append(
                    ResearchSignal(timestamp, str(bars["symbol"].iat[0]), Action.ENTER_LONG, "C")
                )
        return tuple(signals)


# ==========================================================================
# Engine causality
# ==========================================================================


def test_the_production_engine_passes_the_causality_audit() -> None:
    """The control. A detector that flags a causal engine is useless."""
    report = audit_engine_causality(EmaCrossEngine(), BARS)
    assert report.clean, report.codes
    assert report.probes > 0, "a clean report with no probes checked nothing"


def test_a_parametric_engine_passes_the_causality_audit() -> None:
    report = audit_engine_causality(ParametricEmaCross(fast_period=5, slow_period=15), BARS)
    assert report.clean, report.codes


def test_a_next_bar_oracle_is_caught() -> None:
    """CRITICAL. The most direct form of look-ahead there is."""
    report = audit_engine_causality(NextBarOracle(), BARS)
    assert not report.clean
    assert FUTURE_SIGNAL_DEPENDENCE in report.codes


def test_globally_fitted_normalization_is_caught() -> None:
    """CRITICAL. No bar is read out of order, yet the future is still used."""
    report = audit_engine_causality(GlobalNormalizationEngine(), BARS)
    assert not report.clean
    assert FUTURE_SIGNAL_DEPENDENCE in report.codes


def test_a_centered_rolling_window_is_caught() -> None:
    report = audit_engine_causality(CenteredWindowEngine(), BARS)
    assert not report.clean
    assert FUTURE_SIGNAL_DEPENDENCE in report.codes


def test_the_strict_wrapper_raises_on_a_leaking_engine() -> None:
    with pytest.raises(LeakageError, match="FUTURE_SIGNAL_DEPENDENCE"):
        require_causal_engine(NextBarOracle(), BARS)


def test_the_strict_wrapper_is_silent_on_a_causal_engine() -> None:
    require_causal_engine(EmaCrossEngine(), BARS)


def test_an_engine_that_emits_nothing_cannot_be_shown_to_leak() -> None:
    """A real limit of perturbation testing, pinned so nobody mistakes a clean
    report on a silent engine for evidence that the engine is causal.

    The engine below reads the future exactly as `CenteredWindowEngine` does,
    but its threshold is never met, so it emits no signal and there is nothing
    for the audit to compare. The report is clean and means nothing - which is
    why `LeakageReport` carries its probe count, and why an engine's signals
    are audited rather than its source.
    """

    class SilentButLeaking(CenteredWindowEngine):
        def generate(self, bars: pd.DataFrame) -> tuple[ResearchSignal, ...]:
            return ()

    report = audit_engine_causality(SilentButLeaking(), BARS)
    assert report.clean
    assert report.probes > 0


def test_more_probes_check_more_of_the_series() -> None:
    """A clean report's strength is proportional to how hard it looked."""
    light = audit_engine_causality(EmaCrossEngine(), BARS, probes=2)
    heavy = audit_engine_causality(EmaCrossEngine(), BARS, probes=9)
    assert heavy.probes > light.probes
    assert light.clean and heavy.clean


# ==========================================================================
# Feature causality
# ==========================================================================


def causal_ema(bars: pd.DataFrame) -> pd.Series:
    """A legitimate, backward-looking feature."""
    return bars["close"].astype("float64").ewm(span=20, adjust=False, min_periods=20).mean()


def test_a_backward_looking_feature_passes() -> None:
    report = audit_feature_causality(causal_ema, BARS)
    assert report.clean, report.codes
    assert report.probes > 0


def test_a_negative_shift_is_caught() -> None:
    report = audit_feature_causality(lambda bars: bars["close"].astype("float64").shift(-1), BARS)
    assert FUTURE_FEATURE_DEPENDENCE in report.codes


def test_a_centered_rolling_feature_is_caught() -> None:
    report = audit_feature_causality(
        lambda bars: bars["close"].astype("float64").rolling(11, center=True).mean(), BARS
    )
    assert FUTURE_FEATURE_DEPENDENCE in report.codes


def test_a_globally_fitted_zscore_is_caught() -> None:
    def leaking(bars: pd.DataFrame) -> pd.Series:
        close = bars["close"].astype("float64")
        return (close - close.mean()) / close.std()

    assert FUTURE_FEATURE_DEPENDENCE in audit_feature_causality(leaking, BARS).codes


def test_a_backfilled_column_is_caught() -> None:
    """`bfill` copies a later value backwards. It is leakage spelled as tidying."""

    def leaking(bars: pd.DataFrame) -> pd.Series:
        close = bars["close"].astype("float64").copy()
        close.iloc[::7] = float("nan")
        return close.bfill()

    assert FUTURE_FEATURE_DEPENDENCE in audit_feature_causality(leaking, BARS).codes


def test_a_forward_looking_label_is_caught() -> None:
    """The label side of the same mistake: a target built from the future."""

    def forward_return(bars: pd.DataFrame) -> pd.Series:
        close = bars["close"].astype("float64")
        return close.shift(-10) / close - 1.0

    assert FUTURE_FEATURE_DEPENDENCE in audit_feature_causality(forward_return, BARS).codes


def test_a_cumulative_maximum_of_the_whole_series_is_caught() -> None:
    """Expanding backwards - `max()` over everything - is a whole-series read."""

    def leaking(bars: pd.DataFrame) -> pd.Series:
        close = bars["close"].astype("float64")
        return close / close.max()

    assert FUTURE_FEATURE_DEPENDENCE in audit_feature_causality(leaking, BARS).codes


def test_an_expanding_maximum_is_not_flagged() -> None:
    """The causal counterpart of the previous test: `expanding().max()` reads
    only the past, and must pass."""

    def causal(bars: pd.DataFrame) -> pd.Series:
        close = bars["close"].astype("float64")
        return close / close.expanding().max()

    assert audit_feature_causality(causal, BARS).clean


def test_a_warmup_nan_is_not_mistaken_for_a_difference() -> None:
    """NaN compares equal to NaN here; otherwise every warm-up mask would be
    reported as leakage and the detector would be unusable."""
    assert audit_feature_causality(causal_ema, BARS).clean


# ==========================================================================
# Perturbation mechanics
# ==========================================================================


def test_perturbation_changes_only_bars_after_the_probe() -> None:
    perturbed = perturb_after(BARS, 100)
    pd.testing.assert_frame_equal(BARS.iloc[:101], perturbed.iloc[:101])
    assert not BARS.iloc[101:]["close"].equals(perturbed.iloc[101:]["close"])


def test_perturbation_keeps_the_ohlc_relationships_valid() -> None:
    """A probe must not be rejected as malformed data by the very validator the
    engine under test runs behind."""
    from autotrader.data.validation import validate_frame

    perturbed = perturb_after(BARS, 150)
    assert validate_frame(perturbed).valid


def test_probe_indices_are_interior_and_ordered() -> None:
    indices = probe_indices(400, 5)
    assert len(indices) == 5
    assert list(indices) == sorted(indices)
    assert all(0 < index < 399 for index in indices)


def test_a_series_too_short_to_probe_yields_no_probes() -> None:
    assert probe_indices(2) == ()
    assert probe_indices(0) == ()


# ==========================================================================
# Structural: shuffling and ordering
# ==========================================================================


def test_a_shuffled_index_is_caught() -> None:
    """CRITICAL. A random split is the classic time-series mistake, and it
    presents as an out-of-order timestamp column."""
    shuffled = BARS.sample(frac=1.0, random_state=0).reset_index(drop=True)
    report = audit_timestamps(list(shuffled["timestamp"]))
    assert UNORDERED_TIMESTAMPS in report.codes


def test_ordered_timestamps_pass() -> None:
    assert audit_timestamps(TIMESTAMPS).clean


def test_a_duplicated_instant_is_caught() -> None:
    """One instant in two windows at once."""
    duplicated = TIMESTAMPS[:100] + [TIMESTAMPS[99]] + TIMESTAMPS[100:]
    assert DUPLICATE_TIMESTAMP in audit_timestamps(duplicated).codes


# ==========================================================================
# Structural: splits
# ==========================================================================


def clean_splits(embargo: int = 20) -> tuple[TimeSplit, ...]:
    return walk_forward_splits(
        TIMESTAMPS,
        train_bars=150,
        test_bars=50,
        scheme=SplitScheme.ROLLING,
        embargo_bars=embargo,
    )


def test_well_formed_walk_forward_splits_pass() -> None:
    report = audit_splits(clean_splits(), required_embargo=20)
    assert report.clean, report.codes


def test_an_overlapping_train_and_test_window_is_caught() -> None:
    """Constructed by hand, because `walk_forward_splits` cannot produce one -
    which is the point: the generator refuses, and the auditor still checks."""
    overlapping = TimeSplit(
        index=0,
        train_start=0,
        train_end=100,
        test_start=100,
        test_end=200,
        embargo_bars=0,
        train_start_timestamp=TIMESTAMPS[0],
        train_end_timestamp=TIMESTAMPS[99],
        test_start_timestamp=TIMESTAMPS[100],
        test_end_timestamp=TIMESTAMPS[199],
    )
    object.__setattr__(overlapping, "test_start", 50)
    report = audit_splits((overlapping,), required_embargo=0)
    assert TRAIN_TEST_OVERLAP in report.codes


def test_a_test_window_before_its_training_data_is_caught() -> None:
    backwards = TimeSplit(
        index=0,
        train_start=100,
        train_end=200,
        test_start=200,
        test_end=250,
        embargo_bars=0,
        train_start_timestamp=TIMESTAMPS[100],
        train_end_timestamp=TIMESTAMPS[199],
        test_start_timestamp=TIMESTAMPS[200],
        test_end_timestamp=TIMESTAMPS[249],
    )
    object.__setattr__(backwards, "test_start", 20)
    object.__setattr__(backwards, "test_end", 60)
    assert "TEST_BEFORE_TRAIN" in audit_splits((backwards,), required_embargo=0).codes


def test_an_embargo_shorter_than_the_feature_lookback_is_caught() -> None:
    """CRITICAL. Non-overlapping windows are not enough: a 50-bar indicator at
    the test window's first bar still reads 50 training bars."""
    report = audit_splits(clean_splits(embargo=5), required_embargo=50)
    assert EMBARGO_TOO_SHORT in report.codes


def test_no_declared_embargo_is_reported_rather_than_passing_silently() -> None:
    """ "We did not think about it" and "we determined none was needed" produce
    the same split object; they must not produce the same report."""
    report = audit_splits(clean_splits(embargo=0), required_embargo=0)
    assert NO_EMBARGO in report.codes


def test_overlapping_test_windows_are_caught() -> None:
    """Scoring the same bar twice makes an average over windows look like an
    average over independent samples."""
    overlapping = walk_forward_splits(
        TIMESTAMPS,
        train_bars=150,
        test_bars=50,
        scheme=SplitScheme.ROLLING,
        embargo_bars=0,
        step_bars=10,
    )
    assert CROSS_WINDOW_TEST_OVERLAP in audit_splits(overlapping).codes


def test_default_stepping_produces_disjoint_test_windows() -> None:
    report = audit_splits(clean_splits(), required_embargo=20)
    assert CROSS_WINDOW_TEST_OVERLAP not in report.codes


# ==========================================================================
# Structural: the final holdout
# ==========================================================================


def test_a_clean_holdout_passes() -> None:
    holdout = holdout_split(TIMESTAMPS, holdout_bars=80, embargo_bars=20)
    splits = walk_forward_splits(
        TIMESTAMPS[: holdout.study_end],
        train_bars=100,
        test_bars=40,
        scheme=SplitScheme.ROLLING,
        embargo_bars=20,
    )
    report = audit_holdout(holdout, total_bars=len(TIMESTAMPS), selection_splits=splits)
    assert report.clean, report.codes


def test_selecting_against_the_final_holdout_is_caught() -> None:
    """CRITICAL. This is the failure that turns an out-of-sample number into a
    training score without anything looking wrong."""
    holdout = holdout_split(TIMESTAMPS, holdout_bars=80, embargo_bars=20)
    # Windows generated over the *whole* series rather than the study region:
    # the last of them reaches into the holdout.
    reaching = walk_forward_splits(
        TIMESTAMPS,
        train_bars=100,
        test_bars=40,
        scheme=SplitScheme.ROLLING,
        embargo_bars=0,
    )
    report = audit_holdout(holdout, total_bars=len(TIMESTAMPS), selection_splits=reaching)
    assert HOLDOUT_USED_IN_SELECTION in report.codes


def test_a_holdout_that_is_not_the_final_stretch_is_caught() -> None:
    misplaced = HoldoutSplit(
        study_start=0,
        study_end=100,
        holdout_start=120,
        holdout_end=200,
        embargo_bars=20,
        study_end_timestamp=TIMESTAMPS[99],
        holdout_start_timestamp=TIMESTAMPS[120],
    )
    report = audit_holdout(misplaced, total_bars=len(TIMESTAMPS))
    assert HOLDOUT_NOT_LAST in report.codes


# ==========================================================================
# Warm-up
# ==========================================================================


def test_a_window_shorter_than_the_engine_warmup_is_caught() -> None:
    splits = walk_forward_splits(
        TIMESTAMPS, train_bars=30, test_bars=10, scheme=SplitScheme.ROLLING
    )
    assert WARMUP_LONGER_THAN_WINDOW in audit_warmup(splits, warmup_bars=50).codes


def test_a_window_longer_than_the_warmup_passes() -> None:
    assert audit_warmup(clean_splits(), warmup_bars=50).clean


# ==========================================================================
# Incomplete bars
# ==========================================================================


def test_an_incomplete_final_bar_is_caught() -> None:
    """A bar stamped at its open is not final until its duration has elapsed.
    Acting on it trades a close that has not happened."""
    last_open = TIMESTAMPS[-1].to_pydatetime()
    report = audit_bar_completeness(
        BARS,
        bar_duration=BAR_INTERVAL,
        # Five minutes into a fifteen-minute bar: it is still forming.
        as_of=last_open + timedelta(minutes=5),
    )
    assert INCOMPLETE_FINAL_BAR in report.codes


def test_a_closed_final_bar_passes() -> None:
    last_open = TIMESTAMPS[-1].to_pydatetime()
    report = audit_bar_completeness(
        BARS, bar_duration=BAR_INTERVAL, as_of=last_open + timedelta(minutes=15)
    )
    assert report.clean


def test_an_empty_dataset_reports_no_completeness_finding() -> None:
    empty = BARS.iloc[:0]
    assert audit_bar_completeness(
        empty, bar_duration=BAR_INTERVAL, as_of=datetime(2030, 1, 1, tzinfo=UTC)
    ).clean


# ==========================================================================
# The combined study audit
# ==========================================================================


def test_a_sound_study_configuration_is_clean() -> None:
    holdout = holdout_split(TIMESTAMPS, holdout_bars=80, embargo_bars=20)
    splits = walk_forward_splits(
        TIMESTAMPS[: holdout.study_end],
        train_bars=120,
        # At least the engine's 50-bar warm-up, or `audit_warmup` correctly
        # reports that the window is too short to evaluate over.
        test_bars=60,
        scheme=SplitScheme.ROLLING,
        embargo_bars=20,
    )
    report = audit_study(
        engine=EmaCrossEngine(),
        bars=BARS,
        splits=splits,
        holdout=holdout,
        required_embargo=20,
    )
    assert report.clean, report.codes
    assert "engine_causality" in report.checks
    assert "splits" in report.checks


def test_a_study_with_a_leaking_engine_is_not_clean() -> None:
    holdout = holdout_split(TIMESTAMPS, holdout_bars=80, embargo_bars=20)
    splits = walk_forward_splits(
        TIMESTAMPS[: holdout.study_end],
        train_bars=120,
        test_bars=60,
        scheme=SplitScheme.ROLLING,
        embargo_bars=20,
    )
    report = audit_study(
        engine=NextBarOracle(),
        bars=BARS,
        splits=splits,
        holdout=holdout,
        required_embargo=20,
    )
    assert FUTURE_SIGNAL_DEPENDENCE in report.codes


def test_a_report_serializes_its_findings() -> None:
    report = audit_engine_causality(NextBarOracle(), BARS)
    document = report.to_json_dict()
    assert document["clean"] is False
    assert document["findings"]
    assert document["findings"][0]["code"] == FUTURE_SIGNAL_DEPENDENCE
