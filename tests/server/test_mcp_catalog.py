"""Tests for the curated MCP connector catalog.

The catalog feeds one-click installs, so a malformed entry becomes a connector
that silently fails to start on a user's machine. These tests check the shape of
every shipped entry against what ``UpsertMCPServerRequest`` will accept.
"""

from __future__ import annotations

import json

import pytest

from omnicraft.server.routes.mcp_catalog import (
    _CATALOG_PATH,
    create_mcp_catalog_router,
    load_catalog,
)
from omnicraft.server.schemas import UpsertMCPServerRequest

CONNECTORS = load_catalog()["connectors"]


def test_catalog_file_is_valid_json() -> None:
    with _CATALOG_PATH.open(encoding="utf-8") as handle:
        data = json.load(handle)
    assert isinstance(data["connectors"], list)
    assert data["connectors"], "o catálogo não pode ser vazio"


def test_router_exposes_the_endpoint() -> None:
    router = create_mcp_catalog_router()
    paths = {route.path for route in router.routes}  # type: ignore[attr-defined]
    assert "/mcp-catalog" in paths


def test_load_catalog_is_cached() -> None:
    assert load_catalog() is load_catalog()


def test_ids_are_unique() -> None:
    ids = [c["id"] for c in CONNECTORS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("connector", CONNECTORS, ids=lambda c: c["id"])
def test_entry_has_the_display_fields(connector: dict) -> None:
    for field in ("id", "title", "emoji", "category", "description", "transport"):
        assert connector.get(field), f"{connector.get('id')} sem {field}"


@pytest.mark.parametrize("connector", CONNECTORS, ids=lambda c: c["id"])
def test_entry_is_installable_as_written(connector: dict) -> None:
    """Every entry must survive the same validation the install route applies."""
    payload = {
        "name": connector["id"],
        "transport": connector["transport"],
        "description": connector["description"][:512],
    }
    if connector["transport"] == "stdio":
        payload["command"] = connector["command"]
        payload["args"] = connector["args"]
    else:
        payload["url"] = connector["url"]
    # Raises if the catalog entry would be rejected on install.
    UpsertMCPServerRequest(**payload)


@pytest.mark.parametrize("connector", CONNECTORS, ids=lambda c: c["id"])
def test_env_required_entries_are_well_formed(connector: dict) -> None:
    for var in connector.get("env_required", []):
        assert var["name"].isupper() or "_" in var["name"], var["name"]
        assert var.get("label"), f"{connector['id']}: {var['name']} sem label"


def test_connectors_needing_secrets_declare_them() -> None:
    """Spot-check: the connectors that obviously need a token declare one."""
    by_id = {c["id"]: c for c in CONNECTORS}
    for needs_secret in ("github", "slack", "brave-search", "notion"):
        assert by_id[needs_secret]["env_required"], f"{needs_secret} deveria pedir credencial"
