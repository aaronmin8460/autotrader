"""Reproducibility metadata: what a result would have to be re-derived from.

A metric is only evidence if the run that produced it can be identified later.
This module records the identifying facts alongside every experiment - code
version, library versions, the dataset's content digest and interval, and the
seed - so a number in a report can be traced back to the exact inputs that
produced it, or shown to be irreproducible.

**The dataset digest is the important one.** A commit hash says which code ran;
it says nothing about which bars ran through it. Two studies of "BTC/USD 15m
2025" are not comparable if one of them was re-downloaded after the provider
revised a bar. `dataset_digest` hashes the canonical column contents, so a
revised dataset is a different digest and the two runs stop looking alike.

**Nothing here reads the clock or the network.** `collect` takes the instant it
should stamp, so the same inputs produce the same metadata. The only ambient
thing it consults is the git checkout, which is best-effort: a study run from
an export with no `.git` records `None` rather than failing.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from autotrader import __version__ as autotrader_version
from autotrader.research.storage import repository_root

#: How long to wait on a git subprocess before giving up on it. A study must
#: not hang because a repository is in a strange state.
GIT_TIMEOUT_SECONDS = 10.0

#: Prefix recorded when the checkout has uncommitted changes. A dirty tree is
#: not a version, and a result from one must not be reported as if it were.
DIRTY_SUFFIX = "-dirty"


@dataclass(frozen=True)
class ReproducibilityMetadata:
    """The identifying facts about one research run.

    `git_commit` is `None` when the code did not come from a git checkout, and
    `git_dirty` is `True` when the checkout had uncommitted changes - which
    makes the commit an approximate answer rather than an exact one, and is
    recorded rather than hidden.
    """

    autotrader_version: str
    git_commit: str | None
    git_dirty: bool | None
    git_branch: str | None
    python_version: str
    pandas_version: str
    numpy_version: str
    platform: str
    seed: int | None
    created_at_utc: str

    @property
    def code_version(self) -> str:
        """A single human-readable code identity, dirt included."""
        if self.git_commit is None:
            return f"autotrader-{self.autotrader_version}"
        suffix = DIRTY_SUFFIX if self.git_dirty else ""
        return f"{self.git_commit[:12]}{suffix}"

    @property
    def reproducible(self) -> bool:
        """True when this run could be re-created from a committed state."""
        return self.git_commit is not None and self.git_dirty is False

    def to_json_dict(self) -> dict[str, object]:
        """The JSON form written into a manifest and every experiment record."""
        return {
            "autotrader_version": self.autotrader_version,
            "code_version": self.code_version,
            "git_commit": self.git_commit,
            "git_dirty": self.git_dirty,
            "git_branch": self.git_branch,
            "python_version": self.python_version,
            "pandas_version": self.pandas_version,
            "numpy_version": self.numpy_version,
            "platform": self.platform,
            "seed": self.seed,
            "created_at_utc": self.created_at_utc,
            "reproducible": self.reproducible,
        }


def _git(arguments: Sequence[str], *, cwd: Path) -> str | None:
    """Run one read-only git command, or return `None` if it cannot be run.

    Every failure mode - no git binary, not a repository, a timeout - is the
    same answer here: this run's code identity is unknown. It is never an
    exception, because a missing git checkout is not a reason to abandon a
    study.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
            ["git", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def git_identity(root: Path | None = None) -> tuple[str | None, bool | None, str | None]:
    """The checkout's commit, dirty flag and branch. All best-effort."""
    cwd = repository_root() if root is None else Path(root)
    if not cwd.exists():
        return None, None, None
    commit = _git(["rev-parse", "HEAD"], cwd=cwd)
    if commit is None:
        return None, None, None
    status = _git(["status", "--porcelain"], cwd=cwd)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    return commit, bool(status), branch


def collect(
    *,
    created_at: datetime,
    seed: int | None = None,
    root: Path | None = None,
) -> ReproducibilityMetadata:
    """Gather the metadata for one research run, stamped with `created_at`."""
    commit, dirty, branch = git_identity(root)
    return ReproducibilityMetadata(
        autotrader_version=autotrader_version,
        git_commit=commit,
        git_dirty=dirty,
        git_branch=branch,
        python_version=platform.python_version(),
        pandas_version=pd.__version__,
        numpy_version=np.__version__,
        platform=f"{platform.system()}-{platform.machine()}-{sys.version_info.major}."
        f"{sys.version_info.minor}",
        seed=seed,
        created_at_utc=created_at.astimezone(UTC).isoformat(),
    )


def dataset_digest(frame: pd.DataFrame) -> str:
    """A stable content hash of a canonical bar frame.

    Hashes the column names and each column's raw buffer in frame order, so two
    frames hash alike exactly when they hold the same values in the same order
    under the same names. Object columns - the symbol - are hashed through their
    UTF-8 text, because an object buffer holds pointers rather than characters
    and pointers differ between processes.

    Deliberately not `pandas.util.hash_pandas_object`: its output is not
    promised to be stable across pandas releases, and a digest that changes when
    a library is upgraded cannot answer "is this the same dataset as last
    month?".
    """
    digest = hashlib.sha256()
    digest.update(f"rows={len(frame)}".encode())
    for column in frame.columns:
        digest.update(f"|column={column}".encode())
        series = frame[column]
        if series.dtype == object or isinstance(series.dtype, pd.StringDtype):
            for value in series:
                digest.update(f"\x1f{value}".encode())
            continue
        if pd.api.types.is_datetime64_any_dtype(series):
            # Hash the instants as epoch nanoseconds. Two frames holding the
            # same moments must digest alike whether one carries a UTC offset
            # and the other does not, because they describe the same bars.
            values = pd.DatetimeIndex(series).asi8
        else:
            values = series.to_numpy()
        digest.update(str(values.dtype).encode())
        digest.update(np.ascontiguousarray(values).tobytes())
    return digest.hexdigest()


@dataclass(frozen=True)
class DatasetFingerprint:
    """What dataset a result was computed over, and over which interval."""

    symbol: str
    row_count: int
    first_timestamp: str | None
    last_timestamp: str | None
    digest: str

    def to_json_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "row_count": self.row_count,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "digest": self.digest,
        }


def fingerprint_dataset(frame: pd.DataFrame, *, symbol: str | None = None) -> DatasetFingerprint:
    """Summarize `frame` into the fingerprint stored with every result."""
    if symbol is None:
        symbols = pd.unique(frame["symbol"]) if "symbol" in frame.columns else ()
        symbol = str(symbols[0]) if len(symbols) == 1 else "MIXED"
    timestamps = frame["timestamp"] if "timestamp" in frame.columns else pd.Series(dtype="object")
    first = str(timestamps.iloc[0]) if len(timestamps) else None
    last = str(timestamps.iloc[-1]) if len(timestamps) else None
    return DatasetFingerprint(
        symbol=symbol,
        row_count=len(frame),
        first_timestamp=first,
        last_timestamp=last,
        digest=dataset_digest(frame),
    )


def parameter_digest(parameters: Mapping[str, object]) -> str:
    """A short stable hash of one parameter set, for experiment identifiers.

    Sorted by key so ``{"fast": 5, "slow": 20}`` and ``{"slow": 20, "fast": 5}``
    are the same experiment rather than two.
    """
    payload = "|".join(f"{key}={parameters[key]!r}" for key in sorted(parameters))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "DIRTY_SUFFIX",
    "GIT_TIMEOUT_SECONDS",
    "DatasetFingerprint",
    "ReproducibilityMetadata",
    "collect",
    "dataset_digest",
    "fingerprint_dataset",
    "git_identity",
    "parameter_digest",
]
