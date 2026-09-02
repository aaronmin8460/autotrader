"""The runtime-unit panel: three equity services, told apart by name.

The bug these tests pin was not a rendering mistake. The operations page read
`autotrader-equity.service`'s trail out of the operational store, labelled the
row "Equity runtime", and reported `STOPPED` - while `autotrader-equity-paper.
service` was active and submitting paper orders from a store that panel never
opens. An operator reading it would conclude equity trading was down.

So the assertions below are mostly about *provenance*: that each label maps to
exactly one unit, that the current equity status is never computed from the
legacy unit, and that "masked" survives all the way to the screen as its own
word with its own colour instead of collapsing into a generic stopped state.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autotrader.dashboard import equity_paper_api, service_units
from autotrader.dashboard.models import (
    TONE_ATTENTION,
    TONE_MUTED,
    TONE_NEGATIVE,
    TONE_POSITIVE,
)

NOW = datetime(2026, 8, 31, 18, 30, tzinfo=UTC)


def spec_for(key: str) -> service_units.UnitSpec:
    return next(spec for spec in service_units.SERVICE_UNITS if spec.key == key)


def active() -> dict[str, str]:
    return {
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "UnitFileState": "enabled",
    }


def masked() -> dict[str, str]:
    return {
        "LoadState": "masked",
        "ActiveState": "inactive",
        "SubState": "dead",
        "UnitFileState": "masked",
    }


def inactive() -> dict[str, str]:
    return {
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "SubState": "dead",
        "UnitFileState": "enabled",
    }


def failed() -> dict[str, str]:
    return {
        "LoadState": "loaded",
        "ActiveState": "failed",
        "SubState": "failed",
        "UnitFileState": "enabled",
    }


# ==========================================================================
# The four labels, and the unit behind each one
# ==========================================================================


def test_each_label_names_exactly_one_unit() -> None:
    """No label may be satisfiable by more than one service.

    "Equity runtime" was the whole defect: three units could answer to it, so
    the row could not be read. Every label here is specific enough that an
    operator can map it to a unit without guessing.
    """
    by_label = {spec.label: spec.unit for spec in service_units.SERVICE_UNITS}
    assert by_label == {
        "Crypto Paper": "autotrader-crypto.service",
        "Equity Paper · EDA-1": "autotrader-equity-paper.service",
        "Equity Shadow": "autotrader-equity-shadow.service",
        "A1-B U30 Shadow": "autotrader-equity-a1b-shadow.service",
        "Legacy Equity Runtime": "autotrader-equity.service",
    }

    units = [spec.unit for spec in service_units.SERVICE_UNITS]
    assert len(set(units)) == len(units)


def test_no_label_is_the_ambiguous_one() -> None:
    """The exact string that caused the misreading is gone."""
    for spec in service_units.SERVICE_UNITS:
        assert spec.label != "Equity runtime"
        assert spec.label != "Equity Runtime"


def test_only_the_legacy_unit_is_expected_to_be_off() -> None:
    expectations = {spec.key: spec.expectation for spec in service_units.SERVICE_UNITS}
    assert expectations == {
        "crypto": service_units.EXPECT_ACTIVE,
        "equity_paper": service_units.EXPECT_ACTIVE,
        "equity_shadow": service_units.EXPECT_ACTIVE,
        "equity_a1b_shadow": service_units.EXPECT_ACTIVE,
        "equity_legacy": service_units.EXPECT_MASKED,
    }


def test_the_two_observers_are_kinds_apart_from_the_two_traders() -> None:
    """A running observer must never render as a running trader.

    The service manager says `active` for both. The `kind` is what lets the
    screen say OBSERVING in the observation colour for one and RUNNING in
    green for the other - and it is declared on the registry, not inferred.
    """
    kinds = {spec.key: spec.kind for spec in service_units.SERVICE_UNITS}
    assert kinds == {
        "crypto": service_units.KIND_TRADING,
        "equity_paper": service_units.KIND_TRADING,
        "equity_shadow": service_units.KIND_OBSERVER,
        "equity_a1b_shadow": service_units.KIND_OBSERVER,
        "equity_legacy": service_units.KIND_LEGACY,
    }
    for spec in service_units.SERVICE_UNITS:
        if spec.kind == service_units.KIND_OBSERVER:
            assert "ZERO ORDERS" in spec.note, spec.key
            assert "OBSERVATION ONLY" in spec.note, spec.key
        row = service_units.classify(spec, active())
        assert row.kind == spec.kind


def test_the_a1b_shadow_row_says_it_cannot_order() -> None:
    row = service_units.classify(spec_for("equity_a1b_shadow"), active())

    assert row.status == "RUNNING"
    assert row.kind == service_units.KIND_OBSERVER
    assert row.note == "OBSERVATION ONLY · ZERO ORDERS"
    assert row.unit == "autotrader-equity-a1b-shadow.service"
    assert "26" in row.detail
    assert "cancel or replace nothing" in row.detail


# ==========================================================================
# Status mapping
# ==========================================================================


def test_equity_paper_active_renders_running() -> None:
    row = service_units.classify(spec_for("equity_paper"), active())

    assert row.status == "RUNNING"
    assert row.tone == TONE_POSITIVE
    assert row.unit == "autotrader-equity-paper.service"
    assert row.healthy


def test_equity_shadow_active_renders_running() -> None:
    row = service_units.classify(spec_for("equity_shadow"), active())

    assert row.status == "RUNNING"
    assert row.tone == TONE_POSITIVE
    assert row.healthy


def test_crypto_active_renders_running() -> None:
    row = service_units.classify(spec_for("crypto"), active())

    assert row.status == "RUNNING"
    assert row.tone == TONE_POSITIVE


def test_legacy_masked_renders_masked_and_not_stopped() -> None:
    """`MASKED` is its own word. It is not `STOPPED` wearing a different colour."""
    row = service_units.classify(spec_for("equity_legacy"), masked())

    assert row.status == "MASKED"
    assert row.status != "STOPPED"


def test_legacy_masked_is_neutral_rather_than_an_error() -> None:
    """Intentionally off must not use the failure colour, or it trains an
    operator to ignore the failure colour."""
    row = service_units.classify(spec_for("equity_legacy"), masked())

    assert row.tone == TONE_MUTED
    assert row.tone not in {TONE_NEGATIVE, TONE_ATTENTION}
    assert row.healthy
    assert row.expected


def test_legacy_masked_detail_says_current_trading_does_not_depend_on_it() -> None:
    row = service_units.classify(spec_for("equity_legacy"), masked())

    assert "masked" in row.detail
    assert "Equity Paper" in row.detail


def test_a_trading_unit_that_is_inactive_is_an_error() -> None:
    row = service_units.classify(spec_for("equity_paper"), inactive())

    assert row.status == "STOPPED"
    assert row.tone == TONE_NEGATIVE
    assert not row.healthy


def test_a_failed_unit_is_red() -> None:
    row = service_units.classify(spec_for("crypto"), failed())

    assert row.status == "FAILED"
    assert row.tone == TONE_NEGATIVE
    assert not row.healthy


def test_a_masked_trading_unit_would_not_be_neutral() -> None:
    """The same status word, judged against a different expectation.

    This is what makes the neutral tone on the legacy row a reading rather than
    a hardcoded exemption: mask a service that is supposed to trade and the
    same code paints it amber.
    """
    row = service_units.classify(spec_for("equity_paper"), masked())

    assert row.status == "MASKED"
    assert row.tone == TONE_ATTENTION
    assert not row.healthy


def test_an_unmasked_legacy_unit_is_flagged_as_drift() -> None:
    """Stopped-but-unmasked is not the state this host is configured for."""
    row = service_units.classify(spec_for("equity_legacy"), inactive())

    assert row.status == "STOPPED"
    assert row.tone == TONE_ATTENTION
    assert not row.expected


def test_a_running_legacy_unit_is_flagged_rather_than_celebrated() -> None:
    row = service_units.classify(spec_for("equity_legacy"), active())

    assert row.status == "RUNNING"
    assert row.tone == TONE_ATTENTION
    assert not row.expected


@pytest.mark.parametrize(
    ("active_state", "expected_status"),
    [
        ("activating", "STARTING"),
        ("reloading", "STARTING"),
        ("deactivating", "STOPPING"),
    ],
)
def test_transitional_states_are_named_rather_than_rounded(
    active_state: str, expected_status: str
) -> None:
    properties = active() | {"ActiveState": active_state}
    row = service_units.classify(spec_for("crypto"), properties)

    assert row.status == expected_status


def test_a_missing_unit_file_is_not_installed() -> None:
    properties = {
        "LoadState": "not-found",
        "ActiveState": "inactive",
        "SubState": "dead",
        "UnitFileState": "",
    }
    row = service_units.classify(spec_for("equity_paper"), properties)

    assert row.status == "NOT INSTALLED"
    assert row.tone == TONE_NEGATIVE


def test_a_masked_runtime_unit_file_state_still_reads_masked() -> None:
    properties = masked() | {"LoadState": "loaded", "UnitFileState": "masked-runtime"}
    row = service_units.classify(spec_for("equity_legacy"), properties)

    assert row.status == "MASKED"
    assert row.tone == TONE_MUTED


# ==========================================================================
# Provenance: the current book is never read off the legacy unit
# ==========================================================================


def test_equity_paper_status_is_never_derived_from_the_legacy_unit() -> None:
    """The regression test for the actual defect.

    The legacy unit is masked and dead. The paper unit is active. Reading both
    through the same code must produce two different answers, and the paper
    answer must not move when the legacy properties change.
    """
    paper = service_units.classify(spec_for("equity_paper"), active())
    legacy = service_units.classify(spec_for("equity_legacy"), masked())

    assert paper.status == "RUNNING"
    assert legacy.status == "MASKED"
    assert paper.unit != legacy.unit

    for legacy_properties in (masked(), inactive(), failed(), active()):
        service_units.classify(spec_for("equity_legacy"), legacy_properties)
        unchanged = service_units.classify(spec_for("equity_paper"), active())
        assert unchanged.status == "RUNNING"
        assert unchanged.tone == TONE_POSITIVE


def test_every_unit_is_queried_by_its_own_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """One query per unit, each naming that unit and no other."""
    asked: list[str] = []

    def fake_read(unit: str, *, systemctl: str | None = None) -> dict[str, str]:
        asked.append(unit)
        return masked() if unit == "autotrader-equity.service" else active()

    monkeypatch.setattr(service_units, "systemctl_path", lambda: "/usr/bin/systemctl")
    monkeypatch.setattr(service_units, "read_unit_properties", fake_read)

    panel = service_units.build_panel(now=NOW)

    assert asked == [
        "autotrader-crypto.service",
        "autotrader-equity-paper.service",
        "autotrader-equity-shadow.service",
        "autotrader-equity-a1b-shadow.service",
        "autotrader-equity.service",
    ]
    statuses = {row.key: row.status for row in panel.units}
    assert statuses == {
        "crypto": "RUNNING",
        "equity_paper": "RUNNING",
        "equity_shadow": "RUNNING",
        "equity_a1b_shadow": "RUNNING",
        "equity_legacy": "MASKED",
    }


def test_the_panel_preserves_the_reading_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_units, "systemctl_path", lambda: "/usr/bin/systemctl")
    monkeypatch.setattr(
        service_units, "read_unit_properties", lambda unit, systemctl=None: active()
    )

    panel = service_units.build_panel(now=NOW)

    assert [row.key for row in panel.units] == [
        "crypto",
        "equity_paper",
        "equity_shadow",
        "equity_a1b_shadow",
        "equity_legacy",
    ]


# ==========================================================================
# Failing to read is never a claim about the service
# ==========================================================================


def test_an_unanswerable_manager_reports_unknown_not_healthy() -> None:
    row = service_units.classify(spec_for("equity_paper"), None)

    assert row.status == "UNKNOWN"
    assert row.status not in {"RUNNING", "STOPPED", "MASKED"}
    assert not row.healthy


def test_no_systemctl_degrades_the_whole_panel_to_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A developer laptop is not a host where every service has failed."""
    monkeypatch.setattr(service_units, "systemctl_path", lambda: None)

    panel = service_units.build_panel(now=NOW)

    assert not panel.available
    assert panel.unavailable_reason == "SYSTEMCTL_NOT_FOUND"
    assert {row.status for row in panel.units} == {"UNKNOWN"}


