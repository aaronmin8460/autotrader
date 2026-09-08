"""Deciding what a broker activity actually was. Conservative in one direction only.

Getting this wrong in the permissive direction is the single most damaging
mistake this program could make. If a dividend were classified as an external
deposit it would be subtracted out of performance and the account would appear
never to earn it; if a deposit were classified as investment return the account
would appear to have doubled overnight. Both are silent, both are plausible in
a screenshot, and neither shows up as an error.

So classification is a **whitelist in three parts**, and everything outside it
fails closed:

* two types mean external capital moved, and nothing else does;
* a named list of types are recognised as account return or account cost, and
  are recorded as such so they stay inside performance where they belong;
* **anything else at all** - a type this build has never seen, a type whose
  sign contradicts its name, a journal entry - is `UNKNOWN_EXTERNAL_FLOW`,
  which changes no figure and suppresses the whole derived payload until a
  person resolves it.

The last one is the load-bearing clause. A new activity type appearing in this
account is not a reason to guess; it is a reason to stop publishing numbers
that might now be wrong.

**Journal entries are deliberately not classified.** `JNLC` moves cash into or
out of an account without saying why: it is used for transfers between two
accounts under one owner, for promotional credits, for corrections and for
administrative adjustments. Some of those are external capital and some are
account return, and the activity record does not distinguish them. So `JNLC` is
`UNKNOWN_EXTERNAL_FLOW` - not because the type is unrecognised, but because it
is recognised as ambiguous.

Note the deliberate difference from `autotrader.live.budget`. That module's
`CASH_FLOW_ACTIVITY_TYPES` is *broader* - `CSD`, `CSW` and `JNLC` together -
because it answers a safety question ("might today's equity change be something
other than trading?") where over-inclusion costs one quiet day and
under-inclusion costs a wrong halt baseline. This module answers an accounting
question, where over-inclusion silently corrupts performance. Two questions,
two vocabularies, and neither is the other's bug.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from autotrader.liveaccounting.models import (
    CONFIRMATION_CONFIRMED,
    CONFIRMATION_PENDING,
    CONFIRMATION_VOID,
    FLOW_EXTERNAL_DEPOSIT,
    FLOW_EXTERNAL_WITHDRAWAL,
    FLOW_NON_EXTERNAL,
    FLOW_UNKNOWN_EXTERNAL,
    ZERO,
)

#: A cash deposit. Explicit funding from the owner's bank.
ACTIVITY_CASH_DEPOSIT = "CSD"

#: A cash withdrawal. Explicit disbursement to the owner's bank.
ACTIVITY_CASH_WITHDRAWAL = "CSW"

#: The only two types that may ever mean external capital moved.
EXTERNAL_CAPITAL_TYPES: frozenset[str] = frozenset(
    {ACTIVITY_CASH_DEPOSIT, ACTIVITY_CASH_WITHDRAWAL}
)

#: Recognised, and recognised as belonging INSIDE performance.
#:
#: Every name here was **verified against this broker's own API**, not copied
#: from documentation: each one was requested read-only and the ones it rejected
#: as invalid activity types were removed. A list that named types this broker
#: does not publish would be a list nobody had checked, and its errors would sit
#: in the safe-looking direction - a type wrongly listed here is a type that
#: silently would NOT fail closed.
#:
#: Dividends and interest are what the invested capital earned. Fees and taxes
#: are what holding and trading it cost. Trade fills are the trading itself.
#: Corporate actions restate a holding without the owner contributing anything.
#: None of them is the owner adding or removing capital, so none may be netted
#: out of the return - a system that subtracted its own dividends would report
#: an account that never earned any.
NON_EXTERNAL_TYPES: frozenset[str] = frozenset(
    {
        # trading
        "FILL",
        "PTC",  # pass-thru charge on a fill
        # dividends, in all the flavours the broker distinguishes
        "DIV",
        "DIVCGL",
        "DIVCGS",
        "DIVNRA",
        "DIVROC",
        "DIVTXEX",
        "DIVFT",
        "DIVTW",
        # interest
        "INT",
        "INTNRA",
        "INTTW",
        # costs and taxes
        "FEE",
        "CFEE",
        "PTR",
        "NC",
        "WH",
        # corporate actions and share-level journals: no cash contributed
        "MA",
        "REORG",
        "SPIN",
        "SPLIT",
        "SC",
        "JNLS",
        # options lifecycle: not capital the owner contributed
        "OPASN",
        "OPEXP",
    }
)

#: Recognised as *ambiguous*, which is not the same as unrecognised. A journal
#: of cash may be external capital or may be an administrative adjustment, and
#: the activity record does not say which.
AMBIGUOUS_TYPES: frozenset[str] = frozenset({"JNLC", "ACATC", "ACATS", "CSR"})

#: The broker's word for "this settled". Anything else has no effect yet.
STATUS_EXECUTED = "executed"

#: Cancelled or rejected: it never happened and never will.
VOID_STATUSES: frozenset[str] = frozenset({"canceled", "cancelled", "rejected", "voided"})


def confirmation_of(raw_status: object) -> str:
    """CONFIRMED, PENDING or VOID, from the broker's own status word.

    A missing or unrecognised status is `PENDING`, never `CONFIRMED`. Pending
    money has zero accounting effect: an announced ACH, a transfer in flight,
    and an operator saying "another fifty arrives tomorrow" are all the same
    thing here, which is nothing.
    """
    text = str(raw_status or "").strip().lower()
    if text == STATUS_EXECUTED:
        return CONFIRMATION_CONFIRMED
    if text in VOID_STATUSES:
        return CONFIRMATION_VOID
    return CONFIRMATION_PENDING


def parse_amount(raw_amount: object) -> Decimal | None:
    """The signed amount as an exact Decimal, or None when it cannot be read.

    An unreadable amount is not zero. Returning None sends the activity to
    `UNKNOWN_EXTERNAL_FLOW`, which is the honest answer: something moved and
    this build cannot say how much.
    """
    if raw_amount is None:
        return None
    try:
        value = Decimal(str(raw_amount).strip())
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def classify(activity_type: object, amount: Decimal | None) -> tuple[str, str | None]:
    """Classify one activity. Returns `(classification, reason_when_unknown)`.

    `amount` is signed as the broker reports it: positive money in, negative
    money out.
    """
    kind = str(activity_type or "").strip().upper()
    if not kind:
        return FLOW_UNKNOWN_EXTERNAL, "the activity carries no type"

    if kind in NON_EXTERNAL_TYPES:
        return FLOW_NON_EXTERNAL, None

    if kind in AMBIGUOUS_TYPES:
        return (
            FLOW_UNKNOWN_EXTERNAL,
            f"{kind} can mean either external capital or an account-internal adjustment, "
            "and the activity record does not distinguish them. It has been recorded and "
            "has changed no figure.",
        )

    if kind not in EXTERNAL_CAPITAL_TYPES:
        return (
            FLOW_UNKNOWN_EXTERNAL,
            f"{kind} is an activity type this build has never classified. It has been "
            "recorded and has changed no figure.",
        )

    if amount is None:
        return FLOW_UNKNOWN_EXTERNAL, f"the {kind} amount could not be read as a number"
    if amount == ZERO:
        return FLOW_UNKNOWN_EXTERNAL, f"the {kind} reports a zero amount"

    # The sign has to agree with the name. A deposit that removes money, or a
    # withdrawal that adds it, is a contradiction between two things the broker
    # said - and a contradiction is resolved by refusing, not by picking one.
    if kind == ACTIVITY_CASH_DEPOSIT:
        if amount < ZERO:
            return (
                FLOW_UNKNOWN_EXTERNAL,
                f"{kind} is a deposit but reports a negative amount ({amount}). The type "
                "and the sign disagree, so neither was believed.",
            )
        return FLOW_EXTERNAL_DEPOSIT, None

    if amount > ZERO:
        return (
            FLOW_UNKNOWN_EXTERNAL,
            f"{kind} is a withdrawal but reports a positive amount ({amount}). The type "
            "and the sign disagree, so neither was believed.",
        )
    return FLOW_EXTERNAL_WITHDRAWAL, None


__all__ = [
    "ACTIVITY_CASH_DEPOSIT",
    "ACTIVITY_CASH_WITHDRAWAL",
    "AMBIGUOUS_TYPES",
    "EXTERNAL_CAPITAL_TYPES",
    "NON_EXTERNAL_TYPES",
    "STATUS_EXECUTED",
    "VOID_STATUSES",
    "classify",
    "confirmation_of",
    "parse_amount",
]
