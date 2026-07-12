"""Scheduled / webhook-triggered agents — a small file-backed job store + firer.

A *job* binds an installed agent to a prompt and a trigger. Two triggers share
one job shape:

- **schedule** — an ``interval_seconds`` after which the job fires again (the
  lifespan scheduler loop drives this).
- **webhook** — every job carries an opaque ``webhook_token``; a POST to
  ``/v1/webhooks/{token}`` fires it. "Run now" fires it on demand too.

Firing means: discover an online host, then drive the same two-phase start the
web UI uses (``POST /v1/sessions`` bound to that host, then a ``message`` event)
against the app in-process via an ASGI transport — so a triggered run is a real
session the user can open and watch.

State lives in ``~/.omnicraft/scheduled_agents.json`` (no DB migration), mirroring
the evals store.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from typing import Any

_lock = threading.Lock()

# Keep only the most recent trigger outcomes per job so the file stays small.
_MAX_HISTORY = 20

# The first-party sentinel Origin that passes require_trusted_origin without a
# loopback host (see omnicraft/server/routes/_origin.py).
_INTERNAL_ORIGIN = "omnicraft://internal"


def _config_dir() -> str:
    override = os.environ.get("OMNICRAFT_CONFIG_HOME")
    base = override if override else os.path.join(os.path.expanduser("~"), ".omnicraft")
    os.makedirs(base, exist_ok=True)
    return base


def _path() -> str:
    return os.path.join(_config_dir(), "scheduled_agents.json")


def _load() -> dict[str, Any]:
    try:
        with open(_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("jobs", [])
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"jobs": []}


def _save(data: dict[str, Any]) -> None:
    with open(_path(), "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def _now() -> int:
    return int(time.time())


def _next_run_from(base: int, interval_seconds: int | None) -> int | None:
    if not interval_seconds or interval_seconds <= 0:
        return None
    return base + int(interval_seconds)


# --- CRUD ------------------------------------------------------------------


def list_jobs() -> list[dict[str, Any]]:
    with _lock:
        return list(_load()["jobs"])


def get_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        for job in _load()["jobs"]:
            if job.get("id") == job_id:
                return dict(job)
    return None


def get_job_by_token(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    with _lock:
        for job in _load()["jobs"]:
            if job.get("webhook_token") == token:
                return dict(job)
    return None


def create_job(
    *,
    name: str,
    agent_name: str,
    prompt: str,
    workspace: str | None = None,
    host_id: str | None = None,
    interval_seconds: int | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    now = _now()
    job = {
        "id": secrets.token_hex(8),
        "name": name.strip() or agent_name,
        "agent_name": agent_name,
        "prompt": prompt,
        "workspace": (workspace or "").strip() or None,
        "host_id": (host_id or "").strip() or None,
        "interval_seconds": int(interval_seconds) if interval_seconds else None,
        "enabled": bool(enabled),
        "webhook_token": secrets.token_urlsafe(24),
        "created_at": now,
        "last_run_at": None,
        "next_run_at": _next_run_from(now, interval_seconds) if enabled else None,
        "history": [],
    }
    with _lock:
        data = _load()
        data["jobs"].append(job)
        _save(data)
    return dict(job)


_UPDATABLE = {
    "name",
    "agent_name",
    "prompt",
    "workspace",
    "host_id",
    "interval_seconds",
    "enabled",
}


def update_job(job_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    with _lock:
        data = _load()
        for job in data["jobs"]:
            if job.get("id") != job_id:
                continue
            for key in _UPDATABLE:
                if key in patch:
                    job[key] = patch[key]
            if "interval_seconds" in patch:
                job["interval_seconds"] = (
                    int(patch["interval_seconds"]) if patch["interval_seconds"] else None
                )
            for key in ("workspace", "host_id"):
                if key in patch:
                    job[key] = (str(patch[key]).strip() or None) if patch[key] else None
            # Recompute the next fire time from now whenever the cadence or the
            # enabled flag changes, so a re-enabled or re-scheduled job doesn't
            # fire immediately off a stale timestamp.
            if job.get("enabled") and job.get("interval_seconds"):
                job["next_run_at"] = _next_run_from(_now(), job["interval_seconds"])
            else:
                job["next_run_at"] = None
            _save(data)
            return dict(job)
    return None


def delete_job(job_id: str) -> bool:
    with _lock:
        data = _load()
        before = len(data["jobs"])
        data["jobs"] = [j for j in data["jobs"] if j.get("id") != job_id]
        if len(data["jobs"]) == before:
            return False
        _save(data)
        return True


def record_run(job_id: str, entry: dict[str, Any]) -> None:
    """Append a trigger outcome and advance the schedule."""
    now = _now()
    entry = {"at": now, **entry}
    with _lock:
        data = _load()
        for job in data["jobs"]:
            if job.get("id") != job_id:
                continue
            job["last_run_at"] = now
            if job.get("enabled") and job.get("interval_seconds"):
                job["next_run_at"] = _next_run_from(now, job["interval_seconds"])
            history = job.setdefault("history", [])
            history.insert(0, entry)
            del history[_MAX_HISTORY:]
            _save(data)
            return


def due_jobs(now: int | None = None) -> list[dict[str, Any]]:
    """Enabled, interval-scheduled jobs whose next_run_at has passed."""
    ts = now if now is not None else _now()
    out: list[dict[str, Any]] = []
    with _lock:
        for job in _load()["jobs"]:
            if not job.get("enabled") or not job.get("interval_seconds"):
                continue
            nxt = job.get("next_run_at")
            if nxt is None or nxt <= ts:
                out.append(dict(job))
    return out


# --- Firing ----------------------------------------------------------------


def _pick_host(job: dict[str, Any], online_host_ids: list[str]) -> str | None:
    """The job's host if it is online, else the first online host."""
    want = job.get("host_id")
    if want and want in online_host_ids:
        return want
    if want and want not in online_host_ids:
        return None  # a specific host was requested but it is offline
    return online_host_ids[0] if online_host_ids else None


