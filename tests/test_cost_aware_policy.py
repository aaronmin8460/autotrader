"""The cost-aware research layer: costs, causality, and the boundaries it must not cross.

Four things are pinned here, and the last two are the ones that matter.

1. The round-trip arithmetic is exact and derived from the shipped cost models,
   so a change to the fee assumption moves the break-even with it.
2. A policy may remove or delay an upstream proposal and may never invent one.
3. **A policy can never suppress a protective exit.** Every suppression rule is
   driven past its own condition with a risk-originated exit and must admit it.
4. **A policy cannot see the future.** The wrapper is driven through the
   shipped leakage auditor, which perturbs bars after a probe index and
   requires everything at or before it to be unchanged.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pandas as pd
import pytest
from studies.crypto_cost_aware.costs import (
    BPS,
    RoundTripError,
    breakeven_move,
    breakeven_move_bps,
    naive_round_trip,
    round_trip_cost_fraction,
)
from studies.crypto_cost_aware.policy import (
    MIN_VOLATILITY_BARS,
    CostAwareEngine,
    ExpectedEdgeGate,
    Hysteresis,
    MinimumHold,
    PassThrough,
    PolicyError,
    PolicyState,
    build_candidates,
    is_risk_originated,
)

from autotrader.research.costs import (
    CRYPTO_COST,
    EQUITY_COST,
    STRESS_COST,
    ZERO_COST,
    CostModel,
    Side,
)
from autotrader.research.engines import Action, EmaCrossEngine, ResearchSignal, ScriptedEngine
from autotrader.research.leakage import audit_engine_causality
from research_fixtures import bars_from_closes

SYMBOL = "BTC/USD"


def _oscillating_closes(count: int = 600) -> list[float]:
    """A series that crosses its own moving averages repeatedly.

    Deliberately choppy: the churn this research is about only appears when a
    trigger oscillates, and a monotone series would produce one trade and prove
    nothing about a de-bounce rule.
    """
    closes = []
    price = 30_000.0
    for index in range(count):
        swing = 1.0 + (0.012 if (index // 7) % 2 == 0 else -0.011)
        price *= swing
        closes.append(round(price, 2))
    return closes


@pytest.fixture
def choppy_bars() -> pd.DataFrame:
    return bars_from_closes(_oscillating_closes(), symbol=SYMBOL)


def _signal(timestamp: pd.Timestamp, action: Action, reason: str = "TEST") -> ResearchSignal:
    return ResearchSignal(timestamp=timestamp, symbol=SYMBOL, action=action, reason=reason)


# --------------------------------------------------------------------------
# Round-trip cost arithmetic
# --------------------------------------------------------------------------


def test_frictionless_round_trip_costs_nothing() -> None:
    assert breakeven_move(ZERO_COST) == 0


def test_crypto_break_even_is_sixty_basis_points() -> None:
    """The number the whole research question is measured against."""
    assert round(float(breakeven_move_bps(CRYPTO_COST)), 2) == 60.18


def test_stress_break_even_is_higher_than_crypto() -> None:
    assert breakeven_move(STRESS_COST) > breakeven_move(CRYPTO_COST) > breakeven_move(EQUITY_COST)


def test_exact_break_even_exceeds_the_naive_per_side_sum() -> None:
    """The exit fee is charged on the exit notional, so summing sides is optimistic.

    A break-even biased low is a break-even that lets uneconomic trades
    through, which is the one direction this must not err in.
    """
    for model in (CRYPTO_COST, STRESS_COST, EQUITY_COST):
        assert breakeven_move(model) > naive_round_trip(model)


def test_break_even_rises_with_every_cost_component() -> None:
    base = CostModel(label="t", fee_rate=Decimal("0.001"), slippage_rate=Decimal("0.001"))
    pricier_fee = CostModel(label="t", fee_rate=Decimal("0.002"), slippage_rate=Decimal("0.001"))
    pricier_slip = CostModel(label="t", fee_rate=Decimal("0.001"), slippage_rate=Decimal("0.002"))
    assert breakeven_move(pricier_fee) > breakeven_move(base)
    assert breakeven_move(pricier_slip) > breakeven_move(base)


def test_a_round_trip_at_exactly_break_even_returns_the_capital() -> None:
    """The definition, checked as cash rather than as algebra."""
    model = CRYPTO_COST
    entry_reference = Decimal("30000")
    exit_reference = entry_reference * (Decimal(1) + breakeven_move(model))
    quantity = Decimal("1")

    entry_fill = model.fill_price(entry_reference, Side.BUY)
    exit_fill = model.fill_price(exit_reference, Side.SELL)
    spent = model.buy_cost(quantity, entry_fill)
    received = model.sell_proceeds(quantity, exit_fill)
    assert abs(received - spent) < Decimal("0.0000001")


def test_round_trip_cost_fraction_rejects_impossible_prices() -> None:
    with pytest.raises(RoundTripError):
        round_trip_cost_fraction(CRYPTO_COST, Decimal("0"), Decimal("1"))


def test_round_trip_cost_fraction_is_about_the_break_even_on_a_flat_trade() -> None:
    """A trade that goes nowhere pays roughly the break-even and no more."""
    price = Decimal("30000")
    fraction = round_trip_cost_fraction(CRYPTO_COST, price, price)
    assert abs(fraction - breakeven_move(CRYPTO_COST)) * BPS < 1


# --------------------------------------------------------------------------
# The layer may remove and delay. It may never invent.
# --------------------------------------------------------------------------


def test_passthrough_reproduces_the_upstream_engine_exactly(choppy_bars: pd.DataFrame) -> None:
    """The control. If this ever diverges, the wrapper has side effects."""
    upstream = EmaCrossEngine()
    wrapped = CostAwareEngine(upstream, PassThrough())
    baseline = [s for s in upstream.generate(choppy_bars)]

    # The wrapper drops proposals the simulator would treat as no-ops, so the
    # comparison is against the upstream stream with those same no-ops removed.
    expected, holding = [], False
    for signal in baseline:
        if signal.action is Action.ENTER_LONG and holding:
            continue
        if signal.action is Action.EXIT_LONG and not holding:
            continue
        expected.append(signal)
        holding = signal.action is Action.ENTER_LONG

    assert list(wrapped.generate(choppy_bars)) == expected


@pytest.mark.parametrize("label", sorted(build_candidates(CRYPTO_COST)))
def test_no_policy_invents_a_proposal(choppy_bars: pd.DataFrame, label: str) -> None:
    """Every admitted signal was proposed by the upstream engine, unchanged."""
    upstream = EmaCrossEngine()
    policy = build_candidates(CRYPTO_COST)[label]
    proposed = {(s.timestamp, s.action) for s in upstream.generate(choppy_bars)}
    admitted = CostAwareEngine(upstream, policy).generate(choppy_bars)

    assert len(admitted) <= len(proposed)
    for signal in admitted:
        assert (signal.timestamp, signal.action) in proposed


@pytest.mark.parametrize("label", sorted(build_candidates(CRYPTO_COST)))
def test_every_policy_is_deterministic(choppy_bars: pd.DataFrame, label: str) -> None:
    policy = build_candidates(CRYPTO_COST)[label]
    engine = CostAwareEngine(EmaCrossEngine(), policy)
    assert list(engine.generate(choppy_bars)) == list(engine.generate(choppy_bars))


def test_minimum_hold_delays_an_exit_until_the_hold_is_met(choppy_bars: pd.DataFrame) -> None:
    timestamps = list(choppy_bars["timestamp"])
    scripted = ScriptedEngine(
        signals=(
            _signal(timestamps[100], Action.ENTER_LONG),
            _signal(timestamps[103], Action.EXIT_LONG),  # 3 bars later: too soon
            _signal(timestamps[140], Action.EXIT_LONG),  # 40 bars later: allowed
        )
    )
    admitted = CostAwareEngine(scripted, MinimumHold(bars=32)).generate(choppy_bars)
    assert [s.timestamp for s in admitted] == [timestamps[100], timestamps[140]]


def test_hysteresis_requires_the_proposal_to_persist(choppy_bars: pd.DataFrame) -> None:
    timestamps = list(choppy_bars["timestamp"])
    scripted = ScriptedEngine(
        signals=(
            _signal(timestamps[200], Action.ENTER_LONG),  # single bar: refused
            _signal(timestamps[300], Action.ENTER_LONG),
            _signal(timestamps[301], Action.ENTER_LONG),  # second consecutive: admitted
        )
    )
    admitted = CostAwareEngine(scripted, Hysteresis(bars=2)).generate(choppy_bars)
    assert [s.timestamp for s in admitted] == [timestamps[301]]


def test_edge_gate_refuses_an_entry_it_cannot_estimate(choppy_bars: pd.DataFrame) -> None:
    """During warm-up the volatility is undefined, and unknown is not eligible."""
    timestamps = list(choppy_bars["timestamp"])
    scripted = ScriptedEngine(signals=(_signal(timestamps[3], Action.ENTER_LONG),))
    gate = ExpectedEdgeGate(multiple=1.0, horizon_bars=96, cost_model=CRYPTO_COST)
    assert CostAwareEngine(scripted, gate).generate(choppy_bars) == ()


def test_edge_gate_threshold_scales_with_the_cost_model() -> None:
    cheap = ExpectedEdgeGate(multiple=1.0, horizon_bars=96, cost_model=CRYPTO_COST)
    dear = ExpectedEdgeGate(multiple=1.0, horizon_bars=96, cost_model=STRESS_COST)
    assert dear.threshold > cheap.threshold


@pytest.mark.parametrize(
    "factory",
    [
        lambda: MinimumHold(bars=-1),
        lambda: Hysteresis(bars=0),
        lambda: ExpectedEdgeGate(multiple=0, horizon_bars=96, cost_model=CRYPTO_COST),
        lambda: ExpectedEdgeGate(multiple=1, horizon_bars=0, cost_model=CRYPTO_COST),
        lambda: ExpectedEdgeGate(
            multiple=1, horizon_bars=96, cost_model=CRYPTO_COST, volatility_bars=4
        ),
    ],
)
def test_a_policy_refuses_a_configuration_it_cannot_enforce(factory) -> None:
    with pytest.raises(PolicyError):
        factory()


# --------------------------------------------------------------------------
# Risk boundary: a research filter may never hold a protective exit
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "STOP_LOSS_HIT",
        "RISK_LIMIT_BREACH",
        "FORCED_LIQUIDATION",
        "DAILY_LOSS_HALT",
        "MAX_DRAWDOWN",
        "PROTECTIVE_EXIT",
        "EMERGENCY_FLATTEN",
        "KILL_SWITCH",
        "RECONCILIATION_FLATTEN",
        "score_below|risk_limit_breach",
    ],
)
def test_protective_reasons_are_recognised(reason: str) -> None:
    when = pd.Timestamp("2025-01-01", tz="UTC")
    assert is_risk_originated(_signal(when, Action.EXIT_LONG, reason))


def test_an_ordinary_opinion_is_not_mistaken_for_a_protective_exit() -> None:
    assert not is_risk_originated(
        _signal(pd.Timestamp("2025-01-01", tz="UTC"), Action.EXIT_LONG, "EMA20_CROSS_BELOW_EMA50")
    )


def test_minimum_hold_never_delays_a_protective_exit(choppy_bars: pd.DataFrame) -> None:
    """The safety invariant. A cost filter that could hold a stop is not allowed to exist."""
    timestamps = list(choppy_bars["timestamp"])
    scripted = ScriptedEngine(
        signals=(
            _signal(timestamps[100], Action.ENTER_LONG),
            _signal(timestamps[101], Action.EXIT_LONG, "STOP_LOSS_HIT"),
        )
    )
    admitted = CostAwareEngine(scripted, MinimumHold(bars=96)).generate(choppy_bars)
    assert [s.timestamp for s in admitted] == [timestamps[100], timestamps[101]]


def test_hysteresis_never_delays_a_protective_exit(choppy_bars: pd.DataFrame) -> None:
    timestamps = list(choppy_bars["timestamp"])
    # The entry must persist long enough to be admitted, or the exit would be a
    # no-op while flat and would never reach the policy at all.
    entries = tuple(_signal(timestamps[100 + n], Action.ENTER_LONG) for n in range(8))
    scripted = ScriptedEngine(
        signals=(*entries, _signal(timestamps[150], Action.EXIT_LONG, "RISK_LIMIT_BREACH")),
    )
    admitted = CostAwareEngine(scripted, Hysteresis(bars=8)).generate(choppy_bars)
    assert [s.timestamp for s in admitted] == [timestamps[107], timestamps[150]]


def test_the_edge_gate_never_blocks_any_exit(choppy_bars: pd.DataFrame) -> None:
    """The gate is an *entry* condition. It has no opinion about getting out."""
    timestamps = list(choppy_bars["timestamp"])
    scripted = ScriptedEngine(
        signals=(
            _signal(timestamps[200], Action.ENTER_LONG),
            _signal(timestamps[260], Action.EXIT_LONG, "SCORE_BELOW_SELL_THRESHOLD"),
        )
    )
    gate = ExpectedEdgeGate(multiple=1.0, horizon_bars=96, cost_model=CRYPTO_COST)
    admitted = CostAwareEngine(scripted, gate).generate(choppy_bars)
    assert timestamps[260] in [s.timestamp for s in admitted]


# --------------------------------------------------------------------------
# Causality
# --------------------------------------------------------------------------


@pytest.mark.parametrize("label", sorted(build_candidates(CRYPTO_COST)))
def test_no_policy_can_see_the_future(choppy_bars: pd.DataFrame, label: str) -> None:
    """Driven through the shipped auditor, against a *computing* upstream.

    The auditor perturbs every bar after a probe index and re-asks. Anything the
    wrapper said at or before that index must be byte-identical. Using a real
    engine rather than a stored series is the point: a lookup table would pass
    this for a reason that is not evidence.
    """
    policy = build_candidates(CRYPTO_COST)[label]
    engine = CostAwareEngine(EmaCrossEngine(), policy)
    report = audit_engine_causality(engine, choppy_bars)
    assert not report.findings, report.findings


def test_the_wrappers_volatility_is_trailing_only(choppy_bars: pd.DataFrame) -> None:
    """Perturbing the tail must not move an earlier bar's volatility estimate."""
    engine = CostAwareEngine(EmaCrossEngine(), PassThrough(), volatility_bars=MIN_VOLATILITY_BARS)
    cutoff = 300
    original = engine._trailing_volatility(choppy_bars)
    tampered = choppy_bars.copy()
    tampered.loc[tampered.index[cutoff + 1 :], "close"] *= 1.5
    perturbed = engine._trailing_volatility(tampered)

    # NaN during warm-up is a legitimate "no estimate yet" and compares unequal
    # to itself, so the check is on the pair rather than on the list.
    for index, (before, after) in enumerate(
        zip(original[: cutoff + 1], perturbed[: cutoff + 1], strict=True)
    ):
        assert (math.isnan(before) and math.isnan(after)) or before == after, index

    # The tail must actually have moved, or the test would pass on a no-op.
    assert any(
        not math.isnan(a) and not math.isnan(b) and a != b
        for a, b in zip(original[cutoff + 2 :], perturbed[cutoff + 2 :], strict=True)
    )


def test_policy_state_carries_only_its_own_history() -> None:
    """A structural check: the state object has no field that could hold an outcome."""
    assert set(PolicyState.__dataclass_fields__) == {
        "holding",
        "bars_held",
        "consecutive_signal_bars",
    }
