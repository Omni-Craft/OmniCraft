"""Tests for ``GET /v1/monitor/sessions`` — the shared monitor feed.

The feed is what every monitor surface reads, so the properties under
test are the ones a surface cannot recover on its own: ``waiting`` is
never collapsed into ``running``, "blocked on a human" comes from the
elicitation index (not a tool-call flag), attention outranks recency and
survives the row cap, an absent answer never renders as a clean one, and
listing N sessions stays a fixed number of store calls.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from omnicraft.errors import OmniCraftError
from omnicraft.runtime import pending_elicitations
from omnicraft.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnicraft.server.routes import monitor as monitor_module
from omnicraft.server.routes import sessions as sessions_module
from omnicraft.server.routes.monitor import create_monitor_router
from omnicraft.server.routes.sessions import SessionLiveness
from omnicraft.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnicraft.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnicraft.stores.host_store import Host
from omnicraft.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

ALICE = "alice@example.com"
BOB = "bob@example.com"
PROJECT_LABEL_KEY = "omni_project"


@pytest.fixture(autouse=True)
def _clean_module_state() -> Any:
    """Reset the process-wide caches the feed reads between tests."""
    sessions_module._session_status_cache.clear()
    pending_elicitations.reset_for_tests()
    yield
    sessions_module._session_status_cache.clear()
    pending_elicitations.reset_for_tests()


def _live(ids: list[str]) -> dict[str, SessionLiveness]:
    return {sid: SessionLiveness(runner_online=True, host_online=True) for sid in ids}


class _FakeHostStore:
    """Minimal host store: just the ownership lookup the filter validates."""

    def __init__(self, hosts: dict[str, str]) -> None:
        self._hosts = hosts

    def get_host(self, host_id: str) -> Host | None:
        owner = self._hosts.get(host_id)
        if owner is None:
            return None
        return Host(
            host_id=host_id,
            name=host_id,
            owner=owner,
            status="online",
            created_at=0,
            updated_at=0,
        )


def _app(
    db_uri: str,
    liveness_lookup: Any = _live,
    *,
    host_store: Any = None,
    conversation_store: Any = None,
    agent_store: Any = None,
    permission_store: Any = -1,
) -> FastAPI:
    """Header-auth app mounting only the monitor router at ``/v1``."""
    app = FastAPI()

    @app.exception_handler(OmniCraftError)
    async def _handle(request: Request, exc: OmniCraftError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_monitor_router(
            conversation_store or SqlAlchemyConversationStore(db_uri),
            agent_store or SqlAlchemyAgentStore(db_uri),
            auth_provider=UnifiedAuthProvider(source="header"),
            permission_store=(
                SqlAlchemyPermissionStore(db_uri) if permission_store == -1 else permission_store
            ),
            liveness_lookup=liveness_lookup,
            host_store=host_store,
        ),
        prefix="/v1",
    )
    return app


def _seed(
    db_uri: str,
    *,
    title: str,
    host_id: str | None = None,
    runner_id: str | None = None,
    project: str | None = None,
    parent_id: str | None = None,
    kind: str = "default",
    owner: str = ALICE,
) -> str:
    """Create a session owned by *owner*; returns its id.

    Sessions are seeded with a host binding by default so they count as
    "dispatched" — an undispatched session is the one case where a cache
    miss really does mean idle, and it has its own test.
    """
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    perms = SqlAlchemyPermissionStore(db_uri)
    if agent_store.get("ag_test") is None:
        agent_store.create(agent_id="ag_test", name="test-agent", bundle_location="ag_test/bundle")
    conv = conv_store.create_conversation(
        kind=kind,
        title=title,
        agent_id="ag_test",
        host_id=host_id if host_id is not None or parent_id is not None else "host_a",
        runner_id=runner_id,
        parent_conversation_id=parent_id,
        workspace="/tmp/ws",
    )
    if project is not None:
        conv_store.set_labels(conv.id, {PROJECT_LABEL_KEY: project})
    perms.ensure_user(owner)
    perms.grant(owner, conv.id, LEVEL_OWNER)
    return conv.id


def _get(app: FastAPI, query: str = "", *, user: str = ALICE) -> dict[str, Any]:
    resp = TestClient(app).get(f"/v1/monitor/sessions{query}", headers={"X-Forwarded-Email": user})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _elicitation(
    elicitation_id: str, message: str = "Approve running 'rm -rf'?"
) -> dict[str, Any]:
    return {
        "type": "response.elicitation_request",
        "elicitation_id": elicitation_id,
        "params": {
            "mode": "form",
            "message": message,
            "policy_name": "approve_shell_commands",
        },
    }


# ── Shape ──────────────────────────────────────────────────────────


def test_feed_shape_and_counts(db_uri: str) -> None:
    """The payload carries the documented envelope and per-row fields."""
    running = _seed(db_uri, title="Running", host_id="host_a", project="Ship it")
    sessions_module._session_status_cache[running] = "running"
    idle = _seed(db_uri, title="Idle", host_id="host_a")
    sessions_module._session_status_cache[idle] = "idle"

    body = _get(_app(db_uri))

    assert set(body) == {
        "generated_at",
        "host_id",
        "sessions",
        "counts",
        "truncated",
        "degraded",
    }
    assert body["host_id"] is None
    assert body["degraded"] == []
    assert body["truncated"] is False
    # only_active defaults to True, so the idle session is not a row.
    assert [row["session_id"] for row in body["sessions"]] == [running]
    row = body["sessions"][0]
    assert row["status"] == "running"
    assert row["agent_name"] == "test-agent"
    assert row["title"] == "Running"
    assert row["project"] == "Ship it"
    assert row["workspace"] == "/tmp/ws"
    assert row["pending_elicitations_count"] == 0
    assert row["pending_elicitation"] is None
    assert row["runner_online"] is True
    assert row["host_online"] is True
    assert isinstance(row["updated_at"], int)
    # No cost has been recorded — null, never a made-up zero.
    assert row["cost_usd"] is None
    assert row["degraded"] == []
    assert body["counts"] == {
        "active": 1,
        "awaiting": 0,
        "unknown": 0,
        "omitted": 0,
        "partial": False,
    }


# ── Status granularity ─────────────────────────────────────────────


def test_waiting_is_preserved_not_collapsed_to_running(db_uri: str) -> None:
    """The session-list shape collapses ``waiting``; the feed must not."""
    waiting = _seed(db_uri, title="Waiting")
    sessions_module._session_status_cache[waiting] = "waiting"

    body = _get(_app(db_uri))

    assert [row["status"] for row in body["sessions"]] == ["waiting"]
    # And the list shape it reuses would have said "running".
    assert sessions_module._session_status_from_cache(waiting) == "running"


def test_blocked_child_rolls_up_as_waiting(db_uri: str) -> None:
    """A parent whose sub-agent is blocked reads ``waiting``, not ``running``."""
    parent = _seed(db_uri, title="Parent")
    child = _seed(db_uri, title="Child", parent_id=parent, kind="sub_agent")
    sessions_module._session_status_cache[parent] = "idle"
    sessions_module._session_status_cache[child] = "waiting"

    body = _get(_app(db_uri))

    rows = {row["session_id"]: row for row in body["sessions"]}
    # Sub-agent children are not rows of their own; the parent carries them.
    assert set(rows) == {parent}
    assert rows[parent]["status"] == "waiting"


def test_running_child_rolls_up_as_running(db_uri: str) -> None:
    """An idle parent with a running child still reads ``running``."""
    parent = _seed(db_uri, title="Parent")
    child = _seed(db_uri, title="Child", parent_id=parent, kind="sub_agent")
    sessions_module._session_status_cache[parent] = "idle"
    sessions_module._session_status_cache[child] = "running"

    body = _get(_app(db_uri))

    assert [row["status"] for row in body["sessions"]] == ["running"]


def test_launching_stays_distinct_from_idle(db_uri: str) -> None:
    """``launching`` is real progress; collapsing it to idle would hide it."""
    launching = _seed(db_uri, title="Launching")
    sessions_module._session_status_cache[launching] = "launching"

    body = _get(_app(db_uri))

    assert [row["status"] for row in body["sessions"]] == ["launching"]
    assert body["counts"]["active"] == 1


def test_missing_status_for_a_dispatched_session_is_unknown_not_idle(db_uri: str) -> None:
    """A cache miss is ignorance: after a restart a busy session looks
    exactly like a quiet one, so it must not be reported as idle — nor
    silently dropped by the default ``only_active`` view."""
    dispatched = _seed(db_uri, title="Dispatched", host_id="host_a", runner_id="runner_1")
    assert dispatched not in sessions_module._session_status_cache

    body = _get(_app(db_uri))

    assert [row["session_id"] for row in body["sessions"]] == [dispatched]
    row = body["sessions"][0]
    assert row["status"] == "unknown"
    assert "status_unknown" in row["degraded"]
    assert body["counts"]["unknown"] == 1
    # Unknown is not "active" — the client shows it as its own bucket.
    assert body["counts"]["active"] == 0


def test_never_dispatched_session_with_no_status_is_idle(db_uri: str) -> None:
    """The boundary: no runner and no host means it never ran anywhere, so
    ``idle`` is read off the row rather than assumed from silence."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_store = SqlAlchemyAgentStore(db_uri)
    perms = SqlAlchemyPermissionStore(db_uri)
    if agent_store.get("ag_test") is None:
        agent_store.create(agent_id="ag_test", name="test-agent", bundle_location="ag_test/bundle")
    conv = conv_store.create_conversation(title="Fresh", agent_id="ag_test")
    perms.ensure_user(ALICE)
    perms.grant(ALICE, conv.id, LEVEL_OWNER)

    assert _get(_app(db_uri))["sessions"] == []
    body = _get(_app(db_uri), "?only_active=false")
    assert [row["status"] for row in body["sessions"]] == ["idle"]
    assert body["sessions"][0]["degraded"] == []


