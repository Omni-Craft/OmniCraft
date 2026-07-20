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

One more distinction the feed refuses to blur: a **local counter** is
not a **quota**. Everything under ``usage`` is a running total this
server summed as turns landed. A provider quota — a window with a
remaining allowance and a reset — never reaches this server at all: the
adapters call the provider without reading rate-limit headers. So there
is no quota field here, not even a null one, because a field shaped
like a percentage is an invitation to fill it in. The only real
denominator on a row is a limit somebody *declared* (``usage.budget``),
and it is the only thing a percentage may be computed against.

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
    MonitorSessionBudget,
    MonitorSessionItem,
    MonitorSessionUsage,
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

# Settled rows carried per response, capped separately from ``_MAX_ROWS``
# so a burst of completions can never displace an active row (nor be
# displaced by one). What it cannot carry is reported in
# ``settled_omitted`` rather than silently dropped.
_MAX_SETTLED_ROWS = 50

# Ceiling on the ``settled_grace_seconds`` window a caller may ask for.
# The parameter exists so a poller can WITNESS a session settling; it is
# not a way to page through history, and an unbounded window would drag
# every idle session back into the ``only_active`` view.
_MAX_SETTLED_GRACE_SECONDS = 3600

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


# Token buckets read off the usage blob, in the order they are reported.
# Nothing here is derived from anything else: ``total_tokens`` is its own
# recorded figure, because adding two unknowns would manufacture a number.
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

# The one built-in budget whose denominator is this session's own
# ``total_cost_usd`` — the very number the row publishes. The per-user
# daily and sub-agent subtree variants measure different quantities, so
# dividing this row's cost by their limit would be a percentage of
# nothing.
_COST_BUDGET_PATH = "omnicraft.policies.builtins.cost.cost_budget"


def _token_count(raw: Any) -> tuple[int | None, bool]:
    """
    Read one token bucket off the usage blob.

    :param raw: The blob's value for the bucket, e.g. ``1024``.
    :returns: ``(count, unreadable)``. Absent is ``None`` and *not*
        flagged — a harness that bills without reporting tokens simply
        recorded nothing here. Present-but-unusable is ``None`` too,
        flagged, because a bucket we failed to read is not an empty
        one.
    """
    if raw is None:
        return None, False
    if isinstance(raw, bool):
        return None, True
    if isinstance(raw, int):
        return (raw, False) if raw >= 0 else (None, True)
    if isinstance(raw, float) and math.isfinite(raw) and raw >= 0 and raw.is_integer():
        return int(raw), False
    return None, True


def _positive_usd(raw: Any) -> float | None:
    """
    Coerce a declared USD limit, or refuse it.

    :param raw: A limit off a policy's factory arguments, e.g. ``5.0``.
    :returns: The value, or ``None`` when it is not a usable positive
        amount. A non-positive limit is not a small budget — it is a
        denominator that would turn every ratio into nonsense.
    """
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    value = float(raw)
    return value if math.isfinite(value) and value > 0 else None


