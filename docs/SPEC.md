# autotrader - Project Specification (v0.2)

**Status:** Crypto V0.2 through Phase 9 complete, and Phase 8 and Phase 9 are
**integrated**. The active system is crypto spot - BTC/USD and ETH/USD,
15-minute bars, 24/7 - it can place an order only into Alpaca's paper
environment, it reconciles local state against that account at every process
start, and it runs unattended on completed 15-minute UTC bars. Startup
reconciliation is the trading authority: no green result, no new paper order.
**No integrated paper BUY has been observed end to end yet.** Next: the
integrated crypto paper smoke gate, then failure injection, then Phase 10
deployment.
**Archived milestone:** Equity V0.1 is preserved at the Git tag
`equity-v0.1-phase7`.
**Last updated:** 2026-08-28

This document is the authoritative scope definition for this repository. When
this document and any prior conversation, chat history, or memory disagree,
**this document wins**. Change the scope by editing and committing this file,
not by asserting a change in conversation.

---

## 1. Purpose

`autotrader` is a personal, single-user automated trading system for **crypto
spot**. It is an engineering project first: the objective is a correct, safe,
reproducible trading pipeline that can be trusted to run unattended against a
**paper** brokerage account.

This is not a product, not a service for others, and not a claim that any
strategy in it is profitable.

---

## 2. Engineering principles

In priority order. When two principles conflict, the higher-numbered one
yields.

1. **Correctness** - a wrong number is worse than a missing feature.
2. **Safety** - the system must fail closed. No accidental orders, ever.
3. **Reproducibility** - the same inputs must produce the same outputs;
   state and data must be inspectable after the fact.
4. **Simplicity** - the smallest implementation that satisfies the current
   milestone. No abstractions for hypothetical future requirements.
5. **Low operating cost** - local process, local files, free/low-cost data.

Feature count is explicitly *not* a goal.

---

## 3. V0.2 scope (frozen)

| Dimension | Decision |
| --- | --- |
| Asset class | Crypto spot only |
| Broker | Alpaca only |
| Execution environment | Paper trading only |
| Universe | BTC/USD, ETH/USD |
| Quote currency | USD only |
| Primary timeframe | 15-minute bars |
| Operation | 24 hours / 7 days |
| Direction | Long only |
| Leverage / shorting | None |
| Research strategy | EMA 20 / EMA 50 crossover |
| Historical data storage | Parquet |
| Operational trading state | SQLite, local file, schema v3 |
| Quantity representation | Fractional `decimal.Decimal` |
| Application style | Python CLI / local process |
| Frontend | None |

### 3.1 Market and universe

Crypto spot, two liquid pairs: **BTC/USD** and **ETH/USD**. The universe is
fixed for V0.2. Expansion - another pair, another quote currency, another asset
class - is a scope change requiring an edit to this document.

Symbols use the provider's canonical pair form, slash included. `BTCUSD` is
**not** an accepted spelling and is never silently reinterpreted: two spellings
of one market is how stored datasets stop reconciling. A filesystem-safe slug
(`BTC_USD`) exists for filenames only.

### 3.1E Equity V0.2 market and universe

Equity V0.2 is a **second product on the same account**, developed on the same
architecture and gated the same way. It is not a mode of the crypto product and
the crypto product is not a mode of it.

| Dimension | Decision |
| --- | --- |
| Asset class | US equities (cash) |
| Broker | Alpaca only |
| Execution environment | Paper trading only |
| Market data feed | Alpaca stock, **IEX** |
| Universe | SPY, QQQ, IWM, AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA |
| Primary timeframe | 15-minute bars |
| Operation | **US regular market hours only** |
| Direction | Long only |
| Leverage / shorting | None |
| Quantity representation | **Whole shares**, integral `decimal.Decimal` |
| Order type | MARKET, `TimeInForce.DAY`, regular hours |
| Research strategy | The same EMA 20 / EMA 50 crossover |

Exactly **ten** symbols, and the tuple order is the processing order. Adding an
eleventh is a scope change requiring an edit to this document; it is not a
configuration value. An unbounded universe would make the per-cycle API cost,
the risk arithmetic and the reconciliation scope all depend on something nobody
wrote down.

The feed is IEX because that is what an Alpaca Basic subscription serves; `sip`
returns a 403 without a paid data plan. IEX is a single-venue feed, so a symbol
with no IEX prints in an interval simply has no bar for it. That is visible in
the data and is not corrected for.

**The quantity policy is whole shares, rounded down.** Alpaca reports these
symbols as fractionable, and Equity V0.2 does not use it: a whole-share order
needs no notional handling, no fractional-order time-in-force restriction, and
no per-symbol increment metadata - Alpaca reports `min_order_size` and
`min_trade_increment` as `null` for equities. The risk engine's approved
quantity is floored to an integer number of shares at the execution boundary,
so the broker is never asked for more than risk approved, and a quantity that
floors below one share is refused rather than rounded up. One consequence is
stated rather than hidden: a whole-share exit cannot fully close a *fractional*
position, and nothing in this branch can create one.

### 3.2 Data frequency

**15-minute bars** are the primary timeframe. Daily bars may be derived from
stored data if a later phase needs them. Sub-minute, tick, and order-book data
are out of scope.

### 3.3 Initial research strategy

**EMA 20 / EMA 50 crossover, long only.**

This strategy exists to validate the *engineering pipeline* end to end - data
-> signal -> risk -> intent -> execution -> reconciliation. It is a test
fixture, not an edge. No claim is made or implied that it is profitable, and
its backtest results must never be used to justify enabling live trading, nor
to justify tuning its parameters.

### 3.4 Time

Crypto trades continuously. There is no exchange session anywhere in the active
system: no market open, no close, no holiday calendar, and no
`America/New_York`. A **day** means a **UTC calendar day**, 00:00 UTC to the
next 00:00 UTC, and that is the only day boundary the system recognises.

### 3.5 Equity time

Equities do **not** trade continuously, so Equity V0.2 does not schedule on UTC
boundaries alone. The rules, in full:

- Session times come from **the broker's own calendar endpoint**. Holidays,
  weekends and early closes are read, never assumed. `Mon-Fri 09:30-16:00` is
  wrong on roughly a dozen days a year and appears nowhere in the code.
- Alpaca reports each session's open and close as **naive Eastern wall-clock**
  times. `America/New_York` is attached in exactly one function, which converts
  to UTC immediately. Everything after that point - every comparison, bar
  start, wake time and checkpoint - is UTC.
- A **regular-session bar** is a 15-minute boundary whose *whole interval* lies
  inside the session. An ordinary day has twenty-six (09:30 through 15:45); a
  13:00 early close has fourteen (09:30 through 12:45). Pre-market and
  post-market candles, which the IEX feed does serve, are not regular-session
  bars and are filtered out before the strategy sees anything.
- A cycle outside the regular session does **nothing at all**: no fetch, no
  strategy, no checkpoint, no order, and no provider call.
- The bar that closes *at* the bell is therefore never acted on: its cycle
  would fall outside the session. On an ordinary day the actionable bars are
  09:30 through 15:30, acted on at 09:45 through 15:45. That is a named
  consequence of trading regular hours only, not an oversight.
- The **risk day remains a UTC calendar day**, shared with the crypto book.
  There is one account, so there is one answer to "how much has it lost today",
  and a second equity-only baseline would create a second one.

---

## 4. The archived equity milestone, and Equity V0.2

Equity V0.1 - SPY, QQQ, AAPL, MSFT, NVDA on Alpaca's IEX feed, whole shares,
`TimeInForce.DAY`, an equity market clock - was a complete, working system
through Alpaca paper execution. It is preserved at the annotated Git tag:

```
equity-v0.1-phase7
```

**It is not a mode of the current system.** The pivot removed the equity path
rather than wrapping both asset classes in a runtime switch: a dual-market
system nobody runs is a liability, and `if asset_class == "stock"` scattered
through the application would have been a permanent tax on every later change.
Git history and that tag are how it stays recoverable.

**Equity V0.2 is not that milestone restored.** The tag is a read-only
reference. Nothing was reset to it, nothing was cherry-picked from it, and no
part of its architecture was carried over wholesale: the current main is
authoritative, and the equity product is built on the current Decimal
quantities, the current SQLite schema, the current risk engine, the current
execution ordering, and the current reconciliation. What was reused from it is
*semantics* that were already proven right - whole shares, `TimeInForce.DAY`,
regular hours, the IEX feed, and reading the broker's clock rather than
assuming a session.

**The absence rules now scope to the crypto product, and are still enforced.**
No crypto runtime module - the schedule, the bounded fetch, the runner - may
name an equity symbol, `StockHistoricalDataClient`, `StockLatestTradeRequest`,
`StockBarsRequest`, `TimeInForce.DAY`, `get_clock`, or `America/New_York`, and
the test suite asserts each absence against source with docstrings and comments
stripped. The two products share the account, the risk engine, the strategy,
the storage layer, the checkpoint table, the lock mechanism and the
reconciliation pass; they share no scheduler, no market-data client, no order
translation, and no runtime object.

---

## 5. Planned system progression

```
Phase 0  Repository Foundation             <- done (Equity V0.1)
Phase 1  Historical Market Data            <- done, migrated to crypto (C1)
Phase 2  Data Validation                   <- done, migrated to crypto (C2)
Phase 3  Strategy                          <- done, unchanged by the pivot (C3)
Phase 4  Backtesting                       <- done, migrated to crypto (C4)
Phase 5  Risk Engine                       <- done, migrated to crypto (C5)
Phase 6  SQLite Operational State          <- done, schema v5 (C6 + C8 + C9)
Phase 7  Alpaca Paper Trading              <- done, migrated to crypto (C7)
--- Crypto Pivot V0.2 complete ---
Phase 8  Reconciliation / Crash Recovery   <- done (C8)
Phase 9  24/7 Runtime / Monitoring         <- done (C9)
--- Phase 8 + Phase 9 integrated (schema v5) ---
Equity   Equity V0.2                       <- component-complete (E1)
Phase 10 Deployment                        <- after the integrated paper smoke
```

