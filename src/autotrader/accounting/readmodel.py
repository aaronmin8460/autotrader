"""What the dashboard reads: aggregates over the ledger, and nothing else.

Read-only in the way that matters - the connection is opened `mode=ro` with
`query_only`, so a viewer process cannot create the file, migrate it, or write
a row even by accident.

**Vocabulary, deliberately.** A partial sale is a **realized event**, never a
"trade". This book trims positions on drift, so a symbol can produce a dozen
sales without ever closing anything, and calling each of those a trade would
make "win rate" a statistic about rebalancing noise. Round-trip trade
boundaries are not defined by this system, so nothing here reports a figure
that would need them.

**Three different P&L numbers, kept apart.** Daily *account* P&L is equity
minus the stored UTC-day baseline and belongs to the risk engine; this module
does not compute it, change it, or read it. *Unrealized* P&L stays the
broker's own figure over broker positions. What is here is the third thing:
realized trade P&L, from confirmed sales. They are not required to sum, and
the payload says so rather than leaving a viewer to discover it.

**Fees and dividends are not folded in.** Where the broker charges regulatory
fees as a daily account-level total rather than per execution, they are not
attributable to a symbol and are reported separately or not at all. Dividend
income is not trade P&L and has no path into these numbers.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from autotrader.accounting import reconcile, store
from autotrader.accounting.models import STATUS_MISMATCH

#: Money as shown. The ledger keeps ten decimal places; a screen wants cents.
DISPLAY_QUANTUM = Decimal("0.01")

TONE_NEUTRAL = "NEUTRAL"
TONE_POSITIVE = "POSITIVE"
TONE_NEGATIVE = "NEGATIVE"
TONE_ATTENTION = "ATTENTION"
TONE_MUTED = "MUTED"


def _money(value: Decimal | None) -> float | None:
    """A money figure for display, rounded to cents at the last moment.

    A float, and only here. Everything upstream of this function is Decimal;
    this is the boundary where an exact ledger figure becomes something JSON
    can carry to a browser, and it is one-way.
    """
    if value is None:
        return None
    return float(value.quantize(DISPLAY_QUANTUM))


def _exact(value: Decimal | None) -> str | None:
    """The same figure, exact, as text. Carried alongside the rounded one."""
    return None if value is None else store.decimal_text(value)


def _tone(value: Decimal | None) -> str:
    if value is None:
        return TONE_MUTED
    if value > 0:
        return TONE_POSITIVE
    if value < 0:
        return TONE_NEGATIVE
    return TONE_NEUTRAL


def utc_day(now: datetime) -> str:
    return now.astimezone(UTC).date().isoformat()


#: `historical_completeness` meaning "there is nothing before the horizon" -
#: the replay reached the first execution the account ever had.
COMPLETENESS_WHOLE_HISTORY = "EXACT_REPLAY_FROM_ACCOUNT_OPEN"


def tracking_label(tracking_started_at: str | None, completeness: str | None) -> str:
    """What horizon the figures cover, in words a screen can show verbatim.

    Always names the timestamp, and says "all time" never. The horizon here is
    the account's **first confirmed execution**, which on this deployment is
    earlier than the strategy's activation - a hand-run submission smoke came
    first - so calling it "since activation" would be both wrong and
    flattering. When the replay reached that first execution the label adds
    that nothing precedes it; otherwise it is a bare "since", and a reader is
    right to assume there is history the ledger does not have.
    """
    if not tracking_started_at:
        return "REALIZED P&L NOT YET TRACKED"
    try:
        moment = datetime.fromisoformat(tracking_started_at).astimezone(UTC)
        stamp = moment.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:  # pragma: no cover - metadata is written by this package
        stamp = tracking_started_at
    if completeness == COMPLETENESS_WHOLE_HISTORY:
        return f"REALIZED SINCE {stamp} · WHOLE CONFIRMED HISTORY"
    return f"REALIZED SINCE {stamp}"


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RealizedEventRow:
    """One sale, as a screen shows it."""

    event_id: int
    symbol: str
    realized_at: str
    realized_date_utc: str
    side: str
    quantity: str
    execution_price: float
    average_cost_before: float
    released_cost_basis: float
    gross_proceeds: float
    net_realized_pnl: float
    net_realized_pnl_exact: str
    fees: float
    quantity_after: str
    provenance: str
    broker_order_id: str
    broker_execution_id: str | None
    tone: str


@dataclass(frozen=True)
class SymbolRealized:
    """One symbol's realized totals, plus what the ledger holds for it."""

    symbol: str
    realized_today: float
    realized_since_tracking: float
    realized_today_exact: str
    realized_since_tracking_exact: str
    event_count: int
    event_count_today: int
    quantity: str | None
    average_cost: float | None
    total_cost_basis: float | None
    accounting_status: str
    tone_today: str
    tone_since: str


