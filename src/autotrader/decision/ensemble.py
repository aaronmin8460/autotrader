"""V5's ensemble, as a value: the weights, the hold band, and the attribution.

V3 produces a deterministic score and V4 a calibrated probability. Combining
them is arithmetic, and this module is that arithmetic plus the configuration it
is performed under. The engine that drives the two sub-engines lives in `v5.py`;
nothing here reads a bar.

**The ensemble is versioned the way a model is.** V4 keeps the *shape* of a
stored artifact (`PROBABILITY_CONTRACT_VERSION`) apart from the identity of one
particular fitted model (`model_version`), because the two go stale for
different reasons. An ensemble has the same two failure modes - the record's
structure can change, and the weights can change - so it carries the same two
identifiers: `ENSEMBLE_CONTRACT_VERSION` here, and `EnsembleSpec.ensemble_version`
on each configuration. A stored decision names the exact ensemble that produced
it, and `from_record` refuses a record written under another contract rather
than reading it as though the fields still meant what they used to.

**Bounded by construction, at every step.** The directional blend is a weighted
mean of two scores in ``[-1, +1]`` whose weights sum to one, so it cannot leave
that interval - the same argument `FactorWeights` makes for V2's composite. The
confidence blend is a weighted mean of quantities in ``[0, 1]``, and so cannot
leave that one. Regime and volatility then act as *attenuations*: multipliers in
``[0, 1]``, which can shrink a conviction towards zero and can never grow one.
Nothing in this module can produce an out-of-range value that then has to be
clipped back into range, and `_require_bounded` refuses one rather than clamping
it, so a bound that stopped holding by construction would be loud. Only
floating-point drift past a bound the arithmetic already respects is absorbed.

**Context attenuates; it never votes.** A regime and a volatility reading are
not opinions about direction. Letting them add to the score would mean a quiet
market could manufacture a candidate out of two engines that named none, and
would also break the bound. They multiply instead, so the most they can do is
take conviction away - which is the only thing a context reading is evidence for.

**Attribution is the point of the ordering.** Two attenuations applied in
sequence have no order-free decomposition, so the order is written down - regime
first, then volatility - and the contributions are the chain differences along
it. That makes them exact rather than indicative: `EnsembleAttribution` refuses
to exist unless its components sum to the output it claims to explain, so a
recorded decision can always be taken apart into which input moved it and by how
much.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from autotrader.decision.config import WEIGHT_SUM_TOLERANCE, DecisionThresholds
from autotrader.decision.contract import (
    CONFIDENCE_MAX,
    CONFIDENCE_MIN,
    SCORE_MAX,
    SCORE_MIN,
    DecisionConfigError,
    DecisionSignal,
    MarketRegime,
)
from autotrader.decision.scoring import (
    REASON_HOLD_BAND,
    REASON_LOW_CONFIDENCE,
    REASON_REGIME_BLOCKED,
)

#: The version of the ensemble *record* shape below, distinct from any one
#: configuration's own version. This changes when the record's structure
#: changes, which invalidates every stored ensemble at once rather than one.
ENSEMBLE_CONTRACT_VERSION = "1.0.0"

#: How much a sum of contributions may differ from the value it explains, and
#: how far past a bound the arithmetic already respects a value may drift before
#: it is treated as a real violation rather than as the last bit of a float.
DRIFT_TOLERANCE = 1e-9

#: The named components of a V5 decision. Written down because they are keys in
#: an audit record: a contribution reported under a name that changed between
#: releases is a contribution nobody can match to an input afterwards.
COMPONENT_DETERMINISTIC = "v3_deterministic"
COMPONENT_PROBABILISTIC = "v4_probability"
COMPONENT_AGREEMENT = "component_agreement"
COMPONENT_REGIME = "regime"
COMPONENT_VOLATILITY = "volatility"

#: The order the two attenuations are applied in, and therefore the order the
#: chain decomposition below is taken along. Part of the contract, not an
#: implementation detail: a different order gives different contributions for
#: the same decision.
ATTENUATION_ORDER: tuple[str, ...] = (COMPONENT_REGIME, COMPONENT_VOLATILITY)

REASON_ENSEMBLE_BUY = "ENSEMBLE_SCORE_ABOVE_BUY_BAND"
REASON_ENSEMBLE_SELL = "ENSEMBLE_SCORE_BELOW_SELL_BAND"
REASON_COMPONENTS_AGREE = "ENSEMBLE_COMPONENTS_AGREE"
REASON_COMPONENTS_DISAGREE = "ENSEMBLE_COMPONENTS_DISAGREE"
REASON_UNCALIBRATED_MODEL = "ENSEMBLE_MODEL_UNCALIBRATED"

#: Where `component_agreement` sits when the two readings point the same way.
#: Exactly a half is the reading of a component with no opinion, so agreement is
#: claimed only above it and disagreement only below it.
NEUTRAL_AGREEMENT = 0.5


def component_reason(component: str, contribution: float) -> str:
    """The stable token naming how one component moved this bar's score.

    "Raised" and "lowered" rather than V2's and V4's "bullish" and "bearish",
    because an attenuation's sign follows the sign of what it attenuates. The
    regime term on a bullish bar is negative, and a token calling that bearish
    would report that the regime was bearish when what actually happened is that
    it took conviction out of a bullish reading.
    """
    if contribution > 0.0:
        movement = "RAISED"
    elif contribution < 0.0:
        movement = "LOWERED"
    else:
        movement = "UNMOVED"
    return f"COMPONENT_{component.upper()}_{movement}"


def _require_within(value: object, lower: float, upper: float, field_name: str) -> float:
    """Refuse a non-numeric, NaN, or out-of-range configuration value."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DecisionConfigError(
            f"{field_name} must be a real number, got {type(value).__name__}."
        )
    numeric = float(value)
    if numeric != numeric:  # NaN, which compares unequal to itself.
        raise DecisionConfigError(f"{field_name} must not be NaN.")
    if not lower <= numeric <= upper:
        raise DecisionConfigError(f"{field_name} must be within [{lower}, {upper}], got {numeric}.")
    return numeric


