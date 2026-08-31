"""Digest-verified 15m reference bars for +24h markouts and context.

Loads the project's existing historical parquets (2021–23 extended and
2024–26 modern eras), refuses a digest mismatch, and serves the per-event
context the accounting layer needs: +24h reference close, limit-retouch
tests, trailing realized volatility, and the trailing 14-day return used
by the trend-conditioned stratification.
"""

from __future__ import annotations

import hashlib
import os
from datetime import timedelta
from functools import lru_cache
from pathlib import Path

import pandas as pd

#: Recorded digests. Modern-era values match the V1–V5 study's
#: `dataset_provenance.json`; extended-era values match the sidecars
#: written at download time. A mismatch is an error, never a warning.
EXPECTED_SHA256 = {
    "crypto-historical/BTC_USD_15m_2024-01-01_2026-08-28.parquet": (
        "7f04a15a2c28a55c146afe188bff6adc4bd2add53299e7223e4832a96b99dc67"
    ),
    "crypto-historical/ETH_USD_15m_2024-01-01_2026-08-28.parquet": (
        "43d82b851701989cad8e1c220e7d7e84b6cb30d62d0af7c16c4b3f83fc332624"
    ),
    "crypto-historical-extended/BTC_USD_15m_2021-01-01_2023-12-31.parquet": (
        "9e91f9be80a38e2994a9ee6b61a945af98954b9e3133e053989582c8949de489"
    ),
    "crypto-historical-extended/ETH_USD_15m_2021-01-01_2023-12-31.parquet": (
        "cad8932bdcbed7e0710c21a443e4d7c1b11095edd6b0b4eae3df552333a6a4f6"
    ),
}

BARS_PER_DAY = 96
VOL_WINDOW_BARS = 96  # trailing 24h realized volatility
TREND_WINDOW_DAYS = 14


def datasets_root() -> Path:
    qa = os.environ.get("AUTOTRADER_QA", "/Volumes/AUTOTRADER_QA")
    return Path(qa) / "datasets"


def _verified_frame(relative: str) -> pd.DataFrame:
    path = datasets_root() / relative
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = EXPECTED_SHA256[relative]
    if digest != expected:
        raise RuntimeError(f"digest mismatch for {relative}: {digest} != {expected}")
    frame = pd.read_parquet(path)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame


@lru_cache(maxsize=2)
def bars_for(symbol: str) -> pd.DataFrame:
    """The full 2021→2026 15m series for one symbol, digest-verified."""
    slug = symbol.replace("/", "_")
    extended = _verified_frame(
        f"crypto-historical-extended/{slug}_15m_2021-01-01_2023-12-31.parquet"
    )
    modern = _verified_frame(f"crypto-historical/{slug}_15m_2024-01-01_2026-08-28.parquet")
    frame = pd.concat([extended, modern], ignore_index=True)
    frame = frame.drop_duplicates(subset="timestamp", keep="last")
    frame = frame.sort_values("timestamp", kind="stable").reset_index(drop=True)
    if not frame["timestamp"].is_monotonic_increasing:
        raise RuntimeError(f"non-monotonic bar series for {symbol}")
    return frame[["timestamp", "open", "high", "low", "close"]]


def close_at_or_before(symbol: str, ts: pd.Timestamp) -> float | None:
    frame = bars_for(symbol)
    eligible = frame[frame["timestamp"] <= ts]
    if eligible.empty:
        return None
    return float(eligible.iloc[-1]["close"])


def reference_close_24h(symbol: str, decision_ts: pd.Timestamp) -> float | None:
    """Close of the last completed bar at decision + 24h."""
    return close_at_or_before(symbol, decision_ts + timedelta(hours=24))


def limit_retouched_within_24h(
    symbol: str, side: str, limit_price: float, after_ts: pd.Timestamp
) -> tuple[bool, float | None]:
    """Did the bar path touch the limit again within 24h of `after_ts`?

    Returns (touched, hours_to_touch). A buy limit is touched when a bar's
    low reaches it; a sell limit when a bar's high does.
    """
    frame = bars_for(symbol)
    window = frame[
        (frame["timestamp"] > after_ts) & (frame["timestamp"] <= after_ts + timedelta(hours=24))
    ]
    if window.empty:
        return False, None
    if side == "buy":
        touched = window[window["low"] <= limit_price]
    else:
        touched = window[window["high"] >= limit_price]
    if touched.empty:
        return False, None
    first = touched.iloc[0]["timestamp"]
    return True, float((first - after_ts).total_seconds() / 3600.0)


def trailing_context(symbol: str, decision_ts: pd.Timestamp) -> dict:
    """Trailing 24h realized vol and 14d return, knowable at the decision."""
    frame = bars_for(symbol)
    past = frame[frame["timestamp"] <= decision_ts]
    closes = past["close"].tail(VOL_WINDOW_BARS + 1)
    vol = None
    if len(closes) == VOL_WINDOW_BARS + 1:
        returns = closes.pct_change().dropna()
        vol = float(returns.std())
    trend = None
    lookback = past[past["timestamp"] <= decision_ts - timedelta(days=TREND_WINDOW_DAYS)]
    if not lookback.empty and not past.empty:
        then = float(lookback.iloc[-1]["close"])
        now = float(past.iloc[-1]["close"])
        if then > 0:
            trend = now / then - 1.0
    return {"realized_vol_24h": vol, "trend_14d": trend}
