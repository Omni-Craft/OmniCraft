"""Tests for routing the local memory builtins through runner-local dispatch.

Without runner-local dispatch a wrapped harness's (claude-sdk / codex / …) call
to memory_remember falls through to the harness, which has no such tool, and
errors "not in local dispatch table". These lock in that the tools dispatch
locally, are relayed to native harnesses, and thread the session workspace so
memory lands in the project's ``.omnicraft/memory/`` folder.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnicraft.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _LOCAL_MEMORY_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _execute_local_memory_tool,
    should_dispatch_locally,
)


@pytest.fixture(autouse=True)
def _isolate_global_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNICRAFT_CONFIG_HOME", str(tmp_path / "config-home"))


def test_memory_tool_set_is_exactly_the_two() -> None:
    assert set(_LOCAL_MEMORY_TOOLS) == {"memory_remember", "memory_recall"}


@pytest.mark.parametrize("name", ["memory_remember", "memory_recall"])
def test_memory_tools_are_runner_local(name: str) -> None:
    assert name in _ALL_LOCAL_TOOLS
    assert should_dispatch_locally(name) is True


@pytest.mark.parametrize("name", ["memory_remember", "memory_recall"])
def test_memory_tools_relayed_to_native_harnesses(name: str) -> None:
    # Like Hindsight: native harnesses have no built-in long-term memory.
    assert name in _NATIVE_RELAY_BUILTIN_TOOLS


def test_remember_writes_into_the_workspace_project_folder(tmp_path: Path) -> None:
    """With a session workspace, memory lands in <ws>/.omnicraft/memory/."""
    ws = tmp_path / "repo"
    ws.mkdir()
    result = asyncio.run(
        _execute_local_memory_tool(
            {"text": "os testes rodam com make check"},
            tool_name="memory_remember",
            conversation_id="conv_1",
            task_id="task_1",
            agent_id="ag_fucho",
            runner_workspace=ws,
        )
    )
    assert "make check" in result
    assert (ws / ".omnicraft" / "memory" / "memory.json").exists()
    out = asyncio.run(
        _execute_local_memory_tool(
            {"query": "make"},
            tool_name="memory_recall",
            conversation_id="conv_1",
            task_id="task_1",
            agent_id="ag_fucho",
            runner_workspace=ws,
        )
    )
    assert "make check" in out


def test_no_workspace_falls_back_to_global_store(tmp_path: Path) -> None:
    result = asyncio.run(
        _execute_local_memory_tool(
            {"text": "fato global"},
            tool_name="memory_remember",
            conversation_id="conv_1",
            task_id="task_1",
            agent_id="ag_x",
            runner_workspace=None,
        )
    )
    assert "fato global" in result
    # Global store got it (config home is isolated by the fixture).
    out = asyncio.run(
        _execute_local_memory_tool(
            {},
            tool_name="memory_recall",
            conversation_id="conv_1",
            task_id="task_1",
            agent_id="ag_x",
            runner_workspace=None,
        )
    )
    assert "fato global" in out