def _require_bounded(value: float, lower: float, upper: float, field_name: str) -> float:
    """Return `value` inside ``[lower, upper]``, refusing anything genuinely outside it.

    The difference between this and a clip is the whole claim of this module.
    Every quantity passed here is bounded by the arithmetic that produced it, so
    the only way it can arrive out of range is the last bit of a floating-point
    sum - which is absorbed - or a construction that stopped being bounded,
    which is raised rather than quietly trimmed into looking correct.
    """
    numeric = float(value)
    if numeric != numeric:
        raise DecisionConfigError(
            f"{field_name} must not be NaN: an unmeasurable {field_name} is a HOLD with a "
            "reason, never a number that silently propagates."
        )
    if not lower - DRIFT_TOLERANCE <= numeric <= upper + DRIFT_TOLERANCE:
        raise DecisionConfigError(
            f"{field_name} left [{lower}, {upper}] by more than floating-point drift, at "
            f"{numeric}. The ensemble is bounded by construction, so this is a broken "
            "construction rather than a value to clamp."
        )
    return max(lower, min(upper, numeric))


@dataclass(frozen=True)
class EnsembleWeights:
    """How the two engines' directional readings are blended.

    They sum to one, which is what keeps the blend inside ``[-1, +1]`` given
    that both operands are: a weighted mean cannot leave the interval its
    operands live in. The sum is validated rather than normalized, for the same
    reason `FactorWeights` validates its own - silently rescaling a mistyped
    weight would hide the typo and change the engine.

    Equal by default, and deliberately not fitted. There is no walk-forward
    evidence in this repository that a deterministic score deserves more weight
    than a calibrated probability or the reverse, and a weight chosen by looking
    at this system's data would be exactly the fitted constant docs/SPEC.md
    section 2 refuses. A deployment with such evidence sets them and names the
    resulting configuration in `EnsembleSpec.ensemble_version`.
    """

    deterministic: float = 0.5
    probabilistic: float = 0.5

    def __post_init__(self) -> None:
        _require_within(self.deterministic, 0.0, 1.0, "deterministic")
        _require_within(self.probabilistic, 0.0, 1.0, "probabilistic")
        total = float(self.deterministic) + float(self.probabilistic)
        if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
            raise DecisionConfigError(
                f"Ensemble weights must sum to 1.0, got {total}. The blended score is a "
                "weighted mean of two bounded scores, and only a unit weight sum keeps it "
                "inside [-1, +1] by construction."
            )

    def describe(self) -> Mapping[str, float]:
        """The weights as serializable values, for the audit record."""
        return MappingProxyType(
            {
                "deterministic": float(self.deterministic),
                "probabilistic": float(self.probabilistic),
            }
        )


