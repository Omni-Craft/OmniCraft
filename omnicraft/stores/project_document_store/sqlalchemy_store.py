"""SQLAlchemy-backed project document store."""

from __future__ import annotations

import uuid

from sqlalchemy import delete as sa_delete
from sqlalchemy import desc, select

from omnicraft.db.db_models import SqlProjectDocument, current_workspace_id
from omnicraft.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)
from omnicraft.entities import ProjectDocument
from omnicraft.stores.project_document_store import ProjectDocumentStore


def generate_project_document_id() -> str:
    """
    Generate a unique project-document identifier.

    :returns: A string of the form ``"pdoc_<32-char hex>"``.
    """
    return f"pdoc_{uuid.uuid4().hex}"


def _to_entity(row: SqlProjectDocument) -> ProjectDocument:
    """
    Convert a :class:`SqlProjectDocument` ORM row to its entity.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`ProjectDocument` dataclass instance.
    """
    return ProjectDocument(
        id=row.id,
        project=row.project,
        created_at=row.created_at,
        filename=row.filename,
        bytes=row.bytes,
        content_type=row.content_type,
        text_chars=row.text_chars,
    )


class SqlAlchemyProjectDocumentStore(ProjectDocumentStore):
    """SQLAlchemy-backed implementation of :class:`ProjectDocumentStore`."""

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the store.

        :param storage_location: SQLAlchemy database URI.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    def create(
        self,
        project: str,
        filename: str,
        bytes: int,
        content_type: str | None = None,
        text_chars: int = 0,
    ) -> ProjectDocument:
        """Record a new document. See :meth:`ProjectDocumentStore.create`."""
        row = SqlProjectDocument(
            id=generate_project_document_id(),
            project=project,
            created_at=now_epoch(),
            filename=filename,
            bytes=bytes,
            content_type=content_type,
            text_chars=text_chars,
        )
        with self._session() as session:
            session.add(row)
            return _to_entity(row)

    def get(self, document_id: str, project: str | None = None) -> ProjectDocument | None:
        """Fetch one document. See :meth:`ProjectDocumentStore.get`."""
        with self._session() as session:
            row = session.get(SqlProjectDocument, (current_workspace_id(), document_id))
            if row is None:
                return None
            if project is not None and row.project != project:
                return None
            return _to_entity(row)

    def list(self, project: str) -> list[ProjectDocument]:
        """List a project's documents. See :meth:`ProjectDocumentStore.list`."""
        with self._session() as session:
            rows = (
                session.execute(
                    select(SqlProjectDocument)
                    .where(
                        SqlProjectDocument.workspace_id == current_workspace_id(),
                        SqlProjectDocument.project == project,
                    )
                    .order_by(desc(SqlProjectDocument.created_at), desc(SqlProjectDocument.id))
                )
                .scalars()
                .all()
            )
            return [_to_entity(r) for r in rows]

    def delete(self, document_id: str, project: str | None = None) -> bool:
        """Delete one document. See :meth:`ProjectDocumentStore.delete`."""
        with self._session() as session:
            row = session.get(SqlProjectDocument, (current_workspace_id(), document_id))
            if row is None:
                return False
            if project is not None and row.project != project:
                return False
            session.delete(row)
            return True

    def delete_all_for_project(self, project: str) -> list[str]:
        """Delete a project's documents. See :meth:`delete_all_for_project`."""
        with self._session() as session:
            ids = (
                session.execute(
                    select(SqlProjectDocument.id).where(
                        SqlProjectDocument.workspace_id == current_workspace_id(),
                        SqlProjectDocument.project == project,
                    )
                )
                .scalars()
                .all()
            )
            if ids:
                session.execute(
                    sa_delete(SqlProjectDocument).where(
                        SqlProjectDocument.workspace_id == current_workspace_id(),
                        SqlProjectDocument.project == project,
                    )
                )
            return list(ids)
