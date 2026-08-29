<!-- spec-version: 1 -->
# V4 - ML probability decision engine

You are working inside an isolated git worktree on branch `feat/decision-v4`,
cut from the green `integration/v4-prep`. Everything you need is already
merged here: the Decision V2/V3 feature architecture, the quant research
infrastructure, and the ML/data foundation.

Build the V4 ML probability decision engine on top of them.

## What to build

A decision engine that turns completed-bar features into a **calibrated
probability**, and exposes the common Decision Engine contract the later V5
ensemble will consume.

Requirements, all of which are checked independently after you finish:

- **Completed bars only.** No partial or forming bar may reach a feature, a
  training row, or a decision.
- **Temporal / walk-forward validation.** Evaluation splits must move forward
  in time. No random k-fold over time-ordered rows.
- **No lookahead, no leakage.** A feature for bar *t* may use nothing from
  bar *t+1* or later. Target construction must not leak into features. Scalers,
  encoders and imputers must be fit on training folds only, never on the full
  set before splitting.
- **Evidence-driven model choice.** Prefer a robust tabular baseline -
  regularised logistic regression, gradient-boosted trees - and only reach for
  anything heavier if the walk-forward results in this repository justify it.
  Record the comparison. Arbitrary complexity is a defect here, not a feature.
- **Probability output**, not a bare label.
- **Calibration.** The probability must be calibrated and the calibration
  measured, so downstream sizing can treat it as a probability.
- **Versioned model artifacts.** A trained model is written with an explicit
  version and enough metadata - feature version, training window, code
  revision - to identify exactly what produced it.
- **Determinism.** Given the same data, configuration and seed, training and
  inference reproduce. Where a dependency cannot be made deterministic, say so
  in the module rather than pretending otherwise.
- **Crypto and Equity**, with the session semantics each already uses in this
  repository. Do not invent new session rules.

## The contract V5 will consume

Expose a stable, typed interface: given a symbol, a completed bar, and the
feature set, return the probability, the model version, the feature version,
and the reasons behind the answer. V5 must be able to combine this with the V3
deterministic score without reaching inside V4.

## Boundaries you must not cross

These are existing invariants of this system, verified by tests you must not
weaken:

- The decision layer **cannot reach the broker.** No `TradingClient`, no order
  request type, no submission call, no import of the execution boundary for
  the purpose of sending anything.
- Nothing here activates in production, changes the paper submission gate,
  unmasks equity, or alters risk limits, the account halt, reconciliation, or
  at-most-once semantics.
- Do not weaken, rename or delete any existing safety test. If one genuinely
  must change, stop and explain instead of changing it.
- No credentials in code, tests or fixtures.

## Working notes

- Follow the conventions already in `src/autotrader/`: typed, documented,
  small modules, prose that explains *why*.
- Write tests next to the existing suite in `tests/`, in its naming style.
- Cover at minimum: no-lookahead, temporal splitting, leakage guards,
  reproducibility, and model artifact versioning.
- `ruff check .` and `ruff format .` must pass; line length is 100.
- Commit your work on `feat/decision-v4` with a clear message. Do not push, do
  not touch `main`, and do not create or delete branches.
