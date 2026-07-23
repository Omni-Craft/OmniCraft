"""Project knowledge base — upload, list, download and search a project's documents.

Attachments in OmniCraft are per session, so a document that matters to every
conversation in a project had to be re-uploaded into each one. These routes give
a project its own shelf: upload once, and every session in that project can find
it through the ``project_knowledge`` tool.

Projects are implicit — one exists while a conversation carries the
``omni_project`` label — so everything here is keyed by the project NAME, and
every read and write is scoped to it. A document is never reachable through
another project's path, even with its id.
"""

from __future__ import annotations

import contextlib
import mimetypes
from typing import Any

from fastapi import APIRouter, File, Request, Response, UploadFile

from omnicraft.errors import ErrorCode, OmniCraftError
from omnicraft.runtime.content_resolver import (
    MAX_ATTACHMENT_UPLOAD_BYTES,
    _resolve_content_type,
    attachment_upload_limit,
)
from omnicraft.runtime.project_knowledge import chunk_text, extract_text
from omnicraft.server.auth import AuthProvider
from omnicraft.server.routes._auth_helpers import require_user
from omnicraft.server.routes.sessions import _read_upload_capped
from omnicraft.stores.artifact_store import ArtifactStore
from omnicraft.stores.project_document_store import ProjectDocumentStore

#: Cap on how many passages one search returns, whoever asks.
_MAX_SEARCH_LIMIT = 20

#: Module-level singleton — FastAPI needs the marker as a default, and calling
#: File() inline in the signature trips B008.
_FILE_FIELD = File(...)


def _document_json(doc: Any) -> dict[str, Any]:
    """Serialize a :class:`ProjectDocument` for the API."""
    return {
        "id": doc.id,
        "object": "project_document",
        "project": doc.project,
        "created_at": doc.created_at,
        "filename": doc.filename,
        "bytes": doc.bytes,
        "content_type": doc.content_type,
        "text_chars": doc.text_chars,
        # Derived so the UI can say "stored but not searchable" without
        # re-deriving the rule.
        "searchable": doc.text_chars > 0,
    }


def create_project_knowledge_router(
    document_store: ProjectDocumentStore,
    artifact_store: ArtifactStore,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the router for ``/v1/projects/{project}/...``."""
    router = APIRouter()

    @router.get("/projects/{project}/documents")
    async def list_documents(request: Request, project: str) -> dict[str, Any]:
        """List a project's knowledge-base documents, newest first."""
        require_user(request, auth_provider)
        docs = document_store.list(project)
        return {
            "object": "list",
            "data": [_document_json(d) for d in docs],
            "searchable_count": sum(1 for d in docs if d.text_chars > 0),
        }

    @router.post("/projects/{project}/documents", status_code=201)
    async def upload_document(
        request: Request, project: str, file: UploadFile = _FILE_FIELD
    ) -> dict[str, Any]:
        """Store a document and index whatever text it yields."""
        require_user(request, auth_provider)
        # Type first, then read: the limit depends on the type, and reading
        # under a cap means an oversized upload never buffers past it.
        content_type = _resolve_content_type(file.content_type, file.filename)
        limit = attachment_upload_limit(content_type)
        if limit is None:
            raise OmniCraftError(
                f"Tipo não suportado: {content_type}. Envie texto, código, PDF ou imagem.",
                code=ErrorCode.INVALID_INPUT,
            )
        content = await _read_upload_capped(file, min(limit, MAX_ATTACHMENT_UPLOAD_BYTES))
        if not content:
            raise OmniCraftError("Arquivo vazio", code=ErrorCode.INVALID_INPUT)

        filename = file.filename or "documento"
        text = extract_text(content, filename, content_type)
        chunks = chunk_text(text)

        doc = document_store.create(
            project=project,
            filename=filename,
            bytes=len(content),
            content_type=content_type,
            text_chars=len(text),
        )
        # Bytes first would orphan on a metadata failure; metadata first only
        # risks a row whose content is missing, which the download reports.
        artifact_store.put(doc.id, content)
        if chunks:
            document_store.add_chunks(doc.id, project, chunks)
        return _document_json(doc) | {"chunks": len(chunks)}

    @router.get("/projects/{project}/documents/{document_id}/content")
    async def download_document(request: Request, project: str, document_id: str) -> Response:
        """Return a document's bytes."""
        require_user(request, auth_provider)
        doc = document_store.get(document_id, project=project)
        if doc is None:
            raise OmniCraftError("Documento não encontrado", code=ErrorCode.NOT_FOUND)
        try:
            content = artifact_store.get(doc.id)
        except Exception as exc:
            raise OmniCraftError(
                "Conteúdo do documento não encontrado", code=ErrorCode.NOT_FOUND
            ) from exc
        media_type = doc.content_type or (
            mimetypes.guess_type(doc.filename)[0] or "application/octet-stream"
        )
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{doc.filename}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.delete("/projects/{project}/documents/{document_id}", status_code=204)
    async def delete_document(request: Request, project: str, document_id: str) -> Response:
        """Remove a document, its indexed text and its bytes."""
        require_user(request, auth_provider)
        if not document_store.delete(document_id, project=project):
            raise OmniCraftError("Documento não encontrado", code=ErrorCode.NOT_FOUND)
        document_store.delete_chunks(document_id)
        # Bytes already gone is still a successful delete.
        with contextlib.suppress(Exception):
            artifact_store.delete(document_id)
        return Response(status_code=204)

    @router.get("/projects/{project}/knowledge/search")
    async def search_knowledge(
        request: Request, project: str, q: str = "", limit: int = 5
    ) -> dict[str, Any]:
        """Find the passages of a project's base that best match ``q``."""
        require_user(request, auth_provider)
        capped = max(1, min(limit, _MAX_SEARCH_LIMIT))
        hits = document_store.search(project, q, limit=capped)
        return {
            "object": "list",
            "query": q,
            "data": [
                {
                    "document_id": h.document_id,
                    "filename": h.filename,
                    "chunk_index": h.chunk_index,
                    "text": h.text,
                    "score": h.score,
                }
                for h in hits
            ],
        }

    return router