@dataclass(frozen=True)
class AccountingStatusPanel:
    """Whether these numbers may be presented as authoritative, and why."""

    status: str
    tone: str
    tracking_started_at: str | None
    tracking_label: str
    bootstrap_method: str | None
    historical_completeness: str | None
    basis_method: str | None
    execution_granularity: str
    symbols_checked: int
    quantity_mismatches: int
    cost_deviations: int
    basis_divergences: int
    last_reconciled_at: str | None
    last_sync_at: str | None
    last_sync_status: str | None
    message: str | None


@dataclass(frozen=True)
class RealizedSummary:
    """The account-level realized figures, and the statistics that mean something."""

    realized_today: float
    realized_since_tracking: float
    realized_today_exact: str
    realized_since_tracking_exact: str
    event_count: int
    event_count_today: int
    winning_events: int
    losing_events: int
    flat_events: int
    average_winner: float | None
    average_loser: float | None
    tone_today: str
    tone_since: str
    utc_day: str
    status: AccountingStatusPanel
    #: Stated on the wire so a renderer asserts it rather than a reader assuming
    #: it. Daily account P&L, realized P&L and unrealized P&L are three
    #: different measurements and are not required to sum.
    components_are_independent: bool = True
    fills_imported: int = 0
    symbols: tuple[SymbolRealized, ...] = field(default=())


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------