def _budget_from_spec(
    spec: Any, sub_agent_name: str | None
) -> tuple[MonitorSessionBudget | None, bool]:
    """
    The cost budget an agent spec declares for its own session spend.

    Walks ``guardrails.policies`` for the built-in session cost budget
    and reports the limit it was configured with. Several declarations
    are collapsed to the tightest one, because that is the gate that
    fires first.

    A label-gated policy (``condition``) is *not* read as absent: it
    may be in force right now and this feed has no labels to check it
    against, so it is reported as unsettled instead. Same for a limit
    that isn't a usable number, and same for a spec that could not be
    resolved at all — "we never read it" is unsettled, never "there is
    none".

    Checkpoints stay attached to the policy that declared them until
    the effective cap is known, then they are normalised against it: a
    threshold a looser policy declared under its own larger cap can sit
    above the cap that actually fires, and publishing it would state a
    checkpoint the session can never reach — one the client refuses
    outright, taking the whole budget down with it.

    Unsettled is **all-or-nothing**: one declaration this function
    cannot settle discards the ones it could. A budget is only worth
    publishing if it is provably the tightest gate, and a limit read
    alongside a conditional or malformed sibling is not — dividing by
    it would print a comfortable percentage against a cap that may not
    be the one about to fire.

    :param spec: A parsed ``AgentSpec``, or ``None`` when the agent's
        bundle could not be resolved.
    :param sub_agent_name: The bundled sub-agent this session runs as,
        or ``None`` for the brain itself. A head has its own
        guardrails, so the brain's budget must not be reported for it.
    :returns: ``(budget, uncertain)``. *uncertain* means a cost budget
        may well be in force but its limit could not be settled; the
        budget is then ``None`` **and** the row degrades, so it neither
        shows a denominator it cannot vouch for nor implies there is
        none. The two are never both truthy.
    """
    if spec is None:
        # Not "this agent declares no budget" — this feed never read the
        # spec that would say. Answering ``None`` quietly would let a
        # declared cap blink out of existence.
        return None, True
    if sub_agent_name is not None:
        heads = getattr(spec, "sub_agents", None) or []
        spec = next((s for s in heads if getattr(s, "name", None) == sub_agent_name), None)
        if spec is None:
            # The head this session runs as isn't in the spec we hold, so
            # the brain's budget says nothing about it.
            return None, True
    guardrails = getattr(spec, "guardrails", None)
    policies = getattr(guardrails, "policies", None) or []
    uncertain = False
    maxima: list[float] = []
    # Kept per declaration, not merged: a checkpoint only means anything
    # next to the cap it was declared under, and the effective cap is not
    # known until every policy has been walked.
    declared: list[tuple[float | None, set[float]]] = []
    for policy in policies:
        function = getattr(policy, "function", None)
        if function is None or getattr(function, "path", None) != _COST_BUDGET_PATH:
            continue
        if getattr(policy, "condition", None):
            uncertain = True
            continue
        arguments = getattr(function, "arguments", None) or {}
        raw_max = arguments.get("max_cost_usd")
        limit = None if raw_max is None else _positive_usd(raw_max)
        if raw_max is not None and limit is None:
            uncertain = True
        elif limit is not None:
            maxima.append(limit)
        raw_thresholds = arguments.get("ask_thresholds_usd") or []
        if not isinstance(raw_thresholds, list):
            uncertain = True
            raw_thresholds = []
        checkpoints: set[float] = set()
        for raw_threshold in raw_thresholds:
            checkpoint = _positive_usd(raw_threshold)
            # The gate itself refuses a checkpoint outside ``(0, max)``, so a
            # spec carrying one would not build. Reading it back as a valid
            # budget would report a limit that could never have been enforced.
            if checkpoint is None or (limit is not None and checkpoint >= limit):
                uncertain = True
            else:
                checkpoints.add(checkpoint)
        declared.append((limit, checkpoints))
    # Fail closed. A single unsettled declaration means the tightest gate is
    # not known — and the whole worth of this field is being the one
    # denominator a surface may divide by. Publishing the limits that DID
    # parse would hand out a percentage against a cap that may not be the
    # binding one, which is the number this feed exists not to invent.
    if uncertain:
        return None, True
    cap = min(maxima) if maxima else None
    # A checkpoint at or above the cap that actually fires cannot be
    # reached, whichever policy declared it: the run stops first. Dropping
    # it is not losing a warning, it is refusing to publish one that would
    # never arrive — and that the client rejects the whole budget over.
    thresholds = sorted(
        {point for _, points in declared for point in points if cap is None or point < cap}
    )
    if cap is None and not thresholds:
        return None, False
    return MonitorSessionBudget(max_cost_usd=cap, thresholds_usd=thresholds), False


