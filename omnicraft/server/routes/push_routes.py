"""Web Push subscription endpoints.

The browser fetches the VAPID public key, subscribes with the Push API, and
POSTs the resulting subscription here so the server can deliver approval
notifications when the app is closed. See :mod:`omnicraft.server.push`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from omnicraft.errors import ErrorCode, OmniCraftError
from omnicraft.server import push
from omnicraft.server.auth import AuthProvider
from omnicraft.server.routes._auth_helpers import require_user

# Subscriptions on a no-auth local server key under one stable id (require_user
# returns None there), matching how the send path resolves an unauthenticated
# owner.
_LOCAL_USER = "local"


def _user_key(request: Request, auth_provider: AuthProvider | None) -> str:
    return require_user(request, auth_provider) or _LOCAL_USER


def create_push_router(*, auth_provider: AuthProvider | None = None) -> APIRouter:
    """Build the router for ``/v1/push/*``."""
    router = APIRouter()

    @router.get("/push/vapid-public-key")
    async def vapid_public_key(request: Request) -> dict[str, str]:
        """The VAPID public key the browser subscribes with."""
        require_user(request, auth_provider)
        return {"key": push.application_server_key()}

    @router.post("/push/subscriptions", status_code=201)
    async def subscribe(request: Request) -> dict[str, bool]:
        """Store this browser's push subscription for the current user."""
        user_id = _user_key(request, auth_provider)
        try:
            body = await request.json()
        except Exception as exc:
            raise OmniCraftError(
                "invalid subscription body", code=ErrorCode.INVALID_INPUT
            ) from exc
        if not isinstance(body, dict) or not isinstance(body.get("endpoint"), str):
            raise OmniCraftError(
                "subscription must include an 'endpoint'", code=ErrorCode.INVALID_INPUT
            )
        push.add_subscription(user_id, body)
        return {"subscribed": True}

    @router.delete("/push/subscriptions")
    async def unsubscribe(request: Request) -> dict[str, bool]:
        """Remove a subscription (by endpoint) for the current user."""
        user_id = _user_key(request, auth_provider)
        endpoint: Any = None
        try:
            body = await request.json()
            if isinstance(body, dict):
                endpoint = body.get("endpoint")
        except Exception:  # noqa: BLE001 - malformed body falls back to query params
            endpoint = request.query_params.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            raise OmniCraftError("endpoint is required", code=ErrorCode.INVALID_INPUT)
        push.remove_subscription(user_id, endpoint)
        return {"unsubscribed": True}

    return router
