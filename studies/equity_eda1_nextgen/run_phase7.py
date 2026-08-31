"""Phase-7 runner: 5-minute execution pilot (ledger §L8 + the E3 amendment).

The decision series is the incumbent EDA-1 overlay, unchanged — the execution
layer only moves WHEN a transition fills:

- E0 (baseline): next 15m bar's open (the shipped replay semantics).
- E1 TWAP-3: mean of the opens of the first three 5m bars at/after the
  decision bar's close.
- E2 bounded favourable: the first of the next six 5m bars whose open is at
  or better than the decision bar's close (≤ for BUY, ≥ for SELL); the 6th
  bar's open unconditionally otherwise. Hard 30-minute deadline.
- E3 (control, degenerate slow clock): the next 15m open on the 30m grid.

Causality: every fill uses only opens at or after the instant the decision
became known (the decision bar's close). No intrabar high/low is read. When
no 5m bar exists inside a horizon, the E0 fill is used and counted as a
fallback (disclosed).

Shortfall per transition: sign(side) × (fill − reference) / reference, in bp,
reference = decision bar's close. Net-effect estimate: each transition moves
one sleeve (10 % of the book), so Δnet ≈ Σ (E0 − EX) shortfall × 0.10 —
a declared first-order approximation.

Usage:
    python -m studies.equity_eda1_nextgen.run_phase7 --stage execution
"""

from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path

import pandas as pd

from autotrader.decision.contract import DecisionSignal
from studies.equity_10_full import STUDY_SYMBOLS
from studies.equity_deep_arch.evaluate import write_json
from studies.equity_deep_arch.run_eda1 import (
    build_challenger,
    default_datasets,
    default_decisions,
)
from studies.equity_deep_arch.state import ParticipationSpec
from studies.equity_eda1_nextgen import NEXTGEN_DATASETS, REPORT_ROOT

FIVE_DIR = Path(NEXTGEN_DATASETS) / "bars-5m"
DEADLINE_5M_BARS = 6


def five_minute_opens(symbol: str) -> pd.Series:
    files = sorted(FIVE_DIR.glob(f"{symbol}_5m_*session.parquet"))
    if len(files) != 1:
        raise SystemExit(f"Expected one 5m frame for {symbol}, found {files}.")
    frame = pd.read_parquet(files[0])
    return pd.Series(
        frame["open"].to_numpy(dtype="float64"),
        index=pd.DatetimeIndex(frame["timestamp"]),
    ).sort_index()


def fifteen_minute_frame(datasets: Path, symbol: str) -> pd.DataFrame:
    files = sorted(datasets.glob(f"{symbol}_15m_*session.parquet"))
    return pd.read_parquet(files[0])


