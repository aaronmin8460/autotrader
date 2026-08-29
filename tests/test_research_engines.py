"""The Decision Engine contract, its adapters, and the research/production seam.

Two guarantees are pinned here. The **contract** guarantee: anything satisfying
`DecisionEngine` can be evaluated, and the infrastructure names no strategy.
The **fidelity** guarantee: the research-only parametric engine reduces exactly
to the production crossover at the production periods, so a sweep is exploring
the strategy that actually runs rather than a near-miss of it.

The isolation tests at the end are the ones that keep this package research
rather than trading: no broker vocabulary, no order path, no network, no state.
"""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from autotrader.research import engines as engines_module
from autotrader.research.costs import (
    COST_MODELS,
    CRYPTO_COST,
    EQUITY_COST,
    STRESS_COST,
    ZERO_COST,
    CostInputError,
    CostModel,
    Side,
    cost_model_for,
)
from autotrader.research.engines import (
    Action,
    BuyAndHoldEngine,
    DecisionEngine,
    EmaCrossEngine,
    EngineInputError,
    ParametricEmaCross,
    ResearchSignal,
    describe,
)
from autotrader.strategies.ema_cross import FAST_PERIOD, SLOW_PERIOD, generate_ema_cross_signals
from research_fixtures import flat, multi_cycle, rally_then_selloff, wave

BARS = wave(600)


# ==========================================================================
# The contract
# ==========================================================================


@pytest.mark.parametrize(
    "engine",
    [
        EmaCrossEngine(),
        ParametricEmaCross(fast_period=5, slow_period=20),
        BuyAndHoldEngine(),
    ],
)
def test_every_shipped_engine_satisfies_the_protocol(engine: object) -> None:
    assert isinstance(engine, DecisionEngine)
    assert engine.name and engine.version
    assert isinstance(engine.warmup_bars, int) and engine.warmup_bars >= 0
    assert isinstance(dict(engine.parameters), dict)


@pytest.mark.parametrize(
    "engine",
    [EmaCrossEngine(), ParametricEmaCross(fast_period=5, slow_period=20), BuyAndHoldEngine()],
)
def test_no_engine_modifies_the_frame_it_is_given(engine: object) -> None:
    before = BARS.copy(deep=True)
    engine.generate(BARS)
    pd.testing.assert_frame_equal(BARS, before)


@pytest.mark.parametrize(
    "engine",
    [EmaCrossEngine(), ParametricEmaCross(fast_period=5, slow_period=20)],
)
def test_generation_is_pure_with_respect_to_its_input(engine: object) -> None:
    first = list(engine.generate(BARS))
    second = list(engine.generate(BARS))
    assert [(s.timestamp, s.action, s.reason) for s in first] == [
        (s.timestamp, s.action, s.reason) for s in second
    ]


def test_describe_captures_everything_a_result_must_carry() -> None:
    identity = describe(ParametricEmaCross(fast_period=8, slow_period=34))
    assert identity["name"] == "parametric-ema-cross"
    assert identity["version"] == "v1"
    assert identity["parameters"] == {"fast_period": 8, "slow_period": 34}
    assert identity["warmup_bars"] == 34


def test_a_signal_carries_no_price() -> None:
    """What a proposal could have been filled at is the simulator's question,
    not the engine's."""
    fields = set(ResearchSignal.__dataclass_fields__)
    assert "price" not in fields
    assert fields == {"timestamp", "symbol", "action", "reason", "strength"}


def test_signal_strength_is_bounded() -> None:
    with pytest.raises(EngineInputError, match=r"within \[0, 1\]"):
        ResearchSignal(
            pd.Timestamp("2025-01-01", tz="UTC"),
            "BTC/USD",
            Action.ENTER_LONG,
            "R",
            strength=1.5,
        )


def test_the_action_vocabulary_is_long_only() -> None:
    assert {action.value for action in Action} == {"ENTER_LONG", "EXIT_LONG"}


# ==========================================================================
# Fidelity: the parametric engine reduces to the production strategy
# ==========================================================================


@pytest.mark.parametrize("fixture", [wave, rally_then_selloff, multi_cycle, flat])
def test_the_parametric_engine_matches_production_at_the_production_periods(
    fixture: object,
) -> None:
    """CRITICAL. If this ever stops holding, the sweep is exploring a different
    strategy than the one production runs - and it would do so quietly."""
    bars = fixture()
    adapter = [(str(s.timestamp), s.action.value) for s in EmaCrossEngine().generate(bars)]
    parametric = [
        (str(s.timestamp), s.action.value)
        for s in ParametricEmaCross(fast_period=FAST_PERIOD, slow_period=SLOW_PERIOD).generate(bars)
    ]
    assert adapter == parametric


def test_the_adapter_reuses_the_production_strategy_rather_than_copying_it() -> None:
    assert engines_module.generate_ema_cross_signals is generate_ema_cross_signals


