# autotrader - Project Specification (v0.1)

**Status:** Phase 7 complete - Alpaca **paper** order execution is merged into
`main`. The system can now place an order, and only ever into Alpaca's paper
environment. Next: Phase 8 reconciliation / crash recovery.
**Last updated:** 2026-08-27

This document is the authoritative scope definition for this repository. When
this document and any prior conversation, chat history, or memory disagree,
**this document wins**. Change the scope by editing and committing this file,
not by asserting a change in conversation.

---

## 1. Purpose

`autotrader` is a personal, single-user automated trading system for US
equities. It is an engineering project first: the objective is a correct,
safe, reproducible trading pipeline that can be trusted to run unattended
against a **paper** brokerage account.

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
   phase. No abstractions for hypothetical future requirements.
5. **Low operating cost** - local process, local files, free/low-cost data.

Feature count is explicitly *not* a goal.

---

## 3. V0.1 scope (frozen)

| Dimension | Decision |
| --- | --- |
| Market | US equities only |
| Broker | Alpaca only |
| Execution environment | Paper trading only |
| Universe | SPY, QQQ, AAPL, MSFT, NVDA |
| Primary timeframe | 15-minute bars |
| Direction | Long only |
| Research strategy | EMA 20 / EMA 50 crossover |
| Historical data storage | Parquet |
| Operational trading state | SQLite, local file (Phase 6) |
| Application style | Python CLI / local process |
| Frontend | None |

### 3.1 Initial market and universe

US equities, five liquid symbols: **SPY, QQQ, AAPL, MSFT, NVDA**. The
universe is fixed for V0.1. Universe expansion is a scope change requiring an
edit to this document.

### 3.2 Data frequency

**15-minute bars** are the primary timeframe. Daily bars may be derived from
stored data if a later phase needs them. Sub-minute, tick, and order-book data
are out of scope.

### 3.3 Initial research strategy

**EMA 20 / EMA 50 crossover, long only.**

This strategy exists to validate the *engineering pipeline* end to end - data
-> signal -> risk -> intent -> execution -> reconciliation. It is a test
fixture, not an edge. No claim is made or implied that it is profitable. Its
backtest results must never be used to justify enabling live trading.

---

## 4. Planned system progression

Phases are sequential. Each phase must be complete and verified before the
next begins.

```
Phase 0  Repository Foundation          <- done
Phase 1  Historical Market Data         <- done
Phase 2  Data Validation                <- done
Phase 3  Strategy                       <- done
Phase 4  Backtesting                    <- done
Phase 5  Risk Engine                    <- done
Phase 6  SQLite Operational State       <- done
Phase 7  Alpaca Paper Trading           <- done
Phase 8  Reconciliation / Crash Recovery  <- next
Phase 9  Monitoring
Phase 10 Deployment
```

---

## 5. Storage policy

- **Historical market data:** Parquet files under `data/`.
  - `data/raw/` - as fetched from the provider, normalized only to the
    canonical schema in section 8 (Phase 1).
  - `data/processed/` - validated bars used by backtests. Phase 2 validates
    `data/raw/` in place and writes nothing; populating this directory belongs
    to a later phase.
  - Market data is **never committed**. The directories are tracked via
    `.gitkeep`; their contents are ignored.
- **Operational trading state:** a local SQLite database, introduced in
  Phase 6 and extended to schema **v2** in Phase 7. It stores strategy runs,
  signals, risk events, system events, local position snapshots, order
  intents, and broker-order snapshots. Fills, executions, and reconciliation
  records are **not** stored yet - they belong to Phase 8. Database files are
  ignored by git.
- **Secrets:** environment variables loaded from a local `.env`, which is
  ignored by git. `.env.example` documents the variable names only and
  contains no values.
- Data directories and the SQLite file are local artifacts. The repository
  must remain reproducible from source code plus a data re-fetch.

---

## 6. Safety principles

These are architectural rules for the whole project. They are **documented
now and enforced when the relevant subsystem is built**. None of them are
implemented in Phase 0.

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
externally visible, and impossible to flip by accident - distinct credentials,
distinct state storage, and an explicit, deliberate opt-in.

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
state before new orders are placed. A system that cannot recover from a crash
without duplicating orders is not permitted to trade.

### F. Backtesting must not use future information

No look-ahead bias. A decision at bar *t* may use only information available
at the close of bar *t*, and fills must occur at *t+1* or later. Indicator
warm-up periods must be respected; incomplete/partial bars must not be traded
on.

### G. Repository state is the source of truth

Committed code and committed documentation define the system. Do not rely on
prior chat context, memory, or undocumented intent. If it matters, it is in
the repository.

---

## 7. Explicit exclusions

The following are **out of scope** and must not be introduced without an
explicit, documented scope change.

**Trading scope:** live trading, real-money order submission, options, crypto,
futures, forex, short selling, leverage, margin strategies, multiple brokers,
multiple users.

**Signal generation:** AI trading agents, LLM-generated trade signals,
machine-learning trading models, sentiment analysis.

**Application surface:** web frontend, Next.js, FastAPI, mobile application,
TradingView integration, strategy marketplace.

**Infrastructure:** PostgreSQL, Supabase, Redis, Celery, Kafka, Kubernetes,
cloud deployment, Docker (unless explicitly requested in a future phase).

**Process:** speculative refactors, abstractions built for hypothetical future
requirements, complex abstract base classes.

---

## 8. Phase boundaries

### Phase 0 - Repository Foundation (complete)

**In scope:** src-layout Python package, `pyproject.toml`, minimal Typer CLI
that prints help and version, this specification, README, `.gitignore`,
`.env.example`, minimal foundation tests, empty data/docs directory structure.

