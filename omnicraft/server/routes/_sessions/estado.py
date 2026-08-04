"""
Estado de processo compartilhado pelas rotas de sessão.

Primeira fatia da divisão do ``sessions.py`` (ver
``docs/plano-divisao-sessions.md``). Aqui moram os caches e registros de
tarefas que os vários caminhos das rotas leem e escrevem — nada de lógica.

**Estes objetos têm dono único.** O ``sessions.py`` os reexporta, de modo que
``from omnicraft.server.routes.sessions import _session_status_cache`` continua
devolvendo ESTE objeto, não uma cópia. Nenhum deles pode ser reatribuído: só
mutado no lugar (``cache[k] = v``, ``.pop()``, ``.discard()``). Uma
reatribuição em qualquer módulo quebraria o compartilhamento em silêncio — a
sessão apareceria ativa num caminho e ociosa no outro.

Todos são apenas de memória: repovoam a partir dos eventos ao vivo e não
sobrevivem a um reinício do servidor.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

from omnicraft.server.schemas import (
    McpServerStartup,
    SandboxStatus,
    SkillSummary,
)

# Strong-references for per-session task watchers spawned via
# ``asyncio.create_task``. Without this, asyncio's task registry only
# holds a weak reference and the GC can collect a running task before
# it finishes (the well-known RUF006 / Python ``asyncio`` footgun).
# Entries are evicted by a done-callback on the task itself.
_WATCHER_TASKS: set[asyncio.Task[None]] = set()

# Per-session status cache updated by the runner SSE relay.
# Used by _get_session_snapshot.
_session_status_cache: dict[str, str] = {}

# Per-session in-flight response id, tracked alongside _session_status_cache.
# Set when a running/waiting status edge carries a response_id (native Claude's
# turn-start edge does); popped on idle/failed. Projected onto the session
# snapshot as ``active_response_id`` so a client reconnecting mid-turn can
# reopen the streaming ``activeResponse`` and keep forwarded tool cards
# rendering LIVE — the SSE stream is "snapshot + live tail, no replay", so the
# turn-start ``running`` event is never re-sent on reconnect.
_session_active_response_cache: dict[str, str] = {}

# Per-session background-shell tally (claude-native), kept in lockstep with
# ``_session_status_cache`` so a snapshot/reload re-shows "N background tasks
# still running" after the live SSE edge is gone. The authoritative source is
# the ``Stop`` hook's ``background_tasks`` count: a positive count sets the
# tally, an explicit ``0`` clears it (so a finished shell drops the indicator
# at the next turn end), and a new turn (``running``) or a failure also clears
# it. The trailing PTY-activity ``idle`` carries no count and must NOT clear it.
#
# KNOWN LIMITATION — the tally only refreshes at a turn boundary. Claude Code
# emits no background-shell-completion hook, so a ``0`` is only ever posted by
# the next ``Stop``. If a shell exits while the session is already idle and the
# user never sends another message, no ``Stop`` fires and the indicator (chat,
# sidebar, and reloads via ``_get_session_snapshot``) can read "N background
# tasks still running" until the next turn. In practice the agent usually
# narrates the shell's completion — which IS a turn, so its ``Stop`` clears the
# tally — bounding the stale window to the next interaction. This mirrors the
# TUI's own turn-boundary update of its "N shells still running" banner.
# In-memory only — repopulates from live edges, exactly like the status cache.
_session_background_task_count_cache: dict[str, int] = {}

# Per-user read tracking, keyed by the user's discovery key (user id, or
# the shared key in single-user mode) then by session id. Mirrors the two
# values the web client used to keep in localStorage: a "last seen"
# wall-clock baseline and an explicit "marked unread" override set.
# In-memory only — like _session_status_cache it does NOT survive a server
# restart. Unlike status (rederivable from the runner), read state has no
# durable source, so a restart resets it; this is an accepted tradeoff for
# keeping it server-side (shared across a user's devices while up) without
# a DB. Entries are never pruned on session delete (bounded by churn,
# wiped on restart).
_read_last_seen: dict[str, dict[str, int]] = {}

_read_explicit_unread: dict[str, set[str]] = {}

# Sessions whose current turn was Stopped: the relay drops the turn's trailing
# response.* output (no forward, no persist). The fence lifts on the next
# turn's "running" status or on any terminal response.* event.
_interrupt_fenced_sessions: set[str] = set()

# Sessões cujo túnel do runner estamos prestes a derrubar DE PROPÓSITO, como
# parte de um Stop pedido por quem usa. O tratamento de desconexão do relay
# consulta isto para que a queda intencional vire um "parado" quieto em vez de
# um `runner_disconnected` assustador. De uso único: o próprio tratamento
# descarta, então uma desconexão real depois continua aparecendo.
_intentional_stop_sessions: set[str] = set()

# Per-session todo cache updated by external_session_todos events from the
# claude-native forwarder. Used by _build_session_response to populate the
# ``todos`` snapshot field so the panel survives page refresh.
_session_todos_cache: dict[str, list[dict[str, Any]]] = {}

# Per-session terminal-spin-up flag updated by the runner SSE relay from
# ``session.terminal_pending`` events (and self-healed when a real terminal
# resource is created). Used by _build_session_response to populate the
# ``terminal_pending`` snapshot field so a client connecting mid-spin-up
# still sees the Terminal-pill spinner. Only ``True`` entries are stored —
# the key is deleted on clear so the dict never accumulates stale ``False``
# entries for every session that ever spun up a terminal.
_session_terminal_pending_cache: dict[str, bool] = {}

# Managed-sandbox launch progress keyed by session id. Written by
# _publish_sandbox_status as the background launch pipeline advances;
# read by _build_session_response to populate the ``sandbox_status``
# snapshot field so a client opening the session mid-launch sees the
# current stage. Successful launches are evicted on "ready" (absent ==
# no launch in flight); failures are retained — mirroring
# ManagedLaunchTracker — so a reload after a dead launch still shows
# why the sandbox never came up.
_session_sandbox_status_cache: dict[str, SandboxStatus] = {}

# Per-MCP-server startup state keyed by session id. Written by
# _publish_mcp_startup as the native forwarder reports harness MCP
# startup progress; read by _build_session_response to populate the
# ``mcp_startup`` snapshot field so a client opening (or reloading) the
# session mid-startup still sees the startup band. Evicted when the
# forwarder posts an empty/settled map — absent == no startup state.
_session_mcp_startup_cache: dict[str, dict[str, McpServerStartup]] = {}

# Per-session runner-skills cache + in-flight fetch. The snapshot fetches
# these off its critical path (see _fetch_runner_skills) so the continuous
# poll can't pin the runner's event loop and wedge a turn.
_runner_skills_cache: dict[str, list[SkillSummary]] = {}

_runner_skills_inflight: dict[str, asyncio.Task[None]] = {}

# Per-session codex-native model catalog cache + in-flight fetch.
# The snapshot warms this from the bound runner's live Codex app-server
# (``model/list``) off the hot path, same shape as runner skills.
_model_options_cache: dict[str, list[dict[str, Any]]] = {}

_model_options_inflight: dict[str, asyncio.Task[None]] = {}

# (conversation_id, deciding_policy) -> lock serializing native ASK gates.
# When an agent fires several tool calls in parallel, each spawns its own
# PreToolUse hook that lands in the policy-evaluate endpoint concurrently.
# Without serialization every one of them would publish its own approval
# elicitation for the same crossed checkpoint (e.g. a cost-budget warning),
# prompting the human N times for one decision. Holding this lock across the
# human wait lets the first ASK resolve and record its approval, so the
# siblings re-evaluate to ALLOW (against the freshly persisted state) and never
# prompt again. Keyed on the deciding policy so unrelated policies' asks don't
# serialize against each other; keyed on the conversation so different sessions
# stay independent (claude/codex-native sub-agent tool calls share the parent
# conversation id, so this also covers them). A WeakValueDictionary drops a
# lock once no in-flight coroutine references it, bounding the registry without
# an eviction policy that could hand two waiters different lock objects.
_native_ask_gate_locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)

# Strong refs so deferred card-clear tasks aren't GC'd mid-sleep.
_deferred_elicitation_clear_tasks: set[asyncio.Task[None]] = set()

# Fire-and-forget tasks that ask the bound runner to pop a native-terminal
# approval modal for a parked tool-policy ASK. Kept referenced so they aren't
# garbage-collected before the POST completes.
_native_popup_forward_tasks: set[asyncio.Task[None]] = set()

# Strong references to in-flight background managed-launch tasks.
# asyncio.create_task results are weakly held by the loop; without a
# reference here a long provision could be garbage-collected mid-flight.
# Cancelled at server shutdown via cancel_managed_launch_tasks().
_managed_launch_tasks: set[asyncio.Task[None]] = set()

# Locks de compactação por sessão, para dois ``/compact`` simultâneos não
# correrem um contra o outro. Valores fracos limitam o registro sem separar
# quem espera em objetos diferentes durante a evicção.
_COMPACT_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
