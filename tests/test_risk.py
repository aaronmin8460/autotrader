"""C5 tests: the deterministic risk engine.

Every test is offline, needs no credentials, and touches no file. Quantity
expectations are derived independently of the engine: `entry_headroom` restates
the three ceilings the way the specification words them ("resulting exposure <=
cap"), and the sizing assertions then check the two properties an answer must
have - it fits, and one more quantum would not. Neither repeats the engine's
own arithmetic, so the two agreeing is real evidence.

The crypto pivot changed the *representation* of a quantity, not the policy.
Quantities are fractional `decimal.Decimal` amounts with no whole-unit floor;
the caps are still 5% per symbol, 30% total, and a 2% daily-loss halt, and they
are still measured as USD notional.

The most important test in this file is
`test_kill_switch_blocks_entry_but_never_blocks_exit`. A kill switch that
prevented an account from reducing an existing position would trap it, which
is the opposite of a safety control.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import math
import socket
from decimal import Decimal

import pytest

from autotrader.risk import engine
from autotrader.risk.engine import (
    APPROVED,
    DAILY_LOSS_LIMIT,
    DEFAULT_POLICY,
    EXIT_QUANTITY_EXCEEDS_POSITION,
    INSUFFICIENT_CASH,
    INVALID_REQUEST,
    MAX_DAILY_LOSS_FRACTION,
    MAX_POSITION_FRACTION,
    MAX_TOTAL_EXPOSURE_FRACTION,
    NO_POSITION_TO_EXIT,
    POSITION_LIMIT,
    QUANTITY_EXPONENT,
    REASON_CODES,
    TOTAL_EXPOSURE_LIMIT,
    TRADING_DISABLED,
    RiskContext,
    RiskDecision,
    RiskInputError,
    RiskPolicy,
    RiskRequest,
    RiskSide,
    evaluate_risk,
)

EQUITY = 200_000.0
PRICE = 250.0

ZERO = Decimal(0)
ULP = QUANTITY_EXPONENT

#: A healthy, unconstrained account: flat, fully in cash, no loss, enabled.
BASE_CONTEXT = RiskContext(
    equity=EQUITY,
    cash=EQUITY,
    total_exposure=0.0,
    symbol_exposure=0.0,
    current_position_quantity=ZERO,
    daily_pnl=0.0,
    start_of_day_equity=EQUITY,
    trading_enabled=True,
)


def dec(value: object) -> Decimal:
    """A test-side exact Decimal, via the shortest round-tripping form."""
    return Decimal(str(float(value)))


def context(**changes: object) -> RiskContext:
    """`BASE_CONTEXT` with the named fields replaced."""
    return dataclasses.replace(BASE_CONTEXT, **changes)


def buy(quantity: object, price: float = PRICE, symbol: str = "BTC/USD") -> RiskRequest:
    """A BUY request for `quantity` units of the base asset."""
    amount = Decimal(quantity) if isinstance(quantity, (int, str)) else quantity
    return RiskRequest(
        symbol=symbol, side=RiskSide.BUY, reference_price=price, requested_quantity=amount
    )


def sell(quantity: object, price: float = PRICE, symbol: str = "BTC/USD") -> RiskRequest:
    """A SELL request for `quantity` units of the base asset."""
    amount = Decimal(quantity) if isinstance(quantity, (int, str)) else quantity
    return RiskRequest(
        symbol=symbol, side=RiskSide.SELL, reference_price=price, requested_quantity=amount
    )


def code_without_prose(source: str) -> str:
    """`source` with every docstring and comment removed.

    The source-level guarantees below are about executable code, and this
    module's documentation names the very things it forbids.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def entry_headroom(ctx: RiskContext, policy: RiskPolicy = DEFAULT_POLICY) -> Decimal:
    """The USD notional an entry may still use, restated from the policy.

    Deliberately not the engine's code path: this is the specification's own
    sentence - resulting symbol exposure within 5% of equity, resulting total
    exposure within 30%, and spend within cash - turned into the tightest of
    three dollar ceilings.
    """
    position_room = max(
        ZERO, dec(ctx.equity) * dec(policy.max_position_fraction) - dec(ctx.symbol_exposure)
    )
    portfolio_room = max(
        ZERO, dec(ctx.equity) * dec(policy.max_total_exposure_fraction) - dec(ctx.total_exposure)
    )
    return min(position_room, portfolio_room, dec(ctx.cash))


def assert_maximal_entry_quantity(
    decision: RiskDecision, ctx: RiskContext, price: float, policy: RiskPolicy = DEFAULT_POLICY
) -> None:
    """The two properties a sizing answer must have: it fits, and it is maximal."""
    headroom = entry_headroom(ctx, policy)
    quantity = decision.max_allowed_quantity
    assert quantity * dec(price) <= headroom, "an approved size must fit inside the cap"
    assert (quantity + ULP) * dec(price) > headroom, "sizing left a whole quantum unused"


