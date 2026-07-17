"""
Tests that every spawn-env builder threads the session workspace ``cwd`` into
its ``HARNESS_<H>_CWD`` env var.

Regression guard for the escape where a session's selected working folder was
honored by the Files panel / primary OS environment but not by the spawned
harness subprocess: the claude-sdk / codex / cursor / qwen / goose / acp /
copilot builders accepted ``workdir`` (the agent bundle) but never the runtime
``cwd``, so ``HARNESS_<H>_CWD`` went unset and the harness ran outside the tree
the user picked. pi and kimi already threaded ``cwd``; they ride along here so
the behaviour stays locked for every harness that spawns a subprocess.

Scope: these tests assert what the builders emit. What each harness does with
the var — including the ``cwd is None`` path, where the fallback / inheritance
behaviour differs per consumer — is the consumer's own and is not covered here.

Unit test — no subprocess spawn, no real CLIs.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omnicraft.runtime.workflow import (
    _build_acp_spawn_env,
    _build_claude_sdk_spawn_env,
    _build_codex_spawn_env,
    _build_copilot_spawn_env,
    _build_cursor_spawn_env,
    _build_goose_spawn_env,
    _build_kimi_spawn_env,
    _build_pi_spawn_env,
    _build_qwen_spawn_env,
)
from omnicraft.spec.types import AgentSpec, ExecutorSpec

# (harness name, builder, HARNESS_<H>_CWD var, builder accepts ``workdir``)
_BUILDERS: list[tuple[str, Callable[..., dict[str, str]], str, bool]] = [
    ("claude-sdk", _build_claude_sdk_spawn_env, "HARNESS_CLAUDE_SDK_CWD", True),
    ("codex", _build_codex_spawn_env, "HARNESS_CODEX_CWD", True),
    ("cursor", _build_cursor_spawn_env, "HARNESS_CURSOR_CWD", True),
    ("qwen", _build_qwen_spawn_env, "HARNESS_QWEN_CWD", True),
    ("goose", _build_goose_spawn_env, "HARNESS_GOOSE_CWD", True),
    ("acp", _build_acp_spawn_env, "HARNESS_ACP_CWD", True),
    ("copilot", _build_copilot_spawn_env, "HARNESS_COPILOT_CWD", True),
    ("pi", _build_pi_spawn_env, "HARNESS_PI_CWD", True),
    ("kimi", _build_kimi_spawn_env, "HARNESS_KIMI_CWD", False),
]

_CASES = [pytest.param(*b, id=b[0]) for b in _BUILDERS]


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Point OMNICRAFT_CONFIG_HOME at an empty temp dir and stub provider
    detection so the developer's real ``~/.omnicraft/config.yaml`` and ambient
    CLI config (e.g. ``~/.codex/config.toml``) cannot steer the builders.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory for the isolated config.
    """
    monkeypatch.setenv("OMNICRAFT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("omnicraft.onboarding.detected.detect_providers", list)


def _make_spec(harness: str) -> AgentSpec:
    """
    Build a minimal spec for ``harness`` with no auth and no model pinned.

    :param harness: Canonical harness name, e.g. ``"codex"``.
    :returns: A populated :class:`AgentSpec`.
    """
    return AgentSpec(
        spec_version=1,
        name=f"test-{harness}",
        instructions="You are a test agent.",
        executor=ExecutorSpec(type="omnicraft", config={"harness": harness}),
    )


def _build(
    builder: Callable[..., dict[str, str]],
    harness: str,
    *,
    cwd: Path | None,
    workdir: Path | None,
    takes_workdir: bool,
) -> dict[str, str]:
    """
    Call ``builder`` with the kwargs it accepts (kimi has no ``workdir``).

    :param builder: The ``_build_*_spawn_env`` under test.
    :param harness: Canonical harness name.
    :param cwd: Session workspace, or ``None``.
    :param workdir: Bundle workdir, or ``None``.
    :param takes_workdir: Whether the builder accepts a ``workdir`` kwarg.
    :returns: The built spawn-env dict.
    """
    kwargs: dict[str, Any] = {"cwd": cwd}
    if takes_workdir:
        kwargs["workdir"] = workdir
    return builder(_make_spec(harness), **kwargs)


@pytest.mark.parametrize("harness,builder,cwd_var,takes_workdir", _CASES)
def test_builder_threads_session_cwd_distinct_from_bundle(
    tmp_path: Path,
    harness: str,
    builder: Callable[..., dict[str, str]],
    cwd_var: str,
    takes_workdir: bool,
) -> None:
    """
    The session workspace lands in ``HARNESS_<H>_CWD``, separate from the
    bundle ``workdir``. Conflating them launches the harness in the wrong tree.

    :param tmp_path: Temporary directory root.
    :param harness: Canonical harness name.
    :param builder: The ``_build_*_spawn_env`` under test.
    :param cwd_var: The env var expected to carry the workspace.
    :param takes_workdir: Whether the builder accepts a ``workdir`` kwarg.
    """
    workspace = tmp_path / "selected-workspace"
    workspace.mkdir()
    bundle_dir = tmp_path / "runner-specs" / f"ag_{harness}"
    bundle_dir.mkdir(parents=True)

    env = _build(builder, harness, cwd=workspace, workdir=bundle_dir, takes_workdir=takes_workdir)

    assert env[cwd_var] == str(workspace)
    assert env.get(cwd_var) != str(bundle_dir)


@pytest.mark.parametrize("harness,builder,cwd_var,takes_workdir", _CASES)
def test_builder_omits_cwd_when_none(
    harness: str,
    builder: Callable[..., dict[str, str]],
    cwd_var: str,
    takes_workdir: bool,
) -> None:
    """
    With no session workspace the builder emits no CWD var at all, rather than
    baking in a guess. What the harness then does is its own behaviour and is
    not asserted here.

    :param harness: Canonical harness name.
    :param builder: The ``_build_*_spawn_env`` under test.
    :param cwd_var: The env var expected to carry the workspace.
    :param takes_workdir: Whether the builder accepts a ``workdir`` kwarg.
    """
    env = _build(builder, harness, cwd=None, workdir=None, takes_workdir=takes_workdir)

    assert cwd_var not in env
