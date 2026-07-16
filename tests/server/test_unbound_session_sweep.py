"""Tests for the unbound-session TTL sweep (:mod:`omnicraft.server.unbound_session_sweep`)."""

from __future__ import annotations

import pytest

from omnicraft.entities import MessageData, NewConversationItem
from omnicraft.server import unbound_session_sweep
from omnicraft.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


@pytest.fixture()
def conversation_store(db_uri: str) -> SqlAlchemyConversationStore:
    """:returns: A SqlAlchemyConversationStore backed by the test database."""
    return SqlAlchemyConversationStore(db_uri)


def _freeze_creation_clock(monkeypatch: pytest.MonkeyPatch, ts: int) -> None:
    """Pin the conversation store's clock so created_at/updated_at are deterministic."""
    monkeypatch.setattr(
        "omnicraft.stores.conversation_store.sqlalchemy_store.now_epoch",
        lambda: ts,
    )


def _freeze_sweep_clock(monkeypatch: pytest.MonkeyPatch, ts: int, ttl_hours: int) -> None:
    """Pin the sweep's "now" and configured TTL independently of the store's clock."""
    monkeypatch.setattr(unbound_session_sweep, "now_epoch", lambda: ts)
    monkeypatch.setattr(unbound_session_sweep, "unbound_session_ttl_hours", lambda: ttl_hours)


