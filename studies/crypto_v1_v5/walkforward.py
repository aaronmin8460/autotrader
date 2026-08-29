"""V4's walk-forward training plan: one model per out-of-sample window, fitted on its past.

**The rule this module exists to enforce.** A model that scores a window must
have been fitted only on information that existed before that window opened.
Training once over the whole history and scoring the same history would produce
a V4 - and therefore a V5 - that knew its own answers, and every number
downstream of it would be fiction. So the history is cut into consecutive
out-of-sample windows, and each one gets its own model fitted on the data that
preceded it.

**Anchored, not rolling.** Every fold trains on everything from the start of the
dataset up to its own boundary, which is the same scheme
`autotrader.ml.splits.walk_forward_folds` implements and the same one
`autotrader.ml.v4.compare_candidates` grades candidates under. A rolling window
would discard history for no stated reason; an anchored one lets a later fold be
better informed than an earlier one, which is what actually happens live.

**The gap between training and scoring is not decoration.** A label at bar *t*
resolves `horizon_bars` later, so training rows near the boundary carry outcomes
that fall inside the window being scored. `TRAIN_TEST_GAP_BARS` removes the
horizon and then an embargo on top of it, so the last training label resolves
strictly before the first scored bar. `autotrader.ml.v4` applies purge and
embargo *within* the fold as well; this is the outer gap between folds.

**Model choice is re-made every fold, by the repository's own rule.**
`compare_candidates` grades every candidate against a class-frequency baseline
on anchored sub-folds of that fold's training data, and `select_candidate`
refuses anything that does not beat the baseline by a material margin. The
comparison is recorded whether or not it selects a model, because "no candidate
beat its null" is the finding on a market that offered no edge, and a study that
only recorded the winners could not report it.

**The forced variant is a diagnostic, never the headline.** `force_family`
fits the best non-baseline candidate regardless of whether it cleared the
materiality bar. It answers "what would V4 have done with a real model?" - which
the evidence needs - without letting that answer be reported as what V4 is.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from autotrader.decision.probability import ProbabilityArtifact
from autotrader.ml.grid import crypto_grid
from autotrader.ml.splits import SplitSpec
from autotrader.ml.v4 import (
    DEFAULT_HORIZON_BARS,
    MATERIAL_LOG_LOSS_IMPROVEMENT,
    Candidate,
    ModelComparison,
    TrainingFrame,
    build_training_frame,
    compare_candidates,
    default_candidates,
    select_candidate,
    train_model,
)
from autotrader.research.reproducibility import dataset_digest

#: Bars removed between the last training bar and the first scored bar: the
#: label horizon, so no training outcome resolves inside the window, plus a
#: one-day embargo on top of it.
EMBARGO_BARS = 96
TRAIN_TEST_GAP_BARS = DEFAULT_HORIZON_BARS + EMBARGO_BARS

#: The base timeframe interval, as a span.
BAR_INTERVAL = pd.Timedelta("15min")

#: The family name of the null model every candidate is measured against.
BASELINE_FAMILY = "class_frequency"


class WalkForwardError(Exception):
    """A walk-forward plan or fold cannot be built."""


@dataclass(frozen=True)
class FoldPlan:
    """One out-of-sample window and the training range that precedes it."""

    fold_id: str
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    is_holdout: bool

    def to_record(self) -> dict[str, object]:
        return {
            "fold_id": self.fold_id,
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "test_start": self.test_start.isoformat(),
            "test_end": self.test_end.isoformat(),
            "is_holdout": self.is_holdout,
            "train_test_gap_bars": TRAIN_TEST_GAP_BARS,
        }


@dataclass(frozen=True)
class FoldModel:
    """What one fold's training produced, and the evidence behind the choice."""

    plan: FoldPlan
    symbol: str
    artifact: ProbabilityArtifact
    comparison: ModelComparison
    chosen_family: str
    rationale: str
    beat_baseline: bool
    training_rows: int
    training_digest: str
    test_metrics: Mapping[str, float]
    baseline_log_loss: float
    best_candidate_log_loss: float
    best_candidate_family: str
    forced: bool = False
    candidate_means: Mapping[str, Mapping[str, float]] = field(default_factory=dict)

    def to_record(self) -> dict[str, object]:
        return {
            **self.plan.to_record(),
            "symbol": self.symbol,
            "model_version": self.artifact.model_version,
            "model_family": self.artifact.family,
            "calibrated": self.artifact.calibrated,
            "calibration_method": self.artifact.calibration_method,
            "feature_version": self.artifact.feature_version,
            "label_spec_id": self.artifact.label_spec_id,
            "chosen_family": self.chosen_family,
            "forced": self.forced,
            "beat_baseline": self.beat_baseline,
            "rationale": self.rationale,
            "training_rows": self.training_rows,
            "training_dataset_digest": self.training_digest,
            "baseline_mean_log_loss": self.baseline_log_loss,
            "best_candidate_mean_log_loss": self.best_candidate_log_loss,
            "best_candidate_family": self.best_candidate_family,
            "material_improvement_required": MATERIAL_LOG_LOSS_IMPROVEMENT,
            "candidate_mean_metrics": {k: dict(v) for k, v in self.candidate_means.items()},
            "in_sample_test_metrics": dict(self.test_metrics),
        }


