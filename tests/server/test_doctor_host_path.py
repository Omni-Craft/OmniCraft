"""Tests for the doctor's host-PATH discrepancy check (4b).

A CLI installed in the user's shell (nvm/brew) but invisible to the host
daemon's PATH used to read as "not installed" and produce wrong install
advice (observed live with codex under ~/.nvm). The doctor now crosses
the server's own ``shutil.which`` view with the host-reported
``configured_harnesses`` and calls out the discrepancy explicitly.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnicraft.server.routes.doctor import create_doctor_router


class _FakeAgentStore:
    def get_by_name(self, name: str) -> Any:
        return SimpleNamespace(name=name)


class _FakeHostStore:
    def __init__(self, harnesses: dict[str, Any]) -> None:
        self._harnesses = harnesses

    def list_hosts(self, owner: str) -> list[Any]:
        return [SimpleNamespace(configured_harnesses=self._harnesses)]


def _client(harnesses: dict[str, Any] | None) -> TestClient:
    app = FastAPI()
    app.include_router(create_doctor_router(_FakeAgentStore()), prefix="/v1")
    app.state.host_registry = SimpleNamespace(online_host_ids=lambda: ["host_1"])
    if harnesses is not None:
        app.state.host_store = _FakeHostStore(harnesses)
    app.state.scheduled_agent_store = None
    return TestClient(app)


def _host_path_check(body: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in body["checks"] if c["id"] == "host_path")


def test_shell_installed_but_host_missing_is_called_out(
    monkeypatch: Any,
) -> None:
    import omnicraft.server.routes.doctor as doctor_mod

    # Server-side which finds codex; host reports binary-missing.
    monkeypatch.setattr(
        doctor_mod.shutil, "which", lambda cli: "/x/bin/" + cli if cli == "codex" else None
    )
    body = _client({"codex": "binary-missing", "claude_sdk": True}).get("/v1/doctor").json()
    check = _host_path_check(body)
    assert check["ok"] is False
    assert "codex" in check["detail"]
    assert "PATH" in check["hint"]
    assert "ln -sf" in check["hint"]


def test_truly_missing_cli_is_not_a_discrepancy(monkeypatch: Any) -> None:
    import omnicraft.server.routes.doctor as doctor_mod

    # Neither the server nor the host can see pi — that's plain "not
    # installed", owned by the workers check, not a PATH discrepancy.
    monkeypatch.setattr(doctor_mod.shutil, "which", lambda cli: None)
    body = _client({"pi": "binary-missing"}).get("/v1/doctor").json()
    assert _host_path_check(body)["ok"] is True


def test_healthy_host_reports_no_discrepancy(monkeypatch: Any) -> None:
    import omnicraft.server.routes.doctor as doctor_mod

    monkeypatch.setattr(doctor_mod.shutil, "which", lambda cli: "/x/bin/" + cli)
    body = _client({"codex": True, "opencode": True}).get("/v1/doctor").json()
    assert _host_path_check(body)["ok"] is True


def test_missing_host_store_degrades_quietly(monkeypatch: Any) -> None:
    import omnicraft.server.routes.doctor as doctor_mod

    monkeypatch.setattr(doctor_mod.shutil, "which", lambda cli: "/x/bin/" + cli)
    body = _client(None).get("/v1/doctor").json()
    assert _host_path_check(body)["ok"] is True
