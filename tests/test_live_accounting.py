"""Real-money performance accounting: flows, returns, the mark, and the bucket.

The whole suite exists to defend one sentence:

    money moving in is not profit, and money moving out is not loss.

The synthetic cases walk the funding pattern this account is actually in - $50
of capital, a second $50 arriving later, a few dollars of trading, and a manual
withdrawal - and assert at every step that the trading result is the trading
result and nothing else.
"""

from __future__ import annotations

import ast
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from autotrader.liveaccounting import classify, engine, ingest, readmodel, service, store
from autotrader.liveaccounting.models import (
    CHECKPOINT_DAILY_CLOSE,
    CHECKPOINT_INTRADAY,
    CONFIRMATION_CONFIRMED,
    CONFIRMATION_PENDING,
    FLOW_EXTERNAL_DEPOSIT,
    FLOW_EXTERNAL_WITHDRAWAL,
    FLOW_NON_EXTERNAL,
    FLOW_UNKNOWN_EXTERNAL,
    RELATION_PRE_INCEPTION,
    RELATION_SINCE_INCEPTION,
    STATUS_ACCOUNT_MISMATCH,
    STATUS_CLEAN,
    STATUS_NOT_INITIALIZED,
    STATUS_REBUILD_REQUIRED,
    STATUS_UNKNOWN_EXTERNAL_FLOW,
    WITHDRAWAL_MODE_OBSERVE_ONLY,
    ProfitReservePolicy,
)

FINGERPRINT = "a6bbf9c116a5679c58719d82d7e4b3e2"
OTHER_FINGERPRINT = "b7ccf0d227b6789d69820e93e8f5c4f3"

#: Inception. A Tuesday, mid-session, so a same-day flow has room on both sides.
T0 = datetime(2026, 9, 8, 13, 0, tzinfo=UTC)

D = Decimal


def day(offset: int, hour: int = 20) -> datetime:
    return T0.replace(hour=hour) + timedelta(days=offset)


@pytest.fixture
def policy() -> ProfitReservePolicy:
    return ProfitReservePolicy()


