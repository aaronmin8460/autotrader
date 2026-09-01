"""Causal asset fingerprints (ledger §L3) and current-state features (§L7).

Every value reported for mark ``m`` is computed from completed sessions
strictly before ``m`` (one session of lag, the incumbent convention). Windows
are counted on the symbol's own observed-session axis; a feature is NaN until
its full window (and any declared minimum observation count) exists — no
partial windows, no backfill, no pre-listing values.

Market-relative features (beta, up/down beta, residual return, relative
strength) are computed on session dates the symbol and SPY share, pairing
returns over consecutive shared dates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from autotrader.equity.session import market_date

#: Structural windows (ledger §L3): 6 months and 12 months of sessions.
WINDOW_6M = 126
WINDOW_12M = 252

#: Minimum qualifying observations for conditional regressions (§L3).
MIN_UP_DOWN_SESSIONS = 60
MIN_NEGATIVE_SESSIONS = 30
MIN_REVERSAL_PAIRS = 20

#: The structural feature set, exactly as declared (§L3), in ledger order.
STRUCTURAL_FEATURES: tuple[str, ...] = (
    "beta_252",
    "up_beta_252",
    "down_beta_252",
    "vol_126",
    "downside_vol_126",
    "vol_of_vol_126",
    "gap_vol_126",
    "trend_persist_126",
    "trend_share_126",
    "reversal_126",
    "maxdd_252",
    "underwater_252",
    "resid_ret_252",
    "dollar_vol_126",
)

#: The current-state feature set, exactly as declared (§L7).
STATE_FEATURES: tuple[str, ...] = (
    "rs_63",
    "vol_ratio",
    "dd_now",
    "trend_dist",
    "resid_21",
)

ANNUALIZE = 252


class FingerprintError(Exception):
    """A fingerprint request that cannot be answered causally."""


def symbol_sessions(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per session: date, first open, last close, dollar volume.

    Dollar volume is the session sum of bar close × bar volume — the same
    liquidity notion as the prior manifest's screen.
    """
    if frame.empty:
        raise FingerprintError("Cannot derive sessions from an empty frame.")
    days = [market_date(ts.to_pydatetime()) for ts in frame["timestamp"]]
    working = pd.DataFrame(
        {
            "session": days,
            "open": frame["open"].to_numpy(dtype="float64"),
            "close": frame["close"].to_numpy(dtype="float64"),
            "notional": (
                frame["close"].to_numpy(dtype="float64")
                * frame["volume"].to_numpy(dtype="float64")
            ),
        }
    )
    grouped = working.groupby("session", sort=True)
    return pd.DataFrame(
        {
            "session": list(grouped.groups),
            "open": grouped["open"].first().to_numpy(),
            "close": grouped["close"].last().to_numpy(),
            "dollar_volume": grouped["notional"].sum().to_numpy(),
        }
    )


@dataclass(frozen=True)
class SymbolSeries:
    """Per-symbol arrays used by every fingerprint, built once."""

    sessions: np.ndarray  # object array of datetime.date, ascending
    opens: np.ndarray
    closes: np.ndarray
    dollar_volume: np.ndarray
    paired_sessions: np.ndarray  # dates shared with SPY (ascending)
    paired_own_returns: np.ndarray  # log returns over consecutive shared dates
    paired_spy_returns: np.ndarray  # SPY log returns over the same date pairs


def build_series(table: pd.DataFrame, spy_table: pd.DataFrame) -> SymbolSeries:
    """Assemble the raw arrays for one symbol from its session table."""
    sessions = np.asarray(table["session"].tolist(), dtype=object)
    closes = table["close"].to_numpy(dtype="float64")
    spy_map = dict(
        zip(spy_table["session"].tolist(), spy_table["close"].to_numpy(dtype="float64"))
    )
    shared_mask = np.array([day in spy_map for day in sessions], dtype=bool)
    shared_dates = sessions[shared_mask]
    own_shared = closes[shared_mask]
    spy_shared = np.array([spy_map[day] for day in shared_dates], dtype="float64")
    return SymbolSeries(
        sessions=sessions,
        opens=table["open"].to_numpy(dtype="float64"),
        closes=closes,
        dollar_volume=table["dollar_volume"].to_numpy(dtype="float64"),
        paired_sessions=shared_dates[1:],
        paired_own_returns=np.diff(np.log(own_shared)),
        paired_spy_returns=np.diff(np.log(spy_shared)),
    )


