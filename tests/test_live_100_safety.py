"""The $100 real-money validation: every safety claim, asserted mechanically.

This file is the evidence behind the readiness report. Each section states a
promise the operator was given and then proves it with arithmetic rather than
with prose:

*The dollar ceilings do not scale.* Ten LONG symbols size to $90 of gross at
$100 of equity and to $90 at $1,000 of equity. A percentage-only policy sizes
the second case at $900, and that contrast is asserted directly, because "the
ceiling holds" is only meaningful next to what would have happened without it.

*The ceilings tighten, never widen.* An absolute ceiling enters the risk
arithmetic through a `min`. A policy carrying one is bound by whichever of the
two is smaller at that moment, so a small account is still bound by its
percentages and a grown one is bound by its dollars.

*The switch is off by default and stays off across a restart.* A fresh store,
an unreadable row, an unrecognised value and a missing environment gate all
answer DISARMED, and the answer survives a new connection to the same file.

*The account pin refuses everything it was not pointed at.* No pin, a
malformed pin, an unreadable account, an account with no number, and a
different account all stop the runtime before it fetches anything.

*The ceilings follow the balance.* A partly funded account is bound by its
percentages - $45 target and $47.50 hard at $50 of settled equity - and reaches
the authorized $90/$95/$100 only once the broker reports the money. Nothing is
ever sized against a deposit that has not settled.

*A deposit is not a profit.* A non-trade cash movement on the risk day stops
new entries, because the daily-loss halt would otherwise be measuring the
transfer instead of the trading.

*Nothing here is a second strategy.* At the authorized capital the validation
policy and the paper champion produce identical targets, to the cent, and
every policy the sizing study froze still digests to the hash it published.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from autotrader.equity import EQUITY_SYMBOLS
from autotrader.equity.allocation import (
    HARD_ACCOUNT_GROSS_CAP,
    HARD_SYMBOL_GROSS_CAP,
    LIVE_VALIDATION_CAPITAL_BOUND,
    LIVE_VALIDATION_HARD_GROSS_NOTIONAL,
    LIVE_VALIDATION_SYMBOL_NOTIONAL,
    LIVE_VALIDATION_TARGET_GROSS_NOTIONAL,
    POLICY_FRACTIONAL_RESERVED_90,
    POLICY_LIVE_VALIDATION_100,
    TARGET_ACCOUNT_GROSS,
    AllocationError,
    AllocationPolicy,
    allocation_policy_for,
    plan_allocation,
    risk_policy_for,
)
from autotrader.execution.models import OrderSide
from autotrader.live.armstate import (
    ARM_CONFIRMATION_TOKEN,
    LIVE_ARMED_ENV,
    STATE_ARMED,
    STATE_DISARMED,
    LiveDisarmedError,
    arm_live_trading,
    create_arm_state_table,
    disarm_live_trading,
    environment_arm_gate_open,
    read_arm_state,
    require_armed,
)
from autotrader.live.budget import (
    CashFlowEvent,
    LiveBudgetError,
    LiveExposureCeilings,
    cash_flow_block_reason,
    effective_ceilings,
)
from autotrader.live.identity import (
    LIVE_ACCOUNT_FINGERPRINT_ENV,
    AccountIdentityError,
    account_fingerprint,
    require_expected_account,
)
from autotrader.risk import (
    DEFAULT_POLICY,
    RiskContext,
    RiskInputError,
    RiskPolicy,
    RiskRequest,
    RiskSide,
    evaluate_risk,
)
from autotrader.state.sqlite import connect, initialize_database

NOW = datetime(2026, 9, 8, 13, 45, tzinfo=UTC)

#: The four hashes the sizing study and the fractional migration published.
#: Adding a fifth policy must not move any of them.
FROZEN_POLICY_HASHES = {
    "A_EQUAL_ACTIVE": "ef22cfb15525405a334b3363d9ae847500fa3a674ab52dc1d3d52c90bb75673c",
    "B_FIXED_PRO_RATA": "23f4272a60c8ed0fed22f4d24167cb014a9b15cb07da88ab57013a43154a4d80",
    "C_RESERVED_UNIVERSE": "c47288c2aafd84262a1257b783614efead995735027c535aa36d23b2dd9f5277",
    "EDA1_FRACTIONAL_RESERVED_90": (
        "e081e1f6bad9fb8eb35ea0b2671b99f8ddbf083063c05c2932f90e4d3eb380f3"
    ),
}


@pytest.fixture()
def store(tmp_path: Path) -> sqlite3.Connection:
    database = tmp_path / "live.db"
    initialize_database(database)
    with connect(database) as connection:
        yield connection


def plan_at(policy: AllocationPolicy, equity: str, *, price: str = "100", active=None):
    """The whole-book plan at one equity, every symbol priced the same."""
    symbols = set(EQUITY_SYMBOLS) if active is None else set(active)
    return plan_allocation(
        policy,
        active_symbols=symbols,
        account_equity=Decimal(equity),
        external_exposure_fraction=Decimal(0),
        reference_prices={symbol: Decimal(price) for symbol in EQUITY_SYMBOLS},
        actual_quantities={},
    )


def gross_of(plan) -> Decimal:
    return sum(
        (item.target_quantity * item.reference_price for item in plan.allocations), Decimal(0)
    )


# ==========================================================================
# The dollar ceilings
# ==========================================================================


def test_the_target_gross_does_not_grow_when_the_account_does() -> None:
    """CRITICAL. Profit must not silently authorize a larger position."""
    policy = allocation_policy_for(POLICY_LIVE_VALIDATION_100)
    for equity in ("100", "120", "150", "200", "1000", "100000"):
        assert gross_of(plan_at(policy, equity)) == LIVE_VALIDATION_TARGET_GROSS_NOTIONAL


def test_the_percentage_only_policy_would_have_scaled() -> None:
    """The contrast that makes the assertion above mean something."""
    percentage_only = allocation_policy_for(POLICY_FRACTIONAL_RESERVED_90)
    assert gross_of(plan_at(percentage_only, "1000")) == Decimal(900)


def test_a_smaller_account_is_still_bound_by_the_percentage() -> None:
    """The ceilings tighten. Below the authorized capital the fraction binds."""
    policy = allocation_policy_for(POLICY_LIVE_VALIDATION_100)
    assert gross_of(plan_at(policy, "50")) == Decimal(45)


def test_no_single_symbol_is_assigned_more_than_the_symbol_ceiling() -> None:
    policy = allocation_policy_for(POLICY_LIVE_VALIDATION_100)
    for equity in ("100", "1000", "100000"):
        plan = plan_at(policy, equity)
        for item in plan.allocations:
            assert item.target_quantity * item.reference_price <= LIVE_VALIDATION_SYMBOL_NOTIONAL


def test_one_lonely_long_symbol_still_gets_only_its_own_slot() -> None:
    """CRITICAL. A flat symbol's slot is never redistributed to an active one.

    The arithmetic trap of a small account: with nine symbols flat it would be
    easy to write an allocator that handed the whole $90 to the tenth. This one
    hands it $9 and leaves $81 in cash.
    """
    policy = allocation_policy_for(POLICY_LIVE_VALIDATION_100)
    plan = plan_at(policy, "100", active=("SPY",))
    assert gross_of(plan) == Decimal(9)


def test_the_ceilings_are_ordered_outward_from_slot_to_authorization() -> None:
    assert (
        LIVE_VALIDATION_TARGET_GROSS_NOTIONAL
        <= LIVE_VALIDATION_HARD_GROSS_NOTIONAL
        <= LIVE_VALIDATION_CAPITAL_BOUND
    )
    assert LIVE_VALIDATION_SYMBOL_NOTIONAL <= LIVE_VALIDATION_HARD_GROSS_NOTIONAL


def test_a_hard_ceiling_above_the_authorization_cannot_be_constructed() -> None:
    """CRITICAL. Widening the mandate has to fail loudly, not pass review."""
    with pytest.raises(AllocationError, match="authorized capital_bound"):
        AllocationPolicy(
            policy_id=POLICY_LIVE_VALIDATION_100,
            per_symbol_cap=HARD_SYMBOL_GROSS_CAP,
            total_cap=HARD_ACCOUNT_GROSS_CAP,
            target_gross=TARGET_ACCOUNT_GROSS,
            fractional=True,
            deadband_min_notional=Decimal(1),
            target_gross_notional=Decimal(90),
            hard_gross_notional=Decimal(500),
            per_symbol_notional=Decimal(11),
            capital_bound=Decimal(100),
        )


def test_the_named_policy_cannot_be_built_without_its_dollar_ceilings() -> None:
    with pytest.raises(AllocationError, match="frozen parameter set"):
        AllocationPolicy(policy_id=POLICY_LIVE_VALIDATION_100)


# ==========================================================================
# The risk engine's absolute ceilings
# ==========================================================================


def context(*, equity: str, total: str, symbol: str, cash: str) -> RiskContext:
    return RiskContext(
        equity=float(equity),
        cash=float(cash),
        total_exposure=float(total),
        symbol_exposure=float(symbol),
        current_position_quantity=Decimal(0),
        daily_pnl=0.0,
        start_of_day_equity=float(equity),
        trading_enabled=True,
    )


def buy(symbol: str = "SPY", *, price: str = "10", quantity: str = "1000") -> RiskRequest:
    return RiskRequest(
        symbol=symbol,
        side=RiskSide.BUY,
        reference_price=float(price),
        requested_quantity=Decimal(quantity),
    )


def test_the_absolute_total_ceiling_binds_on_a_grown_account() -> None:
    """CRITICAL. $95 of gross, whatever the percentage would have allowed."""
    policy = risk_policy_for(allocation_policy_for(POLICY_LIVE_VALIDATION_100))
    decision = evaluate_risk(
        buy(), context(equity="1000", total="0", symbol="0", cash="1000"), policy
    )
    assert decision.approved
    assert decision.approved_quantity * Decimal(10) <= LIVE_VALIDATION_HARD_GROSS_NOTIONAL


def test_the_absolute_symbol_ceiling_binds_on_a_grown_account() -> None:
    policy = risk_policy_for(allocation_policy_for(POLICY_LIVE_VALIDATION_100))
    decision = evaluate_risk(
        buy(), context(equity="1000", total="0", symbol="0", cash="1000"), policy
    )
    assert decision.approved_quantity * Decimal(10) <= LIVE_VALIDATION_SYMBOL_NOTIONAL


def test_pending_exposure_already_counted_leaves_no_headroom() -> None:
    """CRITICAL. Risk measures against exposure, not against filled positions.

    The caller's job is to hand in a `total_exposure` that already includes
    open-order potential; what this proves is that once it does, the ceiling
    genuinely refuses rather than sizing over it.
    """
    policy = risk_policy_for(allocation_policy_for(POLICY_LIVE_VALIDATION_100))
    decision = evaluate_risk(
        buy(), context(equity="1000", total="95", symbol="0", cash="900"), policy
    )
    assert not decision.approved
    assert decision.approved_quantity == Decimal(0)


def test_an_absolute_ceiling_can_only_tighten_never_widen() -> None:
    """A $10,000 ceiling on a $100 account changes nothing: 95% still binds."""
    generous = RiskPolicy(
        max_position_fraction=0.11,
        max_total_exposure_fraction=0.95,
        max_position_notional=Decimal(10_000),
        max_total_notional=Decimal(10_000),
    )
    plain = RiskPolicy(max_position_fraction=0.11, max_total_exposure_fraction=0.95)
    account = context(equity="100", total="0", symbol="0", cash="100")
    assert (
        evaluate_risk(buy(price="1"), account, generous).approved_quantity
        == evaluate_risk(buy(price="1"), account, plain).approved_quantity
    )


def test_the_daily_loss_halt_is_not_relaxed_for_real_money() -> None:
    """CRITICAL. 2%, unchanged, and no caller in this repository varies it."""
    policy = risk_policy_for(allocation_policy_for(POLICY_LIVE_VALIDATION_100))
    assert policy.max_daily_loss_fraction == DEFAULT_POLICY.max_daily_loss_fraction == 0.02
    halted = RiskContext(
        equity=98.0,
        cash=98.0,
        total_exposure=0.0,
        symbol_exposure=0.0,
        current_position_quantity=Decimal(0),
        daily_pnl=-2.0,
        start_of_day_equity=100.0,
        trading_enabled=True,
    )
    assert evaluate_risk(buy(price="1"), halted, policy).reason_code == "DAILY_LOSS_LIMIT"


def test_an_exit_is_never_blocked_by_the_dollar_ceilings() -> None:
    """CRITICAL. A ceiling that trapped a position would be a defect."""
    policy = risk_policy_for(allocation_policy_for(POLICY_LIVE_VALIDATION_100))
    holding = RiskContext(
        equity=98.0,
        cash=1.0,
        total_exposure=97.0,
        symbol_exposure=20.0,
        current_position_quantity=Decimal(2),
        daily_pnl=-50.0,
        start_of_day_equity=148.0,
        trading_enabled=False,
    )
    decision = evaluate_risk(
        RiskRequest(
            symbol="SPY",
            side=RiskSide.SELL,
            reference_price=10.0,
            requested_quantity=Decimal(2),
        ),
        holding,
        policy,
    )
    assert decision.approved and decision.approved_quantity == Decimal(2)


def test_a_float_dollar_ceiling_is_refused() -> None:
    """An approximation is not a ceiling. Same rule as a float quantity."""
    with pytest.raises(RiskInputError, match="max_total_notional"):
        evaluate_risk(
            buy(),
            context(equity="100", total="0", symbol="0", cash="100"),
            RiskPolicy(max_total_notional=95.0),  # type: ignore[arg-type]
        )


def test_a_symbol_ceiling_above_the_total_ceiling_is_refused() -> None:
    with pytest.raises(RiskInputError, match="exceeds"):
        evaluate_risk(
            buy(),
            context(equity="100", total="0", symbol="0", cash="100"),
            RiskPolicy(max_position_notional=Decimal(99), max_total_notional=Decimal(95)),
        )


def test_leverage_and_shorting_stay_structurally_impossible() -> None:
    policy = risk_policy_for(allocation_policy_for(POLICY_LIVE_VALIDATION_100))
    assert policy.long_only is True
    assert policy.allow_leverage is False
    assert not hasattr(RiskSide, "SHORT")


def test_no_plan_ever_asks_to_sell_more_than_is_held() -> None:
    """An exit may flatten and may never cross below zero into a short."""
    policy = allocation_policy_for(POLICY_LIVE_VALIDATION_100)
    plan = plan_allocation(
        policy,
        active_symbols=set(),
        account_equity=Decimal(100),
        external_exposure_fraction=Decimal(0),
        reference_prices={"SPY": Decimal(10)},
        actual_quantities={"SPY": Decimal("0.4")},
    )
    (item,) = plan.allocations
    assert item.side is OrderSide.SELL
    assert item.delta_quantity == Decimal("0.4")


# ==========================================================================
# Non-regression: the validation policy is the champion under a spending limit
# ==========================================================================


def test_at_the_authorized_capital_the_two_policies_size_identically() -> None:
    """CRITICAL. This is not a second strategy. Same targets, to the cent."""
    live = plan_at(allocation_policy_for(POLICY_LIVE_VALIDATION_100), "100")
    paper = plan_at(allocation_policy_for(POLICY_FRACTIONAL_RESERVED_90), "100")
    assert [item.to_json_dict() for item in live.allocations] == [
        item.to_json_dict() for item in paper.allocations
    ]


def test_every_frozen_policy_still_digests_to_its_published_hash() -> None:
    """CRITICAL. Adding a policy must not move an existing one's identity."""
    for policy_id, digest in FROZEN_POLICY_HASHES.items():
        assert allocation_policy_for(policy_id).config_hash() == digest, policy_id


