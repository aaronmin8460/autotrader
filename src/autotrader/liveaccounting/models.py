"""Value types and vocabulary for real-money performance accounting.

Every money figure is a `Decimal`. Nothing here does I/O, and nothing here is
importable by a strategy, an allocator, a risk engine or an execution path -
the suite asserts that direction, because an accounting figure that could reach
a sizing decision would stop being accounting and start being a strategy input.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

#: This ledger describes the real-money book and only the real-money book.
ENVIRONMENT_LIVE = "LIVE"

CENT = Decimal("0.01")
ZERO = Decimal("0")


class LiveAccountingError(Exception):
    """A live-accounting figure could not be established. Nothing was assumed."""


class LiveAccountingInputError(LiveAccountingError):
    """A caller passed something this layer will not interpret."""


# --------------------------------------------------------------------------
# How a broker activity is classified
# --------------------------------------------------------------------------

#: Explicit user or bank funding. Increases capital, never performance.
FLOW_EXTERNAL_DEPOSIT = "EXTERNAL_DEPOSIT"

#: Explicit user or bank withdrawal. Decreases capital, never performance.
FLOW_EXTERNAL_WITHDRAWAL = "EXTERNAL_WITHDRAWAL"

#: Recognised, and recognised as *not* external capital: dividends, interest,
#: fees, trade proceeds, corporate actions. These are account return or account
#: cost. They belong to performance and must never be netted out of it.
FLOW_NON_EXTERNAL = "NON_EXTERNAL"

#: Recognised as a thing that might have moved external capital, and not
#: resolvable from what the broker reports. It changes nothing, and it stops
#: the derived figures from being published at all. Fail closed.
FLOW_UNKNOWN_EXTERNAL = "UNKNOWN_EXTERNAL_FLOW"

CLASSIFICATIONS: tuple[str, ...] = (
    FLOW_EXTERNAL_DEPOSIT,
    FLOW_EXTERNAL_WITHDRAWAL,
    FLOW_NON_EXTERNAL,
    FLOW_UNKNOWN_EXTERNAL,
)

#: The broker settled it. Only a confirmed flow has any accounting effect.
CONFIRMATION_CONFIRMED = "CONFIRMED"

#: Seen, not settled. An announced ACH, an expectation, a transfer in flight:
#: zero accounting effect until broker truth says otherwise.
CONFIRMATION_PENDING = "PENDING"

#: Cancelled or rejected at the broker. Never had an effect and never will.
CONFIRMATION_VOID = "VOID"

CONFIRMATIONS: tuple[str, ...] = (
    CONFIRMATION_CONFIRMED,
    CONFIRMATION_PENDING,
    CONFIRMATION_VOID,
)

#: The flow that delivered the opening capital. Real history, kept in the
#: ledger, and deliberately worth nothing to performance: it *is* the baseline,
#: so counting it again would read the account's own funding as a doubling.
RELATION_PRE_INCEPTION = "PRE_INCEPTION"
RELATION_SINCE_INCEPTION = "SINCE_INCEPTION"
RELATIONS: tuple[str, ...] = (RELATION_PRE_INCEPTION, RELATION_SINCE_INCEPTION)


# --------------------------------------------------------------------------
# Checkpoints
# --------------------------------------------------------------------------

#: The baseline snapshot. Exactly one, and it starts every chain.
CHECKPOINT_INCEPTION = "INCEPTION"

#: A completed trading day. THE authoritative progression: the high-water mark,
#: the reserve and the harvest all read these and nothing else.
CHECKPOINT_DAILY_CLOSE = "DAILY_CLOSE"

#: A mid-session observation. Recorded, published as `observed_intraday_peak`,
#: and structurally unable to create a withdrawal entitlement.
CHECKPOINT_INTRADAY = "INTRADAY"

CHECKPOINT_KINDS: tuple[str, ...] = (
    CHECKPOINT_INCEPTION,
    CHECKPOINT_DAILY_CLOSE,
    CHECKPOINT_INTRADAY,
)

#: The kinds the high-water mark, the reserve and harvest eligibility may read.
AUTHORITATIVE_KINDS: frozenset[str] = frozenset({CHECKPOINT_INCEPTION, CHECKPOINT_DAILY_CLOSE})


# --------------------------------------------------------------------------
# Accounting status
# --------------------------------------------------------------------------

STATUS_NOT_INITIALIZED = "NOT_INITIALIZED"
STATUS_CLEAN = "CLEAN"
STATUS_STALE = "STALE"
STATUS_UNKNOWN_EXTERNAL_FLOW = "UNKNOWN_EXTERNAL_FLOW"
STATUS_REBUILD_REQUIRED = "REBUILD_REQUIRED"
STATUS_BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
STATUS_ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"

ACCOUNTING_STATUSES: tuple[str, ...] = (
    STATUS_NOT_INITIALIZED,
    STATUS_CLEAN,
    STATUS_STALE,
    STATUS_UNKNOWN_EXTERNAL_FLOW,
    STATUS_REBUILD_REQUIRED,
    STATUS_BROKER_UNAVAILABLE,
    STATUS_ACCOUNT_MISMATCH,
)

#: Statuses under which no derived money figure may be published. Showing
#: "unknown" is always better than showing a precise wrong number.
SUPPRESSING_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_NOT_INITIALIZED,
        STATUS_UNKNOWN_EXTERNAL_FLOW,
        STATUS_REBUILD_REQUIRED,
        STATUS_BROKER_UNAVAILABLE,
        STATUS_ACCOUNT_MISMATCH,
    }
)

FRESHNESS_FRESH = "FRESH"
FRESHNESS_STALE = "STALE"
FRESHNESS_UNKNOWN = "UNKNOWN"

#: Past this many seconds since the last checkpoint the payload is STALE.
STALENESS_HORIZON_SECONDS = 900

#: The broker's Trading API publishes no settled-withdrawable-cash figure, so
#: this system publishes none either rather than passing off `cash` as one.
WITHDRAWABLE_VERIFIED = "VERIFIED"
WITHDRAWABLE_NOT_EXPOSED = "NOT_EXPOSED_BY_BROKER"
WITHDRAWABLE_UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------------------
# The reserve policy
# --------------------------------------------------------------------------

#: Observation only. No transfer, no scheduling, no broker call.
WITHDRAWAL_MODE_OBSERVE_ONLY = "OBSERVE_ONLY"

PROFIT_RESERVE_OBSERVE_V1 = "PROFIT_RESERVE_OBSERVE_V1"

#: The time-weighted return's flow convention. See `engine.subperiod_return`.
TWR_CONVENTION_CONSERVATIVE_MIN = "CONSERVATIVE_MIN"

RESERVE_EVENT_ACCRUAL = "ACCRUAL"
RESERVE_EVENT_WITHDRAWAL_ALLOCATION = "WITHDRAWAL_ALLOCATION"


def floor_cent(value: Decimal) -> Decimal:
    """Down to the cent, always down - the direction every quantity in this system rounds."""
    return value.quantize(CENT, rounding=ROUND_FLOOR)


@dataclass(frozen=True)
class ProfitReservePolicy:
    """The reserve parameters, as one hashable identity.

    **This is not the EDA-1 cash reserve.** That one is trading liquidity and
    changes position sizing. This one moves no money, segregates no broker cash
    and changes no position size: it earmarks a share of newly created
    high-water-mark profit against a *possible future* withdrawal, and during
    the first month it does not even do that operationally - it writes a number
    on a page.
    """

    policy_id: str = PROFIT_RESERVE_OBSERVE_V1
    reserve_rate: Decimal = Decimal("0.20")
    withdrawal_mode: str = WITHDRAWAL_MODE_OBSERVE_ONLY
    automatic_transfer: bool = False
    withdrawal_authorized: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.reserve_rate, Decimal):
            raise LiveAccountingInputError("reserve_rate must be an exact Decimal")
        if not (ZERO <= self.reserve_rate <= Decimal("1")):
            raise LiveAccountingInputError(
                f"reserve_rate must be a fraction between 0 and 1, got {self.reserve_rate}"
            )
        # The first-month policy is observation. A build that could emit any
        # other mode would need this refusal removed, deliberately and visibly.
        if self.withdrawal_mode != WITHDRAWAL_MODE_OBSERVE_ONLY:
            raise LiveAccountingInputError(
                "This build supports OBSERVE_ONLY only. Activating withdrawals is a "
                "policy decision for a later program, not a constructor argument."
            )
        if self.automatic_transfer or self.withdrawal_authorized:
            raise LiveAccountingInputError(
                "automatic_transfer and withdrawal_authorized are false under "
                "OBSERVE_ONLY and this build cannot set them true."
            )

    def to_json_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "reserve_rate": str(self.reserve_rate),
            "withdrawal_mode": self.withdrawal_mode,
            "automatic_transfer": self.automatic_transfer,
            "withdrawal_authorized": self.withdrawal_authorized,
        }

    @property
    def policy_hash(self) -> str:
        import hashlib
        import json

        canonical = json.dumps(self.to_json_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExternalFlow:
    """One external cash movement, as the ledger holds it.

    `amount` is **signed**: positive is money entering the account, negative is
    money leaving it. The sign and the classification must agree, and
    `classify` refuses the pair when they do not rather than trusting either.
    """

    broker_activity_id: str
    activity_type: str
    classification: str
    confirmation: str
    amount: Decimal
    settle_at: datetime
    currency: str = "USD"
    relation: str = RELATION_SINCE_INCEPTION

    @property
    def is_confirmed_external(self) -> bool:
        return self.confirmation == CONFIRMATION_CONFIRMED and self.classification in (
            FLOW_EXTERNAL_DEPOSIT,
            FLOW_EXTERNAL_WITHDRAWAL,
        )

    @property
    def counts_for_performance(self) -> bool:
        """Confirmed, external, and after the baseline was frozen."""
        return self.is_confirmed_external and self.relation == RELATION_SINCE_INCEPTION


@dataclass(frozen=True)
class EquityCheckpoint:
    """Broker truth at one instant. Authoritative input; nothing here is derived."""

    taken_at: datetime
    kind: str
    broker_equity: Decimal
    broker_cash: Decimal
    unrealized_pnl: Decimal | None = None

    @property
    def is_authoritative(self) -> bool:
        return self.kind in AUTHORITATIVE_KINDS


@dataclass(frozen=True)
class ReserveEvent:
    """One thing that happened to the virtual bucket, and why."""

    event_type: str
    occurred_at: datetime
    prior_reserve_hwm: Decimal
    new_reserve_hwm: Decimal
    new_hwm_profit: Decimal
    accrual_amount: Decimal
    withdrawal_amount: Decimal
    allocated_from_bucket: Decimal
    unreserved_amount: Decimal
    bucket_before: Decimal
    bucket_after: Decimal


__all__ = [
    "ACCOUNTING_STATUSES",
    "AUTHORITATIVE_KINDS",
    "CENT",
    "CHECKPOINT_DAILY_CLOSE",
    "CHECKPOINT_INCEPTION",
    "CHECKPOINT_INTRADAY",
    "CHECKPOINT_KINDS",
    "CLASSIFICATIONS",
    "CONFIRMATIONS",
    "CONFIRMATION_CONFIRMED",
    "CONFIRMATION_PENDING",
    "CONFIRMATION_VOID",
    "ENVIRONMENT_LIVE",
    "FLOW_EXTERNAL_DEPOSIT",
    "FLOW_EXTERNAL_WITHDRAWAL",
    "FLOW_NON_EXTERNAL",
    "FLOW_UNKNOWN_EXTERNAL",
    "FRESHNESS_FRESH",
    "FRESHNESS_STALE",
    "FRESHNESS_UNKNOWN",
    "PROFIT_RESERVE_OBSERVE_V1",
    "RELATIONS",
    "RELATION_PRE_INCEPTION",
    "RELATION_SINCE_INCEPTION",
    "RESERVE_EVENT_ACCRUAL",
    "RESERVE_EVENT_WITHDRAWAL_ALLOCATION",
    "STALENESS_HORIZON_SECONDS",
    "STATUS_ACCOUNT_MISMATCH",
    "STATUS_BROKER_UNAVAILABLE",
    "STATUS_CLEAN",
    "STATUS_NOT_INITIALIZED",
    "STATUS_REBUILD_REQUIRED",
    "STATUS_STALE",
    "STATUS_UNKNOWN_EXTERNAL_FLOW",
    "SUPPRESSING_STATUSES",
    "TWR_CONVENTION_CONSERVATIVE_MIN",
    "WITHDRAWABLE_NOT_EXPOSED",
    "WITHDRAWABLE_UNKNOWN",
    "WITHDRAWABLE_VERIFIED",
    "WITHDRAWAL_MODE_OBSERVE_ONLY",
    "ZERO",
    "EquityCheckpoint",
    "ExternalFlow",
    "LiveAccountingError",
    "LiveAccountingInputError",
    "ProfitReservePolicy",
    "ReserveEvent",
    "floor_cent",
]
