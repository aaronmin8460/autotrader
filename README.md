# autotrader

A personal, single-user automated trading system for US equities, built to run
as a local Python CLI process against an Alpaca **paper** account.

This is an engineering project. It makes **no claim of profitability**, and it
is not investment advice.

## Status: Phase 7 complete - Alpaca **paper** order execution. Next: Phase 8 reconciliation / crash recovery

This repository can now submit an order, and it can only ever submit one to an
Alpaca **paper** account. **Live trading is not implemented, not configurable,
and not reachable.** The trading client is constructed with `paper=True`
hardcoded; there is no `--live` flag, no `--paper` option, no environment
variable that selects an environment, and no public function that accepts a
parameter which could change it. Live trading is not disabled by default - it
is unexpressible, and a test asserts `paper=False` appears nowhere in the
source.

What exists today: downloading historical 15-minute US-equity bars from
Alpaca's IEX feed as Parquet, validating a stored dataset, the EMA 20 / EMA 50
signal generator, a deterministic backtester, a deterministic risk engine, a
local SQLite operational-state database, and a single deliberately awkward
paper-order command. Validation never downloads or repairs data; the strategy
emits signals only; the backtester is local arithmetic; the risk engine is a
pure calculator that persists nothing; and the database stores records without
deciding anything.

Submitting a paper order requires **two independent gates**, both closed by
default: the `AUTOTRADER_PAPER_TRADING_ENABLED=true` environment variable and
`--confirm-paper PAPER` on the command line. Every submission is sized by the
risk engine, and the quantity sent to the broker is always the risk-approved
quantity - never the requested one. The order intent and its `client_order_id`
are committed to SQLite *before* the broker is called.

**Reconciliation is not implemented.** Phase 7 creates the durable anchors
crash recovery will need but resolves nothing. An ambiguous submission outcome
is recorded as `UNKNOWN` and left alone - never retried, never re-keyed. There
is still no fills, executions, or reconciliation table.

## Scope summary

| | |
| --- | --- |
| Market | US equities only |
| Broker | Alpaca only |
| Execution | Alpaca paper trading only - live is unreachable (Phase 7) |
| Universe | SPY, QQQ, AAPL, MSFT, NVDA |
| Timeframe | 15-minute bars |
| Direction | Long only |
| Research strategy | EMA 20 / EMA 50 crossover (engineering validation only) |
| Historical storage | Parquet |
| Operational state | SQLite, local file, schema v2 (Phases 6-7) |
| Interface | Python CLI, local process - no web frontend |

Out of scope: live trading, options, crypto, futures, forex, shorting,
leverage, margin, multiple brokers, ML/LLM signal generation, web or mobile
frontends, and cloud deployment.

**[docs/SPEC.md](docs/SPEC.md) is the authoritative scope document.** Read it
before extending this project; it takes precedence over any chat history.

## Setup

Requires Python 3.11. The repository ships with no virtual environment; create
one and install the project in editable mode.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Credentials

Market data and paper trading both require Alpaca API credentials, read from
the process environment. Use your **paper** keys:

```bash
export ALPACA_API_KEY=...
export ALPACA_SECRET_KEY=...
```

`.env.example` documents the variable names. If you keep a local `.env`, it is
git-ignored and must never be committed. Credentials are never logged,
printed, persisted, written into generated files, stored in SQLite, embedded
in a `client_order_id`, or included in an exception message.

Broker submission needs one more variable, which is **disabled unless it is
exactly `true`**:

```bash
export AUTOTRADER_PAPER_TRADING_ENABLED=true
```

Missing, empty, `false`, `TRUE`, `1`, or `yes` all leave submission disabled -
the gate fails closed, so a typo can never enable it. It only ever enables
paper trading; there is no live equivalent.

## Usage

Show CLI help:

```bash
python -m autotrader.cli --help
```

Show the version:

```bash
python -m autotrader.cli version
```

Download historical bars:

```bash
python -m autotrader.cli download --symbol SPY --timeframe 15m --start 2025-01-01 --end 2025-12-31
```

`--start` and `--end` are US market calendar dates (`America/New_York`), and
`--end` is inclusive. `--symbol` accepts only the V0.1 universe, and `15m` is
the only supported timeframe.

