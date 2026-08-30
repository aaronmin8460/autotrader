"""The eight predeclared funding/basis features, joined causally.

Nothing in this module may read a value the engine could not have held at the
instant it decides. Three rules enforce that, and the tests assert all three:

1. **Decision instant.** A feature row stamped `timestamp` T describes the bar
   that *opens* at T; that bar closes at T + 15m, and T + 15m is when the
   engine decides on it (`autotrader.ml.labels` stamps its own knowability the
   same way: exit bar close). So `decision_ts = T + BAR_INTERVAL`.
2. **Backward-only join.** Both derivative series are joined with
   `merge_asof(direction="backward")` on `knowable_at <= decision_ts`. There is
   no nearest-future join, no interpolation, no reindex-and-fill.
3. **Past-only derived statistics.** Every rolling statistic is computed on the
   derivative series' own clock *before* the join, over trailing windows that
   end at the row being described. A window never extends forward, and the join
   then carries only rows already knowable.

Staleness is bounded, declared in advance, and never papered over: a funding
value older than one settlement interval, or a premium bar older than the
declared tolerance, is **unavailable** (NaN), not zero. Zero is a real funding
rate and is never used to mean "missing".
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from autotrader.runtime.schedule import BAR_INTERVAL

#: The eight features, in contract order. No additions without a new
#: predeclaration; `pilot-designs.md` fixes this list.
DERIVATIVE_FEATURES: tuple[str, ...] = (
    "funding_current",
    "funding_z_30",
    "funding_delta",
    "premium_close",
    "premium_mean_24h",
    "premium_pct_90d",
    "funding_trend_interaction",
    "premium_vol_interaction",
)

#: Trailing settlements in the funding z-score (predeclared: 30).
FUNDING_Z_WINDOW = 30

#: Trailing 15m bars in the 24h premium mean (96) and the 90d percentile.
PREMIUM_MEAN_BARS = 96
PREMIUM_PCT_BARS = 90 * 96

#: A rolling statistic must have observed this fraction of its window.
MIN_WINDOW_FRACTION = 0.8

#: **Declared staleness bounds.** Funding settles every 8h, and a decision
#: landing exactly on a settlement boundary legitimately reads the *previous*
#: settlement (the 1s knowability ceil puts the new one just out of reach), so
#: 8h is the largest staleness a complete series can produce. Anything beyond
#: it means a settlement is missing from the archive.
MAX_FUNDING_STALENESS = pd.Timedelta("8h")

#: Premium bars land natively on the decision grid, so staleness is normally
#: exactly zero. One hour tolerates an isolated archive hole without discarding
#: the surrounding day; beyond that the basis reading is not describing the
#: present market and is withdrawn.
MAX_PREMIUM_STALENESS = pd.Timedelta("1h")

#: `merge_asof` refuses to join keys of differing datetime resolution, and the
#: OHLCV grid carries microseconds while the derivative archives carry
#: milliseconds. Both sides are cast to microseconds before the join: ms -> us
#: is exact, so no timestamp - including the 1 ms premium knowability offset -
#: is altered by the cast.
JOIN_RESOLUTION = "datetime64[us, UTC]"


@dataclass(frozen=True)
class JoinAudit:
    """What the causal join actually did, for the audit artifact."""

    rows: int
    funding_available: int
    premium_available: int
    funding_staleness_min: float
    funding_staleness_max: float
    premium_staleness_min: float
    premium_staleness_max: float
    funding_stale_dropped: int
    premium_stale_dropped: int
    negative_staleness: int

    def as_dict(self) -> dict:
        return {
            "rows": self.rows,
            "funding_available": self.funding_available,
            "premium_available": self.premium_available,
            "funding_coverage": self.funding_available / self.rows if self.rows else 0.0,
            "premium_coverage": self.premium_available / self.rows if self.rows else 0.0,
            "funding_staleness_seconds": {
                "min": self.funding_staleness_min,
                "max": self.funding_staleness_max,
            },
            "premium_staleness_seconds": {
                "min": self.premium_staleness_min,
                "max": self.premium_staleness_max,
            },
            "funding_dropped_beyond_staleness": self.funding_stale_dropped,
            "premium_dropped_beyond_staleness": self.premium_stale_dropped,
            "negative_staleness_rows": self.negative_staleness,
        }


def _min_periods(window: int) -> int:
    return max(2, int(np.ceil(window * MIN_WINDOW_FRACTION)))


def funding_series_features(funding: pd.DataFrame) -> pd.DataFrame:
    """Per-settlement statistics, computed on the settlement clock, past-only.

    The trailing window ends at the settlement being described, which is itself
    already knowable at that row's `knowable_at`; nothing later enters.
    """
    frame = funding.sort_values("source_timestamp", kind="stable").reset_index(drop=True)
    rate = frame["funding_rate"].astype("float64")
    rolling = rate.rolling(FUNDING_Z_WINDOW, min_periods=_min_periods(FUNDING_Z_WINDOW))
    mean = rolling.mean()
    deviation = rolling.std(ddof=0)
    return pd.DataFrame(
        {
            "knowable_at": frame["knowable_at"],
            "source_timestamp": frame["source_timestamp"],
            "funding_current": rate,
            # A flat window has no dispersion; a z-score of a constant series is
            # undefined, not zero, so the row is withdrawn rather than invented.
            "funding_z_30": (rate - mean) / deviation.where(deviation > 0.0),
            "funding_delta": rate.diff(),
        }
    )


def premium_series_features(premium: pd.DataFrame) -> pd.DataFrame:
    """Per-bar basis statistics on the premium clock, past-only."""
    frame = premium.sort_values("bar_open", kind="stable").reset_index(drop=True)
    close = frame["premium_close"].astype("float64")
    mean_24h = close.rolling(PREMIUM_MEAN_BARS, min_periods=_min_periods(PREMIUM_MEAN_BARS)).mean()
    # `Rolling.rank` ranks the window's final observation among its own trailing
    # window - the percentile of the current basis within the last 90 days.
    percentile = close.rolling(PREMIUM_PCT_BARS, min_periods=_min_periods(PREMIUM_PCT_BARS)).rank(
        pct=True
    )
    return pd.DataFrame(
        {
            "knowable_at": frame["knowable_at"],
            "bar_open": frame["bar_open"],
            "premium_close": close,
            "premium_mean_24h": mean_24h,
            "premium_pct_90d": percentile,
        }
    )


def join_derivative_features(
    timestamps: pd.Series,
    funding: pd.DataFrame,
    premium: pd.DataFrame,
    *,
    return_2688: pd.Series,
    realized_volatility_96: pd.Series,
) -> tuple[pd.DataFrame, JoinAudit]:
    """The eight features on the decision grid, plus the join's own audit.

    `timestamps` are feature-bar *open* instants on the 15m grid; each row's
    decision happens at `timestamp + BAR_INTERVAL`, and only derivative data
    knowable at or before that instant may reach it.
    """
    decision_ts = (pd.to_datetime(timestamps, utc=True) + BAR_INTERVAL).astype(JOIN_RESOLUTION)
    left = pd.DataFrame({"decision_ts": decision_ts}).reset_index(drop=True)
    order = left["decision_ts"].is_monotonic_increasing
    if not order:
        raise ValueError("decision timestamps must be monotonic for a backward join")

    funding_features = funding_series_features(funding).sort_values("knowable_at", kind="stable")
    premium_features = premium_series_features(premium).sort_values("knowable_at", kind="stable")
    for side in (funding_features, premium_features):
        side["knowable_at"] = side["knowable_at"].astype(JOIN_RESOLUTION)

    joined_funding = pd.merge_asof(
        left,
        funding_features,
        left_on="decision_ts",
        right_on="knowable_at",
        direction="backward",
        allow_exact_matches=True,
    )
    joined_premium = pd.merge_asof(
        left,
        premium_features,
        left_on="decision_ts",
        right_on="knowable_at",
        direction="backward",
        allow_exact_matches=True,
    )

    funding_staleness = joined_funding["decision_ts"] - joined_funding["knowable_at"]
    premium_staleness = joined_premium["decision_ts"] - joined_premium["knowable_at"]
    negative = int(
        (funding_staleness < pd.Timedelta(0)).sum() + (premium_staleness < pd.Timedelta(0)).sum()
    )
    if negative:
        raise ValueError(f"{negative} rows carry a derivative value from the future")

    funding_fresh = funding_staleness.notna() & (funding_staleness <= MAX_FUNDING_STALENESS)
    premium_fresh = premium_staleness.notna() & (premium_staleness <= MAX_PREMIUM_STALENESS)

    frame = pd.DataFrame(index=left.index)
    for name in ("funding_current", "funding_z_30", "funding_delta"):
        frame[name] = joined_funding[name].where(funding_fresh)
    for name in ("premium_close", "premium_mean_24h", "premium_pct_90d"):
        frame[name] = joined_premium[name].where(premium_fresh)

    trend = np.sign(pd.Series(return_2688).reset_index(drop=True).astype("float64"))
    frame["funding_trend_interaction"] = frame["funding_z_30"] * trend
    volatility = pd.Series(realized_volatility_96).reset_index(drop=True).astype("float64")
    frame["premium_vol_interaction"] = frame["premium_mean_24h"] * volatility

    def _seconds(series: pd.Series, mask: pd.Series) -> tuple[float, float]:
        usable = series.where(mask).dropna()
        if usable.empty:
            return (float("nan"), float("nan"))
        return (
            float(usable.min().total_seconds()),
            float(usable.max().total_seconds()),
        )

    funding_range = _seconds(funding_staleness, funding_fresh)
    premium_range = _seconds(premium_staleness, premium_fresh)
    audit = JoinAudit(
        rows=int(len(frame)),
        funding_available=int(frame["funding_current"].notna().sum()),
        premium_available=int(frame["premium_close"].notna().sum()),
        funding_staleness_min=funding_range[0],
        funding_staleness_max=funding_range[1],
        premium_staleness_min=premium_range[0],
        premium_staleness_max=premium_range[1],
        funding_stale_dropped=int((funding_staleness > MAX_FUNDING_STALENESS).sum()),
        premium_stale_dropped=int((premium_staleness > MAX_PREMIUM_STALENESS).sum()),
        negative_staleness=negative,
    )
    return frame[list(DERIVATIVE_FEATURES)], audit


__all__ = [
    "DERIVATIVE_FEATURES",
    "FUNDING_Z_WINDOW",
    "MAX_FUNDING_STALENESS",
    "MAX_PREMIUM_STALENESS",
    "PREMIUM_MEAN_BARS",
    "PREMIUM_PCT_BARS",
    "JoinAudit",
    "funding_series_features",
    "join_derivative_features",
    "premium_series_features",
]