@pytest.fixture
def ledger(tmp_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(tmp_path / "live_accounting.db", isolation_level=None)
    connection.row_factory = sqlite3.Row
    store.initialize(connection)
    store.stamp_metadata(connection, account_fingerprint=FINGERPRINT, now=T0)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def opened(ledger: sqlite3.Connection, policy: ProfitReservePolicy) -> sqlite3.Connection:
    """A ledger whose baseline is frozen at $50 with a clean, never-traded account."""
    service.establish_inception(
        ledger,
        service.BrokerSnapshot(T0, D("50"), D("50"), 0, 0),
        account_fingerprint=FINGERPRINT,
        policy=policy,
        now=T0,
    )
    return ledger


def flow(
    connection: sqlite3.Connection,
    identifier: str,
    amount: str,
    when: datetime,
    *,
    classification: str | None = None,
    confirmation: str = CONFIRMATION_CONFIRMED,
    activity_type: str | None = None,
    relation: str = RELATION_SINCE_INCEPTION,
) -> None:
    signed = D(amount)
    resolved = classification or (FLOW_EXTERNAL_DEPOSIT if signed > 0 else FLOW_EXTERNAL_WITHDRAWAL)
    store.record_flow(
        connection,
        broker_activity_id=identifier,
        account_fingerprint=FINGERPRINT,
        activity_type=activity_type or ("CSD" if signed > 0 else "CSW"),
        broker_status="executed" if confirmation == CONFIRMATION_CONFIRMED else "pending",
        classification=resolved,
        classification_reason=None,
        confirmation=confirmation,
        amount=signed,
        currency="USD",
        settle_at=when,
        relation=relation,
        source_digest="0" * 64,
        now=when,
    )


def close(
    connection: sqlite3.Connection,
    equity: str,
    when: datetime,
    *,
    kind: str = CHECKPOINT_DAILY_CLOSE,
) -> None:
    service.take_checkpoint(
        connection,
        service.BrokerSnapshot(when, D(equity), D(equity), 0, 0),
        account_fingerprint=FINGERPRINT,
        kind=kind,
        now=when,
    )


def summary(
    connection: sqlite3.Connection, policy: ProfitReservePolicy, now: datetime | None = None
) -> dict:
    return readmodel.build_summary(
        connection,
        now=now or day(30),
        expected_fingerprint=FINGERPRINT,
        policy=policy,
    )


# ==========================================================================
# The sixteen cases
# ==========================================================================


def test_case_1_a_funded_account_that_has_done_nothing_reports_nothing(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """$50 of capital, no trades, no flows. Zero profit, and zero is not an error."""
    result = summary(opened, policy, now=T0)
    assert result["accounting_status"] == STATUS_CLEAN
    assert result["baseline_equity"] == "50.00"
    assert result["flow_adjusted_equity"] == "50.00"
    assert result["trading_pnl_since_inception"] == "0.00"
    assert result["confirmed_deposits"] == "0.00"
    assert result["confirmed_withdrawals"] == "0.00"
    assert result["adjusted_equity_hwm"] == "50.00"
    assert result["profit_reserve_accrued"] == "0.00"
    assert result["withdrawal_bucket_balance"] == "0.00"


def test_case_2_a_deposit_is_not_a_return(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """The one that matters most. $50 in, and the account has still earned nothing."""
    flow(opened, "dep-1", "50", day(1))
    close(opened, "100", day(1))

    result = summary(opened, policy)
    assert result["confirmed_deposits"] == "50.00"
    assert result["current_broker_equity"] == "100.00"
    assert result["flow_adjusted_equity"] == "50.00"
    assert result["trading_pnl_since_inception"] == "0.00"
    assert Decimal(result["time_weighted_return"]) == 0
    # And it did not become a high-water mark either.
    assert result["adjusted_equity_hwm"] == "50.00"
    assert result["new_hwm_profit"] == "0.00"
    assert result["profit_reserve_accrued"] == "0.00"


def test_case_3_trading_profit_after_a_deposit_is_the_trading_profit(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    flow(opened, "dep-1", "50", day(1))
    close(opened, "100", day(1))
    close(opened, "103", day(2))

    result = summary(opened, policy)
    assert result["flow_adjusted_equity"] == "53.00"
    assert result["trading_pnl_since_inception"] == "3.00"


def test_case_4_a_withdrawal_does_not_erase_the_profit_that_preceded_it(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """The second half of the invariant, and the one an operator will check by eye."""
    flow(opened, "dep-1", "50", day(1))
    close(opened, "100", day(1))
    close(opened, "103", day(2))
    flow(opened, "wd-1", "-10", day(3, hour=15))
    close(opened, "93", day(3))

    result = summary(opened, policy)
    assert result["net_external_flows"] == "40.00"
    assert result["flow_adjusted_equity"] == "53.00"
    assert result["trading_pnl_since_inception"] == "3.00", "a withdrawal is not a loss"


def test_case_5_a_deposit_cannot_paper_over_a_trading_loss(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """A $50 deposit on top of a $2 loss leaves the loss on the screen, at -$2."""
    close(opened, "48", day(1))
    flow(opened, "dep-1", "50", day(2, hour=15))
    close(opened, "98", day(2))

    result = summary(opened, policy)
    assert result["current_broker_equity"] == "98.00"
    assert result["flow_adjusted_equity"] == "48.00"
    assert result["trading_pnl_since_inception"] == "-2.00"
    assert Decimal(result["time_weighted_return"]) < 0


def test_case_6_a_new_mark_accrues_a_fifth_of_the_new_profit(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    close(opened, "55", day(1))

    result = summary(opened, policy)
    assert result["adjusted_equity_hwm"] == "55.00"
    assert result["new_hwm_profit"] == "5.00"
    assert result["profit_reserve_accrued"] == "1.00"
    assert result["withdrawal_bucket_balance"] == "1.00"


def test_case_7_a_drawdown_keeps_the_bucket_and_zeroes_the_preview(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """The bucket is a record of what was earned. The preview is a judgement about now."""
    close(opened, "55", day(1))
    close(opened, "52", day(40))  # a later month, so harvest eligibility is not the blocker

    result = summary(opened, policy, now=day(41))
    assert result["harvest_eligible"] is True
    assert result["withdrawal_bucket_balance"] == "1.00"
    assert Decimal(result["current_drawdown_from_adjusted_hwm"]) < 0
    assert result["withdrawal_preview"] == "0.00", "nobody harvests out of a drawdown"


def test_case_8_recovery_accrues_only_on_the_part_above_the_old_mark(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """$50 to $55 to $52 to $57 accrues on $5 and then on $2, never on $7."""
    close(opened, "55", day(1))
    close(opened, "52", day(2))
    close(opened, "57", day(3))

    result = summary(opened, policy)
    assert result["adjusted_equity_hwm"] == "57.00"
    assert result["new_hwm_profit"] == "2.00"
    assert result["profit_reserve_accrued"] == "1.40"  # 0.20 * 5 + 0.20 * 2


def test_case_9_a_withdrawal_inside_the_bucket_draws_it_down(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    close(opened, "60", day(1))  # $10 of new profit -> $2 accrued
    flow(opened, "wd-1", "-1.50", day(2, hour=15))
    close(opened, "58.50", day(2))

    result = summary(opened, policy)
    assert result["profit_reserve_accrued"] == "2.00"
    assert result["withdrawal_bucket_balance"] == "0.50"
    assert result["unreserved_withdrawal_total"] == "0.00"


def test_case_10_a_withdrawal_past_the_bucket_is_flagged_not_absorbed(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """Taking $10 against a $2 bucket means $8 of capital left. The bucket floors at zero."""
    close(opened, "60", day(1))
    flow(opened, "wd-1", "-10", day(2, hour=15))
    close(opened, "50", day(2))

    result = summary(opened, policy)
    assert result["withdrawal_bucket_balance"] == "0.00"
    assert Decimal(result["withdrawal_bucket_balance"]) >= 0, "the bucket is never negative"
    assert result["unreserved_withdrawal_total"] == "8.00"


def test_case_11_the_same_transfer_seen_twice_is_imported_once(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """Idempotency is the UNIQUE constraint, not a membership test in Python."""
    first = store.record_flow(
        opened,
        broker_activity_id="dep-1",
        account_fingerprint=FINGERPRINT,
        activity_type="CSD",
        broker_status="executed",
        classification=FLOW_EXTERNAL_DEPOSIT,
        classification_reason=None,
        confirmation=CONFIRMATION_CONFIRMED,
        amount=D("50"),
        currency="USD",
        settle_at=day(1),
        relation=RELATION_SINCE_INCEPTION,
        source_digest="0" * 64,
        now=day(1),
    )
    second = store.record_flow(
        opened,
        broker_activity_id="dep-1",
        account_fingerprint=FINGERPRINT,
        activity_type="CSD",
        broker_status="executed",
        classification=FLOW_EXTERNAL_DEPOSIT,
        classification_reason=None,
        confirmation=CONFIRMATION_CONFIRMED,
        amount=D("50"),
        currency="USD",
        settle_at=day(1),
        relation=RELATION_SINCE_INCEPTION,
        source_digest="0" * 64,
        now=day(1),
    )
    assert first is True
    assert second is False

    close(opened, "100", day(1))
    result = summary(opened, policy)
    assert result["confirmed_deposits"] == "50.00", "counted once, not twice"


def test_case_12_pending_money_is_not_money(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """An announced ACH has zero accounting effect until the broker settles it."""
    flow(opened, "dep-pending", "50", day(1), confirmation=CONFIRMATION_PENDING)
    close(opened, "50", day(1))

    result = summary(opened, policy)
    assert result["confirmed_deposits"] == "0.00"
    assert result["pending_flows_excluded_count"] == 1
    assert result["flow_adjusted_equity"] == "50.00"
    assert result["trading_pnl_since_inception"] == "0.00"


def test_case_13_a_backdated_flow_forces_a_replay_and_the_replay_is_right(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """Late money does not corrupt history; it stops the figures until they are redone."""
    close(opened, "100", day(2))
    service.rebuild(opened, policy=policy, now=day(2), reason="routine")

    # Without the deposit, day 2 looked like a $50 gain. It was not.
    before = summary(opened, policy)
    assert before["trading_pnl_since_inception"] == "50.00"

    def reader(activity_type: str, since: datetime | None):
        if activity_type != "CSD":
            return []
        return [
            {
                "id": "dep-late",
                "activity_type": "CSD",
                "status": "executed",
                "net_amount": "50",
                "created_at": day(1).isoformat(),
                "currency": "USD",
            }
        ]

    # Something has to already be in the ledger for a later arrival to be *late*.
    flow(opened, "dep-early", "0.01", day(3))
    result = ingest.import_activities(opened, reader, account_fingerprint=FINGERPRINT, now=day(4))
    assert result.backdated == 1
    assert result.rebuild_marked is True

    blocked = summary(opened, policy)
    assert blocked["accounting_status"] == STATUS_REBUILD_REQUIRED
    assert blocked["trading_pnl_since_inception"] is None, "a wrong figure is not served"

    service.rebuild(opened, policy=policy, now=day(4), reason="late flow")
    after = summary(opened, policy, now=day(2) + timedelta(minutes=5))
    assert after["accounting_status"] == STATUS_CLEAN
    assert after["confirmed_deposits"] == "50.01"
    assert after["trading_pnl_since_inception"] == "-0.01"
    assert after["rebuild_count"] == 2


def test_case_14_an_ambiguous_journal_changes_nothing_and_stops_everything(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """`JNLC` may be external capital or an internal adjustment. Fail closed."""
    classification, reason = classify.classify("JNLC", D("25"))
    assert classification == FLOW_UNKNOWN_EXTERNAL
    assert reason is not None

    flow(
        opened,
        "jnl-1",
        "25",
        day(1),
        classification=FLOW_UNKNOWN_EXTERNAL,
        activity_type="JNLC",
    )
    close(opened, "75", day(1))

    result = summary(opened, policy)
    assert result["accounting_status"] == STATUS_UNKNOWN_EXTERNAL_FLOW
    assert result["unknown_external_flow_count"] == 1
    assert result["flow_adjusted_equity"] is None
    assert result["trading_pnl_since_inception"] is None
    assert result["adjusted_equity_hwm"] is None
    assert result["profit_reserve_accrued"] is None
    assert result["withdrawal_preview"] is None
    assert "JNLC" in str(result["accounting_status_detail"])


def test_case_15_a_settled_sale_is_not_a_deposit() -> None:
    """Trade proceeds are trading. Classifying them as capital would erase the trade."""
    classification, _ = classify.classify("FILL", D("120"))
    assert classification == FLOW_NON_EXTERNAL


def test_case_16_dividends_and_interest_stay_inside_performance() -> None:
    """A system that netted out its own dividends would report never earning any."""
    for activity_type in ("DIV", "DIVCGL", "INT", "INTNRA"):
        classification, _ = classify.classify(activity_type, D("0.37"))
        assert classification == FLOW_NON_EXTERNAL, activity_type
    for activity_type in ("FEE", "CFEE", "PTR", "WH"):
        classification, _ = classify.classify(activity_type, D("-0.02"))
        assert classification == FLOW_NON_EXTERNAL, activity_type


# ==========================================================================
# Time-weighted return
# ==========================================================================


def test_a_deposit_with_no_market_movement_leaves_the_return_untouched() -> None:
    assert engine.subperiod_return(begin_equity=D("50"), end_equity=D("100"), net_flow=D("50")) == 0


def test_a_withdrawal_with_no_market_movement_leaves_the_return_untouched() -> None:
    assert (
        engine.subperiod_return(begin_equity=D("100"), end_equity=D("90"), net_flow=D("-10")) == 0
    )


def test_a_subperiod_with_no_flow_is_the_ordinary_return() -> None:
    """The conservative convention costs nothing when nothing moved."""
    assert engine.subperiod_return(begin_equity=D("50"), end_equity=D("55"), net_flow=D("0")) == D(
        "0.1"
    )


def test_profit_before_a_deposit_is_attributed_to_the_smaller_base(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """Earn 10% on $50, then take $50 in. The return is 10%, not 5%."""
    close(opened, "55", day(1))
    flow(opened, "dep-1", "50", day(2, hour=15))
    close(opened, "105", day(2))

    result = summary(opened, policy)
    assert Decimal(result["time_weighted_return"]) == pytest.approx(D("0.1"), abs=1e-9)
    assert result["trading_pnl_since_inception"] == "5.00"


def test_profit_after_a_deposit_is_attributed_to_the_larger_base(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """Take $50 in, then earn 10% on $100. The return is 10%, not 20%."""
    flow(opened, "dep-1", "50", day(1, hour=15))
    close(opened, "100", day(1))
    close(opened, "110", day(2))

    result = summary(opened, policy)
    assert Decimal(result["time_weighted_return"]) == pytest.approx(D("0.1"), abs=1e-9)
    assert result["trading_pnl_since_inception"] == "10.00"


def test_a_same_day_flow_uses_the_conservative_limb_and_says_so(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """When a flow and a gain share one sub-period, the lower reading is published.

    $50 grows to $55 and $50 arrives, both inside one day; the close is $105.
    Flow-at-end would read that as +10%, flow-at-start as +5%. The honest
    answer is somewhere between and the ledger cannot tell which, so the
    conservative limb is taken - and `twr_bounded_subperiods` says the figure
    is a bound rather than a measurement.
    """
    flow(opened, "dep-1", "50", day(1, hour=17))
    close(opened, "105", day(1))

    result = summary(opened, policy)
    assert Decimal(result["time_weighted_return"]) == pytest.approx(D("0.05"), abs=1e-9)
    assert result["twr_bounded_subperiods"] == 1
    assert result["twr_convention"] == "CONSERVATIVE_MIN"


def test_multiple_flows_compound_deterministically(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """Two flows, two gains, and the same answer however many times it is asked."""
    flow(opened, "dep-1", "50", day(1, hour=15))
    close(opened, "100", day(1))
    close(opened, "110", day(2))
    flow(opened, "wd-1", "-10", day(3, hour=15))
    close(opened, "110", day(3))

    answers = {summary(opened, policy)["time_weighted_return"] for _ in range(5)}
    assert len(answers) == 1
    assert Decimal(answers.pop()) > 0


def test_the_return_is_undefined_rather_than_flat_when_there_is_no_base() -> None:
    assert engine.subperiod_return(begin_equity=D("0"), end_equity=D("10"), net_flow=D("0")) is None


# ==========================================================================
# High-water mark
# ==========================================================================


def test_a_deposit_cannot_create_high_water_mark_profit(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    flow(opened, "dep-1", "1000", day(1))
    close(opened, "1050", day(1))

    result = summary(opened, policy)
    assert result["adjusted_equity_hwm"] == "50.00"
    assert result["new_hwm_profit"] == "0.00"
    assert result["profit_reserve_accrued"] == "0.00"


def test_a_withdrawal_cannot_erase_the_mark(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    close(opened, "60", day(1))
    flow(opened, "wd-1", "-20", day(2, hour=15))
    close(opened, "40", day(2))

    result = summary(opened, policy)
    assert result["adjusted_equity_hwm"] == "60.00"
    assert result["flow_adjusted_equity"] == "60.00"
    assert result["current_drawdown_from_adjusted_hwm"] == "0.0000000000"


def test_a_trading_gain_creates_the_mark(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    close(opened, "56", day(1))
    assert summary(opened, policy)["adjusted_equity_hwm"] == "56.00"


def test_a_drawdown_accrues_nothing(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    close(opened, "45", day(1))
    result = summary(opened, policy)
    assert result["adjusted_equity_hwm"] == "50.00"
    assert result["profit_reserve_accrued"] == "0.00"
    assert Decimal(result["current_drawdown_from_adjusted_hwm"]) < 0


def test_recovering_to_an_old_mark_accrues_nothing(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """That profit was counted the first time the account reached it."""
    close(opened, "55", day(1))
    close(opened, "50", day(2))
    close(opened, "55", day(3))

    result = summary(opened, policy)
    assert result["profit_reserve_accrued"] == "1.00"
    assert result["new_hwm_profit"] == "0.00"


def test_an_intraday_spike_never_becomes_an_entitlement(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """$70 at 11:04, $50 at the close. The mark stays $50 and the bucket stays empty."""
    close(opened, "70", day(1, hour=15), kind=CHECKPOINT_INTRADAY)
    close(opened, "50", day(1))

    result = summary(opened, policy)
    assert result["observed_intraday_peak"] == "70.00"
    assert result["adjusted_equity_hwm"] == "50.00"
    assert result["profit_reserve_accrued"] == "0.00"
    assert result["withdrawal_bucket_balance"] == "0.00"


# ==========================================================================
# OBSERVE_ONLY
# ==========================================================================


def test_the_first_month_authorizes_no_withdrawal_whatever_the_bucket_holds(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    close(opened, "80", day(1))
    close(opened, "90", day(40))

    result = summary(opened, policy, now=day(41))
    assert Decimal(result["withdrawal_bucket_balance"]) > 0
    assert result["withdrawal_mode"] == WITHDRAWAL_MODE_OBSERVE_ONLY
    assert result["withdrawal_authorized"] is False
    assert result["authorized_withdrawal_amount"] == "0.00"
    assert result["automatic_transfer_enabled"] is False


def test_the_policy_cannot_be_constructed_with_withdrawals_switched_on() -> None:
    """Activating withdrawals is a later program's decision, not a keyword argument."""
    with pytest.raises(Exception, match="OBSERVE_ONLY"):
        ProfitReservePolicy(withdrawal_mode="ACTIVE")
    with pytest.raises(Exception, match="OBSERVE_ONLY"):
        ProfitReservePolicy(automatic_transfer=True)
    with pytest.raises(Exception, match="OBSERVE_ONLY"):
        ProfitReservePolicy(withdrawal_authorized=True)


def test_no_transfer_or_withdrawal_call_is_reachable_from_this_package() -> None:
    """A structural check, not a promise: the names do not appear in the code.

    Prose is stripped first, because this package's own documentation explains
    at length what it must never do and a naive substring scan would trip over
    the sentences that state the rule.
    """
    from autotrader import liveaccounting

    root = Path(liveaccounting.__file__).resolve().parent
    forbidden = (
        "create_transfer",
        "request_withdrawal",
        "submit_order",
        "cancel_order",
        "close_position",
        "close_all_positions",
        "TradingClient",
        "MarketOrderRequest",
    )
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                body = node.body
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    node.body = body[1:] or [ast.Pass()]
        code = ast.unparse(tree)
        for name in forbidden:
            assert name not in code, f"{name} found in liveaccounting/{path.name}"


def test_the_accounting_package_is_not_imported_by_any_trading_path() -> None:
    """Accounting must never become a strategy input. The direction is asserted."""
    from autotrader import liveaccounting

    package_root = Path(liveaccounting.__file__).resolve().parents[1]
    consumers = []
    for path in sorted(package_root.rglob("*.py")):
        if "liveaccounting" in path.parts:
            continue
        if "liveaccounting" in path.read_text():
            consumers.append(path.relative_to(package_root).as_posix())

    # Reading it is fine for a dashboard (it displays the figures) and for the
    # CLI (an operator asks for them). What must never happen is a module that
    # DECIDES or EXECUTES reading them: the moment a reserve balance can reach
    # a sizing calculation, this stops being accounting and starts being a
    # strategy input that nobody validated.
    deciding = (
        "equity/",
        "execution/",
        "risk/",
        "decision/",
        "strategies/",
        "shadow/",
        "runtime/",
        "live/",
        "reconciliation/",
        "accounting/",
        "backtest/",
    )
    offenders = [name for name in consumers if name.startswith(deciding)]
    assert offenders == [], offenders
    assert set(consumers) <= {"cli/__init__.py", "dashboard/live_accounting.py"}, consumers


# ==========================================================================
# Rebuildability
# ==========================================================================


def test_the_whole_derived_state_replays_from_the_authoritative_ledger(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """Drop everything derived, replay, and get the identical answer.

    This is the property that makes the design safe: there is no incrementally
    maintained mark to corrupt and no running bucket total to drift.
    """
    flow(opened, "dep-1", "50", day(1, hour=15))
    close(opened, "100", day(1))
    close(opened, "112", day(2))
    flow(opened, "wd-1", "-3", day(3, hour=15))
    close(opened, "115", day(3))
    close(opened, "108", day(4))

    service.rebuild(opened, policy=policy, now=day(5), reason="first")
    before = summary(opened, policy)
    events_before = [
        dict(row) for row in opened.execute("SELECT * FROM profit_reserve_events ORDER BY event_id")
    ]

    opened.execute("DELETE FROM profit_reserve_events")
    assert opened.execute("SELECT COUNT(*) AS n FROM profit_reserve_events").fetchone()["n"] == 0

    service.rebuild(opened, policy=policy, now=day(5), reason="replay")
    after = summary(opened, policy)
    events_after = [
        dict(row) for row in opened.execute("SELECT * FROM profit_reserve_events ORDER BY event_id")
    ]

    for field in (
        "flow_adjusted_equity",
        "trading_pnl_since_inception",
        "time_weighted_return",
        "adjusted_equity_hwm",
        "current_drawdown_from_adjusted_hwm",
        "profit_reserve_accrued",
        "withdrawal_bucket_balance",
        "unreserved_withdrawal_total",
    ):
        assert before[field] == after[field], field

    stripped_before = [
        {k: v for k, v in row.items() if k not in ("event_id", "created_at")}
        for row in events_before
    ]
    stripped_after = [
        {k: v for k, v in row.items() if k not in ("event_id", "created_at")}
        for row in events_after
    ]
    assert stripped_before == stripped_after


def test_a_rebuild_is_counted_and_never_silent(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    close(opened, "55", day(1))
    assert service.rebuild(opened, policy=policy, now=day(2), reason="one") == 1
    assert service.rebuild(opened, policy=policy, now=day(3), reason="two") == 2
    assert summary(opened, policy)["rebuild_count"] == 2


def test_the_answer_does_not_depend_on_the_order_the_ledger_was_written_in(
    ledger: sqlite3.Connection, tmp_path: Path, policy: ProfitReservePolicy
) -> None:
    """Same facts, opposite insertion order, identical answer."""

    def build(connection: sqlite3.Connection, reversed_order: bool) -> dict:
        service.establish_inception(
            connection,
            service.BrokerSnapshot(T0, D("50"), D("50"), 0, 0),
            account_fingerprint=FINGERPRINT,
            policy=policy,
            now=T0,
        )
        entries = [("dep-1", "50", day(1, hour=15)), ("wd-1", "-5", day(3, hour=15))]
        closes = [("100", day(1)), ("108", day(2)), ("103", day(3))]
        if reversed_order:
            entries.reverse()
            closes.reverse()
        for identifier, amount, when in entries:
            flow(connection, identifier, amount, when)
        for equity, when in closes:
            close(connection, equity, when)
        return readmodel.build_summary(
            connection, now=day(30), expected_fingerprint=FINGERPRINT, policy=policy
        )

    forward = build(ledger, reversed_order=False)

    other = sqlite3.connect(tmp_path / "second.db", isolation_level=None)
    other.row_factory = sqlite3.Row
    store.initialize(other)
    store.stamp_metadata(other, account_fingerprint=FINGERPRINT, now=T0)
    backward = build(other, reversed_order=True)
    other.close()

    for field in (
        "flow_adjusted_equity",
        "trading_pnl_since_inception",
        "adjusted_equity_hwm",
        "profit_reserve_accrued",
        "withdrawal_bucket_balance",
        "time_weighted_return",
    ):
        assert forward[field] == backward[field], field


# ==========================================================================
# Inception, scoping and refusals
# ==========================================================================


def test_the_funding_that_delivered_the_baseline_is_not_also_a_deposit(
    ledger: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """The account's real history: one $50 CSD, dated before inception was frozen.

    Counting it would report a $50 account that had doubled before placing an
    order. It stays in the ledger as history and contributes nothing.
    """
    funding_at = T0 - timedelta(hours=2)

    def reader(activity_type: str, since: datetime | None):
        if activity_type != "CSD":
            return []
        return [
            {
                "id": "20260907000000000::166bd2da",
                "activity_type": "CSD",
                "status": "executed",
                "net_amount": "50",
                "created_at": funding_at.isoformat(),
                "currency": "USD",
            }
        ]

    service.establish_inception(
        ledger,
        service.BrokerSnapshot(T0, D("50"), D("50"), 0, 0),
        account_fingerprint=FINGERPRINT,
        policy=policy,
        now=T0,
    )
    result = ingest.import_activities(ledger, reader, account_fingerprint=FINGERPRINT, now=T0)
    assert result.imported == 1
    assert result.pre_inception == 1

    row = ledger.execute("SELECT * FROM external_cash_flows").fetchone()
    assert row["relation"] == RELATION_PRE_INCEPTION
    assert row["classification"] == FLOW_EXTERNAL_DEPOSIT

    payload = summary(ledger, policy, now=T0)
    assert payload["confirmed_deposits"] == "0.00"
    assert payload["baseline_equity"] == "50.00"
    assert payload["trading_pnl_since_inception"] == "0.00"


def test_inception_refuses_the_shortcut_on_an_account_that_has_traded(
    ledger: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """History is reconstructed or declared, never silently reset."""
    with pytest.raises(service.InceptionRefused, match="Reconstruct"):
        service.establish_inception(
            ledger,
            service.BrokerSnapshot(T0, D("50"), D("50"), position_count=2, order_count=7),
            account_fingerprint=FINGERPRINT,
            policy=policy,
            now=T0,
        )
    assert store.read_state(ledger) is None


def test_the_baseline_cannot_be_quietly_refrozen(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    with pytest.raises(store.LiveAccountingStoreError, match="already frozen"):
        service.establish_inception(
            opened,
            service.BrokerSnapshot(day(5), D("999"), D("999"), 0, 0),
            account_fingerprint=FINGERPRINT,
            policy=policy,
            now=day(5),
        )


def test_a_ledger_belonging_to_another_account_fails_closed(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    close(opened, "60", day(1))
    result = readmodel.build_summary(
        opened, now=day(2), expected_fingerprint=OTHER_FINGERPRINT, policy=policy
    )
    assert result["accounting_status"] == STATUS_ACCOUNT_MISMATCH
    assert result["flow_adjusted_equity"] is None
    assert result["adjusted_equity_hwm"] is None

    with pytest.raises(store.AccountScopeError):
        store.record_checkpoint(
            opened,
            account_fingerprint=OTHER_FINGERPRINT,
            taken_at=day(3),
            kind=CHECKPOINT_DAILY_CLOSE,
            broker_equity=D("1000000"),
            broker_cash=D("1000000"),
            now=day(3),
        )


def test_a_reading_far_from_the_last_checkpoint_says_so_and_still_publishes(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """STALE describes a moment that has passed. It is a caveat, not a suppression.

    Unlike the fail-closed statuses, a stale reading is a *correct* figure about
    an earlier instant, so it is published with the caveat attached rather than
    withheld. Withholding it would hide the last known state of a real-money
    account at exactly the moment somebody wants to see it.
    """
    close(opened, "55", day(1))

    fresh = summary(opened, policy, now=day(1) + timedelta(minutes=2))
    assert fresh["accounting_status"] == STATUS_CLEAN
    assert fresh["data_freshness"] == "FRESH"

    stale = summary(opened, policy, now=day(1) + timedelta(hours=6))
    assert stale["accounting_status"] == "STALE"
    assert stale["data_freshness"] == "STALE"
    assert stale["flow_adjusted_equity"] == "55.00", "stale is a caveat, not a suppression"
    assert stale["data_freshness_seconds"] == 6 * 3600


def test_a_ledger_without_a_baseline_publishes_nothing(
    ledger: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    result = summary(ledger, policy)
    assert result["accounting_status"] == STATUS_NOT_INITIALIZED
    assert result["flow_adjusted_equity"] is None
    assert result["withdrawal_preview"] is None


def test_two_closes_for_one_day_replace_rather_than_accumulate(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """Otherwise the reserve would accrue twice off one day's profit."""
    close(opened, "55", day(1))
    close(opened, "55", day(1))

    rows = opened.execute(
        "SELECT COUNT(*) AS n FROM performance_checkpoints WHERE kind = 'DAILY_CLOSE'"
    ).fetchone()
    assert rows["n"] == 1
    assert summary(opened, policy)["profit_reserve_accrued"] == "1.00"


# ==========================================================================
# Classification
# ==========================================================================


def test_an_unrecognised_activity_type_fails_closed() -> None:
    classification, reason = classify.classify("WOMBAT", D("10"))
    assert classification == FLOW_UNKNOWN_EXTERNAL
    assert "never classified" in str(reason)


def test_a_type_and_a_sign_that_disagree_are_both_disbelieved() -> None:
    """A deposit that removes money is a contradiction, and a contradiction is refused."""
    assert classify.classify("CSD", D("-50"))[0] == FLOW_UNKNOWN_EXTERNAL
    assert classify.classify("CSW", D("50"))[0] == FLOW_UNKNOWN_EXTERNAL
    assert classify.classify("CSD", D("50"))[0] == FLOW_EXTERNAL_DEPOSIT
    assert classify.classify("CSW", D("-50"))[0] == FLOW_EXTERNAL_WITHDRAWAL


def test_an_unreadable_amount_is_not_zero() -> None:
    assert classify.parse_amount("not a number") is None
    assert classify.classify("CSD", None)[0] == FLOW_UNKNOWN_EXTERNAL


def test_only_an_executed_activity_is_confirmed() -> None:
    assert classify.confirmation_of("executed") == CONFIRMATION_CONFIRMED
    assert classify.confirmation_of("pending") == CONFIRMATION_PENDING
    assert classify.confirmation_of(None) == CONFIRMATION_PENDING
    assert classify.confirmation_of("canceled") == "VOID"


def test_the_accounting_vocabulary_is_narrower_than_the_safety_guard_s() -> None:
    """Two questions, two vocabularies, and neither is the other's bug.

    `live.budget` blocks entries on any of CSD, CSW or JNLC because
    over-inclusion there costs one quiet day. Accounting counts only CSD and
    CSW because over-inclusion here silently corrupts performance.
    """
    from autotrader.live.budget import CASH_FLOW_ACTIVITY_TYPES

    assert classify.EXTERNAL_CAPITAL_TYPES < CASH_FLOW_ACTIVITY_TYPES
    assert "JNLC" in CASH_FLOW_ACTIVITY_TYPES
    assert "JNLC" not in classify.EXTERNAL_CAPITAL_TYPES
    assert "JNLC" in classify.AMBIGUOUS_TYPES


def test_every_ingested_type_is_one_the_broker_actually_publishes() -> None:
    """The list was verified against the API, not copied from documentation.

    An unknown type is rejected with `40010001 invalid activity type` and the
    rejection aborts the whole import - the right direction (a loud stop beats
    a quietly missed deposit) but a poor reason to stop. The types below were
    each requested read-only against the real account and answered; `CSC`,
    `SSO`, `SSP`, `SSF`, `NAME` and `REG` were rejected and are therefore not
    named anywhere in this package as though they were real.
    """
    verified_valid = {
        "CSD",
        "CSW",
        "JNLC",
        "JNLS",
        "ACATC",
        "ACATS",
        "CSR",
        "CFEE",
        "MA",
        "NC",
        "REORG",
        "SPIN",
        "SPLIT",
        "SC",
        "DIVFT",
        "DIVTW",
        "WH",
        "PTR",
        "DIV",
        "DIVCGL",
        "DIVCGS",
        "DIVNRA",
        "DIVROC",
        "DIVTXEX",
        "INT",
        "INTNRA",
        "INTTW",
        "FEE",
        "PTC",
        "FILL",
        "OPASN",
        "OPEXP",
    }
    verified_invalid = {"CSC", "SSO", "SSP", "SSF", "NAME", "REG", "TAF", "OPXRC"}

    assert set(ingest.INGESTED_ACTIVITY_TYPES) <= verified_valid
    assert set(ingest.INGESTED_ACTIVITY_TYPES) & verified_invalid == set()
    claimed = (
        classify.NON_EXTERNAL_TYPES | classify.AMBIGUOUS_TYPES | classify.EXTERNAL_CAPITAL_TYPES
    )
    assert claimed & verified_invalid == set(), (
        "this package names an activity type the broker rejects as invalid"
    )
    assert claimed <= verified_valid


def test_a_type_dropped_from_the_recognised_list_now_fails_closed() -> None:
    """Removing a type makes it stricter, not looser. That is the safe direction."""
    for activity_type in ("REG", "TAF", "NAME"):
        assert classify.classify(activity_type, D("-0.01"))[0] == FLOW_UNKNOWN_EXTERNAL


def test_the_group_alias_is_not_ingested() -> None:
    """`TRANS` returns the same rows with the same ids as CSD and CSW."""
    assert "TRANS" not in ingest.INGESTED_ACTIVITY_TYPES
    assert "CSD" in ingest.INGESTED_ACTIVITY_TYPES
    assert "CSW" in ingest.INGESTED_ACTIVITY_TYPES


# ==========================================================================
# The read-only API
# ==========================================================================


def test_the_api_exposes_no_method_that_could_move_money() -> None:
    from autotrader.dashboard import live_accounting_api

    application = live_accounting_api.create_app()
    assert live_accounting_api.route_methods(application) <= live_accounting_api.ALLOWED_METHODS

    paths = {route.path for route in application.routes}
    for forbidden in ("withdraw", "transfer", "harvest", "authorize", "reserve"):
        assert not any(forbidden in path for path in paths), forbidden


def test_the_dashboard_reader_reports_a_missing_ledger_rather_than_zero(
    tmp_path: Path,
) -> None:
    from autotrader.dashboard import live_accounting

    result = live_accounting.build_summary(now=T0, path=tmp_path / "absent.db")
    assert result["accounting_status"] == STATUS_NOT_INITIALIZED
    assert result["withdrawal_authorized"] is False
    assert result["broker_withdrawable_cash"] is None


def test_the_dashboard_connection_refuses_a_write(opened: sqlite3.Connection) -> None:
    path = Path(opened.execute("PRAGMA database_list").fetchone()["file"])
    with (
        store.connect_read_only(path) as connection,
        pytest.raises(sqlite3.OperationalError, match="readonly|read-only"),
    ):
        connection.execute("DELETE FROM performance_checkpoints")


def test_withdrawable_cash_is_never_inferred_from_another_field(
    opened: sqlite3.Connection, policy: ProfitReservePolicy
) -> None:
    """`cash` on a cash account includes unsettled proceeds. It is not withdrawable."""
    close(opened, "60", day(1))
    result = summary(opened, policy)
    assert result["broker_withdrawable_cash"] is None
    assert result["broker_withdrawable_cash_status"] == "NOT_EXPOSED_BY_BROKER"
    assert result["current_cash"] == "60.00"
