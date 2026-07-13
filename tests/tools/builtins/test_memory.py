"""Tests for the local long-term memory builtins."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnicraft.tools.base import ToolContext
from omnicraft.tools.builtins.memory import MemoryRecallTool, MemoryRememberTool


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNICRAFT_CONFIG_HOME", str(tmp_path))


def _ctx(agent_id: str) -> ToolContext:
    return ToolContext(task_id="t", agent_id=agent_id, conversation_id="c1")


def _ws_ctx(agent_id: str, workspace: Path) -> ToolContext:
    return ToolContext(task_id="t", agent_id=agent_id, conversation_id="c1", workspace=workspace)


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


def test_recall_matches_loose_multiword_queries() -> None:
    """An LLM queries with several loose keywords that never appear verbatim.

    Regression: whole-substring matching made "rodar testes test command" miss
    "Os testes deste projeto rodam com make check" — recall must match on ANY
    token instead.
    """
    rem, rec = MemoryRememberTool(), MemoryRecallTool()
    ctx = _ctx("ag_fucho")
    rem.invoke('{"text": "Os testes deste projeto rodam com make check"}', ctx)
    rem.invoke('{"text": "gosta de café"}', ctx)
    out = rec.invoke('{"query": "rodar testes test command"}', ctx)
    assert "make check" in out
    assert "café" not in out
    # Cross-language drift: an English query still hits via the 4-char stem
    # ("tests" → "test" ⊂ "testes").
    out_en = rec.invoke('{"query": "how to run tests"}', ctx)
    assert "make check" in out_en
    # A query matching nothing falls back to recent memories instead of an
    # authoritative-sounding empty result.
    out_none = rec.invoke('{"query": "zzz qqq www"}', ctx)
    assert "make check" in out_none and "café" in out_none


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


class _FakeConv:
    def __init__(self, project: str | None) -> None:
        self.labels = {"omni_project": project} if project else {}


class _FakeStore:
    def __init__(self, project: str | None) -> None:
        self._project = project

    def get_conversation(self, _cid: str) -> _FakeConv:
        return _FakeConv(self._project)


def test_memory_is_scoped_per_project(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnicraft.runtime as runtime

    rem, rec = MemoryRememberTool(), MemoryRecallTool()
    # Same agent + conversation, but filed under project A.
    monkeypatch.setattr(runtime, "get_conversation_store", lambda: _FakeStore("proj-a"))
    rem.invoke('{"text": "fato do projeto A"}', _ctx("ag_chat"))
    # Now the same agent under project B sees a different bank.
    monkeypatch.setattr(runtime, "get_conversation_store", lambda: _FakeStore("proj-b"))
    assert rec.invoke("{}", _ctx("ag_chat")) == "Nenhuma memória encontrada."
    rem.invoke('{"text": "fato do projeto B"}', _ctx("ag_chat"))
    assert "projeto B" in rec.invoke("{}", _ctx("ag_chat"))
    # Back to project A: still isolated.
    monkeypatch.setattr(runtime, "get_conversation_store", lambda: _FakeStore("proj-a"))
    out = rec.invoke("{}", _ctx("ag_chat"))
    assert "projeto A" in out and "projeto B" not in out


def test_memory_uses_project_folder_when_workspace_set(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    ws.mkdir()
    rem, rec = MemoryRememberTool(), MemoryRecallTool()
    ctx = _ws_ctx("ag_code", ws)
    rem.invoke('{"text": "fato no projeto"}', ctx)
    # Memory lands in the project's .omnicraft/ folder, not the global store.
    assert (ws / ".omnicraft" / "memory" / "memory.json").exists()
    assert (ws / ".omnicraft" / "README.md").exists()
    # Lock/tmp artifacts must never reach a commit — a sub-agent `git add -A`
    # would sweep them into a PR without this gitignore.
    gi = (ws / ".omnicraft" / "memory" / ".gitignore").read_text(encoding="utf-8")
    assert "*.lock" in gi and "*.tmp" in gi
    assert "fato no projeto" in rec.invoke("{}", ctx)


def test_project_memory_is_isolated_per_workspace(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    rem, rec = MemoryRememberTool(), MemoryRecallTool()
    rem.invoke('{"text": "só no A"}', _ws_ctx("ag_code", a))
    assert rec.invoke("{}", _ws_ctx("ag_code", b)) == "Nenhuma memória encontrada."
    assert "só no A" in rec.invoke("{}", _ws_ctx("ag_code", a))


def test_project_store_migrates_the_global_bank(tmp_path: Path) -> None:
    rem, rec = MemoryRememberTool(), MemoryRecallTool()
    # A fact saved earlier with no workspace goes to the global store.
    rem.invoke('{"text": "memória antiga global"}', _ctx("ag_code"))
    # The same agent now runs in a workspace: recall still sees it (fallback),
    # and the first project write migrates it into the project folder.
    ws = tmp_path / "proj"
    ws.mkdir()
    ws_ctx = _ws_ctx("ag_code", ws)
    assert "memória antiga global" in rec.invoke("{}", ws_ctx)
    rem.invoke('{"text": "memória nova do projeto"}', ws_ctx)
    out = rec.invoke("{}", ws_ctx)
    assert "memória antiga global" in out and "memória nova do projeto" in out
    assert (ws / ".omnicraft" / "memory" / "memory.json").exists()
