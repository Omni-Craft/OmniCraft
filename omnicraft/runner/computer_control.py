"""Computer control — drive the runner host's screen, pointer and keyboard.

Runner-local execution for the ``computer`` builtin tool. Where the browser
tools only reach inside the desktop app's embedded WebContentsView, this drives
the actual Mac: ``screencapture`` for the screen and ``cliclick`` for pointer
and keyboard input. Screenshots are saved into the session workspace and only
the path is returned — a raw image in a tool result costs a fortune in tokens.

This is the highest-blast-radius tool in the tree: it can click anything the
signed-in user can click. It is opt-in per agent spec, and the shipped policy
requires per-action approval before any of it runs.

Coordinates are the SCREENSHOT's pixels, because that is what the model reads.
``cliclick`` works in screen points, so a Retina display needs the capture
scaled down (a 2x screen shoots 3420px wide but clicks at 1710pt) — the same
class of bug already handled for the simulator's taps.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from omnicraft.runner.host_shell import shell_out as _shell
from omnicraft.runner.host_shell import tail as _tail

# Keys ``cliclick`` presses by name (kp:). Anything else single-character is
# typed instead (t:), which covers letters/digits in combos like ``cmd+s``.
_NAMED_KEYS = frozenset(
    {
        "arrow-down",
        "arrow-left",
        "arrow-right",
        "arrow-up",
        "delete",
        "end",
        "esc",
        "fwd-delete",
        "home",
        "page-down",
        "page-up",
        "return",
        "space",
        "tab",
        *(f"f{i}" for i in range(1, 17)),
        *(f"num-{i}" for i in range(10)),
    }
)
_MODIFIERS = frozenset({"cmd", "ctrl", "alt", "shift", "fn"})

# cliclick verbs for the plain pointer actions, keyed by tool action.
_POINTER_VERBS = {"click": "c", "double_click": "dc", "right_click": "rc", "move": "m"}

# Screen points per screenshot pixel, resolved once per process. ``None`` until
# the first capture (or an explicit probe) establishes it.
_scale: tuple[float, float] | None = None


def _cliclick_missing(action: str) -> str | None:
    """Actionable hint when cliclick isn't installed, else ``None``."""
    if shutil.which("cliclick") is not None:
        return None
    return (
        f"Erro: '{action}' precisa do cliclick, que injeta mouse/teclado no "
        "macOS — o sistema não expõe isso por linha de comando. Instale com "
        "`brew install cliclick`. Depois conceda Acessibilidade ao processo do "
        "runner em Ajustes → Privacidade e Segurança → Acessibilidade."
    )


async def _display_points() -> tuple[int, int] | None:
    """Desktop size in points, via the Finder's desktop window bounds."""
    res = await _shell(
        ["osascript", "-e", 'tell application "Finder" to get bounds of window of desktop']
    )
    if not res.ok:
        return None
    parts = [p.strip() for p in res.stdout.strip().split(",")]
    if len(parts) != 4:
        return None
    try:
        return int(parts[2]), int(parts[3])
    except ValueError:
        return None


def _pixels_of(path: Path) -> tuple[int, int] | None:
    """Pixel dimensions of a PNG, or ``None`` when it can't be read."""
    try:
        from PIL import Image

        with Image.open(path) as im:
            return im.size
    except Exception:  # noqa: BLE001 — a bad/missing capture must not raise here
        return None


def _remember_scale(points: tuple[int, int] | None, pixels: tuple[int, int] | None) -> None:
    """Cache the points-per-pixel ratio when both measurements are usable."""
    global _scale
    if not points or not pixels or not pixels[0] or not pixels[1]:
        return
    _scale = (points[0] / pixels[0], points[1] / pixels[1])


async def _ensure_scale() -> tuple[float, float]:
    """Points-per-pixel for the display, probing with a throwaway capture once.

    Falls back to 1.0 (assume the model already speaks points) when either
    measurement is unavailable, so a click still lands somewhere sane instead of
    failing outright.
    """
    if _scale is not None:
        return _scale
    points = await _display_points()
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.png"
        res = await _shell(["screencapture", "-x", str(probe)])
        pixels = _pixels_of(probe) if res.ok and probe.exists() else None
    _remember_scale(points, pixels)
    return _scale if _scale is not None else (1.0, 1.0)


async def _screenshot(workspace: Path | None) -> str:
    out_dir = (workspace or Path.cwd()) / ".omnicraft" / "computer"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"screen-{int(time.time() * 1000)}.png"
    # -x silences the shutter sound; a capture is not a user-facing event here.
    res = await _shell(["screencapture", "-x", str(out_path)])
    if not res.ok or not out_path.exists():
        return (
            f"Erro ao capturar a tela: {_tail(res.stderr or res.stdout)}\n"
            "Se estiver vazio, conceda Gravação de Tela ao processo do runner em "
            "Ajustes → Privacidade e Segurança → Gravação de Tela."
        )
    pixels = _pixels_of(out_path)
    if _scale is None:
        _remember_scale(await _display_points(), pixels)
    size = f" ({pixels[0]}x{pixels[1]}px)" if pixels else ""
    return f"Screenshot salvo: {out_path}{size} — leia o arquivo para ver a tela."


