"""EDA-2 (own-trend conditioned) and EDA-3 (breadth-gated) participation.

Both are predeclared in the search ledger. They reuse EDA-1's market rule and
overlay machinery; what differs is *which information* gates a sleeve's
participation:

- EDA-2: market rule AND the sleeve's own symbol trades above its own lagged
  SMA-200 of session closes;
- EDA-3: market rule AND cross-sectional breadth (fraction of the ten
  universe symbols above their own lagged SMA-200) is at least 0.6.

Usage:
    python -m studies.equity_deep_arch.run_eda23 --architecture eda2 --stage full
    python -m studies.equity_deep_arch.run_eda23 --architecture eda3 --stage full
    python -m studies.equity_deep_arch.run_eda23 --architecture eda2 --stage perturb
    python -m studies.equity_deep_arch.run_eda23 --architecture eda3 --stage perturb
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd

from autotrader.equity.session import market_date
from studies.equity_10_full import STUDY_SYMBOLS
from studies.equity_deep_arch.evaluate import (
    evaluate_challenger,
    load_region_frame,
    load_stored_series,
    write_json,
)
from studies.equity_deep_arch.overlay import participation_overlay
from studies.equity_deep_arch.run_eda1 import (
    default_datasets,
    default_decisions,
)
from studies.equity_deep_arch.state import (
    ParticipationSpec,
    participation_series,
    session_closes,
)

OUTPUT_ROOT = Path("/Volumes/AUTOTRADER_QA/reports/equity-deep-architecture")

#: Breadth threshold: majority with margin, declared before any run.
BREADTH_THRESHOLD = 0.6


def _full_frame(datasets: Path, symbol: str) -> pd.DataFrame:
    return pd.read_parquet(sorted(datasets.glob(f"{symbol}_15m_*session.parquet"))[0])


def _own_trend_by_session(
    datasets: Path, symbol: str, spec: ParticipationSpec
) -> dict[object, bool]:
    """Whether `symbol`'s lagged session close exceeds its own lagged SMA."""
    closes = session_closes(_full_frame(datasets, symbol))
    values = closes["close"].to_numpy(dtype="float64")
    sma = pd.Series(values).rolling(spec.sma_sessions).mean().to_numpy()
    result: dict[object, bool] = {}
    for i in range(len(closes)):
        j = i - spec.lag_sessions
        above = bool(j >= 0 and not pd.isna(sma[j]) and values[j] > sma[j])
        result[closes["session"].iloc[i]] = above
    return result


def build_eda2(
    datasets: Path, decisions: Path, spec: ParticipationSpec, own_sma: int
) -> dict[str, tuple]:
    spy_closes = session_closes(_full_frame(datasets, "SPY"))
    market = participation_series(spy_closes, spec)
    market_by_session = {row["session"]: bool(row["participate"]) for _, row in market.iterrows()}
    own_spec = ParticipationSpec(sma_sessions=own_sma, lag_sessions=spec.lag_sessions)

    challenger: dict[str, tuple] = {}
    for symbol in STUDY_SYMBOLS:
        own = _own_trend_by_session(datasets, symbol, own_spec)
        frame = load_region_frame(datasets, symbol)
        by_bar = {}
        for ts in frame["timestamp"]:
            day = market_date(ts.to_pydatetime())
            by_bar[pd.Timestamp(ts)] = market_by_session.get(day, False) and own.get(day, False)
        stored = load_stored_series(decisions, symbol, "V3")
        challenger[symbol] = participation_overlay(stored, by_bar, architecture="EDA2_OTP")
    return challenger


def build_eda3(
    datasets: Path, decisions: Path, spec: ParticipationSpec, threshold: float
) -> dict[str, tuple]:
    spy_closes = session_closes(_full_frame(datasets, "SPY"))
    market = participation_series(spy_closes, spec)
    own_by_symbol = {
        symbol: _own_trend_by_session(datasets, symbol, spec) for symbol in STUDY_SYMBOLS
    }
    market_by_session: dict[object, bool] = {}
    for _, row in market.iterrows():
        day = row["session"]
        above = sum(1 for symbol in STUDY_SYMBOLS if own_by_symbol[symbol].get(day, False))
        breadth = above / len(STUDY_SYMBOLS)
        market_by_session[day] = bool(row["participate"]) and breadth >= threshold

    challenger: dict[str, tuple] = {}
    for symbol in STUDY_SYMBOLS:
        frame = load_region_frame(datasets, symbol)
        by_bar = {}
        for ts in frame["timestamp"]:
            day = market_date(ts.to_pydatetime())
            by_bar[pd.Timestamp(ts)] = market_by_session.get(day, False)
        stored = load_stored_series(decisions, symbol, "V3")
        challenger[symbol] = participation_overlay(stored, by_bar, architecture="EDA3_BGP")
    return challenger


def evaluate_and_write(datasets, decisions, challenger, label, path) -> None:
    result = evaluate_challenger(
        datasets, decisions, challenger, label=label, symbols=STUDY_SYMBOLS
    )
    part_counts = {
        symbol: sum(1 for record in challenger[symbol] if record.regime == "PARTICIPATE")
        for symbol in challenger
    }
    result["participate_bars_by_symbol"] = part_counts
    write_json(path, result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", required=True, choices=("eda2", "eda3"))
    parser.add_argument("--stage", required=True, choices=("full", "perturb"))
    arguments = parser.parse_args()
    datasets = default_datasets()
    decisions = default_decisions()
    spec = ParticipationSpec()
    started = time.perf_counter()

    if arguments.architecture == "eda2":
        output = OUTPUT_ROOT / "eda2"
        if arguments.stage == "full":
            challenger = build_eda2(datasets, decisions, spec, own_sma=200)
            evaluate_and_write(
                datasets, decisions, challenger, "EDA2_OTP", output / "full_evaluation.json"
            )
        else:
            if not (output / "full_evaluation.json").exists():
                raise SystemExit("perturb refuses to run before the primary evaluation exists.")
            for own_sma in (150, 250):
                target = output / f"perturb_ownsma{own_sma}.json"
                if target.exists():
                    continue
                challenger = build_eda2(datasets, decisions, spec, own_sma=own_sma)
                evaluate_and_write(datasets, decisions, challenger, "EDA2_OTP", target)
    else:
        output = OUTPUT_ROOT / "eda3"
        if arguments.stage == "full":
            challenger = build_eda3(datasets, decisions, spec, threshold=BREADTH_THRESHOLD)
            evaluate_and_write(
                datasets, decisions, challenger, "EDA3_BGP", output / "full_evaluation.json"
            )
        else:
            if not (output / "full_evaluation.json").exists():
                raise SystemExit("perturb refuses to run before the primary evaluation exists.")
            for threshold in (0.5, 0.7):
                target = output / f"perturb_breadth{int(threshold * 100)}.json"
                if target.exists():
                    continue
                challenger = build_eda3(datasets, decisions, spec, threshold=threshold)
                evaluate_and_write(datasets, decisions, challenger, "EDA3_BGP", target)

    elapsed = time.perf_counter() - started
    print(f"{arguments.architecture} {arguments.stage} complete in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
