"""Diagnóstico — one endpoint that checks the environment end to end.

Half of every "não funciona" is environment: no host connected, a missing CLI,
no GitHub token, the chat agent not installed, a scheduled job pointing at a
deleted agent. ``GET /v1/doctor`` runs every check the server can perform and
returns them with actionable hints, so the Settings page can render a
ready-to-act checklist instead of the user debugging blind.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any

from fastapi import APIRouter, Request

from omnicraft.server.auth import AuthProvider
from omnicraft.server.routes._auth_helpers import require_user

_LOCAL_USER = "local"

# The coding-harness CLIs the orchestrators route work to.
_WORKER_CLIS = ["claude", "codex", "opencode", "cursor-agent", "hermes", "pi"]

# Host-reported harness keys -> the CLI binary each one probes for. Used by
# the PATH-discrepancy check (4b): "server sees it, host daemon doesn't".
_HARNESS_BINARIES = {
    "codex": "codex",
    "opencode": "opencode",
    "pi": "pi",
    "pi-native": "pi",
    "native-pi": "pi",
    "hermes": "hermes",
    "native-hermes": "hermes",
    "cursor": "cursor-agent",
    "goose": "goose",
    "agy": "agy",
    "qwen": "qwen",
    "kimi": "kimi",
}


def _check(
    check_id: str, label: str, ok: bool, detail: str, hint: str | None = None
) -> dict[str, Any]:
    return {"id": check_id, "label": label, "ok": ok, "detail": detail, "hint": hint}


def create_doctor_router(
    agent_store: Any,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the router for ``GET /v1/doctor``."""
    router = APIRouter()

    @router.get("/doctor")
    async def doctor(request: Request) -> dict[str, Any]:
        user_id = require_user(request, auth_provider) or _LOCAL_USER
        checks: list[dict[str, Any]] = []

        # 1. A host is connected (nothing runs without one).
        registry = getattr(request.app.state, "host_registry", None)
        online = list(registry.online_host_ids()) if registry is not None else []
        checks.append(
            _check(
                "host",
                "Máquina conectada",
                len(online) > 0,
                f"{len(online)} máquina(s) online" if online else "nenhuma máquina online",
                None if online else "Abra o app desktop ou rode 'omnicraft host' na máquina.",
            )
        )

        # 2. The no-filesystem chat agent backs the Início tab.
        try:
            chat_ok = agent_store.get_by_name("chat") is not None
        except Exception:  # noqa: BLE001 — a store hiccup reads as "not installed"
            chat_ok = False
        checks.append(
            _check(
                "chat_agent",
                "Agente de Chat instalado",
                chat_ok,
                "agente 'chat' registrado" if chat_ok else "agente 'chat' ausente",
                None if chat_ok else "Instale o agente 'chat' na Galeria (Craftwork › Galeria).",
            )
        )

        # 3. GitHub token for the integration page.
        gh_token = bool(os.environ.get("GITHUB_TOKEN") or shutil.which("gh"))
        checks.append(
            _check(
                "github",
                "GitHub configurado",
                gh_token,
                "token/gh CLI disponível" if gh_token else "sem GITHUB_TOKEN e sem gh CLI",
                None if gh_token else "Rode 'gh auth login' ou exporte GITHUB_TOKEN.",
            )
        )

        # 4. Worker CLIs on PATH (meaningful when the server runs on the same
        # machine as the host — the local desktop case).
        found = [cli for cli in _WORKER_CLIS if shutil.which(cli)]
        missing = [cli for cli in _WORKER_CLIS if cli not in found]
        checks.append(
            _check(
                "workers",
                "CLIs de agentes de código",
                len(found) > 0,
                f"disponíveis: {', '.join(found) or 'nenhum'}",
                (
                    f"Faltando: {', '.join(missing)} — instale os que quiser usar."
                    if missing
                    else None
                ),
            )
        )

        # 4a. Readiness of the `computer` tool. Only meaningful on macOS: it
        # drives the screen with `screencapture` and the pointer/keyboard with
        # `cliclick`. The TCC permissions can't be read without prompting for
        # them, so the check reports what the tool needs and who has to hold it
        # — the prompt lands on the RUNNER's process, not on the server.
        if sys.platform == "darwin":
            has_cliclick = shutil.which("cliclick") is not None
            checks.append(
                _check(
                    "computer_control",
                    "Controle do computador",
                    has_cliclick,
                    (
                        "cliclick disponível"
                        if has_cliclick
                        else "cliclick ausente — só screenshot funcionaria"
                    ),
                    (
                        "Conceda Gravação de Tela e Acessibilidade ao processo do runner "
                        "em Ajustes → Privacidade e Segurança. O macOS pede na primeira "
                        "vez que a ferramenta agir."
                        if has_cliclick
                        else "Instale com 'brew install cliclick' e depois conceda Gravação "
                        "de Tela e Acessibilidade ao processo do runner em Ajustes → "
                        "Privacidade e Segurança."
                    ),
                )
            )

        # 4b. PATH discrepancy: a CLI the server finds but the HOST daemon
        # reported as binary-missing means the tool is installed in the
        # user's shell (nvm/brew) yet invisible to the daemon's PATH --
        # workers won't boot, and "install it" advice would be wrong.
        host_store = getattr(request.app.state, "host_store", None)
        shadowed: list[str] = []
        if host_store is not None:
            try:
                hosts = host_store.list_hosts(user_id)
            except Exception:  # noqa: BLE001 -- a store hiccup reads as "no hosts"
                hosts = []
            for host in hosts:
                for harness, availability in (host.configured_harnesses or {}).items():
                    binary = _HARNESS_BINARIES.get(harness)
                    if (
                        binary is not None
                        and str(availability) == "binary-missing"
                        and shutil.which(binary)
                        and binary not in shadowed
                    ):
                        shadowed.append(binary)
        checks.append(
            _check(
                "host_path",
                "PATH do host enxerga as CLIs",
                not shadowed,
                (
                    "nenhuma discrepância entre o shell e o host"
                    if not shadowed
                    else "instaladas no shell mas invisíveis ao host: " + ", ".join(shadowed)
                ),
                (
                    None
                    if not shadowed
                    else (
                        "O daemon do host não tem o PATH do seu shell (nvm/brew). "
                        'Corrija com: ln -sf "$(command -v '
                        + shadowed[0]
                        + ')" ~/.local/bin/ (e o node, se for CLI npm) -- ou '
                        "reinicie o host após atualizar o OmniCraft."
                    )
                ),
            )
        )

        # 5. Scheduled jobs pointing at agents that no longer exist — they
        # error on every fire until fixed.
        from omnicraft.server import scheduled_agents as sched

        broken: list[str] = []
        for job in sched.list_jobs(owner=user_id):
            try:
                if agent_store.get_by_name(job.get("agent_name") or "") is None:
                    broken.append(str(job.get("name")))
            except Exception:  # noqa: BLE001 — treat lookup failure as broken
                broken.append(str(job.get("name")))
        checks.append(
            _check(
                "scheduled",
                "Agendamentos íntegros",
                not broken,
                (
                    "todos os jobs apontam para agentes instalados"
                    if not broken
                    else f"jobs com agente ausente: {', '.join(broken[:5])}"
                ),
                None
                if not broken
                else "Edite o job e escolha um agente instalado, ou reinstale o agente.",
            )
        )

        # 6. Push subscriptions — the channel scheduled results/failures use.
        try:
            from omnicraft.server import push as _push

            has_push = len(_push.get_subscriptions(user_id)) > 0
        except Exception:  # noqa: BLE001 — push store optional
            has_push = False
        checks.append(
            _check(
                "push",
                "Notificações push",
                has_push,
                "dispositivo inscrito" if has_push else "nenhum dispositivo inscrito",
                (
                    None
                    if has_push
                    else "Ative notificações no app para receber resultados de jobs agendados."
                ),
            )
        )

        ok_count = sum(1 for c in checks if c["ok"])
        return {"checks": checks, "ok": ok_count, "total": len(checks)}

    return router
