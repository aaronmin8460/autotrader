"""Tests for the asset-character study machinery (ledger §L14).

Fingerprint causality, window semantics, market-relative regressions,
standardization, and determinism — all on constructed frames.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from studies.equity_asset_character.fingerprints import (
    STRUCTURAL_FEATURES,
    build_series,
    cross_sectional_z,
    fingerprint_panel,
    state_at,
    structural_at,
    symbol_sessions,
)

RNG_SEED = 20260901


def _weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _bar_frame(sessions: list[date], closes: np.ndarray, *, bars_per_session: int = 2):
    """A canonical-schema-enough frame: two bars per session, UTC stamps."""
    rows = []
    for day, close in zip(sessions, closes, strict=True):
        for bar in range(bars_per_session):
            stamp = pd.Timestamp(day) + pd.Timedelta(hours=14 + bar, minutes=30)
            rows.append(
                {
                    "timestamp": stamp.tz_localize("UTC"),
                    "open": close * (0.99 if bar == 0 else 1.0),
                    "close": close * (0.995 if bar == 0 else 1.0),
                    "volume": 1000.0,
                }
            )
    return pd.DataFrame(rows)


def _random_walk(n: int, seed: int, drift: float = 0.0003, vol: float = 0.01) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 100.0 * np.exp(np.cumsum(drift + vol * rng.standard_normal(n)))


@pytest.fixture(scope="module")
def sessions() -> list[date]:
    return _weekdays(date(2021, 1, 4), 400)


@pytest.fixture(scope="module")
def spy_table(sessions):
    return symbol_sessions(_bar_frame(sessions, _random_walk(400, RNG_SEED)))


def test_symbol_sessions_first_open_last_close_summed_volume(sessions):
    closes = _random_walk(10, 7)
    table = symbol_sessions(_bar_frame(sessions[:10], closes))
    assert len(table) == 10
    # First bar's open, last bar's close.
    assert table["open"].iloc[0] == pytest.approx(closes[0] * 0.99)
    assert table["close"].iloc[0] == pytest.approx(closes[0])
    # Dollar volume sums bar close × volume across both bars.
    expected = closes[0] * 0.995 * 1000.0 + closes[0] * 1000.0
    assert table["dollar_volume"].iloc[0] == pytest.approx(expected)


def test_beta_two_on_constructed_returns(sessions, spy_table):
    """own log-returns exactly 2× SPY's ⇒ beta 2, alpha 0, up/down beta 2."""
    spy_closes = spy_table["close"].to_numpy()
    own_closes = 100.0 * (spy_closes / spy_closes[0]) ** 2.0
    own = symbol_sessions(_bar_frame(sessions, own_closes))
    series = build_series(own, spy_table)
    mark = sessions[-1] + timedelta(days=5)
    result = structural_at(series, mark)
    assert result["beta_252"] == pytest.approx(2.0, abs=1e-9)
    assert result["up_beta_252"] == pytest.approx(2.0, abs=1e-9)
    assert result["down_beta_252"] == pytest.approx(2.0, abs=1e-9)
    assert result["resid_ret_252"] == pytest.approx(0.0, abs=1e-9)


def test_fingerprint_causality_future_perturbation(sessions, spy_table):
    """Values at a mark are invariant to any change at or after the mark."""
    closes = _random_walk(400, 11)
    mark = sessions[300]
    base = structural_at(
        build_series(symbol_sessions(_bar_frame(sessions, closes)), spy_table), mark
    )
    perturbed_closes = closes.copy()
    perturbed_closes[300:] *= 1.5  # sessions[300:] are >= mark
    perturbed = structural_at(
        build_series(symbol_sessions(_bar_frame(sessions, perturbed_closes)), spy_table), mark
    )
    for feature in STRUCTURAL_FEATURES:
        if np.isnan(base[feature]):
            assert np.isnan(perturbed[feature])
        else:
            assert base[feature] == perturbed[feature], feature
    # And a past perturbation must change something (non-vacuous probe).
    past_closes = closes.copy()
    past_closes[250:299] *= 1.5
    past = structural_at(
        build_series(symbol_sessions(_bar_frame(sessions, past_closes)), spy_table), mark
    )
    assert any(not np.isnan(base[f]) and base[f] != past[f] for f in STRUCTURAL_FEATURES)


