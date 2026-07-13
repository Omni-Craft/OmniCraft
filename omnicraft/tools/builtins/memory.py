"""Built-in local long-term memory — no external service, no API key.

A tiny file-backed memory an agent uses to remember durable facts across
sessions. When the agent runs in a workspace (Code/Craftwork), memory lives in
a **project** folder — ``<workspace>/.omnicraft/memory/memory.json`` — so it
travels with the repo (portable, versionable, shareable). When there is no
filesystem workspace (the no-FS Chat agent, tests), it falls back to a global
store under the OmniCraft config home, keyed per agent and project. No
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
_PROJECT_LABEL = "omni_project"

_README = """\
# .omnicraft/

Pasta de projeto do OmniCraft (análoga ao `.claude/`). Guarda dados que devem
viajar com este repositório.

- `memory/memory.json` — memória de longo prazo dos agentes que trabalham neste
  projeto. Versione junto com o código para compartilhar com o time, ou
  adicione ao `.gitignore` se preferir mantê-la pessoal.
"""


def _global_path() -> Path:
    override = os.environ.get("OMNICRAFT_CONFIG_HOME")
    base = Path(override) if override else Path.home() / ".omnicraft"
    base.mkdir(parents=True, exist_ok=True)
    return base / "agent_memory.json"


def _project_file(ctx: ToolContext) -> Path | None:
    """The project's ``.omnicraft/memory/memory.json`` path, or ``None`` when
    there is no usable filesystem workspace (the no-FS Chat agent, tests)."""
    ws = getattr(ctx, "workspace", None)
    if not ws:
        return None
    try:
        base = Path(ws)
    except (TypeError, ValueError):
        return None
    if not base.is_dir():
        return None
    return base / ".omnicraft" / "memory" / "memory.json"


def _bank_key(ctx: ToolContext) -> str:
    """Global-store bank key: per-agent, further scoped by project label when the
    session is filed under one (so each project keeps its own memory)."""
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


def _project_key(ctx: ToolContext) -> str:
    """Bank key WITHIN a project store — the file is already project-scoped, so
    only the agent id is needed (multiple agents keep separate banks)."""
    return ctx.agent_id or ctx.conversation_id or "default"


def _load(path: Path) -> dict[str, list[dict[str, Any]]]:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, list)}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


def _save(path: Path, data: dict[str, list[dict[str, Any]]]) -> None:
    # Write-then-rename so a crash mid-write never corrupts the store.
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as fh:
        json.dump(data, fh)
        tmp = fh.name
    os.replace(tmp, path)


@contextmanager
def _file_lock(path: Path):
    """OS-level lock so concurrent server/runner processes don't lose writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{path}.lock")
    with lock_path.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _seed_project_from_global(ctx: ToolContext, proj_file: Path) -> None:
    """First time a project store is written, migrate the matching global bank
    into it so existing memory isn't lost. Copies (leaves the global as a
    backup); once the project file exists it becomes the source of truth."""
    if proj_file.exists():
        return
    with _file_lock(_global_path()):
        seed = list(_load(_global_path()).get(_bank_key(ctx), []))
    proj_file.parent.mkdir(parents=True, exist_ok=True)
    (proj_file.parent.parent / "README.md").write_text(_README, encoding="utf-8")
    with _file_lock(proj_file):
        if not proj_file.exists():
            _save(proj_file, {_project_key(ctx): seed} if seed else {})


def remember(ctx: ToolContext, text: str) -> dict[str, Any]:
    entry = {"id": secrets.token_hex(6), "at": int(time.time()), "text": text.strip()}
    proj = _project_file(ctx)
    if proj is not None:
        _seed_project_from_global(ctx, proj)
        path, key = proj, _project_key(ctx)
    else:
        path, key = _global_path(), _bank_key(ctx)
    with _lock, _file_lock(path):
        data = _load(path)
        bank = data.setdefault(key, [])
        bank.append(entry)
        del bank[:-_MAX_PER_BANK]
        _save(path, data)
    return entry


def recall(ctx: ToolContext, query: str | None, limit: int) -> list[dict[str, Any]]:
    proj = _project_file(ctx)
    if proj is not None and proj.exists():
        path, key = proj, _project_key(ctx)
    else:
        # No project store yet (or no workspace): read the global bank so recall
        # works before the first project write migrates it.
        path, key = _global_path(), _bank_key(ctx)
    with _lock, _file_lock(path):
        bank = list(_load(path).get(key, []))
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
