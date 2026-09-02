"""Phase-2 machinery: does stable risk character predict forward downside
during DEFENSIVE regimes? (ledger §L4, amendment A1)

Everything here is causal by construction and the construction is the point:

- a characteristic for session ``s`` comes from the latest fingerprint mark
  ``m <= s - 1``, and the panel's own values at ``m`` were themselves computed
  from sessions strictly before ``m`` — two independent layers of lag;
- cross-sectional normalization and ranking happen **within one mark's row**,
  never across the sample;
- forward targets look strictly forward from ``s`` on the shared session axis;
- no archetype assignment, threshold, or feature statistic fitted on the
  whole sample is read anywhere.

Rank correlation is Pearson-on-ranks: the shared venv has no scipy, and
``Series.corr(method="spearman")`` would raise mid-run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

#: The ten characteristics the prior program's stability gate passed, plus the
#: two minimal causal state variables §L4 admits. No feature is added later.
STRUCTURAL: tuple[str, ...] = (
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
)
STATE: tuple[str, ...] = ("trend_dist", "rs_63")
CHARACTERISTICS: tuple[str, ...] = STRUCTURAL + STATE

#: Frozen forward horizons (sessions). 21 is primary.
HORIZONS: tuple[int, ...] = (5, 10, 21)
PRIMARY_HORIZON = 21

#: The forward tail threshold §L4 declares.
TAIL_THRESHOLD = -0.05

#: Bootstrap configuration, frozen before the first result.
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20260902

#: §L4.1 gate constants.
GATE_ABS_MEAN_RHO = 0.10
GATE_SIGN_CONSISTENCY = 0.60
GATE_INTERVAL = 0.90


class InformationError(Exception):
    """An information test that cannot be answered causally."""


def rank_corr(left: pd.Series, right: pd.Series) -> float:
    """Spearman as Pearson-on-ranks over the pairwise-complete rows."""
    frame = pd.DataFrame({"a": left, "b": right}).dropna()
    if len(frame) < 4:
        return float("nan")
    a = frame["a"].rank()
    b = frame["b"].rank()
    if a.std() == 0 or b.std() == 0:
        return float("nan")
    return float(a.corr(b))


@dataclass(frozen=True)
class ForwardTargets:
    """The five §L4 targets for one horizon, sessions × symbols."""

    horizon: int
    fwd_ret: pd.DataFrame
    fwd_exc: pd.DataFrame
    fwd_mdd: pd.DataFrame
    fwd_tail: pd.DataFrame
    fwd_crash: pd.DataFrame

    def as_map(self) -> dict[str, pd.DataFrame]:
        return {
            "fwd_ret": self.fwd_ret,
            "fwd_exc": self.fwd_exc,
            "fwd_mdd": self.fwd_mdd,
            "fwd_tail": self.fwd_tail,
            "fwd_crash": self.fwd_crash,
        }


def forward_targets(
    closes: pd.DataFrame, horizon: int, *, benchmark: str = "SPY"
) -> ForwardTargets:
    """Strictly-forward targets from each session's close over `horizon`.

    All five are measured on the shared session axis of `closes` (its index),
    so a symbol missing a session simply carries NaN there rather than being
    silently paired with a different date.
    """
    if benchmark not in closes.columns:
        raise InformationError(f"Benchmark {benchmark} absent from the close table.")
    if horizon < 1:
        raise InformationError(f"Horizon {horizon} must be positive.")

    values = closes.sort_index()
    ahead = values.shift(-horizon)
    fwd_ret = ahead / values - 1.0
    bench = fwd_ret[benchmark]
    fwd_exc = fwd_ret.sub(bench, axis=0)

    # Worst peak-to-trough close drawdown strictly inside the forward window.
    array = values.to_numpy(dtype="float64")
    rows, cols = array.shape
    mdd = np.full((rows, cols), np.nan)
    crash = np.full((rows, cols), np.nan)
    bench_index = list(values.columns).index(benchmark)
    bench_returns = array[:, bench_index]
    for i in range(rows):
        stop = i + horizon
        if stop >= rows:
            break
        window = array[i : stop + 1, :]
        if np.isnan(window).any(axis=0).all():
            continue
        running = np.fmax.accumulate(window, axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            underwater = window / running - 1.0
        mdd[i, :] = np.nanmin(underwater, axis=0)
        # The single worst SPY session inside the window, and every symbol's
        # own return on exactly that session.
        bench_window = bench_returns[i : stop + 1]
        with np.errstate(invalid="ignore", divide="ignore"):
            bench_steps = bench_window[1:] / bench_window[:-1] - 1.0
        if np.all(np.isnan(bench_steps)):
            continue
        worst = int(np.nanargmin(bench_steps))
        with np.errstate(invalid="ignore", divide="ignore"):
            crash[i, :] = window[worst + 1, :] / window[worst, :] - 1.0

    frame = lambda data: pd.DataFrame(data, index=values.index, columns=values.columns)  # noqa: E731
    return ForwardTargets(
        horizon=horizon,
        fwd_ret=fwd_ret,
        fwd_exc=fwd_exc,
        fwd_mdd=frame(mdd),
        fwd_tail=(fwd_ret <= TAIL_THRESHOLD).where(fwd_ret.notna()).astype("float64"),
        fwd_crash=frame(crash),
    )


def governing_mark_of(sessions: Sequence[date], marks: Sequence[date]) -> dict[date, date]:
    """The latest mark at or before session ``s - 1`` (§L4 causality).

    The extra session of lag is deliberate and is the second of the two layers:
    the panel's own values at a mark already exclude that mark's session, and
    this refuses to read a mark whose date is the session being decided.
    """
    ordered_marks = sorted(marks)
    ordered_sessions = sorted(sessions)
    mapping: dict[date, date] = {}
    cursor = -1
    for index, session in enumerate(ordered_sessions):
        if index == 0:
            continue
        previous = ordered_sessions[index - 1]
        while cursor + 1 < len(ordered_marks) and ordered_marks[cursor + 1] <= previous:
            cursor += 1
        if cursor >= 0:
            mapping[session] = ordered_marks[cursor]
    return mapping


def panel_at(panel: pd.DataFrame, mark: date, universe: Sequence[str]) -> pd.DataFrame:
    """One mark's cross-section, restricted to `universe`, indexed by symbol."""
    rows = panel[panel["mark"] == pd.Timestamp(mark).date().isoformat()]
    if rows.empty:
        rows = panel[panel["mark"] == mark]
    if rows.empty:
        rows = panel[panel["mark"].astype(str) == str(mark)]
    frame = rows.set_index("symbol")
    return frame.reindex([s for s in universe if s in frame.index])


