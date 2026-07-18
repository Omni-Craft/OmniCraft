"""Guard: the OSS Docker entrypoint wires a PolicyStore into the app.

``build_app`` must construct a ``SqlAlchemyPolicyStore`` and thread it into
both ``init_runtime`` (so runtime code sees session/default policies) and
``create_app`` (so the policy CRUD routes have a backing store). Omitting it
leaves the runtime's policy store ``None``, which silently degrades to
"spec-declared policies only" — a fail-open the CLI and Databricks
entrypoints already avoid by wiring the store. This test asserts the same
wiring here without needing a live database or a container: the stores
construct against a temp-file SQLite URI (hermetic, no external DB, no
connection at build time).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnicraft.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore


@pytest.fixture
def _resolved_sqlite_config(tmp_path: Path):
    """A ``_ResolvedConfig`` backed by SQLite so store construction needs no
    live DB, plus auth disabled so ``build_app`` skips accounts-secret work."""
    from deploy.docker.entrypoint import _ResolvedConfig

    return _ResolvedConfig(
        cfg={},
        database_url="sqlite:///" + str(tmp_path / "policy.db"),
        artifact_dir=tmp_path / "artifacts",
        artifact_store_uri=None,
        host="0.0.0.0",
        port=8000,
    )


def test_build_app_wires_policy_store(
    _resolved_sqlite_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNICRAFT_AUTH_ENABLED", "0")

    import omnicraft.runtime as runtime
    import omnicraft.server.app as server_app
    from deploy.docker import entrypoint

    captured: dict[str, dict] = {}

    def _fake_init(**kwargs: object) -> None:
        captured["init_runtime"] = kwargs

    def _fake_create_app(**kwargs: object) -> object:
        captured["create_app"] = kwargs
        return object()

    # build_app imports these by name at call time, so patching the source
    # module attribute intercepts them without mutating process-global runtime
    # state or building the real FastAPI app.
    monkeypatch.setattr(runtime, "init", _fake_init)
    monkeypatch.setattr(server_app, "create_app", _fake_create_app)

    entrypoint.build_app(_resolved_sqlite_config)

    # The store reaches the runtime (else policies degrade to spec-only)...
    init_store = captured["init_runtime"].get("policy_store")
    assert isinstance(init_store, SqlAlchemyPolicyStore)
    # ...and the CRUD routes (else session-policy management has no backing store).
    create_store = captured["create_app"].get("policy_store")
    assert isinstance(create_store, SqlAlchemyPolicyStore)
    # Both destinations share the one store instance build_app constructs.
    assert init_store is create_store
