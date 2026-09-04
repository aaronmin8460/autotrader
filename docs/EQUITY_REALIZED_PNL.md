# Equity realized P&L — accounting, deployment and operation

Accounting only. Nothing described here places, cancels or replaces an order,
changes a limit, sizes a position, starts or stops a runtime, or edits a
trading store. No trading decision reads anything this system writes, and the
test suite asserts the dependency in both directions.

**The rule that outranks everything else in this document:** the ledger is
subordinate to broker truth. When the two disagree, the ledger is what is
wrong. Displaying `REALIZED P&L UNKNOWN` is always better than displaying a
precise incorrect dollar amount.

---

## What the ledger is built from

Broker-confirmed **executions**, and nothing else. Not an intent, not a
submitted order, not a pending order, not a requested quantity, and never a
simulated observer action.

The broker publishes execution-level detail — one record per fill, with that
fill's own quantity and price — so the ledger stores one row per execution
rather than one per order. That is not a stylistic choice: on this account a
third of the filled orders have more than one execution behind them, and the
largest has five. The order-level aggregate would have collapsed those into a
single average and lost the ability to reconcile a partial fill.

The equity runtime's own `broker_orders` table is **not** the source of truth.
It is one row per order intent with `filled_quantity` and
`filled_average_price` — a version coarser than the broker — and it is
consulted read-only, for one thing: establishing which orders that runtime
placed.

| | source | granularity |
|---|---|---|
| accounting ledger | broker execution record | one row per execution |
| equity paper store | the runtime's own writes | one row per order intent |

---

## Accounting semantics

Weighted-average cost, long-only, exact decimals.

**Buy** — adds quantity, adds cost, releases nothing:

    quantity     += fill.quantity
    total_basis  += fill.quantity * fill.price + fill.fees

Both are additions of exact `Decimal`s, so a purchase introduces **no rounding
at all**. This is why `total_cost_basis` is the stored figure and the average
is derived from it: keeping the average instead would divide on every purchase
and accumulate error in the one number that must still agree with the broker a
thousand fills later.

**Sell** — releases a proportional slice:

    released     = total_basis * sell_qty / prior_qty
    proceeds     = sell_qty * price
    gross_pnl    = proceeds - released
    net_pnl      = gross_pnl - fees
    quantity    -= sell_qty
    total_basis -= released

and leaves the average cost of the remaining shares **unchanged**. A partial
sale never re-prices what is left.

**The full-exit case is exact, not nearly exact.** When a sale takes the whole
position, `released` is the entire remaining basis by construction. A position
opened, trimmed any number of times and finally closed releases exactly its
original total cost basis — no residual dust.

**Rounding.** The partial-sale slice is the only division, quantized to ten
decimal places with banker's rounding, and the quantized value is what is
subtracted from the stored basis, so the ledger's arithmetic closes exactly.
Intermediate values are never rounded to cents. Presentation rounds to cents at
the last moment; the exact figure is carried alongside on the wire as
`*_exact`.

**A consequence worth stating**, because it looks like a defect and is not:
totals are summed exactly and then rounded once, so the sum of the displayed
per-event figures can differ from the displayed total by a cent. Summing
rounded parts instead would be the actual error.

**Fail closed.** A confirmed sale larger than the tracked position is not a
rounding difference — this book is long-only, so it means the ledger's picture
of the position is wrong. The engine refuses, the fill is **not** stored, the
symbol is marked `ACCOUNTING_MISMATCH`, and no further event is applied to it
until a person has looked. The ledger's numbers are left exactly as they were:
overwriting them to agree with the broker would destroy half of what a repair
needs.

---

## Idempotency

The broker's own execution id, under a `UNIQUE` constraint. Applying the same
execution twice is a no-op that reports itself as one; a second insert is not
merely skipped by a check, it is unstorable.

Each pass re-reads a window extending **back before** the newest row the ledger
holds (two days by default, so a Monday pass re-reads Friday) and lets the
constraint discard what it has already seen. A cursor that asked only for
strictly-newer executions would lose anything that arrived late or out of
order, and nothing downstream would ever notice. This is also what closes the
bootstrap race: an execution landing mid-bootstrap is either inside the window
this pass reads or inside the window the next pass re-reads.

