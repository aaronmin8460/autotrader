# autotrader - Project Specification (v0.2)

**Status:** Crypto Pivot V0.2 complete. The active system is crypto spot -
BTC/USD and ETH/USD, 15-minute bars, 24/7 - and it can place an order only into
Alpaca's paper environment. Next: Phase 8 reconciliation / crash recovery and
Phase 9 24/7 runtime / monitoring, planned to be developed in parallel.
**Archived milestone:** Equity V0.1 is preserved at the Git tag
`equity-v0.1-phase7`.
**Last updated:** 2026-08-27

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

---

## 4. The archived equity milestone

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

No active production code may depend on an equity symbol,
`StockHistoricalDataClient`, `StockLatestTradeRequest`, the IEX feed,
`TimeInForce.DAY`, a whole-share rule, or an equity market clock. Documentation
may describe the archived milestone historically; executable code may not
contain it, and the test suite asserts each absence against source with
docstrings and comments stripped.

---

## 5. Planned system progression

```
Phase 0  Repository Foundation             <- done (Equity V0.1)
Phase 1  Historical Market Data            <- done, migrated to crypto (C1)
Phase 2  Data Validation                   <- done, migrated to crypto (C2)
Phase 3  Strategy                          <- done, unchanged by the pivot (C3)
Phase 4  Backtesting                       <- done, migrated to crypto (C4)
Phase 5  Risk Engine                       <- done, migrated to crypto (C5)
Phase 6  SQLite Operational State          <- done, schema v3 (C6)
Phase 7  Alpaca Paper Trading              <- done, migrated to crypto (C7)
--- Crypto Pivot V0.2 complete ---
Phase 8  Reconciliation / Crash Recovery   <- next, in parallel with Phase 9
Phase 9  24/7 Runtime / Monitoring         <- next, in parallel with Phase 8
Phase 10 Deployment
```

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
- **Operational trading state:** a local SQLite database at schema **v3**. It
  stores strategy runs, signals, risk events, system events, local position
  snapshots, order intents, broker-order snapshots, and UTC-day risk baselines.
  Fills, executions, and reconciliation records are **not** stored yet - they
  belong to Phase 8. Database files are ignored by git.
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

---

## 8. Explicit exclusions

The following are **out of scope** and must not be introduced without an
explicit, documented scope change.

**Trading scope:** live trading, real-money order submission, US equities,
options, futures, forex, perpetual futures, non-USD quote currencies, pairs
outside BTC/USD and ETH/USD, short selling, leverage, margin strategies,
multiple brokers, multiple users.

**Order types:** anything other than MARKET with GTC time in force. No limit,
stop, stop-limit, trailing-stop, bracket, or OCO orders; no notional orders;
no `DAY` and no `IOC`.

**Signal generation:** AI trading agents, LLM-generated trade signals,
machine-learning trading models, sentiment analysis, additional indicators,
parameter or walk-forward optimization.

**Application surface:** web frontend, Next.js, FastAPI, mobile application,
TradingView integration, strategy marketplace.

**Infrastructure:** PostgreSQL, Supabase, Redis, Celery, Kafka, Kubernetes,
cloud deployment, Docker (unless explicitly requested in a future phase).

**Process:** dual-market abstractions, asset-class switches, speculative
refactors, abstractions built for hypothetical future requirements, complex
abstract base classes.

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

### C6 - SQLite operational state, schema v3 (complete)

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

**Tables.** Exactly nine.

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
14:00 UTC. The stored `captured_at` records how close it was. A 24/7 runner
(Phase 9) will make the first observation land near the boundary.

This is a **persistence primitive only**. Nothing here schedules anything,
watches a clock, or decides when an observation should happen.

**Schema migration (v1 -> v2 -> v3).** A new database is created directly at
v3. An older one is upgraded through an explicit ordered path, in a single
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

A database written by a **newer** version is refused and left untouched; one
older than v1 has no path and is refused too.

**No CLI.** There is no `db-init`, `db-shell`, or migration command.

**Explicitly out of scope for C6:** fills, executions, broker accounts,
reconciliation, crash recovery, trading loops or schedulers, a migration
framework, connection pooling, an ORM, a database CLI, backup and restore
tooling, and database repair.

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

**Explicitly out of scope for C7:** live trading in any form; startup or
crash-recovery reconciliation; open-order synchronization; fill-history
reconciliation; position repair; automatic `UNKNOWN` resolution; retry or
backoff on submission; streaming or websockets; order replacement or
cancellation as part of the execution path; multi-broker abstraction; notional,
limit, stop, bracket, or OCO orders; a scheduler or 24/7 loop; a monitoring
surface; a frontend; and deployment.

### Later phases

**Phase 8 - Reconciliation / Crash Recovery.** It owns: resolving `UNKNOWN`
intents against the broker by `client_order_id`, startup broker-vs-local
reconciliation, open-order synchronization, fill history, position repair, and
the local order state machine. C7 deliberately built only the durable anchors
it will need.

**Phase 9 - 24/7 Runtime / Monitoring.** It owns: the continuous loop, wake-up
scheduling on completed 15-minute bars, bar-freshness checks, heartbeat and
alerting, and the process supervision that makes the first equity observation
of a UTC day land near the boundary. The pivot made every contract 24/7-*safe*;
nothing in the current system loops, schedules, or polls.

These two are planned to be developed in parallel. Neither has started.
