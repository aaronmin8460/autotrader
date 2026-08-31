"""Real-data leakage attacks (search-ledger.md §9).

Attack 1 - future perturbation: corrupt every OI / flow source row knowable
after a cut instant and require every joined feature at or before the cut to
come back bit-identical. Each probe must have teeth: the corruption must
change something after the cut, so a probe cannot pass by perturbing a region
no feature reads.

Attack 2 (run separately, after the pilot) - the one-interval future shift:
`knowable_at` moved 15 minutes toward the past lets the join read each source
one bar early; if headline metrics improve materially, the honest reading is
leakage risk, not extra alpha. Predeclared shift cells: (full, BTC/USD, W03,
h96) and (full, ETH/USD, X05, h96).
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from studies.crypto_new_alpha.frames import PERP_OF, SYMBOLS, load_normalized, study_frames
from studies.crypto_new_alpha.new_features import join_new_features

OUTPUT = Path(
    "/Volumes/AUTOTRADER_QA/reports/crypto-new-alpha-oi-liq-flow/leakage_perturbation.json"
)

#: Predeclared cut instants spanning both eras.
CUTS = ("2021-06-01", "2022-09-01", "2024-06-01", "2026-06-01")


def _era_of(cut: pd.Timestamp) -> str:
    return "extended" if cut.year <= 2023 else "modern"


def probe(symbol: str, stream: str, cut: pd.Timestamp) -> dict:
    era = _era_of(cut)
    study = study_frames(era)[symbol]
    frame = study.frame
    timestamps = frame["timestamp"]
    r16 = frame["return_16"]
    r96 = frame["return_96"]
    oi, flow = load_normalized(PERP_OF[symbol])

    clean, _ = join_new_features(timestamps, oi, flow, return_16=r16, return_96=r96)

    oi_p, flow_p = oi.copy(), flow.copy()
    if stream == "oi":
        mask = oi_p["knowable_at"] > cut
        oi_p.loc[mask, "oi_notional"] = (oi_p.loc[mask, "oi_notional"] * -50.0 + 7.0).abs() + 1.0
        teeth_rows = int(mask.sum())
    else:
        mask = flow_p["knowable_at"] > cut
        flow_p.loc[mask, "taker_buy_quote_volume"] = (
            flow_p.loc[mask, "quote_volume"] - flow_p.loc[mask, "taker_buy_quote_volume"]
        )
        teeth_rows = int(mask.sum())

    poisoned, _ = join_new_features(timestamps, oi_p, flow_p, return_16=r16, return_96=r96)

    decision_ts = pd.to_datetime(timestamps, utc=True) + pd.Timedelta("15min")
    before = (decision_ts <= cut).to_numpy()
    after = ~before

    clean_before = clean.loc[before].reset_index(drop=True)
    poisoned_before = poisoned.loc[before].reset_index(drop=True)
    identical = clean_before.equals(poisoned_before)
    changed_after = not clean.loc[after].reset_index(drop=True).equals(
        poisoned.loc[after].reset_index(drop=True)
    )
    return {
        "symbol": symbol,
        "stream": stream,
        "cut": str(cut),
        "era": era,
        "rows_before": int(before.sum()),
        "rows_after": int(after.sum()),
        "source_rows_poisoned": teeth_rows,
        "past_bit_identical": bool(identical),
        "future_changed_teeth": bool(changed_after),
        "clean": bool(identical and changed_after and teeth_rows > 0),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    probes = []
    for symbol in SYMBOLS:
        for cut_text in CUTS:
            cut = pd.Timestamp(cut_text, tz="UTC")
            for stream in ("oi", "flow"):
                result = probe(symbol, stream, cut)
                probes.append(result)
                print(
                    f"{symbol} {stream} cut={cut_text}: "
                    f"past_identical={result['past_bit_identical']} "
                    f"teeth={result['future_changed_teeth']}"
                )
    payload = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "probes": probes,
        "all_clean": all(p["clean"] for p in probes),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, OUTPUT)
    print(f"ALL CLEAN: {payload['all_clean']} -> {OUTPUT}")


if __name__ == "__main__":
    main()
