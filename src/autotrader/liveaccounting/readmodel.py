"""Assembling the frozen contract payload. The only place field names are written.

`docs/LIVE_ACCOUNTING_API_CONTRACT.md` and `live_accounting_contract.json` are
the interface Prompt 3 builds against, and this module is what has to satisfy
them. `tests/test_live_accounting_contract.py` compares the two mechanically,
so a field renamed here fails the suite rather than silently breaking a
dashboard that was written against the frozen names.

**Suppression is the behaviour worth understanding.** When accounting cannot be
trusted - an unclassifiable activity, a pending rebuild, an unreadable broker,
the wrong account - every derived money figure comes back `null` and
`accounting_status_detail` says why. It would be easy, and much worse, to serve
the last good numbers with a warning badge: the numbers would be precise, they
would be wrong, and nobody reading a dashboard at 09:45 distinguishes a stale
figure from a current one. "Unknown" is the honest answer and it is the one
this module gives.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from autotrader.liveaccounting import engine, store
from autotrader.liveaccounting.models import (
    CONFIRMATION_CONFIRMED,
    ENVIRONMENT_LIVE,
    FRESHNESS_FRESH,
    FRESHNESS_STALE,
    FRESHNESS_UNKNOWN,
    STALENESS_HORIZON_SECONDS,
    STATUS_ACCOUNT_MISMATCH,
    STATUS_BROKER_UNAVAILABLE,
    STATUS_CLEAN,
    STATUS_NOT_INITIALIZED,
    STATUS_REBUILD_REQUIRED,
    STATUS_STALE,
    STATUS_UNKNOWN_EXTERNAL_FLOW,
    SUPPRESSING_STATUSES,
    TWR_CONVENTION_CONSERVATIVE_MIN,
    WITHDRAWABLE_NOT_EXPOSED,
    WITHDRAWAL_MODE_OBSERVE_ONLY,
    ProfitReservePolicy,
    floor_cent,
)

#: The version of the frozen contract this module implements.
CONTRACT_VERSION = "1.0.0"


#: Passed through from Prompt-1 Live safety state, never computed here.
@dataclass(frozen=True)
class LiveSafetyState:
    """`live_ready` and `live_armed`, as read from the Prompt-1 gates.

    Both are `None` when the safety state could not be read, and `None` must be
    displayed as *unknown*. An unreadable arm state is emphatically not a
    disarmed one, and a dashboard that rendered it as "DISARMED" would be
    making the most dangerous possible substitution.
    """

    live_ready: bool | None = None
    live_armed: bool | None = None


def _money(value: Decimal | None) -> str | None:
    """Money, floored to the cent, as a plain decimal string. Never exponent notation."""
    return None if value is None else format(floor_cent(value), "f")


def _ratio(value: Decimal | None) -> str | None:
    """A ratio as a plain decimal string. `format(v, "f")` because `str(Decimal("0E-10"))`
    is `'0E-10'` - exact, and unreadable on a dashboard."""
    return None if value is None else format(value, "f")


def _instant(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def build_summary(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    expected_fingerprint: str | None = None,
    policy: ProfitReservePolicy | None = None,
    safety: LiveSafetyState | None = None,
    realized_pnl: Decimal | None = None,
    realized_pnl_basis_status: str | None = None,
    broker_unavailable: bool = False,
) -> dict[str, object]:
    """The whole contract payload, from one consistent read of the ledger."""
    policy = policy or ProfitReservePolicy()
    safety = safety or LiveSafetyState()

    stored_fingerprint = store.stored_account(connection)
    state = store.read_state(connection)

    payload: dict[str, object] = {
        "contract_version": CONTRACT_VERSION,
        "generated_at": _instant(now),
        "account_fingerprint": stored_fingerprint or "",
        "environment": ENVIRONMENT_LIVE,
        "broker_withdrawable_cash": None,
        "broker_withdrawable_cash_status": WITHDRAWABLE_NOT_EXPOSED,
        "profit_reserve_policy_id": policy.policy_id,
        "profit_reserve_policy_hash": policy.policy_hash,
        "profit_reserve_rate": format(policy.reserve_rate, "f"),
        "twr_convention": TWR_CONVENTION_CONSERVATIVE_MIN,
        # OBSERVE_ONLY, and not as a computed value. These four are literals
        # because there is no code path in this package that could set them
        # otherwise, and writing them as literals says so.
        "withdrawal_mode": WITHDRAWAL_MODE_OBSERVE_ONLY,
        "withdrawal_authorized": False,
        "authorized_withdrawal_amount": "0.00",
        "automatic_transfer_enabled": False,
        "live_ready": safety.live_ready,
        "live_armed": safety.live_armed,
    }

    flows = store.read_flows(connection)
    checkpoints = store.read_checkpoints(connection)
    unknown_count = engine.unknown_flow_count(flows)
    pending_count = sum(1 for flow in flows if flow.confirmation != CONFIRMATION_CONFIRMED)

    payload["unknown_external_flow_count"] = unknown_count
    payload["pending_flows_excluded_count"] = pending_count

    latest = store.latest_checkpoint_row(connection)
    latest_at = None if latest is None else datetime.fromisoformat(str(latest["taken_at"]))
    payload["last_accounting_checkpoint_at"] = _instant(latest_at)
    authoritative = store.latest_authoritative_row(connection)
    authoritative_at = (
        None if authoritative is None else datetime.fromisoformat(str(authoritative["taken_at"]))
    )
    payload["last_completed_day_checkpoint_at"] = _instant(authoritative_at)

    if latest_at is None:
        freshness_seconds = None
        payload["data_freshness"] = FRESHNESS_UNKNOWN
    else:
        freshness_seconds = int((now - latest_at).total_seconds())
        payload["data_freshness"] = (
            FRESHNESS_STALE if freshness_seconds > STALENESS_HORIZON_SECONDS else FRESHNESS_FRESH
        )
    payload["data_freshness_seconds"] = freshness_seconds

    payload["rebuild_count"] = 0 if state is None else int(state["rebuild_count"])
    payload["last_rebuild_at"] = (
        None
        if state is None or state["last_rebuild_at"] is None
        else datetime.fromisoformat(str(state["last_rebuild_at"])).astimezone(UTC).isoformat()
    )

    # ------------------------------------------------------------------
    # Status, decided before any figure is computed
    # ------------------------------------------------------------------
    detail: str | None = None
    if state is None or stored_fingerprint is None:
        status = STATUS_NOT_INITIALIZED
        detail = (
            "Accounting inception has not been frozen for this account, so there is no "
            "baseline to measure anything against."
        )
    elif expected_fingerprint is not None and stored_fingerprint != expected_fingerprint:
        status = STATUS_ACCOUNT_MISMATCH
        detail = (
            f"This ledger belongs to account {stored_fingerprint} but the caller is operating "
            f"{expected_fingerprint}. No figure was published and no balance was combined."
        )
    elif broker_unavailable or latest is None:
        status = STATUS_BROKER_UNAVAILABLE
        detail = (
            "The broker account could not be read, so there is no current equity to adjust. "
            "No figure was published."
        )
    elif unknown_count > 0:
        status = STATUS_UNKNOWN_EXTERNAL_FLOW
        rows = store.unknown_flow_rows(connection)
        kinds = ", ".join(sorted({str(row["activity_type"]) for row in rows}))
        status_reason = next(
            (str(row["classification_reason"]) for row in rows if row["classification_reason"]),
            "",
        )
        detail = (
            f"{unknown_count} broker activity/activities ({kinds}) could not be classified as "
            f"either external capital or account return. {status_reason} Performance figures "
            "are withheld rather than published on an assumption."
        )
    elif int(state["rebuild_required"]):
        status = STATUS_REBUILD_REQUIRED
        reason = str(state["last_rebuild_reason"] or "reason not recorded")
        detail = (
            "A backdated or late external flow has been recorded and the derived state has "
            f"not yet been replayed against it ({reason}). Figures are withheld until the "
            "replay runs."
        )
    elif freshness_seconds is not None and freshness_seconds > STALENESS_HORIZON_SECONDS:
        status = STATUS_STALE
        detail = (
            f"The last checkpoint is {freshness_seconds}s old, past the "
            f"{STALENESS_HORIZON_SECONDS}s horizon. The figures below describe that moment, "
            "not this one."
        )
    else:
        status = STATUS_CLEAN

    payload["accounting_status"] = status
    payload["accounting_status_detail"] = detail

    inception_at = None if state is None else datetime.fromisoformat(str(state["inception_at"]))
    payload["accounting_inception_at"] = _instant(inception_at)
    payload["baseline_equity"] = (
        None if state is None else _money(store.text_decimal(state["baseline_equity"]))
    )
    payload["baseline_cash"] = (
        None if state is None else _money(store.text_decimal(state["baseline_cash"]))
    )

    last_flow_at = max(
        (flow.settle_at for flow in flows if flow.counts_for_performance), default=None
    )
    payload["last_external_flow_at"] = _instant(last_flow_at)

    if status in SUPPRESSING_STATUSES:
        return _suppressed(payload, inception_at=inception_at, now=now)

    assert state is not None and latest is not None  # noqa: S101 - narrowed above

    baseline_equity = store.text_decimal(state["baseline_equity"])
    current_equity = store.text_decimal(latest["broker_equity"])
    current_cash = store.text_decimal(latest["broker_cash"])
    unrealized = (
        None if latest["unrealized_pnl"] is None else store.text_decimal(latest["unrealized_pnl"])
    )

    deposits = engine.confirmed_deposits(flows)
    withdrawals = engine.confirmed_withdrawals(flows)
    net_flows = deposits - withdrawals
    adjusted = engine.flow_adjusted_equity(current_equity, net_flows)
    trading_pnl = adjusted - baseline_equity

    reserve = engine.evaluate_reserve(
        baseline_equity=baseline_equity, checkpoints=checkpoints, flows=flows, policy=policy
    )
    twr = engine.time_weighted_return(checkpoints, flows)
    eligible = engine.harvest_eligible(
        inception_at=inception_at, latest_authoritative_at=authoritative_at
    )
    preview = engine.withdrawal_preview(
        bucket_balance=reserve.withdrawal_bucket_balance,
        adjusted=adjusted,
        hwm=reserve.adjusted_equity_hwm,
        eligible=eligible,
    )

    payload.update(
        {
            "current_broker_equity": _money(current_equity),
            "current_cash": _money(current_cash),
            "confirmed_deposits": _money(deposits),
            "confirmed_withdrawals": _money(withdrawals),
            "net_external_flows": _money(net_flows),
            "flow_adjusted_equity": _money(adjusted),
            "trading_pnl_since_inception": _money(trading_pnl),
            "realized_pnl": _money(realized_pnl),
            "unrealized_pnl": _money(unrealized),
            "realized_pnl_basis_status": realized_pnl_basis_status,
            "time_weighted_return": _ratio(twr.value),
            "twr_subperiods": twr.subperiods,
            "twr_bounded_subperiods": twr.bounded_subperiods,
            "adjusted_equity_hwm": _money(reserve.adjusted_equity_hwm),
            "adjusted_equity_hwm_at": _instant(reserve.adjusted_equity_hwm_at),
            "observed_intraday_peak": _money(reserve.observed_intraday_peak),
            "current_drawdown_from_adjusted_hwm": _ratio(
                engine.current_drawdown(adjusted, reserve.adjusted_equity_hwm)
            ),
            "new_hwm_profit": _money(reserve.new_hwm_profit),
            "profit_reserve_accrued": _money(reserve.profit_reserve_accrued),
            "withdrawal_bucket_balance": _money(reserve.withdrawal_bucket_balance),
            "unreserved_withdrawal_total": _money(reserve.unreserved_withdrawal_total),
            "withdrawal_preview": _money(preview),
            "harvest_eligible": eligible,
            "next_harvest_checkpoint_at": _instant(engine.next_month_start(now)),
        }
    )
    return payload


def _suppressed(
    payload: dict[str, object], *, inception_at: datetime | None, now: datetime
) -> dict[str, object]:
    """Every derived figure as `null`. Nothing is estimated and nothing is stale-served."""
    for name in (
        "current_broker_equity",
        "current_cash",
        "confirmed_deposits",
        "confirmed_withdrawals",
        "net_external_flows",
        "flow_adjusted_equity",
        "trading_pnl_since_inception",
        "realized_pnl",
        "unrealized_pnl",
        "realized_pnl_basis_status",
        "time_weighted_return",
        "adjusted_equity_hwm",
        "adjusted_equity_hwm_at",
        "observed_intraday_peak",
        "current_drawdown_from_adjusted_hwm",
        "new_hwm_profit",
        "profit_reserve_accrued",
        "withdrawal_bucket_balance",
        "unreserved_withdrawal_total",
        "withdrawal_preview",
    ):
        payload[name] = None
    payload["twr_subperiods"] = 0
    payload["twr_bounded_subperiods"] = 0
    payload["harvest_eligible"] = False
    payload["next_harvest_checkpoint_at"] = (
        None if inception_at is None else _instant(engine.next_month_start(now))
    )
    return payload


__all__ = ["CONTRACT_VERSION", "LiveSafetyState", "build_summary"]
