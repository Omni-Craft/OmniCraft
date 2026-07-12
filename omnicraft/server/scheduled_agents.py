"""Scheduled / webhook-triggered agents — a small file-backed job store + firer.

A *job* binds an installed agent to a prompt and a trigger. Triggers share one
job shape:

- **interval** — an ``interval_seconds`` after which the job fires again.
- **cron** — a 5-field cron expression evaluated in ``tz`` (e.g. ``0 9 * * 1-5``
  for weekdays at 09:00). No dependency: :func:`_cron_next` scans forward minute
  by minute in the target timezone with the stdlib ``zoneinfo``.
- **webhook** — every job carries an opaque ``webhook_token``; a POST to
  ``/v1/webhooks/{token}`` fires it, and the POST body is interpolated into the
  prompt via ``{{path.to.field}}`` placeholders. "Run now" fires it on demand.

Firing means: discover an online host, then drive the same two-phase start the
web UI uses (``POST /v1/sessions`` bound to that host, then a ``message`` event)
against the app in-process via an ASGI transport — so a triggered run is a real
session the user can open and watch. Scheduled fires skip while the previous run
is still active (``no_overlap``) and retry soon when no host is online, instead
of losing a whole interval.

State lives in ``~/.omnicraft/scheduled_agents.json`` (no DB migration).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_lock = threading.Lock()

# Keep only the most recent trigger outcomes per job so the file stays small.
_MAX_HISTORY = 20

# How soon to retry a scheduled fire that was skipped (no host / still running),
# rather than waiting the full interval / next cron slot.
_RETRY_SECONDS = 60

# The first-party sentinel Origin that passes require_trusted_origin without a
# loopback host (see omnicraft/server/routes/_origin.py).
_INTERNAL_ORIGIN = "omnicraft://internal"

_PLACEHOLDER = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


# --- cron -----------------------------------------------------------------


def _field_match(spec: str, value: int, lo: int, hi: int) -> bool:
    """Match one cron field (supports ``*``, ``a-b``, ``a,b``, ``*/s``, ``a-b/s``)."""
    for part in spec.split(","):
        rng, _, step_s = part.partition("/")
        step = int(step_s) if step_s else 1
        if step <= 0:
            continue
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, b = rng.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(rng)
        if start <= value <= end and (value - start) % step == 0:
            return True
    return False


def parse_cron(expr: str) -> list[str]:
    """Split + validate a 5-field cron expression. Raises ValueError if invalid."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("cron precisa de 5 campos: minuto hora dia mês dia-da-semana")
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    # Validate each field parses against a representative value.
    for spec, (lo, hi) in zip(fields, bounds, strict=True):
        for part in spec.split(","):
            rng, _, step_s = part.partition("/")
            if step_s and (not step_s.isdigit() or int(step_s) == 0):
                raise ValueError(f"passo inválido em '{part}'")
            if rng == "*":
                continue
            pieces = rng.split("-") if "-" in rng else [rng]
            for p in pieces:
                if not p.lstrip("-").isdigit():
                    raise ValueError(f"campo cron inválido: '{part}'")
                if not (lo <= int(p) <= hi):
                    raise ValueError(f"valor {p} fora do intervalo {lo}-{hi}")
    return fields


def _cron_matches(fields: list[str], dt: datetime) -> bool:
    minute_ok = _field_match(fields[0], dt.minute, 0, 59)
    hour_ok = _field_match(fields[1], dt.hour, 0, 23)
    month_ok = _field_match(fields[3], dt.month, 1, 12)
    cron_dow = (dt.weekday() + 1) % 7  # python Mon=0..Sun=6 → cron Sun=0..Sat=6
    dom_restricted = fields[2] != "*"
    dow_restricted = fields[4] != "*"
    dom_ok = _field_match(fields[2], dt.day, 1, 31)
    dow_ok = _field_match(fields[4], cron_dow, 0, 7) or (
        cron_dow == 0 and _field_match(fields[4], 7, 0, 7)
    )
    if dom_restricted and dow_restricted:
        day_ok = dom_ok or dow_ok
    elif dom_restricted:
        day_ok = dom_ok
    elif dow_restricted:
        day_ok = dow_ok
    else:
        day_ok = True
    return minute_ok and hour_ok and month_ok and day_ok


