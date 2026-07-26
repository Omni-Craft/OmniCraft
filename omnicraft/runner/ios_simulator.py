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

import hashlib
import json
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
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
# Compiling the small capture helper; generous, but it only happens once.
_SWIFT_BUILD_TIMEOUT_S = 180.0


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


# --- live video ---------------------------------------------------------
# ``simctl recordVideo`` only finalizes its QuickTime file on stop, so it can't
# feed a live view. Real video instead screen-captures the Simulator window
# with ffmpeg and crops to the device screen — the same rect the tap fallback
# solves for. Needs the window visible and Screen Recording permission.
#
# That region-capture path is the fallback. It has two flaws that only show up
# in real use: whatever sits on top of the Simulator is what gets captured (the
# app's own window, usually), and the crop is fixed when the stream starts, so
# moving the window points the capture at the wrong place. `window_stream_plan`
# is the preferred path and has neither — it reads the window's own buffer via
# the ScreenCaptureKit helper in resources/simcapture.swift.

# Matches ffmpeg's device listing, e.g. ``[4] Capture screen 0``.
_AVF_SCREEN_RE = re.compile(r"\[(\d+)\]\s+Capture screen", re.IGNORECASE)


def parse_avfoundation_screen_index(listing: str) -> str | None:
    """Pick the ``Capture screen`` input index out of ffmpeg's device listing.

    The index is machine-specific — cameras are enumerated first — so it is
    read rather than assumed.

    :param listing: ffmpeg's ``-list_devices true`` output (it writes stderr).
    :returns: The index as a string, or ``None`` when no screen device exists.
    """
    match = _AVF_SCREEN_RE.search(listing)
    return match.group(1) if match else None


# The window-capture helper, compiled on demand. Anyone driving an iOS
# simulator already has Xcode, so swiftc is available where this matters.
_SIMCAPTURE_SRC = Path(__file__).resolve().parents[1] / "resources" / "simcapture.swift"
_SIMCAPTURE_FPS = 15
_SIMCAPTURE_MAX_WIDTH = 640


def _simcapture_binary() -> Path:
    """Path of the compiled helper, keyed by the source it was built from."""
    digest = hashlib.sha256(_SIMCAPTURE_SRC.read_bytes()).hexdigest()[:12]
    return Path.home() / ".omnicraft" / "bin" / f"simcapture-{digest}"


async def _ensure_simcapture() -> Path | None:
    """Compile the capture helper if needed; ``None`` when that isn't possible.

    The binary is cached under a hash of its source, so a changed helper
    rebuilds and an unchanged one costs nothing.
    """
    if not _SIMCAPTURE_SRC.is_file() or shutil.which("swiftc") is None:
        return None
    binary = _simcapture_binary()
    if binary.exists():
        return binary
    binary.parent.mkdir(parents=True, exist_ok=True)
    res = await _shell(
        ["swiftc", "-O", "-o", str(binary), str(_SIMCAPTURE_SRC)], timeout=_SWIFT_BUILD_TIMEOUT_S
    )
    return binary if res.ok and binary.exists() else None


def window_stream_ffmpeg_args(
    size: tuple[int, int], crop: tuple[int, int, int, int] | None, framerate: int
) -> list[str]:
    """ffmpeg argv that encodes raw BGRA frames from stdin into fragmented MP4.

    :param size: ``(w, h)`` of the incoming frames, as the helper reports them.
    :param crop: Optional ``(w, h, x, y)`` to trim the window chrome away.
    :param framerate: The cadence the helper emits at.
    """
    w, h = size
    chain = f"crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]}," if crop else ""
    return [
        "ffmpeg",
        "-loglevel", "error",
        "-f", "rawvideo",
        "-pixel_format", "bgra",
        "-video_size", f"{w}x{h}",
        "-framerate", str(framerate),
        "-i", "pipe:0",
        "-vf", f"{chain}format=yuv420p",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-g", str(framerate),
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "pipe:1",
    ]  # fmt: skip


def stream_ffmpeg_args(
    screen_index: str, crop: tuple[int, int, int, int], framerate: int = 30
) -> list[str]:
    """Build the ffmpeg argv that streams a screen region as fragmented MP4.

    Fragmented output (``empty_moov``) is what makes the stream playable while
    it is still being written — a plain MP4 keeps its index at the end.

    :param screen_index: avfoundation input index for the screen.
    :param crop: ``(w, h, x, y)`` in captured pixels.
    :param framerate: Capture rate in frames per second.
    :returns: The full argv, writing the stream to stdout.
    """
    w, h, x, y = crop
    return [
        "ffmpeg",
        "-loglevel", "error",
        "-f", "avfoundation",
        "-capture_cursor", "0",
        # The screen device only offers uyvy422; asking it for anything else
        # (including the yuv420p the browser needs) makes it refuse to open.
        "-pixel_format", "uyvy422",
        "-framerate", str(framerate),
        "-i", screen_index,
        # Convert in the filter chain instead — yuv420p is what browsers decode.
        "-vf", f"crop={w}:{h}:{x}:{y},format=yuv420p",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        # A fragment only flushes on a keyframe, and x264 defaults to one every
        # ~250 frames — that would stall the first frame for seconds.
        "-g", str(framerate),
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "pipe:1",
    ]  # fmt: skip


