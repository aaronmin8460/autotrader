"""Production Live orchestration, against read-only broker doubles only."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from autotrader.equity.live import (
    LIVE_CLIENT_ORDER_ID_PREFIX,
    new_live_client_order_id,
)
from autotrader.equity.session import MarketSession
from autotrader.liveaccounting import readmodel, store
from autotrader.liveaccounting.models import (
    FLOW_UNKNOWN_EXTERNAL,
    RELATION_PRE_INCEPTION,
    STATUS_CLEAN,
)
from autotrader.liveops import operations
from test_equity_execution import make_account, make_order

FINGERPRINT = "a6bbf9c116a5679c58719d82d7e4b3e2"
T0 = datetime(2026, 9, 8, 13, 0, tzinfo=UTC)


class ReadOnlyBroker:
    """Only the reads the accounting orchestrator can name."""

    def __init__(self, *, activities=None, orders=None, equity: str = "50", cash: str = "50"):
        self.account = make_account(equity=equity, cash=cash)
        self.activities = activities or {}
        self.orders = orders or []
        self.reads = 0

    def get_account(self):
        self.reads += 1
        return self.account

    def get_all_positions(self):
        self.reads += 1
        return []

    def get_orders(self, request=None):
        self.reads += 1
        return list(self.orders)

    def get_account_activities(self, activity_type, after=None):
        self.reads += 1
        return list(self.activities.get(activity_type, ()))


class Calendar:
    def __init__(self, session: MarketSession | None):
        self.session = session

    def session_for(self, day: date):
        if self.session is not None and self.session.session_date == day:
            return self.session
        return None

    def sessions_between(self, start: date, end: date):
        if self.session is not None and start <= self.session.session_date <= end:
            return (self.session,)
        return ()


def session(*, day: date, close_hour: int) -> MarketSession:
    return MarketSession(
        session_date=day,
        open_utc=datetime(day.year, day.month, day.day, close_hour - 7, 30, tzinfo=UTC),
        close_utc=datetime(day.year, day.month, day.day, close_hour, 0, tzinfo=UTC),
    )


def freeze(database: Path, broker: ReadOnlyBroker, *, now: datetime = T0) -> None:
    operations.freeze_accounting_inception(
        database,
        broker,
        account_fingerprint=FINGERPRINT,
        now=now,
        source_sha="f" * 40,
    )


def test_live_client_order_ids_have_a_distinct_bounded_namespace() -> None:
    first = new_live_client_order_id()
    second = new_live_client_order_id()
    assert first.startswith(LIVE_CLIENT_ORDER_ID_PREFIX)
    assert first != second
    assert len(first) == 48
    assert not first.startswith("autotrader-") or first.startswith("autotrader-live-")


def test_inception_dry_run_never_creates_the_production_ledger(tmp_path: Path) -> None:
    database = tmp_path / "never-created.db"
    result = operations.freeze_accounting_inception(
        database,
        ReadOnlyBroker(),
        account_fingerprint=FINGERPRINT,
        now=T0,
        source_sha="f" * 40,
        dry_run=True,
    )
    assert result.dry_run is True
    assert result.broker_equity == 50
    assert not database.exists()


def test_opening_funding_is_retained_as_pre_inception_not_profit(tmp_path: Path) -> None:
    database = tmp_path / "live-accounting.db"
    broker = ReadOnlyBroker(
        activities={
            "CSD": [
                {
                    "id": "opening-capital",
                    "activity_type": "CSD",
                    "net_amount": "50",
                    "status": "executed",
                    "created_at": "2026-09-07T17:00:00Z",
                }
            ]
        }
    )
    freeze(database, broker)

    with store.connect_read_only(database) as connection:
        (flow,) = store.read_flows(connection)
        summary = readmodel.build_summary(
            connection,
            now=T0,
            expected_fingerprint=FINGERPRINT,
        )
    assert flow.relation == RELATION_PRE_INCEPTION
    assert summary["accounting_status"] == STATUS_CLEAN
    assert summary["baseline_equity"] == "50.00"
    assert summary["confirmed_deposits"] == "0.00"
    assert summary["trading_pnl_since_inception"] == "0.00"


def test_inception_refuses_any_historical_order(tmp_path: Path) -> None:
    broker = ReadOnlyBroker(orders=[make_order("autotrader-prior")])
    with pytest.raises(operations.LiveOperationsError, match="1 order"):
        freeze(tmp_path / "refused.db", broker)


def test_unknown_external_flow_fails_closed_before_a_checkpoint(tmp_path: Path) -> None:
    database = tmp_path / "live-accounting.db"
    freeze(database, ReadOnlyBroker())
    broker = ReadOnlyBroker(
        activities={
            "JNLC": [
                {
                    "id": "ambiguous-journal",
                    "activity_type": "JNLC",
                    "net_amount": "10",
                    "status": "executed",
                    "created_at": "2026-09-08T18:00:00Z",
                }
            ]
        }
    )
    with pytest.raises(operations.LiveOperationsError, match="UNKNOWN_EXTERNAL_FLOW"):
        operations.sync_accounting(
            database,
            broker,
            account_fingerprint=FINGERPRINT,
            now=datetime(2026, 9, 8, 18, 5, tzinfo=UTC),
        )
    with store.connect_read_only(database) as connection:
        assert store.read_flows(connection)[0].classification == FLOW_UNKNOWN_EXTERNAL
        checkpoints = store.read_checkpoints(connection)
    assert len(checkpoints) == 1  # inception only; no optimistic intraday mark


def test_daily_close_is_idempotent_for_the_broker_session(tmp_path: Path) -> None:
    database = tmp_path / "live-accounting.db"
    broker = ReadOnlyBroker(equity="53", cash="53")
    freeze(database, broker)
    market_session = session(day=T0.date(), close_hour=20)
    first = operations.daily_close(
        database,
        broker,
        Calendar(market_session),
        account_fingerprint=FINGERPRINT,
        now=datetime(2026, 9, 8, 20, 10, tzinfo=UTC),
    )
    second = operations.daily_close(
        database,
        broker,
        Calendar(market_session),
        account_fingerprint=FINGERPRINT,
        now=datetime(2026, 9, 8, 20, 40, tzinfo=UTC),
    )
    assert first.skipped is False
    assert second.skipped is True
    assert second.checkpoint_id == first.checkpoint_id
    with store.connect_read_only(database) as connection:
        daily = [row for row in store.read_checkpoints(connection) if row.kind == "DAILY_CLOSE"]
    assert len(daily) == 1


def test_daily_close_uses_the_calendar_close_across_dst(tmp_path: Path) -> None:
    database = tmp_path / "live-accounting.db"
    inception = datetime(2026, 10, 30, 20, 0, tzinfo=UTC)
    broker = ReadOnlyBroker()
    freeze(database, broker, now=inception)
    standard_time = session(day=date(2026, 11, 2), close_hour=21)

    before = operations.daily_close(
        database,
        broker,
        Calendar(standard_time),
        account_fingerprint=FINGERPRINT,
        now=datetime(2026, 11, 2, 20, 59, tzinfo=UTC),
        dry_run=True,
    )
    after = operations.daily_close(
        database,
        broker,
        Calendar(standard_time),
        account_fingerprint=FINGERPRINT,
        now=datetime(2026, 11, 2, 21, 1, tzinfo=UTC),
        dry_run=True,
    )
    assert before.skipped is True
    assert after.skipped is False
    assert after.snapshot_at.hour == 21
