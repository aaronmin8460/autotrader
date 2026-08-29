<!-- spec-version: 1 -->
# V5 - versioned ensemble decision engine

You are working inside an isolated git worktree on branch `feat/decision-v5`,
cut from the green `feat/decision-v4`. The V3 deterministic score and the V4
calibrated probability are both available here.

Build the V5 ensemble on top of them.

## What to build

A versioned ensemble decision engine that combines:

- the **V3 deterministic multi-timeframe score**
- the **V4 calibrated ML probability**
- **market regime context**
- **volatility adjustment**

and produces:

- a **final bounded confidence** - bounded by construction, not by clamping a
  value that could have been anything
- a **BUY / HOLD / SELL candidate**
- **explainable component contributions**: for any decision, which input moved
  it and by how much, recoverable after the fact

## The HOLD zone

Keep an explicit HOLD band. A decision near the boundary must resolve to HOLD
rather than to whichever side is marginally ahead. The band is configuration,
recorded with the decision, not a constant buried in a branch.

## Boundaries you must not cross

- V5 produces a **candidate**. It does not size, it does not approve, and it
  does not execute. The Risk Engine remains the only thing that decides whether
  a candidate becomes an order, and it must not be bypassed, weakened, or
  called with pre-approved arguments.
- The decision layer **cannot reach the broker.** No client, no order request,
  no submission path.
- V5 is not activated in production by this work. No default flips to V5, no
  runtime starts preferring it, no gate opens.
- Do not weaken, rename or delete any existing safety test.
- Existing risk limits, the account halt, reconciliation authority, broker
  truth and at-most-once semantics are unchanged.

## Working notes

- Version the ensemble explicitly, the way V4 versions its models, so a
  recorded decision names the exact ensemble that produced it.
- Follow the conventions already in `src/autotrader/`.
- Tests belong in `tests/`, in the existing naming style. Cover at minimum: the
  HOLD band, bounded confidence, component attribution, regime and volatility
  handling, and that a V5 candidate cannot reach the broker.
- `ruff check .` and `ruff format .` must pass; line length is 100.
- Commit on `feat/decision-v5`. Do not push, do not touch `main`, do not create
  or delete branches.
