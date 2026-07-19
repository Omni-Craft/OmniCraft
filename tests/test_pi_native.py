"""Tests for Pi native CLI capability probes."""

from __future__ import annotations

from subprocess import CompletedProcess

import pytest

from omnicraft.pi_native import pi_supports_approve


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        ("0.78.9\n", "", False),
        ("0.79.0\n", "", True),
        ("", "pi 0.80.1\n", True),
        ("unexpected", "", False),
    ],
)
def test_pi_supports_approve_checks_version_from_both_streams(
    monkeypatch: pytest.MonkeyPatch, stdout: str, stderr: str, expected: bool
) -> None:
    def _run(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(
            args=["pi", "--version"], returncode=0, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr("subprocess.run", _run)
    assert pi_supports_approve("/usr/bin/pi") is expected