# --------------------------------------------------------------------------
# Policy defaults
# --------------------------------------------------------------------------


def test_default_policy_is_exactly_five_thirty_two_percent() -> None:
    assert DEFAULT_POLICY.max_position_fraction == 0.05
    assert DEFAULT_POLICY.max_total_exposure_fraction == 0.30
    assert DEFAULT_POLICY.max_daily_loss_fraction == 0.02


def test_default_policy_stance_is_long_only_and_unlevered() -> None:
    assert DEFAULT_POLICY.long_only is True
    assert DEFAULT_POLICY.allow_leverage is False


def test_the_whole_share_policy_flag_is_gone() -> None:
    """Crypto is fractionable; a flag whose only legal value is False is not a policy."""
    fields = {field.name for field in dataclasses.fields(RiskPolicy)}
    assert "whole_shares_only" not in fields
    assert fields == {
        "max_position_fraction",
        "max_total_exposure_fraction",
        "max_daily_loss_fraction",
        "long_only",
        "allow_leverage",
    }
    with pytest.raises(TypeError):
        RiskPolicy(whole_shares_only=True)  # type: ignore[call-arg]


def test_module_constants_match_the_default_policy() -> None:
    assert DEFAULT_POLICY.max_position_fraction == MAX_POSITION_FRACTION
    assert DEFAULT_POLICY.max_total_exposure_fraction == MAX_TOTAL_EXPOSURE_FRACTION
    assert DEFAULT_POLICY.max_daily_loss_fraction == MAX_DAILY_LOSS_FRACTION


def test_evaluate_risk_defaults_to_the_default_policy() -> None:
    default = inspect.signature(evaluate_risk).parameters["policy"].default

    assert default is DEFAULT_POLICY


def test_policy_and_models_are_immutable() -> None:
    decision = evaluate_risk(buy(1), BASE_CONTEXT)
    for frozen in (DEFAULT_POLICY, BASE_CONTEXT, buy(1), decision):
        with pytest.raises(dataclasses.FrozenInstanceError):
            frozen.__setattr__("symbol", "ETH/USD")


def test_reason_codes_are_the_documented_set() -> None:
    assert set(REASON_CODES) == {
        "APPROVED",
        "INVALID_REQUEST",
        "TRADING_DISABLED",
        "DAILY_LOSS_LIMIT",
        "POSITION_LIMIT",
        "TOTAL_EXPOSURE_LIMIT",
        "INSUFFICIENT_CASH",
        "NO_POSITION_TO_EXIT",
        "EXIT_QUANTITY_EXCEEDS_POSITION",
    }
    assert len(REASON_CODES) == len(set(REASON_CODES))


@pytest.mark.parametrize(
    "unsupported",
    [
        RiskPolicy(long_only=False),
        RiskPolicy(allow_leverage=True),
        RiskPolicy(max_position_fraction=0.0),
        RiskPolicy(max_total_exposure_fraction=1.5),
        RiskPolicy(max_daily_loss_fraction=-0.02),
        RiskPolicy(max_position_fraction=math.nan),
    ],
)
def test_unsupported_policy_is_refused_not_partly_honoured(unsupported: RiskPolicy) -> None:
    with pytest.raises(RiskInputError):
        evaluate_risk(buy(1), BASE_CONTEXT, unsupported)


def test_a_supported_custom_policy_is_honoured() -> None:
    # 10% of $200,000 is $20,000, which is exactly 80 units at $250.
    decision = evaluate_risk(buy(500), BASE_CONTEXT, RiskPolicy(max_position_fraction=0.10))

    assert decision.approved
    assert decision.approved_quantity == Decimal(80)


# --------------------------------------------------------------------------
# Quantities are exact fractional Decimals
# --------------------------------------------------------------------------


def test_a_fractional_requested_quantity_is_accepted() -> None:
    decision = evaluate_risk(buy(Decimal("0.0001")), BASE_CONTEXT)

    assert decision.approved is True
    assert decision.approved_quantity == Decimal("0.0001")
    assert decision.reason_code == APPROVED


def test_an_approved_quantity_may_itself_be_fractional() -> None:
    """$10,000 of headroom against a $104,000 coin buys well under one unit."""
    decision = evaluate_risk(buy(Decimal(1), price=104_000.0), BASE_CONTEXT)

    assert decision.approved is True
    assert 0 < decision.max_allowed_quantity < 1
    assert_maximal_entry_quantity(decision, BASE_CONTEXT, 104_000.0)