**Explicitly out of scope for Phase 0:** market-data download, data validation,
indicators (including EMA), strategy logic, backtesting, risk calculations,
SQLite schemas, order models, broker connectivity, Alpaca API calls, credential
requirements, reconciliation, monitoring, deployment, CI/CD, pre-commit hooks,
Docker.

**Done when:** `import autotrader` works, `python -m autotrader.cli --help`
works, `pytest` passes, `ruff check .` passes, and no secrets or market data
are committed.

### Phase 1 - Historical Market Data (complete)

Fetch 15-minute historical bars for the V0.1 universe from Alpaca's data API
and persist them to Parquet under `data/raw/`. Read-only market data access;
no trading endpoints, no order submission, and no `TradingClient` anywhere in
the codebase.

The contract below is authoritative for every later phase that reads stored
bars.

**Provider and feed.** Alpaca only, via `alpaca-py`, using the **IEX** equity
feed. IEX is the free-tier-compatible path. No second market-data provider and
no paid provider may be added as a fallback; a gap in IEX coverage is a data
problem to be handled in Phase 2, not a reason to add a provider.

**Symbols.** The five V0.1 symbols only (SPY, QQQ, AAPL, MSFT, NVDA). User
input is uppercased; anything outside the universe is rejected.

**Timeframe.** `15m` only. There is deliberately no generic timeframe
framework.

**Date semantics.** `--start` and `--end` are `YYYY-MM-DD` US market calendar
dates interpreted in `America/New_York`. `--end` is **inclusive** from the
user's perspective; internally the request boundary becomes midnight
`America/New_York` on the following day, converted to UTC. Malformed dates and
`end < start` are rejected. Exchange holiday and session-calendar awareness is
**not** part of Phase 1.

**Canonical bar schema.** Every stored row has exactly these columns, in this
order:

| Column | Type | Notes |
| --- | --- | --- |
| `timestamp` | datetime, tz-aware | Always UTC. Bar open time. |
| `symbol` | string | Uppercase. |
| `open` | float | |
| `high` | float | |
| `low` | float | |
| `close` | float | |
| `volume` | float | |
| `trade_count` | float, nullable | Null when Alpaca omits it. |
| `vwap` | float, nullable | Null when Alpaca omits it. |

Rows are ordered **ascending by `timestamp`**.

**File naming.** Output is deterministic and date-ranged, so a download for a
different range can never silently overwrite an existing file:

```
data/raw/{SYMBOL}_15m_{START}_{END}.parquet
data/raw/{SYMBOL}_15m_{START}_{END}.metadata.json
```

Both files are written via a temporary file and an atomic rename, so an
interrupted run cannot leave a truncated Parquet file behind.

**Metadata sidecar.** A small JSON file records `provider`, `feed`, `symbol`,
`timeframe`, `requested_start`, `requested_end`, `timestamp_timezone`,
`retrieved_at_utc`, `row_count`, and `parquet_filename`. It must never contain
credentials or account information.

