"""Which commit the harness is reporting about. The only module here that runs a process.

**Isolated on purpose.** Every other module under `autotrader.smoke` is
forbidden from starting a process at all, and a test asserts that by name -
`subprocess`, `os.system`, `os.popen`, `os.exec*` and `pty` appear in this file
and nowhere else in the package. Confining it here is what makes the important
guarantee checkable: the module that renders an order command line for a human
(`cleanup`) cannot start a process, and this module, which can, holds no
command to run and has no parameter that could carry one - a test asserts that
no autotrader command name appears in this file at all.

**Only `git`, and never a shell.** Every invocation is a literal argument list
beginning with the constant `git`; `shell=True` appears nowhere, no string is
interpolated into a command, and the only caller-supplied value is a directory
path passed as a separate `-C` argument. A test asserts each of those against
the parsed source.

A repository that cannot be read is reported as unknown rather than raised.
Git state is context for a report, not a gate on broker safety.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Every command this module may run, as literal argument lists. `-C <repo>` is
#: appended by `_git`; nothing else is ever added, and nothing is interpolated.
_REV_PARSE_HEAD = ("rev-parse", "HEAD")
_CURRENT_BRANCH = ("rev-parse", "--abbrev-ref", "HEAD")
_PORCELAIN_STATUS = ("status", "--porcelain")

_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class GitState:
    """Branch, commit, and whether the working tree has uncommitted changes.

    `dirty` is `None` when git could not be asked. It is deliberately not
    `False`: "no changes" and "I could not check" are different answers, and a
    report that shows a clean tree it never verified is worse than one that
    admits it does not know.
    """

    branch: str | None
    sha: str | None
    dirty: bool | None
    detail: str

    @property
    def known(self) -> bool:
        return self.sha is not None

    @property
    def short_sha(self) -> str:
        return "unknown" if self.sha is None else self.sha[:7]


def git_state(repo: Path | str) -> GitState:
    """Read `repo`'s branch, HEAD commit, and dirty flag.

    The only argument is a directory. It is passed to git as the value of `-C`,
    as its own element of the argument list, so a path containing a space, a
    quote, or a semicolon is a path and cannot become anything else.
    """
    directory = Path(repo)
    sha = _git(directory, _REV_PARSE_HEAD)
    if sha is None:
        return GitState(
            branch=None,
            sha=None,
            dirty=None,
            detail=f"{directory} could not be read as a git repository.",
        )
    branch = _git(directory, _CURRENT_BRANCH)
    status = _git(directory, _PORCELAIN_STATUS, allow_empty=True)
    dirty = None if status is None else bool(status.strip())
    if dirty is None:
        detail = "The working tree state could not be determined."
    elif dirty:
        detail = "The working tree has uncommitted changes."
    else:
        detail = "The working tree is clean."
    return GitState(branch=branch, sha=sha, dirty=dirty, detail=detail)


def _git(repo: Path, arguments: tuple[str, ...], *, allow_empty: bool = False) -> str | None:
    """Run one read-only git command in `repo`, or return None.

    `check=False` with an explicit return-code test, so a non-zero exit is a
    None rather than an exception: this is diagnostic context, and a missing
    `.git` must not stop a broker audit from running.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - literal argv, no shell, fixed program
            ["git", "-C", str(repo), *arguments],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    output = completed.stdout.strip()
    return output if output or allow_empty else None


__all__ = ["GitState", "git_state"]
