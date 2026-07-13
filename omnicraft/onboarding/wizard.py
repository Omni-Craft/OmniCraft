"""
Interactive setup flow for ``omnicraft``.

``omnicraft setup`` helps users create a coding agent configuration. It detects
locally installed CLI tools, generates a YAML agent spec, and starts
the server + REPL + web UI.

Two use cases:
1. Single coding agent with custom guardrails (wraps a local CLI).
2. Multi-agent coding system (supervisor coordinates multiple workers).
"""

from __future__ import annotations

import configparser
import os
import re
import shutil
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

console = Console()

# ANSI helpers - used in arrow-menu labels (rendered via sys.stdout.write,
# not Rich, so Rich markup like [dim] would appear as literal text).
_GREEN = "\033[32m"
_DIM = "\033[90m"
_BOLD = "\033[1m"
# Brand accent — Otto's magenta-pink (#0fb5bd), matching omnicraft.inner.ui so
# the setup picker's selection pointer reads as the same brand as the banner.
_ACCENT = "\033[38;2;244;59;166m"
_RESET = "\033[0m"
_CHECK = f"{_GREEN}\u2713{_RESET}"
_CROSS = f"{_DIM}\u2717{_RESET}"


class _GoBack(Exception):
    """Raised when the user presses Escape to go back one step."""


# ---------------------------------------------------------------------------
# Arrow-key menu helper
# ---------------------------------------------------------------------------


def _arrow_menu(
    options: list[str],
    *,
    default: int = 0,
    disabled: set[int] | None = None,
    multi: bool = False,
    allow_back: bool = True,
) -> int | list[int]:
    """Render an interactive arrow-key menu in the terminal.

    :param options: Display strings for each option.
    :param default: 0-based index of the initially highlighted option.
    :param disabled: Set of 0-based indices that cannot be selected.
    :param multi: If True, allow toggling multiple options with space;
        enter confirms. Returns a list of 0-based indices.
    :param allow_back: If True, Escape raises :class:`_GoBack`.
    :returns: Selected index (single) or list of indices (multi).
    :raises _GoBack: When the user presses Escape and allow_back is True.
    """
    disabled = disabled or set()
    cursor = default
    selected: set[int] = set()

    # Fall back to number input if not a real terminal.
    if not sys.stdin.isatty():
        return _arrow_menu_fallback(options, default=default, disabled=disabled, multi=multi)

    import select as _select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    def _read_key() -> str:
        """Read a single keypress, using a 50ms timeout to disambiguate Escape."""
        ch = os.read(fd, 1)
        if ch == b"\x1b":
            if _select.select([fd], [], [], 0.05)[0]:
                ch2 = os.read(fd, 1)
                if ch2 == b"[":
                    ch3 = os.read(fd, 1)
                    if ch3 == b"A":
                        return "up"
                    if ch3 == b"B":
                        return "down"
                    return "unknown"
                return "unknown"
            return "escape"
        if ch in (b"\r", b"\n"):
            return "enter"
        if ch == b" ":
            return "space"
        if ch == b"\x03":
            return "ctrl-c"
        if ch == b"\x04":
            return "ctrl-d"
        return "other"

    def _count_terminal_lines() -> int:
        """Count actual terminal lines including multi-line options."""
        count = 0
        for label in options:
            count += max(1, label.count("\n") + 1)
        return count + 1  # +1 for hint line

    def _render(*, clear: bool = False) -> None:
        total_lines = _count_terminal_lines()
        if clear:
            sys.stdout.write(f"\033[{total_lines}A")
            for _ in range(total_lines):
                sys.stdout.write("\033[2K\033[1B")
            sys.stdout.write(f"\033[{total_lines}A")

        for i, label in enumerate(options):
            pointer = f"{_ACCENT}>{_RESET}" if i == cursor else " "
            if multi:
                check = f"{_GREEN}*{_RESET}" if i in selected else " "
                prefix = f" {pointer} {check} "
            else:
                prefix = f" {pointer} "
            if i in disabled:
                # Non-selectable: render as-is with blank prefix
                # (empty strings become blank lines, labels keep
                # their own styling).
                if "\n" in label:
                    for sub in label.split("\n"):
                        sys.stdout.write(f"\033[2K    {sub}\n")
                else:
                    sys.stdout.write(f"\033[2K    {label}\n")
            elif "\n" in label:
                lines = label.split("\n")
                first = lines[0]
                if i == cursor:
                    first_line = f"{prefix}{_BOLD}{first}{_RESET}"
                else:
                    first_line = f"{prefix}{first}"
                sys.stdout.write(f"\033[2K{first_line}\n")
                for sub in lines[1:]:
                    sys.stdout.write(f"\033[2K    {sub}\n")
            else:
                if i == cursor:
                    line = f"{prefix}{_BOLD}{label}{_RESET}"
                else:
                    line = f"{prefix}{label}"
                sys.stdout.write(f"\033[2K{line}\n")
        # Hint line.
        hints = f"{_DIM}  \u2191\u2193 navegar"
        if multi:
            hints += "  espa\u00e7o selecionar/desmarcar  enter confirmar"
        else:
            hints += "  enter selecionar"
        if allow_back:
            hints += "  esc voltar"
        hints += _RESET
        sys.stdout.write(f"\033[2K{hints}\n")
        sys.stdout.flush()

    _render()

    try:
        # setcbreak: immediate key reads + no echo, but keeps output processing
        # (unlike setraw which breaks \n rendering).
        tty.setcbreak(fd)

        def _next_enabled(pos: int, direction: int) -> int:
            """Move cursor in *direction* (+1/-1), skipping disabled."""
            n = len(options)
            pos = (pos + direction) % n
            # Walk until we land on an enabled option (full loop = give up).
            for _ in range(n):
                if pos not in disabled:
                    return pos
                pos = (pos + direction) % n
            return pos

        while True:
            key = _read_key()
            if key == "up":
                cursor = _next_enabled(cursor, -1)
                _render(clear=True)
            elif key == "down":
                cursor = _next_enabled(cursor, 1)
                _render(clear=True)
            elif key == "space" and multi:
                if cursor not in disabled:
                    selected.symmetric_difference_update({cursor})
                    _render(clear=True)
            elif key == "enter":
                if multi:
                    result = sorted(selected)
                    if result:
                        return result
                else:
                    if cursor not in disabled:
                        return cursor
            elif key == "escape" and allow_back:
                raise _GoBack
            elif key in ("ctrl-c", "ctrl-d"):
                raise KeyboardInterrupt
    except KeyboardInterrupt:
        raise SystemExit(0) from None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _arrow_menu_fallback(
    options: list[str],
    *,
    default: int = 0,
    disabled: set[int] | None = None,
    multi: bool = False,
) -> int | list[int]:
    """Non-interactive fallback when stdin is not a tty."""
    disabled = disabled or set()
    for i, label in enumerate(options):
        marker = " [indisponível]" if i in disabled else ""
        console.print(f"  {i + 1}. {label}{marker}")
    console.print()

    if multi:
        while True:
            available = ",".join(str(i + 1) for i in range(len(options)) if i not in disabled)
            raw = str(click.prompt("Selecione (separado por vírgula)", default=available))
            try:
                indices = [int(x.strip()) - 1 for x in raw.split(",")]
                if all(0 <= i < len(options) and i not in disabled for i in indices) and indices:
                    return indices
            except ValueError:
                pass
            console.print("  [red]Seleção inválida.[/red]")
    else:
        while True:
            raw = str(click.prompt("Escolha", default=str(default + 1)))
            try:
                idx = int(raw) - 1
                if 0 <= idx < len(options) and idx not in disabled:
                    return idx
            except ValueError:
                pass
            console.print("  [red]Seleção inválida.[/red]")