Equity V0.2 is a parallel product rather than a later phase of the crypto one.
It is **component-complete and integration-ready**, and it is deliberately not
activated alongside crypto: shared account-level risk, combined exposure, a
shared API budget, the final reconciliation scope and the V0.2 dashboard belong
to a separate Combined Integration phase (section 9, E1).

---

## 6. Storage policy

- **Historical market data:** Parquet files under `data/`.
  - `data/raw/` - as fetched from the provider, normalized only to the
    canonical schema in section 9.
  - `data/processed/` - validated bars used by backtests. Validation works on
    `data/raw/` in place and writes nothing; populating this directory belongs
    to a later phase.
  - Market data is **never committed**. The directories are tracked via
    `.gitkeep`; their contents are ignored.
- **Operational trading state:** a local SQLite database at schema **v5**. It
  stores strategy runs, signals, risk events, system events, local position
  snapshots, order intents, broker-order snapshots, UTC-day risk baselines,
  reconciliation runs and events, and per-symbol completed-bar checkpoints.
  Fills and executions are **not** stored:
  order-level `filled_quantity` is what reconciliation settles, and a
  fill-level history has not been earned. Database files are ignored by git.
- **Secrets:** environment variables loaded from a local `.env`, which is
  ignored by git. `.env.example` documents the variable names only and
  contains no values.
- Data directories and the SQLite file are local artifacts. The repository
  must remain reproducible from source code plus a data re-fetch.

---

## 7. Safety principles

These are architectural rules for the whole project.

### A. Strategy code must never directly submit broker orders

Strategies emit signals. Nothing else. The intended flow is a one-way pipeline
with no shortcuts:

```
Strategy -> Signal -> Risk Engine -> Order Intent -> Execution Engine -> Broker
```

No module may skip a stage. A strategy module must never import a broker
client.

### B. Paper and live trading must eventually be strongly separated

Not a boolean flag buried in a config file. The separation must be explicit,
externally visible, and impossible to flip by accident.

### C. Live trading is outside the current milestone and must not be enabled accidentally

No code path in this repository may submit a real-money order. Live trading is
not merely unimplemented; it is out of scope. Adding it is a scope change
requiring an edit to this document.

### D. Every trading decision should eventually be auditable

For any order the system produces, it must later be possible to reconstruct:
the input bars, the signal, the risk decision, the resulting intent, what was
sent to the broker, and what the broker reported back.

### E. Duplicate-order prevention and reconciliation are mandatory before any live trading

Every order needs a stable client-side idempotency key. On startup and after
any crash, local state must be reconciled against the broker's authoritative
state before new orders are placed.

Reconciliation **observes and repairs; it never creates an order**. Resolving
an ambiguous submission by sending a second one, or correcting a position
difference by trading, is prohibited: recovery that places a trade is not
recovery. The recovery anchor is always the existing `client_order_id`, and it
is never regenerated.

### F. Backtesting must not use future information

No look-ahead bias. A decision at bar *t* may use only information available
at the close of bar *t*, and fills must occur at *t+1* or later. Indicator
warm-up periods must be respected; incomplete/partial bars must not be traded
on.

### G. Repository state is the source of truth

Committed code and committed documentation define the system. Do not rely on
prior chat context, memory, or undocumented intent.

### H. Broker-critical quantities are exact decimals

Crypto positions are fractional, and a binary float cannot represent one
exactly. Every quantity that reaches a risk decision, an order intent, the
database, or the broker is a `decimal.Decimal`. A `float` quantity is refused
rather than converted: an approximation must never become the size of a real
order. NaN, both infinities, negative values, and zero are rejected explicitly.

### I. The broker owns order precision

Minimum order sizes and trade increments are read from the broker's live asset
metadata on every attempt, never hardcoded. Provider rules change, and a stale
constant would produce orders the broker silently refuses - or worse, accepts
at the wrong size. Normalization to a broker increment rounds **down**, never
up: rounding up would exceed what risk approved.

Asset metadata is necessary and **not sufficient**. Alpaca additionally enforces
a minimum cost basis of **$10** on a USD-quoted crypto order, and does not
report it: `min_order_size` still carries an older ~$1-notional-equivalent floor
(0.000012417 BTC, about $1 at an $78,000 BTC). An order can therefore clear every
published constraint and still be refused with *"cost basis must be >= minimal
amount of order 10. No order was created."* That floor is written down once, as
`USD_MINIMUM_ORDER_NOTIONAL`, and enforced locally before any broker request
exists. See section 8, "C7".

---

## 8. Explicit exclusions

The following are **out of scope** and must not be introduced without an
explicit, documented scope change.

**Trading scope:** live trading, real-money order submission, options, futures,
forex, perpetual futures, non-USD quote currencies, pairs outside BTC/USD and
ETH/USD, equities outside the ten named in section 3.1E, short selling,
leverage, margin strategies, multiple brokers, multiple users.

**Order types:** MARKET only, in both products. Crypto sends GTC; Equity V0.2
sends `DAY` with no extended-hours flag, which is the right lifetime for a
regular-hours-only system. No limit, stop, stop-limit, trailing-stop, bracket,
or OCO orders; no notional orders; no `IOC`; no fractional equity orders; no
extended-hours trading.

**Combined activation:** simultaneous autonomous crypto **and** equity
execution against the shared account. Equity V0.2 provides the seams for it and
does not turn it on. Shared account-level risk, combined exposure limits, a
shared API budget, the final reconciliation scope and dashboard V0.2 belong to
the Combined Integration phase.

**Signal generation:** AI trading agents, LLM-generated trade signals,
machine-learning trading models, sentiment analysis, additional indicators,
parameter or walk-forward optimization.

**Application surface:** web frontend, Next.js, FastAPI, mobile application,
TradingView integration, strategy marketplace.

**Infrastructure:** PostgreSQL, Supabase, Redis, Celery, Kafka, Kubernetes,
cloud deployment, Docker (unless explicitly requested in a future phase). The
C8 runtime is a single local synchronous process holding an `fcntl` file lock;
it introduces no broker, queue, scheduler daemon, or external coordination
service, and no `asyncio`.

**Monitoring:** Telegram, Slack, Discord, email, SMS, paid monitoring agents,
and hosted metrics. C8's whole monitoring surface is a heartbeat object and
standard-library structured logging to stdout.

**Process:** asset-class switches inside a shared code path, speculative
refactors, abstractions built for hypothetical future requirements, complex
abstract base classes. Two products on one account is not a "dual-market
abstraction": they share concrete, already-existing components by calling them,
and each owns its own scheduler and its own broker translation outright.

---

## 9. Milestone boundaries

### C1 - Historical crypto market data (complete)

Fetch 15-minute historical crypto bars for the V0.2 universe from Alpaca's data
API and persist them to Parquet under `data/raw/`. Read-only market data
access; no trading endpoints, no order submission, and no `TradingClient` in
this module.

The contract below is authoritative for every later stage that reads stored
bars.

**Provider and feed.** Alpaca only, via `alpaca-py`, using
`CryptoHistoricalDataClient` and `CryptoBarsRequest` on the **US crypto feed**
(`CryptoFeed.US`). The feed is an argument to the client call, not a request
field. There is no second market-data provider and no IEX equity feed anywhere
in the active path.

**Symbols.** `BTC/USD` and `ETH/USD` only, in canonical pair form. Input is
uppercased and stripped; anything else - an equity ticker, `BTCUSD`,
`BTC-USD`, another quote currency - is rejected.

**Timeframe.** `15m` only. There is deliberately no generic timeframe
framework.

**Date semantics.** `--start` and `--end` are `YYYY-MM-DD` **UTC calendar
dates**. `--end` is **inclusive**. Alpaca's crypto endpoint treats its own
`end` as inclusive and a 24/7 market has a bar stamped at exactly midnight, so
the request boundary is the **last instant of the end date** rather than the
next day's midnight - otherwise a file named `..._2025-12-31` would hold a
2026-01-01 bar. A one-day request returns exactly 96 bars. Malformed dates and
`end < start` are rejected.

**Canonical bar schema.** Every stored row has exactly these columns, in this
order:

| Column | Type | Notes |
| --- | --- | --- |
| `timestamp` | datetime, tz-aware | Always UTC. Bar open time. |
| `symbol` | string | Canonical pair, e.g. `BTC/USD`. |
| `open` | float | |
| `high` | float | |
| `low` | float | |
| `close` | float | |
| `volume` | float | |
| `trade_count` | float, nullable | Null when Alpaca omits it. |
| `vwap` | float, nullable | Null when Alpaca omits it. |

Rows are ordered **ascending by `timestamp`**.

**File naming.** Output is deterministic and date-ranged, using the
filesystem-safe slug:

```
data/raw/{SLUG}_15m_{START}_{END}.parquet
data/raw/{SLUG}_15m_{START}_{END}.metadata.json
```

where `{SLUG}` replaces the pair's slash with an underscore: `BTC/USD` becomes
`BTC_USD`. The slug is for filenames **only**. The canonical symbol keeps its
slash in the DataFrame, in the metadata, in the database, and in every domain
model, and is never rewritten as `BTCUSD`.

Both files are written via a temporary file and an atomic rename, so an
interrupted run cannot leave a truncated Parquet file behind.

**Metadata sidecar.** A small JSON file records `provider`, `feed`, `symbol`,
`symbol_slug`, `timeframe`, `requested_start`, `requested_end`,
`timestamp_timezone`, `retrieved_at_utc`, `row_count`, and
`parquet_filename`. It must never contain credentials or account information.

