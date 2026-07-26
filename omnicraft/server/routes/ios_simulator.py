"""iOS Simulator preview bridge — feeds the desktop app's Simulador pane.

The agent-facing control lives in the runner (``ios_simulator`` builtin). This
router is the *view* half: the web pane polls it for a live screenshot and the
device list, streams real video from it, and forwards click-to-tap. Like the
runner tool, it shells out to ``xcrun simctl`` / ffmpeg directly, so it assumes
the server runs on the Mac with Xcode (the local-desktop deployment). When no
simulator is booted the screenshot endpoint answers 409 so the pane shows its
empty state instead of a broken image; the video endpoint answers 409 whenever
screen capture isn't possible, and the pane keeps polling instead.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from omnicraft.runner import ios_simulator as ios
from omnicraft.server.auth import AuthProvider
from omnicraft.server.routes._auth_helpers import require_user

# Read size for the video pump — big enough to avoid syscall churn, small
# enough that a frame reaches the pane promptly.
_STREAM_CHUNK_BYTES = 64 * 1024
# How long ffmpeg gets to exit on SIGTERM before it is killed.
_STREAM_KILL_GRACE_S = 3.0
# How long to wait for the helper's first frame before giving up on video. It
# announces only once a frame really arrives, so this doubles as the check for
# a window that can't be captured (a minimized one has no surface) — short
# enough that the pane drops to frames quickly instead of hanging.
_HELPER_START_S = 6.0

_FRAME_SIZE_RE = re.compile(rb"(\d+)x(\d+)")


def _parse_frame_size(header: bytes) -> tuple[int, int] | None:
    """Read the ``WIDTHxHEIGHT`` line the capture helper prints on startup."""
    match = _FRAME_SIZE_RE.search(header)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)))


class _TapBody(BaseModel):
    """Coordinates for a tap forwarded from the pane."""

    x: int
    y: int
    device: str | None = None


def create_ios_simulator_router(
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the router for ``/v1/sessions/{id}/ios/*``."""
    router = APIRouter()

    @router.get("/sessions/{session_id}/ios/devices")
    async def devices(request: Request, session_id: str) -> dict[str, Any]:
        """List simulators/runtimes (parsed JSON plus a friendly summary)."""
        require_user(request, auth_provider)
        del session_id  # path-scoped by convention; the sim is host-global
        res = await ios._shell(["xcrun", "simctl", "list", "-j", "devices", "runtimes"])
        if not res.ok:
            return {"ok": False, "error": res.stderr or res.stdout}
        try:
            parsed = json.loads(res.stdout)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"invalid simctl output: {exc}"}
        booted = _first_booted(parsed)
        return {
            "ok": True,
            "summary": ios.format_device_list(parsed),
            "booted": booted,
            "raw": parsed,
        }

    @router.get("/sessions/{session_id}/ios/screenshot")
    async def screenshot(request: Request, session_id: str, device: str | None = None) -> Response:
        """Return a fresh PNG of the booted simulator, or 409 if none is up."""
        require_user(request, auth_provider)
        del session_id  # path-scoped by convention; the sim is host-global
        target = device or "booted"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "shot.png"
            res = await ios._shell(["xcrun", "simctl", "io", target, "screenshot", str(out)])
            if res.ok and out.exists():
                return Response(
                    content=out.read_bytes(),
                    media_type="image/png",
                    headers={"Cache-Control": "no-store"},
                )
        return JSONResponse(
            {"ok": False, "error": res.stderr.strip() or "no booted simulator"},
            status_code=409,
        )

    @router.get("/sessions/{session_id}/ios/stream")
    async def stream(request: Request, session_id: str, device: str | None = None) -> Response:
        """Stream the simulator window as fragmented MP4, for the live view.

        Screen-capture based: it needs the Simulator window visible and Screen
        Recording permission. The pane falls back to polling ``/screenshot``
        when this answers 409.
        """
        require_user(request, auth_provider)
        del session_id  # path-scoped by convention; the sim is host-global
        plan = await ios.window_stream_plan(device)
        if isinstance(plan, str):
            return JSONResponse({"ok": False, "error": plan}, status_code=409)
        shot = plan.shot
        # Wire helper → ffmpeg with an OS pipe so the raw frames never travel
        # through Python; a StreamReader can't be another process's stdin.
        read_fd, write_fd = os.pipe()
        # The helper streams the window's own buffer; its first stderr line
        # carries the frame size, which only it can know.
        helper = await asyncio.create_subprocess_exec(
            *plan.helper, stdout=write_fd, stderr=asyncio.subprocess.PIPE
        )
        os.close(write_fd)
        assert helper.stderr is not None
        try:
            header = await asyncio.wait_for(helper.stderr.readline(), timeout=_HELPER_START_S)
        except asyncio.TimeoutError:
            header = b""

        async def discard_helper() -> None:
            """Reap the helper on a path that never starts streaming.

            It may already be gone — announcing nothing is exactly what a
            helper that died on startup does — so a missing process is normal
            here, not an error to propagate.
            """
            with suppress(ProcessLookupError):
                helper.kill()
            await helper.wait()

        frame = _parse_frame_size(header)
        if frame is None:
            os.close(read_fd)
            await discard_helper()
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        "a captura de janela não rendeu nenhum quadro — o "
                        "Simulator pode estar minimizado (restaure a janela)"
                    ),
                },
                status_code=409,
            )
        crop = ios.crop_within_window(plan.window, plan.screen_rect, frame)
        try:
            proc = await asyncio.create_subprocess_exec(
                *ios.window_stream_ffmpeg_args(frame, crop, plan.framerate),
                stdin=read_fd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            # Nothing owns the helper or the pipe yet, so failing to spawn the
            # encoder would strand both — and a live helper holds the capture.
            os.close(read_fd)
            await discard_helper()
            return JSONResponse(
                {"ok": False, "error": "não consegui iniciar o encoder de vídeo"},
                status_code=409,
            )
        os.close(read_fd)

        async def pump() -> AsyncIterator[bytes]:
            """Forward ffmpeg's output until the client goes away."""
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(_STREAM_CHUNK_BYTES)
                if not chunk:
                    return
                yield chunk

        async def stop(child: asyncio.subprocess.Process) -> None:
            """Terminate a child, escalating to a kill, and reap it."""
            if child.returncode is None:
                with suppress(ProcessLookupError):
                    child.terminate()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(child.wait(), timeout=_STREAM_KILL_GRACE_S)
            if child.returncode is None:
                with suppress(ProcessLookupError):
                    child.kill()
                await child.wait()

        async def reap() -> None:
            """Stop both children once the response ends, however it ended.

            Survivors keep capturing and encoding a stream nobody reads. This
            runs as the response's background task rather than in the pump's
            ``finally``: a client disconnect cancels the pump outright, so its
            cleanup is not reliably reached.
            """
            await stop(proc)
            await stop(helper)

        return StreamingResponse(
            pump(),
            media_type="video/mp4",
            background=BackgroundTask(reap),
            headers={
                "Cache-Control": "no-store",
                # The stream is cropped to the screen at capture resolution,
                # while taps are expressed in screenshot pixels — the pane needs
                # this to convert a click on the video.
                "X-Device-Screenshot": f"{shot[0]}x{shot[1]}",
            },
        )

    @router.post("/sessions/{session_id}/ios/tap")
    async def tap(request: Request, session_id: str, body: _TapBody) -> dict[str, Any]:
        """Forward a tap from the pane to the simulator (via idb)."""
        require_user(request, auth_provider)
        del session_id  # path-scoped by convention; the sim is host-global
        out = await ios.run_action(
            "tap", {"x": body.x, "y": body.y, "device": body.device}, workspace=None
        )
        ok = not out.startswith("Erro")
        return {"ok": ok, "message": out}

    return router


def _first_booted(parsed: dict[str, Any]) -> dict[str, Any] | None:
    """Return the first Booted device across all runtimes, or ``None``."""
    for devices in (parsed.get("devices") or {}).values():
        for dev in devices:
            if dev.get("state") == "Booted":
                return {"udid": dev.get("udid"), "name": dev.get("name")}
    return None