def bootstrap_mean(values: Sequence[float], *, seed: int = BOOTSTRAP_SEED) -> dict[str, float]:
    """Percentile bootstrap of the mean over evaluation events (clusters)."""
    clean = np.array([v for v in values if np.isfinite(v)], dtype="float64")
    if clean.size == 0:
        return {"mean": float("nan"), "lo": float("nan"), "hi": float("nan"), "n": 0}
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, clean.size, size=(BOOTSTRAP_RESAMPLES, clean.size))
    means = clean[draws].mean(axis=1)
    tail = (1.0 - GATE_INTERVAL) / 2.0
    return {
        "mean": float(clean.mean()),
        "lo": float(np.quantile(means, tail)),
        "hi": float(np.quantile(means, 1.0 - tail)),
        "n": int(clean.size),
    }


def cluster_bootstrap_mean(
    values: Sequence[float], clusters: Sequence[object], *, seed: int = BOOTSTRAP_SEED
) -> dict[str, float]:
    """Bootstrap resampling whole clusters, not observations.

    Overlapping forward windows inside one DEFENSIVE run are one event, not
    many; resampling runs is what keeps the interval honest.
    """
    pairs = [(c, v) for c, v in zip(clusters, values, strict=True) if np.isfinite(v)]
    if not pairs:
        return {"mean": float("nan"), "lo": float("nan"), "hi": float("nan"), "n": 0, "clusters": 0}
    grouped: dict[object, list[float]] = {}
    for cluster, value in pairs:
        grouped.setdefault(cluster, []).append(value)
    keys = sorted(grouped, key=str)
    per_cluster = np.array([float(np.mean(grouped[k])) for k in keys])
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, per_cluster.size, size=(BOOTSTRAP_RESAMPLES, per_cluster.size))
    means = per_cluster[draws].mean(axis=1)
    tail = (1.0 - GATE_INTERVAL) / 2.0
    return {
        "mean": float(per_cluster.mean()),
        "lo": float(np.quantile(means, tail)),
        "hi": float(np.quantile(means, 1.0 - tail)),
        "n": len(pairs),
        "clusters": int(per_cluster.size),
    }


