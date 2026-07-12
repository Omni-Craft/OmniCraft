"""Agent evaluations — suites, runs, and grading.

A lightweight regression harness for agents: a *suite* is a named set of tasks
(a prompt + a pass/fail check); a *run* records one execution of a suite against
an agent, grading each task's output. Comparing runs surfaces regressions
(a task that passed before and fails now).

Stored as a single JSON file under the config dir — no schema migration, fine
for a self-hosted deployment. Grading is pure and side-effect free (tested).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
from typing import Any

_lock = threading.Lock()

# Supported check types for auto-grading a task's final output.
_CHECK_TYPES = {"contains", "not_contains", "regex"}


def _config_dir() -> str:
    override = os.environ.get("OMNICRAFT_CONFIG_HOME")
    base = override if override else os.path.join(os.path.expanduser("~"), ".omnicraft")
    os.makedirs(base, exist_ok=True)
    return base


def _path() -> str:
    return os.path.join(_config_dir(), "evals.json")


def _load() -> dict[str, Any]:
    try:
        with open(_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("suites", [])
            data.setdefault("runs", [])
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"suites": [], "runs": []}


def _save(data: dict[str, Any]) -> None:
    # Write-then-rename so a crash mid-write never corrupts the store.
    path = _path()
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=os.path.dirname(path), suffix=".tmp", delete=False
    ) as fh:
        json.dump(data, fh)
        tmp = fh.name
    os.replace(tmp, path)


def _now() -> int:
    return int(time.time())


# ── Grading ──────────────────────────────────────────────────────────


def grade(output: str, check: dict[str, Any]) -> bool:
    """Return whether ``output`` passes ``check``.

    :param output: The agent's final text for the task.
    :param check: ``{"type": "contains"|"not_contains"|"regex", "value": str}``.
    :returns: ``True`` if the check passes. An unknown/blank check passes
        (treated as "no assertion") so a task without a check never fails.
    """
    ctype = check.get("type")
    value = check.get("value")
    if not isinstance(value, str) or not value:
        return True
    text = output or ""
    if ctype == "contains":
        return value.lower() in text.lower()
    if ctype == "not_contains":
        return value.lower() not in text.lower()
    if ctype == "regex":
        # Cheap ReDoS guard: an oversized pattern fails, an oversized output is
        # truncated, so a pathological regex can't stall the server.
        if len(value) > 512:
            return False
        try:
            return re.search(value, text[:200_000], re.IGNORECASE | re.DOTALL) is not None
        except re.error:
            return False
    return True


def _normalize_check(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"type": "contains", "value": ""}
    ctype = raw.get("type")
    if ctype not in _CHECK_TYPES:
        ctype = "contains"
    value = raw.get("value")
    return {"type": ctype, "value": value if isinstance(value, str) else ""}


# ── Suites ───────────────────────────────────────────────────────────


def list_suites() -> list[dict[str, Any]]:
    with _lock:
        return list(_load().get("suites", []))


def create_suite(name: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Create a suite from a name + tasks (each ``{prompt, check}``)."""
    suite = {
        "id": f"suite_{secrets.token_hex(6)}",
        "name": name.strip() or "Sem nome",
        "created_at": _now(),
        "tasks": [
            {
                "id": f"task_{secrets.token_hex(4)}",
                "prompt": str(t.get("prompt", "")).strip(),
                "check": _normalize_check(t.get("check")),
            }
            for t in tasks
            if isinstance(t, dict) and str(t.get("prompt", "")).strip()
        ],
    }
    with _lock:
        data = _load()
        data["suites"].append(suite)
        _save(data)
    return suite


def get_suite(suite_id: str) -> dict[str, Any] | None:
    with _lock:
        return next((s for s in _load().get("suites", []) if s.get("id") == suite_id), None)


def delete_suite(suite_id: str) -> None:
    with _lock:
        data = _load()
        data["suites"] = [s for s in data["suites"] if s.get("id") != suite_id]
        data["runs"] = [r for r in data["runs"] if r.get("suite_id") != suite_id]
        _save(data)


# ── Runs ─────────────────────────────────────────────────────────────


def record_run(
    suite_id: str, label: str, task_outputs: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Grade a suite execution and store the run.

    :param suite_id: The suite that was run.
    :param label: A human label for this run (e.g. the agent/version).
    :param task_outputs: ``[{task_id, session_id?, output}]`` from the client.
    :returns: The stored run (graded), or ``None`` if the suite is gone.
    """
    suite = get_suite(suite_id)
    if suite is None:
        return None
    outputs_by_task = {o.get("task_id"): o for o in task_outputs if isinstance(o, dict)}
    results: list[dict[str, Any]] = []
    passed = 0
    for task in suite.get("tasks", []):
        out = outputs_by_task.get(task["id"], {})
        output = str(out.get("output", ""))
        ok = grade(output, task.get("check", {}))
        if ok:
            passed += 1
        results.append(
            {
                "task_id": task["id"],
                "prompt": task.get("prompt", ""),
                "check": task.get("check", {}),
                "session_id": out.get("session_id"),
                "passed": ok,
                "output": output[:2000],
            }
        )
    run = {
        "id": f"run_{secrets.token_hex(6)}",
        "suite_id": suite_id,
        "label": label.strip() or "execução",
        "created_at": _now(),
        "passed": passed,
        "total": len(results),
        "results": results,
    }
    with _lock:
        data = _load()
        data["runs"].append(run)
        _save(data)
    return run


def list_runs(suite_id: str) -> list[dict[str, Any]]:
    """Runs of a suite, newest first."""
    with _lock:
        runs = [r for r in _load().get("runs", []) if r.get("suite_id") == suite_id]
    # Reverse first so that among same-second runs the later-appended (newer)
    # one wins the stable sort's tie.
    runs.reverse()
    return sorted(runs, key=lambda r: r.get("created_at", 0), reverse=True)
