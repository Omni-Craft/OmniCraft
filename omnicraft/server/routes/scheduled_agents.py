"""Scheduled / webhook-triggered agent endpoints.

CRUD for jobs (authed) plus two fire paths: an authed "run now" and a PUBLIC
``POST /v1/webhooks/{token}`` that authenticates the opaque per-job token in the
path (never require_user, so third-party callers can reach it). See
:mod:`omnicraft.server.scheduled_agents` for the store and the firing logic; the
lifespan scheduler loop drives the interval trigger.
"""

from __future__ import annotations

from typing import Any

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
        if agent_store.get_by_name(agent_name) is None:
            raise OmniCraftError(f"agente '{agent_name}' não encontrado", code=ErrorCode.NOT_FOUND)
        return scheduled_agents.create_job(
            name=body.get("name") or agent_name,
            agent_name=agent_name,
            prompt=prompt,
            workspace=body.get("workspace"),
            host_id=body.get("host_id"),
            interval_seconds=body.get("interval_seconds"),
            enabled=bool(body.get("enabled", True)),
        )

    @router.patch("/scheduled-agents/{job_id}")
    async def update_job(request: Request, job_id: str) -> dict[str, Any]:
        require_user(request, auth_provider)
        body = await _json(request)
        if body.get("agent_name"):
            if agent_store.get_by_name(body["agent_name"]) is None:
                raise OmniCraftError(
                    f"agente '{body['agent_name']}' não encontrado", code=ErrorCode.NOT_FOUND
                )
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
        return await scheduled_agents.fire_job(request.app, job, agent_store, trigger="manual")

    @router.post("/webhooks/{token}")
    async def fire_webhook(request: Request, token: str) -> dict[str, Any]:
        # PUBLIC — no require_user. The opaque token IS the credential.
        job = scheduled_agents.get_job_by_token(token)
        if job is None:
            raise OmniCraftError("webhook desconhecido", code=ErrorCode.NOT_FOUND)
        return await scheduled_agents.fire_job(request.app, job, agent_store, trigger="webhook")

    return router


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise OmniCraftError("corpo inválido", code=ErrorCode.INVALID_INPUT) from exc
    if not isinstance(body, dict):
        raise OmniCraftError("corpo inválido", code=ErrorCode.INVALID_INPUT)
    return body
