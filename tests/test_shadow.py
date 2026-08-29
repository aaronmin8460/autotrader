"""Shadow mode tests: observe every version, execute exactly one.

**Every test here is offline and instantaneous.** No socket is opened, no
credential is read, and the only file written is a database in pytest's
temporary directory. That is not incidental - the package under test is supposed
to be unable to reach a broker, and a test suite that needed one would be
evidence against it.

The load-bearing tests are the ones about not acting. Five engines producing
five opinions is easy; the properties worth proving are the ones that stop the
extra four costing anything:

*Only one can execute.* A version that is not the configured one cannot produce
a candidate, and the proof is not that nothing tried - the fixtures are chosen so
that on one bar V1 wants to buy while the others hold, and on another the others
want to buy while V1 holds. Configuring the version that is holding yields no
candidate **while four engines are asking to trade**, which is the case a
convention-based implementation would get wrong.

*Only one can execute, even in the database.* The panel's refusal is in Python
and the schema's is in SQL, and the SQL half is tested by writing raw INSERTs
that bypass every line of this feature's code.

*A replayed bar cannot multiply orders.* Three independent mechanisms have to
fail for that: the bar claim, the panel, and the unique index. Each is tested
with the other two removed.

*The recorder cannot reach a broker.* Asserted against the import graph rather
than against intentions.

Storage-level constraints are exercised here rather than in
`test_state_sqlite.py` because they are this feature's safety argument and
reading them next to the layer that depends on them is worth more than filing
them by table. The v6 -> v7 migration is in `test_state_migration.py` with the
rest of the migration path.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import math
import socket
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from autotrader.decision.contract import (
    VERSION_V1,
    VERSION_V2,
    VERSION_V3,
    VERSION_V4,
    VERSION_V5,
    DecisionInputError,
    DecisionResult,
    DecisionSignal,
    MarketRegime,
)
from autotrader.decision.features import FEATURE_SCHEMA_VERSION
from autotrader.decision.probability import (
    V4_FEATURE_COLUMNS,
    FeatureStandardizer,
    IsotonicCalibration,
    LogisticEstimator,
    ProbabilityArtifact,
    TrainingWindow,
)
from autotrader.decision.v1 import EmaCrossV1Engine
from autotrader.decision.v2 import MultiFactorV2Engine
from autotrader.runtime.checkpoint import (
    InMemoryCheckpoint,
    ProcessedBarCheckpoint,
    SqliteCheckpoint,
)
from autotrader.shadow import cycle as shadow_cycle
from autotrader.shadow import panel as shadow_panel
from autotrader.shadow import recorder as shadow_recorder
from autotrader.shadow import versions as shadow_versions
from autotrader.shadow.cycle import (
    SKIPPED_ALREADY_PROCESSED,
    BarClaim,
    ShadowClaimError,
    ShadowCycle,
)
from autotrader.shadow.panel import (
    EnginePanel,
    ExecutionCandidate,
    PanelEvaluation,
    ShadowConfigError,
    ShadowEvaluationError,
    ShadowObservation,
    feature_version_of,
    model_version_of,
)
from autotrader.shadow.recorder import ShadowRecorder
from autotrader.shadow.versions import PANEL_VERSIONS, panel_for_symbol
from autotrader.state import sqlite as state
from autotrader.state.sqlite import (
    SHADOW_DESIGNATION_EXECUTED,
    SHADOW_DESIGNATION_NOT_EXECUTED,
    ConflictingExecutedDecisionError,
    DuplicateShadowDecisionError,
    StateInputError,
    UnknownShadowDecisionError,
    connect,
    get_shadow_decision,
    initialize_database,
    list_order_intents,
    list_shadow_decisions,
    record_shadow_decision,
    record_strategy_run,
)
from test_runtime import code_without_prose

BTC = "BTC/USD"
FIRST_BAR = datetime(2025, 1, 1, tzinfo=UTC)
STEP = timedelta(minutes=15)

#: The bar at which the choppy series below has a fresh V1 crossover, found by
#: running C3 over it. Every version has more than its required history by then,
#: so all five answer and the disagreement is a real one rather than four
#: engines reporting insufficient warm-up.
V1_CROSSOVER_BARS = 1844


# ==========================================================================
# Fixtures. Two bar sets, chosen so the versions disagree in both directions.
# ==========================================================================


def make_bars(closes: list[float], *, symbol: str = BTC) -> pd.DataFrame:
    """A canonical frame over `closes`, one 15-minute bar each."""
    count = len(closes)
    return pd.DataFrame(
        {
            "timestamp": [FIRST_BAR + STEP * index for index in range(count)],
            "symbol": [symbol] * count,
            "open": closes,
            "high": [close + 0.5 for close in closes],
            "low": [close - 0.5 for close in closes],
            "close": closes,
            "volume": [100.0] * count,
        }
    )


def choppy(count: int = V1_CROSSOVER_BARS) -> list[float]:
    """A deterministic oscillating path. No library randomness anywhere."""
    return [
        500.0 + 20.0 * math.sin(index / 37.0) + 6.0 * math.cos(index / 5.0)
        for index in range(count)
    ]


def rising(count: int = 1800, step: float = 0.05) -> list[float]:
    """A steady climb. The trend versions buy; V1 crossed long ago and holds."""
    return [100.0 + step * index for index in range(count)]


def v1_buys_bars() -> pd.DataFrame:
    """Bars where **V1 buys and every other version holds**.

    The interesting direction for this feature: configuring an observational
    version here must produce no candidate even though V1 is asking to trade.
    """
    return make_bars(choppy())


def others_buy_bars() -> pd.DataFrame:
    """Bars where **V2 to V5 buy and V1 holds**.

    The mirror case. Configuring V1 here must produce no candidate even though
    four versions are asking to trade.
    """
    return make_bars(rising())


def last_bar_of(bars: pd.DataFrame) -> datetime:
    """The start of the newest bar in `bars`, as an aware datetime."""
    return pd.Timestamp(bars["timestamp"].iloc[-1]).to_pydatetime().replace(tzinfo=UTC)


def artifact(**overrides: object) -> ProbabilityArtifact:
    """A valid, calibrated model, so the shipped ensemble accepts it."""
    fields: dict[str, object] = {
        "model_version": "shadow-test-1",
        "feature_version": FEATURE_SCHEMA_VERSION,
        "feature_columns": V4_FEATURE_COLUMNS,
        "label_spec_id": "v4-direction-abcdef123456",
        "standardizer": FeatureStandardizer.identity(len(V4_FEATURE_COLUMNS)),
        "estimator": LogisticEstimator(
            intercept=0.0,
            coefficients=tuple([0.4] + [0.0] * (len(V4_FEATURE_COLUMNS) - 1)),
        ),
        "calibration": IsotonicCalibration(thresholds=(0.0, 0.35, 0.65), values=(0.05, 0.5, 0.95)),
        "training_window": TrainingWindow(
            first_feature_timestamp=FIRST_BAR.isoformat(),
            last_feature_timestamp=(FIRST_BAR + STEP * 500).isoformat(),
            rows=500,
            symbols=(BTC,),
            asset_class="crypto",
        ),
        "trained_at_utc": "2025-06-01T00:00:00+00:00",
        "code_revision": {"branch": "feat/decision-shadow", "sha": "0" * 40, "dirty": False},
        "hyperparameters": {"l2": 1.0},
        "seed": 7,
    }
    fields.update(overrides)
    return ProbabilityArtifact(**fields)  # type: ignore[arg-type]


def panel(execution_version: str = VERSION_V1, *, symbol: str = BTC) -> EnginePanel:
    """All five versions, with one of them configured to execute."""
    return panel_for_symbol(symbol, artifact=artifact(), execution_version=execution_version)


def build_result(**overrides: object) -> DecisionResult:
    """A valid `DecisionResult`, with fields replaced by keyword."""
    fields: dict[str, object] = {
        "version": VERSION_V2,
        "symbol": BTC,
        "timestamp": pd.Timestamp(FIRST_BAR),
        "signal": DecisionSignal.BUY,
        "score": 0.6,
        "confidence": 0.7,
        "reasons": ("SCORE_ABOVE_BUY_THRESHOLD",),
        "features": {"ema_spread_z": 1.0},
        "policy": {"policy_name": "test"},
        "regime": MarketRegime.TREND_UP,
    }
    fields.update(overrides)
    return DecisionResult(**fields)  # type: ignore[arg-type]


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    """An initialized state database, closed when the test finishes."""
    path = initialize_database(tmp_path / "shadow.db")
    with connect(path) as open_connection:
        yield open_connection


@pytest.fixture
def run_id(connection: sqlite3.Connection) -> int:
    return record_strategy_run(
        connection, strategy_name="shadow-panel", mode="PAPER", started_at=FIRST_BAR
    )


def make_cycle(
    connection: sqlite3.Connection,
    *,
    execution_version: str = VERSION_V1,
    strategy_run_id: int | None = None,
    checkpoint: BarClaim | None = None,
) -> ShadowCycle:
    return ShadowCycle(
        panel=panel(execution_version),
        recorder=ShadowRecorder(connection, strategy_run_id=strategy_run_id),
        checkpoint=checkpoint if checkpoint is not None else InMemoryCheckpoint(),
    )


class RecordingExecution:
    """Stands where the execution layer stands, and counts.

    Not the real execution path and not a fake of it: there is no risk engine
    here, no gate, and no broker. It exists to answer one question - how many
    times was a candidate handed onward, and how many durable intents resulted -
    which is the question "one bar, one intent" is about. The real path is
    tested in `test_execution_paper.py` and is unchanged by this branch.
    """

    def __init__(self, connection: sqlite3.Connection, strategy_run_id: int | None = None) -> None:
        self._connection = connection
        self._strategy_run_id = strategy_run_id
        self.candidates: list[ExecutionCandidate] = []

    def submit(self, candidate: ExecutionCandidate) -> str:
        self.candidates.append(candidate)
        client_order_id = f"autotrader-shadow-{len(self.candidates)}"
        state.record_order_intent(
            self._connection,
            client_order_id=client_order_id,
            strategy_run_id=self._strategy_run_id,
            created_at=candidate.timestamp.to_pydatetime(),
            symbol=candidate.symbol,
            side="BUY" if candidate.signal is DecisionSignal.BUY else "SELL",
            requested_quantity=Decimal("1"),
            approved_quantity=Decimal("1"),
            reference_price=500.0,
            risk_reason_code="APPROVED",
        )
        return client_order_id


def drive(
    cycle: ShadowCycle,
    execution: RecordingExecution,
    bars: pd.DataFrame,
    *,
    symbol: str = BTC,
) -> shadow_cycle.BarOutcome:
    """One completed-bar turn, shaped the way a runtime would call it.

    Evaluate, record, and hand the single candidate - if there is one - onward,
    then anchor the executed decision to the intent that resulted.
    """
    outcome = cycle.evaluate_bar(symbol, bars, bar_timestamp=last_bar_of(bars))
    candidate = outcome.candidate
    if candidate is not None:
        client_order_id = execution.submit(candidate)
        cycle.recorder.link_execution(candidate, client_order_id=client_order_id)
    return outcome


# ==========================================================================
# Five versions decide one bar
# ==========================================================================


def test_one_bar_produces_a_decision_from_every_version() -> None:
    """CRITICAL. Five versions, one frame, five recorded opinions."""
    evaluation = panel().evaluate(v1_buys_bars())

    assert evaluation.versions == PANEL_VERSIONS
    assert len(evaluation.observations) == 5
    assert evaluation.failures == ()


def test_every_version_decided_the_same_symbol_and_the_same_bar() -> None:
    """A comparison across versions is only a comparison if it is about one bar."""
    bars = v1_buys_bars()
    evaluation = panel().evaluate(bars)

    assert evaluation.symbol == BTC
    assert evaluation.timestamp == pd.Timestamp(last_bar_of(bars))
    for observation in evaluation.observations:
        assert observation.result.symbol == BTC
        assert observation.result.timestamp == evaluation.timestamp


def test_the_versions_genuinely_disagree_on_the_fixture_bar() -> None:
    """The fixture is load-bearing: a panel that always agrees proves nothing."""
    evaluation = panel().evaluate(v1_buys_bars())
    signals = {
        observation.version: observation.result.signal for observation in evaluation.observations
    }

    assert signals[VERSION_V1] is DecisionSignal.BUY
    assert all(
        signals[version] is DecisionSignal.HOLD
        for version in (VERSION_V2, VERSION_V3, VERSION_V4, VERSION_V5)
    )


def test_the_mirror_fixture_has_the_other_four_asking_to_trade() -> None:
    evaluation = panel().evaluate(others_buy_bars())
    signals = {
        observation.version: observation.result.signal for observation in evaluation.observations
    }

    assert signals[VERSION_V1] is DecisionSignal.HOLD
    assert all(
        signals[version] is DecisionSignal.BUY
        for version in (VERSION_V2, VERSION_V3, VERSION_V4, VERSION_V5)
    )


def test_the_panel_costs_the_history_of_its_most_expensive_version() -> None:
    """Fetching for the cheapest member would record four warm-up HOLDs."""
    built = panel()

    assert built.required_base_bars == max(engine.required_base_bars for engine in built.engines)
    assert built.required_base_bars > EmaCrossV1Engine().required_base_bars


def test_evaluating_five_versions_does_not_require_five_frames() -> None:
    """One fetch, five decisions. The extra versions cost arithmetic, not data."""
    bars = v1_buys_bars()
    before = bars.copy(deep=True)

    panel().evaluate(bars)

    pd.testing.assert_frame_equal(bars, before)


# ==========================================================================
# CRITICAL. Only the configured version can execute
# ==========================================================================


@pytest.mark.parametrize("execution_version", PANEL_VERSIONS)
def test_at_most_one_observation_is_ever_executed(execution_version: str) -> None:
    """CRITICAL. Whichever version is configured, one is the ceiling."""
    evaluation = panel(execution_version).evaluate(v1_buys_bars())
    executed = [observation for observation in evaluation.observations if observation.executed]

    assert len(executed) <= 1


def test_only_the_configured_version_executes_when_it_is_the_one_buying() -> None:
    """CRITICAL. V1 buys, V1 is configured, and only V1 is designated executed."""
    evaluation = panel(VERSION_V1).evaluate(v1_buys_bars())

    candidate = evaluation.candidate
    assert candidate is not None
    assert candidate.version == VERSION_V1
    executed = [
        observation.version for observation in evaluation.observations if observation.executed
    ]
    assert executed == [VERSION_V1]


@pytest.mark.parametrize("execution_version", [VERSION_V2, VERSION_V3, VERSION_V4, VERSION_V5])
def test_an_observational_version_cannot_execute_the_bar_it_wanted_to(
    execution_version: str,
) -> None:
    """CRITICAL. V1 is asking to buy and is not configured, so nothing executes.

    The case a convention-based implementation gets wrong. Something on this bar
    genuinely wants to trade; the configured version does not, and wanting is
    not a route to executing.
    """
    evaluation = panel(execution_version).evaluate(v1_buys_bars())

    assert evaluation.observation_for(VERSION_V1).result.signal is DecisionSignal.BUY
    assert evaluation.candidate is None
    assert all(not observation.executed for observation in evaluation.observations)


def test_four_versions_asking_to_buy_cannot_execute_through_a_holding_one() -> None:
    """CRITICAL. The mirror: V2 to V5 buy, V1 is configured and holds, nothing goes."""
    evaluation = panel(VERSION_V1).evaluate(others_buy_bars())

    assert evaluation.candidate is None
    assert [
        observation.version
        for observation in evaluation.observations
        if observation.result.is_actionable
    ] == [VERSION_V2, VERSION_V3, VERSION_V4, VERSION_V5]


def test_a_candidate_cannot_be_built_for_an_observational_version() -> None:
    """CRITICAL. By construction, not by convention: the value cannot exist."""
    result = build_result(version=VERSION_V2)

    with pytest.raises(ShadowConfigError, match="cannot produce an execution candidate"):
        ExecutionCandidate(result=result, execution_version=VERSION_V5)


def test_a_hold_cannot_be_built_into_a_candidate() -> None:
    """A HOLD is the absence of a direction, not a zero-sized one."""
    result = build_result(version=VERSION_V2, signal=DecisionSignal.HOLD, score=0.0)

    with pytest.raises(ShadowConfigError, match="names no direction"):
        ExecutionCandidate(result=result, execution_version=VERSION_V2)


def test_the_configured_version_holding_releases_nothing() -> None:
    """A HOLD from the executing version is not an execution."""
    observation = ShadowObservation(
        result=build_result(signal=DecisionSignal.HOLD, score=0.0, reasons=("SCORE_IN_HOLD_BAND",)),
        execution_version=VERSION_V2,
    )

    assert observation.is_execution_version
    assert not observation.executed


class ForcedExecution(ShadowObservation):
    """An observation that claims to have executed regardless of its version.

    Unreachable through the ordinary constructor - `executed` is derived from the
    version comparison, so two of them cannot both be true - which is exactly why
    it has to be simulated here. This subclass is what a future edit that
    redefined `executed` would look like, and the test below is the guard that
    edit would run into.
    """

    @property
    def executed(self) -> bool:
        return True


def test_an_evaluation_refuses_to_hold_two_executed_observations() -> None:
    """CRITICAL. The invariant is asserted rather than left to derivation."""
    observations = (
        ForcedExecution(result=build_result(version=VERSION_V2), execution_version=VERSION_V2),
        ForcedExecution(result=build_result(version=VERSION_V3), execution_version=VERSION_V2),
    )

    with pytest.raises(ShadowConfigError, match="are marked executed"):
        PanelEvaluation(
            symbol=BTC,
            timestamp=pd.Timestamp(FIRST_BAR),
            execution_version=VERSION_V2,
            observations=observations,
        )


def test_two_observations_of_one_version_are_refused() -> None:
    """One version decides one bar once."""
    observation = ShadowObservation(
        result=build_result(version=VERSION_V2), execution_version=VERSION_V2
    )

    with pytest.raises(ShadowConfigError, match="share an engine version"):
        PanelEvaluation(
            symbol=BTC,
            timestamp=pd.Timestamp(FIRST_BAR),
            execution_version=VERSION_V2,
            observations=(observation, dataclasses.replace(observation)),
        )


def test_a_panel_refuses_an_execution_version_it_does_not_hold() -> None:
    """A panel that cannot run what it is meant to execute trades on nothing."""
    with pytest.raises(ShadowConfigError, match="is not in this panel"):
        EnginePanel([EmaCrossV1Engine()], execution_version=VERSION_V4)


def test_a_panel_refuses_two_engines_of_one_version() -> None:
    with pytest.raises(ShadowConfigError, match="two engines of the same version"):
        EnginePanel(
            [MultiFactorV2Engine.for_symbol(BTC), MultiFactorV2Engine.for_symbol(BTC)],
            execution_version=VERSION_V2,
        )


def test_an_execution_candidate_names_no_quantity_and_no_price() -> None:
    """CRITICAL. A candidate is a direction to be sized, never an instruction."""
    fields = {field.name for field in dataclasses.fields(ExecutionCandidate)}

    assert fields == {"result", "execution_version"}
    exposed = {name for name in dir(ExecutionCandidate) if not name.startswith("_")}
    for forbidden in ("quantity", "price", "account", "order", "submit", "execute", "size"):
        assert not any(forbidden in name for name in exposed), forbidden


# ==========================================================================
# Failure isolation: an observational engine cannot break the trading path
# ==========================================================================


class RefusingEngine:
    """An engine that will not decide, for the isolation tests."""

    def __init__(self, version: str) -> None:
        self._version = version

    @property
    def version(self) -> str:
        return self._version

    @property
    def required_base_bars(self) -> int:
        return 1

    def describe(self) -> dict[str, object]:
        return {"engine_version": self._version}

    def decide(self, bars: pd.DataFrame) -> DecisionResult:
        raise DecisionInputError(f"{self._version} refuses these bars.")


def test_an_observational_version_failing_does_not_stop_the_executing_one() -> None:
    """CRITICAL. A shadow engine that could abort the cycle is not observational."""
    built = EnginePanel(
        [EmaCrossV1Engine(), RefusingEngine(VERSION_V4)], execution_version=VERSION_V1
    )

    evaluation = built.evaluate(v1_buys_bars())

    assert evaluation.candidate is not None
    assert evaluation.candidate.version == VERSION_V1
    assert [failure.version for failure in evaluation.failures] == [VERSION_V4]


def test_the_executing_version_failing_releases_nothing() -> None:
    """No candidate rather than a candidate from a decision never made."""
    built = EnginePanel(
        [EmaCrossV1Engine(), RefusingEngine(VERSION_V4)], execution_version=VERSION_V4
    )

    evaluation = built.evaluate(v1_buys_bars())

    assert evaluation.candidate is None
    assert evaluation.versions == (VERSION_V1,)


def test_a_panel_that_cannot_decide_at_all_refuses_rather_than_returns_nothing() -> None:
    built = EnginePanel([RefusingEngine(VERSION_V1)], execution_version=VERSION_V1)

    with pytest.raises(ShadowEvaluationError, match="No version in this panel could decide"):
        built.evaluate(v1_buys_bars())


# ==========================================================================
# Provenance: what a stored decision says about how it was made
# ==========================================================================


def test_every_version_records_the_feature_contract_it_actually_reads() -> None:
    evaluation = panel().evaluate(v1_buys_bars())
    versions = {
        observation.version: observation.feature_version for observation in evaluation.observations
    }

    assert versions[VERSION_V1] is None, "the crossover has no feature schema to version"
    for version in (VERSION_V2, VERSION_V3, VERSION_V4, VERSION_V5):
        assert versions[version] == FEATURE_SCHEMA_VERSION


def test_only_the_versions_that_carry_a_model_record_one() -> None:
    """V5 is an ensemble; recording it as modelless would be the one lie."""
    evaluation = panel().evaluate(v1_buys_bars())
    models = {
        observation.version: observation.model_version for observation in evaluation.observations
    }

    assert models[VERSION_V1] is None
    assert models[VERSION_V2] is None
    assert models[VERSION_V3] is None
    assert models[VERSION_V4] == "shadow-test-1"
    assert models[VERSION_V5] == "shadow-test-1"


def test_provenance_is_read_from_the_decision_rather_than_from_the_engine() -> None:
    """A stored decision answers for itself, with no engine object alive."""
    result = build_result(policy={"feature_schema_version": "9", "model_version": "m-1"})

    assert feature_version_of(result) == "9"
    assert model_version_of(result) == "m-1"


def test_a_missing_provenance_key_is_none_rather_than_invented() -> None:
    result = build_result(policy={})

    assert feature_version_of(result) is None
    assert model_version_of(result) is None


# ==========================================================================
# The record
# ==========================================================================


def test_recording_a_bar_writes_one_row_per_version(
    connection: sqlite3.Connection, run_id: int
) -> None:
    """CRITICAL. Five versions decided; five decisions are on disk."""
    bars = v1_buys_bars()
    evaluation = panel(VERSION_V1).evaluate(bars)

    ShadowRecorder(connection, strategy_run_id=run_id).record(evaluation)

    rows = list_shadow_decisions(connection)
    assert [row.engine_version for row in rows] == list(PANEL_VERSIONS)
    assert {row.symbol for row in rows} == {BTC}
    assert {row.bar_timestamp for row in rows} == {last_bar_of(bars)}
    assert {row.strategy_run_id for row in rows} == {run_id}


def test_exactly_one_stored_row_is_designated_executed(
    connection: sqlite3.Connection, run_id: int
) -> None:
    """CRITICAL. The designation is explicit, and it is singular."""
    ShadowRecorder(connection, strategy_run_id=run_id).record(
        panel(VERSION_V1).evaluate(v1_buys_bars())
    )

    rows = list_shadow_decisions(connection)
    executed = [row for row in rows if row.executed]
    observational = [row for row in rows if not row.executed]
    assert [row.engine_version for row in executed] == [VERSION_V1]
    assert all(row.designation == SHADOW_DESIGNATION_NOT_EXECUTED for row in observational)
    assert {row.execution_version for row in rows} == {VERSION_V1}


def test_a_stored_decision_carries_everything_needed_to_compare_it(
    connection: sqlite3.Connection, run_id: int
) -> None:
    """The persistence contract, field by field, against the live decision."""
    evaluation = panel(VERSION_V1).evaluate(v1_buys_bars())
    ShadowRecorder(connection, strategy_run_id=run_id).record(evaluation)

    observation = evaluation.observation_for(VERSION_V5)
    stored = get_shadow_decision(
        connection,
        symbol=BTC,
        bar_timestamp=last_bar_of(v1_buys_bars()),
        engine_version=VERSION_V5,
    )
    assert stored is not None
    assert stored.signal == observation.result.signal.value
    assert stored.score == pytest.approx(observation.result.score)
    assert stored.confidence == pytest.approx(observation.result.confidence)
    assert stored.regime == observation.result.regime.value
    assert stored.reasons == observation.result.reasons
    assert stored.feature_version == FEATURE_SCHEMA_VERSION
    assert stored.model_version == "shadow-test-1"
    assert stored.designation == SHADOW_DESIGNATION_NOT_EXECUTED
    assert stored.client_order_id is None


def test_a_recorded_bar_can_be_located_for_later_outcome_scoring(
    connection: sqlite3.Connection, run_id: int
) -> None:
    """The linkage every row has: which symbol, and which bar.

    That pair is what a later evaluation joins on to score a decision against the
    price action that followed it, and it is the only outcome an observational
    decision will ever have.
    """
    bars = v1_buys_bars()
    ShadowRecorder(connection, strategy_run_id=run_id).record(panel(VERSION_V1).evaluate(bars))

    for version in PANEL_VERSIONS:
        stored = get_shadow_decision(
            connection, symbol=BTC, bar_timestamp=last_bar_of(bars), engine_version=version
        )
        assert stored is not None
        assert stored.bar_timestamp == last_bar_of(bars)


def test_the_executed_decision_can_be_anchored_to_the_order_it_produced(
    connection: sqlite3.Connection, run_id: int
) -> None:
    """The second linkage, which only the executed decision ever gets."""
    bars = v1_buys_bars()
    recorder = ShadowRecorder(connection, strategy_run_id=run_id)
    evaluation = panel(VERSION_V1).evaluate(bars)
    recorder.record(evaluation)

    candidate = evaluation.candidate
    assert candidate is not None
    recorder.link_execution(candidate, client_order_id="autotrader-linked-1")

    stored = get_shadow_decision(
        connection, symbol=BTC, bar_timestamp=last_bar_of(bars), engine_version=VERSION_V1
    )
    assert stored is not None
    assert stored.client_order_id == "autotrader-linked-1"


def test_an_observational_decision_cannot_be_anchored_to_an_order(
    connection: sqlite3.Connection, run_id: int
) -> None:
    """CRITICAL. It produced no order, so there is nothing to name."""
    bars = v1_buys_bars()
    ShadowRecorder(connection, strategy_run_id=run_id).record(panel(VERSION_V1).evaluate(bars))

    with pytest.raises(StateInputError, match="observational"):
        state.link_shadow_decision_order(
            connection,
            symbol=BTC,
            bar_timestamp=last_bar_of(bars),
            engine_version=VERSION_V4,
            client_order_id="autotrader-should-not-exist",
        )


def test_relinking_an_executed_decision_to_a_different_order_is_refused(
    connection: sqlite3.Connection, run_id: int
) -> None:
    bars = v1_buys_bars()
    recorder = ShadowRecorder(connection, strategy_run_id=run_id)
    evaluation = panel(VERSION_V1).evaluate(bars)
    recorder.record(evaluation)
    candidate = evaluation.candidate
    assert candidate is not None
    recorder.link_execution(candidate, client_order_id="autotrader-first")

    recorder.link_execution(candidate, client_order_id="autotrader-first")
    with pytest.raises(StateInputError, match="already linked"):
        recorder.link_execution(candidate, client_order_id="autotrader-second")


def test_linking_a_decision_that_was_never_recorded_is_refused(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(UnknownShadowDecisionError):
        state.link_shadow_decision_order(
            connection,
            symbol=BTC,
            bar_timestamp=FIRST_BAR,
            engine_version=VERSION_V1,
            client_order_id="autotrader-nothing",
        )


def test_a_failed_record_leaves_no_partial_evaluation(
    connection: sqlite3.Connection, run_id: int
) -> None:
    """Five rows or none. A half-written bar is a silently biased comparison."""
    bars = v1_buys_bars()
    recorder = ShadowRecorder(connection, strategy_run_id=run_id)
    evaluation = panel(VERSION_V1).evaluate(bars)
    # Pre-place the row the last version would write, so its insert collides.
    record_shadow_decision(
        connection,
        bar_timestamp=last_bar_of(bars),
        symbol=BTC,
        engine_version=VERSION_V5,
        signal="HOLD",
        score=0.0,
        confidence=0.0,
        regime="RANGE",
        reasons=("SCORE_IN_HOLD_BAND",),
        execution_version=VERSION_V1,
        designation=SHADOW_DESIGNATION_NOT_EXECUTED,
    )

    with pytest.raises(DuplicateShadowDecisionError):
        recorder.record(evaluation)

    assert [row.engine_version for row in list_shadow_decisions(connection)] == [VERSION_V5]


# ==========================================================================
# CRITICAL. The schema enforces what the panel enforces
# ==========================================================================


def raw_insert(connection: sqlite3.Connection, **columns: object) -> None:
    """Write a shadow row through SQL, bypassing every line of this feature."""
    defaults: dict[str, object] = {
        "bar_timestamp": "2025-01-01T00:00:00.000000+00:00",
        "symbol": BTC,
        "engine_version": VERSION_V1,
        "signal": "BUY",
        "score": 0.5,
        "confidence": 0.5,
        "regime": "TREND_UP",
        "reasons": "SCORE_ABOVE_BUY_THRESHOLD",
        "execution_version": VERSION_V1,
        "designation": SHADOW_DESIGNATION_EXECUTED,
        "client_order_id": None,
        "created_at": "2025-01-01T00:00:00.000000+00:00",
    }
    defaults.update(columns)
    connection.execute(
        "INSERT INTO shadow_decisions "
        "(bar_timestamp, symbol, engine_version, signal, score, confidence, regime, reasons, "
        " execution_version, designation, client_order_id, created_at) "
        "VALUES (:bar_timestamp, :symbol, :engine_version, :signal, :score, :confidence, "
        " :regime, :reasons, :execution_version, :designation, :client_order_id, :created_at)",
        defaults,
    )


def test_the_database_refuses_an_observational_version_marked_executed(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. Enforced in SQL, so it holds against a writer that never called us."""
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        raw_insert(connection, engine_version=VERSION_V5, execution_version=VERSION_V1)


