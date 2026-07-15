"""Tests for threading the spec's permission_mode into the claude-native TUI.

Regression for the stalled-overnight-worker bug: a claude-native worker
whose spec said ``permission_mode: auto`` still launched with interactive
approvals, stalled on the first Edit prompt nobody was watching, and was
eventually killed by the idle reaper.
"""

from __future__ import annotations

from types import SimpleNamespace

from omnicraft.runner.app import _spec_permission_mode_args


def _spec(mode: str | None) -> SimpleNamespace:
    config = {} if mode is None else {"permission_mode": mode}
    return SimpleNamespace(executor=SimpleNamespace(config=config))


def test_auto_maps_to_bypass_permissions() -> None:
    assert _spec_permission_mode_args(_spec("auto"), None) == (
        "--permission-mode",
        "bypassPermissions",
    )


def test_explicit_mode_passes_through() -> None:
    assert _spec_permission_mode_args(_spec("acceptEdits"), None) == (
        "--permission-mode",
        "acceptEdits",
    )


def test_no_spec_mode_adds_nothing() -> None:
    assert _spec_permission_mode_args(_spec(None), None) == ()
    assert _spec_permission_mode_args(None, None) == ()


def test_user_pass_through_flags_win() -> None:
    assert _spec_permission_mode_args(_spec("auto"), ["--permission-mode", "plan"]) == ()
    assert _spec_permission_mode_args(_spec("auto"), ["--dangerously-skip-permissions"]) == ()
