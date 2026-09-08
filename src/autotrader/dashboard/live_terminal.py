"""GET-only read model for the real-money terminal.

This module gives the operator detail that the compact Live safety summary does
not carry: broker positions, stored intents/orders, recorded EDA-1 state, and a
structured event tape.  It owns no broker client construction, opens SQLite in
``mode=ro`` with ``query_only`` enabled, and calls only methods present on the
audited :class:`LiveReadOnlyClient` facade.

Money and quantities stay exact decimal text.  Strategy targets and risk reason
codes are quoted from the operational store.  Execution analytics are display
analytics over completed stored orders, always paired with their sample size;
they are not inputs to strategy, risk, sizing, or execution.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from autotrader.dashboard import live_safety
from autotrader.equity import EQUITY_SYMBOLS
from autotrader.live.identity import account_fingerprint

MAX_ROWS = 200
MAX_EVENTS = 120
_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_ -]?key|secret|password|token)\s*[=:]\s*\S+"
)


def _decimal_text(value: object) -> str | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return format(number, "f") if number.is_finite() else None


def _instant(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    text = str(value).strip()
    return text or None


def _safe_message(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return _SECRET_PATTERN.sub(r"\1=[REDACTED]", text)[:500]


@contextmanager
def _read_only(path: Path) -> Iterator[sqlite3.Connection]:
    uri = f"file:{path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = 1")
        yield connection
    finally:
        connection.close()


def _latest_targets(connection: sqlite3.Connection) -> list[dict[str, object]]:
    rows = connection.execute(
        "SELECT t.* FROM equity_paper_targets t "
        "JOIN (SELECT symbol, MAX(id) AS latest_id FROM equity_paper_targets "
        "GROUP BY symbol) latest ON latest.latest_id = t.id "
        "ORDER BY t.symbol"
    )
    return [
        {
            "symbol": str(row["symbol"]),
            "side": str(row["side"]),
            "target_weight": _decimal_text(row["target_weight"]),
            "target_notional": _decimal_text(row["target_notional"]),
            "target_quantity": _decimal_text(row["target_quantity"]),
            "approved_quantity": _decimal_text(row["approved_quantity"]),
            "reference_price": _decimal_text(row["reference_price"]),
            "risk_reason_code": str(row["risk_reason_code"]),
            "bar_timestamp": _instant(row["bar_timestamp"]),
            "decided_at": _instant(row["decided_at"]),
            "rollout_stage": str(row["rollout_stage"]),
            "sizing_policy": str(row["sizing_policy"]),
            "sizing_config_hash": str(row["sizing_config_hash"]),
            "client_order_id": str(row["client_order_id"]),
        }
        for row in rows
    ]


def _latest_regime(connection: sqlite3.Connection) -> dict[str, object] | None:
    row = connection.execute(
        "SELECT * FROM shadow_regime_state ORDER BY session_date DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    participate = bool(row["participate"])
    return {
        "state": "PARTICIPATE" if participate else "DEFENSIVE",
        "participate": participate,
        "session_date": str(row["session_date"]),
        "reference_symbol": str(row["reference_symbol"]),
        "reference_close": _decimal_text(row["info_close"]),
        "sma200": _decimal_text(row["info_sma"]),
        "drawdown_from_peak": _decimal_text(row["info_drawdown"]),
        "sessions_observed": int(row["sessions_observed"]),
        "sma_sessions": int(row["sma_sessions"]),
        "computed_at": _instant(row["computed_at"]),
    }


def _recent_orders(connection: sqlite3.Connection) -> list[dict[str, object]]:
    rows = connection.execute(
        "SELECT b.client_order_id, b.broker_order_id, b.symbol, b.side, b.quantity, "
        "b.filled_quantity, b.filled_average_price, b.status, b.submitted_at, "
        "b.filled_at, b.updated_at, b.created_at, i.reference_price, "
        "i.risk_reason_code, i.status AS intent_status, i.created_at AS intent_created_at "
        "FROM broker_orders b LEFT JOIN order_intents i ON i.id = b.order_intent_id "
        "ORDER BY COALESCE(b.submitted_at, b.created_at) DESC, b.id DESC LIMIT ?",
        (MAX_ROWS,),
    )
    return [
        {
            "client_order_id": str(row["client_order_id"]),
            "broker_order_id": str(row["broker_order_id"]),
            "symbol": str(row["symbol"]),
            "side": str(row["side"]),
            "quantity": _decimal_text(row["quantity"]),
            "filled_quantity": _decimal_text(row["filled_quantity"]),
            "expected_price": _decimal_text(row["reference_price"]),
            "fill_price": _decimal_text(row["filled_average_price"]),
            "status": str(row["status"]),
            "intent_status": None if row["intent_status"] is None else str(row["intent_status"]),
            "risk_reason_code": (
                None if row["risk_reason_code"] is None else str(row["risk_reason_code"])
            ),
            "intent_created_at": _instant(row["intent_created_at"]),
            "submitted_at": _instant(row["submitted_at"]),
            "filled_at": _instant(row["filled_at"]),
            "updated_at": _instant(row["updated_at"]),
        }
        for row in rows
    ]


def _as_decimal(value: object) -> Decimal | None:
    text = _decimal_text(value)
    return None if text is None else Decimal(text)


def _latency_ms(row: dict[str, object]) -> Decimal | None:
    try:
        submitted = datetime.fromisoformat(str(row["submitted_at"]))
        filled = datetime.fromisoformat(str(row["filled_at"]))
    except (TypeError, ValueError):
        return None
    latency = Decimal(str((filled - submitted).total_seconds() * 1000))
    return latency if latency >= 0 else None


def _slippage_bps(row: dict[str, object]) -> Decimal | None:
    expected = _as_decimal(row.get("expected_price"))
    filled = _as_decimal(row.get("fill_price"))
    if expected is None or filled is None or expected <= 0:
        return None
    side = str(row.get("side") or "").upper()
    if side == "BUY":
        return (filled - expected) / expected * Decimal(10_000)
    if side == "SELL":
        return (expected - filled) / expected * Decimal(10_000)
    return None


def _mean(values: list[Decimal]) -> str | None:
    if not values:
        return None
    return format(sum(values, Decimal(0)) / Decimal(len(values)), "f")


def _percentile(values: list[Decimal], percentile: Decimal) -> str | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(float(percentile * len(ordered))) - 1)
    return format(ordered[index], "f")


def _execution_metrics(
    connection: sqlite3.Connection, orders: list[dict[str, object]]
) -> dict[str, object]:
    statuses = [str(row["status"]).upper() for row in orders]
    total = len(orders)
    filled = sum(status == "FILLED" for status in statuses)
    rejected = sum(status in {"REJECTED", "FAILED"} for status in statuses)
    partial = sum(
        status != "FILLED" and (_as_decimal(row.get("filled_quantity")) or Decimal(0)) > 0
        for status, row in zip(statuses, orders, strict=True)
    )
    unknown = sum(status in {"UNKNOWN", "TIMEOUT"} for status in statuses)
    slippage = [value for row in orders if (value := _slippage_bps(row)) is not None]
    latency = [value for row in orders if (value := _latency_ms(row)) is not None]
    duplicates = connection.execute(
        "SELECT COUNT(*) FROM (SELECT client_order_id FROM order_intents "
        "GROUP BY client_order_id HAVING COUNT(*) > 1)"
    ).fetchone()[0]

    def rate(count: int) -> str | None:
        return None if total == 0 else format(Decimal(count) / Decimal(total), "f")

    return {
        "order_count": total,
        "fill_count": filled,
        "reject_count": rejected,
        "partial_fill_count": partial,
        "unknown_count": unknown,
        "fill_rate": rate(filled),
        "reject_rate": rate(rejected),
        "partial_fill_rate": rate(partial),
        "average_slippage_bps": _mean(slippage),
        "median_slippage_bps": _percentile(slippage, Decimal("0.50")),
        "p95_slippage_bps": _percentile(slippage, Decimal("0.95")),
        "slippage_sample_size": len(slippage),
        "average_fill_latency_ms": _mean(latency),
        "latency_sample_size": len(latency),
        "duplicate_client_order_ids": int(duplicates),
        "at_most_once_status": "PASS" if int(duplicates) == 0 else "BLOCKED",
    }


def _events(connection: sqlite3.Connection) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for row in connection.execute(
        "SELECT event_timestamp, event_type, message FROM system_events "
        "ORDER BY event_timestamp DESC LIMIT ?",
        (MAX_EVENTS,),
    ):
        events.append(
            {
                "timestamp": str(row["event_timestamp"]),
                "category": "SYSTEM",
                "type": str(row["event_type"]),
                "symbol": None,
                "status": None,
                "detail": _safe_message(row["message"]),
            }
        )
    for row in connection.execute(
        "SELECT event_timestamp, symbol, decision, reason_code FROM risk_events "
        "ORDER BY event_timestamp DESC LIMIT ?",
        (MAX_EVENTS,),
    ):
        events.append(
            {
                "timestamp": str(row["event_timestamp"]),
                "category": "RISK",
                "type": str(row["reason_code"]),
                "symbol": None if row["symbol"] is None else str(row["symbol"]),
                "status": str(row["decision"]),
                "detail": None,
            }
        )
    for row in connection.execute(
        "SELECT created_at, symbol, side, status, risk_reason_code, client_order_id "
        "FROM order_intents ORDER BY created_at DESC LIMIT ?",
        (MAX_EVENTS,),
    ):
        events.append(
            {
                "timestamp": str(row["created_at"]),
                "category": "ORDER",
                "type": "INTENT",
                "symbol": str(row["symbol"]),
                "status": str(row["status"]),
                "detail": (
                    f"{str(row['side'])} · {str(row['risk_reason_code'])} · "
                    f"{str(row['client_order_id'])}"
                ),
            }
        )
    events.sort(key=lambda item: str(item["timestamp"]), reverse=True)
    return events[:MAX_EVENTS]


def _operational(path: Path) -> dict[str, object]:
    if not path.exists():
        return {
            "status": "NOT_CONFIGURED",
            "strategy": None,
            "targets": [],
            "orders": [],
            "execution": _empty_execution(),
            "events": [],
        }
    try:
        with _read_only(path) as connection:
            targets = _latest_targets(connection)
            orders = _recent_orders(connection)
            regime = _latest_regime(connection)
            latest_run = connection.execute(
                "SELECT * FROM strategy_runs ORDER BY started_at DESC, id DESC LIMIT 1"
            ).fetchone()
            return {
                "status": "CLEAN",
                "strategy": {
                    "name": "EDA-1",
                    "universe": list(EQUITY_SYMBOLS),
                    "regime": regime,
                    "last_run": (
                        None
                        if latest_run is None
                        else {
                            "status": str(latest_run["status"]),
                            "mode": str(latest_run["mode"]),
                            "started_at": _instant(latest_run["started_at"]),
                            "ended_at": _instant(latest_run["ended_at"]),
                        }
                    ),
                    "next_cycle_at": None,
                    "rollout_stage": (
                        None if not targets else str(targets[0]["rollout_stage"])
                    ),
                    "sizing_policy": (
                        None if not targets else str(targets[0]["sizing_policy"])
                    ),
                },
                "targets": targets,
                "orders": orders,
                "execution": _execution_metrics(connection, orders),
                "events": _events(connection),
            }
    except Exception:  # noqa: BLE001 - browser gets a state, never an exception string
        return {
            "status": "UNREADABLE",
            "strategy": None,
            "targets": [],
            "orders": [],
            "execution": _empty_execution(),
            "events": [],
        }


def _empty_execution() -> dict[str, object]:
    return {
        "order_count": 0,
        "fill_count": 0,
        "reject_count": 0,
        "partial_fill_count": 0,
        "unknown_count": 0,
        "fill_rate": None,
        "reject_rate": None,
        "partial_fill_rate": None,
        "average_slippage_bps": None,
        "median_slippage_bps": None,
        "p95_slippage_bps": None,
        "slippage_sample_size": 0,
        "average_fill_latency_ms": None,
        "latency_sample_size": 0,
        "duplicate_client_order_ids": 0,
        "at_most_once_status": "UNKNOWN",
    }


def _broker_positions(
    broker: object | None,
    *,
    expected_fingerprint: str | None,
    targets: list[dict[str, object]],
    orders: list[dict[str, object]],
) -> dict[str, object]:
    if broker is None:
        return {
            "status": "NOT_CONFIGURED",
            "as_of": None,
            "largest_position": None,
            "rows": [],
        }
    try:
        account = broker.get_account()  # type: ignore[attr-defined]
        raw_number = str(getattr(account, "account_number", "") or "")
        observed = account_fingerprint(raw_number) if raw_number else None
        if expected_fingerprint is None or observed != expected_fingerprint:
            return {
                "status": "ACCOUNT_MISMATCH",
                "as_of": None,
                "largest_position": None,
                "rows": [],
            }
        equity = _as_decimal(getattr(account, "equity", None))
        raw_positions = tuple(broker.get_all_positions() or ())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - broker error detail may contain a credential fragment
        return {
            "status": "BROKER_UNAVAILABLE",
            "as_of": None,
            "largest_position": None,
            "rows": [],
        }

    target_by_symbol = {str(row["symbol"]): row for row in targets}
    fill_by_symbol: dict[str, dict[str, object]] = {}
    for order in orders:
        symbol = str(order["symbol"])
        if symbol not in fill_by_symbol and order.get("filled_at"):
            fill_by_symbol[symbol] = order
    rows: list[dict[str, object]] = []
    for position in raw_positions:
        symbol = str(getattr(position, "symbol", "") or "").upper()
        if not symbol:
            continue
        market_value = _as_decimal(getattr(position, "market_value", None))
        actual_weight = (
            market_value / equity
            if market_value is not None and equity is not None and equity > 0
            else None
        )
        target = target_by_symbol.get(symbol)
        target_weight = _as_decimal(None if target is None else target.get("target_weight"))
        drift = (
            actual_weight - target_weight
            if actual_weight is not None and target_weight is not None
            else None
        )
        last_fill = fill_by_symbol.get(symbol)
        rows.append(
            {
                "symbol": symbol,
                "quantity": _decimal_text(getattr(position, "qty", None)),
                "average_cost": _decimal_text(getattr(position, "avg_entry_price", None)),
                "price": _decimal_text(getattr(position, "current_price", None)),
                "market_value": _decimal_text(market_value),
                "actual_weight": _decimal_text(actual_weight),
                "target_weight": _decimal_text(target_weight),
                "drift": _decimal_text(drift),
                "unrealized_pnl": _decimal_text(getattr(position, "unrealized_pl", None)),
                "unrealized_pnl_fraction": _decimal_text(
                    getattr(position, "unrealized_plpc", None)
                ),
                "day_pnl": None,
                "day_change_fraction": _decimal_text(getattr(position, "change_today", None)),
                "stance": None if target is None else str(target["side"]),
                "last_decision_at": None if target is None else target["decided_at"],
                "last_fill_at": None if last_fill is None else last_fill["filled_at"],
            }
        )
    rows.sort(key=lambda item: str(item["symbol"]))
    valued_positions = [
        (abs(value), item)
        for item in rows
        if (value := _as_decimal(item.get("market_value"))) is not None
    ]
    largest_position = max(
        valued_positions,
        key=lambda candidate: candidate[0],
        default=(None, None),
    )[1]
    return {
        "status": "CLEAN",
        "as_of": datetime.now(UTC).isoformat(),
        "largest_position": largest_position,
        "rows": rows,
    }


def build_terminal(
    *,
    now: datetime | None = None,
    path: Path | None = None,
    broker: object | None = None,
    expected_fingerprint: str | None = None,
) -> dict[str, object]:
    """Build one sanitized, read-only terminal snapshot."""
    moment = now or datetime.now(UTC)
    operational = _operational(path or Path("data/equity-live.db"))
    targets = list(operational["targets"])  # type: ignore[arg-type]
    orders = list(operational["orders"])  # type: ignore[arg-type]
    resolved_broker = broker if broker is not None else live_safety.create_live_read_broker()
    expected = (
        expected_fingerprint
        if expected_fingerprint is not None
        else live_safety.configured_fingerprint()
    )
    positions = _broker_positions(
        resolved_broker,
        expected_fingerprint=expected,
        targets=targets,
        orders=orders,
    )
    return {
        "generated_at": moment.astimezone(UTC).isoformat(),
        "environment": "LIVE",
        "read_only": True,
        "operational_status": operational["status"],
        "positions": positions,
        "strategy": operational["strategy"],
        "targets": targets,
        "orders": orders,
        "execution": operational["execution"],
        "events": operational["events"],
    }
__all__ = ["build_terminal"]
