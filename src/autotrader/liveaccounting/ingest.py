"""Importing external cash flows from the broker. Reads only, and imports once.

    broker activities -> classify -> external_cash_flows -> engine -> dashboard

and never the other direction. This module opens the live accounting ledger for
writing and nothing else. It has no order client, no transfer call and no
submission path, and the names of those methods do not appear here, so nothing
typed in this file can be asked to move money.

**Overlap, not a high-water cursor.** Each run re-reads a window that extends
back before the newest flow already stored and lets the UNIQUE constraint on
`broker_activity_id` discard what it has seen. A cursor that asked only for
strictly-newer activities would silently lose a transfer that the broker
published late - and a hole in a capital ledger is not something anyone
notices afterwards.

**A late flow marks a rebuild rather than being quietly absorbed.** If an
activity arrives whose settle time is earlier than something already stored,
the derived state was computed without it and is now wrong. The row is stored,
the ledger is flagged `REBUILD_REQUIRED`, and the payload stops publishing
figures until a replay has run. `engine` recomputes from scratch, so the replay
puts the late flow in its correct place in time by construction - but the flag
is what stops a wrong number being served in the meantime.

**Query the specific types, never the group.** This broker publishes `TRANS` as
a *group alias* that returns the same activity rows, with the same ids, as
`CSD` and `CSW`. Reading both would present every flow twice; the UNIQUE
constraint would catch it, but relying on a constraint to undo an avoidable
mistake is not a design.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from autotrader.liveaccounting import classify, store
from autotrader.liveaccounting.models import (
    CONFIRMATION_CONFIRMED,
    FLOW_EXTERNAL_DEPOSIT,
    FLOW_EXTERNAL_WITHDRAWAL,
    FLOW_UNKNOWN_EXTERNAL,
    RELATION_PRE_INCEPTION,
    RELATION_SINCE_INCEPTION,
)

_ZERO_AMOUNT = Decimal("0")

#: How far back before the newest stored flow each run re-reads. Wide, because
#: a capital ledger that missed a deposit is worse than a redundant request.
DEFAULT_OVERLAP = timedelta(days=7)

#: The activity types worth asking for.
#:
#: Deliberately the specific types and not the `TRANS` group alias - see the
#: module docstring - and deliberately **only types this broker accepts**. Every
#: name here was requested read-only against the live account and answered; the
#: API rejects an unknown type with `40010001 invalid activity type` and a
#: rejected request aborts the whole import, which is the correct direction
#: (better a loud stop than a quietly missed deposit) but a poor reason to stop.
INGESTED_ACTIVITY_TYPES: tuple[str, ...] = (
    classify.ACTIVITY_CASH_DEPOSIT,
    classify.ACTIVITY_CASH_WITHDRAWAL,
    *sorted(classify.AMBIGUOUS_TYPES),
)

#: Reads activities of one type since an instant. Injected so this module needs
#: no broker SDK to be imported, and so the suite never needs a network.
ActivityReader = Callable[[str, datetime | None], Sequence[Any]]


@dataclass(frozen=True)
class IngestResult:
    activities_seen: int
    imported: int
    duplicates_skipped: int
    unknown_classified: int
    pending_seen: int
    pre_inception: int
    backdated: int
    rebuild_marked: bool
    broker_requests: int


def source_digest(payload: object) -> str:
    """A stable digest of the raw activity, stored instead of the raw activity.

    An activity description carries a transfer reference and a funding-source
    description. None of that needs to reach a dashboard for the row to be
    auditable: a digest proves the stored row still describes the activity it
    was imported from, which is the property an audit actually wants.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _field(activity: Any, *names: str) -> Any:
    for name in names:
        if isinstance(activity, dict):
            if name in activity:
                return activity[name]
        elif hasattr(activity, name):
            return getattr(activity, name)
    return None