The command writes two files under `data/raw/`, named after the symbol and the
requested date range so a re-download never silently overwrites a different
range:

```
data/raw/SPY_15m_2025-01-01_2025-12-31.parquet
data/raw/SPY_15m_2025-01-01_2025-12-31.metadata.json
```

Each stored row is `timestamp, symbol, open, high, low, close, volume,
trade_count, vwap`, with timezone-aware UTC timestamps in ascending order.
The sidecar JSON records provider, feed, timeframe, requested range, row
count, and retrieval time - never credentials.

**Everything under `data/` is local and git-ignored.** Downloaded market data
is never committed; the repository is reproducible from source plus a
re-fetch.

Validate a downloaded dataset:

```bash
python -m autotrader.cli validate data/raw/SPY_15m_2025-01-01_2025-12-31.parquet
```

The command reads the file and reports whether it is structurally and
internally consistent enough for later phases to consume. It checks the exact
canonical columns, a non-empty dataset, timezone-aware UTC timestamps that are
unique and ascending, a single supported uppercase symbol, positive finite
OHLC values with `high`/`low` bounding `open` and `close`, non-negative
volume, and - where present - non-negative `trade_count` and positive `vwap`.
Violations are summarized with a row count, never listed one line per row.

It deliberately does **not** check bar-to-bar spacing, session completeness,
or price anomalies: weekends, holidays, and overnight closures make gaps
normal, and anomaly heuristics are out of scope.

```
VALID

File:   data/raw/SPY_15m_2025-01-01_2025-12-31.parquet
Rows:   6552
Symbol: SPY
Errors: 0
```

Exit codes are `0` for a valid dataset, `1` when validation errors were found,
and `2` when the file cannot be read at all. Nothing is written, and neither
an invalid dataset nor a missing file produces a traceback.

The `autotrader` console script is installed as an equivalent entry point.
The only command that reaches a broker is `paper-submit`, described below.

## Strategy signals (Phase 3)

`autotrader.strategies.ema_cross` generates EMA 20 / EMA 50 crossover signals
from a canonical bar frame. It exists to validate the engineering pipeline and
is **not a claim of profitability**.

```python
from autotrader.strategies import generate_ema_cross_signals

signals = generate_ema_cross_signals(bars)  # -> list[Signal], ascending by timestamp
```

Long only, two signal types. `BUY` when the fast EMA moves from at-or-below to
strictly above the slow EMA; `EXIT` when it moves from at-or-above to strictly
below. Both EMAs use `adjust=False` and stay undefined through their warm-up,
so no signal can be produced before 50 bars have been observed. A crossover
produces at most one signal - nothing repeats while the relation merely holds.

A signal's timestamp is the bar whose **close** made the crossover knowable.
It is **not an execution timestamp**, and a signal carries no price: choosing
when and at what price to act belongs to Phase 4 backtesting. The module emits
signals only - no orders, positions, or P&L - imports no broker client, and
requires no credentials or network access. See
[docs/SPEC.md](docs/SPEC.md) section 8, "Phase 3 - Strategy", for the full
contract.

## Backtesting (Phase 4)

The backtester connects the pipeline that already exists - stored Parquet bars
-> Phase 2 validation -> Phase 3 signals -> execution simulation -> portfolio
accounting - and reports how it would have performed.

**This is engineering validation, not a profitability claim.** It exists to
prove the pipeline accounts correctly. Its results are not a reason to trade
anything, and they are not investment advice. Nothing is downloaded, written,
or ordered: the whole simulation is local arithmetic.

```bash
python -m autotrader.cli backtest data/raw/SPY_15m_2025-01-01_2025-12-31.parquet
```

`--initial-cash` overrides the starting balance; it defaults to `100000` and
must be positive.

```
AUTO TRADER BACKTEST

Symbol:                SPY
Strategy:              EMA20 / EMA50
Rows:                  7318

Initial Cash:          $100,000.00
Final Cash:            $99,398.68
Final Equity:          $99,398.68

Total Return:          -0.60%
Max Drawdown:          -16.53%

Signals:               117
BUY Executions:        58
SELL Executions:       58
Completed Round Trips: 58
Ending Position:       0 shares
```

