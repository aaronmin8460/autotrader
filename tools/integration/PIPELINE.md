# The autonomous development pipeline

`orchestrator.py` produces one thing: a green `integration/v4-prep`.
`pipeline.py` carries that forward through the remaining development stages
without a person pasting a prompt for each one, and stops at a green final
development candidate.

It does not replace the orchestrator. It reuses its paths, its lock, its
readiness protocol, its validation, its reporting and its LaunchAgent, and adds
a stage machine on top.

## The production boundary

The furthest this can go on its own is:

> **FINAL DEVELOPMENT CANDIDATE GREEN - READY FOR MANUAL PRODUCTION GATE**

It never merges or pushes `main`, deploys to the VPS, restarts a runtime,
unmasks equity, opens or closes the paper submission gate, places, cancels or
replaces an order, contacts a broker, touches credentials, changes account
activation, runs the Monday SPY smoke, or promotes anything to production.
Those are asserted by tests against the argument vectors the module can build,
not by its prose.

## Stages

| stage | branch | worktree | cut from | marker |
| --- | --- | --- | --- | --- |
| V4 ML probability engine | `feat/decision-v4` | `decision-v4` | green `integration/v4-prep` | `.autotrader-ready/decision-v4` |
| V5 ensemble | `feat/decision-v5` | `decision-v5` | green `feat/decision-v4` | `.autotrader-ready/decision-v5` |
| Shadow engine | `feat/decision-shadow` | `shadow-engine` | green `feat/decision-v5` | `.autotrader-ready/shadow-engine` |
| Final candidate | `integration/final-development-candidate` | `final-integration` | green `feat/decision-shadow` + web publish | `.autotrader-ready/final-development-candidate` |

Every stage is cut from its predecessor's **exact green revision**, read from
the remote, never from `main` and never from a moving ref.

## States

```
WAITING_FOR_V4_PREP
V4_RUNNING      V4_VALIDATING      V4_REPAIRING      V4_GREEN
V5_RUNNING      V5_VALIDATING      V5_REPAIRING      V5_GREEN
SHADOW_RUNNING  SHADOW_VALIDATING  SHADOW_REPAIRING  SHADOW_GREEN
WAITING_FOR_WEB
FINAL_RUNNING   FINAL_VALIDATING   FINAL_REPAIRING   FINAL_GREEN
HARD_STOP
```

Each transition is written atomically to
`$AUTOTRADER_QA/logs/integration-orchestrator/pipeline.json` **before** it is
acted on, with the exact revisions involved. A killed agent, a launchd restart,
a closed terminal or a sleeping Mac resumes from that file; a stage that is
already published is recognised from the remote and never rebuilt.

## The coding agent

Claude Code, driven non-interactively:

```
claude -p --output-format json
       --permission-mode acceptEdits
       --allowedTools "Read Write Edit Glob Grep Bash"
       --disallowedTools "WebFetch WebSearch Bash(sudo:*) Bash(ssh:*) ..."
       --strict-mcp-config --add-dir <stage worktree>
```

The prompt goes on **stdin**. `--add-dir` is variadic, so a prompt passed as a
trailing positional is swallowed as another directory and the agent runs with
no instructions at all.

No permission bypass is used: not `--dangerously-skip-permissions`, not
`bypassPermissions`. The deny list covers the network tools, `sudo`, remote
shells, service managers, and the git subcommands that could move anything
outside the stage.

**Where the boundary actually is.** An agent that can run a shell can in
principle run anything, so the deny list is a fence, not a wall. Three things
hold instead:

1. Broker credentials are stripped from its environment, so it cannot reach a
   broker even by writing its own client.
2. Its working directory and its only additional directory are the stage
   worktree.
3. Every git ref and worktree head is snapshotted **before and after** each
   invocation. If anything moved that was not this stage's own branch, the
   pipeline hard-stops. That check, not the flag list, is what makes the
   boundary provable.

Specifications live in `tools/integration/specs/`, are versioned, and their
SHA-256 is recorded in each stage's provenance, so a stage names the exact
instructions that produced it.

## Validation

