"""A1-B U30 shadow: frozen policy math and the zero-order structure.

Constructed inputs only — no dataset, no network, no runtime. The parity of
the policy math against the research pipeline on real frozen data is proven
separately by the research program's parity harness; what lives here are the
properties that must hold on any input.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import date

import pytest

from autotrader.equity.a1b_policy import (
    A1BFit,
    a1b_multipliers,
    assign_labels,
    cross_sectional_z_at_mark,
    governing_fit,
    governing_mark,
    load_policy,
    mark_weights,
    tilted_weights,
)
from autotrader.equity.a1b_shadow import (
    DESIGNATION,
    EquityA1BShadowRuntime,
    create_a1b_tables,
)

# ==========================================================================
# The packaged policy
# ==========================================================================


def test_the_policy_loads_and_is_the_frozen_research_artifact() -> None:
    policy = load_policy()
    assert len(policy.u30) == 26
    assert len(policy.u45_z_cross_section) == 45
    assert len(policy.incumbents) == 10
    assert set(policy.incumbents) <= set(policy.u30)
    assert set(policy.u30) <= set(policy.u45_z_cross_section)
    assert len(policy.fits) == 5
    assert [fit.fit_mark.isoformat() for fit in policy.fits] == sorted(
        fit.fit_mark.isoformat() for fit in policy.fits
    )
    assert policy.mark_anchor == date(2021, 9, 30)
    assert policy.mark_every_sessions == 21
    assert policy.mult_clip == (0.6, 1.4)
    assert policy.per_symbol_cap == 0.10
    assert len(policy.policy_hash) == 64


def test_the_policy_hash_is_stable_across_loads() -> None:
    load_policy.cache_clear()
    first = load_policy().policy_hash
    load_policy.cache_clear()
    assert load_policy().policy_hash == first


def test_governing_fit_is_the_latest_at_or_before_the_mark() -> None:
    policy = load_policy()
    assert governing_fit(policy, date(2021, 10, 1)) is None
    assert governing_fit(policy, date(2022, 8, 2)).fit_mark == date(2022, 8, 2)
    assert governing_fit(policy, date(2024, 6, 1)).fit_mark == date(2024, 1, 3)
    assert governing_fit(policy, date(2026, 8, 1)).fit_mark == date(2026, 1, 8)


def test_governing_mark_snaps_to_the_grid() -> None:
    policy = load_policy()
    assert governing_mark(policy, 0) == 0
    assert governing_mark(policy, 20) == 0
    assert governing_mark(policy, 21) == 21
    assert governing_mark(policy, 44) == 42


# ==========================================================================
# Weight construction
# ==========================================================================


def _fit(k: int = 3, betas: dict[int, float] | None = None) -> A1BFit:
    return A1BFit(
        fit_mark=date(2024, 1, 3),
        k=k,
        features=("beta_252", "vol_126"),
        centroids_z=tuple((float(i), float(-i)) for i in range(k)),
        beta_median_of_label=betas if betas is not None else {0: 0.5, 1: 1.0, 2: 1.5},
    )


def test_multipliers_are_beta_over_mean_and_clipped() -> None:
    policy = load_policy()
    mults = a1b_multipliers(policy, _fit())
    assert mults[0] == pytest.approx(0.6)  # 0.5/1.0 = 0.5, clipped up
    assert mults[1] == pytest.approx(1.0)
    assert mults[2] == pytest.approx(1.4)  # 1.5/1.0 = 1.5, clipped down


def test_nonpositive_mean_beta_degrades_to_equal_weights() -> None:
    policy = load_policy()
    mults = a1b_multipliers(policy, _fit(betas={0: -1.0, 1: 0.5, 2: 0.5}))
    assert set(mults.values()) == {1.0}


def test_tilted_weights_renormalize_to_the_base_total_and_cap() -> None:
    symbols = ("AAA", "BBB", "CCC", "DDD")
    weights = tilted_weights(symbols, {"AAA": 2.0, "BBB": 1.0, "CCC": 1.0, "DDD": 1.0}, cap=0.10)
    assert sum(weights.values()) <= 4 * 0.10 + 1e-12
    assert weights["AAA"] == 0.10  # capped after renormalization
    assert weights["BBB"] == weights["CCC"] == weights["DDD"]


def test_tilted_weights_default_to_equal_for_unknown_symbols() -> None:
    weights = tilted_weights(("AAA", "BBB"), {}, cap=0.10)
    assert weights == {"AAA": 0.10, "BBB": 0.10}


def test_mark_weights_before_the_first_fit_are_equal() -> None:
    policy = load_policy()
    active, reserved, labels = mark_weights(policy, None, {})
    assert labels == {}
    assert all(value == pytest.approx(1.0 / 26) for value in active.values())
    assert active == reserved


# ==========================================================================
# Cross-sectional z and label assignment
# ==========================================================================


def test_z_scores_use_the_contemporaneous_cross_section_only() -> None:
    values = {f"S{i}": {"beta_252": float(i)} for i in range(21)}
    z = cross_sectional_z_at_mark(values, ("beta_252",), winsor=3.0, min_symbols=20)
    zs = [z[s]["beta_252"] for s in values]
    assert min(zs) >= -3.0 and max(zs) <= 3.0
    assert zs == sorted(zs)


def test_too_few_symbols_yield_nan_z() -> None:
    values = {f"S{i}": {"beta_252": float(i)} for i in range(5)}
    z = cross_sectional_z_at_mark(values, ("beta_252",), winsor=3.0, min_symbols=20)
    import math

    assert all(math.isnan(z[s]["beta_252"]) for s in values)


def test_labels_are_nearest_centroids_and_nan_skips() -> None:
    fit = _fit()
    z = {
        "AAA": {"beta_252": 0.1, "vol_126": -0.1},
        "BBB": {"beta_252": 2.1, "vol_126": -2.0},
        "CCC": {"beta_252": float("nan"), "vol_126": 0.0},
    }
    labels = assign_labels(fit, z)
    assert labels == {"AAA": 0, "BBB": 2}


# ==========================================================================
# The shadow has no execution path
# ==========================================================================


def test_the_constructor_offers_no_execution_seam() -> None:
    """CRITICAL. There is no parameter a gateway could be handed through."""
    parameters = inspect.signature(EquityA1BShadowRuntime.__init__).parameters
    names = set(parameters)
    assert "execution" not in names
    assert not any("gateway" in name.lower() for name in names)


def test_the_module_imports_nothing_from_the_execution_layer() -> None:
    """CRITICAL. Asserted against the module's stripped source."""
    import autotrader.equity.a1b_shadow as module

    source = inspect.getsource(module)
    assert "autotrader.execution" not in source
    assert "TradingClient" not in source
    assert "submit_order" not in source


