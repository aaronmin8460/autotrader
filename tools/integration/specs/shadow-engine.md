<!-- spec-version: 1 -->
# Shadow engine - observe every version, execute exactly one

You are working inside an isolated git worktree on branch
`feat/decision-shadow`, cut from the green `feat/decision-v5`. Decision engine
versions V1 through V5 are all available here.

Build shadow mode.

## What to build

For every eligible completed bar, record what **each** decision engine version
would have decided - V1, V2, V3, V4, V5 - while **exactly one** explicitly
configured version is allowed to produce an execution candidate. Every other
version is observational.

Recording a decision must not require executing it, and evaluating five
versions must cost one execution decision, not five.

Persist enough to compare them honestly later:

- timestamp and symbol
- engine version
- signal, score, confidence
- reasons
- feature version and model version
- linkage for later outcome evaluation, so a recorded decision can be scored
  against what actually happened
- an explicit executed / not-executed designation

## The property that matters

Shadow infrastructure must not be able to submit an order, and running five
evaluations must not be able to produce more than one execution. Prove it:

- the shadow recorder has no path to the broker at all
- only the configured execution version yields a candidate; the other four
  cannot, by construction rather than by convention
- the existing at-most-once and durable-intent semantics still hold when five
  versions all decide on the same bar - one bar, one claim, one intent, at most
  one order
- a duplicate or replayed bar still cannot multiply orders

## Boundaries you must not cross

- No broker call from the decision or shadow layer.
- The Risk Engine is not bypassed. Reconciliation authority, broker truth,
  UNKNOWN-means-no-retry, durable intent and at-most-once semantics are
  unchanged.
- Nothing activates in production. No gate opens, no default execution version
  changes to an unproven engine, equity stays as it is.
- Do not weaken, rename or delete any existing safety test.

## Working notes

- Storage should follow the existing state conventions in
  `src/autotrader/state/`, including a migration if the schema grows.
- Tests belong in `tests/`, in the existing naming style. Cover at minimum:
  five versions recorded from one bar, only the configured version executing,
  no duplicate execution from multiple evaluations, and shadow having no broker
  path.
- `ruff check .` and `ruff format .` must pass; line length is 100.
- Commit on `feat/decision-shadow`. Do not push, do not touch `main`, do not
  create or delete branches.
