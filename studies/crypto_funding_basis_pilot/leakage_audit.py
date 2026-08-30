"""Future-perturbation causality audit on the real acquired series.

The unit tests prove causality on constructed inputs. This module proves it on
the actual BTC and ETH funding and premium archives joined to the real 15m
decision grid, and writes the artifact the final report cites.

Method, per symbol and per stream: pick a cut decision instant, corrupt every
observation whose `knowable_at` falls strictly after it, rebuild the eight
features, and require every feature value at or before the cut to be
bit-identical. Each probe additionally asserts the corruption *did* change
something after the cut - a probe that perturbs a region no feature reads
would pass while proving nothing.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from studies.crypto_funding_basis_pilot.acquire import DATASET_DIR, SYMBOL_MAP
from studies.crypto_funding_basis_pilot.derivative_features import (
    DERIVATIVE_FEATURES,
    join_derivative_features,
)

NORMALIZED_DIR = DATASET_DIR / "normalized"
OUTPUT_DIR = Path("/Volumes/AUTOTRADER_QA/reports/crypto-funding-basis-pilot")

#: Cut instants spread across the regime span, so the audit is not a single
#: lucky point. Each is a 15m decision boundary.
CUT_POINTS = ("2021-06-15 12:00", "2022-06-15 12:00", "2024-03-15 12:00", "2026-03-15 12:00")


def _grid(start: str, end: str) -> pd.Series:
    return pd.Series(pd.date_range(start, end, freq="15min", tz="UTC"))


def _build(timestamps, funding, premium, seed=0):
    rng = np.random.default_rng(seed)
    n = len(timestamps)
    # The two interaction features need an OHLCV trend and volatility input.
    # Their values are irrelevant to causality as long as they are held fixed
    # across the perturbed and unperturbed builds, which they are.
    return join_derivative_features(
        timestamps,
        funding,
        premium,
        return_2688=pd.Series(rng.normal(0, 0.05, n)),
        realized_volatility_96=pd.Series(np.abs(rng.normal(0.004, 0.001, n))),
    )


def audit_symbol(perp: str) -> list[dict]:
    funding = pd.read_parquet(NORMALIZED_DIR / f"{perp}_funding.parquet")
    premium = pd.read_parquet(NORMALIZED_DIR / f"{perp}_premium.parquet")
    results: list[dict] = []

    for cut in CUT_POINTS:
        cut_ts = pd.Timestamp(cut, tz="UTC")
        timestamps = _grid(str(cut_ts - pd.Timedelta("40D")), str(cut_ts + pd.Timedelta("10D")))
        base, _ = _build(timestamps, funding, premium)
        # The decision instant of feature bar T is T + 15m.
        decision = timestamps + pd.Timedelta("15min")
        at_or_before = (decision <= cut_ts).to_numpy()

        for stream in ("funding", "premium"):
            altered_f, altered_p = funding.copy(), premium.copy()
            if stream == "funding":
                future = altered_f["knowable_at"] > cut_ts
                altered_f.loc[future, "funding_rate"] = (
                    altered_f.loc[future, "funding_rate"] * -50.0 + 7.0
                )
            else:
                future = altered_p["knowable_at"] > cut_ts
                altered_p.loc[future, "premium_close"] = (
                    altered_p.loc[future, "premium_close"] * -50.0 + 7.0
                )
            perturbed, _ = _build(timestamps, altered_f, altered_p)

            identical = {}
            for name in DERIVATIVE_FEATURES:
                before_a = base[name].to_numpy()[at_or_before]
                before_b = perturbed[name].to_numpy()[at_or_before]
                identical[name] = bool(np.array_equal(before_a, before_b, equal_nan=True))
            moved_after = any(
                not np.array_equal(
                    base[name].to_numpy()[~at_or_before],
                    perturbed[name].to_numpy()[~at_or_before],
                    equal_nan=True,
                )
                for name in DERIVATIVE_FEATURES
            )
            results.append(
                {
                    "symbol": perp,
                    "spot_symbol": SYMBOL_MAP[perp],
                    "stream": stream,
                    "cut_decision_ts": str(cut_ts),
                    "rows_at_or_before_cut": int(at_or_before.sum()),
                    "rows_after_cut": int((~at_or_before).sum()),
                    "observations_perturbed": int(future.sum()),
                    "all_prior_features_bit_identical": all(identical.values()),
                    "per_feature_identical": identical,
                    "probe_has_teeth": moved_after,
                }
            )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(OUTPUT_DIR / "leakage_audit.json"))
    args = parser.parse_args()

    results: list[dict] = []
    for perp in SYMBOL_MAP:
        results.extend(audit_symbol(perp))

    clean = all(r["all_prior_features_bit_identical"] for r in results)
    toothed = all(r["probe_has_teeth"] for r in results)
    payload = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "probes": len(results),
        "all_probes_leakage_free": clean,
        "all_probes_have_teeth": toothed,
        "audit_passed": clean and toothed,
        "results": results,
    }
    Path(args.output).write_text(json.dumps(payload, indent=2))
    for record in results:
        print(
            f"{record['spot_symbol']:8s} {record['stream']:8s} cut={record['cut_decision_ts']} "
            f"perturbed={record['observations_perturbed']:>6} "
            f"prior_identical={record['all_prior_features_bit_identical']} "
            f"teeth={record['probe_has_teeth']}"
        )
    print(
        f"\nprobes={len(results)} leakage_free={clean} have_teeth={toothed} "
        f"PASSED={clean and toothed}"
    )
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
