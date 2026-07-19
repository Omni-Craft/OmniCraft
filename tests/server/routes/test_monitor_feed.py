"""Tests for ``GET /v1/monitor/sessions`` — the shared monitor feed.

The feed is what every monitor surface reads, so the properties under
test are the ones a surface cannot recover on its own: ``waiting`` is
never collapsed into ``running``, "blocked on a human" comes from the
elicitation index (not a tool-call flag), a part that fails to resolve
says so instead of looking clean, and listing N sessions stays a fixed
number of store calls.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnicraft.errors import OmniCraftError
from omnicraft.runtime import pending_elicitations
from omnicraft.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnicraft.server.routes import sessions as sessions_module
from omnicraft.server.routes.monitor import create_monitor_router
from omnicraft.server.routes.sessions import SessionLiveness
from omnicraft.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnicraft.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnicraft.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

ALICE = "alice@example.com"
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


def _app(db_uri: str, liveness_lookup: Any = _live) -> FastAPI:
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
            SqlAlchemyConversationStore(db_uri),
            SqlAlchemyAgentStore(db_uri),
            auth_provider=UnifiedAuthProvider(source="header"),
            permission_store=SqlAlchemyPermissionStore(db_uri),
            liveness_lookup=liveness_lookup,
        ),
        prefix="/v1",
    )
    return app


def _seed(
    db_uri: str,
    *,
    title: str,
    host_id: str | None = None,
    project: str | None = None,
    parent_id: str | None = None,
    kind: str = "default",
) -> str:
    """Create a session owned by Alice; returns its id."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    perms = SqlAlchemyPermissionStore(db_uri)
    if agent_store.get("ag_test") is None:
        agent_store.create(agent_id="ag_test", name="test-agent", bundle_location="ag_test/bundle")
    conv = conv_store.create_conversation(
        kind=kind,
        title=title,
        agent_id="ag_test",
        host_id=host_id,
        parent_conversation_id=parent_id,
        workspace="/tmp/ws",
    )
    if project is not None:
        conv_store.set_labels(conv.id, {PROJECT_LABEL_KEY: project})
    perms.ensure_user(ALICE)
    perms.grant(ALICE, conv.id, LEVEL_OWNER)
    return conv.id


def _get(app: FastAPI, query: str = "") -> dict[str, Any]:
    resp = TestClient(app).get(
        f"/v1/monitor/sessions{query}", headers={"X-Forwarded-Email": ALICE}
    )
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


def test_feed_shape_and_counts(db_uri: str) -> None:
    """The payload carries the documented envelope and per-row fields."""
    running = _seed(db_uri, title="Running", host_id="host_a", project="Ship it")
    _seed(db_uri, title="Idle", host_id="host_a")
    sessions_module._session_status_cache[running] = "running"

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
    assert body["counts"] == {"active": 1, "awaiting": 0}


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


def test_awaiting_counted_from_pending_elicitations(db_uri: str) -> None:
    """ "Needs a human" comes from the elicitation index, with a summary."""
    blocked = _seed(db_uri, title="Blocked")
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
    pending_elicitations.record_publish(blocked, _elicitation("elicit_2"))

    body = _get(_app(db_uri))

    assert [row["session_id"] for row in body["sessions"]] == [blocked]
    assert body["counts"] == {"active": 0, "awaiting": 1}


def test_only_active_false_returns_idle_sessions(db_uri: str) -> None:
    """The documented default is True; False is the full non-archived page."""
    idle = _seed(db_uri, title="Idle")

    assert _get(_app(db_uri))["sessions"] == []
    body = _get(_app(db_uri), "?only_active=false")
    assert [row["session_id"] for row in body["sessions"]] == [idle]
    assert body["counts"] == {"active": 0, "awaiting": 0}


def test_host_id_filter(db_uri: str) -> None:
    """``host_id`` narrows the feed and is echoed back on the envelope."""
    on_a = _seed(db_uri, title="On A", host_id="host_a")
    on_b = _seed(db_uri, title="On B", host_id="host_b")
    for sid in (on_a, on_b):
        sessions_module._session_status_cache[sid] = "running"

    body = _get(_app(db_uri), "?host_id=host_a")

    assert body["host_id"] == "host_a"
    assert [row["session_id"] for row in body["sessions"]] == [on_a]


