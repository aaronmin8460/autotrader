"""Tick-median BBO spread on the execution venue's historical L1 quotes.

Read-only. The venue serves crypto market data without authentication; this
script issues only GET requests to the public historical-quotes endpoint and
writes summary statistics to JSON. No credential is read and no trading
endpoint is contacted.

Method: for each (symbol, date, one-hour window) cell, fetch up to 10,000
quote ticks and report p25/p50/p75/p95 of the relative bid-ask spread in
basis points. Tick-weighted, not time-weighted: quote updates cluster in
active periods, so these figures over-weight busy moments. They are an
era-level indicator, not an execution model.
"""

from __future__ import annotations

import json
import ssl
import statistics
import urllib.request
from pathlib import Path

import certifi

QUOTES_URL = "https://data.alpaca.markets/v1beta3/crypto/us/quotes"

#: One quiet and one active UTC hour per sampled day.
WINDOWS = (("14:00:00", "15:00:00"), ("02:00:00", "03:00:00"))

#: One day per roughly six-month era across the venue's quote history
#: (which starts between 2023-03 and 2023-05; earlier days return empty).
DATES = (
    "2023-07-05",
    "2024-03-05",
    "2024-11-05",
    "2025-06-04",
    "2026-02-04",
    "2026-08-25",
)

SYMBOLS = ("BTC/USD", "ETH/USD")

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def fetch_quotes(symbol: str, start: str, end: str, limit: int = 10_000) -> list[dict]:
    """One page of historical L1 quotes, oldest-first, unauthenticated."""
    url = f"{QUOTES_URL}?symbols={symbol.replace('/', '%2F')}&start={start}&end={end}&limit={limit}"
    with urllib.request.urlopen(url, timeout=30, context=_SSL_CONTEXT) as response:
        payload = json.load(response)
    return payload.get("quotes", {}).get(symbol, [])


def spread_stats(quotes: list[dict]) -> dict | None:
    """Quartiles of relative spread in bps over valid two-sided ticks."""
    spreads = sorted(
        (tick["ap"] - tick["bp"]) / ((tick["ap"] + tick["bp"]) / 2) * 1e4
        for tick in quotes
        if tick.get("bp") and tick.get("ap") and tick["ap"] > tick["bp"] > 0
    )
    if not spreads:
        return None
    n = len(spreads)
    return {
        "ticks": n,
        "p25_bps": round(spreads[n // 4], 2),
        "p50_bps": round(statistics.median(spreads), 2),
        "p75_bps": round(spreads[(3 * n) // 4], 2),
        "p95_bps": round(spreads[max((95 * n) // 100 - 1, 0)], 2),
    }


def main(output_path: Path) -> None:
    rows = []
    for symbol in SYMBOLS:
        for day in DATES:
            for window_start, window_end in WINDOWS:
                quotes = fetch_quotes(symbol, f"{day}T{window_start}Z", f"{day}T{window_end}Z")
                stats = spread_stats(quotes)
                rows.append(
                    {
                        "symbol": symbol,
                        "date": day,
                        "window_utc": window_start,
                        **(stats or {"ticks": 0}),
                    }
                )
                print(rows[-1])
    output_path.write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main(Path("alpaca_spread_stats.json"))