def test_the_observation_table_refuses_orders_by_constraint() -> None:
    connection = sqlite3.connect(":memory:")
    create_a1b_tables(connection)
    base = (
        "INSERT INTO a1b_observations ("
        " symbol, bar_timestamp, session_date, participate, v3_signal, v3_stance,"
        " alias_scored, mark_index, mark_date, archetype_label, active_weight,"
        " reserved_weight, target_weight, reference_close, designation,"
        " client_order_id, recorded_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    good = (
        "SPY",
        "2026-09-01T14:45:00+00:00",
        "2026-09-01",
        1,
        "HOLD",
        1,
        0,
        1239,
        "2026-08-14",
        2,
        0.04,
        0.0385,
        0.04,
        640.0,
        DESIGNATION,
        None,
        "2026-09-01T14:46:00+00:00",
    )
    connection.execute(base, good)

    with pytest.raises(sqlite3.IntegrityError):
        bad_designation = list(good)
        bad_designation[0] = "QQQ"
        bad_designation[14] = "EXECUTED"
        connection.execute(base, tuple(bad_designation))

    with pytest.raises(sqlite3.IntegrityError):
        bad_link = list(good)
        bad_link[0] = "IWM"
        bad_link[15] = "order-123"
        connection.execute(base, tuple(bad_link))


def test_a_database_holding_an_order_intent_is_refused() -> None:
    connection = sqlite3.connect(":memory:")
    create_a1b_tables(connection)
    connection.execute("CREATE TABLE order_intents (id INTEGER PRIMARY KEY, payload TEXT)")
    connection.execute("INSERT INTO order_intents (payload) VALUES ('x')")

    runtime = object.__new__(EquityA1BShadowRuntime)
    runtime._connection = connection
    from autotrader.equity.shadow import ShadowIntegrityError

    with pytest.raises(ShadowIntegrityError):
        runtime.assert_no_order_intents()


def test_a_clean_database_passes_the_invariant() -> None:
    connection = sqlite3.connect(":memory:")
    create_a1b_tables(connection)
    connection.execute("CREATE TABLE order_intents (id INTEGER PRIMARY KEY, payload TEXT)")
    runtime = object.__new__(EquityA1BShadowRuntime)
    runtime._connection = connection
    runtime.assert_no_order_intents()


def test_mark_states_from_another_policy_are_refused() -> None:
    connection = sqlite3.connect(":memory:")
    create_a1b_tables(connection)
    connection.execute(
        "INSERT INTO a1b_mark_state ("
        " mark_index, mark_date, fit_mark, labels_json, multipliers_json,"
        " active_weights_json, reserved_weights_json, labeled_symbols,"
        " policy_hash, computed_at"
        ") VALUES (0, '2021-09-30', NULL, '{}', '{}', '{}', '{}', 0,"
        " 'deadbeef', '2026-09-01T00:00:00+00:00')"
    )
    runtime = object.__new__(EquityA1BShadowRuntime)
    runtime._connection = connection
    runtime._policy = load_policy()
    from autotrader.equity.shadow import ShadowIntegrityError

    with pytest.raises(ShadowIntegrityError):
        runtime._require_consistent_policy()


def test_the_cli_command_exists_and_names_the_guarantee() -> None:
    from autotrader.cli import app

    commands = {command.name: command for command in app.registered_commands}
    assert "equity-a1b-shadow" in commands
    help_text = commands["equity-a1b-shadow"].callback.__doc__ or ""
    assert "zero orders" in help_text.lower() or "no execution path" in help_text.lower()


def test_the_cli_command_constructs_nothing_that_can_execute() -> None:
    from autotrader import cli

    code = inspect.getsource(cli.equity_a1b_shadow)
    assert "execution=" not in code
    assert "ExecutionGateway" not in code
    assert "--confirm" not in code
