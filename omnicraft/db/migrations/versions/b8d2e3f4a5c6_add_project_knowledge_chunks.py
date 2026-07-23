"""add project_knowledge_chunks table

Revision ID: b8d2e3f4a5c6
Revises: a7c1d2e3f4b5
Create Date: 2026-07-23 00:30:00.000000

Adds ``project_knowledge_chunks``: the retrievable text of a project's
documents, split into pieces. Search matches against ``text`` and ranks by
token hits — deliberately not FTS5, so the same query path works on SQLite and
PostgreSQL alike.

Brand-new table; deployments whose database lacks it simply have no searchable
knowledge, and the documents themselves remain stored and downloadable.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d2e3f4a5c6"
down_revision: str | None = "a7c1d2e3f4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``project_knowledge_chunks`` table."""
    op.create_table(
        "project_knowledge_chunks",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("project", sa.String(256), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_project_knowledge_chunks_document_id",
        "project_knowledge_chunks",
        ["document_id"],
    )
    op.create_index(
        "ix_project_knowledge_chunks_project",
        "project_knowledge_chunks",
        ["project"],
    )


def downgrade() -> None:
    """Drop the ``project_knowledge_chunks`` table."""
    op.drop_index("ix_project_knowledge_chunks_project", table_name="project_knowledge_chunks")
    op.drop_index("ix_project_knowledge_chunks_document_id", table_name="project_knowledge_chunks")
    op.drop_table("project_knowledge_chunks")