def test_a_query_that_raises_returns_none_rather_than_propagating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="systemctl", timeout=1.0)

    monkeypatch.setattr(service_units.subprocess, "run", explode)

    assert service_units.read_unit_properties("x.service", systemctl="/usr/bin/systemctl") is None


def test_unparseable_output_is_unknown_rather_than_a_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Completed:
        stdout = "no equals signs here\n"

    monkeypatch.setattr(service_units.subprocess, "run", lambda *a, **k: Completed())

    assert service_units.read_unit_properties("x.service", systemctl="/usr/bin/systemctl") is None


# ==========================================================================
# The reader can only read
# ==========================================================================


def test_the_only_verb_is_show() -> None:
    assert service_units.SYSTEMCTL_VERB == "show"

    command = service_units._query_command("/usr/bin/systemctl", "autotrader-crypto.service")

    assert command[1] == "show"
    assert "--no-pager" in command
    assert command[2] == "autotrader-crypto.service"


def test_the_module_names_no_control_verb() -> None:
    """A substring audit, because a comment claiming read-only proves nothing.

    Anything that could change a unit's state has to be spelled somewhere, and
    the point of this file is that it is spelled nowhere.
    """
    source = Path(service_units.__file__).read_text(encoding="utf-8")

    for forbidden in (
        '"start"',
        '"stop"',
        '"restart"',
        '"enable"',
        '"disable"',
        '"unmask"',
        '"mask"',
        '"kill"',
        '"daemon-reload"',
        "shell=True",
        "os.system",
        "subprocess.Popen",
        "check_call",
    ):
        assert forbidden not in source, f"service_units names {forbidden}"