async def _pointer(action: str, args: dict[str, Any]) -> str:
    try:
        x, y = int(args["x"]), int(args["y"])
    except (KeyError, TypeError, ValueError):
        return f"Erro: '{action}' precisa de 'x' e 'y' inteiros (pixels do screenshot)."
    hint = _cliclick_missing(action)
    if hint:
        return hint
    sx, sy = await _ensure_scale()
    px, py = round(x * sx), round(y * sy)
    res = await _shell(["cliclick", f"{_POINTER_VERBS[action]}:{px},{py}"])
    if res.ok:
        return f"{action} em ({x}, {y})."
    return f"Erro ({action}): {_tail(res.stderr or res.stdout)}"


async def _drag(args: dict[str, Any]) -> str:
    try:
        x1, y1 = int(args["x"]), int(args["y"])
        x2, y2 = int(args["to_x"]), int(args["to_y"])
    except (KeyError, TypeError, ValueError):
        return "Erro: 'drag' precisa de 'x','y','to_x','to_y' inteiros."
    hint = _cliclick_missing("drag")
    if hint:
        return hint
    sx, sy = await _ensure_scale()
    a = (round(x1 * sx), round(y1 * sy))
    b = (round(x2 * sx), round(y2 * sy))
    res = await _shell(["cliclick", f"dd:{a[0]},{a[1]}", f"dm:{b[0]},{b[1]}", f"du:{b[0]},{b[1]}"])
    if res.ok:
        return f"Arrastou de ({x1}, {y1}) para ({x2}, {y2})."
    return f"Erro (drag): {_tail(res.stderr or res.stdout)}"


def key_argv(combo: str) -> list[str] | None:
    """Translate a combo like ``cmd+s`` or ``return`` into cliclick arguments.

    :param combo: Modifier-prefixed key, e.g. ``"cmd+shift+4"`` or ``"esc"``.
    :returns: cliclick argument list, or ``None`` when the combo is unusable.
    """
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        return None
    *mods, key = parts
    if any(m not in _MODIFIERS for m in mods):
        return None
    if key in _NAMED_KEYS:
        press = f"kp:{key}"
    elif len(key) == 1:
        press = f"t:{key}"
    else:
        return None
    if not mods:
        return [press]
    joined = ",".join(mods)
    return [f"kd:{joined}", press, f"ku:{joined}"]


async def _key(args: dict[str, Any]) -> str:
    combo = str(args.get("keys") or "").strip()
    if not combo:
        return "Erro: 'key' precisa de 'keys' (ex.: 'cmd+s', 'return', 'page-down')."
    argv = key_argv(combo)
    if argv is None:
        return (
            f"Erro: combo inválido {combo!r}. Modificadores: "
            f"{', '.join(sorted(_MODIFIERS))}; teclas nomeadas incluem return, esc, "
            "tab, space, page-up, page-down, arrow-*."
        )
    hint = _cliclick_missing("key")
    if hint:
        return hint
    res = await _shell(["cliclick", *argv])
    if res.ok:
        return f"Tecla: {combo}."
    return f"Erro (key): {_tail(res.stderr or res.stdout)}"


async def _type(args: dict[str, Any]) -> str:
    text = str(args.get("text") or "")
    if not text:
        return "Erro: 'type' precisa de 'text'."
    hint = _cliclick_missing("type")
    if hint:
        return hint
    res = await _shell(["cliclick", f"t:{text}"])
    if res.ok:
        return f"Digitou {len(text)} caracteres."
    return f"Erro (type): {_tail(res.stderr or res.stdout)}"


async def run_action(action: str, args: dict[str, Any], *, workspace: Path | None) -> str:
    """Dispatch one ``computer`` action to its host command.

    :param action: One of the supported action names (``screenshot``, ``click`` …).
    :param args: Parsed LLM arguments for the action.
    :param workspace: Session workspace; screenshots land under it.
    :returns: A compact string result for the model.
    """
    if action == "screenshot":
        return await _screenshot(workspace)
    if action in _POINTER_VERBS:
        return await _pointer(action, args)
    if action == "drag":
        return await _drag(args)
    if action == "type":
        return await _type(args)
    if action == "key":
        return await _key(args)
    if action == "open_app":
        app = str(args.get("app") or "").strip()
        if not app:
            return "Erro: 'open_app' precisa de 'app' (nome do aplicativo)."
        res = await _shell(["open", "-a", app])
        return f"Abriu {app}." if res.ok else f"Erro (open_app): {_tail(res.stderr)}"
    if action == "open_url":
        url = str(args.get("url") or "").strip()
        if not url:
            return "Erro: 'open_url' precisa de 'url'."
        res = await _shell(["open", url])
        return f"Abriu {url}." if res.ok else f"Erro (open_url): {_tail(res.stderr)}"
    return (
        f"Erro: ação desconhecida '{action}'. Use screenshot/click/double_click/"
        "right_click/move/drag/type/key/open_app/open_url."
    )