def test_there_is_no_one_whole_unit_minimum() -> None:
    """The old engine rejected anything that could not afford one whole share."""
    decision = evaluate_risk(buy(Decimal("0.00000001"), price=104_000.0), BASE_CONTEXT)

    assert decision.approved is True
    assert decision.approved_quantity == Decimal("0.00000001")


def test_quantities_are_decimals_not_floats() -> None:
    decision = evaluate_risk(buy(Decimal("1.5")), BASE_CONTEXT)

    assert isinstance(decision.approved_quantity, Decimal)
    assert isinstance(decision.max_allowed_quantity, Decimal)


def test_an_exact_decimal_survives_evaluation_unrounded() -> None:
    """0.1 + 0.2 is 0.3 here. A float engine would have said 0.30000000000000004."""
    requested = Decimal("0.1") + Decimal("0.2")
    decision = evaluate_risk(buy(requested), BASE_CONTEXT)

    assert decision.approved_quantity == Decimal("0.3")
    assert str(decision.approved_quantity) == "0.3"


def test_an_integer_quantity_is_accepted_as_an_exact_value() -> None:
    decision = evaluate_risk(buy(3), BASE_CONTEXT)

    assert decision.approved_quantity == Decimal(3)
    assert isinstance(decision.approved_quantity, Decimal)


# --------------------------------------------------------------------------
# BUY: approval and sizing
# --------------------------------------------------------------------------


def test_normal_buy_is_approved_at_the_requested_quantity() -> None:
    decision = evaluate_risk(buy(10), BASE_CONTEXT)

    assert decision.approved is True
    assert decision.approved_quantity == Decimal(10)
    assert decision.reason_code == APPROVED
    # 5% of $200,000 is $10,000; at $250 that is 40 units of headroom.
    assert decision.max_allowed_quantity == Decimal(40)


def test_buy_larger_than_the_safe_maximum_is_clamped_not_rejected() -> None:
    decision = evaluate_risk(buy(1_000), BASE_CONTEXT)

    assert decision.approved is True
    assert decision.approved_quantity == Decimal(40)
    assert decision.max_allowed_quantity == Decimal(40)
    assert decision.reason_code == POSITION_LIMIT
    assert "sized down from 1000 to 40" in decision.message


def test_position_limit_constrains_quantity() -> None:
    # $7,500 already held leaves $2,500 of the $10,000 per-symbol cap.
    ctx = context(
        symbol_exposure=7_500.0, total_exposure=7_500.0, current_position_quantity=Decimal(30)
    )
    decision = evaluate_risk(buy(1_000), ctx)

    assert decision.approved_quantity == Decimal(10)
    assert decision.reason_code == POSITION_LIMIT
    assert_maximal_entry_quantity(decision, ctx, PRICE)


def test_total_exposure_limit_constrains_quantity() -> None:
    # $55,000 held elsewhere leaves $5,000 of the $60,000 portfolio cap, which
    # is tighter than the untouched $10,000 per-symbol cap.
    ctx = context(total_exposure=55_000.0, symbol_exposure=0.0, cash=145_000.0)
    decision = evaluate_risk(buy(1_000), ctx)

    assert decision.approved_quantity == Decimal(20)
    assert decision.reason_code == TOTAL_EXPOSURE_LIMIT
    assert_maximal_entry_quantity(decision, ctx, PRICE)


def test_cash_constrains_quantity() -> None:
    # $2,500 of cash is tighter than either exposure cap: no leverage.
    ctx = context(cash=2_500.0)
    decision = evaluate_risk(buy(1_000), ctx)

    assert decision.approved_quantity == Decimal(10)
    assert decision.reason_code == INSUFFICIENT_CASH
    assert_maximal_entry_quantity(decision, ctx, PRICE)


def test_tightest_constraint_wins() -> None:
    # Headroom: per-symbol $6,000 (24 units), portfolio $8,000 (32 units),
    # cash $4,000 (16 units). Cash is tightest.
    ctx = context(
        symbol_exposure=4_000.0,
        total_exposure=52_000.0,
        current_position_quantity=Decimal(16),
        cash=4_000.0,
    )
    decision = evaluate_risk(buy(1_000), ctx)

    assert decision.approved_quantity == Decimal(16)
    assert decision.reason_code == INSUFFICIENT_CASH
    assert_maximal_entry_quantity(decision, ctx, PRICE)


