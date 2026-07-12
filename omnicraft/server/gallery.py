"""Agent gallery — browse and install the bundled example agents.

The repo ships ready-made agents under ``examples/`` (fucho, lilo, remy,
scribe, sentinel). This module lists their metadata for a browse view and
installs one into the agent store (idempotent, by name) so it shows up in the
New Session picker — the same registration ``omnicraft run examples/<x>/`` and
the ``--agent`` startup flag perform.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import yaml


def examples_dir() -> Path:
    """Resolve the directory holding the example agents.

    Prefers the repo's ``examples/`` (present in an editable/dev install),
    falling back to the packaged ``omnicraft/resources/examples``.
    """
    import omnicraft

    pkg = Path(omnicraft.__file__).resolve().parent
    repo_examples = pkg.parent / "examples"
    if repo_examples.is_dir():
        return repo_examples
    return pkg / "resources" / "examples"


def _subagent_names(entry: Path, tools: Any) -> list[str]:
    """Sub-agent names: inline ``tools`` of type ``agent`` plus ``agents/<name>/`` dirs."""
    names: list[str] = []
    if isinstance(tools, dict):
        names.extend(
            k for k, v in tools.items() if isinstance(v, dict) and v.get("type") == "agent"
        )
    agents_dir = entry / "agents"
    if agents_dir.is_dir():
        names.extend(sorted(p.name for p in agents_dir.iterdir() if p.is_dir()))
    return names


def list_gallery_agents(agent_store: Any) -> list[dict[str, Any]]:
    """List installable example agents with light metadata (read from config.yaml).

    :param agent_store: Used to flag which examples are already installed.
    :returns: One dict per example: id (dir name), name, description, harness,
        subagents, skills, installed.
    """
    root = examples_dir()
    if not root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir()):
        config = entry / "config.yaml"
        if not entry.is_dir() or not config.is_file():
            continue
        try:
            cfg = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(cfg, dict):
            continue
        name = cfg.get("name")
        if not isinstance(name, str) or not name:
            continue
        executor = cfg.get("executor") if isinstance(cfg.get("executor"), dict) else {}
        harness = executor.get("harness") or executor.get("type")
        skills_dir = entry / "skills"
        skills = (
            sorted(p.name for p in skills_dir.iterdir() if p.is_dir())
            if skills_dir.is_dir()
            else []
        )
        subagent_names = _subagent_names(entry, cfg.get("tools"))
        prompt = cfg.get("prompt")
        prompt_preview = (
            " ".join(str(prompt).split())[:280] if isinstance(prompt, str) and prompt else ""
        )
        installed = False
        try:
            installed = agent_store.get_by_name(name) is not None
        except Exception:  # noqa: BLE001 — best-effort flag, never block the listing
            installed = False
        items.append(
            {
                "id": entry.name,
                "name": name,
                "description": (cfg.get("description") or "").strip(),
                "harness": harness,
                "subagents": len(subagent_names),
                "subagent_names": subagent_names,
                "skills": skills,
                "prompt_preview": prompt_preview,
                "installed": installed,
            }
        )
    return items


def install_gallery_agent(
    example_id: str,
    agent_store: Any,
    artifact_store: Any,
    agent_cache: Any,
) -> dict[str, Any] | None:
    """Materialize an example bundle and register it (idempotent by name).

    Mirrors ``omnicraft.cli._preregister_agent`` — the same path the CLI uses
    for ``--agent`` — so an installed gallery agent behaves identically to one
    added at startup.

    :param example_id: The example directory name (e.g. ``"remy"``).
    :returns: ``{"agent_id", "name"}`` or ``None`` if the example is missing /
        has no name.
    """
    from omnicraft.db.utils import generate_agent_id
    from omnicraft.spec import load, materialize_bundle

    # Guard against path traversal: only a direct child of examples/ is valid.
    if "/" in example_id or "\\" in example_id or example_id in ("", ".", ".."):
        return None
    source = examples_dir() / example_id
    if not (source / "config.yaml").is_file():
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        bundle_dir = materialize_bundle(source, Path(tmpdir) / "bundle")
        buf = io.BytesIO()
        with (
            gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
            tarfile.open(fileobj=gz, mode="w") as tar,
        ):
            tar.add(str(bundle_dir), arcname=".")
        bundle_bytes = buf.getvalue()
        # Don't expand ${ENV} here — registration only needs the name, and an
        # example may reference vars (e.g. an API key) the user sets later at
        # run time. The bundle keeps the placeholders; the runner expands them.
        spec = load(bundle_dir, expand_env=False)

    if spec.name is None:
        return None

    bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()
    existing = agent_store.get_by_name(spec.name)
    if existing is not None:
        new_loc = f"{existing.id}/{bundle_hash}"
        if existing.bundle_location != new_loc:
            # Order matters: artifact first, then cache swap, then the store
            # pointer — so a failure never leaves the store pointing at a
            # bundle the cache never loaded. expand_env=False mirrors load()
            # above: registration keeps placeholders; the runner expands them.
            artifact_store.put(new_loc, bundle_bytes)
            agent_cache.replace(existing.id, new_loc, bundle_bytes, expand_env=False)
            agent_store.update(existing.id, bundle_location=new_loc)
        return {"agent_id": existing.id, "name": spec.name}

    agent_id = generate_agent_id()
    loc = f"{agent_id}/{bundle_hash}"
    artifact_store.put(loc, bundle_bytes)
    agent_store.create(
        agent_id=agent_id,
        name=spec.name,
        bundle_location=loc,
        description=spec.description,
    )
    return {"agent_id": agent_id, "name": spec.name}