# ---------------------------------------------------------------------------
# Text prompt with Esc support
# ---------------------------------------------------------------------------


def _text_prompt(
    label: str,
    *,
    default: str | None = None,
    allow_back: bool = True,
    hide_input: bool = False,
) -> str:
    """Prompt for a text value with Esc-to-go-back support.

    Shows a hint line (enter to confirm, esc to go back) beneath the
    prompt, matching the visual style of :func:`_arrow_menu`.

    :param label: Prompt label (printed before the input area).
    :param default: Default value shown in dim; returned on bare enter.
        ``None`` means no default -- bare enter returns an empty string.
    :param allow_back: If ``True``, Escape raises :class:`_GoBack`.
    :param hide_input: If ``True``, mask typed characters with ``*``
        (for secrets like API keys).
    :returns: The user's input (or *default* on bare enter).
    :raises _GoBack: When the user presses Escape and *allow_back* is True.
    """
    # Non-tty fallback -- just use click.prompt.
    if not sys.stdin.isatty():
        raw = str(
            click.prompt(
                label,
                default=default or "",
                show_default=bool(default),
                hide_input=hide_input,
            )
        )
        if not raw.strip() and not default:
            raise _GoBack
        return raw.strip() or default or ""

    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    buf: list[str] = []
    default_hint = f" {_DIM}({default}){_RESET}" if default is not None else ""

    def _render_line() -> None:
        text = "*" * len(buf) if hide_input else "".join(buf)
        sys.stdout.write(f"\r\033[2K  {label}: {text}{default_hint if not buf else ''}")
        sys.stdout.flush()

    def _render_hint() -> None:
        hints = f"\n{_DIM}  enter confirmar"
        if allow_back:
            hints += "  esc voltar"
        hints += _RESET
        sys.stdout.write(hints)
        sys.stdout.flush()
        # Move cursor back up to the input line.
        sys.stdout.write("\033[1A")
        # Reposition cursor after the typed text.
        col = len(f"  {label}: ") + len("".join(buf))
        sys.stdout.write(f"\r\033[{col}C")
        sys.stdout.flush()

    _render_line()
    _render_hint()

    try:
        tty.setcbreak(fd)
        while True:
            ch = os.read(fd, 1)
            if ch == b"\x1b":
                # Check for arrow sequence vs bare Escape.
                import select as _select

                if _select.select([fd], [], [], 0.05)[0]:
                    # Consume the rest of the escape sequence.
                    os.read(fd, 2)
                    continue
                if allow_back:
                    # Clear input line + hint line.
                    sys.stdout.write("\r\033[2K\n\033[2K\033[1A")
                    sys.stdout.flush()
                    raise _GoBack
            elif ch in (b"\r", b"\n"):
                # Clear the hint line below.
                sys.stdout.write("\n\033[2K\033[1A")
                sys.stdout.write("\r\033[2K")
                result = "".join(buf).strip() or default or ""
                # Reprint the final value cleanly.
                display = "*" * len(result) if hide_input else result
                sys.stdout.write(f"  {label}: {display}\n")
                sys.stdout.flush()
                return result
            elif ch in (b"\x7f", b"\x08"):
                if buf:
                    buf.pop()
                    _render_line()
                    _render_hint()
            elif ch in (b"\x03", b"\x04"):
                sys.stdout.write("\n\033[2K")
                sys.stdout.flush()
                raise KeyboardInterrupt
            elif ch and ch[0] >= 32:
                buf.append(ch.decode("utf-8", errors="replace"))
                _render_line()
                _render_hint()
    except KeyboardInterrupt:
        raise SystemExit(0) from None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


# CLI-based harnesses that wrap local binaries.
# Each needs a CLI binary on PATH to be usable.
_CLI_HARNESSES: dict[str, dict[str, str]] = {
    "claude-sdk": {
        "cli": "claude",
        "display": "Claude Code",
        "install": "npm install -g @anthropic-ai/claude-code",
    },
    "codex": {
        "cli": "codex",
        "display": "Codex",
        "install": "npm install -g @openai/codex",
    },
    "pi": {
        "cli": "pi",
        "display": "Pi",
        "install": "(veja a documentação do Pi)",
    },
}

# Pure-Python harnesses (no CLI binary needed, but require a Python package).
# Need API credentials (env var or Databricks profile).
_API_HARNESSES: dict[str, dict[str, str]] = {
    "openai-agents": {
        "display": "OpenAI Agents",
        "description": "API da OpenAI ou qualquer endpoint compatível com OpenAI",
        "package": "agents",
        "install": "pip install openai-agents",
    },
    "antigravity": {
        "display": "Antigravity (Gemini)",
        "description": "chave de API do Antigravity / Gemini, ou um gateway compatível com OpenAI",
        "package": "google.antigravity",
        "install": "pip install google-antigravity",
    },
}


