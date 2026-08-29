"""Decision Engine tests: the shared contract, and the boundaries of the package.

Every test here is offline, needs no credentials, and touches no database. The
package under test is pure computation over frames, and a test that needed any
of those things would be evidence that it had stopped being that.

The guards at the bottom are the load-bearing ones. They assert, against the
parse tree rather than against prose, that the decision package cannot reach a
broker, cannot reach the execution or risk or state layers, does not need a
provider SDK to import, and contains no construct capable of reading a bar that
has not happened yet.
"""

from __future__ import annotations

import ast
import importlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from autotrader.decision import contract as decision_contract
from autotrader.decision.contract import (
    CRYPTO_SYMBOLS,
    VERSION_V1,
    VERSION_V2,
    VERSION_V3,
    VERSION_V4,
    VERSION_V5,
    AssetClass,
    DecisionConfigError,
    DecisionEngine,
    DecisionInputError,
    DecisionResult,
    DecisionSignal,
    MarketRegime,
    resolve_asset_class,
)
from autotrader.decision.v1 import EmaCrossV1Engine
from autotrader.decision.v2 import MultiFactorV2Engine
from autotrader.decision.v3 import MultiTimeframeV3Engine
from autotrader.equity import EQUITY_SYMBOLS
from test_runtime import code_without_prose

T0 = pd.Timestamp(datetime(2025, 1, 2, 14, 30, tzinfo=UTC))

#: The package under guard, and the only imports its modules may make. Anything
#: outside this list either reaches a network, holds broker state, or drags a
#: provider SDK into a research process that has no use for one.
ALLOWED_IMPORT_ROOTS = frozenset(
    {
        "__future__",
        "autotrader.decision",
        "autotrader.equity",
        "autotrader.runtime.schedule",
        "autotrader.strategies.ema_cross",
        "collections",
        "dataclasses",
        "datetime",
        "enum",
        "math",
        "pandas",
        "types",
        "typing",
    }
)


def decision_modules() -> list[Path]:
    """Every source file in the decision package."""
    root = Path(decision_contract.__file__).parent
    return sorted(root.rglob("*.py"))


