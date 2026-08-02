"""Consolidate the policies secondary indexes.

Revision ID: z7a2b3c4d5e6
Revises: b8d2e3f4a5c6
Create Date: 2026-07-31 00:00:00.000000

A tabela ``policies`` carregava três estruturas secundárias que não pagavam o
próprio custo de escrita:

- ``ix_policies_created_at`` (workspace_id, created_at, id) não casa com
  consulta nenhuma. As duas listagens filtram por escopo ou por sessão e só
  ORDENAM por ``created_at``; nenhuma busca por faixa de data.
- ``ix_policies_session_id`` (workspace_id, session_id, id) é redundante: a
  constraint única ``uq_policies_session_id_name_cksum`` já começa por
  (workspace_id, session_id), e esse prefixo serve o ``list_for_session``.
- Faltava índice para ``list_defaults`` (WHERE workspace_id + scope), que por
  isso varria todas as linhas de sessão para achar as poucas globais.

Entra um ``ix_policies_scope`` (workspace_id, scope, id) no lugar dos dois —
saldo de um índice a menos e a listagem de defaults deixa de varrer.

A constraint única **fica**. Ao contrário do upstream, que moveu a checagem
para a aplicação, o ``create`` deste fork depende dela para a unicidade de
nome dentro da sessão (o próprio docstring promete ``IntegrityError`` na
colisão); dropá-la abriria duplicata silenciosa.

Só índices: nenhuma coluna muda, então não há rebuild de tabela em lote —
``DROP INDEX`` / ``CREATE INDEX`` são nativos em todo dialeto.
"""

from __future__ import annotations

from alembic import op

revision: str = "z7a2b3c4d5e6"
down_revision: str | None = "b8d2e3f4a5c6"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Troca os dois índices sem uso por um que serve o list_defaults."""
    op.drop_index("ix_policies_created_at", table_name="policies")
    op.drop_index("ix_policies_session_id", table_name="policies")
    op.create_index("ix_policies_scope", "policies", ["workspace_id", "scope", "id"], unique=False)


def downgrade() -> None:
    """Restaura os dois índices anteriores."""
    op.drop_index("ix_policies_scope", table_name="policies")
    op.create_index(
        "ix_policies_session_id", "policies", ["workspace_id", "session_id", "id"], unique=False
    )
    op.create_index(
        "ix_policies_created_at", "policies", ["workspace_id", "created_at", "id"], unique=False
    )