async def fire_job(
    app: Any, job: dict[str, Any], agent_store: Any, *, trigger: str
) -> dict[str, Any]:
    """Start a real run for ``job`` and record the outcome.

    :param trigger: how it was triggered — ``"schedule"``, ``"webhook"``, or
        ``"manual"`` — recorded in history.
    :returns: ``{"status": ..., "session_id": ...|None, "detail": ...}``.
    """
    import httpx

    def _fail(status: str, detail: str, session_id: str | None = None) -> dict[str, Any]:
        record_run(
            job["id"],
            {"trigger": trigger, "status": status, "detail": detail, "session_id": session_id},
        )
        return {"status": status, "detail": detail, "session_id": session_id}

    agent = None
    try:
        agent = agent_store.get_by_name(job.get("agent_name"))
    except Exception:  # noqa: BLE001 — treat any lookup error as "not found"
        agent = None
    if agent is None:
        return _fail("error", f"agente '{job.get('agent_name')}' não encontrado")

    host_registry = getattr(app.state, "host_registry", None)
    online = list(host_registry.online_host_ids()) if host_registry is not None else []
    host_id = _pick_host(job, online)
    if host_id is None:
        return _fail("skipped", "nenhum host online para executar")

    workspace = job.get("workspace")
    if not workspace:
        return _fail("error", "workspace não configurado para este job")
    body: dict[str, Any] = {"agent_id": agent.id, "host_id": host_id, "workspace": workspace}

    transport = httpx.ASGITransport(app=app)
    headers = {"Origin": _INTERNAL_ORIGIN, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://internal", timeout=45.0
        ) as client:
            res = await client.post("/v1/sessions", json=body, headers=headers)
            if res.status_code >= 400:
                return _fail(
                    "error", f"criar sessão falhou: HTTP {res.status_code} {res.text[:200]}"
                )
            session_id = res.json().get("id")
            if not session_id:
                return _fail("error", "sessão criada sem id")
            event = {
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": job.get("prompt", "")}],
                },
            }
            ev = await client.post(
                f"/v1/sessions/{session_id}/events", json=event, headers=headers
            )
            if ev.status_code >= 400:
                return _fail(
                    "error",
                    f"enviar mensagem falhou: HTTP {ev.status_code} {ev.text[:200]}",
                    session_id=session_id,
                )
    except Exception as exc:  # noqa: BLE001 — surface any transport error in history
        return _fail("error", f"falha ao disparar: {exc}")

    record_run(
        job["id"],
        {
            "trigger": trigger,
            "status": "started",
            "detail": "sessão iniciada",
            "session_id": session_id,
        },
    )
    return {"status": "started", "session_id": session_id, "detail": "sessão iniciada"}
