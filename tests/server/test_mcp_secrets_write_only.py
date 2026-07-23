"""Tests for write-only MCP secrets.

The connector directory has to install servers that need a credential (GitHub,
Slack, Notion), so ``env`` / ``headers`` are accepted on write. The whole point
is that they never come back out: these tests pin both halves — the secret
reaches the bundle YAML, and no read-side model can carry it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnicraft.server.routes.session_mcp_servers import (
    _body_to_file_yaml,
    _body_to_inline_yaml,
)
from omnicraft.server.schemas import MCPServerSummary, UpsertMCPServerRequest


def _stdio(**over) -> UpsertMCPServerRequest:
    return UpsertMCPServerRequest(
        **{"name": "github", "transport": "stdio", "command": "npx", **over}
    )


def _http(**over) -> UpsertMCPServerRequest:
    return UpsertMCPServerRequest(
        **{"name": "remote", "transport": "http", "url": "https://mcp.example.com", **over}
    )


# --- the read side cannot carry a secret ------------------------------------


def test_summary_model_has_no_secret_fields() -> None:
    """The only model returned by read routes must not even define them."""
    assert "env" not in MCPServerSummary.model_fields
    assert "headers" not in MCPServerSummary.model_fields


def test_summary_drops_secrets_passed_in() -> None:
    """Even if a caller tries, the summary cannot be built carrying secrets."""
    summary = MCPServerSummary(
        name="github",
        transport="stdio",
        command="npx",
        env={"TOKEN": "segredo"},  # type: ignore[call-arg]
        headers={"Authorization": "Bearer segredo"},  # type: ignore[call-arg]
    )
    dumped = summary.model_dump()
    assert "env" not in dumped
    assert "headers" not in dumped
    assert "segredo" not in str(dumped)


# --- the write side persists it ---------------------------------------------


def test_env_is_written_for_stdio() -> None:
    body = _stdio(env={"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_x"})
    assert _body_to_file_yaml(body, {})["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_x"}
    assert _body_to_inline_yaml(body, {})["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_x"}


def test_headers_are_written_for_http() -> None:
    body = _http(headers={"Authorization": "Bearer tok"})
    assert _body_to_file_yaml(body, {})["headers"] == {"Authorization": "Bearer tok"}


def test_env_var_reference_is_kept_verbatim() -> None:
    """A ``${VAR}`` reference must survive unexpanded — that is the safe form."""
    body = _stdio(env={"TOKEN": "${GITHUB_TOKEN}"})
    assert _body_to_file_yaml(body, {})["env"]["TOKEN"] == "${GITHUB_TOKEN}"


# --- editing without the secret preserves it --------------------------------


def test_omitted_env_preserves_existing() -> None:
    existing = {"env": {"TOKEN": "antigo"}, "timeout": 30}
    result = _body_to_file_yaml(_stdio(), existing)
    assert result["env"] == {"TOKEN": "antigo"}
    assert result["timeout"] == 30


def test_supplied_env_replaces_existing() -> None:
    existing = {"env": {"TOKEN": "antigo"}}
    result = _body_to_file_yaml(_stdio(env={"TOKEN": "novo"}), existing)
    assert result["env"] == {"TOKEN": "novo"}


def test_omitted_headers_preserves_existing() -> None:
    existing = {"headers": {"Authorization": "Bearer antigo"}, "auth": {"kind": "x"}}
    result = _body_to_file_yaml(_http(), existing)
    assert result["headers"] == {"Authorization": "Bearer antigo"}
    assert result["auth"] == {"kind": "x"}


# --- validation -------------------------------------------------------------


def test_env_rejected_on_http_transport() -> None:
    with pytest.raises(ValidationError, match="env is not allowed"):
        _http(env={"A": "b"})


def test_headers_rejected_on_stdio_transport() -> None:
    with pytest.raises(ValidationError, match="headers are not allowed"):
        _stdio(headers={"A": "b"})


def test_secret_map_is_bounded() -> None:
    with pytest.raises(ValidationError, match="at most 32"):
        _stdio(env={f"K{i}": "v" for i in range(33)})


def test_secret_keys_must_be_sane() -> None:
    with pytest.raises(ValidationError, match="1-128 characters"):
        _stdio(env={"": "v"})
