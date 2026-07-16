"""Persistent registry of runner subprocesses spawned by the host daemon.

The host records every runner it spawns (pid + log path) in a JSON file so
a restarted host can RE-ADOPT runners that are still alive instead of
losing track of them. Runners hold their own WS tunnel to the server, so
they keep working across a host restart; this registry is what lets the
new host process report them in its hello frame and serve stop/stat
requests for them.

Entries are removed when a runner is stopped or observed dead. Adoption
guards against pid reuse by checking the process's command line actually
is an OmniCraft runner before trusting a recorded pid.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger("omnicraft.host")

# The runner entrypoint every spawned runner runs; used to verify a
# registry pid still belongs to a runner (and not a recycled pid).
_RUNNER_CMDLINE_MARKER = "omnicraft.runner._entry"


def _registry_path() -> Path:
    """Return the on-disk registry file path.

    Computed at call time (not a module constant) so tests that repoint
    ``Path.home`` see the override.

    :returns: ``Path.home() / ".omnicraft" / "host-runners.json"``.
    """
    return Path.home() / ".omnicraft" / "host-runners.json"


@dataclass
class RunnerRecord:
    """One persisted runner: enough to re-adopt it after a host restart.

    :param pid: The runner subprocess OS pid.
    :param log_path: File capturing the runner's stdout/stderr.
    """

    pid: int
    log_path: str


def load_records() -> dict[str, RunnerRecord]:
    """Load the persisted runner records.

    :returns: Mapping of runner_id → :class:`RunnerRecord`. Empty on a
        missing, unreadable, or malformed file (a corrupt registry must
        never block host boot).
    """
    path = _registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    records: dict[str, RunnerRecord] = {}
    for runner_id, entry in data.get("runners", {}).items():
        if not isinstance(entry, dict):
            continue
        pid = entry.get("pid")
        log_path = entry.get("log_path")
        if isinstance(pid, int) and isinstance(log_path, str):
            records[runner_id] = RunnerRecord(pid=pid, log_path=log_path)
    return records


def _save_records(records: dict[str, RunnerRecord]) -> None:
    """Atomically write *records* to the registry file.

    :param records: Mapping of runner_id → :class:`RunnerRecord`.
    """
    path = _registry_path()
    payload = {
        "runners": {
            rid: {"pid": rec.pid, "log_path": rec.log_path} for rid, rec in records.items()
        }
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".host-runners-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp_name, path)
    except OSError:
        # Persistence is best-effort: a write failure costs re-adoption
        # after the NEXT restart, never the current launch.
        _logger.debug("failed to persist runner registry", exc_info=True)


def add_record(runner_id: str, pid: int, log_path: Path) -> None:
    """Record a freshly spawned runner.

    :param runner_id: The runner id, e.g. ``"runner_abc123"``.
    :param pid: The runner subprocess pid.
    :param log_path: The runner's log file.
    """
    records = load_records()
    records[runner_id] = RunnerRecord(pid=pid, log_path=str(log_path))
    _save_records(records)


def remove_record(runner_id: str) -> None:
    """Drop a runner from the registry (stopped or observed dead).

    :param runner_id: The runner id to remove; unknown ids are a no-op.
    """
    records = load_records()
    if records.pop(runner_id, None) is not None:
        _save_records(records)


def _cmdline_from_proc(pid: int) -> str | None:
    """Return *pid*'s argv read from ``/proc``, or ``None`` when unusable.

    Preferred over ``ps`` because a plain file read cannot fail the ways
    spawning a helper can: no fork/exec, so no PATH lookup to miss the
    binary, no timeout to blow under load, and no subprocess to pay for
    on a path that runs once per registry entry.

    ``/proc`` is absent on macOS/BSD, and the read comes back empty for
    kernel threads and zombies. Both cases yield ``None`` so the caller
    falls back to ``ps``.

    :param pid: The process to inspect.
    :returns: The argv joined by spaces, or ``None`` if ``/proc`` gave
        nothing to match against.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    return raw.decode("utf-8", "replace").replace("\0", " ")


def _cmdline_from_ps(pid: int) -> str:
    """Return *pid*'s command line via ``ps``, or ``""`` when unavailable.

    The portable fallback for platforms without ``/proc``.

    Fail-closed: only a ``ps`` that exited cleanly is trusted. Output
    from a failed one could still contain the marker (an error echoing
    the command line, a partial listing) and adopting on it would let a
    later stop request SIGTERM whatever now holds a reused pid.

    Every failure degrades to "not a runner", which is indistinguishable
    from a genuinely reused pid — so each one is logged rather than
    swallowed. Without that, a runner silently fails to be re-adopted and
    the only evidence of why is gone.

    :param pid: The process to inspect.
    :returns: The ``ps`` command column, or ``""`` on any failure.
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        _logger.warning("ps probe for pid %s failed; treating as not-a-runner", pid, exc_info=True)
        return ""
    if result.returncode == 0:
        return result.stdout
    stderr = result.stderr.strip()
    # rc=1 is both ps's ordinary "no such process" — the common case for a
    # dead runner — and its generic error code, so the exit alone cannot
    # tell them apart. A silent rc=1 is the ordinary absence; anything
    # that came with a complaint is a real failure worth surfacing.
    if result.returncode != 1 or stderr:
        _logger.warning(
            "ps probe for pid %s exited %s: %s",
            pid,
            result.returncode,
            stderr or "<no stderr>",
        )
    return ""


def pid_is_live_runner(pid: int) -> bool:
    """Return whether *pid* is alive AND still an OmniCraft runner process.

    Guards adoption against pid reuse: after a reboot (or enough process
    churn) a recorded pid can belong to an unrelated process, and adopting
    that would let a stop request SIGTERM an innocent bystander.

    :param pid: The recorded runner pid.
    :returns: ``True`` only when the pid is alive and its command line
        contains the runner entrypoint module.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive but not ours — a runner spawned by this user is always
        # signalable, so a foreign pid means reuse.
        return False
    cmdline = _cmdline_from_proc(pid)
    if cmdline is None:
        cmdline = _cmdline_from_ps(pid)
    return _RUNNER_CMDLINE_MARKER in cmdline
