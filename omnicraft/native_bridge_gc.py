"""Shared hash↔dir mapping and runner-side GC for native-harness state dirs.

Native TUI harnesses (``codex-native``, ``antigravity-native``, …) each keep a
per-session state directory under ``~/.omnicraft/<harness>-native/<digest>`` that
embeds a full isolated tool home (``codex-home`` / ``agy-home``) plus a bridge
token and MCP config. On a clean session delete the runner already removes the
matching dir (``runner.app._delete_native_bridge_dirs``), but crashed or
never-explicitly-deleted sessions leave their dirs behind, and across dead
sessions these accumulate to ~1.2GB of disk.

This module carries two concerns:

1. **The shared hash mapping** (:func:`bridge_id_digest`,
   :func:`hashed_bridge_dir`, :func:`session_id_from_state_dir`). Both
   ``codex_native_bridge`` and ``antigravity_native_bridge`` compute their per
   -session dir as ``<root>/sha256(bridge_id)[:32]`` and store the plaintext
   OmniCraft ``session_id`` inside ``state.json`` — the reverse handle from a dir
   back to its conversation. That mapping lives here once and both bridge modules
   call it (parametrized by their own root).

2. **The runner-side garbage collector** (:func:`classify_bridge_dir`,
   :func:`sweep_native_bridge_dirs`, :class:`NativeBridgeGarbageCollector`). A
   startup + periodic sweep that resolves every on-disk dir to a conversation and
   removes only genuinely-dead state, gated by an absolute liveness veto.

The mapping helpers are pure stdlib and safe to import from the bridge modules;
the sweep imports the bridge modules lazily so there is no import cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import shutil
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_logger = logging.getLogger(__name__)

_STATE_FILE = "state.json"
# Both bridge families hash the bridge id with sha256 and keep the first 32 hex
# chars as the dir name. Single-sourced here so the two impls cannot drift.
_BRIDGE_ID_HASH_CHARS = 32
# A bridge dir name is exactly the truncated lowercase-hex digest. The sweep uses
# this to enumerate ONLY bridge dirs: the codex-native root doubles as the codex
# process-registry state root, so it also holds ``process-registry.json`` (a file)
# and a ``process-owners/`` lock dir — neither is a bridge dir, and removing the
# owner-lock dir would break crash reconciliation. The name filter excludes them.
_HASH_DIR_RE = re.compile(r"\A[0-9a-f]{32}\Z")


# ── Shared hash mapping ──────────────────────────────────────────────────────


def bridge_id_digest(bridge_id: str) -> str:
    """Return the dir-name digest for a native-harness bridge id.

    :param bridge_id: Opaque bridge id (a rotatable session label, or the
        conversation id when no label was minted), e.g. ``"conv_abc123"``.
    :returns: The first 32 hex chars of ``sha256(bridge_id)``.
    """
    return hashlib.sha256(bridge_id.encode("utf-8")).hexdigest()[:_BRIDGE_ID_HASH_CHARS]


def hashed_bridge_dir(root: Path, bridge_id: str) -> Path:
    """Return the per-session bridge directory for a bridge id under *root*.

    :param root: Native-harness bridge root, e.g.
        ``Path("~/.omnicraft/codex-native")``.
    :param bridge_id: Opaque bridge id, e.g. ``"bridge_abc123"``.
    :returns: ``root / sha256(bridge_id)[:32]``.
    """
    return root / bridge_id_digest(bridge_id)


def session_id_from_state_dir(state_dir: Path) -> str | None:
    """Read the plaintext OmniCraft ``session_id`` from a dir's ``state.json``.

    This is the reverse handle: a dir on disk → the conversation that owns it.
    Both bridge families persist ``session_id`` (the OmniCraft conversation id)
    at the top level of ``state.json`` (see ``write_bridge_state`` in each).

    By design this is a GC-only reverse map — the two bridges never need to go
    dir → session_id, only forward (``bridge_id`` → digest). The shared-helper
    dedup was about that FORWARD digest (now single-sourced in
    :func:`bridge_id_digest` and called by both bridges); this reverse handle
    has exactly one caller (the sweep) and intentionally lives beside it.

    :param state_dir: A native-harness per-session state directory.
    :returns: The non-empty ``session_id`` string, or ``None`` when the file is
        missing, unreadable, not JSON, not an object, or lacks a usable
        ``session_id`` (an *unknown-format* dir — never resolvable to a
        conversation).
    """
    path = state_dir / _STATE_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    session_id = raw.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


# ── Sweep decision logic (pure, unit-testable) ───────────────────────────────


class DirDisposition(str, Enum):
    """What a candidate bridge dir is, independent of config policy.

    The classification is a *fact* about the dir; whether that fact makes the
    dir removable is a separate, config-gated decision (:func:`is_removable`) so
    the safety-critical veto logic can be unit-tested without config plumbing.
    """

    #: A liveness signal fired (active turn, DB binding, or a live vendor
    #: process/pane). Never removed, in any mode. Absolute veto.
    LIVE = "live"
    #: ``state.json`` resolved to a conversation that no longer exists. Removed
    #: by default (the crash / never-explicitly-deleted orphan case).
    ORPHAN = "orphan"
    #: The conversation exists, is archived, and its last activity is older than
    #: the configured TTL. Removed only when archived removal is opted in.
    ARCHIVED_EXPIRED = "archived_expired"
    #: The conversation exists and is archived but was active within the TTL. Kept.
    ARCHIVED_RECENT = "archived_recent"
    #: The conversation exists and is not archived — a real, resumable session. Kept.
    ACTIVE_SESSION = "active_session"
    #: ``state.json`` is missing/unreadable/unparseable, so the dir cannot be
    #: resolved to any conversation. Removed only when unknown-format removal is
    #: opted in (and, in the sweep, only past an age guard).
    UNKNOWN = "unknown"


def classify_bridge_dir(
    *,
    state_readable: bool,
    conversation_exists: bool,
    conversation_archived: bool,
    conversation_updated_at: int | None,
    in_active_turns: bool,
    bound_to_runner: bool,
    process_live: bool,
    archived_ttl_cutoff: int,
) -> DirDisposition:
    """Classify one candidate bridge dir from already-gathered facts.

    Liveness is an **absolute veto**: if *any* of the three liveness signals is
    positive the dir is :attr:`DirDisposition.LIVE` regardless of everything
    else, so a running session's dir is never a removal candidate. The liveness
    check comes first precisely so it cannot be short-circuited by a "conversation
    gone" verdict (a live process whose ``state.json`` is corrupt, say).

    :param state_readable: Whether ``state.json`` yielded a ``session_id``.
        ``False`` ⇒ unknown-format (unless a liveness signal fires).
    :param conversation_exists: Whether the resolved conversation still exists.
    :param conversation_archived: Whether that conversation is archived.
    :param conversation_updated_at: The conversation's ``updated_at`` (Unix
        epoch, bumped on every item append), or ``None`` when unknown — a ``None``
        is treated as *not past TTL* so an archived dir with unknown activity is
        kept rather than TTL-removed.
    :param in_active_turns: Whether this runner has an in-flight turn for the
        conversation (in-memory ``_active_turns`` membership).
    :param bound_to_runner: Whether the conversation has a non-null
        ``host_id``/``runner_id`` (bound to a runner in the DB).
    :param process_live: Whether a live vendor process/pane owns the dir
        (codex app-server socket accepting connections / agy tmux pane alive).
    :param archived_ttl_cutoff: Epoch before which an archived conversation's
        last activity counts as expired.
    :returns: The dir's :class:`DirDisposition`.
    """
    if in_active_turns or bound_to_runner or process_live:
        return DirDisposition.LIVE
    if not state_readable:
        return DirDisposition.UNKNOWN
    if not conversation_exists:
        return DirDisposition.ORPHAN
    if conversation_archived:
        if conversation_updated_at is not None and conversation_updated_at < archived_ttl_cutoff:
            return DirDisposition.ARCHIVED_EXPIRED
        return DirDisposition.ARCHIVED_RECENT
    return DirDisposition.ACTIVE_SESSION


def is_removable(
    disposition: DirDisposition,
    *,
    remove_archived_expired: bool,
    remove_unknown: bool,
) -> bool:
    """Map a disposition + opt-in policy to whether the dir may be removed.

    Orphans are removable by default; archived-past-TTL and unknown-format are
    both opt-in (off by default). ``LIVE`` / ``ACTIVE_SESSION`` / ``ARCHIVED_RECENT``
    are never removable.

    :param disposition: The dir's classification.
    :param remove_archived_expired: Opt-in flag for archived-past-TTL removal.
    :param remove_unknown: Opt-in flag for unknown-format removal.
    :returns: ``True`` when policy permits removal (dry-run still logs only).
    """
    if disposition is DirDisposition.ORPHAN:
        return True
    if disposition is DirDisposition.ARCHIVED_EXPIRED:
        return remove_archived_expired
    if disposition is DirDisposition.UNKNOWN:
        return remove_unknown
    return False


# ── Sweep orchestration ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConversationState:
    """The GC-relevant snapshot of a conversation, resolved from the server.

    :param exists: Whether the conversation still exists (``False`` ⇒ orphan).
    :param archived: Whether it is archived.
    :param bound: Whether it is bound to a runner (non-null host/runner id).
    :param updated_at: Last-activity epoch, or ``None`` when unavailable.
    """

    exists: bool
    archived: bool = False
    bound: bool = False
    updated_at: int | None = None


@dataclass(frozen=True)
class GcConfig:
    """Resolved policy for one sweep tick.

    :param enabled: Master switch; a disabled sweep does nothing.
    :param dry_run: When ``True``, nothing is deleted — removals are only logged.
    :param archived_ttl_seconds: Age past which an archived conversation's dir is
        TTL-eligible, and the minimum dir age before an unknown-format dir may be
        removed.
    :param remove_archived_expired: Opt-in: remove archived-past-TTL dirs.
    :param remove_unknown: Opt-in: remove unknown-format dirs.
    """

    enabled: bool
    dry_run: bool
    archived_ttl_seconds: int
    remove_archived_expired: bool
    remove_unknown: bool


@dataclass(frozen=True)
class BridgeFamily:
    """One native-harness family the sweep enumerates.

    :param name: Short family name for logs, e.g. ``"codex"``.
    :param root: The family's bridge root (``bridge_root()``).
    :param process_live: Sync per-dir probe — ``True`` when a live vendor
        process/pane owns the dir. Runs off the event loop.
    :param pre_sweep: Optional sync hook run once before the family is
        enumerated (e.g. reconciling the codex crash-safe process registry).
    """

    name: str
    root: Path
    process_live: Callable[[Path], bool]
    pre_sweep: Callable[[], None] | None = None


@dataclass
class SweepCounts:
    """Tally of one sweep, for logging and tests."""

    scanned: int = 0
    removed_orphan: int = 0
    removed_archived_expired: int = 0
    removed_unknown: int = 0
    would_remove: int = 0  # dry-run: dirs that a real run would have removed
    skipped_live: int = 0
    # A dir eligible at classification whose FULL liveness gate went positive in
    # the window before rmtree (the atomic-recheck save).
    skipped_live_raced: int = 0
    skipped_active_session: int = 0
    skipped_archived_recent: int = 0
    # Archived past the TTL but retained because remove_archived is off — NOT
    # "recent". A distinct bucket so operators see what opting in would reclaim.
    skipped_archived_disabled: int = 0
    skipped_unknown: int = 0
    skipped_unknown_too_new: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        """Return a plain mapping of the non-zero counters (stable key order)."""
        return {
            "scanned": self.scanned,
            "removed_orphan": self.removed_orphan,
            "removed_archived_expired": self.removed_archived_expired,
            "removed_unknown": self.removed_unknown,
            "would_remove": self.would_remove,
            "skipped_live": self.skipped_live,
            "skipped_live_raced": self.skipped_live_raced,
            "skipped_active_session": self.skipped_active_session,
            "skipped_archived_recent": self.skipped_archived_recent,
            "skipped_archived_disabled": self.skipped_archived_disabled,
            "skipped_unknown": self.skipped_unknown,
            "skipped_unknown_too_new": self.skipped_unknown_too_new,
            "errors": self.errors,
        }


def _list_hash_dirs(root: Path) -> list[Path]:
    """Return the bridge-dir children of a root (digest-named dirs only).

    Non-digest entries (the codex process registry's ``process-owners/`` lock
    dir, stray files) are excluded so the sweep never classifies — let alone
    removes — anything that is not a per-session bridge dir. Empty when the root
    is absent or unreadable.
    """
    try:
        return [
            child for child in root.iterdir() if child.is_dir() and _HASH_DIR_RE.match(child.name)
        ]
    except FileNotFoundError:
        return []
    except OSError:
        _logger.warning("native-bridge-gc: could not list %s", root, exc_info=True)
        return []


def _dir_mtime(state_dir: Path) -> float | None:
    """Return the dir's mtime, or ``None`` when it cannot be stat'd."""
    try:
        return state_dir.stat().st_mtime
    except OSError:
        return None


_RemoveDir = Callable[[Path], None]


def _default_remove_dir(state_dir: Path) -> None:
    shutil.rmtree(state_dir, ignore_errors=False)


async def sweep_native_bridge_dirs(
    *,
    families: Sequence[BridgeFamily],
    config: GcConfig,
    resolve_conversation: Callable[[str], Awaitable[ConversationState]],
    active_turns: Callable[[], frozenset[str]],
    now: int,
    remove_dir: _RemoveDir = _default_remove_dir,
) -> SweepCounts:
    """Run one GC sweep over every family's on-disk dirs.

    For each dir: probe liveness (off-loop), resolve its conversation via
    *resolve_conversation* (only when ``state.json`` yielded a ``session_id``),
    classify, and — unless ``dry_run`` — remove when policy allows. A live dir is
    logged as ``skipped: live`` even in dry-run so the veto is always visible. An
    unknown-format dir is removed only when opted in **and** older than
    ``archived_ttl_seconds`` (so a mid-launch dir whose ``state.json`` has not
    landed yet is never nuked).

    **Atomic recheck.** Classification and removal are not simultaneous: a turn
    can start, a runner can bind, or the vendor process can come up in between.
    Immediately before ``rmtree`` the full classification is recomputed (fresh
    liveness probe + fresh ``active_turns`` snapshot + fresh conversation
    resolve); if the disposition changed at all the removal is aborted and logged
    as ``skipped: live (raced)``. Mirrors ``unbound_session_sweep``'s
    select→per-row-recheck pattern.

    All blocking work (dir listing, ``state.json`` reads, liveness probes,
    ``rmtree``) runs via :func:`asyncio.to_thread`; only *resolve_conversation*
    (an HTTP round-trip to the server) awaits directly.

    :param families: Bridge families to enumerate.
    :param config: Resolved sweep policy for this tick.
    :param resolve_conversation: ``async session_id -> ConversationState``.
    :param active_turns: Zero-arg callable returning a *fresh* snapshot of this
        runner's in-flight conversation ids — re-read at both classification and
        the pre-removal recheck so a turn that starts mid-sweep vetoes removal.
    :param now: Current Unix epoch (passed in — the sweep does no wall-clock read
        itself, keeping it deterministic under test).
    :param remove_dir: Injectable deletion (defaults to ``shutil.rmtree``).
    :returns: The sweep's :class:`SweepCounts`.
    """
    counts = SweepCounts()
    if not config.enabled:
        return counts
    cutoff = now - config.archived_ttl_seconds
    for family in families:
        if family.pre_sweep is not None:
            try:
                await asyncio.to_thread(family.pre_sweep)
            except Exception:  # noqa: BLE001 — a pre-sweep hook must never sink the GC
                _logger.warning(
                    "native-bridge-gc: %s pre-sweep hook failed", family.name, exc_info=True
                )
        for state_dir in await asyncio.to_thread(_list_hash_dirs, family.root):
            counts.scanned += 1
            try:
                await _sweep_one_dir(
                    family=family,
                    state_dir=state_dir,
                    config=config,
                    resolve_conversation=resolve_conversation,
                    active_turns=active_turns,
                    cutoff=cutoff,
                    now=now,
                    remove_dir=remove_dir,
                    counts=counts,
                )
            except Exception:  # noqa: BLE001 — one bad dir must not abort the sweep
                counts.errors += 1
                _logger.warning(
                    "native-bridge-gc: error handling %s/%s",
                    family.name,
                    state_dir.name,
                    exc_info=True,
                )
    if any(v for k, v in counts.as_dict().items() if k not in {"scanned"}):
        _logger.info("native-bridge-gc: sweep complete %s", counts.as_dict())
    return counts


async def _classify_dir(
    *,
    family: BridgeFamily,
    state_dir: Path,
    resolve_conversation: Callable[[str], Awaitable[ConversationState]],
    active_turns_now: frozenset[str],
    cutoff: int,
) -> tuple[DirDisposition, str | None]:
    """Gather the liveness/conversation facts for one dir and classify it.

    Runs the full liveness gate (process/registry/pane probe, ``_active_turns``
    membership, DB binding) plus the conversation resolve, then delegates to the
    pure :func:`classify_bridge_dir`. Called both for the initial pass and — with
    a fresh ``active_turns`` snapshot — for the pre-removal atomic recheck, so the
    two decisions use identical logic.

    :returns: ``(disposition, session_id)`` — ``session_id`` is ``None`` for an
        unknown-format dir.
    """
    process_live = await asyncio.to_thread(family.process_live, state_dir)
    session_id = await asyncio.to_thread(session_id_from_state_dir, state_dir)
    state_readable = session_id is not None
    in_active_turns = state_readable and session_id in active_turns_now

    conv = ConversationState(exists=False)
    if state_readable and not process_live and not in_active_turns:
        # Only pay for the server round-trip when a cheaper local veto has not
        # already spared the dir.
        conv = await resolve_conversation(session_id)  # type: ignore[arg-type]

    disposition = classify_bridge_dir(
        state_readable=state_readable,
        conversation_exists=conv.exists,
        conversation_archived=conv.archived,
        conversation_updated_at=conv.updated_at,
        in_active_turns=in_active_turns,
        bound_to_runner=conv.bound,
        process_live=process_live,
        archived_ttl_cutoff=cutoff,
    )
    return disposition, session_id


async def _sweep_one_dir(
    *,
    family: BridgeFamily,
    state_dir: Path,
    config: GcConfig,
    resolve_conversation: Callable[[str], Awaitable[ConversationState]],
    active_turns: Callable[[], frozenset[str]],
    cutoff: int,
    now: int,
    remove_dir: _RemoveDir,
    counts: SweepCounts,
) -> None:
    """Classify and (conditionally) remove one dir; update *counts* in place."""
    disposition, session_id = await _classify_dir(
        family=family,
        state_dir=state_dir,
        resolve_conversation=resolve_conversation,
        active_turns_now=active_turns(),
        cutoff=cutoff,
    )

    label = f"{family.name}/{state_dir.name}"
    if disposition is DirDisposition.LIVE:
        counts.skipped_live += 1
        _logger.debug("native-bridge-gc: skipped %s (live)", label)
        return
    if disposition is DirDisposition.ACTIVE_SESSION:
        counts.skipped_active_session += 1
        return
    if disposition is DirDisposition.ARCHIVED_RECENT:
        counts.skipped_archived_recent += 1
        return

    if not is_removable(
        disposition,
        remove_archived_expired=config.remove_archived_expired,
        remove_unknown=config.remove_unknown,
    ):
        if disposition is DirDisposition.UNKNOWN:
            counts.skipped_unknown += 1
        else:
            # ARCHIVED_EXPIRED retained because remove_archived is off — expired,
            # not recent; a distinct bucket so operators can see what opting in
            # would reclaim.
            counts.skipped_archived_disabled += 1
        return

    # Unknown-format removal is additionally age-guarded so a dir whose
    # state.json has not been written yet (a session mid-launch) is spared.
    if disposition is DirDisposition.UNKNOWN:
        mtime = await asyncio.to_thread(_dir_mtime, state_dir)
        if mtime is None or (now - mtime) < config.archived_ttl_seconds:
            counts.skipped_unknown_too_new += 1
            _logger.debug("native-bridge-gc: skipped %s (unknown, too new)", label)
            return

    if config.dry_run:
        counts.would_remove += 1
        _logger.info("native-bridge-gc: DRY-RUN would remove %s (%s)", label, disposition.value)
        return

    # Phase-two async recheck: recompute the FULL classification (fresh liveness
    # + active-turns + conversation resolve) and abort if it changed at all — a
    # turn that started, a runner that bound, a process that came up, or a
    # conversation that came back all change the disposition and spare the dir.
    # This catches the DB-binding racer (resolve is async, so it must stay here,
    # just outside the removal worker).
    recheck_disposition, _ = await _classify_dir(
        family=family,
        state_dir=state_dir,
        resolve_conversation=resolve_conversation,
        active_turns_now=active_turns(),
        cutoff=cutoff,
    )
    if recheck_disposition is not disposition:
        counts.skipped_live_raced += 1
        _logger.info(
            "native-bridge-gc: skipped %s (live/raced: %s -> %s)",
            label,
            disposition.value,
            recheck_disposition.value,
        )
        return

    # Final LOCAL liveness veto + rmtree run as ONE uninterrupted worker step: no
    # ``await`` (no event-loop yield) between the last check and the delete. The
    # async recheck above cannot close the window before ``rmtree`` — a
    # process/registry owner or a turn could still go live in it — so the vendor
    # process/pane probe AND a fresh active_turns snapshot are re-read inside the
    # worker, immediately before shutil.rmtree. (The DB racer is not a concern
    # here: an orphan is a 404 and cannot spawn a turn/binding without a live
    # local process, which this re-checks.)
    removed = await asyncio.to_thread(
        _guarded_remove,
        remove_dir=remove_dir,
        state_dir=state_dir,
        process_live=family.process_live,
        active_turns=active_turns,
        session_id=session_id,
    )
    if not removed:
        counts.skipped_live_raced += 1
        _logger.info("native-bridge-gc: skipped %s (live/raced before rmtree)", label)
        return
    if disposition is DirDisposition.ORPHAN:
        counts.removed_orphan += 1
    elif disposition is DirDisposition.ARCHIVED_EXPIRED:
        counts.removed_archived_expired += 1
    else:
        counts.removed_unknown += 1
    _logger.info("native-bridge-gc: removed %s (%s)", label, disposition.value)


def _guarded_remove(
    *,
    remove_dir: _RemoveDir,
    state_dir: Path,
    process_live: Callable[[Path], bool],
    active_turns: Callable[[], frozenset[str]],
    session_id: str | None,
) -> bool:
    """Re-check LOCAL liveness and ``rmtree`` in one uninterrupted worker step.

    Runs inside the :func:`asyncio.to_thread` removal worker, so there is no
    ``await`` — hence no event-loop yield — between the final liveness veto and
    the delete. Only the LOCAL, synchronous signals are re-read here: the vendor
    process/pane probe (codex socket OR live registry owner / agy tmux pane) and
    a fresh ``active_turns`` snapshot. If either is now positive the delete is
    aborted. (Both callables are single C-level reads that do not release the GIL
    mid-call, so the snapshot is consistent even though the event loop may hold
    the real state.)

    :returns: ``True`` if the dir was removed, ``False`` if a local liveness
        signal aborted the delete (the caller counts it as ``skipped_live_raced``).
    """
    if process_live(state_dir):
        return False
    if session_id is not None and session_id in active_turns():
        return False
    _remove(remove_dir, state_dir)
    return True


def _remove(remove_dir: _RemoveDir, state_dir: Path) -> None:
    """Best-effort dir removal; an already-gone dir is a no-op."""
    with contextlib.suppress(FileNotFoundError):
        remove_dir(state_dir)


# ── Periodic loop ────────────────────────────────────────────────────────────

_DEFAULT_INTERVAL_S = 3600.0


class NativeBridgeGarbageCollector:
    """Background task that runs :func:`sweep_native_bridge_dirs` on a schedule.

    Mirrors :class:`omnicraft.terminals.pane_reaper.NativePaneReaper`: a
    cancellable loop that runs a sweep at startup, then every *interval_s*,
    swallowing per-tick errors so a single failure never kills the loop.

    :param sweep: ``async`` callable performing one sweep (the runner builds this
        as a closure over its server client, active-turns map, and config).
    :param interval_s: Seconds between sweeps.
    :param run_on_start: Whether to run one sweep immediately on :meth:`start`
        (the startup sweep) before entering the interval loop.
    """

    def __init__(
        self,
        *,
        sweep: Callable[[], Awaitable[None]],
        interval_s: float = _DEFAULT_INTERVAL_S,
        run_on_start: bool = True,
    ) -> None:
        self._sweep = sweep
        self._interval_s = interval_s
        self._run_on_start = run_on_start
        self._task: asyncio.Task[None] | None = None
        self._started = False

    async def start(self) -> None:
        """Spawn the sweep loop (idempotent)."""
        if self._started:
            return
        self._started = True
        self._task = asyncio.create_task(self._loop(), name="native-bridge-gc")
        _logger.info("native-bridge-gc started (interval=%ss)", self._interval_s)

    async def shutdown(self) -> None:
        """Cancel the sweep loop."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._started = False

    async def _loop(self) -> None:
        if self._run_on_start:
            await self._run_once()
        while True:
            try:
                await asyncio.sleep(self._interval_s)
            except asyncio.CancelledError:
                return
            await self._run_once()

    async def _run_once(self) -> None:
        try:
            await self._sweep()
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("native-bridge-gc: sweep failed")
