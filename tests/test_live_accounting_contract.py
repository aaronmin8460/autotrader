"""The frozen contract, asserted against the thing that implements it.

Prompt 3 builds a dashboard against `live_accounting_contract.json`. Once that
work has begun, a field renamed here is not a refactor - it is a broken page in
somebody else's repository, discovered at the worst possible time.

So the contract is a *file*, and this suite compares the implementation to it
mechanically. Renaming, removing or silently adding a field fails here. If the
implementation genuinely needs a field to change, the change stops and is
explained first; the JSON and the document are updated deliberately, together,
and the version is bumped.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest

from autotrader.liveaccounting import readmodel, service, store
from autotrader.liveaccounting.models import ProfitReservePolicy

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "live_accounting_contract.json"
DOCUMENT_PATH = Path(__file__).resolve().parents[1] / "docs" / "LIVE_ACCOUNTING_API_CONTRACT.md"

FINGERPRINT = "a6bbf9c116a5679c58719d82d7e4b3e2"
T0 = datetime(2026, 9, 8, 13, 0, tzinfo=UTC)


@pytest.fixture
def contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text())


@pytest.fixture
def payload(tmp_path: Path) -> dict:
    policy = ProfitReservePolicy()
    connection = sqlite3.connect(tmp_path / "live.db", isolation_level=None)
    connection.row_factory = sqlite3.Row
    store.initialize(connection)
    store.stamp_metadata(connection, account_fingerprint=FINGERPRINT, now=T0)
    service.establish_inception(
        connection,
        service.BrokerSnapshot(T0, Decimal("50"), Decimal("50"), 0, 0),
        account_fingerprint=FINGERPRINT,
        policy=policy,
        now=T0,
    )
    try:
        return readmodel.build_summary(
            connection, now=T0, expected_fingerprint=FINGERPRINT, policy=policy
        )
    finally:
        connection.close()


def test_the_contract_file_exists_and_says_it_is_frozen(contract: dict) -> None:
    assert contract["frozen"] is True
    assert contract["contract_version"] == readmodel.CONTRACT_VERSION
    assert contract["base_sha"].startswith("fd6cae4")
    assert contract["methods_allowed"] == ["GET", "HEAD"]


def test_every_contract_field_is_present_in_the_payload(contract: dict, payload: dict) -> None:
    promised = {field["name"] for field in contract["fields"]}
    delivered = set(payload)
    assert promised - delivered == set(), "the implementation is missing a promised field"


def test_the_payload_adds_no_field_the_contract_did_not_promise(
    contract: dict, payload: dict
) -> None:
    """An undeclared field is drift too: Prompt 3 would start depending on it."""
    promised = {field["name"] for field in contract["fields"]}
    assert set(payload) - promised == set(), "the implementation emits an undeclared field"


def test_the_minimum_fields_the_program_mandated_are_all_declared(contract: dict) -> None:
    promised = {field["name"] for field in contract["fields"]}
    mandated = {
        "accounting_inception_at",
        "baseline_equity",
        "current_broker_equity",
        "current_cash",
        "confirmed_deposits",
        "confirmed_withdrawals",
        "net_external_flows",
        "flow_adjusted_equity",
        "trading_pnl_since_inception",
        "realized_pnl",
        "unrealized_pnl",
        "time_weighted_return",
        "adjusted_equity_hwm",
        "current_drawdown_from_adjusted_hwm",
        "new_hwm_profit",
        "profit_reserve_rate",
        "profit_reserve_accrued",
        "withdrawal_bucket_balance",
        "withdrawal_preview",
        "withdrawal_authorized",
        "withdrawal_mode",
        "last_external_flow_at",
        "last_accounting_checkpoint_at",
        "accounting_status",
        "data_freshness",
        "live_ready",
        "live_armed",
    }
    assert mandated <= promised, mandated - promised


def test_no_forbidden_alias_appears_anywhere(contract: dict, payload: dict) -> None:
    """One canonical name per concept, enforced rather than requested."""
    forbidden = {
        "adjusted_equity",
        "performance_equity",
        "normalized_equity",
        "hwm",
        "peak_equity",
        "high_water",
        "available_to_withdraw",
        "withdrawable",
        "reserved_cash",
        "cash_reserve",
    }
    promised = {field["name"] for field in contract["fields"]}
    assert forbidden & promised == set()
    assert forbidden & set(payload) == set()
    assert "flow_adjusted_equity" in promised


def test_every_declared_type_matches_what_the_payload_carries(
    contract: dict, payload: dict
) -> None:
    """A decimal_string field must carry an exact decimal string, or null."""
    for field in contract["fields"]:
        name, declared = field["name"], field["type"]
        value = payload[name]
        if value is None:
            assert field["nullable"], f"{name} came back null but is declared non-nullable"
            continue
        if declared == "decimal_string":
            assert isinstance(value, str), name
            try:
                Decimal(value)
            except InvalidOperation:  # pragma: no cover - the assertion is the point
                pytest.fail(f"{name} is not an exact decimal: {value!r}")
            assert "E" not in value.upper(), f"{name} uses exponent notation: {value!r}"
        elif declared == "iso8601_utc":
            assert isinstance(value, str), name
            assert datetime.fromisoformat(value).tzinfo is not None, name
        elif declared == "boolean":
            assert isinstance(value, bool), name
        elif declared == "integer":
            assert isinstance(value, int) and not isinstance(value, bool), name
        elif declared == "string":
            assert isinstance(value, str), name
        else:  # pragma: no cover - a new type would need a new branch
            pytest.fail(f"{name} declares an unknown type {declared!r}")


def test_a_non_nullable_field_is_never_null_even_under_suppression(
    contract: dict, tmp_path: Path
) -> None:
    """The fail-closed payload still satisfies the contract's shape.

    A dashboard written against the contract must render an outage, not crash
    on it - so suppression may null the *nullable* figures and nothing else.
    """
    connection = sqlite3.connect(tmp_path / "bare.db", isolation_level=None)
    connection.row_factory = sqlite3.Row
    store.initialize(connection)
    store.stamp_metadata(connection, account_fingerprint=FINGERPRINT, now=T0)
    try:
        payload = readmodel.build_summary(connection, now=T0, expected_fingerprint=FINGERPRINT)
    finally:
        connection.close()

    assert payload["accounting_status"] == "NOT_INITIALIZED"
    for field in contract["fields"]:
        if not field["nullable"]:
            assert payload[field["name"]] is not None, field["name"]


def test_the_observe_only_fields_are_declared_as_constants(contract: dict) -> None:
    by_name = {field["name"]: field for field in contract["fields"]}
    for name in (
        "withdrawal_mode",
        "withdrawal_authorized",
        "authorized_withdrawal_amount",
        "automatic_transfer_enabled",
    ):
        assert by_name[name]["nullable"] is False
        assert (
            "observe_only_semantics" in by_name[name]
            or "ALWAYS" in by_name[name]["calculation"].upper()
        )


def test_the_payload_holds_observe_only_no_matter_what_is_asked(payload: dict) -> None:
    assert payload["withdrawal_mode"] == "OBSERVE_ONLY"
    assert payload["withdrawal_authorized"] is False
    assert payload["authorized_withdrawal_amount"] == "0.00"
    assert payload["automatic_transfer_enabled"] is False


def test_the_document_and_the_json_agree_on_the_canonical_names(contract: dict) -> None:
    text = DOCUMENT_PATH.read_text()
    assert contract["contract_version"] in text
    for name in (
        "flow_adjusted_equity",
        "adjusted_equity_hwm",
        "profit_reserve_accrued",
        "withdrawal_bucket_balance",
        "CONSERVATIVE_MIN",
        "OBSERVE_ONLY",
    ):
        assert name in text, f"{name} is not explained in the contract document"


def test_the_contract_warns_that_the_profit_reserve_is_not_the_cash_reserve(
    contract: dict,
) -> None:
    """Two different things with similar names is how a reader loses real money."""
    warning = contract["terminology_warning"]
    assert "EDA-1 cash reserve" in warning
    assert "sizing" in warning
    text = DOCUMENT_PATH.read_text()
    assert "EDA-1 Cash Reserve" in text
    assert "Profit Reserve" in text


def test_live_safety_fields_are_declared_as_passed_through(contract: dict) -> None:
    by_name = {field["name"]: field for field in contract["fields"]}
    for name in ("live_ready", "live_armed"):
        assert by_name[name]["nullable"] is True
        assert "NEVER" in by_name[name]["calculation"]


def test_the_live_safety_fields_are_never_computed_by_this_package(payload: dict) -> None:
    """Unreadable stays unknown. A null arm state must never render as DISARMED."""
    assert payload["live_ready"] is None
    assert payload["live_armed"] is None