def run_execution(output: Path) -> None:
    datasets = default_datasets()
    decisions = default_decisions()
    spec = ParticipationSpec()
    challenger = build_challenger(datasets, decisions, tuple(STUDY_SYMBOLS), spec)

    per_symbol: dict[str, object] = {}
    all_rows: list[dict[str, float]] = []
    fallbacks = 0
    transitions_total = 0

    for symbol in STUDY_SYMBOLS:
        records = challenger[symbol]
        frame = fifteen_minute_frame(datasets, symbol)
        closes = pd.Series(
            frame["close"].to_numpy(dtype="float64"),
            index=pd.DatetimeIndex(frame["timestamp"]),
        ).sort_index()
        opens15 = pd.Series(
            frame["open"].to_numpy(dtype="float64"),
            index=pd.DatetimeIndex(frame["timestamp"]),
        ).sort_index()
        opens5 = five_minute_opens(symbol)
        stamps15 = opens15.index

        for record in records:
            if record.signal not in (DecisionSignal.BUY, DecisionSignal.SELL):
                continue
            t = pd.Timestamp(record.timestamp)
            position = stamps15.searchsorted(t, side="right")
            if position >= len(stamps15):
                continue  # final-bar proposal: stays unexecuted, as shipped
            transitions_total += 1
            side = 1.0 if record.signal is DecisionSignal.BUY else -1.0
            reference = float(closes.loc[t])
            known_at = t + pd.Timedelta(minutes=15)

            e0_fill = float(opens15.iloc[position])
            e0_delay = (stamps15[position] - t).total_seconds() / 60.0 - 15.0

            start5 = opens5.index.searchsorted(known_at, side="left")
            window5 = opens5.iloc[start5 : start5 + DEADLINE_5M_BARS]
            if len(window5) == 0:
                e1_fill, e2_fill = e0_fill, e0_fill
                e1_delay = e2_delay = e0_delay
                fallbacks += 1
            else:
                slices = window5.iloc[:3]
                e1_fill = float(slices.mean())
                e1_delay = (slices.index[-1] - known_at).total_seconds() / 60.0
                e2_fill = float(window5.iloc[-1])
                e2_delay = (window5.index[-1] - known_at).total_seconds() / 60.0
                for stamp, price in window5.items():
                    favourable = price <= reference if side > 0 else price >= reference
                    if favourable:
                        e2_fill = float(price)
                        e2_delay = (stamp - known_at).total_seconds() / 60.0
                        break

            # E3: next 15m open on the 30m grid.
            e3_position = position
            while e3_position < len(stamps15) and stamps15[e3_position].minute % 30 != 0:
                e3_position += 1
            if e3_position < len(stamps15):
                e3_fill = float(opens15.iloc[e3_position])
                e3_delay = (stamps15[e3_position] - known_at).total_seconds() / 60.0
            else:
                e3_fill, e3_delay = e0_fill, e0_delay

            row = {"symbol": symbol, "side": side}
            for name, fill, delay in (
                ("E0", e0_fill, e0_delay),
                ("E1", e1_fill, e1_delay),
                ("E2", e2_fill, e2_delay),
                ("E3", e3_fill, e3_delay),
            ):
                row[f"{name}_bp"] = side * (fill - reference) / reference * 1e4
                row[f"{name}_delay_min"] = delay
            all_rows.append(row)

    summary: dict[str, object] = {
        "transitions": transitions_total,
        "fallbacks_no_5m_bar": fallbacks,
        "deadline_5m_bars": DEADLINE_5M_BARS,
        "strategies": {},
    }
    for name in ("E0", "E1", "E2", "E3"):
        shortfalls = [row[f"{name}_bp"] for row in all_rows]
        delays = [row[f"{name}_delay_min"] for row in all_rows]
        summary["strategies"][name] = {
            "mean_shortfall_bp": statistics.fmean(shortfalls),
            "median_shortfall_bp": statistics.median(shortfalls),
            "total_shortfall_bp": sum(shortfalls),
            "mean_delay_min": statistics.fmean(delays),
            "vs_E0_mean_bp": statistics.fmean(
                [row[f"{name}_bp"] - row["E0_bp"] for row in all_rows]
            ),
            "net_effect_estimate_pts": -sum(row[f"{name}_bp"] - row["E0_bp"] for row in all_rows)
            * 0.10
            / 1e4
            * 100,
        }
    per_symbol["rows"] = all_rows
    write_json(output / "execution_summary.json", summary)
    write_json(output / "execution_rows.json", per_symbol)
    for name, block in summary["strategies"].items():
        print(
            f"{name}: mean {block['mean_shortfall_bp']:+.2f} bp, "
            f"vs E0 {block['vs_E0_mean_bp']:+.2f} bp, "
            f"net-effect {block['net_effect_estimate_pts']:+.3f} pts, "
            f"delay {block['mean_delay_min']:.1f} min",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("execution",))
    parser.add_argument("--output", type=Path, default=Path(REPORT_ROOT) / "phase7")
    arguments = parser.parse_args()
    started = time.perf_counter()
    run_execution(arguments.output)
    print(f"execution stage complete in {time.perf_counter() - started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
