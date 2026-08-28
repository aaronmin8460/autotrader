"""Tests for the local integration orchestrator.

The orchestrator is infrastructure: it decides whether three feature branches
may be merged, and its failure mode has to be "do nothing" rather than "merge
something plausible". These tests exercise that decision against real git
repositories built in a temporary directory, so readiness parsing, provenance
matching and locking are checked against the tool git actually is rather than
against a mock of it.

Nothing here contacts a network, a broker or the real `origin`, and nothing
here writes a readiness marker onto a real development branch.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR_PATH = REPO_ROOT / "tools" / "integration" / "orchestrator.py"


def load_orchestrator() -> ModuleType:
    """Import the orchestrator by path; `tools/` is scripts, not a package."""
    spec = importlib.util.spec_from_file_location("_integration_orchestrator", ORCHESTRATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


orchestrator = load_orchestrator()


# --------------------------------------------------------------------------
# Git fixtures
#
# A bare "remote" plus a working clone, so remote heads and marker blobs are
# real objects rather than strings a test asserts about itself.
# --------------------------------------------------------------------------


def run_git(where: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(where), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    """A bare remote, a clone of it, and the external directory layout."""
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    host = tmp_path / "host"
    qa_root = tmp_path / "qa"

    for name in ("worktrees", "reports", "logs", "tmp", "caches"):
        (qa_root / name).mkdir(parents=True, exist_ok=True)

    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    run_git(source, "config", "user.email", "test@example.invalid")
    run_git(source, "config", "user.name", "Integration Test")
    (source / "README.md").write_text("base\n", encoding="utf-8")
    run_git(source, "add", "README.md")
    run_git(source, "commit", "-q", "-m", "base")
    run_git(source, "remote", "add", "origin", str(remote))
    run_git(source, "push", "-q", "origin", "main")

    for branch, _marker in orchestrator.SOURCES:
        run_git(source, "checkout", "-q", "-b", branch, "main")
        run_git(source, "push", "-q", "origin", branch)
        run_git(source, "checkout", "-q", "main")
    run_git(source, "checkout", "-q", "-b", orchestrator.BASE_BRANCH, "main")
    run_git(source, "push", "-q", "origin", orchestrator.BASE_BRANCH)
    run_git(source, "checkout", "-q", "main")

    subprocess.run(["git", "clone", "-q", str(remote), str(host)], check=True)
    run_git(host, "config", "user.email", "test@example.invalid")
    run_git(host, "config", "user.name", "Integration Test")

    return {"remote": remote, "source": source, "host": host, "qa_root": qa_root}


def paths_for(workspace: dict[str, Path]):
    qa_root = workspace["qa_root"]
    return orchestrator.Paths(
        qa_root=qa_root,
        git_host=workspace["host"],
        integration_worktree=qa_root / "worktrees" / "v4-prep-integration",
        state_dir=qa_root / "logs" / "integration-orchestrator",
        reports_dir=qa_root / "reports",
    )


def publish_marker(workspace: dict[str, Path], branch: str, marker: str, content: str) -> str:
    """Write a readiness marker onto a branch of the temporary remote."""
    source = workspace["source"]
    run_git(source, "checkout", "-q", branch)
    target = source / marker
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    run_git(source, "add", marker)
    run_git(source, "commit", "-q", "-m", f"mark {branch}")
    sha = run_git(source, "rev-parse", "HEAD")
    run_git(source, "push", "-q", "origin", branch)
    run_git(source, "checkout", "-q", "main")
    return sha


# --------------------------------------------------------------------------
# The readiness protocol
# --------------------------------------------------------------------------


def test_the_merge_order_is_the_agreed_deterministic_order() -> None:
    assert [branch for branch, _marker in orchestrator.SOURCES] == [
        "feat/decision-v2-v3",
        "feat/quant-research",
        "feat/ml-foundation",
    ]


def test_every_source_marker_is_the_agreed_path() -> None:
    assert dict(orchestrator.SOURCES) == {
        "feat/decision-v2-v3": ".autotrader-ready/decision-v2-v3",
        "feat/quant-research": ".autotrader-ready/quant-research",
        "feat/ml-foundation": ".autotrader-ready/ml-foundation",
    }


def test_a_branch_with_no_marker_is_not_ready(workspace: dict[str, Path]) -> None:
    found = orchestrator.evaluate(paths_for(workspace))

    assert [entry.ready for entry in found] == [False, False, False]
    assert all(entry.verdict == "NOT_READY (marker absent)" for entry in found)


def test_a_marker_reading_exactly_green_is_ready(workspace: dict[str, Path]) -> None:
    for branch, marker in orchestrator.SOURCES:
        publish_marker(workspace, branch, marker, "GREEN\n")

    found = orchestrator.evaluate(paths_for(workspace))

    assert all(entry.ready for entry in found)
    assert all(entry.verdict == "READY" for entry in found)


def test_a_marker_without_a_trailing_newline_is_ready(workspace: dict[str, Path]) -> None:
    branch, marker = orchestrator.SOURCES[0]
    publish_marker(workspace, branch, marker, "GREEN")

    found = orchestrator.evaluate(paths_for(workspace))

    assert found[0].ready


@pytest.mark.parametrize(
    "content",
    ["green\n", "GREEN GREEN\n", "GREEN\nnot really\n", "", "\n", "AMBER\n", " GREENISH \n"],
)
def test_a_marker_that_is_not_green_is_not_ready(workspace: dict[str, Path], content: str) -> None:
    branch, marker = orchestrator.SOURCES[0]
    publish_marker(workspace, branch, marker, content)

    found = orchestrator.evaluate(paths_for(workspace))

    assert not found[0].ready
    assert found[0].verdict == "NOT_READY (marker is not exactly GREEN)"


def test_readiness_follows_the_remote_head_not_an_older_commit(
    workspace: dict[str, Path],
) -> None:
    """A marker that a later commit removed does not keep the branch ready."""
    branch, marker = orchestrator.SOURCES[0]
    publish_marker(workspace, branch, marker, "GREEN\n")
    source = workspace["source"]
    run_git(source, "checkout", "-q", branch)
    run_git(source, "rm", "-q", marker)
    run_git(source, "commit", "-q", "-m", "withdraw readiness")
    run_git(source, "push", "-q", "origin", branch)
    run_git(source, "checkout", "-q", "main")

    found = orchestrator.evaluate(paths_for(workspace))

    assert not found[0].ready


def test_a_local_marker_never_makes_a_branch_ready(workspace: dict[str, Path]) -> None:
    """Working-tree state is not a readiness signal; only the remote head is."""
    branch, marker = orchestrator.SOURCES[0]
    host = workspace["host"]
    local = host / marker
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("GREEN\n", encoding="utf-8")

    found = orchestrator.evaluate(paths_for(workspace))

    assert not found[0].ready


def test_readiness_records_the_exact_remote_sha(workspace: dict[str, Path]) -> None:
    branch, marker = orchestrator.SOURCES[0]
    expected = publish_marker(workspace, branch, marker, "GREEN\n")

    found = orchestrator.evaluate(paths_for(workspace))

    assert found[0].sha == expected


def test_a_missing_remote_branch_reports_itself_rather_than_failing(
    workspace: dict[str, Path],
) -> None:
    remote = workspace["remote"]
    branch = orchestrator.SOURCES[0][0]
    run_git(remote, "update-ref", "-d", f"refs/heads/{branch}")

    found = orchestrator.evaluate(paths_for(workspace))

    assert found[0].sha is None
    assert found[0].verdict == "NO_REMOTE_BRANCH"
    assert not found[0].ready


# --------------------------------------------------------------------------
# run-once refuses to mutate anything while a source is not ready
# --------------------------------------------------------------------------


def test_run_once_mutates_nothing_while_a_source_is_not_ready(
    workspace: dict[str, Path],
) -> None:
    paths = paths_for(workspace)
    branch, marker = orchestrator.SOURCES[0]
    publish_marker(workspace, branch, marker, "GREEN\n")
    before = {
        name: run_git(workspace["remote"], "rev-parse", name)
        for name in ("main", orchestrator.BASE_BRANCH, *[b for b, _m in orchestrator.SOURCES])
    }

    assert orchestrator.run_once(paths) == orchestrator.EXIT_OK

    after = {name: run_git(workspace["remote"], "rev-parse", name) for name in before}
    assert after == before
    assert not paths.integration_worktree.exists()
    assert not paths.lock_dir.exists()
    branches = run_git(workspace["host"], "branch", "--list", orchestrator.TARGET_BRANCH)
    assert branches == ""
    assert orchestrator.TARGET_BRANCH not in run_git(workspace["remote"], "branch", "--list")


def test_a_waiting_run_records_a_waiting_status(workspace: dict[str, Path]) -> None:
    paths = paths_for(workspace)

    orchestrator.run_once(paths)

    assert "WAITING" in paths.status_file.read_text(encoding="utf-8")
    assert not paths.state_file.exists()


def test_preflight_refuses_when_the_external_workspace_is_absent(tmp_path: Path) -> None:
    paths = orchestrator.Paths(
        qa_root=tmp_path / "gone",
        git_host=tmp_path / "gone" / "host",
        integration_worktree=tmp_path / "gone" / "wt",
        state_dir=tmp_path / "gone" / "state",
        reports_dir=tmp_path / "gone" / "reports",
    )

    with pytest.raises(orchestrator.Stop) as raised:
        orchestrator.preflight(paths)

    assert raised.value.code == orchestrator.EXIT_NO_VOLUME


def test_preflight_refuses_when_the_git_host_is_not_a_worktree(tmp_path: Path) -> None:
    qa_root = tmp_path / "qa"
    for name in ("worktrees", "reports"):
        (qa_root / name).mkdir(parents=True)
    paths = orchestrator.Paths(
        qa_root=qa_root,
        git_host=qa_root / "worktrees" / "not-a-repo",
        integration_worktree=qa_root / "worktrees" / "wt",
        state_dir=qa_root / "logs" / "integration-orchestrator",
        reports_dir=qa_root / "reports",
    )

    with pytest.raises(orchestrator.Stop) as raised:
        orchestrator.preflight(paths)

    assert raised.value.code == orchestrator.EXIT_NO_VOLUME


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


def make_attempt(base: str = "b" * 40) -> object:
    readiness = [
        orchestrator.BranchReadiness(
            branch=branch,
            marker=marker,
            sha=f"{index}" * 40,
            marker_present=True,
            marker_content="GREEN\n",
        )
        for index, (branch, marker) in enumerate(orchestrator.SOURCES, start=1)
    ]
    return orchestrator.freeze(readiness, base)


def test_provenance_matching_needs_the_same_sources_and_base() -> None:
    attempt = make_attempt()
    document = {
        "status": "GREEN",
        "base_sha": attempt.base_sha,
        "sources": attempt.source_shas,
    }

    assert orchestrator.matches(document, attempt)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.update(status="RED"),
        lambda doc: doc.update(base_sha="c" * 40),
        lambda doc: doc["sources"].update({"feat/quant-research": "9" * 40}),
        lambda doc: doc["sources"].pop("feat/ml-foundation"),
        lambda doc: doc["sources"].update({"feat/extra": "8" * 40}),
        lambda doc: doc.update(sources="not a mapping"),
        lambda doc: doc.pop("status"),
    ],
)
def test_provenance_matching_refuses_anything_different(mutate) -> None:
    attempt = make_attempt()
    document = {
        "status": "GREEN",
        "base_sha": attempt.base_sha,
        "sources": dict(attempt.source_shas),
    }
    mutate(document)

    assert not orchestrator.matches(document, attempt)


def test_an_existing_target_without_provenance_stops_for_manual_review(
    workspace: dict[str, Path],
) -> None:
    paths = paths_for(workspace)
    run_git(workspace["source"], "checkout", "-q", "-b", orchestrator.TARGET_BRANCH, "main")
    run_git(workspace["source"], "push", "-q", "origin", orchestrator.TARGET_BRANCH)
    run_git(workspace["source"], "checkout", "-q", "main")
    orchestrator.fetch(paths)

    with pytest.raises(orchestrator.Stop) as raised:
        orchestrator.prepare_worktree(paths, make_attempt())

    assert raised.value.code == orchestrator.EXIT_MANUAL_REVIEW
    assert "provenance" in raised.value.reason
    assert not paths.integration_worktree.exists()


def test_an_existing_target_with_other_provenance_stops_for_manual_review(
    workspace: dict[str, Path],
) -> None:
    paths = paths_for(workspace)
    source = workspace["source"]
    run_git(source, "checkout", "-q", "-b", orchestrator.TARGET_BRANCH, "main")
    document = source / orchestrator.PROVENANCE_PATH
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text(
        json.dumps({"status": "GREEN", "base_sha": "z" * 40, "sources": {}}), encoding="utf-8"
    )
    run_git(source, "add", orchestrator.PROVENANCE_PATH)
    run_git(source, "commit", "-q", "-m", "other provenance")
    run_git(source, "push", "-q", "origin", orchestrator.TARGET_BRANCH)
    run_git(source, "checkout", "-q", "main")
    orchestrator.fetch(paths)

    with pytest.raises(orchestrator.Stop) as raised:
        orchestrator.prepare_worktree(paths, make_attempt())

    assert raised.value.code == orchestrator.EXIT_MANUAL_REVIEW
    assert "different provenance" in raised.value.reason


def test_a_target_recording_this_exact_attempt_is_already_complete(
    workspace: dict[str, Path],
) -> None:
    paths = paths_for(workspace)
    attempt = make_attempt()
    source = workspace["source"]
    run_git(source, "checkout", "-q", "-b", orchestrator.TARGET_BRANCH, "main")
    document = source / orchestrator.PROVENANCE_PATH
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text(
        json.dumps(
            {
                "status": "GREEN",
                "base_sha": attempt.base_sha,
                "sources": attempt.source_shas,
            }
        ),
        encoding="utf-8",
    )
    run_git(source, "add", orchestrator.PROVENANCE_PATH)
    run_git(source, "commit", "-q", "-m", "provenance")
    run_git(source, "push", "-q", "origin", orchestrator.TARGET_BRANCH)
    run_git(source, "checkout", "-q", "main")
    orchestrator.fetch(paths)

    with pytest.raises(orchestrator.Stop) as raised:
        orchestrator.prepare_worktree(paths, attempt)

    assert raised.value.code == orchestrator.EXIT_OK
    assert attempt.outcome == "ALREADY_COMPLETE"


# --------------------------------------------------------------------------
# The lock
#
# mkdir is the primitive because macOS ships no GNU flock. A held lock is
# broken only when its owner is proved gone - never because it looks old.
# --------------------------------------------------------------------------


def test_a_second_holder_cannot_take_a_held_lock(tmp_path: Path) -> None:
    first = orchestrator.Lock(tmp_path / "lock")
    second = orchestrator.Lock(tmp_path / "lock")

    assert first.acquire()[0]
    taken, why = second.acquire()

    assert not taken
    assert f"pid {os.getpid()}" in why
    assert not second.held


def test_releasing_a_lock_lets_the_next_holder_take_it(tmp_path: Path) -> None:
    first = orchestrator.Lock(tmp_path / "lock")
    first.acquire()
    first.release()

    assert orchestrator.Lock(tmp_path / "lock").acquire()[0]


def test_a_lock_records_its_owner(tmp_path: Path) -> None:
    lock = orchestrator.Lock(tmp_path / "lock")
    lock.acquire()

    owner = json.loads(lock.owner_file.read_text(encoding="utf-8"))

    assert owner["pid"] == os.getpid()
    assert owner["host"] == os.uname().nodename
    assert owner["started"]


def test_a_lock_whose_owner_is_gone_is_broken(tmp_path: Path) -> None:
    lock = orchestrator.Lock(tmp_path / "lock")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (tmp_path / "lock").mkdir()
    lock.owner_file.write_text(
        json.dumps(
            {
                "pid": dead.pid,
                "host": os.uname().nodename,
                "started": "Wed Jan  1 00:00:00 2020",
            }
        ),
        encoding="utf-8",
    )

    taken, why = lock.acquire()

    assert taken
    assert "no longer exists" in why


def test_a_lock_owned_by_another_host_is_never_broken(tmp_path: Path) -> None:
    lock = orchestrator.Lock(tmp_path / "lock")
    (tmp_path / "lock").mkdir()
    lock.owner_file.write_text(
        json.dumps({"pid": 1, "host": "some-other-machine", "started": "whenever"}),
        encoding="utf-8",
    )

    taken, why = lock.acquire()

    assert not taken
    assert "another host" in why


def test_a_lock_with_an_unreadable_owner_is_never_broken(tmp_path: Path) -> None:
    lock = orchestrator.Lock(tmp_path / "lock")
    (tmp_path / "lock").mkdir()
    lock.owner_file.write_text("{ not json", encoding="utf-8")

    taken, why = lock.acquire()

    assert not taken
    assert "no readable owner record" in why


def test_a_lock_with_no_owner_record_at_all_is_never_broken(tmp_path: Path) -> None:
    (tmp_path / "lock").mkdir()

    taken, why = orchestrator.Lock(tmp_path / "lock").acquire()

    assert not taken
    assert "no readable owner record" in why


def test_an_old_lock_held_by_a_living_process_is_never_broken(tmp_path: Path) -> None:
    """Age is not evidence. Only a proved-absent owner releases a lock."""
    lock = orchestrator.Lock(tmp_path / "lock")
    (tmp_path / "lock").mkdir()
    lock.owner_file.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": os.uname().nodename,
                "started": orchestrator.process_start(os.getpid()),
                "acquired_at": "1999-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    taken, why = lock.acquire()

    assert not taken
    assert "held by running pid" in why


def test_a_reused_process_id_does_not_protect_a_dead_owner(tmp_path: Path) -> None:
    lock = orchestrator.Lock(tmp_path / "lock")
    (tmp_path / "lock").mkdir()
    lock.owner_file.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": os.uname().nodename,
                "started": "Wed Jan  1 00:00:00 2020",
            }
        ),
        encoding="utf-8",
    )

    taken, why = lock.acquire()

    assert taken
    assert "reused" in why


# --------------------------------------------------------------------------
# Durable state
# --------------------------------------------------------------------------


def test_state_round_trips(workspace: dict[str, Path]) -> None:
    paths = paths_for(workspace)

    orchestrator.save_state(paths, {"outcome": "GREEN", "integration_sha": "a" * 40})

    assert orchestrator.load_state(paths)["outcome"] == "GREEN"


def test_corrupt_state_reads_as_empty_rather_than_raising(workspace: dict[str, Path]) -> None:
    paths = paths_for(workspace)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.state_file.write_text("{ truncated", encoding="utf-8")

    assert orchestrator.load_state(paths) == {}
    assert not orchestrator.already_done(paths)


@pytest.mark.parametrize(
    ("state", "settled"),
    [
        ({"outcome": "GREEN", "integration_sha": "a" * 40}, True),
        ({"outcome": "ALREADY_COMPLETE", "integration_sha": "a" * 40}, True),
        ({"outcome": "GREEN"}, False),
        ({"outcome": "CONFLICT", "integration_sha": "a" * 40}, False),
        ({"outcome": "VALIDATION_FAILED", "integration_sha": "a" * 40}, False),
        ({}, False),
    ],
)
def test_only_a_published_integration_settles_the_watcher(
    workspace: dict[str, Path], state: dict[str, object], settled: bool
) -> None:
    paths = paths_for(workspace)
    orchestrator.save_state(paths, state)

    assert orchestrator.already_done(paths) is settled


def test_a_settled_integration_makes_a_later_run_a_no_op(workspace: dict[str, Path]) -> None:
    """The scheduled agent must stop working once the integration is published."""
    paths = paths_for(workspace)
    orchestrator.save_state(paths, {"outcome": "GREEN", "integration_sha": "a" * 40})

    assert orchestrator.run_once(paths) == orchestrator.EXIT_OK
    assert not paths.integration_worktree.exists()
    assert not paths.lock_dir.exists()


# --------------------------------------------------------------------------
# Validation environment and reporting
# --------------------------------------------------------------------------


def test_validation_never_inherits_broker_credentials(workspace: dict[str, Path]) -> None:
    """A validation run must not be able to reach a broker even by accident."""
    paths = paths_for(workspace)
    os.environ["ALPACA_API_KEY"] = "must-not-propagate"
    os.environ["ALPACA_SECRET_KEY"] = "must-not-propagate"
    try:
        environment = orchestrator.validation_env(paths)
    finally:
        del os.environ["ALPACA_API_KEY"]
        del os.environ["ALPACA_SECRET_KEY"]

    assert "ALPACA_API_KEY" not in environment
    assert "ALPACA_SECRET_KEY" not in environment
    assert "AUTOTRADER_PAPER_TRADING_ENABLED" not in environment


def test_validation_keeps_every_cache_on_the_external_workspace(
    workspace: dict[str, Path],
) -> None:
    paths = paths_for(workspace)

    environment = orchestrator.validation_env(paths)

    for key in ("TMPDIR", "npm_config_cache", "PIP_CACHE_DIR", "PYTHONPYCACHEPREFIX"):
        assert environment[key].startswith(str(paths.qa_root))


def test_a_report_names_the_frozen_revisions_and_the_outcome(
    workspace: dict[str, Path],
) -> None:
    paths = paths_for(workspace)
    attempt = make_attempt()
    attempt.started_at = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
    attempt.outcome = "CONFLICT"
    attempt.conflicts = ["src/autotrader/risk/engine.py"]

    report = orchestrator.write_report(paths, attempt)

    text = report.read_text(encoding="utf-8")
    assert report.name == "v4-prep-integration-20260504-120000.md"
    assert "CONFLICT" in text
    assert attempt.base_sha in text
    for sha in attempt.source_shas.values():
        assert sha in text
    assert "src/autotrader/risk/engine.py" in text
    assert "Nothing was pushed" in text


def test_the_frontend_pipeline_is_driven_by_the_merged_diff() -> None:
    assert orchestrator.FRONTEND_TRIGGERS == (
        "dashboard/frontend/",
        "src/autotrader/dashboard/",
    )


# --------------------------------------------------------------------------
# Safety regression coverage
#
# The orchestrator verifies existing invariants rather than inventing new ones,
# so its invariant table has to name tests this repository actually has. A
# merge that renames or deletes one of these guards fails here loudly, which is
# the intended fail-closed outcome: a guard that no longer runs no longer
# guards anything.
# --------------------------------------------------------------------------

REQUIRED_INVARIANTS = (
    "broker paper account only",
    "no live trading path",
    "UNKNOWN means no retry",
    "durable intent before submission",
    "at-most-once submission",
    "broker truth is authoritative",
    "reconciliation semantics",
    "global account safety",
    "5% per-symbol cap",
    "30% total exposure cap",
    "2% UTC-day loss halt",
    "decision layer cannot reach the broker",
)


def defined_test_names() -> set[str]:
    names: set[str] = set()
    for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names.update(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        )
    return names


def test_the_orchestrator_covers_every_required_invariant() -> None:
    assert tuple(name for name, _tests in orchestrator.SAFETY_INVARIANTS) == REQUIRED_INVARIANTS


def test_every_named_safety_guard_exists() -> None:
    defined = defined_test_names()
    missing = {
        name: [test for test in tests if test not in defined]
        for name, tests in orchestrator.SAFETY_INVARIANTS
    }

    assert not {name: gone for name, gone in missing.items() if gone}


def test_every_invariant_names_more_than_one_guard() -> None:
    assert all(len(tests) >= 2 for _name, tests in orchestrator.SAFETY_INVARIANTS)


# --------------------------------------------------------------------------
# What the orchestrator is forbidden to do
#
# Asserted against the argument vocabulary the module can actually hand to git
# and to other programs, recovered from the syntax tree. A prose scan would
# trip over the report template, which names the constructs it does not use.
# --------------------------------------------------------------------------


def orchestrator_tree() -> ast.Module:
    return ast.parse(ORCHESTRATOR_PATH.read_text(encoding="utf-8"))


def constants(node: ast.AST) -> list[str]:
    return [
        element.value
        for element in getattr(node, "elts", [])
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    ]


def git_calls() -> list[list[str]]:
    """Every argument vector this module can hand to git, first path aside."""
    vectors: list[list[str]] = []
    for node in ast.walk(orchestrator_tree()):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in {"git", "git_ok"}:
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


def programs() -> set[str]:
    """Every program name this module can put at the head of an argument list.

    A list literal is an argument vector when its head is either a bare word -
    no argument vector starts with a sentence - or an interpreter expression
    such as `sys.executable`, which is reported as "python".
    """
    found: set[str] = set()
    for node in ast.walk(orchestrator_tree()):
        if not isinstance(node, ast.List) or not node.elts:
            continue
        head = node.elts[0]
        if isinstance(head, ast.Constant):
            if isinstance(head.value, str) and " " not in head.value:
                found.add(head.value)
        elif isinstance(head, ast.Attribute | ast.Call | ast.Name):
            found.add("python")
    return found


def test_the_git_vocabulary_is_small_and_known() -> None:
    """Every git subcommand this tool can reach, enumerated."""
    subcommands = {vector[0] for vector in git_calls() if vector}

    assert subcommands == {
        "add",
        "cat-file",
        "commit",
        "diff",
        "fetch",
        "merge",
        "push",
        "rev-parse",
        "worktree",
    }


@pytest.mark.parametrize(
    "forbidden",
    [
        "--force",
        "-f",
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
        "gc",
        "prune",
        "stash",
        "restore",
        "rm",
        "revert",
        "apply",
        "am",
    ],
)
def test_the_orchestrator_can_hand_git_no_rewriting_or_destructive_word(
    forbidden: str,
) -> None:
    assert forbidden not in git_vocabulary()


@pytest.mark.parametrize("forbidden", ["--ours", "--theirs", "-X", "--strategy-option", "-s"])
def test_the_orchestrator_never_asks_git_to_pick_a_side(forbidden: str) -> None:
    """A conflict is stopped on, never resolved by choosing ours or theirs."""
    assert forbidden not in git_vocabulary()


def test_the_only_merge_is_a_normal_no_fast_forward_merge() -> None:
    merges = [vector for vector in git_calls() if vector and vector[0] == "merge"]

    assert merges == [["merge", "--no-ff", "--no-edit", "-m"]]


def test_the_orchestrator_pushes_exactly_one_refspec() -> None:
    """The only ref this tool can publish is the integration branch."""
    pushes = [vector for vector in git_calls() if vector and vector[0] == "push"]

    assert len(pushes) == 1
    source = ORCHESTRATOR_PATH.read_text(encoding="utf-8")
    refspec = 'f"refs/heads/{TARGET_BRANCH}:refs/heads/{TARGET_BRANCH}"'
    assert refspec in source


def test_the_orchestrator_names_main_in_no_executable_statement() -> None:
    """`main` is never a branch this tool reads, merges, pushes or checks out."""
    code = ast.unparse(orchestrator_tree())

    assert "'main'" not in code
    assert '"main"' not in code
    assert "origin/main" not in code


def test_the_orchestrator_creates_a_worktree_but_removes_none() -> None:
    worktrees = [vector for vector in git_calls() if vector and vector[0] == "worktree"]

    assert worktrees == [["worktree", "add", "-b"]]


def test_the_orchestrator_runs_no_deployment_or_service_program() -> None:
    """Nothing here reaches a host, a service manager or a broker."""
    forbidden = {
        "ssh",
        "scp",
        "rsync",
        "systemctl",
        "service",
        "launchctl",
        "docker",
        "curl",
        "wget",
        "sudo",
        "autotrader",
        "autotrader-smoke",
    }

    assert not (programs() & forbidden)


def test_the_program_list_is_exactly_what_validation_needs() -> None:
    assert programs() == {"git", "ps", "osascript", "npm", "python"}


def test_no_broker_symbol_appears_in_an_executable_statement() -> None:
    """The only broker names present are the credentials validation strips."""
    code = ast.unparse(orchestrator_tree())

    for symbol in ("TradingClient", "submit_order", "MarketOrderRequest", "alpaca."):
        assert symbol not in code
    assert "ALPACA_API_KEY" in code
    assert "environment.pop" in code


# --------------------------------------------------------------------------
# The merge itself
#
# Exercised against real repositories rather than a mock of git, because the
# properties that matter - the base a branch is cut from, the order the merges
# happen in, that a conflict stops everything - are properties of git.
# --------------------------------------------------------------------------


def commit_on(workspace: dict[str, Path], branch: str, name: str, body: str) -> str:
    source = workspace["source"]
    run_git(source, "checkout", "-q", branch)
    (source / name).write_text(body, encoding="utf-8")
    run_git(source, "add", name)
    run_git(source, "commit", "-q", "-m", f"{branch}: {name}")
    sha = run_git(source, "rev-parse", "HEAD")
    run_git(source, "push", "-q", "origin", branch)
    run_git(source, "checkout", "-q", "main")
    return sha


def ready_attempt(workspace: dict[str, Path]):
    """Freeze an attempt over the temporary remote's current heads."""
    paths = paths_for(workspace)
    for branch, marker in orchestrator.SOURCES:
        publish_marker(workspace, branch, marker, "GREEN\n")
    readiness = orchestrator.evaluate(paths)
    assert all(entry.ready for entry in readiness)
    base = orchestrator.remote_sha(paths.git_host, orchestrator.BASE_BRANCH)
    assert base is not None
    return paths, orchestrator.freeze(readiness, base)