def test_the_legacy_policies_carry_no_absolute_ceiling_at_all() -> None:
    for policy_id in FROZEN_POLICY_HASHES:
        policy = allocation_policy_for(policy_id)
        assert policy.slot_notional_ceiling is None
        risk = risk_policy_for(policy)
        assert risk.max_position_notional is None
        assert risk.max_total_notional is None


# ==========================================================================
# The arm switch
# ==========================================================================


def test_a_fresh_store_is_disarmed(store: sqlite3.Connection) -> None:
    """CRITICAL. The absence of a record is not the absence of an answer."""
    assert read_arm_state(store).state == STATE_DISARMED


def test_an_unreadable_arm_row_reads_disarmed(store: sqlite3.Connection) -> None:
    create_arm_state_table(store)
    store.execute("DROP TABLE live_arm_state")
    assert read_arm_state(store).state == STATE_DISARMED


def test_an_unrecognised_arm_value_reads_disarmed(store: sqlite3.Connection) -> None:
    """A state this build cannot interpret is not permission.

    The schema's own CHECK constraint refuses to store such a value, which is
    the first line of defence and is asserted here too. The reader must still
    fail closed on one, because a row can arrive from a build whose constraint
    differed, from a restored backup, or from a hand-edited file - and "this
    store came from somewhere I do not understand" is the case where a
    permissive reader would be most expensive.
    """
    create_arm_state_table(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            "INSERT INTO live_arm_state (id, state, reason, source, changed_at)"
            " VALUES (1, 'PROBABLY', 'x', 'x', '2026-09-08T00:00:00Z')"
        )
    store.rollback()

    store.execute("ALTER TABLE live_arm_state RENAME TO live_arm_state_constrained")
    store.execute(
        "CREATE TABLE live_arm_state ("
        " id INTEGER PRIMARY KEY, state TEXT, reason TEXT, source TEXT,"
        " changed_at TEXT, code_sha TEXT)"
    )
    store.execute(
        "INSERT INTO live_arm_state (id, state, reason, source, changed_at)"
        " VALUES (1, 'PROBABLY', 'x', 'x', '2026-09-08T00:00:00Z')"
    )
    store.commit()
    assert read_arm_state(store).state == STATE_DISARMED