@dataclass(frozen=True)
class ConfidenceMix:
    """How confidence splits between "each engine was sure" and "the two agreed".

    Two genuinely different facts, which is why neither substitutes for the
    other. Two engines that are each highly confident while pointing opposite
    ways are not evidence for a position, and two engines that barely have an
    opinion are not evidence for one merely by having the same sign. Weighted
    towards the components because an agreement term alone is satisfied by two
    weak readings that happen to share a sign.
    """

    components: float = 0.6
    agreement: float = 0.4

    def __post_init__(self) -> None:
        _require_within(self.components, 0.0, 1.0, "components")
        _require_within(self.agreement, 0.0, 1.0, "agreement")
        total = float(self.components) + float(self.agreement)
        if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
            raise DecisionConfigError(
                f"Confidence mix weights must sum to 1.0, got {total}. The blended "
                "confidence is a weighted mean of quantities in [0, 1], and only a unit "
                "weight sum keeps it inside [0, 1] by construction."
            )

    def describe(self) -> Mapping[str, float]:
        """The mix as serializable values, for the audit record."""
        return MappingProxyType(
            {"components": float(self.components), "agreement": float(self.agreement)}
        )


@dataclass(frozen=True)
class EnsembleBand:
    """The ensemble's own hold band, and the confidence a candidate needs.

    **The band is closed, and that is the difference from V2 and V4.** Those two
    name a direction at `score >= buy_score`; V5 requires `score > buy_score`,
    so a score sitting exactly on the boundary is a HOLD. The rule this
    implements is that a decision near the edge resolves to HOLD rather than to
    whichever side is marginally ahead - an ensemble whose inputs disagree by a
    rounding error has not found a direction, it has found a tie, and a tie is
    the thing a hold band exists to express.

    The confidence floor keeps the shared rule instead: at exactly the floor the
    confidence *is* the minimum required, and diverging from `decide_signal`
    there would be a second convention with no argument behind it.

    Nothing here is `DecisionThresholds`. The band travels with the ensemble
    version rather than with the asset-class policy, because widening it is a
    change to the ensemble and has to be identifiable as one in a stored record.
    `require_not_wider_than_policy` then pins the relationship that matters:
    V5 may refuse where its components would trade, never the reverse.
    """

    buy_score: float = 0.30
    sell_score: float = -0.30
    min_confidence: float = 0.45

    def __post_init__(self) -> None:
        _require_within(self.buy_score, 0.0, 1.0, "buy_score")
        _require_within(self.sell_score, -1.0, 0.0, "sell_score")
        _require_within(self.min_confidence, 0.0, 1.0, "min_confidence")
        if self.sell_score >= self.buy_score:
            raise DecisionConfigError(
                f"sell_score ({self.sell_score}) must be below buy_score ({self.buy_score}); "
                "an inverted or empty hold band would make one score mean both directions."
            )

    def require_not_wider_than_policy(self, thresholds: DecisionThresholds) -> None:
        """Refuse a band that would let V5 trade where its own components hold.

        The ensemble may be more cautious than the policy its components are
        judged under and may not be less. A V5 that entered on a score V2, V3
        and V4 all treat as inside the band would be a loosening of the shipped
        thresholds wearing a combination's clothes, and nothing downstream could
        tell the two apart from the candidate alone.
        """
        if self.buy_score < thresholds.buy_score:
            raise DecisionConfigError(
                f"The ensemble buy band ({self.buy_score}) is below the policy's "
                f"({thresholds.buy_score}). An ensemble may refuse where its components "
                "would trade; it may not trade where they would refuse."
            )
        if self.sell_score > thresholds.sell_score:
            raise DecisionConfigError(
                f"The ensemble sell band ({self.sell_score}) is above the policy's "
                f"({thresholds.sell_score}). An ensemble may refuse where its components "
                "would trade; it may not trade where they would refuse."
            )
        if self.min_confidence < thresholds.min_confidence:
            raise DecisionConfigError(
                f"The ensemble confidence floor ({self.min_confidence}) is below the "
                f"policy's ({thresholds.min_confidence}). Combining two readings is not a "
                "reason to require less conviction than either alone."
            )

    def describe(self) -> Mapping[str, float]:
        """The band as serializable values, for the audit record."""
        return MappingProxyType(
            {
                "buy_score": float(self.buy_score),
                "sell_score": float(self.sell_score),
                "min_confidence": float(self.min_confidence),
                # Recorded so an audit reads the boundary convention off the
                # decision rather than off this module's source at some later
                # version of it.
                "boundary_resolves_to": DecisionSignal.HOLD.value,
            }
        )


