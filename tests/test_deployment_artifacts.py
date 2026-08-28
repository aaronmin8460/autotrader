"""Static audit of `deploy/`. No server, no network, no systemd required.

These tests do not check that the deployment works - that needs a VPS. They
check that the artifacts still say what they were written to say, because
every property here is one that a small, reasonable-looking edit could remove
without anything else noticing: a `Restart=always` copied from the dashboard
unit onto a trading unit, a `--hostname` dropped from an ExecStart, a
credential pasted into a unit body "just to test it", a `rm -rf` added to the
deploy script to clean up a stale directory.

The systemd parser here is deliberate: grepping unit files finds the same text
in comments, and these files carry a lot of comments explaining exactly the
dangerous values being asserted against. `0.0.0.0` appears in
autotrader-dashboard-web.service - in a comment saying why `--hostname` is
there. Only directive lines are evidence.
"""

from __future__ import annotations

import ast
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_ROOT = REPOSITORY_ROOT / "deploy"
SYSTEMD_ROOT = DEPLOY_ROOT / "systemd"
ENV_ROOT = DEPLOY_ROOT / "env"
BIN_ROOT = DEPLOY_ROOT / "bin"

TRADING_UNITS = ("autotrader-crypto.service", "autotrader-equity.service")
DASHBOARD_UNITS = ("autotrader-dashboard-api.service", "autotrader-dashboard-web.service")

BASH_SCRIPTS = (
    "autotrader-deploy",
    "autotrader-rollback",
    "autotrader-enable-paper-trading",
    "autotrader-emergency-stop",
)
PYTHON_SCRIPTS = ("autotrader-backup", "autotrader-healthcheck")
ALL_SCRIPTS = BASH_SCRIPTS + PYTHON_SCRIPTS

#: The one file whose existence authorizes order submission.
ACTIVATION_BASENAME = "autotrader.trading.env"


# ---------------------------------------------------------------------------
# A small systemd unit parser
# ---------------------------------------------------------------------------


def parse_unit(path: Path) -> dict[str, list[tuple[str, str]]]:
    """Parse a unit into {section: [(directive, value), ...]}.

    Comments and blank lines are dropped, and continuation lines are joined,
    so what comes back is only what systemd would act on. Duplicate directives
    are kept as separate entries because systemd treats several of them - most
    relevantly `EnvironmentFile` - as an ordered list rather than a mapping.
    """
    sections: dict[str, list[tuple[str, str]]] = {}
    current: str | None = None
    pending = ""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if pending:
            line = pending + line
            pending = ""
        if line.endswith("\\"):
            pending = line[:-1]
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
            continue
        assert current is not None, f"{path.name}: directive outside any section: {line!r}"
        assert "=" in line, f"{path.name}: not a directive: {line!r}"
        key, value = line.split("=", 1)
        sections[current].append((key.strip(), value.strip()))
    assert not pending, f"{path.name}: file ends on a continuation line"
    return sections


def directive(unit: dict[str, list[tuple[str, str]]], section: str, key: str) -> list[str]:
    return [value for name, value in unit.get(section, []) if name == key]


def one(unit: dict[str, list[tuple[str, str]]], section: str, key: str) -> str | None:
    values = directive(unit, section, key)
    return values[-1] if values else None


def unit_files() -> list[Path]:
    return sorted(SYSTEMD_ROOT.glob("*.service")) + sorted(SYSTEMD_ROOT.glob("*.timer"))


def script_code(name: str) -> str:
    """A script with its comment lines removed.

    These scripts explain, at length, the dangerous things they do not do -
    `autotrader-deploy` opens by stating that it never passes
    `--confirm-paper-runtime`. A raw-text search finds that sentence and calls
    it a violation, so the assertions below look at executable lines only.
    """
    lines = (BIN_ROOT / name).read_text().splitlines()
    return "\n".join(line for line in lines if not line.strip().startswith("#"))