def test_neither_gate_alone_arms_anything(
    store: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CRITICAL. Two gates, and neither can satisfy the other."""
    monkeypatch.setenv(LIVE_ARMED_ENV, "true")
    with pytest.raises(LiveDisarmedError):
        require_armed(store)

    monkeypatch.delenv(LIVE_ARMED_ENV, raising=False)
    arm_live_trading(
        store, now=NOW, reason="first-day activation", confirmation=ARM_CONFIRMATION_TOKEN
    )
    with pytest.raises(LiveDisarmedError):
        require_armed(store)

    monkeypatch.setenv(LIVE_ARMED_ENV, "true")
    assert require_armed(store).state == STATE_ARMED


@pytest.mark.parametrize("value", ["TRUE", "True", "1", "yes", "on", "", " true "])
def test_only_the_exact_environment_value_opens_the_gate(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LIVE_ARMED_ENV, value)
    assert environment_arm_gate_open() is (value.strip() == "true")


def test_arming_requires_the_exact_confirmation(store: sqlite3.Connection) -> None:
    for wrong in (None, "", "ARM", "arm-live-real-money", ARM_CONFIRMATION_TOKEN + " "):
        with pytest.raises(LiveDisarmedError):
            arm_live_trading(store, now=NOW, reason="r", confirmation=wrong)
    assert read_arm_state(store).state == STATE_DISARMED


def test_arming_requires_a_recorded_reason(store: sqlite3.Connection) -> None:
    with pytest.raises(LiveDisarmedError, match="reason"):
        arm_live_trading(store, now=NOW, reason="   ", confirmation=ARM_CONFIRMATION_TOKEN)


def test_disarming_needs_no_token_at_all(store: sqlite3.Connection) -> None:
    """The safe direction is deliberately frictionless."""
    arm_live_trading(store, now=NOW, reason="a", confirmation=ARM_CONFIRMATION_TOKEN)
    assert disarm_live_trading(store, now=NOW, reason="anomaly").state == STATE_DISARMED


def test_disarmed_persists_across_a_restart(tmp_path: Path) -> None:
    """CRITICAL. A switch that a restart could flip is not a kill switch."""
    database = tmp_path / "live.db"
    initialize_database(database)
    with connect(database) as connection:
        arm_live_trading(connection, now=NOW, reason="a", confirmation=ARM_CONFIRMATION_TOKEN)
        disarm_live_trading(connection, now=NOW, reason="operator stopped the day")
    with connect(database) as reopened:
        assert read_arm_state(reopened).state == STATE_DISARMED


def test_disarming_deletes_nothing(store: sqlite3.Connection) -> None:
    """It stops the system adding. It does not throw the record away."""
    store.execute(
        "INSERT INTO positions (symbol, quantity, average_price, updated_at)"
        " VALUES ('SPY', '1', '10', '2026-09-08T00:00:00Z')"
    )
    store.commit()
    disarm_live_trading(store, now=NOW, reason="anomaly")
    assert store.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1


def test_the_arm_switch_writes_an_audit_event(store: sqlite3.Connection) -> None:
    arm_live_trading(store, now=NOW, reason="first day", confirmation=ARM_CONFIRMATION_TOKEN)
    disarm_live_trading(store, now=NOW, reason="done for the day")
    kinds = [
        row[0]
        for row in store.execute(
            "SELECT event_type FROM system_events WHERE event_type LIKE 'LIVE_%' ORDER BY id"
        )
    ]
    assert kinds == ["LIVE_ARMED", "LIVE_DISARMED"]


# ==========================================================================
# The account pin
# ==========================================================================


class FakeAccount:
    def __init__(self, number: str) -> None:
        self.account_number = number


class FakeClient:
    def __init__(self, number: str = "9876543210") -> None:
        self._number = number
        self.mutations = 0

    def get_account(self) -> FakeAccount:
        return FakeAccount(self._number)


class UnreadableClient:
    def get_account(self):  # noqa: ANN201 - deliberately raises
        raise TimeoutError("no answer")


def test_an_unpinned_runtime_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """CRITICAL. There is no mode in which whatever answers is accepted."""
    monkeypatch.delenv(LIVE_ACCOUNT_FINGERPRINT_ENV, raising=False)
    with pytest.raises(AccountIdentityError, match="not set"):
        require_expected_account(FakeClient())


def test_the_wrong_account_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """CRITICAL. A credential swap cannot silently retarget the runtime."""
    monkeypatch.setenv(LIVE_ACCOUNT_FINGERPRINT_ENV, account_fingerprint("1111111111"))
    with pytest.raises(AccountIdentityError, match="pinned to"):
        require_expected_account(FakeClient("2222222222"))


def test_the_pinned_account_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LIVE_ACCOUNT_FINGERPRINT_ENV, account_fingerprint("9876543210"))
    assert require_expected_account(FakeClient()) == account_fingerprint("9876543210")


@pytest.mark.parametrize("pin", ["", "not-hex-at-all-not-hex-at-all-xx", "abc", "A" * 32])
def test_a_malformed_pin_is_refused(pin: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LIVE_ACCOUNT_FINGERPRINT_ENV, pin)
    with pytest.raises(AccountIdentityError):
        require_expected_account(FakeClient())


def test_an_unreadable_account_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed check is never the same answer as a passing one."""
    monkeypatch.setenv(LIVE_ACCOUNT_FINGERPRINT_ENV, account_fingerprint("9876543210"))
    with pytest.raises(AccountIdentityError, match="could not be read"):
        require_expected_account(UnreadableClient())


def test_an_account_with_no_number_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LIVE_ACCOUNT_FINGERPRINT_ENV, account_fingerprint("9876543210"))
    with pytest.raises(AccountIdentityError, match="no account number"):
        require_expected_account(FakeClient(""))


