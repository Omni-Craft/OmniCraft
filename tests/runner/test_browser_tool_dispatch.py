"""Tests for runner-local dispatch of the embedded-browser tools.

The runner forwards ``browser_*`` calls to the server's browser bridge and
formats the relay's result for the model — critically, a screenshot's data
URL is SAVED to the workspace as a PNG (never inlined; a raw data URL in a
tool result costs hundreds of thousands of tokens).
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnicraft.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _EMBEDDED_BROWSER_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _execute_embedded_browser_tool,
    should_dispatch_locally,
)

_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n fake").decode()


def _client(payload: dict[str, Any]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/browser/actions")
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")


@pytest.mark.parametrize("name", sorted(_EMBEDDED_BROWSER_TOOLS))
def test_browser_tools_are_runner_local_and_relayed(name: str) -> None:
    assert name in _ALL_LOCAL_TOOLS
    assert name in _NATIVE_RELAY_BUILTIN_TOOLS
    assert should_dispatch_locally(name) is True


def test_navigate_formats_ok(tmp_path: Path) -> None:
    out = asyncio.run(
        _execute_embedded_browser_tool(
            {"url": "http://localhost:3000"},
            tool_name="browser_navigate",
            server_client=_client({"ok": True, "data": {"final_url": "http://localhost:3000"}}),
            conversation_id="conv_1",
            runner_workspace=tmp_path,
        )
    )
    assert "final_url" in out and "localhost:3000" in out


def test_screenshot_is_saved_to_workspace_not_inlined(tmp_path: Path) -> None:
    out = asyncio.run(
        _execute_embedded_browser_tool(
            {},
            tool_name="browser_screenshot",
            server_client=_client({"ok": True, "data_url": f"data:image/png;base64,{_PNG}"}),
            conversation_id="conv_1",
            runner_workspace=tmp_path,
        )
    )
    assert "Screenshot saved:" in out
    assert _PNG not in out  # the base64 payload must never reach the model
    saved = list((tmp_path / ".omnicraft" / "browser").glob("screenshot-*.png"))
    assert len(saved) == 1
    assert saved[0].read_bytes().startswith(b"\x89PNG")


def test_snapshot_returns_tree_with_ids(tmp_path: Path) -> None:
    out = asyncio.run(
        _execute_embedded_browser_tool(
            {},
            tool_name="browser_snapshot",
            server_client=_client(
                {
                    "ok": True,
                    "data": {
                        "snapshot_id": "snap-1",
                        "url": "http://x",
                        "title": "X",
                        "tree": '- button "Enviar" [ref=1]',
                    },
                }
            ),
            conversation_id="conv_1",
            runner_workspace=tmp_path,
        )
    )
    assert "snapshot_id: snap-1" in out
    assert "[ref=1]" in out


def test_bridge_error_is_surfaced(tmp_path: Path) -> None:
    out = asyncio.run(
        _execute_embedded_browser_tool(
            {"url": "http://x"},
            tool_name="browser_navigate",
            server_client=_client({"ok": False, "error": "nenhum navegador respondeu à ação"}),
            conversation_id="conv_1",
            runner_workspace=tmp_path,
        )
    )
    assert out.startswith("Error:")
    assert "nenhum navegador" in out


def test_missing_session_context_fails_loud(tmp_path: Path) -> None:
    out = asyncio.run(
        _execute_embedded_browser_tool(
            {},
            tool_name="browser_snapshot",
            server_client=None,
            conversation_id=None,
            runner_workspace=tmp_path,
        )
    )
    assert out.startswith("Error:")
