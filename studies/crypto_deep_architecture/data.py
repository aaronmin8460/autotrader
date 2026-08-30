"""Dataset access, evaluation windows, and the exact cost bar.

The datasets are the two 15-minute crypto parquet files the V1-V5 historical
study downloaded and fingerprinted. They are read here, never rewritten, and
their SHA-256 digests are asserted before anything is computed from them, so a
silently substituted file fails loudly rather than producing a different study
under the same name.

Everything downstream measures itself against the exact round-trip break-even
of the shipped `crypto-taker` cost model, derived from the cost model itself
rather than restated as a constant that could drift from it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from autotrader.ml.dataset import build_observations
from autotrader.ml.features import compute_features
from autotrader.ml.grid import BarGrid, crypto_grid
from autotrader.research.costs import cost_model_for

#: Where the fingerprinted historical datasets live. Read-only.
DATASET_DIR = Path("/Volumes/AUTOTRADER_QA/datasets/crypto-historical")

#: The exact files and the digests both prior studies recorded for them.
DATASET_FILES: dict[str, tuple[str, str]] = {
    "BTC/USD": (
        "BTC_USD_15m_2024-01-01_2026-08-28.parquet",
        "7f04a15a2c28a55c146afe188bff6adc4bd2add53299e7223e4832a96b99dc67",
    ),
    "ETH/USD": (
        "ETH_USD_15m_2024-01-01_2026-08-28.parquet",
        "43d82b851701989cad8e1c220e7d7e84b6cb30d62d0af7c16c4b3f83fc332624",
    ),
}

#: Grid range shared by both symbols: every 15-minute UTC boundary, inclusive.
GRID_START = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
GRID_END = datetime(2026, 8, 28, 23, 45, tzinfo=UTC)

#: The 2021-2023 extension era, downloaded for the extended-history attack.
#: Files are digest-verified against the metadata sidecars written at
#: download time; the 2024-26 originals are a different directory entirely.
EXTENDED_DIR = Path("/Volumes/AUTOTRADER_QA/datasets/crypto-historical-extended")
EXTENDED_GRID_START = datetime(2021, 1, 1, 0, 0, tzinfo=UTC)
EXTENDED_GRID_END = datetime(2023, 12, 31, 23, 45, tzinfo=UTC)

#: The prior studies' quarterly out-of-sample windows, reused verbatim so every
#: per-window figure here is comparable against the recorded V1-V5 benchmarks.
#: Each value is (first feature timestamp, last feature timestamp), inclusive.
WINDOWS: dict[str, tuple[str, str]] = {
    # P1-P3 are 2024 pre-windows: never part of the prior studies' scoring
    # range and never used for selection here. They exist solely as extra
    # out-of-sample attack windows for rule families that need no training.
    "P1": ("2024-04-01", "2024-06-30 23:45"),
    "P2": ("2024-07-01", "2024-09-30 23:45"),
    "P3": ("2024-10-01", "2024-12-31 23:45"),
    # X01-X09 are the extended-history attack windows (2021-Q4 .. 2023-Q4).
    "X01": ("2021-10-01", "2021-12-31 23:45"),
    "X02": ("2022-01-01", "2022-03-31 23:45"),
    "X03": ("2022-04-01", "2022-06-30 23:45"),
    "X04": ("2022-07-01", "2022-09-30 23:45"),
    "X05": ("2022-10-01", "2022-12-31 23:45"),
    "X06": ("2023-01-01", "2023-03-31 23:45"),
    "X07": ("2023-04-01", "2023-06-30 23:45"),
    "X08": ("2023-07-01", "2023-09-30 23:45"),
    "X09": ("2023-10-01", "2023-12-31 23:45"),
    "W01": ("2025-01-01", "2025-03-31 23:45"),
    "W02": ("2025-04-01", "2025-06-30 23:45"),
    "W03": ("2025-07-01", "2025-09-30 23:45"),
    "W04": ("2025-10-01", "2025-12-31 23:45"),
    "W05": ("2026-01-01", "2026-03-31 23:45"),
    "W06": ("2026-04-01", "2026-06-30 23:45"),
    "W07": ("2026-07-01", "2026-08-28 23:45"),
}

#: Development windows: selection may read these and nothing later.
DEVELOPMENT_WINDOWS: tuple[str, ...] = ("W01", "W02", "W03", "W04", "W05")

#: Consulted once per iteration for an already-selected candidate.
CONFIRMATION_WINDOW = "W06"

#: Final evaluation window for a frozen candidate. Not pristine - the prior
#: studies scored and inspected it - and every use of it says so.
FINAL_WINDOW = "W07"

#: Feature timestamps at or after this instant are out of bounds for every
#: development-phase computation, screening statistics included.
DEVELOPMENT_CUTOFF = pd.Timestamp("2026-04-01", tz="UTC")

#: Univariate feature screening reads only this early slice of development.
SCREENING_START = pd.Timestamp("2024-06-01", tz="UTC")
SCREENING_END = pd.Timestamp("2025-06-30 23:45", tz="UTC")


class StudyDataError(Exception):
    """The study's input data is not what the provenance record says it is."""


