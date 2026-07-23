"""Project document store — metadata for a project's knowledge base."""

from __future__ import annotations

from abc import ABC, abstractmethod

from omnicraft.entities import KnowledgeHit, ProjectDocument


class ProjectDocumentStore(ABC):
    """
    Abstract base for project knowledge-base document metadata.

    Tracks the documents attached to a project (filename, size, type, how much
    text was extracted). Binary content is managed separately by
    :class:`ArtifactStore`, keyed by the document id.

    Projects are implicit — a project exists while at least one conversation
    carries the ``omni_project`` label — so every method is scoped by project
    NAME rather than by a project id.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///omnicraft.db"``.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create(
        self,
        project: str,
        filename: str,
        bytes: int,
        content_type: str | None = None,
        text_chars: int = 0,
    ) -> ProjectDocument:
        """
        Record a new document. Generates a unique document id.

        :param project: Project name the document belongs to.
        :param filename: Original filename, e.g. ``"contrato.pdf"``.
        :param bytes: Content size in bytes.
        :param content_type: MIME type, e.g. ``"application/pdf"``.
        :param text_chars: Characters of text extracted for the index.
        :returns: The newly created :class:`ProjectDocument`.
        """
        ...

    @abstractmethod
    def get(self, document_id: str, project: str | None = None) -> ProjectDocument | None:
        """
        Fetch one document's metadata.

        :param document_id: Unique document identifier.
        :param project: When set, only return the document if it belongs to
            that project — the ownership check for project-scoped routes.
        :returns: The document, or ``None`` when absent or owned elsewhere.
        """
        ...

    @abstractmethod
    def list(self, project: str) -> list[ProjectDocument]:
        """
        List a project's documents, newest first.

        :param project: Project name.
        :returns: The project's documents.
        """
        ...

    @abstractmethod
    def delete(self, document_id: str, project: str | None = None) -> bool:
        """
        Delete one document's metadata row.

        :param document_id: Unique document identifier.
        :param project: When set, only delete if it belongs to that project.
        :returns: ``True`` when a row was deleted.
        """
        ...

    @abstractmethod
    def delete_all_for_project(self, project: str) -> list[str]:
        """
        Delete every document of a project — used when a project is removed.

        :param project: Project name.
        :returns: The ids that were deleted, so the caller can drop their bytes.
        """
        ...

    @abstractmethod
    def add_chunks(self, document_id: str, project: str, chunks: list[str]) -> int:
        """
        Store a document's retrievable text, replacing anything indexed before.

        :param document_id: The document the chunks belong to.
        :param project: Project name, denormalised so search filters without a join.
        :param chunks: Ordered chunks from ``chunk_text``.
        :returns: How many chunks were stored.
        """
        ...

    @abstractmethod
    def search(self, project: str, query: str, limit: int = 5) -> list[KnowledgeHit]:
        """
        Find the passages of a project's base that best match a query.

        :param project: Project name to search within — never crosses projects.
        :param query: Free-text query.
        :param limit: Maximum passages to return.
        :returns: Hits ordered by score, best first. Empty when the query has
            no usable tokens or nothing matched.
        """
        ...

    @abstractmethod
    def delete_chunks(self, document_id: str) -> int:
        """
        Drop a document's indexed text.

        :param document_id: The document whose chunks to remove.
        :returns: How many chunks were deleted.
        """
        ...