def _detect_api_harnesses() -> dict[str, bool]:
    """Check which API harness packages are importable."""
    result = {}
    for harness, info in _API_HARNESSES.items():
        try:
            __import__(info["package"])
            result[harness] = True
        except ImportError:
            result[harness] = False
    return result


_AGENTS_DIR = Path.home() / ".omnicraft" / "agents"


@dataclass
class _AgentChoice:
    harness: str
    display: str


@dataclass
class _SupervisorConfig:
    """Configuration for the supervisor agent in a multi-agent setup.

    :param harness: Harness name, e.g. ``"openai-agents"`` or ``"claude-sdk"``.
    :param model: Model identifier, e.g. ``"gpt-4o"``. ``None`` for CLI
        harnesses that manage their own model selection.
    :param task: User-provided collaboration task description, or ``None``
        if the user skipped the prompt.
    :param profile: Databricks CLI profile name, or ``None``.
    :param base_url: Custom OpenAI-compatible base URL, or ``None``.
    :param api_key: OpenAI API key provided by the user, or ``None``
        (env var already set).
    """

    harness: str
    model: str | None = None
    task: str | None = None
    profile: str | None = None
    base_url: str | None = None
    api_key: str | None = None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _detect_coding_agents() -> dict[str, str | None]:
    """Return {harness_name: path_or_None} for each CLI-based harness."""
    return {name: shutil.which(info["cli"]) for name, info in _CLI_HARNESSES.items()}


def _list_databricks_profiles() -> list[str]:
    """Parse ~/.databrickscfg and return section names."""
    cfg_path = Path.home() / ".databrickscfg"
    if not cfg_path.exists():
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(cfg_path)
    except configparser.Error:
        return []
    return [s for s in parser.sections() if s != "DEFAULT"] or (
        ["DEFAULT"] if parser.defaults() else []
    )


def _sanitize_agent_name(name: str) -> str:
    """Sanitize an agent name for use as a YAML filename and spec name.

    Strips whitespace, lowercases, replaces non-alphanumeric chars with
    underscores, and collapses consecutive underscores.
    """
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9_]", "_", name)
    name = re.sub(r"_+", "_", name)
    name = name.strip("_")
    return name or "my_agent"


# ---------------------------------------------------------------------------
# Global credentials setup (auth + server URL)
# ---------------------------------------------------------------------------


def _prompt_global_auth() -> tuple[dict[str, str], None] | tuple[None, None]:
    """
    Interactive prompt for executor auth config.

    Shows two auth types:

    - ``api_key`` — direct OpenAI-compatible bearer token. Prompts for
      the actual key value (not an env-var reference) and an optional
      custom endpoint URL.
    - ``databricks`` — Databricks profile from ``~/.databrickscfg``.
      Detected profiles are shown as a hint; the user can type any name.

    Internal sub-steps with Esc-to-go-back:

    0. Auth type picker.
    1. Credentials for chosen type.

    :returns: ``(auth_dict, None)`` where *auth_dict* is the ``auth:``
        mapping to write to ``~/.omnicraft/config.yaml``, e.g.
        ``{"type": "api_key", "api_key": "sk-...", "base_url": "..."}``.
        Returns ``(None, None)`` when the user presses Escape at the
        top level (caller may skip auth configuration).
    """
    profiles = _list_databricks_profiles()
    sub = 0
    choice = 0
    auth_dict: dict[str, str] | None = None

    while True:
        try:
            if sub == 0:
                console.print()
                console.print("  [bold]Como o omnicraft vai autenticar com o LLM?[/bold]")
                console.print()

                # Menu label only (display text, not a credential). Named to
                # avoid an api_key/secret substring so CodeQL's clear-text
                # logging heuristic doesn't flag the menu render as leaking a
                # key (it's just the words "API key" on a button).
                direct_auth_label = "chave de API"
                if profiles:
                    profiles_hint = ", ".join(profiles[:3])
                    if len(profiles) > 3:
                        profiles_hint += f", +{len(profiles) - 3} mais"
                    db_label = (
                        f"Databricks\n        {_DIM}perfis detectados: {profiles_hint}{_RESET}"
                    )
                else:
                    db_label = (
                        f"Databricks\n"
                        f"        {_DIM}nenhum perfil em ~/.databrickscfg — "
                        f"você ainda pode digitar um nome de perfil{_RESET}"
                    )
                choice = _arrow_menu([direct_auth_label, db_label])
                sub = 1

            if sub == 1:
                if choice == 0:
                    # API key path
                    console.print()
                    console.print(
                        "  [dim]Dica: a chave é armazenada em ~/.omnicraft/config.yaml,"
                        " não no YAML do agente.[/dim]"
                    )
                    console.print()
                    api_key_val = _text_prompt("chave de API", hide_input=True)
                    if not api_key_val:
                        sub = 0
                        continue
                    console.print()
                    console.print(
                        "  [dim]Deixe em branco para usar o endpoint padrão da OpenAI"
                        " (https://api.openai.com/v1).[/dim]"
                    )
                    base_url_val = _text_prompt("Base URL (opcional)", default="")
                    auth_dict = {"type": "api_key", "api_key": api_key_val}
                    if base_url_val:
                        auth_dict["base_url"] = base_url_val
                else:
                    # Databricks path
                    console.print()
                    default_profile = profiles[0] if len(profiles) == 1 else None
                    if profiles:
                        hint = ", ".join(profiles)
                        console.print(f"  [dim]Perfis detectados: {hint}[/dim]")
                        console.print()
                    profile_val = _text_prompt(
                        "Nome do perfil do Databricks", default=default_profile
                    )
                    if not profile_val:
                        sub = 0
                        continue
                    auth_dict = {"type": "databricks", "profile": profile_val}
                return auth_dict, None

        except _GoBack:
            if sub <= 0:
                return None, None
            sub = 0


