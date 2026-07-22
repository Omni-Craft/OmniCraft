"""iOS Simulator preview bridge — feeds the desktop app's Simulador pane.

The agent-facing control lives in the runner (``ios_simulator`` builtin). This
router is the *view* half: the web pane polls it for a live screenshot and the
device list, and forwards click-to-tap. Like the runner tool, it shells out to
``xcrun simctl`` / ``idb`` directly, so it assumes the server runs on the Mac
with Xcode (the local-desktop deployment). When no simulator is booted the
screenshot endpoint answers 409 so the pane shows its empty state instead of a
broken image.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from omnicraft.runner import ios_simulator as ios
from omnicraft.server.auth import AuthProvider
from omnicraft.server.routes._auth_helpers import require_user


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
