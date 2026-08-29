# V4 prep integration orchestrator

A local macOS watcher that waits for three parallel development branches to
declare themselves ready, and then - once, and only once - merges them,
validates the result, and publishes it.

It is infrastructure. It contains no trading logic, and it is deliberately
incapable of most of the things that would make an automated merge dangerous.

## What it does

1. Fetches `origin` and reads a readiness marker out of each branch's **current
   remote head**.
2. When all three read exactly `GREEN`, freezes those three commit ids and the
   integration base, so a branch that moves mid-attempt is not silently
   consumed.
3. Creates `integration/v4-prep` from `origin/feat/combined-integration` in an
   isolated worktree on the external workspace.
4. Merges the three frozen revisions in a fixed order with ordinary `--no-ff`
   merges.
5. Runs the repository's own validation against the merged tree.
6. Only if everything is green: records provenance, commits it, and pushes
   `integration/v4-prep`.
7. Writes a report, updates its machine-readable state, and posts a local
   notification.

## What it cannot do

The tests in `tests/test_integration_orchestrator.py` assert these against the
argument vectors the module can actually construct, not against its prose:

- it never merges, pushes, checks out or even names `main`
- it pushes exactly one refspec, `integration/v4-prep`, never forced
- its entire git vocabulary is `add`, `cat-file`, `commit`, `diff`, `fetch`,
  `merge`, `push`, `rev-parse`, `worktree`
- no `reset`, `rebase`, `cherry-pick`, `--squash`, `checkout`, `branch -D`,
  `update-ref`, or `worktree remove`
- no `--ours`, `--theirs` or merge strategy option: a conflict stops the
  pipeline rather than being resolved
- the only programs it runs are `git`, `ps`, `osascript`, `npm` and a Python
  interpreter - no `ssh`, `scp`, `rsync`, `launchctl`, `systemctl` or `curl`,
  so no host is contacted and no service is restarted
- broker credentials are stripped from the validation environment, so a
  validation run cannot reach a broker even by accident

## Beyond v4-prep

This orchestrator's job ends at a green `integration/v4-prep`. The autonomous
development pipeline that carries it through V4, V5, shadow mode and a final
development candidate is `pipeline.py`, documented in
[PIPELINE.md](PIPELINE.md). It reuses everything here - the lock, the paths,
the readiness protocol, the validation, the reporting - and the LaunchAgent now
runs `pipeline.py step`, which performs this integration itself before
advancing any later stage. There is still one watcher and one lock.

## The readiness protocol

| branch | marker |
| --- | --- |
| `origin/feat/decision-v2-v3` | `.autotrader-ready/decision-v2-v3` |
| `origin/feat/quant-research` | `.autotrader-ready/quant-research` |
| `origin/feat/ml-foundation` | `.autotrader-ready/ml-foundation` |

Each must contain exactly `GREEN`. A trailing newline is allowed; anything else
- lower case, extra words, a second line - is not ready.

Readiness is a property of the remote head only. Branch age, commit messages,
local working-tree contents and an agent's own claim of success are all ignored,
and `origin` is fetched before every evaluation. Withdrawing readiness is
therefore just a commit that removes the marker.

## Merge order

Fixed, and asserted by a test:

1. `feat/decision-v2-v3`
2. `feat/quant-research`
3. `feat/ml-foundation`

On any conflict the orchestrator stops at once. It does not resolve, does not
choose a side, does not abort and retry in another order. The conflicted merge
is **left in place** in the integration worktree so it can be inspected and
repaired by hand, and a later scheduled run finds a branch whose provenance it
cannot verify and refuses to touch it.

## Validation

Against the merged tree, in a dedicated virtual environment inside the external
integration worktree:

- `pytest -q`
- `ruff check .`
- `ruff format --check .`
- `git diff --check`, and `git diff --check <base> HEAD`
- the twelve critical-invariant guards, run as a named set

When the merged diff touches `dashboard/frontend/` or `src/autotrader/dashboard/`
the established frontend pipeline runs too: `npm ci`, `npm run lint`,
`npm run typecheck`, `npm run build`, `npm test`, using the external npm cache.

The safety regression is verification, not new policy. Each of the twelve
invariants names existing tests, and an invariant whose guards a merge deleted or
renamed is reported `WEAKENED` even when the suite is otherwise green - a guard
that no longer runs no longer guards anything.

## Concurrency

macOS ships no GNU `flock`, so exclusion rests on `mkdir`, which APFS makes
atomic. The lock records its owner's pid, host and process start time.

A held lock is broken only when the owner is *proved* gone: same host, and
either the pid no longer exists or it has been reused by a process with a
different start time. An unreadable owner record, a foreign host, or a living
owner all mean "leave it alone", however old the lock looks.

