"""EDA1_FRACTIONAL_RESERVED_90: the fractional 90%-target policy, end to end.

The operator-authorized successor to `C_RESERVED_UNIVERSE`. Everything the
migration predeclaration promises is asserted here directly:

*The account-wide budget.* The equity budget is `max(0, 0.90 - X)` where X is
the account's non-equity exposure - never "90% equity plus crypto". A FLAT
symbol's reserved slot stays cash and is never redistributed.

*The four critical sizing cases.* $100k/X=0 -> ~$9,000 each; $100k/X=5% ->
~$8,500 each and ~90% combined; $150/X=0 ten LONG -> ~$13.50 each; $150 four
LONG -> ~$54 total, NOT $135.

*Fractional support without churn.* Targets are fractional quantities; an
adjustment is an order only past the deadband ($1 AND 1% of slot); transitions
are exempt in both directions.

*The hard caps ride into Risk.* 11% per symbol and 95% account-wide, projected
against broker truth per order, with the 2% daily halt explicitly unchanged.

*Shared-account hardening.* The peer store's execution lock is contendable
read-only and fails closed when absent; the composite lock takes both in one
fixed order; a broker open order blocks new BUYs and same-symbol SELLs.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alpaca.trading.enums import TimeInForce

from autotrader.account.lock import (
    AccountExecutionLock,
    AccountExecutionLockError,
    CompositeAccountLock,
    account_lock_path_for,
)
from autotrader.equity import EQUITY_SYMBOLS
from autotrader.equity.allocation import (
    FRACTIONAL_SHARE_INCREMENT,
    HARD_ACCOUNT_GROSS_CAP,
    HARD_SYMBOL_GROSS_CAP,
    POLICY_FRACTIONAL_RESERVED_90,
    POLICY_RESERVED_UNIVERSE,
    REBALANCE_MIN_ABSOLUTE_NOTIONAL,
    REBALANCE_MIN_SLOT_FRACTION,
    RESERVED_EQUITY_SLOTS,
    TARGET_ACCOUNT_GROSS,
    AllocationError,
    AllocationPolicy,
    allocation_policy_for,
    plan_allocation,
    risk_policy_for,
    target_weights,
)
from autotrader.equity.paper import (
    AlpacaEquityPaperGateway,
    Disposition,
    foreign_open_order_symbols,
)
from autotrader.execution.equity import (
    MINIMUM_FRACTIONAL_ORDER_NOTIONAL,
    EquityAssetNotTradableError,
    OpenOrderRecord,
    build_fractional_equity_market_order_request,
    normalize_fractional_share_quantity,
)
from autotrader.execution.models import OrderIntent, OrderSide
from autotrader.execution.paper import ExecutionOutcome, QuantityBelowMinimumError
from autotrader.risk import (
    APPROVED,
    DAILY_LOSS_LIMIT,
    DEFAULT_POLICY,
    POSITION_LIMIT,
    TOTAL_EXPOSURE_LIMIT,
    RiskContext,
    RiskRequest,
    RiskSide,
    evaluate_risk,
)
from autotrader.state.sqlite import connect, initialize_database
from conftest import establish_account_safety
from test_equity_execution import (
    FakeDataClient,
    FakeTradingClient,
    make_account,
    make_asset,
    make_position,
    run_execution,
)
from test_equity_paper import (
    PRICES,
    FakeBrokerState,
    RecordingGateway,
    build_paper,
)

pytestmark = pytest.mark.filterwarnings("ignore::FutureWarning")

FRACTIONAL = allocation_policy_for(POLICY_FRACTIONAL_RESERVED_90)

U10 = EQUITY_SYMBOLS
T0 = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)

#: The frozen legacy hash the sizing study published. If this ever moves, the
#: rollback target is no longer the policy that was validated.
LEGACY_HASH = "c47288c2aafd84262a1257b783614efead995735027c535aa36d23b2dd9f5277"

TEST_PRICES = {symbol: Decimal("100") for symbol in U10}


def frac_plan(
    *,
    active: tuple[str, ...] = U10,
    equity: str = "100000",
    external: str = "0",
    prices: dict[str, Decimal] | None = None,
    actual: dict[str, Decimal] | None = None,
):
    return plan_allocation(
        FRACTIONAL,
        active_symbols=active,
        account_equity=Decimal(equity),
        external_exposure_fraction=Decimal(external),
        reference_prices=prices if prices is not None else TEST_PRICES,
        actual_quantities=actual if actual is not None else {},
    )


def plan_notional(plan) -> Decimal:
    return sum(
        (item.target_quantity * item.reference_price for item in plan.allocations),
        Decimal(0),
    )


# ==========================================================================
# Policy identity
# ==========================================================================


def test_the_legacy_policy_hash_is_frozen() -> None:
    """CRITICAL. The rollback target must digest to what the study froze."""
    assert allocation_policy_for(POLICY_RESERVED_UNIVERSE).config_hash() == LEGACY_HASH


def test_the_fractional_policy_carries_the_named_constants() -> None:
    assert FRACTIONAL.per_symbol_cap == HARD_SYMBOL_GROSS_CAP == Decimal("0.11")
    assert FRACTIONAL.total_cap == HARD_ACCOUNT_GROSS_CAP == Decimal("0.95")
    assert FRACTIONAL.budget_target == TARGET_ACCOUNT_GROSS == Decimal("0.90")
    assert FRACTIONAL.universe_size == RESERVED_EQUITY_SLOTS == 10
    assert FRACTIONAL.fractional is True
    assert FRACTIONAL.deadband_min_notional == REBALANCE_MIN_ABSOLUTE_NOTIONAL
    assert FRACTIONAL.deadband_slot_fraction == REBALANCE_MIN_SLOT_FRACTION


def test_the_fractional_policy_hash_is_stable_and_field_sensitive() -> None:
    assert (
        FRACTIONAL.config_hash()
        == allocation_policy_for(POLICY_FRACTIONAL_RESERVED_90).config_hash()
    )
    widened = AllocationPolicy(
        policy_id=POLICY_FRACTIONAL_RESERVED_90,
        per_symbol_cap=Decimal("0.12"),
        total_cap=FRACTIONAL.total_cap,
        target_gross=FRACTIONAL.target_gross,
        universe_size=FRACTIONAL.universe_size,
        fractional=True,
        deadband_min_notional=FRACTIONAL.deadband_min_notional,
        deadband_slot_fraction=FRACTIONAL.deadband_slot_fraction,
    )
    assert widened.config_hash() != FRACTIONAL.config_hash()


def test_the_fractional_name_cannot_be_constructed_with_legacy_defaults() -> None:
    """The name must mean the frozen parameter set, not whatever fields say."""
    with pytest.raises(AllocationError, match="allocation_policy_for"):
        AllocationPolicy(policy_id=POLICY_FRACTIONAL_RESERVED_90)


def test_a_target_past_the_hard_cap_is_refused() -> None:
    with pytest.raises(AllocationError, match="target_gross"):
        AllocationPolicy(
            policy_id=POLICY_RESERVED_UNIVERSE,
            target_gross=Decimal("0.40"),
            total_cap=Decimal("0.30"),
        )


def test_the_risk_policy_for_the_fractional_policy_is_11_95_2() -> None:
    """CRITICAL. Hard caps ride into Risk; the daily halt stays at 2%."""
    limits = risk_policy_for(FRACTIONAL)
    assert limits.max_position_fraction == 0.11
    assert limits.max_total_exposure_fraction == 0.95
    assert limits.max_daily_loss_fraction == DEFAULT_POLICY.max_daily_loss_fraction == 0.02
    assert limits.long_only is True
    assert limits.allow_leverage is False


def test_legacy_policies_keep_the_default_risk_policy() -> None:
    assert risk_policy_for(allocation_policy_for(POLICY_RESERVED_UNIVERSE)) is DEFAULT_POLICY


# ==========================================================================
# The four critical sizing cases, mechanically
# ==========================================================================


def test_case_1_100k_no_crypto_ten_long_is_9000_each() -> None:
    plan = frac_plan()
    total = plan_notional(plan)
    assert Decimal("89999") < total <= Decimal("90000")
    for item in plan.allocations:
        value = item.target_quantity * item.reference_price
        assert Decimal("8999.9") < value <= Decimal("9000")


def test_case_2_100k_five_percent_crypto_is_8500_each_and_90_combined() -> None:
    plan = frac_plan(external="0.05")
    total = plan_notional(plan)
    assert Decimal("84999") < total <= Decimal("85000")
    # Combined account target: 85% equity + 5% crypto = 90% gross.
    assert total / Decimal("100000") + Decimal("0.05") <= Decimal("0.90")


def test_case_3_150_dollar_account_ten_long_is_13_50_each() -> None:
    plan = frac_plan(equity="150")
    total = plan_notional(plan)
    assert Decimal("134.9") < total <= Decimal("135")
    for item in plan.allocations:
        value = item.target_quantity * item.reference_price
        assert Decimal("13.49") < value <= Decimal("13.50")
        assert item.target_quantity == item.target_quantity.quantize(FRACTIONAL_SHARE_INCREMENT)


def test_case_4_150_dollar_account_four_long_is_54_not_135() -> None:
    """CRITICAL. DEFENSIVE stays defensive: a FLAT slot is cash, not a bonus."""
    plan = frac_plan(active=U10[:4], equity="150")
    total = plan_notional(plan)
    assert Decimal("53.9") < total <= Decimal("54")
    weights = {item.target_weight for item in plan.allocations}
    assert weights == {Decimal("0.090000000000")}


def test_100k_ten_percent_crypto_is_8000_each() -> None:
    plan = frac_plan(external="0.10")
    total = plan_notional(plan)
    assert Decimal("79999") < total <= Decimal("80000")


def test_150_dollar_account_five_percent_crypto() -> None:
    plan = frac_plan(equity="150", external="0.05")
    total = plan_notional(plan)
    # 85% of $150 = $127.50 across ten slots.
    assert Decimal("127.4") < total <= Decimal("127.5")


def test_zero_long_plans_nothing_and_closes_everything_held() -> None:
    plan = frac_plan(active=(), actual={"SPY": Decimal("0.135")})
    spy = next(item for item in plan.allocations if item.symbol == "SPY")
    assert spy.side is OrderSide.SELL
    assert spy.delta_quantity == Decimal("0.135")
    assert len(plan.allocations) == 1


def test_a_very_expensive_symbol_on_a_150_dollar_account_gets_a_fractional_sliver() -> None:
    prices = dict(TEST_PRICES)
    prices["NVDA"] = Decimal("700")
    plan = frac_plan(equity="150", prices=prices)
    nvda = next(item for item in plan.allocations if item.symbol == "NVDA")
    assert Decimal(0) < nvda.target_quantity < Decimal(1)
    assert nvda.target_quantity == Decimal("0.019285714")
    assert nvda.side is OrderSide.BUY


def test_an_external_book_past_the_target_leaves_zero_budget() -> None:
    weights = target_weights(
        FRACTIONAL, active_symbols=U10, external_exposure_fraction=Decimal("0.92")
    )
    assert set(weights.values()) == {Decimal(0)}


def test_the_symbol_cap_never_binds_at_assignment() -> None:
    """B/N <= 9% < 11%: the hard cap bounds drift, not the plan."""
    for external in ("0", "0.05", "0.10", "0.50"):
        weights = target_weights(
            FRACTIONAL,
            active_symbols=U10,
            external_exposure_fraction=Decimal(external),
        )
        for weight in weights.values():
            assert weight <= Decimal("0.09")


# ==========================================================================
# The deadband
# ==========================================================================


def held_at_target(plan) -> dict[str, Decimal]:
    return {item.symbol: item.target_quantity for item in plan.allocations}


def test_price_drift_inside_the_deadband_is_not_an_order() -> None:
    held = held_at_target(frac_plan())
    prices = dict(TEST_PRICES)
    prices["SPY"] = Decimal("100.5")  # 0.5% drift < 1% of slot
    plan = plan_allocation(
        FRACTIONAL,
        active_symbols=U10,
        account_equity=Decimal("100000"),
        external_exposure_fraction=Decimal(0),
        reference_prices=prices,
        actual_quantities=held,
    )
    assert plan.ordering == ()


def test_price_drift_outside_the_deadband_is_an_order() -> None:
    held = held_at_target(frac_plan())
    prices = dict(TEST_PRICES)
    prices["SPY"] = Decimal("103")  # ~3% drift > 1% of slot
    plan = plan_allocation(
        FRACTIONAL,
        active_symbols=U10,
        account_equity=Decimal("100000"),
        external_exposure_fraction=Decimal(0),
        reference_prices=prices,
        actual_quantities=held,
    )
    moved = {item.symbol for item in plan.ordering}
    assert moved == {"SPY"}
    spy = next(item for item in plan.ordering)
    assert spy.side is OrderSide.SELL  # price up, quantity target down: trim


def test_the_dollar_floor_governs_a_small_account() -> None:
    """At $150 a slot is $13.50, so the $1 floor is the band that matters."""
    held = held_at_target(frac_plan(equity="150"))
    prices = dict(TEST_PRICES)
    prices["SPY"] = Decimal("105")  # 5% of a $13.50 slot is $0.675 < $1
    plan = plan_allocation(
        FRACTIONAL,
        active_symbols=U10,
        account_equity=Decimal("150"),
        external_exposure_fraction=Decimal(0),
        reference_prices=prices,
        actual_quantities=held,
    )
    assert plan.ordering == ()


def test_a_long_to_flat_transition_is_never_deadbanded() -> None:
    """CRITICAL. A full exit is exempt whatever its size."""
    plan = frac_plan(active=U10[1:], actual={"SPY": Decimal("0.003")})
    spy = next(item for item in plan.allocations if item.symbol == "SPY")
    assert spy.side is OrderSide.SELL
    assert spy.delta_quantity == Decimal("0.003")


def test_a_flat_to_long_entry_passes_the_deadband() -> None:
    plan = frac_plan(active=("SPY",))
    spy = next(item for item in plan.allocations if item.symbol == "SPY")
    assert spy.side is OrderSide.BUY
    assert spy.delta_quantity == spy.target_quantity


def test_crypto_growth_material_to_the_slot_trims_the_book() -> None:
    """External exposure up 5% of equity moves each slot by $500: an order."""
    held = held_at_target(frac_plan())
    plan = plan_allocation(
        FRACTIONAL,
        active_symbols=U10,
        account_equity=Decimal("100000"),
        external_exposure_fraction=Decimal("0.05"),
        reference_prices=TEST_PRICES,
        actual_quantities=held,
    )
    assert len(plan.ordering) == len(U10)
    assert {item.side for item in plan.ordering} == {OrderSide.SELL}


def test_crypto_noise_below_the_slot_band_is_suppressed() -> None:
    """External exposure up 0.05% of equity moves each slot by $5 < 1%: silence."""
    held = held_at_target(frac_plan())
    plan = plan_allocation(
        FRACTIONAL,
        active_symbols=U10,
        account_equity=Decimal("100000"),
        external_exposure_fraction=Decimal("0.0005"),
        reference_prices=TEST_PRICES,
        actual_quantities=held,
    )
    assert plan.ordering == ()


def test_crypto_shrinking_materially_rebuys_the_book() -> None:
    held = held_at_target(frac_plan(external="0.05"))
    plan = plan_allocation(
        FRACTIONAL,
        active_symbols=U10,
        account_equity=Decimal("100000"),
        external_exposure_fraction=Decimal(0),
        reference_prices=TEST_PRICES,
        actual_quantities=held,
    )
    assert len(plan.ordering) == len(U10)
    assert {item.side for item in plan.ordering} == {OrderSide.BUY}


def test_a_partial_fill_produces_a_smaller_follow_up_not_a_repeat() -> None:
    """The next delta is computed against what the broker actually holds."""
    target = held_at_target(frac_plan())["SPY"]
    partial = target / 2
    plan = frac_plan(actual={"SPY": partial})
    spy = next(item for item in plan.allocations if item.symbol == "SPY")
    assert spy.side is OrderSide.BUY
    assert spy.delta_quantity == target - partial


def test_identical_broker_state_reproduces_an_identical_silent_plan() -> None:
    """Restart idempotence at the arithmetic level: same inputs, no orders."""
    held = held_at_target(frac_plan())
    first = frac_plan(actual=held)
    second = frac_plan(actual=held)
    assert first.allocations == second.allocations
    assert first.ordering == () == second.ordering


def test_tuple_order_independence_for_the_fractional_policy() -> None:
    forward = frac_plan()
    backward = plan_allocation(
        FRACTIONAL,
        active_symbols=tuple(reversed(U10)),
        account_equity=Decimal("100000"),
        external_exposure_fraction=Decimal(0),
        reference_prices=dict(reversed(list(TEST_PRICES.items()))),
        actual_quantities={},
    )
    assert forward.allocations == backward.allocations


# ==========================================================================
# The hard caps, through the real Risk Engine
# ==========================================================================


def frac_context(
    *,
    equity: float = 100_000.0,
    cash: float = 100_000.0,
    total_exposure: float = 0.0,
    symbol_exposure: float = 0.0,
    position: Decimal = Decimal(0),
    daily_pnl: float = 0.0,
) -> RiskContext:
    return RiskContext(
        equity=equity,
        cash=cash,
        total_exposure=total_exposure,
        symbol_exposure=symbol_exposure,
        current_position_quantity=position,
        daily_pnl=daily_pnl,
        start_of_day_equity=equity,
        trading_enabled=True,
    )


def buy(quantity: str, price: float = 100.0) -> RiskRequest:
    return RiskRequest(
        symbol="SPY",
        side=RiskSide.BUY,
        reference_price=price,
        requested_quantity=Decimal(quantity),
    )


def test_a_buy_projecting_past_95_percent_account_wide_is_clamped_to_the_cap() -> None:
    """CRITICAL. The crypto book counts one for one in the projection."""
    limits = risk_policy_for(FRACTIONAL)
    # 90% already deployed across both books; 5% of headroom remains.
    decision = evaluate_risk(
        buy("100"), frac_context(total_exposure=90_000.0, cash=10_000.0), limits
    )
    assert decision.reason_code == TOTAL_EXPOSURE_LIMIT
    assert decision.approved_quantity * Decimal("100") <= Decimal("5000")


def test_a_buy_at_the_95_percent_cap_is_rejected_outright() -> None:
    limits = risk_policy_for(FRACTIONAL)
    decision = evaluate_risk(buy("1"), frac_context(total_exposure=95_000.0, cash=5_000.0), limits)
    assert decision.approved is False
    assert decision.reason_code == TOTAL_EXPOSURE_LIMIT


def test_a_symbol_past_11_percent_cannot_be_added_to() -> None:
    limits = risk_policy_for(FRACTIONAL)
    decision = evaluate_risk(
        buy("1"),
        frac_context(total_exposure=11_000.0, symbol_exposure=11_000.0, cash=89_000.0),
        limits,
    )
    assert decision.approved is False
    assert decision.reason_code == POSITION_LIMIT


def test_the_daily_loss_halt_still_engages_at_2_percent() -> None:
    """CRITICAL. Raising exposure did not loosen the halt."""
    limits = risk_policy_for(FRACTIONAL)
    decision = evaluate_risk(buy("1"), frac_context(daily_pnl=-2_000.0), limits)
    assert decision.approved is False
    assert decision.reason_code == DAILY_LOSS_LIMIT


def test_an_exit_is_still_never_blocked_by_the_halt() -> None:
    limits = risk_policy_for(FRACTIONAL)
    decision = evaluate_risk(
        RiskRequest(
            symbol="SPY",
            side=RiskSide.SELL,
            reference_price=100.0,
            requested_quantity=Decimal("0.5"),
        ),
        frac_context(
            total_exposure=95_000.0,
            symbol_exposure=11_000.0,
            position=Decimal("110"),
            daily_pnl=-5_000.0,
            cash=0.0,
        ),
        limits,
    )
    assert decision.approved is True
    assert decision.reason_code == APPROVED


def test_cash_is_the_no_leverage_gate() -> None:
    limits = risk_policy_for(FRACTIONAL)
    decision = evaluate_risk(buy("1000"), frac_context(cash=500.0), limits)
    assert decision.approved_quantity * Decimal("100") <= Decimal("500")


# ==========================================================================
# The fractional execution boundary
# ==========================================================================


def spec(fractionable: bool = True):
    from autotrader.execution.equity import EquityAssetSpec

    return EquityAssetSpec(
        symbol="SPY",
        asset_class="us_equity",
        status="active",
        tradable=True,
        fractionable=fractionable,
    )


def test_fractional_normalization_floors_to_the_increment() -> None:
    shares = normalize_fractional_share_quantity(
        Decimal("0.1234567891999"),
        spec(),
        reference_price=100.0,
        side=OrderSide.BUY,
        position_quantity=Decimal(0),
    )
    assert shares == Decimal("0.123456789")


def test_a_non_fractionable_asset_stops_a_fractional_order_naming_the_symbol() -> None:
    """CRITICAL. The mandatory gate: no silent whole-share fallback."""
    with pytest.raises(EquityAssetNotTradableError, match="SPY"):
        normalize_fractional_share_quantity(
            Decimal("1.5"),
            spec(fractionable=False),
            reference_price=100.0,
            side=OrderSide.BUY,
            position_quantity=Decimal(0),
        )


def test_a_sub_dollar_entry_is_refused_not_rounded_up() -> None:
    with pytest.raises(QuantityBelowMinimumError, match="minimum"):
        normalize_fractional_share_quantity(
            Decimal("0.005"),
            spec(),
            reference_price=100.0,
            side=OrderSide.BUY,
            position_quantity=Decimal(0),
        )
    assert Decimal("1") == MINIMUM_FRACTIONAL_ORDER_NOTIONAL


def test_a_full_exit_below_a_dollar_is_still_attempted() -> None:
    """A floor that trapped an open position would be a safety defect."""
    shares = normalize_fractional_share_quantity(
        Decimal("0.005"),
        spec(),
        reference_price=100.0,
        side=OrderSide.SELL,
        position_quantity=Decimal("0.005"),
    )
    assert shares == Decimal("0.005")


def test_a_partial_trim_below_a_dollar_is_refused() -> None:
    with pytest.raises(QuantityBelowMinimumError):
        normalize_fractional_share_quantity(
            Decimal("0.005"),
            spec(),
            reference_price=100.0,
            side=OrderSide.SELL,
            position_quantity=Decimal("2"),
        )


def test_the_fractional_order_request_is_market_day_quantity_only() -> None:
    """CRITICAL. Fractional qty, DAY, no notional: the forms are never mixed."""
    intent = OrderIntent(
        symbol="SPY",
        side=OrderSide.BUY,
        requested_quantity=Decimal("0.135"),
        approved_quantity=Decimal("0.135"),
        reference_price=100.0,
        risk_reason_code=APPROVED,
        created_at=T0,
    )
    request = build_fractional_equity_market_order_request(intent)
    assert request.qty == pytest.approx(0.135)
    assert Decimal(repr(request.qty)) <= Decimal("0.135")
    assert request.notional is None
    assert request.time_in_force is TimeInForce.DAY
    assert not request.extended_hours


def test_a_fractional_buy_flows_through_the_real_boundary(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """The full pipeline: risk under 11%/95%, fractional floor, submission."""
    client = FakeTradingClient()
    result = run_execution(
        connection,
        client,
        requested_quantity=Decimal("0.25"),
        fractional=True,
    )
    assert result.outcome is ExecutionOutcome.SUBMITTED
    assert len(client.submit_calls) == 1
    sent = client.submit_calls[0]
    assert sent.qty == pytest.approx(0.25)
    assert sent.notional is None


def test_a_fractional_sell_of_a_fractional_position_flows_through(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(
        positions=[make_position(qty="0.75", market_value="375")],
    )
    result = run_execution(
        connection,
        client,
        side="SELL",
        requested_quantity=Decimal("0.75"),
        fractional=True,
    )
    assert result.outcome is ExecutionOutcome.SUBMITTED
    assert client.submit_calls[0].qty == pytest.approx(0.75)


def test_a_non_fractionable_asset_stops_the_real_boundary(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(asset=make_asset(fractionable=False))
    with pytest.raises(EquityAssetNotTradableError, match="SPY"):
        run_execution(
            connection,
            client,
            requested_quantity=Decimal("0.25"),
            fractional=True,
        )
    assert client.submit_calls == []


def test_the_hard_caps_are_the_ones_enforced_when_the_policy_rides_along(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    """9% of equity in one symbol: the legacy 5% cap would clamp it; 11% allows it."""
    client = FakeTradingClient(account=make_account(equity="100000", cash="100000"))
    result = run_execution(
        connection,
        client,
        requested_quantity=Decimal("18"),  # 18 x $500 = $9,000 = 9%
        fractional=True,
        risk_policy=risk_policy_for(FRACTIONAL),
    )
    assert result.risk_decision.reason_code == APPROVED
    assert result.risk_decision.approved_quantity == Decimal("18")


def test_without_the_policy_the_legacy_caps_still_clamp(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(account=make_account(equity="100000", cash="100000"))
    result = run_execution(
        connection,
        client,
        requested_quantity=Decimal("18"),
    )
    assert result.risk_decision.reason_code == POSITION_LIMIT
    assert result.risk_decision.approved_quantity < Decimal("18")


def test_the_gateway_derives_fractional_and_risk_policy_from_the_one_policy_object(
    connection: sqlite3.Connection, enabled_gate: None
) -> None:
    client = FakeTradingClient(account=make_account(equity="100000", cash="100000"))
    gateway = AlpacaEquityPaperGateway(
        trading_client=client,
        data_client=FakeDataClient(500.0),
        policy=FRACTIONAL,
    )
    result = gateway.execute(
        connection,
        symbol="SPY",
        side=OrderSide.BUY,
        requested_quantity=Decimal("17.5"),
        now=T0,
        strategy_run_id=None,
    )
    # 17.5 x $500 = $8,750 = 8.75% - legacy caps would clamp to 5%.
    assert result.risk_decision.reason_code == APPROVED
    assert client.submit_calls[0].qty == pytest.approx(17.5)


# ==========================================================================
# The cross-store account lock
# ==========================================================================


def test_a_read_only_lock_contends_with_the_writable_one(tmp_path: Path) -> None:
    """CRITICAL. The peer's flock and ours exclude each other."""
    path = account_lock_path_for(tmp_path / "peer.db")
    writable = AccountExecutionLock(path)
    writable.acquire()  # creates the file, as the peer service does
    writable.release()

    reader = AccountExecutionLock(path, read_only=True, timeout_seconds=0.05)
    reader.acquire()
    try:
        contender = AccountExecutionLock(path, timeout_seconds=0.05)
        with pytest.raises(AccountExecutionLockError, match="still held"):
            contender.acquire()
    finally:
        reader.release()

    # And the other direction: a writable holder excludes the reader.
    writable.acquire()
    try:
        blocked = AccountExecutionLock(path, read_only=True, timeout_seconds=0.05)
        with pytest.raises(AccountExecutionLockError, match="still held"):
            blocked.acquire()
    finally:
        writable.release()