def _tz(tz_name: str | None) -> ZoneInfo:
    if not tz_name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _cron_next(expr: str, tz_name: str | None, after_ts: int) -> int | None:
    """Next epoch second (minute-aligned) matching ``expr`` in ``tz`` after ``after_ts``.

    Iterates real epoch minutes and matches wall-clock fields, so DST is handled
    correctly. Bounded to ~4 years so rare dates (e.g. Feb 29) still resolve.

    DST fall-back dedupe (Vixie-like): when clocks roll back, the same wall
    time occurs twice ~1h apart. For crons with a *fixed* hour (a daily
    "30 1 * * *") the repeated wall time must not fire again, so candidates
    whose wall clock equals ``after_ts``'s wall clock are skipped. Wildcard-hour
    crons (every-minute/hourly) intentionally keep firing through the repeat.
    """
    try:
        fields = parse_cron(expr)
    except ValueError:
        return None
    tz = _tz(tz_name)
    hour_fixed = fields[1] != "*"
    after_wall = datetime.fromtimestamp(after_ts, tz).replace(second=0, microsecond=0, tzinfo=None)
    base = (after_ts // 60 + 1) * 60
    # ~4 years + 2 days of minutes: covers a leap-day cron from any start point.
    for i in range((4 * 366 + 2) * 24 * 60):
        ts = base + i * 60
        wall = datetime.fromtimestamp(ts, tz)
        if not _cron_matches(fields, wall):
            continue
        if hour_fixed and wall.replace(second=0, microsecond=0, tzinfo=None) == after_wall:
            # Fall-back repeat of the wall time we just fired at — skip it.
            continue
        return ts
    return None


# --- prompt templating -----------------------------------------------------


def _resolve_path(payload: Any, path: str) -> str:
    if path in (".", "body"):
        return json.dumps(payload, ensure_ascii=False)
    cur = payload
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return ""
        else:
            return ""
        if cur is None:
            return ""
    if isinstance(cur, (dict, list)):
        return json.dumps(cur, ensure_ascii=False)
    return str(cur)


def render_prompt(prompt: str, payload: Any) -> str:
    """Interpolate ``{{path.to.field}}`` placeholders from a webhook payload."""
    if payload is None:
        return prompt
    return _PLACEHOLDER.sub(lambda m: _resolve_path(payload, m.group(1)), prompt)


# --- store -----------------------------------------------------------------


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


def _compute_next(job: dict[str, Any], base_ts: int) -> int | None:
    """Next fire time for a job's trigger, or None if it isn't scheduled."""
    if not job.get("enabled"):
        return None
    if job.get("cron"):
        return _cron_next(job["cron"], job.get("tz"), base_ts)
    interval = job.get("interval_seconds")
    if interval and int(interval) > 0:
        return base_ts + int(interval)
    return None


# --- CRUD ------------------------------------------------------------------


def _owned_by(job: dict[str, Any], owner: str | None) -> bool:
    """Owner filter — jobs written before owner scoping default to "local"."""
    return owner is None or job.get("owner", "local") == owner


def list_jobs(owner: str | None = None) -> list[dict[str, Any]]:
    with _lock:
        return [dict(j) for j in _load()["jobs"] if _owned_by(j, owner)]


def get_job(job_id: str, owner: str | None = None) -> dict[str, Any] | None:
    with _lock:
        for job in _load()["jobs"]:
            if job.get("id") == job_id and _owned_by(job, owner):
                return dict(job)
    return None


def get_job_by_token(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    with _lock:
        for job in _load()["jobs"]:
            stored = job.get("webhook_token")
            # Constant-time compare so the public webhook path doesn't leak
            # token prefixes via timing.
            if isinstance(stored, str) and secrets.compare_digest(stored, token):
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
    cron: str | None = None,
    tz: str | None = None,
    no_overlap: bool = True,
    enabled: bool = True,
    owner: str = "local",
) -> dict[str, Any]:
    now = _now()
    job = {
        "id": secrets.token_hex(8),
        "owner": owner,
        "name": name.strip() or agent_name,
        "agent_name": agent_name,
        "prompt": prompt,
        "workspace": (workspace or "").strip() or None,
        "host_id": (host_id or "").strip() or None,
        "interval_seconds": int(interval_seconds) if interval_seconds else None,
        "cron": (cron or "").strip() or None,
        "tz": (tz or "").strip() or None,
        "no_overlap": bool(no_overlap),
        "enabled": bool(enabled),
        "webhook_token": secrets.token_urlsafe(24),
        "created_at": now,
        "last_run_at": None,
        "last_session_id": None,
        "next_run_at": None,
        "history": [],
    }
    job["next_run_at"] = _compute_next(job, now)
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
    "cron",
    "tz",
    "no_overlap",
    "enabled",
}