@dataclass(frozen=True)
class RegimeAdjustments:
    """How much conviction each market regime leaves intact, in ``[0, 1]``.

    Attenuations, never amplifications, so the regime can take a candidate away
    and can never create one. The five values are ordered rather than free:
    a reading that agrees with the prevailing trend keeps the most, a
    counter-trend reading keeps less, a range keeps less than a trend it agrees
    with, disorder keeps least of the classified states, and an unclassified
    regime keeps no more than a counter-trend one.

    `unknown` is 0.0, which makes an unclassified regime a HOLD by arithmetic
    rather than by a branch: no conviction survives, so no band can be cleared.
    It is unreachable in practice - `classify_regime` always names a state, and
    an unavailable timeframe stops the decision earlier - and it is the right
    answer if it ever stops being unreachable.
    """

    trend_aligned: float = 1.00
    trend_opposed: float = 0.50
    range_bound: float = 0.80
    high_volatility: float = 0.40
    unknown: float = 0.00

    def __post_init__(self) -> None:
        for name in ("trend_aligned", "trend_opposed", "range_bound", "high_volatility", "unknown"):
            _require_within(getattr(self, name), 0.0, 1.0, name)
        if not self.trend_aligned >= self.range_bound >= self.high_volatility:
            raise DecisionConfigError(
                "Regime adjustments must not increase with disorder: trend_aligned "
                f"({self.trend_aligned}) >= range_bound ({self.range_bound}) >= "
                f"high_volatility ({self.high_volatility}). A market whose range has "
                "expanded past its own baseline is not more informative than a trending one."
            )
        if self.trend_opposed > self.trend_aligned:
            raise DecisionConfigError(
                f"trend_opposed ({self.trend_opposed}) must not exceed trend_aligned "
                f"({self.trend_aligned}); a reading against the prevailing trend is not "
                "better evidence than the same reading with it."
            )
        if self.unknown > self.trend_opposed:
            raise DecisionConfigError(
                f"unknown ({self.unknown}) must not exceed trend_opposed "
                f"({self.trend_opposed}); an unclassified regime cannot be treated more "
                "generously than a classified one that disagrees."
            )

    def multiplier(self, regime: MarketRegime, *, lean: float) -> float:
        """How much of a directional `lean` this regime leaves intact.

        A lean of exactly zero under a trend is treated as opposed. It changes
        no score - zero times anything is zero - but it does hold the confidence
        of a bar the two engines could not point on down to the conservative
        value, which is what a flat reading under a live trend deserves.
        """
        if regime is MarketRegime.HIGH_VOLATILITY:
            return float(self.high_volatility)
        if regime is MarketRegime.RANGE:
            return float(self.range_bound)
        if regime is MarketRegime.TREND_UP:
            return float(self.trend_aligned if lean > 0.0 else self.trend_opposed)
        if regime is MarketRegime.TREND_DOWN:
            return float(self.trend_aligned if lean < 0.0 else self.trend_opposed)
        return float(self.unknown)

    def describe(self) -> Mapping[str, float]:
        """The adjustments as serializable values, for the audit record."""
        return MappingProxyType(
            {
                "trend_aligned": float(self.trend_aligned),
                "trend_opposed": float(self.trend_opposed),
                "range_bound": float(self.range_bound),
                "high_volatility": float(self.high_volatility),
                "unknown": float(self.unknown),
            }
        )


