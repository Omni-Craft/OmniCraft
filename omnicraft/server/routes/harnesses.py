"""Read-only route for installed harness catalog metadata."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from omnicraft.harness_plugins import harness_catalog
from omnicraft.server.auth import AuthProvider
from omnicraft.server.routes._auth_helpers import require_user


def create_harnesses_router(*, auth_provider: AuthProvider | None = None) -> APIRouter:
    """Build the router for ``GET /v1/harnesses``."""
    router = APIRouter()

    @router.get("/harnesses")
    async def list_harnesses(request: Request) -> dict[str, list[dict[str, Any]]]:
        require_user(request, auth_provider)
        return {"data": harness_catalog()}

    return router