def test_the_integration_branch_is_cut_from_the_frozen_base(
    workspace: dict[str, Path],
) -> None:
    paths, attempt = ready_attempt(workspace)

    orchestrator.prepare_worktree(paths, attempt)

    assert run_git(paths.integration_worktree, "rev-parse", "HEAD") == attempt.base_sha
    assert (
        run_git(paths.integration_worktree, "rev-parse", "--abbrev-ref", "HEAD")
        == orchestrator.TARGET_BRANCH
    )


def test_the_three_sources_merge_in_the_agreed_order(workspace: dict[str, Path]) -> None:
    for branch, _marker in orchestrator.SOURCES:
        commit_on(workspace, branch, f"{branch.split('/')[-1]}.txt", "work\n")
    paths, attempt = ready_attempt(workspace)
    orchestrator.prepare_worktree(paths, attempt)

    orchestrator.merge_sources(paths, attempt)

    assert [entry["branch"] for entry in attempt.merges] == [
        "feat/decision-v2-v3",
        "feat/quant-research",
        "feat/ml-foundation",
    ]
    assert all(entry["result"] == "merged" for entry in attempt.merges)
    for name in ("decision-v2-v3.txt", "quant-research.txt", "ml-foundation.txt"):
        assert (paths.integration_worktree / name).exists()


