"""M1: model artifact metadata, and a filesystem registry to keep it in.

A trained model is a file, and a file on its own is unusable six weeks later:
nobody remembers which dataset it saw, which target it was fitted against, or
which column contract its inputs satisfied. This module stores the file
together with the answers.

**Artifacts are immutable.** Registering `(model_name, model_version)` twice is
refused, not overwritten. The stored `artifact.json` is never rewritten, and
`artifact_version` is the SHA-256 of the model file itself, so the identity of
an artifact is a property of its bytes rather than a name somebody assigned.
An artifact whose recorded hash no longer matches its file is a corrupted
artifact and `verify` says so.

**Stage is the one mutable fact, and it is kept in its own file.** Promotion
history goes to `stage.json` beside the immutable record, appended rather than
replaced, so "when did this become a candidate, and why?" has an answer.

**There is no PRODUCTION stage and no activate() method.** The three stages are
EXPERIMENTAL, CANDIDATE and ARCHIVED. Nothing in this package may turn a model
into a trading decision: strategy activation is a separate, deliberate change
to a runtime that this registry has no vocabulary for, and adding a stage
called PRODUCTION would imply an authority it does not have. A test asserts
both absences by name.

**The registry holds no secret and no account data.** It stores model files and
provenance. Everything written goes through the storage boundary's credential
scan, and the layout lives under `AUTOTRADER_QA_MODELS` rather than in the
repository, because model files are heavy and the internal SSD is not where
they belong.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from autotrader import __version__
from autotrader.data.historical import atomic_write
from autotrader.ml import MLError
from autotrader.ml.grid import now_utc
from autotrader.ml.storage import (
    ensure_directory,
    model_root,
    read_json,
    sha256_of_file,
    write_json,
)

#: Where the registry lives under the models root.
REGISTRY_DIRECTORY = "registry"

#: The immutable provenance record, and the mutable stage record beside it.
ARTIFACT_FILENAME = "artifact.json"
STAGE_FILENAME = "stage.json"

#: The stored model file's name inside an artifact directory. The original
#: extension is preserved so a reader can tell a pickle from a JSON from a
#: booster dump without opening it.
ARTIFACT_STEM = "model"


class RegistryError(MLError):
    """An artifact could not be registered, found, or verified."""


class ArtifactStage(Enum):
    """How far along an artifact is. There is deliberately no production stage.

    `EXPERIMENTAL` is where everything starts. `CANDIDATE` says walk-forward
    evidence exists and someone thinks this one is worth arguing about.
    `ARCHIVED` says it is kept for the record and should not be picked up by
    accident. Nothing here makes a model trade; that is a change to a runtime,
    made on purpose, somewhere else entirely.
    """

    EXPERIMENTAL = "experimental"
    CANDIDATE = "candidate"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class ArtifactMetadata:
    """Everything true about one trained model, recorded once and never edited.

    `model_version` is what an operator called this model. `artifact_version`
    is the SHA-256 of its bytes and is what actually identifies it: two
    artifacts with the same name and different hashes are different models, and
    the hash is the half that cannot be reused by mistake.
    """

    model_name: str
    model_version: str
    artifact_version: str
    artifact_filename: str
    created_at_utc: datetime
    asset_class: str
    symbols: tuple[str, ...]
    timeframe: str
    feature_schema_version: str
    feature_schema_fingerprint: str
    label_spec: dict[str, object]
    label_spec_id: str
    dataset_fingerprint: str
    experiment_id: str
    split: dict[str, object]
    hyperparameters: dict[str, object]
    calibration: dict[str, object]
    metrics: dict[str, float]
    autotrader_version: str = __version__
    notes: str = ""

    def __post_init__(self) -> None:
        for field in ("model_name", "model_version", "artifact_version", "artifact_filename"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise RegistryError(f"{field} must be a non-empty string, got {value!r}.")
        if "/" in self.model_name or "/" in self.model_version:
            raise RegistryError(
                "model_name and model_version become directory names and may not "
                "contain a path separator."
            )
        if not self.symbols:
            raise RegistryError("An artifact must record which symbols it was trained on.")
        if self.created_at_utc.tzinfo is None:
            raise RegistryError("created_at_utc must be timezone-aware.")

    def to_record(self) -> dict[str, object]:
        """The serializable form written to `artifact.json`."""
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "artifact_version": self.artifact_version,
            "artifact_filename": self.artifact_filename,
            "created_at_utc": self.created_at_utc.isoformat(),
            "asset_class": self.asset_class,
            "symbols": list(self.symbols),
            "timeframe": self.timeframe,
            "feature_schema_version": self.feature_schema_version,
            "feature_schema_fingerprint": self.feature_schema_fingerprint,
            "label_spec": dict(self.label_spec),
            "label_spec_id": self.label_spec_id,
            "dataset_fingerprint": self.dataset_fingerprint,
            "experiment_id": self.experiment_id,
            "split": dict(self.split),
            "hyperparameters": dict(self.hyperparameters),
            "calibration": dict(self.calibration),
            "metrics": {name: float(value) for name, value in self.metrics.items()},
            "autotrader_version": self.autotrader_version,
            "notes": self.notes,
        }

    @classmethod
    def from_record(cls, record: dict[str, object]) -> ArtifactMetadata:
        """Rebuild metadata from a stored record, refusing an incomplete one."""
        try:
            return cls(
                model_name=str(record["model_name"]),
                model_version=str(record["model_version"]),
                artifact_version=str(record["artifact_version"]),
                artifact_filename=str(record["artifact_filename"]),
                created_at_utc=datetime.fromisoformat(str(record["created_at_utc"])),
                asset_class=str(record["asset_class"]),
                symbols=tuple(str(symbol) for symbol in record["symbols"]),  # type: ignore[union-attr]
                timeframe=str(record["timeframe"]),
                feature_schema_version=str(record["feature_schema_version"]),
                feature_schema_fingerprint=str(record["feature_schema_fingerprint"]),
                label_spec=dict(record["label_spec"]),  # type: ignore[arg-type]
                label_spec_id=str(record["label_spec_id"]),
                dataset_fingerprint=str(record["dataset_fingerprint"]),
                experiment_id=str(record["experiment_id"]),
                split=dict(record["split"]),  # type: ignore[arg-type]
                hyperparameters=dict(record["hyperparameters"]),  # type: ignore[arg-type]
                calibration=dict(record["calibration"]),  # type: ignore[arg-type]
                metrics={
                    str(name): float(value)
                    for name, value in dict(record["metrics"]).items()  # type: ignore[arg-type]
                },
                autotrader_version=str(record.get("autotrader_version", "")),
                notes=str(record.get("notes", "")),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RegistryError(f"Artifact record is not usable: {error}") from None


@dataclass(frozen=True)
class RegisteredArtifact:
    """A stored artifact: its record, its stage, and where its file is."""

    metadata: ArtifactMetadata
    stage: ArtifactStage
    directory: Path

    @property
    def artifact_path(self) -> Path:
        """The model file itself."""
        return self.directory / self.metadata.artifact_filename

    def verify(self) -> bool:
        """Whether the stored file still hashes to its recorded `artifact_version`."""
        if not self.artifact_path.is_file():
            return False
        return sha256_of_file(self.artifact_path) == self.metadata.artifact_version


class ModelRegistry:
    """A registry of immutable model artifacts on the filesystem.

    There is no index file. The registry *is* the directory tree, so a listing
    is a directory scan and there is no central record to fall out of step with
    what is actually stored - or to be corrupted by two writers at once.
    """

    def __init__(self, root: Path | None = None) -> None:
        base = Path(root) if root is not None else model_root() / REGISTRY_DIRECTORY
        self.root = ensure_directory(base)

    def _directory_for(self, model_name: str, model_version: str) -> Path:
        return self.root / model_name / model_version

    def register(
        self,
        metadata: ArtifactMetadata,
        artifact_path: Path,
        *,
        stage: ArtifactStage = ArtifactStage.EXPERIMENTAL,
    ) -> RegisteredArtifact:
        """Store a model file and its record. Refuses to overwrite either.

        The file's hash is recomputed here rather than trusted: `metadata`
        arrives from a caller, and an artifact whose recorded identity does not
        match its bytes would be undetectably wrong for as long as nobody
        checked.
        """
        source = Path(artifact_path)
        if not source.is_file():
            raise RegistryError(f"No such artifact file: {source}")
        digest = sha256_of_file(source)
        if digest != metadata.artifact_version:
            raise RegistryError(
                f"{source} hashes to {digest}, but the metadata records "
                f"{metadata.artifact_version}. An artifact is identified by its bytes."
            )
        directory = self._directory_for(metadata.model_name, metadata.model_version)
        if directory.exists():
            raise RegistryError(
                f"{metadata.model_name} {metadata.model_version} is already registered at "
                f"{directory}. Artifacts are immutable; register a new version instead."
            )
        ensure_directory(directory)
        stored = directory / metadata.artifact_filename
        atomic_write(stored, lambda temporary: shutil.copyfile(source, temporary))
        write_json(directory / ARTIFACT_FILENAME, metadata.to_record())
        self._write_stage(directory, stage, reason="Registered.", at=now_utc())
        return RegisteredArtifact(metadata=metadata, stage=stage, directory=directory)

    def get(self, model_name: str, model_version: str) -> RegisteredArtifact:
        """One registered artifact, by name and version."""
        directory = self._directory_for(model_name, model_version)
        record_path = directory / ARTIFACT_FILENAME
        if not record_path.is_file():
            raise RegistryError(f"{model_name} {model_version} is not registered.")
        metadata = ArtifactMetadata.from_record(read_json(record_path))
        return RegisteredArtifact(
            metadata=metadata, stage=self._read_stage(directory), directory=directory
        )

    def list_models(self) -> tuple[str, ...]:
        """Every model name with at least one registered version, sorted."""
        return tuple(sorted(entry.name for entry in self.root.iterdir() if entry.is_dir()))

    def list_versions(self, model_name: str) -> tuple[RegisteredArtifact, ...]:
        """Every registered version of one model, oldest first.

        Ordered by `created_at_utc` rather than by version string, because a
        version is an operator's label and string ordering puts `1.10.0` before
        `1.9.0`. Time is the one ordering that cannot be spelled wrong.
        """
        directory = self.root / model_name
        if not directory.is_dir():
            return ()
        artifacts = [
            self.get(model_name, entry.name)
            for entry in sorted(directory.iterdir())
            if entry.is_dir() and (entry / ARTIFACT_FILENAME).is_file()
        ]
        return tuple(sorted(artifacts, key=lambda item: item.metadata.created_at_utc))

    def latest(self, model_name: str) -> RegisteredArtifact:
        """The most recently created version of one model.

        Most recent, and nothing more. It is not "the best", it is not "the one
        in use", and it does not become either by being returned from here.
        """
        versions = self.list_versions(model_name)
        if not versions:
            raise RegistryError(f"No versions of {model_name!r} are registered.")
        return versions[-1]

    def set_stage(
        self,
        model_name: str,
        model_version: str,
        stage: ArtifactStage,
        *,
        reason: str,
        at: datetime | None = None,
    ) -> RegisteredArtifact:
        """Move an artifact between stages, recording who said so and why.

        The reason is required. A stage change with no stated reason is the
        kind of record that looks informative and answers nothing.
        """
        if not isinstance(stage, ArtifactStage):
            raise RegistryError(f"stage must be an ArtifactStage, got {stage!r}.")
        if not isinstance(reason, str) or not reason.strip():
            raise RegistryError("A stage change must record a reason.")
        artifact = self.get(model_name, model_version)
        self._write_stage(
            artifact.directory, stage, reason=reason, at=at if at is not None else now_utc()
        )
        return RegisteredArtifact(
            metadata=artifact.metadata, stage=stage, directory=artifact.directory
        )

    def stage_history(self, model_name: str, model_version: str) -> tuple[dict[str, object], ...]:
        """Every stage this artifact has been in, oldest first."""
        directory = self._directory_for(model_name, model_version)
        return tuple(self._read_stage_record(directory).get("history", []))

    def _read_stage_record(self, directory: Path) -> dict[str, object]:
        path = directory / STAGE_FILENAME
        if not path.is_file():
            return {"stage": ArtifactStage.EXPERIMENTAL.value, "history": []}
        record = read_json(path)
        if not isinstance(record, dict):
            raise RegistryError(f"{path} is not a stage record.")
        return record

    def _read_stage(self, directory: Path) -> ArtifactStage:
        raw = str(self._read_stage_record(directory).get("stage", ""))
        try:
            return ArtifactStage(raw)
        except ValueError as error:
            raise RegistryError(f"{directory} records an unknown stage: {raw!r}.") from error

    def _write_stage(
        self, directory: Path, stage: ArtifactStage, *, reason: str, at: datetime
    ) -> None:
        record = self._read_stage_record(directory)
        history = list(record.get("history", []))
        history.append({"stage": stage.value, "reason": reason, "at_utc": at.isoformat()})
        write_json(directory / STAGE_FILENAME, {"stage": stage.value, "history": history})


def artifact_version_of(path: Path) -> str:
    """The identity a model file will be registered under: the SHA-256 of its bytes."""
    return sha256_of_file(path)


def artifact_filename(model_name: str, source: Path) -> str:
    """The stored filename for a model file, preserving its extension."""
    suffix = "".join(Path(source).suffixes[-1:])
    return f"{ARTIFACT_STEM}{suffix}"


def summarize(artifacts: Sequence[RegisteredArtifact]) -> list[dict[str, object]]:
    """A compact listing, for the CLI and for a report."""
    return [
        {
            "model_name": artifact.metadata.model_name,
            "model_version": artifact.metadata.model_version,
            "artifact_version": artifact.metadata.artifact_version[:12],
            "stage": artifact.stage.value,
            "created_at_utc": artifact.metadata.created_at_utc.isoformat(),
            "label_spec_id": artifact.metadata.label_spec_id,
            "feature_schema_version": artifact.metadata.feature_schema_version,
            "verified": artifact.verify(),
        }
        for artifact in artifacts
    ]


__all__ = [
    "ARTIFACT_FILENAME",
    "ARTIFACT_STEM",
    "REGISTRY_DIRECTORY",
    "STAGE_FILENAME",
    "ArtifactMetadata",
    "ArtifactStage",
    "ModelRegistry",
    "RegisteredArtifact",
    "RegistryError",
    "artifact_filename",
    "artifact_version_of",
    "summarize",
]