@dataclass(frozen=True)
class EnsembleSpec:
    """One complete, named ensemble configuration.

    `ensemble_version` identifies *this* configuration the way a model version
    identifies one fitted model: change a weight or the band and the version
    changes with it, so a stored decision can be matched to the exact ensemble
    that produced it rather than to whatever the defaults happen to be later.

    `requires_calibration` is the one switch, and it defaults to closed. V4
    reports whether its probability was calibrated at all, and an uncalibrated
    logistic score is not a probability - blending it with a deterministic score
    as though it were would be an unstated assumption sitting inside a number
    the layers downstream are entitled to read as odds. A deployment that wants
    to run one anyway sets this False; the flag is recorded with every decision,
    so an audit can tell which regime a candidate was produced under.
    """

    ensemble_version: str
    weights: EnsembleWeights = field(default_factory=EnsembleWeights)
    confidence_mix: ConfidenceMix = field(default_factory=ConfidenceMix)
    band: EnsembleBand = field(default_factory=EnsembleBand)
    regime_adjustments: RegimeAdjustments = field(default_factory=RegimeAdjustments)
    requires_calibration: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.ensemble_version, str) or not self.ensemble_version.strip():
            raise DecisionConfigError(
                "ensemble_version must be a non-empty identifier: a recorded decision has "
                "to name the exact ensemble that produced it."
            )
        if not isinstance(self.requires_calibration, bool):
            raise DecisionConfigError(
                "requires_calibration must be a bool, got "
                f"{type(self.requires_calibration).__name__}."
            )

    def describe(self) -> Mapping[str, object]:
        """The whole specification as serializable values, for the audit record."""
        return MappingProxyType(
            {
                "ensemble_contract_version": ENSEMBLE_CONTRACT_VERSION,
                "ensemble_version": self.ensemble_version,
                "weights": dict(self.weights.describe()),
                "confidence_mix": dict(self.confidence_mix.describe()),
                "band": dict(self.band.describe()),
                "regime_adjustments": dict(self.regime_adjustments.describe()),
                "attenuation_order": list(ATTENUATION_ORDER),
                "requires_calibration": self.requires_calibration,
            }
        )

    def to_record(self) -> dict[str, object]:
        """A JSON-serializable record of this ensemble, for storage and replay."""
        return dict(self.describe())

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> EnsembleSpec:
        """Rebuild a specification from `to_record`, refusing another contract version.

        The version check is the point. A record written under a different
        record *shape* has fields that no longer mean what this code would read
        them as, and reading it anyway would produce an ensemble that runs
        happily and is not the one that was stored.
        """
        stored = record.get("ensemble_contract_version")
        if stored != ENSEMBLE_CONTRACT_VERSION:
            raise DecisionConfigError(
                f"This ensemble contract is {ENSEMBLE_CONTRACT_VERSION}; the record was "
                f"written under {stored!r}. A stored ensemble is not reinterpreted across "
                "contract versions."
            )
        weights = _record_mapping(record, "weights")
        mix = _record_mapping(record, "confidence_mix")
        band = _record_mapping(record, "band")
        adjustments = _record_mapping(record, "regime_adjustments")
        return cls(
            ensemble_version=str(record["ensemble_version"]),
            weights=EnsembleWeights(
                deterministic=float(weights["deterministic"]),  # type: ignore[arg-type]
                probabilistic=float(weights["probabilistic"]),  # type: ignore[arg-type]
            ),
            confidence_mix=ConfidenceMix(
                components=float(mix["components"]),  # type: ignore[arg-type]
                agreement=float(mix["agreement"]),  # type: ignore[arg-type]
            ),
            band=EnsembleBand(
                buy_score=float(band["buy_score"]),  # type: ignore[arg-type]
                sell_score=float(band["sell_score"]),  # type: ignore[arg-type]
                min_confidence=float(band["min_confidence"]),  # type: ignore[arg-type]
            ),
            regime_adjustments=RegimeAdjustments(
                trend_aligned=float(adjustments["trend_aligned"]),  # type: ignore[arg-type]
                trend_opposed=float(adjustments["trend_opposed"]),  # type: ignore[arg-type]
                range_bound=float(adjustments["range_bound"]),  # type: ignore[arg-type]
                high_volatility=float(adjustments["high_volatility"]),  # type: ignore[arg-type]
                unknown=float(adjustments["unknown"]),  # type: ignore[arg-type]
            ),
            requires_calibration=bool(record["requires_calibration"]),
        )


def _record_mapping(record: Mapping[str, object], key: str) -> Mapping[str, object]:
    """One nested mapping out of a stored record, refusing anything else."""
    value = record.get(key)
    if not isinstance(value, Mapping):
        raise DecisionConfigError(
            f"An ensemble record's {key!r} must be a mapping, got {type(value).__name__}."
        )
    return value


#: The shipped ensemble. Equal weights, a band wider than every asset-class
#: policy's, and calibration required.
#:
#: Named for what it is rather than for a tuning run, because there was none.
#: The version string moves whenever any number above it moves, which is what
#: lets a stored decision be matched to a configuration instead of to a date.
BALANCED_ENSEMBLE = EnsembleSpec(ensemble_version="v5-balanced-1.0.0")


