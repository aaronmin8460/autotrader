"""Historical Live accounting read model for terminal charts.

The frozen summary contract stays untouched.  This companion view reads the
same authoritative checkpoints and classified flows, and delegates every
accounting formula to :mod:`autotrader.liveaccounting.engine`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from autotrader.dashboard import live_accounting
from autotrader.liveaccounting import engine, readmodel, store
from autotrader.liveaccounting.models import SUPPRESSING_STATUSES, ProfitReservePolicy


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    from autotrader.liveaccounting.models import floor_cent

    return format(floor_cent(value), "f")


def _ratio(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _unavailable(now: datetime, status: str) -> dict[str, object]:
    return {
        "generated_at": now.astimezone(UTC).isoformat(),
        "environment": "LIVE",
        "read_only": True,
        "status": status,
        "sample_size": 0,
        "today_trading_pnl": None,
        "points": [],
        "flows": [],
    }


def build_history(
    *, now: datetime | None = None, path: Path | None = None
) -> dict[str, object]:
    """Build exact checkpoint history, or an explicit unavailable state."""
    moment = now or datetime.now(UTC)
    target = path or live_accounting.database_path()
    if not target.exists():
        return _unavailable(moment, "NOT_INITIALIZED")
    try:
        with store.connect_read_only(target) as connection:
            policy = ProfitReservePolicy()
            summary = readmodel.build_summary(
                connection,
                now=moment,
                expected_fingerprint=live_accounting.expected_fingerprint(),
                policy=policy,
                safety=live_accounting.read_safety_state(),
            )
            checkpoints = store.read_checkpoints(connection)
            flows = store.read_flows(connection)
            state = store.read_state(connection)
            rows = list(
                connection.execute(
                    "SELECT checkpoint_id, taken_at, utc_date, kind, position_count "
                    "FROM performance_checkpoints ORDER BY taken_at, checkpoint_id"
                )
            )
    except Exception:  # noqa: BLE001 - never send filesystem or exception detail
        return _unavailable(moment, "UNREADABLE")

    status = str(summary["accounting_status"])
    suppress = status in SUPPRESSING_STATUSES or state is None
    baseline = None if state is None else store.text_decimal(state["baseline_equity"])
    points: list[dict[str, object]] = []
    adjusted_by_day: dict[str, Decimal] = {}

    for index, checkpoint in enumerate(checkpoints):
        derived = not suppress and baseline is not None
        adjusted: Decimal | None = None
        trading_pnl: Decimal | None = None
        twr_value: Decimal | None = None
        hwm: Decimal | None = None
        drawdown: Decimal | None = None
        if derived:
            prefix = checkpoints[: index + 1]
            net_flows = engine.net_external_flows_at(flows, checkpoint.taken_at)
            adjusted = engine.flow_adjusted_equity(checkpoint.broker_equity, net_flows)
            trading_pnl = adjusted - baseline
            twr_value = engine.time_weighted_return(prefix, flows).value
            reserve = engine.evaluate_reserve(
                baseline_equity=baseline,
                checkpoints=prefix,
                flows=flows,
                policy=policy,
            )
            hwm = reserve.adjusted_equity_hwm
            drawdown = engine.current_drawdown(adjusted, hwm)
            adjusted_by_day.setdefault(checkpoint.taken_at.date().isoformat(), adjusted)
        row = rows[index]
        points.append(
            {
                "checkpoint_id": int(row["checkpoint_id"]),
                "taken_at": checkpoint.taken_at.astimezone(UTC).isoformat(),
                "utc_date": str(row["utc_date"]),
                "kind": checkpoint.kind,
                "broker_equity": _money(checkpoint.broker_equity),
                "broker_cash": _money(checkpoint.broker_cash),
                "unrealized_pnl": _money(checkpoint.unrealized_pnl),
                "position_count": (
                    None if row["position_count"] is None else int(row["position_count"])
                ),
                "flow_adjusted_equity": _money(adjusted),
                "trading_pnl": _money(trading_pnl),
                "time_weighted_return": _ratio(twr_value),
                "adjusted_equity_hwm": _money(hwm),
                "drawdown": _ratio(drawdown),
            }
        )

    today_pnl: Decimal | None = None
    if points and not suppress:
        latest_day = str(points[-1]["utc_date"])
        opening = adjusted_by_day.get(latest_day)
        latest_adjusted = engine.flow_adjusted_equity(
            checkpoints[-1].broker_equity,
            engine.net_external_flows_at(flows, checkpoints[-1].taken_at),
        )
        if opening is not None:
            today_pnl = latest_adjusted - opening

    flow_rows = [
        {
            "settle_at": flow.settle_at.astimezone(UTC).isoformat(),
            "activity_type": flow.activity_type,
            "classification": flow.classification,
            "confirmation": flow.confirmation,
            "amount": _money(flow.amount),
            "currency": flow.currency,
            "relation": flow.relation,
        }
        for flow in flows
    ]
    return {
        "generated_at": moment.astimezone(UTC).isoformat(),
        "environment": "LIVE",
        "read_only": True,
        "status": status,
        "sample_size": len(points),
        "today_trading_pnl": _money(today_pnl),
        "points": points,
        "flows": flow_rows,
    }


__all__ = ["build_history"]
