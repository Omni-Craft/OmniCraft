"""Login-shell PATH resolution for daemon processes.

A host daemon launched by the desktop app (or launchd) inherits the bare
GUI PATH — no nvm, often no Homebrew. Worker CLIs installed from the
user's terminal (``npm install -g @openai/codex`` under nvm) then read as
``binary-missing`` even though the user can run them fine, and
orchestrators tell the user to "install" something that is installed
(observed live with codex under ``~/.nvm``).

The fix every desktop dev tool ships (VS Code, JetBrains, Claude
Desktop): ask the user's login shell for its real ``PATH`` once at boot
and adopt it. A fallback list of well-known tool directories covers the
case where the login shell itself fails (broken rc file, timeout).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

_logger = logging.getLogger(__name__)

# Bounded: a broken rc file that blocks forever must not wedge daemon boot.
_LOGIN_SHELL_TIMEOUT_S = 8.0

# Markers so rc-file noise (echoes, prompts, motds) can't corrupt parsing.
_MARKER = "__OMNICRAFT_PATH__"

# Version-manager and package-manager bin dirs users actually install
# CLIs into. Only existing directories are appended.
_WELL_KNOWN_DIRS = (
    "~/.local/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.volta/bin",
    "~/.bun/bin",
    "~/.cargo/bin",
    "~/.deno/bin",
    "~/go/bin",
)


def resolve_login_shell_path(timeout_s: float = _LOGIN_SHELL_TIMEOUT_S) -> str | None:
    """
    Ask the user's login shell for its real ``PATH``.

    Runs ``$SHELL -ilc 'echo <marker>$PATH<marker>'`` with a dumb
    terminal and a hard timeout, then extracts the value between the
    markers so rc-file output can't pollute it.

    :param timeout_s: Hard deadline for the shell, e.g. ``8.0``.
    :returns: The login shell's ``PATH`` string, or ``None`` when the
        shell fails, times out, or produces no parseable marker.
    """
    shell = os.environ.get("SHELL") or "/bin/zsh"
    env = dict(os.environ)
    env["TERM"] = "dumb"
    try:
        proc = subprocess.run(
            [shell, "-ilc", f'printf "{_MARKER}%s{_MARKER}" "$PATH"'],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _logger.warning("login-shell PATH probe failed: %s", exc)
        return None
    match = re.search(f"{_MARKER}(.*?){_MARKER}", proc.stdout, flags=re.DOTALL)
    if match is None:
        _logger.warning(
            "login-shell PATH probe produced no marker (rc=%s)",
            proc.returncode,
        )
        return None
    value = match.group(1).strip()
    return value or None


def _nvm_bin_dirs() -> list[str]:
    """
    Locate installed nvm node bin dirs, newest version first.

    :returns: Existing ``~/.nvm/versions/node/<v>/bin`` paths, e.g.
        ``["/Users/a/.nvm/versions/node/v22.22.3/bin"]``.
    """
    root = Path.home() / ".nvm" / "versions" / "node"
    if not root.is_dir():
        return []

    def _version_key(p: Path) -> tuple[int, ...]:
        nums = re.findall(r"\d+", p.name)
        return tuple(int(n) for n in nums) if nums else (0,)

    versions = sorted(
        (p for p in root.iterdir() if p.is_dir()),
        key=_version_key,
        reverse=True,
    )
    return [str(v / "bin") for v in versions if (v / "bin").is_dir()]


def _fallback_dirs() -> list[str]:
    """
    Well-known tool bin dirs that exist on this machine.

    :returns: Absolute existing paths, nvm (newest first) included.
    """
    dirs = [str(Path(d).expanduser()) for d in _WELL_KNOWN_DIRS]
    dirs.extend(_nvm_bin_dirs())
    return [d for d in dirs if Path(d).is_dir()]


def merged_path(
    login_path: str | None,
    current_path: str | None,
    extra_dirs: list[str],
) -> str:
    """
    Merge PATH sources, order-preserving and deduplicated.

    Login shell first (the user's source of truth), then the current
    process PATH (never lose what we already had), then fallbacks.

    :param login_path: The login shell's PATH, or ``None``.
    :param current_path: The process's current PATH, or ``None``.
    :param extra_dirs: Well-known fallback dirs, e.g. ``["~/.local/bin"]``.
    :returns: A single ``:``-joined PATH string.
    """
    seen: set[str] = set()
    out: list[str] = []
    for source in (login_path, current_path):
        for part in (source or "").split(":"):
            if part and part not in seen:
                seen.add(part)
                out.append(part)
    for part in extra_dirs:
        if part and part not in seen:
            seen.add(part)
            out.append(part)
    return ":".join(out)


def augment_path_from_login_shell() -> str:
    """
    Adopt the user's real PATH into this process, best-effort.

    Sets ``os.environ["PATH"]`` to the merge of login-shell PATH,
    current PATH, and well-known tool dirs. Never raises — on total
    failure the current PATH simply gains the existing fallback dirs.

    :returns: The final PATH now in effect.
    """
    login = resolve_login_shell_path()
    final = merged_path(login, os.environ.get("PATH"), _fallback_dirs())
    os.environ["PATH"] = final
    _logger.info(
        "PATH resolved for daemon: login_shell=%s, %d entries",
        "ok" if login else "unavailable",
        len(final.split(":")),
    )
    return final