def test_a_missing_peer_lock_file_fails_closed(tmp_path: Path) -> None:
    reader = AccountExecutionLock(account_lock_path_for(tmp_path / "absent.db"), read_only=True)
    with pytest.raises(AccountExecutionLockError, match="could not be opened read-only"):
        reader.acquire()


def test_the_composite_lock_takes_both_and_releases_both(tmp_path: Path) -> None:
    peer_path = account_lock_path_for(tmp_path / "peer.db")
    creator = AccountExecutionLock(peer_path)
    creator.acquire()  # creates the file, as the peer service does
    creator.release()
    own_path = account_lock_path_for(tmp_path / "own.db")
    composite = CompositeAccountLock(
        (
            AccountExecutionLock(peer_path, read_only=True, timeout_seconds=0.05),
            AccountExecutionLock(own_path, timeout_seconds=0.05),
        )
    )
    with composite:
        assert composite.held
        for lock in composite.locks:
            assert lock.held
    assert not composite.held
    for lock in composite.locks:
        assert not lock.held


def test_a_failure_on_the_second_lock_releases_the_first(tmp_path: Path) -> None:
    """All-or-nothing: a refused section leaves nothing held."""
    peer_path = account_lock_path_for(tmp_path / "peer.db")
    creator = AccountExecutionLock(peer_path)
    creator.acquire()
    creator.release()
    missing = account_lock_path_for(tmp_path / "never-created.db")
    composite = CompositeAccountLock(
        (
            AccountExecutionLock(peer_path, read_only=True, timeout_seconds=0.05),
            AccountExecutionLock(missing, read_only=True, timeout_seconds=0.05),
        )
    )
    with pytest.raises(AccountExecutionLockError):
        composite.acquire()
    assert not composite.held
    assert not composite.locks[0].held


