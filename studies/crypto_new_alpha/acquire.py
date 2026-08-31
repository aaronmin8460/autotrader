"""Resumable, checksum-verified acquisition of the public derivative dumps.

Three datasets, predeclared by `search-ledger.md` §1:

* `klines15m` - USD-margined perpetual 15m klines (aggressor taker-buy fields),
  monthly 2020-01..2026-07 plus daily files for the archive-lag month(s)
* `metrics` - daily files of 5-minute open-interest snapshots
* `cmLiquidationSnapshot` - the narrow coin-margined liquidation record,
  acquired ONLY as proxy-validation evidence, never as a model feature source

The daily inventories (`metrics`, `cmLiquidationSnapshot`, daily klines) are
discovered from the archive's own S3 listing rather than assumed, so absent
days are recorded as absent instead of guessed at.

Resume discipline (identical to the funding-basis pilot, whose tests proved
it): a final file matching the provider `.CHECKSUM` is skipped without a body
fetch; downloads land in a `.part` promoted by `os.replace` only after the
SHA-256 matches; stale `.part` files are discarded, never appended to.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

BASE_URL = "https://data.binance.vision/data"
LISTING_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"

DATASET_DIR = Path("/Volumes/AUTOTRADER_QA/datasets/crypto-new-alpha/raw")

#: Derivative symbol -> spot decision stream. Recorded, never inferred later.
SYMBOL_MAP: dict[str, str] = {"BTCUSDT": "BTC/USD", "ETHUSDT": "ETH/USD"}

#: Coin-margined validation symbols -> spot stream (validation only).
CM_SYMBOL_MAP: dict[str, str] = {"BTCUSD_PERP": "BTC/USD", "ETHUSD_PERP": "ETH/USD"}

#: Predeclared kline monthly span. Months that 404 are recorded, not faked.
FIRST_MONTH = (2020, 1)
LAST_MONTH = (2026, 7)

#: Daily klines cover the monthly-archive lag; discovered from the listing.
DAILY_KLINES_FROM = "2026-08-01"

USER_AGENT = "autotrader-research/crypto-new-alpha (public dump reader)"
RETRIES = 4
BACKOFF_SECONDS = 2.0


def _ssl_context() -> ssl.SSLContext:
    """A verifying context that does not depend on the interpreter's own store."""
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


def _get(url: str, timeout: float = 60.0) -> bytes | None:
    """GET a URL, returning None on a definitive 404 (absent file)."""
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


def list_archive_dates(prefix: str) -> list[str]:
    """Every `YYYY-MM-DD` present under an archive prefix, via the S3 listing.

    Paginated with `marker`; the listing is the archive's own inventory, so
    the returned dates are what exists, not what a calendar predicts.
    """
    dates: list[str] = []
    marker = ""
    while True:
        url = f"{LISTING_URL}?delimiter=/&prefix={prefix}" + (f"&marker={marker}" if marker else "")
        blob = _get(url, timeout=45.0)
        if blob is None:
            raise RuntimeError(f"listing refused for prefix {prefix}")
        xml = blob.decode("utf-8", errors="replace")
        keys = re.findall(r"<Key>([^<]+\.zip)</Key>", xml)
        for key in keys:
            match = re.search(r"(\d{4}-\d{2}-\d{2})\.zip$", key)
            if match:
                dates.append(match.group(1))
        if "<IsTruncated>true" in xml and keys:
            marker = keys[-1] + ".CHECKSUM"
        else:
            break
    return sorted(set(dates))


