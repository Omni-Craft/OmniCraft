"""Tests for the session-scoped auto-approvals switch.

``omnicraft.approvals=auto`` on a session's labels makes the server answer
harness PERMISSION prompts (claude / codex / cursor / antigravity) with
``accept`` before any card is published — flippable mid-session via label
updates. Genuine questions (forms, AskUserQuestion) are never auto-answered.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from omnicraft.server.routes.sessions import (
    AUTO_APPROVALS_LABEL_KEY,
    AUTO_APPROVALS_VALUE,
    _auto_approve_verdict,
)
from omnicraft.server.schemas import ElicitationRequestParams

pytestmark = pytest.mark.asyncio


class _Store:
    def __init__(self, labels: dict[str, str]):
        self._labels = labels

    def get_conversation(self, session_id: str) -> Any:
        return SimpleNamespace(labels=self._labels)


def _params(phase: str, **extras: Any) -> ElicitationRequestParams:
    return ElicitationRequestParams(
        mode="form",
        message="Agent wants approval",
        requestedSchema=None,
        url=None,
        phase=phase,
        policy_name="native_permission",
        **extras,
    )


_AUTO = _Store({AUTO_APPROVALS_LABEL_KEY: AUTO_APPROVALS_VALUE})
_ASK = _Store({})


@pytest.mark.parametrize(
    "phase",
    [
        "pre_tool_use",
        "agy_permission",
        "codex_command_approval",
        "codex_file_change_approval",
        "codex_permissions_approval",
        "codex_apply_patch_approval",
    ],
)
async def test_permission_phases_auto_approve_when_label_set(phase: str) -> None:
    verdict = await _auto_approve_verdict(_AUTO, "conv_1", _params(phase))
    assert verdict is not None and verdict.action == "accept"


async def test_no_label_still_asks() -> None:
    assert await _auto_approve_verdict(_ASK, "conv_1", _params("pre_tool_use")) is None


@pytest.mark.parametrize(
    "phase",
    ["agy_ask_question", "codex_request_user_input", "codex_mcp_elicitation"],
)
async def test_question_phases_never_auto_approve(phase: str) -> None:
    assert await _auto_approve_verdict(_AUTO, "conv_1", _params(phase)) is None


async def test_ask_user_question_payload_never_auto_approves() -> None:
    params = _params("pre_tool_use", ask_user_question={"questions": []})
    assert await _auto_approve_verdict(_AUTO, "conv_1", params) is None


async def test_form_schema_never_auto_approves() -> None:
    params = ElicitationRequestParams(
        mode="form",
        message="fill this in",
        requestedSchema={"type": "object", "properties": {}},
        url=None,
        phase="pre_tool_use",
        policy_name="native_permission",
    )
    assert await _auto_approve_verdict(_AUTO, "conv_1", params) is None


async def test_store_failure_falls_back_to_asking() -> None:
    class _Broken:
        def get_conversation(self, session_id: str) -> Any:
            raise RuntimeError("db down")

    assert await _auto_approve_verdict(_Broken(), "conv_1", _params("pre_tool_use")) is None
    assert await _auto_approve_verdict(None, "conv_1", _params("pre_tool_use")) is None
