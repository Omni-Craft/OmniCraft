"""Server-side YAML config for the non-CLI entrypoints.

The ``omnicraft server`` CLI already takes ``-c/--config`` and reads a
YAML file (see ``omnicraft/cli.py``). The hosted entrypoints —
``deploy/docker/entrypoint.py`` and ``deploy/databricks/src/app.py`` —
don't go through that CLI; they build the app directly from env vars.
This module gives those entrypoints the *same* config-file experience a
laptop gets from ``-c``, so a deployment can keep most of its settings
(admins, allowed domains, policy modules, artifact location, host/port,
database URI) in one file on the persistent volume instead of a pile of
env vars.

**Secrets stay in the environment, not this file.** ``DATABASE_URL``,
the session cookie secret, and the OIDC client secret are injected by
compose / ``bootstrap.sh`` / the platform — keeping them out of a
mounted YAML is deliberate (12-factor; the file is operator-editable
and often world-readable on the box). This config holds non-secret
*settings* only.

Resolution order for the config path:

1. ``OMNICRAFT_CONFIG`` env var, if set (explicit path).
2. ``<data_dir>/config.yaml`` if it exists — ``<data_dir>`` is the same
   directory the admin list / credentials use (``/data`` in the Docker
   stack, ``~/.omnicraft`` on a laptop; see
   :func:`omnicraft.server.admin_list.resolve_data_dir`).
3. Otherwise ``None`` — no file, pure env config (back-compat: existing
   env-only deploys keep working unchanged).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from omnicraft.server.admin_list import resolve_data_dir

logger = logging.getLogger(__name__)


def resolve_config_path() -> Path | None:
    """Resolve the server config file path, or ``None`` if there is none.

    :returns: ``OMNICRAFT_CONFIG`` if set; else ``<data_dir>/config.yaml``
        when that file exists; else ``None``.
    """
    explicit = os.environ.get("OMNICRAFT_CONFIG", "").strip()
    if explicit:
        return Path(explicit)
    default = resolve_data_dir() / "config.yaml"
    return default if default.is_file() else None


def load_server_config() -> dict[str, Any]:
    """Load the resolved server config file into a dict.

    :returns: The parsed mapping, or an empty dict when no config file is
        resolved. A present-but-unreadable / malformed file logs a
        warning and returns ``{}`` rather than crashing startup — the
        entrypoint then falls back to env + defaults.
    """
    path = resolve_config_path()
    if path is None:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("server config %s unreadable/invalid: %s — falling back to env", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("server config %s is not a mapping — ignoring", path)
        return {}
    logger.info("loaded server config from %s", path)
    return data


def config_str_list(value: Any) -> list[str]:
    """Coerce a config value into a list of non-empty strings.

    Accepts a YAML list (``["a", "b"]``) or a single scalar (``"a"``);
    anything else yields an empty list. Used for ``admins`` /
    ``allowed_domains`` so a one-entry value doesn't have to be a list.

    :param value: The raw config value, e.g. ``["alice@example.com"]``.
    :returns: A list of stripped, non-empty strings.
    """
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [str(item).strip() for item in items if str(item).strip()]


def _config_positive_int(key: str, default: int) -> int:
    """Read a positive-int setting from the server config, else *default*.

    A missing, non-numeric, or non-positive value falls back to *default*
    rather than crashing — the config file is operator-editable and a typo
    should degrade to the safe built-in limit, not take the server down.

    :param key: Top-level config key, e.g. ``"copy_max_files"``.
    :param default: Value used when the key is absent or invalid.
    :returns: The configured positive int, or *default*.
    """
    raw = load_server_config().get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("server config %s=%r is not an int — using default %d", key, raw, default)
        return default
    if value <= 0:
        logger.warning(
            "server config %s=%d is not positive — using default %d", key, value, default
        )
        return default
    return value


def _config_bool(key: str, default: bool) -> bool:
    """Read a boolean setting from the server config, else *default*.

    Accepts a native YAML bool, ``0``/``1``, or a truthy/falsey string
    (``true``/``yes``/``on`` vs ``false``/``no``/``off``, case-insensitive). Any
    other value logs a warning and falls back to *default* — an operator typo
    should degrade to the safe built-in, not crash the entrypoint.

    :param key: Top-level config key, e.g. ``"native_bridge_gc_enabled"``.
    :param default: Value used when the key is absent or invalid.
    :returns: The configured bool, or *default*.
    """
    raw = load_server_config().get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    # Strict, like _config_positive_int: only 0/1 (and the documented string
    # forms) are valid. A stray 2/-1 is malformed and must degrade to the knob's
    # default, never silently flip an opt-in removal flag on.
    if isinstance(raw, int):
        if raw == 1:
            return True
        if raw == 0:
            return False
    if isinstance(raw, str):
        norm = raw.strip().lower()
        if norm in {"true", "1", "yes", "on"}:
            return True
        if norm in {"false", "0", "no", "off"}:
            return False
    logger.warning("server config %s=%r is not a bool — using default %s", key, raw, default)
    return default


def copy_file_count_limit() -> int:
    """Max number of files a single copy-at-spawn request may copy.

    Config key ``copy_max_files``; defaults to
    :data:`omnicraft.runtime.content_resolver.MAX_COPY_FILES`.
    """
    from omnicraft.runtime.content_resolver import MAX_COPY_FILES

    return _config_positive_int("copy_max_files", MAX_COPY_FILES)


def copy_total_bytes_limit() -> int:
    """Max summed byte size a single copy-at-spawn request may copy.

    Config key ``copy_max_total_bytes``; defaults to
    :data:`omnicraft.runtime.content_resolver.MAX_COPY_TOTAL_BYTES`.
    """
    from omnicraft.runtime.content_resolver import MAX_COPY_TOTAL_BYTES

    return _config_positive_int("copy_max_total_bytes", MAX_COPY_TOTAL_BYTES)


# Default TTL for the unbound-session sweep: generous because a
# legitimate create-then-bind (POST /v1/sessions without host_id, later
# bound via PATCH) can sit idle for a while before the caller finishes
# picking a host — see designs around late host-bind in
# omnicraft/server/routes/sessions.py's update_session.
_DEFAULT_UNBOUND_SESSION_TTL_HOURS = 24


def unbound_session_ttl_hours() -> int:
    """Hours an unbound session (no ``host_id``, no ``runner_id``) may sit
    with no events before the background sweep archives it.

    Config key ``unbound_session_ttl_hours``; defaults to
    :data:`_DEFAULT_UNBOUND_SESSION_TTL_HOURS`. Archival is reversible —
    the session shows up under ``include_archived=true`` and can be
    unarchived via ``PATCH /v1/sessions/{id}`` like any other archived
    session.
    """
    return _config_positive_int("unbound_session_ttl_hours", _DEFAULT_UNBOUND_SESSION_TTL_HOURS)


# ── Native-harness bridge-dir garbage collector ──────────────────────────────
#
# The runner-side sweep (omnicraft.native_bridge_gc) reclaims stale per-session
# state dirs under ~/.omnicraft/{codex,antigravity}-native/<hash> left behind by
# crashed / never-explicitly-deleted sessions. Every removal is gated by an
# absolute liveness veto; these knobs only widen or narrow WHAT is eligible and
# whether the sweep actually deletes vs logs.

# Generous default: an archived session can legitimately be resumed for a while.
# A week of no activity (updated_at, bumped on every item append) before its dir
# is even *eligible* for the opt-in archived-past-TTL removal.
_DEFAULT_NATIVE_BRIDGE_GC_ARCHIVED_TTL_HOURS = 168
# Sweep cadence: hourly, matching the unbound-session sweep loop.
_DEFAULT_NATIVE_BRIDGE_GC_INTERVAL_SECONDS = 3600


def native_bridge_gc_enabled() -> bool:
    """Whether the runner-side native-bridge GC sweep runs at all.

    Config key ``native_bridge_gc_enabled``; defaults to ``True``. Disabling it
    stops both the startup and the periodic sweep (no dir is touched).
    """
    return _config_bool("native_bridge_gc_enabled", True)


def native_bridge_gc_dry_run() -> bool:
    """Whether the GC sweep only logs what it *would* remove, deleting nothing.

    Config key ``native_bridge_gc_dry_run``; defaults to ``False`` (real
    removal of orphans whose conversation no longer exists). Live dirs are
    logged as skipped in either mode.
    """
    return _config_bool("native_bridge_gc_dry_run", False)


def native_bridge_gc_archived_ttl_hours() -> int:
    """Hours an archived conversation may sit inactive before its dir is
    TTL-eligible (and the min age before an unknown-format dir may be removed).

    Config key ``native_bridge_gc_archived_ttl_hours``; defaults to
    :data:`_DEFAULT_NATIVE_BRIDGE_GC_ARCHIVED_TTL_HOURS`.
    """
    return _config_positive_int(
        "native_bridge_gc_archived_ttl_hours", _DEFAULT_NATIVE_BRIDGE_GC_ARCHIVED_TTL_HOURS
    )


def native_bridge_gc_remove_archived() -> bool:
    """Opt-in: also remove dirs of archived conversations past the TTL.

    Config key ``native_bridge_gc_remove_archived``; defaults to ``False``. An
    archived session is still resumable, so its dir is kept unless an operator
    opts in.
    """
    return _config_bool("native_bridge_gc_remove_archived", False)


def native_bridge_gc_remove_unknown() -> bool:
    """Opt-in: also remove unknown-format dirs (missing/unreadable state.json).

    Config key ``native_bridge_gc_remove_unknown``; defaults to ``False``. Such
    a dir cannot be resolved to a conversation, so it is only removed when an
    operator opts in — and even then only past the archived TTL age guard, so a
    session mid-launch (state.json not yet written) is never nuked.
    """
    return _config_bool("native_bridge_gc_remove_unknown", False)


def native_bridge_gc_interval_seconds() -> int:
    """Seconds between periodic native-bridge GC sweeps.

    Config key ``native_bridge_gc_interval_seconds``; defaults to
    :data:`_DEFAULT_NATIVE_BRIDGE_GC_INTERVAL_SECONDS`.
    """
    return _config_positive_int(
        "native_bridge_gc_interval_seconds", _DEFAULT_NATIVE_BRIDGE_GC_INTERVAL_SECONDS
    )