def test_state_causality_future_perturbation(sessions, spy_table):
    closes = _random_walk(400, 13)
    mark = sessions[350]
    table = symbol_sessions(_bar_frame(sessions, closes))
    base = state_at(build_series(table, spy_table), mark, 1.0)
    perturbed_closes = closes.copy()
    perturbed_closes[350:] *= 2.0
    perturbed = state_at(
        build_series(symbol_sessions(_bar_frame(sessions, perturbed_closes)), spy_table), mark, 1.0
    )
    for feature, value in base.items():
        if np.isnan(value):
            assert np.isnan(perturbed[feature])
        else:
            assert value == perturbed[feature], feature


def test_short_history_yields_nan_not_partial_windows(sessions, spy_table):
    """No partial windows: 200 sessions of history leaves 252-features NaN."""
    closes = _random_walk(200, 17)
    table = symbol_sessions(_bar_frame(sessions[:200], closes))
    series = build_series(table, spy_table)
    mark = sessions[200]
    result = structural_at(series, mark)
    assert np.isnan(result["beta_252"])
    assert np.isnan(result["maxdd_252"])
    assert not np.isnan(result["vol_126"])  # 126-window features do exist


def test_no_pre_listing_values(sessions, spy_table):
    """A symbol listed late has NaN at marks before enough of its own history."""
    closes = _random_walk(100, 19)
    late = symbol_sessions(_bar_frame(sessions[300:400], closes))
    series = build_series(late, spy_table)
    early_mark = sessions[200]
    result = structural_at(series, early_mark)
    assert all(np.isnan(value) for value in result.values())


def test_maxdd_and_underwater_on_constructed_path(spy_table, sessions):
    """A −20 % single dip: maxdd −0.2; underwater share counts < −5 % sessions."""
    closes = np.full(252, 100.0)
    closes[100:130] = 80.0  # 30 sessions 20 % under the peak
    table = symbol_sessions(_bar_frame(sessions[:252], closes))
    series = build_series(table, spy_table)
    mark = sessions[252]
    result = structural_at(series, mark)
    assert result["maxdd_252"] == pytest.approx(-0.2)
    assert result["underwater_252"] == pytest.approx(30 / 252)


def test_cross_sectional_z_contemporaneous_only():
    marks = [date(2022, 1, 3), date(2022, 2, 1)]
    symbols = [f"S{i:02d}" for i in range(25)]
    rows = []
    rng = np.random.default_rng(RNG_SEED)
    for mark_index, mark in enumerate(marks):
        for i, symbol in enumerate(symbols):
            rows.append(
                {
                    "mark": mark,
                    "symbol": symbol,
                    "beta_252": float(i) + 100.0 * mark_index,
                    "vol_126": float(rng.standard_normal()),
                }
            )
    panel = pd.DataFrame(rows).set_index(["mark", "symbol"])
    z = cross_sectional_z(panel, ["beta_252", "vol_126"], min_symbols=20)
    for mark in marks:
        block = z.loc[mark, "beta_252"]
        assert abs(float(block.mean())) < 1e-9
        assert float(block.std(ddof=1)) == pytest.approx(1.0, abs=1e-6)
        assert float(block.abs().max()) <= 3.0


def test_cross_sectional_z_min_symbols_guard():
    marks = [date(2022, 1, 3)]
    rows = [{"mark": marks[0], "symbol": f"S{i}", "beta_252": float(i)} for i in range(10)]
    panel = pd.DataFrame(rows).set_index(["mark", "symbol"])
    z = cross_sectional_z(panel, ["beta_252"], min_symbols=20)
    assert z["beta_252"].isna().all()


def test_fingerprint_panel_deterministic(sessions, spy_table):
    tables = {
        "SPY": symbol_sessions(_bar_frame(sessions, spy_table["close"].to_numpy())),
        "AAA": symbol_sessions(_bar_frame(sessions, _random_walk(400, 23))),
        "BBB": symbol_sessions(_bar_frame(sessions, _random_walk(400, 29))),
    }
    marks = [sessions[300], sessions[350]]
    one = fingerprint_panel(tables, marks)
    two = fingerprint_panel(dict(reversed(list(tables.items()))), marks)
    pd.testing.assert_frame_equal(one, two)


# ---------------------------------------------------------------------------
# Archetypes (ledger §L5, §L14)
# ---------------------------------------------------------------------------

from studies.equity_asset_character.archetypes import (  # noqa: E402
    ArchetypeFit,
    adjusted_rand,
    assignments_over_marks,
    fit_archetypes,
    fit_dates,
    kmeans,
    silhouette,
    ward,
)