The equivalent Python API is `run_backtest(bars, initial_cash=100_000.0)`,
returning a `BacktestResult`.

### The rules it simulates

**Next-bar-open execution - no look-ahead.** A crossover on bar *t* is knowable
only once bar *t* has closed, so the earliest it can be acted on is the open
of bar *t+1*. A signal is **never** filled on its own bar, neither at that
bar's open nor at its close, and every execution's timestamp is strictly later
than its signal's. A signal on the **final** bar is left unexecuted rather
than filled at an invented price.

**$100,000 initial capital, long only.** One symbol, at most one position, no
leverage, no borrowing, and no short selling.

**Whole-share, all-cash sizing.** A `BUY` while flat spends all available cash
on `floor(cash / price)` shares. Cash never goes negative, and fractional
shares are out of scope. An `EXIT` while long sells the entire position. An
`EXIT` while flat and a `BUY` while already long are both no-ops - the real
signal sequence often opens with an `EXIT` while the portfolio is still flat.

**Zero fees and zero slippage.** Fills happen at exactly the next bar's open
with no commission, no fees, no slippage, and no market impact. This is a
deliberate simplified baseline for V0.1, not a realistic execution model, and
results should be read with that in mind.

**Open positions are marked, not liquidated.** A position still open on the
final bar is *not* force-sold and no closing trade is fabricated. It is marked
to market at the final bar's close, so
`final_equity = cash + quantity * final_close`.

**Metrics.** `total_return` is `(final_equity / initial_cash) - 1` and
`max_drawdown` is the worst peak-to-trough decline of the end-of-bar equity
curve. Both are stored as decimal fractions - the CLI renders them as
percentages - and there is no annualization, benchmark, or risk-adjusted
metric. A *completed round trip* is a `BUY` followed later by a `SELL`; a
position still open at the end is not one.

Invalid data stops the run: the dataset is validated with Phase 2 first, and
any finding aborts the backtest with a concise error rather than a silent
repair. Exit codes are `0` for a completed simulation, `1` when the dataset or
the starting cash is unusable, and `2` when the file cannot be read.

See [docs/SPEC.md](docs/SPEC.md) section 8, "Phase 4 - Backtesting", for the
full contract.

## Risk engine (Phase 5)

`autotrader.risk` answers one question about a **proposed** trade: may it be
allowed, and if so, what is the largest safe whole-share quantity?

It is a calculator and nothing else. It submits no order, builds no broker
client, makes no network call, opens no database, and writes no file. It does
not mutate what it is given, and the same inputs always produce the same
decision. It is also **not** wired into the Phase 4 backtester, which keeps its
own all-cash sizing baseline - no backtest result changes.

```python
from autotrader.risk import RiskContext, RiskRequest, RiskSide, evaluate_risk

context = RiskContext(
    equity=200_000.0,
    cash=200_000.0,
    total_exposure=0.0,
    symbol_exposure=0.0,
    current_position_quantity=0,
    daily_pnl=0.0,
    start_of_day_equity=200_000.0,
    trading_enabled=True,
)
request = RiskRequest(
    symbol="SPY", side=RiskSide.BUY, reference_price=250.0, requested_quantity=1_000
)

decision = evaluate_risk(request, context)
# approved=True, approved_quantity=40, reason_code="POSITION_LIMIT"
```

The request asked for 1,000 shares; 5% of $200,000 is $10,000, which is 40
shares at $250. The decision is **approved at 40** - an oversized request is
sized down, not thrown away - and `reason_code` names the constraint that
bound it.

### The V0.1 limits

These are **engineering safety defaults, not investment advice** and not a
recommended allocation.

| Limit | Value |
| --- | --- |
| Maximum market value of any one symbol | 5% of current equity |
| Maximum aggregate long exposure | 30% of current equity |
| Daily loss at which new entries halt | -2% of start-of-day equity |
| Direction | Long only |
| Leverage | None - an entry may only spend cash on hand |
| Share quantities | Whole shares only |

