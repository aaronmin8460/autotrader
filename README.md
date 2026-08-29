# autotrader

A personal, single-user automated trading system built to run as a local Python
CLI process against an Alpaca **paper** account. It operates two products on
that one account: **crypto spot**, 24/7, and **US equities**, regular market
hours only.

This is an engineering project. It makes **no claim of profitability**, and it
is not investment advice.

## Status: combined integration complete, awaiting the combined paper smoke

**Two products, two runtimes, one Alpaca paper account.** Crypto (BTC/USD,
ETH/USD, 24/7) and equities (ten symbols, regular US market hours) run as two
separate processes against one account and one SQLite database. They keep their
own schedules, their own order semantics and their own process locks, and they
share the things that belong to the account rather than to either of them:

| Shared | Not shared |
| --- | --- |
| Account safety - one halt, either service can raise it | Runtime process lock - one per service, so both run at once |
| Total exposure under the one 30% account cap | Order semantics - fractional GTC crypto, whole-share DAY equity |
| The order-decision path, serialized by an account execution lock | Schedule - 24/7 UTC boundaries against regular-session bars |
| The UTC-day risk baseline | Bar checkpoints - per symbol, restart safety stays independent |
| The API budget, across both processes | Market data - crypto feed against IEX |
| Reconciliation, over all twelve tracked symbols | |

**The rule that makes them one system:** `UNKNOWN FROM ANY ASSET = NO NEW
ORDERS FROM ANY ASSET`. An ambiguous submission raised by the equity service
halts the crypto service too - durably, across processes and across restarts -
and only a full-universe reconciliation can clear it. Not time passing, not a
restart, and not either runtime deciding locally that it feels fine.

**No paper order has been observed end to end, and the equity book has never
submitted one at all.** The combined system is green offline and has been
exercised read-only against the real paper account - reconciliation, both
runtimes with `--once --observe-only`, and the dashboard against real data -
with **zero** orders submitted. The combined paper smoke gate is the next step,
and nothing is deployed before it.

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
operational-state database at schema v6, a single deliberately awkward
paper-order command, a full-universe crash-recovery reconciliation command, the
24/7 crypto runtime and the regular-session equity runtime that drive it on a
schedule, and a read-only operations dashboard. Validation never downloads or
repairs data; the strategy emits signals only; the backtester is local
arithmetic; the risk engine is a pure calculator that persists nothing; and the
database stores records without deciding anything.

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
quantity sent is never more than the risk-approved one. An order worth less than
Alpaca's $10 USD minimum cost basis is refused locally, before any broker
request exists, and is never enlarged to clear it. The order intent and
its `client_order_id` are committed to SQLite *before* the broker is called.

**Reconciliation resolves state; it never invents a trade.** `autotrader
reconcile` reads the paper broker and repairs local SQLite from it. An
`UNKNOWN` submission outcome is settled by asking the broker about the *same*
`client_order_id` - never by sending a second order. A stale intent the broker
confirms it never received is closed off rather than executed after a restart.
A position mismatch is corrected in the database, never by trading. The
command reports one answer a runtime can act on, `safe_to_trade`, which is
false for anything ambiguous.

