"""add archived_reason column to conversations

Revision ID: z6a2b3c4d5e6
Revises: z5a2b3c4d5e6
Create Date: 2026-07-16 00:00:00.000000

Adds ``conversations.archived_reason``: provenance for the current
``archived`` state. ``archived`` alone can't distinguish a session a human
archived on purpose (``PATCH /v1/sessions/{id}``) from one the
unbound-session-TTL sweep archived automatically — without that
distinction, a first host bind on either could equally look like a
legitimate create-then-bind completion. This column lets ``set_host_id``
tell the two apart before auto-unarchiving on a first bind: NULL for a
manual archive or a non-archived row; set to
``omnicraft.stores.conversation_store.UNBOUND_SWEEP_ARCHIVE_REASON`` only
by the sweep's own atomic archive write.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "z6a2b3c4d5e6"
down_revision: str | None = "z5a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``archived_reason`` column to ``conversations``.

    No ``server_default``: existing rows (all currently either
    non-archived or manually archived) correctly backfill to NULL.
    Batch mode is used for SQLite compatibility, consistent with the
    other conversations migrations.
    """
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "archived_reason",
                sa.String(length=32),
                nullable=True,
            )
        )


def downgrade() -> None:
    """Drop the ``archived_reason`` column."""
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("archived_reason")