def test_the_pin_is_a_digest_and_not_the_number() -> None:
    """CRITICAL. The pin lives in a world-readable file. It reveals nothing."""
    number = "9876543210"
    pin = account_fingerprint(number)
    assert number not in pin
    assert len(pin) == 32
    assert account_fingerprint(f"  {number.lower()}  \n") == pin


def test_verifying_identity_mutates_nothing() -> None:
    """One GET. No submit, no cancel, no replace, no transfer."""
    client = FakeClient()
    os.environ[LIVE_ACCOUNT_FINGERPRINT_ENV] = account_fingerprint("9876543210")
    try:
        require_expected_account(client)
    finally:
        os.environ.pop(LIVE_ACCOUNT_FINGERPRINT_ENV, None)
    assert client.mutations == 0
    assert not hasattr(client, "submit_order")


# ==========================================================================
# Balance-aware ceilings: the account may start at ~$50 and be topped up later
# ==========================================================================


def ceilings_at(equity: str) -> LiveExposureCeilings:
    return effective_ceilings(
        allocation_policy_for(POLICY_LIVE_VALIDATION_100), verified_equity=Decimal(equity)
    )


def test_a_fifty_dollar_account_is_bound_by_its_balance() -> None:
    """CRITICAL. Half funded means half the ceilings, not the full ones."""
    ceilings = ceilings_at("50")
    assert ceilings.target_gross == Decimal("45.00")
    assert ceilings.hard_gross == Decimal("47.50")
    assert ceilings.exposure_bound == Decimal("50.00")
    assert ceilings.binding == "balance"