@dataclass(frozen=True)
class ComponentContribution:
    """One input's exact, signed effect on one ensemble output.

    `reading` is what the component measured - a score, a confidence, an
    agreement, or an attenuation multiplier. `weight` is the share it carried in
    the blend, and is 1.0 for an attenuation, which carries no share because it
    is not a term in the sum. `contribution` is the amount the output moved
    because of it, and the amounts sum to the output exactly.
    """

    component: str
    reading: float
    weight: float
    contribution: float

    def to_record(self) -> dict[str, object]:
        """A JSON-serializable record of this contribution."""
        return {
            "component": self.component,
            "reading": float(self.reading),
            "weight": float(self.weight),
            "contribution": float(self.contribution),
        }


@dataclass(frozen=True)
class EnsembleAttribution:
    """The complete explanation of one ensemble decision, checked against itself.

    Refuses to exist unless the contributions sum to the outputs they claim to
    explain. An attribution that does not reconstruct its own decision is worse
    than none: it looks like an audit trail, it survives serialization, and it
    is discovered to be wrong only by whoever eventually needs it.
    """

    score: float
    confidence: float
    score_components: tuple[ComponentContribution, ...]
    confidence_components: tuple[ComponentContribution, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "score_components", tuple(self.score_components))
        object.__setattr__(self, "confidence_components", tuple(self.confidence_components))
        object.__setattr__(
            self, "score", _require_bounded(self.score, SCORE_MIN, SCORE_MAX, "score")
        )
        object.__setattr__(
            self,
            "confidence",
            _require_bounded(self.confidence, CONFIDENCE_MIN, CONFIDENCE_MAX, "confidence"),
        )
        _require_components_sum_to(self.score_components, self.score, "score")
        _require_components_sum_to(self.confidence_components, self.confidence, "confidence")

    def score_contributions(self) -> Mapping[str, float]:
        """Each component's signed effect on the score, keyed by component name."""
        return MappingProxyType(
            {item.component: float(item.contribution) for item in self.score_components}
        )

    def confidence_contributions(self) -> Mapping[str, float]:
        """Each component's signed effect on the confidence, keyed by component name."""
        return MappingProxyType(
            {item.component: float(item.contribution) for item in self.confidence_components}
        )

    def confidence_readings(self) -> Mapping[str, float]:
        """What each confidence component measured, keyed by component name.

        The readings rather than their effects: the agreement term, the two
        component confidences, and the two attenuation multipliers as they were
        measured. A caller that wants to describe one of them - the agreement
        token, say - reads it here instead of recomputing it, so a token and the
        contribution it belongs to cannot end up describing two different numbers.
        """
        return MappingProxyType(
            {item.component: float(item.reading) for item in self.confidence_components}
        )

    def to_record(self) -> dict[str, object]:
        """A JSON-serializable record of the whole explanation."""
        return {
            "score": float(self.score),
            "confidence": float(self.confidence),
            "attenuation_order": list(ATTENUATION_ORDER),
            "score_components": [item.to_record() for item in self.score_components],
            "confidence_components": [item.to_record() for item in self.confidence_components],
        }


def _require_components_sum_to(
    components: Sequence[ComponentContribution], total: float, field_name: str
) -> None:
    """Refuse an attribution whose parts do not add up to the whole."""
    summed = sum(float(item.contribution) for item in components)
    if abs(summed - float(total)) > DRIFT_TOLERANCE:
        raise DecisionConfigError(
            f"The {field_name} contributions sum to {summed}, not to the reported "
            f"{field_name} of {total}. An attribution that cannot reconstruct its own "
            "decision is not an audit trail."
        )


def blended_score(
    deterministic_score: float, probabilistic_score: float, weights: EnsembleWeights
) -> float:
    """The two engines' directional readings as one number, before any attenuation.

    A weighted mean of two values in ``[-1, +1]`` with weights summing to one,
    so the result is in ``[-1, +1]`` because of what it is. Exposed separately
    because the regime adjustment has to know which way the blend leans in order
    to decide whether a reading agrees with the prevailing trend, and a caller
    recomputing that sum by hand would be a second copy of the blend.
    """
    total = weights.deterministic * float(deterministic_score) + weights.probabilistic * float(
        probabilistic_score
    )
    return _require_bounded(total, SCORE_MIN, SCORE_MAX, "blended_score")


