"""Autonomous development pipeline on top of the integration orchestrator.

The orchestrator produces one thing: a green `integration/v4-prep`. This module
carries that forward through the remaining development stages - V4, V5, shadow
mode, and a final integration candidate - by handing each stage's written
specification to a coding agent, validating the result independently of that
agent, and repairing a bounded number of times before giving up.

It reuses the orchestrator wholesale: its paths, its lock, its readiness
protocol, its validation, its reporting and its LaunchAgent. Nothing here
duplicates that machinery.

The end state this can reach on its own is a green final development
candidate. Promotion to production is a manual gate and is not automated here:
nothing in this module merges or pushes `main`, deploys, restarts a runtime,
touches credentials, or reaches a broker.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path


def _load_orchestrator():
    """Import the sibling orchestrator; `tools/integration` is scripts, not a package."""
    location = Path(__file__).resolve().parent / "orchestrator.py"
    spec = importlib.util.spec_from_file_location("_autotrader_orchestrator", location)
    if spec is None or spec.loader is None:  # pragma: no cover - unreachable in practice
        raise RuntimeError(f"cannot load {location}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


orch = _load_orchestrator()

SPEC_DIR = Path(__file__).resolve().parent / "specs"

#: Where the running copy lives. When the LaunchAgent runs the installed copy
#: this is the install directory, so `INSTALLED_FROM.json` sits beside it and
#: names the revision this process is actually executing.
INSTALL_DIR = Path(
    os.environ.get("AUTOTRADER_INTEGRATION_INSTALL_DIR", str(Path(__file__).resolve().parent))
)

# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------

WAITING_FOR_V4_PREP = "WAITING_FOR_V4_PREP"
WAITING_FOR_WEB = "WAITING_FOR_WEB"
HARD_STOP = "HARD_STOP"
PIPELINE_COMPLETE = "PIPELINE_COMPLETE"

RUNNING = "RUNNING"
VALIDATING = "VALIDATING"
REPAIRING = "REPAIRING"
GREEN = "GREEN"

#: The most times a stage may be handed back to the agent before hard-stopping.
MAX_REPAIR_ATTEMPTS = 3

#: A stage's agent invocation is given this long before it is abandoned.
AGENT_TIMEOUT_SECONDS = 7200


@dataclass(frozen=True)
class Stage:
    """One autonomous development stage."""

    key: str
    prefix: str
    title: str
    branch: str
    worktree: str
    marker: str
    spec: str
    base: str
    kind: str = "build"

    def state(self, phase: str) -> str:
        return f"{self.prefix}_{phase}"

    @property
    def states(self) -> tuple[str, ...]:
        return tuple(self.state(phase) for phase in (RUNNING, VALIDATING, REPAIRING, GREEN))


#: The v4-prep integration the orchestrator itself produces. Not a stage this
#: module runs; the stage the first real stage is cut from.
V4_PREP = Stage(
    key="v4-prep",
    prefix="V4_PREP",
    title="V4 prep integration",
    branch=orch.TARGET_BRANCH,
    worktree="v4-prep-integration",
    marker=orch.TARGET_READY_PATH,
    spec="",
    base="",
    kind="integration",
)
"""The orchestrator's own stage. Listed here so the pipeline can read its
readiness and, when a merge or a check goes ordinary-wrong, repair it on the
same terms as every other stage."""

WEB_PUBLISH = Stage(
    key="web-publish",
    prefix="WEB",
    title="Web publish",
    branch="feat/web-publish",
    worktree="web-publish",
    marker=".autotrader-ready/web-publish",
    spec="",
    base="",
    kind="external",
)

STAGES: tuple[Stage, ...] = (
    Stage(
        key="v4",
        prefix="V4",
        title="V4 ML probability decision engine",
        branch="feat/decision-v4",
        worktree="decision-v4",
        marker=".autotrader-ready/decision-v4",
        spec="v4-decision-engine.md",
        base="v4-prep",
    ),
    Stage(
        key="v5",
        prefix="V5",
        title="V5 ensemble decision engine",
        branch="feat/decision-v5",
        worktree="decision-v5",
        marker=".autotrader-ready/decision-v5",
        spec="v5-ensemble.md",
        base="v4",
    ),
    Stage(
        key="shadow",
        prefix="SHADOW",
        title="Shadow engine",
        branch="feat/decision-shadow",
        worktree="shadow-engine",
        marker=".autotrader-ready/shadow-engine",
        spec="shadow-engine.md",
        base="v5",
    ),
    Stage(
        key="final",
        prefix="FINAL",
        title="Final development candidate",
        branch="integration/final-development-candidate",
        worktree="final-integration",
        marker=".autotrader-ready/final-development-candidate",
        spec="resolve-conflict.md",
        base="shadow",
        kind="merge",
    ),
)

STAGE_BY_KEY = {stage.key: stage for stage in STAGES}

#: Every stage the state machine can name, including the orchestrator's own and
#: the externally-owned web publish branch, so a transition always resolves to a
#: prefixed state name rather than a bare phase.
ALL_STAGES = {stage.key: stage for stage in (V4_PREP, WEB_PUBLISH, *STAGES)}

FINAL_PROVENANCE_PATH = ".autotrader-integration/final-development-candidate.json"
STAGE_PROVENANCE_DIR = ".autotrader-integration"


# ---------------------------------------------------------------------------
# What is never repaired automatically
#
# A failure touching trading-safety semantics is not a bug to hand back to a
# coding agent. It is a question for a person, because the cheapest wrong answer
# here is one that makes the tests pass.
# ---------------------------------------------------------------------------

PROTECTED_WORDS: tuple[str, ...] = (
    "paper",
    "live",
    "broker",
    "trading_client",
    "tradingclient",
    "submit",
    "order",
    "cancel",
    "replace",
    "unknown",
    "at_most_once",
    "duplicate",
    "intent",
    "reconcil",
    "authoritative",
    "truth",
    "risk",
    "exposure",
    "halt",
    "cap",
    "account_safety",
    "safety",
    "credential",
    "secret",
    "api_key",
    "leak",
    "lookahead",
    "look_ahead",
    "provenance",
    "kill_switch",
    "execution_lock",
)

#: Paths whose conflicts are never handed to an agent to resolve.
PROTECTED_PATHS: tuple[str, ...] = (
    "src/autotrader/execution/",
    "src/autotrader/account/",
    "src/autotrader/reconciliation/",
    "src/autotrader/risk/",
    "src/autotrader/state/",
)

#: Validation steps whose failure is always a hard stop, never a repair.
UNREPAIRABLE_STEPS: tuple[str, ...] = ("safety regression (critical invariants)",)


def mentions_protected(text: str) -> list[str]:
    """Which protected words appear in a piece of failure output."""
    lowered = text.lower()
    return sorted({word for word in PROTECTED_WORDS if word in lowered})


def touches_protected_path(paths: Sequence[str]) -> list[str]:
    return sorted(path for path in paths if path.startswith(PROTECTED_PATHS))


@dataclass
class Verdict:
    """Whether a failed validation may be handed back to the agent."""

    repairable: bool
    reason: str
    evidence: list[str] = field(default_factory=list)


def classify(attempt: orch.Attempt) -> Verdict:
    """Decide whether a failed stage validation is safe to repair automatically."""
    weakened = [
        str(entry["invariant"])
        for entry in attempt.invariants
        if entry.get("status") not in ("guarded", None)
    ]
    if weakened:
        return Verdict(
            False,
            "a critical invariant lost the tests that guard it",
            weakened,
        )

    failures = [step for step in attempt.validations if not step["passed"]]
    if not failures:
        return Verdict(False, "validation reported no failing step to repair")

    for step in failures:
        if str(step["name"]) in UNREPAIRABLE_STEPS:
            return Verdict(False, f"{step['name']} failed", [str(step["command"])])

    evidence: list[str] = []
    for step in failures:
        body = "\n".join(str(line) for line in step["tail"])
        evidence.extend(f"{step['name']}: {word}" for word in mentions_protected(body))
    if evidence:
        return Verdict(
            False,
            "the failure output names trading-safety semantics",
            sorted(set(evidence)),
        )

    return Verdict(
        True,
        "ordinary implementation failure",
        [str(step["name"]) for step in failures],
    )


# ---------------------------------------------------------------------------
# Containment
#
# The agent runs with a narrowed tool set, but a coding agent that can run a
# shell can in principle run anything. The boundary that actually holds is
# checked here, afterwards, against git: if anything outside the stage's own
# branch moved, the pipeline hard-stops rather than continuing.
# ---------------------------------------------------------------------------


def snapshot_refs(paths: orch.Paths) -> dict[str, str]:
    """Every local branch and worktree head, so a stray mutation is visible."""
    seen: dict[str, str] = {}
    branches = orch.git(paths.git_host, "for-each-ref", "--format=%(refname) %(objectname)")
    for line in branches.out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            seen[parts[0]] = parts[1]
    listing = orch.git(paths.git_host, "worktree", "list", "--porcelain")
    current = ""
    for line in listing.out.splitlines():
        if line.startswith("worktree "):
            current = line.split(" ", 1)[1]
        elif line.startswith("HEAD ") and current:
            seen[f"worktree:{current}"] = line.split(" ", 1)[1]
    return seen


@dataclass(frozen=True)
class SelfIdentity:
    """Who the orchestrator itself is, so its own upgrade is not a stage's fault.

    A stage can take half an hour. The orchestrator's own branch may legitimately
    move inside that window - somebody commits an improvement to it and
    reinstalls - and a snapshot taken before the stage began would otherwise
    attribute that move to the stage's agent.

    `installed_commit` is the pin: the revision the running copy was installed
    from, read from the install directory rather than from the branch, so a
    branch that moved without an install is still a breach.
    """

    branch: str | None
    worktree: str | None
    installed_commit: str | None

    def owns(self, ref: str) -> bool:
        if self.branch and self.branch in ref:
            return True
        return bool(self.worktree) and ref == f"worktree:{self.worktree}"


def install_records(paths: orch.Paths) -> tuple[Path, ...]:
    """Where an installed-provenance record may be found, most specific first.

    Beside the running module when the LaunchAgent runs the installed copy, and
    otherwise at the conventional install location - so a manual invocation from
    a worktree is measured against the same pin the scheduled one uses.
    """
    return (
        INSTALL_DIR / "INSTALLED_FROM.json",
        paths.qa_root / "integration-orchestrator" / "INSTALLED_FROM.json",
    )


def orchestrator_identity(paths: orch.Paths) -> SelfIdentity:
    """Read the running copy's own provenance from where it was installed."""
    record: dict[str, object] = {}
    for candidate in install_records(paths):
        try:
            loaded = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(loaded, dict) and loaded.get("source_commit"):
            record = loaded
            break
    branch = record.get("source_branch")
    commit = record.get("source_commit")
    return SelfIdentity(
        branch=branch if isinstance(branch, str) and branch else None,
        worktree=str(paths.git_host),
        installed_commit=commit if isinstance(commit, str) and commit else None,
    )


