"""Project knowledge tool — let an agent consult its project's document shelf.

A session filed under a project can reach the documents uploaded to that
project. The tool resolves the project from the session's ``omni_project``
label, so the agent never picks the project (and so can never read another
one's shelf), and returns the matching passages with the file they came from —
an answer that cannot cite its source is worse than no answer.

Sessions outside any project get a clear message rather than an empty result,
because "no project" and "nothing found" are different problems for the caller.
"""

from __future__ import annotations

import json
from typing import Any

from omnicraft.tools.base import Tool, ToolContext

#: Conversation label that files a session under a project. Mirrors
#: ``PROJECT_LABEL_KEY`` in the conversation store.
_PROJECT_LABEL = "omni_project"

#: Default and ceiling for how many passages one call returns.
_DEFAULT_LIMIT = 5
_MAX_LIMIT = 20


def _resolve_project(ctx: ToolContext) -> str | None:
    """
    Find the project this session is filed under.

    :param ctx: The tool context; its ``conversation_id`` carries the labels.
    :returns: The project name, or ``None`` when the session is in no project
        (or the lookup fails, which reads the same to the caller).
    """
    if not ctx.conversation_id:
        return None
    try:
        from omnicraft.runtime import get_conversation_store

        conv = get_conversation_store().get_conversation(ctx.conversation_id)
    except Exception:
        return None
    if conv is None or not conv.labels:
        return None
    return conv.labels.get(_PROJECT_LABEL) or None


def _document_store():
    """Build a document store on the same database the file store uses."""
    from omnicraft.runtime import get_file_store
    from omnicraft.stores.project_document_store.sqlalchemy_store import (
        SqlAlchemyProjectDocumentStore,
    )

    file_store = get_file_store()
    if file_store is None:
        return None
    return SqlAlchemyProjectDocumentStore(file_store.storage_location)


class ProjectKnowledgeTool(Tool):
    """Search the knowledge base of the project the session belongs to."""

    @classmethod
    def name(cls) -> str:
        return "project_knowledge"

    @classmethod
    def description(cls) -> str:
        return (
            "Search the documents uploaded to this session's project — its "
            "knowledge base. Use it when the answer may live in the project's "
            "own material (contracts, specs, notes) rather than in the "
            "conversation or the repo. Returns the matching passages with the "
            "file each came from, so you can cite the source. Only works in a "
            "session filed under a project."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to look for, in natural language or keywords.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                f"How many passages to return (default {_DEFAULT_LIMIT}, "
                                f"max {_MAX_LIMIT})."
                            ),
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """Search the session's project base and format the hits for the model."""
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            return "Erro: argumentos inválidos (JSON malformado)."
        query = str(args.get("query") or "").strip()
        if not query:
            return "Erro: 'query' é obrigatório."
        try:
            limit = int(args.get("limit") or _DEFAULT_LIMIT)
        except (TypeError, ValueError):
            limit = _DEFAULT_LIMIT
        limit = max(1, min(limit, _MAX_LIMIT))

        project = _resolve_project(ctx)
        if project is None:
            return (
                "Esta sessão não está em nenhum projeto, então não há base de "
                "conhecimento para consultar. Mova a sessão para um projeto na "
                "barra lateral para usar esta ferramenta."
            )

        store = _document_store()
        if store is None:
            return "Erro: base de conhecimento indisponível nesta sessão."

        hits = store.search(project, query, limit=limit)
        if not hits:
            return (
                f"Nada encontrado na base do projeto {project!r} para {query!r}. "
                "A base pode estar vazia, ou o documento pode não ter texto "
                "extraível (um PDF digitalizado, por exemplo)."
            )
        parts = [f"{len(hits)} trecho(s) da base do projeto {project!r}:"]
        for hit in hits:
            parts.append(f"\n— {hit.filename} (trecho {hit.chunk_index + 1})\n{hit.text}")
        return "\n".join(parts)
