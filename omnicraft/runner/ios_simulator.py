"""iOS Simulator control — drive ``xcrun simctl`` / ``xcodebuild`` from the runner host.

Runner-local execution for the ``ios_simulator`` builtin tool. The simulator
lives on the machine with Xcode (the runner host), so the tool shells out to
``simctl`` and ``xcodebuild`` directly here — no server bridge, unlike the
browser tools. Screenshots are saved into the session workspace
(``<workspace>/.omnicraft/ios/``) and only the path is returned: a raw image in
a tool result would cost hundreds of thousands of tokens.

Touch input (tap/type/swipe) is not something ``simctl`` exposes. It goes
through ``idb`` (fb-idb) when that is installed; otherwise it falls back to
clicking the Simulator window on screen with ``cliclick``. The fallback exists
because idb is archived and no longer installable on recent macOS, where it
would otherwise leave touch input dead.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from omnicraft.runner.host_shell import shell_out as _shell
from omnicraft.runner.host_shell import tail as _tail

# A device reference the caller passes: a UDID, a device name ("iPhone 17 Pro"),
# or the sentinel "booted". Matched against this to tell a UDID from a name.
_UDID_RE = re.compile(r"^[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}$")

# Actions that operate on a running device and so default to "booted" when the
# caller names none.
_BOOTED_DEFAULT_ACTIONS = frozenset(
    {
        "shutdown",
        "install",
        "launch",
        "terminate",
        "screenshot",
        "openurl",
        "appearance",
        "tap",
        "type",
        "swipe",
    }
)

# How long each shell-out may run before we give up. Builds are slow; the rest
# are quick simctl calls.
_SIMCTL_TIMEOUT_S = 60.0
_BUILD_TIMEOUT_S = 1200.0


def _destination(device: str | None) -> str:
    """Build an ``xcodebuild -destination`` value from a device reference."""
    if not device or device == "booted":
        return "generic/platform=iOS Simulator"
    if _UDID_RE.match(device):
        return f"platform=iOS Simulator,id={device}"
    return f"platform=iOS Simulator,name={device}"


def _target(device: str | None, action: str) -> str:
    """Resolve the simctl device operand (a UDID or the ``booted`` sentinel)."""
    if device:
        return device
    if action in _BOOTED_DEFAULT_ACTIONS:
        return "booted"
    return "booted"


def format_device_list(parsed: dict[str, Any]) -> str:
    """Render ``simctl list -j devices runtimes`` JSON into a compact summary.

    :param parsed: Parsed JSON from ``xcrun simctl list -j devices runtimes``.
    :returns: A human/LLM-readable listing of runtimes and their devices.
    """
    runtimes = parsed.get("runtimes") or []
    devices_by_runtime = parsed.get("devices") or {}

    if not runtimes:
        return (
            "Nenhum runtime iOS instalado. Instale um em Xcode → Settings → "
            "Components, ou rode `xcodebuild -downloadPlatform iOS`."
        )

    lines: list[str] = []
    # Map runtime identifier → friendly name for the device grouping below.
    names = {rt.get("identifier"): rt.get("name", rt.get("identifier")) for rt in runtimes}
    for identifier, devices in devices_by_runtime.items():
        available = [d for d in devices if d.get("isAvailable", True)]
        if not available:
            continue
        lines.append(f"\n{names.get(identifier, identifier)}:")
        for dev in available:
            state = dev.get("state", "Unknown")
            marker = "▶" if state == "Booted" else "·"
            lines.append(f"  {marker} {dev.get('name')}  [{state}]  {dev.get('udid')}")
    if not lines:
        return "Runtimes instalados, mas nenhum dispositivo criado. Crie um pelo Xcode."
    header = "Runtimes: " + ", ".join(rt.get("name", "?") for rt in runtimes)
    return header + "\n" + "\n".join(lines).lstrip("\n")


async def _list() -> str:
    res = await _shell(["xcrun", "simctl", "list", "-j", "devices", "runtimes"])
    if not res.ok:
        return f"Erro ao listar simuladores: {_tail(res.stderr or res.stdout)}"
    try:
        parsed = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        return f"Erro ao ler a saída do simctl: {exc}"
    return format_device_list(parsed)


async def _boot(device: str | None) -> str:
    if not device or device == "booted":
        return "Erro: 'boot' precisa de um device (nome ou UDID). Use 'list' para ver as opções."
    res = await _shell(["xcrun", "simctl", "boot", device])
    already = (
        "current state: Booted" in res.stderr
        or "Unable to boot device in current state" in res.stderr
    )
    if res.ok or already:
        # Bring the Simulator window to the front so the user (and the pane) can
        # see it — booting alone leaves it headless.
        await _shell(["open", "-a", "Simulator"])
        return f"Simulador iniciado: {device}" + (
            " (já estava rodando)" if already and not res.ok else ""
        )
    return f"Erro ao bootar {device}: {_tail(res.stderr or res.stdout)}"


async def _simple(argv_tail: list[str], device: str | None, action: str, ok_msg: str) -> str:
    target = _target(device, action)
    res = await _shell(["xcrun", "simctl", *argv_tail[:1], target, *argv_tail[1:]])
    if res.ok:
        return ok_msg
    return f"Erro ({action}): {_tail(res.stderr or res.stdout)}"


async def _screenshot(device: str | None, workspace: Path | None) -> str:
    target = _target(device, "screenshot")
    out_dir = (workspace or Path.cwd()) / ".omnicraft" / "ios"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"screenshot-{int(time.time() * 1000)}.png"
    res = await _shell(["xcrun", "simctl", "io", target, "screenshot", str(out_path)])
    if res.ok and out_path.exists():
        return f"Screenshot salvo: {out_path} — leia o arquivo para ver a tela."
    return f"Erro ao capturar a tela: {_tail(res.stderr or res.stdout)}"


def _idb_missing(action: str) -> str | None:
    """Actionable hint when neither idb nor the cliclick fallback is usable."""
    if shutil.which("idb") is not None or shutil.which("cliclick") is not None:
        return None
    return (
        f"Erro: '{action}' precisa injetar toques no simulador, e o simctl não "
        "faz isso. Instale `brew install cliclick` (clica na janela do "
        "Simulator; exige o app aberto e permissão de Acessibilidade)."
    )


# --- cliclick fallback -------------------------------------------------
# idb is archived and no longer installable on recent macOS, so touch input
# falls back to clicking the Simulator window on screen. Coordinates arrive in
# screenshot PIXELS and must land on macOS screen POINTS, which means locating
# the device screen inside the window (title bar + bezel) first.

# Plausible chrome for a Simulator window, used to pick the device scale.
_MIN_BEZEL_PT, _MAX_BEZEL_PT = 0.0, 80.0
_MIN_TITLEBAR_PT, _MAX_TITLEBAR_PT = 15.0, 80.0


async def _osascript(script: str) -> str | None:
    """Run an AppleScript snippet, returning stdout or ``None`` on failure."""
    res = await _shell(["osascript", "-e", script])
    return res.stdout.strip() if res.ok else None


async def _simulator_window() -> tuple[float, float, float, float] | None:
    """Front Simulator window as ``(x, y, w, h)`` in screen points."""
    out = await _osascript(
        'tell application "System Events" to tell process "Simulator" '
        "to get {position, size} of window 1"
    )
    if not out:
        return None
    try:
        x, y, w, h = (float(p.strip()) for p in out.split(",")[:4])
    except ValueError:
        return None
    return (x, y, w, h) if w > 0 and h > 0 else None


def _png_size(path: Path) -> tuple[int, int] | None:
    """Read a PNG's pixel dimensions from its IHDR header."""
    try:
        head = path.read_bytes()[:24]
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big"))