def fast_forward_in(paths: orch.Paths):
    """A predicate answering whether `old` is an ancestor of `new`."""

    def answer(old: str, new: str) -> bool:
        if not old or not new:
            return False
        return orch.git(paths.git_host, "merge-base", "--is-ancestor", old, new).ok

    return answer


def containment_breach(
    before: dict[str, str],
    after: dict[str, str],
    allowed: Sequence[str],
    *,
    identity: SelfIdentity | None = None,
    fast_forward=None,
) -> list[str]:
    """Refs that changed which this stage had no business changing.

    A ref belonging to the orchestrator itself is excused only when the move is
    a *proven* self-upgrade: it must land exactly on the revision the running
    copy was installed from, and it must be a fast-forward. A move to any other
    revision, a rewind, or a move with no install behind it is still a breach -
    a stage agent must never be able to touch another worktree or ref.
    """
    permitted = tuple(allowed)
    moved: list[str] = []
    for name in sorted(set(before) | set(after)):
        was, now = before.get(name), after.get(name)
        if was == now:
            continue
        if any(token and token in name for token in permitted):
            continue
        if (
            identity is not None
            and identity.owns(name)
            and _is_self_upgrade(was, now, identity, fast_forward)
        ):
            continue
        moved.append(f"{name}: {was or 'absent'} -> {now or 'absent'}")
    return moved


def _is_self_upgrade(
    was: str | None, now: str | None, identity: SelfIdentity, fast_forward
) -> bool:
    """Is this exactly the orchestrator moving onto the revision it now runs?"""
    if not was or not now:
        return False
    if not identity.installed_commit or now != identity.installed_commit:
        return False
    return bool(fast_forward) and bool(fast_forward(was, now))


# ---------------------------------------------------------------------------
# Durable pipeline state
#
# Written atomically after every transition, so a killed agent, a launchd
# restart, a closed terminal or a sleeping Mac resumes where it stopped rather
# than repeating a stage that already finished.
# ---------------------------------------------------------------------------


def pipeline_file(paths: orch.Paths) -> Path:
    return paths.state_dir / "pipeline.json"