async def _display_scale(rect: tuple[float, float, float, float]) -> float:
    """Captured pixels per screen point, measured rather than assumed.

    Reads it from a probe capture of *rect*: the desktop's own bounds are an
    unreliable source on a multi-monitor setup.
    """
    x, y, w, h = rect
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "scale.png"
        res = await _shell(
            [
                "screencapture",
                "-x",
                "-R",
                f"{round(x)},{round(y)},{round(w)},{round(h)}",
                str(probe),
            ]
        )
        size = _png_size(probe) if res.ok else None
    if not size or not w:
        return 1.0
    return size[0] / w


@dataclass(frozen=True)
class WindowStreamPlan:
    """Everything needed to start a window-capture stream.

    The helper announces its exact frame size on stderr once running, which is
    only knowable then — so the ffmpeg side is built afterwards, from
    :func:`crop_within_window` and :func:`window_stream_ffmpeg_args`.
    """

    helper: list[str]
    window: tuple[float, float, float, float]
    screen_rect: tuple[float, float, float, float]
    shot: tuple[int, int]
    framerate: int


async def window_stream_plan(device: str | None) -> WindowStreamPlan | str:
    """Resolve how to stream the device screen from the window's own buffer.

    Preferred over :func:`live_stream_command`: reading the window's buffer
    keeps working when the window is covered or moved, which capturing a screen
    region does not.

    :returns: A :class:`WindowStreamPlan`, or an actionable error string.
    """
    if shutil.which("ffmpeg") is None:
        return "Erro: o vídeo ao vivo precisa do ffmpeg (`brew install ffmpeg`)."
    binary = await _ensure_simcapture()
    if binary is None:
        return (
            "Erro: não consegui preparar a captura de janela (precisa do "
            "swiftc, que vem com o Xcode)."
        )
    window = await _simulator_window()
    if window is None:
        return "Erro: não achei a janela do Simulator. Abra o app (`open -a Simulator`)."
    shot = await _shot_size(_target(device, "screenshot"))
    if shot is None:
        return "Erro: não consegui medir a tela do simulador."
    rect = _device_screen_rect(window, shot)
    if rect is None:
        return (
            "Erro: a janela do Simulator não bate com a tela do device. "
            "Deixe o zoom em 100% (Janela ▸ Tamanho Físico)."
        )
    return WindowStreamPlan(
        helper=[str(binary), "Simulator", str(_SIMCAPTURE_FPS), str(_SIMCAPTURE_MAX_WIDTH)],
        window=window,
        screen_rect=rect,
        shot=shot,
        framerate=_SIMCAPTURE_FPS,
    )


def crop_within_window(
    window: tuple[float, float, float, float],
    rect: tuple[float, float, float, float],
    frame: tuple[int, int],
) -> tuple[int, int, int, int]:
    """Device-screen crop in helper-frame pixels, from the screen-space rect.

    The rect is in screen points and the frame in the helper's pixels, so both
    the origin (window-relative) and the scale have to be converted.
    """
    win_x, win_y, win_w, _ = window
    rx, ry, rw, rh = rect
    scale = frame[0] / win_w if win_w else 1.0
    return (
        max(2, int(rw * scale) // 2 * 2),
        max(2, int(rh * scale) // 2 * 2),
        max(0, round((rx - win_x) * scale)),
        max(0, round((ry - win_y) * scale)),
    )


async def _shot_size(target: str) -> tuple[int, int] | None:
    """Pixel size of the device's screenshot."""
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.png"
        res = await _shell(["xcrun", "simctl", "io", target, "screenshot", str(probe)])
        return _png_size(probe) if res.ok else None


async def live_stream_command(device: str | None) -> tuple[list[str], tuple[int, int]] | str:
    """Resolve the ffmpeg command that streams the booted simulator's screen.

    :param device: Device reference, or ``None`` for the booted one.
    :returns: ``(argv, (px_w, px_h))``, or an actionable error string.
    """
    if shutil.which("ffmpeg") is None:
        return "Erro: o vídeo ao vivo precisa do ffmpeg (`brew install ffmpeg`)."
    listing = await _shell(["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""])
    # ffmpeg writes the listing to stderr and exits non-zero by design.
    index = parse_avfoundation_screen_index(listing.stderr or listing.stdout)
    if index is None:
        return "Erro: o ffmpeg não achou um dispositivo de captura de tela."
    calib = await _cliclick_calibrate(_target(device, "screenshot"), None)
    if isinstance(calib, str):
        return calib
    rect, shot = calib
    scale = await _display_scale(rect)
    x, y, w, h = rect
    # Even dimensions only — H.264 chroma subsampling rejects odd sizes.
    crop = (
        int(w * scale) // 2 * 2,
        int(h * scale) // 2 * 2,
        round(x * scale),
        round(y * scale),
    )
    return stream_ffmpeg_args(index, crop), shot


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
