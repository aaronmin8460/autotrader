"""Settled-funding agreement between the historical source and a live source.

The deep historical funding record (2020-01 onward) comes from one venue's
public data dumps; that venue's REST API is geo-blocked from this machine, so
a *live* engine would read funding from a different, accessible venue. This
script measures how much that substitution costs, over the accessible venue's
~3-month API history window.

Both venues settle on the same 8-hour UTC grid, so settlements are matched by
snapping millisecond jitter to the grid instant. Reported: Pearson
correlation, mean absolute difference in bps per interval, and sign
agreement. A low-dispersion overlap window attenuates correlation; the
numbers are a floor on agreement, not a ceiling.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

EIGHT_HOURS_MS = 8 * 3600 * 1000


def load_live_pages(samples_dir: Path) -> pd.DataFrame:
    """Concatenate the saved live-venue funding-history pages."""
    records: list[dict] = []
    for page in sorted(samples_dir.glob("okx_funding_page_*.json")):
        records.extend(json.loads(page.read_text()).get("data", []))
    frame = pd.DataFrame(records)
    frame["settlement_ms"] = pd.to_numeric(frame["fundingTime"])
    frame["live_rate"] = pd.to_numeric(frame["realizedRate"], errors="coerce")
    return frame.drop_duplicates("settlement_ms").sort_values("settlement_ms")


def load_historical(samples_dir: Path, months: tuple[str, ...]) -> pd.DataFrame:
    frames = [pd.read_csv(samples_dir / f"BTCUSDT-fundingRate-{month}.csv") for month in months]
    frame = pd.concat(frames, ignore_index=True)
    frame["settlement_ms"] = (frame["calc_time"] // EIGHT_HOURS_MS) * EIGHT_HOURS_MS
    return frame


def main(samples_dir: Path) -> None:
    live = load_live_pages(samples_dir)
    historical = load_historical(samples_dir, ("2026-06", "2026-07"))

    matched = pd.merge(
        historical[["settlement_ms", "last_funding_rate"]],
        live[["settlement_ms", "live_rate"]],
        on="settlement_ms",
        how="inner",
    ).dropna()

    assert len(matched) > 100, "overlap too small to report"
    correlation = matched["last_funding_rate"].corr(matched["live_rate"])
    mean_abs_diff_bps = (matched["last_funding_rate"] - matched["live_rate"]).abs().mean() * 1e4
    sign_agreement = ((matched["last_funding_rate"] > 0) == (matched["live_rate"] > 0)).mean()

    print(f"matched settlements: {len(matched)}")
    print(f"corr(historical, live): {correlation:.3f}")
    print(f"mean |diff|: {mean_abs_diff_bps:.3f} bps per 8h interval")
    print(f"sign agreement: {sign_agreement:.3f}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path())
