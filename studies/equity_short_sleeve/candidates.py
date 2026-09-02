"""The predeclared short-candidate tournament (ledger §L6, amendments A2/A3).

A candidate is a **signed** target series: the incumbent's long book, which is
never touched, plus a DEFENSIVE-only short book. The long book is built by the
inherited `e_sleeve_targets`/`build_targets` path and is asserted float-identical
to B0's (§L13) — the short program must not win by editing the long side.

Selection rules here are the ones §L6 declared before Phase 2 ran, and the
cohort §L4.2 assigned after Phase 2 failed its gate. Nothing is chosen by a
backtest.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import pandas as pd

from studies.equity_eda1_nextgen.universe import INCUMBENTS
from studies.equity_short_sleeve.information import panel_at

#: §L6 sizing envelope, binding on every primary row.
MAX_SHORT_GROSS = 0.15
MAX_SINGLE_SHORT = 0.03
MAX_SHORT_NAMES = 5

#: The declared short gross grid.
GROSS_GRID: tuple[float, ...] = (0.05, 0.10, 0.15)

#: Cohort fraction for S3 (top tercile of the qualifying characteristic).
COHORT_FRACTION = 1.0 / 3.0


class CandidateError(Exception):
    """A candidate that cannot be built under the ledger's semantics."""


Targets = dict[str, dict[pd.Timestamp, float]]


@dataclass(frozen=True)
class ShortPlan:
    """Which names are short, and at what weight, on each session."""

    label: str
    weight_of: dict[date, dict[str, float]]

    def realized_names(self) -> int:
        return max((len(w) for w in self.weight_of.values()), default=0)


def index_short_plan(
    sessions: Sequence[date],
    participate: Mapping[date, bool],
    *,
    gross: float,
    members: tuple[str, ...],
) -> ShortPlan:
    """S1: a fixed index hedge, on only while DEFENSIVE. Zero parameters
    beyond the declared gross and the declared member list.

    The 3 % single-name cap does not bind here (ledger amendment A4): it
    governs idiosyncratic single-name risk, and a broad-index ETF hedge is
    the instrument the cap exists to prefer over. The 15 % gross cap does
    bind, and does so identically for every row in the tournament.
    """
    per_name = gross / len(members)
    weight_of: dict[date, dict[str, float]] = {}
    for session in sessions:
        weight_of[session] = (
            {} if participate[session] else {symbol: per_name for symbol in members}
        )
    return ShortPlan(label=f"S1_{'_'.join(members)}_{int(gross * 100)}", weight_of=weight_of)


def _weakness_rank(row: pd.DataFrame, eligible: Sequence[str]) -> list[str]:
    """Weakest first, by `rs_63` ascending — the declared ranking."""
    scores = {s: float(row.loc[s, "rs_63"]) for s in eligible if s in row.index}
    scores = {s: v for s, v in scores.items() if v == v}  # drop NaN
    return sorted(scores, key=lambda s: (scores[s], s))


def _qualifies_weak(row: pd.DataFrame, symbol: str) -> bool:
    """The declared two-condition weakness confirmation."""
    if symbol not in row.index:
        return False
    trend = row.loc[symbol, "trend_dist"]
    rs = row.loc[symbol, "rs_63"]
    if trend != trend or rs != rs:
        return False
    return bool(trend < 0.0) and bool(rs < 0.0)


def selected_short_plan(
    sessions: Sequence[date],
    participate: Mapping[date, bool],
    panel: pd.DataFrame,
    mark_of: Mapping[date, date],
    universe: Sequence[str],
    *,
    label: str,
    gross: float,
    names: int,
    characteristic: str | None = None,
    require_weakness: bool = True,
    cohort_fraction: float = COHORT_FRACTION,
    exclude_symbols: frozenset[str] = frozenset(),
) -> ShortPlan:
    """S2/S3: qualified weak names, DEFENSIVE only.

    `characteristic=None` is S2 — no risk-character cohort, weakness alone
    over the incumbent universe. A characteristic selects S3's cohort first,
    then weakness confirms, then `rs_63` ranks *within* the cohort.
    """
    cache: dict[date, list[str]] = {}
    weight_of: dict[date, dict[str, float]] = {}
    for session in sessions:
        if participate[session]:
            weight_of[session] = {}
            continue
        mark = mark_of.get(session)
        if mark is None:
            weight_of[session] = {}
            continue
        if mark not in cache:
            row = panel_at(panel, mark, [u for u in universe if u not in exclude_symbols])
            if row.empty:
                cache[mark] = []
            else:
                pool = [u for u in universe if u not in exclude_symbols]
                if characteristic is None:
                    eligible = list(pool)
                else:
                    values = {
                        s: float(row.loc[s, characteristic])
                        for s in pool
                        if s in row.index
                        and row.loc[s, characteristic] == row.loc[s, characteristic]
                    }
                    ordered = sorted(values, key=lambda s: (-values[s], s))
                    take = max(1, int(round(len(ordered) * cohort_fraction)))
                    eligible = ordered[:take]
                if require_weakness:
                    eligible = [s for s in eligible if _qualifies_weak(row, s)]
                cache[mark] = _weakness_rank(row, eligible)[:names]
        chosen = cache[mark]
        weight_of[session] = {symbol: gross / len(chosen) for symbol in chosen} if chosen else {}
    return ShortPlan(label=label, weight_of=weight_of)


