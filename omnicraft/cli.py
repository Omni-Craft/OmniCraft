"""CLI entry point for omnicraft."""

from __future__ import annotations

import collections.abc
import contextlib
import copy
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, TypeAlias, cast

import click
import yaml
from pydantic import BaseModel, ConfigDict
from rich import box
from rich.console import Console
from rich.table import Table

from omnicraft._platform import IS_WINDOWS, resolve_repo_symlink
from omnicraft._startup_profile import StartupProfiler
from omnicraft.cli_sandbox import lakebox as _lakebox_alias_group
from omnicraft.cli_sandbox import sandbox as _sandbox_group
from omnicraft.config import (
    global_config_path,
    load_global_config,
    load_local_config,
)
from omnicraft.harness_aliases import canonicalize_harness
from omnicraft.host.local_server import (
    _DEFAULT_LOCAL_PORT,
    _pid_alive,
    ensure_local_omnicraft_server,
    local_server_status,
    local_server_url_if_healthy,
    server_config_signature,
    stop_local_omnicraft_server,
    stop_untracked_local_server,
)
from omnicraft.inner import _proc, ui
from omnicraft.onboarding.sandboxes import available_providers as _sandbox_providers
from omnicraft.onboarding.ucode_setup import (
    build_ucode_configure_command,
    find_ucode_command,
    model_gateway_workspace_urls,
)

if TYPE_CHECKING:
    import httpx

    from omnicraft._runner_startup import RunnerStartupProgress
    from omnicraft.onboarding.ambient import DetectedProvider
    from omnicraft.onboarding.provider_config import ProviderEntry
    from omnicraft.update_check import _InstalledWheelInfo


# Any: YAML configs have heterogeneous value types (str, int, list, etc.)
def _load_config(path: str | None) -> dict[str, Any]:  # type: ignore[explicit-any]
    """
    Load and return config from a YAML file.
    Returns an empty dict if no path is provided.
    """
    if path is None:
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _server_uvicorn_log_config() -> dict[str, Any]:  # type: ignore[explicit-any]
    """
    Return Uvicorn logging config with request-duration access logs.

    Uvicorn emits the FastAPI access line itself, so OmniCraft swaps
    only the access formatter while preserving Uvicorn's default
    handlers, levels, and server-log formatting.

    :returns: Uvicorn ``log_config`` suitable for ``uvicorn.run``.
    """
    import uvicorn.config

    log_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    log_config["formatters"]["access"]["()"] = (
        "omnicraft.server.performance_metrics.RequestDurationAccessFormatter"
    )
    return log_config


# Path to the user-level global config file, analogous to ~/.gitconfig.
# Tests may set ``OMNICRAFT_CONFIG_HOME`` to isolate subprocesses from a
# developer's real ``~/.omnicraft/config.yaml``.
_CONFIG_HOME_ENV_VAR = "OMNICRAFT_CONFIG_HOME"
_GLOBAL_CONFIG_PATH: Path = Path.home() / ".omnicraft" / "config.yaml"

# Per-user state directories before / after the omniagents -> omnicraft rename.
# All per-user state (config, registered agents, auth tokens, the host daemon
# pidfile, runner identity, native session state, logs) lives under
# :data:`_STATE_DIR`; :func:`_migrate_legacy_state_dir` relocates the old
# directory on first run. ``OMNICRAFT_DATA_DIR`` is the data-isolation override
# a worktree / test sets; when present the user manages their own state and
# migration is skipped.
_STATE_DIR: Path = Path.home() / ".omnicraft"
# Pre-rename state directories, newest first. The name evolved
# ``~/.omniagents`` -> ``~/.omnicrafts`` -> ``~/.omnicraft``; migrate from the
# newest legacy directory that still exists.
_LEGACY_STATE_DIRS: tuple[Path, ...] = (
    Path.home() / ".omnicrafts",
    Path.home() / ".omniagents",
)
_DATA_DIR_ENV_VAR = "OMNICRAFT_DATA_DIR"


def _migrate_legacy_state_dir() -> None:
    """
    One-time relocation of a pre-rename state directory to ``~/.omnicraft``.

    Earlier releases stored all per-user state under ``~/.omniagents`` and then
    ``~/.omnicrafts`` as the name evolved. To avoid silently losing that state,
    move the newest surviving legacy directory to ``~/.omnicraft`` on first run,
    but only when **all** of the following hold:

    - the new ``~/.omnicraft`` does not yet exist (never clobber new state),
    - at least one directory in :data:`_LEGACY_STATE_DIRS` exists,
    - neither :data:`_CONFIG_HOME_ENV_VAR` nor :data:`_DATA_DIR_ENV_VAR` is set
      (an operator who redirects state elsewhere manages it themselves), and
    - no live host daemon is running out of that legacy directory -- moving its
      pidfile / socket dir out from under a running daemon would wedge it.

    On failure the migration is skipped with a warning rather than crashing the
    CLI; a fresh ``~/.omnicraft`` is then created normally and the legacy
    directory is left untouched for the user to migrate by hand. Idempotent:
    once ``~/.omnicraft`` exists this is a no-op.

    :returns: ``None``.
    """
    if _STATE_DIR.exists():
        return
    if os.environ.get(_CONFIG_HOME_ENV_VAR) or os.environ.get(_DATA_DIR_ENV_VAR):
        return
    legacy_src = next((d for d in _LEGACY_STATE_DIRS if d.exists()), None)
    if legacy_src is None:
        return

    # Guard: a daemon spawned by the old release may still be running with its
    # pidfile + unix socket under the legacy dir. Relocating those would leave
    # the daemon orphaned and the CLI unable to find it.
    legacy_pid_file = legacy_src / "host.pid"
    if legacy_pid_file.exists():
        try:
            first_line = legacy_pid_file.read_text().strip().splitlines()[0]
            legacy_pid = int(first_line)
        except (ValueError, OSError, IndexError):
            legacy_pid = None
        if legacy_pid is not None and _pid_alive(legacy_pid):
            click.echo(
                f"Nota: encontrado estado pré-renomeação em {legacy_src}, mas um "
                "daemon host ainda está rodando a partir dele; migração pulada. Rode "
                "`omnicraft stop` e rode de novo para migrar, ou mova manualmente "
                "para ~/.omnicraft.",
                err=True,
            )
            return

    try:
        shutil.move(str(legacy_src), str(_STATE_DIR))
    except OSError as exc:
        click.echo(
            f"Nota: não foi possível migrar {legacy_src} para ~/.omnicraft ({exc}); "
            f"iniciando com estado novo. Seus dados antigos estão intactos em {legacy_src}.",
            err=True,
        )
        return
    click.echo(f"Estado por usuário migrado de {legacy_src} para ~/.omnicraft.", err=True)


# Project-level config relative to cwd, analogous to .git/config.
# Resolved at call time so tests can control cwd.
_LOCAL_CONFIG_RELPATH: Path = Path(".omnicraft") / "config.yaml"

# Keys that ``omnicraft config`` accepts.  Mirrors the option names in
# the ``run`` command so the mapping is explicit and auditable.
_AUTO_OPEN_CONVERSATION_CONFIG_KEY = "auto_open_conversation"
_GLOBAL_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "default_agent",
        "harness",
        "model",
        # OpenCode-specific default model (``provider/model``) the native
        # ``omni opencode`` TUI launches on; set via `omni setup` → OpenCode.
        "opencode_model",
        "server",
        _AUTO_OPEN_CONVERSATION_CONFIG_KEY,
    }
)
_BOOLEAN_CONFIG_KEYS: frozenset[str] = frozenset({_AUTO_OPEN_CONVERSATION_CONFIG_KEY})
_CONFIG_TRUE_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})
_CONFIG_FALSE_VALUES: frozenset[str] = frozenset({"0", "false", "no", "off"})
_ConfigValue: TypeAlias = (
    str | int | float | bool | None | list["_ConfigValue"] | dict[str, "_ConfigValue"]
)

_GLOBAL_AGENTS_DIR: Path = Path.home() / ".omnicraft" / "agents"
_INTERNAL_BETA_DEFAULT_AGENT_NAME: str = "databricks_coding_agent.yaml"
_INTERNAL_BETA_BUNDLED_AGENTS: tuple[str, ...] = (
    "databricks_coding_agent.yaml",
    "knowledge_work_agent.yaml",
)
# _INTERNAL_BETA_DEFAULT_SERVER (internal Databricks Apps host) moved to
# omnicraft.onboarding.internal_beta (excluded from the OSS build); the
# internal-beta setup branch and the sandbox CLI import it from there.
_CLAUDE_STARTUP_PROFILE_ENV_VAR = "OMNICRAFT_CLAUDE_STARTUP_PROFILE"
# Brand shown for an auto-configured CLI login in the credentials callout —
# the product the login authenticates, not the CLI name (the codex CLI logs in
# a ChatGPT subscription). Keyed by the ambient detection name; these are the
# only two subscription CLIs ambient detection emits.
_CLI_LOGIN_BRAND: dict[str, str] = {"claude": "Claude", "codex": "ChatGPT"}
_HOST_DAEMON_STOP_GRACE_S = 5.0
# How often ``omni upgrade`` re-polls the local server for in-flight
# (connected) sessions while draining before it stops the server.
_UPGRADE_DRAIN_POLL_S = 2.0
# When reusing an existing daemon, how long to let a live-but-offline daemon
# (re)establish its server tunnel before treating it as a zombie and
# respawning. Covers the daemon's reconnect backoff after a transient drop.
_DAEMON_RECONNECT_GRACE_S = 5.0
# Don't tear down a daemon younger than this for an offline tunnel: it may be
# a freshly-spawned daemon (possibly from a concurrent invocation) still
# bringing its tunnel up. Avoids racing/thrashing sibling invocations.
_DAEMON_REUSE_MIN_AGE_S = 6.0

# How long uvicorn waits for active connections (WebSocket, SSE) after
# SIGTERM before force-closing them.  SSE streams signal themselves via
# session_stream.shutdown_all() in _ShutdownSignalingServer.shutdown(),
# so the main remaining consumers of this window are WebSocket tunnels
# that need a moment to drain.  5 s is enough for a clean tunnel teardown
# while keeping Ctrl-C feeling instant.
# Overridable via OMNICRAFT_SERVER_SHUTDOWN_TIMEOUT_S for deployments that
# need a longer drain window (e.g. large file uploads).
_SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S_DEFAULT = 5
_SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S = int(
    os.environ.get(
        "OMNICRAFT_SERVER_SHUTDOWN_TIMEOUT_S",
        str(_SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S_DEFAULT),
    )
)

_LOCAL_DAEMON_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "AWS_BEARER_TOKEN_BEDROCK",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
        "COHERE_API_KEY",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GROQ_API_KEY",
        "MISTRAL_API_KEY",
        "OMNICRAFT_DATABASE_URI",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION",
        "OPENROUTER_API_KEY",
        "PERPLEXITY_API_KEY",
        "TOGETHER_API_KEY",
        "VOYAGE_API_KEY",
        "XAI_API_KEY",
    }
)
_LOCAL_DAEMON_ENV_PREFIXES: tuple[str, ...] = (
    "ANTHROPIC_DEFAULT_",
    "AZURE_OPENAI_",
    "DATABRICKS_",
    "MLFLOW_",
    "OTEL_",
    "OMNICRAFT_",
    "OPENAI_",
)
_HostJsonValue: TypeAlias = (
    str | int | float | bool | None | list["_HostJsonValue"] | dict[str, "_HostJsonValue"]
)
_HostJsonObject: TypeAlias = dict[str, _HostJsonValue]
_HostSessionRow: TypeAlias = dict[str, _HostJsonValue]
_HostPayload: TypeAlias = dict[str, _HostJsonValue]


def _effective_global_config_path() -> Path:
    """
    Return the path to the user-level OmniCraft config.

    :returns: ``$OMNICRAFT_CONFIG_HOME/config.yaml`` when the env
        override is set, otherwise :data:`_GLOBAL_CONFIG_PATH`.
    """
    return global_config_path(_GLOBAL_CONFIG_PATH)


def _display_path(path: Path) -> str:
    """
    Format a filesystem path for display, collapsing the home prefix to ``~``.

    A path under the user's home directory is shown as ``~/...`` for
    readability; anything else is shown as its plain string. Unlike a
    hardcoded ``~/.omnicraft/...`` literal, this reflects the *actual*
    effective path — so a state dir outside ``$HOME`` (an
    ``OMNICRAFT_CONFIG_HOME`` / ``OMNICRAFT_DATA_DIR`` override) renders as
    its real location rather than a misleading ``~``.

    :param path: The path to display, e.g.
        ``Path("/Users/alice/.omnicraft/logs/server/local-server-ab12.log")``.
    :returns: ``"~/.omnicraft/..."`` when *path* is under ``$HOME``,
        otherwise ``str(path)``.
    """
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        # Not under $HOME (e.g. an OMNICRAFT_DATA_DIR outside home).
        return str(path)


def _display_config_path(path: Path) -> str:
    """
    Format a config path for display, collapsing the home prefix to ``~``.

    Thin wrapper over :func:`_display_path` kept for call-site readability
    where the path is specifically the effective config file.

    :param path: The config path to display, e.g.
        ``Path("/Users/alice/.omnicraft/config.yaml")``.
    :returns: ``"~/.omnicraft/config.yaml"`` when *path* is under
        ``$HOME``, otherwise ``str(path)``.
    """
    return _display_path(path)


def _load_global_config() -> dict[str, Any]:  # type: ignore[explicit-any]
    """
    Load the global omnicraft config from ``~/.omnicraft/config.yaml``.

    Returns an empty dict when the file does not exist or is empty.
    Top-level default keys (``default_agent``, ``server``,
    ``model``, ``harness``) hold plain string values.  The optional
    ``auto_open_conversation`` key is a boolean. The optional
    ``auth:`` key holds a nested mapping —
    ``{"type": "databricks", "profile": "oss"}`` or
    ``{"type": "api_key", "api_key": "…"}`` — written by
    ``omnicraft setup`` and used by the runtime to supply executor
    credentials when an agent spec does not declare ``executor.auth``.

    :returns: Parsed YAML as a dict, e.g.
        ``{"default_agent": "examples/hello_world.yaml",
        "auth": {"type": "databricks", "profile": "oss"}}``.
    """
    return load_global_config(_effective_global_config_path())


def _load_local_config() -> dict[str, Any]:  # type: ignore[explicit-any]
    """
    Load the project-level config from ``.omnicraft/config.yaml`` in cwd.

    Returns an empty dict when the file does not exist or is empty.

    :returns: Parsed YAML as a dict.
    """
    return load_local_config(Path.cwd() / _LOCAL_CONFIG_RELPATH)


def _load_effective_config() -> dict[str, Any]:  # type: ignore[explicit-any]
    """
    Merge global and project-level config.

    Precedence (highest last): global (``~/.omnicraft/config.yaml``)
    → local (``.omnicraft/config.yaml`` in cwd).  Project config
    always wins so per-repo settings override user defaults.

    :returns: Merged config dict.
    """
    return {**_load_global_config(), **_load_local_config()}


def _peek_default_agent_harness(target: str) -> str | None:
    """
    Return the canonical harness declared by a default-agent YAML, or ``None``.

    Reads ``executor.harness`` / ``executor.type`` from a local YAML path so
    :func:`_resolve_default_agent_target` can compare it to an explicit
    ``--harness``. Returns ``None`` for URLs, missing/unreadable files, or
    specs that declare no harness — the caller treats ``None`` as "cannot
    confirm a match".

    :param target: The configured ``default_agent`` value, e.g.
        ``"/Users/me/.omnicraft/agents/databricks_coding_agent.yaml"``.
    :returns: The canonical harness, e.g. ``"openai-agents-sdk"``, or ``None``.
    """
    if "://" in target:
        return None
    path = Path(target).expanduser()
    if not path.is_file():
        return None
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    executor = raw.get("executor")
    if not isinstance(executor, dict):
        return None
    declared = executor.get("harness") or executor.get("type")
    if not isinstance(declared, str) or not declared:
        return None
    return canonicalize_harness(declared) or declared


@dataclass(frozen=True)
class _FirstRunPlan:
    """The harness + optional default agent a bare ``run`` should launch.

    Derived fresh from the configured credentials on each bare ``run`` and
    never persisted (see :func:`_resolve_first_run_plan`).

    :param harness: The canonical harness id to launch, e.g. ``"claude-sdk"``.
    :param agent: The default agent target to launch (the bundled fucho path
        for Claude), or ``None`` for a bare harness REPL (codex / pi).
    """

    harness: str
    agent: str | None


def _bundled_example_path(name: str) -> str:
    """Return the filesystem path to a bundled example agent directory.

    Located via the packaged ``omnicraft.resources.examples`` (symlinks to
    ``examples/<name>`` in a dev checkout, real directories in an installed
    wheel), mirroring how the model catalog is located.

    :param name: Bundled example directory name, e.g. ``"fucho"``.
    :returns: Absolute path string to the agent directory.
    """
    import importlib.resources

    resource = importlib.resources.files("omnicraft.resources.examples").joinpath(name)
    # On a no-symlink Windows checkout the packaged symlink is a stub text file;
    # dereference it to the real examples/<name> directory.
    return str(resolve_repo_symlink(Path(str(resource))))


def _pick_first_run_harness() -> _FirstRunPlan | None:
    """Pick the harness a bare first ``run`` should launch, by configured creds.

    Priority Claude → Codex → Pi over the ambient-merged config (a detected env
    key / CLI login counts as configured). Claude gets the bundled fucho
    orchestrator as its default agent; Codex / Pi launch a bare harness REPL.
    Shared with ``configure harnesses`` via
    :func:`~omnicraft.onboarding.provider_config.default_provider_for_harness`,
    so the two surfaces agree on "what's configured".

    :returns: A :class:`_FirstRunPlan`, or ``None`` when no harness has a usable
        credential.
    """
    from omnicraft.onboarding.detected import effective_config_with_detected
    from omnicraft.onboarding.provider_config import (
        default_provider_for_harness,
        load_config,
    )

    config = effective_config_with_detected(load_config())
    if default_provider_for_harness(config, "claude-sdk") is not None:
        return _FirstRunPlan(harness="claude-sdk", agent=_bundled_example_path("fucho"))
    if default_provider_for_harness(config, "codex") is not None:
        return _FirstRunPlan(harness="codex", agent=None)
    if default_provider_for_harness(config, "pi") is not None:
        return _FirstRunPlan(harness="pi", agent=None)
    # Kimi authenticates against its own backend (``kimi login`` OAuth or a
    # Moonshot API key) rather than the ambient-detected provider config, so
    # ``default_provider_for_harness`` can't gate it. Fall back to "binary
    # installed" as the readiness proxy: the executor will fail loud at the
    # first turn if no provider is actually configured.
    from omnicraft.onboarding.harness_install import KIMI_KEY, harness_cli_installed

    if harness_cli_installed(KIMI_KEY):
        return _FirstRunPlan(harness="kimi", agent=None)
    return None


def _resolve_first_run_plan() -> _FirstRunPlan | None:
    """Resolve the harness + default agent for a bare ``omnicraft run``.

    Adopts ambient-detected credentials, then picks a harness from what's
    configured (Claude→fucho / Codex / Pi). When nothing is configured,
    prints a notice, drops the user into ``configure harnesses``, then
    re-checks once.

    The pick is **deliberately not persisted** as a global default: it is
    derived state, recomputed on every bare ``run`` from the *current*
    credentials. So a user who starts with only Codex (→ a codex REPL) and
    later adds Claude is promoted to fucho on their next bare ``run`` —
    keeping fucho as the primary experience — rather than being pinned to
    the earlier fallback. An *explicit* default (a user-set global
    ``harness`` / ``default_agent``, or ``run <agent>`` / ``--harness``)
    still short-circuits this path upstream and is always honored.

    :returns: The chosen :class:`_FirstRunPlan`, or ``None`` when the user still
        has no configured harness after the configure step — the caller exits
        cleanly rather than erroring.
    """
    # Adopt any ambient creds so a detected key/login becomes a real provider
    # default, exactly as opening `configure harnesses` does (and announce what
    # was auto-configured, so a never-set-up user sees which credentials we
    # picked up). This persists *credentials* (the provider layer), NOT the
    # agent/harness pick — the pick stays ephemeral so it tracks whatever creds
    # are currently available.
    _adopt_ambient_credentials()

    plan = _pick_first_run_harness()
    if plan is None:
        ui.warn("Nenhum harness configurado encontrado.")
        _run_configure_harnesses_interactive()
        plan = _pick_first_run_harness()
    return plan


def _resolve_default_agent_target(
    default_agent: str | None,
    requested_harness: str | None,
) -> str | None:
    """
    Decide the ``run`` target when no AGENT was passed on the command line.

    - No ``default_agent`` → ``None`` (the no-AGENT ``--harness`` launcher
      builds an ad-hoc spec, or ``run`` errors when no harness either).
    - No ``--harness`` → the ``default_agent`` (the configured default
      experience, unchanged).
    - ``--harness X`` given with a ``default_agent`` whose harness is ``Y``:
      use the ``default_agent`` when ``Y == X`` (harness matches, so the user
      gets their richer configured agent); otherwise **warn** and return
      ``None`` so a minimal built-in ``X`` agent launches instead of forcing
      ``X`` onto a ``Y``-shaped spec (which would, e.g., point claude-sdk at a
      gpt model and 400 with an API-type mismatch). When ``Y`` can't be
      determined, fall back to the minimal launcher silently (can't assert a
      mismatch, but also can't confirm a match).

    :param default_agent: The configured ``default_agent`` value, or ``None``.
    :param requested_harness: The explicit ``--harness`` value, or ``None``.
    :returns: The target to run (``default_agent`` path) or ``None`` to use
        the no-AGENT launcher.
    """
    if not default_agent:
        return None
    if requested_harness is None:
        return default_agent
    requested = canonicalize_harness(requested_harness) or requested_harness
    default_harness = _peek_default_agent_harness(str(default_agent))
    if default_harness == requested:
        return default_agent
    if default_harness is not None:
        click.echo(
            f"omnicraft: o agente padrão '{default_agent}' usa o harness "
            f"{default_harness!r}, mas você especificou --harness {requested!r}; "
            f"iniciando um agente {requested!r} interno mínimo no lugar.",
            err=True,
        )
    return None


def _parse_config_bool(key: str, value: _ConfigValue) -> bool:
    """
    Parse a boolean value from YAML or ``omnicraft config KEY=VALUE``.

    :param key: Config key being parsed, e.g.
        ``"auto_open_conversation"``.
    :param value: Raw value from YAML or CLI parsing, e.g. ``"true"``.
    :returns: Parsed boolean value.
    :raises click.ClickException: If *value* is not a supported boolean.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _CONFIG_TRUE_VALUES:
            return True
        if normalized in _CONFIG_FALSE_VALUES:
            return False
    raise click.ClickException(
        f"A chave de config {key!r} deve ser um booleano (true/false, yes/no, on/off ou 1/0)."
    )


def _resolve_auto_open_conversation_setting(cfg: dict[str, Any]) -> bool | None:  # type: ignore[explicit-any]
    """
    Resolve the explicit ``auto_open_conversation`` config value, if set.

    Tri-state on purpose so callers can distinguish "the user has not
    expressed a preference" (``None``) from an explicit opt-in/opt-out.
    ``omnicraft run`` uses this to default the browser-open ON for
    interactive launches while still honoring an explicit
    ``auto_open_conversation: false``; see :func:`run`.

    :param cfg: Effective config dict from :func:`_load_effective_config`,
        e.g. ``{"auto_open_conversation": True}``.
    :returns: ``True`` / ``False`` when the key is present, or ``None``
        when the user has not configured it.
    :raises click.ClickException: If the configured value is not a
        supported boolean.
    """
    raw = cfg.get(_AUTO_OPEN_CONVERSATION_CONFIG_KEY)
    if raw is None:
        return None
    return _parse_config_bool(_AUTO_OPEN_CONVERSATION_CONFIG_KEY, raw)


def _resolve_auto_open_conversation_from_config(cfg: dict[str, Any]) -> bool:  # type: ignore[explicit-any]
    """
    Resolve whether CLI launches should open conversation URLs.

    Defaults to ``False`` when the user has not configured the key.
    ``omnicraft run`` does not use this resolver — it defaults the
    browser-open ON for interactive launches via
    :func:`_resolve_auto_open_conversation_setting`.

    :param cfg: Effective config dict from :func:`_load_effective_config`,
        e.g. ``{"auto_open_conversation": True}``.
    :returns: ``True`` when conversation links should be opened
        automatically.
    :raises click.ClickException: If the configured value is not a
        supported boolean.
    """
    setting = _resolve_auto_open_conversation_setting(cfg)
    return setting if setting is not None else False


def _save_global_config(  # type: ignore[explicit-any]
    # Any (matching the yaml-boundary helpers above): config values are
    # heterogeneous YAML scalars and nested mappings — e.g. the providers:
    # block, whose entries come back as dict[str, object] from
    # provider_entry_settings / set_default_provider. _ConfigValue can't
    # express that interop without invariance errors against those object
    # returns, so this stays the same Any boundary _load_*_config uses.
    settings: Mapping[str, Any],
    unset_keys: tuple[str, ...] = (),
    deep_merge_keys: tuple[str, ...] = (),
) -> None:
    """
    Merge *settings* into ``~/.omnicraft/config.yaml`` and remove any
    keys listed in *unset_keys*.

    Creates the ``~/.omnicraft/`` directory if it does not exist.
    Values may be plain strings, booleans, or nested mappings (the
    ``auth:`` block written by ``omnicraft setup``, or a ``providers:``
    block written by ``omnicraft setup --no-internal-beta``).

    By default every key in *settings* **replaces** the existing value
    wholesale (a shallow ``dict.update``). For keys listed in
    *deep_merge_keys*, the incoming mapping is instead merged one level
    deep into the existing mapping for that key — so passing a single
    provider under ``providers:`` adds/updates that one entry without
    dropping the others. Use the default (shallow replace) when the new
    mapping must become the *entire* block (e.g. after
    :func:`~omnicraft.onboarding.provider_config.set_default_provider`,
    which clears sibling ``default`` flags a deep-merge could not reach).

    :param settings: Key/value pairs to set, e.g.
        ``{"default_agent": "/abs/path/agent.yaml",
        "auto_open_conversation": True,
        "auth": {"type": "databricks", "profile": "oss"}}``.
    :param unset_keys: Keys to remove from the config, e.g.
        ``("server",)``.
    :param deep_merge_keys: Keys whose mapping value should be merged
        one level deep into the existing mapping rather than replacing
        it, e.g. ``("providers",)`` to add one provider entry without
        dropping the rest.
    """
    cfg = _load_global_config()
    for key, value in settings.items():
        if key in deep_merge_keys and isinstance(value, Mapping):
            existing = cfg.get(key)
            merged = dict(existing) if isinstance(existing, Mapping) else {}
            merged.update(value)
            cfg[key] = merged
        else:
            cfg[key] = value
    for key in unset_keys:
        cfg.pop(key, None)
    path = _effective_global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=True)


def _materialize_bundled_example(name: str) -> Path:
    """
    Copy a single bundled example YAML into the user config dir.

    ``uv tool install`` installs package files, not the repository checkout, so the
    top-level ``examples/<name>`` paths are not available to users. Materialize a
    user-editable copy under ``~/.omnicraft/agents`` and never overwrite an
    existing file so local edits survive reinstalls and reruns.

    :param name: Filename of the bundled example (e.g.
        ``"databricks_coding_agent.yaml"``).
    :returns: Absolute path to the materialized agent YAML.
    """
    agent_path = _GLOBAL_AGENTS_DIR / name
    if agent_path.exists():
        return agent_path

    agent_path.parent.mkdir(parents=True, exist_ok=True)
    resource = resources.files("omnicraft.resources.examples").joinpath(name)
    text = resource.read_text(encoding="utf-8")
    executable_placeholder = "__OMNICRAFT_PYTHON_EXECUTABLE__"
    text = text.replace('"${OMNICRAFT_HOME:-$PWD}/.venv/bin/python"', executable_placeholder)
    text = text.replace("${OMNICRAFT_HOME:-$PWD}/.venv/bin/python", executable_placeholder)
    text = text.replace(".venv/bin/python", sys.executable)
    text = text.replace(executable_placeholder, sys.executable)
    agent_path.write_text(text, encoding="utf-8")
    return agent_path


def _materialize_internal_beta_agents() -> Path:
    """
    Materialize every bundled internal-beta example and return the default's path.

    :returns: Absolute path to the default agent YAML
        (:data:`_INTERNAL_BETA_DEFAULT_AGENT_NAME`).
    """
    default_path: Path | None = None
    for name in _INTERNAL_BETA_BUNDLED_AGENTS:
        path = _materialize_bundled_example(name)
        if name == _INTERNAL_BETA_DEFAULT_AGENT_NAME:
            default_path = path
    assert default_path is not None, (
        f"_INTERNAL_BETA_BUNDLED_AGENTS must include {_INTERNAL_BETA_DEFAULT_AGENT_NAME}"
    )
    return default_path


def _save_local_config(
    settings: dict[str, str | bool],
    unset_keys: tuple[str, ...] = (),
) -> None:
    """
    Merge *settings* into ``.omnicraft/config.yaml`` in cwd and remove
    any keys listed in *unset_keys*.

    Creates the ``.omnicraft/`` directory if it does not exist.

    :param settings: Key/value pairs to set, e.g.
        ``{"default_agent": "examples/agent.yaml",
        "auto_open_conversation": True}``.
    :param unset_keys: Keys to remove from the config, e.g.
        ``("server",)``.
    """
    path = Path.cwd() / _LOCAL_CONFIG_RELPATH
    cfg = _load_local_config()
    cfg.update(settings)
    for key in unset_keys:
        cfg.pop(key, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=True)


def _default_db_uri() -> str:
    """Default DB URI for ``omnicraft server`` — the machine-global
    ``<data_dir>/chat.db``.

    Resolves to the same path the ``omnicraft run`` daemon spawns its
    local server against (``_local_data_dir()``, honoring
    ``OMNICRAFT_DATA_DIR`` → else ``~/.omnicraft``). Pinning ``server``
    to the same DB as ``run`` means there is **one local DB — and so one
    accounts admin — per machine**, instead of a fresh CWD-relative
    ``omnicraft.db`` (and a fresh admin) for every directory you launch
    from. ``--database-uri`` / the config file still override.

    :returns: e.g. ``"sqlite:////home/alice/.omnicraft/chat.db"``.
    """
    from omnicraft.host.local_server import _local_data_dir

    return f"sqlite:///{_local_data_dir() / 'chat.db'}"


def _default_artifact_location() -> str:
    """Default artifact dir for ``omnicraft server`` — ``<data_dir>/artifacts``.

    Kept in lock-step with :func:`_default_db_uri` so a default-config
    ``omnicraft server`` and ``omnicraft run`` share one coherent
    machine-global instance (same DB *and* same artifacts) — otherwise a
    conversation created by one would reference files the other can't
    resolve. ``--artifact-location`` / the config file still override.

    :returns: e.g. ``"/home/alice/.omnicraft/artifacts"``.
    """
    from omnicraft.host.local_server import _local_data_dir

    return str(_local_data_dir() / "artifacts")


def _ensure_sqlite_parent_dir(db_uri: str) -> None:
    """Create the parent directory of a SQLite DB file if it's missing.

    SQLite creates the ``.db`` file on first connect but **not** its
    parent directory — an absent parent raises ``sqlite3.OperationalError:
    unable to open database file``. The default ``server`` DB now lives at
    ``<data_dir>/chat.db`` (machine-global, honoring ``OMNICRAFT_DATA_DIR``),
    so a first-ever run — or any run after the data dir was cleared — must
    create that dir before the stores connect. The daemon-spawned server
    handles this in ``ensure_local_omnicraft_server``; this is the equivalent for
    the foreground ``omnicraft server`` command.

    No-op for non-SQLite URIs (Postgres etc.) and for in-memory SQLite.

    :param db_uri: The resolved store DB URI, e.g.
        ``"sqlite:////home/alice/.omnicraft/chat.db"`` or
        ``"postgresql://host/db"``.
    :returns: None.
    """
    from sqlalchemy.engine import make_url

    url = make_url(db_uri)
    if url.get_backend_name() != "sqlite":
        return
    # url.database is the filesystem path for file-backed SQLite, None or
    # ":memory:" for in-memory — neither needs a parent dir.
    if not url.database or url.database == ":memory:":
        return
    Path(url.database).parent.mkdir(parents=True, exist_ok=True)


def _maybe_prompt_first_admin(account_store: Any, auth_provider: Any, *, auto_open: bool) -> None:  # type: ignore[explicit-any]  # SqlAlchemyAccountStore | None, AuthProvider
    """Interactively claim the first admin on a TTY when setup is pending.

    The "terminal" entry point of first-run setup. It's the FALLBACK,
    not the default: when the browser is about to auto-open the web
    Create-admin form (the default ``--open`` on a loopback server), we
    skip the prompt and let the browser own setup — otherwise the
    terminal prompt would block before the lifespan ever opens the
    browser, so the form would never appear.

    No-ops unless ALL of:

    - accounts mode is active (``account_store`` is not ``None``);
    - no password-having account exists yet (a ``--admin-password`` /
      ``INIT_ADMIN_PASSWORD`` would already have created one, and a
      re-boot already has an admin);
    - stdin AND stdout are a TTY — a headless / piped / agent run must
      NOT block on a prompt (it falls through to the web form);
    - the browser is NOT auto-opening a usable form, i.e. ``--no-open``
      was passed OR the base URL isn't loopback (remote-over-SSH, where
      opening a browser on the server box is useless but a terminal IS
      available).

    On success, creates the admin and mints the loopback CLI token so a
    subsequent ``omnicraft run`` against this server is signed in.

    :param account_store: The accounts store, or ``None`` in
        header/OIDC mode (then this is a no-op).
    :param auth_provider: The active auth provider; its accounts config
        supplies the cookie secret / base URL / session TTL.
    :param auto_open: The resolved ``--open/--no-open`` flag. When True
        and the base URL is loopback, the lifespan opens the browser to
        the form, so we defer to it and skip the prompt.
    :returns: None.
    """
    if account_store is None:
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    if any(u.has_password for u in account_store.list_users()):
        return

    from omnicraft.server.accounts_bootstrap import (
        _is_loopback_base_url,
        _mint_loopback_cli_token,
        resolve_admin_username,
    )
    from omnicraft.server.auth import UnifiedAuthProvider
    from omnicraft.server.passwords import hash_password
    from omnicraft.server.routes.accounts_auth import _MIN_PASSWORD_LENGTH

    # Read the accounts config off the concrete provider (same direct
    # access app.py uses). isinstance-narrowed so mypy sees the attribute
    # rather than reaching through getattr(..., "<literal>").
    base_url: str | None = None
    if isinstance(auth_provider, UnifiedAuthProvider):
        cfg = auth_provider._accounts_config
        base_url = cfg.base_url if cfg is not None else None
    # Defer to the browser form when it's going to open (default --open
    # on a loopback server). Only prompt when no browser form will appear.
    if auto_open and base_url is not None and _is_loopback_base_url(base_url):
        return

    click.echo("\n  Configuração inicial — crie a conta de admin para este servidor.")
    username = click.prompt("  Usuário", default=resolve_admin_username()).strip().lower()
    while True:
        password = click.prompt("  Senha", hide_input=True, confirmation_prompt=True)
        if len(password) >= _MIN_PASSWORD_LENGTH:
            break
        click.echo(f"  A senha deve ter pelo menos {_MIN_PASSWORD_LENGTH} caracteres.", err=True)

    try:
        account_store.create_user_with_password(username, hash_password(password), is_admin=True)
    except ValueError:
        # Raced another claimer (e.g. someone hit the web form first).
        click.echo("  Um admin acabou de ser criado em outro lugar — pulando.", err=True)
        return

    # Mint the loopback CLI token so `omnicraft run` is signed in.
    # (Reuses cfg/base_url resolved above.)
    if (
        cfg is not None
        and base_url is not None
        and cfg.cookie_secret is not None
        and _is_loopback_base_url(base_url)
    ):
        _mint_loopback_cli_token(
            username,
            base_url=base_url,
            cookie_secret=cfg.cookie_secret,
            session_ttl_hours=cfg.session_ttl_hours,
        )
    click.echo(f"  ✓ Admin '{username}' criado. Faça login na URL do servidor.\n")


def _create_artifact_store(location: str) -> Any:  # type: ignore[explicit-any]  # returns ArtifactStore protocol (optional deps)
    """
    Create an artifact store based on the location URI scheme.

    ``dbfs:/Volumes/...`` URIs use
    :class:`DatabricksVolumesArtifactStore` (requires
    ``databricks-sdk``). All other locations use
    :class:`LocalArtifactStore`.

    :param location: Artifact storage location, e.g.
        ``"./artifacts"`` for local or
        ``"dbfs:/Volumes/cat/schema/vol"`` for UC Volumes.
    :returns: An :class:`ArtifactStore` instance.
    """
    if location.startswith("dbfs:/Volumes/"):
        from omnicraft.stores.artifact_store.databricks_volumes import (
            DatabricksVolumesArtifactStore,
        )

        return DatabricksVolumesArtifactStore(location)

    from omnicraft.stores.artifact_store.local import LocalArtifactStore

    return LocalArtifactStore(location)


def _preregister_agent(  # type: ignore[explicit-any]  # agent_store / artifact_store / agent_cache typed Any to avoid import cycle
    agent_source: Path,
    agent_store: Any,
    artifact_store: Any,
    agent_cache: Any,
) -> str | None:
    """
    Register an agent from a directory or standalone YAML file.

    Materializes *agent_source* into a uniform bundle directory via
    :func:`omnicraft.spec.materialize_bundle`, tars it, validates
    the spec, and creates (or replaces) the agent in the store. This
    runs at server startup for each ``--agent`` flag.

    :param agent_source: Either an agent-image directory containing
        ``config.yaml`` (standard omnicraft shape) or a standalone
        omnicraft YAML file (e.g.
        ``examples/coding_supervisor.yaml``). The file-vs-directory
        branch lives inside ``materialize_bundle``; this function
        operates uniformly on a directory downstream of it.
    :param agent_store: The AgentStore for agent metadata.
    :param artifact_store: The ArtifactStore for bundle storage.
    :param agent_cache: The AgentCache. Required so the on-disk
        extracted-bundle tier (cache_dir/<agent_id>/) is swapped
        in lockstep with the artifact-store update — otherwise a
        persistent session reuses the prior extraction and any
        newly-added local-tool files (or other bundle edits) are
        silently ignored on the next request.
    :returns: The registered agent id, or ``None`` if the source
        spec has no name and is skipped.
    """
    import gzip
    import hashlib
    import io
    import tarfile

    from omnicraft.db.utils import generate_agent_id
    from omnicraft.spec import load, materialize_bundle

    with tempfile.TemporaryDirectory() as tmpdir:
        bundle_dir = materialize_bundle(agent_source, Path(tmpdir) / "bundle")

        # Build tarball in memory from the materialized bundle dir.
        # ``arcname="."`` puts the contents at the tarball root so
        # extraction produces the same shape ``spec.load`` expects.
        # Pin gzip mtime so sha256(bundle_bytes) is deterministic across calls.
        buf = io.BytesIO()
        with (
            gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
            tarfile.open(fileobj=gz, mode="w") as tar,
        ):
            tar.add(str(bundle_dir), arcname=".")
        bundle_bytes = buf.getvalue()

        # Validate via the materialized directory directly — cheaper
        # than round-tripping through extract.
        spec = load(bundle_dir)

    if spec.name is None:
        click.echo(f"  aviso: {agent_source} não tem nome, pulando")
        return None

    # Idempotent registration. Mirrors
    # :func:`omnicraft.inner.cli._omnicraft_register_yaml_bundle` —
    # see designs/RUN_OMNICRAFT_SESSION_RESUMPTION.md. Reusing the
    # existing ``agent_id`` (rather than delete + recreate)
    # is load-bearing for ``--continue``: deleting the old
    # row cascades through the ``tasks`` FK
    # (``ondelete=CASCADE`` in
    # :class:`omnicraft.db.db_models.SqlTask`), wiping every
    # prior task — which makes the next ``--continue``
    # filter by ``agent_id`` return zero conversations and
    # exit ``"No prior conversation for agent ..."``. Update
    # the bundle in place and only refresh
    # ``bundle_location`` when the content hash actually
    # changed so the row stays stable across no-op restarts.
    bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()
    existing = agent_store.get_by_name(spec.name)
    if existing is not None:
        new_loc = f"{existing.id}/{bundle_hash}"
        if existing.bundle_location != new_loc:
            artifact_store.put(new_loc, bundle_bytes)
            agent_store.update(existing.id, bundle_location=new_loc)
            # Swap the cache's extracted bundle in lockstep. Without
            # this, ``AgentCache.load`` will hit Tier 2 (disk —
            # ``cache_dir/<agent_id>/``) on the next request and
            # return the OLD spec, even though the artifact store
            # and the DB row both point at the new bundle.
            # Mirrors what the HTTP PUT /agents/{id} route does at
            # ``omnicraft/server/routes/agents.py:248``.
            # ``--agent`` registers operator-authored template agents,
            # so ${VAR} may expand against the server env here.
            agent_cache.replace(existing.id, new_loc, bundle_bytes, expand_env=True)
        click.echo(f"  agente: {spec.name} (de {agent_source})")
        return cast(str, existing.id)

    agent_id = generate_agent_id()
    loc = f"{agent_id}/{bundle_hash}"
    artifact_store.put(loc, bundle_bytes)
    agent_store.create(
        agent_id=agent_id,
        name=spec.name,
        bundle_location=loc,
        description=spec.description,
    )
    click.echo(f"  agente: {spec.name} (de {agent_source})")
    return agent_id


def _format_version() -> str:
    """Render the version line shown by ``--version`` and ``version``.

    Always includes the package version. When the build hook in
    ``setup.py`` wrote ``omnicraft/_build_info.py``, the line is
    additionally annotated with the short commit SHA and the build
    time in ISO-8601 UTC. For source checkouts that have never
    been built, only the bare version prints — matching the
    behavior before this feature shipped.

    :returns: Either ``"omnicraft 0.1.0"`` (no build info), or
        ``"omnicraft 0.1.0 (010cf77c, built 2026-05-21T14:34:45Z)"``.
    """
    import datetime

    from omnicraft.update_check import _read_build_info
    from omnicraft.version import VERSION

    version_str = VERSION
    info = _read_build_info()
    if info is None:
        return f"OmniCraft {version_str}"
    epoch, sha = info
    when = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    if sha:
        # Short SHA (first 8 chars) — enough to disambiguate in bug
        # reports without making the line unwieldy.
        return f"OmniCraft {version_str} ({sha[:8]}, built {when})"
    # _build_info exists but has no SHA (built without git available).
    return f"OmniCraft {version_str} (built {when})"


def _print_version_callback(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    """Click callback that lazily renders the version line and exits.

    We deliberately do NOT use ``@click.version_option(version=...)``
    here: that decorator evaluates its ``version`` argument at module
    import time, which would call ``_format_version()`` — and through
    it ``_read_build_info()`` — during ``omnicraft.cli`` import. The
    successful sub-import would then set ``omnicraft._build_info`` as
    an attribute on the ``omnicraft`` package object. Once that
    attribute exists, ``from omnicraft import _build_info`` short-
    circuits *before* consulting ``sys.modules``, defeating the
    test-suite's ``sys.modules[...] = None`` blocker and making most
    update_check tests pick up live values from disk.

    Doing the work in a callback keeps the import side-effect-free:
    ``_format_version`` runs only when the user actually passes
    ``--version`` on the command line.
    """
    if not value or ctx.resilient_parsing:
        return
    click.echo(_format_version())
    ctx.exit()


class _OmniCraftCLI(click.Group):
    """Top-level group that prints the brand lockup above its help.

    The Otto + wordmark lockup is drawn on stderr (decoration) and is
    TTY-gated by :func:`omnicraft.inner.ui.show_banner`, so ``omnicraft
    --help`` shows the banner interactively while piped/CI help stays
    clean. Only the top-level group overrides help; subcommand help
    (``omnicraft run --help``) is untouched.
    """

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        from omnicraft.inner import ui

        if ui.show_banner():
            from omnicraft.version import VERSION

            epilogue = [("Comece agora", "omnicraft setup")]
            if VERSION:
                epilogue.insert(0, ("Versão", VERSION))
            ui.print_landing(tagline="todos os seus agentes, um só cli", epilogue=epilogue)
        super().format_help(ctx, formatter)


@click.group(cls=_OmniCraftCLI)
@click.option(
    "--version",
    is_flag=True,
    callback=_print_version_callback,
    expose_value=False,
    is_eager=True,
    help="Mostra a versão e sai.",
)
def cli() -> None:
    """CLI do OmniCraft."""


# Names of every subcommand the click group owns. Used by
# :func:`main` to reject the removed top-level ad-hoc chat path
# before click reports an opaque "no such command" error.
# Keep in sync with ``@cli.command()`` decorations below.
_CLICK_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "antigravity",
        "attach",
        "claude",
        "codex",
        "config",
        "cursor",
        "lilo",
        "debug",
        "goose",
        "hermes",
        "host",
        "kimi",
        "kiro",
        "lakebox",
        "login",
        "opencode",
        "pane-picker",
        "pane-split",
        "pi",
        "fucho",
        "qwen",
        "resume",
        "run",
        "session",
        "sandbox",
        "server",
        "setup",
        "stop",
        "update",
        "upgrade",
        "version",
    }
)


def _should_skip_update_check(argv: list[str]) -> bool:
    """Decide whether the update notice should be suppressed for *argv*.

    Skipped for help / version requests, internal TUI subcommands
    (``pane-split`` / ``pane-picker``, invoked by the terminal UI rather
    than the user), and ``upgrade`` (and its ``update`` alias) itself
    (pointing the user at ``omni upgrade`` while they are running it is
    noise).

    :param argv: CLI arguments without the program name, e.g.
        ``["run", "agent.yaml"]``.
    :returns: ``True`` when the update notice should not be shown.
    """
    if not argv:
        return True
    return argv[0] in {
        "--help",
        "-h",
        "--version",
        "version",
        "update",
        "upgrade",
        "pane-split",
        "pane-picker",
    }


def main() -> None:
    """
    Console-script entry point for ``omnicraft``.

    Dispatches to the click CLI for subcommands like ``run``,
    ``attach``, and ``server``. The removed top-level ad-hoc chat
    shape (``omnicraft [--flags] [prompt]``) is rejected here so it
    cannot fall back to the legacy in-process runner path.

    Also inserts the current working directory at ``sys.path[0]``
    so dotted callables declared in user YAMLs (``callable:
    mypackage.mymodule.my_fn``) resolve against the user's project,
    not the console-script's install directory. Console entry
    points put the script's own directory at sys.path[0] by
    default, which is almost never what a CLI that imports
    user-authored modules wants.

    Sets up the always-on CLI diagnostics log before Click dispatch
    so unhandled exceptions are captured even when the user didn't
    enable ``--log`` or ``--debug-events``.
    """
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    # Relocate pre-rename ~/.omniagents state before anything reads ~/.omnicraft
    # (update-check cache, diagnostics logs, config). No-op once migrated.
    _migrate_legacy_state_dir()

    argv = sys.argv[1:]

    # Bare ``omnicraft`` with no args behaves like ``omnicraft run`` on an
    # interactive terminal: ``run`` resolves the configured default agent /
    # first-run plan and drops into ``setup`` when nothing is configured. In
    # a non-interactive context (pipe, CI, no TTY) fall back to ``--help`` so
    # we never launch a REPL that would hang waiting on stdin.
    if not argv:
        argv = ["run"] if sys.stdin.isatty() else ["--help"]

    # Shorthand: ``omnicraft --harness claude [opts]`` →
    # ``run --harness claude [opts]``. Click group-level options are
    # intentionally tiny (currently only help/version); runner flags live on
    # ``run``. Treat a leading non-top-level flag as bare-run shorthand so
    # users can type the natural no-AGENT launcher form.
    if argv and argv[0].startswith("-") and argv[0] not in {"--help", "-h", "--version"}:
        argv = ["run", *argv]

    # Shorthand: ``omnicraft myagent.yaml [opts]`` → ``run myagent.yaml [opts]``.
    # Allows ``omnicraft`` to act as a transparent alias for ``omnicraft run``
    # when the first positional argument is an agent path.
    if _is_run_shorthand(argv):
        argv = ["run", *argv]

    if argv and _is_server_url(argv[0]):
        click.echo(
            "Erro: URLs de servidor devem ser passadas com --server. "
            f"Use `omnicraft run --server {argv[0]}`.",
            err=True,
        )
        raise SystemExit(2)

    if _is_removed_ad_hoc_invocation(argv):
        click.echo(
            "Erro: o chat ad-hoc de nível superior foi removido. Use "
            "`omnicraft run <agent.yaml>` ou "
            "`omnicraft run --harness <harness>`.",
            err=True,
        )
        raise SystemExit(2)

    # Always-on diagnostics — captures exceptions, lifecycle events,
    # and warnings to ~/.omnicraft/logs/cli-*.log even when --log
    # (conversation JSON) and --debug-events (SSE tape) are off.
    # Skip for pure help/version so quick invocations don't create
    # log litter.
    if argv[0] in {"--help", "-h", "--version"}:
        cli(args=argv)
        return

    from omnicraft.cli_diagnostics import (
        log_cli_error_hint,
        log_cli_exception,
        print_setup_hint,
        setup_cli_logging,
    )

    setup_cli_logging(argv)

    # ``omnicraft setup`` IS the setup wizard — if it fails, telling the
    # user to "run omnicraft setup" would be circular. ``upgrade`` (and its
    # ``update`` alias) is excluded too: its failures (unreachable index,
    # dev checkout, install error) are never about a missing model
    # credential, so the setup hint would only mislead.
    suggest_setup = argv[0] not in {"setup", "update", "upgrade"}

    # Lightweight update notice: only on an interactive terminal and only
    # for user-facing commands. Reads a cached "latest PyPI version" and
    # prints at most once per release (the network refresh runs detached,
    # off the hot path). Never blocks; any failure is swallowed inside.
    if not _should_skip_update_check(argv) and sys.stderr.isatty():
        from omnicraft.update_check import maybe_show_update_notice

        maybe_show_update_notice()

    try:
        cli(args=argv, standalone_mode=False)
    except click.ClickException as exc:
        log_cli_exception(exc, prefix="Click CLI error")
        exc.show()
        if suggest_setup:
            print_setup_hint()
        raise SystemExit(exc.exit_code) from exc
    except click.Abort as exc:
        # Ctrl+C / user cancel — no hint, the user knows what they did.
        log_cli_exception(exc, prefix="Aborted CLI")
        click.echo("Abortado!", err=True)
        raise SystemExit(1) from exc
    except Exception as exc:
        log_cli_error_hint(exc)
        if suggest_setup:
            print_setup_hint()
        raise


def _is_run_shorthand(argv: list[str]) -> bool:
    """Return True when *argv* looks like ``omnicraft <target> [opts]``
    where *target* is an agent YAML/directory rather than a subcommand.

    Used by :func:`main` to transparently redirect
    ``omnicraft myagent.yaml --model m`` to
    ``omnicraft run myagent.yaml --model m``.

    :param argv: CLI arguments without the program name, e.g.
        ``["myagent.yaml", "--model", "m"]``.
    :returns: ``True`` when the first positional argument looks like a
        run target (file path).
    """
    if not argv:
        return False
    first = argv[0]
    if first.startswith("-"):
        return False  # leading flag, not a positional target
    if first in _CLICK_SUBCOMMANDS:
        return False  # already a known subcommand
    if _is_server_url(first):
        return False
    # Accept paths ending with .yaml/.yml and explicit relative/absolute
    # paths. Server addresses are only accepted through ``--server``.
    return (
        first.endswith((".yaml", ".yml")) or first.startswith(("./", "../")) or (os.sep in first)
    )


def _is_server_url(value: str) -> bool:
    """Return whether *value* is a server URL.

    :param value: CLI argument value, e.g. ``"http://localhost:6767"``.
    :returns: ``True`` for ``http://`` or ``https://`` URLs.
    """
    return value.startswith(("http://", "https://"))


def _is_removed_ad_hoc_invocation(argv: list[str]) -> bool:
    """
    Decide whether *argv* targets the removed top-level ad-hoc chat.

    True when:
    - The first non-flag token isn't a known click subcommand and is
      a quoted multi-word prompt (e.g.
      ``omnicraft "what does this repo do?"``) — the free-text shape
      the removed top-level ad-hoc chat accepted.

    False when the first non-flag token matches a known
    subcommand (``omnicraft run ...``, ``omnicraft attach ...``),
    when the user asks for top-level help/version
    (``omnicraft --help``, ``omnicraft --version``), or when the
    token is a single command-shaped word (e.g. ``omnicraft blah``)
    — those stay on the click path so an unknown command produces
    click's standard "No such command" error rather than the ad-hoc
    removal notice.

    :param argv: Argv without the program name, e.g.
        ``sys.argv[1:]``.
    :returns: True for removed ad-hoc dispatch, False for click dispatch.
    """
    if not argv:
        return False
    # Top-level click flags (``--help`` / ``-h`` / ``--version``)
    # should go through click so the user sees the click group's
    # help listing subcommands, not the legacy argparse help.
    if argv[0] in {"--help", "-h", "--version"}:
        return False
    # Skip leading flags to find the first positional. If all
    # tokens are flags (e.g. ``omnicraft --system-prompt "..."``),
    # treat it as removed ad-hoc chat rather than handing it to click
    # as a top-level option.
    for token in argv:
        if token.startswith("-"):
            continue
        if token in _CLICK_SUBCOMMANDS:
            return False
        # A single command-shaped word (no whitespace) is an unknown
        # subcommand: hand it to click for its standard "No such
        # command" error. Only a quoted multi-word prompt matches the
        # removed top-level ad-hoc chat shape.
        return any(ch.isspace() for ch in token)
    return True


def _runner_loopback_host(host: str) -> str:
    """Return a loopback-safe host for local runner callbacks.

    :param host: Server bind host, e.g. ``"0.0.0.0"``.
    :returns: Hostname the local runner can call back, e.g.
        ``"127.0.0.1"``.
    """
    return "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host


_HOST_PID_PATH = Path.home() / ".omnicraft" / "host.pid"


# host.pid records the daemon PID + the "target" it serves: a normalized
# server URL for remote/explicit targets, or the literal marker ``"local"``
# for a daemon that owns a local OmniCraft server. Daemon reuse is keyed on this
# target (real URLs never collide with the marker).
_LOCAL_DAEMON_MARKER = "local"


@dataclass(frozen=True)
class _HostDaemonRecord:
    """
    Local registry record for one background host daemon.

    :param pid: Process id of the background daemon, e.g. ``4242``.
    :param target: Normalized daemon target, e.g.
        ``"https://example.databricksapps.com"`` or ``"local"``.
    :param mode: Launch mode, either ``"server"`` or ``"local"``.
    :param server_url: Normalized requested server URL for ``"server"``
        mode, e.g. ``"https://example.databricksapps.com"``. ``None``
        for local mode.
    :param log_path: Daemon log file path, e.g.
        ``"/Users/me/.omnicraft/logs/host-daemon/daemon-abc.log"``.
    :param started_at: Unix epoch seconds when the daemon was spawned,
        e.g. ``1710000000``.
    :param host_id: Local host id advertised to OmniCraft servers, e.g.
        ``"host_abc123"``. ``None`` for legacy records.
    :param resolved_server_url: Concrete local server URL discovered for
        local mode, e.g. ``"http://127.0.0.1:8123"``. ``None`` until
        discovery succeeds or for remote mode.
    :param config_sig: Signature of the server-affecting config (resolved
        auth source) the daemon was spawned under, e.g.
        ``"3f9a1c2b4d5e6f70"`` (see :func:`_server_config_signature`).
        ``None`` for legacy records written before config-signature
        tracking existed; a ``None`` signature is never treated as a
        config mismatch (we can't know what it was started with).
    """

    pid: int
    target: str
    mode: str
    server_url: str | None
    log_path: str | None
    started_at: int
    host_id: str | None = None
    resolved_server_url: str | None = None
    config_sig: str | None = None


@dataclass(frozen=True)
class _HostHttpResult:
    """
    Decoded OmniCraft management HTTP response.

    :param status_code: HTTP status code, e.g. ``200``. ``0`` means no
        HTTP response was received because the request failed locally.
    :param body: Decoded JSON object or response text, e.g.
        ``{"data": []}`` or ``"not found"``.
    """

    status_code: int
    body: _HostJsonObject | str


@dataclass(frozen=True)
class _HostSessionsTableWidths:
    """
    Column widths for one host status sessions table.

    :param session_id: Width for the ``Session ID`` column, e.g. ``41``.
    :param runner_id: Width for the ``Runner ID`` column, e.g. ``44``.
    :param title: Width for the ``Title`` column, e.g. ``28``.
    :param workspace: Optional width for ``Workspace``, e.g. ``48``.
        ``None`` means the terminal is too narrow to show it.
    """

    session_id: int
    runner_id: int
    title: int
    workspace: int | None


@dataclass(frozen=True)
class _DaemonSessionsResult:
    """
    Sessions fetched for one daemon target.

    :param base_url: OmniCraft server base URL, e.g.
        ``"https://example.databricksapps.com"``. ``None`` when a
        local daemon's server cannot be discovered.
    :param sessions: Session rows owned by the daemon host id.
    :param error: Human-readable error text, or ``None`` on success.
    """

    base_url: str | None
    sessions: list[_HostSessionRow]
    error: str | None


@dataclass(frozen=True)
class _SessionsPageResult:
    """
    Decoded sessions page.

    :param sessions: Session rows returned by the page.
    :param last_id: Last session id in the page, e.g. ``"conv_abc123"``.
    :param has_more: Whether another page should be fetched.
    :param error: Human-readable error text, or ``None`` on success.
    """

    sessions: list[_HostSessionRow]
    last_id: str | None
    has_more: bool
    error: str | None


@dataclass(frozen=True)
class _SessionPagesResult:
    """
    Accumulated sessions from a paginated query.

    :param sessions: Session rows across all fetched pages.
    :param error: Human-readable error text, or ``None`` on success.
    """

    sessions: list[_HostSessionRow]
    error: str | None


@dataclass(frozen=True)
class _SpawnedDaemonProcess:
    """
    Background host daemon process metadata.

    :param pid: Spawned process id, e.g. ``4242``.
    :param log_path: Daemon log path, e.g.
        ``"/Users/me/.omnicraft/logs/host-daemon/daemon-abc.log"``.
    """

    pid: int
    log_path: str


def _normalize_daemon_target(server_url: str | None) -> str:
    """
    Normalize a daemon target key.

    :param server_url: Requested OmniCraft server URL, e.g.
        ``"https://example.databricksapps.com/"``. ``None`` or empty
        string selects local mode.
    :returns: ``"local"`` for local mode, otherwise the URL without a
        trailing slash.
    """
    return _LOCAL_DAEMON_MARKER if not server_url else server_url.rstrip("/")


def _daemon_host_online(record: _HostDaemonRecord, *, timeout_s: float = 2.0) -> bool:
    """
    Probe whether a daemon's host is currently online on its server.

    A daemon process being alive (PID check) does not mean its WebSocket
    tunnel to the OmniCraft server is up: the server only reports the host
    ``online`` while a daemon holds an authenticated tunnel and has
    heartbeated within ``HOST_LIVENESS_TTL_S``. After a server restart,
    an ungraceful daemon death, or a flapping tunnel, the daemon can be a
    "zombie" — alive but not registered. This probe distinguishes the two
    so reuse can heal instead of polling a zombie until timeout.

    :param record: Daemon record to probe.
    :param timeout_s: Per-request HTTP timeout in seconds, e.g. ``2.0``.
    :returns: ``True`` only when the server reports the record's host id
        as ``"online"``; ``False`` if the host id is unknown, the server
        is unreachable, or the host reports offline.
    """
    from omnicraft.claude_native_bridge import url_component

    host_id = record.host_id or _load_existing_host_id()
    if host_id is None:
        return False
    base_url = _daemon_base_url(record)
    if base_url is None:
        return False
    result = _host_http_json(
        base_url=base_url,
        method="GET",
        path=f"/v1/hosts/{url_component(host_id)}",
        timeout_s=timeout_s,
    )
    if result.status_code != 200 or not isinstance(result.body, dict):
        return False
    return result.body.get("status") == "online"


def _daemon_registry_dir() -> Path:
    """
    Return the directory containing per-target daemon registry records.

    Tests patch :data:`_HOST_PID_PATH`, so derive the registry root from
    the pidfile's parent instead of capturing ``Path.home()`` separately.

    :returns: Registry directory path, e.g.
        ``Path("~/.omnicraft/daemons")``.
    """
    return _HOST_PID_PATH.parent / "daemons"


def _daemon_record_path(target: str) -> Path:
    """
    Return the registry JSON path for *target*.

    :param target: Normalized daemon target, e.g.
        ``"https://example.databricksapps.com"`` or ``"local"``.
    :returns: JSON registry path for the target.
    """
    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:16]
    return _daemon_registry_dir() / f"{digest}.json"


def _record_from_json(raw: _HostJsonObject) -> _HostDaemonRecord | None:
    """
    Parse a daemon record from decoded JSON.

    :param raw: Decoded JSON object, e.g.
        ``{"pid": 4242, "target": "local", "mode": "local"}``.
    :returns: Parsed :class:`_HostDaemonRecord`, or ``None`` if the
        record is malformed.
    """
    try:
        pid_raw = raw["pid"]
        if not isinstance(pid_raw, str | int) or isinstance(pid_raw, bool):
            return None
        pid = int(pid_raw)
        target = str(raw["target"])
        mode = str(raw["mode"])
        started_at_raw = raw["started_at"]
        if not isinstance(started_at_raw, str | int) or isinstance(started_at_raw, bool):
            return None
        started_at = int(started_at_raw)
    except (KeyError, TypeError, ValueError):
        return None
    if mode not in {"local", "server"} or not target:
        return None
    server_url = raw.get("server_url")
    log_path = raw.get("log_path")
    host_id = raw.get("host_id")
    resolved_server_url = raw.get("resolved_server_url")
    config_sig = raw.get("config_sig")
    return _HostDaemonRecord(
        pid=pid,
        target=target,
        mode=mode,
        server_url=server_url if isinstance(server_url, str) and server_url else None,
        log_path=log_path if isinstance(log_path, str) and log_path else None,
        started_at=started_at,
        host_id=host_id if isinstance(host_id, str) and host_id else None,
        resolved_server_url=(
            resolved_server_url
            if isinstance(resolved_server_url, str) and resolved_server_url
            else None
        ),
        config_sig=config_sig if isinstance(config_sig, str) and config_sig else None,
    )


def _read_daemon_record(path: Path) -> _HostDaemonRecord | None:
    """
    Read a daemon registry record from disk.

    :param path: JSON file path to read, e.g.
        ``Path("~/.omnicraft/daemons/abc.json")``.
    :returns: Parsed daemon record, or ``None`` if unreadable or malformed.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return _record_from_json(cast(_HostJsonObject, raw))


def _write_daemon_record(record: _HostDaemonRecord) -> None:
    """
    Persist a daemon registry record.

    :param record: Record to write, e.g. a local daemon record with
        ``target == "local"``.
    """
    path = _daemon_record_path(record.target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(record), indent=2, sort_keys=True) + "\n")


def _delete_daemon_record(record: _HostDaemonRecord) -> None:
    """
    Delete a daemon registry record if it exists.

    Removes the per-target JSON record, and also clears the legacy
    ``host.pid`` when it names the same target — otherwise a daemon tracked
    only by the legacy pidfile (no JSON record) leaves a phantom that
    reappears on every subsequent ``stop`` / ``host status``.

    :param record: Record whose target path should be removed.
    """
    with contextlib.suppress(OSError):
        _daemon_record_path(record.target).unlink()
    legacy = _read_host_pid_file()
    if legacy is not None and legacy[1] == record.target:
        with contextlib.suppress(OSError):
            _HOST_PID_PATH.unlink()


def _legacy_daemon_record() -> _HostDaemonRecord | None:
    """
    Build a daemon record from the legacy ``host.pid`` file.

    :returns: Legacy record, or ``None`` if the pidfile is absent or
        malformed.
    """
    existing = _read_host_pid_file()
    if existing is None:
        return None
    pid, target = existing
    mode = "local" if target == _LOCAL_DAEMON_MARKER else "server"
    return _HostDaemonRecord(
        pid=pid,
        target=target,
        mode=mode,
        server_url=None if mode == "local" else target,
        log_path=None,
        started_at=0,
        host_id=_load_existing_host_id(),
    )


def _list_daemon_records(*, include_legacy: bool = True) -> list[_HostDaemonRecord]:
    """
    List daemon registry records.

    :param include_legacy: When ``True``, include a synthetic record
        from ``host.pid`` if no JSON record exists for that target.
    :returns: Records ordered by ``started_at`` descending.
    """
    records: dict[str, _HostDaemonRecord] = {}
    registry = _daemon_registry_dir()
    if registry.exists():
        for path in registry.glob("*.json"):
            record = _read_daemon_record(path)
            if record is not None:
                records[record.target] = record
    if include_legacy:
        legacy = _legacy_daemon_record()
        if legacy is not None and legacy.target not in records:
            records[legacy.target] = legacy
    return sorted(records.values(), key=lambda r: r.started_at, reverse=True)


def _find_daemon_record(target: str) -> _HostDaemonRecord | None:
    """
    Find a daemon record by target.

    :param target: Normalized daemon target, e.g. ``"local"``.
    :returns: Matching daemon record, or ``None``.
    """
    for record in _list_daemon_records():
        if record.target == target:
            return record
    return None


def _update_daemon_resolved_server_url(target: str, server_url: str) -> None:
    """
    Record the concrete OmniCraft server URL served by a daemon target.

    :param target: Normalized target, e.g. ``"local"``.
    :param server_url: Concrete server URL, e.g.
        ``"http://127.0.0.1:8123"``.
    """
    record = _find_daemon_record(target)
    if record is None:
        return
    _write_daemon_record(
        _HostDaemonRecord(
            **{
                **asdict(record),
                "resolved_server_url": server_url.rstrip("/"),
            }
        )
    )


def _load_existing_host_id() -> str | None:
    """
    Load the existing local host id without creating one.

    :returns: Host id from config, e.g. ``"host_abc123"``, or ``None``.
    """
    candidate_paths = [_effective_global_config_path()]
    from omnicraft.host.identity import CONFIG_PATH

    if CONFIG_PATH not in candidate_paths:
        candidate_paths.append(CONFIG_PATH)
    for path in candidate_paths:
        try:
            raw = yaml.safe_load(path.read_text()) if path.exists() else None
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(raw, dict):
            continue
        host = raw.get("host")
        if isinstance(host, dict):
            host_id = host.get("host_id")
            if isinstance(host_id, str) and host_id:
                return host_id
    return None


def _daemon_tunnel_recovers(
    record: _HostDaemonRecord,
    *,
    grace_s: float = _DAEMON_RECONNECT_GRACE_S,
) -> bool:
    """
    Return whether a daemon's host tunnel is (or quickly becomes) online.

    Probes the host status immediately, then polls for up to *grace_s* to
    let a daemon mid-reconnect (after a transient tunnel drop) re-register
    before we judge it a zombie.

    :param record: Daemon record to probe.
    :param grace_s: Seconds to keep polling for recovery, e.g. ``5.0``.
    :returns: ``True`` if the host reports online within the grace window.
    """
    if _daemon_host_online(record):
        return True
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if _daemon_host_online(record):
            return True
    return False


def _daemon_host_identity_changed(record: _HostDaemonRecord) -> bool:
    """
    Return whether a daemon record belongs to a different current host id.

    A live daemon can outlast edits to ``~/.omnicraft/config.yaml``. Reusing
    that process leaves commands polling for the new host id while the daemon
    is still connected as the old host id, which can never succeed.

    :param record: Daemon record being considered for reuse.
    :returns: ``True`` when the record has a host id and the current config
        either has a different id or no id.
    """
    if record.host_id is None:
        return False
    current_host_id = _load_existing_host_id()
    return record.host_id != current_host_id


def _terminate_host_unit(record: _HostDaemonRecord, *, reason: str) -> None:
    """
    Tear down a daemon and, in local mode, the OmniCraft server it owns.

    The ``--local`` daemon spawns its OmniCraft server once and never respawns
    it, so a stale daemon and its server must be replaced as a unit:
    killing only the daemon would strand the server (and vice versa). This
    stops both so the caller can spawn a fresh, correctly-configured pair.

    :param record: Daemon record to tear down.
    :param reason: Human-readable reason surfaced to the user, e.g.
        ``"config changed (auth)"`` or ``"host tunnel is offline"``.
    :returns: None.
    """
    click.echo(f"Reiniciando daemon host para {record.target!r} ({reason}).", err=True)
    # Best-effort: a daemon that refuses to die shouldn't hard-fail the
    # run — the fresh daemon's record overwrites this one regardless.
    with contextlib.suppress(click.ClickException):
        _terminate_daemon(record, force=True)
    if record.mode == "local":
        stop_local_omnicraft_server()


@dataclass(frozen=True)
class _DaemonReuseDecision:
    """Outcome of evaluating whether an existing daemon can be reused.

    :param reuse: ``True`` when the existing daemon is live, config-matching,
        and tunnel-healthy, so the caller should NOT spawn a new one.
    :param config_changed: ``True`` when the existing daemon was torn down
        specifically because its config signature no longer matches this
        invocation (e.g. the user flipped ``OMNICRAFT_AUTH_ENABLED``).
        Distinct from a transparent tunnel-health heal — only a config
        change forces the caller to ask the user to re-run, because the
        server was restarted into a different auth posture mid-command.
    """

    reuse: bool
    config_changed: bool


def _reuse_existing_daemon_record(target: str) -> _DaemonReuseDecision:
    """
    Decide whether an existing daemon for *target* can be reused.

    Reuse requires more than a live PID: a daemon whose process is alive
    but whose server tunnel is down (server restart, ungraceful death,
    flapping tunnel) is a zombie — the host reads ``offline`` and the
    caller would poll until timeout. And a daemon spawned under a
    different server config (e.g. the user flipped
    ``OMNICRAFT_AUTH_ENABLED``) would silently keep its old auth
    mode. In both cases we tear the unit down here and return
    ``reuse=False`` so the caller spawns a fresh one — flagging
    ``config_changed`` for the auth-drift case so the caller can ask the
    user to re-run against the freshly-restarted server.

    Self-healing is limited to daemons this CLI spawned in the background
    (they carry a ``log_path``). Foreground ``host`` daemons
    (``log_path is None``) and legacy records (``config_sig is None``) are
    never silently killed — we don't tear down an interactive process or
    one whose config we can't verify.

    :param target: Normalized daemon target, e.g. ``"local"``.
    :returns: A :class:`_DaemonReuseDecision`.
    """
    existing = _find_daemon_record(target)
    if existing is None:
        return _DaemonReuseDecision(reuse=False, config_changed=False)
    if not _pid_alive(existing.pid):
        _delete_daemon_record(existing)
        return _DaemonReuseDecision(reuse=False, config_changed=False)

    background = existing.log_path is not None
    if background and _daemon_host_identity_changed(existing):
        _terminate_host_unit(existing, reason="host identity changed")
        return _DaemonReuseDecision(reuse=False, config_changed=False)

    if target != _LOCAL_DAEMON_MARKER:
        # Remote / explicit ``--server`` mode: the daemon connects to a server
        # we don't own and can't restart, so the config-signature / heal /
        # "re-run" semantics below don't apply (auth posture is the remote's
        # concern; its own reconnect loop covers transient tunnel drops). Keep
        # the original PID-liveness reuse so a live daemon for the URL is
        # reused as-is.
        return _DaemonReuseDecision(reuse=True, config_changed=False)

    if not background:
        # Foreground host / legacy host.pid: keep prior behavior — a
        # live PID is reused as-is (don't kill the user's interactive
        # process or guess at an unstamped config).
        return _DaemonReuseDecision(reuse=True, config_changed=False)

    # Config drift → the running server has the wrong auth source.
    desired_sig = server_config_signature()
    if existing.config_sig is not None and existing.config_sig != desired_sig:
        _terminate_host_unit(existing, reason="config changed (auth)")
        return _DaemonReuseDecision(reuse=False, config_changed=True)

    # Tunnel health → don't reuse a zombie. Skip very young daemons (a
    # concurrent invocation may have just spawned one still connecting). This
    # is a transparent heal, NOT a config change — the caller continues.
    age_s = time.time() - existing.started_at
    if age_s >= _DAEMON_REUSE_MIN_AGE_S and not _daemon_tunnel_recovers(existing):
        _terminate_host_unit(existing, reason="host tunnel is offline")
        return _DaemonReuseDecision(reuse=False, config_changed=False)
    return _DaemonReuseDecision(reuse=True, config_changed=False)


def _local_daemon_serves_target(target: str, server_url: str | None) -> bool:
    """
    Check whether the local daemon already serves a requested URL target.

    :param target: Normalized daemon target, e.g.
        ``"http://127.0.0.1:8123"``.
    :param server_url: Requested server URL, or ``None`` for local mode.
    :returns: ``True`` if the live local daemon already serves *target*.
    """
    if not server_url:
        return False
    local_record = _find_daemon_record(_LOCAL_DAEMON_MARKER)
    if local_record is None or not _pid_alive(local_record.pid):
        return False
    local_url = local_server_url_if_healthy()
    return local_url is not None and local_url.rstrip("/") == target


def _spawn_host_daemon_process(
    *,
    args: list[str],
    env: dict[str, str],
) -> _SpawnedDaemonProcess | None:
    """
    Spawn the background host daemon and attach its log file.

    :param args: Process argv, e.g. ``["python", "-m", "..."]``.
    :param env: Allowlisted daemon environment.
    :returns: Spawned process metadata, or ``None`` if spawn fails.
    """
    log_dir = _HOST_PID_PATH.parent / "logs" / "host-daemon"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_fd, log_path = tempfile.mkstemp(prefix="daemon-", suffix=".log", dir=log_dir)
    log_fh = os.fdopen(log_fd, "wb")
    try:
        proc = subprocess.Popen(
            args,
            env=env,
            stdout=log_fh,
            stderr=log_fh,
            **_proc.spawn_kwargs(),
        )
    except OSError:
        return None
    finally:
        log_fh.close()
    return _SpawnedDaemonProcess(pid=proc.pid, log_path=log_path)


def _persist_spawned_daemon(
    *,
    target: str,
    spawned: _SpawnedDaemonProcess,
    config_sig: str,
) -> None:
    """
    Persist registry and legacy pidfile entries for a spawned daemon.

    :param target: Normalized daemon target, e.g. ``"local"``.
    :param spawned: Spawned process metadata.
    :param config_sig: Config signature this daemon was spawned under,
        e.g. ``"3f9a1c2b4d5e6f70"`` (see :func:`server_config_signature`).
    """
    mode = "local" if target == _LOCAL_DAEMON_MARKER else "server"
    _write_daemon_record(
        _HostDaemonRecord(
            pid=spawned.pid,
            target=target,
            mode=mode,
            server_url=None if mode == "local" else target,
            log_path=spawned.log_path,
            started_at=int(time.time()),
            host_id=_load_existing_host_id(),
            config_sig=config_sig,
        )
    )
    _HOST_PID_PATH.write_text(f"{spawned.pid}\n{target}\n")


def _foreground_daemon_record(
    *,
    target: str,
    server_url: str,
    host_id: str | None,
) -> _HostDaemonRecord:
    """
    Build the registry record for the current foreground host process.

    :param target: Normalized daemon target, e.g.
        ``"https://example.databricksapps.com"`` or ``"local"``.
    :param server_url: Concrete OmniCraft server URL being connected to, e.g.
        ``"http://127.0.0.1:8123"``.
    :param host_id: Local host id, e.g. ``"host_abc123"``.
    :returns: Daemon registry record for ``os.getpid()``.
    """
    mode = "local" if target == _LOCAL_DAEMON_MARKER else "server"
    return _HostDaemonRecord(
        pid=os.getpid(),
        target=target,
        mode=mode,
        server_url=None if mode == "local" else target,
        log_path=None,
        started_at=int(time.time()),
        host_id=host_id,
        resolved_server_url=server_url.rstrip("/") if mode == "local" else None,
        config_sig=server_config_signature(),
    )


def _live_daemon_conflict(record: _HostDaemonRecord) -> _HostDaemonRecord | None:
    """
    Find a live daemon that already serves a foreground record target.

    :param record: Foreground daemon record this process wants to claim.
    :returns: Conflicting live record, or ``None``.
    """
    existing = _find_daemon_record(record.target)
    if existing is not None and existing.pid != record.pid and _pid_alive(existing.pid):
        return existing
    if record.mode == "server" and record.server_url is not None:
        local_record = _find_daemon_record(_LOCAL_DAEMON_MARKER)
        if (
            local_record is not None
            and local_record.pid != record.pid
            and _pid_alive(local_record.pid)
            and local_record.resolved_server_url == record.server_url.rstrip("/")
        ):
            return local_record
    return None


def _claim_foreground_daemon_record(
    record: _HostDaemonRecord,
) -> _HostDaemonRecord | None:
    """
    Persist a foreground daemon record unless a live duplicate exists.

    :param record: Foreground process record, e.g. one with
        ``pid == os.getpid()``.
    :returns: Previous record for the same target, or ``None``.
    :raises click.ClickException: If a live daemon already serves the
        same target.
    """
    conflict = _live_daemon_conflict(record)
    if conflict is not None:
        raise click.ClickException(
            "Um daemon host já está rodando para este servidor "
            f"(pid={conflict.pid}, target={conflict.target}). "
            "Rode `omnicraft host status` para inspecioná-lo ou "
            "`omnicraft host stop --server ...` para pará-lo primeiro."
        )
    previous = _find_daemon_record(record.target)
    if previous is not None and not _pid_alive(previous.pid):
        _delete_daemon_record(previous)
        previous = None
    _write_daemon_record(record)
    return previous


def _restore_replaced_daemon_record(
    record: _HostDaemonRecord,
    previous: _HostDaemonRecord | None,
) -> None:
    """
    Restore the record replaced by a foreground host process.

    If another process has already written a newer record for the same
    target, this function leaves it untouched.

    :param record: Foreground daemon record written by this process.
    :param previous: Previous record returned by
        :func:`_claim_foreground_daemon_record`, or ``None``.
    """
    current = _read_daemon_record(_daemon_record_path(record.target))
    if current is None:
        return
    if current.pid != record.pid or current.started_at != record.started_at:
        return
    if previous is None:
        _delete_daemon_record(record)
        return
    _write_daemon_record(previous)


def _load_or_create_host_id() -> str | None:
    """
    Load or create the host id used by a foreground host process.

    :returns: Host id from local config, e.g. ``"host_abc123"``, or
        ``None`` if the identity file cannot be created.
    """
    host_id = _load_existing_host_id()
    if host_id is not None:
        return host_id
    from omnicraft.host.identity import CONFIG_PATH, load_or_create_host_identity

    try:
        return load_or_create_host_identity(CONFIG_PATH).host_id
    except OSError:
        return None


def _ensure_host_daemon(server_url: str | None) -> bool:
    """Start or reuse a host daemon for one target.

    :param server_url: OmniCraft server URL the daemon connects to, or ``None``
        for local mode — the daemon starts (or reuses) a persistent local
        OmniCraft server and connects to that.
    :returns: ``True`` when an existing daemon was torn down and respawned
        because its config (auth source) changed — the caller
        should ask the user to re-run against the freshly-restarted server
        rather than continue this command mid-restart. ``False`` for a
        plain reuse, a transparent tunnel-health heal, or a first spawn.
    """
    target = _normalize_daemon_target(server_url)
    decision = _reuse_existing_daemon_record(target)
    if decision.reuse:
        return False
    if not decision.config_changed and _local_daemon_serves_target(target, server_url):
        return False

    _HOST_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    mode_args = ["--local"] if not server_url else ["--server", server_url]
    args = [sys.executable, "-m", "omnicraft.host._daemon_entry", *mode_args]
    spawned = _spawn_host_daemon_process(
        args=args, env=_build_host_daemon_env(server_url=server_url)
    )
    if spawned is None:
        return False
    _persist_spawned_daemon(
        target=target,
        spawned=spawned,
        config_sig=server_config_signature(),
    )
    return decision.config_changed


def _build_host_daemon_env(
    *,
    server_url: str | None,
) -> dict[str, str]:
    """
    Build the environment for the background host daemon.

    Remote daemons connect to an already-running OmniCraft server, so they only
    need process essentials, TLS trust, and Databricks auth. Local daemons
    also start the local OmniCraft server; that server is the user's local runtime
    and must inherit OmniCraft config plus provider credentials such as
    ``OPENAI_API_KEY`` and ``OPENAI_BASE_URL``. Both modes are allowlisted:
    local mode carries the runtime/provider vars needed by the local server,
    but unrelated shell secrets are not inherited merely because the daemon
    runs on the user's machine. Runners launched by the daemon still pass
    through :func:`omnicraft.host.connect._build_runner_env`, so these
    local-server credentials do not leak into runner subprocesses.

    :param server_url: OmniCraft server URL for remote mode, e.g.
        ``"https://example.databricksapps.com"``, or a falsey value
        such as ``None`` / ``""`` for local daemon mode.
    :returns: Environment dict for ``subprocess.Popen``.
    """
    from omnicraft.host.connect import (
        _RUNNER_ENV_ALLOWLIST,
        _RUNNER_ENV_ALLOWLIST_PREFIXES,
    )

    if not server_url:
        daemon_env_prefixes = (*_RUNNER_ENV_ALLOWLIST_PREFIXES, *_LOCAL_DAEMON_ENV_PREFIXES)
        env = {
            key: value
            for key, value in os.environ.items()
            if key in _RUNNER_ENV_ALLOWLIST
            or key in _LOCAL_DAEMON_ENV_ALLOWLIST
            or key.startswith(daemon_env_prefixes)
        }
    else:
        # Allowlist the remote daemon's environment (W8): pass process
        # essentials + TLS trust + the user's Databricks auth (the daemon
        # authenticates to the server with it), but not unrelated provider
        # secrets like ANTHROPIC_API_KEY / OPENAI_API_KEY.
        daemon_env_prefixes = (*_RUNNER_ENV_ALLOWLIST_PREFIXES, "DATABRICKS_")
        env = {
            key: value
            for key, value in os.environ.items()
            if key in _RUNNER_ENV_ALLOWLIST or key.startswith(daemon_env_prefixes)
        }
    return env


def _read_host_pid_file() -> tuple[int, str] | None:
    """Read the host daemon PID file (two lines: PID and server URL).

    :returns: ``(pid, server_url)`` if well-formed, ``None`` otherwise.
    """
    if not _HOST_PID_PATH.exists():
        return None
    try:
        lines = _HOST_PID_PATH.read_text().strip().splitlines()
        if len(lines) < 2:
            return None
        return int(lines[0]), lines[1]
    except (ValueError, OSError):
        return None


def _host_daemon_alive() -> bool:
    """Check whether the local-mode host daemon is still alive.

    :returns: ``True`` if a local daemon record exists and its process
        is running.
    """
    existing = _find_daemon_record(_LOCAL_DAEMON_MARKER)
    if existing is None:
        return False
    return _pid_alive(existing.pid)


# Generous because a port-contended spawn boots TWICE: the bind-race loser
# runs to its natural EADDRINUSE exit (completing DB migrations) before the
# free-port respawn cold-boots — see ensure_local_omnicraft_server.
_LOCAL_SERVER_DISCOVER_TIMEOUT_S = 120.0


def _ensure_databricks_server_auth(server: str, *, non_interactive: bool = False) -> None:
    """Sign in (or fail with the login hint) for Databricks-fronted servers.

    Probes ``/v1/me`` with whatever credentials the auth chain can mint
    today. A non-200 answer that carries the Databricks edge signature
    (302 to the workspace OAuth page, or a DatabricksRealm 401) means
    the run would otherwise die much later with an opaque "non-JSON
    response (status=302)" traceback from the session-create call. On a
    TTY we run the same flow ``omnicraft login`` would and continue;
    headless invocations get the exact command to run instead.

    Non-Databricks postures are deliberately left alone: local accounts
    servers auto-authenticate downstream (magic-link redeem), and
    header-mode servers answer 200 outright.

    :param server: Remote server base URL without a trailing slash,
        e.g. ``"https://myapp-123.aws.databricksapps.com"``.
    :param non_interactive: When ``True``, never run the browser login —
        emit the same fail-loud hint a headless invocation gets, even on a
        TTY. Lets callers (e.g. ``omnicraft host --non-interactive``) keep
        their scripted, no-prompt behavior.
    :raises click.ClickException: When the server is Databricks-fronted,
        no credentials resolve, and the login flow is suppressed (stdin is
        not a TTY or ``non_interactive`` is set) — or the login flow itself
        fails.
    """
    import httpx as _httpx

    from omnicraft.chat import _remote_headers

    try:
        probe = _httpx.get(
            f"{server}/v1/me",
            headers=_remote_headers(server_url=server),
            timeout=10.0,
        )
    except _httpx.HTTPError:
        # Unreachable / transient: let the connect path raise its own,
        # already-actionable error rather than failing the pre-flight.
        return
    if probe.status_code == 200:
        return
    workspace_host = _databricks_workspace_login_target(server, probe)
    if workspace_host is None:
        return
    login_cmd = f"omnicraft login {server}"
    if non_interactive or not sys.stdin.isatty():
        raise click.ClickException(
            f"Não autenticado em {server} (fronteado por Databricks; /v1/me respondeu "
            f"HTTP {probe.status_code}). Rode `{login_cmd}` e tente de novo."
        )
    click.echo(f"Não autenticado em {server} — rodando `{login_cmd}` primeiro.")
    # Recover the ``?o=`` selector from a prior login record so a re-login
    # still targets the right workspace.
    from omnicraft.cli_auth import load_databricks_org_id

    _databricks_login(server, workspace_host, org_id=load_databricks_org_id(server))


def _ensure_backend(server: str | None) -> str:
    """Ensure the host daemon is running and return the OmniCraft server URL.

    The daemon is the single backend for ``attach`` / ``run`` / ``claude`` /
    ``codex``: it spawns the runner and, in local mode, the OmniCraft server too.
    The CLI is a pure client of the returned URL.

    :param server: ``--server`` value after config fallback. A non-empty
        value targets that (remote or explicit-local) server. ``None`` or
        ``""`` selects local mode: the daemon starts (or reuses) a
        persistent local OmniCraft server and this returns its discovered loopback
        URL.
    :returns: A concrete base URL, e.g. ``"http://127.0.0.1:8123"`` or the
        remote URL passed in.
    :raises click.ClickException: If local mode's server never becomes
        reachable.
    """
    from omnicraft._runner_startup import (
        STARTUP_PHASE_CONNECTING_REMOTE,
        STARTUP_PHASE_LOCAL_SERVER,
        STARTUP_PHASE_STARTING,
        runner_startup_progress,
    )

    if server:
        # Remote / explicit-server mode: the server isn't ours to restart, so
        # there's no auth-mode-flip "re-run" to surface (config_changed is
        # always False for a non-local target). Expand a bare workspace URL
        # to its /api/2.0/omnicraft mount, then sign in first when the
        # server is Databricks-fronted and we hold no usable credentials —
        # otherwise the session-create call deep in the REPL bring-up
        # surfaces the edge redirect as an opaque non-JSON-response
        # traceback.
        server = _resolve_server_url(server)
        _ensure_databricks_server_auth(server)
        with runner_startup_progress(initial_message=STARTUP_PHASE_CONNECTING_REMOTE):
            _ensure_host_daemon(server)
        return server
    # Local mode: the daemon spawns (or reuses) a persistent local OmniCraft server.
    # On a cold start this is the longest silent gap between the user pressing
    # Enter and any output, so render a spinner whose label tracks the step.
    # It clears on context exit — before any auth-mode-change echo below and
    # before the REPL/terminal the caller brings up — and falls back to plain
    # stderr lines off a TTY (CI, daemon logfiles).
    with runner_startup_progress(initial_message=STARTUP_PHASE_STARTING) as progress:
        config_changed = _ensure_host_daemon(None)
        progress.update(STARTUP_PHASE_LOCAL_SERVER)
        local_url = _discover_local_server_url()
    _update_daemon_resolved_server_url(_LOCAL_DAEMON_MARKER, local_url)
    if config_changed:
        _exit_for_auth_mode_change(local_url)
    return local_url


def _exit_for_auth_mode_change(base_url: str) -> None:
    """Tell the user the server was restarted in a new mode, then exit clean.

    The local OmniCraft server bakes its auth posture (header vs accounts, cookie
    secret) at boot, so an ``OMNICRAFT_AUTH_ENABLED`` flip restarts it
    via :func:`_ensure_host_daemon`. Continuing the *same* command across
    that restart is brittle — the in-flight session/credential/terminal
    bring-up straddles two server identities. Instead we stop here with a
    clear, actionable message and exit 0, so the next ``omnicraft run`` is
    a clean single-mode start. When the new mode is accounts and no admin
    exists yet, point the user at the one-time setup URL.

    :param base_url: The freshly-restarted OmniCraft server URL, e.g.
        ``"http://127.0.0.1:6767"``.
    :returns: Never returns — raises ``SystemExit(0)``.
    :raises SystemExit: Always, with code 0 (a clean, expected stop).
    """
    needs_admin_setup = False
    result = _host_http_json(base_url=base_url, method="GET", path="/v1/info")
    if result.status_code == 200 and isinstance(result.body, dict):
        needs_admin_setup = bool(
            result.body.get("accounts_enabled") and result.body.get("needs_setup")
        )

    click.echo("", err=True)
    click.echo(
        "  ✓ Modo de auth alterado — o servidor local foi reiniciado para acompanhar.",
        err=True,
    )
    if needs_admin_setup:
        click.echo(
            f"  Crie sua conta de admin única em  {base_url.rstrip('/')}  "
            "(pode ter aberto automaticamente),",
            err=True,
        )
        click.echo("  depois rode `omnicraft run` de novo para começar.", err=True)
    else:
        click.echo("  Rode `omnicraft run` de novo para começar.", err=True)
    click.echo("", err=True)
    raise SystemExit(0)


def _discover_local_server_url(
    timeout: float = _LOCAL_SERVER_DISCOVER_TIMEOUT_S,
) -> str:
    """Poll until the daemon-started local OmniCraft server is reachable.

    In local mode the daemon owns the OmniCraft server; the CLI discovers its URL
    via the local-server pidfile + ``/health`` rather than starting it
    itself.

    :param timeout: Max seconds to wait, e.g. ``60.0``.
    :returns: The loopback server URL, e.g. ``"http://127.0.0.1:8123"``.
    :raises click.ClickException: If the daemon exits first, or the server
        does not come up within the timeout.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = local_server_url_if_healthy()
        if url is not None:
            return url
        if not _host_daemon_alive():
            raise click.ClickException(
                "O daemon local saiu antes do servidor OmniCraft ficar pronto. "
                "Veja os logs em ~/.omnicraft/logs/host-daemon/ e "
                "~/.omnicraft/logs/server/."
            )
        time.sleep(0.2)
    raise click.ClickException(
        f"Tempo esgotado após {timeout:.0f}s esperando o servidor OmniCraft local "
        "iniciar. Veja ~/.omnicraft/logs/server/ para detalhes."
    )


@dataclass
class _CliRunnerProcess:
    """Runner subprocess metadata for the ``omnicraft server`` command.

    :param proc: Runner subprocess handle.
    :param runner_id: Runner id used for the WS tunnel, e.g.
        ``"runner_0123456789abcdef"``.
    :param tunnel_token: Secret token that binds the tunnel to
        ``runner_id``, e.g. ``"uA6Zz..."``.
    """

    proc: subprocess.Popen[bytes]
    runner_id: str
    tunnel_token: str
    log_path: Path | None = None


def _start_cli_runner_process(
    *,
    server_url: str,
    tunnel_token: str | None = None,
    runner_id: str | None = None,
    workspace_cwd: str | Path | None = None,
    capture_logs: bool = False,
    log_dir: str | Path | None = None,
    prewarm_spec_path: str | Path | None = None,
    isolate_session: bool = False,
    extra_env: dict[str, str] | None = None,
) -> _CliRunnerProcess:
    """Start the out-of-process runner used by CLI server flows.

    The runner always connects back over the WebSocket tunnel. Local
    ``omnicraft server`` passes its loopback URL; ``run --server``
    passes the remote OmniCraft server URL.

    For remote Databricks-fronted servers, the runner subprocess
    authenticates via the stored ``omnicraft login`` record (or
    ambient Databricks SDK credentials). Tokens are refreshed
    transparently on each WebSocket reconnect and HTTP callback —
    no static token is passed via environment variable.

    :param server_url: Server base URL, e.g.
        ``"http://127.0.0.1:6767"``.
    :param tunnel_token: Optional binding token for the runner id,
        e.g. ``"uA6Zz..."``. ``None`` generates a fresh token.
    :param runner_id: Optional runner id to advertise. ``None``
        uses a per-run token-bound id for authenticated remote
        servers, or the stable runner id from
        :func:`omnicraft.runner.identity.get_stable_runner_id`
        for unauthenticated local servers.
    :param workspace_cwd: Optional local workspace root to expose
        to runner-local filesystem tools when a spec uses the
        placeholder cwd ``"."``. Remote ``run/attach --server``
        passes the CLI launch cwd so local runner tools operate
        in the user's project checkout.
    :param capture_logs: When True, redirect the runner
        subprocess's stdout/stderr to a per-run temp log file
        instead of inheriting the parent's stdio. The attach-remote
        flow sets this so runner WARNINGs (e.g. expected
        tunnel-dispatch failures like sandbox-unsupported)
        don't paint onto the REPL terminal.
    :param log_dir: Optional base log directory to use when
        ``capture_logs`` is true. Defaults to the shared
        ``~/.omnicraft/logs`` location; tests should pass a
        temporary directory to avoid writing to the developer's
        real home.
    :param prewarm_spec_path: Optional YAML path; the runner registers
        its MCP routing metadata during startup without opening transports.
    :param isolate_session: ``True`` for shared-host runners;
        enables per-session workspace isolation so each
        session gets its own subdirectory. ``False`` (default)
        lets the agent see the project root directly.
    :param extra_env: Optional mapping of additional environment
        variables overlaid on top of ``os.environ`` for the runner
        subprocess. Used by tests to route the runner at a mock LLM
        server instead of the ambient API endpoint.
    :returns: The spawned runner process metadata.
    :raises click.ClickException: If the runner exits immediately.
    """
    from omnicraft.runner.identity import (
        RUNNER_ID_ENV_VAR,
        RUNNER_ISOLATE_SESSION_ENV_VAR,
        RUNNER_PARENT_PID_ENV_VAR,
        RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
        RUNNER_WORKSPACE_ENV_VAR,
        token_bound_runner_id,
    )

    binding_token = tunnel_token.strip() if tunnel_token is not None else None
    if tunnel_token is not None and not binding_token:
        raise click.ClickException("O token de vínculo do túnel do runner não pode ser vazio")
    binding_token = binding_token or secrets.token_urlsafe(32)
    resolved_runner_id = runner_id.strip() if runner_id is not None else None
    if runner_id is not None and not resolved_runner_id:
        raise click.ClickException("O id do runner não pode ser vazio")
    if resolved_runner_id is None:
        # The runner sends the binding token in the tunnel header;
        # the server derives expected_runner_id from it via
        # token_bound_runner_id(). The path runner_id must match,
        # so we always derive from the binding token — not the
        # stable runner id, which is unrelated to the token.
        resolved_runner_id = token_bound_runner_id(binding_token)
    env = {
        **os.environ,
        **(extra_env or {}),
        "RUNNER_SERVER_URL": server_url,
        RUNNER_ID_ENV_VAR: resolved_runner_id,
        RUNNER_PARENT_PID_ENV_VAR: str(os.getpid()),
    }
    env[RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR] = binding_token
    if workspace_cwd is not None:
        env[RUNNER_WORKSPACE_ENV_VAR] = str(Path(workspace_cwd).expanduser().resolve())
    if isolate_session:
        env[RUNNER_ISOLATE_SESSION_ENV_VAR] = "1"
    if prewarm_spec_path is not None:
        env["RUNNER_PREWARM_SPEC_PATH"] = str(Path(prewarm_spec_path).expanduser().resolve())

    log_path: Path | None = None
    log_fh: BinaryIO | None = None
    if capture_logs:
        base_log_dir = (
            Path(log_dir).expanduser()
            if log_dir is not None
            else Path.home() / ".omnicraft" / "logs"
        )
        runner_log_dir = base_log_dir / "runner"
        runner_log_dir.mkdir(parents=True, exist_ok=True)
        log_fd, log_name = tempfile.mkstemp(prefix="runner-", suffix=".log", dir=runner_log_dir)
        log_path = Path(log_name)
        log_fh = os.fdopen(log_fd, "wb")
    try:
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnicraft.runner._entry"],
            env=env,
            stdout=log_fh,
            stderr=log_fh,
            **_proc.spawn_kwargs(),
        )
    finally:
        if log_fh is not None:
            log_fh.close()
    if runner_proc.poll() is not None:
        from omnicraft._runner_startup import format_runner_log_tail

        raise click.ClickException(
            f"O processo do runner saiu cedo com o código {runner_proc.returncode}."
            f"{format_runner_log_tail(log_path)}"
        )
    return _CliRunnerProcess(
        proc=runner_proc,
        runner_id=resolved_runner_id,
        tunnel_token=binding_token,
        log_path=log_path,
    )


def _stop_cli_runner_process(
    proc: subprocess.Popen[bytes],
    *,
    grace_timeout: float = 5.0,
) -> None:
    """Stop a runner subprocess started by :func:`_start_cli_runner_process`.

    :param proc: Runner subprocess handle to terminate.
    :param grace_timeout: Seconds to wait after SIGTERM before
        sending SIGKILL, e.g. ``5.0``.
    :returns: None.
    """
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=grace_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _adopt_cli_runner_process(proc: subprocess.Popen[bytes]) -> None:
    """Detach a runner from this CLI so it keeps running after CLI exit.

    Sends :data:`RUNNER_ADOPT_SIGNAL` (SIGUSR1, when available) so the
    runner cancels its parent-pid watchdog and survives the launching
    CLI's exit. Used when the user detaches from tmux: Claude and the
    runner stay alive and the web UI stays connected. A no-op if the
    runner has already exited, or if the platform has no adopt signal.

    :param proc: Runner subprocess handle to adopt.
    :returns: None.
    """
    from omnicraft.runner.identity import RUNNER_ADOPT_SIGNAL

    if RUNNER_ADOPT_SIGNAL is None:
        return
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(RUNNER_ADOPT_SIGNAL)


def _assert_server_port_bindable(host: str, port: int) -> None:
    """
    Fail before app startup when the requested TCP listener cannot bind.

    Mirrors uvicorn's TCP bind shape closely enough for CLI preflight:
    IPv6 is selected when the host contains ``":"``, and
    ``SO_REUSEADDR`` is set before bind. This is intentionally a bind
    probe, not a connect probe, so a failed client connection to the
    port does not make us report the port as occupied.

    :param host: Interface to bind, e.g. ``"127.0.0.1"``.
    :param port: TCP port to bind, e.g. ``6767``.
    :returns: None.
    :raises click.ClickException: If the host/port cannot be bound.
    """
    import socket

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family=family, type=socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError as exc:
            reason = exc.strerror or str(exc)
            raise click.ClickException(
                f"Não é possível iniciar o servidor em {host}:{port}: "
                f"porta indisponível ({reason})."
            ) from exc


@cli.group("server", invoke_without_command=True)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Host para vincular.",
)
@click.option(
    "--port",
    "-p",
    default=_DEFAULT_LOCAL_PORT,
    show_default=True,
    type=int,
    help="Porta para escutar.",
)
@click.option(
    "--database-uri",
    default=None,
    help="URI do banco de dados para os stores.  [padrão: sqlite em <data-dir>/chat.db, "
    "global na máquina, então `server` e `run` compartilham um admin]",
)
@click.option(
    "--artifact-location",
    default=None,
    help="Caminho para armazenamento de artifacts.  [padrão: <data-dir>/artifacts]",
)
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Caminho para o arquivo de config YAML.",
)
@click.option(
    "--execution-timeout",
    default=None,
    type=int,
    help="Máximo de segundos de relógio por execução de agente.  [padrão: 7200]",
)
@click.option(
    "--agent",
    "agent_dirs",
    multiple=True,
    type=click.Path(exists=True),
    help=(
        "Pré-registra um agente a partir de um diretório na inicialização. "
        "Pode ser repetido. Se o nome do agente já existir, "
        "o bundle é substituído."
    ),
)
@click.option(
    "--open/--no-open",
    "auto_open",
    default=True,
    help=(
        "No primeiro boot da auth de contas, abre a URL de magic-redeem no "
        "navegador do usuário para o web UI logar sem digitar senha. "
        "Padrão: --open. Passe --no-open para headless / SSH / Docker."
    ),
)
@click.option(
    "--admin-password",
    default=None,
    help=(
        "Define a senha de admin de contas do primeiro boot de forma não interativa "
        "(alternativa a OMNICRAFT_ACCOUNTS_INIT_ADMIN_PASSWORD). Só "
        "tem efeito no primeiro boot do banco de contas da máquina; "
        "ignorado com um aviso se um admin já existir."
    ),
)
@click.pass_context
def server(
    ctx: click.Context,
    host: str,
    port: int,
    database_uri: str | None,
    artifact_location: str | None,
    config_path: str | None,
    execution_timeout: int | None,
    agent_dirs: tuple[str, ...],
    auto_open: bool,
    admin_password: str | None,
) -> None:
    """Inicia o servidor OmniCraft em primeiro plano, ou gerencia o servidor em segundo plano.

    O ``omnicraft server`` puro roda o servidor em PRIMEIRO PLANO (Ctrl-C para
    parar) — para deploys / Docker. Os subcomandos gerenciam o servidor em segundo
    plano destacado que ``run`` / ``claude`` / ``codex`` usam: ``start`` (garante que
    está no ar), ``stop`` (para ele e o daemon host local), ``status`` (está no ar?).

    :param host: Interface para vincular, ex. ``"127.0.0.1"``.
    :param ctx: Contexto de invocação do Click usado para saber se
        ``--port`` veio da linha de comando ou do padrão.
    :param port: Porta TCP para escutar, ex. ``6767``.
    :param database_uri: URI opcional do banco de dados, ex.
        ``"sqlite:///omnicraft.db"``.
    :param artifact_location: Local opcional de artifacts, ex.
        ``"./artifacts"``.
    :param config_path: Caminho opcional do arquivo de config YAML.
    :param execution_timeout: Máximo opcional de segundos de execução do agente,
        ex. ``7200``.
    :param agent_dirs: Diretórios de agente ou arquivos YAML passados com
        ``--agent``.
    :param auto_open: Se deve abrir a URL de magic-redeem no
        navegador do usuário no primeiro boot do modo de contas. Traduzido
        para a env var ``OMNICRAFT_ACCOUNTS_AUTO_OPEN`` para que o
        hook de startup do lifespan (que de fato dispara a abertura depois
        do uvicorn vincular) a leia sem alterar o threading de kwargs.
    :param admin_password: Senha opcional de admin de contas do primeiro boot
        vinda de ``--admin-password``, ex. ``"hunter2"``. Dobrada para a
        env var ``OMNICRAFT_ACCOUNTS_INIT_ADMIN_PASSWORD`` que o
        bootstrap lê; ``None`` deixa a env var intacta.
    :returns: None.
    """
    if ctx.invoked_subcommand is not None:
        # A subcommand (start/stop/status) handles this invocation; the body
        # below is the foreground-server path for the bare ``server`` group.
        return
    port_source = ctx.get_parameter_source("port")
    port_was_explicit = port_source is click.core.ParameterSource.COMMANDLINE
    if port_was_explicit:
        _assert_server_port_bindable(host, port)

    # --admin-password is sugar for the INIT_ADMIN_PASSWORD env var that
    # bootstrap_admin already consumes — fold it in here so the rest of
    # the startup path has a single source. setdefault so an explicit
    # env var wins over the flag (consistent with "explicit env wins").
    # Whether it actually takes effect (vs. being ignored with a warning
    # because an admin already exists) is decided in bootstrap_admin.
    if admin_password:
        os.environ.setdefault("OMNICRAFT_ACCOUNTS_INIT_ADMIN_PASSWORD", admin_password)

    # Translate --no-open into the env var the lifespan hook reads.
    # We use an env var rather than threading the flag through
    # create_app so the same toggle works for callers (Docker
    # entrypoint, future `omnicraft run`) that build the app
    # outside this CLI command.
    os.environ["OMNICRAFT_ACCOUNTS_AUTO_OPEN"] = "1" if auto_open else "0"

    # Unified local-server lifecycle — applies ONLY to a *bare* loopback
    # `omnicraft server` (default port + default DB + artifacts), i.e.
    # THE canonical machine-global local server recorded in
    # ~/.omnicraft/local_server.pid:
    #   - If a healthy one is already running (started here OR spawned by
    #     the `run`/`host` daemon), reuse it — print its URL and exit
    #     instead of starting a competing second server on the shared DB.
    #   - Otherwise prefer the requested port (default 6767), falling back
    #     to a free one if taken, and register ourselves in the pidfile so
    #     the daemon reuses THIS server. (See host/local_server.py.)
    #
    # An explicit --port / --database-uri / --artifact-location means "be a
    # DEDICATED server here" — the daemon's own spawn (ensure_local_omnicraft_server)
    # and the e2e harness both do this. Such a server must bind its requested
    # port and must NOT consult or register in the shared pidfile, or it would
    # reuse/hijack the canonical server and exit without ever binding its port.
    # Likewise a non-loopback bind (`--host 0.0.0.0`, a real deploy) is exempt
    # and binds the exact port.
    _is_canonical_local_server = (
        host in ("127.0.0.1", "localhost")
        and database_uri is None
        and artifact_location is None
        and not port_was_explicit
    )

    # Single-user marker: ANY loopback-bound `omnicraft server` running
    # the env-unset header default IS a local single-user runtime — the
    # user's own machine, no proxy to inject identity — so it keeps the
    # no-login header-mode "local" fallback (same posture as the daemon
    # / `omnicraft run` spawn paths, which set this var themselves). The
    # bind address is the discriminator, NOT the port/db-uri: a
    # dedicated `omnicraft server --port 9001 --database-uri …` on
    # loopback (manual local runs, the e2e harness) is still single
    # user, so it must not 401 its own headerless traffic. What stays
    # fail-closed: a non-loopback bind (`--host 0.0.0.0`,
    # a network-exposed deploy — those MUST front a proxy or use
    # accounts/oidc) and an explicit OMNICRAFT_AUTH_PROVIDER=header
    # deploy behind an identity-injecting proxy. setdefault so an
    # operator's explicit OMNICRAFT_LOCAL_SINGLE_USER=0 wins. Must run
    # before create_auth_provider() below, which reads the var.
    from omnicraft.server.auth import resolve_auth_source as _resolve_auth_source

    _is_loopback_bind = host in ("127.0.0.1", "localhost", "::1")
    # Compose-style deploys pass OMNICRAFT_AUTH_PROVIDER as an empty
    # string when unset ("${VAR:-}"), so empty and missing both mean
    # "not explicitly pinned".
    _raw_auth_provider = os.environ.get("OMNICRAFT_AUTH_PROVIDER")
    _auth_provider_explicit = bool(_raw_auth_provider and _raw_auth_provider.strip())
    if _is_loopback_bind and not _auth_provider_explicit and _resolve_auth_source() == "header":
        os.environ.setdefault("OMNICRAFT_LOCAL_SINGLE_USER", "1")

    if _is_canonical_local_server:
        from omnicraft.host.local_server import (
            local_server_url_if_healthy,
            pick_local_port,
        )

        _existing = local_server_url_if_healthy()
        if _existing is not None:
            click.echo(
                f"Um servidor local já está rodando em {_existing} — reutilizando.\n"
                "Pare-o primeiro se quiser iniciar um novo "
                "(ou passe --server <url> para mirar em um servidor diferente)."
            )
            return
        _picked = pick_local_port(port)
        if _picked != port:
            click.echo(
                f"  ⚠ porta {port} está ocupada — usando {_picked} no lugar.",
                err=True,
            )
        port = _picked

    import uvicorn
    import uvicorn.server

    from omnicraft.runner.transports.ws_tunnel.limits import (
        RUNNER_TUNNEL_MAX_MESSAGE_BYTES,
        TUNNEL_KEEPALIVE_PING_INTERVAL_S,
        TUNNEL_KEEPALIVE_PING_TIMEOUT_S,
    )
    from omnicraft.server.app import create_app
    from omnicraft.server.auth import create_auth_provider
    from omnicraft.server.server_config import config_str_list
    from omnicraft.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnicraft.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnicraft.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnicraft.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnicraft.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore

    cfg = _load_config(config_path)

    # CLI args take precedence over config file, which takes precedence
    # over defaults.
    db_uri = database_uri or cfg.get("database_uri", _default_db_uri())
    art_loc = artifact_location or cfg.get("artifact_location", _default_artifact_location())

    # Resolve relative artifact location against config file's directory
    # (only when the value came from the config file, not CLI).
    if config_path and artifact_location is None and not Path(art_loc).is_absolute():
        art_loc = str(Path(config_path).parent / art_loc)

    # SQLite won't create the DB file's parent dir; do it before any store
    # connects, else a fresh <data_dir> (first run, or a cleared dir) fails
    # with "unable to open database file".
    _ensure_sqlite_parent_dir(db_uri)

    from omnicraft.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    agent_store = SqlAlchemyAgentStore(db_uri)
    file_store = SqlAlchemyFileStore(db_uri)
    conversation_store = SqlAlchemyConversationStore(db_uri)
    comment_store = SqlAlchemyCommentStore(db_uri)
    policy_store = SqlAlchemyPolicyStore(db_uri)
    permission_store = SqlAlchemyPermissionStore(db_uri)
    artifact_store = _create_artifact_store(art_loc)

    # Initialize the runtime with store references so workflow code
    # can access them via getter functions (get_agent_cache(), etc.).
    from omnicraft.runtime import init as init_runtime
    from omnicraft.runtime.agent_cache import AgentCache
    from omnicraft.runtime.caps import RuntimeCaps

    agent_cache = AgentCache(
        artifact_store=artifact_store,
        cache_dir=Path(art_loc) / ".cache",
    )
    # CLI flag > config file > RuntimeCaps default (7200s = 2 hours).
    # 7200 matches RuntimeCaps.execution_timeout default.
    effective_timeout = execution_timeout or cfg.get("execution_timeout") or 7200

    from omnicraft.spec import parse_default_policies, parse_server_llm

    server_llm = parse_server_llm(cfg.get("llm"))

    # Build the default LLM-based routing client when BOTH the server
    # has an ``llm:`` config AND the feature is explicitly enabled via
    # OMNICRAFT_SMART_ROUTING=1.  Hidden by default — managed deployments
    # override RuntimeCaps.routing_client with their own implementation.
    routing_client = None
    if server_llm is not None and os.environ.get("OMNICRAFT_SMART_ROUTING") == "1":
        from omnicraft.runtime.policies.builder import (
            _build_policy_llm_client,
            _resolve_server_llm_connection,
        )

        _conn = _resolve_server_llm_connection(server_llm)
        _policy_client = _build_policy_llm_client(server_llm, _conn)
        if _policy_client is not None:
            from omnicraft.server.smart_routing import LLMRoutingClient

            routing_client = LLMRoutingClient(_policy_client)

    caps = RuntimeCaps(
        execution_timeout=int(effective_timeout),
        default_policies=parse_default_policies(cfg.get("policies")),
        llm=server_llm,
        routing_client=routing_client,
    )
    init_runtime(
        conversation_store=conversation_store,
        agent_store=agent_store,
        agent_cache=agent_cache,
        file_store=file_store,
        artifact_store=artifact_store,
        comment_store=comment_store,
        policy_store=policy_store,
        caps=caps,
    )

    # Initialize OpenTelemetry observability. No-op when
    # OTEL_EXPORTER_OTLP_ENDPOINT is unset; see
    # designs/OBSERVABILITY.md for the env var reference.
    from omnicraft.runtime import telemetry

    telemetry.init("omni-server")

    # Read a pre-shared tunnel token from the environment if the
    # caller (e.g. _start_local_server) spawns the runner externally
    # and needs the server to accept exactly that runner's tunnel.
    # When unset the server accepts any token-bound runner
    # (runner_tunnel_tokens=None) — the standard posture for deployed
    # servers where runners authenticate via Databricks OAuth.
    _tunnel_token = os.environ.get("OMNICRAFT_RUNNER_TUNNEL_TOKEN")
    _runner_tunnel_tokens: frozenset[str] | None = (
        frozenset({_tunnel_token}) if _tunnel_token else None
    )

    # Pre-register agents from --agent directories.
    for agent_dir in agent_dirs:
        _preregister_agent(
            Path(agent_dir),
            agent_store,
            artifact_store,
            agent_cache,
        )

    from omnicraft.stores.host_store import HostStore

    host_store = HostStore(db_uri)

    # Managed sandbox hosts (host_type="managed" sessions): parse the
    # config's `sandbox:` section up front so an operator typo stops
    # startup instead of 502-ing the first managed session.
    from omnicraft.server.managed_hosts import parse_sandbox_config

    try:
        sandbox_config = parse_sandbox_config(cfg.get("sandbox"))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    # Accounts mode ergonomics: when accounts mode is selected
    # (OMNICRAFT_AUTH_ENABLED=1 without OIDC config, or an explicit
    # OMNICRAFT_AUTH_PROVIDER=accounts), supply sensible defaults
    # for the two vars they would otherwise have to set manually.
    # Both defaults respect operator overrides (setdefault, no
    # override clobber). We gate on the *resolved* selection (not
    # just "auth provider unset") so a bare header-mode local server
    # — the env-unset default — and an OIDC deploy don't mint accounts
    # secrets they never read.
    #
    # COOKIE_SECRET: persist in the artifact dir so sessions survive
    # restart. Operator-set value still wins for HA deploys.
    # BASE_URL: default to the CLI's bind+port so local dev "just
    # works". Docker / remote deploys behind a public domain still
    # set this explicitly.
    from omnicraft.server.auth import resolve_auth_source

    if resolve_auth_source() == "accounts":
        from omnicraft.server.accounts_secret import load_or_generate_cookie_secret

        os.environ.setdefault(
            "OMNICRAFT_ACCOUNTS_COOKIE_SECRET",
            load_or_generate_cookie_secret(art_loc),
        )
        os.environ.setdefault("OMNICRAFT_ACCOUNTS_BASE_URL", f"http://{host}:{port}")

    auth_provider = create_auth_provider()

    # Accounts mode: construct the AccountStore (sibling to PermissionStore)
    # here and pass it to create_app explicitly. Any deploy that doesn't run
    # accounts (the internal hosted product) passes account_store=None and
    # the entire accounts surface stays inactive.
    account_store = None
    from omnicraft.server.auth import UnifiedAuthProvider as _UAP

    if isinstance(auth_provider, _UAP) and auth_provider._source == "accounts":
        from omnicraft.server.accounts_store import SqlAlchemyAccountStore

        account_store = SqlAlchemyAccountStore(db_uri)

    app = create_app(
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        comment_store=comment_store,
        policy_store=policy_store,
        artifact_store=artifact_store,
        agent_cache=agent_cache,
        runner_tunnel_tokens=_runner_tunnel_tokens,
        permission_store=permission_store,
        auth_provider=auth_provider,
        host_store=host_store,
        account_store=account_store,
        policy_modules=cfg.get("policy_modules"),
        admins=config_str_list(cfg.get("admins")),
        allowed_domains=config_str_list(cfg.get("allowed_domains")),
        sandbox_config=sandbox_config,
    )

    click.echo(f"Iniciando servidor omnicraft em {host}:{port}")
    click.echo(f"  banco de dados:  {db_uri}")
    click.echo(f"  artifacts: {art_loc}")
    # A foreground server streams uvicorn logs to this terminal, but the
    # always-on diagnostics (omnicraft.* loggers, captured warnings) also land
    # in a persistent per-invocation file — point at it so there's a concrete
    # log to grep after the terminal scrolls. None only in the detached spawn
    # path (`-m omnicraft.cli server`, no setup_cli_logging), whose captured
    # log `server start` already reports.
    from omnicraft.cli_diagnostics import current_cli_log_path

    _cli_log = current_cli_log_path()
    if _cli_log is not None:
        click.echo(f"  log:       {_display_path(_cli_log)}")

    # First-run terminal setup: the FALLBACK entry point. Fires only on
    # an interactive TTY when no admin exists AND the browser isn't about
    # to open the web Create-admin form (i.e. --no-open, or a non-loopback
    # base URL). The default `omnicraft server` on loopback opens the
    # browser to the form instead, so this no-ops there. (The other entry
    # points are --admin-password and the web form.)
    _maybe_prompt_first_admin(account_store, auth_provider, auto_open=auto_open)

    # Warn loudly when the SPA bundle is absent: the server still boots
    # but serves an API-only JSON landing at "/", so the operator hits
    # http://host:port expecting the web UI and gets JSON with no clue
    # why. The bundle is npm-build output (not tracked in git); a dev
    # checkout that never ran `npm run build` has an empty static dir.
    from omnicraft.server.app import _WEB_UI_DIST

    if not (_WEB_UI_DIST / "index.html").is_file():
        click.echo(
            "  ⚠ web UI não foi buildado — servindo apenas a API. "
            "Rode `cd web && npm install && npm run build`, "
            "depois reinicie (ou instale um wheel/imagem de release).",
            err=True,
        )

    # Advertise this server in the shared pidfile so the run/host
    # daemon discovers and reuses it (loopback only). Cleared on exit so
    # a clean shutdown doesn't leave a stale record.
    if _is_canonical_local_server:
        from omnicraft.host.local_server import (
            clear_local_server_record,
            register_local_server,
        )

        # Stamp the same config signature host/run compute so they reuse
        # this foreground server instead of tearing it down on a spurious
        # sig mismatch.
        register_local_server(port)

    class _ShutdownSignalingServer(uvicorn.server.Server):
        """uvicorn.Server that signals active SSE subscribers before the
        graceful-shutdown wait starts.

        uvicorn calls ``Server.shutdown()`` in this order:
          1. close listening sockets / call connection.shutdown()
          2. ``asyncio.wait_for(_wait_tasks_to_complete(), timeout=…)``
          3. force-cancel remaining tasks on timeout
          4. run the ASGI lifespan shutdown handler

        The ASGI lifespan ``finally`` block runs at step 4 — too late. SSE
        generators waiting on a heartbeat tick are already force-cancelled by
        step 3, which produces spurious ``CancelledError`` tracebacks.
        Overriding here lets us drain SSE streams before step 2 so they exit
        cleanly within the graceful window.
        """

        async def shutdown(self, sockets=None) -> None:  # type: ignore[override]
            import asyncio as _asyncio

            from omnicraft.runtime import session_stream as _session_stream

            _session_stream.shutdown_all()
            # Yield to the event loop so generators can consume _DONE,
            # flush their final "data: [DONE]\n\n" chunk, and exit before
            # super().shutdown() calls connection.shutdown() / transport.close().
            # Without this pause the generators write to an already-closing
            # transport, leaving connections open past the graceful window.
            await _asyncio.sleep(0)
            await super().shutdown(sockets)

    _config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=_server_uvicorn_log_config(),
        ws_max_size=RUNNER_TUNNEL_MAX_MESSAGE_BYTES,
        # Server side of the runner/host tunnels' protocol keepalive, aligned
        # to the 90 s app-level budget instead of uvicorn's 20 s default that
        # drops a busy-but-healthy tunnel with 1011 — issue #1116.
        #
        # uvicorn's ws_ping_* is server-global (no per-route override), so this
        # 30 s/90 s budget also applies to the app's other WebSocket routes —
        # /v1/sessions/updates (browser stream) and .../terminals/{id}/attach.
        # Deliberate and acceptable: for an IDLE such socket the protocol
        # PING/PONG is the only half-open detector (the sessions-updates
        # heartbeat is a server->client send, and an idle terminal has no
        # traffic), so widening it means a dead idle browser/terminal socket is
        # reaped at worst ~120 s (30 s interval + 90 s timeout) instead of
        # ~40 s — a slightly later half-open cleanup (e.g. the out-of-process
        # terminal-attach proxy holds its runner socket + tmux child ~80 s
        # longer), bounded and eventually reaped, not a leak or correctness
        # change. The tunnels are the sockets that actually need the looser
        # budget (issue #1116).
        ws_ping_interval=TUNNEL_KEEPALIVE_PING_INTERVAL_S,
        ws_ping_timeout=TUNNEL_KEEPALIVE_PING_TIMEOUT_S,
        timeout_graceful_shutdown=_SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S,
    )
    try:
        _ShutdownSignalingServer(_config).run()
    except KeyboardInterrupt:
        # uvicorn.run() swallows KeyboardInterrupt; match that behaviour so
        # a Ctrl-C exit doesn't print Click's "Aborted!" or exit non-zero.
        pass
    finally:
        if _is_canonical_local_server:
            clear_local_server_record()


def _stop_local_server_and_daemon(*, force: bool) -> bool:
    """Stop the background OmniCraft server and the local host daemon that owns it.

    Stops the local-mode host daemon first (the daemon spawns its server
    once and never respawns it, so leaving it alive would only have it
    reconnect-flap against a dead server), then the detached OmniCraft server
    recorded in ``~/.omnicraft/local_server.pid``. Best-effort and
    idempotent — a missing daemon or server is a no-op.

    :param force: SIGKILL the daemon after the grace period if it does not
        exit on SIGTERM.
    :returns: ``True`` if a healthy background server was running when
        called, ``False`` otherwise.
    """
    was_running = local_server_url_if_healthy() is not None
    local_record = _find_daemon_record(_LOCAL_DAEMON_MARKER)
    if local_record is not None:
        # A stubborn daemon shouldn't block stopping the server.
        with contextlib.suppress(click.ClickException):
            _terminate_daemon(local_record, force=force)
    stop_local_omnicraft_server()
    # Also catch an orphan on the canonical port whose pidfile was lost, so
    # `server stop` isn't blind to it (it reported "No background server is
    # running" while one was still listening on the default port).
    orphan_pid = stop_untracked_local_server()
    return was_running or orphan_pid is not None


@server.command("start")
def server_start() -> None:
    """Garante que o servidor OmniCraft gerenciado em segundo plano está rodando.

    Reutiliza um servidor em segundo plano saudável se já houver um no ar (iniciado
    aqui ou por um ``run`` / ``host`` anterior); caso contrário, cria um destacado em
    uma porta loopback livre e imprime sua URL. A contraparte em segundo plano do
    ``omnicraft server`` puro em primeiro plano.

    :returns: None.
    """
    startup = ensure_local_omnicraft_server()
    verb = (
        "Servidor em segundo plano iniciado em"
        if startup.spawned
        else "Servidor em segundo plano já rodando em"
    )
    click.echo(f"{verb} {startup.url}")
    # Surface the exact log file so a detached server isn't a black box —
    # `server start` is otherwise the only signal it ever emits. Known for a
    # spawned server and (via the log-path sidecar) for a reused one too;
    # absent only for a foreground `omnicraft server` whose logs stream to
    # its own terminal.
    if startup.log_path is not None:
        click.echo(f"  log: {_display_path(startup.log_path)}")


@server.command("stop")
@click.option(
    "--force",
    is_flag=True,
    help="SIGKILL no daemon host local se ele não sair no SIGTERM.",
)
def server_stop(force: bool) -> None:
    """Para o servidor OmniCraft em segundo plano e o daemon host local.

    Para primeiro o daemon host local, depois o servidor destacado registrado
    em ``~/.omnicraft/local_server.pid`` — seu web UI e sessões ficam
    inacessíveis. Para parar o hosting mas MANTER o servidor no ar, use
    ``omnicraft host stop``; para parar tudo, use ``omnicraft stop``.

    :param force: SIGKILL no daemon host local após o período de tolerância se ele
        não sair no SIGTERM.
    :returns: None.
    """
    if _stop_local_server_and_daemon(force=force):
        click.echo("Servidor em segundo plano parado.")
    else:
        click.echo("Nenhum servidor em segundo plano está rodando.")


@server.command("status")
@click.option("--json", "json_output", is_flag=True, help="Emite JSON.")
def server_status(json_output: bool) -> None:
    """Mostra se o servidor OmniCraft em segundo plano está rodando.

    Reporta o pid/porta registrados, a URL, a contagem de sessões ativas e se um
    daemon host local está anexado. Lê ``~/.omnicraft/local_server.pid``
    e sonda ``/health``.

    :param json_output: Emite JSON legível por máquina em vez de texto.
    :returns: None.
    """
    info = local_server_status()
    daemon_attached = _find_daemon_record(_LOCAL_DAEMON_MARKER) is not None
    sessions: int | None = None
    if info.running and info.url is not None:
        # Session count crosses the HTTP boundary; a transient failure
        # shouldn't break `status`, so leave the count unknown instead.
        with contextlib.suppress(click.ClickException):
            pages = _fetch_session_pages(base_url=info.url, connected_only=True)
            sessions = len(pages.sessions)
    if json_output:
        click.echo(
            json.dumps(
                {
                    "running": info.running,
                    "pid": info.pid,
                    "port": info.port,
                    "url": info.url,
                    "log_path": str(info.log_path) if info.log_path else None,
                    "live_sessions": sessions,
                    "daemon_attached": daemon_attached,
                },
                indent=2,
            )
        )
        return
    if not info.running:
        click.echo("Servidor em segundo plano: não está rodando.")
        return
    click.echo(
        f"Servidor em segundo plano: rodando em {info.url} (pid {info.pid}, porta {info.port})"
    )
    if info.log_path is not None:
        click.echo(f"  log: {_display_path(info.log_path)}")
    if sessions is not None:
        click.echo(f"  sessões ativas: {sessions}")
    click.echo(f"  daemon host anexado: {'sim' if daemon_attached else 'não'}")


@cli.command("stop")
@click.option(
    "--force",
    is_flag=True,
    help="Continua após falhas e SIGKILL nos daemons que não saem no SIGTERM.",
)
def stop(force: bool) -> None:
    """Para tudo que o OmniCraft está rodando nesta máquina.

    O botão de desligar: para todos os daemons host (locais e mirados em remoto)
    e o servidor em segundo plano destacado. Os runners são recolhidos quando seu
    daemon sai. Para parar apenas o hosting mantendo o servidor local (web UI /
    histórico) no ar, use ``omnicraft host stop`` em vez disso.

    :param force: Continua após falhas individuais e SIGKILL nos daemons que
        não saem no SIGTERM.
    :returns: None.
    """
    stopped = 0
    failures: list[str] = []
    for record in _list_daemon_records():
        # Terminating the daemon reaps its runners (orphan-watchdog), so the
        # off-switch doesn't need the graceful per-session HTTP stop that
        # `host stop` does — that keeps teardown quiet and dependency-free.
        try:
            _terminate_daemon(record, force=force)
            stopped += 1
        except click.ClickException as exc:
            failures.append(exc.message)
    server_was_running = local_server_url_if_healthy() is not None
    stop_local_omnicraft_server()
    # Sweep the canonical port for an orphaned server the pidfile lost track
    # of (a torn/cleared record, or a respawn that landed elsewhere). Without
    # this, that server survives the off-switch — the exact "I ran stop and a
    # server is still on the default port" symptom.
    orphan_pid = stop_untracked_local_server()

    parts: list[str] = []
    if stopped:
        parts.append(f"{stopped} daemon(s)")
    if server_was_running:
        parts.append("o servidor em segundo plano")
    if orphan_pid is not None:
        parts.append(f"um servidor não rastreado em :{_DEFAULT_LOCAL_PORT} (pid {orphan_pid})")
    if parts:
        click.echo("Parado(s): " + " e ".join(parts) + ".")
    else:
        click.echo("Nada para parar.")
    if failures:
        raise click.ClickException("; ".join(failures) + " — tente de novo com --force.")


def _count_running_sessions(base_url: str) -> int:
    """Count sessions actively running a turn on the local server.

    Gates on the session-list ``status`` field (``"running"`` — a runner
    mid-turn, or with a still-running sub-agent), NOT mere connectedness:
    an idle session keeps its host/runner connection open indefinitely, so
    counting connected sessions would make the drain wait forever for
    sessions that aren't doing any work. Only ``"running"`` sessions hold
    in-flight work an upgrade should avoid interrupting.

    A transient HTTP failure is treated as "none running" rather than
    blocking the upgrade — the server's own graceful shutdown still drains
    any runner that happens to be mid-turn.

    :param base_url: Local server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :returns: Number of sessions with ``status == "running"``, or ``0`` on
        a query failure.
    """
    with contextlib.suppress(click.ClickException):
        pages = _fetch_session_pages(base_url=base_url, connected_only=True)
        return sum(1 for session in pages.sessions if session.get("status") == "running")
    return 0


def _wait_for_local_sessions_to_drain() -> None:
    """Block until no local session is actively running a turn.

    Used by ``omni upgrade`` (without ``--force``) so an upgrade never
    yanks a running agent turn. Waits only on sessions whose status is
    ``"running"`` (see :func:`_count_running_sessions`) — idle-but-connected
    sessions do not hold it up. Polls every :data:`_UPGRADE_DRAIN_POLL_S`
    seconds and re-prints the count whenever it changes; ``Ctrl-C`` aborts
    the wait (and the upgrade) cleanly. Returns immediately when the server
    is down or already idle.
    """
    info = local_server_status()
    if not (info.running and info.url is not None):
        return
    count = _count_running_sessions(info.url)
    if count == 0:
        return
    click.echo(
        f"Esperando {count} sessão(ões) em execução terminar — pressione Ctrl-C para "
        "abortar, ou rode de novo com --force para pará-las agora."
    )
    last = count
    while True:
        time.sleep(_UPGRADE_DRAIN_POLL_S)
        info = local_server_status()
        if not (info.running and info.url is not None):
            return
        count = _count_running_sessions(info.url)
        if count == 0:
            return
        if count != last:
            click.echo(f"  {count} sessão(ões) ainda em execução…")
            last = count


def _drain_and_stop_local_server(*, force: bool) -> None:
    """Drain (or force-stop) the local server + daemon before an upgrade.

    Shared by both ``omni upgrade`` paths (registry and git): the running
    process must stop serving BEFORE its code is swapped, so it never serves
    half-upgraded modules. The next ``omni`` invocation respawns a fresh
    server on the new version.

    :param force: When ``False``, wait for in-flight sessions to drain first;
        when ``True``, stop them immediately.
    """
    if not force:
        _wait_for_local_sessions_to_drain()
    if _stop_local_server_and_daemon(force=force):
        click.echo("Servidor em segundo plano parado antes da atualização.")


def _upgrade_vcs_install(
    info: _InstalledWheelInfo, *, check_only: bool, force: bool, pre: bool
) -> None:
    """Update a git/VCS ``omni`` install by re-pulling its tracked ref.

    A git install's version string is frozen at whatever its source branch
    declares (e.g. ``0.1.0`` on an unbumped ``main``), so it cannot be
    compared against PyPI — that comparison reports a build *ahead* of the
    latest release as "behind" and never converges, because reinstalling the
    ref can't change the version string. Instead, compare the installed commit
    against the remote ref's HEAD, and after re-pulling verify the commit
    actually moved rather than asserting a PyPI version the ref can't produce.

    :param info: Installed-distribution metadata, with ``info.vcs_url`` set.
    :param check_only: Report status only; exit non-zero only when we can
        positively confirm the install is behind its tracked ref.
    :param force: Stop in-flight sessions immediately instead of draining.
    :param pre: Pass the installer's allow-pre-releases flag (no-op for git).
    """
    from omnicraft.update_check import (
        _build_upgrade_suggestion,
        _probe_installed_distribution,
        _remote_git_head,
        _run_upgrade_command,
    )

    current_sha = info.commit_sha or ""
    cur_short = current_sha[:9] if current_sha else "unknown"
    remote_sha = _remote_git_head(info.vcs_url) if info.vcs_url else None
    remote_short = remote_sha[:9] if remote_sha else ""
    known_behind = bool(remote_sha and current_sha and remote_sha != current_sha)

    if remote_sha and current_sha and remote_sha == current_sha:
        click.echo(f"omnicraft está atualizado (git {cur_short}, seguindo {info.vcs_url}).")
        return
    if known_behind:
        click.echo(
            f"Um commit mais novo está disponível: {cur_short} → {remote_short} "
            f"(instalação git seguindo {info.vcs_url})."
        )
    else:
        click.echo(
            f"Esta é uma instalação git ({info.vcs_url} @ {cur_short}). O commit mais "
            "recente não pôde ser determinado; re-puxando o ref seguido."
        )

    if check_only:
        # Exit non-zero only when we KNOW it's behind, so `--check` stays a
        # reliable CI gate; an indeterminate remote is not a failure. SystemExit
        # (not ctx.exit) for the same reason as the PyPI path — main() runs the
        # group with standalone_mode=False, where ctx.exit's code is dropped.
        if known_behind:
            raise SystemExit(1)
        return

    if pre:
        # ``--pre`` only steers a PyPI resolve; a git install gets exactly the
        # commit its ref points at, so say so rather than implying it had effect.
        click.echo(
            "Nota: --pre não tem efeito em uma instalação git; o ref seguido decide o commit."
        )

    suggestion = _build_upgrade_suggestion(info, allow_prerelease=pre)
    if not suggestion.runnable:
        raise click.ClickException(
            f"Nenhum comando de atualização automática é conhecido para esta instalação. "
            f"{suggestion.command}."
        )

    _drain_and_stop_local_server(force=force)

    console = Console()
    code = _run_upgrade_command(suggestion.command, console)
    if code != 0:
        raise click.ClickException(
            f"O comando de atualização saiu com status {code}; "
            "sua instalação anterior está intacta."
        )

    # Verify by commit, not exit code: a re-pull of a ref that hasn't moved (or
    # a pinned ref, or a cached reinstall) exits 0 without changing anything.
    _, new_sha = _probe_installed_distribution()
    if new_sha and current_sha and new_sha != current_sha:
        click.echo(
            f"✓ Atualizado para git {new_sha[:9]}. Rode seu comando de novo — o "
            "servidor local vai iniciar na nova versão."
        )
        return
    if known_behind and new_sha and new_sha == current_sha:
        # We positively confirmed the ref had advanced, yet the re-pull left the
        # install on the same commit — a silent no-op that would otherwise
        # recreate the "still behind" loop. Fail loudly, mirroring the PyPI guard.
        raise click.ClickException(
            f"O re-pull rodou mas a instalação ainda está em {cur_short} (o ref está em "
            f"{remote_short}). O ref pode estar fixado ou a reinstalação reutilizou um "
            f"commit em cache; tente `uv tool install --reinstall {info.vcs_url}`."
        )
    if new_sha and current_sha and new_sha == current_sha:
        # Remote was indeterminate, so we never claimed it was behind — a
        # no-change re-pull is fine here.
        click.echo(f"Já no commit mais recente do ref seguido ({cur_short}); nada mudou.")
        return
    # Couldn't read the new commit — the re-pull ran, but don't assert a
    # result we can't confirm.
    click.echo("Ref git re-puxado. Rode `omni upgrade --check` para confirmar.")


@cli.command("upgrade")
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    help="Reporta se uma release mais nova está disponível, sem atualizar. "
    "Sai com código não-zero quando existe uma release mais nova.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Para sessões em andamento imediatamente em vez de esperar drenarem.",
)
@click.option(
    "--pre",
    "pre",
    is_flag=True,
    help="Considera pré-releases (ex. release candidates) e passa a "
    "flag allow-pre-releases do instalador. Útil para validar um rc do TestPyPI.",
)
def upgrade(check_only: bool, force: bool, pre: bool) -> None:
    """Atualiza o CLI omnicraft para a release mais recente no PyPI.

    Detecta como o omnicraft foi instalado (uv / pip / pipx / poetry), verifica
    no índice configurado uma release mais nova e — a menos que ``--check`` —
    drena e para o servidor local em segundo plano e o daemon host, depois roda
    o comando de atualização correspondente. A próxima invocação de ``omni`` inicia
    um servidor novo no código atualizado automaticamente (via a assinatura de
    config ciente da versão), então nenhum restart explícito é necessário.

    As sessões de agente em andamento são aguardadas por padrão; passe ``--force``
    para pará-las imediatamente. Passe ``--pre`` para considerar pré-releases (rc /
    beta) — útil para validar um candidato do TestPyPI contra seu índice
    configurado. Checkouts de código / instalações editáveis não são atualizados
    aqui — atualize-os com ``git pull``.

    :param check_only: Apenas reporta disponibilidade; não atualiza. Sai
        com status 1 quando existe uma release mais nova.
    :param force: Para sessões em andamento imediatamente em vez de drenar.
    :param pre: Considera pré-releases e permite ao instalador buscá-las.
    :returns: None.
    """
    import importlib.metadata

    from omnicraft.update_check import (
        _UPGRADE_INDEX_TIMEOUT_SECONDS,
        _build_upgrade_suggestion,
        _find_repo_root,
        _is_newer,
        _probe_installed_distribution,
        _read_installed_wheel_info,
        _run_upgrade_command,
        fetch_latest_version,
    )

    # Source checkout / editable install — there's no released wheel to
    # swap in place; the correct update path is git, not a reinstall.
    if _find_repo_root() is not None:
        raise click.ClickException(
            "Este é um checkout de código — atualize-o com `git pull` (e reinstale "
            "as dependências), não `omni upgrade`."
        )
    info = _read_installed_wheel_info()
    if info is None:
        raise click.ClickException(
            "Não foi possível determinar como o omnicraft está instalado; atualize-o manualmente."
        )
    if info.is_editable:
        raise click.ClickException(
            "Esta é uma instalação editável — atualize-a com `git pull`, não `omni upgrade`."
        )

    # A git/VCS install tracks a moving git ref, not a PyPI release. Its
    # version string (a frozen ``0.1.0`` on an unbumped ``main``, say) is NOT
    # comparable to the latest PyPI release: comparing them reports a build
    # that is *ahead* of the release as "behind" and loops forever, because
    # reinstalling the ref can never change that version string. For these
    # installs "upgrade" means re-pulling the ref — compared and verified by
    # commit, not by PyPI version.
    if info.vcs_url:
        _upgrade_vcs_install(info, check_only=check_only, force=force, pre=pre)
        return

    current = importlib.metadata.version("omnicraft")
    # User-initiated: a more forgiving timeout + one retry so a momentarily slow
    # mirror doesn't spuriously report the index as unreachable.
    latest = fetch_latest_version(
        include_prereleases=pre, timeout=_UPGRADE_INDEX_TIMEOUT_SECONDS, attempts=2
    )
    if latest is None:
        raise click.ClickException(
            "Não foi possível acessar o índice de pacotes para checar uma release mais nova. "
            "Verifique sua conexão (ou OMNICRAFT_INDEX_URL / seu índice configurado) e "
            "tente de novo."
        )
    if not _is_newer(latest, current):
        click.echo(f"omnicraft está atualizado (v{current}).")
        return

    click.echo(f"Uma nova release está disponível: v{current} → v{latest}.")
    if check_only:
        # Non-zero so scripts/CI can gate on "an upgrade is available".
        # SystemExit (not ctx.exit) because main() runs the group with
        # standalone_mode=False, where ctx.exit's code is returned and
        # dropped rather than applied — SystemExit propagates correctly.
        raise SystemExit(1)

    suggestion = _build_upgrade_suggestion(info, allow_prerelease=pre)
    if not suggestion.runnable:
        raise click.ClickException(
            f"Nenhum comando de atualização automática é conhecido para esta instalação. "
            f"{suggestion.command}."
        )

    _drain_and_stop_local_server(force=force)

    console = Console()
    code = _run_upgrade_command(suggestion.command, console)
    if code != 0:
        raise click.ClickException(
            f"O comando de atualização saiu com status {code}; "
            "sua instalação anterior está intacta."
        )

    # Trust the installed version, not the installer's exit code. The running
    # process still has the OLD version loaded, so re-read it in a fresh
    # subprocess. A no-op upgrade (version-pinned spec, a cooldown /
    # exclude-newer that excludes the new release, or a stale index cache)
    # exits 0 without moving — claiming "✓ Upgraded" there is exactly the
    # "I upgraded but it still says an update is available" bug.
    new_version, _ = _probe_installed_distribution()
    if new_version is None:
        click.echo(
            "O comando de atualização rodou, mas não foi possível confirmar a versão "
            "instalada. Rode `omni upgrade --check` para verificar."
        )
        return
    if _is_newer(new_version, current):
        click.echo(
            f"✓ Atualizado para v{new_version}. Rode seu comando de novo — o "
            "servidor local vai iniciar na nova versão."
        )
        return
    raise click.ClickException(
        f"O comando de atualização rodou mas o omnicraft ainda é v{new_version} (esperado "
        f"v{latest}). A instalação provavelmente está com versão fixada, um cooldown / "
        "exclude-newer está excluindo a nova release, ou o cache do índice está velho. "
        "Reinstale explicitamente — ex. `uv tool upgrade --reinstall omnicraft` ou "
        f"`pip install --force-reinstall 'omnicraft=={latest}'`."
    )


# ``omni update`` is an alias for ``omni upgrade`` — mistyping the latter as
# the former is common, and silently doing nothing is annoying. Registering
# the same Command object under a second name shares the exact callback,
# options, and semantics; there is no duplicated implementation to drift.
cli.add_command(upgrade, name="update")


def _bundle(source: Path) -> bytes:
    """
    Produce a tar.gz bundle from a directory or standalone
    OmniCraft YAML file, or pass through an existing tarball.

    Environment variable references (``${VAR}``) in
    ``config.yaml`` and ``tools/mcp/*.yaml`` are expanded
    using the client's environment before bundling. This
    ensures the server receives resolved secrets rather
    than unresolved ``${VAR}`` references it cannot
    resolve.

    :param source: Path to an agent image directory,
        standalone OmniCraft YAML file, or an existing
        ``.tar.gz`` bundle file.
    :returns: The gzipped tarball bytes.
    :raises OmniCraftError: If a required env var is
        missing during expansion.
    """
    import io
    import tarfile

    if source.is_file() and source.suffix.lower() in {".yaml", ".yml"}:
        from omnicraft.spec import materialize_bundle

        with tempfile.TemporaryDirectory() as tmpdir:
            bundle_dir = materialize_bundle(source, Path(tmpdir) / "bundle")
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                for file_path in bundle_dir.rglob("*"):
                    if file_path.is_file():
                        tf.add(str(file_path), arcname=str(file_path.relative_to(bundle_dir)))
            return buf.getvalue()

    if source.is_file():
        return source.read_bytes()

    # Pre-resolve env vars in YAML files that contain secrets.
    resolved = _resolve_bundle_env_vars(source)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for file_path in source.rglob("*"):
            if file_path.is_file():
                arcname = str(file_path.relative_to(source))
                if arcname in resolved:
                    # Write the resolved YAML instead of the
                    # original file (which has ${VAR} refs).
                    data = resolved[arcname].encode("utf-8")
                    info = tarfile.TarInfo(name=arcname)
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))
                else:
                    tf.add(str(file_path), arcname=arcname)
    return buf.getvalue()


def _resolve_bundle_env_vars(source: Path) -> dict[str, str]:
    """
    Expand ``${VAR}`` references in YAML files that contain
    secrets, using the client's environment.

    Returns a mapping of ``arcname → resolved YAML text`` for
    files that were modified. Files without env var references
    are omitted (bundled as-is).

    Expanded fields:

    - ``config.yaml``: ``llm.connection.*`` and
      ``executor.connection.*`` values, ``executor.auth``
      ``api_key`` / ``base_url`` (when ``type: api_key``), and
      ``tools.builtins[*]`` dict-entry values (except ``name``)
    - ``tools/mcp/*.yaml``: ``headers.*`` and ``env.*`` values

    These mirror the server-side parser's ``${VAR}`` expansion
    sites. Resolving here, against the client's own environment,
    is what keeps secrets working now that the server refuses to
    expand tenant-uploaded bundles against its process env.

    :param source: The agent image directory.
    :returns: ``{arcname: resolved_yaml_text}`` for files
        that had env vars expanded.
    :raises OmniCraftError: If a ``${VAR}`` reference
        cannot be resolved from the environment.
    """
    from omnicraft.spec import expand_env_vars

    resolved: dict[str, str] = {}

    # ── config.yaml ──────────────────────────────────
    config_path = source / "config.yaml"
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text())
        if isinstance(raw, dict):
            changed = _expand_config_env_vars(raw, expand_env_vars)
            if changed:
                resolved["config.yaml"] = yaml.dump(
                    raw,
                    default_flow_style=False,
                )

    # ── tools/mcp/*.yaml ─────────────────────────────
    # ``headers`` (HTTP transport auth) and ``env`` (stdio transport
    # process env) are both secret-bearing and both expanded by the
    # server-side parser, so resolve both client-side.
    mcp_dir = source / "tools" / "mcp"
    if mcp_dir.is_dir():
        for yaml_file in sorted(mcp_dir.glob("*.yaml")):
            raw = yaml.safe_load(yaml_file.read_text())
            if not isinstance(raw, dict):
                continue
            changed = False
            for field in ("headers", "env"):
                value = raw.get(field)
                if isinstance(value, dict):
                    raw[field] = expand_env_vars(
                        {str(k): str(v) for k, v in value.items()},
                    )
                    changed = True
            if changed:
                arcname = str(yaml_file.relative_to(source))
                resolved[arcname] = yaml.dump(
                    raw,
                    default_flow_style=False,
                )

    return resolved


class _LLMDeploy(BaseModel):  # type: ignore[explicit-any]  # Pydantic extra="allow" stubs use Any
    """
    Pydantic model for the ``llm:`` block during deploy-time
    env var expansion.

    :param connection: Key-value pairs for LLM connection
        config, e.g. ``{"api_key": "${OPENAI_API_KEY}"}``.
    """

    model_config = ConfigDict(extra="allow")
    connection: dict[str, str] | None = None


class _BuiltinEntry(BaseModel):  # type: ignore[explicit-any]  # Pydantic extra="allow" stubs use Any
    """
    Pydantic model for a single dict entry in
    ``tools.builtins`` during deploy-time env var expansion.

    :param name: The built-in tool name, e.g.
        ``"web_search"``.
    """

    model_config = ConfigDict(extra="allow")
    name: str


class _ToolsDeploy(BaseModel):  # type: ignore[explicit-any]  # builtins field is list[str | dict[str, Any]]
    """
    Pydantic model for the ``tools:`` block during deploy-time
    env var expansion.

    :param builtins: Mixed list of string tool names and dict
        entries with config fields, e.g.
        ``["web_search", {"name": "web_search",
        "api_key": "${KEY}"}]``.
    """

    model_config = ConfigDict(extra="allow")
    builtins: list[str | dict[str, Any]] | None = None  # type: ignore[explicit-any]


class _ExecutorDeploy(BaseModel):  # type: ignore[explicit-any]  # auth is a free-form mapping
    """
    Pydantic model for the ``executor:`` block during deploy-time
    env var expansion.

    Mirrors the secret-bearing fields the server-side parser
    expands (``omnicraft/spec/parser.py`` — ``_parse_executor`` /
    ``_parse_executor_auth``): the ``connection`` dict and, for
    ``auth.type == "api_key"``, the ``api_key`` / ``base_url``
    values. Resolving these client-side keeps ``${VAR}`` working
    for operator specs now that the server no longer expands
    tenant bundles.

    :param connection: Key-value pairs for executor connection
        config, e.g. ``{"api_key": "${OPENAI_API_KEY}"}``.
    :param auth: The ``auth:`` mapping, e.g.
        ``{"type": "api_key", "api_key": "${OPENAI_API_KEY}"}``.
        Only expanded when ``type == "api_key"``.
    """

    model_config = ConfigDict(extra="allow")
    connection: dict[str, str] | None = None
    auth: dict[str, Any] | None = None  # type: ignore[explicit-any]


class _DeployConfig(BaseModel):  # type: ignore[explicit-any]  # Pydantic extra="allow" stubs use Any
    """
    Pydantic model for the top-level config.yaml structure
    during deploy-time env var expansion.

    Only the fields containing secrets (``llm``, ``executor``,
    ``tools``) are modeled; all other fields pass through via
    ``extra="allow"``.

    :param llm: The LLM configuration block, or ``None``
        if absent.
    :param executor: The executor configuration block, or
        ``None`` if absent.
    :param tools: The tools configuration block, or ``None``
        if absent.
    """

    model_config = ConfigDict(extra="allow")
    llm: _LLMDeploy | None = None
    executor: _ExecutorDeploy | None = None
    tools: _ToolsDeploy | None = None


def _expand_config_env_vars(  # type: ignore[explicit-any]  # raw is parsed YAML (heterogeneous values)
    raw: dict[str, Any],
    expand_fn: Callable[[dict[str, str]], dict[str, str]],
) -> bool:
    """
    Expand ``${VAR}`` references in-place in a parsed
    ``config.yaml`` dict. Returns ``True`` if any field
    was expanded.

    Expanded fields (mirrors the server-side parser's expansion
    sites so operator specs resolve identically client-side now
    that the server no longer expands tenant bundles):

    - ``llm.connection`` — all values
    - ``executor.connection`` — all values
    - ``executor.auth`` — ``api_key`` / ``base_url`` when
      ``type == "api_key"``
    - ``tools.builtins[*]`` — dict-entry values except ``name``

    :param raw: The parsed config.yaml dict (modified in-place).
    :param expand_fn: Callable that expands env var references
        in a string-to-string dict, e.g.
        :func:`omnicraft.spec.expand_env_vars`.
    :returns: ``True`` if any values were expanded.
    """
    cfg = _DeployConfig.model_validate(raw)
    changed = False

    if cfg.llm is not None and cfg.llm.connection is not None:
        raw["llm"]["connection"] = expand_fn(cfg.llm.connection)
        changed = True

    if cfg.executor is not None and cfg.executor.connection is not None:
        raw["executor"]["connection"] = expand_fn(cfg.executor.connection)
        changed = True

    # ``executor.auth`` with ``type: api_key`` — only ``api_key`` and
    # ``base_url`` are secret-bearing (matches _parse_executor_auth).
    if (
        cfg.executor is not None
        and cfg.executor.auth is not None
        and cfg.executor.auth.get("type") == "api_key"
    ):
        auth_secrets = {
            k: str(cfg.executor.auth[k])
            for k in ("api_key", "base_url")
            if cfg.executor.auth.get(k) is not None
        }
        if auth_secrets:
            raw["executor"]["auth"].update(expand_fn(auth_secrets))
            changed = True

    if cfg.tools is not None and cfg.tools.builtins is not None:
        changed = (
            _expand_builtin_env_vars(
                raw["tools"]["builtins"],
                cfg.tools.builtins,
                expand_fn,
            )
            or changed
        )

    return changed


def _expand_builtin_env_vars(  # type: ignore[explicit-any]  # entries are parsed YAML dicts
    raw_builtins: list[str | dict[str, Any]],
    parsed_builtins: list[str | dict[str, Any]],
    expand_fn: Callable[[dict[str, str]], dict[str, str]],
) -> bool:
    """
    Expand ``${VAR}`` references in dict entries of
    ``tools.builtins``, modifying *raw_builtins* in-place.

    String entries are skipped (no config to expand). Dict
    entries have all fields except ``name`` expanded.

    :param raw_builtins: The mutable builtins list from the
        raw config dict (modified in-place).
    :param parsed_builtins: The Pydantic-parsed builtins list
        used for typed access.
    :param expand_fn: Callable that expands env var references
        in a string-to-string dict.
    :returns: ``True`` if any values were expanded.
    """
    changed = False
    for i, entry in enumerate(parsed_builtins):
        if not isinstance(entry, dict):
            continue
        parsed = _BuiltinEntry.model_validate(entry)
        # Extra fields are the tool-specific config (api_key, etc.).
        config_fields = (
            {str(k): str(v) for k, v in parsed.model_extra.items()} if parsed.model_extra else {}
        )
        if config_fields:
            expanded = expand_fn(config_fields)
            raw_builtins[i] = {"name": parsed.name, **expanded}
            changed = True
    return changed


# Click ``flag_value`` for bare ``--resume`` (no arg). Must exist
# before any command's decorator evaluates.
_RESUME_PICKER_SENTINEL = "__resume_picker__"


def _reject_native_on_windows(harness: str) -> None:
    """Fail a native (tmux/PTY) harness command with an actionable message.

    The ``omnicraft claude`` / ``codex`` / ``cursor`` native wrappers drive a
    private tmux server and PTY, which don't exist on Windows. Point users at
    the SDK harnesses / web UI instead of letting them hit a tmux crash.

    :param harness: The native command name, e.g. ``"claude"``.
    :raises click.ClickException: Always, when running on Windows.
    """
    if IS_WINDOWS:
        raise click.ClickException(
            f"`omnicraft {harness}` (terminal nativo tmux/PTY) não é suportado no "
            "Windows. Use um harness baseado em SDK via `omnicraft run <agent.yaml>` "
            "ou o web UI."
        )


@cli.command(
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    }
)
@click.option(
    "--server",
    default=None,
    help=(
        "URL omnicraft remota. Inicia um runner local, vincula a sessão, "
        "lança o Claude em um recurso de terminal e anexa este TTY. "
        'Passe --server "" para auto-criar um servidor local persistente em '
        "segundo plano e usar esse em vez de um remoto."
    ),
)
@click.option(
    "-r",
    "--resume",
    "resume",
    is_flag=False,
    flag_value=_RESUME_PICKER_SENTINEL,
    default=None,
    help=(
        "Retoma uma conversa anterior do OmniCraft. Com um id de conversa "
        "(ex. ``--resume conv_abc123``) anexa diretamente; sem valor "
        "abre um seletor interativo restrito a sessões claude-native."
    ),
)
@click.option(
    "--session",
    "session_id",
    metavar="SESSION_ID",
    default=None,
    hidden=True,
    help="Alias descontinuado para ``--resume <id>``; mantido por uma release.",
)
@click.option(
    "--host",
    "register_host",
    is_flag=True,
    default=False,
    help=(
        "Registra esta máquina como host (equivalente inline de `omnicraft host`). "
        "Requer --server."
    ),
)
@click.option(
    "--use-native-config",
    "use_claude_config",
    is_flag=True,
    default=False,
    help=(
        "Usa sua configuração existente do Claude Code em vez da auth do Databricks. "
        "Quando definido, qualquer provedor configurado é ignorado e o Claude "
        "autentica via suas próprias configs em ``~/.claude/``."
    ),
)
@click.option(
    "--profile-startup",
    "profile_startup",
    is_flag=True,
    default=False,
    help=(
        "Imprime marcas de tempo da inicialização nativa do Claude no stderr. Também "
        f"habilitado por {_CLAUDE_STARTUP_PROFILE_ENV_VAR}=1."
    ),
)
@click.option(
    "--command",
    "claude_command",
    default=None,
    metavar="CMD",
    help=(
        "Executável do CLI Claude Code para rodar. "
        "Padrão é ``claude``. Use isto quando um binário wrapper substitui o "
        "CLI ``claude`` preservando sua interface (ex. um lançador customizado "
        "que injeta auth ou ambiente antes de delegar ao ``claude``)."
    ),
)
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
def claude(
    server: str | None,
    resume: str | None,
    session_id: str | None,
    register_host: bool,
    use_claude_config: bool,
    profile_startup: bool,
    claude_command: str | None,
    claude_args: tuple[str, ...],
) -> None:
    # Param docs live in comments — Click uses the docstring for --help.
    # :param server: Remote OmniCraft server URL, or None for local.
    # :param resume: None, picker sentinel, or a conversation id.
    # :param session_id: Legacy ``--session`` id; mutually exclusive with ``--resume``.
    # :param use_claude_config: When True, skip ucode/Databricks auth and use
    #     existing Claude config.
    # :param profile_startup: When True, print startup timing marks.
    # :param claude_args: Pass-through args for ``claude``.
    """Lança o Claude Code em um terminal OmniCraft.

    \b
    Exemplos:
      omnicraft claude
      omnicraft claude --resume conv_abc123
      omnicraft claude --resume                  # seletor interativo
      omnicraft claude --server https://<app>.databricksapps.com
    """
    _reject_native_on_windows("claude")
    startup_profiler = StartupProfiler.from_env(
        name="omnicraft claude",
        env_var=_CLAUDE_STARTUP_PROFILE_ENV_VAR,
        explicit=profile_startup,
    )
    startup_profiler.mark("cli entered")

    # Apply config defaults (same as ``run`` does).
    cfg = _load_effective_config()
    if server is None:
        server = cfg.get("server")
    auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)
    startup_profiler.mark("config resolved")

    # Validate option combinations BEFORE any side effects (daemon
    # spawn, server discovery). Calling _ensure_backend first would
    # mean a bad arg pair waits the full local-server-discover
    # timeout (60s in CI) before surfacing the UsageError, which
    # the test_claude_command_session_and_resume_mutually_exclusive
    # regression caught in CI.
    del register_host
    choice = _split_resume_value(resume)
    if session_id is not None and (choice.picker or choice.conversation_id is not None):
        raise click.UsageError(
            "--session e --resume são mutuamente exclusivos; "
            "prefira --resume (--session está descontinuado).",
        )
    startup_profiler.mark("arguments validated")

    # Ensure the host daemon (local when ``--server`` is omitted/empty,
    # remote otherwise) and resolve the concrete OmniCraft server URL. The daemon
    # owns the runner; the CLI only connects. ``--host`` is now redundant
    # (the daemon is always ensured) and kept only as a no-op for scripts.
    startup_profiler.mark("ensuring backend")
    server = _ensure_backend(server)
    startup_profiler.mark("backend ready", detail=f"server={server}")

    resolved_session_id = (
        choice.conversation_id if choice.conversation_id is not None else session_id
    )

    from omnicraft.claude_native import run_claude_native

    startup_profiler.mark("native module imported")

    run_claude_native(
        server=server,
        session_id=resolved_session_id,
        resume_picker=choice.picker,
        claude_args=claude_args,
        use_claude_config=use_claude_config,
        auto_open_conversation=auto_open_conversation,
        startup_profiler=startup_profiler,
        **({"command": claude_command} if claude_command else {}),
    )


@cli.command(
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    }
)
@click.option(
    "--server",
    default=None,
    help=(
        "URL omnicraft remota. Garante o daemon host, pede ao "
        "runner criado pelo daemon para lançar o Codex e anexa este TTY. "
        'Passe --server "" para auto-criar um servidor local persistente em '
        "segundo plano e usar esse em vez de um remoto."
    ),
)
@click.option(
    "-r",
    "--resume",
    "resume",
    is_flag=False,
    flag_value=_RESUME_PICKER_SENTINEL,
    default=None,
    help=(
        "Retoma uma conversa anterior do OmniCraft. Com um id de conversa "
        "(ex. ``--resume conv_abc123``) anexa diretamente; sem valor "
        "abre um seletor interativo restrito a sessões codex-native."
    ),
)
@click.option(
    "--session",
    "session_id",
    metavar="SESSION_ID",
    default=None,
    hidden=True,
    help="Alias descontinuado para ``--resume <id>``; mantido por uma release.",
)
@click.option("--model", default=None, help="Modelo Codex para usar na thread nativa.")
@click.option(
    "-p",
    "--prompt",
    default=None,
    help="Envia isto como a primeira mensagem depois que a TUI do Codex inicia.",
)
@click.argument("codex_args", nargs=-1, type=click.UNPROCESSED)
def codex(
    server: str | None,
    resume: str | None,
    session_id: str | None,
    model: str | None,
    prompt: str | None,
    codex_args: tuple[str, ...],
) -> None:
    # Param docs live in comments — Click uses the docstring for --help.
    # :param server: Remote OmniCraft server URL, or None for local.
    # :param resume: None, picker sentinel, or a conversation id.
    # :param session_id: Legacy ``--session`` id; mutually exclusive with ``--resume``.
    # :param model: Codex model id.
    # :param prompt: Optional first prompt.
    # :param codex_args: Pass-through args for ``codex`` before ``resume``.
    """Lança a TUI do Codex em um terminal OmniCraft.

    \b
    Exemplos:
      omnicraft codex
      omnicraft codex --resume conv_abc123
      omnicraft codex --resume                  # seletor interativo
      omnicraft codex --server https://<app>.databricksapps.com
    """
    _reject_native_on_windows("codex")
    choice = _split_resume_value(resume)
    if session_id is not None and (choice.picker or choice.conversation_id is not None):
        raise click.UsageError(
            "--session e --resume são mutuamente exclusivos; "
            "prefira --resume (--session está descontinuado).",
        )

    from omnicraft.codex_native import run_codex_native

    cfg = _load_effective_config()
    if server is None:
        server = cfg.get("server")
    if model is None:
        model = cfg.get("model")
    auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)

    # Validate option combinations before any side effects — see
    # the same comment in the claude command. _ensure_backend can
    # spawn the daemon and take the full local-server-discover
    # timeout to fail, which would make a bad arg pair look like
    # a backend outage instead of a usage error.
    choice = _split_resume_value(resume)
    if session_id is not None and (choice.picker or choice.conversation_id is not None):
        raise click.UsageError(
            "--session e --resume são mutuamente exclusivos; "
            "prefira --resume (--session está descontinuado).",
        )

    # Ensure the host daemon (local when ``--server`` is omitted/empty,
    # remote otherwise) and resolve the concrete OmniCraft server URL. Codex follows
    # the same ownership model as attach/run/claude: the daemon-spawned runner
    # owns the app-server and TUI; the CLI attaches to the tmux terminal.
    server = _ensure_backend(server)

    resolved_session_id = (
        choice.conversation_id if choice.conversation_id is not None else session_id
    )

    run_codex_native(
        server=server,
        session_id=resolved_session_id,
        resume_picker=choice.picker,
        codex_args=codex_args,
        model=model,
        prompt=prompt,
        auto_open_conversation=auto_open_conversation,
    )


@cli.command(
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    }
)
@click.option(
    "--server",
    default=None,
    help=(
        "URL omnicraft remota. Garante o daemon host, pede ao "
        "runner criado pelo daemon para lançar OpenCode, e anexa este TTY. "
        'Passe --server "" para auto-criar um servidor local persistente em '
        "segundo plano e usar esse em vez de um remoto."
    ),
)
@click.option(
    "-r",
    "--resume",
    "resume",
    is_flag=False,
    flag_value=_RESUME_PICKER_SENTINEL,
    default=None,
    help=(
        "Retoma uma conversa anterior do OmniCraft. Com um id de conversa "
        "(ex. ``--resume conv_abc123``) anexa diretamente; sem valor "
        "abre um seletor interativo restrito a sessões opencode-native."
    ),
)
@click.option(
    "--session",
    "session_id",
    metavar="SESSION_ID",
    default=None,
    hidden=True,
    help="Alias descontinuado para ``--resume <id>``; mantido por uma release.",
)
@click.option("--model", default=None, help="Modelo OpenCode para usar na sessão nativa.")
@click.argument("opencode_args", nargs=-1, type=click.UNPROCESSED)
def opencode(
    server: str | None,
    resume: str | None,
    session_id: str | None,
    model: str | None,
    opencode_args: tuple[str, ...],
) -> None:
    # :param server: Remote OmniCraft server URL, or None for local.
    # :param resume: None, picker sentinel, or a conversation id.
    # :param session_id: Legacy ``--session`` id; mutually exclusive with ``--resume``.
    # :param model: OpenCode model id pinned on the wrapper spec.
    # :param opencode_args: Pass-through args persisted for the ``opencode attach`` TUI.
    """Lança a TUI do OpenCode em um terminal OmniCraft.

    \b
    Exemplos:
      omnicraft opencode
      omnicraft opencode --resume conv_abc123
      omnicraft opencode --resume                  # seletor interativo
      omnicraft opencode --server https://<app>.databricksapps.com
    """
    from omnicraft.opencode_native import run_opencode_native

    cfg = _load_effective_config()
    if server is None:
        server = cfg.get("server")
    if model is None:
        # Prefer the OpenCode-specific default (set in `omni setup` → OpenCode →
        # "Set default model"); fall back to the shared `model` key for back-compat.
        model = cfg.get("opencode_model") or cfg.get("model")
    auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)

    # Validate option combinations before any side effects (see the codex
    # command): _ensure_backend can spawn the daemon and take the full
    # local-server-discover timeout, which would mask a bad arg pair as an
    # outage instead of a usage error.
    choice = _split_resume_value(resume)
    if session_id is not None and (choice.picker or choice.conversation_id is not None):
        raise click.UsageError(
            "--session e --resume são mutuamente exclusivos; "
            "prefira --resume (--session está descontinuado).",
        )

    # Ensure the host daemon (local when ``--server`` is omitted/empty, remote
    # otherwise); the daemon-spawned runner owns ``opencode serve`` + the TUI,
    # and this CLI attaches to the tmux terminal.
    server = _ensure_backend(server)
    resolved_session_id = (
        choice.conversation_id if choice.conversation_id is not None else session_id
    )
    run_opencode_native(
        server=server,
        session_id=resolved_session_id,
        resume_picker=choice.picker,
        opencode_args=opencode_args,
        model=model,
        auto_open_conversation=auto_open_conversation,
    )


@cli.command(
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    }
)
@click.option(
    "--server",
    default=None,
    help=(
        "URL omnicraft remota. Garante o daemon host, pede ao "
        "runner criado pelo daemon para lançar Pi, e anexa este TTY. "
        'Passe --server "" para auto-criar um servidor local persistente em '
        "segundo plano e usar esse em vez de um remoto."
    ),
)
@click.option(
    "-r",
    "--resume",
    "resume",
    is_flag=False,
    flag_value=_RESUME_PICKER_SENTINEL,
    default=None,
    help=(
        "Retoma uma conversa anterior do OmniCraft. Com um id de conversa "
        "(ex. ``--resume conv_abc123``) anexa diretamente; sem valor "
        "abre um seletor interativo restrito a sessões pi-native."
    ),
)
@click.option(
    "--session",
    "session_id",
    metavar="SESSION_ID",
    default=None,
    hidden=True,
    help="Alias descontinuado para ``--resume <id>``; mantido por uma release.",
)
@click.argument("pi_args", nargs=-1, type=click.UNPROCESSED)
def pi(
    server: str | None,
    resume: str | None,
    session_id: str | None,
    pi_args: tuple[str, ...],
) -> None:
    """Lança a TUI do Pi em um terminal OmniCraft.

    \b
    Exemplos:
      omnicraft pi
      omnicraft pi --resume conv_abc123
      omnicraft pi --resume                    # seletor interativo
      omnicraft pi --model local-deepseek/deepseek-v4-flash
    """
    choice = _split_resume_value(resume)
    if session_id is not None and (choice.picker or choice.conversation_id is not None):
        raise click.UsageError(
            "--session e --resume são mutuamente exclusivos; "
            "prefira --resume (--session está descontinuado).",
        )

    from omnicraft.pi_native import run_pi_native

    cfg = _load_effective_config()
    if server is None:
        server = cfg.get("server")
    auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)

    server = _ensure_backend(server)
    resolved_session_id = (
        choice.conversation_id if choice.conversation_id is not None else session_id
    )

    run_pi_native(
        server=server,
        session_id=resolved_session_id,
        resume_picker=choice.picker,
        pi_args=pi_args,
        auto_open_conversation=auto_open_conversation,
    )


def _bundled_agent_brain_harness(name: str) -> str | None:
    """Return the canonical brain harness of a bundled agent, or ``None``.

    Reads the brain harness (``executor.config.harness``, falling back to
    ``executor.harness`` / ``executor.type``) from the bundled agent's
    ``config.yaml`` — e.g. fucho's and lilo's ``claude-sdk`` brain — so
    credential fallback can target the model family the brain actually
    runs on. Mirrors :func:`_peek_default_agent_harness`'s YAML-reading
    style.

    :param name: Bundled example directory name, e.g. ``"fucho"``.
    :returns: The canonical harness id, e.g. ``"claude-sdk"``, or ``None``
        when the bundle is missing/unreadable or declares no brain harness.
    """
    config_path = Path(_bundled_example_path(name)) / "config.yaml"
    if not config_path.is_file():
        return None
    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    executor = raw.get("executor")
    if not isinstance(executor, dict):
        return None
    declared: object = None
    config_block = executor.get("config")
    if isinstance(config_block, dict):
        declared = config_block.get("harness")
    if not isinstance(declared, str) or not declared:
        declared = executor.get("harness") or executor.get("type")
    if not isinstance(declared, str) or not declared:
        return None
    return canonicalize_harness(declared) or declared


def _ensure_bundled_agent_brain_credential(name: str) -> None:
    """Ensure the bundled agent's brain harness has a credential to launch with.

    Fucho and Lilo launch with the *first available* credential for their
    brain's model family rather than requiring a specific one to be marked
    ``default: true`` up front — so users can start without manually
    picking/configuring one. When no default provider is configured for the
    agent's brain harness, pick the first available credential serving that
    family and mark it the default so the downstream ``run`` resolves it —
    printing a notice (to stderr) since this mutates the user's config on a
    launch command, mirroring the confirmation ``setup`` / ``/model`` show.

    No-op when a default is already configured, or when no credential is
    available for the family (the harness raises its own launch error then).
    Only an explicit default (or none) is touched — an existing default is
    never overridden. Marking the first available credential the default
    mirrors :func:`_add_provider_entry`'s "a first provider just works"
    adoption (see :func:`omnicraft.setup`).

    :param name: Bundled example directory name, e.g. ``"fucho"``.
    """
    from omnicraft.errors import OmniCraftError
    from omnicraft.onboarding.configure_models import family_label
    from omnicraft.onboarding.detected import effective_config_with_detected
    from omnicraft.onboarding.provider_config import (
        default_provider_for_harness,
        harness_family,
        load_config,
        load_providers,
        provider_families,
        set_default_provider,
    )

    brain_harness = _bundled_agent_brain_harness(name)
    if brain_harness is None:
        return
    family = harness_family(brain_harness)
    if family is None:
        return
    # Best-effort: adopting a default must never crash a launch. Any malformed
    # or unexpected config state (corrupt YAML, ambiguous defaults, a divergent
    # on-disk entry) degrades to a no-op — the harness then raises its own
    # credential error.
    try:
        config = effective_config_with_detected(load_config())
        if default_provider_for_harness(config, brain_harness) is not None:
            return
        on_disk = _load_global_config()
        disk_block = on_disk.get("providers") if isinstance(on_disk, dict) else None
        if not isinstance(disk_block, dict):
            return
        # Skip ambient-detected entries (not on disk) — auto-defaulted upstream.
        for entry_name, entry in load_providers(config).items():
            if family not in provider_families(entry) or entry_name not in disk_block:
                continue
            _save_global_config(
                {"providers": set_default_provider(disk_block, entry_name, family)}
            )
            # Announce: this mutates the user's config on a launch command.
            click.echo(
                f"Nenhuma credencial {family_label(family)} padrão definida — "
                f"usando {_credential_label(entry_name, entry)} e salvando como "
                f"o padrão (mude quando quiser com: omnicraft /model).",
                err=True,
            )
            return
    except (OSError, yaml.YAMLError, OmniCraftError):
        return


@cli.command(
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    }
)
@click.option(
    "--server",
    default=None,
    help=(
        "URL omnicraft remota. Garante o daemon host, pede ao "
        "runner criado pelo daemon para lançar a TUI do Cursor, e anexa este TTY. "
        'Passe --server "" para auto-criar um servidor local persistente em '
        "segundo plano e usar esse em vez de um remoto."
    ),
)
@click.option(
    "-r",
    "--resume",
    "resume",
    is_flag=False,
    flag_value=_RESUME_PICKER_SENTINEL,
    default=None,
    help=(
        "Retoma uma conversa anterior do OmniCraft. Com um id de conversa "
        "(ex. ``--resume conv_abc123``) anexa diretamente; sem valor "
        "abre um seletor interativo restrito a sessões cursor-native."
    ),
)
@click.option(
    "--session",
    "session_id",
    metavar="SESSION_ID",
    default=None,
    hidden=True,
    help="Alias descontinuado para ``--resume <id>``; mantido por uma release.",
)
@click.option(
    "--mode",
    "mode",
    default=None,
    type=click.Choice(["plan", "ask"]),
    help=(
        "Inicia o cursor-agent no modo de execução dado. "
        "``plan``: somente leitura/planejamento (analisa, propõe planos, sem edições). "
        "``ask``: estilo perguntas e respostas para explicações e perguntas (somente leitura)."
    ),
)
@click.option(
    "--model",
    default=None,
    help="Modelo Cursor para usar na TUI nativa (ex. gpt-5.2, claude-4.6-sonnet-medium).",
)
@click.argument("cursor_args", nargs=-1, type=click.UNPROCESSED)
def cursor(
    server: str | None,
    resume: str | None,
    session_id: str | None,
    mode: str | None,
    model: str | None,
    cursor_args: tuple[str, ...],
) -> None:
    # Param docs live in comments — Click uses the docstring for --help.
    # :param model: Cursor model id passed to cursor-agent as ``--model``.
    """Lança a TUI do Cursor em um terminal OmniCraft.

    \b
    Exemplos:
      omnicraft cursor
      omnicraft cursor --model gpt-5.2
      omnicraft cursor --resume conv_abc123
      omnicraft cursor --resume                 # seletor interativo
      omnicraft cursor --mode plan              # inicia no modo plan (somente leitura)
      omnicraft cursor --mode ask               # inicia no modo ask (perguntas e respostas)
    """
    _reject_native_on_windows("cursor")
    choice = _split_resume_value(resume)
    if session_id is not None and (choice.picker or choice.conversation_id is not None):
        raise click.UsageError(
            "--session e --resume são mutuamente exclusivos; "
            "prefira --resume (--session está descontinuado).",
        )

    from omnicraft.cursor_native import run_cursor_native

    cfg = _load_effective_config()
    if server is None:
        server = cfg.get("server")
    # Deliberately no ``cfg.get("model")`` fallback (unlike ``codex``): the
    # global config model is a Claude/Codex catalog id, not a cursor-agent
    # model id, and pinning it would break the cursor TUI launch. Cursor's
    # model is explicit-only here; persistent selection rides the web /model.
    auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)

    server = _ensure_backend(server)
    resolved_session_id = (
        choice.conversation_id if choice.conversation_id is not None else session_id
    )

    run_cursor_native(
        server=server,
        session_id=resolved_session_id,
        resume_picker=choice.picker,
        cursor_args=cursor_args,
        model=model,
        auto_open_conversation=auto_open_conversation,
        mode=mode,
    )


@cli.command(
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    }
)
@click.option(
    "--server",
    default=None,
    help=(
        "URL omnicraft remota. Garante o daemon host, pede ao "
        "runner criado pelo daemon para lançar a TUI do Kiro, e anexa este TTY. "
        'Passe --server "" para auto-criar um servidor local persistente em '
        "segundo plano e usar esse em vez de um remoto."
    ),
)
@click.option(
    "-r",
    "--resume",
    "resume",
    is_flag=False,
    flag_value=_RESUME_PICKER_SENTINEL,
    default=None,
    help=(
        "Retoma uma conversa anterior do OmniCraft. Com um id de conversa "
        "(ex. ``--resume conv_abc123``) anexa diretamente; sem valor "
        "abre um seletor interativo restrito a sessões kiro-native."
    ),
)
@click.option(
    "--session",
    "session_id",
    metavar="SESSION_ID",
    default=None,
    hidden=True,
    help="Alias descontinuado para ``--resume <id>``; mantido por uma release.",
)
@click.option("--model", default=None, help="Modelo Kiro para usar no chat nativo.")
@click.option("--effort", default=None, help="Nível de esforço do Kiro para o chat nativo.")
@click.option("--agent", "kiro_agent", default=None, help="Agente Kiro para usar no chat nativo.")
@click.option(
    "--trust-tools",
    "trust_tools",
    multiple=True,
    metavar="TOOL",
    help="Confia em uma ferramenta específica do Kiro. Pode ser passado várias vezes.",
)
@click.option(
    "--trust-all-tools",
    is_flag=True,
    default=False,
    help="Confia explicitamente em todas as ferramentas do Kiro neste lançamento local.",
)
@click.option(
    "-p",
    "--prompt",
    default=None,
    help="Envia isto como a entrada inicial do chat do Kiro quando a TUI inicia.",
)
@click.argument("kiro_args", nargs=-1, type=click.UNPROCESSED)
def kiro(
    server: str | None,
    resume: str | None,
    session_id: str | None,
    model: str | None,
    effort: str | None,
    kiro_agent: str | None,
    trust_tools: tuple[str, ...],
    trust_all_tools: bool,
    prompt: str | None,
    kiro_args: tuple[str, ...],
) -> None:
    """Lança a TUI do Kiro em um terminal OmniCraft.

    \b
    Exemplos:
      omnicraft kiro
      omnicraft kiro --resume conv_abc123
      omnicraft kiro --resume                  # seletor interativo
      omnicraft kiro --model auto -p "review this repo"
    """
    choice = _split_resume_value(resume)
    if session_id is not None and (choice.picker or choice.conversation_id is not None):
        raise click.UsageError(
            "--session e --resume são mutuamente exclusivos; "
            "prefira --resume (--session está descontinuado).",
        )
    _reject_reserved_kiro_resume_args(kiro_args)

    from omnicraft.kiro_native import run_kiro_native

    cfg = _load_effective_config()
    if server is None:
        server = cfg.get("server")
    if model is None:
        model = cfg.get("model")
    auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)
    launch_args = _build_kiro_launch_args(
        effort=effort,
        kiro_agent=kiro_agent,
        trust_tools=trust_tools,
        trust_all_tools=trust_all_tools,
        passthrough_args=kiro_args,
    )

    server = _ensure_backend(server)
    resolved_session_id = (
        choice.conversation_id if choice.conversation_id is not None else session_id
    )

    run_kiro_native(
        server=server,
        session_id=resolved_session_id,
        resume_picker=choice.picker,
        kiro_args=launch_args,
        model=model,
        prompt=prompt,
        auto_open_conversation=auto_open_conversation,
    )


def _reject_reserved_kiro_resume_args(kiro_args: tuple[str, ...]) -> None:
    """Reject Kiro-owned resume flags in passthrough args."""
    reserved = {"--resume", "--resume-id", "--resume-picker"}
    if any(arg == flag or arg.startswith(f"{flag}=") for arg in kiro_args for flag in reserved):
        raise click.UsageError(
            "As flags de resume do Kiro são reservadas para o tratamento de resume do "
            "OmniCraft; use `omnicraft kiro --resume [CONVERSATION]` em vez disso."
        )


def _build_kiro_launch_args(
    *,
    effort: str | None,
    kiro_agent: str | None,
    trust_tools: tuple[str, ...],
    trust_all_tools: bool,
    passthrough_args: tuple[str, ...],
) -> tuple[str, ...]:
    """Build mapped Kiro CLI args for the runner-owned terminal launch."""
    args: list[str] = []
    if effort:
        args.extend(["--effort", effort])
    if kiro_agent:
        args.extend(["--agent", kiro_agent])
    for tool in trust_tools:
        args.extend(["--trust-tools", tool])
    if trust_all_tools:
        args.append("--trust-all-tools")
    args.extend(passthrough_args)
    return tuple(args)


@cli.command(
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    }
)
@click.option(
    "--server",
    default=None,
    help=(
        "URL omnicraft remota. Garante o daemon host, pede ao "
        "runner criado pelo daemon para lançar a TUI do Goose, e anexa este TTY. "
        'Passe --server "" para auto-criar um servidor local persistente em '
        "segundo plano e usar esse em vez de um remoto."
    ),
)
@click.option(
    "-r",
    "--resume",
    "resume",
    is_flag=False,
    flag_value=_RESUME_PICKER_SENTINEL,
    default=None,
    help=(
        "Retoma uma conversa anterior do OmniCraft. Com um id de conversa "
        "(ex. ``--resume conv_abc123``) anexa diretamente; sem valor "
        "abre um seletor interativo restrito a sessões goose-native."
    ),
)
@click.option(
    "--session",
    "session_id",
    metavar="SESSION_ID",
    default=None,
    hidden=True,
    help="Alias descontinuado para ``--resume <id>``; mantido por uma release.",
)
@click.argument("goose_args", nargs=-1, type=click.UNPROCESSED)
def goose(
    server: str | None,
    resume: str | None,
    session_id: str | None,
    goose_args: tuple[str, ...],
) -> None:
    """Lança a TUI do Goose em um terminal OmniCraft.

    \b
    Exemplos:
      omnicraft goose
      omnicraft goose --resume conv_abc123
      omnicraft goose --resume                 # seletor interativo
    """
    choice = _split_resume_value(resume)
    if session_id is not None and (choice.picker or choice.conversation_id is not None):
        raise click.UsageError(
            "--session e --resume são mutuamente exclusivos; "
            "prefira --resume (--session está descontinuado).",
        )

    from omnicraft.goose_native import run_goose_native

    cfg = _load_effective_config()
    if server is None:
        server = cfg.get("server")
    auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)

    server = _ensure_backend(server)
    resolved_session_id = (
        choice.conversation_id if choice.conversation_id is not None else session_id
    )

    run_goose_native(
        server=server,
        session_id=resolved_session_id,
        resume_picker=choice.picker,
        goose_args=goose_args,
        auto_open_conversation=auto_open_conversation,
    )


@cli.command(
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    }
)
@click.option(
    "--server",
    default=None,
    help=(
        "URL omnicraft remota. Garante o daemon host, pede ao "
        "runner criado pelo daemon para lançar a TUI do Hermes, e anexa este TTY. "
        'Passe --server "" para auto-criar um servidor local persistente em '
        "segundo plano e usar esse em vez de um remoto."
    ),
)
@click.option(
    "-r",
    "--resume",
    "resume",
    is_flag=False,
    flag_value=_RESUME_PICKER_SENTINEL,
    default=None,
    help=(
        "Retoma uma conversa anterior do OmniCraft. Com um id de conversa "
        "(ex. ``--resume conv_abc123``) anexa diretamente; sem valor "
        "abre um seletor interativo restrito a sessões hermes-native."
    ),
)
@click.option(
    "--session",
    "session_id",
    metavar="SESSION_ID",
    default=None,
    hidden=True,
    help="Alias descontinuado para ``--resume <id>``; mantido por uma release.",
)
@click.argument("hermes_args", nargs=-1, type=click.UNPROCESSED)
def hermes(
    server: str | None,
    resume: str | None,
    session_id: str | None,
    hermes_args: tuple[str, ...],
) -> None:
    """Lança a TUI do Hermes em um terminal OmniCraft.

    \b
    Exemplos:
      omnicraft hermes
      omnicraft hermes --resume conv_abc123
      omnicraft hermes --resume                 # seletor interativo
    """
    choice = _split_resume_value(resume)
    if session_id is not None and (choice.picker or choice.conversation_id is not None):
        raise click.UsageError(
            "--session e --resume são mutuamente exclusivos; "
            "prefira --resume (--session está descontinuado).",
        )

    from omnicraft.hermes_native import run_hermes_native

    cfg = _load_effective_config()
    if server is None:
        server = cfg.get("server")
    auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)

    server = _ensure_backend(server)
    resolved_session_id = (
        choice.conversation_id if choice.conversation_id is not None else session_id
    )

    run_hermes_native(
        server=server,
        session_id=resolved_session_id,
        resume_picker=choice.picker,
        hermes_args=hermes_args,
        auto_open_conversation=auto_open_conversation,
    )


@cli.command(
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    }
)
@click.option(
    "--server",
    default=None,
    help=(
        "URL omnicraft remota. Garante o daemon host, vincula um runner, "
        "lança o Antigravity (agy) em um recurso de terminal e anexa "
        'este TTY. Passe --server "" para auto-criar um servidor local '
        "persistente em segundo plano e usar esse em vez de um remoto."
    ),
)
@click.option(
    "-r",
    "--resume",
    "resume",
    is_flag=False,
    flag_value=_RESUME_PICKER_SENTINEL,
    default=None,
    help=(
        "Retoma uma conversa anterior do OmniCraft. Com um id de conversa "
        "(ex. ``--resume conv_abc123``) anexa diretamente; sem valor "
        "abre um seletor interativo restrito a sessões antigravity-native."
    ),
)
@click.option(
    "--session",
    "session_id",
    metavar="SESSION_ID",
    default=None,
    hidden=True,
    help="Alias descontinuado para ``--resume <id>``; mantido por uma release.",
)
@click.option("--model", default=None, help="Modelo Antigravity (agy) para usar na sessão.")
@click.argument("antigravity_args", nargs=-1, type=click.UNPROCESSED)
def antigravity(
    server: str | None,
    resume: str | None,
    session_id: str | None,
    model: str | None,
    antigravity_args: tuple[str, ...],
) -> None:
    """Lança a TUI do Antigravity (agy) em um terminal OmniCraft.

    \b
    Exemplos:
      omnicraft antigravity
      omnicraft antigravity --resume conv_abc123
      omnicraft antigravity --resume                  # seletor interativo
      omnicraft antigravity --server https://<app>.databricksapps.com
    """
    # Validate option combinations BEFORE any side effects (daemon spawn,
    # server discovery) -- see the same comment in the claude command.
    choice = _split_resume_value(resume)
    if session_id is not None and (choice.picker or choice.conversation_id is not None):
        raise click.UsageError(
            "--session e --resume são mutuamente exclusivos; "
            "prefira --resume (--session está descontinuado).",
        )

    from omnicraft.antigravity_native import run_antigravity_native

    cfg = _load_effective_config()
    if server is None:
        server = cfg.get("server")
    if model is None:
        model = cfg.get("model")
    auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)

    server = _ensure_backend(server)
    resolved_session_id = (
        choice.conversation_id if choice.conversation_id is not None else session_id
    )

    # permission_mode is left None here (parity with the claude/codex/pi CLI
    # launchers): the attended terminal launch lets agy's own request-review
    # prompt govern each tool, and an unattended/headless launch auto-bypasses
    # inside run_antigravity_native. It is plumbed through build_agy_launch so a
    # future caller CAN set it, but this human CLI path exposes no permission
    # flag and never needs one.
    run_antigravity_native(
        server=server,
        session_id=resolved_session_id,
        resume_picker=choice.picker,
        antigravity_args=antigravity_args,
        model=model,
        auto_open_conversation=auto_open_conversation,
    )


@cli.command(
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    }
)
@click.option(
    "--server",
    default=None,
    help=(
        "URL omnicraft remota. Garante o daemon host, pede ao "
        "runner criado pelo daemon para lançar a TUI do qwen, e anexa este TTY. "
        'Passe --server "" para auto-criar um servidor local persistente em '
        "segundo plano e usar esse em vez de um remoto."
    ),
)
@click.option(
    "-r",
    "--resume",
    "resume",
    is_flag=False,
    flag_value=_RESUME_PICKER_SENTINEL,
    default=None,
    help=(
        "Retoma uma conversa anterior do OmniCraft. Com um id de conversa "
        "(ex. ``--resume conv_abc123``) anexa diretamente; sem valor "
        "abre um seletor interativo restrito a sessões qwen-native."
    ),
)
@click.option(
    "--session",
    "session_id",
    metavar="SESSION_ID",
    default=None,
    hidden=True,
    help="Alias descontinuado para ``--resume <id>``; mantido por uma release.",
)
@click.argument("qwen_args", nargs=-1, type=click.UNPROCESSED)
def qwen(
    server: str | None,
    resume: str | None,
    session_id: str | None,
    qwen_args: tuple[str, ...],
) -> None:
    """Lança a TUI do qwen (Qwen Code) em um terminal OmniCraft.

    \b
    Exemplos:
      omnicraft qwen
      omnicraft qwen --resume conv_abc123
      omnicraft qwen --resume                  # seletor interativo
    """
    choice = _split_resume_value(resume)
    if session_id is not None and (choice.picker or choice.conversation_id is not None):
        raise click.UsageError(
            "--session e --resume são mutuamente exclusivos; "
            "prefira --resume (--session está descontinuado).",
        )

    from omnicraft.qwen_native import run_qwen_native

    cfg = _load_effective_config()
    if server is None:
        server = cfg.get("server")
    auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)

    server = _ensure_backend(server)
    resolved_session_id = (
        choice.conversation_id if choice.conversation_id is not None else session_id
    )

    run_qwen_native(
        server=server,
        session_id=resolved_session_id,
        resume_picker=choice.picker,
        qwen_args=qwen_args,
        auto_open_conversation=auto_open_conversation,
    )


def _run_bundled_agent(name: str, run_args: tuple[str, ...]) -> None:
    """Forward a bundled-agent subcommand to ``run`` on its packaged path.

    Implements ``omnicraft fucho`` / ``omnicraft lilo``: resolves the bundled
    example directory and re-dispatches through the ``run`` command's own
    parser, so every ``run`` flag (``--server``, ``-p``, ``--resume``, ...)
    works unchanged on the agent shorthands without duplicating ``run``'s
    option declarations.

    ``prog_name`` is pinned to ``"omnicraft run"`` so context-derived output —
    usage errors and the :func:`_build_resume_parts` replay prefix — renders
    as the canonical ``omnicraft run <path>`` form, which stays valid when
    replayed.

    :param name: Bundled example directory name, e.g. ``"fucho"``.
    :param run_args: Unparsed pass-through CLI args for ``run``,
        e.g. ``("-p", "review the last commit")``.
    """
    # Fucho/Lilo launch with the first available credential for their
    # brain's family when no specific one is configured up front (#334).
    _ensure_bundled_agent_brain_credential(name)
    # standalone_mode=False propagates ClickExceptions to main()'s handler
    # (CLI diagnostics logging + setup hint) instead of exiting inline,
    # matching the outer `cli(args=argv, standalone_mode=False)` dispatch.
    run.main(
        args=[_bundled_example_path(name), *run_args],
        prog_name="omnicraft run",
        standalone_mode=False,
    )


@cli.command(
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    }
)
@click.argument("run_args", nargs=-1, type=click.UNPROCESSED)
def fucho(run_args: tuple[str, ...]) -> None:
    # Param docs live in comments — Click uses the docstring for --help.
    # :param run_args: Pass-through args for ``run``.
    """Lança o fucho, o orquestrador de código multiagente incluído.

    Atalho para ``omnicraft run`` no agente fucho empacotado — o mesmo
    agente que um ``omnicraft`` puro lança quando uma credencial Claude está
    configurada. Todas as opções de ``run`` são aceitas e repassadas.

    \b
    Exemplos:
      omnicraft fucho
      omnicraft fucho -p "revise o último commit"
      omnicraft fucho --server https://<app>.databricksapps.com
    """
    _run_bundled_agent("fucho", run_args)


@cli.command(
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    }
)
@click.argument("run_args", nargs=-1, type=click.UNPROCESSED)
def lilo(run_args: tuple[str, ...]) -> None:
    # Param docs live in comments — Click uses the docstring for --help.
    # :param run_args: Pass-through args for ``run``.
    """Lança o lilo, o agente de brainstorming de duas cabeças incluído.

    Atalho para ``omnicraft run`` no agente lilo empacotado. O Lilo distribui
    cada pergunta tanto para um sub-agente Claude quanto para um GPT, então um
    provedor Claude e um OpenAI devem ambos estar configurados. Todas as opções
    de ``run`` são aceitas e repassadas.

    \b
    Exemplos:
      omnicraft lilo
      omnicraft lilo -p "ideias de nome para um CLI que roda agentes"
    """
    _run_bundled_agent("lilo", run_args)


@cli.command(
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    }
)
@click.option(
    "--server",
    default=None,
    help=(
        "URL omnicraft remota. Garante o daemon host, pede ao "
        "runner criado pelo daemon para lançar a TUI do Kimi, e anexa este TTY. "
        'Passe --server "" para auto-criar um servidor local persistente em '
        "segundo plano e usar esse em vez de um remoto."
    ),
)
@click.option(
    "-r",
    "--resume",
    "resume",
    is_flag=False,
    flag_value=_RESUME_PICKER_SENTINEL,
    default=None,
    help=(
        "Retoma uma conversa anterior do OmniCraft. Com um id de conversa "
        "(ex. ``--resume conv_abc123``) anexa diretamente; sem valor "
        "abre um seletor interativo restrito a sessões kimi-native."
    ),
)
@click.option(
    "--session",
    "session_id",
    metavar="SESSION_ID",
    default=None,
    hidden=True,
    help="Alias descontinuado para ``--resume <id>``; mantido por uma release.",
)
@click.argument("kimi_args", nargs=-1, type=click.UNPROCESSED)
def kimi(
    server: str | None,
    resume: str | None,
    session_id: str | None,
    kimi_args: tuple[str, ...],
) -> None:
    """Lança a TUI do Kimi Code em um terminal OmniCraft.

    Inicia a TUI interativa ``kimi`` da Moonshot AI
    (https://github.com/MoonshotAI/Kimi-Code) em um terminal de propriedade do
    runner e anexa seu TTY — a experiência nativa, embutida no web UI do
    OmniCraft. Nenhuma config de provedor do OmniCraft é necessária: o kimi
    autentica contra seu próprio backend (``kimi login`` para OAuth, ou uma
    chave de API da Moonshot).

    Para o harness SDK headless (``kimi -p`` por turno atrás do REPL do
    OmniCraft) use ``omnicraft run --harness kimi`` em vez disso.

    \b
    Exemplos:
      omnicraft kimi
      omnicraft kimi --resume conv_abc123
      omnicraft kimi --resume                   # seletor interativo
    """
    choice = _split_resume_value(resume)
    if session_id is not None and (choice.picker or choice.conversation_id is not None):
        raise click.UsageError(
            "--session e --resume são mutuamente exclusivos; "
            "prefira --resume (--session está descontinuado).",
        )

    from omnicraft.kimi_native import run_kimi_native

    cfg = _load_effective_config()
    if server is None:
        server = cfg.get("server")
    auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)

    server = _ensure_backend(server)
    resolved_session_id = (
        choice.conversation_id if choice.conversation_id is not None else session_id
    )

    run_kimi_native(
        server=server,
        session_id=resolved_session_id,
        resume_picker=choice.picker,
        kimi_args=kimi_args,
        auto_open_conversation=auto_open_conversation,
    )


@cli.command()
@click.argument("target", required=False, metavar="[CONV_ID]")
@click.option(
    "--server",
    default=None,
    help=(
        "URL omnicraft remota. Quando definida, o seletor / a busca consulta "
        "este servidor em vez de iniciar um local. Obrigatório ao "
        "rodar ``omnicraft resume`` sem um id de conversa."
    ),
)
def resume(
    target: str | None,
    server: str | None,
) -> None:
    # Click uses the docstring as --help text — keep param docs in
    # comments so they don't leak into CLI output.
    #
    # :param target: Optional OmniCraft conversation id, e.g.
    #     ``"conv_abc123"``. None falls through to the picker.
    # :param server: Remote OmniCraft server URL (optional in id mode;
    #     required in picker mode).
    """Retoma uma conversa do OmniCraft, despachando automaticamente pelo runtime.

    \b
    Com CONV_ID: procura a conversa e despacha para o
    wrapper correspondente. Sessões claude-native vão para
    ``omnicraft claude``; todo o resto exibe uma dica clara para
    usar ``omnicraft run --resume <id> <agent.yaml>``.

    \b
    Sem CONV_ID: abre um seletor entre agentes sobre suas conversas
    anteriores (requer ``--server``). O despacho segue da
    linha que você selecionar.

    \b
    Exemplos:
      omnicraft resume conv_abc123
      omnicraft resume conv_abc123 --server https://<app>.databricksapps.com
      omnicraft resume --server https://<app>.databricksapps.com
    """
    from omnicraft.resume_dispatch import run_resume

    run_resume(
        target=target,
        server=_resolve_server_url(server) if server else server,
    )


@cli.group("session", invoke_without_command=True)
@click.pass_context
def session(ctx: click.Context) -> None:
    """Gerencia sessões do OmniCraft.

    \b
    Exemplos:
      omnicraft session export --id conv_abc123
      omnicraft session export --id conv_abc123 --output transcript.jsonl
      omnicraft session export --id conv_abc123 --server https://myserver.com
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@session.command("export")
@click.option(
    "--id",
    "session_id",
    required=True,
    metavar="SESSION_ID",
    help="ID da sessão para exportar, ex. conv_abc123.",
)
@click.option(
    "--output",
    "-o",
    "output",
    default=None,
    metavar="FILE",
    help="Caminho do arquivo de saída.  Padrão: <SESSION_ID>.jsonl no diretório atual.",
)
@click.option(
    "--server",
    default=None,
    help=(
        "URL do servidor OmniCraft. "
        "Padrão: o servidor configurado, ou um servidor local já em execução."
    ),
)
def session_export(session_id: str, output: str | None, server: str | None) -> None:
    """Exporta a transcrição de uma sessão para um arquivo JSONL portável.

    Cada linha da saída é um objeto JSON.  A primeira linha carrega
    os metadados da sessão (``"record_type": "session_meta"``); toda
    linha subsequente é um item de conversa
    (``"record_type": "item"``).  O arquivo preserva a ordem completa dos
    turnos e pode ser reimportado com um futuro ``omnicraft session import``.

    \b
    Exemplos:
      omnicraft session export --id conv_abc123
      omnicraft session export --id conv_abc123 --output my_session.jsonl
      omnicraft session export --id conv_abc123 --server https://myserver.com
    """
    import httpx

    from omnicraft.chat import _remote_headers

    cfg = _load_effective_config()
    base_url = _resolve_attach_server(server, cfg.get("server"))
    if base_url is None:
        startup = ensure_local_omnicraft_server()
        base_url = startup.url

    base_url = base_url.rstrip("/")
    out_path = Path(output) if output else Path(f"{session_id}.jsonl")

    with httpx.Client(
        base_url=base_url, headers=_remote_headers(server_url=base_url), timeout=30.0
    ) as client:
        # Fetch session metadata (items fetched separately via pagination).
        resp = client.get(
            f"/v1/sessions/{session_id}",
            params={"include_items": "false", "include_liveness": "false"},
        )
        if resp.status_code == 404:
            raise click.ClickException(f"Sessão {session_id!r} não encontrada.")
        resp.raise_for_status()
        session_data = resp.json()

        n_items = 0
        with out_path.open("w", encoding="utf-8") as fh:
            # First line: session metadata.
            meta_record = {"record_type": "session_meta", **session_data}
            fh.write(json.dumps(meta_record) + "\n")

            # Remaining lines: items in ascending order, paginated.
            after: str | None = None
            while True:
                params: dict[str, str | int] = {"limit": 500, "order": "asc"}
                if after:
                    params["after"] = after
                items_resp = client.get(f"/v1/sessions/{session_id}/items", params=params)
                items_resp.raise_for_status()
                page = items_resp.json()
                for item in page["data"]:
                    item_record = {"record_type": "item", **item}
                    fh.write(json.dumps(item_record) + "\n")
                    n_items += 1
                if not page.get("has_more"):
                    break
                after = page.get("last_id")

    click.echo(f"Exportado(s) {n_items} item(ns) de {session_id} para {out_path}")


# Shared option help for ``run`` and the harness commands. These are the same
# flags the legacy argparse CLI exposed — keeping them on the unified
# click CLI so users don't regress when a YAML declares no executor
# block (e.g. ``examples/hello_world.yaml``) or when they want to
# choose model/harness without editing the agent file. See
# ``omnicraft.chat.run_chat`` for how local-agent options get baked
# into a materialized copy of the spec before the server starts.
_HARNESS_CHOICES_HELP = (
    "'claude' (alias for 'claude-sdk'), 'claude-sdk', 'codex', "
    "'cursor', 'kimi', "
    "'openai-agents', 'open-responses', 'pi', 'antigravity', 'qwen', 'goose', or 'copilot'"
)
_HARNESS_HELP = f"Harness para usar em um agente local: {_HARNESS_CHOICES_HELP}."
_RUN_HARNESS_HELP = (
    f"Harness para usar: {_HARNESS_CHOICES_HELP}. Sem AGENT, lança esse harness diretamente."
)
_MODEL_HELP = "Modelo para usar no agente."
_PROMPT_HELP = "Envia isto como a primeira mensagem quando o REPL inicia."
_SYSTEM_PROMPT_HELP = "Instruções para usar no agente."
_RESUME_HELP = (
    "Retoma uma conversa anterior. Sem valor, abre um seletor "
    "interativo; com um id de conversa (ex. --resume conv_abc123), anexa "
    "diretamente a essa conversa."
)
_CONTINUE_HELP = "Continua a conversa mais recente deste agente."
_NO_SESSION_HELP = "Usa um store de sessão local temporário novo para esta execução."

_FORK_HELP = "Bifurca uma sessão existente por id e abre o REPL na bifurcação."
_LOG_HELP = "Escreve um dump JSON da conversa em ~/.omnicraft/logs/ ao sair."


_DEFAULT_HARNESS_PROMPTS = {
    "claude-sdk": (
        "You are Claude Code, running through OmniCraft. "
        "Help the user with software engineering tasks."
    ),
    "codex": (
        "You are Codex, running through OmniCraft. Help the user with software engineering tasks."
    ),
    "cursor": (
        "You are Cursor, running through OmniCraft. Help the user with software engineering tasks."
    ),
    "kimi": (
        "You are Kimi Code, running through OmniCraft. "
        "Help the user with software engineering tasks."
    ),
    "qwen": (
        "You are Qwen Code, running through OmniCraft. "
        "Help the user with software engineering tasks."
    ),
    "goose": (
        "You are Goose, running through OmniCraft. Help the user with software engineering tasks."
    ),
}
_DEFAULT_HARNESS_PROMPT = "You are a helpful coding agent running through OmniCraft."

# Harnesses whose auto-generated launcher YAML should include an
# ``os_env`` block.  This triggers the workflow's ``ToolManager``
# to inject ``sys_os_*`` tools into the request so file/shell
# operations route through the OmniCraft dispatch path (runner
# visibility, timeouts, error recovery) instead of the harness's
# internal built-in tools.
_OS_ENV_HARNESSES: frozenset[str] = frozenset(
    {"claude-sdk", "codex", "pi", "qwen", "goose", "kimi"}
)


def _validate_harness(harness: str) -> None:
    """
    Fail fast when *harness* is not a supported OmniCraft harness.

    :param harness: Harness id from ``--harness``, e.g.
        ``"claude-sdk"``.
    :raises click.ClickException: If *harness* is unsupported.
    """
    from omnicraft.spec._omnicraft_compat import OMNICRAFT_HARNESSES

    if canonicalize_harness(harness) in OMNICRAFT_HARNESSES:
        return
    allowed = ", ".join(sorted(OMNICRAFT_HARNESSES))
    raise click.ClickException(f"Harness {harness!r} não suportado. Esperado um de: {allowed}.")


def _default_harness_prompt(harness: str) -> str:
    """
    Return the lightweight generated-agent instructions for *harness*.

    :param harness: Supported harness id.
    :returns: Prompt text for the generated OmniCraft YAML.
    """
    return _DEFAULT_HARNESS_PROMPTS.get(harness, _DEFAULT_HARNESS_PROMPT)


def _materialize_harness_launcher_file(
    *,
    harness: str,
    model: str | None,
    system_prompt: str | None,
) -> Path:
    """
    Create a temporary standalone OmniCraft YAML for no-AGENT ``run``.

    The generated file uses the single-file OmniCraft YAML shape
    (``name`` / ``prompt`` / ``executor``), not native AP
    ``config.yaml``. Passing this file to ``run_chat`` exercises the
    same compat adapter as ``omnicraft run examples/foo.yaml``.

    Harnesses listed in :data:`_OS_ENV_HARNESSES` get an ``os_env``
    block so the workflow injects ``sys_os_*`` tools into the
    request — routing file/shell operations through the OmniCraft
    dispatch path rather than the harness's internal built-ins.

    :param harness: Supported harness id to launch, e.g.
        ``"claude-sdk"``.
    :param model: Optional model value to bake into ``executor``.
    :param system_prompt: Optional instructions text to use as the
        YAML's top-level ``prompt``.
    :returns: Path to the generated ``*.yaml`` file.
    :raises click.ClickException: If *harness* is unsupported.
    """
    _validate_harness(harness)
    canonical = canonicalize_harness(harness) or harness
    # An acp:<slug> harness id carries a colon: it canonicalizes to the base
    # `acp` harness, but the slug selects a user-configured ACP agent resolved
    # at spawn and must be preserved. So the effective harness id written to
    # executor.harness is the FULL acp:<slug> (keep the slug), or the canonical
    # id for every other harness (so aliases still resolve, e.g. kimi ->
    # kimi-code). The agent NAME and temp filename must be path-safe /
    # [a-zA-Z0-9_-]+, so the colon is sanitized there only.
    effective_harness = harness if canonical == "acp" and ":" in harness else canonical
    # Name preserves the user's input (matching the pre-acp behavior, e.g.
    # --harness claude -> name "claude"), sanitized for the colon so acp:<slug>
    # yields a valid [a-zA-Z0-9_-]+ name. Filename uses the canonical/effective
    # id (also colon-sanitized) as before.
    display_name = harness.replace(":", "-")

    tmpdir = Path(tempfile.mkdtemp(prefix="omnicraft-harness-launcher-"))
    yaml_path = tmpdir / f"{effective_harness.replace(':', '-')}.yaml"

    executor: dict[str, str] = {"harness": effective_harness}
    if model is not None:
        executor["model"] = model

    raw = {
        "name": display_name,
        "prompt": system_prompt or _default_harness_prompt(canonical),
        "executor": executor,
    }
    if canonical in _OS_ENV_HARNESSES:
        raw["os_env"] = {"type": "caller_process", "sandbox": {"type": "none"}}
    yaml_path.write_text(yaml.safe_dump(raw, default_flow_style=False))
    return yaml_path


def _missing_run_agent_message() -> str:
    """Return the no-AGENT ``run`` guidance shown on missing input."""
    return (
        "Forneça um caminho de AGENT, passe --server para conectar a um servidor, "
        "ou passe --harness para lançar um "
        "harness interno diretamente:\n"
        "  omnicraft run examples/hello_world.yaml\n"
        "  omnicraft run --server http://localhost:6767\n"
        "  omnicraft run --harness claude-sdk\n"
        "  omnicraft run --harness codex"
    )


@dataclass(frozen=True)
class _ResumeChoice:
    """
    Outcome of parsing the click ``--resume`` option value.

    Named fields rather than a tuple so a future shape change (e.g. a
    third resume mode) doesn't become a positional break at every
    call site.
    """

    picker: bool
    conversation_id: str | None


def _split_resume_value(resume: str | None) -> _ResumeChoice:
    """
    Translate the click ``--resume`` option value into the internal
    ``resume_picker`` / ``resume_conversation_id`` shape.

    ``--resume`` is wired with ``is_flag=False`` + ``flag_value``, so
    click hands us one of three values:

    - ``None`` — option absent. No resume requested.
    - :data:`_RESUME_PICKER_SENTINEL` — ``--resume`` passed without a
      value. User wants the interactive picker.
    - any other string — ``--resume <id>``. User wants to attach to
      that specific conversation id.

    The downstream dispatcher / ``run_chat`` boundary still takes the
    two-field shape (the picker mode and the conv-id mode end up in
    different code paths inside ``_resolve_resume_target``); the
    split lives here so the click layer is the only place that knows
    about the consolidation.
    """
    if resume is None:
        return _ResumeChoice(picker=False, conversation_id=None)
    if resume == _RESUME_PICKER_SENTINEL:
        return _ResumeChoice(picker=True, conversation_id=None)
    return _ResumeChoice(picker=False, conversation_id=resume)


# Params that are one-shot or replaced on resume — excluded from the
# resume command hint.  Everything else Click parsed is preserved
# automatically, so new flags don't need any resume-hint bookkeeping.
_RESUME_SKIP_PARAMS: frozenset[str] = frozenset(
    {
        "prompt",
        "resume",
        "resume_latest",
        "fork_session_id",
        # ephemeral is session-scoped infrastructure flag, not
        # meaningful across invocations.
        "ephemeral",
    }
)


def _build_resume_parts() -> list[str]:
    """Build the flag-preserving prefix for the resume command from Click's
    parsed context.

    Iterates the active Click context's parameters and reconstructs
    every flag/argument whose value differs from its default, skipping
    one-shot params (``-p``, ``--fork``, ``-c``, ``--resume``, etc.).
    The caller appends ``--resume <conversation_id>`` and joins with
    :func:`shlex.join`.

    Must be called while a Click context is active (i.e. inside a
    Click command handler or a function it calls synchronously).

    :returns: Argument list prefix, e.g.
        ``["omnicraft", "run", "agent.yaml", "--server",
        "https://example.com"]``.
    """
    ctx = click.get_current_context()
    parts: list[str] = ctx.command_path.split()

    for param in ctx.command.params:
        if param.name is None or param.name in _RESUME_SKIP_PARAMS:
            continue
        value = ctx.params.get(param.name)
        if value is None or value == param.default:
            continue

        if isinstance(param, click.Argument):
            parts.append(str(value))
        elif isinstance(param, click.Option):
            # Prefer the long-form flag (e.g. --harness over -h).
            flag = max(param.opts, key=len)
            if param.is_flag:
                parts.append(flag)
            else:
                parts.append(flag)
                parts.append(str(value))

    return parts


def _dispatch_native_terminal_harness(
    *,
    harness: str,
    server: str | None,
    model: str | None,
    prompt: str | None,
    system_prompt: str | None,
    tools: str | None,
    log: bool,
    debug_events: bool,
    resume_conversation_id: str | None,
    resume_picker: bool,
    resume_latest: bool,
    fork_session_id: str | None,
    ephemeral: bool,
    auto_open_conversation: bool,
) -> bool:
    """
    Launch a ``*-native`` terminal harness via its TUI wrapper directly.

    ``run --harness cursor-native`` (and the claude/codex/pi equivalents)
    must NOT go through the materialized-launcher REPL: that drives an
    OmniCraft turn per message — which persists its own user item — *while*
    the harness forwarder mirrors the same message back from the TUI's
    transcript, recording every user message twice. These harnesses are
    terminal-mirror sessions whose turns originate in the TUI, so dispatch
    straight to the native wrapper (the same code ``omnicraft cursor`` /
    ``omnicraft claude`` / etc. run), keeping the TUI the single source of
    turns. A top-level ``--model`` is forwarded as a passthrough CLI flag.

    ``--continue`` is honored (not rejected): it resolves to this harness's
    most-recent conversation and hands that off to the wrapper, matching the
    pre-dispatch launcher behavior so it is not a silent resume regression.

    :param harness: The requested ``--harness`` value (canonical or alias).
    :returns: ``True`` when *harness* is a native terminal harness and was
        dispatched here; ``False`` when it is not one (caller continues).
    """
    from omnicraft.native_coding_agents import native_coding_agent_for_harness

    native_agent = native_coding_agent_for_harness(harness)
    if native_agent is None:
        return False

    # The native TUI wrappers attach to a tmux pane and own their own turn
    # loop, so REPL-only options have no analog there. Reject them loudly
    # rather than silently dropping them, and point at the dedicated
    # subcommand. (``--continue``/``--resume <id>``/``--resume`` picker ARE
    # supported below — they map onto the wrapper's session selection.)
    unsupported = [
        flag
        for flag, active in (
            ("-p/--prompt", prompt is not None),
            ("--system-prompt", system_prompt is not None),
            ("--tools", tools is not None),
            ("--log", log),
            ("--debug-events", debug_events),
            ("--fork", fork_session_id is not None),
            ("--no-session", ephemeral),
        )
        if active
    ]
    if unsupported:
        # These are REPL-only options with no analog in the TUI — and the
        # dedicated subcommand doesn't accept them either (it would treat them
        # as passthrough args), so tell the user to drop them rather than
        # redirect. ``--model`` and session selection (--resume/--continue) ARE
        # honored here.
        raise click.ClickException(
            f"`run --harness {harness}` lança a TUI do {native_agent.display_name} diretamente; "
            f"a(s) opção(ões) só-de-REPL {', '.join(unsupported)} não têm efeito ali — remova-as."
        )

    server = _ensure_backend(server)
    passthrough = ("--model", model) if model else ()

    # Resolve --continue to a concrete conversation id (the wrappers take a
    # session id / picker, not a "latest" flag). Precedence matches the REPL:
    # an explicit id wins, then the picker, then --continue.
    session_id = resume_conversation_id
    if session_id is None and not resume_picker and resume_latest:
        from omnicraft.chat import _remote_headers, _resolve_latest_conversation_id

        session_id = _resolve_latest_conversation_id(
            base_url=server,
            agent_name=native_agent.agent_name,
            headers=_remote_headers(server_url=server),
        )
        # The user explicitly asked to continue; if there's nothing to continue,
        # fail loud rather than silently starting fresh (matches the REPL's
        # _resolve_resume_target behavior).
        if session_id is None:
            raise click.ClickException(
                f"Nenhuma conversa anterior para o agente {native_agent.agent_name!r}."
            )

    common = {
        "server": server,
        "session_id": session_id,
        "resume_picker": resume_picker,
        "auto_open_conversation": auto_open_conversation,
    }
    if native_agent.key == "claude":
        from omnicraft.claude_native import run_claude_native

        run_claude_native(claude_args=passthrough, **common)
    elif native_agent.key == "codex":
        from omnicraft.codex_native import run_codex_native

        # Codex takes its model as a first-class arg, not a passthrough flag.
        run_codex_native(codex_args=(), model=model, **common)
    elif native_agent.key == "pi":
        from omnicraft.pi_native import run_pi_native

        run_pi_native(pi_args=passthrough, **common)
    elif native_agent.key == "cursor":
        from omnicraft.cursor_native import run_cursor_native

        run_cursor_native(cursor_args=passthrough, **common)
    elif native_agent.key == "opencode":
        from omnicraft.opencode_native import run_opencode_native

        # OpenCode pins its model on the wrapper spec (like Codex), so it takes
        # ``model`` first-class rather than via a ``--model`` passthrough arg.
        run_opencode_native(opencode_args=(), model=model, **common)
    elif native_agent.key == "kimi":
        from omnicraft.kimi_native import run_kimi_native

        run_kimi_native(kimi_args=passthrough, **common)
    else:  # pragma: no cover - new native agent added without a dispatch arm
        raise click.ClickException(
            f"Nenhum lançador de terminal nativo ligado ao harness {harness!r}."
        )
    return True


def _reject_agent_with_native_terminal_harness(harness: str) -> None:
    """
    Reject ``run AGENT --harness <x>-native``: native harnesses own their TUI.

    A ``*-native`` harness mirrors an external CLI's own TUI; the agent spec's
    prompt/tools are never consulted, and driving it through the REPL would
    double-record every message (OmniCraft turn + forwarder mirror). So an
    explicit AGENT path combined with a native terminal harness has no coherent
    meaning — fail loud and point at the dedicated subcommand.

    :param harness: The requested ``--harness`` value (canonical or alias).
    :raises click.ClickException: When *harness* is a native terminal harness.
    """
    from omnicraft.native_coding_agents import native_coding_agent_for_harness

    native_agent = native_coding_agent_for_harness(harness)
    if native_agent is None:
        return
    raise click.ClickException(
        f"`--harness {harness}` lança a TUI do {native_agent.display_name} e "
        f"ignora um spec de AGENT; remova o caminho de AGENT e rode "
        f"`omnicraft {native_agent.terminal_name}` (ou `run --harness {harness}`)."
    )


def _dispatch_run(
    *,
    target: str | None,
    tools: str | None,
    harness: str | None,
    model: str | None,
    prompt: str | None,
    system_prompt: str | None,
    server: str | None = None,
    resume_picker: bool = False,
    resume_latest: bool = False,
    resume_conversation_id: str | None = None,
    fork_session_id: str | None = None,
    ephemeral: bool = False,
    log: bool = False,
    debug_events: bool = False,
    resume_parts: list[str] | None = None,
    auto_open_conversation: bool = False,
    server_from_cli: bool = False,
) -> None:
    """
    Route ``omnicraft run`` to the right impl.

    The click path always drives the OmniCraft server-backed REPL. With
    ``--server <url>``, use that server URL instead of starting a
    local server. (``omnicraft attach`` is a separate attach-only
    client and does NOT route through here.)

    :param target: Agent YAML/directory path, or ``None`` for
        ``run --harness ...`` launcher mode / ``--server`` direct-server
        mode.
    :param tools: ``--tools`` client-side tool set name.
    :param harness: ``--harness`` value.
    :param model: ``--model`` value.
    :param prompt: ``-p`` / ``--prompt`` value.
    :param system_prompt: ``--system-prompt`` value.
    :param server: Server URL from ``--server`` or config. With a local
        target, this is the OmniCraft server used for upload/session setup; with
        no target and explicit ``--server``, this is the direct server.
    :param resume_picker: True when ``--resume`` / ``-r`` is set with
        no value (interactive picker).
    :param resume_latest: True when ``--continue`` / ``-c`` is set.
    :param resume_conversation_id: Explicit conversation id from
        ``--resume <id>``.
    :param fork_session_id: When set, fork this session and open the
        REPL on the fork. Mutually exclusive with ``--resume`` and
        ``--continue``.
    :param ephemeral: True when ``--no-session`` is set.
    :param log: True when ``--log`` is set.
    :param debug_events: True when ``--debug-events`` is set.
        Enables the SSE event tape overlay, JSONL event logging,
        and pipeline counters in the toolbar.
    :param resume_parts: Pre-built argument list prefix for the
        resume command shown on exit, e.g.
        ``["omnicraft", "run", "agent.yaml", "--harness", "codex"]``.
        ``None`` when called outside the Click command path.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL when the session id becomes known.
    :param server_from_cli: ``True`` when ``--server`` was explicitly
        provided on the command line. Used to distinguish direct-server
        mode from a configured default server.
    """
    if target is not None and _is_server_url(target):
        raise click.ClickException(
            "URLs de servidor não são mais aceitas como o argumento AGENT. "
            f"Use `omnicraft run --server {target}` em vez disso."
        )

    if target is None:
        if server_from_cli and server is not None and harness is None:
            # Normalize like every other entry point: expand a bare workspace
            # URL to its /api/2.0/omnicraft mount and strip any ?o= query. Else
            # a direct ``--server`` request hits the root and bounces to /login.
            base_url = _resolve_server_url(server)
            # Direct ``--server`` (no AGENT) has no local runner to bind, so an
            # interactive resume-by-id is an ATTACH: route it through the
            # `attach` pair (`_require_live_conversation` + `run_attach`), not
            # the picker+create path that crashed at runner-bind ("requires a
            # registered runner id"). Only the *pure interactive*
            # shape reroutes — a one-shot ``-p`` or any local-agent-only flag
            # (--model/--system-prompt/--log/--no-session) falls through to the
            # existing remote-URL path below, which one-shots or fails loud as
            # before instead of silently no-op'ing here. Picker/`--continue`
            # have no id to attach to and likewise stay on that path.
            # Pure interactive shape = no one-shot prompt and no local-agent-only
            # override; the ``resume_conversation_id is not None`` check stays in
            # the ``if`` so the type narrows for the calls below.
            is_interactive_shape = (
                prompt is None
                and not resume_latest
                and not resume_picker
                and fork_session_id is None
                and not log
                and not ephemeral
                and model is None
                and system_prompt is None
            )
            if resume_conversation_id is not None and is_interactive_shape:
                from omnicraft.chat import _redirect_native_resume_if_needed, run_attach

                if _redirect_native_resume_if_needed(
                    base_url=base_url,
                    conversation_id=resume_conversation_id,
                    auto_open_conversation=auto_open_conversation,
                ):
                    return

                _require_live_conversation(
                    base_url=base_url,
                    conversation_id=resume_conversation_id,
                )
                run_attach(
                    base_url=base_url,
                    conversation_id=resume_conversation_id,
                    client_tools=tools,
                    debug_events=debug_events,
                    auto_open_conversation=auto_open_conversation,
                    # Keep the run-style parts so the exit "Resume:" hint
                    # reproduces the (now-working) command the user ran.
                    resume_parts=resume_parts,
                )
                return

            from omnicraft.chat import run_chat

            run_chat(
                target=base_url,
                client_tools=tools,
                server_url=None,
                harness=harness,
                model=model,
                prompt=prompt,
                system_prompt=system_prompt,
                ephemeral=ephemeral,
                resume_conversation_id=resume_conversation_id,
                resume_latest=resume_latest,
                resume_picker=resume_picker,
                fork_session_id=fork_session_id,
                log=log,
                debug_events=debug_events,
                resume_parts=resume_parts,
                auto_open_conversation=auto_open_conversation,
            )
            return
        if harness is None:
            raise click.ClickException(_missing_run_agent_message())
        # ``*-native`` terminal harnesses launch their own TUI wrapper instead of
        # the materialized-launcher REPL — the REPL would double-record every
        # user message (OmniCraft turn + forwarder mirror). Returns False for
        # non-native harnesses, which fall through to the launcher below.
        if _dispatch_native_terminal_harness(
            harness=harness,
            server=server,
            model=model,
            prompt=prompt,
            system_prompt=system_prompt,
            tools=tools,
            log=log,
            debug_events=debug_events,
            resume_conversation_id=resume_conversation_id,
            resume_picker=resume_picker,
            resume_latest=resume_latest,
            fork_session_id=fork_session_id,
            ephemeral=ephemeral,
            auto_open_conversation=auto_open_conversation,
        ):
            return
        if ephemeral:
            raise click.ClickException(
                "--no-session requer um caminho de AGENT; o lançamento de harness sem "
                "AGENT já usa um spec de agente temporário gerado."
            )
        target = str(
            _materialize_harness_launcher_file(
                harness=harness,
                model=model,
                system_prompt=system_prompt,
            )
        )
        harness = None
        model = None
        system_prompt = None
    elif harness is not None:
        _validate_harness(harness)
        # A ``*-native`` harness IS its own TUI agent — pairing it with an AGENT
        # spec is meaningless, and routing it through the REPL would double-record
        # every message (OmniCraft turn + forwarder mirror, same as the no-AGENT
        # path above). Reject rather than silently launch the broken surface.
        _reject_agent_with_native_terminal_harness(harness)

    if server is not None:
        if _is_server_url(target):
            raise click.ClickException(
                "--server serve para vincular um YAML de agente LOCAL a um servidor "
                "remoto. Passe um caminho de YAML como alvo (recebeu uma URL)."
            )

    if fork_session_id is not None:
        if resume_conversation_id or resume_latest or resume_picker:
            raise click.ClickException("--fork é mutuamente exclusivo com --resume e --continue.")
        if prompt is not None:
            raise click.ClickException("--fork requer o modo REPL interativo; remova -p/--prompt.")

    harness = canonicalize_harness(harness)
    if prompt is not None:
        if resume_conversation_id is not None or resume_latest or resume_picker:
            from omnicraft.chat import run_chat

            run_chat(
                target=target,
                client_tools=tools,
                server_url=server,
                harness=harness,
                model=model,
                prompt=prompt,
                system_prompt=system_prompt,
                ephemeral=ephemeral,
                resume_conversation_id=resume_conversation_id,
                resume_latest=resume_latest,
                resume_picker=resume_picker,
                debug_events=debug_events,
                auto_open_conversation=auto_open_conversation,
            )
            return
        if log:
            raise click.ClickException(
                "--log só é suportado no modo REPL interativo neste caminho do CLI; "
                "remova -p/--prompt para rodar em modo headless."
            )
        # Headless ``-p`` runs against the daemon-backed server too (the
        # host daemon connects to ``--server`` or starts a local server),
        # so it stays consistent with interactive mode. ``run_chat`` runs
        # one-shot and exits when ``initial_message`` is set. The only
        # exception is ``--no-session``: it keeps the legacy in-process
        # ephemeral path via ``run_prompt`` (no daemon, no persistence).
        if not ephemeral:
            from omnicraft.chat import run_chat

            run_chat(
                target=target,
                client_tools=tools,
                server_url=server,
                harness=harness,
                model=model,
                prompt=prompt,
                system_prompt=system_prompt,
                ephemeral=False,
                debug_events=debug_events,
                auto_open_conversation=auto_open_conversation,
            )
            return

        from omnicraft.chat import run_prompt

        run_prompt(
            target=target,
            client_tools=tools,
            harness=harness,
            model=model,
            prompt=prompt,
            system_prompt=system_prompt,
            ephemeral=ephemeral,
        )
        return

    from omnicraft.chat import run_chat

    run_chat(
        target=target,
        client_tools=tools,
        server_url=server,
        harness=harness,
        model=model,
        prompt=None,
        system_prompt=system_prompt,
        ephemeral=ephemeral,
        resume_conversation_id=resume_conversation_id,
        resume_latest=resume_latest,
        resume_picker=resume_picker,
        fork_session_id=fork_session_id,
        log=log,
        debug_events=debug_events,
        resume_parts=resume_parts,
        auto_open_conversation=auto_open_conversation,
    )


def _resolve_attach_server(server: str | None, configured_server: str | None) -> str | None:
    """
    Resolve the OmniCraft server URL ``attach`` should join.

    Resolution order: an explicit ``--server`` value, then the configured
    ``server`` default, then a local OmniCraft server already running in the
    background. ``attach`` never starts a server, so this returns ``None``
    when none of those is available and the caller fails loud.

    :param server: Explicit ``--server`` value, e.g.
        ``"https://example.databricksapps.com"``, or ``None``.
    :param configured_server: The ``server`` default from config (the
        ``server`` key of the effective merged config), or ``None``.
    :returns: Normalized base URL without a trailing slash, or ``None``.
    """
    chosen = server if server is not None else configured_server
    if chosen:
        return _resolve_server_url(chosen)
    local = local_server_url_if_healthy()
    return local.rstrip("/") if local else None


def _require_live_conversation(
    *,
    base_url: str,
    conversation_id: str,
) -> None:
    """
    Fail loud unless *conversation_id* is reachable on *base_url*.

    ``attach`` is an attach-only client; if the session is not live there
    is nothing to join, so we surface a clear error rather than letting the
    REPL connect to a phantom conversation. Issues a single
    ``GET /v1/sessions/{id}`` and raises on a transport failure or any
    non-200 status.

    :param base_url: OmniCraft server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :param conversation_id: Conversation id to attach to, e.g.
        ``"conv_abc123"``.
    :raises click.ClickException: When the server is unreachable or the
        conversation does not exist.
    """
    result = _host_http_json(
        base_url=base_url,
        method="GET",
        path=f"/v1/sessions/{conversation_id}",
    )
    # ``_host_http_json`` reports transport failures as status 0 (never
    # raises), so the server-down and missing-session cases both land here.
    if result.status_code == 0:
        raise click.ClickException(
            f"Não foi possível acessar um servidor em {base_url}: "
            f"{_host_error_text(result.body)}. "
            "`attach` nunca inicia um servidor — verifique a URL, ou inicie um com "
            "`omnicraft run`."
        )
    if result.status_code != 200:
        raise click.ClickException(
            f"Nenhuma sessão ativa '{conversation_id}' em {base_url} "
            f"(servidor retornou {result.status_code}). Rode `omnicraft host status` "
            "para listar sessões ativas, ou `omnicraft run <agent.yaml>` para iniciar uma."
        )


@cli.command()
@click.argument("conversation", required=False, metavar="[CONVERSATION_ID]")
@click.option(
    "--server",
    default=None,
    help=(
        "Servidor AP hospedando a sessão. Padrão: o servidor configurado, "
        "ou um servidor local já em execução em segundo plano."
    ),
)
@click.option(
    "--tools",
    default=None,
    help="Nome do conjunto de ferramentas do lado do cliente (ex. 'coding') para acesso ao shell.",
)
@click.option(
    "--debug-events",
    "debug_events",
    is_flag=True,
    default=False,
    help=(
        "Habilita o pipeline de debug SSE-para-UI: sobreposição de fita "
        "de eventos Ctrl+E, log de eventos JSONL (~/.omnicraft/debug/) e "
        "contadores de estágio do pipeline na toolbar."
    ),
)
def attach(
    conversation: str | None,
    server: str | None,
    tools: str | None,
    debug_events: bool,
) -> None:
    """Anexa o REPL a uma sessão ATIVA — nunca inicia nada.

    ``attach`` é um cliente leve: junta-se a uma conversa já em execução
    em um servidor e transmite sua E/S. Nunca cria um servidor, runner ou
    harness, não aplica padrões de model/harness e erra em alto e bom som
    quando não há nada ativo para anexar. Para INICIAR uma sessão use
    ``omnicraft run``; para reabrir/reiniciar uma armazenada use
    ``omnicraft resume``.

    \b
    Exemplos:
      omnicraft attach conv_abc123
      omnicraft attach conv_abc123 --server https://<app>.databricksapps.com
    """
    cfg = _load_effective_config()
    base_url = _resolve_attach_server(server, cfg.get("server"))
    if base_url is None:
        raise click.ClickException(
            "Nenhum servidor para anexar. `attach` junta-se a uma sessão ATIVA em um "
            "servidor em execução — inicie um com `omnicraft run`, ou aponte para um com "
            "`--server <url>`."
        )
    if conversation is None:
        raise click.ClickException(
            "Nada para anexar: `attach` junta-se a uma sessão ATIVA por id. "
            f"Rode `omnicraft host status` para listar sessões em {base_url}, ou "
            "`omnicraft run <agent.yaml>` para iniciar uma nova."
        )
    _require_live_conversation(base_url=base_url, conversation_id=conversation)
    auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)
    from omnicraft.chat import run_attach

    # Attach is a pure client: it joins the live session and dispatches turns to
    # the runner the host already bound (like the web UI co-drive), never
    # spawning a server/runner/harness. ``run_attach`` fails loud if the host
    # is offline (no online runner to dispatch to).
    run_attach(
        base_url=base_url,
        conversation_id=conversation,
        client_tools=tools,
        debug_events=debug_events,
        auto_open_conversation=auto_open_conversation,
        resume_parts=["cli", "attach", conversation, "--server", base_url],
    )


# `run` absorbs the legacy ``omnicraft run`` subcommand. With an AGENT
# argument it opens the interactive REPL on a freshly started session;
# without AGENT it can launch a built-in harness directly via ``--harness``.
# Both paths route through the same OmniCraft server+REPL dispatcher.
@cli.command()
@click.argument("target", required=False, metavar="[AGENT]")
@click.option(
    "--tools",
    default=None,
    help="Nome do conjunto de ferramentas do lado do cliente (ex. 'coding') para acesso ao shell.",
)
@click.option("--harness", default=None, help=_RUN_HARNESS_HELP)
@click.option("--model", default=None, help=_MODEL_HELP)
@click.option("-p", "--prompt", default=None, help=_PROMPT_HELP)
@click.option("--system-prompt", "system_prompt", default=None, help=_SYSTEM_PROMPT_HELP)
@click.option(
    "-r",
    "--resume",
    "resume",
    is_flag=False,
    flag_value=_RESUME_PICKER_SENTINEL,
    default=None,
    help=_RESUME_HELP,
)
@click.option(
    "-c", "--continue", "resume_latest", is_flag=True, default=False, help=_CONTINUE_HELP
)
@click.option("--fork", "fork_session_id", default=None, help=_FORK_HELP)
@click.option("--no-session", "ephemeral", is_flag=True, default=False, help=_NO_SESSION_HELP)
@click.option("--log/--no-log", "log", default=False, help=_LOG_HELP)
@click.option(
    "--server",
    default=None,
    help=(
        "URL omnicraft remota. Faz upload do YAML local como um agente "
        "efêmero, cria um runner LOCAL que faz túnel para este servidor (para "
        "terminais/MCPs rodarem no seu laptop) e conecta o REPL a ele. "
        'Passe --server "" para auto-criar um servidor local persistente em '
        "segundo plano e mirar nele em vez de um remoto."
    ),
)
@click.option(
    "--debug-events",
    "debug_events",
    is_flag=True,
    default=False,
    help=(
        "Habilita o pipeline de debug SSE-para-UI: sobreposição de fita "
        "de eventos Ctrl+E, log de eventos JSONL (~/.omnicraft/debug/) e "
        "contadores de estágio do pipeline na toolbar."
    ),
)
@click.option(
    "--host",
    "register_host",
    is_flag=True,
    default=False,
    help=(
        "Registra esta máquina como um host no servidor remoto "
        "(equivalente inline de `omnicraft host`). Requer --server."
    ),
)
def run(
    target: str | None,
    tools: str | None,
    harness: str | None,
    model: str | None,
    prompt: str | None,
    system_prompt: str | None,
    resume: str | None,
    resume_latest: bool,
    fork_session_id: str | None,
    ephemeral: bool,
    log: bool,
    server: str | None,
    debug_events: bool,
    register_host: bool,
) -> None:
    """Inicia uma sessão com um agente OmniCraft.

    AGENT pode ser um arquivo YAML de agente ou um diretório de agente. Sem AGENT,
    passe ``--server`` para conectar diretamente a um servidor, ou passe
    ``--harness`` para lançar um harness interno diretamente.

    Padrão: arquitetura servidor+REPL do omnicraft (cria um servidor
    local, o REPL conecta como cliente HTTP). Com ``--server <url>`` e
    sem AGENT, conecta diretamente a esse servidor; com AGENT, usa a topologia
    runner local + servidor remoto (RUNNER.md §6 Flow 1) - o laptop hospeda
    runner/harnesses, o servidor hospeda o estado.

    \b
    Exemplos:
      omnicraft run --harness claude-sdk
      omnicraft run --harness codex -p "revise o último commit"
      omnicraft run examples/hello_world.yaml
      omnicraft run examples/hello_world.yaml --harness codex --model gpt-5.4-mini
      omnicraft run --server http://localhost:6767
      omnicraft run examples/databricks_coding_agent.yaml --server https://<app>.databricksapps.com
    """
    # Apply config defaults for any value the user did not pass explicitly.
    # Explicit CLI args always take precedence; project-local config overrides
    # global config, which provides user-level defaults.
    server_source = click.get_current_context().get_parameter_source("server")
    server_from_cli = server_source is not None and server_source.name == "COMMANDLINE"
    harness_source = click.get_current_context().get_parameter_source("harness")
    harness_from_cli = harness_source is not None and harness_source.name == "COMMANDLINE"
    direct_server_cli = (
        target is None and server_from_cli and server is not None and not harness_from_cli
    )

    _global_cfg = _load_effective_config()
    if target is None and not direct_server_cli:
        # Harness-aware default-agent resolution (this branch) under main's
        # direct-`--server` guard: skip the configured default_agent when the
        # invocation is a bare `--server` (no AGENT, no --harness), else pick
        # it — but fall back to a built-in launcher when an explicit --harness
        # doesn't match the default agent's harness.
        target = _resolve_default_agent_target(_global_cfg.get("default_agent"), harness)
    if server is None:
        server = _global_cfg.get("server")
    if model is None and not direct_server_cli:
        model = _global_cfg.get("model")
    if harness is None and not direct_server_cli:
        harness = _global_cfg.get("harness")

    # First-run smart defaults: a bare `run` with no AGENT, no --harness, and no
    # explicit persisted default → derive a harness from the *current* creds
    # (Claude→fucho, else Codex, else Pi); or drop into `configure harnesses`
    # when nothing is set up. The derived pick is NOT persisted, so it tracks
    # the credentials — adding Claude later promotes a Codex-only user to fucho.
    if target is None and harness is None and not direct_server_cli:
        plan = _resolve_first_run_plan()
        if plan is None:
            return  # nothing configured even after offering configure — exit cleanly
        harness = plan.harness
        target = plan.agent  # fucho path for Claude; None (bare harness) for codex/pi

    # Interactive ``omnicraft run`` opens the live conversation in the
    # browser by default so users discover the web UI once the server is up
    # (the accounts-mode magic-redeem auto-open used to surface this, but
    # accounts is no longer the default auth). An explicit
    # ``auto_open_conversation`` config value (true/false) always wins, so
    # users who opted out stay opted out. Headless ``-p`` one-shots stay
    # quiet unless the user explicitly opted in.
    auto_open_setting = _resolve_auto_open_conversation_setting(_global_cfg)
    auto_open_conversation = auto_open_setting if auto_open_setting is not None else prompt is None

    # NOTE: the host daemon + OmniCraft server are ensured inside ``run_chat``'s
    # non-URL branch (a URL ``target`` connects directly). ``--host`` is now
    # redundant (the daemon is always ensured) and kept only as a no-op.
    del register_host

    choice = _split_resume_value(resume)
    # Capture resume-safe CLI parts before dispatch mutates target,
    # harness, or model for no-AGENT launcher mode.
    resume_parts = _build_resume_parts()
    _dispatch_run(
        target=target,
        tools=tools,
        harness=harness,
        model=model,
        prompt=prompt,
        system_prompt=system_prompt,
        server=server,
        resume_picker=choice.picker,
        resume_latest=resume_latest,
        resume_conversation_id=choice.conversation_id,
        fork_session_id=fork_session_id,
        ephemeral=ephemeral,
        log=log,
        debug_events=debug_events,
        resume_parts=resume_parts,
        auto_open_conversation=auto_open_conversation,
        server_from_cli=server_from_cli,
    )


class _HostGroup(click.Group):
    """
    ``host`` group that accepts a server URL as a positional argument.

    ``omnicraft host <url>`` is shorthand for ``omnicraft host
    --server <url>`` when ``<url>`` is URL-like or the empty local-mode
    marker. A leading positional token that matches a registered
    management subcommand (``status``, ``stop``, ``stop-session``)
    still dispatches to that subcommand, and other unknown tokens fall
    through to Click's normal unknown-command error.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """
        Redirect a leading URL-like positional into ``--server``.

        ``omnicraft host <url>`` is shorthand for ``omnicraft host --server
        <url>``. We detect a leading URL-like positional with a throwaway
        option parse and, when present, rewrite the argument list to inject
        ``--server <url>`` *before* Click parses it -- so Click sees a normal
        option and never treats the URL as a would-be subcommand.

        This deliberately avoids Click's internal ``protected_args`` (made a
        read-only property in click 8.2 and slated for removal in click 9),
        so the shorthand keeps working across click versions. A leading token
        that is a registered subcommand, or not URL-like, is left untouched
        for Click's normal dispatch / unknown-command error.

        :param ctx: Click context for the ``host`` group.
        :param args: Raw argument tokens for the group.
        :returns: Remaining args after the group consumes its own.
        """
        return super().parse_args(ctx, self._rewrite_positional_server(ctx, list(args)))

    def _rewrite_positional_server(self, ctx: click.Context, args: list[str]) -> list[str]:
        """
        Rewrite a leading URL-like positional into an explicit ``--server``.

        Runs a throwaway parse of the group's own options to find the first
        positional token. When that token is URL-like (and not a registered
        subcommand), removes it from *args* and prepends ``--server <token>``;
        otherwise returns *args* unchanged so Click dispatches the subcommand
        or raises its own unknown-command error. Raises when the positional
        URL is combined with an explicit ``--server`` or with extra
        positionals.

        :param ctx: Click context for the ``host`` group.
        :param args: Raw argument tokens for the group.
        :returns: Possibly-rewritten argument tokens.
        """
        # Resilient parsing (shell completion) must keep default behavior so
        # subcommand names still complete.
        if ctx.resilient_parsing or not args:
            return args
        try:
            parser = self.make_parser(ctx)
            # A click.Group defaults to allow_interspersed_args=False, which would
            # treat an option *after* the positional URL (e.g.
            # `host <url> --non-interactive`) as an extra positional. Enable
            # interspersed parsing so trailing options are classified as options.
            parser.allow_interspersed_args = True
            opts, positionals, _ = parser.parse_args(list(args))
        except click.UsageError:
            # Malformed options: let the real parse surface the error.
            return args
        if (
            not positionals
            or positionals[0] in self.commands
            or not self._token_is_positional_server(positionals[0])
        ):
            return args
        url = positionals[0]
        if opts.get("server") is not None:
            raise click.UsageError(
                "Passe a URL do servidor posicionalmente ou via --server, não ambos."
            )
        if positionals[1:]:
            raise click.UsageError(
                f"Argumento(s) extra(s) inesperado(s): {' '.join(positionals[1:])}"
            )
        # remove() drops the first token equal to `url`. Safe because the only
        # value-taking group option (--server) triggers the conflict error above,
        # so the URL can't be some other option's value.
        remaining = list(args)
        remaining.remove(url)
        return ["--server", url, *remaining]

    def _token_is_positional_server(self, token: str) -> bool:
        """
        Return whether a token may be used as positional ``host`` server.

        The shorthand intentionally accepts only HTTP(S) server URLs and
        the empty string local-mode marker. Plain words such as
        ``"sessions"`` are more likely command typos, so Click should
        report them as unknown subcommands instead of treating them as
        remote server addresses.

        :param token: Leading positional token, e.g.
            ``"https://example.databricksapps.com"`` or ``""``.
        :returns: ``True`` if the token should bind to ``--server``.
        """
        return token == "" or _is_server_url(token)


def _prompt_stop_local_server() -> None:
    """Ask whether to also stop the detached local OmniCraft server after exit.

    The local-mode host daemon spawns a detached, persistent local AP
    server (:func:`ensure_local_omnicraft_server`) that survives the daemon's exit
    so sessions and the Web UI stay reachable across ``host`` / ``run``.
    Users expect Ctrl-C to stop "everything", so when a healthy local server
    is still running we prompt to stop it too. Declining — or a
    non-interactive / aborted prompt (EOF, a second Ctrl-C) — leaves it
    running. No-op when no healthy local server is found (never spawned, or
    already stopped).

    :returns: None.
    """
    url = local_server_url_if_healthy()
    if url is None:
        return
    try:
        stop = click.confirm(
            f"\nO servidor local em {url} ainda está rodando para que suas sessões e "
            "o Web UI continuem acessíveis entre `host`/`run`.\nParar ele também?",
            default=False,
        )
    except click.Abort:
        # Non-interactive stdin (EOF) or a second Ctrl-C: leave it running
        # rather than hang. ``click.confirm`` maps both to ``Abort``.
        click.echo()
        stop = False
    if stop:
        stop_local_omnicraft_server()
        click.echo(f"Servidor local parado ({url}).")
    else:
        click.echo(f"Servidor local deixado rodando em {url}.")


@cli.group("host", cls=_HostGroup, invoke_without_command=True)
@click.option("--server", default=None, help="URL do servidor omnicraft remoto.")
@click.option(
    "--non-interactive",
    "non_interactive",
    is_flag=True,
    default=False,
    help=(
        "Nunca pede login. Quando o servidor requer auth e você "
        "não está logado, falha com a dica `omnicraft login` em vez de "
        "lançar o fluxo de login no navegador. Use isto em scripts e CI."
    ),
)
@click.pass_context
def host(ctx: click.Context, server: str | None, non_interactive: bool) -> None:
    """
    Registra esta máquina como um host em um servidor.

    \b
    Exemplos:
      omnicraft host https://omnicraft-app.databricksapps.com
      omnicraft host --server https://omnicraft-app.databricksapps.com
      omnicraft host ""   # cria + conecta a um servidor local

    A URL do servidor pode ser dada posicionalmente (``omnicraft host
    <url>``) ou via ``--server <url>``. Um token inicial ``status``, ``stop``
    ou ``stop-session`` ainda roda aquele subcomando de gerenciamento.

    Quando o servidor alvo é fronteado por Databricks e você não está
    logado, ``host`` roda o mesmo fluxo que ``omnicraft login`` rodaria antes
    de conectar (um fluxo interativo no navegador). Passe ``--non-interactive``
    para manter o comportamento antigo de script: falhar com o comando de login
    a ser rodado em vez de pedir.

    :param ctx: Contexto de invocação do Click. ``ctx.invoked_subcommand`` é
        definido quando um subcomando de gerenciamento como ``"status"`` está rodando.
    :param server: URL do servidor OmniCraft remoto, ex.
        ``"https://example.databricksapps.com"``. ``None`` recai
        na config; string vazia seleciona o modo local.
    :param non_interactive: Quando ``True``, nunca lança o login no navegador
        para um servidor remoto não autenticado — falha com a dica
        ``omnicraft login`` em vez disso.
    """
    ctx.ensure_object(dict)
    ctx.obj["server"] = server
    if ctx.invoked_subcommand is not None:
        return
    cfg = _load_effective_config()
    if server is None:
        server = cfg.get("server")
    if server:
        server = _resolve_server_url(server)
    # Remote mode is decided here, before the local-mode branch reassigns
    # ``server`` to the spawned loopback URL — only a remote target needs
    # the sign-in pre-flight.
    remote_mode = bool(server)

    from omnicraft.host.connect import run_host_process

    # ``host`` IS the daemon (foreground). With no server URL, start (or
    # reuse) the local OmniCraft server here and connect to it; otherwise connect to
    # the given remote/local URL. Unlike the background commands, we do not
    # spawn a second daemon via ``_ensure_host_daemon``.
    target = _normalize_daemon_target(server)
    # Only true when THIS invocation started the local server (vs reusing one
    # already started by `omnicraft server` or a prior host/run daemon) —
    # gates the Ctrl-C stop-server prompt so we never offer to stop a server
    # we didn't bring up.
    spawned_local_server = False
    if not server:
        startup = ensure_local_omnicraft_server()
        server = startup.url
        spawned_local_server = startup.spawned
    record = _foreground_daemon_record(
        target=target,
        server_url=server,
        host_id=_load_or_create_host_id(),
    )
    previous = _claim_foreground_daemon_record(record)
    # Only offer to stop the local server after a clean stop (Ctrl-C / normal
    # exit). A connection failure (SystemExit) leaves this False so we don't
    # prompt over an error.
    stopped_cleanly = False
    try:
        # Sign in first when the remote server is Databricks-fronted and we
        # hold no usable credentials — otherwise the tunnel upgrade is
        # redirected to a login page and the host dies with an opaque
        # "redirected to a login page" error after several retries. On a TTY
        # this runs the browser login and continues; ``--non-interactive``
        # (or a headless invocation) fails loud with the command to run.
        if remote_mode:
            _ensure_databricks_server_auth(server, non_interactive=non_interactive)
        run_host_process(server_url=server)
        stopped_cleanly = True
    except KeyboardInterrupt:
        # Ctrl-C is the normal way to stop the foreground daemon — swallow it
        # so we can prompt below instead of exiting with an "Aborted!" trace.
        stopped_cleanly = True
    finally:
        _restore_replaced_daemon_record(record, previous)
        # Offer to stop the local server only when WE spawned it this run.
        # Not in --server mode (someone else's server), and not when we reused
        # a server started by `omnicraft server` or another daemon — killing
        # that would surprise the user who brought it up independently. Users
        # expect Ctrl-C to stop "everything" they started, so the server we
        # spawned is fair game.
        if stopped_cleanly and spawned_local_server:
            _prompt_stop_local_server()


def _host_group_option(ctx: click.Context, key: str) -> str | None:
    """
    Read a group-level ``omnicraft host`` option for a subcommand.

    :param ctx: Click context passed to a host subcommand.
    :param key: Group option key, e.g. ``"server"``.
    :returns: The string option value, or ``None``.
    """
    obj = ctx.obj if isinstance(ctx.obj, dict) else {}
    value = obj.get(key)
    return value if isinstance(value, str) else None


def _resolve_host_server(server: str | None) -> str | None:
    """
    Resolve a host-management server from CLI or config.

    :param server: Explicit ``--server`` value, e.g.
        ``"https://example.databricksapps.com"``. ``None`` falls back
        to config; empty string selects local mode.
    :returns: Normalized server URL, or ``None`` for local mode.
    """
    if server is None:
        configured = _load_effective_config().get("server")
        server = str(configured) if configured else None
    return _resolve_server_url(server) if server else None


def _daemon_base_url(record: _HostDaemonRecord) -> str | None:
    """
    Resolve the OmniCraft server URL for a daemon record.

    :param record: Daemon registry record to inspect.
    :returns: OmniCraft server URL, e.g. ``"http://127.0.0.1:8123"``, or
        ``None`` when a local daemon's server cannot be discovered.
    """
    if record.mode == "local":
        if record.resolved_server_url:
            return record.resolved_server_url.rstrip("/")
        local_url = local_server_url_if_healthy()
        return local_url.rstrip("/") if local_url else None
    return (record.server_url or record.target).rstrip("/")


def _selected_daemon_records(
    *,
    server: str | None,
    all_targets: bool,
    default_all: bool,
) -> list[_HostDaemonRecord]:
    """
    Select daemon records for a host-management command.

    :param server: Explicit ``--server`` value, e.g.
        ``"https://example.databricksapps.com"``. ``None`` may mean
        all targets or config/local depending on ``default_all``.
    :param all_targets: Whether ``--all`` was passed.
    :param default_all: Whether no selector should mean all records.
    :returns: Matching daemon records.
    :raises click.ClickException: If ``--server`` and ``--all`` conflict.
    """
    if all_targets and server is not None:
        raise click.ClickException("Use --server ou --all, não os dois.")
    if all_targets or (server is None and default_all):
        return _list_daemon_records()
    target = _normalize_daemon_target(_resolve_host_server(server))
    record = _find_daemon_record(target)
    return [] if record is None else [record]


def _host_http_json(
    *,
    base_url: str,
    method: str,
    path: str,
    params: dict[str, str | int] | None = None,
    json_body: _HostJsonObject | None = None,
    timeout_s: float = 10.0,
) -> _HostHttpResult:
    """
    Send one management request to an OmniCraft server.

    :param base_url: OmniCraft server base URL, e.g.
        ``"https://example.databricksapps.com"``.
    :param method: HTTP method, e.g. ``"GET"`` or ``"POST"``.
    :param path: Request path beginning with ``/``, e.g.
        ``"/v1/hosts/host_abc"``.
    :param params: Optional query parameters, e.g. ``{"limit": 1000}``.
    :param json_body: Optional JSON body, e.g.
        ``{"type": "stop_session", "data": {}}``.
    :param timeout_s: Request timeout in seconds, e.g. ``2.0`` for a
        quick liveness probe. Defaults to ``10.0`` for management calls.
    :returns: Decoded HTTP result.
    """
    import httpx

    from omnicraft.chat import _remote_headers

    try:
        with httpx.Client(
            base_url=base_url,
            headers=_remote_headers(server_url=base_url),
            timeout=timeout_s,
        ) as client:
            resp = client.request(method, path, params=params, json=json_body)
    except (httpx.HTTPError, OSError) as exc:
        return _HostHttpResult(
            status_code=0,
            body=f"{type(exc).__name__}: {exc}",
        )
    body: _HostJsonObject | str
    try:
        decoded = resp.json()
    except ValueError:
        body = resp.text
    else:
        body = cast(_HostJsonObject, decoded) if isinstance(decoded, dict) else str(decoded)
    return _HostHttpResult(status_code=resp.status_code, body=body)


def _host_error_text(body: _HostJsonObject | str) -> str:
    """
    Extract a concise error string from an OmniCraft response body.

    :param body: Response body decoded by :func:`_host_http_json`.
    :returns: Human-readable error text.
    """
    if isinstance(body, str):
        return body[:400]
    detail = body.get("detail")
    if isinstance(detail, str):
        return detail
    error = body.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message
    return json.dumps(body)[:400]


def _daemon_session_request_params(
    *,
    connected_only: bool,
    after: str | None,
) -> dict[str, str | int]:
    """
    Build query parameters for one sessions page.

    :param connected_only: When ``True``, ask the server for connected
        sessions only.
    :param after: Optional cursor from the prior page, e.g.
        ``"conv_abc123"``.
    :returns: Query parameters for ``GET /v1/sessions``.
    """
    params: dict[str, str | int] = {
        "limit": 1000,
        "include_archived": "true",
    }
    if connected_only:
        params["connected"] = "true"
    if after is not None:
        params["after"] = after
    return params


def _decode_sessions_page(
    result: _HostHttpResult,
) -> _SessionsPageResult:
    """
    Decode one ``GET /v1/sessions`` response page.

    :param result: HTTP result returned by :func:`_host_http_json`.
    :returns: Decoded page result. ``error`` is ``None`` on success.
    """
    if result.status_code == 0:
        return _SessionsPageResult(
            sessions=[],
            last_id=None,
            has_more=False,
            error=f"listagem de sessões falhou: {_host_error_text(result.body)}",
        )
    if result.status_code >= 400:
        return _SessionsPageResult(
            sessions=[],
            last_id=None,
            has_more=False,
            error=(
                f"listagem de sessões falhou ({result.status_code}): "
                f"{_host_error_text(result.body)}"
            ),
        )
    if not isinstance(result.body, dict):
        return _SessionsPageResult(
            sessions=[],
            last_id=None,
            has_more=False,
            error="a listagem de sessões retornou uma resposta que não é objeto",
        )
    data = result.body.get("data")
    if not isinstance(data, list):
        return _SessionsPageResult(
            sessions=[],
            last_id=None,
            has_more=False,
            error="a listagem de sessões retornou um campo de dados malformado",
        )
    rows = [s for s in data if isinstance(s, dict)]
    last_id = result.body.get("last_id")
    has_more = result.body.get("has_more")
    return _SessionsPageResult(
        sessions=rows,
        last_id=last_id if isinstance(last_id, str) and last_id else None,
        has_more=has_more if isinstance(has_more, bool) else False,
        error=None,
    )


def _fetch_session_pages(
    *,
    base_url: str,
    connected_only: bool,
) -> _SessionPagesResult:
    """
    Fetch every available session page from a server.

    :param base_url: OmniCraft server base URL, e.g.
        ``"https://example.databricksapps.com"``.
    :param connected_only: When ``True``, ask the server for connected
        sessions only.
    :returns: Accumulated sessions result. ``error`` is ``None`` on success.
    """
    after: str | None = None
    sessions: list[_HostSessionRow] = []
    while True:
        page_result = _host_http_json(
            base_url=base_url,
            method="GET",
            path="/v1/sessions",
            params=_daemon_session_request_params(
                connected_only=connected_only,
                after=after,
            ),
        )
        page = _decode_sessions_page(page_result)
        if page.error is not None:
            return _SessionPagesResult(sessions=[], error=page.error)
        sessions.extend(page.sessions)
        if not page.has_more or page.last_id is None:
            return _SessionPagesResult(sessions=sessions, error=None)
        after = page.last_id


def _sessions_for_daemon(
    record: _HostDaemonRecord,
    *,
    connected_only: bool = False,
) -> _DaemonSessionsResult:
    """
    Fetch sessions owned by a daemon's host id.

    :param record: Daemon record whose sessions should be listed.
    :param connected_only: When ``True``, ask the server for connected
        sessions only.
    :returns: Sessions result. ``error`` is ``None`` on success.
    """
    base_url = _daemon_base_url(record)
    if base_url is None:
        return _DaemonSessionsResult(
            base_url=None,
            sessions=[],
            error="servidor OmniCraft local inacessível",
        )
    host_id = record.host_id or _load_existing_host_id()
    if not host_id:
        return _DaemonSessionsResult(
            base_url=base_url,
            sessions=[],
            error="id do host não disponível na config local",
        )
    pages = _fetch_session_pages(
        base_url=base_url,
        connected_only=connected_only,
    )
    if pages.error is not None:
        return _DaemonSessionsResult(base_url=base_url, sessions=[], error=pages.error)
    owned = [s for s in pages.sessions if s.get("host_id") == host_id]
    return _DaemonSessionsResult(base_url=base_url, sessions=owned, error=None)


def _runner_online_map(
    *,
    base_url: str,
    sessions: list[_HostSessionRow],
) -> dict[str, bool | None]:
    """
    Resolve live runner connectivity for sessions.

    :param base_url: OmniCraft server base URL, e.g.
        ``"https://example.databricksapps.com"``.
    :param sessions: Session rows containing ``runner_id`` values.
    :returns: Map of ``runner_id`` to ``True`` / ``False``. ``None``
        means the runner status could not be resolved.
    """
    from omnicraft.claude_native_bridge import url_component

    runner_ids = sorted(
        {
            runner_id
            for session in sessions
            if isinstance((runner_id := session.get("runner_id")), str) and runner_id
        }
    )
    statuses: dict[str, bool | None] = {}
    for runner_id in runner_ids:
        result = _host_http_json(
            base_url=base_url,
            method="GET",
            path=f"/v1/runners/{url_component(runner_id)}/status",
        )
        if result.status_code == 200 and isinstance(result.body, dict):
            online = result.body.get("online")
            statuses[runner_id] = online if isinstance(online, bool) else None
        else:
            statuses[runner_id] = None
    return statuses


def _annotate_sessions_with_runner_online(
    *,
    base_url: str,
    sessions: list[_HostSessionRow],
) -> list[_HostSessionRow]:
    """
    Add ``runner_online`` to session rows.

    :param base_url: OmniCraft server base URL, e.g.
        ``"https://example.databricksapps.com"``.
    :param sessions: Session rows returned by ``GET /v1/sessions``.
    :returns: Copies of the session rows with ``runner_online`` added.
    """
    statuses = _runner_online_map(base_url=base_url, sessions=sessions)
    annotated: list[_HostSessionRow] = []
    for session in sessions:
        runner_id = session.get("runner_id")
        runner_online = statuses.get(runner_id) if isinstance(runner_id, str) else None
        annotated.append({**session, "runner_online": runner_online})
    return annotated


def _base_daemon_status_payload(record: _HostDaemonRecord) -> _HostPayload:
    """
    Build daemon metadata for status output.

    :param record: Daemon registry record to inspect.
    :returns: JSON-serializable daemon metadata.
    """
    base_url = _daemon_base_url(record)
    host_id = record.host_id or _load_existing_host_id()
    return {
        "target": record.target,
        "mode": record.mode,
        "server_url": base_url,
        "pid": record.pid,
        "process": "online" if _pid_alive(record.pid) else "offline",
        "log_path": record.log_path,
        "host_id": host_id,
        "host_status": None,
        "sessions": [],
        "error": None,
    }


def _add_daemon_host_status(
    payload: _HostPayload,
) -> None:
    """
    Add host status or host status error to a daemon payload.

    :param payload: Payload from :func:`_base_daemon_status_payload`.
    """
    base_url = payload.get("server_url")
    host_id = payload.get("host_id")
    if not isinstance(base_url, str):
        payload["error"] = "servidor OmniCraft local inacessível"
        return
    if not isinstance(host_id, str) or not host_id:
        payload["error"] = "id do host não disponível na config local"
        return
    from omnicraft.claude_native_bridge import url_component

    host_result = _host_http_json(
        base_url=base_url,
        method="GET",
        path=f"/v1/hosts/{url_component(host_id)}",
    )
    if host_result.status_code == 200 and isinstance(host_result.body, dict):
        status = host_result.body.get("status")
        payload["host_status"] = status if isinstance(status, str) else None
    elif host_result.status_code == 0:
        payload["error"] = f"status do host falhou: {_host_error_text(host_result.body)}"
    elif host_result.status_code >= 400:
        payload["error"] = (
            f"status do host falhou ({host_result.status_code}): "
            f"{_host_error_text(host_result.body)}"
        )


def _add_daemon_sessions(
    payload: _HostPayload,
    record: _HostDaemonRecord,
    *,
    connected_sessions_only: bool,
) -> None:
    """
    Add owned sessions and runner connectivity to a daemon payload.

    :param payload: Payload from :func:`_base_daemon_status_payload`.
    :param record: Daemon registry record to inspect.
    :param connected_sessions_only: Whether session listing should use
        the server's connected filter.
    """
    sessions_result = _sessions_for_daemon(
        record,
        connected_only=connected_sessions_only,
    )
    sessions = sessions_result.sessions
    if sessions_result.base_url is not None and sessions:
        sessions = _annotate_sessions_with_runner_online(
            base_url=sessions_result.base_url,
            sessions=sessions,
        )
    payload["sessions"] = cast(_HostJsonValue, sessions)
    if sessions_result.error is not None and payload["error"] is None:
        payload["error"] = sessions_result.error


def _daemon_status_payload(
    record: _HostDaemonRecord,
    *,
    include_sessions: bool,
    connected_sessions_only: bool,
) -> _HostPayload:
    """
    Build a display payload for one daemon.

    :param record: Daemon registry record to inspect.
    :param include_sessions: Whether to include session rows.
    :param connected_sessions_only: Whether session listing should use
        the server's connected filter.
    :returns: JSON-serializable status payload.
    """
    payload = _base_daemon_status_payload(record)
    _add_daemon_host_status(payload)
    if include_sessions:
        _add_daemon_sessions(
            payload,
            record,
            connected_sessions_only=connected_sessions_only,
        )
    return payload


def _host_console() -> Console:
    """
    Build the Rich console used by host management output.

    :returns: A :class:`rich.console.Console` configured for predictable
        CLI rendering.
    """
    return Console(highlight=False)


def _host_table(title: str) -> Table:
    """
    Build a host CLI table with the shared style.

    :param title: Table title, e.g. ``"Host daemons"``.
    :returns: A :class:`rich.table.Table` ready for columns and rows.
    """
    return Table(
        title=title,
        box=box.SIMPLE_HEAVY,
        border_style="dim",
        header_style="bold cyan",
        show_edge=False,
    )


def _host_display_value(value: _HostJsonValue, *, missing: str = "-") -> str:
    """
    Convert optional payload values into display text.

    :param value: Payload value, e.g. ``None`` or ``"runner_abc"``.
    :param missing: Text to use when *value* is absent, e.g. ``"-"``.
    :returns: Display string.
    """
    if value is None:
        return missing
    text = str(value)
    return text if text else missing


def _host_shorten(text: _HostJsonValue, *, max_chars: int) -> str:
    """
    Shorten long daemon, session, and runner identifiers for terminal display.

    :param text: Value to shorten, e.g. ``"conv_abcdef123456"``.
    :param max_chars: Maximum display width, e.g. ``24``.
    :returns: The original text if it fits, otherwise a middle-truncated
        string.
    """
    value = _host_display_value(text)
    if len(value) <= max_chars:
        return value
    if max_chars <= 1:
        return value[:max_chars]
    head = max(1, (max_chars - 1) // 2)
    tail = max(1, max_chars - head - 1)
    return f"{value[:head]}…{value[-tail:]}"


def _host_truncate(text: _HostJsonValue, *, max_chars: int) -> str:
    """
    Truncate long text from the right for compact terminal display.

    :param text: Value to truncate, e.g. an OmniCraft error message.
    :param max_chars: Maximum display width, e.g. ``96``.
    :returns: The original text if it fits, otherwise a right-truncated
        string ending in an ellipsis.
    """
    value = _host_display_value(text)
    if len(value) <= max_chars:
        return value
    if max_chars <= 1:
        return value[:max_chars]
    return f"{value[: max_chars - 1]}…"


def _host_markup(text: _HostJsonValue, *, missing: str = "-") -> str:
    """
    Escape dynamic values before embedding them in Rich markup.

    :param text: Value to render, e.g. a session title containing ``"["``.
    :param missing: Text to use when *text* is absent, e.g. ``"-"``.
    :returns: Markup-safe display text.
    """
    from rich.markup import escape

    return escape(_host_display_value(text, missing=missing))


def _host_target_label(payload: _HostPayload, *, width: int) -> str:
    """
    Build a compact daemon target label.

    :param payload: Payload from :func:`_daemon_status_payload`.
    :param width: Maximum label width, e.g. ``48``.
    :returns: Compact target label for headers and error rows.
    """
    target = _host_display_value(payload.get("target"))
    server_url = payload.get("server_url")
    if target == _LOCAL_DAEMON_MARKER and server_url:
        target = f"local ({server_url})"
    return _host_shorten(target, max_chars=width)


def _host_status_style(value: _HostJsonValue) -> str:
    """
    Pick a Rich style for a daemon, host, or session status.

    :param value: Status value, e.g. ``"online"``, ``"idle"``, or
        ``"failed"``.
    :returns: Rich style name for the value.
    """
    status = _host_display_value(value).lower()
    if status in {"online", "connected", "running", "idle"}:
        return "green"
    if status in {"offline", "failed", "error", "unknown"}:
        return "red"
    return "yellow"


def _host_runner_state(session: _HostSessionRow) -> str:
    """
    Return a display state for the session's bound runner.

    :param session: Session row, e.g.
        ``{"runner_id": "runner_abc", "runner_online": True}``.
    :returns: ``"online"``, ``"offline"``, or ``"unknown"``.
    """
    runner_id = session.get("runner_id")
    if not isinstance(runner_id, str) or not runner_id:
        return "unknown"
    runner_online = session.get("runner_online")
    if runner_online is True:
        return "online"
    if runner_online is False:
        return "offline"
    return "unknown"


def _host_sessions_table_widths(
    *, console_width: int, sessions: list[_HostJsonValue]
) -> _HostSessionsTableWidths:
    """
    Compute compact sessions table widths for the available terminal space.

    :param console_width: Console width in cells, e.g. ``120``.
    :param sessions: Raw session payloads from status data.
    :returns: Column widths that prefer full IDs when they fit.
    """
    rows = [session for session in sessions if isinstance(session, dict)]
    full_session_id = max(
        [len("Session ID"), *[len(_host_display_value(row.get("id"))) for row in rows]]
    )
    full_runner_id = max(
        [len("Runner ID"), *[len(_host_display_value(row.get("runner_id"))) for row in rows]]
    )
    min_title = 12
    # Padding, separators, and the fixed State / Runner columns consume
    # space that is not represented by the three variable-width columns.
    table_chrome = 34
    full_ids_fit = console_width >= full_session_id + full_runner_id + min_title + table_chrome
    session_id = full_session_id if full_ids_fit else min(full_session_id, 18)
    runner_id = full_runner_id if full_ids_fit else min(full_runner_id, 20)
    title = max(min_title, min(console_width - session_id - runner_id - table_chrome, 60))
    workspace = 48 if console_width >= session_id + runner_id + title + table_chrome + 50 else None
    return _HostSessionsTableWidths(
        session_id=session_id,
        runner_id=runner_id,
        title=title,
        workspace=workspace,
    )


def _add_host_payload_sessions_table(console: Console, payload: _HostPayload) -> None:
    """
    Render one daemon's owned sessions as a compact table.

    :param console: Rich console returned by :func:`_host_console`.
    :param payload: Payload from :func:`_daemon_status_payload`.
    """
    raw_sessions = payload.get("sessions")
    sessions = raw_sessions if isinstance(raw_sessions, list) else []
    if not sessions:
        console.print("  [dim]Nenhuma sessão própria encontrada.[/dim]")
        return
    table = _host_table("Sessões")
    widths = _host_sessions_table_widths(console_width=console.width, sessions=sessions)
    table.add_column(
        "ID da sessão",
        style="bold",
        overflow="ellipsis",
        no_wrap=True,
        max_width=widths.session_id,
    )
    table.add_column("Estado", width=7, no_wrap=True)
    table.add_column("Runner", width=7, no_wrap=True)
    table.add_column(
        "ID do runner",
        overflow="ellipsis",
        no_wrap=True,
        max_width=widths.runner_id,
    )
    table.add_column(
        "Título",
        overflow="ellipsis",
        no_wrap=True,
        max_width=widths.title,
    )
    if widths.workspace is not None:
        table.add_column(
            "Workspace",
            overflow="ellipsis",
            no_wrap=True,
            max_width=widths.workspace,
        )
    for session in sessions:
        if not isinstance(session, dict):
            continue
        session_row = session
        status = _host_display_value(session_row.get("status"), missing="unknown")
        runner_state = _host_runner_state(session_row)
        row = [
            _host_shorten(session_row.get("id"), max_chars=widths.session_id),
            f"[{_host_status_style(status)}]{status}[/]",
            f"[{_host_status_style(runner_state)}]{runner_state}[/]",
            _host_shorten(session_row.get("runner_id"), max_chars=widths.runner_id),
            _host_truncate(
                session_row.get("title"),
                max_chars=widths.title,
            ),
        ]
        if widths.workspace is not None:
            row.append(_host_shorten(session_row.get("workspace"), max_chars=widths.workspace))
        table.add_row(*row)
    console.print(table)


def _echo_daemon_payloads(payloads: list[_HostPayload]) -> None:
    """
    Render host status as one block per daemon target.

    :param payloads: Payloads from :func:`_daemon_status_payload`.
    """
    console = _host_console()
    if not payloads:
        console.print("[dim]Nenhum daemon host encontrado.[/dim]")
        return
    for idx, payload in enumerate(payloads):
        if idx:
            console.print()
        target = _host_target_label(payload, width=max(24, min(console.width - 2, 96)))
        process = _host_display_value(payload.get("process"), missing="unknown")
        host_status = _host_display_value(payload.get("host_status"), missing="unknown")
        console.print(f"[bold cyan]{_host_markup(target)}[/bold cyan]")
        console.print(
            "  "
            f"mode={_host_markup(payload.get('mode'))}  "
            f"pid={_host_markup(payload.get('pid'))}  "
            f"process=[{_host_status_style(process)}]{process}[/]  "
            f"host=[{_host_status_style(host_status)}]{host_status}[/]"
        )
        server_text = _host_shorten(
            payload.get("server_url"),
            max_chars=max(24, console.width - 11),
        )
        console.print(f"  server={_host_markup(server_text)}")
        console.print(f"  host_id={_host_markup(payload.get('host_id'))}")
        if payload.get("log_path"):
            console.print(f"  log={_host_markup(payload.get('log_path'))}")
        if payload.get("error"):
            message = _host_truncate(
                payload.get("error"),
                max_chars=max(24, console.width - 10),
            )
            console.print(f"  [red]error={_host_markup(message)}[/red]")
        _add_host_payload_sessions_table(console, payload)


@host.command("status")
@click.option("--server", default=None, help="Inspeciona apenas este alvo de servidor.")
@click.option(
    "--all", "all_targets", is_flag=True, help="Inspeciona todos os alvos de daemon conhecidos."
)
@click.option("--json", "json_output", is_flag=True, help="Emite JSON.")
@click.pass_context
def host_status(
    ctx: click.Context,
    server: str | None,
    all_targets: bool,
    json_output: bool,
) -> None:
    """
    Inspeciona o status do daemon host, runner e sessões.

    :param ctx: Contexto do Click carregando opções de nível de grupo.
    :param server: Alvo de servidor opcional para inspecionar, ex.
        ``"https://example.databricksapps.com"``.
    :param all_targets: Se deve inspecionar todos os alvos de daemon conhecidos.
    :param json_output: Se deve emitir JSON legível por máquina.
    """
    if server is None:
        server = _host_group_option(ctx, "server")
    records = _selected_daemon_records(server=server, all_targets=all_targets, default_all=True)
    payloads = [
        _daemon_status_payload(
            record,
            include_sessions=True,
            connected_sessions_only=True,
        )
        for record in records
    ]
    if json_output:
        click.echo(json.dumps({"daemons": payloads}, indent=2, sort_keys=True))
        return
    _echo_daemon_payloads(payloads)


def _stop_session_on_server(
    *,
    base_url: str,
    session_id: str,
) -> None:
    """
    Stop one OmniCraft session via the server lifecycle event API.

    :param base_url: OmniCraft server base URL, e.g.
        ``"https://example.databricksapps.com"``.
    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :raises click.ClickException: If the server rejects the stop event.
    """
    from omnicraft.claude_native_bridge import url_component

    result = _host_http_json(
        base_url=base_url,
        method="POST",
        path=f"/v1/sessions/{url_component(session_id)}/events",
        json_body={"type": "stop_session", "data": {}},
    )
    if result.status_code == 0:
        raise click.ClickException(
            f"Falha ao parar a sessão {session_id!r}: {_host_error_text(result.body)}"
        )
    if result.status_code >= 400:
        raise click.ClickException(
            f"Falha ao parar a sessão {session_id!r} ({result.status_code}): "
            f"{_host_error_text(result.body)}"
        )


def _stop_daemon_sessions(
    record: _HostDaemonRecord,
    *,
    force: bool,
) -> int:
    """
    Stop sessions owned by a daemon before terminating it.

    :param record: Daemon record whose host-bound sessions should stop.
    :param force: Continue stopping remaining sessions after failures.
    :returns: Number of sessions successfully stopped.
    :raises click.ClickException: If session listing or stop fails and
        ``force`` is ``False``.
    """
    result = _sessions_for_daemon(record)
    if result.error is not None:
        if force:
            click.echo(f"{record.target}: pulando parada de sessão: {result.error}", err=True)
            return 0
        raise click.ClickException(f"{record.target}: {result.error}")
    if result.base_url is None:
        return 0
    stopped = 0
    for session in result.sessions:
        session_id = session.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue
        try:
            _stop_session_on_server(
                base_url=result.base_url,
                session_id=session_id,
            )
        except click.ClickException as exc:
            if not force:
                raise
            click.echo(str(exc), err=True)
            continue
        stopped += 1
    return stopped


def _terminate_daemon(record: _HostDaemonRecord, *, force: bool) -> None:
    """
    Terminate one local daemon process.

    :param record: Daemon record whose process should terminate.
    :param force: Send SIGKILL after the SIGTERM grace period.
    :raises click.ClickException: If the process stays alive.
    """
    if not _pid_alive(record.pid):
        _delete_daemon_record(record)
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(record.pid, signal.SIGTERM)
    deadline = time.monotonic() + _HOST_DAEMON_STOP_GRACE_S
    while time.monotonic() < deadline:
        if not _pid_alive(record.pid):
            _delete_daemon_record(record)
            return
        time.sleep(0.1)
    if force:
        with contextlib.suppress(ProcessLookupError):
            os.kill(record.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not _pid_alive(record.pid):
                _delete_daemon_record(record)
                return
            time.sleep(0.1)
    raise click.ClickException(
        f"O daemon {record.pid} para {record.target!r} não saiu; tente de novo com --force."
    )


@host.command("stop")
@click.option("--server", default=None, help="Para apenas este alvo de servidor.")
@click.option(
    "--all", "all_targets", is_flag=True, help="Para todos os alvos de daemon conhecidos."
)
@click.option(
    "--daemon-only",
    is_flag=True,
    help="Termina os processos de daemon sem primeiro parar as sessões.",
)
@click.option("--force", is_flag=True, help="Continua após falhas e usa SIGKILL se necessário.")
@click.pass_context
def host_stop(
    ctx: click.Context,
    server: str | None,
    all_targets: bool,
    daemon_only: bool,
    force: bool,
) -> None:
    """
    Para as sessões do daemon host, depois para os processos de daemon.

    :param ctx: Contexto do Click carregando opções de nível de grupo.
    :param server: Alvo de servidor opcional para parar, ex.
        ``"https://example.databricksapps.com"``.
    :param all_targets: Se deve parar todos os alvos de daemon conhecidos.
    :param daemon_only: Pula as chamadas de parada de sessão do servidor quando ``True``.
    :param force: Continua após falhas e usa SIGKILL se necessário.
    """
    if server is None:
        server = _host_group_option(ctx, "server")
    # No selector means "stop everything `host status` lists": status defaults
    # to all known daemons, so stop must too. Defaulting to the config/local
    # target instead would miss a live daemon registered under a different
    # target (e.g. a local daemon when config points at a remote server).
    records = _selected_daemon_records(server=server, all_targets=all_targets, default_all=True)
    if not records:
        click.echo("Nenhum daemon host correspondente encontrado.")
        return
    for record in records:
        stopped = 0
        if not daemon_only:
            stopped = _stop_daemon_sessions(record, force=force)
        _terminate_daemon(record, force=force)
        click.echo(f"Daemon {record.target} parado pid={record.pid}; sessions_stopped={stopped}.")


@host.command("stop-session")
@click.argument("session_ids", nargs=-1, required=True)
@click.option("--server", default=None, help="Servidor dono das sessões.")
@click.option("--force", is_flag=True, help="Continua após falhas de parada individuais.")
@click.pass_context
def host_stop_session(
    ctx: click.Context,
    session_ids: Sequence[str],
    server: str | None,
    force: bool,
) -> None:
    """
    Para sessões específicas sem parar um daemon.

    :param ctx: Contexto do Click carregando opções de nível de grupo.
    :param session_ids: Ids de sessão para parar, ex.
        ``["conv_abc123", "conv_def456"]``.
    :param server: URL do servidor OmniCraft dono das sessões, ex.
        ``"https://example.databricksapps.com"``. ``None`` recai
        na descoberta de config/local.
    :param force: Continua após falhas de parada individuais.
    """
    if server is None:
        server = _host_group_option(ctx, "server")
    resolved_server = _resolve_host_server(server)
    if resolved_server is None:
        resolved_server = local_server_url_if_healthy()
        if resolved_server is None:
            raise click.ClickException(
                "Nenhum servidor foi fornecido e nenhum servidor OmniCraft local está acessível."
            )
    for session_id in session_ids:
        try:
            _stop_session_on_server(
                base_url=resolved_server,
                session_id=session_id,
            )
        except click.ClickException:
            if not force:
                raise
            click.echo(f"Falha ao parar a sessão {session_id!r}.", err=True)
            continue
        click.echo(f"Sessão {session_id} parada.")


@cli.command(hidden=True)
def version() -> None:
    """Imprime a versão instalada do OmniCraft."""
    print(_format_version())


def _parse_config_settings(
    settings: tuple[str, ...],
    *,
    resolve_paths: bool = False,
) -> dict[str, str | bool]:
    """
    Parse and validate ``KEY=VALUE`` pairs from the ``config`` command.

    Raises :class:`click.ClickException` for malformed items or unknown keys.

    :param settings: Raw ``KEY=VALUE`` strings, e.g.
        ``("default_agent=examples/hello.yaml", "model=gpt-5.4-mini")``.
    :param resolve_paths: When ``True``, resolve relative ``default_agent``
        paths to absolute so the config works regardless of working directory.
        Set for ``--global`` writes; leave ``False`` for project-local writes
        where the path is intentionally relative to the project root.
    :returns: Validated mapping of config key → value, e.g.
        ``{"agent": "examples/hello.yaml", "model": "gpt-5.4-mini"}``.
    """
    parsed: dict[str, str | bool] = {}
    for item in settings:
        if "=" not in item:
            raise click.ClickException(
                f"Esperado KEY=VALUE, recebido: {item!r}. "
                "Exemplo: omnicraft config set --global default_agent=myagent.yaml"
            )
        key, _, value = item.partition("=")
        if key not in _GLOBAL_CONFIG_KEYS:
            raise click.ClickException(
                f"Chave de config {key!r} desconhecida. "
                f"Chaves suportadas: {', '.join(sorted(_GLOBAL_CONFIG_KEYS))}"
            )
        # Resolve ``default_agent`` to an absolute path so ``omnicraft`` works from
        # any working directory, not just the directory where config was set.
        if (
            resolve_paths
            and key == "default_agent"
            and not value.startswith(("http://", "https://"))
        ):
            value = str(Path(value).resolve())
        if key in _BOOLEAN_CONFIG_KEYS:
            parsed[key] = _parse_config_bool(key, value)
        else:
            parsed[key] = value
    return parsed


def _validate_unset_keys(unset_keys: tuple[str, ...]) -> list[str]:
    """
    Validate keys passed to ``--unset`` against ``_GLOBAL_CONFIG_KEYS``.

    Raises :class:`click.ClickException` for any unrecognised key.

    :param unset_keys: Keys to remove from global config, e.g.
        ``("server",)``.
    :returns: The same keys as a list, confirming they are all valid.
    """
    validated: list[str] = []
    for key in unset_keys:
        if key not in _GLOBAL_CONFIG_KEYS:
            raise click.ClickException(
                f"Chave de config {key!r} desconhecida. "
                f"Chaves suportadas: {', '.join(sorted(_GLOBAL_CONFIG_KEYS))}"
            )
        validated.append(key)
    return validated


def _print_config_defaults() -> None:
    """Print the effective CLI defaults (user + project-level).

    The ``KEY=VALUE`` defaults from ``~/.omnicraft/config.yaml`` (user) and
    ``.omnicraft/config.yaml`` in the cwd (project, takes precedence).
    Used by ``omnicraft config list``.

    :returns: None. Side effect: writes to stdout.
    """
    # Only the user-facing run defaults (the keys ``config set`` accepts).
    # Internal blocks (``providers``, ``host``, ``tui``) are omitted — the
    # ``providers`` block is shown in the credentials-by-harness section.
    global_cfg = {k: v for k, v in _load_global_config().items() if k in _GLOBAL_CONFIG_KEYS}
    local_cfg = {k: v for k, v in _load_local_config().items() if k in _GLOBAL_CONFIG_KEYS}
    if not global_cfg and not local_cfg:
        click.echo(
            "  (nenhum definido — `omnicraft config set key=value` para projeto,\n"
            "   ou `omnicraft config set --global key=value` para nível de usuário)"
        )
        return
    global_path = _effective_global_config_path()
    local_path = Path.cwd() / _LOCAL_CONFIG_RELPATH
    # When the cwd IS the home directory, the project-level path
    # (``cwd/.omnicraft/config.yaml``) resolves to the SAME file as the
    # user-level path (``~/.omnicraft/config.yaml``). Dedup on the resolved
    # absolute path so the one file is shown once, not twice under two
    # spellings. ``resolve()`` collapses ``~`` and symlinks for the compare.
    local_is_global = local_cfg and local_path.resolve() == global_path.resolve()
    if global_cfg:
        click.echo(f"  # {_display_config_path(global_path)}")
        for k, v in sorted(global_cfg.items()):
            click.echo(f"  {k}={v}")
    if local_cfg and not local_is_global:
        click.echo(f"  # {local_path}")
        for k, v in sorted(local_cfg.items()):
            click.echo(f"  {k}={v}")


class _ConfigGroup(click.Group):
    """``config`` group that nudges the pre-split flat form to the subcommands.

    Before the noun-verb split, ``config`` took a positional ``KEY=VALUE``
    plus ``--list`` / ``--unset`` / ``--global`` flags. Those now live under
    ``config set`` / ``config list`` / ``config unset``. Click's default
    error for the old form is opaque (``No such command 'x=y'`` / ``No such
    option: --list``), so this intercepts the legacy first token and raises
    a hint pointing at the new command instead.
    """

    @staticmethod
    def _legacy_hint(first: str) -> str | None:
        """Return a migration hint for a legacy first token, else ``None``.

        :param first: The first CLI token after ``config``, e.g.
            ``"--list"`` or ``"model=gpt-5.4-mini"``.
        :returns: A hint string for a recognized legacy form, else ``None``.
        """
        if first == "--list":
            return "`config --list` agora é `omnicraft config list`."
        if first == "--unset":
            return "`config --unset KEY` agora é `omnicraft config unset KEY`."
        if first == "--global":
            return (
                "`--global` agora vai no subcomando — "
                "`omnicraft config set --global KEY=VALUE` ou "
                "`omnicraft config unset --global KEY`."
            )
        if "=" in first and not first.startswith("-"):
            return f"definir padrões agora é `omnicraft config set {first}`."
        return None

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """Intercept the legacy flat form before normal group parsing.

        :param ctx: The click context.
        :param args: Raw argument tokens after ``config``.
        :returns: The remaining args from the base parser (for valid forms).
        :raises click.UsageError: When the first token is a legacy form, with
            a hint pointing at the new ``config set`` / ``list`` / ``unset``.
        """
        # Only the FIRST token is inspected: a known subcommand (set/list/
        # unset) parses normally — so ``config set default_agent=x`` is not
        # mistaken for the legacy ``config default_agent=x``.
        if args and args[0] not in self.commands:
            hint = self._legacy_hint(args[0])
            if hint is not None:
                raise click.UsageError(hint)
        return super().parse_args(ctx, args)


@cli.group("config", cls=_ConfigGroup)
def config_grp() -> None:
    """Obtém, define e visualiza os padrões e credenciais do OmniCraft.

    Os padrões (auto_open_conversation, default_agent, harness, model,
    server) são usados pelo ``omnicraft run``. A config de nível de projeto
    (``.omnicraft/config.yaml`` no cwd, como ``.git/config``) sobrescreve
    a config de nível de usuário (``~/.omnicraft/config.yaml``, como ``~/.gitconfig``).

    \b
    Subcomandos:
      list   Mostra os padrões efetivos + credenciais configuradas (por harness).
      set    Define um ou mais padrões (KEY=VALUE).
      unset  Remove um ou mais padrões.
    """


@config_grp.command("list")
def config_list() -> None:
    """Lista os padrões efetivos e as credenciais configuradas.

    Imprime os padrões (usuário + projeto), depois as credenciais de modelo
    configuradas agrupadas por harness com o padrão de cada harness marcado — a
    visão combinada de tudo que o ``omnicraft run`` vai usar (incluindo
    credenciais detectadas no ambiente).

    :returns: None.
    """
    click.echo("Padrões")
    _print_config_defaults()
    click.echo()
    _print_credentials_by_harness()


@config_grp.command("set")
@click.option(
    "--global",
    "is_global",
    is_flag=True,
    default=False,
    help="Escreve em ~/.omnicraft/config.yaml (nível de usuário) em vez da config do projeto.",
)
@click.argument("settings", nargs=-1, required=True, metavar="KEY=VALUE...")
def config_set(is_global: bool, settings: tuple[str, ...]) -> None:
    """Define um ou mais padrões do OmniCraft.

    Sem ``--global``, os pares são escritos em ``.omnicraft/config.yaml``
    no diretório atual (nível de projeto, como ``.git/config``); com
    ``--global`` em ``~/.omnicraft/config.yaml`` (nível de usuário, como
    ``~/.gitconfig``). Os valores de projeto têm precedência.

    Chaves suportadas: auto_open_conversation, default_agent, harness,
    model, server.

    :param is_global: Quando ``True``, escreve em ``~/.omnicraft/config.yaml``;
        quando ``False``, em ``.omnicraft/config.yaml`` no cwd.
    :param settings: Pares ``KEY=VALUE`` para definir, ex.
        ``("default_agent=examples/hello.yaml", "model=gpt-5.4-mini")``.

    \b
    Exemplos:
      omnicraft config set default_agent=examples/hello_world.yaml
      omnicraft config set --global server=https://<app>.databricksapps.com
    """
    if is_global:
        parsed = _parse_config_settings(settings, resolve_paths=True)
        _save_global_config(parsed, ())
        config_path: Path = _effective_global_config_path()
    else:
        parsed = _parse_config_settings(settings, resolve_paths=False)
        _save_local_config(parsed, ())
        config_path = Path.cwd() / _LOCAL_CONFIG_RELPATH
    click.echo(f"Definida(s) {len(parsed)} chave(s) em {config_path}")


@config_grp.command("unset")
@click.option(
    "--global",
    "is_global",
    is_flag=True,
    default=False,
    help="Remove de ~/.omnicraft/config.yaml (nível de usuário) em vez da config do projeto.",
)
@click.argument("keys", nargs=-1, required=True, metavar="KEY...")
def config_unset(is_global: bool, keys: tuple[str, ...]) -> None:
    """Remove um ou mais padrões do OmniCraft.

    :param is_global: Quando ``True``, remove de ``~/.omnicraft/config.yaml``;
        quando ``False``, de ``.omnicraft/config.yaml`` no cwd.
    :param keys: Chaves para remover, ex. ``("server", "model")``.
    """
    validated = _validate_unset_keys(keys)
    if is_global:
        _save_global_config({}, tuple(validated))
        config_path: Path = _effective_global_config_path()
    else:
        _save_local_config({}, tuple(validated))
        config_path = Path.cwd() / _LOCAL_CONFIG_RELPATH
    click.echo(f"Removida(s) {len(validated)} chave(s) de {config_path}")


# Node version hint shared by the preflight problem messages and surfaced
# to the user. The Node-based harness CLIs (Claude Code, Codex, Pi) bundle
# a copy of ``undici`` that calls ``worker_threads.markAsUncloneable`` — a
# Node API added in 22.10 that is absent from every 20.x release. On older
# Node it surfaces as the opaque
# ``TypeError: webidl.util.markAsUncloneable is not a function``.
_NODE_MIN_VERSION_HINT = "Node.js 22 LTS ou mais novo (uma API 22.10+ é necessária)"


def _node_version(node_path: str) -> str | None:
    """
    Return the ``node --version`` string (e.g. ``v20.12.2``) or ``None``.

    Used only to make the "too old" warning concrete; a failure to read the
    version is non-fatal — the caller still reports the underlying problem.

    :param node_path: Absolute path to the ``node`` binary, as resolved by
        :func:`shutil.which`.
    :returns: The trimmed version string, or ``None`` if ``node`` could not
        be invoked.
    """
    try:
        result = subprocess.run(
            [node_path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def _node_dependency_problem() -> str | None:
    """
    Return a one-line problem if Node is missing or too old, else ``None``.

    The Node-based harnesses (``claude-native``, ``codex``, ``pi``) shell
    out to CLIs that bundle ``undici``; that bundle calls
    ``worker_threads.markAsUncloneable`` (added in Node 22.10). We invoke
    ``node`` to probe for the symbol directly rather than parse
    ``node --version``, so the check tracks the actual capability across
    the 22.x/23.x version split and never goes stale against a hardcoded
    floor.

    :returns: A human-readable description suitable for a warning bullet,
        or ``None`` when Node is present and new enough. A flaky/timed-out
        probe also yields ``None`` — setup should not block on it.
    """
    node = shutil.which("node")
    if node is None:
        return f"node não encontrado — Claude, Codex e Pi precisam de {_NODE_MIN_VERSION_HINT}."
    # Probe the exact API the bundled undici calls. Exit 0 ⇒ capability
    # present; exit 1 ⇒ too old; we treat any other failure as inconclusive.
    probe = (
        "process.exit("
        "typeof require('node:worker_threads').markAsUncloneable === 'function' ? 0 : 1)"
    )
    try:
        result = subprocess.run(
            [node, "-e", probe],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return None
    version = _node_version(node)
    detected = f" (detectado {version})" if version else ""
    return (
        f"Node.js muito antigo{detected} — Claude, Codex e Pi precisam de "
        f"{_NODE_MIN_VERSION_HINT}."
    )


@contextlib.contextmanager
def _isolated_databricks_cfg() -> collections.abc.Generator[None, None, None]:
    """Run Databricks setup against a temp config containing only our three profiles.

    The temp file starts with just the canonical internal-beta profile
    sections (see ``DEFAULT_PROFILES``) seeded from the original when they
    exist, so there is exactly one section per workspace host and
    ``databricks auth token --host X`` never hits the "multiple profiles
    match" ambiguity error.

    The user's real config is never modified while this context is active.
    On normal exit the three sections are merged back into the original.
    On SIGTERM / SIGINT the temp file is removed and the original is left
    exactly as it was.  SIGKILL cannot be caught, but the original is
    always safe because we never touch it.

    Uses ``DATABRICKS_CONFIG_FILE`` so both subprocess CLI calls *and*
    the direct configparser writes in ``omnicraft.onboarding.setup``
    (via ``_databrickscfg_path()``) all operate on the temp file. Also
    strips every entry in ``CONFLICTING_ENV_VARS`` for the duration of
    the context so a stale Databricks credential env var (see that list)
    can't shadow ``--host`` inside ``databricks auth token``.
    """
    import configparser
    import signal
    import tempfile

    from omnicraft.onboarding.internal_beta import DEFAULT_PROFILES
    from omnicraft.onboarding.setup import CONFLICTING_ENV_VARS

    original_cfg = Path.home() / ".databrickscfg"
    saved_env: dict[str, str | None] = {
        "DATABRICKS_CONFIG_FILE": os.environ.get("DATABRICKS_CONFIG_FILE"),
    }
    for var in CONFLICTING_ENV_VARS:
        saved_env[var] = os.environ.pop(var, None)

    def _restore_env() -> None:
        for var, prev in saved_env.items():
            if prev is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = prev

    # Temp file contains only the canonical internal-beta profile sections
    # (see DEFAULT_PROFILES), seeded from the original when they already
    # exist. Everything else is excluded so there is exactly one
    # section per workspace host and `databricks auth token --host X`
    # never hits the "multiple profiles match" ambiguity error.
    orig_cfg = configparser.ConfigParser()
    if original_cfg.exists():
        orig_cfg.read(original_cfg)
    cfg = configparser.ConfigParser()
    for spec in DEFAULT_PROFILES:
        if orig_cfg.has_section(spec.name):
            cfg[spec.name] = dict(orig_cfg[spec.name])

    omnicraft_dir = Path.home() / ".omnicraft"
    omnicraft_dir.mkdir(exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix="databrickscfg-setup-",
        dir=omnicraft_dir,
        suffix=".tmp",
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            cfg.write(f)
    except Exception:
        os.unlink(tmp_name)
        raise
    tmp_path = Path(tmp_name)

    os.environ["DATABRICKS_CONFIG_FILE"] = tmp_name

    def _on_signal(signum: int, _frame: types.FrameType | None) -> None:
        tmp_path.unlink(missing_ok=True)
        _restore_env()
        # Restore the original handler before re-raising so signal chaining
        # (e.g. Click's Ctrl-C → Abort) is preserved rather than falling
        # back to SIG_DFL which would kill the process through the OS.
        signal.signal(signum, prev_sigterm if signum == signal.SIGTERM else prev_sigint)
        signal.raise_signal(signum)

    prev_sigterm = signal.signal(signal.SIGTERM, _on_signal)
    prev_sigint = signal.signal(signal.SIGINT, _on_signal)

    write_tmp: Path | None = None
    try:
        yield
        # Merge canonical sections written by setup back into the real cfg.
        tmp_cfg = configparser.ConfigParser()
        tmp_cfg.read(tmp_path)
        orig_cfg = configparser.ConfigParser()
        if original_cfg.exists():
            orig_cfg.read(original_cfg)
        for spec in DEFAULT_PROFILES:
            if tmp_cfg.has_section(spec.name):
                orig_cfg[spec.name] = dict(tmp_cfg[spec.name])
        write_tmp = original_cfg.with_suffix(".tmp")
        with write_tmp.open("w") as f:
            orig_cfg.write(f)
        write_tmp.replace(original_cfg)
        write_tmp = None
    finally:
        tmp_path.unlink(missing_ok=True)
        if write_tmp is not None:
            write_tmp.unlink(missing_ok=True)
        signal.signal(signal.SIGTERM, prev_sigterm)
        signal.signal(signal.SIGINT, prev_sigint)
        _restore_env()


def _run_configure_databricks() -> None:
    """
    Configure coding harnesses to use Databricks Unity AI Gateway.

    Shells out to ``ucode configure`` to authenticate workspaces and set
    up harnesses (Claude SDK, Codex, OpenAI Agents, Pi). After setup,
    OmniCraft reads ``~/.ucode/state.json`` to pick per-harness model
    defaults and base URLs.

    :returns: None.
    :raises click.ClickException: If ucode command resolution,
        configuration, or state verification fails.
    """
    ucode_command = find_ucode_command()
    # ucode only configures the model-serving gateway, so it gets the
    # gateway workspace(s) only — not the MCP-only profiles, which are
    # authenticated during profile onboarding and have no ucode role.
    workspace_urls = model_gateway_workspace_urls()
    click.echo("Rodando `ucode configure --workspaces ...`...")

    result = subprocess.run(
        build_ucode_configure_command(ucode_command, workspace_urls=workspace_urls),
        check=False,
    )
    if result.returncode != 0:
        raise click.ClickException(
            f"`ucode configure` saiu com o código {result.returncode}; "
            "veja a saída do comando acima para detalhes."
        )

    click.echo(
        "Configuração do ucode concluída. O OmniCraft usará state.json para configurar "
        "os harnesses."
    )


def _warn_missing_harness_dependencies() -> None:
    """
    Warn about external (non-Python) tools the coding harnesses need.

    Surfaces every missing/outdated dependency up front (when the user
    opens ``configure harnesses``) so a fresh machine learns about all of
    them at once, rather than discovering each at the moment a harness or
    wrapper needs it (Node when a harness CLI runs, tmux when ``omnicraft
    claude`` launches). This *warns* rather than aborts on purpose: the
    pure-Python ``openai-agents`` harness runs without either tool, so a
    hard failure would block a valid flow — but ``omnicraft claude`` /
    ``codex`` do need both, hence the prominent notice.

    :returns: None. Side effect: writes a yellow warning block to stderr
        via :mod:`omnicraft.inner.ui` when one or more dependencies are
        missing.
    """
    problems: list[str] = []
    node_problem = _node_dependency_problem()
    if node_problem is not None:
        problems.append(node_problem)
    if shutil.which("tmux") is None:
        problems.append(
            "tmux não encontrado — Claude/Codex nativos precisam de tmux "
            "(macOS: `brew install tmux`)."
        )
    if not problems:
        return
    ui.warn("Alguns harnesses precisam de ferramentas externas:")
    for problem in problems:
        ui.err_console.print(f"  • {problem}", style="omni.warning", markup=False)
    ui.err_console.print(
        "Você pode configurar credenciais agora; instale estas antes de lançar esses harnesses.",
        style="omni.warning",
        markup=False,
    )


def _print_credentials_by_harness() -> None:
    """Print configured model credentials grouped by harness (the ``config list`` view).

    Renders the effective config **merged with ambient detections** (a
    detected env key / CLI login shows as an ordinary credential, with no
    separate "detected vs configured" split) grouped under each harness
    family, with the per-family default marked — via
    :func:`render_provider_listing_by_harness`.

    :returns: None. Side effect: writes the listing to the onboarding
        console.
    """
    from omnicraft.onboarding.configure_models import render_provider_listing_by_harness
    from omnicraft.onboarding.detected import effective_config_with_detected
    from omnicraft.onboarding.provider_config import load_providers

    config = effective_config_with_detected(_load_effective_config())
    providers = load_providers(config)
    render_provider_listing_by_harness(config, providers)


def _existing_key_name_for_ref(  # type: ignore[explicit-any]  # config is a yaml-boundary mapping
    config: dict[str, Any],
    family: str,
    api_key_ref: str,
) -> str | None:
    """Return the name of a ``key`` provider on *family* using *api_key_ref*.

    Two API keys are "the same key" when they read the same secret source
    (the same ``env:`` / ``keychain:`` reference). The add flow uses this to
    update such a key in place rather than writing a second, identical entry —
    so re-adding a key you already have stays idempotent, while a key from a
    genuinely different source gets its own entry (the "keep both" behavior).

    :param config: The parsed global config mapping (``providers:`` block).
    :param family: The harness family the key serves, ``"anthropic"`` or
        ``"openai"``.
    :param api_key_ref: The secret reference to match, e.g.
        ``"env:ANTHROPIC_API_KEY"`` or ``"keychain:anthropic"``.
    :returns: The provider name whose *family* block references the same
        secret, e.g. ``"anthropic"``, or ``None`` when no such key exists.
    """
    from omnicraft.onboarding.provider_config import KEY_KIND, load_providers

    for name, entry in load_providers(config).items():
        if entry.kind != KEY_KIND:
            continue
        fam = entry.families.get(family)
        if fam is not None and fam.api_key_ref == api_key_ref:
            return name
    return None


def _unique_provider_name(  # type: ignore[explicit-any]  # config is a yaml-boundary mapping
    config: dict[str, Any],
    candidate: str,
) -> str:
    """Return *candidate*, suffixed numerically until it's a free provider name.

    Provider names key the ``providers:`` mapping, so a colliding name would
    overwrite an existing entry on deep-merge. When the add flow keeps a
    second credential (an API key from a new source for a vendor that already
    has one), this derives a fresh name — ``anthropic`` → ``anthropic-2`` →
    ``anthropic-3`` — so both coexist.

    :param config: The parsed global config mapping (``providers:`` block).
    :param candidate: The preferred name, e.g. ``"anthropic"``.
    :returns: *candidate* if unused, else the first free ``<candidate>-<n>``
        (``n`` starting at 2), e.g. ``"anthropic-2"``.
    """
    from omnicraft.onboarding.provider_config import load_providers

    existing = set(load_providers(config))
    if candidate not in existing:
        return candidate
    n = 2
    while f"{candidate}-{n}" in existing:
        n += 1
    return f"{candidate}-{n}"


def _resolve_key_provider_name(  # type: ignore[explicit-any]  # config is a yaml-boundary mapping
    config: dict[str, Any],
    family: str,
    candidate: str,
    api_key_ref: str,
) -> str:
    """Pick the entry name for an API key being added — update vs keep-both.

    Realizes the "allow multiple API keys, keep both if source differs"
    behavior: a key whose secret source (*api_key_ref*) matches an existing
    key on *family* reuses that entry's name (an in-place update of the same
    credential); a key from a new source takes a fresh, unique name so it
    coexists with the others.

    :param config: The parsed global config mapping (``providers:`` block).
    :param family: The harness family the key serves, ``"anthropic"`` or
        ``"openai"``.
    :param candidate: The preferred name (the vendor id for a preset, or the
        user-typed name for "Other provider"), e.g. ``"anthropic"``.
    :param api_key_ref: The key's secret reference, e.g.
        ``"env:ANTHROPIC_API_KEY"`` or ``"keychain:anthropic"``.
    :returns: The existing same-source entry's name (update in place), else a
        unique name derived from *candidate* (keep both), e.g.
        ``"anthropic-2"``.
    """
    same_source = _existing_key_name_for_ref(config, family, api_key_ref)
    if same_source is not None:
        return same_source
    return _unique_provider_name(config, candidate)


def _credential_source_hint(entry: ProviderEntry, family: str) -> str | None:
    """A short, non-secret descriptor of where a key's secret comes from.

    Used to disambiguate two API keys that would otherwise share a label
    (e.g. two "Anthropic API Key" rows): an ``env:`` ref renders as
    ``$VAR``, a ``keychain:`` ref as its stored name, an inline ``$VAR`` as
    itself. Only meaningful for credential kinds that carry an inline family
    block (``key`` / ``gateway`` / ``local``).

    :param entry: The parsed provider entry.
    :param family: The surface whose secret source to describe,
        ``"anthropic"``, ``"openai"``, or ``"pi"``.
    :returns: A display hint such as ``"$ANTHROPIC_API_KEY"`` or
        ``"anthropic-2"``, or ``None`` when the family has no resolvable
        source descriptor.
    """
    from omnicraft.onboarding.provider_config import (
        ANTHROPIC_FAMILY,
        OPENAI_FAMILY,
        PI_SURFACE,
    )

    raw = entry.families.get(family)
    if raw is None and family == PI_SURFACE:
        # The pi surface carries no family block of its own — pi consumes
        # the credential of whichever family it routes through (anthropic
        # preferred), so describe that family's source instead.
        for fam in (ANTHROPIC_FAMILY, OPENAI_FAMILY):
            raw = entry.families.get(fam)
            if raw is not None:
                break
    if raw is None:
        return None
    if raw.api_key_ref is not None:
        if raw.api_key_ref.startswith("env:"):
            return f"${raw.api_key_ref[len('env:') :]}"
        if raw.api_key_ref.startswith("keychain:"):
            return raw.api_key_ref[len("keychain:") :]
    if raw.api_key is not None and raw.api_key.startswith("$"):
        return raw.api_key
    return None


def _family_key_count(  # type: ignore[explicit-any]  # config is a yaml-boundary mapping
    config: dict[str, Any],
    family: str,
) -> int:
    """Count the ``key`` providers serving *family*.

    The ``($VAR)`` disambiguation hint is shown only when more than one API
    key serves a harness — a lone key needs no source qualifier.

    :param config: The parsed global config mapping (``providers:`` block).
    :param family: The harness family, ``"anthropic"`` or ``"openai"``.
    :returns: The number of ``kind: key`` providers serving *family*.
    """
    from omnicraft.onboarding.provider_config import (
        KEY_KIND,
        load_providers,
        provider_families,
    )

    return sum(
        1
        for entry in load_providers(config).values()
        if entry.kind == KEY_KIND and family in provider_families(entry)
    )


def _family_credential_label(  # type: ignore[explicit-any]  # config is a yaml-boundary mapping
    config: dict[str, Any],
    family: str,
    name: str,
    entry: ProviderEntry,
) -> str:
    """A credential label, qualified with its source when keys would collide.

    Wraps :func:`_credential_label`, appending the ``($VAR)`` source hint for
    a ``key`` provider when more than one API key serves *family* (so two
    "Anthropic API Key" rows read as distinct). Non-key kinds and the
    single-key case render the plain label.

    :param config: The parsed global config mapping (``providers:`` block).
    :param family: The harness family in context, ``"anthropic"`` /
        ``"openai"``.
    :param name: The provider id keyed under ``providers:``, e.g.
        ``"anthropic-2"``.
    :param entry: The parsed provider entry.
    :returns: A human label, e.g. ``"Anthropic API Key ($ANTHROPIC_API_KEY)"``
        when disambiguation applies, else ``"Anthropic API Key"``.
    """
    from omnicraft.onboarding.provider_config import KEY_KIND

    base = _credential_label(name, entry)
    if entry.kind != KEY_KIND or _family_key_count(config, family) <= 1:
        return base
    hint = _credential_source_hint(entry, family)
    return f"{base} ({hint})" if hint else base


def _configure_harness_add(family: str | None = None) -> str | None:
    """Run the interactive ``add a provider`` flow and persist the entry.

    Prompts for the provider kind (key / subscription / gateway /
    databricks), gathers the kind-specific fields, deep-merges the single
    entry under ``providers:`` (an add never rewrites siblings), and makes
    it the default for any family it serves that has **no** default yet
    (so a first provider just works; an existing default is left for the
    user to change by selecting it in the harness tree).

    :param family: When set (``"anthropic"`` / ``"openai"`` / ``"pi"``),
        the add menu is scoped to credentials that can drive that harness —
        the per-harness "Add a provider" path. ``None`` shows the full menu.
    :returns: A confirmation message for the caller to show as a transient
        status. Side effect: writes to ``~/.omnicraft/config.yaml`` and,
        for a pasted API key, the secret store.
    """
    from omnicraft.onboarding import secrets as secret_store
    from omnicraft.onboarding.ambient import detect_providers
    from omnicraft.onboarding.configure_models import (
        AddOption,
        add_menu_options,
        add_menu_options_for_family,
        build_bedrock_provider_entry,
        build_cli_config_provider_entry,
        build_databricks_provider_entry,
        build_gateway_provider_entry,
        build_key_provider_entry,
        build_subscription_provider_entry,
        default_base_url_for_family,
        family_for_key_provider,
        key_provider_endpoint,
        other_key_providers,
        provider_display_name,
    )
    from omnicraft.onboarding.interactive import console, prompt_text, select
    from omnicraft.onboarding.provider_config import (
        ANTHROPIC_FAMILY,
        BEDROCK_KIND,
        CHAT_WIRE_API,
        CLI_CONFIG_KIND,
        DATABRICKS_KIND,
        OPENAI_FAMILY,
        PI_SURFACE,
        RESPONSES_WIRE_API,
        SUBSCRIPTION_KIND,
        load_providers,
        provider_entry_settings,
        set_default_provider,
    )

    # The ucode agent that backs each harness surface's model serving. When the
    # user adds Databricks from a specific harness page, we configure ucode for
    # ONLY that harness (not all of claude/codex/pi) so ucode touches just the
    # one tool the user is wiring up.
    _FAMILY_UCODE_AGENT = {ANTHROPIC_FAMILY: "claude", OPENAI_FAMILY: "codex", PI_SURFACE: "pi"}

    # A flat, credential-aware menu: the user picks "OpenAI — API key" or
    # "Claude — subscription" directly (rather than a bare kind then
    # provider two-step). Each option carries the resolved kind and, for
    # the common cases, a preset provider/cli. When entered from a specific
    # harness, the menu is scoped to that harness's surface.
    options = add_menu_options_for_family(family) if family is not None else add_menu_options()
    # A custom provider defined by the user's own ~/.codex/config.toml
    # (e.g. isaac's Databricks AI Gateway) that is not currently configured
    # gets its own add option. This is the only way back after Remove —
    # removal dismisses the detection so it stops auto-adopting, and there
    # is nothing to type/paste here (the credential lives in that file).
    cli_config_dets: list[DetectedProvider] = []
    if family in (None, OPENAI_FAMILY):
        configured_names = set(load_providers(_load_global_config()))
        cli_config_dets = [
            d
            for d in detect_providers()
            if d.kind == CLI_CONFIG_KIND and d.name not in configured_names
        ]
    # Base options first, then one row per detected config provider — the
    # selection index maps back into cli_config_dets below.
    base_option_count = len(options)
    options = options + [
        AddOption(
            label=f"\N{GEAR}\N{VARIATION SELECTOR-16} {d.display_name or d.name} — "
            "from your Codex config",
            description=(
                f"Use the {str(d.model_provider)!r} provider your ~/.codex/config.toml "
                "defines and authenticates."
            ),
            kind=CLI_CONFIG_KIND,
        )
        for d in cli_config_dets
    ]
    choice = select(
        "What do you want to add?",
        [o.label for o in options],
        descriptions=[o.description for o in options],
        clear_on_exit=True,
    )
    if choice < 0:  # Esc — abort the add
        return None
    chosen = options[choice]
    kind = chosen.kind

    name: str
    # Any (not object): this entry is handed to provider_entry_settings /
    # set_default_provider, which type their config mappings as object;
    # _ConfigValue would trip dict invariance against those. Matches the
    # cli.py yaml-boundary convention.
    entry: dict[str, Any]  # type: ignore[explicit-any]

    if kind == CLI_CONFIG_KIND:
        # One detected-config row was appended per cli_config_dets entry, in
        # order, after the base options — map the selection back to its
        # detection. Nothing to prompt for: the provider definition AND its
        # credential live in ~/.codex/config.toml; the entry only pins it.
        det = cli_config_dets[choice - base_option_count]
        if det.model_provider is None:  # always set on cli-config detections
            raise click.ClickException("interno: detecção de cli-config sem model_provider")
        name = det.name
        entry = build_cli_config_provider_entry("codex", det.model_provider, det.display_name)
        # Re-adding is the user saying "I want this auto-detected credential
        # after all" — drop any standing dismissal so it behaves like an
        # ordinary detection again (e.g. re-adopts after a config self-heal).
        _clear_detection_dismissal(name)

    elif kind == "key":
        if chosen.provider is not None:
            provider = chosen.provider  # preset by the flat option (OpenAI/Anthropic/OpenRouter)
            # Preset: the preferred name is the provider id — but the final name
            # is resolved from the key's source below (update in place vs keep
            # both), so a second key for the same vendor doesn't overwrite the
            # first.
            candidate = provider
        else:
            # "Other provider — API key": pick from the remaining catalog,
            # shown by friendly display name. This is the one key case where a
            # custom name is useful (e.g. two configs for the same vendor), so
            # it's the only non-gateway path that still prompts for a name.
            others = other_key_providers()
            if not others:  # ponytail: every catalog key-provider is already a preset/configured
                click.echo("Nenhum outro provedor de chave de API para adicionar.")
                return None
            _other_choice = select(
                "Qual provedor?",
                [provider_display_name(p) for p in others],
                clear_on_exit=True,
            )
            if _other_choice < 0:  # Esc — abort the add
                return None
            provider = others[_other_choice]
            candidate = prompt_text("Nome para este provedor", default=provider)
        disp = provider_display_name(provider)
        family = family_for_key_provider(provider)
        # The entry name is resolved from the key's source (not just the
        # candidate): a key whose source matches an existing one updates it in
        # place, while a key from a new source takes a fresh name so both
        # coexist ("allow multiple API keys"). See _resolve_key_provider_name.
        config_now = _load_global_config()
        # Offer to reuse a detected env var for this provider rather than
        # forcing the user to re-paste a key they already have in the env.
        detected = {d.name: d for d in detect_providers()}
        api_key_ref: str
        if (
            provider in detected
            and detected[provider].kind == "key"
            and click.confirm(
                f"Detectado {detected[provider].source} no ambiente — usar?",
                default=True,
            )
        ):
            env_var = detected[provider].source.lstrip("$")  # e.g. "ANTHROPIC_API_KEY"
            api_key_ref = f"env:{env_var}"
            name = _resolve_key_provider_name(config_now, family, candidate, api_key_ref)
        else:
            # A pasted key is stored at keychain:<name>; resolve the name first
            # (an existing key in this same keychain slot is replaced in place,
            # otherwise we pick a free name) so we store under and reference the
            # final name.
            name = _resolve_key_provider_name(
                config_now, family, candidate, f"keychain:{candidate}"
            )
            pasted = prompt_text(f"Chave de API do {disp}", hide_input=True)
            secret_store.store_secret(name, pasted)
            api_key_ref = f"keychain:{name}"

        # Default model — free-form text entry. The bundled catalog lags new
        # releases (e.g. a brand-new claude-sonnet-4-6 won't be listed yet), so
        # a fixed picker would block the user from a model they can actually
        # use. Pre-fill the canonical default and let the user type ANY model
        # id. Blank → the default (or no pin when unknown). Always persisting
        # a pin keeps a later re-add from silently dropping ``models.default``.
        from omnicraft.onboarding.providers import default_chat_model

        catalog_default = default_chat_model(provider)
        # default=catalog_default (str | None): a known provider pre-fills its
        # default (blank-enter accepts it); an unknown provider has no default,
        # so the user types a model id. ``.strip() or None`` keeps an
        # all-whitespace entry from becoming a bogus pin.
        typed = prompt_text("Modelo padrão", default=catalog_default)
        default_model = typed.strip() or None

        # A third-party OpenAI-compatible vendor (OpenRouter, Groq, …) is
        # reached at its OWN base_url and speaks Chat Completions; openai /
        # anthropic use the canonical family endpoint (and openai keeps the
        # Responses default). Using the family default for a vendor sent its
        # traffic to api.openai.com — the reason an OpenRouter key failed.
        endpoint = key_provider_endpoint(provider)
        if endpoint is not None:
            base_url = endpoint.base_url
            key_wire_api: str | None = endpoint.wire_api
        else:
            base_url = default_base_url_for_family(family)
            key_wire_api = None
        entry = build_key_provider_entry(
            family=family,
            base_url=base_url,
            api_key_ref=api_key_ref,
            default_model=default_model,
            wire_api=key_wire_api,
        )

    elif kind == "subscription":
        cli_name = chosen.cli  # preset by the flat option (claude / codex)
        if cli_name is None:
            raise click.ClickException("interno: opção de assinatura sem um login de cli")
        from omnicraft.onboarding.harness_install import harness_install_spec, harness_login

        login_family = {agent: fam for fam, agent in _FAMILY_UCODE_AGENT.items()}.get(cli_name)
        if login_family is None:
            raise click.ClickException(
                f"interno: nenhuma família de login para o cli {cli_name!r}"
            )
        spec = harness_install_spec(login_family)
        disp = spec.display if spec is not None else cli_name
        # A harness has at most ONE subscription — the CLI's own login. If one
        # is already configured for this CLI (under any name, including an
        # ambient login adopted as e.g. ``claude``), adding another just
        # duplicates it — the ``claude`` + ``claude-subscription`` bug. Offer to
        # replace the existing one; declining aborts before we touch the login.
        existing_subs = [
            n
            for n, e in load_providers(_load_global_config()).items()
            if e.kind == SUBSCRIPTION_KIND and e.cli == cli_name
        ]
        if existing_subs:
            brand = _CLI_LOGIN_BRAND.get(cli_name, cli_name)
            replace = select(
                f"Uma assinatura {brand} já está configurada. Substituir?",
                ["Substituir", "Manter a atual"],
                default=0,
                clear_on_exit=True,
            )
            if replace != 0:  # "Keep the current one" or Esc — abort the add
                return None
        # Configure is the single place to sign in: drive the harness's own
        # login (a no-op if already logged in). Only record the subscription
        # once the CLI is actually authenticated — otherwise we'd persist a
        # phantom subscription that strands the user at the harness's own login
        # screen at run time (the exact bug this whole flow fixes).
        console.print(f"  [dim]Entrando em {disp} (o login dele vai abrir)…[/dim]")
        if not harness_login(login_family):
            return f"✗ Login do {disp} não concluído — assinatura não adicionada"
        # Login succeeded — drop the existing subscription(s) for this CLI so the
        # canonical entry is the only one left (clearing the old default lets the
        # new entry re-claim the family default below). Done AFTER login so a
        # failed login leaves the existing subscription intact.
        if existing_subs:
            block = _load_global_config().get("providers")
            if isinstance(block, dict):
                remaining = {k: v for k, v in block.items() if k not in existing_subs}
                _save_global_config({"providers": remaining})  # wholesale replace
        # Subscription name is derived from the CLI login — no prompt.
        name = f"{cli_name}-subscription"
        entry = build_subscription_provider_entry(cli_name)

    elif kind == "gateway":
        name = prompt_text("Nome para este gateway", default="gateway")
        base_url = prompt_text("base_url do gateway (compatível com OpenAI/Anthropic)")
        pasted = prompt_text("Chave de API do gateway", hide_input=True)
        secret_store.store_secret(name, pasted)
        # Which harness surfaces — one clear pick instead of two y/n prompts.
        # (These are *harness* surfaces: Codex/OpenAI → codex + openai-agents;
        # Claude/Anthropic → claude-sdk + native-claude.)
        surface_choice = select(
            "Quais harnesses este gateway pode dirigir?",
            [
                "Claude e Codex",
                "Só Codex / OpenAI (codex, openai-agents)",
                "Só Claude (claude-sdk, native-claude)",
            ],
            default=0,
            clear_on_exit=True,
        )
        if surface_choice < 0:  # Esc — abort the add
            return None
        families = (
            [OPENAI_FAMILY, ANTHROPIC_FAMILY]
            if surface_choice == 0
            else [OPENAI_FAMILY]
            if surface_choice == 1
            else [ANTHROPIC_FAMILY]
        )
        # Wire protocol for the OpenAI surface: OpenAI / LiteLLM speak the
        # Responses API; OpenRouter and many OSS-model gateways are
        # Chat-Completions-only. Picking wrong makes every turn fail (the
        # exact "OpenRouter doesn't work but LiteLLM does" symptom), so ask —
        # defaulting to Chat when the URL looks like OpenRouter.
        wire_api: str | None = None
        if OPENAI_FAMILY in families:
            wire_choice = select(
                "Protocolo de comunicação OpenAI para este gateway?",
                [
                    "Responses API (OpenAI, LiteLLM)",
                    "Chat Completions (OpenRouter, maioria dos gateways de modelo OSS)",
                ],
                default=1 if "openrouter" in base_url.lower() else 0,
                clear_on_exit=True,
            )
            if wire_choice < 0:  # Esc — abort the add
                return None
            wire_api = RESPONSES_WIRE_API if wire_choice == 0 else CHAT_WIRE_API
        # Default model per served surface. A gateway has NO catalog default,
        # so without a pin routing would fall back to a vendor model the
        # gateway can't serve. The OpenAI surface pre-fills a broadly-served
        # OSS default (moonshotai/kimi-k2.6, via the openrouter pin); the
        # user can type any gateway model id.
        from omnicraft.onboarding.providers import default_chat_model

        models: dict[str, str] = {}
        if OPENAI_FAMILY in families:
            models[OPENAI_FAMILY] = prompt_text(
                "Modelo padrão para a superfície Codex / OpenAI",
                default=default_chat_model("openrouter"),
            ).strip()
        if ANTHROPIC_FAMILY in families:
            models[ANTHROPIC_FAMILY] = prompt_text(
                "Modelo padrão para a superfície Claude (o id do modelo Claude do gateway)"
            ).strip()
        entry = build_gateway_provider_entry(
            base_url=base_url,
            api_key_ref=f"keychain:{name}",
            families=families,
            wire_api=wire_api,
            models=models,
        )

    elif kind == BEDROCK_KIND:
        # Bedrock drives the native Claude terminal in AWS Bedrock mode. It
        # authenticates from AWS_BEARER_TOKEN_BEDROCK in the env at launch
        # (Claude Code ignores apiKeyHelper once Bedrock mode is on), so offer
        # to reference an exported token, else store a pasted one in the keychain.
        name = prompt_text("Nome para este provedor Bedrock", default="bedrock")
        base_url = prompt_text(
            "base_url do Bedrock (endpoint de runtime regional, ou seu gateway "
            "compatível com Bedrock)",
            default="https://bedrock-runtime.us-east-1.amazonaws.com",
        )
        if os.environ.get("AWS_BEARER_TOKEN_BEDROCK") and click.confirm(
            "Detectado AWS_BEARER_TOKEN_BEDROCK no ambiente — usar?", default=True
        ):
            api_key_ref = "env:AWS_BEARER_TOKEN_BEDROCK"
        else:
            pasted = prompt_text("Chave de API do Amazon Bedrock (bearer token)", hide_input=True)
            secret_store.store_secret(name, pasted)
            api_key_ref = f"keychain:{name}"
        # Bedrock has no catalog default and Claude's own default model is
        # usually not enabled on a Bedrock account, so pin an explicit id.
        default_model = (
            prompt_text(
                "Modelo padrão (id de inference-profile do Bedrock, ex. "
                "us.anthropic.claude-opus-4-5-20251101-v1:0)"
            ).strip()
            or None
        )
        family = ANTHROPIC_FAMILY
        entry = build_bedrock_provider_entry(
            base_url=base_url,
            api_key_ref=api_key_ref,
            default_model=default_model,
        )

    else:  # databricks
        # Gate on the `databricks` extra: a `kind: databricks` provider mints
        # workspace OAuth tokens via databricks-sdk at runtime
        # (omnicraft/runtime/credentials/databricks.py), and the SDK is no
        # longer a default dependency. Abort before any side effect (the
        # `databricks auth login` browser flow, `ucode configure`) so the
        # user isn't signed into a workspace that routing then can't use.
        from omnicraft.onboarding.databricks_config import (
            DATABRICKS_EXTRA_INSTALL_HINT,
            databricks_sdk_installed,
        )

        if not databricks_sdk_installed():
            from rich.markup import escape as _rich_escape

            # The status renders through Text.from_markup, where the literal
            # `[databricks]` in the install command would parse as a tag.
            return (
                "✗ O roteamento Databricks precisa do extra databricks — "
                f"{_rich_escape(DATABRICKS_EXTRA_INSTALL_HINT)}"
            )

        # The intro + URL prompt render inline, exactly like every other add
        # flow (the add-menu picker already erased its own frame on exit via
        # `clear_on_exit`) — entering the Databricks option should NOT blank the
        # whole screen. The one clear we keep is *after* the subprocess (below):
        # `databricks auth login` + `ucode configure` print a lot, and the
        # in-place menu redraw we return to can only erase its own frame, so we
        # wipe that leftover output once the login finishes.
        # Ask only for the workspace URL — never a profile name. The flow
        # below authenticates that one workspace and runs `ucode configure`
        # against it, scoped to the harness the user drilled into. This is
        # the one place OmniCraft triggers a Databricks CLI / ucode login;
        # it never happens on a bare `run`, so a user who only wants their
        # own provider is never routed through Databricks unexpectedly.
        from omnicraft.onboarding.configure_models import family_label
        from omnicraft.onboarding.databricks_config import normalize_workspace_url
        from omnicraft.onboarding.interactive import clear_screen
        from omnicraft.onboarding.setup import login_databricks_workspace
        from omnicraft.onboarding.ucode_setup import (
            configure_ucode_for_workspace,
            ucode_workspace_exists,
        )

        _routed = family_label(family) if family is not None else "seus harnesses"
        console.print(
            f"  [dim]Roteia as chamadas de modelo de {_routed} pelo "
            "Databricks Unity AI Gateway deste workspace (via ucode), então o uso é "
            "governado e cobrado lá. Isto faz seu login no workspace e roda "
            "`ucode configure` para ele.[/dim]"
        )
        workspace_url = prompt_text(
            "URL do workspace Databricks (ex. https://example.cloud.databricks.com)"
        ).strip()
        if not workspace_url:  # blank — abort the add
            return None
        if not workspace_url.startswith(("http://", "https://")):
            workspace_url = f"https://{workspace_url}"
        # Reduce to scheme://host. Users paste the URL from a browser address
        # bar, whose `/browse?o=...` path breaks both the saved profile host
        # and `ucode configure` (the Databricks CLI keys OAuth tokens by host,
        # so a path-laden value yields "no access token").
        normalized_workspace_url = normalize_workspace_url(workspace_url)
        if normalized_workspace_url != workspace_url.rstrip("/"):
            console.print(
                f"  [dim]Usando {normalized_workspace_url} — o caminho extra da "
                "URL colada foi ignorado.[/dim]"
            )
        workspace_url = normalized_workspace_url

        # 1. Authenticate the workspace (returns the ~/.databrickscfg profile
        #    name) and 2. run `ucode configure` against it for model serving —
        #    scoped to the harness the user drilled into (or both when added
        #    from the un-scoped menu), so ucode configures only what's needed.
        if family is not None:
            ucode_agents = [_FAMILY_UCODE_AGENT[family]]
        else:
            ucode_agents = sorted(_FAMILY_UCODE_AGENT.values())
        profile = login_databricks_workspace(workspace_url, console=console)
        configure_ucode_for_workspace(workspace_url, agents=ucode_agents)
        # Fail loud if ucode didn't actually record state for the workspace —
        # otherwise routing would silently fall back and confuse the user.
        if not ucode_workspace_exists(workspace_url):
            raise click.ClickException(
                f"`ucode configure` terminou mas não registrou estado para {workspace_url}. "
                "Rode de novo e verifique a saída do ucode acima."
            )
        # Wipe the verbose login + ucode output so the menu we return to (with a
        # "✓ Added databricks" status) renders on a clean screen.
        clear_screen()
        # Databricks name is fixed — no prompt. The provider keys on the
        # profile; runtime resolves profile → workspace URL → ucode state.
        name = "databricks"
        entry = build_databricks_provider_entry(profile)

    from omnicraft.onboarding.configure_models import family_label
    from omnicraft.onboarding.provider_config import (
        provider_families,
        surface_default_provider,
    )

    # Persist the entry (deep-merge — doesn't disturb sibling entries).
    _save_global_config(
        provider_entry_settings(name, entry, make_default=False),
        deep_merge_keys=("providers",),
    )
    # Become the default for any surface it serves that has NO default yet,
    # so a first provider "just works". An existing default is left alone —
    # the user changes defaults by selecting a provider in the harness tree
    # (per-surface, so a shared provider can default one harness, not both).
    # The pi surface checks its *effective* default: a family default already
    # drives pi via the fallback, so claiming the explicit pi scope then
    # would silently re-route pi away from it.
    parsed = load_providers({"providers": {name: entry}})[name]
    # Databricks routing is configured in ucode PER HARNESS (we only ran
    # `ucode configure` for the surface the user drilled into), so it must only
    # become the default for THAT surface — defaulting the other harnesses too
    # would route them through a workspace ucode never configured for them.
    # Other kinds (a gateway serving both families with one base_url + key)
    # still default every surface they serve.
    if entry["kind"] == DATABRICKS_KIND and family is not None:
        default_families = [family]
    else:
        default_families = sorted(provider_families(parsed))
    became_default: list[str] = []
    for fam in default_families:
        cfg = _load_global_config()
        if surface_default_provider(cfg, fam) is not None:
            continue
        block = cfg.get("providers")
        if isinstance(block, dict):
            _save_global_config({"providers": set_default_provider(block, name, fam)})
            became_default.append(fam)
    if became_default:
        labels = " · ".join(family_label(f) for f in became_default)
        return f"✓ {name} adicionado — padrão para {labels}"
    return f"✓ {name} adicionado"


def _adopt_detected_providers() -> list[str]:
    """Persist ambient-detected providers into the config, returning new names.

    Opening ``configure harnesses`` adopts any detected credential (env key,
    CLI login, local Ollama) not already in ``providers:`` as a real,
    editable entry — so the tree shows one uniform provider list with no
    "detected vs configured" split. Writes the merged view (explicit +
    detected, with detected auto-defaulting per family) wholesale, and only
    when there is something new to adopt (idempotent on re-open).

    :returns: The names adopted this call, e.g. ``["anthropic", "codex"]``;
        empty when every detection is already configured.
    """
    from omnicraft.onboarding.detected import (
        effective_config_with_detected,
        providers_to_adopt,
    )

    config = _load_global_config()
    to_adopt = providers_to_adopt(config)
    if not to_adopt:
        return []
    merged = effective_config_with_detected(config)
    _save_global_config({"providers": merged["providers"]})  # wholesale replace
    return list(to_adopt)


def _promote_global_auth_to_provider() -> str | None:
    """Backfill a databricks providers entry from an existing global ``auth:`` block.

    Older ``omnicraft setup`` runs configured Databricks only via the top-level
    ``auth: {type: databricks}`` block — which ``configure harnesses`` does not
    read — so the readout showed no Databricks provider (and an ambient CLI
    login as the default) even though routing used Databricks. This promotes
    that block into a first-class ``kind: databricks`` providers entry the next
    time ``configure harnesses`` opens, so existing configs self-heal without
    re-running ``omnicraft setup``.

    Becomes the default only for families with no existing **provider** default —
    mirroring routing precedence (explicit provider default > ``auth:`` block),
    so an explicitly-chosen default is left untouched while a config that only
    ever had the ``auth:`` block gets Databricks as its default (matching what
    routing already does at runtime). Must run BEFORE
    :func:`_adopt_detected_providers` so Databricks claims the default ahead of
    an ambient CLI login (``auth:`` outranks ambient detection in routing too).

    :returns: ``"databricks"`` if a provider was backfilled, else ``None`` (no
        databricks ``auth:`` block, or a databricks provider already exists).
    """
    from omnicraft.onboarding.configure_models import build_databricks_provider_entry
    from omnicraft.onboarding.provider_config import (
        load_providers,
        provider_entry_settings,
        provider_families,
        set_default_provider,
        surface_default_provider,
    )

    config = _load_global_config()
    auth = config.get("auth")
    if not isinstance(auth, dict) or auth.get("type") != "databricks":
        return None
    profile = auth.get("profile")
    if not isinstance(profile, str) or not profile:
        return None
    name = "databricks"
    if name in load_providers(config):
        return None  # already a first-class provider — nothing to backfill

    entry = build_databricks_provider_entry(profile)
    _save_global_config(
        provider_entry_settings(name, entry, make_default=False),
        deep_merge_keys=("providers",),
    )
    parsed = load_providers({"providers": {name: entry}})[name]
    for fam in sorted(provider_families(parsed)):
        cfg = _load_global_config()
        # Effective check (matters for the pi surface): a default that
        # already drives the surface — explicitly or via pi's fallback —
        # outranks the legacy auth: block, exactly like routing does.
        if surface_default_provider(cfg, fam) is not None:
            continue  # respect an existing provider default (it outranks auth:)
        block = cfg.get("providers")
        if isinstance(block, dict):
            _save_global_config({"providers": set_default_provider(block, name, fam)})
    return name


def _compact_credential_label(det: DetectedProvider) -> str:
    """A short, brand-qualified label for an auto-configured credential.

    Unlike :func:`omnicraft.onboarding.configure_models.credential_label`
    (which renders every CLI login as a bare ``"Subscription"`` because a
    harness only ever has one), this names the *brand* behind a login —
    ``"Claude Subscription"`` / ``"ChatGPT Subscription"`` — so a single
    comma-joined callout listing several credentials at once stays unambiguous
    without a per-line source. API keys and local endpoints reuse the shared
    ``credential_label`` (``"Anthropic API Key"``, ``"Ollama"``).

    :param det: A credential found by
        :func:`omnicraft.onboarding.ambient.detect_providers`.
    :returns: A short human label, e.g. ``"Anthropic API Key"``,
        ``"Claude Subscription"``, or ``"ChatGPT Subscription"``.
    """
    from omnicraft.onboarding.ambient import SUBSCRIPTION_KIND
    from omnicraft.onboarding.configure_models import credential_label

    if det.kind == SUBSCRIPTION_KIND:
        # Fallback to the raw CLI name is unreachable for today's detections
        # (see _CLI_LOGIN_BRAND) but keeps an added CLI readable, not crashing.
        brand = _CLI_LOGIN_BRAND.get(det.name, det.name)
        return f"Assinatura {brand}"
    # A cli-config detection carries the provider's own display name
    # ("Databricks AI Gateway"); other kinds ignore the keyword.
    return credential_label(det.kind, det.name, display_name=det.display_name)


def _announce_auto_configured_credentials(adopted: list[str]) -> None:
    """Print the "found existing credentials → auto-configured" callout.

    Re-runs ambient detection to recover each adopted credential, then prints a
    single compact, dimmed line naming them inline (e.g. ``Anthropic API Key,
    Claude Subscription, ChatGPT Subscription``) — so a user who never ran an
    explicit setup sees, the first time we auto-configure, exactly which
    credentials omnicraft picked up (rather than silently inheriting them).
    Styled ``dim`` rather than the onboarding accent so it reads as a quiet
    notice, not a prominent header.

    :param adopted: Provider names just persisted by
        :func:`_adopt_detected_providers`, e.g. ``["anthropic", "codex"]``.
        A name with no matching live detection is skipped (defensive — the
        adopt set and the detection list come from the same detection pass, so
        in practice every name resolves).
    :returns: None. Side effect: writes the callout to the shared onboarding
        console (stdout). Prints nothing when no adopted name resolves to a
        live detection.
    """
    from omnicraft.onboarding.ambient import detect_providers
    from omnicraft.onboarding.interactive import console

    detected = {det.name: det for det in detect_providers()}
    labels = [_compact_credential_label(detected[name]) for name in adopted if name in detected]
    if not labels:
        return
    console.print(
        "\n[dim]Encontrei credenciais existentes na sua máquina, "
        f"configuradas automaticamente para o omnicraft: {', '.join(labels)}[/dim]"
    )


def _adopt_ambient_credentials(progress: RunnerStartupProgress | None = None) -> list[str]:
    """Self-heal config, adopt ambient credentials, and announce what was added.

    The shared front half of both a bare ``omnicraft run``'s first-run path
    (:func:`_resolve_first_run_plan`) and the ``configure harnesses`` picker
    (:func:`_run_configure_harnesses_interactive`): it (1) backfills a legacy
    databricks ``auth:`` block into a real provider, (2) adopts any
    ambient-detected credential (env API key, logged-in ``claude`` / ``codex``
    CLI, local Ollama) not already configured as an ordinary provider entry,
    and (3) prints a callout naming exactly the credentials it just
    auto-configured. Idempotent: a second open adopts nothing, so no callout
    prints.

    The callout is scoped to *machine* credentials — the ambient detections —
    not the databricks ``auth:`` backfill, which promotes an existing config
    block rather than something newly "found on your machine".

    :param progress: Optional spinner handle (from
        :func:`omnicraft._runner_startup.runner_startup_progress`) covering the
        detection step — slow on macOS, where Claude detection now shells out to
        ``claude auth status`` to read the Keychain. When supplied, it is
        ``finish()``-ed (the spinner cleared) right before the callout prints,
        so the "Found existing credentials…" line is not clobbered by the
        animating spinner. ``None`` (the ``run`` first-run path) means no
        spinner — behavior is unchanged.
    :returns: The provider names adopted this call, e.g. ``["anthropic"]``;
        empty when every detection was already configured.
    """
    _promote_global_auth_to_provider()
    adopted = _adopt_detected_providers()
    # Clear the search spinner (if any) before printing — the callout writes to
    # stdout while the spinner animates on stderr, and on a shared TTY the two
    # would otherwise overwrite each other.
    if progress is not None:
        progress.finish()
    if adopted:
        _announce_auto_configured_credentials(adopted)
    return adopted


@dataclass(frozen=True)
class _HarnessMenuRow:
    """One selectable row in a harness's provider-management menu (level 2).

    :param label: Display text, e.g. ``"🔑 anthropic   ✓ default"``.
    :param action: The action on Enter — ``"set_default"`` / ``"add"`` /
        ``"remove"`` / ``"back"``.
    :param provider: For ``set_default``, the provider name to default;
        ``None`` for the other actions.
    """

    label: str
    action: str
    provider: str | None = None


_SOFT_INSTALL_ABORT = "\x00soft-install-abort"


def _credential_label(name: str, entry: ProviderEntry) -> str:
    """A friendly, jargon-free label for a configured credential.

    A logged-in CLI reads as ``"Subscription"`` (within a harness there is only
    one, so the plan name adds no information); an API-key provider names the
    vendor and the credential type (``"Anthropic API Key"`` / ``"OpenAI API
    Key"``); Databricks as ``"Databricks (<profile>)"``; a gateway / local
    endpoint as its display name — so menus and summaries avoid raw provider
    ids and the word "provider".

    :param name: The provider id keyed under ``providers:``, e.g. ``"openai"``.
    :param entry: The parsed provider entry.
    :returns: A human label, e.g. ``"Anthropic API Key"`` or ``"Databricks (oss)"``.
    """
    from omnicraft.onboarding.configure_models import credential_label

    return credential_label(
        entry.kind, name, profile=entry.profile, display_name=entry.display_name
    )


def _harness_credential_rows(config: dict[str, Any], family: str) -> list[_HarnessMenuRow]:  # type: ignore[explicit-any]
    """Build the level-2 rows: each credential serving *family*, then ``+ Add``.

    Each credential row drills into level 3 (make default / remove). The
    current default is marked with a green ✓. ``+ Add a credential`` runs the
    add flow; ``← Back`` returns to the harness picker (as do Esc / ``q``).

    :param config: The parsed config mapping (``providers:`` block).
    :param family: The harness surface being managed.
    :returns: The ordered, all-selectable rows.
    """
    from omnicraft.onboarding.configure_models import kind_glyph
    from omnicraft.onboarding.provider_config import (
        load_providers,
        provider_families,
        surface_default_provider,
    )

    serving = [
        (name, entry)
        for name, entry in load_providers(config).items()
        if family in provider_families(entry)
    ]
    # The surface's effective default (for pi: explicit scope, else fallback)
    # so the ✓ always marks the credential the harness would actually use.
    default = surface_default_provider(config, family)
    rows: list[_HarnessMenuRow] = []
    for name, entry in serving:
        glyph = kind_glyph(entry.kind)
        cred = _family_credential_label(config, family, name, entry)
        # The current default renders bold-green with a ✓ so it stands out in
        # the list; the rest are plain. Provider names are markup-safe in
        # practice (same assumption select() already makes for every label).
        if default is not None and name == default.name:
            label = f"[bold green]{glyph} {cred}  ✓ default[/]"
        else:
            label = f"{glyph} {cred}"
        rows.append(_HarnessMenuRow(label, action="credential", provider=name))
    rows.append(_HarnessMenuRow("+ Add a credential", action="add"))
    rows.append(_HarnessMenuRow("← Back", action="back"))
    return rows


def _prompt_install_harness(family: str) -> bool:
    """Offer to install an uninstalled harness CLI; return whether to proceed.

    Shown when the user drills into a harness whose CLI isn't on PATH. Offers
    three choices: install it now (``npm install -g …``), go back, or print the
    command to run manually.

    :param family: The harness surface being configured (``"anthropic"`` /
        ``"openai"`` / ``"pi"``).
    :returns: ``True`` only when the CLI is installed afterward (user chose
        install and it succeeded), so the caller continues to credential
        configuration; ``False`` when the user declines, asks to run it
        themselves, the install fails, or they Esc — the caller returns to the
        harness picker.
    """
    from omnicraft.onboarding.configure_models import family_label
    from omnicraft.onboarding.harness_install import (
        harness_install_command,
        install_harness_cli,
    )
    from omnicraft.onboarding.interactive import console, select

    label = family_label(family)
    cmd = " ".join(harness_install_command(family))
    choice = select(
        f"O CLI do {label} não está instalado. Instalar agora?",
        [
            f"Sim — instalar ({cmd})",
            "Não — voltar aos harnesses",
            "Eu mesmo rodo (mostrar o comando)",
        ],
        descriptions=[
            f"Roda `{cmd}` (precisa de npm), depois continua para a configuração de credenciais.",
            "Volta ao seletor de harnesses sem instalar.",
            "Imprime o comando para você instalar por conta, depois volta.",
        ],
        default=0,
        clear_on_exit=True,
    )
    if choice == 0:
        console.print(f"  [dim]Instalando {label} — rodando `{cmd}`…[/dim]")
        if install_harness_cli(family):
            console.print(f"  [green]✓ {label} instalado[/green]")
            return True
        console.print(
            f"  [red]Falha na instalação.[/red] Rode manualmente e reabra: [bold]{cmd}[/bold]"
        )
        return False
    if choice == 2:  # run it yourself
        console.print(f"  Instale {label} com:\n    [bold]{cmd}[/bold]")
    return False


def _manage_harness_providers(family: str) -> None:
    """Run the level-2 loop for one harness: pick a credential or add one.

    Selecting a credential opens level 3 (make default / remove); ``+ Add``
    runs the add flow. Esc (TTY) / ``q`` (fallback) returns to the harness
    picker. The menu re-renders (cleared in place) after each action so the
    session stays on one tidy screen.

    :param family: The harness family being managed.
    :returns: None.
    """
    from omnicraft.onboarding.configure_models import family_label
    from omnicraft.onboarding.harness_install import harness_cli_installed
    from omnicraft.onboarding.interactive import select

    # If the harness CLI isn't installed, offer to install it before showing
    # the credential menu. Declining (or copy-the-command) returns to the
    # harness picker — there's nothing to configure for a harness you can't run.
    if not harness_cli_installed(family) and not _prompt_install_harness(family):
        return

    # Carry the prior action's confirmation as a transient status line so the
    # menu shows only the latest result — not an accumulating stack of "✓ …".
    status: str | None = None
    while True:
        rows = _harness_credential_rows(_load_global_config(), family)
        idx = select(
            f"{family_label(family)} — select or add a credential",
            [r.label for r in rows],
            clear_on_exit=True,
            status=status,
        )
        if idx < 0:  # Esc / q — back to the harness picker
            return
        row = rows[idx]
        if row.action == "back":
            return
        if row.action == "add":
            status = _configure_harness_add(family=family)
        elif row.action == "credential" and row.provider is not None:
            status = _manage_credential(row.provider, family)


def _prompt_install_cursor() -> str | None:
    """Offer to install the missing ``cursor`` extra; return a status line.

    Shown atop the Cursor drill-in when the optional-extra ``cursor-sdk`` is
    absent. Three-choice ``select`` like :func:`_prompt_install_antigravity` /
    :func:`_prompt_install_harness` (install now / set key anyway / show
    command), but does NOT gate key management on the SDK: the ``cursor:`` key
    is stored independently and is useful once the SDK lands, so declining falls
    through to the key menu (whereas ``_prompt_install_harness`` returns to the
    picker, since pi can't configure credentials without its CLI). Install is
    portable and index-free — see
    :func:`omnicraft.onboarding.cursor_auth.cursor_install_command`.

    :returns: Status string for the drill-in's transient status line, or
        ``None`` (set-key-anyway / Esc / printed-command, no actionable result).
    """
    from rich.markup import escape as _rich_escape

    from omnicraft.onboarding.cursor_auth import CURSOR_EXTRA, install_cursor_sdk
    from omnicraft.onboarding.extra_install import extra_install_display
    from omnicraft.onboarding.interactive import console, select

    cmd = extra_install_display(CURSOR_EXTRA)
    # ``select`` renders text through Rich markup; escape the literal
    # ``[cursor]`` so it renders verbatim.
    cmd_markup = _rich_escape(cmd)
    choice = select(
        "O SDK do Cursor (cursor-sdk) não está instalado. Instalar agora?",
        [
            f"Instalar agora ({cmd_markup})",
            "Definir a chave do Cursor mesmo assim",
            "Eu mesmo rodo (mostrar o comando)",
        ],
        descriptions=[
            f"Roda `{cmd_markup}`, depois continua.",
            "Pula a instalação — armazena a chave agora; o SDK pode ser adicionado depois.",
            "Imprime o comando para você instalar por conta, depois continua.",
        ],
        default=0,
        clear_on_exit=True,
    )
    if choice == 0:
        console.print(f"  [dim]Instalando o extra cursor — rodando `{cmd_markup}`…[/dim]")
        if install_cursor_sdk():
            console.print("  [green]✓ cursor-sdk instalado[/green]")
            return "✓ cursor-sdk instalado"
        console.print(
            f"  [red]Falha na instalação.[/red] Rode manualmente: [bold]{cmd_markup}[/bold]"
        )
        return "✗ Falha na instalação — defina a chave mesmo assim, ou instale na mão"
    if choice < 0:
        return _SOFT_INSTALL_ABORT
    if choice == 2:  # run it yourself
        console.print(f"  Instale o extra cursor com:\n    [bold]{cmd_markup}[/bold]")
        return None
    # choice == 1 (set key anyway): fall through to the key menu silently.
    return None


def _manage_cursor_harness() -> None:
    """Run the level-2 loop for Cursor: manage its ``CURSOR_API_KEY``.

    Cursor runs via the ``cursor-sdk`` package and authenticates against
    Cursor's own backend with a ``CURSOR_API_KEY`` — the SDK requires one (a
    ``cursor-agent login`` does not apply, and cursor has no provider/gateway
    family). So this manages exactly that credential: set / replace / remove an
    API key stored in the omnicraft secret store, mirroring how the other
    harnesses persist their api keys (the secret in the store, a
    ``keychain:``/``env:`` reference in ``~/.omnicraft/config.yaml``).

    When the optional ``cursor-sdk`` is missing, the drill-in first offers to
    install it (:func:`_prompt_install_cursor`). Unlike the CLI-backed harnesses
    (which gate on the CLI), declining still drops into the key menu — the
    ``cursor:`` key is independently storable. Mirrors Antigravity post-#322.

    :returns: None. Side effects: may install the ``cursor`` extra, and may
        write the ``cursor:`` block of ``~/.omnicraft/config.yaml`` and the
        secret store.
    """
    from omnicraft.onboarding import secrets as secret_store
    from omnicraft.onboarding.cursor_auth import (
        cursor_api_key_configured,
        cursor_api_key_ref,
        cursor_sdk_installed,
    )
    from omnicraft.onboarding.interactive import select

    # Offer the install once on entry (not per loop iteration) when the SDK is
    # absent; the result seeds the menu's status line. Declining falls through
    # to key management, since the key is SDK-independent.
    status: str | None = None
    if not cursor_sdk_installed():
        status = _prompt_install_cursor()
        if status == _SOFT_INSTALL_ABORT:
            return
    while True:
        config = _load_global_config()
        key_set = cursor_api_key_configured(config)

        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow(
                "Substituir chave de API (CURSOR_API_KEY)"
                if key_set
                else "Definir chave de API (CURSOR_API_KEY)",
                action="set_key",
            )
        ]
        if key_set:
            rows.append(_HarnessMenuRow("Remover chave de API", action="remove_key"))
        rows.append(_HarnessMenuRow("← Voltar", action="back"))

        header = (
            "Cursor — chave de API configurada" if key_set else "Cursor — sem chave de API ainda"
        )
        idx = select(header, [r.label for r in rows], clear_on_exit=True, status=status)
        if idx < 0:  # Esc / q
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "set_key":
            status = _set_cursor_api_key()
        elif action == "remove_key":
            ref = cursor_api_key_ref(config)
            # Only a keychain-stored secret is ours to delete; an ``env:`` ref
            # points at the user's own environment, so just drop the config.
            if ref is not None and ref.startswith("keychain:"):
                secret_store.delete_secret(ref[len("keychain:") :])
            _save_global_config({}, unset_keys=("cursor",))
            status = "✓ Chave de API do Cursor removida"


def _set_cursor_api_key() -> str | None:
    """Prompt for and store a Cursor ``CURSOR_API_KEY``; return a status line.

    Offers an existing ``CURSOR_API_KEY`` from the environment first (recorded
    as an ``env:`` reference, so the secret never enters the config or the
    secret store), else reads the key with a hidden prompt and stores it in the
    omnicraft secret store under ``keychain:cursor``. The ``crsr_`` prefix is
    validated with a soft warning so a wrong paste is caught without
    hard-blocking a future key format. The key value is never echoed.

    :returns: A confirmation string for the menu's transient status, or
        ``None`` when the user aborted (empty input / declined the warning).
    """
    from omnicraft.onboarding import secrets as secret_store
    from omnicraft.onboarding.cursor_auth import (
        CURSOR_SECRET_NAME,
        cursor_api_key_settings,
        looks_like_cursor_api_key,
    )
    from omnicraft.onboarding.interactive import prompt_text

    # Strip surrounding whitespace before validating/forwarding so a key
    # exported with a trailing newline (a common ``export $(…)`` mishap)
    # validates and resolves cleanly — matching the pasted-key branch's
    # ``.strip()`` below and the strip in ``resolve_secret``'s ``env:`` branch.
    raw_detected = os.environ.get("CURSOR_API_KEY")
    detected = raw_detected.strip() if raw_detected else None
    if detected and click.confirm("Detectada CURSOR_API_KEY no ambiente — usar?", default=True):
        if not looks_like_cursor_api_key(detected) and not click.confirm(
            "$CURSOR_API_KEY não começa com 'crsr_'. Usar mesmo assim?", default=False
        ):
            return None
        _save_global_config(cursor_api_key_settings("env:CURSOR_API_KEY"))
        return "✓ Chave de API do Cursor definida (de $CURSOR_API_KEY)"

    pasted = prompt_text("Chave de API do Cursor (CURSOR_API_KEY)", hide_input=True).strip()
    if not pasted:
        return None
    if not looks_like_cursor_api_key(pasted) and not click.confirm(
        "Isso não começa com 'crsr_'. Armazenar mesmo assim?", default=False
    ):
        return None
    secret_store.store_secret(CURSOR_SECRET_NAME, pasted)
    _save_global_config(cursor_api_key_settings(f"keychain:{CURSOR_SECRET_NAME}"))
    return "✓ Chave de API do Cursor armazenada"


def _prompt_install_antigravity() -> str | None:
    """Offer to install the missing ``antigravity`` extra; return a status line.

    Shown atop the Antigravity drill-in when the ``google-antigravity`` SDK is absent.
    Mirrors :func:`_prompt_install_harness` — a three-choice ``select`` (install now /
    set key anyway / print command) — but does NOT gate key management on the SDK:
    unlike pi (which can't be configured without its CLI), the ``antigravity:`` key is
    storable independently, so declining just falls through to the key menu. The
    install carries no index URL (see :func:`antigravity_install_command`); on failure
    it prints the command to run by hand.

    :returns: A status string for the drill-in's transient status (install result or
        printed-command note), or ``None`` on set-key-anyway / Esc.
    """
    from rich.markup import escape as _rich_escape

    from omnicraft.onboarding.antigravity_auth import ANTIGRAVITY_EXTRA, install_antigravity_sdk
    from omnicraft.onboarding.extra_install import extra_install_display
    from omnicraft.onboarding.interactive import console, select

    cmd = extra_install_display(ANTIGRAVITY_EXTRA)
    # ``select`` renders through Rich markup, so escape the literal ``[antigravity]``.
    cmd_markup = _rich_escape(cmd)
    choice = select(
        "O SDK do Antigravity (google-antigravity) não está instalado. Instalar agora?",
        [
            f"Instalar agora ({cmd_markup})",
            "Definir a chave do Gemini mesmo assim",
            "Eu mesmo rodo (mostrar o comando)",
        ],
        descriptions=[
            f"Roda `{cmd_markup}`, depois continua.",
            "Pula a instalação — armazena a chave agora; o SDK pode ser adicionado depois.",
            "Imprime o comando para você instalar por conta, depois continua.",
        ],
        default=0,
        clear_on_exit=True,
    )
    if choice == 0:
        console.print(f"  [dim]Instalando o extra antigravity — rodando `{cmd_markup}`…[/dim]")
        if install_antigravity_sdk():
            console.print("  [green]✓ google-antigravity instalado[/green]")
            return "✓ google-antigravity instalado"
        console.print(
            f"  [red]Falha na instalação.[/red] Rode manualmente: [bold]{cmd_markup}[/bold]"
        )
        return "✗ Falha na instalação — defina a chave mesmo assim, ou instale na mão"
    if choice < 0:
        return _SOFT_INSTALL_ABORT
    if choice == 2:
        console.print(f"  Instale o extra antigravity com:\n    [bold]{cmd_markup}[/bold]")
        return None
    # choice == 1 (set key anyway): fall through to the key menu silently.
    return None


def _manage_antigravity_harness() -> None:
    """Run the level-2 loop for Antigravity: set / replace / remove its Gemini key.

    Antigravity is Gemini-native (no provider family), so this manages just its
    API key — stored in the secret store, referenced from the ``antigravity:``
    config block — mirroring how the other harnesses persist api keys.

    When the optional ``google-antigravity`` SDK is missing, the drill-in first offers
    to install it (:func:`_prompt_install_antigravity`). Unlike the CLI-backed harnesses
    (whose drill-in *gates* on the CLI), declining here still drops into the key menu,
    since the ``antigravity:`` key is independently storable.

    :returns: None. Side effects: may install the ``antigravity`` extra, and may write
        the ``antigravity:`` config block and the secret store.
    """
    from omnicraft.onboarding import secrets as secret_store
    from omnicraft.onboarding.antigravity_auth import (
        ANTIGRAVITY_CONFIG_KEY,
        ANTIGRAVITY_SECRET_NAME,
        antigravity_api_key_configured,
        antigravity_api_key_ref,
        antigravity_sdk_installed,
    )
    from omnicraft.onboarding.interactive import select

    # Offer the install once on entry (not per loop iteration); the returned status
    # seeds the menu's transient status line.
    status: str | None = None
    if not antigravity_sdk_installed():
        status = _prompt_install_antigravity()
        if status == _SOFT_INSTALL_ABORT:
            return
    while True:
        config = _load_global_config()
        key_set = antigravity_api_key_configured(config)

        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow(
                "Substituir chave de API do Gemini"
                if key_set
                else "Definir chave de API do Gemini",
                action="set_key",
            )
        ]
        if key_set:
            rows.append(_HarnessMenuRow("Remover chave de API", action="remove_key"))
        rows.append(_HarnessMenuRow("← Voltar", action="back"))

        header = (
            "Antigravity — chave de API do Gemini configurada"
            if key_set
            else "Antigravity — sem chave de API do Gemini ainda"
        )
        idx = select(header, [r.label for r in rows], clear_on_exit=True, status=status)
        if idx < 0:  # Esc / q
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "set_key":
            status = _set_antigravity_api_key()
        elif action == "remove_key":
            ref = antigravity_api_key_ref(config)
            # Only the secret we own (``keychain:antigravity``) is ours to
            # delete: a hand-edited block may point at a shared ``keychain:<other>``
            # secret, and an ``env:`` ref names the user's own environment. In
            # both of those cases just drop the config block and leave the secret.
            if ref == f"keychain:{ANTIGRAVITY_SECRET_NAME}":
                secret_store.delete_secret(ANTIGRAVITY_SECRET_NAME)
            _save_global_config({}, unset_keys=(ANTIGRAVITY_CONFIG_KEY,))
            status = "✓ Chave de API do Gemini removida"


def _set_antigravity_api_key() -> str | None:
    """Prompt for and store a Gemini API key; return a status line.

    Offers an existing ``GEMINI_API_KEY`` / ``ANTIGRAVITY_API_KEY`` first
    (recorded as an ``env:`` ref, so the secret stays in the environment), else
    reads it with a hidden prompt and stores it under ``keychain:antigravity``.
    The key prefix (``AIza`` or ``AQ``) is checked softly (a wrong paste is
    caught but can be forced). The key is never echoed.

    :returns: A status string for the menu, or ``None`` if the user aborted.
    """
    from omnicraft.onboarding import secrets as secret_store
    from omnicraft.onboarding.antigravity_auth import (
        ANTIGRAVITY_API_KEY_PREFIX_HINT,
        ANTIGRAVITY_ENV_VARS,
        ANTIGRAVITY_SECRET_NAME,
        antigravity_api_key_settings,
        looks_like_gemini_api_key,
    )
    from omnicraft.onboarding.interactive import prompt_text

    detected_var = next((v for v in ANTIGRAVITY_ENV_VARS if os.environ.get(v)), None)
    if detected_var is not None and click.confirm(
        f"Detectada {detected_var} no ambiente — usar?", default=True
    ):
        detected = os.environ[detected_var]
        if not looks_like_gemini_api_key(detected) and not click.confirm(
            f"${detected_var} não começa com {ANTIGRAVITY_API_KEY_PREFIX_HINT}. Usar mesmo assim?",
            default=False,
        ):
            return None
        _save_global_config(antigravity_api_key_settings(f"env:{detected_var}"))
        return f"✓ Chave de API do Gemini definida (de ${detected_var})"

    pasted = prompt_text("Chave de API do Gemini (GEMINI_API_KEY)", hide_input=True).strip()
    if not pasted:
        return None
    if not looks_like_gemini_api_key(pasted) and not click.confirm(
        f"Isso não começa com {ANTIGRAVITY_API_KEY_PREFIX_HINT}. Armazenar mesmo assim?",
        default=False,
    ):
        return None
    secret_store.store_secret(ANTIGRAVITY_SECRET_NAME, pasted)
    _save_global_config(antigravity_api_key_settings(f"keychain:{ANTIGRAVITY_SECRET_NAME}"))
    return "✓ Chave de API do Gemini armazenada"


def _qwen_auth_configured() -> bool:
    """Best-effort check whether Qwen Code can authenticate non-interactively.

    Qwen has **no CLI login** — its ``auth`` subcommand was removed. For our
    ``qwen --acp`` executor, auth must come from one of:

    - API-key / provider env vars (the headless path): ``OPENAI_API_KEY``,
      ``BAILIAN_CODING_PLAN_API_KEY``, or ``OPENROUTER_API_KEY``; or
    - an auth type selected via the interactive ``/auth`` flow (API key or the
      Alibaba Cloud Coding Plan), persisted to ``~/.qwen/settings.json``.

    (Qwen OAuth was discontinued on 2026-04-15, so it is not an auth path here.)

    Best-effort: the env-var check is reliable; the on-disk check keys off
    ``settings.json`` fields whose schema is not contract-stable (see
    docs/QWEN_FOLLOWUPS.md). Returns ``False`` for a fresh install with no auth —
    the case that must NOT render as "signed in".

    :returns: ``True`` when auth is detectable, else ``False``.
    """
    from pathlib import Path

    if any(
        os.environ.get(v)
        for v in ("OPENAI_API_KEY", "BAILIAN_CODING_PLAN_API_KEY", "OPENROUTER_API_KEY")
    ):
        return True
    settings = Path.home() / ".qwen" / "settings.json"
    if settings.is_file():
        try:
            data = json.loads(settings.read_text())
        except (OSError, ValueError):
            return False
        if isinstance(data, dict):
            if data.get("selectedAuthType"):
                return True
            security = data.get("security")
            auth = security.get("auth") if isinstance(security, dict) else None
            if isinstance(auth, dict) and (
                auth.get("selectedType") or auth.get("selectedAuthType")
            ):
                return True
    return False


def _print_qwen_auth_help() -> None:
    """Print Qwen's authentication options (it has no ``qwen login``)."""
    from omnicraft.onboarding.interactive import console

    console.print(
        "\n  [bold]Autenticar o Qwen Code[/bold]:\n"
        "    • Interativo: rode [bold]qwen[/bold] e use [bold]/auth[/bold] "
        "(chave de API ou Alibaba Cloud Coding Plan)\n"
        "    • Headless / ACP: defina [bold]OPENAI_API_KEY[/bold] + "
        "[bold]OPENAI_BASE_URL[/bold] + [bold]OPENAI_MODEL[/bold]\n"
        "    • Coding Plan: [bold]BAILIAN_CODING_PLAN_API_KEY[/bold] + a "
        "base URL do Coding Plan\n"
        "    • OpenRouter: [bold]OPENROUTER_API_KEY[/bold] + "
        "OPENAI_BASE_URL=https://openrouter.ai/api/v1\n"
    )


def _launch_qwen_auth() -> str | None:
    """Launch the interactive ``qwen`` TUI so the user can run ``/auth``.

    The ``/auth`` flow (API key or Alibaba Cloud Coding Plan) is interactive, so
    this hands the terminal to ``qwen``; when the user exits, re-check auth.

    :returns: A status line for the menu reflecting the post-launch auth state.
    """
    from omnicraft.onboarding.harness_install import (
        QWEN_KEY,
        harness_cli_installed,
        harness_install_spec,
    )
    from omnicraft.onboarding.interactive import console

    if not harness_cli_installed(QWEN_KEY):
        return "✗ CLI qwen não encontrado"
    spec = harness_install_spec(QWEN_KEY)
    assert spec is not None
    console.print(
        "  [dim]Lançando o Qwen — digite [bold]/auth[/bold] para configurar a autenticação, "
        "depois saia (/quit) para voltar.[/dim]"
    )
    with contextlib.suppress(OSError, KeyboardInterrupt):
        subprocess.run([spec.binary], check=False)
    return "✓ autenticação detectada" if _qwen_auth_configured() else "Auth ainda não detectada"


def _manage_qwen_harness() -> None:
    """Run the level-2 loop for Qwen Code: install the CLI and guide auth setup.

    Qwen has **no CLI subscription login** — its ``auth`` subcommand was removed.
    Authentication is either OpenAI-compatible env vars (for the headless
    ``qwen --acp`` path) or the interactive ``/auth`` command (API key or
    Alibaba Cloud Coding Plan). So this drill-in installs the CLI when missing,
    reports best-effort auth status (:func:`_qwen_auth_configured`), and offers
    to launch ``qwen`` for ``/auth`` — it does **not** pretend to run a ``qwen
    login``
    (there isn't one). Storing/injecting an OpenAI-compatible key *through
    OmniCraft* is deferred (see docs/QWEN_FOLLOWUPS.md, Provider Injection).

    Like the CLI-backed harnesses, a missing CLI gates the drill-in — there's
    nothing to configure for a harness you can't run.

    :returns: None. Side effects: may ``npm install`` the qwen CLI and launch the
        interactive ``qwen`` TUI for ``/auth``.
    """
    from omnicraft.onboarding.harness_install import (
        QWEN_KEY,
        harness_cli_installed,
        harness_install_command,
        install_harness_cli,
    )
    from omnicraft.onboarding.interactive import console, select

    # Gate on the CLI. Offer to install it; declining (or copy-the-command)
    # returns to the harness picker.
    if not harness_cli_installed(QWEN_KEY):
        cmd = " ".join(harness_install_command(QWEN_KEY))
        choice = select(
            "O CLI do Qwen Code não está instalado. Instalar agora?",
            [
                f"Sim — instalar ({cmd})",
                "Não — voltar aos harnesses",
                "Eu mesmo rodo (mostrar o comando)",
            ],
            descriptions=[
                f"Roda `{cmd}` (precisa de npm), depois continua para a configuração de auth.",
                "Volta ao seletor de harnesses sem instalar.",
                "Imprime o comando para você instalar por conta, depois volta.",
            ],
            default=0,
            clear_on_exit=True,
        )
        if choice == 0:
            console.print(f"  [dim]Instalando o Qwen Code — rodando `{cmd}`…[/dim]")
            if install_harness_cli(QWEN_KEY):
                console.print("  [green]✓ Qwen Code instalado[/green]")
            else:
                console.print(
                    f"  [red]Falha na instalação.[/red] Rode manualmente e reabra: "
                    f"[bold]{cmd}[/bold]"
                )
                return
        else:
            if choice == 2:  # run it yourself
                console.print(f"  Instale o Qwen Code com:\n    [bold]{cmd}[/bold]")
            return

    # Carry the prior action's confirmation as a transient status line.
    status: str | None = None
    while True:
        configured = _qwen_auth_configured()
        header = (
            "Qwen Code — autenticação detectada"
            if configured
            else "Qwen Code — ainda não autenticado"
        )
        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow("Abrir o Qwen para rodar /auth", action="auth"),
            _HarnessMenuRow("Mostrar opções de auth", action="help"),
            _HarnessMenuRow("← Voltar", action="back"),
        ]
        idx = select(header, [r.label for r in rows], clear_on_exit=True, status=status)
        if idx < 0:  # Esc / q
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "auth":
            status = _launch_qwen_auth()
        elif action == "help":
            _print_qwen_auth_help()
            status = None


def _print_goose_auth_help() -> None:
    """Print Goose's configuration options (OmniCraft manages no Goose credential)."""
    from omnicraft.onboarding.interactive import console

    console.print(
        "\n  [bold]Configurar o Goose[/bold] (o OmniCraft não armazena credencial do Goose):\n"
        "    • Interativo: rode [bold]goose configure[/bold] para escolher um provedor "
        "e armazenar sua chave (keyring ou ~/.config/goose/config.yaml)\n"
        "    • Override por env: defina [bold]GOOSE_PROVIDER[/bold] + [bold]GOOSE_MODEL[/bold] "
        "(mais a chave do provedor, ex. ANTHROPIC_API_KEY / OPENAI_API_KEY)\n"
    )


def _launch_goose_configure() -> str | None:
    """Launch the interactive ``goose configure`` flow; return a status line.

    ``goose configure`` is interactive (pick a provider, enter its key), so this
    hands the terminal to ``goose``; when the user exits, re-read the configured
    provider. Mirrors :func:`_launch_qwen_auth`.

    :returns: A status line reflecting the post-configure provider state.
    """
    from omnicraft.onboarding.goose_auth import goose_config_summary
    from omnicraft.onboarding.harness_install import (
        GOOSE_KEY,
        harness_cli_installed,
        harness_install_spec,
    )
    from omnicraft.onboarding.interactive import console

    if not harness_cli_installed(GOOSE_KEY):
        return "✗ CLI goose não encontrado"
    spec = harness_install_spec(GOOSE_KEY)
    assert spec is not None
    console.print(
        "  [dim]Lançando [bold]goose configure[/bold] — escolha um provedor e "
        "digite sua chave, depois volte.[/dim]"
    )
    with contextlib.suppress(OSError, KeyboardInterrupt):
        subprocess.run([spec.binary, "configure"], check=False)
    summary = goose_config_summary()
    if summary.provider:
        model = f" ({summary.model})" if summary.model else ""
        return f"✓ provedor configurado: {summary.provider}{model}"
    return "Provedor ainda não detectado"


def _manage_goose_harness() -> None:
    """Run the level-2 loop for Goose: ensure the CLI, then guide ``goose configure``.

    Goose owns its own auth (keyring / ``~/.config/goose/config.yaml``) — OmniCraft
    stores no Goose credential — so, like the Qwen drill-in, this reports
    best-effort configuration status and offers to launch ``goose configure``; it
    does not store a key through OmniCraft. A missing CLI gates the drill-in
    (nothing to configure for a harness you can't run); Goose ships out-of-band
    (brew / curl, no npm package), so we show its install hint rather than
    auto-installing. Serves both ``goose-native`` (TUI) and the headless
    ``goose`` (ACP) harness — both launch the same ``goose`` binary and read the
    same config.

    :returns: None. Side effects: may launch the interactive ``goose configure``.
    """
    from omnicraft.onboarding.goose_auth import goose_config_summary
    from omnicraft.onboarding.harness_install import (
        GOOSE_KEY,
        harness_cli_installed,
        harness_install_spec,
    )
    from omnicraft.onboarding.interactive import console, select

    # Gate on the CLI. Goose installs out-of-band (no npm package), so we can't
    # auto-install — show the hint and return.
    if not harness_cli_installed(GOOSE_KEY):
        spec = harness_install_spec(GOOSE_KEY)
        hint = spec.install_hint if spec and spec.install_hint else "brew install block-goose-cli"
        console.print(
            f"  O CLI do Goose não está instalado. Instale com:\n    [bold]{hint}[/bold]\n"
            "  depois reabra este menu."
        )
        return

    status: str | None = None
    while True:
        summary = goose_config_summary()
        if summary.provider:
            model = f" · {summary.model}" if summary.model else ""
            header = f"Goose — provedor configurado: {summary.provider}{model}"
        else:
            header = "Goose — nenhum provedor configurado ainda"
        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow("Rodar goose configure", action="configure"),
            _HarnessMenuRow("Mostrar opções de configuração", action="help"),
            _HarnessMenuRow("← Voltar", action="back"),
        ]
        idx = select(header, [r.label for r in rows], clear_on_exit=True, status=status)
        if idx < 0:  # Esc / q
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "configure":
            status = _launch_goose_configure()
        elif action == "help":
            _print_goose_auth_help()
            status = None


def _print_acp_examples() -> None:
    """Print example ACP-agent commands (OmniCraft stores no credential)."""
    from omnicraft.onboarding.interactive import console

    console.print(
        "\n  [bold]Agentes ACP customizados[/bold] — conecte qualquer agente que fale o "
        "Agent Client Protocol ([underline]agentclientprotocol.com[/underline]).\n"
        "  O OmniCraft não armazena credencial — faça login em cada agente pelo "
        "seu próprio CLI primeiro.\n\n"
        "  Comandos de exemplo para colar:\n"
        "    • Gemini CLI     [bold]gemini --experimental-acp[/bold]\n"
        "    • Qwen Code      [bold]qwen --acp[/bold]\n"
        "    • Goose          [bold]goose acp[/bold]\n"
        "    • Claude Code    [bold]npx -y @zed-industries/claude-code-acp[/bold]\n"
    )


def _add_acp_agent() -> None:
    """Prompt for a new ACP agent and append it to the ``acp:`` config block.

    Reached straight from the "Add custom ACP agent" overview row (no
    intermediate menu). Prints the paste-ready examples first, then prompts for
    name / command / optional model.
    """
    from omnicraft.onboarding.acp_auth import (
        AcpAgentEntry,
        acp_agents,
        acp_agents_settings,
        slugify,
    )
    from omnicraft.onboarding.interactive import console, prompt_text

    _print_acp_examples()
    name = prompt_text("Nome do agente (ex. Gemini CLI)").strip()
    if not name:
        console.print("  [yellow]Nenhum nome digitado — nada adicionado.[/yellow]")
        return
    command = prompt_text("Comando para lançar (ex. gemini --experimental-acp)").strip()
    if not command:
        console.print("  [yellow]Nenhum comando digitado — nada adicionado.[/yellow]")
        return
    model = (prompt_text("Modelo (opcional — Enter para pular)", default="") or "").strip() or None

    entries = list(acp_agents())
    entries.append(AcpAgentEntry(slug=slugify(name), name=name, command=command, model=model))
    _save_global_config(acp_agents_settings(entries))
    console.print(f"  ✓ {name} adicionado")


def _manage_acp_agent(slug: str) -> None:
    """Per-agent drill-in for one configured ACP agent: remove it.

    Reached by selecting the agent's own row in the configure-harnesses overview.
    A single-shot menu (Remove / Back) — OmniCraft stores no credential, so there
    is nothing else to manage per agent yet.

    :param slug: The agent's slug (see :func:`omnicraft.onboarding.acp_auth.slugify`).
    """
    from omnicraft.onboarding.acp_auth import acp_agents, acp_agents_settings
    from omnicraft.onboarding.interactive import console, select

    agents = list(acp_agents())
    agent = next((a for a in agents if a.slug == slug), None)
    if agent is None:
        return
    suffix = f"  ·  {agent.model}" if agent.model else ""
    header = f"{agent.name} — {agent.command}{suffix}"
    rows: list[_HarnessMenuRow] = [
        _HarnessMenuRow("Remover este agente", action="remove"),
        _HarnessMenuRow("← Voltar", action="back"),
    ]
    idx = select(header, [r.label for r in rows], clear_on_exit=True)
    if idx < 0 or rows[idx].action == "back":
        return
    _save_global_config(acp_agents_settings([a for a in agents if a.slug != slug]))
    console.print(f"  ✓ {agent.name} removido")


def _manage_hermes_harness() -> None:
    """Run the level-2 loop for Hermes: ensure the CLI is installed.

    Hermes owns its own auth via ``hermes model`` (interactive provider/model
    picker) and is installed via a curl script from Nous Research — OmniCraft
    stores no Hermes credential. A missing CLI gates the drill-in; when
    installed, the drill-in offers to launch ``hermes model`` for provider
    configuration.

    :returns: None. Side effects: may launch ``hermes model``.
    """
    from omnicraft.onboarding.harness_install import (
        HERMES_KEY,
        harness_cli_installed,
        harness_install_spec,
    )
    from omnicraft.onboarding.interactive import console, select

    if not harness_cli_installed(HERMES_KEY):
        spec = harness_install_spec(HERMES_KEY)
        hint = (
            spec.install_hint
            if spec and spec.install_hint
            else "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
        )
        console.print(
            f"  O Hermes não está instalado. Instale com:\n    [bold]{hint}[/bold]\n"
            "  depois reabra este menu."
        )
        return

    status: str | None = None
    while True:
        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow("Rodar hermes model (configurar provedor)", action="model"),
            _HarnessMenuRow("← Voltar", action="back"),
        ]
        idx = select(
            "Hermes Agent",
            [r.label for r in rows],
            clear_on_exit=True,
            status=status,
        )
        if idx < 0:
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "model":
            import subprocess

            try:
                subprocess.run(["hermes", "model"], check=False)
                status = "✓ hermes model concluído"
            except FileNotFoundError:
                status = "✗ binário hermes não encontrado"


def _manage_kiro_harness() -> None:
    """Run the level-2 loop for Kiro: ensure the CLI is installed and signed in.

    Kiro owns its own auth via ``kiro-cli login`` (Builder ID / social login /
    Identity Center) and is installed via Kiro's curl installer — OmniCraft stores
    no Kiro credential. A missing CLI gates the drill-in; when installed, the
    drill-in offers to launch ``kiro-cli login`` to sign in. Mirrors
    :func:`_manage_hermes_harness`.

    :returns: None. Side effects: may launch ``kiro-cli login``.
    """
    from omnicraft.onboarding.harness_install import (
        KIRO_KEY,
        harness_cli_installed,
        harness_install_spec,
    )
    from omnicraft.onboarding.interactive import console, select

    if not harness_cli_installed(KIRO_KEY):
        spec = harness_install_spec(KIRO_KEY)
        hint = (
            spec.install_hint
            if spec and spec.install_hint
            else "curl -fsSL https://cli.kiro.dev/install | bash"
        )
        console.print(
            f"  O Kiro não está instalado. Instale com:\n    [bold]{hint}[/bold]\n"
            "  depois reabra este menu."
        )
        return

    status: str | None = None
    while True:
        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow("Rodar kiro-cli login (fazer login)", action="login"),
            _HarnessMenuRow("← Voltar", action="back"),
        ]
        idx = select(
            "Kiro",
            [r.label for r in rows],
            clear_on_exit=True,
            status=status,
        )
        if idx < 0:
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "login":
            import subprocess

            try:
                subprocess.run(["kiro-cli", "login"], check=False)
                status = "✓ kiro-cli login concluído"
            except FileNotFoundError:
                status = "✗ binário kiro-cli não encontrado"


def _print_kimi_auth_help() -> None:
    """Print Kimi Code's authentication options.

    Kimi authenticates against Moonshot AI's backend rather than an OmniCraft
    credential: ``kimi login`` (OAuth or a Moonshot API key) for the default
    provider, and ``kimi provider add`` to register any other provider (an
    OpenAI-compatible endpoint, a Databricks gateway, …) in
    ``~/.kimi/config.toml``. OmniCraft has no per-spawn provider override for
    upstream kimi, so all of this lives in the kimi CLI's own config —
    OmniCraft-side injection remains a deferred follow-up.
    """
    from omnicraft.onboarding.interactive import console

    console.print(
        "\n  [bold]Autenticar o Kimi Code[/bold] (o kimi gerencia sua própria config em "
        "~/.kimi/config.toml):\n"
        "    • Provedor padrão: rode [bold]kimi login[/bold] "
        "(OAuth da Moonshot, ou cole uma chave de API da Moonshot)\n"
        "    • Outros provedores: rode [bold]kimi provider add[/bold] "
        "(endpoint compatível com OpenAI, gateway, …), depois fixe esse id de modelo "
        "no spec do agente\n"
        "    • O OmniCraft não armazena credencial do kimi e não pode passar uma por "
        "spawn — configure uma vez no CLI do kimi\n"
    )


def _manage_kimi_harness() -> None:
    """Run the level-2 loop for Kimi Code: install the CLI and drive ``kimi login``.

    Unlike Qwen (which has no ``login`` subcommand), Kimi ships a real
    ``kimi login`` (Moonshot OAuth or API key) and ``kimi logout``, so this
    drill-in offers sign-in / sign-out directly. Kimi has no first-class
    "am I logged in?" probe (its install spec sets ``status_args=None``), so
    :func:`~omnicraft.onboarding.harness_install.harness_cli_logged_in` always
    reports ``False`` for it — meaning ``harness_login`` runs ``kimi login``
    every time it is asked (the interactive flow lets the user cancel if
    already authenticated) and its boolean return is not a reliable success
    signal. We therefore treat login / logout as best-effort side effects and
    report that the flow finished rather than asserting an auth state.

    Like the other CLI-backed harnesses, a missing CLI gates the drill-in —
    there is nothing to configure for a harness you can't run.

    :returns: None. Side effects: may install the kimi CLI and run
        ``kimi login`` / ``kimi logout`` in the foreground.
    """
    from omnicraft.onboarding.harness_install import (
        KIMI_KEY,
        harness_cli_installed,
        harness_install_spec,
        harness_login,
        harness_logout,
    )
    from omnicraft.onboarding.interactive import console, select

    # Gate on the CLI. Kimi ships a single binary via a curl installer (not
    # npm), so there's no in-process auto-install — name the command and let
    # the user run it, then re-open. Mirrors how ``harness_setup_hint`` treats
    # the other curl-installed CLI (cursor-agent).
    if not harness_cli_installed(KIMI_KEY):
        spec = harness_install_spec(KIMI_KEY)
        hint = (spec.install_hint if spec else None) or "see Kimi Code docs"
        console.print(
            "  O CLI do Kimi Code não está instalado. Instale com:\n"
            f"    [bold]{hint}[/bold]\n"
            "  depois reabra este menu para fazer login."
        )
        return

    # Carry the prior action's confirmation as a transient status line.
    status: str | None = None
    while True:
        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow("Entrar (kimi login)", action="login"),
            _HarnessMenuRow("Sair (kimi logout)", action="logout"),
            _HarnessMenuRow("Mostrar opções de auth", action="help"),
            _HarnessMenuRow("← Voltar", action="back"),
        ]
        idx = select(
            "Kimi Code — a autenticação é gerenciada pelo CLI do kimi",
            [r.label for r in rows],
            clear_on_exit=True,
            status=status,
        )
        if idx < 0:  # Esc / q
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "login":
            # ``kimi login`` runs in the foreground (OAuth / API-key prompt);
            # its boolean return is unreliable for kimi (no status probe), so
            # don't assert success — just confirm the flow finished.
            console.print("  [dim]Entrando no Kimi (o login dele vai abrir)…[/dim]")
            harness_login(KIMI_KEY)
            status = (
                "fluxo de login do kimi finalizado — o kimi armazena suas próprias credenciais"
            )
        elif action == "logout":
            console.print("  [dim]Saindo do Kimi…[/dim]")
            harness_logout(KIMI_KEY)
            status = "fluxo de logout do kimi finalizado"
        elif action == "help":
            _print_kimi_auth_help()
            status = None


def _prompt_install_copilot() -> str | None:
    """Offer to install the missing ``copilot`` extra; return a status line.

    Shown atop the Copilot drill-in when the optional-extra ``github-copilot-sdk``
    is absent. Three-choice ``select`` like :func:`_prompt_install_cursor` /
    :func:`_prompt_install_antigravity` (install now / set token anyway / show
    command), and like them does NOT gate token management on the SDK: the
    ``copilot:`` token is stored independently and is useful once the SDK lands,
    so declining falls through to the token menu. Install is portable and
    index-free — see
    :func:`omnicraft.onboarding.copilot_auth.copilot_install_command`.

    :returns: Status string for the drill-in's transient status line, or
        ``None`` (set-token-anyway / Esc / printed-command, no actionable result).
    """
    from rich.markup import escape as _rich_escape

    from omnicraft.onboarding.copilot_auth import COPILOT_EXTRA, install_copilot_sdk
    from omnicraft.onboarding.extra_install import extra_install_display
    from omnicraft.onboarding.interactive import console, select

    cmd = extra_install_display(COPILOT_EXTRA)
    # ``select`` renders text through Rich markup; escape the literal
    # ``[copilot]`` so it renders verbatim.
    cmd_markup = _rich_escape(cmd)
    choice = select(
        "O SDK do Copilot (github-copilot-sdk) não está instalado. Instalar agora?",
        [
            f"Instalar agora ({cmd_markup})",
            "Definir o token do GitHub mesmo assim",
            "Eu mesmo rodo (mostrar o comando)",
        ],
        descriptions=[
            f"Roda `{cmd_markup}`, depois continua.",
            "Pula a instalação — armazena o token agora; o SDK pode ser adicionado depois.",
            "Imprime o comando para você instalar por conta, depois continua.",
        ],
        default=0,
        clear_on_exit=True,
    )
    if choice == 0:
        console.print(f"  [dim]Instalando o extra copilot — rodando `{cmd_markup}`…[/dim]")
        if install_copilot_sdk():
            console.print("  [green]✓ github-copilot-sdk instalado[/green]")
            return "✓ github-copilot-sdk instalado"
        console.print(
            f"  [red]Falha na instalação.[/red] Rode manualmente: [bold]{cmd_markup}[/bold]"
        )
        return "✗ Falha na instalação — defina o token mesmo assim, ou instale na mão"
    if choice < 0:
        return _SOFT_INSTALL_ABORT
    if choice == 2:  # run it yourself
        console.print(f"  Instale o extra copilot com:\n    [bold]{cmd_markup}[/bold]")
        return None
    # choice == 1 (set token anyway): fall through to the token menu silently.
    return None


def _manage_copilot_harness() -> None:
    """Run the level-2 loop for Copilot: manage its GitHub token.

    Copilot runs via the ``github-copilot-sdk`` package and authenticates against
    GitHub's Copilot backend with a GitHub token — the SDK requires one and it
    has no provider/gateway family. So this manages exactly that credential:
    set / replace / remove a token stored in the omnicraft secret store, mirroring
    how cursor / antigravity persist theirs (the secret in the store, a
    ``keychain:``/``env:`` reference in ``~/.omnicraft/config.yaml``).

    When the optional ``github-copilot-sdk`` is missing, the drill-in first
    offers to install it (:func:`_prompt_install_copilot`). Unlike the CLI-backed
    harnesses (which gate on the CLI), declining still drops into the token
    menu — the ``copilot:`` token is independently storable. Mirrors cursor /
    antigravity.

    :returns: None. Side effects: may install the ``copilot`` extra, and may
        write the ``copilot:`` block of ``~/.omnicraft/config.yaml`` and the
        secret store.
    """
    from omnicraft.onboarding import secrets as secret_store
    from omnicraft.onboarding.copilot_auth import (
        COPILOT_CONFIG_KEY,
        COPILOT_SECRET_NAME,
        copilot_github_token_configured,
        copilot_github_token_ref,
        copilot_sdk_installed,
    )
    from omnicraft.onboarding.interactive import select

    # Offer the install once on entry (not per loop iteration) when the SDK is
    # absent; the result seeds the menu's status line. Declining falls through
    # to token management, since the token is SDK-independent.
    status: str | None = None
    if not copilot_sdk_installed():
        status = _prompt_install_copilot()
        if status == _SOFT_INSTALL_ABORT:
            return
    while True:
        config = _load_global_config()
        token_set = copilot_github_token_configured(config)

        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow(
                "Substituir token do GitHub" if token_set else "Definir token do GitHub",
                action="set_key",
            )
        ]
        if token_set:
            rows.append(_HarnessMenuRow("Remover token do GitHub", action="remove_key"))
        rows.append(_HarnessMenuRow("← Voltar", action="back"))

        header = (
            "Copilot — token do GitHub configurado"
            if token_set
            else "Copilot — sem token do GitHub ainda"
        )
        idx = select(header, [r.label for r in rows], clear_on_exit=True, status=status)
        if idx < 0:  # Esc / q
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "set_key":
            status = _set_copilot_github_token()
        elif action == "remove_key":
            ref = copilot_github_token_ref(config)
            # Only the secret we own (``keychain:copilot``) is ours to delete: a
            # hand-edited block may point at a shared ``keychain:<other>`` secret,
            # and an ``env:`` ref names the user's own environment. In both of
            # those cases just drop the config block and leave the secret.
            if ref == f"keychain:{COPILOT_SECRET_NAME}":
                secret_store.delete_secret(COPILOT_SECRET_NAME)
            _save_global_config({}, unset_keys=(COPILOT_CONFIG_KEY,))
            status = "✓ Token do GitHub do Copilot removido"


def _set_copilot_github_token() -> str | None:
    """Prompt for and store a Copilot GitHub token; return a status line.

    Offers an existing ``COPILOT_GITHUB_TOKEN`` / ``GH_TOKEN`` / ``GITHUB_TOKEN``
    first (recorded as an ``env:`` ref, so the secret stays in the environment),
    else reads it with a hidden prompt and stores it under ``keychain:copilot``.
    The token shape is checked softly (a classic ``ghp_`` PAT — which Copilot
    rejects — or a wrong paste is flagged but can be forced). The token is never
    echoed.

    :returns: A status string for the menu, or ``None`` if the user aborted.
    """
    from omnicraft.onboarding import secrets as secret_store
    from omnicraft.onboarding.copilot_auth import (
        COPILOT_SECRET_NAME,
        COPILOT_TOKEN_ENV_VARS,
        copilot_github_token_settings,
        looks_like_github_copilot_token,
    )
    from omnicraft.onboarding.interactive import prompt_text

    detected_var = next((v for v in COPILOT_TOKEN_ENV_VARS if os.environ.get(v)), None)
    if detected_var is not None and click.confirm(
        f"Detectado {detected_var} no ambiente — usar?", default=True
    ):
        detected = os.environ[detected_var]
        if not looks_like_github_copilot_token(detected) and not click.confirm(
            f"${detected_var} não parece um token do GitHub compatível com Copilot "
            "(github_pat_/gho_). Usar mesmo assim?",
            default=False,
        ):
            return None
        _save_global_config(copilot_github_token_settings(f"env:{detected_var}"))
        return f"✓ Token do GitHub do Copilot definido (de ${detected_var})"

    pasted = prompt_text("Token do GitHub com acesso ao Copilot", hide_input=True).strip()
    if not pasted:
        return None
    if not looks_like_github_copilot_token(pasted) and not click.confirm(
        "Isso não parece um token do GitHub compatível com Copilot (github_pat_/gho_). "
        "Armazenar mesmo assim?",
        default=False,
    ):
        return None
    secret_store.store_secret(COPILOT_SECRET_NAME, pasted)
    _save_global_config(copilot_github_token_settings(f"keychain:{COPILOT_SECRET_NAME}"))
    return "✓ Token do GitHub do Copilot armazenado"


def _manage_credential(provider: str, family: str) -> str | None:
    """Run the level-3 loop for one credential: make default / remove.

    Opened by selecting a credential at level 2. Offers ``Make default`` (only
    when it is not already this harness's default), ``Remove``, and ``← Back``.
    Make-default / remove return to level 2 with a confirmation; ``← Back`` /
    Esc / ``q`` return with no change.

    :param provider: The provider id of the chosen credential, e.g. ``"openai"``.
    :param family: The harness surface in context, ``"anthropic"`` /
        ``"openai"`` / ``"pi"``.
    :returns: A confirmation string to show as a transient status at level 2,
        or ``None`` when nothing changed.
    """
    from omnicraft.onboarding.configure_models import family_label
    from omnicraft.onboarding.interactive import select
    from omnicraft.onboarding.provider_config import (
        DATABRICKS_KIND,
        SUBSCRIPTION_KIND,
        load_providers,
        surface_default_provider,
    )

    config = _load_global_config()
    entry = load_providers(config).get(provider)
    if entry is None:
        return None
    label = _family_credential_label(config, family, provider, entry)
    rows: list[_HarnessMenuRow] = []
    # "Make default" is offered unless this credential is already the
    # surface's *effective* default (matching the ✓ on the level-2 row) —
    # for pi that covers the fallback-driven default too, where offering
    # "make default" would be a confusing no-op.
    default = surface_default_provider(config, family)
    if default is None or default.name != provider:
        rows.append(
            _HarnessMenuRow(
                f"Tornar padrão para {family_label(family)}",
                action="set_default",
                provider=provider,
            )
        )
    rows.append(_HarnessMenuRow("Remover", action="remove", provider=provider))
    rows.append(_HarnessMenuRow("← Voltar", action="back"))

    idx = select(label, [r.label for r in rows], clear_on_exit=True)
    if idx < 0:  # Esc / q — back to the credential list, no change
        return None
    row = rows[idx]
    if row.action == "back":
        return None
    if row.action == "set_default":
        return _set_harness_default(provider, family)
    # A subscription's credential lives in the harness CLI's own auth file, not
    # our config — so removing it means signing out of that CLI (otherwise the
    # login persists and ambient detection re-adopts it on the next open).
    if entry.kind == SUBSCRIPTION_KIND:
        return _remove_subscription(provider, family)
    # A databricks provider was wired by `ucode configure`, which edits
    # harness configs outside ~/.omnicraft/config.yaml — so removing it
    # also cleans those edits up (otherwise codex keeps routing through
    # the workspace gateway).
    if entry.kind == DATABRICKS_KIND:
        return _remove_databricks_provider(provider)
    return _remove_credential(provider)


def _remove_subscription(provider: str, family: str) -> str | None:
    """Sign out of the harness CLI and remove the subscription credential.

    Unlike a key/gateway provider (whose credential is ours to drop), a
    subscription is backed by the harness CLI's own login file
    (``~/.codex/auth.json`` / ``~/.claude/.credentials.json``). Deleting only
    our entry would leave that login in place — so it would still drive the
    standalone CLI, and ambient detection would re-adopt the subscription on the
    next ``configure`` open. So "remove" here runs the harness's own logout
    (``codex logout`` / ``claude auth logout``) and then drops our entry. Guarded
    by an explicit confirm (default No) because it signs the user out of the
    standalone CLI too. (To merely stop *using* a subscription while staying
    logged in, the user makes another provider the default instead.)

    :param provider: The subscription provider id, e.g. ``"codex-subscription"``.
    :param family: The harness family, ``"anthropic"`` (Claude) / ``"openai"``
        (Codex).
    :returns: A confirmation message for the level-2 status line, or ``None``
        when the user declined (nothing changed). Side effects: runs the
        harness logout command and writes ``~/.omnicraft/config.yaml``.
    """
    from omnicraft.onboarding.harness_install import harness_install_spec, harness_logout
    from omnicraft.onboarding.interactive import select

    spec = harness_install_spec(family)
    disp = spec.display if spec is not None else family
    logout_cmd = (
        f"{spec.binary} {' '.join(spec.logout_args)}"
        if spec is not None and spec.logout_args is not None
        else "logout"
    )
    choice = select(
        f"Remover a assinatura {disp}?",
        [f"Sim — sair do {disp} e remover", "Não — manter"],
        descriptions=[
            f"Roda `{logout_cmd}`, deslogando você do CLI standalone do {disp} "
            "também, depois remove aqui.",
            f"Deixa a assinatura e seu login do {disp} intactos.",
        ],
        default=1,  # default to the non-destructive choice
        clear_on_exit=True,
    )
    if choice != 0:
        return None
    signed_out = harness_logout(family)
    # Drop our entry regardless — the user asked to remove it. If logout failed
    # we say so, since the standalone login may persist (and be re-detected).
    _remove_credential(provider)
    if signed_out:
        return f"✓ Deslogado do {disp} e removido"
    return (
        f"✓ Assinatura {disp} removida — nota: `{logout_cmd}` não concluiu, "
        f"então você pode ainda estar logado no CLI do {disp}"
    )


def _remove_databricks_provider(provider: str) -> str:
    """Remove a databricks provider and clean up ucode's harness wiring.

    A ``kind: databricks`` provider was wired by running ``ucode configure``
    (the add flow), which writes harness configs *outside*
    ``~/.omnicraft/config.yaml`` — most damagingly, for Codex < 0.134.0 it
    rewrites the user's real ``~/.codex/config.toml`` (top-level
    ``profile = "ucode"``) so even the bare ``codex`` CLI routes through the
    workspace gateway, and ``ucode revert`` does not undo that edit. Removing
    the provider therefore undoes that wiring as part of the removal — no
    extra confirm, matching how a key provider's ``Remove`` acts immediately.
    The cleanup only ever touches ucode-namespaced artifacts (the ``profile``
    selector only when it equals ``"ucode"``; see
    :mod:`omnicraft.onboarding.ucode_cleanup`), so the user's own settings
    are never at risk. Removal applies to every harness the provider
    serves — a databricks entry routes both Claude and Codex.

    :param provider: The databricks provider id, e.g. ``"databricks"``.
    :returns: A confirmation message for the level-2 status line reporting
        the removal and what wiring was cleaned (nothing extra is appended
        when no ucode wiring existed). Side effects: may edit
        ``~/.codex/config.toml``, delete ucode sidecar files, run
        ``claude mcp remove``, and write ``~/.omnicraft/config.yaml``.
    """
    from omnicraft.errors import OmniCraftError
    from omnicraft.onboarding.ucode_cleanup import remove_ucode_wiring

    cleanup_note = ""
    try:
        removal = remove_ucode_wiring()
    except (OmniCraftError, OSError) as exc:
        # The entry removal below still proceeds — the user asked for it —
        # but say exactly what was left behind instead of failing silently.
        cleanup_note = f" — ucode cleanup incomplete: {exc}"
    else:
        cleaned: list[str] = []
        if removal.codex_config_stripped:
            cleaned.append("cleaned ~/.codex/config.toml")
        if removal.removed_sidecars:
            cleaned.append(f"deleted {len(removal.removed_sidecars)} ucode sidecar file(s)")
        if removal.web_search_mcp_removed:
            cleaned.append("unregistered ucode's web_search MCP")
        if cleaned:
            cleanup_note = f" — {', '.join(cleaned)}"
    removed_msg = _remove_credential(provider) or f"✓ {provider} removido"
    return f"{removed_msg}{cleanup_note}"


def _set_harness_default(provider: str, family: str) -> str | None:
    """Make *provider* the default for *family* and persist wholesale.

    :param provider: The provider name to default, e.g. ``"openrouter"``.
    :param family: The harness surface to scope the default to,
        ``"anthropic"``, ``"openai"``, or ``"pi"`` — leaving the other
        harnesses' defaults untouched.
    :returns: A confirmation message for the caller to show as a transient
        status, or ``None`` when there was nothing to do. Side effect:
        writes ``~/.omnicraft/config.yaml``.
    """
    from omnicraft.onboarding.configure_models import family_label
    from omnicraft.onboarding.provider_config import load_providers, set_default_provider

    block = _load_global_config().get("providers")
    if not isinstance(block, dict):
        return None
    entry = load_providers({"providers": block}).get(provider)
    label = _credential_label(provider, entry) if entry is not None else provider
    _save_global_config({"providers": set_default_provider(block, provider, family)})
    return f"✓ {label} agora é o padrão de {family_label(family)}"


def _clear_detection_dismissal(name: str) -> None:
    """Drop *name* from the persisted ``dismissed_detections`` list, if present.

    Called when the user explicitly re-adds a previously Removed (and thus
    dismissed) ambient credential — e.g. picking the detected codex
    config.toml provider from the add menu — so the detection behaves like
    an ordinary one again.

    :param name: The detection name to un-dismiss, e.g. ``"codex-databricks"``.
    :returns: None. Side effect: writes ``~/.omnicraft/config.yaml`` when the
        name was dismissed; no write otherwise.
    """
    from omnicraft.onboarding.detected import (
        DISMISSED_DETECTIONS_KEY,
        dismissed_detection_names,
    )

    dismissed = dismissed_detection_names(_load_global_config())
    if name not in dismissed:
        return
    _save_global_config({DISMISSED_DETECTIONS_KEY: sorted(dismissed - {name})})


def _remove_credential(provider: str) -> str | None:
    """Remove the *provider* credential and persist wholesale.

    The stored secret (if any) is left in place — removing a credential does
    not assume its key is unwanted.

    :param provider: The provider id to remove, e.g. ``"openrouter"``.
    :returns: A confirmation message for the caller to show as a transient
        status, or ``None`` when there was nothing to remove. Side effect:
        writes ``~/.omnicraft/config.yaml`` (and, when the removed entry is
        backed by a live ambient detection that cannot be signed out,
        records its name under ``dismissed_detections`` so the next
        configure open does not silently re-adopt it).
    """
    from omnicraft.onboarding.ambient import detect_providers
    from omnicraft.onboarding.detected import (
        DISMISSED_DETECTIONS_KEY,
        dismissed_detection_names,
    )
    from omnicraft.onboarding.provider_config import load_providers

    config = _load_global_config()
    block = config.get("providers")
    if not isinstance(block, dict) or provider not in block:
        return None
    entry = load_providers({"providers": block}).get(provider)
    label = _credential_label(provider, entry) if entry is not None else provider
    remaining = {k: v for k, v in block.items() if k != provider}
    settings: dict[str, Any] = {"providers": remaining}  # type: ignore[explicit-any]  # yaml-boundary mapping
    # If a live ambient detection backs this entry, removing the entry alone
    # is a no-op: the next configure open re-detects and re-adopts it (the
    # "Remove doesn't remove" bug). Subscriptions are exempt — their removal
    # path signs out of the CLI instead, and a future re-login SHOULD
    # re-adopt. Everything else (env API key, codex config.toml provider,
    # local Ollama) gets a persisted dismissal that the add menu's detected
    # option clears on re-add.
    backing = next(
        (d for d in detect_providers() if d.name == provider and d.kind != "subscription"),
        None,
    )
    if backing is not None:
        settings[DISMISSED_DETECTIONS_KEY] = sorted(dismissed_detection_names(config) | {provider})
    _save_global_config(settings)  # wholesale replace per key
    if backing is not None:
        return (
            f"✓ {label} removido — permanece na sua máquina mas não será autoconfigurado de novo"
        )
    return f"✓ {label} removido"


def _launch_opencode_auth_login() -> str | None:
    """Launch interactive ``opencode auth login``; return a post-login status.

    ``opencode auth login`` is interactive (pick a provider, sign in), so this
    hands the terminal to ``opencode`` and re-reads the credential state on
    return. Mirrors :func:`_launch_goose_configure`.
    """
    from omnicraft.onboarding.harness_install import (
        OPENCODE_KEY,
        harness_cli_installed,
        harness_install_spec,
    )
    from omnicraft.onboarding.interactive import console
    from omnicraft.onboarding.opencode_auth import opencode_auth_summary

    if not harness_cli_installed(OPENCODE_KEY):
        return "✗ CLI opencode não encontrado"
    spec = harness_install_spec(OPENCODE_KEY)
    assert spec is not None
    console.print(
        "  [dim]Lançando [bold]opencode auth login[/bold] — escolha um provedor e "
        "faça login, depois volte.[/dim]"
    )
    with contextlib.suppress(OSError, KeyboardInterrupt):
        subprocess.run([spec.binary, "auth", "login"], check=False)
    summary = opencode_auth_summary()
    if summary.has_provider:
        return f"✓ provedores: {summary.describe()}"
    return "Nenhum provedor detectado ainda"


def _run_opencode_auth_list() -> None:
    """Show ``opencode auth list`` (stored credentials + detected env providers)."""
    from omnicraft.onboarding.harness_install import OPENCODE_KEY, harness_install_spec

    spec = harness_install_spec(OPENCODE_KEY)
    if spec is None:
        return
    with contextlib.suppress(OSError, KeyboardInterrupt):
        subprocess.run([spec.binary, "auth", "list"], check=False)


def _list_opencode_models() -> list[str]:
    """Return the ``provider/model`` ids OpenCode can launch (``opencode models``).

    Best-effort: an absent CLI or a failed/empty invocation yields ``[]`` (the
    caller then tells the user to sign a provider in first).
    """
    from omnicraft.onboarding.harness_install import OPENCODE_KEY, harness_install_spec

    spec = harness_install_spec(OPENCODE_KEY)
    if spec is None:
        return []
    try:
        result = subprocess.run(
            [spec.binary, "models"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _set_opencode_default_model(current: str | None) -> str | None:
    """Pick OpenCode's default model and persist it as ``opencode_model``.

    The choice is what ``omni opencode`` launches on when no ``--model`` is
    given — written into the per-session ``opencode.json`` at spawn so the TUI
    starts on it instead of ``opencode/big-pickle``. Returns a status line for
    the drill-in, or ``None`` when cancelled.

    :param current: The currently-persisted default model, or ``None``.
    """
    from omnicraft.onboarding.interactive import console, select
    from omnicraft.onboarding.opencode_auth import reachable_provider_ids

    models = _list_opencode_models()
    if not models:
        return "✗ sem modelos — faça login em um provedor primeiro (opencode auth login)"
    # `opencode models` can list hundreds of `provider/model` ids across every
    # provider on models.dev — too long for the picker (it overflows the
    # viewport and flickers). Narrow to the providers the user can actually
    # authenticate (stored auth.json + env keys); fall back to the full list
    # only if that filter would hide everything.
    reachable = reachable_provider_ids()
    if reachable:
        scoped = [m for m in models if m.split("/", 1)[0] in reachable]
        models = scoped or models
    options = list(models)
    clear_index = -1
    if current is not None:
        clear_index = len(options)
        options.append("Limpar padrão (usar o padrão do próprio OpenCode)")
    default = models.index(current) if current in models else 0
    # Even filtered to reachable providers the list can exceed the screen, so
    # bound the picker to a scrolling viewport sized to the terminal (leaving
    # room for the title / status / footer / "N more" markers).
    rows = shutil.get_terminal_size(fallback=(80, 24)).lines
    idx = select(
        "Escolha o modelo padrão do OpenCode",
        options,
        default=default,
        clear_on_exit=True,
        status=f"atual: {current}" if current else None,
        max_visible=max(5, rows - 8),
    )
    if idx < 0:
        return None
    if idx == clear_index:
        _save_global_config({}, unset_keys=("opencode_model",))
        console.print("  [green]✓ modelo padrão limpo[/green]")
        return "✓ modelo padrão limpo"
    chosen = models[idx]
    _save_global_config({"opencode_model": chosen})
    console.print(f"  [green]✓ modelo padrão definido para[/green] [bold]{chosen}[/bold]")
    return f"✓ modelo padrão: {chosen}"


def _print_opencode_auth_help() -> None:
    """Explain where OpenCode's model credentials come from."""
    from omnicraft.onboarding.interactive import console

    console.print(
        "  O OpenCode resolve um modelo a partir do provedor que seu agente usa:\n"
        "    • [bold]opencode auth login[/bold] — faça login em um provedor "
        "(OpenAI, Anthropic, …);\n"
        "      armazenado em ~/.local/share/opencode/auth.json.\n"
        "    • Env vars de provedor (OPENAI_API_KEY / ANTHROPIC_API_KEY / …) são autodetectadas.\n"
        "    • Gateway Databricks: defina um ``profile`` de agente "
        "(configurado sob Claude / Codex);\n"
        "      o OmniCraft sintetiza a config de provedor por sessão do opencode a partir dele.\n"
        "  O OmniCraft não armazena credencial própria do OpenCode.\n"
        "  [dim]Dica:[/dim] 'Definir modelo padrão' escolhe em qual modelo o "
        "`omni opencode` lança\n"
        "  (caso contrário o OpenCode usa seu padrão interno, opencode/big-pickle)."
    )


def _manage_opencode_harness() -> None:
    """Run the level-2 drill-in for OpenCode: ensure the CLI, then manage providers.

    OpenCode owns its own provider auth — ``opencode auth login`` (stored in
    ``~/.local/share/opencode/auth.json``) or ambient provider env vars — so,
    like the Goose / Qwen drill-ins, this reports which providers OpenCode can
    reach and offers to launch its native login; it never stores a key through
    OmniCraft. (For the Databricks-gateway path the agent's ``profile`` is
    synthesized into opencode's per-session config instead — set under
    Claude / Codex.)

    OpenCode is npm-installable, so a missing CLI gates the drill-in with an
    install offer.

    :returns: None. Side effect: may ``npm install`` the opencode CLI.
    """
    from omnicraft.onboarding.harness_install import (
        OPENCODE_KEY,
        harness_cli_installed,
        harness_install_command,
        install_harness_cli,
    )
    from omnicraft.onboarding.interactive import console, select

    if not harness_cli_installed(OPENCODE_KEY):
        cmd = " ".join(harness_install_command(OPENCODE_KEY))
        choice = select(
            "O CLI do OpenCode não está instalado. Instalar agora?",
            [
                f"Sim — instalar ({cmd})",
                "Não — voltar aos harnesses",
                "Eu mesmo rodo (mostrar o comando)",
            ],
            descriptions=[
                f"Roda `{cmd}` (precisa de npm).",
                "Volta ao seletor de harnesses sem instalar.",
                "Imprime o comando para você instalar por conta, depois volta.",
            ],
            default=0,
            clear_on_exit=True,
        )
        if choice == 0:
            console.print(f"  [dim]Instalando o OpenCode — rodando `{cmd}`…[/dim]")
            if install_harness_cli(OPENCODE_KEY):
                console.print("  [green]✓ OpenCode instalado[/green]")
            else:
                console.print(
                    f"  [red]Falha na instalação.[/red] Rode manualmente e reabra: "
                    f"[bold]{cmd}[/bold]"
                )
                return
        elif choice == 2:  # run it yourself
            console.print(f"  Instale o OpenCode com:\n    [bold]{cmd}[/bold]")
            return
        else:
            return

    # OpenCode owns its provider auth (``opencode auth login`` → auth.json) or
    # ambient env keys; OmniCraft stores nothing. Report what's reachable and
    # offer to run its native login — like the Goose/Qwen drill-ins.
    status: str | None = None
    while True:
        from omnicraft.onboarding.opencode_auth import opencode_auth_summary

        summary = opencode_auth_summary()
        default_model = _load_effective_config().get("opencode_model")
        header = (
            f"OpenCode — provedores: {summary.describe()}"
            if summary.has_provider
            else "OpenCode — nenhum provedor configurado ainda"
        )
        model_label = (
            f"Definir modelo padrão (atual: {default_model})"
            if default_model
            else "Definir modelo padrão"
        )
        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow("Rodar opencode auth login", action="login"),
            _HarnessMenuRow(model_label, action="model"),
            _HarnessMenuRow("Listar provedores e credenciais", action="list"),
            _HarnessMenuRow("Mostrar opções de provedor", action="help"),
            _HarnessMenuRow("← Voltar", action="back"),
        ]
        idx = select(header, [r.label for r in rows], clear_on_exit=True, status=status)
        if idx < 0:  # Esc / q
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "login":
            status = _launch_opencode_auth_login()
        elif action == "model":
            status = _set_opencode_default_model(default_model)
        elif action == "list":
            _run_opencode_auth_list()
            status = None
        elif action == "help":
            _print_opencode_auth_help()
            status = None


def _run_configure_harnesses_interactive() -> None:
    """Run the interactive model/credential three-level picker.

    Invoked by ``omnicraft setup --no-internal-beta`` and the bare-``run``
    first-run path, so both drive the identical flow.
    Opening it backfills a legacy databricks ``auth:`` block into a real
    provider and adopts any ambient-detected credential — announcing the
    newly auto-configured machine credentials in a callout — then loops on
    the level-1 harness overview. Every harness is shown on a single compact
    row — the harness name on the left, then an aligned ``✓``/``✗`` status
    column (the configured credential, or "Não instalado" / "Não configurado")
    — in 0.3 priority order: Claude, Codex, Cursor, OpenCode,
    Hermes, Pi, then Antigravity, Qwen Code, Goose, Copilot, Kiro, Kimi Code.
    The actionable hint (install command / next step) renders only for the
    highlighted row, as the selector's description line, so the overview stays
    uncluttered.

    :returns: None. Side effect: may write ``~/.omnicraft/config.yaml`` via
        the backfill/adopt steps and any add/set-default/remove the user
        performs while navigating.
    """
    from rich.cells import cell_len
    from rich.markup import escape

    from omnicraft.onboarding.antigravity_auth import (
        ANTIGRAVITY_ENV_VARS,
        ANTIGRAVITY_EXTRA,
        antigravity_api_key_configured,
        antigravity_sdk_installed,
    )
    from omnicraft.onboarding.configure_models import family_label
    from omnicraft.onboarding.copilot_auth import (
        COPILOT_EXTRA,
        COPILOT_TOKEN_ENV_VARS,
        copilot_github_token_configured,
        copilot_sdk_installed,
    )
    from omnicraft.onboarding.cursor_auth import (
        CURSOR_EXTRA,
        cursor_api_key_configured,
        cursor_sdk_installed,
    )
    from omnicraft.onboarding.extra_install import extra_install_display
    from omnicraft.onboarding.goose_auth import goose_config_summary
    from omnicraft.onboarding.harness_install import (
        COPILOT_KEY,
        CURSOR_KEY,
        GOOSE_KEY,
        HERMES_KEY,
        KIMI_KEY,
        KIRO_KEY,
        OPENCODE_KEY,
        QWEN_KEY,
        harness_cli_installed,
        harness_install_command,
        harness_install_spec,
    )
    from omnicraft.onboarding.interactive import select
    from omnicraft.onboarding.provider_config import (
        ANTHROPIC_FAMILY,
        OPENAI_FAMILY,
        PI_SURFACE,
        surface_default_provider,
    )

    # Surface missing external tooling (Node ≥22.10 / tmux) the harnesses need,
    # once up front, so configuring a credential doesn't lead to a cryptic
    # failure when the harness later can't launch.
    _warn_missing_harness_dependencies()

    # Backfill a databricks provider from a legacy global auth: block FIRST (it
    # outranks ambient detection in routing), then adopt ambient detections.
    # The databricks backfill is silent (it just shows up in the harness status
    # line); newly-adopted machine credentials get a one-time callout naming
    # what was auto-configured and from where. No progress spinner here: a
    # transient spinner over the (fast) detection left a cleared-region gap and
    # a residual line directly above the menu on first paint.
    _adopt_ambient_credentials()

    # Level 1: pick a harness. The cursor moves between Claude, Codex, Pi, and
    # Quit; each harness's status renders as a non-selectable sub-line beneath
    # it (skipped by ↑/↓). Drilling in (level 2) keeps add/manage off this
    # overview. The menu clears in place on each choice so the session stays on
    # one screen. Quit / Esc / q exits.
    _QUIT = "\x00quit"  # sentinel marking the Quit row (not a family)
    # Sentinel marking the Antigravity row — it is not a provider family (Gemini
    # is outside the anthropic/openai machinery), so it dispatches to its own
    # credential manager rather than ``_manage_harness_providers``.
    _ANTIGRAVITY = "\x00antigravity"
    # Sentinel marking the Qwen Code row — like Antigravity/Cursor it is not a
    # provider family (its v1 auth is the CLI's own env vars / ``/auth`` flow,
    # not an OmniCraft credential), so it dispatches to its own drill-in.
    _QWEN = "\x00qwen"
    # Sentinel marking the OpenCode row — native-server harness with no OmniCraft
    # credential of its own (it routes through the bound agent's Databricks
    # gateway profile or ambient provider env), so it dispatches to its own
    # binary-install/info drill-in.
    _OPENCODE = "\x00opencode"
    # Sentinel marking the Goose row — like Qwen/Antigravity/Cursor it is not a
    # provider family (Goose owns its own auth via ``goose configure``, not an
    # OmniCraft credential), so it dispatches to its own drill-in.
    _GOOSE = "\x00goose"
    # Sentinel marking the Hermes row — like Goose it owns its own auth via
    # ``hermes model`` and is installed via a curl installer.
    _HERMES = "\x00hermes"
    # Sentinel marking the Kiro row — like Goose/Hermes it owns its own auth (via
    # ``kiro-cli login``) and is installed via Kiro's curl installer, so it
    # dispatches to its own drill-in rather than a provider family.
    _KIRO = "\x00kiro"
    # Sentinel marking the Kimi Code row — like Cursor/Antigravity/Qwen it is
    # not a provider family. Auth lives entirely in the kimi CLI (``kimi login``
    # / ``kimi provider add`` → ~/.kimi/config.toml), so it dispatches to its
    # own drill-in rather than ``_manage_harness_providers``.
    _KIMI = "\x00kimi"
    # Sentinels for the generic-ACP rows. Each configured agent gets its own row
    # (``_ACP_AGENT_PREFIX + slug`` → per-agent remove drill-in); a single
    # ``_ACP_ADD`` row jumps straight into the add flow. Not a provider family —
    # each ACP agent owns its own auth.
    _ACP_ADD = "\x00acp-add"
    _ACP_AGENT_PREFIX = "\x00acp-agent:"
    families = [ANTHROPIC_FAMILY, OPENAI_FAMILY, PI_SURFACE]

    # Status glyph + Rich color per readiness kind: "ready" is a configured,
    # launchable harness (green ✓); "missing" is an absent CLI/SDK (red ✗);
    # "warn" is installed-but-unconfigured (yellow ✗ — present, not usable
    # yet); "action" is a do-something row (e.g. Add) with no status glyph. The
    # glyph leads the status, which sits in a left-aligned column right of the
    # names, so every ✓/✗ lines up in a single column.
    status_styles = {
        "ready": ("✓", "green"),
        "missing": ("✗", "red"),
        "warn": ("✗", "yellow"),
        "action": ("", "cyan"),
    }

    def _install_hint(command: str) -> str:
        # Selection-only tooltip. The command is escaped so a bracketed extra
        # (e.g. ``pip install "omnicraft[cursor]"``) renders literally instead of
        # parsing as Rich markup.
        return f"Instale com `{escape(command)}`"

    def _truncate_cells(text: str, max_cells: int) -> str:
        """Truncate *text* to a terminal-cell budget, adding an ellipsis if needed."""
        if cell_len(text) <= max_cells:
            return text
        ellipsis = "…"
        budget = max(0, max_cells - cell_len(ellipsis))
        out: list[str] = []
        used = 0
        for ch in text:
            width = cell_len(ch)
            if used + width > budget:
                break
            out.append(ch)
            used += width
        return "".join(out) + ellipsis

    def _family_row(fam: str) -> tuple[str, str, str, str, str]:
        # Claude / Codex / Pi: a CLI binary plus a usable default credential.
        # Pi's default is its *effective* one (explicit pi scope, else the
        # cross-family fallback).
        name = family_label(fam)
        if not harness_cli_installed(fam):
            return (
                fam,
                name,
                "Não instalado",
                "missing",
                _install_hint(" ".join(harness_install_command(fam))),
            )
        default = surface_default_provider(config, fam)
        if default is None:
            return (fam, name, "Não configurado", "warn", "Abra para adicionar uma credencial.")
        label = _family_credential_label(config, fam, default.name, default)
        return (fam, name, label, "ready", "")

    def build_harness_rows() -> list[tuple[str, str, str, str, str]]:
        # One visible row per harness, in 0.3 priority order. No folding — every
        # harness shows at once. Each row is (target, name, status, kind, hint),
        # where ``hint`` is the selection-only description (install command /
        # next step), empty for a ready harness.
        from omnicraft.onboarding.hermes_auth import hermes_config_summary
        from omnicraft.onboarding.opencode_auth import opencode_auth_summary

        rows: list[tuple[str, str, str, str, str]] = []
        rows.append(_family_row(ANTHROPIC_FAMILY))
        rows.append(_family_row(OPENAI_FAMILY))

        # Cursor — readiness is the CURSOR_API_KEY (the cursor-sdk extra is a
        # soft dependency; the key is independently storable, so a missing SDK
        # is surfaced as the install hint, not a hard block).
        if cursor_api_key_configured(config) or bool(os.environ.get("CURSOR_API_KEY")):
            rows.append((CURSOR_KEY, "Cursor", "Chave de API", "ready", ""))
        elif not cursor_sdk_installed():
            rows.append(
                (
                    CURSOR_KEY,
                    "Cursor",
                    "Não instalado",
                    "missing",
                    _install_hint(extra_install_display(CURSOR_EXTRA)),
                ),
            )
        else:
            rows.append(
                (
                    CURSOR_KEY,
                    "Cursor",
                    "Não configurado",
                    "warn",
                    "Abra para adicionar a chave de API do Cursor.",
                ),
            )

        # OpenCode — its own provider auth (login or env keys); the status is
        # what it can reach (e.g. "1 stored").
        opencode = opencode_auth_summary()
        if not opencode.installed:
            rows.append(
                (
                    _OPENCODE,
                    "OpenCode",
                    "Não instalado",
                    "missing",
                    _install_hint(" ".join(harness_install_command(OPENCODE_KEY))),
                ),
            )
        elif opencode.ready:
            rows.append((_OPENCODE, "OpenCode", opencode.describe(), "ready", ""))
        else:
            rows.append(
                (
                    _OPENCODE,
                    "OpenCode",
                    "Não configurado",
                    "warn",
                    "Abra para fazer login (opencode auth login).",
                ),
            )

        # Hermes — curl-installed; its provider/model live in
        # ``~/.hermes/config.yaml`` (written by `hermes model`). Read that so a
        # configured Hermes shows the picked model as ready, instead of always
        # reading "not configured" on an installed binary. A fresh install
        # ships ``provider: auto`` (nothing picked), so it still reads
        # "not configured" until `hermes model` selects a concrete provider.
        hermes = hermes_config_summary()
        if not hermes.installed:
            hermes_spec = harness_install_spec(HERMES_KEY)
            hermes_hint = (
                hermes_spec.install_hint
                if hermes_spec and hermes_spec.install_hint
                else "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
            )
            rows.append(
                (_HERMES, "Hermes", "Não instalado", "missing", _install_hint(hermes_hint)),
            )
        elif hermes.ready:
            rows.append((_HERMES, "Hermes", hermes.describe(), "ready", ""))
        else:
            rows.append(
                (
                    _HERMES,
                    "Hermes",
                    "Não configurado",
                    "warn",
                    "Abra para configurar com `hermes model`.",
                ),
            )

        rows.append(_family_row(PI_SURFACE))

        # Antigravity — Gemini key (antigravity-sdk extra is soft, like Cursor).
        if antigravity_api_key_configured(config) or any(
            os.environ.get(v) for v in ANTIGRAVITY_ENV_VARS
        ):
            rows.append((_ANTIGRAVITY, "Antigravity", "Chave de API do Gemini", "ready", ""))
        elif not antigravity_sdk_installed():
            rows.append(
                (
                    _ANTIGRAVITY,
                    "Antigravity",
                    "Não instalado",
                    "missing",
                    _install_hint(extra_install_display(ANTIGRAVITY_EXTRA)),
                ),
            )
        else:
            rows.append(
                (
                    _ANTIGRAVITY,
                    "Antigravity",
                    "Não configurado",
                    "warn",
                    "Abra para adicionar a chave de API do Gemini.",
                ),
            )

        # Qwen Code — no CLI login; auth via OpenAI-compatible env vars or the
        # interactive /auth flow.
        if not harness_cli_installed(QWEN_KEY):
            rows.append(
                (
                    _QWEN,
                    "Qwen Code",
                    "Não instalado",
                    "missing",
                    _install_hint(" ".join(harness_install_command(QWEN_KEY))),
                ),
            )
        elif _qwen_auth_configured():
            rows.append((_QWEN, "Qwen Code", "Autenticado", "ready", ""))
        else:
            rows.append(
                (
                    _QWEN,
                    "Qwen Code",
                    "Não configurado",
                    "warn",
                    "Abra para configurar a auth (/auth ou env vars).",
                ),
            )

        # Goose — its own provider config via `goose configure`.
        if not harness_cli_installed(GOOSE_KEY):
            goose_spec = harness_install_spec(GOOSE_KEY)
            goose_hint = (
                goose_spec.install_hint
                if goose_spec and goose_spec.install_hint
                else "brew install block-goose-cli"
            )
            rows.append((_GOOSE, "Goose", "Não instalado", "missing", _install_hint(goose_hint)))
        else:
            goose_summary = goose_config_summary()
            if goose_summary.provider:
                rows.append((_GOOSE, "Goose", goose_summary.provider, "ready", ""))
            else:
                rows.append(
                    (
                        _GOOSE,
                        "Goose",
                        "Não configurado",
                        "warn",
                        "Abra para rodar `goose configure`.",
                    ),
                )

        # Copilot — GitHub token (github-copilot-sdk extra is soft).
        if copilot_github_token_configured(config) or any(
            os.environ.get(v) for v in COPILOT_TOKEN_ENV_VARS
        ):
            rows.append((COPILOT_KEY, "Copilot", "Token do GitHub", "ready", ""))
        elif not copilot_sdk_installed():
            rows.append(
                (
                    COPILOT_KEY,
                    "Copilot",
                    "Não instalado",
                    "missing",
                    _install_hint(extra_install_display(COPILOT_EXTRA)),
                ),
            )
        else:
            rows.append(
                (
                    COPILOT_KEY,
                    "Copilot",
                    "Não configurado",
                    "warn",
                    "Abra para adicionar o token do GitHub.",
                ),
            )

        # Kiro — native CLI, own auth via `kiro-cli login`; there is no
        # reliable local status probe, so an installed binary is still only
        # "not configured" until the user signs in.
        if harness_cli_installed(KIRO_KEY):
            rows.append(
                (_KIRO, "Kiro", "Não configurado", "warn", "Faça login com `kiro-cli login`.")
            )
        else:
            kiro_spec = harness_install_spec(KIRO_KEY)
            kiro_hint = (
                kiro_spec.install_hint
                if kiro_spec and kiro_spec.install_hint
                else "curl -fsSL https://cli.kiro.dev/install | bash"
            )
            rows.append((_KIRO, "Kiro", "Não instalado", "missing", _install_hint(kiro_hint)))

        # Kimi Code — native CLI, own auth via `kimi login`; there is no local
        # login status probe yet. Curl-installed (no npm package), so use its
        # install_hint when absent and show "not configured" when present.
        if harness_cli_installed(KIMI_KEY):
            rows.append(
                (_KIMI, "Kimi Code", "Não configurado", "warn", "Faça login com `kimi login`.")
            )
        else:
            kimi_spec = harness_install_spec(KIMI_KEY)
            kimi_hint = (kimi_spec.install_hint if kimi_spec else None) or "see Kimi Code docs"
            rows.append((_KIMI, "Kimi Code", "Não instalado", "missing", _install_hint(kimi_hint)))

        # Custom ACP agents — the generic `acp` harness driving any user-configured
        # ACP-agent command. Each configured agent gets its own overview row
        # (select → per-agent remove drill-in) so it sits alongside the built-in
        # harnesses, followed by an "Add" row that jumps straight into the add
        # flow. Not gated on a binary — each agent owns its own install.
        from omnicraft.onboarding.acp_auth import acp_config_summary

        acp_summary = acp_config_summary()
        for agent in acp_summary.agents:
            rows.append(
                (
                    _ACP_AGENT_PREFIX + agent.slug,
                    agent.name,
                    f"ACP · {agent.command}",
                    "ready",
                    "Selecione para remover este agente ACP.",
                )
            )
        rows.append(
            (
                _ACP_ADD,
                "Adicionar agente ACP customizado"
                if acp_summary.configured
                else "Agente ACP customizado",
                "" if acp_summary.configured else "Nenhum configurado",
                "action",
                "Adicione um agente ACP (gemini, qwen, goose, …).",
            )
        )
        return rows

    while True:
        config = _load_global_config()
        harness_rows = build_harness_rows()
        # Place the status in a single column a fixed gutter right of the names,
        # so every ✓/✗ glyph lines up vertically (the earlier right-aligned
        # status scattered the glyphs and read as messy). The name column is the
        # widest harness name + a 4-space gutter; the status is escaped when
        # interpolated into markup so a credential label containing a ``[`` can't
        # parse as a Rich tag (descriptions are escaped the same way).
        name_col = max(len(name) for _t, name, *_rest in harness_rows) + 4
        term_width = max(40, shutil.get_terminal_size(fallback=(80, 24)).columns)
        # _render_menu prefixes selected rows with ``"    ❯  "`` (7 cells).
        # Cap the status text from the actual terminal width so verbose status
        # rows (e.g. OpenCode's provider summary) do not wrap in the compact
        # single-line overview.
        max_status_width = max(8, min(30, term_width - 7 - name_col - len("✓ ")))
        options: list[str] = []
        selectable: list[bool] = []
        row_target: list[str | None] = []
        descriptions: list[str] = []
        for target, name, status_text, kind, desc in harness_rows:
            status_text = _truncate_cells(status_text, max_status_width)
            glyph, color = status_styles[kind]
            options.append(f"{name.ljust(name_col)}[{color}]{glyph} {escape(status_text)}[/]")
            selectable.append(True)
            row_target.append(target)
            descriptions.append(desc)
        options.append("Sair")
        selectable.append(True)
        row_target.append(_QUIT)
        descriptions.append("")
        idx = select(
            "Configurar harnesses",
            options,
            descriptions=descriptions,
            selectable=selectable,
            clear_on_exit=True,
            compact=True,
        )
        if idx < 0:  # Esc / q — exit
            return
        target = row_target[idx]
        if target == CURSOR_KEY:
            _manage_cursor_harness()
        elif target == COPILOT_KEY:
            _manage_copilot_harness()
        elif target in families:
            _manage_harness_providers(target)
        elif target == _ANTIGRAVITY:
            _manage_antigravity_harness()
        elif target == _QWEN:
            _manage_qwen_harness()
        elif target == _OPENCODE:
            _manage_opencode_harness()
        elif target == _GOOSE:
            _manage_goose_harness()
        elif target == _ACP_ADD:
            _add_acp_agent()
        elif isinstance(target, str) and target.startswith(_ACP_AGENT_PREFIX):
            _manage_acp_agent(target[len(_ACP_AGENT_PREFIX) :])
        elif target == _HERMES:
            _manage_hermes_harness()
        elif target == _KIRO:
            _manage_kiro_harness()
        elif target == _KIMI:
            _manage_kimi_harness()
        else:  # Quit row (or, defensively, a non-family row)
            return


@cli.command("setup")
@click.option(
    "--internal-beta/--no-internal-beta",
    default=False,
    help="Roda a configuração padrão de modelo/credencial (padrão): escolha um "
    "provedor para cada harness e defina seus padrões. Passe --internal-beta "
    "para configurar padrões e autenticação do internal-beta do Databricks.",
)
def setup(internal_beta: bool) -> None:
    """
    Lança o fluxo de configuração inicial do OmniCraft.

    Por padrão isto roda o seletor padrão de modelo/credencial — escolha um
    provedor para cada harness e defina seus padrões, depois inicie uma sessão
    com ``omnicraft run``. (Liste as credenciais configuradas com
    ``omnicraft config list``.) Passe ``--internal-beta`` para configurar
    padrões e autenticação do internal-beta do Databricks em vez disso.
    """
    from omnicraft.inner import ui

    # Brand the first-run experience without pushing the actual picker below a
    # typical 80×24 terminal. The full lockup is great in roomy terminals, but
    # on short terminals it combines with the missing-tool warning and scrolls
    # the menu off the first screen.
    if shutil.get_terminal_size(fallback=(80, 24)).lines >= 32:
        ui.print_landing(tagline="todos os seus agentes, um só cli")
    else:
        ui.print_brandmark("setup")

    if internal_beta:
        # The internal-beta workspace defaults are excluded from the public OSS
        # build. Fail loud with a clear message instead of an ImportError deep
        # in the onboarding flow when someone passes --internal-beta there.
        try:
            import omnicraft.onboarding.internal_beta  # noqa: F401
        except ImportError:
            raise click.ClickException(
                "A configuração internal-beta do Databricks não está disponível neste build. "
                "Rode `omnicraft setup` para a configuração padrão de modelo/credencial."
            ) from None
        # Internal-beta routing mints workspace OAuth tokens via
        # databricks-sdk at runtime, and the SDK ships in the `databricks`
        # extra rather than the default install. Fail loud up front instead
        # of completing the whole login flow and breaking on the first turn.
        from omnicraft.onboarding.databricks_config import (
            DATABRICKS_EXTRA_INSTALL_HINT,
            databricks_sdk_installed,
        )

        if not databricks_sdk_installed():
            raise click.ClickException(
                "A configuração internal-beta do Databricks precisa do extra databricks "
                f"(databricks-sdk). Reinstale com:\n  {DATABRICKS_EXTRA_INSTALL_HINT}"
            )
        # Surface missing external tooling (Node, tmux) before the Databricks
        # bootstrap so a fresh machine sees every gap at once.
        _warn_missing_harness_dependencies()
        from omnicraft.onboarding.internal_beta import _INTERNAL_BETA_DEFAULT_SERVER
        from omnicraft.onboarding.sandboxes.lakebox import install_demo_databricks_cli
        from omnicraft.onboarding.setup import run_onboarding

        # Install the demo `databricks` CLI (with the `lakebox`
        # subcommand) BEFORE profile onboarding — `run_onboarding`
        # shells out to `databricks auth login`, and a fresh machine
        # might not have the binary on PATH at all. Idempotent: skips
        # the installer when the demo CLI is already present, but
        # still persists ~/.local/bin in the user's shell rc files.
        install_demo_databricks_cli()
        with _isolated_databricks_cfg():
            if not run_onboarding():
                raise click.ClickException("o onboarding não concluiu; veja a saída acima.")
            _run_configure_databricks()
        agent_path = _materialize_internal_beta_agents()
        _save_global_config(
            {
                "default_agent": str(agent_path),
                "profile": "oss",
                "server": _INTERNAL_BETA_DEFAULT_SERVER,
                # auth: block provides the default executor credentials for
                # agents that do not declare executor.auth themselves.
                "auth": {"type": "databricks", "profile": "oss"},
            }
        )
        click.echo(f"Definido default_agent={agent_path} em {_GLOBAL_CONFIG_PATH}")
        click.echo("Digite `omnicraft claude` para começar com o Claude Code no omnicraft.")
        return

    # --no-internal-beta: the standard model/credential picker. It warns
    # about missing Node/tmux itself, configures providers/defaults, and
    # returns; the user then starts a session with ``omnicraft run``.
    _run_configure_harnesses_interactive()


# ─── sandbox group ────────────────────────────────────────────────
# The provider-agnostic sandbox CLI lives in omnicraft/cli_sandbox.py.
# Provider launcher modules are optional and may be absent from a given
# distribution; hide the group when none are available.
# `omnicraft lakebox` is kept as an alias for `omnicraft sandbox …
# --provider lakebox`, registered only when the lakebox provider ships.
if _sandbox_providers():
    cli.add_command(_sandbox_group)
    if "lakebox" in _sandbox_providers():
        cli.add_command(_lakebox_alias_group)

# ─── debug group ──────────────────────────────────────────────────
#
# Operator-only maintenance commands, grouped under ``omnicraft debug``
# so they stay out of the everyday surface.
#
# ``db-upgrade`` runs manual schema operations on an OmniCraft tracking
# database. Mirrors ``mlflow db upgrade`` (``mlflow/db.py``) so the
# workflow is familiar to anyone who's bumped an MLflow database before.
# The server initializes a fresh database on first boot and attempts to
# auto-upgrade an existing database that is behind head; this command
# remains available for explicit/manual upgrades, or for retrying an
# automatic migration that failed.
#
# ``migrate-accounts-to-oidc`` remaps user identities when switching the
# built-in accounts provider to OIDC.


@cli.group("debug")
def debug() -> None:
    """Comandos internos de manutenção (avançado — não necessários para uso normal).

    Abriga a manutenção de banco de dados e contas apenas para operadores:
    upgrades de schema do banco de rastreamento (``db-upgrade``) e o remapeamento
    de identidade contas→OIDC (``migrate-accounts-to-oidc``).
    """


@debug.command("db-upgrade")
@click.argument("url")
def debug_db_upgrade(url: str) -> None:
    """
    Atualiza o schema de um banco de dados de rastreamento do OmniCraft para a
    versão suportada mais recente.

    URL é uma URL de banco SQLAlchemy, ex.
    ``sqlite:////caminho/absoluto/para/chat.db`` ou
    ``postgresql://user:pass@host/dbname``.

    \b
    IMPORTANTE: migrações de schema podem ser lentas e não têm garantia
    de serem transacionais — sempre faça um backup do seu banco
    antes de rodar migrações.
    """
    from sqlalchemy import create_engine

    from omnicraft.db.utils import _run_migrations

    click.echo(f"Atualizando {url} ...")
    engine = create_engine(url)
    try:
        _run_migrations(engine, url)
    finally:
        engine.dispose()
    click.echo("Atualização concluída.")


@debug.command("migrate-accounts-to-oidc")
@click.argument("url")
@click.option(
    "--map",
    "maps",
    multiple=True,
    metavar="OLD=NEW",
    help="Remapeamento explícito de identidade, ex. --map alice=alice@example.com "
    "(repetível; sobrescreve --domain para o mesmo OLD).",
)
@click.option(
    "--domain",
    default=None,
    metavar="DOMAIN",
    help="Acrescenta @DOMAIN a todo username sem @, ex. "
    "--domain example.com mapeia alice -> alice@example.com.",
)
@click.option(
    "--commit",
    is_flag=True,
    default=False,
    help="Aplica as mudanças. Sem esta flag o comando é um "
    "dry run que reporta o que mudaria e não altera nada.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Permite mesclar sobre um id NEW que já existe como um "
    "usuário distinto (mescla direitos de admin). Desligado por padrão para evitar "
    "mesclagens acidentais de privilégios.",
)
def debug_migrate_accounts_to_oidc(
    url: str,
    maps: tuple[str, ...],
    domain: str | None,
    commit: bool,
    force: bool,
) -> None:
    """Remapeia identidades de usuário ao trocar o provedor de contas para OIDC.

    O provedor de contas indexa os usuários por username (``alice``); o OIDC os
    indexa por email do IdP (``alice@example.com``). Isto reescreve toda linha
    que carrega um user-id (concessões de permissão, comentários, políticas, tokens,
    propriedade de host) para que o time mantenha seu admin e seus dados na
    troca. Agnóstico de provedor: toca apenas o banco de dados, então rode-o
    contra seu DB ao vivo *antes* de virar ``OMNICRAFT_AUTH_PROVIDER``.

    URL é uma URL de banco SQLAlchemy, ex.
    ``sqlite:////caminho/absoluto/para/chat.db`` ou
    ``postgresql://user:pass@host/dbname``.

    \b
    Exemplos:
      # Dry run: acrescenta o domínio da org a todo username
      omnicraft debug migrate-accounts-to-oidc sqlite:///chat.db --domain example.com
      # Aplica
      omnicraft debug migrate-accounts-to-oidc sqlite:///chat.db --domain example.com --commit
      # Mapeamento explícito por usuário (adicione --commit para aplicar)
      omnicraft debug migrate-accounts-to-oidc sqlite:///chat.db --map alice=alice@corp.com

    \b
    IMPORTANTE: sempre faça backup do seu banco antes de rodar com
    --commit. O remapeamento roda em uma transação mas reescreve chaves
    primárias em várias tabelas.
    """
    from sqlalchemy import create_engine

    from omnicraft.server.identity_migration import build_domain_mapping, remap_identities

    engine = create_engine(url)
    try:
        mapping: dict[str, str] = {}
        if domain:
            mapping.update(build_domain_mapping(engine, domain))
        # Explicit --map pairs win over the domain-derived mapping.
        for pair in maps:
            if "=" not in pair:
                raise click.BadParameter(f"--map espera OLD=NEW, recebeu {pair!r}")
            old, new = (part.strip() for part in pair.split("=", 1))
            if not old or not new:
                raise click.BadParameter(f"--map espera OLD=NEW não vazio, recebeu {pair!r}")
            mapping[old] = new

        if not mapping:
            raise click.UsageError("nada para migrar: passe --domain DOMAIN e/ou --map OLD=NEW")

        report = remap_identities(engine, mapping, dry_run=not commit, force=force)
    finally:
        engine.dispose()

    mode = "COMMITADO" if report.committed else "DRY RUN (nenhuma mudança escrita)"
    click.echo(f"\nRemapeamento de identidade — {mode}")
    click.echo(f"  banco de dados: {url}")
    click.echo(f"  mapeamentos ({len(report.mapping)}):")
    for old, new in report.mapping.items():
        click.echo(f"    {old}  ->  {new}")

    # The NEW ids must equal what the IdP returns at login, or the user
    # signs in as a brand-new principal (not admin, no prior sessions).
    # This is the #1 footgun with --domain when the IdP email isn't
    # <username>@<domain> (e.g. GitHub returning a @gmail.com address).
    click.echo(
        "\n  ⚠ Cada id NEW deve corresponder ao email que seu IdP retorna para aquele usuário.\n"
        "    Se não corresponder, esse usuário loga como um novo principal — readicione-o à\n"
        "    lista de admins, ou rode de novo com --map OLD=<email-exato-do-idp>."
    )
    bare = sorted({new for new in report.mapping.values() if "@" not in new})
    if bare:
        click.echo(
            "    Estes alvos não têm '@' e provavelmente não são emails de IdP: " + ", ".join(bare)
        )

    if report.per_table:
        click.echo("  linhas alteradas:")
        for table, count in sorted(report.per_table.items()):
            click.echo(f"    {table}: {count}")
    else:
        click.echo("  linhas alteradas: nenhuma")

    if report.skipped_missing:
        click.echo(f"  pulados (sem linha de usuário): {', '.join(report.skipped_missing)}")
    if report.refused:
        click.echo(
            "  RECUSADO (id NEW já existe — rode de novo com --force para mesclar): "
            + ", ".join(report.refused)
        )

    if not report.committed:
        click.echo("\nIsto foi um dry run. Rode de novo com --commit para aplicar.\n")
    else:
        click.echo("\nConcluído. Vire OMNICRAFT_AUTH_PROVIDER=oidc e reinicie.\n")


@debug.command("logs")
@click.option(
    "--type",
    "log_type",
    type=click.Choice(["runner", "host-runner", "server", "cli"], case_sensitive=False),
    default="runner",
    show_default=True,
    help="Categoria de log: runner (runner local do CLI via omnicraft run), "
    "host-runner (runner criado por um daemon host), "
    "server (servidor local), ou cli (diagnósticos do CLI).",
)
@click.option(
    "--session",
    "session_id",
    default=None,
    metavar="SESSION_ID",
    help="Filtra logs de host-runner por id de sessão, ex. conv_abc123. "
    "Só se aplica a --type host-runner. Mostra todos os arquivos de log da "
    "sessão, do mais antigo primeiro.",
)
@click.option(
    "--list",
    "list_only",
    is_flag=True,
    default=False,
    help="Lista os arquivos de log disponíveis com tamanho e timestamp "
    "em vez de mostrar o conteúdo.",
)
@click.option(
    "--lines",
    "-n",
    default=50,
    show_default=True,
    metavar="N",
    type=click.IntRange(min=0),
    help="Linhas a mostrar do fim do log (0 = arquivo inteiro). "
    "Com --session, aplicado por arquivo.",
)
@click.option(
    "--follow",
    "-f",
    is_flag=True,
    default=False,
    help="Acompanha o arquivo de log mais recente em tempo real (como tail -f). "
    "Com --session, acompanha o arquivo mais recente da sessão. "
    "Não suportado no Windows.",
)
def debug_logs(
    log_type: str, session_id: str | None, list_only: bool, lines: int, follow: bool
) -> None:
    """Mostra os logs de diagnóstico do runner, servidor ou CLI.

    Imprime o final do arquivo de log mais recente da categoria escolhida.
    Use ``--list`` para ver todos os arquivos disponíveis, ou ``--follow`` para
    transmitir a nova saída conforme é escrita.

    Passe ``--session SESSION_ID`` (só ``--type host-runner``) para restringir a
    saída a todos os arquivos de log produzidos para uma sessão específica entre relançamentos.

    \b
    Locais dos logs (relativos a ~/.omnicraft ou $OMNICRAFT_DATA_DIR):
      runner       logs/runner/runner-*.log
      host-runner  logs/host-runner/runner-*.log
      server       logs/server/*server*.log
      cli          logs/cli-*.log

    \b
    Exemplos:
      # Mostra o final do log de runner local mais recente (padrão)
      omnicraft debug logs
      # Lista todos os arquivos de log de runner local com tamanhos
      omnicraft debug logs --list
      # Mostra logs de host-runner de uma sessão específica (entre relançamentos)
      omnicraft debug logs --type host-runner --session conv_abc123
      # Lista arquivos de log de host-runner de uma sessão
      omnicraft debug logs --type host-runner --session conv_abc123 --list
      # Acompanha o log de servidor mais recente em tempo real
      omnicraft debug logs --type server --follow
      # Mostra o log completo de diagnósticos do CLI mais recente
      omnicraft debug logs --type cli -n 0
    """
    import re
    import subprocess

    from omnicraft.host.local_server import _local_data_dir

    if session_id is not None and log_type != "host-runner":
        raise click.UsageError("--session só é suportado com --type host-runner")

    if follow and IS_WINDOWS:
        raise click.UsageError("--follow não é suportado no Windows")

    data_dir = _local_data_dir()

    _log_configs: dict[str, tuple[Path, str]] = {
        "runner": (data_dir / "logs" / "runner", "runner-*.log"),
        "host-runner": (data_dir / "logs" / "host-runner", "runner-*.log"),
        # Covers both server-*.log (omnicraft run) and local-server-*.log (daemon).
        "server": (data_dir / "logs" / "server", "*server*.log"),
        "cli": (data_dir / "logs", "cli-*.log"),
    }

    log_dir, pattern = _log_configs[log_type]

    if not log_dir.exists():
        raise click.ClickException(f"Nenhum log de {log_type} encontrado — {log_dir} não existe.")

    if session_id is not None:
        # Sanitize the same way connect.py does so the glob matches.
        slug = re.sub(r"[^\w-]", "", session_id)[:32]
        pattern = f"runner-{slug}-*.log"

    # Exclude symlinks (e.g. latest-cli.log), sort newest first.
    log_files = sorted(
        (f for f in log_dir.glob(pattern) if not f.is_symlink()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if not log_files:
        if session_id is not None:
            raise click.ClickException(
                f"Nenhum log de host-runner encontrado para a sessão {session_id!r}. "
                "Ids de sessão aparecem nos nomes de arquivo apenas para runners lançados "
                "depois que este recurso foi adicionado."
            )
        raise click.ClickException(f"Nenhum arquivo de log de {log_type} encontrado em {log_dir}.")

    if list_only:
        header = (
            f"logs de host-runner da sessão {session_id!r} em {log_dir}:"
            if session_id
            else f"logs de {log_type} em {log_dir}:"
        )
        click.echo(header)
        for f in log_files:
            stat = f.stat()
            size_kb = stat.st_size / 1024
            mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
            click.echo(f"  {mtime}  {size_kb:6.1f} KB  {f.name}")
        return

    if follow:
        # Follow the most recent file only (tail -f can only track one file).
        latest = log_files[0]
        click.echo(f"# {latest}", err=True)
        subprocess.run(["tail", "-f", str(latest)])
        return

    if session_id is not None:
        # Show all files for the session, oldest first, with separators.
        for f in reversed(log_files):
            click.echo(f"# {f}", err=True)
            content = f.read_text(errors="replace")
            if lines > 0:
                content = "\n".join(content.splitlines()[-lines:])
            click.echo(content)
            click.echo()
    else:
        latest = log_files[0]
        click.echo(f"# {latest}", err=True)
        content = latest.read_text(errors="replace")
        if lines > 0:
            content = "\n".join(content.splitlines()[-lines:])
        click.echo(content)


def _workspace_mount_probe_matches(candidate: str, probe: httpx.Response) -> bool:
    """Whether a ``/api/2.0/omnicraft`` mount probe answered like omnicraft.

    :param candidate: The probed mount base URL, e.g.
        ``"https://example.databricks.com/api/2.0/omnicraft"``.
    :param probe: The ``GET <candidate>/v1/me`` response.
    :returns: ``True`` when the mount answered 200 (omnicraft itself) or
        with a Databricks-fronted shape (302 to ``/oidc/`` or 401 with
        the ``DatabricksRealm`` challenge).
    """
    return probe.status_code == 200 or (
        _databricks_workspace_login_target(candidate, probe) is not None
    )


def _cached_workspace_bearer(workspace_host: str) -> str | None:
    """Best-effort bearer for *workspace_host* from the OAuth cache.

    Unlike :func:`_databricks_workspace_token`, a missing ``databricks``
    extra is not an error here — probe callers simply fall back to
    unauthenticated behavior.

    :param workspace_host: The workspace host, e.g.
        ``"https://example.databricks.com"``.
    :returns: A bearer token, or ``None`` when the ``databricks`` extra
        is not installed or no cached grant resolves for the host.
    """
    from omnicraft.onboarding.databricks_config import databricks_sdk_installed

    if not databricks_sdk_installed():
        return None
    return _databricks_workspace_token(workspace_host)


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _with_default_scheme(server_url: str) -> str:
    """Prepend a scheme to a schemeless server URL, defaulting to https.

    The internal user guide hands out workspace URLs without a scheme
    (e.g. ``example.cloud.databricks.com/omnicraft``), so a missing
    scheme defaults to ``https`` to let that URL be pasted verbatim.
    Loopback hosts (``localhost``, ``127.0.0.1``, ``::1``) default to
    ``http`` instead — local dev servers are plain http (the examples
    use ``http://localhost:6767``). A URL that already carries a scheme
    is returned unchanged.

    :param server_url: The user-supplied server URL, possibly
        schemeless, e.g. ``"example.cloud.databricks.com/omnicraft"``.
    :returns: The URL with a scheme, e.g.
        ``"https://example.cloud.databricks.com/omnicraft"``.
    """
    from urllib.parse import urlsplit

    server_url = server_url.strip()
    if "://" in server_url:
        return server_url
    host = urlsplit(f"https://{server_url}").hostname or ""
    scheme = "http" if host in _LOOPBACK_HOSTS else "https"
    return f"{scheme}://{server_url}"


def _workspace_api_server_url(server: str) -> str:
    """Expand a bare Databricks workspace URL to its omnicraft API base.

    ``https://<workspace>`` hosts serve the workspace web app at the
    root; workspace-hosted omnicraft lives at ``/api/2.0/omnicraft``.
    Users naturally paste the bare host, so when a path-less server URL
    answers like a Databricks workspace web app (a non-omnicraft reply
    carrying the ``server: databricks`` header) AND the
    ``/api/2.0/omnicraft`` mount answers like the API proxy, the
    expanded URL is adopted. Detection is behavioral — no hostname
    patterns — and URLs that already carry a path are returned
    untouched without any probe, the one exception being the
    guide-issued web-UI URL (``https://<ws>/omnicraft``): its bare root
    is probed so the pasted web URL logs in just like the bare host
    (a root that is not a workspace leaves the URL untouched).

    Some workspace edges (Azure) answer the anonymous mount probe with
    a plain 404 — not the AWS proxy's 401-with-``DatabricksRealm``
    challenge — so a mount that works for authenticated callers is
    invisible to the anonymous probe. When the host-keyed Databricks
    OAuth cache holds a grant for the workspace (the user ran
    ``databricks auth login``), the mount probe is retried with that
    bearer before giving up.

    :param server: The user-supplied server URL, e.g.
        ``"https://example.databricks.com"``.
    :returns: The normalized base URL without a trailing slash, e.g.
        ``"https://example.databricks.com/api/2.0/omnicraft"`` — or the
        input (normalized) when expansion does not apply.
    """
    from urllib.parse import urlsplit, urlunsplit

    import httpx as _httpx

    from omnicraft.conversation_browser import (
        WORKSPACE_API_PATH,
        WORKSPACE_UI_PATH,
        display_server_url,
    )

    server = server.rstrip("/")
    parsed = urlsplit(server)
    # Strip any ?o= selector / query / fragment before probing: callers append
    # a path (``f"{base}/v1/..."``), so a query-bearing base would push that
    # path into the query (``…/?o=123/v1/me``) and break the probe + expansion.
    # The selector is carried separately (recorded at login, replayed as the
    # X-Databricks-Org-Id header), never on the base URL.
    if parsed.query or parsed.fragment:
        server = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")).rstrip("/")
        parsed = urlsplit(server)
    # The internal user guide hands out the workspace web-UI URL
    # (``https://<ws>/omnicraft``) for browser access; accept it for login
    # too by expanding its bare root to the API mount. A root that does
    # not answer as a Databricks workspace leaves the pasted URL
    # untouched, so a non-workspace server served under ``/omnicraft``
    # still works.
    if parsed.scheme == "https" and parsed.path == WORKSPACE_UI_PATH:
        root = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        expanded = _workspace_api_server_url(root)
        return expanded if expanded != root else server
    if parsed.path not in ("", "/") or parsed.scheme != "https":
        return server
    try:
        probe = _httpx.get(f"{server}/v1/me", timeout=10.0)
    except _httpx.HTTPError:
        return server
    # Already something we understand at the root: an omnicraft server
    # (200 / 401-with-login_url JSON) or a Databricks Apps edge /
    # API proxy (the login-target detector recognizes both).
    if probe.status_code == 200:
        return server
    if _databricks_workspace_login_target(server, probe) is not None:
        return server
    server_header = probe.headers.get("server")
    if server_header is None or server_header.lower() != "databricks":
        return server
    candidate = urlunsplit((parsed.scheme, parsed.netloc, WORKSPACE_API_PATH, "", ""))
    try:
        api_probe = _httpx.get(f"{candidate}/v1/me", timeout=10.0)
    except _httpx.HTTPError:
        return server
    if _workspace_mount_probe_matches(candidate, api_probe):
        click.echo(
            f"Usando {display_server_url(candidate)} "
            "(omnicraft hospedado no workspace Databricks)."
        )
        return candidate
    # The anonymous probe came back inconclusive (404 on Azure even
    # when the mount exists). Retry it with a cached workspace bearer;
    # either way, say what was decided — this branch is only reached
    # for genuine workspace web hosts, where a silent decline strands
    # the user on a bare URL that can only 404.
    token = _cached_workspace_bearer(server)
    if token is not None:
        try:
            authed_probe = _httpx.get(
                f"{candidate}/v1/me",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
        except _httpx.HTTPError:
            authed_probe = None
        if authed_probe is not None and _workspace_mount_probe_matches(candidate, authed_probe):
            click.echo(
                f"Usando {display_server_url(candidate)} "
                "(omnicraft hospedado no workspace Databricks)."
            )
            return candidate
        click.echo(
            f"Nota: {server} responde como um workspace Databricks, mas "
            f"{candidate} não respondeu como um servidor omnicraft mesmo com "
            f"as credenciais de workspace em cache. Conectando a {server} como "
            "informado; se o omnicraft estiver hospedado neste workspace, atualize o "
            f"login com `databricks auth login --host {server}` ou passe "
            "a URL completa do mount."
        )
        return server
    click.echo(
        f"Nota: {server} responde como um workspace Databricks, mas "
        f"{candidate} não respondeu à sondagem anônima "
        f"(HTTP {api_probe.status_code}). Algumas edges escondem o mount de "
        "requisições não autenticadas — se o omnicraft estiver hospedado neste "
        f"workspace, rode `databricks auth login --host {server}` e "
        "tente de novo, ou passe a URL completa do mount."
    )
    return server


def _resolve_server_url(server: str) -> str:
    """
    Normalize a user-supplied ``--server`` value to the OmniCraft API base.

    Every ``--server`` entry point (and ``login``) needs the same
    normalization, so they all route through here: strip a trailing slash,
    default a schemeless URL to ``https`` (``http`` for loopback hosts),
    then expand a bare Databricks workspace URL — or the ``/omnicraft``
    web-UI URL the internal user guide hands out — to the
    ``/api/2.0/omnicraft`` mount.

    :param server: A non-empty ``--server`` value, e.g.
        ``"example.cloud.databricks.com/omnicraft"``.
    :returns: The normalized API base URL without a trailing slash, e.g.
        ``"https://example.cloud.databricks.com/api/2.0/omnicraft"``.
    """
    return _workspace_api_server_url(_with_default_scheme(server.rstrip("/")))


def _databricks_workspace_login_target(server: str, probe: httpx.Response) -> str | None:
    """Return the workspace host when *server* sits behind Databricks auth.

    Recognizes the two Databricks-fronted deployment shapes from the
    unauthenticated probe alone — no hostname pattern matching, so
    custom domains work too:

    - **Databricks Apps**: the Apps edge answers with a 302 to the
      fronting workspace's OIDC authorize endpoint
      (``https://<workspace>/oidc/oauth2/v2.0/authorize?...``); the
      redirect names the workspace to authenticate against.
    - **Workspace-hosted omnicraft** (e.g.
      ``https://<workspace>/api/2.0/omnicraft``): the workspace API
      proxy answers 401 with ``WWW-Authenticate: Bearer
      realm="DatabricksRealm"``; the workspace is the URL's own host.

    :param server: The server URL the user is logging in to, e.g.
        ``"https://myapp-123.aws.databricksapps.com"``.
    :param probe: The unauthenticated ``GET /v1/me`` probe response.
    :returns: The workspace host, e.g.
        ``"https://example.databricks.com"``, or ``None`` when the
        response matches neither Databricks shape.
    """
    from urllib.parse import urlparse

    if probe.status_code in (302, 303, 307):
        raw_location = probe.headers.get("location")
        if raw_location is None:
            return None
        location = urlparse(raw_location)
        if location.scheme != "https" or not location.netloc:
            return None
        if not location.path.startswith("/oidc/"):
            return None
        return f"https://{location.netloc}"

    if probe.status_code == 401:
        www_authenticate = probe.headers.get("www-authenticate")
        if www_authenticate and "databricksrealm" in www_authenticate.lower():
            parsed = urlparse(server)
            if parsed.scheme == "https" and parsed.netloc:
                return f"https://{parsed.netloc}"

    return None


def _org_id_from_url(url: str) -> str | None:
    """Extract the ``?o=<workspace-id>`` workspace selector from *url*.

    A Databricks host can front many workspaces under one hostname, where
    the bare host resolves to the account and ``?o=<workspace-id>`` picks
    the workspace. The selector is threaded into both the login (to bind
    the grant to the workspace) and every API request (to route to it).

    :param url: A user-supplied server URL, possibly carrying ``?o=``,
        e.g. ``"https://acme.databricks.com/?o=123"``.
    :returns: The workspace id, e.g. ``"123"``, or ``None`` when absent.
    """
    from urllib.parse import parse_qs, urlsplit

    values = parse_qs(urlsplit(url).query).get("o")
    return values[0] if values and values[0] else None


def _host_with_org(workspace_host: str, org_id: str | None) -> str:
    """Append the ``?o=<org>`` workspace selector to *workspace_host*.

    ``databricks auth login --host https://<ws>/?o=<org>`` makes the CLI
    record ``workspace_id`` in the profile and bind the grant to that
    workspace; without it the grant is account-scoped and the workspace
    rejects it (HTTP 403). Returns *workspace_host* unchanged when no org
    id is known, so single-workspace hosts are untouched.

    :param workspace_host: The workspace host, e.g.
        ``"https://example.databricks.com"``.
    :param org_id: The workspace id from :func:`_org_id_from_url`, or
        ``None``.
    :returns: ``"https://<ws>/?o=<org>"`` when *org_id* is set, else
        *workspace_host*.
    """
    if not org_id:
        return workspace_host
    # Encode (not interpolate) so a value with ``&``/``=`` can't inject extra
    # query params onto the ``--host`` URL; keep the ``/?o=`` slash the CLI wants.
    from urllib.parse import urlencode, urlsplit, urlunsplit

    parsed = urlsplit(workspace_host.rstrip("/"))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", urlencode({"o": org_id}), "")
    )


def _databricks_login(server: str, workspace_host: str, org_id: str | None = None) -> None:
    """Log in to a Databricks-fronted OmniCraft server.

    Covers both Databricks Apps deployments and workspace-hosted
    omnicraft (``https://<workspace>/api/2.0/omnicraft``). Reuses an
    existing host-keyed Databricks CLI OAuth grant when one resolves;
    otherwise runs ``databricks auth login --host <workspace>``
    (browser flow). The minted token is verified against the server
    before anything is stored; a *cached* grant that fails
    verification (e.g. a stale token-cache entry minted for a
    different workspace) triggers one fresh browser login and a
    re-verify before failing loud. On success, a pointer record is
    stored in ``~/.omnicraft/auth_tokens.json`` — no profile name is
    created or consulted anywhere.

    :param server: The server URL, e.g.
        ``"https://myapp-123.aws.databricksapps.com"``.
    :param workspace_host: The Databricks workspace to authenticate
        against, e.g. ``"https://example.databricks.com"``.
    :param org_id: The ``?o=`` workspace selector from the login URL
        (see :func:`_org_id_from_url`). When set, the login binds the
        grant to this workspace and the verify request routes to it —
        needed where the bare host is the account, not a workspace.
    :raises click.ClickException: When the ``databricks`` extra or CLI
        binary is missing, the workspace login fails, or the server
        rejects the workspace token.
    """
    from omnicraft.onboarding.databricks_config import (
        DATABRICKS_EXTRA_INSTALL_HINT,
        databricks_sdk_installed,
    )

    click.echo(f"{server} autentica via o workspace Databricks {workspace_host}.")

    if not databricks_sdk_installed():
        raise click.ClickException(
            "Fazer login em um servidor fronteado por Databricks (um Databricks App ou "
            "omnicraft hospedado no workspace) requer o extra `databricks` "
            f"(databricks-sdk não está instalado). Reinstale com:\n  "
            f"{DATABRICKS_EXTRA_INSTALL_HINT}"
        )

    token = _databricks_workspace_token(workspace_host)
    fresh_login_done = False
    if token is None:
        token = _login_and_mint_workspace_token(workspace_host, org_id)
        fresh_login_done = True

    # Verify the workspace token actually gets through the edge to THIS
    # server (the user may lack access to it), and learn our identity
    # for the success message.
    verify = _verify_databricks_server_token(server, token, org_id)
    if verify.status_code != 200 and not fresh_login_done:
        # A cached grant can be stale or minted for a different
        # workspace (the CLI token cache is host-keyed but not
        # validated against the issuer). One fresh browser login
        # replaces the bad cache entry; then re-verify.
        click.echo(
            f"As credenciais Databricks em cache foram rejeitadas por {server} "
            f"(HTTP {verify.status_code}) — atualizando o login do workspace."
        )
        token = _login_and_mint_workspace_token(workspace_host, org_id)
        verify = _verify_databricks_server_token(server, token, org_id)
    if verify.status_code != 200:
        raise click.ClickException(
            f"{workspace_host} aceitou o login, mas {server} rejeitou o token "
            f"(HTTP {verify.status_code}). Verifique se seu usuário tem acesso a este app."
        )
    user_id: str | None = None
    with contextlib.suppress(ValueError):
        raw_user = verify.json().get("user_id")
        user_id = raw_user if isinstance(raw_user, str) else None

    from omnicraft.cli_auth import store_databricks_auth

    store_databricks_auth(
        server,
        workspace_host,
        user_id=user_id,
        # Recorded so later commands replay it as ``?o=`` to route requests
        # and browser links append it. The login URL's selector wins; fall
        # back to the org id the workspace stamps on responses.
        org_id=org_id or verify.headers.get("x-databricks-org-id"),
    )
    who = f" como {user_id}" if user_id else ""
    click.echo(
        f"Login feito{who}. Comandos mirando {server} agora geram tokens de "
        "workspace automaticamente."
    )


def _login_and_mint_workspace_token(workspace_host: str, org_id: str | None = None) -> str:
    """Run the browser login for a workspace and mint a bearer from it.

    :param workspace_host: The workspace host, e.g.
        ``"https://example.databricks.com"``.
    :param org_id: The ``?o=`` workspace selector (see
        :func:`_org_id_from_url`); passed to the browser login so the
        minted grant is bound to the workspace.
    :returns: A fresh bearer token for the workspace.
    :raises click.ClickException: When the Databricks CLI binary is
        missing, the login exits non-zero, or no token resolves after
        a successful login.
    """
    _run_databricks_browser_login(workspace_host, org_id)
    token = _databricks_workspace_token(workspace_host)
    if token is None:
        raise click.ClickException(
            f"O login do workspace concluiu mas nenhum token resolve para {workspace_host}. "
            f"Rode `databricks auth token --host {workspace_host}` para depurar."
        )
    return token


def _run_databricks_browser_login(workspace_host: str, org_id: str | None = None) -> None:
    """Run ``databricks auth login --host <workspace>`` (browser flow).

    :param workspace_host: The workspace host, e.g.
        ``"https://example.databricks.com"``.
    :param org_id: The ``?o=`` workspace selector (see
        :func:`_org_id_from_url`). When set, ``?o=<org_id>`` is appended
        to ``--host`` so the CLI records ``workspace_id`` and binds the
        grant to that workspace (else the grant is account-scoped and
        the workspace rejects it).
    :raises click.ClickException: When the Databricks CLI binary is
        missing or the login exits non-zero.
    """
    databricks_bin = shutil.which("databricks")
    if databricks_bin is None:
        raise click.ClickException(
            "O CLI do Databricks é necessário para fazer login em um workspace. "
            "Instale-o primeiro: https://docs.databricks.com/dev-tools/cli/install.html"
        )
    login_host = _host_with_org(workspace_host, org_id)
    click.echo(f"Abrindo o navegador para fazer login em {login_host} ...")
    result = subprocess.run(
        [databricks_bin, "auth", "login", "--host", login_host],
        check=False,
    )
    if result.returncode != 0:
        raise click.ClickException(
            f"`databricks auth login --host {login_host}` falhou "
            f"(saída {result.returncode}). Se o workspace estiver inacessível "
            "desta máquina (VPN / listas de acesso por IP), resolva isso e tente de novo."
        )


def _verify_databricks_server_token(
    server: str, token: str, org_id: str | None = None
) -> httpx.Response:
    """Probe ``GET /v1/me`` on *server* with a workspace bearer.

    :param server: The server URL, e.g.
        ``"https://myapp-123.aws.databricksapps.com"``.
    :param token: The workspace bearer token to present.
    :param org_id: The ``?o=`` workspace selector (see
        :func:`_org_id_from_url`). When set, the probe carries
        ``?o=<org_id>`` so the request routes to the workspace rather
        than defaulting to the account (which answers HTTP 503).
    :returns: The probe response (200 means the token is accepted and
        the body carries ``user_id``).
    :raises click.ClickException: When the server is unreachable.
    """
    import httpx as _httpx

    try:
        return _httpx.get(
            f"{server}/v1/me",
            headers={"Authorization": f"Bearer {token}"},
            params={"o": org_id} if org_id else None,
            timeout=10.0,
        )
    except _httpx.HTTPError as exc:
        raise click.ClickException(
            f"Não foi possível acessar {server}/v1/me para verificar o login: {exc}"
        ) from exc


def _databricks_workspace_token(workspace_host: str) -> str | None:
    """Mint a bearer for a workspace from the host-keyed OAuth cache.

    :param workspace_host: The workspace host, e.g.
        ``"https://example.databricks.com"``.
    :returns: A bearer token, or ``None`` when no cached grant
        resolves (the caller should run ``databricks auth login``).
    """
    from omnicraft.inner.databricks_executor import (
        DatabricksAuthError,
        _resolve_databricks_auth,
    )

    try:
        auth, _host = _resolve_databricks_auth(host=workspace_host)
        return auth.current_token()
    except (DatabricksAuthError, ValueError):
        return None


def _remember_default_server(server: str) -> None:
    """
    Persist *server* as the user-level default after a successful login.

    A bare ``omnicraft`` (and ``omnicraft host``) fall back to the
    configured ``server`` key when no ``--server`` is passed (see
    :func:`run` and :func:`host`). Without this, a user who runs
    ``omnicraft login <server>`` and then bare ``omnicraft`` is still routed
    at whatever default ``setup`` baked in — the confusing "I just logged
    in, yet I'm asked to log in again to a different server" path.
    Recording the just-logged-in server as the default closes that gap.

    Any existing default is overwritten: targeting more than one server is
    rare, and the server the user most recently logged in to is the best
    available signal of intent.

    :param server: Normalized server URL the login succeeded against, e.g.
        ``"https://example.databricks.com/api/2.0/omnicraft"``.
    """
    _save_global_config({"server": server})
    click.echo(f"Definido {server} como seu servidor padrão.")


@cli.command("login")
@click.argument("server_url")
def login(server_url: str) -> None:
    """Autentica com um servidor OmniCraft remoto.

    Sonda o modo de auth do servidor e roda o fluxo correspondente:

    \b
    - modo contas: pede usuário + senha (sem navegador
      necessário), faz POST em ``/auth/login``, armazena o JWT de sessão em
      ``~/.omnicraft/auth_tokens.json`` indexado pela URL do servidor.
    - modo OIDC: abre o navegador, faz polling no endpoint de ticket do CLI,
      armazena o JWT de sessão quando o fluxo do navegador conclui.
    - modo header: nenhum login necessário (o proxy injeta a identidade); nós
      imprimimos uma dica e saímos com sucesso.
    - fronteado por Databricks (um Databricks App, ou omnicraft hospedado em
      um caminho de API de workspace): detectado a partir da resposta da sondagem — nós
      fazemos login no workspace via ``databricks auth login --host
      <workspace>`` (navegador) e armazenamos um registro-ponteiro para que comandos
      posteriores gerem novos tokens de workspace automaticamente. Requer
      o extra ``databricks``.

    Comandos ``omnicraft run --server <url>`` subsequentes então
    usam o token armazenado via a cadeia de auth runner / host-tunnel. Um
    login bem-sucedido também registra o servidor como o padrão de nível de usuário
    (a chave ``server`` em ``~/.omnicraft/config.yaml``), então um
    ``omnicraft`` puro depois mira nele em vez de qualquer padrão que o
    ``setup`` tenha fixado.

    \b
    Exemplo:
      omnicraft login http://localhost:6767
      omnicraft login example.cloud.databricks.com/omnicraft  # https:// assumido
      omnicraft          # conecta ao servidor que acabou de logar

    :param server_url: A URL do servidor remoto, ex.
        ``"http://localhost:6767"``. Um esquema ausente assume
        ``https://`` (``http://`` para hosts loopback), e a URL do web-UI do
        workspace (``<ws>/omnicraft``) é aceita junto com a raiz
        do workspace pura.
    """
    import httpx as _httpx

    server = _resolve_server_url(server_url)
    # Read the ``?o=`` selector from the raw input: normalization strips the
    # query when expanding to the API mount.
    org_id = _org_id_from_url(server_url)

    # ── Step 0: Probe the server's auth mode. ──────────────────
    # /v1/me returns a JSON ``login_url`` on 401 — "/login" for
    # accounts, "/auth/login" for OIDC, and no login_url at all
    # for header mode. A 302 to a workspace OAuth page (Databricks
    # Apps) or a 401 with a DatabricksRealm challenge (workspace-
    # hosted omnicraft) means Databricks fronts the server. This
    # lets one CLI command handle every posture without a flag.
    try:
        probe = _httpx.get(f"{server}/v1/me", timeout=10.0)
    except _httpx.HTTPError as exc:
        raise click.ClickException(
            f"Não foi possível acessar {server}/v1/me: {exc}\nO servidor está rodando?"
        ) from exc

    databricks_workspace = _databricks_workspace_login_target(server, probe)
    if databricks_workspace is not None:
        _databricks_login(server, databricks_workspace, org_id=org_id)
        _remember_default_server(server)
        return

    detected_login_url: str | None = None
    if probe.status_code == 401:
        import contextlib as _contextlib

        # 401 with non-JSON body — probably not an OmniCraft server.
        # Suppress: we fall through to the OIDC path below which has
        # its own clearer error message.
        with _contextlib.suppress(ValueError):
            detected_login_url = probe.json().get("login_url")
    elif probe.status_code == 200:
        # Header mode (or already authenticated). Tell the user
        # they don't need to log in and exit cleanly.
        click.echo(
            f"{server} está no modo de auth por header — nenhum login necessário. "
            "O proxy na frente dele injeta sua identidade em cada "
            "requisição."
        )
        _remember_default_server(server)
        return

    if detected_login_url == "/login":
        _accounts_login(server)
        _remember_default_server(server)
        return

    # Fall through: OIDC mode (or unknown — let the ticket endpoint's
    # error message guide the user).
    import webbrowser

    from omnicraft.cli_auth import store_token

    # Step 1: Request a CLI login ticket.
    try:
        resp = _httpx.post(f"{server}/auth/cli-login", timeout=10.0)
        resp.raise_for_status()
    except _httpx.HTTPError as exc:
        raise click.ClickException(
            f"Não foi possível acessar {server}/auth/cli-login: {exc}\n"
            f"O servidor está rodando com OMNICRAFT_AUTH_PROVIDER=oidc?"
        ) from exc

    data = resp.json()
    ticket = data["ticket"]
    login_url = f"{server}{data['login_url']}"

    # Step 2: Open the browser.
    click.echo(f"Abrindo o navegador para login: {login_url}")
    click.echo("Aguardando a autenticação...")
    webbrowser.open(login_url)

    # Step 3: Poll until the ticket is fulfilled or expired.
    poll_url = f"{server}/auth/cli-poll?ticket={ticket}"
    import time as _time

    deadline = _time.time() + _CLI_LOGIN_TIMEOUT_SECONDS
    while _time.time() < deadline:
        _time.sleep(2)
        try:
            poll_resp = _httpx.get(poll_url, timeout=10.0)
        except _httpx.HTTPError:
            continue

        if poll_resp.status_code == 202:
            # Still pending.
            continue
        if poll_resp.status_code == 200:
            result = poll_resp.json()
            token = result["token"]
            user_id = result["user_id"]
            expires_in = result.get("expires_in", 8 * 3600)
            store_token(
                server_url=server,
                token=token,
                user_id=user_id,
                expires_at=_time.time() + expires_in,
            )
            click.echo(f"Login feito como {user_id}")
            _remember_default_server(server)
            return
        # 410 or other error — ticket expired.
        raise click.ClickException("O ticket de login expirou ou foi rejeitado. Tente de novo.")

    raise click.ClickException(
        "O login expirou — o fluxo do navegador não foi concluído "
        f"em {_CLI_LOGIN_TIMEOUT_SECONDS} segundos."
    )


_CLI_LOGIN_TIMEOUT_SECONDS = 300  # 5 minutes


def _accounts_login(server: str) -> None:
    """Run the accounts-mode login flow: prompt + POST /auth/login.

    No browser, no polling — accounts auth is username + password,
    we just collect them, send them, and store the returned JWT.

    Three failure paths surface as ClickExceptions so the click
    error formatter renders them consistently with the rest of
    the CLI:

    - Network failure on /auth/login → connection error.
    - 401 from /auth/login → "invalid username or password"
      (the server's generic message — we don't reveal whether
      the username was unknown or the password was wrong).
    - 5xx → "server error".

    On success, the session JWT goes to
    ``~/.omnicraft/auth_tokens.json`` via the existing
    :func:`omnicraft.cli_auth.store_token`. From there both
    ``omnicraft run`` and ``omnicraft host`` pick it up
    automatically when they call ``--server <url>``.
    """
    import httpx as _httpx

    from omnicraft.cli_auth import store_token

    click.echo(f"Entrando em {server} (auth de contas).")
    # `admin` is the bootstrap username; prefill to match what
    # the web LoginPage does.
    username = click.prompt("Usuário", default="admin")
    password = click.prompt("Senha", hide_input=True)

    try:
        resp = _httpx.post(
            f"{server}/auth/login",
            json={"username": username, "password": password},
            timeout=10.0,
        )
    except _httpx.HTTPError as exc:
        raise click.ClickException(f"Não foi possível acessar {server}/auth/login: {exc}") from exc

    if resp.status_code == 401:
        # Generic message — matches what the server returns and
        # what the web form shows. Don't echo the username back
        # in case the terminal is being recorded / shared.
        raise click.ClickException("Usuário ou senha inválidos.")
    if resp.status_code >= 500:
        raise click.ClickException(
            "Erro do servidor durante o login. Tente de novo em um momento."
        )
    if not resp.is_success:
        raise click.ClickException(f"Login falhou ({resp.status_code}): {resp.text[:200]}")

    body = resp.json()
    token = body["token"]
    user_id = body["user"]["id"]
    expires_in = body.get("expires_in", 8 * 3600)

    import time as _time

    store_token(
        server_url=server,
        token=token,
        user_id=user_id,
        expires_at=_time.time() + expires_in,
    )
    click.echo(f"Login feito como {user_id}.")


# Direction codes used by ``pane-split`` and ``pane-picker``.
# ``"v"`` = vertical split (new pane stacked below; tmux ``-v``).
# ``"h"`` = horizontal split (new pane side-by-side; tmux ``-h``).
# ``"w"`` = new window/tab (tmux ``new-window``).
_PANE_SPLIT_DIRECTIONS = ("v", "h", "w")


@cli.command("pane-split", hidden=True)
@click.option("-v", "direction", flag_value="v", help="Divisão vertical (novo pane abaixo)")
@click.option(
    "-h",
    "direction",
    flag_value="h",
    help="Divisão horizontal (novo pane à direita)",
)
@click.option("-w", "direction", flag_value="w", help="Nova janela/aba")
@click.option(
    "-p",
    "--parent-pane",
    "parent_pane",
    required=True,
    help="Id do pane tmux do pane omnicraft pai (ex. '%0'). "
    "Repassado pelo key-binding encapsulado via #{pane_id}.",
)
def pane_split(direction: str | None, parent_pane: str) -> None:
    """
    Divide o pane omnicraft pai e roda o seletor no novo pane.

    Subcomando interno invocado pelos wrappers de key-binding do tmux
    instalados por ``omnicraft.repl._tmux_pane``. O wrapper dispara
    ``run-shell 'omnicraft pane-split -<v|h|w> -p #{pane_id}'`` quando
    o usuário pressiona sua tecla de divisão enquanto focado em um pane
    omnicraft; o tmux substitui ``#{pane_id}`` pelo id do pane focado
    e nós executamos a invocação ``tmux split-window`` / ``new-window``
    correta apontando para ``omnicraft pane-picker``.

    :param direction: Um de ``v`` / ``h`` / ``w``. Obrigatório.
    :param parent_pane: O id do pane omnicraft, ex. ``%0``. Obrigatório.
    """
    import shlex

    from omnicraft.repl._tmux_pane import _resolve_omnicraft_argv

    if direction not in _PANE_SPLIT_DIRECTIONS:
        raise click.ClickException("pane-split requer exatamente um de -v, -h ou -w")
    # The new pane runs ``omnicraft pane-picker`` which reads the
    # parent's pane options and exec's into the chosen agent run.
    # We pass the parent pane id explicitly because the new pane's
    # ``$TMUX_PANE`` will be the new pane, not the parent.
    #
    # tmux's ``split-window`` / ``new-window`` spawns the new
    # pane's initial command via ``/bin/sh -c``, and that shell
    # inherits the tmux server's PATH — which typically does NOT
    # include the venv ``bin/`` where ``omnicraft`` lives.
    # ``_resolve_omnicraft_argv`` returns either an absolute
    # path to the binary (preferred) or ``[python, "-m",
    # "omnicraft.cli"]`` as a fallback that always works.
    picker_argv = [
        *_resolve_omnicraft_argv(),
        "pane-picker",
        "--parent-pane",
        parent_pane,
    ]
    picker_cmd = " ".join(shlex.quote(p) for p in picker_argv)
    # Resolve the parent pane's working directory and pass it via
    # ``-c`` so the new pane inherits the same cwd. Without this,
    # tmux's ``split-window`` / ``new-window`` defaults to the
    # tmux server's cwd (often the user's HOME), which means
    # relative agent paths in the parent's launch argv (e.g.
    # ``examples/databricks_coding_agent.yaml``) don't resolve in
    # the new pane and the spawned REPL exits with "agent path
    # not found" within seconds.
    parent_cwd = subprocess.run(
        ["tmux", "display-message", "-p", "-t", parent_pane, "-F", "#{pane_current_path}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    cwd_args = ["-c", parent_cwd] if parent_cwd else []
    if direction == "v":
        argv = ["tmux", "split-window", "-v", "-t", parent_pane, *cwd_args, picker_cmd]
    elif direction == "h":
        argv = ["tmux", "split-window", "-h", "-t", parent_pane, *cwd_args, picker_cmd]
    else:  # "w"
        argv = ["tmux", "new-window", *cwd_args, picker_cmd]
    os.execvp("tmux", argv)


@cli.command("pane-picker", hidden=True)
@click.option(
    "--parent-pane",
    "parent_pane",
    required=True,
    help="Id do pane tmux do pane omnicraft pai (ex. '%0'). "
    "Usado para ler o contexto de lançamento (nome do agente, argv de lançamento, "
    "URL do servidor) "
    "das opções de pane customizadas que o pai definiu via "
    "``omnicraft.repl._tmux_pane.register_pane``.",
)
def pane_picker(parent_pane: str) -> None:
    """
    Lança uma nova conversa REPL no novo pane atual.

    Subcomando interno. O novo pane tmux (criado por
    ``omnicraft pane-split``) executa este comando, que:

    1. Lê o ``@omnicraft-launch-argv`` do pane omnicraft pai
       e associados.
    2. Faz ``os.execvp`` do argv de lançamento do pai para criar um novo
       REPL contra o mesmo agente neste pane.

    A v1 tem exatamente um caminho: "nova conversa com o mesmo
    agente". Um diálogo seletor (listagem de sub-agentes, "continuar
    sub-agente X", etc.) chega na Fase 2 — veja
    ``designs/REPL_TMUX_PANE_SPLIT.md``. Com apenas uma opção,
    um seletor é fricção; nós apenas executamos.

    :param parent_pane: O id do pane omnicraft pai, ex. ``%0``.
    """
    import json

    from omnicraft.repl._tmux_pane import (
        OPT_LAUNCH_ARGV,
        read_pane_option,
    )

    launch_argv_json = read_pane_option(parent_pane, OPT_LAUNCH_ARGV)
    if not launch_argv_json:
        click.echo(
            f"erro: o pane pai {parent_pane} não tem contexto omnicraft "
            f"(opção {OPT_LAUNCH_ARGV} ausente). Não é possível lançar o REPL irmão.",
            err=True,
        )
        sys.exit(1)
    try:
        launch_argv = json.loads(launch_argv_json)
    except json.JSONDecodeError as exc:
        click.echo(
            f"erro: a opção {OPT_LAUNCH_ARGV} do pane pai {parent_pane} não é JSON válido: {exc}",
            err=True,
        )
        sys.exit(1)
    if not isinstance(launch_argv, list) or not launch_argv:
        click.echo(
            f"erro: o argv de lançamento do pane pai {parent_pane} está vazio ou "
            f"não é uma lista — não é possível reconstruir um comando de lançamento.",
            err=True,
        )
        sys.exit(1)

    # Strip resume-related flags from the parent's argv so the new
    # pane starts a FRESH conversation instead of trying to resume
    # the parent's. The parent may have been launched with
    # ``--resume`` (bare picker), ``--resume <id>`` (specific
    # conversation pin), or ``--continue`` (latest-conv shortcut);
    # replaying them in the new pane would re-open the parent's
    # conversation, defeating the point of a sibling pane. Legacy
    # ``--session <id>`` is also handled here so pre-consolidation
    # parent argvs still sanitize cleanly.
    fresh_argv = _strip_resume_flags(launch_argv)
    # Same treatment for ``-p`` / ``--prompt`` and ``--system-prompt``:
    # the parent's auto-prompt was for THAT conversation; we don't
    # want the new pane to silently re-send it.
    fresh_argv = _strip_one_shot_flags(fresh_argv)
    os.execvp(fresh_argv[0], fresh_argv)


# Pure boolean resume flags: presence drops one token.
# ``-c`` is the short form of ``--continue`` (resume most-recent).
_RESUME_BOOLEAN_FLAGS = frozenset({"--continue", "-c"})

# Resume flags with an optional value: ``--resume`` / ``-r`` may
# appear bare (interactive picker) OR with a conversation id
# (``--resume conv_abc``). We peek at the next token to decide
# whether to drop one or two tokens. Legacy ``--session`` / ``-s``
# remain here so an argv saved by a pre-consolidation client can
# still be sanitized cleanly — newly-saved argvs won't contain them.
_RESUME_OPTIONAL_VALUE_FLAGS = frozenset({"--resume", "-r", "--session", "-s"})

# One-shot flags whose value is bound to a specific conversation
# (the parent's first user message) and thus shouldn't be replayed
# verbatim in a sibling pane. Same valued-flag shape as resume.
_ONE_SHOT_VALUED_FLAGS = frozenset({"-p", "--prompt", "--system-prompt"})


def _strip_resume_flags(argv: list[str]) -> list[str]:
    """
    Return *argv* with all resume-related flags removed.

    Handles three flag shapes:

    - Boolean-only flags (``--continue`` / ``-c``): drop the single
      token.
    - Optional-value flags (``--resume`` / ``-r``, plus the legacy
      ``--session`` / ``-s``): if followed by a non-flag token, drop
      both; otherwise drop just the flag.
    - Long-form ``--key=value`` (``--resume=<id>`` /
      ``--session=<id>``): drop the single combined token.

    :param argv: Parent's launch argv, e.g.
        ``["python", "-m", "omnicraft.cli", "run", "agent.yaml",
        "--model", "my-model", "--resume"]``.
    :returns: The same argv with resume flags removed. Other flags
        (``--model``, ``--harness``, etc.) survive untouched.
    """
    out: list[str] = []
    skip_next = False
    for idx, token in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if token in _RESUME_BOOLEAN_FLAGS:
            continue
        if token in _RESUME_OPTIONAL_VALUE_FLAGS:
            next_token = argv[idx + 1] if idx + 1 < len(argv) else None
            if next_token is not None and not next_token.startswith("-"):
                skip_next = True
            continue
        # ``--resume=value`` / ``--session=value`` long-form.
        if "=" in token:
            head = token.split("=", 1)[0]
            if head in _RESUME_OPTIONAL_VALUE_FLAGS:
                continue
        out.append(token)
    return out


def _strip_one_shot_flags(argv: list[str]) -> list[str]:
    """
    Return *argv* with one-shot conversation flags
    (``-p``/``--prompt``/``--system-prompt``) removed.

    Same flag-shape handling as :func:`_strip_resume_flags`. The
    parent's ``-p "do X"`` was for the parent's first user turn;
    re-applying it in a sibling pane would silently auto-send the
    same prompt, surprising the user.
    """
    out: list[str] = []
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token in _ONE_SHOT_VALUED_FLAGS:
            skip_next = True
            continue
        if "=" in token:
            head = token.split("=", 1)[0]
            if head in _ONE_SHOT_VALUED_FLAGS:
                continue
        out.append(token)
    return out


if __name__ == "__main__":
    cli()
