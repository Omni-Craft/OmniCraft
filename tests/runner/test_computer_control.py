"""Tests for the runner-local computer-control tool.

The tool shells out to ``screencapture`` / ``cliclick`` / ``open`` on the runner
host. These tests stub the shell-out so they run anywhere — no macOS, no
cliclick, no screen — and exercise the command wiring, the Retina pixel→point
scaling, argument validation, and the missing-cliclick degradation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnicraft.runner import computer_control as cc
from omnicraft.runner.host_shell import ShellResult
from omnicraft.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _COMPUTER_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _execute_computer_tool,
    should_dispatch_locally,
)
from omnicraft.tools.base import ToolContext
from omnicraft.tools.builtins import INSTANTIABLE_BUILTINS, get_builtin_tool


@pytest.fixture(autouse=True)
def _fixed_scale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the display scale so no test probes the real screen."""
    monkeypatch.setattr(cc, "_scale", (1.0, 1.0))


def _fake_shell(recorder: list[list[str]], result: ShellResult):
    """A ``shell_out`` stub that records argv and returns a fixed result."""

    async def run(argv: list[str], *, timeout: float = 60.0) -> ShellResult:
        recorder.append(argv)
        return result

    return run


def _with_cliclick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc.shutil, "which", lambda _name: "/opt/homebrew/bin/cliclick")


# --- registration -----------------------------------------------------------


def test_tool_is_registered_and_runner_local() -> None:
    assert "computer" in INSTANTIABLE_BUILTINS
    assert "computer" in _COMPUTER_TOOLS
    assert "computer" in _ALL_LOCAL_TOOLS
    assert "computer" in _NATIVE_RELAY_BUILTIN_TOOLS
    assert should_dispatch_locally("computer") is True


def test_schema_shape() -> None:
    tool = get_builtin_tool("computer")
    assert tool is not None
    schema = tool.get_schema()["function"]
    assert schema["name"] == "computer"
    params = schema["parameters"]
    assert params["required"] == ["action"]
    actions = params["properties"]["action"]["enum"]
    for expected in ("screenshot", "click", "drag", "type", "key"):
        assert expected in actions


def test_server_side_invoke_is_a_guard() -> None:
    tool = get_builtin_tool("computer")
    assert "runner" in tool.invoke("{}", ToolContext(task_id="t", agent_id="a")).lower()


# --- key combos (pure) ------------------------------------------------------


@pytest.mark.parametrize(
    ("combo", "expected"),
    [
        ("return", ["kp:return"]),
        ("page-down", ["kp:page-down"]),
        ("cmd+s", ["kd:cmd", "t:s", "ku:cmd"]),
        ("cmd+shift+4", ["kd:cmd,shift", "t:4", "ku:cmd,shift"]),
        ("CMD+S", ["kd:cmd", "t:s", "ku:cmd"]),
    ],
)
def test_key_argv_translates_combos(combo: str, expected: list[str]) -> None:
    assert cc.key_argv(combo) == expected


@pytest.mark.parametrize("combo", ["bogus+x", "naosei", "", "+"])
def test_key_argv_rejects_junk(combo: str) -> None:
    assert cc.key_argv(combo) is None


# --- validation -------------------------------------------------------------


def test_unknown_action() -> None:
    out = asyncio.run(cc.run_action("frobnicate", {}, workspace=None))
    assert "desconhecida" in out


def test_click_needs_coordinates(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_cliclick(monkeypatch)
    out = asyncio.run(cc.run_action("click", {"x": "nope"}, workspace=None))
    assert "inteiros" in out


def test_type_needs_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_cliclick(monkeypatch)
    out = asyncio.run(cc.run_action("type", {}, workspace=None))
    assert "text" in out


def test_key_rejects_bad_combo(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_cliclick(monkeypatch)
    out = asyncio.run(cc.run_action("key", {"keys": "bogus+x"}, workspace=None))
    assert "inválido" in out


def test_open_app_needs_name() -> None:
    out = asyncio.run(cc.run_action("open_app", {}, workspace=None))
    assert "app" in out


# --- cliclick degradation ---------------------------------------------------


@pytest.mark.parametrize("action", ["click", "double_click", "right_click", "move", "type", "key"])
def test_actions_degrade_without_cliclick(action: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc.shutil, "which", lambda _name: None)
    args = {"x": 1, "y": 2, "text": "oi", "keys": "return"}
    out = asyncio.run(cc.run_action(action, args, workspace=None))
    assert "cliclick" in out and "brew install" in out


# --- command wiring (stubbed shell) ----------------------------------------


def test_click_scales_pixels_to_points(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 2x Retina capture clicks at half the pixel coordinates."""
    calls: list[list[str]] = []
    _with_cliclick(monkeypatch)
    monkeypatch.setattr(cc, "_scale", (0.5, 0.5))
    monkeypatch.setattr(cc, "_shell", _fake_shell(calls, ShellResult(0, "", "")))
    out = asyncio.run(cc.run_action("click", {"x": 400, "y": 200}, workspace=None))
    assert "click em (400, 200)" in out
    assert calls == [["cliclick", "c:200,100"]]


def test_right_click_uses_its_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    _with_cliclick(monkeypatch)
    monkeypatch.setattr(cc, "_shell", _fake_shell(calls, ShellResult(0, "", "")))
    asyncio.run(cc.run_action("right_click", {"x": 10, "y": 20}, workspace=None))
    assert calls == [["cliclick", "rc:10,20"]]


def test_drag_emits_down_move_up(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    _with_cliclick(monkeypatch)
    monkeypatch.setattr(cc, "_shell", _fake_shell(calls, ShellResult(0, "", "")))
    asyncio.run(cc.run_action("drag", {"x": 1, "y": 2, "to_x": 3, "to_y": 4}, workspace=None))
    assert calls == [["cliclick", "dd:1,2", "dm:3,4", "du:3,4"]]


def test_key_combo_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    _with_cliclick(monkeypatch)
    monkeypatch.setattr(cc, "_shell", _fake_shell(calls, ShellResult(0, "", "")))
    asyncio.run(cc.run_action("key", {"keys": "cmd+s"}, workspace=None))
    assert calls == [["cliclick", "kd:cmd", "t:s", "ku:cmd"]]


def test_open_url_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(cc, "_shell", _fake_shell(calls, ShellResult(0, "", "")))
    out = asyncio.run(cc.run_action("open_url", {"url": "https://ex.com"}, workspace=None))
    assert "Abriu" in out
    assert calls == [["open", "https://ex.com"]]


def test_screenshot_saved_to_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake(argv: list[str], *, timeout: float = 60.0) -> ShellResult:
        # argv[-1] is the capture path; drop a placeholder there.
        Path(argv[-1]).write_bytes(b"\x89PNG fake")
        return ShellResult(0, "", "")

    monkeypatch.setattr(cc, "_shell", fake)
    out = asyncio.run(cc.run_action("screenshot", {}, workspace=tmp_path))
    assert "Screenshot salvo" in out
    saved = list((tmp_path / ".omnicraft" / "computer").glob("screen-*.png"))
    assert len(saved) == 1


def test_screenshot_failure_mentions_permission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cc, "_shell", _fake_shell([], ShellResult(1, "", "not authorized")))
    out = asyncio.run(cc.run_action("screenshot", {}, workspace=tmp_path))
    assert "Gravação de Tela" in out


# --- dispatcher wrapper -----------------------------------------------------


def test_execute_wrapper_requires_action(tmp_path: Path) -> None:
    out = asyncio.run(_execute_computer_tool({}, runner_workspace=tmp_path))
    assert "action" in out
