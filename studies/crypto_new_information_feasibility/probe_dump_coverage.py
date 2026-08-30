"""Enumerate the public futures-data dump catalog and pin coverage starts.

Read-only GET requests against the dump host's S3-style listing API. For each
dataset class of interest, lists the first few keys under the symbol's
prefix, which yields the earliest available file without downloading
anything.

Findings this probe established (retrieved 2026-08-30):

===================  =========================  ==========================
dataset              earliest file              cadence / shape
===================  =========================  ==========================
fundingRate          2020-01 (monthly)          one row per 8h settlement
premiumIndexKlines   2020-01 (monthly, 15m)     klines named SYMBOL-15m-YM
metrics              2020-09-01 (daily)         5m OI / long-short ratios
bookDepth            2023-01-01 (daily)         ~30s, +/-1..5% cum depth
bookTicker           2023-05 (monthly)          L1 ticks, ~6.7 GB/month
===================  =========================  ==========================
"""

from __future__ import annotations

import ssl
import urllib.request
import xml.etree.ElementTree as ElementTree

import certifi

LISTING_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

PREFIXES = (
    "data/futures/um/monthly/fundingRate/{symbol}/",
    "data/futures/um/monthly/premiumIndexKlines/{symbol}/15m/",
    "data/futures/um/daily/metrics/{symbol}/",
    "data/futures/um/daily/bookDepth/{symbol}/",
    "data/futures/um/monthly/bookTicker/{symbol}/",
)

SYMBOLS = ("BTCUSDT", "ETHUSDT")

_S3_NAMESPACE = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def earliest_keys(prefix: str, max_keys: int = 4) -> list[str]:
    """The lexically first keys under a prefix - i.e. the earliest files."""
    url = f"{LISTING_URL}?prefix={prefix}&max-keys={max_keys}"
    with urllib.request.urlopen(url, timeout=30, context=_SSL_CONTEXT) as response:
        tree = ElementTree.parse(response)
    return [
        element.text
        for element in tree.getroot().iter(f"{_S3_NAMESPACE}Key")
        if element.text and not element.text.endswith(".CHECKSUM")
    ]


def main() -> None:
    for symbol in SYMBOLS:
        for template in PREFIXES:
            prefix = template.format(symbol=symbol)
            keys = earliest_keys(prefix)
            first = keys[0].rsplit("/", 1)[-1] if keys else "(none)"
            print(f"{prefix}: earliest = {first}")


if __name__ == "__main__":
    main()