---

## What is out of scope, and why

**Crypto.** Measured, not assumed. Replaying the same execution feed for the
crypto book leaves a residual of exactly the coin-denominated pair fees: this
broker charges crypto fees **in the coin**, as records that reduce inventory
without appearing in the execution feed at all. A fill-only cost-basis engine
is therefore correct for equity and wrong for crypto. Crypto executions are
skipped at ingestion and counted, and crypto needs its own accounting program.

**Asset class is looked up, never inferred.** An execution record does not say
which book it belongs to, and guessing from the ticker's punctuation is how a
crypto fill ends up in an equity ledger. Every execution is joined to its
order, which does say; an execution whose order cannot be read is **skipped and
reported**, never assumed to be equity.

Because of that join, the order read reaches **thirty days further back** than
the execution read: an order submitted before the window can still produce an
execution inside it. It cannot happen with today's same-second market fills,
which is precisely the reason to handle it — the ledger would otherwise be
correct only because of a property of the current order type. A pass that still
finds an execution it cannot explain re-reads the whole order record once and
retries, at a cost of one extra request on an already-anomalous pass. An
execution with no order at all remains unresolved and reported.

**Dividends** are not trade P&L and have no path into these numbers.

**Fees.** Where this broker charges equity regulatory fees, it charges them as
a *daily account-level total* naming a day and a trade count, not an execution.
Execution records carry no fee field. There is therefore no authoritative
per-execution fee, and attributing the daily total to individual fills would
require inventing an allocation. Per-fill `fees` is stored as an explicit zero;
the schema carries the columns at full precision so a broker that does publish
per-execution fees needs no redesign.

---

## Provenance

Three answers, each earned from evidence:

| value | evidence |
|---|---|
| `EQUITY_RUNTIME` | the runtime's own store holds this broker order id |
| `MANUAL_OPERATOR` | no runtime store holds it, but the client order id carries this system's prefix |
| `UNKNOWN_EXTERNAL` | neither |

Account realized P&L includes all three, because a broker-confirmed trade moved
the position whoever placed it. Any figure labelled as a *strategy* result must
exclude the non-runtime rows. A symbol being in the traded universe is not
evidence of anything — one account, and anyone with the keys can trade it.

---

## The database

Its own file, its own schema, its own version counter:

    /var/lib/autotrader-accounting/equity-accounting.db

**Deliberately not tables in an operational store.** Three stores describe this
one account at two different schema versions, and every command in the trading
lineage migrates a store upward when it opens it. Accounting tables in that
schema would mean a version bump, and the next process from this lineage to
open the equity paper store would migrate it out from under a *running trader*
that only understands the version it was installed at.

| table | shape |
|---|---|
| `accounting_fills` | immutable source events, `idempotency_key` UNIQUE |
| `realized_pnl_events` | one row per sale, `accounting_event_id` UNIQUE |
| `position_cost_basis` | derived current state, the one table rewritten |
| `accounting_metadata` | single row: horizon, bootstrap method, account fingerprint |
| `accounting_sync_runs` | one row per pass |
| `accounting_reconciliation_runs` / `_symbols` | one row per pass / per symbol |

`accounting_fills` and `realized_pnl_events` have no UPDATE path in the module
at all.

**A rollback journal, not WAL** — the one place this store departs from the
operational stores' convention, and it is deliberate. A `mode=ro` connection to
a WAL database must *create* the `-shm` side file when no writer is holding
one, which needs write access to the directory. The dashboard reader runs under
`ProtectSystem=strict` with no write access there, so under WAL it could read
the ledger only during the few seconds every five minutes that the writer was
running, and correctly reported `DATABASE_UNREADABLE` the rest of the time.
WAL's benefit — concurrent reads during a write — is worth nothing at a
five-minute write cadence, and its cost here was the reader. Opening for
writing sets the mode on every connection, so a ledger created under WAL by an
older build is converted in place with no manual migration. Recording a fill, writing its realized event and advancing the cost
basis are one `BEGIN IMMEDIATE`: a crash between them leaves none of them, so a
fill can never exist without the state transition it caused.

