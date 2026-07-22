"""Tests for the runner-local iOS Simulator tool.

The tool shells out to ``xcrun simctl`` / ``xcodebuild`` / ``idb`` on the runner
host. These tests stub the shell-out (``_shell``) so they run anywhere — no
Xcode, no booted simulator, no idb — and exercise the command wiring, argument
validation, the no-runtime and no-idb degradations, and screenshot saving.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from omnicraft.runner import ios_simulator as ios
from omnicraft.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _IOS_SIMULATOR_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _execute_ios_simulator_tool,
    should_dispatch_locally,
)
from omnicraft.tools.base import ToolContext
from omnicraft.tools.builtins import INSTANTIABLE_BUILTINS, get_builtin_tool


def _fake_shell(recorder: list[list[str]], result: ios.ShellResult):
    """An ``_shell`` stub that records argv and returns a fixed result."""

    async def run(argv: list[str], *, timeout: float = 60.0) -> ios.ShellResult:
        recorder.append(argv)
        return result

    return run


# --- registration -----------------------------------------------------------


def test_tool_is_registered_and_runner_local() -> None:
    assert "ios_simulator" in INSTANTIABLE_BUILTINS
    assert "ios_simulator" in _IOS_SIMULATOR_TOOLS
    assert "ios_simulator" in _ALL_LOCAL_TOOLS
    assert "ios_simulator" in _NATIVE_RELAY_BUILTIN_TOOLS
    assert should_dispatch_locally("ios_simulator") is True


def test_schema_shape() -> None:
    tool = get_builtin_tool("ios_simulator")
    assert tool is not None
    schema = tool.get_schema()["function"]
    assert schema["name"] == "ios_simulator"
    params = schema["parameters"]
    assert params["required"] == ["action"]
    assert "list" in params["properties"]["action"]["enum"]
    assert "screenshot" in params["properties"]["action"]["enum"]


def test_server_side_invoke_is_a_guard() -> None:
    tool = get_builtin_tool("ios_simulator")
    ctx = ToolContext(task_id="t", agent_id="a")
    assert "runner" in tool.invoke("{}", ctx).lower()


# --- pure helpers -----------------------------------------------------------


def test_format_device_list_no_runtimes() -> None:
    out = ios.format_device_list({"runtimes": [], "devices": {}})
    assert "Nenhum runtime iOS" in out


def test_format_device_list_marks_booted() -> None:
    parsed = {
        "runtimes": [{"identifier": "com.apple.rt.iOS-18-4", "name": "iOS 18.4"}],
        "devices": {
            "com.apple.rt.iOS-18-4": [
                {"name": "iPhone 17 Pro", "udid": "U1", "state": "Booted", "isAvailable": True},
                {"name": "iPad", "udid": "U2", "state": "Shutdown", "isAvailable": True},
            ]
        },
    }
    out = ios.format_device_list(parsed)
    assert "iOS 18.4" in out
    assert "▶ iPhone 17 Pro" in out
    assert "· iPad" in out


@pytest.mark.parametrize(
    ("device", "expected"),
    [
        (None, "generic/platform=iOS Simulator"),
        ("booted", "generic/platform=iOS Simulator"),
        ("iPhone 17 Pro", "platform=iOS Simulator,name=iPhone 17 Pro"),
        (
            "D3AD2222-0000-4000-8000-AABBCCDDEEFF",
            "platform=iOS Simulator,id=D3AD2222-0000-4000-8000-AABBCCDDEEFF",
        ),
    ],
)
def test_destination(device: str | None, expected: str) -> None:
    assert ios._destination(device) == expected


# --- validation (no subprocess) ---------------------------------------------


def test_unknown_action() -> None:
    out = asyncio.run(ios.run_action("frobnicate", {}, workspace=None))
    assert "desconhecida" in out


def test_boot_needs_device() -> None:
    out = asyncio.run(ios.run_action("boot", {}, workspace=None))
    assert "precisa de um device" in out


def test_appearance_rejects_bad_mode() -> None:
    out = asyncio.run(ios.run_action("appearance", {"mode": "sepia"}, workspace=None))
    assert "light" in out and "dark" in out


def test_install_needs_app_path() -> None:
    out = asyncio.run(ios.run_action("install", {}, workspace=None))
    assert "app_path" in out


def test_launch_needs_bundle_id() -> None:
    out = asyncio.run(ios.run_action("launch", {}, workspace=None))
    assert "bundle_id" in out


def test_build_needs_scheme() -> None:
    out = asyncio.run(ios.run_action("build", {}, workspace=None))
    assert "scheme" in out


# --- idb degradation --------------------------------------------------------


def test_tap_without_idb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ios.shutil, "which", lambda _name: None)
    out = asyncio.run(ios.run_action("tap", {"x": 10, "y": 20}, workspace=None))
    assert "idb" in out


def test_tap_needs_coords(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ios.shutil, "which", lambda _name: "/usr/local/bin/idb")
    out = asyncio.run(ios.run_action("tap", {"x": "nope"}, workspace=None))
    assert "inteiros" in out


def test_tap_with_idb_builds_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(ios.shutil, "which", lambda _name: "/usr/local/bin/idb")
    monkeypatch.setattr(ios, "_shell", _fake_shell(calls, ios.ShellResult(0, "", "")))
    out = asyncio.run(ios.run_action("tap", {"x": 12, "y": 34, "device": "U9"}, workspace=None))
    assert "Toque" in out
    assert calls == [["idb", "ui", "tap", "12", "34", "--udid", "U9"]]


# --- simctl command wiring (stubbed shell) ----------------------------------


def test_list_formats_json(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        {
            "runtimes": [{"identifier": "r", "name": "iOS 18.4"}],
            "devices": {"r": [{"name": "iPhone 17 Pro", "udid": "U1", "state": "Shutdown"}]},
        }
    )
    monkeypatch.setattr(ios, "_shell", _fake_shell([], ios.ShellResult(0, payload, "")))
    out = asyncio.run(ios.run_action("list", {}, workspace=None))
    assert "iOS 18.4" in out and "iPhone 17 Pro" in out


def test_launch_targets_booted_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(ios, "_shell", _fake_shell(calls, ios.ShellResult(0, "", "")))
    out = asyncio.run(ios.run_action("launch", {"bundle_id": "com.acme.field"}, workspace=None))
    assert "com.acme.field" in out
    assert calls == [["xcrun", "simctl", "launch", "booted", "com.acme.field"]]


def test_appearance_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(ios, "_shell", _fake_shell(calls, ios.ShellResult(0, "", "")))
    asyncio.run(ios.run_action("appearance", {"mode": "dark", "device": "U1"}, workspace=None))
    assert calls == [["xcrun", "simctl", "ui", "U1", "appearance", "dark"]]


def test_screenshot_saved_to_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake(argv: list[str], *, timeout: float = 60.0) -> ios.ShellResult:
        # argv[-1] is the screenshot output path; write a placeholder PNG there.
        Path(argv[-1]).write_bytes(b"\x89PNG fake")
        return ios.ShellResult(0, "", "")

    monkeypatch.setattr(ios, "_shell", fake)
    out = asyncio.run(ios.run_action("screenshot", {}, workspace=tmp_path))
    assert "Screenshot salvo" in out
    saved = list((tmp_path / ".omnicraft" / "ios").glob("screenshot-*.png"))
    assert len(saved) == 1 and saved[0].read_bytes().startswith(b"\x89PNG")


def test_boot_opens_simulator_app(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(ios, "_shell", _fake_shell(calls, ios.ShellResult(0, "", "")))
    out = asyncio.run(ios.run_action("boot", {"device": "iPhone 17 Pro"}, workspace=None))
    assert "iniciado" in out
    assert ["xcrun", "simctl", "boot", "iPhone 17 Pro"] in calls
    assert ["open", "-a", "Simulator"] in calls


# --- dispatcher wrapper -----------------------------------------------------


def test_execute_wrapper_requires_action(tmp_path: Path) -> None:
    out = asyncio.run(_execute_ios_simulator_tool({}, runner_workspace=tmp_path))
    assert "action" in out