def load_pipeline(paths: orch.Paths) -> dict[str, object]:
    try:
        loaded = json.loads(pipeline_file(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_pipeline(paths: orch.Paths, state: dict[str, object]) -> None:
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    target = pipeline_file(paths)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(target)


def stage_record(state: dict[str, object], key: str) -> dict[str, object]:
    stages = state.setdefault("stages", {})
    assert isinstance(stages, dict)
    record = stages.setdefault(key, {})
    assert isinstance(record, dict)
    return record


def transition(
    paths: orch.Paths, state: dict[str, object], key: str, phase: str, **facts: object
) -> dict[str, object]:
    """Record one state-machine transition and persist it before returning."""
    stage = ALL_STAGES.get(key)
    named = stage.state(phase) if stage else phase
    record = stage_record(state, key)
    record["state"] = named
    record["updated_at"] = orch.iso(orch.utc_now())
    record.update(facts)
    state["state"] = named
    state["updated_at"] = record["updated_at"]
    save_pipeline(paths, state)
    orch.log(paths, f"[{key}] -> {named}")
    return record


def hard_stop(
    paths: orch.Paths, state: dict[str, object], key: str, reason: str, evidence: Sequence[str] = ()
) -> int:
    """Stop the whole pipeline. Nothing after this runs until a person clears it."""
    record = stage_record(state, key)
    record["state"] = HARD_STOP
    record["hard_stop_reason"] = reason
    record["hard_stop_evidence"] = list(evidence)
    record["updated_at"] = orch.iso(orch.utc_now())
    state["state"] = HARD_STOP
    state["hard_stop"] = {
        "stage": key,
        "reason": reason,
        "evidence": list(evidence),
        "at": record["updated_at"],
    }
    state["updated_at"] = record["updated_at"]
    save_pipeline(paths, state)
    orch.log(paths, f"[{key}] HARD STOP: {reason}")
    report = write_pipeline_report(paths, state)
    orch.notify(
        "AutoTrader Development Pipeline Stopped",
        f"{key} hard-stopped: {reason}. Review the report.",
    )
    orch.log(paths, f"hard stop report: {report}")
    return orch.EXIT_MANUAL_REVIEW


def stopped(state: dict[str, object]) -> bool:
    return state.get("state") == HARD_STOP


# ---------------------------------------------------------------------------
# Stage readiness, read from the remote rather than from memory
# ---------------------------------------------------------------------------


def stage_green_sha(paths: orch.Paths, stage: Stage) -> str | None:
    """The remote head of a stage's branch, if it is published and marked GREEN."""
    sha = orch.remote_sha(paths.git_host, stage.branch)
    if sha is None:
        return None
    marker = orch.read_blob(paths.git_host, f"{orch.REMOTE}/{stage.branch}", stage.marker)
    if (marker or "").strip() != orch.READY_CONTENT:
        return None
    return sha


def stage_provenance(paths: orch.Paths, stage: Stage) -> dict[str, object] | None:
    if stage.key == "v4-prep":
        return orch.provenance_of(paths.git_host, f"{orch.REMOTE}/{stage.branch}")
    path = f"{STAGE_PROVENANCE_DIR}/{stage.key}.json"
    if stage.key == "final":
        path = FINAL_PROVENANCE_PATH
    raw = orch.read_blob(paths.git_host, f"{orch.REMOTE}/{stage.branch}", path)
    if raw is None:
        return None
    try:
        loaded = json.loads(raw)
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def base_sha_for(paths: orch.Paths, stage: Stage) -> str | None:
    """The exact revision this stage must be cut from, or None if not ready."""
    if stage.base == "v4-prep":
        return stage_green_sha(paths, V4_PREP)
    return stage_green_sha(paths, STAGE_BY_KEY[stage.base])


# ---------------------------------------------------------------------------
# The web publish gate
#
# A marker alone is not enough here. An earlier web publish verdict was
# withdrawn after a real browser regression - Next.js inline bootstrap scripts
# blocked by CSP, leaving the published page blank - so the gate additionally
# requires a browser regression test on the branch that would catch it again.
# ---------------------------------------------------------------------------

BROWSER_TEST_HINTS = ("playwright", "e2e", "browser")
BROWSER_EVIDENCE = (
    ("csp", ("csp", "content-security-policy", "content_security_policy")),
    ("console errors", ("console", "pageerror", "page_error", "uncaught")),
    ("hydration", ("hydrat",)),
    ("visible render", ("tobevisible", "to_be_visible", "isvisible", "waitforselector")),
)


@dataclass
class WebGate:
    """What the web publish branch currently proves about itself."""

    sha: str | None
    marker_green: bool
    test_files: list[str] = field(default_factory=list)
    proven: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return bool(self.sha) and self.marker_green and not self.missing

    @property
    def verdict(self) -> str:
        if self.sha is None:
            return "NO_REMOTE_BRANCH"
        if not self.marker_green:
            return "WAITING (marker not GREEN)"
        if self.missing:
            return f"WAITING (browser regression unproven: {', '.join(self.missing)})"
        return "GREEN"


def evaluate_web(paths: orch.Paths) -> WebGate:
    """Read the web publish branch's current head and what its tests prove."""
    sha = orch.remote_sha(paths.git_host, WEB_PUBLISH.branch)
    if sha is None:
        return WebGate(sha=None, marker_green=False, missing=["branch absent"])
    marker = orch.read_blob(
        paths.git_host, f"{orch.REMOTE}/{WEB_PUBLISH.branch}", WEB_PUBLISH.marker
    )
    green = (marker or "").strip() == orch.READY_CONTENT

    listing = orch.git(
        paths.git_host, "ls-tree", "-r", "--name-only", f"{orch.REMOTE}/{WEB_PUBLISH.branch}"
    )
    candidates = [
        line
        for line in listing.out.splitlines()
        if line.strip() and any(hint in line.lower() for hint in BROWSER_TEST_HINTS)
    ]

    corpus = ""
    for path in candidates:
        blob = orch.read_blob(paths.git_host, f"{orch.REMOTE}/{WEB_PUBLISH.branch}", path)
        if blob:
            corpus += blob.lower()
    compact = corpus.replace("-", "").replace("_", "").replace(" ", "")

    proven: list[str] = []
    missing: list[str] = []
    for name, needles in BROWSER_EVIDENCE:
        flat = tuple(needle.replace("-", "").replace("_", "") for needle in needles)
        (proven if any(needle in compact for needle in flat) else missing).append(name)
    if not candidates:
        missing = [name for name, _needles in BROWSER_EVIDENCE]
        proven = []

    return WebGate(
        sha=sha,
        marker_green=green,
        test_files=candidates,
        proven=proven,
        missing=missing,
    )


# ---------------------------------------------------------------------------
# The coding agent
#
# Claude Code, driven non-interactively. Permissions are narrowed explicitly
# rather than by bypassing the permission system, and the credentials the rest
# of the system uses are stripped from its environment so it cannot reach a
# broker even by writing its own code to do so.
#
# An agent that can run a shell can, in principle, run anything. The boundary
# that actually holds is therefore not this flag list but `snapshot_refs` and
# `containment_breach`, which are checked either side of every invocation.
# ---------------------------------------------------------------------------

AGENT_ALLOWED_TOOLS = "Read Write Edit Glob Grep Bash"

AGENT_DISALLOWED_TOOLS = " ".join(
    (
        "WebFetch",
        "WebSearch",
        "Bash(sudo:*)",
        "Bash(ssh:*)",
        "Bash(scp:*)",
        "Bash(rsync:*)",
        "Bash(curl:*)",
        "Bash(wget:*)",
        "Bash(nc:*)",
        "Bash(security:*)",
        "Bash(defaults:*)",
        "Bash(launchctl:*)",
        "Bash(systemctl:*)",
        "Bash(git push:*)",
        "Bash(git remote:*)",
        "Bash(git worktree:*)",
        "Bash(git branch:*)",
        "Bash(git checkout:*)",
        "Bash(git switch:*)",
        "Bash(git reset:*)",
        "Bash(git rebase:*)",
    )
)

AGENT_BUNDLE_GLOB = (
    "Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude"
)


@dataclass
class AgentRun:
    """One non-interactive coding-agent invocation."""

    ok: bool
    exit_code: int
    result: str
    session_id: str
    cost_usd: float
    denials: list[object]
    prompt_path: Path
    output_path: Path


def find_agent() -> Path | None:
    """Locate the Claude Code executable, preferring an explicit override."""
    override = os.environ.get("AUTOTRADER_INTEGRATION_AGENT")
    if override and Path(override).is_file() and os.access(override, os.X_OK):
        return Path(override)
    found = shutil.which("claude")
    if found:
        return Path(found)
    bundles = sorted(Path.home().glob(AGENT_BUNDLE_GLOB))
    return bundles[-1] if bundles else None


def run_agent(paths: orch.Paths, stage: Stage, worktree: Path, prompt: str, label: str) -> AgentRun:
    """Hand one written prompt to the coding agent inside one worktree."""
    agent = find_agent()
    run_dir = paths.run_log_dir / stage.key
    run_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = run_dir / f"{label}.prompt.md"
    output_path = run_dir / f"{label}.result.json"
    prompt_path.write_text(prompt, encoding="utf-8")

    if agent is None:
        message = "no Claude Code executable found"
        output_path.write_text(json.dumps({"error": message}) + "\n", encoding="utf-8")
        return AgentRun(False, 127, message, "", 0.0, [], prompt_path, output_path)

    argv = [
        str(agent),
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        AGENT_ALLOWED_TOOLS,
        "--disallowedTools",
        AGENT_DISALLOWED_TOOLS,
        "--strict-mcp-config",
        "--add-dir",
        str(worktree),
    ]
    environment = orch.validation_env(paths)
    environment["CLAUDE_CODE_ENTRYPOINT"] = "autotrader-integration-pipeline"

    # The prompt goes on stdin. `--add-dir` takes a variadic list, so a prompt
    # passed as a trailing positional is swallowed as another directory and the
    # agent is invoked with no instructions at all.
    orch.log(paths, f"[{stage.key}] agent {label}: {agent.name} in {worktree}")
    result = orch.run(
        argv,
        cwd=worktree,
        env=environment,
        timeout=AGENT_TIMEOUT_SECONDS,
        stdin_text=prompt,
    )
    output_path.write_text(result.out or result.err, encoding="utf-8")

    try:
        payload = json.loads(result.out)
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    ok = result.ok and not payload.get("is_error", False)
    return AgentRun(
        ok=ok,
        exit_code=result.code,
        result=str(payload.get("result", result.err.strip()))[:4000],
        session_id=str(payload.get("session_id", "")),
        cost_usd=float(payload.get("total_cost_usd", 0.0) or 0.0),
        denials=list(payload.get("permission_denials") or []),
        prompt_path=prompt_path,
        output_path=output_path,
    )


def read_spec(name: str) -> str:
    return (SPEC_DIR / name).read_text(encoding="utf-8")


def spec_version(name: str) -> dict[str, str]:
    """Identify exactly which specification produced a stage."""
    text = read_spec(name)
    first = text.splitlines()[0] if text else ""
    version = first.split("spec-version:", 1)[-1].strip(" ->") if "spec-version:" in first else "?"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {"spec": name, "spec_version": version, "spec_sha256": digest}


def build_prompt(stage: Stage, base_sha: str) -> str:
    """The stage specification, with the revision it is being built on."""
    return (
        f"{read_spec(stage.spec)}\n\n"
        "---\n\n"
        f"Branch: `{stage.branch}`\n"
        f"Cut from: `{base_sha}`\n"
        f"Worktree: `{stage.worktree}`\n"
    )


def repair_prompt(stage: Stage, attempt: orch.Attempt, head: str, number: int) -> str:
    """Everything the agent needs to fix what independent validation caught."""
    failures = [step for step in attempt.validations if not step["passed"]]
    blocks = []
    for step in failures:
        body = "\n".join(str(line) for line in step["tail"])
        blocks.append(f"### `{step['command']}` - exit {step['exit_code']}\n\n```\n{body}\n```")
    implicated = sorted(
        {
            token.split("::", 1)[0]
            for step in failures
            for line in step["tail"]
            for token in [str(line).strip()]
            if token.startswith(("tests/", "src/")) and "::" in token
        }
    )
    return (
        f"{read_spec('repair.md')}\n\n"
        "---\n\n"
        f"Stage: **{stage.title}**\n"
        f"Branch: `{stage.branch}`\n"
        f"Current revision: `{head}`\n"
        f"Repair attempt: {number} of {MAX_REPAIR_ATTEMPTS}\n\n"
        f"Files implicated by the output: "
        f"{', '.join(f'`{path}`' for path in implicated) if implicated else '(none named)'}\n\n"
        "## Failing validation\n\n" + "\n\n".join(blocks) + "\n"
    )


# ---------------------------------------------------------------------------
# Preparing a stage worktree
# ---------------------------------------------------------------------------


def stage_worktree(paths: orch.Paths, stage: Stage) -> Path:
    return paths.qa_root / "worktrees" / stage.worktree


def prepare_stage(paths: orch.Paths, stage: Stage, base: str) -> str:
    """Create the stage's branch and worktree, or prove an existing one is ours.

    An existing branch is never reset, forced or overwritten. It is either
    provably this stage cut from this base - in which case work resumes in it -
    or it is ambiguous, and the pipeline stops.
    """
    where = stage_worktree(paths, stage)
    local = orch.git(
        paths.git_host, "rev-parse", "--verify", "--quiet", f"{stage.branch}^{{commit}}"
    )
    exists_local = local.ok and bool(local.out.strip())
    exists_remote = orch.remote_sha(paths.git_host, stage.branch) is not None

    if exists_local or exists_remote:
        recorded = stage_provenance(paths, stage)
        if recorded is None and (where / ".git").exists():
            merged = orch.git(where, "merge-base", "--is-ancestor", base, "HEAD")
            if merged.ok:
                return f"resuming {stage.branch} in {where}"
            raise orch.Stop(
                f"{stage.branch} exists but is not descended from the agreed base {base}; "
                "manual review required."
            )
        if recorded is not None and recorded.get("base_sha") == base:
            return f"{stage.branch} already records this base"
        raise orch.Stop(
            f"{stage.branch} already exists with unverifiable provenance "
            f"(recorded base {(recorded or {}).get('base_sha')!r}, expected {base!r}); "
            "manual review required - it will not be reset."
        )

    if (where / ".git").exists():
        raise orch.Stop(f"{where} exists but {stage.branch} does not; manual review required.")
    if where.exists() and any(where.iterdir()):
        raise orch.Stop(f"{where} exists and is not empty; manual review required.")

    added = orch.git(
        paths.git_host, "worktree", "add", "-b", stage.branch, str(where), base, timeout=900
    )
    if not added.ok:
        raise orch.Stop(f"could not create {where}: {added.err.strip()}", code=orch.EXIT_ERROR)
    return f"created {stage.branch} at {base[:12]} in {where}"


# ---------------------------------------------------------------------------
# Validating a stage, independently of whatever built it
# ---------------------------------------------------------------------------


def validate_stage(paths: orch.Paths, stage: Stage, base: str) -> orch.Attempt:
    """Run the repository's own checks against the stage worktree.

    The orchestrator's validation is reused verbatim by pointing it at this
    stage's worktree, so a stage is measured by exactly the same pytest, ruff,
    whitespace, frontend and safety-invariant checks the integration is.
    """
    where = stage_worktree(paths, stage)
    scoped = replace(paths, integration_worktree=where)
    attempt = orch.Attempt(
        started_at=orch.utc_now(),
        base_sha=base,
        sources=(),
        readiness=[],
    )
    attempt.integration_sha = orch.git(where, "rev-parse", "HEAD").out.strip() or None
    green = orch.validate(scoped, attempt)
    attempt.outcome = "GREEN" if green else "VALIDATION_FAILED"
    return attempt


def publish_stage(
    paths: orch.Paths, stage: Stage, base: str, attempt: orch.Attempt, extra: dict[str, object]
) -> str:
    """Record provenance, mark the stage GREEN, and push its branch. Never forced."""
    where = stage_worktree(paths, stage)
    document: dict[str, object] = {
        "status": "GREEN",
        "stage": stage.key,
        "title": stage.title,
        "branch": stage.branch,
        "base_sha": base,
        "validation": [
            {
                "name": step["name"],
                "command": step["command"],
                "exit_code": step["exit_code"],
                "passed": step["passed"],
            }
            for step in attempt.validations
        ],
        "safety_invariants": attempt.invariants,
        "frontend_validated": attempt.frontend_required,
        "generated_at": orch.iso(orch.utc_now()),
        "generator": "tools/integration/pipeline.py",
    }
    document.update(extra)
    if stage.spec:
        document.update(spec_version(stage.spec))

    relative = (
        FINAL_PROVENANCE_PATH
        if stage.key == "final"
        else f"{STAGE_PROVENANCE_DIR}/{stage.key}.json"
    )
    provenance = where / relative
    provenance.parent.mkdir(parents=True, exist_ok=True)
    provenance.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    marker = where / stage.marker
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(orch.READY_CONTENT + "\n", encoding="utf-8")

    orch.git_ok(where, "add", relative, stage.marker)
    committed = orch.git(
        where,
        "commit",
        "-m",
        f"chore: record {stage.key} provenance\n\nbase {base}\nstage {stage.title}",
    )
    if not committed.ok:
        raise orch.Stop(f"could not commit {stage.key} provenance: {committed.err.strip()}")

    head = orch.git_ok(where, "rev-parse", "HEAD")
    pushed = orch.git(
        where,
        "push",
        orch.REMOTE,
        f"refs/heads/{stage.branch}:refs/heads/{stage.branch}",
        timeout=900,
    )
    if not pushed.ok:
        raise orch.Stop(
            f"the remote refused {stage.branch}; remote state changed underneath this stage. "
            f"Stopping rather than forcing. ({pushed.err.strip()})"
        )
    return head


# ---------------------------------------------------------------------------
# Running one stage
# ---------------------------------------------------------------------------


#: Git writes exactly seven characters and then a space and a label:
#: `<<<<<<< HEAD` and `>>>>>>> <revision>`. The run length is the whole point -
#: a longer rule of the same character is ordinary prose, and this repository
#: has reStructuredText tables whose separators are rows of `=`.
CONFLICT_OPEN = "<" * 7
CONFLICT_CLOSE = ">" * 7


def is_conflict_marker(line: str) -> bool:
    for marker in (CONFLICT_OPEN, CONFLICT_CLOSE):
        if not line.startswith(marker):
            continue
        rest = line[len(marker) :]
        if rest.startswith(" ") and not rest.startswith(marker[0]):
            return True
    return False


def marker_bearing_files(where: Path) -> list[str]:
    """Tracked text files still carrying merge conflict markers.

    Committing one of these would turn an unresolved conflict into a merge that
    looks resolved, which is the single worst thing this pipeline could do, so
    it is checked before anything is committed rather than left to a later
    whitespace check.
    """
    listed = orch.git(where, "diff", "--name-only", "HEAD")
    tracked = orch.git(where, "ls-files")
    names = {line for line in listed.out.splitlines() if line.strip()}
    names |= {line for line in tracked.out.splitlines() if line.strip()}
    found: list[str] = []
    for name in sorted(names):
        path = where / name
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(is_conflict_marker(line) for line in text.splitlines()):
            found.append(name)
    return found


def commit_agent_work(paths: orch.Paths, stage: Stage, message: str) -> str | None:
    """Capture whatever the agent left behind, so nothing is silently lost."""
    where = stage_worktree(paths, stage)
    bearing = marker_bearing_files(where)
    if bearing:
        raise orch.Stop(
            "refusing to commit a tree that still carries conflict markers: " + ", ".join(bearing)
        )
    orch.git(where, "add", "-A")
    staged = orch.git(where, "diff", "--cached", "--name-only")
    if not staged.out.strip():
        return None
    committed = orch.git(where, "commit", "-m", message)
    if not committed.ok:
        raise orch.Stop(f"could not commit {stage.key} work: {committed.err.strip()}")
    return orch.git(where, "rev-parse", "HEAD").out.strip()


def invoke_agent_guarded(
    paths: orch.Paths, state: dict[str, object], stage: Stage, prompt: str, label: str
) -> AgentRun | int:
    """Run the agent and prove it changed nothing outside this stage's branch."""
    where = stage_worktree(paths, stage)

    # The orchestrator's own revision is pinned before the stage launches, so
    # any later argument about what moved has a recorded starting point.
    launched_at = orchestrator_identity(paths)
    stage_record(state, stage.key)["orchestrator_at_launch"] = launched_at.installed_commit
    save_pipeline(paths, state)

    before = snapshot_refs(paths)
    run = run_agent(paths, stage, where, prompt, label)
    after = snapshot_refs(paths)

    # Re-read after the run: an orchestrator that upgraded itself mid-stage is
    # compared against the revision now installed, not the one it started on.
    settled = orchestrator_identity(paths)
    if settled.installed_commit != launched_at.installed_commit:
        orch.log(
            paths,
            f"[{stage.key}] the orchestrator upgraded itself during this stage: "
            f"{launched_at.installed_commit} -> {settled.installed_commit}",
        )
    breach = containment_breach(
        before,
        after,
        allowed=(stage.branch, str(where)),
        identity=settled,
        fast_forward=fast_forward_in(paths),
    )
    if breach:
        return hard_stop(
            paths,
            state,
            stage.key,
            "the agent changed git state outside its own stage",
            breach,
        )
    if run.denials:
        orch.log(paths, f"[{stage.key}] agent was denied {len(run.denials)} tool call(s)")
    if not run.ok:
        orch.log(paths, f"[{stage.key}] agent {label} failed; leaving the tree uncommitted")
        return run
    commit_agent_work(paths, stage, f"feat({stage.key}): {stage.title} - agent {label}")
    return run


def validate_and_repair(
    paths: orch.Paths, state: dict[str, object], stage: Stage, base: str
) -> int:
    """Validate, and hand ordinary failures back a bounded number of times."""
    record = stage_record(state, stage.key)
    repairs = int(record.get("repair_attempts", 0) or 0)

    while True:
        transition(paths, state, stage.key, VALIDATING, repair_attempts=repairs)
        attempt = validate_stage(paths, stage, base)
        head = attempt.integration_sha or ""

        if attempt.outcome == "GREEN":
            published = publish_stage(
                paths,
                stage,
                base,
                attempt,
                {"repair_attempts": repairs, "head_before_provenance": head},
            )
            transition(
                paths,
                state,
                stage.key,
                GREEN,
                head_sha=published,
                base_sha=base,
                repair_attempts=repairs,
                validation=[
                    {"name": step["name"], "passed": step["passed"]} for step in attempt.validations
                ],
            )
            orch.log(paths, f"[{stage.key}] GREEN at {published} and pushed")
            return orch.EXIT_OK

        verdict = classify(attempt)
        failed = [str(step["name"]) for step in attempt.validations if not step["passed"]]
        orch.log(paths, f"[{stage.key}] validation failed: {', '.join(failed) or 'unknown'}")

        if not verdict.repairable:
            return hard_stop(paths, state, stage.key, verdict.reason, verdict.evidence)
        if repairs >= MAX_REPAIR_ATTEMPTS:
            return hard_stop(
                paths,
                state,
                stage.key,
                f"{MAX_REPAIR_ATTEMPTS} repair attempts did not make this stage green",
                failed,
            )

        repairs += 1
        transition(paths, state, stage.key, REPAIRING, repair_attempts=repairs)
        outcome = invoke_agent_guarded(
            paths,
            state,
            stage,
            repair_prompt(stage, attempt, head, repairs),
            f"repair-{repairs}",
        )
        if isinstance(outcome, int):
            return outcome
        if not outcome.ok:
            orch.log(paths, f"[{stage.key}] repair {repairs} agent exit {outcome.exit_code}")


def run_build_stage(paths: orch.Paths, state: dict[str, object], stage: Stage) -> int:
    """Build one development stage from its predecessor's exact green revision."""
    published = stage_green_sha(paths, stage)
    if published is not None:
        record = stage_record(state, stage.key)
        if record.get("state") != stage.state(GREEN):
            transition(paths, state, stage.key, GREEN, head_sha=published, note="already published")
        return orch.EXIT_OK

    base = base_sha_for(paths, stage)
    if base is None:
        orch.log(paths, f"[{stage.key}] waiting: {stage.base} is not GREEN yet")
        return orch.EXIT_OK

    note = prepare_stage(paths, stage, base)
    orch.log(paths, f"[{stage.key}] {note}")

    record = stage_record(state, stage.key)
    if record.get("state") not in stage.states:
        transition(
            paths,
            state,
            stage.key,
            RUNNING,
            base_sha=base,
            branch=stage.branch,
            worktree=str(stage_worktree(paths, stage)),
            repair_attempts=0,
            **spec_version(stage.spec),
        )
        outcome = invoke_agent_guarded(paths, state, stage, build_prompt(stage, base), "build")
        if isinstance(outcome, int):
            return outcome
        if not outcome.ok:
            return hard_stop(
                paths,
                state,
                stage.key,
                f"the coding agent failed to complete the build (exit {outcome.exit_code})",
                [outcome.result[:500]],
            )
    return validate_and_repair(paths, state, stage, base)


def run_merge_stage(paths: orch.Paths, state: dict[str, object], stage: Stage) -> int:
    """Integrate the development lineage with the corrected web publish branch."""
    published = stage_green_sha(paths, stage)
    if published is not None:
        if stage_record(state, stage.key).get("state") != stage.state(GREEN):
            transition(paths, state, stage.key, GREEN, head_sha=published, note="already published")
        return orch.EXIT_OK

    base = base_sha_for(paths, stage)
    if base is None:
        orch.log(paths, f"[{stage.key}] waiting: {stage.base} is not GREEN yet")
        return orch.EXIT_OK

    web = evaluate_web(paths)
    if not web.ready:
        state["state"] = WAITING_FOR_WEB
        state["web_publish"] = {
            "sha": web.sha,
            "verdict": web.verdict,
            "proven": web.proven,
            "missing": web.missing,
            "test_files": web.test_files,
        }
        save_pipeline(paths, state)
        orch.log(paths, f"[{stage.key}] {WAITING_FOR_WEB}: {web.verdict}")
        return orch.EXIT_OK

    note = prepare_stage(paths, stage, base)
    orch.log(paths, f"[{stage.key}] {note}")
    where = stage_worktree(paths, stage)
    record = stage_record(state, stage.key)

    if record.get("state") not in stage.states:
        transition(
            paths,
            state,
            stage.key,
            RUNNING,
            base_sha=base,
            branch=stage.branch,
            worktree=str(where),
            web_publish_sha=web.sha,
            repair_attempts=0,
        )
        merged = orch.git(
            where,
            "merge",
            "--no-ff",
            "--no-edit",
            "-m",
            f"Merge {WEB_PUBLISH.branch} at {(web.sha or '')[:12]} into {stage.branch}",
            web.sha or "",
            timeout=900,
        )
        if not merged.ok:
            conflicted = [
                line
                for line in orch.git(
                    where, "diff", "--name-only", "--diff-filter=U"
                ).out.splitlines()
                if line.strip()
            ]
            protected = touches_protected_path(conflicted)
            if protected:
                return hard_stop(
                    paths,
                    state,
                    stage.key,
                    "the merge conflicts in trading-safety code",
                    protected,
                )
            orch.log(paths, f"[{stage.key}] {len(conflicted)} ordinary conflict(s) to resolve")
            prompt = (
                f"{read_spec(stage.spec)}\n\n---\n\n"
                f"Branch: `{stage.branch}`\nBase: `{base}`\n"
                f"Merging: `{WEB_PUBLISH.branch}` at `{web.sha}`\n\n"
                "## Conflicted files\n\n" + "\n".join(f"- `{path}`" for path in conflicted) + "\n"
            )
            outcome = invoke_agent_guarded(paths, state, stage, prompt, "resolve")
            if isinstance(outcome, int):
                return outcome
            remaining = orch.git(where, "diff", "--name-only", "--diff-filter=U").out.strip()
            if remaining:
                return hard_stop(
                    paths,
                    state,
                    stage.key,
                    "conflicts remain after the agent attempted them",
                    remaining.splitlines(),
                )
    return validate_and_repair(paths, state, stage, base)


# ---------------------------------------------------------------------------
# Driving the pipeline
# ---------------------------------------------------------------------------


def unmerged_paths(where: Path) -> list[str]:
    if not (where / ".git").exists():
        return []
    listed = orch.git(where, "diff", "--name-only", "--diff-filter=U")
    return [line for line in listed.out.splitlines() if line.strip()]


def merge_in_progress(where: Path) -> str | None:
    """The revision being merged, when a merge is started but not committed.

    A conflict that has been resolved and staged but not yet committed looks
    exactly like a clean tree to `--diff-filter=U`. Recovering from a process
    that died in that window is the difference between resuming and declaring
    the branch ambiguous, so the merge state is read rather than inferred.
    """
    if not (where / ".git").exists():
        return None
    found = orch.git(where, "rev-parse", "--verify", "--quiet", "MERGE_HEAD")
    return found.out.strip() or None if found.ok else None


def conflict_prompt(branch: str, base: str, merging: str, conflicted: Sequence[str]) -> str:
    return (
        f"{read_spec('resolve-conflict.md')}\n\n---\n\n"
        f"Branch: `{branch}`\n"
        f"Base: `{base}`\n"
        f"Merging: `{merging}`\n\n"
        "## Conflicted files\n\n" + "\n".join(f"- `{path}`" for path in conflicted) + "\n"
    )


def resume_prep_conflict(paths: orch.Paths, state: dict[str, object]) -> int | None:
    """Finish a v4-prep integration the orchestrator left conflicted.

    The orchestrator stops at a conflict and leaves it in place, by design and
    by test. This layer may go one step further, on exactly the terms the
    pipeline is allowed to: a conflict in trading-safety code is a hard stop,
    and only an ordinary one is handed to the agent.

    Returns None when there is no conflicted integration to resume.
    """
    where = paths.integration_worktree
    if not (where / ".git").exists():
        return None
    on = orch.git(where, "rev-parse", "--abbrev-ref", "HEAD").out.strip()
    if on != orch.TARGET_BRANCH:
        return None

    conflicted = unmerged_paths(where)
    merging = merge_in_progress(where)

    protected = touches_protected_path(conflicted)
    if protected:
        return hard_stop(
            paths,
            state,
            V4_PREP.key,
            "the v4-prep merge conflicts in trading-safety code",
            protected,
        )

    readiness = orch.evaluate(paths)
    if not all(found.ready for found in readiness):
        return hard_stop(
            paths,
            state,
            V4_PREP.key,
            "a source branch stopped being READY while its merge was conflicted",
            [f"{found.branch}: {found.verdict}" for found in readiness if not found.ready],
        )

    # The frozen set is re-read here and used for the rest of the attempt, so the
    # provenance ends up naming exactly the revisions the branch actually
    # contains - a source that moved while the merge sat conflicted is merged
    # forward and recorded at its new head, not at the one that conflicted.
    base = orch.remote_sha(paths.git_host, orch.BASE_BRANCH)
    if base is None:
        return hard_stop(paths, state, V4_PREP.key, "the integration base disappeared")

    # Only an integration this pipeline could have produced is resumed. A branch
    # of the same name that does not descend from the agreed base is somebody
    # else's, and is left exactly as it is.
    descends = orch.git(where, "merge-base", "--is-ancestor", base, "HEAD")
    if not descends.ok and merging is None:
        return hard_stop(
            paths,
            state,
            V4_PREP.key,
            f"{orch.TARGET_BRANCH} exists in {where} but does not descend from {base}; "
            "manual review required - it will not be reset.",
        )

    orch.log(
        paths,
        f"[v4-prep] resuming an unfinished integration in {where} "
        f"({len(conflicted)} conflicted, merge {'pending' if merging else 'committed'})",
    )
    transition(paths, state, V4_PREP.key, REPAIRING, conflicts=conflicted)

    if conflicted:
        orch.log(paths, f"[v4-prep] resolving {len(conflicted)} ordinary conflict(s)")
        outcome = invoke_agent_guarded(
            paths,
            state,
            V4_PREP,
            conflict_prompt(orch.TARGET_BRANCH, base, merging or "the pending merge", conflicted),
            "resolve",
        )
        if isinstance(outcome, int):
            return outcome
        remaining = unmerged_paths(where)
        if remaining:
            return hard_stop(
                paths,
                state,
                V4_PREP.key,
                "conflicts remain after the agent attempted them",
                remaining,
            )
    elif merge_in_progress(where) is not None:
        # Resolved and staged, but the process died before the commit.
        orch.log(paths, "[v4-prep] committing a merge that was resolved but never committed")
        commit_agent_work(paths, V4_PREP, f"Merge {merging} into {orch.TARGET_BRANCH}")

    attempt = orch.freeze(readiness, base)
    try:
        orch.merge_sources(paths, attempt)
        return finish_prep(paths, state, attempt)
    except orch.Stop as stop:
        attempt.outcome = attempt.outcome if attempt.outcome != "INCOMPLETE" else "STOPPED"
        attempt.detail = attempt.detail or stop.reason
        orch.finish(paths, attempt)
        return hard_stop(paths, state, V4_PREP.key, attempt.detail, attempt.conflicts)


def finish_prep(paths: orch.Paths, state: dict[str, object], attempt: orch.Attempt) -> int:
    """Validate the merged v4-prep tree, repairing ordinary failures.

    The same bounded loop every other stage gets. The orchestrator on its own
    stops at a red integration; this pipeline may hand an ordinary failure back
    to the agent up to `MAX_REPAIR_ATTEMPTS` times, and hard-stops on anything
    that names trading-safety semantics.
    """
    record = stage_record(state, V4_PREP.key)
    repairs = int(record.get("repair_attempts", 0) or 0)

    while True:
        transition(paths, state, V4_PREP.key, VALIDATING, repair_attempts=repairs)
        attempt.validations = []
        attempt.invariants = []
        if orch.validate(paths, attempt):
            orch.write_provenance(paths, attempt)
            orch.publish(paths, attempt)
            attempt.outcome = "GREEN"
            attempt.detail = "all validation green, branch published"
            orch.finish(paths, attempt)
            transition(
                paths,
                state,
                V4_PREP.key,
                GREEN,
                head_sha=attempt.integration_sha,
                repair_attempts=repairs,
            )
            return orch.EXIT_OK

        attempt.outcome = "VALIDATION_FAILED"
        attempt.detail = "the merged tree did not validate; nothing was pushed"
        verdict = classify(attempt)
        failed = [str(step["name"]) for step in attempt.validations if not step["passed"]]
        orch.log(paths, f"[v4-prep] validation failed: {', '.join(failed) or 'unknown'}")

        if not verdict.repairable:
            orch.finish(paths, attempt)
            return hard_stop(paths, state, V4_PREP.key, verdict.reason, verdict.evidence)
        if repairs >= MAX_REPAIR_ATTEMPTS:
            orch.finish(paths, attempt)
            return hard_stop(
                paths,
                state,
                V4_PREP.key,
                f"{MAX_REPAIR_ATTEMPTS} repair attempts did not make v4-prep green",
                failed,
            )

        repairs += 1
        transition(paths, state, V4_PREP.key, REPAIRING, repair_attempts=repairs)
        head = orch.git(paths.integration_worktree, "rev-parse", "HEAD").out.strip()
        outcome = invoke_agent_guarded(
            paths,
            state,
            V4_PREP,
            repair_prompt(V4_PREP, attempt, head, repairs),
            f"repair-{repairs}",
        )
        if isinstance(outcome, int):
            return outcome
        if not outcome.ok:
            orch.log(paths, f"[v4-prep] repair {repairs} agent exit {outcome.exit_code}")


def advance_v4_prep(paths: orch.Paths, state: dict[str, object]) -> int:
    """Let the orchestrator finish its own stage first. The lock is already held."""
    if stage_green_sha(paths, V4_PREP) is not None:
        if stage_record(state, V4_PREP.key).get("state") != "V4_PREP_GREEN":
            transition(paths, state, V4_PREP.key, GREEN, note="published by the orchestrator")
        return orch.EXIT_OK

    resumed = resume_prep_conflict(paths, state)
    if resumed is not None:
        return resumed

    readiness = orch.evaluate(paths)
    for found in readiness:
        orch.log(paths, f"{found.branch}: {found.verdict} @ {found.sha or 'absent'}")
    if not all(found.ready for found in readiness):
        state["state"] = WAITING_FOR_V4_PREP
        state["sources"] = {
            found.branch: {"sha": found.sha, "verdict": found.verdict} for found in readiness
        }
        save_pipeline(paths, state)
        return orch.EXIT_OK

    transition(paths, state, V4_PREP.key, RUNNING)
    code = orch.integrate_locked(paths, readiness)
    if code == orch.EXIT_OK:
        transition(paths, state, V4_PREP.key, GREEN)
        return orch.EXIT_OK

    # The orchestrator stops at a conflict and leaves it in place. An ordinary
    # one may be resolved here, on the pipeline's terms; anything else carries
    # the orchestrator's own diagnosis into the hard stop rather than an
    # anonymous exit code.
    recorded = orch.load_state(paths)
    if recorded.get("outcome") == "CONFLICT":
        resumed = resume_prep_conflict(paths, state)
        if resumed is not None:
            return resumed
    return hard_stop(
        paths,
        state,
        V4_PREP.key,
        str(recorded.get("detail") or f"the v4-prep integration did not complete (exit {code})"),
        [str(path) for path in (recorded.get("conflicts") or [])],
    )


def step(paths: orch.Paths) -> int:
    """Advance the pipeline as far as it can go in one invocation."""
    orch.preflight(paths)
    state = load_pipeline(paths)

    if stopped(state):
        detail = state.get("hard_stop", {})
        orch.log(paths, f"pipeline is HARD_STOPPED: {detail}; not advancing")
        return orch.EXIT_MANUAL_REVIEW

    if state.get("state") == PIPELINE_COMPLETE:
        orch.log(paths, "development pipeline is complete; nothing to do")
        return orch.EXIT_OK

    lock = orch.Lock(paths.lock_dir)
    acquired, why = lock.acquire()
    if not acquired:
        orch.log(paths, f"another pipeline invocation owns the lock: {why}")
        return orch.EXIT_LOCK_HELD

    try:
        code = advance_v4_prep(paths, state)
        if code != orch.EXIT_OK or stopped(state):
            return code
        if stage_green_sha(paths, V4_PREP) is None:
            return orch.EXIT_OK

        for stage in STAGES:
            runner = run_merge_stage if stage.kind == "merge" else run_build_stage
            code = runner(paths, state, stage)
            if code != orch.EXIT_OK or stopped(state):
                return code
            if stage_green_sha(paths, stage) is None:
                return orch.EXIT_OK

        state["state"] = PIPELINE_COMPLETE
        state["completed_at"] = orch.iso(orch.utc_now())
        save_pipeline(paths, state)
        report = write_pipeline_report(paths, state)
        orch.notify(
            "AutoTrader Development Pipeline GREEN",
            "Final development candidate is ready for manual production gate.",
        )
        orch.log(paths, f"DEVELOPMENT PIPELINE GREEN; report {report}")
        return orch.EXIT_OK
    except orch.Stop as stop:
        # Keyed by the pipeline itself: a stop raised out of the driver belongs
        # to no single stage, and using the current state name as a key would
        # invent a stage record nothing ever reads.
        return hard_stop(paths, state, "pipeline", stop.reason)
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def write_pipeline_report(paths: orch.Paths, state: dict[str, object]) -> Path:
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    now = orch.utc_now()
    path = paths.reports_dir / f"development-pipeline-{orch.stamp(now)}.md"
    stages = state.get("stages", {})
    stages = stages if isinstance(stages, dict) else {}

    rows = []
    for stage in (V4_PREP, *STAGES):
        record = stages.get(stage.key, {})
        record = record if isinstance(record, dict) else {}
        rows.append(
            f"| {stage.title} | `{stage.branch}` | {record.get('state', 'not started')} | "
            f"`{record.get('head_sha', record.get('base_sha', '-'))}` | "
            f"{record.get('repair_attempts', 0)} |"
        )

    web = state.get("web_publish", {})
    web = web if isinstance(web, dict) else {}
    web_files = ", ".join(f"`{name}`" for name in (web.get("test_files") or []))
    halt = state.get("hard_stop", {})
    halt = halt if isinstance(halt, dict) else {}

    text = f"""# AutoTrader development pipeline - {state.get("state", "unknown")}

- Written: {orch.iso(now)}
- Pipeline state: **{state.get("state", "unknown")}**

## Stages

| stage | branch | state | revision | repairs |
| --- | --- | --- | --- | --- |
{chr(10).join(rows)}

## Web publish gate

- Head: `{web.get("sha", "-")}`
- Verdict: {web.get("verdict", "not evaluated")}
- Browser regression proven: {", ".join(web.get("proven") or []) or "nothing yet"}
- Still missing: {", ".join(web.get("missing") or []) or "nothing"}
- Test files considered: {web_files or "none"}

## Hard stop

{("**" + str(halt.get("stage")) + "**: " + str(halt.get("reason"))) if halt else "None. "}
{chr(10).join(f"- `{item}`" for item in (halt.get("evidence") or []))}

## Production boundary

This pipeline stops at a green development candidate. It has not merged or
pushed `main`, deployed anything, restarted a runtime, changed the paper
submission gate, unmasked equity, touched credentials, or contacted a broker.
Promotion to production remains a manual gate.

## Remaining manual gates

- review the final development candidate and its provenance
- decide whether to open a pull request toward `main`
- production deployment, runtime restart and any paper smoke remain manual
"""
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Operator status
# ---------------------------------------------------------------------------


def label_for(paths: orch.Paths, state: dict[str, object], stage: Stage) -> str:
    """One word for what a stage is doing, preferring the remote over memory."""
    if stage_green_sha(paths, stage) is not None:
        return "GREEN"
    stages = state.get("stages", {})
    record = stages.get(stage.key, {}) if isinstance(stages, dict) else {}
    named = str(record.get("state", "")) if isinstance(record, dict) else ""
    if named == HARD_STOP:
        return "RED"
    for phase, shown in (
        (RUNNING, "RUNNING"),
        (REPAIRING, "REPAIRING"),
        (VALIDATING, "VALIDATING"),
    ):
        if named == stage.state(phase):
            return shown
    return "WAITING"


def stage_has_work(paths: orch.Paths, stage: Stage) -> bool:
    """Has this stage's branch already been built on top of its base?"""
    where = stage_worktree(paths, stage)
    if not (where / ".git").exists():
        return False
    base = stage_record(load_pipeline(paths), stage.key).get("base_sha")
    if not isinstance(base, str) or not base:
        return False
    counted = orch.git(where, "rev-list", "--count", f"{base}..HEAD")
    return counted.ok and counted.out.strip() not in ("", "0")


def clear_hard_stop(paths: orch.Paths) -> list[str]:
    """Lift a hard stop without discarding what the stopped stage had finished.

    A stage whose branch already carries its build resumes at validation rather
    than being rebuilt from scratch - the work is committed, and repeating it
    would cost the same hours again and consume nothing but goodwill. A stage
    with nothing on its branch is forgotten entirely so it starts clean.

    Repair budgets are untouched: an infrastructure stop is not a failed repair.
    """
    state = load_pipeline(paths)
    said: list[str] = []
    halt = state.get("hard_stop")
    if isinstance(halt, dict) and halt:
        said.append(f"cleared: {halt.get('stage')} - {halt.get('reason')}")
    state.pop("hard_stop", None)

    stages = state.get("stages")
    stages = stages if isinstance(stages, dict) else {}
    for key in sorted(stages):
        record = stages[key]
        if not isinstance(record, dict):
            continue
        if key not in ALL_STAGES:
            # A record keyed by something that was never a stage; nothing reads
            # it, and leaving it makes the persisted machine harder to follow.
            del stages[key]
            said.append(f"pruned a record that names no stage: {key!r}")
            continue
        if record.get("state") != HARD_STOP:
            continue
        record.pop("hard_stop_reason", None)
        record.pop("hard_stop_evidence", None)
        stage = ALL_STAGES[key]
        if stage.kind != "integration" and stage_has_work(paths, stage):
            record["state"] = stage.state(VALIDATING)
            said.append(
                f"{key}: build is committed, resuming at validation "
                f"(repairs {record.get('repair_attempts', 0)}/{MAX_REPAIR_ATTEMPTS})"
            )
        else:
            del stages[key]
            said.append(f"{key}: nothing built yet, will start clean")

    state["stages"] = stages
    state["state"] = "RESUMED"
    state["updated_at"] = orch.iso(orch.utc_now())
    save_pipeline(paths, state)
    said.append("hard stop cleared; the next step re-evaluates from the remote")
    return said


def command_status(paths: orch.Paths) -> int:
    orch.preflight(paths)
    orch.fetch(paths)
    state = load_pipeline(paths)
    readiness = orch.evaluate(paths)
    web = evaluate_web(paths)

    titles = {
        "feat/decision-v2-v3": "V2/V3",
        "feat/quant-research": "Quant",
        "feat/ml-foundation": "ML Foundation",
    }
    print("PIPELINE")
    for found in readiness:
        shown_name = titles.get(found.branch, found.branch)
        print(f"  {shown_name:<18} {'READY' if found.ready else 'WAITING'}")
    prep = stage_green_sha(paths, V4_PREP)
    print(f"  {'V4 Prep':<18} {'READY' if prep else 'WAITING'}")
    for stage in STAGES[:3]:
        print(f"  {stage.title.split()[0]:<18} {label_for(paths, state, stage)}")
    print(f"  {'Web Publish':<18} {'GREEN' if web.ready else 'WAITING'}")
    print(f"  {'Final Integration':<18} {label_for(paths, state, STAGES[3])}")

    overall = state.get("state", "not started")
    if overall == HARD_STOP:
        shown = "HARD_STOP"
    elif overall == PIPELINE_COMPLETE:
        shown = "COMPLETE"
    else:
        shown = "ACTIVE"
    print(f"  {'Pipeline':<18} {shown}")

    print("\nREVISIONS")
    print(f"  {'v4-prep':<18} {prep or '-'}")
    stages = state.get("stages", {})
    stages = stages if isinstance(stages, dict) else {}
    for stage in STAGES:
        record = stages.get(stage.key, {})
        record = record if isinstance(record, dict) else {}
        head = stage_green_sha(paths, stage) or record.get("head_sha") or record.get("base_sha")
        repairs = record.get("repair_attempts", 0)
        print(f"  {stage.key:<18} {head or '-'}  repairs {repairs}/{MAX_REPAIR_ATTEMPTS}")
    print(f"  {'web-publish':<18} {web.sha or '-'}  {web.verdict}")

    halt = state.get("hard_stop")
    if isinstance(halt, dict) and halt:
        print(f"\nHARD STOP\n  stage  {halt.get('stage')}\n  reason {halt.get('reason')}")
        for item in halt.get("evidence") or []:
            print(f"  - {item}")

    agent = find_agent()
    print(f"\nAGENT\n  {agent or 'not found'}")

    latest = sorted(paths.reports_dir.glob("development-pipeline-*.md"))
    integration = sorted(paths.reports_dir.glob("v4-prep-integration-*.md"))
    print(f"\nLATEST REPORTS\n  pipeline    {latest[-1] if latest else 'none yet'}")
    print(f"  integration {integration[-1] if integration else 'none yet'}")
    print(f"\nLOCK\n  {'held' if paths.lock_dir.exists() else 'free'}  {paths.lock_dir}")
    return orch.EXIT_OK


def command_agent_check(paths: orch.Paths) -> int:
    """Prove the coding agent can be driven unattended, without changing anything."""
    agent = find_agent()
    print(f"executable: {agent or 'NOT FOUND'}")
    if agent is None:
        return orch.EXIT_MANUAL_REVIEW
    version = orch.run([str(agent), "--version"], timeout=120)
    print(f"version:    {version.out.strip() or version.err.strip()}")
    probe = orch.run(
        [
            str(agent),
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            "dontAsk",
            "--disallowedTools",
            "Bash Edit Write WebFetch WebSearch",
            "--strict-mcp-config",
            "Reply with exactly the word PROBE_OK and nothing else.",
        ],
        cwd=paths.state_dir if paths.state_dir.is_dir() else None,
        env=orch.validation_env(paths),
        timeout=300,
    )
    try:
        payload = json.loads(probe.out)
    except ValueError:
        payload = {}
    answer = str(payload.get("result", "")).strip() if isinstance(payload, dict) else ""
    print(f"exit:       {probe.code}")
    print(f"reply:      {answer or probe.err.strip()[:200]}")
    healthy = probe.ok and answer == "PROBE_OK"
    print(f"unattended: {'yes' if healthy else 'no'}")
    return orch.EXIT_OK if healthy else orch.EXIT_MANUAL_REVIEW


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipeline.py",
        description="Carry the integration forward through V4, V5, shadow and final.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show every stage, revision and gate")
    sub.add_parser("step", help="advance the pipeline as far as one invocation can")
    sub.add_parser("agent-check", help="prove the coding agent runs unattended")
    sub.add_parser("report", help="write a pipeline report from the current state")
    clear = sub.add_parser("clear-hard-stop", help="resume after a person has resolved a stop")
    clear.add_argument("--confirm", action="store_true", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = orch.resolve_paths()
    try:
        if args.command == "status":
            return command_status(paths)
        if args.command == "step":
            return step(paths)
        if args.command == "agent-check":
            return command_agent_check(paths)
        if args.command == "report":
            orch.preflight(paths)
            print(write_pipeline_report(paths, load_pipeline(paths)))
            return orch.EXIT_OK
        if args.command == "clear-hard-stop":
            orch.preflight(paths)
            for line in clear_hard_stop(paths):
                print(line)
            return orch.EXIT_OK
    except orch.Stop as stop:
        print(f"stopped: {stop.reason}", file=sys.stderr)
        return stop.code
    except KeyboardInterrupt:
        return orch.EXIT_OK
    return orch.EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
