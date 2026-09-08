"""The real-money safety panel, and the promise that it cannot act.

Two halves. The first proves the panel reports what Prompt 1's own functions
say - the ceilings especially, which are quoted from `effective_ceilings` rather
than recomputed, and which this suite checks against the figures Prompt 1
published in its own risk report. The second proves the service has no way to
change anything: no write route, no mutation call reachable from the module, and
no credential or account number anywhere in a payload bound for a browser.

The accounting figures are deliberately absent from all of it. Flow-adjusted
equity, trading P&L, the high-water mark, the profit reserve and the withdrawal
bucket belong to the frozen contract on its own service, and a second
implementation of any of them here would be the exact defect the contract's
canonical-naming rule exists to prevent.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autotrader.dashboard import live_api, live_safety
from autotrader.equity.allocation import (
    POLICY_LIVE_VALIDATION_100,
    allocation_policy_for,
)
from autotrader.execution.live import LiveReadOnlyClient
from autotrader.live.armstate import ArmState
from autotrader.live.budget import CashFlowEvent
from autotrader.live.identity import account_fingerprint
from autotrader.state.sqlite import initialize_database

PINNED = "a6bbf9c116a5679c58719d82d7e4b3e2"
NOW = datetime(2026, 9, 8, 14, 5, tzinfo=UTC)


@pytest.fixture
def policy():
    return allocation_policy_for(POLICY_LIVE_VALIDATION_100)


def account(**overrides) -> live_safety.LiveAccountFacts:
    base = {
        "status": live_safety.ACCOUNT_OK,
        "equity": "50.00",
        "cash": "50.00",
        "buying_power": "50.00",
        "account_status": "ACTIVE",
        "account_type": "CASH",
        "multiplier": "1",
        "shorting_enabled": False,
        "trading_blocked": False,
        "account_blocked": False,
        "transfers_blocked": False,
        "position_count": 0,
        "open_order_count": 0,
        "read_at": NOW.isoformat(),
    }
    base.update(overrides)
    return live_safety.LiveAccountFacts(**base)


# ---------------------------------------------------------------- ceilings --


@pytest.mark.parametrize(
    ("equity", "target", "hard", "bound", "per_symbol", "binding"),
    [
        # Straight from Prompt 1's own `risk_limits.json`. If the dashboard and
        # that report ever disagree, one of them is lying to an operator.
        ("50.00", "45.00", "47.50", "50.00", "5.50", "balance"),
        ("60.00", "54.00", "57.00", "60.00", "6.60", "balance"),
        ("75.00", "67.50", "71.25", "75.00", "8.25", "balance"),
        ("99.99", "89.99", "94.99", "99.99", "10.99", "balance"),
        ("100.00", "90.00", "95.00", "100.00", "11.00", "balance"),
        ("150.00", "90.00", "95.00", "100.00", "11.00", "authorization"),
        ("1000.00", "90.00", "95.00", "100.00", "11.00", "authorization"),
    ],
)
def test_the_panel_reports_the_ceilings_prompt_one_published(
    policy, equity, target, hard, bound, per_symbol, binding
) -> None:
    envelope = live_safety.build_risk_envelope(policy, account=account(equity=equity))
    assert envelope.status == live_safety.CEILINGS_RESOLVED
    assert envelope.target_gross == target
    assert envelope.hard_gross == hard
    assert envelope.exposure_bound == bound
    assert envelope.per_symbol == per_symbol
    assert envelope.binding == binding


def test_funding_above_the_authorization_does_not_raise_authorized_risk(policy) -> None:
    """The rule the operator has to understand, stated as an assertion."""
    at_hundred = live_safety.build_risk_envelope(policy, account=account(equity="100.00"))
    at_ten_thousand = live_safety.build_risk_envelope(policy, account=account(equity="10000.00"))
    assert at_hundred.target_gross == at_ten_thousand.target_gross == "90.00"
    assert at_hundred.hard_gross == at_ten_thousand.hard_gross == "95.00"
    assert at_hundred.exposure_bound == at_ten_thousand.exposure_bound == "100.00"
    assert at_hundred.per_symbol == at_ten_thousand.per_symbol == "11.00"


def test_the_policy_identity_is_the_allocators_own(policy) -> None:
    envelope = live_safety.build_risk_envelope(policy, account=account())
    assert envelope.policy_id == "LIVE_VALIDATION_100"
    # The digest Prompt 1 recorded. A changed parameter changes this hash, so a
    # dashboard showing a policy the risk engine is not running fails here.
    assert envelope.policy_config_hash == (
        "ecf003eb9a86814f2583c4e981bf39365e942e0e4dcc683c6c742fb3c00292d5"
    )
    assert envelope.daily_loss_halt_fraction == "0.02"
    assert envelope.capital_bound == "100"


def test_an_unreadable_balance_yields_no_ceilings_rather_than_zero_ones(policy) -> None:
    """An account whose equity could not be read has unknown headroom, not none."""
    envelope = live_safety.build_risk_envelope(
        policy, account=account(status=live_safety.ACCOUNT_UNREADABLE, equity=None)
    )
    assert envelope.status == live_safety.CEILINGS_NOT_VERIFIED
    assert envelope.target_gross is None
    assert envelope.hard_gross is None
    assert envelope.exposure_bound is None
    assert envelope.per_symbol is None
    assert envelope.binding is None
    assert envelope.detail is not None
    # The policy constants survive, because they do not depend on a balance.
    assert envelope.capital_bound == "100"


def test_ceilings_are_never_resolved_from_a_remembered_balance(policy) -> None:
    """There is no configuration field for capital this account does not have."""
    envelope = live_safety.build_risk_envelope(
        policy, account=account(status=live_safety.ACCOUNT_NOT_CONFIGURED, equity=None)
    )
    assert envelope.status == live_safety.CEILINGS_NOT_VERIFIED
    assert envelope.verified_equity is None


def test_remaining_capacity_is_measured_against_the_hard_cap(policy) -> None:
    envelope = live_safety.build_risk_envelope(
        policy, account=account(), gross_exposure=Decimal("20.00")
    )
    assert envelope.current_gross_exposure == "20.00"
    assert envelope.remaining_gross_capacity == "27.50"  # 47.50 - 20.00


def test_remaining_capacity_floors_at_zero_rather_than_going_negative(policy) -> None:
    envelope = live_safety.build_risk_envelope(
        policy, account=account(), gross_exposure=Decimal("60.00")
    )
    assert envelope.remaining_gross_capacity == "0.00"


# -------------------------------------------------------------------- arm --


def test_a_disarmed_switch_reports_disarmed_with_its_reason() -> None:
    state = ArmState(
        state="DISARMED",
        reason="nothing has authorized submission",
        source="DEFAULT",
        changed_at=None,
    )
    panel = live_safety.build_arm(state)
    assert panel.state == "DISARMED"
    assert panel.armed is False
    assert panel.reason


def test_an_unreadable_arm_switch_is_unknown_and_never_false() -> None:
    """A switch nobody can read has not been proven off."""
    panel = live_safety.build_arm(None)
    assert panel.state == live_safety.ARM_UNKNOWN
    assert panel.armed is None
    assert panel.armed is not False


def test_anything_that_is_not_the_literal_armed_string_is_not_armed() -> None:
    state = ArmState(state="Armed", reason="", source="", changed_at=None)
    assert live_safety.build_arm(state).armed is False


def test_both_arm_gates_are_required_for_the_dashboard_to_say_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable = ArmState(
        state="ARMED",
        reason="operator authorized a later session",
        source="operator",
        changed_at=NOW,
    )
    monkeypatch.setenv("AUTOTRADER_LIVE_ARMED", "false")
    closed = live_safety.build_arm(durable)
    assert closed.durable_state == "ARMED"
    assert closed.environment_gate_open is False
    assert closed.state == "DISARMED"
    assert closed.armed is False

    monkeypatch.setenv("AUTOTRADER_LIVE_ARMED", "true")
    opened = live_safety.build_arm(durable)
    assert opened.environment_gate_open is True
    assert opened.state == "ARMED"
    assert opened.armed is True


# --------------------------------------------------------------- identity --


def test_a_matching_account_is_reported_pinned_with_a_short_fingerprint() -> None:
    panel = live_safety.build_identity(observed_fingerprint=PINNED, expected_fingerprint=PINNED)
    assert panel.status == live_safety.IDENTITY_PINNED
    assert panel.fingerprint_short == "a6bbf9c1"
    assert len(panel.fingerprint_short) == live_safety.FINGERPRINT_DISPLAY_CHARS


def test_a_mismatched_account_fails_closed_and_shows_no_figure(policy) -> None:
    """A payload from the wrong account is discarded, not displayed."""
    panel = live_safety.build_panel(
        now=NOW,
        policy=policy,
        account=account(equity="999999.00"),
        arm_state=None,
        observed_fingerprint="0" * 32,
        expected_fingerprint=PINNED,
    )
    assert panel.identity.status == live_safety.IDENTITY_MISMATCH
    assert panel.account.status == live_safety.ACCOUNT_MISMATCH
    assert panel.account.equity is None
    assert panel.risk.status == live_safety.CEILINGS_NOT_VERIFIED
    assert any("pinned account" in notice for notice in panel.notices)


def test_an_unpinned_deployment_says_so_rather_than_claiming_a_match() -> None:
    panel = live_safety.build_identity(observed_fingerprint=PINNED, expected_fingerprint=None)
    assert panel.status == live_safety.IDENTITY_NOT_PINNED
    assert panel.detail is not None


def test_an_unpinned_deployment_hides_readable_account_figures(policy) -> None:
    panel = live_safety.build_panel(
        now=NOW,
        policy=policy,
        account=account(equity="1000.00"),
        arm_state=None,
        observed_fingerprint=PINNED,
        expected_fingerprint=None,
    )
    assert panel.identity.status == live_safety.IDENTITY_NOT_PINNED
    assert panel.account.status == live_safety.ACCOUNT_MISMATCH
    assert panel.account.equity is None
    assert panel.risk.status == live_safety.CEILINGS_NOT_VERIFIED


# ------------------------------------------------------- deposit-day guard --


def test_a_cash_movement_on_the_risk_day_activates_the_guard() -> None:
    events = [
        CashFlowEvent(
            activity_id="1",
            activity_type="CSD",
            amount=Decimal("50.00"),
            transaction_time=datetime(2026, 9, 8, 14, 0, tzinfo=UTC),
        )
    ]
    panel = live_safety.build_deposit_day_guard(events, risk_day=date(2026, 9, 8))
    assert panel.status == live_safety.GUARD_ACTIVE
    assert panel.active is True
    assert panel.cash_flow_count == 1
    # The operator's sentence is quoted, not paraphrased.
    assert "exits remain available" in panel.reason


def test_a_trading_only_day_leaves_the_guard_inactive() -> None:
    panel = live_safety.build_deposit_day_guard([], risk_day=date(2026, 9, 8))
    assert panel.status == live_safety.GUARD_INACTIVE
    assert panel.active is False
    assert panel.reason is None


def test_an_unreadable_activity_feed_is_unknown_not_a_clean_day() -> None:
    panel = live_safety.build_deposit_day_guard(None, risk_day=date(2026, 9, 8))
    assert panel.status == live_safety.GUARD_UNKNOWN
    assert panel.active is None
    assert panel.active is not False


# ---------------------------------------------------------------- account --


class _ExplodingClient:
    """A broker whose failure message carries something that must never be shown."""

    def get_account(self) -> object:
        raise RuntimeError("auth failed for key AKIA-SECRET-1234")


def test_a_missing_credential_and_an_unreadable_broker_are_different_states() -> None:
    assert live_safety.read_live_account(None).status in {
        live_safety.ACCOUNT_NOT_CONFIGURED,
        live_safety.ACCOUNT_UNREADABLE,
    }

    read = live_safety.read_live_account(_ExplodingClient())
    assert read.status == live_safety.ACCOUNT_UNREADABLE
    # The exception text is discarded rather than forwarded: it is the likeliest
    # place for a credential fragment to appear, and this output reaches a browser.
    assert "SECRET" not in repr(read)
    assert "AKIA" not in repr(read)


class _Client:
    """A broker client that answers a read and can do nothing else.

    One method wide, deliberately: a fake carrying a submission method would be
    a fake this package could be written against.
    """

    def __init__(self, account: object) -> None:
        self._account = account

    def get_account(self) -> object:
        return self._account


def test_the_account_is_read_as_exact_decimal_text_never_a_float() -> None:
    class Account:
        equity = "103.37"
        cash = "61.40"
        buying_power = "61.40"
        status = "ACTIVE"
        multiplier = "1"
        shorting_enabled = False
        trading_blocked = False
        account_blocked = False
        transfers_blocked = False

    read = live_safety.read_live_account(_Client(Account()), position_count=4, open_order_count=0)
    assert read.equity == "103.37"
    assert isinstance(read.equity, str)
    assert read.account_type == "CASH"
    assert read.position_count == 4


def test_a_margin_multiplier_is_reported_as_margin() -> None:
    class Account:
        equity = "100.00"
        cash = "100.00"
        multiplier = "4"

    assert live_safety.read_live_account(_Client(Account())).account_type == "MARGIN"


def test_a_broker_field_that_vanished_costs_that_field_and_not_the_read() -> None:
    class Sparse:
        equity = "50.00"

    read = live_safety.read_live_account(_Client(Sparse()))
    assert read.status == live_safety.ACCOUNT_OK
    assert read.equity == "50.00"
    assert read.multiplier is None
    assert read.account_type is None
    assert read.trading_blocked is None  # unknown, not False


# ---------------------------------------------------------------- service --


def test_the_live_unit_defaults_to_not_installed_which_is_the_expected_state(policy) -> None:
    panel = live_safety.build_panel(
        now=NOW, policy=policy, account=account(), arm_state=None, expected_fingerprint=None
    )
    assert panel.service.state == live_safety.SERVICE_NOT_INSTALLED
    assert panel.service.unit == "autotrader-equity-live.service"
    assert "first-day runbook" in panel.service.detail


def test_the_five_service_states_are_distinct_values() -> None:
    states = {
        live_safety.SERVICE_NOT_INSTALLED,
        live_safety.SERVICE_DISABLED,
        live_safety.SERVICE_STOPPED,
        live_safety.SERVICE_RUNNING,
        live_safety.SERVICE_UNKNOWN,
    }
    assert len(states) == 5


# ------------------------------------------------------------------- HTTP --


def test_every_route_is_a_read() -> None:
    app = live_api.create_app()
    for route in app.routes:
        methods = getattr(route, "methods", set())
        assert methods <= live_api.ALLOWED_METHODS | {"OPTIONS"}, (
            f"{getattr(route, 'path', route)} exposes {methods}. This service describes an "
            "account holding real money; the absence of a write route is the only thing "
            "between a dashboard viewer and a real-money order."
        )


def test_the_service_declares_only_get_and_head() -> None:
    assert frozenset({"GET", "HEAD"}) == live_api.ALLOWED_METHODS


def test_the_port_does_not_collide_with_the_frozen_accounting_service() -> None:
    """8005 is reserved by the contract for Prompt 2."""
    assert live_api.DEFAULT_PORT == 8006
    assert live_api.DEFAULT_HOST == "127.0.0.1"


def test_health_opens_nothing_and_says_it_is_read_only() -> None:
    client = TestClient(live_api.create_app())
    response = client.get("/api/live-safety/health")
    assert response.status_code == 200
    body = response.json()
    assert body["read_only"] is True
    assert body["environment"] == "LIVE"


def test_the_summary_answers_without_credentials_and_fails_closed() -> None:
    client = TestClient(live_api.create_app())
    response = client.get("/api/live-safety/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["environment"] == "LIVE"
    # No credentials in this process, so the account is not readable and the
    # ceilings are not resolved. Neither becomes a zero.
    assert body["account"]["status"] in {"NOT_CONFIGURED", "UNREADABLE"}
    assert body["account"]["equity"] is None
    assert body["risk"]["status"] == "NOT_VERIFIED"
    assert body["risk"]["target_gross"] is None
    assert body["arm"]["armed"] is None
    assert body["live_ready"] is None


def test_a_write_to_any_route_is_refused() -> None:
    client = TestClient(live_api.create_app())
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)("/api/live-safety/summary")
        assert response.status_code in {404, 405}


def test_the_real_panel_uses_broker_and_local_truth_without_a_mutation_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokerAccount:
        account_number = "LIVE-TEST-8990"
        equity = "50.00"
        cash = "50.00"
        buying_power = "50.00"
        status = "ACTIVE"
        multiplier = "1"
        shorting_enabled = False
        trading_blocked = False
        account_blocked = False
        transfers_blocked = False

    class Position:
        market_value = "4.25"

    class Reader:
        def get_account(self):
            return BrokerAccount()

        def get_all_positions(self):
            return [Position()]

        def get_orders(self, request=None):
            return []

        def get_account_activities(self, activity_type, after=None):
            return []

    database = tmp_path / "live.db"
    initialize_database(database)
    monkeypatch.setenv("AUTOTRADER_EQUITY_LIVE_DB", str(database))
    monkeypatch.setenv("AUTOTRADER_LIVE_READY", "true")
    monkeypatch.setenv("AUTOTRADER_LIVE_ARMED", "false")
    monkeypatch.setenv("AUTOTRADER_DEPLOYED_SHA", "d" * 40)
    monkeypatch.setenv(
        "AUTOTRADER_LIVE_ACCOUNT_FINGERPRINT",
        account_fingerprint(BrokerAccount.account_number),
    )
    monkeypatch.setattr(live_safety, "create_live_read_broker", lambda: Reader())
    monkeypatch.setattr(
        live_api,
        "read_unit_properties",
        lambda unit: {
            "LoadState": "loaded",
            "ActiveState": "active",
            "SubState": "running",
            "UnitFileState": "enabled",
        },
    )

    panel = live_api.build_panel(now=NOW)
    assert panel.live_ready is True
    assert panel.arm.state == "DISARMED"
    assert panel.arm.environment_gate_open is False
    assert panel.identity.status == "PINNED"
    assert panel.account.account_type == "CASH"
    assert panel.account.position_count == 1
    assert panel.account.open_order_count == 0
    assert panel.risk.current_gross_exposure == "4.25"
    assert panel.risk.target_gross == "45.00"
    assert panel.deposit_day_guard.status == "INACTIVE"
    assert panel.service.state == "RUNNING"
    assert panel.code_sha == "d" * 40


def test_the_live_read_facade_is_explicit_and_pages_with_a_bound() -> None:
    class RawClient:
        _base_url = "live"
        _sandbox = False

        def __init__(self):
            self.requests = []

        def get(self, path, params):
            self.requests.append((path, params))
            if len(self.requests) == 1:
                return [
                    {"id": "one", "activity_type": "CSD"},
                    {"id": "two", "activity_type": "CSD"},
                ]
            return []

    raw = RawClient()
    reader = LiveReadOnlyClient(raw)  # type: ignore[arg-type]
    rows = reader.get_account_activities("CSD", NOW, page_size=2)
    assert [row["id"] for row in rows] == ["one", "two"]
    assert reader.read_count == 2
    assert raw.requests[1][1]["page_token"] == "two"
    for forbidden in (
        "submit_order",
        "cancel_order",
        "replace_order",
        "close_position",
        "create_transfer",
    ):
        assert not hasattr(reader, forbidden)


# ------------------------------------------------------- structural safety --


def _executable_source(module) -> str:
    """The module's code with every docstring removed.

    These modules discuss arming, withdrawal and order submission at length and
    must be able to; what must not exist is a call.
    """
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


@pytest.mark.parametrize("module", [live_safety, live_api])
def test_no_mutation_entry_point_is_reachable_from_the_live_dashboard(module) -> None:
    code = _executable_source(module)
    for forbidden in (
        "arm_live_trading",
        "disarm_live_trading",
        "submit_order",
        "cancel_order",
        "cancel_orders",
        "replace_order",
        "close_position",
        "close_all_positions",
        "create_transfer",
        "request_withdrawal",
        "create_withdrawal",
        # Call shapes, not words. Both modules DESCRIBE withdrawal and transfers
        # in their prose - the whole point of the description is to say that
        # neither exists here - so the check is for a call, not a mention.
        ".post(",
        ".put(",
        ".patch(",
        ".delete(",
    ):
        assert forbidden not in code, f"{module.__name__} reaches {forbidden}"


@pytest.mark.parametrize("module", [live_safety, live_api])
def test_the_module_never_names_a_credential_variable(module) -> None:
    """The real-money credential vocabulary lives in one audited boundary only.

    `tests/test_execution_paper.py` enforces this repository-wide; it is
    asserted here too because this module is the one most likely to want to
    name those variables, and delegating to `execution.live` instead is a
    decision that should fail loudly if it is ever undone.
    """
    code = _executable_source(module)
    # The credential vocabulary, not the environment as such: `live_api` reads
    # its port from the environment exactly as every sibling service does, and
    # a port is not a secret.
    for forbidden in ("ALPACA_LIVE", "TRADING_LIVE", "API_KEY", "SECRET_KEY"):
        assert forbidden not in code, f"{module.__name__} names {forbidden}"


def test_the_panel_carries_no_account_number_and_no_secret(policy) -> None:
    panel = live_safety.build_panel(
        now=NOW,
        policy=policy,
        account=account(),
        arm_state=None,
        observed_fingerprint=PINNED,
        expected_fingerprint=PINNED,
        gross_exposure=Decimal("0.00"),
        cash_flow_events=[],
        live_ready=True,
    )
    rendered = repr(panel)
    for forbidden in ("api_key", "secret", "token", "password", "ALPACA_LIVE"):
        assert forbidden.lower() not in rendered.lower(), f"the panel names {forbidden}"
    # The fingerprint is a digest, not an account number, and is 32 hex chars.
    assert len(panel.identity.fingerprint) == 32
    assert all(character in "0123456789abcdef" for character in panel.identity.fingerprint)


def test_the_panel_computes_no_accounting_figure() -> None:
    """Flow-adjusted equity and its family belong to the frozen contract."""
    code = _executable_source(live_safety) + _executable_source(live_api)
    for accounting_field in (
        "flow_adjusted_equity",
        "trading_pnl_since_inception",
        "adjusted_equity_hwm",
        "profit_reserve",
        "withdrawal_bucket",
        "withdrawal_preview",
        "time_weighted_return",
    ):
        assert accounting_field not in code, (
            f"the safety panel implements {accounting_field}, which is the accounting "
            "contract's field. Two implementations of one number is the defect the "
            "contract's canonical-naming rule exists to prevent."
        )


def test_the_live_dashboard_touches_no_other_programs_module() -> None:
    """EDA-1, V3, A1-B, crypto and paper trading are not in the import surface."""
    source = Path(inspect.getfile(live_safety)).read_text(encoding="utf-8")
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    joined = "\n".join(imports)
    for forbidden in ("autotrader.crypto", "autotrader.shadow", "autotrader.decision"):
        assert forbidden not in joined, f"live_safety imports {forbidden}"