def apply_short_plan(
    long_targets: Targets,
    frames: Mapping[str, pd.DataFrame],
    plan: ShortPlan,
    sessions: Sequence[date],
    *,
    unavailable: set[tuple[str, date]] | None = None,
    delay_bars: int = 0,
    net_against_long: bool = False,
) -> Targets:
    """Combine the untouched long book with the short book (§L13, §A3).

    A symbol already carried long on a bar is NOT shorted (amendment A3): the
    short fails CLOSED and its capital stays cash. `unavailable` is the §L8
    borrow-failure scenario, applied the same way. `delay_bars` shifts the
    whole short book forward by that many bars (§L9 stress) — the long book
    is never shifted.
    """
    from autotrader.equity.session import market_date

    blocked = unavailable or set()
    session_set = set(sessions)
    combined: Targets = {}

    for symbol in sorted(frames):
        frame = frames[symbol]
        stamps = [pd.Timestamp(ts) for ts in frame["timestamp"]]
        # A symbol the long book never holds still needs an EXPLICIT 0.0 on
        # every region bar. Without it the replay carries the last acted
        # weight forward on bars the series omits, and a short opened once is
        # never closed — the short book would accumulate without bound.
        long_series = long_targets.get(symbol, {})
        series: dict[pd.Timestamp, float] = {}
        for stamp in stamps:
            session = market_date(stamp.to_pydatetime())
            if session not in session_set:
                continue
            series[stamp] = long_series.get(stamp, 0.0)
        short_by_stamp: dict[pd.Timestamp, float] = {}
        for stamp in stamps:
            session = market_date(stamp.to_pydatetime())
            if session not in session_set:
                continue
            weight = plan.weight_of.get(session, {}).get(symbol, 0.0)
            if weight and (symbol, session) not in blocked:
                short_by_stamp[stamp] = weight
        if delay_bars:
            shifted: dict[pd.Timestamp, float] = {}
            for index, stamp in enumerate(stamps):
                source = stamps[index - delay_bars] if index >= delay_bars else None
                if source is not None and source in short_by_stamp:
                    shifted[stamp] = short_by_stamp[source]
            short_by_stamp = shifted
        for stamp, weight in short_by_stamp.items():
            existing = series.get(stamp, 0.0)
            if existing > 0.0:
                if not net_against_long:
                    continue  # amendment A3: held long, not shortable — fail closed
                series[stamp] = existing - weight  # amendment A5: netted hedge
                continue
            series[stamp] = -weight
        combined[symbol] = series
    return combined


def plan_diagnostics(
    plan: ShortPlan, participate: Mapping[date, bool], sessions: Sequence[date] | None = None
) -> dict[str, object]:
    """What the plan proposes, before any fill — selection facts only."""
    active = {s: w for s, w in plan.weight_of.items() if w}
    counts = [len(w) for w in active.values()]
    gross = [sum(w.values()) for w in active.values()]
    tally: dict[str, int] = {}
    for weights in active.values():
        for symbol in weights:
            tally[symbol] = tally.get(symbol, 0) + 1
    scope = set(sessions) if sessions is not None else set(plan.weight_of)
    defensive = sum(1 for s, p in participate.items() if not p and s in scope)
    return {
        "sessions_with_shorts": len(active),
        "defensive_sessions": defensive,
        "coverage_of_defensive": len(active) / defensive if defensive else 0.0,
        "mean_names_when_on": sum(counts) / len(counts) if counts else 0.0,
        "max_names": max(counts) if counts else 0,
        "mean_proposed_gross_when_on": sum(gross) / len(gross) if gross else 0.0,
        "distinct_symbols": len(tally),
        "sessions_by_symbol": dict(sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def unavailable_entries(
    plan: ShortPlan, fraction: float, *, seed: str = "20260902"
) -> set[tuple[str, date]]:
    """§L8 forced-unavailability, deterministic and reproducible.

    A proposed (symbol, session) entry is unavailable iff a stable hash of
    (seed, symbol, session) falls in the bottom `fraction` of the hash space.
    No RNG state, no ordering dependence — the same scenario every run, and
    the same scenario for every candidate that proposes the same entry.
    """
    import hashlib

    blocked: set[tuple[str, date]] = set()
    if fraction <= 0.0:
        return blocked
    ceiling = int(fraction * (1 << 32))
    for session, weights in plan.weight_of.items():
        for symbol in weights:
            digest = hashlib.sha256(f"{seed}|{symbol}|{session}".encode()).digest()
            if int.from_bytes(digest[:4], "big") < ceiling:
                blocked.add((symbol, session))
    return blocked


def symbol_entries(plan: ShortPlan, symbol: str) -> set[tuple[str, date]]:
    """Every proposed entry for one symbol — the targeted-unavailability set."""
    return {(symbol, session) for session, weights in plan.weight_of.items() if symbol in weights}


def u10_members() -> tuple[str, ...]:
    return tuple(sorted(INCUMBENTS))


__all__ = [
    "COHORT_FRACTION",
    "GROSS_GRID",
    "MAX_SHORT_GROSS",
    "MAX_SHORT_NAMES",
    "MAX_SINGLE_SHORT",
    "CandidateError",
    "ShortPlan",
    "Targets",
    "apply_short_plan",
    "index_short_plan",
    "plan_diagnostics",
    "selected_short_plan",
    "symbol_entries",
    "u10_members",
    "unavailable_entries",
]
