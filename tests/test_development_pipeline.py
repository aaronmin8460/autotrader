"""Tests for the autonomous development pipeline.

The pipeline decides when a machine may build the next stage of this system
without a person watching, so what matters is not that it can go forward but
that it refuses to. These tests drive it against real temporary repositories
and a stub coding agent: no live branch is marked ready, no real agent is
invoked, and nothing here reaches a network, a broker or `main`.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_PATH = REPO_ROOT / "tools" / "integration" / "pipeline.py"
SPEC_DIR = REPO_ROOT / "tools" / "integration" / "specs"


def load_pipeline_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_development_pipeline", PIPELINE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pipeline = load_pipeline_module()
orch = pipeline.orch


def run_git(where: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(where), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    """A bare remote, a clone, and the external directory layout."""
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    host = tmp_path / "host"
    qa_root = tmp_path / "qa"
    for name in ("worktrees", "reports", "logs", "tmp", "caches"):
        (qa_root / name).mkdir(parents=True, exist_ok=True)

    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    run_git(source, "config", "user.email", "test@example.invalid")
    run_git(source, "config", "user.name", "Pipeline Test")
    (source / "README.md").write_text("base\n", encoding="utf-8")
    run_git(source, "add", "README.md")
    run_git(source, "commit", "-q", "-m", "base")
    run_git(source, "remote", "add", "origin", str(remote))
    run_git(source, "push", "-q", "origin", "main")
    for branch in (
        orch.BASE_BRANCH,
        *[name for name, _marker in orch.SOURCES],
        pipeline.WEB_PUBLISH.branch,
    ):
        run_git(source, "checkout", "-q", "-b", branch, "main")
        run_git(source, "push", "-q", "origin", branch)
        run_git(source, "checkout", "-q", "main")

    subprocess.run(["git", "clone", "-q", str(remote), str(host)], check=True)
    run_git(host, "config", "user.email", "test@example.invalid")
    run_git(host, "config", "user.name", "Pipeline Test")
    return {"remote": remote, "source": source, "host": host, "qa_root": qa_root}


def paths_for(workspace: dict[str, Path]) -> orch.Paths:
    qa_root = workspace["qa_root"]
    return orch.Paths(
        qa_root=qa_root,
        git_host=workspace["host"],
        integration_worktree=qa_root / "worktrees" / "v4-prep-integration",
        state_dir=qa_root / "logs" / "integration-orchestrator",
        reports_dir=qa_root / "reports",
    )


def publish_file(workspace: dict[str, Path], branch: str, path: str, body: str) -> str:
    source = workspace["source"]
    run_git(source, "checkout", "-q", branch)
    target = source / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    run_git(source, "add", path)
    run_git(source, "commit", "-q", "-m", f"{branch}: {path}")
    sha = run_git(source, "rev-parse", "HEAD")
    run_git(source, "push", "-q", "origin", branch)
    run_git(source, "checkout", "-q", "main")
    return sha


def mark_stage_green(
    workspace: dict[str, Path], stage, base: str, extra: dict[str, object] | None = None
) -> str:
    """Publish a stage branch carrying its marker and provenance."""
    source = workspace["source"]
    run_git(source, "checkout", "-q", "-B", stage.branch, base)
    relative = (
        pipeline.FINAL_PROVENANCE_PATH
        if stage.key == "final"
        else f"{pipeline.STAGE_PROVENANCE_DIR}/{stage.key}.json"
    )
    document = {"status": "GREEN", "stage": stage.key, "base_sha": base}
    document.update(extra or {})
    for path, body in (
        (relative, json.dumps(document)),
        (stage.marker, "GREEN\n"),
    ):
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        run_git(source, "add", path)
    run_git(source, "commit", "-q", "-m", f"mark {stage.key}")
    sha = run_git(source, "rev-parse", "HEAD")
    run_git(source, "push", "-q", "origin", stage.branch)
    run_git(source, "checkout", "-q", "main")
    return sha


# --------------------------------------------------------------------------
# The state machine
# --------------------------------------------------------------------------


def test_every_required_state_name_exists() -> None:
    """The stage names the pipeline was specified with, exactly."""
    named = {pipeline.WAITING_FOR_V4_PREP, pipeline.WAITING_FOR_WEB, pipeline.HARD_STOP}
    for stage in pipeline.STAGES:
        named.update(stage.states)

    assert named == {
        "WAITING_FOR_V4_PREP",
        "V4_RUNNING",
        "V4_VALIDATING",
        "V4_REPAIRING",
        "V4_GREEN",
        "V5_RUNNING",
        "V5_VALIDATING",
        "V5_REPAIRING",
        "V5_GREEN",
        "SHADOW_RUNNING",
        "SHADOW_VALIDATING",
        "SHADOW_REPAIRING",
        "SHADOW_GREEN",
        "WAITING_FOR_WEB",
        "FINAL_RUNNING",
        "FINAL_VALIDATING",
        "FINAL_REPAIRING",
        "FINAL_GREEN",
        "HARD_STOP",
    }


def test_the_stage_order_and_lineage_are_fixed() -> None:
    assert [stage.key for stage in pipeline.STAGES] == ["v4", "v5", "shadow", "final"]
    assert [stage.base for stage in pipeline.STAGES] == ["v4-prep", "v4", "v5", "shadow"]


def test_each_stage_has_its_own_branch_worktree_and_marker() -> None:
    branches = [stage.branch for stage in pipeline.STAGES]
    worktrees = [stage.worktree for stage in pipeline.STAGES]
    markers = [stage.marker for stage in pipeline.STAGES]

    assert branches == [
        "feat/decision-v4",
        "feat/decision-v5",
        "feat/decision-shadow",
        "integration/final-development-candidate",
    ]
    assert len(set(worktrees)) == len(worktrees)
    assert len(set(markers)) == len(markers)
    assert all(marker.startswith(".autotrader-ready/") for marker in markers)


def test_every_stage_specification_exists_and_is_versioned() -> None:
    for stage in pipeline.STAGES:
        assert (SPEC_DIR / stage.spec).is_file(), stage.spec
        identity = pipeline.spec_version(stage.spec)
        assert identity["spec_version"] == "1"
        assert len(identity["spec_sha256"]) == 64


def test_a_transition_is_persisted_before_it_is_returned(workspace: dict[str, Path]) -> None:
    paths = paths_for(workspace)
    state: dict[str, object] = {}

    pipeline.transition(paths, state, "v4", pipeline.RUNNING, base_sha="a" * 40)

    stored = pipeline.load_pipeline(paths)
    assert stored["state"] == "V4_RUNNING"
    assert stored["stages"]["v4"]["base_sha"] == "a" * 40
    assert stored["stages"]["v4"]["updated_at"]


def test_a_completed_stage_survives_a_restart(workspace: dict[str, Path]) -> None:
    """A launchd re-invocation reads the same file and does not forget."""
    paths = paths_for(workspace)
    state: dict[str, object] = {}
    pipeline.transition(paths, state, "v4", pipeline.GREEN, head_sha="b" * 40)

    revived = pipeline.load_pipeline(paths)

    assert revived["stages"]["v4"]["state"] == "V4_GREEN"
    assert revived["stages"]["v4"]["head_sha"] == "b" * 40


def test_corrupt_pipeline_state_reads_as_empty(workspace: dict[str, Path]) -> None:
    paths = paths_for(workspace)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    pipeline.pipeline_file(paths).write_text("{ truncated", encoding="utf-8")

    assert pipeline.load_pipeline(paths) == {}


def test_a_hard_stop_is_recorded_and_halts_the_pipeline(workspace: dict[str, Path]) -> None:
    paths = paths_for(workspace)
    state: dict[str, object] = {}

    code = pipeline.hard_stop(paths, state, "v4", "a protected thing moved", ["evidence"])

    assert code == orch.EXIT_MANUAL_REVIEW
    assert pipeline.stopped(state)
    stored = pipeline.load_pipeline(paths)
    assert stored["state"] == "HARD_STOP"
    assert stored["hard_stop"]["reason"] == "a protected thing moved"
    assert list(paths.reports_dir.glob("development-pipeline-*.md"))


def test_a_hard_stopped_pipeline_does_no_further_work(workspace: dict[str, Path]) -> None:
    paths = paths_for(workspace)
    pipeline.save_pipeline(paths, {"state": pipeline.HARD_STOP, "hard_stop": {"stage": "v4"}})

    assert pipeline.step(paths) == orch.EXIT_MANUAL_REVIEW
    assert not paths.lock_dir.exists()


# --------------------------------------------------------------------------
# What is never repaired automatically
# --------------------------------------------------------------------------


def failing(name: str, tail: list[str]) -> orch.Attempt:
    attempt = orch.Attempt(started_at=orch.utc_now(), base_sha="a" * 40, sources=(), readiness=[])
    attempt.validations = [
        {"name": name, "command": f"run {name}", "exit_code": 1, "passed": False, "tail": tail}
    ]
    attempt.invariants = [{"invariant": "broker paper account only", "status": "guarded"}]
    return attempt


def test_an_ordinary_test_failure_is_repairable() -> None:
    verdict = pipeline.classify(
        failing("pytest -q", ["E   ImportError: cannot import name 'featurise'"])
    )

    assert verdict.repairable


def test_a_lint_failure_is_repairable() -> None:
    verdict = pipeline.classify(failing("ruff check .", ["F401 imported but unused"]))

    assert verdict.repairable


@pytest.mark.parametrize(
    "tail",
    [
        ["E   assert paper is True"],
        ["FAILED tests/test_execution_paper.py::test_the_trading_client_is_paper"],
        ["E   the broker returned an UNKNOWN order"],
        ["E   at_most_once violated"],
        ["E   durable intent missing before submit"],
        ["E   reconciliation authority lost"],
        ["E   the 5% risk cap was exceeded"],
        ["E   total exposure over the limit"],
        ["E   daily loss halt not applied"],
        ["E   credential found in response body"],
        ["E   target leakage detected in fold 3"],
        ["E   lookahead in feature construction"],
    ],
)
def test_a_failure_naming_trading_safety_is_never_repaired(tail: list[str]) -> None:
    verdict = pipeline.classify(failing("pytest -q", tail))

    assert not verdict.repairable
    assert verdict.evidence


def test_a_failed_safety_regression_step_is_never_repaired() -> None:
    verdict = pipeline.classify(failing("safety regression (critical invariants)", ["1 failed"]))

    assert not verdict.repairable


def test_a_weakened_invariant_is_never_repaired() -> None:
    """A merge that deletes a guard is a safety regression, not a bug to fix."""
    attempt = failing("pytest -q", ["E   TypeError"])
    attempt.invariants = [
        {"invariant": "2% UTC-day loss halt", "status": "WEAKENED", "missing": ["test_x"]}
    ]

    verdict = pipeline.classify(attempt)

    assert not verdict.repairable
    assert "2% UTC-day loss halt" in verdict.evidence


def test_every_protected_area_named_in_the_brief_is_covered() -> None:
    words = set(pipeline.PROTECTED_WORDS)
    for needed in (
        "paper",
        "live",
        "submit",
        "unknown",
        "intent",
        "at_most_once",
        "reconcil",
        "truth",
        "risk",
        "exposure",
        "halt",
        "credential",
        "leak",
        "provenance",
    ):
        assert needed in words


def test_a_conflict_in_trading_safety_code_is_never_handed_to_an_agent() -> None:
    conflicted = [
        "docs/SPEC.md",
        "src/autotrader/risk/engine.py",
        "src/autotrader/execution/paper.py",
    ]

    protected = pipeline.touches_protected_path(conflicted)

    assert protected == ["src/autotrader/execution/paper.py", "src/autotrader/risk/engine.py"]


def test_an_ordinary_documentation_conflict_is_not_protected() -> None:
    assert pipeline.touches_protected_path(["docs/SPEC.md", "README.md"]) == []


# --------------------------------------------------------------------------
# Containment
# --------------------------------------------------------------------------


def test_a_stage_may_move_its_own_branch(workspace: dict[str, Path]) -> None:
    before = {"refs/heads/feat/decision-v4": "a" * 40}
    after = {"refs/heads/feat/decision-v4": "b" * 40}

    assert pipeline.containment_breach(before, after, ["feat/decision-v4"]) == []


def test_a_stage_moving_main_is_a_breach() -> None:
    before = {"refs/heads/main": "a" * 40}
    after = {"refs/heads/main": "b" * 40}

    breach = pipeline.containment_breach(before, after, ["feat/decision-v4"])

    assert breach and "refs/heads/main" in breach[0]


def test_a_stage_moving_another_feature_branch_is_a_breach() -> None:
    before = {"refs/heads/feat/quant-research": "a" * 40}
    after = {"refs/heads/feat/quant-research": "b" * 40}

    assert pipeline.containment_breach(before, after, ["feat/decision-v4"])


def test_a_stage_moving_another_worktree_is_a_breach() -> None:
    before = {"worktree:/Volumes/AUTOTRADER_QA/worktrees/ml-foundation": "a" * 40}
    after = {"worktree:/Volumes/AUTOTRADER_QA/worktrees/ml-foundation": "b" * 40}

    assert pipeline.containment_breach(before, after, ["feat/decision-v4"])


def test_a_deleted_branch_is_a_breach() -> None:
    before = {"refs/heads/feat/ema-strategy": "a" * 40}

    assert pipeline.containment_breach(before, {}, ["feat/decision-v4"])


def test_snapshot_sees_branches_and_worktree_heads(workspace: dict[str, Path]) -> None:
    paths = paths_for(workspace)
    orch.fetch(paths)

    seen = pipeline.snapshot_refs(paths)

    assert any(name.startswith("refs/heads/") for name in seen)
    assert any(name.startswith("worktree:") for name in seen)


# --------------------------------------------------------------------------
# The coding agent invocation
# --------------------------------------------------------------------------


def capture_agent(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Intercept the agent call so its argv and stdin can be asserted on."""
    seen: dict[str, object] = {}

    def fake_run(argv, *, cwd=None, env=None, timeout=600, stdin_text=None):
        seen.update(argv=list(argv), cwd=cwd, env=env, stdin_text=stdin_text, timeout=timeout)
        return orch.Ran(tuple(argv), 0, json.dumps({"result": "done", "is_error": False}), "")

    monkeypatch.setattr(pipeline, "find_agent", lambda: Path("/fake/claude"))
    monkeypatch.setattr(orch, "run", fake_run)
    return seen


