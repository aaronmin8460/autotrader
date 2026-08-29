# Decision shadow mode — observe every version, execute exactly one

Shadow mode runs **all five** decision engine versions over one completed bar,
writes down what each of them decided, and hands back **at most one** execution
candidate: the one belonging to the version a caller explicitly configured. The
other four are observational, permanently.

Nothing in this milestone activates in production. No default flips to any
engine, no runtime constructs a panel, no gate reads a recorded decision, and no
risk limit, account halt, reconciliation rule or at-most-once guarantee changes.
The crypto and equity runtimes still evaluate the C3 crossover and still hand it
to the same Risk Engine.

---

## 1. Where it sits

    Decision Engine → DecisionResult → Risk Engine → Order Intent → Execution
    ^^^^^^^^^^^^^^^
    V1  V2  V3  V4  V5          ← all five evaluated
                 └──────────────→ one candidate, from the configured version

| Module | Holds |
| --- | --- |
| `autotrader.shadow.panel` | The shape. Runs every version over one frame and sorts the answers into observations and at most one `ExecutionCandidate` |
| `autotrader.shadow.recorder` | The persistence. One row per version into `shadow_decisions`, five rows or none |
| `autotrader.shadow.cycle` | The composition. Claim the bar, evaluate, record, then release the candidate |
| `autotrader.shadow.versions` | The shipped five, built for one symbol, with no default about which executes |

The package imports `autotrader.decision`, `autotrader.state`, `pandas` and the
standard library. **That is the whole list**, and it is the argument: nothing
here imports the execution layer, the risk engine, the reconciliation layer, the
account layer, a runtime that holds a gateway, or a provider SDK, so "the shadow
recorder has no path to the broker" is a statement about the import graph rather
than about anyone's intentions. A test walks every file in the package and
asserts it against an allowlist.

The processed-bar checkpoint arrives as a **structural protocol**
(`cycle.BarClaim`) rather than an import, for exactly that reason: the module
that owns the production checkpoint also owns the production execution gateway.
A test pins the local protocol's method signatures against
`runtime.checkpoint.ProcessedBarCheckpoint`, which is the same declare-and-pin
arrangement the decision package already uses to keep a provider library out of
a research process.

---

## 2. Why one execution is a type, not a flag

The obvious shape is a list of results with an `executed` boolean on each, and
the obvious bug in that shape is a caller that reads the wrong element.

`ExecutionCandidate` instead carries the configured execution version alongside
the result and **refuses to exist** when the two disagree:

```python
ExecutionCandidate(result=v5_result, execution_version="v1")
# ShadowConfigError: Engine version 'v5' cannot produce an execution candidate…
```

`PanelEvaluation.candidate` returns one optional candidate, and there is no API
anywhere that returns two. So "evaluating five versions costs one execution
decision" is not a discipline anyone has to maintain — one is the most the shape
can express.

A HOLD from the configured version releases nothing. A candidate is a direction
to be sized and then approved or refused; a HOLD is the absence of one, not a
zero-sized version of one.

---

## 3. Why an observational engine cannot break the trading path

Each version is evaluated independently, and a controlled `DecisionError` from
one is captured as a `ShadowFailure` rather than propagated. A shadow version
that can abort the cycle is not observational — it is a fifth way to lose a
trade.

The converse is deliberately **not** softened. When the *configured* version
fails there is simply no candidate, which misses a trade rather than executing a
decision that was never made. Anything that is not a `DecisionError` — a genuine
defect rather than a controlled refusal — still propagates, because hiding those
would buy reliability with silence.

---

## 4. What is recorded (schema v7)

One row per `(symbol, bar_timestamp, engine_version)` in `shadow_decisions`:

| Column | Notes |
| --- | --- |
| `bar_timestamp`, `symbol` | The bar the decision was made on. **This pair is the linkage** any recorded decision is later scored against price action with |
| `engine_version` | Which version decided |
| `signal`, `score`, `confidence` | The decision, bounded by the same `[-1, 1]` and `[0, 1]` the decision contract enforces |
| `regime`, `reasons` | The classification and the stable machine tokens explaining it |
| `feature_version`, `model_version` | Provenance. **NULL is an answer**: V1 computes no feature schema and carries no model, and inventing versions for it would falsify every V1 row |
| `execution_version` | Which version was configured to execute, written on every row so the record is self-describing |
| `designation` | `EXECUTED` or `NOT_EXECUTED` |
| `client_order_id` | The second linkage, for the executed decision only |

