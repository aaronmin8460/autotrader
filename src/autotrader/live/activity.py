"""Read and normalize non-trade cash activity for the Live entry guard.

This module has no client factory and no broker mutation. A caller supplies an
allowlisted reader, and the result is Prompt 1's existing ``CashFlowEvent``.
Malformed or unreadable activity fails closed: an unknown day is not a clean
day.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from autotrader.live.budget import CASH_FLOW_ACTIVITY_TYPES, CashFlowEvent

ActivityReader = Callable[[str, datetime | None], Sequence[Any]]


class LiveActivityError(Exception):
    """The non-trade cash record could not be established."""


def _field(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _moment(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value or "").strip()
    if not text:
        raise LiveActivityError("an account activity has no transaction time")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise LiveActivityError("an account activity has an unreadable transaction time") from None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _amount(value: object) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise LiveActivityError("an account activity has an unreadable amount") from None
    if not amount.is_finite():
        raise LiveActivityError("an account activity has a non-finite amount")
    return amount


def read_cash_flow_events(
    reader: ActivityReader,
    *,
    after: datetime | None,
) -> tuple[CashFlowEvent, ...]:
    """Read the three conservative guard types and return validated events."""
    events: list[CashFlowEvent] = []
    seen: set[str] = set()
    for activity_type in sorted(CASH_FLOW_ACTIVITY_TYPES):
        try:
            rows = reader(activity_type, after)
        except Exception as error:  # noqa: BLE001 - an unreadable feed fails closed
            raise LiveActivityError(
                f"the {activity_type} activity record could not be read ({type(error).__name__})"
            ) from None
        for row in rows:
            identity = str(_field(row, "id") or _field(row, "activity_id") or "").strip()
            if not identity:
                raise LiveActivityError("an account activity has no stable identity")
            if identity in seen:
                continue
            seen.add(identity)
            kind = str(_field(row, "activity_type") or activity_type).strip().upper()
            if kind not in CASH_FLOW_ACTIVITY_TYPES:
                raise LiveActivityError("an account activity returned under the wrong type")
            raw_amount = _field(row, "net_amount")
            if raw_amount is None:
                raw_amount = _field(row, "amount")
            events.append(
                CashFlowEvent(
                    activity_id=identity,
                    activity_type=kind,
                    amount=_amount(raw_amount),
                    transaction_time=_moment(
                        _field(row, "transaction_time")
                        or _field(row, "created_at")
                        or _field(row, "date")
                    ),
                )
            )
    return tuple(sorted(events, key=lambda event: (event.transaction_time, event.activity_id)))


__all__ = ["ActivityReader", "LiveActivityError", "read_cash_flow_events"]
