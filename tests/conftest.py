"""Shared test helpers.

Deliberately tiny. There is no global fixture here that any test silently
depends on, and nothing here is autouse: a test's preconditions should be
visible in the test file that needs them.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from autotrader import state
from autotrader.execution.models import TRADABLE_SYMBOLS

#: A fixed instant for establishing a test's starting account safety. Distinct
#: from the timestamps tests use for their own events, so a halt written *by* a
#: test is always distinguishable from this precondition.
ACCOUNT_SAFETY_ESTABLISHED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def establish_account_safety(connection: sqlite3.Connection) -> None:
    """Put the shared account state where a reconciled system starts: SAFE.

    A freshly initialized database has never had a reconciliation pass, so
    `read_account_safety_state` correctly reports `UNSAFE_RECONCILIATION` and
    the execution boundary refuses to submit. That is the real production
    sequence - a runtime reconciles the full universe at startup and only then
    trades - and a test exercising the boundary has to start from the same
    place rather than from a state no live process ever submits in.

    Written through the storage primitive rather than through
    `account.safety.restore_account_safety`, because the point here is to set up
    a precondition, not to exercise the policy that decides it. The tests that
    do exercise that policy call it directly and assert on what it writes.
    """
    state.set_account_safety_state(
        connection,
        account_state=state.ACCOUNT_SAFETY_SAFE,
        reason=(
            f"Test precondition: a full-universe pass over all {len(TRADABLE_SYMBOLS)} "
            "tracked symbols established that broker truth is understood."
        ),
        source="test-precondition",
        client_order_id=None,
        updated_at=ACCOUNT_SAFETY_ESTABLISHED_AT,
    )
