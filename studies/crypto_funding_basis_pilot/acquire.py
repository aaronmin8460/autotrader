"""Resumable, checksum-verified acquisition of the public derivative dumps.

Two datasets, both predeclared by `pilot-designs.md` PILOT 1:

* `fundingRate`  - monthly settled perpetual funding, one row per 8h settlement
* `premiumIndexKlines/15m` - monthly premium-index bars on the 15m UTC grid

Both are USD-margined (`futures/um`) **perpetual** products for `BTCUSDT` and
`ETHUSDT`. No coin-margined product, no quarterly future, no other interval.

Resume discipline, in the order the mandate requires:

1. A final file that already exists and still matches the provider's
   `.CHECKSUM` sidecar is skipped without a network body fetch.
2. A download lands in a `.part` file and is promoted by `os.replace` only
   after its SHA-256 equals the provider digest, so an interrupted or
   truncated transfer can never be mistaken for a complete one.
3. A stale `.part` from a previous run is discarded, never appended to.

Every acquired file gets a manifest row: source URL, symbol, data type,
period, size, provider checksum, locally computed SHA-256, retrieval
timestamp and validation status.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

BASE_URL = "https://data.binance.vision/data/futures/um/monthly"

DATASET_DIR = Path("/Volumes/AUTOTRADER_QA/datasets/crypto-funding-basis")

#: Derivative symbol -> spot decision stream. Recorded, never inferred later.
SYMBOL_MAP: dict[str, str] = {"BTCUSDT": "BTC/USD", "ETHUSDT": "ETH/USD"}

#: Predeclared acquisition span. The true inventory is discovered, not assumed:
#: months that 404 are recorded as absent rather than retried or faked.
FIRST_MONTH = (2020, 1)
LAST_MONTH = (2026, 8)

USER_AGENT = "autotrader-research/crypto-funding-basis-pilot (public dump reader)"
RETRIES = 4
BACKOFF_SECONDS = 2.0


def _ssl_context() -> ssl.SSLContext:
    """A verifying context that does not depend on the interpreter's own store.

    This Python build ships without a populated system trust store, so the
    default context fails every HTTPS handshake with CERTIFICATE_VERIFY_FAILED.
    `certifi`'s bundle is used explicitly. Verification stays **on**: an
    unverified context would make a silent man-in-the-middle indistinguishable
    from the real CDN, and the provider checksums alone would not catch it
    because a substituting proxy could serve a matching sidecar.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CONTEXT = _ssl_context()


@dataclass(frozen=True)
class Target:
    """One file to acquire."""

    data_type: str
    symbol: str
    period: str
    url: str
    path: Path

    @property
    def checksum_url(self) -> str:
        return self.url + ".CHECKSUM"


@dataclass
class Record:
    """One manifest row."""

    data_type: str
    symbol: str
    spot_symbol: str
    period: str
    source_url: str
    local_path: str
    size_bytes: int | None
    provider_sha256: str | None
    local_sha256: str | None
    retrieved_at: str | None
    status: str


def months(first: tuple[int, int], last: tuple[int, int]) -> list[str]:
    """Inclusive `YYYY-MM` sequence."""
    year, month = first
    out: list[str] = []
    while (year, month) <= last:
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return out


def targets() -> list[Target]:
    """Every candidate file in the predeclared span, both types, both symbols."""
    out: list[Target] = []
    for symbol in SYMBOL_MAP:
        for period in months(FIRST_MONTH, LAST_MONTH):
            name = f"{symbol}-fundingRate-{period}.zip"
            out.append(
                Target(
                    data_type="fundingRate",
                    symbol=symbol,
                    period=period,
                    url=f"{BASE_URL}/fundingRate/{symbol}/{name}",
                    path=DATASET_DIR / "fundingRate" / symbol / name,
                )
            )
            name = f"{symbol}-15m-{period}.zip"
            out.append(
                Target(
                    data_type="premiumIndexKlines15m",
                    symbol=symbol,
                    period=period,
                    url=f"{BASE_URL}/premiumIndexKlines/{symbol}/15m/{name}",
                    path=DATASET_DIR / "premiumIndexKlines15m" / symbol / name,
                )
            )
    return out