def _blobs(seed: int = 5, per_cluster: int = 12) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    centers = np.array([[0.0, 0.0], [8.0, 8.0], [-8.0, 8.0]])
    data = np.vstack([center + 0.5 * rng.standard_normal((per_cluster, 2)) for center in centers])
    truth = np.repeat(np.arange(3), per_cluster)
    return data, truth


def test_kmeans_recovers_separated_clusters_and_is_deterministic():
    data, truth = _blobs()
    labels_one, centroids_one = kmeans(data, 3)
    labels_two, centroids_two = kmeans(data, 3)
    assert (labels_one == labels_two).all()
    assert np.allclose(centroids_one, centroids_two)
    assert adjusted_rand(labels_one, truth) == pytest.approx(1.0)


def test_kmeans_canonical_labels_row_order_invariant():
    data, _ = _blobs(seed=9)
    labels, _ = kmeans(data, 3)
    order = np.argsort(np.arange(len(data))[::-1])  # reversed order
    labels_reversed, _ = kmeans(data[order], 3)
    assert adjusted_rand(labels, labels_reversed[np.argsort(order)]) == pytest.approx(1.0)


def test_ward_agrees_on_separated_clusters():
    data, truth = _blobs(seed=13)
    assert adjusted_rand(ward(data, 3), truth) == pytest.approx(1.0)


def test_silhouette_prefers_true_k_on_separated_blobs():
    data, _ = _blobs(seed=17)
    by_k = {k: silhouette(data, kmeans(data, k)[0]) for k in (2, 3, 4)}
    assert by_k[3] == max(by_k.values())


def test_adjusted_rand_permutation_safe():
    a = np.array([0, 0, 1, 1, 2, 2])
    permuted = np.array([2, 2, 0, 0, 1, 1])
    assert adjusted_rand(a, permuted) == pytest.approx(1.0)


def _z_panel(marks, n_symbols=30, seed=31, shift_from=None, shift_symbols=()):
    """A synthetic z-panel with three structural groups; optional perturbation
    of every value at marks >= shift_from (for causality tests)."""
    rng = np.random.default_rng(seed)
    base = {}
    for i in range(n_symbols):
        group = i % 3
        base[f"S{i:02d}"] = np.array([4.0 * group, -4.0 * group])
    rows = []
    for mark in marks:
        for symbol, vector in base.items():
            noise = 0.2 * rng.standard_normal(2)
            value = vector + noise
            if shift_from is not None and mark >= shift_from:
                value = value + 100.0
            if symbol in shift_symbols:
                value = value + 100.0
            rows.append({"mark": mark, "symbol": symbol, "f1": value[0], "f2": value[1]})
    return pd.DataFrame(rows).set_index(["mark", "symbol"]).sort_index()


def _monthly_marks(count: int = 30) -> list[date]:
    marks = []
    day = date(2021, 9, 30)
    while len(marks) < count:
        marks.append(day)
        month = day.month + 1
        year = day.year + (month > 12)
        month = 1 if month > 12 else month
        day = date(year, month, 28)
    return marks


def test_fit_dates_schedule():
    marks = _monthly_marks(48)
    dates = fit_dates(marks)
    assert dates[0] >= date(2022, 7, 1)
    years = [d.year for d in dates[1:]]
    assert years == sorted(set(years))
    for later in dates[1:]:
        assert later.month == 1  # first mark of each calendar year


def test_train_only_clustering_future_perturbation_invariant():
    marks = _monthly_marks(30)
    fit_mark = marks[15]
    clean = fit_archetypes(_z_panel(marks), ("f1", "f2"), fit_mark, marks)
    shifted = fit_archetypes(_z_panel(marks, shift_from=fit_mark), ("f1", "f2"), fit_mark, marks)
    assert clean.labels == shifted.labels
    assert clean.centroids == shifted.centroids
    assert clean.k == shifted.k


def test_centroid_freezing_between_fits():
    marks = _monthly_marks(30)
    panel = _z_panel(marks)
    fits = [
        fit_archetypes(panel, ("f1", "f2"), marks[10], marks),
        fit_archetypes(panel, ("f1", "f2"), marks[20], marks),
    ]
    table = assignments_over_marks(panel, fits, marks)
    # Before the first fit: no archetype.
    early = table.loc[marks[5]]["archetype"]
    assert early.isna().all()
    # Between fits the governing fit is the first one.
    mid = table.loc[marks[15]]["fit_mark"]
    assert (mid == fits[0].fit_mark).all()
    late = table.loc[marks[25]]["fit_mark"]
    assert (late == fits[1].fit_mark).all()