Money and quantities are TEXT holding the plain decimal string, read back with
`Decimal(text)`, which round-trips exactly. The `CHECK` constraints `CAST` to
REAL as a coarse guard against a writer that bypassed the module; the cast is
not the value.

---

## Reconciliation

A **second, independent** reconciliation. The trading runtimes already
reconcile their orders and positions and gate trading on the result; nothing
here touches that, reads its verdict, or can influence it.

| verdict | meaning |
|---|---|
| `CLEAN` | every symbol's quantity matches and every average is inside tolerance |
| `BASIS_DIVERGENCE` | quantities match exactly and an average differs, but lot relief over the ledger's own fills accounts for all of it |
| `DEGRADED` | quantities match; an average differs beyond tolerance and no lot-relief order over the recorded fills accounts for it |
| `MISMATCH` | a quantity differs, or a symbol has stopped accounting |
| `UNKNOWN` | the broker could not be read, or the ledger was never bootstrapped |

Worst-first when several apply: `MISMATCH`, `UNKNOWN`, `DEGRADED`,
`BASIS_DIVERGENCE`, `CLEAN`. One explained symbol never softens the verdict on
an unexplained one.

**Quantity is the hard test.** Two systems that disagree about how many shares
exist have not made a rounding error; one has missed an execution.

**Average cost is the soft test**, because the broker publishes
`average_entry_price` rounded to six decimal places — exact equality is not
available at any precision the broker can express. The tolerance is `1e-6`.

### Beyond the tolerance, the difference is tested rather than assumed

Two systems holding the same shares, bought at the same prices, can still carry
different average costs, because *which shares did that sale consume?* has more
than one defensible answer. This ledger relieves a sale at the running weighted
average. This account's broker restates `avg_entry_price` after the close,
relieving that day's sales against the inventory carried in from prior days —
which moves its published figure away from the weighted average without either
side being wrong about a single fill.

That was observed directly on 2026-09-03: META, NVDA and TSLA all flipped from
`CLEAN` to `DEGRADED` in one five-minute pass at 07:35 UTC, market closed, zero
fills imported, the ledger's own average byte-identical either side of the
transition. The broker restated; the ledger did not move.

Reported as a bare `DEGRADED`, that is indistinguishable from a missing
execution — the one thing this reconciliation exists to be able to tell apart.
So a deviation beyond tolerance is now tested: **is the broker's implied cost
basis reachable by relieving this ledger's own recorded purchase lots?** Two
greedy lot books are advanced over the same fills in the same chronological
order, one always taking the dearest lot on hand and one the cheapest. Every
admissible method — weighted average, FIFO, LIFO, the broker's day-carry
convention — lands between them.

- inside the range → `BASIS_DIVERGENCE`, carrying the exact dollar figure and
  the bounds it was judged on, stored on the reconciliation row
- outside it → `DEGRADED`, exactly as before

The question is deliberately never *which* method the broker used. That answer
was inferred from a handful of restatements and could change without notice;
this one is answered from the ledger's own evidence and stays correct if the
broker changes convention tomorrow. Chronological, not sorted: a sale may only
consume shares that had already been purchased when it happened.

The envelope is widened by `broker_quantity x 5e-7` — half the broker's last
published digit, per share — because the basis implied by a six-decimal average
is uncertain by exactly that much. That is a tolerance on the question, not on
the answer, and `AVERAGE_COST_TOLERANCE` itself is unchanged at `1e-6`.

Every path that cannot gather evidence returns *not explained*: no fill history
for the symbol, fills that do not fold back to the stored position, a flat
position, an oversell. Missing evidence never becomes agreement.

**On the difference being a timing difference and not a loss.** Under every
cost-basis method, `cost_basis - cumulative_realized == sum of buy notional -
sum of sale proceeds`, and the right-hand side is fixed by fills both sides
agree on. So the basis difference and the realized difference are the same
number with opposite sign: relieving a dearer lot releases more cost and books
less profit. Nothing is lost or created — the two sides recognise the same P&L
on different days, and the reconciliation message states the amount.

Nothing here repairs anything.

---

## Operating it

The unit is a oneshot on a five-minute timer:

    autotrader-equity-accounting.service
    autotrader-equity-accounting.timer      OnCalendar=*-*-* *:04/5:30