def exact_break_even(label: str = "crypto-taker") -> float:
    """The exact round-trip break-even move of a cost model, as a fraction.

    Derived from the model's own rates: a round trip multiplies equity by
    (1 + r) * (1 - s)(1 - f) / ((1 + s)(1 + f)), so the move that breaks even
    is (1 + s)(1 + f) / ((1 - s)(1 - f)) - 1. For `crypto-taker` this is
    60.18 bps, not the naive 60.00.
    """
    model = cost_model_for(label)
    fee = float(model.fee_rate)
    slip = float(model.slippage_rate)
    return (1.0 + slip) * (1.0 + fee) / ((1.0 - slip) * (1.0 - fee)) - 1.0


def _verify_digest(path: Path, expected: str) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise StudyDataError(
            f"{path.name} has SHA-256 {digest}, expected {expected}. The dataset "
            "is not the one the prior studies fingerprinted; refusing to compute."
        )


def load_bars(symbol: str) -> pd.DataFrame:
    """One symbol's raw bars, digest-verified, columns untouched."""
    if symbol not in DATASET_FILES:
        raise StudyDataError(f"No dataset recorded for {symbol!r}.")
    filename, expected = DATASET_FILES[symbol]
    path = DATASET_DIR / filename
    _verify_digest(path, expected)
    return pd.read_parquet(path)


def shared_grid() -> BarGrid:
    """The continuous 15-minute grid both symbols share."""
    return crypto_grid(GRID_START, GRID_END)


@dataclass(frozen=True)
class SymbolFrame:
    """One symbol's observations on the shared grid, plus its M1 features."""

    symbol: str
    observations: pd.DataFrame
    features: pd.DataFrame
    grid: BarGrid

    @property
    def timestamps(self) -> pd.Series:
        return self.observations["timestamp"]


def load_symbol_frame(symbol: str, grid: BarGrid | None = None) -> SymbolFrame:
    """Observations reindexed onto the shared grid, and the 13 M1 features.

    Missing provider bars stay missing: they are NaN rows on the grid, and
    every feature whose window covers one is NaN by the M1 layer's own policy.
    """
    bars = load_bars(symbol)
    the_grid = grid if grid is not None else shared_grid()
    observations = build_observations(bars, the_grid, symbol)
    features = compute_features(observations, has_session_gaps=the_grid.has_session_gaps)
    return SymbolFrame(symbol=symbol, observations=observations, features=features, grid=the_grid)


def extended_grid() -> BarGrid:
    """The continuous 15-minute grid of the 2021-2023 extension era."""
    return crypto_grid(EXTENDED_GRID_START, EXTENDED_GRID_END)


def load_extended_symbol_frame(symbol: str, grid: BarGrid | None = None) -> SymbolFrame:
    """The extension era's observations and M1 features, digest-verified.

    The parquet's SHA-256 must match the metadata sidecar written at download
    time, so a silently substituted or truncated file fails loudly.
    """
    import json

    slug = symbol.replace("/", "_")
    path = EXTENDED_DIR / f"{slug}_15m_2021-01-01_2023-12-31.parquet"
    sidecar = EXTENDED_DIR / f"{slug}_15m_2021-01-01_2023-12-31.metadata.json"
    if not path.exists() or not sidecar.exists():
        raise StudyDataError(f"Extended dataset for {symbol} is not downloaded yet.")
    recorded = json.loads(sidecar.read_text())["sha256"]
    _verify_digest(path, recorded)
    bars = pd.read_parquet(path)
    # The provider treats the request end as inclusive, so the file carries
    # one bar at the exclusive boundary (2024-01-01 00:00). It is off this
    # era's grid and is dropped rather than silently reindexed away.
    the_grid = grid if grid is not None else extended_grid()
    timestamps = pd.to_datetime(bars["timestamp"], utc=True)
    bars = bars.loc[timestamps <= pd.Timestamp(EXTENDED_GRID_END)].reset_index(drop=True)
    observations = build_observations(bars, the_grid, symbol)
    features = compute_features(observations, has_session_gaps=the_grid.has_session_gaps)
    return SymbolFrame(symbol=symbol, observations=observations, features=features, grid=the_grid)


def window_mask(timestamps: pd.Series, window: str) -> pd.Series:
    """Boolean mask of feature timestamps inside a named window, inclusive."""
    start, end = WINDOWS[window]
    lo = pd.Timestamp(start, tz="UTC")
    hi = pd.Timestamp(end, tz="UTC")
    return (timestamps >= lo) & (timestamps <= hi)


__all__ = [
    "CONFIRMATION_WINDOW",
    "DATASET_DIR",
    "DATASET_FILES",
    "DEVELOPMENT_CUTOFF",
    "DEVELOPMENT_WINDOWS",
    "FINAL_WINDOW",
    "GRID_END",
    "GRID_START",
    "SCREENING_END",
    "SCREENING_START",
    "WINDOWS",
    "StudyDataError",
    "SymbolFrame",
    "exact_break_even",
    "load_bars",
    "load_symbol_frame",
    "shared_grid",
    "window_mask",
]
