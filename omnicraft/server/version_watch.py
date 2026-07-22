"""Detect when the running server is behind the code on disk.

OmniCraft is often installed editable from a git checkout: the process
imports its modules once at startup, so pulling or committing new code moves
the working copy ahead while the running server keeps executing the old
modules. This module notices exactly that — the commit the process started on
versus the commit checked out now — so the UI can offer a restart.

It never guesses. If the package is not inside a git checkout, or ``git`` is
absent, or the command times out, the status is simply "unknown" and no update
is claimed. A restart prompt that fires on a hunch is worse than none.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# How long to wait on a git call before treating the repo state as unknown.
# A local ``rev-parse`` answers in milliseconds; anything slower is a stuck
# filesystem or lock we would rather report as "unknown" than block a request.
_GIT_TIMEOUT_S = 2.0


def _repo_root() -> Path | None:
    """
    Return the git checkout the package lives in, or ``None``.

    :returns: Repo root path, e.g. ``Path("/Users/me/OmniCraft")``, or
        ``None`` when the package is not inside a working tree.
    """
    # omnicraft/server/version_watch.py -> repo root is three parents up.
    start = Path(__file__).resolve().parents[2]
    head = _git_head(start)
    return start if head is not None else None


def _git_head(repo_root: Path) -> str | None:
    """
    Return the current ``HEAD`` commit of *repo_root*, or ``None``.

    :param repo_root: Directory to read the commit from.
    :returns: Full 40-char sha, or ``None`` if it cannot be read (not a
        repo, ``git`` missing, timeout, or any git error).
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    return head or None


@lru_cache(maxsize=1)
def _startup_commit() -> str | None:
    """
    Return the commit the server process started on.

    Captured once and cached for the process lifetime: this is the baseline
    the live status compares against, and it must not move while the server
    runs even as the working copy does.

    :returns: Full sha, or ``None`` when not a git checkout.
    """
    root = _repo_root()
    return _git_head(root) if root is not None else None


@dataclass(frozen=True)
class UpdateStatus:
    """Whether the checkout has moved ahead of the running server.

    :param running_commit: Commit the process started on, or ``None`` when
        the version is not tracked in git.
    :param current_commit: Commit checked out right now, or ``None``.
    :param update_available: ``True`` only when both commits are known and
        differ — never on a missing reading.
    """

    running_commit: str | None
    current_commit: str | None
    update_available: bool

    def as_dict(self) -> dict[str, object]:
        """
        Serialize for the JSON endpoint.

        :returns: ``{"running_commit", "current_commit",
            "update_available"}``.
        """
        return {
            "running_commit": self.running_commit,
            "current_commit": self.current_commit,
            "update_available": self.update_available,
        }


def update_status() -> UpdateStatus:
    """
    Compare the running commit with the one checked out now.

    :returns: The current :class:`UpdateStatus`. ``update_available`` is
        ``True`` only when both commits are known and differ.
    """
    running = _startup_commit()
    root = _repo_root()
    current = _git_head(root) if root is not None else None
    available = running is not None and current is not None and running != current
    return UpdateStatus(
        running_commit=running,
        current_commit=current,
        update_available=available,
    )