## Commands

    orchestrator.py check       fetch and report readiness; mutates nothing
    orchestrator.py run-once    integrate if and only if all three are READY
    orchestrator.py watch       poll until the integration completes
    orchestrator.py status      branches, integration state, latest report

Exit codes: `0` fine (including "not ready" and "already complete"), `1`
unexpected error, `2` stopped and needs a person, `4` another instance holds the
lock, `5` the external workspace is not mounted.

## Installation

    tools/integration/install.sh

Installs three things and nothing else:

| path | why there |
| --- | --- |
| `$AUTOTRADER_QA/integration-orchestrator/` | the copy the agent runs, plus `INSTALLED_FROM.json` naming the commit it came from |
| `~/Library/Application Support/AutoTrader/integration-orchestrator/bootstrap.sh` | on the internal disk so launchd always has a program to start |
| `~/Library/LaunchAgents/com.autotrader.integration-orchestrator.plist` | the agent, in the user domain |

**The LaunchAgent runs the installed external copy, not this worktree.** The
installer refuses to run with uncommitted changes under `tools/integration`, and
records the commit it installed, so the scheduled agent can never depend on
somebody's working directory. Re-run `install.sh` after changing the
orchestrator.

### The git host

The installed copy carries the orchestrator's *logic*; it still needs a
repository to fetch into and to create the integration worktree from. That is
`$AUTOTRADER_QA/worktrees/auto-integrator` by default, overridable with
`AUTOTRADER_INTEGRATION_GIT_HOST`. Only remote-tracking refs and worktree
metadata are written there, and the branch that worktree has checked out is
irrelevant - every git command names an explicit directory, so nothing depends
on which branch happens to be current. If that worktree is removed, the
orchestrator exits with code `5` and integrates nothing.

The authoritative repository at `/Users/byeongilmin/dev/autotrader` is never
checked out, merged, committed to or pushed. Creating the integration worktree
does register it in that repository's shared `.git/worktrees/` metadata, as any
`git worktree add` does; its own HEAD, branch and working tree are untouched.

The bootstrap is the only piece on the internal disk, because a program launchd
cannot find is a spawn failure every five minutes rather than a clean no-op. It
checks that the workspace is a genuinely mounted APFS volume, finds an
interpreter of 3.11 or newer - macOS's own `/usr/bin/python3` is 3.9 - exports
the same external cache paths `session-env.sh` does, and hands over. Everything
bulky is written to `$AUTOTRADER_QA/logs/integration-orchestrator/`; only the
bootstrap's own diagnostics reach the internal launchd log.

## Operating it

    /Volumes/AUTOTRADER_QA/integration-status.sh            # status
    /Volumes/AUTOTRADER_QA/integration-status.sh run-once   # integrate now
    /Volumes/AUTOTRADER_QA/integration-status.sh enable      # start the agent
    /Volumes/AUTOTRADER_QA/integration-status.sh disable     # stop the agent
    /Volumes/AUTOTRADER_QA/integration-status.sh log         # recent activity
    /Volumes/AUTOTRADER_QA/integration-status.sh report      # latest report
    /Volumes/AUTOTRADER_QA/integration-status.sh agent       # LaunchAgent state

## Where things are written

| path | contents |
| --- | --- |
| `$AUTOTRADER_QA/worktrees/v4-prep-integration/` | the integration worktree |
| `$AUTOTRADER_QA/reports/v4-prep-integration-*.md` | one report per attempt |
| `$AUTOTRADER_QA/logs/integration-orchestrator/state.json` | machine-readable state |
| `$AUTOTRADER_QA/logs/integration-orchestrator/latest-status.txt` | the short answer |
| `$AUTOTRADER_QA/logs/integration-orchestrator/orchestrator.log` | event log |
| `$AUTOTRADER_QA/logs/integration-orchestrator/launchd-run.log` | scheduled-run output |

On the integration branch itself, once it is green:

| path | contents |
| --- | --- |
| `.autotrader-integration/v4-prep.json` | base, the three source revisions, merge order, every command run and its result |
| `.autotrader-ready/v4-prep` | `GREEN` |

## Stopping

Every ambiguity resolves to no integration rather than a questionable one. The
orchestrator stops, writes a report and asks for a person when: a source branch
is missing, a marker is not exactly `GREEN`, `integration/v4-prep` already
exists with unknown or different provenance, any merge conflicts, any check
fails, an invariant lost its guards, the push is rejected, the external
workspace is absent, or another instance holds the lock.

Once the integration is published, later scheduled runs read the recorded state
and return immediately - they do not even fetch.
