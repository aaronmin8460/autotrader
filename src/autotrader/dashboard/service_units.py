"""Which runtime units are up on this host, asked of the service manager.

Every other panel on this dashboard derives a runtime's state from what that
runtime *wrote down* - lifecycle events and processed-bar checkpoints in a
store. That is the right source for "is the loop looping", and it is the wrong
source for "is the service supposed to be here at all", which is the question
this module answers instead.

WHY THIS MODULE EXISTS
----------------------

Three equity concepts share the word "equity" on this host and they are not the
same thing:

  * ``autotrader-equity.service`` is the older general equity execution
    runtime. It is deliberately **masked** and must stay that way. Its trail
    still sits in the operational store, so a panel derived from that store
    reports it ``STOPPED`` - true of the trail, and badly misleading about the
    system, because an operator reads "Equity ... STOPPED" as "equity trading
    is down".
  * ``autotrader-equity-paper.service`` is the current equity execution
    runtime, running EDA-1 against the paper brokerage. It writes to a
    different store entirely, so nothing it does can ever change the row above.
  * ``autotrader-equity-shadow.service`` observes and mutates no order.

Deriving any of the three from the others is what produced the misleading
screen. So this module does not derive: it names four units explicitly and asks
the service manager about each one by name.

WHY IT IS SAFE
--------------

``systemctl show`` is a read-only property query over the system bus. It needs
no privilege, it changes nothing, and it is the only verb this module knows.
The argument vector is built from a module-level literal - there is no
caller-supplied verb, no shell, and no unit name that did not come from
``SERVICE_UNITS`` below. The suite asserts all of that rather than trusting the
paragraph.

There is no start, stop, restart, enable, disable or unmask anywhere in this
package, and no route in front of it that could reach one if there were.

EXPECTATION IS PART OF THE READING
----------------------------------

A status word alone cannot be coloured correctly. ``MASKED`` is the healthy,
intended state of the legacy unit and an alarming one for anything else, so
each unit declares what it is *supposed* to be and the tone is computed against
that. The result is the three-colour vocabulary the operator needs:

  * green - in its expected running state,
  * neutral - intentionally off, exactly as configured,
  * red - not what this host is supposed to look like.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime

from autotrader.dashboard.models import (
    TONE_ATTENTION,
    TONE_MUTED,
    TONE_NEGATIVE,
    TONE_POSITIVE,
)

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

#: A unit that is loaded and running.
STATUS_RUNNING = "RUNNING"
#: Loaded, not running, and not masked.
STATUS_STOPPED = "STOPPED"
#: The service manager gave up on it.
STATUS_FAILED = "FAILED"
#: Symlinked to /dev/null. It cannot be started, even by accident, and that is
#: a configuration decision rather than a fault.
STATUS_MASKED = "MASKED"
STATUS_STARTING = "STARTING"
STATUS_STOPPING = "STOPPING"
#: No such unit file on this host.
STATUS_NOT_INSTALLED = "NOT INSTALLED"
#: The service manager could not be asked. Never a claim about the unit.
STATUS_UNKNOWN = "UNKNOWN"

#: What a unit is supposed to be on a correctly configured host.
EXPECT_ACTIVE = "ACTIVE"
EXPECT_MASKED = "MASKED"

#: What kind of process a unit is, which is not something a status word
#: carries. A running observer and a running trader are both `RUNNING` to the
#: service manager; to an operator one of them can place an order and the
#: other structurally cannot, and the screen has to say which.
KIND_TRADING = "TRADING"
KIND_OBSERVER = "OBSERVER"
KIND_LEGACY = "LEGACY"

#: The properties asked for, in one call per unit. All four are needed: a
#: masked unit reports `LoadState=masked` while `ActiveState` only says
#: `inactive`, which is the exact confusion this module exists to remove.
UNIT_PROPERTIES: tuple[str, ...] = (
    "LoadState",
    "ActiveState",
    "SubState",
    "UnitFileState",
)

#: The only verb. Named as a constant so the suite can assert there is no other.
SYSTEMCTL_VERB = "show"

#: A property query answers in milliseconds. This bound exists so a wedged bus
#: degrades the panel to UNKNOWN instead of hanging an operator's poll.
QUERY_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class UnitSpec:
    """One unit, its user-facing name, and what it is supposed to be doing.

    ``label`` is deliberately not the unit name. "Equity runtime" was the label
    that caused this whole panel to lie, because three services can answer to
    it; every label here names exactly one of them.
    """

    key: str
    label: str
    unit: str
    expectation: str
    #: A standing qualifier shown with the row whatever its status - what this
    #: service is allowed to do, which is not something a status word carries.
    note: str
    #: What the unit is, in one sentence, for the row's tooltip.
    description: str
    #: Trading, observer, or legacy. Observers render as OBSERVING rather than
    #: RUNNING on screen, in the observation colour, so a green row never
    #: implies a process that can trade.
    kind: str = KIND_TRADING


#: The five units this host runs, in the order an operator should read them:
#: the two books that trade, the two observers that cannot, and the one that
#: is off.
SERVICE_UNITS: tuple[UnitSpec, ...] = (
    UnitSpec(
        key="crypto",
        label="Crypto Paper",
        unit="autotrader-crypto.service",
        expectation=EXPECT_ACTIVE,
        note="PAPER · NO REAL MONEY",
        description=(
            "The 24/7 crypto execution runtime. Submits paper orders to the broker "
            "sandbox account. No real money is involved."
        ),
    ),
    UnitSpec(
        key="equity_paper",
        label="Equity Paper · EDA-1",
        unit="autotrader-equity-paper.service",
        expectation=EXPECT_ACTIVE,
        note="PAPER · NO REAL MONEY",
        description=(
            "The current equity execution runtime, running the EDA-1 strategy. This "
            "is the service that submits paper equity orders. It is not "
            "autotrader-equity.service and shares no state with it."
        ),
    ),
    UnitSpec(
        key="equity_shadow",
        label="Equity Shadow",
        unit="autotrader-equity-shadow.service",
        expectation=EXPECT_ACTIVE,
        note="OBSERVATION ONLY · ZERO ORDERS",
        description=(
            "The independent V3 and EDA-1 observer. It records what it would have "
            "done and can submit, cancel or replace nothing."
        ),
        kind=KIND_OBSERVER,
    ),
    UnitSpec(
        key="equity_a1b_shadow",
        label="A1-B U30 Shadow",
        unit="autotrader-equity-a1b-shadow.service",
        expectation=EXPECT_ACTIVE,
        note="OBSERVATION ONLY · ZERO ORDERS",
        description=(
            "The A1-B U30 archetype-allocation observer over the frozen 26-symbol "
            "universe. It records hypothetical target weights per bar and can "
            "submit, cancel or replace nothing: its observation table refuses any "
            "order linkage by constraint."
        ),
        kind=KIND_OBSERVER,
    ),
    UnitSpec(
        key="equity_legacy",
        label="Legacy Equity Runtime",
        unit="autotrader-equity.service",
        expectation=EXPECT_MASKED,
        note="INTENTIONALLY OFF",
        description=(
            "The older general equity execution runtime, superseded by Equity Paper "
            "and deliberately masked. Masked is its correct state: it is off on "
            "purpose, and current equity paper trading does not depend on it."
        ),
        kind=KIND_LEGACY,
    ),
)


@dataclass(frozen=True)
class ServiceUnitRow:
    """One unit as the service manager currently describes it.

    ``expected`` and ``healthy`` are separated on purpose. A masked legacy unit
    is both; a masked *trading* unit would be neither; a legacy unit somebody
    unmasked and started is unexpected without yet being broken. Collapsing the
    two into one boolean is how "intentionally off" gets painted as an error.
    """

    key: str
    label: str
    unit: str
    status: str
    tone: str
    note: str
    detail: str
    expected: bool
    healthy: bool
    kind: str = KIND_TRADING
    load_state: str | None = None
    active_state: str | None = None
    sub_state: str | None = None
    unit_file_state: str | None = None


@dataclass(frozen=True)
class ServiceUnitsPanel:
    """Every unit above, and whether the service manager answered at all."""

    available: bool
    generated_at: str
    units: tuple[ServiceUnitRow, ...]
    source: str = "SYSTEMD"
    unavailable_reason: str | None = None


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def systemctl_path() -> str | None:
    """Where `systemctl` is, or None off systemd (a developer's laptop)."""
    return shutil.which("systemctl")


def _query_command(systemctl: str, unit: str) -> list[str]:
    """The exact argument vector, built from literals and one unit name.

    No shell, no caller-supplied verb, and `--no-pager` because a pager
    attaching to a service's stdout is how a status read becomes a hang.
    """
    return [
        systemctl,
        SYSTEMCTL_VERB,
        unit,
        "--no-pager",
        *(f"--property={name}" for name in UNIT_PROPERTIES),
    ]


def read_unit_properties(unit: str, *, systemctl: str | None = None) -> dict[str, str] | None:
    """The four properties of one unit, or None if the manager could not answer.

    Never raises. A dashboard poll that threw because the bus was busy would
    take the whole page down to report one row, and this panel's failure mode
    has to be "I do not know" rather than "everything is fine" or "everything
    is broken".
    """
    binary = systemctl if systemctl is not None else systemctl_path()
    if not binary:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, read-only verb
            _query_command(binary, unit),
            capture_output=True,
            text=True,
            timeout=QUERY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    properties: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        name, separator, value = line.partition("=")
        if separator:
            properties[name.strip()] = value.strip()
    if not properties:
        return None
    return properties


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def _is_masked(properties: dict[str, str]) -> bool:
    """Masked shows up in either field depending on how it was masked."""
    return properties.get("LoadState") == "masked" or properties.get("UnitFileState") in {
        "masked",
        "masked-runtime",
    }


def classify(spec: UnitSpec, properties: dict[str, str] | None) -> ServiceUnitRow:
    """One unit's properties, read against what that unit is supposed to be.

    The mapping is the plain one - active is RUNNING, inactive is STOPPED,
    failed is FAILED - and the whole judgement lives in the tone, which is
    computed against `spec.expectation` rather than from the status word alone.
    """
    common = {
        "key": spec.key,
        "label": spec.label,
        "unit": spec.unit,
        "note": spec.note,
        "kind": spec.kind,
    }

    if properties is None:
        return ServiceUnitRow(
            **common,
            status=STATUS_UNKNOWN,
            tone=TONE_ATTENTION,
            detail=(
                f"The service manager could not be asked about {spec.unit}. This is a "
                "statement about the query, not about the service."
            ),
            expected=False,
            healthy=False,
        )

    load_state = properties.get("LoadState")
    active_state = properties.get("ActiveState")
    sub_state = properties.get("SubState")
    unit_file_state = properties.get("UnitFileState")
    observed = {
        "load_state": load_state,
        "active_state": active_state,
        "sub_state": sub_state,
        "unit_file_state": unit_file_state,
    }
    wants_active = spec.expectation == EXPECT_ACTIVE

    if _is_masked(properties):
        # The status word is the same either way; only the expectation decides
        # whether it is a configuration fact or a missing service.
        expected = not wants_active
        return ServiceUnitRow(
            **common,
            **observed,
            status=STATUS_MASKED,
            tone=TONE_ATTENTION if wants_active else TONE_MUTED,
            detail=(
                f"{spec.unit} is masked and cannot be started. {spec.description}"
                if expected
                else f"{spec.unit} is masked, so it cannot run. {spec.description}"
            ),
            expected=expected,
            healthy=expected,
        )

    if load_state == "not-found":
        return ServiceUnitRow(
            **common,
            **observed,
            status=STATUS_NOT_INSTALLED,
            tone=TONE_NEGATIVE if wants_active else TONE_MUTED,
            detail=f"No unit file named {spec.unit} exists on this host.",
            expected=not wants_active,
            healthy=not wants_active,
        )

    if active_state == "failed" or sub_state == "failed":
        return ServiceUnitRow(
            **common,
            **observed,
            status=STATUS_FAILED,
            tone=TONE_NEGATIVE,
            detail=f"{spec.unit} failed and the service manager is not running it.",
            expected=False,
            healthy=False,
        )

    if active_state == "active":
        return ServiceUnitRow(
            **common,
            **observed,
            status=STATUS_RUNNING,
            tone=TONE_POSITIVE if wants_active else TONE_ATTENTION,
            detail=(
                spec.description
                if wants_active
                # A unit that is supposed to be masked and is instead running is
                # the one case worth shouting about, because somebody unmasked it.
                else f"{spec.unit} is running, and it is configured to be off."
            ),
            expected=wants_active,
            healthy=wants_active,
        )

    if active_state in {"activating", "reloading"}:
        return ServiceUnitRow(
            **common,
            **observed,
            status=STATUS_STARTING,
            tone=TONE_ATTENTION,
            detail=f"{spec.unit} is starting up.",
            expected=False,
            healthy=True,
        )

    if active_state == "deactivating":
        return ServiceUnitRow(
            **common,
            **observed,
            status=STATUS_STOPPING,
            tone=TONE_ATTENTION,
            detail=f"{spec.unit} is shutting down.",
            expected=False,
            healthy=False,
        )

    if active_state == "inactive":
        return ServiceUnitRow(
            **common,
            **observed,
            status=STATUS_STOPPED,
            tone=TONE_NEGATIVE if wants_active else TONE_ATTENTION,
            detail=(
                f"{spec.unit} is not running, and this host expects it to be."
                if wants_active
                # Not masked, not running: the mask this host relies on is gone,
                # which is a configuration drift rather than a healthy off state.
                else f"{spec.unit} is stopped but no longer masked."
            ),
            expected=False,
            healthy=False,
        )

    return ServiceUnitRow(
        **common,
        **observed,
        status=STATUS_UNKNOWN,
        tone=TONE_ATTENTION,
        detail=f"{spec.unit} reported an unrecognised state: {active_state!r}.",
        expected=False,
        healthy=False,
    )


def build_panel(
    *,
    now: datetime,
    specs: tuple[UnitSpec, ...] = SERVICE_UNITS,
) -> ServiceUnitsPanel:
    """Every unit in `specs`, in order, as the service manager describes them."""
    binary = systemctl_path()
    if not binary:
        return ServiceUnitsPanel(
            available=False,
            generated_at=now.isoformat(),
            units=tuple(classify(spec, None) for spec in specs),
            source="UNAVAILABLE",
            unavailable_reason="SYSTEMCTL_NOT_FOUND",
        )
    units = tuple(
        classify(spec, read_unit_properties(spec.unit, systemctl=binary)) for spec in specs
    )
    return ServiceUnitsPanel(
        available=any(unit.status != STATUS_UNKNOWN for unit in units),
        generated_at=now.isoformat(),
        units=units,
    )


__all__ = [
    "EXPECT_ACTIVE",
    "EXPECT_MASKED",
    "KIND_LEGACY",
    "KIND_OBSERVER",
    "KIND_TRADING",
    "QUERY_TIMEOUT_SECONDS",
    "SERVICE_UNITS",
    "STATUS_FAILED",
    "STATUS_MASKED",
    "STATUS_NOT_INSTALLED",
    "STATUS_RUNNING",
    "STATUS_STARTING",
    "STATUS_STOPPED",
    "STATUS_STOPPING",
    "STATUS_UNKNOWN",
    "SYSTEMCTL_VERB",
    "UNIT_PROPERTIES",
    "ServiceUnitRow",
    "ServiceUnitsPanel",
    "UnitSpec",
    "build_panel",
    "classify",
    "read_unit_properties",
    "systemctl_path",
]