def test_unreadable_status_is_kept_and_flagged(db_uri: str) -> None:
    """A status value this server doesn't understand must not vanish as idle."""
    weird = _seed(db_uri, title="Weird")
    sessions_module._session_status_cache[weird] = "quantum"

    body = _get(_app(db_uri))

    row = body["sessions"][0]
    assert row["session_id"] == weird
    assert row["status"] == "unknown"
    assert "status_unreadable" in row["degraded"]


def test_failed_is_reported_under_both_only_active_values(db_uri: str) -> None:
    """A failed session is exactly what a monitor must not hide."""
    failed = _seed(db_uri, title="Failed")
    sessions_module._session_status_cache[failed] = "failed"

    for query in ("", "?only_active=false"):
        body = _get(_app(db_uri), query)
        assert [row["status"] for row in body["sessions"]] == ["failed"], query
        assert body["counts"]["active"] == 1, query


# ── Awaiting a human ───────────────────────────────────────────────


def test_awaiting_counted_from_pending_elicitations(db_uri: str) -> None:
    """ "Needs a human" comes from the elicitation index, with a summary."""
    blocked = _seed(db_uri, title="Blocked")
    sessions_module._session_status_cache[blocked] = "idle"
    pending_elicitations.record_publish(blocked, _elicitation("elicit_1"))

    body = _get(_app(db_uri))

    row = body["sessions"][0]
    assert row["pending_elicitations_count"] == 1
    assert row["pending_elicitation"] == {
        "id": "elicit_1",
        "session_id": blocked,
        "kind": "approve_shell_commands",
        "summary": "Approve running 'rm -rf'?",
    }
    assert body["counts"]["awaiting"] == 1
    assert row["degraded"] == []