def test_every_merge_keeps_its_source_lineage_explicit(workspace: dict[str, Path]) -> None:
    """`--no-ff` merges, so each source is a visible parent, not a fast-forward."""
    for branch, _marker in orchestrator.SOURCES:
        commit_on(workspace, branch, f"{branch.split('/')[-1]}.txt", "work\n")
    paths, attempt = ready_attempt(workspace)
    orchestrator.prepare_worktree(paths, attempt)

    orchestrator.merge_sources(paths, attempt)

    for entry in attempt.merges:
        parents = run_git(
            paths.integration_worktree, "rev-list", "--parents", "-n", "1", entry["merge_commit"]
        ).split()
        assert entry["sha"] in parents[1:]


def test_the_merged_history_reaches_every_frozen_revision(workspace: dict[str, Path]) -> None:
    for branch, _marker in orchestrator.SOURCES:
        commit_on(workspace, branch, f"{branch.split('/')[-1]}.txt", "work\n")
    paths, attempt = ready_attempt(workspace)
    orchestrator.prepare_worktree(paths, attempt)

    orchestrator.merge_sources(paths, attempt)

    for sha in attempt.source_shas.values():
        assert (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(paths.integration_worktree),
                    "merge-base",
                    "--is-ancestor",
                    sha,
                    "HEAD",
                ],
                check=False,
            ).returncode
            == 0
        )


