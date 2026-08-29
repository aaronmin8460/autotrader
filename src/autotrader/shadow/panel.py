"""Five versions decide one bar; exactly one of them may be a candidate.

This module is the arithmetic-free half of shadow mode: it runs a set of
decision engines over one completed bar and sorts their answers into **one**
possible execution candidate and a set of observations. It computes no score,
holds no threshold, and reverses no decision - every number here came out of
`autotrader.decision`, which is the only thing in this system that decides
anything.

**Why the candidate is a type rather than a flag.** The obvious shape for this
is a list of results with an `executed` boolean on each, and the obvious bug in
that shape is a caller that reads the wrong element. `ExecutionCandidate`
instead carries the configured execution version alongside the result and
refuses to exist when the two disagree, so a candidate naming an observational
version is not a mistake that gets caught downstream - it is a value that cannot
be constructed. `PanelEvaluation.candidate` returns one optional candidate and
there is no API anywhere that returns two, which is the other half of the same
property: five evaluations cost one execution decision because one is the most
the shape can express.

**HOLD is not a candidate.** The configured version returning HOLD releases
nothing, so there is nothing to execute and `candidate` is None. That is the
same answer as "the configured version could not decide this bar", and both are
distinguishable in the record by the reasons the decision carries.

**An observational engine cannot break the execution path.** Each version is
evaluated independently and a controlled `DecisionError` from one is captured as
a `ShadowFailure` rather than propagated, because a shadow version that can
abort the cycle is not observational - it is a fifth way to lose a trade. The
converse is deliberately not softened: when the *configured* version fails there
is simply no candidate, which misses a trade rather than executing a decision
that was never made. Anything that is not a `DecisionError` - a genuine defect
rather than a controlled refusal - still propagates, because hiding those would
buy reliability with silence.

Nothing here writes anything, and nothing here can reach the layers downstream.
`recorder.py` persists what this produces; the risk engine remains the sole
authority over whether a candidate ever becomes an order.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import pandas as pd

from autotrader.decision.contract import (
    DecisionEngine,
    DecisionError,
    DecisionResult,
    DecisionSignal,
)

#: Where an engine's `describe()` records the feature contract it reads. Every
#: version that computes standardized features publishes this key; V1 does not,
#: because the EMA crossover has no feature schema to version.
FEATURE_VERSION_KEY = "feature_schema_version"

#: Where an engine's `describe()` records the identity of a trained model. V4
#: publishes it at the top level; V5 is an ensemble and publishes its model
#: under the component that holds one, so both places are read.
MODEL_VERSION_KEY = "model_version"
COMPONENTS_KEY = "components"
PROBABILISTIC_COMPONENT_KEY = "probabilistic"


class ShadowError(Exception):
    """Base class for every controlled failure in shadow mode."""


class ShadowConfigError(ShadowError):
    """A panel or a candidate was configured in a way it will not exist with."""


class ShadowEvaluationError(ShadowError):
    """A bar could not be evaluated by any configured version."""


def feature_version_of(result: DecisionResult) -> str | None:
    """The feature contract version behind `result`, or None when it has none.

    Read from the decision's own policy record rather than from the engine that
    made it, because the policy record is what survives into storage: a stored
    decision and this function together answer "which features was this scored
    from" without needing the engine object that produced it to still exist.

    None is an answer, not a gap. V1 is a crossover of two moving averages and
    computes no standardized feature schema, so it has no feature version, and
    inventing one for it would falsify the provenance of every V1 row.
    """
    version = result.policy.get(FEATURE_VERSION_KEY)
    return None if version is None else str(version)


def model_version_of(result: DecisionResult) -> str | None:
    """The trained model behind `result`, or None when no model was involved.

    Two places, because two versions carry a model differently: V4 *is* the
    model and names it directly, while V5 blends a deterministic score with a
    model's probability and names it under the component that holds one. A rule
    that only looked at the top level would silently record every V5 decision as
    modelless, which is the one thing an ensemble is not.
    """
    version = result.policy.get(MODEL_VERSION_KEY)
    if version is not None:
        return str(version)
    components = result.policy.get(COMPONENTS_KEY)
    if isinstance(components, Mapping):
        component = components.get(PROBABILISTIC_COMPONENT_KEY)
        if isinstance(component, Mapping):
            nested = component.get(MODEL_VERSION_KEY)
            if nested is not None:
                return str(nested)
    return None


@dataclass(frozen=True)
class ExecutionCandidate:
    """The one decision a panel is willing to offer the layers downstream.

    Constructible only for the configured execution version, and only from an
    actionable decision. Those two checks are what makes "the other four cannot
    produce a candidate" a property of the type rather than a rule someone has
    to remember: there is no argument list that builds a candidate for an
    observational version, so no caller - including a future one that stops
    reading this docstring - can assemble one by accident.

    It names a symbol, a bar and a direction, and deliberately nothing else. No
    quantity, no price, no account, no order. Sizing belongs to the risk engine,
    which remains the only thing that can turn this into an order, and a
    candidate that arrived carrying a quantity would be an instruction wearing a
    suggestion's clothes.
    """

    result: DecisionResult
    execution_version: str

    def __post_init__(self) -> None:
        if not self.execution_version:
            raise ShadowConfigError("execution_version must be a non-empty identifier.")
        if self.result.version != self.execution_version:
            raise ShadowConfigError(
                f"Engine version {self.result.version!r} cannot produce an execution "
                f"candidate: the configured execution version is "
                f"{self.execution_version!r}. Every other version is observational, and "
                "a candidate from one would be an execution nobody configured."
            )
        if not self.result.is_actionable:
            raise ShadowConfigError(
                f"The {self.result.version!r} decision for {self.result.symbol} at "
                f"{self.result.timestamp.isoformat()} is HOLD, which names no direction. "
                "A candidate is a direction to be sized and refused or approved; a HOLD "
                "is the absence of one, not a zero-sized version of one."
            )

    @property
    def version(self) -> str:
        """The engine version that produced this candidate."""
        return self.result.version

    @property
    def symbol(self) -> str:
        return self.result.symbol

    @property
    def timestamp(self) -> pd.Timestamp:
        """The start of the completed bar this candidate was decided on."""
        return self.result.timestamp

    @property
    def signal(self) -> DecisionSignal:
        """The direction. BUY or SELL; never HOLD, which cannot be a candidate."""
        return self.result.signal


@dataclass(frozen=True)
class ShadowObservation:
    """One version's decision about one bar, and whether it was released.

    `executed` is true for exactly one observation in an evaluation at most: the
    configured execution version, on a bar where it named a direction. It says
    the decision was *released* to the layers downstream, never that an order
    exists - risk, the account gates and reconciliation all still stand between
    a candidate and a broker, and this value is computed before any of them have
    been asked.
    """

    result: DecisionResult
    execution_version: str

    def __post_init__(self) -> None:
        if not self.execution_version:
            raise ShadowConfigError("execution_version must be a non-empty identifier.")

    @property
    def version(self) -> str:
        return self.result.version

    @property
    def is_execution_version(self) -> bool:
        """Whether this version is the one configured to execute."""
        return self.result.version == self.execution_version

    @property
    def executed(self) -> bool:
        """Whether this decision became the bar's one execution candidate."""
        return self.is_execution_version and self.result.is_actionable

    @property
    def feature_version(self) -> str | None:
        return feature_version_of(self.result)

    @property
    def model_version(self) -> str | None:
        return model_version_of(self.result)

    def candidate(self) -> ExecutionCandidate:
        """This observation as a candidate, which only the executed one can be."""
        return ExecutionCandidate(result=self.result, execution_version=self.execution_version)