def test_sweep_archives_orphan_past_ttl(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unbound session with no events for longer than the TTL is archived."""
    _freeze_creation_clock(monkeypatch, 0)
    conv = conversation_store.create_conversation(agent_id="ag_orphan")

    _freeze_sweep_clock(monkeypatch, 24 * 3600 + 1, ttl_hours=24)
    archived_count = unbound_session_sweep.sweep_unbound_sessions(conversation_store)

    assert archived_count == 1
    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.archived is True


def test_sweep_leaves_session_within_ttl_untouched(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session still inside the (generous, hours-scale) TTL window is left alone.

    Create-then-bind can legitimately sit idle for a while before the caller
    finishes picking a host, so a fresh unbound session must never be swept.
    """
    _freeze_creation_clock(monkeypatch, 0)
    conv = conversation_store.create_conversation(agent_id="ag_recent")

    _freeze_sweep_clock(monkeypatch, 3600, ttl_hours=24)  # 1h old, TTL is 24h
    archived_count = unbound_session_sweep.sweep_unbound_sessions(conversation_store)

    assert archived_count == 0
    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.archived is False


def test_sweep_leaves_session_that_bound_before_sweep_ran(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session that completed create-then-bind is never archived, even past
    the TTL clock — the sweep must not disturb a legitimately-used session."""
    _freeze_creation_clock(monkeypatch, 0)
    conv = conversation_store.create_conversation(agent_id="ag_bound_late")

    # Late host-bind, mirroring PATCH /v1/sessions/{id}'s create-then-bind path
    # (update_session in omnicraft/server/routes/sessions.py).
    conversation_store.set_host_id(conv.id, "host_a", workspace="/tmp/ws")

    _freeze_sweep_clock(monkeypatch, 24 * 3600 + 1, ttl_hours=24)
    archived_count = unbound_session_sweep.sweep_unbound_sessions(conversation_store)

    assert archived_count == 0
    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.archived is False
    assert fetched.host_id == "host_a"


def test_sweep_archival_is_reversible_via_unarchive(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A swept session unarchives like any other archived session — archival
    never hard-deletes the row, and reversal never fabricates a binding the
    session didn't actually have."""
    _freeze_creation_clock(monkeypatch, 0)
    conv = conversation_store.create_conversation(agent_id="ag_reversible")

    _freeze_sweep_clock(monkeypatch, 24 * 3600 + 1, ttl_hours=24)
    unbound_session_sweep.sweep_unbound_sessions(conversation_store)
    archived = conversation_store.get_conversation(conv.id)
    assert archived is not None
    assert archived.archived is True

    unarchived = conversation_store.update_conversation(conv.id, archived=False)
    assert unarchived is not None
    assert unarchived.archived is False
    assert unarchived.host_id is None
    assert unarchived.runner_id is None


def test_late_bind_against_an_archived_session_succeeds_and_unarchives_it(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The create-then-bind flow (PATCH /v1/sessions/{id} -> set_host_id)
    works directly against a session the sweep already archived — the caller
    doesn't have to separately unarchive first. Binding is itself the signal
    that the session is back in active use, so the bind clears ``archived``
    as part of the same write.
    """
    _freeze_creation_clock(monkeypatch, 0)
    conv = conversation_store.create_conversation(agent_id="ag_late_bind")

    _freeze_sweep_clock(monkeypatch, 24 * 3600 + 1, ttl_hours=24)
    archived_count = unbound_session_sweep.sweep_unbound_sessions(conversation_store)
    assert archived_count == 1
    still_archived = conversation_store.get_conversation(conv.id)
    assert still_archived is not None
    assert still_archived.archived is True

    # Bind directly against the still-archived row — no unarchive step first.
    bound = conversation_store.set_host_id(conv.id, "host_b", workspace="/tmp/ws2")
    assert bound.host_id == "host_b"
    assert bound.archived is False

    refetched = conversation_store.get_conversation(conv.id)
    assert refetched is not None
    assert refetched.host_id == "host_b"
    assert refetched.archived is False


def test_set_host_id_never_resurrects_a_manually_archived_unbound_row(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain unbound session a human archived by hand — never touched by
    the sweep — must stay archived on its first host bind. Only rows the
    sweep itself archived (carrying its provenance marker) get
    auto-unarchived; ``archived`` alone can't prove that on its own.
    """
    _freeze_creation_clock(monkeypatch, 0)
    conv = conversation_store.create_conversation(agent_id="ag_manually_archived")
    conversation_store.update_conversation(conv.id, archived=True)
    archived = conversation_store.get_conversation(conv.id)
    assert archived is not None
    assert archived.archived is True
    assert archived.archived_reason is None

    bound = conversation_store.set_host_id(conv.id, "host_e", workspace="/tmp/ws5")
    assert bound.host_id == "host_e"
    assert bound.archived is True, "a manual archive must never be auto-cleared by a bind"

    refetched = conversation_store.get_conversation(conv.id)
    assert refetched is not None
    assert refetched.archived is True


def test_set_host_id_never_resurrects_a_manually_archived_row_with_runner_set(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session with ``runner_id`` set (so the sweep could NEVER have
    archived it — ``archive_if_still_stale_unbound`` requires
    ``runner_id IS NULL``) but manually archived while ``host_id`` was
    still NULL must stay archived on its first host bind. This is the
    exact hole a bare "host_id was NULL" check leaves open: it looks
    like a first bind, but the sweep could not possibly be the one
    that archived this row.
    """
    _freeze_creation_clock(monkeypatch, 0)
    conv = conversation_store.create_conversation(agent_id="ag_runner_then_archived")
    conversation_store.set_runner_id(conv.id, "runner_x")
    conversation_store.update_conversation(conv.id, archived=True)
    archived = conversation_store.get_conversation(conv.id)
    assert archived is not None
    assert archived.archived is True
    assert archived.archived_reason is None
    assert archived.host_id is None

    bound = conversation_store.set_host_id(conv.id, "host_d", workspace="/tmp/ws4")
    assert bound.host_id == "host_d"
    assert bound.archived is True, "sweep could never have archived a runner-bound row"

    refetched = conversation_store.get_conversation(conv.id)
    assert refetched is not None
    assert refetched.archived is True


def test_set_host_id_loses_race_against_a_concurrent_manual_archive(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manual archive that clears the sweep marker between the sweep's
    archive and a later first bind must win — the bind must not resurrect
    the session using a decision made before that manual write landed.

    This is the read-then-write TOCTOU set_host_id used to have: a
    version that reads ``archived_reason`` first and only *then* writes
    ``archived=False`` would use its stale read here and wrongly
    unarchive. The current implementation folds the "is this still a
    first bind with the marker intact" check into the same ``UPDATE``
    statement that writes ``host_id``, so there is no separate read to
    go stale — it re-evaluates the predicate against the row exactly as
    it stands when the write executes.
    """
    _freeze_creation_clock(monkeypatch, 0)
    conv = conversation_store.create_conversation(agent_id="ag_race_manual_archive")

    # The sweep archives the orphan — marker present.
    cutoff = 24 * 3600 + 1
    archived = conversation_store.archive_if_still_stale_unbound(conv.id, cutoff)
    assert archived is True
    sweep_archived = conversation_store.get_conversation(conv.id)
    assert sweep_archived is not None
    assert sweep_archived.archived_reason is not None

    # A concurrent manual archive-state change lands next (simulating a
    # write that interleaves between the sweep and the eventual bind) —
    # this clears the marker even though archived stays True.
    conversation_store.update_conversation(conv.id, archived=True)
    reaffirmed = conversation_store.get_conversation(conv.id)
    assert reaffirmed is not None
    assert reaffirmed.archived is True
    assert reaffirmed.archived_reason is None

    # The first bind arrives after that — host_id was still NULL, so a
    # naive read-then-write implementation would see "first bind" and
    # (using a stale pre-manual-write read) wrongly unarchive. The
    # atomic implementation re-checks the marker at write time and must
    # leave the session archived.
    bound = conversation_store.set_host_id(conv.id, "host_race", workspace="/tmp/ws_race")
    assert bound.host_id == "host_race"
    assert bound.archived is True, (
        "a first bind must not resurrect a session whose sweep marker "
        "was cleared by a concurrent manual write"
    )

    refetched = conversation_store.get_conversation(conv.id)
    assert refetched is not None
    assert refetched.archived is True


# ── TOCTOU: a race between selection and the archive write ──────────


def test_archive_write_skips_a_row_bound_after_selection(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session host-bound between the sweep's SELECT and its archive
    UPDATE must not be archived — the write re-checks the unbound predicate
    itself rather than trusting the stale read."""
    _freeze_creation_clock(monkeypatch, 0)
    conv = conversation_store.create_conversation(agent_id="ag_race_bind")

    cutoff = 24 * 3600 + 1
    stale = conversation_store.list_stale_unbound_conversations(cutoff)
    assert [c.id for c in stale] == [conv.id]

    # A concurrent late bind lands after the read, before the write.
    conversation_store.set_host_id(conv.id, "host_c", workspace="/tmp/ws3")

    archived = conversation_store.archive_if_still_stale_unbound(conv.id, cutoff)
    assert archived is False

    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.archived is False
    assert fetched.host_id == "host_c"


def test_archive_write_skips_a_row_that_received_an_event_after_selection(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session that receives an event (bumping updated_at) between the
    sweep's SELECT and its archive UPDATE must not be archived."""
    _freeze_creation_clock(monkeypatch, 0)
    conv = conversation_store.create_conversation(agent_id="ag_race_event")

    cutoff = 24 * 3600 + 1
    stale = conversation_store.list_stale_unbound_conversations(cutoff)
    assert [c.id for c in stale] == [conv.id]

    # An event lands after the read (still unbound, but no longer idle),
    # bumping updated_at past the cutoff the read used.
    _freeze_creation_clock(monkeypatch, cutoff)
    conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_race",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
            ),
        ],
    )

    archived = conversation_store.archive_if_still_stale_unbound(conv.id, cutoff)
    assert archived is False

    fetched = conversation_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.archived is False