def plan_folds(
    *,
    oos_start: pd.Timestamp,
    oos_end: pd.Timestamp,
    dataset_start: pd.Timestamp,
    period: str = "QS",
    holdout_windows: int = 1,
) -> tuple[FoldPlan, ...]:
    """Cut ``[oos_start, oos_end]`` into consecutive windows, each with its training past.

    The last `holdout_windows` are marked as holdout. They are scored like every
    other window and excluded from anything that resembles a choice, so the
    final period stays a test rather than becoming a selection criterion.
    """
    if oos_start >= oos_end:
        raise WalkForwardError("The out-of-sample range is empty.")
    edges = list(pd.date_range(oos_start, oos_end, freq=period, tz="UTC"))
    if not edges or edges[0] > oos_start:
        edges.insert(0, oos_start)
    if edges[-1] < oos_end:
        edges.append(oos_end + BAR_INTERVAL)

    plans: list[FoldPlan] = []
    for index in range(len(edges) - 1):
        start = max(edges[index], oos_start)
        end = min(edges[index + 1] - BAR_INTERVAL, oos_end)
        if start > end:
            continue
        plans.append(
            FoldPlan(
                fold_id=f"W{index + 1:02d}",
                train_start=dataset_start,
                train_end=start - TRAIN_TEST_GAP_BARS * BAR_INTERVAL,
                test_start=start,
                test_end=end,
                is_holdout=False,
            )
        )
    if not plans:
        raise WalkForwardError("The out-of-sample range produced no windows.")
    cut = max(0, len(plans) - max(0, holdout_windows))
    return tuple(
        FoldPlan(**{**plan.__dict__, "is_holdout": index >= cut})
        for index, plan in enumerate(plans)
    )


def training_frame_for(bars: pd.DataFrame, plan: FoldPlan) -> TrainingFrame:
    """The rows a fold's model may be fitted on: strictly before its gap-adjusted boundary."""
    window = bars[(bars["timestamp"] >= plan.train_start) & (bars["timestamp"] <= plan.train_end)]
    window = window.reset_index(drop=True)
    if window.empty:
        raise WalkForwardError(f"{plan.fold_id} has no training bars before {plan.train_end}.")
    grid = crypto_grid(
        window["timestamp"].iloc[0].to_pydatetime(), window["timestamp"].iloc[-1].to_pydatetime()
    )
    return build_training_frame(window, grid=grid)


def _means(comparison: ModelComparison) -> dict[str, dict[str, float]]:
    return {
        result.candidate.family: {k: float(v) for k, v in result.mean_metrics.items()}
        for result in comparison.results
    }


