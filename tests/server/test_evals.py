"""Tests for agent evaluation grading + suite/run store."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture()
def ev(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("OMNICRAFT_CONFIG_HOME", str(tmp_path))
    module = importlib.import_module("omnicraft.server.evals")
    return importlib.reload(module)


def test_grade_check_types(ev) -> None:
    assert ev.grade("Hello World", {"type": "contains", "value": "world"}) is True
    assert ev.grade("Hello World", {"type": "contains", "value": "bye"}) is False
    assert ev.grade("Hello World", {"type": "not_contains", "value": "error"}) is True
    assert ev.grade("an error occurred", {"type": "not_contains", "value": "error"}) is False
    assert ev.grade("code: 200", {"type": "regex", "value": r"code:\s*\d+"}) is True
    assert ev.grade("abc", {"type": "regex", "value": "("}) is False  # bad regex -> fail
    # No/blank check is a no-op pass, so a task without an assertion never fails.
    assert ev.grade("anything", {"type": "contains", "value": ""}) is True


def test_suite_and_run_grading_with_regression(ev) -> None:
    suite = ev.create_suite(
        "smoke",
        [
            {"prompt": "say hi", "check": {"type": "contains", "value": "hi"}},
            {"prompt": "no errors", "check": {"type": "not_contains", "value": "error"}},
            {"prompt": "  ", "check": {}},  # blank prompt dropped
        ],
    )
    assert len(suite["tasks"]) == 2
    t0, t1 = suite["tasks"][0]["id"], suite["tasks"][1]["id"]

    # First run: both pass.
    r1 = ev.record_run(
        suite["id"],
        "v1",
        [{"task_id": t0, "output": "hi there"}, {"task_id": t1, "output": "all good"}],
    )
    assert r1["passed"] == 2 and r1["total"] == 2

    # Second run: task 1 regresses (now contains "error").
    r2 = ev.record_run(
        suite["id"],
        "v2",
        [{"task_id": t0, "output": "hi"}, {"task_id": t1, "output": "an error"}],
    )
    assert r2["passed"] == 1
    by_task = {res["task_id"]: res["passed"] for res in r2["results"]}
    assert by_task[t0] is True and by_task[t1] is False

    runs = ev.list_runs(suite["id"])
    assert [r["label"] for r in runs] == ["v2", "v1"]  # newest first

    ev.delete_suite(suite["id"])
    assert ev.get_suite(suite["id"]) is None
    assert ev.list_runs(suite["id"]) == []  # runs cascade-deleted