def test_a_hundred_dollar_account_reaches_the_authorized_ceilings() -> None:
    ceilings = ceilings_at("100")
    assert ceilings.target_gross == Decimal("90.00")
    assert ceilings.hard_gross == Decimal("95.00")
    assert ceilings.exposure_bound == Decimal("100.00")


def test_the_ceilings_stop_at_the_authorization_however_large_the_account() -> None:
    """CRITICAL. The second deposit raises the ceilings; a good month does not."""
    for equity in ("150", "1000", "100000"):
        ceilings = ceilings_at(equity)
        assert ceilings.target_gross == Decimal("90.00")
        assert ceilings.hard_gross == Decimal("95.00")
        assert ceilings.exposure_bound == Decimal("100.00")
        assert ceilings.binding == "authorization"


def test_exposure_never_exceeds_money_actually_held() -> None:
    """CRITICAL. No leverage, and no spending against a deposit in transit."""
    for equity in ("1", "50", "99.99", "100", "1000"):
        ceilings = ceilings_at(equity)
        assert ceilings.exposure_bound <= Decimal(equity)
        assert ceilings.hard_gross <= ceilings.exposure_bound
        assert ceilings.target_gross <= ceilings.hard_gross


def test_the_allocator_sizes_a_fifty_dollar_book_to_forty_five() -> None:
    """The reported ceiling and the sized plan are the same number."""
    policy = allocation_policy_for(POLICY_LIVE_VALIDATION_100)
    assert gross_of(plan_at(policy, "50")) == Decimal(45)
    assert ceilings_at("50").target_gross == Decimal("45.00")