def _usage_for_row(
    session_usage: Any,
    cost: float | None,
    spec: Any,
    sub_agent_name: str | None,
) -> tuple[MonitorSessionUsage, list[str]]:
    """
    Assemble a row's local usage counters and its declared budget.

    Same rule as everywhere else on this feed: a bucket the blob
    doesn't carry is ``None``, never ``0``. Claiming a session burned
    zero tokens off a payload that simply didn't mention them is the
    same class of lie as an empty feed.

    :param session_usage: The conversation's ``session_usage`` blob.
    :param cost: The spend already read by :func:`_cost_usd`, reused so
        the row and its usage object cannot disagree.
    :param spec: The agent's parsed spec, or ``None`` when it could not
        be resolved — which degrades the row rather than reading as an
        agent that declared no budget.
    :param sub_agent_name: The bundled sub-agent this session runs as.
    :returns: ``(usage, degraded)``.
    """
    budget, budget_uncertain = _budget_from_spec(spec, sub_agent_name)
    degraded = ["budget_unreadable"] if budget_uncertain else []
    if not isinstance(session_usage, dict):
        # A blob that isn't an object is a total we lost, not a session
        # that spent nothing.
        return (
            MonitorSessionUsage(cost_usd=cost, budget=budget),
            [*degraded, "usage_unreadable"] if session_usage is not None else degraded,
        )
    counts: dict[str, int | None] = {}
    unreadable = False
    for field in _TOKEN_FIELDS:
        counts[field], field_unreadable = _token_count(session_usage.get(field))
        unreadable = unreadable or field_unreadable
    if unreadable:
        degraded.append("usage_unreadable")
    return MonitorSessionUsage(cost_usd=cost, budget=budget, **counts), degraded


def _resolve_specs(agent_ids: list[str], agent_store: Any) -> dict[str, Any]:
    """
    Agent specs for the page's agents, read from their stored bundles.

    Deliberately *not* the warm cache tier alone. ``bundle_location`` is
    content-addressed and persisted on the agent row, so every replica
    and every restart resolves the same spec for the same agent — the
    budget a row published a minute ago is the budget it publishes now.
    Reading whichever specs happened to be parsed in this process made a
    declared cap vanish after a restart, with no degradation to say so.

    The cost is still paid once: the loader's own tiers answer from
    memory, then from the extracted directory on disk, and only a
    genuinely cold agent fetches its bundle. Callers run this off the
    event loop.

    An agent missing from the returned map is **unknown**, never "no
    budget" — the caller degrades those rows.

    :param agent_ids: Unique agent ids on the page.
    :param agent_store: Store holding each agent's ``bundle_location``.
    :returns: ``{agent_id: AgentSpec}`` for the agents that resolved.
    """
    try:
        from omnicraft.runtime import get_agent_cache

        cache = get_agent_cache()
    except Exception:  # noqa: BLE001 — no runtime cache is "unknown", not an error
        _logger.debug("Monitor feed has no agent cache to read specs from", exc_info=True)
        return {}
    specs: dict[str, Any] = {}
    for agent_id in agent_ids:
        try:
            agent = agent_store.get(agent_id)
            if agent is None:
                continue
            # ``expand_env`` follows the agent's provenance, never the
            # caller's convenience: expanding a tenant-supplied bundle
            # against the server env would leak its secrets into the spec.
            specs[agent_id] = cache.load(
                agent.id,
                agent.bundle_location,
                expand_env=agent.session_id is None,
            ).spec
        except Exception:  # noqa: BLE001 — one bad agent must not cost the page
            _logger.debug("Monitor feed spec load failed for %s", agent_id, exc_info=True)
    return specs


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


class _Degradation:
    """
    The feed's one channel for "the server could not work this out".

    Every unresolved part of a response goes through :meth:`note` (or,
    when there is no feed-wide slug to add, :meth:`note_floor`), and
    both mark the tallies as a floor. Keeping the marker and the slug in
    a single call is the point: with N separate ``append`` sites, one of
    them eventually forgets to also set ``partial`` and an incomplete
    count ships as a total. Here that combination is unreachable.

    The slug list is read-only from the outside (:attr:`slugs` hands
    back a tuple, and there is no setter) so the invariant is enforced
    by the type rather than by every call site remembering it: there is
    no ``degradation.slugs.append(...)`` that could record a failure
    while leaving the tallies looking complete.

    :param slugs: Feed-level degradation slugs, in first-seen order.
    :param partial: Whether anything at all went unresolved.
    """

    def __init__(self) -> None:
        self._slugs: list[str] = []
        self.partial: bool = False

    @property
    def slugs(self) -> tuple[str, ...]:
        """Feed-level slugs recorded so far, in first-seen order."""
        return tuple(self._slugs)

    def note(self, slug: str) -> None:
        """
        Record a feed-level failure and mark the tallies a floor.

        :param slug: Stable slug, e.g. ``"child_sessions_unavailable"``.
        """
        if slug not in self._slugs:
            self._slugs.append(slug)
        self.partial = True

    def note_floor(self) -> None:
        """Mark the tallies a floor without adding a feed-level slug.

        For gaps already named on the row that carries them (an
        unreadable prompt count), where the feed-wide list would only
        duplicate the row's own marker.
        """
        self.partial = True