def test_the_query_uses_a_fixed_argument_vector_and_no_shell() -> None:
    source = Path(service_units.__file__).read_text(encoding="utf-8")

    assert "subprocess.run(" in source
    # `shell=` in any form, not the English word - the module's own prose
    # explains why there is no shell, and prose is not what this asserts.
    assert "shell=" not in source
    assert "shlex" not in source


# ==========================================================================
# The API surface it is served through
# ==========================================================================


@pytest.fixture
def client() -> TestClient:
    return TestClient(equity_paper_api.create_app())


def test_the_services_route_is_get_only(client: TestClient) -> None:
    application = equity_paper_api.create_app()

    for route in application.routes:
        methods = set(getattr(route, "methods", set()) or set())
        assert not methods & {"POST", "PUT", "PATCH", "DELETE"}, getattr(route, "path", route)
        assert methods <= equity_paper_api.ALLOWED_METHODS | {"OPTIONS"}, (
            f"{getattr(route, 'path', route)} exposes {sorted(methods)}"
        )


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_no_mutation_method_reaches_the_services_route(client: TestClient, method: str) -> None:
    response = getattr(client, method)("/api/equity-paper/services")

    assert response.status_code == 405


@pytest.mark.parametrize(
    "path",
    [
        "/api/equity-paper/services/start",
        "/api/equity-paper/services/stop",
        "/api/equity-paper/services/restart",
        "/api/equity-paper/services/unmask",
        "/api/equity-paper/services/autotrader-equity.service",
    ],
)
def test_the_control_routes_someone_might_look_for_do_not_exist(
    client: TestClient, path: str
) -> None:
    assert client.get(path).status_code == 404