def _sum(connection: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> Decimal:
    total = Decimal(0)
    for row in connection.execute(sql, params):
        total += store.text_decimal(row["net_realized_pnl"])
    return total


def _column(row: sqlite3.Row, name: str, default: object) -> object:
    """One column of a row, or a default when this build knows a column the file does not."""
    try:
        value = row[name]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def build_status(connection: sqlite3.Connection) -> AccountingStatusPanel:
    """Everything a viewer needs to decide whether to believe the numbers."""
    metadata = store.read_metadata(connection)
    run = reconcile.latest(connection)
    sync = store.latest_sync_run(connection)

    if metadata is None:
        return AccountingStatusPanel(
            status=store.RECON_UNKNOWN,
            tone=TONE_ATTENTION,
            tracking_started_at=None,
            tracking_label="REALIZED P&L NOT YET TRACKED",
            bootstrap_method=None,
            historical_completeness=None,
            basis_method=None,
            execution_granularity="UNKNOWN",
            symbols_checked=0,
            quantity_mismatches=0,
            cost_deviations=0,
            basis_divergences=0,
            last_reconciled_at=None,
            last_sync_at=None if sync is None else str(sync["completed_at"]),
            last_sync_status=None if sync is None else str(sync["status"]),
            message="The ledger has not been bootstrapped, so there is no horizon to report.",
        )

    stopped = connection.execute(
        "SELECT COUNT(*) AS n FROM position_cost_basis WHERE accounting_status = ?",
        (STATUS_MISMATCH,),
    ).fetchone()
    stopped_count = 0 if stopped is None else int(stopped["n"])

    if run is None:
        status = store.RECON_UNKNOWN
        message = "The ledger has never been reconciled against the broker."
        checked = mismatches = deviations = divergences = 0
        reconciled_at = None
    else:
        status = str(run["status"])
        message = run["message"]
        checked = int(run["symbols_checked"])
        mismatches = int(run["quantity_mismatches"])
        deviations = int(run["cost_deviations"])
        # A read-only viewer can be running the new build against a ledger the
        # writer has not migrated yet - the two are deployed as one tree but
        # start seconds apart, and the migration happens on the write path.
        # A column that is not there yet reads as zero, not as a 500.
        divergences = int(_column(run, "basis_divergences", 0))
        reconciled_at = str(run["run_at"])

    if stopped_count:
        status = store.RECON_MISMATCH
        note = f"{stopped_count} symbol(s) have stopped accounting after a refused execution"
        message = f"{message}; {note}" if message else note

    granularity_row = connection.execute(
        "SELECT DISTINCT execution_granularity FROM accounting_fills"
    ).fetchall()
    granularities = sorted(str(row[0]) for row in granularity_row)
    granularity = (
        granularities[0] if len(granularities) == 1 else ("MIXED" if granularities else "UNKNOWN")
    )

    tone = {
        store.RECON_CLEAN: TONE_POSITIVE,
        # Neutral, not positive and not attention. The ledger and the broker
        # agree about every share and every price; they recognise a stated
        # amount of P&L on different days. That is a fact to read, not a fault
        # to chase - and not something to colour green either.
        store.RECON_BASIS_DIVERGENCE: TONE_NEUTRAL,
        store.RECON_DEGRADED: TONE_ATTENTION,
        store.RECON_MISMATCH: TONE_NEGATIVE,
        store.RECON_UNKNOWN: TONE_ATTENTION,
    }.get(status, TONE_ATTENTION)

    label = tracking_label(metadata.tracking_started_at, metadata.historical_completeness)

    return AccountingStatusPanel(
        status=status,
        tone=tone,
        tracking_started_at=metadata.tracking_started_at,
        tracking_label=label,
        bootstrap_method=metadata.bootstrap_method,
        historical_completeness=metadata.historical_completeness,
        basis_method=metadata.basis_method,
        execution_granularity=granularity,
        symbols_checked=checked,
        quantity_mismatches=mismatches,
        cost_deviations=deviations,
        basis_divergences=divergences,
        last_reconciled_at=reconciled_at,
        last_sync_at=None if sync is None else str(sync["completed_at"]),
        last_sync_status=None if sync is None else str(sync["status"]),
        message=message,
    )


def build_summary(connection: sqlite3.Connection, *, now: datetime) -> RealizedSummary:
    """Account-level realized totals for today and since tracking started."""
    day = utc_day(now)
    since = _sum(connection, "SELECT net_realized_pnl FROM realized_pnl_events", ())
    today = _sum(
        connection,
        "SELECT net_realized_pnl FROM realized_pnl_events WHERE realized_date_utc = ?",
        (day,),
    )

    counts = connection.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN realized_date_utc = ? THEN 1 ELSE 0 END) AS today,
            SUM(CASE WHEN CAST(net_realized_pnl AS REAL) > 0 THEN 1 ELSE 0 END) AS winners,
            SUM(CASE WHEN CAST(net_realized_pnl AS REAL) < 0 THEN 1 ELSE 0 END) AS losers
        FROM realized_pnl_events
        """,
        (day,),
    ).fetchone()
    total = int(counts["total"] or 0)
    today_count = int(counts["today"] or 0)
    winners = int(counts["winners"] or 0)
    losers = int(counts["losers"] or 0)

    winner_total = _sum(
        connection,
        "SELECT net_realized_pnl FROM realized_pnl_events WHERE CAST(net_realized_pnl AS REAL) > 0",
        (),
    )
    loser_total = _sum(
        connection,
        "SELECT net_realized_pnl FROM realized_pnl_events WHERE CAST(net_realized_pnl AS REAL) < 0",
        (),
    )

    fills = connection.execute("SELECT COUNT(*) AS n FROM accounting_fills").fetchone()

    return RealizedSummary(
        realized_today=_money(today) or 0.0,
        realized_since_tracking=_money(since) or 0.0,
        realized_today_exact=store.decimal_text(today),
        realized_since_tracking_exact=store.decimal_text(since),
        event_count=total,
        event_count_today=today_count,
        winning_events=winners,
        losing_events=losers,
        flat_events=total - winners - losers,
        average_winner=_money(winner_total / winners) if winners else None,
        average_loser=_money(loser_total / losers) if losers else None,
        tone_today=_tone(today),
        tone_since=_tone(since),
        utc_day=day,
        status=build_status(connection),
        fills_imported=0 if fills is None else int(fills["n"]),
        symbols=tuple(build_by_symbol(connection, now=now)),
    )


def build_by_symbol(connection: sqlite3.Connection, *, now: datetime) -> list[SymbolRealized]:
    """Per-symbol realized totals, joined to the cost basis the ledger holds."""
    day = utc_day(now)
    basis = {
        str(row["symbol"]): row for row in connection.execute("SELECT * FROM position_cost_basis")
    }
    aggregated: dict[str, dict[str, object]] = {}
    for row in connection.execute(
        "SELECT symbol, net_realized_pnl, realized_date_utc FROM realized_pnl_events"
    ):
        symbol = str(row["symbol"])
        bucket = aggregated.setdefault(
            symbol, {"since": Decimal(0), "today": Decimal(0), "n": 0, "n_today": 0}
        )
        value = store.text_decimal(row["net_realized_pnl"])
        bucket["since"] = bucket["since"] + value  # type: ignore[operator]
        bucket["n"] = int(bucket["n"]) + 1  # type: ignore[call-overload]
        if str(row["realized_date_utc"]) == day:
            bucket["today"] = bucket["today"] + value  # type: ignore[operator]
            bucket["n_today"] = int(bucket["n_today"]) + 1  # type: ignore[call-overload]

    rows: list[SymbolRealized] = []
    for symbol in sorted(set(basis) | set(aggregated)):
        bucket = aggregated.get(symbol) or {
            "since": Decimal(0),
            "today": Decimal(0),
            "n": 0,
            "n_today": 0,
        }
        since = bucket["since"]
        today = bucket["today"]
        assert isinstance(since, Decimal) and isinstance(today, Decimal)
        held = basis.get(symbol)
        rows.append(
            SymbolRealized(
                symbol=symbol,
                realized_today=_money(today) or 0.0,
                realized_since_tracking=_money(since) or 0.0,
                realized_today_exact=store.decimal_text(today),
                realized_since_tracking_exact=store.decimal_text(since),
                event_count=int(bucket["n"]),  # type: ignore[call-overload]
                event_count_today=int(bucket["n_today"]),  # type: ignore[call-overload]
                quantity=None if held is None else str(held["quantity"]),
                average_cost=(
                    None
                    if held is None or held["average_cost"] is None
                    else _money(store.text_decimal(held["average_cost"]))
                ),
                total_cost_basis=(
                    None if held is None else _money(store.text_decimal(held["total_cost_basis"]))
                ),
                accounting_status=("UNTRACKED" if held is None else str(held["accounting_status"])),
                tone_today=_tone(today),
                tone_since=_tone(since),
            )
        )
    return rows


def build_events(
    connection: sqlite3.Connection, *, symbol: str | None = None, limit: int = 50
) -> list[RealizedEventRow]:
    """Realized events, newest first. One row per confirmed sale."""
    sql = """
        SELECT r.*, f.broker_order_id, f.broker_execution_id
        FROM realized_pnl_events AS r
        JOIN accounting_fills AS f ON f.accounting_event_id = r.accounting_event_id
    """
    params: tuple[object, ...] = ()
    if symbol:
        sql += " WHERE r.symbol = ?"
        params = (symbol,)
    sql += " ORDER BY r.realized_at DESC, r.event_id DESC LIMIT ?"
    params = (*params, int(limit))

    rows: list[RealizedEventRow] = []
    for row in connection.execute(sql, params):
        net = store.text_decimal(row["net_realized_pnl"])
        rows.append(
            RealizedEventRow(
                event_id=int(row["event_id"]),
                symbol=str(row["symbol"]),
                realized_at=str(row["realized_at"]),
                realized_date_utc=str(row["realized_date_utc"]),
                side="SELL",
                quantity=str(row["quantity"]),
                execution_price=_money(store.text_decimal(row["execution_price"])) or 0.0,
                average_cost_before=_money(store.text_decimal(row["average_cost_before"])) or 0.0,
                released_cost_basis=_money(store.text_decimal(row["released_cost_basis"])) or 0.0,
                gross_proceeds=_money(store.text_decimal(row["gross_proceeds"])) or 0.0,
                net_realized_pnl=_money(net) or 0.0,
                net_realized_pnl_exact=store.decimal_text(net),
                fees=_money(store.text_decimal(row["fees"])) or 0.0,
                quantity_after=str(row["quantity_after"]),
                provenance=str(row["provenance"]),
                broker_order_id=str(row["broker_order_id"]),
                broker_execution_id=row["broker_execution_id"],
                tone=_tone(net),
            )
        )
    return rows


__all__ = [
    "COMPLETENESS_WHOLE_HISTORY",
    "DISPLAY_QUANTUM",
    "AccountingStatusPanel",
    "RealizedEventRow",
    "RealizedSummary",
    "SymbolRealized",
    "build_by_symbol",
    "build_events",
    "build_status",
    "build_summary",
    "tracking_label",
    "utc_day",
]
