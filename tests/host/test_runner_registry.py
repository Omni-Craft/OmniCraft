"""Tests for the persistent host runner registry (re-adoption support)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from omnicraft.host import runner_registry

_MARKER = runner_registry._RUNNER_CMDLINE_MARKER


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


@contextmanager
def _fake_runner(tmp_path: Path) -> Iterator[subprocess.Popen[bytes]]:
    """Spawn a sleeper that merely LOOKS like a runner (argv carries the
    marker) — the guard checks cmdline, not behavior.

    Waits for the child to touch a ready file before yielding, so the
    assertions never depend on how fast it gets going. Production is not
    exposed to this: adoption reads pids off disk, long after the process
    started, while asserting straight after ``Popen`` would not.
    """
    # A long argv puts the marker deep in the line, which is what caught ps
    # cutting to display width in CI (probable cause: COLUMNS set there).
    ready = tmp_path / "runner-ready"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import pathlib, time; pathlib.Path({str(ready)!r}).touch(); "
            "time.sleep(30)  # omnicraft.runner._entry",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5.0
        while not ready.exists():
            assert time.monotonic() < deadline, "fake runner never started"
            time.sleep(0.01)
        yield proc
    finally:
        proc.terminate()
        proc.wait()


def test_pid_is_live_runner_accepts_real_runner_cmdline(tmp_path: Path) -> None:
    # A live process whose command line carries the runner entrypoint
    # marker is accepted.
    with _fake_runner(tmp_path) as proc:
        assert runner_registry.pid_is_live_runner(proc.pid) is True


def test_pid_is_live_runner_accepts_via_ps_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Platforms without /proc (macOS, BSD, restricted containers) fall
    # back to ps; force that branch so it stays covered on Linux too.
    monkeypatch.setattr(runner_registry, "_cmdline_from_proc", lambda pid: None)
    with _fake_runner(tmp_path) as proc:
        assert runner_registry.pid_is_live_runner(proc.pid) is True
    # The pid-reuse guard must survive the fallback path as well.
    assert runner_registry.pid_is_live_runner(os.getpid()) is False


def test_cmdline_from_ps_asks_for_unlimited_width(monkeypatch: pytest.MonkeyPatch) -> None:
    # ps cuts a long argv to the display width, which drops the marker and
    # makes a live runner read as reused. BSD ps ignores COLUMNS when not on
    # a terminal, so losing either defence is invisible on macOS and only
    # bites on Linux — assert the invocation instead of the output.
    captured: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setattr(runner_registry.subprocess, "run", fake_run)
    runner_registry._cmdline_from_ps(4242)

    args = captured["args"]
    assert isinstance(args, list)
    # Both procps and BSD document a repeated -w as "unlimited width".
    assert args.count("-w") == 2, args
    env = captured["env"]
    assert isinstance(env, dict)
    assert "COLUMNS" not in env, "COLUMNS reached ps and would cap the argv"
    assert env.get("PATH") == os.environ["PATH"], "ps must still be findable"


def test_cmdline_from_ps_logs_and_degrades_when_ps_cannot_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A ps that cannot be spawned (binary absent, PATH stripped) must not
    # raise — but it must leave a trace: the empty result it degrades to
    # is otherwise indistinguishable from a genuinely reused pid.
    def boom(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError(2, "No such file or directory: 'ps'")

    monkeypatch.setattr(runner_registry.subprocess, "run", boom)
    with caplog.at_level(logging.WARNING, logger="omnicraft.host"):
        assert runner_registry._cmdline_from_ps(4242) == ""
    assert "ps probe for pid 4242 failed" in caplog.text


def _ps_returning(
    returncode: int, stdout: str = "", stderr: str = ""
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=stderr
        )

    return fake_run


def test_cmdline_from_ps_fails_closed_when_exit_code_unexpected(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A failing ps must never be trusted to say a pid IS a runner: its
    # stdout can carry anything (an error echoing the command line, a
    # partial listing). Trusting it would adopt — and later SIGTERM — a
    # reused pid, so any doubt degrades to "not a runner".
    monkeypatch.setattr(
        runner_registry.subprocess,
        "run",
        _ps_returning(2, stdout=f"ps: error near {_MARKER}", stderr="ps: bad flag"),
    )
    with caplog.at_level(logging.WARNING, logger="omnicraft.host"):
        assert runner_registry._cmdline_from_ps(4242) == ""
    assert "ps: bad flag" in caplog.text

    # The guard itself must hold, not just the helper: a live pid (our own)
    # with /proc unavailable must stay False even though ps's stdout
    # carried the marker.
    monkeypatch.setattr(runner_registry, "_cmdline_from_proc", lambda pid: None)
    assert runner_registry.pid_is_live_runner(os.getpid()) is False


def test_cmdline_from_ps_stays_quiet_when_pid_simply_absent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # rc=1 with nothing on stderr is ps's ordinary "no such process" — the
    # common case for a dead runner. It must not log, or the warning that
    # marks a real failure drowns in noise.
    monkeypatch.setattr(runner_registry.subprocess, "run", _ps_returning(1))
    with caplog.at_level(logging.WARNING, logger="omnicraft.host"):
        assert runner_registry._cmdline_from_ps(4242) == ""
    assert caplog.text == ""


def test_cmdline_from_ps_logs_rc1_that_carries_a_complaint(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # rc=1 is also ps's generic error code, so it cannot mean "absent" on
    # its own. When it arrives with a complaint on stderr, that is a real
    # failure and has to be surfaced.
    monkeypatch.setattr(
        runner_registry.subprocess,
        "run",
        _ps_returning(1, stderr="ps: permission denied"),
    )
    with caplog.at_level(logging.WARNING, logger="omnicraft.host"):
        assert runner_registry._cmdline_from_ps(4242) == ""
    assert "ps: permission denied" in caplog.text