def _unreadable_feed(host_id: str | None, degradation: _Degradation) -> MonitorFeedResponse:
    """
    The terminal answer when the feed could not be built at all.

    :param host_id: The requested host filter, echoed back.
    :param degradation: Accumulator carrying whatever was already noted.
    :returns: An empty feed marked ``internal_error``. The tallies are
        zero but flagged ``partial``: the server doesn't know what is
        running, so zero is a floor, never a total.
    """
    degradation.note("internal_error")
    return MonitorFeedResponse(
        generated_at=int(time.time()),
        host_id=host_id,
        counts=MonitorCounts(partial=degradation.partial),
        truncated=True,
        degraded=list(degradation.slugs),
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
        ``None`` means a ``host_id`` cannot be checked at all, so the
        request is refused with a typed ``503 host_unverifiable`` rather
        than answered with a feed scoped to nothing.
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
        except Exception as exc:
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
            except Exception as exc:
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
        degradation: _Degradation,
    ) -> tuple[
        dict[str, list[SessionPermission]],
        dict[str, str | None],
        dict[str, list[str]],
        bool,
    ]:
        """
        Pull the per-page batches ``GET /v1/sessions`` pulls, same shape.

        Each batch is independent: one that fails degrades its own field
        (a missing agent name, no owner level) instead of taking the feed
        down or, worse, coming back as a confident empty value. A lost
        batch also costs coverage — no child ids means a parent's blocked
        sub-agent is invisible to the tallies — so every fallback goes
        through *degradation*, which marks the counts a floor.

        :returns: ``(grants, agent_names, child_ids, is_admin)``.
        """

        async def _safe(coro: Any, slug: str, fallback: Any) -> Any:
            try:
                return await coro
            except Exception:  # noqa: BLE001 — any failed batch degrades, none 500s
                _logger.warning("Monitor feed batch %s failed", slug, exc_info=True)
                degradation.note(slug)
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
        return perms_by_conv, agent_names_by_id, child_ids_by_parent, user_is_admin

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
        degradation: _Degradation,
    ) -> tuple[list[Conversation], int]:
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
        :param degradation: Accumulator every failure is reported to.
        :returns: ``(conversations, unresolved)`` — where ``unresolved``
            is the number of attention-bearing candidates this call
            could not resolve into rows.
        """
        candidates = set(pending_elicitations.pending_session_ids())
        candidates |= {
            sid
            for sid, status in sessions_module._session_status_cache.items()
            if status in _ATTENTION_STATUSES
        }
        candidates -= already
        if not candidates:
            return [], 0
        if permission_store is None and user_id is not None:
            # No grants to check against; including these rows could leak
            # another user's session, dropping them silently could hide the
            # caller's own. Say how many were left unresolved.
            degradation.note("attention_rescue_unavailable")
            return [], len(candidates)
        ordered = sorted(candidates)
        unresolved = 0
        if len(ordered) > _RESCUE_MAX:
            unresolved = len(ordered) - _RESCUE_MAX
            degradation.note("attention_rescue_truncated")
            ordered = ordered[:_RESCUE_MAX]
        try:
            convs = await asyncio.to_thread(_load_attention_rows, ordered, already, host_id)
        except Exception:  # noqa: BLE001 — an unresolved sweep degrades, never 500s
            _logger.warning("Monitor feed attention rescue failed", exc_info=True)
            degradation.note("attention_rescue_unavailable")
            return [], len(candidates)
        if not convs or permission_store is None:
            return convs, unresolved
        rescued_ids = [conv.id for conv in convs]
        try:
            grants = await asyncio.to_thread(permission_store.list_for_sessions, rescued_ids)
            is_admin = (
                await asyncio.to_thread(permission_store.is_admin, user_id)
                if user_id is not None
                else False
            )
        except Exception:  # noqa: BLE001 — an unresolved sweep degrades, never 500s
            _logger.warning("Monitor feed attention rescue authz failed", exc_info=True)
            degradation.note("attention_rescue_unavailable")
            return [], len(candidates)
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
        return allowed, unresolved

    def _settled_recently(updated_at: int | None, now: int, grace_seconds: int) -> bool:
        """
        Whether a settled session finished recently enough to still be carried.

        A client that polls ``only_active`` sees a session that finishes
        VANISH, and an absence is not a fact: a row can be missing because
        the work ended, because the row cap dropped it, or because the
        filter changed. Carrying settled rows for a short grace window is
        what lets a poller witness the transition itself instead of
        inferring one from a gap.

        An unreadable ``updated_at`` is not proof of anything, so it does
        not extend the window; a timestamp in the future is treated the
        same as "now" rather than being trusted to push the row out.
        """
        if grace_seconds <= 0 or updated_at is None:
            return False
        return now - updated_at <= grace_seconds

    async def _build_feed(
        user_id: str | None,
        host_id: str | None,
        only_active: bool,
        settled_grace_seconds: int,
        degradation: _Degradation,
    ) -> MonitorFeedResponse:
        """Assemble the feed; see the route docstring for the semantics."""
        # One reading of the clock for the whole feed: ``generated_at`` is what
        # a client measures row ages against, so the window that decides which
        # settled rows are carried has to be measured from the same instant.
        now = int(time.time())
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
            degradation.note("scan_truncated")
        # A session that may be waiting on a human is either carried as a
        # row or counted here. It is never simply gone.
        rescued, unresolved_attention = await _rescue_attention(
            {conv.id for conv in convs}, user_id, host_id, degradation
        )
        convs.extend(rescued)
        if not convs:
            return MonitorFeedResponse(
                generated_at=now,
                host_id=host_id,
                counts=MonitorCounts(omitted=unresolved_attention, partial=degradation.partial),
                truncated=page.has_more or unresolved_attention > 0,
                degraded=list(degradation.slugs),
            )

        conv_ids = [conv.id for conv in convs]
        unique_agent_ids = list({conv.agent_id for conv in convs if conv.agent_id is not None})
        (
            perms_by_conv,
            agent_names_by_id,
            child_ids_by_parent,
            user_is_admin,
        ) = await _batch_context(conv_ids, unique_agent_ids, user_id, degradation)
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
            degradation.note("pending_elicitations_unavailable")

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
            degradation.note("liveness_unavailable")
            for item in items:
                item.runner_online = None
                item.host_online = None

        # One lookup per distinct agent, off the event loop; the loader's
        # own tiers make every poll after the first a dict hit.
        agent_specs = await asyncio.to_thread(_resolve_specs, unique_agent_ids, agent_store)
        convs_by_id = {conv.id: conv for conv in convs}
        rows: list[MonitorSessionItem] = []
        # Sessions that settled inside the grace window. Deliberately NOT in
        # ``rows``: they exist so a poller can witness a session finishing,
        # and letting them into the active view would rank them, count them
        # and — worst of all — let the row cap drop the very transition they
        # were carried to show.
        settled_rows: list[MonitorSessionItem] = []
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
            usage, usage_degraded = _usage_for_row(
                conv.session_usage,
                cost,
                agent_specs.get(conv.agent_id) if conv.agent_id is not None else None,
                conv.sub_agent_name,
            )
            degraded.extend(usage_degraded)
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
            if pending_count is None:
                # The row says it can't tell; the tallies must say the same.
                degradation.note_floor()
            settled = only_active and status == "idle" and pending_count == 0 and not degraded
            if settled and not _settled_recently(item.updated_at, now, settled_grace_seconds):
                continue
            # A settled row is carried for OBSERVATION only, in its own
            # collection: it is not part of the active view, so it must not
            # be ranked with it, counted in it, or take up room in it.
            (settled_rows if settled else rows).append(
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
                    usage=usage,
                    degraded=degraded,
                )
            )
        rows.sort(key=_row_rank)
        # Counts describe every matching session, including the ones the
        # row cap dropped — a headline that shrank with the page would be
        # the same lie as an empty feed. ``partial`` comes straight off the
        # degradation accumulator: anything the server failed to work out
        # already went through it, so the counts cannot claim to be a total
        # while some part of the answer is missing.
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
            partial=degradation.partial,
        )
        # Most recent first: with more settlements than the cota can carry,
        # the ones a poller has not seen yet are the recent ones.
        settled_rows.sort(key=lambda row: -(row.updated_at or 0))
        return MonitorFeedResponse(
            generated_at=now,
            host_id=host_id,
            sessions=rows[:_MAX_ROWS],
            counts=counts,
            truncated=page.has_more or counts.omitted > 0,
            settled=settled_rows[:_MAX_SETTLED_ROWS],
            # The settled collection states its OWN completeness. A consumer
            # watching for completions has to know whether it saw all of them,
            # and this must never be inferred from ``truncated``, which is
            # about the active view.
            settled_omitted=max(0, len(settled_rows) - _MAX_SETTLED_ROWS),
            degraded=list(degradation.slugs),
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
        settled_grace_seconds: int = Query(default=0, ge=0, le=_MAX_SETTLED_GRACE_SECONDS),
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
        :param settled_grace_seconds: Widens ``only_active`` to also carry
            sessions that settled within this many seconds, e.g. ``120``.
            Ignored when ``only_active`` is ``False``. It exists for
            pollers that must WITNESS a session finishing: without it a
            finished session simply stops appearing, and a caller cannot
            tell "it ended" from "the row cap dropped it" or "it was
            deleted" — the same ambiguity that makes an absent row unsafe
            to act on. Bounded (``0``–``3600``) and defaulted to ``0``, so
            the plain view is unchanged and the window cannot be widened
            into a history page. The rows come back in ``settled``, NOT
            in ``sessions``: they are not part of the active view, so
            they neither move ``counts`` nor take room from it, and
            ``settled_omitted`` states whether that collection is
            complete on its own terms.
        :returns: A :class:`MonitorFeedResponse`. Any failure inside the
            feed degrades into explicit markers on the payload rather
            than an error status, so the contract is ``200`` plus
            ``degraded`` / ``counts.partial``, or one of the typed
            ``400`` / ``404`` / ``503`` above. The route does not emit
            ``500``: a monitor that answers "server error" and one that
            answers "nothing needs you" are equally useless to the human
            watching it, so a crash is reported as an unreadable feed
            (and logged with a traceback for whoever owns the bug).
        """
        degradation = _Degradation()
        try:
            # Inside the boundary: auth resolution is code like any other
            # and can fail unexpectedly. Its deliberate verdicts (401 /
            # 403) pass through below; a crash in it must not become the
            # 500 this route promises never to emit.
            #
            # Fail closed on auth: ``accessible_by=None`` means "no ACL
            # filter", so an unauthenticated caller slipping through as
            # ``None`` would monitor every user's sessions.
            user_id = require_user(request, auth_provider)
            if host_id is not None:
                await _validate_host(host_id, user_id)
            return await _build_feed(
                user_id, host_id, only_active, settled_grace_seconds, degradation
            )
        except OmniCraftError as exc:
            # Deliberate verdicts about the request itself — unauthorized,
            # forbidden, a bad or unverifiable host — are the caller's
            # business and keep their status. An OmniCraftError that maps
            # to 5xx is a failure to answer, not an answer, so it degrades
            # like any other crash rather than leaking the 500.
            if exc.http_status < 500 or exc.code == ErrorCode.HOST_UNVERIFIABLE:
                raise
            _logger.exception("Monitor feed build failed")
            return _unreadable_feed(host_id, degradation)
        except Exception:
            _logger.exception("Monitor feed build failed")
            return _unreadable_feed(host_id, degradation)

    return router
