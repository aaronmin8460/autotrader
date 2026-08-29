<!-- spec-version: 1 -->
# Repair - fix what validation caught

You are working inside the same isolated git worktree you built this stage in.
Independent validation ran after your last change and failed. The exact
commands, their exit codes and their output are below.

Fix the cause.

## How to approach it

- Read the failure before changing anything. The output is the evidence; your
  recollection of what you wrote is not.
- Fix the **cause**, not the symptom. Do not delete, skip, `xfail`, or loosen a
  test to make it pass. Do not widen an assertion until it stops failing.
- If a test's expectation genuinely contradicts the interface this stage was
  asked to build, changing the test is legitimate - but say explicitly which
  test, and why the new expectation is the correct one.
- If the failure is in an area you were told not to touch, or you cannot fix it
  without weakening a safety guarantee, **stop and explain**. Stopping is a
  correct outcome.

## What you must not do

- Do not weaken, rename, skip or delete any existing safety test.
- Do not change risk limits, account safety, reconciliation, broker truth,
  UNKNOWN handling, durable intent, or at-most-once semantics to make a test
  pass.
- Do not add a credential, a network call, or a broker path.
- Do not push, do not touch `main`, do not create or delete branches.

## Working notes

- `ruff check .` and `ruff format .` must pass; line length is 100.
- Commit the fix on the current branch with a message that says what was wrong.