@pytest.mark.parametrize(
    ("changes", "expected_code"),
    [
        ({"symbol_exposure": 10_000.0, "total_exposure": 10_000.0}, POSITION_LIMIT),
        ({"total_exposure": 60_000.0, "symbol_exposure": 0.0}, TOTAL_EXPOSURE_LIMIT),
        ({"cash": 0.0}, INSUFFICIENT_CASH),
    ],
)
def test_zero_safe_quantity_rejects_the_buy(changes: dict, expected_code: str) -> None:
    """Only *no* headroom rejects now - not "less than one whole unit"."""
    ctx = context(**changes)
    decision = evaluate_risk(buy(10), ctx)

    assert decision.approved is False
    assert decision.approved_quantity == 0
    assert decision.max_allowed_quantity == 0
    assert decision.reason_code == expected_code
    assert entry_headroom(ctx) == 0


def test_headroom_smaller_than_one_unit_is_now_approved_not_rejected() -> None:
    """The archived engine rejected $249.99 of headroom at $250. Crypto does not."""
    ctx = context(cash=249.99)
    decision = evaluate_risk(buy(10), ctx)

    assert decision.approved is True
    assert 0 < decision.approved_quantity < 1
    assert decision.reason_code == INSUFFICIENT_CASH
    assert_maximal_entry_quantity(decision, ctx, PRICE)


def test_sizing_matches_the_independent_reference_across_a_grid() -> None:
    for symbol_exposure in (0.0, 1_000.0, 4_321.0, 9_999.0, 10_000.0):
        for extra_exposure in (0.0, 30_000.0, 49_999.0):
            for cash in (0.0, 137.0, 5_000.0, EQUITY):
                for price in (7.5, 33.33, 250.0, 1_234.56, 104_000.0):
                    ctx = context(
                        symbol_exposure=symbol_exposure,
                        total_exposure=symbol_exposure + extra_exposure,
                        cash=cash,
                    )
                    decision = evaluate_risk(buy(10_000, price=price), ctx)
                    headroom = entry_headroom(ctx)

                    assert_maximal_entry_quantity(decision, ctx, price)
                    assert decision.approved_quantity == decision.max_allowed_quantity
                    assert decision.approved is (headroom > 0)


def test_an_approved_buy_never_breaches_a_limit() -> None:
    for symbol_exposure in (0.0, 2_500.0, 9_000.0):
        for cash in (500.0, 9_000.0, EQUITY):
            for price in (7.77, 250.0, 999.0, 104_321.55):
                ctx = context(
                    symbol_exposure=symbol_exposure,
                    total_exposure=symbol_exposure + 20_000.0,
                    cash=cash,
                )
                decision = evaluate_risk(buy(10_000, price=price), ctx)
                notional = decision.approved_quantity * dec(price)

                assert dec(ctx.symbol_exposure) + notional <= dec(ctx.equity) * Decimal("0.05")
                assert dec(ctx.total_exposure) + notional <= dec(ctx.equity) * Decimal("0.30")
                assert notional <= dec(ctx.cash)


def test_the_approved_notional_never_exceeds_the_five_percent_cap_exactly() -> None:
    """The cap is a dollar ceiling: check the equality boundary, not an epsilon."""
    decision = evaluate_risk(buy(10_000, price=3.0), BASE_CONTEXT)
    cap = dec(EQUITY) * Decimal("0.05")

    assert decision.max_allowed_quantity * Decimal(3) <= cap
    # $10,000 / $3 does not divide exactly, so the answer is strictly under.
    assert decision.max_allowed_quantity == Decimal("3333.333333333333333333")


def test_an_exactly_dividing_cap_is_approved_to_the_penny() -> None:
    decision = evaluate_risk(buy(10_000, price=200.0), BASE_CONTEXT)

    assert decision.max_allowed_quantity == Decimal(50)
    assert decision.max_allowed_quantity * Decimal(200) == dec(EQUITY) * Decimal("0.05")


# --------------------------------------------------------------------------
# BUY: the kill switch and the UTC-day loss halt
# --------------------------------------------------------------------------


def test_trading_disabled_rejects_a_buy() -> None:
    decision = evaluate_risk(buy(1), context(trading_enabled=False))

    assert decision.approved is False
    assert decision.approved_quantity == 0
    assert decision.reason_code == TRADING_DISABLED


def test_exactly_minus_two_percent_daily_pnl_rejects_a_buy() -> None:
    """The boundary is inclusive, and it is exact in Decimal arithmetic."""
    ctx = context(daily_pnl=-0.02 * EQUITY, equity=EQUITY - 0.02 * EQUITY)

    assert dec(ctx.daily_pnl) / dec(ctx.start_of_day_equity) == Decimal("-0.02")
    decision = evaluate_risk(buy(1), ctx)

    assert decision.approved is False
    assert decision.reason_code == DAILY_LOSS_LIMIT