def test_the_database_refuses_a_second_execution_for_one_bar(
    connection: sqlite3.Connection,
) -> None:
    """CRITICAL. Five versions on one bar cannot become two executions."""
    raw_insert(connection, engine_version=VERSION_V1, execution_version=VERSION_V1)

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        raw_insert(connection, engine_version=VERSION_V4, execution_version=VERSION_V4)


def test_a_second_execution_through_the_api_is_named_for_what_it_is(
    connection: sqlite3.Connection,
) -> None:
    """The refusal an operator reads must say "two executions", not "duplicate"."""
    record_shadow_decision(
        connection,
        bar_timestamp=FIRST_BAR,
        symbol=BTC,
        engine_version=VERSION_V1,
        signal="BUY",
        score=1.0,
        confidence=1.0,
        regime="TREND_UP",
        reasons=("EMA20_CROSSED_ABOVE_EMA50",),
        execution_version=VERSION_V1,
        designation=SHADOW_DESIGNATION_EXECUTED,
    )

    with pytest.raises(ConflictingExecutedDecisionError, match="at most one execution"):
        record_shadow_decision(
            connection,
            bar_timestamp=FIRST_BAR,
            symbol=BTC,
            engine_version=VERSION_V5,
            signal="BUY",
            score=0.8,
            confidence=0.8,
            regime="TREND_UP",
            reasons=("ENSEMBLE_SCORE_ABOVE_BUY_BAND",),
            execution_version=VERSION_V5,
            designation=SHADOW_DESIGNATION_EXECUTED,
        )


