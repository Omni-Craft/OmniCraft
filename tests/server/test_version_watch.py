"""Update detection: the running commit versus the one checked out now.

The property that matters is asymmetric. Missing an available update is a
mild annoyance; claiming one that is not there — prompting a restart on a
hunch — is worse, so every "unknown" reading must resolve to "no update", not
to a guess. These tests pin that: an update is announced only when both
commits are known and genuinely differ.
"""

from __future__ import annotations

import subprocess

import pytest

from omnicraft.server import version_watch


@pytest.fixture(autouse=True)
def _clear_startup_cache() -> None:
    """The startup commit is cached for the process; reset it per test."""
    version_watch._startup_commit.cache_clear()


def _pin(monkeypatch: pytest.MonkeyPatch, *, startup: str | None, current: str | None) -> None:
    """Pin the two commits the status compares.

    :param monkeypatch: Fixture used to replace the git readers.
    :param startup: Commit the process started on.
    :param current: Commit checked out right now.
    """
    monkeypatch.setattr(version_watch, "_startup_commit", lambda: startup)
    monkeypatch.setattr(version_watch, "_repo_root", lambda: version_watch.Path("/repo"))
    monkeypatch.setattr(version_watch, "_git_head", lambda _root: current)


def test_no_update_when_the_checkout_has_not_moved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same commit running and on disk: nothing to offer."""
    _pin(monkeypatch, startup="abc123", current="abc123")

    status = version_watch.update_status()

    assert status.update_available is False
    assert status.running_commit == "abc123"
    assert status.current_commit == "abc123"


def test_update_when_the_checkout_moved_ahead(monkeypatch: pytest.MonkeyPatch) -> None:
    """New code was pulled or committed under a still-running server."""
    _pin(monkeypatch, startup="abc123", current="def456")

    status = version_watch.update_status()

    assert status.update_available is True
    assert status.running_commit == "abc123"
    assert status.current_commit == "def456"


def test_no_update_claimed_when_the_current_commit_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed read is not a difference. Never prompt on a missing side."""
    _pin(monkeypatch, startup="abc123", current=None)

    status = version_watch.update_status()

    assert status.update_available is False


def test_no_update_claimed_off_a_git_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A packaged (non-git) install has no baseline, so it never nags."""
    monkeypatch.setattr(version_watch, "_startup_commit", lambda: None)
    monkeypatch.setattr(version_watch, "_repo_root", lambda: None)

    status = version_watch.update_status()

    assert status.update_available is False
    assert status.running_commit is None
    assert status.current_commit is None


def test_git_missing_reads_as_unknown_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """`git` absent from PATH is an ordinary state, not an error."""

    def _no_git(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _no_git)

    assert version_watch._git_head(version_watch.Path("/repo")) is None


def test_git_timeout_reads_as_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stuck filesystem/lock resolves to unknown, never a hang or a claim."""

    def _timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=version_watch._GIT_TIMEOUT_S)

    monkeypatch.setattr(subprocess, "run", _timeout)

    assert version_watch._git_head(version_watch.Path("/repo")) is None


def test_git_nonzero_exit_reads_as_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A git error (e.g. not a repo) is unknown, not an update."""

    def _fail(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["git"], returncode=128, stdout="", stderr="nope")

    monkeypatch.setattr(subprocess, "run", _fail)

    assert version_watch._git_head(version_watch.Path("/repo")) is None


def test_serializes_the_three_fields_the_ui_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wire shape the banner depends on."""
    _pin(monkeypatch, startup="abc123", current="def456")

    assert version_watch.update_status().as_dict() == {
        "running_commit": "abc123",
        "current_commit": "def456",
        "update_available": True,
    }
