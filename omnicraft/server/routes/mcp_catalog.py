"""Curated MCP connector catalog — the data behind the connector directory.

Adding an MCP server used to mean knowing the package name, the transport and
the exact args by heart. This serves a vetted list instead, so the UI can offer
one-click installs: each entry carries everything ``POST /v1/agents/{id}/
mcp-servers`` needs, plus the names of the environment variables the connector
requires so the UI can ask for them.

The catalog is static data (``data/mcp_catalog.json``) rather than a hardcoded
frontend array, so it can grow without a web rebuild. It is deliberately NOT a
live registry crawl: every entry was checked against npm/PyPI when added, and
the existing "test connection" endpoint remains the ground truth for whether a
given machine can actually run one.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from omnicraft.server.auth import AuthProvider
from omnicraft.server.routes._auth_helpers import require_user

_CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "mcp_catalog.json"


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, Any]:
    """Read and cache the bundled catalog.

    :returns: The parsed catalog, or an empty one when the file is missing or
        unreadable — a broken catalog must degrade to "no suggestions", never
        take the connectors page down.
    """
    try:
        with _CATALOG_PATH.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"version": 0, "connectors": []}
    if not isinstance(data, dict) or not isinstance(data.get("connectors"), list):
        return {"version": 0, "connectors": []}
    return data


def create_mcp_catalog_router(
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the router for ``GET /v1/mcp-catalog``."""
    router = APIRouter()

    @router.get("/mcp-catalog")
    async def mcp_catalog(request: Request) -> dict[str, Any]:
        """Return the curated connector catalog."""
        require_user(request, auth_provider)
        return load_catalog()

    return router
