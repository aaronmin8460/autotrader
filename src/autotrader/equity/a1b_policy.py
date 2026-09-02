"""Frozen A1-B archetype allocation policy for the U30 observation stream.

Everything here is a pure, deterministic function of (a) the packaged policy
artifact `a1b_policy.json` — the asset-character research program's frozen
walk-forward archetype fits, surviving structural features, universe
manifests, and bounds, reused byte-for-byte — and (b) completed-session bar
history. Nothing here fetches, records, sizes an order, or holds any state.

The numerics are ports of the validated research implementations
(`fingerprints.structural_at`, `cross_sectional_z`, `retro_labels`,
`archetype_multipliers`, `tilted_weights`), kept semantically identical so a
value computed live can be compared 1:1 against the research pipeline on the
same bars (the parity requirement of the two-sleeve program's ledger §L15):

- structural fingerprints at a mark use completed sessions strictly before
  the mark, windows counted on the symbol's own observed-session axis,
  market-relative features on the dates the symbol shares with the reference
  symbol;
- fingerprints are z-scored cross-sectionally over the frozen 45-name
  research cross-section at that mark only (winsorized), never over time;
- a symbol's archetype is the governing frozen fit's nearest centroid in
  z-space; symbols with any NaN feature take no archetype (multiplier 1);
- the A1-B multiplier of an archetype is its training-median market beta
  over the cross-archetype mean of those medians, clipped to [0.6, 1.4],
  applied during PARTICIPATE only; weights renormalize to the equal-weight
  total and re-cap at 0.10, residual to cash;
- DEFENSIVE sessions hold equal reserved weight × the per-bar V3 stance —
  handled by the runtime, not here.

Marks are every 21st session from the research anchor (2021-09-30), counted
on the reference symbol's observed-session axis, exactly the research
rebalance grid extended forward in time.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from importlib import resources

import numpy as np
import pandas as pd

from autotrader.equity.session import market_date

#: Structural windows, exactly the research constants.
WINDOW_6M = 126
WINDOW_12M = 252
MIN_UP_DOWN_SESSIONS = 60
MIN_NEGATIVE_SESSIONS = 30
ANNUALIZE = 252


class A1BPolicyError(Exception):
    """A policy request that cannot be answered causally or deterministically."""


@dataclass(frozen=True)
class A1BFit:
    """One frozen walk-forward archetype fit."""

    fit_mark: date
    k: int
    features: tuple[str, ...]
    centroids_z: tuple[tuple[float, ...], ...]
    beta_median_of_label: dict[int, float]


@dataclass(frozen=True)
class A1BPolicy:
    """The packaged policy artifact, parsed and hashed."""

    u30: tuple[str, ...]
    u45_z_cross_section: tuple[str, ...]
    incumbents: tuple[str, ...]
    surviving_features: tuple[str, ...]
    fits: tuple[A1BFit, ...]
    mark_anchor: date
    #: The research grid's FINAL mark. The research session axis (the
    #: reference symbol's observed sessions) lacks one exchange session the
    #: broker calendar has, so a naive calendar count from the anchor lands
    #: one session early late in the region. Live marks therefore anchor
    #: here — a date both axes agree on by construction — and count calendar
    #: sessions forward.
    grid_reference_mark: date
    mark_every_sessions: int
    mult_clip: tuple[float, float]
    per_symbol_cap: float
    z_winsor: float
    z_min_symbols: int
    policy_hash: str


@lru_cache(maxsize=1)
def load_policy() -> A1BPolicy:
    """Parse the packaged artifact; the hash covers its canonical JSON form."""
    raw = resources.files("autotrader.equity").joinpath("a1b_policy.json").read_text()
    data = json.loads(raw)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    policy_hash = hashlib.sha256(canonical.encode()).hexdigest()
    fits = []
    for record in sorted(data["fits"], key=lambda r: r["fit_mark"]):
        fits.append(
            A1BFit(
                fit_mark=date.fromisoformat(record["fit_mark"]),
                k=int(record["k"]),
                features=tuple(record["features"]),
                centroids_z=tuple(tuple(float(v) for v in row) for row in record["centroids_z"]),
                beta_median_of_label={
                    int(label): float(stats["beta_252"])
                    for label, stats in record["raw_feature_medians"].items()
                },
            )
        )
    return A1BPolicy(
        u30=tuple(data["u30"]),
        u45_z_cross_section=tuple(data["u45_z_cross_section"]),
        incumbents=tuple(data["incumbents"]),
        surviving_features=tuple(data["surviving_features"]),
        fits=tuple(fits),
        mark_anchor=date.fromisoformat(data["mark_anchor"]),
        grid_reference_mark=date.fromisoformat(data["grid_reference_mark"]),
        mark_every_sessions=int(data["mark_every_sessions"]),
        mult_clip=(float(data["mult_clip"][0]), float(data["mult_clip"][1])),
        per_symbol_cap=float(data["per_symbol_cap"]),
        z_winsor=float(data["z_winsor"]),
        z_min_symbols=int(data["z_min_symbols"]),
        policy_hash=policy_hash,
    )


def governing_fit(policy: A1BPolicy, mark: date) -> A1BFit | None:
    """The latest frozen fit at or before `mark`; None before the first."""
    chosen: A1BFit | None = None
    for fit in policy.fits:
        if fit.fit_mark <= mark:
            chosen = fit
    return chosen


def governing_mark(policy: A1BPolicy, sessions_since_reference: int) -> int:
    """Offset (on the reference-anchored session axis) of the governing mark
    for the session `sessions_since_reference` sessions after the grid
    reference mark (the reference itself = 0)."""
    if sessions_since_reference < 0:
        raise A1BPolicyError("The governing session precedes the grid reference mark.")
    return (sessions_since_reference // policy.mark_every_sessions) * policy.mark_every_sessions


# ----------------------------------------------------------------------
# Fingerprints (ports of the validated research numerics)
# ----------------------------------------------------------------------


def symbol_sessions(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per session: date, first open, last close, dollar volume."""
    if frame.empty:
        raise A1BPolicyError("Cannot derive sessions from an empty frame.")
    days = [market_date(ts.to_pydatetime()) for ts in frame["timestamp"]]
    working = pd.DataFrame(
        {
            "session": days,
            "open": frame["open"].to_numpy(dtype="float64"),
            "close": frame["close"].to_numpy(dtype="float64"),
            "notional": (
                frame["close"].to_numpy(dtype="float64") * frame["volume"].to_numpy(dtype="float64")
            ),
        }
    )
    grouped = working.groupby("session", sort=True)
    return pd.DataFrame(
        {
            "session": list(grouped.groups),
            "open": grouped["open"].first().to_numpy(),
            "close": grouped["close"].last().to_numpy(),
            "dollar_volume": grouped["notional"].sum().to_numpy(),
        }
    )