def best_non_baseline(comparison: ModelComparison) -> tuple[Candidate, float]:
    """The strongest candidate that is not the null model, by mean walk-forward log loss."""
    contenders = [r for r in comparison.results if r.candidate.family != BASELINE_FAMILY]
    if not contenders:
        raise WalkForwardError("The comparison held no candidate other than the baseline.")
    best = min(contenders, key=lambda result: result.mean_log_loss)
    return best.candidate, best.mean_log_loss


@dataclass(frozen=True)
class FoldEvidence:
    """One fold's training rows and the graded comparison over them.

    Separated from fitting because the comparison is the expensive half and both
    the selected model and the forced diagnostic are fitted from the same one.
    Grading twice would also risk the two variants being judged on different
    folds, which would make the diagnostic incomparable to the thing it
    diagnoses.
    """

    plan: FoldPlan
    symbol: str
    training: TrainingFrame
    comparison: ModelComparison


def grade_fold(
    bars: pd.DataFrame,
    plan: FoldPlan,
    *,
    symbol: str,
    candidates: Sequence[Candidate] | None = None,
    inner_folds: int = 4,
    seed: int = 0,
) -> FoldEvidence:
    """Build one fold's training rows and grade every candidate on them."""
    training = training_frame_for(bars, plan)
    entries = tuple(candidates) if candidates is not None else default_candidates()
    comparison = compare_candidates(
        training, candidates=entries, folds=inner_folds, embargo_bars=EMBARGO_BARS, seed=seed
    )
    return FoldEvidence(plan=plan, symbol=symbol, training=training, comparison=comparison)


def fit_fold_model(
    evidence: FoldEvidence,
    *,
    seed: int = 0,
    force_family: bool = False,
    model_version_prefix: str = "v4",
    trained_at: datetime | None = None,
) -> FoldModel:
    """Fit one fold's model from already-graded evidence."""
    plan, symbol = evidence.plan, evidence.symbol
    training, comparison = evidence.training, evidence.comparison
    selected, rationale = select_candidate(comparison.results)
    baseline = comparison.result_for(
        next(r.candidate.name for r in comparison.results if r.candidate.family == BASELINE_FAMILY)
    )
    best_candidate, best_log_loss = best_non_baseline(comparison)
    beat = selected.family != BASELINE_FAMILY

    chosen = selected
    if force_family:
        chosen = best_candidate
        rationale = (
            f"Diagnostic variant: {best_candidate.family} was fitted regardless of the "
            f"materiality rule, which selected {selected.family}."
        )

    suffix = "forced" if force_family else "selected"
    version = f"{model_version_prefix}-{symbol.replace('/', '')}-{plan.fold_id}-{suffix}"
    trained = train_model(
        training,
        chosen,
        model_version=version,
        split=SplitSpec(embargo_bars=EMBARGO_BARS),
        seed=seed,
        trained_at=trained_at,
        notes=f"Walk-forward fold {plan.fold_id} for {symbol}; scores {plan.test_start.date()}"
        f" to {plan.test_end.date()}.",
    )
    return FoldModel(
        plan=plan,
        symbol=symbol,
        artifact=trained.artifact,
        comparison=comparison,
        chosen_family=chosen.family,
        rationale=rationale,
        beat_baseline=beat,
        training_rows=training.row_count,
        training_digest=dataset_digest(training.frame),
        test_metrics={k: float(v) for k, v in trained.test_metrics.items()},
        baseline_log_loss=float(baseline.mean_log_loss),
        best_candidate_log_loss=float(best_log_loss),
        best_candidate_family=best_candidate.family,
        forced=force_family,
        candidate_means=_means(comparison),
    )


__all__ = [
    "BASELINE_FAMILY",
    "EMBARGO_BARS",
    "TRAIN_TEST_GAP_BARS",
    "FoldEvidence",
    "FoldModel",
    "FoldPlan",
    "WalkForwardError",
    "best_non_baseline",
    "fit_fold_model",
    "grade_fold",
    "plan_folds",
    "training_frame_for",
]
