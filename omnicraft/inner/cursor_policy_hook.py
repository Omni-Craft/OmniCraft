"""Cursor preToolUse hook script for OmniCraft policy enforcement.

Runs as a subprocess of the Cursor SDK bridge process, not the harness.

Reads tool-call info from stdin (Cursor hook protocol), evaluates
PHASE_TOOL_CALL policy via the OmniCraft server, and returns the
verdict on stdout.

Environment variables (baked into the hooks.json command by the
CursorExecutor at session startup):

    _OMNICRAFT_SERVER_URL  : Base URL of the OmniCraft server
                            (e.g. ``http://127.0.0.1:6767``).
    _OMNICRAFT_SESSION_ID  : Session / conversation ID for policy
                            evaluation.
"""

from __future__ import annotations

import json
import os
import sys

# Verdicts that grant the call. UNSPECIFIED means "no agent, no policies"
# (the server's pass-through), so it grants like the sibling hooks. Every
# other value -- unknown string, future POLICY_ACTION_* -- is not a vouched
# verdict and must not reach the else-branch as an allow.
_ALLOW_ACTIONS = frozenset({"POLICY_ACTION_ALLOW", "POLICY_ACTION_UNSPECIFIED"})

# Bounds the tool name echoed back to the agent; the raw value is untrusted.
_MAX_TOOL_NAME = 64


def _tool_label(raw: object) -> str:
    """Render an untrusted tool name for the agent transcript.

    A name carrying newlines or prompt-shaped text could inject into the
    transcript, so anything that isn't a short printable string collapses to
    a generic label and is reported by the caller on stderr instead.
    """
    if not isinstance(raw, str):
        return "Tool call"
    name = raw.strip()
    if not name or len(name) > _MAX_TOOL_NAME or not name.isprintable():
        return "Tool call"
    return f"Tool '{name}'"


def _deny(message: str, detail: str | None = None) -> None:
    """Emit a fail-closed verdict.

    ``message`` reaches the agent transcript, so it stays generic; ``detail``
    carries server URLs and auth diagnostics and goes to stderr only.
    """
    if detail:
        print(f"[cursor preToolUse] {message}: {detail}", file=sys.stderr)
    json.dump({"permission": "deny", "agent_message": message}, sys.stdout)


def main() -> None:
    server_url = os.environ.get("_OMNICRAFT_SERVER_URL", "")
    session_id = os.environ.get("_OMNICRAFT_SESSION_ID", "")

    if not server_url or not session_id:
        # No server wired -- no enforcement to bypass, so allow.
        json.dump({"permission": "allow"}, sys.stdout)
        return

    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
    except (json.JSONDecodeError, EOFError, ValueError) as exc:
        # A tool call we can't identify can't be evaluated -- deny rather
        # than let it through unreviewed.
        _deny("Tool call blocked: unreadable OmniCraft policy hook input", str(exc))
        return

    tool_name = payload.get("tool_name") or payload.get("toolName") or "unknown"
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}

    label = _tool_label(tool_name)
    if label == "Tool call":
        # The policy engine still sees the raw name; only the transcript is
        # spared it. Keep it on stderr so the deny stays diagnosable.
        print(f"[cursor preToolUse] unsafe tool name: {tool_name!r}", file=sys.stderr)

    # Build the evaluation request matching the server's EvaluationRequest
    # schema.
    eval_body: dict[str, object] = {
        "event": {
            "type": "PHASE_TOOL_CALL",
            "target": "",
            "data": {
                "name": tool_name,
                "arguments": tool_input if isinstance(tool_input, dict) else {},
            },
            "context": {},
        },
    }

    url = f"{server_url.rstrip('/')}/v1/sessions/{session_id}/policies/evaluate"

    try:
        from omnicraft.native_policy_hook import (
            policy_hook_reauth,
            policy_hook_request_headers,
            post_evaluate_with_retry,
        )

        headers = policy_hook_request_headers()
        reauth = policy_hook_reauth(server_url, headers)
        resp, api_error = post_evaluate_with_retry(
            url=url,
            headers=headers,
            eval_request=eval_body,
            # One day — must match the hooks.json ``timeout`` and the
            # server's ``ask_timeout`` so the hook stays alive while the
            # human responds to the web-UI approval card.
            read_timeout=86400.0,
            hook_label="cursor preToolUse",
            # Re-mint the baked one-shot token if it lapses mid-session.
            reauth=reauth,
        )
    except Exception as exc:  # noqa: BLE001 -- import / auth / unexpected error
        # Evaluation never ran, so nothing vouched for this call -- deny.
        _deny(f"{label} blocked: OmniCraft policy evaluation failed", repr(exc))
        return

    if resp is None:
        # Network error / retry budget exhausted -- fail closed so a
        # transient server outage doesn't skip DENY/ASK enforcement.
        _deny(
            f"{label} blocked: OmniCraft policy evaluation unavailable",
            api_error or reauth.failure_reason,
        )
        return

    malformed = f"{label} blocked: malformed OmniCraft policy response"

    try:
        result = resp.json()
    except Exception as exc:  # noqa: BLE001
        _deny(malformed, repr(exc))
        return

    if not isinstance(result, dict):
        _deny(malformed, f"expected a JSON object, got {type(result).__name__}")
        return

    # A body without a verdict is malformed -- never treat it as ALLOW.
    action = result.get("result")
    if not isinstance(action, str):
        _deny(malformed, f"'result' is {type(action).__name__}, expected str")
        return

    reason = result.get("reason", "")

    if action == "POLICY_ACTION_DENY":
        out: dict[str, str] = {"permission": "deny"}
        if reason:
            out["agent_message"] = f"{label} denied by OmniCraft policy: {reason}"
        json.dump(out, sys.stdout)
    elif action == "POLICY_ACTION_ASK":
        # The server resolves ASK by parking the HTTP request until the
        # human decides via the web-UI approval card and returning a hard
        # ALLOW/DENY.  Receiving ASK here means the gate was not held
        # (e.g. read-only caller) — fail closed rather than granting
        # unreviewed permission.
        out = {"permission": "deny"}
        if reason:
            out["agent_message"] = f"{label} requires approval: {reason}"
        json.dump(out, sys.stdout)
    elif action in _ALLOW_ACTIONS:
        json.dump({"permission": "allow"}, sys.stdout)
    else:
        # An unrecognized verdict vouches for nothing -- treat it as malformed.
        _deny(malformed, f"unknown verdict {action!r}")


if __name__ == "__main__":
    main()
