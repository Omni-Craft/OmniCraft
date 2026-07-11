"""Unit tests for the Web Push module (VAPID keys, store, delivery)."""

from __future__ import annotations

import base64
import importlib
import sys
import types
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture()
def push(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The push module rooted at a throwaway config dir."""
    monkeypatch.setenv("OMNICRAFT_CONFIG_HOME", str(tmp_path))
    module = importlib.import_module("omnicraft.server.push")
    return importlib.reload(module)


def test_vapid_public_key_is_a_valid_uncompressed_point(push) -> None:
    """The applicationServerKey is a 65-byte uncompressed EC point (0x04)."""
    key = push.application_server_key()
    raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
    assert len(raw) == 65 and raw[0] == 0x04
    # Stable across calls (generated once, cached).
    assert push.application_server_key() == key


def test_subscription_store_dedupes_by_endpoint(push) -> None:
    """Adding the same endpoint twice replaces it; remove drops it."""
    push.add_subscription("alice", {"endpoint": "e1", "keys": {"p256dh": "old"}})
    push.add_subscription("alice", {"endpoint": "e2", "keys": {}})
    push.add_subscription("alice", {"endpoint": "e1", "keys": {"p256dh": "new"}})

    subs = push.get_subscriptions("alice")
    assert len(subs) == 2
    e1 = next(s for s in subs if s["endpoint"] == "e1")
    assert e1["keys"]["p256dh"] == "new"

    push.remove_subscription("alice", "e1")
    assert [s["endpoint"] for s in push.get_subscriptions("alice")] == ["e2"]


def test_add_subscription_ignores_missing_endpoint(push) -> None:
    """A payload without an endpoint is a no-op, not a crash."""
    push.add_subscription("bob", {"keys": {}})
    assert push.get_subscriptions("bob") == []


def _install_fake_pywebpush(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]):
    """Stub the ``pywebpush`` module so send tests run without the real dep."""

    class WebPushException(Exception):
        def __init__(self, message: str, response: Any = None) -> None:
            super().__init__(message)
            self.response = response

    def webpush(**kwargs: Any) -> None:
        calls.append(kwargs)

    module = types.ModuleType("pywebpush")
    module.webpush = webpush  # type: ignore[attr-defined]
    module.WebPushException = WebPushException  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pywebpush", module)
    return module


def test_send_web_push_calls_webpush_with_vapid(push, monkeypatch: pytest.MonkeyPatch) -> None:
    """A send passes the subscription, JSON payload, and VAPID creds through."""
    calls: list[dict[str, Any]] = []
    _install_fake_pywebpush(monkeypatch, calls)

    subscription = {"endpoint": "https://fcm/e", "keys": {"p256dh": "x", "auth": "y"}}
    result = push.send_web_push(subscription, {"title": "OmniCraft", "body": "aprove"})

    assert result is True
    assert len(calls) == 1
    call = calls[0]
    assert call["subscription_info"] == subscription
    assert '"title": "OmniCraft"' in call["data"]
    assert call["vapid_private_key"].endswith("push_vapid_private.pem")
    assert call["vapid_claims"]["sub"].startswith("mailto:")


def test_send_web_push_reports_gone_subscription(push, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 410 from the push service maps to False so the caller prunes it."""
    module = _install_fake_pywebpush(monkeypatch, [])

    def raise_gone(**_: Any) -> None:
        response = types.SimpleNamespace(status_code=410)
        raise module.WebPushException("gone", response=response)

    monkeypatch.setattr(module, "webpush", raise_gone)
    assert push.send_web_push({"endpoint": "e"}, {}) is False


def test_deliver_to_user_prunes_dead_subscriptions(push, monkeypatch: pytest.MonkeyPatch) -> None:
    """deliver_to_user drops a subscription the push service rejected as gone."""
    module = _install_fake_pywebpush(monkeypatch, [])
    push.add_subscription("carol", {"endpoint": "dead", "keys": {}})
    push.add_subscription("carol", {"endpoint": "live", "keys": {}})

    def selective(**kwargs: Any) -> None:
        if kwargs["subscription_info"]["endpoint"] == "dead":
            raise module.WebPushException("gone", response=types.SimpleNamespace(status_code=410))

    monkeypatch.setattr(module, "webpush", selective)
    push.deliver_to_user("carol", {"title": "x"})

    assert [s["endpoint"] for s in push.get_subscriptions("carol")] == ["live"]


def test_send_web_push_without_pywebpush_is_a_noop(push, monkeypatch: pytest.MonkeyPatch) -> None:
    """With pywebpush absent, a send returns None instead of raising."""
    # A None entry in sys.modules makes ``from pywebpush import ...`` raise
    # ImportError even if the package happens to be installed.
    monkeypatch.setitem(sys.modules, "pywebpush", None)
    assert push.send_web_push({"endpoint": "e"}, {}) is None
