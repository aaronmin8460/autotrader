"""M1: temporal train/validation/test splitting, with purging and an embargo.

Time-series data cannot be split randomly. A shuffled split puts a Tuesday
afternoon in the training set and the Tuesday morning that produced it in the
test set, and the resulting score measures interpolation rather than
prediction. Every split here is a cut along the time axis, in order, and there
is no shuffle parameter to turn on.

**Ordering alone is not enough, because labels overlap.** A row stamped 15:00
whose label measures the next four bars does not become a fact until 16:00. If
the validation period begins at 15:15, that training row's outcome was decided
by prices inside the validation window - the model was fitted on the answer to
a question it is about to be graded on. Cutting on `feature_timestamp` does not
catch this; cutting on `label_knowable_at` does. That is *purging*: a training
row survives only if its label had fully resolved before the first validation
bar began.

**The embargo covers what purging cannot.** Even a fully resolved label sits
next to the boundary in a market with autocorrelated returns, so the last few
training rows carry information about the first validation rows through the
market itself rather than through their labels. `embargo_bars` drops that many
additional bars before each boundary. It is zero by default, because an
embargo is a modelling judgement rather than a correctness rule - purging is
the correctness rule, and it is not optional.

**Boundaries snap to whole sessions by default.** Half a session in training
and half in validation splits one continuous stretch of trading across the
boundary, and on an equity grid it also splits one overnight gap's worth of
context. Snapping moves the whole session into the earlier split. For crypto
the session is the UTC day, which is the natural place to cut a 24/7 series.

**Only labelled rows are split.** A row with no target cannot be trained on or
scored against, so the unlabelled tail a dataset deliberately keeps is reported
and excluded here rather than silently carried into a fold.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from autotrader.ml import MLError
from autotrader.ml.storage import sha256_of_record

#: The three splits, in time order. The names are part of the contract.
TRAIN = "train"
VALIDATION = "validation"
TEST = "test"
SPLIT_NAMES: tuple[str, ...] = (TRAIN, VALIDATION, TEST)

#: Columns a frame must carry to be splittable. All of them are written by
#: `autotrader.ml.dataset`; a frame without them is not a built dataset.
REQUIRED_COLUMNS: tuple[str, ...] = (
    "feature_timestamp",
    "knowable_at",
    "grid_index",
    "session_id",
    "label_knowable_at",
    "label_valid",
)


class SplitError(MLError):
    """A split that cannot be made, or a frame that cannot be split."""


@dataclass(frozen=True)
class SplitSpec:
    """How to cut one dataset into train, validation and test.

    The test fraction is the remainder, so the three always sum to one and
    there is no way to specify a set of fractions that does not.
    """

    train_fraction: float = 0.6
    validation_fraction: float = 0.2
    embargo_bars: int = 0
    snap_to_session: bool = True

    def __post_init__(self) -> None:
        for field, value in (
            ("train_fraction", self.train_fraction),
            ("validation_fraction", self.validation_fraction),
        ):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise SplitError(f"{field} must be a number, got {type(value).__name__}.")
            if not 0.0 < float(value) < 1.0:
                raise SplitError(f"{field} must be strictly between 0 and 1, got {value}.")
        if float(self.train_fraction) + float(self.validation_fraction) >= 1.0:
            raise SplitError(
                f"train_fraction ({self.train_fraction}) plus validation_fraction "
                f"({self.validation_fraction}) must leave room for a test set."
            )
        if isinstance(self.embargo_bars, bool) or not isinstance(self.embargo_bars, int):
            raise SplitError("embargo_bars must be an int.")
        if self.embargo_bars < 0:
            raise SplitError(f"embargo_bars must not be negative, got {self.embargo_bars}.")

    @property
    def test_fraction(self) -> float:
        """Whatever the other two leave."""
        return 1.0 - float(self.train_fraction) - float(self.validation_fraction)

    def to_record(self) -> dict[str, object]:
        """The serializable, fingerprinted form."""
        return {
            "train_fraction": float(self.train_fraction),
            "validation_fraction": float(self.validation_fraction),
            "test_fraction": self.test_fraction,
            "embargo_bars": self.embargo_bars,
            "snap_to_session": bool(self.snap_to_session),
        }

    @property
    def fingerprint(self) -> str:
        return sha256_of_record(self.to_record())


@dataclass(frozen=True)
class SplitPart:
    """One split's rows, and what was removed from it to keep the next one clean."""

    name: str
    frame: pd.DataFrame
    purged_rows: int
    embargoed_rows: int

    @property
    def row_count(self) -> int:
        return len(self.frame)

    @property
    def first_timestamp(self) -> pd.Timestamp | None:
        return None if self.frame.empty else self.frame["feature_timestamp"].iloc[0]

    @property
    def last_timestamp(self) -> pd.Timestamp | None:
        return None if self.frame.empty else self.frame["feature_timestamp"].iloc[-1]


