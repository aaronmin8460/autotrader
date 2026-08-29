"""Time-series splits: train and test windows that respect the arrow of time.

A random train/test split is the standard tool for independent samples and the
standard way to destroy a time-series result. Bars are ordered and correlated:
training on Wednesday to predict Tuesday is not cross-validation, it is reading
the answer. Every split this module produces is contiguous and strictly
ordered, and there is deliberately **no** shuffling parameter anywhere in it -
not defaulted to off, not present. A knob that must never be turned should not
exist.

**Walk-forward.** The train window is fitted, the test window that immediately
follows it is evaluated, and both then step forward. Two schemes:

``ROLLING``   fixed-length train window; the distant past drops out.
``ANCHORED``  train window grows from a fixed origin; nothing is forgotten.

Neither is correct in general. Rolling assumes the recent past is more relevant;
anchored assumes more data is better. Which one a study uses is a modelling
claim, so it is a required argument rather than a default.

**The embargo.** A gap of bars between train end and test start. Without it, a
feature with a lookback of *k* bars computed at the start of the test window is
partly a function of the last *k* training bars, and a label whose horizon is
*h* bars at the end of the training window resolves inside the test window.
Both are leakage that no amount of "the windows do not overlap" prevents,
because the windows not overlapping is exactly what people check instead. The
embargo must be at least as long as the longer of the feature lookback and the
label horizon; this module cannot know either, so it takes the number and the
leakage auditor checks that one was supplied.

**The final holdout is not one of these windows.** `holdout_split` carves the
last stretch of the dataset off *before* any walk-forward split is generated,
and nothing that selects a parameter set may see it. That separation is what
makes a single, honest out-of-sample number possible at the end of a study, and
`autotrader.research.leakage` enforces it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

import pandas as pd


class SplitError(Exception):
    """A split was requested that cannot be produced, or would not be sound."""


class SplitScheme(Enum):
    """How the train window moves as the study walks forward."""

    ROLLING = "ROLLING"
    ANCHORED = "ANCHORED"


@dataclass(frozen=True)
class TimeSplit:
    """One train/test window pair, as half-open index ranges.

    All four indices are positions into the timestamp sequence the split was
    generated from, and every range is half-open: ``[start, end)``. The
    timestamps are carried alongside so a stored split record identifies actual
    instants rather than positions into a dataset nobody kept.

    The invariant this type exists to make checkable:
    ``train_end <= test_start - embargo_bars``, and ``test_start > train_end``
    always. A `TimeSplit` that violates it cannot be constructed.
    """

    index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    embargo_bars: int
    train_start_timestamp: pd.Timestamp
    train_end_timestamp: pd.Timestamp
    test_start_timestamp: pd.Timestamp
    test_end_timestamp: pd.Timestamp

    def __post_init__(self) -> None:
        if self.train_start < 0 or self.train_end <= self.train_start:
            raise SplitError(
                f"Split {self.index} has an empty or negative train window "
                f"[{self.train_start}, {self.train_end})."
            )
        if self.test_end <= self.test_start:
            raise SplitError(
                f"Split {self.index} has an empty test window [{self.test_start}, {self.test_end})."
            )
        if self.test_start < self.train_end:
            raise SplitError(
                f"Split {self.index} tests at {self.test_start} which is before its train "
                f"window ends at {self.train_end}. A test window must follow its training "
                "data; this is the defining property of a time-series split."
            )
        gap = self.test_start - self.train_end
        if gap < self.embargo_bars:
            raise SplitError(
                f"Split {self.index} leaves {gap} bar(s) between train and test but declares "
                f"an embargo of {self.embargo_bars}."
            )

    @property
    def train_length(self) -> int:
        return self.train_end - self.train_start

    @property
    def test_length(self) -> int:
        return self.test_end - self.test_start

    @property
    def gap(self) -> int:
        """Bars actually left between the train and test windows."""
        return self.test_start - self.train_end

    def train_slice(self, frame: pd.DataFrame) -> pd.DataFrame:
        """The training rows of `frame`, as a copy.

        A copy rather than a view, so a caller that mutates what it was handed
        cannot reach back into the full dataset the next window will read.
        """
        return frame.iloc[self.train_start : self.train_end].copy()

    def test_slice(self, frame: pd.DataFrame) -> pd.DataFrame:
        """The test rows of `frame`, as a copy."""
        return frame.iloc[self.test_start : self.test_end].copy()

    def to_json_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "train_start": self.train_start,
            "train_end": self.train_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "embargo_bars": self.embargo_bars,
            "train_start_timestamp": str(self.train_start_timestamp),
            "train_end_timestamp": str(self.train_end_timestamp),
            "test_start_timestamp": str(self.test_start_timestamp),
            "test_end_timestamp": str(self.test_end_timestamp),
        }


def require_ordered_timestamps(timestamps: Sequence[pd.Timestamp]) -> None:
    """Reject a timestamp sequence that is unsorted or holds duplicates.

    Splitting an unsorted series produces windows that are contiguous in
    position and scrambled in time, which is a shuffled split wearing a
    walk-forward costume. Duplicates make "the bar at index i" ambiguous and
    let the same instant land in both train and test.
    """
    if len(timestamps) < 2:
        return
    previous = timestamps[0]
    for position, current in enumerate(timestamps[1:], start=1):
        if current < previous:
            raise SplitError(
                f"Timestamps are not ascending: index {position} ({current}) precedes "
                f"index {position - 1} ({previous}). Splits over unordered bars are not "
                "time-series splits."
            )
        if current == previous:
            raise SplitError(
                f"Duplicate timestamp {current} at index {position}. A duplicated instant "
                "can fall in both a train and a test window."
            )
        previous = current


def walk_forward_splits(
    timestamps: Sequence[pd.Timestamp],
    *,
    train_bars: int,
    test_bars: int,
    scheme: SplitScheme,
    embargo_bars: int = 0,
    step_bars: int | None = None,
) -> tuple[TimeSplit, ...]:
    """Generate walk-forward windows over `timestamps`.

    `step_bars` defaults to `test_bars`, which produces **non-overlapping test
    windows** - every bar is tested at most once, so the out-of-sample record is
    a partition of the data rather than a resampling of it. A smaller step
    overlaps test windows and inflates the apparent sample size; it is
    available because it is sometimes wanted, and it is not the default.

    The windows are laid out from the start of the data forward. A trailing
    remainder too short for a full test window is dropped rather than being
    tested as a short window, because a final window of a different length is
    not comparable to the others and would be averaged in as though it were.

    Raises `SplitError` when the parameters cannot produce at least one window.
    """
    require_ordered_timestamps(timestamps)
    total = len(timestamps)

    for label, value in (
        ("train_bars", train_bars),
        ("test_bars", test_bars),
        ("embargo_bars", embargo_bars),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise SplitError(f"{label} must be an int, got {value!r}.")
    if train_bars < 1:
        raise SplitError(f"train_bars must be at least 1, got {train_bars}.")
    if test_bars < 1:
        raise SplitError(f"test_bars must be at least 1, got {test_bars}.")
    if embargo_bars < 0:
        raise SplitError(f"embargo_bars must not be negative, got {embargo_bars}.")

    step = test_bars if step_bars is None else step_bars
    if not isinstance(step, int) or isinstance(step, bool) or step < 1:
        raise SplitError(f"step_bars must be a positive int, got {step_bars!r}.")

    minimum = train_bars + embargo_bars + test_bars
    if total < minimum:
        raise SplitError(
            f"{total} bars cannot produce a {train_bars}-bar train window, a "
            f"{embargo_bars}-bar embargo and a {test_bars}-bar test window; "
            f"{minimum} bars are needed."
        )

    splits: list[TimeSplit] = []
    origin = 0
    train_end = train_bars
    while True:
        test_start = train_end + embargo_bars
        test_end = test_start + test_bars
        if test_end > total:
            break
        train_start = origin if scheme is SplitScheme.ANCHORED else train_end - train_bars
        splits.append(
            TimeSplit(
                index=len(splits),
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                embargo_bars=embargo_bars,
                train_start_timestamp=timestamps[train_start],
                train_end_timestamp=timestamps[train_end - 1],
                test_start_timestamp=timestamps[test_start],
                test_end_timestamp=timestamps[test_end - 1],
            )
        )
        train_end += step

    if not splits:  # pragma: no cover - the length check above already refuses
        raise SplitError("No walk-forward window could be generated.")
    return tuple(splits)


@dataclass(frozen=True)
class HoldoutSplit:
    """The study region and the final holdout, separated once and for all.

    `study_end` and `holdout_start` are separated by `embargo_bars`, and the
    bars in the gap belong to neither: they are burned on purpose so that no
    feature computed at the holdout's first bar can reach back into data a
    parameter was selected on.
    """

    study_start: int
    study_end: int
    holdout_start: int
    holdout_end: int
    embargo_bars: int
    study_end_timestamp: pd.Timestamp
    holdout_start_timestamp: pd.Timestamp

    @property
    def study_length(self) -> int:
        return self.study_end - self.study_start

    @property
    def holdout_length(self) -> int:
        return self.holdout_end - self.holdout_start

    def study_slice(self, frame: pd.DataFrame) -> pd.DataFrame:
        """The rows a study may select parameters on."""
        return frame.iloc[self.study_start : self.study_end].copy()

    def holdout_slice(self, frame: pd.DataFrame) -> pd.DataFrame:
        """The rows nothing may select against. Read exactly once, at the end."""
        return frame.iloc[self.holdout_start : self.holdout_end].copy()

    def to_json_dict(self) -> dict[str, object]:
        return {
            "study_start": self.study_start,
            "study_end": self.study_end,
            "holdout_start": self.holdout_start,
            "holdout_end": self.holdout_end,
            "embargo_bars": self.embargo_bars,
            "study_end_timestamp": str(self.study_end_timestamp),
            "holdout_start_timestamp": str(self.holdout_start_timestamp),
        }


def holdout_split(
    timestamps: Sequence[pd.Timestamp],
    *,
    holdout_bars: int,
    embargo_bars: int = 0,
) -> HoldoutSplit:
    """Carve the last `holdout_bars` bars off as an untouchable final holdout.

    Called **first**, before any walk-forward split is generated, and the
    walk-forward splits are then generated over the study region only. Doing it
    the other way around - selecting on everything and then testing on the tail
    - is the single most common way a backtest reports an out-of-sample number
    that is nothing of the sort.
    """
    require_ordered_timestamps(timestamps)
    total = len(timestamps)
    if holdout_bars < 1:
        raise SplitError(f"holdout_bars must be at least 1, got {holdout_bars}.")
    if embargo_bars < 0:
        raise SplitError(f"embargo_bars must not be negative, got {embargo_bars}.")
    if holdout_bars + embargo_bars >= total:
        raise SplitError(
            f"A {holdout_bars}-bar holdout with a {embargo_bars}-bar embargo leaves no study "
            f"region in {total} bars."
        )

    holdout_start = total - holdout_bars
    study_end = holdout_start - embargo_bars
    return HoldoutSplit(
        study_start=0,
        study_end=study_end,
        holdout_start=holdout_start,
        holdout_end=total,
        embargo_bars=embargo_bars,
        study_end_timestamp=timestamps[study_end - 1],
        holdout_start_timestamp=timestamps[holdout_start],
    )


__all__ = [
    "HoldoutSplit",
    "SplitError",
    "SplitScheme",
    "TimeSplit",
    "holdout_split",
    "require_ordered_timestamps",
    "walk_forward_splits",
]