**The two linkages are asymmetric, honestly.** Every decision carries its bar,
so any of them can be scored against what the market did next — the only outcome
an observational decision will ever have. The one that was executed additionally
carries the `client_order_id` of the intent it produced, so it can also be scored
against what actually happened. An observational decision has no order to be
compared with, because it never got one, and the schema refuses to attach one.

`designation = EXECUTED` says the candidate was **released** to the layers
downstream — never that an order exists. The row is written before risk, the
account gates and reconciliation have been asked anything.

### Reasons are a separator-joined token list

Reason tokens are stable machine tokens (`SCORE_IN_HOLD_BAND`,
`TIMEFRAMES_ALIGNED_BULLISH`), never prose, so they are stored space-joined. A
token containing whitespace is **refused on write** rather than silently split in
half on read. This keeps the state module's dependency footprint at zero — it
still imports only the standard library, with no serializer.

---

## 5. What the database enforces, not the caller

Three rules are in SQL, because a rule that only holds when the caller remembers
it is not a rule. Each is tested by writing raw `INSERT`s that bypass every line
of this feature's code.

```sql
UNIQUE (symbol, bar_timestamp, engine_version)
CHECK (designation = 'NOT_EXECUTED' OR engine_version = execution_version)
CHECK (client_order_id IS NULL OR designation = 'EXECUTED')

CREATE UNIQUE INDEX idx_shadow_decisions_one_execution
ON shadow_decisions (symbol, bar_timestamp)
WHERE designation = 'EXECUTED';
```

The partial unique index is the durable half of "five versions cost one
execution": a second `EXECUTED` row for one bar has nowhere to go. The rule is
one execution per **bar**, not one per instant — two symbols may each execute on
the same timestamp.

---

## 6. One bar, one claim, one intent, at most one order

The sequence is C9's, with the panel dropped into the middle of it. The order of
the steps is the safety argument:

1. **Ask the checkpoint.** A bar at or older than the symbol's durable claim has
   already been acted on. Nothing is evaluated and nothing is recorded.
2. **Claim the bar, durably, before deciding.** The claim commits before any
   version sees the frame. Unchanged from C9: **miss a trade rather than
   duplicate a trade.**
3. **Evaluate every version.** Five decisions, one frame, in memory.
4. **Record all of them, atomically.**
5. **Only then release the candidate**, and at most one exists to release.

**Step 4 before step 5 is not cosmetic.** The candidate reaches the caller only
after the record commits, so the storage layer's refusal to hold two execution
candidates for one bar is a second, *durable* guard on the thing that costs
money — not merely an audit constraint. A bar whose decisions cannot be written
is a bar that produces no candidate, whatever the in-process panel computed.

**Three independent things have to fail** before one completed bar could become
two orders: the checkpoint would have to forget the claim, the panel would have
to offer a second candidate, and the database would have to accept a second
executed row. Each is enforced somewhere the other two cannot reach, and each is
tested with the other two removed.

---

## 7. There is no default execution version

`execution_version` is a required keyword-only argument on `EnginePanel` and on
`panel_for_symbol`. Which engine trades is precisely the question shadow mode
exists to answer with evidence, and a default would answer it by omission — for
whoever forgot to pass one, in production.

V1 is the only version this system has ever executed, and that is a fact about
history rather than a recommendation encoded anywhere in this package.

---

## 8. The fixtures that make the tests mean something

Five engines that all agree prove nothing about which one is allowed to act, so
the tests use two bar sets chosen to disagree in opposite directions:

| Fixture | V1 | V2–V5 |
| --- | --- | --- |
| `v1_buys_bars()` — a deterministic oscillation | **BUY** | HOLD |
| `others_buy_bars()` — a steady climb | HOLD | **BUY** |

Configuring V2–V5 on the first fixture yields **no candidate while V1 is asking
to trade**. Configuring V1 on the second yields **no candidate while four
versions are asking to trade**. That is the case a convention-based
implementation gets wrong, and it is checked from both sides.

---

## 9. Boundaries this milestone does not cross

- No broker call from the decision or shadow layer, asserted against the import
  graph and against a socket-blocked run of the whole cycle.
- The Risk Engine is not bypassed. Reconciliation authority, broker truth,
  UNKNOWN-means-no-retry, durable intent and at-most-once semantics are
  unchanged.
- Nothing activates. No gate opens, no default execution version exists, equity
  stays as it is, and nothing outside `autotrader.shadow` imports it.
- No existing safety test was weakened, renamed or deleted. One — V5's
  "nothing outside the decision package has started preferring V5" — grew a
  narrow exemption for this package, paired with a new test that holds the
  package to the symmetry the exemption claims: every version in the panel, no
  default, and V5 named no more often than V1.
