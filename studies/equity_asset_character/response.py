"""Phase-4 regime × archetype response machinery (ledger §L6 + amendments).

Forward returns are measured on each symbol's SPY-paired session axis: the
h-session forward return from mark m runs from the last shared close before
m through the h-th shared close at or after m. The observation also records
the calendar session on which its window closes, so later phases can purge
estimates whose windows cross a fit date.

Pooled reporting uses lineage ids: cluster labels chained fit-to-fit by
nearest-centroid matching (dated amendment); a cluster matching no
predecessor starts a new lineage.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

from studies.equity_asset_character.archetypes import ArchetypeFit
from studies.equity_asset_character.fingerprints import SymbolSeries, _end_index

HORIZON_PRIMARY = 21
HORIZON_SECONDARY = 5

#: §L6 gates for the character-effect test (cell floor per the dated
#: amendment: presence in ≥ ⅔ of the regime's assigned marks, ≥ 100 obs).
CELL_MARK_PRESENCE = 2.0 / 3.0
CELL_MIN_OBSERVATIONS = 100
SPREAD_MIN_ANNUAL = 0.04

PERIODS_PER_YEAR = {HORIZON_PRIMARY: 252 / 21, HORIZON_SECONDARY: 252 / 5}


@dataclass(frozen=True)
class ForwardObservation:
    mark: date
    symbol: str
    horizon: int
    own_return: float  # forward log return
    spy_return: float
    window_closes: date  # calendar session on which the window's last return lands


def forward_observations(
    series: Mapping[str, SymbolSeries],
    marks: Sequence[date],
    horizon: int,
) -> list[ForwardObservation]:
    """One observation per (mark, symbol) with a full forward window."""
    out: list[ForwardObservation] = []
    for symbol in sorted(series):
        s = series[symbol]
        for mark in marks:
            start = _end_index(s.paired_sessions, mark)
            if start + horizon > len(s.paired_sessions):
                continue
            own = float(s.paired_own_returns[start : start + horizon].sum())
            spy = float(s.paired_spy_returns[start : start + horizon].sum())
            out.append(
                ForwardObservation(
                    mark=mark,
                    symbol=symbol,
                    horizon=horizon,
                    own_return=own,
                    spy_return=spy,
                    window_closes=s.paired_sessions[start + horizon - 1],
                )
            )
    return out


def lineage_maps(fits: Sequence[ArchetypeFit]) -> list[dict[int, int]]:
    """Per-fit mapping cluster label → lineage id (chained centroid match)."""
    ordered = sorted(fits, key=lambda fit: fit.fit_mark)
    maps: list[dict[int, int]] = []
    next_lineage = 0
    previous_centroids: np.ndarray | None = None
    previous_map: dict[int, int] = {}
    for fit in ordered:
        centroids = np.asarray(fit.centroids)
        mapping: dict[int, int] = {}
        if previous_centroids is None:
            for label in range(len(centroids)):
                mapping[label] = next_lineage
                next_lineage += 1
        else:
            pairs = sorted(
                (float(((c - p) ** 2).sum()), i, j)
                for i, c in enumerate(centroids)
                for j, p in enumerate(previous_centroids)
            )
            taken: set[int] = set()
            for _, i, j in pairs:
                if i in mapping or j in taken:
                    continue
                mapping[i] = previous_map[j]
                taken.add(j)
            for label in range(len(centroids)):
                if label not in mapping:
                    mapping[label] = next_lineage
                    next_lineage += 1
        maps.append(mapping)
        previous_centroids, previous_map = centroids, mapping
    return maps


def response_matrix(
    observations: Sequence[ForwardObservation],
    lineage_of: Mapping[tuple[date, str], int],
    regime_of: Mapping[date, str],
    horizon: int,
) -> dict[str, dict[str, dict[str, float]]]:
    """regime → lineage → stats over symbol-mark observations."""
    cells: dict[str, dict[int, list[ForwardObservation]]] = {}
    for obs in observations:
        lineage = lineage_of.get((obs.mark, obs.symbol))
        regime = regime_of.get(obs.mark)
        if lineage is None or regime is None:
            continue
        cells.setdefault(regime, {}).setdefault(lineage, []).append(obs)

    annualize = PERIODS_PER_YEAR[horizon]
    report: dict[str, dict[str, dict[str, float]]] = {}
    for regime, by_lineage in sorted(cells.items()):
        report[regime] = {}
        for lineage, rows in sorted(by_lineage.items()):
            own = np.array([r.own_return for r in rows])
            spy = np.array([r.spy_return for r in rows])
            excess = own - spy
            report[regime][str(lineage)] = {
                "mean_return_ann": float(own.mean() * annualize),
                "mean_excess_ann": float(excess.mean() * annualize),
                "vol_ann": float(own.std(ddof=1) * np.sqrt(annualize))
                if len(own) > 1
                else float("nan"),
                "downside_share": float((own < 0.0).mean()),
                "hit_rate_vs_spy": float((excess > 0.0).mean()),
                "observations": int(len(rows)),
                "distinct_marks": int(len({r.mark for r in rows})),
            }
    return report


def _annualized_cell_mean(
    observations: Sequence[ForwardObservation],
    lineage_of: Mapping[tuple[date, str], int],
    regime_of: Mapping[date, str],
    regime: str,
    lineage: str,
    horizon: int,
    *,
    exclude_symbol: str | None = None,
    exclude_year: int | None = None,
) -> float | None:
    values = [
        obs.own_return - obs.spy_return
        for obs in observations
        if regime_of.get(obs.mark) == regime
        and str(lineage_of.get((obs.mark, obs.symbol))) == lineage
        and obs.symbol != exclude_symbol
        and obs.mark.year != exclude_year
    ]
    if not values:
        return None
    return float(np.mean(values) * PERIODS_PER_YEAR[horizon])


def character_effect_test(
    observations: Sequence[ForwardObservation],
    lineage_of: Mapping[tuple[date, str], int],
    regime_of: Mapping[date, str],
    horizon: int,
    *,
    strongest_symbol: str,
) -> dict[str, object]:
    """The §L6 predeclared test.

    Per primary regime: the best-vs-worst archetype spread of mean annualized
    excess forward returns over qualified cells (≥ 30 distinct marks) must be
    ≥ 4 pts/yr, and the base best>worst ordering must survive (a) strongest-
    symbol removal, (b) every single-calendar-year removal, and (c) member
    leave-one-out on the best archetype (median LOO keeps ≥ half the spread).
    """
    matrix = response_matrix(observations, lineage_of, regime_of, horizon)
    # Assigned marks per regime: marks carrying any lineage assignment.
    assigned_marks: dict[str, set[date]] = {}
    for obs in observations:
        regime = regime_of.get(obs.mark)
        if regime is not None and (obs.mark, obs.symbol) in lineage_of:
            assigned_marks.setdefault(regime, set()).add(obs.mark)

    verdict: dict[str, object] = {}
    for regime, by_lineage in matrix.items():
        mark_floor = CELL_MARK_PRESENCE * len(assigned_marks.get(regime, ()))
        qualified = {
            lineage: stats
            for lineage, stats in by_lineage.items()
            if stats["distinct_marks"] >= mark_floor
            and stats["observations"] >= CELL_MIN_OBSERVATIONS
        }
        if len(qualified) < 2:
            verdict[regime] = {
                "qualified_cells": len(qualified),
                "passes_spread_gate": False,
                "ordering_survives_attacks": False,
            }
            continue
        means = {k: v["mean_excess_ann"] for k, v in qualified.items()}
        best = max(means, key=lambda k: means[k])
        worst = min(means, key=lambda k: means[k])
        spread = means[best] - means[worst]
        passes = spread >= SPREAD_MIN_ANNUAL

        def pair_spread(
            exclude_symbol=None, exclude_year=None, regime=regime, best=best, worst=worst
        ) -> float | None:
            top = _annualized_cell_mean(
                observations,
                lineage_of,
                regime_of,
                regime,
                best,
                horizon,
                exclude_symbol=exclude_symbol,
                exclude_year=exclude_year,
            )
            bottom = _annualized_cell_mean(
                observations,
                lineage_of,
                regime_of,
                regime,
                worst,
                horizon,
                exclude_symbol=exclude_symbol,
                exclude_year=exclude_year,
            )
            if top is None or bottom is None:
                return None
            return top - bottom

        # (a) strongest-symbol removal keeps the ordering.
        no_symbol = pair_spread(exclude_symbol=strongest_symbol)
        # (b) every single-year removal keeps the ordering.
        years = sorted({obs.mark.year for obs in observations})
        by_year = {year: pair_spread(exclude_year=year) for year in years}
        worst_year = min((v for v in by_year.values() if v is not None), default=None)
        # (c) LOO on the best archetype's members.
        best_members = sorted(
            {
                obs.symbol
                for obs in observations
                if regime_of.get(obs.mark) == regime
                and str(lineage_of.get((obs.mark, obs.symbol))) == best
            }
        )
        loo = [
            value
            for member in best_members
            if (value := pair_spread(exclude_symbol=member)) is not None
        ]
        loo_median = float(np.median(loo)) if loo else None

        survives = bool(
            passes
            and no_symbol is not None
            and no_symbol > 0.0
            and worst_year is not None
            and worst_year > 0.0
            and loo_median is not None
            and loo_median >= spread / 2.0
        )
        verdict[regime] = {
            "qualified_cells": len(qualified),
            "means": means,
            "best": best,
            "worst": worst,
            "spread": spread,
            "passes_spread_gate": bool(passes),
            "spread_without_strongest_symbol": no_symbol,
            "spread_by_year_removal": {str(y): v for y, v in by_year.items()},
            "min_spread_over_year_removals": worst_year,
            "loo_median_spread": loo_median,
            "best_member_count": len(best_members),
            "ordering_survives_attacks": survives,
        }
    return {
        "horizon": horizon,
        "strongest_symbol": strongest_symbol,
        "by_regime": verdict,
    }


__all__ = [
    "CELL_MARK_PRESENCE",
    "CELL_MIN_OBSERVATIONS",
    "HORIZON_PRIMARY",
    "HORIZON_SECONDARY",
    "SPREAD_MIN_ANNUAL",
    "ForwardObservation",
    "character_effect_test",
    "forward_observations",
    "lineage_maps",
    "response_matrix",
]
