"""Tests for the shared server-config loader (:mod:`omnicraft.server.server_config`).

Covers path resolution (env override → ``<data_dir>/config.yaml`` →
None), loading + fail-open behavior (missing / malformed / non-mapping
→ empty dict, never a crash), and the ``config_str_list`` coercion used
for ``admins`` / ``allowed_domains``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnicraft.server.server_config import (
    config_str_list,
    load_server_config,
    resolve_config_path,
    unbound_session_ttl_hours,
)


def _pin_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point <data_dir> at tmp_path and clear the explicit-path override."""
    monkeypatch.delenv("OMNICRAFT_CONFIG", raising=False)
    monkeypatch.setenv("OMNICRAFT_ADMIN_CREDENTIALS_PATH", str(tmp_path / "admin-credentials"))


# ── path resolution ───────────────────────────────────────────────


def test_resolve_config_path_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``OMNICRAFT_CONFIG`` wins over the data-dir default."""
    p = tmp_path / "custom.yaml"
    p.write_text("{}")
    monkeypatch.setenv("OMNICRAFT_CONFIG", str(p))
    assert resolve_config_path() == p


def test_resolve_config_path_default_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Falls back to ``<data_dir>/config.yaml`` when that file exists."""
    _pin_data_dir(monkeypatch, tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("admins: [a@x.com]\n")
    assert resolve_config_path() == cfg


def test_resolve_config_path_none_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No env, no default file → ``None`` (pure-env back-compat)."""
    _pin_data_dir(monkeypatch, tmp_path)
    assert resolve_config_path() is None


# ── loading ───────────────────────────────────────────────────────


def test_load_server_config_parses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A well-formed config loads into a dict."""
    _pin_data_dir(monkeypatch, tmp_path)
    (tmp_path / "config.yaml").write_text("admins:\n  - a@x.com\nallowed_domains: [x.com]\n")
    cfg = load_server_config()
    assert cfg["admins"] == ["a@x.com"]
    assert cfg["allowed_domains"] == ["x.com"]


def test_load_server_config_empty_when_no_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No config file → empty dict (not an error)."""
    _pin_data_dir(monkeypatch, tmp_path)
    assert load_server_config() == {}


def test_load_server_config_malformed_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Malformed YAML fails open to empty rather than crashing startup."""
    _pin_data_dir(monkeypatch, tmp_path)
    (tmp_path / "config.yaml").write_text("admins: [unclosed\n")
    assert load_server_config() == {}


def test_load_server_config_non_mapping_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A top-level non-mapping (e.g. a list) is ignored."""
    _pin_data_dir(monkeypatch, tmp_path)
    (tmp_path / "config.yaml").write_text("- a\n- b\n")
    assert load_server_config() == {}


# ── config_str_list ───────────────────────────────────────────────


def test_config_str_list_accepts_list() -> None:
    assert config_str_list(["a@x.com", "b@x.com"]) == ["a@x.com", "b@x.com"]


def test_config_str_list_accepts_scalar() -> None:
    """A single scalar is wrapped — a one-entry value needn't be a list."""
    assert config_str_list("a@x.com") == ["a@x.com"]


def test_config_str_list_none_is_empty() -> None:
    assert config_str_list(None) == []


def test_config_str_list_strips_and_drops_empty() -> None:
    assert config_str_list(["  a@x.com  ", "", "  "]) == ["a@x.com"]


# ── unbound_session_ttl_hours ────────────────────────────────────────


def test_unbound_session_ttl_hours_default_when_no_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No config file → the generous 24h built-in default."""
    _pin_data_dir(monkeypatch, tmp_path)
    assert unbound_session_ttl_hours() == 24


def test_unbound_session_ttl_hours_reads_config_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The TTL is surfaced the same way as other server settings (config.yaml)."""
    _pin_data_dir(monkeypatch, tmp_path)
    (tmp_path / "config.yaml").write_text("unbound_session_ttl_hours: 6\n")
    assert unbound_session_ttl_hours() == 6


def test_unbound_session_ttl_hours_invalid_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-positive/non-numeric override degrades to the safe default."""
    _pin_data_dir(monkeypatch, tmp_path)
    (tmp_path / "config.yaml").write_text("unbound_session_ttl_hours: -1\n")
    assert unbound_session_ttl_hours() == 24