def test_child_elicitation_surfaces_on_the_parent_row(db_uri: str) -> None:
    """A child's prompt is acted on from the parent row, so it counts there."""
    parent = _seed(db_uri, title="Parent")
    child = _seed(db_uri, title="Child", parent_id=parent, kind="sub_agent")
    sessions_module._session_status_cache[parent] = "idle"
    sessions_module._session_status_cache[child] = "idle"
    pending_elicitations.record_publish(child, _elicitation("elicit_child"))

    body = _get(_app(db_uri))

    row = body["sessions"][0]
    assert row["session_id"] == parent
    assert row["pending_elicitations_count"] == 1
    # The verdict still belongs to the child's resolve endpoint.
    assert row["pending_elicitation"]["session_id"] == child


def test_idle_session_with_pending_prompt_survives_only_active(db_uri: str) -> None:
    """An idle session blocked on a human is exactly what a monitor is for."""
    blocked = _seed(db_uri, title="Idle but blocked")
    sessions_module._session_status_cache[blocked] = "idle"
    pending_elicitations.record_publish(blocked, _elicitation("elicit_2"))

    body = _get(_app(db_uri))

    assert [row["session_id"] for row in body["sessions"]] == [blocked]
    # Documented split: blocked-and-doing-nothing is awaiting, not active.
    assert body["counts"]["active"] == 0
    assert body["counts"]["awaiting"] == 1


def test_pending_count_without_readable_payload_degrades(db_uri: str) -> None:
    """A blocked session whose payload can't be read still reads as blocked."""
    blocked = _seed(db_uri, title="Blocked")
    sessions_module._session_status_cache[blocked] = "idle"
    pending_elicitations.record_publish(blocked, _elicitation("elicit_3"))

    app = _app(db_uri)
    original = pending_elicitations.snapshot_for
    pending_elicitations.snapshot_for = lambda _sid: []  # type: ignore[assignment]
    try:
        body = _get(app)
    finally:
        pending_elicitations.snapshot_for = original  # type: ignore[assignment]

    row = body["sessions"][0]
    assert row["pending_elicitations_count"] == 1
    assert row["pending_elicitation"] is None
    assert "pending_elicitation_unreadable" in row["degraded"]
    assert body["counts"]["awaiting"] == 1


