"""Leakage detection: proving a result could have been produced in real time.

Every backtest is a claim that the strategy could have been run live. Leakage
is any way that claim is false - the simulation used information that did not
exist yet. It never announces itself: a leaking backtest looks like a very good
backtest, which is precisely why it survives review.

This module makes leakage *checkable* rather than merely forbidden. Each
protection below is a function that returns findings, and each has a test that
deliberately injects the corresponding defect and proves the check catches it.
A rule with no failing case behind it is a comment.

**What is checked, and how.**

*Structural* checks read a split definition and need no engine: ordering,
overlap, embargo, duplicated instants, cross-window contamination, and whether
parameter selection was allowed to see the final holdout. These are cheap and
exhaustive.

*Behavioural* checks are the interesting ones, and they work by **perturbation**.
To ask "does this feature depend on the future?", compute it over the data,
then change bars *after* some index k, recompute, and compare the values at or
before k. A causal feature cannot notice; a leaking one changes. This catches
the whole family at once - `shift(-1)`, centered rolling windows, normalization
fitted over the full series, `bfill`, a peak-to-trough label - without needing
to know which of them was written. Something no static scan can do.

*Completeness* checks ask whether the last bar in a dataset had actually closed
when it was acted on. Trading a partial bar is look-ahead by another name: the
bar's high, low and close are not yet what they will be.

**A clean report is not a proof.** Perturbation samples probe points; it can
miss a leak that only manifests elsewhere. It is strong evidence and a strictly
better position than assertion, and `audit_engine_causality` says how many
probes it ran so a clean result carries its own strength.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from autotrader.research.engines import DecisionEngine, ResearchSignal
from autotrader.research.splits import HoldoutSplit, TimeSplit

#: Stable, machine-readable finding codes. Messages may be reworded; codes may
#: not - a study's stored report is read by later tooling.
TEST_BEFORE_TRAIN = "TEST_BEFORE_TRAIN"
TRAIN_TEST_OVERLAP = "TRAIN_TEST_OVERLAP"
EMBARGO_TOO_SHORT = "EMBARGO_TOO_SHORT"
NO_EMBARGO = "NO_EMBARGO"
UNORDERED_TIMESTAMPS = "UNORDERED_TIMESTAMPS"
DUPLICATE_TIMESTAMP = "DUPLICATE_TIMESTAMP"
CROSS_WINDOW_TEST_OVERLAP = "CROSS_WINDOW_TEST_OVERLAP"
HOLDOUT_USED_IN_SELECTION = "HOLDOUT_USED_IN_SELECTION"
HOLDOUT_NOT_LAST = "HOLDOUT_NOT_LAST"
WARMUP_LONGER_THAN_WINDOW = "WARMUP_LONGER_THAN_WINDOW"
FUTURE_FEATURE_DEPENDENCE = "FUTURE_FEATURE_DEPENDENCE"
FUTURE_SIGNAL_DEPENDENCE = "FUTURE_SIGNAL_DEPENDENCE"
SIGNAL_AFTER_WINDOW = "SIGNAL_AFTER_WINDOW"
INCOMPLETE_FINAL_BAR = "INCOMPLETE_FINAL_BAR"

FINDING_CODES: tuple[str, ...] = (
    TEST_BEFORE_TRAIN,
    TRAIN_TEST_OVERLAP,
    EMBARGO_TOO_SHORT,
    NO_EMBARGO,
    UNORDERED_TIMESTAMPS,
    DUPLICATE_TIMESTAMP,
    CROSS_WINDOW_TEST_OVERLAP,
    HOLDOUT_USED_IN_SELECTION,
    HOLDOUT_NOT_LAST,
    WARMUP_LONGER_THAN_WINDOW,
    FUTURE_FEATURE_DEPENDENCE,
    FUTURE_SIGNAL_DEPENDENCE,
    SIGNAL_AFTER_WINDOW,
    INCOMPLETE_FINAL_BAR,
)

#: How many probe points a behavioural audit uses when not told otherwise.
#: Spread evenly through the interior of the dataset; more probes cost one
#: recomputation each.
DEFAULT_PROBE_COUNT = 5

#: The multiplier applied to future bars during a perturbation probe. Large
#: enough that any real dependence moves a value well past floating-point
#: noise, and applied to every price column together so the OHLC relationships
#: the validator enforces still hold - a probe must not be rejected as invalid
#: data by the very code it is testing.
PERTURBATION_FACTOR = 1.5

#: Absolute tolerance when comparing a value against its unperturbed self.
#: Recomputation over an identical prefix is deterministic, so this is a guard
#: against representation noise rather than a threshold on real differences.
COMPARISON_TOLERANCE = 1e-9

_PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "vwap")


class LeakageError(Exception):
    """Raised by the strict wrappers when an audit finds leakage."""


@dataclass(frozen=True)
class LeakageFinding:
    """One detected way information could travel backwards in time."""

    code: str
    message: str
    detail: Mapping[str, object] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_json_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "detail": dict(self.detail)}


@dataclass(frozen=True)
class LeakageReport:
    """The outcome of one audit.

    `probes` records how much work a behavioural audit actually did, so a clean
    report can be read with the right amount of confidence: zero probes and no
    findings means nothing was checked.
    """

    findings: tuple[LeakageFinding, ...] = ()
    probes: int = 0
    checks: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        """True when nothing was found."""
        return not self.findings

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(finding.code for finding in self.findings)

    def raise_for_findings(self) -> None:
        """Raise `LeakageError` describing every finding, if there are any."""
        if self.clean:
            return
        listed = "\n".join(f"- {finding}" for finding in self.findings)
        raise LeakageError(f"Leakage audit found {len(self.findings)} problem(s):\n{listed}")

    def merged_with(self, other: LeakageReport) -> LeakageReport:
        """Combine two reports, summing their probe counts."""
        return LeakageReport(
            findings=self.findings + other.findings,
            probes=self.probes + other.probes,
            checks=self.checks + other.checks,
        )

    def to_json_dict(self) -> dict[str, object]:
        return {
            "clean": self.clean,
            "probes": self.probes,
            "checks": list(self.checks),
            "findings": [finding.to_json_dict() for finding in self.findings],
        }


# --------------------------------------------------------------------------
# Structural audits
# --------------------------------------------------------------------------


def audit_timestamps(timestamps: Sequence[pd.Timestamp]) -> LeakageReport:
    """Check that a bar index is ordered and free of duplicated instants.

    An unsorted frame is the signature of a shuffled split: positions remain
    contiguous while time does not, so every downstream ordering check passes
    while the data is scrambled. A duplicated instant lets one bar sit in both
    a train and a test window.
    """
    findings: list[LeakageFinding] = []
    unordered: list[int] = []
    duplicates: list[int] = []
    for position in range(1, len(timestamps)):
        if timestamps[position] < timestamps[position - 1]:
            unordered.append(position)
        elif timestamps[position] == timestamps[position - 1]:
            duplicates.append(position)

    if unordered:
        findings.append(
            LeakageFinding(
                UNORDERED_TIMESTAMPS,
                f"{len(unordered)} bar(s) precede the bar before them; the series is not "
                "ordered in time. A split over unordered bars is a shuffled split.",
                {"first_index": unordered[0], "count": len(unordered)},
            )
        )
    if duplicates:
        findings.append(
            LeakageFinding(
                DUPLICATE_TIMESTAMP,
                f"{len(duplicates)} duplicated timestamp(s); one instant can then fall in "
                "both a train and a test window.",
                {"first_index": duplicates[0], "count": len(duplicates)},
            )
        )
    return LeakageReport(tuple(findings), checks=("timestamps",))


def audit_splits(
    splits: Sequence[TimeSplit],
    *,
    required_embargo: int = 0,
    require_disjoint_tests: bool = True,
) -> LeakageReport:
    """Check a set of walk-forward windows for every structural leak.

    `required_embargo` is the minimum gap the study's features and labels
    demand - normally the longer of the feature lookback and the label horizon.
    A study that supplies zero is told so (`NO_EMBARGO`) rather than silently
    passing, because "we did not think about it" and "we determined none was
    needed" produce the same split object and must not produce the same report.

    `require_disjoint_tests` catches the subtler contamination: overlapping test
    windows mean the same bar is scored more than once, so an average over
    windows is not an average over independent samples and its error bars are
    fiction.
    """
    findings: list[LeakageFinding] = []

    for split in splits:
        if split.test_start < split.train_end:
            findings.append(
                LeakageFinding(
                    TEST_BEFORE_TRAIN,
                    f"Split {split.index} tests from bar {split.test_start}, before its train "
                    f"window ends at {split.train_end}.",
                    {"split": split.index},
                )
            )
        train_range = range(split.train_start, split.train_end)
        test_range = range(split.test_start, split.test_end)
        overlap = set(train_range) & set(test_range)
        if overlap:
            findings.append(
                LeakageFinding(
                    TRAIN_TEST_OVERLAP,
                    f"Split {split.index} shares {len(overlap)} bar(s) between its train and "
                    "test windows; those bars were both fitted and scored.",
                    {"split": split.index, "shared_bars": len(overlap)},
                )
            )
        if required_embargo > 0 and split.gap < required_embargo:
            findings.append(
                LeakageFinding(
                    EMBARGO_TOO_SHORT,
                    f"Split {split.index} leaves {split.gap} bar(s) between train and test but "
                    f"the study's features and labels require {required_embargo}. A feature "
                    "at the test window's start still reads training bars.",
                    {
                        "split": split.index,
                        "gap": split.gap,
                        "required": required_embargo,
                    },
                )
            )

    if required_embargo == 0 and splits and all(split.embargo_bars == 0 for split in splits):
        findings.append(
            LeakageFinding(
                NO_EMBARGO,
                "No embargo was declared on any window. Adjacent train and test bars share "
                "indicator lookback and label horizon; state the required embargo explicitly, "
                "even if the justified value is zero.",
                {"split_count": len(splits)},
            )
        )

    if require_disjoint_tests:
        seen: dict[int, int] = {}
        collisions = 0
        for split in splits:
            for bar in range(split.test_start, split.test_end):
                if bar in seen:
                    collisions += 1
                else:
                    seen[bar] = split.index
        if collisions:
            findings.append(
                LeakageFinding(
                    CROSS_WINDOW_TEST_OVERLAP,
                    f"{collisions} bar(s) are scored by more than one test window. Averaging "
                    "over these windows is not averaging over independent samples.",
                    {"repeated_bars": collisions},
                )
            )

    return LeakageReport(tuple(findings), checks=("splits",))


def audit_holdout(
    holdout: HoldoutSplit,
    *,
    total_bars: int,
    selection_splits: Sequence[TimeSplit] = (),
) -> LeakageReport:
    """Check the final holdout was carved off correctly and never selected on.

    Two distinct failures. `HOLDOUT_NOT_LAST` means the holdout is not the tail
    of the dataset, so the study trained on data that follows what it calls
    out-of-sample. `HOLDOUT_USED_IN_SELECTION` means a walk-forward window that
    informed parameter selection reaches into the holdout - which turns the
    final number from an honest estimate into the thing it was tuned against.
    """
    findings: list[LeakageFinding] = []

    if holdout.holdout_end != total_bars:
        findings.append(
            LeakageFinding(
                HOLDOUT_NOT_LAST,
                f"The holdout ends at bar {holdout.holdout_end} of {total_bars}; it is not the "
                "final stretch of the dataset, so the study saw data that follows it.",
                {"holdout_end": holdout.holdout_end, "total_bars": total_bars},
            )
        )

    holdout_range = set(range(holdout.holdout_start, holdout.holdout_end))
    for split in selection_splits:
        touched = holdout_range & set(range(split.train_start, split.test_end))
        if touched:
            findings.append(
                LeakageFinding(
                    HOLDOUT_USED_IN_SELECTION,
                    f"Selection window {split.index} spans {len(touched)} holdout bar(s). A "
                    "parameter chosen against the holdout makes the final out-of-sample "
                    "number a training score.",
                    {"split": split.index, "holdout_bars_touched": len(touched)},
                )
            )

    return LeakageReport(tuple(findings), checks=("holdout",))


def audit_warmup(
    splits: Sequence[TimeSplit],
    *,
    warmup_bars: int,
) -> LeakageReport:
    """Check every window is long enough for the engine's declared warm-up.

    A window shorter than the warm-up produces an engine output that is all
    ramp and no signal. It does not import future information, so it is not
    leakage in the strict sense - it is the other way a walk-forward result
    becomes meaningless, and it is checked here because this is where a study
    looks.
    """
    findings = [
        LeakageFinding(
            WARMUP_LONGER_THAN_WINDOW,
            f"Split {split.index} has a {split.train_length}-bar train window and a "
            f"{split.test_length}-bar test window, but the engine needs {warmup_bars} bars "
            "of warm-up. Its output over this window is under-informed ramp, not signal.",
            {
                "split": split.index,
                "train_length": split.train_length,
                "test_length": split.test_length,
                "warmup_bars": warmup_bars,
            },
        )
        for split in splits
        if split.train_length < warmup_bars or split.test_length < warmup_bars
    ]
    return LeakageReport(tuple(findings), checks=("warmup",))


def audit_bar_completeness(
    bars: pd.DataFrame,
    *,
    bar_duration: timedelta,
    as_of: datetime,
) -> LeakageReport:
    """Check the last bar had actually closed by `as_of`.

    A bar stamped at its open is complete only once ``open + duration`` has
    passed. Acting on a bar before then trades a high, low and close that are
    not final - look-ahead, arrived at from the other direction, and the one
    form of it that a purely offline dataset check cannot see.
    """
    if bars.empty:
        return LeakageReport(checks=("bar_completeness",))
    last = pd.Timestamp(bars["timestamp"].iloc[-1])
    closes_at = last.to_pydatetime() + bar_duration
    reference = pd.Timestamp(as_of).to_pydatetime()
    if closes_at > reference:
        return LeakageReport(
            (
                LeakageFinding(
                    INCOMPLETE_FINAL_BAR,
                    f"The final bar opens at {last} and closes at {closes_at}, which is after "
                    f"{reference}. It had not finished forming and must not be traded on.",
                    {
                        "final_bar_timestamp": str(last),
                        "closes_at": str(closes_at),
                        "as_of": str(reference),
                    },
                ),
            ),
            checks=("bar_completeness",),
        )
    return LeakageReport(checks=("bar_completeness",))


# --------------------------------------------------------------------------
# Behavioural audits: perturbation
# --------------------------------------------------------------------------


def probe_indices(bar_count: int, probes: int = DEFAULT_PROBE_COUNT) -> tuple[int, ...]:
    """Evenly spaced interior probe points for a perturbation audit.

    Interior on purpose: probing index 0 perturbs everything and probing the
    last index perturbs nothing, and neither tells you anything.
    """
    if bar_count < 3 or probes < 1:
        return ()
    usable = min(probes, bar_count - 2)
    step = (bar_count - 2) / usable
    chosen = {
        max(1, min(bar_count - 2, int((position + 0.5) * step))) for position in range(usable)
    }
    return tuple(sorted(chosen))


def perturb_after(
    bars: pd.DataFrame,
    index: int,
    factor: float = PERTURBATION_FACTOR,
) -> pd.DataFrame:
    """A copy of `bars` with every price strictly after `index` scaled.

    All price columns move together by the same factor, so every OHLC
    relationship the validator enforces still holds and the perturbed frame is
    valid data rather than a malformed one that a strategy might reject for
    unrelated reasons. Rows at or before `index` are untouched, which is the
    whole basis of the comparison.
    """
    perturbed = bars.copy()
    if index + 1 >= len(perturbed):
        return perturbed
    tail = perturbed.index[index + 1 :]
    for column in _PRICE_COLUMNS:
        if column in perturbed.columns:
            perturbed.loc[tail, column] = perturbed.loc[tail, column] * factor
    return perturbed


def audit_feature_causality(
    compute: Callable[[pd.DataFrame], pd.Series],
    bars: pd.DataFrame,
    *,
    probes: int = DEFAULT_PROBE_COUNT,
    tolerance: float = COMPARISON_TOLERANCE,
) -> LeakageReport:
    """Prove a feature function cannot see the future, by trying to make it.

    `compute` maps bars to one value per bar, positionally aligned. For each
    probe index k the future is perturbed and the feature recomputed; every
    value at or before k must be unchanged. A value that moves is a value that
    was reading a bar that had not happened.

    Catches, without being told which to look for: negative shifts, centered
    rolling windows, normalization fitted over the whole series, backward
    filling, and any label built from a forward-looking horizon. NaN is
    compared as NaN, so a warm-up mask is not mistaken for a difference.
    """
    findings: list[LeakageFinding] = []
    baseline = pd.Series(compute(bars)).reset_index(drop=True)
    indices = probe_indices(len(bars), probes)

    for index in indices:
        candidate = pd.Series(compute(perturb_after(bars, index))).reset_index(drop=True)
        if len(candidate) != len(baseline):
            findings.append(
                LeakageFinding(
                    FUTURE_FEATURE_DEPENDENCE,
                    f"Perturbing bars after index {index} changed the feature's length from "
                    f"{len(baseline)} to {len(candidate)}.",
                    {"probe_index": index},
                )
            )
            continue

        original = baseline.iloc[: index + 1]
        recomputed = candidate.iloc[: index + 1]
        differing = _differing_positions(original, recomputed, tolerance)
        if differing:
            findings.append(
                LeakageFinding(
                    FUTURE_FEATURE_DEPENDENCE,
                    f"Perturbing bars after index {index} changed {len(differing)} feature "
                    f"value(s) at or before it, the earliest at index {differing[0]}. The "
                    "feature reads bars that had not happened yet.",
                    {
                        "probe_index": index,
                        "earliest_affected_index": differing[0],
                        "affected_count": len(differing),
                    },
                )
            )

    return LeakageReport(tuple(findings), probes=len(indices), checks=("feature_causality",))


def _differing_positions(
    original: pd.Series,
    recomputed: pd.Series,
    tolerance: float,
) -> list[int]:
    """Positions where two aligned series genuinely differ. NaN equals NaN."""
    differing: list[int] = []
    for position, (left, right) in enumerate(zip(original, recomputed, strict=True)):
        left_missing = pd.isna(left)
        right_missing = pd.isna(right)
        if left_missing and right_missing:
            continue
        if left_missing != right_missing:
            differing.append(position)
            continue
        try:
            if abs(float(left) - float(right)) > tolerance:
                differing.append(position)
        except (TypeError, ValueError):
            if left != right:
                differing.append(position)
    return differing


def _signature(signals: Sequence[ResearchSignal], cutoff: pd.Timestamp) -> tuple[tuple, ...]:
    """The signals at or before `cutoff`, as comparable tuples."""
    return tuple(
        (str(signal.timestamp), signal.symbol, signal.action.value, signal.reason)
        for signal in signals
        if signal.timestamp <= cutoff
    )


def audit_engine_causality(
    engine: DecisionEngine,
    bars: pd.DataFrame,
    *,
    probes: int = DEFAULT_PROBE_COUNT,
) -> LeakageReport:
    """Prove a Decision Engine's signals cannot see the future.

    The same perturbation argument as `audit_feature_causality`, applied to the
    thing that actually gets traded. For each probe index k the future is
    perturbed and the engine re-asked; the signals it emits at or before bar k
    must be identical - same instants, same actions, same reasons.

    This is the check every future V2/V3/V4/V5 engine must pass before its
    backtest means anything, and it needs no knowledge of how the engine works.
    A `None` engine output, an exception, or a changed signal set are all
    findings rather than crashes.
    """
    findings: list[LeakageFinding] = []
    timestamps = list(bars["timestamp"])
    baseline = tuple(engine.generate(bars))

    known = set(timestamps)
    stray = [signal for signal in baseline if signal.timestamp not in known]
    if stray:
        findings.append(
            LeakageFinding(
                SIGNAL_AFTER_WINDOW,
                f"{engine.name} emitted {len(stray)} signal(s) at instants that are not bars "
                "in the frame it was given.",
                {"count": len(stray), "first": str(stray[0].timestamp)},
            )
        )

    indices = probe_indices(len(bars), probes)
    for index in indices:
        cutoff = timestamps[index]
        recomputed = tuple(engine.generate(perturb_after(bars, index)))
        before = _signature(baseline, cutoff)
        after = _signature(recomputed, cutoff)
        if before != after:
            findings.append(
                LeakageFinding(
                    FUTURE_SIGNAL_DEPENDENCE,
                    f"Perturbing bars after {cutoff} changed the signals {engine.name} emits "
                    f"at or before it ({len(before)} became {len(after)}). The engine reads "
                    "bars that had not happened yet.",
                    {
                        "probe_index": index,
                        "cutoff": str(cutoff),
                        "signals_before": len(before),
                        "signals_after": len(after),
                    },
                )
            )

    return LeakageReport(tuple(findings), probes=len(indices), checks=("engine_causality",))


def require_causal_engine(
    engine: DecisionEngine,
    bars: pd.DataFrame,
    *,
    probes: int = DEFAULT_PROBE_COUNT,
) -> None:
    """Audit `engine` and raise `LeakageError` if it looks into the future.

    The strict form, for a study that should refuse to produce numbers at all
    rather than produce ones it has to caveat.
    """
    audit_engine_causality(engine, bars, probes=probes).raise_for_findings()


def audit_study(
    *,
    engine: DecisionEngine,
    bars: pd.DataFrame,
    splits: Sequence[TimeSplit],
    holdout: HoldoutSplit | None = None,
    required_embargo: int = 0,
    probes: int = DEFAULT_PROBE_COUNT,
) -> LeakageReport:
    """Run every applicable audit over one study's configuration.

    The single call a study makes before it computes anything. Structural
    checks first because they are cheap and their failures invalidate the
    behavioural ones anyway.
    """
    report = audit_timestamps(list(bars["timestamp"]))
    report = report.merged_with(audit_splits(splits, required_embargo=required_embargo))
    report = report.merged_with(audit_warmup(splits, warmup_bars=engine.warmup_bars))
    if holdout is not None:
        report = report.merged_with(
            audit_holdout(holdout, total_bars=len(bars), selection_splits=splits)
        )
    return report.merged_with(audit_engine_causality(engine, bars, probes=probes))


__all__ = [
    "COMPARISON_TOLERANCE",
    "CROSS_WINDOW_TEST_OVERLAP",
    "DEFAULT_PROBE_COUNT",
    "DUPLICATE_TIMESTAMP",
    "EMBARGO_TOO_SHORT",
    "FINDING_CODES",
    "FUTURE_FEATURE_DEPENDENCE",
    "FUTURE_SIGNAL_DEPENDENCE",
    "HOLDOUT_NOT_LAST",
    "HOLDOUT_USED_IN_SELECTION",
    "INCOMPLETE_FINAL_BAR",
    "NO_EMBARGO",
    "PERTURBATION_FACTOR",
    "SIGNAL_AFTER_WINDOW",
    "TEST_BEFORE_TRAIN",
    "TRAIN_TEST_OVERLAP",
    "UNORDERED_TIMESTAMPS",
    "WARMUP_LONGER_THAN_WINDOW",
    "LeakageError",
    "LeakageFinding",
    "LeakageReport",
    "audit_bar_completeness",
    "audit_engine_causality",
    "audit_feature_causality",
    "audit_holdout",
    "audit_splits",
    "audit_study",
    "audit_timestamps",
    "audit_warmup",
    "perturb_after",
    "probe_indices",
    "require_causal_engine",
]