def build_result(**overrides: object) -> DecisionResult:
    """A valid `DecisionResult`, with fields replaced by keyword."""
    fields: dict[str, object] = {
        "version": VERSION_V2,
        "symbol": "BTC/USD",
        "timestamp": T0,
        "signal": DecisionSignal.HOLD,
        "score": 0.0,
        "confidence": 0.0,
        "reasons": ("SCORE_IN_HOLD_BAND",),
        "features": {"ema_fast": 1.0},
        "policy": {"policy_name": "test"},
    }
    fields.update(overrides)
    return DecisionResult(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The result shape
# --------------------------------------------------------------------------


def test_a_valid_result_normalizes_its_timestamp_to_utc() -> None:
    eastern = pd.Timestamp("2025-01-02 09:30", tz="America/New_York")
    result = build_result(timestamp=eastern)

    assert str(result.timestamp.tz) == "UTC"
    assert result.timestamp == eastern


def test_a_naive_timestamp_is_refused_rather_than_assumed_utc() -> None:
    with pytest.raises(DecisionConfigError, match="timezone-aware"):
        build_result(timestamp=pd.Timestamp("2025-01-02 14:30"))


@pytest.mark.parametrize("score", [-1.0, -0.5, 0.0, 0.5, 1.0])
def test_every_score_inside_the_bound_is_accepted(score: float) -> None:
    assert build_result(score=score).score == score


@pytest.mark.parametrize("score", [-1.0001, 1.0001, 2.0, -7.0])
def test_a_score_outside_the_bound_is_refused(score: float) -> None:
    with pytest.raises(DecisionConfigError, match=r"score must be within"):
        build_result(score=score)


@pytest.mark.parametrize("confidence", [-0.0001, 1.0001, -1.0])
def test_a_confidence_outside_the_unit_interval_is_refused(confidence: float) -> None:
    with pytest.raises(DecisionConfigError, match=r"confidence must be within"):
        build_result(confidence=confidence)


def test_a_nan_score_is_refused_rather_than_propagated() -> None:
    with pytest.raises(DecisionConfigError, match="must not be NaN"):
        build_result(score=float("nan"))


def test_an_empty_reason_list_is_refused() -> None:
    """A decision that cannot say why it was reached is not auditable."""
    with pytest.raises(DecisionConfigError, match="reasons must not be empty"):
        build_result(reasons=())


def test_features_and_policy_are_read_only_and_key_sorted() -> None:
    """A caller must not be able to edit the record of a decision already made."""
    result = build_result(features={"zeta": 1.0, "alpha": 2.0})

    assert list(result.features) == ["alpha", "zeta"]
    with pytest.raises(TypeError):
        result.features["alpha"] = 9.0  # type: ignore[index]


def test_mutating_the_source_mapping_cannot_change_a_built_result() -> None:
    features = {"ema_fast": 1.0}
    result = build_result(features=features)
    features["ema_fast"] = 99.0

    assert result.features["ema_fast"] == 1.0


def test_to_dict_is_json_serializable_and_deterministic() -> None:
    import json

    result = build_result(
        signal=DecisionSignal.BUY,
        score=0.5,
        confidence=0.75,
        regime=MarketRegime.TREND_UP,
        reasons=("SCORE_ABOVE_BUY_THRESHOLD",),
    )
    payload = result.to_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert payload["timestamp"] == "2025-01-02T14:30:00+00:00"
    assert payload["signal"] == "BUY"
    assert payload["regime"] == "TREND_UP"


def test_is_actionable_is_exactly_not_hold() -> None:
    assert not build_result(signal=DecisionSignal.HOLD).is_actionable
    assert build_result(signal=DecisionSignal.BUY, score=1.0, confidence=1.0).is_actionable
    assert build_result(signal=DecisionSignal.SELL, score=-1.0, confidence=1.0).is_actionable


def test_the_three_signals_are_the_whole_vocabulary() -> None:
    """A short is not a signal value; adding one is a documented scope change."""
    assert {signal.value for signal in DecisionSignal} == {"BUY", "HOLD", "SELL"}


# --------------------------------------------------------------------------
# Asset classes
# --------------------------------------------------------------------------


def test_the_declared_crypto_universe_matches_the_execution_boundary() -> None:
    """The decision package declares the pairs; the execution boundary owns them."""
    from autotrader.execution.models import SUPPORTED_SYMBOLS

    assert CRYPTO_SYMBOLS == SUPPORTED_SYMBOLS == ("BTC/USD", "ETH/USD")


@pytest.mark.parametrize("symbol", CRYPTO_SYMBOLS)
def test_crypto_pairs_resolve_to_crypto(symbol: str) -> None:
    assert resolve_asset_class(symbol) is AssetClass.CRYPTO


@pytest.mark.parametrize("symbol", EQUITY_SYMBOLS)
def test_equity_symbols_resolve_to_equity(symbol: str) -> None:
    assert resolve_asset_class(symbol) is AssetClass.EQUITY


@pytest.mark.parametrize("symbol", ["BTCUSD", "BTC-USD", "DOGE/USD", "", "TSLQ", "BTC / USD"])
def test_a_symbol_outside_both_universes_is_refused(symbol: str) -> None:
    """Including a slashless crypto spelling: membership decides, not punctuation."""
    with pytest.raises(DecisionInputError, match="Unsupported symbol"):
        resolve_asset_class(symbol)


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        (" spy ", AssetClass.EQUITY),
        ("aapl", AssetClass.EQUITY),
        ("btc/usd", AssetClass.CRYPTO),
        (" ETH/USD ", AssetClass.CRYPTO),
    ],
)
def test_symbols_are_stripped_and_uppercased_like_every_other_boundary(
    written: str, expected: AssetClass
) -> None:
    """The same `strip().upper()` normalization the two execution boundaries use."""
    assert resolve_asset_class(written) is expected


def test_asset_class_resolution_is_not_a_slash_heuristic() -> None:
    """A slash in an unknown symbol does not make it crypto."""
    with pytest.raises(DecisionInputError):
        resolve_asset_class("SPY/USD")


# --------------------------------------------------------------------------
# Protocol conformance
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("engine", "expected_version"),
    [
        (EmaCrossV1Engine(), VERSION_V1),
        (MultiFactorV2Engine.for_symbol("BTC/USD"), VERSION_V2),
        (MultiTimeframeV3Engine.for_symbol("BTC/USD"), VERSION_V3),
    ],
)
def test_every_engine_satisfies_the_shared_protocol(
    engine: DecisionEngine, expected_version: str
) -> None:
    assert isinstance(engine, DecisionEngine)
    assert engine.version == expected_version
    assert isinstance(engine.required_base_bars, int)
    assert engine.required_base_bars > 0
    assert engine.describe()["engine_version"] == expected_version


def test_the_five_versions_are_distinct_identifiers() -> None:
    assert len({VERSION_V1, VERSION_V2, VERSION_V3, VERSION_V4, VERSION_V5}) == 5


def test_v5_exists_and_is_not_activated_by_existing_here() -> None:
    """The successor to this file's V4-and-V5 scope marker, narrowed once again.

    The marker began as "V4 and V5 are unimplemented", was narrowed to V5 alone
    when the probability engine landed, and is narrowed again now that
    `decision.v5` is the ensemble. What was ever load-bearing about it is the
    half that survives: a decision engine existing in this package is not the
    same as a decision engine being *used*, and no version here has ever been
    wired into a runtime. The check that nothing outside the package has started
    preferring V5 lives with the rest of V5's boundary tests, in
    `test_decision_v5.py`.
    """
    from autotrader import decision

    assert decision.VERSION_V5 == "v5"
    assert issubclass(decision.EnsembleV5Engine, object)
    assert not hasattr(decision, "DEFAULT_ENGINE")