**Credentials are optional here.** Alpaca serves crypto market data
unauthenticated, so a download must succeed with nothing configured. When both
`ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are present they are passed through,
because an authenticated client gets better provider rate limits; a
half-configured environment is treated as unconfigured rather than sent as a
broken credential pair. Paper order submission still requires credentials.

**Empty responses.** Zero returned bars is an error, not an empty file. No
Parquet or metadata file is written.

**Explicitly out of scope for C1:** data-quality validation, indicators,
strategies, backtesting, risk, SQLite, order models, every trading endpoint,
and any exchange-calendar dependency.

### C2 - Data validation (complete)

Validate a Parquet bar dataset that C1 already wrote, and answer one question:
is it structurally and internally valid enough for later code to consume?

C2 is read-only. It does not download, modify, repair, or rewrite market data,
and it produces no signals, backtests, or broker calls. Validation must not
mutate the DataFrame it is given.

**The architecture is unchanged from the archived equity milestone.** Only the
supported-symbol set moved. That is deliberate: the validator was already
asset-class agnostic, and rewriting it would have risked a working contract for
no gain.

**Validation rules.** The C1 canonical schema is the authoritative input
contract. A dataset is valid when all of the following hold.

| Area | Rule |
| --- | --- |
| Schema | Exactly the canonical columns. Missing and unexpected columns are both reported; none are silently dropped or added. |
| Rows | At least one row. A zero-row dataset is invalid. |
| `timestamp` | Present, non-null, timezone-aware, UTC, strictly ascending with no duplicates. |
| `symbol` | Non-null, uppercase, exactly one distinct value, and in the V0.2 pair universe. |
| OHLC | `open`, `high`, `low`, `close` non-null, finite, and greater than zero, with `high >= low`, `high >= open`, `high >= close`, `low <= open`, and `low <= close`. |
| `volume` | Present, numeric, finite, and `>= 0`. Zero volume is legitimate; it is not an error. |
| `trade_count` | Nullable. When present: numeric, finite, `>= 0`. |
| `vwap` | Nullable. When present: numeric, finite, `> 0`. |

**A slash is not lowercase.** `"BTC/USD".upper()` is itself, so the uppercase
rule passes unchanged for a pair symbol.

**Bar spacing is not checked, and must not be.** Crypto is continuous, but a
provider or data outage can still legitimately leave a gap, and rejecting every
missing 15-minute interval globally would make a transient outage
indistinguishable from corrupt data. Bar freshness - "did we receive the newest
completed bar?" - is a *runtime* question and belongs to the future 24/7
runner, not to structural validation.

**There is no exchange calendar.** No NYSE or Nasdaq session logic, no
weekend or overnight rule. A Saturday bar and a 03:00 UTC bar are ordinary
data.

**Result model.** Validation returns a small dataclass exposing `valid`,
`row_count`, `symbol` (when a single one is determinable), `error_count`, and
the collected `errors`. Each error carries a stable machine-readable code and
a concise human-readable message. The codes are:

```
EMPTY_DATASET        MISSING_COLUMN       UNEXPECTED_COLUMN
INVALID_TIMESTAMP    DUPLICATE_TIMESTAMP  UNSORTED_TIMESTAMP
INVALID_SYMBOL       NULL_OHLC            INVALID_OHLC
INVALID_VOLUME       INVALID_TRADE_COUNT  INVALID_VWAP
```

Codes are part of the contract; messages are not. Ordinary data-quality
problems are collected rather than aborting on the first one, and repeated
violations are summarized with a row count rather than emitted one error per
row. A malformed or unreadable Parquet file is a controlled input error
instead.

**CLI.** `validate <path>` prints a `VALID` or `INVALID` report and exits `0`
for a valid dataset, `1` when validation errors were found, and `2` when the
file cannot be read.

**Explicitly out of scope for C2:** bar-spacing and missing-interval detection,
bar freshness, exchange calendars, session completeness, outlier and
price-spike heuristics, statistical anomaly detection, cross-provider
consistency, data repair, and writing to `data/processed/`.

### C3 - Strategy (complete, unchanged by the pivot)

The EMA 20 / EMA 50 crossover signal generator,
`autotrader.strategies.ema_cross`. It is a pure domain module: canonical bars
in, signals out. There is no CLI command, no broker client, and no import of
one (section 7A).

**The pivot changed nothing here, deliberately.** The strategy reads a `close`
price and a symbol string; nothing in it was ever asset-class specific, so
`BTC/USD` works exactly as `SPY` did. Its tests were migrated to the crypto
pairs to prove that, including that a symbol containing a slash is carried
through verbatim.

**No crypto-specific indicator was added.** No RSI, no MACD, no sentiment, no
AI, no ML, and no parameter optimization. A test asserts the module defines
exactly the functions it did before.

**Indicator.** Two EMAs of the **`close`** column, periods **20** and **50**,
both computed with pandas `ewm(span=period, adjust=False)`. The periods are
fixed for V0.2 and deliberately not configurable.

**Warm-up.** `min_periods=period`, so `ema_20` is undefined for the first 19
bars and `ema_50` for the first 49. A crossover also needs the previous bar's
relation, so the earliest bar that can carry a signal is the 51st.

**Crossover semantics.** Long only, two signal types:

| Signal | Condition |
| --- | --- |
| `BUY` | fast EMA was `<=` slow EMA on the previous bar and is strictly `>` on this bar |
| `EXIT` | fast EMA was `>=` slow EMA on the previous bar and is strictly `<` on this bar |

A crossover yields at most one signal and neither signal repeats while the
relation merely persists. There are no short signals, no stop loss, no take
profit, and no additional filters.

**Signal model.** A frozen dataclass of `timestamp`, `symbol`, `type`
(`BUY`/`EXIT`), and `reason`. `reason` is a stable machine string -
`EMA20_CROSS_ABOVE_EMA50` or `EMA20_CROSS_BELOW_EMA50`.

**Signal timestamp is not an execution timestamp.** The timestamp is the bar
whose close made the crossover knowable. The signal carries no price.

**Input contract.** Exactly one symbol, timestamps ascending, and the
`timestamp`, `symbol`, and `close` columns present. Violations raise
`StrategyInputError`. The supplied DataFrame is never modified or sorted in
place.

### C4 - Backtesting (complete)

The deterministic long-only backtesting engine, `autotrader.backtest.engine`.
It connects the stages that already exist - stored Parquet bars -> C2
validation -> C3 signals -> execution simulation -> portfolio accounting ->
metrics.

C4 is **engineering validation**. It is not a claim that the EMA crossover is
profitable, and its results must never be used to justify enabling any form of
trading or to justify tuning the strategy. The whole simulation is local
arithmetic: no order is created, no broker is contacted, and no network access
occurs.

**Execution timing (section 7F).** A crossover on bar *t* is knowable only once
bar *t* has closed, so the earliest it can be acted on is the open of bar
*t+1*:

```
signal on bar t  ->  fill at bar t+1 open
```

A signal is never filled on its own bar, and `execution_timestamp` is always
strictly greater than `signal_timestamp`. **A signal on the final bar is not
executed.** Crypto is continuous, so "the next bar" is simply the next bar:
there is no market-open logic, no session boundary, and no overnight gap.

**Execution price.** Exactly the next bar's `open`. There is no slippage,
market impact, or partial fill.

**Fractional quantities, in `Decimal`.** Crypto is fractionable, so the equity
milestone's `floor(cash / price)` whole-share rule is **removed**. There is
deliberately no one-whole-coin minimum: $100 buys a fraction of a $100,000
coin. Quantities and cash are `decimal.Decimal`, never binary floats, and a
position is quantized **down** to `1e-18`.

**Provider increments are deliberately not modelled here.** Minimum sizes and
trade increments change, and a historical simulation must stay reproducible.
The broker's live asset metadata is the runtime authority at the execution
boundary instead (section 7I).

**Transaction fees.** A flat taker fee of **`0.0025` (0.25%)** is charged on
**every** executed side, BUY and SELL. This is a deliberately simple,
deliberately conservative V0.2 backtest assumption:

- it models the cost of crossing the spread with a market order;
- Alpaca's real crypto fees depend on 30-day trailing volume tiers and on
  provider rules that change, and **none of that is implemented**;
- it is **not** billing or reconciliation logic.

Zero fees was an acceptable equity-era baseline. For a strategy that
round-trips hundreds of times a year it is not, and a backtest must not report
a return that silently assumes trading is free.

**Sizing reserves the fee.** A BUY solves `q * price * (1 + fee) <= cash`
rather than spending all cash on notional and discovering the fee afterwards.
**Cash can never become negative** - a fee that pushed a balance below zero
would be an accounting bug, not a modelling choice - and that boundary is
explicitly tested. A quantity is quantized down and then stepped back until the
full cost genuinely fits.

**Portfolio.** $100,000.00 initial cash, long only, no leverage, no borrowing,
no short selling, and at most one position in the single symbol being
backtested. `initial_cash` must be positive and finite.

**Exit.** On an `EXIT` while long, the entire position is sold at the next
bar's open. There are no partial exits.

**No-op signals.** `EXIT` while flat and `BUY` while already long are both
no-ops - never a short, a double-buy, a pyramid, or a duplicate holding.

**End of backtest.** An open position is **not** force-liquidated and no
closing trade or closing fee is fabricated. It is marked to market at the final
bar's `close`.

**Equity curve.** One end-of-bar equity value per bar. When a fill occurs at a
bar's open, that fill is processed *before* that same bar's close is marked.

**Max drawdown.** Peak-to-trough over that equity curve, stored as a decimal
fraction and never positive.

**Total return.** `(final_equity / initial_cash) - 1`. No annualization, no
benchmark, no risk-adjusted metric.

**Execution model.** A frozen `Execution` of `signal_timestamp`,
`execution_timestamp`, `symbol`, `side`, `quantity`, `price`, `fee`, and
`cash_after`. `side` is `BUY` or `SELL`: an `EXIT` *signal* produces a `SELL`
*execution*.

**Result model.** A frozen `BacktestResult` exposing `symbol`, `bar_count`,
`initial_cash`, `final_cash`, `final_equity`, `total_return`, `max_drawdown`,
`ending_position_quantity`, `ending_position_market_value`, `total_fees`,
`completed_round_trips`, `signal_count`, `unexecuted_last_bar_signal_count`,
`executions`, and `equity_curve`. Money and quantities are `Decimal`;
`total_return` and `max_drawdown` are floats, because they are presentation
ratios rather than balances. The same input always produces the same result,
digit for digit.

**Explicitly out of scope for C4:** risk limits, order intents, broker
adapters, any trading client, SQLite, reconciliation, monitoring, multiple
strategies or symbols, parameter or walk-forward optimization, Monte Carlo,
Sharpe, Sortino, benchmark comparison, a volume-tiered fee engine, slippage
modelling, and any market-session logic.

### C5 - Risk engine (complete)

The deterministic risk engine, `autotrader.risk.engine`. It occupies the stage
between a signal and an order intent (section 7A) and answers exactly one
question: may this proposed trade be allowed, and if so, what is the largest
safe quantity under the V0.2 limits?

C5 is a **calculator**. It submits no order, constructs no broker client, makes
no network call, opens no database, persists nothing, writes no file, and
mutates neither the request nor the context it is given.

**Not wired into C4.** The backtester keeps its own all-cash sizing baseline.

**V0.2 risk policy.** These are engineering safety defaults. They are **not**
investment advice and not a recommended allocation. The pivot did not loosen
any of them.

| Field | Value | Meaning |
| --- | --- | --- |
| `max_position_fraction` | `0.05` | market value of any one symbol <= **5%** of current equity |
| `max_total_exposure_fraction` | `0.30` | aggregate long exposure <= **30%** of current equity |
| `max_daily_loss_fraction` | `0.02` | new entries halt once the UTC day is down **2%** of its baseline |
| `long_only` | `true` | no short side exists to request |
| `allow_leverage` | `false` | an entry may only spend cash already held |

A policy that flips one of the booleans is **refused** rather than partly
honoured.

**There is no `whole_shares_only` field.** It was removed, not set to `False`:
crypto is fractionable, and a flag whose only legal value is "off" is not a
policy.

**Limits are USD notional.** Each cap bounds the market *value* of a position,
not a count of units, so the arithmetic is `quantity * reference_price`
measured against a dollar ceiling. Only the quantity representation changed in
the pivot.

**Quantities are exact `Decimal`s** (section 7H). `Decimal` and `int` are
accepted; a `float`, a numeric string, `bool`, NaN, infinity, a negative value,
and zero are all refused.

**Risk context.** A flat, immutable snapshot: `equity`, `cash`,
`total_exposure`, `symbol_exposure`, `current_position_quantity` (a `Decimal`),
`daily_pnl`, `start_of_day_equity`, and `trading_enabled`. Monetary fields are
USD floats supplied by the caller; the sizing arithmetic converts them to exact
decimals internally so a cap means what it reads as.

**Entry gates.** A BUY is approved only when all of these hold, checked in this
order so the reported reason is deterministic:

1. the request is well formed;
2. `trading_enabled` is `true`;
3. `daily_pnl / start_of_day_equity > -0.02`;
4. the resulting symbol exposure is within 5% of equity;
5. the resulting total exposure is within 30% of equity;
6. the required cash is within available cash - the no-leverage rule.

**Entry sizing.** The tightest of the three ceilings wins, and the quantity is
rounded **down**:

```
position_remaining  = max(0, equity * 0.05 - symbol_exposure)
portfolio_remaining = max(0, equity * 0.30 - total_exposure)
max_notional        = min(position_remaining, portfolio_remaining, cash)
max_quantity        = max_notional / reference_price   (quantized down to 1e-18)
approved_quantity   = min(requested_quantity, max_quantity)
```

**Clamping, not refusing.** A BUY larger than the safe maximum is approved at
`max_quantity` rather than rejected, and `reason_code` names the constraint
that bound it. Only genuinely **zero** headroom rejects: headroom smaller than
one whole unit used to be a rejection and is now an approved fractional order.

**Daily-loss halt.** `daily_pnl / start_of_day_equity <= -0.02` blocks new
entries. The boundary is inclusive - **exactly -2.00% blocks** - and is
evaluated in exact decimal arithmetic so that boundary is not a floating-point
accident. Exits are unaffected.

**The risk day is a UTC calendar day** (section 3.4). `start_of_day_equity` is
the **UTC-day baseline**, not a broker's equity-session `last_equity`;
`last_equity` is not read anywhere in the active system. Resolving the baseline
durably is the caller's job - see C6 and C7.

**Exits reduce risk, so risk never blocks them.** The `trading_enabled` kill
switch, the daily-loss halt, the per-symbol cap, and the total-exposure cap are
**entry** gates, and none is consulted for an exit. A control that prevented an
account from reducing exposure would trap an open position. A SELL is rejected
only when there is no position, and a quantity larger than the position is
clamped to it.

**Invalid input is not a risk denial.** A *context* that cannot describe a real
account raises `RiskInputError`; a malformed *request* returns an ordinary
rejected decision with `INVALID_REQUEST`. An ordinary risk denial never raises.

**Decision model.** A frozen `RiskDecision` of `approved`, `approved_quantity`
(a `Decimal`), `reason_code`, `message`, and `max_allowed_quantity` (a
`Decimal`). The codes are:

```
APPROVED             INVALID_REQUEST      TRADING_DISABLED
DAILY_LOSS_LIMIT     POSITION_LIMIT       TOTAL_EXPOSURE_LIMIT
INSUFFICIENT_CASH    NO_POSITION_TO_EXIT  EXIT_QUANTITY_EXCEEDS_POSITION
```

**Public API.** `evaluate_risk(request, context, policy=DEFAULT_POLICY) ->
RiskDecision`. There is no CLI command.

**Explicitly out of scope for C5:** order creation, broker adapters, any
trading client, persistence of any kind, changes to C4 accounting, stop losses,
take profits, trailing stops, volatility or drawdown sizing, correlation
limits, intraday rate limiting, reconciliation, monitoring, and deployment.

### C6 - SQLite operational state, schema v5 (complete)

The local operational-state store, `autotrader.state.sqlite`. It is
**persistence infrastructure and nothing else**: it opens a database, creates a
fixed schema, and stores durable records. It decides nothing, orchestrates
nothing, and contacts nobody. It imports only the standard library, requires no
credentials, and opens no socket.

**Technology.** The standard library's `sqlite3`, and nothing else. No ORM, no
migration framework, no async driver, no database service.

**Database file.** The conventional local path is `data/autotrader.db`.
Nothing creates it implicitly. Database files are git-ignored.

**Connections.** Every connection - not just the first - sets
`foreign_keys = ON`, `journal_mode = WAL`, and `busy_timeout = 5000`.

**Transactions.** Every write goes through `transaction()`, which commits on
success and rolls back on **any** exception. Nested use joins the outer
transaction.

**Timestamps.** Every persisted timestamp is ISO-8601 UTC in one canonical
fixed-width form. Aware inputs in another zone are converted; **naive datetimes
are rejected**.

**SQL safety.** Every statement is a literal; every value is bound as a
parameter. No SQL is ever built by string interpolation, and a test asserts
that at the source level.

**Tables.** Exactly twelve.

| Table | Purpose | Invariants |
| --- | --- | --- |
| `schema_metadata` | Schema version marker | Exactly one row |
| `strategy_runs` | One logical strategy session | `mode` is `BACKTEST` or `PAPER`; a run ends once |
| `signals` | Durable strategy signals | `signal_type` is `BUY` or `EXIT`; unique per run/timestamp/symbol/type/reason |
| `risk_events` | Risk-decision audit trail | `decision` and `reason_code` opaque non-empty text |
| `system_events` | General operational events | `event_type` non-empty |
| `positions` | Latest **local** position snapshot | `symbol` PRIMARY KEY; `quantity >= 0`; `average_price` NULL or `> 0` |
| `order_intents` | An order this system decided to place, written **before** the broker call | `client_order_id` UNIQUE; `requested_quantity > 0`; `0 < approved_quantity <= requested_quantity`; `reference_price > 0` |
| `broker_orders` | Latest normalized broker snapshot | one per intent; `quantity > 0`; `filled_quantity >= 0`; `status` opaque text |
| `daily_risk_baselines` | The UTC-day equity baseline | `risk_date_utc` PRIMARY KEY; `baseline_equity > 0`; `captured_at` |
| `reconciliation_runs` | What one finished reconciliation pass concluded (v4) | `status` in `CLEAN`/`REPAIRED`/`UNRESOLVED`/`FAILED`; `safe_to_trade` in `(0, 1)`; counts `>= 0`; `completed_at >= started_at` |
| `reconciliation_events` | Which order or position a pass repaired, observed, or could not settle (v4) | one run reference; `category` in `ORDER`/`POSITION`/`RUN`; `outcome` in the four statuses plus `OBSERVED`; `detail` non-empty |
| `runtime_checkpoints` | The newest completed bar a runtime durably claimed, per symbol (v5) | `symbol` PRIMARY KEY; `last_processed_bar_timestamp` non-empty; monotonic - an older claim updates nothing |

**Exact decimal quantities.** Every broker-critical quantity column -
`positions.quantity`, `order_intents.requested_quantity`,
`order_intents.approved_quantity`, `broker_orders.quantity`, and
`broker_orders.filled_quantity` - is stored as **canonical decimal `TEXT`** and
read back as a `decimal.Decimal`. The text is plain fixed-point (`0.0001`,
never `1E-4`) and preserves the scale it was written with, because a quantity's
precision is information about how it was derived. A `float` is refused rather
than converted. The storage string is an implementation detail: no read model
exposes it.

**Price columns stayed `REAL`, deliberately.** The audit covered every money
and quantity column. `INTEGER` quantities had to migrate because a whole number
cannot represent a fractional coin at all. A `REAL` price already represents a
fractional USD mark, and moving prices to text would discard the
`CHECK (... > 0)` constraints that make an impossible price unstorable even by
a writer that bypassed this module. A price here is a mark, never a quantity.

The `TEXT` quantity columns keep a coarse `CAST(... AS REAL)` CHECK, so a
negative or zero quantity is still unstorable by a writer bypassing Python.
Python holds the exact `Decimal`; the cast is a guard, not the value.

**Daily risk baselines.** The first account equity observed on a UTC calendar
date establishes that date's baseline; every later observation on the same date
returns the stored row untouched. **First observation wins, permanently** - a
baseline that drifted during the day would silently reset the daily-loss halt,
which is the one thing it exists to prevent. Exactly one row per UTC date,
enforced by the primary key as well as in Python.

**Honest limitation:** this records the first equity the system *observed* on
that date, not the equity at exactly 00:00 UTC. Nothing in this milestone runs
continuously, so a day whose first observation is at 14:00 UTC is measured from
14:00 UTC. The stored `captured_at` records how close it was. The C9 runner,
running continuously, makes the first observation land near the boundary.

This is a **persistence primitive only**. Nothing here schedules anything,
watches a clock, or decides when an observation should happen.

**Schema migration (v1 -> v2 -> v3 -> v4 -> v5).** A new database is created
directly at v5. An older one is upgraded through an explicit ordered path, in a single
transaction; SQLite's DDL is transactional, so a failure anywhere rolls back to
the original version rather than leaving a half-applied state.

v1 -> v2 is additive. **v2 -> v3 is a rebuild**: SQLite cannot retype a column,
so `positions`, `order_intents`, and `broker_orders` are renamed aside,
recreated under their real names from the *same* literals a fresh database
uses, copied across row by row with quantities converted, and the old copies
dropped. Creating the new table under its real name - rather than renaming a
temporary one into place - is what keeps a migrated database's stored schema
byte-identical to a fresh one, and a test asserts exactly that.

Referential enforcement and modern rename semantics are suspended for the
rebuild and restored afterwards whatever happens; foreign keys are re-checked
before the transaction commits, and a violation rolls the whole upgrade back.

**Existing data survives.** An integer `1` becomes the decimal `"1"` and `100`
becomes `"100"` - the same number, written out, with no scale invented and no
row dropped. Ids, keys, prices, statuses, and timestamps all come through
unchanged. A stored quantity that is not an exact integer means the column
holds something this module never wrote, and the migration fails closed rather
than guessing.

**v3 -> v4 uses the same rebuild machinery for a smaller reason.** Only
`order_intents` is rebuilt, and only because SQLite cannot widen a CHECK
constraint in place: the intent vocabulary gains `CONFIRMED_NOT_SUBMITTED`
(section 9, C8). Every column is copied verbatim - nothing is converted, and no
existing row can hold the new status, so no row changes meaning. The two
reconciliation tables are created empty; backfilling an audit trail of passes
that never ran would be a fabrication. The byte-identity assertion covers this
path too, from v1, v2, and v3 alike.

**v4 -> v5 is purely additive.** One new table, `runtime_checkpoints`, and
nothing else: no existing table is rebuilt, renamed, retyped, or reindexed, and
a test asserts every pre-existing schema object comes through byte-identical.
Phase 8's `reconciliation_runs` and `reconciliation_events` rows, every order
intent, every fractional quantity, and every daily risk baseline survive
because nothing in the step reads or writes them. The table starts **empty**,
which is the correct starting state rather than an omission: a checkpoint
claims that some process already acted on a bar, and backfilling one from
`signals` would invent a claim this schema never recorded as claimed.

A database written by a **newer** version is refused and left untouched; one
older than v1 has no path and is refused too.

**No CLI.** There is no `db-init`, `db-shell`, or migration command.

**Explicitly out of scope for C6:** fills, executions, broker accounts, trading
loops or schedulers, a migration framework, connection pooling, an ORM, a
database CLI, backup and restore tooling, and database repair. Reconciliation
*records* live here as of v4 and runtime bar *claims* as of v5, but nothing in
this module performs a reconciliation or decides that a bar may be acted on: it
stores the conclusions `autotrader.reconciliation` and `autotrader.runtime`
reached and interprets none of them.

### C7 - Alpaca paper crypto execution (complete)

The only stage permitted to submit a broker order, and permitted to submit it
**only** to Alpaca paper trading. `autotrader.execution` is the single boundary
that speaks to a broker: it is the only place in the repository that constructs
a trading client or calls `submit_order`.

**Live trading remains impossible, structurally.**

- `create_paper_trading_client()` builds `TradingClient(api_key, secret_key,
  paper=True)` with `paper=True` written literally. It takes **no parameters**.
- No public function in the package accepts a `paper` or `live` argument.
- There is no `--live` flag, no `--paper` option, and no environment variable
  that selects an environment.
- Tests assert that `paper=False` appears nowhere in the executable source,
  that the client factory's signature is empty, and that no live CLI option
  exists.

**Two independent submission gates**, both closed by default, neither able to
satisfy the other:

1. **Environment:** `AUTOTRADER_PAPER_TRADING_ENABLED` must equal exactly
   `true` after stripping surrounding whitespace.
2. **Explicit confirmation:** `--confirm-paper PAPER`, compared exactly.

`--dry-run` requires neither, because it cannot submit.

**Supported scope.** Crypto spot; BTC/USD and ETH/USD; BUY and SELL; positive
fractional `Decimal` quantities; MARKET orders; **GTC** time in force. No
notional orders, no limit/stop/stop-limit/trailing-stop orders, no bracket or
OCO, no options, no equities, and no shorts.

**Time in force is GTC, never DAY and never IOC.** `DAY` expires at a session
close that a 24/7 market does not have, and `IOC` would silently cancel the
unfilled part of an order this system believes it placed.

**Market data.** The reference price is the current latest **crypto** trade,
via `CryptoHistoricalDataClient` and `CryptoLatestTradeRequest` on
`CryptoFeed.US`. `StockLatestTradeRequest`, `StockHistoricalDataClient`, and
the IEX feed appear nowhere in the active execution path. A stored Parquet bar
is never used to size a live order, and no CLI-supplied price can bypass the
risk engine. A price that cannot be obtained, or that is not finite and
positive, fails closed.

**No market clock.** `get_clock()` is an equity-market concept and is not
called. The CLI prints no `Market: OPEN/CLOSED` line. Deciding *when* to act -
on completed 15-minute bars - is Phase 9's job.

**Crypto asset metadata is the runtime authority** (section 7I). Before any
submission the asset is read from the broker and must be crypto (not an equity
and not a perpetual future), active, tradable, and fractionable, and must report
both a `min_order_size` and a `min_trade_increment`. Anything missing or
contradictory fails closed. No BTC or ETH constant is hardcoded anywhere.

**The pipeline**, in a mandatory order:

```
paper account + positions + asset metadata + current crypto reference price
        -> UTC-day equity baseline   (daily_risk_baselines)
        -> RiskContext
        -> evaluate_risk
        -> RiskDecision              persisted to risk_events
        -> normalize DOWN to the broker's trade increment
        -> refuse below the broker's effective minimum quantity
        -> OrderIntent               persisted AND COMMITTED
        -> broker duplicate preflight
        -> Alpaca PAPER MARKET order, GTC
        -> broker snapshot           persisted to broker_orders