@dataclass(frozen=True)
class TemporalSplit:
    """A dataset cut into three time-ordered parts, with the leakage guards applied."""

    spec: SplitSpec
    train: SplitPart
    validation: SplitPart
    test: SplitPart
    unlabelled_rows: int

    @property
    def parts(self) -> tuple[SplitPart, ...]:
        return (self.train, self.validation, self.test)

    def to_record(self) -> dict[str, object]:
        """The serializable summary an experiment record stores."""
        return {
            "specification": self.spec.to_record(),
            "unlabelled_rows_excluded": self.unlabelled_rows,
            "parts": [
                {
                    "name": part.name,
                    "rows": part.row_count,
                    "purged_rows": part.purged_rows,
                    "embargoed_rows": part.embargoed_rows,
                    "first_feature_timestamp": (
                        None if part.first_timestamp is None else part.first_timestamp.isoformat()
                    ),
                    "last_feature_timestamp": (
                        None if part.last_timestamp is None else part.last_timestamp.isoformat()
                    ),
                }
                for part in self.parts
            ],
        }


# --------------------------------------------------------------------------
# Preparation
# --------------------------------------------------------------------------


def _require_splittable(frame: pd.DataFrame) -> None:
    """Refuse a frame that is not a built dataset, or is not in time order."""
    if not isinstance(frame, pd.DataFrame):
        raise SplitError(f"Expected a DataFrame, got {type(frame).__name__}.")
    missing = [name for name in REQUIRED_COLUMNS if name not in frame.columns]
    if missing:
        raise SplitError(
            f"Frame is missing column(s) required to split it safely: {', '.join(missing)}."
        )
    if not frame["feature_timestamp"].is_monotonic_increasing:
        raise SplitError(
            "Rows must be ordered ascending by feature_timestamp. This module does "
            "not sort its input: a dataset that arrived out of order is an upstream "
            "fault, and quietly sorting it would hide one."
        )