def _end_index(sessions: np.ndarray, mark: date) -> int:
    """Number of sessions strictly before ``mark`` (the causal slice length)."""
    return int(np.searchsorted(sessions, mark, side="left"))


def _ols_beta(own: np.ndarray, spy: np.ndarray) -> tuple[float, float]:
    """OLS slope and intercept of own on SPY returns."""
    spy_mean = spy.mean()
    own_mean = own.mean()
    var = float(((spy - spy_mean) ** 2).sum())
    if var <= 0.0:
        return float("nan"), float("nan")
    beta = float(((spy - spy_mean) * (own - own_mean)).sum() / var)
    return beta, float(own_mean - beta * spy_mean)


def structural_at(series: SymbolSeries, mark: date) -> dict[str, float]:
    """The 14 structural fingerprints for one symbol at one mark (§L3)."""
    out = dict.fromkeys(STRUCTURAL_FEATURES, float("nan"))

    # --- market-relative block (paired axis, 252 sessions of returns) ---
    pend = _end_index(series.paired_sessions, mark)
    if pend >= WINDOW_12M:
        own = series.paired_own_returns[pend - WINDOW_12M : pend]
        spy = series.paired_spy_returns[pend - WINDOW_12M : pend]
        beta, alpha = _ols_beta(own, spy)
        out["beta_252"] = beta
        out["resid_ret_252"] = alpha * ANNUALIZE
        up = spy > 0.0
        down = spy < 0.0
        if int(up.sum()) >= MIN_UP_DOWN_SESSIONS:
            out["up_beta_252"] = _ols_beta(own[up], spy[up])[0]
        if int(down.sum()) >= MIN_UP_DOWN_SESSIONS:
            out["down_beta_252"] = _ols_beta(own[down], spy[down])[0]

    # --- volatility / persistence block (paired axis, 126 returns) ---
    if pend >= WINDOW_6M:
        r = series.paired_own_returns[pend - WINDOW_6M : pend]
        out["vol_126"] = float(r.std(ddof=1)) * np.sqrt(ANNUALIZE)
        negative = r[r < 0.0]
        if len(negative) >= MIN_NEGATIVE_SESSIONS:
            out["downside_vol_126"] = float(negative.std(ddof=1)) * np.sqrt(ANNUALIZE)
        out["trend_persist_126"] = _lag1_autocorr(r)
        out["reversal_126"] = _reversal(r)
    if pend >= WINDOW_6M + 20:
        r = series.paired_own_returns[pend - (WINDOW_6M + 20) : pend]
        rolling = np.array(
            [r[i : i + 21].std(ddof=1) for i in range(len(r) - 20)], dtype="float64"
        )
        out["vol_of_vol_126"] = float(rolling.std(ddof=1)) * np.sqrt(ANNUALIZE)

    # --- own-axis block ---
    end = _end_index(series.sessions, mark)
    if end >= WINDOW_6M + 1:
        opens = series.opens[end - WINDOW_6M : end]
        prior_closes = series.closes[end - WINDOW_6M - 1 : end - 1]
        gaps = np.log(opens / prior_closes)
        out["gap_vol_126"] = float(gaps.std(ddof=1)) * np.sqrt(ANNUALIZE)
    if end >= WINDOW_6M:
        out["dollar_vol_126"] = float(
            np.log10(np.median(series.dollar_volume[end - WINDOW_6M : end]))
        )
    if end >= WINDOW_6M + 50:
        closes = series.closes[end - WINDOW_6M - 50 : end]
        sma_lagged = np.array(
            [closes[i : i + 50].mean() for i in range(WINDOW_6M)], dtype="float64"
        )
        out["trend_share_126"] = float((closes[50:] > sma_lagged).mean())
    if end >= WINDOW_12M:
        closes = series.closes[end - WINDOW_12M : end]
        peak = np.maximum.accumulate(closes)
        drawdown = closes / peak - 1.0
        out["maxdd_252"] = float(drawdown.min())
        out["underwater_252"] = float((drawdown < -0.05).mean())
    return out


def _lag1_autocorr(returns: np.ndarray) -> float:
    a, b = returns[:-1], returns[1:]
    sa, sb = a.std(ddof=1), b.std(ddof=1)
    if sa <= 0.0 or sb <= 0.0:
        return float("nan")
    return float(((a - a.mean()) * (b - b.mean())).mean() / (sa * sb))


