"""Tests for the iOS Simulator preview router's non-delegating logic.

The device/screenshot/tap endpoints delegate to the runner's ``ios_simulator``
module (covered in tests/runner). The router's own logic is picking the booted
device out of a simctl listing.
"""

from __future__ import annotations

from omnicraft.server.routes.ios_simulator import _first_booted, create_ios_simulator_router


def test_first_booted_finds_the_running_device() -> None:
    parsed = {
        "devices": {
            "iOS-18-4": [
                {"name": "iPhone 17 Pro", "udid": "U1", "state": "Shutdown"},
                {"name": "iPad", "udid": "U2", "state": "Booted"},
            ]
        }
    }
    assert _first_booted(parsed) == {"udid": "U2", "name": "iPad"}


def test_first_booted_returns_none_when_all_shutdown() -> None:
    parsed = {"devices": {"iOS-18-4": [{"name": "iPhone", "udid": "U1", "state": "Shutdown"}]}}
    assert _first_booted(parsed) is None


def test_first_booted_handles_empty() -> None:
    assert _first_booted({}) is None


def test_router_exposes_the_three_endpoints() -> None:
    router = create_ios_simulator_router()
    paths = {route.path for route in router.routes}  # type: ignore[attr-defined]
    assert "/sessions/{session_id}/ios/devices" in paths
    assert "/sessions/{session_id}/ios/screenshot" in paths
    assert "/sessions/{session_id}/ios/tap" in paths
