"""A1 archetype allocation and A2 individual soft tilt (ledger §L8, §L9).

Weight construction, per session s of the scored region:

- the governing mark is the latest rebalance mark ≤ s; the governing fit is
  the latest frozen archetype fit ≤ that mark;
- every symbol starts from the base weight w0 = min(1/M, cap);
- archetype multipliers (per §L8 scheme) and, for A2, the individual state
  multiplier scale w0; the vector is then renormalized so its total equals
  the base configuration's total (M × w0), and re-capped at 0.10 with the
  residual left in cash (no iterative refill — the AL-C convention);
- PARTICIPATE sessions trade the tilted active weights; DEFENSIVE sessions
  hold per-symbol reserved weights × the bar-level V3 stance (equal for
  A1-B/A1-P; tilted by the DEFENSIVE-conditional scheme for A1-R).

Symbols without an archetype at the governing mark (pre-initial-fit, or NaN
fingerprints) take multiplier 1 — the defensive-until-evidence analogue.

Causality: multipliers at mark m derive from (a) the governing fit's frozen
training medians (A1-B), or (b) response estimates over past marks whose
21-session forward windows close strictly before the governing fit date
(A1-P / A1-R, the purge amendment), with those past marks labelled by the
governing fit's frozen centroids.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from studies.equity_asset_character import REPORT_ROOT
from studies.equity_asset_character.response import (
    HORIZON_PRIMARY,
    PERIODS_PER_YEAR,
    ForwardObservation,
)

PER_SYMBOL_CAP = 0.10
MULT_CLIP = (0.6, 1.4)
TILT_LAMBDA = 0.5
A2_BAND = (0.85, 1.15)
A2_SHRINK = 3.0

A1_SCHEMES = ("A1_B", "A1_P", "A1_R")
A2_COMPOSITES = ("A2_M", "A2_Q")


class AllocationError(Exception):
    """An allocation request that cannot be honoured causally."""


def governing_marks(sessions: Sequence[date], marks: Sequence[date]) -> dict[date, date]:
    """session → latest mark ≤ session (marks are region sessions)."""
    out: dict[date, date] = {}
    ordered = sorted(marks)
    index = -1
    for session in sorted(sessions):
        while index + 1 < len(ordered) and ordered[index + 1] <= session:
            index += 1
        if index >= 0:
            out[session] = ordered[index]
    return out


def load_fit_records() -> list[dict]:
    return json.loads((Path(REPORT_ROOT) / "phase3" / "fits.json").read_text())["fits"]


def governing_fit(fit_records: Sequence[dict], mark: date) -> dict | None:
    chosen = None
    for record in sorted(fit_records, key=lambda r: r["fit_mark"]):
        if date.fromisoformat(record["fit_mark"]) <= mark:
            chosen = record
    return chosen


def retro_labels(
    fit_record: dict,
    z_panel: pd.DataFrame,
    mark: date,
) -> dict[str, int]:
    """Assign symbols at one (past or current) mark with a fit's frozen
    centroids — used both for live assignment and purged response estimation."""
    centroids = np.asarray(fit_record["centroids_z"], dtype="float64")
    features = list(fit_record["features"])
    labels: dict[str, int] = {}
    try:
        block = z_panel.loc[mark]
    except KeyError:
        return labels
    for symbol, row in block[features].iterrows():
        vector = row.to_numpy(dtype="float64")
        if np.isnan(vector).any():
            continue
        labels[symbol] = int(((centroids - vector) ** 2).sum(axis=1).argmin())
    return labels


def response_estimates(
    fit_record: dict,
    z_panel: pd.DataFrame,
    observations: Sequence[ForwardObservation],
    regime_of: Mapping[date, str],
) -> dict[str, dict[int, float]]:
    """regime → label → mean annualized 21-session forward excess, purged.

    Training marks: forward window closes strictly before the fit date;
    labels: the fit's frozen centroids applied to each mark's own z-scores.
    """
    fit_mark = date.fromisoformat(fit_record["fit_mark"])
    label_cache: dict[date, dict[str, int]] = {}
    sums: dict[str, dict[int, list[float]]] = {}
    for obs in observations:
        if obs.horizon != HORIZON_PRIMARY or obs.window_closes >= fit_mark:
            continue
        regime = regime_of.get(obs.mark)
        if regime is None:
            continue
        if obs.mark not in label_cache:
            label_cache[obs.mark] = retro_labels(fit_record, z_panel, obs.mark)
        label = label_cache[obs.mark].get(obs.symbol)
        if label is None:
            continue
        sums.setdefault(regime, {}).setdefault(label, []).append(obs.own_return - obs.spy_return)
    annualize = PERIODS_PER_YEAR[HORIZON_PRIMARY]
    return {
        regime: {
            label: float(np.mean(values) * annualize)
            for label, values in by_label.items()
            if values
        }
        for regime, by_label in sums.items()
    }


def _clip(value: float, bounds: tuple[float, float]) -> float:
    return float(min(max(value, bounds[0]), bounds[1]))


def archetype_multipliers(
    scheme: str,
    fit_record: dict,
    regime: str,
    estimates: Mapping[str, Mapping[int, float]],
) -> dict[int, float]:
    """label → multiplier for one scheme in one regime (§L8)."""
    k = len(fit_record["centroids_z"])
    if scheme == "A1_B":
        if regime != "PARTICIPATE":
            return dict.fromkeys(range(k), 1.0)
        medians = {
            int(label): stats["beta_252"]
            for label, stats in fit_record["raw_feature_medians"].items()
        }
        mean = float(np.mean(list(medians.values())))
        if mean <= 0.0:
            return dict.fromkeys(range(k), 1.0)
        return {label: _clip(value / mean, MULT_CLIP) for label, value in medians.items()}
    if scheme == "A1_P" and regime != "PARTICIPATE":
        return dict.fromkeys(range(k), 1.0)
    by_label = estimates.get(regime, {})
    values = [by_label.get(label) for label in range(k)]
    known = [v for v in values if v is not None]
    if len(known) < 2 or float(np.std(known, ddof=1)) <= 0.0:
        return dict.fromkeys(range(k), 1.0)
    mean = float(np.mean(known))
    std = float(np.std(known, ddof=1))
    out: dict[int, float] = {}
    for label in range(k):
        value = by_label.get(label)
        if value is None:
            out[label] = 1.0
        else:
            out[label] = _clip(1.0 + TILT_LAMBDA * (value - mean) / std, MULT_CLIP)
    return out


def state_multipliers(
    composite: str,
    z_state: pd.DataFrame,
    mark: date,
    symbols: Sequence[str],
    *,
    band: tuple[float, float] = A2_BAND,
) -> dict[str, float]:
    """symbol → individual multiplier for one A2 composite (§L9)."""
    feature = "rs_63" if composite == "A2_M" else "vol_ratio"
    sign = 1.0 if composite == "A2_M" else -1.0
    out: dict[str, float] = {}
    try:
        block = z_state.loc[mark]
    except KeyError:
        return dict.fromkeys(symbols, 1.0)
    for symbol in symbols:
        value = block[feature].get(symbol, float("nan")) if feature in block else float("nan")
        if pd.isna(value):
            out[symbol] = 1.0
        else:
            out[symbol] = _clip(1.0 + sign * float(value) / A2_SHRINK, band)
    return out


def tilted_weights(
    symbols: Sequence[str],
    multiplier_of: Mapping[str, float],
) -> dict[str, float]:
    """Base × multiplier, renormalized to the base total, re-capped (§L8)."""
    ordered = sorted(symbols)
    m = len(ordered)
    base = min(1.0 / m, PER_SYMBOL_CAP)
    total_target = base * m
    raw = {symbol: base * float(multiplier_of.get(symbol, 1.0)) for symbol in ordered}
    raw_total = sum(raw.values())
    if raw_total <= 0.0:
        return dict.fromkeys(ordered, base)
    scale = total_target / raw_total
    return {symbol: min(value * scale, PER_SYMBOL_CAP) for symbol, value in raw.items()}


def build_targets_tilted(
    frames: Mapping[str, pd.DataFrame],
    region_sessions: Sequence[date],
    participate: Mapping[date, bool],
    stance: Mapping[str, Mapping[pd.Timestamp, int]],
    *,
    active_weight_of: Mapping[date, Mapping[str, float]],
    reserved_weight_of: Mapping[date, Mapping[str, float]],
) -> dict[str, dict[pd.Timestamp, float]]:
    """`build_targets` with per-symbol reserved weights (A1-R needs them)."""
    from autotrader.equity.session import market_date

    session_set = set(region_sessions)
    targets: dict[str, dict[pd.Timestamp, float]] = {}
    for symbol in sorted(frames):
        frame = frames[symbol]
        symbol_stance = stance.get(symbol, {})
        series: dict[pd.Timestamp, float] = {}
        for ts in frame["timestamp"]:
            stamp = pd.Timestamp(ts)
            session = market_date(stamp.to_pydatetime())
            if session not in session_set:
                continue
            if participate[session]:
                weight = float(active_weight_of[session].get(symbol, 0.0))
            else:
                reserved = float(reserved_weight_of[session].get(symbol, 0.0))
                weight = reserved * float(symbol_stance.get(stamp, 0))
            series[stamp] = weight
        targets[symbol] = series
    return targets


__all__ = [
    "A1_SCHEMES",
    "A2_BAND",
    "A2_COMPOSITES",
    "A2_SHRINK",
    "MULT_CLIP",
    "PER_SYMBOL_CAP",
    "TILT_LAMBDA",
    "AllocationError",
    "archetype_multipliers",
    "build_targets_tilted",
    "governing_fit",
    "governing_marks",
    "load_fit_records",
    "response_estimates",
    "retro_labels",
    "state_multipliers",
    "tilted_weights",
]