They are fixed for V0.1, strategy-independent, and deliberately not loaded
from the environment. A policy that claims to allow leverage or shorting is
refused outright rather than quietly ignored.

### Entries are gated

A BUY must clear a well-formed request, the `trading_enabled` kill switch, and
the daily-loss halt, and is then sized against the **tightest** of three
ceilings:

```
position_remaining  = max(0, equity * 0.05 - symbol_exposure)
portfolio_remaining = max(0, equity * 0.30 - total_exposure)
max_notional        = min(position_remaining, portfolio_remaining, cash)
max_quantity        = floor(max_notional / reference_price)
approved_quantity   = min(requested_quantity, max_quantity)
```

An oversized BUY is **clamped** to `max_quantity` and approved, with the
binding constraint reported as the reason. A `max_quantity` of zero is
rejected. Nothing is ever approved above a limit. The daily-loss boundary is
inclusive - exactly -2.00% blocks.

### Exits are not

Every limit above exists to stop the account **adding** risk. None of them may
stop it **removing** risk, so a SELL against an existing long is evaluated
separately: the kill switch, the daily-loss halt, and both exposure caps are
entry gates and none of them can block an exit. A kill switch that trapped an
open position would be a safety defect, not a safety feature.

A SELL is rejected only when there is no position (`NO_POSITION_TO_EXIT`), and
a request for more shares than are held is clamped to the position - an exit
can fully flatten a holding but can never cross below zero into a short. Long
only is structural: there is no short side to request.

### Decisions and errors

A decision carries `approved`, `approved_quantity`, a stable machine
`reason_code`, a human-readable `message`, and `max_allowed_quantity`:

```
APPROVED             INVALID_REQUEST      TRADING_DISABLED
DAILY_LOSS_LIMIT     POSITION_LIMIT       TOTAL_EXPOSURE_LIMIT
INSUFFICIENT_CASH    NO_POSITION_TO_EXIT  EXIT_QUANTITY_EXCEEDS_POSITION
```

A malformed *request* - a zero, negative, or fractional quantity, or a price
that is zero, negative, NaN, or infinite - is an ordinary rejected decision
with `INVALID_REQUEST`. A *context* that could not describe a real account -
zero equity, negative cash, a symbol exposure larger than the total - raises
`RiskInputError` instead, because that is a programming error rather than a
risk outcome. Nothing is silently repaired, and an ordinary risk denial never
raises.

See [docs/SPEC.md](docs/SPEC.md) section 8, "Phase 5 - Risk Engine", for the
full contract.

## Operational state (Phase 6)

Operational state lives in a **local SQLite file**. SQLite was chosen because
this is a single-user, single-process, local system: the standard library ships
the driver, so the whole feature adds **zero dependencies** - no ORM, no
migration framework, and no database service. There is no PostgreSQL, no
Supabase, no Redis, and no cloud database anywhere in this project.

`autotrader.state` is persistence infrastructure and nothing else. It stores
records; it decides nothing, orchestrates nothing, and contacts nobody. It
imports only the standard library, needs no credentials, and opens no socket.

```python
from datetime import UTC, datetime

from autotrader.state import connect, initialize_database, record_strategy_run

path = initialize_database("data/autotrader.db")
with connect(path) as connection:
    run_id = record_strategy_run(
        connection,
        strategy_name="EMA20/EMA50",
        mode="BACKTEST",
        started_at=datetime.now(UTC),
    )
```

**The database file is git-ignored** (`*.db`, `*.sqlite`, `*.sqlite3`, plus the
WAL sidecars), like everything else under `data/`. Nothing creates it
implicitly - a caller passes an explicit path - and the whole test suite writes
into temporary directories, so running `pytest` never creates a real database.

Every connection sets **`journal_mode = WAL`**, **`foreign_keys = ON`**, and a
5-second busy timeout. Foreign keys are per-connection in SQLite, so
configuring them once at creation time would silently disable referential
integrity for every later caller.

Writes are transactional. `transaction()` commits on success and rolls back on
**any** exception, so a failure halfway through a multi-write unit of work
leaves none of it behind. Nested use joins the outer transaction, which lets
several records be written as one atomic unit.