**The 24/7 runner reconciles before it is allowed to trade.** It loops on
completed 15-minute UTC boundaries, observes, and records; broker submission is
gated on the startup reconciliation answer, on the paper environment gate, and
on a runtime `PAPER` confirmation - all three, on every start. See
[The 24/7 runtime](#the-247-runtime).

## Scope summary

| | |
| --- | --- |
| Asset classes | Crypto spot, and US equities (cash) |
| Broker | Alpaca only - **one** paper account for both books |
| Execution | Alpaca paper trading only - live is unreachable |
| Crypto universe | BTC/USD, ETH/USD - 24/7, GTC market, fractional |
| Equity universe | SPY QQQ IWM AAPL MSFT NVDA AMZN GOOGL META TSLA - regular session, DAY market, whole shares |
| Quote currency | USD only |
| Timeframe | 15-minute bars, both products |
| Processes | Two runtimes, two runtime locks, one shared account execution lock |
| Direction | Long only |
| Leverage / shorting | None |
| Research strategy | EMA 20 / EMA 50 crossover, identical parameters across all twelve symbols (engineering validation only) |
| Risk | 5% per symbol, 30% total account exposure, 2% UTC daily loss. **No per-book cap.** |
| Historical storage | Parquet |
| Operational state | SQLite, local file, schema v6 |
| Startup authority | Full-universe reconciliation `safe_to_trade` - no green result, no order |
| Account authority | One durable halt: UNKNOWN from any asset stops every asset |
| Duplicate protection | OS process locks + durable per-symbol bar checkpoints |
| Safety preference | At-most-once: miss a trade rather than duplicate one |
| Interface | Python CLI, local processes, plus a read-only local dashboard |

Out of scope: live trading, options, futures, forex, perpetual futures, non-USD
quote currencies, equities outside the ten named above, shorting, leverage,
margin, multiple brokers, ML/LLM signal generation, mobile frontends, cloud
deployment - and **per-book allocation limits**, which have not been approved
and are not implemented.

**[docs/SPEC.md](docs/SPEC.md) is the authoritative scope document.** Read it
before extending this project; it takes precedence over any chat history.

### The archived equity milestone

Equity V0.1 - SPY, QQQ, AAPL, MSFT, NVDA on Alpaca's IEX feed, whole shares,
DAY market orders - is tagged:

```bash
git show equity-v0.1-phase7
```

It is a **read-only reference**. Equity V0.2 was not reset to it, not developed
from it, and not cherry-picked out of it: current main is authoritative, and
the new product is built on the current Decimal quantities, schema, risk
engine, execution ordering and reconciliation. What was reused is semantics
that were already right - whole shares, `TimeInForce.DAY`, regular hours, the
IEX feed, and reading the broker's clock rather than assuming a session.

The absence rules now scope to the **crypto** product and are still enforced:
no crypto runtime module names an equity symbol,
`StockHistoricalDataClient`, `StockLatestTradeRequest`, `StockBarsRequest`,
`TimeInForce.DAY`, `get_clock`, or `America/New_York`, and the test suite
asserts each absence against executable source with docstrings and comments
stripped.

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

### Running the combined system

Two processes against one account and one database. Reconcile first, then start
each service; the order between the two services does not matter, because they
take different runtime locks and serialize only where they must.

```bash
python -m autotrader.cli reconcile
```

```bash
python -m autotrader.cli crypto-run --confirm-paper-runtime PAPER
```

```bash
python -m autotrader.cli equity-run --confirm-paper-runtime PAPER
```

Each runtime reconciles the whole account itself at startup, so the standalone
`reconcile` above is diagnostics rather than a prerequisite. Drop
`--confirm-paper-runtime` - or add `--observe-only` - and the process runs the
full loop and submits nothing.

If either service records an ambiguous submission, **both stop submitting.**
The halt is durable, so restarting does not clear it; a full-universe
`reconcile` that resolves the ambiguity does.

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
"did we receive the newest completed bar?" is a runtime question, answered by
[the 24/7 runner](#the-247-runtime) rather than by structural validation. There is no
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

## Quant research infrastructure

The `backtest` command above answers "does the pipeline account correctly?".
This answers a harder question: **would this result have survived contact with
reality, and is it evidence about anything?**

It exists because Decision Engines V2/V3/V4/V5 are coming, and an unaudited
backtest is not evidence about any of them.

```bash
# Replay one engine over a stored dataset, with full metrics.
python -m autotrader.cli research replay-dataset "$AUTOTRADER_QA_DATASETS/BTC_USD_15m.parquet"

# Audit a study configuration for look-ahead and contamination.
python -m autotrader.cli research audit "$AUTOTRADER_QA_DATASETS/BTC_USD_15m.parquet" \
    --train-bars 500 --test-bars 200 --embargo-bars 60 --holdout-bars 400

# Sweep parameters under walk-forward validation, recording every experiment.
python -m autotrader.cli research sweep "$AUTOTRADER_QA_DATASETS/BTC_USD_15m.parquet" \
    --study my-study --fast-periods 10,20,30 --slow-periods 50,80,120
```

**Research only.** Nothing in `autotrader.research` submits an order,
constructs a broker client, reads a credential, opens a socket, or touches the
operational database - and that is enforced by tests that scan every module in
the package, not by convention. No research command accepts a confirmation
token, because a research command that had one would be a research command that
could do something.

### How a Decision Engine plugs in

An engine supplies four things and nothing else:

```python
class DecisionEngine(Protocol):
    name: str            # stable identifier, used in reports
    version: str         # so V2 and V3 results never merge
    parameters: Mapping  # recorded with every result
    warmup_bars: int     # bars needed before output means anything

    def generate(self, bars: pd.DataFrame) -> Sequence[ResearchSignal]: ...
```

It is never handed cash, a position, an account, or a broker. Sizing and
execution belong to the simulator, exactly as in production they belong to the
risk engine and the execution boundary. An engine that cannot see the portfolio
cannot accidentally be evaluated on one it would not have had.

Every other module - `replay`, `metrics`, `splits`, `walkforward`,
`experiments`, `trades`, `leakage` - consumes that protocol and **names no
strategy**. A test asserts they contain no reference to the EMA crossover. So a
future V2 engine becomes evaluable by writing an adapter, and its production
code is not rewritten to suit research.

`EmaCrossEngine` is the worked example: it adapts the existing crossover by
calling it and computes no indicator of its own. `ParametricEmaCross` is a
research-only generalization over the periods, because a sweep needs something
to vary and the production periods are deliberately fixed - and a test pins
that at 20/50 the two emit identical signals, so a sweep explores the strategy
production actually runs. `BuyAndHoldEngine` is the benchmark, because most of
what a long-only strategy earns in a rising sample is the sample.

### The simulator reproduces the production backtester exactly

With slippage at zero and the C4 taker fee, `autotrader.research.replay` and
`autotrader.backtest` agree on every fill, every fee, every point of the equity
curve and the final cash **to the last decimal place**, across four independent
price fixtures. That is a test, not a claim. It is what makes the research
simulator's additions - the engine seam, slippage, exposure and turnover
accounting, portfolio aggregation - additions rather than a second, subtly
different backtester.

Costs are named and carried into every result: `crypto-taker` (0.25% fee, 5bp
adverse slippage), `equity-marketable` (no commission, 2bp slippage),
`frictionless` (an upper bound, on purpose and under a name), and `stress`.
Slippage is adverse by construction - a BUY fills above the reference price and
a SELL below it, and there is no setting that makes trading pay you.

### Leakage protection

Look-ahead never announces itself: a leaking backtest looks like a very good
backtest. So it is made *checkable* rather than forbidden.

**Perturbation is the interesting part.** To ask "does this engine see the
future?", the bars *after* some index are changed and the engine is re-asked.
Every signal at or before that index must be identical. A causal engine cannot
notice; a leaking one changes. This catches a negative shift, a centered
rolling window, a normalization fitted over the whole series, a backfill, and a
forward-looking label - without knowing which was written. No static scan can
do that.

`tests/test_research_leakage.py` injects each of those defects deliberately and
proves the auditor catches it, and pairs every one with a legitimate construct
that must **not** be flagged. A detector tested only against leaks can pass by
rejecting everything.

Alongside that, the structural checks: unordered timestamps (a shuffled split
is contiguous in position and scrambled in time), duplicated instants, a test
window before its training data, train/test overlap, an embargo shorter than
the feature lookback, no embargo declared at all, a bar scored by two windows,
a selection window reaching into the holdout, a window too short for the
engine's warm-up, and a final bar that had not finished forming.

A clean report is strong evidence, **not a proof**: perturbation samples probe
points, so `LeakageReport` carries its probe count. A test pins the honest
limit - an engine that emits no signal at all cannot be shown to leak.

### Walk-forward

There is **no shuffle parameter anywhere in the split API** - not defaulted
off, not present. A knob that must never be turned should not exist, and a test
greps for it.

Windows are contiguous, strictly ordered, and rolling or anchored by explicit
choice, because which one to use is a modelling claim rather than a default.
Test windows do not overlap by default, so an average over windows is an
average over independent samples. An embargo leaves a real gap between train
and test - without it, a 50-bar indicator at the test window's first bar still
reads 50 training bars, which "the windows do not overlap" does not prevent.

Each window is replayed from flat with its own capital, so one lucky early
window cannot compound through the study and be reported as consistency. The
engine's warm-up is drawn from bars strictly before the window and then
excluded from scoring, so a window is never credited with bars belonging to the
window before it.

Results are reported as a **distribution** - median, mean, spread, and the
fraction of windows that were positive. A mean Sharpe over eight windows where
seven are negative is not a strategy.

**The final holdout is carved off before any window is generated**, and is
evaluated once, for one already-selected candidate. There is deliberately no
way to spell "evaluate every candidate on the holdout and keep the best".

### Metrics

Total return, annualized return with its **sample length printed beside it**,
realized and unrealized PnL kept apart, Sharpe, Sortino, annualized volatility,
max drawdown and its duration, win rate, trade count, turnover, exposure,
average trade and hold, profit factor, and cost drag.

**An undefined metric is `None`, never `0.0`.** A Sharpe ratio over a flat
curve, a win rate over zero trades, a profit factor with no losing trade - all
`None`, printed as `n/a`. Zero would survive into a leaderboard and get a
parameter set selected on the strength of it.

Annualization is tied to an explicit bar clock: a 15-minute crypto bar arrives
35,040 times a year and a 15-minute equity bar about 6,552. Annualizing one
with the other's constant overstates everything fivefold.

Win rate and raw PnL are reported *alongside* risk-adjusted return, drawdown,
exposure and turnover rather than instead of them. Nine small wins and one
large loss is a 90% win rate and a losing strategy; both numbers are present so
that is visible rather than discoverable.

### Sweeps, and what a study leaves behind

The grid ceiling is **256 experiments**, and an oversized grid is refused
rather than truncated - truncation would silently explore a corner of the space
and report it as a search. The bound is checked before any constraint, so a
filter cannot smuggle a wide search past it.

Every experiment writes a record carrying its parameter set, code version and
dirty flag, dataset digest and interval, train/test interval, cost model, seed,
and metrics. A number that cannot be traced back to its inputs is not evidence.

Selection names every window that informed it and **how many candidates were
compared**, because a best-of-200 score is a different claim from a best-of-3.
The default objective refuses to rank a candidate that traded too little to
measure or was profitable in half its windows or fewer - and "no candidate was
scoreable" is reported as the result it is, rather than resolved by picking the
least bad.

```
$AUTOTRADER_QA_REPORTS/research/<study>/<run-id>/
    manifest.json       parameter space, dataset fingerprint, costs, versions
    splits.json         every walk-forward window, and the withheld holdout
    experiments.jsonl   one line per experiment: parameters + full metrics
    selection.json      what was chosen, on what basis, against which windows
```

**Storage is external and refuses to be otherwise.** An unset
`AUTOTRADER_QA_REPORTS` is an error rather than a fallback to a local
directory, and a path resolving inside the repository is refused - the
repository root is derived from the package's own location, so the check cannot
be defeated by running from elsewhere.

### Stated limitations

**Portfolio replay allocates sleeves; it does not model shared cash.** Capital
is partitioned across symbols and each is replayed independently, then the
curves are aggregated on a union timestamp index with each sleeve
forward-filled. Two symbols never compete for the same dollar. Modelling the
production runtime's sequential sizing against one shared account means
modelling the account execution lock and the combined exposure ceiling, which
are runtime properties - and a research replay that pretended to would produce
a number that looks like a paper-trading forecast and is not.

**Research evaluates; it may not activate.** No sweep result changes what
either runtime does, and no parameter set a study selects is adopted. Adopting
one would be a separate, documented scope change. A sweep producing a winner is
not that decision, and the winner is not a recommendation.

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

Twelve tables exist:

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
| `reconciliation_runs` | What one finished reconciliation pass concluded (v4) |
| `reconciliation_events` | Which order or position it repaired, observed, or could not settle (v4) |
| `runtime_checkpoints` | The newest completed bar the runtime durably claimed, per symbol (v5) |

**There is still no fill persistence.** No `fills`, `executions`, or
`broker_accounts` table exists. Order-level `filled_quantity` is what
reconciliation actually settles, so a fill-level history would be a shape
guessed at rather than needed.

### Schema v4: the reconciliation vocabulary

v4 does two things. It adds the two audit tables above, and it widens the
`order_intents` status CHECK by one value, `CONFIRMED_NOT_SUBMITTED` - the
state of an intent whose absence at the broker has been *confirmed*, which is
neither `CREATED` nor `REJECTED`. Widening a CHECK requires a table rebuild in
SQLite, so `order_intents` is renamed aside, recreated from the same literal a
fresh database uses, copied across column by column, and the old copy dropped.
No row's meaning changes: the rebuild only makes one more status storable, and
writes no row into it. The two new tables arrive empty, because backfilling an
audit trail that never happened would be a fabrication.

### Schema v5: the durable bar checkpoint

v5 adds one table, `runtime_checkpoints`, and changes nothing else. One row per
symbol, holding the newest completed bar that runtime has durably claimed:

| Column | Holds |
| --- | --- |
| `symbol` | `BTC/USD` or `ETH/USD`, PRIMARY KEY |
| `last_processed_bar_timestamp` | The newest completed bar start acted on |
| `updated_at` | When the claim was written |

The claim is **monotonic in SQL**, not merely in the caller: an upsert with an
older timestamp updates nothing, so an out-of-order write cannot re-open a bar
something already acted on. See [One bar, one
decision](#one-bar-one-decision---including-across-a-restart) for why the claim
is committed before the broker is ever called.

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

### Schema migration (v1 -> v2 -> v3 -> v4 -> v5)

A new database is created directly at v5. An older one is upgraded through an
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

v3 -> v4 uses the same rebuild machinery for a smaller reason: only
`order_intents` is rebuilt, and only to widen a CHECK constraint. Every column
is copied verbatim - no value is converted and no row changes meaning - and the
two reconciliation tables are created empty alongside it. The byte-identity
test covers this path too, from v1, v2, and v3 alike.

**v4 -> v5 is purely additive** - one new table and nothing else. A test asserts
every pre-existing schema object comes through byte-identical, and that the
reconciliation runs and events, order intents, fractional quantities and daily
risk baselines a v4 database already held are all still there afterwards.

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
        -> refuse below the broker's effective minimum quantity
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

### The $10 USD minimum order notional

That metadata is necessary and **not sufficient**. Alpaca also enforces a
minimum **cost basis of $10** on a USD-quoted crypto order, and does not report
it: `min_order_size` still carries an older ~$1-notional floor - 0.000012417 BTC,
about $1 at an $78,000 BTC. An order can clear every published constraint and
still come back as

```
cost basis must be >= minimal amount of order 10. No order was created.
```

So the floor is written down once, as `USD_MINIMUM_ORDER_NOTIONAL`, and the
effective minimum for a USD pair combines both sources:

```
effective_min_qty = max(
    asset.min_order_size,                                  # live broker metadata
    ceil_to_increment(USD_MINIMUM_ORDER_NOTIONAL / price)  # the $10 cost basis
)
```

- **`Decimal` throughout.** A binary float would make a threshold that decides
  whether a real order is sent depend on a rounding artefact.
- **The threshold rounds up** to the next whole `min_trade_increment`; rounding
  it down would produce a floor itself worth less than $10. The *submitted*
  quantity still rounds **down**, and never exceeds
  `RiskDecision.approved_quantity`. These are different operations on different
  values.
- **An undersized order is refused locally**, before any broker request exists:
  zero `submit_order` calls, no intent persisted, and a known outcome rather
  than an `UNKNOWN` one.
- **The quantity is never enlarged** to clear the floor. Doing so would send
  more than risk approved, so the caller is told to request more instead.
- **No trustworthy price, no submission.** The threshold cannot be computed
  without one, and that fails closed.

The floor applies to **both sides** - Alpaca states the USD-pair minimum without
a side distinction and describes the cost-basis check as covering buy and sell
orders alike. A position worth less than $10 therefore cannot be closed until it
recovers; that is Alpaca's constraint rather than this system's, and it is why
an opening order should be sized with room above the floor (about $12-$15)
rather than at it. The rule is scoped to USD-quoted pairs: Alpaca documents a
separate `0.000000002` floor for its BTC, ETH, and USDT pairs.

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
| Reply arrives but cannot be read or stored | Intent `UNKNOWN`; **never retried** - a request went out |
| The order intent is not durably committed | Refused before any broker call |

**An ambiguous outcome is never retried.** A timeout after `submit_order` could
mean the broker accepted the order or never saw it. Re-sending it risks a
duplicate position, so the intent is marked `UNKNOWN`, an audit event is
written, and the attempt stops. The `client_order_id` is kept so
[reconciliation](#reconciliation-and-crash-recovery) can ask the broker about
that exact key. The CLI exits `2` for this case specifically. The SDK's own
internal retry of `429`/`504` responses is switched off on the trading client
for the same reason.

**Ambiguity starts at the call, not at the exception.** Once `submit_order` has
been entered a request has gone out, so a reply that comes back and cannot be
parsed, or cannot be written to the database, is `UNKNOWN` for exactly the same
reason a timeout is. Treating either as an ordinary failure would let the caller
trade on the next boundary over an order it never recorded.

**No durable intent, no broker submission.** The recovery anchor is a
`client_order_id` on disk *before* the request goes out; an order placed without
one is an order no restart could find. `submit_order_intent` refuses if its
connection is inside an open transaction, because a write inside one is
invisible to every other process and is rolled back by a crash. The bar
checkpoint is guarded the same way, for the same reason.

### Accepted is not filled

A stored broker snapshot proves the broker **accepted** an order. Nothing
infers a position from that: the local `positions` table is only written from a
position actually observed at the broker, and a successful submission never
increments it. Reconciliation keeps the same rule.

### Not implemented

No fill history, no streaming or websockets, no order replacement or
cancellation, and no live trading. Submission is one attempt; settling what
it produced is [reconciliation](#reconciliation-and-crash-recovery)'s job,
and scheduling it is [the runtime](#the-247-runtime)'s.

## Reconciliation and crash recovery

```bash
python -m autotrader.cli reconcile
```

`autotrader.reconciliation` makes local SQLite state reflect **verified broker
truth** after a crash, a restart, or a submission whose outcome was never
knowable. It is the only part of the system that rewrites local rows from what
a broker says, and it is read-only towards the broker.

### The authority hierarchy

| | |
| --- | --- |
| **Broker** | Truth for orders, fills, and positions |
| **Local SQLite** | Durable intent, audit trail, last-known snapshot |

Where they disagree, the snapshot is rewritten. Never the other way around.

### Reconciliation never invents a trade

This is the property the whole phase is built around. No `UNKNOWN` intent is
resubmitted, no `client_order_id` is regenerated, no submission is retried, no
replacement order is created, and no offsetting order is placed to correct a
position mismatch. The package imports nothing that could place an order, and
source-level tests assert each forbidden identifier is absent from its
executable code. In the test suite the fake broker's submit call does not merely
count invocations - it raises.

### The startup question

One result object, one question:

```python
result = reconcile_paper_state(connection)
if result.safe_to_trade:
    ...
```

| Status | Meaning | `safe_to_trade` | Exit code |
| --- | --- | --- | --- |
| `CLEAN` | Local state already agreed with the broker | true | 0 |
| `REPAIRED` | Differences were resolved from verified broker truth | true | 0 |
| `UNRESOLVED` | The pass ran; something stayed ambiguous | false | 2 |
| `FAILED` | The pass could not complete | false | 1 |

`safe_to_trade` is **derived** from the status rather than stored beside it, so
a result that says `UNRESOLVED` and "safe" cannot be constructed. A pass that
never finished returns nothing at all, which is not permission.

### Resolving an UNKNOWN intent

An `UNKNOWN` intent means a submission ended without a knowable outcome - the
order may or may not exist. The recovery anchor is the `client_order_id`
committed *before* the request went out and never regenerated:

| The broker says | What happens |
| --- | --- |
| It has that order | Its snapshot is recorded as-is. **Nothing is submitted.** |
| It definitively has no such order, on more than one read | The intent becomes `CONFIRMED_NOT_SUBMITTED` - terminal, never sent |
| The lookup times out, 5xx's, or cannot be read | Nothing changes; the pass is `UNRESOLVED` and startup is blocked |

**One not-found is never enough.** A single `404` could be a lookup that
overtook a submission still in flight, so the read is repeated a small fixed
number of times with a short pause between, and every read must agree. It is a
bounded confirmation, not a poll: no growing backoff, and no loop that waits
for the answer it wants.

The same handling covers a `CREATED` intent (the process died before
submitting) and a `SUBMITTING` one (it died mid-call). A stale decision from
before a crash is closed off rather than executed on the next run.

### Order snapshot repair

For an intent the broker does have, the snapshot is copied across: broker order
id, client order id, symbol, side, quantity, filled quantity, filled average
price when given, status, submitted time, and filled time. Nothing is inferred.

- A **partial fill stays partial** - 0.0004 filled of 0.001 ordered is stored
  as exactly that, and a partially filled order is re-read on the next pass
  because it can still fill.
- A **missing fill price is not invented**; absent stays absent.
- **Accepted is still not filled**, and a submitted order never conjures a
  position.
- An order returned under this key that names a *different* key, market, or
  side is not evidence about this intent: it is reported and nothing is written.
- A broker order this system already recorded, which the broker now denies,
  leaves the stored snapshot untouched and blocks trading. Recorded evidence is
  not deleted because a later read disagreed with it.

The broker's own `updated_at` is carried into the audit detail rather than into
`broker_orders.updated_at`, which already means "when this snapshot was
refreshed". One column, one clock.

### Positions come only from the broker

BTC/USD and ETH/USD are reconciled independently. A position the broker holds
and local state does not is written in; a local position the broker no longer
holds goes to zero. Quantities are exact `Decimal` values throughout. Nothing
is derived from `order_intents`, and no order is placed to make the two agree.
A short position at the broker fails the whole pass - this system is long only
and cannot reason about one - and the `positions` column itself refuses a
negative quantity. A broker position outside the traded universe is recorded as
an observation and left alone.

### Failing closed

| Situation | Result |
| --- | --- |
| The client cannot be **proven** to reach Alpaca paper | `FAILED`, before any local write |
| Authentication failure, or an unreadable account | `FAILED` |
| The position list cannot be read | `FAILED` |
| A short position at the broker | `FAILED` |
| An order lookup times out or cannot be read | That item `UNRESOLVED`; the pass continues |
| A malformed broker order | That item `UNRESOLVED` |
| The audit record cannot be written | `FAILED` |

Proving paper is a precondition, not an afterthought: a process about to
rewrite local state from what a broker says checks *which* broker first, and an
environment that cannot be read is refused exactly like a live one.

An unresolved item does not abort the pass. A runtime is better served by every
problem than by the first one.

### Audit, idempotency, and dry run

Every finished pass writes one `reconciliation_runs` row - when it ran, what it
concluded, whether trading was allowed afterwards, and how much it checked -
plus a `reconciliation_events` row for each item it repaired, observed, or
could not settle. Clean items write nothing: an audit table where most rows say
"no change" stops being readable. A summary also lands in `system_events`.

Repairs commit as they are made and the audit record commits at the end, so a
crash mid-pass leaves durable repairs and no run row - work was done, nothing
was concluded - and the next pass reconciles from broker truth again.
Reconciliation is idempotent: a second run after a repair reports `CLEAN`.

`--dry-run` reports exactly the same findings and reconciles **nothing** into
the database - no repair, no run row, no event. Opening the database still
applies any pending schema migration, as every command here does; that is a
structural upgrade, not a reconciliation result.

### Run by hand, and run automatically

`autotrader reconcile` remains available for diagnostics and manual repair, and
it runs once, when asked, and returns. It is **not** a step an operator has to
remember before every daemon start: [`crypto-run`](#the-247-runtime) invokes
this same pass itself at startup. Both callers share one implementation - there
is no second copy of reconciliation anywhere in the repository.

### Not implemented here

No order replacement or cancellation, no fill history, no automatic retry, and
no loop, scheduler, or heartbeat of its own.

## The 24/7 runtime

`crypto-run` is the long-running process. It adds no trading logic: the data,
validation, strategy, risk, state, reconciliation and paper-execution stages
are the existing ones, and it is the schedule and the safety envelope around
them.

Run one completed-bar cycle and exit - the intended way to check the runtime by
hand, and safe with no gate open:

```bash
python -m autotrader.cli crypto-run --once --observe-only
```

Run it continuously, with paper submission enabled:

```bash
AUTOTRADER_PAPER_TRADING_ENABLED=true python -m autotrader.cli crypto-run --confirm-paper-runtime PAPER
```

You do **not** have to run `reconcile` first. Every start reconciles on its
own; the standalone command remains for diagnostics and manual repair.

### Startup: reconcile, then decide

Every `crypto-run` start does this, in this order, before it observes anything:

```
1. acquire the OS single-instance runtime lock
2. open the operational SQLite database
3. apply any pending schema migration
4. construct/verify the Alpaca PAPER read boundary
5. run reconcile_paper_state(connection)
6. evaluate the result
7. CLEAN or REPAIRED -> safe to trade
   UNRESOLVED or FAILED -> not safe to trade
8. start runtime observation
9. submit only if safe AND the env gate is open AND PAPER was confirmed
```

Step 5 is the real Phase 8 pass, not a placeholder, and **nothing bypasses it**.
There is no environment variable and no flag combination that reaches the broker
while reconciliation says no.

A start that is not safe does not crash and does not continue silently: it says

```
RECONCILIATION NOT SAFE - TRADING DISABLED
```

on the banner and in the logs, reports the reconciliation status in the
heartbeat, keeps observing, and exits `0`. `--observe-only` still reconciles -
knowing whether local state survived is useful either way - and still cannot
submit, because it constructs no execution path at all.

Each start reconciles for itself. A previous run's green result describes the
world at the moment it was produced, and is never carried across a restart.

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

### One bar, one decision - including across a restart

BTC/USD is processed to completion before ETH/USD is looked at, so two signals
landing on the same boundary cannot size themselves against the same stale cash
figure. Only the **newest completed bar** may cause an action: older crossovers
in the lookback exist to establish EMA state and are never replayed.

A per-symbol checkpoint in `runtime_checkpoints` (schema v5) is **committed
before** the bar can reach the strategy, so the claim outlives the process that
made it. A restarted runner sees its predecessor's claim and skips the bar.

The lock and the checkpoint solve different problems and both are kept: the
`fcntl` lock stops two runners existing at once, the checkpoint stops one
runner - or its replacement after a crash - acting twice on one bar.

#### Safety preference: miss a trade rather than duplicate a trade

The chain is:

```
completed bar -> durable claim (committed) -> signal -> risk
              -> OrderIntent committed -> broker submission
```

| Crash point | After restart |
| --- | --- |
| before the durable claim | the bar may be processed |
| after the claim, before the intent | the bar is **skipped** - that trade is missed, permanently |
| after the intent, before submit | reconciliation resolves `CREATED` / no broker order |
| during submit | reconciliation resolves `UNKNOWN` by the persisted `client_order_id` |
| after submit | reconciliation repairs from broker truth |

This is **at-most-once**, not exactly-once, and it does not pretend otherwise.
Losing one fifteen-minute crossover is recoverable; a duplicate position is
not.

### Three gates, all closed by default

Unattended paper execution requires **all** of:

1. `AUTOTRADER_PAPER_TRADING_ENABLED=true` in the environment - the same C7
   gate, not bypassed;
2. `--confirm-paper-runtime PAPER`, which authorizes *this process* for its
   lifetime. A daemon cannot have a token typed every fifteen minutes, so the
   confirmation moved to process start rather than being removed;
3. startup reconciliation reporting `safe_to_trade` - `CLEAN` or `REPAIRED`.

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
resolves it *inside that cycle*, and nothing else is submitted on top of it.
Recovery is a **new process**, which runs startup reconciliation before it may
trade again - the same pass, at the only moment allowed to reopen the gate.

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

## Equity V0.2

A second product on the same Alpaca paper account, developed on the same
architecture and gated the same way. It is not a mode of the crypto system, and
the crypto system is not a mode of it.

| | |
| --- | --- |
| Asset class | US equities (cash) |
| Universe | SPY, QQQ, IWM, AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA |
| Timeframe | 15-minute bars |
| Market data | Alpaca stock, **IEX** feed |
| Session | **US regular market hours only** |
| Direction | Long only, no shorting, no leverage |
| Quantities | **Whole shares**, floored, never rounded up |
| Order type | MARKET, `TimeInForce.DAY`, no extended hours |
| Strategy | The same EMA 20 / EMA 50 crossover, same parameters |
| Execution | Alpaca paper only - live is unreachable |
| Status | Component-complete; **no stock paper order has been submitted** |

### The ten symbols, and only the ten

The universe is exactly ten, and the tuple order is the processing order: SPY,
QQQ, IWM, AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA. One symbol is finished -
risk sized against the account as it stands, order submitted or refused -
before the next is looked at, so ten signals landing on the same bar can never
size themselves against the same stale cash and exposure figures. Adding an
eleventh is a scope change requiring an edit to `docs/SPEC.md`; it is not a
configuration value.

### The market session is read, never assumed

`Mon-Fri 09:30-16:00` is wrong on roughly a dozen days a year, so it appears
nowhere in the code. Session times come from Alpaca's own calendar endpoint:
holidays are simply absent from it, and a half day reports its real 13:00
close.

Alpaca returns those times as **naive Eastern wall-clock** strings.
`America/New_York` is attached in exactly one function, which converts to UTC
immediately; everything after that point - every comparison, bar start, wake
time and checkpoint - is UTC.

A **regular-session bar** is a 15-minute boundary whose whole interval lies
inside the session. An ordinary day has twenty-six (09:30 through 15:45); a
13:00 early close has fourteen (09:30 through 12:45). The IEX feed serves
pre-market and post-market candles in the same response, and they are filtered
out before the strategy sees anything.

**A cycle outside the session does nothing at all** - no fetch, no strategy, no
checkpoint, no order, no provider call. One consequence is worth stating
plainly: the bar that closes *at* the bell is never acted on, because its cycle
would fall outside the session. On an ordinary day the actionable bars are
09:30 through 15:30, acted on at 09:45 through 15:45.

### Whole shares, rounded down

Alpaca reports these symbols as fractionable and Equity V0.2 does not use it. A
whole-share order needs no notional handling, no fractional-order restriction,
and no per-symbol increment metadata - Alpaca reports `min_order_size` and
`min_trade_increment` as `null` for equities anyway.

The risk engine's approved quantity is floored to an integer number of shares
at the execution boundary, so the broker is never asked for more than risk
approved, and a quantity that floors below one share is refused rather than
rounded up to one. The limitation that follows is stated rather than hidden: a
whole-share exit cannot fully close a *fractional* position. Nothing in this
branch can create one, and reconciliation reports the remainder rather than
trading it away.

### Four gates, all closed by default

Unattended equity paper execution requires **all** of:

1. `AUTOTRADER_PAPER_TRADING_ENABLED=true` in the environment
2. `--confirm-paper-runtime PAPER` on the command line
3. startup reconciliation reporting that trading is safe
4. the regular market session being open at the moment of submission

None substitutes for another, and the fourth is checked twice: the runtime
refuses to run a cycle outside the session, and the execution boundary refuses
again against the **broker's own clock** immediately before submitting. The
second check catches a cycle that started inside the session and ran past the
close.

There is no live mode. `--observe-only` goes further than the gates and
constructs no execution path at all.

### Usage

Download historical equity bars:

```bash
python -m autotrader.cli equity-download --symbol SPY --timeframe 15m --start 2026-01-02 --end 2026-08-27
```

`--start` and `--end` are **US market calendar dates**, not UTC dates, and
`--end` is inclusive. Stored timestamps are UTC; the metadata sidecar records
both facts so a dataset can be reproduced without guessing which was meant.
Stock market data requires credentials - unlike crypto, Alpaca does not serve
it unauthenticated.

Validate and backtest a stored equity dataset with the same commands the crypto
path uses; `--equity` switches the symbol universe and nothing else:

```bash
python -m autotrader.cli validate --equity data/raw/SPY_15m_2026-01-02_2026-08-27.parquet
python -m autotrader.cli backtest --equity data/raw/SPY_15m_2026-01-02_2026-08-27.parquet
```

One caveat worth stating: the backtester is the existing engineering-validation
simulator and is **not** the live execution model. It fills at the next bar's
open with *fractional* quantities and a flat conservative taker fee, which is
neither the whole-share policy nor the fee structure a real equity order would
meet. It was never connected to the risk engine either. It is a
data-to-signal-to-fill pipeline check, and its numbers are not a profitability
claim for either product.

Run one observation-only cycle - this can never submit anything:

```bash
python -m autotrader.cli equity-run --once --observe-only
```

And, once startup safety is genuinely satisfied, the gated runtime:

```bash
python -m autotrader.cli equity-run --confirm-paper-runtime PAPER
```

### One request per cycle

The whole universe is fetched in a **single batched market-data call** per
cycle - Alpaca's stock bars endpoint takes a list - so ten symbols cost one
request rather than ten. Batching bars is not batching orders: symbols are
still processed strictly in order and there is never more than one broker
submission in flight. The lookback window is anchored on real sessions read
from the cached calendar rather than on calendar days, so a holiday week does
not silently shorten the strategy's history.

Provider call counts are exposed on the market-data source, the calendar and
the execution gateway, and summed into the heartbeat, so the later shared
crypto+equity API budget has real numbers to start from. No rate limiter is
introduced here.

### Two services, one account

Later deployment will conceptually run `autotrader-crypto.service` and
`autotrader-equity.service` as separate processes against the same account. The
lock naming already supports that: the equity runner holds
`<database>.equity.runtime.lock` and the crypto runner holds
`<database>.runtime.lock`, so the two never block each other - while two
runners of the *same* product still collide, which is the property that
actually prevents duplicate trading. Nothing about account-level order safety
is weakened by that: the duplicate preflight, the durable per-symbol
checkpoint, and the `client_order_id` are all unchanged.

The per-symbol checkpoint lives in the same `runtime_checkpoints` table the
crypto runner uses. `SPY` is not `BTC/USD`, so the two products share a table
without sharing a row, and a restarted equity runner skips exactly the bars its
predecessor claimed.

### Reconciliation across two books

`reconcile_paper_state` gained one parameter: the **position universe**,
defaulting to the crypto pairs so every existing caller is unchanged. Order
intents are deliberately *not* filtered by it - one account has one
`client_order_id` namespace, so an ambiguous equity order blocks the crypto
runner and an ambiguous crypto order blocks the equity runner. A position held
outside a pass's universe is recorded as observed and never traded out of.

### What Equity V0.2 deliberately does not do

Combined crypto+equity activation, shared account-level risk arithmetic,
combined exposure limits, per-book allocations (a "crypto 20% / stocks 20%"
split would be a Combined Integration decision and is **not** frozen here), a
shared API budget, a distributed rate limiter, deployment artefacts, and any
real equity paper submission. Those seams are wired up in Combined Integration
below.

## Combined integration

Everything above describes two products. This section is about the one account
they share, and it exists because **crypto safety and equity safety cannot be
independent when a single brokerage account holds both books.**

### The shared account layer

`src/autotrader/account/` holds exactly what the two products cannot own
separately, and nothing else. No module in it submits, cancels, or replaces an
order, and none of them contacts a broker at all - they are the constraints an
order passes through, not a path an order travels down.

### One halt, either service can raise it

An ambiguous submission - a lost reply, a `504`, a response that cannot be read
or stored - has always marked the intent `UNKNOWN` and paused the runtime that
hit it. On one account that is no longer enough. While an order of unknown
status exists, the account's true position and true exposure are both unknown,
and *every* number the other runtime would size against is derived from them.

So the halt is durable and account-wide. It is written to
`account_safety_state` in the same transaction that marks the intent `UNKNOWN`,
which means an ambiguous intent without a halt beside it is not a state that
can exist. It carries the `client_order_id` an operator needs to ask the broker
what actually happened.

**Only a full-universe reconciliation clears it.** A pass that is not
`safe_to_trade` halts the account whatever it covered. A safe pass over all
twelve tracked symbols clears it. A safe pass over *fewer* symbols leaves the
state exactly as it found it - it can neither vouch for a book it did not read
nor report a problem it did not find. A database whose safety row has never
been written reports `UNSAFE_RECONCILIATION`, because "nobody has checked" is
not "we checked and it is fine".

Observation is never gated. A dry run and `--observe-only` keep working while
the account is halted, which is precisely when an operator needs them.

### One exposure figure, and no per-book cap

The risk engine is unchanged: **5% per symbol, 30% total, 2% UTC daily loss.**
`RiskContext.total_exposure` was already summed over every position the account
holds, so the arithmetic was right before this phase; what it needed was
serialization.

Crypto 18% plus equity 9% is 27% of one 30% budget, with 3% left. It is **not**
12% for crypto and 21% for equity. There is **no per-book allocation**, none
was invented here, and a "crypto 20% / equity 20%" split would be a *loosening*
of the account cap rather than a tightening of it. The dashboard shows the
crypto/equity split as a display breakdown and labels it as one.

### The account execution lock

The two runtime locks stay separate files, so the services run simultaneously -
that is unchanged and is the point. What is added is a third, account-scoped
`fcntl` lock, held across the order-decision path only:

```
acquire the shared account execution lock
  -> verify the durable account safety state
  -> charge the API budget for this section's calls
  -> read the broker account, read every position
  -> build the GLOBAL RiskContext, evaluate risk, persist the decision
  -> persist the durable OrderIntent and its client_order_id
  -> duplicate preflight
  -> submit exactly once
  -> persist the broker snapshot
release
```

It is **not** held across the fifteen-minute wait, the market-data fetch, or
the strategy evaluation. Unlike the runtime lock it *blocks*, with a bounded
timeout: contention here is the normal case rather than an operator error, and
a wait that could not be bounded could outlive the bar its decision belongs to.
A lock that cannot be taken in time fails the action closed rather than sending
the order late.

The race it closes is concrete. 28% of the account is used; both runtimes wake
with a signal; each reads 2% of headroom and each sizes into it; the account
ends at 32%. Serialized, the second caller reads the exposure the first one
consumed and is rejected or re-sized by the existing risk contract.

### One daily baseline

One account, one UTC-day baseline. `ON CONFLICT DO NOTHING` inside a
`BEGIN IMMEDIATE` transaction behind a primary key means two processes making
the first observation of a day agree on one value, and the loser's figure is
discarded rather than overwriting the winner's.

### The shared API budget

Two processes, one set of credentials. **Two counters, not one**, because the
execution boundary builds one client against the paper *trading* host and a
separate one against the *market data* host - different hosts, different
subscriptions, different allowances. Metering a bar fetch against a trading
allowance would be a limit this system invented rather than observed.

The ceilings are explicitly **this system's own** conservative numbers, derived
from its own worst realistic cycle with better than two times headroom. They
are a runaway detector - against retry storms, parallel bursts, and a loop
making calls it was never designed to make - and **not** a transcription of a
published provider rate limit, which this repository does not claim to know.

A window that cannot accommodate an action **refuses** it. Nothing sleeps,
queues, or grants the call later: a strategy signal belongs to the completed
bar that produced it, and submitting it minutes afterwards because a token
freed up would be sending a stale decision. Storage is SQLite - no Redis, no
Kafka, no Celery, no external rate-limit service.

### Full-universe reconciliation

One pass, twelve symbols: BTC/USD, ETH/USD, SPY, QQQ, IWM, AAPL, MSFT, NVDA,
AMZN, GOOGL, META, TSLA. Order intents were never filtered by the universe, so
an ambiguous equity order already blocked the crypto runner; what changes is
that positions are covered too, and that a pass records which symbols it saw,
so partial coverage is visible rather than merely smaller. Repeated passes stay
idempotent: `REPAIRED`, then `CLEAN`.

Both runtimes reconcile the whole account at startup, not just their own book -
each sizes against total exposure, and only a full-universe pass may clear the
shared halt.

### What combined integration deliberately does not do

Per-book allocation limits, live trading, deployment, and any real equity paper
order. The combined system has been exercised read-only against the real paper
account and has submitted **zero** orders.

## The operations dashboard (V0.2)

A read-only web view of the system: is it healthy, is reconciliation clean, is
trading allowed, what is held, what happened recently, how much risk is used,
are the runtimes and checkpoints current, and does anything need a person.

**V0.2 shows the combined system**, with the same visual language as V0.1 and
no redesign:

* **Two runtime cards**, crypto and equity, side by side. They are told apart
  by real durable evidence - each service writes its own lifecycle event types
  and claims bars only for its own symbols - and never by guesswork. What is
  deliberately *not* split is the strategy run: both services open one under
  the same strategy name, so attributing one to a service would be a guess.
* **A shared account safety strip** above them, because "may anything trade?"
  outranks either service's answer to "am I running?". When the account is
  halted it shows the state, who set it, and the unresolved `client_order_id`;
  when it is safe it is one quiet line.
* **An exposure breakdown** - crypto, equity, total - inside the risk card.
  It is a breakdown of the one enforced number, and it is built so it cannot be
  misread as two limits: only the total row carries a cap, and only the total
  row gets a bar.
* **The shared API budget**, two compact rows, counted across both processes,
  labelled as this system's own ceiling rather than a provider limit.
* **The header reflects the account halt.** An `UNSAFE_UNKNOWN` state - an
  order whose broker outcome is unresolved - makes the whole page `PAUSED`.
  `UNSAFE_RECONCILIATION` is `ATTENTION` instead: it is the ordinary state of a
  system that has not reconciled yet, and giving it the loud colour would make
  the loud colour mean less.

The last failure event is reported **once, on the page** rather than on each
runtime card: the events this system records are account-level and are not
tagged with a service, so printing one on both cards would attribute an equity
problem to the crypto runtime half the time.

It is **structurally incapable of trading.** There is no `POST`, `PUT`,
`PATCH`, or `DELETE` route anywhere in `src/autotrader/dashboard`, so there is
nothing a browser can send that places an order, cancels one, moves a risk
limit, starts or stops the runtime, edits a row, or triggers a reconciliation
repair. It opens SQLite with the `mode=ro` URI and `PRAGMA query_only`, so a
write is refused by the engine rather than avoided by convention. It never
imports the order-submission entry points, and it does not even name the
concrete broker client class - the client is typed as a two-method read-only
protocol. `tests/test_dashboard.py::test_dashboard_has_no_trading_write_surface`
asserts all of it. Hiding a button would have left the capability; there is no
capability.

It **owns no state.** No dashboard database, no new table, no migration, no
cached copy of trading state. Every figure is derived from the existing schema
v5 tables through `autotrader.state`'s own read helpers, or read live from the
paper account through `autotrader.execution.paper`'s read-only helpers.

It **does not interfere.** One short deferred read transaction per poll, which
in WAL mode takes no lock a writer waits on, and no journal-mode pragma is
issued against a database the trading runtime owns. A busy or missing database
is reported unreadable within a couple of seconds rather than waited on.

It **invents nothing.** A figure this system cannot truthfully read reaches the
browser as an explicit unavailable state with a reason - never as `$0.00`, a
stale carry-over, or an empty table that reads like nothing happened. There is
no chart, because nothing here persists an equity time series and a graph of
numbers nobody recorded is a lie with axes on it.

### Running it locally

Two processes. Install the extra first:

```bash
pip install -e ".[dashboard]"
```

The API, on loopback:

```bash
python -m autotrader.dashboard
```

That binds `127.0.0.1:8000` and serves six GET routes:
`/api/dashboard/overview` (the whole page in one consistent read), plus
`positions`, `orders`, `risk`, `system`, and `health`. `--port` moves the port;
there is deliberately no host flag.

The frontend, in a second terminal:

```bash
cd dashboard/frontend && npm install && npm run dev
```

Then open `http://localhost:3000`. The frontend proxies `/api/dashboard/*` to
the API process, so the browser only ever sees one origin and there is no CORS
policy to get wrong. It polls `overview` every five seconds - this system
trades on completed 15-minute bars, and a faster refresh would show motion
rather than information.

### Environment

`AUTOTRADER_DASHBOARD_DB` overrides the database path; it defaults to
`data/autotrader.db`. The dashboard needs no variable of its own beyond that.

Account equity, cash, exposure, and live positions come from the paper account,
so `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` must be exported for those to
appear - the same credentials the rest of the system uses, read in the same
place, and never returned from it. Without them the dashboard still runs and
reports every one of those figures as unavailable rather than as zero. The
credentials stay server-side: no route can carry one, broker error text is
discarded rather than forwarded, and a test searches every response body for
the configured secrets.

`AUTOTRADER_PAPER_TRADING_ENABLED` is only ever *read* here, to report whether
the submission gate is open. Nothing in the dashboard can change it.

### Not deployment

This is a local development setup. The API has **no authentication**, and
binding it anywhere but loopback would publish an unauthenticated view of an
account. Putting it on a VPS needs a reverse proxy and an authentication layer
in front of it, and that is a deployment concern that comes after Combined
Integration - it is not implemented here and nothing here implies it.

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

The research tests need no fixture data and no external storage: every bar
series is generated from a closed-form expression, and the storage root is a
temporary directory supplied explicitly rather than read from the environment,
so a test can prove the repository is never written to.

```bash
pytest -q tests/test_research_leakage.py
```

That file is worth reading before trusting any backtest in this repository. It
injects a negative shift, a centered rolling window, a globally-fitted
normalization, a backfill, a forward-looking label, a shuffled index, an
overlapping window and a holdout consulted during selection - and proves each
one is caught, alongside a legitimate construct that must not be flagged.

The dashboard frontend is checked from `dashboard/frontend`:

```bash
npm ci && npm run lint && npm run typecheck && npm run build && npm test
```

`npm test` runs the formatting rules that would otherwise misreport a figure -
an unavailable value rendering as a number, a timestamp rendering in the
viewer's timezone rather than UTC - on Node's own test runner. No test
framework is installed for it; lint, typecheck, and build cover the rest.

## Layout

```
src/autotrader/data/        Alpaca crypto bars -> canonical Parquet, and
                            stored-dataset validation (shared by both products)
src/autotrader/cli/         Typer CLI (version, download, validate, backtest,
                            paper-submit, reconcile, crypto-run,
                            equity-download, equity-run, research)
src/autotrader/strategies/  EMA 20 / EMA 50 crossover signals - one strategy,
                            both products, no per-symbol parameters
src/autotrader/backtest/    deterministic next-bar-open backtester, fractional
                            quantities, modelled taker fee
src/autotrader/research/    quant research infrastructure - the Decision Engine
                            contract, the engine-agnostic replay simulator,
                            cost models, trade accounting, metrics,
                            walk-forward splits, the leakage auditor, and
                            bounded parameter sweeps. Offline and read-only:
                            nothing here can reach an order path or the
                            operational database
src/autotrader/risk/        deterministic risk decisions and sizing
src/autotrader/state/       local SQLite operational state, schema v6
src/autotrader/execution/   Alpaca PAPER execution - the only place a trading
                            client exists or an order is sent. `paper.py` is
                            crypto (fractional, GTC); `equity.py` is equities
                            (whole shares, DAY, regular hours) and holds the
                            broker session calendar
src/autotrader/account/     the shared account layer - the durable
                            account-wide halt, the account execution lock, and
                            the shared API budget. Nothing here trades
src/autotrader/reconciliation/
                            crash recovery: broker truth -> local state, and
                            the safe_to_trade startup answer, over the full
                            twelve-symbol universe by default
src/autotrader/runtime/     shared runtime machinery - durable per-symbol bar
                            checkpoints, the process lock, startup safety, the
                            heartbeat - plus the 24/7 crypto loop and its UTC
                            boundary scheduling
src/autotrader/equity/      the equity product: the ten-symbol universe, the
                            market-session arithmetic, the IEX bar boundary,
                            the batched runtime window, and the
                            regular-session runtime loop
src/autotrader/dashboard/   the read-only operations API: GET routes only, a
                            read-only SQLite connection, and a derived read
                            model that invents no number
dashboard/frontend/         the dashboard UI (Next.js, TypeScript, Tailwind);
                            one page, five-second polling, no control
tests/                      offline tests; no test contacts the network
data/raw/                   downloaded market data (git-ignored)
data/processed/             validated market data (git-ignored)
data/autotrader.db          local operational state (git-ignored)
docs/SPEC.md                authoritative scope specification
```

## What comes next

**The combined paper smoke gate.** The combined system is code-complete, green
offline, and has been exercised read-only against the real paper account -
full-universe reconciliation, both runtimes with `--once --observe-only`, and
the dashboard against real data, with zero orders submitted. What has not
happened is a paper order observed end to end, and the equity book has never
submitted one at all. That is next, and it is a separate gate.

**Failure injection** is done. `tests/test_failure_injection.py` breaks the
system at each point in the claim/intent/submit chain - process death, lost
replies, `408`/`429`/`5xx`, malformed responses, a locked database, a second
runner - and asserts the documented recovery every time. It is offline: the
whole suite passes with sockets disabled, because reproducing a lost reply
against a real broker means placing real orders at moments chosen to be
unrecoverable. Every protection it covers was mutation-checked by removing the
protection and confirming the tests fail.

**Combined integration** is done and is described above. The four protections
it adds were mutation-checked the same way: removing the account execution lock
fails the exposure race test, ignoring the durable halt fails the cross-asset
tests, narrowing the reconciliation universe fails the reconciliation tests,
and adding a write route fails the dashboard read-only guard.

**Quant research infrastructure** is done and is described above. It is the
apparatus a future Decision Engine V2/V3/V4/V5 has to survive before it is
taken seriously: an engine-agnostic replay simulator that reproduces the
production backtester exactly, walk-forward validation with a real embargo and
an untouchable final holdout, a leakage auditor that catches look-ahead by
perturbing the future and re-asking, and bounded parameter sweeps that record
everything they tried. It evaluates and it cannot activate - no result from it
changes what either runtime does, and adopting a parameter set a study selected
would be a separate, documented scope change.

**Per-book allocation limits** are *not* approved and *not* implemented. If a
crypto/equity split is ever wanted it is a policy decision, and it belongs in
`docs/SPEC.md` before it belongs in code.

**Phase 10 - Deployment** comes after the combined paper smoke: systemd units,
container images, supervision, and an authentication layer in front of the
dashboard API, which today has none and binds loopback only. Supervising a
process whose first paper order has not been observed would be premature.
