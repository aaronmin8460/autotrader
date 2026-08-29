"""Local integration orchestrator for the V4 preparation merge.

This module is infrastructure, not trading code. It watches three parallel
development branches for an explicit readiness marker and, once and only once
all three are ready, performs a deterministic integration merge, validates it,
and publishes the result.

It never merges or pushes ``main``, never deploys, never restarts a runtime
service, never contacts a broker, and never resolves a merge conflict on its
own. Every ambiguity resolves to "do nothing" rather than to a questionable
integration.

Standard library only, so a scheduled invocation needs no virtual environment
of its own. A dedicated environment is built inside the external integration
worktree at validation time.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# The integration contract
#
# These are the facts the orchestrator is not allowed to infer. A branch is
# ready when the marker blob exists in its *current remote head* and reads
# exactly GREEN - never because of branch age, commit text, or local state.
# ---------------------------------------------------------------------------

READY_CONTENT = "GREEN"
READY_DIR = ".autotrader-ready"
PROVENANCE_PATH = ".autotrader-integration/v4-prep.json"
TARGET_READY_PATH = f"{READY_DIR}/v4-prep"

BASE_BRANCH = "feat/combined-integration"
TARGET_BRANCH = "integration/v4-prep"
REMOTE = "origin"

#: Branches to merge, in the deterministic order they are merged.
SOURCES: tuple[tuple[str, str], ...] = (
    ("feat/decision-v2-v3", f"{READY_DIR}/decision-v2-v3"),
    ("feat/quant-research", f"{READY_DIR}/quant-research"),
    ("feat/ml-foundation", f"{READY_DIR}/ml-foundation"),
)

#: Paths whose modification makes the frontend checks part of validation.
FRONTEND_TRIGGERS = ("dashboard/frontend/", "src/autotrader/dashboard/")

# ---------------------------------------------------------------------------
# Critical invariants
#
# Phase 8 regression verification. Each invariant names existing tests. A merge
# that deletes or renames one of these is treated as weakening the invariant
# even when the suite is otherwise green, because a guard that no longer runs
# no longer guards anything.
# ---------------------------------------------------------------------------

SAFETY_INVARIANTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "broker paper account only",
        (
            "test_the_trading_client_is_always_constructed_with_paper_true",
            "test_the_real_client_factory_produces_a_provably_paper_client",
            "test_a_live_base_url_is_refused",
        ),
    ),
    (
        "no live trading path",
        (
            "test_the_source_contains_no_live_trading_path",
            "test_the_integrated_system_has_no_live_path",
            "test_live_mode_cannot_be_constructed_from_execution_api",
            "test_no_cli_option_can_request_live_trading",
        ),
    ),
    (
        "UNKNOWN means no retry",
        (
            "test_an_ambiguous_submission_is_recorded_unknown_and_never_retried",
            "test_a_submitting_intent_is_treated_as_ambiguous_not_as_unsent",
            "test_an_unknown_intent_keeps_its_client_order_id_for_recovery",
        ),
    ),
    (
        "durable intent before submission",
        (
            "test_the_intent_is_committed_before_the_broker_is_called",
            "test_a_non_durable_intent_can_never_reach_the_broker",
            "test_a_broker_order_needs_a_real_intent",
        ),
    ),
    (
        "at-most-once submission",
        (
            "test_a_duplicate_client_order_id_is_rejected",
            "test_a_duplicate_signal_is_recorded_once_and_does_not_multiply_orders",
            "test_a_created_intent_with_a_broker_order_is_repaired_not_resubmitted",
            "test_a_crash_after_the_claim_but_before_the_intent_does_not_replay_the_bar",
        ),
    ),
    (
        "broker truth is authoritative",
        (
            "test_broker_position_is_authoritative_over_local_snapshot",
            "test_a_stale_local_position_is_overwritten_by_broker_truth",
            "test_a_stale_local_order_snapshot_is_repaired_from_broker_truth",
        ),
    ),
    (
        "reconciliation semantics",
        (
            "test_full_universe_reconciliation_is_account_authoritative",
            "test_the_paper_env_gate_cannot_bypass_a_failed_reconciliation",
            "test_full_universe_reconciliation_clears_global_halt_only_when_safe",
        ),
    ),
    (
        "global account safety",
        (
            "test_crypto_and_equity_share_one_global_exposure_limit",
            "test_a_green_pass_does_not_override_a_standing_account_halt",
            "test_crypto_cannot_trade_after_equity_unknown_order",
            "test_equity_cannot_trade_after_crypto_unknown_order",
        ),
    ),
    (
        "5% per-symbol cap",
        (
            "test_the_approved_notional_never_exceeds_the_five_percent_cap_exactly",
            "test_a_symbol_over_its_cap_is_reported_as_breached",
            "test_default_policy_is_exactly_five_thirty_two_percent",
        ),
    ),
    (
        "30% total exposure cap",
        (
            "test_total_exposure_limit_constrains_quantity",
            "test_global_exposure_race_cannot_approve_two_orders_from_same_free_capacity",
            "test_default_policy_is_exactly_five_thirty_two_percent",
        ),
    ),
    (
        "2% UTC-day loss halt",
        (
            "test_exactly_minus_two_percent_daily_pnl_rejects_a_buy",
            "test_better_than_minus_two_percent_does_not_trigger_the_halt",
            "test_the_daily_halt_is_measured_against_start_of_day_equity_only",
            "test_the_halt_is_not_cleared_by_time_passing",
        ),
    ),
    (
        "decision layer cannot reach the broker",
        (
            "test_strategy_module_imports_no_broker_client",
            "test_only_the_execution_package_imports_a_broker_trading_client",
            "test_the_order_api_exists_only_inside_the_paper_execution_boundary",
            "test_reconciliation_reaches_the_broker_only_through_the_execution_boundary",
        ),
    ),
)

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_MANUAL_REVIEW = 2
EXIT_LOCK_HELD = 4
EXIT_NO_VOLUME = 5

DEFAULT_POLL_SECONDS = 300
PYTEST_NO_TESTS_COLLECTED = 5
PYTEST_TIMEOUT_SECONDS = 3600
PYTEST_TAIL_LINES = 40
PYTEST_LOG_LINES = 400


# ---------------------------------------------------------------------------
# Paths
#
# Everything the orchestrator writes lives on the external workspace. The
# authoritative repository on the internal disk is only ever read from, and its
# checked-out branch, HEAD and working tree are never touched.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Paths:
    """Resolved filesystem layout for one orchestrator invocation."""

    qa_root: Path
    git_host: Path
    integration_worktree: Path
    state_dir: Path
    reports_dir: Path

    @property
    def lock_dir(self) -> Path:
        return self.state_dir / "orchestrator.lock"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def status_file(self) -> Path:
        return self.state_dir / "latest-status.txt"

    @property
    def event_log(self) -> Path:
        return self.state_dir / "orchestrator.log"

    @property
    def run_log_dir(self) -> Path:
        return self.state_dir / "runs"


def resolve_paths() -> Paths:
    """Read the layout from the environment, falling back to the defaults."""
    qa_root = Path(os.environ.get("AUTOTRADER_QA", "/Volumes/AUTOTRADER_QA"))
    git_host = Path(
        os.environ.get(
            "AUTOTRADER_INTEGRATION_GIT_HOST",
            str(qa_root / "worktrees" / "auto-integrator"),
        )
    )
    integration_worktree = Path(
        os.environ.get(
            "AUTOTRADER_INTEGRATION_WORKTREE",
            str(qa_root / "worktrees" / "v4-prep-integration"),
        )
    )
    return Paths(
        qa_root=qa_root,
        git_host=git_host,
        integration_worktree=integration_worktree,
        state_dir=qa_root / "logs" / "integration-orchestrator",
        reports_dir=qa_root / "reports",
    )


class Stop(Exception):
    """Raised to abandon an attempt without mutating anything further."""

    def __init__(self, reason: str, code: int = EXIT_MANUAL_REVIEW) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


def utc_now() -> datetime:
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%d-%H%M%S")


def iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


@dataclass
class Ran:
    """The outcome of one external command."""

    argv: tuple[str, ...]
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def shown(self) -> str:
        return " ".join(self.argv)


def run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 600,
    stdin_text: str | None = None,
) -> Ran:
    """Run a command, capturing both streams. Never raises on a bad exit."""
    try:
        done = subprocess.run(  # noqa: S603 - argv is built from constants
            list(argv),
            cwd=None if cwd is None else str(cwd),
            env=env,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Ran(tuple(argv), 124, "", f"timed out after {timeout}s")
    except OSError as exc:
        return Ran(tuple(argv), 127, "", str(exc))
    return Ran(tuple(argv), done.returncode, done.stdout, done.stderr)


def git(where: Path, *args: str, timeout: int = 600) -> Ran:
    """Run git against one explicit worktree. Never against the process cwd."""
    return run(["git", "-C", str(where), *args], timeout=timeout)


def git_ok(where: Path, *args: str, timeout: int = 600) -> str:
    """Run git and insist that it succeeded, returning trimmed stdout."""
    result = git(where, *args, timeout=timeout)
    if not result.ok:
        raise Stop(f"`{result.shown}` failed ({result.code}): {result.err.strip()}")
    return result.out.strip()


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


@dataclass
class BranchReadiness:
    """What the current remote head of one source branch says about itself."""

    branch: str
    marker: str
    sha: str | None
    marker_present: bool
    marker_content: str | None

    @property
    def ready(self) -> bool:
        return (
            self.sha is not None
            and self.marker_present
            and (self.marker_content or "").strip() == READY_CONTENT
        )

    @property
    def verdict(self) -> str:
        if self.sha is None:
            return "NO_REMOTE_BRANCH"
        if not self.marker_present:
            return "NOT_READY (marker absent)"
        if not self.ready:
            return "NOT_READY (marker is not exactly GREEN)"
        return "READY"


def fetch(paths: Paths) -> None:
    """Refresh remote-tracking refs. Mutates no branch and no working tree."""
    result = git(paths.git_host, "fetch", "--prune", REMOTE, timeout=300)
    if not result.ok:
        raise Stop(f"could not fetch {REMOTE}: {result.err.strip()}", code=EXIT_ERROR)


def read_blob(where: Path, ref: str, path: str) -> str | None:
    """Return the blob at ``ref:path``, or None when it is absent."""
    spec = f"{ref}:{path}"
    kind = git(where, "cat-file", "-t", spec)
    if not kind.ok or kind.out.strip() != "blob":
        return None
    blob = git(where, "cat-file", "blob", spec)
    return blob.out if blob.ok else None


def remote_sha(where: Path, branch: str) -> str | None:
    result = git(where, "rev-parse", "--verify", "--quiet", f"{REMOTE}/{branch}^{{commit}}")
    return result.out.strip() if result.ok and result.out.strip() else None


def evaluate(paths: Paths) -> list[BranchReadiness]:
    """Fetch, then read every marker out of the current remote heads."""
    fetch(paths)
    found: list[BranchReadiness] = []
    for branch, marker in SOURCES:
        sha = remote_sha(paths.git_host, branch)
        content = None if sha is None else read_blob(paths.git_host, f"{REMOTE}/{branch}", marker)
        found.append(
            BranchReadiness(
                branch=branch,
                marker=marker,
                sha=sha,
                marker_present=content is not None,
                marker_content=content,
            )
        )
    return found


# ---------------------------------------------------------------------------
# The lock
#
# macOS ships no GNU flock, so exclusion rests on mkdir, which APFS makes
# atomic: exactly one caller can create a directory that does not yet exist.
#
# A held lock is broken only when its owner is *proved* gone - same host, and
# either the process id no longer exists or it has been reused by a process
# with a different start time. Age alone is never a reason.
# ---------------------------------------------------------------------------


def process_start(pid: int) -> str | None:
    """The start time macOS reports for a live pid, or None when it is gone."""
    result = run(["ps", "-o", "lstart=", "-p", str(pid)], timeout=20)
    started = result.out.strip()
    return started or None


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class Lock:
    """An mkdir-based exclusive lock over the whole integration pipeline."""

    directory: Path
    held: bool = False

    @property
    def owner_file(self) -> Path:
        return self.directory / "owner.json"

    def _claim(self) -> bool:
        try:
            self.directory.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            return False
        pid = os.getpid()
        owner = {
            "pid": pid,
            "host": os.uname().nodename,
            "started": process_start(pid),
            "argv": sys.argv,
            "acquired_at": iso(utc_now()),
        }
        self.owner_file.write_text(json.dumps(owner, indent=2) + "\n", encoding="utf-8")
        self.held = True
        return True

    def _owner(self) -> dict[str, object] | None:
        try:
            return json.loads(self.owner_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _breakable(self) -> tuple[bool, str]:
        """Decide whether the existing lock's owner is provably gone."""
        owner = self._owner()
        if owner is None:
            return False, "lock has no readable owner record; refusing to break it"
        host = owner.get("host")
        if host != os.uname().nodename:
            return False, f"lock is owned by another host ({host!r}); refusing to break it"
        pid = owner.get("pid")
        if not isinstance(pid, int):
            return False, "lock owner record has no process id; refusing to break it"
        if not process_alive(pid):
            return True, f"lock owner pid {pid} no longer exists"
        recorded = owner.get("started")
        current = process_start(pid)
        if isinstance(recorded, str) and current is not None and recorded != current:
            return True, f"lock owner pid {pid} was reused by a newer process"
        return False, f"lock is held by running pid {pid}"

    def acquire(self) -> tuple[bool, str]:
        """Take the lock, breaking a provably dead one exactly once."""
        if self._claim():
            return True, "acquired"
        breakable, why = self._breakable()
        if not breakable:
            return False, why
        shutil.rmtree(self.directory, ignore_errors=True)
        if self._claim():
            return True, f"acquired after breaking a dead lock ({why})"
        return False, "lost the race to reclaim a dead lock"

    def release(self) -> None:
        if self.held:
            shutil.rmtree(self.directory, ignore_errors=True)
            self.held = False