def test_worse_than_minus_two_percent_rejects_a_buy() -> None:
    ctx = context(daily_pnl=-9_000.0, equity=191_000.0)

    assert dec(ctx.daily_pnl) / dec(ctx.start_of_day_equity) < Decimal("-0.02")
    decision = evaluate_risk(buy(1), ctx)

    assert decision.approved is False
    assert decision.reason_code == DAILY_LOSS_LIMIT


def test_one_cent_better_than_the_halt_still_permits_an_entry() -> None:
    """The tightest passing case: -1,999,999 hundredths of a percent."""
    ctx = context(daily_pnl=-3_999.99, equity=196_000.01)

    assert Decimal("-0.02") < dec(ctx.daily_pnl) / dec(ctx.start_of_day_equity) < 0
    assert evaluate_risk(buy(1), ctx).approved is True


def test_better_than_minus_two_percent_does_not_trigger_the_halt() -> None:
    ctx = context(daily_pnl=-3_999.0, equity=196_001.0)

    assert -0.02 < ctx.daily_pnl / ctx.start_of_day_equity < 0
    decision = evaluate_risk(buy(1), ctx)

    assert decision.approved is True
    assert decision.reason_code == APPROVED


def test_a_profitable_day_never_triggers_the_halt() -> None:
    decision = evaluate_risk(buy(1), context(daily_pnl=25_000.0, equity=225_000.0))

    assert decision.approved is True


def test_the_daily_halt_is_measured_against_start_of_day_equity_only() -> None:
    """`start_of_day_equity` is the UTC-day baseline the caller supplies.

    Two contexts with identical current equity but different baselines must
    reach different decisions - which is what makes the baseline load-bearing.
    """
    common = {"equity": 196_000.0, "cash": 196_000.0}
    halted = context(**common, daily_pnl=-4_000.0, start_of_day_equity=200_000.0)
    fresh = context(**common, daily_pnl=0.0, start_of_day_equity=196_000.0)

    assert evaluate_risk(buy(1), halted).reason_code == DAILY_LOSS_LIMIT
    assert evaluate_risk(buy(1), fresh).approved is True


def test_the_engine_names_no_broker_equity_field() -> None:
    """`last_equity` is an equity-session previous close and has no place here.

    Scanned over executable code: the module's docstring explains *why* it does
    not use `last_equity`, so a raw substring scan would trip over that.
    """
    source = code_without_prose(inspect.getsource(engine))
    for forbidden in ("last_equity", "TradeAccount", "get_clock", "market_open"):
        assert forbidden not in source, forbidden


def test_the_engine_documents_the_risk_day_as_a_utc_calendar_day() -> None:
    assert "UTC" in (engine.__doc__ or "")
    assert "UTC" in (RiskContext.__doc__ or "")


def test_the_kill_switch_is_reported_ahead_of_the_daily_loss_halt() -> None:
    # Both gates are breached; the reported code is deterministic.
    ctx = context(trading_enabled=False, daily_pnl=-50_000.0, equity=150_000.0)

    assert evaluate_risk(buy(1), ctx).reason_code == TRADING_DISABLED


def test_a_malformed_request_is_named_ahead_of_any_risk_gate() -> None:
    ctx = context(trading_enabled=False, daily_pnl=-50_000.0, equity=150_000.0)

    assert evaluate_risk(buy(0), ctx).reason_code == INVALID_REQUEST


# --------------------------------------------------------------------------
# The boundary that matters: a halt must not trap an open position
# --------------------------------------------------------------------------


def test_kill_switch_blocks_entry_but_never_blocks_exit() -> None:
    """A halted account must still be able to reduce risk.

    This is the regression test for the whole design: risk controls exist to
    stop the account adding exposure, and a control that also prevented it
    from *removing* exposure would trap an open position.
    """
    halted = context(
        trading_enabled=False,
        symbol_exposure=5_000.0,
        total_exposure=5_000.0,
        current_position_quantity=Decimal("20.5"),
    )

    entry = evaluate_risk(buy(1), halted)
    assert entry.approved is False
    assert entry.reason_code == TRADING_DISABLED

    exit_decision = evaluate_risk(sell(Decimal("20.5")), halted)
    assert exit_decision.approved is True
    assert exit_decision.approved_quantity == Decimal("20.5")
    assert exit_decision.reason_code == APPROVED


def test_exit_is_allowed_after_a_daily_loss_halt() -> None:
    halted = context(
        daily_pnl=-40_000.0,
        equity=160_000.0,
        symbol_exposure=5_000.0,
        total_exposure=5_000.0,
        current_position_quantity=Decimal(20),
    )

    assert evaluate_risk(buy(1), halted).reason_code == DAILY_LOSS_LIMIT

    exit_decision = evaluate_risk(sell(20), halted)
    assert exit_decision.approved is True
    assert exit_decision.approved_quantity == Decimal(20)