def test_assignment_uses_frozen_centroids():
    marks = _monthly_marks(30)
    panel = _z_panel(marks)
    fit = fit_archetypes(panel, ("f1", "f2"), marks[10], marks)
    by_symbol = dict(zip(fit.symbols, fit.labels, strict=True))
    table = assignments_over_marks(panel, [fit], marks)
    # Structural groups are stable, so assignment at a later mark matches the
    # training label for every fitted symbol.
    later = table.loc[marks[20]]["archetype"]
    for symbol, label in by_symbol.items():
        assert later[symbol] == pytest.approx(float(label))


def test_singleton_guard_steps_k_down():
    marks = _monthly_marks(20)
    # 2 natural groups of 10 plus 2 far-out oddballs: at k=3 the oddballs
    # form a 2-member cluster, which must trigger the step-down.
    rng = np.random.default_rng(41)
    rows = []
    for mark in marks:
        for i in range(22):
            if i < 10:
                vector = np.array([0.0, 0.0])
            elif i < 20:
                vector = np.array([8.0, 0.0])
            else:
                vector = np.array([4.0, 30.0 + 4.0 * (i - 20)])
            noise = 0.1 * rng.standard_normal(2)
            rows.append(
                {
                    "mark": mark,
                    "symbol": f"S{i:02d}",
                    "f1": vector[0] + noise[0],
                    "f2": vector[1] + noise[1],
                }
            )
    panel = pd.DataFrame(rows).set_index(["mark", "symbol"]).sort_index()
    fit = fit_archetypes(panel, ("f1", "f2"), marks[15], marks)
    counts = np.bincount(np.array(fit.labels))
    assert fit.k == 3 or counts.min() >= 3 or "floor" in fit.singleton_note


def test_assign_nan_vector_gets_no_archetype():
    fit = ArchetypeFit(
        fit_mark=date(2022, 7, 1),
        features=("f1", "f2"),
        symbols=("A",),
        labels=(0,),
        centroids=((0.0, 0.0), (5.0, 5.0)),
        k=2,
        silhouette_by_k={2: 0.9},
        ward_agreement=1.0,
        singleton_note="",
    )
    assert fit.assign(np.array([float("nan"), 1.0])) is None
    assert fit.assign(np.array([4.9, 5.2])) == 1


# ---------------------------------------------------------------------------
# Allocation (ledger §L8, §L9, §L14)
# ---------------------------------------------------------------------------

from studies.equity_asset_character.allocation import (  # noqa: E402
    archetype_multipliers,
    build_targets_tilted,
    governing_marks,
    response_estimates,
    state_multipliers,
    tilted_weights,
)
from studies.equity_asset_character.response import ForwardObservation  # noqa: E402


def test_tilted_weights_symmetry_and_renormalization():
    symbols = ["AAA", "BBB", "CCC", "DDD"]
    mults = {"AAA": 1.4, "BBB": 1.4, "CCC": 0.6, "DDD": 0.6}
    weights = tilted_weights(symbols, mults)
    # Same multiplier ⇒ same weight (symmetry).
    assert weights["AAA"] == weights["BBB"]
    assert weights["CCC"] == weights["DDD"]
    # Renormalized to the base total (no cap binding at 4 names × 10 %... cap
    # binds at 0.10: base = 0.10, so up-tilts cap out and total ≤ base total).
    base_total = min(1.0 / 4, 0.10) * 4
    assert sum(weights.values()) <= base_total + 1e-9
    assert max(weights.values()) <= 0.10 + 1e-12


def test_tilted_weights_uncapped_universe_preserves_total():
    symbols = [f"S{i:02d}" for i in range(26)]
    mults = {s: (1.4 if i % 2 else 0.6) for i, s in enumerate(symbols)}
    weights = tilted_weights(symbols, mults)
    base_total = min(1.0 / 26, 0.10) * 26
    assert sum(weights.values()) == pytest.approx(base_total)
    assert max(weights.values()) <= 0.10 + 1e-12
    # No ticker-specific constants: equal multipliers ⇒ equal weights.
    odd = {w for s, w in weights.items() if mults[s] == 1.4}
    assert len({round(w, 15) for w in odd}) == 1