def _device_screen_rect(
    window: tuple[float, float, float, float], shot: tuple[int, int]
) -> tuple[float, float, float, float] | None:
    """Locate the device screen inside the Simulator window, in screen points.

    The window wraps the screen in a title bar and a symmetric bezel, and the
    screenshot is in pixels at the device's scale (2x or 3x). Solve for the
    scale that leaves plausible chrome — a wrong scale yields a negative or
    absurd bezel — so nothing about the window's size is hard-coded. Assumes
    the Simulator is at 100% zoom; a zoomed window fails the chrome check.
    """
    win_x, win_y, win_w, win_h = window
    px_w, px_h = shot
    for scale in (3.0, 2.0, 1.0):
        dev_w, dev_h = px_w / scale, px_h / scale
        bezel = (win_w - dev_w) / 2
        titlebar = win_h - dev_h - 2 * bezel
        if not (_MIN_BEZEL_PT <= bezel <= _MAX_BEZEL_PT):
            continue
        if not (_MIN_TITLEBAR_PT <= titlebar <= _MAX_TITLEBAR_PT):
            continue
        return (win_x + bezel, win_y + titlebar + bezel, dev_w, dev_h)
    return None


async def _cliclick_calibrate(
    target: str, workspace: Path | None
) -> tuple[tuple[float, float, float, float], tuple[int, int]] | str:
    """Resolve the on-screen device rect and the screenshot size it maps from.

    :returns: ``(rect, (px_w, px_h))``, or an actionable error string.
    """
    window = await _simulator_window()
    if window is None:
        return (
            "Erro: não achei a janela do Simulator. Abra o app "
            "(`open -a Simulator`) — o fallback por cliclick clica nela."
        )
    out_dir = (workspace or Path.cwd()) / ".omnicraft" / "ios"
    out_dir.mkdir(parents=True, exist_ok=True)
    probe = out_dir / "._calib.png"
    res = await _shell(["xcrun", "simctl", "io", target, "screenshot", str(probe)])
    shot = _png_size(probe) if res.ok else None
    probe.unlink(missing_ok=True)
    if shot is None:
        return "Erro: não consegui medir a tela do simulador para calibrar o toque."
    rect = _device_screen_rect(window, shot)
    if rect is None:
        return (
            "Erro: a janela do Simulator não bate com a tela do device. "
            "Deixe o zoom em 100% (Janela ▸ Tamanho Físico) e tente de novo."
        )
    return rect, shot