def update_job(
    job_id: str, patch: dict[str, Any], owner: str | None = None
) -> dict[str, Any] | None:
    with _lock:
        data = _load()
        for job in data["jobs"]:
            if job.get("id") != job_id or not _owned_by(job, owner):
                continue
            for key in _UPDATABLE:
                if key in patch:
                    job[key] = patch[key]
            if "interval_seconds" in patch:
                job["interval_seconds"] = (
                    int(patch["interval_seconds"]) if patch["interval_seconds"] else None
                )
            for key in ("workspace", "host_id", "cron", "tz"):
                if key in patch:
                    job[key] = (str(patch[key]).strip() or None) if patch[key] else None
            # Recompute the next fire time from now whenever the cadence or the
            # enabled flag changes, so a re-enabled or re-scheduled job doesn't
            # fire immediately off a stale timestamp.
            job["next_run_at"] = _compute_next(job, _now())
            _save(data)
            return dict(job)
    return None


def delete_job(job_id: str, owner: str | None = None) -> bool:
    with _lock:
        data = _load()
        before = len(data["jobs"])
        data["jobs"] = [
            j for j in data["jobs"] if not (j.get("id") == job_id and _owned_by(j, owner))
        ]
        if len(data["jobs"]) == before:
            return False
        _save(data)
        return True


def record_run(
    job_id: str,
    entry: dict[str, Any],
    *,
    advance_schedule: bool = False,
    retry_seconds: int | None = None,
) -> None:
    """Append a trigger outcome; optionally advance the schedule.

    :param advance_schedule: recompute ``next_run_at`` (only scheduled triggers).
    :param retry_seconds: instead of the normal cadence, retry this soon (used
        when a scheduled fire was skipped for no host / overlap).
    """
    now = _now()
    entry = {"at": now, **entry}
    with _lock:
        data = _load()
        for job in data["jobs"]:
            if job.get("id") != job_id:
                continue
            job["last_run_at"] = now
            if entry.get("session_id"):
                job["last_session_id"] = entry["session_id"]
            if advance_schedule:
                if retry_seconds is not None:
                    job["next_run_at"] = now + retry_seconds
                else:
                    # May be None (no next occurrence); due_jobs skips None.
                    job["next_run_at"] = _compute_next(job, now)
            history = job.setdefault("history", [])
            history.insert(0, entry)
            del history[_MAX_HISTORY:]
            _save(data)
            return


def due_jobs(now: int | None = None) -> list[dict[str, Any]]:
    """Enabled, scheduled jobs (interval or cron) whose next_run_at has passed."""
    ts = now if now is not None else _now()
    out: list[dict[str, Any]] = []
    with _lock:
        for job in _load()["jobs"]:
            if not job.get("enabled") or not (job.get("interval_seconds") or job.get("cron")):
                continue
            nxt = job.get("next_run_at")
            # next_run_at=None means "no computed occurrence" — never due, or the
            # scheduler would fire it on every poll.
            if nxt is not None and nxt <= ts:
                out.append(dict(job))
    return out


# --- Firing ----------------------------------------------------------------


