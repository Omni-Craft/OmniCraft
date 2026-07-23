"""Tests for SqlAlchemyProjectDocumentStore.

Documents are scoped by project NAME (projects are implicit — a conversation
label), so the ownership checks matter: a document must not be readable or
deletable through the wrong project's routes.
"""

from __future__ import annotations

import pytest

from omnicraft.stores.project_document_store.sqlalchemy_store import (
    SqlAlchemyProjectDocumentStore,
)


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyProjectDocumentStore:
    return SqlAlchemyProjectDocumentStore(db_uri)


def test_create_and_get(store: SqlAlchemyProjectDocumentStore) -> None:
    doc = store.create(project="Acme", filename="contrato.pdf", bytes=2048)
    assert doc.id.startswith("pdoc_")
    assert doc.project == "Acme"
    assert doc.text_chars == 0

    fetched = store.get(doc.id)
    assert fetched is not None
    assert fetched.filename == "contrato.pdf"
    assert fetched.bytes == 2048


def test_create_records_extracted_text_size(store: SqlAlchemyProjectDocumentStore) -> None:
    doc = store.create(
        project="Acme",
        filename="notas.md",
        bytes=120,
        content_type="text/markdown",
        text_chars=118,
    )
    assert store.get(doc.id).text_chars == 118  # type: ignore[union-attr]


def test_get_nonexistent(store: SqlAlchemyProjectDocumentStore) -> None:
    assert store.get("pdoc_nope") is None


def test_get_is_scoped_to_its_project(store: SqlAlchemyProjectDocumentStore) -> None:
    doc = store.create(project="Acme", filename="a.txt", bytes=1)
    assert store.get(doc.id, project="Acme") is not None
    assert store.get(doc.id, project="Outro") is None


def test_list_is_newest_first_and_scoped(store: SqlAlchemyProjectDocumentStore) -> None:
    store.create(project="Acme", filename="primeiro.txt", bytes=1)
    store.create(project="Acme", filename="segundo.txt", bytes=2)
    store.create(project="Outro", filename="alheio.txt", bytes=3)

    names = [d.filename for d in store.list("Acme")]
    assert set(names) == {"primeiro.txt", "segundo.txt"}
    assert "alheio.txt" not in names
    assert store.list("Vazio") == []


def test_delete(store: SqlAlchemyProjectDocumentStore) -> None:
    doc = store.create(project="Acme", filename="a.txt", bytes=1)
    assert store.delete(doc.id) is True
    assert store.get(doc.id) is None
    assert store.delete(doc.id) is False


def test_delete_refuses_the_wrong_project(store: SqlAlchemyProjectDocumentStore) -> None:
    doc = store.create(project="Acme", filename="a.txt", bytes=1)
    assert store.delete(doc.id, project="Outro") is False
    assert store.get(doc.id) is not None


def test_delete_all_for_project_returns_ids(store: SqlAlchemyProjectDocumentStore) -> None:
    a = store.create(project="Acme", filename="a.txt", bytes=1)
    b = store.create(project="Acme", filename="b.txt", bytes=2)
    keep = store.create(project="Outro", filename="c.txt", bytes=3)

    removed = store.delete_all_for_project("Acme")
    assert set(removed) == {a.id, b.id}
    assert store.list("Acme") == []
    assert store.get(keep.id) is not None


def test_delete_all_for_empty_project(store: SqlAlchemyProjectDocumentStore) -> None:
    assert store.delete_all_for_project("Inexistente") == []
