"""Code landing stats — a personal activity dashboard for the Code tab.

Aggregates the viewer's coding sessions (excluding the no-filesystem "chat"
agent) into totals, per-model breakdown, active-day streaks, peak hour and a
daily session heatmap. Reuses :func:`aggregate_sessions` from the observability
route for the token/model rollup; adds day/hour bucketing over session
timestamps for the activity metrics.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timedelta, tzinfo
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request

from omnicraft.server.auth import AuthProvider
from omnicraft.server.routes._auth_helpers import require_user
from omnicraft.server.routes.observability import aggregate_sessions

_LOCAL_USER = "local"
_MAX_SESSIONS = 1000

# Short in-process TTL cache: the dashboard refetches on tab/window clicks and
# the aggregation scans up to _MAX_SESSIONS rows with JSON usage blobs each hit.
_CACHE_TTL_S = 30
_cache: dict[tuple[str, int, str | None], tuple[float, dict[str, Any]]] = {}


def _day(ts: float, tz: tzinfo | None = None) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")


def _streaks(days: set[str]) -> tuple[int, int]:
    """(current_streak, longest_streak) from a set of active YYYY-MM-DD days."""
    if not days:
        return 0, 0
    dates = sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in days)
    longest = run = 1
    for prev, cur in pairwise(dates):
        run = run + 1 if (cur - prev).days == 1 else 1
        longest = max(longest, run)
    # Current streak: consecutive days ending today or yesterday.
    today = datetime.now().date()
    if dates[-1] not in (today, today - timedelta(days=1)):
        return 0, longest
    current = 1
    for later, earlier in pairwise(reversed(dates)):
        if (later - earlier).days == 1:
            current += 1
        else:
            break
    return current, longest


def _pretty_model(raw: str) -> str:
    """ "claude-opus-4-8" -> "Opus 4.8"; best-effort, falls back to the raw id."""
    if not raw:
        return raw
    name = raw.rsplit("/", 1)[-1].removeprefix("claude-")
    parts = name.split("-")
    if not parts:
        return raw
    head = parts[0].capitalize()
    ver = ".".join(p for p in parts[1:] if p.isdigit())
    return f"{head} {ver}".strip() if ver else head


def create_code_stats_router(
    conversation_store: Any,
    agent_store: Any,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the router for ``GET /v1/code-stats``."""
    router = APIRouter()

    @router.get("/code-stats")
    async def code_stats(
        request: Request,
        days: int = Query(default=365, ge=1, le=730),
        tz: str | None = Query(default=None),
    ) -> dict[str, Any]:
        user_id = require_user(request, auth_provider) or _LOCAL_USER

        cache_key = (user_id, days, tz)
        cached = _cache.get(cache_key)
        if cached is not None and time.time() - cached[0] < _CACHE_TTL_S:
            return cached[1]

        # Bucket days/hours in the viewer's timezone when a valid one is given;
        # otherwise fall back to server-local.
        zone: tzinfo | None = None
        if tz:
            try:
                zone = ZoneInfo(tz)
            except Exception:  # noqa: BLE001 — invalid tz falls back to server-local
                zone = None

        try:
            chat_agent = agent_store.get_by_name("chat")
            chat_agent_id = chat_agent.id if chat_agent is not None else None
        except Exception:  # noqa: BLE001 — best-effort filter, never block the dashboard
            chat_agent_id = None

        page = conversation_store.list_conversations(
            limit=_MAX_SESSIONS,
            has_agent_id=True,
            accessible_by=user_id,
            owned_by=user_id,
            sort_by="created_at",
            order="desc",
        )
        cutoff = time.time() - days * 86400
        sessions = [
            s for s in page.data if (s.created_at or 0) >= cutoff and s.agent_id != chat_agent_id
        ]

        agg = aggregate_sessions([(s.id, s.title, s.session_usage or {}) for s in sessions])

        day_counts: Counter[str] = Counter()
        hour_counts: Counter[int] = Counter()
        for s in sessions:
            ts = s.created_at
            if not ts:
                continue
            day_counts[_day(ts, zone)] += 1
            hour_counts[datetime.fromtimestamp(ts, zone).hour] += 1

        current_streak, longest_streak = _streaks(set(day_counts))
        by_model = agg.get("by_model", [])
        favorite = max(by_model, key=lambda m: m.get("total_tokens", 0), default=None)

        payload = {
            "total_sessions": len(sessions),
            "total_tokens": agg.get("total_tokens", 0),
            "total_usd": agg.get("total_usd", 0.0),
            "active_days": len(day_counts),
            "current_streak": current_streak,
            "longest_streak": longest_streak,
            "peak_hour": (hour_counts.most_common(1)[0][0] if hour_counts else None),
            "favorite_model": (_pretty_model(favorite["model"]) if favorite else None),
            "by_model": [
                {
                    "model": _pretty_model(m.get("model", "")),
                    "total_tokens": m.get("total_tokens", 0),
                    "usd": m.get("total_cost_usd", m.get("usd", 0.0)),
                }
                for m in by_model[:8]
            ],
            "daily": dict(day_counts),
            "window_days": days,
            "truncated": len(page.data) >= _MAX_SESSIONS,
        }
        # Drop stale entries opportunistically so the map can't grow unbounded.
        now = time.time()
        for key in [k for k, (at, _) in _cache.items() if now - at >= _CACHE_TTL_S]:
            _cache.pop(key, None)
        _cache[cache_key] = (now, payload)
        return payload

    return router