def test_a_conflict_stops_immediately_and_is_not_resolved(
    workspace: dict[str, Path],
) -> None:
    """Two sources touching one file the same way must stop the pipeline."""
    commit_on(workspace, "feat/decision-v2-v3", "shared.txt", "decision wins\n")
    commit_on(workspace, "feat/quant-research", "shared.txt", "quant wins\n")
    paths, attempt = ready_attempt(workspace)
    orchestrator.prepare_worktree(paths, attempt)

    with pytest.raises(orchestrator.Stop) as raised:
        orchestrator.merge_sources(paths, attempt)

    assert raised.value.code == orchestrator.EXIT_MANUAL_REVIEW
    assert attempt.outcome == "CONFLICT"
    assert attempt.conflicts == ["shared.txt"]
    assert [entry["result"] for entry in attempt.merges] == ["merged", "CONFLICT"]


def test_a_conflict_leaves_the_third_source_unmerged(workspace: dict[str, Path]) -> None:
    """No alternative order is tried after a conflict."""
    commit_on(workspace, "feat/decision-v2-v3", "shared.txt", "decision wins\n")
    commit_on(workspace, "feat/quant-research", "shared.txt", "quant wins\n")
    commit_on(workspace, "feat/ml-foundation", "ml.txt", "ml work\n")
    paths, attempt = ready_attempt(workspace)
    orchestrator.prepare_worktree(paths, attempt)

    with pytest.raises(orchestrator.Stop):
        orchestrator.merge_sources(paths, attempt)

    assert not (paths.integration_worktree / "ml.txt").exists()
    assert "feat/ml-foundation" not in [entry["branch"] for entry in attempt.merges]