def test_archived_sessions_are_excluded(db_uri: str) -> None:
    """Archived work is not something to monitor."""
    archived = _seed(db_uri, title="Archived")
    sessions_module._session_status_cache[archived] = "running"
    SqlAlchemyConversationStore(db_uri).update_conversation(archived, archived=True)

    assert _get(_app(db_uri), "?only_active=false")["sessions"] == []


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


def test_unreadable_status_is_kept_and_flagged(db_uri: str) -> None:
    """A status value this server doesn't understand must not vanish as idle."""
    weird = _seed(db_uri, title="Weird")
    sessions_module._session_status_cache[weird] = "quantum"

    body = _get(_app(db_uri))

    row = body["sessions"][0]
    assert row["session_id"] == weird
    assert "status_unreadable" in row["degraded"]


def test_pending_count_without_readable_payload_degrades(db_uri: str) -> None:
    """A blocked session whose payload can't be read still reads as blocked."""
    blocked = _seed(db_uri, title="Blocked")
    pending_elicitations.record_publish(blocked, _elicitation("elicit_3"))

    app = _app(db_uri)
    with TestClient(app) as client:
        # Index reports the count but the payload snapshot comes back empty.
        original = pending_elicitations.snapshot_for
        pending_elicitations.snapshot_for = lambda _sid: []  # type: ignore[assignment]
        try:
            resp = client.get("/v1/monitor/sessions", headers={"X-Forwarded-Email": ALICE})
        finally:
            pending_elicitations.snapshot_for = original  # type: ignore[assignment]

    body = resp.json()
    row = body["sessions"][0]
    assert row["pending_elicitations_count"] == 1
    assert row["pending_elicitation"] is None
    assert "pending_elicitation_unreadable" in row["degraded"]
    assert body["counts"]["awaiting"] == 1


def test_internal_failure_degrades_instead_of_500(db_uri: str) -> None:
    """A broken feed is explicit; an empty list alone would read as "all clear"."""
    app = FastAPI()

    class _BrokenStore:
        def list_conversations(self, **kwargs: Any) -> Any:
            raise RuntimeError("database is on fire")

    app.include_router(
        create_monitor_router(
            _BrokenStore(),  # type: ignore[arg-type]
            SqlAlchemyAgentStore(db_uri),
            auth_provider=UnifiedAuthProvider(source="header"),
            permission_store=None,
        ),
        prefix="/v1",
    )

    resp = TestClient(app).get("/v1/monitor/sessions", headers={"X-Forwarded-Email": ALICE})

    assert resp.status_code == 200
    assert resp.json()["degraded"] == ["internal_error"]
    assert resp.json()["sessions"] == []


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


def test_query_count_is_flat_in_the_number_of_sessions(db_uri: str) -> None:
    """Row count must not drive store round-trips (no N+1)."""
    calls: list[str] = []

    class _CountingConversationStore(SqlAlchemyConversationStore):
        def list_conversations(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("list_conversations")
            return super().list_conversations(*args, **kwargs)

        def list_child_conversation_ids_by_parent(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("list_child_conversation_ids_by_parent")
            return super().list_child_conversation_ids_by_parent(*args, **kwargs)

        def get_conversation(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("get_conversation")
            return super().get_conversation(*args, **kwargs)

    def _build(session_count: int) -> list[str]:
        calls.clear()
        app = FastAPI()
        app.include_router(
            create_monitor_router(
                _CountingConversationStore(db_uri),
                SqlAlchemyAgentStore(db_uri),
                auth_provider=UnifiedAuthProvider(source="header"),
                permission_store=SqlAlchemyPermissionStore(db_uri),
                liveness_lookup=_live,
            ),
            prefix="/v1",
        )
        resp = TestClient(app).get(
            "/v1/monitor/sessions?only_active=false", headers={"X-Forwarded-Email": ALICE}
        )
        assert resp.status_code == 200
        assert len(resp.json()["sessions"]) == session_count
        return list(calls)

    for index in range(3):
        _seed(db_uri, title=f"S{index}")
    three = _build(3)
    for index in range(3, 9):
        _seed(db_uri, title=f"S{index}")
    nine = _build(9)

    assert three == nine
    assert "get_conversation" not in nine
