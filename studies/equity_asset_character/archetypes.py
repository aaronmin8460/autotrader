"""Phase-3 archetype discovery (ledger §L5).

Deterministic numpy clustering — no sklearn, matching the repository's
numpy-native ML convention. KMeans (k-means++ init, fixed seed, 50 restarts)
is the primary method; Ward agglomerative is a robustness check only.

Causality: a fit at mark F consumes cross-sectional z-scores at marks
strictly before F; centroids and the feature list freeze at F and govern all
assignments until the next fit. Assignments at mark m use m's own
(contemporaneous, causal) z-scores against the frozen centroids.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

KMEANS_SEED = 0
KMEANS_RESTARTS = 50
KMEANS_MAX_ITER = 300
K_CANDIDATES: tuple[int, ...] = (3, 4, 5, 6)
MIN_CLUSTER_MEMBERS = 3
MIN_TRAIN_MARKS = 6

#: Walk-forward fit schedule (§L5 + dated amendment): initial fit at the
#: first mark ≥ this date (the earliest with MIN_TRAIN_MARKS complete
#: 252-fingerprint training marks), then the first mark of each later year.
INITIAL_FIT_FROM = date(2022, 8, 1)
ANNUAL_FIT_YEARS: tuple[int, ...] = (2023, 2024, 2025, 2026)


class ArchetypeError(Exception):
    """An archetype request that cannot be answered causally."""


def _kmeans_once(data: np.ndarray, k: int, rng: np.random.Generator) -> tuple[np.ndarray, float]:
    n = len(data)
    # k-means++ seeding.
    centroids = np.empty((k, data.shape[1]))
    first = int(rng.integers(n))
    centroids[0] = data[first]
    closest = ((data - centroids[0]) ** 2).sum(axis=1)
    for j in range(1, k):
        total = closest.sum()
        if total <= 0.0:
            centroids[j] = data[int(rng.integers(n))]
        else:
            r = rng.random() * total
            index = int(np.searchsorted(np.cumsum(closest), r))
            centroids[j] = data[min(index, n - 1)]
        closest = np.minimum(closest, ((data - centroids[j]) ** 2).sum(axis=1))
    # Lloyd iterations.
    labels = np.zeros(n, dtype=int)
    for _ in range(KMEANS_MAX_ITER):
        distances = ((data[:, None, :] - centroids[None, :, :]) ** 2).sum(axis=2)
        new_labels = distances.argmin(axis=1)
        if (new_labels == labels).all() and _ > 0:
            break
        labels = new_labels
        for j in range(k):
            members = data[labels == j]
            if len(members):
                centroids[j] = members.mean(axis=0)
    inertia = float(((data - centroids[labels]) ** 2).sum())
    return labels, inertia


def kmeans(data: np.ndarray, k: int, *, seed: int = KMEANS_SEED) -> tuple[np.ndarray, np.ndarray]:
    """Best-of-restarts KMeans; returns (labels, centroids), deterministic.

    Labels are canonicalized: clusters renumbered by first occurrence in
    symbol order, so identical partitions get identical label vectors.
    """
    best_labels, best_inertia = None, float("inf")
    for restart in range(KMEANS_RESTARTS):
        rng = np.random.default_rng(seed + restart)
        labels, inertia = _kmeans_once(data, k, rng)
        if inertia < best_inertia - 1e-12:
            best_labels, best_inertia = labels, inertia
    assert best_labels is not None
    mapping: dict[int, int] = {}
    canonical = np.empty_like(best_labels)
    for i, label in enumerate(best_labels):
        if label not in mapping:
            mapping[label] = len(mapping)
        canonical[i] = mapping[label]
    centroids = np.vstack([data[canonical == j].mean(axis=0) for j in range(canonical.max() + 1)])
    return canonical, centroids


def ward(data: np.ndarray, k: int) -> np.ndarray:
    """Ward agglomerative clustering to k clusters (robustness check only)."""
    clusters: list[list[int]] = [[i] for i in range(len(data))]
    centroids: list[np.ndarray] = [data[i].astype(float) for i in range(len(data))]
    sizes: list[int] = [1] * len(data)
    while len(clusters) > k:
        best = (float("inf"), -1, -1)
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                merge_cost = (sizes[a] * sizes[b] / (sizes[a] + sizes[b])) * float(
                    ((centroids[a] - centroids[b]) ** 2).sum()
                )
                if merge_cost < best[0]:
                    best = (merge_cost, a, b)
        _, a, b = best
        merged = clusters[a] + clusters[b]
        centroid = (centroids[a] * sizes[a] + centroids[b] * sizes[b]) / (sizes[a] + sizes[b])
        size = sizes[a] + sizes[b]
        for index in sorted((a, b), reverse=True):
            del clusters[index], centroids[index], sizes[index]
        clusters.append(merged)
        centroids.append(centroid)
        sizes.append(size)
    labels = np.zeros(len(data), dtype=int)
    ordered = sorted(clusters, key=min)
    for j, members in enumerate(ordered):
        for i in members:
            labels[i] = j
    return labels


def silhouette(data: np.ndarray, labels: np.ndarray) -> float:
    """Mean silhouette coefficient (Euclidean)."""
    n = len(data)
    unique = np.unique(labels)
    if len(unique) < 2:
        return float("nan")
    distances = np.sqrt(((data[:, None, :] - data[None, :, :]) ** 2).sum(axis=2))
    scores = np.zeros(n)
    for i in range(n):
        own = labels[i]
        own_mask = labels == own
        if own_mask.sum() <= 1:
            scores[i] = 0.0
            continue
        a = distances[i][own_mask & (np.arange(n) != i)].mean()
        b = min(distances[i][labels == other].mean() for other in unique if other != own)
        scores[i] = (b - a) / max(a, b) if max(a, b) > 0 else 0.0
    return float(scores.mean())


def adjusted_rand(a: np.ndarray, b: np.ndarray) -> float:
    """Adjusted Rand index — permutation-safe partition agreement."""
    n = len(a)
    table: dict[tuple[int, int], int] = {}
    for x, y in zip(a, b, strict=True):
        table[(int(x), int(y))] = table.get((int(x), int(y)), 0) + 1
    sum_comb = sum(v * (v - 1) / 2 for v in table.values())
    a_counts: dict[int, int] = {}
    b_counts: dict[int, int] = {}
    for x in a:
        a_counts[int(x)] = a_counts.get(int(x), 0) + 1
    for y in b:
        b_counts[int(y)] = b_counts.get(int(y), 0) + 1
    sum_a = sum(v * (v - 1) / 2 for v in a_counts.values())
    sum_b = sum(v * (v - 1) / 2 for v in b_counts.values())
    total = n * (n - 1) / 2
    expected = sum_a * sum_b / total if total else 0.0
    maximum = (sum_a + sum_b) / 2
    if maximum == expected:
        return 1.0
    return float((sum_comb - expected) / (maximum - expected))


@dataclass(frozen=True)
class ArchetypeFit:
    """One frozen walk-forward fit."""

    fit_mark: date
    features: tuple[str, ...]
    symbols: tuple[str, ...]
    labels: tuple[int, ...]
    centroids: tuple[tuple[float, ...], ...]
    k: int
    silhouette_by_k: dict[int, float]
    ward_agreement: float
    singleton_note: str

    def assign(self, z_vector: np.ndarray) -> int | None:
        """Nearest frozen centroid; None when any feature is NaN."""
        if np.isnan(z_vector).any():
            return None
        centroids = np.asarray(self.centroids)
        return int(((centroids - z_vector) ** 2).sum(axis=1).argmin())


def fit_dates(marks: list[date]) -> list[date]:
    """The §L5 schedule: first mark ≥ 2022-07-01, then annual firsts."""
    dates = [next(m for m in marks if m >= INITIAL_FIT_FROM)]
    for year in ANNUAL_FIT_YEARS:
        candidate = next((m for m in marks if m.year == year), None)
        if candidate is not None:
            dates.append(candidate)
    return dates


def training_vectors(
    z_panel: pd.DataFrame,
    features: tuple[str, ...],
    train_marks: list[date],
) -> tuple[list[str], np.ndarray]:
    """Per-symbol mean z-vector over complete training marks (§L5).

    A symbol enters iff it has ≥ MIN_TRAIN_MARKS marks with every feature
    non-NaN inside the training span.
    """
    symbols: list[str] = []
    vectors: list[np.ndarray] = []
    all_symbols = sorted(z_panel.index.get_level_values("symbol").unique())
    for symbol in all_symbols:
        rows = []
        for mark in train_marks:
            try:
                row = z_panel.loc[(mark, symbol), list(features)]
            except KeyError:
                continue
            values = row.to_numpy(dtype="float64")
            if not np.isnan(values).any():
                rows.append(values)
        if len(rows) >= MIN_TRAIN_MARKS:
            symbols.append(symbol)
            vectors.append(np.mean(rows, axis=0))
    if not symbols:
        raise ArchetypeError("No symbol has enough complete training marks.")
    return symbols, np.vstack(vectors)


def fit_archetypes(
    z_panel: pd.DataFrame,
    features: tuple[str, ...],
    fit_mark: date,
    marks: list[date],
) -> ArchetypeFit:
    """One frozen fit from marks strictly before ``fit_mark`` (§L5)."""
    train_marks = [m for m in marks if m < fit_mark]
    if not train_marks:
        raise ArchetypeError(f"No training marks before {fit_mark}.")
    symbols, data = training_vectors(z_panel, features, train_marks)

    silhouette_by_k: dict[int, float] = {}
    labels_by_k: dict[int, np.ndarray] = {}
    for k in K_CANDIDATES:
        if k >= len(symbols):
            continue
        labels, _ = kmeans(data, k)
        labels_by_k[k] = labels
        silhouette_by_k[k] = silhouette(data, labels)
    if not silhouette_by_k:
        raise ArchetypeError("Too few symbols for any candidate k.")
    best_k = max(sorted(silhouette_by_k), key=lambda k: (silhouette_by_k[k], -k))

    # §L5 singleton guard: clusters below MIN_CLUSTER_MEMBERS → step k down.
    note = ""
    k = best_k
    while True:
        labels = labels_by_k.get(k)
        if labels is None:
            labels, _ = kmeans(data, k)
        counts = np.bincount(labels)
        if counts.min() >= MIN_CLUSTER_MEMBERS or k <= min(K_CANDIDATES):
            if counts.min() < MIN_CLUSTER_MEMBERS:
                note = f"k={k} retains a cluster of {int(counts.min())} member(s) at floor"
            break
        note = f"k stepped down from {k} (cluster of {int(counts.min())} member(s))"
        k -= 1

    centroids = np.vstack([data[labels == j].mean(axis=0) for j in range(labels.max() + 1)])
    ward_labels = ward(data, k)
    return ArchetypeFit(
        fit_mark=fit_mark,
        features=tuple(features),
        symbols=tuple(symbols),
        labels=tuple(int(x) for x in labels),
        centroids=tuple(tuple(float(v) for v in row) for row in centroids),
        k=int(labels.max() + 1),
        silhouette_by_k={int(k_): float(v) for k_, v in silhouette_by_k.items()},
        ward_agreement=adjusted_rand(labels, ward_labels),
        singleton_note=note,
    )


def assignments_over_marks(
    z_panel: pd.DataFrame,
    fits: list[ArchetypeFit],
    marks: list[date],
) -> pd.DataFrame:
    """Per (mark, symbol) archetype id from the governing frozen fit.

    The governing fit for mark m is the latest fit with fit_mark ≤ m; marks
    before the initial fit get no archetype (NaN), the §L5 no-lookahead rule.
    """
    ordered = sorted(fits, key=lambda fit: fit.fit_mark)
    rows: list[dict[str, object]] = []
    symbols = sorted(z_panel.index.get_level_values("symbol").unique())
    for mark in marks:
        governing = None
        for fit in ordered:
            if fit.fit_mark <= mark:
                governing = fit
        for symbol in symbols:
            archetype: float = float("nan")
            if governing is not None:
                try:
                    row = z_panel.loc[(mark, symbol), list(governing.features)]
                except KeyError:
                    row = None
                if row is not None:
                    assigned = governing.assign(row.to_numpy(dtype="float64"))
                    if assigned is not None:
                        archetype = float(assigned)
            rows.append(
                {
                    "mark": mark,
                    "symbol": symbol,
                    "archetype": archetype,
                    "fit_mark": governing.fit_mark if governing else None,
                }
            )
    return pd.DataFrame(rows).set_index(["mark", "symbol"]).sort_index()


__all__ = [
    "ANNUAL_FIT_YEARS",
    "INITIAL_FIT_FROM",
    "K_CANDIDATES",
    "KMEANS_RESTARTS",
    "KMEANS_SEED",
    "MIN_CLUSTER_MEMBERS",
    "MIN_TRAIN_MARKS",
    "ArchetypeError",
    "ArchetypeFit",
    "adjusted_rand",
    "assignments_over_marks",
    "fit_archetypes",
    "fit_dates",
    "kmeans",
    "silhouette",
    "training_vectors",
    "ward",
]