@dataclass(frozen=True)
class ShadowFailure:
    """A version that refused to decide a bar, and what it said.

    Not a decision and not stored as one. An engine that cannot be applied to a
    bar at all - a model whose features are missing, a frame it will not accept -
    has produced no answer, and writing a synthetic HOLD row for it would put a
    decision nobody made into a table meant for comparing decisions. Ordinary
    "I cannot tell" is not this: every engine expresses that as a HOLD carrying
    its reason, which is a real decision and is recorded like any other.
    """

    version: str
    error: str


@dataclass(frozen=True)
class PanelEvaluation:
    """What every configured version decided about one symbol's completed bar.

    The invariants are checked here rather than assumed, because this is the
    value everything else reads: every observation is about the same symbol and
    the same bar, no version appears twice, and **at most one observation is
    executed**. The last of those is a consequence of how observations are built
    and is asserted anyway - it is the property the whole feature exists to
    guarantee, and a guarantee nobody checks is a comment.
    """

    symbol: str
    timestamp: pd.Timestamp
    execution_version: str
    observations: tuple[ShadowObservation, ...]
    failures: tuple[ShadowFailure, ...] = ()

    def __post_init__(self) -> None:
        if not self.observations:
            raise ShadowEvaluationError(
                "A panel evaluation must hold at least one decision; an evaluation of "
                "nothing has no bar to be about."
            )
        versions = [observation.version for observation in self.observations]
        if len(set(versions)) != len(versions):
            raise ShadowConfigError(
                f"Two observations share an engine version: {', '.join(sorted(versions))}. "
                "One version decides one bar once."
            )
        for observation in self.observations:
            if observation.execution_version != self.execution_version:
                raise ShadowConfigError(
                    f"Observation {observation.version!r} was built against execution "
                    f"version {observation.execution_version!r}, but this evaluation is "
                    f"configured for {self.execution_version!r}."
                )
            if observation.result.symbol != self.symbol:
                raise ShadowConfigError(
                    f"Observation {observation.version!r} decided "
                    f"{observation.result.symbol!r}, not {self.symbol!r}. Five versions "
                    "comparing decisions must have decided the same thing."
                )
            if observation.result.timestamp != self.timestamp:
                raise ShadowConfigError(
                    f"Observation {observation.version!r} decided the bar at "
                    f"{observation.result.timestamp.isoformat()}, not "
                    f"{self.timestamp.isoformat()}."
                )
        executed = [observation for observation in self.observations if observation.executed]
        if len(executed) > 1:
            raise ShadowConfigError(
                f"{len(executed)} observations are marked executed for {self.symbol} at "
                f"{self.timestamp.isoformat()}. One completed bar yields at most one "
                "execution, however many versions decided on it."
            )

    @property
    def versions(self) -> tuple[str, ...]:
        """Every version that produced a decision, in evaluation order."""
        return tuple(observation.version for observation in self.observations)

    @property
    def executed_observation(self) -> ShadowObservation | None:
        """The one released decision, or None when nothing was released."""
        for observation in self.observations:
            if observation.executed:
                return observation
        return None

    @property
    def observational(self) -> tuple[ShadowObservation, ...]:
        """Every decision that was recorded without being allowed to act."""
        return tuple(observation for observation in self.observations if not observation.executed)

    @property
    def candidate(self) -> ExecutionCandidate | None:
        """The bar's single execution candidate, or None when there is none.

        None means one of three things, all of which are the same instruction to
        the caller - do nothing: the configured version held, it failed to decide
        this bar, or it was not among the versions that answered.
        """
        observation = self.executed_observation
        return None if observation is None else observation.candidate()

    def observation_for(self, version: str) -> ShadowObservation | None:
        """One version's decision, or None when that version did not answer."""
        for observation in self.observations:
            if observation.version == version:
                return observation
        return None