@dataclass(frozen=True)
class SymbolSeries:
    """Per-symbol arrays used by every fingerprint, built once."""

    sessions: np.ndarray
    opens: np.ndarray
    closes: np.ndarray
    dollar_volume: np.ndarray
    paired_sessions: np.ndarray
    paired_own_returns: np.ndarray
    paired_reference_returns: np.ndarray


def build_series(table: pd.DataFrame, reference_table: pd.DataFrame) -> SymbolSeries:
    """Assemble the raw arrays for one symbol from its session table."""
    sessions = np.asarray(table["session"].tolist(), dtype=object)
    closes = table["close"].to_numpy(dtype="float64")
    reference_map = dict(
        zip(
            reference_table["session"].tolist(),
            reference_table["close"].to_numpy(dtype="float64"),
            strict=True,
        )
    )
    shared_mask = np.array([day in reference_map for day in sessions], dtype=bool)
    shared_dates = sessions[shared_mask]
    own_shared = closes[shared_mask]
    reference_shared = np.array([reference_map[day] for day in shared_dates], dtype="float64")
    return SymbolSeries(
        sessions=sessions,
        opens=table["open"].to_numpy(dtype="float64"),
        closes=closes,
        dollar_volume=table["dollar_volume"].to_numpy(dtype="float64"),
        paired_sessions=shared_dates[1:],
        paired_own_returns=np.diff(np.log(own_shared)),
        paired_reference_returns=np.diff(np.log(reference_shared)),
    )