def _prompt_server_url(current: str | None) -> str | None:
    """
    Prompt for the OmniCraft server URL, or confirm the existing one.

    Skipped when *current* is already set and the user presses Enter to
    accept it. The user can type a new value to override.

    :param current: The server URL already in ``~/.omnicraft/config.yaml``,
        or ``None`` if not configured yet.
    :returns: The server URL the user confirmed or typed, or ``None`` if
        they pressed Escape without providing one.
    """
    console.print()
    if current:
        console.print(f"  [dim]URL do servidor já configurada: {current}[/dim]")
        console.print("  [dim]Pressione Enter para manter, ou digite uma nova URL.[/dim]")
        console.print()
        try:
            val = _text_prompt("URL do servidor", default=current)
            return val or current
        except _GoBack:
            return current
    console.print("  [bold]URL do servidor[/bold]")
    console.print(
        "  [dim]O servidor OmniCraft ao qual seus agentes se conectam."
        " Deixe em branco para rodar localmente (sem servidor).[/dim]"
    )
    console.print()
    try:
        val = _text_prompt("URL do servidor (opcional)", default="")
        return val or None
    except _GoBack:
        return None


# ---------------------------------------------------------------------------
# Section header helper
# ---------------------------------------------------------------------------


def _section() -> None:
    """Print a visual section separator between wizard steps."""
    console.print()
    console.rule(style="dim")
    console.print()


# ---------------------------------------------------------------------------
# Returning user shortcut
# ---------------------------------------------------------------------------


