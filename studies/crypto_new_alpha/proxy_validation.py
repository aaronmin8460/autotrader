"""Does the deleveraging proxy actually select liquidation-heavy periods?

The pilot's liquidation-pressure family is a proxy (OI-drop x price-move)
because no free multi-year liquidation record exists for the USDT-M market.
The one real record available - the coin-margined liquidationSnapshot,
2023-06-25..2024-10-14 - is used here as validation evidence only:

For every 15m grid bar in the overlap, sum the CM liquidation notional by
side over the bar's own window, then compare bars flagged by the proxy
event (trailing-p95 `delev_long_4h` / `delev_short_4h`) against unflagged
bars. The proxy is doing its job if flagged bars carry an order of magnitude
more force-close notional of the matching side.

The CM record is coin-margined while the proxy watches the USDT-M market -
the same underlying, a different contract population. Agreement here is
supportive, not proof; disagreement would be damning. Reported as such.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from studies.crypto_new_alpha.events import _trailing_quantile, concatenated_frame
from studies.crypto_new_alpha.frames import SYMBOLS

NORMALIZED_DIR = Path("/Volumes/AUTOTRADER_QA/datasets/crypto-new-alpha/normalized")
OUTPUT = Path(
    "/Volumes/AUTOTRADER_QA/reports/crypto-new-alpha-oi-liq-flow/event-study/proxy_validation.json"
)

CM_OF = {"BTC/USD": "BTCUSD_PERP", "ETH/USD": "ETHUSD_PERP"}

#: The liquidation record's own verified bounds.
OVERLAP_START = pd.Timestamp("2023-06-26", tz="UTC")
OVERLAP_END = pd.Timestamp("2024-10-13 23:45", tz="UTC")


def liquidation_per_bar(cm_symbol: str, bars: pd.Series) -> pd.DataFrame:
    """CM force-close notional per 15m bar, split by liquidated side."""
    events = pd.read_parquet(NORMALIZED_DIR / f"{cm_symbol}_liq.parquet")
    bucket = events["event_ts"].dt.floor("15min")
    # side SELL = long force-closed, BUY = short force-closed.
    long_notional = (
        events.loc[events["side"] == "SELL"]
        .assign(bucket=bucket.loc[events["side"] == "SELL"])
        .groupby("bucket")["notional_usd"]
        .sum()
    )
    short_notional = (
        events.loc[events["side"] == "BUY"]
        .assign(bucket=bucket.loc[events["side"] == "BUY"])
        .groupby("bucket")["notional_usd"]
        .sum()
    )
    frame = pd.DataFrame(index=pd.DatetimeIndex(bars))
    frame["long_liq_usd"] = long_notional.reindex(frame.index).fillna(0.0)
    frame["short_liq_usd"] = short_notional.reindex(frame.index).fillna(0.0)
    return frame.reset_index(drop=True)


def validate(symbol: str) -> dict:
    frame = concatenated_frame(symbol)
    overlap = frame.loc[
        (frame["timestamp"] >= OVERLAP_START) & (frame["timestamp"] <= OVERLAP_END)
    ].reset_index(drop=True)
    liq = liquidation_per_bar(CM_OF[symbol], overlap["timestamp"])

    # The liquidation that lands over the NEXT 4 hours (16 bars): the proxy is
    # a trailing detector, so the fair question is whether flagged instants
    # sit inside liquidation-heavy episodes (trailing 4h window, inclusive).
    trailing_long = liq["long_liq_usd"].rolling(16, min_periods=1).sum()
    trailing_short = liq["short_liq_usd"].rolling(16, min_periods=1).sum()

    out: dict = {"symbol": symbol, "overlap_bars": int(len(overlap))}
    for name, proxy_column, matching in (
        ("delev_long_p95", "delev_long_4h", trailing_long),
        ("delev_short_p95", "delev_short_4h", trailing_short),
    ):
        series = overlap[proxy_column]
        threshold = _trailing_quantile(series, 0.95)
        flagged = (series > threshold).fillna(False).to_numpy()
        if flagged.sum() == 0:
            out[name] = {"flagged": 0}
            continue
        flagged_notional = matching.to_numpy()[flagged]
        unflagged_notional = matching.to_numpy()[~flagged]
        out[name] = {
            "flagged": int(flagged.sum()),
            "flagged_mean_usd": float(np.mean(flagged_notional)),
            "flagged_median_usd": float(np.median(flagged_notional)),
            "unflagged_mean_usd": float(np.mean(unflagged_notional)),
            "unflagged_median_usd": float(np.median(unflagged_notional)),
            "mean_ratio": float(np.mean(flagged_notional) / max(np.mean(unflagged_notional), 1e-9)),
            "flagged_share_of_total_notional": float(
                flagged_notional.sum() / max(matching.sum(), 1e-9)
            ),
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    results = [validate(symbol) for symbol in SYMBOLS]
    payload = {"generated_at": datetime.now(tz=UTC).isoformat(), "symbols": results}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, OUTPUT)
    for row in results:
        print(json.dumps(row, indent=1)[:600])


if __name__ == "__main__":
    main()