def test_the_composite_lock_is_reentrant(tmp_path: Path) -> None:
    own = account_lock_path_for(tmp_path / "own.db")
    composite = CompositeAccountLock((AccountExecutionLock(own),))
    with composite:
        with composite:
            assert composite.held
        assert composite.held
    assert not composite.held


# ==========================================================================
# The open-order guard, through a full runtime cycle
# ==========================================================================


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    database = tmp_path / "state.db"
    initialize_database(database)
    with connect(database) as open_connection:
        establish_account_safety(open_connection)
        yield open_connection


@pytest.fixture
def enabled_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOTRADER_PAPER_TRADING_ENABLED", "true")


def test_foreign_open_order_symbols_normalizes_and_deduplicates() -> None:
    records = (
        OpenOrderRecord(symbol="ETH/USD", side="buy", client_order_id="a"),
        OpenOrderRecord(symbol="ethusd", side="buy", client_order_id="b"),
        OpenOrderRecord(symbol="SPY", side="sell", client_order_id="c"),
    )
    assert foreign_open_order_symbols(records) == ("ETHUSD", "SPY")


def test_an_open_crypto_order_blocks_every_buy(connection: sqlite3.Connection) -> None:
    """CRITICAL. Exposure in flight from the other product stops new exposure here."""
    gateway = RecordingGateway()
    runtime = build_paper(
        connection,
        gateway=gateway,
        open_orders=lambda: (OpenOrderRecord(symbol="BTC/USD", side="buy", client_order_id="x"),),
    )
    runtime.start()
    report = runtime.run_cycle()
    runtime.stop()
    assert gateway.calls == []
    blocked = [
        outcome
        for outcome in report.outcomes
        if outcome.disposition is Disposition.EXTERNAL_ORDER_OPEN
    ]
    assert len(blocked) == len(EQUITY_SYMBOLS)