def _settle_time(activity: Any) -> datetime | None:
    """When the broker says the money moved.

    `date` is the settlement date the broker assigns; `created_at` carries the
    instant. The instant is preferred when both are present because a same-day
    ordering question - did this deposit settle before or after the close
    checkpoint? - can only be answered by one of them.
    """
    raw = _field(activity, "created_at", "transaction_time", "date")
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    text = str(raw).strip()
    if not text:
        return None
    try:
        if len(text) == 10:  # a bare YYYY-MM-DD settles at the start of its UTC day
            return datetime.fromisoformat(text).replace(tzinfo=UTC)
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def import_activities(
    connection: sqlite3.Connection,
    reader: ActivityReader,
    *,
    account_fingerprint: str,
    now: datetime,
    types: Sequence[str] = INGESTED_ACTIVITY_TYPES,
    overlap: timedelta = DEFAULT_OVERLAP,
) -> IngestResult:
    """Read the activity feed and record what it says. Never mutates the broker."""
    store.require_account(connection, account_fingerprint)
    state = store.read_state(connection)
    inception_at = None if state is None else datetime.fromisoformat(str(state["inception_at"]))

    newest = connection.execute(
        "SELECT MAX(settle_at) AS newest FROM external_cash_flows"
    ).fetchone()
    since = None
    newest_stored: datetime | None = None
    if newest is not None and newest["newest"]:
        newest_stored = datetime.fromisoformat(str(newest["newest"]))
        since = newest_stored - overlap

    seen = imported = duplicates = unknown = pending = pre_inception = backdated = 0
    requests = 0
    for activity_type in types:
        activities = reader(activity_type, since)
        requests += 1
        for activity in activities:
            seen += 1
            activity_id = str(_field(activity, "id", "activity_id") or "").strip()
            settle_at = _settle_time(activity)
            if not activity_id or settle_at is None:
                # No identity or no time means no idempotency and no ordering.
                # Refusing the row is the only safe move; it is reported as an
                # unknown so the payload stops publishing.
                unknown += 1
                continue

            raw_status = _field(activity, "status")
            amount = classify.parse_amount(_field(activity, "net_amount", "amount"))
            classification, reason = classify.classify(
                _field(activity, "activity_type") or activity_type, amount
            )
            confirmation = classify.confirmation_of(raw_status)
            if classification == FLOW_UNKNOWN_EXTERNAL:
                unknown += 1
            if confirmation != CONFIRMATION_CONFIRMED:
                pending += 1

            relation = RELATION_SINCE_INCEPTION
            if inception_at is not None and settle_at <= inception_at:
                relation = RELATION_PRE_INCEPTION
                pre_inception += 1

            is_backdated = newest_stored is not None and settle_at < newest_stored
            stored = store.record_flow(
                connection,
                broker_activity_id=activity_id,
                account_fingerprint=account_fingerprint,
                activity_type=str(_field(activity, "activity_type") or activity_type),
                broker_status=str(raw_status or "unknown"),
                classification=classification,
                classification_reason=reason,
                confirmation=confirmation,
                amount=amount if amount is not None else _ZERO_AMOUNT,
                currency=str(_field(activity, "currency") or "USD"),
                settle_at=settle_at,
                relation=relation,
                source_digest=source_digest(
                    activity if isinstance(activity, dict) else str(activity)
                ),
                now=now,
                backdated=is_backdated,
            )
            if not stored:
                duplicates += 1
                continue
            imported += 1
            if is_backdated and classification in (
                FLOW_EXTERNAL_DEPOSIT,
                FLOW_EXTERNAL_WITHDRAWAL,
            ):
                backdated += 1

    rebuild_marked = False
    if backdated and state is not None:
        store.mark_rebuild_required(
            connection,
            reason=(
                f"{backdated} confirmed external flow(s) arrived with a settle time earlier "
                "than the ledger's newest row, so every derived figure was computed without "
                "them."
            ),
            now=now,
        )
        rebuild_marked = True

    return IngestResult(
        activities_seen=seen,
        imported=imported,
        duplicates_skipped=duplicates,
        unknown_classified=unknown,
        pending_seen=pending,
        pre_inception=pre_inception,
        backdated=backdated,
        rebuild_marked=rebuild_marked,
        broker_requests=requests,
    )


__all__ = [
    "DEFAULT_OVERLAP",
    "INGESTED_ACTIVITY_TYPES",
    "ActivityReader",
    "IngestResult",
    "import_activities",
    "source_digest",
]
