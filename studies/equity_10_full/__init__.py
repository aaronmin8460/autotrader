"""The full ten-symbol Equity historical evaluation of Decision Engines V1-V5.

This is the study the SPY/QQQ pilot existed to make safe and the V4 horizon
study existed to make honest. Both are inputs here, not baselines: the harness
under ``studies/equity_v1_v5`` is reused verbatim from the pilot's verified
commits, and the frozen V4 research configuration (4-bar horizon, 0.002
materiality gate, null-capable selection) is the horizon study's §20, unchanged.

**What is fixed before any result exists.** The universe (the shipped
``EQUITY_SYMBOLS`` ten), the data window, the split adjustment, the walk-forward
window count, the holdout designation, the lookback floor, the cost models and
the engine contracts are all declared in this package before scoring begins.
Nothing in this study tunes a parameter, weakens a gate, or drops a symbol
because its result looks bad.

**Research only.** Nothing here deploys, activates, unmasks, or reaches a
broker except through the read-only historical-bars GET the pilot already
validated.
"""

from __future__ import annotations

from datetime import date

from autotrader.equity import EQUITY_SYMBOLS

#: The full frozen universe, in the shipped processing order. Exactly the
#: production ``EQUITY_SYMBOLS`` tuple: this study evaluates the system's own
#: universe, not a research-convenient variant of it.
STUDY_SYMBOLS: tuple[str, ...] = EQUITY_SYMBOLS

#: The symbols whose evaluation frames the pilot already built and fingerprinted.
#: Reused under pinned digests rather than re-downloaded; the pilot proved the
#: stored frames byte-identical to split-adjusted re-downloads.
PILOT_BUILT_SYMBOLS: tuple[str, ...] = ("SPY", "QQQ")

#: Pinned digests for the reused pilot frames. A frame that does not reproduce
#: its digest is refused, not warned about.
PILOT_FRAME_SHA256: dict[str, str] = {
    "SPY": "d409cd3b1bdf7847bcc879db68e8b7f8a4b8f310b6b032cef85a06a9017ccc5b",
    "QQQ": "c53d984e588955fa09e0db4aeec0a2d3911a436d380f640da9ab77d6c1cc5a9f",
}

#: The dataset window, identical to the pilot's. The IEX feed's history is a
#: rolling ~6-year window, so this range is only reachable while the study
#: runs; the stored Parquet and its digest are the durable artifact.
DATA_START = date(2021, 1, 4)
DATA_END = date(2026, 8, 28)

#: The calendar snapshot range the pilot stored. The snapshot spans the whole
#: study window with margin on both sides, so it is reused under its own
#: digest rather than re-fetched: same sessions, same provenance chain.
CALENDAR_START = date(2020, 1, 1)
CALENDAR_END = date(2026, 12, 31)

#: Known stock splits inside the data window, from the exchanges' own records.
#: The split-step audit uses these to prove the frames are split-adjusted: a
#: raw frame would show a one-bar overnight step of about -(1 - 1/ratio) at
#: each of these dates, and a split-adjusted frame must show none.
KNOWN_SPLITS: dict[str, tuple[tuple[str, int], ...]] = {
    "NVDA": (("2024-06-10", 10),),
    "AMZN": (("2022-06-06", 20),),
    "GOOGL": (("2022-07-18", 20),),
    "TSLA": (("2022-08-25", 3),),
}

#: Fixed seed for every fit in this study.
STUDY_SEED = 0

__all__ = [
    "CALENDAR_END",
    "CALENDAR_START",
    "DATA_END",
    "DATA_START",
    "KNOWN_SPLITS",
    "PILOT_BUILT_SYMBOLS",
    "PILOT_FRAME_SHA256",
    "STUDY_SEED",
    "STUDY_SYMBOLS",
]