def test_an_open_order_on_one_equity_blocks_its_sell_but_not_other_exits(
    connection: sqlite3.Connection,
) -> None:
    """SELLs stay allowed while orders are in flight - except stacked ones."""
    broker = FakeBrokerState()
    # Both oversized far past their 3% targets, so both want a trimming SELL.
    broker.hold("SPY", Decimal(30), PRICES["SPY"])
    broker.hold("QQQ", Decimal(30), PRICES["QQQ"])
    gateway = RecordingGateway()
    runtime = build_paper(
        connection,
        gateway=gateway,
        broker=broker,
        open_orders=lambda: (OpenOrderRecord(symbol="SPY", side="sell", client_order_id="y"),),
    )
    runtime.start()
    report = runtime.run_cycle()
    runtime.stop()
    sold = {symbol for symbol, side, _ in gateway.calls if side is OrderSide.SELL}
    assert "QQQ" in sold  # an exit elsewhere is not blocked
    assert "SPY" not in sold  # a SELL is never stacked onto an open order
    bought = {symbol for symbol, side, _ in gateway.calls if side is OrderSide.BUY}
    assert bought == set()  # every BUY waits while anything is in flight
    spy = next(outcome for outcome in report.outcomes if outcome.symbol == "SPY")
    assert spy.disposition is Disposition.EXTERNAL_ORDER_OPEN


