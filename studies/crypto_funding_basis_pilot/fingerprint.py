"""One manifest from which the whole pilot is reproducible.

Combines every provenance record the study produces - the monthly
acquisition, the daily backfill, the normalised outputs, and the OHLCV
datasets the baseline arm reads - into a single fingerprint file: source URL,
size, provider checksum, locally computed SHA-256, row counts and date ranges.

Reproducibility here means: given this file, another machine can re-fetch the
same bytes, verify them against both the provider's digest and ours, rebuild
the normalised frames, and confirm the output hashes match.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from studies.crypto_funding_basis_pilot.acquire import DATASET_DIR, SYMBOL_MAP
from studies.crypto_funding_basis_pilot.frozen_data import DATASET_DIR as OHLCV_DIR
from studies.crypto_funding_basis_pilot.frozen_data import DATASET_FILES, EXTENDED_DIR

OUTPUT_DIR = Path("/Volumes/AUTOTRADER_QA/reports/crypto-funding-basis-pilot")
NORMALIZED_DIR = DATASET_DIR / "normalized"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def derivative_sources() -> list[dict]:
    """Every acquired dump file, with both digests and its provenance."""
    acquisition = _read(OUTPUT_DIR / "acquisition_log.json").get("files", [])
    backfill = _read(OUTPUT_DIR / "backfill_log.json").get("files", [])
    rows: list[dict] = []
    for record in list(acquisition) + list(backfill):
        if record.get("status") not in ("downloaded", "skipped-valid"):
            continue
        rows.append(
            {
                "data_type": record["data_type"],
                "symbol": record["symbol"],
                "spot_symbol": record.get("spot_symbol"),
                "period": record["period"],
                "source_url": record["source_url"],
                "size_bytes": record["size_bytes"],
                "provider_sha256": record["provider_sha256"],
                "local_sha256": record["local_sha256"],
                "provider_digest_verified": record["provider_sha256"] == record["local_sha256"],
                "retrieved_at": record["retrieved_at"],
                "status": record["status"],
            }
        )
    return rows


def normalized_outputs() -> list[dict]:
    rows = []
    for perp in SYMBOL_MAP:
        for kind, time_column in (("funding", "source_timestamp"), ("premium", "bar_open")):
            path = NORMALIZED_DIR / f"{perp}_{kind}.parquet"
            if not path.exists():
                continue
            frame = pd.read_parquet(path)
            rows.append(
                {
                    "symbol": perp,
                    "spot_symbol": SYMBOL_MAP[perp],
                    "kind": kind,
                    "path": str(path),
                    "rows": int(len(frame)),
                    "first": str(frame[time_column].iloc[0]),
                    "last": str(frame[time_column].iloc[-1]),
                    "size_bytes": path.stat().st_size,
                    "output_sha256": sha256_of(path),
                }
            )
    return rows


def ohlcv_datasets() -> list[dict]:
    """The OHLCV parquets the baseline arm reads, with their recorded digests."""
    rows = []
    for symbol, (filename, expected) in DATASET_FILES.items():
        path = OHLCV_DIR / filename
        if path.exists():
            rows.append(
                {
                    "symbol": symbol,
                    "era": "2024-26",
                    "path": str(path),
                    "recorded_sha256": expected,
                    "observed_sha256": sha256_of(path),
                    "size_bytes": path.stat().st_size,
                }
            )
    for symbol in DATASET_FILES:
        slug = symbol.replace("/", "_")
        path = EXTENDED_DIR / f"{slug}_15m_2021-01-01_2023-12-31.parquet"
        sidecar = EXTENDED_DIR / f"{slug}_15m_2021-01-01_2023-12-31.metadata.json"
        if path.exists() and sidecar.exists():
            rows.append(
                {
                    "symbol": symbol,
                    "era": "2021-23",
                    "path": str(path),
                    "recorded_sha256": json.loads(sidecar.read_text())["sha256"],
                    "observed_sha256": sha256_of(path),
                    "size_bytes": path.stat().st_size,
                }
            )
    for row in rows:
        row["digest_verified"] = row["recorded_sha256"] == row["observed_sha256"]
    return rows


def main() -> None:
    sources = derivative_sources()
    outputs = normalized_outputs()
    ohlcv = ohlcv_datasets()
    payload = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "symbol_map": SYMBOL_MAP,
        "derivative_source_files": {
            "count": len(sources),
            "total_bytes": sum(r["size_bytes"] or 0 for r in sources),
            "all_provider_digests_verified": all(r["provider_digest_verified"] for r in sources),
            "files": sources,
        },
        "normalized_outputs": outputs,
        "ohlcv_datasets": {
            "all_digests_verified": all(r["digest_verified"] for r in ohlcv),
            "files": ohlcv,
        },
        "frozen_harness": _read(OUTPUT_DIR / "frozen_harness_provenance.json"),
    }
    path = OUTPUT_DIR / "dataset_manifest.json"
    path.write_text(json.dumps(payload, indent=2))
    print(
        f"derivative files : {len(sources)} "
        f"({payload['derivative_source_files']['total_bytes'] / 1e6:.1f} MB), "
        f"all provider digests verified: "
        f"{payload['derivative_source_files']['all_provider_digests_verified']}"
    )
    for row in outputs:
        print(
            f"  {row['symbol']:8s} {row['kind']:8s} {row['rows']:>7} rows  "
            f"{row['first']} .. {row['last']}"
        )
    print(f"OHLCV digests verified: {payload['ohlcv_datasets']['all_digests_verified']}")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
