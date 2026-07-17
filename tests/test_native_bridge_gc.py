"""Tests for the shared native-bridge hash mapping and the runner-side GC."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import omnicraft.antigravity_native_bridge as agy_bridge
import omnicraft.codex_native_bridge as codex_bridge
from omnicraft.native_bridge_gc import (
    BridgeFamily,
    ConversationState,
    DirDisposition,
    GcConfig,
    bridge_id_digest,
    classify_bridge_dir,
    hashed_bridge_dir,
    is_removable,
    session_id_from_state_dir,
    sweep_native_bridge_dirs,
)

# ── Shared hash mapping (both directions, both families, label/fallback) ──────


def _legacy_digest(bridge_id: str) -> str:
    """The pre-refactor inline digest, reproduced to prove behavior is identical."""
    return hashlib.sha256(bridge_id.encode("utf-8")).hexdigest()[:32]


@pytest.mark.parametrize("bridge_id", ["conv_abc123", "bridge_x", "agy_conv_1", "", "../etc"])
def test_bridge_id_digest_matches_legacy(bridge_id: str) -> None:
    assert bridge_id_digest(bridge_id) == _legacy_digest(bridge_id)


def test_hashed_bridge_dir_composes_root_and_digest(tmp_path: Path) -> None:
    root = tmp_path / "codex-native"
    got = hashed_bridge_dir(root, "bridge_x")
    assert got == root / _legacy_digest("bridge_x")
    assert got.parent == root


def test_codex_bridge_dir_uses_shared_helper() -> None:
    for bridge_id in ("conv_abc123", "bridge_x"):
        got = codex_bridge.bridge_dir_for_bridge_id(bridge_id)
        assert got.name == _legacy_digest(bridge_id)
        assert got == hashed_bridge_dir(codex_bridge.bridge_root(), bridge_id)


def test_antigravity_bridge_dir_uses_shared_helper() -> None:
    for bridge_id in ("conv_abc123", "bridge_x"):
        got = agy_bridge.bridge_dir_for_bridge_id(bridge_id)
        assert got.name == _legacy_digest(bridge_id)
        assert got == hashed_bridge_dir(agy_bridge.bridge_root(), bridge_id)


def test_codex_and_antigravity_share_digest_but_differ_by_root() -> None:
    bridge_id = "conv_shared"
    codex_dir = codex_bridge.bridge_dir_for_bridge_id(bridge_id)
    agy_dir = agy_bridge.bridge_dir_for_bridge_id(bridge_id)
    # Same digest (same shared helper), different root.
    assert codex_dir.name == agy_dir.name == _legacy_digest(bridge_id)
    assert codex_dir.parent != agy_dir.parent


def test_codex_spawn_env_label_vs_conversation_fallback() -> None:
    conv = "conv_target"
    label = "bridge_rotated"
    # With a label the dir keys on the label...
    env_with_label = codex_bridge.build_codex_native_spawn_env(conv, bridge_id=label)
    assert env_with_label[codex_bridge.CODEX_NATIVE_BRIDGE_DIR_ENV_VAR].endswith(
        _legacy_digest(label)
    )
    # ...and the request session id is always the conversation id.
    assert env_with_label[codex_bridge.CODEX_NATIVE_REQUEST_SESSION_ID_ENV_VAR] == conv
    # Without a label it falls back to the conversation id.
    env_no_label = codex_bridge.build_codex_native_spawn_env(conv)
    assert env_no_label[codex_bridge.CODEX_NATIVE_BRIDGE_DIR_ENV_VAR].endswith(
        _legacy_digest(conv)
    )


def test_antigravity_spawn_env_label_vs_conversation_fallback() -> None:
    conv = "conv_target"
    label = "bridge_rotated"
    env_with_label = agy_bridge.build_antigravity_native_spawn_env(conv, bridge_id=label)
    assert env_with_label[agy_bridge.ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR].endswith(
        _legacy_digest(label)
    )
    assert env_with_label[agy_bridge.ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID_ENV_VAR] == conv
    env_no_label = agy_bridge.build_antigravity_native_spawn_env(conv)
    assert env_no_label[agy_bridge.ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR].endswith(
        _legacy_digest(conv)
    )


# ── Reverse handle: state dir -> session_id ──────────────────────────────────


def _write_state(state_dir: Path, payload: object) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(json.dumps(payload), encoding="utf-8")


def test_session_id_from_state_dir_codex_roundtrip(tmp_path: Path) -> None:
    bridge_dir = tmp_path / bridge_id_digest("conv_x")
    codex_bridge.write_bridge_state(
        bridge_dir,
        codex_bridge.CodexNativeBridgeState(
            session_id="conv_x",
            socket_path=str(bridge_dir / "app-server.sock"),
            thread_id="thread_1",
            codex_home=str(bridge_dir / "codex-home"),
        ),
    )
    assert session_id_from_state_dir(bridge_dir) == "conv_x"


def test_session_id_from_state_dir_antigravity_roundtrip(tmp_path: Path) -> None:
    bridge_dir = tmp_path / bridge_id_digest("conv_y")
    agy_bridge.write_bridge_state(
        bridge_dir,
        agy_bridge.AntigravityNativeBridgeState(
            session_id="conv_y",
            conversation_id="agy-real-uuid",
        ),
    )
    assert session_id_from_state_dir(bridge_dir) == "conv_y"


@pytest.mark.parametrize(
    "payload",
    [None, "not-an-object", {"session_id": ""}, {"session_id": 123}, {"other": "x"}],
)
def test_session_id_from_state_dir_rejects_bad_shapes(tmp_path: Path, payload: object) -> None:
    state_dir = tmp_path / "d"
    _write_state(state_dir, payload)
    assert session_id_from_state_dir(state_dir) is None


def test_session_id_from_state_dir_missing_or_corrupt(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    missing.mkdir()
    assert session_id_from_state_dir(missing) is None
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / "state.json").write_text("{not json", encoding="utf-8")
    assert session_id_from_state_dir(corrupt) is None


# ── Decision logic (classify_bridge_dir / is_removable) ──────────────────────

_CUTOFF = 1_000


def _classify(**overrides: object) -> DirDisposition:
    base: dict[str, object] = {
        "state_readable": True,
        "conversation_exists": True,
        "conversation_archived": False,
        "conversation_updated_at": _CUTOFF + 100,
        "in_active_turns": False,
        "bound_to_runner": False,
        "process_live": False,
        "archived_ttl_cutoff": _CUTOFF,
    }
    base.update(overrides)
    return classify_bridge_dir(**base)  # type: ignore[arg-type]


def test_orphan_when_conversation_gone() -> None:
    assert _classify(conversation_exists=False) is DirDisposition.ORPHAN


@pytest.mark.parametrize("veto", ["in_active_turns", "bound_to_runner", "process_live"])
def test_liveness_is_absolute_veto_over_orphan(veto: str) -> None:
    # Even a "conversation gone" dir is LIVE if any liveness signal fires.
    assert _classify(conversation_exists=False, **{veto: True}) is DirDisposition.LIVE


@pytest.mark.parametrize("veto", ["in_active_turns", "bound_to_runner", "process_live"])
def test_liveness_is_absolute_veto_over_unknown(veto: str) -> None:
    assert _classify(state_readable=False, **{veto: True}) is DirDisposition.LIVE


def test_unknown_when_state_unreadable() -> None:
    assert _classify(state_readable=False) is DirDisposition.UNKNOWN


def test_active_session_kept() -> None:
    assert _classify() is DirDisposition.ACTIVE_SESSION


def test_archived_expired_vs_recent() -> None:
    assert (
        _classify(conversation_archived=True, conversation_updated_at=_CUTOFF - 1)
        is DirDisposition.ARCHIVED_EXPIRED
    )
    assert (
        _classify(conversation_archived=True, conversation_updated_at=_CUTOFF + 1)
        is DirDisposition.ARCHIVED_RECENT
    )
    # Unknown updated_at on an archived conv is treated as not-expired (kept).
    assert (
        _classify(conversation_archived=True, conversation_updated_at=None)
        is DirDisposition.ARCHIVED_RECENT
    )


def test_is_removable_policy_matrix() -> None:
    # Orphan: always removable.
    assert is_removable(DirDisposition.ORPHAN, remove_archived_expired=False, remove_unknown=False)
    # Archived-expired: opt-in.
    assert not is_removable(
        DirDisposition.ARCHIVED_EXPIRED, remove_archived_expired=False, remove_unknown=False
    )
    assert is_removable(
        DirDisposition.ARCHIVED_EXPIRED, remove_archived_expired=True, remove_unknown=False
    )
    # Unknown: opt-in.
    assert not is_removable(
        DirDisposition.UNKNOWN, remove_archived_expired=False, remove_unknown=False
    )
    assert is_removable(DirDisposition.UNKNOWN, remove_archived_expired=False, remove_unknown=True)
    # Never removable.
    for keep in (
        DirDisposition.LIVE,
        DirDisposition.ACTIVE_SESSION,
        DirDisposition.ARCHIVED_RECENT,
    ):
        assert not is_removable(keep, remove_archived_expired=True, remove_unknown=True)


# ── Sweep orchestration ──────────────────────────────────────────────────────


def _cfg(**overrides: object) -> GcConfig:
    base: dict[str, object] = {
        "enabled": True,
        "dry_run": False,
        "archived_ttl_seconds": 3600,
        "remove_archived_expired": False,
        "remove_unknown": False,
    }
    base.update(overrides)
    return GcConfig(**base)  # type: ignore[arg-type]


def _family(root: Path, *, name: str = "codex", live: set[str] | None = None) -> BridgeFamily:
    live = live or set()
    return BridgeFamily(
        name=name,
        root=root,
        process_live=lambda d: d.name in live,
    )


# A valid 32-hex bridge-dir name for unknown-format (no state.json) dirs.
_UNKNOWN_HASH = "deadbeef" * 4


def _make_dir(root: Path, session_id: str | None, *, name: str | None = None) -> Path:
    d = root / (name or (bridge_id_digest(session_id) if session_id else _UNKNOWN_HASH))
    d.mkdir(parents=True, exist_ok=True)
    (d / "codex-home").mkdir(exist_ok=True)  # simulate embedded tool home
    if session_id is not None:
        (d / "state.json").write_text(json.dumps({"session_id": session_id}), encoding="utf-8")
    return d


async def _sweep(root: Path, cfg: GcConfig, convs: dict[str, ConversationState], **kw: object):
    async def resolve(session_id: str) -> ConversationState:
        return convs.get(session_id, ConversationState(exists=False))

    removed: list[Path] = []

    def remove(d: Path) -> None:
        removed.append(d)
        import shutil

        shutil.rmtree(d)

    active = frozenset(kw.get("active_turns", frozenset()))  # type: ignore[arg-type]
    counts = await sweep_native_bridge_dirs(
        families=[_family(root, live=kw.get("live"))],  # type: ignore[arg-type]
        config=cfg,
        resolve_conversation=resolve,
        active_turns=lambda: active,
        now=kw.get("now", 10_000),  # type: ignore[arg-type]
        remove_dir=remove,
    )
    return counts, removed


@pytest.mark.asyncio
async def test_sweep_removes_orphan(tmp_path: Path) -> None:
    d = _make_dir(tmp_path, "conv_gone")
    counts, removed = await _sweep(tmp_path, _cfg(), {})
    assert counts.removed_orphan == 1
    assert removed == [d]
    assert not d.exists()


@pytest.mark.asyncio
async def test_sweep_keeps_existing_unbound_session(tmp_path: Path) -> None:
    d = _make_dir(tmp_path, "conv_live")
    convs = {"conv_live": ConversationState(exists=True)}
    counts, removed = await _sweep(tmp_path, _cfg(), convs)
    assert counts.skipped_active_session == 1
    assert removed == []
    assert d.exists()


@pytest.mark.asyncio
async def test_sweep_skips_live_via_active_turns(tmp_path: Path) -> None:
    _make_dir(tmp_path, "conv_gone")  # conversation gone -> would be orphan
    counts, removed = await _sweep(tmp_path, _cfg(), {}, active_turns=frozenset({"conv_gone"}))
    assert counts.skipped_live == 1
    assert counts.removed_orphan == 0
    assert removed == []


@pytest.mark.asyncio
async def test_sweep_skips_live_via_db_binding(tmp_path: Path) -> None:
    _make_dir(tmp_path, "conv_gone")
    convs = {"conv_gone": ConversationState(exists=True, bound=True)}
    counts, removed = await _sweep(tmp_path, _cfg(), convs)
    assert counts.skipped_live == 1
    assert removed == []


@pytest.mark.asyncio
async def test_sweep_skips_live_via_process_probe(tmp_path: Path) -> None:
    d = _make_dir(tmp_path, "conv_gone")  # gone in DB, but a live process owns it
    counts, removed = await _sweep(tmp_path, _cfg(), {}, live={d.name})
    assert counts.skipped_live == 1
    assert removed == []
    assert d.exists()


@pytest.mark.asyncio
async def test_sweep_unknown_bucketed_not_deleted_by_default(tmp_path: Path) -> None:
    d = _make_dir(tmp_path, None)  # no state.json
    counts, removed = await _sweep(tmp_path, _cfg(), {})
    assert counts.skipped_unknown == 1
    assert removed == []
    assert d.exists()


@pytest.mark.asyncio
async def test_sweep_ignores_non_hash_entries(tmp_path: Path) -> None:
    """Non-digest entries (codex ``process-owners/`` lock dir, stray files) are
    never enumerated — even with every removal opt-in on."""
    owners = tmp_path / "process-owners"  # the codex owner-lock dir
    owners.mkdir()
    (owners / "abc.lock").write_text("", encoding="utf-8")
    (tmp_path / "process-registry.json").write_text("[]", encoding="utf-8")
    (tmp_path / "not-a-hash").mkdir()
    counts, removed = await _sweep(
        tmp_path, _cfg(remove_unknown=True, remove_archived_expired=True), {}, now=10_000
    )
    assert counts.scanned == 0
    assert removed == []
    assert owners.exists()


@pytest.mark.asyncio
async def test_sweep_unknown_removed_when_opted_in_and_old(tmp_path: Path) -> None:
    import os

    d = _make_dir(tmp_path, None)
    old = 1_000  # long before `now`
    os.utime(d, (old, old))
    counts, removed = await _sweep(tmp_path, _cfg(remove_unknown=True), {}, now=10_000)
    assert counts.removed_unknown == 1
    assert removed == [d]


@pytest.mark.asyncio
async def test_sweep_unknown_kept_when_too_new_even_if_opted_in(tmp_path: Path) -> None:
    import os

    d = _make_dir(tmp_path, None)
    os.utime(d, (9_999, 9_999))  # freshly touched relative to now=10_000, ttl=3600
    counts, removed = await _sweep(tmp_path, _cfg(remove_unknown=True), {}, now=10_000)
    assert counts.skipped_unknown_too_new == 1
    assert removed == []
    assert d.exists()


@pytest.mark.asyncio
async def test_sweep_archived_ttl_only_when_opted_in(tmp_path: Path) -> None:
    _make_dir(tmp_path, "conv_arch")
    # archived, last activity well before cutoff (now - ttl).
    convs = {"conv_arch": ConversationState(exists=True, archived=True, updated_at=1)}
    # Default: archived removal off -> kept.
    counts, removed = await _sweep(tmp_path, _cfg(), convs, now=10_000)
    assert counts.removed_archived_expired == 0
    assert removed == []
    # Opted in -> removed.
    _make_dir(tmp_path, "conv_arch")  # recreate (previous run kept it anyway)
    counts, removed = await _sweep(tmp_path, _cfg(remove_archived_expired=True), convs, now=10_000)
    assert counts.removed_archived_expired == 1


@pytest.mark.asyncio
async def test_sweep_archived_recent_kept_even_when_opted_in(tmp_path: Path) -> None:
    _make_dir(tmp_path, "conv_arch")
    convs = {"conv_arch": ConversationState(exists=True, archived=True, updated_at=9_999)}
    counts, removed = await _sweep(tmp_path, _cfg(remove_archived_expired=True), convs, now=10_000)
    assert counts.removed_archived_expired == 0
    assert removed == []


@pytest.mark.asyncio
async def test_sweep_dry_run_deletes_nothing_but_logs(tmp_path: Path) -> None:
    d = _make_dir(tmp_path, "conv_gone")
    counts, removed = await _sweep(tmp_path, _cfg(dry_run=True), {})
    assert counts.would_remove == 1
    assert counts.removed_orphan == 0
    assert removed == []
    assert d.exists()


@pytest.mark.asyncio
async def test_sweep_disabled_is_noop(tmp_path: Path) -> None:
    d = _make_dir(tmp_path, "conv_gone")
    counts, removed = await _sweep(tmp_path, _cfg(enabled=False), {})
    assert counts.scanned == 0
    assert removed == []
    assert d.exists()


@pytest.mark.asyncio
async def test_sweep_missing_root_is_noop(tmp_path: Path) -> None:
    counts, removed = await _sweep(tmp_path / "does-not-exist", _cfg(), {})
    assert counts.scanned == 0
    assert removed == []


@pytest.mark.asyncio
async def test_sweep_pre_sweep_hook_runs_and_errors_are_swallowed(tmp_path: Path) -> None:
    _make_dir(tmp_path, "conv_gone")
    calls: list[str] = []

    def pre() -> None:
        calls.append("pre")
        raise RuntimeError("boom")

    async def resolve(_sid: str) -> ConversationState:
        return ConversationState(exists=False)

    fam = BridgeFamily(name="codex", root=tmp_path, process_live=lambda _d: False, pre_sweep=pre)
    counts = await sweep_native_bridge_dirs(
        families=[fam],
        config=_cfg(dry_run=True),
        resolve_conversation=resolve,
        active_turns=frozenset,
        now=10_000,
    )
    assert calls == ["pre"]  # hook ran despite raising
    assert counts.would_remove == 1  # sweep proceeded


@pytest.mark.asyncio
async def test_sweep_does_not_resolve_when_local_veto_hits(tmp_path: Path) -> None:
    """A live/active dir must not incur a server round-trip."""
    d = _make_dir(tmp_path, "conv_x")
    resolved: list[str] = []

    async def resolve(session_id: str) -> ConversationState:
        resolved.append(session_id)
        return ConversationState(exists=True)

    fam = _family(tmp_path, live={d.name})
    await sweep_native_bridge_dirs(
        families=[fam],
        config=_cfg(),
        resolve_conversation=resolve,
        active_turns=frozenset,
        now=10_000,
    )
    assert resolved == []  # process-live short-circuited the resolve


# ── Two-phase atomic recheck: a signal flipping live between classify + rmtree ─


async def _orphan_resolve(_sid: str) -> ConversationState:
    return ConversationState(exists=False)


@pytest.mark.asyncio
async def test_recheck_process_race_keeps_dir(tmp_path: Path) -> None:
    """Process comes up between classification and removal → dir kept."""
    d = _make_dir(tmp_path, "conv_gone")
    calls = {"n": 0}

    def process_live(_d: Path) -> bool:
        calls["n"] += 1
        return calls["n"] >= 2  # dead at classify, live at the recheck

    fam = BridgeFamily(name="codex", root=tmp_path, process_live=process_live)
    removed: list[Path] = []
    counts = await sweep_native_bridge_dirs(
        families=[fam],
        config=_cfg(),
        resolve_conversation=_orphan_resolve,
        active_turns=frozenset,
        now=10_000,
        remove_dir=removed.append,
    )
    assert counts.skipped_live_raced == 1
    assert counts.removed_orphan == 0
    assert removed == []
    assert d.exists()


@pytest.mark.asyncio
async def test_recheck_active_turns_race_keeps_dir(tmp_path: Path) -> None:
    """A turn starts between classification and removal → dir kept."""
    d = _make_dir(tmp_path, "conv_gone")
    snapshots = iter([frozenset(), frozenset({"conv_gone"})])
    removed: list[Path] = []
    counts = await sweep_native_bridge_dirs(
        families=[_family(tmp_path)],  # process_live False
        config=_cfg(),
        resolve_conversation=_orphan_resolve,
        active_turns=lambda: next(snapshots),
        now=10_000,
        remove_dir=removed.append,
    )
    assert counts.skipped_live_raced == 1
    assert removed == []
    assert d.exists()


@pytest.mark.asyncio
async def test_recheck_db_binding_race_keeps_dir(tmp_path: Path) -> None:
    """A runner binds the conversation between classification and removal → kept."""
    d = _make_dir(tmp_path, "conv_x")
    results = iter([ConversationState(exists=False), ConversationState(exists=True, bound=True)])

    async def resolve(_sid: str) -> ConversationState:
        return next(results)

    removed: list[Path] = []
    counts = await sweep_native_bridge_dirs(
        families=[_family(tmp_path)],  # process_live False
        config=_cfg(),
        resolve_conversation=resolve,
        active_turns=frozenset,
        now=10_000,
        remove_dir=removed.append,
    )
    assert counts.skipped_live_raced == 1
    assert removed == []
    assert d.exists()


@pytest.mark.asyncio
async def test_worker_process_race_keeps_dir(tmp_path: Path) -> None:
    """Process comes up AFTER the async recheck but before the in-worker rmtree.

    The signal is dead through the initial classify (call 1) and the async
    recheck (call 2), and only flips live on the in-worker probe (call 3) — so
    only the final local veto adjacent to rmtree can save the dir.
    """
    d = _make_dir(tmp_path, "conv_gone")
    calls = {"n": 0}

    def process_live(_d: Path) -> bool:
        calls["n"] += 1
        return calls["n"] >= 3  # dead at classify + async recheck; live in worker

    fam = BridgeFamily(name="codex", root=tmp_path, process_live=process_live)
    removed: list[Path] = []
    counts = await sweep_native_bridge_dirs(
        families=[fam],
        config=_cfg(),
        resolve_conversation=_orphan_resolve,
        active_turns=frozenset,
        now=10_000,
        remove_dir=removed.append,
    )
    assert calls["n"] == 3  # reached the in-worker probe
    assert counts.skipped_live_raced == 1  # counted once — no double-count
    assert counts.removed_orphan == 0
    assert removed == []
    assert d.exists()


@pytest.mark.asyncio
async def test_worker_active_turns_race_keeps_dir(tmp_path: Path) -> None:
    """A turn starts AFTER the async recheck but before the in-worker rmtree."""
    d = _make_dir(tmp_path, "conv_gone")
    # classify (empty) → async recheck (empty) → in-worker snapshot ({conv_gone}).
    snapshots = iter([frozenset(), frozenset(), frozenset({"conv_gone"})])
    removed: list[Path] = []
    counts = await sweep_native_bridge_dirs(
        families=[_family(tmp_path)],  # process_live False everywhere
        config=_cfg(),
        resolve_conversation=_orphan_resolve,
        active_turns=lambda: next(snapshots),
        now=10_000,
        remove_dir=removed.append,
    )
    assert counts.skipped_live_raced == 1
    assert counts.removed_orphan == 0
    assert removed == []
    assert d.exists()


@pytest.mark.asyncio
async def test_recheck_stable_still_removes(tmp_path: Path) -> None:
    """When nothing races, both rechecks agree and the orphan is removed."""
    d = _make_dir(tmp_path, "conv_gone")
    counts, removed = await _sweep(tmp_path, _cfg(), {})
    assert counts.skipped_live_raced == 0
    assert counts.removed_orphan == 1
    assert removed == [d]


@pytest.mark.asyncio
async def test_archived_expired_disabled_has_own_bucket(tmp_path: Path) -> None:
    """An archived-past-TTL dir with removal off counts as disabled, not recent."""
    _make_dir(tmp_path, "conv_arch")
    convs = {"conv_arch": ConversationState(exists=True, archived=True, updated_at=1)}
    counts, removed = await _sweep(tmp_path, _cfg(), convs, now=10_000)
    assert counts.skipped_archived_disabled == 1
    assert counts.skipped_archived_recent == 0
    assert removed == []
