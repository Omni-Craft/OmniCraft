"""Tests for the policy dry-run simulator."""

from __future__ import annotations

import pytest

from omnicraft.server.routes.policy_simulate import _apply_state_updates, simulate_policy

_ASK_OS = "omnicraft.policies.builtins.safety.ask_on_os_tools"
_MAX_CALLS = "omnicraft.policies.builtins.safety.max_tool_calls_per_session"


@pytest.mark.asyncio
async def test_ask_on_os_tools_asks_for_file_and_shell_tools() -> None:
    calls = [
        ("Bash", {"command": "ls"}),
        ("search.web", {"q": "x"}),
        ("Write", {"file_path": "/y"}),
    ]
    out = await simulate_policy(_ASK_OS, None, calls)
    assert [r["result"] for r in out["results"]] == ["ASK", "ALLOW", "ASK"]
    assert out["summary"] == {"ALLOW": 1, "ASK": 2, "DENY": 0}
    assert out["tool_call_count"] == 3


@pytest.mark.asyncio
async def test_max_tool_calls_denies_after_limit_via_carried_state() -> None:
    """The counter carries across calls, so it denies once the limit is hit."""
    calls = [("a", {}), ("b", {}), ("c", {}), ("d", {})]
    out = await simulate_policy(_MAX_CALLS, {"limit": 2}, calls)
    assert [r["result"] for r in out["results"]] == ["ALLOW", "ALLOW", "DENY", "DENY"]
    assert out["summary"] == {"ALLOW": 2, "ASK": 0, "DENY": 2}


@pytest.mark.asyncio
async def test_empty_session_is_a_clean_zero() -> None:
    out = await simulate_policy(_ASK_OS, None, [])
    assert out["tool_call_count"] == 0 and out["results"] == []
    assert out["summary"] == {"ALLOW": 0, "ASK": 0, "DENY": 0}


def test_apply_state_updates_actions() -> None:
    state: dict = {"n": 1, "xs": ["a"]}
    _apply_state_updates(
        state,
        [
            {"key": "n", "action": "increment", "value": 2},
            {"key": "flag", "action": "set", "value": True},
            {"key": "xs", "action": "append", "value": "b"},
            {"key": "gone", "action": "delete"},
            "not-a-dict",  # ignored
        ],
    )
    assert state == {"n": 3, "xs": ["a", "b"], "flag": True}