`initialize_database(path)` is idempotent - calling it repeatedly creates
nothing twice. The schema carries an explicit `schema_version` (currently `1`);
a database written by a newer version is **refused and left untouched** rather
than downgraded, and an inconsistent database raises a clear error instead of
being repaired. There is deliberately no migration framework and no database
CLI.

Eight tables exist. The last two arrived with schema **v2** in Phase 7:

| Table | Holds |
| --- | --- |
| `schema_metadata` | The schema version |
| `strategy_runs` | One logical strategy session |
| `signals` | Durable Phase 3 BUY/EXIT signals, linked to a run |
| `risk_events` | A generic risk-decision audit trail |
| `system_events` | General operational events |
| `positions` | The latest **local** position snapshot per symbol |
| `order_intents` | An order this system decided to place, written **before** the broker call |
| `broker_orders` | The latest normalized snapshot of what the broker said |

**There is still no fill or reconciliation persistence.** No `fills`,
`executions`, `broker_accounts`, or `reconciliation_runs` table exists: those
belong to Phase 8, and adding them correctly later is better than guessing
their shape now. `risk_events` and `broker_orders.status` are deliberately
opaque text for the same reason - Phase 5 owns what a risk decision means and
the broker owns its own order vocabulary, so this module stores the strings
without interpreting them.

Some deliberate invariants:

- **All timestamps are ISO-8601 UTC** in one canonical form. Aware values in
  another zone are converted; **naive datetimes are rejected**, because reading
  one as local time would silently misdate an audit record.
- **`EXIT` is stored as `EXIT`**, never rewritten as `SELL`. A signal is not a
  trade, and persistence must not make that translation on a caller's behalf.
- **The same logical signal cannot be stored twice** for one run. This is a
  storage invariant only - real order idempotency is Phase 7's problem.
- **`quantity >= 0`**, enforced in Python *and* as a SQLite `CHECK`, because the
  system is long only. `average_price` is either NULL or greater than zero. No
  P&L is stored.
- **Positions are local.** The persistence layer has never spoken to a broker,
  so an empty `positions` table means "no local snapshot", not "flat at the
  broker". It is only ever written from a position actually *observed* at the
  broker - never inferred from an order that was merely accepted.

### Schema migration (v1 -> v2)

A new database is created directly at v2. An existing v1 database is upgraded
by one small, explicit, **additive** migration: it creates the two new tables
and re-stamps the version marker, and drops, recreates, or rewrites nothing, so
every existing row survives. It runs in a single transaction, so a failure
rolls back and leaves the database on v1 rather than half-migrated. A test
asserts a migrated v2 database is schema-identical to a freshly created one.

There is still no migration *framework* - no Alembic, no version graph. A
database written by a newer version is refused; one older than v1 has no path
and is refused too.

See [docs/SPEC.md](docs/SPEC.md) section 8 for the full contract.

## Paper execution (Phase 7)

`autotrader.execution` turns a risk-approved decision into exactly one Alpaca
**paper** order. It is the only part of the repository that constructs a
trading client or submits an order.

```bash
export AUTOTRADER_PAPER_TRADING_ENABLED=true

python -m autotrader.cli paper-submit --symbol SPY --side BUY --qty 1 --dry-run

python -m autotrader.cli paper-submit \
  --symbol SPY --side BUY --qty 1 --confirm-paper PAPER
```

### PAPER ONLY

**There is no live mode.** The trading client is built as
`TradingClient(api_key, secret_key, paper=True)` with `paper=True` written
literally, in one function that takes no arguments. No public function accepts
a `paper` or `live` parameter, no CLI option selects an environment, and no
environment variable switches one. Live trading is not disabled by default -
it cannot be expressed. Tests assert `paper=False` appears nowhere in the
source and that no live CLI option exists.

### Two independent gates

Both are closed by default, and neither can satisfy the other:

1. `AUTOTRADER_PAPER_TRADING_ENABLED` must be exactly `true`. `TRUE`, `1`,
   `yes`, empty, and missing all leave submission disabled.
2. `--confirm-paper PAPER` must be typed exactly.

