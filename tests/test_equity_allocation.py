"""The shared-account allocator: order-independent, symmetric, and capped.

Blocker 2 of the failed activation was that no sizing policy existed, and the
runtime's stand-in - ask for a billion shares, let Risk clamp - funded six of
ten symbols by their index in a Python tuple. Every property that failure
violated is asserted here directly:

*Order independence.* The same active set in any order produces byte-identical
weights, and a plan built from a reversed or shuffled universe is the same plan.

*Symmetry.* Two symbols that are both active are sized identically. Always, at
every active-set size and every external-exposure level, not on a sample.

*The ceilings hold by construction.* No weight exceeds the per-symbol cap and
no total exceeds the account cap, checked exhaustively over every reachable
active-set size and a grid of external exposures.

*Crypto counts.* The budget is reduced by exactly what the account already
holds outside the equity book.

*Target semantics.* Equal means no order; more means buy the difference; less
means sell only the excess; zero means close the long and never further.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from autotrader.equity import EQUITY_SYMBOLS, EquityError
from autotrader.equity.allocation import (
    DEFAULT_FIXED_TARGET,
    POLICY_EQUAL_ACTIVE,
    POLICY_FIXED_PRO_RATA,
    POLICY_IDS,
    POLICY_RESERVED_UNIVERSE,
    AllocationError,
    AllocationPolicy,
    allocation_policy_for,
    available_budget_fraction,
    external_exposure_fraction_from,
    plan_allocation,
    target_weights,
    whole_shares,
)
from autotrader.execution.models import OrderSide
from autotrader.risk.engine import MAX_POSITION_FRACTION, MAX_TOTAL_EXPOSURE_FRACTION

#: Every external-exposure level the study predeclared, plus the boundary cases
#: that only arithmetic can reach.
EXTERNALS = (
    Decimal("0.00"),
    Decimal("0.05"),
    Decimal("0.10"),
    Decimal("0.29"),
    Decimal("0.30"),
    Decimal("0.45"),
)

ALL_POLICIES = tuple(allocation_policy_for(policy_id) for policy_id in POLICY_IDS)

PRICES = {
    symbol: Decimal(price)
    for symbol, price in zip(
        EQUITY_SYMBOLS,
        (765, 714, 293, 314, 510, 219, 261, 338, 571, 366),
        strict=True,
    )
}

EQUITY = Decimal("100000")


# ==========================================================================
# The caps this module may never exceed are the Risk Engine's own
# ==========================================================================


def test_the_default_ceilings_are_read_from_the_risk_engine() -> None:
    """CRITICAL. A cap copied by hand is a cap that can drift out of step."""
    policy = AllocationPolicy(policy_id=POLICY_EQUAL_ACTIVE)
    assert policy.per_symbol_cap == Decimal(str(MAX_POSITION_FRACTION))
    assert policy.total_cap == Decimal(str(MAX_TOTAL_EXPOSURE_FRACTION))


def test_policy_b_target_is_the_weight_that_fills_the_total_cap_with_ten() -> None:
    """The 3% figure is a consequence of the two caps, not a chosen number."""
    assert DEFAULT_FIXED_TARGET * len(EQUITY_SYMBOLS) == Decimal(str(MAX_TOTAL_EXPOSURE_FRACTION))


def test_an_unknown_policy_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(AllocationError, match="Unknown allocation policy"):
        AllocationPolicy(policy_id="D_SOMETHING_PLAUSIBLE")


def test_the_config_hash_changes_with_every_field_and_is_stable() -> None:
    """What a service logs to prove it runs the policy that was validated."""
    base = AllocationPolicy(policy_id=POLICY_EQUAL_ACTIVE)
    assert base.config_hash() == AllocationPolicy(policy_id=POLICY_EQUAL_ACTIVE).config_hash()
    widened = AllocationPolicy(policy_id=POLICY_EQUAL_ACTIVE, per_symbol_cap=Decimal("0.06"))
    assert widened.config_hash() != base.config_hash()


# ==========================================================================
# Order independence - the property the tuple-order failure violated
# ==========================================================================


@pytest.mark.parametrize("policy", ALL_POLICIES, ids=POLICY_IDS)
@pytest.mark.parametrize("external", EXTERNALS, ids=str)
def test_weights_are_identical_under_every_ordering_of_the_active_set(
    policy: AllocationPolicy, external: Decimal
) -> None:
    """CRITICAL. Tuple order decided who got funded before. It decides nothing now."""
    active = list(EQUITY_SYMBOLS)
    reference = target_weights(policy, active_symbols=active, external_exposure_fraction=external)

    rng = random.Random(20260831)
    for _ in range(25):
        shuffled = active[:]
        rng.shuffle(shuffled)
        assert (
            target_weights(policy, active_symbols=shuffled, external_exposure_fraction=external)
            == reference
        )
    assert (
        target_weights(policy, active_symbols=reversed(active), external_exposure_fraction=external)
        == reference
    )
    assert (
        target_weights(policy, active_symbols=set(active), external_exposure_fraction=external)
        == reference
    )


@pytest.mark.parametrize("policy", ALL_POLICIES, ids=POLICY_IDS)
def test_a_whole_plan_is_identical_under_every_ordering(policy: AllocationPolicy) -> None:
    """CRITICAL. Not just the weights: the quantities and the deltas too."""
    actual = dict.fromkeys(EQUITY_SYMBOLS, Decimal(0))
    forward = plan_allocation(
        policy,
        active_symbols=EQUITY_SYMBOLS,
        account_equity=EQUITY,
        external_exposure_fraction=Decimal("0.05"),
        reference_prices=PRICES,
        actual_quantities=actual,
    )
    backward = plan_allocation(
        policy,
        active_symbols=tuple(reversed(EQUITY_SYMBOLS)),
        account_equity=EQUITY,
        external_exposure_fraction=Decimal("0.05"),
        reference_prices=dict(reversed(list(PRICES.items()))),
        actual_quantities=dict(reversed(list(actual.items()))),
    )
    assert forward.allocations == backward.allocations


# ==========================================================================
# Symmetry and the ceilings, exhaustively
# ==========================================================================


@pytest.mark.parametrize("policy", ALL_POLICIES, ids=POLICY_IDS)
def test_equivalent_active_symbols_are_always_sized_identically(
    policy: AllocationPolicy,
) -> None:
    """CRITICAL. Every active-set size, every external level, one weight each."""
    for count in range(1, len(EQUITY_SYMBOLS) + 1):
        for external in EXTERNALS:
            weights = target_weights(
                policy,
                active_symbols=EQUITY_SYMBOLS[:count],
                external_exposure_fraction=external,
            )
            assert len(set(weights.values())) == 1, (policy.policy_id, count, external)


@pytest.mark.parametrize("policy", ALL_POLICIES, ids=POLICY_IDS)
def test_no_weight_exceeds_the_per_symbol_cap_and_no_total_exceeds_the_account_cap(
    policy: AllocationPolicy,
) -> None:
    """CRITICAL. Exhaustive over the reachable inputs, not sampled."""
    for count in range(1, len(EQUITY_SYMBOLS) + 1):
        for external in EXTERNALS:
            weights = target_weights(
                policy,
                active_symbols=EQUITY_SYMBOLS[:count],
                external_exposure_fraction=external,
            )
            for weight in weights.values():
                assert weight <= policy.per_symbol_cap, (policy.policy_id, count, external)
            total = sum(weights.values(), Decimal(0))
            # Against the *available* budget, which is what the allocator
            # controls. An external book already past the account ceiling puts
            # the account over it on its own; what the allocator must never do
            # is add to that, and a budget of zero is how it does not.
            assert total <= available_budget_fraction(policy, external), (
                policy.policy_id,
                count,
                external,
            )


@pytest.mark.parametrize("policy", ALL_POLICIES, ids=POLICY_IDS)
def test_an_external_book_that_has_eaten_the_whole_ceiling_leaves_no_budget(
    policy: AllocationPolicy,
) -> None:
    """A crypto position past the budget target leaves zero budget, never a negative one."""
    weights = target_weights(
        policy,
        active_symbols=EQUITY_SYMBOLS,
        external_exposure_fraction=policy.budget_target + Decimal("0.15"),
    )
    assert set(weights.values()) == {Decimal(0)}


def test_a_negative_external_exposure_is_refused_rather_than_manufacturing_budget() -> None:
    policy = AllocationPolicy(policy_id=POLICY_EQUAL_ACTIVE)
    with pytest.raises(AllocationError, match="cannot be negative"):
        available_budget_fraction(policy, Decimal("-0.01"))


def test_more_active_symbols_than_the_frozen_universe_is_refused() -> None:
    """A universe this policy was never validated on is not silently allocated.

    Unreachable through the production universe - `normalize_symbol` refuses a
    symbol outside the frozen ten before the count is ever taken - so it is
    exercised through a policy declaring a smaller universe, which is the shape
    a future narrowed rollout would have.
    """
    policy = AllocationPolicy(policy_id=POLICY_EQUAL_ACTIVE, universe_size=3)
    with pytest.raises(AllocationError, match="exceeds the frozen universe size"):
        target_weights(
            policy,
            active_symbols=EQUITY_SYMBOLS[:4],
            external_exposure_fraction=Decimal(0),
        )


def test_a_symbol_outside_the_frozen_universe_never_reaches_the_allocator() -> None:
    """The universe guard is the second line; the symbol guard is the first."""
    policy = AllocationPolicy(policy_id=POLICY_EQUAL_ACTIVE)
    with pytest.raises(EquityError):
        target_weights(
            policy, active_symbols=("NOTASYMBOL",), external_exposure_fraction=Decimal(0)
        )


# ==========================================================================
# The three policies do what the ledger predeclared
# ==========================================================================


def test_policy_a_fills_the_available_budget_across_the_active_symbols() -> None:
    policy = AllocationPolicy(policy_id=POLICY_EQUAL_ACTIVE)
    ten = target_weights(
        policy, active_symbols=EQUITY_SYMBOLS, external_exposure_fraction=Decimal(0)
    )
    assert set(ten.values()) == {Decimal("0.030000000000")}
    four = target_weights(
        policy, active_symbols=EQUITY_SYMBOLS[:4], external_exposure_fraction=Decimal(0)
    )
    # Four symbols cannot absorb 30% at a 5% ceiling; the ceiling binds.
    assert set(four.values()) == {Decimal("0.050000000000")}


def test_policy_b_holds_a_constant_target_until_the_budget_binds() -> None:
    policy = AllocationPolicy(policy_id=POLICY_FIXED_PRO_RATA)
    four = target_weights(
        policy, active_symbols=EQUITY_SYMBOLS[:4], external_exposure_fraction=Decimal(0)
    )
    assert set(four.values()) == {Decimal("0.030000000000")}
    ten_contended = target_weights(
        policy, active_symbols=EQUITY_SYMBOLS, external_exposure_fraction=Decimal("0.10")
    )
    # 20% available across ten desired 3% targets scales all of them, equally.
    assert set(ten_contended.values()) == {Decimal("0.020000000000")}


def test_policy_c_reserves_a_share_per_universe_symbol_regardless_of_who_is_active() -> None:
    policy = AllocationPolicy(policy_id=POLICY_RESERVED_UNIVERSE)
    for count in (1, 4, 7, 10):
        weights = target_weights(
            policy,
            active_symbols=EQUITY_SYMBOLS[:count],
            external_exposure_fraction=Decimal("0.05"),
        )
        assert set(weights.values()) == {Decimal("0.025000000000")}, count


def test_no_policy_zeroes_a_later_symbol_to_fund_an_earlier_one() -> None:
    """CRITICAL. The exact failure mode of the clamp-based stand-in."""
    for policy in ALL_POLICIES:
        weights = target_weights(
            policy, active_symbols=EQUITY_SYMBOLS, external_exposure_fraction=Decimal("0.10")
        )
        assert all(weight > 0 for weight in weights.values()), policy.policy_id
        assert len(weights) == len(EQUITY_SYMBOLS)


# ==========================================================================
# Crypto counts against the same account
# ==========================================================================


def test_external_exposure_is_measured_against_account_equity() -> None:
    fraction = external_exposure_fraction_from(account_equity=99824.63, non_equity_exposure=4997.40)
    assert Decimal("0.0500") < fraction < Decimal("0.0502")


def test_crypto_exposure_reduces_the_equity_budget_one_for_one() -> None:
    policy = AllocationPolicy(policy_id=POLICY_EQUAL_ACTIVE)
    without = target_weights(
        policy, active_symbols=EQUITY_SYMBOLS, external_exposure_fraction=Decimal(0)
    )
    with_crypto = target_weights(
        policy, active_symbols=EQUITY_SYMBOLS, external_exposure_fraction=Decimal("0.05")
    )
    total_without = sum(without.values(), Decimal(0))
    total_with = sum(with_crypto.values(), Decimal(0))
    assert total_without - total_with == Decimal("0.05")


# ==========================================================================
# Quantization and target semantics
# ==========================================================================


def test_whole_shares_rounds_down_and_never_asks_for_more_than_the_target() -> None:
    assert whole_shares(Decimal("3000"), Decimal("765.56")) == Decimal(3)
    assert whole_shares(Decimal("764"), Decimal("765.56")) == Decimal(0)
    assert whole_shares(Decimal(0), Decimal("765.56")) == Decimal(0)


def test_a_non_positive_price_cannot_size_anything() -> None:
    with pytest.raises(AllocationError, match="non-positive price"):
        whole_shares(Decimal("3000"), Decimal(0))


def _plan(active: tuple[str, ...], actual: dict[str, Decimal]):
    return plan_allocation(
        AllocationPolicy(policy_id=POLICY_RESERVED_UNIVERSE),
        active_symbols=active,
        account_equity=EQUITY,
        external_exposure_fraction=Decimal(0),
        reference_prices=PRICES,
        actual_quantities=actual,
    )


def test_a_target_equal_to_the_holding_produces_no_order() -> None:
    """CRITICAL. The normal case for a target-state strategy, and it must be silent."""
    opening = _plan(("SPY",), {})
    spy = next(item for item in opening.allocations if item.symbol == "SPY")
    settled = _plan(("SPY",), {"SPY": spy.target_quantity})
    held = next(item for item in settled.allocations if item.symbol == "SPY")
    assert held.side is None
    assert held.delta_quantity == Decimal(0)
    assert held.orders is False
    assert settled.ordering == ()


def test_a_target_above_the_holding_buys_only_the_difference() -> None:
    plan = _plan(("SPY",), {"SPY": Decimal(1)})
    spy = next(item for item in plan.allocations if item.symbol == "SPY")
    assert spy.side is OrderSide.BUY
    assert spy.delta_quantity == spy.target_quantity - Decimal(1)


def test_a_target_below_the_holding_sells_only_the_excess() -> None:
    plan = _plan(("SPY",), {"SPY": Decimal(50)})
    spy = next(item for item in plan.allocations if item.symbol == "SPY")
    assert spy.side is OrderSide.SELL
    assert spy.delta_quantity == Decimal(50) - spy.target_quantity
    assert spy.delta_quantity < Decimal(50)


def test_a_symbol_that_left_the_active_set_is_closed_and_never_oversold() -> None:
    """CRITICAL. desired == 0 closes the long. It does not open a short."""
    plan = _plan(("QQQ",), {"SPY": Decimal(7)})
    spy = next(item for item in plan.allocations if item.symbol == "SPY")
    assert spy.target_weight == Decimal(0)
    assert spy.target_quantity == Decimal(0)
    assert spy.side is OrderSide.SELL
    assert spy.delta_quantity == Decimal(7)


def test_a_sell_is_never_larger_than_the_position() -> None:
    """CRITICAL. No arithmetic path produces a SELL that would go short."""
    for actual in (Decimal(1), Decimal(3), Decimal(7), Decimal("0.5")):
        plan = _plan((), {"SPY": actual})
        spy = next(item for item in plan.allocations if item.symbol == "SPY")
        assert spy.delta_quantity <= actual
        assert spy.side is OrderSide.SELL


def test_a_symbol_that_is_neither_active_nor_held_is_absent_from_the_plan() -> None:
    plan = _plan(("SPY",), {})
    assert {item.symbol for item in plan.allocations} == {"SPY"}


def test_planning_against_a_non_positive_equity_is_refused() -> None:
    with pytest.raises(AllocationError, match="non-positive account equity"):
        plan_allocation(
            AllocationPolicy(policy_id=POLICY_EQUAL_ACTIVE),
            active_symbols=("SPY",),
            account_equity=Decimal(0),
            external_exposure_fraction=Decimal(0),
            reference_prices=PRICES,
            actual_quantities={},
        )


def test_a_wanted_symbol_without_a_price_is_refused_rather_than_guessed() -> None:
    with pytest.raises(AllocationError, match="No reference price"):
        plan_allocation(
            AllocationPolicy(policy_id=POLICY_EQUAL_ACTIVE),
            active_symbols=("SPY",),
            account_equity=EQUITY,
            external_exposure_fraction=Decimal(0),
            reference_prices={},
            actual_quantities={},
        )


def test_the_realized_plan_respects_both_ceilings_at_full_participation() -> None:
    """CRITICAL. Ten simultaneous LONGs - the bootstrap case - stay inside the caps."""
    for policy in ALL_POLICIES:
        plan = plan_allocation(
            policy,
            active_symbols=EQUITY_SYMBOLS,
            account_equity=EQUITY,
            external_exposure_fraction=Decimal("0.05"),
            reference_prices=PRICES,
            actual_quantities={},
        )
        notional = sum(item.target_quantity * item.reference_price for item in plan.allocations)
        assert notional / EQUITY + Decimal("0.05") <= policy.total_cap, policy.policy_id
        for item in plan.allocations:
            assert item.target_quantity * item.reference_price / EQUITY <= policy.per_symbol_cap, (
                policy.policy_id,
                item.symbol,
            )
