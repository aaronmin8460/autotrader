# autotrader

A personal, single-user automated trading system for **crypto spot**, built to
run as a local Python CLI process against an Alpaca **paper** account.

This is an engineering project. It makes **no claim of profitability**, and it
is not investment advice.

## Status: Crypto V0.2 plus the 24/7 runtime (Phase 9). Next: Phase 8 reconciliation, then integration

**Current active version: Crypto V0.2.** The system trades BTC/USD and ETH/USD
only, on 15-minute bars, 24 hours a day and 7 days a week.

**Phase 9 - the 24/7 runtime - is complete, and it does not trade yet.** The
runtime wakes on completed 15-minute UTC boundaries, fetches, validates,
evaluates the strategy, records signals and logs a heartbeat - but broker
submission stays off until an external startup-safety check says trading is
safe, and the shipped default says `UNRESOLVED` because Phase 8 reconciliation
is not integrated. That is the intended state: a process that assumes its local
view survived the last shutdown is how a duplicate position gets created.

**Archived milestone: Equity V0.1** is preserved at the Git tag
[`equity-v0.1-phase7`](#the-archived-equity-milestone). It was a complete,
working US-equities system through Alpaca paper execution. It is **not**
maintained here as a second mode: the pivot removed the equity path rather
than wrapping both in an asset-class switch, because a dual-market system
nobody runs is a liability, not a feature. Git history and that tag are how it
stays recoverable.

What exists today: downloading historical 15-minute crypto bars from Alpaca's
US crypto feed as Parquet, validating a stored dataset, the EMA 20 / EMA 50
signal generator, a deterministic backtester with fractional positions and a
modelled taker fee, a deterministic risk engine, a local SQLite
operational-state database at schema v3, a single deliberately awkward
paper-order command, and the 24/7 runtime that drives all of it on a
schedule. Validation never downloads or repairs data; the strategy
emits signals only; the backtester is local arithmetic; the risk engine is a
pure calculator that persists nothing; and the database stores records without
deciding anything.

**Live trading is not implemented, not configurable, and not reachable.** The
trading client is constructed with `paper=True` hardcoded; there is no
`--live` flag, no `--paper` option, no environment variable that selects an
environment, and no public function that accepts a parameter which could
change it. Live trading is not disabled by default - it is unexpressible, and
a test asserts `paper=False` appears nowhere in the source.

Submitting a paper order requires **two independent gates**, both closed by
default: the `AUTOTRADER_PAPER_TRADING_ENABLED=true` environment variable and
`--confirm-paper PAPER` on the command line. Every submission is sized by the
risk engine, then rounded **down** to the broker's own trade increment, so the
quantity sent is never more than the risk-approved one. The order intent and
its `client_order_id` are committed to SQLite *before* the broker is called.

**Reconciliation is not implemented.** The system creates the durable anchors
crash recovery will need but resolves nothing. An ambiguous submission outcome
is recorded as `UNKNOWN` and left alone - never retried, never re-keyed. There
is no fills, executions, or reconciliation table.

**The 24/7 runner exists, and it is deliberately not allowed to trade.** It
loops on completed 15-minute UTC boundaries, observes, and records - but
submission is gated on a startup-safety answer that only Phase 8 reconciliation
can give, so today it observes and nothing else. See
[The 24/7 runtime](#the-247-runtime).

## Scope summary

| | |
| --- | --- |
| Asset class | Crypto spot only |
| Broker | Alpaca only |
| Execution | Alpaca paper trading only - live is unreachable |
| Universe | BTC/USD, ETH/USD |
| Quote currency | USD only |
| Timeframe | 15-minute bars |
| Operation | 24 hours / 7 days |
| Direction | Long only |
| Leverage / shorting | None |
| Research strategy | EMA 20 / EMA 50 crossover (engineering validation only) |
| Historical storage | Parquet |
| Operational state | SQLite, local file, schema v3 |
| Quantities | Fractional, `decimal.Decimal` |
| Interface | Python CLI, local process - no web frontend |

Out of scope: live trading, US equities, options, futures, forex, perpetual
futures, non-USD quote currencies, shorting, leverage, margin, multiple
brokers, ML/LLM signal generation, web or mobile frontends, and cloud
deployment.

**[docs/SPEC.md](docs/SPEC.md) is the authoritative scope document.** Read it
before extending this project; it takes precedence over any chat history.

### The archived equity milestone

Equity V0.1 - SPY, QQQ, AAPL, MSFT, NVDA on Alpaca's IEX feed, whole shares,
DAY market orders - is tagged:

```bash
git show equity-v0.1-phase7
```

Nothing in the active tree depends on it. There is no equity symbol, no
`StockHistoricalDataClient`, no IEX feed, no whole-share rule, no
`TimeInForce.DAY`, and no market clock in production code; a test suite asserts
each of those absences against the executable source rather than the prose.

## Setup

Requires Python 3.11. The repository ships with no virtual environment; create
one and install the project in editable mode.

```bash
python3.11 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
```

### Credentials

**Crypto market data needs no credentials.** Alpaca serves crypto bars
unauthenticated, so `download` and `validate` work with nothing configured. If
`ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are set they are used, which raises
the provider's rate limit:

```bash
export ALPACA_API_KEY=...
export ALPACA_SECRET_KEY=...
```

**Paper trading does need them.** Use your **paper** keys. `.env.example`
documents the variable names. If you keep a local `.env`, it is git-ignored and
must never be committed. Credentials are never logged, printed, persisted,
written into generated files, stored in SQLite, embedded in a
`client_order_id`, or included in an exception message.

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

Download historical crypto bars:

```bash
python -m autotrader.cli download --symbol BTC/USD --timeframe 15m --start 2025-01-01 --end 2025-12-31
```

`--start` and `--end` are **UTC calendar dates**, and `--end` is inclusive.
There is no exchange session to anchor a day to, so there is no
`America/New_York` anywhere in the data path. `--symbol` accepts only `BTC/USD`
and `ETH/USD`, and `15m` is the only supported timeframe.

Alpaca's crypto endpoint treats its own `end` as *inclusive*, and a 24/7 market
has a bar stamped at exactly midnight, so the request boundary is the last
instant of the end date. A one-day download returns exactly 96 bars rather than
97.

### The BTC_USD filename slug

A slash cannot appear in a flat filename, so the pair is slugged for the
filesystem **only**:

```
data/raw/BTC_USD_15m_2025-01-01_2025-12-31.parquet
data/raw/BTC_USD_15m_2025-01-01_2025-12-31.metadata.json
```

The canonical symbol stays `BTC/USD` everywhere it means something: in the
stored DataFrame, in the metadata sidecar, in the database, in every domain
model, and on the command line. It is never rewritten as `BTCUSD` - two
spellings of one market is how stored datasets stop reconciling - and
`--symbol BTCUSD` is rejected rather than silently reinterpreted. The sidecar
records both forms, as `symbol` and `symbol_slug`.

Each stored row is `timestamp, symbol, open, high, low, close, volume,
trade_count, vwap`, with timezone-aware UTC timestamps in ascending order.

**Everything under `data/` is local and git-ignored.** Downloaded market data
is never committed; the repository is reproducible from source plus a
re-fetch.

Validate a downloaded dataset:

```bash
python -m autotrader.cli validate data/raw/BTC_USD_15m_2025-01-01_2025-12-31.parquet
```

The command reads the file and reports whether it is structurally and
internally consistent enough for later stages to consume. It checks the exact
canonical columns, a non-empty dataset, timezone-aware UTC timestamps that are
unique and ascending, a single supported symbol, positive finite OHLC values
with `high`/`low` bounding `open` and `close`, non-negative volume, and - where
present - non-negative `trade_count` and positive `vwap`.

It deliberately does **not** check bar-to-bar spacing or bar freshness.
Crypto is continuous, but a provider outage can still leave a gap, and
"did we receive the newest completed bar?" is a runtime question that belongs
to the future 24/7 runner rather than to structural validation. There is no
exchange calendar here and no session logic: a Saturday bar and a 03:00 UTC bar
are ordinary data.

```
VALID

File:   data/raw/BTC_USD_15m_2025-01-01_2025-12-31.parquet
Rows:   34926
Symbol: BTC/USD
Errors: 0
```

Exit codes are `0` for a valid dataset, `1` when validation errors were found,
and `2` when the file cannot be read at all.

## Strategy signals

`autotrader.strategies.ema_cross` generates EMA 20 / EMA 50 crossover signals
from a canonical bar frame. It exists to validate the engineering pipeline and
is **not a claim of profitability**.

```python
from autotrader.strategies import generate_ema_cross_signals

signals = generate_ema_cross_signals(bars)  # -> list[Signal], ascending by timestamp
```

**The pivot changed nothing here.** The crossover reads a close price and a
symbol string; nothing in it was ever asset-class specific, so `BTC/USD` works
exactly as `SPY` did. No crypto-specific indicator was added - no RSI, no MACD,
no sentiment, no ML, and no parameter optimization.

Long only, two signal types. `BUY` when the fast EMA moves from at-or-below the
slow EMA to strictly above it; `EXIT` when it moves from at-or-above to
strictly below. Both EMAs use `adjust=False` and stay undefined through their
warm-up, so no signal can be produced before 50 bars have been observed. A
crossover produces at most one signal.

A signal's timestamp is the bar whose **close** made the crossover knowable. It
is **not** an execution timestamp, and a signal carries no price.

## Backtesting

```bash
python -m autotrader.cli backtest data/raw/BTC_USD_15m_2025-01-01_2025-12-31.parquet
```

**This is engineering validation, not a profitability claim.** It exists to
prove the pipeline accounts correctly. Its results are not a reason to trade
anything, and they are not investment advice. Nothing is downloaded, written,
or ordered: the whole simulation is local arithmetic.

### The rules it simulates

**Next-bar-open execution - no look-ahead.** A crossover on bar *t* is knowable
only once bar *t* has closed, so the earliest it can be acted on is the open of
bar *t+1*. A signal is **never** filled on its own bar, and every execution's
timestamp is strictly later than its signal's. A signal on the final bar is
left unexecuted rather than filled at an invented price. Crypto is continuous,
so "the next bar" is simply the next bar - there is no session boundary to
reason about.

**Fractional positions, in `Decimal`.** Crypto is fractionable, so the equity
milestone's `floor(cash / price)` whole-share rule is gone. There is
deliberately **no one-whole-coin minimum**: $100 buys a fraction of a $100,000
coin. Quantities and cash are `decimal.Decimal` rather than binary floats, so
an accounting identity that should hold exactly does hold exactly, and a
position size is quantized down to `1e-18` - fine enough to be continuous,
coarse enough to be reproducible.

Provider increments are deliberately **not** modelled here. They change, and a
historical simulation must stay reproducible; the broker's live asset metadata
is the authority at the execution boundary instead.

**A conservative 0.25% taker fee, on both sides.** `TAKER_FEE_RATE` is
`Decimal("0.0025")`, charged on every executed BUY and SELL. Read it for what
it is:

- it is a **deliberately simple V0.2 backtest assumption**, not a fee schedule;
- it models the cost of crossing the spread with a market order;
- Alpaca's real crypto fees depend on 30-day trailing volume tiers and on
  provider rules that change, and none of that is implemented;
- it is **not** billing or reconciliation logic.

It exists so a backtest cannot report a return that silently assumes trading is
free. Zero fees was an acceptable equity-era baseline; for a strategy that
round-trips hundreds of times a year it is not.

**Sizing reserves the fee.** A BUY solves `q * price * (1 + fee) <= cash`
rather than spending every dollar on notional and discovering the fee
afterwards. Cash can therefore never go negative - a fee that pushed a balance
below zero would be an accounting bug, not a modelling choice - and the
boundary is explicitly tested.

**$100,000 initial capital, long only.** One symbol, at most one position, no
leverage, no borrowing, no short selling. An `EXIT` while long sells the entire
position. An `EXIT` while flat and a `BUY` while already long are both no-ops.

**Open positions are marked, not liquidated.** A position still open on the
final bar is *not* force-sold and no closing trade is fabricated. It is marked
to market at the final bar's close.

**Metrics.** `total_return` is `(final_equity / initial_cash) - 1` and
`max_drawdown` is the worst peak-to-trough decline of the end-of-bar equity
curve. Both are decimal fractions rendered as percentages; there is no
annualization, benchmark, or risk-adjusted metric.

Invalid data stops the run: the dataset is validated first, and any finding
aborts the backtest rather than being silently repaired. Exit codes are `0` for
a completed simulation, `1` when the dataset or the starting cash is unusable,
and `2` when the file cannot be read.

## Risk engine

`autotrader.risk` answers one question about a **proposed** trade: may it be
allowed, and if so, what is the largest safe quantity?

It is a calculator and nothing else. It submits no order, builds no broker
client, makes no network call, opens no database, and writes no file. It is
also **not** wired into the backtester, which keeps its own all-cash sizing
baseline.

### The V0.2 limits

These are **engineering safety defaults, not investment advice** and not a
recommended allocation. The pivot did not loosen any of them.

| Limit | Value |
| --- | --- |
| Maximum market value of any one symbol | 5% of current equity |
| Maximum aggregate long exposure | 30% of current equity |
| Daily loss at which new entries halt | -2% of the UTC-day baseline |
| Direction | Long only |
| Leverage | None - an entry may only spend cash on hand |
| Quantities | Fractional `Decimal`; no whole-unit rule |

Every limit is a **USD notional** ceiling. It bounds the market *value* of a
position, not a count of units, so the arithmetic is
`quantity * reference_price` measured against a dollar cap. Only the quantity
representation changed in the pivot.

There is deliberately no `whole_shares_only` policy field any more: crypto is
fractionable, and a flag whose only legal value is "off" is not a policy.

### Quantities are exact Decimals

A `float` quantity is **refused rather than converted**. A binary float is an
approximation of the number the caller meant, and an approximation must never
become the size of a real order. NaN, both infinities, negative values, and
zero are rejected too. The CLI parses `--qty` text straight into a `Decimal`,
so `0.0001` typed at a prompt is exactly `0.0001` all the way to the broker.

### Entries are gated

A BUY must clear a well-formed request, the `trading_enabled` kill switch, and
the daily-loss halt, and is then sized against the **tightest** of three
ceilings:

```
position_remaining  = max(0, equity * 0.05 - symbol_exposure)
portfolio_remaining = max(0, equity * 0.30 - total_exposure)
max_notional        = min(position_remaining, portfolio_remaining, cash)
max_quantity        = max_notional / reference_price   (rounded down)
```

An oversized BUY is **clamped** to `max_quantity` and approved, with the
binding constraint reported as the reason. Only genuinely *zero* headroom
rejects now: $249.99 of room against a $250 asset used to be a rejection and is
now an approved fractional order.

### Exits are not

Every limit above exists to stop the account **adding** risk. None of them may
stop it **removing** risk, so a SELL against an existing long is evaluated
separately: the kill switch, the daily-loss halt, and both exposure caps are
entry gates and none of them can block an exit. A kill switch that trapped an
open position would be a safety defect, not a safety feature.

A SELL is rejected only when there is no position, and a request for more than
is held is clamped to the position - an exit can fully flatten a holding but
can never cross below zero into a short.

### The risk day is a UTC calendar day

A 24/7 market has no previous close, so Alpaca's `last_equity` - the *equity
session's* prior close - is **not** the crypto daily-loss baseline and is not
read anywhere in the active code.

Instead, the first account equity observed on a UTC calendar date is recorded
durably in `daily_risk_baselines` and reused for the rest of that date, so
`daily_pnl = current_equity - baseline` and the halt survives a process restart
and cannot be reset by re-running a command. The day runs 00:00 UTC to the next
00:00 UTC.

**An honest limitation:** this is the first equity the system *observed* on that
date, not the equity at exactly 00:00 UTC. Nothing in this milestone runs
continuously, so a day whose first observation is at 14:00 UTC is measured from
14:00 UTC. The stored `captured_at` records how close that was. A 24/7 runner
(Phase 9) is what will make the first observation land near the boundary.

### Decisions and errors

A decision carries `approved`, `approved_quantity`, a stable machine
`reason_code`, a human-readable `message`, and `max_allowed_quantity`:

```
APPROVED             INVALID_REQUEST      TRADING_DISABLED
DAILY_LOSS_LIMIT     POSITION_LIMIT       TOTAL_EXPOSURE_LIMIT
INSUFFICIENT_CASH    NO_POSITION_TO_EXIT  EXIT_QUANTITY_EXCEEDS_POSITION
```

A malformed *request* is an ordinary rejected decision with `INVALID_REQUEST`.
A *context* that could not describe a real account raises `RiskInputError`
instead, because that is a programming error rather than a risk outcome.

## Operational state

Operational state lives in a **local SQLite file**. SQLite was chosen because
this is a single-user, single-process, local system: the standard library ships
the driver, so the whole feature adds **zero dependencies** - no ORM, no
migration framework, and no database service.

```python
from autotrader.state import connect, initialize_database

path = initialize_database("data/autotrader.db")
```

**The database file is git-ignored** (`*.db`, `*.sqlite`, `*.sqlite3`, plus the
WAL sidecars). Every connection sets **`journal_mode = WAL`**,
**`foreign_keys = ON`**, and a 5-second busy timeout - foreign keys are
per-connection in SQLite, so configuring them once would silently disable
referential integrity for every later caller.

Writes are transactional: `transaction()` commits on success and rolls back on
**any** exception. All timestamps are ISO-8601 UTC in one canonical form, and
**naive datetimes are rejected**.

Nine tables exist:

| Table | Holds |
| --- | --- |
| `schema_metadata` | The schema version |
| `strategy_runs` | One logical strategy session |
| `signals` | Durable BUY/EXIT signals, linked to a run |
| `risk_events` | A generic risk-decision audit trail |
| `system_events` | General operational events |
| `positions` | The latest **local** position snapshot per symbol |
| `order_intents` | An order this system decided to place, written **before** the broker call |
| `broker_orders` | The latest normalized snapshot of what the broker said |
| `daily_risk_baselines` | The UTC-day equity baseline the loss halt measures against |

**There is still no fill or reconciliation persistence.** No `fills`,
`executions`, `broker_accounts`, or `reconciliation_runs` table exists: those
belong to Phase 8.

### Exact decimal quantities (schema v3)

Every broker-critical quantity is stored as **canonical decimal text** and read
back as a `decimal.Decimal`:

| Column | v2 | v3 |
| --- | --- | --- |
| `positions.quantity` | `INTEGER` | `TEXT` decimal |
| `order_intents.requested_quantity` | `INTEGER` | `TEXT` decimal |
| `order_intents.approved_quantity` | `INTEGER` | `TEXT` decimal |
| `broker_orders.quantity` | `INTEGER` | `TEXT` decimal |
| `broker_orders.filled_quantity` | `INTEGER` | `TEXT` decimal |

The text is plain fixed-point - `0.0001`, never `1E-4` - and preserves the
scale it was written with, so `1.25000000` round-trips as `1.25000000`. A
`float` is refused rather than converted. The storage string is an
implementation detail: no read model exposes it, and every public quantity is a
`Decimal`.

**Price columns stayed `REAL`, deliberately.** The audit covered every money
and quantity column. `INTEGER` quantities had to move because a whole number
cannot represent a fractional coin at all; a `REAL` price already represents a
fractional USD mark, and moving prices to text would discard the
`CHECK (... > 0)` constraints that make an impossible price unstorable even by
a writer bypassing this module. A price here is a mark, never a quantity.

### Schema migration (v1 -> v2 -> v3)

A new database is created directly at v3. An older one is upgraded through an
explicit ordered path, in a single transaction, so a failed upgrade rolls back
and leaves the database on its original version rather than half-migrated.

v1 -> v2 is additive. **v2 -> v3 is not**, and that is the interesting part:
SQLite cannot retype a column, so `positions`, `order_intents`, and
`broker_orders` are rebuilt - renamed aside, recreated from the *same* literal
a fresh database uses, copied across row by row with quantities converted, and
the old copies dropped. Because the new table is created under its real name
rather than renamed into place, a migrated database's stored schema is
byte-identical to a fresh one, and a test asserts exactly that.

Existing data survives. An integer `1` becomes the decimal `"1"` and `100`
becomes `"100"` - the same number, written out, with no scale invented and no
row dropped. Referential integrity is suspended for the rebuild and re-checked
before the transaction commits; a violation rolls the whole upgrade back.

A database written by a **newer** version is still refused and left untouched.

## Paper execution

`autotrader.execution` turns a risk-approved decision into exactly one Alpaca
**paper** crypto order. It is the only part of the repository that constructs a
trading client or submits an order.

```bash
export AUTOTRADER_PAPER_TRADING_ENABLED=true

python -m autotrader.cli paper-submit --symbol BTC/USD --side BUY --qty 0.0001 --dry-run

python -m autotrader.cli paper-submit \
  --symbol BTC/USD --side BUY --qty 0.0001 --confirm-paper PAPER
```

### PAPER ONLY

**There is no live mode.** The trading client is built as
`TradingClient(api_key, secret_key, paper=True)` with `paper=True` written
literally, in one function that takes no arguments. No public function accepts
a `paper` or `live` parameter, no CLI option selects an environment, and no
environment variable switches one. Live trading is not disabled by default - it
cannot be expressed.

### Two independent gates

Both are closed by default, and neither can satisfy the other:

1. `AUTOTRADER_PAPER_TRADING_ENABLED` must be exactly `true`.
2. `--confirm-paper PAPER` must be typed exactly.

`--dry-run` requires neither, because it cannot submit: it reads the account,
positions, the asset's broker metadata, and the current price, runs the risk
engine, prints the preview, and stops. Running it first is the intended way to
check an order.

### The pipeline

```
paper account + positions + asset metadata + current crypto price
        -> UTC-day equity baseline   (daily_risk_baselines)
        -> RiskContext
        -> evaluate_risk
        -> RiskDecision              (persisted to risk_events)
        -> round DOWN to the broker's trade increment
        -> OrderIntent               (persisted and COMMITTED first)
        -> duplicate preflight       (by client_order_id)
        -> Alpaca PAPER MARKET order, GTC
        -> broker snapshot           (persisted to broker_orders)
```

- **The risk engine is never bypassed.** The quantity sent to the broker is
  never larger than `RiskDecision.approved_quantity`. A rejected decision means
  no broker request is even constructed.
- **The reference price is current**, taken from Alpaca's latest crypto trade -
  never a stored Parquet bar, and never a price supplied on the command line.
- **The intent is committed before the broker call.** A crash between the
  request and its response therefore still leaves a durable `client_order_id`
  to resolve against.
- **`client_order_id` is `autotrader-<uuid4>`**, generated once per intent and
  never regenerated. It contains no credential and no account information.

### The broker owns order precision

Before any submission, the asset is read from the broker and must be crypto
(not an equity and not a perpetual future), active, tradable, and fractionable,
and must report both a `min_order_size` and a `min_trade_increment`. Anything
missing or contradictory fails closed.

**No BTC or ETH increment is hardcoded anywhere.** Provider rules change, so
the live metadata is the authority. The risk-approved quantity is rounded
**down** to that increment - never up, because rounding up would send more than
risk allowed - and if the result lands below the broker's minimum there is no
valid order and that is reported rather than papered over.

The SDK's request field is typed as a float, so the exact `Decimal` becomes one
at the very last step - and only after checking that the value the broker will
actually receive is not *larger* than the approved quantity. A quantity may
shrink on the way to the broker; it may never grow.

### MARKET, GTC, and nothing else

Time in force is **`GTC`**. `DAY` expires at a session close that a 24/7 market
does not have, and `IOC` would silently cancel the unfilled part of an order
this system believes it placed. There is no limit, stop, stop-limit, trailing,
bracket, or OCO order, and `notional` is never set - a notional order would be
sized in dollars by the broker rather than by the risk engine.

### No market clock

Crypto trades continuously, so there is no session to open or close and nothing
to gate a submission on. `get_clock()` is an equity-market concept and is not
called; the CLI prints no `Market: OPEN/CLOSED` line. Deciding *when* to act -
on completed 15-minute bars - is Phase 9's job.

### Failure semantics

| Situation | Result |
| --- | --- |
| Gate closed, wrong confirmation, missing credentials | Refused before any broker call |
| Untradable/blocked account | Fails closed, for BUY **and** SELL |
| Asset metadata missing, stale, or contradictory | Fails closed |
| Quantity below the broker's minimum after rounding down | Fails closed; nothing is submitted |
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
specifically. The SDK's own internal retry of `429`/`504` responses is switched
off on the trading client for the same reason.

### Accepted is not filled

A stored broker snapshot proves the broker **accepted** an order. Nothing
infers a position from that: the local `positions` table is only written from a
position actually observed at the broker, and a successful submission never
increments it.

### Not implemented

No reconciliation, no crash recovery, no automatic `UNKNOWN` resolution, no
fill history, no open-order synchronization, no position repair, no streaming
or websockets, and no live trading.

## The 24/7 runtime

`crypto-run` is the long-running process. It adds no trading logic: the data,
validation, strategy, risk, state, and paper-execution stages are the existing
ones, and it is the schedule and the safety envelope around them.

Run one completed-bar cycle and exit - the intended way to check the runtime by
hand, and safe with no gate open:

```bash
python -m autotrader.cli crypto-run --once --observe-only
```

Run it continuously:

```bash
python -m autotrader.cli crypto-run --confirm-paper-runtime PAPER
```

### The schedule

The runtime wakes at `00`, `15`, `30` and `45` minutes past every hour, **UTC,
every day of the week**. Weekends are ordinary trading days. The next wake-up
is recomputed from the wall clock each cycle rather than by sleeping 900
seconds repeatedly, because a fixed sleep accumulates every delay and drifts
off the boundary within a day.

### Completed bars only

Alpaca stamps a crypto 15-minute bar at its **interval start**, and it serves
the interval that is still running: at 00:16 UTC it will hand you a bar stamped
00:15, whose close has not happened. So a bar is processed only once

```
bar_timestamp + 15 minutes <= now - safety_delay
```

An in-progress candle is never evaluated. `--safety-delay` (5 seconds by
default) covers the provider's publication lag and is subtracted from `now`
everywhere completeness is judged, so waking early cannot smuggle an
unpublished bar through.

Each cycle fetches one bounded window of 200 completed bars per symbol - enough
for the EMA 50 to have forgotten its seed, and small enough to be one cheap
request. Two provider calls every fifteen minutes for the whole system: nothing
polls, and the account and positions are read only when a signal actually needs
sizing.

### One bar, one decision

BTC/USD is processed to completion before ETH/USD is looked at, so two signals
landing on the same boundary cannot size themselves against the same stale cash
figure. Only the **newest completed bar** may cause an action: older crossovers
in the lookback exist to establish EMA state and are never replayed. A
per-symbol checkpoint means a completed bar is acted on at most once per
process, even if the provider repeats it.

Cross-restart exactly-once recovery is Phase 8's, and is deliberately not
invented here.

### Three gates, all closed by default

Unattended paper execution requires **all** of:

1. `AUTOTRADER_PAPER_TRADING_ENABLED=true` in the environment - the same C7
   gate, not bypassed;
2. `--confirm-paper-runtime PAPER`, which authorizes *this process* for its
   lifetime. A daemon cannot have a token typed every fifteen minutes, so the
   confirmation moved to process start rather than being removed;
3. a startup-safety check reporting that trading is safe - which, until Phase 8
   is integrated, it never does.

`--observe-only` goes further than refusing: it constructs no execution path at
all. There is still no live mode, no `--live`, and no `paper=False`.

### When something goes wrong

| Failure | What happens |
| --- | --- |
| Provider error, invalid bars, strategy input violation | logged; no order; retried next cycle |
| Risk rejection, including an EXIT while flat | an ordinary no-order result, not a failure |
| Ambiguous `UNKNOWN` submission outcome | **trading paused for the process**; exit `2` |
| Rejected credentials, untradable account, broken state | stops, fails closed; exit `1` |

An `UNKNOWN` outcome means an order may or may not exist at the broker. Nothing
here resolves it, and nothing else is submitted on top of it.

### Monitoring and operation

Structured `event=... key=value` lines on the standard library's `logging`,
written to stdout - which is what systemd and journald already collect, so no
log file is required in the repository. A heartbeat reports the runtime start,
the last cycle, the last successful cycle, the last processed bar per symbol,
whether execution is enabled and why not, counts, and the last error. No
Telegram, no Slack, no webhook, no agent.

Only one runner may hold a database at a time, enforced by an `fcntl` lock on
`<database>.runtime.lock` and released in a `finally`. A second runner exits
non-zero before it fetches a bar. `SIGINT` and `SIGTERM` stop the process
cleanly: no new cycle, no new submission, the strategy run closed, the lock
released.

## Development

```bash
pytest -q
```

```bash
ruff check . && ruff format --check .
```

Every automated test is offline: the Alpaca boundary is mocked, the fakes
return real alpaca-py models so normalization is exercised against real
response shapes, no real credential is read, and sockets are asserted shut.
Source-level tests scan *executable* code with docstrings and comments
stripped, so prose describing a forbidden construct cannot mask its presence.

## Layout

```
src/autotrader/data/        Alpaca crypto bars -> canonical Parquet, and
                            stored-dataset validation
src/autotrader/cli/         Typer CLI (version, download, validate, backtest,
                            paper-submit, crypto-run)
src/autotrader/strategies/  EMA 20 / EMA 50 crossover signals
src/autotrader/backtest/    deterministic next-bar-open backtester, fractional
                            quantities, modelled taker fee
src/autotrader/risk/        deterministic risk decisions and sizing
src/autotrader/state/       local SQLite operational state, schema v3
src/autotrader/execution/   Alpaca PAPER crypto execution - the only place a
                            trading client exists or an order is sent
src/autotrader/runtime/     the 24/7 loop: UTC boundary scheduling, bounded
                            bar fetching, startup safety, duplicate
                            protection, heartbeat, process lock
tests/                      offline tests; no test contacts the network
data/raw/                   downloaded market data (git-ignored)
data/processed/             validated market data (git-ignored)
data/autotrader.db          local operational state (git-ignored)
docs/SPEC.md                authoritative scope specification
```

## What comes next

**Phase 8 - Reconciliation / Crash Recovery**, developed in parallel on its own
branch. It owns resolving `UNKNOWN` intents against the broker, startup
broker-vs-local reconciliation, open-order synchronization, fill history, and
position repair.

Then the **integration gate**: Phase 8's reconciliation result becomes the
startup-safety answer the runtime already asks for and already fails closed
against. Until that lands, the runtime observes and does not trade - by design,
not by accident.

**Phase 10 - Deployment** comes after that. Supervising a process that is not
yet allowed to trade would be premature.
