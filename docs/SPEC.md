# autotrader - Project Specification (v0.1)

**Status:** Phase 0 - repository foundation only.
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
Phase 0  Repository Foundation          <- current
Phase 1  Historical Market Data
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
  - `data/raw/` - as fetched from the provider, unmodified.
  - `data/processed/` - validated / normalized bars used by backtests.
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

### Phase 0 - Repository Foundation (current)

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

### Phase 1 - Historical Market Data (next, not started)

Fetch 15-minute historical bars for the V0.1 universe from Alpaca's data API
and persist them to Parquet under `data/raw/`. Read-only market data access;
no trading endpoints, no order submission.

### Later phases

Each later phase is specified when it is reached. A phase may not begin until
the previous phase's acceptance criteria are met and committed.
