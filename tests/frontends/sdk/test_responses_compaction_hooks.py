"""
Regression coverage for :meth:`ResponsesNamespace.stream` dispatching
``on_compaction_end``.

The SSE parser surfacing ``CompactionCompleted``/``CompactionFailed``
(pinned in ``test_async_client_tool_sdk.py``) is necessary but not
sufficient: the streaming loop in ``_responses.py`` must also match
those event types and call ``hooks.on_compaction_end``. Before the
fix, only ``CompactionInProgress`` was matched — a consumer awaiting
``on_compaction_end`` to dismiss a "Compacting…" spinner would hang
forever once compaction actually finished (or failed).

Mocks at the HTTP transport boundary via :class:`httpx.MockTransport`,
mirroring the pattern in ``test_sessions_namespace.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from omnicraft_client._responses import ResponsesNamespace
from omnicraft_client._tool_handler import CompactionEndCtx, StreamHooks


def _make_namespace(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[ResponsesNamespace, httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://srv")
    return ResponsesNamespace(client, "http://srv"), client


def _sse_frame(event_type: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n".encode()


@pytest.mark.asyncio
async def test_on_compaction_end_fires_for_completed_and_failed() -> None:
    frames = (
        _sse_frame(
            "response.created",
            {
                "type": "response.created",
                "response": {"id": "resp_1", "status": "in_progress", "model": "test-agent"},
            },
        )
        + _sse_frame(
            "response.compaction.completed",
            {"type": "response.compaction.completed", "total_tokens": 100},
        )
        + _sse_frame(
            "response.compaction.failed",
            {"type": "response.compaction.failed"},
        )
        + _sse_frame(
            "response.completed",
            {
                "type": "response.completed",
                "response": {"id": "resp_1", "status": "completed", "model": "test-agent"},
            },
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=frames,
            headers={"content-type": "text/event-stream"},
        )

    ns, client = _make_namespace(handler)
    seen: list[CompactionEndCtx] = []

    async def _on_compaction_end(ctx: CompactionEndCtx) -> None:
        seen.append(ctx)

    try:
        with pytest.warns(DeprecationWarning):
            events = [
                event
                async for event in ns.stream(
                    model="test-agent",
                    input="hi",
                    hooks=StreamHooks(on_compaction_end=_on_compaction_end),
                )
            ]
    finally:
        await client.aclose()

    assert events, "stream() should still yield the underlying events"
    assert len(seen) == 2, f"Expected on_compaction_end for Completed + Failed; got {seen!r}"

    completed_ctx, failed_ctx = seen
    assert completed_ctx.item["status"] == "completed"
    assert completed_ctx.item["total_tokens"] == 100

    assert failed_ctx.item["status"] == "failed"
