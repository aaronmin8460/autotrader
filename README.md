# autotrader

A personal, single-user automated trading system for US equities, built to run
as a local Python CLI process against an Alpaca **paper** account.

This is an engineering project. It makes **no claim of profitability**, and it
is not investment advice.

## Status: Phase 2 - data validation

There is **no trading in this repository, and none is planned within the
current milestone** - no live trading and no paper trading. No order is ever
submitted, and no Alpaca trading client is constructed anywhere in the code.

What exists today are two read-only capabilities: downloading historical
15-minute US-equity bars from Alpaca's IEX feed and storing them locally as
Parquet, and validating a stored dataset against that canonical schema.
Validation never downloads, modifies, or repairs data. Strategies,
backtesting, and risk logic belong to later phases and are not implemented.

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
There are no strategy, backtest, or trading commands.

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
src/autotrader/cli/         Typer CLI (version, download, validate)
src/autotrader/strategies/  empty stub (Phase 3)
src/autotrader/backtest/    empty stub (Phase 4)
src/autotrader/risk/        empty stub (Phase 5)
tests/                      offline tests; no test contacts the network
data/raw/                   downloaded market data (git-ignored)
data/processed/             validated market data (git-ignored)
docs/SPEC.md                authoritative scope specification
```