def test_the_adapter_computes_no_indicator_of_its_own() -> None:
    """`EmaCrossEngine` must translate, not calculate. An adapter that
    recomputed the EMA could drift from production without failing."""
    source = inspect.getsource(EmaCrossEngine)
    assert "ewm" not in source
    assert "rolling" not in source


def test_the_adapter_translates_the_production_signal_vocabulary() -> None:
    signals = EmaCrossEngine().generate(rally_then_selloff())
    production = generate_ema_cross_signals(rally_then_selloff())
    assert len(signals) == len(production)
    for research, original in zip(signals, production, strict=True):
        assert research.timestamp == original.timestamp
        assert research.reason == original.reason
        assert research.action.value == (
            "ENTER_LONG" if original.type.value == "BUY" else "EXIT_LONG"
        )


def test_the_adapter_reports_the_production_warmup() -> None:
    assert EmaCrossEngine().warmup_bars == SLOW_PERIOD


def test_the_adapter_reports_the_production_periods_as_its_parameters() -> None:
    assert dict(EmaCrossEngine().parameters) == {
        "fast_period": FAST_PERIOD,
        "slow_period": SLOW_PERIOD,
    }


# ==========================================================================
# Parametric engine validation
# ==========================================================================


@pytest.mark.parametrize(("fast", "slow"), [(50, 50), (60, 50)])
def test_periods_that_cannot_cross_are_refused(fast: int, slow: int) -> None:
    with pytest.raises(EngineInputError, match="strictly shorter"):
        ParametricEmaCross(fast_period=fast, slow_period=slow)


@pytest.mark.parametrize("period", [0, -1])
def test_a_non_positive_period_is_refused(period: int) -> None:
    with pytest.raises(EngineInputError, match="at least 1"):
        ParametricEmaCross(fast_period=period, slow_period=50)


def test_a_non_integer_period_is_refused() -> None:
    with pytest.raises(EngineInputError, match="must be an int"):
        ParametricEmaCross(fast_period=5.5, slow_period=50)  # type: ignore[arg-type]


def test_a_multi_symbol_frame_is_refused() -> None:
    mixed = pd.concat([wave(100), wave(100, symbol="ETH/USD")], ignore_index=True)
    with pytest.raises(EngineInputError, match="exactly one symbol"):
        ParametricEmaCross().generate(mixed)


def test_unsorted_bars_are_refused_rather_than_sorted() -> None:
    scrambled = BARS.iloc[::-1].reset_index(drop=True)
    with pytest.raises(EngineInputError, match="ascending"):
        ParametricEmaCross().generate(scrambled)


def test_an_empty_frame_yields_no_signals() -> None:
    assert ParametricEmaCross().generate(BARS.iloc[:0]) == ()


def test_different_periods_produce_different_signals() -> None:
    """Otherwise a sweep over them would be measuring nothing."""
    fast = ParametricEmaCross(fast_period=3, slow_period=10).generate(BARS)
    slow = ParametricEmaCross(fast_period=30, slow_period=100).generate(BARS)
    assert len(fast) != len(slow)


# ==========================================================================
# The benchmark
# ==========================================================================


def test_the_benchmark_enters_once_and_never_exits() -> None:
    signals = BuyAndHoldEngine().generate(BARS)
    assert len(signals) == 1
    assert signals[0].action is Action.ENTER_LONG


def test_the_benchmark_can_be_given_the_same_head_start_as_the_strategy() -> None:
    """A benchmark that started fifty bars earlier is not a benchmark."""
    timestamps = list(BARS["timestamp"])
    assert BuyAndHoldEngine(warmup=50).generate(BARS)[0].timestamp == timestamps[50]
    assert BuyAndHoldEngine(warmup=0).generate(BARS)[0].timestamp == timestamps[0]


def test_a_benchmark_with_no_room_to_enter_emits_nothing() -> None:
    assert BuyAndHoldEngine(warmup=1000).generate(BARS) == ()


def test_a_negative_benchmark_warmup_is_refused() -> None:
    with pytest.raises(EngineInputError, match="non-negative"):
        BuyAndHoldEngine(warmup=-1)


# ==========================================================================
# Cost models
# ==========================================================================


def test_slippage_is_adverse_on_both_sides() -> None:
    """There is no configuration in which trading pays you."""
    model = CostModel(label="t", fee_rate=Decimal("0"), slippage_rate=Decimal("0.01"))
    reference = Decimal("100")
    assert model.fill_price(reference, Side.BUY) > reference
    assert model.fill_price(reference, Side.SELL) < reference


def test_slippage_cost_is_positive_on_both_sides() -> None:
    model = CostModel(label="t", fee_rate=Decimal("0"), slippage_rate=Decimal("0.01"))
    for side in (Side.BUY, Side.SELL):
        assert model.slippage_cost(Decimal("2"), Decimal("100"), side) > 0


