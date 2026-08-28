"""Ask the broker what became of exactly one order, and report only that.

Three rules hold this module together, and each of them exists because the
opposite reading has caused a real duplicate order somewhere:

**Submitted is not filled.** A broker snapshot proves an order was accepted.
The filled quantity is a separate number and it is reported separately; nothing
here derives one from the other, and no status string is translated into a
fill.

**Filled is not a position.** Fees on a crypto BUY come out of the base asset,
so a fill of `0.00016705` BTC can settle as a position of `0.000166632` BTC.
Both numbers are printed side by side and the broker's position is labelled as
the authoritative one, because the next step after this command is a cleanup
sized from it.

**A failed lookup is not an absent order.** When the broker cannot be asked,
this reports `ORDER_TRUTH_UNRESOLVED` and prints `DO NOT RETRY ORIGINAL ORDER`.
It does not guess, it does not retry the lookup in a loop hoping for a better
answer, and it never suggests re-sending the original order - the one action
that turns an unknown outcome into a certain duplicate.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from autotrader.execution.models import format_quantity
from autotrader.smoke import broker
from autotrader.smoke.broker import LookupOutcome
from autotrader.smoke.models import (
    DO_NOT_RETRY_BANNER,
    ORDER_TRUTH_UNRESOLVED,
    BrokerReadClient,
    BrokerUnreadableError,
    OrderReport,
    PositionSnapshot,
)
from autotrader.smoke.readonly import normalize_smoke_symbol
from autotrader.smoke.tracking import find_broker_snapshot, find_intent


@dataclass(frozen=True)
class InspectionResult:
    """One order lookup: what the broker said, and what local state believes.

    Both are carried so a divergence is visible. A local snapshot that says
    `accepted` next to a broker that says `filled` is normal between
    reconciliation passes and is not an error; the same pair the other way
    round is worth an operator's attention, and neither is detectable from one
    source alone.
    """

    outcome: LookupOutcome
    identifier: str
    report: OrderReport | None
    detail: str
    local_intent_status: str | None = None
    local_snapshot_status: str | None = None
    position_detail: str | None = None

    @property
    def unresolved(self) -> bool:
        """Whether the broker could not be asked. The one blocking answer."""
        return self.outcome is LookupOutcome.UNRESOLVED

    @property
    def verdict_text(self) -> str:
        """The single line a script or an operator should read first."""
        if self.unresolved:
            return ORDER_TRUTH_UNRESOLVED
        if self.outcome is LookupOutcome.NOT_FOUND:
            return "ORDER_NOT_FOUND_AT_BROKER"
        return "ORDER_FOUND"

    def banners(self) -> tuple[str, ...]:
        """Lines that must be printed prominently, if any.

        Only the unresolved case produces one. An operator who has just been
        told this system does not know whether an order exists needs the "do
        not retry" instruction next to that fact, not three paragraphs below
        it.
        """
        return (DO_NOT_RETRY_BANNER,) if self.unresolved else ()


def inspect_order(
    client: BrokerReadClient,
    *,
    client_order_id: str | None = None,
    broker_order_id: str | None = None,
    connection: sqlite3.Connection | None = None,
) -> InspectionResult:
    """Look one order up at the broker and describe it. Reads only.

    Exactly one identifier, enforced by `broker.read_order`. `connection`, when
    supplied, is the read-only database handle used to show what local state
    believes alongside what the broker says; it is optional because the broker
    answer is the one that matters and must be obtainable without a database.

    The broker's position in the order's symbol is fetched separately and
    attached to the report. If that read fails the order report still stands -
    it is the position line that goes missing, and it says so.
    """
    identifier = (client_order_id or broker_order_id or "").strip()
    outcome, snapshot, detail = broker.read_order(
        client, client_order_id=client_order_id, broker_order_id=broker_order_id
    )

    if outcome is LookupOutcome.UNRESOLVED:
        return InspectionResult(
            outcome=outcome,
            identifier=identifier,
            report=None,
            detail=(
                f"The broker could not be asked about {identifier}: {detail}. This is "
                "not evidence that the order is absent - it may exist and it may be "
                "working. Resolve it by re-reading the broker (this command, again, "
                "later) or by running `autotrader reconcile`, which asks about the "
                "same client_order_id and never sends a second order."
            ),
        )

    if outcome is LookupOutcome.NOT_FOUND:
        return InspectionResult(
            outcome=outcome,
            identifier=identifier,
            report=None,
            detail=(
                f"The broker reports no order under {identifier}. This is the broker's "
                "own definitive answer, not a failed lookup."
            ),
            **_local_context(connection, client_order_id),
        )

    assert snapshot is not None  # noqa: S101 - FOUND always carries a snapshot
    position, position_detail = _position_for_order(client, snapshot.symbol)
    report = OrderReport(
        broker_order_id=snapshot.broker_order_id,
        client_order_id=snapshot.client_order_id,
        symbol=snapshot.symbol,
        side=snapshot.side,
        status=snapshot.status,
        quantity=snapshot.quantity,
        filled_quantity=snapshot.filled_quantity,
        filled_average_price=snapshot.filled_average_price,
        submitted_at=snapshot.submitted_at,
        filled_at=snapshot.filled_at,
        broker_updated_at=snapshot.broker_updated_at,
        broker_position=position,
    )
    return InspectionResult(
        outcome=outcome,
        identifier=identifier,
        report=report,
        detail=detail,
        position_detail=position_detail,
        **_local_context(connection, snapshot.client_order_id),
    )


def _position_for_order(
    client: BrokerReadClient, symbol: str
) -> tuple[PositionSnapshot | None, str | None]:
    """The broker's current position in `symbol`, or a note saying why not.

    A failed position read does not invalidate the order report. It does mean
    the number a cleanup would be sized from is missing, and the caller says so
    rather than printing a blank.
    """
    try:
        positions = broker.read_positions(client)
    except BrokerUnreadableError as error:
        return None, (
            f"The broker position for {symbol} could not be read ({error}). Do not size "
            "a cleanup from the filled quantity above - re-run this command until the "
            "position reads cleanly."
        )
    return broker.position_for(positions, normalize_smoke_symbol(symbol)), None


def _local_context(
    connection: sqlite3.Connection | None, client_order_id: str | None
) -> dict[str, str | None]:
    """What the local database believes about this order, if anything."""
    if connection is None or not client_order_id:
        return {"local_intent_status": None, "local_snapshot_status": None}
    intent = find_intent(connection, client_order_id)
    snapshot = find_broker_snapshot(connection, client_order_id)
    return {
        "local_intent_status": intent.status if intent is not None else None,
        "local_snapshot_status": snapshot.status if snapshot is not None else None,
    }


def fill_versus_position_note(report: OrderReport) -> str | None:
    """One line reconciling this order's fill against the position it left behind.

    The number an operator would naturally reach for next is the filled
    quantity, and on this system it is usually the wrong one. What that means
    depends on the side, so this says four different things rather than one
    vague one - in particular it does not blame the taker fee for a position
    that is simply zero because a later order already closed it.
    """
    position = report.broker_position
    if position is None or report.filled_quantity <= 0:
        return None

    held = format_quantity(position.quantity)
    filled = format_quantity(report.filled_quantity)
    symbol = report.symbol

    if report.side.strip().upper() == "SELL":
        if position.quantity == 0:
            return (
                f"This SELL filled {filled} and the broker now reports no {symbol} "
                "position. Exposure in this symbol is closed."
            )
        return (
            f"This SELL filled {filled}, and the broker still reports a {symbol} "
            f"position of {held}. That remainder is residual exposure - plan any "
            "further cleanup from the POSITION, never from the filled quantity."
        )

    if position.quantity == 0:
        return (
            f"This BUY filled {filled}, but the broker reports no {symbol} position. "
            "Either a later order already closed it or the account never held it. Do "
            "not read the filled quantity as exposure, and do not size a cleanup from "
            "it - there is nothing to close."
        )

    difference: Decimal = report.filled_quantity - position.quantity
    if difference > 0:
        return (
            f"The broker position ({held}) is smaller than the filled quantity "
            f"({filled}) by {format_quantity(difference)}. On Alpaca crypto the taker "
            "fee is taken out of the base asset, so this is expected. Size any cleanup "
            "from the POSITION, never from the fill."
        )
    if difference < 0:
        return (
            f"The broker position ({held}) is larger than this order's filled quantity "
            f"({filled}). That is normal if the account already held some of this "
            "asset; confirm the baseline before treating the whole position as this "
            "smoke's exposure."
        )
    return None


__all__ = ["InspectionResult", "fill_versus_position_note", "inspect_order"]
