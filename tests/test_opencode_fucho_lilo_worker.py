"""Guards the OpenCode worker's presence in the shipped example agents.

Both fucho and lilo once declared an optional ``opencode`` sub-agent
(``harness: opencode-native``). Shipping a sub-agent whose harness older
clients don't recognize made every old runner/host fail to launch the agent at
all — the version-skew incident behind omnicraft-ai/omnicraft#1145. That incident
is now mitigated on the execution path: ``spec.load(...,
prune_invalid_sub_agents=True)`` (runner ``_entry`` + server ``agent_cache``)
gracefully DROPS a sub-agent whose harness a client doesn't recognize, so an old
client loads fucho with its remaining workers instead of failing. Combined with
``opencode-native`` now being a recognized harness
(``omnicraft.spec._omnicraft_compat.OMNICRAFT_HARNESSES``), fucho re-declares its
``opencode`` worker; the positive test below guards that it stays wired.

lilo, however, is still deliberately opencode-free (reverted in #1295), and the
negative test below guards that OpenCode does not creep back into that spec.
"""

from __future__ import annotations

from pathlib import Path

from omnicraft.spec import load

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _sub_agents(bundle: str) -> dict[str, object]:
    spec = load(_REPO_ROOT / "examples" / bundle)
    return {sa.name: sa for sa in (getattr(spec, "sub_agents", None) or [])}


def _config(sub_agent: object) -> dict[str, object]:
    executor = getattr(sub_agent, "executor", None)
    config = getattr(executor, "config", None)
    if isinstance(config, dict):
        return config
    return {}


def test_fucho_declares_opencode_worker() -> None:
    """fucho declares its ``opencode`` worker on the ``opencode-native`` harness.

    Safe to re-add because #1145's graceful pruning drops the worker (rather than
    failing the whole agent) on any client too old to recognize the harness, and
    ``opencode-native`` is a recognized harness on current clients. If this ever
    regresses to failing old clients, prune-on-load is the contract to check.
    """
    subs = _sub_agents("fucho")
    assert "opencode" in subs
    assert _config(subs["opencode"]).get("harness") == "opencode-native"


def test_lilo_does_not_declare_opencode_head() -> None:
    """lilo stays opencode-free, so an older client can load it without skew.

    lilo is reverted to its two-head roster (claude + gpt). Re-adding an
    ``opencode`` head (or any ``opencode-native`` harness override) would
    reintroduce the harness that broke old clients on spec validation.
    """
    subs = _sub_agents("lilo")
    assert "opencode" not in subs
    assert {"claude", "gpt"} <= set(subs)
    config_text = (_REPO_ROOT / "examples" / "lilo" / "config.yaml").read_text(encoding="utf-8")
    assert "opencode" not in config_text.lower()
    # No head re-introduces opencode-native via a harness override either.
    for sub in subs.values():
        assert "opencode-native" not in (_config(sub).get("allowed_harnesses") or [])
