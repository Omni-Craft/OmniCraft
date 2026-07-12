"""Tests for the agent gallery (list + install)."""

from __future__ import annotations

from typing import Any

import pytest

from omnicraft.server import gallery


class _FakeAgent:
    def __init__(self, agent_id: str, name: str) -> None:
        self.id = agent_id
        self.name = name
        self.bundle_location = ""


class _FakeAgentStore:
    def __init__(self) -> None:
        self.agents: dict[str, _FakeAgent] = {}

    def get_by_name(self, name: str) -> _FakeAgent | None:
        return next((a for a in self.agents.values() if a.name == name), None)

    def create(self, agent_id: str, name: str, bundle_location: str, description: Any) -> None:
        self.agents[agent_id] = _FakeAgent(agent_id, name)
        self.agents[agent_id].bundle_location = bundle_location

    def update(self, agent_id: str, bundle_location: str) -> None:
        self.agents[agent_id].bundle_location = bundle_location


class _FakeArtifactStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put(self, loc: str, data: bytes) -> None:
        self.blobs[loc] = data


class _FakeAgentCache:
    def replace(self, *a: Any, **k: Any) -> None:
        pass


def test_list_gallery_agents_includes_examples() -> None:
    store = _FakeAgentStore()
    items = gallery.list_gallery_agents(store)
    names = {i["name"] for i in items}
    # The repo ships at least these example agents.
    assert {"fucho", "lilo", "remy", "scribe", "sentinel"} <= names
    lilo = next(i for i in items if i["name"] == "lilo")
    assert lilo["installed"] is False  # not in the fake store
    assert "description" in lilo and isinstance(lilo["skills"], list)


def test_install_is_idempotent_by_name() -> None:
    store, artifacts, cache = _FakeAgentStore(), _FakeArtifactStore(), _FakeAgentCache()
    first = gallery.install_gallery_agent("lilo", store, artifacts, cache)
    assert first is not None and first["name"] == "lilo"
    # Now it reads as installed.
    assert any(i["name"] == "lilo" and i["installed"] for i in gallery.list_gallery_agents(store))
    # Re-install returns the SAME agent id (no duplicate row).
    second = gallery.install_gallery_agent("lilo", store, artifacts, cache)
    assert second is not None and second["agent_id"] == first["agent_id"]
    assert len(store.agents) == 1


@pytest.mark.parametrize("bad", ["../etc", "a/b", "", ".", ".."])
def test_install_rejects_traversal(bad: str) -> None:
    store, artifacts, cache = _FakeAgentStore(), _FakeArtifactStore(), _FakeAgentCache()
    assert gallery.install_gallery_agent(bad, store, artifacts, cache) is None