def component_agreement(deterministic_score: float, probabilistic_score: float) -> float:
    """How far the two engines point the same way, in ``[0, 1]``.

    ``(1 + ab) / 2`` over two scores in ``[-1, +1]``, so the result is bounded
    by construction and continuous in both. Two strong readings of the same sign
    approach 1, two opposed readings approach 0, and a component with no opinion
    leaves the pair at exactly a half - which is the honest answer, because one
    engine saying nothing is neither corroboration nor contradiction.
    """
    product = float(deterministic_score) * float(probabilistic_score)
    return _require_bounded((1.0 + product) / 2.0, 0.0, 1.0, "component_agreement")


def agreement_reason(agreement: float) -> str:
    """The stable token naming whether the two engines corroborated each other."""
    if agreement > NEUTRAL_AGREEMENT:
        return REASON_COMPONENTS_AGREE
    if agreement < NEUTRAL_AGREEMENT:
        return REASON_COMPONENTS_DISAGREE
    return "ENSEMBLE_COMPONENTS_NEUTRAL"


def combine_regimes(context: MarketRegime, base: MarketRegime) -> MarketRegime:
    """The regime a decision made on two timescales was made under.

    Disorder on either scale is disorder: a 4-hour range expansion and a
    15-minute one are both reasons not to enter, and taking only the broader
    reading would let a violent bar be scored as though the hour it sits in were
    calm. An unclassified reading on either scale leaves the pair unclassified
    for the same reason. Otherwise the broad context governs, which is what
    "regime" has meant in V3 since it was written.
    """
    pair = (context, base)
    if MarketRegime.HIGH_VOLATILITY in pair:
        return MarketRegime.HIGH_VOLATILITY
    if MarketRegime.UNKNOWN in pair:
        return MarketRegime.UNKNOWN
    return context


def combine(
    *,
    deterministic_score: float,
    deterministic_confidence: float,
    probabilistic_score: float,
    probabilistic_confidence: float,
    regime_multiplier: float,
    volatility_multiplier: float,
    spec: EnsembleSpec,
) -> EnsembleAttribution:
    """Blend two readings under `spec`, attenuate by context, and explain the result.

    Four steps, in this order, and the order is why the contributions are exact.
    The two scores are blended, the blend is attenuated by the regime, that is
    attenuated by the volatility reading, and each stage's contribution is the
    difference it made along that chain. Applying the attenuations in the other
    order would produce the same output and different contributions, which is
    why `ATTENUATION_ORDER` is written down and recorded.
    """
    weights = spec.weights
    mix = spec.confidence_mix

    deterministic = float(deterministic_score)
    probabilistic = float(probabilistic_score)
    regime = _require_within(regime_multiplier, 0.0, 1.0, "regime_multiplier")
    volatility = _require_within(volatility_multiplier, 0.0, 1.0, "volatility_multiplier")

    blended = blended_score(deterministic, probabilistic, weights)
    agreement = component_agreement(deterministic, probabilistic)
    blended_confidence = (
        mix.components
        * (
            weights.deterministic * float(deterministic_confidence)
            + weights.probabilistic * float(probabilistic_confidence)
        )
        + mix.agreement * agreement
    )

    score_components = (
        ComponentContribution(
            component=COMPONENT_DETERMINISTIC,
            reading=deterministic,
            weight=weights.deterministic,
            contribution=weights.deterministic * deterministic,
        ),
        ComponentContribution(
            component=COMPONENT_PROBABILISTIC,
            reading=probabilistic,
            weight=weights.probabilistic,
            contribution=weights.probabilistic * probabilistic,
        ),
        *_attenuation_contributions(blended, regime=regime, volatility=volatility),
    )
    confidence_components = (
        ComponentContribution(
            component=COMPONENT_DETERMINISTIC,
            reading=float(deterministic_confidence),
            weight=mix.components * weights.deterministic,
            contribution=mix.components * weights.deterministic * float(deterministic_confidence),
        ),
        ComponentContribution(
            component=COMPONENT_PROBABILISTIC,
            reading=float(probabilistic_confidence),
            weight=mix.components * weights.probabilistic,
            contribution=mix.components * weights.probabilistic * float(probabilistic_confidence),
        ),
        ComponentContribution(
            component=COMPONENT_AGREEMENT,
            reading=agreement,
            weight=mix.agreement,
            contribution=mix.agreement * agreement,
        ),
        *_attenuation_contributions(blended_confidence, regime=regime, volatility=volatility),
    )
    return EnsembleAttribution(
        score=blended * regime * volatility,
        confidence=blended_confidence * regime * volatility,
        score_components=score_components,
        confidence_components=confidence_components,
    )