def test_risk_refuses_to_size_against_an_unreadable_balance() -> None:
    """An equity that could not be read is unknown headroom, not zero risk."""
    policy = allocation_policy_for(POLICY_LIVE_VALIDATION_100)
    for bad in (Decimal(0), Decimal(-1)):
        with pytest.raises(LiveBudgetError):
            effective_ceilings(policy, verified_equity=bad)
    with pytest.raises(LiveBudgetError):
        effective_ceilings(policy, verified_equity=50.0)  # type: ignore[arg-type]


def test_ceilings_are_floored_to_the_cent_never_rounded_up() -> None:
    assert ceilings_at("99.99").target_gross == Decimal("89.99")
    assert ceilings_at("99.99").hard_gross == Decimal("94.99")


# ==========================================================================
# A deposit is not a profit
# ==========================================================================


RISK_DAY = date(2026, 9, 15)


def flow(kind: str, amount: str, day: date = RISK_DAY) -> CashFlowEvent:
    return CashFlowEvent(
        activity_id=f"{kind}-{amount}",
        activity_type=kind,
        amount=Decimal(amount),
        transaction_time=datetime(day.year, day.month, day.day, 17, 0, tzinfo=UTC),
    )


def test_a_trading_only_day_is_not_blocked() -> None:
    assert cash_flow_block_reason([flow("FILL", "10")], risk_day=RISK_DAY) is None
    assert cash_flow_block_reason([], risk_day=RISK_DAY) is None


