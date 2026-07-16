"""Background sweep that archives orphan unbound sessions.

A session created via ``POST /v1/sessions`` without a ``host_id`` is
legitimately unbound until a caller finishes the create-then-bind flow
with ``PATCH /v1/sessions/{id}`` (the late host-bind path in
``omnicraft.server.routes.sessions.update_session``). This module
archives sessions that sat unbound (``host_id`` and ``runner_id`` both
``None``) with no events — no item append, which is what bumps
``conversations.updated_at`` — past the configured TTL.

Archival reuses the existing ``conversations.archived`` column, so it
is reversible the same way any other archived session is: it shows up
under ``include_archived=true`` and can be unarchived via
``PATCH /v1/sessions/{id}`` with ``{"archived": false}`` — or by
simply completing the late host-bind, which clears ``archived`` as
part of the same write (see ``ConversationStore.set_host_id``).

The select (:meth:`ConversationStore.list_stale_unbound_conversations`)
and the write (:meth:`ConversationStore.archive_if_still_stale_unbound`)
are two separate calls, so a bind or event append can land on a row
between them. The write re-checks every predicate itself, so that race
degrades to "skip this row" rather than archiving a session that just
became legitimate.
"""

from __future__ import annotations

import logging

from omnicraft.db.utils import now_epoch
from omnicraft.server.server_config import unbound_session_ttl_hours
from omnicraft.stores.conversation_store import ConversationStore

logger = logging.getLogger(__name__)


def sweep_unbound_sessions(conversation_store: ConversationStore) -> int:
    """Archive every unbound session past the configured TTL.

    Each candidate row is archived through
    :meth:`ConversationStore.archive_if_still_stale_unbound`, which
    re-checks the same cutoff and unbound predicate at write time —
    a row that got bound or received an event between the select and
    this write is skipped rather than archived.

    :param conversation_store: Store to sweep.
    :returns: Number of sessions actually archived this tick (may be
        less than the number of candidates selected, when a race
        made one of them legitimate in between).
    """
    cutoff = now_epoch() - unbound_session_ttl_hours() * 3600
    stale = conversation_store.list_stale_unbound_conversations(cutoff)
    archived_count = sum(
        1 for conv in stale if conversation_store.archive_if_still_stale_unbound(conv.id, cutoff)
    )
    if archived_count:
        logger.info("unbound-session-sweep: archived %d orphan session(s)", archived_count)
    return archived_count
