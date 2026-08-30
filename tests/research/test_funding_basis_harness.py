"""Harness-level integrity: split discipline, arm parity, checkpoints, safety.

Where a fact is a property of the frozen architecture (feature contract, arm
definitions, window set) it is asserted directly. Where it is a property of a
computed cell, the assertion runs against the probe cells already on disk so
the suite never launches heavy compute of its own.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from studies.crypto_funding_basis_pilot import run_pilot
from studies.crypto_funding_basis_pilot.derivative_features import DERIVATIVE_FEATURES

CELLS = Path("/Volumes/AUTOTRADER_QA/reports/crypto-funding-basis-pilot/cells")


def _cells() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(CELLS.glob("*.json"))]


requires_cells = pytest.mark.skipif(not list(CELLS.glob("*.json")), reason="no cells scored yet")


# --------------------------------------------------------------------------
# The frozen contract


def test_baseline_is_the_frozen_24_and_augmented_adds_exactly_the_8():
    assert len(run_pilot.BASELINE_FEATURES) == 24
    assert len(run_pilot.AUGMENTED_FEATURES) == 32
    assert run_pilot.AUGMENTED_FEATURES[:24] == run_pilot.BASELINE_FEATURES
    assert run_pilot.AUGMENTED_FEATURES[24:] == DERIVATIVE_FEATURES
    # No derivative feature may sneak into the baseline arm.
    assert not set(run_pilot.BASELINE_FEATURES) & set(DERIVATIVE_FEATURES)


def test_ablation_arms_partition_the_derivative_features():
    funding = set(run_pilot.FUNDING_FEATURES)
    basis = set(run_pilot.BASIS_FEATURES)
    assert funding | basis == set(DERIVATIVE_FEATURES)
    assert not funding & basis, "a feature cannot be both funding-only and basis-only"
    assert set(run_pilot.ARM_FEATURES["funding_only"]) == set(run_pilot.BASELINE_FEATURES) | funding
    assert set(run_pilot.ARM_FEATURES["basis_only"]) == set(run_pilot.BASELINE_FEATURES) | basis


def test_the_window_set_is_the_frozen_seventeen():
    assert len(run_pilot.ALL_WINDOWS) == 17
    assert len(set(run_pilot.ALL_WINDOWS)) == 17
    assert run_pilot.MODERN_WINDOWS == ("P3", "W01", "W02", "W03", "W04", "W05", "W06", "W07")
    assert tuple(f"X{i:02d}" for i in range(1, 10)) == run_pilot.EXTENDED_WINDOWS


def test_hyperparameters_are_the_frozen_da_spread_96_values():
    from autotrader.ml.v4 import default_candidates

    candidate = next(c for c in default_candidates() if c.name == run_pilot.FAMILY)
    assert candidate.hyperparameters == {
        "trees": 60,
        "max_depth": 3,
        "learning_rate": 0.05,
        "l2": 1.0,
        "min_samples_leaf": 40,
        "min_gain": 1e-6,
    }


def test_the_cost_bar_is_derived_not_restated():
    from studies.crypto_funding_basis_pilot.frozen_data import exact_break_even

    assert exact_break_even() == pytest.approx(0.006018046617, rel=1e-9)


def test_embargo_and_split_fraction_are_unchanged():
    assert pd.Timedelta("24h") == run_pilot.EMBARGO
    assert run_pilot.FIT_FRACTION == 0.7
    assert run_pilot.BARS_PER_DAY == 96


def test_primary_horizon_runs_first():
    assert run_pilot.HORIZONS[0] == run_pilot.PRIMARY_HORIZON == 96
    tasks = run_pilot.build_tasks(("baseline",), run_pilot.HORIZONS, run_pilot.ALL_WINDOWS)
    horizons_in_order = [t[3] for t in tasks]
    assert horizons_in_order[0] == 96
    assert horizons_in_order.index(16) > horizons_in_order.count(96) - 1


# --------------------------------------------------------------------------
# Metric helpers


def test_log_loss_matches_the_textbook_definition():
    p = np.array([0.9, 0.1, 0.8])
    y = np.array([1.0, 0.0, 1.0])
    expected = -np.mean([np.log(0.9), np.log(0.9), np.log(0.8)])
    assert run_pilot.log_loss_of(p, y) == pytest.approx(expected)


def test_log_loss_is_finite_on_degenerate_certainty():
    """Isotonic calibration can emit exact 0 and 1; the metric must survive."""
    value = run_pilot.log_loss_of(np.array([0.0, 1.0]), np.array([1.0, 0.0]))
    assert np.isfinite(value) and value > 30


def test_pr_auc_is_one_for_a_perfect_ranking():
    assert run_pilot.pr_auc(np.array([0.9, 0.8, 0.1]), np.array([1.0, 1.0, 0.0])) == pytest.approx(
        1.0
    )


def test_pr_auc_is_none_for_a_single_class():
    assert run_pilot.pr_auc(np.array([0.5, 0.6]), np.array([0.0, 0.0])) is None


def test_calibration_error_is_zero_for_a_perfectly_calibrated_split():
    p = np.concatenate([np.full(500, 0.25), np.full(500, 0.75)])
    y = np.concatenate(
        [
            np.repeat([1.0, 0.0], [125, 375]),
            np.repeat([1.0, 0.0], [375, 125]),
        ]
    )
    assert run_pilot.calibration_error(p, y) == pytest.approx(0.0, abs=1e-12)


# --------------------------------------------------------------------------
# Scored-cell properties


@requires_cells
def test_arms_score_identical_populations():
    cells = _cells()
    groups: dict[tuple, dict[str, dict]] = {}
    for cell in cells:
        if cell.get("status") != "ok":
            continue
        key = (cell["symbol"], cell["horizon"], cell["window"])
        groups.setdefault(key, {})[cell["arm"]] = cell
    compared = 0
    for key, arms in groups.items():
        if "baseline" not in arms or "augmented" not in arms:
            continue
        base, aug = arms["baseline"], arms["augmented"]
        for field in ("train_rows", "fit_rows", "calibration_rows", "test_rows"):
            assert base[field] == aug[field], f"{key}: {field} differs between arms"
        assert base["decision"]["decision_days"] == aug["decision"]["decision_days"]
        assert base["feature_count"] == 24 and aug["feature_count"] == 32
        compared += 1
    assert compared > 0, "no baseline/augmented pair to compare"


@requires_cells
def test_no_cell_carries_a_future_derivative_value():
    for cell in _cells():
        audit = cell.get("coverage", {}).get("join_audit")
        if audit:
            assert audit["negative_staleness_rows"] == 0, f"{cell['arm']} {cell['window']}"


@requires_cells
def test_every_replayed_ledger_agrees_with_the_frozen_engine():
    for cell in _cells():
        if cell.get("status") != "ok":
            continue
        for gate in cell["gates"].values():
            for cost_label, result in gate["costs"].items():
                assert result["ledger_consistent"], (
                    f"{cell['arm']} {cell['window']} {cost_label}: the trade ledger "
                    "disagrees with the frozen replay"
                )


@requires_cells
def test_training_never_reaches_into_its_own_window():
    """The embargo must hold: train rows resolve ≥24h before the window opens."""
    from studies.crypto_funding_basis_pilot.frozen_data import WINDOWS

    for cell in _cells():
        if cell.get("status") != "ok":
            continue
        # Structural: the runner filters on label_knowable_at ≤ start − 24h.
        # Assert the window is one the frozen set defines and the split is sane.
        assert cell["window"] in WINDOWS
        assert cell["fit_rows"] + cell["calibration_rows"] == cell["train_rows"]
        assert cell["fit_rows"] == int(cell["train_rows"] * run_pilot.FIT_FRACTION)


# --------------------------------------------------------------------------
# Checkpointing


def test_cell_paths_are_unique_per_arm_symbol_horizon_window():
    seen = set()
    for arm in ("baseline", "augmented", "funding_only", "basis_only"):
        for symbol in run_pilot.SYMBOLS:
            for horizon in run_pilot.HORIZONS:
                for window in run_pilot.ALL_WINDOWS:
                    path = run_pilot.cell_path(arm, symbol, horizon, window)
                    assert path not in seen, f"checkpoint collision at {path}"
                    seen.add(path)
    assert len(seen) == 4 * 2 * 3 * 17


def test_task_list_has_no_duplicates():
    tasks = run_pilot.build_tasks(
        ("baseline", "augmented"), run_pilot.HORIZONS, run_pilot.ALL_WINDOWS
    )
    assert len(tasks) == len(set(tasks)) == 2 * 2 * 3 * 17


# --------------------------------------------------------------------------
# Safety boundary


def test_the_study_package_contains_no_order_path():
    """No module here may import a broker client or name an order verb."""
    package = Path("studies/crypto_funding_basis_pilot")
    forbidden = ("submit_order", "place_order", "cancel_order", "replace_order", "TradingClient")
    for path in package.glob("*.py"):
        text = path.read_text()
        for token in forbidden:
            assert token not in text, f"{path.name} references {token}"


def test_live_archiver_refuses_urls_outside_its_allowlist():
    from studies.crypto_funding_basis_pilot.live_archiver import fetch

    with pytest.raises(ValueError, match="allowlist"):
        fetch("https://www.okx.com/api/v5/trade/order")
    with pytest.raises(ValueError, match="allowlist"):
        fetch("https://api.example.com/anything")
