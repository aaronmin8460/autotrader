"""Phase 0 foundation tests: the package and CLI load. No I/O, no network."""

import subprocess
import sys

import autotrader
from autotrader.cli import app


def test_package_imports() -> None:
    assert autotrader.__name__ == "autotrader"


def test_version_is_defined() -> None:
    assert isinstance(autotrader.__version__, str)
    assert autotrader.__version__


def test_cli_app_is_loadable() -> None:
    assert app.info.name == "autotrader"


def test_cli_help_runs() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "autotrader.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "autotrader" in result.stdout
