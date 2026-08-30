"""Read-only live-leg archiver for funding / premium semantic-drift measurement.

Why this exists. The pilot's historical features come from a perpetual venue
whose REST API is geo-blocked from this machine; a deployed system would have
to read funding live from a *different*, accessible venue. The feasibility
study measured only ~3 months of overlap between the two (corr 0.684, sign
agreement 82.5%) and recommended starting a same-source live record now so a
longer overlap exists before any deployment decision is ever made. This
archiver is that record. It informs nothing in this pilot's results.

**Safety boundary, enforced by construction.** Every URL this module can
build is a public, unauthenticated market-data GET. It holds no credentials,
imports no broker client, and has no code path that places, cancels, replaces
or queries an order. The endpoint allowlist below is the whole surface.

Each capture writes one newline-delimited JSON record: the raw response
verbatim, the venue's own source timestamp, local receipt time, the
normalised timestamp, symbol and data type - so a later study can measure
historical-vs-live drift without re-deriving what was fetched.
"""

from __future__ import annotations

import argparse
import json
import ssl
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ARCHIVE_DIR = Path("/Volumes/AUTOTRADER_QA/datasets/crypto-funding-basis/live-archive")

#: The complete set of endpoints this module may contact. Read-only public
#: market data only; anything not on this list cannot be requested.
ENDPOINTS: dict[str, str] = {
    "okx_funding_current": "https://www.okx.com/api/v5/public/funding-rate?instId={inst}",
    "okx_funding_history": (
        "https://www.okx.com/api/v5/public/funding-rate-history?instId={inst}&limit=100"
    ),
}

#: Live instrument -> the pilot's spot decision stream. USDT-quoted swaps are
#: used because they are the live analogue of the USDT-quoted perps the
#: historical features come from; matching the contaminant is the point.
INSTRUMENTS: dict[str, str] = {
    "BTC-USDT-SWAP": "BTC/USD",
    "ETH-USDT-SWAP": "ETH/USD",
}

USER_AGENT = "autotrader-research/crypto-funding-basis-pilot (read-only market data)"


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def fetch(url: str, timeout: float = 25.0) -> tuple[str, str]:
    """GET a public endpoint. Returns (body, local receipt time)."""
    if not url.startswith("https://www.okx.com/api/v5/public/"):
        raise ValueError(f"refusing a URL outside the public market-data allowlist: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
        body = response.read().decode("utf-8")
    return body, datetime.now(tz=UTC).isoformat()


def _source_timestamp(payload: dict, data_type: str) -> str | None:
    rows = payload.get("data") or []
    if not rows:
        return None
    key = "fundingTime"
    value = rows[0].get(key)
    return str(value) if value is not None else None


def capture_once() -> list[dict]:
    """One capture cycle across every instrument and endpoint."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    path = ARCHIVE_DIR / f"okx-funding-{day}.jsonl"
    records: list[dict] = []
    for inst, spot in INSTRUMENTS.items():
        for data_type, template in ENDPOINTS.items():
            url = template.format(inst=inst)
            body, received = fetch(url)
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {}
            source = _source_timestamp(payload, data_type)
            normalized = (
                datetime.fromtimestamp(int(source) / 1000.0, tz=UTC).isoformat()
                if source and source.isdigit()
                else None
            )
            record = {
                "data_type": data_type,
                "venue": "okx",
                "instrument": inst,
                "spot_symbol": spot,
                "source_url": url,
                "source_timestamp_raw": source,
                "source_timestamp_normalized": normalized,
                "local_receipt_time": received,
                "raw_response": body,
            }
            records.append(record)
    with path.open("a") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="capture a single cycle and exit")
    args = parser.parse_args()
    if not args.once:
        raise SystemExit(
            "This archiver only runs single capture cycles (--once). Continuous "
            "operation is the operator's scheduling decision, not this module's: "
            "see the pilot report's live-leg section for the schedule command."
        )
    records = capture_once()
    for record in records:
        print(
            f"{record['venue']} {record['instrument']} {record['data_type']} "
            f"source={record['source_timestamp_normalized']} "
            f"received={record['local_receipt_time']} bytes={len(record['raw_response'])}"
        )
    print(f"appended {len(records)} record(s) to {ARCHIVE_DIR}")


if __name__ == "__main__":
    main()