def deploy_text_files() -> list[Path]:
    """Every artifact under deploy/, for the whole-directory scans."""
    return sorted(p for p in DEPLOY_ROOT.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_the_expected_units_exist() -> None:
    names = {path.name for path in unit_files()}
    assert set(TRADING_UNITS) <= names
    assert set(DASHBOARD_UNITS) <= names
    assert {"autotrader-backup.service", "autotrader-backup.timer"} <= names


@pytest.mark.parametrize("path", unit_files(), ids=lambda p: p.name)
def test_every_unit_parses_and_has_the_sections_systemd_needs(path: Path) -> None:
    unit = parse_unit(path)
    assert "Unit" in unit, f"{path.name} has no [Unit] section"
    if path.suffix == ".service":
        assert "Service" in unit
        assert one(unit, "Service", "ExecStart"), f"{path.name} has no ExecStart"
    if path.suffix == ".timer":
        assert "Timer" in unit
    assert one(unit, "Unit", "Description"), f"{path.name} has no Description"


@pytest.mark.parametrize("name", ALL_SCRIPTS)
def test_every_script_is_executable(name: str) -> None:
    path = BIN_ROOT / name
    assert path.is_file(), f"{name} is missing"
    assert path.stat().st_mode & stat.S_IXUSR, f"{name} is not executable"


@pytest.mark.parametrize("name", BASH_SCRIPTS)
def test_every_bash_script_parses(name: str) -> None:
    result = subprocess.run(
        ["bash", "-n", str(BIN_ROOT / name)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", BASH_SCRIPTS)
def test_every_bash_script_fails_fast(name: str) -> None:
    """`set -euo pipefail`, so a half-finished deploy stops rather than continuing."""
    assert "set -euo pipefail" in (BIN_ROOT / name).read_text()


@pytest.mark.parametrize("name", PYTHON_SCRIPTS)
def test_every_python_script_parses(name: str) -> None:
    ast.parse((BIN_ROOT / name).read_text())


# ---------------------------------------------------------------------------
# Paper only
# ---------------------------------------------------------------------------


#: Spellings of "go live" that must never appear in a deployment artifact.
#: `paper=False` and `--live` are the two the application could not express
#: even if asked; a deployment file inventing them would be inventing a mode.
LIVE_PATTERNS = (
    re.compile(r"paper\s*=\s*False", re.IGNORECASE),
    re.compile(r"--live\b"),
    re.compile(r"\bLIVE_TRADING\b"),
    re.compile(r"api\.alpaca\.markets"),
)


@pytest.mark.parametrize("path", deploy_text_files(), ids=lambda p: str(p.relative_to(DEPLOY_ROOT)))
def test_no_deployment_artifact_names_a_live_mode(path: Path) -> None:
    text = path.read_text()
    for pattern in LIVE_PATTERNS:
        assert not pattern.search(text), f"{path.name} matches {pattern.pattern}"


def test_the_paper_endpoint_is_the_only_alpaca_host_mentioned() -> None:
    """`paper-api.alpaca.markets` may appear; the live host may not."""
    for path in deploy_text_files():
        for line in path.read_text().splitlines():
            if "alpaca.markets" in line:
                assert "paper-api.alpaca.markets" in line, f"{path.name}: {line.strip()!r}"


# ---------------------------------------------------------------------------
# Restart semantics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", TRADING_UNITS)
def test_a_trading_runtime_is_never_restarted_after_a_safety_halt(name: str) -> None:
    """Exit 2 means an order may exist at the broker. systemd must leave it alone."""
    unit = parse_unit(SYSTEMD_ROOT / name)
    prevented = one(unit, "Service", "RestartPreventExitStatus")
    assert prevented is not None, f"{name} has no RestartPreventExitStatus"
    assert "2" in prevented.split(), f"{name} does not hold exit 2 down: {prevented!r}"


@pytest.mark.parametrize("name", TRADING_UNITS)
def test_a_trading_runtime_does_not_restart_unconditionally(name: str) -> None:
    unit = parse_unit(SYSTEMD_ROOT / name)
    assert one(unit, "Service", "Restart") == "on-failure"


@pytest.mark.parametrize("name", TRADING_UNITS)
def test_a_trading_runtime_cannot_flap_forever(name: str) -> None:
    """A cause that never clears must exhaust the limit and stay visibly failed."""
    unit = parse_unit(SYSTEMD_ROOT / name)
    assert one(unit, "Unit", "StartLimitBurst") is not None
    assert one(unit, "Unit", "StartLimitIntervalSec") is not None


@pytest.mark.parametrize("name", TRADING_UNITS)
def test_a_trading_runtime_is_stopped_gracefully(name: str) -> None:
    """SIGTERM and time to finish: a runtime killed mid-submission is an UNKNOWN."""
    unit = parse_unit(SYSTEMD_ROOT / name)
    assert one(unit, "Service", "KillSignal") == "SIGTERM"
    assert int(one(unit, "Service", "TimeoutStopSec") or 0) >= 60


# ---------------------------------------------------------------------------
# Gates: nothing in a unit body may open one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", unit_files(), ids=lambda p: p.name)
def test_no_unit_opens_the_paper_trading_gate(path: Path) -> None:
    unit = parse_unit(path)
    for value in directive(unit, "Service", "Environment"):
        assert not value.startswith("AUTOTRADER_PAPER_TRADING_ENABLED"), path.name


@pytest.mark.parametrize("path", unit_files(), ids=lambda p: p.name)
def test_no_unit_carries_the_runtime_confirmation_token(path: Path) -> None:
    """`--confirm-paper-runtime PAPER` arrives from the activation file, or not at all."""
    unit = parse_unit(path)
    for value in directive(unit, "Service", "ExecStart"):
        assert "--confirm-paper-runtime" not in value, path.name


@pytest.mark.parametrize("name", TRADING_UNITS)
def test_a_trading_runtime_reads_the_activation_file_last_and_optionally(name: str) -> None:
    """Order is the override, and the leading `-` is what makes absence safe."""
    unit = parse_unit(SYSTEMD_ROOT / name)
    files = directive(unit, "Service", "EnvironmentFile")
    assert files, f"{name} loads no EnvironmentFile"
    assert files[-1].lstrip("-").endswith(ACTIVATION_BASENAME), files
    assert files[-1].startswith("-"), f"{name}: activation file must be optional: {files[-1]!r}"


def test_the_default_posture_in_the_shared_env_file_is_observe_only() -> None:
    text = (ENV_ROOT / "autotrader.env.example").read_text()
    assert "AUTOTRADER_PAPER_TRADING_ENABLED=false" in text
    assert "AUTOTRADER_CRYPTO_ARGS=--observe-only" in text
    assert "AUTOTRADER_EQUITY_ARGS=--observe-only" in text


# ---------------------------------------------------------------------------
# Deploy is not activation
# ---------------------------------------------------------------------------


def test_the_deploy_script_cannot_enable_trading() -> None:
    """The separation is only real if the deploy script cannot reach the gate."""
    code = script_code("autotrader-deploy")
    assert "AUTOTRADER_PAPER_TRADING_ENABLED=true" not in code
    assert "--confirm-paper-runtime" not in code


def test_only_the_activation_script_writes_the_activation_file() -> None:
    """Any other script gaining the ability to write it would collapse the split."""
    writers = []
    for name in ALL_SCRIPTS:
        for line in script_code(name).splitlines():
            stripped = line.strip()
            # Both spellings: the scripts address the file through
            # $ACTIVATION_FILE, and a future edit might inline the path.
            if "ACTIVATION_FILE" not in stripped and ACTIVATION_BASENAME not in stripped:
                continue
            if re.match(r"^ACTIVATION_FILE=", stripped):  # the definition, not a write
                continue
            creates = re.search(r"\b(install|tee)\b", stripped)
            redirects = re.search(r"(^|\s)(cat|echo|printf)\b.*>", stripped) or re.search(
                r">>?\s*\"?\$\{?ACTIVATION_FILE", stripped
            )
            if creates or redirects:
                writers.append(name)
                break
    assert writers == ["autotrader-enable-paper-trading"], writers


def test_the_activation_script_demands_a_typed_confirmation() -> None:
    text = (BIN_ROOT / "autotrader-enable-paper-trading").read_text()
    assert 'CONFIRMATION="ENABLE PAPER TRADING"' in text
    assert '[ "${reply}" = "${CONFIRMATION}" ]' in text


def test_activation_can_be_reversed() -> None:
    text = (BIN_ROOT / "autotrader-enable-paper-trading").read_text()
    assert "--disable" in text


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def test_no_unit_embeds_a_credential() -> None:
    """Credentials reach a process through EnvironmentFile, never a unit body."""
    for path in unit_files():
        unit = parse_unit(path)
        for value in directive(unit, "Service", "Environment"):
            assert not value.startswith(("ALPACA_API_KEY", "ALPACA_SECRET_KEY")), path.name


def test_the_units_that_need_credentials_reference_the_secrets_file() -> None:
    for name in (*TRADING_UNITS, "autotrader-dashboard-api.service"):
        unit = parse_unit(SYSTEMD_ROOT / name)
        files = " ".join(directive(unit, "Service", "EnvironmentFile"))
        assert "autotrader.secrets.env" in files, name


def test_the_frontend_never_receives_a_credential() -> None:
    """It is the one process that renders bytes into a browser."""
    unit = parse_unit(SYSTEMD_ROOT / "autotrader-dashboard-web.service")
    files = " ".join(directive(unit, "Service", "EnvironmentFile"))
    assert "autotrader.secrets.env" not in files
    assert ACTIVATION_BASENAME not in files


def test_the_credential_template_ships_empty() -> None:
    text = (ENV_ROOT / "autotrader.secrets.env.example").read_text()
    assert "ALPACA_API_KEY=\n" in text
    assert "ALPACA_SECRET_KEY=\n" in text


#: Alpaca keys are alphanumeric and 20+ characters. This is a shape check, not
#: a vault: it catches a real key pasted into a tracked file, which is the
#: mistake that actually happens.
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"\b(ALPACA_API_KEY|ALPACA_SECRET_KEY)[ \t]*=[ \t]*['\"]?([A-Za-z0-9/+_-]{16,})",
    re.IGNORECASE,
)
_ALLOWED_PLACEHOLDERS = re.compile(r"PLACEHOLDER|REPLACE|EXAMPLE|YOUR_|\.\.\.|xxx", re.IGNORECASE)


def test_no_deployment_artifact_contains_something_shaped_like_a_key() -> None:
    for path in deploy_text_files():
        for match in _CREDENTIAL_ASSIGNMENT.finditer(path.read_text()):
            assert _ALLOWED_PLACEHOLDERS.search(match.group(2)), (
                f"{path.relative_to(REPOSITORY_ROOT)}: {match.group(1)} looks like a real value"
            )


def test_the_repository_dotenv_is_not_vendored_into_the_deploy_package() -> None:
    """A developer .env copied into the branch is the classic way keys get committed."""
    for path in deploy_text_files():
        assert path.name not in {".env", ".env.local"}, path


def test_the_activation_script_does_not_read_credential_values() -> None:
    """It checks that the keys are present. It must not print, log or use them."""
    text = (BIN_ROOT / "autotrader-enable-paper-trading").read_text()
    assert "grep -qE '^ALPACA_API_KEY=.+'" in text
    for pattern in ("echo ${ALPACA", "echo $ALPACA", 'printf "${ALPACA'):
        assert pattern not in text


# ---------------------------------------------------------------------------
# Network exposure
# ---------------------------------------------------------------------------


def test_the_frontend_is_pinned_to_loopback() -> None:
    """`next start` binds 0.0.0.0 by default, so the flag is the whole boundary."""
    unit = parse_unit(SYSTEMD_ROOT / "autotrader-dashboard-web.service")
    exec_start = one(unit, "Service", "ExecStart") or ""
    assert "--hostname 127.0.0.1" in exec_start, exec_start


def test_no_unit_directive_binds_a_wildcard_address() -> None:
    """Comments explaining `0.0.0.0` are fine. A directive containing it is not."""
    for path in unit_files():
        unit = parse_unit(path)
        for _section, entries in unit.items():
            for key, value in entries:
                assert "0.0.0.0" not in value, f"{path.name}: {key}={value}"
                assert "::" not in value or "://" in value, f"{path.name}: {key}={value}"


def test_the_dashboard_api_is_not_given_a_host_argument() -> None:
    """Its entry point hardcodes 127.0.0.1; a --host here would be a new way to fail."""
    unit = parse_unit(SYSTEMD_ROOT / "autotrader-dashboard-api.service")
    exec_start = one(unit, "Service", "ExecStart") or ""
    assert "--host " not in exec_start, exec_start


def test_the_proxy_template_keeps_an_authentication_boundary() -> None:
    text = (DEPLOY_ROOT / "caddy" / "Caddyfile.example").read_text()
    assert "basic_auth" in text
    # One upstream. A second route straight to the API would bypass basic_auth.
    assert text.count("reverse_proxy") == 1, "the API must not be proxied separately"
    assert "127.0.0.1:3000" in text


# ---------------------------------------------------------------------------
# Dashboard independence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", TRADING_UNITS)
def test_no_trading_unit_depends_on_the_dashboard(name: str) -> None:
    unit = parse_unit(SYSTEMD_ROOT / name)
    for key in ("Requires", "Wants", "After", "Before", "BindsTo", "PartOf"):
        for value in directive(unit, "Unit", key):
            assert "dashboard" not in value, f"{name}: {key}={value}"


@pytest.mark.parametrize("name", DASHBOARD_UNITS)
def test_no_dashboard_unit_can_stop_or_start_trading(name: str) -> None:
    unit = parse_unit(SYSTEMD_ROOT / name)
    for key in ("Requires", "Wants", "After", "Before", "BindsTo", "PartOf"):
        for value in directive(unit, "Unit", key):
            assert "autotrader-crypto" not in value, f"{name}: {key}={value}"
            assert "autotrader-equity" not in value, f"{name}: {key}={value}"


def test_the_frontend_only_wants_the_api_and_does_not_require_it() -> None:
    """A dropped API must not tear down the page that is reporting it dropped."""
    unit = parse_unit(SYSTEMD_ROOT / "autotrader-dashboard-web.service")
    assert directive(unit, "Unit", "Wants") == ["autotrader-dashboard-api.service"]
    assert not directive(unit, "Unit", "Requires")


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------


def test_every_unit_that_touches_state_declares_the_state_directory() -> None:
    """`StateDirectory=` is what creates /var/lib/autotrader with the right owner."""
    for name in (*TRADING_UNITS, "autotrader-dashboard-api.service", "autotrader-backup.service"):
        unit = parse_unit(SYSTEMD_ROOT / name)
        assert one(unit, "Service", "StateDirectory") == "autotrader", name


def test_the_dashboard_api_state_directory_is_not_mounted_read_only() -> None:
    """A WAL reader writes the -shm index; ReadOnlyPaths here breaks it at start."""
    unit = parse_unit(SYSTEMD_ROOT / "autotrader-dashboard-api.service")
    for value in directive(unit, "Service", "ReadOnlyPaths"):
        assert "/var/lib/autotrader" not in value


#: Ways a deployment script could destroy state or history. None of these are
#: things a deploy needs, and each one has removed a production database
#: somewhere.
DESTRUCTIVE_PATTERNS = (
    re.compile(r"rm\s+(-[a-zA-Z]*\s+)*-?[a-zA-Z]*[rf]"),
    re.compile(r"git\s+reset\s+--hard"),
    re.compile(r"git\s+push\s+.*--force"),
    re.compile(r"git\s+clean\s+-[a-zA-Z]*[dfx]"),
    re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\s+TABLE\b", re.IGNORECASE),
    re.compile(r"mkfs|dd\s+if="),
)


@pytest.mark.parametrize("name", ALL_SCRIPTS)
def test_no_script_destroys_state_or_rewrites_history(name: str) -> None:
    for number, line in enumerate((BIN_ROOT / name).read_text().splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pattern in DESTRUCTIVE_PATTERNS:
            match = pattern.search(stripped)
            if match is None:
                continue
            # The one deletion in the package: `--disable` removing the
            # activation file. That file is not state, it is a switch, and
            # deleting it is the documented off switch.
            if name == "autotrader-enable-paper-trading" and ACTIVATION_BASENAME in stripped:
                continue
            if "ACTIVATION_FILE" in stripped:
                continue
            raise AssertionError(f"{name}:{number}: {match.group(0)!r} in {stripped!r}")


def test_the_deploy_script_never_writes_the_database() -> None:
    text = (BIN_ROOT / "autotrader-deploy").read_text()
    assert "autotrader.db" not in text


def test_the_deploy_script_touches_the_state_directory_only_to_append_its_log() -> None:
    """One append-only line. Anything else under /var/lib is out of scope for a deploy."""
    text = (BIN_ROOT / "autotrader-deploy").read_text()
    redirects = [
        line.strip()
        for line in text.splitlines()
        if "STATE_DIR" in line and (">" in line or "rm " in line or "cp " in line)
    ]
    assert redirects == [], redirects
    assert '>>"${DEPLOY_LOG}"' in text


def test_the_rollback_script_does_not_restore_a_database() -> None:
    """Code rolls back; the broker's record of what happened does not."""
    text = (BIN_ROOT / "autotrader-rollback").read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not re.search(r"\bcp\b.*\.db", stripped), stripped
        assert "--restore" not in stripped


def test_the_rollback_script_checks_schema_compatibility_before_stopping_anything() -> None:
    text = (BIN_ROOT / "autotrader-rollback").read_text()
    gate = text.index("SCHEMA INCOMPATIBLE")
    stop = text.index("Stopping services")
    assert gate < stop, "the schema gate must run before any service is stopped"


def test_the_rollback_script_fails_closed_when_it_cannot_tell() -> None:
    text = (BIN_ROOT / "autotrader-rollback").read_text()
    assert "Refusing to roll back blind" in text


# ---------------------------------------------------------------------------
# The health check reads
# ---------------------------------------------------------------------------


def test_the_health_check_issues_no_write_statement() -> None:
    text = (BIN_ROOT / "autotrader-healthcheck").read_text()
    for statement in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "CREATE TABLE", "ALTER "):
        assert statement not in text.upper().replace("CREATE THE", ""), statement


def test_the_health_check_opens_the_database_without_write_intent() -> None:
    text = (BIN_ROOT / "autotrader-healthcheck").read_text()
    assert "mode=ro" in text
    assert "PRAGMA query_only = ON" in text


def test_the_health_check_never_repairs_what_it_finds() -> None:
    """It reports UNRESOLVED. A human runs reconcile."""
    tree = ast.parse((BIN_ROOT / "autotrader-healthcheck").read_text())
    commands: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Attribute) and target.attr in {"run", "Popen", "call"}:
            for argument in node.args:
                if isinstance(argument, ast.List):
                    for element in argument.elts:
                        if isinstance(element, ast.Constant) and isinstance(element.value, str):
                            commands.append(element.value)
    assert commands, "expected at least one subprocess invocation to inspect"
    assert "systemctl" in commands
    for command in commands:
        assert "reconcile" not in command
        assert "crypto-run" not in command
        assert "equity-run" not in command


def test_the_health_check_tolerates_a_schema_it_has_not_seen() -> None:
    """Combined Integration will add tables. This must not crash on them."""
    text = (BIN_ROOT / "autotrader-healthcheck").read_text()
    assert "sqlite_master" in text
    assert "PRAGMA table_info" in text
    assert "no shared safety table on this schema yet" in text


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def test_the_backup_uses_the_online_backup_api_rather_than_a_file_copy() -> None:
    text = (BIN_ROOT / "autotrader-backup").read_text()
    assert "connection.backup(" in text
    assert "shutil.copy" not in text


def test_the_backup_never_overwrites_an_existing_file() -> None:
    text = (BIN_ROOT / "autotrader-backup").read_text()
    assert "refusing to overwrite it" in text


def test_backup_retention_can_only_delete_files_it_recognizes() -> None:
    """A misconfigured --into must not make the live database look expired."""
    text = (BIN_ROOT / "autotrader-backup").read_text()
    assert "_looks_like_backup(p)" in text
    assert "datetime.strptime(stamp, TIMESTAMP_FORMAT)" in text


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------


def test_the_runbook_exists_and_covers_the_operations_an_operator_needs() -> None:
    text = (REPOSITORY_ROOT / "docs" / "DEPLOYMENT.md").read_text()
    for heading in (
        "Deploy is not activation",
        "Filesystem layout",
        "Secrets",
        "Restart policy",
        "SQLite",
        "Networking",
        "Runbook",
        "What is still pinned to Combined Integration",
    ):
        assert heading in text, heading


def test_the_runbook_documents_the_emergency_stop_as_placing_no_orders() -> None:
    text = (REPOSITORY_ROOT / "docs" / "DEPLOYMENT.md").read_text()
    assert "It places no orders." in text
