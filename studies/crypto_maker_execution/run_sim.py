"""Checkpointed simulation runner.

Durable unit: (symbol, quarter, mode). Each completed unit is one JSON
file written atomically (tmp + rename); a re-run skips completed units, so
interruption loses at most the unit in flight and can never double-count.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from studies.crypto_maker_execution.accounting import account_all_policies
from studies.crypto_maker_execution.acquire import fetch_window
from studies.crypto_maker_execution.schedule import events_for


def results_root() -> Path:
    qa = os.environ.get("AUTOTRADER_QA", "/Volumes/AUTOTRADER_QA")
    return Path(qa) / "reports" / "crypto-maker-execution" / "sim_results"


def checkpoint_path(symbol: str, quarter: str, mode: str) -> Path:
    slug = symbol.replace("/", "_")
    return results_root() / mode / f"{slug}__{quarter}.json"


def run_unit(symbol: str, quarter: str, mode: str) -> dict:
    """Simulate every scheduled event of one symbol-quarter; idempotent."""
    path = checkpoint_path(symbol, quarter, mode)
    if path.exists():
        return json.loads(path.read_text())
    pilot = mode == "pilot"
    events = [event for event in events_for(symbol, pilot=pilot) if event.quarter == quarter]
    records = []
    for event in events:
        window = fetch_window(symbol, event.decision_ts)
        records.extend(
            account_all_policies(
                symbol=symbol,
                decision_ts=window.decision_ts,
                quarter=quarter,
                quotes=window.quotes,
                trades=window.trades,
            )
        )
    unit = {
        "symbol": symbol,
        "quarter": quarter,
        "mode": mode,
        "event_count": len(events),
        "records": records,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(unit))
    os.replace(tmp, path)
    return unit


def load_all(mode: str) -> list[dict]:
    records: list[dict] = []
    directory = results_root() / mode
    if not directory.exists():
        return records
    for path in sorted(directory.glob("*.json")):
        records.extend(json.loads(path.read_text())["records"])
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("pilot", "full"), required=True)
    parser.add_argument("--symbols", nargs="*", default=["BTC/USD", "ETH/USD"])
    parser.add_argument("--quarters", nargs="*", default=None)
    args = parser.parse_args()

    for symbol in args.symbols:
        wanted = args.quarters
        seen: dict[str, int] = defaultdict(int)
        for event in events_for(symbol, pilot=args.mode == "pilot"):
            seen[event.quarter] += 1
        for quarter in sorted(seen):
            if wanted and quarter not in wanted:
                continue
            done = checkpoint_path(symbol, quarter, args.mode).exists()
            unit = run_unit(symbol, quarter, args.mode)
            ok = sum(1 for r in unit["records"] if r.get("status") == "OK")
            print(
                f"{symbol} {quarter} [{args.mode}] events={unit['event_count']} "
                f"records={len(unit['records'])} ok={ok}"
                f"{' (cached)' if done else ''}",
                flush=True,
            )


if __name__ == "__main__":
    sys.exit(main())
