"""Tests for scheduled / webhook-triggered agents (store + firing)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from omnicraft.server import scheduled_agents as sched


@pytest.fixture(autouse=True)
def _isolate_config_home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the JSON store at a throwaway dir so tests never touch ~/.omnicraft."""
    monkeypatch.setenv("OMNICRAFT_CONFIG_HOME", str(tmp_path))


def test_create_lists_and_gets() -> None:
    job = sched.create_job(
        name="Nightly",
        agent_name="scribe",
        prompt="resumo",
        workspace="/repo",
        interval_seconds=3600,
    )
    assert job["id"]
    assert job["webhook_token"]
    assert job["next_run_at"] is not None  # enabled + interval → scheduled
    assert len(sched.list_jobs()) == 1
    assert sched.get_job(job["id"])["name"] == "Nightly"
    assert sched.get_job_by_token(job["webhook_token"])["id"] == job["id"]
    assert sched.get_job_by_token("nope") is None


def test_update_recomputes_schedule_and_disable_clears_it() -> None:
    job = sched.create_job(
        name="j", agent_name="a", prompt="p", workspace="/w", interval_seconds=60
    )
    sched.update_job(job["id"], {"enabled": False})
    assert sched.get_job(job["id"])["next_run_at"] is None
    assert sched.due_jobs() == []  # disabled → never due

    sched.update_job(job["id"], {"enabled": True, "interval_seconds": 1})
    time.sleep(1.1)
    due = sched.due_jobs()
    assert len(due) == 1 and due[0]["id"] == job["id"]


def test_webhook_only_job_is_never_schedule_due() -> None:
    # No interval → webhook/manual only; the scheduler must ignore it.
    sched.create_job(name="hook", agent_name="a", prompt="p", workspace="/w")
    assert sched.due_jobs() == []


def test_record_run_advances_and_caps_history() -> None:
    job = sched.create_job(
        name="j", agent_name="a", prompt="p", workspace="/w", interval_seconds=3600
    )
    for i in range(sched._MAX_HISTORY + 5):
        sched.record_run(
            job["id"], {"trigger": "manual", "status": "started", "session_id": f"c{i}"}
        )
    got = sched.get_job(job["id"])
    assert len(got["history"]) == sched._MAX_HISTORY
    assert got["history"][0]["session_id"] == f"c{sched._MAX_HISTORY + 4}"  # newest first
    assert got["last_run_at"] is not None


def test_delete() -> None:
    job = sched.create_job(name="j", agent_name="a", prompt="p", workspace="/w")
    assert sched.delete_job(job["id"]) is True
    assert sched.delete_job(job["id"]) is False
    assert sched.list_jobs() == []


# --- firing edge cases (no real host / missing agent) ----------------------


class _FakeAgent:
    id = "ag_x"


class _FakeAgentStore:
    def __init__(self, has: bool) -> None:
        self._has = has

    def get_by_name(self, name: str) -> Any:
        return _FakeAgent() if self._has else None


class _FakeRegistry:
    def __init__(self, hosts: list[str]) -> None:
        self._hosts = hosts

    def online_host_ids(self) -> list[str]:
        return self._hosts


class _FakeState:
    def __init__(self, hosts: list[str]) -> None:
        self.host_registry = _FakeRegistry(hosts)


class _FakeApp:
    def __init__(self, hosts: list[str]) -> None:
        self.state = _FakeState(hosts)


@pytest.mark.asyncio
async def test_fire_missing_agent_records_error() -> None:
    job = sched.create_job(name="j", agent_name="ghost", prompt="p", workspace="/w")
    res = await sched.fire_job(_FakeApp(["h1"]), job, _FakeAgentStore(has=False), trigger="manual")
    assert res["status"] == "error"
    assert "não encontrado" in res["detail"]
    assert sched.get_job(job["id"])["history"][0]["status"] == "error"


@pytest.mark.asyncio
async def test_fire_no_online_host_is_skipped() -> None:
    job = sched.create_job(name="j", agent_name="a", prompt="p", workspace="/w")
    res = await sched.fire_job(_FakeApp([]), job, _FakeAgentStore(has=True), trigger="schedule")
    assert res["status"] == "skipped"
    assert sched.get_job(job["id"])["history"][0]["trigger"] == "schedule"


@pytest.mark.asyncio
async def test_fire_specific_offline_host_is_skipped() -> None:
    job = sched.create_job(
        name="j", agent_name="a", prompt="p", workspace="/w", host_id="host_offline"
    )
    res = await sched.fire_job(
        _FakeApp(["host_other"]), job, _FakeAgentStore(has=True), trigger="webhook"
    )
    assert res["status"] == "skipped"
