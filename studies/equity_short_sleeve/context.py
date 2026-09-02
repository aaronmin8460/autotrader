"""Shared loading for every short phase: frames, regime, stances, panel, and
the untouched incumbent long book.

Loaded once and reused, so that every row in the tournament is a difference
between architectures and never a difference between inputs.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pandas as pd

from studies.equity_eda1_nextgen.run_phase234 import (
    load_frame,
    load_stance,
    load_universe,
    participation_map,
    region_frame,
    region_sessions_of,
    spy_states,
)
from studies.equity_eda1_nextgen.selection import PER_SYMBOL_CAP, build_targets, close_table
from studies.equity_eda1_nextgen.universe import INCUMBENTS
from studies.equity_short_sleeve import CHARACTER_DATASETS
from studies.equity_short_sleeve.information import governing_mark_of


def targets_digest(targets) -> str:
    """A stable digest of a target series, for the §L13 non-regression proof."""
    payload = {
        symbol: sorted((str(stamp), repr(weight)) for stamp, weight in series.items())
        for symbol, series in sorted(targets.items())
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class ShortContext:
    """Everything the short tournament shares, loaded once."""

    def __init__(self, universe_name: str = "u30") -> None:
        self.universe = sorted(load_universe(universe_name))
        self.incumbents = tuple(sorted(INCUMBENTS))
        self.frames_full = {symbol: load_frame(symbol) for symbol in self.universe}
        self.frames = {s: region_frame(f) for s, f in self.frames_full.items()}
        self.participate = participation_map()
        self.sessions = region_sessions_of(self.frames_full["SPY"])
        self.stance = {s: load_stance(s, self.frames[s]) for s in self.universe}
        self.states = spy_states()
        self.closes = close_table(self.frames_full)

        panel = pd.read_parquet(Path(CHARACTER_DATASETS) / "fingerprints.parquet")
        panel["mark"] = [pd.Timestamp(m).date() for m in panel["mark"]]
        self.panel = panel
        self.marks = sorted(set(panel["mark"].unique()))
        self.mark_of = governing_mark_of(self.sessions, self.marks)

        self.defensive_sessions = [s for s in self.sessions if not self.participate[s]]
        self.run_of = self._defensive_runs()

        # The incumbent long book — built once, never modified, digested.
        members = self.incumbents
        membership = {session: members for session in self.sessions}
        weights = {
            session: dict.fromkeys(members, min(1.0 / len(members), PER_SYMBOL_CAP))
            for session in self.sessions
        }
        self.long_frames = {s: self.frames[s] for s in members}
        self.long_targets = build_targets(
            self.long_frames,
            self.sessions,
            self.participate,
            membership,
            {s: self.stance[s] for s in members},
            active_weight_of=weights,
            reserved_weight=min(1.0 / len(members), PER_SYMBOL_CAP),
        )
        self.long_digest = targets_digest(self.long_targets)

    def _defensive_runs(self) -> dict[date, int]:
        run_of: dict[date, int] = {}
        run = -1
        previous = None
        for session in self.sessions:
            state = bool(self.participate[session])
            if not state:
                if previous is None or previous is True:
                    run += 1
                run_of[session] = run
            previous = state
        return run_of

    def transitions_to_participate(self) -> list[date]:
        """Sessions on which the regime flips DEFENSIVE -> PARTICIPATE."""
        ordered = sorted(self.sessions)
        flips: list[date] = []
        for previous, current in zip(ordered[:-1], ordered[1:], strict=True):
            if not self.participate[previous] and self.participate[current]:
                flips.append(current)
        return flips


__all__ = ["ShortContext", "targets_digest"]
