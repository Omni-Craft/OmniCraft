"""Monitor feed — one read that answers "what's running, what needs me".

``GET /v1/monitor/sessions`` is the single source every monitor surface
polls (the Electron HUD today, a native app later), so those surfaces
never re-derive session state from a handful of half-overlapping
endpoints.

Two things make it different from ``GET /v1/sessions``:

* ``waiting`` survives. The session-list shape collapses
  ``waiting`` into ``running``; a monitor's whole job is telling
  "still working" apart from "blocked on a human", so the collapse
  would erase the only distinction that matters here.
* An unresolved part is named, not smoothed over. Liveness that
  can't be computed, an elicitation payload that can't be read, a
  cost blob that isn't a number — each degrades into an explicit
  marker on the row (or the feed) instead of a value that happens to
  look fine. The route never returns 5xx: a feed that fails entirely
  comes back with ``degraded=["internal_error"]`` and no rows, which
  a client must not read as "nothing is running".

Row assembly reuses the session-list builder and the same batched
pulls ``GET /v1/sessions`` makes (labels, permission grants, agent
names, child ids), so listing N sessions stays a fixed number of
queries.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Literal, get_args

from fastapi import APIRouter, Query, Request

from omnicraft.entities.permission import SessionPermission
from omnicraft.errors import OmniCraftError
from omnicraft.runtime import pending_elicitations
from omnicraft.server.auth import AuthProvider
from omnicraft.server.routes import sessions as sessions_module
from omnicraft.server.routes._auth_helpers import require_user
from omnicraft.server.schemas import (
    MonitorCounts,
    MonitorFeedResponse,
    MonitorPendingElicitation,
    MonitorSessionItem,
)
from omnicraft.stores.conversation_store import PROJECT_LABEL_KEY, ConversationStore
from omnicraft.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

# Cap on rows scanned per request. The feed is polled, so it trades
# completeness for a bounded response; ``truncated`` tells the client
# when the cap bit.
_MAX_SESSIONS = 200

# Longest prompt excerpt carried on a row — enough for a HUD line.
_SUMMARY_MAX = 200

# The relay-fed status cache holds these; anything else is a value this
# server doesn't understand and must not translate into a verdict.
MonitorStatus = Literal["idle", "launching", "running", "waiting", "failed"]
_KNOWN_STATUSES = frozenset(get_args(MonitorStatus))


def _monitor_status(
    conversation_id: str,
    child_session_ids: list[str],
) -> tuple[MonitorStatus, list[str]]:
    """
    Resolve a session's monitor status, preserving ``waiting``.

    Builds on :func:`sessions._session_status_with_child_rollup` for the
    child/background rollup, then re-adds the granularity the list shape
    drops: an own ``waiting`` stays ``waiting``, a blocked child rolls up
    as ``waiting`` (not ``running`` — the parent's tree needs a human),
    and ``launching`` stays distinct from ``idle``.

    :param conversation_id: Session identifier, e.g. ``"conv_abc123"``.
    :param child_session_ids: Direct sub-agent child ids.
    :returns: ``(status, degraded)`` — the status literal, plus the
        degradation slugs earned while resolving it (a cached value this
        server doesn't recognize yields ``"status_unreadable"`` rather
        than a silent ``"idle"``).
    """
    degraded: list[str] = []
    cache = sessions_module._session_status_cache
    raw = cache.get(conversation_id)
    if raw is not None and raw not in _KNOWN_STATUSES:
        degraded.append("status_unreadable")
        raw = None
    rolled = sessions_module._session_status_with_child_rollup(conversation_id, child_session_ids)
    if rolled == "failed":
        return "failed", degraded
    if raw == "waiting":
        return "waiting", degraded
    if rolled == "running":
        # Rolled up from a child or the background-shell tally. A blocked
        # child is a human-facing wait, not silent progress.
        if raw != "running" and any(cache.get(cid) == "waiting" for cid in child_session_ids):
            return "waiting", degraded
        return "running", degraded
    if raw == "launching":
        return "launching", degraded
    return "idle", degraded


def _pending_for_row(
    conversation_id: str,
    child_session_ids: list[str],
    counts: dict[str, int],
) -> tuple[int, MonitorPendingElicitation | None, list[str]]:
    """
    Total outstanding prompts for a row, plus a summary of the first one.

    A sub-agent child's prompt is mirrored into its ancestors' streams,
    so the parent row is where a human acts on it; the count therefore
    covers the session and its direct children. Own prompts win the
    summary slot.

    :param conversation_id: Session identifier, e.g. ``"conv_abc123"``.
    :param child_session_ids: Direct sub-agent child ids.
    :param counts: Batched pending counts from
        :func:`pending_elicitations.counts_for`, covering the session
        and its children.
    :returns: ``(total, summary, degraded)``. ``summary`` is ``None``
        with a ``"pending_elicitation_unreadable"`` slug when the count
        is non-zero but no readable payload backs it — an absent summary
        must never read as "nothing to decide".
    """
    own = counts.get(conversation_id, 0)
    total = own + sum(counts.get(cid, 0) for cid in child_session_ids)
    if total <= 0:
        return 0, None, []
    owner_id = conversation_id if own > 0 else None
    if owner_id is None:
        owner_id = next((cid for cid in child_session_ids if counts.get(cid, 0) > 0), None)
    events: list[dict[str, Any]] = []
    if owner_id is not None:
        try:
            events = pending_elicitations.snapshot_for(owner_id)
        except Exception:  # noqa: BLE001 — an unreadable index degrades, never 500s
            _logger.debug("Elicitation snapshot failed for %s", owner_id, exc_info=True)
    event = events[0] if events else None
    elicitation_id = event.get("elicitation_id") if isinstance(event, dict) else None
    if owner_id is None or not isinstance(elicitation_id, str) or not elicitation_id:
        return total, None, ["pending_elicitation_unreadable"]
    params = event.get("params") if isinstance(event, dict) else None
    params = params if isinstance(params, dict) else {}
    kind = next(
        (
            value
            for value in (params.get("policy_name"), params.get("phase"), params.get("mode"))
            if isinstance(value, str) and value
        ),
        "unknown",
    )
    message = params.get("message")
    degraded: list[str] = []
    if isinstance(message, str) and message:
        summary = message[:_SUMMARY_MAX]
    else:
        summary = None
        degraded.append("pending_elicitation_unreadable")
    return (
        total,
        MonitorPendingElicitation(
            id=elicitation_id,
            session_id=owner_id,
            kind=kind,
            summary=summary,
        ),
        degraded,
    )


def _cost_usd(session_usage: Any) -> tuple[float | None, list[str]]:
    """
    Read the session's own recorded spend off its usage blob.

    Deliberately shallow: the blob already rides on the conversation row
    the feed loaded, so this costs nothing. Sub-agent spend would need
    the paginated subtree walk (``load_session_usage``) that makes the
    feed slow, so it is left out rather than paid for on every poll.

    :param session_usage: The conversation's ``session_usage`` blob.
    :returns: ``(cost, degraded)``. ``None`` when nothing is recorded;
        a present-but-unusable value also yields ``"cost_unreadable"``
        so a null is never mistaken for a measured zero.
    """
    if not isinstance(session_usage, dict):
        return None, []
    raw = session_usage.get("total_cost_usd")
    if raw is None:
        return None, []
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None, ["cost_unreadable"]
    value = float(raw)
    if not math.isfinite(value):
        return None, ["cost_unreadable"]
    return value, []


def create_monitor_router(
    conversation_store: ConversationStore,
    agent_store: Any,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    liveness_lookup: Any = None,
) -> APIRouter:
    """
    Build the router for ``GET /v1/monitor/sessions``.

    :param conversation_store: Store the session page is read from.
    :param agent_store: Store agent display names are batch-read from.
    :param auth_provider: Auth provider; ``None`` disables auth (the
        single-user CLI server).
    :param permission_store: Permission store, or ``None`` when
        permissions are disabled.
    :param liveness_lookup: Bulk session-liveness lookup (the app's
        ``_bulk_session_liveness``). ``None`` means this server cannot
        compute liveness, which the feed reports as unknown rather than
        offline.
    :returns: The configured router.
    """
    router = APIRouter()

    async def _batch_context(
        conv_ids: list[str],
        unique_agent_ids: list[str],
        user_id: str | None,
    ) -> tuple[
        dict[str, list[SessionPermission]], dict[str, str | None], dict[str, list[str]], bool
    ]:
        """Pull the per-page batches ``GET /v1/sessions`` pulls, same shape."""
        if permission_store is not None:
            perms_by_conv, agent_names_by_id, child_ids_by_parent = await asyncio.gather(
                asyncio.to_thread(permission_store.list_for_sessions, conv_ids),
                asyncio.to_thread(agent_store.get_names, unique_agent_ids),
                asyncio.to_thread(
                    conversation_store.list_child_conversation_ids_by_parent, conv_ids
                ),
            )
            user_is_admin = (
                await asyncio.to_thread(permission_store.is_admin, user_id)
                if user_id is not None
                else False
            )
            return perms_by_conv, agent_names_by_id, child_ids_by_parent, user_is_admin
        agent_names_by_id, child_ids_by_parent = await asyncio.gather(
            asyncio.to_thread(agent_store.get_names, unique_agent_ids),
            asyncio.to_thread(conversation_store.list_child_conversation_ids_by_parent, conv_ids),
        )
        return {}, agent_names_by_id, child_ids_by_parent, False

    async def _build_feed(
        user_id: str | None,
        host_id: str | None,
        only_active: bool,
    ) -> MonitorFeedResponse:
        """Assemble the feed; see the route docstring for the semantics."""
        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=_MAX_SESSIONS,
            accessible_by=user_id,
            has_agent_id=True,
            kind="default",
            order="desc",
            sort_by="updated_at",
            include_archived=False,
        )
        convs = [conv for conv in page.data if conv.agent_id is not None]
        if host_id is not None:
            convs = [conv for conv in convs if conv.host_id == host_id]
        feed_degraded: list[str] = []
        if not convs:
            return MonitorFeedResponse(
                generated_at=int(time.time()),
                host_id=host_id,
                truncated=page.has_more,
            )

        conv_ids = [conv.id for conv in convs]
        unique_agent_ids = list({conv.agent_id for conv in convs if conv.agent_id is not None})
        (
            perms_by_conv,
            agent_names_by_id,
            child_ids_by_parent,
            user_is_admin,
        ) = await _batch_context(conv_ids, unique_agent_ids, user_id)
        # One in-memory sweep covering parents and their children, so the
        # child rollup below adds no lookups of its own.
        pending_ids = list(conv_ids)
        for cid in conv_ids:
            pending_ids.extend(child_ids_by_parent.get(cid, []))
        pending_counts = pending_elicitations.counts_for(pending_ids)

        items = [
            sessions_module._build_session_list_item(
                conv,
                agent_names_by_id=agent_names_by_id,
                grants=perms_by_conv.get(conv.id, []),
                user_id=user_id,
                user_is_admin=user_is_admin,
                permissions_enabled=permission_store is not None,
                pending_count=pending_counts.get(conv.id, 0),
                child_session_ids=child_ids_by_parent.get(conv.id, []),
                comments_fingerprint=None,
            )
            for conv in convs
        ]
        liveness_resolved = liveness_lookup is not None
        if liveness_resolved:
            try:
                await sessions_module._apply_liveness_to_items(items, liveness_lookup)
            except Exception:  # noqa: BLE001 — unknown liveness degrades, never 500s
                _logger.warning("Monitor feed liveness lookup failed", exc_info=True)
                liveness_resolved = False
        if not liveness_resolved:
            feed_degraded.append("liveness_unavailable")
            for item in items:
                item.runner_online = None
                item.host_online = None

        usage_by_id = {conv.id: conv.session_usage for conv in convs}
        rows: list[MonitorSessionItem] = []
        for item in items:
            child_ids = child_ids_by_parent.get(item.id, [])
            status, degraded = _monitor_status(item.id, child_ids)
            pending_count, pending, pending_degraded = _pending_for_row(
                item.id, child_ids, pending_counts
            )
            degraded.extend(pending_degraded)
            cost, cost_degraded = _cost_usd(usage_by_id.get(item.id))
            degraded.extend(cost_degraded)
            if not liveness_resolved:
                degraded.append("liveness_unavailable")
            # An unresolved row is never filtered out: "we don't know"
            # must not disappear from the feed as if it were idle.
            if only_active and status == "idle" and pending_count == 0 and not degraded:
                continue
            rows.append(
                MonitorSessionItem(
                    session_id=item.id,
                    agent_name=item.agent_name,
                    title=item.title,
                    project=item.labels.get(PROJECT_LABEL_KEY),
                    workspace=item.workspace,
                    status=status,
                    pending_elicitations_count=pending_count,
                    pending_elicitation=pending,
                    runner_online=item.runner_online,
                    host_online=item.host_online,
                    updated_at=item.updated_at,
                    cost_usd=cost,
                    degraded=degraded,
                )
            )
        return MonitorFeedResponse(
            generated_at=int(time.time()),
            host_id=host_id,
            sessions=rows,
            counts=MonitorCounts(
                active=sum(1 for row in rows if row.status != "idle"),
                awaiting=sum(1 for row in rows if row.pending_elicitations_count > 0),
            ),
            truncated=page.has_more,
            degraded=feed_degraded,
        )

    @router.get(
        "/monitor/sessions",
        response_model=None,
        responses={200: {"model": MonitorFeedResponse}},
    )
    async def monitor_sessions(
        request: Request,
        host_id: str | None = Query(default=None),
        only_active: bool = Query(default=True),
    ) -> MonitorFeedResponse:
        """
        Monitor feed of the caller's sessions, newest activity first.

        :param host_id: When set, only sessions bound to this host,
            e.g. ``"host_abc123"``. ``None`` (default) returns every
            session visible to the caller.
        :param only_active: When ``True`` (the default), drop rows that
            are idle **and** have no outstanding prompt **and** resolved
            cleanly — the "needs an eye on it" view. Rows carrying a
            ``degraded`` marker are always kept, so an unknown state is
            never mistaken for an idle one. ``False`` returns every
            non-archived session in the page.
        :returns: A :class:`MonitorFeedResponse`. Never 5xx — an
            internal failure comes back as an empty ``sessions`` list
            with ``degraded=["internal_error"]``.
        """
        # Fail closed on auth: ``accessible_by=None`` means "no ACL
        # filter", so an unauthenticated caller slipping through as
        # ``None`` would monitor every user's sessions.
        user_id = require_user(request, auth_provider)
        try:
            return await _build_feed(user_id, host_id, only_active)
        except OmniCraftError:
            raise
        except Exception:
            _logger.exception("Monitor feed build failed")
            return MonitorFeedResponse(
                generated_at=int(time.time()),
                host_id=host_id,
                degraded=["internal_error"],
            )

    return router
