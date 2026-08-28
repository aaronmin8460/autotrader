"""M1: the reproducibility record for one training run.

An experiment record answers a single question: if I ran this again, would I
get the same model? Everything that decides the answer goes in - the datasets
and their content fingerprints, the column contract, the target definition, the
split, the model and its hyperparameters, the seed, the library versions, and
optionally the commit the code was at.

**The experiment id is derived, not assigned.** It is a SHA-256 over exactly
the fields that determine the outcome, so two runs of the same configuration
produce the same id and a changed hyperparameter produces a different one. The
timestamp and the free-text notes are deliberately excluded from the hash:
running the same experiment on Tuesday does not make it a different experiment,
and neither does describing it better.

**A seed is required.** Not defaulted, not optional. A training run whose
randomness is unrecorded cannot be reproduced, and the moment to notice that is
before the run rather than after someone asks for the model back. Models that
use no randomness still record one - `ClassFrequencyModel` does - because "this
run used seed 0 and ignored it" is a fact, and "there was no seed" is a gap.

**Git state arrives from the caller.** This module runs no process and imports
nothing that does; the CLI reads the repository with the smoke harness's
`git_state` and passes the result in. That keeps the record honest about code
provenance without giving a library that builds datasets the ability to execute
anything.

**Records go to `AUTOTRADER_QA_REPORTS`.** They are small, but they belong with
the evidence rather than in the repository, and they pass the storage
boundary's credential scan on the way out like everything else.
"""

from __future__ import annotations

import platform
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from autotrader import __version__
from autotrader.ml import MLError
from autotrader.ml.grid import now_utc
from autotrader.ml.labels import LabelSpec
from autotrader.ml.schema import FeatureSchema
from autotrader.ml.splits import SplitSpec
from autotrader.ml.storage import (
    ensure_directory,
    report_root,
    sha256_of_record,
    write_json,
)

#: Where experiment records are written under the reports root.
EXPERIMENTS_DIRECTORY = "experiments"

#: The fields whose values determine the experiment id. Ordered, and complete:
#: anything not listed here is metadata about the run rather than a
#: determinant of its outcome.
IDENTITY_FIELDS: tuple[str, ...] = (
    "dataset_fingerprints",
    "feature_schema_version",
    "feature_schema_fingerprint",
    "label_spec",
    "split_spec",
    "model_name",
    "model_version",
    "hyperparameters",
    "seed",
)


class ExperimentError(MLError):
    """An experiment record that cannot be built or written."""


def library_versions() -> dict[str, str]:
    """The versions of everything whose behaviour could change a result.

    Recorded rather than pinned. A dataset fingerprint computed under a
    different pandas is not guaranteed to match one computed under this
    version, and knowing which was used is what turns a surprising mismatch
    into a diagnosable one.
    """
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "autotrader": __version__,
    }


@dataclass(frozen=True)
class GitProvenance:
    """Which commit the code was at, when the caller could find out.

    Every field is optional because a repository that cannot be read is
    reported as unknown rather than raised: code provenance is context for a
    record, not a gate on producing one. `dirty` is deliberately three-valued -
    a record that claims a clean tree it never checked is worse than one that
    admits it does not know.
    """

    branch: str | None = None
    sha: str | None = None
    dirty: bool | None = None

    def to_record(self) -> dict[str, object]:
        return {"branch": self.branch, "sha": self.sha, "dirty": self.dirty}