def test_a_conflict_is_left_in_place_for_manual_repair(workspace: dict[str, Path]) -> None:
    """The conflicted merge is not aborted, so an operator can see and fix it."""
    commit_on(workspace, "feat/decision-v2-v3", "shared.txt", "decision wins\n")
    commit_on(workspace, "feat/quant-research", "shared.txt", "quant wins\n")
    paths, attempt = ready_attempt(workspace)
    orchestrator.prepare_worktree(paths, attempt)

    with pytest.raises(orchestrator.Stop):
        orchestrator.merge_sources(paths, attempt)

    assert (paths.integration_worktree / ".git").exists()
    unmerged = run_git(paths.integration_worktree, "diff", "--name-only", "--diff-filter=U")
    assert unmerged == "shared.txt"


def test_a_conflicted_attempt_pushes_nothing(workspace: dict[str, Path]) -> None:
    commit_on(workspace, "feat/decision-v2-v3", "shared.txt", "decision wins\n")
    commit_on(workspace, "feat/quant-research", "shared.txt", "quant wins\n")
    paths, attempt = ready_attempt(workspace)
    before = run_git(workspace["remote"], "branch", "--list")

    assert orchestrator.run_once(paths) == orchestrator.EXIT_MANUAL_REVIEW

    assert run_git(workspace["remote"], "branch", "--list") == before
    assert attempt.push_result == "not attempted"
    state = orchestrator.load_state(paths)
    assert state["outcome"] == "CONFLICT"
    assert state["push_result"] == "not attempted"


