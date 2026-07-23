"""add project_documents table

Revision ID: a7c1d2e3f4b5
Revises: z6a2b3c4d5e6
Create Date: 2026-07-23 00:00:00.000000

Adds the ``project_documents`` table: the metadata half of a project's
knowledge base. One row per uploaded document, keyed by project NAME because
projects are implicit — a project exists while a conversation carries the
``omni_project`` label, so there is no project row to reference.

Brand-new table, so deployments whose database lacks it are unaffected: every
read and write happens on the project-knowledge routes, which simply return an
empty base when nothing was ever uploaded.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c1d2e3f4b5"
down_revision: str | None = "z6a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``project_documents`` table."""
    op.create_table(
        "project_documents",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("project", sa.String(256), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(256), nullable=True),
        sa.Column("text_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    # Listing a project's base is the hot path.
    op.create_index(
        "ix_project_documents_project",
        "project_documents",
        ["project"],
    )


def downgrade() -> None:
    """Drop the ``project_documents`` table."""
    op.drop_index("ix_project_documents_project", table_name="project_documents")
    op.drop_table("project_documents")
