"""Tests for the ``project_knowledge`` tool.

The tool never lets the agent name the project — it resolves it from the
session's label. That is the whole safety property: an agent in one project
cannot reach another project's shelf by asking nicely.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnicraft.entities import KnowledgeHit
from omnicraft.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _PROJECT_KNOWLEDGE_TOOLS,
    should_dispatch_locally,
)
from omnicraft.tools.base import ToolContext
from omnicraft.tools.builtins import INSTANTIABLE_BUILTINS, get_builtin_tool
from omnicraft.tools.builtins import project_knowledge as pk


def _tool():
    tool = get_builtin_tool("project_knowledge")
    assert tool is not None
    return tool


def _ctx(conversation_id: str | None = "conv_1") -> ToolContext:
    return ToolContext(task_id="t", agent_id="a", conversation_id=conversation_id)


class _FakeStore:
    """A document store that answers only for the project it was built with."""

    def __init__(self, project: str, hits: list[KnowledgeHit]) -> None:
        self.project = project
        self.hits = hits
        self.asked: list[tuple[str, str, int]] = []

    def search(self, project: str, query: str, limit: int = 5) -> list[KnowledgeHit]:
        self.asked.append((project, query, limit))
        return self.hits if project == self.project else []


def _hit(text: str = "Rescisão em 30 dias.", filename: str = "contrato.pdf") -> KnowledgeHit:
    return KnowledgeHit(document_id="pdoc_1", filename=filename, chunk_index=0, text=text, score=2)


def _wire(monkeypatch: pytest.MonkeyPatch, *, project: str | None, store: Any) -> None:
    monkeypatch.setattr(pk, "_resolve_project", lambda _ctx: project)
    monkeypatch.setattr(pk, "_document_store", lambda: store)


# --- registration -----------------------------------------------------------


def test_tool_is_registered_and_runner_local() -> None:
    assert "project_knowledge" in INSTANTIABLE_BUILTINS
    assert "project_knowledge" in _PROJECT_KNOWLEDGE_TOOLS
    assert "project_knowledge" in _ALL_LOCAL_TOOLS
    assert should_dispatch_locally("project_knowledge") is True


def test_schema_takes_a_query_and_nothing_about_the_project() -> None:
    """The agent must not be able to choose which project it reads."""
    params = _tool().get_schema()["function"]["parameters"]
    assert params["required"] == ["query"]
    assert set(params["properties"]) == {"query", "limit"}


# --- behaviour --------------------------------------------------------------


def test_returns_passages_with_their_source(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, project="Acme", store=_FakeStore("Acme", [_hit()]))
    out = _tool().invoke(json.dumps({"query": "rescisão"}), _ctx())
    assert "contrato.pdf" in out
    assert "Rescisão em 30 dias." in out
    assert "Acme" in out


def test_session_outside_a_project_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "No project" and "nothing found" are different problems for the caller."""
    _wire(monkeypatch, project=None, store=_FakeStore("Acme", [_hit()]))
    out = _tool().invoke(json.dumps({"query": "rescisão"}), _ctx())
    assert "não está em nenhum projeto" in out


def test_no_match_explains_why_it_might_be_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, project="Acme", store=_FakeStore("Acme", []))
    out = _tool().invoke(json.dumps({"query": "coisa ausente"}), _ctx())
    assert "Nada encontrado" in out
    assert "extraível" in out


def test_the_project_comes_from_the_session_not_the_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if the model invents a project argument, the label decides."""
    store = _FakeStore("Acme", [_hit()])
    _wire(monkeypatch, project="Acme", store=store)
    _tool().invoke(json.dumps({"query": "x", "project": "Outro"}), _ctx())
    assert store.asked[0][0] == "Acme"


def test_limit_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore("Acme", [_hit()])
    _wire(monkeypatch, project="Acme", store=store)
    _tool().invoke(json.dumps({"query": "x", "limit": 9999}), _ctx())
    assert store.asked[0][2] == pk._MAX_LIMIT


def test_bad_limit_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore("Acme", [_hit()])
    _wire(monkeypatch, project="Acme", store=store)
    _tool().invoke(json.dumps({"query": "x", "limit": "muitos"}), _ctx())
    assert store.asked[0][2] == pk._DEFAULT_LIMIT


# --- input validation -------------------------------------------------------


def test_missing_query() -> None:
    assert "query" in _tool().invoke(json.dumps({}), _ctx())


def test_malformed_arguments() -> None:
    assert "inválidos" in _tool().invoke("{nao é json", _ctx())


def test_store_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, project="Acme", store=None)
    assert "indisponível" in _tool().invoke(json.dumps({"query": "x"}), _ctx())


# --- project resolution -----------------------------------------------------


def test_resolve_project_without_a_conversation() -> None:
    assert pk._resolve_project(_ctx(conversation_id=None)) is None


def test_resolve_project_survives_a_store_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store hiccup must read as "no project", not blow up the tool call."""
    import omnicraft.runtime as runtime

    def boom() -> Any:
        raise RuntimeError("sem store")

    monkeypatch.setattr(runtime, "get_conversation_store", boom)
    assert pk._resolve_project(_ctx()) is None
