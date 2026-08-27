# autotrader

A personal, single-user automated trading system for US equities, built to run
as a local Python CLI process against an Alpaca **paper** account.

This is an engineering project. It makes **no claim of profitability**, and it
is not investment advice.

## Status: Phase 6 complete - local SQLite operational state

There is **no trading in this repository, and none is planned within the
current milestone** - no live trading and no paper trading. No order is ever
submitted, and no Alpaca trading client is constructed anywhere in the code.

What exists today are five capabilities: downloading historical 15-minute
US-equity bars from Alpaca's IEX feed and storing them locally as Parquet,
validating a stored dataset against that canonical schema, the EMA 20 / EMA 50
signal generator that turns those bars into BUY/EXIT signals, a deterministic
backtester that simulates what those signals would have done, and a local
SQLite database that can durably record operational state. Validation never
downloads, modifies, or repairs data; the strategy emits signals only; the
backtester is local arithmetic over a DataFrame; and the database only stores
records - it decides nothing and contacts nobody.

The risk engine (Phase 5) is being developed independently and is **not**
complete. Broker connectivity (Phase 7) and reconciliation (Phase 8) are not
implemented, and the database deliberately has no table for broker orders,
fills, executions, or reconciliation runs.

## Scope summary

| | |
| --- | --- |
| Market | US equities only |
| Broker | Alpaca only |
| Execution | Paper trading only (later phase) |
| Universe | SPY, QQQ, AAPL, MSFT, NVDA |
| Timeframe | 15-minute bars |
| Direction | Long only |
| Research strategy | EMA 20 / EMA 50 crossover (engineering validation only) |
| Historical storage | Parquet |
| Operational state | SQLite, local file (Phase 6) |
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

Downloading historical data requires Alpaca API credentials, read from the
process environment:

```bash
export ALPACA_API_KEY=...
export ALPACA_SECRET_KEY=...
```

`.env.example` documents the variable names. If you keep a local `.env`, it is
git-ignored and must never be committed. Credentials are only ever used to
authenticate market-data requests; they are never logged, printed, or written
into generated files.

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
There is no trading command.

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

Six tables exist:

| Table | Holds |
| --- | --- |
| `schema_metadata` | The schema version |
| `strategy_runs` | One logical strategy session |
| `signals` | Durable Phase 3 BUY/EXIT signals, linked to a run |
| `risk_events` | A generic risk-decision audit trail |
| `system_events` | General operational events |
| `positions` | The latest **local** position snapshot per symbol |

**There is no broker or order persistence yet.** No `broker_orders`, `fills`,
`executions`, `broker_accounts`, or `reconciliation_runs` table exists, because
those records describe an external system this repository does not talk to.
Adding them correctly in Phase 7/8 is better than guessing their shape now.
`risk_events` is deliberately generic for the same reason: Phase 5 owns what a
risk decision means, and this module stores opaque text rather than importing
or mirroring its model.

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
- **Positions are local.** Nothing here has ever spoken to a broker, so an
  empty `positions` table means "no local snapshot", not "flat at the broker".

See [docs/SPEC.md](docs/SPEC.md) section 8, "Phase 6 - SQLite Operational
State", for the full contract.

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
src/autotrader/cli/         Typer CLI (version, download, validate, backtest)
src/autotrader/strategies/  EMA 20 / EMA 50 crossover signals (Phase 3)
src/autotrader/backtest/    deterministic next-bar-open backtester (Phase 4)
src/autotrader/risk/        empty stub (Phase 5, developed separately)
src/autotrader/state/       local SQLite operational state (Phase 6)
tests/                      offline tests; no test contacts the network
data/raw/                   downloaded market data (git-ignored)
data/processed/             validated market data (git-ignored)
data/autotrader.db          local operational state (git-ignored, not created
                            unless an application asks for it)
docs/SPEC.md                authoritative scope specification
```
