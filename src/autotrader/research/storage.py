"""Where research artifacts live: on external storage, never in the repository.

A parameter sweep writes one record per experiment, and a walk-forward study
writes one per window per parameter set. That is bounded but not small, and the
internal SSD is not where it belongs. This module is the single place that
answers "where do I write?", and it answers by reading the two environment
variables the external QA workspace exports:

``AUTOTRADER_QA_REPORTS``   experiment records, manifests, selection records
``AUTOTRADER_QA_DATASETS``  bar datasets a study reads

**It refuses rather than guesses.** An unset variable is an error, not a
fallback to ``./reports``: a silent fallback is how a research run fills the
internal disk, and by the time anyone notices, the artifacts are in a directory
git is watching. For the same reason a variable that resolves *inside* the
repository working tree is refused too - pointing the reports root at the
checkout would defeat the whole arrangement, and a typo is the likely cause.

**Nothing here is trading state.** No database, no credentials, no account
data: research artifacts are derived from stored bars and code, and a study
directory can be deleted without the trading system noticing.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

#: The environment variables the external QA workspace exports.
REPORTS_ENV = "AUTOTRADER_QA_REPORTS"
DATASETS_ENV = "AUTOTRADER_QA_DATASETS"

#: Every research artifact lives under this subdirectory of the reports root,
#: so a study can never collide with another tool's output.
RESEARCH_SUBDIRECTORY = "research"

#: Filenames inside one run directory. Fixed names, because a reader that has
#: to guess which file holds the metrics is a reader that will guess wrong.
MANIFEST_FILENAME = "manifest.json"
EXPERIMENTS_FILENAME = "experiments.jsonl"
SPLITS_FILENAME = "splits.json"
SELECTION_FILENAME = "selection.json"

_SLUG_ALLOWED = re.compile(r"[^a-z0-9]+")
_RUN_ID_PATTERN = "%Y%m%dT%H%M%SZ"


class ResearchStorageError(Exception):
    """A research artifact could not be located or written.

    Raised for an unset or unusable storage root. The CLI reports these without
    a traceback, because "set AUTOTRADER_QA_REPORTS" is an instruction to an
    operator rather than a defect to debug.
    """


def slugify(value: str) -> str:
    """Reduce `value` to a lowercase filesystem-safe slug.

    Used for study names and symbols. ``BTC/USD`` becomes ``btc-usd``, so a
    pair symbol is a directory name without the slash ever being written to
    disk and without the domain symbol being rewritten anywhere else.
    """
    slug = _SLUG_ALLOWED.sub("-", value.strip().lower()).strip("-")
    if not slug:
        raise ResearchStorageError(f"Cannot derive a directory name from {value!r}.")
    return slug


def repository_root() -> Path:
    """The checkout this package was imported from.

    Derived from this module's own location rather than from the working
    directory, so the containment check below cannot be defeated by running the
    CLI from somewhere else.
    """
    return Path(__file__).resolve().parents[3]


def _is_inside(candidate: Path, container: Path) -> bool:
    """True when `candidate` is `container` or lies beneath it."""
    return candidate == container or container in candidate.parents


def _resolve_root(variable: str, environ: Mapping[str, str] | None = None) -> Path:
    """Resolve one storage root from the environment, or refuse.

    The variable must be set, non-empty, absolute, and outside the repository.
    Each of those is a separate message: an operator who mistyped a path should
    not have to guess which of four rules they broke.
    """
    source = os.environ if environ is None else environ
    raw = source.get(variable)
    if raw is None or not raw.strip():
        raise ResearchStorageError(
            f"{variable} is not set. Research artifacts are written to external storage; "
            "source the QA workspace environment before running a study."
        )

    root = Path(raw.strip()).expanduser()
    if not root.is_absolute():
        raise ResearchStorageError(f"{variable} must be an absolute path, got {raw.strip()!r}.")

    resolved = root.resolve()
    if _is_inside(resolved, repository_root()):
        raise ResearchStorageError(
            f"{variable} resolves to {resolved}, which is inside the repository at "
            f"{repository_root()}. Research artifacts must not be written into the checkout."
        )
    return resolved


def resolve_reports_root(environ: Mapping[str, str] | None = None) -> Path:
    """The directory research reports are written under. Never created here."""
    return _resolve_root(REPORTS_ENV, environ)


def resolve_datasets_root(environ: Mapping[str, str] | None = None) -> Path:
    """The directory stored bar datasets are read from."""
    return _resolve_root(DATASETS_ENV, environ)


def research_root(environ: Mapping[str, str] | None = None) -> Path:
    """The research subtree of the reports root."""
    return resolve_reports_root(environ) / RESEARCH_SUBDIRECTORY


def format_run_id(created_at: datetime) -> str:
    """A sortable UTC run identifier: ``20260828T190400Z``.

    Derived from a supplied instant rather than read from the clock, so a study
    that is re-run with the same inputs and the same stamp lands in the same
    directory and a test can assert on the path.
    """
    from datetime import UTC

    return created_at.astimezone(UTC).strftime(_RUN_ID_PATTERN)


def run_directory(
    study: str,
    run_id: str,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """The directory one study run owns: ``<reports>/research/<study>/<run>``.

    The path is computed, not created. Creating it is `ensure_run_directory`,
    kept separate so a caller can show an operator where output *would* go
    without leaving an empty directory behind when they change their mind.
    """
    return research_root(environ) / slugify(study) / run_id


def ensure_run_directory(
    study: str,
    run_id: str,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Create and return one study run's directory."""
    directory = run_directory(study, run_id, environ)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def atomic_write_text(path: Path, payload: str) -> None:
    """Write `payload` to `path` atomically.

    A study that is interrupted must leave either the previous file or the
    complete new one, never a half-written manifest that a later reader parses
    as truth. Same discipline as the market-data sidecar writer; a temporary
    file in the destination directory is renamed over the target, which is
    atomic within one filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, document: object) -> None:
    """Persist `document` as sorted, indented JSON."""
    atomic_write_text(path, json.dumps(document, indent=2, sort_keys=True, default=str) + "\n")


def append_jsonl(path: Path, record: Mapping[str, object]) -> None:
    """Append one JSON object as a line.

    Experiment records are appended rather than accumulated in memory and
    written once, so a sweep that is killed halfway still leaves every
    experiment it actually completed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, default=str)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_jsonl(path: Path) -> tuple[dict[str, object], ...]:
    """Read every record from a JSONL file, skipping blank lines."""
    if not path.exists():
        return ()
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return tuple(records)


__all__ = [
    "DATASETS_ENV",
    "EXPERIMENTS_FILENAME",
    "MANIFEST_FILENAME",
    "REPORTS_ENV",
    "RESEARCH_SUBDIRECTORY",
    "SELECTION_FILENAME",
    "SPLITS_FILENAME",
    "ResearchStorageError",
    "append_jsonl",
    "atomic_write_text",
    "ensure_run_directory",
    "format_run_id",
    "read_jsonl",
    "repository_root",
    "research_root",
    "resolve_datasets_root",
    "resolve_reports_root",
    "run_directory",
    "slugify",
    "write_json",
]
