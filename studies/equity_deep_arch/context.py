"""Session-level market-context features for EDA-4 and EDA-5.

One row per (symbol, session). Every feature is computable at that session's
close from session closes alone; the forward label resolves at the *next*
session's close and carries its own knowable-at index for purging.

Close histories are stitched from the locked w00 fragment (2020-08-17..) and
the main frames (2021-01-04..) so that 200-session warm-ups complete before
the scored region opens. The fragment is used for feature warm-up only — no
label row before the scored region is ever emitted to a strategy result, per
the ledger's w00 lock protocol.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from studies.equity_10_full import STUDY_SYMBOLS
from studies.equity_deep_arch.state import session_closes

#: Trailing window for realized volatility, returns and relative strength.
SHORT_SESSIONS = 20

#: Trailing window for the trend average.
TREND_SESSIONS = 200

FEATURE_COLUMNS: tuple[str, ...] = (
    "mkt_dist_sma",
    "mkt_drawdown",
    "mkt_vol20",
    "breadth",
    "dispersion",
    "own_dist_sma",
    "own_rel20",
    "own_drawdown",
)


class ContextError(Exception):
    """Context features that cannot be built as specified."""


def stitched_closes(datasets: Path, symbol: str) -> pd.Series:
    """Session-close series for `symbol`: w00 fragment + main frame."""
    main = sorted(datasets.glob(f"{symbol}_15m_*session.parquet"))
    fragment = sorted((datasets / "w00-fragment").glob(f"{symbol}_15m_*session.parquet"))
    if len(main) != 1:
        raise ContextError(f"Expected one main frame for {symbol}, found {main}.")
    frames = [pd.read_parquet(path) for path in [*fragment, *main]]
    closes = pd.concat([session_closes(frame) for frame in frames], ignore_index=True)
    if closes["session"].duplicated().any():
        raise ContextError(f"{symbol}: fragment and main frames overlap on sessions.")
    return pd.Series(
        closes["close"].to_numpy(dtype="float64"),
        index=pd.Index(closes["session"], name="session"),
    )


def build_context_frame(datasets: Path) -> pd.DataFrame:
    """All context feature rows, one per (symbol, session), with forward labels.

    Columns: symbol, session (date), the eight FEATURE_COLUMNS, forward_return
    (next session close-to-close), label_knowable_session (the session whose
    close resolves the label). Rows with any undefined feature or label are
    dropped.
    """
    closes = {symbol: stitched_closes(datasets, symbol) for symbol in STUDY_SYMBOLS}
    master = closes["SPY"].index
    aligned = pd.DataFrame({s: series.reindex(master).ffill() for s, series in closes.items()})

    spy = aligned["SPY"]
    spy_sma = spy.rolling(TREND_SESSIONS).mean()
    spy_dd = spy / spy.cummax() - 1.0
    spy_logret = np.log(spy / spy.shift(1))
    spy_vol20 = spy_logret.rolling(SHORT_SESSIONS).std() * float(np.sqrt(252.0))
    ret20 = aligned / aligned.shift(SHORT_SESSIONS) - 1.0
    above = pd.DataFrame(
        {s: aligned[s] > aligned[s].rolling(TREND_SESSIONS).mean() for s in STUDY_SYMBOLS}
    )
    sma_ready = pd.DataFrame(
        {s: aligned[s].rolling(TREND_SESSIONS).mean().notna() for s in STUDY_SYMBOLS}
    ).all(axis=1)
    breadth = above.mean(axis=1).where(sma_ready)
    dispersion = ret20.std(axis=1)

    sessions = list(master)
    rows: list[dict[str, object]] = []
    for symbol in STUDY_SYMBOLS:
        own = aligned[symbol]
        own_sma = own.rolling(TREND_SESSIONS).mean()
        own_dd = own / own.cummax() - 1.0
        own_rel = ret20[symbol] - ret20["SPY"]
        forward = own.shift(-1) / own - 1.0
        frame = pd.DataFrame(
            {
                "mkt_dist_sma": spy / spy_sma - 1.0,
                "mkt_drawdown": spy_dd,
                "mkt_vol20": spy_vol20,
                "breadth": breadth,
                "dispersion": dispersion,
                "own_dist_sma": own / own_sma - 1.0,
                "own_rel20": own_rel,
                "own_drawdown": own_dd,
                "forward_return": forward,
            }
        )
        valid = frame.notna().all(axis=1)
        for position in np.flatnonzero(valid.to_numpy()):
            if position + 1 >= len(sessions):
                continue
            record: dict[str, object] = {"symbol": symbol, "session": sessions[position]}
            record.update({c: float(frame.iloc[position][c]) for c in FEATURE_COLUMNS})
            record["forward_return"] = float(frame.iloc[position]["forward_return"])
            record["label_knowable_session"] = sessions[position + 1]
            rows.append(record)
    return pd.DataFrame(rows)


def session_index(datasets: Path) -> dict[date, int]:
    """Ordinal position of every session on the stitched SPY calendar."""
    master = stitched_closes(datasets, "SPY").index
    return {day: position for position, day in enumerate(master)}


__all__ = [
    "FEATURE_COLUMNS",
    "SHORT_SESSIONS",
    "TREND_SESSIONS",
    "ContextError",
    "build_context_frame",
    "session_index",
    "stitched_closes",
]