def test_exit_is_allowed_while_every_entry_gate_is_breached_at_once() -> None:
    trapped = context(
        trading_enabled=False,
        daily_pnl=-40_000.0,
        equity=160_000.0,
        cash=0.0,
        symbol_exposure=50_000.0,
        total_exposure=160_000.0,
        current_position_quantity=Decimal("200.000001"),
    )

    assert evaluate_risk(buy(1), trapped).approved is False

    exit_decision = evaluate_risk(sell(Decimal("200.000001")), trapped)
    assert exit_decision.approved is True
    assert exit_decision.approved_quantity == Decimal("200.000001")


# --------------------------------------------------------------------------
# SELL: exits reduce risk and can never open a short
# --------------------------------------------------------------------------


def test_sell_with_no_position_is_rejected() -> None:
    decision = evaluate_risk(sell(10), BASE_CONTEXT)

    assert decision.approved is False
    assert decision.approved_quantity == 0
    assert decision.max_allowed_quantity == 0
    assert decision.reason_code == NO_POSITION_TO_EXIT


def test_sell_larger_than_the_position_clamps_to_the_full_position() -> None:
    held = context(
        symbol_exposure=5_000.0, total_exposure=5_000.0, current_position_quantity=Decimal("20.25")
    )
    decision = evaluate_risk(sell(500), held)

    assert decision.approved is True
    assert decision.approved_quantity == Decimal("20.25")
    assert decision.max_allowed_quantity == Decimal("20.25")
    assert decision.reason_code == EXIT_QUANTITY_EXCEEDS_POSITION


def test_a_partial_fractional_exit_is_approved_as_requested() -> None:
    held = context(
        symbol_exposure=5_000.0, total_exposure=5_000.0, current_position_quantity=Decimal(20)
    )
    decision = evaluate_risk(sell(Decimal("7.125")), held)

    assert decision.approved is True
    assert decision.approved_quantity == Decimal("7.125")
    assert decision.reason_code == APPROVED


def test_an_approved_sell_can_never_create_a_short() -> None:
    for position in (ZERO, Decimal("0.00000001"), Decimal("0.5"), Decimal(20), Decimal("39.9")):
        held = context(
            symbol_exposure=float(position) * PRICE,
            total_exposure=float(position) * PRICE,
            current_position_quantity=position,
        )
        for requested in (
            Decimal("0.00000001"),
            position,
            position + ULP,
            Decimal(10_000),
        ):
            if requested <= 0:
                continue
            decision = evaluate_risk(sell(requested), held)

            assert decision.approved_quantity <= position
            assert position - decision.approved_quantity >= 0
            if position == 0:
                assert decision.approved is False


def test_exit_limits_are_not_applied_to_a_position_far_above_the_caps() -> None:
    # A position well beyond every cap - exactly the state that most needs to
    # be reducible - is still fully exitable.
    oversized = context(
        cash=0.0,
        symbol_exposure=150_000.0,
        total_exposure=180_000.0,
        current_position_quantity=Decimal(600),
    )
    decision = evaluate_risk(sell(600), oversized)

    assert decision.approved is True
    assert decision.approved_quantity == Decimal(600)


# --------------------------------------------------------------------------
# Malformed requests: rejected decisions, never exceptions
# --------------------------------------------------------------------------