```

**The risk engine is never bypassed.** Every submission is sized by C5, and the
quantity sent to the broker is never larger than
`RiskDecision.approved_quantity`. A rejected decision means no broker request
is constructed at all.

**Quantity normalization rounds down, never up.** If the result lands below the
broker's `min_order_size`, there is **no order** and that is reported clearly -
rounding up to clear the minimum would put sizing outside the risk engine's
control. The SDK's request field is typed as a float, so the exact `Decimal`
becomes one at the last step and only after checking that the value the broker
will actually receive is not larger than the approved quantity.

**The USD minimum order notional is enforced locally** (section 7I). For a
USD-quoted pair the effective minimum quantity is

```
effective_min_qty = max(
    asset.min_order_size,                                  # live broker metadata
    ceil_to_increment(USD_MINIMUM_ORDER_NOTIONAL / price)  # the $10 cost basis
)
```

computed entirely in `Decimal`; a binary float would make a threshold that
decides whether a real order is sent depend on a rounding artefact. The
*threshold* rounds **up** to the next whole `min_trade_increment` - rounding it
down would produce a floor itself worth less than $10 - while the *submitted*
quantity still rounds **down** and may never exceed `RiskDecision.approved_quantity`.
Threshold arithmetic and submission normalization are different operations on
different values.

An order below the effective minimum is a **definite local refusal**
(`MinimumNotionalError`, a `QuantityBelowMinimumError`): `submit_order` is
called zero times, no intent is persisted, and the outcome is known rather than
`UNKNOWN`. The quantity is **never** raised to clear the floor; the caller must
request more. Without a trustworthy price no threshold can be computed and
nothing is submitted.

The floor applies to **both sides**. Alpaca states the USD-pair minimum without
a side distinction and describes the cost-basis check as covering buy and sell
orders alike, so a SELL below $10 is refused by the endpoint exactly as a BUY
is. Enforcing it locally on both sides sends no request rather than making one
that cannot succeed. The consequence is Alpaca's rather than this system's: a
position worth less than $10 cannot be closed until it recovers, which is why an
opening order is sized with room above the floor rather than at it. The rule is
scoped to USD-quoted pairs; Alpaca documents a separate `0.000000002` floor for
its BTC, ETH, and USDT pairs, which the asset metadata already carries.

**Risk context mapping.** Built from current paper broker state:

| `RiskContext` field | Source |
| --- | --- |
| `equity` | `TradeAccount.equity` |
| `cash` | `TradeAccount.cash` |
| `start_of_day_equity` | the stored **UTC-day baseline** |
| `daily_pnl` | `equity - baseline` |
| `total_exposure` | sum of positive **long** position market values |
| `symbol_exposure` | that pair's long market value, else 0 |
| `current_position_quantity` | that pair's long quantity as a `Decimal`, else 0 |
| `trading_enabled` | caller-supplied kill switch (a parameter, not an env var) |

`TradeAccount.last_equity` is **not** used and is not read: it is an
equity-session previous close, and a market that never closes does not have
one.

Alpaca reports a crypto market as `BTC/USD` in some responses and `BTCUSD` in
others. Positions are keyed by a provider-agnostic form so a position is
matched either way; the canonical spelling is what the domain models, the
stored data, and the database use.

**SELL follows the same path.** A risk-reducing exit is still permitted when
`trading_enabled` is false or the daily-loss halt is active, is clamped to the
actual broker position so it can flatten but never open a short, and is then
normalized down to the broker increment like any other order.

**Account safety.** The account must be tradable - an accepted status and none
of `trading_blocked`, `account_blocked`, or `trade_suspended_by_user` - checked
before any submission, for BUY *and* SELL. A short position in the account is
refused outright.

**`client_order_id`.** `autotrader-<uuid4>`, generated **once** when the intent
is created, committed before the broker call, and never regenerated. It carries
no secret and no account information.

**Why the intent is persisted first.** A crash between the request and its
response must still leave a durable anchor. Submitting first and recording
afterwards would produce a real broker order with no local trace.

**Duplicate preflight.** Before submitting, the broker is asked for an order
under this `client_order_id`. If one exists, it is recorded and returned and
**nothing is submitted**. A clear `404` proceeds. Any other failure **fails
closed** - "could not check" is never "there is no duplicate".

**Submission outcomes.** `submit_order` is called at most once, ever:

| Outcome | Local state |
| --- | --- |
| Broker returned an order | `SUBMITTED`, snapshot persisted |
| Broker definitively refused (a 4xx other than 408/429) | `REJECTED`, no order exists |
| Timeout, reset, 5xx, 408, 429, or unreadable status | `UNKNOWN` |

**An ambiguous outcome is never retried.** There is no automatic resubmission,
no exponential backoff, and no new `client_order_id`. The SDK's own internal
retry of `429`/`504` responses is disabled on the trading client for the same
reason.

**Accepted is not filled.** A stored broker snapshot proves acceptance only.
The local `positions` table is written **only** from a position actually
observed at the broker.

**CLI.** One command, `paper-submit`, with `--symbol`, `--side`, `--qty`,
`--confirm-paper`, `--dry-run`, and `--db`. `--qty` is parsed as text straight
into a `Decimal`, so a fractional quantity never passes through a binary float.
There is no `trade` command, no `live-submit` command, and no asset-class
selector. Exit codes: `0` submitted, already existing, or dry run; `1` a
controlled refusal; **`2` an `UNKNOWN` outcome**.

**Testing.** The automated suite is entirely offline: the Alpaca boundary is
mocked, the fakes return real alpaca-py models so normalization is exercised
against real response shapes, no real credential is read, and sockets are
asserted shut. Source-level tests scan *executable* code with docstrings and
comments stripped.

**Explicitly out of scope for C7:** live trading in any form; reconciliation
and crash recovery, which are C8's; retry or backoff on submission; streaming
or websockets; order replacement or cancellation as part of the execution path;
multi-broker abstraction; notional, limit, stop, bracket, or OCO orders; a
scheduler or 24/7 loop; a monitoring surface; a frontend; and deployment.

### C8 - Reconciliation and crash recovery (complete)

`autotrader.reconciliation` makes local SQLite state reflect **verified broker
truth** after a process crash, a restart, a submission whose outcome was never
knowable, a lost broker response, a stale local snapshot, a fill that landed
after the local process died, or a cancellation that arrived after the snapshot
was written.

**Authority hierarchy.** The broker is truth for orders, fills, and positions.
Local SQLite is durable intent, an audit trail, and a last-known snapshot.
Where they disagree, the snapshot is rewritten from the broker - never the
reverse.

**Reconciliation never creates an order.** It may read broker state, look up
orders that already exist, update local snapshots, repair local status, update
observed positions, and classify what it cannot settle. It may **not**
automatically resubmit an `UNKNOWN` intent, mint a new `client_order_id` for an
existing intent, retry a submission, create a replacement order, or trade to
correct a mismatch. The package imports nothing that could place an order, and
source-level tests assert each forbidden identifier is absent from its
executable code with docstrings and comments stripped.

**Public API.** One function and one result:

```python
result = reconcile_paper_state(connection, dry_run=False)
result.safe_to_trade
```

`ReconciliationResult` carries `status`, `safe_to_trade`, `issues`,
`orders_checked`, `positions_checked`, `unresolved_count`, `repaired_count`,
`started_at`, `completed_at`, and the `reconciliation_run_id` it recorded.
There is no multi-broker framework: there is one broker, Alpaca paper.

**Status vocabulary and the startup contract.**

| Status | Meaning | `safe_to_trade` |
| --- | --- | --- |
| `CLEAN` | Local and broker state already agreed | true |
| `REPAIRED` | Differences were resolved from verified broker truth | true |
| `UNRESOLVED` | The pass ran; ambiguity remains | false |
| `FAILED` | The pass could not complete | false |

`safe_to_trade` is **derived** from `status`, not stored alongside it, so a
result claiming `UNRESOLVED` and "safe" cannot be constructed. A pass that
never finished returns no result at all, which is not permission.

**Paper environment verification, first.** Before any local write, the trading
client must be *provably* pointed at Alpaca paper: its base URL must be the
paper host and the SDK's sandbox flag must be set. An attribute that is
missing, of an unexpected type, or pointing elsewhere **fails closed**. There
is no benefit of the doubt, and no live path.

**`UNKNOWN` recovery.** The anchor is the persisted `client_order_id`,
generated once and never regenerated.

| The broker's answer | Outcome |
| --- | --- |
| It has that order | The snapshot is normalized and persisted; the intent becomes `SUBMITTED`. **Nothing is submitted.** |
| It definitively has no such order | A **bounded** confirmation follows: the read is repeated a small fixed number of times with a short pause, and every read must agree. If they do, the intent becomes `CONFIRMED_NOT_SUBMITTED` - terminal. |
| Ambiguous, timed out, 5xx, or unreadable | The intent is left exactly as it was; the pass is `UNRESOLVED`. |

A single not-found is never sufficient: a lookup that overtook a submission
still in flight would answer "no" about an order that exists. The confirmation
is bounded - a fixed count, a fixed pause, no growing backoff and no loop that
waits for the answer it wants.

`CONFIRMED_NOT_SUBMITTED` means the stale historical intent will not be
submitted later. The strategy and a future runtime react to new bars normally;
the pre-crash signal is not re-sent.

**`CREATED` and `SUBMITTING` crash residue.** A `CREATED` intent with no broker
evidence is **never** automatically submitted. Reconciliation establishes
whether broker evidence exists and, if the broker definitively has none, marks
it `CONFIRMED_NOT_SUBMITTED`. A `SUBMITTING` intent - a process that died
mid-call - is treated as ambiguous and handled identically to `UNKNOWN`.

**Order snapshot repair.** For an intent whose local state says `SUBMITTED`,
`accepted`, `new`, `pending`, or `partially_filled`, current broker truth is
read and the local snapshot updated: `accepted` -> `filled`, `accepted` ->
`canceled`, `accepted` -> `rejected`, and so on. Only observed broker truth is
persisted; no transition is fabricated. The reconciled fields are the broker
order id, client order id, symbol, side, quantity, filled quantity, filled
average price when known, status, submitted time, and filled time. The broker's
own `updated_at` is carried into the audit detail rather than into
`broker_orders.updated_at`, which already means "when this snapshot was
refreshed" - one column, one clock.

An order returned under this key that names a different `client_order_id`,
market, or side is not evidence about this intent: it is reported and nothing
is written. A broker order already recorded locally which the broker now denies
leaves the stored snapshot untouched and blocks trading; recorded evidence is
not deleted because a later read disagreed with it.

**Partial fills stay partial.** 0.0004 filled of 0.001 ordered is stored as
exactly that and never becomes a full fill. A `partially_filled` order is
re-read on the next pass, because it can still fill. The remainder is never
automatically canceled or replaced. A missing fill price is not invented, and
no fill is derived from a submitted quantity.

**Positions come only from the broker.** BTC/USD and ETH/USD are reconciled
independently. A broker position that local state lacks is written in; a local
position the broker no longer holds goes to zero. Quantities are exact
`Decimal` values, normalized from the broker's strings and stored through the
existing decimal serialization - no float ever holds an authoritative position
quantity. A current position is **never** derived from `order_intents`,
`SUBMITTED` is never read as "a position exists", and no short local position
can be written. A broker position outside the traded universe is recorded as an
observation, not reconciled, and never traded out of. Mismatches produce audit
evidence; no offsetting order is placed.

**Broker read failure fails closed.** A client that cannot be proven to be
paper, an authentication failure, an unreadable account, a position-list
failure, a short position at the broker, or an inability to write the audit
record each make the whole pass `FAILED`. An order lookup that times out or a
malformed broker order makes that item `UNRESOLVED` while the rest of the pass
completes - a runtime is better served by every problem than by the first one.
No pass reports green on truth it could not read.

**Audit.** Every finished pass writes one `reconciliation_runs` row and a
`reconciliation_events` row per repaired, observed, or unresolved item, in a
single transaction, plus a summary in `system_events`. Clean items write no
event: an audit table where most rows say "no change" stops being readable.
Repairs commit as they are made and the audit record commits at the end, so a
crash mid-pass leaves durable repairs and no run row - work was done, nothing
was concluded - and the next pass reconciles again. No secret is ever written.

**Idempotency.** Running reconciliation twice costs a second look and changes
nothing; a second pass after a repair reports `CLEAN`. An intent in a terminal
state, and a broker order in a terminal broker status, are not re-queried.

**Precondition.** This is a startup and after-the-fact operation. It must not
run while a submission is in flight in another process, which nothing in this
repository does.

**CLI.** `autotrader reconcile`, with `--db` and `--dry-run`. It may modify
local SQLite; it can never submit an order, which is why it needs neither the
environment gate nor a confirmation token. `--dry-run` reports identical
findings and reconciles nothing into the database; opening the database still
applies any pending schema migration, as every command does, which is a
structural upgrade rather than a reconciliation result. Exit codes: `0` for
`CLEAN` or `REPAIRED`, `1` for `FAILED`, `2` for `UNRESOLVED`. Operational
failures print a message, never a traceback.

**CLI, and the runtime.** `autotrader reconcile` remains available for
diagnostics and manual repair. It is **not** a prerequisite an operator has to
remember before every daemon start: `crypto-run` invokes this same pass itself
at startup (C9, below). The two callers share one implementation; there is no
second copy of reconciliation anywhere.

**Explicitly out of scope for C8:** order replacement or cancellation, fill- or
execution-level history, automatic retry or resubmission of anything, a
multi-broker abstraction, live trading, the continuous loop, the scheduler,
completed-bar polling, heartbeats, monitoring, and deployment.

### C9 - 24/7 crypto runtime and monitoring (complete)

The long-running process that operates BTC/USD and ETH/USD unattended.
`autotrader.runtime` joins the existing stages and adds **no trading logic of
its own**: C1 supplies the bars, C2 validates them, C3 produces the signal, C5
sizes it, C6 records it, and C7 stays the only thing that speaks to a broker.

**Scheduling is UTC wall-clock, every day.** The runtime wakes at `00`, `15`,
`30` and `45` minutes past every hour, recomputed from the current UTC time on
every cycle rather than by repeatedly sleeping 900 seconds - a fixed sleep
accumulates every scheduler delay and drifts off the boundary within a day.
There is no `get_clock`, no exchange calendar, no market open or close, no
weekday filter, and no `America/New_York`. Saturday and Sunday are ordinary
days, and source-level tests assert each of those absences.

**Completed bars only.** Alpaca's crypto 15-minute bars are stamped at
**interval start** - measured against the live endpoint, not assumed - and the
endpoint serves the interval that is still running: at 00:16:17 UTC it already
returns a bar stamped 00:15:00, whose close has not happened. So a bar is
processed only when

```
bar_timestamp + 15 minutes <= now - safety_delay
```

An in-progress candle is never evaluated and never traded.

**A small explicit safety delay.** 5 seconds by default, configurable with
`--safety-delay`, and required to be shorter than one bar. It covers provider
publication lag: an interval ending at exactly 10:15:00 does not mean the
provider has published it at 10:15:00.000. It is subtracted from `now`
everywhere completeness is judged, so an early wake-up cannot smuggle an
unpublished bar through.

**Bounded fetching.** Each cycle requests one window of 200 completed bars per
symbol, bounded to 100-200. EMA 50 needs 50 observations plus the previous bar,
and after 200 bars a span-50 EMA retains about 0.04% of its seed. The window
ends at the last instant of the newest completed interval, so the in-progress
candle is not even asked for. Two provider calls every fifteen minutes for the
whole system: no polling, no constant account or position reads, and no
re-download of history. A per-cycle provider-call counter feeds the later
shared crypto+equity API-budget work.

**Deterministic, sequential processing.** BTC/USD is processed to completion -
risk sized against the account as it stands, order submitted or refused -
before ETH/USD is looked at. Nothing runs concurrently and there is no
`asyncio`: two symbols and one cycle every fifteen minutes have no concurrency
in them, and sequencing keeps two same-boundary signals from sizing against the
same stale cash and exposure figures.

**Only the newest completed bar may act.** The lookback exists to give the
recursive EMA its state. Historical signals inside the window are **not**
replayed: every crossover older than the newest bar has already happened, and
re-emitting them would turn a restart into a burst of stale orders.

**Durable duplicate protection.** A per-symbol `last_processed_bar_timestamp`
checkpoint is claimed **before** the strategy runs, so one completed bar is one
decision per symbol even when the provider repeats the newest bar, a cycle
overruns its boundary, or the process is restarted. The production
implementation is `SqliteCheckpoint` on `runtime_checkpoints` (schema v5) and
is the constructor default; `InMemoryCheckpoint` remains for tests. See
**C8+C9 integration**, below, for the safety model this encodes.

**Startup fail-closed.** Broker submission is off until the startup-safety
check reports `SAFE`, and in production that check is Phase 8's reconciliation
pass. A runtime constructed without one keeps `unresolved_startup_safety()`,
which reports `UNRESOLVED` and also keeps submission off. The seam is one
zero-argument callable returning a `StartupSafetyResult`: no plugin framework,
no registry, no discovery. While unsafe the runtime still fetches, validates,
evaluates, records signals, and logs; it simply does not trade.

**Unattended paper execution needs three things, all closed by default:**

1. `AUTOTRADER_PAPER_TRADING_ENABLED=true` in the environment - C7's gate,
   unchanged and not bypassed;
2. `--confirm-paper-runtime PAPER`, authorizing **this process** for its
   lifetime. A daemon cannot have a token typed every fifteen minutes, so the
   confirmation moved to process start rather than being removed or weakened;
3. a startup reconciliation reporting `safe_to_trade`.

`--observe-only` goes further than refusing: it constructs no execution path at
all, so submission is unexpressible rather than merely disabled. There is no
live mode, no `--live`, no `paper=False`, no `stock-run`, and no `live-run`.

**Cycle failure policy** - three outcomes, no exception framework:

| Failure | Severity |
| --- | --- |
| Provider fetch error, invalid bars, strategy input violation | `RETRY_NEXT_CYCLE` |
| Risk rejection, including EXIT while flat | not a failure: an ordinary no-order result |
| Ambiguous `UNKNOWN` submission outcome | `TRADING_PAUSED` |
| Rejected credentials, untradable account, broken local state, anything unexpected | `FATAL` |

Invalid bars fail that cycle closed: nothing is sorted, repaired, or traded.

**`UNKNOWN` pauses trading for the life of the process.** The order may or may
not exist at the broker, so no later signal is submitted on top of a position
nobody can describe. Observation continues, `run_forever` stops scheduling, and
the CLI exits `2`. Nothing resolves, retries, or reasons about the ambiguity
*inside that cycle*: recovery is a **new process**, which runs startup
reconciliation before it may trade again. Startup and mid-run are two distinct
reconciliation moments, and only the first one is allowed to open the gate.

**Heartbeat.** A structured status object exposing runtime start, last cycle
start, last successful cycle, last processed bar per symbol, whether paper
execution is enabled and the reason when it is not, the startup-safety code,
cycle and order counts, provider-call counters, and the last error.

**Logging.** Standard-library `logging` under `autotrader.runtime`, emitted as
parseable `event=... key=value` lines to stdout - suitable for systemd and
journald, with no repository log file required. No monitoring dependency, no
Telegram, no Slack, no webhook, no agent. Credentials are read only inside C7,
are never returned from it, and are never arguments to anything in the runtime,
so there is no line for them to leak through; a test asserts it.

**Single-instance lock.** An `fcntl` exclusive non-blocking lock on
`<database>.runtime.lock`, released in a `finally`. Two runners on one database
would each hold their own in-process checkpoint, neither able to see the
other's, and both would act on the same completed bar. A PID file is not
sufficient: it records an intention, survives a crash, and names a pid that may
have been reused. A second runner exits non-zero before it fetches a bar or
reaches a broker.

**Graceful shutdown.** `SIGINT` and `SIGTERM` set a flag and the loop stops at
its next safe point. The handler does not raise, cancel, or touch the database -
a signal can arrive mid-broker-call, and the only correct response there is to
let that call finish. No new cycle is scheduled, no new submission is started,
the strategy run is closed, database resources are released, and the lock is
released.

**Strategy run lifecycle.** One `strategy_runs` row per runtime session, opened
at startup in mode `PAPER` and closed at shutdown - `COMPLETED` on a clean
stop, `FAILED` when the runtime ended paused or fatal. The newest completed
bar's signal is recorded through C6's existing `record_signal`; a repeat raises
`DuplicateSignalError`, which is respected rather than worked around. No table
was redesigned; the integration adds one (`runtime_checkpoints`, v5).

**Sizing stays C5's.** The runtime holds no sizing policy. A signal requests a
quantity larger than any position this account can hold or any ceiling this
policy can approve, and the risk engine's clamp - never the runtime's - is the
size that reaches the broker.

**CLI.** `crypto-run`, with `--once`, `--confirm-paper-runtime`,
`--observe-only`, `--safety-delay`, and `--db`. Exit codes: `0` a clean stop
including `SIGINT`/`SIGTERM`; `1` a controlled refusal or a fatal cycle
failure; **`2` trading paused by an `UNKNOWN` outcome**.

**Testing.** Entirely offline. The clock, the sleep, the market-data boundary,
the execution boundary, the startup-safety check and the checkpoint are all
injected, so no test waits fifteen real minutes or opens a socket. Four
critical regressions are pinned: an in-progress bar is never processed, the
same completed bar is never processed twice in one process, an `UNKNOWN`
outcome pauses future trading, and a second runner instance is refused.

**Explicitly out of scope for C9:** `UNKNOWN` *resolution* and every other part
of reconciliation, which C8 owns and this package calls rather than
re-implements; live trading in any form; `asyncio`; alerting and monitoring
integrations (Telegram, Slack, Discord, email, SMS); a distributed rate
limiter; and every deployment artefact - systemd units, Docker, cloud scripts,
and VPS provisioning, which are Phase 10.

### C8 + C9 integration - reconciliation as the startup authority (complete)

The two phases were built in parallel against one written contract:

> A runtime may begin trading only when a reconciliation result reports
> `safe_to_trade` true.

**The frozen startup sequence.** Every `crypto-run` start, in this order:

```
1. acquire the OS single-instance runtime lock
2. open the operational SQLite database
3. apply any pending schema migration (transactionally)
4. construct/verify the Alpaca PAPER read boundary
5. run reconcile_paper_state(connection)
6. evaluate the result
7. CLEAN or REPAIRED -> safe_to_trade true
   UNRESOLVED or FAILED -> safe_to_trade false
