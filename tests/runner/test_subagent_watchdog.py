"""Tests for the sub-agent first-output watchdog.

A dispatched child whose harness stalls before its first signal used to
hang the parent orchestrator forever: the child never registered an
in-flight turn, the idle reaper killed it silently ~30min later, and
nothing reached the parent inbox (observed live with a claude-native
child stuck at an empty prompt). The watchdog fails such dispatches
loudly into the parent inbox.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

import omnicraft.runner.app as runner_app
import omnicraft.runner.tool_dispatch as td


def _mock_client(status: str, items: list[dict[str, Any]]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json={"data": items})
        return httpx.Response(200, json={"id": "conv_child", "status": status})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")


@pytest.fixture(autouse=True)
def _fast_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(td, "_SUBAGENT_WATCHDOG_POLL_S", 0.01)
    monkeypatch.setattr(td, "_SUBAGENT_WATCHDOG_QUIET_FAIL_S", 0.02)
    monkeypatch.setattr(td, "_SUBAGENT_WATCHDOG_HARD_FAIL_S", 0.2)


@pytest.fixture()
def _registered_child() -> Any:
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref["conv_parent"] = inbox
    entry = runner_app.register_subagent_work(
        parent_session_id="conv_parent",
        child_session_id="conv_child",
        agent="claude_code",
        title="fidelity-pass",
    )
    yield entry, inbox
    runner_app.unregister_subagent_work("conv_child")
    runner_app._session_inboxes_ref.pop("conv_parent", None)


STUCK_ITEMS = [
    # The stuck-at-boot signature: terminal resource + the dispatched user
    # message, and nothing else.
    {"type": "resource_event", "resource_type": "terminal"},
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "briefing"}]},
]


@pytest.mark.asyncio
async def test_stuck_child_fails_loudly_into_parent_inbox(_registered_child: Any) -> None:
    entry, inbox = _registered_child
    client = _mock_client("idle", STUCK_ITEMS)
    await td._watch_subagent_first_output("conv_child", server_client=client)
    assert entry.status == "failed"
    payload = inbox.get_nowait()
    assert payload["status"] == "failed"
    assert payload["conversation_id"] == "conv_child"
    assert "no output" in payload["output"]


@pytest.mark.asyncio
async def test_stuck_but_busy_child_fails_only_at_hard_deadline(_registered_child: Any) -> None:
    entry, inbox = _registered_child
    # Status "running" (busy) keeps the quiet deadline from firing; the
    # hard deadline still catches a worker that never produces anything.
    client = _mock_client("running", STUCK_ITEMS)
    await td._watch_subagent_first_output("conv_child", server_client=client)
    assert entry.status == "failed"
    assert not inbox.empty()


@pytest.mark.asyncio
async def test_healthy_child_exits_without_failing(_registered_child: Any) -> None:
    entry, inbox = _registered_child
    items = [*STUCK_ITEMS, {"type": "function_call", "name": "Read", "arguments": "{}"}]
    client = _mock_client("running", items)
    await td._watch_subagent_first_output("conv_child", server_client=client)
    assert entry.status == "launching"  # untouched — normal paths own it
    assert inbox.empty()


@pytest.mark.asyncio
async def test_assistant_message_counts_as_output(_registered_child: Any) -> None:
    entry, inbox = _registered_child
    items = [
        *STUCK_ITEMS,
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "oi"}],
        },
    ]
    client = _mock_client("running", items)
    await td._watch_subagent_first_output("conv_child", server_client=client)
    assert entry.status == "launching"
    assert inbox.empty()


@pytest.mark.asyncio
async def test_terminal_entry_is_left_alone(_registered_child: Any) -> None:
    entry, inbox = _registered_child
    runner_app.mark_subagent_work_terminal("conv_child", status="completed", output="done")
    inbox.get_nowait()  # drain the completion payload
    client = _mock_client("idle", STUCK_ITEMS)
    await td._watch_subagent_first_output("conv_child", server_client=client)
    assert entry.status == "completed"
    assert inbox.empty()


@pytest.mark.asyncio
async def test_deleted_child_ends_watch_quietly(_registered_child: Any) -> None:
    entry, inbox = _registered_child

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    await td._watch_subagent_first_output("conv_child", server_client=client)
    assert entry.status == "launching"
    assert inbox.empty()


@pytest.mark.asyncio
async def test_transient_http_errors_are_tolerated(_registered_child: Any) -> None:
    entry, inbox = _registered_child
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ConnectError("boom", request=request)
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json={"data": STUCK_ITEMS})
        return httpx.Response(200, json={"status": "idle"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    await td._watch_subagent_first_output("conv_child", server_client=client)
    # Survived the transient errors and still reached the quiet-fail verdict.
    assert entry.status == "failed"
    payload = inbox.get_nowait()
    assert json.loads(json.dumps(payload))["status"] == "failed"


def test_output_detection_signature() -> None:
    assert not td._child_produced_output(STUCK_ITEMS)
    assert td._child_produced_output([{"type": "reasoning"}])
    assert td._child_produced_output([{"type": "function_call_output"}])
    assert not td._child_produced_output([{"type": "message", "role": "user"}])