def _attenuation_contributions(
    blended: float, *, regime: float, volatility: float
) -> tuple[ComponentContribution, ...]:
    """The two attenuation stages' effects on `blended`, as chain differences.

    ``blended*r - blended`` and then ``blended*r*v - blended*r``, so the two
    added to `blended` telescope exactly to ``blended*r*v``. Both are negative
    whenever `blended` is positive and either multiplier is below one, which is
    the honest sign: an attenuation removed conviction, it did not add any.
    """
    return (
        ComponentContribution(
            component=COMPONENT_REGIME,
            reading=regime,
            weight=1.0,
            contribution=blended * (regime - 1.0),
        ),
        ComponentContribution(
            component=COMPONENT_VOLATILITY,
            reading=volatility,
            weight=1.0,
            contribution=blended * regime * (volatility - 1.0),
        ),
    )


def decide_candidate(
    *,
    score: float,
    confidence: float,
    regime: MarketRegime,
    band: EnsembleBand,
) -> tuple[DecisionSignal, tuple[str, ...]]:
    """Turn an ensemble score into a candidate, or into the reason there is none.

    Gate order is the contract, and it is V2's: confidence first, so an
    unconvincing reading is reported as unconvincing rather than as a blocked
    entry; then the buy side, where a high-volatility regime still refuses; then
    the sell side, which nothing here refuses; then the band.

    The asymmetry is inherited rather than restated. Entering into disorder is
    optional; declining to say "reduce" because the market has become disorderly
    is the engine going quiet exactly when a position is most exposed, and the
    layers downstream treat a SELL with nothing to reduce as an ordinary
    no-order outcome.

    A candidate, and only a candidate. Nothing here sizes it, prices it, or
    approves it, and the risk engine remains the sole authority on whether it
    ever becomes an order.
    """
    if confidence < band.min_confidence:
        return DecisionSignal.HOLD, (REASON_LOW_CONFIDENCE,)
    if score > band.buy_score:
        if regime is MarketRegime.HIGH_VOLATILITY:
            return DecisionSignal.HOLD, (REASON_REGIME_BLOCKED,)
        return DecisionSignal.BUY, (REASON_ENSEMBLE_BUY,)
    if score < band.sell_score:
        return DecisionSignal.SELL, (REASON_ENSEMBLE_SELL,)
    return DecisionSignal.HOLD, (REASON_HOLD_BAND,)


def contribution_reasons(contributions: Iterable[ComponentContribution]) -> tuple[str, ...]:
    """One token per component that actually moved the score, largest effect first.

    Sorted by absolute contribution and then by name, so two components of equal
    influence are reported in a stable order rather than in whichever order the
    sequence happened to be built. Components that moved nothing are omitted:
    a token saying an input did not matter is true and is not an explanation.
    """
    ranked = sorted(contributions, key=lambda item: (-abs(item.contribution), item.component))
    return tuple(
        component_reason(item.component, item.contribution)
        for item in ranked
        if item.contribution != 0.0
    )


__all__ = [
    "ATTENUATION_ORDER",
    "BALANCED_ENSEMBLE",
    "COMPONENT_AGREEMENT",
    "COMPONENT_DETERMINISTIC",
    "COMPONENT_PROBABILISTIC",
    "COMPONENT_REGIME",
    "COMPONENT_VOLATILITY",
    "DRIFT_TOLERANCE",
    "ENSEMBLE_CONTRACT_VERSION",
    "NEUTRAL_AGREEMENT",
    "REASON_COMPONENTS_AGREE",
    "REASON_COMPONENTS_DISAGREE",
    "REASON_ENSEMBLE_BUY",
    "REASON_ENSEMBLE_SELL",
    "REASON_UNCALIBRATED_MODEL",
    "ComponentContribution",
    "ConfidenceMix",
    "EnsembleAttribution",
    "EnsembleBand",
    "EnsembleSpec",
    "EnsembleWeights",
    "RegimeAdjustments",
    "agreement_reason",
    "blended_score",
    "combine",
    "combine_regimes",
    "component_agreement",
    "component_reason",
    "contribution_reasons",
    "decide_candidate",
]