def _end_index(sessions: np.ndarray, mark: date) -> int:
    return int(np.searchsorted(sessions, mark, side="left"))


def _ols_beta(own: np.ndarray, reference: np.ndarray) -> float:
    reference_mean = reference.mean()
    own_mean = own.mean()
    var = float(((reference - reference_mean) ** 2).sum())
    if var <= 0.0:
        return float("nan")
    return float(((reference - reference_mean) * (own - own_mean)).sum() / var)


def structural_at(series: SymbolSeries, mark: date) -> dict[str, float]:
    """The ten surviving structural fingerprints for one symbol at one mark."""
    out: dict[str, float] = dict.fromkeys(
        (
            "beta_252",
            "up_beta_252",
            "down_beta_252",
            "vol_126",
            "downside_vol_126",
            "vol_of_vol_126",
            "gap_vol_126",
            "maxdd_252",
            "underwater_252",
            "dollar_vol_126",
        ),
        float("nan"),
    )

    pend = _end_index(series.paired_sessions, mark)
    if pend >= WINDOW_12M:
        own = series.paired_own_returns[pend - WINDOW_12M : pend]
        reference = series.paired_reference_returns[pend - WINDOW_12M : pend]
        out["beta_252"] = _ols_beta(own, reference)
        up = reference > 0.0
        down = reference < 0.0
        if int(up.sum()) >= MIN_UP_DOWN_SESSIONS:
            out["up_beta_252"] = _ols_beta(own[up], reference[up])
        if int(down.sum()) >= MIN_UP_DOWN_SESSIONS:
            out["down_beta_252"] = _ols_beta(own[down], reference[down])

    if pend >= WINDOW_6M:
        r = series.paired_own_returns[pend - WINDOW_6M : pend]
        out["vol_126"] = float(r.std(ddof=1)) * float(np.sqrt(ANNUALIZE))
        negative = r[r < 0.0]
        if len(negative) >= MIN_NEGATIVE_SESSIONS:
            out["downside_vol_126"] = float(negative.std(ddof=1)) * float(np.sqrt(ANNUALIZE))
    if pend >= WINDOW_6M + 20:
        r = series.paired_own_returns[pend - (WINDOW_6M + 20) : pend]
        rolling = np.array([r[i : i + 21].std(ddof=1) for i in range(len(r) - 20)], dtype="float64")
        out["vol_of_vol_126"] = float(rolling.std(ddof=1)) * float(np.sqrt(ANNUALIZE))

    end = _end_index(series.sessions, mark)
    if end >= WINDOW_6M + 1:
        opens = series.opens[end - WINDOW_6M : end]
        prior_closes = series.closes[end - WINDOW_6M - 1 : end - 1]
        gaps = np.log(opens / prior_closes)
        out["gap_vol_126"] = float(gaps.std(ddof=1)) * float(np.sqrt(ANNUALIZE))
    if end >= WINDOW_6M:
        out["dollar_vol_126"] = float(
            np.log10(np.median(series.dollar_volume[end - WINDOW_6M : end]))
        )
    if end >= WINDOW_12M:
        closes = series.closes[end - WINDOW_12M : end]
        peak = np.maximum.accumulate(closes)
        drawdown = closes / peak - 1.0
        out["maxdd_252"] = float(drawdown.min())
        out["underwater_252"] = float((drawdown < -0.05).mean())
    return out


def cross_sectional_z_at_mark(
    values_by_symbol: dict[str, dict[str, float]],
    features: tuple[str, ...],
    *,
    winsor: float,
    min_symbols: int,
) -> dict[str, dict[str, float]]:
    """Z-score each feature across the cross-section at one mark.

    Identical semantics to the research `cross_sectional_z` restricted to a
    single mark: only contemporaneous values, sample std (ddof=1), winsorized;
    a feature with too few non-NaN symbols or zero variance goes NaN for all.
    """
    out: dict[str, dict[str, float]] = {
        symbol: dict.fromkeys(features, float("nan")) for symbol in values_by_symbol
    }
    for feature in features:
        pairs = [
            (symbol, values[feature])
            for symbol, values in values_by_symbol.items()
            if np.isfinite(values.get(feature, float("nan")))
        ]
        if len(pairs) < min_symbols:
            continue
        sample = np.array([value for _, value in pairs], dtype="float64")
        std = float(sample.std(ddof=1))
        if std <= 0.0:
            continue
        mean = float(sample.mean())
        for symbol, value in pairs:
            z = (value - mean) / std
            out[symbol][feature] = float(min(max(z, -winsor), winsor))
    return out