def kline_targets() -> list[Target]:
    """Monthly 15m klines for the predeclared span, plus lag-month daily files."""
    out: list[Target] = []
    for symbol in SYMBOL_MAP:
        for period in months(FIRST_MONTH, LAST_MONTH):
            name = f"{symbol}-15m-{period}.zip"
            out.append(
                Target(
                    data_type="klines15m",
                    symbol=symbol,
                    period=period,
                    url=f"{BASE_URL}/futures/um/monthly/klines/{symbol}/15m/{name}",
                    path=DATASET_DIR / "klines" / symbol / name,
                )
            )
        prefix = f"data/futures/um/daily/klines/{symbol}/15m/"
        for day in list_archive_dates(prefix):
            if day < DAILY_KLINES_FROM:
                continue
            name = f"{symbol}-15m-{day}.zip"
            out.append(
                Target(
                    data_type="klines15mDaily",
                    symbol=symbol,
                    period=day,
                    url=f"{BASE_URL}/futures/um/daily/klines/{symbol}/15m/{name}",
                    path=DATASET_DIR / "klines" / symbol / name,
                )
            )
    return out


def metrics_targets() -> list[Target]:
    """Every daily metrics file the archive lists, both symbols."""
    out: list[Target] = []
    for symbol in SYMBOL_MAP:
        prefix = f"data/futures/um/daily/metrics/{symbol}/"
        for day in list_archive_dates(prefix):
            name = f"{symbol}-metrics-{day}.zip"
            out.append(
                Target(
                    data_type="metrics",
                    symbol=symbol,
                    period=day,
                    url=f"{BASE_URL}/futures/um/daily/metrics/{symbol}/{name}",
                    path=DATASET_DIR / "metrics" / symbol / name,
                )
            )
    return out


def cm_liquidation_targets() -> list[Target]:
    """The narrow coin-margined liquidation record (validation evidence only)."""
    out: list[Target] = []
    for symbol in CM_SYMBOL_MAP:
        prefix = f"data/futures/cm/daily/liquidationSnapshot/{symbol}/"
        for day in list_archive_dates(prefix):
            name = f"{symbol}-liquidationSnapshot-{day}.zip"
            out.append(
                Target(
                    data_type="cmLiquidationSnapshot",
                    symbol=symbol,
                    period=day,
                    url=f"{BASE_URL}/futures/cm/daily/liquidationSnapshot/{symbol}/{name}",
                    path=DATASET_DIR / "liquidations-cm" / symbol / name,
                )
            )
    return out


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


def _spot_of(symbol: str) -> str:
    return SYMBOL_MAP.get(symbol) or CM_SYMBOL_MAP.get(symbol) or "?"


def acquire_one(target: Target) -> Record:
    """Acquire one file idempotently. Returns its manifest row."""
    record = Record(
        data_type=target.data_type,
        symbol=target.symbol,
        spot_symbol=_spot_of(target.symbol),
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
        default="/Volumes/AUTOTRADER_QA/reports/crypto-new-alpha-oi-liq-flow/acquisition_log.json",
    )
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0, help="stop after N targets (probe use)")
    parser.add_argument(
        "--datasets",
        default="klines,metrics,cmliq",
        help="comma list from klines,metrics,cmliq",
    )
    args = parser.parse_args()

    wanted = {token.strip() for token in args.datasets.split(",") if token.strip()}
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    all_targets: list[Target] = []
    if "klines" in wanted:
        all_targets += kline_targets()
    if "metrics" in wanted:
        all_targets += metrics_targets()
    if "cmliq" in wanted:
        all_targets += cm_liquidation_targets()
    if args.limit:
        all_targets = all_targets[: args.limit]

    records: list[Record] = []
    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for index, record in enumerate(pool.map(acquire_one, all_targets), start=1):
            records.append(record)
            noisy = record.status not in ("skipped-valid", "downloaded", "absent")
            if index % 200 == 0 or noisy:
                print(
                    f"[{index}/{len(all_targets)}] {record.data_type} {record.symbol} "
                    f"{record.period} -> {record.status}",
                    flush=True,
                )

    manifest = Path(args.manifest)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "base_url": BASE_URL,
        "symbol_map": SYMBOL_MAP,
        "cm_symbol_map": CM_SYMBOL_MAP,
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