def test_malformed_pending_count_is_not_read_as_zero(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A junk count means "can't tell", which is closer to blocked than free."""
    blocked = _seed(db_uri, title="Blocked")
    sessions_module._session_status_cache[blocked] = "idle"
    monkeypatch.setattr(pending_elicitations, "counts_for", lambda ids: dict.fromkeys(ids, "two"))

    body = _get(_app(db_uri))

    row = body["sessions"][0]
    assert row["pending_elicitations_count"] >= 1
    assert "pending_elicitation_unreadable" in row["degraded"]
    assert body["counts"]["awaiting"] == 1


# ── Ordering, cap and truncation ───────────────────────────────────


def test_rows_are_ordered_by_need_for_a_human_not_recency(db_uri: str) -> None:
    """Recency is the wrong primary key: the session stuck the longest is
    the one that updated least recently."""
    blocked = _seed(db_uri, title="Blocked")
    sessions_module._session_status_cache[blocked] = "idle"
    pending_elicitations.record_publish(blocked, _elicitation("elicit_rank"))
    waiting = _seed(db_uri, title="Waiting")
    sessions_module._session_status_cache[waiting] = "waiting"
    failed = _seed(db_uri, title="Failed")
    sessions_module._session_status_cache[failed] = "failed"
    running = _seed(db_uri, title="Running")
    sessions_module._session_status_cache[running] = "running"

    body = _get(_app(db_uri))

    # Seeded oldest-first, so pure updated_at DESC would invert this.
    assert [row["session_id"] for row in body["sessions"]] == [
        blocked,
        waiting,
        failed,
        running,
    ]


def test_row_cap_drops_the_least_urgent_and_counts_stay_whole(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cap applied before ranking would drop the blocked session; the
    headline counts must still describe everything that matched."""
    waiting = _seed(db_uri, title="Waiting")
    sessions_module._session_status_cache[waiting] = "waiting"
    for index in range(3):
        running = _seed(db_uri, title=f"Running {index}")
        sessions_module._session_status_cache[running] = "running"
    monkeypatch.setattr(monitor_module, "_MAX_ROWS", 1)

    body = _get(_app(db_uri))

    assert [row["session_id"] for row in body["sessions"]] == [waiting]
    assert body["truncated"] is True
    assert body["counts"]["active"] == 4
    assert body["counts"]["omitted"] == 3


def test_blocked_session_outside_the_scan_is_still_reported(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the feed: a session blocked long before the scan
    window must not be answered as "nothing needs you"."""
    blocked = _seed(db_uri, title="Blocked long ago")
    sessions_module._session_status_cache[blocked] = "idle"
    pending_elicitations.record_publish(blocked, _elicitation("elicit_old"))
    for index in range(3):
        newer = _seed(db_uri, title=f"Newer {index}")
        sessions_module._session_status_cache[newer] = "idle"
    # Only the newest session fits the scan; the blocked one is out of it.
    monkeypatch.setattr(monitor_module, "_SCAN_LIMIT", 1)

    body = _get(_app(db_uri))

    assert [row["session_id"] for row in body["sessions"]] == [blocked]
    assert body["counts"]["awaiting"] == 1
    assert "scan_truncated" in body["degraded"]


def test_waiting_session_outside_the_scan_is_still_reported(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same rescue for a session parked in ``waiting``."""
    waiting = _seed(db_uri, title="Waiting long ago")
    sessions_module._session_status_cache[waiting] = "waiting"
    for index in range(3):
        newer = _seed(db_uri, title=f"Newer {index}")
        sessions_module._session_status_cache[newer] = "idle"
    monkeypatch.setattr(monitor_module, "_SCAN_LIMIT", 1)

    body = _get(_app(db_uri))

    assert [row["session_id"] for row in body["sessions"]] == [waiting]


def test_rescued_session_of_another_user_is_not_leaked(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rescue path reaches around the ACL-scoped scan, so it re-checks
    the grants itself."""
    bobs = _seed(db_uri, title="Bob's blocked session", owner=BOB)
    pending_elicitations.record_publish(bobs, _elicitation("elicit_bob"))
    mine = _seed(db_uri, title="Mine")
    sessions_module._session_status_cache[mine] = "running"
    monkeypatch.setattr(monitor_module, "_SCAN_LIMIT", 1)

    body = _get(_app(db_uri))

    assert [row["session_id"] for row in body["sessions"]] == [mine]


def test_attention_beyond_the_rescue_cap_is_counted_not_dropped(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep is bounded, but what it can't carry must still be
    reported: a blocked session either lands in the response or shows up
    in ``counts.omitted`` with the tallies marked partial. Vanishing is
    the one thing it may not do."""
    blocked = []
    for index in range(3):
        sid = _seed(db_uri, title=f"Blocked {index}")
        sessions_module._session_status_cache[sid] = "idle"
        pending_elicitations.record_publish(sid, _elicitation(f"elicit_{index}"))
        blocked.append(sid)
    newest = _seed(db_uri, title="Newest")
    sessions_module._session_status_cache[newest] = "idle"
    # Only the newest session is scanned; all three blocked ones are out,
    # and the sweep can only carry one of them.
    monkeypatch.setattr(monitor_module, "_SCAN_LIMIT", 1)
    monkeypatch.setattr(monitor_module, "_RESCUE_MAX", 1)

    body = _get(_app(db_uri))

    carried = {row["session_id"] for row in body["sessions"]}
    assert len(carried & set(blocked)) == 1
    assert body["counts"]["omitted"] == 2
    assert body["counts"]["partial"] is True
    assert body["truncated"] is True
    assert "attention_rescue_truncated" in body["degraded"]


def test_unresolvable_attention_sweep_is_counted(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep that fails outright still reports how many sessions it
    could not account for."""
    blocked = _seed(db_uri, title="Blocked")
    sessions_module._session_status_cache[blocked] = "idle"
    pending_elicitations.record_publish(blocked, _elicitation("elicit_x"))
    newest = _seed(db_uri, title="Newest")
    sessions_module._session_status_cache[newest] = "idle"
    monkeypatch.setattr(monitor_module, "_SCAN_LIMIT", 1)

    class _BrokenLookupStore(SqlAlchemyConversationStore):
        def get_conversation(self, *args: Any, **kwargs: Any) -> Any:
            raise SQLAlchemyError("conversations table unreachable")

    body = _get(_app(db_uri, conversation_store=_BrokenLookupStore(db_uri)))

    assert blocked not in {row["session_id"] for row in body["sessions"]}
    assert body["counts"]["omitted"] == 1
    assert body["counts"]["partial"] is True
    assert "attention_rescue_unavailable" in body["degraded"]


def test_cut_scan_marks_the_counts_as_a_floor(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tallies over a cut scan are an undercount and have to say so."""
    for index in range(3):
        sid = _seed(db_uri, title=f"Running {index}")
        sessions_module._session_status_cache[sid] = "running"
    monkeypatch.setattr(monitor_module, "_SCAN_LIMIT", 1)

    body = _get(_app(db_uri))

    assert body["counts"]["active"] == 1
    assert body["counts"]["partial"] is True
    assert body["truncated"] is True
    assert "scan_truncated" in body["degraded"]


def test_unreadable_prompt_index_leaves_the_count_unknown_not_zero(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prompt index that can't be read must not publish every row as
    "0 prompts outstanding" — that is the all-clear this feed exists to
    never fake."""
    session = _seed(db_uri, title="Maybe blocked")
    sessions_module._session_status_cache[session] = "idle"

    def _boom(ids: list[str]) -> dict[str, int]:
        raise RuntimeError("index lock poisoned")

    monkeypatch.setattr(pending_elicitations, "counts_for", _boom)

    body = _get(_app(db_uri))

    row = body["sessions"][0]
    assert row["session_id"] == session
    assert row["pending_elicitations_count"] is None
    assert "pending_elicitations_unknown" in row["degraded"]
    assert body["counts"]["partial"] is True
    assert "pending_elicitations_unavailable" in body["degraded"]


# ── Host filter ────────────────────────────────────────────────────


def test_host_id_filter(db_uri: str) -> None:
    """``host_id`` narrows the feed and is echoed back on the envelope."""
    on_a = _seed(db_uri, title="On A", host_id="host_a")
    on_b = _seed(db_uri, title="On B", host_id="host_b")
    for sid in (on_a, on_b):
        sessions_module._session_status_cache[sid] = "running"
    hosts = _FakeHostStore({"host_a": ALICE, "host_b": ALICE})

    body = _get(_app(db_uri, host_store=hosts), "?host_id=host_a")

    assert body["host_id"] == "host_a"
    assert [row["session_id"] for row in body["sessions"]] == [on_a]


def test_host_filter_is_applied_in_the_store_query(db_uri: str) -> None:
    """Filtering after the page cap silently hides everything the cap cut,
    so the filter has to reach the query."""
    seen: list[Any] = []

    class _SpyStore(SqlAlchemyConversationStore):
        def list_conversations(self, *args: Any, **kwargs: Any) -> Any:
            seen.append(kwargs.get("host_id"))
            return super().list_conversations(*args, **kwargs)

    _seed(db_uri, title="On A", host_id="host_a")
    hosts = _FakeHostStore({"host_a": ALICE})

    _get(
        _app(db_uri, host_store=hosts, conversation_store=_SpyStore(db_uri)),
        "?host_id=host_a",
    )

    assert seen == ["host_a"]


def test_session_on_a_legitimate_host_outside_the_scan_still_appears(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the filter in the query, a host's sessions page on their own —
    other hosts' recent sessions can't crowd them out."""
    on_a = _seed(db_uri, title="On A", host_id="host_a")
    sessions_module._session_status_cache[on_a] = "running"
    for index in range(3):
        newer = _seed(db_uri, title=f"On B {index}", host_id="host_b")
        sessions_module._session_status_cache[newer] = "running"
    monkeypatch.setattr(monitor_module, "_SCAN_LIMIT", 2)
    hosts = _FakeHostStore({"host_a": ALICE, "host_b": ALICE})

    body = _get(_app(db_uri, host_store=hosts), "?host_id=host_a")

    assert [row["session_id"] for row in body["sessions"]] == [on_a]


def test_unknown_host_is_rejected_not_answered_with_an_empty_feed(db_uri: str) -> None:
    """An empty feed for a typo'd host reads as "nothing running there"."""
    _seed(db_uri, title="On A", host_id="host_a")
    app = _app(db_uri, host_store=_FakeHostStore({"host_a": ALICE}))

    resp = TestClient(app).get(
        "/v1/monitor/sessions?host_id=host_nope", headers={"X-Forwarded-Email": ALICE}
    )

    assert resp.status_code == 404


def test_another_users_host_is_rejected(db_uri: str) -> None:
    """Host scoping: a caller can't monitor a host that isn't theirs."""
    app = _app(db_uri, host_store=_FakeHostStore({"host_bob": BOB}))

    resp = TestClient(app).get(
        "/v1/monitor/sessions?host_id=host_bob", headers={"X-Forwarded-Email": ALICE}
    )

    assert resp.status_code == 404


def test_malformed_host_id_is_a_bad_request(db_uri: str) -> None:
    """A blank host filter is a client error, not an empty feed."""
    app = _app(db_uri, host_store=_FakeHostStore({"host_a": ALICE}))

    resp = TestClient(app).get(
        "/v1/monitor/sessions?host_id=%20", headers={"X-Forwarded-Email": ALICE}
    )

    assert resp.status_code == 400


def test_unverifiable_host_filter_is_refused_not_answered(db_uri: str) -> None:
    """With no registry to check against, a typo and a real host look the
    same — so the request is refused instead of answered with a feed that
    could be silently scoped to nothing."""
    on_a = _seed(db_uri, title="On A", host_id="host_a")
    sessions_module._session_status_cache[on_a] = "running"

    resp = TestClient(_app(db_uri)).get(
        "/v1/monitor/sessions?host_id=host_a", headers={"X-Forwarded-Email": ALICE}
    )

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "host_unverifiable"


def test_host_lookup_failure_is_refused_not_a_500(db_uri: str) -> None:
    """An unreachable host registry is a typed 503, not a leaked 500 and
    not a 200 that scopes the feed to nothing."""

    class _BrokenHostStore:
        def get_host(self, host_id: str) -> Any:
            raise SQLAlchemyError("hosts table unreachable")

    app = _app(db_uri, host_store=_BrokenHostStore())

    resp = TestClient(app).get(
        "/v1/monitor/sessions?host_id=host_a", headers={"X-Forwarded-Email": ALICE}
    )

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "host_unverifiable"


# ── Liveness ───────────────────────────────────────────────────────


def test_missing_liveness_reads_unknown_not_offline(db_uri: str) -> None:
    """No liveness lookup wired: say so, don't claim the runner is down."""
    running = _seed(db_uri, title="Running")
    sessions_module._session_status_cache[running] = "running"

    body = _get(_app(db_uri, liveness_lookup=None))

    assert body["degraded"] == ["liveness_unavailable"]
    row = body["sessions"][0]
    assert row["runner_online"] is None
    assert row["host_online"] is None
    assert "liveness_unavailable" in row["degraded"]


def test_failing_liveness_degrades_without_500(db_uri: str) -> None:
    """A liveness lookup that raises must not take the whole feed down."""
    running = _seed(db_uri, title="Running")
    sessions_module._session_status_cache[running] = "running"

    def _boom(ids: list[str]) -> dict[str, SessionLiveness]:
        raise RuntimeError("hosts table unreachable")

    body = _get(_app(db_uri, liveness_lookup=_boom))

    assert body["degraded"] == ["liveness_unavailable"]
    assert body["sessions"][0]["status"] == "running"
    assert body["sessions"][0]["runner_online"] is None


def test_partially_resolved_liveness_is_flagged_per_row(db_uri: str) -> None:
    """A lookup that answers for some sessions and not others must not let
    the unanswered ones read as reachable."""
    running = _seed(db_uri, title="Running", host_id="host_a")
    sessions_module._session_status_cache[running] = "running"

    body = _get(_app(db_uri, liveness_lookup=lambda ids: {}))

    row = body["sessions"][0]
    assert row["runner_online"] is None
    assert row["host_online"] is None
    assert "liveness_partial" in row["degraded"]


def test_unknown_runner_liveness_is_flagged(db_uri: str) -> None:
    """``None`` from the lookup is "not known" and has to be marked as such;
    only a real ``False`` may render as offline."""
    running = _seed(db_uri, title="Running", host_id="host_a")
    sessions_module._session_status_cache[running] = "running"

    def _unknown(ids: list[str]) -> dict[str, SessionLiveness]:
        return {sid: SessionLiveness(runner_online=None, host_online=None) for sid in ids}

    body = _get(_app(db_uri, liveness_lookup=_unknown))

    assert "liveness_partial" in body["sessions"][0]["degraded"]

    def _offline(ids: list[str]) -> dict[str, SessionLiveness]:
        return {sid: SessionLiveness(runner_online=False, host_online=False) for sid in ids}

    body = _get(_app(db_uri, liveness_lookup=_offline))

    assert body["sessions"][0]["degraded"] == []
    assert body["sessions"][0]["runner_online"] is False


# ── Cost ───────────────────────────────────────────────────────────


def test_cost_read_from_the_session_usage_blob(db_uri: str) -> None:
    """Cost comes off the row already loaded — no subtree walk."""
    priced = _seed(db_uri, title="Priced")
    sessions_module._session_status_cache[priced] = "running"
    SqlAlchemyConversationStore(db_uri).set_session_usage(priced, {"total_cost_usd": 1.25})

    assert _get(_app(db_uri))["sessions"][0]["cost_usd"] == 1.25


def test_unreadable_cost_degrades_instead_of_reading_as_zero(db_uri: str) -> None:
    """A present-but-unusable cost is unknown, not free."""
    priced = _seed(db_uri, title="Priced")
    sessions_module._session_status_cache[priced] = "running"
    SqlAlchemyConversationStore(db_uri).set_session_usage(priced, {"total_cost_usd": "lots"})

    row = _get(_app(db_uri))["sessions"][0]
    assert row["cost_usd"] is None
    assert "cost_unreadable" in row["degraded"]


# ── Filtering and failure handling ─────────────────────────────────


def test_only_active_false_returns_idle_sessions(db_uri: str) -> None:
    """The documented default is True; False is the full non-archived scan."""
    idle = _seed(db_uri, title="Idle")
    sessions_module._session_status_cache[idle] = "idle"

    assert _get(_app(db_uri))["sessions"] == []
    body = _get(_app(db_uri), "?only_active=false")
    assert [row["session_id"] for row in body["sessions"]] == [idle]
    assert body["counts"] == {
        "active": 0,
        "awaiting": 0,
        "unknown": 0,
        "omitted": 0,
        "partial": False,
    }


def test_archived_sessions_are_excluded(db_uri: str) -> None:
    """Archived work is not something to monitor."""
    archived = _seed(db_uri, title="Archived")
    sessions_module._session_status_cache[archived] = "running"
    SqlAlchemyConversationStore(db_uri).update_conversation(archived, archived=True)

    assert _get(_app(db_uri), "?only_active=false")["sessions"] == []


def test_agent_name_batch_failure_degrades_only_that_field(db_uri: str) -> None:
    """One broken batch must not empty the feed — the rows are still the
    answer to "what needs me", minus one label."""
    running = _seed(db_uri, title="Running")
    sessions_module._session_status_cache[running] = "running"

    class _BrokenAgentStore(SqlAlchemyAgentStore):
        def get_names(self, *args: Any, **kwargs: Any) -> Any:
            raise SQLAlchemyError("agents table unreachable")

    body = _get(_app(db_uri, agent_store=_BrokenAgentStore(db_uri)))

    assert [row["session_id"] for row in body["sessions"]] == [running]
    assert body["sessions"][0]["agent_name"] is None
    assert "agent_names_unavailable" in body["degraded"]


def test_child_batch_failure_degrades_only_the_rollup(db_uri: str) -> None:
    """Losing the child lookup costs the rollup, not the whole feed."""
    running = _seed(db_uri, title="Running")
    sessions_module._session_status_cache[running] = "running"

    class _BrokenChildStore(SqlAlchemyConversationStore):
        def list_child_conversation_ids_by_parent(self, *args: Any, **kwargs: Any) -> Any:
            raise SQLAlchemyError("children query failed")

    body = _get(_app(db_uri, conversation_store=_BrokenChildStore(db_uri)))

    assert [row["session_id"] for row in body["sessions"]] == [running]
    assert "child_sessions_unavailable" in body["degraded"]
    # Without child ids a parent's blocked sub-agent is invisible to the
    # rollup, so the tallies are an undercount and have to say so.
    assert body["counts"]["partial"] is True


def test_every_feed_level_degradation_marks_the_counts_partial(db_uri: str) -> None:
    """The invariant behind the accumulator: there is no way to report a
    feed-level failure while still presenting the counts as complete."""
    running = _seed(db_uri, title="Running")
    sessions_module._session_status_cache[running] = "running"

    class _BrokenAgentStore(SqlAlchemyAgentStore):
        def get_names(self, *args: Any, **kwargs: Any) -> Any:
            raise SQLAlchemyError("agents table unreachable")

    class _BrokenPermissionStore(SqlAlchemyPermissionStore):
        def list_for_sessions(self, *args: Any, **kwargs: Any) -> Any:
            raise SQLAlchemyError("permissions table unreachable")

    for label, app in (
        ("agent names", _app(db_uri, agent_store=_BrokenAgentStore(db_uri))),
        ("permissions", _app(db_uri, permission_store=_BrokenPermissionStore(db_uri))),
        ("liveness", _app(db_uri, liveness_lookup=None)),
    ):
        body = _get(app)
        assert body["degraded"], label
        assert body["counts"]["partial"] is True, label


def test_infrastructure_failure_degrades_instead_of_500(db_uri: str) -> None:
    """A broken feed is explicit; an empty list alone would read as "all clear"
    — and so would zeroed tallies presented as totals."""

    class _BrokenStore:
        def list_conversations(self, **kwargs: Any) -> Any:
            raise SQLAlchemyError("database is on fire")

    app = _app(db_uri, conversation_store=_BrokenStore(), permission_store=None)

    resp = TestClient(app).get("/v1/monitor/sessions", headers={"X-Forwarded-Email": ALICE})

    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] == ["internal_error"]
    assert body["sessions"] == []
    # The counts are a floor of zero, not a total of zero.
    assert body["counts"]["partial"] is True
    assert body["truncated"] is True


def test_unexpected_error_is_contained_as_an_unreadable_feed(db_uri: str) -> None:
    """No failure escapes as a 500. A monitor that answers "server error" is
    as useless as one that answers "nothing needs you", so a crash comes
    back as an explicitly unreadable feed (and a logged traceback)."""

    class _BuggyStore:
        def list_conversations(self, **kwargs: Any) -> Any:
            raise TypeError("someone changed a signature")

    app = _app(db_uri, conversation_store=_BuggyStore(), permission_store=None)

    resp = TestClient(app).get("/v1/monitor/sessions", headers={"X-Forwarded-Email": ALICE})

    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] == ["internal_error"]
    assert body["sessions"] == []
    assert body["counts"]["partial"] is True


def test_unexpected_host_lookup_error_is_contained_as_503(db_uri: str) -> None:
    """The host filter's own boundary holds too: unverifiable is a typed 503
    whatever the lookup raised, never a 500 and never a scoped-to-nothing
    200."""

    class _BuggyHostStore:
        def get_host(self, host_id: str) -> Any:
            raise TypeError("host store contract changed")

    app = _app(db_uri, host_store=_BuggyHostStore())

    resp = TestClient(app).get(
        "/v1/monitor/sessions?host_id=host_a", headers={"X-Forwarded-Email": ALICE}
    )

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "host_unverifiable"


def test_caller_without_identity_sees_no_one_elses_sessions(db_uri: str) -> None:
    """``accessible_by=None`` would monitor everyone's sessions — fail closed.

    The test runtime sets ``OMNICRAFT_LOCAL_SINGLE_USER``, so an absent
    identity header resolves to the reserved local user rather than 401;
    either way the feed must stay scoped, never fall back to "all rows".
    """
    owned = _seed(db_uri, title="Someone's session")
    sessions_module._session_status_cache[owned] = "running"

    resp = TestClient(_app(db_uri)).get("/v1/monitor/sessions?only_active=false")

    assert resp.status_code in (200, 401)
    if resp.status_code == 200:
        assert resp.json()["sessions"] == []


# ── Query budget ───────────────────────────────────────────────────


def test_query_count_is_flat_in_the_number_of_sessions(db_uri: str) -> None:
    """Row count must not drive store round-trips (no N+1)."""
    calls: list[str] = []

    def _record(name: str) -> None:
        calls.append(name)

    class _CountingConversationStore(SqlAlchemyConversationStore):
        def list_conversations(self, *args: Any, **kwargs: Any) -> Any:
            _record("list_conversations")
            return super().list_conversations(*args, **kwargs)

        def list_child_conversation_ids_by_parent(self, *args: Any, **kwargs: Any) -> Any:
            _record("list_child_conversation_ids_by_parent")
            return super().list_child_conversation_ids_by_parent(*args, **kwargs)

        def get_conversation(self, *args: Any, **kwargs: Any) -> Any:
            _record("get_conversation")
            return super().get_conversation(*args, **kwargs)

    class _CountingAgentStore(SqlAlchemyAgentStore):
        def get_names(self, *args: Any, **kwargs: Any) -> Any:
            _record("get_names")
            return super().get_names(*args, **kwargs)

        def get(self, *args: Any, **kwargs: Any) -> Any:
            _record("agent_get")
            return super().get(*args, **kwargs)

    class _CountingPermissionStore(SqlAlchemyPermissionStore):
        def list_for_sessions(self, *args: Any, **kwargs: Any) -> Any:
            _record("list_for_sessions")
            return super().list_for_sessions(*args, **kwargs)

        def is_admin(self, *args: Any, **kwargs: Any) -> Any:
            _record("is_admin")
            return super().is_admin(*args, **kwargs)

    def _counting_liveness(ids: list[str]) -> dict[str, SessionLiveness]:
        _record("liveness")
        return _live(ids)

    def _build(session_count: int) -> list[str]:
        calls.clear()
        app = _app(
            db_uri,
            liveness_lookup=_counting_liveness,
            conversation_store=_CountingConversationStore(db_uri),
            agent_store=_CountingAgentStore(db_uri),
            permission_store=_CountingPermissionStore(db_uri),
        )
        resp = TestClient(app).get(
            "/v1/monitor/sessions?only_active=false", headers={"X-Forwarded-Email": ALICE}
        )
        assert resp.status_code == 200
        assert len(resp.json()["sessions"]) == session_count
        return list(calls)

    for index in range(3):
        sid = _seed(db_uri, title=f"S{index}")
        sessions_module._session_status_cache[sid] = "idle"
    three = _build(3)
    for index in range(3, 9):
        sid = _seed(db_uri, title=f"S{index}")
        sessions_module._session_status_cache[sid] = "idle"
    nine = _build(9)

    assert three == nine
    assert "get_conversation" not in nine