# ----------------------------------------------------------------------
# Archetype assignment and A1-B weights
# ----------------------------------------------------------------------


def assign_labels(fit: A1BFit, z_by_symbol: dict[str, dict[str, float]]) -> dict[str, int]:
    """Nearest frozen centroid per symbol; symbols with any NaN take no label."""
    centroids = np.asarray(fit.centroids_z, dtype="float64")
    labels: dict[str, int] = {}
    for symbol in sorted(z_by_symbol):
        vector = np.array(
            [z_by_symbol[symbol].get(feature, float("nan")) for feature in fit.features],
            dtype="float64",
        )
        if np.isnan(vector).any():
            continue
        labels[symbol] = int(((centroids - vector) ** 2).sum(axis=1).argmin())
    return labels


def a1b_multipliers(policy: A1BPolicy, fit: A1BFit) -> dict[int, float]:
    """label → PARTICIPATE multiplier: training-median beta over the mean."""
    medians = dict(fit.beta_median_of_label)
    mean = float(np.mean(list(medians.values())))
    if mean <= 0.0:
        return dict.fromkeys(range(fit.k), 1.0)
    low, high = policy.mult_clip
    return {
        label: float(min(max(value / mean, low), high)) for label, value in sorted(medians.items())
    }


def tilted_weights(
    symbols: tuple[str, ...],
    multiplier_of: dict[str, float],
    *,
    cap: float,
) -> dict[str, float]:
    """Base × multiplier, renormalized to the base total, re-capped."""
    ordered = sorted(symbols)
    m = len(ordered)
    base = min(1.0 / m, cap)
    total_target = base * m
    raw = {symbol: base * float(multiplier_of.get(symbol, 1.0)) for symbol in ordered}
    raw_total = sum(raw.values())
    if raw_total <= 0.0:
        return dict.fromkeys(ordered, base)
    scale = total_target / raw_total
    return {symbol: min(value * scale, cap) for symbol, value in raw.items()}


def mark_weights(
    policy: A1BPolicy,
    fit: A1BFit | None,
    z_by_symbol: dict[str, dict[str, float]],
) -> tuple[dict[str, float], dict[str, float], dict[str, int]]:
    """(active weights, reserved weights, labels) for one governing mark.

    Before the first frozen fit — or for symbols without a label — the
    multiplier is 1: the equal-weight, defensive-until-evidence default the
    research validated.
    """
    if fit is None:
        equal = tilted_weights(policy.u30, {}, cap=policy.per_symbol_cap)
        return equal, dict(equal), {}
    labels = assign_labels(fit, z_by_symbol)
    mults = a1b_multipliers(policy, fit)
    multiplier_of = {
        symbol: mults.get(labels[symbol], 1.0) for symbol in policy.u30 if symbol in labels
    }
    active = tilted_weights(policy.u30, multiplier_of, cap=policy.per_symbol_cap)
    reserved = tilted_weights(policy.u30, {}, cap=policy.per_symbol_cap)
    return active, reserved, {s: labels[s] for s in policy.u30 if s in labels}


__all__ = [
    "ANNUALIZE",
    "MIN_NEGATIVE_SESSIONS",
    "MIN_UP_DOWN_SESSIONS",
    "WINDOW_6M",
    "WINDOW_12M",
    "A1BFit",
    "A1BPolicy",
    "A1BPolicyError",
    "SymbolSeries",
    "a1b_multipliers",
    "assign_labels",
    "build_series",
    "cross_sectional_z_at_mark",
    "governing_fit",
    "governing_mark",
    "load_policy",
    "mark_weights",
    "structural_at",
    "symbol_sessions",
    "tilted_weights",
]
