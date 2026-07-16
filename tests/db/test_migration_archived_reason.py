"""Tests for the ``conversations.archived_reason`` column and its migration.

Provenance for the current ``archived`` state — ``archived`` alone can't
tell a manual archive (``PATCH /v1/sessions/{id}``) apart from one the
unbound-session-TTL sweep set automatically. ``set_host_id`` reads this
column to gate auto-unarchive on a first host bind to sessions the sweep
itself archived, never one a human archived on purpose. The column is
nullable with no ``server_default`` — existing rows backfill to NULL
(no marker) when the migration applies to a populated database.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from omnicraft.db.utils import clear_engine_cache, get_or_create_engine


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """
    Fresh SQLite DB with the full alembic chain applied; cleaned up after.

    :param tmp_path: Pytest-managed temp directory for the SQLite file.
    :returns: Engine pointed at the migrated database.
    """
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_archived_reason_column_present_and_nullable(db_engine: Engine) -> None:
    """
    The migration creates ``conversations.archived_reason`` as a
    nullable string column.

    A failure on presence means the migration didn't apply — the ORM
    mapping would then crash on every conversation read. Nullable
    matters because most rows (never archived, or archived manually)
    carry no provenance marker at all.
    """
    cols = sa.inspect(db_engine).get_columns("conversations")
    reason_cols = [c for c in cols if c["name"] == "archived_reason"]
    assert len(reason_cols) == 1, (
        f"Expected exactly one 'archived_reason' column on conversations, "
        f"got {len(reason_cols)}. If 0, the migration didn't apply."
    )
    assert reason_cols[0]["nullable"], (
        "conversations.archived_reason must be nullable — most rows carry "
        "no sweep-provenance marker at all."
    )


def test_archived_reason_defaults_null_on_insert(db_engine: Engine) -> None:
    """
    An insert that omits ``archived_reason`` lands as NULL.

    No ``server_default`` is set (unlike ``archived``): a pre-existing
    row backfilled by this migration, or any ordinary insert that
    doesn't go through the sweep's write path, must never accidentally
    carry the sweep's provenance marker.
    """
    with db_engine.connect() as conn:
        # root_conversation_id is a NOT NULL self-FK; a top-level row's
        # root is its own id, so bind :id for both.
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, kind, root_conversation_id) "
                "VALUES (:id, :ts, :ts, 1, :id)"
            ),
            {"id": "conv_reason_default", "ts": 1700000000},
        )
        conn.commit()
        value = conn.execute(
            sa.text("SELECT archived_reason FROM conversations WHERE id = :id"),
            {"id": "conv_reason_default"},
        ).scalar_one_or_none()
        assert value is None, f"Expected archived_reason to default to NULL; got {value!r}."
