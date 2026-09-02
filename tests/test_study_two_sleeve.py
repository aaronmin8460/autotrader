"""Two-sleeve blend semantics (ledger §L16). Constructed inputs only — no
dataset, no network, no runtime."""

from __future__ import annotations

import pandas as pd
import pytest
from studies.equity_two_sleeve.blend import (
    BlendError,
    combine_targets,
    scale_targets,
    target_diagnostics,
)

T0 = pd.Timestamp("2024-06-03 14:30:00+00:00")
T1 = pd.Timestamp("2024-06-03 14:45:00+00:00")


class TestCombine:
    def test_overlap_symbols_sum_to_one_final_target(self) -> None:
        sleeve_e = {"AAA": {T0: 0.10, T1: 0.10}, "BBB": {T0: 0.10, T1: 0.0}}
        sleeve_a = {"AAA": {T0: 0.04, T1: 0.02}, "CCC": {T0: 0.04, T1: 0.04}}
        combined = combine_targets([(0.6, sleeve_e), (0.3, sleeve_a)])
        assert combined["AAA"][T0] == pytest.approx(0.6 * 0.10 + 0.3 * 0.04)
        assert combined["AAA"][T1] == pytest.approx(0.6 * 0.10 + 0.3 * 0.02)
        assert combined["BBB"][T1] == 0.0
        assert combined["CCC"][T0] == pytest.approx(0.012)
        assert set(combined) == {"AAA", "BBB", "CCC"}

    def test_input_order_invariance(self) -> None:
        sleeve_e = {"AAA": {T0: 0.10}, "BBB": {T0: 0.08}}
        sleeve_a = {"AAA": {T0: 0.05}}
        one = combine_targets([(0.6, sleeve_e), (0.3, sleeve_a)])
        two = combine_targets([(0.3, sleeve_a), (0.6, sleeve_e)])
        assert one == two
        assert list(one) == sorted(one)

    def test_cash_retention_no_redistribution(self) -> None:
        # A defensive sleeve (all zero weights) must leave the total at the
        # other sleeve's scaled weights — never absorb the idle budget.
        sleeve_e = {"AAA": {T0: 0.0}}  # defensive: stance 0
        sleeve_a = {"AAA": {T0: 0.05}, "CCC": {T0: 0.04}}
        combined = combine_targets([(0.6, sleeve_e), (0.3, sleeve_a)])
        total = sum(series[T0] for series in combined.values())
        assert total == pytest.approx(0.3 * 0.09)

    def test_combined_cap_binds_excess_to_cash(self) -> None:
        sleeve_e = {"AAA": {T0: 0.10}}
        sleeve_a = {"AAA": {T0: 0.12}}
        combined = combine_targets([(0.7, sleeve_e), (0.3, sleeve_a)], cap=0.10)
        assert combined["AAA"][T0] == 0.10

    def test_budget_overflow_refused(self) -> None:
        with pytest.raises(BlendError):
            combine_targets([(0.7, {"AAA": {T0: 0.1}}), (0.4, {"AAA": {T0: 0.1}})])

    def test_misaligned_bars_refused(self) -> None:
        with pytest.raises(BlendError):
            combine_targets([(0.5, {"AAA": {T0: 0.1}}), (0.4, {"AAA": {T1: 0.1}})])


class TestScale:
    def test_scale_is_uniform(self) -> None:
        scaled = scale_targets({"AAA": {T0: 0.10, T1: 0.04}}, 0.9)
        assert scaled["AAA"][T0] == pytest.approx(0.09)
        assert scaled["AAA"][T1] == pytest.approx(0.036)

    def test_scale_bounds(self) -> None:
        with pytest.raises(BlendError):
            scale_targets({"AAA": {T0: 0.1}}, 1.2)


class TestDiagnostics:
    def test_concentration_accounting(self) -> None:
        targets = {
            "AAA": {T0: 0.06, T1: 0.08},
            "BBB": {T0: 0.05, T1: 0.01},
            "CCC": {T0: 0.0, T1: 0.02},
        }
        diag = target_diagnostics(targets)
        assert diag["max_symbol_weight"] == pytest.approx(0.08)
        assert diag["max_symbol_weight_symbol"] == "AAA"
        assert diag["peak_bar_total_weight"] == pytest.approx(0.11)
        assert diag["final_bar_top5_weight"] == pytest.approx(0.11)
        assert diag["final_bar_active_names"] == 3
