"""Non-vacuous future-perturbation causality audit of the overlay pipeline.

The stored V3 series was causality-audited in its own study (50/50 PASS, zero
changed decisions). What remains to prove here is that the *state and overlay*
layers added by this program are causal: for a probe instant T inside the
scored region, multiplying every bar strictly after T by 1.5 (a violent future
shock) must leave every overlay decision at or before T bit-identical.

Probes are placed inside the scored region across distinct market regimes
(calm, pullback, drawdown sessions) and multiple symbols, and each probe
reports how many decisions it actually compared — a probe covering none is a
failure, not a pass.

Usage:
    python -m studies.equity_deep_arch.run_causality
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import pandas as pd

from studies.equity_deep_arch.evaluate import (
    load_region_frame,
    load_stored_series,
    write_json,
)
from studies.equity_deep_arch.overlay import participation_overlay
from studies.equity_deep_arch.run_eda1 import default_datasets, default_decisions
from studies.equity_deep_arch.state import (
    ParticipationSpec,
    participation_series,
    per_bar_participation,
    session_closes,
)

OUTPUT = Path("/Volumes/AUTOTRADER_QA/reports/equity-deep-architecture/causality")

#: Probe sessions: one inside each distinct market state of the region —
#: calm uptrend (2023-12), the 2022 bear (drawdown), the 2025 spring shock,
#: a 2021 late-cycle calm, and the 2024 summer pullback.
PROBE_SESSIONS: tuple[date, ...] = (
    date(2021, 11, 15),
    date(2022, 6, 15),
    date(2023, 12, 15),
    date(2024, 8, 7),
    date(2025, 4, 15),
)

PROBE_SYMBOLS: tuple[str, ...] = ("SPY", "NVDA", "META", "GOOGL")


def overlay_records(datasets: Path, decisions: Path, symbol: str, spy_frame: pd.DataFrame):
    spec = ParticipationSpec()
    closes = session_closes(spy_frame)
    participation = participation_series(closes, spec)
    frame = load_region_frame(datasets, symbol)
    by_bar = per_bar_participation(frame, participation)
    stored = load_stored_series(decisions, symbol, "V3")
    return participation_overlay(stored, by_bar, architecture="EDA1_RGP")


def main() -> None:
    datasets = default_datasets()
    decisions = default_decisions()
    started = time.perf_counter()
    OUTPUT.mkdir(parents=True, exist_ok=True)

    spy_full = pd.read_parquet(sorted(datasets.glob("SPY_15m_*session.parquet"))[0])

    probes = []
    all_pass = True
    for symbol in PROBE_SYMBOLS:
        baseline = overlay_records(datasets, decisions, symbol, spy_full)
        for probe_day in PROBE_SESSIONS:
            cutoff = pd.Timestamp(probe_day).tz_localize("UTC") + pd.Timedelta(hours=23)
            shocked = spy_full.copy()
            future = pd.DatetimeIndex(shocked["timestamp"]) > cutoff
            for column in ("open", "high", "low", "close"):
                shocked.loc[future, column] = shocked.loc[future, column] * 1.5
            perturbed = overlay_records(datasets, decisions, symbol, shocked)

            compared = 0
            changed = 0
            for before, after in zip(baseline, perturbed, strict=True):
                if before.timestamp > cutoff:
                    break
                compared += 1
                if (
                    before.signal is not after.signal
                    or before.regime != after.regime
                    or before.reasons != after.reasons
                ):
                    changed += 1
            verdict = "PASS" if changed == 0 and compared > 0 else "FAIL"
            if verdict == "FAIL":
                all_pass = False
            probes.append(
                {
                    "symbol": symbol,
                    "probe_session": str(probe_day),
                    "decisions_compared": compared,
                    "changed_decisions": changed,
                    "future_bars_shocked": int(future.sum()),
                    "verdict": verdict,
                }
            )
            print(
                f"{symbol} @ {probe_day}: {compared} compared, {changed} changed -> {verdict}",
                flush=True,
            )

    payload = {
        "description": (
            "Future-perturbation audit of the state+overlay pipeline: SPY bars strictly "
            "after each probe instant multiplied by 1.5; overlay decisions at or before "
            "the probe must be identical. The stored V3 series' own causality was "
            "audited in the ten-symbol study (50/50 PASS)."
        ),
        "probes": probes,
        "all_pass": all_pass,
        "elapsed_seconds": time.perf_counter() - started,
    }
    write_json(OUTPUT / "overlay_causality.json", payload)
    print(f"causality: {'ALL PASS' if all_pass else 'FAILURES PRESENT'}")


if __name__ == "__main__":
    main()