@pytest.mark.parametrize("side", [RiskSide.BUY, RiskSide.SELL])
@pytest.mark.parametrize(
    "quantity",
    [
        Decimal(0),
        Decimal(-1),
        Decimal("-0.5"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        0,
        -100,
        "10",
        None,
        True,
    ],
    ids=[
        "zero",
        "minus-one",
        "negative-fraction",
        "nan",
        "inf",
        "-inf",
        "int-zero",
        "negative-int",
        "string",
        "none",
        "bool",
    ],
)
def test_an_unusable_quantity_is_rejected(side: RiskSide, quantity: object) -> None:
    held = context(
        symbol_exposure=5_000.0, total_exposure=5_000.0, current_position_quantity=Decimal(20)
    )
    request = RiskRequest(
        symbol="BTC/USD", side=side, reference_price=PRICE, requested_quantity=quantity
    )

    decision = evaluate_risk(request, held)

    assert decision.approved is False
    assert decision.approved_quantity == 0
    assert decision.reason_code == INVALID_REQUEST


@pytest.mark.parametrize("side", [RiskSide.BUY, RiskSide.SELL])
@pytest.mark.parametrize("quantity", [0.5, 1.0, 10.5], ids=["half", "integral", "fraction"])
def test_a_float_quantity_is_refused_rather_than_converted(side: RiskSide, quantity: float) -> None:
    """A binary float is an approximation, and an approximation is not a size.

    This is the crypto-era replacement for the old "fractional shares are out
    of scope" rule: the objection is now the *type*, not the fraction.
    """
    held = context(
        symbol_exposure=5_000.0, total_exposure=5_000.0, current_position_quantity=Decimal(20)
    )
    request = RiskRequest(
        symbol="BTC/USD", side=side, reference_price=PRICE, requested_quantity=quantity
    )

    decision = evaluate_risk(request, held)

    assert decision.approved is False
    assert decision.reason_code == INVALID_REQUEST
    assert "Decimal" in decision.message


@pytest.mark.parametrize("side", [RiskSide.BUY, RiskSide.SELL])
@pytest.mark.parametrize(
    "price",
    [0.0, -1.0, -250.0, math.nan, math.inf, -math.inf, None, "250"],
    ids=["zero", "minus-one", "negative", "nan", "inf", "-inf", "none", "string"],
)
def test_an_unusable_reference_price_is_rejected(side: RiskSide, price: object) -> None:
    held = context(
        symbol_exposure=5_000.0, total_exposure=5_000.0, current_position_quantity=Decimal(20)
    )
    request = RiskRequest(
        symbol="BTC/USD", side=side, reference_price=price, requested_quantity=Decimal(5)
    )

    decision = evaluate_risk(request, held)

    assert decision.approved is False
    assert decision.reason_code == INVALID_REQUEST


@pytest.mark.parametrize("symbol", ["", "   ", None, 5])
def test_an_unusable_symbol_is_rejected(symbol: object) -> None:
    request = RiskRequest(
        symbol=symbol, side=RiskSide.BUY, reference_price=PRICE, requested_quantity=Decimal(1)
    )

    assert evaluate_risk(request, BASE_CONTEXT).reason_code == INVALID_REQUEST


def test_a_side_that_is_not_a_risk_side_is_rejected() -> None:
    request = RiskRequest(
        symbol="BTC/USD", side="BUY", reference_price=PRICE, requested_quantity=Decimal(1)
    )

    assert evaluate_risk(request, BASE_CONTEXT).reason_code == INVALID_REQUEST


def test_risk_side_has_no_short() -> None:
    assert {member.value for member in RiskSide} == {"BUY", "SELL"}


# --------------------------------------------------------------------------
# Malformed contexts: controlled exceptions, never a silent repair
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"equity": 0.0},
        {"equity": -1.0},
        {"equity": math.nan},
        {"equity": math.inf},
        {"cash": -0.01},
        {"cash": math.nan},
        {"total_exposure": -1.0},
        {"symbol_exposure": -1.0},
        {"symbol_exposure": math.inf},
        {"symbol_exposure": 10.0, "total_exposure": 5.0},
        {"current_position_quantity": Decimal(-1)},
        {"current_position_quantity": Decimal("-0.5")},
        {"current_position_quantity": Decimal("NaN")},
        {"current_position_quantity": Decimal("Infinity")},
        {"current_position_quantity": 2.5},
        {"current_position_quantity": "5"},
        {"start_of_day_equity": 0.0},
        {"start_of_day_equity": -100.0},
        {"start_of_day_equity": math.nan},
        {"daily_pnl": math.nan},
        {"daily_pnl": -math.inf},
        {"trading_enabled": 1},
        {"trading_enabled": None},
    ],
)
def test_an_impossible_context_raises_rather_than_being_repaired(changes: dict) -> None:
    with pytest.raises(RiskInputError):
        evaluate_risk(buy(1), context(**changes))


def test_a_zero_position_context_is_perfectly_valid() -> None:
    assert evaluate_risk(buy(1), context(current_position_quantity=ZERO)).approved is True


def test_symbol_exposure_equal_to_total_exposure_is_accepted() -> None:
    ctx = context(
        symbol_exposure=5_000.0, total_exposure=5_000.0, current_position_quantity=Decimal(20)
    )

    assert evaluate_risk(buy(1), ctx).approved is True


def test_an_ordinary_risk_denial_never_raises() -> None:
    denied = (
        (buy(1), context(trading_enabled=False)),
        (buy(1), context(daily_pnl=-100_000.0, equity=100_000.0)),
        (buy(1), context(cash=0.0)),
        (sell(1), BASE_CONTEXT),
        (buy(0), BASE_CONTEXT),
    )
    for request, ctx in denied:
        decision = evaluate_risk(request, ctx)

        assert isinstance(decision, RiskDecision)
        assert decision.approved is False