def _pick_host(job: dict[str, Any], online_host_ids: list[str]) -> str | None:
    """The job's host if it is online, else the first online host."""
    want = job.get("host_id")
    if want:
        return want if want in online_host_ids else None
    return online_host_ids[0] if online_host_ids else None


async def _session_active(client: Any, session_id: str) -> bool:
    """True if the session has a turn in progress (used for no-overlap)."""
    try:
        res = await client.get(f"/v1/sessions/{session_id}")
        if res.status_code >= 400:
            return False
        data = res.json()
        return data.get("active_response_id") is not None or data.get("status") == "running"
    except Exception:  # noqa: BLE001 — a status probe must never block firing
        return False


async def fire_job(
    app: Any,
    job: dict[str, Any],
    agent_store: Any,
    *,
    trigger: str,
    payload: Any = None,
) -> dict[str, Any]:
    """Start a real run for ``job`` and record the outcome.

    :param trigger: ``"schedule"`` / ``"webhook"`` / ``"manual"`` — recorded and
        used to decide whether the schedule advances.
    :param payload: webhook body (or a test payload) interpolated into the prompt
        via ``{{...}}``; ``None`` leaves the prompt literal.
    :returns: ``{"status", "session_id", "detail"}``.
    """
    import httpx

    scheduled = trigger == "schedule"

    def _record(status: str, detail: str, session_id: str | None = None, retry: int | None = None):
        record_run(
            job["id"],
            {"trigger": trigger, "status": status, "detail": detail, "session_id": session_id},
            advance_schedule=scheduled,
            retry_seconds=retry,
        )
        return {"status": status, "detail": detail, "session_id": session_id}

    agent = None
    try:
        agent = agent_store.get_by_name(job.get("agent_name"))
    except Exception:  # noqa: BLE001 — treat any lookup error as "not found"
        agent = None
    if agent is None:
        return _record("error", f"agente '{job.get('agent_name')}' não encontrado")

    workspace = job.get("workspace")
    if not workspace:
        return _record("error", "workspace não configurado para este job")

    host_registry = getattr(app.state, "host_registry", None)
    online = list(host_registry.online_host_ids()) if host_registry is not None else []
    host_id = _pick_host(job, online)
    if host_id is None:
        # Retry soon on a scheduled run rather than losing the whole interval.
        return _record("skipped", "nenhum host online para executar", retry=_RETRY_SECONDS)

    transport = httpx.ASGITransport(app=app)
    headers = {"Origin": _INTERNAL_ORIGIN, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://internal", timeout=45.0
        ) as client:
            # Skip a scheduled/webhook fire while the previous run is still
            # going. Manual "run now" stays exempt so the user can force a run.
            if (
                trigger in ("schedule", "webhook")
                and job.get("no_overlap", True)
                and job.get("last_session_id")
            ):
                if await _session_active(client, job["last_session_id"]):
                    return _record(
                        "skipped", "execução anterior ainda ativa", retry=_RETRY_SECONDS
                    )

            prompt = render_prompt(job.get("prompt", ""), payload)
            body: dict[str, Any] = {
                "agent_id": agent.id,
                "host_id": host_id,
                "workspace": workspace,
            }
            res = await client.post("/v1/sessions", json=body, headers=headers)
            if res.status_code >= 400:
                return _record(
                    "error", f"criar sessão falhou: HTTP {res.status_code} {res.text[:200]}"
                )
            session_id = res.json().get("id")
            if not session_id:
                return _record("error", "sessão criada sem id")
            event = {
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
            }
            ev = await client.post(
                f"/v1/sessions/{session_id}/events", json=event, headers=headers
            )
            if ev.status_code >= 400:
                return _record(
                    "error",
                    f"enviar mensagem falhou: HTTP {ev.status_code} {ev.text[:200]}",
                    session_id=session_id,
                )
    except Exception as exc:  # noqa: BLE001 — surface any transport error in history
        return _record("error", f"falha ao disparar: {exc}")

    return _record("started", "sessão iniciada", session_id=session_id)
