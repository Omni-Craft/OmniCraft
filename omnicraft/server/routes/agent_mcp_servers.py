"""MCP servers on TEMPLATE agents — the surface the session route can't reach.

``/v1/sessions/{id}/agent/mcp-servers`` edits only session-scoped agents, so
gallery-installed templates (chat, capataz, the fábricas) had no MCP management
at all. This router mirrors the same bundle-mutation CRUD addressed by agent id,
plus a connection test that actually dials the server and reports its tools.

Bundle edits reuse the session route's module helpers (find/write/replace/
delete + deterministic tar). ``agent_cache.replace`` runs with
``expand_env=False`` — same rationale as the gallery: registration doesn't need
``${VAR}`` expansion, and expanding would break templates whose env is only set
at run time.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Request, Response, status

from omnicraft.entities import Agent
from omnicraft.errors import ErrorCode, OmniCraftError
from omnicraft.runtime.agent_cache import AgentCache
from omnicraft.server.auth import AuthProvider, local_single_user_enabled
from omnicraft.server.bundles import bundle_location, validate_agent_bundle
from omnicraft.server.routes._auth_helpers import require_user
from omnicraft.server.routes.session_mcp_servers import (
    _delete_mcp_server,
    _find_mcp_location,
    _replace_mcp_server,
    _summary_from_config,
    _tar_gz_dir,
    _write_new_mcp_server,
)
from omnicraft.server.schemas import UpsertMCPServerRequest
from omnicraft.spec import extract_safe
from omnicraft.spec.types import MCPServerConfig
from omnicraft.stores.agent_store import AgentStore
from omnicraft.stores.artifact_store import ArtifactStore

_TEST_TIMEOUT_S = 20


def create_agent_mcp_servers_router(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    agent_cache: AgentCache,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the router for ``/v1/agents/{agent_id}/mcp-servers*``."""
    router = APIRouter()

    def _template_agent(agent_id: str) -> Agent:
        agent = agent_store.get(agent_id)
        if agent is None or agent.session_id is not None:
            raise OmniCraftError("agente não encontrado", code=ErrorCode.NOT_FOUND)
        return agent

    def _load_spec(agent: Agent) -> Any:
        # expand_env=False: template bundles may reference ${VARS} the operator
        # sets only at run time; listing/editing must not require them.
        return agent_cache.load(agent.id, agent.bundle_location, expand_env=False)

    def _mutate(
        agent: Agent,
        body: UpsertMCPServerRequest | None,
        *,
        mode: Literal["create", "update", "delete"],
        target_name: str | None,
    ) -> Any:
        bundle_bytes = artifact_store.get(agent.bundle_location)
        if bundle_bytes is None:
            raise OmniCraftError("Agent bundle not found", code=ErrorCode.INTERNAL_ERROR)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "agent"
            extract_safe(bundle_bytes, root)
            current_spec = validate_agent_bundle(
                bundle_bytes,
                enforce_handler_allowlist=not local_single_user_enabled(),
            )
            current_names = {server.name for server in current_spec.mcp_servers}
            if mode == "create":
                assert body is not None
                if body.name in current_names:
                    raise OmniCraftError(
                        f"MCP server {body.name!r} already exists", code=ErrorCode.CONFLICT
                    )
                _write_new_mcp_server(root, body)
            elif mode == "update":
                assert body is not None
                assert target_name is not None
                location = _find_mcp_location(root, target_name)
                if location is None:
                    raise OmniCraftError("MCP server not found", code=ErrorCode.NOT_FOUND)
                if body.name != target_name and body.name in current_names:
                    raise OmniCraftError(
                        f"MCP server {body.name!r} already exists", code=ErrorCode.CONFLICT
                    )
                _replace_mcp_server(location, target_name, body)
            else:
                assert target_name is not None
                location = _find_mcp_location(root, target_name)
                if location is None:
                    raise OmniCraftError("MCP server not found", code=ErrorCode.NOT_FOUND)
                _delete_mcp_server(location, target_name)

            new_bundle = _tar_gz_dir(root)
            new_spec = validate_agent_bundle(
                new_bundle,
                enforce_handler_allowlist=not local_single_user_enabled(),
            )
            if new_spec.name != agent.name:
                raise OmniCraftError(
                    "MCP edit changed the agent name; refusing to save.",
                    code=ErrorCode.INVALID_INPUT,
                )

        new_location = bundle_location(agent.id, new_bundle)
        if new_location != agent.bundle_location:
            artifact_store.put(new_location, new_bundle)
            updated = agent_store.update(agent.id, new_location)
            if updated is None:
                raise OmniCraftError("Agent not found", code=ErrorCode.NOT_FOUND)
            agent_cache.replace(agent.id, new_location, new_bundle, expand_env=False)
        return new_spec

    @router.get("/agents/{agent_id}/mcp-servers")
    async def list_servers(request: Request, agent_id: str) -> dict[str, Any]:
        require_user(request, auth_provider)
        agent = _template_agent(agent_id)
        loaded = await asyncio.to_thread(_load_spec, agent)
        return {
            "object": "list",
            "data": [_summary_from_config(s).model_dump() for s in loaded.spec.mcp_servers],
        }

    @router.post("/agents/{agent_id}/mcp-servers", status_code=201)
    async def create_server(
        request: Request, agent_id: str, body: UpsertMCPServerRequest
    ) -> dict[str, Any]:
        require_user(request, auth_provider)
        agent = _template_agent(agent_id)
        spec = await asyncio.to_thread(_mutate, agent, body, mode="create", target_name=None)
        server = next(s for s in spec.mcp_servers if s.name == body.name)
        return _summary_from_config(server).model_dump()

    @router.put("/agents/{agent_id}/mcp-servers/{server_name}")
    async def update_server(
        request: Request, agent_id: str, server_name: str, body: UpsertMCPServerRequest
    ) -> dict[str, Any]:
        require_user(request, auth_provider)
        agent = _template_agent(agent_id)
        spec = await asyncio.to_thread(
            _mutate, agent, body, mode="update", target_name=server_name
        )
        server = next(s for s in spec.mcp_servers if s.name == body.name)
        return _summary_from_config(server).model_dump()

    @router.delete("/agents/{agent_id}/mcp-servers/{server_name}", status_code=204)
    async def delete_server(request: Request, agent_id: str, server_name: str) -> Response:
        require_user(request, auth_provider)
        agent = _template_agent(agent_id)
        await asyncio.to_thread(_mutate, agent, None, mode="delete", target_name=server_name)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/agents/{agent_id}/mcp-servers/{server_name}/test")
    async def test_server(request: Request, agent_id: str, server_name: str) -> dict[str, Any]:
        """Dial the server and report its tools — the 'does it work?' button."""
        require_user(request, auth_provider)
        agent = _template_agent(agent_id)
        loaded = await asyncio.to_thread(_load_spec, agent)
        config: MCPServerConfig | None = next(
            (s for s in loaded.spec.mcp_servers if s.name == server_name), None
        )
        if config is None:
            raise OmniCraftError("MCP server not found", code=ErrorCode.NOT_FOUND)

        from omnicraft.tools.mcp import McpServerConnection

        conn = McpServerConnection(config=config)
        try:
            tools = await asyncio.wait_for(conn.connect(), timeout=_TEST_TIMEOUT_S)
            return {
                "ok": True,
                "tools": [t.name for t in tools][:50],
                "tool_count": len(tools),
            }
        except TimeoutError:
            return {"ok": False, "error": f"timeout após {_TEST_TIMEOUT_S}s"}
        except Exception as exc:  # noqa: BLE001 — the point is reporting the failure
            return {"ok": False, "error": str(exc)[:300]}
        finally:
            with contextlib.suppress(Exception):
                await conn.close()

    return router