class EnginePanel:
    """A fixed set of decision engines, one of which may execute.

    The execution version is a required argument with no default. There is no
    "usual" answer to which engine trades: picking one is the entire decision
    shadow mode exists to inform, and a default here would make it silently for
    whoever forgot to pass it.

    The panel holds engines and evaluates them. It cannot write, cannot size,
    cannot submit, and holds no connection to anything - `ShadowRecorder` is
    what persists an evaluation, and the two are separate so that recording a
    decision never requires the ability to act on one.
    """

    def __init__(
        self,
        engines: Sequence[DecisionEngine],
        *,
        execution_version: str,
    ) -> None:
        if not engines:
            raise ShadowConfigError("A panel must hold at least one decision engine.")
        if not execution_version:
            raise ShadowConfigError("execution_version must be a non-empty identifier.")
        versions = [engine.version for engine in engines]
        duplicates = sorted({version for version in versions if versions.count(version) > 1})
        if duplicates:
            raise ShadowConfigError(
                f"A panel cannot hold two engines of the same version: {', '.join(duplicates)}. "
                "Two engines answering under one identifier would produce two rows that "
                "claim to be the same decision."
            )
        if execution_version not in versions:
            raise ShadowConfigError(
                f"The configured execution version {execution_version!r} is not in this "
                f"panel, which holds {', '.join(versions)}. A panel that cannot run the "
                "version it is supposed to execute would observe five engines and trade "
                "on none of them without saying so."
            )
        self._engines = tuple(engines)
        self._execution_version = execution_version

    @property
    def engines(self) -> tuple[DecisionEngine, ...]:
        """The engines in evaluation order."""
        return self._engines

    @property
    def versions(self) -> tuple[str, ...]:
        """Every version this panel evaluates, in evaluation order."""
        return tuple(engine.version for engine in self._engines)

    @property
    def execution_version(self) -> str:
        """The one version allowed to produce an execution candidate."""
        return self._execution_version

    @property
    def observational_versions(self) -> tuple[str, ...]:
        """Every version that is recorded and can never be acted on."""
        return tuple(version for version in self.versions if version != self._execution_version)

    @property
    def required_base_bars(self) -> int:
        """Completed base bars before every version in the panel can answer.

        The most expensive version's requirement, because a panel that fetched
        for the cheapest would record four HOLDs about insufficient history and
        call it a comparison.
        """
        return max(engine.required_base_bars for engine in self._engines)

    def describe(self) -> Mapping[str, object]:
        """The panel's configuration, as serializable values."""
        return {
            "versions": list(self.versions),
            "execution_version": self._execution_version,
            "observational_versions": list(self.observational_versions),
            "required_base_bars": self.required_base_bars,
        }

    def evaluate(self, bars: pd.DataFrame) -> PanelEvaluation:
        """Run every version on the newest completed bar in `bars`.

        One frame, evaluated once per version. The engines share their input and
        share nothing else: each is a pure function of those bars, so the order
        they run in cannot change what any of them says, and the panel does not
        promise an order beyond the one it was constructed with.

        Costs five evaluations and produces at most one candidate. That is the
        trade the whole feature is: measuring five engines is cheap because a
        decision is arithmetic over a frame already in memory, while acting on
        one is not, so the expensive half stays singular.
        """
        observations: list[ShadowObservation] = []
        failures: list[ShadowFailure] = []
        for engine in self._engines:
            try:
                result = engine.decide(bars)
            except DecisionError as error:
                failures.append(ShadowFailure(version=engine.version, error=str(error)))
                continue
            observations.append(
                ShadowObservation(result=result, execution_version=self._execution_version)
            )
        if not observations:
            raise ShadowEvaluationError(
                "No version in this panel could decide the supplied bars: "
                f"{_describe_failures(failures)}. Nothing was recorded and nothing was "
                "released."
            )
        first = observations[0].result
        return PanelEvaluation(
            symbol=first.symbol,
            timestamp=first.timestamp,
            execution_version=self._execution_version,
            observations=tuple(observations),
            failures=tuple(failures),
        )


def _describe_failures(failures: Iterable[ShadowFailure]) -> str:
    return "; ".join(f"{failure.version}: {failure.error}" for failure in failures)


__all__ = [
    "COMPONENTS_KEY",
    "FEATURE_VERSION_KEY",
    "MODEL_VERSION_KEY",
    "PROBABILISTIC_COMPONENT_KEY",
    "EnginePanel",
    "ExecutionCandidate",
    "PanelEvaluation",
    "ShadowConfigError",
    "ShadowError",
    "ShadowEvaluationError",
    "ShadowFailure",
    "ShadowObservation",
    "feature_version_of",
    "model_version_of",
]
