"""Normalisation, symbol mapping, duplicate and missing-data semantics.

These run against the real acquired dataset where the fact under test is a
property of that dataset (coverage, settlement grid), and against constructed
inputs where the fact under test is a rule (schema drift must raise, a
conflicting duplicate must raise).
"""

from __future__ import annotations

import zipfile

import numpy as np
import pandas as pd
import pytest
from studies.crypto_funding_basis_pilot import normalize
from studies.crypto_funding_basis_pilot.acquire import SYMBOL_MAP
from studies.crypto_funding_basis_pilot.normalize import (
    NORMALIZED_DIR,
    NormalizationError,
    _read_csv_member,
)

REAL_DATA = (NORMALIZED_DIR / "BTCUSDT_funding.parquet").exists()
requires_data = pytest.mark.skipif(not REAL_DATA, reason="normalised dataset not built yet")


def _zip_with(tmp_path, name, text):
    path = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}.csv", text)
    return path


# --------------------------------------------------------------------------
# Schema handling


def test_headered_and_headerless_files_both_parse(tmp_path):
    body = "1609459200002,8,0.00022753\n1609488000006,8,0.00026336\n"
    headerless = _zip_with(tmp_path, "a", body)
    headered = _zip_with(
        tmp_path, "b", "calc_time,funding_interval_hours,last_funding_rate\n" + body
    )
    one = _read_csv_member(headerless, normalize.FUNDING_COLUMNS)
    two = _read_csv_member(headered, normalize.FUNDING_COLUMNS)
    pd.testing.assert_frame_equal(one, two)


def test_schema_drift_raises_rather_than_being_coerced(tmp_path):
    path = _zip_with(tmp_path, "c", "calc_time,interval,rate\n1609459200002,8,0.0002\n")
    with pytest.raises(NormalizationError, match="schema drift"):
        _read_csv_member(path, normalize.FUNDING_COLUMNS)


def test_multiple_csv_members_raise(tmp_path):
    path = tmp_path / "d.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("one.csv", "1,8,0.1\n")
        archive.writestr("two.csv", "2,8,0.2\n")
    with pytest.raises(NormalizationError, match="exactly one CSV"):
        _read_csv_member(path, normalize.FUNDING_COLUMNS)


# --------------------------------------------------------------------------
# Symbol mapping


def test_symbol_mapping_is_explicit_and_perpetual_usd_margined():
    assert SYMBOL_MAP == {"BTCUSDT": "BTC/USD", "ETHUSDT": "ETH/USD"}
    from studies.crypto_funding_basis_pilot.acquire import BASE_URL, targets

    assert "/futures/um/" in BASE_URL, "USD-margined family only"
    assert "/cm/" not in BASE_URL, "coin-margined must never be mixed in"
    for target in targets():
        assert "quarterly" not in target.url.lower()
        assert target.symbol in SYMBOL_MAP


# --------------------------------------------------------------------------
# Real-dataset properties


@requires_data
@pytest.mark.parametrize("perp", ["BTCUSDT", "ETHUSDT"])
def test_funding_settlements_are_a_perfect_8h_grid(perp):
    frame = pd.read_parquet(NORMALIZED_DIR / f"{perp}_funding.parquet")
    gaps = frame["source_timestamp"].diff().dropna()
    # Settlement instants carry a few ms of venue jitter, so the grid is 8h
    # plus or minus that jitter - never a missing or an extra settlement.
    drift = (gaps - pd.Timedelta("8h")).abs()
    assert drift.max() < pd.Timedelta("1s"), (
        f"largest deviation from the 8h grid is {drift.max()}: a settlement is "
        "missing or duplicated"
    )
    assert (frame["funding_interval_hours"] == 8).all()
    assert not frame["source_timestamp"].duplicated().any()
    assert frame["source_timestamp"].is_monotonic_increasing


@requires_data
@pytest.mark.parametrize("perp", ["BTCUSDT", "ETHUSDT"])
def test_funding_knowable_at_never_precedes_settlement(perp):
    frame = pd.read_parquet(NORMALIZED_DIR / f"{perp}_funding.parquet")
    delta = frame["knowable_at"] - frame["source_timestamp"]
    assert (delta >= pd.Timedelta(0)).all()
    assert (delta < pd.Timedelta("1s")).all(), "the ceil must add under one second"


