"""Provider-agnostic tests for the :class:`SandboxLauncher` base behavior.

The exec-model defaults (``run_background`` / ``start_host``) are shared by
every provider whose sandbox is a bare box the server execs into (Modal,
Daytona, E2B, Boxlite, Islo, …), so they are tested once here against a
minimal recording launcher rather than per provider.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import ClassVar

from omnicraft.onboarding.sandboxes.base import (
    RemoteCommandResult,
    SandboxLauncher,
    foreground_kill_command,
    foreground_pidfile,
    foreground_record_prefix,
)


class _RecordingLauncher(SandboxLauncher):
    """Minimal exec-model launcher that records every ``run`` command."""

    provider: ClassVar[str] = "recording"

    def __init__(self, home: str = "/root") -> None:
        self.commands: list[str] = []
        self.backgrounded: list[str] = []
        self._home = home

    def prepare(self) -> None:  # pragma: no cover - unused preflight stub
        pass

    def provision(self, name: str) -> str:  # pragma: no cover - unused stub
        return "sb-1"

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        self.commands.append(command)
        # start_host probes $HOME first; everything else returns empty.
        stdout = self._home if command == 'printf %s "$HOME"' else ""
        return RemoteCommandResult(returncode=0, stdout=stdout, stderr="")

    def run_background(
        self, sandbox_id: str, command: str, *, log_path: str = "/tmp/omnicraft-host.log"
    ) -> RemoteCommandResult:
        # Capture the raw (pre-wrap) command so a test can prove a real shell
        # honors its env prefix, independent of the setsid/nohup wrapper.
        self.backgrounded.append(command)
        return super().run_background(sandbox_id, command, log_path=log_path)


def test_run_background_wraps_command_in_sh_c() -> None:
    """
    ``run_background`` must wrap the command in ``sh -c`` so env-var prefixes
    survive ``nohup``. ``nohup ENV=val cmd`` makes nohup try to exec a program
    literally named ``ENV=val`` ("No such file or directory") — re-parsing under
    ``sh -c`` lets the inner shell apply the assignment before running ``cmd``.
    Regression: managed Daytona/Modal hosts never came online because the
    in-sandbox ``omnicraft host`` launch died on its ``OMNICRAFT_HOST_TOKEN=…``
    prefix.
    """
    launcher = _RecordingLauncher()

    launcher.run_background("sb-1", "FOO=bar omnicraft host --server https://srv")

    [cmd] = launcher.commands
    assert cmd == (
        "setsid nohup sh -c 'FOO=bar omnicraft host --server https://srv' "
        "> /tmp/omnicraft-host.log 2>&1 < /dev/null & echo launched"
    )


def test_start_host_env_prefix_is_honored_by_a_real_shell() -> None:
    """
    The env-prefixed command ``start_host`` hands to ``run_background`` must
    apply its ``OMNICRAFT_HOST_*`` assignments when re-parsed by a shell — the
    exact thing the ``sh -c`` wrapper restores. Run the raw command through a
    real ``sh -c`` (the inner shell of the wrapper) with ``omnicraft host``
    swapped for a probe that echoes the injected vars; the broken bare-``nohup``
    form would never reach this assignment-honoring shell.
    """
    launcher = _RecordingLauncher()

    workspace = launcher.start_host(
        "sb-1",
        token="tok-123",
        host_id="host_abc",
        host_name="managed-abc",
        server_url="https://srv",
    )
    assert workspace == "/root/workspace"

    [raw] = launcher.backgrounded
    # A nested `sh -c` reads the *inherited* env (a bare `$VAR` in the same
    # simple command would expand in the parent shell, before the temporary
    # assignment takes effect — and print empty).
    probe = raw.replace(
        "omnicraft host --server https://srv",
        "sh -c 'printf %s:%s:%s "
        '"$OMNICRAFT_HOST_TOKEN" "$OMNICRAFT_HOST_ID" "$OMNICRAFT_HOST_NAME"\'',
    )
    out = subprocess.run(
        ["sh", "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "tok-123:host_abc:managed-abc"


# ── foreground pidfile helper (exec_foreground kill path) ───


def test_foreground_pidfile_is_unpredictable() -> None:
    """Each allocation is a fresh, unguessable dir so a less-privileged
    co-resident process can't predict the path to pre-seed a symlink the
    (more-privileged) foreground would write its pid through."""
    run_dir, pidfile = foreground_pidfile()
    run_dir2, _ = foreground_pidfile()

    assert run_dir.startswith("/tmp/oa-foreground-")
    assert pidfile == f"{run_dir}/pid"
    # No fixed path: two allocations never collide.
    assert run_dir != run_dir2


def test_foreground_record_prefix_mkdir_fails_closed_on_existing_dir(
    tmp_path: Path,
) -> None:
    """``mkdir -m 700`` (no ``-p``) fails if the dir already exists — the
    fail-closed property that stops a co-tenant pre-seeding the run dir (e.g.
    as a symlink we'd otherwise write our pid through). Proven by running the
    real shell fragment twice against the same path."""
    run_dir = tmp_path / "oa-foreground-fixed"
    prefix = foreground_record_prefix(f"{run_dir}/pid")

    # First run creates the mode-700 dir and records the pid (the chained
    # `echo` only runs because mkdir succeeded).
    first = subprocess.run(["bash", "-c", f"{prefix}true"], capture_output=True, text=True)
    assert first.returncode == 0
    assert (run_dir / "pid").exists()
    assert oct((run_dir).stat().st_mode & 0o777) == "0o700"

    # Second run hits the existing dir: mkdir errors, the `&&` chain stops,
    # so nothing is written through a pre-existing path.
    second = subprocess.run(["bash", "-c", f"{prefix}true"], capture_output=True, text=True)
    assert second.returncode != 0
    assert "File exists" in second.stderr


def test_foreground_kill_command_signals_only_a_plausible_pid(tmp_path: Path) -> None:
    """Only a plausible pid ever reaches ``kill``. Empty, non-numeric, or
    leading-zero content — including ``0`` (``kill 0`` signals the whole
    process group) and ``00`` — is rejected by the ``case`` gate, so
    unvalidated file contents never signal a process. Proven by running the
    real shell command with ``kill`` shadowed by a probe that records what
    would be signalled."""
    run_dir = tmp_path / "rundir"
    pidfile = f"{run_dir}/pid"
    marker = tmp_path / "killed"

    def observe(content: str) -> str | None:
        run_dir.mkdir(exist_ok=True)
        Path(pidfile).write_text(content)
        marker.unlink(missing_ok=True)
        # Shadow `kill` with a function that records its target *outside* the
        # run dir (which the command rm -rf's on the way out), so we see
        # exactly what — if anything — gets signalled.
        harness = f'kill() {{ printf "%s" "$1" > {marker}; }}; ' + foreground_kill_command(pidfile)
        subprocess.run(["bash", "-c", harness], check=True)
        return marker.read_text() if marker.exists() else None

    # A real numeric pid is signalled.
    assert observe("12345") == "12345"
    # `kill 0` / all-zeros never fire (they'd hit the whole process group).
    assert observe("0") is None
    assert observe("00") is None
    # Non-numeric / planted payloads are never signalled (and never eval'd —
    # the pid is captured into a variable, so injection is impossible too).
    assert observe("0; rm -rf /") is None
    assert observe("evil") is None
    assert observe("") is None
    # The run dir is cleaned up on the way out.
    assert not run_dir.exists()
