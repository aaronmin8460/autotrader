"""Integration tests: a risk decision persisted as a risk event.

The risk engine and the state module were built independently and stay that
way. The risk engine is a pure calculator that has never heard of SQLite, and
the state module stores `decision` and `reason_code` as opaque text rather than
importing the risk engine's vocabulary. Nothing in `src/` joins them.

That independence is only safe if the two contracts actually fit, so these
tests do the joining themselves - exactly as a future orchestrator would:
call `evaluate_risk`, then *separately* call `record_risk_event`. The mapping
below lives here, in the test, on purpose. Promoting it into either package
would create the coupling both phases were designed to avoid.

Every test is offline and writes only into pytest's `tmp_path`.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from autotrader.risk import (
    APPROVED,
    TRADING_DISABLED,
    RiskContext,
    RiskDecision,
    RiskRequest,
    RiskSide,
    evaluate_risk,
)
from autotrader.state import (
    connect,
    initialize_database,
    list_risk_events,
    record_risk_event,
)

#: Stable textual values for the generic `risk_events.decision` column. The
#: risk engine reports approval as a bool; the audit trail stores text. This
#: two-value mapping is the whole of the adaptation, and it is test-local.
DECISION_APPROVED = "APPROVED"
DECISION_REJECTED = "REJECTED"

T0 = datetime(2026, 8, 27, 14, 30, tzinfo=UTC)


def decision_text(decision: RiskDecision) -> str:
    """The textual form of `decision.approved` used by these tests."""
    return DECISION_APPROVED if decision.approved else DECISION_REJECTED


@pytest.fixture
def connection(tmp_path: Path):
    with connect(initialize_database(tmp_path / "state.db")) as open_connection:
        yield open_connection


def flat_account(*, trading_enabled: bool = True) -> RiskContext:
    """A flat, funded, unhalted account with room under every cap."""
    return RiskContext(
        equity=200_000.0,
        cash=200_000.0,
        total_exposure=0.0,
        symbol_exposure=0.0,
        current_position_quantity=Decimal(0),
        daily_pnl=0.0,
        start_of_day_equity=200_000.0,
        trading_enabled=trading_enabled,
    )


@pytest.mark.parametrize(
    ("trading_enabled", "expected_decision", "expected_reason"),
    [
        (True, DECISION_APPROVED, APPROVED),
        (False, DECISION_REJECTED, TRADING_DISABLED),
    ],
)
def test_risk_decision_round_trips_through_risk_events(
    connection, trading_enabled: bool, expected_decision: str, expected_reason: str
) -> None:
    """A decision, persisted explicitly, reads back with its meaning intact."""
    request = RiskRequest(
        symbol="BTC/USD",
        side=RiskSide.BUY,
        reference_price=100.0,
        requested_quantity=Decimal("0.05"),
    )

    decision = evaluate_risk(request, flat_account(trading_enabled=trading_enabled))
    assert decision.reason_code == expected_reason

    # The caller joins the layers - not the engine, and not the database.
    record_risk_event(
        connection,
        event_timestamp=T0,
        decision=decision_text(decision),
        reason_code=decision.reason_code,
        symbol=request.symbol,
        message=decision.message,
    )

    (stored,) = list_risk_events(connection)
    assert stored.decision == expected_decision
    assert stored.reason_code == decision.reason_code
    assert stored.message == decision.message
    assert stored.symbol == request.symbol
    assert stored.event_timestamp == T0


def test_every_reason_code_survives_storage(connection) -> None:
    """`reason_code` is stored verbatim; the schema constrains no vocabulary."""
    from autotrader.risk import REASON_CODES

    for index, code in enumerate(REASON_CODES):
        record_risk_event(
            connection,
            event_timestamp=T0,
            decision=DECISION_APPROVED if code == APPROVED else DECISION_REJECTED,
            reason_code=code,
            symbol="BTC/USD",
            message=f"case {index}",
        )

    assert [event.reason_code for event in list_risk_events(connection)] == list(REASON_CODES)


def test_evaluating_risk_writes_no_database(tmp_path: Path) -> None:
    """`evaluate_risk` has no persistence side effect, even with a db to hand."""
    database = initialize_database(tmp_path / "state.db")
    with connect(database) as open_connection:
        before = list_risk_events(open_connection)

        for _ in range(3):
            evaluate_risk(
                RiskRequest(
                    symbol="BTC/USD",
                    side=RiskSide.BUY,
                    reference_price=100.0,
                    requested_quantity=Decimal(10),
                ),
                flat_account(),
            )

        assert list_risk_events(open_connection) == before == []


def test_recording_a_risk_event_evaluates_nothing(connection) -> None:
    """The audit trail stores what it is told, including a nonsensical pairing."""
    record_risk_event(
        connection,
        event_timestamp=T0,
        decision=DECISION_APPROVED,
        reason_code=TRADING_DISABLED,
        symbol="BTC/USD",
        message="stored verbatim; the database does not second-guess it",
    )

    (stored,) = list_risk_events(connection)
    assert stored.decision == DECISION_APPROVED
    assert stored.reason_code == TRADING_DISABLED


def code_without_prose(source: str) -> str:
    """`source` with every docstring and comment removed.

    The independence guarantee is about *code*. The risk engine's prose points
    a reader at where the UTC-day baseline is persisted, which is documentation
    of a boundary rather than a crossing of it.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_neither_package_imports_the_other() -> None:
    """The separation is structural, not merely conventional."""
    import autotrader.risk.engine as engine
    import autotrader.state.sqlite as state_sqlite

    risk_source = code_without_prose(Path(engine.__file__).read_text())
    state_source = code_without_prose(Path(state_sqlite.__file__).read_text())

    assert "sqlite" not in risk_source.lower()
    assert "autotrader.state" not in risk_source
    assert "autotrader.risk" not in state_source
    assert "evaluate_risk" not in state_source
