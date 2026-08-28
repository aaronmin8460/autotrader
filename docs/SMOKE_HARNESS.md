# Paper Smoke Operations Harness

A read-only operational harness for the Combined Paper Smoke. It removes the
manual work from either side of a smoke so that the two moments which genuinely
need a human — placing one BUY and placing one cleanup SELL — are the only
moments that need one.

**This harness cannot place an order.** Not behind a flag, not behind an
environment variable, not behind a confirmation token. There is no `--execute`,
no `--yes`, and no `--auto-cleanup`, because there is nothing for them to switch
on. See [Safety guarantees](#safety-guarantees) for how that is enforced rather
than asserted.

It is a **separate program** from `autotrader`. The main CLI owns `paper-submit`
and `crypto-run`, the two commands that can reach a broker's order endpoint;
this one owns nothing of the kind. `autotrader-smoke --help` lists every action
it can take, and none of them is an order.

```bash
autotrader-smoke --help
```

From a git worktree, where the installed console script still points at
whichever checkout was installed into the virtualenv:

```bash
PYTHONPATH=src python -m autotrader.smoke --help
```

---

## Commands

| Command | Answers | Exit codes |
| --- | --- | --- |
| `preflight` | May a smoke begin? | 0 `READY_FOR_PAPER_SMOKE`, 1 `BLOCKED` |
| `inspect-order` | What did the broker actually do with one order? | 0 found, 1 not found, 2 `ORDER_TRUTH_UNRESOLVED` |
| `cleanup-plan` | How much may be sold, and with which command? | 0 plan or nothing to do, 1 cannot be closed |
| `final-audit` | Did the smoke finish, and is exposure restored? | 0 `SMOKE_COMPLETE`, 1 `SMOKE_INCOMPLETE`, 2 unresolved |
| `sequence` | The operator checklist, printed | 0 |

Exit codes deliberately mirror `paper-submit` and `reconcile`: **2 always means
"an order may exist at the broker"**, so no script can read it as a clean no.

---

## Operator sequence

`autotrader-smoke sequence` prints this with your paths filled in. Steps marked
**YOU** are yours to run; the harness runs none of them.

```
 1.  autotrader-smoke preflight --db data/autotrader.db --symbol BTC/USD --write-baseline
 2.  YOU run ONE paper BUY, sized from the preflight's --dry-run output
 3.  autotrader-smoke inspect-order --client-order-id <BUY id> --db data/autotrader.db
 4.  autotrader reconcile --db data/autotrader.db          (only if step 3 says to)
 5.  autotrader-smoke cleanup-plan --symbol BTC/USD --db data/autotrader.db
 6.  YOU run ONE cleanup SELL, exactly the command step 5 printed
 7.  autotrader-smoke inspect-order --client-order-id <SELL id> --db data/autotrader.db
 8.  autotrader reconcile --db data/autotrader.db          (pass 1)
 9.  autotrader reconcile --db data/autotrader.db          (pass 2 — expect CLEAN)
10.  autotrader-smoke final-audit --db data/autotrader.db --symbol BTC/USD \
         --buy-client-order-id <BUY id> --sell-client-order-id <SELL id>
11.  autotrader crypto-run --once --observe-only --db data/autotrader.db
12.  autotrader equity-run --once --observe-only --db data/autotrader.db   (when it exists)
13.  autotrader-smoke preflight --dashboard-url <url>      (dashboard health)
```

Steps 2 and 6 are the only ones that place an order. Both need
`AUTOTRADER_PAPER_TRADING_ENABLED=true` **and** `--confirm-paper PAPER`, and
neither gate can satisfy the other.

---

## Design rules

Three rules run through every module, each enforced by a test in
`tests/test_smoke_harness.py` rather than by documentation.

### Read and repair stay apart

Reconciliation may rewrite local SQLite from broker truth. That is right for
reconciliation and wrong for an inspection, so the harness reads the **latest
persisted** reconciliation run and prints the command when a fresh pass is
needed. It never starts one — `reconcile_paper_state` is not reachable from this
package.

The database connection is opened `file:...?mode=ro` with `PRAGMA query_only =
ON` — two independent guards. Neither `state.connect` (which sets `journal_mode =
WAL`) nor `initialize_database` (which applies migrations) is called. A missing
database file is an error, never a freshly created empty one, because an audit of
a mistyped path must not report a serene and meaningless CLEAN.

One honest caveat. Reading a WAL database correctly needs the `-shm`
coordination file, and SQLite creates `-shm` and an empty `-wal` beside the
database if they are absent. Every WAL reader does this; it writes no rows, and a
test asserts both that the database is byte-identical across a read and that the
write-ahead log does not grow. The alternative — `immutable=1` — would avoid the
sidecars by telling SQLite to ignore the WAL entirely, which returns stale data
whenever a runtime has pages in flight. Stale numbers in an audit are worse than
two empty coordination files.

### The broker's position is the only quantity a cleanup is sized from

Not the requested quantity, not the filled quantity, not the local `positions`
table. A crypto BUY of `0.00016705` BTC settles as a position of `0.000166632`
BTC once the taker fee comes out of the base asset. A cleanup sized from the fill
would try to sell more than the account holds. That exact case is pinned as a
test fixture.

Rounding is always **down**, to the broker's own trade increment, read live on
every call. Rounding a cleanup up to clear a minimum would sell an asset the
account does not hold — a short, which this system cannot express.

### An unanswerable question is never answered

A lookup that times out reports `ORDER_TRUTH_UNRESOLVED` and `DO NOT RETRY
ORIGINAL ORDER`. "The check failed" and "there is no such order" are different
answers; conflating them is how a system submits an order it has already
submitted.

---

## Baseline snapshots

`preflight --write-baseline` records the "before" numbers so `final-audit` can
compare exposure automatically instead of by eye.

- Default location `.smoke/baseline.json`, which is gitignored. Never commit one.
- Quantities are stored as canonical decimal **text**, so `0.000166632` survives
  a JSON round trip exactly. Comparison is exact equality: a dust remainder is
  residual exposure, not noise, and a tolerance would hide the fee-adjustment
  case this harness exists to catch.
- The payload is an **allowlist** — `Baseline.to_payload` names every field by
  hand, so a field added upstream cannot start writing itself to disk.
- Before every write the document is scanned twice: any credential-shaped *key*
  is refused, and so is any document containing a value the Alpaca credential
  variables currently hold. The scan runs before the file is opened, so a
  rejected snapshot leaves nothing behind — not even a truncated file.

---

## Safety guarantees

Each of these is a test, not a promise.

| Guarantee | How it is enforced |
| --- | --- |
| No order-submission surface | `submit_order`, `execute_paper_order`, `cancel_order`, `close_position`, `replace_order`, `paper=False` and friends are absent from executable code (docstrings stripped first) |
| No broker SDK import | No module under `autotrader.smoke` imports `alpaca`; the broker is reached only through `execution.paper`'s reading half |
| Generating a command is not running one | Only `gitinfo.py` may start a process. Every other module is barred from importing `subprocess`/`multiprocessing`/`pty` or calling `os.system`/`os.popen`/`os.exec*` |
| `gitinfo` cannot run anything but git | Every `subprocess` call is checked against the parsed source: a literal argv list beginning with the constant `git`, never `shell=True`, and no autotrader command name anywhere in the file |
| No execution flag exists | The `--flag` strings the CLI declares are read off the parsed source; the command callbacks' own signatures are checked too |
| No hidden environment switch | Nothing assigns to `os.environ`, `putenv`, or `setenv` |
| The audit cannot write | `query_only` is asserted on, and a real `UPDATE` is asserted to fail |
| Offline | Every test runs with `socket.socket` and `socket.create_connection` blocked; the fake broker's submitting methods raise rather than record |

---

## Combined Integration adaptation points

The harness was built against `main` and degrades gracefully. When
`feat/combined-integration` lands, these are the only places to look.

> **Status: adapted.** The harness has been merged into
> `feat/combined-integration`. Points 1 and 3 below were applied; the rest
> needed no code change and the reasons are recorded under each. What was
> actually done:
>
> * **Universe (1).** `("autotrader.execution.models", "TRADABLE_SYMBOLS")` was
>   appended to `UNIVERSE_SOURCES`. `resolve_universe()` now returns all twelve
>   tracked symbols and `universe_source()` names that module. The list is
>   still discovered by import, never copied.
> * **Global account safety (3).** `tracking.account_safety` reads the
>   `account_safety_state` row, and both `preflight` and `audit` gained an
>   `account.safety` check beside the existing reconciliation one. The two are
>   kept separate on purpose: a pass narrower than the tracked universe can be
>   CLEAN while a halt still stands, so reading only the pass would clear a
>   smoke the execution boundary would then refuse. The state is carried on
>   `PreflightReport` and recorded in the baseline snapshot, which took
>   `BASELINE_SCHEMA` to 2.
> * **Schema (4).** No source change: `preflight` already read
>   `state.SCHEMA_VERSION`. Two tests pinned the literal `5` and now assert
>   against the constant, so the next migration cannot break them.
> * **Equity metadata (2), dashboard (5), checkpoints (6), correlation (7).**
>   Unchanged, and each still behaves as documented below. The whole-share
>   equity policy in `cleanup.policy_for` already matches what the equity
>   boundary enforces (`normalize_share_quantity`, a one-share floor, no USD
>   notional rule), so it is assumed-but-correct rather than wrong; making it
>   read broker metadata remains a worthwhile follow-up, not a blocker.

### 1. Tracked universe — usually no change needed

`smoke/readonly.py` resolves the universe in this order: an explicit
`--universe`/`--universe-file`, then `AUTOTRADER_SMOKE_UNIVERSE`, then the first
of `UNIVERSE_SOURCES` that exists, then `execution.models.SUPPORTED_SYMBOLS`.

`UNIVERSE_SOURCES` already probes:

```
autotrader.universe.TRACKED_UNIVERSE
autotrader.universe.SUPPORTED_SYMBOLS
autotrader.config.TRACKED_UNIVERSE
```

If Combined Integration publishes its 12-symbol universe at any of those, the
harness widens on its own. If it publishes elsewhere, add the `(module,
attribute)` pair to `UNIVERSE_SOURCES` — that is the whole change. Do **not**
copy the symbol list into this package; a second frozen universe is exactly what
this design avoids.

It published elsewhere, so the pair was added:

```
autotrader.execution.models.TRADABLE_SYMBOLS
```

That tuple is the union of both books — the same one an `OrderIntent` is
validated against, and the same one a full-universe reconciliation must cover
before it may clear the shared account halt. The crypto-only
`SUPPORTED_SYMBOLS` remains the last-resort fallback for an older build.

### 2. Equity asset metadata

`smoke/cleanup.py:policy_for` currently applies a whole-share policy to equities
with `min_order_size = min_trade_increment = 1` and no USD notional floor,
because `main` reads no equity broker metadata. When integration adds an equity
asset reader, return a policy built from it — the same shape `CryptoAssetSpec`
produces today — and update the `source` string so a report still says whether
the numbers were read or assumed.

Related: `smoke/broker.py:read_reference_price` returns `None` for equities, and
the planner falls back to the broker's own `market_value / quantity` for the
position. An equity price path makes that fallback unnecessary but not wrong.

### 3. Global account safety table

`smoke/tracking.py:latest_reconciliation` reads `reconciliation_runs` and the
preflight/audit gate on `status` + `safe_to_trade`. If integration adds a global
account-safety table, read it in `tracking.py` and add one `CheckResult` to
`preflight._reconciliation_check` and `audit._reconciliation_check`. Keep the
existing run check: a per-pass conclusion and a global flag answer different
questions.

Integration added `account_safety_state`, so this was done:
`tracking.account_safety` reads the row, and `preflight._account_safety_check`
and `audit._account_safety_check` each contribute one `account.safety`
`CheckResult`. The run check was kept, and the advice about the two answering
different questions turned out to be the point — the case that motivates the
separate check is a CLEAN pass recorded *before* an ambiguous submission, with
the halt that submission raised still standing afterwards. An unreadable row is
treated as unsafe, and a row that no reconciliation has ever established is
`FAIL` rather than a silent pass. The harness never clears a halt; only a
full-universe reconciliation does.

### 4. Schema version

`preflight._database_checks` blocks unless the database's `schema_metadata`
version equals `state.SCHEMA_VERSION` (6 after Combined Integration). It reads
the constant, so a schema bump needs no edit here — but the harness will (correctly) refuse a
database that has not been migrated yet, and will not migrate it. Run any writing
`autotrader` command first.

### 5. Dashboard endpoint

`preflight --dashboard-url <url>` / `final-audit --dashboard-url <url>` performs
one `GET`, requires HTTP success and valid JSON, and reports any
credential-shaped field by name. For Dashboard V0.2 just pass the new
health/overview URL. A dashboard failure never blocks broker verification; only a
dashboard that answers **and** leaks a credential field is a failing check.

### 6. Equity runtime checkpoints

`health.runtime_health` reports per-symbol checkpoint freshness for every symbol
in the universe, including `NOT_RECORDED`. Equity symbols will show `STALE`
outside market hours — this is reported, never gated, because the harness holds
no session calendar. If integration adds one, that is where to consult it.

### 7. Order correlation limits

`audit._correlation_checks` counts local `order_intents`, which is a complete
record of what *this system* attempted — every submission path persists an intent
before calling the broker. An order placed by hand in Alpaca's web UI leaves no
row and would surface only as a position mismatch. The audit says so in its own
output. If integration adds a broker order-listing read, that is the check to
strengthen.
