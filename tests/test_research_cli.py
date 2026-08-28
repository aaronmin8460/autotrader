"""The `autotrader research` command group: offline, read-only, honest output.

These tests drive the real Typer application rather than the library behind it,
because the CLI is where a researcher actually reads a number and the way it is
printed is part of whether the number is honest. In particular: an undefined
metric must print as `n/a`, never as `0.00%`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from autotrader.cli import app as root_app
from autotrader.research import storage
from autotrader.research.cli import (
    INPUT_EXIT_CODE,
    LEAKAGE_EXIT_CODE,
    STORAGE_EXIT_CODE,
    UNREADABLE_INPUT_EXIT_CODE,
)
from research_fixtures import equity_bars, flat, multi_cycle, wave

runner = CliRunner()


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    path = tmp_path / "btc.parquet"
    wave(900).to_parquet(path, engine="pyarrow")
    return path


@pytest.fixture
def quiet_dataset(tmp_path: Path) -> Path:
    """A dataset on which nothing ever trades, so metrics are undefined."""
    path = tmp_path / "flat.parquet"
    flat(300).to_parquet(path, engine="pyarrow")
    return path


def invoke(*arguments: str, env: dict[str, str] | None = None):
    return runner.invoke(root_app, ["research", *arguments], env=env)


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


def test_the_research_group_is_registered_on_the_root_cli() -> None:
    result = runner.invoke(root_app, ["--help"])
    assert result.exit_code == 0
    assert "research" in result.stdout


def test_the_group_lists_its_commands() -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    for command in ("replay-dataset", "audit", "sweep"):
        assert command in result.stdout


# --------------------------------------------------------------------------
# replay-dataset
# --------------------------------------------------------------------------


def test_replay_reports_a_full_metric_block(dataset: Path) -> None:
    result = invoke("replay-dataset", str(dataset))
    assert result.exit_code == 0, result.stdout
    for label in (
        "Max drawdown",
        "Sharpe (ann.)",
        "Total return",
        "Win rate",
        "Profit factor",
        "Turnover",
        "Exposure",
        "Fees",
        "Slippage",
    ):
        assert label in result.stdout


def test_replay_states_that_it_is_not_a_profitability_claim(dataset: Path) -> None:
    result = invoke("replay-dataset", str(dataset))
    assert "no broker was contacted" in result.stdout
    assert "not a provider fee schedule" in result.stdout


def test_an_undefined_metric_prints_as_not_available_rather_than_zero(
    quiet_dataset: Path,
) -> None:
    """CRITICAL. A reader must not mistake "could not be computed" for
    "was computed and came out at zero"."""
    result = invoke("replay-dataset", str(quiet_dataset))
    assert result.exit_code == 0, result.stdout
    assert "n/a" in result.stdout
    assert "Trades:" in result.stdout


def test_a_missing_dataset_exits_with_the_unreadable_code(tmp_path: Path) -> None:
    result = invoke("replay-dataset", str(tmp_path / "nope.parquet"))
    assert result.exit_code == UNREADABLE_INPUT_EXIT_CODE


def test_an_unknown_engine_is_refused(dataset: Path) -> None:
    result = invoke("replay-dataset", str(dataset), "--engine", "crystal-ball")
    assert result.exit_code == INPUT_EXIT_CODE
    assert "Known engines" in result.stdout + str(result.stderr or "")


def test_an_unknown_cost_model_is_refused(dataset: Path) -> None:
    result = invoke("replay-dataset", str(dataset), "--cost", "free")
    assert result.exit_code == INPUT_EXIT_CODE


def test_an_equity_dataset_replays_under_the_equity_flag(tmp_path: Path) -> None:
    path = tmp_path / "spy.parquet"
    equity_bars(400, symbol="SPY").to_parquet(path, engine="pyarrow")
    result = invoke("replay-dataset", str(path), "--equity", "--clock", "equity-15m")
    assert result.exit_code == 0, result.stdout
    assert "SPY" in result.stdout
    assert "equity-15m" in result.stdout


def test_an_equity_dataset_is_refused_without_the_flag(tmp_path: Path) -> None:
    path = tmp_path / "spy.parquet"
    equity_bars(400, symbol="SPY").to_parquet(path, engine="pyarrow")
    result = invoke("replay-dataset", str(path))
    assert result.exit_code == INPUT_EXIT_CODE


def test_the_benchmark_engine_is_selectable(dataset: Path) -> None:
    result = invoke("replay-dataset", str(dataset), "--engine", "buy-and-hold")
    assert result.exit_code == 0, result.stdout
    assert "buy-and-hold" in result.stdout


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------


def test_a_clean_audit_exits_zero_and_says_how_hard_it_looked(dataset: Path) -> None:
    result = invoke(
        "audit",
        str(dataset),
        "--train-bars",
        "200",
        "--test-bars",
        "100",
        "--embargo-bars",
        "50",
        "--holdout-bars",
        "150",
    )
    assert result.exit_code == 0, result.stdout
    assert "No leakage detected" in result.stdout
    assert "Perturbation probes" in result.stdout
    assert "not a proof" in result.stdout


def test_the_audit_reports_the_holdout_as_withheld(dataset: Path) -> None:
    result = invoke(
        "audit",
        str(dataset),
        "--train-bars",
        "200",
        "--test-bars",
        "100",
        "--embargo-bars",
        "50",
        "--holdout-bars",
        "150",
    )
    assert "withheld" in result.stdout


def test_an_impossible_split_configuration_is_refused(dataset: Path) -> None:
    result = invoke("audit", str(dataset), "--train-bars", "100000")
    assert result.exit_code == INPUT_EXIT_CODE


def test_an_audit_with_no_embargo_reports_the_missing_declaration(
    dataset: Path,
) -> None:
    """Exits 3, because "nobody declared one" is a finding rather than a pass."""
    result = invoke(
        "audit",
        str(dataset),
        "--train-bars",
        "200",
        "--test-bars",
        "100",
        "--embargo-bars",
        "0",
        "--holdout-bars",
        "150",
    )
    assert result.exit_code == LEAKAGE_EXIT_CODE
    assert "NO_EMBARGO" in result.stdout


# --------------------------------------------------------------------------
# sweep
# --------------------------------------------------------------------------


def test_a_sweep_without_a_reports_root_is_refused(dataset: Path) -> None:
    """Research artifacts go to external storage or nowhere."""
    result = invoke(
        "sweep",
        str(dataset),
        "--study",
        "cli-test",
        env={storage.REPORTS_ENV: ""},
    )
    assert result.exit_code == STORAGE_EXIT_CODE


def test_a_sweep_writes_its_records_and_reports_where(dataset: Path, tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    result = invoke(
        "sweep",
        str(dataset),
        "--study",
        "cli-test",
        "--fast-periods",
        "5,10",
        "--slow-periods",
        "20,40",
        "--train-bars",
        "200",
        "--test-bars",
        "100",
        "--embargo-bars",
        "50",
        "--holdout-bars",
        "150",
        env={storage.REPORTS_ENV: str(reports)},
    )
    assert result.exit_code == 0, result.stdout
    assert "RESEARCH PARAMETER SWEEP" in result.stdout
    assert "withheld" in result.stdout

    run_directories = list((reports / "research" / "cli-test").iterdir())
    assert len(run_directories) == 1
    written = run_directories[0]
    assert (written / storage.MANIFEST_FILENAME).exists()
    assert (written / storage.EXPERIMENTS_FILENAME).exists()
    assert (written / storage.SPLITS_FILENAME).exists()


def test_a_sweep_warns_that_a_best_of_many_score_is_optimistic(
    dataset: Path, tmp_path: Path
) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    result = invoke(
        "sweep",
        str(dataset),
        "--study",
        "cli-warn",
        "--fast-periods",
        "5,10",
        "--slow-periods",
        "20,40",
        "--train-bars",
        "200",
        "--test-bars",
        "100",
        "--embargo-bars",
        "50",
        "--holdout-bars",
        "150",
        "--objective",
        "median-return",
        env={storage.REPORTS_ENV: str(reports)},
    )
    assert result.exit_code == 0, result.stdout
    assert "selection luck" in result.stdout
    assert "Candidates" in result.stdout


def test_non_numeric_periods_are_refused(dataset: Path, tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    result = invoke(
        "sweep",
        str(dataset),
        "--study",
        "bad",
        "--fast-periods",
        "a,b",
        env={storage.REPORTS_ENV: str(reports)},
    )
    assert result.exit_code == INPUT_EXIT_CODE


def test_the_holdout_is_scored_only_when_asked(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    path = tmp_path / "cycles.parquet"
    multi_cycle().to_parquet(path, engine="pyarrow")

    result = invoke(
        "sweep",
        str(path),
        "--study",
        "cli-holdout",
        "--fast-periods",
        "5,10",
        "--slow-periods",
        "20,30",
        "--train-bars",
        "60",
        "--test-bars",
        "60",
        "--embargo-bars",
        "0",
        "--holdout-bars",
        "60",
        "--objective",
        "median-return",
        "--evaluate-holdout",
        env={storage.REPORTS_ENV: str(reports)},
    )
    assert result.exit_code == 0, result.stdout
    assert "FINAL HOLDOUT" in result.stdout
    assert "one candidate, one evaluation" in result.stdout


def test_without_the_flag_the_holdout_is_never_scored(dataset: Path, tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    result = invoke(
        "sweep",
        str(dataset),
        "--study",
        "cli-noholdout",
        "--fast-periods",
        "5,10",
        "--slow-periods",
        "20,40",
        "--train-bars",
        "200",
        "--test-bars",
        "100",
        "--embargo-bars",
        "50",
        "--holdout-bars",
        "150",
        "--objective",
        "median-return",
        env={storage.REPORTS_ENV: str(reports)},
    )
    assert result.exit_code == 0, result.stdout
    assert "FINAL HOLDOUT" not in result.stdout


# --------------------------------------------------------------------------
# The research group cannot trade
# --------------------------------------------------------------------------


def test_no_research_command_accepts_a_confirmation_token() -> None:
    """The trading commands are gated behind one. A research command that had
    such a flag would be a research command that could do something."""
    result = invoke("--help")
    for token in ("--confirm", "--live", "--submit", "--yes-i-mean-it"):
        assert token not in result.stdout
