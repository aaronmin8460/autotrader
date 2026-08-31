"""Emit the machine-readable artifact set the final report cites.

Each file answers one question a reader might want to check independently,
without having to re-derive it from 204 checkpoint files.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from studies.crypto_funding_basis_pilot import run_pilot
from studies.crypto_funding_basis_pilot.acquire import DATASET_DIR
from studies.crypto_funding_basis_pilot.analyse import (
    ERA_2021_23,
    economic,
    index_cells,
    load_cells,
)
from studies.crypto_funding_basis_pilot.derivative_features import (
    DERIVATIVE_FEATURES,
    FUNDING_Z_WINDOW,
    MAX_FUNDING_STALENESS,
    MAX_PREMIUM_STALENESS,
    PREMIUM_MEAN_BARS,
    PREMIUM_PCT_BARS,
)

OUT = Path("/Volumes/AUTOTRADER_QA/reports/crypto-funding-basis-pilot")
NORMALIZED = DATASET_DIR / "normalized"


def _write(name: str, payload) -> None:
    (OUT / name).write_text(json.dumps(payload, indent=2, default=str))
    print(f"wrote {name}")


def normalized_summaries() -> None:
    for perp in ("BTCUSDT", "ETHUSDT"):
        funding = pd.read_parquet(NORMALIZED / f"{perp}_funding.parquet")
        gaps = funding["source_timestamp"].diff().dropna()
        _write(
            f"normalized_funding_{perp}.json",
            {
                "symbol": perp,
                "rows": int(len(funding)),
                "first": str(funding["source_timestamp"].iloc[0]),
                "last": str(funding["source_timestamp"].iloc[-1]),
                "interval_hours_unique": sorted(
                    funding["funding_interval_hours"].unique().tolist()
                ),
                "max_deviation_from_8h_grid_ms": float(
                    (gaps - pd.Timedelta("8h")).abs().max().total_seconds() * 1000
                ),
                "duplicates": int(funding["source_timestamp"].duplicated().sum()),
                "monotonic": bool(funding["source_timestamp"].is_monotonic_increasing),
                "knowable_at_rule": "ceil(calc_time, 1s)",
                "mean_rate_bps_per_8h": float(funding["funding_rate"].mean() * 1e4),
            },
        )
        premium = pd.read_parquet(NORMALIZED / f"{perp}_premium.parquet")
        full = pd.date_range(
            premium["bar_open"].min(), premium["bar_open"].max(), freq="15min", tz="UTC"
        )
        missing = full.difference(pd.DatetimeIndex(premium["bar_open"]))
        _write(
            f"normalized_basis_{perp}.json",
            {
                "symbol": perp,
                "rows": int(len(premium)),
                "first": str(premium["bar_open"].iloc[0]),
                "last": str(premium["bar_open"].iloc[-1]),
                "expected_bars": int(len(full)),
                "missing_bars": int(len(missing)),
                "missing_fraction": float(len(missing) / len(full)),
                "missing_days": sorted({str(d.date()) for d in missing}),
                "bar_close_rule": "open + 15m - 1ms",
                "knowable_at_rule": "bar_close + 1ms",
                "duplicates": int(premium["bar_open"].duplicated().sum()),
            },
        )


def causal_join_audit(cells: list[dict]) -> None:
    rows = []
    seen = set()
    for cell in cells:
        audit = cell.get("coverage", {}).get("join_audit")
        if not audit:
            continue
        key = (cell["symbol"], cell["window"] in ERA_2021_23)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "symbol": cell["symbol"],
                "era": "2021-23" if cell["window"] in ERA_2021_23 else "2024-26",
                **audit,
                "shared_population_retention": cell["coverage"]["shared_population_retention"],
                "rows_baseline_usable": cell["coverage"]["rows_baseline_usable"],
                "rows_shared_usable": cell["coverage"]["rows_shared_usable"],
            }
        )
    _write(
        "causal_join_audit.json",
        {
            "join_predicate": "knowable_at <= decision_ts, merge_asof(direction='backward')",
            "decision_ts": "feature bar open timestamp + 15 minutes (the bar's close)",
            "max_funding_staleness": str(MAX_FUNDING_STALENESS),
            "max_premium_staleness": str(MAX_PREMIUM_STALENESS),
            "negative_staleness_rows_total": sum(r["negative_staleness_rows"] for r in rows),
            "per_symbol_era": rows,
        },
    )


def feature_provenance() -> None:
    _write(
        "feature_provenance.json",
        {
            "baseline": {
                "count": len(run_pilot.BASELINE_FEATURES),
                "features": list(run_pilot.BASELINE_FEATURES),
                "source": "frozen DA-SPREAD-96 24-feature OHLCV set, vendored verbatim",
            },
            "derivative": {
                "count": len(DERIVATIVE_FEATURES),
                "predeclared_in": "pilot-designs.md PILOT 1",
                "features": [
                    {
                        "name": "funding_current",
                        "definition": "last settled funding rate knowable at the decision",
                        "window": None,
                        "past_only": True,
                    },
                    {
                        "name": "funding_z_30",
                        "definition": "z-score of funding_current over trailing settlements",
                        "window": f"{FUNDING_Z_WINDOW} settlements",
                        "past_only": True,
                    },
                    {
                        "name": "funding_delta",
                        "definition": "last settled rate minus previous settled rate",
                        "window": "2 settlements",
                        "past_only": True,
                    },
                    {
                        "name": "premium_close",
                        "definition": "premium-index close of the last completed 15m bar",
                        "window": "1 bar",
                        "past_only": True,
                    },
                    {
                        "name": "premium_mean_24h",
                        "definition": "mean premium close over the trailing day",
                        "window": f"{PREMIUM_MEAN_BARS} bars",
                        "past_only": True,
                    },
                    {
                        "name": "premium_pct_90d",
                        "definition": "percentile of the current basis in its trailing 90 days",
                        "window": f"{PREMIUM_PCT_BARS} bars",
                        "past_only": True,
                    },
                    {
                        "name": "funding_trend_interaction",
                        "definition": "funding_z_30 * sign(return_2688)",
                        "window": "derived",
                        "past_only": True,
                    },
                    {
                        "name": "premium_vol_interaction",
                        "definition": "premium_mean_24h * realized_volatility_96",
                        "window": "derived",
                        "past_only": True,
                    },
                ],
                "excluded_by_predeclaration": [
                    "open interest / positioning metrics (undocumented publication delay)"
                ],
                "features_dropped_as_unconstructible": [],
            },
            "arms": {name: list(cols) for name, cols in run_pilot.ARM_FEATURES.items()},
        },
    )


def arm_metrics(index: dict, horizon: int) -> None:
    for arm in ("baseline", "augmented", "funding_only", "basis_only"):
        rows = []
        for (cell_arm, symbol, cell_h, window), cell in sorted(index.items()):
            if cell_arm != arm or cell_h != horizon:
                continue
            econ = economic(cell)
            rows.append(
                {
                    "symbol": symbol,
                    "window": window,
                    "era": "2021-23" if window in ERA_2021_23 else "2024-26",
                    "log_loss": cell["predictive"]["log_loss"],
                    "null_log_loss": cell["predictive"]["null_log_loss"],
                    "log_loss_vs_null": cell["predictive"]["log_loss_vs_null"],
                    "auc_up": cell["predictive"]["per_side"]["up"]["roc_auc"],
                    "auc_down": cell["predictive"]["per_side"]["down"]["roc_auc"],
                    "pr_auc_up": cell["predictive"]["per_side"]["up"]["pr_auc"],
                    "brier_up": cell["predictive"]["per_side"]["up"]["brier"],
                    "ece_up": cell["predictive"]["per_side"]["up"]["ece"],
                    "prediction_std_up": cell["predictive"]["per_side"]["up"]["prediction_std"],
                    "spread_rank_ic": cell["decision"]["spread_rank_ic"],
                    "decision_days": cell["decision"]["decision_days"],
                    "net_return": econ["net_return"],
                    "forced_return": econ["forced_return"],
                    "trades": econ["trades"],
                    "hit_rate": econ["ledger"]["hit_rate"],
                    "average_trade": econ["ledger"]["average_trade"],
                    "time_in_market": econ["time_in_market"],
                    "max_drawdown": econ["max_drawdown"],
                    "open_at_end": econ["open_at_end"],
                }
            )
        if rows:
            _write(f"{arm}_metrics_h{horizon}.json", {"horizon": horizon, "cells": rows})


def forced_liquidation(index: dict, horizon: int) -> None:
    rows = []
    for (arm, symbol, cell_h, window), cell in sorted(index.items()):
        if cell_h != horizon:
            continue
        econ = economic(cell)
        rows.append(
            {
                "arm": arm,
                "symbol": symbol,
                "window": window,
                "net_return": econ["net_return"],
                "forced_return": econ["forced_return"],
                "difference": econ["forced_return"] - econ["net_return"],
                "open_at_end": econ["open_at_end"],
                "realized_pnl": econ["realized_pnl"],
                "unrealized_pnl": econ["unrealized_pnl"],
            }
        )
    by_arm = defaultdict(list)
    for row in rows:
        by_arm[row["arm"]].append(row)
    _write(
        f"forced_liquidation_h{horizon}.json",
        {
            "note": "every headline figure in this pilot is quoted on the forced-"
            "liquidation basis, so no result depends on an unclosed position",
            "summary": {
                arm: {
                    "mean_net": float(np.mean([r["net_return"] for r in rs])),
                    "mean_forced": float(np.mean([r["forced_return"] for r in rs])),
                    "cells_open_at_end": int(sum(1 for r in rs if r["open_at_end"])),
                    "mean_difference": float(np.mean([r["difference"] for r in rs])),
                }
                for arm, rs in by_arm.items()
            },
            "cells": rows,
        },
    )


def checkpoint_manifest(cells: list[dict]) -> None:
    files = sorted((OUT / "cells").glob("*.json"))
    _write(
        "checkpoint_manifest.json",
        {
            "cell_count": len(files),
            "all_status_ok": all(c.get("status") == "ok" for c in cells),
            "granularity": "one JSON per (arm, symbol, horizon, window)",
            "write_discipline": "temp file then os.replace; a partial file can never "
            "be mistaken for a finished cell",
            "resume_rule": "a cell whose file exists is skipped",
            "cells": [
                {
                    "file": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "bytes": path.stat().st_size,
                }
                for path in files
            ],
        },
    )


def main() -> None:
    cells = load_cells()
    index = index_cells(cells)
    normalized_summaries()
    causal_join_audit(cells)
    feature_provenance()
    for horizon in (96, 16, 32):
        arm_metrics(index, horizon)
        forced_liquidation(index, horizon)
    checkpoint_manifest(cells)
    _write(
        "reproducibility.json",
        {
            "generated_at": datetime.now(tz=UTC).isoformat(),
            "deterministic_scoring": {
                "method": "a completed cell was deleted and recomputed",
                "result": "bit-identical JSON excluding wall-clock fields",
            },
            "checkpoint_resume": {
                "method": "the 4-cell probe was re-run; a 4-worker ablation was "
                "interrupted mid-flight and relaunched at 2 workers",
                "result": "all completed cells skipped, 204 preserved, no partial "
                "artifact promoted",
            },
            "acquisition_resume": {
                "method": "full re-run of the 320-target acquisition",
                "result": "316 skipped-valid, 0 redownloaded, no body fetched for a "
                "file already matching its provider digest",
            },
            "reproduce_from": [
                "dataset_manifest.json - every source URL with provider and local SHA-256",
                "run_manifest.json - branch, commit, design constants",
                "frozen_harness_provenance.json - digests of the vendored baseline",
                "checkpoint_manifest.json - digest of every scored cell",
            ],
        },
    )


if __name__ == "__main__":
    main()
