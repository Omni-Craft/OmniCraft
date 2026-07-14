"""Tests for login-shell PATH resolution at daemon boot.

A daemon spawned by the desktop app inherits the bare GUI PATH; CLIs
installed under nvm/brew then probe as "binary-missing" even though the
user runs them fine (observed live with codex under ~/.nvm). These lock
in the marker parsing, the merge semantics, and the never-raise fallback.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnicraft.host import shell_env


def test_merged_path_orders_and_dedupes() -> None:
    merged = shell_env.merged_path(
        "/nvm/bin:/usr/bin",
        "/usr/bin:/bin",
        ["/opt/homebrew/bin", "/bin"],
    )
    assert merged == "/nvm/bin:/usr/bin:/bin:/opt/homebrew/bin"


def test_merged_path_handles_missing_sources() -> None:
    assert shell_env.merged_path(None, None, []) == ""
    assert shell_env.merged_path(None, "/usr/bin", []) == "/usr/bin"


def test_resolve_parses_between_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    noisy = f"motd banner\n{shell_env._MARKER}/nvm/bin:/usr/bin{shell_env._MARKER}\ntrailing"

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=noisy, stderr="")

    monkeypatch.setattr(shell_env.subprocess, "run", fake_run)
    assert shell_env.resolve_login_shell_path() == "/nvm/bin:/usr/bin"


def test_resolve_returns_none_on_shell_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="zsh", timeout=1)

    monkeypatch.setattr(shell_env.subprocess, "run", boom)
    assert shell_env.resolve_login_shell_path() is None


def test_resolve_returns_none_without_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="garbage", stderr="")

    monkeypatch.setattr(shell_env.subprocess, "run", fake_run)
    assert shell_env.resolve_login_shell_path() is None


def test_nvm_dirs_newest_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nvm = tmp_path / ".nvm" / "versions" / "node"
    for v in ("v20.1.0", "v22.22.3", "v9.9.9"):
        (nvm / v / "bin").mkdir(parents=True)
    monkeypatch.setattr(shell_env.Path, "home", staticmethod(lambda: tmp_path))
    dirs = shell_env._nvm_bin_dirs()
    assert [Path(d).parent.name for d in dirs] == ["v22.22.3", "v20.1.0", "v9.9.9"]


def test_augment_never_raises_and_sets_env(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise OSError("no shell")

    monkeypatch.setattr(shell_env.subprocess, "run", boom)
    monkeypatch.setenv("PATH", "/usr/bin")
    final = shell_env.augment_path_from_login_shell()
    # Current PATH survives; existing fallback dirs may be appended.
    assert final.split(":")[0] == "/usr/bin"
    import os

    assert os.environ["PATH"] == final
