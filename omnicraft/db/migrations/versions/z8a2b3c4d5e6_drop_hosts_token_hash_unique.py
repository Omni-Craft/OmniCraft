"""Drop the hosts token_hash unique constraint.

Revision ID: z8a2b3c4d5e6
Revises: z7a2b3c4d5e6
Create Date: 2026-07-31 00:00:00.000000

O caminho de auth do túnel de host gerenciado não precisa mais de índice em
``token_hash``. O endpoint é ``/hosts/{host_id}/tunnel``, então o par que
conecta já nomeia o host que afirma ser; o ``resolve_launch_token`` passou a
buscar a linha pela chave primária ``(workspace_id, host_id)`` e comparar o
digest guardado com o do token apresentado via ``hmac.compare_digest`` —
tempo constante, mantendo a propriedade de não haver oráculo de temporização.

A unicidade nunca foi estrutural: token de lançamento é um segredo de 256
bits (``secrets.token_urlsafe(32)``), cujos digests não colidem na prática, e
nada mais depende dela agora que a busca é pela PK.

Diferente do índice de ``policies``, aqui a constraint pode cair mesmo: ela
não protegia nenhuma regra de negócio, só dava caminho de busca.

SQLite não sabe dropar constraint no lugar, então esta é a única migração da
série que precisa de ``batch_alter_table`` (recria a tabela). As demais eram
só índice.
"""

from __future__ import annotations

from alembic import op

revision: str = "z8a2b3c4d5e6"
down_revision: str | None = "z7a2b3c4d5e6"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Remove a unicidade de (workspace_id, token_hash)."""
    with op.batch_alter_table("hosts") as batch:
        batch.drop_constraint("uq_hosts_token_hash", type_="unique")


def downgrade() -> None:
    """Restaura a unicidade de (workspace_id, token_hash)."""
    with op.batch_alter_table("hosts") as batch:
        batch.create_unique_constraint("uq_hosts_token_hash", ["workspace_id", "token_hash"])
