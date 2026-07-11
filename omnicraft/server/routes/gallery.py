"""Agent gallery endpoints — browse and install bundled example agents."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from omnicraft.errors import ErrorCode, OmniCraftError
from omnicraft.server import gallery
from omnicraft.server.auth import AuthProvider
from omnicraft.server.routes._auth_helpers import require_user


def create_gallery_router(
    agent_store: Any,
    artifact_store: Any,
    agent_cache: Any,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the router for ``/v1/gallery/*``."""
    router = APIRouter()

    @router.get("/gallery/agents")
    async def list_agents(request: Request) -> dict[str, Any]:
        """List the installable example agents with light metadata."""
        require_user(request, auth_provider)
        return {"data": gallery.list_gallery_agents(agent_store)}

    @router.post("/gallery/agents/{example_id}/install")
    async def install_agent(request: Request, example_id: str) -> dict[str, Any]:
        """Register an example agent so it appears in the New Session picker."""
        require_user(request, auth_provider)
        result = gallery.install_gallery_agent(
            example_id, agent_store, artifact_store, agent_cache
        )
        if result is None:
            raise OmniCraftError("example agent not found", code=ErrorCode.NOT_FOUND)
        return result

    return router