def _get(url: str, timeout: float = 60.0) -> bytes | None:
    """GET a URL, returning None on a definitive 404 (absent month)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code in (403, 404):
                return None
            last = error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last = error
        time.sleep(BACKOFF_SECONDS * (attempt + 1))
    raise RuntimeError(f"GET failed after {RETRIES} attempts: {url}") from last


def parse_checksum(blob: bytes) -> str | None:
    """The digest from a `sha256sum`-style sidecar."""
    text = blob.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    token = text.split()[0].lower()
    return token if len(token) == 64 and all(c in "0123456789abcdef" for c in token) else None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def acquire_one(target: Target) -> Record:
    """Acquire one file idempotently. Returns its manifest row."""
    record = Record(
        data_type=target.data_type,
        symbol=target.symbol,
        spot_symbol=SYMBOL_MAP[target.symbol],
        period=target.period,
        source_url=target.url,
        local_path=str(target.path),
        size_bytes=None,
        provider_sha256=None,
        local_sha256=None,
        retrieved_at=None,
        status="pending",
    )

    checksum_blob = _get(target.checksum_url, timeout=30.0)
    if checksum_blob is None:
        # No sidecar means no file for this month. Absent, not failed.
        record.status = "absent"
        return record
    provider = parse_checksum(checksum_blob)
    record.provider_sha256 = provider
    if provider is None:
        record.status = "checksum-unparseable"
        return record

    if target.path.exists():
        local = sha256_of(target.path)
        if local == provider:
            record.local_sha256 = local
            record.size_bytes = target.path.stat().st_size
            record.retrieved_at = datetime.fromtimestamp(
                target.path.stat().st_mtime, tz=UTC
            ).isoformat()
            record.status = "skipped-valid"
            return record
        # Present but wrong: never trusted, never appended to.
        target.path.unlink()

    target.path.parent.mkdir(parents=True, exist_ok=True)
    part = target.path.with_suffix(target.path.suffix + ".part")
    if part.exists():
        part.unlink()

    body = _get(target.url)
    if body is None:
        record.status = "absent-body"
        return record
    part.write_bytes(body)
    local = sha256_of(part)
    record.local_sha256 = local
    if local != provider:
        part.unlink()
        record.status = "checksum-mismatch"
        return record

    os.replace(part, target.path)
    record.size_bytes = target.path.stat().st_size
    record.retrieved_at = datetime.now(tz=UTC).isoformat()
    record.status = "downloaded"
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default="/Volumes/AUTOTRADER_QA/reports/crypto-funding-basis-pilot/acquisition_log.json",
    )
    parser.add_argument("--limit", type=int, default=0, help="stop after N targets (probe use)")
    args = parser.parse_args()

    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    all_targets = targets()
    if args.limit:
        all_targets = all_targets[: args.limit]

    records: list[Record] = []
    started = time.time()
    for index, target in enumerate(all_targets, start=1):
        record = acquire_one(target)
        records.append(record)
        if index % 25 == 0 or record.status not in ("skipped-valid", "downloaded", "absent"):
            print(
                f"[{index}/{len(all_targets)}] {target.data_type} {target.symbol} "
                f"{target.period} -> {record.status}",
                flush=True,
            )

    manifest = Path(args.manifest)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "base_url": BASE_URL,
        "symbol_map": SYMBOL_MAP,
        "span": {
            "first_month": f"{FIRST_MONTH[0]:04d}-{FIRST_MONTH[1]:02d}",
            "last_month": f"{LAST_MONTH[0]:04d}-{LAST_MONTH[1]:02d}",
        },
        "elapsed_seconds": round(time.time() - started, 1),
        "files": [asdict(r) for r in records],
    }
    tmp = manifest.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, manifest)

    counts: dict[str, int] = {}
    for record in records:
        counts[record.status] = counts.get(record.status, 0) + 1
    total = sum(r.size_bytes or 0 for r in records)
    print(f"acquisition complete in {payload['elapsed_seconds']}s")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")
    print(f"  total bytes: {total} ({total / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
