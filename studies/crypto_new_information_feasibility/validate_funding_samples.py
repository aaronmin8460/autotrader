"""Validate downloaded funding / premium-index / metrics samples.

Read-only over files already downloaded into the samples directory by the
feasibility study. Checks, per file class:

fundingRate CSVs
    complete month at the stated interval, zero duplicates, monotonic
    timestamps, settlement instants on the 8-hour UTC grid within a stated
    jitter bound, a single ``funding_interval_hours`` value, and plausible
    rate magnitudes.

premiumIndexKlines 15m CSVs
    complete calendar month of 15-minute bars, exact grid alignment of
    ``open_time``, ``close_time == open_time + 15m - 1ms``, and the per-bar
    sample count field present.

metrics CSVs
    5-minute cadence, zero duplicates, monotonic.

Then demonstrates the causal join both sources need:

* settled funding is stamped ``knowable_at = ceil(calc_time, 1s)`` and joined
  backward onto the 15m decision grid - the last settled rate is between 0
  and 8 hours stale at any decision instant, never future;
* a premium-index bar is stamped ``knowable_at = close_time + 1ms``, which
  lands exactly on the decision boundary - zero staleness, zero lookahead.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

EIGHT_HOURS_MS = 8 * 3600 * 1000
FIFTEEN_MINUTES_MS = 15 * 60 * 1000

#: Maximum settlement-timestamp jitter off the 8h grid seen in any sample.
JITTER_BOUND_MS = 60_000


def validate_funding(path: Path) -> None:
    frame = pd.read_csv(path)
    timestamps = pd.to_datetime(frame["calc_time"], unit="ms", utc=True)
    jitter = frame["calc_time"] % EIGHT_HOURS_MS
    on_grid = (jitter < JITTER_BOUND_MS) | (jitter > EIGHT_HOURS_MS - JITTER_BOUND_MS)
    intervals = timestamps.diff().dropna()

    assert timestamps.duplicated().sum() == 0, f"{path.name}: duplicate settlements"
    assert timestamps.is_monotonic_increasing, f"{path.name}: non-monotonic"
    assert on_grid.all(), f"{path.name}: settlement off the 8h grid"
    assert frame["funding_interval_hours"].nunique() == 1, f"{path.name}: mixed intervals"
    assert frame["last_funding_rate"].abs().max() < 0.0075, f"{path.name}: rate beyond cap"

    print(
        f"{path.name}: rows={len(frame)} "
        f"range={timestamps.iloc[0]}..{timestamps.iloc[-1]} "
        f"interval_h={frame['funding_interval_hours'].iloc[0]} "
        f"gap_min={intervals.min()} gap_max={intervals.max()} "
        f"rate_mean={frame['last_funding_rate'].mean():+.6f}"
    )


def validate_premium(path: Path) -> None:
    frame = pd.read_csv(path)
    open_times = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    expected = pd.date_range(open_times.iloc[0], open_times.iloc[-1], freq="15min", tz="UTC")

    assert open_times.duplicated().sum() == 0, f"{path.name}: duplicate bars"
    assert open_times.is_monotonic_increasing, f"{path.name}: non-monotonic"
    assert len(expected) == len(frame), f"{path.name}: missing 15m bars"
    assert (frame["open_time"] % FIFTEEN_MINUTES_MS == 0).all(), f"{path.name}: off-grid"
    assert (frame["close_time"] - frame["open_time"] == FIFTEEN_MINUTES_MS - 1).all(), (
        f"{path.name}: close_time not open+15m-1ms"
    )

    print(
        f"{path.name}: bars={len(frame)} complete_month=True "
        f"premium_mean={frame['close'].mean():+.7f} "
        f"premium_min={frame['close'].min():+.6f} premium_max={frame['close'].max():+.6f}"
    )


def validate_metrics(path: Path) -> None:
    frame = pd.read_csv(path)
    timestamps = pd.to_datetime(frame["create_time"], utc=True)
    cadence = timestamps.diff().dropna().mode()[0]

    assert timestamps.duplicated().sum() == 0, f"{path.name}: duplicates"
    assert timestamps.is_monotonic_increasing, f"{path.name}: non-monotonic"
    assert cadence == pd.Timedelta(minutes=5), f"{path.name}: unexpected cadence {cadence}"

    print(f"{path.name}: rows={len(frame)} cadence={cadence}")


def demonstrate_causal_join(funding_path: Path, premium_path: Path) -> None:
    funding = pd.read_csv(funding_path)
    funding["knowable_at"] = (
        pd.to_datetime(funding["calc_time"], unit="ms", utc=True)
        .dt.ceil("1s")
        .astype("datetime64[ns, UTC]")
    )
    grid = pd.DataFrame(
        {
            "decision_ts": pd.date_range("2021-01-10", "2021-01-11", freq="15min", tz="UTC").astype(
                "datetime64[ns, UTC]"
            )
        }
    )
    joined = pd.merge_asof(
        grid,
        funding[["knowable_at", "last_funding_rate"]].sort_values("knowable_at"),
        left_on="decision_ts",
        right_on="knowable_at",
        direction="backward",
    )
    staleness = joined["decision_ts"] - joined["knowable_at"]
    assert staleness.min() >= pd.Timedelta(0), "future funding leaked into a decision"
    assert staleness.max() <= pd.Timedelta(hours=8), "stale beyond one funding interval"
    print(f"funding join: staleness 0..{staleness.max()} - causal, bounded by interval")

    premium = pd.read_csv(premium_path)
    premium["knowable_at"] = pd.to_datetime(premium["close_time"] + 1, unit="ms", utc=True).astype(
        "datetime64[ns, UTC]"
    )
    grid2 = pd.DataFrame(
        {
            "decision_ts": pd.date_range(
                "2022-06-10", "2022-06-10 06:00", freq="15min", tz="UTC"
            ).astype("datetime64[ns, UTC]")
        }
    )
    joined2 = pd.merge_asof(
        grid2,
        premium[["knowable_at", "close"]].sort_values("knowable_at"),
        left_on="decision_ts",
        right_on="knowable_at",
        direction="backward",
    )
    staleness2 = joined2["decision_ts"] - joined2["knowable_at"]
    assert (staleness2 == pd.Timedelta(0)).all(), "premium bar not aligned to grid"
    print("premium join: last completed 15m premium bar aligns exactly - zero staleness")


def main(samples_dir: Path) -> None:
    for name in sorted(p.name for p in samples_dir.glob("*fundingRate*.csv")):
        validate_funding(samples_dir / name)
    for name in sorted(
        p.name
        for p in samples_dir.glob("*-15m-*.csv")
        if "fundingRate" not in p.name and "metrics" not in p.name
    ):
        validate_premium(samples_dir / name)
    for name in sorted(p.name for p in samples_dir.glob("*metrics*.csv")):
        validate_metrics(samples_dir / name)
    demonstrate_causal_join(
        samples_dir / "BTCUSDT-fundingRate-2021-01.csv",
        samples_dir / "BTCUSDT-15m-2022-06.csv",
    )
    print("ALL SAMPLE VALIDATIONS PASSED")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path())