def test_the_prompt_reaches_the_agent_on_stdin(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--add-dir` is variadic, so a trailing positional prompt is swallowed."""
    seen = capture_agent(monkeypatch)
    paths = paths_for(workspace)

    pipeline.run_agent(paths, pipeline.STAGES[0], workspace["host"], "BUILD THIS", "build")

    assert seen["stdin_text"] == "BUILD THIS"
    assert "BUILD THIS" not in seen["argv"]
    assert seen["argv"][-2:] == ["--add-dir", str(workspace["host"])]


def test_the_agent_runs_non_interactively_with_explicit_permissions(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = capture_agent(monkeypatch)

    pipeline.run_agent(paths_for(workspace), pipeline.STAGES[0], workspace["host"], "x", "build")

    argv = seen["argv"]
    assert "-p" in argv
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--dangerously-skip-permissions" not in argv
    assert "bypassPermissions" not in argv
    assert "--allow-dangerously-skip-permissions" not in argv


def test_the_agent_never_receives_broker_credentials(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = capture_agent(monkeypatch)
    monkeypatch.setenv("ALPACA_API_KEY", "must-not-propagate")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "must-not-propagate")

    pipeline.run_agent(paths_for(workspace), pipeline.STAGES[0], workspace["host"], "x", "build")

    environment = seen["env"]
    assert "ALPACA_API_KEY" not in environment
    assert "ALPACA_SECRET_KEY" not in environment
    assert "AUTOTRADER_PAPER_TRADING_ENABLED" not in environment


@pytest.mark.parametrize(
    "denied",
    [
        "WebFetch",
        "WebSearch",
        "Bash(sudo:*)",
        "Bash(ssh:*)",
        "Bash(scp:*)",
        "Bash(rsync:*)",
        "Bash(curl:*)",
        "Bash(git push:*)",
        "Bash(git remote:*)",
        "Bash(git branch:*)",
        "Bash(git checkout:*)",
        "Bash(git reset:*)",
        "Bash(launchctl:*)",
        "Bash(systemctl:*)",
    ],
)
def test_the_agent_is_denied_the_tools_it_must_never_have(denied: str) -> None:
    assert denied in pipeline.AGENT_DISALLOWED_TOOLS


def test_the_agent_prompt_and_result_are_kept(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capture_agent(monkeypatch)
    paths = paths_for(workspace)

    run = pipeline.run_agent(paths, pipeline.STAGES[0], workspace["host"], "REMEMBER ME", "build")

    assert run.prompt_path.read_text(encoding="utf-8") == "REMEMBER ME"
    assert run.output_path.is_file()


def test_a_missing_agent_is_reported_rather_than_guessed(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "find_agent", lambda: None)

    run = pipeline.run_agent(
        paths_for(workspace), pipeline.STAGES[0], workspace["host"], "x", "build"
    )

    assert not run.ok
    assert run.exit_code == 127


# --------------------------------------------------------------------------
# Conflict markers are never committed
#
# The failure this guards against is the worst one available to this pipeline:
# staging a file that still carries markers turns an unresolved conflict into a
# merge that looks resolved.
# --------------------------------------------------------------------------


def conflicted_worktree(workspace: dict[str, Path], stage) -> Path:
    where = workspace["qa_root"] / "worktrees" / stage.worktree
    subprocess.run(["git", "init", "-q", "-b", "main", str(where)], check=True)
    run_git(where, "config", "user.email", "t@example.invalid")
    run_git(where, "config", "user.name", "T")
    (where / "doc.md").write_text("base\n", encoding="utf-8")
    run_git(where, "add", "doc.md")
    run_git(where, "commit", "-q", "-m", "base")
    return where


def test_a_tree_carrying_conflict_markers_is_detected(workspace: dict[str, Path]) -> None:
    where = conflicted_worktree(workspace, pipeline.STAGES[0])
    (where / "doc.md").write_text(
        "intro\n<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> other\n", encoding="utf-8"
    )

    assert pipeline.marker_bearing_files(where) == ["doc.md"]


def test_a_clean_tree_carries_no_markers(workspace: dict[str, Path]) -> None:
    where = conflicted_worktree(workspace, pipeline.STAGES[0])
    (where / "doc.md").write_text("intro\nmine\ntheirs\n", encoding="utf-8")

    assert pipeline.marker_bearing_files(where) == []


def test_committing_a_tree_with_markers_is_refused(workspace: dict[str, Path]) -> None:
    paths = paths_for(workspace)
    where = conflicted_worktree(workspace, pipeline.STAGES[0])
    (where / "doc.md").write_text("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> x\n", encoding="utf-8")

    with pytest.raises(orch.Stop) as raised:
        pipeline.commit_agent_work(paths, pipeline.STAGES[0], "should not happen")

    assert "conflict markers" in raised.value.reason
    assert run_git(where, "rev-list", "--count", "HEAD") == "1"


def test_a_failed_agent_run_commits_nothing(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = paths_for(workspace)
    where = conflicted_worktree(workspace, pipeline.STAGES[0])
    (where / "doc.md").write_text("half written\n", encoding="utf-8")

    def failing_run(argv, *, cwd=None, env=None, timeout=600, stdin_text=None):
        return orch.Ran(tuple(argv), 1, "", "the agent died")

    monkeypatch.setattr(pipeline, "find_agent", lambda: Path("/fake/claude"))
    monkeypatch.setattr(orch, "run", failing_run)
    monkeypatch.setattr(pipeline, "snapshot_refs", lambda _paths: {})

    outcome = pipeline.invoke_agent_guarded(paths, {}, pipeline.STAGES[0], "prompt", "build")

    assert isinstance(outcome, pipeline.AgentRun)
    assert not outcome.ok
    assert run_git(where, "rev-list", "--count", "HEAD") == "1"


# --------------------------------------------------------------------------
# The web publish gate
#
# An earlier web publish verdict was withdrawn after a real browser regression:
# Next.js inline bootstrap scripts blocked by CSP left the published page
# blank. A marker alone therefore does not open this gate.
# --------------------------------------------------------------------------

GOOD_BROWSER_TEST = """
import { test, expect } from '@playwright/test';

test('the published dashboard renders for an authenticated viewer', async ({ page }) => {
  const violations: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error') violations.push(message.text());
  });
  page.on('pageerror', (error) => violations.push(String(error)));
  await page.goto('/');
  await expect(page.getByTestId('equity')).toBeVisible();
  await page.waitForFunction(() => document.documentElement.dataset.hydrated === 'true');
  expect(violations.filter((line) => /Content-Security-Policy/i.test(line))).toEqual([]);
  expect(violations).toEqual([]);
});
"""


def test_the_gate_is_shut_while_the_marker_is_absent(workspace: dict[str, Path]) -> None:
    paths = paths_for(workspace)
    orch.fetch(paths)

    gate = pipeline.evaluate_web(paths)

    assert not gate.ready
    assert gate.verdict == "WAITING (marker not GREEN)"


def test_a_marker_alone_does_not_open_the_gate(workspace: dict[str, Path]) -> None:
    """This is the regression that was actually shipped and withdrawn."""
    publish_file(workspace, pipeline.WEB_PUBLISH.branch, pipeline.WEB_PUBLISH.marker, "GREEN\n")
    paths = paths_for(workspace)
    orch.fetch(paths)

    gate = pipeline.evaluate_web(paths)

    assert not gate.ready
    assert "browser regression unproven" in gate.verdict
    assert set(gate.missing) == {"csp", "console errors", "hydration", "visible render"}


def test_a_browser_test_that_proves_the_regression_opens_the_gate(
    workspace: dict[str, Path],
) -> None:
    publish_file(
        workspace,
        pipeline.WEB_PUBLISH.branch,
        "dashboard/frontend/e2e/published-page.spec.ts",
        GOOD_BROWSER_TEST,
    )
    publish_file(workspace, pipeline.WEB_PUBLISH.branch, pipeline.WEB_PUBLISH.marker, "GREEN\n")
    paths = paths_for(workspace)
    orch.fetch(paths)

    gate = pipeline.evaluate_web(paths)

    assert gate.ready
    assert gate.verdict == "GREEN"
    assert set(gate.proven) == {"csp", "console errors", "hydration", "visible render"}


def test_a_browser_test_missing_the_csp_assertion_keeps_the_gate_shut(
    workspace: dict[str, Path],
) -> None:
    publish_file(
        workspace,
        pipeline.WEB_PUBLISH.branch,
        "dashboard/frontend/e2e/published-page.spec.ts",
        GOOD_BROWSER_TEST.replace("Content-Security-Policy", "SomethingElse"),
    )
    publish_file(workspace, pipeline.WEB_PUBLISH.branch, pipeline.WEB_PUBLISH.marker, "GREEN\n")
    paths = paths_for(workspace)
    orch.fetch(paths)

    gate = pipeline.evaluate_web(paths)

    assert not gate.ready
    assert gate.missing == ["csp"]


def test_the_gate_reads_the_current_remote_head(workspace: dict[str, Path]) -> None:
    """Withdrawing the marker shuts the gate again, as it did in reality."""
    publish_file(
        workspace,
        pipeline.WEB_PUBLISH.branch,
        "dashboard/frontend/e2e/published-page.spec.ts",
        GOOD_BROWSER_TEST,
    )
    publish_file(workspace, pipeline.WEB_PUBLISH.branch, pipeline.WEB_PUBLISH.marker, "GREEN\n")
    source = workspace["source"]
    run_git(source, "checkout", "-q", pipeline.WEB_PUBLISH.branch)
    run_git(source, "rm", "-q", pipeline.WEB_PUBLISH.marker)
    run_git(source, "commit", "-q", "-m", "withdraw")
    run_git(source, "push", "-q", "origin", pipeline.WEB_PUBLISH.branch)
    run_git(source, "checkout", "-q", "main")
    paths = paths_for(workspace)
    orch.fetch(paths)

    assert not pipeline.evaluate_web(paths).ready


def test_web_publish_never_blocks_the_earlier_stages() -> None:
    """Only the final integration waits for the web branch."""
    waiting = [stage for stage in pipeline.STAGES if stage.kind == "merge"]

    assert [stage.key for stage in waiting] == ["final"]


# --------------------------------------------------------------------------
# The production boundary
#
# Asserted against the argument vectors the module can construct, the same way
# the orchestrator's are, because prose here describes prohibitions rather than
# performing them.
# --------------------------------------------------------------------------


def pipeline_tree() -> ast.Module:
    return ast.parse(PIPELINE_PATH.read_text(encoding="utf-8"))


def git_calls() -> list[list[str]]:
    vectors: list[list[str]] = []
    for node in ast.walk(pipeline_tree()):
        if not isinstance(node, ast.Call):
            continue
        name = ""
        if isinstance(node.func, ast.Attribute):
            name = node.func.attr
        elif isinstance(node.func, ast.Name):
            name = node.func.id
        if name not in {"git", "git_ok"}:
            continue
        vectors.append(
            [
                argument.value
                for argument in node.args[1:]
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            ]
        )
    return vectors


def git_vocabulary() -> set[str]:
    return {word for vector in git_calls() for word in vector}


def test_the_git_vocabulary_is_small_and_known() -> None:
    subcommands = {vector[0] for vector in git_calls() if vector}

    assert subcommands == {
        "add",
        "commit",
        "diff",
        "for-each-ref",
        "ls-files",
        "ls-tree",
        "merge",
        "merge-base",
        "push",
        "rev-parse",
        "worktree",
    }


@pytest.mark.parametrize(
    "forbidden",
    [
        "--force",
        "--force-with-lease",
        "reset",
        "rebase",
        "cherry-pick",
        "--squash",
        "filter-branch",
        "checkout",
        "switch",
        "clean",
        "branch",
        "update-ref",
        "remote",
        "config",
        "stash",
        "restore",
        "rm",
        "revert",
        "--ours",
        "--theirs",
        "-X",
    ],
)
def test_the_pipeline_can_hand_git_no_destructive_word(forbidden: str) -> None:
    assert forbidden not in git_vocabulary()


def test_the_only_merge_is_a_normal_no_fast_forward_merge() -> None:
    merges = [vector for vector in git_calls() if vector and vector[0] == "merge"]

    assert merges == [["merge", "--no-ff", "--no-edit", "-m"]]


def test_the_pipeline_creates_worktrees_and_removes_none() -> None:
    worktrees = {tuple(vector) for vector in git_calls() if vector and vector[0] == "worktree"}

    assert worktrees == {("worktree", "add", "-b"), ("worktree", "list", "--porcelain")}
    assert "remove" not in git_vocabulary()


def test_the_pipeline_names_main_in_no_executable_statement() -> None:
    code = ast.unparse(pipeline_tree())

    assert "'main'" not in code
    assert "origin/main" not in code


def test_the_pipeline_pushes_only_stage_branches() -> None:
    """Every push is a stage's own refspec; no other ref can be published."""
    source = PIPELINE_PATH.read_text(encoding="utf-8")
    pushes = [vector for vector in git_calls() if vector and vector[0] == "push"]

    assert len(pushes) == 1
    assert 'f"refs/heads/{stage.branch}:refs/heads/{stage.branch}"' in source


def argv_heads() -> set[str]:
    """What this module can put at the head of an argument vector.

    A constant head is that program's name. A computed head is the coding agent
    this module located, and is reported as "the located agent". Lists whose
    head is a sentence are not argument vectors.
    """
    heads: set[str] = set()
    for node in ast.walk(pipeline_tree()):
        if not isinstance(node, ast.List) or not node.elts:
            continue
        head = node.elts[0]
        if isinstance(head, ast.Constant):
            if isinstance(head.value, str) and " " not in head.value:
                heads.add(head.value)
        elif isinstance(head, ast.Attribute | ast.Call | ast.Name):
            heads.add("the located agent")
    return heads


def test_the_pipeline_can_execute_nothing_but_the_agent_it_located() -> None:
    """Every other command this pipeline runs goes through the orchestrator."""
    assert argv_heads() == {"the located agent"}


def test_no_broker_symbol_appears_in_an_executable_statement() -> None:
    code = ast.unparse(pipeline_tree())

    for symbol in ("TradingClient", "submit_order", "MarketOrderRequest", "alpaca."):
        assert symbol not in code
    assert "ALPACA_API_KEY" not in code


def test_the_pipeline_never_bypasses_the_permission_system() -> None:
    code = PIPELINE_PATH.read_text(encoding="utf-8")

    assert "dangerously" not in code.lower()
    assert "bypassPermissions" not in code


def test_the_repair_budget_is_three() -> None:
    assert pipeline.MAX_REPAIR_ATTEMPTS == 3


@pytest.mark.parametrize(
    "line",
    [
        "<<<<<<< HEAD",
        ">>>>>>> 7139db61f011de05fe46dfda5fbccb3c23d0a256",
        "<<<<<<< feat/quant-research",
    ],
)
def test_a_real_conflict_marker_is_recognised(line: str) -> None:
    assert pipeline.is_conflict_marker(line)


@pytest.mark.parametrize(
    "line",
    [
        "===========  =======================================  ====================",
        "=======",
        "=====================",
        "<<<<<<<<<<<<<<<< a decorative rule",
        ">>>>>>>>>>>> another one",
        ">>> a doctest",
        "<<< nothing",
        "# ======= section =======",
        "",
    ],
)
def test_ordinary_prose_is_not_mistaken_for_a_conflict_marker(line: str) -> None:
    """A reStructuredText table separator is not an unresolved merge."""
    assert not pipeline.is_conflict_marker(line)


def test_a_docstring_table_does_not_block_a_commit(workspace: dict[str, Path]) -> None:
    """The exact false positive that stopped a real integration."""
    paths = paths_for(workspace)
    where = conflicted_worktree(workspace, pipeline.STAGES[0])
    (where / "doc.md").write_text(
        '"""\n'
        "===========  ==========\n"
        "Window       Outcome\n"
        "===========  ==========\n"
        "1            fine\n"
        "===========  ==========\n"
        '"""\n',
        encoding="utf-8",
    )

    assert pipeline.marker_bearing_files(where) == []
    assert pipeline.commit_agent_work(paths, pipeline.STAGES[0], "ordinary work")


# --------------------------------------------------------------------------
# Recovery
#
# The pipeline must survive a process that died at any point, without either
# repeating a finished stage or mistaking its own half-finished work for
# somebody else's branch.
# --------------------------------------------------------------------------


def merging_worktree(workspace: dict[str, Path]) -> Path:
    """A worktree left mid-merge, the way a killed process leaves one."""
    where = workspace["qa_root"] / "worktrees" / "v4-prep-integration"
    subprocess.run(["git", "init", "-q", "-b", "main", str(where)], check=True)
    run_git(where, "config", "user.email", "t@example.invalid")
    run_git(where, "config", "user.name", "T")
    (where / "doc.md").write_text("base\n", encoding="utf-8")
    run_git(where, "add", "doc.md")
    run_git(where, "commit", "-q", "-m", "base")
    run_git(where, "checkout", "-q", "-b", "side")
    (where / "doc.md").write_text("theirs\n", encoding="utf-8")
    run_git(where, "commit", "-q", "-am", "theirs")
    run_git(where, "checkout", "-q", "main")
    (where / "doc.md").write_text("ours\n", encoding="utf-8")
    run_git(where, "commit", "-q", "-am", "ours")
    subprocess.run(["git", "-C", str(where), "merge", "side"], capture_output=True, check=False)
    return where


def test_an_interrupted_merge_is_visible_after_the_conflict_is_staged(
    workspace: dict[str, Path],
) -> None:
    """Staged-but-uncommitted looks clean to --diff-filter=U; MERGE_HEAD does not."""
    where = merging_worktree(workspace)
    assert pipeline.unmerged_paths(where) == ["doc.md"]

    (where / "doc.md").write_text("ours\ntheirs\n", encoding="utf-8")
    run_git(where, "add", "doc.md")

    assert pipeline.unmerged_paths(where) == []
    assert pipeline.merge_in_progress(where) is not None


def test_a_finished_merge_reports_no_pending_merge(workspace: dict[str, Path]) -> None:
    where = merging_worktree(workspace)
    (where / "doc.md").write_text("ours\ntheirs\n", encoding="utf-8")
    run_git(where, "add", "doc.md")
    run_git(where, "commit", "-q", "-m", "merged")

    assert pipeline.merge_in_progress(where) is None


def test_a_worktree_that_was_never_started_is_not_resumed(workspace: dict[str, Path]) -> None:
    paths = paths_for(workspace)

    assert pipeline.resume_prep_conflict(paths, {}) is None


def test_a_worktree_on_another_branch_is_not_resumed(workspace: dict[str, Path]) -> None:
    """Only a worktree actually sitting on the integration branch is ours."""
    paths = paths_for(workspace)
    merging_worktree(workspace)

    assert pipeline.resume_prep_conflict(paths, {}) is None


# --------------------------------------------------------------------------
# The bounded repair loop
# --------------------------------------------------------------------------


def stub_validation(monkeypatch: pytest.MonkeyPatch, outcomes: list[bool]) -> dict[str, int]:
    """Make validation return a scripted sequence, counting agent hand-backs."""
    counts = {"validated": 0, "agent": 0}

    def fake_validate(_paths, attempt):
        index = min(counts["validated"], len(outcomes) - 1)
        passing = outcomes[index]
        counts["validated"] += 1
        attempt.validations = [
            {
                "name": "ruff format --check .",
                "command": "ruff format --check .",
                "exit_code": 0 if passing else 1,
                "passed": passing,
                "tail": ["1 file would be reformatted"],
            }
        ]
        attempt.invariants = [{"invariant": "broker paper account only", "status": "guarded"}]
        return passing

    def fake_agent(_paths, _state, _stage, _prompt, label):
        counts["agent"] += 1
        return pipeline.AgentRun(True, 0, "fixed", "s", 0.0, [], Path("p"), Path("o"))

    monkeypatch.setattr(orch, "validate", fake_validate)
    monkeypatch.setattr(pipeline, "invoke_agent_guarded", fake_agent)
    monkeypatch.setattr(orch, "finish", lambda *_args, **_kwargs: None)
    return counts


def prep_attempt() -> orch.Attempt:
    attempt = orch.Attempt(started_at=orch.utc_now(), base_sha="b" * 40, sources=(), readiness=[])
    attempt.integration_sha = "c" * 40
    return attempt


def test_a_repairable_failure_is_handed_back_and_then_published(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = paths_for(workspace)
    counts = stub_validation(monkeypatch, [False, True])
    monkeypatch.setattr(orch, "write_provenance", lambda *_a, **_k: None)
    monkeypatch.setattr(orch, "publish", lambda *_a, **_k: None)
    state: dict[str, object] = {}

    code = pipeline.finish_prep(paths, state, prep_attempt())

    assert code == orch.EXIT_OK
    assert counts["agent"] == 1
    assert pipeline.load_pipeline(paths)["stages"]["v4-prep"]["state"] == "V4_PREP_GREEN"


def test_the_repair_loop_gives_up_after_three_attempts(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = paths_for(workspace)
    counts = stub_validation(monkeypatch, [False])
    state: dict[str, object] = {}

    code = pipeline.finish_prep(paths, state, prep_attempt())

    assert code == orch.EXIT_MANUAL_REVIEW
    assert counts["agent"] == pipeline.MAX_REPAIR_ATTEMPTS
    assert pipeline.stopped(state)
    assert "3 repair attempts" in state["hard_stop"]["reason"]


def test_an_unrepairable_failure_is_never_handed_back(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = paths_for(workspace)
    counts = stub_validation(monkeypatch, [False])

    def unsafe_validate(_paths, attempt):
        attempt.validations = [
            {
                "name": "pytest -q",
                "command": "pytest -q",
                "exit_code": 1,
                "passed": False,
                "tail": ["FAILED test_the_trading_client_is_always_constructed_with_paper_true"],
            }
        ]
        attempt.invariants = []
        return False

    monkeypatch.setattr(orch, "validate", unsafe_validate)
    state: dict[str, object] = {}

    code = pipeline.finish_prep(paths, state, prep_attempt())

    assert code == orch.EXIT_MANUAL_REVIEW
    assert counts["agent"] == 0
    assert "trading-safety" in state["hard_stop"]["reason"]


def test_nothing_is_published_while_validation_is_red(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = paths_for(workspace)
    stub_validation(monkeypatch, [False])
    published: list[str] = []
    monkeypatch.setattr(orch, "publish", lambda *_a, **_k: published.append("pushed"))

    pipeline.finish_prep(paths, state := {}, prep_attempt())

    assert published == []
    assert pipeline.stopped(state)
