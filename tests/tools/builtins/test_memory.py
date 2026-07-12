"""Tests for the local long-term memory builtins."""

from __future__ import annotations

from typing import Any

import pytest

from omnicraft.tools.base import ToolContext
from omnicraft.tools.builtins.memory import MemoryRecallTool, MemoryRememberTool


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNICRAFT_CONFIG_HOME", str(tmp_path))


def _ctx(agent_id: str) -> ToolContext:
    return ToolContext(task_id="t", agent_id=agent_id, conversation_id="c1")


def test_remember_and_recall() -> None:
    rem, rec = MemoryRememberTool(), MemoryRecallTool()
    ctx = _ctx("ag_chat")
    rem.invoke('{"text": "Usuário prefere pt-BR"}', ctx)
    rem.invoke('{"text": "Projeto principal é OmniCraft"}', ctx)
    out = rec.invoke("{}", ctx)
    assert "OmniCraft" in out and "pt-BR" in out
    # Most recent first.
    assert out.index("OmniCraft") < out.index("pt-BR")


def test_recall_query_filters() -> None:
    rem, rec = MemoryRememberTool(), MemoryRecallTool()
    ctx = _ctx("ag_chat")
    rem.invoke('{"text": "gosta de café"}', ctx)
    rem.invoke('{"text": "trabalha com Swift"}', ctx)
    assert rec.invoke('{"query": "swift"}', ctx) == "- trabalha com Swift"


def test_bank_is_per_agent() -> None:
    rem, rec = MemoryRememberTool(), MemoryRecallTool()
    rem.invoke('{"text": "só do agente A"}', _ctx("ag_a"))
    assert rec.invoke("{}", _ctx("ag_b")) == "Nenhuma memória encontrada."


def test_remember_requires_text() -> None:
    assert "obrigatório" in MemoryRememberTool().invoke('{"text": "  "}', _ctx("ag_x"))


def test_schemas_are_valid() -> None:
    for tool in (MemoryRememberTool(), MemoryRecallTool()):
        schema = tool.get_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == tool.name()