@dataclass(frozen=True)
class ExperimentMetadata:
    """One training run, described completely enough to repeat it."""

    name: str
    seed: int
    dataset_fingerprints: tuple[str, ...]
    feature_schema_version: str
    feature_schema_fingerprint: str
    label_spec: dict[str, object]
    split_spec: dict[str, object]
    model_name: str
    model_version: str
    hyperparameters: dict[str, object]
    calibration: dict[str, object] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    created_at_utc: datetime = field(default_factory=now_utc)
    git: GitProvenance = field(default_factory=GitProvenance)
    libraries: dict[str, str] = field(default_factory=library_versions)
    notes: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ExperimentError("An experiment needs a non-empty name.")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ExperimentError(
                f"seed must be an int, got {type(self.seed).__name__}. A training run "
                "with no recorded seed cannot be reproduced."
            )
        if not self.dataset_fingerprints:
            raise ExperimentError(
                "An experiment must name the dataset(s) it used, by content "
                "fingerprint. A record that does not is not reproducible."
            )
        if self.created_at_utc.tzinfo is None:
            raise ExperimentError("created_at_utc must be timezone-aware.")

    def identity_record(self) -> dict[str, object]:
        """Exactly the fields the experiment id is derived from."""
        values: dict[str, object] = {
            "dataset_fingerprints": list(self.dataset_fingerprints),
            "feature_schema_version": self.feature_schema_version,
            "feature_schema_fingerprint": self.feature_schema_fingerprint,
            "label_spec": dict(self.label_spec),
            "split_spec": dict(self.split_spec),
            "model_name": self.model_name,
            "model_version": self.model_version,
            "hyperparameters": dict(self.hyperparameters),
            "seed": int(self.seed),
        }
        return {name: values[name] for name in IDENTITY_FIELDS}

    @property
    def experiment_id(self) -> str:
        """SHA-256 over the determinants of the run. Same configuration, same id."""
        return sha256_of_record(self.identity_record())

    @property
    def short_id(self) -> str:
        """The first twelve characters of the id, for filenames and reports."""
        return self.experiment_id[:12]

    def to_record(self) -> dict[str, object]:
        """The full serializable record, id included."""
        return {
            "experiment_id": self.experiment_id,
            "name": self.name,
            "created_at_utc": self.created_at_utc.isoformat(),
            "identity": self.identity_record(),
            "calibration": dict(self.calibration),
            "metrics": {name: float(value) for name, value in self.metrics.items()},
            "git": self.git.to_record(),
            "libraries": dict(self.libraries),
            "notes": self.notes,
        }

    def with_metrics(self, metrics: dict[str, float]) -> ExperimentMetadata:
        """A copy carrying results. The id is unchanged, because results are not inputs."""
        return ExperimentMetadata(
            name=self.name,
            seed=self.seed,
            dataset_fingerprints=self.dataset_fingerprints,
            feature_schema_version=self.feature_schema_version,
            feature_schema_fingerprint=self.feature_schema_fingerprint,
            label_spec=self.label_spec,
            split_spec=self.split_spec,
            model_name=self.model_name,
            model_version=self.model_version,
            hyperparameters=self.hyperparameters,
            calibration=self.calibration,
            metrics=dict(metrics),
            created_at_utc=self.created_at_utc,
            git=self.git,
            libraries=self.libraries,
            notes=self.notes,
        )


def new_experiment(
    *,
    name: str,
    seed: int,
    dataset_fingerprints: Sequence[str],
    schema: FeatureSchema,
    label: LabelSpec,
    split: SplitSpec,
    model_name: str,
    model_version: str,
    hyperparameters: dict[str, object] | None = None,
    calibration: dict[str, object] | None = None,
    git: GitProvenance | None = None,
    notes: str = "",
    created_at: datetime | None = None,
) -> ExperimentMetadata:
    """Build a record from the objects a run already holds.

    Takes the specifications themselves rather than dictionaries, so a record
    cannot describe a schema or a label that was never used: the fingerprints
    are read off the real objects.
    """
    return ExperimentMetadata(
        name=name,
        seed=seed,
        dataset_fingerprints=tuple(dataset_fingerprints),
        feature_schema_version=schema.version,
        feature_schema_fingerprint=schema.fingerprint,
        label_spec=label.to_record(),
        split_spec=split.to_record(),
        model_name=model_name,
        model_version=model_version,
        hyperparameters=dict(hyperparameters or {}),
        calibration=dict(calibration or {}),
        git=git if git is not None else GitProvenance(),
        notes=notes,
        created_at_utc=created_at if created_at is not None else now_utc(),
    )


def experiment_path(metadata: ExperimentMetadata, *, root: Path | None = None) -> Path:
    """Where one experiment record is written.

    Named by the experiment id rather than by the run's name, so rerunning the
    same configuration rewrites the same file instead of accumulating near
    duplicates that differ only in when they happened.
    """
    base = Path(root) if root is not None else report_root() / EXPERIMENTS_DIRECTORY
    return ensure_directory(base) / f"{metadata.name}-{metadata.short_id}.json"


def write_experiment(metadata: ExperimentMetadata, *, root: Path | None = None) -> Path:
    """Persist an experiment record to external storage."""
    return write_json(experiment_path(metadata, root=root), metadata.to_record())


def reproduces(left: ExperimentMetadata, right: ExperimentMetadata) -> bool:
    """Whether two records describe runs that should produce the same model.

    A straight comparison of the derived ids, exposed as a named function so a
    caller asks the question in the words they mean rather than by comparing
    two hex strings and hoping they picked the right pair.
    """
    return left.experiment_id == right.experiment_id


__all__ = [
    "EXPERIMENTS_DIRECTORY",
    "IDENTITY_FIELDS",
    "ExperimentError",
    "ExperimentMetadata",
    "GitProvenance",
    "experiment_path",
    "library_versions",
    "new_experiment",
    "reproduces",
    "write_experiment",
]