def gate_l41(by_horizon: Mapping[int, Mapping[str, float]]) -> dict[str, object]:
    """The §L4.1 gate, applied exactly as declared.

    A characteristic passes iff at H = 21 the mean rank correlation with
    `fwd_ret` reaches ±0.10 in a direction a short rule could use, the 90 %
    bootstrap interval for that mean excludes zero, sign-consistency across
    qualifying marks is >= 0.60, and the same sign holds at H = 5 and H = 10.
    """
    primary = by_horizon.get(PRIMARY_HORIZON)
    if primary is None:
        return {"pass": False, "reason": "no primary-horizon result"}
    mean = float(primary["mean"])
    lo, hi = float(primary["lo"]), float(primary["hi"])
    consistency = float(primary["sign_consistency"])
    reasons: list[str] = []

    magnitude = abs(mean) >= GATE_ABS_MEAN_RHO
    if not magnitude:
        reasons.append(f"|mean rho| {abs(mean):.3f} < {GATE_ABS_MEAN_RHO}")
    excludes_zero = (lo > 0.0 and hi > 0.0) or (lo < 0.0 and hi < 0.0)
    if not excludes_zero:
        reasons.append(f"90% interval [{lo:.3f}, {hi:.3f}] contains zero")
    consistent = consistency >= GATE_SIGN_CONSISTENCY
    if not consistent:
        reasons.append(f"sign consistency {consistency:.3f} < {GATE_SIGN_CONSISTENCY}")
    same_sign = True
    for horizon in HORIZONS:
        if horizon == PRIMARY_HORIZON:
            continue
        other = by_horizon.get(horizon)
        if (
            other is None
            or not np.isfinite(other["mean"])
            or np.sign(other["mean"]) != np.sign(mean)
        ):
            same_sign = False
            reasons.append(f"H={horizon} sign differs")
    passed = magnitude and excludes_zero and consistent and same_sign
    return {
        "pass": bool(passed),
        "direction": ("short_the_high" if mean < 0 else "short_the_low") if passed else None,
        "mean_rho_primary": mean,
        "interval": [lo, hi],
        "sign_consistency": consistency,
        "failures": reasons,
    }


__all__ = [
    "BOOTSTRAP_RESAMPLES",
    "BOOTSTRAP_SEED",
    "CHARACTERISTICS",
    "GATE_ABS_MEAN_RHO",
    "GATE_SIGN_CONSISTENCY",
    "HORIZONS",
    "PRIMARY_HORIZON",
    "STATE",
    "STRUCTURAL",
    "TAIL_THRESHOLD",
    "ForwardTargets",
    "InformationError",
    "bootstrap_mean",
    "cluster_bootstrap_mean",
    "forward_targets",
    "gate_l41",
    "governing_mark_of",
    "panel_at",
    "rank_corr",
]
