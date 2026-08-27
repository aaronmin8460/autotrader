# autotrader

A personal, single-user automated trading system for US equities, built to run
as a local Python CLI process against an Alpaca **paper** account.

This is an engineering project. It makes **no claim of profitability**, and it
is not investment advice.

## Status: Phase 0 - foundation only

There is **no live trading in this repository, and none is planned within the
current milestone.** As of Phase 0 there is also no paper trading: no market
data is downloaded, no broker API is called, no credentials are required, and
no strategy, backtest, or risk logic exists. The repository currently contains
the package skeleton, the CLI entry point, the specification, and tests that
verify those load.

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
| Trading state | SQLite (later phase) |
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

Configuration is not needed yet. `.env.example` documents the environment
variables a later phase will use; copy it to `.env` when that phase arrives.
`.env` is git-ignored and must never be committed.

## Usage

Show CLI help:

```bash
python -m autotrader.cli --help
```

Show the version:

```bash
python -m autotrader.cli version
```

The `autotrader` console script is installed as an equivalent entry point.
There are no data, strategy, backtest, or trading commands yet.

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
src/autotrader/     package (data, strategies, backtest, risk, cli - all empty stubs)
tests/              foundation tests
data/raw/           downloaded market data (git-ignored)
data/processed/     validated market data (git-ignored)
docs/SPEC.md        authoritative scope specification
```