**Credentials.** `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are read from the
process environment. Missing credentials produce a clear message, not a
traceback. Credentials are never logged, printed, or persisted.

**Empty responses.** Zero returned bars is an error, not an empty file. No
Parquet or metadata file is written.

**Explicitly out of scope for Phase 1:** data-quality validation (duplicate
timestamps, OHLC relationships, missing bars, session continuity, anomalous
prices, quality reports - all Phase 2), indicators, strategies, backtesting,
risk, SQLite, order models, and every trading endpoint.

### Phase 2 - Data Validation (complete)

Validate a Parquet bar dataset that Phase 1 already wrote, and answer one
question: is it structurally and internally valid enough for future
strategy and backtesting code to consume?

Phase 2 is read-only. It does not download, modify, repair, or rewrite market
data, and it produces no signals, backtests, or broker calls. Validation must
not mutate the DataFrame it is given.

**Validation rules.** The Phase 1 canonical schema in section 8 is the
authoritative input contract. A dataset is valid when all of the following
hold.

| Area | Rule |
| --- | --- |
| Schema | Exactly the canonical columns. Missing and unexpected columns are both reported; none are silently dropped or added. |
| Rows | At least one row. A zero-row dataset is invalid. |
| `timestamp` | Present, non-null, timezone-aware, UTC, strictly ascending with no duplicates. |
| `symbol` | Non-null, uppercase, exactly one distinct value, and in the V0.1 universe. |
| OHLC | `open`, `high`, `low`, `close` non-null, finite, and greater than zero, with `high >= low`, `high >= open`, `high >= close`, `low <= open`, and `low <= close`. |
| `volume` | Present, numeric, finite, and `>= 0`. Zero volume is legitimate; it is not an error. |
| `trade_count` | Nullable. When present: numeric, finite, `>= 0`. |
| `vwap` | Nullable. When present: numeric, finite, `> 0`. |

**Bar spacing is not checked.** Continuous 15-minute intervals are *not*
required. Weekends, holidays, overnight closures, and early closes make gaps
normal, and Phase 2 adds no exchange-calendar dependency.

**Explicitly out of scope for Phase 2:** exchange-holiday awareness, market
session completeness, missing-bar detection, early-close handling, outlier
and price-spike heuristics, statistical anomaly detection, split and
corporate-action correctness, suspicious-volume patterns, cross-provider
consistency, data repair, and writing to `data/processed/`. These may be
researched in a later phase if a concrete need appears.

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
violations are summarized with a row count (`3 rows violate high >= low`)
rather than emitted one error per row. A malformed or unreadable Parquet file
is a controlled input error instead, because there are no rows to report on.

**CLI.** `validate <path>` prints a `VALID` or `INVALID` report and exits `0`
for a valid dataset, `1` when validation errors were found, and `2` when the
file cannot be read. Neither an invalid dataset nor a missing file produces a
traceback, and the command is non-interactive.

**Done when:** the validator implements exactly the rules above, leaves its
input unmodified, the CLI honours those exit codes, and `pytest`, `ruff check
.`, and `ruff format --check .` all pass with every test offline.

### Phase 3 - Strategy (complete)

The EMA 20 / EMA 50 crossover signal generator, `autotrader.strategies.ema_cross`.
It is a pure domain module: canonical bars in, signals out. There is no CLI
command, no broker client, and no import of one (section 6A).

**Purpose.** This strategy validates the engineering pipeline. It is a test
fixture, not an edge, and **no claim of profitability is made or implied.**

**Indicator.** Two EMAs of the **`close`** column, periods **20** and **50**,
both computed with pandas `ewm(span=period, adjust=False)` - the recursive
form seeded with the first observation, `ema[i] = ema[i-1] + alpha *
(close[i] - ema[i-1])` with `alpha = 2 / (period + 1)`. The periods are fixed
for V0.1 and deliberately not configurable.

**Warm-up.** `min_periods=period`, so `ema_20` is undefined for the first 19
bars and `ema_50` for the first 49. A crossover also needs the previous bar's
relation, so the earliest bar that can carry a signal is the 51st.

**Crossover semantics.** Long only, two signal types:

| Signal | Condition |
| --- | --- |
| `BUY` | fast EMA was `<=` slow EMA on the previous bar and is strictly `>` on this bar |
| `EXIT` | fast EMA was `>=` slow EMA on the previous bar and is strictly `<` on this bar |

Any other bar produces nothing. Because the rule reads the previous bar's
relation, a crossover yields at most one signal and neither signal repeats
while the relation merely persists. There are no short signals, no stop loss,
no take profit, and no additional indicators or filters.

**Signal model.** A frozen dataclass of `timestamp`, `symbol`, `type`
(`BUY`/`EXIT`), and `reason`. `reason` is a stable machine string -
`EMA20_CROSS_ABOVE_EMA50` or `EMA20_CROSS_BELOW_EMA50` - never a
natural-language explanation. Signals are returned ascending by timestamp.

**Signal timestamp is not an execution timestamp.** The timestamp is the bar
whose close made the crossover knowable. The signal carries no price and
asserts no trade. When and at what price a signal could be acted on is Phase 4
backtesting's decision, bound by section 6F: a decision at bar *t* uses only
information available at the close of *t*, and fills occur at *t+1* or later.

**Input contract.** Exactly one symbol, timestamps ascending, and the
`timestamp`, `symbol`, and `close` columns present. Violations raise
`StrategyInputError`. The supplied DataFrame is never modified and is never
sorted in place - silently reordering would mask an upstream data-contract
violation. Empty input yields no signals. These checks are the minimum needed
to avoid obscure pandas errors; full data-quality validation is Phase 2 and is
not duplicated here.

**Explicitly out of scope for Phase 3:** trade simulation, execution prices,
fills, position sizing, portfolio or P&L calculation, risk management, order
creation, broker connectivity, multi-symbol grouped processing, configurable
periods, additional indicators, parameter optimization, a strategy-plugin
framework, and any strategy CLI command.

### Phase 4 - Backtesting (complete)

The deterministic long-only backtesting engine, `autotrader.backtest.engine`.
It connects the phases that already exist - stored Parquet bars -> Phase 2
validation -> Phase 3 signals -> execution simulation -> portfolio accounting
-> metrics - and answers one question: does that pipeline hold together and
account correctly?

Phase 4 is **engineering validation**. It is not a claim that the EMA
crossover is profitable, and its results must never be used to justify
enabling any form of trading. The whole simulation is local arithmetic: no
order is created, no broker is contacted, and no network access occurs.

**Input.** The Phase 1 canonical bar frame in section 8, unchanged. There is
no second bar schema. Bars are validated with the Phase 2 validator before any
signal is generated; a dataset with any validation finding aborts the backtest
with a controlled error. Nothing is repaired - no re-sorting, no column
patching, no dropped rows - and the supplied DataFrame is never modified.
Validation rules are not duplicated in the engine.

**Strategy.** The existing Phase 3 public API, `generate_ema_cross_signals`.
The engine computes no EMA and re-detects no crossover; it consumes Phase 3
signals and does not alter their semantics. There is exactly one strategy and
no strategy selection.

**Execution timing (section 6F).** A crossover on bar *t* is knowable only
once bar *t* has closed, so the earliest it can be acted on is the open of bar
*t+1*:

```
signal on bar t  ->  fill at bar t+1 open
```

A signal is never filled on its own bar - not at that bar's open, not at its
close - and `execution_timestamp` is always strictly greater than
`signal_timestamp`. **A signal on the final bar is not executed**: there is no
following bar, and no future price is invented or substituted. It is reported
as pending historical information via
`unexecuted_last_bar_signal_count`.

**Execution price.** Exactly the next bar's `open`. Commission, fees, and
slippage are all **zero**, and there is no market impact or partial fill. This
is a deliberate engineering baseline for V0.1, not a realistic execution
model, and there is deliberately no transaction-cost framework.

**Portfolio.** $100,000.00 initial cash, long only, no leverage, no borrowing,
no short selling, and at most one position in the single symbol being
backtested. `initial_cash` must be positive and finite; anything else is a
controlled error.

**Sizing.** On a `BUY` while flat, all available cash buys the largest
whole-share quantity possible at the fill price:
`quantity = floor(cash / price)`. Cash never becomes negative. Fractional
shares are out of scope. A `BUY` that cannot afford one whole share creates no
execution.

**Exit.** On an `EXIT` while long, the entire position is sold at the next
bar's open. There are no partial exits.

**No-op signals.** The real signal sequence may open with an `EXIT` while the
simulated portfolio is flat. `EXIT` while flat and `BUY` while already long
are both no-ops - never a short, a double-buy, a pyramid, or a duplicate
holding - and neither produces an execution.

**End of backtest.** An open position is **not** force-liquidated at the final
bar and no closing `SELL` record is fabricated. It is marked to market at the
final bar's `close`:
`final_equity = cash + position_quantity * final_close`. This mark is not an
execution.

**Equity curve.** One end-of-bar equity value per bar,
`equity = cash + position_quantity * bar.close`. When a fill occurs at a bar's
open, that fill is processed *before* that same bar's close is marked. No
future bar is consulted.

**Max drawdown.** Peak-to-trough over that equity curve:
`drawdown_t = equity_t / max(equity_0..equity_t) - 1`, and `max_drawdown` is
the minimum observed. It is stored as a **decimal fraction** and is never
positive (`-0.25` is a 25% drawdown); the CLI renders it as a percentage.

**Total return.** `(final_equity / initial_cash) - 1`, a decimal fraction. No
annualization, no benchmark, no alpha or beta, and no additional metrics.

**Execution model.** A frozen `Execution` of `signal_timestamp`,
`execution_timestamp`, `symbol`, `side`, `quantity`, `price`, and
`cash_after`. `side` is `BUY` or `SELL`: an `EXIT` *signal* produces a `SELL`
*execution*, and the two vocabularies are kept distinct so a signal is never
mistaken for a trade.

**Completed round trip.** A `BUY` execution followed later by a `SELL`
execution. An open position at the end is **not** a completed round trip. The
word "trade" is deliberately avoided for an individual execution.

**Result model.** A frozen `BacktestResult` exposing `symbol`, `bar_count`,
`initial_cash`, `final_cash`, `final_equity`, `total_return`, `max_drawdown`,
`ending_position_quantity`, `ending_position_market_value`,
`completed_round_trips`, `signal_count`, `unexecuted_last_bar_signal_count`,
`executions`, `equity_curve`, and the derived `buy_execution_count` and
`sell_execution_count`. The same input always produces the same result.

**Public API.** `run_backtest(bars, initial_cash=100_000.0) -> BacktestResult`.

**CLI.** `backtest <path> [--initial-cash 100000]` prints a concise summary -
never a per-execution blotter - and exits `0` on a completed simulation, `1`
when the dataset or the starting cash is unusable, and `2` when the file
cannot be read. No expected failure produces a traceback.

**Explicitly out of scope for Phase 4:** risk limits of any kind, position or
exposure caps, daily loss limits, order intents, broker adapters, any trading
client, paper or live orders, SQLite, execution persistence, reconciliation,
crash recovery, monitoring, a frontend, deployment, multiple strategies or
symbols, strategy selection, parameter or walk-forward optimization, Monte
Carlo, Sharpe, Sortino, alpha, beta, benchmark comparison, fractional shares,
transaction-cost modelling, an event bus, a plugin or broker-simulator
framework, and vectorized optimization.

### Phase 5 - Risk Engine (complete)

The deterministic risk engine, `autotrader.risk.engine`. It occupies the stage
between a signal and an order intent (section 6A) and answers exactly one
question: may this proposed trade be allowed, and if so, what is the largest
safe whole-share quantity under the V0.1 limits?

Phase 5 is a **calculator**. It submits no order, constructs no broker client,
makes no network call, opens no database, persists nothing, writes no file,
and mutates neither the request nor the context it is given. The same inputs
always produce the same decision.

**Not wired into Phase 4.** The backtester keeps its own all-cash sizing
baseline and is deliberately *not* re-plumbed through these limits, so no
Phase 4 result changes. Integrating risk into real sizing belongs to paper
execution, a later phase.

**V0.1 risk policy.** These are engineering safety defaults. They are **not**
investment advice and not a recommended allocation.

| Field | Value | Meaning |
| --- | --- | --- |
| `max_position_fraction` | `0.05` | market value of any one symbol <= **5%** of current equity |
| `max_total_exposure_fraction` | `0.30` | aggregate long exposure <= **30%** of current equity |
| `max_daily_loss_fraction` | `0.02` | new entries halt once the day is down **2%** of start-of-day equity |
| `long_only` | `true` | no short side exists to request |
| `allow_leverage` | `false` | an entry may only spend cash already held |
| `whole_shares_only` | `true` | quantities are floored to whole shares |

The policy is fixed for V0.1, strategy-independent, and deliberately not
loaded from the environment. `DEFAULT_POLICY` encodes exactly the table above.
A policy that flips one of the booleans is **refused** rather than partly
honoured: silently ignoring `allow_leverage=True` would leave a caller
believing something untrue about the system.

**Risk context.** A flat, immutable snapshot - not a portfolio object
hierarchy: `equity`, `cash`, `total_exposure`, `symbol_exposure`,
`current_position_quantity`, `daily_pnl`, `start_of_day_equity`, and
`trading_enabled`. `symbol_exposure` is part of `total_exposure`, and
`daily_pnl` is the only field that may be negative.

**Risk request.** A small internal `RiskRequest` of `symbol`, `side`,
`reference_price`, and `requested_quantity`, where `side` is `BUY` or `SELL`.
It is a risk-calculation question, **not** a broker order and not a persisted
order intent: it carries no order type, no time in force, no identifier, and
no broker field. `reference_price` is the price sizing is done against - a
mark, never a promised fill.

**Kill switch.** `trading_enabled` is an explicit boolean in the context. When
it is `false`, **every new BUY entry is rejected**. It does **not** block an
exit; see below.

**Entry gates.** A BUY is approved only when all of these hold, checked in
this order so the reported reason is deterministic:

1. the request is well formed - positive finite price, positive whole-share
   quantity, non-empty symbol, a real `RiskSide`;
2. `trading_enabled` is `true`;
3. `daily_pnl / start_of_day_equity > -0.02`;
4. the resulting symbol exposure is within 5% of equity;
5. the resulting total exposure is within 30% of equity;
6. the required cash is within available cash - the no-leverage rule.

**Entry sizing.** The tightest of the three ceilings wins, and the quantity is
floored to whole shares:

```
position_remaining  = max(0, equity * 0.05 - symbol_exposure)
portfolio_remaining = max(0, equity * 0.30 - total_exposure)
max_notional        = min(position_remaining, portfolio_remaining, cash)
max_quantity        = floor(max_notional / reference_price)
approved_quantity   = min(requested_quantity, max_quantity)
```

Ties between ceilings resolve in that fixed order. That `floor` is the
**only** rounding in the engine.

**Clamping, not refusing.** A BUY larger than the safe maximum is approved at
`max_quantity` rather than rejected, and `reason_code` names the constraint
that bound it - `POSITION_LIMIT`, `TOTAL_EXPOSURE_LIMIT`, or
`INSUFFICIENT_CASH` - instead of `APPROVED`. Requesting 100 shares when 12 are
safe yields an approved decision for 12. When `max_quantity` is **zero** the
request is rejected. Nothing is ever approved above a limit, and sizing is
never silently constrained without saying so.

**Daily-loss halt.** `daily_pnl / start_of_day_equity <= -0.02` blocks new
entries, and `start_of_day_equity` must be positive. The boundary is
inclusive: **exactly -2.00% blocks.** Exits are unaffected.

**Exits reduce risk, so risk never blocks them.** A SELL only reduces an
existing long. The `trading_enabled` kill switch, the daily-loss halt, the
per-symbol cap, and the total-exposure cap are **entry** gates, and none of
them is consulted for an exit - a control that prevented an account from
reducing exposure would trap an open position, which is the opposite of a
safety control. A SELL is still checked for a usable price and quantity, and:

- `current_position_quantity <= 0` is rejected as `NO_POSITION_TO_EXIT`;
- a quantity larger than the position is **clamped to the position**
  (`EXIT_QUANTITY_EXCEEDS_POSITION`), so an exit can fully flatten a holding
  but can never cross below zero into a short.

**Long only is structural.** `RiskSide` has exactly `BUY` and `SELL`, so a
short cannot be expressed, and an approved SELL never exceeds the position.
There is therefore no `LONG_ONLY_VIOLATION` reason code: it would be
unreachable, and an unreachable code in a machine-readable contract is a false
promise. Selling while flat surfaces as `NO_POSITION_TO_EXIT`.

**Invalid input is not a risk denial.** The two are deliberately distinct:

- A **context** that cannot describe a real account raises `RiskInputError` -
  `equity <= 0`, `cash < 0`, a negative exposure, `symbol_exposure >
  total_exposure`, a negative or non-integer position, `start_of_day_equity <=
  0`, a non-boolean `trading_enabled`, or any NaN or infinity. Nothing is
  repaired or clamped into range. An unsupported policy raises the same way.
- A **request** that is malformed returns an ordinary rejected decision with
  `INVALID_REQUEST`.
- An ordinary risk denial never raises.

Numeric strings are not coerced: a price or quantity that arrived as text
means something upstream lost its type, and parsing it would hide that.

**Decision model.** A frozen `RiskDecision` of `approved`,
`approved_quantity`, `reason_code`, `message`, and `max_allowed_quantity`.
`approved_quantity` is `0` whenever `approved` is false, and never exceeds
either the requested quantity or `max_allowed_quantity`.
`max_allowed_quantity` is the cap that applied - the sizing ceiling for an
entry, the current position for an exit. `reason_code` is a stable machine
string; `message` is human-readable and is **not** part of the contract. The
codes are:

```
APPROVED             INVALID_REQUEST      TRADING_DISABLED
DAILY_LOSS_LIMIT     POSITION_LIMIT       TOTAL_EXPOSURE_LIMIT
INSUFFICIENT_CASH    NO_POSITION_TO_EXIT  EXIT_QUANTITY_EXCEEDS_POSITION
```

**Numeric policy.** Plain floats, as elsewhere in the project; no `Decimal`
framework is introduced. Values are checked for finiteness explicitly, and
quantities are deterministic integers.

**Public API.** `evaluate_risk(request, context, policy=DEFAULT_POLICY) ->
RiskDecision`. There is no CLI command: Phase 5 has no user-facing action to
offer, only a decision for a later phase to consume.

**Explicitly out of scope for Phase 5:** order creation, order intents, broker
adapters, any trading client, paper or live orders, Alpaca calls of any kind,
SQLite and every other form of persistence, risk-event journaling, changes to
Phase 4 accounting, portfolio state mutation, an event bus, dependency
injection, a generic policy framework, environment-loaded policies,
strategy-dependent risk parameters, per-strategy limits, stop losses, take
profits, trailing stops, volatility or drawdown sizing, correlation limits,
sector limits, intraday rate limiting, reconciliation, monitoring, a frontend,
and deployment.

**Done when:** the limits, decision vocabulary, and entry/exit asymmetry above
hold; the engine imports no persistence, broker, or network module; every test
is offline; and `pytest`, `ruff check .`, and `ruff format --check .` all pass.

### Phase 6 - SQLite Operational State (complete)

The local operational-state foundation, `autotrader.state.sqlite`. It is
**persistence infrastructure and nothing else**: it opens a database, creates a
fixed schema, and stores durable records. It decides nothing, orchestrates
nothing, and contacts nobody. No order is placed, no broker is called, no
signal is executed, and no risk rule is evaluated. It imports only the standard
library, requires no credentials, and opens no socket.

Phase 5 was developed independently and is now merged, but the separation is
deliberate and permanent: Phase 6 does not import it, mirror its models, or
assume its vocabulary. The `risk_events` table stores opaque text so that a
`RiskDecision` can be persisted by a caller that holds both layers, without
either layer depending on the other.

**Technology.** The standard library's `sqlite3`, and nothing else. No ORM, no
migration framework, no async driver, and no database service - the dependency
footprint of this phase is zero. One user, one local process, one file.

**Database file.** The conventional local path is `data/autotrader.db`
(`DEFAULT_DATABASE_PATH`). Nothing creates it implicitly; a caller passes an
explicit path to `initialize_database`. Database files are git-ignored
(`*.db`, `*.sqlite`, `*.sqlite3`, and the WAL/journal sidecars). Every test
writes into a temporary directory, so running the suite never creates a real
persistent database.

**Connections.** Every connection - not just the first - sets
`foreign_keys = ON`, `journal_mode = WAL`, and `busy_timeout = 5000`. Foreign
keys are per-connection in SQLite, so configuring them once at creation time
would silently disable referential integrity for every later caller. There is
no connection pool.

**Transactions.** Connections run with `isolation_level=None`: nothing is
implicitly in a transaction and nothing implicitly commits. Every write goes
through `transaction()`, which commits on success and rolls back on **any**
exception, so a caller never observes a partially written multi-step state.
Nested use joins the outer transaction and leaves the commit to it, which lets
several `record_*` calls be grouped into one atomic unit.

**Schema versioning.** A `schema_metadata` table holds a single
`schema_version` row. Phase 6 shipped version **1**; Phase 7 extended it to
version **2** by one explicit additive migration, and the section below
describes the schema as Phase 6 defined it. `initialize_database(path)` is
idempotent: repeated calls create nothing twice and change nothing. A database
written by a **newer** version is refused with `UnsupportedSchemaVersionError`
and left untouched - never downgraded or overwritten. There is still no
migration *framework*.

**Damaged databases are not repaired.** If the schema metadata and the tables
disagree - a missing table, a version marker without tables, tables without a
version marker - initialization raises `DatabaseStateError` rather than
attempting a fix. Detection is a table-name and version check only. There is no
backup or restore tooling.

**Timestamps.** Every persisted timestamp is ISO-8601 UTC in one canonical
fixed-width form, `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`, so text ordering is also
chronological ordering. Aware inputs in any other zone are converted to UTC.
**Naive datetimes are rejected** - there is no correct guess for the offset,
and reading one as local time would silently misdate an audit record. Domain
times (`started_at`, `signal_timestamp`, `event_timestamp`, `updated_at`) are
supplied by the caller; `created_at` is stamped by this module and records when
the row was written.

**Identifiers.** `INTEGER PRIMARY KEY` row ids. No UUID dependency. A row id is
a local database identifier and must never be presented as a broker id.

**SQL safety.** Every statement is a literal; every value is bound as a
parameter. No SQL is ever built by string interpolation, and a test asserts
that at the source level.

**Tables.** Exactly six as of Phase 6; Phase 7 added `order_intents` and
`broker_orders`.

| Table | Purpose | Invariants |
| --- | --- | --- |
| `schema_metadata` | Schema version marker | Exactly one row (`CHECK (id = 1)`) |
| `strategy_runs` | One logical strategy session | `mode` is `BACKTEST` or `PAPER`; `status` is `RUNNING`, `COMPLETED`, or `FAILED`; `ended_at` NULL until the run ends; a run ends once, never before it started |
| `signals` | Durable Phase 3 signals | `strategy_run_id` -> `strategy_runs.id`; `signal_type` is `BUY` or `EXIT`; unique on `(strategy_run_id, signal_timestamp, symbol, signal_type, reason)` |
| `risk_events` | Risk-decision audit trail | `strategy_run_id` and `symbol` nullable; `decision` and `reason_code` opaque non-empty text |
| `system_events` | General operational events | `event_type` non-empty; `message` nullable |
| `positions` | Latest **local** position snapshot | `symbol` PRIMARY KEY; `quantity >= 0`; `average_price` NULL or `> 0` |

**Signals are immutable facts.** A signal is stored exactly as Phase 3 produced
it. `EXIT` is persisted as `EXIT` and is **never** rewritten as `SELL` - that
translation is an execution decision, and the schema rejects `SELL` outright.
No EMA is recomputed, no risk engine is consulted, and nothing is executed.
Persistence is not orchestration.

**Duplicate signal protection.** The same logical signal - same run, timestamp,
symbol, type, and reason - cannot be stored twice; a repeat raises
`DuplicateSignalError` rather than silently creating a second copy. This is a
storage invariant only, and is unrelated to order idempotency: the stable
client-side order key required by section 6E is `order_intents.client_order_id`,
added in Phase 7.

**Position invariants.** (Phase 7 note: this table is now written from
positions *observed* at the broker, but still never from an order that was
merely accepted.) The system is long only (section 3), so `quantity`
must be a non-negative whole number. This is enforced twice - in Python and as
a SQLite `CHECK` constraint - so a write that bypassed the module still cannot
store a short position. `average_price` is NULL, the natural value for a flat
position, or a finite number greater than zero. No P&L is computed or stored.
**Phase 6 never populates this table from a broker**: it is local state,
nothing synchronizes it, and reconciling it against a broker's authoritative
positions is Phase 8.

**Read models.** Small frozen dataclasses - `StrategyRun`, `StoredSignal`,
`RiskEvent`, `SystemEvent`, `Position` - rather than raw `sqlite3.Row` objects
passed through the codebase. `StoredSignal` is named distinctly from
`autotrader.strategies.Signal`: that one is a freshly computed observation,
this one is a durable record of it. There is no ORM and no repository
framework.

**Auditability.** The schema supports asking, later, which strategy run
produced a signal, when it was produced, what risk event occurred, what
operational event occurred, and what local position snapshot was last stored.
It does **not** yet support "what broker order resulted", because no broker
order exists.

**No tables were created for:** `broker_orders`, `fills`, `executions`,
`broker_accounts`, or `reconciliation_runs`. Their semantics are defined by an
external system this repository did not talk to yet. Adding them later,
correctly, is better than guessing now - which is what happened:
`order_intents` and `broker_orders` arrived in Phase 7 once the broker's actual
vocabulary had been read, and the rest remain Phase 8's.

**No CLI.** Phase 6 adds no `db-init`, `db-shell`, or migration command.
Initialization will be driven by application startup in a later phase.

**Explicitly out of scope for Phase 6:** the risk engine (Phase 5), order
models, order intents, broker orders, fills, executions, broker accounts,
reconciliation, crash recovery, paper or live execution, any Alpaca call,
trading loops or schedulers, migrations, connection pooling, an ORM, a
database CLI, backup and restore tooling, database repair, monitoring, and
deployment.

**Done when:** the schema, invariants, and transaction behaviour above hold;
initialization is idempotent and refuses an unsupported version; every test is
offline and writes only to a temporary directory; and `pytest`, `ruff check .`,
and `ruff format --check .` all pass.

### Phase 7 - Alpaca Paper Execution (complete)

The first phase permitted to submit a broker order, and permitted to submit it
**only** to Alpaca paper trading. `autotrader.execution` is the single boundary
that speaks to a broker: it is the only place in the repository that constructs
a trading client or calls `submit_order`, and a test asserts that the broker
vocabulary appears nowhere outside it.

**Live trading remains impossible, structurally.** This is the whole point of
the phase's design, and it is enforced rather than documented:

- `create_paper_trading_client()` builds `TradingClient(api_key, secret_key,
  paper=True)` with `paper=True` written literally. It takes **no parameters**.
- No public function in the package accepts a `paper` or `live` argument.
- There is no `--live` flag, no `--paper` option, no `ALPACA_LIVE`, no
  `LIVE_TRADING`, and no `BROKER_MODE`. Nothing selects an environment.
- Tests assert that `paper=False` and `TRADING_LIVE` appear nowhere in the
  executable source, that the client factory's signature is empty, and that no
  live CLI option exists.

Live trading is therefore not "off by default" - it cannot be expressed.
Adding it is a scope change requiring an edit to this document (section 6C).

**Two independent submission gates**, both closed by default, neither able to
satisfy the other:

1. **Environment:** `AUTOTRADER_PAPER_TRADING_ENABLED` must equal exactly
   `true` after stripping surrounding whitespace. Missing, empty, `false`,
   `TRUE`, `1`, `yes`, and any other value all leave submission disabled. One
   canonical spelling means a typo always fails closed.
2. **Explicit confirmation:** `--confirm-paper PAPER`, compared exactly.

`--dry-run` requires neither, because it cannot submit. It performs the
read-only work - account, positions, clock, current price, risk evaluation -
prints the preview, and stops without persisting an intent or calling the
broker. The confirmation token is deliberately *not* required for it, so
typing `PAPER` never becomes a reflex.

**Supported scope.** US equities; the five V0.1 symbols; BUY and SELL; positive
whole shares; MARKET orders; DAY time in force; extended hours explicitly
false. No fractional or notional orders, no limit/stop/stop-limit/trailing-stop
orders, no bracket or OCO, no options, no crypto, and no shorts. None of these
are generically supported "but disabled" - they are absent.

**Credentials** are `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`. They are never
printed, logged, persisted, written to SQLite, embedded in a `client_order_id`,
or included in an exception message; errors name the *variables* only. Missing
credentials fail clearly before any broker call.

**The pipeline**, in a mandatory order:

```
current paper account + positions + current IEX reference price
        -> RiskContext
        -> evaluate_risk
        -> RiskDecision              persisted to risk_events
        -> OrderIntent               persisted AND COMMITTED
        -> broker duplicate preflight
        -> Alpaca PAPER market order
        -> broker snapshot           persisted to broker_orders