def test_a_frictionless_model_changes_nothing() -> None:
    assert ZERO_COST.frictionless
    assert ZERO_COST.fill_price(Decimal("100"), Side.BUY) == Decimal("100")
    assert ZERO_COST.fee(Decimal("5"), Decimal("100")) == 0


@pytest.mark.parametrize("rate", [Decimal("-0.01"), Decimal("0.5"), Decimal("1")])
def test_an_implausible_rate_is_refused(rate: Decimal) -> None:
    """A rate at or above 50% is almost certainly basis points typed as a
    fraction, and accepting it would produce a plausible-looking equity curve
    that means nothing."""
    with pytest.raises(CostInputError):
        CostModel(label="t", fee_rate=rate, slippage_rate=Decimal("0"))


def test_a_float_rate_is_refused() -> None:
    with pytest.raises(CostInputError, match="must be a Decimal"):
        CostModel(label="t", fee_rate=0.0025, slippage_rate=Decimal("0"))  # type: ignore[arg-type]


def test_an_unlabelled_cost_model_is_refused() -> None:
    with pytest.raises(CostInputError, match="must be labelled"):
        CostModel(label="  ", fee_rate=Decimal("0"), slippage_rate=Decimal("0"))


def test_the_crypto_fee_matches_the_production_backtester() -> None:
    """The two must agree, or a research replay cannot reproduce a production
    backtest and the equivalence test in test_research_replay.py is a lie."""
    from autotrader.backtest import TAKER_FEE_RATE

    assert CRYPTO_COST.fee_rate == TAKER_FEE_RATE


def test_the_equity_model_charges_no_commission_but_does_charge_spread() -> None:
    """Zero commission is the retail reality; it is not a claim the trade was
    free - the cost moved into the spread."""
    assert EQUITY_COST.fee_rate == 0
    assert EQUITY_COST.slippage_rate > 0


def test_the_stress_model_is_strictly_more_punitive() -> None:
    assert STRESS_COST.fee_rate > CRYPTO_COST.fee_rate
    assert STRESS_COST.slippage_rate > CRYPTO_COST.slippage_rate


def test_every_named_cost_model_is_addressable() -> None:
    for label in COST_MODELS:
        assert cost_model_for(label).label == label


def test_an_unknown_cost_model_lists_the_known_ones() -> None:
    with pytest.raises(CostInputError, match="Known models"):
        cost_model_for("free-money")


def test_a_cost_model_serializes_its_rates_as_exact_strings() -> None:
    document = CRYPTO_COST.to_json_dict()
    assert document["fee_rate"] == str(CRYPTO_COST.fee_rate)
    assert isinstance(document["slippage_rate"], str)


# ==========================================================================
# Isolation: this package is research, not trading
# ==========================================================================


def research_modules() -> list[Path]:
    root = Path(engines_module.__file__).parent
    return sorted(root.rglob("*.py"))


def test_no_research_module_names_the_order_api() -> None:
    """docs/SPEC.md section 6A. The backtester's own guard covers the whole
    source tree; this states the rule for this package explicitly, so a future
    reader of `research` sees it without reading `test_backtest.py`."""
    forbidden = ("TradingClient", "submit_order", "OrderRequest", "MarketOrderRequest")
    for module in research_modules():
        text = module.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{module.name} names {token}"


def test_no_research_module_imports_a_broker_sdk() -> None:
    for module in research_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not name.startswith("alpaca"), f"{module.name} imports {name}"


def test_no_research_module_touches_the_operational_state() -> None:
    """A study must not be able to change what the trading system does."""
    for module in research_modules():
        text = module.read_text(encoding="utf-8")
        for token in ("autotrader.state", "sqlite3", "consume_api_budget"):
            assert token not in text, f"{module.name} names {token}"


def test_no_research_module_opens_a_network_client() -> None:
    for module in research_modules():
        text = module.read_text(encoding="utf-8")
        for token in ("requests.", "urllib.request", "httpx.", "socket.socket"):
            assert token not in text, f"{module.name} names {token}"


def test_the_evaluator_names_no_strategy() -> None:
    """CRITICAL. The whole point of the seam: a future V2/V3/V4/V5 engine is
    evaluated by writing an adapter, not by the infrastructure learning about
    it. Only `engines` may know a strategy exists."""
    root = Path(engines_module.__file__).parent
    evaluator_modules = (
        "replay.py",
        "metrics.py",
        "splits.py",
        "walkforward.py",
        "experiments.py",
        "trades.py",
        "leakage.py",
    )
    for name in evaluator_modules:
        text = (root / name).read_text(encoding="utf-8")
        for token in ("ema_cross", "EmaCross", "generate_ema_cross_signals", "ParametricEma"):
            assert token not in text, f"{name} names the strategy {token}"