def test_no_route_path_names_a_control_action() -> None:
    """The same audit the operational API runs, applied to this application."""
    application = equity_paper_api.create_app()
    control_verbs = {
        "start",
        "stop",
        "restart",
        "enable",
        "disable",
        "unmask",
        "mask",
        "kill",
        "submit",
        "cancel",
        "place",
        "buy",
        "sell",
        "close",
        "liquidate",
        "pause",
        "resume",
        "execute",
        "run",
        "create",
        "update",
        "delete",
        "reset",
        "override",
    }
    for route in application.routes:
        segments = {segment.lower() for segment in str(getattr(route, "path", "")).split("/")}
        assert not segments & control_verbs, getattr(route, "path", route)


def test_the_services_payload_carries_a_row_per_unit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_units, "systemctl_path", lambda: "/usr/bin/systemctl")
    monkeypatch.setattr(
        service_units,
        "read_unit_properties",
        lambda unit, systemctl=None: masked() if unit == "autotrader-equity.service" else active(),
    )

    payload = client.get("/api/equity-paper/services").json()

    assert payload["available"] is True
    rows = {row["key"]: row for row in payload["units"]}
    assert rows["equity_paper"]["label"] == "Equity Paper · EDA-1"
    assert rows["equity_paper"]["status"] == "RUNNING"
    assert rows["equity_shadow"]["status"] == "RUNNING"
    assert rows["crypto"]["status"] == "RUNNING"
    assert rows["equity_legacy"]["label"] == "Legacy Equity Runtime"
    assert rows["equity_legacy"]["status"] == "MASKED"
    assert rows["equity_legacy"]["tone"] == TONE_MUTED


def test_the_payload_states_the_paper_environment(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No real money, said on the row rather than only in a page header."""
    monkeypatch.setattr(service_units, "systemctl_path", lambda: "/usr/bin/systemctl")
    monkeypatch.setattr(
        service_units, "read_unit_properties", lambda unit, systemctl=None: active()
    )

    rows = {row["key"]: row for row in client.get("/api/equity-paper/services").json()["units"]}

    assert "NO REAL MONEY" in rows["equity_paper"]["note"]
    assert "NO REAL MONEY" in rows["crypto"]["note"]
    assert "ZERO ORDERS" in rows["equity_shadow"]["note"]
    assert rows["equity_legacy"]["note"] == "INTENTIONALLY OFF"


def test_no_response_carries_a_credential(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_units, "systemctl_path", lambda: "/usr/bin/systemctl")
    monkeypatch.setattr(
        service_units, "read_unit_properties", lambda unit, systemctl=None: active()
    )

    body = client.get("/api/equity-paper/services").text

    for secret in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "api_key", "secret", "password"):
        assert secret not in body