def test_an_unreadable_open_order_answer_blocks_everything(
    connection: sqlite3.Connection,
) -> None:
    def broken() -> tuple[OpenOrderRecord, ...]:
        raise ConnectionError("no answer")

    gateway = RecordingGateway()
    runtime = build_paper(connection, gateway=gateway, open_orders=broken)
    runtime.start()
    report = runtime.run_cycle()
    runtime.stop()
    assert gateway.calls == []
    assert all(
        outcome.disposition is Disposition.EXTERNAL_ORDER_OPEN
        for outcome in report.outcomes
        if outcome.bar_timestamp is not None
    )


def test_no_open_orders_means_the_cycle_proceeds_normally(
    connection: sqlite3.Connection,
) -> None:
    gateway = RecordingGateway()
    runtime = build_paper(connection, gateway=gateway, open_orders=lambda: ())
    runtime.start()
    runtime.run_cycle()
    runtime.stop()
    assert len(gateway.calls) == len(EQUITY_SYMBOLS)


def test_a_fractional_full_cycle_targets_ninety_percent(
    connection: sqlite3.Connection,
) -> None:
    """The runtime under the fractional policy plans 9% per symbol."""
    gateway = RecordingGateway()
    runtime = build_paper(connection, gateway=gateway, policy=FRACTIONAL)
    runtime.start()
    report = runtime.run_cycle()
    runtime.stop()
    assert report.plan is not None
    weights = {item.target_weight for item in report.plan.allocations}
    assert weights == {Decimal("0.090000000000")}
    assert len(gateway.calls) == len(EQUITY_SYMBOLS)
    for _, side, quantity in gateway.calls:
        assert side is OrderSide.BUY
        assert quantity == quantity.quantize(FRACTIONAL_SHARE_INCREMENT)


