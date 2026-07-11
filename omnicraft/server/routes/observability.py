"""Live cost / observability aggregation.

Rolls a user's LLM spend into one payload for the cost panel: today's and
all-time totals, a daily-cost trend (from the ``user_daily_cost`` rollup), a
per-model breakdown and the priciest sessions (both summed from each
conversation's persisted ``session_usage``). Read-only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query, Request

from omnicraft.server.auth import AuthProvider
from omnicraft.server.routes._auth_helpers import require_user
from omnicraft.stores.conversation_store import ConversationStore

_LOCAL_USER = "local"
_MAX_SESSIONS = 1000
_TOP_SESSIONS = 12


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _i(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def aggregate_sessions(sessions: list[tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
    """Sum per-session usage into totals, a per-model breakdown, and top sessions.

    :param sessions: ``(id, title, session_usage)`` triples.
    :returns: ``total_usd``, ``total_tokens``, ``session_count``, ``by_model``
        (usd-desc), ``top_sessions`` (usd-desc, capped).
    """
    by_model: dict[str, dict[str, Any]] = {}
    priced: list[dict[str, Any]] = []
    total_usd = 0.0
    total_tokens = 0
    for session_id, title, usage in sessions:
        cost = _f(usage.get("total_cost_usd"))
        tokens = _i(usage.get("total_tokens"))
        total_usd += cost
        total_tokens += tokens
        per_model = usage.get("by_model")
        if isinstance(per_model, dict):
            for model, model_usage in per_model.items():
                if not isinstance(model_usage, dict):
                    continue
                entry = by_model.setdefault(
                    model,
                    {
                        "model": model,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "usd": 0.0,
                    },
                )
                entry["input_tokens"] += _i(model_usage.get("input_tokens"))
                entry["output_tokens"] += _i(model_usage.get("output_tokens"))
                entry["total_tokens"] += _i(model_usage.get("total_tokens"))
                entry["usd"] += _f(model_usage.get("total_cost_usd"))
        if cost > 0 or tokens > 0:
            priced.append({"id": session_id, "title": title, "usd": cost, "tokens": tokens})
    return {
        "total_usd": total_usd,
        "total_tokens": total_tokens,
        "session_count": len(priced),
        "by_model": sorted(by_model.values(), key=lambda e: e["usd"], reverse=True),
        "top_sessions": sorted(priced, key=lambda s: (s["usd"], s["tokens"]), reverse=True)[
            :_TOP_SESSIONS
        ],
    }


def create_observability_router(
    conversation_store: ConversationStore,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the router for ``GET /v1/observability/costs``."""
    router = APIRouter()

    @router.get("/observability/costs")
    async def costs(
        request: Request,
        days: int = Query(default=30, ge=1, le=90),
    ) -> dict[str, Any]:
        """Aggregate the current user's LLM spend for the cost panel."""
        user_id = require_user(request, auth_provider) or _LOCAL_USER

        # Daily-cost trend from the O(1) rollup, oldest → newest.
        today = datetime.now(timezone.utc).date()
        daily: list[dict[str, Any]] = []
        for offset in range(days - 1, -1, -1):
            day = (today - timedelta(days=offset)).isoformat()
            daily.append({"day": day, "usd": conversation_store.get_daily_cost(user_id, day)})
        today_usd = daily[-1]["usd"] if daily else 0.0

        # Per-model + per-session totals summed from each session's usage.
        page = conversation_store.list_conversations(
            limit=_MAX_SESSIONS,
            kind=None,  # include sub-agent sessions so their spend counts once
            has_agent_id=True,
            accessible_by=user_id,
            owned_by=user_id,
        )
        aggregated = aggregate_sessions(
            [(conv.id, conv.title or conv.id, conv.session_usage or {}) for conv in page.data]
        )
        return {"today_usd": today_usd, "daily": daily, **aggregated}

    return router