def test_two_symbols_may_each_execute_on_the_same_bar(connection: sqlite3.Connection) -> None:
    """The rule is one execution per *bar*, not one per instant across the book."""
    for symbol in (BTC, "ETH/USD"):
        record_shadow_decision(
            connection,
            bar_timestamp=FIRST_BAR,
            symbol=symbol,
            engine_version=VERSION_V1,
            signal="BUY",
            score=1.0,
            confidence=1.0,
            regime="TREND_UP",
            reasons=("EMA20_CROSSED_ABOVE_EMA50",),
            execution_version=VERSION_V1,
            designation=SHADOW_DESIGNATION_EXECUTED,
        )

    assert len([row for row in list_shadow_decisions(connection) if row.executed]) == 2


def test_the_api_refuses_to_designate_a_version_that_was_not_configured(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(StateInputError, match="cannot be recorded as executed"):
        record_shadow_decision(
            connection,
            bar_timestamp=FIRST_BAR,
            symbol=BTC,
            engine_version=VERSION_V3,
            signal="BUY",
            score=0.5,
            confidence=0.5,
            regime="TREND_UP",
            reasons=("SCORE_ABOVE_BUY_THRESHOLD",),
            execution_version=VERSION_V1,
            designation=SHADOW_DESIGNATION_EXECUTED,
        )


def test_the_database_refuses_an_order_on_an_observational_row(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        raw_insert(
            connection,
            engine_version=VERSION_V4,
            execution_version=VERSION_V1,
            designation=SHADOW_DESIGNATION_NOT_EXECUTED,
            client_order_id="autotrader-impossible",
        )


@pytest.mark.parametrize("score", [-1.5, 1.5])
def test_a_score_outside_the_decision_contract_is_refused(
    connection: sqlite3.Connection, score: float
) -> None:
    with pytest.raises(StateInputError, match="score must be within"):
        record_shadow_decision(
            connection,
            bar_timestamp=FIRST_BAR,
            symbol=BTC,
            engine_version=VERSION_V1,
            signal="BUY",
            score=score,
            confidence=0.5,
            regime="TREND_UP",
            reasons=("X",),
            execution_version=VERSION_V1,
            designation=SHADOW_DESIGNATION_NOT_EXECUTED,
        )


def test_a_decision_with_no_reason_is_refused(connection: sqlite3.Connection) -> None:
    """A decision that cannot say why it was reached is not auditable."""
    with pytest.raises(StateInputError, match="must not be empty"):
        record_shadow_decision(
            connection,
            bar_timestamp=FIRST_BAR,
            symbol=BTC,
            engine_version=VERSION_V1,
            signal="HOLD",
            score=0.0,
            confidence=0.0,
            regime="RANGE",
            reasons=(),
            execution_version=VERSION_V1,
            designation=SHADOW_DESIGNATION_NOT_EXECUTED,
        )


def test_a_reason_token_with_whitespace_is_refused_rather_than_split(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(StateInputError, match="contains whitespace"):
        record_shadow_decision(
            connection,
            bar_timestamp=FIRST_BAR,
            symbol=BTC,
            engine_version=VERSION_V1,
            signal="HOLD",
            score=0.0,
            confidence=0.0,
            regime="RANGE",
            reasons=("a reason with spaces",),
            execution_version=VERSION_V1,
            designation=SHADOW_DESIGNATION_NOT_EXECUTED,
        )


def test_every_reason_the_shipped_versions_produce_round_trips(
    connection: sqlite3.Connection, run_id: int
) -> None:
    """The storage form is only safe if it is safe for real reason tokens."""
    evaluation = panel(VERSION_V1).evaluate(v1_buys_bars())
    ShadowRecorder(connection, strategy_run_id=run_id).record(evaluation)

    stored = {row.engine_version: row.reasons for row in list_shadow_decisions(connection)}
    for observation in evaluation.observations:
        assert stored[observation.version] == observation.result.reasons


# ==========================================================================
# CRITICAL. One bar, one claim, one intent, at most one order
# ==========================================================================


def test_a_bar_is_claimed_before_any_version_decides(connection: sqlite3.Connection) -> None:
    """The claim commits first, exactly as C9 requires. Miss a trade, never duplicate one."""
    checkpoint = InMemoryCheckpoint()
    bars = v1_buys_bars()

    make_cycle(connection, checkpoint=checkpoint).evaluate_bar(
        BTC, bars, bar_timestamp=last_bar_of(bars)
    )

    assert checkpoint.last_processed(BTC) == last_bar_of(bars)


def test_a_replayed_bar_evaluates_nothing_and_releases_nothing(
    connection: sqlite3.Connection, run_id: int
) -> None:
    """CRITICAL. A duplicate bar cannot become a second candidate."""
    bars = v1_buys_bars()
    cycle = make_cycle(connection, strategy_run_id=run_id)

    first = cycle.evaluate_bar(BTC, bars, bar_timestamp=last_bar_of(bars))
    second = cycle.evaluate_bar(BTC, bars, bar_timestamp=last_bar_of(bars))

    assert first.candidate is not None
    assert second.candidate is None
    assert second.skipped_reason == SKIPPED_ALREADY_PROCESSED
    assert not second.claimed
    assert len(list_shadow_decisions(connection)) == 5


def test_one_bar_one_claim_one_intent_at_most_one_order(
    connection: sqlite3.Connection, run_id: int
) -> None:
    """CRITICAL. Five versions decide; exactly one intent exists afterwards."""
    bars = v1_buys_bars()
    execution = RecordingExecution(connection, run_id)
    cycle = make_cycle(connection, strategy_run_id=run_id)

    outcome = drive(cycle, execution, bars)

    assert len(outcome.recorded_versions) == 5
    assert len(execution.candidates) == 1
    assert execution.candidates[0].version == VERSION_V1
    intents = list_order_intents(connection)
    assert len(intents) == 1
    assert intents[0].symbol == BTC
    executed = [row for row in list_shadow_decisions(connection) if row.executed]
    assert len(executed) == 1
    assert executed[0].client_order_id == intents[0].client_order_id


def test_repeating_the_cycle_on_one_bar_never_produces_a_second_intent(
    connection: sqlite3.Connection, run_id: int
) -> None:
    """CRITICAL. Three turns on one bar, one order."""
    bars = v1_buys_bars()
    execution = RecordingExecution(connection, run_id)
    cycle = make_cycle(connection, strategy_run_id=run_id)

    for _ in range(3):
        drive(cycle, execution, bars)

    assert len(execution.candidates) == 1
    assert len(list_order_intents(connection)) == 1
    assert len(list_shadow_decisions(connection)) == 5


def test_a_forgotten_claim_still_cannot_multiply_orders(
    connection: sqlite3.Connection, run_id: int
) -> None:
    """CRITICAL. The second guard, tested with the first one removed.

    A fresh checkpoint is exactly what a restart with a lost claim would look
    like. The bar is re-claimed and re-evaluated, and the record refuses it -
    which happens *before* the candidate is released, so the caller never
    receives a second one to act on.
    """
    bars = v1_buys_bars()
    execution = RecordingExecution(connection, run_id)
    drive(make_cycle(connection, strategy_run_id=run_id), execution, bars)

    amnesiac = make_cycle(connection, strategy_run_id=run_id, checkpoint=InMemoryCheckpoint())
    with pytest.raises(DuplicateShadowDecisionError):
        drive(amnesiac, execution, bars)

    assert len(execution.candidates) == 1
    assert len(list_order_intents(connection)) == 1
    assert len(list_shadow_decisions(connection)) == 5


def test_the_durable_claim_survives_a_new_cycle_on_the_same_database(
    tmp_path: Path, run_id: int, connection: sqlite3.Connection
) -> None:
    """A restart inherits the claim, so the replayed bar is skipped outright."""
    bars = v1_buys_bars()
    first = ShadowCycle(
        panel=panel(VERSION_V1),
        recorder=ShadowRecorder(connection, strategy_run_id=run_id),
        checkpoint=SqliteCheckpoint(connection),
    )
    first.evaluate_bar(BTC, bars, bar_timestamp=last_bar_of(bars))

    restarted = ShadowCycle(
        panel=panel(VERSION_V1),
        recorder=ShadowRecorder(connection, strategy_run_id=run_id),
        checkpoint=SqliteCheckpoint(connection),
    )
    outcome = restarted.evaluate_bar(BTC, bars, bar_timestamp=last_bar_of(bars))

    assert outcome.skipped_reason == SKIPPED_ALREADY_PROCESSED
    assert outcome.candidate is None
    assert len(list_shadow_decisions(connection)) == 5


def test_an_older_bar_cannot_reopen_a_claimed_one(
    connection: sqlite3.Connection, run_id: int
) -> None:
    """An out-of-order provider response is not a new bar."""
    bars = v1_buys_bars()
    cycle = make_cycle(connection, strategy_run_id=run_id)
    cycle.evaluate_bar(BTC, bars, bar_timestamp=last_bar_of(bars))

    outcome = cycle.evaluate_bar(BTC, bars, bar_timestamp=last_bar_of(bars) - STEP)

    assert outcome.skipped_reason == SKIPPED_ALREADY_PROCESSED
    assert len(list_shadow_decisions(connection)) == 5


def test_the_cycle_refuses_bars_that_are_not_the_claimed_bar(
    connection: sqlite3.Connection, run_id: int
) -> None:
    """Recording five decisions under a bar label none of them used corrupts the join."""
    bars = v1_buys_bars()
    cycle = make_cycle(connection, strategy_run_id=run_id)

    with pytest.raises(ShadowConfigError, match="was claimed"):
        cycle.evaluate_bar(BTC, bars, bar_timestamp=last_bar_of(bars) + STEP)

    assert list_shadow_decisions(connection) == []


def test_a_naive_bar_timestamp_is_refused(connection: sqlite3.Connection) -> None:
    bars = v1_buys_bars()

    with pytest.raises(ShadowClaimError, match="timezone-aware"):
        make_cycle(connection).evaluate_bar(
            BTC, bars, bar_timestamp=last_bar_of(bars).replace(tzinfo=None)
        )


def test_the_local_claim_protocol_matches_the_runtime_checkpoint() -> None:
    """The declare-and-pin arrangement, pinned.

    `cycle.BarClaim` is declared locally so this package imports nothing that
    holds an execution gateway. That is only safe while the two agree, so the
    real checkpoints are asserted to satisfy the local protocol and the method
    signatures are compared.
    """
    assert isinstance(InMemoryCheckpoint(), BarClaim)
    for method in ("last_processed", "mark_processed"):
        assert inspect.signature(getattr(BarClaim, method)) == inspect.signature(
            getattr(ProcessedBarCheckpoint, method)
        )


# ==========================================================================
# CRITICAL. The shadow layer has no path to a broker
# ==========================================================================

SHADOW_MODULES = (shadow_panel, shadow_recorder, shadow_cycle, shadow_versions)

#: The only imports the shadow package's modules may make. Anything outside this
#: list either reaches a network, holds broker state, or drags a provider SDK
#: into a package whose entire purpose is to observe without acting.
ALLOWED_IMPORT_ROOTS = frozenset(
    {
        "__future__",
        "autotrader.decision",
        "autotrader.shadow",
        "autotrader.state",
        "collections",
        "dataclasses",
        "datetime",
        "pandas",
        "sqlite3",
        "typing",
    }
)


def shadow_module_paths() -> list[Path]:
    """Every source file in the shadow package."""
    root = Path(shadow_panel.__file__).parent
    return sorted(root.rglob("*.py"))


def test_the_shadow_package_imports_nothing_that_can_reach_a_broker() -> None:
    """CRITICAL. Asserted against the import graph, not against intentions."""
    for path in shadow_module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for name in imported:
            allowed = any(
                name == root or name.startswith(f"{root}.") for root in ALLOWED_IMPORT_ROOTS
            )
            assert allowed, f"{path.name} imports {name}, which is outside the boundary"


def test_the_shadow_package_never_names_an_execution_or_broker_module() -> None:
    """CRITICAL. Not even indirectly, through a runtime that holds a gateway."""
    forbidden = (
        "autotrader.execution",
        "autotrader.risk",
        "autotrader.account",
        "autotrader.reconciliation",
        "autotrader.runtime",
        "autotrader.dashboard",
        "autotrader.smoke",
        "alpaca",
        "TradingClient",
        "submit_order",
    )
    for path in shadow_module_paths():
        code = code_without_prose(path.read_text(encoding="utf-8"))
        for token in forbidden:
            assert token not in code, f"{path.name} names {token}"


def test_no_shadow_module_opens_a_socket_or_reads_a_credential() -> None:
    forbidden = ("socket", "requests", "urllib", "http", "os.environ", "getenv", "api_key")
    for path in shadow_module_paths():
        code = code_without_prose(path.read_text(encoding="utf-8"))
        for token in forbidden:
            assert token not in code, f"{path.name} names {token}"


def test_the_recorder_holds_a_connection_and_nothing_else() -> None:
    """CRITICAL. There is no client on it to call, because there is no field for one."""
    recorder = ShadowRecorder(None)  # type: ignore[arg-type]

    assert set(recorder.__dict__) == {"_connection", "_strategy_run_id"}


def test_no_shadow_module_binds_a_name_from_the_execution_side() -> None:
    """A module-level import of a broker type would show up here even if unused."""
    for module in SHADOW_MODULES:
        for name, value in vars(module).items():
            origin = getattr(value, "__module__", "")
            if not isinstance(origin, str):
                continue
            for forbidden in ("alpaca", "autotrader.execution", "autotrader.risk"):
                assert not origin.startswith(forbidden), f"{module.__name__}.{name} is {origin}"


def test_the_whole_cycle_runs_with_every_socket_blocked(
    monkeypatch: pytest.MonkeyPatch, connection: sqlite3.Connection, run_id: int
) -> None:
    """CRITICAL. Recording five decisions needs no network, and proves it."""

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("the shadow layer must not open a socket")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

    outcome = make_cycle(connection, strategy_run_id=run_id).evaluate_bar(
        BTC, v1_buys_bars(), bar_timestamp=last_bar_of(v1_buys_bars())
    )

    assert len(outcome.recorded_ids) == 5


def test_the_shadow_package_defines_nothing_that_submits() -> None:
    """No function here is named for acting, because none of them acts."""
    for module in SHADOW_MODULES:
        for name, value in vars(module).items():
            if name.startswith("_") or not callable(value):
                continue
            lowered = name.lower()
            for forbidden in ("submit", "place_order", "send_order", "trade"):
                assert forbidden not in lowered, f"{module.__name__}.{name}"


def test_the_shadow_package_writes_only_the_decision_table() -> None:
    """The one table it touches, asserted against the state calls it makes."""
    allowed = {
        "record_shadow_decision",
        "link_shadow_decision_order",
        "transaction",
        "SHADOW_DESIGNATION_EXECUTED",
        "SHADOW_DESIGNATION_NOT_EXECUTED",
    }
    tree = ast.parse(Path(shadow_recorder.__file__).read_text(encoding="utf-8"))
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "state"
    }

    assert used <= allowed, used - allowed


# ==========================================================================
# CRITICAL. Nothing is activated
# ==========================================================================


def source_modules() -> list[Path]:
    """Every source file in the application package."""
    root = Path(state.__file__).parent.parent
    return sorted(path for path in root.rglob("*.py") if "shadow" not in path.parts)


def test_nothing_outside_the_shadow_package_imports_it() -> None:
    """CRITICAL. No runtime constructs a panel; no gate reads a shadow decision."""
    for path in source_modules():
        code = code_without_prose(path.read_text(encoding="utf-8"))
        assert "autotrader.shadow" not in code, f"{path.name} imports the shadow package"


def test_the_crypto_runtime_still_evaluates_the_existing_strategy() -> None:
    """The production loop is untouched: same strategy, same risk engine."""
    from autotrader.runtime import runner

    source = inspect.getsource(runner)
    assert "generate_ema_cross_signals" in source
    assert "shadow" not in source.lower()


def test_there_is_no_default_execution_version_anywhere() -> None:
    """CRITICAL. Which engine trades is never answered by omission."""
    for callable_object in (EnginePanel.__init__, panel_for_symbol):
        parameter = inspect.signature(callable_object).parameters["execution_version"]
        assert parameter.default is inspect.Parameter.empty
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY

    from autotrader import shadow

    assert not hasattr(shadow, "DEFAULT_EXECUTION_VERSION")
    assert not hasattr(shadow, "EXECUTION_VERSION")


def test_the_panel_observes_every_shipped_version() -> None:
    """Five versions exist; a panel that quietly observed four would be a lie."""
    assert PANEL_VERSIONS == (VERSION_V1, VERSION_V2, VERSION_V3, VERSION_V4, VERSION_V5)
    assert panel().versions == PANEL_VERSIONS


def test_an_observational_version_is_named_as_such_by_the_panel() -> None:
    built = panel(VERSION_V1)

    assert built.execution_version == VERSION_V1
    assert built.observational_versions == (VERSION_V2, VERSION_V3, VERSION_V4, VERSION_V5)


def test_the_shadow_package_imports_without_the_state_module_being_writable(
    connection: sqlite3.Connection,
) -> None:
    """Constructing the whole feature writes nothing until a bar is evaluated."""
    make_cycle(connection)

    assert list_shadow_decisions(connection) == []


def test_the_shadow_module_names_survive_a_fresh_import() -> None:
    """A stale module object would make the boundary tests above vacuous."""
    names = [name for name in sys.modules if name.startswith("autotrader.shadow")]
    saved = {name: sys.modules[name] for name in names}
    try:
        for name in saved:
            del sys.modules[name]
        module = importlib.import_module("autotrader.shadow")
        assert module.PANEL_VERSIONS == PANEL_VERSIONS
    finally:
        sys.modules.update(saved)
