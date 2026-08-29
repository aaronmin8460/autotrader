"""M1: the external-storage boundary, and the refusal to write a secret.

Datasets and model artifacts are heavy and they do not belong on the internal
SSD. Three environment variables name where they go - `AUTOTRADER_QA_DATASETS`,
`AUTOTRADER_QA_MODELS`, `AUTOTRADER_QA_REPORTS` - and this module is the only
place they are read.

**There is deliberately no fallback root.** An unset or unmounted variable
raises. The tempting alternative - quietly writing into the repository, or
into a home directory - is exactly how an external-storage policy stops being
true: the first run after a volume fails to mount would fill the disk the
policy exists to protect, and nothing would say so. A missing external volume
is a loud failure with the remedy in the message.

**The root is never created.** Subdirectories under an existing root are
created freely; the root itself is not. `mkdir -p` on an unmounted mount point
succeeds and produces an ordinary empty directory on the underlying disk, which
looks identical to a working volume until it is full. So the root must already
exist and must already be a directory, or this module refuses.

**Nothing written here may carry a credential.** `write_json` walks the payload
and refuses any key that reads like a secret before a byte reaches the disk.
Dataset and artifact metadata are provenance records that get copied around,
attached to reports, and kept for as long as the model is; a broker key that
reached one would be a key in a file nobody thinks of as sensitive. The check
is a blunt keyword scan on purpose - it costs nothing, and it is a floor rather
than a security boundary.

Writes go through C1's `atomic_write`, so a crash mid-write cannot leave a
half-written metadata sidecar next to a complete dataset.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from autotrader.data.historical import atomic_write
from autotrader.ml import MLError

#: The environment variable naming each storage root. Read nowhere else.
DATASETS_ENV = "AUTOTRADER_QA_DATASETS"
MODELS_ENV = "AUTOTRADER_QA_MODELS"
REPORTS_ENV = "AUTOTRADER_QA_REPORTS"

STORAGE_ENVIRONMENT_VARIABLES: tuple[str, ...] = (DATASETS_ENV, MODELS_ENV, REPORTS_ENV)

#: How an operator makes the variables above exist. Named in every error
#: message, because "set the variable" is not actionable and this is.
SETUP_HINT = "source /Volumes/AUTOTRADER_QA/session-env.sh"

#: Substrings that make a metadata key look like a credential.
#:
#: Matched case-insensitively against every key at every depth. Deliberately
#: over-broad: a provenance record has no legitimate need for a key containing
#: any of these, so a false positive costs a rename and a false negative costs
#: a leaked key.
SECRET_KEY_MARKERS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "passphrase",
    "token",
    "credential",
    "private_key",
    "access_key",
    "authorization",
)

#: Read in fixed-size blocks so hashing a multi-gigabyte dataset does not load
#: it into memory.
_HASH_BLOCK_SIZE = 1024 * 1024


class StorageError(MLError):
    """A storage root is unusable, or a payload may not be written."""


def _root(variable: str) -> Path:
    """The directory `variable` names, requiring that it already exists.

    Existence is the mount check. This workspace lives on an external volume
    that is not always mounted, and every failure mode of writing to an
    unmounted mount point is silent, so the one cheap question - is the
    directory there? - is asked before anything is written.
    """
    raw = os.environ.get(variable, "").strip()
    if not raw:
        raise StorageError(
            f"{variable} is not set, so there is nowhere external to write. "
            f"Heavy ML artifacts must not land on the internal SSD. Run: {SETUP_HINT}"
        )
    path = Path(raw).expanduser()
    if not path.exists():
        raise StorageError(
            f"{variable} points at {path}, which does not exist. The external "
            f"workspace is probably not mounted; this is not created for you, "
            f"because creating it would write to the internal SSD instead. Run: {SETUP_HINT}"
        )
    if not path.is_dir():
        raise StorageError(f"{variable} points at {path}, which is not a directory.")
    return path.resolve()


def dataset_root() -> Path:
    """Where built feature datasets are written."""
    return _root(DATASETS_ENV)


def model_root() -> Path:
    """Where model artifacts and the registry live."""
    return _root(MODELS_ENV)


def report_root() -> Path:
    """Where experiment records and evaluation reports are written."""
    return _root(REPORTS_ENV)


def ensure_directory(path: Path) -> Path:
    """Create `path` under an already-verified root, and return it.

    Only ever called with a path *below* one of the roots above, which is why
    creating it is safe: the root's existence has already answered the "is the
    volume mounted?" question that `mkdir -p` cannot.
    """
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def find_secret_keys(payload: Any, *, prefix: str = "") -> tuple[str, ...]:
    """Every key path in `payload` whose name reads like a credential.

    Returns paths rather than a boolean so the error can say which key is the
    problem. Walks mappings and sequences alike, because a list of dicts is a
    perfectly ordinary shape for a metadata record.
    """
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            text = str(key)
            path = f"{prefix}.{text}" if prefix else text
            lowered = text.lower()
            if any(marker in lowered for marker in SECRET_KEY_MARKERS):
                found.append(path)
            found.extend(find_secret_keys(value, prefix=path))
    elif isinstance(payload, list | tuple):
        for index, value in enumerate(payload):
            found.extend(find_secret_keys(value, prefix=f"{prefix}[{index}]"))
    return tuple(found)


def assert_no_secrets(payload: Any) -> None:
    """Refuse a payload carrying a credential-shaped key."""
    offending = find_secret_keys(payload)
    if offending:
        raise StorageError(
            "Refusing to write metadata containing credential-shaped key(s): "
            f"{', '.join(offending)}. Dataset and model metadata are provenance "
            "records and must never carry a secret."
        )


def canonical_json(payload: Any) -> str:
    """The one JSON rendering used for both storage and fingerprinting.

    Sorted keys and a fixed separator, so the same record always produces the
    same bytes. That is what makes a fingerprint over a metadata record mean
    "the same configuration" rather than "the same dictionary ordering".
    """
    return json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"


def write_json(path: Path, payload: Any) -> Path:
    """Write `payload` as canonical JSON, atomically, after the secret check."""
    assert_no_secrets(payload)
    text = canonical_json(payload)
    target = Path(path)
    atomic_write(target, lambda temporary: temporary.write_text(text, encoding="utf-8"))
    return target


def read_json(path: Path) -> Any:
    """Read a JSON record written by `write_json`."""
    target = Path(path)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise StorageError(f"No such file: {target}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise StorageError(f"{target} could not be read as JSON: {error}") from error


def sha256_of_file(path: Path) -> str:
    """The SHA-256 of a file's bytes, as lowercase hex."""
    digest = hashlib.sha256()
    target = Path(path)
    try:
        with target.open("rb") as handle:
            while block := handle.read(_HASH_BLOCK_SIZE):
                digest.update(block)
    except FileNotFoundError as error:
        raise StorageError(f"No such file: {target}") from error
    except OSError as error:
        raise StorageError(f"{target} could not be read: {error}") from error
    return digest.hexdigest()


def sha256_of_record(payload: Any) -> str:
    """The SHA-256 of a record's canonical JSON, as lowercase hex.

    The fingerprint primitive for every configuration object in this package:
    a schema, a label specification, a split specification, an experiment.
    Equal fingerprints mean the records are the same in every field that was
    serialized, which is the only definition of "the same configuration" that
    survives being written to disk and read back.
    """
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


__all__ = [
    "DATASETS_ENV",
    "MODELS_ENV",
    "REPORTS_ENV",
    "SECRET_KEY_MARKERS",
    "SETUP_HINT",
    "STORAGE_ENVIRONMENT_VARIABLES",
    "StorageError",
    "assert_no_secrets",
    "canonical_json",
    "dataset_root",
    "ensure_directory",
    "find_secret_keys",
    "model_root",
    "read_json",
    "report_root",
    "sha256_of_file",
    "sha256_of_record",
    "write_json",
]