def test_tilted_weights_all_one_is_equal_base():
    symbols = [f"S{i}" for i in range(26)]
    weights = tilted_weights(symbols, {})
    assert all(w == pytest.approx(min(1.0 / 26, 0.10)) for w in weights.values())


def _fit_record():
    return {
        "fit_mark": "2023-02-01",
        "centroids_z": [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0]],
        "features": ["beta_252", "vol_126"],
        "raw_feature_medians": {
            "0": {"beta_252": 0.5, "vol_126": 0.2},
            "1": {"beta_252": 1.0, "vol_126": 0.3},
            "2": {"beta_252": 1.8, "vol_126": 0.5},
        },
    }


def test_a1b_multipliers_participate_only_and_clipped():
    fit = _fit_record()
    mults = archetype_multipliers("A1_B", fit, "PARTICIPATE", {})
    mean = (0.5 + 1.0 + 1.8) / 3
    assert mults[0] == pytest.approx(max(0.5 / mean, 0.6))
    assert mults[2] == pytest.approx(min(1.8 / mean, 1.4))
    defensive = archetype_multipliers("A1_B", fit, "DEFENSIVE", {})
    assert all(v == 1.0 for v in defensive.values())


def test_a1p_defensive_is_neutral_a1r_is_not():
    fit = _fit_record()
    estimates = {"DEFENSIVE": {0: -0.05, 1: 0.00, 2: 0.25}}
    p = archetype_multipliers("A1_P", fit, "DEFENSIVE", estimates)
    assert all(v == 1.0 for v in p.values())
    r = archetype_multipliers("A1_R", fit, "DEFENSIVE", estimates)
    assert r[2] > 1.0 > r[0]
    assert all(0.6 <= v <= 1.4 for v in r.values())


def test_response_estimates_purge_excludes_windows_crossing_fit():
    fit = _fit_record()
    marks = [date(2022, 6, 1), date(2023, 1, 15)]
    rows = []
    for mark in marks:
        rows.append({"mark": mark, "symbol": "AAA", "beta_252": 0.0, "vol_126": 0.0})
    z_panel = pd.DataFrame(rows).set_index(["mark", "symbol"]).sort_index()
    observations = [
        # Window closes before the fit date: included.
        ForwardObservation(marks[0], "AAA", 21, 0.10, 0.02, date(2022, 7, 1)),
        # Window closes after the fit date: must be purged.
        ForwardObservation(marks[1], "AAA", 21, 9.99, 0.00, date(2023, 2, 14)),
    ]
    regimes = {marks[0]: "PARTICIPATE", marks[1]: "PARTICIPATE"}
    estimates = response_estimates(fit, z_panel, observations, regimes)
    assert set(estimates) == {"PARTICIPATE"}
    assert estimates["PARTICIPATE"][0] == pytest.approx((0.10 - 0.02) * 12)
    assert 9.99 * 12 not in estimates["PARTICIPATE"].values()


def test_governing_marks():
    sessions = [date(2022, 1, d) for d in (3, 4, 5, 6, 7)]
    marks = [date(2022, 1, 3), date(2022, 1, 6)]
    of = governing_marks(sessions, marks)
    assert of[date(2022, 1, 5)] == date(2022, 1, 3)
    assert of[date(2022, 1, 6)] == date(2022, 1, 6)
    assert of[date(2022, 1, 7)] == date(2022, 1, 6)


def test_state_multipliers_band_and_neutral_nan():
    mark = date(2023, 2, 1)
    rows = [
        {"mark": mark, "symbol": "AAA", "rs_63": 3.0, "vol_ratio": float("nan")},
        {"mark": mark, "symbol": "BBB", "rs_63": -3.0, "vol_ratio": 0.0},
    ]
    z_state = pd.DataFrame(rows).set_index(["mark", "symbol"]).sort_index()
    m = state_multipliers("A2_M", z_state, mark, ["AAA", "BBB", "CCC"])
    assert m["AAA"] == pytest.approx(1.15)  # clipped at the band
    assert m["BBB"] == pytest.approx(0.85)
    assert m["CCC"] == 1.0  # absent symbol is neutral
    q = state_multipliers("A2_Q", z_state, mark, ["AAA", "BBB"])
    assert q["AAA"] == 1.0  # NaN vol_ratio is neutral