def _to_screen(
    rect: tuple[float, float, float, float], shot_w: int, shot_h: int, x: int, y: int
) -> tuple[int, int]:
    """Map a screenshot-pixel point onto macOS screen points."""
    rx, ry, rw, rh = rect
    return (round(rx + (x / shot_w) * rw), round(ry + (y / shot_h) * rh))


async def _cliclick_gesture(
    kind: str, coords: list[int], device: str | None, ok_msg: str, workspace: Path | None
) -> str:
    """Tap or swipe by clicking the Simulator window with cliclick."""
    target = _target(device, kind)
    calib = await _cliclick_calibrate(target, workspace)
    if isinstance(calib, str):
        return calib
    rect, (shot_w, shot_h) = calib
    # A click only raises an unfocused window — the event never reaches the
    # device — so bring the Simulator forward first.
    await _osascript('tell application "Simulator" to activate')
    pts = [
        _to_screen(rect, shot_w, shot_h, coords[i], coords[i + 1])
        for i in range(0, len(coords) - 1, 2)
    ]
    if kind == "tap":
        argv = ["cliclick", f"c:{pts[0][0]},{pts[0][1]}"]
    else:
        argv = [
            "cliclick",
            f"dd:{pts[0][0]},{pts[0][1]}",
            f"du:{pts[-1][0]},{pts[-1][1]}",
        ]
    res = await _shell(argv)
    if res.ok:
        return f"{ok_msg} (via cliclick na janela do Simulator)"
    return f"Erro ({kind}) via cliclick: {_tail(res.stderr or res.stdout)}"


async def _cliclick_text(text: str, device: str | None) -> str:
    """Type into the focused simulator by driving the keyboard with cliclick."""
    del device  # Typing goes to whatever the Simulator has focused.
    if await _simulator_window() is None:
        return (
            "Erro: não achei a janela do Simulator. Abra o app "
            "(`open -a Simulator`) — o fallback por cliclick digita nela."
        )
    await _osascript('tell application "Simulator" to activate')
    res = await _shell(["cliclick", f"t:{text}"])
    if res.ok:
        return "Texto digitado (via cliclick na janela do Simulator)"
    return f"Erro (type) via cliclick: {_tail(res.stderr or res.stdout)}"


async def _idb(ui_args: list[str], device: str | None, action: str, ok_msg: str) -> str:
    hint = _idb_missing(action)
    if hint:
        return hint
    if shutil.which("idb") is None:
        # Only `type` reaches here without idb; its text is the last argument.
        return await _cliclick_text(ui_args[-1], device)
    target = _target(device, action)
    res = await _shell(["idb", "ui", *ui_args, "--udid", target])
    if res.ok:
        return ok_msg
    return f"Erro ({action}) via idb: {_tail(res.stderr or res.stdout)}"


async def _idb_point_scale(target: str) -> tuple[float, float]:
    """Points-per-pixel ratio for a device, from idb's own screen dimensions.

    The pane and the agent reason in screenshot PIXELS (a 3x device shoots at
    1206×2622), but ``idb ui tap`` works in POINTS (402×874). Convert with the
    ratio idb itself reports so a click on the image lands where intended.
    Falls back to 1.0 (no scaling) when idb can't describe the target.
    """
    res = await _shell(["idb", "describe", "--json", "--udid", target])
    if not res.ok:
        return (1.0, 1.0)
    try:
        dims = json.loads(res.stdout).get("screen_dimensions") or {}
        w, h = dims.get("width"), dims.get("height")
        wp, hp = dims.get("width_points"), dims.get("height_points")
        if w and h and wp and hp:
            return (wp / w, hp / h)
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass
    return (1.0, 1.0)