8. start runtime observation
9. a paper order is possible ONLY when all three hold:
      safe_to_trade
      AND AUTOTRADER_PAPER_TRADING_ENABLED=true
      AND --confirm-paper-runtime PAPER
```

**No environment variable, flag, or combination of the two can bypass step 5.**
The gates are independent conditions, not alternatives, and each defaults
closed. Tests assert that both paper gates fully open plus a non-green
reconciliation still submits zero orders.

**The status mapping**, written once in
`runtime.safety.startup_safety_from_reconciliation_result`:

| Reconciliation | Startup safety | May trade |
| --- | --- | --- |
| `CLEAN` | `SAFE` | yes |
| `REPAIRED` | `SAFE` | yes |
| `UNRESOLVED` | `UNSAFE` | no |
| `FAILED` | `UNSAFE` | no |

The decision is read off `result.safe_to_trade` rather than re-derived from
`status`, so the mapping cannot drift from C8's own rule. **`REPAIRED` is
safe**: a local snapshot rewritten *from the broker* is now correct, and
blocking the runner afterwards would let one resolved historical difference
stop it permanently.

**Not safe is observation, not a crash.** An unsafe start continues in an
explicit observation-only state, exits `0`, and says so loudly -
`RECONCILIATION NOT SAFE - TRADING DISABLED` on the banner, in the
`startup_safety` log line at `WARNING`, in the heartbeat's
`reconciliation_status`, and in `system_events`. This is the smallest behaviour
consistent with C9's existing contract, which already observed while unsafe. It
never continues silently as though safe.

**Reconciliation is per process, never cached.** Each start runs a fresh pass.
A previous run's green result describes the world at the moment it was
produced; a new process inherits the database, not the conclusion.

**`--observe-only` still reconciles** - startup-safety visibility is useful even
when nothing could be sent - and remains incapable of submission whatever the
result, because it constructs no execution path at all.

**Two locks for two problems, both kept.** The `fcntl` lock stops two runners
existing against one database. The durable checkpoint stops one runner - or its
replacement after a crash - acting twice on one bar. Neither substitutes for
the other.

**The safety preference, stated plainly: miss a trade rather than duplicate a
trade.** A completed bar is durably claimed *before* it can cause a broker
submission:

```
completed bar
 -> durable bar claim (committed)
 -> signal
 -> risk
 -> OrderIntent committed
 -> broker submission
