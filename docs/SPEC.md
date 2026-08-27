# autotrader - Project Specification (v0.1)

**Status:** Phase 1 - historical market data.
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
| Operational trading state | SQLite (later phase; not now) |
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
Phase 1  Historical Market Data         <- current
Phase 2  Data Validation
Phase 3  Strategy
Phase 4  Backtesting
Phase 5  Risk Engine
Phase 6  SQLite Trading State
Phase 7  Alpaca Paper Trading
Phase 8  Reconciliation / Crash Recovery
Phase 9  Monitoring
Phase 10 Deployment
```

---

## 5. Storage policy

- **Historical market data:** Parquet files under `data/`.
  - `data/raw/` - as fetched from the provider, normalized only to the
    canonical schema in section 8 (Phase 1).
  - `data/processed/` - validated bars used by backtests (Phase 2).
  - Market data is **never committed**. The directories are tracked via
    `.gitkeep`; their contents are ignored.
- **Operational trading state** (orders, positions, fills, run journal):
  a local SQLite database, introduced in Phase 6. Database files are ignored
  by git.
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

### Phase 1 - Historical Market Data (current)

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

### Phase 3 - Strategy

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

### Later phases

Each later phase is specified when it is reached. A phase may not begin until
the previous phase's acceptance criteria are met and committed.