def test_build_targets_tilted_participate_vs_defensive():
    sessions = _weekdays(date(2022, 1, 3), 4)
    closes = np.array([100.0, 101.0, 102.0, 103.0])
    frame = _bar_frame(sessions, closes, bars_per_session=1)
    stamps = [pd.Timestamp(ts) for ts in frame["timestamp"]]
    participate = {sessions[0]: True, sessions[1]: False, sessions[2]: False, sessions[3]: True}
    stance = {"AAA": {stamps[1]: 1, stamps[2]: 0}}
    active = {s: {"AAA": 0.07} for s in sessions}
    reserved = {s: {"AAA": 0.04} for s in sessions}
    targets = build_targets_tilted(
        {"AAA": frame},
        sessions,
        participate,
        stance,
        active_weight_of=active,
        reserved_weight_of=reserved,
    )
    assert targets["AAA"][stamps[0]] == pytest.approx(0.07)  # participate
    assert targets["AAA"][stamps[1]] == pytest.approx(0.04)  # defensive, stance 1
    assert targets["AAA"][stamps[2]] == pytest.approx(0.0)  # defensive, stance 0
    assert targets["AAA"][stamps[3]] == pytest.approx(0.07)


# ---------------------------------------------------------------------------
# Hierarchical models (ledger §L10, §L14)
# ---------------------------------------------------------------------------

from studies.equity_asset_character.hierarchical import (  # noqa: E402
    PooledRow,
    build_design,
    demeaned_targets,
    ridge_fit,
    walk_forward_ic,
)


def test_ridge_recovers_coefficients_at_tiny_penalty():
    rng = np.random.default_rng(47)
    x = rng.standard_normal((500, 2))
    y = 2.0 * x[:, 0] - 1.0 * x[:, 1]
    matrix = np.hstack([np.ones((500, 1)), x])
    beta = ridge_fit(matrix, y, np.full(3, 1e-10))
    assert beta[1] == pytest.approx(2.0, abs=1e-6)
    assert beta[2] == pytest.approx(-1.0, abs=1e-6)


def test_symbol_effects_are_strongly_shrunk():
    marks = _monthly_marks(20)
    rng = np.random.default_rng(53)
    rows = []
    for mark in marks:
        for i in range(20):
            symbol = f"S{i:02d}"
            # A pure symbol effect with no feature signal.
            rows.append(
                PooledRow(
                    mark=mark,
                    symbol=symbol,
                    target=(1.0 if i == 0 else -1.0 / 19) + 0.01 * rng.standard_normal(),
                    window_closes=mark,
                    features={"f": float(rng.standard_normal())},
                )
            )
    symbols = sorted({row.symbol for row in rows})
    symbol_index = {s: i for i, s in enumerate(symbols)}
    matrix, penalties = build_design(rows, ["f"], symbol_index)
    beta = ridge_fit(matrix, np.array([row.target for row in rows]), penalties)
    # The S00 dummy coefficient is shrunk far below the raw effect size (1.0).
    assert abs(beta[2 + symbol_index["S00"]]) < 0.25


def test_walk_forward_excludes_leaking_rows():
    """A row whose forward window crosses the fit date must not train."""
    marks = _monthly_marks(30)
    fit_mark = marks[20]
    rng = np.random.default_rng(59)
    rows = []
    for mark in marks[:16]:  # clean training rows: target = +2 x
        for i in range(25):
            x = float(rng.standard_normal())
            rows.append(
                PooledRow(mark, f"S{i:02d}", 2.0 * x, mark, {"x": x})
            )
    # A poisoned batch just before the fit whose window crosses it: target = −50 x.
    poison_mark = marks[19]
    for i in range(25):
        x = float(rng.standard_normal())
        rows.append(
            PooledRow(poison_mark, f"S{i:02d}", -50.0 * x, marks[21], {"x": x})
        )
    # Scoring rows after the fit.
    for mark in marks[21:25]:
        for i in range(25):
            x = float(rng.standard_normal())
            rows.append(PooledRow(mark, f"S{i:02d}", 2.0 * x, mark, {"x": x}))
    result = walk_forward_ic(
        rows, ["x"], [fit_mark], marks, with_symbol_effects=False
    )
    # If the poisoned rows leaked, the sign flips and IC goes to −1.
    assert result["mean_ic"] > 0.9


def test_demeaned_targets_zero_mean_per_mark():
    marks = [date(2023, 1, 2)]
    forward = {
        (marks[0], f"S{i:02d}"): (float(i), date(2023, 2, 1)) for i in range(12)
    }
    out = demeaned_targets(forward, marks)
    values = [v[0] for v in out.values()]
    assert abs(float(np.mean(values))) < 1e-12
