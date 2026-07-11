"""Policy dry-run: replay a past session's tool calls through a policy.

Lets the visual editor answer "what would this policy have done?" without
touching a live session. Builds the policy's callable from ``{handler,
factory_params}`` (the same registry-validated shape the CRUD API stores),
feeds it each recorded ``function_call`` as a ``tool_call`` event, and reports
the ALLOW / ASK / DENY decision per call — carrying ``session_state`` forward
so counter policies (e.g. max-tool-calls) behave exactly as they would live.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

from fastapi import APIRouter, Request

from omnicraft.errors import ErrorCode, OmniCraftError
from omnicraft.policies.function import _has_no_required_params, _resolve_dotted_path
from omnicraft.policies.registry import is_registered_handler
from omnicraft.server.auth import AuthProvider
from omnicraft.server.routes._auth_helpers import require_user
from omnicraft.stores.conversation_store import ConversationStore

_MAX_ITEMS = 2000


def _resolve_callable(handler: str, factory_params: dict[str, Any] | None) -> Any:
    """Build the policy's evaluator callable from its handler + params."""
    target = _resolve_dotted_path(handler)
    if factory_params is not None:
        return target(**factory_params)
    if _has_no_required_params(target):
        return target()  # factory with all-default params
    return target  # direct ``def handler(event)`` callable


def _apply_state_updates(state: dict[str, Any], updates: Any) -> None:
    """Apply a PolicyResponse's state_updates to the running session_state."""
    if not isinstance(updates, list):
        return
    for update in updates:
        if not isinstance(update, dict):
            continue
        key = update.get("key")
        action = update.get("action")
        value = update.get("value")
        if not isinstance(key, str):
            continue
        if action == "set":
            state[key] = value
        elif action == "increment":
            try:
                state[key] = int(state.get(key, 0)) + int(value or 0)
            except (TypeError, ValueError):
                pass
        elif action == "delete":
            state.pop(key, None)
        elif action == "append":
            state.setdefault(key, []).append(value)


async def simulate_policy(
    handler: str,
    factory_params: dict[str, Any] | None,
    tool_calls: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    """Replay tool calls through a policy, returning the per-call verdicts.

    :param handler: Dotted path of a registered policy handler.
    :param factory_params: Factory kwargs, or ``None`` for a direct callable.
    :param tool_calls: ``(tool_name, arguments)`` pairs in call order.
    :returns: ``results`` (one verdict per call) + a ``summary`` count.
    """
    evaluator = _resolve_callable(handler, factory_params)
    state: dict[str, Any] = {}
    results: list[dict[str, Any]] = []
    summary = {"ALLOW": 0, "ASK": 0, "DENY": 0}
    for name, arguments in tool_calls:
        event = {
            "type": "tool_call",
            "target": name,
            "data": {"name": name, "arguments": arguments},
            "context": {},
            "session_state": state,
        }
        raw = evaluator(event)
        if inspect.isawaitable(raw):
            raw = await raw
        response = raw if isinstance(raw, dict) else {}
        result = str(response.get("result", "ALLOW")).upper()
        if result not in summary:
            result = "ALLOW"
        summary[result] += 1
        results.append({"tool": name, "result": result, "reason": response.get("reason")})
        _apply_state_updates(state, response.get("state_updates"))
    return {"results": results, "summary": summary, "tool_call_count": len(tool_calls)}


def create_policy_simulate_router(
    conversation_store: ConversationStore,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the router for ``POST /v1/policies/simulate``."""
    router = APIRouter()

    @router.post("/policies/simulate")
    async def simulate(request: Request) -> dict[str, Any]:
        """Dry-run a policy against a past session's recorded tool calls."""
        require_user(request, auth_provider)
        try:
            body = await request.json()
        except Exception as exc:
            raise OmniCraftError("invalid request body", code=ErrorCode.INVALID_INPUT) from exc
        handler = body.get("handler") if isinstance(body, dict) else None
        session_id = body.get("session_id") if isinstance(body, dict) else None
        factory_params = body.get("factory_params") if isinstance(body, dict) else None
        if not isinstance(handler, str) or not is_registered_handler(handler):
            raise OmniCraftError(
                "handler must be a registered policy handler", code=ErrorCode.INVALID_INPUT
            )
        if not isinstance(session_id, str) or not session_id:
            raise OmniCraftError("session_id is required", code=ErrorCode.INVALID_INPUT)
        if factory_params is not None and not isinstance(factory_params, dict):
            raise OmniCraftError("factory_params must be an object", code=ErrorCode.INVALID_INPUT)

        page = conversation_store.list_items(session_id, limit=_MAX_ITEMS, order="asc")
        tool_calls: list[tuple[str, dict[str, Any]]] = []
        for item in page.data:
            if getattr(item, "type", None) != "function_call":
                continue
            # The name/arguments live on the item's typed ``data`` (FunctionCallData).
            data = getattr(item, "data", None)
            name = getattr(data, "name", None)
            if not isinstance(name, str) or not name:
                continue
            raw_args = getattr(data, "arguments", None)
            arguments: dict[str, Any] = {}
            if isinstance(raw_args, str) and raw_args.strip():
                try:
                    parsed = json.loads(raw_args)
                    if isinstance(parsed, dict):
                        arguments = parsed
                except json.JSONDecodeError:
                    pass
            elif isinstance(raw_args, dict):
                arguments = raw_args
            tool_calls.append((name, arguments))

        try:
            return await simulate_policy(handler, factory_params, tool_calls)
        except Exception as exc:  # a bad factory param, etc.
            raise OmniCraftError(
                f"policy could not be simulated: {exc}", code=ErrorCode.INVALID_INPUT
            ) from exc

    return router