# ---------------------------------------------------------------------------
# Durable state and logging
# ---------------------------------------------------------------------------


def load_state(paths: Paths) -> dict[str, object]:
    try:
        loaded = json.loads(paths.state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_state(paths: Paths, state: dict[str, object]) -> None:
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    temporary = paths.state_file.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(paths.state_file)


def log(paths: Paths, message: str) -> None:
    """Append one line to the external event log and echo it to stdout."""
    line = f"{iso(utc_now())} {message}"
    print(line, flush=True)
    try:
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        with paths.event_log.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def notify(title: str, message: str) -> None:
    """Post a local macOS notification. Nothing leaves this machine."""
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    run(["osascript", "-e", script], timeout=30)


def write_status(paths: Paths, text: str) -> None:
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.status_file.write_text(text.rstrip() + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# One integration attempt
# ---------------------------------------------------------------------------


@dataclass
class Attempt:
    """The frozen facts of a single integration attempt.

    The source revisions are resolved once, at the moment readiness became
    true, and every later step uses these exact object names. A branch that
    moves while the attempt runs is not silently consumed.
    """

    started_at: datetime
    base_sha: str
    sources: tuple[tuple[str, str, str], ...]
    readiness: list[BranchReadiness]
    merges: list[dict[str, str]] = field(default_factory=list)
    validations: list[dict[str, object]] = field(default_factory=list)
    invariants: list[dict[str, object]] = field(default_factory=list)
    frontend_required: bool = False
    integration_sha: str | None = None
    push_result: str = "not attempted"
    outcome: str = "INCOMPLETE"
    detail: str = ""
    conflicts: list[str] = field(default_factory=list)
    report_path: Path | None = None

    @property
    def source_shas(self) -> dict[str, str]:
        return {branch: sha for branch, _marker, sha in self.sources}

    @property
    def green(self) -> bool:
        return self.outcome == "GREEN"


def freeze(readiness: list[BranchReadiness], base_sha: str) -> Attempt:
    sources = tuple(
        (found.branch, found.marker, found.sha) for found in readiness if found.sha is not None
    )
    return Attempt(
        started_at=utc_now(),
        base_sha=base_sha,
        sources=sources,
        readiness=readiness,
    )


def provenance_of(where: Path, ref: str) -> dict[str, object] | None:
    """Read a recorded integration provenance document off a branch tip."""
    raw = read_blob(where, ref, PROVENANCE_PATH)
    if raw is None:
        return None
    try:
        loaded = json.loads(raw)
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def matches(provenance: dict[str, object], attempt: Attempt) -> bool:
    """Does an existing integration already record exactly this attempt?"""
    if provenance.get("status") != "GREEN":
        return False
    if provenance.get("base_sha") != attempt.base_sha:
        return False
    recorded = provenance.get("sources")
    if not isinstance(recorded, dict):
        return False
    return recorded == attempt.source_shas


def existing_target(paths: Paths) -> tuple[str | None, str | None]:
    """The local and remote revisions of the integration branch, if any."""
    local = git(paths.git_host, "rev-parse", "--verify", "--quiet", f"{TARGET_BRANCH}^{{commit}}")
    remote = remote_sha(paths.git_host, TARGET_BRANCH)
    return (local.out.strip() or None if local.ok else None), remote


def prepare_worktree(paths: Paths, attempt: Attempt) -> str:
    """Bring the integration worktree into existence, or refuse to guess.

    Returns a short description of what was done, for the report.
    """
    local_target, remote_target = existing_target(paths)
    worktree_exists = (paths.integration_worktree / ".git").exists()

    if local_target or remote_target:
        ref = TARGET_BRANCH if local_target else f"{REMOTE}/{TARGET_BRANCH}"
        recorded = provenance_of(paths.git_host, ref)
        if recorded is None:
            raise Stop(
                f"{TARGET_BRANCH} already exists at {local_target or remote_target} but carries "
                f"no {PROVENANCE_PATH}; its provenance is unknown. Manual review required - "
                "the orchestrator will not reset, force or overwrite it."
            )
        if matches(recorded, attempt):
            attempt.outcome = "ALREADY_COMPLETE"
            attempt.integration_sha = local_target or remote_target
            attempt.detail = (
                f"{TARGET_BRANCH} already records exactly these source revisions and is GREEN."
            )
            raise Stop(attempt.detail, code=EXIT_OK)
        raise Stop(
            f"{TARGET_BRANCH} already exists with different provenance (recorded sources "
            f"{recorded.get('sources')!r}, this attempt {attempt.source_shas!r}). "
            "Manual review required - the orchestrator will not reset it."
        )

    if worktree_exists:
        raise Stop(
            f"{paths.integration_worktree} already exists but {TARGET_BRANCH} does not. "
            "Ambiguous state; manual review required."
        )
    if paths.integration_worktree.exists() and any(paths.integration_worktree.iterdir()):
        raise Stop(f"{paths.integration_worktree} exists and is not empty. Manual review required.")

    added = git(
        paths.git_host,
        "worktree",
        "add",
        "-b",
        TARGET_BRANCH,
        str(paths.integration_worktree),
        attempt.base_sha,
        timeout=600,
    )
    if not added.ok:
        raise Stop(
            f"could not create the integration worktree: {added.err.strip()}", code=EXIT_ERROR
        )
    return f"created {TARGET_BRANCH} at {attempt.base_sha[:12]} in {paths.integration_worktree}"


def merge_sources(paths: Paths, attempt: Attempt) -> None:
    """Merge the three frozen revisions in order, stopping at any conflict."""
    where = paths.integration_worktree
    for branch, _marker, sha in attempt.sources:
        before = git_ok(where, "rev-parse", "HEAD")
        message = f"Merge {branch} at {sha[:12]} into {TARGET_BRANCH}"
        merged = git(where, "merge", "--no-ff", "--no-edit", "-m", message, sha, timeout=600)
        if not merged.ok:
            conflicted = git(where, "diff", "--name-only", "--diff-filter=U")
            attempt.conflicts = [line for line in conflicted.out.splitlines() if line.strip()]
            attempt.outcome = "CONFLICT"
            attempt.detail = (
                f"merging {branch} at {sha} into {before[:12]} conflicted. "
                "The conflicted merge has been left in place for manual repair; "
                "no resolution was attempted and no alternative order was tried."
            )
            attempt.merges.append(
                {"branch": branch, "sha": sha, "result": "CONFLICT", "merge_commit": ""}
            )
            raise Stop(attempt.detail)
        after = git_ok(where, "rev-parse", "HEAD")
        attempt.merges.append(
            {"branch": branch, "sha": sha, "result": "merged", "merge_commit": after}
        )
        log(paths, f"merged {branch} {sha[:12]} -> {after[:12]}")
    attempt.integration_sha = git_ok(where, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# Validation
#
# The repository's own established commands, run against the merged tree in a
# dedicated environment inside the external worktree. Every test in this
# repository is offline: the broker boundary is mocked and sockets are asserted
# shut, so nothing here reaches a broker and no order can be submitted.
# ---------------------------------------------------------------------------


def validation_env(paths: Paths) -> dict[str, str]:
    """The repository's external-cache environment, as session-env.sh sets it."""
    environment = dict(os.environ)
    caches = paths.qa_root / "caches"
    environment.update(
        {
            "AUTOTRADER_QA": str(paths.qa_root),
            "TMPDIR": str(paths.qa_root / "tmp"),
            "npm_config_cache": str(caches / "npm"),
            "PIP_CACHE_DIR": str(caches / "pip"),
            "UV_CACHE_DIR": str(caches / "uv"),
            "PYTHONPYCACHEPREFIX": str(caches / "pycache"),
            "PLAYWRIGHT_BROWSERS_PATH": str(caches / "playwright"),
        }
    )
    for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "AUTOTRADER_PAPER_TRADING_ENABLED"):
        environment.pop(key, None)
    return environment


def record(attempt: Attempt, name: str, result: Ran, *, tail: int = PYTEST_TAIL_LINES) -> bool:
    """Store one validation step's outcome on the attempt and report pass/fail."""
    output = (result.out + result.err).strip().splitlines()
    attempt.validations.append(
        {
            "name": name,
            "command": result.shown,
            "exit_code": result.code,
            "passed": result.ok,
            "tail": output[-tail:],
        }
    )
    return result.ok


def build_env(paths: Paths, attempt: Attempt) -> Path:
    """Create the integration virtual environment inside the external worktree."""
    where = paths.integration_worktree
    python = where / ".venv" / "bin" / "python"
    environment = validation_env(paths)
    if not python.exists():
        created = run(
            [sys.executable, "-m", "venv", str(where / ".venv")],
            cwd=where,
            env=environment,
            timeout=600,
        )
        if not created.ok:
            record(attempt, "create integration venv", created)
            raise Stop("could not create the integration virtual environment")
    installed = run(
        [str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        cwd=where,
        env=environment,
        timeout=900,
    )
    if not installed.ok:
        record(attempt, "upgrade pip", installed)
        raise Stop("could not prepare the integration virtual environment")
    deps = run(
        [str(python), "-m", "pip", "install", "--quiet", "-e", ".[dev]"],
        cwd=where,
        env=environment,
        timeout=1800,
    )
    if not record(attempt, "install dev dependencies", deps):
        raise Stop("could not install the integration dependencies")
    return python


def collected_tests(python: Path, paths: Paths) -> set[str]:
    """Every test function name pytest can currently collect."""
    result = run(
        [str(python), "-m", "pytest", "-q", "--collect-only"],
        cwd=paths.integration_worktree,
        env=validation_env(paths),
        timeout=PYTEST_TIMEOUT_SECONDS,
    )
    names: set[str] = set()
    for line in result.out.splitlines():
        node = line.strip()
        if "::" not in node:
            continue
        leaf = node.rsplit("::", 1)[-1]
        names.add(leaf.split("[", 1)[0])
    return names


def check_invariants(python: Path, paths: Paths, attempt: Attempt) -> bool:
    """Verify the critical invariants still have guards, and that they pass.

    A guard that a merge deleted or renamed counts as a weakened invariant even
    when the rest of the suite is green.
    """
    present = collected_tests(python, paths)
    if not present:
        attempt.invariants.append(
            {"invariant": "test collection", "status": "FAILED", "missing": [], "anchors": 0}
        )
        return False

    anchors: list[str] = []
    healthy = True
    for name, tests in SAFETY_INVARIANTS:
        missing = [test for test in tests if test not in present]
        anchors.extend(test for test in tests if test in present)
        status = "WEAKENED" if missing else "guarded"
        healthy = healthy and not missing
        attempt.invariants.append(
            {
                "invariant": name,
                "status": status,
                "missing": missing,
                "anchors": len(tests) - len(missing),
            }
        )

    selection = " or ".join(sorted(set(anchors)))
    result = run(
        [str(python), "-m", "pytest", "-q", "-k", selection],
        cwd=paths.integration_worktree,
        env=validation_env(paths),
        timeout=PYTEST_TIMEOUT_SECONDS,
    )
    passed = record(attempt, "safety regression (critical invariants)", result)
    if result.code == PYTEST_NO_TESTS_COLLECTED:
        passed = False
    return healthy and passed


def touches_frontend(paths: Paths, attempt: Attempt) -> bool:
    changed = git(paths.integration_worktree, "diff", "--name-only", attempt.base_sha, "HEAD")
    return any(
        line.startswith(FRONTEND_TRIGGERS) for line in changed.out.splitlines() if line.strip()
    )


def frontend_checks(paths: Paths, attempt: Attempt) -> bool:
    """The established dashboard frontend pipeline, using the external npm cache."""
    where = paths.integration_worktree / "dashboard" / "frontend"
    if not (where / "package.json").exists():
        attempt.validations.append(
            {
                "name": "frontend",
                "command": "(skipped)",
                "exit_code": 0,
                "passed": True,
                "tail": ["no dashboard/frontend/package.json in the merged tree"],
            }
        )
        return True
    environment = validation_env(paths)
    steps: tuple[tuple[str, list[str], int], ...] = (
        ("npm ci", ["npm", "ci"], 1800),
        ("npm run lint", ["npm", "run", "lint"], 900),
        ("npm run typecheck", ["npm", "run", "typecheck"], 900),
        ("npm run build", ["npm", "run", "build"], 1800),
        ("npm test", ["npm", "test"], 900),
    )
    healthy = True
    for name, argv, timeout in steps:
        result = run(argv, cwd=where, env=environment, timeout=timeout)
        if not record(attempt, name, result):
            healthy = False
            break
    return healthy


def validate(paths: Paths, attempt: Attempt) -> bool:
    """Run the repository's established checks against the merged tree."""
    where = paths.integration_worktree
    environment = validation_env(paths)
    python = build_env(paths, attempt)

    healthy = True
    suite = run(
        [str(python), "-m", "pytest", "-q"],
        cwd=where,
        env=environment,
        timeout=PYTEST_TIMEOUT_SECONDS,
    )
    healthy = record(attempt, "pytest -q", suite, tail=PYTEST_LOG_LINES) and healthy

    lint = run([str(python), "-m", "ruff", "check", "."], cwd=where, env=environment, timeout=600)
    healthy = record(attempt, "ruff check .", lint) and healthy

    formatting = run(
        [str(python), "-m", "ruff", "format", "--check", "."],
        cwd=where,
        env=environment,
        timeout=600,
    )
    healthy = record(attempt, "ruff format --check .", formatting) and healthy

    whitespace = git(where, "diff", "--check")
    healthy = record(attempt, "git diff --check", whitespace) and healthy

    ranged = git(where, "diff", "--check", attempt.base_sha, "HEAD")
    healthy = record(attempt, f"git diff --check {attempt.base_sha[:12]} HEAD", ranged) and healthy

    attempt.frontend_required = touches_frontend(paths, attempt)
    if attempt.frontend_required:
        healthy = frontend_checks(paths, attempt) and healthy

    healthy = check_invariants(python, paths, attempt) and healthy
    return healthy


# ---------------------------------------------------------------------------
# Provenance and publication
# ---------------------------------------------------------------------------


def write_provenance(paths: Paths, attempt: Attempt) -> None:
    """Record how this integration was produced, then commit it.

    Written only once validation is fully green, so the marker cannot describe
    an integration that was never proved.
    """
    where = paths.integration_worktree
    document = {
        "status": "GREEN",
        "integration_branch": TARGET_BRANCH,
        "integration_base_branch": BASE_BRANCH,
        "base_sha": attempt.base_sha,
        "sources": attempt.source_shas,
        "merge_order": [branch for branch, _marker, _sha in attempt.sources],
        "merges": attempt.merges,
        "readiness": [
            {"branch": found.branch, "marker": found.marker, "sha": found.sha}
            for found in attempt.readiness
        ],
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
        "generated_at": iso(utc_now()),
        "generator": "tools/integration/orchestrator.py",
    }

    provenance = where / PROVENANCE_PATH
    provenance.parent.mkdir(parents=True, exist_ok=True)
    provenance.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    marker = where / TARGET_READY_PATH
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(READY_CONTENT + "\n", encoding="utf-8")

    git_ok(where, "add", PROVENANCE_PATH, TARGET_READY_PATH)
    message = (
        f"chore: record {TARGET_BRANCH} integration provenance\n\n"
        f"base {BASE_BRANCH} {attempt.base_sha}\n"
        + "\n".join(f"{branch} {sha}" for branch, sha in attempt.source_shas.items())
    )
    committed = git(where, "commit", "-m", message)
    if not committed.ok:
        raise Stop(f"could not commit the provenance: {committed.err.strip()}", code=EXIT_ERROR)
    attempt.integration_sha = git_ok(where, "rev-parse", "HEAD")


def publish(paths: Paths, attempt: Attempt) -> None:
    """Push the integration branch. Never forced, and never any other branch."""
    pushed = git(
        paths.integration_worktree,
        "push",
        REMOTE,
        f"refs/heads/{TARGET_BRANCH}:refs/heads/{TARGET_BRANCH}",
        timeout=900,
    )
    if not pushed.ok:
        attempt.push_result = f"REJECTED ({pushed.err.strip()})"
        attempt.outcome = "PUSH_REJECTED"
        attempt.detail = (
            "the remote refused the push; remote state changed underneath this attempt. "
            "Stopping rather than forcing."
        )
        raise Stop(attempt.detail)
    attempt.push_result = f"pushed {REMOTE}/{TARGET_BRANCH} at {attempt.integration_sha}"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def bullet(lines: Iterable[str]) -> str:
    collected = [line for line in lines if line]
    return "\n".join(f"- {line}" for line in collected) if collected else "- (none)"


def write_report(paths: Paths, attempt: Attempt) -> Path:
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    path = paths.reports_dir / f"v4-prep-integration-{stamp(attempt.started_at)}.md"

    readiness = bullet(
        f"`{found.branch}` -> {found.verdict}; remote head "
        f"`{found.sha or 'absent'}`; marker `{found.marker}`"
        for found in attempt.readiness
    )
    merges = bullet(
        f"`{entry['branch']}` at `{entry['sha']}` -> {entry['result']}"
        + (f", merge commit `{entry['merge_commit']}`" if entry["merge_commit"] else "")
        for entry in attempt.merges
    )
    checks = bullet(
        f"`{step['command']}` -> {'PASS' if step['passed'] else 'FAIL'} (exit {step['exit_code']})"
        for step in attempt.validations
    )
    invariants = bullet(
        f"{entry['invariant']}: {entry['status']}"
        + (f"; missing guards: {', '.join(entry['missing'])}" if entry["missing"] else "")
        for entry in attempt.invariants
    )
    failures = [step for step in attempt.validations if not step["passed"]]
    failure_detail = ""
    if failures:
        blocks = []
        for step in failures:
            body = "\n".join(str(line) for line in step["tail"])
            blocks.append(f"### `{step['command']}`\n\n```\n{body}\n```")
        failure_detail = "\n\n## Failing output\n\n" + "\n\n".join(blocks)

    remaining = (
        "Integration branch is published. Reviewing and merging it onward is a "
        "separate, manual decision; this orchestrator never opens or merges a "
        "pull request against `main`."
        if attempt.green
        else "Integration did not complete. Resolve the cause above, then re-run "
        "`orchestrator.py run-once`. Nothing was pushed."
    )

    text = f"""# V4 prep integration - {attempt.outcome}

- Attempt started: {iso(attempt.started_at)}
- Attempt finished: {iso(utc_now())}
- Outcome: **{attempt.outcome}**
- Detail: {attempt.detail or "-"}

## Sources

- Integration base branch: `{BASE_BRANCH}`
- Integration base SHA: `{attempt.base_sha}`
- Target branch: `{TARGET_BRANCH}`

{bullet(f"`{branch}` frozen at `{sha}`" for branch, _marker, sha in attempt.sources)}

## Readiness proof

Every marker below was read from the branch's current remote head at fetch
time, and had to contain exactly `{READY_CONTENT}`.

{readiness}

## Merges

Deterministic order: {", ".join(f"`{branch}`" for branch, _m, _s in attempt.sources)}.
Normal `--no-ff` merges only - no squash, rebase, cherry-pick or history rewrite.

{merges}

Conflicts: {", ".join(f"`{path}`" for path in attempt.conflicts) if attempt.conflicts else "none"}

## Validation

{checks}

Frontend pipeline required by the merged diff: {"yes" if attempt.frontend_required else "no"}

## Safety regression

Existing critical invariants, verified rather than redefined. An invariant whose
guarding tests a merge removed is reported as WEAKENED even when the suite is
green.

{invariants}

## Result

- Final integration SHA: `{attempt.integration_sha or "-"}`
- Remote push: {attempt.push_result}

## Remaining work

{remaining}

## Scope

This run touched no other branch. `main` was never checked out, merged or
pushed; no VPS was contacted; no runtime service was started or restarted; no
broker request was made - the suite is offline by construction.
{failure_detail}
"""
    path.write_text(text, encoding="utf-8")
    attempt.report_path = path
    return path


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def preflight(paths: Paths) -> None:
    """Refuse to do anything unless the external workspace is really there."""
    required = [paths.qa_root, paths.qa_root / "worktrees", paths.reports_dir]
    missing = [str(path) for path in required if not path.is_dir()]
    if missing:
        raise Stop(
            "external workspace is not available: " + ", ".join(missing), code=EXIT_NO_VOLUME
        )
    if not (paths.git_host / ".git").exists():
        raise Stop(f"git host {paths.git_host} is not a worktree", code=EXIT_NO_VOLUME)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.run_log_dir.mkdir(parents=True, exist_ok=True)


def already_done(paths: Paths) -> bool:
    """Has a previous run already published a green integration?"""
    state = load_state(paths)
    settled = {"GREEN", "ALREADY_COMPLETE"}
    return state.get("outcome") in settled and bool(state.get("integration_sha"))


def finish(paths: Paths, attempt: Attempt) -> None:
    """Persist the report, the machine-readable state, and notify."""
    report = write_report(paths, attempt)
    save_state(
        paths,
        {
            "outcome": attempt.outcome,
            "detail": attempt.detail,
            "base_branch": BASE_BRANCH,
            "base_sha": attempt.base_sha,
            "sources": attempt.source_shas,
            "merge_order": [branch for branch, _marker, _sha in attempt.sources],
            "integration_branch": TARGET_BRANCH,
            "integration_sha": attempt.integration_sha,
            "push_result": attempt.push_result,
            "frontend_validated": attempt.frontend_required,
            "conflicts": attempt.conflicts,
            "validation": [
                {"name": step["name"], "passed": step["passed"]} for step in attempt.validations
            ],
            "safety_invariants": attempt.invariants,
            "report": str(report),
            "finished_at": iso(utc_now()),
        },
    )
    write_status(
        paths,
        "\n".join(
            [
                f"outcome:    {attempt.outcome}",
                f"finished:   {iso(utc_now())}",
                f"base:       {BASE_BRANCH} {attempt.base_sha}",
                *(f"source:     {branch} {sha}" for branch, sha in attempt.source_shas.items()),
                f"integration:{attempt.integration_sha or '-'}",
                f"push:       {attempt.push_result}",
                f"report:     {report}",
                f"detail:     {attempt.detail or '-'}",
            ]
        ),
    )
    if attempt.green:
        notify(
            "AutoTrader V4 Prep Ready",
            "V2/V3 + Quant + ML Foundation integration is GREEN.",
        )
    elif attempt.outcome != "ALREADY_COMPLETE":
        notify(
            "AutoTrader Integration Needs Attention",
            "V4 prep integration stopped safely. Review the report.",
        )
    log(paths, f"attempt finished {attempt.outcome}; report {report}")


def run_once(paths: Paths, *, force: bool = False) -> int:
    """Evaluate readiness and, only when all three are ready, integrate."""
    preflight(paths)

    if already_done(paths) and not force:
        state = load_state(paths)
        log(
            paths,
            f"integration already GREEN at {state.get('integration_sha')}; nothing to do",
        )
        return EXIT_OK

    readiness = evaluate(paths)
    for found in readiness:
        log(paths, f"{found.branch}: {found.verdict} @ {found.sha or 'absent'}")
    if not all(found.ready for found in readiness):
        log(paths, "not all sources are READY; no integration attempted, nothing mutated")
        write_status(
            paths,
            "\n".join(
                [
                    "outcome:    WAITING",
                    f"checked:    {iso(utc_now())}",
                    *(f"{found.branch}: {found.verdict} @ {found.sha}" for found in readiness),
                    "integration:not started",
                ]
            ),
        )
        return EXIT_OK

    lock = Lock(paths.lock_dir)
    acquired, why = lock.acquire()
    if not acquired:
        log(paths, f"another orchestrator owns the lock: {why}")
        return EXIT_LOCK_HELD

    try:
        return integrate_locked(paths, readiness)
    finally:
        lock.release()


def integrate_locked(paths: Paths, readiness: list[BranchReadiness]) -> int:
    """Perform the integration itself. The caller must already hold the lock.

    Separated from `run_once` so a longer pipeline can take the lock once and
    keep it across several stages, rather than releasing and retaking it
    between steps that must not interleave.
    """
    base = remote_sha(paths.git_host, BASE_BRANCH)
    if base is None:
        raise Stop(f"{REMOTE}/{BASE_BRANCH} does not exist", code=EXIT_ERROR)
    attempt = freeze(readiness, base)
    log(paths, f"all sources READY; base {BASE_BRANCH} frozen at {base[:12]}")
    try:
        note = prepare_worktree(paths, attempt)
        log(paths, note)
        merge_sources(paths, attempt)
        if validate(paths, attempt):
            write_provenance(paths, attempt)
            publish(paths, attempt)
            attempt.outcome = "GREEN"
            attempt.detail = "all merges clean, all validation green, branch published"
        else:
            attempt.outcome = "VALIDATION_FAILED"
            attempt.detail = (
                "one or more checks failed against the merged tree; "
                "nothing was committed as ready and nothing was pushed"
            )
    except Stop as stop:
        if attempt.outcome == "INCOMPLETE":
            attempt.outcome = "STOPPED"
        attempt.detail = attempt.detail or stop.reason
        finish(paths, attempt)
        return EXIT_OK if attempt.outcome == "ALREADY_COMPLETE" else stop.code
    finish(paths, attempt)
    return EXIT_OK if attempt.green else EXIT_MANUAL_REVIEW


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def command_check(paths: Paths) -> int:
    preflight(paths)
    readiness = evaluate(paths)
    base = remote_sha(paths.git_host, BASE_BRANCH)
    print(f"base   {BASE_BRANCH:<28} {base or 'absent'}")
    for found in readiness:
        print(f"source {found.branch:<28} {found.sha or 'absent'}  {found.verdict}")
    ready = all(found.ready for found in readiness)
    print(f"\nall three READY: {'yes' if ready else 'no'}")
    return EXIT_OK


def command_status(paths: Paths) -> int:
    preflight(paths)
    readiness = evaluate(paths)
    base = remote_sha(paths.git_host, BASE_BRANCH)
    local_target, remote_target = existing_target(paths)
    state = load_state(paths)

    print("SOURCE BRANCHES")
    for found in readiness:
        print(f"  {found.branch:<28} {found.sha or 'absent'}")
        print(f"  {'':<28} marker {found.marker} -> {found.verdict}")
    print(f"\nBASE\n  {BASE_BRANCH:<28} {base or 'absent'}")

    print("\nINTEGRATION")
    print(f"  branch                       {TARGET_BRANCH}")
    print(f"  local                        {local_target or 'does not exist'}")
    print(f"  remote                       {remote_target or 'does not exist'}")
    print(f"  worktree                     {paths.integration_worktree}")
    print(f"  state                        {state.get('outcome', 'never run')}")
    if state.get("detail"):
        print(f"  detail                       {state['detail']}")

    marker = None
    if local_target or remote_target:
        ref = TARGET_BRANCH if local_target else f"{REMOTE}/{TARGET_BRANCH}"
        marker = read_blob(paths.git_host, ref, TARGET_READY_PATH)
    ready = (marker or "").strip() == READY_CONTENT
    print(f"  {TARGET_READY_PATH:<28} {'GREEN' if ready else 'not published'}")

    print(f"\nLATEST REPORT\n  {state.get('report', 'none yet')}")
    print(f"\nLOCK\n  {'held' if paths.lock_dir.exists() else 'free'}  {paths.lock_dir}")
    return EXIT_OK


def command_watch(paths: Paths, interval: int) -> int:
    """Poll until the integration completes. The LaunchAgent is the durable path."""
    preflight(paths)
    log(paths, f"watch started; polling every {interval}s")
    while True:
        code = run_once(paths)
        if already_done(paths):
            log(paths, "integration is GREEN; watch is done")
            return EXIT_OK
        if code not in (EXIT_OK, EXIT_LOCK_HELD):
            log(paths, f"watch stopping; run-once returned {code} and needs attention")
            return code
        time.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestrator.py",
        description="Watch three feature branches for readiness and integrate them once.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="fetch and report readiness; mutates nothing")
    once = sub.add_parser("run-once", help="integrate if and only if all three are READY")
    once.add_argument(
        "--force",
        action="store_true",
        help="re-attempt even when a previous run already recorded GREEN",
    )
    watch = sub.add_parser("watch", help="poll until the integration completes")
    watch.add_argument("--interval", type=int, default=DEFAULT_POLL_SECONDS)
    sub.add_parser("status", help="show branches, integration state and the latest report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = resolve_paths()
    try:
        if args.command == "check":
            return command_check(paths)
        if args.command == "run-once":
            return run_once(paths, force=args.force)
        if args.command == "watch":
            return command_watch(paths, args.interval)
        if args.command == "status":
            return command_status(paths)
    except Stop as stop:
        print(f"stopped: {stop.reason}", file=sys.stderr)
        return stop.code
    except KeyboardInterrupt:
        return EXIT_OK
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