def test_a_stopped_attempt_leaves_the_lock_free(workspace: dict[str, Path]) -> None:
    commit_on(workspace, "feat/decision-v2-v3", "shared.txt", "decision wins\n")
    commit_on(workspace, "feat/quant-research", "shared.txt", "quant wins\n")
    paths, _attempt = ready_attempt(workspace)

    orchestrator.run_once(paths)

    assert not paths.lock_dir.exists()


def test_a_second_attempt_refuses_while_the_lock_is_held(workspace: dict[str, Path]) -> None:
    paths, _attempt = ready_attempt(workspace)
    holder = orchestrator.Lock(paths.lock_dir)
    assert holder.acquire()[0]
    try:
        assert orchestrator.run_once(paths) == orchestrator.EXIT_LOCK_HELD
    finally:
        holder.release()

    assert not paths.integration_worktree.exists()


def test_the_conflicted_branch_blocks_a_later_unattended_attempt(
    workspace: dict[str, Path],
) -> None:
    """A half-finished integration is never quietly resumed or reset."""
    commit_on(workspace, "feat/decision-v2-v3", "shared.txt", "decision wins\n")
    commit_on(workspace, "feat/quant-research", "shared.txt", "quant wins\n")
    paths, _attempt = ready_attempt(workspace)
    orchestrator.run_once(paths)

    assert orchestrator.run_once(paths) == orchestrator.EXIT_MANUAL_REVIEW

    state = orchestrator.load_state(paths)
    assert "Manual review required" in state["detail"]