```

**The risk engine is never bypassed.** Every submission is sized by Phase 5,
and the quantity sent to the broker is `RiskDecision.approved_quantity` - never
the caller's requested quantity. A request for 100 that risk clamps to 3 sends
exactly 3. A rejected decision means no broker request is constructed at all.
The risk decision is persisted explicitly by the caller; `evaluate_risk` was
**not** modified to persist itself and remains a pure calculator.

**Risk context mapping.** Built from current paper broker state:

| `RiskContext` field | Source |
| --- | --- |
| `equity` | `TradeAccount.equity` |
| `cash` | `TradeAccount.cash` |
| `start_of_day_equity` | `TradeAccount.last_equity` (prior trading day's close) |
| `daily_pnl` | `equity - last_equity`, derived rather than read separately |
| `total_exposure` | sum of positive **long** position market values |
| `symbol_exposure` | that symbol's long market value, else 0 |
| `current_position_quantity` | that symbol's long share count, else 0 |
| `trading_enabled` | caller-supplied kill switch (a parameter, not an env var) |

`total_exposure` is summed from the positions themselves rather than read from
`long_market_value`, so the total and the per-symbol figure it must contain
cannot disagree. A missing or non-numeric account field is reported, never
guessed. `trading_enabled` is deliberately not environment-driven: the paper
gate is this phase's operational off switch, and a second env-driven switch
would make it ambiguous which one stopped a trade.

**SELL follows the same path.** Phase 5's contract is preserved exactly: a
risk-reducing exit is still permitted when `trading_enabled` is false or the
daily-loss halt is active, and is clamped to the position so it can flatten but
never open a short. A SELL never exceeds the approved quantity.

**Reference price.** The current latest IEX trade, via the existing Alpaca
market-data client. A stored Parquet bar is never used to size a live order,
and no CLI-supplied price can bypass the risk engine. A price that cannot be
obtained, or that is not finite and positive, fails closed - no order.

**Account safety.** The account must be tradable - an accepted status and none
of `trading_blocked`, `account_blocked`, or `trade_suspended_by_user` - checked
before any submission, for BUY *and* SELL. A short position in the account is
refused outright rather than coerced into a long.

**Market clock.** Read and reported, never used to gate a submission: Alpaca
queues a DAY market order placed while closed. No fill expectation is inferred
from it.

**`client_order_id`.** `autotrader-<uuid4>`, generated **once** when the intent
is created, committed before the broker call, and never regenerated. It is
non-empty, bounded well under Alpaca's 128-character limit, unique locally by a
UNIQUE constraint, and carries no secret and no account information.

**Why the intent is persisted first.** A crash between the request and its
response must still leave a durable anchor. Submitting first and recording
afterwards would produce a real broker order with no local trace - exactly the
orphan that reconciliation exists to resolve. A regression test proves the row
is committed and visible to an independent connection at the moment
`submit_order` is entered.

**Duplicate preflight.** Before submitting, the broker is asked for an order
under this `client_order_id`. If one exists, it is recorded and returned and
**nothing is submitted**. A clear "not found" (`404`) proceeds. Any other
failure - a `5xx`, a timeout, an unreadable status - **fails closed**. "Could
not check" is never treated as "there is no duplicate".

**Submission outcomes.** `submit_order` is called at most once, ever:

| Outcome | Local state |
| --- | --- |
| Broker returned an order | `SUBMITTED`, snapshot persisted |
| Broker definitively refused (a 4xx other than 408/429) | `REJECTED`, no order exists |
| Timeout, reset, 5xx, 408, 429, or unreadable status | `UNKNOWN` |

**An ambiguous outcome is never retried.** There is no automatic resubmission,
no exponential backoff, and no new `client_order_id`. The intent is marked
`UNKNOWN`, a system event records the ambiguity, and the attempt stops. Phase 8
resolves it by asking the broker about that exact key. The SDK's own internal
retry of `429`/`504` responses is disabled on the trading client, because a
silently resubmitted `POST /orders` would defeat this rule.

**Accepted is not filled.** A stored broker snapshot proves acceptance only.
The local `positions` table is written **only** from a position actually
observed at the broker, never inferred from an accepted order, and a successful
submission never increments it.

**Order status is opaque.** The broker's returned status is stored as
normalized text. Phase 7 deliberately defines no local order state machine over
it; formalizing transitions is Phase 8's job.

**Schema v2.** The current schema version is **2**. A new database is created
directly at v2; an existing v1 database is upgraded by one small explicit
migration. There is still no migration framework. The migration is additive -
it creates two tables and re-stamps the version, dropping, recreating, and
rewriting nothing - runs in a single transaction so a failure rolls back to v1
intact, and is idempotent through normal initialization. A database written by
a newer version is still refused; one older than v1 has no path and is refused
too. A test asserts a migrated v2 database is schema-identical to a fresh one.

| Table | Purpose | Invariants |
| --- | --- | --- |
| `order_intents` | Durable pre-submission intent | `client_order_id` UNIQUE and non-empty; `side` is `BUY` or `SELL`; `requested_quantity > 0`; `0 < approved_quantity <= requested_quantity`; `reference_price > 0` and finite; `status` in `CREATED`, `SUBMITTING`, `SUBMITTED`, `UNKNOWN`, `REJECTED`. No broker order id. |
| `broker_orders` | Latest broker snapshot | `order_intent_id` -> `order_intents.id`, UNIQUE; `broker_order_id` UNIQUE; `client_order_id` UNIQUE; `quantity > 0`; `filled_quantity >= 0`; `status` opaque non-empty text |

One broker order per intent, by construction. **No `fills`, `executions`,
`broker_accounts`, or `reconciliation_runs` table exists** - Phase 8 owns them.

**CLI.** One command, `paper-submit`, with `--symbol`, `--side`, `--qty`,
`--confirm-paper`, `--dry-run`, and `--db`. There is no `trade` command and no
`live-submit` command. It prints a non-sensitive preview - environment, market
open/closed, symbol, side, requested quantity, reference price, account equity
and cash, risk decision, approved quantity, `client_order_id` - and never a
credential or an authorization header. Expected operational failures are
reported concisely without a traceback. Exit codes: `0` submitted, already
existing, or dry run; `1` a controlled refusal; **`2` an `UNKNOWN` outcome**,
given its own code so a script can never mistake it for a clean refusal.

**Testing.** The automated suite is entirely offline: the Alpaca boundary is
mocked, the fakes return real alpaca-py models so normalization is exercised
against real response shapes, no real credential is read, and sockets are
asserted shut. Source-level tests scan *executable* code with docstrings and
comments stripped, so prose describing a forbidden construct cannot mask its
presence or absence.

**Explicitly out of scope for Phase 7:** live trading in any form; startup or
crash-recovery reconciliation; open-order synchronization; fill-history
reconciliation; position repair; automatic `UNKNOWN` resolution; retry or
backoff on submission; `TradingStream`, websockets, or any streaming; order
replacement or cancellation as part of the execution path; multi-broker
abstraction; fractional, notional, limit, stop, bracket, or OCO orders;
options; crypto; shorting; a monitoring surface; a frontend; and deployment.

**Done when:** the gates, ordering, failure semantics, and schema above hold;
`submit_order` is never called more than once per intent and never after an
ambiguous outcome; the risk-approved quantity is the only quantity sent; the
intent is committed before submission; the migration preserves Phase 6 data;
every automated test is offline; and `pytest`, `ruff check .`, and
`ruff format --check .` all pass.

### Later phases

Each later phase is specified when it is reached. A phase may not begin until
the previous phase's acceptance criteria are met and committed.

**Phase 8 - Reconciliation / Crash Recovery** is next. It owns: resolving
`UNKNOWN` intents against the broker by `client_order_id`, startup
broker-vs-local reconciliation, open-order synchronization, fill history,
position repair, and the local order state machine. Phase 7 deliberately built
only the durable anchors it will need.
