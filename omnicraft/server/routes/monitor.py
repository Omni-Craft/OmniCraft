"""Monitor feed — one read that answers "what's running, what needs me".

``GET /v1/monitor/sessions`` is the single source every monitor surface
polls (the Electron HUD today, a native app later), so those surfaces
never re-derive session state from a handful of half-overlapping
endpoints.

Three things make it different from ``GET /v1/sessions``:

* ``waiting`` survives. The session-list shape collapses
  ``waiting`` into ``running``; a monitor's whole job is telling
  "still working" apart from "blocked on a human", so the collapse
  would erase the only distinction that matters here.
* Attention outranks recency. Rows are ordered by how much they need
  a human — blocked first, then failed, then active — and the page
  cap is applied *after* filtering and ranking. Capping a
  recency-ordered scan first is how a blocked session two hundred
  updates ago becomes an empty feed.
* An unresolved part is named, not smoothed over. A status this
  server has no record of is ``unknown``, not ``idle``; liveness it
  can't compute is ``null``, not "offline"; a prompt index it can't
  read leaves the count ``null``, not ``0``. Each degrades into an
  explicit marker on the row (or the feed), and a degraded row is
  never filtered out by ``only_active`` — that is how "we don't know"
  would silently become "nothing to see". Anything matching that the
  server never resolved at all is still counted (``counts.omitted``)
  and flagged (``counts.partial``), so the tallies read as a floor
  rather than a total.

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
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from sqlalchemy.exc import SQLAlchemyError

from omnicraft.entities.conversation import Conversation
from omnicraft.entities.permission import SessionPermission
from omnicraft.errors import ErrorCode, OmniCraftError
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

# Rows carried in one response. Applied AFTER filtering and ranking, so
# the cap drops the least urgent sessions, never the blocked ones.
_MAX_ROWS = 200

# Conversations pulled from the store per request. Wider than the row cap
# because ``only_active`` filters afterwards; ``truncated`` reports when
# the scan itself was cut.
_SCAN_LIMIT = 1000

# Ceiling on sessions pulled in by the attention rescue (below). Each one
# costs a row read, so a pathological index can't turn a poll into a scan.
_RESCUE_MAX = 50

# Depth guard when walking a sub-agent up to the session that represents
# it in the feed.
_ANCESTOR_MAX_DEPTH = 8

# Longest prompt excerpt carried on a row — enough for a HUD line.
_SUMMARY_MAX = 200

# Longest accepted ``host_id`` query value.
_HOST_ID_MAX = 128

# The relay-fed status cache holds these; anything else is a value this
# server doesn't understand and must not translate into a verdict.
# ``unknown`` is never cached — it is what this route reports when the
# cache has nothing to say about a session that could be running.
MonitorStatus = Literal["idle", "launching", "running", "waiting", "failed", "unknown"]
_KNOWN_CACHE_STATUSES = frozenset({"idle", "launching", "running", "waiting", "failed"})

# Cached statuses that mean a human may be needed. Sessions in these
# states are pulled into the feed even when they fall outside the scan.
_ATTENTION_STATUSES = frozenset({"waiting", "failed"})

# Feed ordering: how much each status wants a human's eyes.
_STATUS_RANK: dict[str, int] = {
    "waiting": 0,
    "failed": 1,
    "launching": 2,
    "running": 2,
    "unknown": 3,
    "idle": 4,
}


def _monitor_status(
    conversation_id: str,
    child_session_ids: list[str],
    *,
    dispatched: bool,
) -> tuple[MonitorStatus, list[str]]:
    """
    Resolve a session's monitor status, preserving ``waiting``.

    Builds on :func:`sessions._session_status_with_child_rollup` for the
    child/background rollup, then re-adds the granularity the list shape
    drops: an own ``waiting`` stays ``waiting``, a blocked child rolls up
    as ``waiting`` (not ``running`` — the parent's tree needs a human),
    and ``launching`` stays distinct from ``idle``.

    The status cache is in-memory and per-replica, so a miss is
    ignorance, not quiescence: a session that was dispatched (it has a
    runner or host binding) and isn't in the cache reads ``unknown``
    rather than ``idle``, because after a restart — or on another
    replica — a busy session looks exactly like a miss. A session with
    no binding at all has never been dispatched anywhere, so ``idle``
    there is a fact off the row, not an absence.

    :param conversation_id: Session identifier, e.g. ``"conv_abc123"``.
    :param child_session_ids: Direct sub-agent child ids.
    :param dispatched: Whether the session has a runner or host binding.
    :returns: ``(status, degraded)`` — the status literal, plus the
        degradation slugs earned while resolving it.
    """
    degraded: list[str] = []
    cache = sessions_module._session_status_cache
    raw = cache.get(conversation_id)
    unreadable = raw is not None and raw not in _KNOWN_CACHE_STATUSES
    if unreadable:
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
    # ``idle`` is only ever asserted from proof: either the relay said so
    # (cached ``idle``), or the row itself shows the session was never
    # dispatched anywhere. Silence about a session that HAS a binding is
    # ignorance — it reports ``unknown``.
    if raw == "idle":
        return "idle", degraded
    if unreadable:
        return "unknown", degraded
    if dispatched:
        return "unknown", [*degraded, "status_unknown"]
    return "idle", degraded


def _pending_for_row(
    conversation_id: str,
    child_session_ids: list[str],
    counts: dict[str, Any] | None,
) -> tuple[int | None, MonitorPendingElicitation | None, list[str]]:
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
        and its children. ``None`` when that lookup failed.
    :returns: ``(total, summary, degraded)``. ``total`` is ``None``
        when the index could not be read at all — a session that may be
        blocked must not be published as ``0``, which reads as "nobody
        is waiting on you". ``summary`` is ``None`` with a
        ``"pending_elicitation_unreadable"`` slug when the count is
        non-zero but no readable payload backs it. A count that isn't a
        usable number is treated as one unreadable prompt rather than
        dropped to zero.
    """
    if counts is None:
        return None, None, ["pending_elicitations_unknown"]
    own, own_degraded = _pending_count(counts.get(conversation_id))
    total = own
    degraded = list(own_degraded)
    for child_id in child_session_ids:
        child_count, child_degraded = _pending_count(counts.get(child_id))
        total += child_count
        degraded.extend(child_degraded)
    if total <= 0:
        return 0, None, degraded
    owner_id = conversation_id if own > 0 else None
    if owner_id is None:
        owner_id = next(
            (cid for cid in child_session_ids if _pending_count(counts.get(cid))[0] > 0),
            None,
        )
    events: list[dict[str, Any]] = []
    if owner_id is not None:
        try:
            events = pending_elicitations.snapshot_for(owner_id)
        except Exception:  # noqa: BLE001 — an unreadable index degrades, never 500s
            _logger.debug("Elicitation snapshot failed for %s", owner_id, exc_info=True)
    event = events[0] if events else None
    elicitation_id = event.get("elicitation_id") if isinstance(event, dict) else None
    if owner_id is None or not isinstance(elicitation_id, str) or not elicitation_id:
        return total, None, [*degraded, "pending_elicitation_unreadable"]
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


def _pending_count(raw: Any) -> tuple[int, list[str]]:
    """
    Coerce one entry of the pending-count map into a usable count.

    A malformed entry must not read as "no prompts outstanding" — the
    session may well be blocked. It counts as one, flagged.

    :param raw: The value the index returned for a session, e.g. ``2``.
    :returns: ``(count, degraded)``.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return (0, []) if raw is None else (1, ["pending_elicitation_unreadable"])
    return (raw, []) if raw >= 0 else (1, ["pending_elicitation_unreadable"])


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


def _row_rank(row: MonitorSessionItem) -> tuple[int, int, int]:
    """
    Sort key: how badly the row wants a human, then recency.

    Blocked-on-a-human first regardless of status, then failed, then
    active work, then unknown, then idle. Recency only breaks ties —
    it is the wrong primary key for a monitor, because the session that
    has been stuck waiting the longest is the one that updated least
    recently. A row whose pending count is *unknown* ranks with the
    blocked ones: it may be blocked, and the cap must not drop it.

    :param row: The assembled row.
    :returns: An ascending sort key.
    """
    pending = row.pending_elicitations_count
    return (
        0 if pending is None or pending > 0 else 1,
        _STATUS_RANK.get(row.status, len(_STATUS_RANK)),
        -row.updated_at,
    )


def create_monitor_router(
    conversation_store: ConversationStore,
    agent_store: Any,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    liveness_lookup: Any = None,
    host_store: Any = None,
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
    :param host_store: Store used to validate the ``host_id`` filter.
        ``None`` means the filter still applies (sessions stay
        ACL-scoped) but cannot be verified, which the feed reports as
        ``"host_unverified"``.
    :returns: The configured router.
    """
    router = APIRouter()

    async def _validate_host(host_id: str, user_id: str | None) -> None:
        """
        Reject a host filter that is malformed, unknown, not the
        caller's, or unverifiable.

        An unknown host must not answer with an empty feed — that reads
        as "nothing running on it". Neither may an *unverifiable* one:
        with no host registry to check against, a typo and a real host
        are indistinguishable, so the request is refused rather than
        answered with a feed that looks clean.

        :param host_id: The requested host filter.
        :param user_id: The authenticated caller, or ``None``.
        :raises OmniCraftError: 400 when malformed, 404 when unknown or
            owned by someone else, 503 when the registry is missing or
            unreachable.
        """
        if not host_id.strip() or len(host_id) > _HOST_ID_MAX:
            raise OmniCraftError(
                "host_id is not a valid host identifier", code=ErrorCode.INVALID_INPUT
            )
        if host_store is None:
            raise OmniCraftError(
                "host_id filtering is unavailable: this server has no host registry",
                code=ErrorCode.HOST_UNVERIFIABLE,
            )
        try:
            host = await asyncio.to_thread(host_store.get_host, host_id)
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            _logger.warning("Monitor feed host lookup failed for %s", host_id, exc_info=True)
            raise OmniCraftError(
                "host registry is unreachable; cannot scope the feed to this host",
                code=ErrorCode.HOST_UNVERIFIABLE,
            ) from exc
        if host is None:
            raise OmniCraftError("Host not found", code=ErrorCode.NOT_FOUND)
        if user_id is not None and host.owner != user_id:
            try:
                is_admin = (
                    await asyncio.to_thread(permission_store.is_admin, user_id)
                    if permission_store is not None
                    else False
                )
            except (SQLAlchemyError, OSError, TimeoutError) as exc:
                _logger.warning("Monitor feed host admin check failed", exc_info=True)
                raise OmniCraftError(
                    "cannot verify access to this host right now",
                    code=ErrorCode.HOST_UNVERIFIABLE,
                ) from exc
            if not is_admin:
                # Same answer as an unknown host: a caller who doesn't own
                # it learns nothing about whether it exists.
                raise OmniCraftError("Host not found", code=ErrorCode.NOT_FOUND)

    async def _batch_context(
        conv_ids: list[str],
        unique_agent_ids: list[str],
        user_id: str | None,
    ) -> tuple[
        dict[str, list[SessionPermission]],
        dict[str, str | None],
        dict[str, list[str]],
        bool,
        list[str],
    ]:
        """
        Pull the per-page batches ``GET /v1/sessions`` pulls, same shape.

        Each batch is independent: one that fails degrades its own field
        (a missing agent name, no owner level) instead of taking the feed
        down or, worse, coming back as a confident empty value.

        :returns: ``(grants, agent_names, child_ids, is_admin,
            degraded)``.
        """
        degraded: list[str] = []

        async def _safe(coro: Any, slug: str, fallback: Any) -> Any:
            try:
                return await coro
            except (SQLAlchemyError, OSError, TimeoutError):
                _logger.warning("Monitor feed batch %s failed", slug, exc_info=True)
                degraded.append(slug)
                return fallback

        perms = permission_store
        perms_task = (
            _safe(
                asyncio.to_thread(perms.list_for_sessions, conv_ids),
                "permissions_unavailable",
                {},
            )
            if perms is not None
            else None
        )
        names_task = _safe(
            asyncio.to_thread(agent_store.get_names, unique_agent_ids),
            "agent_names_unavailable",
            {},
        )
        children_task = _safe(
            asyncio.to_thread(conversation_store.list_child_conversation_ids_by_parent, conv_ids),
            "child_sessions_unavailable",
            {},
        )
        if perms is not None and perms_task is not None:
            perms_by_conv, agent_names_by_id, child_ids_by_parent = await asyncio.gather(
                perms_task, names_task, children_task
            )
            user_is_admin = (
                await _safe(
                    asyncio.to_thread(perms.is_admin, user_id),
                    "permissions_unavailable",
                    False,
                )
                if user_id is not None
                else False
            )
        else:
            agent_names_by_id, child_ids_by_parent = await asyncio.gather(
                names_task, children_task
            )
            perms_by_conv, user_is_admin = {}, False
        return perms_by_conv, agent_names_by_id, child_ids_by_parent, user_is_admin, degraded

    def _load_attention_rows(
        session_ids: list[str],
        already: set[str],
        host_id: str | None,
    ) -> list[Conversation]:
        """
        Resolve attention-bearing session ids into feed rows.

        A sub-agent is not a row of its own, so it resolves to the
        top-level session that carries it. Runs in a worker thread.

        :param session_ids: Ids to resolve.
        :param already: Ids the scan already produced.
        :param host_id: Active host filter, or ``None``.
        :returns: Conversations to append to the scan.
        """
        seen = set(already)
        rows: list[Conversation] = []
        for session_id in session_ids:
            conv = conversation_store.get_conversation(session_id)
            depth = 0
            while (
                conv is not None
                and conv.parent_conversation_id is not None
                and depth < _ANCESTOR_MAX_DEPTH
            ):
                conv = conversation_store.get_conversation(conv.parent_conversation_id)
                depth += 1
            if conv is None or conv.id in seen or conv.agent_id is None or conv.archived:
                continue
            if host_id is not None and conv.host_id != host_id:
                continue
            seen.add(conv.id)
            rows.append(conv)
        return rows

    async def _rescue_attention(
        already: set[str],
        user_id: str | None,
        host_id: str | None,
    ) -> tuple[list[Conversation], list[str], int]:
        """
        Pull in sessions that need a human but fell outside the scan.

        "Blocked on a human" and "failed" live in in-memory indexes
        keyed by session id, so they can be enumerated directly — which
        is the only way a session that has been waiting since long
        before the scan window still reaches the HUD.

        Whatever this path cannot resolve is *counted*, never dropped in
        silence: a session waiting on a human either lands in the
        response or is reported as unresolved, so the client can say
        "N more may need you" instead of showing an all-clear.

        :param already: Session ids the scan already returned.
        :param user_id: The authenticated caller, or ``None``.
        :param host_id: Active host filter, or ``None``.
        :returns: ``(conversations, degraded, unresolved)`` — where
            ``unresolved`` is the number of attention-bearing candidates
            this call could not resolve into rows.
        """
        candidates = set(pending_elicitations.pending_session_ids())
        candidates |= {
            sid
            for sid, status in sessions_module._session_status_cache.items()
            if status in _ATTENTION_STATUSES
        }
        candidates -= already
        if not candidates:
            return [], [], 0
        if permission_store is None and user_id is not None:
            # No grants to check against; including these rows could leak
            # another user's session, dropping them silently could hide the
            # caller's own. Say how many were left unresolved.
            return [], ["attention_rescue_unavailable"], len(candidates)
        degraded: list[str] = []
        ordered = sorted(candidates)
        unresolved = 0
        if len(ordered) > _RESCUE_MAX:
            unresolved = len(ordered) - _RESCUE_MAX
            degraded.append("attention_rescue_truncated")
            ordered = ordered[:_RESCUE_MAX]
        try:
            convs = await asyncio.to_thread(_load_attention_rows, ordered, already, host_id)
        except (SQLAlchemyError, OSError, TimeoutError):
            _logger.warning("Monitor feed attention rescue failed", exc_info=True)
            return [], ["attention_rescue_unavailable"], len(candidates)
        if not convs or permission_store is None:
            return convs, degraded, unresolved
        rescued_ids = [conv.id for conv in convs]
        try:
            grants = await asyncio.to_thread(permission_store.list_for_sessions, rescued_ids)
            is_admin = (
                await asyncio.to_thread(permission_store.is_admin, user_id)
                if user_id is not None
                else False
            )
        except (SQLAlchemyError, OSError, TimeoutError):
            _logger.warning("Monitor feed attention rescue authz failed", exc_info=True)
            return [], [*degraded, "attention_rescue_unavailable"], len(candidates)
        allowed = [
            conv
            for conv in convs
            if user_id is None
            or sessions_module._permission_level_from_grants(
                user_id, grants.get(conv.id, []), is_admin
            )
            is not None
        ]
        # Rows the ACL excluded are resolved, not unresolved — "not yours"
        # is an answer, so they must not inflate the unresolved tally.
        return allowed, degraded, unresolved

    async def _build_feed(
        user_id: str | None,
        host_id: str | None,
        only_active: bool,
        feed_degraded: list[str],
    ) -> MonitorFeedResponse:
        """Assemble the feed; see the route docstring for the semantics."""
        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=_SCAN_LIMIT,
            accessible_by=user_id,
            has_agent_id=True,
            kind="default",
            order="desc",
            sort_by="updated_at",
            include_archived=False,
            host_id=host_id,
        )
        convs = [conv for conv in page.data if conv.agent_id is not None]
        if page.has_more:
            # More sessions exist than were scanned, so the counts below
            # describe the scan, not the account.
            feed_degraded.append("scan_truncated")
        rescued, rescue_degraded, unresolved_attention = await _rescue_attention(
            {conv.id for conv in convs}, user_id, host_id
        )
        convs.extend(rescued)
        feed_degraded.extend(rescue_degraded)
        # A session that may be waiting on a human is either carried as a
        # row or counted here. It is never simply gone.
        counts_partial = page.has_more or unresolved_attention > 0
        if not convs:
            return MonitorFeedResponse(
                generated_at=int(time.time()),
                host_id=host_id,
                counts=MonitorCounts(omitted=unresolved_attention, partial=counts_partial),
                truncated=page.has_more or unresolved_attention > 0,
                degraded=feed_degraded,
            )

        conv_ids = [conv.id for conv in convs]
        unique_agent_ids = list({conv.agent_id for conv in convs if conv.agent_id is not None})
        (
            perms_by_conv,
            agent_names_by_id,
            child_ids_by_parent,
            user_is_admin,
            batch_degraded,
        ) = await _batch_context(conv_ids, unique_agent_ids, user_id)
        feed_degraded.extend(batch_degraded)
        # One in-memory sweep covering parents and their children, so the
        # child rollup below adds no lookups of its own.
        pending_ids = list(conv_ids)
        for cid in conv_ids:
            pending_ids.extend(child_ids_by_parent.get(cid, []))
        pending_counts: dict[str, Any] | None
        try:
            pending_counts = pending_elicitations.counts_for(pending_ids)
        except Exception:  # noqa: BLE001 — the index is in-memory; degrade, never 500
            # ``None``, not ``{}``: an empty map would publish every row as
            # "0 prompts outstanding", which is the all-clear this feed
            # exists to avoid claiming.
            _logger.warning("Monitor feed pending-elicitation counts failed", exc_info=True)
            pending_counts = None
            feed_degraded.append("pending_elicitations_unavailable")

        items = [
            sessions_module._build_session_list_item(
                conv,
                agent_names_by_id=agent_names_by_id,
                grants=perms_by_conv.get(conv.id, []),
                user_id=user_id,
                user_is_admin=user_is_admin,
                permissions_enabled=permission_store is not None,
                pending_count=(
                    _pending_count(pending_counts.get(conv.id))[0]
                    if pending_counts is not None
                    else 0
                ),
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

        convs_by_id = {conv.id: conv for conv in convs}
        rows: list[MonitorSessionItem] = []
        for item in items:
            conv = convs_by_id[item.id]
            child_ids = child_ids_by_parent.get(item.id, [])
            status, degraded = _monitor_status(
                item.id,
                child_ids,
                dispatched=conv.runner_id is not None or conv.host_id is not None,
            )
            pending_count, pending, pending_degraded = _pending_for_row(
                item.id, child_ids, pending_counts
            )
            degraded.extend(pending_degraded)
            cost, cost_degraded = _cost_usd(conv.session_usage)
            degraded.extend(cost_degraded)
            if not liveness_resolved:
                degraded.append("liveness_unavailable")
            elif item.runner_online is None or (
                conv.host_id is not None and item.host_online is None
            ):
                # Partial resolution: the lookup answered, but not for this
                # session. Unknown is not offline.
                degraded.append("liveness_partial")
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
        rows.sort(key=_row_rank)
        # Counts describe every matching session, including the ones the
        # row cap dropped — a headline that shrank with the page would be
        # the same lie as an empty feed. ``partial`` marks the counts as an
        # undercount whenever something matching was never resolved at all
        # (scan cut, attention rescue cut, unreadable prompt index), so a
        # client reading them knows they are a floor, not a total.
        capped = max(0, len(rows) - _MAX_ROWS)
        counts = MonitorCounts(
            active=sum(1 for row in rows if row.status not in ("idle", "unknown")),
            awaiting=sum(
                1
                for row in rows
                if row.pending_elicitations_count is not None
                and row.pending_elicitations_count > 0
            ),
            unknown=sum(1 for row in rows if row.status == "unknown"),
            omitted=capped + unresolved_attention,
            partial=(
                counts_partial or any(row.pending_elicitations_count is None for row in rows)
            ),
        )
        return MonitorFeedResponse(
            generated_at=int(time.time()),
            host_id=host_id,
            sessions=rows[:_MAX_ROWS],
            counts=counts,
            truncated=page.has_more or counts.omitted > 0,
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
        Monitor feed of the caller's sessions, most in need of a human first.

        :param host_id: When set, only sessions bound to this host,
            e.g. ``"host_abc123"``. Applied in the store query and
            validated first — an empty feed must never stand in for an
            answer about a host: ``400`` when the id is malformed,
            ``404`` when it is unknown or not the caller's, ``503`` when
            no host registry is available to check it against. ``None``
            (default) returns every session visible to the caller,
            including ones on other people's hosts shared with them.
        :param only_active: When ``True`` (the default), drop rows that
            are idle **and** have no outstanding prompt **and** resolved
            cleanly — the "needs an eye on it" view. Rows carrying a
            ``degraded`` marker (including every ``unknown`` status) are
            always kept, so an unresolved state is never mistaken for an
            idle one. ``False`` returns every non-archived session in
            the scan.
        :returns: A :class:`MonitorFeedResponse`. Infrastructure
            failures inside the feed degrade into explicit markers on
            the payload rather than an error status, so the contract is
            ``200`` plus ``degraded`` / ``counts.partial``, or one of
            the typed ``400`` / ``404`` / ``503`` above.
        """
        # Fail closed on auth: ``accessible_by=None`` means "no ACL
        # filter", so an unauthenticated caller slipping through as
        # ``None`` would monitor every user's sessions.
        user_id = require_user(request, auth_provider)
        feed_degraded: list[str] = []
        if host_id is not None:
            await _validate_host(host_id, user_id)
        try:
            return await _build_feed(user_id, host_id, only_active, feed_degraded)
        except (SQLAlchemyError, OSError, TimeoutError):
            # Infrastructure, not logic: report an unreadable feed. A bug
            # in this route must NOT land here — it would ship a 200 that
            # says "nothing needs you".
            _logger.exception("Monitor feed build failed")
            return MonitorFeedResponse(
                generated_at=int(time.time()),
                host_id=host_id,
                degraded=[*feed_degraded, "internal_error"],
            )

    return router
