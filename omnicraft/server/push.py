"""Web Push for approval notifications (closed-app delivery).

When a session pauses for approval, the server sends a Web Push to the owner's
subscribed browsers so the notification arrives even with the PWA closed. The
page-side notification (useIdleNotifications) covers the app-open case; this
covers app-closed.

Design notes:
  * VAPID keypair is generated once and cached under the config dir. The public
    key (an uncompressed EC point, base64url) is the browser's
    ``applicationServerKey``; the private key is kept as a PEM file so it can be
    handed straight to ``pywebpush`` (which reads PEM paths).
  * Subscriptions live in a small JSON file keyed by user id — no schema
    migration, fine for a self-hosted deployment.
  * ``pywebpush`` is imported lazily inside :func:`send_web_push`, so the
    key/subscription endpoints work on a server that hasn't installed it yet;
    only actual delivery needs it.

Delivery to a device can only be verified on a real phone/browser (it routes
through FCM / Mozilla's push service); everything up to the send is local.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

_logger = logging.getLogger(__name__)

# mailto used as the VAPID ``sub`` claim (push services want a contact).
_VAPID_SUBJECT = "mailto:push@omnicraft.local"

_lock = threading.Lock()


def _config_dir() -> Path:
    """The ~/.omnicraft data dir (or ``OMNICRAFT_CONFIG_HOME`` when set)."""
    override = os.environ.get("OMNICRAFT_CONFIG_HOME")
    base = Path(override) if override else Path.home() / ".omnicraft"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _vapid_pem_path() -> Path:
    return _config_dir() / "push_vapid_private.pem"


def _vapid_public_path() -> Path:
    return _config_dir() / "push_vapid_public.txt"


def _subscriptions_path() -> Path:
    return _config_dir() / "push_subscriptions.json"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _ensure_vapid() -> None:
    """Generate the VAPID keypair on first use (idempotent)."""
    pem_path = _vapid_pem_path()
    pub_path = _vapid_public_path()
    if pem_path.exists() and pub_path.exists():
        return
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    # The browser's applicationServerKey is the raw uncompressed public point.
    public_point = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    pem_path.write_bytes(pem)
    os.chmod(pem_path, 0o600)
    pub_path.write_text(_b64url(public_point))


def application_server_key() -> str:
    """The VAPID public key (base64url) the browser subscribes with."""
    _ensure_vapid()
    return _vapid_public_path().read_text().strip()


# ── Subscription store (JSON file, keyed by user) ────────────────────


def _load_all() -> dict[str, list[dict[str, Any]]]:
    path = _subscriptions_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_all(data: dict[str, list[dict[str, Any]]]) -> None:
    path = _subscriptions_path()
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)


def _subscription_endpoint(subscription: dict[str, Any]) -> str | None:
    endpoint = subscription.get("endpoint")
    return endpoint if isinstance(endpoint, str) and endpoint else None


def add_subscription(user_id: str, subscription: dict[str, Any]) -> None:
    """Store a browser's push subscription for a user (deduped by endpoint)."""
    endpoint = _subscription_endpoint(subscription)
    if endpoint is None:
        return
    with _lock:
        data = _load_all()
        subs = [s for s in data.get(user_id, []) if _subscription_endpoint(s) != endpoint]
        subs.append(subscription)
        data[user_id] = subs
        _save_all(data)


def remove_subscription(user_id: str, endpoint: str) -> None:
    """Drop a user's subscription by endpoint (e.g. on unsubscribe or 410)."""
    with _lock:
        data = _load_all()
        if user_id not in data:
            return
        data[user_id] = [s for s in data[user_id] if _subscription_endpoint(s) != endpoint]
        if not data[user_id]:
            del data[user_id]
        _save_all(data)


def get_subscriptions(user_id: str) -> list[dict[str, Any]]:
    """All push subscriptions currently stored for a user."""
    with _lock:
        return list(_load_all().get(user_id, []))


# ── Delivery ─────────────────────────────────────────────────────────


def send_web_push(subscription: dict[str, Any], payload: dict[str, Any]) -> bool | None:
    """Deliver one Web Push (blocking).

    :returns: ``True`` on accepted delivery, ``False`` when the subscription is
        gone (404/410 — caller should prune it), ``None`` when delivery could
        not be attempted (``pywebpush`` missing) or failed transiently.
    """
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        _logger.warning(
            "pywebpush is not installed; web push skipped. Reinstall the server "
            "to enable closed-app approval notifications."
        )
        return None
    _ensure_vapid()
    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps(payload),
            vapid_private_key=str(_vapid_pem_path()),
            vapid_claims={"sub": _VAPID_SUBJECT},
        )
        return True
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            return False
        _logger.warning("web push delivery failed: %s", exc)
        return None
    except Exception as exc:  # pragma: no cover - defensive
        _logger.warning("web push unexpected error: %s", exc)
        return None


def deliver_to_user(user_id: str, payload: dict[str, Any]) -> None:
    """Send a payload to all of a user's subscriptions, pruning dead ones."""
    for subscription in get_subscriptions(user_id):
        endpoint = _subscription_endpoint(subscription)
        result = send_web_push(subscription, payload)
        if result is False and endpoint is not None:
            remove_subscription(user_id, endpoint)
