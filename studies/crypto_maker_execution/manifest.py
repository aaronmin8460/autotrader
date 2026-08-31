"""Run and market-data manifests for reproducibility."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

from studies.crypto_maker_execution.acquire import cache_root
from studies.crypto_maker_execution.analysis import report_root
from studies.crypto_maker_execution.bars import EXPECTED_SHA256
from studies.crypto_maker_execution.schedule import events_for
from studies.crypto_maker_execution.simulator import POLICIES, SCENARIOS


def market_data_manifest() -> dict:
    windows = []
    for sidecar in sorted(cache_root().rglob("*.provenance.json")):
        record = json.loads(sidecar.read_text())
        record["cache_file"] = sidecar.name.replace(".provenance.json", ".json.gz")
        windows.append(record)
    return {
        "tick_cache_root": str(cache_root()),
        "window_count": len(windows),
        "total_quote_rows": sum(w["quote_rows"] for w in windows),
        "total_trade_rows": sum(w["trade_rows"] for w in windows),
        "reference_bars": EXPECTED_SHA256,
        "windows": windows,
    }


def run_manifest(mode: str) -> dict:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "branch": branch,
        "commit": head,
        "baseline": (
            "integration/final-development-candidate @ aee7a77af090fd9d3dd60f66c400fa2360f2f478"
        ),
        "mode": mode,
        "policies": {
            name: {
                "max_wait_s": policy.max_wait_s,
                "price_improve_ticks": policy.price_improve_ticks,
                "taker_fallback": policy.taker_fallback,
            }
            for name, policy in POLICIES.items()
        },
        "scenarios": {
            name: {
                "latency_s": scenario.latency_s,
                "fill_rule": scenario.fill_rule,
                "size_cap_fraction": scenario.size_cap_fraction,
            }
            for name, scenario in SCENARIOS.items()
        },
        "events_scheduled": {
            symbol: len(events_for(symbol, pilot=mode == "pilot"))
            for symbol in ("BTC/USD", "ETH/USD")
        },
        "notionals_usd": [10_000.0, 1_000.0],
    }


def write_manifests(mode: str) -> None:
    root = report_root()
    (root / "market_data_manifest.json").write_text(json.dumps(market_data_manifest(), indent=1))
    (root / "run_manifest.json").write_text(json.dumps(run_manifest(mode), indent=1))
    print("manifests written")


if __name__ == "__main__":
    import sys

    write_manifests(sys.argv[1] if len(sys.argv) > 1 else "full")
