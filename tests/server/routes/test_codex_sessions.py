"""Unit tests for Codex session routes."""

from __future__ import annotations

import pytest

from omnicraft.server.routes.codex import sessions as codex_sessions


@pytest.mark.asyncio
async def test_initialize_codex_goal_runner_passes_conversation_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Goal runner initialization passes the store required by the handshake."""
    session_id = "conv_goal"
    conversation = object()
    runner_client = object()
    received: tuple[object, ...] | None = None

    class _ConversationStore:
        def get_conversation(self, _session_id: str) -> object:
            return conversation

    conversation_store = _ConversationStore()

    async def _initialize(*args: object) -> None:
        nonlocal received
        received = args

    monkeypatch.setattr(codex_sessions, "_ensure_runner_session_initialized", _initialize)

    await codex_sessions._initialize_codex_goal_runner(
        session_id, runner_client, conversation_store
    )

    assert received == (session_id, conversation, runner_client, conversation_store)
