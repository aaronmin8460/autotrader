"""Phase 6: the smallest safe fractional round trip on the CURRENT paper account.

BUY 0.002 SPY (~$1.50), wait for the fill, SELL the same 0.002, wait for the
fill, and prove the account came back to exactly where it started. Both legs go
through `execute_equity_paper_order` with the fractional form and the hard-cap
risk policy - the byte-identical path the migrated runtime submits through -
including the durable intent, the duplicate preflight, the exactly-once
submission, and the composite cross-store lock.

PAPER ONLY, structurally: the one client factory hardcodes the paper host, and
this module proves the host and the account prefix before anything else. It is
meant to run in the migration window while the runtime service is stopped, as
the operator's controlled smoke, with the store the runtime itself uses.

    python -m studies.equity_eda1_sizing.paper_fractional_smoke --db <store>
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from autotrader.account.lock import (
    AccountExecutionLock,
    CompositeAccountLock,
    account_lock_path_for,
)
from autotrader.equity.allocation import (
    POLICY_FRACTIONAL_RESERVED_90,
    allocation_policy_for,
    risk_policy_for,
)
from autotrader.equity.paper import require_paper_account
from autotrader.execution.equity import (
    create_market_data_client,
    execute_equity_paper_order,
)
from autotrader.execution.models import OrderSide
from autotrader.execution.paper import (
    broker_symbol_key,
    create_paper_trading_client,
    fetch_paper_positions,
    find_broker_order_by_client_id,
)
from autotrader.state.sqlite import connect

SYMBOL = "SPY"
QUANTITY = Decimal("0.002")
FILL_TIMEOUT_SECONDS = 90.0
POLL_SECONDS = 2.0


def wait_for_fill(client, client_order_id: str) -> dict[str, object]:
    deadline = time.monotonic() + FILL_TIMEOUT_SECONDS
    while True:
        snapshot = find_broker_order_by_client_id(client, client_order_id)
        if snapshot is not None and snapshot.status.lower() == "filled":
            return {
                "client_order_id": client_order_id,
                "status": snapshot.status,
                "filled_quantity": str(snapshot.filled_quantity),
                "filled_average_price": snapshot.filled_average_price,
            }
        if time.monotonic() >= deadline:
            return {
                "client_order_id": client_order_id,
                "status": snapshot.status if snapshot is not None else "NOT_FOUND",
                "filled_quantity": str(snapshot.filled_quantity) if snapshot else "0",
                "timed_out": True,
            }
        time.sleep(POLL_SECONDS)


def spy_quantity(client) -> Decimal:
    positions = fetch_paper_positions(client)
    held = positions.get(broker_symbol_key(SYMBOL))
    return held.quantity if held is not None else Decimal(0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--crypto-db", type=Path, required=True)
    args = parser.parse_args()

    policy = allocation_policy_for(POLICY_FRACTIONAL_RESERVED_90)
    limits = risk_policy_for(policy)
    client = create_paper_trading_client()
    account_number = require_paper_account(client)
    data_client = create_market_data_client()
    lock = CompositeAccountLock(
        (
            AccountExecutionLock(account_lock_path_for(args.crypto_db), read_only=True),
            AccountExecutionLock(account_lock_path_for(args.db)),
        )
    )

    report: dict[str, object] = {
        "account_number": account_number,
        "symbol": SYMBOL,
        "quantity": str(QUANTITY),
        "started_at": datetime.now(UTC).isoformat(),
    }

    with connect(args.db) as connection:
        outstanding = connection.execute(
            "SELECT COUNT(*) FROM order_intents WHERE status IN ('CREATED','SUBMITTING','UNKNOWN')"
        ).fetchone()[0]
        if outstanding:
            raise SystemExit(f"{outstanding} unresolved intent(s); refusing to smoke-test.")

        start_quantity = spy_quantity(client)
        report["start_quantity"] = str(start_quantity)

        buy = execute_equity_paper_order(
            connection,
            symbol=SYMBOL,
            side=OrderSide.BUY,
            requested_quantity=QUANTITY,
            trading_client=client,
            data_client=data_client,
            fractional=True,
            risk_policy=limits,
            account_lock=lock,
        )
        report["buy"] = {
            "outcome": buy.outcome.value,
            "risk": buy.risk_decision.reason_code,
            "sent_quantity": str(buy.submitted_quantity),
        }
        assert buy.intent is not None
        report["buy_fill"] = wait_for_fill(client, buy.intent.client_order_id)

        sell = execute_equity_paper_order(
            connection,
            symbol=SYMBOL,
            side=OrderSide.SELL,
            requested_quantity=QUANTITY,
            trading_client=client,
            data_client=data_client,
            fractional=True,
            risk_policy=limits,
            account_lock=lock,
        )
        report["sell"] = {
            "outcome": sell.outcome.value,
            "risk": sell.risk_decision.reason_code,
            "sent_quantity": str(sell.submitted_quantity),
        }
        assert sell.intent is not None
        report["sell_fill"] = wait_for_fill(client, sell.intent.client_order_id)

        end_quantity = spy_quantity(client)
        report["end_quantity"] = str(end_quantity)
        report["round_trip_exact"] = end_quantity == start_quantity

        rows = connection.execute(
            "SELECT client_order_id, COUNT(*) FROM order_intents GROUP BY client_order_id"
            " HAVING COUNT(*) > 1"
        ).fetchall()
        report["duplicate_client_order_ids"] = len(rows)

    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