def _reversal(returns: np.ndarray) -> float:
    """Correlation of non-overlapping 5-session returns with the next one."""
    chunks = len(returns) // 5
    if chunks < 2:
        return float("nan")
    fives = returns[: chunks * 5].reshape(chunks, 5).sum(axis=1)
    a, b = fives[:-1], fives[1:]
    if len(a) < MIN_REVERSAL_PAIRS:
        return float("nan")
    sa, sb = a.std(ddof=1), b.std(ddof=1)
    if sa <= 0.0 or sb <= 0.0:
        return float("nan")
    return float(((a - a.mean()) * (b - b.mean())).mean() / (sa * sb))


def state_at(series: SymbolSeries, mark: date, beta_252: float) -> dict[str, float]:
    """The 5 current-state features for one symbol at one mark (§L7)."""
    out = dict.fromkeys(STATE_FEATURES, float("nan"))

    pend = _end_index(series.paired_sessions, mark)
    if pend >= 63:
        own63 = float(series.paired_own_returns[pend - 63 : pend].sum())
        spy63 = float(series.paired_spy_returns[pend - 63 : pend].sum())
        out["rs_63"] = own63 - spy63
    if pend >= 21 and np.isfinite(beta_252):
        own21 = float(series.paired_own_returns[pend - 21 : pend].sum())
        spy21 = float(series.paired_spy_returns[pend - 21 : pend].sum())
        out["resid_21"] = own21 - beta_252 * spy21
    if pend >= WINDOW_12M + 20:
        r = series.paired_own_returns[pend - (WINDOW_12M + 20) : pend]
        rolling = np.array(
            [r[i : i + 21].std(ddof=1) for i in range(len(r) - 20)], dtype="float64"
        )
        median = float(np.median(rolling))
        if median > 0.0:
            out["vol_ratio"] = float(rolling[-1] / median)

    end = _end_index(series.sessions, mark)
    if end >= WINDOW_12M:
        closes = series.closes[end - WINDOW_12M : end]
        out["dd_now"] = float(closes[-1] / closes.max() - 1.0)
    if end >= 100:
        closes = series.closes[end - 100 : end]
        out["trend_dist"] = float(closes[-1] / closes.mean() - 1.0)
    return out


def fingerprint_panel(
    tables: Mapping[str, pd.DataFrame],
    marks: Sequence[date],
    *,
    spy_symbol: str = "SPY",
) -> pd.DataFrame:
    """Structural + state features for every (mark, symbol).

    Returns a frame indexed by (mark, symbol) with one column per feature.
    ``tables`` maps symbol → session table (from :func:`symbol_sessions`).
    """
    if spy_symbol not in tables:
        raise FingerprintError(f"{spy_symbol} session table is required.")
    spy_table = tables[spy_symbol]
    series = {
        symbol: build_series(tables[symbol], spy_table) for symbol in sorted(tables)
    }
    rows: list[dict[str, object]] = []
    for mark in marks:
        for symbol in sorted(tables):
            structural = structural_at(series[symbol], mark)
            state = state_at(series[symbol], mark, structural["beta_252"])
            rows.append({"mark": mark, "symbol": symbol, **structural, **state})
    panel = pd.DataFrame(rows).set_index(["mark", "symbol"]).sort_index()
    return panel


def cross_sectional_z(
    panel: pd.DataFrame,
    features: Sequence[str],
    *,
    winsor: float = 3.0,
    min_symbols: int = 20,
) -> pd.DataFrame:
    """Z-score each feature across symbols at each mark (§L3 standardization).

    Uses only that mark's contemporaneous values; marks with fewer than
    ``min_symbols`` non-NaN symbols keep NaN for that feature.
    """
    out = panel[list(features)].copy()
    for mark in out.index.get_level_values("mark").unique():
        block = out.loc[mark]
        for feature in features:
            values = block[feature]
            valid = values.dropna()
            if len(valid) < min_symbols or float(valid.std(ddof=1)) <= 0.0:
                out.loc[(mark, slice(None)), feature] = float("nan")
                continue
            z = (values - valid.mean()) / valid.std(ddof=1)
            out.loc[(mark, slice(None)), feature] = z.clip(-winsor, winsor).to_numpy()
    return out


__all__ = [
    "ANNUALIZE",
    "STATE_FEATURES",
    "STRUCTURAL_FEATURES",
    "WINDOW_6M",
    "WINDOW_12M",
    "FingerprintError",
    "SymbolSeries",
    "build_series",
    "cross_sectional_z",
    "fingerprint_panel",
    "state_at",
    "structural_at",
    "symbol_sessions",
]