`--dry-run` requires neither, because it cannot submit: it reads the account,
positions, the clock, and the current price, runs the risk engine, prints the
preview, and stops. It persists no intent and calls no broker. Running it first
is the intended way to check an order - which is also why the confirmation
token is not required for it, so typing `PAPER` never becomes a habit.

### The pipeline

```
paper account + positions + current IEX price
        -> RiskContext
        -> evaluate_risk
        -> RiskDecision            (persisted to risk_events)
        -> OrderIntent             (persisted and COMMITTED first)
        -> duplicate preflight     (by client_order_id)
        -> Alpaca PAPER market order
        -> broker snapshot         (persisted to broker_orders)
```

- **The risk engine is never bypassed.** The quantity sent to the broker is
  always `RiskDecision.approved_quantity`. If risk clamps a request for 100
  shares to 3, the broker is asked for 3. A rejected decision means no broker
  request is even constructed.
- **The reference price is current**, taken from Alpaca's latest IEX trade -
  never a stored Parquet bar, and never a price supplied on the command line.
- **The intent is committed before the broker call.** A crash between the
  request and its response therefore still leaves a durable `client_order_id`
  to resolve against. Submitting first would leave a real order with no local
  trace.
- **`client_order_id` is `autotrader-<uuid4>`**, generated once per intent and
  never regenerated. It contains no credential and no account information.

### Failure semantics

| Situation | Result |
| --- | --- |
| Gate closed, wrong confirmation, missing credentials | Refused before any broker call |
| Untradable/blocked account | Fails closed, for BUY **and** SELL |
| No current price, or an unusable one | Fails closed |
| Risk rejects | No intent, no broker request; risk event persisted |
| Duplicate preflight finds an existing order | Snapshot stored; **nothing submitted** |
| Duplicate preflight cannot complete | **Fails closed** - "could not check" is never "no duplicate" |
| Broker definitively refuses (a 4xx) | Intent `REJECTED`; no order exists |
| Timeout, reset, 5xx, or unreadable status | Intent `UNKNOWN`; **never retried** |

**An ambiguous outcome is never retried.** A timeout after `submit_order` could
mean the broker accepted the order or never saw it. Re-sending it risks a
duplicate position, so the intent is marked `UNKNOWN`, an audit event is
written, and the attempt stops. The `client_order_id` is kept so Phase 8 can
ask the broker about that exact key. The CLI exits `2` for this case
specifically, so a script can never confuse it with an ordinary refusal.

The SDK's own internal retry of `429`/`504` responses is switched off on the
trading client for the same reason: a silently resubmitted `POST /orders` would
defeat this entirely.

### Accepted is not filled

A stored broker snapshot proves the broker **accepted** an order. Nothing
infers a position from that: the local `positions` table is only written from a
position actually observed at the broker, and a successful submission never
increments it. Reconciling local state against the broker is Phase 8.

### Not implemented

No reconciliation, no crash recovery, no automatic `UNKNOWN` resolution, no
fill history, no open-order synchronization, no position repair, no streaming
or websockets, and no live trading.

## Development

Run the tests:

```bash
pytest -q
```

Lint:

```bash
ruff check .
```

Format check:

```bash
ruff format --check .
```

## Layout

```
src/autotrader/data/        Alpaca historical bars -> canonical Parquet (Phase 1),
                            stored-dataset validation (Phase 2)
src/autotrader/cli/         Typer CLI (version, download, validate, backtest,
                            paper-submit)
src/autotrader/strategies/  EMA 20 / EMA 50 crossover signals (Phase 3)
src/autotrader/backtest/    deterministic next-bar-open backtester (Phase 4)
src/autotrader/risk/        deterministic risk decisions and sizing (Phase 5)
src/autotrader/state/       local SQLite operational state, schema v2 (Phases 6-7)
src/autotrader/execution/   Alpaca PAPER order execution (Phase 7) - the only
                            place a trading client exists or an order is sent
tests/                      offline tests; no test contacts the network
data/raw/                   downloaded market data (git-ignored)
data/processed/             validated market data (git-ignored)
data/autotrader.db          local operational state (git-ignored, not created
                            unless an application asks for it)
docs/SPEC.md                authoritative scope specification
```
