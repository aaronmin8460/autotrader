"""Durable checkpoints for the full study, so a restart resumes instead of repeating.

The horizon study's discipline, kept exactly: every finished unit of work is one
JSON file whose name is fully determined by the unit's identity, written
atomically (temp file, then ``os.replace``) and stamped ``"complete": true`` as
the last field the writer sets. Unstamped or truncated JSON is a crashed write
and is redone; a stamped file is finished work and is skipped; ``write_json``
refuses to overwrite a stamped file, so a resume can neither duplicate nor
silently recompute.

Two unit shapes exist here:

- a **cell** - one ``symbol x window`` JSON carrying the V4 training record,
  the scoring integrity counts, and every replay metric for that window;
- a **series** - one engine's decision parquet for one ``symbol x window``,
  written through the same temp-then-replace rename. A parquet's completeness
  is vouched for by its cell: the cell is stamped only after every series it
  describes is on disk, so a torn parquet can only exist beside an unstamped
  cell, which is redone as a whole.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

COMPLETE_KEY = "complete"


class CheckpointError(Exception):
    """A checkpoint that cannot be trusted or written."""


def cell_path(root: Path, *, kind: str, symbol: str, unit: str) -> Path:
    """The one filename a unit's result may live under."""
    return Path(root) / kind / f"{symbol}_{unit}.json"


def series_path(root: Path, *, symbol: str, window: str, engine: str) -> Path:
    """Where one engine's decision series for one window is stored."""
    return Path(root) / "decisions" / f"{symbol}_{window}_{engine}.parquet"


def is_complete(path: Path) -> bool:
    """Whether finished work already exists at `path`."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get(COMPLETE_KEY) is True


def write_json(path: Path, payload: dict[str, object]) -> Path:
    """Atomically persist one finished unit, refusing to overwrite finished work."""
    if is_complete(path):
        raise CheckpointError(
            f"{path} already holds a completed unit. A finished unit is never "
            "overwritten; delete it deliberately if it must be recomputed."
        )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    stamped = dict(payload)
    stamped[COMPLETE_KEY] = True
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(stamped, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    os.replace(temporary, target)
    return target


def read_json(path: Path) -> dict[str, object]:
    """Load one completed unit, refusing an incomplete one."""
    if not is_complete(path):
        raise CheckpointError(f"{path} does not hold a completed unit.")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_series(path: Path, frame: pd.DataFrame) -> Path:
    """Atomically persist one decision series parquet."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp.parquet")
    frame.to_parquet(temporary, engine="pyarrow", index=False)
    os.replace(temporary, target)
    return target


__all__ = [
    "COMPLETE_KEY",
    "CheckpointError",
    "cell_path",
    "is_complete",
    "read_json",
    "series_path",
    "write_json",
    "write_series",
]
