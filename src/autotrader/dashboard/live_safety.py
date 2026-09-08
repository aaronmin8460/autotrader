"""The real-money safety panel: what the Live page shows that accounting does not.

The frozen Live accounting contract answers "how much money is there, and how
much of it did trading make". It deliberately does not answer "may this system
trade, how much may it expose, and is the account the one we pinned" - those are
Prompt 1's questions, and Prompt 1 wrote the functions that answer them. This
module serves those answers over HTTP and adds nothing to them.

**Every figure here is quoted, not derived.** The four ceilings come from
`live.budget.effective_ceilings`, which its own docstring says exists "so the
dashboard, the runbook and the first-day checklist can *show* the numbers that
are being enforced, rather than an operator having to rederive them". The arm
state comes from `live.armstate.read_arm_state`. The account pin comes from
`live.identity`. The deposit-day guard reason is the sentence
`live.budget.cash_flow_block_reason` already writes for the operator. Nothing in
this module re-implements a ceiling, a halt, or a gate; a second implementation
of a risk number is a second risk policy nobody validated.

**Verified means the broker said so, now.** `effective_ceilings` refuses a
balance that did not come from the broker, and this module honours that: when
the account cannot be read, the ceilings come back `NOT_VERIFIED` with every
figure `None`. It does not fall back to a configured balance, a last-known
balance, or a balance from the accounting record. An account whose equity could
not be read is not an account with no headroom - it is an account whose headroom
is unknown, and those must not look the same on a screen.

**Nothing here mutates, and nothing here can.** The import list from the live
packages is read functions and dataclasses. `arm_live_trading` and
`disarm_live_trading` exist in `live.armstate`; they are not imported, not
referenced and not reachable from this module, and the suite asserts that
against this file's executable code with prose stripped. The panel reports the
arm switch; it is not a way to throw it.

**No credential and no account number leaves here.** The account appears only as
the 32-character fingerprint Prompt 1 pinned, and the panel carries a short
8-character form for display. Broker exception text is discarded rather than
forwarded - an authentication error's message is the likeliest place for a key
fragment to appear, and this module's whole output is bound for a browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol, runtime_checkable

from autotrader.execution.live import live_credentials_configured
from autotrader.live.armstate import (
    STATE_ARMED,
    STATE_DISARMED,
    ArmState,
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
    configured_account_fingerprint,
)

# --------------------------------------------------------------------------
# Vocabulary
#
# Stable machine strings. The frontend maps them to words in two languages; it
# never invents one, so "what states exist" is defined once, here.
# --------------------------------------------------------------------------

ENVIRONMENT_LIVE = "LIVE"

#: How the account read went. `NOT_CONFIGURED` and `UNREADABLE` are kept apart
#: because they call for different operator action: one is a deployment step
#: that has not happened, the other is a broker that is not answering.
ACCOUNT_OK = "OK"
ACCOUNT_NOT_CONFIGURED = "NOT_CONFIGURED"
ACCOUNT_UNREADABLE = "UNREADABLE"
ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"

#: Whether the ceilings on screen are the ones being enforced. There is no
#: third value that means "probably": either a broker balance resolved them or
#: they are not known.
CEILINGS_RESOLVED = "RESOLVED"
CEILINGS_NOT_VERIFIED = "NOT_VERIFIED"

#: The arm switch as the panel reports it. `UNKNOWN` is not `DISARMED`: an arm
#: state that could not be read is not a proven-off one, and the contract's own
#: rule for `live_armed` says a null must never be displayed as false.
ARM_ARMED = STATE_ARMED
ARM_DISARMED = STATE_DISARMED
ARM_UNKNOWN = "UNKNOWN"

#: The account pin.
IDENTITY_PINNED = "PINNED"
IDENTITY_NOT_PINNED = "NOT_PINNED"
IDENTITY_MISMATCH = "MISMATCH"
IDENTITY_UNKNOWN = "UNKNOWN"

#: The deposit-day guard. `UNKNOWN` means the activity feed could not be read,
#: which is not the same as a clean day and must not be shown as one.
GUARD_ACTIVE = "ACTIVE"
GUARD_INACTIVE = "INACTIVE"
GUARD_UNKNOWN = "UNKNOWN"

#: The Live systemd unit. Prompt 1 prepared it and deliberately did not install
#: it; that is a declared, expected state on this program's timeline and must
#: not render as a trading failure. The five values are distinct because an
#: operator does different things about each.
SERVICE_NOT_INSTALLED = "NOT_INSTALLED"
SERVICE_DISABLED = "DISABLED"
SERVICE_STOPPED = "STOPPED"
SERVICE_RUNNING = "RUNNING"
SERVICE_UNKNOWN = "UNKNOWN"

#: The unit's name. A service name is a machine identifier and is rendered
#: verbatim in both locales.
LIVE_SERVICE_NAME = "autotrader-equity-live.service"

#: How many characters of the fingerprint the browser shows. Enough to tell two
#: accounts apart in a runbook, far too few to be the account number - which is
#: not recoverable from any length of a SHA-256 digest in any case.
FINGERPRINT_DISPLAY_CHARS = 8


@runtime_checkable
class ReadableLiveBroker(Protocol):
    """The one broker capability this panel needs.

    Structural, and one method wide, for the same reason `dashboard.broker`
    types its client as `ReadableBroker`: a type that cannot name a submission
    method cannot be asked to submit, and the absence is checkable even though
    Python cannot make it impossible.
    """

    def get_account(self) -> object: ...


def _decimal_text(value: object) -> str | None:
    """A broker figure as exact decimal text, or None when it is not a number.

    Text, not `float`, all the way to the browser. The contract this panel sits
    beside requires money to be an exact decimal string parsed with an exact
    type, and a figure that has been through a binary float is a different
    figure from the one the broker reported.
    """
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not parsed.is_finite():
        return None
    return str(parsed)


def _decimal_or_none(value: object) -> Decimal | None:
    text = _decimal_text(value)
    if text is None:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _bool_or_none(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _text_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class LiveAccountFacts:
    """The broker account as the panel reports it. All JSON-ready primitives.

    Every money field is decimal text and every unknown is `None`. There is no
    partially populated success: when `status` is not `OK` the figures are all
    `None` and the frontend says the account could not be read, rather than
    drawing a zero an operator would read as a flat account.
    """

    status: str
    equity: str | None = None
    cash: str | None = None
    buying_power: str | None = None
    account_status: str | None = None
    account_type: str | None = None
    multiplier: str | None = None
    shorting_enabled: bool | None = None
    trading_blocked: bool | None = None
    account_blocked: bool | None = None
    transfers_blocked: bool | None = None
    position_count: int | None = None
    open_order_count: int | None = None
    read_at: str | None = None

    @property
    def readable(self) -> bool:
        return self.status == ACCOUNT_OK


@dataclass(frozen=True)
class LiveRiskEnvelope:
    """The four ceilings, resolved against one verified balance - or not at all.

    `status` is the whole contract. `RESOLVED` means a broker balance produced
    these figures on this read; `NOT_VERIFIED` means it did not, and then every
    figure is `None`. There is deliberately no configured or remembered balance
    behind a `NOT_VERIFIED`: Prompt 1's doctrine is that only a balance the
    broker reports is equity, and a panel that quietly resolved ceilings from a
    stale number would be showing limits that are not the enforced ones.
    """

    status: str
    policy_id: str
    policy_config_hash: str
    #: The absolute authorization, independent of any balance. This is the one
    #: figure here that is a policy constant rather than a resolved ceiling, and
    #: it is what makes "funding above $100 does not raise the limit" showable.
    capital_bound: str | None = None
    daily_loss_halt_fraction: str | None = None
    verified_equity: str | None = None
    target_gross: str | None = None
    hard_gross: str | None = None
    exposure_bound: str | None = None
    per_symbol: str | None = None
    slot: str | None = None
    binding: str | None = None
    current_gross_exposure: str | None = None
    remaining_gross_capacity: str | None = None
    universe_size: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class LiveArmPanel:
    """The durable arm switch, as it was read.

    `DISARMED` is a valid, safe, expected state and the frontend is required to
    render it as one. `UNKNOWN` is the state that deserves attention, because a
    switch that cannot be read has not been proven off.
    """

    state: str
    armed: bool | None
    reason: str | None = None
    source: str | None = None
    changed_at: str | None = None
    code_sha: str | None = None


@dataclass(frozen=True)
class LiveIdentityPanel:
    """The account pin, in the only form that may reach a browser."""

    status: str
    fingerprint: str | None = None
    fingerprint_short: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class DepositDayGuardPanel:
    """Prompt 1's non-trade cash-movement guard, for the risk day it applies to.

    `reason` is the sentence `cash_flow_block_reason` already writes - the one
    an operator reads at 09:45 on the morning a deposit lands. It is quoted
    rather than summarized, because "blocked" without it is the kind of message
    that gets overridden.
    """

    status: str
    active: bool | None
    risk_day: str | None = None
    reason: str | None = None
    cash_flow_count: int | None = None


@dataclass(frozen=True)
class LiveServicePanel:
    """The Live runtime unit, across the five states it can genuinely be in.

    `NOT_INSTALLED` is where this unit stands today by design: Prompt 1 prepared
    the template and left installation as the first step of the first-day
    runbook. The frontend must not colour it as a fault.
    """

    state: str
    unit: str = LIVE_SERVICE_NAME
    detail: str | None = None
    since: str | None = None


@dataclass(frozen=True)
class LiveReconciliationPanel:
    """The startup reconciliation verdict, quoted from the stored run."""

    available: bool
    status: str | None = None
    safe_to_trade: bool | None = None
    completed_at: str | None = None
    issues: int | None = None
    unresolved: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class LiveSafetyPanel:
    """One read of the real-money safety state, assembled for one page.

    Assembled from a single pass so two cards on the Live page cannot disagree
    about the same instant, exactly as `dashboard.service` does for the paper
    overview.
    """

    generated_at: str
    environment: str
    live_ready: bool | None
    arm: LiveArmPanel
    identity: LiveIdentityPanel
    account: LiveAccountFacts
    risk: LiveRiskEnvelope
    deposit_day_guard: DepositDayGuardPanel
    reconciliation: LiveReconciliationPanel
    service: LiveServicePanel
    code_sha: str | None = None
    notices: tuple[str, ...] = ()


def live_credentials_present() -> bool:
    """Whether real-money read credentials are configured for this process.

    Delegated to `execution.live`, which is the single audited real-money
    boundary and the only module in this repository permitted to name the
    credential variables - `tests/test_execution_paper.py` asserts that, and it
    is a rule worth keeping: one place to audit is the whole value of having a
    boundary. This function answers a yes/no and never reads, returns, logs or
    compares what is in those variables.
    """
    return live_credentials_configured()


def configured_fingerprint() -> str | None:
    """The pinned account fingerprint, when one is configured.

    Prompt 1's `live.identity` owns the pin. Re-reading the environment here
    would be a second definition of which account this system is authorized to
    operate, which is precisely the thing a pin exists to make singular.
    """
    return _text_or_none(configured_account_fingerprint())


def build_identity(
    *, observed_fingerprint: str | None, expected_fingerprint: str | None = None
) -> LiveIdentityPanel:
    """The account pin, compared against the account that actually answered.

    A mismatch fails closed and says so. It is not softened into "unknown":
    a payload from an account that is not the pinned one must be discarded
    rather than displayed, and an operator needs to know which of the two
    happened.
    """
    expected = (
        expected_fingerprint if expected_fingerprint is not None else configured_fingerprint()
    )
    if expected is None:
        return LiveIdentityPanel(
            status=IDENTITY_NOT_PINNED,
            fingerprint=observed_fingerprint,
            fingerprint_short=(
                observed_fingerprint[:FINGERPRINT_DISPLAY_CHARS] if observed_fingerprint else None
            ),
            detail=(
                f"No account is pinned. Set {LIVE_ACCOUNT_FINGERPRINT_ENV} to the "
                "fingerprint of the account this system is authorized to operate."
            ),
        )
    if observed_fingerprint is None:
        return LiveIdentityPanel(
            status=IDENTITY_UNKNOWN,
            fingerprint=expected,
            fingerprint_short=expected[:FINGERPRINT_DISPLAY_CHARS],
            detail="An account is pinned, but no account answered, so the pin is unchecked.",
        )
    if observed_fingerprint != expected:
        return LiveIdentityPanel(
            status=IDENTITY_MISMATCH,
            fingerprint=expected,
            fingerprint_short=expected[:FINGERPRINT_DISPLAY_CHARS],
            detail=(
                "The account that answered is not the pinned account. Every figure on "
                "this page is scoped to one account and none may be shown for another."
            ),
        )
    return LiveIdentityPanel(
        status=IDENTITY_PINNED,
        fingerprint=expected,
        fingerprint_short=expected[:FINGERPRINT_DISPLAY_CHARS],
    )


def build_arm(state: ArmState | None) -> LiveArmPanel:
    """The arm switch as a panel. `None` in means `UNKNOWN` out, never off."""
    if state is None:
        return LiveArmPanel(
            state=ARM_UNKNOWN,
            armed=None,
            reason=(
                "The durable arm state could not be read. An unreadable arm switch is "
                "not a disarmed one."
            ),
        )
    return LiveArmPanel(
        state=STATE_ARMED if state.armed else STATE_DISARMED,
        armed=state.armed,
        reason=state.reason,
        source=state.source,
        changed_at=_iso(state.changed_at),
        code_sha=state.code_sha,
    )


def _policy_config_hash(policy: object) -> str:
    """The policy's own digest. A method on the policy, called, never recomputed.

    Recomputing it here would be a second definition of what a policy *is*, and
    the whole value of the hash is that there is one.
    """
    digest = getattr(policy, "config_hash", None)
    if callable(digest):
        try:
            return _text_or_none(digest()) or "UNKNOWN"
        except Exception:  # noqa: BLE001 - a policy that cannot digest itself is reported, not raised
            return "UNKNOWN"
    return _text_or_none(digest) or "UNKNOWN"


def _daily_loss_halt_fraction(policy: object) -> str | None:
    """The halt the Risk Engine will actually apply to this policy.

    Read through `risk_policy_for` rather than off the allocation policy, which
    carries no such field: the halt is a Risk Engine parameter, and asking the
    function that resolves it is the only way to be showing the number that
    stops trading rather than one that resembles it.
    """
    try:
        from autotrader.equity.allocation import risk_policy_for

        return _decimal_text(risk_policy_for(policy).max_daily_loss_fraction)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - an unresolvable halt is reported as unknown, not as zero
        return None


def build_risk_envelope(
    policy: object,
    *,
    account: LiveAccountFacts,
    gross_exposure: Decimal | None = None,
) -> LiveRiskEnvelope:
    """The enforced ceilings at this balance, or an honest `NOT_VERIFIED`.

    The arithmetic is not here. `effective_ceilings` owns `min(fraction x
    equity, absolute)` and this function calls it; if the two ever disagreed the
    dashboard would be drawing limits the risk engine is not enforcing, which is
    the specific failure this indirection exists to make impossible.
    """
    policy_id = _text_or_none(getattr(policy, "policy_id", None)) or "UNKNOWN"
    config_hash = _policy_config_hash(policy)
    capital_bound = _decimal_text(getattr(policy, "capital_bound", None))
    daily_loss = _daily_loss_halt_fraction(policy)
    universe_size = getattr(policy, "universe_size", None)
    universe = universe_size if isinstance(universe_size, int) else None

    verified = _decimal_or_none(account.equity) if account.readable else None
    if verified is None:
        return LiveRiskEnvelope(
            status=CEILINGS_NOT_VERIFIED,
            policy_id=policy_id,
            policy_config_hash=config_hash,
            capital_bound=capital_bound,
            daily_loss_halt_fraction=daily_loss,
            universe_size=universe,
            detail=(
                "The ceilings are resolved against the balance the broker reports on the "
                "cycle that uses it. No balance was read, so the enforced ceilings are "
                "not known. They are not shown from a remembered or configured figure."
            ),
        )

    try:
        ceilings: LiveExposureCeilings = effective_ceilings(policy, verified_equity=verified)  # type: ignore[arg-type]
    except LiveBudgetError as error:
        return LiveRiskEnvelope(
            status=CEILINGS_NOT_VERIFIED,
            policy_id=policy_id,
            policy_config_hash=config_hash,
            capital_bound=capital_bound,
            daily_loss_halt_fraction=daily_loss,
            universe_size=universe,
            detail=str(error),
        )

    remaining: str | None = None
    if gross_exposure is not None:
        headroom = ceilings.hard_gross - gross_exposure
        remaining = str(headroom if headroom > Decimal(0) else Decimal("0.00"))

    return LiveRiskEnvelope(
        status=CEILINGS_RESOLVED,
        policy_id=policy_id,
        policy_config_hash=config_hash,
        capital_bound=capital_bound,
        daily_loss_halt_fraction=daily_loss,
        verified_equity=str(ceilings.verified_equity),
        target_gross=str(ceilings.target_gross),
        hard_gross=str(ceilings.hard_gross),
        exposure_bound=str(ceilings.exposure_bound),
        per_symbol=str(ceilings.per_symbol),
        slot=str(ceilings.slot),
        binding=ceilings.binding,
        current_gross_exposure=None if gross_exposure is None else str(gross_exposure),
        remaining_gross_capacity=remaining,
        universe_size=universe,
    )


def build_deposit_day_guard(
    events: tuple[CashFlowEvent, ...] | list[CashFlowEvent] | None,
    *,
    risk_day: date,
) -> DepositDayGuardPanel:
    """Prompt 1's guard, reported for `risk_day`.

    `None` events means the activity feed could not be read, and that is
    `UNKNOWN` rather than `INACTIVE`. A day whose cash movements are unknown is
    not a day proven free of them.
    """
    if events is None:
        return DepositDayGuardPanel(
            status=GUARD_UNKNOWN,
            active=None,
            risk_day=risk_day.isoformat(),
            reason=(
                "Non-trade cash activity could not be read for this risk day, so whether "
                "the daily-loss baseline still describes trading is not known."
            ),
        )
    reason = cash_flow_block_reason(events, risk_day=risk_day)
    relevant = [
        event
        for event in events
        if event.is_cash_flow and event.transaction_time.date() == risk_day
    ]
    return DepositDayGuardPanel(
        status=GUARD_ACTIVE if reason else GUARD_INACTIVE,
        active=reason is not None,
        risk_day=risk_day.isoformat(),
        reason=reason,
        cash_flow_count=len(relevant),
    )


def read_live_account(
    client: ReadableLiveBroker | None,
    *,
    position_count: int | None = None,
    open_order_count: int | None = None,
    now: datetime | None = None,
) -> LiveAccountFacts:
    """Read the real-money account, or say why it could not be read.

    Never raises. A missing credential and an unreachable broker are distinct
    values, and the underlying exception text is discarded rather than
    forwarded - it is the likeliest place for a key fragment or an account
    identifier to appear, and this function's output is bound for a browser.

    The attributes are read defensively because the panel reports facts the
    normalized account state does not carry - account type, multiplier, whether
    shorting is enabled - and a broker that stops sending one of them should
    cost that one field, not the whole read.
    """
    if client is None:
        if not live_credentials_present():
            return LiveAccountFacts(status=ACCOUNT_NOT_CONFIGURED)
        return LiveAccountFacts(status=ACCOUNT_UNREADABLE)
    try:
        account = client.get_account()
    except Exception:  # noqa: BLE001 - the text is discarded on purpose; see the docstring
        return LiveAccountFacts(status=ACCOUNT_UNREADABLE)
    if account is None:
        return LiveAccountFacts(status=ACCOUNT_UNREADABLE)

    multiplier = _decimal_text(getattr(account, "multiplier", None))
    account_type: str | None = None
    if multiplier is not None:
        try:
            account_type = "CASH" if Decimal(multiplier) == Decimal(1) else "MARGIN"
        except InvalidOperation:
            account_type = None

    return LiveAccountFacts(
        status=ACCOUNT_OK,
        equity=_decimal_text(getattr(account, "equity", None)),
        cash=_decimal_text(getattr(account, "cash", None)),
        buying_power=_decimal_text(getattr(account, "buying_power", None)),
        account_status=_text_or_none(getattr(account, "status", None)),
        account_type=account_type,
        multiplier=multiplier,
        shorting_enabled=_bool_or_none(getattr(account, "shorting_enabled", None)),
        trading_blocked=_bool_or_none(getattr(account, "trading_blocked", None)),
        account_blocked=_bool_or_none(getattr(account, "account_blocked", None)),
        transfers_blocked=_bool_or_none(getattr(account, "transfers_blocked", None)),
        position_count=position_count,
        open_order_count=open_order_count,
        read_at=_iso(now or datetime.now(UTC)),
    )


def build_panel(
    *,
    now: datetime,
    policy: object,
    account: LiveAccountFacts,
    arm_state: ArmState | None,
    observed_fingerprint: str | None = None,
    expected_fingerprint: str | None = None,
    gross_exposure: Decimal | None = None,
    cash_flow_events: tuple[CashFlowEvent, ...] | list[CashFlowEvent] | None = None,
    reconciliation: LiveReconciliationPanel | None = None,
    service: LiveServicePanel | None = None,
    live_ready: bool | None = None,
    code_sha: str | None = None,
) -> LiveSafetyPanel:
    """One consistent read of the real-money safety state.

    `live_ready` is passed through exactly as the accounting contract passes it
    through: this module does not compute readiness, and a caller that cannot
    establish it hands in `None`, which the frontend shows as unknown.
    """
    identity = build_identity(
        observed_fingerprint=observed_fingerprint,
        expected_fingerprint=expected_fingerprint,
    )
    resolved_account = (
        LiveAccountFacts(status=ACCOUNT_MISMATCH)
        if identity.status == IDENTITY_MISMATCH
        else account
    )
    risk = build_risk_envelope(policy, account=resolved_account, gross_exposure=gross_exposure)
    guard = build_deposit_day_guard(cash_flow_events, risk_day=now.astimezone(UTC).date())

    notices: list[str] = []
    if identity.status == IDENTITY_MISMATCH:
        notices.append(
            "The account that answered is not the pinned account. No figure is shown for it."
        )
    if risk.status == CEILINGS_NOT_VERIFIED:
        notices.append(
            "Live exposure ceilings are not resolved because no verified broker balance was read."
        )
    if guard.status == GUARD_ACTIVE:
        notices.append("Deposit-day guard active: new entries are blocked; exits remain available.")

    return LiveSafetyPanel(
        generated_at=_iso(now) or now.isoformat(),
        environment=ENVIRONMENT_LIVE,
        live_ready=live_ready,
        arm=build_arm(arm_state),
        identity=identity,
        account=resolved_account,
        risk=risk,
        deposit_day_guard=guard,
        reconciliation=reconciliation
        or LiveReconciliationPanel(
            available=False,
            detail="No real-money reconciliation run has been recorded in this store.",
        ),
        service=service
        or LiveServicePanel(
            state=SERVICE_NOT_INSTALLED,
            detail=(
                "The real-money unit is prepared but not installed. Installing and "
                "starting it DISARMED is the first step of the first-day runbook."
            ),
        ),
        code_sha=code_sha,
        notices=tuple(notices),
    )


__all__ = [
    "ACCOUNT_MISMATCH",
    "ACCOUNT_NOT_CONFIGURED",
    "ACCOUNT_OK",
    "ACCOUNT_UNREADABLE",
    "ARM_ARMED",
    "ARM_DISARMED",
    "ARM_UNKNOWN",
    "CEILINGS_NOT_VERIFIED",
    "CEILINGS_RESOLVED",
    "ENVIRONMENT_LIVE",
    "FINGERPRINT_DISPLAY_CHARS",
    "GUARD_ACTIVE",
    "GUARD_INACTIVE",
    "GUARD_UNKNOWN",
    "IDENTITY_MISMATCH",
    "IDENTITY_NOT_PINNED",
    "IDENTITY_PINNED",
    "IDENTITY_UNKNOWN",
    "LIVE_SERVICE_NAME",
    "SERVICE_DISABLED",
    "SERVICE_NOT_INSTALLED",
    "SERVICE_RUNNING",
    "SERVICE_STOPPED",
    "SERVICE_UNKNOWN",
    "DepositDayGuardPanel",
    "LiveAccountFacts",
    "LiveArmPanel",
    "LiveIdentityPanel",
    "LiveReconciliationPanel",
    "LiveRiskEnvelope",
    "LiveSafetyPanel",
    "LiveServicePanel",
    "ReadableLiveBroker",
    "build_arm",
    "build_deposit_day_guard",
    "build_identity",
    "build_panel",
    "build_risk_envelope",
    "configured_fingerprint",
    "live_credentials_present",
    "read_live_account",
]