# --------------------------------------------------------------------------
# Package boundaries
# --------------------------------------------------------------------------


def _import_roots(tree: ast.AST) -> set[str]:
    """Every module root imported by `tree`, dotted for `autotrader` submodules."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def test_the_decision_package_imports_nothing_that_can_reach_a_broker() -> None:
    """CRITICAL. The leftmost box of the pipeline can reach none of the others."""
    for path in decision_modules():
        for imported in _import_roots(ast.parse(path.read_text(encoding="utf-8"))):
            allowed = any(
                imported == root or imported.startswith(f"{root}.") for root in ALLOWED_IMPORT_ROOTS
            )
            assert allowed, f"{path.name} imports {imported}, which is outside the boundary"


def test_the_decision_package_never_names_an_execution_or_state_module() -> None:
    forbidden = (
        "autotrader.execution",
        "autotrader.risk",
        "autotrader.state",
        "autotrader.account",
        "autotrader.reconciliation",
        "autotrader.dashboard",
        "autotrader.runtime.runner",
        "autotrader.equity.runtime",
        "alpaca",
    )
    for path in decision_modules():
        code = code_without_prose(path.read_text(encoding="utf-8"))
        for token in forbidden:
            assert token not in code, f"{path.name} names {token}"


def test_the_decision_package_imports_without_a_provider_sdk_installed() -> None:
    """A backtest or a training run must not need a broker client library.

    Simulated by hiding every `alpaca` module from the import system and
    re-importing the package from scratch. If any module in it reached a
    provider SDK - directly, or through a package `__init__` that does - this
    raises `ModuleNotFoundError` instead of passing.
    """
    hidden = {
        name: module for name, module in sys.modules.items() if name.split(".")[0] == "alpaca"
    }
    decision_names = [name for name in sys.modules if name.startswith("autotrader.decision")]
    saved_decision = {name: sys.modules[name] for name in decision_names}
    try:
        for name in hidden:
            sys.modules[name] = None  # type: ignore[assignment]
        for name in decision_names:
            del sys.modules[name]
        module = importlib.import_module("autotrader.decision")
        assert module.VERSION_V2 == "v2"
    finally:
        for name in decision_names:
            sys.modules.pop(name, None)
        sys.modules.update(hidden)
        sys.modules.update(saved_decision)


def test_no_decision_module_opens_a_socket_or_reads_the_clock() -> None:
    """Determinism is not compatible with either.

    A decision engine that read the wall clock would score the same bars
    differently on a replay, and one that opened a socket would not be a
    function of its inputs at all.
    """
    forbidden = (
        "socket",
        "requests",
        "urllib",
        "datetime.now",
        "datetime.utcnow",
        "time.time",
        "random",
    )
    for path in decision_modules():
        code = code_without_prose(path.read_text(encoding="utf-8"))
        for token in forbidden:
            assert token not in code, f"{path.name} names {token}"


def test_no_decision_module_contains_a_look_ahead_construct() -> None:
    """CRITICAL. docs/SPEC.md section 7F, asserted against the parse tree.

    A negative shift, a centred window, a backward fill, or a reversal are the
    four ways a pandas pipeline reads the future by accident. None of them
    appears anywhere in this package, and a property test elsewhere in this
    suite confirms the consequence: truncating the bars changes no earlier value.
    """
    for path in decision_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        code = code_without_prose(path.read_text(encoding="utf-8"))
        for token in ("bfill", "backfill", "ffill", "[::-1]", "ascending=False"):
            assert token not in code, f"{path.name} contains {token}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "shift":
                    for argument in node.args:
                        assert not (
                            isinstance(argument, ast.UnaryOp) and isinstance(argument.op, ast.USub)
                        ), f"{path.name} shifts a series backwards"
                for keyword in node.keywords:
                    assert keyword.arg != "center", f"{path.name} uses a centred window"


def test_the_decision_package_writes_nothing() -> None:
    """No file, no database, no state. It returns a value and nothing else."""
    forbidden = ("open(", "Path(", "to_parquet", "to_csv", "sqlite3", "connect(", "write_text")
    for path in decision_modules():
        code = code_without_prose(path.read_text(encoding="utf-8"))
        for token in forbidden:
            assert token not in code, f"{path.name} names {token}"


def test_bar_timestamps_are_treated_as_interval_starts_everywhere() -> None:
    """The convention the rest of the system uses, restated as an assertion."""
    from autotrader.decision.timeframes import BASE_TIMEFRAME
    from autotrader.runtime.schedule import BAR_INTERVAL

    assert BASE_TIMEFRAME.interval == BAR_INTERVAL == timedelta(minutes=15)
