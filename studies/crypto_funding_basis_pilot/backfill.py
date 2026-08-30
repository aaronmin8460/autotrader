"""Close monthly-archive holes with the venue's own daily premium files.

The monthly `premiumIndexKlines` archives omit eleven whole days that the
*daily* archives for the same product publish normally - the gap is an
artefact of how the monthly files were assembled, not a venue outage. Left
alone it would trip the pilot's predeclared "premium bars missing > 0.5% in
any quarter" rejection trigger on four scored quarters.

This module finds the holes from the normalised series rather than from a
hard-coded list, fetches only the missing days, and reuses the same
checksum-verified, atomic, resumable acquisition path as the monthly pull.

Scope is deliberately limited to the span the monthly archives cover
(2020-01 → 2026-07). 2026-08 is **not** backfilled: the venue publishes no
daily funding product at all, so an August premium series with no August
funding could not produce a single usable augmented row, and extending one
leg of the pair would only blur the coverage story.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from studies.crypto_funding_basis_pilot.acquire import (
    BASE_URL,
    DATASET_DIR,
    SYMBOL_MAP,
    Target,
    acquire_one,
)

DAILY_BASE_URL = BASE_URL.replace("/monthly", "/daily")

#: Directory for daily premium files, kept separate from the monthly pull so
#: the manifest can always say which archive each row came from.
DAILY_KIND = "premiumIndexKlines15m_daily"

#: The monthly archives' span. Holes outside it are not backfilled.
BACKFILL_FIRST = pd.Timestamp("2020-01-01", tz="UTC")
BACKFILL_LAST = pd.Timestamp("2026-07-31", tz="UTC")


def missing_days(premium: pd.DataFrame) -> list[str]:
    """Whole and partial days absent from a normalised premium series."""
    bar_open = pd.DatetimeIndex(premium["bar_open"])
    full = pd.date_range(bar_open.min(), bar_open.max(), freq="15min", tz="UTC")
    absent = full.difference(bar_open)
    if absent.empty:
        return []
    days = sorted({d.date() for d in absent})
    return [d.isoformat() for d in days if BACKFILL_FIRST.date() <= d <= BACKFILL_LAST.date()]


def daily_target(symbol: str, day: str) -> Target:
    name = f"{symbol}-15m-{day}.zip"
    return Target(
        data_type=DAILY_KIND,
        symbol=symbol,
        period=day,
        url=f"{DAILY_BASE_URL}/premiumIndexKlines/{symbol}/15m/{name}",
        path=DATASET_DIR / DAILY_KIND / symbol / name,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default="/Volumes/AUTOTRADER_QA/reports/crypto-funding-basis-pilot/backfill_log.json",
    )
    args = parser.parse_args()

    normalized_dir = DATASET_DIR / "normalized"
    records = []
    for symbol in SYMBOL_MAP:
        premium = pd.read_parquet(normalized_dir / f"{symbol}_premium.parquet")
        days = missing_days(premium)
        print(f"{symbol}: {len(days)} day(s) to backfill -> {days}", flush=True)
        for day in days:
            record = acquire_one(daily_target(symbol, day))
            records.append(record.__dict__ if hasattr(record, "__dict__") else record)
            print(f"  {symbol} {day} -> {record.status}", flush=True)

    manifest = Path(args.manifest)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "daily_base_url": DAILY_BASE_URL,
        "backfill_span": [str(BACKFILL_FIRST.date()), str(BACKFILL_LAST.date())],
        "files": records,
    }
    tmp = manifest.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, manifest)
    print(f"wrote {manifest}")


if __name__ == "__main__":
    main()
