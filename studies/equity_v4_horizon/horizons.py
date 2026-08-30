"""The frozen horizon set, and what each horizon means on a session-traded market.

**The set is fixed and small on purpose.** Four candidates were declared in the
study design before any model was trained, and nothing in this module offers a
way to add one after results exist. A sweep that grows when the first numbers
disappoint is how a research study becomes a noise-fitting exercise.

**A horizon is counted in completed regular-session bars, never in wall-clock
time.** ``LabelSpec.horizon_bars`` steps through positions of the session-aware
equity grid, so "16 bars after Friday 15:45" lands inside Monday's session, an
early close contributes 14 positions rather than 26, and a bar the provider
never published invalidates the interval instead of being stepped over. That
arithmetic lives in ``autotrader.ml.labels`` and is reused, not restated - this
module only *names* the horizons and predicts their measurable consequences so
the study can verify them.

**Longer horizons overlap more, and the guards must scale with them.** Two rows
closer together than the horizon share future bars, so purging removes more
training rows at every boundary and the outer train->score gap grows by the
horizon itself. ``overlap_factor`` and ``outer_gap_bars`` are the study's
declared expectations; the tests and the run artifacts measure the reality.
"""

from __future__ import annotations

from dataclasses import dataclass

from autotrader.ml.labels import LabelSpec
from autotrader.ml.v4 import DEFAULT_HORIZON_BARS, default_label_spec
from studies.equity_v1_v5.windows import EMBARGO_BARS, SCORING_WINDOWS, ScoringWindow

#: The predeclared candidate horizons, in 15-minute regular-session bars.
#: 4 is the shipped default; 8, 16 and 26 are roughly two hours, four hours and
#: one full regular session of market time. Frozen in design.md.
STUDY_HORIZONS: tuple[int, ...] = (4, 8, 16, 26)

#: Bars in one full regular session, which is also the embargo the pilot used.
FULL_SESSION_BARS = 26

#: The seed every training run in this study uses.
STUDY_SEED = 0

#: How many validation rows an isotonic step must hold before a >=0.99 or
#: <=0.01 calibrated probability from it is considered supported rather than a
#: thin-bin artifact. Declared in the design before results were seen.
MIN_EXTREME_SUPPORT = 30

#: The windows the winner rule is applied to, and the untouched holdout.
#: 2026-summer is excluded from horizon selection entirely; its cells at the
#: alternative horizons are not computed until the selection-set verdict is
#: recorded (design.md section 5).
SELECTION_WINDOWS: tuple[ScoringWindow, ...] = SCORING_WINDOWS[:5]
HOLDOUT_WINDOW: ScoringWindow = SCORING_WINDOWS[5]


class HorizonError(Exception):
    """A horizon outside the frozen study set was requested."""


def require_study_horizon(horizon_bars: int) -> int:
    """Refuse any horizon the design did not declare.

    The refusal is the point: an expanded sweep must be impossible to perform
    accidentally, because the design promised the set would not grow after the
    first results were seen.
    """
    if horizon_bars not in STUDY_HORIZONS:
        raise HorizonError(
            f"Horizon {horizon_bars} is not in the frozen study set "
            f"{STUDY_HORIZONS}. The design predeclares the candidates; adding "
            "one after evaluation began would invalidate the study."
        )
    return int(horizon_bars)


def label_spec_for(horizon_bars: int) -> LabelSpec:
    """The CURRENT V4 label semantics at `horizon_bars`.

    Everything except the horizon is the shipped default - binary direction,
    zero threshold, entry at the next bar's open, exit at the open
    ``horizon_bars`` later, session gaps spanned and flagged. Holding the rest
    of the specification fixed is what isolates the horizon effect from a
    label-definition effect.
    """
    return default_label_spec(horizon_bars=require_study_horizon(horizon_bars))


def outer_gap_bars(horizon_bars: int) -> int:
    """Bars between the last training row and the first scored bar.

    The horizon itself - so the last training label resolves strictly before
    the window opens - plus one whole regular session, the same embargo the
    pilot used. The gap must scale with the horizon: a 26-bar label written 30
    bars before the window would resolve four bars inside it.
    """
    return require_study_horizon(horizon_bars) + EMBARGO_BARS


def overlap_factor(horizon_bars: int) -> int:
    """How many neighbouring rows share future bars with a given row.

    Rows *i* and *j* have overlapping label intervals exactly when
    ``|i - j| < horizon_bars`` in grid positions (the entry offset shifts both
    intervals equally). This is the declared expectation the purge counts in
    the run artifacts are checked against.
    """
    return require_study_horizon(horizon_bars)


@dataclass(frozen=True)
class HorizonPrediction:
    """An analytic prediction the measured data must reproduce.

    ``session_gap_fraction`` is the fraction of full-session rows whose label
    crosses at least one session boundary: a feature bar at position *k* of a
    26-bar session exits at position ``k + horizon + 1``, which lies beyond the
    session for ``k >= 26 - horizon - 1``. Verifying the measured fraction
    against this is a cheap end-to-end check that the grid arithmetic means
    what this module claims it means.
    """

    horizon_bars: int
    approx_trading_time: str
    session_gap_fraction_full_session: float


#: What each horizon should look like on an uninterrupted run of full sessions.
HORIZON_PREDICTIONS: tuple[HorizonPrediction, ...] = (
    HorizonPrediction(4, "1 trading hour", (4 + 1) / 26),
    HorizonPrediction(8, "2 trading hours", (8 + 1) / 26),
    HorizonPrediction(16, "4 trading hours", (16 + 1) / 26),
    HorizonPrediction(26, "one full regular session", 1.0),
)


def prediction_for(horizon_bars: int) -> HorizonPrediction:
    """The declared expectations for one horizon."""
    require_study_horizon(horizon_bars)
    for prediction in HORIZON_PREDICTIONS:
        if prediction.horizon_bars == horizon_bars:
            return prediction
    raise HorizonError(f"No prediction declared for horizon {horizon_bars}.")  # pragma: no cover


__all__ = [
    "FULL_SESSION_BARS",
    "HOLDOUT_WINDOW",
    "HORIZON_PREDICTIONS",
    "MIN_EXTREME_SUPPORT",
    "SELECTION_WINDOWS",
    "STUDY_HORIZONS",
    "STUDY_SEED",
    "DEFAULT_HORIZON_BARS",
    "HorizonError",
    "HorizonPrediction",
    "label_spec_for",
    "outer_gap_bars",
    "overlap_factor",
    "prediction_for",
    "require_study_horizon",
]
