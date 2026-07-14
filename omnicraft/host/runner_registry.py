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
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return _RUNNER_CMDLINE_MARKER in result.stdout