async def _idb_gesture(
    kind: str,
    coords: list[int],
    device: str | None,
    ok_msg: str,
    workspace: Path | None = None,
) -> str:
    """Run a tap/swipe, scaling the pixel coords into the injector's space."""
    hint = _idb_missing(kind)
    if hint:
        return hint
    if shutil.which("idb") is None:
        return await _cliclick_gesture(kind, coords, device, ok_msg, workspace)
    target = _target(device, kind)
    sx, sy = await _idb_point_scale(target)
    # Even indices are X, odd are Y — scale each by its axis ratio.
    scaled = [str(round(c * (sx if i % 2 == 0 else sy))) for i, c in enumerate(coords)]
    res = await _shell(["idb", "ui", kind, *scaled, "--udid", target])
    if res.ok:
        return ok_msg
    return f"Erro ({kind}) via idb: {_tail(res.stderr or res.stdout)}"


async def _build(args: dict[str, Any]) -> str:
    scheme = str(args.get("scheme") or "").strip()
    if not scheme:
        return "Erro: 'build' precisa de 'scheme'."
    argv = ["xcodebuild", "-scheme", scheme]
    if args.get("project"):
        argv += ["-project", str(args["project"])]
    elif args.get("workspace"):
        argv += ["-workspace", str(args["workspace"])]
    argv += ["-configuration", str(args.get("configuration") or "Debug")]
    argv += ["-destination", _destination(args.get("device"))]
    argv += ["-sdk", "iphonesimulator", "build"]
    res = await _shell(argv, timeout=_BUILD_TIMEOUT_S)
    if res.ok:
        # Surface the last lines so the model sees the build-products path.
        return f"Build OK ({scheme}).\n{_tail(res.stdout, 1500)}"
    return f"Build falhou ({scheme}):\n{_tail(res.stderr or res.stdout)}"


async def run_action(action: str, args: dict[str, Any], *, workspace: Path | None) -> str:
    """Dispatch one ``ios_simulator`` action to its simctl/xcodebuild/idb call.

    :param action: One of the supported action names (``list``, ``boot`` …).
    :param args: Parsed LLM arguments for the action.
    :param workspace: Session workspace; screenshots land under it.
    :returns: A compact string result for the model.
    """
    device = args.get("device")
    device = str(device) if device else None

    if action == "list":
        return await _list()
    if action == "boot":
        return await _boot(device)
    if action == "shutdown":
        return await _simple(
            ["shutdown"], device, action, f"Simulador desligado: {device or 'booted'}."
        )
    if action == "install":
        app = str(args.get("app_path") or "").strip()
        if not app:
            return "Erro: 'install' precisa de 'app_path' (caminho do .app)."
        return await _simple(["install", app], device, action, f"App instalado: {app}")
    if action == "launch":
        bundle = str(args.get("bundle_id") or "").strip()
        if not bundle:
            return "Erro: 'launch' precisa de 'bundle_id'."
        return await _simple(["launch", bundle], device, action, f"App iniciado: {bundle}")
    if action == "terminate":
        bundle = str(args.get("bundle_id") or "").strip()
        if not bundle:
            return "Erro: 'terminate' precisa de 'bundle_id'."
        return await _simple(["terminate", bundle], device, action, f"App encerrado: {bundle}")
    if action == "screenshot":
        return await _screenshot(device, workspace)
    if action == "openurl":
        url = str(args.get("url") or "").strip()
        if not url:
            return "Erro: 'openurl' precisa de 'url'."
        return await _simple(["openurl", url], device, action, f"URL aberta: {url}")
    if action == "appearance":
        mode = str(args.get("mode") or "").strip().lower()
        if mode not in ("light", "dark"):
            return "Erro: 'appearance' aceita mode 'light' ou 'dark'."
        return await _simple(["ui", "appearance", mode], device, action, f"Aparência: {mode}.")
    if action == "tap":
        try:
            x, y = int(args["x"]), int(args["y"])
        except (KeyError, TypeError, ValueError):
            return "Erro: 'tap' precisa de 'x' e 'y' inteiros."
        return await _idb_gesture("tap", [x, y], device, f"Toque em ({x}, {y}).", workspace)
    if action == "swipe":
        try:
            x1, y1 = int(args["x1"]), int(args["y1"])
            x2, y2 = int(args["x2"]), int(args["y2"])
        except (KeyError, TypeError, ValueError):
            return "Erro: 'swipe' precisa de 'x1','y1','x2','y2' inteiros."
        return await _idb_gesture("swipe", [x1, y1, x2, y2], device, "Swipe executado.", workspace)
    if action == "type":
        text = str(args.get("text") or "")
        if not text:
            return "Erro: 'type' precisa de 'text'."
        return await _idb(["text", text], device, action, "Texto digitado.")
    if action == "build":
        return await _build(args)
    return (
        f"Erro: ação desconhecida '{action}'. Use list/boot/install/launch/screenshot/tap/build…"
    )
