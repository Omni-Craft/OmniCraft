"""Tests for the embedded-browser action bridge (server half of the relay).

The web UI's relay (useBrowserAgentRelay.ts) claims actions announced on a
``browser.action_request`` SSE event and POSTs results back. These lock in
the claim-first CAS (single winner), token-guarded results, the timeout
path when no renderer exists, and the SSE payload shape the frontend
parses (action_id / action / args).
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import omnicraft.server.routes.browser_actions as ba
from omnicraft.server.routes.browser_actions import create_browser_actions_router


@pytest.fixture()
def published(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(ba.session_stream, "publish", lambda sid, evt: events.append((sid, evt)))
    return events


@pytest.fixture()
def app(published: Any) -> FastAPI:
    a = FastAPI()
    a.include_router(create_browser_actions_router(), prefix="/v1")
    return a


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _relay_answers(app: FastAPI, published: list, result: dict[str, Any]) -> None:
    """Background 'renderer': waits for the SSE publish, claims, posts result."""

    def run() -> None:
        client = TestClient(app)
        deadline = time.time() + 5
        while not published and time.time() < deadline:
            time.sleep(0.01)
        sid, evt = published[0]
        claim = client.post(f"/v1/sessions/{sid}/browser/action_claim/{evt['action_id']}").json()
        assert claim["claimed"] is True
        client.post(
            f"/v1/sessions/{sid}/browser/action_result/{evt['action_id']}",
            json={"result": result, "claim_token": claim["claim_token"]},
        )

    threading.Thread(target=run, daemon=True).start()


def test_action_roundtrip_with_claim(app: FastAPI, client: TestClient, published: list) -> None:
    _relay_answers(app, published, {"ok": True, "data": {"final_url": "http://x"}})
    resp = client.post(
        "/v1/sessions/conv_1/browser/actions",
        json={"action": "navigate", "args": {"url": "http://x"}, "timeout_s": 5},
    )
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["final_url"] == "http://x"
    # SSE payload carries exactly what the frontend parseEvent expects.
    sid, evt = published[0]
    assert sid == "conv_1"
    assert evt["type"] == "browser.action_request"
    assert evt["action"] == "navigate"
    assert evt["args"] == {"url": "http://x"}
    assert evt["action_id"].startswith("bact_")


def test_second_claim_loses(app: FastAPI, client: TestClient, published: list) -> None:
    def first_claim_only() -> None:
        client = TestClient(app)
        deadline = time.time() + 5
        while not published and time.time() < deadline:
            time.sleep(0.01)
        sid, evt = published[0]
        c1 = client.post(f"/v1/sessions/{sid}/browser/action_claim/{evt['action_id']}").json()
        c2 = client.post(f"/v1/sessions/{sid}/browser/action_claim/{evt['action_id']}").json()
        assert c1["claimed"] is True
        assert c2["claimed"] is False  # CAS: one winner
        client.post(
            f"/v1/sessions/{sid}/browser/action_result/{evt['action_id']}",
            json={"result": {"ok": True}, "claim_token": c1["claim_token"]},
        )

    threading.Thread(target=first_claim_only, daemon=True).start()
    body = client.post(
        "/v1/sessions/conv_1/browser/actions",
        json={"action": "screenshot", "timeout_s": 5},
    ).json()
    assert body["ok"] is True


def test_wrong_token_result_is_rejected(app: FastAPI, client: TestClient, published: list) -> None:
    def bad_then_good() -> None:
        client = TestClient(app)
        deadline = time.time() + 5
        while not published and time.time() < deadline:
            time.sleep(0.01)
        sid, evt = published[0]
        claim = client.post(f"/v1/sessions/{sid}/browser/action_claim/{evt['action_id']}").json()
        bad = client.post(
            f"/v1/sessions/{sid}/browser/action_result/{evt['action_id']}",
            json={"result": {"ok": False, "error": "impostor"}, "claim_token": "wrong"},
        ).json()
        assert bad["ok"] is False
        client.post(
            f"/v1/sessions/{sid}/browser/action_result/{evt['action_id']}",
            json={"result": {"ok": True}, "claim_token": claim["claim_token"]},
        )

    threading.Thread(target=bad_then_good, daemon=True).start()
    body = client.post(
        "/v1/sessions/conv_1/browser/actions",
        json={"action": "click", "args": {"ref": 3}, "timeout_s": 5},
    ).json()
    assert body["ok"] is True  # the impostor result never resolved the future


def test_timeout_when_no_renderer(client: TestClient, published: list) -> None:
    body = client.post(
        "/v1/sessions/conv_1/browser/actions",
        json={"action": "snapshot", "timeout_s": 0.2},
    ).json()
    assert body["ok"] is False
    assert "app desktop" in body["error"]


def test_unknown_action_rejected(client: TestClient, published: list) -> None:
    body = client.post(
        "/v1/sessions/conv_1/browser/actions",
        json={"action": "explode", "timeout_s": 1},
    ).json()
    assert body["ok"] is False
    assert "desconhecida" in body["error"]
    assert published == []  # never announced to renderers


def test_claim_for_wrong_session_loses(app: FastAPI, client: TestClient, published: list) -> None:
    def wrong_session_claim() -> None:
        client = TestClient(app)
        deadline = time.time() + 5
        while not published and time.time() < deadline:
            time.sleep(0.01)
        _, evt = published[0]
        c = client.post(f"/v1/sessions/conv_OUTRA/browser/action_claim/{evt['action_id']}").json()
        assert c["claimed"] is False
        # Right session still wins afterwards.
        c2 = client.post(f"/v1/sessions/conv_1/browser/action_claim/{evt['action_id']}").json()
        client.post(
            f"/v1/sessions/conv_1/browser/action_result/{evt['action_id']}",
            json={"result": {"ok": True}, "claim_token": c2["claim_token"]},
        )

    threading.Thread(target=wrong_session_claim, daemon=True).start()
    body = client.post(
        "/v1/sessions/conv_1/browser/actions",
        json={"action": "type", "args": {"ref": 1, "text": "oi"}, "timeout_s": 5},
    ).json()
    assert body["ok"] is True