def test_a_deposit_blocks_new_entries_for_the_day() -> None:
    """CRITICAL. On a $50 book the 2% halt is $1; a $50 deposit would hide it.

    The halt is not corrected or netted around - it is declared untrustworthy
    for the day, which is the honest description and the safe one.
    """
    reason = cash_flow_block_reason([flow("CSD", "50")], risk_day=RISK_DAY)
    assert reason is not None
    assert "CSD" in reason and "No new entries today" in reason


def test_a_withdrawal_blocks_it_too() -> None:
    """A withdrawal moves the baseline the other way and reads as a loss."""
    assert cash_flow_block_reason([flow("CSW", "20")], risk_day=RISK_DAY) is not None


def test_a_deposit_on_another_day_does_not_block_today() -> None:
    """Each UTC day takes a fresh baseline against the settled balance."""
    yesterday = date(2026, 9, 14)
    assert cash_flow_block_reason([flow("CSD", "50", yesterday)], risk_day=RISK_DAY) is None


def test_the_guard_does_not_care_about_direction_or_size() -> None:
    """Any movement at all means the day's equity change is not trading."""
    assert cash_flow_block_reason([flow("CSD", "0.01")], risk_day=RISK_DAY) is not None
    assert cash_flow_block_reason([flow("JNLC", "-5")], risk_day=RISK_DAY) is not None
