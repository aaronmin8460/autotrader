"""Durable per-cell checkpoints, so a restart resumes instead of repeating.

The unit of work is one symbol x window x horizon cell. Each finishes as one
JSON file whose name is fully determined by the cell's identity, written
atomically (temp file, then rename) and stamped ``"complete": true`` as the
last field the writer sets. A file that exists but does not carry the stamp is
a crashed write and is redone; a file that carries it is finished work and is
skipped. There is no append anywhere, so a resume cannot produce duplicates -
re-running a finished cell would overwrite the identical file, and the runner
refuses to even do that.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

COMPLETE_KEY = "complete"


class CheckpointError(Exception):
    """A checkpoint that cannot be trusted or written."""


def cell_path(root: Path, *, symbol: str, window: str, horizon_bars: int) -> Path:
    """The one filename a cell's result may live under."""
    return Path(root) / "cells" / f"{symbol}_{window}_h{horizon_bars:02d}.json"


def is_complete(path: Path) -> bool:
    """Whether finished work already exists at `path`.

    Unreadable or unstamped JSON is treated as an interrupted write - the cell
    is redone - rather than as an error, because that is exactly the state a
    power cut leaves behind.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get(COMPLETE_KEY) is True


def write_cell(path: Path, payload: dict[str, object]) -> Path:
    """Atomically persist one finished cell.

    The stamp is added here, so a payload cannot claim completeness without
    having gone through the atomic write path.
    """
    if is_complete(path):
        raise CheckpointError(
            f"{path} already holds a completed cell. A finished cell is never "
            "overwritten; delete it deliberately if it must be recomputed."
        )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    stamped = dict(payload)
    stamped[COMPLETE_KEY] = True
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(stamped, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target


def read_cell(path: Path) -> dict[str, object]:
    """Load one completed cell, refusing an incomplete one."""
    if not is_complete(path):
        raise CheckpointError(f"{path} does not hold a completed cell.")
    return json.loads(Path(path).read_text(encoding="utf-8"))


__all__ = [
    "COMPLETE_KEY",
    "CheckpointError",
    "cell_path",
    "is_complete",
    "read_cell",
    "write_cell",
]