@requires_data
@pytest.mark.parametrize("perp", ["BTCUSDT", "ETHUSDT"])
def test_premium_bars_land_on_the_decision_grid(perp):
    frame = pd.read_parquet(NORMALIZED_DIR / f"{perp}_premium.parquet")
    offsets = frame["bar_open"].dt.floor("15min")
    assert (offsets == frame["bar_open"]).all(), "a premium bar is off the 15m grid"
    span = frame["bar_close"] - frame["bar_open"]
    assert (span == pd.Timedelta("15min") - pd.Timedelta(1, unit="ms")).all()
    knowable = frame["knowable_at"] - frame["bar_close"]
    assert (knowable == pd.Timedelta(1, unit="ms")).all()
    assert not frame["bar_open"].duplicated().any()


@requires_data
@pytest.mark.parametrize("perp", ["BTCUSDT", "ETHUSDT"])
def test_premium_coverage_after_backfill_is_essentially_complete(perp):
    frame = pd.read_parquet(NORMALIZED_DIR / f"{perp}_premium.parquet")
    full = pd.date_range(frame["bar_open"].min(), frame["bar_open"].max(), freq="15min", tz="UTC")
    missing = full.difference(pd.DatetimeIndex(frame["bar_open"]))
    # Eight bars over 6.5 years remain absent from the daily archives too.
    assert len(missing) <= 8, f"{len(missing)} premium bars missing"


@requires_data
def test_the_two_symbols_share_an_identical_settlement_clock():
    btc = pd.read_parquet(NORMALIZED_DIR / "BTCUSDT_funding.parquet")
    eth = pd.read_parquet(NORMALIZED_DIR / "ETHUSDT_funding.parquet")
    pd.testing.assert_series_equal(
        btc["source_timestamp"], eth["source_timestamp"], check_names=False
    )


@requires_data
def test_funding_and_premium_are_not_secretly_the_same_series():
    """A sanity check that the two information families are distinct."""
    funding = pd.read_parquet(NORMALIZED_DIR / "BTCUSDT_funding.parquet")
    premium = pd.read_parquet(NORMALIZED_DIR / "BTCUSDT_premium.parquet")
    assert len(funding) < len(premium) / 10
    assert funding["funding_rate"].std() > 0
    assert premium["premium_close"].std() > 0


@requires_data
def test_regime_signs_match_recorded_history():
    """2021-01 bull funding positive; 2022-06 post-crash basis negative."""
    funding = pd.read_parquet(NORMALIZED_DIR / "BTCUSDT_funding.parquet")
    bull = funding.loc[
        (funding["source_timestamp"] >= pd.Timestamp("2021-01-01", tz="UTC"))
        & (funding["source_timestamp"] < pd.Timestamp("2021-02-01", tz="UTC")),
        "funding_rate",
    ]
    assert bull.mean() > 0.0003, "January 2021 funding should be strongly positive"

    premium = pd.read_parquet(NORMALIZED_DIR / "BTCUSDT_premium.parquet")
    bear = premium.loc[
        (premium["bar_open"] >= pd.Timestamp("2022-06-01", tz="UTC"))
        & (premium["bar_open"] < pd.Timestamp("2022-07-01", tz="UTC")),
        "premium_close",
    ]
    assert bear.mean() < 0, "June 2022 basis should show backwardation"


# --------------------------------------------------------------------------
# Missing-data semantics


def test_missing_is_never_conflated_with_zero():
    """A zero funding rate is real data; absence is NaN. They must differ."""
    from studies.crypto_funding_basis_pilot.derivative_features import (
        join_derivative_features,
    )

    source = pd.date_range("2024-01-01", periods=60, freq="8h", tz="UTC")
    funding = pd.DataFrame(
        {
            "source_timestamp": source,
            "publication_timestamp": source,
            "knowable_at": source.ceil("1s"),
            "funding_rate": 0.0,
            "funding_interval_hours": 8,
        }
    )
    bar_open = pd.date_range("2024-01-01", periods=2000, freq="15min", tz="UTC")
    premium = pd.DataFrame(
        {
            "bar_open": bar_open,
            "bar_close": bar_open + pd.Timedelta("15min") - pd.Timedelta(1, "ms"),
            "knowable_at": bar_open + pd.Timedelta("15min"),
            "premium_close": 0.0,
            "premium_open": 0.0,
            "premium_high": 0.0,
            "premium_low": 0.0,
            "sample_count": 180,
        }
    )
    timestamps = pd.Series(pd.date_range("2024-01-05", periods=200, freq="15min", tz="UTC"))
    n = len(timestamps)
    frame, _ = join_derivative_features(
        timestamps,
        funding,
        premium,
        return_2688=pd.Series(np.full(n, 0.01)),
        realized_volatility_96=pd.Series(np.full(n, 0.004)),
    )
    # Genuine zeros survive as zeros, not as NaN.
    assert (frame["funding_current"] == 0.0).all()
    assert (frame["premium_close"] == 0.0).all()