Minutes 4, 9, 14 … at :30 keep it clear of the decision cycles at the quarter
boundaries, the equity submission at about two minutes past, and both reconcile
passes (:07:30 crypto, :22:30 equity paper). Two to three provider reads per
pass — under half a request a minute against an allowance of a hundred and
eighty.

It runs from `/opt/autotrader-dashboard/venv` as `ateqpaper`. **Never** from
`/opt/autotrader-equity-paper/venv`: that one is shared with the live trader as
a non-editable copy install, and a `pip install` into it leaves no `autotrader`
package under a running trader for the seconds between uninstall and install.

    # first time only — writes the tracking horizon
    sudo -u ateqpaper /opt/autotrader-dashboard/venv/bin/autotrader \
        equity-accounting bootstrap --confirm --source-sha "$(git rev-parse HEAD)"

    # what the ledger says
    sudo -u ateqpaper /opt/autotrader-dashboard/venv/bin/autotrader equity-accounting status
    sudo -u ateqpaper /opt/autotrader-dashboard/venv/bin/autotrader equity-accounting events

A failing pass leaves the unit `failed` and visible. Nothing retries; the next
scheduled run is the retry, and the dashboard shows the status regardless of
the unit's state.

### When reconciliation reports MISMATCH

    autotrader equity-accounting inspect

shows both sides per symbol, the last execution each symbol processed,
executions the broker has that the ledger does not, and ledger rows the
broker's record no longer shows. It changes nothing, and it is deliberately not
called `repair` — **there is no `--fix`.**

A discrepancy is resolved by understanding it and then rebuilding from the
immutable source events:

    autotrader equity-accounting rebuild --into /tmp/rebuilt.db --confirm

which refuses to write over an existing file. Compare it against the ledger in
service, then swap the files deliberately. There is no command anywhere that
edits a realized total.

---

## On the dashboard

The Equity Paper page carries a four-metric strip: **Daily account P&L**,
**Realized today**, **Unrealized open**, **Realized since tracking**, with the
accounting status beside them.

These are three different measurements and the page says so. Daily account P&L
is account equity against the stored UTC-day baseline and covers the whole
account, crypto included — it is the figure the risk engine's daily-loss halt
is measured on, and this feature does not change it in any way. Unrealized is
the broker's own figure over open equity positions. Realized is what confirmed
equity sales released. **They are not required to sum**, and nothing on the
page adds them.

The horizon is always stated, from the ledger's own metadata, as a timestamp —
`REALIZED SINCE 2026-08-31 13:34 UTC · WHOLE CONFIRMED HISTORY`. It never says
"all time", and it deliberately never says "since activation": on this
deployment the first confirmed execution *precedes* EDA-1's activation, because
a hand-run submission smoke came first, so "since activation" would be both
wrong and flattering. The `· WHOLE CONFIRMED HISTORY` suffix appears only when
the replay reached the first execution the account ever had; without it a
reader is right to assume there is history the ledger does not have.

When the status is not `CLEAN` it renders in the strip, in the target-vs-actual
header, and in the symbol drawer — a reader scanning any of those must be able
to see that the figures are in doubt without scrolling.

The symbol drawer adds the ledger's own average cost beside the broker's,
realized today and since tracking, and a realized-event table. On the price
chart a SELL marker carries what that order realized, matched on broker order
id — the only identifier the two records provably share. A BUY marker never
carries one: a purchase realizes nothing.

Routes, all GET, all under the existing basic-auth, no-store and 405
protections:

    GET /api/equity-paper/realized-pnl/summary
    GET /api/equity-paper/realized-pnl/by-symbol
    GET /api/equity-paper/realized-pnl/events?symbol=&limit=
    GET /api/equity-paper/realized-pnl/status
    GET /api/equity-paper/symbols/{symbol}/realized-pnl

---

## Rollback

Realized P&L is additive. Removing it removes a panel and a timer; it cannot
leave a trading process in a different state than it found one.

    systemctl disable --now autotrader-equity-accounting.timer
    # the dashboard tree and frontend roll back per docs/DASHBOARD_V2.md

The ledger file can be left in place — nothing reads it — or moved aside. No
trading store, no trading unit and no venv a trader uses is touched by either
deploying or removing this feature.
