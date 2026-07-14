"""Embedded-browser action bridge — the server half of the browser relay.

The web UI ships a complete Electron-side relay
(``web/src/hooks/useBrowserAgentRelay.ts``): on a ``browser.action_request``
SSE event, renderers race to CLAIM the action, the winner drives the
conversation's WebContentsView (navigate / screenshot / snapshot / click /
type) and POSTs the result back. This module supplies the server half that
was missing: mint an action, publish the SSE event, park a Future, and
resolve it from the relay's claim + result calls.

The agent-facing ``browser_*`` tools (runner-local dispatch) call
``POST /v1/sessions/{id}/browser/actions`` and block until the relay
answers or the timeout fires — when no desktop renderer is open on the
conversation, nobody claims and the action times out cleanly, exactly as
the relay's contract promises (no headless fallback).
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from omnicraft.runtime import session_stream
from omnicraft.server.auth import AuthProvider
from omnicraft.server.routes._auth_helpers import require_user

# The bare verbs the relay's dispatch() switch implements. Anything else is
# rejected server-side so a typo'd tool name can't park a Future forever.
_ALLOWED_ACTIONS = frozenset({"navigate", "screenshot", "snapshot", "click", "type"})

# Default / ceiling for how long an action waits for a renderer to answer.
_DEFAULT_TIMEOUT_S = 30.0
_MAX_TIMEOUT_S = 120.0

# Pending actions are pruned when older than this — a crashed caller must
# not leak Futures forever.
_STALE_AFTER_S = 300.0


@dataclass
class _PendingAction:
    """One in-flight browser action awaiting a renderer's claim + result."""

    session_id: str
    future: asyncio.Future[dict[str, Any]]
    # The loop the future belongs to — results may arrive on a different
    # loop/thread (multi-worker test harnesses), so resolution goes through
    # call_soon_threadsafe instead of touching the future directly.
    loop: asyncio.AbstractEventLoop
    claim_token: str | None = None
    created_at: float = field(default_factory=time.monotonic)


_pending: dict[str, _PendingAction] = {}


def _prune_stale() -> None:
    cutoff = time.monotonic() - _STALE_AFTER_S
    for action_id in [aid for aid, p in _pending.items() if p.created_at < cutoff]:
        entry = _pending.pop(action_id, None)
        if entry is not None and not entry.future.done():
            entry.loop.call_soon_threadsafe(entry.future.cancel)


class BrowserActionRequest(BaseModel):
    """Body for ``POST /sessions/{id}/browser/actions`` (runner-issued)."""

    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float | None = None


class BrowserActionResultBody(BaseModel):
    """Body the relay POSTs back with its claim token."""

    result: dict[str, Any]
    claim_token: str


def create_browser_actions_router(
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the router for ``/v1/sessions/{id}/browser/*``."""
    router = APIRouter()

    @router.post("/sessions/{session_id}/browser/actions")
    async def run_action(
        request: Request, session_id: str, body: BrowserActionRequest
    ) -> dict[str, Any]:
        """Mint an action, publish the SSE request, await the relay's result."""
        require_user(request, auth_provider)
        _prune_stale()
        if body.action not in _ALLOWED_ACTIONS:
            return {
                "ok": False,
                "error": (
                    f"ação de navegador desconhecida: {body.action!r} "
                    f"(válidas: {', '.join(sorted(_ALLOWED_ACTIONS))})"
                ),
            }
        action_id = f"bact_{secrets.token_hex(12)}"
        loop = asyncio.get_running_loop()
        entry = _PendingAction(
            session_id=session_id,
            future=loop.create_future(),
            loop=loop,
        )
        _pending[action_id] = entry
        session_stream.publish(
            session_id,
            {
                "type": "browser.action_request",
                "conversation_id": session_id,
                "action_id": action_id,
                "action": body.action,
                "args": body.args,
            },
        )
        timeout = min(body.timeout_s or _DEFAULT_TIMEOUT_S, _MAX_TIMEOUT_S)
        try:
            result = await asyncio.wait_for(asyncio.shield(entry.future), timeout)
        except (TimeoutError, asyncio.CancelledError):
            return {
                "ok": False,
                "error": (
                    "nenhum navegador respondeu à ação — abra o app desktop "
                    "nesta conversa (painel Navegador) e tente de novo"
                ),
            }
        finally:
            _pending.pop(action_id, None)
        return result

    @router.post("/sessions/{session_id}/browser/action_claim/{action_id}")
    async def claim_action(request: Request, session_id: str, action_id: str) -> dict[str, Any]:
        """Atomic check-and-set: exactly one renderer wins the action."""
        require_user(request, auth_provider)
        entry = _pending.get(action_id)
        if entry is None or entry.session_id != session_id or entry.claim_token is not None:
            return {"claimed": False}
        entry.claim_token = secrets.token_hex(16)
        return {"claimed": True, "claim_token": entry.claim_token}

    @router.post("/sessions/{session_id}/browser/action_result/{action_id}")
    async def post_result(
        request: Request,
        session_id: str,
        action_id: str,
        body: BrowserActionResultBody,
    ) -> dict[str, Any]:
        """Resolve the parked Future with the winning renderer's result."""
        require_user(request, auth_provider)
        entry = _pending.get(action_id)
        if (
            entry is None
            or entry.session_id != session_id
            or entry.claim_token is None
            or body.claim_token != entry.claim_token
        ):
            # Unknown / expired / tokenless — reject so a losing renderer
            # (or a replay) can't overwrite the winner's result.
            return {"ok": False, "error": "claim inválido ou ação expirada"}

        def _resolve() -> None:
            if not entry.future.done():
                entry.future.set_result(body.result)

        entry.loop.call_soon_threadsafe(_resolve)
        return {"ok": True}

    return router
