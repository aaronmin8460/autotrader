"""Re-running the pipeline and requiring the identical answer, bit for bit.

Three separate claims, checked separately because they can fail independently:

*The dataset is stable.* Re-reading the stored Parquet and re-deriving its
fingerprint must give the digest recorded at download time. If it does not, the
file changed underneath the study and every number is against a different
dataset than the one the report names.

*Scoring is deterministic.* Scoring the same window twice with the same engine
must produce the identical decision series - the same signals, and the same
scores to full precision. An engine that used an unseeded random draw, iterated
a set, or depended on dictionary order would show up here and nowhere else.

*Training is deterministic.* Fitting the same window's model twice from the same
seed must select the same family and produce the same model version and the same
fitted probabilities. Anything else would mean the reported walk-forward is not
the one a re-run would get.

Small on purpose: one window, one short slice, both symbols. Determinism is a
property that either holds or does not; testing it over more bars costs more and
proves the same thing.

    python -m studies.equity_v1_v5.run_reproducibility --output <dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from studies.equity_v1_v5 import PILOT_SYMBOLS
from studies.equity_v1_v5.calendar import read_snapshot, snapshot_path
from studies.equity_v1_v5.dataset import evaluation_path, frame_digest
from studies.equity_v1_v5.run_pilot import (
    CALENDAR_END,
    CALENDAR_START,
    DATA_END,
    DATA_START,
    SEED,
    TRAINED_AT,
)
from studies.equity_v1_v5.scoring import build_engines, decisions_to_frame, score_window
from studies.equity_v1_v5.walkforward import train_for_window
from studies.equity_v1_v5.windows import LOOKBACK_BARS, ScoringWindow

#: How many bars the determinism slice scores. Two sessions is enough for every
#: engine to produce a mixture of signals and holds.
SLICE_BARS = 52


def series_digest(frame: pd.DataFrame) -> str:
    """A fingerprint of a decision series, scores included at full precision."""
    return hashlib.sha256(frame.to_csv(index=False).encode("utf-8")).hexdigest()


def run(datasets: Path, output: Path, symbols: list[str]) -> dict[str, object]:
    calendar, _ = read_snapshot(snapshot_path(datasets, CALENDAR_START, CALENDAR_END))
    from studies.equity_v1_v5.windows import SCORING_WINDOWS

    source = SCORING_WINDOWS[-1]
    checks: list[dict[str, object]] = []

    for symbol in symbols:
        path = evaluation_path(datasets, symbol, DATA_START, DATA_END)
        frame = pd.read_parquet(path)
        recorded = json.loads(path.with_suffix(".provenance.json").read_text(encoding="utf-8"))
        recomputed = frame_digest(frame)
        checks.append(
            {
                "symbol": symbol,
                "check": "dataset_digest",
                "expected": recorded["frame_sha256"],
                "observed": recomputed,
                "ok": recomputed == recorded["frame_sha256"],
            }
        )

        # A short window inside the last scoring window, so the warm-up exists.
        first, last = source.positions(frame)
        from autotrader.equity.session import market_date

        end_day = market_date(frame["timestamp"].iloc[first + SLICE_BARS].to_pydatetime())
        window = ScoringWindow(
            f"repro-{symbol}",
            market_date(frame["timestamp"].iloc[first].to_pydatetime()),
            end_day,
            "determinism slice",
        )

        first_model = train_for_window(
            frame, calendar, window, symbol=symbol, seed=SEED, trained_at=TRAINED_AT
        )
        second_model = train_for_window(
            frame, calendar, window, symbol=symbol, seed=SEED, trained_at=TRAINED_AT
        )
        checks.append(
            {
                "symbol": symbol,
                "check": "training_determinism",
                "expected": f"{first_model.selected_family}/{first_model.model_version}",
                "observed": f"{second_model.selected_family}/{second_model.model_version}",
                "ok": (
                    first_model.selected_family == second_model.selected_family
                    and first_model.model_version == second_model.model_version
                    and first_model.selected_log_loss == second_model.selected_log_loss
                ),
            }
        )

        for spec in build_engines():
            artifact = first_model.artifact if spec.needs_model else None
            one = series_digest(
                decisions_to_frame(
                    score_window(
                        frame,
                        window,
                        spec,
                        symbol=symbol,
                        artifact=artifact,
                        lookback_bars=LOOKBACK_BARS,
                    )
                )
            )
            two = series_digest(
                decisions_to_frame(
                    score_window(
                        frame,
                        window,
                        spec,
                        symbol=symbol,
                        artifact=artifact,
                        lookback_bars=LOOKBACK_BARS,
                    )
                )
            )
            checks.append(
                {
                    "symbol": symbol,
                    "check": f"scoring_determinism_{spec.name}",
                    "expected": one[:32],
                    "observed": two[:32],
                    "ok": one == two,
                }
            )
            verdict = "OK" if one == two else "MISMATCH"
            print(f"  {symbol}/{spec.name}: {verdict} {one[:16]}", flush=True)

    report = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "seed": SEED,
        "slice_bars": SLICE_BARS,
        "all_reproducible": all(check["ok"] for check in checks),
        "checks": checks,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "reproducibility.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Check the pilot reproduces itself.")
    parser.add_argument("--datasets", default=os.environ.get("EQUITY_DATASETS", "."))
    parser.add_argument("--output", default=os.environ.get("PILOT_REPORTS", "."))
    parser.add_argument("--symbols", nargs="*", default=list(PILOT_SYMBOLS))
    arguments = parser.parse_args()
    report = run(Path(arguments.datasets), Path(arguments.output), arguments.symbols)
    print(f"\nall_reproducible={report['all_reproducible']}")


if __name__ == "__main__":
    main()