def labelled_rows(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """The rows carrying a usable target, and how many were dropped."""
    mask = frame["label_valid"].fillna(False).to_numpy(dtype=bool)
    return frame.loc[mask].reset_index(drop=True), int((~mask).sum())


def _snap_forward(session_ids: Sequence[str], cut: int) -> int:
    """Move `cut` forward until it falls on a session boundary.

    Forward rather than backward so the session straddling the cut lands wholly
    in the *earlier* split. A model may train on a complete session it has
    already seen; it may not be graded on the tail of a session it trained on.
    """
    position = cut
    total = len(session_ids)
    while 0 < position < total and session_ids[position] == session_ids[position - 1]:
        position += 1
    return position


def _apply_guards(
    earlier: pd.DataFrame,
    later: pd.DataFrame,
    *,
    embargo_bars: int,
) -> tuple[pd.DataFrame, int, int]:
    """Purge and embargo `earlier` against the start of `later`.

    Purging compares `label_knowable_at` against the first feature bar of the
    later split: a row survives only if its label had entirely resolved before
    that bar began, so no training outcome was decided by data the later split
    is meant to be judged on. The embargo then removes a further `embargo_bars`
    grid positions immediately before that boundary.
    """
    if earlier.empty or later.empty:
        return earlier, 0, 0
    boundary_timestamp = later["feature_timestamp"].iloc[0]
    boundary_index = int(later["grid_index"].iloc[0])

    resolved = earlier["label_knowable_at"] <= boundary_timestamp
    purged = int((~resolved).sum())
    kept = earlier.loc[resolved.to_numpy(dtype=bool)]

    if embargo_bars > 0 and not kept.empty:
        outside = (boundary_index - kept["grid_index"].to_numpy(dtype="int64")) > embargo_bars
        embargoed = int((~outside).sum())
        kept = kept.loc[outside]
    else:
        embargoed = 0
    return kept.reset_index(drop=True), purged, embargoed


# --------------------------------------------------------------------------
# Splitting
# --------------------------------------------------------------------------


def temporal_split(frame: pd.DataFrame, spec: SplitSpec | None = None) -> TemporalSplit:
    """Cut `frame` into train, validation and test, in time order.

    The cuts are placed by row fraction, snapped to session boundaries when
    asked, and then guarded: training rows whose labels reach into validation
    are purged, validation rows whose labels reach into test are purged, and
    `embargo_bars` further bars are removed before each boundary.

    On a dataset too small to honour both the fractions and the snapping, the
    cuts are clamped to leave one row per part, and that clamp can land inside
    a session. Non-empty parts win over whole sessions there, because an empty
    validation set is a split that silently measures nothing - but it is a
    degenerate case, and a dataset that hits it is too short to train on.
    """
    specification = spec if spec is not None else SplitSpec()
    _require_splittable(frame)
    usable, unlabelled = labelled_rows(frame)
    total = len(usable)
    if total < len(SPLIT_NAMES):
        raise SplitError(
            f"Only {total} labelled row(s) available; a three-way temporal split "
            "needs at least one row per part."
        )

    train_cut = int(total * float(specification.train_fraction))
    validation_cut = int(
        total * (float(specification.train_fraction) + float(specification.validation_fraction))
    )
    if specification.snap_to_session:
        session_ids = list(usable["session_id"].astype("string"))
        train_cut = _snap_forward(session_ids, train_cut)
        validation_cut = _snap_forward(session_ids, max(validation_cut, train_cut))
    train_cut = max(1, min(train_cut, total - 2))
    validation_cut = max(train_cut + 1, min(validation_cut, total - 1))

    raw_train = usable.iloc[:train_cut].reset_index(drop=True)
    raw_validation = usable.iloc[train_cut:validation_cut].reset_index(drop=True)
    raw_test = usable.iloc[validation_cut:].reset_index(drop=True)

    train_frame, train_purged, train_embargoed = _apply_guards(
        raw_train, raw_validation, embargo_bars=specification.embargo_bars
    )
    validation_frame, validation_purged, validation_embargoed = _apply_guards(
        raw_validation, raw_test, embargo_bars=specification.embargo_bars
    )
    return TemporalSplit(
        spec=specification,
        train=SplitPart(TRAIN, train_frame, train_purged, train_embargoed),
        validation=SplitPart(VALIDATION, validation_frame, validation_purged, validation_embargoed),
        test=SplitPart(TEST, raw_test, 0, 0),
        unlabelled_rows=unlabelled,
    )


def assert_no_leakage(split: TemporalSplit) -> None:
    """Refuse a split whose parts overlap in time or through their labels.

    Cheap enough to run every time, and it checks the property the whole module
    exists for rather than trusting that the arithmetic above stayed correct.
    """
    ordered = [part for part in split.parts if not part.frame.empty]
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        earlier_last_feature = earlier.frame["feature_timestamp"].max()
        later_first_feature = later.frame["feature_timestamp"].min()
        if earlier_last_feature >= later_first_feature:
            raise SplitError(
                f"{earlier.name} ends at {earlier_last_feature.isoformat()} but "
                f"{later.name} begins at {later_first_feature.isoformat()}: the parts "
                "overlap in time."
            )
        latest_label = earlier.frame["label_knowable_at"].max()
        if pd.notna(latest_label) and latest_label > later_first_feature:
            raise SplitError(
                f"A {earlier.name} label does not resolve until "
                f"{latest_label.isoformat()}, which is inside {later.name} "
                f"(begins {later_first_feature.isoformat()}). Purging failed."
            )


@dataclass(frozen=True)
class WalkForwardFold:
    """One anchored walk-forward fold: everything up to a point, then what follows."""

    index: int
    train: SplitPart
    test: SplitPart


def walk_forward_folds(
    frame: pd.DataFrame,
    *,
    folds: int,
    initial_train_fraction: float = 0.5,
    embargo_bars: int = 0,
) -> tuple[WalkForwardFold, ...]:
    """Anchored walk-forward folds over a built dataset.

    Fold *k* trains on everything from the start of the data up to a moving
    boundary and tests on the stretch that follows it, so every fold trains on
    the past and is graded on its own future. The training window is anchored
    at the beginning rather than rolling, because discarding early history is a
    modelling choice that should be made deliberately rather than by the shape
    of a helper.

    Each fold gets the same purge and embargo `temporal_split` applies. This is
    the primitive V4's model-selection evidence is meant to be produced from;
    it deliberately fits nothing and scores nothing itself.
    """
    if isinstance(folds, bool) or not isinstance(folds, int) or folds < 1:
        raise SplitError(f"folds must be a positive int, got {folds!r}.")
    if not 0.0 < float(initial_train_fraction) < 1.0:
        raise SplitError(
            f"initial_train_fraction must be strictly between 0 and 1, got "
            f"{initial_train_fraction}."
        )
    if isinstance(embargo_bars, bool) or not isinstance(embargo_bars, int) or embargo_bars < 0:
        raise SplitError(f"embargo_bars must be a non-negative int, got {embargo_bars!r}.")

    _require_splittable(frame)
    usable, _ = labelled_rows(frame)
    total = len(usable)
    start = int(total * float(initial_train_fraction))
    if start < 1 or total - start < folds:
        raise SplitError(
            f"{total} labelled row(s) cannot yield {folds} walk-forward fold(s) after "
            f"an initial training window of {start} row(s)."
        )

    edges = np.linspace(start, total, folds + 1).astype(int)
    built: list[WalkForwardFold] = []
    for position in range(folds):
        train_end, test_end = int(edges[position]), int(edges[position + 1])
        if test_end <= train_end:
            continue
        raw_train = usable.iloc[:train_end].reset_index(drop=True)
        raw_test = usable.iloc[train_end:test_end].reset_index(drop=True)
        train_frame, purged, embargoed = _apply_guards(
            raw_train, raw_test, embargo_bars=embargo_bars
        )
        built.append(
            WalkForwardFold(
                index=position,
                train=SplitPart(TRAIN, train_frame, purged, embargoed),
                test=SplitPart(TEST, raw_test, 0, 0),
            )
        )
    return tuple(built)


__all__ = [
    "REQUIRED_COLUMNS",
    "SPLIT_NAMES",
    "TEST",
    "TRAIN",
    "VALIDATION",
    "SplitError",
    "SplitPart",
    "SplitSpec",
    "TemporalSplit",
    "WalkForwardFold",
    "assert_no_leakage",
    "labelled_rows",
    "temporal_split",
    "walk_forward_folds",
]
