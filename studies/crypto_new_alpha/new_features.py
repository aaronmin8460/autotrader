"""The 18 predeclared OI / flow / liquidation-proxy features, joined causally.

Causality rules (identical discipline to the funding-basis pilot, whose
convention `autotrader.ml.labels` shares):

1. **Decision instant.** A feature row stamped `timestamp` T describes the bar
   that *opens* at T; the engine decides on it at `T + 15m` (bar close).
2. **Backward-only join.** Every derivative series is joined with
   `merge_asof(direction="backward")` on `knowable_at <= decision_ts`. No
   nearest-future join, no interpolation, no reindex-and-fill.
3. **Past-only derived statistics.** Every rolling statistic is computed on
   the derivative series' own clock *before* the join, over trailing windows
   ending at the row described.

Staleness bounds are declared in the search ledger and enforced here: an open
interest reading older than 2h, or a flow bar older than 1h, is unavailable
(NaN) - never zero, because zero is a real flow value. Negative staleness
anywhere is a hard error, not a warning.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from autotrader.runtime.schedule import BAR_INTERVAL

#: The 18 features, in contract order (search-ledger.md §3). Fixed.
OI_FEATURES: tuple[str, ...] = (
    "oi_chg_15m",
    "oi_chg_1h",
    "oi_chg_4h",
    "oi_chg_24h",
    "oi_z_30d",
    "oi_accel_4h",
    "oi_vol_ratio_24h",
)
FLOW_FEATURES: tuple[str, ...] = (
    "flow_imb_15m",
    "flow_imb_1h",
    "flow_imb_4h",
    "flow_imb_24h",
    "cvd_z_30d",
    "avg_trade_size_z_30d",
)
LIQPROXY_FEATURES: tuple[str, ...] = (
    "delev_long_4h",
    "delev_short_4h",
    "oi_ret_inter_24h",
)
INTERACTION_FEATURES: tuple[str, ...] = (
    "oi_flow_inter_4h",
    "flow_ret_inter_24h",
)
NEW_FEATURES: tuple[str, ...] = (
    OI_FEATURES + FLOW_FEATURES + LIQPROXY_FEATURES + INTERACTION_FEATURES
)

#: Lookbacks for OI changes, and the tolerance on the reference snapshot.
OI_CHANGE_WINDOWS: tuple[tuple[str, pd.Timedelta], ...] = (
    ("oi_chg_15m", pd.Timedelta("15min")),
    ("oi_chg_1h", pd.Timedelta("1h")),
    ("oi_chg_4h", pd.Timedelta("4h")),
    ("oi_chg_24h", pd.Timedelta("24h")),
)
OI_REFERENCE_TOLERANCE = pd.Timedelta("30min")

#: Time-based z-score window for the OI level, and its minimum coverage.
OI_Z_WINDOW = "30D"
OI_Z_MIN_SNAPSHOTS = 4320  # 50% of a full 30d at 5-min cadence

#: Flow rolling windows in 15m bars, with the prior pilots' 80% min fraction.
FLOW_HOUR_BARS = 4
FLOW_4H_BARS = 16
FLOW_DAY_BARS = 96
FLOW_Z_BARS = 2880  # 30 days
MIN_WINDOW_FRACTION = 0.8

#: Declared staleness bounds (search-ledger.md §2).
MAX_OI_STALENESS = pd.Timedelta("2h")
MAX_FLOW_STALENESS = pd.Timedelta("1h")

#: merge_asof needs equal key resolution; ms -> us casts are exact.
JOIN_RESOLUTION = "datetime64[us, UTC]"


@dataclass(frozen=True)
class JoinAudit:
    """What the causal join actually did, for the audit artifact."""

    rows: int
    oi_available: int
    flow_available: int
    oi_staleness_min: float
    oi_staleness_max: float
    flow_staleness_min: float
    flow_staleness_max: float
    oi_stale_dropped: int
    flow_stale_dropped: int
    negative_staleness: int

    def as_dict(self) -> dict:
        return {
            "rows": self.rows,
            "oi_available": self.oi_available,
            "flow_available": self.flow_available,
            "oi_coverage": self.oi_available / self.rows if self.rows else 0.0,
            "flow_coverage": self.flow_available / self.rows if self.rows else 0.0,
            "oi_staleness_seconds": {"min": self.oi_staleness_min, "max": self.oi_staleness_max},
            "flow_staleness_seconds": {
                "min": self.flow_staleness_min,
                "max": self.flow_staleness_max,
            },
            "oi_dropped_beyond_staleness": self.oi_stale_dropped,
            "flow_dropped_beyond_staleness": self.flow_stale_dropped,
            "negative_staleness_rows": self.negative_staleness,
        }


def _min_periods(window: int) -> int:
    return max(2, int(np.ceil(window * MIN_WINDOW_FRACTION)))


def _lagged_reference(
    frame: pd.DataFrame, column: str, lag: pd.Timedelta, tolerance: pd.Timedelta
) -> pd.Series:
    """The series value as of `create_time - lag`, past-only, tolerance-bounded.

    For each snapshot t the reference is the latest snapshot at or before
    t - lag; a reference older than `t - lag - tolerance` is withdrawn (NaN)
    rather than silently accepted, so a hole in the snapshot record can never
    masquerade as a longer-window change.
    """
    keys = pd.DataFrame({"lookup": frame["create_time"] - lag}).reset_index(drop=True)
    reference = frame[["create_time", column]].rename(
        columns={"create_time": "reference_time", column: "reference_value"}
    )
    joined = pd.merge_asof(
        keys,
        reference,
        left_on="lookup",
        right_on="reference_time",
        direction="backward",
        allow_exact_matches=True,
    )
    staleness = joined["lookup"] - joined["reference_time"]
    fresh = staleness.notna() & (staleness <= tolerance)
    return joined["reference_value"].where(fresh).reset_index(drop=True)


def oi_series_features(oi: pd.DataFrame) -> pd.DataFrame:
    """Per-snapshot OI statistics on the snapshot clock, past-only."""
    frame = oi.sort_values("create_time", kind="stable").reset_index(drop=True)
    level = np.log(frame["oi_notional"].astype("float64").where(frame["oi_notional"] > 0.0))
    working = pd.DataFrame({"create_time": frame["create_time"], "log_level": level})

    out = pd.DataFrame(
        {
            "knowable_at": frame["knowable_at"],
            "create_time": frame["create_time"],
            "oi_notional": frame["oi_notional"].astype("float64"),
        }
    )
    for name, lag in OI_CHANGE_WINDOWS:
        reference = _lagged_reference(working, "log_level", lag, OI_REFERENCE_TOLERANCE)
        out[name] = level - reference

    indexed = frame.set_index("create_time")["oi_notional"].astype("float64")
    rolling = indexed.rolling(OI_Z_WINDOW, min_periods=OI_Z_MIN_SNAPSHOTS)
    mean = rolling.mean().reset_index(drop=True)
    deviation = rolling.std(ddof=0).reset_index(drop=True)
    out["oi_z_30d"] = (out["oi_notional"] - mean) / deviation.where(deviation > 0.0)

    accel_frame = pd.DataFrame({"create_time": out["create_time"], "log_level": out["oi_chg_4h"]})
    prior_chg = _lagged_reference(
        accel_frame, "log_level", pd.Timedelta("4h"), OI_REFERENCE_TOLERANCE
    )
    out["oi_accel_4h"] = out["oi_chg_4h"] - prior_chg
    return out


def flow_series_features(flow: pd.DataFrame) -> pd.DataFrame:
    """Per-bar flow statistics on the perp-kline clock, past-only.

    The series is reindexed onto its own continuous 15m grid so positional
    rolling windows cannot silently span a hole; missing bars are NaN and the
    80% minimum-coverage rule decides whether a window still answers.
    """
    frame = flow.sort_values("bar_open", kind="stable").reset_index(drop=True)
    grid = pd.date_range(
        frame["bar_open"].iloc[0], frame["bar_open"].iloc[-1], freq="15min", tz="UTC"
    )
    frame = frame.set_index("bar_open").reindex(grid)
    frame.index.name = "bar_open"

    quote = frame["quote_volume"].astype("float64")
    taker_buy = frame["taker_buy_quote_volume"].astype("float64")
    count = frame["count"].astype("float64")
    signed = 2.0 * taker_buy - quote

    out = pd.DataFrame(index=frame.index)
    out["knowable_at"] = frame["knowable_at"]
    out["flow_imb_15m"] = signed / quote.where(quote > 0.0)

    for name, bars in (
        ("flow_imb_1h", FLOW_HOUR_BARS),
        ("flow_imb_4h", FLOW_4H_BARS),
        ("flow_imb_24h", FLOW_DAY_BARS),
    ):
        signed_sum = signed.rolling(bars, min_periods=_min_periods(bars)).sum()
        quote_sum = quote.rolling(bars, min_periods=_min_periods(bars)).sum()
        out[name] = signed_sum / quote_sum.where(quote_sum > 0.0)

    cvd_24h = signed.rolling(FLOW_DAY_BARS, min_periods=_min_periods(FLOW_DAY_BARS)).sum()
    cvd_rolling = cvd_24h.rolling(FLOW_Z_BARS, min_periods=_min_periods(FLOW_Z_BARS))
    cvd_deviation = cvd_rolling.std(ddof=0)
    out["cvd_z_30d"] = (cvd_24h - cvd_rolling.mean()) / cvd_deviation.where(cvd_deviation > 0.0)

    trade_size = quote / count.where(count > 0.0)
    size_rolling = trade_size.rolling(FLOW_Z_BARS, min_periods=_min_periods(FLOW_Z_BARS))
    size_deviation = size_rolling.std(ddof=0)
    out["avg_trade_size_z_30d"] = (trade_size - size_rolling.mean()) / size_deviation.where(
        size_deviation > 0.0
    )

    out["quote_volume_24h"] = quote.rolling(
        FLOW_DAY_BARS, min_periods=_min_periods(FLOW_DAY_BARS)
    ).sum()

    # Rows with no knowable_at are grid holes: they carry no publishable bar
    # and must not survive into the join (their statistics are NaN anyway).
    out = out.loc[out["knowable_at"].notna()].reset_index()
    return out


def join_new_features(
    timestamps: pd.Series,
    oi: pd.DataFrame,
    flow: pd.DataFrame,
    *,
    return_16: pd.Series,
    return_96: pd.Series,
) -> tuple[pd.DataFrame, JoinAudit]:
    """The 18 features on the decision grid, plus the join's own audit.

    `timestamps` are feature-bar *open* instants on the 15m grid; each row's
    decision happens at `timestamp + BAR_INTERVAL`, and only derivative data
    knowable at or before that instant may reach it.
    """
    decision_ts = (pd.to_datetime(timestamps, utc=True) + BAR_INTERVAL).astype(JOIN_RESOLUTION)
    left = pd.DataFrame({"decision_ts": decision_ts}).reset_index(drop=True)
    if not left["decision_ts"].is_monotonic_increasing:
        raise ValueError("decision timestamps must be monotonic for a backward join")

    oi_features = oi_series_features(oi).sort_values("knowable_at", kind="stable")
    flow_features = flow_series_features(flow).sort_values("knowable_at", kind="stable")
    for side in (oi_features, flow_features):
        side["knowable_at"] = side["knowable_at"].astype(JOIN_RESOLUTION)

    joined_oi = pd.merge_asof(
        left,
        oi_features,
        left_on="decision_ts",
        right_on="knowable_at",
        direction="backward",
        allow_exact_matches=True,
    )
    joined_flow = pd.merge_asof(
        left,
        flow_features,
        left_on="decision_ts",
        right_on="knowable_at",
        direction="backward",
        allow_exact_matches=True,
    )

    oi_staleness = joined_oi["decision_ts"] - joined_oi["knowable_at"]
    flow_staleness = joined_flow["decision_ts"] - joined_flow["knowable_at"]
    negative = int(
        (oi_staleness < pd.Timedelta(0)).sum() + (flow_staleness < pd.Timedelta(0)).sum()
    )
    if negative:
        raise ValueError(f"{negative} rows carry a derivative value from the future")

    oi_fresh = oi_staleness.notna() & (oi_staleness <= MAX_OI_STALENESS)
    flow_fresh = flow_staleness.notna() & (flow_staleness <= MAX_FLOW_STALENESS)

    frame = pd.DataFrame(index=left.index)
    for name in ("oi_chg_15m", "oi_chg_1h", "oi_chg_4h", "oi_chg_24h", "oi_z_30d", "oi_accel_4h"):
        frame[name] = joined_oi[name].where(oi_fresh)
    oi_notional = joined_oi["oi_notional"].where(oi_fresh)
    for name in FLOW_FEATURES:
        frame[name] = joined_flow[name].where(flow_fresh)
    quote_volume_24h = joined_flow["quote_volume_24h"].where(flow_fresh)

    frame["oi_vol_ratio_24h"] = oi_notional / quote_volume_24h.where(quote_volume_24h > 0.0)

    r16 = pd.Series(return_16).reset_index(drop=True).astype("float64")
    r96 = pd.Series(return_96).reset_index(drop=True).astype("float64")
    oi_down = (-frame["oi_chg_4h"]).clip(lower=0.0)
    frame["delev_long_4h"] = oi_down * (-r16).clip(lower=0.0)
    frame["delev_short_4h"] = oi_down * r16.clip(lower=0.0)
    frame["oi_ret_inter_24h"] = frame["oi_chg_24h"] * r96
    frame["oi_flow_inter_4h"] = frame["oi_chg_4h"] * frame["flow_imb_4h"]
    frame["flow_ret_inter_24h"] = frame["flow_imb_24h"] * r96

    def _seconds(series: pd.Series, mask: pd.Series) -> tuple[float, float]:
        usable = series.where(mask).dropna()
        if usable.empty:
            return (float("nan"), float("nan"))
        return (float(usable.min().total_seconds()), float(usable.max().total_seconds()))

    oi_range = _seconds(oi_staleness, oi_fresh)
    flow_range = _seconds(flow_staleness, flow_fresh)
    audit = JoinAudit(
        rows=int(len(frame)),
        oi_available=int(frame["oi_chg_24h"].notna().sum()),
        flow_available=int(frame["flow_imb_24h"].notna().sum()),
        oi_staleness_min=oi_range[0],
        oi_staleness_max=oi_range[1],
        flow_staleness_min=flow_range[0],
        flow_staleness_max=flow_range[1],
        oi_stale_dropped=int((oi_staleness > MAX_OI_STALENESS).sum()),
        flow_stale_dropped=int((flow_staleness > MAX_FLOW_STALENESS).sum()),
        negative_staleness=negative,
    )
    return frame[list(NEW_FEATURES)], audit


__all__ = [
    "FLOW_FEATURES",
    "INTERACTION_FEATURES",
    "LIQPROXY_FEATURES",
    "MAX_FLOW_STALENESS",
    "MAX_OI_STALENESS",
    "NEW_FEATURES",
    "OI_FEATURES",
    "JoinAudit",
    "flow_series_features",
    "join_new_features",
    "oi_series_features",
]
