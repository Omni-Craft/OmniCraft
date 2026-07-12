"""Scheduled / webhook-triggered agent endpoints.

CRUD for jobs (authed) plus two fire paths: an authed "run now" and a PUBLIC
``POST /v1/webhooks/{token}`` that authenticates the opaque per-job token in the
path (never require_user, so third-party callers can reach it). See
:mod:`omnicraft.server.scheduled_agents` for the store and the firing logic; the
lifespan scheduler loop drives the interval trigger.
"""

from __future__ import annotations

import time
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request

from omnicraft.errors import ErrorCode, OmniCraftError
from omnicraft.server import scheduled_agents
from omnicraft.server.auth import AuthProvider
from omnicraft.server.routes._auth_helpers import require_user


def _installed_agent_names(agent_store: Any) -> list[str]:
    # agent_store.list() returns a PagedList (its rows are under .data) and
    # already filters to top-level (session_id IS NULL) agents.
    try:
        page = agent_store.list(limit=1000)
        rows = getattr(page, "data", page)
        names = [
            a.name
            for a in rows
            if getattr(a, "name", None) and getattr(a, "session_id", None) is None
        ]
    except Exception:  # noqa: BLE001 — best-effort picker list, never block CRUD
        return []
    return sorted(set(names))


def create_scheduled_agents_router(
    agent_store: Any,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the router for ``/v1/scheduled-agents/*`` and ``/v1/webhooks/{token}``."""
    router = APIRouter()

    @router.get("/scheduled-agents")
    async def list_jobs(request: Request) -> dict[str, Any]:
        require_user(request, auth_provider)
        return {
            "data": scheduled_agents.list_jobs(),
            "agents": _installed_agent_names(agent_store),
        }

    @router.post("/scheduled-agents", status_code=201)
    async def create_job(request: Request) -> dict[str, Any]:
        require_user(request, auth_provider)
        body = await _json(request)
        agent_name = body.get("agent_name")
        prompt = body.get("prompt")
        if not isinstance(agent_name, str) or not agent_name.strip():
            raise OmniCraftError("agent_name é obrigatório", code=ErrorCode.INVALID_INPUT)
        if not isinstance(prompt, str) or not prompt.strip():
            raise OmniCraftError("prompt é obrigatório", code=ErrorCode.INVALID_INPUT)
        workspace = body.get("workspace")
        if not isinstance(workspace, str) or not workspace.strip():
            raise OmniCraftError(
                "workspace é obrigatório (caminho absoluto onde o agente roda)",
                code=ErrorCode.INVALID_INPUT,
            )
        _validate_cron(body.get("cron"))
        _validate_schedule(body)
        _validate_cron_has_next(body.get("cron"), body.get("tz"), bool(body.get("enabled", True)))
        if agent_store.get_by_name(agent_name) is None:
            raise OmniCraftError(f"agente '{agent_name}' não encontrado", code=ErrorCode.NOT_FOUND)
        return scheduled_agents.create_job(
            name=body.get("name") or agent_name,
            agent_name=agent_name,
            prompt=prompt,
            workspace=body.get("workspace"),
            host_id=body.get("host_id"),
            interval_seconds=body.get("interval_seconds"),
            cron=body.get("cron"),
            tz=body.get("tz"),
            no_overlap=bool(body.get("no_overlap", True)),
            enabled=bool(body.get("enabled", True)),
        )

    @router.patch("/scheduled-agents/{job_id}")
    async def update_job(request: Request, job_id: str) -> dict[str, Any]:
        require_user(request, auth_provider)
        body = await _json(request)
        if "agent_name" in body:
            agent_name = body["agent_name"]
            if not isinstance(agent_name, str) or not agent_name.strip():
                raise OmniCraftError("agent_name inválido", code=ErrorCode.INVALID_INPUT)
            if agent_store.get_by_name(agent_name) is None:
                raise OmniCraftError(
                    f"agente '{agent_name}' não encontrado", code=ErrorCode.NOT_FOUND
                )
        for flag in ("enabled", "no_overlap"):
            if flag in body:
                if not isinstance(body[flag], bool):
                    raise OmniCraftError(f"{flag} deve ser booleano", code=ErrorCode.INVALID_INPUT)
                body[flag] = bool(body[flag])
        if body.get("cron"):
            _validate_cron(body.get("cron"))
        _validate_schedule(body)
        current = scheduled_agents.get_job(job_id)
        if current is None:
            raise OmniCraftError("job não encontrado", code=ErrorCode.NOT_FOUND)
        # Validate the prospective (merged) schedule, not just the patch.
        merged = {**current, **body}
        _validate_cron_has_next(merged.get("cron"), merged.get("tz"), merged.get("enabled"))
        updated = scheduled_agents.update_job(job_id, body)
        if updated is None:
            raise OmniCraftError("job não encontrado", code=ErrorCode.NOT_FOUND)
        return updated

    @router.delete("/scheduled-agents/{job_id}")
    async def delete_job(request: Request, job_id: str) -> dict[str, bool]:
        require_user(request, auth_provider)
        if not scheduled_agents.delete_job(job_id):
            raise OmniCraftError("job não encontrado", code=ErrorCode.NOT_FOUND)
        return {"deleted": True}

    @router.post("/scheduled-agents/{job_id}/run")
    async def run_now(request: Request, job_id: str) -> dict[str, Any]:
        require_user(request, auth_provider)
        job = scheduled_agents.get_job(job_id)
        if job is None:
            raise OmniCraftError("job não encontrado", code=ErrorCode.NOT_FOUND)
        # An optional {"payload": {...}} lets the UI test webhook templating.
        payload = None
        try:
            b = await request.json()
            if isinstance(b, dict) and isinstance(b.get("payload"), (dict, list)):
                payload = b["payload"]
        except Exception:  # noqa: BLE001 — body is optional here
            payload = None
        return await scheduled_agents.fire_job(
            request.app, job, agent_store, trigger="manual", payload=payload
        )

    @router.post("/webhooks/{token}")
    async def fire_webhook(request: Request, token: str) -> dict[str, Any]:
        # PUBLIC — no require_user. The opaque token IS the credential.
        job = scheduled_agents.get_job_by_token(token)
        if job is None:
            raise OmniCraftError("webhook desconhecido", code=ErrorCode.NOT_FOUND)
        # The POST body is interpolated into the prompt via {{...}} placeholders.
        payload: Any = {}
        try:
            body = await request.json()
            if isinstance(body, (dict, list)):
                payload = body
        except Exception:  # noqa: BLE001 — a bodyless webhook is fine
            payload = {}
        result = await scheduled_agents.fire_job(
            request.app, job, agent_store, trigger="webhook", payload=payload
        )
        # Opaque response: this endpoint is unauthenticated, so never leak the
        # session id or failure detail. The full record lives in job history.
        if result.get("status") in ("started", "skipped"):
            return {"status": "accepted"}
        return {"status": "error"}

    return router


def _validate_schedule(body: dict[str, Any]) -> None:
    """Reject malformed schedule fields with a 400 instead of a 500/misfire."""
    if "interval_seconds" in body and body["interval_seconds"] is not None:
        raw = body["interval_seconds"]
        if isinstance(raw, bool):
            raise OmniCraftError("interval_seconds inválido", code=ErrorCode.INVALID_INPUT)
        if isinstance(raw, float):
            if not raw.is_integer():
                raise OmniCraftError("interval_seconds inválido", code=ErrorCode.INVALID_INPUT)
            raw = int(raw)
        elif isinstance(raw, str):
            try:
                raw = int(raw)
            except ValueError:
                raise OmniCraftError(
                    "interval_seconds inválido", code=ErrorCode.INVALID_INPUT
                ) from None
        elif not isinstance(raw, int):
            raise OmniCraftError("interval_seconds inválido", code=ErrorCode.INVALID_INPUT)
        if not (60 <= raw <= 31_536_000):
            raise OmniCraftError(
                "interval_seconds deve estar entre 60 e 31536000",
                code=ErrorCode.INVALID_INPUT,
            )
    tz = body.get("tz")
    if tz is not None:
        if not isinstance(tz, str):
            raise OmniCraftError("fuso horário inválido", code=ErrorCode.INVALID_INPUT)
        if tz.strip():
            try:
                ZoneInfo(tz.strip())
            except Exception:  # noqa: BLE001 — any lookup failure is "invalid tz"
                raise OmniCraftError(
                    "fuso horário inválido", code=ErrorCode.INVALID_INPUT
                ) from None
    name = body.get("name")
    if isinstance(name, str) and len(name) > 200:
        raise OmniCraftError("name muito longo (máx. 200)", code=ErrorCode.INVALID_INPUT)
    prompt = body.get("prompt")
    if isinstance(prompt, str) and len(prompt) > 32_000:
        raise OmniCraftError("prompt muito longo (máx. 32000)", code=ErrorCode.INVALID_INPUT)


def _validate_cron_has_next(cron: Any, tz: Any, enabled: Any) -> None:
    """Reject an enabled cron with no computable next occurrence (e.g. 0 0 31 2 *)."""
    cron = cron.strip() if isinstance(cron, str) else None
    if not enabled or not cron:
        return
    tz_name = tz.strip() if isinstance(tz, str) and tz.strip() else None
    if scheduled_agents._cron_next(cron, tz_name, int(time.time())) is None:
        raise OmniCraftError("cron sem próxima ocorrência", code=ErrorCode.INVALID_INPUT)


def _validate_cron(cron: Any) -> None:
    if cron is None or (isinstance(cron, str) and not cron.strip()):
        return
    if not isinstance(cron, str):
        raise OmniCraftError("cron inválido", code=ErrorCode.INVALID_INPUT)
    try:
        scheduled_agents.parse_cron(cron.strip())
    except ValueError as exc:
        raise OmniCraftError(f"cron inválido: {exc}", code=ErrorCode.INVALID_INPUT) from exc


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise OmniCraftError("corpo inválido", code=ErrorCode.INVALID_INPUT) from exc
    if not isinstance(body, dict):
        raise OmniCraftError("corpo inválido", code=ErrorCode.INVALID_INPUT)
    return body