# ==========================================================================
# The legacy target-table shape - the first live finding of this migration
# ==========================================================================


def test_a_legacy_target_table_is_rebuilt_in_place_with_rows_preserved(
    tmp_path: Path,
) -> None:
    """CRITICAL. Found live on the first fractional cycle, 2026-09-02 13:49Z.

    A store from before the audit-key fix declares client_order_id NOT NULL,
    and CREATE IF NOT EXISTS never upgrades it - so the NULL-first target
    write, whose whole point is surviving a crash before an id exists, hit an
    IntegrityError before any broker call. The creator now rebuilds the legacy
    shape in place, keeping every audit row.
    """
    from autotrader.equity.paper import create_paper_target_table

    database = tmp_path / "legacy.db"
    initialize_database(database)
    legacy_create = """
        CREATE TABLE equity_paper_targets (
            id                 INTEGER PRIMARY KEY,
            client_order_id    TEXT NOT NULL UNIQUE CHECK (client_order_id <> ''),
            engine             TEXT NOT NULL CHECK (engine <> ''),
            environment        TEXT NOT NULL CHECK (environment = 'PAPER'),
            sizing_policy      TEXT NOT NULL CHECK (sizing_policy <> ''),
            sizing_config_hash TEXT NOT NULL CHECK (sizing_config_hash <> ''),
            rollout_stage      TEXT NOT NULL CHECK (rollout_stage <> ''),
            symbol             TEXT NOT NULL CHECK (symbol <> ''),
            side               TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
            target_weight      TEXT NOT NULL,
            target_notional    TEXT NOT NULL,
            target_quantity    TEXT NOT NULL,
            broker_quantity    TEXT NOT NULL,
            requested_delta    TEXT NOT NULL,
            approved_quantity  TEXT,
            risk_reason_code   TEXT,
            reference_price    TEXT NOT NULL,
            account_equity     TEXT NOT NULL,
            external_exposure  TEXT NOT NULL,
            budget_fraction    TEXT NOT NULL,
            bar_timestamp      TEXT NOT NULL,
            decided_at         TEXT NOT NULL
        )
    """
    audit_row = (
        "autotrader-legacy-1",
        "EDA1_RGP",
        "PAPER",
        "C_RESERVED_UNIVERSE",
        "c47288c2",
        "C",
        "SPY",
        "BUY",
        "0.03",
        "3000",
        "3",
        "0",
        "3",
        "3",
        "APPROVED",
        "765.80",
        "99854.92",
        "0.05",
        "0.25",
        "2026-08-31T17:00:00+00:00",
        "2026-08-31T17:20:28+00:00",
    )
    with connect(database) as connection:
        connection.execute(legacy_create)
        connection.execute(
            "INSERT INTO equity_paper_targets ("
            " client_order_id, engine, environment, sizing_policy,"
            " sizing_config_hash, rollout_stage, symbol, side, target_weight,"
            " target_notional, target_quantity, broker_quantity, requested_delta,"
            " approved_quantity, risk_reason_code, reference_price, account_equity,"
            " external_exposure, budget_fraction, bar_timestamp, decided_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            audit_row,
        )
        connection.commit()

        create_paper_target_table(connection)

        notnull = {
            row[1]: row[3] for row in connection.execute("PRAGMA table_info(equity_paper_targets)")
        }
        assert notnull["client_order_id"] == 0  # nullable, the current shape
        kept = [
            tuple(row)
            for row in connection.execute(
                "SELECT client_order_id, symbol, target_weight FROM equity_paper_targets"
            )
        ]
        assert kept == [("autotrader-legacy-1", "SPY", "0.03")]
        # The write that found the defect: a row with no key yet.
        connection.execute(
            "INSERT INTO equity_paper_targets ("
            " client_order_id, engine, environment, sizing_policy,"
            " sizing_config_hash, rollout_stage, symbol, side, target_weight,"
            " target_notional, target_quantity, broker_quantity, requested_delta,"
            " approved_quantity, risk_reason_code, reference_price, account_equity,"
            " external_exposure, budget_fraction, bar_timestamp, decided_at"
            ") VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            audit_row[1:],
        )
        # And a second modern store is untouched by the check. Idempotent.
        create_paper_target_table(connection)
        assert connection.execute("SELECT COUNT(*) FROM equity_paper_targets").fetchone()[0] == 2