Independent of whatever wrote the code. The orchestrator's own validation is
pointed at the stage worktree, so a stage is measured by exactly the checks the
integration is: `pytest -q`, `ruff check .`, `ruff format --check .`,
`git diff --check` (plain and against the base), the frontend pipeline when the
diff touches it, and the twelve critical safety invariants.

## Repair

A failed validation is classified before anything is retried.

**Repairable** - handed back to the agent with the exact failing commands,
their output, the current revision and the implicated files. At most
**3 attempts** per stage.

**Never repaired, always a hard stop:**

- a failed safety-invariant step
- an invariant whose guarding tests were deleted or renamed
- failure output naming the paper/live boundary, order submission, UNKNOWN
  handling, at-most-once, durable intent, reconciliation, broker truth, risk
  limits, account safety, credentials, leakage, lookahead or provenance
- a merge conflict in `execution/`, `account/`, `reconciliation/`, `risk/` or
  `state/`
- an agent that changed git state outside its own stage
- a tree that still carries conflict markers
- a rejected push, a moved source branch, or an ambiguous existing branch

A hard stop writes a report, posts a local notification, and stops the whole
pipeline until a person runs `clear-stop`.

## Conflict resolution

An ordinary conflict - documentation, unrelated modules - may be handed to the
agent inside the isolated integration worktree. A conflict touching
trading-safety code never is.

Nothing is committed until the tree is proved free of conflict markers. Git
writes exactly seven characters and a space (`<<<<<<< HEAD`); a longer rule of
the same character is ordinary prose, and this repository has reStructuredText
tables made of `=`. Staging a marker-bearing file would turn an unresolved
conflict into a merge that looks resolved, which is the worst thing this
pipeline could do.

A merge that was resolved and staged but never committed - a process killed in
that window - is detected through `MERGE_HEAD` and committed on the next run,
rather than being mistaken for an ambiguous branch.

## The web publish gate

Web publish is independent: it never blocks V4, V5 or shadow. Only the final
integration waits for it.

A marker alone does not open this gate. An earlier web publish verdict was
withdrawn after a real browser regression - Next.js inline bootstrap scripts
blocked by CSP left the published page blank - so the gate additionally requires
a browser regression test on the branch's current head that demonstrably covers:

- the page visibly rendering
- hydration completing
- no CSP violations
- no uncaught JavaScript errors

If that cannot be read off the branch, the pipeline waits rather than guessing.

## Scheduling

The existing user-level LaunchAgent, unchanged in label and interval:
`com.autotrader.integration-orchestrator`, every 300 seconds. Its bootstrap now
runs `pipeline.py step`, which performs the v4-prep integration itself and then
advances the stages. One entry point, one lock, no competing watcher.

A long stage simply holds the lock; launchd does not run a second copy of a job
that is still running, and a manual invocation that finds the lock held exits
with code 4 rather than starting a second agent.

## Operating it

```
/Volumes/AUTOTRADER_QA/integration-status.sh              # every stage and gate
/Volumes/AUTOTRADER_QA/integration-status.sh step         # advance now
/Volumes/AUTOTRADER_QA/integration-status.sh agent-check  # prove the agent runs
/Volumes/AUTOTRADER_QA/integration-status.sh clear-stop   # resume after a stop
/Volumes/AUTOTRADER_QA/integration-status.sh report       # latest report
/Volumes/AUTOTRADER_QA/integration-status.sh log          # recent activity
/Volumes/AUTOTRADER_QA/integration-status.sh prep-status  # the v4-prep stage alone
```

## Where things are written

| path | contents |
| --- | --- |
| `$AUTOTRADER_QA/logs/integration-orchestrator/pipeline.json` | the state machine |
| `$AUTOTRADER_QA/logs/integration-orchestrator/runs/<stage>/` | every prompt handed to the agent and every result |
| `$AUTOTRADER_QA/reports/development-pipeline-*.md` | one report per stop or completion |
| `$AUTOTRADER_QA/worktrees/<stage>/` | one isolated worktree per stage |

Worktrees are never deleted by the pipeline, in any state.
