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
        "rev-list",
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


def test_a_stop_raised_by_the_driver_is_keyed_to_the_pipeline(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not to whatever state happened to be current, which is not a stage."""
    paths = paths_for(workspace)
    monkeypatch.setattr(
        pipeline, "advance_v4_prep", lambda *_a: (_ for _ in ()).throw(orch.Stop("boom"))
    )

    assert pipeline.step(paths) == orch.EXIT_MANUAL_REVIEW

    stored = pipeline.load_pipeline(paths)
    assert set(stored["stages"]) == {"pipeline"}
    assert stored["hard_stop"]["stage"] == "pipeline"


# --------------------------------------------------------------------------
# The orchestrator's own upgrade is not a stage agent's trespass
#
# A stage can take half an hour. On 2026-08-29 the orchestrator was extended,
# committed, pushed and reinstalled while the V4 agent was building - and the
# containment guard, whose baseline predated that upgrade, attributed the
# orchestrator's own branch moving to the V4 agent and hard-stopped a stage
# that had done nothing wrong.
#
# The upgrade is now excused only when it is *proved*: it must land exactly on
# the revision the running copy was installed from, and be a fast-forward.
# --------------------------------------------------------------------------

ORCHESTRATOR_BRANCH = "feat/auto-integration-orchestrator"
ORCHESTRATOR_WORKTREE = "/Volumes/AUTOTRADER_QA/worktrees/auto-integrator"
OLD_ORCHESTRATOR = "a2170ea9f6faf832be22478c1037a6d1e575b468"
NEW_ORCHESTRATOR = "c6c6a41ebb8b4a751d7b64bd90f5b51d07ed5642"
V4_BASE = "3c6f590c084ed8467a3b867edad3524756f2edc6"
V4_HEAD = "00d318187e7811fc068a63fe894ebed25fc06a4e"
V4_SCOPE = ("feat/decision-v4", "/Volumes/AUTOTRADER_QA/worktrees/decision-v4")


def identity(installed: str | None) -> object:
    return pipeline.SelfIdentity(
        branch=ORCHESTRATOR_BRANCH,
        worktree=ORCHESTRATOR_WORKTREE,
        installed_commit=installed,
    )


def orchestrator_upgrade_snapshots() -> tuple[dict[str, str], dict[str, str]]:
    """The exact before/after of the real false positive."""
    before = {
        f"refs/heads/{ORCHESTRATOR_BRANCH}": OLD_ORCHESTRATOR,
        f"refs/remotes/origin/{ORCHESTRATOR_BRANCH}": OLD_ORCHESTRATOR,
        f"worktree:{ORCHESTRATOR_WORKTREE}": OLD_ORCHESTRATOR,
        "refs/heads/feat/decision-v4": V4_BASE,
        "worktree:/Volumes/AUTOTRADER_QA/worktrees/decision-v4": V4_BASE,
        "refs/heads/main": "2d74fce20f20443210ac832e1bfecfc994545edb",
    }
    after = dict(before)
    after[f"refs/heads/{ORCHESTRATOR_BRANCH}"] = NEW_ORCHESTRATOR
    after[f"refs/remotes/origin/{ORCHESTRATOR_BRANCH}"] = NEW_ORCHESTRATOR
    after[f"worktree:{ORCHESTRATOR_WORKTREE}"] = NEW_ORCHESTRATOR
    after["refs/heads/feat/decision-v4"] = V4_HEAD
    after["worktree:/Volumes/AUTOTRADER_QA/worktrees/decision-v4"] = V4_HEAD
    return before, after


def test_a_proven_orchestrator_self_upgrade_does_not_hard_stop_a_stage() -> None:
    """The reproduction: old SHA -> legitimate extension + install -> V4 build."""
    before, after = orchestrator_upgrade_snapshots()

    breach = pipeline.containment_breach(
        before,
        after,
        allowed=V4_SCOPE,
        identity=identity(NEW_ORCHESTRATOR),
        fast_forward=lambda old, new: (old, new) == (OLD_ORCHESTRATOR, NEW_ORCHESTRATOR),
    )

    assert breach == []


def test_without_the_identity_the_same_snapshots_still_read_as_a_breach() -> None:
    """The old behaviour, kept explicit so the fix is visibly the discriminator."""
    before, after = orchestrator_upgrade_snapshots()

    breach = pipeline.containment_breach(before, after, allowed=V4_SCOPE)

    assert len(breach) == 3
    assert all(ORCHESTRATOR_BRANCH in line or "auto-integrator" in line for line in breach)


def test_an_orchestrator_move_to_an_uninstalled_revision_is_a_breach() -> None:
    """A branch that moved with no install behind it is not a self-upgrade."""
    before, after = orchestrator_upgrade_snapshots()

    breach = pipeline.containment_breach(
        before,
        after,
        allowed=V4_SCOPE,
        identity=identity("deadbeef" * 5),
        fast_forward=lambda _old, _new: True,
    )

    assert len(breach) == 3


def test_an_orchestrator_move_that_is_not_a_fast_forward_is_a_breach() -> None:
    """A rewind or a rewrite is never excused, whatever is installed."""
    before, after = orchestrator_upgrade_snapshots()

    breach = pipeline.containment_breach(
        before,
        after,
        allowed=V4_SCOPE,
        identity=identity(NEW_ORCHESTRATOR),
        fast_forward=lambda _old, _new: False,
    )

    assert len(breach) == 3


def test_an_orchestrator_with_no_recorded_install_excuses_nothing() -> None:
    before, after = orchestrator_upgrade_snapshots()

    breach = pipeline.containment_breach(
        before,
        after,
        allowed=V4_SCOPE,
        identity=identity(None),
        fast_forward=lambda _old, _new: True,
    )

    assert len(breach) == 3


@pytest.mark.parametrize(
    ("ref", "name"),
    [
        ("refs/heads/main", "main"),
        ("refs/heads/feat/quant-research", "another feature branch"),
        ("refs/heads/feat/decision-v5", "a later stage's branch"),
        ("worktree:/Volumes/AUTOTRADER_QA/worktrees/ml-foundation", "another worktree"),
    ],
)
def test_a_stage_agent_touching_anything_else_still_hard_stops(ref: str, name: str) -> None:
    """The protection is not weakened: only the orchestrator's own is excused."""
    before, after = orchestrator_upgrade_snapshots()
    before[ref] = "1" * 40
    after[ref] = "2" * 40

    breach = pipeline.containment_breach(
        before,
        after,
        allowed=V4_SCOPE,
        identity=identity(NEW_ORCHESTRATOR),
        fast_forward=lambda old, new: (old, new) == (OLD_ORCHESTRATOR, NEW_ORCHESTRATOR),
    )

    assert breach == [f"{ref}: {'1' * 40} -> {'2' * 40}"], name


def test_a_stage_agent_committing_on_the_orchestrator_branch_still_hard_stops() -> None:
    """The agent moving it somewhere other than the installed revision is caught."""
    before, after = orchestrator_upgrade_snapshots()
    forged = "f" * 40
    after[f"refs/heads/{ORCHESTRATOR_BRANCH}"] = forged

    breach = pipeline.containment_breach(
        before,
        after,
        allowed=V4_SCOPE,
        identity=identity(NEW_ORCHESTRATOR),
        fast_forward=lambda old, new: (old, new) == (OLD_ORCHESTRATOR, NEW_ORCHESTRATOR),
    )

    assert breach == [f"refs/heads/{ORCHESTRATOR_BRANCH}: {OLD_ORCHESTRATOR} -> {forged}"]


def test_the_identity_only_owns_its_own_refs() -> None:
    who = identity(NEW_ORCHESTRATOR)

    assert who.owns(f"refs/heads/{ORCHESTRATOR_BRANCH}")
    assert who.owns(f"refs/remotes/origin/{ORCHESTRATOR_BRANCH}")
    assert who.owns(f"worktree:{ORCHESTRATOR_WORKTREE}")
    assert not who.owns("refs/heads/main")
    assert not who.owns("refs/heads/feat/decision-v4")
    assert not who.owns("worktree:/Volumes/AUTOTRADER_QA/worktrees/decision-v4")


def test_the_identity_is_read_from_the_installed_provenance(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install = tmp_path / "install"
    install.mkdir()
    (install / "INSTALLED_FROM.json").write_text(
        json.dumps({"source_branch": ORCHESTRATOR_BRANCH, "source_commit": NEW_ORCHESTRATOR}),
        encoding="utf-8",
    )
    monkeypatch.setattr(pipeline, "INSTALL_DIR", install)

    who = pipeline.orchestrator_identity(paths_for(workspace))

    assert who.branch == ORCHESTRATOR_BRANCH
    assert who.installed_commit == NEW_ORCHESTRATOR
    assert who.worktree == str(workspace["host"])


def test_a_missing_install_record_yields_an_identity_that_excuses_nothing(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pipeline, "INSTALL_DIR", tmp_path / "nowhere")

    who = pipeline.orchestrator_identity(paths_for(workspace))

    assert who.installed_commit is None
    assert who.branch is None


# --------------------------------------------------------------------------
# Lifting a hard stop without throwing away finished work
# --------------------------------------------------------------------------


def built_stage_worktree(workspace: dict[str, Path], stage) -> tuple[Path, str]:
    """A stage worktree whose branch already carries a build commit."""
    where = workspace["qa_root"] / "worktrees" / stage.worktree
    subprocess.run(["git", "init", "-q", "-b", stage.branch, str(where)], check=True)
    run_git(where, "config", "user.email", "t@example.invalid")
    run_git(where, "config", "user.name", "T")
    (where / "base.txt").write_text("base\n", encoding="utf-8")
    run_git(where, "add", "base.txt")
    run_git(where, "commit", "-q", "-m", "base")
    base = run_git(where, "rev-parse", "HEAD")
    (where / "built.py").write_text("# the stage's work\n", encoding="utf-8")
    run_git(where, "add", "built.py")
    run_git(where, "commit", "-q", "-m", "the build")
    return where, base


def test_clearing_a_stop_resumes_a_built_stage_at_validation(
    workspace: dict[str, Path],
) -> None:
    """The V4 recovery: 32 minutes of committed work is not rebuilt."""
    paths = paths_for(workspace)
    stage = pipeline.STAGE_BY_KEY["v4"]
    _where, base = built_stage_worktree(workspace, stage)
    pipeline.save_pipeline(
        paths,
        {
            "state": pipeline.HARD_STOP,
            "hard_stop": {"stage": "v4", "reason": "the agent changed git state"},
            "stages": {
                "v4-prep": {"state": "V4_PREP_GREEN", "head_sha": "a" * 40},
                "v4": {
                    "state": pipeline.HARD_STOP,
                    "hard_stop_reason": "the agent changed git state",
                    "hard_stop_evidence": ["something"],
                    "base_sha": base,
                    "repair_attempts": 0,
                },
            },
        },
    )

    said = pipeline.clear_hard_stop(paths)

    state = pipeline.load_pipeline(paths)
    assert state["stages"]["v4"]["state"] == "V4_VALIDATING"
    assert state["stages"]["v4"]["repair_attempts"] == 0
    assert "hard_stop_reason" not in state["stages"]["v4"]
    assert "hard_stop" not in state
    assert state["stages"]["v4-prep"]["state"] == "V4_PREP_GREEN"
    assert any("resuming at validation" in line for line in said)


def test_clearing_a_stop_forgets_a_stage_that_never_built(
    workspace: dict[str, Path],
) -> None:
    paths = paths_for(workspace)
    pipeline.save_pipeline(
        paths,
        {
            "state": pipeline.HARD_STOP,
            "hard_stop": {"stage": "v4", "reason": "no worktree"},
            "stages": {"v4": {"state": pipeline.HARD_STOP, "base_sha": "b" * 40}},
        },
    )

    pipeline.clear_hard_stop(paths)

    assert "v4" not in pipeline.load_pipeline(paths)["stages"]


def test_clearing_a_stop_prunes_a_record_that_names_no_stage(
    workspace: dict[str, Path],
) -> None:
    """The stray REPAIRING record the earlier driver bug left behind."""
    paths = paths_for(workspace)
    pipeline.save_pipeline(
        paths,
        {
            "state": pipeline.HARD_STOP,
            "stages": {
                "REPAIRING": {"state": pipeline.HARD_STOP},
                "v4-prep": {"state": "V4_PREP_GREEN", "head_sha": "a" * 40},
            },
        },
    )

    pipeline.clear_hard_stop(paths)

    stages = pipeline.load_pipeline(paths)["stages"]
    assert set(stages) == {"v4-prep"}


def test_clearing_a_stop_never_touches_a_green_stage(workspace: dict[str, Path]) -> None:
    paths = paths_for(workspace)
    pipeline.save_pipeline(
        paths,
        {
            "state": pipeline.HARD_STOP,
            "stages": {
                "v4-prep": {
                    "state": "V4_PREP_GREEN",
                    "head_sha": "3c6f590c084ed8467a3b867edad3524756f2edc6",
                    "repair_attempts": 1,
                }
            },
        },
    )

    pipeline.clear_hard_stop(paths)

    prep = pipeline.load_pipeline(paths)["stages"]["v4-prep"]
    assert prep["state"] == "V4_PREP_GREEN"
    assert prep["head_sha"] == "3c6f590c084ed8467a3b867edad3524756f2edc6"
    assert prep["repair_attempts"] == 1


def test_a_resumed_stage_skips_the_build_and_goes_straight_to_validation(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state left by clear-hard-stop must not re-invoke the build agent."""
    paths = paths_for(workspace)
    stage = pipeline.STAGE_BY_KEY["v4"]
    _where, base = built_stage_worktree(workspace, stage)
    calls: list[str] = []
    monkeypatch.setattr(pipeline, "stage_green_sha", lambda _p, s: None)
    monkeypatch.setattr(pipeline, "base_sha_for", lambda _p, _s: base)
    monkeypatch.setattr(pipeline, "prepare_stage", lambda _p, _s, _b: "resuming")
    monkeypatch.setattr(
        pipeline,
        "invoke_agent_guarded",
        lambda *_a: (
            calls.append("agent")
            or pipeline.AgentRun(True, 0, "", "", 0.0, [], Path("p"), Path("o"))
        ),
    )
    monkeypatch.setattr(
        pipeline, "validate_and_repair", lambda *_a: calls.append("validate") or orch.EXIT_OK
    )
    state = {"stages": {"v4": {"state": "V4_VALIDATING", "base_sha": base, "repair_attempts": 0}}}

    pipeline.run_build_stage(paths, state, stage)

    assert calls == ["validate"]


def test_the_identity_falls_back_to_the_conventional_install_location(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A manual run from a worktree is measured against the same pin launchd uses."""
    monkeypatch.setattr(pipeline, "INSTALL_DIR", tmp_path / "not-installed-here")
    install = workspace["qa_root"] / "integration-orchestrator"
    install.mkdir(parents=True, exist_ok=True)
    (install / "INSTALLED_FROM.json").write_text(
        json.dumps({"source_branch": ORCHESTRATOR_BRANCH, "source_commit": NEW_ORCHESTRATOR}),
        encoding="utf-8",
    )

    who = pipeline.orchestrator_identity(paths_for(workspace))

    assert who.installed_commit == NEW_ORCHESTRATOR
    assert who.branch == ORCHESTRATOR_BRANCH


def test_an_install_record_without_a_commit_is_ignored(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A half-written record must not become a pin that excuses anything."""
    empty = tmp_path / "install"
    empty.mkdir()
    (empty / "INSTALLED_FROM.json").write_text(json.dumps({"source_branch": "x"}), encoding="utf-8")
    monkeypatch.setattr(pipeline, "INSTALL_DIR", empty)

    who = pipeline.orchestrator_identity(paths_for(workspace))

    assert who.installed_commit is None
