"""Wiring test for the runner-side native-bridge GC.

Drives the real sweep closure built inside ``create_runner_app`` (config
resolution → family construction → per-dir conversation resolution over the
server client → classification → removal) end to end, using a fake server
client and monkeypatched bridge roots.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

import omnicraft.antigravity_native_bridge as agy_bridge
import omnicraft.codex_native_bridge as codex_bridge
from omnicraft.native_bridge_gc import NativeBridgeGarbageCollector, bridge_id_digest
from omnicraft.runner import create_runner_app


class _Resp:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


class _GcServerClient:
    """Fake server client answering ``GET /v1/sessions/{id}`` from a fixture map.

    A session id absent from the map (or mapped to ``None``) answers 404 —
    the "conversation gone" signal.
    """

    def __init__(self, sessions: dict[str, dict[str, Any] | None]) -> None:
        self._sessions = sessions
        self.gets: list[str] = []

    async def get(self, url: str, **kwargs: Any) -> _Resp:
        self.gets.append(url)
        sid = urllib.parse.unquote(url.rsplit("/", 1)[-1])
        body = self._sessions.get(sid)
        if sid not in self._sessions or body is None:
            return _Resp(404, {})
        return _Resp(200, body)

    async def post(self, *_a: Any, **_k: Any) -> _Resp:
        return _Resp(200, {})

    async def patch(self, *_a: Any, **_k: Any) -> _Resp:
        return _Resp(200, {})


def _codex_dir(root: Path, session_id: str) -> Path:
    d = root / bridge_id_digest(session_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "codex-home").mkdir(exist_ok=True)
    (d / "state.json").write_text(json.dumps({"session_id": session_id}), encoding="utf-8")
    return d


def _agy_dir(root: Path, session_id: str) -> Path:
    d = root / bridge_id_digest(session_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "agy-home").mkdir(exist_ok=True)
    (d / "state.json").write_text(
        json.dumps({"session_id": session_id, "conversation_id": "agy-uuid"}),
        encoding="utf-8",
    )
    return d


@pytest.fixture
def gc_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    codex_root = tmp_path / "codex-native"
    agy_root = tmp_path / "antigravity-native"
    monkeypatch.setattr(codex_bridge, "_BRIDGE_ROOT", codex_root)
    monkeypatch.setattr(agy_bridge, "_BRIDGE_ROOT", agy_root)
    # Isolate the codex process-registry reconcile (a pre-sweep hook) from the
    # real ~/.omnicraft so the test never touches the developer's state.
    monkeypatch.setenv("OMNICRAFT_CODEX_NATIVE_STATE_DIR", str(tmp_path / "codex-state"))
    # Isolate server config to defaults (enabled, real removal, opt-ins off).
    monkeypatch.setenv("OMNICRAFT_CONFIG", str(tmp_path / "no-such-config.yaml"))
    return codex_root, agy_root


def test_native_bridge_gc_registered_on_app_state() -> None:
    app: FastAPI = create_runner_app(server_client=_GcServerClient({}))  # type: ignore[arg-type]
    assert isinstance(app.state.native_bridge_gc, NativeBridgeGarbageCollector)


async def test_sweep_removes_orphan_keeps_live_and_bound(
    gc_roots: tuple[Path, Path],
) -> None:
    codex_root, agy_root = gc_roots
    orphan = _codex_dir(codex_root, "conv_gone")  # 404 -> orphan -> removed
    active = _codex_dir(codex_root, "conv_active")  # exists, unbound, not archived
    bound = _agy_dir(agy_root, "conv_bound")  # bound in DB -> LIVE

    client = _GcServerClient(
        {
            # "conv_gone" intentionally absent -> 404.
            "conv_active": {"archived": False, "host_id": None, "runner_id": None},
            "conv_bound": {"archived": False, "host_id": "host-1", "runner_id": None},
        }
    )
    app = create_runner_app(server_client=client)  # type: ignore[arg-type]

    await app.state.native_bridge_gc._sweep()

    assert not orphan.exists(), "orphan (conversation gone) should be removed"
    assert active.exists(), "existing unbound session dir must be kept"
    assert bound.exists(), "bound (live) session dir must be kept"


async def test_sweep_transient_server_error_never_removes(
    gc_roots: tuple[Path, Path],
) -> None:
    codex_root, _ = gc_roots
    d = _codex_dir(codex_root, "conv_x")

    class _BoomClient(_GcServerClient):
        async def get(self, url: str, **kwargs: Any) -> _Resp:
            import httpx

            raise httpx.ConnectError("server down")

    app = create_runner_app(server_client=_BoomClient({}))  # type: ignore[arg-type]
    await app.state.native_bridge_gc._sweep()
    assert d.exists(), "a transient server error must resolve conservatively (kept)"


async def test_sweep_disabled_via_config_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_root = tmp_path / "codex-native"
    monkeypatch.setattr(codex_bridge, "_BRIDGE_ROOT", codex_root)
    monkeypatch.setattr(agy_bridge, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    monkeypatch.setenv("OMNICRAFT_CODEX_NATIVE_STATE_DIR", str(tmp_path / "codex-state"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("native_bridge_gc_enabled: false\n")
    monkeypatch.setenv("OMNICRAFT_CONFIG", str(cfg))
    orphan = _codex_dir(codex_root, "conv_gone")

    app = create_runner_app(server_client=_GcServerClient({}))  # type: ignore[arg-type]
    await app.state.native_bridge_gc._sweep()
    assert orphan.exists(), "a disabled GC must not remove anything"
