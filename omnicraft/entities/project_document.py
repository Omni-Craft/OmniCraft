"""Project knowledge-base document entity."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProjectDocument:
    """
    A document attached to a project's knowledge base.

    Projects themselves are implicit — a project exists while at least one
    conversation carries the ``omni_project`` label — so a document is keyed by
    the project NAME rather than by a project row.

    Binary content lives in the :class:`ArtifactStore` under the document id;
    this entity is the metadata half.

    :param id: Unique document identifier, e.g. ``"pdoc_a1b2c3..."``.
    :param project: Project name this document belongs to, e.g. ``"OmniCraft"``.
    :param created_at: Unix epoch seconds when the document was uploaded.
    :param filename: Original filename, e.g. ``"contrato.pdf"``.
    :param bytes: Size of the stored content in bytes.
    :param content_type: MIME type, e.g. ``"application/pdf"``. ``None`` when
        the uploader did not provide one.
    :param text_chars: Characters of text extracted for the search index. ``0``
        means nothing searchable was extracted (an image-only PDF, say) — the
        file is still stored and downloadable, just not findable by content.
    """

    id: str
    project: str
    created_at: int
    filename: str
    bytes: int
    content_type: str | None = None
    text_chars: int = 0
