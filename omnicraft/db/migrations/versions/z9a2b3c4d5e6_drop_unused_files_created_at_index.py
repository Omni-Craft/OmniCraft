"""Drop the unused ix_files_created_at index.

Revision ID: z9a2b3c4d5e6
Revises: z8a2b3c4d5e6
Create Date: 2026-08-02 00:00:00.000000

``ix_files_created_at`` (workspace_id, created_at, id) só servia uma listagem
sem escopo de sessão — ``WHERE workspace_id ORDER BY created_at, id`` — e nada
emitia essa consulta. Os dois caminhos de leitura de arquivos de uma sessão (a
ferramenta ``list_files`` do agente e a rota de recursos da sessão) filtram por
``session_id`` e são servidos pelo ``ix_files_session_id_created_at``; os
arquivos globais (``session_id IS NULL``) aparecem pela cláusula ``OR`` do
``include_unscoped``, que também roda sobre esse índice.

O ``FileStore.list`` passou a exigir ``session_id`` justamente para que a
listagem sem escopo não volte a existir sem que alguém repare — sem isso, um
chamador futuro reintroduziria a varredura em silêncio.

Só índice: nenhuma coluna muda, então não há rebuild de tabela.
"""

from __future__ import annotations

from alembic import op

revision: str = "z9a2b3c4d5e6"
down_revision: str | None = "z8a2b3c4d5e6"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Remove o índice que nenhuma consulta usava."""
    op.drop_index("ix_files_created_at", table_name="files")


def downgrade() -> None:
    """Recria o índice."""
    op.create_index(
        "ix_files_created_at", "files", ["workspace_id", "created_at", "id"], unique=False
    )
