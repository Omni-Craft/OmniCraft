"""Tests for runner-local timer tool dispatch."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest

from omnicraft.runner.tool_dispatch import execute_tool


class _TimerPostRecorder:
    """
    ``httpx.MockTransport`` handler that records timer wake POSTs.

    The ``posts`` attribute stores dictionaries with ``url``,
    ``method``, ``json``, and ``headers`` keys, e.g.
    ``{"url": "/v1/sessions/...", "method": "POST"}``.
    """

    def __init__(self) -> None:
        """Initialize an empty call log."""
        self.posts: list[dict[str, Any]] = []
        self.post_seen = asyncio.Event()

    async def __call__(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        """
        Record a timer wake request and return an accepted response.

        :param request: HTTPX request, e.g. POST to
            ``"/v1/sessions/conv_x/events"``.
        :returns: HTTP 202 response matching the session event endpoint.
        """
        self.posts.append(
            {
                "url": request.url.path,
                "method": request.method,
                "json": json.loads(request.content),
                "headers": dict(request.headers),
            }
        )
        self.post_seen.set()
        return httpx.Response(202, json={"queued": True})


@pytest.mark.asyncio
async def test_timer_firing_posts_hidden_meta_message() -> None:
    """
    Timer firings wake the agent but stay hidden from user-facing UI.

    The timer POST must remain a ``role="user"`` message so the
    sessions event path starts or steers the next turn. Marking it
    ``is_meta=True`` is what makes existing web/TUI transcript
    rendering skip the synthetic ``[System: timer ... fired]`` row.
    """
    recorder = _TimerPostRecorder()
    transport = httpx.MockTransport(recorder)

    async with httpx.AsyncClient(transport=transport, base_url="http://server") as server_client:
        output = await execute_tool(
            tool_name="sys_timer_set",
            arguments=json.dumps({"seconds": 0, "note": "check build"}),
            conversation_id="conv_parent",
            server_client=server_client,
        )

        result = json.loads(output)
        assert result["status"] == "scheduled"
        assert isinstance(result["timer_id"], str)

        await asyncio.wait_for(recorder.post_seen.wait(), timeout=1.0)

    # A non-repeating timer should produce exactly one wake POST:
    # zero means the firing never reached AP, more than one means it
    # accidentally behaved like a repeating timer.
    assert len(recorder.posts) == 1
    post = recorder.posts[0]
    assert post["method"] == "POST"
    assert post["url"] == "/v1/sessions/conv_parent/events"
    payload = post["json"]
    assert payload == {
        "type": "message",
        "data": {
            "role": "user",
            "is_meta": True,
            "content": [
                {
                    "type": "input_text",
                    "text": f"[System: timer {result['timer_id']} fired]\nnote: 'check build'",
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_timer_set_rejects_invalid_args_via_shared_validator() -> None:
    """
    The runner dispatch path validates through the shared
    ``validate_timer_set_args`` helper, so a bad ``seconds`` returns the
    same message the in-process builtin surfaces and starts no timer
    task (no wake POST is ever made).
    """
    recorder = _TimerPostRecorder()
    transport = httpx.MockTransport(recorder)

    async with httpx.AsyncClient(transport=transport, base_url="http://server") as server_client:
        output = await execute_tool(
            tool_name="sys_timer_set",
            arguments=json.dumps({"seconds": -1}),
            conversation_id="conv_parent",
            server_client=server_client,
        )

    assert json.loads(output) == {"error": "seconds must be non-negative"}
    assert recorder.posts == []


@pytest.mark.asyncio
async def test_timer_set_rejects_zero_delay_repeat() -> None:
    """
    A ``repeat=true`` timer with ``seconds=0`` is rejected at dispatch
    and starts no timer task.

    Left unguarded, ``_timer_loop`` would ``sleep(0)`` and POST in a
    tight loop forever (a self-inflicted DoS on the session events
    endpoint). We assert the value is refused — never run the loop —
    and that no wake POST is emitted.
    """
    recorder = _TimerPostRecorder()
    transport = httpx.MockTransport(recorder)

    async with httpx.AsyncClient(transport=transport, base_url="http://server") as server_client:
        output = await execute_tool(
            tool_name="sys_timer_set",
            arguments=json.dumps({"seconds": 0, "repeat": True}),
            conversation_id="conv_parent",
            server_client=server_client,
        )

    assert json.loads(output) == {"error": "seconds must be > 0 when repeat is true"}
    assert recorder.posts == []


class _FailingPostRecorder(_TimerPostRecorder):
    """Timer POST handler that replies 500 to exercise the error path."""

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record the request then return a server error response."""
        await super().__call__(request)
        return httpx.Response(500, json={"error": "boom"})


@pytest.mark.asyncio
async def test_timer_firing_surfaces_http_delivery_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A 4xx/5xx wake response is treated as a delivery failure and logged.

    ``httpx`` does not raise on error status by default, so without the
    ``raise_for_status`` guard a failed firing would be silently
    swallowed. A one-shot timer still makes exactly one POST; the fix
    routes the 500 into the existing warning path.
    """
    recorder = _FailingPostRecorder()
    transport = httpx.MockTransport(recorder)

    async with httpx.AsyncClient(transport=transport, base_url="http://server") as server_client:
        with caplog.at_level(logging.WARNING, logger="omnicraft.runner.tool_dispatch"):
            output = await execute_tool(
                tool_name="sys_timer_set",
                arguments=json.dumps({"seconds": 0}),
                conversation_id="conv_parent",
                server_client=server_client,
            )
            assert json.loads(output)["status"] == "scheduled"
            await asyncio.wait_for(recorder.post_seen.wait(), timeout=1.0)

            # The warning is logged after the 500 returns and
            # ``raise_for_status`` fires, which happens a few event-loop
            # hops past ``post_seen``. Poll with a real deadline rather
            # than a fixed ``sleep(0)`` drain, which races the loop and
            # flakes in CI.
            def _warning_logged() -> bool:
                return any("firing persist failed" in rec.message for rec in caplog.records)

            loop = asyncio.get_running_loop()
            deadline = loop.time() + 1.0
            while not _warning_logged() and loop.time() < deadline:
                await asyncio.sleep(0.01)

    assert len(recorder.posts) == 1
    assert _warning_logged()
