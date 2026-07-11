"""Agent evaluation endpoints — suites, runs, and regression history.

See :mod:`omnicraft.server.evals`. The client runs each task (spawns a session,
delivers the prompt, collects the final output) and POSTs the outputs here; the
server grades them against the suite's checks and stores the run so runs can be
compared over time.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from omnicraft.errors import ErrorCode, OmniCraftError
from omnicraft.server import evals
from omnicraft.server.auth import AuthProvider
from omnicraft.server.routes._auth_helpers import require_user


def create_evals_router(*, auth_provider: AuthProvider | None = None) -> APIRouter:
    """Build the router for ``/v1/evals/*``."""
    router = APIRouter()

    @router.get("/evals/suites")
    async def list_suites(request: Request) -> dict[str, Any]:
        require_user(request, auth_provider)
        return {"data": evals.list_suites()}

    @router.post("/evals/suites", status_code=201)
    async def create_suite(request: Request) -> dict[str, Any]:
        require_user(request, auth_provider)
        try:
            body = await request.json()
        except Exception as exc:
            raise OmniCraftError("invalid body", code=ErrorCode.INVALID_INPUT) from exc
        if not isinstance(body, dict):
            raise OmniCraftError("invalid body", code=ErrorCode.INVALID_INPUT)
        name = body.get("name")
        tasks = body.get("tasks")
        if not isinstance(name, str) or not name.strip():
            raise OmniCraftError("name is required", code=ErrorCode.INVALID_INPUT)
        if not isinstance(tasks, list) or not tasks:
            raise OmniCraftError("at least one task is required", code=ErrorCode.INVALID_INPUT)
        return evals.create_suite(name, tasks)

    @router.delete("/evals/suites/{suite_id}")
    async def delete_suite(request: Request, suite_id: str) -> dict[str, bool]:
        require_user(request, auth_provider)
        evals.delete_suite(suite_id)
        return {"deleted": True}

    @router.get("/evals/suites/{suite_id}/runs")
    async def list_runs(request: Request, suite_id: str) -> dict[str, Any]:
        require_user(request, auth_provider)
        if evals.get_suite(suite_id) is None:
            raise OmniCraftError("suite not found", code=ErrorCode.NOT_FOUND)
        return {"data": evals.list_runs(suite_id)}

    @router.post("/evals/suites/{suite_id}/runs", status_code=201)
    async def record_run(request: Request, suite_id: str) -> dict[str, Any]:
        require_user(request, auth_provider)
        try:
            body = await request.json()
        except Exception as exc:
            raise OmniCraftError("invalid body", code=ErrorCode.INVALID_INPUT) from exc
        if not isinstance(body, dict):
            raise OmniCraftError("invalid body", code=ErrorCode.INVALID_INPUT)
        label = body.get("label")
        task_outputs = body.get("task_outputs")
        if not isinstance(task_outputs, list):
            raise OmniCraftError("task_outputs must be a list", code=ErrorCode.INVALID_INPUT)
        run = evals.record_run(
            suite_id, label if isinstance(label, str) else "execução", task_outputs
        )
        if run is None:
            raise OmniCraftError("suite not found", code=ErrorCode.NOT_FOUND)
        return run

    return router
