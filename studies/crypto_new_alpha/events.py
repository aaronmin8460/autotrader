"""Phase-5 event/conditional studies - run BEFORE any model is fitted.

Seven predeclared conditions (search-ledger.md §5), thresholds computed as
trailing 90-day percentiles on past data only, per symbol. Every condition is
reported at all three horizons with: n, mean, median, naive SE, %positive,
per-year and per-symbol splits, and a de-overlapped subset (events at least
one horizon apart) so dense overlapping bars cannot inflate the evidence.

The economic screening gate (40 bps gross) and the cost floor (60.18 bps
round trip) are applied in the report, not silently baked into definitions.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from studies.crypto_new_alpha.frames import HORIZONS, SYMBOLS, study_frames

OUTPUT_DIR = Path("/Volumes/AUTOTRADER_QA/reports/crypto-new-alpha-oi-liq-flow/event-study")

#: Trailing window for event thresholds: 90 days of 15m bars, half required.
THRESHOLD_BARS = 8640
THRESHOLD_MIN = 4320

#: Thin-sample flags (search-ledger.md §5).
MIN_POOLED = 100
MIN_YEAR = 30


def _trailing_quantile(series: pd.Series, quantile: float) -> pd.Series:
    """The trailing 90d quantile of a series, past-only (excludes the row).

    Shifted by one bar so the threshold at row i is computed from rows
    strictly before i - the event definition can never read its own value.
    """
    return series.rolling(THRESHOLD_BARS, min_periods=THRESHOLD_MIN).quantile(quantile).shift(1)


def build_event_masks(frame: pd.DataFrame) -> dict[str, pd.Series]:
    """The predeclared event conditions for one symbol's concatenated rows."""
    masks: dict[str, pd.Series] = {}

    delev_long = frame["delev_long_4h"]
    delev_short = frame["delev_short_4h"]
    oi_chg = frame["oi_chg_24h"]
    flow_4h = frame["flow_imb_4h"]
    flow_24h = frame["flow_imb_24h"]
    ret_96 = frame["return_96"]

    masks["E1_delev_long_p95"] = delev_long > _trailing_quantile(delev_long, 0.95)
    masks["E2_delev_short_p95"] = delev_short > _trailing_quantile(delev_short, 0.95)
    masks["E3a_oi_rise_p95"] = oi_chg > _trailing_quantile(oi_chg, 0.95)
    masks["E3b_oi_fall_p5"] = oi_chg < _trailing_quantile(oi_chg, 0.05)
    masks["E4a_flow_buy_p95"] = flow_4h > _trailing_quantile(flow_4h, 0.95)
    masks["E4b_flow_sell_p5"] = flow_4h < _trailing_quantile(flow_4h, 0.05)

    up, down = ret_96 > 0, ret_96 < 0
    oi_up, oi_down = oi_chg > 0, oi_chg < 0
    masks["E5a_price_up_oi_up"] = up & oi_up
    masks["E5b_price_up_oi_down"] = up & oi_down
    masks["E5c_price_down_oi_up"] = down & oi_up
    masks["E5d_price_down_oi_down"] = down & oi_down

    masks["E6a_delev_long_flow_sell"] = masks["E1_delev_long_p95"] & (flow_4h < 0)
    masks["E6b_delev_long_flow_buy"] = masks["E1_delev_long_p95"] & (flow_4h > 0)

    masks["E7a_cvd_div_up"] = up & (flow_24h < _trailing_quantile(flow_24h, 0.25))
    masks["E7b_cvd_div_down"] = down & (flow_24h > _trailing_quantile(flow_24h, 0.75))
    return {name: mask.fillna(False) for name, mask in masks.items()}


def deoverlap(positions: np.ndarray, horizon: int) -> np.ndarray:
    """Keep events at least `horizon` grid rows apart (first wins)."""
    kept: list[int] = []
    last = -(10**12)
    for position in positions:
        if position - last >= horizon:
            kept.append(position)
            last = position
    return np.asarray(kept, dtype="int64")


def _stats(returns: np.ndarray) -> dict:
    if len(returns) == 0:
        return {"n": 0}
    mean = float(np.mean(returns))
    std = float(np.std(returns, ddof=1)) if len(returns) > 1 else float("nan")
    return {
        "n": int(len(returns)),
        "mean_bps": mean * 1e4,
        "median_bps": float(np.median(returns)) * 1e4,
        "se_bps": (std / np.sqrt(len(returns))) * 1e4 if len(returns) > 1 else None,
        "pct_positive": float(np.mean(returns > 0)),
    }


def study_one(frame: pd.DataFrame, mask: pd.Series, horizon: int) -> dict:
    """One condition at one horizon: pooled, de-overlapped, per-year."""
    usable = mask & frame[f"usable_{horizon}"]
    rows = frame.loc[usable]
    returns = rows[f"fwd_{horizon}"].to_numpy(dtype="float64")
    pooled = _stats(returns)
    pooled["thin"] = pooled["n"] < MIN_POOLED

    positions = rows["grid_position"].to_numpy(dtype="int64")
    # grid_position restarts per era; offset the modern era so ordering holds.
    era_offset = np.where(rows["timestamp"].dt.year.to_numpy() >= 2024, 10_000_000, 0)
    order = np.argsort(positions + era_offset, kind="stable")
    kept = deoverlap((positions + era_offset)[order], horizon)
    kept_mask = np.isin(positions + era_offset, kept)
    deoverlapped = _stats(returns[kept_mask])

    per_year = {}
    years = rows["timestamp"].dt.year
    for year in sorted(years.unique()):
        year_returns = returns[(years == year).to_numpy()]
        year_stats = _stats(year_returns)
        year_stats["thin"] = year_stats["n"] < MIN_YEAR
        per_year[str(int(year))] = year_stats

    return {"pooled": pooled, "deoverlapped": deoverlapped, "per_year": per_year}


def concatenated_frame(symbol: str) -> pd.DataFrame:
    """Both eras' rows for one symbol, time-ordered, era boundary contiguous."""
    extended = study_frames("extended")[symbol].frame
    modern = study_frames("modern")[symbol].frame
    frame = pd.concat([extended, modern], ignore_index=True)
    if not frame["timestamp"].is_monotonic_increasing:
        raise ValueError("concatenated eras are not time-ordered")
    return frame


def run() -> dict:
    results: dict = {"generated_at": datetime.now(tz=UTC).isoformat(), "symbols": {}}
    for symbol in SYMBOLS:
        frame = concatenated_frame(symbol)
        masks = build_event_masks(frame)
        symbol_out: dict = {}
        for name, mask in masks.items():
            condition_out = {}
            for horizon in HORIZONS:
                condition_out[f"h{horizon}"] = study_one(frame, mask, horizon)
            symbol_out[name] = condition_out
        results["symbols"][symbol] = symbol_out

        # Unconditional reference: what an average row of the same population does.
        reference = {}
        for horizon in HORIZONS:
            usable = frame[f"usable_{horizon}"]
            reference[f"h{horizon}"] = _stats(
                frame.loc[usable, f"fwd_{horizon}"].to_numpy(dtype="float64")
            )
        results["symbols"][symbol]["UNCONDITIONAL"] = reference
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(OUTPUT_DIR / "event_study.json"))
    args = parser.parse_args()
    results = run()
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(results, indent=2, default=str))
    os.replace(tmp, path)
    print(f"event study -> {path}")


if __name__ == "__main__":
    main()
