"""Built-in local long-term memory — no external service, no API key.

A tiny file-backed memory the conversational Chat agent uses to remember durable
facts across sessions. Memories are keyed by the agent id (so every run of the
same agent shares one bank), stored as JSON under the OmniCraft config home. No
dependency and no key, so it boots clean — unlike the Hindsight builtins.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from omnicraft.tools.base import Tool, ToolContext

_lock = threading.Lock()
_MAX_PER_BANK = 500


def _path() -> Path:
    override = os.environ.get("OMNICRAFT_CONFIG_HOME")
    base = Path(override) if override else Path.home() / ".omnicraft"
    base.mkdir(parents=True, exist_ok=True)
    return base / "agent_memory.json"


def _load() -> dict[str, list[dict[str, Any]]]:
    try:
        with _path().open(encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, list)}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


def _save(data: dict[str, list[dict[str, Any]]]) -> None:
    # Write-then-rename so a crash mid-write never corrupts the store.
    path = _path()
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as fh:
        json.dump(data, fh)
        tmp = fh.name
    os.replace(tmp, path)


@contextmanager
def _file_lock():
    """OS-level lock so concurrent server/runner processes don't lose writes."""
    lock_path = Path(f"{_path()}.lock")
    with lock_path.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


_PROJECT_LABEL = "omni_project"


def _bank_key(ctx: ToolContext) -> str:
    """Memory bank key: per-agent, further scoped by project when the session
    is filed under one (so each project keeps its own memory)."""
    base = ctx.agent_id or ctx.conversation_id or "default"
    project = None
    if ctx.conversation_id:
        try:
            from omnicraft.runtime import get_conversation_store

            conv = get_conversation_store().get_conversation(ctx.conversation_id)
            if conv is not None and conv.labels:
                project = conv.labels.get(_PROJECT_LABEL)
        except Exception:
            project = None
    return f"{base}::{project}" if project else base


def remember(ctx: ToolContext, text: str) -> dict[str, Any]:
    entry = {"id": secrets.token_hex(6), "at": int(time.time()), "text": text.strip()}
    with _lock, _file_lock():
        data = _load()
        bank = data.setdefault(_bank_key(ctx), [])
        bank.append(entry)
        del bank[:-_MAX_PER_BANK]
        _save(data)
    return entry


def recall(ctx: ToolContext, query: str | None, limit: int) -> list[dict[str, Any]]:
    with _lock, _file_lock():
        bank = list(_load().get(_bank_key(ctx), []))
    if query:
        q = query.lower()
        bank = [m for m in bank if q in str(m.get("text", "")).lower()]
    return bank[-limit:][::-1]  # most recent first


class MemoryRememberTool(Tool):
    """Store a durable fact in the agent's long-term memory."""

    @classmethod
    def name(cls) -> str:
        return "memory_remember"

    @classmethod
    def description(cls) -> str:
        return (
            "Save a durable fact, preference or decision to long-term memory so "
            "it is available in future conversations. Call this whenever the user "
            "shares something worth remembering, or asks you to remember it."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "memory_remember",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "The fact to remember, as a self-contained sentence.",
                        }
                    },
                    "required": ["text"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return "Erro: argumentos inválidos."
        text = str(args.get("text", "")).strip()
        if not text:
            return "Erro: 'text' é obrigatório."
        remember(ctx, text)
        return f"Memória salva: {text}"


class MemoryRecallTool(Tool):
    """Recall facts from the agent's long-term memory."""

    @classmethod
    def name(cls) -> str:
        return "memory_recall"

    @classmethod
    def description(cls) -> str:
        return (
            "Recall durable facts saved earlier in long-term memory. Call this "
            "BEFORE answering anything that might depend on what you already know "
            "about the user or past conversations. Optionally filter by a query."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "memory_recall",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Optional keywords to filter; omit for recent.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max memories to return (default 10).",
                        },
                    },
                    "required": [],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        query = args.get("query")
        limit = int(args.get("limit") or 10)
        memories = recall(ctx, query if isinstance(query, str) else None, max(1, min(limit, 50)))
        if not memories:
            return "Nenhuma memória encontrada."
        return "\n".join(f"- {m['text']}" for m in memories)