```

Crash recovery by position in that chain:

| Crash point | Consequence after restart |
| --- | --- |
| before the durable claim | the bar may be processed |
| after the claim, before the intent | the bar is **skipped** - that trade is missed, permanently |
| after the intent, before submit | C8 resolves `CREATED` / no broker order safely |
| during submit | C8 resolves `UNKNOWN` using the persisted `client_order_id` |
| after submit | C8 repairs from broker truth |

This is **at-most-once**, and it is not exactly-once. Nothing here pretends to
provide mathematically perfect exactly-once distributed execution; that is not
achievable with one local SQLite file and one remote broker, and claiming it
would be worse than not having it. Losing a fifteen-minute crossover is
recoverable. A duplicate position is not.

**Dependencies unchanged.** No Redis, no Postgres, no distributed lock service,
no Kafka. One SQLite file and one `fcntl` lock.

**Still pending after this gate:** no integrated paper BUY has been observed end
to end. That smoke gate comes first, then failure injection, then Phase 10.

### C10 - Failure injection and production hardening (complete)

Everything above says what the system does when it works. This phase asked what
it does when it breaks, at each of the seven points where a process death or a
lost reply straddles something irreversible - and closed the three gaps the
answers exposed.

**The guarantees, in one table.** Each row is enforced by tests in
`tests/test_failure_injection.py`, and each was mutation-checked: the protection
was deliberately removed and the tests confirmed to fail.

| Failure | Guarantee |
| --- | --- |
| crash before the bar claim | the bar may be processed after restart |
| crash after the bar claim | the bar is **skipped forever** - that trade is missed |
| crash after the intent commits, before submit | reconciliation confirms broker absence and marks the intent `CONFIRMED_NOT_SUBMITTED`; the stale decision is never sent |
| submit reply lost - timeout, reset, `408`, `429`, `5xx`, unreadable status | `UNKNOWN` under the **same** `client_order_id`; no retry; trading paused for that process |
| submit reply unreadable or unstorable | also `UNKNOWN`, for the same reason: a request went out |
| definitive `4xx` rejection | `REJECTED`, terminal, no retry, and the runtime is **not** paused |
| broker accepted, process died before the snapshot | reconciliation adopts the existing order; no replacement is placed |
| partial fill | `filled_quantity`, fill price and broker status are preserved exactly; no remainder order |
| broker fill, cancel or reject after process death | discovered on restart and recorded; the broker's position is adopted |
| market-data timeout / `429` / `5xx` | no trade that cycle, no broker call at all, one attempt per symbol, next wake on the next 15-minute boundary |
| broker read failure before submission | fails closed; no order |
| SQLite locked before the bar claim | fatal; no signal, no intent, no order |
| SQLite locked before the intent commit | fatal; `submit_order` is never called |
| second runtime process | refused by the `fcntl` lock before market data, before the broker, and before startup reconciliation |
| repeated reconciliation | first pass `REPAIRED`, second `CLEAN`; no extra writes and no extra broker reads for settled orders |

**Three production changes, all narrow.**

*An unreadable or unstorable reply to a submission is ambiguous.* Previously only
an exception raised *by* `submit_order` produced `UNKNOWN`; a reply that came
back and could not be parsed or written was an ordinary `ExecutionError`, so the
runtime carried on and traded the next boundary with an unaccounted order at the
broker. Once the call has been entered a request has gone out, so both halves of
the response now mark the intent `UNKNOWN` and pause trading.

*Nothing is submitted against an intent that is not committed.* `NO DURABLE
INTENT = NO BROKER SUBMISSION` was an arrangement of call sites rather than a
rule: `record_order_intent` joins an enclosing transaction instead of committing
inside one, so a caller that wrapped the pipeline would have put a real order at
the broker under a `client_order_id` no restart could find. `submit_order_intent`
now refuses when its connection is inside an open transaction.

*Nothing acts on a bar whose claim is not committed*, for the same reason and
with the same check, in `SqliteCheckpoint.mark_processed`. A claim that can still
be rolled back cannot stop a restarted process re-deciding the bar - the
duplicate-trade direction.

**What was deliberately not built.** No chaos mode, no fault-injection endpoint,
no environment variable that makes the system misbehave, and no third-party
chaos tooling. Failures are injected through the seams the system already had -
the trading client, the data client, the market-data source, the clock, and a
second connection holding a real SQLite write lock. A test asserts no production
module contains a fault switch.

**The automated matrix is offline, by design.** Reproducing "the reply was lost"
or "the database was locked at that instant" against a real broker means
submitting real orders at moments chosen to be unrecoverable, which is the one
thing this system exists to avoid; and a suite that depended on a broker
misbehaving on cue would not be reproducible. The whole suite is proven to pass
with sockets globally disabled. Real-broker validation stays what it has always
been: a bounded, read-only `reconcile` plus `crypto-run --once --observe-only`.

**No behaviour was relaxed.** Every change above turns a previously
ordinary failure into a stricter one. Nothing that used to stop now continues.

### E1 - Equity V0.2 (component-complete, not activated)

The ten-symbol US equity product, on the current architecture, gated the same
way as the crypto one and scheduled entirely differently.

**Universe and timeframe.** SPY, QQQ, IWM, AAPL, MSFT, NVDA, AMZN, GOOGL, META,
TSLA - exactly ten, in that order, which is the processing order. 15-minute
bars, Alpaca stock market data on the IEX feed (section 3.1E).

**Session contract.** Regular US market hours only, read from the broker's
calendar. Holidays are absent from that calendar and are therefore absent here;
early closes report their real 13:00 close and produce a genuinely shorter bar
grid. Timezone semantics are explicit in both directions: naive Eastern in, UTC
everywhere after (section 3.5).

**Completed bars.** The shared `is_bar_complete` rule is reused rather than
re-implemented, with one equity condition added on top - the bar must also be a
regular-session bar of *today's* session. Only the newest completed bar may
cause an action; the lookback exists to give the recursive EMA its state and is
never replayed.

**Strategy.** The existing EMA 20 / EMA 50 crossover, unchanged, unoptimized,
with identical parameters across all ten symbols. No EMA arithmetic was
duplicated and no per-symbol parameter exists.

**Quantities.** Whole shares, floored from the risk engine's approved quantity,
refused below one share, never rounded up (section 3.1E).

**Execution.** The C7 pipeline, called rather than copied: environment gate,
confirmation token, account read, position read with the short refusal, risk
sizing, risk decision persisted, intent and `client_order_id` committed
**before** the broker is called, duplicate preflight failing closed, exactly one
submission attempt, no retry of an ambiguous outcome, broker snapshot
persisted. Two things are added - whole-share normalization, and a
regular-hours gate checked against the broker's **own clock** immediately
before submitting, positioned *before* the intent is written so a closed market
leaves nothing for reconciliation to chase.

**Runtime.** `autotrader.equity.runtime.EquityRuntime`, driven by
`equity-run`. Fixed processing order, one symbol finished before the next is
looked at, never more than one broker submission in flight. Market data for all
ten symbols is fetched in **one batched request per cycle**. The durable
per-symbol checkpoint is the same `runtime_checkpoints` table the crypto runner
uses - `SPY` and `BTC/USD` cannot collide - and the claim commits before the
bar can reach the strategy. The lock is
`<database>.equity.runtime.lock`, a different file from the crypto runner's, so
two services can share an account without blocking each other while two runners
of the *same* product still collide.

**Reconciliation.** `reconcile_paper_state` gained one parameter: the position
universe, defaulting to the crypto pairs so every existing caller is unchanged.
Order intents are deliberately **not** filtered by it - one account has one
`client_order_id` namespace, so an ambiguous equity order blocks the crypto
runner and an ambiguous crypto order blocks the equity runner. A position held
outside the pass's universe is recorded as observed and never traded out of.

**Gates.** Unattended equity paper execution requires all four of:
`AUTOTRADER_PAPER_TRADING_ENABLED=true`, `--confirm-paper-runtime PAPER`,
startup reconciliation reporting safe, and the regular session being open at
the moment of submission. Every one defaults to closed and none can satisfy
another.

**Deliberately not done here.** Combined crypto+equity activation, shared
account-level risk arithmetic, combined exposure limits, per-book allocations
(a "crypto 20% / stocks 20%" split is a Combined Integration decision and is
*not* frozen here), a shared API budget, a distributed rate limiter, a
dashboard, deployment artefacts, and any real equity paper submission. Equity
V0.2 has been exercised read-only against the real paper account and has
submitted **no** stock order.

### Later phases

**The integrated crypto paper smoke gate.** The integration is code-complete and
green offline, and the runtime has been exercised read-only against the real
paper account. What has **not** happened is an integrated paper BUY observed end
to end. That is the next gate, and nothing is deployed before it.

**Failure injection** is complete and is described in C10 above. It is offline
rather than against the real paper account, for the reason stated there:
reproducing a lost reply or a mid-write crash at the broker means placing real
orders at moments chosen to be unrecoverable.

**Phase 10 - Deployment.** Systemd units, container images, host provisioning,
and supervision. Deliberately after the integrated paper smoke gate and failure
injection: there is no point supervising a process whose first real paper order
has not yet been observed end to end.