def _find_existing_configs() -> list[Path]:
    """Return YAML files in ~/.omnicraft/agents/, newest first."""
    if not _AGENTS_DIR.is_dir():
        return []
    return sorted(
        _AGENTS_DIR.glob("*.yaml"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _prompt_existing_or_new(configs: list[Path]) -> Path | None:
    """Offer to create a new agent or pick an existing one.

    Returns None to create new, or the path of an existing config.
    Escape in the agent picker returns to the create/run choice.
    """
    while True:
        console.print()
        console.print("[bold]O que você gostaria de fazer?[/bold]")
        console.print()
        choice = _arrow_menu(["Criar um novo OmniCraft", "Rodar um OmniCraft existente"])
        if choice == 0:
            return None

        try:
            console.print()
            console.print("[bold]Escolha um agente existente:[/bold]")
            console.print()
            labels = [p.stem for p in configs]
            labels.append("Digitar um caminho…")
            picked = _arrow_menu(labels)
            if picked == len(configs):
                return _prompt_agent_config_path()
            return configs[picked]
        except _GoBack:
            continue


def _prompt_agent_config_path() -> Path:
    """Prompt for a YAML agent config path and validate that it exists."""
    while True:
        raw = _text_prompt("Caminho do YAML do agente", default="", allow_back=True).strip()
        if not raw:
            raise _GoBack
        path = Path(raw).expanduser()
        if not path.exists():
            console.print(f"  [red]{path} não existe.[/red]")
            continue
        if not path.is_file():
            console.print(f"  [red]{path} não é um arquivo.[/red]")
            continue
        if path.suffix not in {".yaml", ".yml"}:
            console.print("  [red]Por favor, informe um arquivo .yaml ou .yml.[/red]")
            continue
        return path


# ---------------------------------------------------------------------------
# Step: Welcome
# ---------------------------------------------------------------------------


def _show_welcome() -> None:
    from omnicraft.inner.banner import startup_banner_strings
    from omnicraft.inner.mascots import MASCOT_ART_COLOR

    banner = startup_banner_strings(
        "Bem-vindo ao OmniCraft!",
        hint_line="pule quando quiser: omnicraft run <agent.yaml>",
        art_color=MASCOT_ART_COLOR,
    )
    console.print()
    sys.stdout.write(banner.ansi + "\n")
    console.print()
    console.print(
        "  O OmniCraft \u00e9 um framework declarativo de cria\u00e7\u00e3o e "
        "execu\u00e7\u00e3o de agentes."
    )
    console.print(
        "  Defina seu agente em uma configura\u00e7\u00e3o YAML e o framework cuida do resto."
    )
    console.print()
    console.print(
        "  [dim]\u2022[/dim] [bold]Declarativo[/bold]"
        " [dim]- descreva seu agente em YAML, n\u00e3o em c\u00f3digo[/dim]"
    )
    console.print(
        "  [dim]\u2022[/dim] [bold]Port\u00e1vel[/bold]   "
        " [dim]- rode em qualquer LLM ou harness"
        " (Claude Code, Codex, OpenAI Agents e mais)[/dim]"
    )
    console.print(
        "  [dim]\u2022[/dim] [bold]Implant\u00e1vel[/bold]"
        " [dim]- entregue como CLI, web UI, bot do Slack,"
        " API REST ou na nuvem[/dim]"
    )
    console.print(
        "  [dim]\u2022[/dim] [bold]Govern\u00e1vel[/bold] "
        " [dim]- adicione pol\u00edticas para permiss\u00f5es, aprova\u00e7\u00f5es"
        " e controles de custo[/dim]"
    )
    console.print(
        "  [dim]\u2022[/dim] [bold]Combin\u00e1vel[/bold] "
        " [dim]- use agentes como ferramentas, encadeie-os com"
        " supervisores ou envolva-os em c\u00f3digo[/dim]"
    )
    console.print()
    console.print(
        "  Este fluxo de configura\u00e7\u00e3o vai ajudar voc\u00ea a criar sua "
        "primeira configura\u00e7\u00e3o YAML."
    )
    console.print("  Quando estiver familiarizado, basta escrever a sua e rodar:")
    console.print("  [dim]omnicraft run <your-agent.yaml>[/dim]")
    console.print()
    console.print(
        "  [dim]Confira examples/ no reposit\u00f3rio para configura\u00e7\u00f5es "
        "de agente prontas para rodar.[/dim]"
    )


# ---------------------------------------------------------------------------
# Step: Use case
# ---------------------------------------------------------------------------


def _prompt_use_case() -> int:
    """Prompt for use case. Returns 1 (single), 2 (multi), or 3 (custom)."""
    console.print(
        "  Aqui estão dois cenários populares de agentes de código onde as "
        "pessoas acham o OmniCraft útil:"
    )
    console.print()
    options = [
        (
            f"Aprimorar um único agente de código\n"
            f"        {_DIM}Adicione ferramentas e guardrails melhores ao"
            f" Claude Code, Codex ou outros agentes de código{_RESET}"
        ),
        "",
        (
            f"Construir um sistema de código multiagente\n"
            f"        {_DIM}Vários agentes de código trabalhando juntos,"
            f" ex.: o Codex revisa o trabalho do Claude Code{_RESET}"
        ),
        "",
        "Ou construa algo diferente:",
        "",
        (
            f"Agente personalizado\n"
            f"        {_DIM}Não se limita a agentes de código;"
            f" defina qualquer (multi)agente com uma configuração guiada{_RESET}"
        ),
    ]
    disabled = {1, 3, 4, 5}  # blank lines and label are not selectable
    choice = _arrow_menu(options, disabled=disabled)
    # map indices: 0->1 (single), 2->2 (multi), 6->3 (custom)
    return {0: 1, 2: 2, 6: 3}[choice]


# ---------------------------------------------------------------------------
# Step: Agent naming
# ---------------------------------------------------------------------------


def _default_agent_name(use_case: int) -> str:
    """Generate a default agent name that doesn't collide with existing files.

    :param use_case: ``1`` for single agent (``my_coding_agent``),
        ``2`` for multi-agent (``my_coding_team``).
    """
    import random

    base = "my_coding_agent" if use_case == 1 else "my_coding_team"
    if not (_AGENTS_DIR / f"{base}.yaml").exists():
        return base
    for _ in range(100):
        candidate = f"{base}_{random.randint(1, 999)}"
        if not (_AGENTS_DIR / f"{candidate}.yaml").exists():
            return candidate
    return f"{base}_{random.randint(1000, 9999)}"


def _prompt_agent_name(use_case: int) -> str:
    """Ask the user to name their agent. Returns sanitized name.

    Rejects names that collide with existing YAML files in the agents dir.
    Raises _GoBack if the user presses Escape.
    """
    default = _default_agent_name(use_case)
    console.print(
        f"  [dim]Pressione enter para usar [bold]{default}[/bold], ou digite um nome."
        f"\n  O nome ser\u00e1 usado como nome do arquivo YAML"
        f" ({default} \u2192 {default}.yaml)[/dim]"
    )
    console.print()
    while True:
        raw = _text_prompt("Nomeie seu agente", default=default)
        sanitized = _sanitize_agent_name(raw)
        if sanitized != raw.strip():
            console.print(f"  [dim](ajustado para: {sanitized})[/dim]")
        existing_path = _AGENTS_DIR / f"{sanitized}.yaml"
        if existing_path.exists():
            console.print(
                f"  [red]{sanitized}.yaml j\u00e1 existe. Escolha um nome diferente.[/red]"
            )
            continue
        return sanitized


# ---------------------------------------------------------------------------
# Step: Detect + pick coding agents
# ---------------------------------------------------------------------------


def _build_agent_labels(
    detected: dict[str, str | None],
) -> tuple[list[str], set[int], list[str], bool]:
    """Build arrow-menu labels for CLI-based coding agents.

    Returns (labels, disabled_indices, harness_order, any_available).
    """
    labels = []
    disabled = set()
    any_available = False
    harness_order: list[str] = []

    for i, (harness, path) in enumerate(detected.items()):
        info = _CLI_HARNESSES[harness]
        harness_order.append(harness)
        if path:
            mark = _CHECK
            detail = f"{_DIM}encontrado em {path}{_RESET}"
            any_available = True
        else:
            mark = _CROSS
            detail = f"{_DIM}não encontrado ({info['install']}){_RESET}"
            disabled.add(i)
        labels.append(f"{mark} {info['display']:<18} {detail}")

    return labels, disabled, harness_order, any_available


def _show_no_agents_found(labels: list[str]) -> None:
    """Display a message when no coding agents are available."""
    for label in labels:
        console.print(f"  {label}")
    console.print()
    console.print(
        "  [yellow]Nenhum agente de código encontrado. Instale pelo menos um "
        "e tente de novo.[/yellow]"
    )


def _show_coding_agents_and_pick(detected: dict[str, str | None]) -> _AgentChoice | None:
    """Show CLI-based coding agents and let user pick one.

    Only shows Claude Code, Codex, and Pi (the actual coding agents).
    Returns None if none are available.
    """
    labels, disabled, harness_order, any_available = _build_agent_labels(detected)

    if not any_available:
        _show_no_agents_found(labels)
        return None

    first_available = next(i for i in range(len(labels)) if i not in disabled)
    choice = _arrow_menu(labels, default=first_available, disabled=disabled)
    harness = harness_order[choice]
    display = _CLI_HARNESSES[harness]["display"]

    console.print(f"\n  Selecionado: [bold]{display}[/bold]")
    return _AgentChoice(harness=harness, display=display)


def _show_coding_agents_and_pick_multi(
    detected: dict[str, str | None],
) -> list[_AgentChoice] | None:
    """Show CLI-based coding agents and let user pick multiple workers.

    Only shows Claude Code, Codex, and Pi. Returns ``None`` if none
    are available.

    :param detected: Mapping of CLI harness name to binary path (or
        ``None`` if not found).
    """
    labels, disabled, harness_order, any_available = _build_agent_labels(detected)

    if not any_available:
        _show_no_agents_found(labels)
        return None

    console.print(
        "  Quais agentes de código devem trabalhar juntos? Selecione os que "
        "você quer que colaborem."
    )
    console.print()
    first_available = next(i for i in range(len(labels)) if i not in disabled)
    chosen_indices = _arrow_menu(labels, default=first_available, disabled=disabled, multi=True)

    selected = []
    for i in chosen_indices:
        harness = harness_order[i]
        display = _CLI_HARNESSES[harness]["display"]
        selected.append(_AgentChoice(harness=harness, display=display))

    names = ", ".join(a.display for a in selected)
    console.print(f"\n  Workers: [bold]{names}[/bold]")
    return selected


# ---------------------------------------------------------------------------
# Step: Configure supervisor (multi-agent only)
# ---------------------------------------------------------------------------


def _prompt_supervisor(detected: dict[str, str | None]) -> _SupervisorConfig:
    """Prompt for supervisor task, harness, and credentials.

    Three internal sub-steps with Esc-to-go-back between them:

    0. Describe how the workers should collaborate (written to YAML prompt).
    1. Pick the supervisor harness.
    2. Configure harness-specific credentials.

    :param detected: Mapping of CLI harness name to binary path (or
        ``None`` if not found). Same dict returned by
        :func:`_detect_coding_agents`.
    :returns: Fully populated :class:`_SupervisorConfig`.
    :raises _GoBack: When the user presses Escape at sub-step 0.
    """
    # Internal sub-step index so Esc navigates back within this step
    # rather than bubbling up to the main wizard loop.
    #   0 = task description
    #   1 = harness picker
    #   2 = harness-specific config (credentials / model)
    sub = 0
    task = ""
    harness_name = ""

    while True:
        try:
            if sub == 0:
                # --- Sub-step 0: Task description ---
                console.print(
                    "  Seus agentes de código precisam de um supervisor para coordená-los."
                    " O supervisor é um agente que decide qual worker chamar"
                    " e quando."
                )
                console.print()
                console.print(
                    "  [bold]Como seus agentes devem colaborar?[/bold]"
                    " Descreva o fluxo de trabalho em linguagem simples."
                )
                console.print(
                    "  [dim]Por exemplo: Sempre peça ao Claude Code para escrever o código,"
                    " depois chame o Codex para revisá-lo."
                    "\n  Se o Codex encontrar problemas, envie-os de volta ao Claude Code"
                    " para corrigir.[/dim]"
                )
                console.print()
                task = _text_prompt("Tarefa de colaboração", allow_back=True)
                sub = 1

            if sub == 1:
                # --- Sub-step 1: Pick supervisor harness ---
                console.print()
                console.print("  [bold]Escolha um harness para o supervisor:[/bold]")
                console.print(
                    "  [dim]É com isso que seu agente supervisor vai rodar."
                    " Tudo bem usar um agente de código aqui também"
                    " - ele não vai conflitar com seus workers.[/dim]"
                )
                console.print()

                labels, disabled, harness_order, _ = _build_agent_labels(detected)

                api_available = _detect_api_harnesses()
                for harness, info in _API_HARNESSES.items():
                    idx = len(labels)
                    harness_order.append(harness)
                    if api_available.get(harness):
                        mark = _CHECK
                        detail = f"{_DIM}{info['description']}{_RESET}"
                    else:
                        mark = _CROSS
                        detail = f"{_DIM}pacote não instalado ({info['install']}){_RESET}"
                        disabled.add(idx)
                    labels.append(f"{mark} {info['display']:<18} {detail}")

                available_indices = [i for i in range(len(labels)) if i not in disabled]
                if not available_indices:
                    for label in labels:
                        console.print(f"  {label}")
                    console.print()
                    console.print(
                        "  [yellow]Nenhum harness disponível. Instale um agente"
                        " de código ou o pacote openai-agents,"
                        " e tente de novo.[/yellow]"
                    )
                    raise SystemExit(1)

                choice = _arrow_menu(labels, default=available_indices[0], disabled=disabled)
                harness_name = harness_order[choice]
                sub = 2

            if sub == 2:
                # --- Sub-step 2: Harness-specific config ---
                if harness_name in _CLI_HARNESSES:
                    config = _prompt_cli_supervisor_config(harness_name)
                else:
                    config = _prompt_openai_agents_config()
                config.task = task
                return config

        except _GoBack:
            if sub <= 0:
                # Already at the first sub-step -- let it bubble up
                # to the main wizard loop to go back a full step.
                raise
            sub -= 1


def _prompt_cli_supervisor_config(harness_name: str) -> _SupervisorConfig:
    """Configure a CLI-based harness as supervisor.

    CLI harnesses manage their own model selection, so no model or
    credentials are needed.

    :param harness_name: Harness identifier, e.g. ``"claude-sdk"``.
    """
    return _SupervisorConfig(harness=harness_name)


def _prompt_openai_agents_config() -> _SupervisorConfig:
    """Configure the openai-agents supervisor.

    Shows three endpoint options (OpenAI API, custom endpoint,
    Databricks profile) with credential detection status inline,
    then drills into credential prompts for the chosen option.

    Internal sub-steps with Esc-to-go-back:

    0. Endpoint type picker (OpenAI / custom / Databricks).
    1. Credentials for chosen endpoint (API key, base URL, profile).
    2. Model picker.

    :returns: Fully populated :class:`_SupervisorConfig` with
        ``harness="openai-agents"``.
    :raises _GoBack: When the user presses Escape at sub-step 0.
    """
    has_key = bool(os.environ.get("OPENAI_API_KEY"))
    has_base = bool(os.environ.get("OPENAI_BASE_URL"))
    profiles = _list_databricks_profiles()

    sub = 0
    choice = 0
    base_url: str | None = None
    api_key: str | None = None
    profile: str | None = None

    while True:
        try:
            if sub == 0:
                # --- Pick endpoint type ---
                base_url = None
                api_key = None
                profile = None

                console.print()
                console.print("  [bold]Como o supervisor deve acessar um LLM?[/bold]")
                console.print()

                # OpenAI API option.
                if has_key:
                    openai_detail = f"{_DIM}OPENAI_API_KEY detectada{_RESET}"
                else:
                    openai_detail = f"{_DIM}vai precisar de OPENAI_API_KEY{_RESET}"
                openai_label = f"OpenAI API\n        {openai_detail}"

                # Custom endpoint option.
                custom_parts = []
                if has_base:
                    custom_parts.append("OPENAI_BASE_URL detectada")
                else:
                    custom_parts.append("vai precisar de OPENAI_BASE_URL")
                if has_key:
                    custom_parts.append("OPENAI_API_KEY detectada")
                else:
                    custom_parts.append("vai precisar de OPENAI_API_KEY")
                custom_label = (
                    f"Endpoint personalizado\n"
                    f"        {_DIM}qualquer URL que fale a API"
                    f" Responses da OpenAI"
                    f" -- {', '.join(custom_parts)}{_RESET}"
                )

                # Databricks profile option.
                if profiles:
                    profiles_hint = ", ".join(profiles)
                    db_label = (
                        f"Databricks\n"
                        f"        {_DIM}{len(profiles)}"
                        f" {'perfis' if len(profiles) != 1 else 'perfil'}"
                        f" {'detectados' if len(profiles) != 1 else 'detectado'}:"
                        f" {profiles_hint}{_RESET}"
                    )
                else:
                    db_label = (
                        f"Databricks\n        {_DIM}nenhum perfil encontrado "
                        f"em ~/.databrickscfg{_RESET}"
                    )

                labels = [openai_label, custom_label, db_label]
                disabled: set[int] = set()
                if not profiles:
                    disabled.add(2)
                choice = _arrow_menu(labels, disabled=disabled)
                sub = 1

            if sub == 1:
                # --- Credentials for chosen endpoint ---
                if choice == 0:
                    if not has_key:
                        console.print()
                        api_key = _text_prompt("OPENAI_API_KEY", hide_input=True)
                elif choice == 1:
                    console.print()
                    if has_base:
                        base_url = os.environ["OPENAI_BASE_URL"]
                        console.print(f"  Usando OPENAI_BASE_URL: [bold]{base_url}[/bold]")
                    else:
                        base_url = _text_prompt("OPENAI_BASE_URL")
                    if not has_key:
                        console.print()
                        api_key = _text_prompt("OPENAI_API_KEY", hide_input=True)
                else:
                    console.print()
                    if len(profiles) == 1:
                        profile = profiles[0]
                        console.print(f"  Usando perfil: [bold]{profile}[/bold]")
                    else:
                        console.print("  [bold]Escolha um perfil do Databricks:[/bold]")
                        console.print()
                        pidx = _arrow_menu(list(profiles))
                        profile = profiles[pidx]
                sub = 2

            if sub == 2:
                # --- Pick model ---
                default_model = "databricks-gpt-5-4" if profile else "gpt-4o"
                console.print()
                console.print("  [bold]Qual modelo o supervisor deve usar?[/bold]")
                model = _text_prompt("Modelo do supervisor", default=default_model)

                return _SupervisorConfig(
                    harness="openai-agents",
                    model=model,
                    base_url=base_url,
                    api_key=api_key,
                    profile=profile,
                )

        except _GoBack:
            if sub <= 0:
                raise
            sub = 0


# ---------------------------------------------------------------------------
# YAML generation
# ---------------------------------------------------------------------------


def _generate_single_agent_yaml(agent_name: str, agent: _AgentChoice) -> str:
    """Generate a minimal single-agent YAML spec."""
    return textwrap.dedent(f"""\
        name: {agent_name}
        prompt: |
          You are a coding assistant working in the current directory.
          Use your tools to read, edit, and run code.

        executor:
          harness: {agent.harness}
    """)


def _generate_multi_agent_yaml(
    agent_name: str,
    workers: list[_AgentChoice],
    supervisor: _SupervisorConfig,
) -> str:
    """Generate a multi-agent supervisor YAML spec."""
    if supervisor.task:
        prompt_lines = [
            "  You are a coding supervisor coordinating work through multiple workers.",
            f"  Your task: {supervisor.task}",
            "  Delegate substantial work through persistent subagent sessions using",
            "  sys_session_send. You will be automatically notified when they finish.",
            "  Use your own OS tools only for tiny, fast actions that unblock delegation.",
        ]
    else:
        prompt_lines = [
            "  You are a coding supervisor coordinating work through multiple workers.",
            "  Delegate substantial work through persistent subagent sessions using",
            "  sys_session_send. You will be automatically notified when they finish.",
            "  Use your own OS tools only for tiny, fast actions that unblock delegation.",
        ]

    lines = [
        f"name: {agent_name}",
        "prompt: |",
        *prompt_lines,
        "",
        "executor:",
        f"  harness: {supervisor.harness}",
    ]
    if supervisor.model:
        lines.append(f"  model: {supervisor.model}")
    if supervisor.profile:
        # Emit the typed auth block (deprecated bare `profile:` key replaced).
        lines += [
            "  auth:",
            "    type: databricks",
            f"    profile: {supervisor.profile}",
        ]
    elif supervisor.api_key:
        # api_key: use an env-var reference so the secret stays out of the file.
        lines += [
            "  auth:",
            "    type: api_key",
            "    api_key: $OPENAI_API_KEY",
        ]
    lines += [
        "",
        "async: true",
        "cancellable: true",
        "",
        "os_env:",
        "  type: caller_process",
        "  cwd: .",
        "  sandbox:",
        "    type: none",
        "",
        "tools:",
    ]

    for worker in workers:
        safe_name = worker.harness.replace("-", "_") + "_worker"
        lines.extend(
            [
                f"  {safe_name}:",
                "    type: agent",
                "    description: >-",
                f"      {worker.display} coding worker for investigation and coding.",
                "    prompt: |",
                "      You are a coding worker operating inside the current repository.",
                "      Investigate carefully and summarize findings precisely.",
                "    max_sessions: 4",
                "    os_env: inherit",
                "    executor:",
                f"      harness: {worker.harness}",
                "",
            ]
        )

    return "\n".join(lines)


def _save_yaml(content: str, filename: str) -> Path:
    """Write YAML content to ~/.omnicraft/agents/<filename>."""
    _AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _AGENTS_DIR / filename
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------


def _store_default_config(yaml_path: Path, supervisor: _SupervisorConfig | None = None) -> None:
    """
    Store the generated agent as the global default and persist auth.

    Writes ``default_agent`` and, when *supervisor* carries auth info,
    the ``auth:`` block so agents that omit ``executor.auth`` inherit
    credentials from ``~/.omnicraft/config.yaml``.

    :param yaml_path: Absolute path to the generated agent YAML.
    :param supervisor: Optional supervisor config from the wizard.
        When provided and it carries ``profile`` or ``api_key``,
        the matching ``auth:`` block is written to the global config.
    """
    from omnicraft.cli import _GLOBAL_CONFIG_PATH, _save_global_config

    settings: dict[str, str | dict[str, str]] = {"default_agent": str(yaml_path)}  # type: ignore[assignment]  # str | dict union: starts as dict[str, str], may later hold dict[str, str] values
    if supervisor is not None:
        if supervisor.profile:
            settings["auth"] = {"type": "databricks", "profile": supervisor.profile}
        elif supervisor.api_key:
            settings["auth"] = {"type": "api_key", "api_key": "$OPENAI_API_KEY"}
    _save_global_config(settings)
    console.print(f"  [green]✓ default_agent armazenado em {_GLOBAL_CONFIG_PATH}[/green]")
    console.print("  [dim]Digite `omnicraft` para iniciar uma nova sessão.[/dim]\n")


def _finish_new_setup(
    yaml_path: Path,
    yaml_content: str,
    supervisor: _SupervisorConfig | None = None,
) -> None:
    """
    Show YAML preview, store defaults, and tell the user how to start.

    :param yaml_path: Absolute path to the generated agent YAML.
    :param yaml_content: YAML text to display in the preview panel.
    :param supervisor: Optional supervisor config whose auth is persisted
        into ``~/.omnicraft/config.yaml`` as the global default.
    """
    console.print()
    file_uri = yaml_path.resolve().as_uri()
    console.print(f"  Criado: [bold][link={file_uri}]{yaml_path}[/link][/bold]")

    # Show YAML preview in a syntax-highlighted panel.
    syntax = Syntax(yaml_content, "yaml", theme="ansi_dark", line_numbers=False)
    console.print()
    console.print(
        Panel(syntax, title="Prévia da configuração do agente", border_style="dim", expand=False)
    )
    console.print()
    console.print(
        "  [dim]Dica: Edite este YAML diretamente para mudar o harness,"
        " o modelo, adicionar políticas, ferramentas e mais.[/dim]\n"
        f"  [dim]Rode quando quiser com:[/dim] omnicraft run [link={file_uri}]{yaml_path}[/link]\n"
        "  [dim]Veja exemplos:[/dim] omnicraft/examples/ no repositório, ou omnicraft run --help\n"
    )

    _store_default_config(yaml_path, supervisor=supervisor)


def _finish_existing_setup(yaml_path: Path) -> None:
    """Store an existing agent config as the global default."""
    console.print()
    file_uri = yaml_path.resolve().as_uri()
    console.print(f"  Selecionado: [bold][link={file_uri}]{yaml_path}[/link][/bold]\n")
    _store_default_config(yaml_path)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_wizard_and_launch() -> None:
    """
    Run the simplified first-time setup flow.

    Asks for three things in order, then writes them to
    ``~/.omnicraft/config.yaml``:

    1. **Server URL** — the OmniCraft server to connect to (optional;
       blank means run locally).
    2. **Auth** — ``api_key`` (bearer token + optional base URL) or
       ``databricks`` (profile name). When ``type: databricks``, the
       same profile is reused automatically for OmniCraft server OAuth so no
       separate ``profile:`` key is needed.
    3. **Agent YAML** — path to the agent spec file that becomes
       ``default_agent`` so ``omnicraft run`` uses it without an
       argument.

    All three prompts are skippable by pressing Enter or Escape; the
    user can re-run ``omnicraft setup --no-internal-beta`` at any time
    to update the values.
    """
    from omnicraft.cli import _GLOBAL_CONFIG_PATH, _load_global_config, _save_global_config

    _show_welcome()

    global_cfg = _load_global_config()
    existing_server = str(global_cfg.get("server") or "") or None
    existing_auth = global_cfg.get("auth")
    existing_agent = str(global_cfg.get("default_agent") or "") or None

    save_settings: dict[str, str | dict[str, str]] = {}

    # ── Step 1: Server URL ────────────────────────────────────────────
    _section()
    console.print("  [bold]Passo 1 / 3 — URL do servidor[/bold]")
    server_url = _prompt_server_url(existing_server)
    if server_url:
        save_settings["server"] = server_url

    # ── Step 2: LLM executor auth ─────────────────────────────────────
    # When auth.type == "databricks", the same profile is also used to
    # authenticate with the OmniCraft server, so no separate ``profile:`` key
    # is needed in the global config.
    _section()
    console.print("  [bold]Passo 2 / 3 — Autenticação do LLM[/bold]")
    if existing_auth and isinstance(existing_auth, dict):
        auth_type = existing_auth.get("type", "?")
        console.print()
        console.print(f"  [dim]Autenticação do LLM já configurada: type={auth_type}.[/dim]")
        console.print()
        try:
            reconfig = _arrow_menu(["Manter autenticação existente", "Reconfigurar"])
        except _GoBack:
            reconfig = 0  # treat Escape as "keep existing"
        if reconfig == 1:
            auth_dict, _ = _prompt_global_auth()
            if auth_dict is not None:
                save_settings["auth"] = auth_dict
    else:
        auth_dict, _ = _prompt_global_auth()
        if auth_dict is not None:
            save_settings["auth"] = auth_dict

    # ── Step 3: Default agent YAML ───────────────────────────────────
    _section()
    console.print("  [bold]Passo 3 / 3 — YAML do agente padrão[/bold]")
    console.print()
    if existing_agent:
        console.print(f"  [dim]Agente padrão já definido: {existing_agent}[/dim]")
        console.print("  [dim]Pressione Enter para manter, ou digite um novo caminho.[/dim]")
        console.print()
    else:
        console.print(
            "  [dim]Caminho para o arquivo YAML do seu agente "
            "(ex.: examples/hello_world.yaml).[/dim]"
        )
        console.print(
            "  [dim]Deixe em branco para pular — rode ``omnicraft run <yaml>`` "
            "diretamente mais tarde.[/dim]"
        )
        console.print()
    try:
        agent_path_raw = _text_prompt(
            "Caminho do YAML do agente (opcional)", default=existing_agent
        )
        if agent_path_raw:
            agent_path = str(Path(agent_path_raw).expanduser().resolve())
            save_settings["default_agent"] = agent_path
    except _GoBack:
        pass

    # ── Persist ───────────────────────────────────────────────────────
    console.print()
    console.rule("[bold]Concluído![/bold]", style="green")
    console.print()
    if save_settings:
        _save_global_config(save_settings)
        console.print(f"  [green]✓ Configuração salva em {_GLOBAL_CONFIG_PATH}[/green]")
    else:
        console.print("  [dim]Nenhuma mudança — configuração inalterada.[/dim]")
    console.print()
    if save_settings.get("default_agent"):
        console.print(
            f"  Rode seu agente:  [bold]omnicraft run {save_settings['default_agent']}[/bold]"
        )
    else:
        console.print("  Rode um agente:   [bold]omnicraft run <your-agent.yaml>[/bold]")
    console.print("  Veja exemplos:    [bold]omnicraft/examples/[/bold] no repositório")
    console.print()