# --------------------------------------------------------------------------
# Decision invariants
# --------------------------------------------------------------------------

HELD = context(
    symbol_exposure=5_000.0, total_exposure=5_000.0, current_position_quantity=Decimal("20.5")
)

ALL_CASES = (
    (buy(10), BASE_CONTEXT),
    (buy(10_000), BASE_CONTEXT),
    (buy(Decimal("0.00025")), BASE_CONTEXT),
    (buy(1), context(trading_enabled=False)),
    (buy(1), context(daily_pnl=-4_000.0, equity=196_000.0)),
    (buy(1), context(cash=0.0)),
    (buy(0), BASE_CONTEXT),
    (buy(5), context(symbol_exposure=9_900.0, total_exposure=9_900.0)),
    (sell(Decimal("5.25")), HELD),
    (sell(500), HELD),
    (sell(5), BASE_CONTEXT),
)


def test_decision_invariants_hold_for_every_case() -> None:
    for request, ctx in ALL_CASES:
        decision = evaluate_risk(request, ctx)

        assert decision.reason_code in REASON_CODES
        assert decision.message
        assert decision.approved_quantity >= 0
        assert decision.max_allowed_quantity >= 0
        assert decision.approved_quantity <= decision.max_allowed_quantity
        if not decision.approved:
            assert decision.approved_quantity == 0
        else:
            assert decision.approved_quantity > 0
            assert decision.approved_quantity <= request.requested_quantity
        if decision.reason_code == APPROVED:
            assert decision.approved is True
            assert decision.approved_quantity == request.requested_quantity


def test_repeated_evaluation_is_deterministic() -> None:
    for request, ctx in ALL_CASES:
        decisions = [evaluate_risk(request, ctx) for _ in range(5)]

        assert all(decision == decisions[0] for decision in decisions)
        assert all(
            str(decision.approved_quantity) == str(decisions[0].approved_quantity)
            for decision in decisions
        )


def test_evaluation_does_not_mutate_the_request_or_the_context() -> None:
    for request, ctx in ALL_CASES:
        request_before = dataclasses.asdict(request)
        context_before = dataclasses.asdict(ctx)

        evaluate_risk(request, ctx)

        assert dataclasses.asdict(request) == request_before
        assert dataclasses.asdict(ctx) == context_before


def test_evaluation_does_not_mutate_the_policy() -> None:
    before = dataclasses.asdict(DEFAULT_POLICY)

    for request, ctx in ALL_CASES:
        evaluate_risk(request, ctx)

    assert dataclasses.asdict(DEFAULT_POLICY) == before


def test_the_decimal_context_is_not_altered_globally() -> None:
    import decimal

    before = decimal.getcontext().prec
    for request, ctx in ALL_CASES:
        evaluate_risk(request, ctx)
    assert decimal.getcontext().prec == before


# --------------------------------------------------------------------------
# Boundary safety: no broker, no persistence, no network
# --------------------------------------------------------------------------


def test_risk_engine_imports_only_the_standard_library_it_needs() -> None:
    imported = {
        getattr(value, "__name__", "") for value in vars(engine).values() if inspect.ismodule(value)
    }

    assert imported == {"math"}


def test_risk_engine_imports_no_broker_client() -> None:
    source = inspect.getsource(engine).lower()

    assert "alpaca" not in source
    assert "tradingclient" not in source
    assert "submit_order" not in source


def test_risk_engine_touches_no_database() -> None:
    source = inspect.getsource(engine).lower()

    for token in ("sqlite3", "cursor", "commit()", "create table"):
        assert token not in source, f"{token!r} must not appear in the risk engine"


def test_risk_engine_writes_nothing() -> None:
    source = inspect.getsource(engine)

    for token in ("open(", "Path(", "to_parquet", "json.dump", "write_text"):
        assert token not in source, f"{token!r} must not appear in the risk engine"


def test_risk_engine_needs_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    assert evaluate_risk(buy(10), BASE_CONTEXT).approved is True


def test_risk_engine_makes_no_network_access(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("the risk engine must not open a socket")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

    for request, ctx in ALL_CASES:
        evaluate_risk(request, ctx)


def test_risk_engine_creates_no_files(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    for request, ctx in ALL_CASES:
        evaluate_risk(request, ctx)

    assert list(tmp_path.iterdir()) == []


def test_backtesting_does_not_depend_on_the_risk_engine() -> None:
    """C4 keeps its own all-cash sizing baseline, so its results are unchanged."""
    from autotrader.backtest import engine as backtest_engine

    assert "autotrader.risk" not in inspect.getsource(backtest_engine)
    assert "evaluate_risk" not in inspect.getsource(backtest_engine)
