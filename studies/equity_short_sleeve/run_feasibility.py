"""Phase-1 runner: short feasibility audit (ledger §L0.2, §L8).

**Read-only.** This module makes exactly one kind of network call: the
broker's asset-metadata GET, one symbol at a time. It cannot submit an
order — no order model, no submission function, and no position call is
imported or referenced anywhere in this file. It writes one JSON artifact.

What the audit can and cannot establish is the point of it: the broker
publishes `shortable` and `easy_to_borrow` for *right now* and publishes no
history of either, so this run produces CURRENT OPERATIONAL INFORMATION and
proves that the corresponding HISTORICAL information is UNAVAILABLE.

Usage:
    python -m studies.equity_short_sleeve.run_feasibility
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from studies.equity_deep_arch.evaluate import write_json
from studies.equity_short_sleeve import REPORT_ROOT

FIELDS = (
    "symbol",
    "exchange",
    "status",
    "tradable",
    "marginable",
    "shortable",
    "easy_to_borrow",
    "fractionable",
)


def _log(message: str) -> None:
    print(message, flush=True)


def _value(raw: object) -> object:
    from enum import Enum

    if isinstance(raw, Enum):
        return raw.value
    if raw is None or isinstance(raw, (bool, int, float, str)):
        return raw
    return str(raw)


def audit(symbols: list[str]) -> dict[str, object]:
    """One metadata GET per symbol. No order path is reachable from here."""
    from autotrader.execution.paper import create_paper_trading_client

    client = create_paper_trading_client()
    rows: list[dict[str, object]] = []
    for symbol in symbols:
        asset = client.get_asset(symbol)
        rows.append({field: _value(getattr(asset, field, None)) for field in FIELDS})
        _log(
            f"  {symbol}: shortable={rows[-1]['shortable']} "
            f"etb={rows[-1]['easy_to_borrow']} fractionable={rows[-1]['fractionable']}"
        )
        time.sleep(0.15)
    return {
        "read_at_utc": datetime.now(UTC).isoformat(),
        "endpoint": "paper trading assets metadata (read-only GET)",
        "orders_submitted": 0,
        "positions_read": False,
        "fields": list(FIELDS),
        "assets": rows,
    }


def main() -> None:
    manifest = json.loads(
        (
            Path("/Volumes/AUTOTRADER_QA/reports/equity-eda1-next-generation")
            / "phase2"
            / "universe_manifest.json"
        ).read_text()
    )
    symbols = sorted(manifest["manifests"]["u30"])
    _log(f"auditing {len(symbols)} U30 symbols (read-only metadata)")
    payload = audit(symbols)
    shortable = [r["symbol"] for r in payload["assets"] if r["shortable"]]
    etb = [r["symbol"] for r in payload["assets"] if r["easy_to_borrow"]]
    payload["summary"] = {
        "universe": symbols,
        "count": len(symbols),
        "shortable_count": len(shortable),
        "easy_to_borrow_count": len(etb),
        "not_shortable": [s for s in symbols if s not in shortable],
        "not_easy_to_borrow": [s for s in symbols if s not in etb],
    }
    write_json(Path(REPORT_ROOT) / "feasibility" / "asset_metadata.json", payload)
    _log(f"shortable {len(shortable)}/{len(symbols)}; easy-to-borrow {len(etb)}/{len(symbols)}")


if __name__ == "__main__":
    main()
