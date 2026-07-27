#!/usr/bin/env python3
"""Drive one real turn and return the agent's answer.

The health check's decisive step: everything before it can pass while the
product still cannot answer a question — a missing provider key, an offline
host, a wedged runner. Asking something and waiting for the reply is the only
check that exercises the whole chain.

Reads its inputs from the environment (BASE_URL, SESSION, PROMPT, TIMEOUT) so
the shell script stays readable. Prints the answer on success; prints why it
failed and exits non-zero otherwise.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:6767").rstrip("/")
SESSION = os.environ.get("SESSION", "")
PROMPT = os.environ.get("PROMPT", "Responda apenas: OK")
TIMEOUT = float(os.environ.get("TIMEOUT", "90"))
# The turn is remote; polling faster only burns CPU waiting on a provider.
POLL_SECONDS = 2.0


def _request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    # The CSRF guard rejects a write whose Origin it doesn't trust; sending the
    # server's own origin is what the browser does.
    req.add_header("Origin", BASE_URL)
    with urllib.request.urlopen(req, timeout=30) as res:
        raw = res.read().decode()
    return json.loads(raw) if raw else {}


def _assistant_text(items: list) -> str | None:
    """The newest assistant message's text, if one has arrived."""
    for item in reversed(items):
        if item.get("role") != "assistant":
            continue
        parts = item.get("content") or []
        text = " ".join(
            part.get("text", "") for part in parts if isinstance(part, dict) and part.get("text")
        ).strip()
        if text:
            return text
    return None


def main() -> int:
    if not SESSION:
        print("SESSION não informada", file=sys.stderr)
        return 2
    try:
        _request(
            "POST",
            f"/v1/sessions/{SESSION}/events",
            {
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": PROMPT}]},
            },
        )
    except urllib.error.HTTPError as exc:
        print(
            f"enviar mensagem falhou: HTTP {exc.code} {exc.read()[:160].decode(errors='replace')}"
        )
        return 1
    except Exception as exc:  # noqa: BLE001 — any transport problem is a failure
        print(f"enviar mensagem falhou: {exc}")
        return 1

    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        try:
            payload = _request("GET", f"/v1/sessions/{SESSION}/items")
        except Exception:  # noqa: BLE001 — a blip shouldn't end the wait
            continue
        items = payload.get("data", payload if isinstance(payload, list) else [])
        answer = _assistant_text(items)
        if answer:
            print(answer.replace("\n", " ")[:200])
            return 0
    print("nenhuma resposta do agente dentro do tempo")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
