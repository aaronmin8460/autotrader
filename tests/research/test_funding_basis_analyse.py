"""The verdict arithmetic, tested against constructed cells.

The success threshold is the one thing in this pilot that must not bend, so
its evaluation is tested directly: a set of records engineered to sit just
above and just below each of the three predeclared criteria.
"""

from __future__ import annotations

from studies.crypto_funding_basis_pilot import analyse

WINDOWS_2024 = ("P3", "W01", "W02", "W03", "W04", "W05", "W06", "W07")
WINDOWS_2021 = tuple(f"X{i:02d}" for i in range(1, 10))


def record(symbol, window, delta_log_loss, delta_forced):
    """One paired record with only the fields the verdict reads."""
    return {
        "symbol": symbol,
        "window": window,
        "era": "2021-23" if window in WINDOWS_2021 else "2024-26",
        "baseline_log_loss": 0.7,
        "treatment_log_loss": 0.7 + delta_log_loss,
        "delta_log_loss": delta_log_loss,
        "baseline_forced": 0.0,
        "treatment_forced": delta_forced,
        "delta_forced": delta_forced,
    }


def build(delta_log_loss, delta_forced, windows=WINDOWS_2024 + WINDOWS_2021):
    return [
        record(symbol, window, delta_log_loss, delta_forced)
        for window in windows
        for symbol in ("BTC/USD", "ETH/USD")
    ]


def test_all_criteria_met_when_everything_improves():
    verdict = analyse.verdict(build(-0.01, 0.02))
    assert verdict["windows_scored"] == 17
    assert verdict["log_loss_wins"] == 17
    assert verdict["criterion_1_log_loss_wins"]
    assert verdict["criterion_2_net_improvement"]
    assert verdict["criterion_3_era_2021_23_not_negative"]
    assert verdict["all_criteria_met"]


def test_failing_the_window_count_fails_the_verdict():
    records = build(-0.01, 0.02)
    # Flip 6 windows to losses -> 11 wins, one short of the required 12.
    for row in records:
        if row["window"] in ("X01", "X02", "X03", "X04", "X05", "X06"):
            row["delta_log_loss"] = +0.01
    verdict = analyse.verdict(records)
    assert verdict["log_loss_wins"] == 11
    assert not verdict["criterion_1_log_loss_wins"]
    assert not verdict["all_criteria_met"]


def test_net_improvement_below_the_bar_fails():
    verdict = analyse.verdict(build(-0.01, 0.0149))
    assert verdict["criterion_1_log_loss_wins"]
    assert not verdict["criterion_2_net_improvement"]
    assert not verdict["all_criteria_met"]

    passing = analyse.verdict(build(-0.01, 0.0150))
    assert passing["criterion_2_net_improvement"]


def test_a_negative_2021_23_era_fails_even_with_a_strong_modern_era():
    records = build(-0.01, 0.0)
    for row in records:
        row["delta_forced"] = 0.10 if row["era"] == "2024-26" else -0.01
        row["treatment_forced"] = row["delta_forced"]
    verdict = analyse.verdict(records)
    assert verdict["era_2024_26_delta_forced"] > 0
    assert verdict["era_2021_23_delta_forced"] < 0
    assert not verdict["criterion_3_era_2021_23_not_negative"]
    assert not verdict["all_criteria_met"]


def test_drop_one_attacks_expose_a_single_window_carrying_the_result():
    records = build(-0.01, 0.0)
    for row in records:
        # One spectacular window, everything else flat.
        row["delta_forced"] = 1.0 if row["window"] == "W03" else 0.0
        row["treatment_forced"] = row["delta_forced"]
    verdict = analyse.verdict(records)
    assert verdict["mean_delta_forced_per_window"] > 0.05
    assert verdict["strongest_window"] == "W03"
    assert verdict["delta_forced_without_strongest_window"] == 0.0
    assert verdict["windows_with_positive_delta_forced"] == 1


def test_drop_one_symbol_attack_isolates_a_single_symbol_result():
    records = build(-0.01, 0.0)
    for row in records:
        row["delta_forced"] = 0.10 if row["symbol"] == "BTC/USD" else 0.0
        row["treatment_forced"] = row["delta_forced"]
    verdict = analyse.verdict(records)
    assert verdict["strongest_symbol"] == "BTC/USD"
    assert verdict["delta_forced_without_strongest_symbol"] == 0.0


def test_materiality_bar_flags_a_trivial_log_loss_difference():
    trivial = analyse.verdict(build(-0.001, 0.02))
    assert not trivial["mean_delta_log_loss_material"]
    material = analyse.verdict(build(-0.003, 0.02))
    assert material["mean_delta_log_loss_material"]


def test_window_level_averages_the_two_symbols():
    records = [
        record("BTC/USD", "W01", -0.02, 0.04),
        record("ETH/USD", "W01", 0.00, 0.00),
    ]
    windows = analyse.window_level(records)
    assert len(windows) == 1
    assert windows[0]["delta_log_loss"] == -0.01
    assert windows[0]["delta_forced"] == 0.02
    assert windows[0]["symbols"] == 2


def test_the_predeclared_constants_are_what_the_design_fixed():
    assert analyse.REQUIRED_WINDOW_WINS == 12
    assert analyse.REQUIRED_WINDOW_TOTAL == 17
    assert analyse.REQUIRED_NET_IMPROVEMENT_PER_WINDOW == 0.015
    assert analyse.MATERIALITY == 0.002
    assert analyse.PRIMARY_HORIZON == 96
    assert analyse.PRIMARY_GATE == "q80"
    assert analyse.PRIMARY_COST == "crypto-taker"
    assert len(analyse.ERA_2021_23) == 9
    assert len(analyse.ERA_2024_26) == 8
