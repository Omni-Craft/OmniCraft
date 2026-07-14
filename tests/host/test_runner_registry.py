"""Tests for the persistent host runner registry (re-adoption support)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnicraft.host import runner_registry


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "host-runners.json"
    monkeypatch.setattr(runner_registry, "_registry_path", lambda: path)
    return path


def test_add_load_remove_roundtrip(tmp_path: Path) -> None:
    runner_registry.add_record("runner_a", 111, tmp_path / "a.log")
    runner_registry.add_record("runner_b", 222, tmp_path / "b.log")
    records = runner_registry.load_records()
    assert records["runner_a"].pid == 111
    assert records["runner_b"].log_path == str(tmp_path / "b.log")

    runner_registry.remove_record("runner_a")
    assert set(runner_registry.load_records()) == {"runner_b"}
    # Removing an unknown id is a no-op, not an error.
    runner_registry.remove_record("runner_zzz")


def test_load_records_tolerates_missing_and_malformed(_isolated_registry: Path) -> None:
    assert runner_registry.load_records() == {}
    _isolated_registry.write_text("{not json", encoding="utf-8")
    assert runner_registry.load_records() == {}
    _isolated_registry.write_text('{"runners": {"r": {"pid": "not-int"}}}', encoding="utf-8")
    assert runner_registry.load_records() == {}


def test_pid_is_live_runner_rejects_dead_and_foreign_pids() -> None:
    # A dead pid is never a runner.
    proc = subprocess.Popen(["sleep", "0"])
    proc.wait()
    assert runner_registry.pid_is_live_runner(proc.pid) is False
    # A live process that is NOT a runner (this test's own python) is
    # rejected by the cmdline marker — the pid-reuse guard.
    assert runner_registry.pid_is_live_runner(os.getpid()) is False


def test_pid_is_live_runner_accepts_real_runner_cmdline() -> None:
    # A live process whose command line carries the runner entrypoint
    # marker is accepted. Spawn a sleeper that merely LOOKS like a runner
    # (argv carries the marker) — the guard checks cmdline, not behavior.
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time; time.sleep(30)  # omnicraft.runner._entry",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert runner_registry.pid_is_live_runner(proc.pid) is True
    finally:
        proc.terminate()
        proc.wait()
